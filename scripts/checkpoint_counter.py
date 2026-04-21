#!/usr/bin/env python3
import json
import sys
import os
import tempfile

THRESHOLD = 10

data = json.load(sys.stdin)
session_id = data.get("session_id", "unknown")

counter_file = os.path.join(tempfile.gettempdir(), f"claude_checkpoint_counter_{session_id}.txt")

count = 0
if os.path.exists(counter_file):
    try:
        with open(counter_file) as f:
            count = int(f.read().strip())
    except Exception:
        count = 0

count += 1

if count >= THRESHOLD:
    try:
        os.remove(counter_file)
    except Exception:
        pass
    print(json.dumps({
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                "CHECKPOINT REMINDER: 10+ tool calls have been made since the last checkpoint. "
                "Consider running claude-memory-guard CHECKPOINT phase to save progress to MEMORY.md."
            )
        }
    }))
else:
    with open(counter_file, "w") as f:
        f.write(str(count))
    print(json.dumps({"suppressOutput": True}))
