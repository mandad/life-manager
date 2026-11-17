#!/usr/bin/env python3
"""Login-health watchdog (hub build #153): probe every token the agents depend on, once a day.

Each probe makes one real, cheap API call and returns ok / auth / transient:
  auth       the login itself is dead (invalid_grant, 401, revoked, no token cache): needs the re-auth step.
  transient  network or server trouble: only alerts after 3 failed runs in a row.
Results go to the hub as dash/logins (the owner's dashboard). A dead login files one action for
"damian" titled "Re-auth <service>" with the exact step; the hub pushes it to his phone once, its
title dedup stops repeats, and the next ok run closes it. If this script stops reporting for 26 h
(laptop asleep), the hub's hourly job files its own alert.

Not probed: Gmail and Strava, which exist only as claude.ai connectors.

  python3 scripts/login-watchdog.py            # probe, report to the hub
  python3 scripts/login-watchdog.py --dry-run  # probe and print only

Runs from Task Scheduler via login-watchdog.vbs (daily 06:00, at logon, catch-up after a missed start).
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _secrets import load_secrets  # noqa: E402
load_secrets()  # before anything reads token paths or client ids from the environment
STATE = Path.home() / ".local/state/llm-land/login-watchdog.json"
TRANSIENT_ALERT_AFTER = 3


def _module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


hook = _module("hub-hook")  # rpc(), and loads secrets.env


def http_verdict(r):
    if r.status_code == 200:
        return "ok", ""
    return ("auth" if r.status_code in (400, 401, 403) else "transient"), f"HTTP {r.status_code}"


def probe_google_tasks():
    g = _module("gtasks-diff")
    try:
        tok = g.access_token()
    except RuntimeError as e:  # gtasks-diff says "rejected (<code>)" for any non-200; a 5xx is Google's outage, not a dead token (#62)
        m = re.search(r"rejected \((\d+)\)", str(e))
        return ("transient" if m and int(m.group(1)) >= 500 else "auth"), str(e).split(". Re-auth")[0]
    except requests.RequestException as e:
        return "transient", type(e).__name__
    return http_verdict(requests.get(f"{g.API}/users/@me/lists", params={"maxResults": 1}, headers={"Authorization": f"Bearer {tok}"}, timeout=20))


def probe_whoop():
    p = subprocess.run([sys.executable, str(ROOT / "scripts/whoop-auth-check.py"), "--json"], capture_output=True, text=True, timeout=60)
    try:
        msg = json.loads(p.stdout).get("message", "")
    except ValueError:
        msg = (p.stdout + p.stderr).strip()
    return {0: "ok", 2: "auth"}.get(p.returncode, "transient"), "" if p.returncode == 0 else msg[:200]


ONENOTE_TOKENS = Path(os.environ.get("ONENOTE_TOKEN_PATH", Path.home() / ".config/llm-land/onenote-tokens.json")).expanduser()
ONENOTE_SCOPES = "https://graph.microsoft.com/Notes.Read https://graph.microsoft.com/Notes.Create offline_access"  # same as mcp-servers/onenote/src/auth.ts


def probe_onenote():
    try:
        cache = json.loads(ONENOTE_TOKENS.read_text())
    except (OSError, ValueError):
        return "auth", "no OneNote token cache"
    if time.time() * 1000 >= cache.get("expires_at", 0) - 120_000:
        r = requests.post("https://login.microsoftonline.com/consumers/oauth2/v2.0/token", timeout=30, data={
            "grant_type": "refresh_token", "client_id": os.environ.get("ONENOTE_CLIENT_ID", ""),
            "scope": ONENOTE_SCOPES, "refresh_token": cache.get("refresh_token", "")})
        if r.status_code != 200:
            return http_verdict(r)[0], f"token refresh HTTP {r.status_code} {r.json().get('error', '') if r.headers.get('content-type', '').startswith('application/json') else ''}".strip()
        d = r.json()  # Microsoft may rotate the refresh token: keep the cache current, same shape the MCP server writes
        cache = {"access_token": d["access_token"], "refresh_token": d.get("refresh_token", cache["refresh_token"]), "expires_at": int(time.time() * 1000) + int(d["expires_in"]) * 1000}
        ONENOTE_TOKENS.write_text(json.dumps(cache, indent=2))
        ONENOTE_TOKENS.chmod(0o600)
    return http_verdict(requests.get("https://graph.microsoft.com/v1.0/me/onenote/notebooks", params={"$top": 1}, headers={"Authorization": f"Bearer {cache['access_token']}"}, timeout=30))


def probe_rescuetime():
    key = os.environ.get("RESCUETIME_API_KEY")
    if not key:
        return "auth", "RESCUETIME_API_KEY not set"
    return http_verdict(requests.get("https://www.rescuetime.com/anapi/daily_summary_feed", params={"key": key}, timeout=20))


SERVICES = [  # name, probe, exact re-auth step
    ("google-tasks", probe_google_tasks, "In LLM_Land: cd mcp-servers/google-tasks && npm run auth (needs a terminal with a browser)."),
    ("whoop", probe_whoop, "In LLM_Land: cd mcp-servers/whoop && npm run auth"),
    ("onenote", probe_onenote, "In LLM_Land: cd mcp-servers/onenote && npm run auth, then finish the device-code login (SETUP.md Part B)."),
    ("rescuetime", probe_rescuetime, "Make a new key at https://www.rescuetime.com/anapi/manage and set RESCUETIME_API_KEY in ~/.config/llm-land-mcp/secrets.env."),
]


def probe(fn):
    try:
        return fn()
    except requests.RequestException as e:
        return "transient", type(e).__name__
    except Exception as e:  # a probe bug must not hide the other services
        return "transient", f"probe error: {type(e).__name__}: {e}"[:200]


def main(dry=False):
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rows, alerts, healthy = [], [], []
    for name, fn, step in SERVICES:
        status, detail = probe(fn)
        fails = state.get(name, 0) + 1 if status == "transient" else 0
        state[name] = fails
        rows.append({"title": name, "status": status, "detail": detail, "flag": status == "auth" or fails >= TRANSIENT_ALERT_AFTER})
        title = f"Re-auth {name}" if status == "auth" else f"{name} unreachable for {TRANSIENT_ALERT_AFTER}+ daily checks"
        if status == "auth" or fails >= TRANSIENT_ALERT_AFTER:
            alerts.append((title, f"{detail}\n\n{step}" if status == "auth" else f"Last error: {detail}. Probably an outage; if it persists, try: {step}"))
        if status == "ok":
            healthy.append(name)
        print(f"{name}: {status} {detail}".rstrip())
    if dry:
        return 0
    hook.rpc("memory_set", timeout=20, key="dash/logins", value=json.dumps({"items": rows}))
    for title, body in alerts:  # title dedup: an open "Re-auth whoop" is not filed twice
        hook.rpc("action_add", timeout=20, title=title, body=body, assign_to="damian", ask="todo", priority=1)  # priority: a to-do only pushes his phone when urgent
    if healthy:  # close earlier alerts for logins that work again
        for r in hook.rpc("action_list", timeout=20, status="open", limit=200) or []:
            n = re.match(r"(?:Re-auth (\S+)|(\S+) unreachable for \d\+ daily checks)$", r["title"])
            if n and r.get("created_by") == "life-manager" and (n.group(1) or n.group(2)) in healthy:
                hook.rpc("action_update", timeout=20, id=r["id"], status="done", result="login works again (login-watchdog)")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
