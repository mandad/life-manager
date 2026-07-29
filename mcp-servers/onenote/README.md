# OneNote MCP (personal Microsoft account)

Reads OneNote via Microsoft Graph for use in the LLM Land vault workflows (e.g. pulling JO 1:1 notes into Command Observations). Scopes `Notes.Read` + `Notes.Create` (read everything + create new pages/sections/notebooks; **cannot edit or delete** existing notes), device-code auth, **no client secret**. All currently-exposed tools are read-only — `Notes.Create` was consented 2026-06-25 ahead of a write tool (`onenote_create_page`, not yet built).

## Tools
- `onenote_list_notebooks` / `onenote_list_sections` / `onenote_browse_notebook` / `onenote_list_pages` — navigation.
- `onenote_get_page` — typed page text from the HTML. Flags `has_ink` and `has_images` (with an image manifest) so you know when a page's content is locked in handwriting or screenshots that the text extraction can't see.
- `onenote_scan_sections` — batch watermark scan: loops `onenote_list_pages` over up to 30 sections server-side (sharing the 429 backoff) and returns only sections with pages at/after `since`. Built for the /daily scan. **Detects NEW pages only** — see the next entry for why.
- `onenote_hash_pages` — SHA256 of each page's whitespace-normalized typed text, grouped by section, for **edit detection**. On this consumer account `lastModifiedDateTime` is never bumped on an edit (it always equals `createdDateTime`, confirmed 2026-07-26), so any timestamp watermark is structurally blind to edits; a changed hash is the only reliable signal. Bound the fan-out with `created_since` (client-side filter — one content GET per surviving page). Caveat: typed text only, so ink-only / image-only edits don't move the hash, and an ink-only page hashes to the empty-string digest `e3b0c442…7852b855`.
- `onenote_get_page_image` — render handwritten **ink** (InkML) → PNG tiles for vision-OCR.
- `onenote_get_page_screenshots` — fetch a page's embedded **raster images** (screenshots, pasted pictures, diagrams) as PNG blocks for vision-OCR. OneNote OCRs images server-side but **never exposes that text** in the page HTML (img tags carry no `alt`/OCR attrs) or via `$search`, so fetching the binary and reading it with vision is the only way to recover a screenshot's text. Big images are downscaled to ~1568px to stay legible and small.
- `onenote_search` — title/full-text search. Graph's native `$search` is rejected on this consumer account (`400 unsupported OData query parameters`) and the global `/me/onenote/pages` collection `400`s on accounts with many sections, so search **enumerates pages per-section and matches client-side** (AND of all terms, case-insensitive). Title-only by default; `deep=true` also scans page body text. The account is large (~190 sections / ~2300 pages) so an unscoped run is slow — pass `notebook` to scope to a notebook/section-group by name (and the only practical way to run a thorough `deep` search). Retries 429s with backoff (OneNote throttles hard). Caveat: **screenshot/image text is not matched** (not in the HTML) — open a hit with `onenote_get_page_screenshots`.

## Setup
See **[SETUP.md](SETUP.md)**. Short version:
1. Register a personal-account app (Azure portal → App registrations), enable public-client flows, add delegated `Notes.Read` + `Notes.Create` + `offline_access`. Copy the **client ID**.
2. `npm install && npm run build`
3. `npm run auth` → device-code login (one time). `ONENOTE_CLIENT_ID` / `ONENOTE_TOKEN_PATH` are read from `~/.config/llm-land-mcp/secrets.env` automatically (auth.ts loads it as a fallback when the vars aren't already in the env), so no inline prefix is needed. Override the file path with `LLM_LAND_SECRETS_ENV`, or just export the vars yourself.
4. Register the server (managed centrally by `scripts/mcp-sync.py` → `~/.claude.json` + Claude Desktop); restart the client.

## Config (env)
Provided via `~/.config/llm-land-mcp/secrets.env` (sourced by `mcp-sync.py` for the running server; auto-loaded by `auth.ts` for `npm run auth`):
- `ONENOTE_CLIENT_ID` — app client ID (required)
- `ONENOTE_TOKEN_PATH` — token cache path (e.g. `$HOME/.config/llm-land-mcp/onenote-token.json`). **Keep outside OneDrive.**

## Wiring (managed by `mcp-sync.py`)
MCP registration is generated, **not hand-edited**. `scripts/mcp-sync.py` reads `mcp-servers/mcp-manifest.json` and writes both Claude Code (`~/.claude.json`, user scope) and Claude Desktop (`claude_desktop_config.json`) in WSL-bridge mode — both clients run the same Linux build, each launched as:
```bash
bash -c 'set -a; [ -f "$HOME/.config/llm-land-mcp/secrets.env" ] && . "$HOME/.config/llm-land-mcp/secrets.env"; set +a; exec <wsl-node> .../onenote/dist/index.js'
```
(Desktop wraps that in `wsl.exe -d <distro> -- …`.) The `set -a; . secrets.env` line is what feeds `ONENOTE_CLIENT_ID` / `ONENOTE_TOKEN_PATH` to the server. To (re)wire:
```bash
python3 scripts/mcp-sync.py --dry-run   # preview
python3 scripts/mcp-sync.py             # apply, then restart the client(s)
```
Don't add this server with `claude mcp add` or in `.claude/settings.local.json` — `mcp-sync.py` prunes such duplicates to avoid conflicting-scope warnings. Runbook: `AI Scratchpad/Notes/MCP config sync.md`.

## Security
- `Notes.Read` + `Notes.Create` — can read all notes and create new pages/sections, but **cannot modify or delete** existing notes (Notes.Create is create-only; we deliberately avoid `Notes.ReadWrite`). No client secret exists.
- Refresh token lives in `ONENOTE_TOKEN_PATH` (outside OneDrive, `chmod 600`), never printed/committed.
- Revoke at account.microsoft.com or by deleting the app registration.
