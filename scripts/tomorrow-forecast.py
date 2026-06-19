#!/usr/bin/env python3
"""Localized marine-ish FORECAST from the tomorrow.io v4 API for /daily.

Complements the NWS marine forecast and the Synoptic/SCS observations with a hi-res point forecast
at the ship's live position (or any lat/lon). tomorrow.io has a free tier (≈25 req/hr, 500/day,
3 req/s — be sparing). Docs: https://docs.tomorrow.io/reference/welcome

Defaults to the live ship AIS position via ship-position.py (mfphub); pass --lat/--lon to override.
Pulls /weather/realtime (now) + /weather/forecast hourly. Units requested in metric, shown
marine-friendly: wind kt, temp °C(°F), pressure mb, visibility nm.

Key resolution (never hardcoded): --key  >  $TOMORROW_API_KEY  >  scripts/.tomorrow_token

Usage:
    bash -ic 'python3 scripts/tomorrow-forecast.py'                  # now + 24 h at the live ship pos
    bash -ic 'python3 scripts/tomorrow-forecast.py --hours 48 --step 6'
    bash -ic 'python3 scripts/tomorrow-forecast.py --lat 64.5 --lon -165.4 --label Nome'
    python3 scripts/tomorrow-forecast.py --json
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

REALTIME = "https://api.tomorrow.io/v4/weather/realtime"
FORECAST = "https://api.tomorrow.io/v4/weather/forecast"

WX_CODE = {
    0: "Unknown", 1000: "Clear", 1100: "Mostly Clear", 1101: "Partly Cloudy", 1102: "Mostly Cloudy",
    1001: "Cloudy", 2000: "Fog", 2100: "Light Fog", 4000: "Drizzle", 4001: "Rain", 4200: "Light Rain",
    4201: "Heavy Rain", 5000: "Snow", 5001: "Flurries", 5100: "Light Snow", 5101: "Heavy Snow",
    6000: "Freezing Drizzle", 6001: "Freezing Rain", 6200: "Light Freezing Rain",
    6201: "Heavy Freezing Rain", 7000: "Ice Pellets", 7101: "Heavy Ice Pellets",
    7102: "Light Ice Pellets", 8000: "Thunderstorm",
}
MS_TO_KT = 1.94384
KM_TO_NM = 0.539957


def resolve_key(arg_key):
    if arg_key:
        return arg_key
    env = os.environ.get("TOMORROW_API_KEY")
    if env:
        return env
    f = Path(__file__).resolve().parent / ".tomorrow_token"
    if f.exists():
        return f.read_text().strip()
    sys.exit("No tomorrow.io key — pass --key, set $TOMORROW_API_KEY (in ~/.bashrc; run via `bash -ic`), "
             "or write scripts/.tomorrow_token. Get a free key at https://app.tomorrow.io/development/keys")


def ship_latlon():
    script = Path(__file__).resolve().parent / "ship-position.py"
    try:
        out = subprocess.run([sys.executable, str(script), "--source", "mfphub", "--latlon"],
                             capture_output=True, text=True, timeout=45)
    except Exception as e:
        sys.exit(f"--from-ship: could not run ship-position.py: {e}")
    if out.returncode != 0:
        sys.exit("ship-position.py failed: " + (out.stderr.strip() or "unknown error"))
    parts = out.stdout.split()
    if len(parts) != 2:
        sys.exit("unexpected position output: " + (out.stdout.strip() or "<empty>"))
    return float(parts[0]), float(parts[1])


def get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "tomorrow-forecast/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        sys.exit(f"tomorrow.io {url} HTTP {e.code}: {body}")
    except Exception as e:
        sys.exit(f"tomorrow.io request failed: {e}")


def cardinal(deg):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg % 360) / 22.5 + 0.5) % 16]


def fmt_vals(v):
    """One-line marine summary of a tomorrow.io values dict (metric input)."""
    bits = []
    code = v.get("weatherCode")
    if code is not None:
        bits.append(WX_CODE.get(int(code), str(code)))
    t = v.get("temperature")
    if t is not None:
        bits.append(f"{t:.0f}°C/{t*9/5+32:.0f}°F")
    ws = v.get("windSpeed")
    if ws is not None:
        wd = v.get("windDirection")
        g = v.get("windGust")
        s = f"wind {ws*MS_TO_KT:.0f}kt"
        if wd is not None:
            s += f" {cardinal(wd)}"
        if g is not None:
            s += f" g{g*MS_TO_KT:.0f}"
        bits.append(s)
    p = v.get("pressureSeaLevel")
    if p is not None:
        bits.append(f"{p:.0f}mb")
    pp = v.get("precipitationProbability")
    if pp:
        bits.append(f"precip {pp:.0f}%")
    vis = v.get("visibility")
    if vis is not None:
        bits.append(f"vis {vis*KM_TO_NM:.0f}nm")
    cc = v.get("cloudCover")
    if cc is not None:
        bits.append(f"cloud {cc:.0f}%")
    return " · ".join(bits)


def hhmm(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%m-%d %H%MZ")
    except Exception:
        return iso


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, help="latitude (default: live ship position)")
    ap.add_argument("--lon", type=float, help="longitude")
    ap.add_argument("--hours", type=int, default=24, help="forecast horizon in hours (default 24)")
    ap.add_argument("--step", type=int, default=3, help="hours between forecast rows (default 3)")
    ap.add_argument("--label", default="", help="header label")
    ap.add_argument("--key", help="tomorrow.io API key (else $TOMORROW_API_KEY, else scripts/.tomorrow_token)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    key = resolve_key(args.key)
    if args.lat is None or args.lon is None:
        args.lat, args.lon = ship_latlon()
        if not args.label:
            args.label = "live ship pos"
    loc = f"{args.lat},{args.lon}"

    now = get(REALTIME, {"location": loc, "units": "metric", "apikey": key})
    fc = get(FORECAST, {"location": loc, "timesteps": "1h", "units": "metric", "apikey": key})
    hourly = fc.get("timelines", {}).get("hourly", [])

    if args.json:
        print(json.dumps({"location": loc, "label": args.label,
                          "realtime": now.get("data", {}),
                          "hourly": hourly[: args.hours]}, indent=2))
        return

    hdr = f"### Tomorrow.io forecast — {loc}" + (f" ({args.label})" if args.label else "")
    print(hdr)
    nd = now.get("data", {})
    print(f"- **now** {hhmm(nd.get('time',''))}: {fmt_vals(nd.get('values', {}))}")
    print(f"- next {args.hours} h (every {args.step} h):")
    for i, row in enumerate(hourly[: args.hours]):
        if i % args.step != 0:
            continue
        print(f"  - {hhmm(row.get('time',''))}: {fmt_vals(row.get('values', {}))}")


if __name__ == "__main__":
    main()
