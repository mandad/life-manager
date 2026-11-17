#!/usr/bin/env python3
"""Sync the agent-link hub with the LLM_Land vault.

  hub-sync.py pull                                   -> Notes/_daily-data/<YYYY-MM>/hub-<date>.md + hub-export-<date>.json + Notes/Profile (hub mirror).md
  hub-sync.py push --today "AI Scratchpad/My Day.md" [< summary.txt]
                                                     -> every "- [ ]" line becomes action_add (idempotent per vault line);
                                                        stdin (if any) becomes memory_set life/daily-summary;
                                                        the deadline radar (weekly-prep.py, 400 days) becomes deadlines/vault;
                                                        My Day's "Do — today only" boxes become dash/today;
                                                        both leave out what the owner checked off on the dashboard (owner/done),
                                                        and owner/done entries already logged and gone from the vault are pruned
  (seed-profile was retired 2026-10-13: profile/* is now edited in place on the hub; the pull keeps a read-only local mirror.)

Config: --url/--secret, or env HUB_URL / HUB_SECRET (loaded from ~/.config/llm-land-mcp/secrets.env by scripts/_secrets.py when run inside LLM_Land).
Hub unreachable on push: parks the payload in Notes/_daily-data/hub-outbox.json and exits 0; next push retries.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("pip install requests")
try:  # LLM_Land only: loads secrets.env into os.environ
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _secrets import load_secrets
    load_secrets()  # fills from secrets.env; anything already exported wins
except Exception:
    pass

AGENT = "life-manager"


def rpc(url, secret, tool, **args):
    r = requests.post(f"{url.rstrip('/')}/rpc", json={"tool": tool, "args": {"agent": AGENT, **args}},
                      headers={"Authorization": f"Bearer {secret}"}, timeout=25)
    if r.status_code == 403:
        raise SystemExit("hub refused the secret (403)")
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(j.get("error"))
    return j["result"]


def when(t):
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%Y-%m-%d %H:%MZ") if t else ""


def pull(a):
    today = dt.date.today().isoformat()
    d = pathlib.Path(a.vault) / "Notes" / "_daily-data" / today[:7]  # #67: the month folder, beside the day files
    d.mkdir(parents=True, exist_ok=True)
    b = rpc(a.url, a.secret, "brief")
    rpc(a.url, a.secret, "status_set", text="daily sync", about="Life Manager: the owner's local life-manager vault (Obsidian + Claude Code, folder LLM_Land). Full sync once a day (/daily); a headless pickup checks the hub every 15 min, answers from the vault and queues the rest for the next run; pushes tasks the owner tags for agents.")
    ex = rpc(a.url, a.secret, "hub_export")
    (d / f"hub-export-{today}.json").write_text(json.dumps(ex, indent=1))
    emails = [r for r in b["open"] + b["claimed"] if r.get("kind") == "email"]
    tasks_open = [r for r in b["open"] if r.get("kind") != "email" and r.get("assigned_to") != AGENT]
    L = [f"---\ntitle: Hub pull {today}\npurpose: What the cloud agents proposed, claimed, finished or said since last pull. Triage into project files, never into Inbox.\n---\n",
         "# Hub pull", "", "## Agents"]
    L += [f"- {ag['name']}: {ag.get('status') or '(no status)'} ({'set ' + when(ag['status_at']) + ', ' if ag.get('status_at') else ''}last seen {when(ag.get('last_seen_at'))})" for ag in b["agents"]] or ["- (none)"]
    if b.get("digest"):
        L += ["", "## Latest digest", b["digest"].strip()]
    L += ["", "## Assigned to life-manager"]
    L += [f"- [ ] #{r['id']} {r['title']} (from {r['created_by']})" for r in b["assigned_to_me"]] or ["- (none)"]
    L += ["", "## Open (agents proposed; route to the right project file)"]
    L += [f"- [ ] #{r['id']} {r['title']} (by {r['created_by']}" + (f", assigned to {r['assigned_to']}" if r.get('assigned_to') else "") + ")" for r in tasks_open] or ["- (none)"]
    L += ["", "## Claimed"]
    L += [f"- #{r['id']} {r['title']} → {r['claimed_by']} until {when(r.get('lease_until'))}" for r in b["claimed"] if r.get("kind") != "email"] or ["- (none)"]
    L += ["", "## Email triage in progress"]
    L += [f"- #{r['id']} {r['title']} [{r['status']}" + (f" by {r['claimed_by']}" if r.get('claimed_by') else "") + f"] ({r.get('source')})" for r in emails] or ["- (none)"]
    L += ["", "## Confirm? (agents say done; never auto-check vault boxes)"]
    L += [f"- [ ] #{r['id']} {r['title']} → {r['done_by']}: {r.get('result') or ''} ({when(r.get('done_at'))})" for r in b["recent_done"]] or ["- (none)"]
    # for_you repeats on every brief (3-day window), so keep earlier lines of the day and drop repeats.
    day = d / f"hub-{today}.md"
    old = []
    if day.exists():
        txt = day.read_text(encoding="utf-8")
        tail = txt.split("## Messages", 1)[1] if "## Messages" in txt else ""
        old = [l for l in tail.splitlines() if l.startswith("- from ")]
    new = [f"- from {m['from_agent']}" + (f" to {m['to_agent']}" if m.get('to_agent') else "") + f": {m['body']}" for m in b["for_you"]]
    L += ["", "## Messages"]
    L += list(dict.fromkeys(old + new)) or ["- (none)"]
    day.write_text("\n".join(L) + "\n", encoding="utf-8")
    prof = rpc(a.url, a.secret, "profile_get")
    (pathlib.Path(a.vault) / "Notes" / "Profile (hub mirror).md").write_text(
        f"---\ntitle: Profile (hub mirror)\npurpose: Read-only mirror of profile/* on the agent-link hub. Edit on the hub (https://agent.mandabot.com/ui/memory), not here.\nmirrored: {today}\n---\n\n# Profile (hub mirror)\n\n{prof['text']}")
    print(f"pulled {len(tasks_open)} open, {len(b['claimed'])} claimed, {len(emails)} emails, {len(b['for_you'])} addressed to us, {len(b['assigned_to_me'])} assigned, {len(prof['sections'])} profile sections")


RADAR = re.compile(r"^- `(\d{4}-\d{2}-\d{2})` \([+-]\d+d\) — `([^`]+):\d+` — (.+)$", re.M)


def radar_deadlines():
    """The vault's dated open tasks (weekly-prep.py radar, 400 days out) as the deadlines/vault feed.
    None when the radar is missing or fails, so a broken run never wipes the calendar."""
    wp = pathlib.Path(__file__).resolve().parent / "weekly-prep.py"
    if not wp.exists():
        return None
    p = subprocess.run([sys.executable, str(wp), "--radar-only", "--horizon", "400"],
                       capture_output=True, text=True, timeout=120, cwd=wp.parent.parent)
    if p.returncode != 0 or not p.stdout.startswith("# Deadline radar"):
        return None
    items = []
    for date, path, body in RADAR.findall(p.stdout):
        m = re.search(r"\*\*(.+?)\*\*", body)  # the task's bold headline, without the notes after it
        title = re.sub(r"\s+", " ", m.group(1) if m else body).strip(" *…")[:150]
        items.append({"id": hashlib.sha1(f"{path}|{title}".encode()).hexdigest()[:16], "date": date, "title": title})
    return {"items": items}


DO_SECTION = re.compile(r"^# ✅ Do — today only[^\n]*\n(.*?)(?=^# )", re.M | re.S)
DO_GROUPS = {"Pinned": "pinned", "Surfacing": "surfacing", "Squeeze": "squeeze"}


def today_items(vault):
    """My Day's "Do — today only" boxes (Pinned, Surfacing now, Squeeze in) as the dash/today feed. None if My Day is missing."""
    p = pathlib.Path(vault) / "My Day.md"
    m = DO_SECTION.search(p.read_text(encoding="utf-8")) if p.exists() else None
    if not m:
        return None
    items, group = [], None
    for line in m.group(1).splitlines():
        h = re.match(r"###\s+\S+\s+(\w+)", line)  # "### 🔴 Pinned — scored ..."
        if h:
            group = DO_GROUPS.get(h.group(1))
            continue
        t = re.match(r"- \[ \] (.+)", line)
        if not t or not group:
            continue
        hm = re.search(r"\*\*(.+?)\*\*", t.group(1))
        head = re.sub(r"\s+", " ", hm.group(1) if hm else t.group(1)).strip()
        n = re.match(r"#(\d+) — (.+)", head)
        title = (n.group(2) if n else head)[:150]
        it = {"id": hashlib.sha1(f"today|{title}".encode()).hexdigest()[:16], "title": title,
              "status": f"pinned #{n.group(1)}" if n and group == "pinned" else group}
        dm = re.search(r"\*\((.+?)\)\*", t.group(1))  # "*(20m · commitment · due 10/28 ...)*"
        if dm:
            d = dm.group(1).replace("**", "")
            it["detail"] = d if len(d) <= 160 else d[:159] + "…"
        items.append(it)
    return {"items": items}


def owner_done(a):
    """(entries, rev) of owner/done, the dashboard check-offs; ([], None) when the hub can't be read."""
    try:
        cur = rpc(a.url, a.secret, "memory_get", key="owner/done", max_chars=0)
    except (requests.RequestException, RuntimeError):
        return [], None
    return (json.loads(cur["value"]).get("items", []) if cur.get("value") else []), cur.get("rev", 0)


def push(a):
    outbox = pathlib.Path(a.vault) / "Notes" / "_daily-data" / "hub-outbox.json"
    items = json.loads(outbox.read_text()) if outbox.exists() else []
    if a.today:
        p = pathlib.Path(a.today)
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*- \[ \] (.+)", line)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
                items.append({"tool": "action_add", "args": {"title": title, "idempotency_key": "vault:" + hashlib.sha1((str(p) + "|" + title).encode()).hexdigest()}})
    if not sys.stdin.isatty():
        s = sys.stdin.read().strip()
        if s:
            items.append({"tool": "memory_set", "args": {"key": "life/daily-summary", "value": s[:8000]}})
    done_items, done_rev = owner_done(a)
    done = {d.get("ref") for d in done_items}
    dl, td = radar_deadlines(), today_items(a.vault)
    for key, feed, pre in (("deadlines/vault", dl, "vault/"), ("dash/today", td, "today/")):
        if feed is not None:  # full replace: the dashboard and the Agent deadlines calendar follow these keys
            keep = [i for i in feed["items"] if pre + i["id"] not in done]
            items.append({"tool": "memory_set", "args": {"key": key, "value": json.dumps({"items": keep}, ensure_ascii=False)}})
    if done_rev is not None and dl is not None and td is not None:  # logged check-offs whose task left the vault are finished
        live = {"vault/" + i["id"] for i in dl["items"]} | {"today/" + i["id"] for i in td["items"]}
        left_over = [d for d in done_items if not d.get("logged") or d.get("ref") in live]
        if len(left_over) != len(done_items):
            items.append({"tool": "memory_set", "args": {"key": "owner/done", "value": json.dumps({"items": left_over}, ensure_ascii=False), "expected_rev": done_rev}})
    left = []
    sent = 0
    for it in items:
        try:
            rpc(a.url, a.secret, it["tool"], **it["args"])
            sent += 1
        except requests.HTTPError as e:  # server answered (e.g. DreamHost WAF 418/500): report, don't retry forever
            print(f"hub rejected (HTTP {e.response.status_code}) {it['tool']}: {str(it['args'].get('title') or it['args'].get('key'))[:80]}")
        except requests.RequestException:
            left.append(it)
    if left:
        outbox.parent.mkdir(parents=True, exist_ok=True)
        outbox.write_text(json.dumps(left))
        print(f"hub unreachable; parked {len(left)} items in {outbox}")
        return
    if outbox.exists():
        outbox.unlink()
    print(f"pushed {sent} items")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["pull", "push"])
    ap.add_argument("--url", default=os.environ.get("HUB_URL", "https://agent.mandabot.com"))
    ap.add_argument("--secret", default=os.environ.get("HUB_SECRET"))
    ap.add_argument("--vault", default=os.environ.get("LIFE_VAULT_DIR", "AI Scratchpad"))
    ap.add_argument("--today", help="markdown file whose '- [ ]' lines are pushed as actions")
    a = ap.parse_args()
    if not a.secret:
        sys.exit("need HUB_SECRET (env or --secret)")
    try:
        {"pull": pull, "push": push}[a.cmd](a)
    except requests.RequestException as e:
        if a.cmd == "push":
            push(a)  # parks the items
        else:
            print(f"hub unreachable: {e}")
