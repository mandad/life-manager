# google-tasks MCP server

**Read-only** access to Google Tasks for a work Google account, over MCP.

Built for the LLM-Land `/daily` routine (automation #34): the vault could see command work
only through what the user jotted, what OneNote captured, and what telemetry implied — the
actual work task list lived somewhere it had never been able to read.

## Scope: `tasks.readonly`, and nothing else

That is a **different OAuth scope from Gmail**. Granting it yields the task list and **no
mailbox access whatsoever**, even though the items themselves originate as email flags.

The constraint is enforced by construction rather than by promise: **there is no write path in
this code**. Nothing here can create, edit, complete or delete a task. Read-write would be a
deliberate change to both the requested scope and the tool surface, not a config flag.

## Tools

| Tool | Purpose |
|---|---|
| `google_tasks_list_tasklists` | The account's task lists — start here for list ids |
| `google_tasks_list_tasks` | Tasks in one list. `due_min` / `due_max` / `updated_min` filters; open-only by default; `@default` works as a list id |
| `google_tasks_search` | Client-side search across every list — the Google Tasks API has **no** search endpoint, so this enumerates and matches title + notes |

## Auth

OAuth 2.0 **installed-app (loopback) flow with PKCE**, not device code — Google's device flow
supports only a restricted scope set that does not include Tasks.

`npm run auth` starts an ephemeral `127.0.0.1` listener, prints the authorization URL, catches
the redirect, and writes a `chmod 600` token cache at `GOOGLE_TASKS_TOKEN_PATH`
(default `~/.config/llm-land-mcp/google-tasks-token.json` — outside the repo and outside
OneDrive). The server refreshes silently thereafter. The token is never printed.

Credentials come **only** from environment variables (`GOOGLE_TASKS_CLIENT_ID`,
`GOOGLE_TASKS_CLIENT_SECRET`, `GOOGLE_TASKS_TOKEN_PATH`), sourced from the shared
`secrets.env`. No credential is ever hard-coded — audited 2026-08-16.

## Setup

Full walkthrough, including the Google Cloud Console steps and a troubleshooting table:
**[SETUP.md](SETUP.md)**.

```bash
npm install
npm run build
npm run auth      # one-time, interactive
```

Then `python3 scripts/mcp-sync.py` from the repo root and restart Claude Code.

## Notes

- If a Workspace admin blocks unconfigured third-party apps, the consent screen **refuses and
  grants nothing** — the safe failure. The fix is one allowlist entry for the OAuth client ID.
  No code signing is involved anywhere in this path.
- Paging is capped (20 pages / 500 items) so a runaway list cannot hang a `/daily` run.
