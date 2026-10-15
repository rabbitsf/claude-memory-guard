# claude-memory-guard

**Persistent memory and workflow discipline for [Claude Code](https://claude.ai/code).**

Claude Code forgets everything between sessions, loses detail when the context is compacted, and will happily write a second copy of logic that already exists. claude-memory-guard fixes that with three layers:

1. **Working memory**: a per-project `MEMORY.md` with the current goal, in-progress state, canonical implementations and key decisions. It is injected into every session, so Claude picks up where it left off.
2. **Workflow agent**: a subagent that runs at fixed points (session start, new goal, checkpoint, done). It plans changes, finds the one canonical place to make them and keeps project docs current.
3. **Session history**: a searchable SQLite record of every session across all projects: your prompts, what Claude did and a short AI summary.

A read-only **dashboard** at `http://localhost:37778` shows all of it across every project.

Everything runs locally through Claude Code hooks. The only network call is one small Haiku request per session for its summary.

---

## Contents

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [The workflow phases](#the-workflow-phases)
- [Session history](#session-history)
- [Dashboard](#dashboard)
- [What gets installed](#what-gets-installed)
- [Files created in your projects](#files-created-in-your-projects)
- [Hooks](#hooks)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Works with Superpowers, replaces claude-mem](#works-with-superpowers-replaces-claude-mem)

---

## Quick start

**Requirements**

- Claude Code (CLI or desktop app)
- Python 3.10+ with SQLite FTS5 (standard in python.org and Homebrew builds)
- macOS with Homebrew Python: the hooks call `/opt/homebrew/bin/python3`. On other setups, edit the hook commands in `~/.claude/settings.json` after installing, or change the `p()` helper in `install.sh` before running it.

**Install**

```bash
git clone https://github.com/rabbitsf/claude-memory-guard.git
cd claude-memory-guard
./install.sh
```

Restart Claude Code. From the next session on, the agent restores context automatically.

**Use an existing project.** Open Claude Code in the project and say:

```
onboard this project
```

The agent looks at what is already there, creates whatever is missing and fills `MEMORY.md` from your existing docs. It does not overwrite anything.

---

## How it works

```
┌─ every session ───────────────────────────────────────────────────────────┐
│                                                                           │
│  SessionStart hook                                                        │
│    ├─ creates missing scaffolding (CLAUDE.md, MEMORY.md, PROJECT_GUIDE)   │
│    ├─ injects a RESTORE reminder (escalated if a task is in progress)     │
│    ├─ injects the last 3 session summaries for this project               │
│    └─ starts the dashboard if it isn't running                            │
│                                                                           │
│  You work → agent phases: START → CHECKPOINT (every ~10 tool calls) → END │
│                                                                           │
│  PreCompact hook  → saves an in-progress block + exports the transcript   │
│  Stop hook        → reminds Claude once to run END if a task is open      │
│  SessionEnd hook  → ingests the transcript, writes an AI summary          │
└───────────────────────────────────────────────────────────────────────────┘
```

Where each kind of memory lives:

| Layer | Location | Holds | Read |
|-------|----------|-------|------|
| `MEMORY.md` | `~/.claude/projects/<encoded>/memory/` | Goal, status, in-progress block, top canonicals and decisions | Auto-injected every session |
| `docs/PROJECT_GUIDE.md` | Project | Full system map: architecture, every canonical location, duplication hotspots | On demand |
| `docs/plans/*.md` | Project | One plan per task, with checkbox progress | First 15 lines on RESTORE |
| `docs/CHANGELOG_AI.md` | Project | Append-only log of AI-assisted changes | On demand |
| `history.db` | `~/.claude/memory-guard/` | Every session: prompts, actions, summary | Last 3 summaries at start; full search on demand |

The rule behind it: **`MEMORY.md` and the docs hold current state; history holds what happened.** Lasting decisions are promoted from history into `MEMORY.md`. The history itself is never copied there.

---

## The workflow phases

The agent (`claude-memory-guard`) runs in one of these phases depending on the moment:

| Moment | Phase | What happens |
|--------|-------|--------------|
| Session start, or after compaction | **RESTORE** | Summarizes state from `MEMORY.md` and surfaces any in-progress task first |
| No `CLAUDE.md` in the project | **INIT** | Creates `CLAUDE.md` with the development rules |
| No `docs/PROJECT_GUIDE.md` | **BOOTSTRAP** | Creates the project system map |
| Existing project, no `MEMORY.md` | **ONBOARD** | Adds the memory system to an existing project |
| You state a new coding goal | **START** | Restates the goal, finds the canonical implementation, checks for duplication, writes a plan file |
| Small edit or typo fix | **QUICK-CHECK** | Confirms you are editing the canonical location |
| About every 10 tool calls, or "checkpoint" | **CHECKPOINT** | Saves progress to `MEMORY.md` and ticks off plan items, so compaction loses nothing |
| Work is done | **END** | Updates `PROJECT_GUIDE.md`, `CHANGELOG_AI.md` and `MEMORY.md` |

The rules it enforces (also written into each project's `CLAUDE.md`):

- **Single source of truth.** Each behavior has one canonical implementation, and every trigger calls into it.
- **Reuse over duplication.** Extend or refactor existing code. Never copy it.
- **Artifacts vs. generators.** Change the template or generator, then regenerate. Never hand-edit output.
- **Search beyond the first match.** Check more than one plausible location before deciding where a change goes.

---

## Session history

Every session is recorded in one SQLite + FTS5 database, `~/.claude/memory-guard/history.db`, covering all projects.

**How it's captured**

- **Prompts and actions** (tools run, files read and edited) are parsed from Claude Code's own transcripts, `~/.claude/projects/*/*.jsonl`. No AI is involved. Claude Code deletes transcripts after about 30 days, so this is what keeps them.
- **Summaries** come from one background `claude -p --model haiku` call when a session ends: request, investigated, learned, completed, next steps. It costs about a cent or less. The call runs with hooks disabled and `MEMORY_GUARD_SUMMARIZER=1` set, so it can't trigger itself. Sessions with fewer than 2 prompts are skipped.
- **Catch-up**: sessions that ended without a `SessionEnd` hook (a crash or a closed terminal) are summarized at the next session start, at most 3 per start.
- Headless `claude -p` sessions are not recorded.

**How it's used**

The SessionStart hook adds the last 3 summaries for the current project to Claude's context. Claude, or you, can search everything else:

```bash
H=~/.claude/scripts/history.py
python3 $H search "upload extension" --project myapp   # full-text search
python3 $H show b941aabb                               # one session in full
python3 $H recent --project myapp                      # latest sessions
python3 $H backfill                                    # ingest all existing transcripts
python3 $H summarize --pending 10 --days 30            # write missing summaries
python3 $H import-claude-mem                           # one-time import from claude-mem
```

Ingest is idempotent: each transcript's size and mtime are tracked, so re-running is always safe.

---

## Dashboard

A read-only web view of every project's memory, at **http://localhost:37778**.

- **Overview**: one card per project with status, active goal, plan progress, last activity and warnings (missing `PROJECT_GUIDE.md` or `CHANGELOG_AI.md`, stale canonicals, deleted project folder). Filters: *In progress*, *Open plans*, *Needs attention*, *Idle 30+ days*.
- **Search** (header): projects, canonical implementations and key decisions across all projects, plus session history (prompts, summaries, files touched). Use it before building something that may already exist elsewhere.
- **Project page**: active session, in-progress block, plans with progress, canonicals, decisions, changelog, `PROJECT_GUIDE.md` and the list of recorded sessions.
- **Session page**: summary, your prompts, files edited and the full action timeline.

The SessionStart hook starts it in the background if nothing is listening on the port. It then runs until you log out, reboot or stop it, so the URL can be bookmarked. It listens on `127.0.0.1` only, re-reads files on every request (no cache) and never writes anything.

```bash
python3 ~/.claude/scripts/dashboard.py status   # running?
python3 ~/.claude/scripts/dashboard.py start    # start in background
python3 ~/.claude/scripts/dashboard.py stop     # stop
python3 ~/.claude/scripts/dashboard.py serve    # foreground, for debugging
```

Log: `~/.claude/memory-guard-dashboard/dashboard.log`.

---

## What gets installed

```
~/.claude/agents/claude-memory-guard.md   the agent definition
~/.claude/scripts/
    session_start_reminder.py             SessionStart + PostCompact hook
    checkpoint_counter.py                 PostToolUse hook (10-call checkpoint counter)
    relocate_plan.py                      PostToolUse:Write hook (moves plan-mode plans into docs/plans/)
    checkpoint_memory.py                  PreCompact hook (saves in-progress block)
    export_conversation.py                PreCompact hook (transcript → Markdown)
    end_reminder.py                       Stop hook (END-phase reminder)
    history.py                            SessionEnd hook + history store and CLI
    dashboard.py                          dashboard server
~/.claude/memory-guard/history.db         session history (built on install)
~/.claude/settings.json                   hooks merged in; existing content kept
~/.claude/CLAUDE.md                       coordination block appended; file created if absent
```

On install, `install.sh` also ingests your existing transcripts into history. If `~/.claude-mem/claude-mem.db` exists, it imports that too, read-only.

### The `~/.claude/CLAUDE.md` block

`~/.claude/CLAUDE.md` is loaded into every Claude Code session. The installer appends a block between `<!-- BEGIN claude-memory-guard -->` and `<!-- END claude-memory-guard -->` that tells Claude:

- when to run each phase;
- that `MEMORY.md` is already in context and must not be re-read, and that an in-progress block is surfaced first;
- where plan files live and how they track progress;
- how to search session history;
- how to coexist with Superpowers, if you use it.

If the block is already present, it is skipped, so re-running the installer never duplicates it. `uninstall.sh` removes only that block. The source is [`templates/global-claude-md-block.md`](templates/global-claude-md-block.md); edit it before installing if you want to customize it.

---

## Files created in your projects

| File | Purpose |
|------|---------|
| `CLAUDE.md` | Development rules (single source of truth, no duplication) |
| `docs/PROJECT_GUIDE.md` | Living system map: architecture, canonical implementations, hotspots |
| `docs/CHANGELOG_AI.md` | Append-only log of AI-assisted changes |
| `docs/plans/YYYY-MM-DD-<slug>.md` | One plan per task, with `- [ ]` / `- [x]` progress |
| `docs/conversations/*.md` | Transcript exports, written before automatic compaction |
| `~/.claude/projects/<encoded>/memory/MEMORY.md` | Per-project working memory |

`docs/` and `CLAUDE.md` are local workflow files. Add them to `.gitignore` before pushing a project to a shared remote:

```gitignore
# AI workflow artifacts — local only
docs/
CLAUDE.md
```

---

## Hooks

| Event | Script | When |
|-------|--------|------|
| `SessionStart` | `session_start_reminder.py` | Startup, resume, `/clear`. Also injects history and starts the dashboard |
| `PostCompact` | `session_start_reminder.py` | After compaction |
| `PreCompact` (auto) | `checkpoint_memory.py` → `export_conversation.py` | Before automatic compaction |
| `PostToolUse` | `checkpoint_counter.py` | Every tool call; prompts a CHECKPOINT every 10 |
| `PostToolUse` (Write) | `relocate_plan.py` | A plan written to `~/.claude/plans/` is moved to `docs/plans/` |
| `Stop` | `end_reminder.py` | If a task is still open, asks Claude once per session and goal to run END |
| `SessionEnd` | `history.py hook-session-end` | Ingests the transcript and summarizes it in the background |

---

## Updating and uninstalling

**Update**

```bash
cd claude-memory-guard
git pull
./install.sh
```

The installer is safe to re-run. It overwrites the agent and scripts, skips hooks that are already registered, stops any running dashboard so the next session starts the new version, and only adds the CLAUDE.md block if it's missing. Restart Claude Code afterwards.

**Uninstall**

```bash
./uninstall.sh           # keeps session history
./uninstall.sh --purge   # also deletes ~/.claude/memory-guard/
```

This removes the agent, scripts, hook entries and the CLAUDE.md block, and stops the dashboard. Files in your projects (`CLAUDE.md`, `docs/`, `MEMORY.md`) are left alone because they belong to the projects.

---

## Works with Superpowers, replaces claude-mem

> Superpowers and claude-mem are independent projects. claude-memory-guard is not affiliated with or endorsed by their authors.

### Superpowers

[Superpowers](https://github.com/obra/superpowers) is a library of workflow skills for Claude Code. Both tools want to own planning, so when claude-memory-guard is active the coordination block limits which Superpowers skills Claude uses:

| Skills | When claude-memory-guard is active |
|--------|------------------------------------|
| `writing-plans`, `executing-plans`, `brainstorming`, `finishing-a-development-branch` | **Suppressed**: START / CHECKPOINT / END own these |
| `systematic-debugging`, `verification-before-completion`, `test-driven-development`, `receiving-code-review`, `dispatching-parallel-agents`, `subagent-driven-development` | Used when the situation calls for them |

This is configured by the installer. There are no manual steps.

### claude-mem

[claude-mem](https://github.com/thedotmack/claude-mem) is an episodic-memory plugin. Session history covers the same ground, so you don't need both. `install.sh` imports your claude-mem sessions, prompts, summaries and observations once, without modifying its database. After that you can uninstall it.

What you give up: per-tool-call AI "observations" (claude-memory-guard writes one summary per session instead), vector search (SQLite full-text search here), and claude-mem's extra skills.

---

## License

MIT
