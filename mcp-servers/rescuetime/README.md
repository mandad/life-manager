# rescuetime-mcp-server

Local MCP server that exposes your personal RescueTime computer-usage data so Claude can analyze how your time is spent (productivity pulse, app/category breakdowns, hourly trends, efficiency).

Wraps the RescueTime **Analytic Data API** (`www.rescuetime.com/anapi`). Read-only, single-user — uses your personal API key.

## Tools

| Tool | What it does |
| --- | --- |
| `rescuetime_daily_summary` | Past 14 days of pre-computed daily summaries (productivity pulse, productive/distracting %, total hours). Fast at-a-glance overview. |
| `rescuetime_query_activity_data` | Flexible Analytic Data query: date range, group by category/activity/productivity/efficiency/document, optional time resolution (hour/day/week/month). The workhorse. |
| `rescuetime_schedule_summary` | Timeline view of a day broken into 5/10/15/30-min chunks, showing the dominant activity per chunk. Useful for "what was I doing at 2:15pm?" or spotting context switches. |
| `rescuetime_highlights` | Manually-logged daily highlights. |

All tools are read-only and accept `response_format: 'markdown' | 'json'` (default `markdown`).

## Setup

### 1. Get your RescueTime API key

1. Go to <https://www.rescuetime.com/anapi/manage>.
2. Click **New API Key** if you don't already have one. Give it a name like "Claude MCP".
3. Copy the key string.

API keys don't expire automatically but can be revoked from that same page.

### 2. Build

```bash
cd "C:/Users/damia/OneDrive/Documents/LLM Land/mcp-servers/rescuetime"
npm install
npm run build
```

This produces `dist/index.js`.

### 3. Wire it into Claude Code

Add to your Claude Code `settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "rescuetime": {
      "command": "node",
      "args": ["C:/Users/damia/OneDrive/Documents/LLM Land/mcp-servers/rescuetime/dist/index.js"],
      "env": {
        "RESCUETIME_API_KEY": "paste-your-key-here"
      }
    }
  }
}
```

Restart Claude Code. The four tools should appear under the `rescuetime` server.

### 4. Try it

Ask Claude things like:

- "How was my productivity pulse last week?"
- "Which apps dominated my time yesterday?"
- "Compare my hourly productivity yesterday vs. today."
- "How much time did I spend in the Software Development category in April?"
- "What did I get done last week according to my highlights — does it match the time-tracking data?"

## Notes

- **Time-zone**: Date ranges are interpreted in the time zone configured on your RescueTime account.
- **Productivity scale**: Each activity has a productivity score from -2 (very distracting) to +2 (very productive). The server adds a `productivity_label` field for readability.
- **Premium-only**: `kind='document'` and `restrict_thingy` (document-level filtering) require a RescueTime Premium subscription.
- **Truncation**: Responses larger than 25 000 characters are truncated. Narrow the date range or use a coarser `kind`.
- **Privacy**: Local stdio server. The API key never leaves your machine except in calls to `www.rescuetime.com`.
