#!/usr/bin/env python3
"""
session_start_reminder.py — SessionStart hook for ai-guardrails.

Injects a context reminder into Claude's first turn instructing it to run
ai-guardrails RESTORE. If MEMORY.md has an in-progress task or active goal,
the reminder is escalated so Claude prioritises surfacing it immediately.

Also auto-creates missing scaffolding files (CLAUDE.md, MEMORY.md,
docs/PROJECT_GUIDE.md) so projects are always bootstrapped before Claude
starts responding.

Fires on: startup, resume, /clear, post-compact.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

CLAUDE_MD_TEMPLATE = """\
# CLAUDE.md — AI Development Rules (Read First)

This project uses an **external memory + canonical implementation** workflow.
AI assistance is allowed only when these rules are followed.

---

## Core principles (non-negotiable)

IMPORTANT: Always restate the goal before doing anything.

1. **Single Source of Truth**
   - Every behavior must have exactly one canonical implementation.
   - All triggers (UI, shortcuts, APIs, scripts, jobs) must call into it.

2. **Reuse Over Duplication**
   - Never copy logic to "just make it work".
   - If similar code exists, reuse or refactor instead of re-implementing.

3. **Artifacts vs. Generators**
   - Generated files (artifacts) must NOT be edited directly.
   - Always modify the canonical generator/template and regenerate outputs.

4. **Search Beyond First Match**
   - Do not stop at the first grep/search result.
   - Explicitly check multiple plausible locations before deciding where to change code.

---

## Required workflow for every change

### Before making code changes
- Read `docs/PROJECT_GUIDE.md`
- Restate the goal in terms of **behavior**, not files
- Identify the **canonical implementation**
- List at least two other locations you checked
- Decide: reuse existing code or extend canonical code

### After making code changes
- Ensure all relevant triggers route to the canonical implementation
- Update `docs/PROJECT_GUIDE.md` if any of the following changed:
  - canonical locations
  - responsibilities of modules / templates / generators
  - new entrypoints, shortcuts, or workflows
- Regenerate artifacts if generators/templates were modified

---

## Documentation rules

- **CLAUDE.md**
  - Defines permanent rules and invariants
  - Changes rarely
  - Updated only when a new rule applies to *all future work*

- **docs/PROJECT_GUIDE.md**
  - Living system map and external memory
  - Records *what exists*, *where*, and *why*
  - Updated whenever the system structure or behavior changes

---

## If rules conflict or are unclear
- Stop and ask for clarification
- Do NOT guess or invent new structure
"""

MEMORY_MD_TEMPLATE = """\
# MEMORY.md — {project_name}
# Auto-loaded by Claude Code. Keep under 150 lines. Updated by ai-guardrails.
# Last updated: {date}

<!-- SECTION: ACTIVE -->
## Active Session Context
- Goal: NONE
- Status: NONE
- Started: —
- Files touched: none
<!-- END: ACTIVE -->

<!-- SECTION: INPROGRESS -->
## In-Progress Work Block
<!-- EMPTY — no task in flight -->
<!-- When filled:
What's done:
- [completed step]

What remains:
- [next step]

Files modified so far:
- path/to/file

Plan file: docs/plans/YYYY-MM-DD-name.md

Known blockers:
- none

Resume instruction: [one concrete actionable sentence]
-->
<!-- END: INPROGRESS -->

<!-- SECTION: CANONICAL -->
## Canonical Implementations (Top 10)
<!-- Format: Behavior → `file:function` [YYYY-MM-DD] -->
<!-- END: CANONICAL -->

<!-- SECTION: DECISIONS -->
## Key Decisions
<!-- Format: [YYYY-MM-DD] Topic: Decision -->
[{date_short}] INIT: Project scaffolding auto-created by SessionStart hook
<!-- END: DECISIONS -->

<!-- SECTION: PREFERENCES -->
## Workflow Preferences
<!-- Project-specific preferences discovered over time -->
<!-- END: PREFERENCES -->
"""

PROJECT_GUIDE_TEMPLATE = """\
# PROJECT_GUIDE.md — AI External Memory

> This file is the **living system map** for AI assistance.
> Update it whenever the system structure or behavior changes.

---

## 1. Project Overview

<!-- Brief description of what this project does -->

---

## 2. High-Level Architecture

<!-- Key components, layers, or modules and how they relate -->

---

## 3. End-to-End Workflows

<!-- Major user flows or system processes, step by step -->

---

## 4. Canonical Implementations (Single Source of Truth)

<!-- List each behavior and its ONE canonical location -->

| Behavior | Canonical Location | Notes |
|----------|-------------------|-------|
| _example_ | `src/module.ts:functionName` | _description_ |

---

## 5. Generated Artifacts vs. Canonical Sources

<!-- List generated files and their source generators -->

| Artifact | Generator/Template | Regenerate Command |
|----------|-------------------|-------------------|
| _example_ | `scripts/generate.py` | `make generate` |

---

## 6. Duplication Hotspots

<!-- Known areas where duplication risk is high -->

---

## 7. Safe Change Playbook

<!-- Step-by-step guides for common changes -->
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def encode_project_path(project_dir: str) -> str:
    """Match Claude Code's directory → memory path encoding.
    Claude Code replaces '/' and ' ' with '-', and keeps the leading '-'.
    e.g. /Users/fung/Claude Projects/gradebook → -Users-fung-Claude-Projects-gradebook
    """
    return project_dir.replace("/", "-").replace(" ", "-")


def read_memory(project_dir: str) -> str | None:
    """Return MEMORY.md contents for the project, or None if missing."""
    home = Path.home()
    encoded = encode_project_path(project_dir)
    path = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def inprogress_is_filled(content: str) -> bool:
    """Return True if INPROGRESS section has real task content.
    Strips HTML comments and markdown headings; returns True only if
    substantive text (e.g. 'What's done:', 'Resume instruction:') remains.
    """
    match = re.search(
        r"<!-- SECTION: INPROGRESS -->(.*?)<!-- END: INPROGRESS -->",
        content, re.DOTALL,
    )
    if not match:
        return False
    body = match.group(1)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)   # strip comments
    body = re.sub(r"^#+\s+.*$", "", body, flags=re.MULTILINE)  # strip headings
    return bool(body.strip())


def active_status(content: str) -> str:
    """Extract Status value from ACTIVE section."""
    m = re.search(r"- Status:\s*(.+)", content)
    return m.group(1).strip() if m else "NONE"


def active_goal(content: str) -> str:
    """Extract Goal value from ACTIVE section."""
    m = re.search(r"- Goal:\s*(.+)", content)
    return m.group(1).strip() if m else "NONE"


def plan_file(content: str) -> str | None:
    """Extract Plan file path from INPROGRESS section, if set outside comments."""
    match = re.search(
        r"<!-- SECTION: INPROGRESS -->(.*?)<!-- END: INPROGRESS -->",
        content, re.DOTALL,
    )
    if not match:
        return None
    # Only look in non-comment text
    body = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL)
    m = re.search(r"Plan file:\s*(?!none\b)(\S+\.md)", body)
    return m.group(1).strip() if m else None


def days_since_last_update(content: str) -> int | None:
    """Return days since last MEMORY.md update, or None if unparseable."""
    m = re.search(r"# Last updated:\s*(\d{4}-\d{2}-\d{2})", content)
    if not m:
        return None
    try:
        last_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        return (datetime.now().date() - last_date).days
    except ValueError:
        return None


def stale_canonicals(content: str, project_dir: str) -> list[str]:
    """Return list of canonical file paths that no longer exist in the project.

    Parses CANONICAL section entries in format:
      Behavior → `path/to/file:function` [YYYY-MM-DD]
    and checks if the file portion (before the colon) exists under project_dir.
    """
    match = re.search(
        r"<!-- SECTION: CANONICAL -->(.*?)<!-- END: CANONICAL -->",
        content,
        re.DOTALL,
    )
    if not match:
        return []
    body = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL)
    # Extract all backtick-quoted paths; take file part (before first colon)
    raw = re.findall(r"`([^`]+?)`", body)
    project_path = Path(project_dir)
    missing = []
    for entry in raw:
        file_part = entry.split(":")[0].strip()
        if file_part and not (project_path / file_part).exists():
            missing.append(file_part)
    return missing


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def ensure_scaffolding(project_dir: str) -> list[str]:
    """Create missing scaffolding files. Returns list of files created.

    Creates (if missing):
    - CLAUDE.md in project root
    - docs/PROJECT_GUIDE.md in project root
    - MEMORY.md in ~/.claude/projects/<encoded>/memory/
    """
    created: list[str] = []
    project_name = Path(project_dir).name
    now = datetime.now()
    date_full = now.strftime("%Y-%m-%d %H:%M")
    date_short = now.strftime("%Y-%m-%d")

    # CLAUDE.md in project root
    claude_md = Path(project_dir) / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD_TEMPLATE, encoding="utf-8")
        created.append("CLAUDE.md")

    # docs/PROJECT_GUIDE.md
    docs_dir = Path(project_dir) / "docs"
    project_guide = docs_dir / "PROJECT_GUIDE.md"
    if not project_guide.exists():
        docs_dir.mkdir(parents=True, exist_ok=True)
        project_guide.write_text(PROJECT_GUIDE_TEMPLATE, encoding="utf-8")
        created.append("docs/PROJECT_GUIDE.md")

    # MEMORY.md in ~/.claude/projects/<encoded>/memory/
    home = Path.home()
    encoded = encode_project_path(project_dir)
    memory_dir = home / ".claude" / "projects" / encoded / "memory"
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_dir.mkdir(parents=True, exist_ok=True)
        content = MEMORY_MD_TEMPLATE.format(
            project_name=project_name,
            date=date_full,
            date_short=date_short,
        )
        memory_file.write_text(content, encoding="utf-8")
        created.append("MEMORY.md")

    return created


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def build_message(
    project_dir: str,
    memory: str | None,
    created: list[str] | None = None,
) -> str:
    project_name = Path(project_dir).name
    created = created or []

    scaffolding_note = ""
    if created:
        files_list = ", ".join(created)
        scaffolding_note = (
            f"Scaffolding auto-created: {files_list}\n"
        )

    # --- Case 1: Memory was just created (or still missing somehow) ---
    if memory is None:
        return (
            f"<ai-guardrails-reminder project=\"{project_name}\">\n"
            f"{scaffolding_note}"
            "MANDATORY: Run ai-guardrails agent — RESTORE phase — before your first response.\n"
            "MEMORY.md not found for this project.\n"
            "→ RESTORE will detect this and trigger ONBOARD to create CLAUDE.md, "
            "docs/PROJECT_GUIDE.md, and MEMORY.md.\n"
            "</ai-guardrails-reminder>"
        )

    status = active_status(memory)
    goal = active_goal(memory)
    inprogress = inprogress_is_filled(memory)
    plan = plan_file(memory)

    # --- Intelligence checks ---
    days = days_since_last_update(memory)
    stale = stale_canonicals(memory, project_dir)
    gap_note = (
        f"⚠️ Last session was {days} days ago — PROJECT_GUIDE.md canonicals may be stale.\n"
        if days is not None and days > 7 else ""
    )
    stale_note = (
        "⚠️ Stale canonicals (files no longer exist at these paths): "
        f"{', '.join(stale)}\n"
        if stale else ""
    )

    # --- Case 2: In-progress task detected ---
    if inprogress or status.lower() not in ("none", "completed", "—", "-"):
        lines = [
            f"<ai-guardrails-reminder project=\"{project_name}\">",
        ]
        if scaffolding_note:
            lines.append(scaffolding_note.rstrip())
        if gap_note:
            lines.append(gap_note.rstrip())
        if stale_note:
            lines.append(stale_note.rstrip())
        lines += [
            "⚠️  IN-PROGRESS TASK DETECTED — Run ai-guardrails RESTORE immediately.",
            f"    Goal:   {goal}",
            f"    Status: {status}",
        ]
        if plan:
            lines.append(f"    Plan:   {plan}")
        lines += [
            "RESTORE must surface this as RESUMING IN-PROGRESS TASK before anything else.",
            "Read the plan file header (first 15 lines) to find the first unchecked task.",
            "</ai-guardrails-reminder>",
        ]
        return "\n".join(lines)

    # --- Case 3: Clean session ---
    return (
        f"<ai-guardrails-reminder project=\"{project_name}\">\n"
        f"{scaffolding_note}"
        f"{gap_note}"
        f"{stale_note}"
        "Run ai-guardrails RESTORE phase before your first response.\n"
        "MEMORY.md is already in context — do NOT re-read it.\n"
        "Fast path: Status is clean → output a brief session summary and proceed.\n"
        "Check: CLAUDE.md present? docs/PROJECT_GUIDE.md present? "
        "If either missing, run INIT / BOOTSTRAP.\n"
        "</ai-guardrails-reminder>"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        hook_input = {}

    project_dir = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or hook_input.get("cwd")
        or os.getcwd()
    )

    # Auto-create missing scaffolding before anything else
    created = ensure_scaffolding(project_dir)

    # Now read memory (which may have just been created)
    memory = read_memory(project_dir)
    message = build_message(project_dir, memory, created)

    # json.dumps handles all escaping correctly
    output = {
        "additional_context": message,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        },
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
