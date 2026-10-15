#!/usr/bin/env python3
"""
history.py — session history store for claude-memory-guard.

Canonical home of everything "episodic": what you asked, what Claude did, and
an AI summary of each session, kept in one SQLite + FTS5 database for all
projects at ~/.claude/memory-guard/history.db.

  • Prompts and actions are parsed deterministically from Claude Code
    transcripts (~/.claude/projects/<encoded>/<session>.jsonl), which Claude
    Code deletes after ~30 days — ingesting them makes them permanent.
  • Summaries come from one background `claude -p --model haiku` call per
    session (request / investigated / learned / completed / next steps).
  • `import-claude-mem` copies claude-mem history in (read-only on its side).

Usage:
    history.py backfill                    ingest all new/changed transcripts
    history.py summarize <session> | --pending N
    history.py import-claude-mem [db]
    history.py search "<terms>" [--project NAME] [-n N]
    history.py show <session-id-prefix>
    history.py recent [--project NAME] [-n N]
    history.py hook-session-end            (SessionEnd hook; reads stdin)
    history.py catchup                     (spawned by SessionStart hook)

Every hook entry point exits immediately when MEMORY_GUARD_SUMMARIZER is set,
so the summarizer's own `claude -p` run can never trigger another summary.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_start_reminder import encode_project_path  # noqa: E402

HOME = Path.home()
STORE_DIR = HOME / ".claude" / "memory-guard"
DB_PATH = STORE_DIR / "history.db"
LOG_FILE = STORE_DIR / "history.log"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_MEM_DB = HOME / ".claude-mem" / "claude-mem.db"
GUARD_ENV = "MEMORY_GUARD_SUMMARIZER"

# Transcript folders that are machinery, not user projects.
SKIP_PROJECT_DIRS = {
    encode_project_path(str(STORE_DIR)),          # our summarizer's cwd
    encode_project_path(str(HOME / ".claude-mem")),
}
SKIP_PROJECT_PREFIXES = (  # claude-mem observer sessions, both key encodings
    re.sub(r"[^a-zA-Z0-9]", "-", str(HOME / ".claude-mem")),
    str(HOME / ".claude-mem").replace("/", "-"),
)

SUMMARY_MODEL = "haiku"
SUMMARY_INPUT_CAP = 20_000      # chars of condensed transcript sent to Haiku
CATCHUP_MAX = 3                 # summaries per SessionStart catch-up
CATCHUP_DAYS = 7
MIN_PROMPTS_TO_SUMMARIZE = 2
QUIET_MINUTES = 10              # a session idle this long counts as ended

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,          -- encoded dir, same key as memory dirs
    cwd TEXT,
    title TEXT,
    started_at TEXT,
    ended_at TEXT,
    n_prompts INTEGER DEFAULT 0,
    n_actions INTEGER DEFAULT 0,
    source TEXT NOT NULL            -- 'transcript' | 'claude-mem'
);
CREATE INDEX IF NOT EXISTS ix_sessions_project ON sessions(project, started_at);
CREATE TABLE IF NOT EXISTS prompts (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    ts TEXT,
    text TEXT NOT NULL,
    PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS actions (
    session_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    ts TEXT,
    tool TEXT NOT NULL,
    kind TEXT NOT NULL,             -- read | edit | run | search | web | agent | other
    target TEXT,
    PRIMARY KEY (session_id, idx)
);
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT,
    source TEXT NOT NULL,           -- 'haiku' | 'claude-mem'
    source_id INTEGER,              -- claude-mem row id (import dedup)
    n_prompts INTEGER,              -- prompts covered when generated
    request TEXT, investigated TEXT, learned TEXT,
    completed TEXT, next_steps TEXT, notes TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_summaries_session ON summaries(session_id);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT,
    type TEXT, title TEXT, subtitle TEXT, narrative TEXT, facts TEXT, files TEXT,
    source_id INTEGER UNIQUE        -- claude-mem row id
);
CREATE INDEX IF NOT EXISTS ix_observations_session ON observations(session_id);
CREATE TABLE IF NOT EXISTS ingested (
    path TEXT PRIMARY KEY, size INTEGER, mtime REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    body, kind UNINDEXED, ref UNINDEXED, session_id UNINDEXED,
    project UNINDEXED, ts UNINDEXED, tokenize='porter unicode61'
);
"""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect(readonly: bool = False) -> sqlite3.Connection:
    """Open the store. Read-only callers (dashboard, SessionStart) never create it."""
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    else:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def index(conn, kind: str, ref: str, session_id: str, project: str,
          ts: str | None, body: str) -> None:
    if body and body.strip():
        conn.execute(
            "INSERT INTO search_fts(body, kind, ref, session_id, project, ts) "
            "VALUES (?,?,?,?,?,?)", (body, kind, ref, session_id, project, ts or ""))


def project_label(project: str, cwd: str | None = None) -> str:
    """Human name for a project: the real folder name when the cwd is known,
    else decoded from the key (encoding is lossy: '_' and ' ' became '-')."""
    if cwd:
        return Path(cwd).name or cwd
    base = encode_project_path(str(HOME / "Claude Projects"))
    if project == base:
        return "Claude Projects"
    if project.startswith(base + "-"):
        return project[len(base) + 1:]
    return project.rsplit("-", 1)[-1] or project


def log(msg: str) -> None:
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

READ_TOOLS = {"Read", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH_TOOLS = {"Grep", "Glob", "LSP", "ToolSearch"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
AGENT_TOOLS = {"Agent", "Task", "SendMessage"}


def classify(tool: str, inp: dict) -> tuple[str, str]:
    """Return (kind, short target) for a tool_use block."""
    inp = inp if isinstance(inp, dict) else {}
    if tool in READ_TOOLS:
        return "read", inp.get("file_path") or inp.get("notebook_path") or ""
    if tool in EDIT_TOOLS:
        return "edit", inp.get("file_path") or inp.get("notebook_path") or ""
    if tool == "Bash":
        return "run", (inp.get("description") or inp.get("command") or "")[:300]
    if tool in SEARCH_TOOLS:
        return "search", inp.get("pattern") or inp.get("query") or ""
    if tool in WEB_TOOLS:
        return "web", inp.get("url") or inp.get("query") or ""
    if tool in AGENT_TOOLS:
        return "agent", inp.get("description") or inp.get("summary") or ""
    if tool == "Skill":
        return "other", inp.get("skill") or ""
    return "other", ""


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


COMMAND_RE = re.compile(r"<command-name>(/[^<]+)</command-name>")
ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)


def prompt_text(rec: dict) -> str | None:
    """Return the user's typed prompt for a transcript record, or None if the
    record is a tool result, system injection, notification or meta noise."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") != "human":
        return None
    if origin is None and rec.get("promptSource") in ("sdk", "system"):
        return None
    text = text_of(content).strip()
    if not text:
        return None
    cmd = COMMAND_RE.search(text)
    if cmd:  # slash command — keep it short, drop trivial ones
        name = cmd.group(1).strip()
        if name in ("/exit", "/clear", "/statusline", "/compact", "/resume"):
            return None
        args = ARGS_RE.search(text)
        return f"{name} {args.group(1).strip()}".strip() if args else name
    if text.startswith(("<local-command", "<task-notification", "<system-reminder",
                        "Caveat:", "[Request interrupted")):
        return None
    return text


def parse_transcript(path: Path) -> dict:
    """Parse one transcript into {session fields, prompts, actions, texts}."""
    s = {"id": path.stem, "project": path.parent.name, "cwd": None, "title": None,
         "started_at": None, "ended_at": None, "prompts": [], "actions": [],
         "texts": []}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("timestamp")
            if ts:
                s["started_at"] = s["started_at"] or ts
                s["ended_at"] = ts
            if rec.get("cwd") and not s["cwd"]:
                s["cwd"] = rec["cwd"]
            if rec.get("type") == "ai-title":
                s["title"] = rec.get("aiTitle") or rec.get("title") or s["title"]
            if rec.get("type") == "user":
                text = prompt_text(rec)
                if text:
                    s["prompts"].append((ts, text))
            elif rec.get("type") == "assistant" and not rec.get("isSidechain"):
                for b in (rec.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        kind, target = classify(b.get("name", ""), b.get("input"))
                        s["actions"].append((ts, b.get("name", ""), kind, target))
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        s["texts"].append((ts, b["text"].strip()))
    return s


def ingest_transcript(conn, path: Path, force: bool = False) -> bool:
    """Ingest one transcript if new or changed. Returns True if (re)ingested."""
    try:
        st = path.stat()
    except OSError:
        return False
    row = conn.execute("SELECT size, mtime FROM ingested WHERE path=?",
                       (str(path),)).fetchone()
    if row and not force and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
        return False
    s = parse_transcript(path)
    sid, project = s["id"], s["project"]
    if not s["prompts"] and not s["actions"]:
        conn.execute("INSERT OR REPLACE INTO ingested VALUES (?,?,?)",
                     (str(path), st.st_size, st.st_mtime))
        conn.commit()
        return False

    title = s["title"]
    existing = conn.execute("SELECT title, started_at FROM sessions WHERE id=?",
                            (sid,)).fetchone()
    started = s["started_at"]
    if existing and existing["started_at"] and started:
        started = min(existing["started_at"], started)
    conn.execute(
        "INSERT INTO sessions(id, project, cwd, title, started_at, ended_at, "
        "n_prompts, n_actions, source) VALUES (?,?,?,?,?,?,?,?, 'transcript') "
        "ON CONFLICT(id) DO UPDATE SET project=excluded.project, cwd=excluded.cwd, "
        "title=COALESCE(excluded.title, sessions.title), started_at=excluded.started_at, "
        "ended_at=excluded.ended_at, n_prompts=excluded.n_prompts, "
        "n_actions=excluded.n_actions, source='transcript'",
        (sid, project, s["cwd"], title, started, s["ended_at"],
         len(s["prompts"]), len(s["actions"])))

    # Replace transcript-derived rows; keep summaries/observations.
    conn.execute("DELETE FROM prompts WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM actions WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM search_fts WHERE session_id=? AND kind IN "
                 "('prompt','actions','title')", (sid,))
    conn.executemany("INSERT INTO prompts VALUES (?,?,?,?)",
                     [(sid, i, ts, t) for i, (ts, t) in enumerate(s["prompts"])])
    conn.executemany("INSERT INTO actions VALUES (?,?,?,?,?,?)",
                     [(sid, i, ts, tool, kind, target)
                      for i, (ts, tool, kind, target) in enumerate(s["actions"])])
    for i, (ts, t) in enumerate(s["prompts"]):
        index(conn, "prompt", str(i), sid, project, ts, t)
    targets = sorted({a[3] for a in s["actions"] if a[2] in ("edit", "read", "run") and a[3]})
    index(conn, "actions", "", sid, project, s["started_at"], "\n".join(targets))
    if title:
        index(conn, "title", "", sid, project, s["started_at"], title)
    conn.execute("INSERT OR REPLACE INTO ingested VALUES (?,?,?)",
                 (str(path), st.st_size, st.st_mtime))
    conn.commit()
    return True


def transcript_paths() -> list[Path]:
    paths = []
    if not CLAUDE_PROJECTS.is_dir():
        return paths
    for d in CLAUDE_PROJECTS.iterdir():
        if not d.is_dir() or d.name in SKIP_PROJECT_DIRS or d.name.startswith(SKIP_PROJECT_PREFIXES):
            continue
        paths.extend(d.glob("*.jsonl"))
    return paths


def backfill(conn) -> int:
    n = 0
    for p in transcript_paths():
        try:
            n += ingest_transcript(conn, p)
        except Exception as e:  # one bad file must not stop the rest
            log(f"ingest failed {p}: {e}")
    return n


# ---------------------------------------------------------------------------
# Summaries (Haiku)
# ---------------------------------------------------------------------------

SUMMARY_FIELDS = ("request", "investigated", "learned", "completed", "next_steps", "notes")
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "request": {"type": "string", "description": "What the user wanted, one sentence"},
        "investigated": {"type": "string", "description": "What was examined, bullet lines"},
        "learned": {"type": "string", "description": "Non-obvious findings, bullet lines"},
        "completed": {"type": "string", "description": "What was actually done/shipped, bullet lines"},
        "next_steps": {"type": "string", "description": "Open items or what comes next"},
        "notes": {"type": "string", "description": "Gotchas worth remembering; empty if none"},
    },
    "required": list(SUMMARY_FIELDS),
}
SUMMARY_INSTRUCTIONS = """You write the session log for a coding assistant's memory.
Below is a condensed Claude Code session: the user's prompts, Claude's replies,
and the tools Claude ran. Summarize it into the JSON fields. Be concrete: name
files, commands, commits, decisions and errors. Use short "- " bullet lines.
Only state what the transcript shows; never invent results. If something was
started but not verified or finished, say so in next_steps. Long texts are
truncated with "…" on purpose — do not mention truncation.

SESSION
"""


def condensed_transcript(conn, sid: str) -> str:
    """Prompts, Claude replies and actions interleaved by time, capped."""
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    path = next((p for p in transcript_paths() if p.stem == sid), None)
    events: list[tuple[str, str]] = []
    if path:
        s = parse_transcript(path)
        events += [(ts or "", f"USER: {t[:2000]}") for ts, t in s["prompts"]]
        events += [(ts or "", f"CLAUDE: {t[:600]}") for ts, t in s["texts"]]
        events += [(ts or "", f"  [{tool}] {target[:160]}")
                   for ts, tool, kind, target in s["actions"]]
    else:
        events += [(r["ts"] or "", f"USER: {r['text'][:2000]}") for r in conn.execute(
            "SELECT ts, text FROM prompts WHERE session_id=?", (sid,))]
        events += [(r["ts"] or "", f"  [{r['tool']}] {(r['target'] or '')[:160]}")
                   for r in conn.execute("SELECT ts, tool, target FROM actions "
                                         "WHERE session_id=?", (sid,))]
    events.sort(key=lambda e: e[0])
    head = f"Project: {project_label(sess['project'], sess['cwd'])}  Started: {sess['started_at']}\n"
    body = "\n".join(e[1] for e in events)
    if len(body) > SUMMARY_INPUT_CAP:  # keep the opening ask and the ending state
        keep = SUMMARY_INPUT_CAP // 2
        body = body[:keep] + "\n…[middle of session omitted]…\n" + body[-keep:]
    return head + body


def summarize(conn, sid: str) -> bool:
    """Generate (or refresh) the Haiku summary for one session."""
    sess = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if not sess:
        return False
    prompt = SUMMARY_INSTRUCTIONS + condensed_transcript(conn, sid)
    env = dict(os.environ, **{GUARD_ENV: "1"})
    data, out = {}, {}
    for attempt in (1, 2):  # Haiku occasionally returns no structured output
        try:
            proc = subprocess.run(
                ["claude", "-p", "--model", SUMMARY_MODEL, "--no-session-persistence",
                 "--settings", json.dumps({"disableAllHooks": True}), "--tools", "",
                 "--output-format", "json", "--json-schema", json.dumps(SUMMARY_SCHEMA)],
                input=prompt, capture_output=True, text=True, timeout=180,
                cwd=STORE_DIR, env=env)
            out = json.loads(proc.stdout)
            data = out.get("structured_output") or json.loads(out.get("result") or "{}")
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            log(f"summarize {sid} attempt {attempt} failed: {e}")
            data = {}
        if isinstance(data, dict) and data.get("request"):
            break
        log(f"summarize {sid} attempt {attempt}: empty result "
            f"({str(out.get('result') or out.get('subtype') or '')[:200]!r})")
    else:
        return False
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM search_fts WHERE session_id=? AND kind='summary' AND ref='haiku'", (sid,))
    conn.execute("DELETE FROM summaries WHERE session_id=? AND source='haiku'", (sid,))
    conn.execute(
        "INSERT INTO summaries(session_id, ts, source, n_prompts, request, investigated, "
        "learned, completed, next_steps, notes) VALUES (?,?,'haiku',?,?,?,?,?,?,?)",
        (sid, now, sess["n_prompts"], *[str(data.get(k, "")) for k in SUMMARY_FIELDS]))
    index(conn, "summary", "haiku", sid, sess["project"], now,
          "\n".join(str(data.get(k, "")) for k in SUMMARY_FIELDS))
    conn.commit()
    log(f"summarized {sid} ({sess['n_prompts']} prompts, ${out.get('total_cost_usd', 0):.4f})")
    return True


def pending_summaries(conn, limit: int, days: int | None = CATCHUP_DAYS) -> list[str]:
    """Ended sessions with enough prompts that lack an up-to-date summary."""
    quiet = (datetime.now(timezone.utc) - timedelta(minutes=QUIET_MINUTES)).isoformat()
    since = ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
             if days else "")
    rows = conn.execute(
        """SELECT s.id FROM sessions s
           WHERE s.source='transcript' AND s.n_prompts >= ?
             AND s.ended_at < ? AND s.ended_at >= ?
             AND NOT EXISTS (SELECT 1 FROM summaries m WHERE m.session_id=s.id
                   AND (m.source='claude-mem' OR m.n_prompts >= s.n_prompts))
           ORDER BY s.ended_at DESC LIMIT ?""",
        (MIN_PROMPTS_TO_SUMMARIZE, quiet, since, limit)).fetchall()
    return [r["id"] for r in rows]


def spawn(*args: str) -> None:
    """Run this script detached so hooks return immediately."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, cwd=STORE_DIR)


# ---------------------------------------------------------------------------
# claude-mem import (one-time, read-only on claude-mem's side)
# ---------------------------------------------------------------------------

def claude_mem_project(name: str, cwd: str | None) -> str:
    if cwd:
        return encode_project_path(cwd)
    base = HOME / "Claude Projects"
    if name == base.name:
        return encode_project_path(str(base))
    return encode_project_path(str(base / name.split("/")[0]))


def import_claude_mem(conn, src: Path = CLAUDE_MEM_DB) -> dict:
    cm = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    cm.row_factory = sqlite3.Row
    counts = {"sessions": 0, "prompts": 0, "summaries": 0, "observations": 0}
    # memory_session_id → (claude code session id, project)
    sessmap: dict[str, tuple[str, str]] = {}
    for r in cm.execute("SELECT * FROM sdk_sessions"):
        sid = r["content_session_id"]
        project = claude_mem_project(r["project"], r["cwd"])
        if r["memory_session_id"]:
            sessmap[r["memory_session_id"]] = (sid, project)
        cur = conn.execute(
            "INSERT OR IGNORE INTO sessions(id, project, cwd, title, started_at, ended_at, "
            "source) VALUES (?,?,?,?,?,?, 'claude-mem')",
            (sid, project, r["cwd"], r["custom_title"], r["started_at"], r["completed_at"]))
        counts["sessions"] += cur.rowcount
        if cur.rowcount:
            have = 0
        else:
            have = conn.execute("SELECT COUNT(*) FROM prompts WHERE session_id=?",
                                (sid,)).fetchone()[0]
        if have:
            continue  # transcript already gave us this session's prompts
        n = 0
        for p in cm.execute("SELECT * FROM user_prompts WHERE content_session_id=? "
                            "ORDER BY prompt_number", (sid,)):
            conn.execute("INSERT OR IGNORE INTO prompts VALUES (?,?,?,?)",
                         (sid, p["prompt_number"], p["created_at"], p["prompt_text"]))
            index(conn, "prompt", str(p["prompt_number"]), sid, project,
                  p["created_at"], p["prompt_text"])
            n += 1
        counts["prompts"] += n
        conn.execute("UPDATE sessions SET n_prompts=MAX(n_prompts, ?) WHERE id=?", (n, sid))

    def resolve(mem_id: str, project_name: str) -> tuple[str, str]:
        if mem_id in sessmap:
            return sessmap[mem_id]
        project = claude_mem_project(project_name, None)
        conn.execute("INSERT OR IGNORE INTO sessions(id, project, source) "
                     "VALUES (?,?, 'claude-mem')", (mem_id, project))
        sessmap[mem_id] = (mem_id, project)
        return sessmap[mem_id]

    for r in cm.execute("SELECT * FROM session_summaries ORDER BY id"):
        sid, project = resolve(r["memory_session_id"], r["project"])
        cur = conn.execute(
            "INSERT OR IGNORE INTO summaries(session_id, ts, source, source_id, request, "
            "investigated, learned, completed, next_steps, notes) "
            "VALUES (?,?,'claude-mem',?,?,?,?,?,?,?)",
            (sid, r["created_at"], r["id"], r["request"], r["investigated"],
             r["learned"], r["completed"], r["next_steps"], r["notes"]))
        if cur.rowcount:
            counts["summaries"] += 1
            index(conn, "summary", f"cm{r['id']}", sid, project, r["created_at"],
                  "\n".join(filter(None, (r["request"], r["investigated"], r["learned"],
                                          r["completed"], r["next_steps"], r["notes"]))))
        conn.execute("UPDATE sessions SET started_at=COALESCE(started_at, ?), "
                     "ended_at=MAX(COALESCE(ended_at, ''), ?) WHERE id=?",
                     (r["created_at"], r["created_at"], sid))

    for r in cm.execute("SELECT * FROM observations ORDER BY id"):
        sid, project = resolve(r["memory_session_id"], r["project"])
        files = ", ".join(filter(None, (r["files_read"], r["files_modified"])))
        cur = conn.execute(
            "INSERT OR IGNORE INTO observations(session_id, ts, type, title, subtitle, "
            "narrative, facts, files, source_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, r["created_at"], r["type"], r["title"], r["subtitle"],
             r["narrative"], r["facts"], files, r["id"]))
        if cur.rowcount:
            counts["observations"] += 1
            index(conn, "observation", str(r["id"]), sid, project, r["created_at"],
                  "\n".join(filter(None, (r["title"], r["subtitle"], r["narrative"], r["facts"]))))
        conn.execute("UPDATE sessions SET started_at=COALESCE(started_at, ?), "
                     "ended_at=MAX(COALESCE(ended_at, ''), ?) WHERE id=?",
                     (r["created_at"], r["created_at"], sid))
    conn.commit()
    cm.close()
    return counts


# ---------------------------------------------------------------------------
# Queries (shared by CLI, SessionStart injection and dashboard)
# ---------------------------------------------------------------------------

def resolve_project(name: str | None) -> str | None:
    """Accept an encoded key, a path, or a short name like 'quizlet'."""
    if not name:
        return None
    if name.startswith("-"):
        return name
    if "/" in name:
        return encode_project_path(str(Path(name).expanduser()))
    return claude_mem_project(name, None)


def fts_query(terms: str) -> str:
    """Turn free text into a safe FTS5 query (AND of quoted tokens)."""
    words = re.findall(r"[\w./-]+", terms)
    return " ".join('"' + w.replace('"', "") + '"' for w in words)


def search(conn, terms: str, project: str | None = None, limit: int = 20) -> list[dict]:
    q = fts_query(terms)
    if not q:
        return []
    sql = ("SELECT kind, ref, session_id, project, ts, "
           "snippet(search_fts, 0, '[', ']', '…', 24) AS snip "
           "FROM search_fts WHERE search_fts MATCH ?")
    args: list = [q]
    if project:
        sql += " AND project = ?"
        args.append(project)
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def best_summary(conn, sid: str):
    """Haiku summary if present, else the latest claude-mem summary."""
    return conn.execute(
        "SELECT * FROM summaries WHERE session_id=? "
        "ORDER BY source='haiku' DESC, ts DESC LIMIT 1", (sid,)).fetchone()


def recent_sessions(conn, project: str | None = None, limit: int = 20,
                    offset: int = 0) -> list[dict]:
    sql = "SELECT * FROM sessions"
    args: list = []
    if project:
        sql += " WHERE project = ?"
        args.append(project)
    sql += " ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    out = []
    for s in conn.execute(sql, args):
        d = dict(s)
        m = best_summary(conn, s["id"])
        d["summary"] = dict(m) if m else None
        if not d["title"]:
            first = conn.execute("SELECT text FROM prompts WHERE session_id=? "
                                 "ORDER BY idx LIMIT 1", (s["id"],)).fetchone()
            d["title"] = (m and m["request"]) or (first and first["text"][:120]) or ""
        out.append(d)
    return out


def session_detail(conn, sid_prefix: str) -> dict | None:
    s = conn.execute("SELECT * FROM sessions WHERE id LIKE ? ORDER BY started_at DESC",
                     (sid_prefix + "%",)).fetchone()
    if not s:
        return None
    sid = s["id"]
    return {
        "session": dict(s),
        "prompts": [dict(r) for r in conn.execute(
            "SELECT idx, ts, text FROM prompts WHERE session_id=? ORDER BY idx", (sid,))],
        "actions": [dict(r) for r in conn.execute(
            "SELECT idx, ts, tool, kind, target FROM actions WHERE session_id=? "
            "ORDER BY idx", (sid,))],
        "summaries": [dict(r) for r in conn.execute(
            "SELECT * FROM summaries WHERE session_id=? ORDER BY source='haiku' DESC, ts DESC",
            (sid,))],
        "observations": [dict(r) for r in conn.execute(
            "SELECT * FROM observations WHERE session_id=? ORDER BY ts", (sid,))],
    }


def first_line(text: str | None, cap: int = 160) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if line:
            return line if len(line) <= cap else line[:cap - 1] + "…"
    return ""


def recent_context(project_dir: str, n: int = 3, exclude: str | None = None) -> str:
    """Compact 'recent sessions' block for SessionStart injection ('' if none)."""
    if not DB_PATH.exists():
        return ""
    try:
        conn = connect(readonly=True)
        rows = [r for r in recent_sessions(conn, encode_project_path(project_dir), n * 3)
                if r["summary"] and r["id"] != exclude][:n]
        conn.close()
    except sqlite3.Error:
        return ""
    if not rows:
        return ""
    lines = ["Recent sessions (claude-memory-guard history):"]
    for r in rows:
        m = r["summary"]
        day = (r["ended_at"] or r["started_at"] or "")[:10]
        lines.append(f"- {day} [{r['id'][:8]}] {first_line(m['request'], 140)}")
        if first_line(m["completed"]):
            lines.append(f"    done: {first_line(m['completed'])}")
        if first_line(m["next_steps"]):
            lines.append(f"    next: {first_line(m['next_steps'])}")
    lines.append('Search past sessions: python3 ~/.claude/scripts/history.py search "<terms>" '
                 f"--project {Path(project_dir).name}  "
                 "(details: history.py show <id>)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _opt(args: list[str], flag: str, default=None):
    if flag in args:
        i = args.index(flag)
        val = args[i + 1] if i + 1 < len(args) else default
        del args[i:i + 2]
        return val
    return default


def print_summary(m) -> None:
    for k in SUMMARY_FIELDS:
        if m[k] and str(m[k]).strip():
            print(f"  {k.replace('_', ' ').title()}:")
            for line in str(m[k]).strip().splitlines():
                print(f"    {line}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd in ("hook-session-end", "catchup", "finalize") and os.environ.get(GUARD_ENV):
        return 0

    if cmd == "hook-session-end":
        try:
            hook = json.load(sys.stdin)
        except ValueError:
            hook = {}
        path = hook.get("transcript_path")
        if path:
            spawn("finalize", path)
        return 0

    if cmd == "finalize":  # detached: ingest one transcript, then summarize it
        conn = connect()
        path = Path(args[0])
        if path.parent.name in SKIP_PROJECT_DIRS:
            return 0
        ingest_transcript(conn, path)
        s = conn.execute("SELECT n_prompts FROM sessions WHERE id=?", (path.stem,)).fetchone()
        if s and s["n_prompts"] >= MIN_PROMPTS_TO_SUMMARIZE:
            have = conn.execute("SELECT MAX(n_prompts) FROM summaries WHERE session_id=? "
                                "AND source='haiku'", (path.stem,)).fetchone()[0]
            if not have or have < s["n_prompts"]:
                summarize(conn, path.stem)
        return 0

    if cmd == "catchup":  # detached from SessionStart
        conn = connect()
        n = backfill(conn)
        done = sum(summarize(conn, sid) for sid in pending_summaries(conn, CATCHUP_MAX))
        if n or done:
            log(f"catchup: ingested {n}, summarized {done}")
        return 0

    if cmd == "backfill":
        conn = connect()
        t = time.time()
        n = backfill(conn)
        print(f"Ingested {n} new/changed transcripts in {time.time() - t:.1f}s")
        return 0

    if cmd == "summarize":
        conn = connect()
        pending = _opt(args, "--pending")
        if pending:
            days = _opt(args, "--days")
            ids = pending_summaries(conn, int(pending), int(days) if days else None)
        else:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ?", (args[0] + "%",))][:1] if args else []
        for sid in ids:
            print(f"{sid}: {'ok' if summarize(conn, sid) else 'failed (see history.log)'}")
        if not ids:
            print("Nothing to summarize.")
        return 0

    if cmd == "import-claude-mem":
        src = Path(args[0]).expanduser() if args else CLAUDE_MEM_DB
        if not src.exists():
            print(f"claude-mem database not found: {src}")
            return 1
        counts = import_claude_mem(connect(), src)
        print("Imported from claude-mem: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        return 0

    if not DB_PATH.exists():
        print("No history yet. Run: history.py backfill")
        return 1
    conn = connect(readonly=True)

    if cmd == "search":
        project = resolve_project(_opt(args, "--project"))
        limit = int(_opt(args, "-n", 20))
        hits = search(conn, " ".join(args), project, limit)
        if not hits:
            print("No matches.")
        for h in hits:
            snip = " ".join(h["snip"].split())
            print(f"{h['ts'][:10]}  {project_label(h['project']):<20} "
                  f"[{h['session_id'][:8]}] {h['kind']:<11} {snip}")
        return 0

    if cmd == "show":
        d = session_detail(conn, args[0] if args else "")
        if not d:
            print("Session not found.")
            return 1
        s = d["session"]
        print(f"Session {s['id']}  ·  {project_label(s['project'], s['cwd'])}  ·  "
              f"{(s['started_at'] or '')[:16]} → {(s['ended_at'] or '')[:16]}")
        if s["title"]:
            print(f"Title: {s['title']}")
        for m in d["summaries"][:1]:
            print(f"\nSummary ({m['source']}):")
            print_summary(m)
        if len(d["summaries"]) > 1:
            print(f"  (+{len(d['summaries']) - 1} more summaries)")
        print(f"\nPrompts ({len(d['prompts'])}):")
        for p in d["prompts"]:
            print(f"  {p['idx'] + 1:>2}. {first_line(p['text'], 200)}")
        edited = sorted({a["target"] for a in d["actions"] if a["kind"] == "edit"})
        print(f"\nActions: {len(d['actions'])}  ·  files edited: {len(edited)}")
        for f in edited:
            print(f"  ✎ {f}")
        if d["observations"]:
            print(f"\nObservations (claude-mem import): {len(d['observations'])}")
            for o in d["observations"][:15]:
                print(f"  • [{o['type']}] {o['title']}")
        return 0

    if cmd == "recent":
        project = resolve_project(_opt(args, "--project"))
        for r in recent_sessions(conn, project, int(_opt(args, "-n", 10))):
            day = (r["ended_at"] or r["started_at"] or "")[:10]
            print(f"{day}  {project_label(r['project'], r['cwd']):<20} [{r['id'][:8]}] "
                  f"{r['n_prompts']:>3}p {r['n_actions']:>4}a  {first_line(r['title'], 90)}")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
