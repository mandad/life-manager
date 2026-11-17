#!/usr/bin/env python3
"""Pull the ship's position from the relay (laptop side) — latest fix for /daily, or the time series.

Pairs with `ship-locator/push-position.ps1` (ship work PC) + `ship-locator/relay/ship-relay.php`
(DreamHost, SQLite time-series). Position is public-equivalent (Windy shows it by call sign), so the
payload is plaintext JSON; access is gated by a read token over HTTPS.

Primary location source is the public mfphub AIS feed (https://mfphub.global/Ship/Read, no auth) —
the relay isn't built yet. `--source auto` (default) pulls mfphub first and only drops to the relay
if mfphub fails; `--source mfphub|relay` forces one. mfphub yields position + AIS nav status (under
way / at anchor / moored) and has no time series, so `--history` is relay-only. Note: mfphub's
`Speed` field is a nominal vessel speed, NOT live SOG (it reads 5–13 kt even at anchor), so it is
deliberately not reported — real SOG/COG come from the relay or from differencing `--history` fixes.

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
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONF = Path(__file__).resolve().parent / ".ship-relay.conf"

# Automation #59 (2026-09-26/28): cross-check the AIS nav status against actual displacement.
# mfphub's status field LAGS the ship (user, 2026-09-28: "we always update the status right away
# on the ship, but it doesn't seem to propagate to mfphub") — on 9/26 it broadcast "under way,
# 10 kt" while the ship swung at anchor in Zachar Bay. Same stationary set as the plot room
# (backend/app/services/mfphub.py _STATIONARY; frontend shipStatus.isStationaryStatus).
FIX_CACHE = Path.home() / ".cache" / "llm-land" / "ship-fixes.json"
STATIONARY_STATUS = {1, 5, 6}   # at anchor / moored / aground
STILL_NM = 0.25                 # less than this over >= STILL_GAP_MIN = effectively stationary (anchor swing)
MOVED_NM = 0.35                 # more than this = genuinely moved: beyond the ~0.3 nm max anchor-swing diameter
MIN_GAP_MIN = 8                 # 10/12: two fixes 9 min apart moved 0.38 nm while AIS said "at anchor"
STILL_GAP_MIN = 20              # a slow survey line can cover < STILL_NM in 8 min, so "stationary" needs the longer gap
MAX_GAP_MIN = 240               # older baselines mislead: a transit then a mooring reads as "moored but moved"
NOMINATIM = "https://nominatim.openstreetmap.org"
UA = {"User-Agent": "llm-land-ship-position/1.1 (personal /daily routine)"}

MFPHUB_URL = "https://mfphub.global/Ship/Read"  # public NOAA/UNOLS AIS feed, no auth
MFPHUB_NAME = "FAIRWEATHER"
MFPHUB_MMSI = 369960000

# AIS navigational status (ITU-R M.1371). mfphub's TrackHistory.Status carries this — the
# trustworthy "is the ship moving" signal, unlike its Speed field (see fetch_mfphub).
AIS_STATUS = {
    0: "under way (engine)", 1: "at anchor", 2: "not under command",
    3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
    6: "aground", 7: "fishing", 8: "under way (sailing)", 9: "reserved",
    11: "towing astern", 12: "towing alongside", 14: "AIS-SART/EPIRB", 15: "undefined",
}
UNDERWAY_STATUS = {0, 8}  # under way (engine) / under way (sailing) → Speed & COG are real SOG/COG


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
    insensitive) or MMSI.

    mfphub's `Speed`/`Course` are only reliable WHEN UNDER WAY. Empirically (2026-06-26) every
    moored/anchored ship reports Speed 5–13 kt and not one of 169 ships reads 0–4 kt, so the field
    floors and falsely shows motion at rest. But when actually transiting (>5 kt) the value is
    correct. So we gate on AIS nav status (`TrackHistory.Status`: 0=under way, 1=at anchor,
    5=moored, …): report Speed→SOG and Course→COG only when under way (status 0/8); suppress them
    when anchored/moored/unknown. The decoded nav status is always returned (the movement signal).
    AIS sentinels (Course 360, Heading 511 = "not available") are nulled.
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
                status_code = (s.get("TrackHistory") or {}).get("Status")
                nav_status = AIS_STATUS.get(status_code)  # None for 99/unknown/missing
                underway = status_code in UNDERWAY_STATUS
                return {
                    "lat": s.get("Latitude"),
                    "lon": s.get("Longitude"),
                    "utc": s.get("RecordDate"),
                    # Speed/Course are real SOG/COG only under way; floored & bogus at rest, so gate.
                    "sog_kt": s.get("Speed") if underway else None,
                    "cog": cog if (underway and cog not in (360, None)) else None,
                    "heading": None if hdg in (511, None) else hdg,
                    "nav_status": nav_status,
                    "nav_status_code": status_code,
                    "mmsi": s.get("MMSI"),
                    "name": s.get("Name"),
                    "_source": "mfphub (public AIS)",
                }
    raise RuntimeError(f"ship '{name}' (MMSI {mmsi}) not in mfphub feed")


def _parse_utc(iso):
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(h))


def cache_fix(data):
    """Append this fix to the rolling cache (deduped by fix time; keeps the last 20). Never raises."""
    try:
        fixes = json.loads(FIX_CACHE.read_text()) if FIX_CACHE.exists() else []
    except Exception:
        fixes = []
    row = {k: data.get(k) for k in ("utc", "lat", "lon", "heading", "nav_status_code", "nav_status", "sog_kt", "cog")}
    if row["utc"] and row["lat"] is not None and not any(f.get("utc") == row["utc"] for f in fixes):
        fixes.append(row)
    fixes = sorted(fixes, key=lambda f: str(f.get("utc")))[-20:]
    try:
        FIX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FIX_CACHE.write_text(json.dumps(fixes, indent=1))
    except OSError:
        pass
    return fixes


def motion_check(data, fixes):
    """Compare the AIS status with displacement since the newest cached fix >= MIN_GAP_MIN older.
    Returns (verdict_code, sentence) — verdict in {agrees, status_lag_stationary, status_lag_moving,
    no_baseline}."""
    now_t, lat, lon = _parse_utc(data.get("utc")), data.get("lat"), data.get("lon")
    if now_t is None or lat is None:
        return "no_baseline", "no fix time — motion not checked"
    prior, newest_gap = None, None
    for f in reversed(fixes):
        t = _parse_utc(f.get("utc"))
        if not t:
            continue
        gap = (now_t - t).total_seconds() / 60
        if gap > 0 and newest_gap is None:
            newest_gap = gap
        if MIN_GAP_MIN <= gap <= MAX_GAP_MIN:
            prior = (f, t)
            break
    if not prior:
        # Worded as the gap between THIS fix and the previous cached one; "cached fix is 19 h old"
        # was read as "the ship hasn't reported for 19 h" (10/7, a sub-agent flagged a phantom stall).
        why = (f"the previous cached fix is {newest_gap/60:.1f} h before this one (> {MAX_GAP_MIN/60:.0f} h)"
               if newest_gap and newest_gap > MAX_GAP_MIN
               else f"no earlier cached fix {MIN_GAP_MIN} min–{MAX_GAP_MIN/60:.0f} h before this one")
        return "no_baseline", (f"{why} — motion not checked; this fix itself is current. "
                               f"Re-run once AIS posts a newer fix (≥{MIN_GAP_MIN} min later)")
    f, t = prior
    mins = (now_t - t).total_seconds() / 60
    dist = _nm(f["lat"], f["lon"], lat, lon)
    smg = dist / (mins / 60) if mins else 0.0
    code = data.get("nav_status_code")
    status = data.get("nav_status") or "status unknown"
    span = f"{dist:.2f} nm in {mins/60:.1f} h" if mins >= 90 else f"{dist:.2f} nm in {mins:.0f} min"
    swing = ""
    if f.get("heading") is not None and data.get("heading") is not None:
        dh = abs((data["heading"] - f["heading"] + 180) % 360 - 180)
        if dh >= 20 and dist < STILL_NM and (code in STATIONARY_STATUS or mins >= STILL_GAP_MIN):
            swing = f", heading swung {dh:.0f}° (swinging on the hook)"
    # #63: the same SOG, COG and heading on two different fixes, with far less ground covered than that
    # SOG implies, is the provider repeating old kinematics. Holds at any gap >= MIN_GAP_MIN (10/23: 25 min).
    sog = data.get("sog_kt")
    if (sog and smg < 0.2 * sog
            and all(f.get(k) is not None and f.get(k) == data.get(k) for k in ("sog_kt", "cog", "heading"))):
        return ("status_lag_stationary",
                f"⚠️ AIS says **{status}** at {sog} kt, but the ship moved only {span} (~{smg:.1f} kt made good) and "
                f"SOG, COG and heading are identical to the previous fix → effectively STATIONARY (frozen AIS kinematics)")
    if dist < STILL_NM and code not in STATIONARY_STATUS and mins >= STILL_GAP_MIN:
        return ("status_lag_stationary",
                f"⚠️ AIS says **{status}**, but the ship moved only {span}{swing} → effectively STATIONARY "
                f"(at anchor / moored; mfphub status lag)")
    if dist > MOVED_NM and code in STATIONARY_STATUS:
        return ("status_lag_moving",
                f"⚠️ AIS says **{status}**, but the ship moved {span} (~{smg:.1f} kt made good) → UNDER WAY "
                f"(mfphub status lag)")
    return "agrees", f"agrees with {status}: {span}" + (f" (~{smg:.1f} kt made good)" if dist >= STILL_NM else "") + swing


def _get_json(path, params):
    req = urllib.request.Request(f"{NOMINATIM}/{path}?" + urllib.parse.urlencode(params), headers=UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def nearest_place(lat, lon):
    """Two Nominatim calls (1.1 s apart, per its usage policy): the locality from a reverse lookup,
    and the nearest named bay from a bounded search. Reverse alone returns only the borough at sea.
    Returns a sentence or None; never raises."""
    parts = []
    try:
        rev = _get_json("reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14, "addressdetails": 1})
        addr = rev.get("address") or {}
        town = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet")
        area = town or addr.get("county") or addr.get("state")
        if area:
            parts.append(f"in {town}" if town else f"in {area}")
    except Exception:
        pass
    try:
        time.sleep(1.1)
        vb = f"{lon - 0.35},{lat + 0.2},{lon + 0.35},{lat - 0.2}"
        # limit 40 (Nominatim's max): at 10 the nearest bay can fall outside the importance-ranked top
        # hits, so the same fix read "Saint Herman Bay 0.4 nm" on one call and "Cook Bay 5.4 nm" on the next.
        hits = _get_json("search", {"q": "bay", "viewbox": vb, "bounded": 1, "format": "jsonv2", "limit": 40})
        bays = sorted((_nm(lat, lon, float(h["lat"]), float(h["lon"])), h.get("name")) for h in hits if h.get("name"))
        if bays:
            d, name = bays[0]
            parts.append(f"nearest named bay **{name}** ({d:.1f} nm from its charted centre)")
    except Exception:
        pass
    return " · ".join(parts) or None


def publish_hub(data, verdict, place):
    """Write the fix to the hub's dash/ship feed so every agent (and the dashboard) sees where the ship is
    (user, 2026-10-15). Never breaks the script."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("hub_hook", Path(__file__).resolve().parent / "hub-hook.py")
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        # The motion check overrides mfphub's lagging nav status; say so, so the title doesn't read as a contradiction.
        status = {"status_lag_stationary": f"stationary per motion check (AIS status lagging: {data.get('nav_status')})",
                  "status_lag_moving": f"under way per motion check (AIS status lagging: {data.get('nav_status')})"}.get(
            verdict, data.get("nav_status") or "status unknown")
        lat, lon = data["lat"], data["lon"]
        bits = [f"{lat:.4f}, {lon:.4f}"]
        if verdict != "status_lag_stationary":
            bits += [f"{k} {data[v]}{u}" for k, v, u in (("SOG", "sog_kt", " kt"), ("COG", "cog", "°")) if data.get(v) is not None]
        if place:
            bits.append(place.replace("**", ""))
        row = {"title": f"Fairweather: {status}", "status": f"AIS {data.get('utc', '?')}", "detail": " · ".join(bits),
               "url": f"https://www.google.com/maps?q={lat},{lon}", "source": "life-manager (ship-position.py)", "flag": False}
        hook.rpc("memory_set", key="dash/ship", value=json.dumps({"items": [row]}, ensure_ascii=False))
    except Exception as e:
        print(f"(hub dash/ship not updated: {e})", file=sys.stderr)


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
    ap.add_argument("--no-place", action="store_true", help="skip the nearest-named-place lookup (Nominatim)")
    ap.add_argument("--no-hub", action="store_true", help="don't publish the fix to the hub's dash/ship feed")
    ap.add_argument("--self-test", action="store_true", help="run the motion-check self-test and exit")
    args = ap.parse_args()

    if args.self_test:  # #63: the 10/23 case, plus a real 7 kt transit that must not be flagged
        prev = {"utc": "2026-10-23T20:00:00Z", "lat": 60.0, "lon": -146.0, "heading": 298, "sog_kt": 7, "cog": 84,
                "nav_status_code": 0, "nav_status": "under way"}
        frozen = dict(prev, utc="2026-10-23T20:17:00Z", lat=60.001)  # 0.06 nm in 17 min (< STILL_GAP_MIN), same SOG/COG/heading
        moving = dict(prev, utc="2026-10-23T20:17:00Z", lat=60.033)  # ~2 nm in 17 min = 7 kt made good
        assert motion_check(frozen, [prev])[0] == "status_lag_stationary", motion_check(frozen, [prev])
        assert "frozen AIS kinematics" in motion_check(frozen, [prev])[1]
        assert motion_check(moving, [prev])[0] == "agrees", motion_check(moving, [prev])
        old_row = {k: v for k, v in prev.items() if k not in ("sog_kt", "cog")}  # cache rows written before #63
        assert motion_check(frozen, [old_row])[0] == "agrees", motion_check(frozen, [old_row])
        print("self-test OK")
        return

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

    # Motion cross-check against the fix cache (#59). Computed before caching this fix.
    try:
        prior_fixes = json.loads(FIX_CACHE.read_text()) if FIX_CACHE.exists() else []
    except Exception:
        prior_fixes = []
    verdict, motion = motion_check(data, prior_fixes)
    cache_fix(data)

    if args.latlon:
        if not args.no_hub:
            publish_hub(data, verdict, None)
        print(f"{data['lat']} {data['lon']}")
        return

    place = None if args.no_place else nearest_place(data["lat"], data["lon"])
    if not args.no_hub:
        publish_hub(data, verdict, place)
    h, agetxt = age_str(data.get("utc", ""))
    stale = " ⚠️STALE" if (h is not None and h > args.max_age_h) else ""
    if args.json:
        data["_age_hours"] = round(h, 2) if h is not None else None
        data["_stale"] = bool(stale)
        data["_motion"] = {"verdict": verdict, "text": motion}
        data["_place"] = place
        print(json.dumps(data, indent=2))
        return

    lat, lon = data.get("lat"), data.get("lon")
    bits = []
    if data.get("nav_status"):
        bits.append(data["nav_status"])  # AIS nav status (mfphub) — the reliable movement signal
    for k, label, unit in (("sog_kt", "SOG", " kt"), ("cog", "COG", "°"), ("heading", "hdg", "°"),
                           ("wtmp", "water", ""), ("atmp", "air", ""), ("baro", "baro", ""), ("wspd", "wind", "")):
        if data.get(k) is not None:
            bits.append(f"{label} {data[k]}{unit}")
    src = data.get("_source", "relay (own GPS)")
    print(f"### Ship position (via {src})")
    print(f"- **{lat}, {lon}** — obs {agetxt}{stale}" + (f" · {' · '.join(bits)}" if bits else ""))
    if place:
        print(f"- near: {place}")
    print(f"- motion check: {motion}")
    print(f"- feed to marine forecast: `nws_get_marine_forecast(lat={lat}, lon={lon})`")
    if stale:
        print(f"- _latest fix older than {args.max_age_h}h — pusher may be down / ship off-network; "
              f"fall back to the schedule-leg point._")


if __name__ == "__main__":
    main()
