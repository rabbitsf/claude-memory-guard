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

---

## Requirements

- [Claude Code](https://claude.ai/code) (CLI or desktop app)
- Python 3.8+

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
~/.claude/settings.json                 ← hooks merged in (existing content preserved)
~/.claude/CLAUDE.md                     ← coordination rules appended (created if absent)
```

### What gets added to `~/.claude/CLAUDE.md`

`~/.claude/CLAUDE.md` is the global instruction file Claude Code loads in every session. claude-memory-guard appends a delimited block to it that tells Claude three things:

**1. When to invoke each phase** — a trigger table mapping session moments to RESTORE / START / CHECKPOINT / END / etc.

**2. How to handle MEMORY.md** — MEMORY.md is auto-injected by Claude Code, so Claude must never re-read it manually. The block encodes this rule and tells Claude to surface any INPROGRESS block immediately on session start.

**3. Plugin integration rules** — if you also use [Superpowers](https://github.com/anthropics/superpowers) or [claude-mem](https://github.com/thedotmack/claude-mem), the block defines strict role boundaries so the tools don't duplicate each other's work.

The block is wrapped in sentinel comments (`<!-- BEGIN claude-memory-guard -->` / `<!-- END claude-memory-guard -->`). `install.sh` skips it if already present; `uninstall.sh` removes it cleanly. Any content you already have in `~/.claude/CLAUDE.md` is preserved.

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

This removes the agent, scripts, hook entries from `settings.json`, and the coordination block from `~/.claude/CLAUDE.md`. Project files (`CLAUDE.md`, `docs/`, `MEMORY.md`) are **not** removed — they belong to your projects.

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
```

`MEMORY.md` is stored in `~/.claude/projects/<encoded-path>/memory/` and auto-injected by Claude Code — Claude never needs to read it manually.

---

## Hook overview

| Hook event | Script | Trigger |
|------------|--------|---------|
| `SessionStart` | `session_start_reminder.py` | Every new session |
| `PostCompact` | `session_start_reminder.py` | After `/compact` |
| `PreCompact` | `checkpoint_memory.py` + `export_conversation.py` | Before compaction |
| `PostToolUse` (all) | `checkpoint_counter.py` | Every tool call |
| `PostToolUse` (Write) | `relocate_plan.py` | When a plan file is written |
| `Stop` | `end_reminder.py` | After each assistant response |

---

## Optional companion plugins

claude-memory-guard is designed to work standalone, but integrates cleanly with two Claude Code plugins when both are installed. Each has a strictly separated role — they do not overlap.

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

[claude-mem](https://github.com/thedotmack/claude-mem) is a Claude Code plugin that provides episodic memory — it records observations from past conversations and lets Claude search and recall them semantically across sessions.

**How it works with claude-memory-guard:**

The two systems cover different memory layers and must not duplicate each other's work:

| Responsibility | Owner |
|---------------|-------|
| Structured task state (`MEMORY.md`, plan files) | claude-memory-guard |
| Workflow lifecycle (START / CHECKPOINT / END / RESTORE) | claude-memory-guard |
| Canonical implementations, `PROJECT_GUIDE.md` | claude-memory-guard |
| Episodic recall from past conversations | claude-mem |
| Semantic search across conversation history | claude-mem |
| AST-aware code structure exploration | claude-mem `smart-explore` |
| Project knowledge corpus building | claude-mem `knowledge-agent` |

**Recommended RESTORE sequence when both are installed:**

1. claude-memory-guard RESTORE runs first — surfaces `MEMORY.md` state and any in-progress task
2. Then invoke `claude-mem:mem-search` to retrieve episodic context from past sessions not captured in `MEMORY.md` (especially useful after a gap of several days)

These boundaries are encoded in the coordination block that `install.sh` appends to `~/.claude/CLAUDE.md`. No manual steps needed.

---

## License

MIT
