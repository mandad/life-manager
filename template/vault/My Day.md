# My Day

My daily dashboard — today's plan{{#if wearable}}, body/readiness{{/if}}{{#if weather}}, weather{{/if}}{{#if flourish}}, and the daily {{FLOURISH_NAME}}{{/if}} — plus the master task index. The **Today** section is curated from across active projects each day; see [[Notes/Daily update routine]] for how it's built and synced.

---

## Today — {{DAY YYYY-MM-DD}}

*One-to-three short paragraphs written fresh each /daily: yesterday's headline (what got done, what the data showed), anything time-critical today (weather window, deadline, event), and the body read if a wearable is wired. Written for the user to scan in 30 seconds.*

{{#if wearable}}
### Body / readiness

| Date | Recovery | Key metric | Sleep | Notes |
|---|---|---|---|---|
| *(7-day rolling window — /daily drops the oldest row and appends today's)* | | | | |

**Read:** *one-or-two-sentence honest interpretation — trend over single days; recovery-aware task sizing feeds the Pinned picks below.*
{{/if}}

{{#if flourish}}
### {{FLOURISH_EMOJI}} {{FLOURISH_NAME}} (for {{FLOURISH_CONTEXT}})

> *(/daily places one short item here each day — rotating categories, no repeats. Rate past ones 👍/👎 in [[Notes/_daily-data/{{FLOURISH_SLUG}}-registry]] to shape future picks.)*
{{/if}}

### Recurring

*(daily/weekly habit lines, if any — these don't count against the 3-2-1 cap)*

### 🟢 Pinned — next 1-2 days (3-2-1)

- [ ] **#1 — {{the "if you only finish one thing today" item}}** *(effort estimate)* — why-now context. #tag → [source Tasks file](Projects/)
- [ ] **#2 — ...**
- [ ] **#3 — ...**

### Surfacing now (deadline radar — max 2)

- [ ] *(deadline-driven items from the 7-day radar, ranked by tightness × effort)*

### Squeeze in if energy (1)

- [ ] *(one nice-to-do)*

*Radar: one-line mentions of items 3-7 days out that didn't make the cut — visible, not scheduled.*

### 🗒️ Noted today — work done today, to be logged

*Jot things you **got done / handled** during the day here — quick capture feeding the [Work log](Documents/Work%20log.md). /daily rewords each into a supervisor-readable line in the ✅ Completed buffer below (plus updates the matching project task), then clears this section. A genuine new to-do is fine here too — it gets routed to the right project file. **Persistent section — the daily rebuild must keep it AND must merge the live file's contents immediately before any rewrite.***

- 

### ✅ Completed (to log) — drained into the [Work log](Documents/Work%20log.md) on /weekly

*Running buffer of finished work items. Drop anything you complete here; /weekly compiles these into the Work log and clears the list. Keep wording supervisor-readable and stable.*

- 

### Carry-over

- [ ] *(multi-week items in active flight — not today, not deferred)*

### Defer / lower priority

*(Each line carries `(deferred-since: YYYY-MM-DD)` and, once surfaced, `(last-surfaced: YYYY-MM-DD)` — the rotation stamps that keep this bucket from becoming a graveyard. /daily pulls 1-2 items back into the day when block-fit and energy allow; items stuck 14+ days trigger a be-specific probe.)*

**Date-bound (exempt from rotation):**
- *(calendar-anchored facts and windows the day plans around — deadlines, travel, events)*

### Done — {{today}}
*(items completed today accumulate here, then archive on the next /daily)*

---

## Active projects

Full catalog: **[[Projects/Index|Projects index]]** *(create when a second project exists)*.

- **[[Projects/Example Project/Overview|Example Project]]** — one-line objective. Tasks: [[Projects/Example Project/Tasks|→]]

## Open questions

- [[Daily update questions]] — Claude generates these from the daily routine; answer when you have a minute.

## Standalone tasks

- [ ] *(one-off tasks that belong to no project)*
