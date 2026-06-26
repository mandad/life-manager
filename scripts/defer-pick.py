#!/usr/bin/env python3
"""defer-pick.py — Pick 1–2 Defer items to rotate into today's Today list.

Reads the Defer table in `AI Scratchpad/My Day.md`, scores each row by the
rotation formula documented in `Notes/Daily update routine.md` Step 4:

    block-fit  >  effort × deadline-proximity  >  aging
                  (with recovery-aware sizing + avoid-repeats as gates)

The /daily routine still scans active task lists for deferred items first;
this script supplements that scan with a deterministic ranking over the
explicit Defer table.

Run from the LLM_Land root:

    python3 scripts/defer-pick.py [--today YYYY-MM-DD]
                                  [--recovery green|yellow|red]
                                  [--blocks tag1,tag2]
                                  [--count N]

Output: top N picks (default 2) with a one-line reason each, plus a Skipped
section listing entries blocked by gates.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

TASKS = Path("AI Scratchpad/My Day.md")

# Effort token (case-insensitive) → minutes.
EFFORT_MINUTES = {
    "5m": 5, "10m": 10, "15m": 15, "20m": 20, "30m": 30, "45m": 45,
    "1hr": 60, "1.5hr": 90, "2hr": 120, "3hr": 180, "4hr": 240,
    "half-day": 240, "full-day": 480,
    "unknown": 90,
}

WEEKDAY_BLOCKS = {
    0: {"monday"},
    1: {"tuesday"},
    2: {"wednesday"},
    3: {"thursday"},
    4: {"friday"},
    5: {"saturday", "weekend"},
    6: {"sunday", "weekend"},
}

DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
TAG_RE = re.compile(r"#([A-Za-z][\w/-]*)")
EFFORT_RE = re.compile(
    r"\(?(\d+m|\d+(?:\.\d+)?hr|half-day|full-day|unknown)\)?",
    re.IGNORECASE,
)


def find_defer_section(lines: list[str]) -> tuple[int | None, int | None]:
    """Find the Defer section bounds. Matches `### Defer ...` or `## Defer ...`."""
    start: int | None = None
    end: int | None = None
    header_level: int | None = None
    for i, line in enumerate(lines):
        if start is None:
            m = re.match(r"^(#{2,3})\s+Defer\b", line, re.IGNORECASE)
            if m:
                start = i
                header_level = len(m.group(1))
            continue
        # End at the next header of the same or shallower level.
        m = re.match(r"^(#{1,6})\s+", line)
        if m and len(m.group(1)) <= (header_level or 3):
            end = i
            break
    if start is None:
        return None, None
    return start, end if end is not None else len(lines)


def parse_table(lines: list[str]) -> list[dict[str, str]]:
    """Parse a markdown pipe table. Returns one dict per data row."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            # End of table when we hit a non-table line after starting.
            if header is not None and rows:
                # Allow blank lines mid-table? No — table ends at first non-pipe.
                pass
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            continue  # separator
        if header is None:
            header = [c.lower().strip() for c in cells]
            continue
        if len(cells) != len(header):
            continue
        rows.append({header[i]: cells[i] for i in range(len(header))})
    return rows


def parse_date(s: str) -> dt.date | None:
    if not s or s.strip() in {"—", "-", ""}:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_effort(s: str) -> int:
    if not s:
        return EFFORT_MINUTES["unknown"]
    m = EFFORT_RE.search(s)
    token = (m.group(1) if m else s).strip().lower()
    if token in EFFORT_MINUTES:
        return EFFORT_MINUTES[token]
    # Try numeric hour pattern like "2.5hr"
    hr = re.match(r"^(\d+(?:\.\d+)?)hr$", token)
    if hr:
        return int(float(hr.group(1)) * 60)
    mn = re.match(r"^(\d+)m$", token)
    if mn:
        return int(mn.group(1))
    return EFFORT_MINUTES["unknown"]


def parse_tags(blob: str) -> set[str]:
    return {m.group(1).lower() for m in TAG_RE.finditer(blob or "")}


def score(row: dict[str, str], today: dt.date, recovery: str,
          blocks: set[str]) -> dict | None:
    task = row.get("task") or row.get("item") or ""
    effort_min = parse_effort(row.get("effort", ""))
    tags = parse_tags(" ".join([row.get("tags", ""), task, row.get("notes / source", ""),
                                row.get("notes", "")]))
    deferred_since = parse_date(row.get("deferred-since", ""))
    last_surfaced = parse_date(row.get("last-surfaced", ""))
    deadline = parse_date(row.get("deadline", ""))

    # Gate: avoid-repeats — skip if surfaced within last 2 days.
    if last_surfaced and (today - last_surfaced).days < 2:
        return {"task": task, "skipped": "surfaced < 2d ago"}

    # Gate: recovery-aware sizing.
    if recovery == "red" and effort_min > 30:
        return {"task": task, "skipped": f"effort {effort_min}m > 30m on red day"}
    if recovery == "yellow" and effort_min > 60:
        return {"task": task, "skipped": f"effort {effort_min}m > 60m on yellow day"}

    components: dict[str, int] = {}

    # Block-fit: largest single weight (1000).
    components["block_fit"] = 1000 if (tags & blocks) else 0

    # Effort × deadline-proximity (the user's added factor):
    #   - past-due → flat 600 (urgent but capped to not dominate block-fit on green days)
    #   - within 30 days → (30 - days_until) * (1 / effort_hr) * 10
    #     small effort + tight deadline → high score
    effort_hr = max(effort_min / 60.0, 0.25)
    if deadline:
        days_until = (deadline - today).days
        if days_until < 0:
            components["deadline_proximity"] = 600
        elif days_until <= 30:
            tightness = 30 - days_until  # 0..30
            ratio = tightness / effort_hr
            components["deadline_proximity"] = int(ratio * 10)
        else:
            components["deadline_proximity"] = 0
    else:
        components["deadline_proximity"] = 0

    # Aging: 5 points per day deferred.
    if deferred_since:
        age = max((today - deferred_since).days, 0)
        components["aging"] = age * 5
    else:
        components["aging"] = 0

    total = sum(components.values())
    return {
        "task": task,
        "effort_min": effort_min,
        "tags": tags,
        "deferred_since": deferred_since,
        "last_surfaced": last_surfaced,
        "deadline": deadline,
        "components": components,
        "total": total,
    }


def format_pick(pick: dict, today: dt.date) -> str:
    parts: list[str] = []
    if pick.get("deadline"):
        days = (pick["deadline"] - today).days
        parts.append(f"deadline {pick['deadline'].isoformat()} ({days:+d}d)")
    if pick["components"].get("block_fit"):
        parts.append("block-fit")
    if pick.get("deferred_since"):
        age = (today - pick["deferred_since"]).days
        parts.append(f"deferred {age}d")
    parts.append(f"effort {pick['effort_min']}m")
    return f"- **{pick['task']}** — score {pick['total']} — {', '.join(parts)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Pick Defer items for today's rotation.")
    ap.add_argument("--today", help="Override today (YYYY-MM-DD).")
    ap.add_argument("--recovery", choices=["green", "yellow", "red"], default="green",
                    help="Whoop recovery color; gates effort sizing.")
    ap.add_argument("--blocks", default="",
                    help="Comma-separated block tags available today "
                         "(e.g. 'wifi-evening,weekend,reftra'). "
                         "Day-of-week tag is added automatically.")
    ap.add_argument("--count", type=int, default=2, help="Max picks to return.")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    weekday_blocks = WEEKDAY_BLOCKS.get(today.weekday(), set())
    user_blocks = {b.strip().lower() for b in args.blocks.split(",") if b.strip()}
    blocks = weekday_blocks | user_blocks

    if not TASKS.exists():
        print(f"Tasks file not found: {TASKS}", file=sys.stderr)
        sys.exit(1)

    lines = TASKS.read_text(encoding="utf-8").splitlines()
    start, end = find_defer_section(lines)
    if start is None:
        print("No Defer section found in My Day.md", file=sys.stderr)
        sys.exit(1)

    rows = parse_table(lines[start:end])
    if not rows:
        print("No table rows under Defer section. Restructure Defer as a table first.",
              file=sys.stderr)
        sys.exit(1)

    eligible: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        result = score(row, today, args.recovery, blocks)
        if result is None:
            continue
        if result.get("skipped"):
            skipped.append(result)
        else:
            eligible.append(result)

    eligible.sort(key=lambda p: p["total"], reverse=True)
    picks = eligible[: args.count]

    print(f"# Defer rotation picks — {today.isoformat()}")
    print(f"_Recovery: {args.recovery}; blocks: {sorted(blocks) or '(none)'}; "
          f"eligible: {len(eligible)}, skipped: {len(skipped)}_\n")

    if picks:
        for p in picks:
            print(format_pick(p, today))
    else:
        print("_No eligible picks today._")

    if skipped:
        print("\n## Skipped\n")
        for s in skipped:
            print(f"- {s['task']} — {s['skipped']}")


if __name__ == "__main__":
    main()
