#!/usr/bin/env python3
"""weekly-prep.py — Mechanical inputs for the /weekly review.

Walks the Obsidian vault under `AI Scratchpad/` and emits a markdown report
covering the mechanical 80 percent of /weekly: broken wikilinks, orphan
candidates, stale files, tag usage, file counts (with delta vs the last prep
output), and a deadline radar pulling every open dated task across
`Projects/*/Tasks.md`.

Run from the LLM_Land root:

    python3 scripts/weekly-prep.py [--out PATH] [--today YYYY-MM-DD]

Default output prints to stdout. The /weekly routine consumes the report and
applies human judgment on top (archive decisions, work-log filtering, report
synthesis).
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

VAULT = Path("AI Scratchpad")
WEEKLY_DATA_DIR = VAULT / "Notes" / "_weekly-data"

ALWAYS_CURRENT = {
    "Tasks.md",
    "Inbox.md",
    "Index.md",
    "Memory digest.md",
    "MEMORY.md",
    "Daily update questions.md",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^|\]]*)?(?:\|[^\]]*)?\]\]")
TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")
CHECKBOX_LINE_RE = re.compile(r"^\s*-\s*\[\s*\]\s*(.+)$")

MONTH_LOOKUP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ---------- date parsing ----------

def _resolve_md(month: int, day: int, today: dt.date) -> dt.date | None:
    try:
        candidate = dt.date(today.year, month, day)
    except ValueError:
        return None
    # If the date is more than 30 days in the past, assume next year.
    if candidate < today - dt.timedelta(days=30):
        try:
            candidate = candidate.replace(year=today.year + 1)
        except ValueError:
            return None
    return candidate


def _resolve_month(name: str, day: int, year: str | None, today: dt.date) -> dt.date | None:
    key = name.lower()[:3]
    month = MONTH_LOOKUP.get(key)
    if not month:
        return None
    yr = int(year) if year else today.year
    try:
        candidate = dt.date(yr, month, day)
    except ValueError:
        return None
    if not year and candidate < today - dt.timedelta(days=30):
        try:
            candidate = candidate.replace(year=yr + 1)
        except ValueError:
            return None
    return candidate


# Order matters; the first non-None result wins.
DATE_PATTERNS = [
    # YYYY-MM-DD anywhere
    (
        re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
        lambda m, today: _safe_iso(m.group(1), m.group(2), m.group(3)),
    ),
    # M/D or MM/DD — must not be followed by another / (e.g. dates inside URLs)
    (
        re.compile(r"(?<![\w/])(\d{1,2})/(\d{1,2})(?![/\d])"),
        lambda m, today: _resolve_md(int(m.group(1)), int(m.group(2)), today),
    ),
    # Mon DD or Month DD with optional year
    (
        re.compile(
            r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
            r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
            r"\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
            re.IGNORECASE,
        ),
        lambda m, today: _resolve_month(m.group(1), int(m.group(2)), m.group(3), today),
    ),
]


def _safe_iso(y: str, m: str, d: str) -> dt.date | None:
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def first_date(text: str, today: dt.date) -> dt.date | None:
    """Return the earliest plausible date found in `text`, or None.

    Falls through patterns in DATE_PATTERNS order; takes the first match per
    pattern, then returns the minimum across patterns so vague dates don't
    shadow specific ones.
    """
    candidates: list[dt.date] = []
    for rx, builder in DATE_PATTERNS:
        m = rx.search(text)
        if m:
            result = builder(m, today)
            if result is not None:
                candidates.append(result)
    if not candidates:
        return None
    # Prefer earliest future date over earliest past date; if all past, take latest past.
    future = [c for c in candidates if c >= today]
    if future:
        return min(future)
    return max(candidates)


# ---------- vault scanning ----------

def walk_md(root: Path):
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.parts):
            continue
        yield path


def file_basenames(root: Path) -> dict[str, list[Path]]:
    by_basename: dict[str, list[Path]] = defaultdict(list)
    for path in walk_md(root):
        by_basename[path.stem].append(path)
    return by_basename


def resolve_wikilink(target: str, source_path: Path, by_basename: dict[str, list[Path]],
                     vault_root: Path) -> Path | None:
    target_clean = target.strip()
    if not target_clean:
        return None
    if "/" in target_clean:
        rel = (source_path.parent / f"{target_clean}.md").resolve()
        if rel.exists():
            return rel
        absp = (vault_root / f"{target_clean}.md").resolve()
        if absp.exists():
            return absp
        return None
    matches = by_basename.get(target_clean, [])
    if matches:
        return matches[0]
    return None


def scan(root: Path, today: dt.date) -> dict:
    by_basename = file_basenames(root)
    broken: list[tuple[str, str]] = []
    inbound: dict[Path, set[Path]] = defaultdict(set)
    tag_counts: Counter = Counter()
    folder_counts: Counter = Counter()
    open_dated: list[tuple[dt.date, int, str, int, str]] = []

    for path in walk_md(root):
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == "Archive":
            # Still count Archive for orphan resolution, but don't include in
            # folder counts or radar.
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in WIKILINK_RE.finditer(text):
                target = m.group(1).strip()
                resolved = resolve_wikilink(target, path, by_basename, root)
                if resolved is not None:
                    inbound[resolved].add(path)
            continue

        folder = str(rel.parent) if str(rel.parent) != "." else "(root)"
        folder_counts[folder] += 1

        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            resolved = resolve_wikilink(target, path, by_basename, root)
            if resolved is None:
                broken.append((str(rel), target))
            else:
                inbound[resolved].add(path)

        for m in TAG_RE.finditer(text):
            tag_counts[m.group(1)] += 1

        if "Projects" in path.parts and path.name == "Tasks.md":
            for ln, line in enumerate(text.splitlines(), 1):
                m = CHECKBOX_LINE_RE.match(line)
                if not m:
                    continue
                body = m.group(1)
                d = first_date(body, today)
                if d is not None:
                    delta = (d - today).days
                    open_dated.append((d, delta, str(rel), ln, body.strip()))

    return {
        "by_basename": by_basename,
        "broken": broken,
        "inbound": inbound,
        "tag_counts": tag_counts,
        "folder_counts": folder_counts,
        "open_dated": open_dated,
    }


def orphans(root: Path, inbound: dict[Path, set[Path]]) -> list[str]:
    orphan_list: list[str] = []
    for path in walk_md(root):
        rel = path.relative_to(root)
        if not rel.parts or rel.parts[0] not in {"Notes", "Documents"}:
            continue
        if path.name in ALWAYS_CURRENT:
            continue
        # Skip _daily-data / _weekly-data captures — not meant to be linked.
        if any(p.startswith("_") for p in rel.parts):
            continue
        if not inbound.get(path):
            orphan_list.append(str(rel))
    return orphan_list


def stale(root: Path, today: dt.date, threshold_days: int = 60) -> list[tuple[dt.date, str]]:
    stale_list: list[tuple[dt.date, str]] = []
    threshold = today - dt.timedelta(days=threshold_days)
    for path in walk_md(root):
        rel = path.relative_to(root)
        if path.name in ALWAYS_CURRENT:
            continue
        if rel.parts and rel.parts[0] == "Archive":
            continue
        if any(p.startswith("_") for p in rel.parts):
            continue
        mtime = dt.date.fromtimestamp(path.stat().st_mtime)
        if mtime < threshold:
            stale_list.append((mtime, str(rel)))
    stale_list.sort()
    return stale_list


def tag_drift(tag_counts: Counter) -> list[list[str]]:
    by_lower: dict[str, list[str]] = defaultdict(list)
    for tag in tag_counts:
        by_lower[tag.lower()].append(tag)
    drift = [sorted(variants) for variants in by_lower.values() if len(variants) > 1]
    return drift


def previous_folder_counts() -> dict[str, int] | None:
    """Read folder counts from the most recent *-prep.md file, if any."""
    if not WEEKLY_DATA_DIR.exists():
        return None
    candidates = sorted(WEEKLY_DATA_DIR.glob("*-prep.md"))
    if not candidates:
        return None
    text = candidates[-1].read_text(encoding="utf-8", errors="ignore")
    counts: dict[str, int] = {}
    in_section = False
    for line in text.splitlines():
        if re.match(r"^##\s+File counts", line, re.IGNORECASE):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section:
            m = re.match(r"^\|\s*`?([^|`]+?)`?\s*\|\s*(\d+)\s*\|", line)
            if m:
                counts[m.group(1).strip()] = int(m.group(2))
    return counts or None


# ---------- rendering ----------

def render(today: dt.date, scan_result: dict) -> str:
    out: list[str] = []
    out.append(f"# Weekly prep — {today.isoformat()}\n")
    out.append("_Generated by `scripts/weekly-prep.py`. Mechanical inputs only; "
               "human judgment applies on top in the /weekly routine._\n")

    # Broken wikilinks
    out.append("## Broken wikilinks\n")
    if scan_result["broken"]:
        out.append("| Source | Broken target |")
        out.append("|---|---|")
        for src, tgt in scan_result["broken"]:
            out.append(f"| `{src}` | `{tgt}` |")
    else:
        out.append("_None._")
    out.append("")

    # Orphans
    out.append("## Orphan candidates (Notes/, Documents/ with 0 inbound links)\n")
    orph = orphans(VAULT, scan_result["inbound"])
    if orph:
        for p in orph:
            out.append(f"- `{p}`")
    else:
        out.append("_None._")
    out.append("")

    # Stale
    out.append("## Stale candidates (mtime > 60 days; excludes Archive, always-current, _*)\n")
    s = stale(VAULT, today)
    if s:
        for mtime, p in s:
            out.append(f"- `{p}` — last modified {mtime.isoformat()}")
    else:
        out.append("_None._")
    out.append("")

    # Tag usage
    out.append("## Tag usage\n")
    if scan_result["tag_counts"]:
        out.append("| Tag | Count |")
        out.append("|---|---:|")
        for tag, count in scan_result["tag_counts"].most_common():
            out.append(f"| `#{tag}` | {count} |")
    else:
        out.append("_No tags found._")
    drift = tag_drift(scan_result["tag_counts"])
    if drift:
        out.append("")
        out.append("**Case-drift / synonym candidates:**")
        for variants in drift:
            out.append(f"- {', '.join(f'`#{v}`' for v in variants)}")
    out.append("")

    # File counts
    out.append("## File counts by folder\n")
    prev = previous_folder_counts() or {}
    out.append("| Folder | Count | Δ vs last prep |")
    out.append("|---|---:|---:|")
    for folder, count in sorted(scan_result["folder_counts"].items()):
        prev_count = prev.get(folder)
        if prev_count is None:
            delta = "—"
        else:
            d = count - prev_count
            delta = f"{d:+d}" if d else "0"
        out.append(f"| `{folder}` | {count} | {delta} |")
    out.append("")

    # Deadline radar
    out.append("## Deadline radar — open dated tasks in Projects/*/Tasks.md\n")
    out.append("Buckets: past due → this week (0–7) → next 30 (8–30) → future (> 30).\n")
    buckets: dict[str, list] = {"past": [], "week": [], "month": [], "future": []}
    for date, delta, path, ln, body in scan_result["open_dated"]:
        if delta < 0:
            buckets["past"].append((date, delta, path, ln, body))
        elif delta <= 7:
            buckets["week"].append((date, delta, path, ln, body))
        elif delta <= 30:
            buckets["month"].append((date, delta, path, ln, body))
        else:
            buckets["future"].append((date, delta, path, ln, body))
    for name, label in [
        ("past", "Past due"),
        ("week", "This week (0–7 days)"),
        ("month", "Next 30 days (8–30)"),
        ("future", "Future (> 30 days)"),
    ]:
        out.append(f"### {label}\n")
        items = sorted(buckets[name])
        if not items:
            out.append("_None._")
        else:
            for date, delta, path, ln, body in items:
                # Truncate body to keep lines scannable.
                snippet = body if len(body) <= 140 else body[:137] + "…"
                out.append(f"- `{date.isoformat()}` ({delta:+d}d) — `{path}:{ln}` — {snippet}")
        out.append("")

    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-compute mechanical inputs for /weekly.")
    ap.add_argument("--out", help="Write report to this path instead of stdout.")
    ap.add_argument("--today", help="Override today's date (YYYY-MM-DD).")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    if not VAULT.is_dir():
        print(f"Vault directory not found: {VAULT}", file=sys.stderr)
        sys.exit(1)

    result = scan(VAULT, today)
    report = render(today, result)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
