#!/usr/bin/env python3
"""PostToolUse hook for AI Scratchpad/Inbox.md edits.

Two checks on the "Last triaged:" line:
  1. HH:MM format must be present.
  2. The stamped time must be within STALENESS_THRESHOLD_MIN of the system's
     current local time. A formatted-but-stale timestamp (e.g., reusing an
     anchor from earlier in the session) gets flagged just like a missing
     timestamp would.

Reads hook input JSON on stdin. If a problem is detected, emits a
PostToolUse `additionalContext` system reminder telling Claude to fix it.
Reminder-only — does not auto-edit the file.

Wired in .claude/settings.local.json under hooks.PostToolUse.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

INBOX_SUFFIX = "/" + os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad") + "/Inbox.md"
TIMESTAMP_RE = re.compile(
    r"^Last triaged:\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", re.MULTILINE
)
MARKER_RE = re.compile(r"^Last triaged:", re.MULTILINE)

# A stamped time more than this many minutes off from current local time is
# treated as stale (likely a reused anchor). 60 min lets a triage that takes
# a long /daily run still pass.
STALENESS_THRESHOLD_MIN = 60


def emit_reminder(text: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }
    print(json.dumps(out))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    fp = (
        (data.get("tool_input") or {}).get("file_path")
        or (data.get("tool_response") or {}).get("filePath")
        or ""
    )
    if not fp.endswith(INBOX_SUFFIX) or not os.path.isfile(fp):
        return 0

    try:
        with open(fp, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return 0

    if not MARKER_RE.search(content):
        return 0

    match = TIMESTAMP_RE.search(content)
    if not match:
        emit_reminder(
            'Inbox.md "Last triaged:" line is missing an HH:MM timestamp. '
            "Run `date` and write the current local time per the "
            "feedback_inbox_format / feedback_fresh_timestamp memory rules."
        )
        return 0

    date_str, hh, mm = match.group(1), int(match.group(2)), int(match.group(3))
    try:
        stamped = datetime.strptime(
            f"{date_str} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return 0

    # System local time. WSL inherits the host's timezone; on this user's
    # machine `date` reports EDT, so naive datetime arithmetic is fine.
    now = datetime.now()
    delta_min = abs((now - stamped).total_seconds()) / 60.0

    if delta_min > STALENESS_THRESHOLD_MIN:
        emit_reminder(
            f'Inbox.md "Last triaged: {date_str} {hh:02d}:{mm:02d}" appears '
            f"stale ({int(delta_min)} min off from current local time "
            f"{now.strftime('%Y-%m-%d %H:%M')}). Run `date` and write the "
            "actual current local time per feedback_fresh_timestamp."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
