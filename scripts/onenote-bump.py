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
  python3 scripts/onenote-bump.py --date 2026-09-26 --all --exclude "Room Layout" "Buy Wish List"
  add --write to apply; without it the script is a dry run.
  --all             name EVERY row of the Stored-page-hashes table (automation #57, 2026-09-26)
                    — replaces the hand-rolled awk rows-file. `--exclude` is the only opt-out:
                    list the frozen rows and the day's hand-handled edited page there. The
                    excluded set is printed, and an --exclude title that matches no row is an
                    error (exit 1, nothing written), same as a missing --rows title.
  --days-static N   also rewrites a "(walked; unchanged since … — N days)" style
                    annotation's day count on CME-Relief-type cells (optional).
  --check-tables    only run the table-integrity check below and exit (for /weekly hygiene).

Table integrity (automation #60, 2026-10-06): before any write, both tables are checked for a
row split across lines — a line starting with `|` that doesn't close with `|`, or any non-table
line sitting between two table rows. The 9/30 run put bullets inside the FA All Hands watchlist
cell; Obsidian broke the table there and the bump silently reached 5 of 6 watchlist cells. On a
split row the script prints the row title + line range and exits 3 WITHOUT writing.

Exit codes: 0 ok · 1 a named row was not found (nothing written) · 2 file error ·
            3 a table row is split across lines (nothing written).
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


TABLE_SECTIONS = ("## Active-page watchlist", "## Stored page hashes")


def table_breaks(lines: list[str]) -> list[str]:
    """Problems in the two state tables: a row opened with `|` but not closed on its line, or a
    non-blank non-table line between the first and last table row of a section."""
    problems: list[str] = []
    for heading in TABLE_SECTIONS:
        start = next((i for i, l in enumerate(lines) if l.startswith(heading)), None)
        if start is None:
            problems.append(f"section {heading!r} not found")
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        rows = [i for i in range(start + 1, end) if lines[i].startswith("|")]
        if not rows:
            continue
        last_title = "?"
        for i in range(rows[0], rows[-1] + 1):
            l = lines[i]
            if l.startswith("|"):
                cells = split_row(l)
                if cells:
                    last_title = row_title(cells) or last_title
                elif not l.rstrip().endswith("|"):
                    m = re.match(r"\|\s*(\*\*[^*]+\*\*|`[^`]+`\s*\|\s*[^|]+)", l)
                    last_title = norm(m.group(1).split("|")[-1]) if m else last_title
                    problems.append(f"{heading[3:]}: line {i + 1} opens a row that doesn't close "
                                    f"on its line (row {last_title!r})")
            elif l.strip():
                problems.append(f"{heading[3:]}: line {i + 1} is inside the table but isn't a row "
                                f"(after row {last_title!r}): {l.strip()[:60]!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="YYYY-MM-DD to write into the Last checked cell")
    ap.add_argument("--check-tables", action="store_true",
                    help="only check both tables for rows split across lines, then exit (0 ok · 3 broken)")
    ap.add_argument("--rows", nargs="*", default=[], help="page titles to bump")
    ap.add_argument("--rows-file", help="file with one page title per line (# comments ok)")
    ap.add_argument("--all", action="store_true",
                    help="name every row of the Stored-page-hashes table (use --exclude to opt rows out)")
    ap.add_argument("--exclude", nargs="*", default=[],
                    help="with --all: page titles to leave untouched (frozen rows, hand-handled edits)")
    ap.add_argument("--file", default=str(DEFAULT_FILE), help="path to onenote-sync-state.md")
    ap.add_argument("--write", action="store_true", help="apply the change (default: dry run)")
    ap.add_argument("--days-static", type=int, help="rewrite an 'N days' annotation on matched cells")
    args = ap.parse_args()

    if args.check_tables:
        try:
            chk_lines = Path(args.file).read_text(encoding="utf-8").split("\n")
        except OSError as e:
            print(f"ERROR: cannot read {args.file}: {e}")
            return 2
        probs = table_breaks(chk_lines)
        for p in probs:
            print(f"  TABLE BROKEN — {p}")
        print("  tables OK" if not probs else f"  {len(probs)} table problem(s)")
        return 3 if probs else 0

    if not args.date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"ERROR: --date must be YYYY-MM-DD, got {args.date!r}")
        return 2

    if args.all and (args.rows or args.rows_file):
        print("ERROR: --all names every hash-table row; don't combine it with --rows/--rows-file.")
        return 2
    if args.exclude and not args.all:
        print("ERROR: --exclude only applies with --all (with --rows, just leave the title out).")
        return 2
    if args.all and args.days_static is not None:
        # --days-static rewrites the first "N days" in EVERY bumped cell — with --all that
        # corrupts unrelated annotations ("~1–2 days" → "~1–78 days", caught 2026-09-26).
        print("ERROR: --days-static is for one named row (e.g. --rows \"CME Relief\"); never with --all.")
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
    if args.days_static is not None and len(wanted) != 1:
        # same corruption as --all, via a multi-row --rows-file (2026-10-26: "~4 days diving" → "~108 days")
        print("ERROR: --days-static is for exactly one named row (e.g. --rows \"CME Relief\").")
        return 2
    path = Path(args.file)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot read {path}: {e}")
        return 2
    lines = text.split("\n")

    probs = table_breaks(lines)
    if probs:
        for p in probs:
            print(f"  TABLE BROKEN — {p}")
        print("  → nothing written: re-join the split row(s) onto one line first (Edit, ` · ` separators).")
        return 3

    excluded: list[str] = []
    if args.all:
        table: dict[str, str] = {}      # norm title -> display title, hash-table rows only
        for line in lines:
            cells = split_row(line)
            if cells and cells[0].strip().startswith("`"):
                table.setdefault(norm(cells[1]), cells[1].strip())
        bad = [t for t in args.exclude if norm(t) not in table]
        if bad:
            for t in bad:
                print(f"  MISSING exclude {t!r}: no Stored-page-hashes row with that title")
            print("  → nothing written: fix the --exclude title(s) first.")
            return 1
        excl_norm = {norm(t) for t in args.exclude}
        excluded = [table[t] for t in table if t in excl_norm]
        wanted = [table[t] for t in table if t not in excl_norm]

    if not wanted:
        print("ERROR: no rows named (use --rows, --rows-file or --all); refusing to bump anything.")
        return 2
    wanted_norm = {norm(t): t for t in wanted}

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
    if args.all:
        print(f"  --all: {len(wanted_norm)} hash-table titles named · excluded ({len(excluded)}): "
              + (", ".join(repr(t) for t in excluded) or "none"))
        watch = [row_title(c) for c in (split_row(l) for l in lines)
                 if c and c[0].strip().startswith("**")]
        reach = sum(1 for t in watch if t in wanted_norm)
        print(f"  watchlist rows: {len(watch)} in table · {reach} reached by this bump "
              f"(the rest are excluded or watchlist-only titles)")
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
