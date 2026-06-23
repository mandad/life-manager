# OneNote MCP (read-only, personal Microsoft account)

Reads OneNote via Microsoft Graph for use in the LLM Land vault workflows (e.g. pulling JO 1:1 notes into Command Observations). Read-only (`Notes.Read`), device-code auth, **no client secret**.

## Status
- **Phase 1 (scaffolded, unbuilt-against-live):** list notebooks/sections/pages, get typed page text, search.
- **Phase 2 (TODO):** `onenote_get_page_image` — render ink (InkML) → PNG for Claude to OCR handwriting.

## Setup
See **[SETUP.md](SETUP.md)**. Short version:
1. Register a personal-account app (Azure portal → App registrations), enable public-client flows, add delegated `Notes.Read` + `offline_access`. Copy the **client ID**.
2. `npm install && npm run build`
3. `ONENOTE_CLIENT_ID=<your-id> npm run auth` → device-code login (one time).
4. Wire into `.claude/settings.local.json` (see below); restart Claude Code.

## Config (env)
- `ONENOTE_CLIENT_ID` — app client ID (required)
- `ONENOTE_TOKEN_PATH` — token cache path; default `~/.config/llm-land/onenote-tokens.json`. **Keep outside OneDrive.**

## Wiring (`.claude/settings.local.json`)
```json
"onenote": {
  "type": "stdio",
  "command": "node",
  "args": ["/mnt/c/Users/damia/OneDrive/Documents/LLM_Land/mcp-servers/onenote/dist/index.js"],
  "env": {
    "ONENOTE_CLIENT_ID": "<your-client-id>",
    "ONENOTE_TOKEN_PATH": "/home/damia/.config/llm-land/onenote-tokens.json"
  }
}
```

## Security
- `Notes.Read` only — cannot modify/delete notes. No client secret exists.
- Refresh token lives in `ONENOTE_TOKEN_PATH` (outside OneDrive, `chmod 600`), never printed/committed.
- Revoke at account.microsoft.com or by deleting the app registration.
