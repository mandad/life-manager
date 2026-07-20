# {{REPO_OR_SYSTEM_NAME}}

Personal workspace whose primary content is an Obsidian vault used as the working surface for tasks, notes, and drafted deliverables.

## Directory layout

- `{{VAULT_NAME}}/` — Obsidian vault (the primary working area)
  - `My Day.md` — daily dashboard (today's plan{{#if wearable}} + body/readiness{{/if}}{{#if weather}}, weather{{/if}}) and master task index pointing to active project task lists.
  - `Inbox.md` — quick capture for triage
  - `Notes/` — knowledge / reference notes (with `Index.md`)
  - `Documents/` — drafted deliverables: memos, plans, lists, research docs (with `Index.md`)
  - `Projects/` — multi-task project folders; each contains `Overview.md` + `Tasks.md`
  - `Archive/` — items moved out of active rotation. Mirrors the active layout so Obsidian search still finds everything. See `Archive/Index.md`.
- `scripts/` — automation scripts supporting the routines (memory digest regen, weekly vault prep, etc.). Lives outside the vault so Obsidian doesn't index it. Invoke from the repo root: `python3 scripts/<name>.py`.{{#if custom_vault_name}} Vault directory is named `{{VAULT_NAME}}` — `LIFE_VAULT_DIR` is set accordingly in the shell profile.{{/if}}

## Working preferences

- **Use the vault for user-facing artifacts.** Drafted documents, task lists, notes — anything the user will read/browse outside of chat — write into `{{VAULT_NAME}}/`. Do not leave important deliverables only in the chat transcript.
- **Use Obsidian conventions:** `[[wikilinks]]` between notes, `#tags`, YAML frontmatter for metadata, `- [ ]` / `- [x]` for tasks.
- **Evolve the structure organically.** Add folders for new projects, split files when they grow unwieldy, keep indexes current. Small organizational moves don't need permission; surface bigger restructures before doing them.
- **Keep filenames human-readable.** Spaces and Title Case are fine — this is for human consumption in Obsidian, not code.
- **Don't mirror tasks in TodoWrite.** The vault is the source of truth for tasks.
- **Archive completed / superseded items.** When a doc's load-bearing purpose is done, move it to `Archive/<original-folder>/`, add `archived: YYYY-MM-DD` + `archived_reason:` frontmatter, update `Archive/Index.md`, and repoint inbound `[[wikilinks]]`. Active indexes drop archived entries.

## Active projects

{{ACTIVE_PROJECTS_LIST — one line per project: name, one-line objective, key dates, link to its folder}}

## Daily task routine

The root `{{VAULT_NAME}}/My Day.md` has a **Today** section curated daily from across active projects. When the user says "update today's tasks", "refresh today", "/daily", or similar, follow the procedure in `{{VAULT_NAME}}/Notes/Daily update routine.md`:

1. Sync any `- [x]` items in Today back to their source project files.
2. Pick up any items the user checked directly in project files since last run.
3. Triage the Inbox and the dashboard's "Noted today" capture.
4. Build a fresh Today list (3-2-1 capacity: 3 pinned / 2 surfacing / 1 squeeze).
5. Run the 7-day deadline radar; archive yesterday's Done items.
6. Pull personal data sources (parallel sub-agents) and cross-reference against tasks.
7. Persist the daily-data record; observe patterns and learn; report a brief summary.

When adding new items to a project's Tasks file, prefer wording stable enough to support exact-match sync.

## Weekly review

A separate routine in `{{VAULT_NAME}}/Notes/Weekly review routine.md` covers vault hygiene that doesn't fit in /daily: broken `[[wikilinks]]`, orphan/stale notes, duplicates, tag drift, deeper archive scans, a **30-day deadline radar**, and the **Work log** compilation. Invocation: "/weekly", "run weekly review", "vault audit". Cadence is user-driven; don't auto-trigger. /daily may mention it if more than ~10 days have passed since the last run.

## User context

{{USER_CONTEXT — role/occupation, domain jargon Claude should recognize without explanation, timezone/location, household point-of-contact, anything else durable}}

## Memory

Claude Code's auto-memory directory for this project persists durable cross-session preferences and project context (user role, feedback, project facts). It is separate from this CLAUDE.md but complementary — both load at session start. `scripts/regen-memory-digest.py` mirrors it into `{{VAULT_NAME}}/Notes/Memory digest.md` for the user's review.

## Style

- Concise responses. End-of-turn summaries one or two sentences.
- Reference vault files as markdown links (`[Tasks.md]({{VAULT_NAME}}/Projects/Example/Tasks.md)`) so the user can click through.
- Don't add comments or scaffolding to vault files unless the user asks; treat them as the user's working documents, not code.
- Appending a row to a markdown table (esp. via script): put the new row on the line **immediately after the last data row, no blank line between** — a blank line splits the table in Obsidian.
