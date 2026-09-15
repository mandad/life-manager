#!/usr/bin/env python3
"""onenote-bump.py — bump the `Last checked` cells in onenote-sync-state.md by NAMED ROW.

Automation #53 (approved 2026-09-08). Replaces the EOL-anchored herd sed
(`sed 's/<prev> |$/<today> |/'`) that re-bumped rows which should have FROZEN on
their age-out day (PARS Brief 9/04, Long Range AIS 9/07).

The script touches exactly the rows you name — never a row you didn't — and
rewrites only the leading ISO date of the last cell, preserving any prose
annotation after it (`(ink-only, 201 strokes …)`, `(walked; unchanged …)`).

Rows are matched by page TITLE, case-insensitively, trailing whitespace ignored,
in BOTH tables of the state file:
  * Stored page hashes:   | `id` | Title | Section | Created | `hash` | Last checked |
  * Active-page watchlist: | **Title** | Section | `id` | signature | Last checked |
A title that appears in both tables is bumped in both (that is the intended
behaviour for watchlisted pages that are still inside the hash window).

Usage
  python3 scripts/onenote-bump.py --date 2026-09-08 --rows "CME Notes" "Nome Cab" …
  python3 scripts/onenote-bump.py --date 2026-09-08 --rows-file /path/list.txt   # one title per line
  add --write to apply; without it the script is a dry run.
  --days-static N   also rewrites a "(walked; unchanged since … — N days)" style
                    annotation's day count on CME-Relief-type cells (optional).

Exit codes: 0 ok · 1 a named row was not found (nothing written) · 2 file error.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_FILE = (
    Path(__file__).resolve().parent.parent
    / "AI Scratchpad" / "Notes" / "_daily-data" / "onenote-sync-state.md"
)
DATE_RE = re.compile(r"^(\s*)(\d{4}-\d{2}-\d{2})(.*)$")
DAYS_RE = re.compile(r"(\d+) days")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("**", "").replace("`", "")).strip().lower()


def split_row(line: str) -> list[str] | None:
    """Return the cells of a markdown table row, or None if the line isn't one."""
    s = line.rstrip("\n")
    if not s.startswith("|") or not s.rstrip().endswith("|"):
        return None
    inner = s.strip()[1:-1]
    cells = inner.split("|")
    return cells if len(cells) >= 5 else None


def row_title(cells: list[str]) -> str | None:
    c0 = cells[0].strip()
    if c0.startswith("`"):            # hash-table row: id | Title | …
        return norm(cells[1])
    if c0.startswith("**"):           # watchlist row: **Title** | …
        return norm(c0)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD to write into the Last checked cell")
    ap.add_argument("--rows", nargs="*", default=[], help="page titles to bump")
    ap.add_argument("--rows-file", help="file with one page title per line (# comments ok)")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="path to onenote-sync-state.md")
    ap.add_argument("--write", action="store_true", help="apply the change (default: dry run)")
    ap.add_argument("--days-static", type=int, help="rewrite an 'N days' annotation on matched cells")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"ERROR: --date must be YYYY-MM-DD, got {args.date!r}")
        return 2

    wanted: list[str] = list(args.rows)
    if args.rows_file:
        try:
            for raw in Path(args.rows_file).read_text(encoding="utf-8").splitlines():
                t = raw.strip()
                if t and not t.startswith("#"):
                    wanted.append(t)
        except OSError as e:
            print(f"ERROR: cannot read --rows-file: {e}")
            return 2
    if not wanted:
        print("ERROR: no rows named (use --rows or --rows-file); refusing to bump anything.")
        return 2
    wanted_norm = {norm(t): t for t in wanted}

    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot read {path}: {e}")
        return 2
    lines = text.split("\n")

    def count_ending(date: str) -> int:
        return sum(1 for l in lines if l.rstrip().endswith(f"{date} |"))

    before_today = count_ending(args.date)
    changed: list[tuple[str, str, str]] = []   # (title, old cell, new cell)
    skipped: list[tuple[str, str]] = []        # (title, reason)
    found: set[str] = set()

    for i, line in enumerate(lines):
        cells = split_row(line)
        if not cells:
            continue
        title = row_title(cells)
        if title is None or title not in wanted_norm:
            continue
        found.add(title)
        last = cells[-1]
        m = DATE_RE.match(last)
        if not m:
            skipped.append((wanted_norm[title], f"last cell has no leading date: {last.strip()!r}"))
            continue
        lead, old_date, rest = m.groups()
        if old_date == args.date and args.days_static is None:
            skipped.append((wanted_norm[title], f"already {args.date}"))
            continue
        new_rest = rest
        if args.days_static is not None and DAYS_RE.search(rest):
            new_rest = DAYS_RE.sub(f"{args.days_static} days", rest, count=1)
        new_last = f"{lead}{args.date}{new_rest}"
        cells[-1] = new_last
        lines[i] = "|" + "|".join(cells) + "|"
        changed.append((wanted_norm[title], last.strip(), new_last.strip()))

    missing = [wanted_norm[t] for t in wanted_norm if t not in found]

    mode = "WRITE" if args.write else "DRY RUN"
    print(f"onenote-bump [{mode}] → {path.name} · date {args.date}")
    for t, old, new in changed:
        print(f"  bump   {t!r}: {old} → {new}")
    for t, why in skipped:
        print(f"  skip   {t!r}: {why}")
    for t in missing:
        print(f"  MISSING {t!r}: no row with that title in either table")
    print(f"  rows named {len(wanted_norm)} · matched {len(found)} · to change {len(changed)} · skipped {len(skipped)} · missing {len(missing)}")

    if missing:
        print("  → nothing written: fix the missing title(s) first (a typo here is how a frozen row gets touched).")
        return 1

    # Only cells that END with the bare date count toward the EOL tally — annotated cells
    # ("2026-09-08 (ink-only …)") are bumped but never match the `<date> |` suffix.
    after_today = before_today + sum(1 for _, _, new in changed if new.endswith(args.date))
    print(f"  cells ending '{args.date} |': {before_today} → {after_today} (expected)")
    if args.write and changed:
        path.write_text("\n".join(lines), encoding="utf-8")
        actual = count_ending(args.date)
        print(f"  written · cells ending '{args.date} |' now {actual}")
        if actual != after_today:
            print("  ⚠️ count mismatch after write — inspect the table by hand")
            return 1
    elif args.write:
        print("  nothing to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
