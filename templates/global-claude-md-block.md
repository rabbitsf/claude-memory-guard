<!-- BEGIN claude-memory-guard -->

## claude-memory-guard Agent

Always trigger the `claude-memory-guard` subagent proactively at these moments:

| Trigger | Phase |
|---------|-------|
| Session start (a SessionStart hook already fires a reminder — do not skip) | RESTORE |
| New project, no `CLAUDE.md` | ONBOARD |
| User states a new coding goal | START |
| ~10+ tool calls mid-task, or user says "checkpoint" / "save state" | CHECKPOINT |
| Small edit, typo fix | QUICK-CHECK |
| Code changes completed / user says "done" | END |

## MEMORY.md Rules

- MEMORY.md is **auto-injected into every session** — never call `Read` on it; it is already in context
- It is the primary working memory: check ACTIVE and INPROGRESS sections before reading any other file
- If INPROGRESS is filled, surface it immediately as `RESUMING IN-PROGRESS TASK` before anything else
- If MEMORY.md is missing → run ONBOARD to create it

## Plan Files

- Implementation plans live in `docs/plans/YYYY-MM-DD-<slug>.md` inside the project root
- Plans track their own progress via `- [ ]` / `- [x]` checkboxes and a `# Progress:` header
- START creates the plan file; CHECKPOINT marks completed tasks; END marks all done
- RESTORE reads only the first 15 lines of the plan file to find the resume point

## claude-mem Integration

claude-mem (episodic recall) runs alongside claude-memory-guard (workflow discipline) with strict role separation — no overlap.

### Role separation

| Responsibility | Owner |
|---------------|-------|
| Structured session state (auto-injected, current goals/decisions) | claude-memory-guard |
| Workflow lifecycle (START / CHECKPOINT / END / RESTORE) | claude-memory-guard |
| Structured task state (MEMORY.md, plan files) | claude-memory-guard |
| Canonical implementations, PROJECT_GUIDE.md | claude-memory-guard |
| Episodic recall from past conversations | claude-mem |
| Semantic search across conversation history | claude-mem |
| AST-aware code structure exploration | claude-mem `smart-explore` |
| Project knowledge corpus building | claude-mem `knowledge-agent` |

### When to use claude-mem

- **During RESTORE**: After claude-memory-guard surfaces MEMORY.md state, invoke `claude-mem:mem-search` to retrieve relevant episodic context not captured in MEMORY.md — especially after a gap of days or a new project session
- **Code exploration**: Prefer `claude-mem:smart-explore` for deep codebase understanding (AST-aware, token-efficient)
- **Retrospective analysis**: Use `claude-mem:timeline-report` for project history

### When NOT to use claude-mem (claude-memory-guard owns these)

- Do NOT use `claude-mem:make-plan` when claude-memory-guard is active — claude-memory-guard START phase owns plan files
- Do NOT use `claude-mem:do` for execution tracking — claude-memory-guard CHECKPOINT/END owns this
- Do NOT duplicate MEMORY.md structured state into claude-mem corpora

## Superpowers: Conditional Auto-Trigger

**Do NOT auto-invoke `superpowers:using-superpowers` at session start.**

### Project detection — run this check first, every session:

**claude-memory-guard is active** if ANY of these are true:
- This project's MEMORY.md is injected in context (you will see it)
- This project's CLAUDE.md mentions "claude-memory-guard" or "ai-guardrails"

**If claude-memory-guard IS active → restricted superpowers mode:**
- Do NOT invoke: `writing-plans`, `executing-plans`, `brainstorming`, `finishing-a-development-branch`
  (claude-memory-guard START / CHECKPOINT / END owns these)
- Only invoke these skills when the specific situation demands it:
  - `systematic-debugging` — when debugging a concrete bug or test failure
  - `verification-before-completion` — before claiming a task is done
  - `test-driven-development` — when writing new features or bug fixes
  - `receiving-code-review` — when handling PR review feedback
  - `dispatching-parallel-agents` / `subagent-driven-development` — when parallelizing independent tasks

**If claude-memory-guard is NOT active → selective superpowers mode:**
- Simple questions, explanations, analysis, file reads → No skills needed, respond directly
- Bug or test failure → invoke `systematic-debugging`
- New feature or multi-step implementation → invoke `brainstorming` then `writing-plans`
- Claiming work is done → invoke `verification-before-completion`
- Do NOT invoke skills "just in case" — only invoke when the task type clearly matches

<!-- END claude-memory-guard -->
