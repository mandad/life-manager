#!/usr/bin/env python3
"""Render a RescueTime hourly activity JSON dump as a compact timeline.

Input: JSON output from `rescuetime_query_activity_data` with kind='activity'
       and resolution='hour'. Read from stdin, or from a path passed as argv[1].

Output (stdout):
    07:00 — Obsidian (35m), Claude (15m)
    08:00 — email (40m)
    09:00 — Claude (50m) ← deep work block
    ...

Used by /daily routine Step 5. Pure transform — no API calls.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

DEEP_WORK_APPS = {"claude", "obsidian", "Visual Studio Code", "Windows Terminal", "lightroom", "Google Documents"}
DEEP_WORK_MIN_SECONDS = 30 * 60  # 30 minutes of one app in an hour
TOP_N_PER_HOUR = 2  # show this many top apps per hour


def fmt_minutes(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return f"{seconds}s"
    if minutes < 60:
        return f"{minutes}m"
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


def render(payload: dict) -> str:
    rows = payload.get("rows", [])
    by_hour: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for r in rows:
        ts = r.get("date", "")
        # ts format: "2026-05-02T14:00:00"
        try:
            hour = ts.split("T", 1)[1][:5]
        except IndexError:
            continue
        secs = int(r.get("time_seconds", 0))
        act = r.get("activity", "?")
        if secs > 0:
            by_hour[hour].append((secs, act))

    out_lines: list[str] = []
    for hour in sorted(by_hour):
        items = sorted(by_hour[hour], key=lambda x: x[0], reverse=True)
        top = items[:TOP_N_PER_HOUR]
        rendered = ", ".join(f"{act} ({fmt_minutes(secs)})" for secs, act in top)
        annotation = ""
        if any(secs >= DEEP_WORK_MIN_SECONDS and act in DEEP_WORK_APPS for secs, act in top):
            annotation = " ← deep work block"
        out_lines.append(f"{hour} — {rendered}{annotation}")

    return "\n".join(out_lines)


def main() -> int:
    if len(sys.argv) > 1:
        text = open(sys.argv[1]).read()
    else:
        text = sys.stdin.read()
    payload = json.loads(text)
    print(render(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
