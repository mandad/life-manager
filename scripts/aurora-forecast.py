#!/usr/bin/env python3
"""Aurora / geomagnetic forecast for /daily — alert when Kp is expected to exceed a threshold.

Two free, no-auth NOAA SWPC feeds:
  - Planetary K-index 3-day forecast (global geomagnetic activity, 3-hourly).
  - OVATION aurora model (probability of visible aurora on a 1° lat/lon grid) — gives the
    *location-specific* signal at the ship's position.

Kp is a planetary index (same everywhere); at the user's high latitudes (Bering ≈60°N, Nome
64.5°N, Arctic ≥70°N) Kp > 4 means aurora is likely overhead. The OVATION probability at the
live position refines "likely" into a percentage. Defaults to the live ship AIS position via
ship-position.py (mfphub); pass --lat/--lon to override.

Usage:
    bash -ic 'python3 scripts/aurora-forecast.py'                 # peak Kp + aurora % at ship, alert if Kp>4
    python3 scripts/aurora-forecast.py --lat 64.5 --lon -165.4 --label Nome
    python3 scripts/aurora-forecast.py --threshold 5 --hours 48
    python3 scripts/aurora-forecast.py --json
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index-forecast.json"
OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aurora-forecast/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


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


def parse_kp_forecast(rows, hours):
    """Return (latest_observed, peak_predicted) where each is (datetime, kp) or None.
    peak_predicted is the highest predicted Kp within the next `hours`."""
    now = datetime.now(timezone.utc)
    horizon = now.timestamp() + hours * 3600
    latest_obs = None
    peak = None
    for row in rows:
        try:
            t = datetime.fromisoformat(row["time_tag"]).replace(tzinfo=timezone.utc)
            kp = float(row["kp"])
        except (KeyError, ValueError, TypeError):
            continue
        if row.get("observed") == "observed":
            if latest_obs is None or t > latest_obs[0]:
                latest_obs = (t, kp)
        elif row.get("observed") == "predicted":
            if t.timestamp() <= horizon and (peak is None or kp > peak[1]):
                peak = (t, kp)
    return latest_obs, peak


def aurora_prob_at(ovation, lat, lon):
    """Nearest-grid aurora probability (%) at lat/lon from the OVATION coordinate list."""
    coords = ovation.get("coordinates", [])
    if not coords:
        return None
    lon360 = round(lon % 360)
    latr = round(lat)
    grid = {(int(c[0]), int(c[1])): c[2] for c in coords}
    for dlon in (0, 1, -1, 2, -2):
        for dlat in (0, 1, -1, 2, -2):
            v = grid.get(((lon360 + dlon) % 360, latr + dlat))
            if v is not None:
                return v
    return None


def kt(dt):
    return dt.strftime("%Y-%m-%d %HZ") if dt else "—"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, help="latitude (default: live ship position)")
    ap.add_argument("--lon", type=float, help="longitude")
    ap.add_argument("--threshold", type=float, default=4.0, help="alert if peak predicted Kp exceeds this (default 4)")
    ap.add_argument("--hours", type=int, default=72, help="forecast horizon in hours (default 72)")
    ap.add_argument("--label", default="", help="header label")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.lat is None or args.lon is None:
        args.lat, args.lon = ship_latlon()
        if not args.label:
            args.label = "live ship pos"

    try:
        kp_rows = get(KP_URL)
    except Exception as e:
        sys.exit(f"SWPC Kp forecast fetch failed: {e}")
    latest_obs, peak = parse_kp_forecast(kp_rows, args.hours)

    prob = None
    ov_time = None
    try:
        ov = get(OVATION_URL)
        prob = aurora_prob_at(ov, args.lat, args.lon)
        ov_time = ov.get("Forecast Time")
    except Exception:
        pass  # OVATION optional; Kp alert still works

    alert = bool(peak and peak[1] > args.threshold)

    if args.json:
        print(json.dumps({
            "location": [args.lat, args.lon], "label": args.label,
            "latest_observed_kp": {"time": kt(latest_obs[0]), "kp": latest_obs[1]} if latest_obs else None,
            "peak_predicted_kp": {"time": kt(peak[0]), "kp": peak[1]} if peak else None,
            "threshold": args.threshold, "alert": alert,
            "aurora_probability_pct": prob, "ovation_forecast_time": ov_time,
        }, indent=2))
        return

    loc = f"{args.lat},{args.lon}" + (f" ({args.label})" if args.label else "")
    print("### Aurora / Kp (NOAA SWPC)")
    if latest_obs:
        print(f"- latest observed Kp: **{latest_obs[1]:.2f}** @ {kt(latest_obs[0])}")
    if peak:
        flag = "  ⚠️ Kp>{:.0f}".format(args.threshold) if alert else ""
        print(f"- peak forecast Kp (next {args.hours}h): **{peak[1]:.2f}** @ {kt(peak[0])}{flag}")
    else:
        print(f"- no predicted Kp in the next {args.hours}h")
    if prob is not None:
        print(f"- aurora probability @ {loc}: **{prob:.0f}%**" + (f" (OVATION {ov_time})" if ov_time else ""))
    if alert:
        p = f", aurora prob {prob:.0f}% at your location" if prob is not None else ""
        print(f"- **ALERT: Kp {peak[1]:.2f} expected @ {kt(peak[0])} (> {args.threshold:.0f}){p} — aurora likely.**")


if __name__ == "__main__":
    main()
