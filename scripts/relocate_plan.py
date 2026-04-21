#!/usr/bin/env python3
"""
relocate_plan.py — PostToolUse:Write hook for claude-memory-guard plan management.

When Claude writes a plan to ~/.claude/plans/<random-name>.md (via built-in
plan mode), this hook automatically moves it to the current project's
docs/plans/YYYY-MM-DD-<slug>.md with a human-readable name.

Does nothing for any Write outside ~/.claude/plans/.
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(title: str) -> str:
    """Convert a plan title to a filesystem-safe slug (max 60 chars)."""
    # Strip common boilerplate prefixes/suffixes
    title = re.sub(
        r'^(Plan|Fix|Feature|Add|Update|Implement|Create|Refactor)\s*:?\s*',
        '', title, flags=re.IGNORECASE,
    )
    title = re.sub(
        r'\s*[-—]\s*(Implementation Plan|Plan)$', '', title, flags=re.IGNORECASE
    )
    title = re.sub(r'\s*Implementation Plan$', '', title, flags=re.IGNORECASE)

    slug = title.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:60].rstrip('-') or 'plan'


def extract_title(content: str) -> str:
    """Return the text of the first H1 heading, or 'plan' as fallback."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('# '):
            return stripped[2:].strip()
    return 'plan'


def unique_dest(dest_dir: Path, filename: str) -> Path:
    """Return a non-colliding path inside dest_dir for filename."""
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    counter = 2
    while dest.exists():
        dest = dest_dir / f'{stem}-{counter}{suffix}'
        counter += 1
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # --- 1. Read hook JSON from stdin ---
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        return 0

    # --- 2. Only act on Write tool ---
    if hook_input.get('tool_name') != 'Write':
        return 0

    file_path_str = hook_input.get('tool_input', {}).get('file_path', '')
    if not file_path_str:
        return 0

    # --- 3. Only act on writes to ~/.claude/plans/ ---
    home = Path.home()
    global_plans_dir = (home / '.claude' / 'plans').resolve()

    try:
        written_path = Path(file_path_str).expanduser().resolve()
    except Exception:
        return 0

    if written_path.parent != global_plans_dir:
        return 0  # Not a global plan file — nothing to do

    if not written_path.exists():
        return 0  # Already moved or never created

    # --- 4. Determine project directory ---
    project_dir_str = (
        os.environ.get('CLAUDE_PROJECT_DIR')
        or hook_input.get('cwd')
        or os.getcwd()
    )
    project_path = Path(project_dir_str)

    # --- 5. Build destination path ---
    content = written_path.read_text(encoding='utf-8')
    title = extract_title(content)
    slug = slugify(title)
    today = datetime.now().strftime('%Y-%m-%d')
    filename = f'{today}-{slug}.md'

    dest_dir = project_path / 'docs' / 'plans'
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = unique_dest(dest_dir, filename)

    # --- 6. Move the file ---
    shutil.move(str(written_path), str(dest_path))
    print(
        f'relocate_plan.py: Moved\n'
        f'  {written_path}\n'
        f'  → {dest_path}'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
