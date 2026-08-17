# Google Tasks MCP — setup (READ-ONLY)

One-time, ~10 minutes. Everything below happens **once**; after that the server refreshes its
own token silently.

## What this grants, precisely

**Scope: `https://www.googleapis.com/auth/tasks.readonly` — nothing else.**

That is a *different OAuth scope from Gmail*. Granting it gives this machine your **task list
and no mailbox access whatsoever**, even though the items themselves originate as email flags.
The server implements **no write path at all** — there is no code in it capable of creating,
editing, completing or deleting a task. If you later want write access it is a deliberate
change to both the scope and the tool surface, not a config flag.

The refresh token is cached at `~/.config/llm-land-mcp/google-tasks-token.json`, `chmod 600`,
outside OneDrive, and is never printed.

## 1. Create the OAuth client (Google Cloud Console)

Sign in as the **work Google account** whose tasks you want to read.

1. <https://console.cloud.google.com/projectcreate> → create a project (e.g. `llm-land-daily`).
2. **Enable the API**: APIs & Services → Library → search **"Google Tasks API"** → Enable.
3. **OAuth consent screen**: choose **Internal** if your Workspace offers it (simplest — no
   verification, no test-user list). If only **External** is available, pick it and add your own
   address under *Test users*; an unverified external app still works for its own test users.
   - App name: anything, e.g. `LLM Land daily`. Support/developer email: yours.
   - Scopes: you do **not** need to add scopes here; the client requests `tasks.readonly` at
     runtime.
4. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app.**
   Name it, create, and copy the **Client ID** (and the secret if one is shown).

> **Why "Desktop app":** it enables the loopback redirect (`http://localhost:<port>/callback`),
> which is the supported installed-app flow for arbitrary scopes. Google's device-code flow
> covers only a restricted scope set and does **not** include Tasks.

## 2. Put the credentials in the shared secrets file

```bash
# if you don't already have it
mkdir -p ~/.config/llm-land-mcp
cp mcp-servers/secrets.env.example ~/.config/llm-land-mcp/secrets.env
chmod 600 ~/.config/llm-land-mcp/secrets.env
```

Then fill in:

```
GOOGLE_TASKS_CLIENT_ID=<your client id>.apps.googleusercontent.com
GOOGLE_TASKS_CLIENT_SECRET=<the secret, or leave blank>
GOOGLE_TASKS_TOKEN_PATH="$HOME/.config/llm-land-mcp/google-tasks-token.json"
```

## 3. Authorize (the one interactive step)

```bash
cd mcp-servers/google-tasks
npm install      # if you haven't
npm run build
npm run auth
```

It prints a URL. Open it in a browser **signed in to the work account**, approve, and the local
listener catches the redirect and writes the token cache.

- You will likely see a **"Google hasn't verified this app"** interstitial. That is expected for
  a single-user app on a sensitive scope — click through *Advanced → Go to …*. Verification is
  only required past 100 users.
- **If your Workspace admin blocks unconfigured third-party apps, the consent screen refuses and
  nothing is granted.** That is the safe failure, and the fix is one entry: ask them to
  allowlist this OAuth **client ID** under Admin console → Security → API controls. No code
  signing is involved anywhere in this process.

## 4. Register and restart

```bash
python3 scripts/mcp-sync.py     # picks up the new manifest entry
```

Then **restart Claude Code** — MCP servers load at session start.

## 5. Verify

```bash
node dist/index.js   # should print: google-tasks MCP server running on stdio (READ-ONLY, scope: …)
```

In session, `google_tasks_list_tasklists` should return your lists.

## Tools

| Tool | What it does |
|---|---|
| `google_tasks_list_tasklists` | The account's task lists (start here for list ids) |
| `google_tasks_list_tasks` | Tasks in one list; `due_min`/`due_max`/`updated_min` filters, open-only by default. `@default` works as a list id |
| `google_tasks_search` | Client-side search across every list — the Tasks API has no search endpoint, so this enumerates and matches title + notes |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No Google Tasks token cache at …` | `npm run auth` hasn't been run, or `GOOGLE_TASKS_TOKEN_PATH` differs between the auth run and the server |
| `403 … Google Tasks API has not been used` | Step 1.2 — enable the Tasks API on the project |
| `403` mentioning policy / blocked | Workspace admin has not allowlisted the client ID (step 3 note) |
| `401` after working fine | Token revoked (password change, admin action) — re-run `npm run auth` |
| Google returns no `refresh_token` | Client isn't type *Desktop app*, or a prior grant exists — the flow already sends `prompt=consent`, so re-run after revoking at <https://myaccount.google.com/permissions> |
