#!/usr/bin/env python3
"""Inbox-triage guard for /daily runs — UserPromptSubmit + Stop hook.

Why this exists (2026-09-25): the PostToolUse hook `inbox-triage-hook.py` only
fires when Inbox.md is EDITED, so it cannot catch the two real failures:
  * a /daily run that never touches the Inbox at all (9/16–9/19), and
  * a run that reads an empty Inbox but never re-stamps it (9/21–9/25) —
    which leaves "Last triaged:" stale, so a checked inbox looks identical to
    a skipped one from the user's side.

UserPromptSubmit: if the prompt is a /daily invocation, inject the Inbox's
  current "Last triaged" stamp + New items into context, and write a marker
  recording that a /daily run started today.
Stop: if a /daily marker exists and Inbox "Last triaged" is older than the
  marker's date, block the stop ONCE with instructions. On a later stop (or when
  stop_hook_active is set) it lets the turn end and shows the user a warning
  instead, so it can never loop. The marker is cleared once satisfied or warned.

Reads hook JSON on stdin; dispatches on `hook_event_name`. Silent (exit 0, no
output) whenever there is nothing to do. Wired in .claude/settings.local.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INBOX = REPO / os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad") / "Inbox.md"
MARKER = Path.home() / ".cache" / "llm-land" / "inbox-daily-marker.json"

# Phrases that start a /daily run (mirrors CLAUDE.md "Daily task routine").
DAILY_RE = re.compile(
    r"(^|[\s(])/daily\b|\bdaily update\b|update today'?s tasks|refresh today",
    re.IGNORECASE,
)
STAMP_RE = re.compile(r"^Last triaged:\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?", re.MULTILINE)


def read_inbox() -> tuple[str | None, str | None, list[str]]:
    """Return (stamp_date, stamp_time, new_items)."""
    try:
        text = INBOX.read_text(encoding="utf-8")
    except OSError:
        return None, None, []
    m = STAMP_RE.search(text)
    stamp_date, stamp_time = (m.group(1), m.group(2)) if m else (None, None)
    items: list[str] = []
    in_new = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_new = line.strip().lower() == "## new items"
            continue
        if in_new:
            s = line.strip()
            if s and s not in ("-", "*", "- [ ]"):
                items.append(s)
    return stamp_date, stamp_time, items


def load_marker() -> dict | None:
    try:
        return json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_marker(data: dict) -> None:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps(data), encoding="utf-8")


def clear_marker() -> None:
    try:
        MARKER.unlink()
    except OSError:
        pass


def on_prompt(data: dict) -> None:
    prompt = data.get("prompt") or ""
    if not DAILY_RE.search(prompt):
        return
    today = date.today().isoformat()
    save_marker({"date": today, "blocked": False})
    stamp_date, stamp_time, items = read_inbox()
    if stamp_date:
        age = (date.today() - date.fromisoformat(stamp_date)).days
        stamp = f"{stamp_date} {stamp_time or '??:??'} ({age} day{'s' if age != 1 else ''} ago)"
    else:
        stamp = "MISSING"
    listing = "\n".join(f"  {i}" for i in items) if items else "  (none — the section holds only the empty `-` bullet)"
    ctx = (
        "📥 INBOX CHECK (inbox-daily-guard hook) — /daily run detected.\n"
        f"Inbox.md \"Last triaged:\" {stamp}. New items ({len(items)}):\n{listing}\n"
        "Step 3 is non-skippable: triage every item above (Inbox = the user's own captures only), "
        "then re-stamp \"Last triaged:\" with the current local time (run `date`) EVEN IF THE INBOX IS EMPTY — "
        "the stamp is the user's only evidence the check happened. Keep one empty `-` bullet under \"## New items\". "
        "A Stop hook will block ending this run once if the stamp isn't dated today."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}}))


def on_stop(data: dict) -> None:
    marker = load_marker()
    if not marker or not marker.get("date"):
        return
    stamp_date, stamp_time, items = read_inbox()
    if stamp_date and stamp_date >= marker["date"]:
        clear_marker()  # satisfied
        return
    shown = f"{stamp_date} {stamp_time or ''}".strip() if stamp_date else "missing"
    if not marker.get("blocked") and not data.get("stop_hook_active"):
        marker["blocked"] = True
        save_marker(marker)
        n = len(items)
        print(json.dumps({
            "decision": "block",
            "reason": (
                f"/daily Step 3 not closed: Inbox.md \"Last triaged:\" is still {shown} "
                f"({n} new item{'s' if n != 1 else ''}). Triage any items, then re-stamp \"Last triaged:\" "
                "with the current local time from `date` — even if the Inbox is empty — before ending the run."
            ),
        }))
        return
    clear_marker()  # already nudged once — never loop; tell the user instead
    print(json.dumps({
        "systemMessage": f"⚠️ /daily ended without re-stamping Inbox.md \"Last triaged\" (still {shown})."
    }))


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    event = data.get("hook_event_name")
    try:  # invocation log — proves the hook is actually loaded (added 2026-09-27; it wasn't firing)
        log = Path.home() / ".cache" / "llm-land" / "inbox-guard.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {event}\n")
    except OSError:
        pass
    try:
        if event == "UserPromptSubmit":
            on_prompt(data)
        elif event == "Stop":
            on_stop(data)
    except Exception:  # a guard must never break the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
