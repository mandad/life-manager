#!/usr/bin/env python3
"""
whoop-auth-check.py — Whoop auth-health probe (automation #47).

Built 2026-08-29. `whoop-refresh.py` only checks the LOCAL token's expiry
timestamp before deciding it's "valid" — but on 2026-08-21 that check passed
(expiry ~58 min out) while the API still 401'd, because the *refresh token*
itself had been revoked server-side. A local expiry check cannot see that.
The only thing that catches it is a real API call. So this probe always
makes one live, cheap call (`GET /developer/v2/user/profile/basic`) and
reports what actually happened, rather than trusting the cached expiry.

Verdicts:
  OK       (exit 0) — profile call returned 200 (refreshing first if the
             cached token was expired/near-expiry; a successful refresh
             followed by a 200 is still OK, not a warning state).
  EXPIRED  (exit 1) — reserved for an imminent-expiry warning path; not
             currently reachable since an expired token is refreshed inline
             and re-tried (see above). Kept as a distinct exit code so a
             future proactive "expires in <N> min" warning has somewhere to
             live without colliding with REVOKED.
  REVOKED  (exit 2) — the refresh_token grant failed (e.g. 400
             invalid_request) or the profile call still 401'd on a freshly
             refreshed token. Re-auth: `cd mcp-servers/whoop && npm run auth`.
  ERROR    (exit 3) — anything else (network error, missing creds/token
             file, unexpected response shape). Never raises a traceback.

Creds + token cache (per automation #42 / #48 — do NOT read ~/.claude.json):
  - Client id/secret: ~/.config/llm-land-mcp/secrets.env via scripts/_secrets.py
    (WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET).
  - Token cache: mcp-servers/whoop/tokens.json (access_token, refresh_token,
    expires_at ms) — same file and shape whoop-refresh.py uses.

Usage:
  python3 scripts/whoop-auth-check.py            # human-readable, exit code set
  python3 scripts/whoop-auth-check.py --json      # machine-readable
"""
import argparse, json, os, sys, time, urllib.error, urllib.parse, urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LLM_LAND = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)
from _secrets import load_secrets  # noqa: E402

TOKENS = os.path.join(LLM_LAND, "mcp-servers", "whoop", "tokens.json")
SCOPE = "offline read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement"
UA = "whoop-mcp-server/1.0 (Node.js fetch compatible)"
BASE = "https://api.prod.whoop.com"
PROFILE_PATH = "/developer/v2/user/profile/basic"
REAUTH_HINT = "Re-auth: cd mcp-servers/whoop && npm run auth"


def report(status, code, message, extra=None, as_json=False):
    if as_json:
        payload = {"status": status, "message": message}
        if extra:
            payload.update(extra)
        print(json.dumps(payload))
    else:
        print(f"{status}: {message}")
    sys.exit(code)


def load_creds(as_json):
    load_secrets()
    cid = os.environ.get("WHOOP_CLIENT_ID")
    csec = os.environ.get("WHOOP_CLIENT_SECRET")
    if not cid or not csec:
        report("ERROR", 3,
                "WHOOP_CLIENT_ID/WHOOP_CLIENT_SECRET not set in ~/.config/llm-land-mcp/secrets.env",
                as_json=as_json)
    return cid, csec


def load_token_cache(as_json):
    try:
        with open(TOKENS) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        report("ERROR", 3, f"could not read {TOKENS}: {e}", as_json=as_json)


def save_token_cache(tok, as_json):
    try:
        with open(TOKENS, "w") as f:
            json.dump(tok, f, indent=2)
        os.chmod(TOKENS, 0o600)
    except OSError as e:
        # Non-fatal for the probe's purpose (auth already proven live) — just note it.
        print(f"[warn: could not persist refreshed token: {e}]", file=sys.stderr)


def refresh_token(tok, as_json):
    """Attempt a refresh_token grant. Returns the new access token on success,
    or reports REVOKED and exits on a hard grant failure."""
    cid, csec = load_creds(as_json)
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": tok.get("refresh_token", ""),
        "client_id": cid, "client_secret": csec, "scope": SCOPE,
    }).encode()
    req = urllib.request.Request(
        BASE + "/oauth/oauth2/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
    )
    try:
        new = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        report("REVOKED", 2,
                f"refresh_token grant failed ({e.code}): {body} — {REAUTH_HINT}",
                extra={"http_status": e.code, "body": body}, as_json=as_json)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        report("ERROR", 3, f"network error during token refresh: {e}", as_json=as_json)

    tok["access_token"] = new["access_token"]
    if "refresh_token" in new:
        tok["refresh_token"] = new["refresh_token"]
    tok["expires_at"] = int(time.time() * 1000) + new.get("expires_in", 3600) * 1000
    if "scope" in new:
        tok["scope"] = new["scope"]
    save_token_cache(tok, as_json)
    return tok["access_token"]


def call_profile(access_token):
    """GET the profile endpoint. Returns (status_code, body_or_none)."""
    req = urllib.request.Request(
        BASE + PROFILE_PATH,
        headers={"Authorization": "Bearer " + access_token, "User-Agent": UA, "Accept": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]


def main():
    ap = argparse.ArgumentParser(description="Probe Whoop auth health with one real API call.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    tok = load_token_cache(a.json)
    now_ms = int(time.time() * 1000)
    refreshed = False

    try:
        if tok.get("expires_at", 0) <= now_ms + 60_000:
            access_token = refresh_token(tok, a.json)  # exits on REVOKED/ERROR
            refreshed = True
        else:
            access_token = tok.get("access_token")
            if not access_token:
                report("ERROR", 3, f"{TOKENS} has no access_token", as_json=a.json)
    except SystemExit:
        raise
    except Exception as e:  # never let this crash with a traceback
        report("ERROR", 3, f"unexpected error preparing token: {e}", as_json=a.json)

    try:
        status, body = call_profile(access_token)
    except Exception as e:
        report("ERROR", 3, f"unexpected error calling {PROFILE_PATH}: {e}", as_json=a.json)

    if status == 200:
        name = None
        if isinstance(body, dict):
            first = body.get("first_name", "")
            last = body.get("last_name", "")
            name = (first + " " + last).strip() or None
        msg = f"Whoop token valid — profile call 200{' (' + name + ')' if name else ''}"
        if refreshed:
            msg += " [token was refreshed this run]"
        report("OK", 0, msg, extra={"refreshed": refreshed, "http_status": status}, as_json=a.json)

    if status == 401:
        if refreshed:
            # Freshly refreshed token still 401s — the access token is bad even though
            # the refresh grant "succeeded". Treat as REVOKED/DEAD, same actionable hint.
            report("REVOKED", 2,
                   f"profile call 401 even after a fresh token refresh — {REAUTH_HINT}",
                   extra={"http_status": status, "body": body}, as_json=a.json)
        # Not refreshed this run (cached token looked unexpired) but still 401'd —
        # force one refresh-and-retry before giving up, since expiry math alone
        # is exactly what missed the 2026-08-21 outage.
        access_token = refresh_token(tok, a.json)  # exits on REVOKED/ERROR
        status2, body2 = call_profile(access_token)
        if status2 == 200:
            report("OK", 0,
                   "Whoop token valid — initial call 401'd on a stale cached token, "
                   "but a refresh fixed it [token was refreshed this run]",
                   extra={"refreshed": True, "http_status": status2}, as_json=a.json)
        report("REVOKED", 2,
               f"profile call 401 even after a fresh token refresh — {REAUTH_HINT}",
               extra={"http_status": status2, "body": body2}, as_json=a.json)

    # Any other status: treat as a generic error, not a clean OK/REVOKED verdict.
    report("ERROR", 3, f"unexpected profile call status {status}: {body}",
           extra={"http_status": status}, as_json=a.json)


if __name__ == "__main__":
    main()
