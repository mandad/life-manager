#!/usr/bin/env python3
"""Bering / Chukchi / Beaufort sea-ice outlook for the /daily ship-weather pull.

Source: NWS Alaska Sea Ice Program (ASIP) "Ice Forecast" text product (WMO FZAK80 / AWIPS
ICEAFC, issued by PAFC ~2x weekly), pulled no-auth from api.weather.gov:
    /products/types/ICE/locations/AFC  ->  newest product id  ->  /products/{id}

The product covers the whole Alaska ice domain; this script reduces it to the part that
matters at ONE point:
  * the marine zone(s) containing the point (api.weather.gov /zones?type=marine&point=)
    and that zone group's ice status line from the product ("Ice free" / "Ice covered"),
  * distance + bearing to the nearest main ice edge, computed from the lat/lon vertex list
    ASIP writes into the product ("...main ice edge extends from 66 3'N 169 25'W to ..."),
  * the basin's DAYS 1-5 ice forecast paragraph.

Ice-edge geometry comes from the text product, not from the U.S. National Ice Center ArcGIS
services — gis.usicecenter.gov does not answer over plain HTTPS from here (connection fails,
no HTTP status), so the text vertices are the only reliable no-auth machine-readable edge.

In late summer the Bering is ice-free and the edge sits far north in the Chukchi; the block
stays useful by reporting the edge distance/bearing and its southernmost latitude anyway.

With no --lat/--lon (or with --from-ship) the point defaults to the live ship AIS position via
ship-position.py, same as aurora-forecast.py / synoptic-obs.py.

Usage:
    python3 scripts/sea-ice-outlook.py                                   # at the live ship pos
    python3 scripts/sea-ice-outlook.py --lat 64.5 --lon -165.4 --label Nome
    python3 scripts/sea-ice-outlook.py --lat 71.0 --lon -165.0 --label "PARS box"
    python3 scripts/sea-ice-outlook.py --lat 71.0 --lon -165.0 --synopsis
    python3 scripts/sea-ice-outlook.py --lat 64.5 --lon -165.4 --json
Longitude is negative west. Any single source failing degrades to a note line; exit code 0.
"""
import argparse
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "sea-ice-outlook/1.0 (NOAA Ship Fairweather /daily; accounts@dmanda.com)"
API = "https://api.weather.gov"
R_NM = 3440.065  # earth radius, nautical miles

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

PROBE_BUDGET_S = 15.0   # total wall-clock ceiling for the offshore zone probe
STATUS_MAX_CHARS = 120  # a zone status is a short sentence; guard the /daily line against blobs

# lines like "PKZ505-Central US Arctic Offshore-" (product has a "PZK741" typo in the wild)
ZONE_RE = re.compile(r"^(?:PKZ|PZK)(\d{3})\s*-\s*(.*?)\s*-?\s*$")
SECTION_RE = re.compile(r"^-\s*([A-Z][A-Z .,'/&-]*[A-Z])\s*-\s*$")
FCST_RE = re.compile(r"^FORECAST FOR\s+(?:THE\s+)?(.+?)\s*\(", re.I)
# "66 3'N 169 25'W", "66-03N 169-25W", "66 03 N 169 25 W"
DMS_RE = re.compile(r"(\d{1,3})[\s\-]+(\d{1,2})\s*'?\s*([NS])[\s,]+(\d{1,3})[\s\-]+(\d{1,2})\s*'?\s*([EW])")
DEC_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*([NS])[\s,]+(\d{1,3}(?:\.\d+)?)\s*([EW])")

# validity-date parsing: explicit month-name map + regex, NOT strptime("%A %d %B %Y") --
# %A/%B are locale-dependent and break if NWS ever drops the weekday. Live shape of the
# 'valid' field from parse_product(): "Saturday 8 August 2026", "Wednesday 5 August 2026".
MONTH_MAP = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}
VALID_DATE_RE = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})")
WEEKDAY_RE = re.compile(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*$")
AK_TZ = ZoneInfo("America/Anchorage")


def get(url, timeout=30, accept="application/geo+json"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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


# ---------------------------------------------------------------- geo helpers
def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    return COMPASS[int((deg + 11.25) % 360 // 22.5)]


def dm(val, pos, neg):
    h = pos if val >= 0 else neg
    v = abs(val)
    d = int(v)
    m = round((v - d) * 60, 1)
    if m >= 60.0:      # rounding carry: 64.99999 -> 65°00.0', not 64°60.0'
        d += 1
        m = 0.0
    return f"{d}°{m:04.1f}'{h}"


def fmt_pt(lat, lon):
    return f"{dm(lat, 'N', 'S')} {dm(lon, 'E', 'W')}"


def nearest_on_polyline(lat, lon, pts, steps=200):
    """Min great-circle distance from (lat,lon) to a polyline, by densifying each segment.
    Returns (distance_nm, near_lat, near_lon)."""
    best = None
    for i, (la, lo) in enumerate(pts):
        d = haversine_nm(lat, lon, la, lo)
        if best is None or d < best[0]:
            best = (d, la, lo)
        if i + 1 < len(pts):
            la2, lo2 = pts[i + 1]
            dlo = lo2 - lo
            if dlo > 180:
                dlo -= 360
            elif dlo < -180:
                dlo += 360
            for s in range(1, steps):
                t = s / steps
                sla = la + (la2 - la) * t
                slo = ((lo + dlo * t + 180) % 360) - 180
                d = haversine_nm(lat, lon, sla, slo)
                if d < best[0]:
                    best = (d, sla, slo)
    return best


def basin_from_point(lat, lon):
    """Coarse basin for a point when the marine-zone lookup can't place it.
    Returns None outside the ASIP ice domain (Gulf of Alaska, SE Alaska, Kodiak, Ketchikan...)."""
    if 58.0 <= lat <= 61.6 and -155.0 <= lon <= -149.0:
        return "COOK INLET"
    if lat >= 68.0 and -157.0 < lon <= -120.0:
        return "BEAUFORT SEA"
    # west of the Alaska mainland (or across the dateline); everything east of ~157W at these
    # latitudes is the Gulf of Alaska / Inside Passage, which this product does not cover.
    west = lon <= -157.0 or lon >= 160.0
    if lat >= 65.8 and west:
        return "CHUKCHI SEA"
    if lat >= 51.0 and west:
        return "BERING SEA"
    return None


# ------------------------------------------------------------ product parsing
def latest_product(office, timeout):
    lst = get(f"{API}/products/types/ICE/locations/{office}", timeout, "application/ld+json")
    graph = lst.get("@graph") or []
    if not graph:
        raise RuntimeError(f"no ICE products listed for {office}")
    graph.sort(key=lambda p: p.get("issuanceTime") or "", reverse=True)
    newest = graph[0]
    return get(newest["@id"], timeout, "application/ld+json")


def paragraphs(text):
    out, buf = [], []
    for ln in text.splitlines():
        if ln.strip():
            buf.append(ln.strip())
        elif buf:
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return out


def parse_product(text):
    """-> {'zones': {id: [{...}, ...]}, 'basin_forecasts': {BASIN: para}, 'edges': [...],
           'valid': str, 'confidence': str, 'synopsis': str}

    Zone groups read: zone id lines -> blank -> status sentence -> blank. A zone id can be
    listed under two basin sections with different statuses (PKZ505/813/859 sit in both the
    Beaufort and Chukchi blocks), so each id maps to a LIST of entries, one per section."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    zones, basin = {}, None
    pending = []           # zone ids awaiting their status text
    pending_names = {}
    status_buf = []

    def flush():
        if pending and status_buf:
            st = " ".join(status_buf).strip()
            for zid in pending:
                zones.setdefault(zid, []).append(
                    {"id": zid, "name": pending_names.get(zid, ""), "basin": basin, "status": st})
        pending.clear()
        status_buf.clear()

    for ln in lines:
        s = ln.strip()
        if not s:
            # a blank line closes a group's status; without this the last group of a section
            # keeps swallowing the ice-edge paragraphs that follow it
            if pending and status_buf:
                flush()
            continue
        m = SECTION_RE.match(s)
        if m:
            flush()
            basin = m.group(1).strip()
            continue
        if FCST_RE.match(s) or s.startswith(("SYNOPSIS", "ANALYSIS CONFIDENCE", "FORECAST VALID")):
            flush()
            continue
        zm = ZONE_RE.match(s)
        if zm:
            if status_buf:            # a new group started after a status -> close previous
                flush()
            zid = "PKZ" + zm.group(1)
            pending.append(zid)
            pending_names[zid] = zm.group(2)
            continue
        if pending:
            status_buf.append(s)
    flush()

    paras = paragraphs(text)
    basin_forecasts, edges = {}, []
    valid = confidence = synopsis = ""
    for p in paras:
        fm = FCST_RE.match(p)
        if fm:
            key = fm.group(1).strip().upper()
            basin_forecasts[key] = p
            continue
        if p.upper().startswith("FORECAST VALID"):
            valid = p[len("FORECAST VALID"):].strip(" .:")
        elif p.upper().startswith("ANALYSIS CONFIDENCE"):
            confidence = p[len("ANALYSIS CONFIDENCE"):].strip(" .:")
        elif p.upper().startswith("SYNOPSIS"):
            synopsis = p[len("SYNOPSIS"):].strip(" .:")
        if "ice edge" in p.lower():
            pts = []
            for g in DMS_RE.finditer(p):
                d1, m1, h1, d2, m2, h2 = g.groups()
                la = int(d1) + int(m1) / 60.0
                lo = int(d2) + int(m2) / 60.0
                pts.append((la if h1 == "N" else -la, lo if h2 == "E" else -lo))
            if not pts:
                for g in DEC_RE.finditer(p):
                    v1, h1, v2, h2 = g.groups()
                    la, lo = float(v1), float(v2)
                    pts.append((la if h1 == "N" else -la, lo if h2 == "E" else -lo))
            edges.append({"text": p, "points": pts})
    return {"zones": zones, "basin_forecasts": basin_forecasts, "edges": edges,
            "valid": valid, "confidence": confidence, "synopsis": synopsis}


def basin_forecast(parsed, basin):
    if not basin:
        return ""
    key = basin.upper()
    for k, v in parsed["basin_forecasts"].items():
        if k == key or key.split()[0] in k:
            return v
    return ""


# ------------------------------------------------------------- zone lookup
def zones_at(lat, lon, timeout):
    d = get(f"{API}/zones?type=marine&point={lat:.4f},{lon:.4f}", timeout)
    out = []
    for f in d.get("features", []):
        p = f.get("properties", {})
        if p.get("id"):
            out.append((p["id"], p.get("name", "")))
    return out


def locate_zones(lat, lon, timeout, probe=True, budget=PROBE_BUDGET_S):
    """Zones containing the point; if it's over land, probe a ring offshore.
    The ring is up to 16 sequential lookups, so cap the whole probe at `budget` seconds —
    a degraded api.weather.gov must not stall the /daily block for minutes.
    -> (zones, note) where note describes any offset used."""
    try:
        z = zones_at(lat, lon, timeout)
    except Exception as e:
        return [], f"marine-zone lookup failed ({type(e).__name__})"
    if z or not probe:
        return z, ""
    deadline = time.monotonic() + budget
    for d_nm in (10, 25):
        for br in range(0, 360, 45):
            if time.monotonic() >= deadline:
                return [], f"offshore zone probe gave up after {budget:.0f}s (weather.gov slow)"
            rb = math.radians(br)
            pla = lat + (d_nm / 60.0) * math.cos(rb)
            plo = lon + (d_nm / 60.0) * math.sin(rb) / max(0.05, math.cos(math.radians(lat)))
            try:
                z = zones_at(pla, plo, min(timeout, max(1.0, deadline - time.monotonic())))
            except Exception:
                continue
            if z:
                return z, f"point is over land/inshore; nearest marine zone found {d_nm} nm {compass(br)}"
    return [], "no marine zone within 25 nm (point appears to be inland)"


def pick_zone(zones, parsed, basin_hint):
    """Choose the product entry for the located zones, preferring the hinted basin.
    -> (entry|None, [other entries for the same zone whose status differs])"""
    cands = []
    for zid, _ in zones:
        cands.extend(parsed["zones"].get(zid, []))
    if not cands:
        return None, []
    chosen = cands[0]
    if basin_hint:
        for c in cands:
            if c.get("basin") and c["basin"].upper() == basin_hint.upper():
                chosen = c
                break
    others = [c for c in cands
              if c is not chosen and c["id"] == chosen["id"] and c["status"] != chosen["status"]]
    return chosen, others


def iso_z(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def parse_valid_end(raw):
    """Parse the ASIP 'valid' field's END date -- e.g. "Wednesday 5 August 2026" or a
    "... through Saturday 8 August 2026" range, where the LAST date in the string wins.
    Explicit month-name map + regex (see VALID_DATE_RE), not strptime("%A %d %B %Y").
    -> (end_date | None, multi_matched: bool, weekday_note: str | None)
    weekday_note is a tripwire, not a gate: if the stated weekday disagrees with the
    parsed date's actual weekday, the numeric date is still used but flagged."""
    if not raw:
        return None, False, None
    matches = list(VALID_DATE_RE.finditer(raw))
    if not matches:
        return None, False, None
    multi = len(matches) > 1
    m = matches[-1]
    d, mon, y = m.groups()
    try:
        end = date(int(y), MONTH_MAP[mon], int(d))
    except (KeyError, ValueError):
        return None, multi, None
    weekday_note = None
    wm = WEEKDAY_RE.search(raw[:m.start()])
    if wm:
        stated, actual = wm.group(1), end.strftime("%A")
        if stated != actual:
            weekday_note = (f"stated weekday \"{stated}\" does not match parsed date "
                            f"{end.isoformat()}'s actual weekday ({actual}) -- possible misparse")
    return end, multi, weekday_note


def _parse_iso_date(s):
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--needed-through must be YYYY-MM-DD, got {s!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, help="latitude (default: live ship position)")
    ap.add_argument("--lon", type=float, help="longitude (negative west)")
    ap.add_argument("--from-ship", action="store_true",
                    help="locate at the live ship AIS position (mfphub); also the default with no --lat/--lon")
    ap.add_argument("--label", default="", help="header label, e.g. 'Nome' or 'PARS box'")
    ap.add_argument("--office", default="AFC", help="issuing office for the ICE product (default AFC/Anchorage)")
    ap.add_argument("--timeout", type=float, default=30, help="per-request timeout seconds (default 30)")
    ap.add_argument("--max-age-h", type=float, default=96,
                    help="flag the product stale beyond this many hours (default 96; ASIP issues ~2x/week)")
    ap.add_argument("--needed-through", type=_parse_iso_date, default=None,
                    help="YYYY-MM-DD: flag when the product's stated validity lapses before this "
                         "Alaska-local date (America/Anchorage). No default -- only checked when "
                         "explicitly passed, e.g. the next departure/commitment date.")
    ap.add_argument("--synopsis", action="store_true", help="also print the product SYNOPSIS paragraph")
    ap.add_argument("--no-zone-probe", action="store_true",
                    help="don't search offshore when the point itself is over land")
    ap.add_argument("--probe-budget", type=float, default=PROBE_BUDGET_S,
                    help=f"total seconds allowed for the offshore zone probe (default {PROBE_BUDGET_S:.0f})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.from_ship or args.lat is None or args.lon is None:
        args.lat, args.lon = ship_latlon()
        if not args.label:
            args.label = "live ship pos"

    lat, lon = args.lat, args.lon
    loc = f"{lat:.3f},{lon:.3f}" + (f" ({args.label})" if args.label else "")
    notes = []
    needed_through = args.needed_through  # date | None -- no default, checked only if passed

    # --- source 1: the ASIP product (hard requirement; degrade gracefully) --------
    prod = None
    try:
        prod = latest_product(args.office, args.timeout)
    except Exception as e:
        notes.append(f"ASIP Ice Forecast unavailable ({type(e).__name__}: {e})")

    if prod is None:
        if needed_through:
            notes.append(f"coverage through {needed_through.isoformat()} could not be assessed "
                         f"— no product available this run")
        if args.json:
            print(json.dumps({"location": [lat, lon], "label": args.label,
                              "available": False, "notes": notes,
                              "needed_through": needed_through.isoformat() if needed_through else None,
                              "valid_end": None, "valid_raw": None, "covers": None,
                              "margin_days": None}, indent=2))
        else:
            print("### Sea ice (NWS Alaska Sea Ice Program)")
            print(f"- _no outlook this run — {notes[0]}_")
            for n in notes[1:]:
                print(f"- _note: {n}_")
        return

    parsed = parse_product(prod.get("productText", "") or "")
    issued = iso_z(prod.get("issuanceTime", "") or "")
    age_h = (datetime.now(timezone.utc) - issued).total_seconds() / 3600 if issued else None
    stale = age_h is not None and age_h > args.max_age_h
    if stale:
        notes.append(f"product is {age_h:.0f}h old (>{args.max_age_h:.0f}h)")

    # --- validity-coverage check: issuance age and decision-relevance are different
    # questions. A product can sit under the staleness threshold and still have expired
    # coverage for a date you actually care about. Dates compared in America/Anchorage
    # (zoneinfo), not UTC -- validity covers *through end of* that Alaska local date.
    valid_end, valid_multi, weekday_note = parse_valid_end(parsed["valid"])
    if valid_multi:
        notes.append("valid field had more than one date; used the last (end) date")
    if weekday_note:
        notes.append(weekday_note)

    today_ak = datetime.now(AK_TZ).date()
    covers = None          # True/False once compared against --needed-through, else None
    margin_days = None
    coverage_lines = []    # loud, standalone bullets -- printed apart from generic notes

    if valid_end is None:
        if needed_through:
            raw_disp = parsed["valid"] or "<no 'valid' field found in product>"
            coverage_lines.append(
                f'- ⚠️ validity date unparsed ("{raw_disp}") — cannot check coverage through '
                f'{needed_through.isoformat()}; age-based staleness check still active')
    else:
        # free companion check, unconditional -- no --needed-through required
        if today_ak > valid_end:
            coverage_lines.append(
                f"- ⚠️ **VALIDITY LAPSED — product valid through {valid_end.isoformat()}; "
                f"today is {today_ak.isoformat()} (Alaska time).** Reissue is overdue; "
                f"re-pull or go direct to USNIC before relying on this again.")
        if needed_through:
            margin_days = (valid_end - needed_through).days
            covers = margin_days >= 0
            if covers:
                margin_txt = ("covers with NO margin" if margin_days == 0
                              else f"+{margin_days} day{'s' if margin_days != 1 else ''} margin")
                coverage_lines.append(
                    f"- validity covers {needed_through.isoformat()} "
                    f"(ends {valid_end.isoformat()}, {margin_txt})")
            else:
                short = -margin_days
                coverage_lines.append(
                    f"- ⚠️ **VALIDITY GAP — product valid through {valid_end.isoformat()}; needed "
                    f"through {needed_through.isoformat()} ({short} day{'s' if short != 1 else ''} "
                    f"short).** The current answer expires before your commitment; re-pull or go "
                    f"direct to USNIC before departure.")

    # --- source 2: marine zone containing the point (optional) -------------------
    zones, zone_note = locate_zones(lat, lon, min(args.timeout, 20), probe=not args.no_zone_probe,
                                    budget=args.probe_budget)
    if zone_note:
        notes.append(zone_note)
    hint = basin_from_point(lat, lon)
    entry, alt = pick_zone(zones, parsed, hint)
    basin = (entry or {}).get("basin") or hint
    if zones and not entry:
        notes.append("zone found but not named in this product issuance")
    if not basin:
        notes.append("point is outside the ASIP ice domain (Bering / Chukchi / Beaufort / Cook Inlet)")
    for a in alt:
        notes.append(f"{a['id']} is also listed under {a['basin'].title()}: {a['status'].rstrip('.')}")

    # --- ice edge geometry from the product text --------------------------------
    nearest = None
    all_pts = []
    for e in parsed["edges"]:
        if len(e["points"]) < 1:
            continue
        all_pts.extend(e["points"])
        cand = nearest_on_polyline(lat, lon, e["points"])
        if cand and (nearest is None or cand[0] < nearest[0]):
            nearest = cand
    southernmost = min(all_pts, key=lambda p: p[0]) if all_pts else None
    if not all_pts:
        notes.append("no numeric ice-edge vertices in this issuance (landmark wording only)")

    fcst = basin_forecast(parsed, basin)
    status = (entry or {}).get("status", "")
    ice_covered = "ice covered" in status.lower()

    if args.json:
        print(json.dumps({
            "location": [lat, lon], "label": args.label, "available": True,
            "product": {"office": args.office, "code": "ICE",
                        "issued": issued.strftime("%Y-%m-%dT%H:%MZ") if issued else None,
                        "age_hours": round(age_h, 1) if age_h is not None else None,
                        "stale": stale, "valid": parsed["valid"],
                        "confidence": parsed["confidence"]},
            "basin": basin,
            "zones_at_point": [{"id": z, "name": n} for z, n in zones],
            "zone_status": {"id": entry["id"], "name": entry["name"], "basin": entry["basin"],
                            "status": status} if entry else None,
            "zone_status_other_basins": [{"id": a["id"], "basin": a["basin"], "status": a["status"]}
                                         for a in alt],
            "ice_covered": ice_covered if entry else None,
            "ice_edge": {
                "vertices": len(all_pts),
                "nearest": {"distance_nm": round(nearest[0], 1),
                            "bearing_deg": round(bearing_deg(lat, lon, nearest[1], nearest[2])),
                            "compass": compass(bearing_deg(lat, lon, nearest[1], nearest[2])),
                            "lat": round(nearest[1], 4), "lon": round(nearest[2], 4)} if nearest else None,
                "southernmost": {"lat": round(southernmost[0], 4), "lon": round(southernmost[1], 4)}
                if southernmost else None,
            },
            "basin_forecast": fcst,
            "synopsis": parsed["synopsis"],
            "needed_through": needed_through.isoformat() if needed_through else None,
            "valid_end": valid_end.isoformat() if valid_end else None,
            "valid_raw": parsed["valid"] or None,
            "covers": covers,
            "margin_days": margin_days,
            "notes": notes,
        }, indent=2))
        return

    # ------------------------------------------------------------------ markdown
    head = f"### Sea ice — {basin.title() if basin else 'Alaska'} (NWS Alaska Sea Ice Program)"
    print(head)
    meta = []
    if issued:
        meta.append("issued " + issued.strftime("%Y-%m-%d %H:%MZ") + (f" ⚠️ {age_h:.0f}h old" if stale else ""))
    if parsed["valid"]:
        # raw + parsed together, always -- if the parse is ever wrong, the agent reading
        # this output catches it (last line of defense; see parse_valid_end()).
        meta.append("valid " + parsed["valid"] + (f" (→ {valid_end.isoformat()})" if valid_end else ""))
    if parsed["confidence"]:
        meta.append("confidence " + parsed["confidence"].rstrip("."))
    print(f"_{loc} · " + " · ".join(meta) + "_" if meta else f"_{loc}_")
    print()

    for cl in coverage_lines:
        print(cl)

    if entry:
        flag = "🧊 " if ice_covered else ""
        shown = status.rstrip(".")
        if len(shown) > STATUS_MAX_CHARS:   # never let a parse surprise dump a blob on the zone line
            shown = shown[:STATUS_MAX_CHARS - 1].rstrip() + "…"
        print(f"- zone **{entry['id']}** {entry['name']}: {flag}**{shown}**")
    elif zones:
        print("- zone " + ", ".join(f"**{z}** {n}" for z, n in zones) + ": _not addressed in this issuance_")
    else:
        print("- _no marine zone resolved for this point_")

    if nearest:
        d, nla, nlo = nearest
        brg = bearing_deg(lat, lon, nla, nlo)
        if ice_covered:
            print(f"- main ice edge **{d:.0f} nm {compass(brg)}** ({brg:03.0f}°) at {fmt_pt(nla, nlo)} "
                  f"— you are inside the ice-covered zone")
        else:
            lead = "ice-free at the point; nearest" if entry else "nearest"
            print(f"- {lead} main ice edge **{d:.0f} nm {compass(brg)}** "
                  f"({brg:03.0f}°) at {fmt_pt(nla, nlo)}")
        dup = (abs(southernmost[0] - nla) < 0.02 and abs(southernmost[1] - nlo) < 0.02) if southernmost else True
        if southernmost and not dup:
            print(f"- edge southern limit **{dm(southernmost[0], 'N', 'S')}** "
                  f"({dm(southernmost[1], 'E', 'W')}) · {len(all_pts)} edge vertices in product")
        elif southernmost:
            print(f"- that is the edge's southern limit · {len(all_pts)} edge vertices in product")
    else:
        for e in parsed["edges"]:
            if e["text"]:
                print("- edge (text): " + e["text"][:300] + ("…" if len(e["text"]) > 300 else ""))
                break

    if fcst:
        body = re.sub(r"^FORECAST FOR\s+(?:THE\s+)?.*?\)\s*\.*\s*", "", fcst, flags=re.I)
        print(f"- 5-day: {body or fcst}")
    if args.synopsis and parsed["synopsis"]:
        print(f"- synopsis: {parsed['synopsis']}")
    for n in notes:
        print(f"- _note: {n}_")


if __name__ == "__main__":
    main()
