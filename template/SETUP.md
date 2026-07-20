# Setup — instantiate your Life Manager

**For the user:** clone this repository, open [Claude Code](https://claude.com/claude-code) at the repo root, and say **"set up my life manager"**. Claude reads this file and walks you through the rest. Total setup is a ~10-minute interview plus a few minutes of file generation.

**For Claude:** when a user asks to set up their life manager (or points you here), follow this playbook. Interview first, then instantiate. Don't copy files before the interview — the answers decide what gets included.

---

## What you're instantiating

A personal life-management system with three parts:

1. **An Obsidian vault** — the user-facing surface: a daily dashboard (`My Day.md`), quick-capture inbox, notes, drafted documents, per-project task lists, and an archive. Claude reads and writes it; the user browses it in Obsidian (or any Markdown editor).
2. **Two Claude-run routines** — `/daily` (build today's plan from all projects, sync completions, pull personal data, learn) and `/weekly` (vault hygiene, 30-day deadline radar, work-log compilation). Documented in the vault itself so the user can read and tune them.
3. **A feedback loop** — a questions file the user answers asynchronously, an automation backlog Claude proposes into and builds from, and persistent memory so lessons survive across sessions.

The vault is **git-ignored by design** — personal data never enters the repository. Only code and templates are tracked.

## Phase 1 — Interview

Ask conversationally or with structured questions. Gather:

1. **Vault name.** Default `AI Scratchpad` (works with the scripts out of the box). Any other name works too — set `LIFE_VAULT_DIR=<name>` in the user's shell profile so the scripts find it.
2. **About the user.** Role/occupation and any domain context Claude should recognize without explanation (jargon, acronyms); timezone/location; household point-of-contact if someone else handles things when they're away. → feeds the CLAUDE.md user-context section.
3. **Active projects.** 1–4 to scaffold now (a move, a certification, home maintenance, a work role...). For each: name, one-line objective, any hard deadlines. More projects can be added organically later.
4. **Data sources** (each optional — wire only what the user actually has):
   - **Wearable / recovery** (Whoop, Garmin, Oura...) — powers the daily body/readiness read. This repo includes a Whoop MCP server (`mcp-servers/whoop/`).
   - **Computer time** (RescueTime...) — hourly activity timeline; corroborates work done. Server included (`mcp-servers/rescuetime/`).
   - **Location / check-ins** (Foursquare/Swarm...) — errand confirmation, travel signal. Server included (`mcp-servers/foursquare-checkins/`).
   - **Activities** (Strava...) — workout log. Available as a claude.ai connector.
   - **Notes system** (OneNote, Notion...) — scan for new notes, extract actions. OneNote server included (`mcp-servers/onenote/`).
   - **Weather** — forecast for a fixed home point (simple) or a moving location (see the ship-tracking scripts for the mobile pattern). NWS server included (`mcp-servers/nws-forecast/`, US-only, no auth).
5. **Daily flourish** (optional). A daily short item Claude generates for a context the user chooses — the original was a nightly fun fact for a ship's night orders; other shapes: a briefing opener for a team, a fact for the family dinner table, a daily learning prompt. Comes with a 👍/👎 rating loop so it improves. Skip if not wanted.
6. **Work log / performance reporting** (optional but recommended for anyone with an annual review). A weekly supervisor-readable accomplishments log. If the user's job has a written review framework (evaluation categories + descriptors), offer to build a personalized language guide from it — ask them to paste or point to the framework, then adapt `vault/Notes/Performance reporting guide.md`.
7. **Cadence.** When do they want /daily (morning default) and /weekly (Friday afternoon / Sunday evening typical)? Note: both are user-invoked, never automatic.

## Phase 2 — Instantiate

1. **Copy the vault skeleton**: `template/vault/` → `<vault name>/` at the repo root. Never overwrite an existing directory — if one exists, stop and ask.
2. **Fill placeholders.** Every `{{PLACEHOLDER}}` in the copied files and in `template/CLAUDE.template.md` gets replaced from the interview. Write the completed CLAUDE template to `./CLAUDE.md` at the repo root.
3. **Prune skipped modules.** If no wearable: remove the Body/readiness section from `My Day.md` and the wearable sub-step from the daily routine. If no flourish: remove the flourish block and its registry reference. If no performance framework: leave the generic guide but note in it that it's dormant. Remove data-source sub-steps for sources the user doesn't have.
4. **Scaffold projects.** For each interviewed project, create `<vault>/Projects/<Name>/Overview.md` + `Tasks.md` from the Example Project pattern, then delete `Projects/Example Project/` and list the real projects in `My Day.md` → Active projects and `Projects/` links.
5. **Vault name ≠ default?** Tell the user to add `export LIFE_VAULT_DIR="<name>"` to their shell profile, and add the same fact to CLAUDE.md so future sessions know.
6. **Wire data sources.** For each chosen source: point the user at the matching `mcp-servers/<name>/README.md` (or connector) for auth/registration, then write the per-source sub-step into the daily routine's Step 7 using the source recipes below. Sources can be wired later — the routine tolerates missing sources.
7. **Gitignore check.** The repo `.gitignore` ignores everything top-level by default, so a custom-named vault is automatically untracked — verify with `git status` (the vault must NOT appear).
8. **First run.** Do a mini `/daily`: triage the (empty) inbox, build a first Today list from the scaffolded projects, write the first `Notes/_daily-data/<month>/<date>.md`, and show the user the report format. Fix anything that feels off *now* — the routines are theirs to tune.

## Phase 3 — How it evolves (tell the user)

- **The routines are living documents.** When the user corrects Claude, the fix gets written into the routine or memory — the system should never make the same mistake twice.
- **The Automation backlog** (`Notes/Automation backlog.md`) is the improvement loop: Claude proposes mechanical automations; the user checks a box to approve; Claude builds it on the next run.
- **Questions flow asynchronously** through `Daily update questions.md` — Claude appends, the user answers whenever, the next run acts on answers.
- **Memory** persists in Claude Code's auto-memory; `scripts/regen-memory-digest.py` mirrors it into the vault (`Notes/Memory digest.md`) so the user can see and correct what's remembered.

## Source recipes (for Step 7 of the daily routine)

Generic shapes — adapt names/tools to the user's actual source. Each sub-step states: what to pull, what to look for, where insights go. Spawn one sub-agent per source in parallel; sub-agents return text only and **never write files**.

- **Wearable/recovery:** pull today's recovery (anchored on last night's sleep — be explicit about the date), last night's sleep metrics, yesterday's strain/workouts. Look for: multi-day HRV/RHR trends, sleep-vs-schedule patterns, recovery-vs-load conflicts. Feeds the body/readiness read + recovery-aware task sizing. Never auto-diagnose from one day.
- **Computer time:** pull yesterday's daily summary + hourly breakdown; build an hourly timeline (top 1-2 apps per hour). Look for: blocks corroborating tasks, unaccounted productive time, distraction windows, last-activity-of-night (sleep cross-check). **Never mark a task done from telemetry** — surface a question instead.
- **Location/check-ins:** pull yesterday's check-ins (filter client-side by date string — don't pass computed timestamps to APIs). Look for: errand confirmation, timing calibration, travel signals. Same rule: suggest, never auto-complete.
- **Activities:** pull yesterday's workouts; reconcile against the wearable when both exist. Look for: unlogged activity, effort vs. baseline.
- **Weather:** forecast at the user's point (or moving location). Look for: alerts, windows that gate outdoor/plans, multi-day patterns worth planning around.
- **Notes system:** scan for new/changed pages since a watermark (store it in `Notes/_daily-data/`); extract candidate actions → Inbox with provenance. Beware: some platforms don't bump modified-timestamps on edits — keep a small watchlist of live pages and re-read them every run.

## Hard-won rules (bake these in — they're written into the template routines)

1. **Checkbox wins over prose.** A checked `- [x]` parent task is COMPLETE even if a sub-bullet note reads as partial — notes are often stale mid-day jots.
2. **Absence of telemetry ≠ not done.** Verify a task's actual completion mechanism before using any data source as its proxy; when a source returns nothing, probe before concluding.
3. **Merge before rebuild.** Any full-file rewrite of the dashboard must re-read the live file's persistent capture sections from disk in the same run — never trust a cached view. User capture is sacred.
4. **Correlation never auto-checks a task.** Data suggests; only the user confirms.
5. **Fresh timestamps.** Run `date` immediately before writing any timestamp into the vault.
6. **Questions go in the questions file,** not buried in reports — the user answers asynchronously.
7. **Hard deadlines stay hard.** A casual "this can slip" about an externally-imposed date is usually a coping hedge — keep the date real and mark it overdue when missed.
