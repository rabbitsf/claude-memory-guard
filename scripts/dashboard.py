#!/usr/bin/env python3
"""
dashboard.py — read-only web dashboard for claude-memory-guard.

Serves http://localhost:37778 with the state of every project that has a
MEMORY.md under ~/.claude/projects/: active goal, in-progress block,
canonicals, decisions, plan-file progress, CHANGELOG_AI.md and
PROJECT_GUIDE.md. Files are re-read on every request; nothing is cached
and nothing is ever written to a project.

Usage:
  dashboard.py serve    run the server in the foreground
  dashboard.py start    start it in the background if not already running
  dashboard.py stop     stop the background server
  dashboard.py status   report whether it is running

The SessionStart hook (session_start_reminder.py) calls ensure_running(),
so the dashboard comes up with the first Claude Code session.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# MEMORY.md parsing lives in the SessionStart hook; reuse it, don't copy it.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_start_reminder import (  # noqa: E402
    active_goal,
    active_status,
    days_since_last_update,
    encode_project_path,
    inprogress_is_filled,
    plan_file,
    stale_canonicals,
)

HOST = "127.0.0.1"
PORT = 37778
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
STATE_DIR = Path.home() / ".claude" / "memory-guard-dashboard"
PID_FILE = STATE_DIR / "dashboard.pid"
LOG_FILE = STATE_DIR / "dashboard.log"


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------

def is_running() -> bool:
    """True if something is listening on the dashboard port."""
    try:
        with socket.create_connection((HOST, PORT), timeout=0.2):
            return True
    except OSError:
        return False


def ensure_running() -> bool:
    """Start the server as a detached background process unless it is
    already up. Returns True if a new process was spawned. Never blocks."""
    if is_running():
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survive the hook / Claude Code exiting
        )
    return True


def _pid_if_ours() -> int | None:
    """PID from the pid file, only if that process is still dashboard.py."""
    try:
        pid = int(PID_FILE.read_text().strip())
        cmd = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True,
        ).stdout
    except (OSError, ValueError):
        return None
    return pid if "dashboard.py" in cmd else None


def stop() -> bool:
    pid = _pid_if_ours()
    if pid is None:
        PID_FILE.unlink(missing_ok=True)
        return False
    os.kill(pid, signal.SIGTERM)
    return True


# ---------------------------------------------------------------------------
# Project discovery
# ---------------------------------------------------------------------------

def resolve_project_dir(encoded: str) -> tuple[Path, str]:
    """Map an encoded ~/.claude/projects/<name> back to its real directory.

    The encoding is lossy ('/' and ' ' both become '-'), so walk the
    filesystem from '/' and match each child's encoded name as a prefix.
    Returns (deepest existing directory, unmatched remainder); the
    remainder is "" when the project folder itself exists.
    """
    def walk(cur: Path, rest: str) -> tuple[Path, str]:
        best = (cur, rest)
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            return best
        for name in names:
            enc = encode_project_path("/" + name)
            if rest != enc and not rest.startswith(enc + "-"):
                continue
            child = cur / name
            if not child.is_dir():
                continue
            found = walk(child, rest[len(enc):])
            if not found[1]:
                return found
            if len(found[1]) < len(best[1]):
                best = found
        return best

    return walk(Path("/"), encoded)


def memory_dirs() -> list[tuple[Path, Path | None, str]]:
    """(memory dir, project dir or None, display name) for each project.

    Skips folders that only contain projects (home, a projects root) and
    hidden tool folders such as claude-mem's observer sessions.
    """
    if not CLAUDE_PROJECTS.is_dir():
        return []
    found = []
    for d in sorted(CLAUDE_PROJECTS.iterdir()):
        if not (d / "memory" / "MEMORY.md").is_file():
            continue
        path, rest = resolve_project_dir(d.name)
        if rest:  # project folder is gone; name it by the unmatched tail
            found.append((d, None, rest.lstrip("-")))
        elif path != Path.home() and not any(p.startswith(".") for p in path.parts):
            found.append((d, path, path.name))
    containers = {p.parent for _, p, _ in found if p}
    return [f for f in found if f[1] not in containers]


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------

SECTIONS = {
    "ACTIVE": "Active Session Context",
    "INPROGRESS": "In-Progress Work Block",
    "CANONICAL": "Canonical Implementations",
    "DECISIONS": "Key Decisions",
    "PREFERENCES": "Workflow Preferences",
}


def section(content: str, key: str) -> str:
    """Body of a MEMORY.md section, comments stripped.

    Uses the <!-- SECTION: X --> markers when present, otherwise falls back
    to the '## Heading' (older MEMORY.md files have no markers).
    """
    m = re.search(
        rf"<!-- SECTION: {key} -->(.*?)<!-- END: {key} -->", content, re.DOTALL
    )
    if not m:
        m = re.search(
            rf"^## {re.escape(SECTIONS[key])}[^\n]*\n(.*?)(?=^## |\Z)",
            content, re.DOTALL | re.MULTILINE,
        )
    if not m:
        return ""
    body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL)
    body = re.sub(rf"^## {re.escape(SECTIONS[key])}.*$", "", body, flags=re.MULTILINE)
    return body.strip()


def items(body: str) -> list[str]:
    """Non-empty lines of a section, minus leading list markers."""
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(re.sub(r"^[-*]\s+", "", line))
    return out


def field(body: str, name: str) -> str:
    m = re.search(rf"^- {name}:\s*(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


CHECKBOX = re.compile(r"^\s*[-*] \[([ xX])\]", re.MULTILINE)


def parse_plan(path: Path, with_content: bool) -> dict:
    text = read(path) or ""
    boxes = CHECKBOX.findall(text)
    title = re.search(r"^# (?!Progress:)(.+)$", text, re.MULTILINE)
    progress = re.search(r"^# Progress:\s*(.+)$", text, re.MULTILINE)
    plan = {
        "file": path.name,
        "title": title.group(1).strip() if title else path.stem,
        "progress": progress.group(1).strip() if progress else "",
        "done": sum(1 for b in boxes if b in "xX"),
        "total": len(boxes),
        "mtime": mtime(path),
    }
    if with_content:
        plan["content"] = text
    return plan


def parse_changelog(text: str) -> list[dict]:
    entries = []
    for chunk in re.split(r"^(?=## )", text, flags=re.MULTILINE):
        if not chunk.startswith("## "):
            continue
        heading, _, body = chunk.partition("\n")
        date = re.search(r"\d{4}-\d{2}-\d{2}", heading)
        entries.append({
            "title": heading[3:].strip(),
            "date": date.group(0) if date else "",
            "body": body.strip().rstrip("-").strip(),
        })
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


def project_data(mem_dir: Path, proj: Path | None, name: str,
                 detail: bool = False) -> dict:
    memory_path = mem_dir / "memory" / "MEMORY.md"
    memory = read(memory_path) or ""
    docs = proj / "docs" if proj else None

    guide_path = docs / "PROJECT_GUIDE.md" if docs else None
    changelog_path = docs / "CHANGELOG_AI.md" if docs else None
    plan_paths = sorted((docs / "plans").glob("*.md"), reverse=True) if docs else []

    active = section(memory, "ACTIVE")
    changelog_text = read(changelog_path) if changelog_path else None
    changelog = parse_changelog(changelog_text) if changelog_text else []
    plans = [parse_plan(p, detail) for p in plan_paths]

    updated = re.search(r"# Last updated:\s*(.+)$", memory, re.MULTILINE)
    times = [mtime(memory_path)] + [p["mtime"] for p in plans]
    times += [mtime(p) for p in (guide_path, changelog_path) if p]

    data = {
        "id": mem_dir.name,
        "name": name,
        "path": str(proj) if proj else None,
        "last_updated": updated.group(1).strip() if updated else "",
        "days": days_since_last_update(memory),
        "last_activity": max((t for t in times if t), default=None),
        "goal": active_goal(memory),
        "status": active_status(memory),
        "started": field(active, "Started"),
        "files_touched": field(active, "Files touched"),
        "inprogress": inprogress_is_filled(memory),
        "plan_file": plan_file(memory),
        "canonicals": items(section(memory, "CANONICAL")),
        "decisions": items(section(memory, "DECISIONS")),
        "stale_canonicals": stale_canonicals(memory, str(proj)) if proj else [],
        "has_guide": bool(guide_path and guide_path.is_file()),
        "has_changelog": changelog_text is not None,
        "changelog_count": len(changelog),
        "changelog_latest": changelog[0]["date"] if changelog else "",
        "plans": plans,
    }
    if detail:
        data["inprogress_text"] = section(memory, "INPROGRESS")
        data["preferences"] = section(memory, "PREFERENCES")
        data["memory_raw"] = memory
        data["changelog"] = changelog
        data["guide"] = read(guide_path) if data["has_guide"] else None
    return data


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Only answer requests addressed to localhost (blocks DNS rebinding).
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self.send(403, "text/plain", b"Forbidden")
        url = urlparse(self.path)
        try:
            if url.path == "/":
                return self.send(200, "text/html; charset=utf-8", PAGE.encode())
            if url.path == "/api/projects":
                return self.json({
                    "generated": datetime.now().isoformat(timespec="seconds"),
                    "projects": [project_data(*m) for m in memory_dirs()],
                })
            if url.path == "/api/project":
                pid = parse_qs(url.query).get("id", [""])[0]
                match = [m for m in memory_dirs() if m[0].name == pid]
                if not match:
                    return self.json({"error": "not found"}, 404)
                return self.json(project_data(*match[0], detail=True))
            if url.path == "/api/health":
                return self.json({"app": "claude-memory-guard-dashboard"})
            self.send(404, "text/plain", b"Not found")
        except Exception as exc:  # keep serving even if one file is odd
            self.json({"error": str(exc)}, 500)

    def json(self, obj, code=200):
        self.send(code, "application/json", json.dumps(obj).encode())

    def send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] dashboard on http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory Guard Dashboard</title>
<style>
:root {
  --bg: #f6f5f1; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a64; --line: #e3e1da;
  --accent: #2f6f5e; --accent-soft: #e2efe9; --warn: #a2541b; --warn-soft: #f8eadf;
  --busy: #7a4fb0; --busy-soft: #eee6f7; --code: #f0eee8;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161614; --panel: #1f1f1c; --ink: #ecebe6; --muted: #9a998f; --line: #33332e;
    --accent: #6fc2a8; --accent-soft: #1f3530; --warn: #e3a06b; --warn-soft: #3a2a1d;
    --busy: #b897e6; --busy-soft: #2e2540; --code: #2a2a26;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
header { position: sticky; top: 0; z-index: 2; background: var(--bg);
  border-bottom: 1px solid var(--line); padding: 12px 24px; display: flex; gap: 16px;
  align-items: center; flex-wrap: wrap; }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
header h1 a { color: var(--ink); }
header .gen { color: var(--muted); font-size: 12px; margin-left: auto; }
main { max-width: 1200px; margin: 0 auto; padding: 20px 24px 60px; }
input[type=search] { flex: 1 1 260px; max-width: 420px; padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); font: inherit; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 14px; min-width: 130px; }
.stat b { display: block; font-size: 22px; font-variant-numeric: tabular-nums; }
.stat span { color: var(--muted); font-size: 12px; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.chip { border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  padding: 4px 12px; border-radius: 999px; cursor: pointer; font: inherit; font-size: 13px; }
.chip.on { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; color: var(--ink); }
.card:hover { border-color: var(--accent); text-decoration: none; }
.card .top { display: flex; align-items: baseline; gap: 8px; }
.card h3 { margin: 0; font-size: 15px; flex: 1; overflow-wrap: anywhere; }
.goal { color: var(--muted); display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; min-height: 2.9em; }
.meta { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--muted); font-size: 12px; }
.badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  white-space: nowrap; background: var(--code); color: var(--muted); }
.badge.busy { background: var(--busy-soft); color: var(--busy); }
.badge.ok { background: var(--accent-soft); color: var(--accent); }
.badge.warn { background: var(--warn-soft); color: var(--warn); }
.bar { height: 6px; background: var(--code); border-radius: 3px; overflow: hidden; }
.bar i { display: block; height: 100%; background: var(--accent); }
.warns { display: flex; gap: 6px; flex-wrap: wrap; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted);
  margin: 28px 0 10px; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 18px; }
.kv { display: grid; grid-template-columns: 120px 1fr; gap: 6px 14px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; overflow-wrap: anywhere; }
ul.list { margin: 0; padding-left: 18px; }
ul.list li { margin: 4px 0; overflow-wrap: anywhere; }
li.stale { color: var(--warn); }
details { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 8px; }
details > summary { cursor: pointer; padding: 10px 14px; list-style: none; display: flex;
  gap: 10px; align-items: center; }
details > summary::-webkit-details-marker { display: none; }
details > summary::before { content: "▸"; color: var(--muted); }
details[open] > summary::before { content: "▾"; }
details .body { padding: 0 18px 14px; border-top: 1px solid var(--line); }
summary .grow { flex: 1; overflow-wrap: anywhere; }
summary .date { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.md { overflow-wrap: anywhere; }
.md h1, .md h2, .md h3, .md h4 { text-transform: none; letter-spacing: 0; color: var(--ink);
  margin: 16px 0 6px; }
.md h1 { font-size: 18px; } .md h2 { font-size: 16px; } .md h3, .md h4 { font-size: 14px; }
.md code { background: var(--code); padding: 1px 5px; border-radius: 4px; font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.md pre { background: var(--code); padding: 10px 12px; border-radius: 8px; overflow-x: auto; }
.md pre code { background: none; padding: 0; }
.md table { border-collapse: collapse; display: block; overflow-x: auto; margin: 8px 0; }
.md td, .md th { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
.md blockquote { margin: 8px 0; padding-left: 12px; border-left: 3px solid var(--line); color: var(--muted); }
.md li.task { list-style: none; margin-left: -18px; }
.results .hit { padding: 8px 0; border-bottom: 1px solid var(--line); }
.results .hit:last-child { border-bottom: 0; }
.results .src { font-size: 12px; color: var(--muted); }
mark { background: var(--warn-soft); color: inherit; }
.muted { color: var(--muted); }
.empty { color: var(--muted); padding: 30px; text-align: center; }
@media (max-width: 600px) {
  header, main { padding-left: 16px; padding-right: 16px; }
  .grid { grid-template-columns: 1fr; }
  .kv { grid-template-columns: 1fr; }
  .kv dt { margin-top: 6px; }
}
</style>
</head>
<body>
<header>
  <h1><a href="#/">Memory Guard</a></h1>
  <input type="search" id="q" placeholder="Search projects, canonicals, decisions…" autocomplete="off">
  <span class="gen" id="gen"></span>
</header>
<main id="app"><div class="empty">Loading…</div></main>
<script>
const $ = s => document.querySelector(s);
const app = $('#app');
let projects = [], filter = 'all';

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const inline = s => esc(s)
  .replace(/`([^`]+)`/g, '<code>$1</code>')
  .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

function md(text) {
  const lines = String(text || '').replace(/<!--[\s\S]*?-->/g, '').split('\n');
  let out = [], list = null, table = null, para = [], fence = null;
  const flushPara = () => { if (para.length) out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; };
  const flushList = () => { if (list) out.push('<ul>' + list.join('') + '</ul>'); list = null; };
  const flushTable = () => {
    if (!table) return;
    const rows = table.filter(r => !/^\|?\s*:?-{2,}/.test(r));
    out.push('<table>' + rows.map((r, i) => '<tr>' + r.replace(/^\||\|$/g, '').split('|')
      .map(c => i ? `<td>${inline(c.trim())}</td>` : `<th>${inline(c.trim())}</th>`).join('') + '</tr>').join('') + '</table>');
    table = null;
  };
  const flush = () => { flushPara(); flushList(); flushTable(); };
  for (const line of lines) {
    if (fence !== null) {
      if (/^\s*```/.test(line)) { out.push('<pre><code>' + esc(fence.join('\n')) + '</code></pre>'); fence = null; }
      else fence.push(line);
      continue;
    }
    if (/^\s*```/.test(line)) { flush(); fence = []; continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) { flush(); const n = Math.min(m[1].length + 1, 4); out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if (/^\s*\|/.test(line)) { flushPara(); flushList(); (table = table || []).push(line.trim()); continue; }
    if ((m = line.match(/^\s*[-*]\s+\[([ xX])\]\s+(.*)/))) { flushPara(); flushTable(); (list = list || []).push(`<li class="task">${m[1] === ' ' ? '☐' : '☑'} ${inline(m[2])}</li>`); continue; }
    if ((m = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/))) { flushPara(); flushTable(); (list = list || []).push('<li>' + inline(m[1]) + '</li>'); continue; }
    if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flush(); continue; }
    if ((m = line.match(/^>\s?(.*)/))) { flush(); out.push('<blockquote>' + inline(m[1]) + '</blockquote>'); continue; }
    if (!line.trim()) { flush(); continue; }
    flushList(); flushTable(); para.push(line.trim());
  }
  if (fence) out.push('<pre><code>' + esc(fence.join('\n')) + '</code></pre>');
  flush();
  return out.join('');
}

function ago(ts) {
  if (!ts) return '—';
  const d = Math.floor((Date.now() / 1000 - ts) / 86400);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : d < 60 ? `${d} days ago` : `${Math.round(d / 30)} months ago`;
}
const openPlans = p => p.plans.filter(x => x.total && x.done < x.total);
const DONE = /^\W*(none|complete[d]?|closed|done|shipped|reverted|—|-|$)/i;
const ACTIVE = /^\W*(in.?progress|planning|active|wip|started|blocked)/i;
function badge(p) {
  if (busy(p)) return '<span class="badge busy">In progress</span>';
  if (DONE.test(p.status || '')) return '<span class="badge ok">Clean</span>';
  const s = p.status.replace(/[*`]/g, '');
  return `<span class="badge" title="${esc(s)}">${esc(s.length > 22 ? s.slice(0, 21) + '…' : s)}</span>`;
}
function warnings(p) {
  const w = [];
  if (!p.path) w.push('folder not found');
  else {
    if (!p.has_guide) w.push('no PROJECT_GUIDE');
    if (!p.has_changelog) w.push('no CHANGELOG_AI');
  }
  if (p.stale_canonicals.length) w.push(`${p.stale_canonicals.length} stale canonical${p.stale_canonicals.length > 1 ? 's' : ''}`);
  return w;
}
const busy = p => p.inprogress || ACTIVE.test(p.status || '');

async function load() {
  const r = await fetch('/api/projects');
  const data = await r.json();
  projects = data.projects.sort((a, b) =>
    (busy(b) - busy(a)) || ((b.last_activity || 0) - (a.last_activity || 0)));
  $('#gen').textContent = 'Read ' + new Date(data.generated).toLocaleTimeString();
}

function card(p) {
  const open = openPlans(p);
  const plan = open[0];
  const w = warnings(p);
  return `<a class="card" href="#/p/${encodeURIComponent(p.id)}">
    <div class="top"><h3>${esc(p.name)}</h3>${badge(p)}</div>
    <div class="goal">${esc(!p.goal || p.goal === 'NONE' ? 'No active goal' : p.goal)}</div>
    ${plan ? `<div><div class="meta"><span>${esc(plan.title)}</span><span>${plan.done}/${plan.total}</span></div>
      <div class="bar"><i style="width:${100 * plan.done / plan.total}%"></i></div></div>` : ''}
    <div class="meta"><span>Active ${ago(p.last_activity)}</span>
      <span>${p.plans.length} plan${p.plans.length === 1 ? '' : 's'}${open.length ? ` · ${open.length} open` : ''}</span>
      <span>${p.changelog_count} changelog entr${p.changelog_count === 1 ? 'y' : 'ies'}</span></div>
    ${w.length ? `<div class="warns">${w.map(x => `<span class="badge warn">${esc(x)}</span>`).join('')}</div>` : ''}
  </a>`;
}

function renderOverview() {
  const q = $('#q').value.trim().toLowerCase();
  const filters = {
    all: () => true,
    busy,
    open: p => openPlans(p).length > 0,
    attention: p => warnings(p).length > 0,
    stale: p => p.last_activity && (Date.now() / 1000 - p.last_activity) > 30 * 86400,
  };
  const labels = { all: 'All', busy: 'In progress', open: 'Open plans', attention: 'Needs attention', stale: 'Idle 30+ days' };
  const shown = projects.filter(filters[filter]).filter(p => !q ||
    [p.name, p.goal, p.status].join(' ').toLowerCase().includes(q));

  let hits = '';
  if (q) {
    const mark = s => inline(s).replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `<mark>${m}</mark>`);
    const found = [];
    for (const p of projects)
      for (const [kind, arr] of [['Canonical', p.canonicals], ['Decision', p.decisions]])
        for (const t of arr) if (t.toLowerCase().includes(q)) found.push({ p, kind, t });
    hits = `<h2>Canonicals &amp; decisions matching “${esc(q)}” (${found.length})</h2>
      <div class="panel results">${found.length ? found.slice(0, 200).map(h => `<div class="hit">
        <div class="src"><a href="#/p/${encodeURIComponent(h.p.id)}">${esc(h.p.name)}</a> · ${h.kind}</div>
        <div class="md">${mark(h.t)}</div></div>`).join('') : '<div class="muted">No matches.</div>'}</div>`;
  }

  const n = f => projects.filter(filters[f]).length;
  app.innerHTML = `
    <div class="stats">
      <div class="stat"><b>${projects.length}</b><span>projects</span></div>
      <div class="stat"><b>${n('busy')}</b><span>in progress</span></div>
      <div class="stat"><b>${projects.reduce((s, p) => s + openPlans(p).length, 0)}</b><span>open plans</span></div>
      <div class="stat"><b>${n('attention')}</b><span>need attention</span></div>
    </div>
    <div class="chips">${Object.keys(labels).map(k =>
      `<button class="chip ${k === filter ? 'on' : ''}" data-f="${k}">${labels[k]} · ${n(k)}</button>`).join('')}</div>
    ${shown.length ? `<div class="grid">${shown.map(card).join('')}</div>` : '<div class="empty">No projects match.</div>'}
    ${hits}`;
  app.querySelectorAll('.chip').forEach(b => b.onclick = () => { filter = b.dataset.f; renderOverview(); });
}

async function renderProject(id) {
  app.innerHTML = '<div class="empty">Loading…</div>';
  const r = await fetch('/api/project?id=' + encodeURIComponent(id));
  const p = await r.json();
  if (p.error) { app.innerHTML = `<div class="empty">${esc(p.error)}</div>`; return; }
  const stale = new Set(p.stale_canonicals);
  const isStale = t => [...stale].some(s => t.includes(s));
  const list = (arr, cls) => arr.length
    ? `<ul class="list md">${arr.map(t => `<li class="${cls && cls(t) ? 'stale' : ''}">${inline(t)}${cls && cls(t) ? ' — file missing' : ''}</li>`).join('')}</ul>`
    : '<div class="muted">None recorded.</div>';
  const w = warnings(p);
  app.innerHTML = `
    <div class="top" style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">
      <h1 style="margin:0;font-size:22px">${esc(p.name)}</h1>${badge(p)}
      ${w.map(x => `<span class="badge warn">${esc(x)}</span>`).join('')}
    </div>
    <div class="muted" style="margin-top:4px;overflow-wrap:anywhere">${esc(p.path || p.id)}</div>

    <h2>Active session</h2>
    <div class="panel"><dl class="kv">
      <dt>Goal</dt><dd class="md">${inline(p.goal)}</dd>
      <dt>Status</dt><dd class="md">${inline(p.status)}</dd>
      <dt>Started</dt><dd>${esc(p.started || '—')}</dd>
      <dt>Files touched</dt><dd class="md">${inline(p.files_touched || '—')}</dd>
      <dt>MEMORY.md</dt><dd>${esc(p.last_updated || '—')}${p.days != null ? ` (${p.days} days ago)` : ''}</dd>
    </dl></div>

    <h2>In-progress work block</h2>
    <div class="panel md">${p.inprogress_text ? md(p.inprogress_text) : '<span class="muted">Empty — no task in flight.</span>'}</div>

    <h2>Plans (${p.plans.length})</h2>
    ${p.plans.length ? p.plans.map((pl, i) => `${i === 6 ? `<details class="more"><summary><span class="grow muted">${p.plans.length - 6} older plans</span></summary><div class="body" style="padding-top:10px">` : ''}<details>
      <summary><span class="grow">${esc(pl.title)}<br><span class="muted" style="font-size:12px">${esc(pl.file)}${pl.progress ? ' · ' + esc(pl.progress) : ''}</span></span>
        ${pl.total ? `<span style="width:110px"><div class="bar"><i style="width:${100 * pl.done / pl.total}%"></i></div></span>
        <span class="date">${pl.done}/${pl.total}</span>` : ''}</summary>
      <div class="body md">${md(pl.content)}</div></details>`).join('') + (p.plans.length > 6 ? '</div></details>' : '') : '<div class="panel muted">No plan files.</div>'}

    <h2>Canonical implementations</h2>
    <div class="panel">${list(p.canonicals, isStale)}</div>

    <h2>Key decisions</h2>
    <div class="panel">${list(p.decisions)}</div>

    ${p.preferences ? `<h2>Workflow preferences</h2><div class="panel md">${md(p.preferences)}</div>` : ''}

    <h2>Changelog (${p.changelog.length})</h2>
    ${p.changelog.length ? p.changelog.map((e, i) => `<details ${i === 0 ? 'open' : ''}>
      <summary><span class="grow">${inline(e.title.replace(/^\[[^\]]*\]\s*-?\s*/, ''))}</span><span class="date">${esc(e.date)}</span></summary>
      <div class="body md">${md(e.body)}</div></details>`).join('') : '<div class="panel muted">No CHANGELOG_AI.md entries.</div>'}

    <h2>Project guide</h2>
    ${p.guide ? `<details><summary><span class="grow">docs/PROJECT_GUIDE.md</span></summary><div class="body md">${md(p.guide)}</div></details>`
      : '<div class="panel muted">No docs/PROJECT_GUIDE.md.</div>'}

    <h2>Raw MEMORY.md</h2>
    <details><summary><span class="grow">Show file</span></summary><div class="body md"><pre><code>${esc(p.memory_raw)}</code></pre></div></details>`;
  window.scrollTo(0, 0);
}

async function route() {
  const m = location.hash.match(/^#\/p\/(.+)$/);
  if (m) return renderProject(decodeURIComponent(m[1]));
  await load();
  renderOverview();
}
$('#q').addEventListener('input', () => {
  if (location.hash.startsWith('#/p/')) location.hash = '#/'; else renderOverview();
});
window.addEventListener('hashchange', route);
route().catch(e => app.innerHTML = `<div class="empty">Could not load: ${esc(e.message)}</div>`);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        serve()
    elif cmd == "start":
        spawned = ensure_running()
        print(("Started" if spawned else "Already running") + f": http://localhost:{PORT}")
    elif cmd == "stop":
        print("Stopped" if stop() else "Not running")
    elif cmd == "status":
        print(f"Running: http://localhost:{PORT}" if is_running() else "Not running")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
