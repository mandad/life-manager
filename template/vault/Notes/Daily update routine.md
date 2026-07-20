---
title: Daily update routine
purpose: How the Today section in My Day.md is built, synced, rolled forward, and learned from each day
---

# Daily update routine

The root [[../My Day|My Day]] dashboard holds a **Today** section curated from across active projects. This note documents the procedure Claude follows to update it each day, including a learning step that captures patterns over time. **This is a living document** — when the user corrects the routine, the correction gets written in here (dated) so it never repeats.

## How to invoke

Say any of: "/daily" · "update today's tasks" · "refresh today" · "run daily update".

## Procedure

Order: catch-up sync (1–2) → triage (3) → plan today (4–5) → cleanup (6) → data pulls (7) → persist + reflect (8–10) → report (11).

### Step 1 — Sync completed items from yesterday's Today back to project files

For every `- [x]` item in the current Today section, find the corresponding line in the linked source project file and mark it `- [x]` there (append `— done YYYY-MM-DD` where the file uses dated completions). Items with no project source get recorded under Done today only.

**⚠️ Checkbox wins over sub-bullet prose.** A checked `- [x]` parent is **COMPLETE**, even when a sub-bullet note reads as partial ("still need to…") — sub-bullets are often stale mid-day jots written *before* the user finished and checked the parent. Never silently keep a checked item open on the strength of its note text or absent telemetry. Sync it as done; if the contradiction seems genuinely material, sync done AND confirm via [[../Daily update questions]].

### Step 2 — Sync completions made directly in project files

If the user checked something off in a project file since the last run, reflect it in Today's **Done** subsection so it's visible before archiving.

### Step 3 — Triage [[../Inbox|Inbox]] + the dashboard capture

For each item under **New items** in `Inbox.md`: decide task / note / doc / trash; update the right target file (project Tasks, `Notes/`, `Documents/`); then clear New items and stamp **Last triaged:** with date AND time (run `date` first — never reuse a stale timestamp). **Not skippable** — check even when it looks empty. Unsure where something belongs → ask in [[../Daily update questions]] rather than guessing.

**Dashboard capture:** [[../My Day]] has a persistent **"🗒️ Noted today"** section — the user's quick-capture of **work they got done during the day** (NOT a to-do inbox). For each item: reword it supervisor-readable (see [[Performance reporting guide]] if active — verb-first, quantified, tagged) into the **"✅ Completed (to log)"** buffer, update the matching project task, then clear the section to a single empty `-` bullet. Genuine new to-dos found here get routed like Inbox items.

**⚠️ Merge-before-rebuild.** Step 4 rewrites My Day with a full-file write. Before EVERY rebuild, **re-read the live file's "Noted today" and "Completed" sections from disk in the same run** — never rely on a cached view or truncated diff. If the file can't be re-read, splice around those sections with targeted edits instead of rewriting them. User capture is sacred; losing a day's jots is the worst failure this routine has.

### Step 4 — Build the new Today list

Replace the Today section with today's date and a fresh curated list. Sources in priority order: (1) date-specific entries in any project's sequencing block; (2) items tagged `#today`; (3) explicit deadlines matching today; (4) recurring habit items on their cadence; (5) urgent outstanding items if there's bandwidth.

Each Today line: mirrors the source text (so exact-match sync works), links back to the source file, carries its tags, and carries an effort estimate `(15m)`, `(1hr)`, `(half-day)` — overestimate rather than under.

**Capacity rule (3-2-1):** Pinned max 3 · Surfacing-now max 2 · Squeeze-in max 1. Recurring habits and Defer don't count. On light days, fewer is fine — the cap forces ranking on crowded days.

**Name #1.** Pinned items are ordinally ranked; `#1` is the "if you only finish one thing today" item, called out by name in the Step 11 report — the recovery anchor when the day fragments.

**Effort × block-fit.** Weigh effort estimates against the day's actual free blocks, not just priority. A `(15m)` hard-deadline item with a lunch window → pin. A `(half-day)` item on a fragmented day → defer to a day that can hold it. Sum of Pinned + Surfacing effort should fit the day's free hours minus an interrupt buffer.{{#if wearable}} On low-recovery days, size down — pull only short items.{{/if}}

**Defer rotation.** Each Defer line carries `(deferred-since: YYYY-MM-DD)`; when pulled into a day, add `(last-surfaced: YYYY-MM-DD)`. Each run selects 1-2 Defer items back into the actionable buckets — block-fit beats age, then oldest first; skip items surfaced within the last 2 days that didn't complete. **Items stuck 14+ days trigger a probe question:** "Be specific about why this hasn't happened — wrong framing, wrong estimate, wrong priority, hidden dependency, or actually low-value?" Don't accept "no time" as the answer.

{{#if flourish}}
**{{FLOURISH_NAME}}.** Generate one short item for the {{FLOURISH_CONTEXT}} and place it in the dashboard block. Registry at `Notes/_daily-data/{{FLOURISH_SLUG}}-registry.md` — **read it first every run**: honor 👍/👎 ratings and the preferences-learned block, **check the user's Queued-ideas table before generating fresh** (users add queue rows asynchronously), rotate categories (don't repeat the last 1-2), append the log row with blank rating, and roll new ratings up into preferences. If it can't be verified, pick something safer — accuracy over novelty.
{{/if}}

### Step 5 — Deadline radar (next 7 days)

Sweep all active project Tasks files for explicit deadlines/sequencing dates in the next 7 days not already surfaced. <3 days out + ≥1hr effort → Surfacing-now candidate (cap 2, rank by tightness × effort). 3-7 days out → one-line radar mention. >7 days → the weekly routine's 30-day radar owns it. The radar surfaces; it never reschedules or pins by itself.

### Step 6 — Archive completed work

Move yesterday's Done items into the archived-day entries (keep the last ~4 days on the dashboard). Scan for documents whose load-bearing purpose is done → archive per the CLAUDE.md convention. **Projects are never archived without user confirmation** — ask in [[../Daily update questions]].

### Step 7 — Data pulls (parallel sub-agent fan-out)

The connected data sources are independent — spawn **one sub-agent per source in parallel** (cheaper model is fine for mechanical fetch+summarize), then consume their digests. Each sub-agent prompt contains: the exact date, the tool calls + arguments, what to look for, the digest shape to return, and **an explicit "do not write any files"** (sub-agents return text; the main agent owns all writes).

Per-source sub-steps live below — added/removed as the user connects sources (see `template/SETUP.md` source recipes for the generic shapes):

{{PER_SOURCE_SUBSTEPS — wired during setup; e.g. 7.1 wearable, 7.2 computer-time, 7.3 location, 7.4 activities, 7.5 weather, 7.6 notes-scan}}

**Standing rules for all sources:**
- **Correlation never auto-checks a task.** A telemetry match (app usage, check-in, workout) is *suggestive* — append a question ("Did X get done? Inferred from Y") instead of marking done.
- **Absence of telemetry ≠ not done.** Verify a task's actual completion mechanism before using any data domain as its proxy; when a source returns nothing or errors, probe (retry / ask) before recording a slip or declaring the source dead.
- Auth failures surface as plain text in the report; two-in-a-row → ask whether to swap sources.

### Step 8 — Persist daily data

Write `Notes/_daily-data/YYYY-MM/YYYY-MM-DD.md` — **filename = yesterday** (the date the file describes); one file per run; month folder created as needed. Sections: one per data source + a Patterns/observations block. **Non-skippable, and it happens before the report** — the report is ephemeral; this file is the durable record.

### Step 9 — Observe and learn

- **Patterns → memory.** Repeated rollovers (3+ days → question), quick-win shapes, time-of-day clustering, reframings, friction sources. Durable patterns get written to auto-memory without asking; genuine questions go to [[../Daily update questions]].
- **Mechanical patterns → [[Automation backlog]].** Propose 0-3 candidates per run (what / why / mechanism / effort). **Check the Approved section EARLY each run** — a newly-checked item gets built this run, and a missed build is a process failure.
- **Blind-spot probe.** Before the report, ask: *"What am I assuming that I might be wrong about? What am I reinforcing because it matches the user's priors, not because it's true?"* Surface 0-2 as questions when material.

### Step 10 — Memory digest regen

Run `python3 scripts/regen-memory-digest.py` from the repo root — mirrors auto-memory into [[Memory digest]] so the user can see and correct what's remembered.

### Step 11 — Report

Brief summary: N completed yesterday · today's plan as 3-2-1 with **#1 named** · noteworthy items (deadlines approaching, rollovers, patterns, capacity warnings) · count of new questions · automation proposals pending. Vault files referenced as clickable markdown links. If the last /weekly was >10 days ago, mention it.

## Failure modes to watch

- Rephrased tasks lose their sync link — check both files when a checked item matches nothing.
- Items rolling over repeatedly get stale — surface as stuck, don't silently re-pin forever.
- Direct project-file edits between runs are picked up next run, not in real time.
