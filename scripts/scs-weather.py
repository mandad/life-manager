#!/usr/bin/env python3
"""Pull the ship's own met/sea sensor OBSERVATIONS from the NOAA SCS shore page for /daily.

Source: https://scsshore.noaa.gov/FSDB/SensorData?shipName=Fairweather (public, no auth). The page
embeds ~48 h of underway sensor data as Kendo chart series (20-min spacing); this scrapes the JSON
out of that HTML and reports the latest reading of each. Unlike Synoptic (regional buoys) this IS the
ship's own instruments — wind, barometric pressure, air temp, sea-surface temp, salinity, SOG/COG —
so it's the authoritative weather-at-the-ship until the relay exists.

Timestamps from SCS are UTC (Zulu). Times reported by NOAA SCS run a few hours behind real-time.

Usage:
    python3 scripts/scs-weather.py                 # markdown summary of the latest reading
    python3 scripts/scs-weather.py --json
    python3 scripts/scs-weather.py --ship "Rainier"
    python3 scripts/scs-weather.py --max-age-h 6   # flag if latest reading older than this

Stuck-channel check (automation #61, 2026-10-06): a channel whose ~48 h series holds a single
distinct value is flagged `STUCK?` — a frozen *plausible* reading passes every sanity check, which
is how the wind channel sat at 19.78 kt / 200° from 9/25 to 10/6 unnoticed. Each run also appends
the latest values to a snapshot cache and reports how many consecutive daily snapshots a stuck
value has persisted. Reportability is the routine's call (7.5 gate: underway/at anchor only).
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://scsshore.noaa.gov/FSDB/SensorData?shipName={ship}"
SNAP_CACHE = Path.home() / ".cache" / "llm-land" / "scs-snapshots.json"
STUCK_MIN_POINTS = 12      # ≥ 4 h of 20-min samples before a flat series counts as stuck
SNAP_KEEP = 60

# SCS series name -> (short label, unit, sort order). Depth dropped (always 0 underway).
SERIES = {
    "Wind Speed": ("wind", "kt", 0),
    "Wind Direction": ("dir", "°", 1),
    "Barometric Pressure": ("baro", "mb", 2),
    "Air Temp": ("air", "°C", 3),
    "Water Temp": ("water", "°C", 4),
    "Surface Salinity": ("sal", "PSU", 5),
    "SOG": ("SOG", "kt", 6),
    "COG": ("COG", "°", 7),
}

SERIES_RE = re.compile(r'\{"name":"([^"]+)","axis":"[^"]+","type":"[^"]+","data":\[([^\]]*)\]\}')
CATS_RE = re.compile(r'"categoryAxis":\[\{.*?"categories":\[([^\]]*)\]', re.DOTALL)


def fetch(ship):
    url = URL.format(ship=urllib.parse.quote(ship))
    req = urllib.request.Request(url, headers={"User-Agent": "scs-weather/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def last_value(data_str):
    """Last non-empty numeric value in a Kendo data array body."""
    for tok in reversed(data_str.split(",")):
        tok = tok.strip()
        if tok and tok.lower() not in ("null", "nan", '""'):
            try:
                return float(tok)
            except ValueError:
                return None
    return None


def numeric_values(data_str):
    out = []
    for tok in data_str.split(","):
        tok = tok.strip()
        if tok and tok.lower() not in ("null", "nan", '""'):
            try:
                out.append(float(tok))
            except ValueError:
                pass
    return out


def stuck_channels(raw):
    """raw: {series name: [floats]} for the page's ~48 h window. A channel is stuck when it has
    ≥ STUCK_MIN_POINTS readings and exactly one distinct value. SOG/COG are skipped when the ship
    isn't moving (both legitimately flat at a pier or anchor); Depth isn't a SERIES channel."""
    moving = max(raw.get("SOG") or [0.0]) >= 0.5
    hits = []
    for name, (lbl, unit, _o) in SERIES.items():
        vals = raw.get(name) or []
        if name in ("SOG", "COG") and not moving:
            continue
        if len(vals) >= STUCK_MIN_POINTS and len(set(vals)) == 1:
            hits.append({"channel": lbl, "value": vals[-1], "unit": unit, "readings": len(vals),
                         "window_h": round(len(vals) / 3, 1)})
    return hits


def update_snapshots(obs_iso, values):
    """Append this run's latest values (deduped by obs time); return the cache, oldest first."""
    try:
        snaps = json.loads(SNAP_CACHE.read_text())
    except (OSError, ValueError):
        snaps = []
    if obs_iso and not any(s.get("obs_utc") == obs_iso for s in snaps):
        snaps.append({"obs_utc": obs_iso, "values": values})
        snaps = snaps[-SNAP_KEEP:]
        try:
            SNAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SNAP_CACHE.write_text(json.dumps(snaps, indent=1))
        except OSError:
            pass
    return snaps


def unchanged_run(snaps, lbl, value):
    """How many consecutive most-recent snapshots carry exactly `value` for `lbl`, and since when."""
    n, since = 0, None
    for s in reversed(snaps):
        if s.get("values", {}).get(lbl) != value:
            break
        n, since = n + 1, s.get("obs_utc")
    return n, since


def cardinal(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg % 360) / 22.5 + 0.5) % 16]


def age_str(dt):
    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return h, (f"{h*60:.0f} min ago" if h < 1 else f"{h:.1f} h ago")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ship", default="Fairweather", help="SCS ship name (default Fairweather)")
    ap.add_argument("--max-age-h", type=float, default=6.0, help="flag latest reading older than this (default 6h)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        html = fetch(args.ship)
    except Exception as e:
        sys.exit(f"SCS fetch failed: {e}")

    found = SERIES_RE.findall(html)
    series = {name: last_value(body) for name, body in found}
    if not series:
        sys.exit("SCS parse failed: no sensor series found (page layout may have changed)")
    stuck = stuck_channels({name: numeric_values(body) for name, body in found})

    cats = CATS_RE.search(html)
    obs_iso = None
    obs_dt = None
    if cats:
        stamps = re.findall(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}", cats.group(1))
        if stamps:
            obs_iso = stamps[-1]
            try:
                obs_dt = datetime.strptime(obs_iso, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    h, agetxt = age_str(obs_dt) if obs_dt else (None, "time unknown")
    stale = bool(h is not None and h > args.max_age_h)

    latest = {lbl: series[name] for name, (lbl, _u, _o) in SERIES.items() if series.get(name) is not None}
    snaps = update_snapshots(obs_iso, latest)
    for s in stuck:
        s["snapshots_unchanged"], s["since_obs_utc"] = unchanged_run(snaps, s["channel"], s["value"])

    if args.json:
        out = {"ship": args.ship, "obs_utc": obs_iso, "age_hours": round(h, 2) if h is not None else None,
               "stale": stale, "units": {lbl: unit for _, (lbl, unit, _o) in SERIES.items()},
               "values": latest, "stuck": stuck}
        print(json.dumps(out, indent=2))
        return

    bits = []
    for name, (lbl, unit, _o) in sorted(SERIES.items(), key=lambda kv: kv[1][2]):
        v = series.get(name)
        if v is None:
            continue
        if lbl == "dir":
            bits.append(f"{lbl} {v:.0f}{unit} ({cardinal(v)})")
        elif lbl in ("air", "water"):
            bits.append(f"{lbl} {v:.1f}{unit} ({v*9/5+32:.0f}°F)")
        elif lbl == "baro":
            bits.append(f"{lbl} {v:.1f} {unit}")
        elif lbl == "sal":
            bits.append(f"{lbl} {v:.1f} {unit}")
        else:
            bits.append(f"{lbl} {v:.1f} {unit}")

    flag = " ⚠️STALE" if stale else ""
    print("### Ship weather (own SCS sensors)")
    print(f"- obs {agetxt}{flag} (SCS {obs_iso} UTC)" if obs_iso else f"- obs {agetxt}{flag}")
    print("- " + " · ".join(bits))
    if stale:
        print(f"- _SCS latest reading older than {args.max_age_h}h — NOAA SCS publishing lag; "
              f"position from mfphub may be fresher._")
    for s in stuck:
        run = (f"; unchanged across {s['snapshots_unchanged']} cached daily snapshots since {s['since_obs_utc']} UTC"
               if s["snapshots_unchanged"] >= 2 else "")
        print(f"- ⚠️ STUCK? {s['channel']} {s['value']:g} {s['unit']} — 1 distinct value across "
              f"{s['readings']} readings (~{s['window_h']:g} h){run}. "
              f"_Reportable underway/at anchor only (routine 7.5 gate)._")


if __name__ == "__main__":
    main()
