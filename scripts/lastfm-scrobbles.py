#!/usr/bin/env python3
"""Last.fm scrobbles for one day — the /daily music stat (automation #41).

WHY LAST.FM AND NOT SPOTIFY
    The user streams mostly through Spotify but scrobbles to Last.fm, so Last.fm sits
    DOWNSTREAM and is the superset — it also catches anything played elsewhere. It needs only a
    read API key (no OAuth, no refresh dance), and `user.getRecentTracks` takes real `from`/`to`
    timestamps, so "yesterday" is exact. Spotify's `recently-played` is a 50-item cursor with no
    date range: it truncates on a heavy day and returns stale tracks on a quiet one.

WHAT IT REPORTS
    Scrobble count, distinct artists/tracks, top artist and track, the first and last scrobble of
    the day, and an **hourly histogram**. The histogram is the point: it lines up directly with
    the RescueTime hourly timeline the same run already produces, which is the one question no
    other source in this system can answer — what was playing during a given work block.

    Deliberately NOT reported: total listening minutes. `user.getRecentTracks` carries no
    duration field, so any minutes figure would have to be invented or would cost one extra API
    call per track. Count and span are honest; a fabricated duration is not.

DESIGN NOTES
    - stdlib only, like the other scripts here. Credentials come from `secrets.env` via
      `_secrets.py` (automation #42) — no `bash -ic`.
    - Day boundaries are LOCAL: `--date` means local midnight to local midnight, converted to
      the Unix timestamps the API wants. Scrobble times are rendered in local time too, so the
      histogram is directly comparable with RescueTime.
    - Any failure degrades to one note line and **exit 0**. A music stat must never take out a
      /daily run.

USAGE
    python3 scripts/lastfm-scrobbles.py                    # yesterday
    python3 scripts/lastfm-scrobbles.py --date 2026-08-15
    python3 scripts/lastfm-scrobbles.py --date 2026-08-15 --json
    python3 scripts/lastfm-scrobbles.py --today            # partial day so far
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _secrets import load_secrets  # automation #42: secrets.env is the single store

API = "https://ws.audioscrobbler.com/2.0/"
UA = "llm-land-lastfm/1.0 (daily music stat; accounts@dmanda.com)"
PAGE_LIMIT = 200          # API max
MAX_PAGES = 10            # 2000 scrobbles in a day would be extraordinary; bound it anyway


def note(msg):
    """Single-line graceful degradation. Always exit 0 — see module docstring."""
    print(f"LASTFM: {msg}")
    return 0


def call(method, key, user, timeout, **params):
    p = {"method": method, "user": user, "api_key": key, "format": "json"}
    p.update({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(p), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_day(key, user, start_ts, end_ts, timeout):
    """All scrobbles in [start_ts, end_ts). Returns (tracks, now_playing_or_None)."""
    tracks, now_playing, page = [], None, 1
    while page <= MAX_PAGES:
        data = call("user.getrecenttracks", key, user, timeout,
                    limit=PAGE_LIMIT, page=page,
                    **{"from": start_ts, "to": end_ts - 1})
        rt = data.get("recenttracks") or {}
        items = rt.get("track") or []
        if isinstance(items, dict):          # single-result responses aren't wrapped in a list
            items = [items]
        for t in items:
            d = (t.get("date") or {}).get("uts")
            if d is None:                     # the currently-playing track carries no date
                now_playing = t
                continue
            tracks.append((int(d), t))
        attr = rt.get("@attr") or {}
        total_pages = int(attr.get("totalPages") or 1)
        if page >= total_pages:
            break
        page += 1
    tracks.sort(key=lambda x: x[0])
    return tracks, now_playing


def main():
    ap = argparse.ArgumentParser(description="Last.fm scrobbles for one day.")
    ap.add_argument("--date", help="local date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--today", action="store_true", help="use today so far instead of yesterday")
    ap.add_argument("--user", help="Last.fm username (else $LASTFM_USER)")
    ap.add_argument("--key", help="Last.fm API key (else $LASTFM_API_KEY)")
    ap.add_argument("--top", type=int, default=3, help="how many top artists/tracks to show")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_secrets()
    key = args.key or os.environ.get("LASTFM_API_KEY")
    user = args.user or os.environ.get("LASTFM_USER")
    if not key or not user:
        return note("no credentials — set LASTFM_API_KEY and LASTFM_USER in "
                    "~/.config/llm-land-mcp/secrets.env (see mcp-servers/secrets.env.example)")

    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            return note(f"bad --date {args.date!r} — expected YYYY-MM-DD")
    else:
        day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if not args.today:
            day -= timedelta(days=1)
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = int(day.timestamp())
    end_ts = int((day + timedelta(days=1)).timestamp())

    try:
        tracks, now_playing = fetch_day(key, user, start_ts, end_ts, args.timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = json.loads(e.read()).get("message", "")
        except Exception:
            pass
        return note(f"API returned HTTP {e.code}{' — ' + body if body else ''}")
    except Exception as e:
        return note(f"unavailable ({type(e).__name__}: {e})")

    artists = Counter(t["artist"]["#text"] for _, t in tracks)
    titles = Counter(f'{t["artist"]["#text"]} — {t["name"]}' for _, t in tracks)
    hours = Counter(datetime.fromtimestamp(ts).hour for ts, _ in tracks)

    result = {
        "available": True,
        "date": day.strftime("%Y-%m-%d"),
        "user": user,
        "scrobbles": len(tracks),
        "distinct_artists": len(artists),
        "distinct_tracks": len(titles),
        "top_artists": artists.most_common(args.top),
        "top_tracks": titles.most_common(args.top),
        "first_scrobble": datetime.fromtimestamp(tracks[0][0]).strftime("%H:%M") if tracks else None,
        "last_scrobble": datetime.fromtimestamp(tracks[-1][0]).strftime("%H:%M") if tracks else None,
        "hourly": {f"{h:02d}": hours.get(h, 0) for h in range(24) if hours.get(h)},
        "now_playing": (f'{now_playing["artist"]["#text"]} — {now_playing["name"]}'
                        if now_playing else None),
    }

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"### Music — Last.fm ({result['date']})")
    if not tracks:
        print(f"- **no scrobbles** for {result['date']}"
              + (f" · now playing: {result['now_playing']}" if result["now_playing"] else ""))
        print("\n_Nothing scrobbled is not the same as nothing listened to — offline play, or a "
              "broken Spotify↔Last.fm link, both look like this._")
        return 0

    print(f"- **{result['scrobbles']} scrobbles** · {result['distinct_artists']} artists · "
          f"{result['distinct_tracks']} tracks · {result['first_scrobble']}–{result['last_scrobble']}")
    top_a = " · ".join(f"**{a}** ({n})" for a, n in result["top_artists"])
    print(f"- top artists: {top_a}")
    if result["top_tracks"] and result["top_tracks"][0][1] > 1:
        top_t = " · ".join(f"{t} ({n})" for t, n in result["top_tracks"] if n > 1)
        print(f"- on repeat: {top_t}")
    peak = max(hours.values())
    bars = " ".join(f"{h}{'▁▂▃▄▅▆▇█'[min(7, (hours[h] * 8 - 1) // peak)]}"
                    for h in sorted(hours))
    print(f"- by hour (local): {bars}")
    if result["now_playing"]:
        print(f"- ▶ now playing: {result['now_playing']}")
    print("\n_Hours are local, so this lines up with the RescueTime timeline — useful for "
          "'what was playing during that block'. Scrobble count only; the API carries no track "
          "duration, so no listening-time figure is invented._")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:                                # never take out the /daily run
        sys.exit(note(f"unexpected failure ({type(e).__name__}: {e})"))
