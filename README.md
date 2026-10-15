# claude-memory-guard

A Claude Code subagent that enforces workflow discipline across every coding session — preventing duplication, maintaining external memory, and ensuring every change follows a canonical implementation pattern.

---

## What it does

claude-memory-guard runs automatically at key moments in your Claude Code session:

| Moment | Phase | What happens |
|--------|-------|--------------|
| Session start / after `/compact` | **RESTORE** | Reads MEMORY.md and surfaces any in-progress task |
| New project, no `CLAUDE.md` | **INIT** | Creates `CLAUDE.md` with AI development rules |
| No `docs/PROJECT_GUIDE.md` | **BOOTSTRAP** | Creates the project system map |
| You state a new coding goal | **START** | Plans the change, checks for duplication, creates a plan file |
| Small edit or typo fix | **QUICK-CHECK** | Verifies you're editing the canonical location |
| Every 10 tool calls (automatic) | **CHECKPOINT** | Saves progress to `MEMORY.md` so context compaction is safe |
| Code changes complete | **END** | Updates `PROJECT_GUIDE.md`, `CHANGELOG_AI.md`, and `MEMORY.md` |
| Existing project, no `MEMORY.md` | **ONBOARD** | Retrofits the memory system onto an existing project |
| Session ends (automatic) | **History** | Records your prompts and Claude's actions, writes a short AI summary — see [Session history](#session-history) |

---

## Requirements

- [Claude Code](https://claude.ai/code) (CLI or desktop app)
- Python 3.10+ (with SQLite FTS5 — standard in python.org and Homebrew builds)

---

## Installation

```bash
git clone https://github.com/rabbitsf/claude-memory-guard.git
cd claude-memory-guard
chmod +x install.sh
./install.sh
```

Then **restart Claude Code** (or open a new session). The agent will activate automatically.

---

## Updating

If you installed an earlier version, updating is a two-step process:

```bash
cd claude-memory-guard
git pull
./install.sh
```

`install.sh` is safe to re-run — it overwrites the agent definition and scripts with the latest versions, skips any hooks already present in `settings.json`, and appends the `~/.claude/CLAUDE.md` coordination block if it isn't there yet. Then restart Claude Code.

---

## What gets installed

```
~/.claude/agents/claude-memory-guard.md       ← the agent definition
~/.claude/scripts/
    session_start_reminder.py           ← SessionStart + PostCompact hook
    checkpoint_memory.py                ← PreCompact hook (safety net)
    checkpoint_counter.py               ← PostToolUse hook (10-call counter)
    relocate_plan.py                    ← PostToolUse:Write hook (plan file mover)
    end_reminder.py                     ← Stop hook (end-of-turn reminder)
    export_conversation.py              ← PreCompact hook (transcript exporter)
    dashboard.py                        ← read-only web dashboard (localhost:37778)
    history.py                          ← SessionEnd hook + session history store/search
~/.claude/memory-guard/history.db       ← session history (all projects; created on install)
~/.claude/settings.json                 ← hooks merged in (existing content preserved)
~/.claude/CLAUDE.md                     ← coordination rules appended (created if absent)
```

### What gets added to `~/.claude/CLAUDE.md`

`~/.claude/CLAUDE.md` is the global instruction file Claude Code loads in every session. claude-memory-guard appends a delimited block to it that tells Claude three things:

**1. When to invoke each phase** — a trigger table mapping session moments to RESTORE / START / CHECKPOINT / END / etc.

**2. How to handle MEMORY.md** — MEMORY.md is auto-injected by Claude Code, so Claude must never re-read it manually. The block encodes this rule and tells Claude to surface any INPROGRESS block immediately on session start.

**3. Session history and plugin rules**: how Claude searches past sessions (`history.py search`). If you also use [Superpowers](https://github.com/anthropics/superpowers), the block sets role boundaries so the two tools don't duplicate each other's work.

The block is wrapped in sentinel comments (`<!-- BEGIN claude-memory-guard -->` / `<!-- END claude-memory-guard -->`), and `install.sh` handles all three cases safely:

| Situation | What happens |
|-----------|-------------|
| `~/.claude/CLAUDE.md` does not exist | File is created with a minimal header, then the block is appended |
| File exists, block not yet present | Block is appended; all existing content is preserved |
| File exists, block already present | Skipped — safe to re-run `install.sh` without duplicating anything |

`uninstall.sh` removes the block cleanly, leaving any other content in the file intact.

The template lives at `templates/global-claude-md-block.md` in this repo if you want to inspect or customise it before installing.

---

## Files created in your projects

After install, claude-memory-guard will create these files in each project as needed:

| File | Purpose |
|------|---------|
| `CLAUDE.md` | AI development rules (single source of truth, no duplication) |
| `docs/PROJECT_GUIDE.md` | Living system map — canonical implementations, architecture |
| `docs/CHANGELOG_AI.md` | Append-only audit log of every AI-assisted change |
| `docs/plans/YYYY-MM-DD-<slug>.md` | Per-task implementation plan with checkbox progress |
| `docs/conversations/*.md` | Session transcript exports (created on `/compact`) |
| `~/.claude/projects/<encoded>/memory/MEMORY.md` | Per-project working memory, auto-injected every session |

---

## Onboarding an existing project

Open Claude Code in your project directory and say:

```
onboard this project
```

The agent will audit what exists, create any missing files, and seed `MEMORY.md` from your existing docs — without overwriting anything.

---

## Uninstall

```bash
./uninstall.sh
```

This removes the agent, scripts, hook entries from `settings.json`, and the coordination block from `~/.claude/CLAUDE.md`. Session history in `~/.claude/memory-guard/` is kept; run `./uninstall.sh --purge` to delete it too. Project files (`CLAUDE.md`, `docs/`, `MEMORY.md`) are **not** removed — they belong to your projects.

---

## How the memory system works

```
MEMORY.md (auto-injected every session)
    └── Active goal, status, in-progress resume block
    └── Top 10 canonical implementations
    └── Top 10 key decisions

docs/PROJECT_GUIDE.md (read on demand)
    └── Full system map, all canonicals, safe-change playbooks

docs/CHANGELOG_AI.md (append-only)
    └── Audit log of every AI-assisted change

~/.claude/memory-guard/history.db (written by hooks, searched on demand)
    └── Every session: your prompts, Claude's actions, AI summary
```

`MEMORY.md` is stored in `~/.claude/projects/<encoded-path>/memory/` and auto-injected by Claude Code — Claude never needs to read it manually.

---

## Session history

The memory files above hold **current state** (what's true now and what's next). Session history holds **what happened**: every session's prompts, the tools Claude ran and files it touched, and a short summary. Everything lives in one SQLite database, `~/.claude/memory-guard/history.db`, covering all projects.

How it's captured:

- **Prompts and actions** are parsed from Claude Code's own transcripts (`~/.claude/projects/*/*.jsonl`). No AI is involved. Claude Code deletes transcripts after about 30 days, so this is what keeps them permanently.
- **Summaries** come from one background `claude -p --model haiku` call per session when it ends (about a cent or less). The summary covers request, investigated, learned, completed, next steps and notes. The call runs with hooks disabled and `MEMORY_GUARD_SUMMARIZER=1` set, so it never triggers itself.
- **Catch-up**: sessions that ended without a `SessionEnd` hook (crash, closed terminal) are picked up at the next session start. At most 3 summaries are written per start.

How it's used:

- The `SessionStart` hook adds the **last 3 session summaries** for the current project to Claude's context.
- Claude (or you) can search everything:

```bash
python3 ~/.claude/scripts/history.py search "upload extension" --project quizlet
python3 ~/.claude/scripts/history.py show b941aabb        # one session in full
python3 ~/.claude/scripts/history.py recent --project quizlet
python3 ~/.claude/scripts/history.py summarize --pending 10 --days 30   # backfill summaries
```

- The dashboard shows a **Sessions** list on every project page, a page per session (summary, your prompts, what Claude did) and full-text history search in the header.

**Migrating from claude-mem:** `install.sh` imports `~/.claude-mem/claude-mem.db` once, without changing it: its sessions, prompts, summaries and observations. Imported observations are shown on the session pages and included in search. Re-running is safe because already-imported rows are skipped. To run it manually: `python3 ~/.claude/scripts/history.py import-claude-mem`.

---

## Dashboard

A read-only web view of every project's memory, at **http://localhost:37778**.

It starts automatically: the `SessionStart` hook checks the port and, if nothing is listening, launches `dashboard.py` as a background process. It keeps running after you quit Claude Code (until you log out, reboot, or stop it), so you can bookmark the URL.

What it shows:

- **Overview** — one card per project with its status, active goal, open plan progress, last activity and warnings (missing `PROJECT_GUIDE.md` / `CHANGELOG_AI.md`, stale canonicals, project folder deleted). Filter by *In progress*, *Open plans*, *Needs attention* or *Idle 30+ days*.
- **Search**: type in the header to find matching projects, canonical implementations and key decisions across all projects. This helps before building something that may already exist elsewhere. It also searches session history: prompts, summaries and the files sessions touched.
- **Project page** — active session, in-progress block, recorded sessions, plans with checkbox progress, canonicals, decisions, changelog entries and `PROJECT_GUIDE.md`.
- **Session page** — the session's summary, your prompts, the files Claude edited and its full action timeline.

Projects are discovered from `~/.claude/projects/*/memory/MEMORY.md`. Every file is re-read on each page load, so there is no cache to go stale and nothing is ever written. The server listens on `127.0.0.1` only.

Manual control:

```bash
python3 ~/.claude/scripts/dashboard.py status   # running?
python3 ~/.claude/scripts/dashboard.py stop     # stop it
python3 ~/.claude/scripts/dashboard.py start    # start in background
python3 ~/.claude/scripts/dashboard.py serve    # run in foreground (debugging)
```

Log: `~/.claude/memory-guard-dashboard/dashboard.log`. `install.sh` stops an older running version so the next session starts the new one; `uninstall.sh` stops it and removes it.

---

## Hook overview

| Hook event | Script | Trigger |
|------------|--------|---------|
| `SessionStart` | `session_start_reminder.py` | Every new session (also starts the dashboard if it isn't running) |
| `PostCompact` | `session_start_reminder.py` | After `/compact` |
| `PreCompact` | `checkpoint_memory.py` + `export_conversation.py` | Before compaction |
| `PostToolUse` (all) | `checkpoint_counter.py` | Every tool call |
| `PostToolUse` (Write) | `relocate_plan.py` | When a plan file is written |
| `Stop` | `end_reminder.py` | After each assistant response |
| `SessionEnd` | `history.py hook-session-end` | Session exit; ingests the transcript and writes the summary in the background |

---

## Optional companion plugins

claude-memory-guard is designed to work standalone, but integrates cleanly with Superpowers, and replaces claude-mem.

> **Disclaimer:** Superpowers and claude-mem are independent projects. claude-memory-guard is not affiliated with or endorsed by their respective authors. All product names and trademarks belong to their respective owners.

### Superpowers

**Superpowers** is a Claude Code plugin (available via the Claude Code plugin marketplace) that provides a library of workflow skills — structured prompts that guide Claude through specific tasks like test-driven development, systematic debugging, writing plans, and dispatching parallel agents.

**How it works with claude-memory-guard:**

When claude-memory-guard is active, Superpowers runs in a restricted mode. Because claude-memory-guard owns the planning and task-tracking lifecycle (START / CHECKPOINT / END), certain overlapping Superpowers skills are intentionally suppressed to avoid conflicts:

| Mode | Suppressed skills | Reason |
|------|------------------|--------|
| claude-memory-guard active | `writing-plans`, `executing-plans`, `brainstorming`, `finishing-a-development-branch` | claude-memory-guard's START / CHECKPOINT / END phases own these |
| claude-memory-guard active | All others available | `systematic-debugging`, `verification-before-completion`, `test-driven-development`, `receiving-code-review`, `dispatching-parallel-agents` still apply |

These rules are included automatically in the coordination block that `install.sh` appends to `~/.claude/CLAUDE.md`. No manual steps needed.

---

### claude-mem

[claude-mem](https://github.com/thedotmack/claude-mem) is a Claude Code plugin for episodic memory. claude-memory-guard now covers the same ground (see [Session history](#session-history)), so you don't need both. Its history imports with `history.py import-claude-mem`.

What you give up by removing it: per-tool-call AI "observations" (we write one summary per session instead), vector/semantic search (we use SQLite full-text search), and its extra skills such as `smart-explore` and `knowledge-agent`.

---

## License

MIT
