#!/usr/bin/env python3
"""Reconcile a day's Strava activities against Whoop workouts for /daily Step 7.

The /daily Whoop + Strava sub-agents already pull the raw data; this script does the
mechanical cross-check so the main agent doesn't re-derive it each run:
  - matches activities by start time within a window (default ±30 min)
  - flags items present in one source but not the other
  - flags duration / avg-HR deltas above a threshold (default 25%) on matched pairs

Context (per user 2026-05-31): the user's Strava entries come from a **Garmin Fenix**
watch, Whoop is a separate strap — so small HR/duration deltas on matched pairs are
EXPECTED instrumentation noise, not a problem. The threshold filters that out; only
larger gaps or unmatched activities surface.

Input — pass a single JSON object on stdin or via --infile:
    { "strava": [ {...}, ... ], "whoop": [ {...}, ... ] }
or two files: --strava strava.json --whoop whoop.json
Each list holds the raw objects from the respective MCP (field-name aliases handled).

Usage:
    cat day.json | python3 scripts/strava-whoop-reconcile.py --date 2026-05-29
    python3 scripts/strava-whoop-reconcile.py --strava s.json --whoop w.json
    ... --window-min 30 --threshold 0.25 --json
"""
import argparse
import json
import sys
from datetime import datetime, timezone


def first(d, *keys, default=None):
    """Return the first present, non-null value among aliased keys (supports a.b nesting)."""
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur and cur[part] is not None:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return default


def parse_dt(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):  # epoch seconds
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:  # assume UTC if naive (Whoop is UTC; Strava UTC field preferred)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def norm_strava(a):
    start = parse_dt(first(a, "start_date", "start_date_local", "start"))
    dur_s = first(a, "elapsed_time", "moving_time", "duration", default=None)
    return {
        "source": "strava",
        "label": first(a, "name", "type", "sport_type", default="activity"),
        "type": first(a, "sport_type", "type", default=""),
        "start": start,
        "dur_min": round(dur_s / 60, 1) if isinstance(dur_s, (int, float)) else None,
        "avg_hr": first(a, "average_heartrate", "average_hr", "avg_hr"),
        "max_hr": first(a, "max_heartrate", "max_hr"),
        "dist_km": round(first(a, "distance", default=0) / 1000, 2)
        if isinstance(first(a, "distance"), (int, float))
        else None,
    }


def norm_whoop(w):
    start = parse_dt(first(w, "start", "start_time", "during.start"))
    end = parse_dt(first(w, "end", "end_time", "during.end"))
    dur_min = round((end - start).total_seconds() / 60, 1) if start and end else None
    return {
        "source": "whoop",
        "label": first(w, "sport_name", "sport", "activity_name", default=None)
        or f"sport_id {first(w, 'sport_id', default='?')}",
        "type": str(first(w, "sport_name", "sport_id", default="")),
        "start": start,
        "dur_min": dur_min,
        "avg_hr": first(w, "score.average_heart_rate", "average_heart_rate", "avg_hr"),
        "max_hr": first(w, "score.max_heart_rate", "max_heart_rate", "max_hr"),
        "strain": first(w, "score.strain", "strain"),
        "dist_km": None,
    }


def pct_delta(a, b):
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    hi = max(abs(a), abs(b))
    if hi == 0:
        return 0.0
    return abs(a - b) / hi


def hhmm(dt):
    return dt.astimezone(timezone.utc).strftime("%H:%MZ") if dt else "—"


def reconcile(stravas, whoops, window_min, threshold):
    s_list = [norm_strava(a) for a in stravas]
    w_list = [norm_whoop(w) for w in whoops]
    matched = []
    used_w = set()
    for s in s_list:
        best, best_i, best_gap = None, None, None
        for i, w in enumerate(w_list):
            if i in used_w or not s["start"] or not w["start"]:
                continue
            gap = abs((s["start"] - w["start"]).total_seconds()) / 60
            if gap <= window_min and (best_gap is None or gap < best_gap):
                best, best_i, best_gap = w, i, gap
        if best is not None:
            used_w.add(best_i)
            flags = []
            dd = pct_delta(s["dur_min"], best["dur_min"])
            hd = pct_delta(s["avg_hr"], best["avg_hr"])
            if dd is not None and dd > threshold:
                flags.append(f"dur Δ{dd*100:.0f}%")
            if hd is not None and hd > threshold:
                flags.append(f"avgHR Δ{hd*100:.0f}%")
            matched.append({"strava": s, "whoop": best, "gap_min": round(best_gap, 1), "flags": flags})
    strava_only = [s for s in s_list if all(m["strava"] is not s for m in matched)]
    whoop_only = [w for i, w in enumerate(w_list) if i not in used_w]
    return matched, strava_only, whoop_only


def render_md(matched, strava_only, whoop_only, date, window_min, threshold):
    out = [f"### Strava ↔ Whoop reconciliation{f' — {date}' if date else ''}"]
    if not matched and not strava_only and not whoop_only:
        out.append("No activities in either source.")
        return "\n".join(out)
    if matched:
        out.append("")
        out.append("| Match (±gap) | Strava | Whoop | Flags |")
        out.append("|---|---|---|---|")
        for m in matched:
            s, w = m["strava"], m["whoop"]
            s_cell = f"{s['label']} · {s['dur_min']}m · HR {s['avg_hr'] or '—'}/{s['max_hr'] or '—'}"
            strain = f" · strain {w['strain']}" if w.get("strain") is not None else ""
            w_cell = f"{w['label']} · {w['dur_min']}m · HR {w['avg_hr'] or '—'}/{w['max_hr'] or '—'}{strain}"
            flags = ", ".join(m["flags"]) if m["flags"] else "✓ within tol"
            out.append(f"| {hhmm(s['start'])} ±{m['gap_min']}m | {s_cell} | {w_cell} | {flags} |")
    if strava_only:
        out.append("")
        out.append("**Strava only (no Whoop match):** " + "; ".join(
            f"{s['label']} @ {hhmm(s['start'])} ({s['dur_min']}m)" for s in strava_only))
    if whoop_only:
        out.append("")
        out.append("**Whoop only (no Strava match):** " + "; ".join(
            f"{w['label']} @ {hhmm(w['start'])} ({w['dur_min']}m)" for w in whoop_only))
    note = (
        f"_Window ±{window_min}m, flag threshold {threshold*100:.0f}%. "
        f"Strava = Garmin Fenix, Whoop = strap — small matched deltas are expected instrumentation "
        f"noise, not a data problem. Unmatched items or large deltas are the signal._"
    )
    out += ["", note]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strava", help="JSON file: array of Strava activities")
    ap.add_argument("--whoop", help="JSON file: array of Whoop workouts")
    ap.add_argument("--infile", help="JSON file: {strava:[...], whoop:[...]} (else stdin)")
    ap.add_argument("--date", default="", help="date label for the header (YYYY-MM-DD)")
    ap.add_argument("--window-min", type=float, default=30, help="match window, minutes (default 30)")
    ap.add_argument("--threshold", type=float, default=0.25, help="delta flag threshold 0-1 (default 0.25)")
    ap.add_argument("--json", action="store_true", help="emit structured JSON instead of markdown")
    args = ap.parse_args()

    if args.strava or args.whoop:
        stravas = json.load(open(args.strava)) if args.strava else []
        whoops = json.load(open(args.whoop)) if args.whoop else []
    else:
        raw = open(args.infile).read() if args.infile else sys.stdin.read()
        if not raw.strip():
            sys.exit("no input — pass --strava/--whoop, --infile, or pipe {strava:[],whoop:[]} on stdin")
        obj = json.loads(raw)
        stravas, whoops = obj.get("strava", []), obj.get("whoop", [])
    # tolerate a top-level {activities:[...]} / {workouts:[...]} or {records:[...]} wrapper
    if isinstance(stravas, dict):
        stravas = stravas.get("activities") or stravas.get("records") or []
    if isinstance(whoops, dict):
        whoops = whoops.get("workouts") or whoops.get("records") or []

    matched, s_only, w_only = reconcile(stravas, whoops, args.window_min, args.threshold)

    if args.json:
        def clean(d):
            return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items()}
        print(json.dumps({
            "matched": [{"gap_min": m["gap_min"], "flags": m["flags"],
                         "strava": clean(m["strava"]), "whoop": clean(m["whoop"])} for m in matched],
            "strava_only": [clean(s) for s in s_only],
            "whoop_only": [clean(w) for w in w_only],
        }, indent=2))
    else:
        print(render_md(matched, s_only, w_only, args.date, args.window_min, args.threshold))


if __name__ == "__main__":
    main()
