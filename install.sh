#!/usr/bin/env bash
# install.sh — claude-memory-guard installer
# Copies agent + scripts and merges hooks into ~/.claude/settings.json

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
AGENTS_DIR="$CLAUDE_DIR/agents"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
SETTINGS="$CLAUDE_DIR/settings.json"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
RESET="\033[0m"

echo ""
echo "==================================="
echo "  claude-memory-guard installer"
echo "==================================="
echo ""

# --- Check Python 3 ---
if ! command -v python3 &>/dev/null; then
  echo -e "${RED}Error: python3 is required but not found.${RESET}"
  exit 1
fi

PYTHON3=$(command -v python3)

# --- Create directories ---
mkdir -p "$AGENTS_DIR" "$SCRIPTS_DIR"

# --- Copy agent definition ---
cp "$REPO_DIR/agents/claude-memory-guard.md" "$AGENTS_DIR/claude-memory-guard.md"
echo -e "${GREEN}✓${RESET} Copied agents/claude-memory-guard.md → $AGENTS_DIR/"

# --- Copy scripts ---
for script in \
  session_start_reminder.py \
  checkpoint_memory.py \
  checkpoint_counter.py \
  relocate_plan.py \
  end_reminder.py \
  export_conversation.py; do
  cp "$REPO_DIR/scripts/$script" "$SCRIPTS_DIR/$script"
  chmod +x "$SCRIPTS_DIR/$script"
  echo -e "${GREEN}✓${RESET} Copied scripts/$script → $SCRIPTS_DIR/"
done

# --- Merge hooks into settings.json ---
echo ""
echo "Merging hooks into $SETTINGS ..."

"$PYTHON3" - "$SETTINGS" "$SCRIPTS_DIR" <<'PYEOF'
import json
import sys
import os
from pathlib import Path

settings_path = sys.argv[1]
scripts_dir   = sys.argv[2]

def p(script):
    return f"/opt/homebrew/bin/python3 '{scripts_dir}/{script}'"

NEW_HOOKS = {
    "SessionStart": [
        {"hooks": [{"type": "command", "command": p("session_start_reminder.py")}]}
    ],
    "PostCompact": [
        {"hooks": [{"type": "command", "command": p("session_start_reminder.py")}]}
    ],
    "PreCompact": [
        {"matcher": "auto", "hooks": [{"type": "command",
            "command": f"{p('checkpoint_memory.py')} && {p('export_conversation.py')} && echo 'Pre-compact hooks complete'"}]}
    ],
    "PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": p("relocate_plan.py")}]},
        {"hooks": [{"type": "command", "command": p("checkpoint_counter.py")}]},
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": p("end_reminder.py")}]}
    ],
}

# Load or create settings
if Path(settings_path).exists():
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

def already_present(existing_list, new_entry):
    """Check if a hook entry with the same command already exists."""
    new_cmds = {h.get("command") for h in new_entry.get("hooks", [])}
    for existing in existing_list:
        existing_cmds = {h.get("command") for h in existing.get("hooks", [])}
        if new_cmds & existing_cmds:
            return True
    return False

added = []
skipped = []

for event, entries in NEW_HOOKS.items():
    existing = hooks.setdefault(event, [])
    for entry in entries:
        if already_present(existing, entry):
            skipped.append(event)
        else:
            existing.append(entry)
            added.append(event)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

if added:
    print(f"  Added hooks: {', '.join(added)}")
if skipped:
    print(f"  Already present (skipped): {', '.join(skipped)}")
PYEOF

echo ""
echo -e "${GREEN}Installation complete.${RESET}"
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code (or open a new session)"
echo "  2. The claude-memory-guard agent will auto-run RESTORE on session start"
echo "  3. For a new project, say: 'onboard this project' to set up MEMORY.md"
echo ""
