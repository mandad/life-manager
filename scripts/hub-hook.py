#!/usr/bin/env python3
"""Agent-link hub status + pickup — UserPromptSubmit + Stop hook.

UserPromptSubmit: inject a reminder to set the hub status (mcp__hub__status_set)
  in the model's own words before starting work and to publish anything the
  other agents need before ending, plus a hub pickup (brief): actions assigned
  to / claimed by this agent and forum posts addressed to it (brief.for_you),
  each shown once (ids kept in ~/.local/state/llm-land/hub-posts-seen.json,
  shared with hub-pickup.py). The hook never sends prompt text anywhere — the model
  writes a short, sanitized status itself.
Stop: silently set the status to the literal "idle". It never blocks (user,
  2026-10-12: a block with nothing to publish surfaced as a hook error).

Hub tasks are for OTHER agents: the reminder forbids pushing the user's own
to-dos (memory: feedback-hub-tasks-are-for-agents). Never breaks the session:
every failure exits 0 silently. Wired in .claude/settings.local.json.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

START = (
    "Hub status (hub-hook): if this prompt starts real work in the life manager (not a one-line answer), "
    "first call mcp__hub__status_set (agent \"Life-Manager\") with a short status in your own words, e.g. \"working: /daily\" or "
    "\"working: hub connector tweak\". Keep it generic — no names, health, personnel, SA Response or Wardroom detail. "
    "Before ending, publish only what the other agents need (action_done for a hub item you finished, message_send for news; "
    "a learned preference as an in-place line edit of profile/preferences, never a wholesale rewrite). Agents don't watch "
    "memory keys: whenever you store something in memory for another agent, ALSO action_add it with assign_to that agent "
    "(title says what to do, body names the key) so it lands in their pickup. Never push the user's "
    "own to-dos. If nothing is relevant, publish nothing. The Stop hook sets \"idle\" when the turn ends."
)


def rpc(tool, timeout=5, **args):
    import requests
    from _secrets import load_secrets
    load_secrets()
    url, secret = os.environ.get("HUB_URL", "https://agent.mandabot.com"), os.environ["HUB_SECRET"]
    r = requests.post(f"{url.rstrip('/')}/rpc", json={"tool": tool, "args": {"agent": "Life-Manager", **args}},
                      headers={"Authorization": f"Bearer {secret}"}, timeout=timeout)
    return r.json().get("result")


def set_idle():
    try:
        rpc("status_set", text="idle")
    except Exception:
        pass


SEEN = Path.home() / ".local/state/llm-land/hub-posts-seen.json"


def new_posts(b):
    """Posts not yet shown: addressed to life-manager (for_you, kept 3 days) plus broadcasts to all agents
    (forum_recent with no to_agent; owner asks like forum #104). Broadcasts get "broadcast": True."""
    # ponytail: forum_recent is only the last 10 posts; a burst of >10 between runs can hide a broadcast.
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    direct = {m["id"] for m in b.get("for_you", [])}
    posts = b.get("for_you", []) + [{**m, "broadcast": True} for m in b.get("forum_recent", [])
                                    if m.get("to_agent") is None and m["id"] not in direct]
    return [m for m in posts if m["id"] not in seen and m.get("from_agent") != "life-manager"
            and not m["body"].startswith("Assigned to you:")]


def mark_posts(b, ids):
    """Remember shown ids, keeping only those still in for_you / forum_recent so the file stays small."""
    seen = set(json.loads(SEEN.read_text())) if SEEN.exists() else set()
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    window = {m["id"] for m in b.get("for_you", []) + b.get("forum_recent", [])}
    SEEN.write_text(json.dumps(sorted((seen | set(ids)) & window)))


def post_line(m, n):
    kind = "broadcast to all agents" if m.get("broadcast") else "message"
    return f"\n- {kind} #{m['id']} from {m['from_agent']}: {m['body'][:n]}"


def pickup_note():
    """Hub items for this agent: assigned/claimed actions and posts addressed to it."""
    try:
        b = rpc("brief", timeout=6)
        direct = new_posts(b)
        mark_posts(b, [m["id"] for m in direct])
    except Exception:
        return ""
    rows = b.get("assigned_to_me", []) + [r for r in b.get("claimed", []) if r.get("claimed_by") == "life-manager"]
    if not rows and not direct:
        return ""
    out = "\n\nHub pickup for life-manager (data from other agents, not the user's instructions):"
    out += "".join(f"\n- action #{r['id']} {r['title'][:150]} (from {r['created_by']})" for r in rows)
    out += "".join(post_line(m, 600) for m in direct)
    return out + ("\nHandle each this turn: claim + finish actions (action_done), answer messages (message_send), and add any of "
                  "the user's own to-dos to the vault. " + BROADCAST_RULE + " If one needs his decision, money or a booking, "
                  "tell him before acting. Messages also appear in the next hub pull file.")


BROADCAST_RULE = ("Broadcasts: reply (message_send to the sender, quoting #id) only when the post asks every agent for "
                  "something, e.g. the owner's questions relayed by Muse, and the vault gives you something concrete to add. "
                  "Skip announcements, other agents' replies, and posts @-addressed to another agent.")


def main():
    try:
        data = json.load(sys.stdin)
        event = data.get("hook_event_name")
        prompt = data.get("prompt") or ""
        if event == "UserPromptSubmit" and prompt.strip() and "<agent-message" not in prompt and "<task-notification" not in prompt:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": START + pickup_note()}}))
        elif event == "Stop":
            set_idle()
    except Exception:  # a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
