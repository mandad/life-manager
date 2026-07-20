---
title: Weekly review routine
purpose: Periodic vault hygiene + 30-day deadline radar — covers what /daily can't justify doing every morning
companion: "[[Daily update routine]]"
---

# Weekly review routine

/daily keeps the Today list current and pulls the data. This routine handles the slower-moving work: link rot, orphan notes, tag drift, deeper archive scans, a longer deadline horizon, and the **Work log** compilation.

## How to invoke

Say any of: "/weekly" · "run weekly review" · "weekly cleanup" · "vault audit". Cadence is **user-driven, never automatic** — Friday afternoon or Sunday evening are natural slots. If 14+ days have passed, run a shortened version (Steps 1, 4, 7 only) rather than skipping.

## Procedure

Order: vault cleanup (1–5) → records (6–8) → reflection (9) → telemetry (10) → report (11).

### Step 1 — Broken `[[wikilinks]]`

Run `python3 scripts/weekly-prep.py --today YYYY-MM-DD` from the repo root — it emits broken links, orphan candidates, stale files, tag census, file counts with deltas, and the bucketed deadline radar in one pass. Fix unambiguous breaks (renames, archive moves) in place; ask about ambiguous ones via [[../Daily update questions]].

### Step 2 — Orphan and stale notes

Orphans (zero inbound links from active files): keep if genuinely standalone reference, re-link if they lost their home, archive-candidate if their purpose is done. Stale (60+ days unmodified, excluding always-current files): surface with a proposed disposition; don't auto-archive.

### Step 3 — Duplicate / near-duplicate scan

Filename edit-distance, shared headings, shared first paragraph. Surface candidate pairs; **never merge automatically** — ask which is canonical.

### Step 4 — Tag hygiene

From the prep script's tag census: case drift (pick canonical, replace globally after confirming the hits are in live files, not meta-docs quoting examples), singletons (typo or one-off?), retired workflow tags, near-synonyms. Confirm before bulk-replacing — two similar tags may be deliberately distinct.

### Step 5 — Archive candidates (deeper than /daily)

Documents with no edits and no inbound links for 30+ days; projects where every task is `[x]` AND the objective has resolved (**full-project archives always need user confirmation** via the questions file); notes that hardened from capture into stable reference (move to the right folder).

### Step 6 — Memory digest regen

`python3 scripts/regen-memory-digest.py` — weekly backstop in case a /daily skipped it. Note entry-count delta in the report.

### Step 7 — 30-day deadline radar

From the prep script: bucket every dated task into overdue / 0-7 days (cross-check against /daily's radar — a miss there is a process bug worth noting) / **8-30 days (this routine's distinctive contribution)**. For each: source, date, days-until, status.

**Anchor vague dates.** The radar only parses day-anchored dates — targets written as "2026-07", "Fall", "late July" are invisible to it. The prep script flags them; rewrite each with a concrete day anchor (end-of-month for month-only; confirm with the user when the anchor choice matters).

### Step 7b — Monthly deep pass (first /weekly of each calendar month)

Check: is this run's month different from the last `_weekly-data/*.md` file's? If yes: (1) **project-plan schedule health** — compare each project's phase targets against the calendar, flag silent drift; (2) **31-90-day radar extension**; (3) **memory content audit** — re-read the ~5 oldest memory files, delete/update stale ones; (4) **recurring costs** — anything renewing this month that shouldn't?; (5) **portfolio check** — zombie projects, missing projects. ~15 minutes of scan-and-flag, not a rebuild.

### Step 8 — Work log{{#if perf_guide}} + performance-record{{/if}} compilation

Append a new week-section to [[../Documents/Work log]]. **Sources, de-duplicated:** (1) the **"✅ Completed (to log)" buffer** in [[../My Day]] — the primary feed; drain it into the log and clear it; (2) completed `- [x]` tasks since the last run, filtered to work-relevant tags; (3) work the user noted elsewhere with no task behind it.

Write bullets per {{#if perf_guide}}[[Performance reporting guide]] — verb-first, quantified, impact-stated, category-tagged{{else}}the Work log's format note — verb-first, quantified, supervisor-readable{{/if}}. Group related items into one bullet when they tell a single story; mark the period-defining item ⭐. Skip personal admin — the log is supervisor-facing. If the week genuinely had no work output, say so honestly; don't pad.

*(Optional companion: a private reflection journal — decisions + why, lessons, things to tell a successor — drafted weekly right after the Work log. Different audience, same rhythm. Wire during setup if wanted.)*

### Step 9 — Automation reflection + blind-spot probe

Look at the **week's accumulated work** for cross-cutting mechanical patterns no single /daily would surface. Bar for proposing: mechanical, repeated 2+ times or weekly+, computer-bound. Cap 0-3 per run; prefer surfacing existing unaddressed proposals over inventing new ones. → [[Automation backlog]] Proposed, with what / why / mechanism / effort.

**Blind-spot probe:** *"What did the agent reinforce all week because it matched the user's framing, not because it was true? Which pattern got rationalized rather than questioned?"* 0-2 candidates in the report; name the assumption explicitly so the user can break it.

### Step 10 — Vault growth snapshot

File counts by folder (from the prep script) with deltas vs. last run — spots bloat and drift.

### Step 11 — Report + persistence

Write `Notes/_weekly-data/YYYY-MM-DD.md` (run stamp, findings, radar snapshot, work-log summary, growth deltas) — the durable record. Then report: links fixed · orphans/dupes flagged · tags normalized · archive candidates · digest delta · **the full 30-day radar** · work-log week summary · automation proposals · questions added.

## Failure modes to watch

- Aggressive tag auto-fixes can merge deliberately-distinct tags — confirm first.
- "No inbound wikilinks" ≠ "no value" — notes may be referenced from outside the vault.
- The radar surfaces, never reschedules — moving items into the day is the user's call.
