# OneNote MCP (read-only, personal Microsoft account)

Reads OneNote via Microsoft Graph for use in the LLM Land vault workflows (e.g. pulling JO 1:1 notes into Command Observations). Read-only (`Notes.Read`), device-code auth, **no client secret**.

## Tools
- `onenote_list_notebooks` / `onenote_list_sections` / `onenote_browse_notebook` / `onenote_list_pages` — navigation.
- `onenote_get_page` — typed page text from the HTML. Flags `has_ink` and `has_images` (with an image manifest) so you know when a page's content is locked in handwriting or screenshots that the text extraction can't see.
- `onenote_get_page_image` — render handwritten **ink** (InkML) → PNG tiles for vision-OCR.
- `onenote_get_page_screenshots` — fetch a page's embedded **raster images** (screenshots, pasted pictures, diagrams) as PNG blocks for vision-OCR. OneNote OCRs images server-side but **never exposes that text** in the page HTML (img tags carry no `alt`/OCR attrs) or via `$search`, so fetching the binary and reading it with vision is the only way to recover a screenshot's text. Big images are downscaled to ~1568px to stay legible and small.
- `onenote_search` — title/full-text search. Graph's native `$search` is rejected on this consumer account (`400 unsupported OData query parameters`) and the global `/me/onenote/pages` collection `400`s on accounts with many sections, so search **enumerates pages per-section and matches client-side** (AND of all terms, case-insensitive). Title-only by default; `deep=true` also scans page body text. The account is large (~190 sections / ~2300 pages) so an unscoped run is slow — pass `notebook` to scope to a notebook/section-group by name (and the only practical way to run a thorough `deep` search). Retries 429s with backoff (OneNote throttles hard). Caveat: **screenshot/image text is not matched** (not in the HTML) — open a hit with `onenote_get_page_screenshots`.

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
