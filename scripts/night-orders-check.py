#!/usr/bin/env python3
"""Night-orders fact de-duplication guard.

Built 2026-08-05 after a repeat slipped through: a St. Matthew polar-bear fact was
generated and placed even though the user had already used it. Root cause was twofold —
(a) the generator checked the *category* rotation but never scanned fact *content*, and
(b) the registry only ever recorded facts this system generated, so anything the user
placed himself (his own substitutions) was invisible to it.

Two modes:

  --list                 Compact index of every fact on record: date, category, and a
                         short subject line. Read this BEFORE generating.
  --check "text..."      Score a candidate against everything on record and print the
                         closest matches. Exits 2 if anything looks like a repeat.

Sources scanned (both, always):
  ## Log                              — facts this system generated
  ## Already used (not generated here) — facts the user placed himself; add rows freely

House style: degrades to one note line and exit 0 rather than raising; --json for
chaining; stdout is read by an LLM mid-routine, so lines must be greppable.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

VAULT = os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad")
REGISTRY = Path(VAULT) / "Notes" / "_daily-data" / "night-orders-facts.md"

# Words that carry no subject signal — ignored when comparing candidates to history.
STOP = set("""
a an the and or but if then than that this these those there here it its it's is are was
were be been being am do does did doing have has had having will would shall should can
could may might must of in on at to from by for with without within into onto over under
about above below up down out off again further once all any both each few more most
other some such no nor not only own same so too very s t just don now we our us you your
they them their he she his her i me my one two three first second new old also as when
where which who whom what how why while during before after because between through
during still yet even much many get got make made take taken go goes going come comes
tonight today tomorrow yesterday night day fact
""".split())

TOKEN_RE = re.compile(r"[a-z0-9']+")


def note(msg):
    print(msg)


def tokens(text):
    text = re.sub(r"\*+|_+|`+", " ", text.lower())
    return {w for w in TOKEN_RE.findall(text) if len(w) > 2 and w not in STOP}


def split_cells(line):
    parts = re.split(r"(?<!\\)\|", line)
    return [c.strip() for c in parts[1:-1]] if len(parts) > 2 else []


def load_rows(text):
    """Return [{date, category, fact, source}] from both the Log and Already-used tables."""
    rows = []
    for header, source in (("## Log", "log"),
                           ("## Already used", "already-used")):
        m = re.search(rf"^{re.escape(header)}[^\n]*\n(.*?)(?=^## |\Z)",
                      text, re.MULTILINE | re.DOTALL)
        if not m:
            continue
        for line in m.group(1).split("\n"):
            if not line.lstrip().startswith("|"):
                continue
            cells = split_cells(line)
            if len(cells) < 3:
                continue
            date, category, fact = cells[0], cells[1], cells[2]
            if not re.match(r"\d{4}-\d{2}-\d{2}", date) and date.lower() != "unknown":
                continue  # header/separator row
            rows.append({"date": date, "category": category,
                         "fact": fact, "source": source})
    return rows


def subject(fact, width=110):
    """First clause of the fact, stripped of markdown — enough to recognise it."""
    s = re.sub(r"\*+|_+|`+", "", fact)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:width] + ("…" if len(s) > width else "")


def score(cand_tokens, row_tokens):
    if not cand_tokens or not row_tokens:
        return 0.0
    overlap = cand_tokens & row_tokens
    # Overlap relative to the smaller set — catches a short candidate hiding inside a
    # long logged fact, which is exactly how the polar-bear repeat got through.
    return len(overlap) / min(len(cand_tokens), len(row_tokens))


def main():
    ap = argparse.ArgumentParser(description="Night-orders fact de-duplication guard")
    ap.add_argument("--list", action="store_true", help="print the full fact index")
    ap.add_argument("--check", metavar="TEXT", help="score a candidate fact against history")
    ap.add_argument("--registry", default=str(REGISTRY), help="path to night-orders-facts.md")
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="overlap fraction at which a candidate is called a likely repeat (default 0.30)")
    ap.add_argument("--top", type=int, default=5, help="how many nearest matches to show (default 5)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    path = Path(args.registry)
    if not path.exists():
        note(f"NIGHT-ORDERS CHECK: registry not found at {path} — cannot verify; skipped")
        return 0

    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        note(f"NIGHT-ORDERS CHECK: could not read registry ({exc}) — cannot verify; skipped")
        return 0

    rows = load_rows(text)
    if not rows:
        note("NIGHT-ORDERS CHECK: no fact rows parsed from the registry — cannot verify; skipped")
        return 0

    if args.list or not args.check:
        if args.json:
            print(json.dumps({"count": len(rows), "facts": [
                {"date": r["date"], "category": r["category"],
                 "subject": subject(r["fact"]), "source": r["source"]} for r in rows]}, indent=2))
            return 0
        note(f"NIGHT-ORDERS CHECK: {len(rows)} facts on record "
             f"({sum(1 for r in rows if r['source'] == 'log')} generated, "
             f"{sum(1 for r in rows if r['source'] == 'already-used')} used-elsewhere)")
        for r in sorted(rows, key=lambda x: x["date"]):
            tag = "" if r["source"] == "log" else "  [used elsewhere]"
            print(f"  {r['date']}  {r['category']:<18}  {subject(r['fact'], 96)}{tag}")
        return 0

    cand = tokens(args.check)
    scored = sorted(
        ({**r, "score": score(cand, tokens(r["fact"]))} for r in rows),
        key=lambda x: x["score"], reverse=True)
    top = scored[: args.top]
    worst = top[0]["score"] if top else 0.0
    repeat = worst >= args.threshold

    if args.json:
        print(json.dumps({
            "candidate_tokens": sorted(cand),
            "likely_repeat": repeat,
            "threshold": args.threshold,
            "matches": [{"date": r["date"], "category": r["category"],
                         "score": round(r["score"], 3), "source": r["source"],
                         "subject": subject(r["fact"])} for r in top],
        }, indent=2))
        return 2 if repeat else 0

    if repeat:
        note(f"NIGHT-ORDERS CHECK: ⚠️ LIKELY REPEAT — {worst:.0%} subject overlap with an existing fact. Pick something else.")
    else:
        note(f"NIGHT-ORDERS CHECK: ✅ no repeat detected — closest match {worst:.0%} (threshold {args.threshold:.0%}).")
    for r in top:
        tag = "" if r["source"] == "log" else " [used elsewhere]"
        print(f"  {r['score']:.0%}  {r['date']}  {r['category']}{tag}  {subject(r['fact'], 88)}")
    return 2 if repeat else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        note(f"NIGHT-ORDERS CHECK: unexpected error ({exc}) — cannot verify; skipped")
        sys.exit(0)
