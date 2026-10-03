#!/usr/bin/env python3
"""
end_reminder.py — Stop hook for claude-memory-guard memory system.

Fires after each Claude response. If the current project has an active
in-progress task in MEMORY.md, blocks the stop ONCE per session per goal
with a reason Claude sees, so it either runs the END phase or says the
work is not finished yet. Silent when no task is active (Status: NONE or
completed), when already continuing from a Stop hook, and after the first
nudge for a given session + goal (marker in ~/.claude/state/).

Fires on: every assistant turn stop.
"""

import json
import os
import re
import sys
from pathlib import Path

# A status is "clean" (no task in flight) if empty or it STARTS with none / done /
# complete(d) / a dash — e.g. "DONE — published as 73d9d4c". Same rule in every hook.
_CLEAN_STATUS = re.compile(r"(none|done|completed?)\b|[—-]?$", re.I)


def status_is_clean(status: str) -> bool:
    return bool(_CLEAN_STATUS.match(status.strip()))



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
    if status_is_clean(status):
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

    # A plain systemMessage is shown to the user only; "block" + reason is the
    # only way Claude sees it. Nudge once per session+goal to avoid loops/nagging.
    if hook_input.get("stop_hook_active"):
        return 0
    session_id = hook_input.get("session_id") or "nosession"
    key = re.sub(r"[^\w.-]", "_", f"{session_id}-{goal}")[:150]
    marker_dir = Path.home() / ".claude" / "state" / "mem-guard-end"
    marker = marker_dir / key
    if marker.exists():
        return 0
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker.touch()

    print(json.dumps({"decision": "block", "reason": message + (
        "\nIf the work is complete, run END now. If not, reply in one line that "
        "the task is still in progress and stop.")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
