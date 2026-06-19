# foursquare-checkins-mcp-server

Local MCP server that exposes your personal Foursquare/Swarm check-in history so Claude can analyze your activity (where you go, how often, by city/category/time period, etc.).

Wraps the Foursquare **v2 user API** (`api.foursquare.com/v2`) — that's the surface that exposes a user's own check-ins. The newer v3 Places API does **not** expose personal check-ins.

## Tools

| Tool | What it does |
| --- | --- |
| `foursquare_list_checkins` | Browse your check-in history, paginated, with optional date range and sort. Primary tool. |
| `foursquare_get_checkin` | Full detail on one check-in — shout, photo URLs, comments, who you were with. |
| `foursquare_get_self` | Your profile + lifetime stats (total check-ins, badges, etc.). |
| `foursquare_get_venue` | Full venue detail by ID. |
| `foursquare_search_venues` | Search the venue database near a point or place — useful for "have I been here?" cross-references. |

All tools are read-only and accept `response_format: 'markdown' | 'json'` (default `markdown`).

## Setup

### 1. Get a user OAuth token

Foursquare requires a per-user OAuth token (the consumer-app keys are not enough — those only authorize the app itself).

1. Go to <https://foursquare.com/developers> and create an app (or use an existing one). Note the **Client ID** and set a **Redirect URI** (any HTTPS URL you control will do — even `https://localhost/`).
2. In a browser, visit (replace `YOUR_CLIENT_ID` and `YOUR_REDIRECT_URI`):

   ```
   https://foursquare.com/oauth2/authenticate?client_id=YOUR_CLIENT_ID&response_type=token&redirect_uri=YOUR_REDIRECT_URI
   ```

3. Approve the app. Foursquare will redirect to:

   ```
   YOUR_REDIRECT_URI#access_token=ABCDEF…
   ```

4. Copy the `access_token` value out of the URL fragment. That's your token.

The token does not expire on a fixed schedule but can be revoked if you remove the app from your account.

### 2. Build

```bash
cd "C:/Users/damia/OneDrive/Documents/LLM Land/mcp-servers/foursquare-checkins"
npm install
npm run build
```

This produces `dist/index.js`.

### 3. Wire it into Claude Code

Add to your Claude Code `settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "foursquare-checkins": {
      "command": "node",
      "args": ["C:/Users/damia/OneDrive/Documents/LLM Land/mcp-servers/foursquare-checkins/dist/index.js"],
      "env": {
        "FOURSQUARE_OAUTH_TOKEN": "paste-your-token-here"
      }
    }
  }
}
```

Restart Claude Code. The five tools should appear under the `foursquare-checkins` server.

### 4. Try it

Ask Claude things like:

- "List my last 20 Foursquare check-ins."
- "How many times did I check in during 2025? Group by city."
- "What category do I check in to most often?"
- "Find my first ever check-in."
- "Have I ever been to a coffee shop named Blue Bottle in DC?"

## Notes

- **Endpoint version**: pinned to `v=20240101`. Bump in `src/index.ts` if Foursquare's API contract changes.
- **Rate limits**: Foursquare v2 limits user-token calls per hour. The server surfaces 429s as actionable error text.
- **Truncation**: Responses larger than 25 000 characters are truncated with a `truncation_message`. Reduce `limit` or narrow the time range.
- **Privacy**: This is a local stdio server. The token never leaves your machine except in calls to `api.foursquare.com`.
