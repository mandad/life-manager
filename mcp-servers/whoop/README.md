# whoop-mcp-server

Local MCP server that exposes your personal Whoop health-tracker data so Claude can analyze how your body is doing alongside your daily routine (recovery, sleep, strain, workouts).

Wraps the **Whoop API v2** (`api.prod.whoop.com/developer/v2`). Read-only, single-user — uses an OAuth 2.0 access token stored locally in `tokens.json` and refreshed automatically.

## Tools

| Tool | What it does |
| --- | --- |
| `whoop_get_today` | Cycle + recovery + sleep + workouts for today (or a chosen date) in one call. The default for daily-routine analysis. |
| `whoop_get_recovery` | Recovery scores, HRV, RHR, SpO2, skin temp over a date range. |
| `whoop_get_sleep` | Sleep sessions with stage breakdown (light/SWS/REM/awake), efficiency, performance, consistency. |
| `whoop_get_cycles` | Daily strain, avg/max HR, kilojoules burned. |
| `whoop_get_workouts` | Per-workout strain, HR zones, sport, distance. |
| `whoop_get_body_measurement` | Height, weight, max HR. |
| `whoop_get_profile` | User ID, name, email. |

All tools are read-only and accept `response_format: 'markdown' | 'json'` (default `markdown`). Range tools auto-paginate up to a 500-record safety cap.

## Setup

### 1. Register a Whoop developer app

1. Go to <https://developer.whoop.com/> and sign in with your Whoop account.
2. Create a new app. The fields you need to set:
   - **Redirect URI:** `http://localhost:3456/callback` (must match exactly — the auth CLI listens here).
   - **Scopes:** check all six read scopes plus `offline`:
     - `offline` (required to get a refresh token)
     - `read:recovery`
     - `read:cycles`
     - `read:sleep`
     - `read:workout`
     - `read:profile`
     - `read:body_measurement`
3. Copy the **Client ID** and **Client Secret** somewhere safe — you'll paste them in the next step.

> If you'd rather use a different port, set `WHOOP_REDIRECT_PORT=NNNN` (and register the matching `http://localhost:NNNN/callback`) before running the auth step.

### 2. Build

```bash
cd "/mnt/c/Users/damia/OneDrive/Documents/LLM Land/mcp-servers/whoop"
npm install
npm run build
```

### 3. Authorize (one-time)

Export the client credentials and run the auth helper. It will print a URL to visit, listen on `localhost:3456`, and write `tokens.json` once the redirect completes.

```bash
export WHOOP_CLIENT_ID="paste-client-id"
export WHOOP_CLIENT_SECRET="paste-client-secret"
npm run auth
```

Open the printed URL in a browser, sign in to Whoop, approve the scopes. The tab will say "authorization complete" and the terminal will report `Tokens saved to .../tokens.json`.

`tokens.json` is gitignored and written with mode `0600`. Whoop rotates the refresh token on every refresh, so the file is rewritten silently each time the access token expires.

### 4. Wire it into Claude Code

Add to your Claude Code `settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "whoop": {
      "command": "node",
      "args": ["C:/Users/damia/OneDrive/Documents/LLM_Land/mcp-servers/whoop/dist/index.js"],
      "env": {
        "WHOOP_CLIENT_ID": "paste-client-id",
        "WHOOP_CLIENT_SECRET": "paste-client-secret"
      }
    }
  }
}
```

Both `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` need to be present at runtime — the server uses them to refresh the access token when it expires.

Restart Claude Code. The seven tools should appear under the `whoop` server.

### 5. Try it

- "Pull my Whoop snapshot for today."
- "How was my recovery last week?"
- "What was my deepest sleep this month?"
- "Plot my strain trend for the last 14 days." (use `whoop_get_cycles`, response_format='json')
- "Compare my HRV trend to my workout intensity over the last month."

## Data notes

- **Strain scale:** 0–21, logarithmic. 10–13 is moderate, 14–17 hard, 18+ all-out.
- **Recovery:** 0–100. Whoop colour codes: red <34, yellow 34–66, green 67–100.
- **`user_calibrating: true`** on a recovery means Whoop is still building your baseline — early scores are less meaningful.
- **Pagination cap:** 500 records per call. A year of daily recoveries is ~365, so most ranges fit; for multi-year pulls, narrow the range.
- **Time zones:** Range params accept `YYYY-MM-DD` (treated as UTC) or full ISO 8601 with offset. Whoop record fields include `timezone_offset` so you can re-anchor locally if needed.

## Troubleshooting

- **`No Whoop token file found`** — run `npm run auth`.
- **`Whoop rejected the access token`** repeatedly — the refresh token was revoked (e.g. you re-authorized elsewhere). Re-run `npm run auth`.
- **`403`** — your Whoop app doesn't have one of the required scopes. Re-check the app's scope list, then `npm run auth` again.
- **Auth CLI hangs** — the redirect URI on your Whoop app must match `http://localhost:3456/callback` (or whatever `WHOOP_REDIRECT_PORT` you set). Whoop is strict about exact match.
