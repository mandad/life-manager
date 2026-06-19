#!/usr/bin/env python3
"""Pull the ship's position from the relay (laptop side) — latest fix for /daily, or the time series.

Pairs with `ship-locator/push-position.ps1` (ship work PC) + `ship-locator/relay/ship-relay.php`
(DreamHost, SQLite time-series). Position is public-equivalent (Windy shows it by call sign), so the
payload is plaintext JSON; access is gated by a read token over HTTPS.

Primary location source is the public mfphub AIS feed (https://mfphub.global/Ship/Read, no auth) —
the relay isn't built yet. `--source auto` (default) pulls mfphub first and only drops to the relay
if mfphub fails; `--source mfphub|relay` forces one. mfphub yields position/SOG/COG/heading only and
has no time series, so `--history` is relay-only.

Weather (water temp, air temp, baro, wind) never comes from mfphub — AIS carries none. It comes only
from the relay's ship-sensor obs or a NOAA SCS feed; absent both, use `nws_get_marine_forecast` at
the reported lat/lon.

Config (no secrets in code): CLI arg > env > scripts/.ship-relay.conf (JSON):
  url   : SHIP_RELAY_URL         e.g. https://yourdomain/scs/ship-relay.php
  read  : SHIP_RELAY_READ_TOKEN
Run via `bash -ic` in /daily so the ~/.bashrc env loads (the bare tool shell is non-interactive).

Usage:
  bash -ic 'python3 scripts/ship-position.py'                 # latest fix (markdown summary + freshness)
  bash -ic 'python3 scripts/ship-position.py --latlon'        # "LAT LON" to pipe to the marine forecast
  bash -ic 'python3 scripts/ship-position.py --json'
  bash -ic 'python3 scripts/ship-position.py --history --since 2026-06-01T00:00:00Z --limit 200'
  bash -ic 'python3 scripts/ship-position.py --history --csv > track.csv'
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONF = Path(__file__).resolve().parent / ".ship-relay.conf"

MFPHUB_URL = "https://mfphub.global/Ship/Read"  # public NOAA/UNOLS AIS feed, no auth
MFPHUB_NAME = "FAIRWEATHER"
MFPHUB_MMSI = 369960000


def _conf_get(key):
    if CONF.exists():
        try:
            return json.loads(CONF.read_text()).get(key)
        except Exception:
            return None
    return None


def resolve(arg, env, conf_key):
    return arg or os.environ.get(env) or _conf_get(conf_key)


def fetch(url, read_token, params=None):
    q = dict(params or {})
    if read_token:
        q["read"] = read_token
    sep = "&" if "?" in url else "?"
    full = url + (sep + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(full, headers={"User-Agent": "ship-position/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode(), r.headers.get_content_type()


def fetch_mfphub(name=MFPHUB_NAME, mmsi=MFPHUB_MMSI, url=MFPHUB_URL):
    """Latest AIS fix for the ship from the public mfphub feed, normalized to the relay's field shape.

    Feed is an array of host groups, each with a `Ships` list. Match by name substring (case-
    insensitive) or MMSI. AIS sentinels (Course 360, Heading 511 = "not available") are nulled.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "ship-position/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        groups = json.loads(r.read().decode())
    want = (name or "").strip().upper()
    for grp in groups:
        for s in grp.get("Ships", []):
            if (want and want in (s.get("Name", "") or "").upper()) or (mmsi and s.get("MMSI") == mmsi):
                cog = s.get("Course")
                hdg = s.get("Heading")
                return {
                    "lat": s.get("Latitude"),
                    "lon": s.get("Longitude"),
                    "utc": s.get("RecordDate"),
                    "sog_kt": s.get("Speed"),
                    "cog": None if cog in (360, None) else cog,
                    "heading": None if hdg in (511, None) else hdg,
                    "mmsi": s.get("MMSI"),
                    "name": s.get("Name"),
                    "_source": "mfphub (public AIS)",
                }
    raise RuntimeError(f"ship '{name}' (MMSI {mmsi}) not in mfphub feed")


def age_str(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return h, (f"{h*60:.0f} min ago" if h < 1 else f"{h:.1f} h ago")
    except Exception:
        return None, "age unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--read-token")
    ap.add_argument("--source", choices=("auto", "relay", "mfphub"), default="auto",
                    help="latest-fix source: auto=mfphub then relay on fail (default); relay or mfphub forces one")
    ap.add_argument("--ship-name", default=MFPHUB_NAME, help="mfphub name match (default FAIRWEATHER)")
    ap.add_argument("--max-age-h", type=float, default=3.0, help="warn if latest fix older than this (default 3h)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--latlon", action="store_true", help="print 'LAT LON' only (for piping)")
    ap.add_argument("--history", action="store_true", help="query the time series instead of the latest fix")
    ap.add_argument("--since", help="history: ISO lower bound on utc")
    ap.add_argument("--until", help="history: ISO upper bound on utc")
    ap.add_argument("--limit", type=int, help="history: max rows (relay caps at 5000)")
    ap.add_argument("--csv", action="store_true", help="history: pass through CSV from the relay")
    args = ap.parse_args()

    if args.history and args.source == "mfphub":
        sys.exit("--history not available from mfphub (latest fix only); use the relay")

    url = resolve(args.url, "SHIP_RELAY_URL", "url")
    read_token = resolve(args.read_token, "SHIP_RELAY_READ_TOKEN", "read")
    # relay is only required for history or when the user forced --source relay; mfphub needs no relay
    needs_relay = args.history or args.source == "relay"
    if needs_relay and not url:
        sys.exit("missing relay URL (CLI/env SHIP_RELAY_URL/scripts/.ship-relay.conf)")

    # ---- history mode ----
    if args.history:
        params = {"history": "1"}
        if args.since:
            params["since"] = args.since
        if args.until:
            params["until"] = args.until
        if args.limit:
            params["limit"] = args.limit
        if args.csv:
            params["format"] = "csv"
        try:
            body, _ = fetch(url, read_token, params)
        except Exception as e:
            sys.exit(f"history pull failed: {e}")
        if args.csv:
            sys.stdout.write(body)
            return
        d = json.loads(body)
        rows = d.get("positions", [])
        if args.json:
            print(json.dumps(d, indent=2))
            return
        print(f"### Ship track ({d.get('count', len(rows))} fixes)")
        if rows:
            newest, oldest = rows[0], rows[-1]  # relay returns DESC
            print(f"- span: {oldest.get('utc')} → {newest.get('utc')}")
            print(f"- newest: {newest.get('lat')}, {newest.get('lon')}"
                  + (f" (SOG {newest.get('sog_kt')} kt, COG {newest.get('cog')}°)" if newest.get('sog_kt') is not None else ""))
            print(f"- oldest: {oldest.get('lat')}, {oldest.get('lon')}")
            lats = [r["lat"] for r in rows if r.get("lat") is not None]
            lons = [r["lon"] for r in rows if r.get("lon") is not None]
            if lats and lons:
                print(f"- bbox: lat {min(lats):.3f}…{max(lats):.3f} · lon {min(lons):.3f}…{max(lons):.3f}")
        else:
            print("- no fixes in range")
        return

    # ---- latest fix (default) ----
    # mfphub is the primary location source (no relay built yet); relay is the fallback when present.
    data, errs = None, []
    if args.source in ("auto", "mfphub"):
        try:
            data = fetch_mfphub(args.ship_name)
        except Exception as e:
            errs.append(f"mfphub: {e}")
    if data is None and args.source in ("auto", "relay") and url:
        try:
            data = json.loads(fetch(url, read_token)[0])
            data.setdefault("_source", "relay (own GPS)")
        except Exception as e:
            errs.append(f"relay: {e}")
    if data is None:
        sys.exit("ship-position pull failed: " + ("; ".join(errs) or "no source available"))

    if args.latlon:
        print(f"{data['lat']} {data['lon']}")
        return

    h, agetxt = age_str(data.get("utc", ""))
    stale = " ⚠️STALE" if (h is not None and h > args.max_age_h) else ""
    if args.json:
        data["_age_hours"] = round(h, 2) if h is not None else None
        data["_stale"] = bool(stale)
        print(json.dumps(data, indent=2))
        return

    lat, lon = data.get("lat"), data.get("lon")
    bits = []
    for k, label, unit in (("sog_kt", "SOG", " kt"), ("cog", "COG", "°"), ("heading", "hdg", "°"),
                           ("wtmp", "water", ""), ("atmp", "air", ""), ("baro", "baro", ""), ("wspd", "wind", "")):
        if data.get(k) is not None:
            bits.append(f"{label} {data[k]}{unit}")
    src = data.get("_source", "relay (own GPS)")
    print(f"### Ship position (via {src})")
    print(f"- **{lat}, {lon}** — obs {agetxt}{stale}" + (f" · {' · '.join(bits)}" if bits else ""))
    print(f"- feed to marine forecast: `nws_get_marine_forecast(lat={lat}, lon={lon})`")
    if stale:
        print(f"- _latest fix older than {args.max_age_h}h — pusher may be down / ship off-network; "
              f"fall back to the schedule-leg point._")


if __name__ == "__main__":
    main()
