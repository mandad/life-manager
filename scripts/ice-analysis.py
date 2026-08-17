#!/usr/bin/env python3
"""ASIP DAILY ice analysis at a point — the map, not the written forecast (automation #35/#39).

WHY THIS EXISTS
    `sea-ice-outlook.py` (#26) parses the NWS Alaska Sea Ice Program *text* product, which
    reissues roughly twice a week. The user said plainly (Q39, 2026-08-03) that the text
    product was never the ship's whole ice picture — he reads USNIC reports, PolarView, and
    "the map produced by the Alaska Sea Ice Program, which updates daily, as opposed to the
    written forecast."

    That map turns out to be published as a **shapefile**, not just a JPG:
        https://www.weather.gov/source/afc/icedata/full_latest.zip   (daily analysis)
        https://www.weather.gov/source/afc/icedata/forecast_latest.zip
    The archive member carries its own date (`full_260815.shp`), the polygons carry the
    **WMO egg code** in the DBF (CT = total concentration in tenths), and on 2026-08-15 the
    analysis was ~24 h fresher than the text product the daily pull had been quoting. So this
    reads the same product the ship reads, at the cadence the ship reads it.

WHAT IT REPORTS
    - ice concentration AT the point (decoded from the egg code, not guessed)
    - nearest ice at or above a concentration threshold, in nm and true bearing
    - the analysis date, so a stale text product is visible as stale next to a fresh map

DESIGN NOTES
    - **stdlib only**, like every other script here. The shapefile reader (~90 lines) and the
      polar-stereographic projection are implemented inline; there is no geopandas/pyproj.
    - The shapefile CRS is WGS_1984_Stereographic_North_Pole, central meridian 180,
      standard parallel 60N, metres. Point-in-polygon is done in PROJECTED space so the
      antimeridian is a non-issue (the Chukchi polygons straddle it); distance and bearing are
      computed GEODETICALLY after inverse-projecting, so the numbers match `sea-ice-outlook.py`.
    - Any failure degrades to a single note line and **exit 0** — an ice source being down must
      never take out the /daily ship block.

USAGE
    python3 scripts/ice-analysis.py --from-ship --label "Nome inport"
    python3 scripts/ice-analysis.py --lat 71.4 --lon -160.0 --json
    python3 scripts/ice-analysis.py --from-ship --threshold 7      # nearest ice >= 7/10
    python3 scripts/ice-analysis.py --from-ship --no-usnic         # skip the weekly synopsis
"""

import argparse
import io
import json
import math
import re
import struct
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ANALYSIS_URL = "https://www.weather.gov/source/afc/icedata/full_latest.zip"
FORECAST_URL = "https://www.weather.gov/source/afc/icedata/forecast_latest.zip"
USNIC_SYNOPSIS_URL = "https://usicecenter.gov/Products/ArcticSynopsis"
UA = "llm-land-ice/1.0 (NOAA Ship Fairweather daily pull; accounts@dmanda.com)"

# Shapefile CRS: PROJCS["WGS_1984_Stereographic_North_Pole", ... Central_Meridian 180,
# Standard_Parallel_1 60, WGS84 spheroid, metres]. Read from the .prj at runtime and only
# fall back to these if parsing fails.
DEF_CENTRAL_MERIDIAN = 180.0
DEF_STANDARD_PARALLEL = 60.0
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563

NM_PER_M = 1 / 1852.0


# --------------------------------------------------------------- WMO egg code
def decode_ct(ct):
    """SIGRID-3 / WMO total-concentration code -> (tenths, human label).

    Returns tenths as a float so thresholds compare cleanly; None when the code carries no
    concentration (missing, or a land/no-data polygon). Deliberately does NOT guess: an
    unrecognised code comes back as (None, raw) rather than being coerced to a number."""
    ct = (ct or "").strip()
    table = {
        "00": (0.0, "ice free"),
        "01": (0.5, "open water (<1/10)"),
        "02": (0.5, "bergy water"),
        "91": (9.5, "9+/10"),
        "92": (10.0, "10/10 (consolidated)"),
    }
    if ct in table:
        return table[ct]
    if re.fullmatch(r"\d{2}", ct):
        # 10 -> 1/10, 20 -> 2/10 ... 90 -> 9/10; also handles ranges like 13 (1-3/10) by
        # taking the first digit as the low end, which is how the egg code reads.
        lo = int(ct[0])
        hi = int(ct[1])
        if hi and hi != 0:
            return (float(lo), f"{lo}-{hi}/10")
        return (float(lo), f"{lo}/10")
    return (None, ct or "(no code)")


# --------------------------------------------------------------- projection
class PolarStereo:
    """Ellipsoidal polar stereographic (north), standard-parallel form. EPSG method 9829.

    Only the two directions this script needs: forward for the ship point, inverse for the
    nearest polygon vertex so distance/bearing can be computed geodetically."""

    def __init__(self, lon0=DEF_CENTRAL_MERIDIAN, lat_ts=DEF_STANDARD_PARALLEL,
                 a=WGS84_A, f=WGS84_F):
        self.lon0 = math.radians(lon0)
        self.a = a
        self.e = math.sqrt(2 * f - f * f)
        phi_f = math.radians(lat_ts)
        e = self.e
        self.t_f = self._t(phi_f)
        self.m_f = math.cos(phi_f) / math.sqrt(1 - e * e * math.sin(phi_f) ** 2)

    def _t(self, phi):
        e = self.e
        s = math.sin(phi)
        return math.tan(math.pi / 4 - phi / 2) / (((1 - e * s) / (1 + e * s)) ** (e / 2))

    def forward(self, lat, lon):
        phi = math.radians(lat)
        theta = math.radians(lon) - self.lon0
        rho = self.a * self.m_f * self._t(phi) / self.t_f
        return rho * math.sin(theta), -rho * math.cos(theta)

    def inverse(self, x, y):
        rho = math.hypot(x, y)
        if rho == 0:
            return 90.0, math.degrees(self.lon0)
        t = rho * self.t_f / (self.a * self.m_f)
        chi = math.pi / 2 - 2 * math.atan(t)
        e = self.e
        phi = chi
        for _ in range(12):  # converges in ~4; cheap insurance
            s = math.sin(phi)
            new = math.pi / 2 - 2 * math.atan(
                t * (((1 - e * s) / (1 + e * s)) ** (e / 2)))
            if abs(new - phi) < 1e-12:
                phi = new
                break
            phi = new
        lon = self.lon0 + math.atan2(x, -y)
        return math.degrees(phi), (math.degrees(lon) + 540) % 360 - 180


def parse_prj(text):
    """Pull central meridian + standard parallel out of the .prj so a product change to the
    projection is picked up rather than silently mis-projecting."""
    def grab(name, default):
        m = re.search(rf'PARAMETER\["{name}",\s*(-?[\d.]+)\]', text, re.I)
        return float(m.group(1)) if m else default
    return grab("Central_Meridian", DEF_CENTRAL_MERIDIAN), \
        grab("Standard_Parallel_1", DEF_STANDARD_PARALLEL)


# --------------------------------------------------------------- geodesy
def haversine_nm(lat1, lon1, lat2, lon2):
    r_nm = 6371008.8 * NM_PER_M
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[int((deg + 11.25) % 360 / 22.5)]


# --------------------------------------------------------------- shapefile
def read_shp_polygons(buf):
    """Minimal ESRI shapefile reader for shape type 5 (Polygon).

    Returns [[ring, ring, ...], ...] where a ring is a list of (x, y). Non-polygon records are
    skipped rather than raising — a mixed file should degrade, not explode."""
    if struct.unpack(">i", buf[0:4])[0] != 9994:
        raise ValueError("not a shapefile (bad file code)")
    shapes = []
    off = 100
    n = len(buf)
    while off + 8 <= n:
        _, clen = struct.unpack(">ii", buf[off:off + 8])
        off += 8
        end = off + clen * 2
        if end > n:
            break
        stype = struct.unpack("<i", buf[off:off + 4])[0]
        if stype != 5:                      # 0 = null, others unsupported here
            shapes.append([])
            off = end
            continue
        nparts, npoints = struct.unpack("<ii", buf[off + 36:off + 44])
        p = off + 44
        parts = list(struct.unpack(f"<{nparts}i", buf[p:p + 4 * nparts]))
        p += 4 * nparts
        coords = struct.unpack(f"<{2 * npoints}d", buf[p:p + 16 * npoints])
        rings = []
        for i, start in enumerate(parts):
            stop = parts[i + 1] if i + 1 < nparts else npoints
            rings.append([(coords[2 * j], coords[2 * j + 1]) for j in range(start, stop)])
        shapes.append(rings)
        off = end
    return shapes


def read_dbf(buf):
    """Field values per record, as strings. Enough for the egg-code attributes."""
    _, hlen, rlen = struct.unpack("<IHH", buf[4:12])
    fields, off = [], 32
    while buf[off] != 0x0D:
        name = buf[off:off + 11].split(b"\0")[0].decode("latin-1")
        length = buf[off + 16]
        fields.append((name, length))
        off += 32
    out, start = [], hlen
    while start + rlen <= len(buf):
        rec = buf[start:start + rlen]
        if rec[:1] == b"*":                 # deleted record
            start += rlen
            continue
        vals, p = {}, 1
        for name, length in fields:
            vals[name] = rec[p:p + length].decode("latin-1").strip()
            p += length
        out.append(vals)
        start += rlen
    return out


def point_in_rings(px, py, rings):
    """Even-odd ray cast across all rings of a polygon (outer + holes)."""
    inside = False
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            if (y1 > py) != (y2 > py):
                xint = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
                if xint > px:
                    inside = not inside
    return inside


def nearest_point_on_rings(px, py, rings):
    """Closest point on any ring segment, in projected metres."""
    best = None
    for ring in rings:
        for i in range(len(ring)):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % len(ring)]
            dx, dy = x2 - x1, y2 - y1
            seg = dx * dx + dy * dy
            if seg == 0:
                qx, qy = x1, y1
            else:
                t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg))
                qx, qy = x1 + t * dx, y1 + t * dy
            d = (qx - px) ** 2 + (qy - py) ** 2
            if best is None or d < best[0]:
                best = (d, qx, qy)
    return best


# --------------------------------------------------------------- fetch
def fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Last-Modified", "")


def load_analysis(url, timeout):
    raw, last_mod = fetch(url, timeout)
    z = zipfile.ZipFile(io.BytesIO(raw))
    shp_name = next(n for n in z.namelist() if n.lower().endswith(".shp"))
    base = shp_name[:-4]
    prj = ""
    try:
        prj = z.read(base + ".prj").decode("utf-8", "replace")
    except KeyError:
        pass
    polys = read_shp_polygons(z.read(shp_name))
    attrs = read_dbf(z.read(base + ".dbf"))
    # the member name carries the analysis date: full_260815 -> 2026-08-15
    m = re.search(r"_(\d{2})(\d{2})(\d{2})$", base)
    analysis_date = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""
    return polys, attrs, prj, analysis_date, last_mod, base


def fetch_usnic(timeout):
    """USNIC Arctic Regional Synopsis. Updated WEEKLY on Fridays (stated on the page), so this
    is regional narrative rather than a daily source — kept per the user's call 2026-08-15."""
    try:
        raw, last_mod = fetch(USNIC_SYNOPSIS_URL, timeout)
    except Exception as e:
        return {"available": False, "note": f"{type(e).__name__}: {e}"}
    html = raw.decode("utf-8", "replace")
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    txt = " ".join(re.sub(r"<[^>]+>", " ", txt).split())
    return {"available": True, "last_modified": last_mod, "url": USNIC_SYNOPSIS_URL,
            "cadence": "weekly (Fridays)", "chars": len(txt)}


# --------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="ASIP daily ice analysis at a point.")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float, help="negative west")
    ap.add_argument("--from-ship", action="store_true",
                    help="locate via mfphub AIS (default when --lat/--lon omitted)")
    ap.add_argument("--label", default="", help="place label for the output header")
    ap.add_argument("--threshold", type=float, default=1.0,
                    help="also report nearest ice at or above this many tenths (default 1)")
    ap.add_argument("--no-usnic", action="store_true", help="skip the USNIC synopsis check")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    lat, lon = args.lat, args.lon
    if args.from_ship or lat is None or lon is None:
        script = Path(__file__).with_name("ship-position.py")
        try:
            out = subprocess.run([sys.executable, str(script), "--source", "mfphub", "--latlon"],
                                 capture_output=True, text=True, timeout=90)
        except Exception as e:
            print(f"ICE-ANALYSIS: could not locate ship ({type(e).__name__}: {e})")
            return 0
        if out.returncode != 0:
            print("ICE-ANALYSIS: ship-position.py failed: "
                  + (out.stderr.strip() or "unknown error"))
            return 0
        try:
            lat, lon = (float(v) for v in out.stdout.split())
        except Exception:
            print("ICE-ANALYSIS: unexpected position output: "
                  + (out.stdout.strip() or "<empty>"))
            return 0

    try:
        polys, attrs, prj, analysis_date, last_mod, base = load_analysis(ANALYSIS_URL,
                                                                        args.timeout)
    except Exception as e:
        print(f"ICE-ANALYSIS: ASIP analysis unavailable ({type(e).__name__}: {e}) — "
              "the text product via sea-ice-outlook.py stands alone this run.")
        return 0

    lon0, lat_ts = parse_prj(prj)
    proj = PolarStereo(lon0, lat_ts)
    sx, sy = proj.forward(lat, lon)

    at_point = None
    candidates = []
    for rings, a in zip(polys, attrs):
        if not rings:
            continue
        tenths, label = decode_ct(a.get("CT", ""))
        if at_point is None and point_in_rings(sx, sy, rings):
            at_point = {"ct": a.get("CT", ""), "tenths": tenths, "label": label,
                        "basin": a.get("NAME", ""), "icecode": a.get("ICECODE", ""),
                        "poly_type": a.get("POLY_TYPE", "")}
        if tenths is not None and tenths >= args.threshold:
            hit = nearest_point_on_rings(sx, sy, rings)
            if hit:
                candidates.append((hit[0], hit[1], hit[2], tenths, label, a.get("NAME", "")))

    nearest = None
    if candidates:
        candidates.sort(key=lambda c: c[0])
        # re-rank the closest few geodetically — projected distance is distorted at range
        best = None
        for _, qx, qy, tenths, label, basin in candidates[:12]:
            qlat, qlon = proj.inverse(qx, qy)
            d = haversine_nm(lat, lon, qlat, qlon)
            if best is None or d < best[0]:
                best = (d, qlat, qlon, tenths, label, basin)
        brg = bearing_deg(lat, lon, best[1], best[2])
        nearest = {"distance_nm": round(best[0], 1), "bearing_deg": round(brg),
                   "compass": compass(brg), "lat": round(best[1], 4), "lon": round(best[2], 4),
                   "tenths": best[3], "concentration": best[4], "basin": best[5]}

    usnic = None if args.no_usnic else fetch_usnic(min(args.timeout, 30))

    result = {
        "available": True,
        "location": {"lat": lat, "lon": lon, "label": args.label},
        "source": {"product": "NWS ASIP daily ice analysis (shapefile)", "member": base,
                   "analysis_date": analysis_date, "last_modified": last_mod,
                   "url": ANALYSIS_URL, "polygons": len([p for p in polys if p])},
        "at_point": at_point,
        "nearest_ice": nearest,
        "threshold_tenths": args.threshold,
        "usnic_synopsis": usnic,
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    where = f" ({args.label})" if args.label else ""
    print(f"### Ice — ASIP daily analysis{where}")
    print(f"_{lat:.3f},{lon:.3f} · analysis {analysis_date or '?'} "
          f"· published {last_mod or '?'} · {result['source']['polygons']} polygons_")
    print()
    if at_point:
        extra = f" · {at_point['basin']}" if at_point["basin"] else ""
        print(f"- concentration **at the point**: **{at_point['label']}** "
              f"(CT `{at_point['ct'] or '--'}`){extra}")
    else:
        print("- concentration at the point: **outside the analysed area** "
              "(the ASIP polygons don't cover this position)")
    in_ice = (at_point and at_point["tenths"] is not None
              and at_point["tenths"] >= args.threshold)
    if in_ice:
        print(f"- ⚠️ **the point is INSIDE ice ≥{args.threshold:g}/10** — this is not an "
              f"approach distance, you are in it")
    elif nearest:
        print(f"- nearest ice ≥{args.threshold:g}/10: **{nearest['distance_nm']} nm** "
              f"{nearest['compass']} ({nearest['bearing_deg']}°) — "
              f"{nearest['concentration']} in the {nearest['basin'] or 'analysis'}")
    else:
        print(f"- no ice ≥{args.threshold:g}/10 anywhere in the analysis")
    if usnic and usnic.get("available"):
        print(f"- USNIC Arctic Regional Synopsis reachable · {usnic['cadence']} · "
              f"last modified {usnic['last_modified'] or '?'}")
    elif usnic:
        print(f"- USNIC synopsis unavailable: {usnic.get('note', '')}")
    print()
    print("_Daily analysis — compare its date against the twice-weekly text product from "
          "`sea-ice-outlook.py`; when they differ, this is the fresher picture._")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                   # never take out the ship block
        print(f"ICE-ANALYSIS: unexpected failure ({type(e).__name__}: {e})")
        sys.exit(0)
