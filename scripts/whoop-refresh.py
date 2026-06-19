#!/usr/bin/env python3
"""
whoop-refresh.py — reliable headless Whoop pull for /daily (automation #17).

Built 2026-06-17. Sidesteps the flaky Whoop MCP server: refreshes the stored
access token (it has a ~1h TTL) via the refresh_token grant, then pulls recovery /
sleep / cycle directly from the v2 API.

Why this path: the MCP server intermittently 500/400s, and even when up it does not
reliably pick up an out-of-band token refresh. The refresh_token exchange is
deterministic and headless — only a full scope re-auth (rare, on revocation) needs a
browser (`npm run auth` in mcp-servers/whoop). See memory reference-whoop-direct-api.

Token + creds:
  - tokens.json:  mcp-servers/whoop/tokens.json   (access_token, refresh_token, expires_at ms)
  - client creds: read from the whoop MCP env block in ~/.claude.json (never printed)

Cloudflare gotcha: the API returns 403 "error code: 1010" to urllib's default
User-Agent — we send a browser-ish UA to avoid it.

Usage:
  python3 scripts/whoop-refresh.py                 # ensure token valid + print 3-day table
  python3 scripts/whoop-refresh.py --days 7        # 7-day window
  python3 scripts/whoop-refresh.py --start 2026-06-14 --end 2026-06-17
  python3 scripts/whoop-refresh.py --refresh-only   # just refresh the token, no pull
  python3 scripts/whoop-refresh.py --json           # machine-readable
Exit codes: 0 ok; 1 refresh failed (likely needs browser re-auth); 2 config/IO error.
"""
import argparse, json, os, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

LLM_LAND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS = os.path.join(LLM_LAND, "mcp-servers", "whoop", "tokens.json")
CLAUDE_CFG = os.path.expanduser("~/.claude.json")
PROJECT_KEY = "/mnt/c/Users/damia/OneDrive/Documents/LLM_Land"
SCOPE = "offline read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement"
UA = "whoop-mcp-server/1.0 (Node.js fetch compatible)"
BASE = "https://api.prod.whoop.com"


def die(code, msg):
    print(msg, file=sys.stderr)
    sys.exit(code)


def load_creds():
    try:
        cfg = json.load(open(CLAUDE_CFG))
        env = cfg["projects"][PROJECT_KEY]["mcpServers"]["whoop"]["env"]
        return env["WHOOP_CLIENT_ID"], env["WHOOP_CLIENT_SECRET"]
    except (KeyError, IOError, ValueError) as e:
        die(2, f"could not read Whoop client creds from {CLAUDE_CFG}: {e}")


def ensure_token():
    """Return a valid access token, refreshing if expired/near-expiry. Never prints it."""
    try:
        tok = json.load(open(TOKENS))
    except (IOError, ValueError) as e:
        die(2, f"could not read {TOKENS}: {e}")
    now_ms = int(time.time() * 1000)
    if tok.get("expires_at", 0) > now_ms + 60_000:
        return tok["access_token"], False  # still valid
    cid, csec = load_creds()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": cid, "client_secret": csec, "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(BASE + "/oauth/oauth2/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    try:
        new = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        die(1, f"token refresh failed ({e.code}): {e.read().decode()[:200]}\n"
               f"refresh_token may be revoked — run `npm run auth` in mcp-servers/whoop.")
    tok["access_token"] = new["access_token"]
    if "refresh_token" in new:
        tok["refresh_token"] = new["refresh_token"]
    tok["expires_at"] = int(time.time() * 1000) + new.get("expires_in", 3600) * 1000
    if "scope" in new:
        tok["scope"] = new["scope"]
    json.dump(tok, open(TOKENS, "w"), indent=2)
    os.chmod(TOKENS, 0o600)
    return tok["access_token"], True


def get(at, path, params):
    url = BASE + "/developer" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + at, "User-Agent": UA, "Accept": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"ERROR": e.code, "body": e.read().decode()[:200]}


def h(ms):
    return None if ms is None else round(ms / 3600000, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--refresh-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    at, refreshed = ensure_token()
    print(f"[token {'refreshed' if refreshed else 'valid'}]", file=sys.stderr)
    if a.refresh_only:
        return

    end = a.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = a.start or (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=a.days)).strftime("%Y-%m-%d")
    s_iso, e_iso = f"{start}T00:00:00.000Z", f"{end}T23:59:59.999Z"

    rec = get(at, "/v2/recovery", {"start": s_iso, "end": e_iso, "limit": 25})
    slp = get(at, "/v2/activity/sleep", {"start": s_iso, "end": e_iso, "limit": 25})
    cyc = get(at, "/v2/cycle", {"start": s_iso, "end": e_iso, "limit": 25})
    for label, d in (("recovery", rec), ("sleep", slp), ("cycle", cyc)):
        if isinstance(d, dict) and d.get("ERROR"):
            die(1, f"{label} pull failed {d['ERROR']}: {d['body']}")

    if a.json:
        print(json.dumps({"recovery": rec.get("records", []), "sleep": slp.get("records", []), "cycle": cyc.get("records", [])}))
        return

    print("=== RECOVERY (by morning) ===")
    for r in sorted(rec.get("records", []), key=lambda x: x["created_at"]):
        s = r.get("score", {})
        print(f"{r['created_at'][:10]} | score {s.get('recovery_score')} | HRV {round(s.get('hrv_rmssd_milli',0),1)} | RHR {s.get('resting_heart_rate')} | SpO2 {round(s.get('spo2_percentage',0),1)}")
    print("=== SLEEP (by end) ===")
    for r in sorted(slp.get("records", []), key=lambda x: x["end"]):
        if r.get("nap"):
            continue
        sc = r.get("score", {}); ss = sc.get("stage_summary", {})
        asleep = ss.get("total_in_bed_time_milli", 0) - ss.get("total_awake_time_milli", 0)
        debt = sc.get("sleep_needed", {}).get("need_from_sleep_debt_milli")
        print(f"{r['end'][:10]} | asleep {h(asleep)} | SWS {h(ss.get('total_slow_wave_sleep_time_milli'))} | REM {h(ss.get('total_rem_sleep_time_milli'))} | cyc {ss.get('sleep_cycle_count')} | dist {ss.get('disturbance_count')} | perf {sc.get('sleep_performance_percentage')} | debt {h(debt)}")
    print("=== CYCLE (strain) ===")
    for r in sorted(cyc.get("records", []), key=lambda x: x["start"]):
        s = r.get("score", {})
        print(f"{r['start'][:10]} | strain {round(s.get('strain',0),1)} | avgHR {s.get('average_heart_rate')} | maxHR {s.get('max_heart_rate')}")


if __name__ == "__main__":
    main()
