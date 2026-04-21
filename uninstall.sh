#!/usr/bin/env bash
# uninstall.sh — claude-memory-guard uninstaller
# Removes agent, scripts, and hook entries from ~/.claude/settings.json

set -euo pipefail

CLAUDE_DIR="$HOME/.claude"
AGENTS_DIR="$CLAUDE_DIR/agents"
SCRIPTS_DIR="$CLAUDE_DIR/scripts"
SETTINGS="$CLAUDE_DIR/settings.json"

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RESET="\033[0m"

echo ""
echo "==================================="
echo "  claude-memory-guard uninstaller"
echo "==================================="
echo ""

PYTHON3=$(command -v python3 || true)

# --- Remove agent ---
if [ -f "$AGENTS_DIR/claude-memory-guard.md" ]; then
  rm "$AGENTS_DIR/claude-memory-guard.md"
  echo -e "${GREEN}✓${RESET} Removed claude-memory-guard.md"
fi

# --- Remove scripts ---
for script in \
  session_start_reminder.py \
  checkpoint_memory.py \
  checkpoint_counter.py \
  relocate_plan.py \
  end_reminder.py \
  export_conversation.py; do
  if [ -f "$SCRIPTS_DIR/$script" ]; then
    rm "$SCRIPTS_DIR/$script"
    echo -e "${GREEN}✓${RESET} Removed $script"
  fi
done

# --- Remove hooks from settings.json ---
if [ -f "$SETTINGS" ] && [ -n "$PYTHON3" ]; then
  echo ""
  echo "Removing hooks from $SETTINGS ..."
  "$PYTHON3" - "$SETTINGS" "$SCRIPTS_DIR" <<'PYEOF'
import json
import sys
from pathlib import Path

settings_path = sys.argv[1]
scripts_dir   = sys.argv[2]

SCRIPT_NAMES = [
    "session_start_reminder.py",
    "checkpoint_memory.py",
    "checkpoint_counter.py",
    "relocate_plan.py",
    "end_reminder.py",
    "export_conversation.py",
]

def references_our_scripts(command_str):
    return any(name in command_str for name in SCRIPT_NAMES)

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.get("hooks", {})
removed = []

for event in list(hooks.keys()):
    original = hooks[event]
    filtered = [
        entry for entry in original
        if not any(
            references_our_scripts(h.get("command", ""))
            for h in entry.get("hooks", [])
        )
    ]
    if len(filtered) < len(original):
        removed.append(event)
    if filtered:
        hooks[event] = filtered
    else:
        del hooks[event]

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

if removed:
    print(f"  Removed hooks from: {', '.join(removed)}")
else:
    print("  No claude-memory-guard hooks found in settings.json")
PYEOF
fi

echo ""
echo -e "${GREEN}Uninstall complete.${RESET} Restart Claude Code to apply changes."
echo ""
echo -e "${YELLOW}Note:${RESET} Project files (CLAUDE.md, docs/PROJECT_GUIDE.md, MEMORY.md)"
echo "were NOT removed — those belong to your projects."
echo ""
