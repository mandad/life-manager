# NWS Forecast MCP

Local MCP server exposing the US National Weather Service forecast API (`api.weather.gov`) — multi-day textual forecast, hourly forecast, and active alerts for a lat/lon.

NWS is free and unauthenticated, but requires a `User-Agent` header that identifies the application and a contact email.

## Tools exposed

| Tool | Args | Returns |
|---|---|---|
| `nws_get_forecast` | `lat`, `lon`, `periods?` (default 7), `response_format?` | Multi-day **land/coastal** textual forecast in 12-hour periods (max 14) |
| `nws_get_hourly_forecast` | `lat`, `lon`, `hours?` (default 24), `response_format?` | Hourly land forecast (up to 156 hours) |
| `nws_get_alerts` | `lat`, `lon`, `response_format?` | Active watches / warnings / advisories at the point |
| `nws_get_marine_forecast` | `lat`, `lon`, `response_format?` | **Offshore marine** forecast — resolves the point to its NWS marine zone, returns the Coastal Waters / Offshore text forecast (wind + seas) + active marine alerts for that zone |

`response_format` is `"markdown"` (default) or `"json"`. NWS coverage is **US + territories only**; lat/lon outside that range will fail at the `/points` lookup step.

**Land vs marine:** `nws_get_forecast` is the gridpoint land forecast — for an offshore point it snaps to the nearest coastal *town* (a Bering Sea point lands on Mekoryuk ~150 nm away) and gives no seas. For a **ship underway**, use `nws_get_marine_forecast`, which goes point → marine zone (`/zones?type=marine&point=`) → issuing office → latest CWF (`ALU`/`AJK`) or OFF (`AFG`) text product → that zone's block. Nearshore zones that carry no text segment fall back gracefully (use the land grid for inports — e.g. the Nome town grid is accurate).

## Build

```bash
cd mcp-servers/nws-forecast
npm install
npm run build
```

This compiles `src/index.ts` to `dist/index.js`. Re-run `npm run build` after editing the source.

## Configuration

Add this block to `.claude/settings.local.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "nws-forecast": {
      "type": "stdio",
      "command": "node",
      "args": [
        "/mnt/c/Users/damia/OneDrive/Documents/LLM_Land/mcp-servers/nws-forecast/dist/index.js"
      ],
      "env": {
        "NWS_USER_AGENT": "LLM-Land-NWS-MCP/1.0 (accounts@dmanda.com)"
      }
    }
  }
}
```

The `NWS_USER_AGENT` value identifies this client to NWS; keep the contact email accurate (NWS may rate-limit or contact misbehaving clients). Defaults to the same string if the env var isn't set.

**Activation:** MCP server changes take effect on next Claude Code session start. If a session was already running when the config was added, exit and restart. To verify the server starts cleanly without launching Claude Code:

```bash
timeout 3 node dist/index.js < /dev/null; echo "exit:$?"
# Expected: silent (server is waiting on stdio); exit code 143 from timeout means it was running fine.
```

## Coverage notes

- **US + territories only** — overseas points (e.g., a Fairweather port call outside US waters) will not return data; the server reports the `/points` 404 cleanly.
- **No auth, no key** — the only requirement is the User-Agent header.
- **Alerts endpoint** is point-based (`/alerts/active?point=lat,lon`); covers Wind / Flood / Marine / Fire / etc. advisories at that location.
- **Caching** — point lookups (lat/lon → grid) are cached in-memory for 24 hours to avoid hitting `/points` on every forecast call.

## Used by

`/daily` Step 10 — pulls forecast for the user's current location and any upcoming travel destinations. See `AI Scratchpad/Notes/Daily update routine.md`.
