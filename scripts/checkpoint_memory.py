#!/usr/bin/env python3
"""
checkpoint_memory.py — PreCompact hook for ai-guardrails memory system.

Called by Claude Code before context compaction. If the current project has
an active in-progress task in MEMORY.md, writes a minimal INPROGRESS block
so the next session can surface a resume instruction.
"""

import os
import re
import sys
from datetime import datetime
from pathlib import Path


def encode_project_path(project_dir: str) -> str:
    """Encode project path to match Claude Code's memory directory naming.
    Claude Code replaces '/' and ' ' with '-', and keeps the leading '-'.
    e.g. /Users/fung/Claude Projects/gradebook → -Users-fung-Claude-Projects-gradebook
    """
    return project_dir.replace("/", "-").replace(" ", "-")


def find_memory_file(project_dir: str) -> Path | None:
    """Locate MEMORY.md for the given project directory."""
    home = Path.home()
    encoded = encode_project_path(project_dir)
    memory_path = home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"
    return memory_path if memory_path.exists() else None


def get_active_section(content: str) -> str:
    """Extract the ACTIVE section content from MEMORY.md."""
    match = re.search(
        r"<!-- SECTION: ACTIVE -->(.*?)<!-- END: ACTIVE -->",
        content,
        re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def is_task_in_progress(active_section: str) -> bool:
    """Return True if the ACTIVE section shows an in-progress goal."""
    # Status is considered in-progress if it's not NONE or completed
    status_match = re.search(r"- Status:\s*(.+)", active_section)
    if not status_match:
        return False
    status = status_match.group(1).strip().lower()
    return status not in ("none", "completed", "—", "-")


def inprogress_already_set(content: str) -> bool:
    """Return True if the INPROGRESS block is already filled (not just a comment)."""
    match = re.search(
        r"<!-- SECTION: INPROGRESS -->(.*?)<!-- END: INPROGRESS -->",
        content,
        re.DOTALL,
    )
    if not match:
        return False
    body = match.group(1)
    # If there's real content outside comments, it's already set
    stripped = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
    return bool(stripped)


def build_inprogress_block() -> str:
    """Build a minimal INPROGRESS block for pre-compact unknown state."""
    return """## In-Progress Work Block
What's done:
- Unknown — pre-compact hook fired before CHECKPOINT was run

What remains:
- Unknown — review recent conversation and git diff

Files modified so far:
- unknown

Known blockers:
- none

Resume instruction: Run ai-guardrails RESTORE to reconstruct context, then verify last goal in ACTIVE section and review recent git diff.
"""


def update_timestamp(content: str, now: str) -> str:
    """Update the Last updated line in MEMORY.md."""
    return re.sub(
        r"# Last updated:.*",
        f"# Last updated: {now}",
        content,
    )


def replace_inprogress_section(content: str, new_body: str) -> str:
    """Replace the INPROGRESS section body in MEMORY.md."""
    replacement = f"<!-- SECTION: INPROGRESS -->\n{new_body}\n<!-- END: INPROGRESS -->"
    return re.sub(
        r"<!-- SECTION: INPROGRESS -->.*?<!-- END: INPROGRESS -->",
        replacement,
        content,
        flags=re.DOTALL,
    )


def main() -> int:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        # Try to infer from current working directory as fallback
        project_dir = os.getcwd()

    memory_file = find_memory_file(project_dir)
    if not memory_file:
        # No MEMORY.md for this project — nothing to do
        print(f"checkpoint_memory.py: No MEMORY.md found for {project_dir}, skipping.")
        return 0

    content = memory_file.read_text(encoding="utf-8")
    active_section = get_active_section(content)

    if not is_task_in_progress(active_section):
        print("checkpoint_memory.py: No in-progress task detected, skipping.")
        return 0

    if inprogress_already_set(content):
        # CHECKPOINT was already called by the agent — just update timestamp
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        updated = update_timestamp(content, now)
        memory_file.write_text(updated, encoding="utf-8")
        print("checkpoint_memory.py: INPROGRESS already set. Updated timestamp.")
        return 0

    # Write minimal INPROGRESS block
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_body = build_inprogress_block()
    updated = replace_inprogress_section(content, new_body)
    updated = update_timestamp(updated, now)
    memory_file.write_text(updated, encoding="utf-8")
    print(f"checkpoint_memory.py: Wrote INPROGRESS block to {memory_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
