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

# --- Remove coordination block from ~/.claude/CLAUDE.md ---
GLOBAL_CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
SENTINEL_BEGIN="<!-- BEGIN claude-memory-guard -->"
SENTINEL_END="<!-- END claude-memory-guard -->"

if [ -f "$GLOBAL_CLAUDE_MD" ] && grep -qF "$SENTINEL_BEGIN" "$GLOBAL_CLAUDE_MD"; then
  echo ""
  echo "Removing coordination block from $GLOBAL_CLAUDE_MD ..."
  if [ -n "$PYTHON3" ]; then
    "$PYTHON3" - "$GLOBAL_CLAUDE_MD" "$SENTINEL_BEGIN" "$SENTINEL_END" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
begin = sys.argv[2]
end   = sys.argv[3]

text = path.read_text()
start_idx = text.find(begin)
end_idx   = text.find(end)

if start_idx == -1 or end_idx == -1:
    print("  Block markers not found — nothing removed")
    sys.exit(0)

# Include the trailing newline after the end sentinel if present
end_idx += len(end)
if end_idx < len(text) and text[end_idx] == '\n':
    end_idx += 1

# Also trim any leading blank line before the sentinel
trimmed = text[:start_idx].rstrip('\n')
remainder = text[end_idx:]
result = trimmed + ('\n' if remainder.strip() else '') + remainder

path.write_text(result)
print("  Removed claude-memory-guard coordination block")
PYEOF
  fi
fi

echo ""
echo -e "${GREEN}Uninstall complete.${RESET} Restart Claude Code to apply changes."
echo ""
echo -e "${YELLOW}Note:${RESET} Project files (CLAUDE.md, docs/PROJECT_GUIDE.md, MEMORY.md)"
echo "were NOT removed — those belong to your projects."
echo ""
