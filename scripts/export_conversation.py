#!/usr/bin/env python3
"""
Export Claude Code conversation from JSONL to Markdown.

Usage:
    python scripts/export_conversation.py [session_id]

If no session_id provided, exports the most recent conversation.
Project directory is resolved dynamically from CLAUDE_PROJECT_DIR env var
or the current working directory — consistent with other claude-memory-guard hooks.
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Path resolution (dynamic — no hardcoded project paths)
# ---------------------------------------------------------------------------

def encode_project_path(project_dir: str) -> str:
    """Encode project path to match Claude Code's memory directory naming.
    Claude Code replaces '/' and ' ' with '-', and keeps the leading '-'.
    e.g. /Users/fung/Claude Projects/gradebook → -Users-fung-Claude-Projects-gradebook
    """
    return project_dir.replace("/", "-").replace(" ", "-")


def get_claude_dir(project_dir: str) -> Path:
    """Return the Claude Code transcript directory for the given project."""
    encoded = encode_project_path(project_dir)
    return Path.home() / ".claude" / "projects" / encoded


def get_output_dir(project_dir: str) -> Path:
    """Return the output directory for exported conversations."""
    return Path(project_dir) / "docs" / "conversations"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def find_latest_session(claude_dir: Path) -> Path | None:
    """Find the most recent .jsonl file in the Claude project directory."""
    jsonl_files = list(claude_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda f: f.stat().st_mtime)


def parse_jsonl(filepath: Path) -> list:
    """Parse JSONL file and extract messages."""
    messages = []
    with open(filepath, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    data = json.loads(line)
                    messages.append(data)
                except json.JSONDecodeError:
                    continue
    return messages


def extract_content(message_obj: dict) -> str:
    """Extract text content from a message object."""
    content = message_obj.get('content', '')

    if isinstance(content, str):
        return content

    # Handle content that's a list (tool calls, thinking, text blocks)
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get('type') == 'text':
                    text_parts.append(part.get('text', ''))
                elif part.get('type') == 'tool_use':
                    tool_name = part.get('name', 'unknown')
                    text_parts.append(f"\n*[Tool: {tool_name}]*\n")
                # Skip 'thinking' blocks
            elif isinstance(part, str):
                text_parts.append(part)
        return '\n'.join(text_parts)

    return str(content) if content else ''


def export_to_markdown(project_dir: str, session_id: str | None = None) -> Path | None:
    """Export conversation to a markdown file in the project's docs/conversations/."""
    claude_dir = get_claude_dir(project_dir)
    output_dir = get_output_dir(project_dir)

    if session_id:
        filepath = claude_dir / f"{session_id}.jsonl"
    else:
        filepath = find_latest_session(claude_dir)

    if not filepath or not filepath.exists():
        print(f"export_conversation.py: No conversation found: {filepath}")
        return None

    entries = parse_jsonl(filepath)

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate output filename
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    session_name = filepath.stem[:8]  # First 8 chars of session ID
    output_file = output_dir / f"{timestamp}_{session_name}.md"

    # Track seen messages to avoid duplicates (assistant messages come in chunks)
    seen_user_messages: set = set()
    current_assistant_id: str | None = None
    current_assistant_text: list[str] = []

    # Write markdown
    with open(output_file, 'w') as f:
        f.write("# Conversation Export\n\n")
        f.write(f"**Session:** {filepath.stem}\n")
        f.write(f"**Project:** {project_dir}\n")
        f.write(f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("---\n\n")

        for entry in entries:
            entry_type = entry.get('type')

            # User messages
            if entry_type == 'user':
                # Flush any pending assistant message
                if current_assistant_text:
                    combined = '\n'.join(current_assistant_text)
                    if combined.strip():
                        f.write(f"## Assistant\n\n{combined}\n\n---\n\n")
                    current_assistant_text = []
                    current_assistant_id = None

                message = entry.get('message', {})
                content = extract_content(message)

                # Deduplicate user messages
                content_hash = hash(content[:200] if len(content) > 200 else content)
                if content_hash not in seen_user_messages and content.strip():
                    seen_user_messages.add(content_hash)
                    f.write(f"## User\n\n{content}\n\n---\n\n")

            # Assistant messages
            elif entry_type == 'assistant':
                message = entry.get('message', {})
                msg_id = message.get('id', '')
                content = extract_content(message)

                # Accumulate content for same message ID (streamed chunks)
                if msg_id != current_assistant_id:
                    # Flush previous assistant message
                    if current_assistant_text:
                        combined = '\n'.join(current_assistant_text)
                        if combined.strip():
                            f.write(f"## Assistant\n\n{combined}\n\n---\n\n")
                    current_assistant_text = []
                    current_assistant_id = msg_id

                if content.strip():
                    current_assistant_text.append(content)

        # Flush final assistant message
        if current_assistant_text:
            combined = '\n'.join(current_assistant_text)
            if combined.strip():
                f.write(f"## Assistant\n\n{combined}\n\n---\n\n")

    print(f"export_conversation.py: Exported to {output_file}")
    return output_file


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    project_dir = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    session_id = sys.argv[1] if len(sys.argv) > 1 else None
    result = export_to_markdown(project_dir, session_id)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())