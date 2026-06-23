# OneNote MCP — setup (personal Microsoft account)

One-time app registration so the MCP can read your OneNote via Microsoft Graph. **Read-only, no client secret** (device-code public-client flow). ~5 minutes. You do Part A; hand me the **client ID** and I finish Parts B–D.

---

## Part A — Register the app (you do this)

Sign in everywhere below with the **personal Microsoft account** that owns the OneNote notebook (the one your iPad OneNote syncs to).

1. Go to **https://portal.azure.com** → search bar → **App registrations** → **+ New registration**.
   *(No Azure subscription needed — registration is free. If it nags about a subscription, ignore; App registrations is separate.)*

2. Fill in:
   - **Name:** `LLM-Land OneNote Reader`
   - **Supported account types:** select **"Personal Microsoft accounts only"**.
   - **Redirect URI:** leave **blank**.
   - Click **Register**.

3. On the app's **Overview** page, copy the **Application (client) ID** (a GUID like `1a2b3c4d-...`). **This is the one value I need from you.**

4. Left menu → **Authentication** → scroll to **Advanced settings** → **Allow public client flows** → set to **Yes** → **Save**.
   *(This enables the device-code login. Without it, login fails.)*

5. Left menu → **API permissions** → **+ Add a permission** → **Microsoft Graph** → **Delegated permissions** → search + check:
   - **`Notes.Read`** (read your OneNote notebooks — read-only)
   - **`offline_access`** (lets the token refresh silently so you don't re-login daily)
   - Click **Add permissions**.
   *(No "Grant admin consent" needed — it's a personal account; you'll consent yourself at first login. The page may show "Not granted for personal accounts" — that's normal and fine.)*

6. **Do NOT create a client secret.** Device-code flow uses none — that's deliberate (nothing secret to leak).

**Hand me:** the **Application (client) ID** from step 3. That's it.

---

## Part B — First login (you + me, once I've built it)

The MCP authenticates with the **OAuth 2.0 device-code flow**:
- I run the auth command → it prints a short code + the URL **https://microsoft.com/devicelogin**.
- You open that URL (phone or browser), enter the code, sign in with the personal account, and approve **"read your OneNote notebooks."**
- The token is cached locally; the refresh token renews it silently after that. You only do this once (until you revoke).

---

## Part C — Config & secrets (I wire this)

- **Authority / tenant:** `consumers` (personal accounts) → `https://login.microsoftonline.com/consumers`
- **Scopes:** `Notes.Read offline_access`
- **Client ID:** env var `ONENOTE_CLIENT_ID` (yours from Part A — not secret, but kept in the settings env block, not in code)
- **Token cache:** `ONENOTE_TOKEN_PATH` → a file **outside OneDrive** (e.g. `~/.config/llm-land/onenote-tokens.json`), `chmod 600`. Never written into the OneDrive-synced repo. Refresh token lives here only.
- Wired into `.claude/settings.local.json` under `mcpServers.onenote` like the other servers.

## Part D — Tools exposed (I build, incrementally)

1. `onenote_list_notebooks` → notebooks (id, name, last-modified)
2. `onenote_list_sections(notebook)` → sections
3. `onenote_list_pages(section, since?)` → pages (id, title, created/modified)
4. `onenote_get_page(id)` → **typed text** extracted from page HTML + `has_ink` flag
5. `onenote_search(query)` → pages matching text
6. `onenote_get_page_image(id)` → rendered **PNG** of the page's ink (InkML→raster) for me to OCR the handwriting

Build order: 1–4 first (verify typed text flows against your JO 1:1 section), then 5–6.

---

## Security notes

- Scope is **`Notes.Read`** — read-only; the app cannot modify or delete your notes.
- **No client secret** exists (public-client device-code) — nothing to exfiltrate.
- Refresh token is the only sensitive artifact; it lives in `ONENOTE_TOKEN_PATH` outside OneDrive, `chmod 600`, never printed, never committed.
- Revoke anytime: account.microsoft.com → Privacy / app access, or delete the app registration.
