#!/usr/bin/env python3
"""
end_reminder.py — Stop hook for claude-memory-guard memory system.

Fires after each Claude response. If the current project has an active
in-progress task in MEMORY.md, injects a reminder to run the END phase
when work is complete. Silent when no task is active (Status: NONE or
completed) to avoid noise during clean sessions.

Fires on: every assistant turn stop.
"""

import json
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers (mirrors session_start_reminder.py conventions)
# ---------------------------------------------------------------------------

def encode_project_path(project_dir: str) -> str:
    """Match Claude Code's directory → memory path encoding."""
    return project_dir.replace("/", "-").replace(" ", "-")


def read_memory(project_dir: str) -> str | None:
    """Return MEMORY.md contents for the project, or None if missing."""
    home = Path.home()
    encoded = encode_project_path(project_dir)
    path = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def active_status(content: str) -> str:
    m = re.search(r"- Status:\s*(.+)", content)
    return m.group(1).strip() if m else "NONE"


def active_goal(content: str) -> str:
    m = re.search(r"- Goal:\s*(.+)", content)
    return m.group(1).strip() if m else "NONE"


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

    memory = read_memory(project_dir)
    if not memory:
        return 0  # No MEMORY.md — nothing to remind

    status = active_status(memory).lower()
    if status in ("none", "completed", "—", "-"):
        return 0  # Clean session — stay silent

    goal = active_goal(memory)
    project_name = Path(project_dir).name

    message = (
        f"<claude-memory-guard-end-reminder project=\"{project_name}\">\n"
        f"Active task still in progress — Status: {status} | Goal: {goal}\n"
        "When code changes are complete, run claude-memory-guard END phase to update:\n"
        "  - docs/PROJECT_GUIDE.md (canonical implementations)\n"
        "  - docs/CHANGELOG_AI.md (audit log)\n"
        "  - MEMORY.md (status → completed, INPROGRESS cleared)\n"
        "</claude-memory-guard-end-reminder>"
    )

    # print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
