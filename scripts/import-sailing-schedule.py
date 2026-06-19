#!/usr/bin/env python3
"""Import the FY26 NOAA Ship Fairweather sailing schedule (.ods) into a vault markdown doc.

Auto-selects the latest FY26 sheet (highest "FY26 V#.#" version) unless --sheet is given,
parses the DEP/ARR/alongside rows into clean legs, and regenerates the schedule doc:

    AI Scratchpad/Projects/Fairweather Command/FY26 Sailing Schedule.md

The doc is a mechanical mirror of the ODS — re-run it whenever a new schedule version drops.
Live operational color (current position, ETA, transit notes) lives in the project Overview,
NOT here, so regenerating never clobbers hand-kept context.

Usage:
    python3 scripts/import-sailing-schedule.py                 # latest FY26 sheet → write doc
    python3 scripts/import-sailing-schedule.py --list           # list sheets + versions
    python3 scripts/import-sailing-schedule.py --sheet "FY26 V4.1 - 145 DAS PARS Moorings"
    python3 scripts/import-sailing-schedule.py --dry-run        # print doc to stdout, don't write
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ODS = Path(
    "/mnt/c/Users/damia/OneDrive/Documents/NOAA Corps/FA CO/FY26 FA DRAFT Sailing Schedule.ods"
)
DEFAULT_OUT = ROOT / "AI Scratchpad/Projects/Fairweather Command/FY26 Sailing Schedule.md"
DEPLOY_CUTOFF = dt.date(2026, 5, 1)  # legs on/after this go in the deployment-season table

# Columns in the schedule grid (0-indexed).
C_TYPE, C_DATE, C_DAY, C_PORT, C_PROJ, C_LO, C_NOTES, C_DAS, C_DIP, C_CUM = range(10)


def cell(row, idx):
    """Safe string fetch from a pandas row (positional), '' for NaN/missing."""
    if idx >= len(row):
        return ""
    v = row.iloc[idx]
    if pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse_date(v):
    if isinstance(v, (pd.Timestamp, dt.datetime, dt.date)):
        return pd.Timestamp(v).date()
    if isinstance(v, str) and v.strip():
        try:
            return pd.Timestamp(v.strip()).date()
        except Exception:
            return None
    return None


def pick_sheet(sheet_names, override):
    if override:
        if override not in sheet_names:
            sys.exit(f"sheet not found: {override!r}\navailable:\n  " + "\n  ".join(sheet_names))
        return override
    candidates = []
    for n in sheet_names:
        m = re.search(r"FY26\s+V(\d+(?:\.\d+)?)", n)
        if m:
            candidates.append((float(m.group(1)), n))
    if not candidates:
        sys.exit(f"no 'FY26 V#' sheet found. Sheets:\n  " + "\n  ".join(sheet_names))
    candidates.sort()
    return candidates[-1][1]


def parse_schedule(df):
    """Return (legs, meta). Each leg: dict(start,end,start_port,end_port,project,lo,das,dip,typ)."""
    rows = [df.iloc[i] for i in range(len(df))]

    meta = {"authorized_das": "", "updated": "", "total_sched": "", "total_dip": ""}
    for r in rows:
        joined = " ".join(cell(r, i) for i in range(min(len(r), 12)))
        if "Authorized DAS from FAP" in joined and not meta["authorized_das"]:
            meta["authorized_das"] = cell(r, C_TYPE)  # number sits in col0
            md = re.search(r"\d{1,2}-[A-Za-z]{3}-\d{2}", joined)  # "22-Apr-26"
            if md:
                meta["updated"] = md.group(0)
            else:  # pandas may have parsed the date cell to a Timestamp (seen in col ~7)
                for ci in range(2, min(len(r), 10)):
                    d = parse_date(r.iloc[ci])
                    if d:
                        meta["updated"] = d.strftime("%d-%b-%y")
                        break
        m = re.search(r"Total DAS Scheduled\D+(\d+)", joined)  # value right after label
        if m:
            meta["total_sched"] = m.group(1)
        m = re.search(r"Total DIP\D+(\d+)", joined)
        if m:
            meta["total_dip"] = m.group(1)

    # Collect every row that carries a real date in the DATE column.
    events = []
    for r in rows:
        d = parse_date(r.iloc[C_DATE] if C_DATE < len(r) else None)
        if d is None:
            continue
        events.append(
            {
                "date": d,
                "typ": cell(r, C_TYPE).rstrip(":"),
                "port": cell(r, C_PORT),
                "proj": cell(r, C_PROJ),
                "lo": cell(r, C_LO),
                "notes": cell(r, C_NOTES),
                "das": cell(r, C_DAS),
                "dip": cell(r, C_DIP),
            }
        )

    # A leg starts on any event row that names a project; it ends on the next dated row.
    legs = []
    for i, ev in enumerate(events):
        if not ev["proj"]:
            continue
        end = events[i + 1] if i + 1 < len(events) else ev
        legs.append(
            {
                "start": ev["date"],
                "end": end["date"],
                "start_port": ev["port"],
                "end_port": end["port"] or ev["port"],
                "project": ev["proj"],
                "lo": ev["lo"],
                "das": ev["das"],
                "dip": ev["dip"],
                "typ": ev["typ"],
                "notes": ev["notes"],
            }
        )
    return legs, meta


def fmt_d(d):
    return d.strftime("%-m/%d")


def leg_row(leg):
    span = fmt_d(leg["start"])
    if leg["end"] != leg["start"]:
        span += f" → {fmt_d(leg['end'])}"
    ports = leg["start_port"]
    if leg["end_port"] and leg["end_port"] != leg["start_port"]:
        ports += f" → {leg['end_port']}"
    days = ""
    if leg["das"] and leg["das"] not in ("0",):
        days = f"{leg['das']} DAS"
    elif leg["dip"] and leg["dip"] not in ("0",):
        days = f"{leg['dip']} DIP"
    proj = leg["project"]
    if leg["notes"]:
        proj += f" — {leg['notes']}"
    return f"| {span} | {ports} | {days} | {proj} | {leg['lo']} |"


LEGEND = """## Key / abbreviations

- **DAS** — Days at Sea · **DIP** — Days in Port · **CUM DAS** — cumulative DAS
- **LO** — Line Office sponsoring the leg: **NOS** (National Ocean Service — hydro/survey science) · **OMAO** (maintenance, readiness, transit, crew recovery) · NMFS (fisheries)
- **OPR-####-FA-26** — Office of Coast Survey hydrographic project numbers (FA = *Fairweather*, 26 = FY26)
- **PARS** — Arctic project leg (paired with "Arctic Ecosystems Moorings" on the final leg) *(expand/confirm)*
- **ORT** — Operational Readiness Test/Trial · **HSRR** — Hydrographic Systems Readiness Review *(confirm)* · **MOPED / IPOP / PIP / PoP** — dockside repair / maintenance & engineering periods *(abbrev — confirm against MOC usage)*
- **Project areas:** Revillagigedo (SE AK), Northeast Kodiak, St. Matthew Island (Bering Sea), Arctic Ecosystems"""

NOTES = """## Notes

- **CoC date discrepancy:** schedule shows Change of Command 5/25; actual CoC was **2026-05-22** (Kodiak). Schedule is a draft; the [[Overview]] and command record reflect the actual date.
- **Live operational status** (current position, ETA, transit notes — e.g. Unimak Pass transit, St. Matthew ETA) lives in [[Overview]] → Schedule anchors, not here. This doc is a mechanical mirror of the ODS.
- Re-import when a newer schedule version is issued: `python3 scripts/import-sailing-schedule.py`."""


def build_doc(legs, meta, sheet_name, ods_path):
    today = dt.date.today()
    version_m = re.search(r"FY26\s+V(\d+(?:\.\d+)?)", sheet_name)
    version = version_m.group(1) if version_m else "?"

    current = next((l for l in legs if l["start"] <= today <= l["end"] and l["project"]), None)
    cur_line = "_No leg matches today's date (between legs or schedule not current)._"
    if current:
        ports = current["start_port"]
        if current["end_port"] != current["start_port"]:
            ports += f" → {current['end_port']}"
        cur_line = (
            f"**{current['project']}** ({ports}), "
            f"{fmt_d(current['start'])} → {fmt_d(current['end'])}. "
            f"Live position/ETA: see [[Overview]]."
        )

    deploy = [l for l in legs if l["start"] >= DEPLOY_CUTOFF]
    pre = [l for l in legs if l["start"] < DEPLOY_CUTOFF]

    # Inport quick list (alongside legs in deployment season with a DIP value).
    inports = [
        f"{l['start_port'].split(',')[0]} {fmt_d(l['start'])}–{fmt_d(l['end'])}"
        for l in deploy
        if l["dip"] and l["dip"] != "0" and "Recovery" in l["project"]
    ]

    head = (
        "| Dates | Ports | Days | Project / Area | LO |\n"
        "|---|---|---|---|---|\n"
    )
    deploy_tbl = head + "\n".join(leg_row(l) for l in deploy)
    pre_tbl = head + "\n".join(leg_row(l) for l in pre)

    return f"""---
project: Fairweather Command
type: reference / planning
source: "{ods_path.name}" → sheet "{sheet_name}"
schedule_version: FY26 V{version} (DRAFT)
authorized_das: {meta['authorized_das']} (FAP, updated {meta['updated']}) · scheduled {meta['total_sched']} · total DIP {meta['total_dip']}
home_port: Ketchikan, AK
generated: {today.isoformat()}
generated_by: scripts/import-sailing-schedule.py
status: DRAFT — confirm against VPASS/SDAT for authoritative
---

<!-- GENERATED FILE — edits will be overwritten. Re-run: python3 scripts/import-sailing-schedule.py -->

# FY26 Sailing Schedule — NOAA Ship *Fairweather*

Mechanical import of the FY26 DRAFT sailing schedule (MOC-Pacific). **Latest sheet: {sheet_name} ({meta['authorized_das']} authorized DAS).** Draft — planning reference, not authoritative tasking; VPASS/SDAT is the system of record.

> **Current leg (as of {today.isoformat()}):** {cur_line}

---

## Deployment season — operational legs (on/after {DEPLOY_CUTOFF.isoformat()})

The active planning horizon. **Crew-recovery rows = inports** — the windows for logistics, connectivity-heavy admin, crew turnover, parts, fuel.

{deploy_tbl}

**Inport quick list:** {' · '.join(inports) if inports else '(none parsed)'}

---

## Pre-season reference (before {DEPLOY_CUTOFF.isoformat()})

{pre_tbl}

---

{LEGEND}

{NOTES}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ods", type=Path, default=DEFAULT_ODS, help="path to the .ods schedule")
    ap.add_argument("--sheet", default=None, help="sheet name override (default: latest FY26 V#)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output markdown path")
    ap.add_argument("--list", action="store_true", help="list sheets and exit")
    ap.add_argument("--dry-run", action="store_true", help="print to stdout, don't write")
    args = ap.parse_args()

    if not args.ods.exists():
        sys.exit(f"ODS not found: {args.ods}")

    xls = pd.ExcelFile(args.ods, engine="odf")
    if args.list:
        for n in xls.sheet_names:
            print(n)
        return

    sheet = pick_sheet(xls.sheet_names, args.sheet)
    df = xls.parse(sheet, header=None)
    legs, meta = parse_schedule(df)
    if not legs:
        sys.exit(f"parsed 0 legs from sheet {sheet!r} — schedule layout may have changed")

    doc = build_doc(legs, meta, sheet, args.ods)

    if args.dry_run:
        print(doc)
        print(f"\n--- parsed {len(legs)} legs from sheet {sheet!r} (dry-run, not written) ---", file=sys.stderr)
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(doc)
    print(f"wrote {args.out} — sheet {sheet!r}, {len(legs)} legs")


if __name__ == "__main__":
    main()
