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
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

VAULT = Path(os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad"))
WEEKLY_DATA_DIR = VAULT / "Notes" / "_weekly-data"

ALWAYS_CURRENT = {
    "My Day.md",
    "Tasks.md",
    "Inbox.md",
    "Index.md",
    "Memory digest.md",
    "MEMORY.md",
    "Daily update questions.md",
}

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^|\]]*)?(?:\|[^\]]*)?\]\]")
# Markdown-style local links ([text](Some%20File.md) or .csv) — counted as
# inbound for orphan detection (#22: path-qualified/markdown links previously
# invisible, false-flagging linked docs as orphans).
MDLINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s#][^)\s]*\.(?:md|csv))\)")
TAG_RE = re.compile(r"(?<![\w/])#([A-Za-z][\w/-]*)")
CHECKBOX_LINE_RE = re.compile(r"^\s*-\s*\[\s*\]\s*(.+)$")

# ---------- #33 tag-census span masking (built 2026-08-04) ----------

# Broad wikilink-span matcher for masking purposes ONLY — deliberately not
# WIKILINK_RE, which requires >=1 non-"#" char in the target ([^\]|#]+?) and
# so never matches same-file heading anchors like [[#Tools / gear]]. That gap
# is exactly why those anchors are invisible to the link scan yet visible to
# the tag scan: [[#Tools / gear]] leaves a bare #Tools in the text for TAG_RE
# to pick up. This regex matches any [[...]] span regardless of contents.
WIKILINK_SPAN_RE = re.compile(r"\[\[[^\[\]]+\]\]")
# Inline code spans — single line only, so an unmatched/stray backtick can't
# swallow the rest of the file the way a multiline regex could.
CODE_SPAN_RE = re.compile(r"`[^`\n]+`")
FENCE_LINE_RE = re.compile(r"^\s*(```|~~~)")


def _mask_fenced_code(text: str) -> str:
    """Blank the interior of fenced code blocks via a line-based fence
    toggle, not a greedy re.DOTALL regex — a stray unclosed fence would
    otherwise silently swallow the rest of the file."""
    lines = text.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            lines[i] = " "
    return "\n".join(lines)


def mask_census_spans(text: str) -> str:
    """Build a census-only masked copy of `text` for the tag scan: blank
    fenced code blocks, then wikilink spans, then inline code spans, so
    #token-shaped text inside them isn't counted as a live tag (#33).

    Spans are replaced with a single space, never "" — empty-string removal
    can butt adjacent characters together and change what TAG_RE's
    `(?<![\\w/])` lookbehind sees for a real tag right after a masked span
    (e.g. `[[X]] #real-tag` must still count `#real-tag`).

    Callers must NOT mask `text` itself — only this returned copy. The
    wikilink/inbound-link scan runs on the original `text` and needs the
    unmasked links intact.
    """
    masked = _mask_fenced_code(text)
    masked = WIKILINK_SPAN_RE.sub(" ", masked)
    masked = CODE_SPAN_RE.sub(" ", masked)
    return masked

# ---------- #22 broken-link false-positive filters (built 2026-07-06) ----------

# Memory-slug wikilinks intentionally point outside the vault (auto-memory dir).
# Derived from cwd (Claude Code project-key munging); override with LIFE_MEMORY_DIR.
MEMORY_DIR = (
    Path(os.environ["LIFE_MEMORY_DIR"])
    if os.environ.get("LIFE_MEMORY_DIR")
    else Path.home()
    / ".claude"
    / "projects"
    / re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd().resolve()))
    / "memory"
)

# Literal example tokens used in routine docs to *illustrate* wikilink syntax.
EXAMPLE_TOKENS = {
    "wikilinks", "wikilink", "wikilink]]s", "path", "memory-slug", "Foo",
    "name", "their-name", "feedback_*",
}

# Frozen snapshot dirs — their links describe a past state; rot there is not
# actionable and floods the report.
SNAPSHOT_DIRS = ("_daily-data", "_weekly-data")

# ---------- #25 tag-census meta-doc filter (built 2026-07-26) ----------

# Routine docs list tags as examples of drift to watch for; frozen snapshots
# quote old tag tables; Automation backlog.md talks about tags. None of these
# reflect live tag usage — counting them false-flags case-drift (e.g. #Cmd vs
# #cmd) that only ever existed in prose, never in a live task file.


def is_tag_census_meta_doc(rel: Path) -> bool:
    """True when `rel` (vault-relative) should be excluded from the tag census."""
    if any(part in SNAPSHOT_DIRS for part in rel.parts):
        return True
    if "routine" in rel.name.lower():
        return True
    if rel.name == "Automation backlog.md":
        return True
    return False


# ---------- #29 deadline-radar resolved/struck suppression (built 2026-07-27) ----------

# The past-due bucket kept re-listing "zombie" lines whose own text already says
# they're resolved (struck-through tasks, NOT NEEDED / moot / handed-off notes),
# burying the real past-due signal. Radar-only: the link/tag/orphan scans still
# see these lines. Suppressed count is shown in the radar section (#22/#25 style).
RADAR_RESOLVED_MARKERS = (
    "not needed",
    "no longer needed",
    "moot",
    "dropped",
    "superseded",
    "handed to",
    "handed off",
    "consolidated",
    "track there",
)
RADAR_STRUCK_RE = re.compile(r"^\s*~~")


def is_radar_resolved(body: str) -> bool:
    """True when an open dated task line is resolved-in-text and should be
    suppressed from the deadline radar."""
    if RADAR_STRUCK_RE.match(body):
        return True
    lowered = body.lower()
    return any(marker in lowered for marker in RADAR_RESOLVED_MARKERS)


def memory_slugs() -> set[str]:
    """File stems of the auto-memory dir, hyphen-normalized (memory files use
    underscore filenames but hyphenated `name:` slugs — match either form)."""
    if not MEMORY_DIR.is_dir():
        return set()
    return {p.stem.replace("_", "-") for p in MEMORY_DIR.glob("*.md")}


def is_false_positive(target: str, source_rel: Path, slugs: set[str]) -> bool:
    """True when a broken-looking wikilink is a known non-actionable pattern."""
    if any(part in SNAPSHOT_DIRS for part in source_rel.parts):
        return True  # frozen snapshot source
    t = target.strip()
    if "..." in t or "*" in t:
        return True  # elided/glob illustration
    if "/home/" in t or t.startswith("~"):
        return True  # external filesystem path
    base = t.rsplit("/", 1)[-1]
    if t in EXAMPLE_TOKENS or base in EXAMPLE_TOKENS:
        return True  # doc example, not a real link
    if base.replace("_", "-") in slugs:
        return True  # auto-memory cross-reference (lives outside the vault)
    return False

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


HEADLINE_RE = re.compile(r"^\s*\*\*(.+?)\*\*")


def deadline_scope(body: str) -> str:
    """The part of a task line where a DEADLINE may legitimately live: the bolded headline.

    Vault convention is `- [ ] **<task, with its deadline>** — <annotations, updates, Q-answer
    history…>`, and those annotations are dense with dates that are NOT deadlines: capture
    dates, "re-anchored 8/12", "Q47 answered 2026-08-08", prior targets. Scanning the whole
    line made every one of them a candidate, so long-lived tasks drifted to whichever date
    happened to match first — four MMC lines showed as PAST DUE on 2026-08-13 purely off
    annotation stamps, none of which was a real deadline (found on the radar's second live
    run; automation #40).

    Returns the headline when one exists, else the whole body — so lines that don't follow
    the convention keep the old behaviour rather than silently losing their date."""
    m = HEADLINE_RE.match(body)
    return m.group(1) if m else body


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


# ---------- #24 vague-date detector (built 2026-07-07) ----------
# Targets written month-only / season / fuzzy never parse on the deadline
# radar (precedent: the MMC "2026-07" renewal milestone went unsurfaced for
# two months). Detect them on open task lines + headings in Projects/*/Tasks.md
# so the /weekly anchoring pass (routine Step 7) has a worklist.

_MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)

VAGUE_DATE_RES = [
    # ISO month-only: "2026-07" not followed by -DD
    ("month-only ISO", re.compile(r"\b20\d{2}-(?:0[1-9]|1[0-2])\b(?!-\d)")),
    # Month name + year with NO day number attached: "July 2026", "Feb 2027"
    ("month + year, no day", re.compile(
        rf"(?<!\d )\b(?:{_MONTHS})\.?\s+20\d{{2}}\b", re.IGNORECASE)),
    # Season + year: "Fall 2026"
    ("season", re.compile(r"\b(?:Spring|Summer|Fall|Autumn|Winter)\s+20\d{2}\b", re.IGNORECASE)),
    # Fuzzy qualifiers: "~late July", "early Aug"
    ("fuzzy (early/mid/late)", re.compile(
        rf"\b(?:early|mid|late)[\s-]+(?:{_MONTHS})\b", re.IGNORECASE)),
    # Month ranges: "Aug–Sep" / "Aug-Sep"
    ("month range", re.compile(rf"\b(?:{_MONTHS})\s*[–—-]\s*(?:{_MONTHS})\b")),
]

# A day-anchored date on the same line means the radar already sees it.
DAY_ANCHORED_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|(?<![\w/])\d{{1,2}}/\d{{1,2}}(?![/\d])|"
    rf"\b(?:{_MONTHS})\s+\d{{1,2}}\b|\b\d{{1,2}}\s+(?:{_MONTHS})\b",
    re.IGNORECASE,
)

STRUCK_RE = re.compile(r"~~.*~~")
DONE_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[xX]\]")
HEADING_RE = re.compile(r"^#{1,6}\s")


_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def vague_dates_in(text: str, rel: str, today: dt.date) -> list[tuple[str, int, str, str]]:
    """Flag open-task and heading lines carrying only vague date wording."""
    found: list[tuple[str, int, str, str]] = []
    for ln, line in enumerate(text.splitlines(), 1):
        is_open_task = CHECKBOX_LINE_RE.match(line) is not None
        is_heading = HEADING_RE.match(line) is not None
        if not (is_open_task or is_heading):
            continue
        if DONE_CHECKBOX_RE.match(line) or STRUCK_RE.search(line):
            continue
        if DAY_ANCHORED_RE.search(line):
            continue  # radar already parses this line
        for kind, rx in VAGUE_DATE_RES:
            m = rx.search(line)
            if m:
                # Past-year mention (e.g. "Nov 2021" course history) is a
                # reference, not a target — skip.
                ym = _YEAR_RE.search(m.group(0))
                if ym and int(ym.group(1)) < today.year:
                    continue
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "…"
                found.append((rel, ln, kind, snippet))
                break  # one flag per line
    return found


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
    # #22: extension-bearing targets (.csv etc.) resolve as-is, no .md appended.
    suffixes = ["", ".md"] if "." not in target_clean.rsplit("/", 1)[-1] else [""]
    if "/" in target_clean:
        for suffix in suffixes:
            rel = (source_path.parent / f"{target_clean}{suffix}").resolve()
            if rel.exists():
                return rel
            absp = (vault_root / f"{target_clean}{suffix}").resolve()
            if absp.exists():
                return absp
        return None
    if "." in target_clean:  # bare filename with extension, e.g. [[file.csv]]
        stem = target_clean.rsplit(".", 1)[0]
        matches = by_basename.get(stem, [])
        if matches:
            return matches[0]
    matches = by_basename.get(target_clean, [])
    if matches:
        return matches[0]
    return None


def _mdlink_inbound(text: str, path: Path, inbound: dict[Path, set[Path]]) -> None:
    """#22: credit markdown-style local links ([x](Some%20File.md)) as inbound."""
    from urllib.parse import unquote
    for m in MDLINK_RE.finditer(text):
        href = unquote(m.group(1))
        if href.startswith(("http://", "https://")):
            continue
        resolved = (path.parent / href).resolve()
        if resolved.exists():
            inbound[resolved].add(path)


def scan(root: Path, today: dt.date) -> dict:
    by_basename = file_basenames(root)
    slugs = memory_slugs()
    broken: list[tuple[str, str]] = []
    filtered_count = 0
    inbound: dict[Path, set[Path]] = defaultdict(set)
    tag_counts: Counter = Counter()
    tag_census_skipped = 0
    tag_census_suppressed = 0
    folder_counts: Counter = Counter()
    open_dated: list[tuple[dt.date, int, str, int, str]] = []
    radar_suppressed = 0
    vague_dates: list[tuple[str, int, str, str]] = []

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
            _mdlink_inbound(text, path, inbound)
            continue

        folder = str(rel.parent) if str(rel.parent) != "." else "(root)"
        folder_counts[folder] += 1

        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            resolved = resolve_wikilink(target, path, by_basename, root)
            if resolved is None:
                if is_false_positive(target, rel, slugs):
                    filtered_count += 1
                else:
                    broken.append((str(rel), target))
            else:
                inbound[resolved].add(path)
        _mdlink_inbound(text, path, inbound)

        if is_tag_census_meta_doc(rel):
            tag_census_skipped += 1
        else:
            # #33: census runs on a masked COPY (wikilink spans, inline code
            # spans, fenced code blocks blanked) — `text` itself stays
            # unmasked for the wikilink/inbound-link scan above.
            census_text = mask_census_spans(text)
            for m in TAG_RE.finditer(census_text):
                tag_counts[m.group(1)] += 1
            tag_census_suppressed += (
                len(TAG_RE.findall(text)) - len(TAG_RE.findall(census_text))
            )

        if "Projects" in path.parts and path.name == "Tasks.md":
            vague_dates.extend(vague_dates_in(text, str(rel), today))
            for ln, line in enumerate(text.splitlines(), 1):
                m = CHECKBOX_LINE_RE.match(line)
                if not m:
                    continue
                body = m.group(1)
                d = first_date(deadline_scope(body), today)
                if d is not None:
                    if is_radar_resolved(body):
                        radar_suppressed += 1
                        continue
                    delta = (d - today).days
                    open_dated.append((d, delta, str(rel), ln, body.strip()))

    return {
        "by_basename": by_basename,
        "broken": broken,
        "filtered_count": filtered_count,
        "inbound": inbound,
        "tag_counts": tag_counts,
        "tag_census_skipped": tag_census_skipped,
        "tag_census_suppressed": tag_census_suppressed,
        "folder_counts": folder_counts,
        "open_dated": open_dated,
        "radar_suppressed": radar_suppressed,
        "vague_dates": vague_dates,
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
        # #22: inbound keys mix resolved-absolute (path-qualified/mdlink) and
        # walk-relative (basename) paths — check both forms.
        if not inbound.get(path) and not inbound.get(path.resolve()):
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
    if scan_result.get("filtered_count"):
        out.append("")
        out.append(f"_(#22 filter suppressed {scan_result['filtered_count']} known "
                   "false positives: memory slugs, doc examples, snapshot-dir links, "
                   "external paths.)_")
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
    if scan_result.get("tag_census_skipped"):
        out.append("")
        out.append(f"_(tag census skipped {scan_result['tag_census_skipped']} meta-doc files)_")
    if scan_result.get("tag_census_suppressed"):
        out.append("")
        out.append(f"_(tag census: suppressed {scan_result['tag_census_suppressed']} "
                   "tag-shaped tokens inside wikilinks/code spans)_")
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

    # Vague dates needing anchors (#24)
    out.append("## Vague dates needing anchors (radar-invisible — Step 7 anchoring pass)\n")
    vd = scan_result.get("vague_dates", [])
    if vd:
        out.append("| Source | Kind | Line |")
        out.append("|---|---|---|")
        for rel, ln, kind, snippet in vd:
            out.append(f"| `{rel}:{ln}` | {kind} | {snippet} |")
    else:
        out.append("_None — all dated targets are day-anchored._")
    out.append("")

    # Deadline radar
    out.append("## Deadline radar — open dated tasks in Projects/*/Tasks.md\n")
    out.append("Buckets: past due → this week (0–7) → next 30 (8–30) → future (> 30).\n")
    if scan_result.get("radar_suppressed"):
        out.append(f"_(#29 filter suppressed {scan_result['radar_suppressed']} "
                   "resolved/struck lines.)_\n")
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


def render_radar_only(today: dt.date, scan_result: dict, horizon: int) -> str:
    """Just the dated-task radar out to `horizon` days, for the /daily Step-5 sweep.

    Automation #40 (built 2026-08-12). The /daily radar was agent judgment over the project
    files, and that missed a dated item for FOUR consecutive runs — the HOA-fee verification
    due 8/10, captured into a NEWLY created project folder (Personal Admin, 8/04) that was
    never in the agent's habitual sweep set. Discovery is mechanical and globs
    `Projects/*/Tasks.md`, so a new project is covered the day it exists; only bucketing and
    prioritisation need judgment, and those stay with the agent."""
    rows = [r for r in scan_result["open_dated"] if r[1] <= horizon]
    out = [f"# Deadline radar — next {horizon} days (as of {today.isoformat()})", ""]
    if scan_result.get("radar_suppressed"):
        out.append(f"_(#29 filter suppressed {scan_result['radar_suppressed']} "
                   "resolved/struck lines.)_")
        out.append("")
    buckets: dict[str, list] = {"past": [], "today": [], "soon": []}
    for row in rows:
        delta = row[1]
        buckets["past" if delta < 0 else "today" if delta == 0 else "soon"].append(row)
    for key, label in [("past", "⚠️ PAST DUE"), ("today", "🔴 TODAY"),
                       (" soon".strip(), f"Next {horizon} days")]:
        items = sorted(buckets[key])
        out.append(f"## {label} — {len(items)}")
        if not items:
            out.append("_None._")
        else:
            for date, delta, path, ln, body in items:
                snippet = body if len(body) <= 160 else body[:157] + "…"
                out.append(f"- `{date.isoformat()}` ({delta:+d}d) — `{path}:{ln}` — {snippet}")
        out.append("")
    out.append(f"_{len(rows)} dated task lines within the horizon. "
               "Bucketing into Pinned / Surfacing / Defer is the agent's judgment call; "
               "this list is discovery only._")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-compute mechanical inputs for /weekly.")
    ap.add_argument("--out", help="Write report to this path instead of stdout.")
    ap.add_argument("--today", help="Override today's date (YYYY-MM-DD).")
    ap.add_argument("--radar-only", action="store_true",
                    help="Print ONLY the dated-task deadline radar (for the /daily Step-5 sweep).")
    ap.add_argument("--horizon", type=int, default=7,
                    help="Days ahead to include with --radar-only (default 7).")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    if not VAULT.is_dir():
        print(f"Vault directory not found: {VAULT}", file=sys.stderr)
        sys.exit(1)

    result = scan(VAULT, today)
    report = (render_radar_only(today, result, args.horizon) if args.radar_only
              else render(today, result))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Wrote {out_path}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
