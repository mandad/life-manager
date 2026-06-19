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
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

URL = "https://scsshore.noaa.gov/FSDB/SensorData?shipName={ship}"

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

    series = {name: last_value(body) for name, body in SERIES_RE.findall(html)}
    if not series:
        sys.exit("SCS parse failed: no sensor series found (page layout may have changed)")

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

    if args.json:
        out = {"ship": args.ship, "obs_utc": obs_iso, "age_hours": round(h, 2) if h is not None else None,
               "stale": stale, "units": {lbl: unit for _, (lbl, unit, _o) in SERIES.items()},
               "values": {}}
        for name, (lbl, unit, _o) in SERIES.items():
            if series.get(name) is not None:
                out["values"][lbl] = series[name]
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


if __name__ == "__main__":
    main()
