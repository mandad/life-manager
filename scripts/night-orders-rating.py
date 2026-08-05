#!/usr/bin/env python3
"""night-orders-rating.py — transfer the My Day night-orders 👍/👎 rating into the registry.

Reads the two rating checkboxes under the night-orders fact in
`AI Scratchpad/My Day.md` and writes the derived thumb into the single
matching row of `AI Scratchpad/Notes/_daily-data/night-orders-facts.md`.

Design (build #31) — this script's ENTIRE write surface is one existing
line of the registry. It never appends rows, never inserts or deletes
lines, never touches blank lines. That is what makes it safe by
construction against the table-corruption bugs markdown-table edits have
caused this week (a whitespace mismatch, an append-position bug, and the
general hazard that a blank line after a table row splits it into two
tables in Obsidian).

Row key: the date is parsed from My Day's `## Today — ... (YYYY-MM-DD)`
header, NOT `today - 1`. At transfer time (Step 4, before the daily
rebuild) that header still reads the *previous* day's date — deriving
from the system date would break on late runs and around gaps in the
registry (e.g. the in-port pause, which left a real 7/21 → 7/28 gap).
Override with --date when needed.

Checkbox read is bounded to the `### 🌙 Night Orders note` block (up to
the next `###` heading) and matched against `- [ ]`/`- [x]` lines that
are also tagged 👍/👎 — `- [x]` lines exist all over My Day, so both
conditions matter. Any trailing text typed after the canonical labels
("more like this" / "less like this") is captured as feedback, from
either line, regardless of its own check state.

Idempotent: re-running after a successful write reports "already
recorded" and makes no further changes. A registry cell that already
holds something different from the derived thumb (e.g. a user-written
"👍👍") is never overwritten — reported as a CONFLICT for a human to
adjudicate instead.

Does NOT reset the My Day checkboxes — the daily rebuild owns My Day and
regenerates the whole Night Orders block minutes later anyway; a mid-run
edit underneath the rebuilding agent's in-memory view is exactly the
stale-view clobber pattern that has destroyed user data before. One
writer per file: this script writes the registry, the agent writes My
Day.

Usage:
    python3 scripts/night-orders-rating.py                    # read + write + report
    python3 scripts/night-orders-rating.py --dry-run           # show what would happen; no write
    python3 scripts/night-orders-rating.py --date 2026-08-03   # override the registry row key
    python3 scripts/night-orders-rating.py --json              # machine-readable result

Exit code is always 0 — on anything unexpected (missing file, changed
table structure, unparseable date) this degrades to a single graceful
outcome line rather than raising. Every outcome line is prefixed
"NIGHT-ORDERS RATING:" and states the resolved date, so the daily
routine can grep it unambiguously.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

VAULT = Path(os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad"))
MY_DAY = VAULT / "My Day.md"
REGISTRY = VAULT / "Notes/_daily-data/night-orders-facts.md"

PREFIX = "NIGHT-ORDERS RATING"

HEADER_DATE_RE = re.compile(r"^## Today\b.*?(\d{4}-\d{2}-\d{2})", re.MULTILINE)

NIGHT_ORDERS_BLOCK_RE = re.compile(
    r"^### 🌙 Night Orders note\n(.*?)(?=^### |\Z)", re.MULTILINE | re.DOTALL
)
CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(👍|👎)\s*(.*)$", re.MULTILINE)

CANONICAL_LABEL = {"👍": "more like this", "👎": "less like this"}

# Obsidian requires `\|` to escape a literal pipe inside a table cell, so an
# unescaped `|` is a real column separator. Split on it (negative lookbehind
# skips escaped pipes) to get exact cell boundaries without touching content.
PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")


# --------------------------------------------------------------------------- helpers


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def escape_pipes(text: str) -> str:
    return re.sub(r"(?<!\\)\|", r"\\|", text)


def split_row(line: str) -> list[str]:
    return PIPE_SPLIT_RE.split(line)


def resolve_date(my_day_text: str, override: str | None) -> tuple[str | None, str]:
    """Return (date, source) on success, or (None, error_reason)."""
    if override is not None:
        try:
            dt.date.fromisoformat(override)
        except ValueError:
            return None, f"--date {override!r} is not a valid YYYY-MM-DD date"
        return override, "override"
    m = HEADER_DATE_RE.search(my_day_text)
    if not m:
        return None, "no '## Today — ... (YYYY-MM-DD)' header found in My Day.md"
    return m.group(1), "header"


def parse_boxes(my_day_text: str) -> tuple[dict | None, str | None]:
    """Return (boxes, None) or (None, error). boxes: {'👍': {...}, '👎': {...}}."""
    block_m = NIGHT_ORDERS_BLOCK_RE.search(my_day_text)
    if not block_m:
        return None, "could not locate the '### 🌙 Night Orders note' block in My Day.md"
    block = block_m.group(1)
    found: dict[str, dict] = {}
    for cm in CHECKBOX_RE.finditer(block):
        checked = cm.group(1) in ("x", "X")
        emoji = cm.group(2)
        trailing = cm.group(3).strip()
        canonical = CANONICAL_LABEL[emoji]
        if trailing == canonical:
            extra = ""
        elif trailing.startswith(canonical):
            extra = trailing[len(canonical):].strip(" \t-–—:")
        else:
            extra = trailing
        found[emoji] = {"checked": checked, "extra": extra}
    if "👍" not in found or "👎" not in found:
        return None, "could not find both 👍 and 👎 checkbox lines in the Night Orders block"
    return found, None


def collect_feedback(boxes: dict) -> str:
    fragments = [boxes[e]["extra"] for e in ("👍", "👎") if boxes[e]["extra"]]
    return "<br>".join(fragments)


def locate_registry_row(registry_text: str, date: str) -> dict:
    """Return dict with lines, header_idx, row_idx, error (mutually exclusive w/ idx)."""
    lines = registry_text.split("\n")
    result: dict = {"lines": lines, "header_idx": None, "row_idx": None, "error": None}

    start_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "## Log":
            start_idx = i
            break
    if start_idx is None:
        result["error"] = "no '## Log' section found in registry"
        return result

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break

    pipe_idxs = [i for i in range(start_idx, end_idx) if lines[i].lstrip().startswith("|")]
    if not pipe_idxs:
        result["error"] = "no table found under '## Log'"
        return result

    result["header_idx"] = pipe_idxs[0]
    row_re = re.compile(r"^\|\s*" + re.escape(date) + r"\s*\|")
    matches = [i for i in pipe_idxs[1:] if row_re.match(lines[i])]
    if not matches:
        result["error"] = f"no row for {date} found under '## Log'"
        return result
    if len(matches) > 1:
        result["error"] = f"multiple rows matched {date} under '## Log' — refusing to guess"
        return result
    result["row_idx"] = matches[0]
    return result


def column_index(header_line: str, name: str) -> int | None:
    for idx, cell in enumerate(split_row(header_line)):
        if cell.strip() == name:
            return idx
    return None


def build_new_row(row_line: str, rating_idx: int, feedback_idx: int, thumb: str, feedback_text: str) -> str:
    cells = split_row(row_line)
    cells[rating_idx] = f" {thumb} "
    if feedback_text:
        existing_stripped = cells[feedback_idx].strip()
        escaped = escape_pipes(feedback_text)
        new_feedback = f"{existing_stripped}<br>{escaped}" if existing_stripped else escaped
        cells[feedback_idx] = f" {new_feedback} "
    return "|".join(cells)


# --------------------------------------------------------------------------- reporting


def emit(status: str, date: str | None, message: str, *, as_json: bool,
          extra: dict | None = None, print_trailer: bool = False) -> int:
    if as_json:
        payload = {"status": status, "date": date, "message": message}
        if extra:
            payload.update(extra)
        if print_trailer:
            payload["boxes_reset"] = False
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"{PREFIX}: {message}")
        if print_trailer:
            print(f"{PREFIX}: boxes left set — rebuild resets them")
    return 0


# --------------------------------------------------------------------------- main


def run(args: argparse.Namespace) -> int:
    as_json = args.json

    my_day_text = read_text(MY_DAY)
    if my_day_text is None:
        return emit("skipped", None, f"could not read {MY_DAY} — skipped, no changes made", as_json=as_json)

    date, source_or_err = resolve_date(my_day_text, args.date)
    if date is None:
        return emit("skipped", None, f"{source_or_err} — skipped, no changes made", as_json=as_json)

    boxes, err = parse_boxes(my_day_text)
    if err:
        return emit("skipped", date, f"row {date} — {err} — skipped, no changes made", as_json=as_json)

    up = boxes["👍"]["checked"]
    down = boxes["👎"]["checked"]
    feedback_text = collect_feedback(boxes)

    if up and down:
        return emit(
            "ambiguous", date,
            f"AMBIGUOUS (both boxes checked) — row {date} not written, needs user clarification",
            as_json=as_json, extra={"feedback_captured": feedback_text or None}, print_trailer=True,
        )

    if not up and not down:
        msg = f"neutral (no box checked) — row {date} left blank (blank = neutral by convention)"
        if feedback_text:
            msg += f" — note: feedback text present ({feedback_text!r}) but not persisted since rating is neutral"
        return emit(
            "neutral", date, msg, as_json=as_json,
            extra={"feedback_captured": feedback_text or None}, print_trailer=True,
        )

    thumb = "👍" if up else "👎"

    registry_text = read_text(REGISTRY)
    if registry_text is None:
        return emit(
            "skipped", date,
            f"could not read {REGISTRY} — skipped, no changes made (boxes read: {thumb})",
            as_json=as_json, extra={"thumb": thumb}, print_trailer=True,
        )

    loc = locate_registry_row(registry_text, date)
    if loc["error"]:
        return emit(
            "skipped", date, f"{loc['error']} — skipped, no changes made (boxes read: {thumb})",
            as_json=as_json, extra={"thumb": thumb}, print_trailer=True,
        )

    lines = loc["lines"]
    header_line = lines[loc["header_idx"]]
    row_line = lines[loc["row_idx"]]

    rating_idx = column_index(header_line, "Rating")
    feedback_idx = column_index(header_line, "Feedback")
    if rating_idx is None or feedback_idx is None:
        return emit(
            "skipped", date,
            "could not locate Rating/Feedback columns from the header row — "
            "registry structure may have changed — skipped, no changes made",
            as_json=as_json, extra={"thumb": thumb}, print_trailer=True,
        )

    cells = split_row(row_line)
    if rating_idx >= len(cells) or feedback_idx >= len(cells):
        return emit(
            "skipped", date, "row cell count doesn't match header — skipped, no changes made",
            as_json=as_json, extra={"thumb": thumb}, print_trailer=True,
        )

    existing_rating = cells[rating_idx].strip()

    if existing_rating == thumb:
        return emit(
            "already_recorded", date, f"already recorded — row {date} already {thumb}, no change",
            as_json=as_json, extra={"existing": existing_rating, "thumb": thumb}, print_trailer=True,
        )

    if existing_rating:
        return emit(
            "conflict", date,
            f"CONFLICT — row {date} already {existing_rating}, boxes say {thumb} — not overwritten",
            as_json=as_json, extra={"existing": existing_rating, "thumb": thumb}, print_trailer=True,
        )

    # Existing Rating cell is blank -> this is the one write this script is allowed to make.
    new_row_line = build_new_row(row_line, rating_idx, feedback_idx, thumb, feedback_text)

    if args.dry_run:
        fb_note = " and Feedback cell" if feedback_text else ""
        msg = f"DRY RUN — would write {thumb} — row {date} Rating cell{fb_note} would update; no changes made"
        return emit(
            "dry_run", date, msg, as_json=as_json,
            extra={"thumb": thumb, "feedback_captured": feedback_text or None}, print_trailer=True,
        )

    lines[loc["row_idx"]] = new_row_line
    REGISTRY.write_text("\n".join(lines), encoding="utf-8", newline="\n")

    fb_note = " (+ feedback appended)" if feedback_text else ""
    msg = f"wrote {thumb} — row {date} Rating cell updated{fb_note}"
    return emit(
        "written", date, msg, as_json=as_json,
        extra={"thumb": thumb, "feedback_written": bool(feedback_text)}, print_trailer=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None,
                     help="override the registry row date (YYYY-MM-DD); default: parsed from "
                          "My Day's '## Today — ... (YYYY-MM-DD)' header")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; don't write")
    ap.add_argument("--json", action="store_true", help="emit a JSON result object instead of a text line")
    args = ap.parse_args()

    try:
        return run(args)
    except Exception as e:  # house style: never raise, degrade to one graceful line, exit 0
        message = f"error — unexpected {type(e).__name__}: {e} — degraded gracefully, no changes made"
        if args.json:
            print(json.dumps({"status": "error", "date": None, "message": message}, ensure_ascii=False))
        else:
            print(f"{PREFIX}: {message}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
