#!/usr/bin/env python3
"""gtasks-diff.py — snapshot the (read-only) Google Tasks work list and print the delta.

Automation #52 (approved 2026-09-08). Each /daily run:
  1. pulls every OPEN task from every list on the account (tasks.readonly scope — the
     same token cache the MCP server uses; this script cannot modify a task),
  2. writes the snapshot to  AI Scratchpad/Notes/_daily-data/.gtasks/YYYY-MM-DD.json,
  3. diffs it against the most recent earlier snapshot, keyed on task ID, and prints a
     markdown block:  GONE (= user-completed, per his standing rule) · NEW · RE-DATED ·
     RENAMED · plus the due-today / overdue / next-7-days buckets.

Why ID-keyed: on 2026-09-06 a hand diff reported "Update SDAT dashboard closed" when the
task had just been *created*; an ID diff cannot confuse the two.

Usage
  python3 scripts/gtasks-diff.py                 # snapshot today + diff vs previous
  python3 scripts/gtasks-diff.py --date 2026-09-08 --horizon 7
  python3 scripts/gtasks-diff.py --json          # machine-readable
  python3 scripts/gtasks-diff.py --no-snapshot   # diff only, don't write today's file

Credentials: GOOGLE_TASKS_CLIENT_ID / _SECRET via scripts/_secrets.py (secrets.env);
token cache at GOOGLE_TASKS_TOKEN_PATH (default ~/.config/llm-land-mcp/google-tasks-token.json),
refreshed in place when near expiry. No token value is ever printed.
Exit: 0 ok · 1 auth/API failure (message printed, safe to fall back to the MCP) · 2 file error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _secrets import load_secrets  # noqa: E402

load_secrets()

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "AI Scratchpad" / "Notes" / "_daily-data" / ".gtasks"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
API = "https://tasks.googleapis.com/tasks/v1"
REAUTH = "cd mcp-servers/google-tasks && npm run auth"


def token_path() -> Path:
    p = os.environ.get("GOOGLE_TASKS_TOKEN_PATH")
    return Path(p).expanduser() if p else Path.home() / ".config" / "llm-land-mcp" / "google-tasks-token.json"


def access_token() -> str:
    tp = token_path()
    try:
        cache = json.loads(tp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError(f"no Google Tasks token cache at {tp}. Re-auth: {REAUTH}")
    if not cache.get("refresh_token"):
        raise RuntimeError(f"token cache has no refresh_token. Re-auth: {REAUTH}")
    now_ms = int(time.time() * 1000)
    if cache.get("access_token") and now_ms < int(cache.get("expires_at", 0)) - 120_000:
        return cache["access_token"]
    client_id = os.environ.get("GOOGLE_TASKS_CLIENT_ID")
    if not client_id:
        raise RuntimeError("GOOGLE_TASKS_CLIENT_ID not set (secrets.env)")
    body = {"client_id": client_id, "refresh_token": cache["refresh_token"], "grant_type": "refresh_token"}
    if os.environ.get("GOOGLE_TASKS_CLIENT_SECRET"):
        body["client_secret"] = os.environ["GOOGLE_TASKS_CLIENT_SECRET"]
    r = requests.post(TOKEN_ENDPOINT, data=body, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"token refresh rejected ({r.status_code}). Re-auth: {REAUTH}")
    data = r.json()
    cache.update({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", cache["refresh_token"]),
        "expires_at": now_ms + int(data.get("expires_in", 3600)) * 1000,
        "scope": data.get("scope", cache.get("scope")),
    })
    tp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    try:
        tp.chmod(0o600)
    except OSError:
        pass
    return cache["access_token"]


def get(url: str, tok: str, params: dict | None = None) -> dict:
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"{url.split('/v1/')[-1]} → HTTP {r.status_code}")
    return r.json()


def pull_open_tasks(tok: str) -> list[dict]:
    out: list[dict] = []
    lists = get(f"{API}/users/@me/lists", tok, {"maxResults": 100}).get("items", [])
    for lst in lists:
        page = None
        while True:
            params = {"showCompleted": "false", "showHidden": "false", "maxResults": 100}
            if page:
                params["pageToken"] = page
            data = get(f"{API}/lists/{lst['id']}/tasks", tok, params)
            for t in data.get("items", []):
                if t.get("status") == "completed":
                    continue
                out.append({
                    "id": t["id"],
                    "list": lst.get("title", ""),
                    "title": (t.get("title") or "").strip(),
                    "due": (t.get("due") or "")[:10] or None,
                    "updated": t.get("updated"),
                    "notes": (t.get("notes") or "").strip()[:160],
                })
            page = data.get("nextPageToken")
            if not page:
                break
    out.sort(key=lambda t: (t["due"] or "9999", t["title"].lower()))
    return out


def previous_snapshot(before: str) -> tuple[str, list[dict]] | None:
    if not SNAP_DIR.exists():
        return None
    cands = sorted(p for p in SNAP_DIR.glob("????-??-??.json") if p.stem < before)
    if not cands:
        return None
    p = cands[-1]
    try:
        return p.stem, json.loads(p.read_text(encoding="utf-8")).get("tasks", [])
    except (OSError, ValueError):
        return None


def fmt(t: dict) -> str:
    return f"{t['title']} ({t['due'] or 'undated'})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="snapshot date (default today)")
    ap.add_argument("--horizon", type=int, default=7, help="days ahead for the 'next N days' bucket")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-snapshot", action="store_true", help="don't write today's snapshot file")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.date)

    try:
        tok = access_token()
        tasks = pull_open_tasks(tok)
    except (RuntimeError, requests.RequestException) as e:
        print(f"GTASKS-DIFF: FAILED — {e}")
        return 1

    prev = previous_snapshot(args.date)
    prev_date, prev_tasks = prev if prev else (None, [])
    prev_by_id = {t["id"]: t for t in prev_tasks}
    now_by_id = {t["id"]: t for t in tasks}

    gone = [t for i, t in prev_by_id.items() if i not in now_by_id]
    new = [t for i, t in now_by_id.items() if i not in prev_by_id]
    redated, renamed = [], []
    for i, t in now_by_id.items():
        p = prev_by_id.get(i)
        if not p:
            continue
        if p.get("due") != t.get("due"):
            redated.append((p, t))
        if p.get("title") != t.get("title"):
            renamed.append((p, t))

    overdue = [t for t in tasks if t["due"] and dt.date.fromisoformat(t["due"]) < today]
    due_today = [t for t in tasks if t["due"] == args.date]
    upcoming = [t for t in tasks if t["due"] and today < dt.date.fromisoformat(t["due"]) <= today + dt.timedelta(days=args.horizon)]
    undated = [t for t in tasks if not t["due"]]

    if not args.no_snapshot:
        try:
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            (SNAP_DIR / f"{args.date}.json").write_text(
                json.dumps({"date": args.date, "count": len(tasks), "tasks": tasks}, indent=1, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            print(f"GTASKS-DIFF: snapshot write failed — {e}")
            return 2

    if args.json:
        print(json.dumps({
            "date": args.date, "count": len(tasks), "prev_date": prev_date, "prev_count": len(prev_tasks),
            "gone": gone, "new": new,
            "redated": [{"id": t["id"], "title": t["title"], "from": p["due"], "to": t["due"]} for p, t in redated],
            "renamed": [{"id": t["id"], "from": p["title"], "to": t["title"]} for p, t in renamed],
            "overdue": overdue, "due_today": due_today, "upcoming": upcoming, "undated": undated, "tasks": tasks,
        }, indent=1, ensure_ascii=False))
        return 0

    print(f"### Google Tasks — {args.date} · {len(tasks)} open" +
          (f" (was {len(prev_tasks)} on {prev_date})" if prev_date else " (first snapshot — no diff)"))
    if prev_date:
        print(f"- **GONE ({len(gone)})** — left the open list = user-completed: " + ("; ".join(fmt(t) for t in gone) if gone else "none"))
        print(f"- **NEW ({len(new)})**: " + ("; ".join(fmt(t) for t in new) if new else "none"))
        print(f"- **RE-DATED ({len(redated)})**: " + ("; ".join(f"{t['title']} {p['due'] or '—'} → {t['due'] or '—'}" for p, t in redated) if redated else "none"))
        print(f"- **RENAMED ({len(renamed)})**: " + ("; ".join(f"“{p['title']}” → “{t['title']}”" for p, t in renamed) if renamed else "none"))
    print(f"- **Overdue ({len(overdue)})**: " + ("; ".join(fmt(t) for t in overdue) if overdue else "none"))
    print(f"- **Due today ({len(due_today)})**: " + ("; ".join(t['title'] for t in due_today) if due_today else "none"))
    print(f"- **Next {args.horizon} days ({len(upcoming)})**: " + ("; ".join(fmt(t) for t in upcoming) if upcoming else "none"))
    print(f"- **Undated ({len(undated)})**: " + ("; ".join(t['title'] for t in undated) if undated else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
