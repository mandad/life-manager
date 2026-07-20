# Life Manager

A personal life-management system run by [Claude Code](https://claude.com/claude-code) on top of
a private Obsidian vault: a daily dashboard curated from per-project task lists, a quick-capture
inbox, `/daily` and `/weekly` routines that sync tasks and pull personal data (health, computer
time, location, weather, notes), an asynchronous question channel, an automation backlog Claude
proposes into and builds from, and a work log that compiles straight into performance reporting.

The vault itself is private and never tracked. **This repository publishes the code and the
starter template** — enough for anyone to instantiate their own instance.

## 🚀 Start your own

```bash
git clone https://github.com/mandad/life-manager
cd life-manager
claude   # then say: "set up my life manager"
```

Claude reads [`template/SETUP.md`](template/SETUP.md), interviews you (~10 minutes: your
projects, which data sources you have, what optional modules you want), and instantiates a
personalized vault + `CLAUDE.md` from [`template/vault/`](template/vault/). Your vault is
git-ignored automatically — personal data never enters the repo. Data-source integrations are
optional and pluggable; the routines run fine with none wired.

## What's here

| Directory | What it is |
|-----------|------------|
| [`template/`](template/) | The releasable starter: vault skeleton, genericized daily/weekly routines, CLAUDE.md template, and the Claude-guided setup playbook. |
| [`mcp-servers/`](mcp-servers/) | Local [Model Context Protocol](https://modelcontextprotocol.io) servers that expose personal data sources (health, location, weather, notes, computer usage) to Claude. |
| [`scripts/`](scripts/) | Python automation supporting the daily/weekly routines — data pulls, renderers, vault maintenance, hooks. Vault path configurable via `LIFE_VAULT_DIR` (default `AI Scratchpad`); memory paths auto-derive from the repo location. |
| [`ship-locator/`](ship-locator/) | A user-space pusher + DreamHost relay serving a ship's live NMEA position — the author's mobile-location source, kept as a worked example of a custom data feed. |

The author's own Obsidian vault (`AI Scratchpad/`), drafted documents, CSV/PNG exports, and
local tool config live in the working tree but are deliberately git-ignored.

## MCP servers

Single-user, mostly read-only servers. Each is a standalone TypeScript package with its own
`README.md` and `package.json`; build with `npm install && npm run build` inside the server dir.

| Server | Source | Purpose |
|--------|--------|---------|
| [`foursquare-checkins`](mcp-servers/foursquare-checkins/) | Foursquare/Swarm v2 user API | Personal check-in history (where/when/how often). |
| [`nws-forecast`](mcp-servers/nws-forecast/) | NWS `api.weather.gov` | Multi-day + hourly forecast, marine forecast, active alerts for a lat/lon. Free, no auth. |
| [`onenote`](mcp-servers/onenote/) | Microsoft Graph | Read OneNote (typed text, ink→PNG for OCR, page screenshots). Device-code auth, no client secret. |
| [`rescuetime`](mcp-servers/rescuetime/) | RescueTime Analytic Data API | Computer-usage productivity data (pulse, app/category breakdowns, hourly trends). |
| [`whoop`](mcp-servers/whoop/) | Whoop API v2 | Recovery, sleep, strain, workouts. OAuth 2.0, auto-refreshing token. |

### Configuration

`mcp-servers/mcp-manifest.json` is the single source of truth for which servers run and which
env vars each needs. `scripts/mcp-sync.py` reads it and generates both the Claude Code (WSL) and
Claude Desktop (Windows) configs in WSL-bridge mode.

Secrets are **never** committed. Copy the template, fill it in outside OneDrive/git, and lock it
down:

```bash
mkdir -p ~/.config/llm-land-mcp
cp mcp-servers/secrets.env.example ~/.config/llm-land-mcp/secrets.env
chmod 600 ~/.config/llm-land-mcp/secrets.env
# fill in real values, then:
python3 scripts/mcp-sync.py
```

## Scripts

Invoke from the repo root: `python3 scripts/<name>.py`.

**Daily/weekly data pulls**
- `whoop-refresh.py` — headless Whoop pull (refreshes the stored token; sidesteps the flaky MCP).
- `scs-weather.py` — the ship's own met/sea sensor observations from the public NOAA SCS shore page.
- `synoptic-obs.py` — nearby marine/coastal observations from the Synoptic Data API.
- `tomorrow-forecast.py` — hi-res point forecast from tomorrow.io v4.
- `aurora-forecast.py` — geomagnetic/aurora forecast; alerts when Kp is expected to exceed a threshold (free NOAA SWPC feeds).
- `ship-position.py` — latest fix or full time series from the ship-locator relay.
- `rescuetime-timeline.py` — render a RescueTime activity dump as a compact hourly timeline.
- `strava-whoop-reconcile.py` — reconcile a day's Strava activities against Whoop workouts.

**Vault maintenance & routines**
- `defer-pick.py` — rotate 1–2 Defer items into today's list.
- `regen-memory-digest.py` — regenerate the auto-memory `MEMORY.md` index and the vault-side memory digest.
- `weekly-prep.py` — mechanical inputs for the weekly review (broken links, orphans, deadline radar, etc.).
- `import-sailing-schedule.py` — import the FY26 *Fairweather* sailing schedule (`.ods`) into a vault doc.
- `inbox-triage-hook.py` — PostToolUse hook validating the `Inbox.md` "Last triaged" line.
- `mcp-sync.py` — generate Claude Code + Claude Desktop MCP configs from the manifest.

## ship-locator

Pushes *Fairweather*'s live NMEA position off the ship's work PC (user-space, no admin,
outbound 443 only) to a token-gated DreamHost relay, which serves the latest fix to the daily
routine and logs a time series to SQLite for later query. Position is public-equivalent (already
on Windy by call sign); the relay token-gates writes/reads over HTTPS so only the owner touches
it. See [`ship-locator/README.md`](ship-locator/README.md) for the full setup.

## Publishing model

The `.gitignore` is a **whitelist**: ignore everything at the top level, re-include only
`scripts/`, `mcp-servers/`, and `ship-locator/`, then strip secrets, dependencies, and build
artifacts back out even inside those directories. Personal data is never tracked.

A `.gitignore` can't protect a secret hard-coded in source, so the code was audited (2026-06-18):
the scripts and relay read tokens only from env vars / external config, never literals. Keep it
that way — no `tokens.json`, `.env`, real `*.config.php`, or `.mcpb` bundles get committed.

## License

[MIT](LICENSE).
