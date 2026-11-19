#!/usr/bin/env python3
"""Sweep old completed material out of live vault files into cumulative archive files.

Two kinds of source:
  - task files (every Projects/*/Tasks.md and CO Work Tasks.md): a top-level "- [x]" line
    plus its indented children moves when the newest date mentioned in the block is older
    than --days. Blocks with an open "- [ ]" child, or with no date at all, stay and are listed.
  - Daily update questions.md: a "### ..." entry outside "## Open" moves when the newest
    date in its heading is older than --days.

Moved text is appended to "Archive/<same folder>/<name> — completed.md" (or "— resolved.md")
under a "## Swept YYYY-MM-DD" heading, with relative links re-pointed for the new depth.

    python3 scripts/archive-done-lines.py            # dry run: counts only
    python3 scripts/archive-done-lines.py --write    # move the text
"""
import argparse
import datetime as dt
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

VAULT = Path(__file__).resolve().parent.parent / "AI Scratchpad"
QUESTIONS = "Daily update questions.md"
ISO = re.compile(r"(20\d\d)-(\d\d)-(\d\d)")
MD = re.compile(r"(?<![\d/.-])(\d{1,2})/(\d{1,2})(?![\d/])")
MD_LINK = re.compile(r"\]\((?!https?:|mailto:|#|/)([^)\s]+\.md)((?:#[^)]*)?)\)")
WIKI_REL = re.compile(r"\[\[(\.\./[^\]|#]+)")


def newest_date(text, today):
    """Newest past date mentioned in text, or None. M/D is read as this year, or last year if that is still ahead."""
    found = []
    for y, m, d in ISO.findall(text):
        try:
            found.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for m, d in MD.findall(ISO.sub("", text)):
        try:
            cand = dt.date(today.year, int(m), int(d))
        except ValueError:
            continue
        found.append(cand if cand <= today else cand.replace(year=today.year - 1))
    past = [x for x in found if x <= today]
    return max(past) if past else None


def relink(text, src_dir, dst_dir):
    """Re-point relative markdown links and ../ wikilinks from src_dir to dst_dir (both relative to the vault)."""
    def fix(rel):
        target = os.path.normpath(os.path.join(src_dir, unquote(rel)))
        return os.path.relpath(target, dst_dir).replace(os.sep, "/")
    text = MD_LINK.sub(lambda m: "](" + quote(fix(m.group(1)), safe="/.-_()") + m.group(2) + ")", text)
    return WIKI_REL.sub(lambda m: "[[" + fix(m.group(1)), text)


def split_tasks(lines, cutoff, today):
    """Return (kept, moved, skipped). moved is a list of (heading, block_lines)."""
    kept, moved, skipped, heading, i = [], [], [], "", 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            heading = line.strip()
        if not re.match(r"- \[[xX]\] ", line):
            kept.append(line)
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and lines[j][0] in " \t":
            j += 1
        block = lines[i:j]
        newest = newest_date("".join(block), today)
        if any(re.match(r"\s+- \[ \] ", b) for b in block[1:]):
            skipped.append(("open child", line))
            kept.extend(block)
        elif newest is None:
            skipped.append(("no date", line))
            kept.extend(block)
        elif newest < cutoff:
            moved.append((heading, block))
        else:
            kept.extend(block)
        i = j
    return kept, moved, skipped


def split_questions(lines, cutoff, today):
    kept, moved, section, i = [], [], "", 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            section = line.strip()
        if not line.startswith("### ") or section == "## Open":
            kept.append(line)
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].startswith(("## ", "### ")):
            j += 1
        newest = newest_date(line, today)
        if newest is not None and newest < cutoff:
            moved.append(("", lines[i:j]))
        else:
            kept.extend(lines[i:j])
        i = j
    return kept, moved, []


def sweep(src, days, write, today):
    rel = src.relative_to(VAULT)
    is_q = rel.name == QUESTIONS
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)
    kept, moved, skipped = (split_questions if is_q else split_tasks)(lines, today - dt.timedelta(days=days), today)
    assert len(kept) + sum(len(b) for _, b in moved) == len(lines), f"line count mismatch in {rel}"
    print(f"{rel}: move {len(moved)} blocks ({sum(len(b) for _, b in moved)} lines), keep {len(kept)} lines"
          + (f", skipped {len(skipped)}" if skipped else ""))
    for why, line in skipped:
        print(f"    stays ({why}): {line.strip()[:90]}")
    if not write or not moved:
        return
    dst = VAULT / "Archive" / rel.parent / f"{rel.stem} — {'resolved' if is_q else 'completed'}.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = [] if dst.exists() else [
        "---\n", f"title: {dst.stem}\n", f"source: {rel.as_posix()}\n",
        "purpose: Completed material swept out of the live file by scripts/archive-done-lines.py; newest sweep last\n",
        "---\n"]
    out.append(f"\n## Swept {today.isoformat()}\n")
    last = None
    for heading, block in moved:
        if heading and heading != last:
            out.append(f"\n### From: {heading.lstrip('# ')}\n\n")
            last = heading
        out.extend(relink(b, str(rel.parent), str(dst.relative_to(VAULT).parent)) for b in block)
    with dst.open("a", encoding="utf-8") as fh:  # archive first, so a failure never loses text
        fh.writelines(out)
    src.write_text("".join(kept), encoding="utf-8")
    print(f"    appended to {dst.relative_to(VAULT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="minimum age of the newest date in a block (default 30)")
    ap.add_argument("--write", action="store_true", help="move the text (default is a dry run)")
    args = ap.parse_args()
    today = dt.date.today()
    assert newest_date("done 2026-10-24, ship 12/31/x 99/99", dt.date(2026, 11, 18)) == dt.date(2026, 10, 24)
    assert relink("[t](Tasks.md) [[../../Documents/A|a]]", "Projects/P", "Archive/Projects/P") == \
        "[t](../../../Projects/P/Tasks.md) [[../../../Documents/A|a]]"
    sources = sorted(VAULT.glob("Projects/*/Tasks.md")) + sorted(VAULT.glob("Projects/*/CO Work Tasks.md"))
    for src in sources + [VAULT / QUESTIONS]:
        sweep(src, args.days, args.write, today)
    if not args.write:
        print("dry run: nothing changed. Re-run with --write to move the text.")


if __name__ == "__main__":
    main()
