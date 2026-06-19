#!/usr/bin/env python3
"""Regenerate the auto-memory MEMORY.md index and the vault-side Memory digest.md.

Walks every *.md in the memory directory (skipping MEMORY.md), parses YAML frontmatter
for name / description / type, and writes:

  1. <memory_dir>/MEMORY.md         — one-line index, grouped by type
  2. <vault>/Notes/Memory digest.md — human-readable digest, grouped by type

Invoked from /daily routine Step 8. Idempotent.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

MEMORY_DIR = Path.home() / ".claude/projects/-mnt-c-Users-damia-OneDrive-Documents-LLM-Land/memory"
VAULT = Path("/mnt/c/Users/damia/OneDrive/Documents/LLM_Land/AI Scratchpad")
DIGEST_PATH = VAULT / "Notes/Memory digest.md"

TYPE_ORDER = ["user", "feedback", "project", "reference"]
TYPE_LABELS = {
    "user": "User profile",
    "feedback": "Feedback / preferences",
    "project": "Project facts",
    "reference": "References",
}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Frontmatter is delimited by --- on its own lines."""
    if not text.startswith("---"):
        return {}, text
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not match:
        return {}, text
    fm_block, body = match.group(1), match.group(2)
    fm: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm, body


def load_entries(memory_dir: Path) -> list[dict]:
    entries = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        if not fm.get("name") or not fm.get("type"):
            print(f"warn: {path.name} missing name/type in frontmatter — skipped", file=sys.stderr)
            continue
        entries.append({
            "file": path.name,
            "name": fm.get("name", path.stem),
            "description": fm.get("description", ""),
            "type": fm.get("type", "reference").lower(),
        })
    return entries


def write_memory_index(entries: list[dict], memory_dir: Path) -> None:
    lines = ["# Memory index", ""]
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e["type"], []).append(e)
    # Flat index (kept compact — 1 line per entry, max 200 lines per CLAUDE.md guidance)
    for t in TYPE_ORDER + sorted(set(by_type) - set(TYPE_ORDER)):
        for e in by_type.get(t, []):
            lines.append(f"- [{e['name']}]({e['file']}) — {e['description']}")
    (memory_dir / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_digest(entries: list[dict], digest_path: Path) -> None:
    today = dt.date.today().isoformat()
    out = [
        "---",
        "title: Memory digest",
        "purpose: Human-readable mirror of what's in Claude's auto-memory at ~/.claude/.../memory/ — so the user can see and correct what's being remembered",
        "regenerated_by: \"[[Daily update routine]] (via scripts/regen-memory-digest.py)\"",
        f"last_refreshed: {today}",
        "---",
        "",
        "# Memory digest",
        "",
        "A scannable summary of what Claude has recorded in persistent auto-memory. The canonical files live at `~/.claude/projects/-mnt-c-Users-damia-OneDrive-Documents-LLM-Land/memory/`. This mirror is short blurbs only — refresh by the daily routine.",
        "",
        "**If anything below is wrong, outdated, or shouldn't be remembered, just say so** and Claude will update both this file and the source memory entry.",
        "",
        "---",
        "",
    ]
    by_type: dict[str, list[dict]] = {}
    for e in entries:
        by_type.setdefault(e["type"], []).append(e)
    for t in TYPE_ORDER + sorted(set(by_type) - set(TYPE_ORDER)):
        out.append(f"## {TYPE_LABELS.get(t, t.title())}")
        out.append("")
        bucket = by_type.get(t, [])
        if not bucket:
            out.append("*(none recorded yet)*")
        else:
            for e in bucket:
                out.append(f"- **{e['name']}** — {e['description']}")
        out.append("")
    digest_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    if not MEMORY_DIR.is_dir():
        print(f"error: memory dir not found: {MEMORY_DIR}", file=sys.stderr)
        return 1
    entries = load_entries(MEMORY_DIR)
    write_memory_index(entries, MEMORY_DIR)
    write_digest(entries, DIGEST_PATH)
    print(f"wrote {len(entries)} entries to MEMORY.md + Memory digest.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
