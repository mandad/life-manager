#!/usr/bin/env python3
"""Pull nearby marine/coastal weather OBSERVATIONS from the Synoptic Data API for /daily.

Ground-truth to sit alongside the NWS marine FORECAST (nws_get_marine_forecast): given the
ship's operating-area lat/lon (from the FY26 sailing-schedule leg), this returns the nearest
buoy / CMAN / coastal station obs — actual wind, seas, water/air temp, pressure. For an inport,
pass --stid (e.g. PAOM for Nome) to read that station directly.

NOTE — position: Synoptic does NOT carry the ship's own report by call sign (WTEB is not a
Synoptic station; its WIS2 network is land SYNOP with synthetic IDs, marine nets are fixed
buoys/CMAN). So this gives *regional* obs near a point you supply. The ship's live AIS position now
comes from mfphub via ship-position.py — pass --from-ship to auto-locate (the 2026-05-31 "needs AIS
by IMO 6710920" gap is filled). For ground-truth weather at the ship itself, see scs-weather.py.

Token resolution (never hardcoded): --token  >  $SYNOPTIC_TOKEN  >  scripts/.synoptic_token
Because $SYNOPTIC_TOKEN lives in ~/.bashrc (interactive only), invoke under an interactive
shell so the profile loads:
    bash -ic 'python3 scripts/synoptic-obs.py --from-ship'                  # obs near the live ship pos
    bash -ic 'python3 scripts/synoptic-obs.py --lat 60.0 --lon -173.5'
    bash -ic 'python3 scripts/synoptic-obs.py --stid PAOM --label "Nome inport"'
Add --json for structured output.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from _secrets import load_secrets  # automation #42: secrets.env is the single store

API = "https://api.synopticdata.com/v2/stations/latest"
MARINE_NETWORKS = "96,286,179,332"  # MARITIME(buoys/CMAN), NDBC, AJKNDBC, Marine Exchange AK

# var key (without _value_N suffix) -> short label, in display order
VAR_LABELS = [
    ("wind_speed", "wind"),
    ("wind_gust", "gust"),
    ("wind_direction", "dir"),
    ("wind_cardinal_direction", "dir"),
    ("wave_height", "seas"),
    ("dominant_wave_period", "wave-per"),
    ("primary_swell_wave_height", "swell"),
    ("T_water_temp", "water"),
    ("air_temp", "air"),
    ("sea_level_pressure", "SLP"),
    ("pressure", "pres"),
    ("visibility", "vis"),
]


def resolve_token(arg_token):
    if arg_token:
        return arg_token
    load_secrets()          # fills from secrets.env; anything already exported wins
    env = os.environ.get("SYNOPTIC_TOKEN")
    if env:
        return env
    f = Path(__file__).resolve().parent / ".synoptic_token"
    if f.exists():
        return f.read_text().strip()
    sys.exit("No token — pass --token, or set SYNOPTIC_TOKEN in "
             "~/.config/llm-land-mcp/secrets.env (see mcp-servers/secrets.env.example). "
             "Legacy fallbacks still honoured: $SYNOPTIC_TOKEN in the environment, "
             "or scripts/.synoptic_token.")


def fetch(params):
    url = API + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=40) as r:
        return json.loads(r.read())


def ship_latlon():
    """Live ship lat/lon from ship-position.py (mfphub AIS). Returns (lat, lon)."""
    script = Path(__file__).resolve().parent / "ship-position.py"
    try:
        out = subprocess.run([sys.executable, str(script), "--source", "mfphub", "--latlon"],
                             capture_output=True, text=True, timeout=45)
    except Exception as e:
        sys.exit(f"--from-ship: could not run ship-position.py: {e}")
    if out.returncode != 0:
        sys.exit("--from-ship: ship-position.py failed: " + (out.stderr.strip() or "unknown error"))
    parts = out.stdout.split()
    if len(parts) != 2:
        sys.exit("--from-ship: unexpected position output: " + (out.stdout.strip() or "<empty>"))
    return float(parts[0]), float(parts[1])


def age_hours(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return None


def pick_obs(observations):
    """Return ordered list of (label, value, var, date_time) for marine-relevant vars present."""
    out = []
    seen = set()
    for var, label in VAR_LABELS:
        for suffix in ("_value_1", "_value_1d"):
            key = var + suffix
            if key in observations and var not in seen:
                v = observations[key]
                val = v.get("value") if isinstance(v, dict) else v
                dt = v.get("date_time", "") if isinstance(v, dict) else ""
                if val is not None and val != "":
                    out.append((label, val, var, dt))
                    seen.add(var)
                break
    return out


def station_line(s, max_age_h):
    obs = pick_obs(s.get("OBSERVATIONS", {}))
    if not obs:
        return None
    # newest ob time for staleness
    times = [age_hours(dt) for _, _, _, dt in obs if dt]
    newest = min([t for t in times if t is not None], default=None)
    stale = f" ⚠️stale {newest:.0f}h" if newest is not None and newest > max_age_h else ""
    dist = s.get("DISTANCE")
    dist_s = f"{float(dist):.0f} mi" if dist not in (None, "") else "—"
    bits = []
    for label, val, var, _ in obs:
        if var in ("wind_cardinal_direction",) and any(b.startswith("dir ") for b in bits):
            continue
        bits.append(f"{label} {val}")
    return f"- **{s.get('STID')}** {s.get('NAME','')} ({dist_s}){stale}: " + " · ".join(bits)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--stid", help="specific station id (e.g. PAOM for Nome inport)")
    g.add_argument("--lat", type=float, help="operating-area latitude (with --lon)")
    g.add_argument("--from-ship", action="store_true", help="auto-locate at the live ship AIS position (mfphub)")
    ap.add_argument("--lon", type=float, help="operating-area longitude")
    ap.add_argument("--radius", type=float, default=200, help="search radius miles (default 200, max 300)")
    ap.add_argument("--networks", default=MARINE_NETWORKS, help="Synoptic network ids (default marine)")
    ap.add_argument("--nearest", type=int, default=3, help="how many nearest stations to show (radius mode)")
    ap.add_argument("--max-age-h", type=float, default=6, help="flag obs older than this many hours")
    ap.add_argument("--label", default="", help="header label, e.g. 'St. Matthew grounds'")
    ap.add_argument("--token", help="Synoptic token (else $SYNOPTIC_TOKEN, else scripts/.synoptic_token)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    token = resolve_token(args.token)
    if args.from_ship:
        args.lat, args.lon = ship_latlon()
        if not args.label:
            args.label = "at live ship pos"
    params = {"token": token, "units": "english", "obtimezone": "utc"}
    if args.stid:
        params["stid"] = args.stid
        where = f"station {args.stid}"
    else:
        if args.lon is None:
            sys.exit("--lat requires --lon")
        params["radius"] = f"{args.lat},{args.lon},{min(args.radius,300)}"
        params["network"] = args.networks
        where = f"{args.lat},{args.lon} r{min(args.radius,300):.0f}mi"

    try:
        d = fetch(params)
    except Exception as e:
        sys.exit(f"Synoptic request failed: {e}")

    msg = d.get("SUMMARY", {}).get("RESPONSE_MESSAGE", "")
    sts = d.get("STATION", [])
    sts.sort(key=lambda s: float(s.get("DISTANCE", 99999) or 99999))
    if not args.stid:
        sts = sts[: args.nearest]

    if args.json:
        print(json.dumps({"where": where, "message": msg,
                          "stations": [{"stid": s.get("STID"), "name": s.get("NAME"),
                                        "lat": s.get("LATITUDE"), "lon": s.get("LONGITUDE"),
                                        "distance_mi": s.get("DISTANCE"),
                                        "obs": s.get("OBSERVATIONS", {})} for s in sts]}, indent=2))
        return

    hdr = f"### Ship-area obs (Synoptic){f' — {args.label}' if args.label else ''}"
    lines = [hdr, f"_{where} · units: knots/°F/ft/mb · {msg}_", ""]
    any_line = False
    for s in sts:
        ln = station_line(s, args.max_age_h)
        if ln:
            lines.append(ln)
            any_line = True
    if not any_line:
        lines.append("_No usable obs returned (no stations in range, or all fields empty). "
                     "Synoptic carries no ship-by-callsign data — this is regional ground-truth only._")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
