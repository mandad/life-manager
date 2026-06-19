#!/usr/bin/env node
/**
 * MCP server for personal RescueTime computer-usage data.
 *
 * Wraps the RescueTime Analytic Data API (www.rescuetime.com/anapi) using a
 * personal API key supplied via RESCUETIME_API_KEY. Read-only.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";

const API_BASE_URL = "https://www.rescuetime.com/anapi";
const CHARACTER_LIMIT = 25000;
const DEFAULT_TIMEOUT_MS = 30000;

enum ResponseFormat {
  MARKDOWN = "markdown",
  JSON = "json",
}

const ResponseFormatSchema = z
  .nativeEnum(ResponseFormat)
  .default(ResponseFormat.MARKDOWN)
  .describe(
    "Output format: 'markdown' for human-readable summary or 'json' for full structured data",
  );

const DateStringSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/, "Must be YYYY-MM-DD");

const PRODUCTIVITY_LABELS: Record<number, string> = {
  2: "very productive",
  1: "productive",
  0: "neutral",
  [-1]: "distracting",
  [-2]: "very distracting",
};

interface RescueTimeAnalyticResponse {
  notes?: string;
  row_headers: string[];
  rows: Array<Array<string | number>>;
}

interface RescueTimeDailySummary {
  id: number;
  date: string;
  productivity_pulse: number;
  very_productive_percentage: number;
  productive_percentage: number;
  neutral_percentage: number;
  distracting_percentage: number;
  very_distracting_percentage: number;
  all_productive_percentage: number;
  all_distracting_percentage: number;
  total_hours: number;
  total_duration_formatted: string;
  // Plus per-category percentage fields like business_percentage, etc.
  [key: string]: string | number;
}

interface RescueTimeHighlight {
  id?: number;
  description?: string;
  date?: string;
  source?: string;
  created_at?: string;
}

function getApiKey(): string {
  const key = process.env.RESCUETIME_API_KEY;
  if (!key) {
    throw new Error(
      "RESCUETIME_API_KEY environment variable is not set. " +
        "Create one at https://www.rescuetime.com/anapi/manage and export it before starting the server.",
    );
  }
  return key;
}

class RescueTimeApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`RescueTime API error ${status}: ${detail}`);
    this.name = "RescueTimeApiError";
  }
}

async function rescueTimeRequest<T>(
  endpoint: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const key = getApiKey();
  const cleanParams: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") cleanParams[k] = v;
  }

  try {
    const response = await axios.get<T>(`${API_BASE_URL}${endpoint}`, {
      params: { key, format: "json", ...cleanParams },
      timeout: DEFAULT_TIMEOUT_MS,
      headers: { Accept: "application/json" },
      // RescueTime returns 200 with an HTML body on bad keys; let us detect that.
      transformResponse: [(data) => data],
    });
    // Some error states arrive as 200 with non-JSON. Try to parse.
    if (typeof response.data === "string") {
      const text = response.data as unknown as string;
      try {
        return JSON.parse(text) as T;
      } catch {
        const snippet = text.slice(0, 200).replace(/\s+/g, " ");
        throw new RescueTimeApiError(
          response.status,
          `Non-JSON response from RescueTime (likely an invalid API key): ${snippet}`,
        );
      }
    }
    return response.data;
  } catch (error) {
    if (error instanceof RescueTimeApiError) throw error;
    if (axios.isAxiosError(error) && error.response) {
      const body =
        typeof error.response.data === "string"
          ? error.response.data.slice(0, 300)
          : JSON.stringify(error.response.data).slice(0, 300);
      throw new RescueTimeApiError(error.response.status, body);
    }
    throw error;
  }
}

function handleApiError(error: unknown): string {
  if (error instanceof RescueTimeApiError) {
    if (error.status === 401 || error.status === 403) {
      return (
        "Error: RescueTime rejected the API key. " +
        "Generate a new key at https://www.rescuetime.com/anapi/manage and update RESCUETIME_API_KEY."
      );
    }
    if (error.status === 429) {
      return "Error: RescueTime rate limit hit. Wait and retry.";
    }
    return `Error: ${error.message}`;
  }
  if (axios.isAxiosError(error)) {
    if ((error as AxiosError).code === "ECONNABORTED") {
      return "Error: Request to RescueTime timed out after 30s. Try a narrower date range.";
    }
    return `Error: Network error reaching RescueTime: ${error.message}`;
  }
  return `Error: ${error instanceof Error ? error.message : String(error)}`;
}

function secondsToHuman(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return "0s";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts: string[] = [];
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  if (!h && !m) parts.push(`${s}s`);
  else if (s && !h) parts.push(`${s}s`);
  return parts.join(" ");
}

function maybeTruncate(textContent: string, payload: Record<string, unknown>): {
  text: string;
  payload: Record<string, unknown>;
} {
  if (textContent.length <= CHARACTER_LIMIT) {
    return { text: textContent, payload };
  }
  const truncated = {
    ...payload,
    truncated: true,
    truncation_message: `Response exceeded ${CHARACTER_LIMIT} characters. Narrow the date range, change 'kind', or restrict by 'restrict_thing'.`,
  };
  const truncatedText = JSON.stringify(truncated, null, 2).slice(0, CHARACTER_LIMIT);
  return { text: truncatedText, payload: truncated };
}

/**
 * Convert RescueTime's [headers, rows] into a typed array of records, with
 * convenience fields added (time_formatted, productivity_label).
 */
function parseAnalyticRows(
  data: RescueTimeAnalyticResponse,
): Array<Record<string, string | number>> {
  const headers = data.row_headers ?? [];
  const headerKey: Record<string, string> = {
    Rank: "rank",
    Date: "date",
    "Time Spent (seconds)": "time_seconds",
    "Number of People": "people",
    Activity: "activity",
    Category: "category",
    Productivity: "productivity",
    Document: "document",
    "Efficiency (percent)": "efficiency_percent",
  };

  return (data.rows ?? []).map((row) => {
    const record: Record<string, string | number> = {};
    headers.forEach((h, i) => {
      const key = headerKey[h] ?? h.toLowerCase().replace(/\s+/g, "_");
      record[key] = row[i];
    });
    if (typeof record.time_seconds === "number") {
      record.time_formatted = secondsToHuman(record.time_seconds);
    }
    if (typeof record.productivity === "number") {
      record.productivity_label =
        PRODUCTIVITY_LABELS[record.productivity] ?? String(record.productivity);
    }
    return record;
  });
}

const server = new McpServer({
  name: "rescuetime-mcp-server",
  version: "1.0.0",
});

// ---------------------------------------------------------------------------
// Tool: rescuetime_daily_summary
// ---------------------------------------------------------------------------

const DailySummarySchema = z
  .object({ response_format: ResponseFormatSchema })
  .strict();

server.registerTool(
  "rescuetime_daily_summary",
  {
    title: "Get my RescueTime daily summary (last 14 days)",
    description: `Return RescueTime's pre-computed daily summaries for the last 14 days. Each day includes the productivity pulse (0-100), the breakdown of time across the five productivity tiers, total hours tracked, and per-category percentages.

Use this for fast at-a-glance questions ("how productive was I last week?", "which day this week did I have the most distracting time?"). For deeper analysis (specific apps, custom date range, hourly resolution) use rescuetime_query_activity_data instead.

Args:
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "days": [
      {
        "date": string,                                 // YYYY-MM-DD
        "productivity_pulse": number,                   // 0-100
        "total_hours": number,
        "total_duration_formatted": string,
        "very_productive_percentage": number,
        "productive_percentage": number,
        "neutral_percentage": number,
        "distracting_percentage": number,
        "very_distracting_percentage": number,
        "all_productive_percentage": number,
        "all_distracting_percentage": number,
        ...                                             // Per-category percentages (e.g. business_percentage)
      }
    ]
  }

Errors:
  - 401/403 -> bad API key; reissue at rescuetime.com/anapi/manage.`,
    inputSchema: DailySummarySchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await rescueTimeRequest<RescueTimeDailySummary[]>(
        "/daily_summary_feed",
      );
      const days = Array.isArray(data) ? data : [];
      const output = { count: days.length, days };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# RescueTime daily summary (${days.length} days)`,
          "",
          "| Date | Pulse | Hours | Productive | Distracting |",
          "| --- | ---: | ---: | ---: | ---: |",
        ];
        for (const d of days) {
          lines.push(
            `| ${d.date} | ${d.productivity_pulse} | ${d.total_hours.toFixed(2)} | ${d.all_productive_percentage}% | ${d.all_distracting_percentage}% |`,
          );
        }
        if (!days.length) lines.push("_No daily summaries available._");
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }

      const final = maybeTruncate(text, output);
      return {
        content: [{ type: "text", text: final.text }],
        structuredContent: final.payload,
      };
    } catch (error) {
      return {
        content: [{ type: "text", text: handleApiError(error) }],
        isError: true,
      };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: rescuetime_query_activity_data
// ---------------------------------------------------------------------------

const QueryActivityDataSchema = z
  .object({
    kind: z
      .enum(["category", "activity", "productivity", "efficiency", "document"])
      .default("category")
      .describe(
        "What dimension to break the time down by. 'category' = high-level groups; 'activity' = individual apps/sites; 'productivity' = the five productivity tiers; 'efficiency' = productivity-weighted score over time (requires resolution); 'document' = document/window titles (premium).",
      ),
    start_date: DateStringSchema.optional().describe(
      "Start of date range, YYYY-MM-DD inclusive. Defaults to today if omitted.",
    ),
    end_date: DateStringSchema.optional().describe(
      "End of date range, YYYY-MM-DD inclusive. Defaults to today if omitted.",
    ),
    resolution: z
      .enum(["month", "week", "day", "hour"])
      .optional()
      .describe(
        "If set, returns a time series at this resolution (perspective='interval'). If omitted, returns a ranked summary over the whole range.",
      ),
    restrict_thing: z
      .string()
      .max(200)
      .optional()
      .describe(
        "Filter to a specific overview/category/activity name (depends on 'kind'). E.g. with kind='activity' set 'Software Development' to scope to that category.",
      ),
    restrict_thingy: z
      .string()
      .max(200)
      .optional()
      .describe(
        "Further filter by document/window title within the restrict_thing scope (premium).",
      ),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "rescuetime_query_activity_data",
  {
    title: "Query my RescueTime activity data",
    description: `Run a flexible query against the RescueTime Analytic Data API. This is the workhorse tool for time-use analysis: "how much time did I spend in VS Code last week?", "what was my busiest hour yesterday?", "which categories ate the most time in April?".

The API has two perspectives:
  - **Rank** (default): summary over the whole date range, sorted by time spent.
  - **Interval**: time-series at a chosen resolution. Set the 'resolution' arg to switch into this mode.

Args:
  - kind ('category' | 'activity' | 'productivity' | 'efficiency' | 'document', default 'category'): the dimension to break down by.
  - start_date (string, YYYY-MM-DD, optional): inclusive. Defaults to today.
  - end_date (string, YYYY-MM-DD, optional): inclusive. Defaults to today.
  - resolution ('month' | 'week' | 'day' | 'hour', optional): if set, returns a time-series.
  - restrict_thing (string, optional): scope to one overview/category/activity name (e.g. 'Software Development').
  - restrict_thingy (string, optional): further scope by document/window title (premium).
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "params": { ...the effective query parameters... },
    "notes": string,                    // RescueTime's own notes about the result
    "row_headers": string[],            // Original column names from the API
    "count": number,
    "rows": [
      {
        "rank"?: number,                // Present in rank perspective
        "date"?: string,                // Present in interval perspective
        "time_seconds": number,
        "time_formatted": string,       // e.g. "1h 23m"
        "people": number,
        "activity"?: string,
        "category"?: string,
        "document"?: string,
        "productivity"?: number,        // -2..2
        "productivity_label"?: string,  // 'very productive' | 'productive' | 'neutral' | 'distracting' | 'very distracting'
        "efficiency_percent"?: number   // For kind='efficiency'
      }
    ]
  }

Examples:
  - "How did I spend last week?" -> kind='category', start_date=monday, end_date=sunday.
  - "Which apps dominated yesterday?" -> kind='activity', start_date=yesterday, end_date=yesterday.
  - "Hourly productivity yesterday" -> kind='productivity', resolution='hour', start_date=yesterday, end_date=yesterday.
  - "Efficiency trend across April" -> kind='efficiency', resolution='day', start_date='2026-04-01', end_date='2026-04-30'.
  - "Time inside Software Development category" -> kind='activity', restrict_thing='Software Development'.

Errors:
  - 401/403 -> bad API key.
  - 429 -> rate limited.`,
    inputSchema: QueryActivityDataSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const perspective = params.resolution ? "interval" : "rank";
      const apiParams: Record<string, string | number | undefined> = {
        pv: perspective,
        rk: params.kind,
        rs: params.resolution,
        rb: params.start_date,
        re: params.end_date,
        ry: params.restrict_thing,
        rt: params.restrict_thingy,
      };

      const data = await rescueTimeRequest<RescueTimeAnalyticResponse>(
        "/data",
        apiParams,
      );

      const rows = parseAnalyticRows(data);
      const output: Record<string, unknown> = {
        params: {
          kind: params.kind,
          start_date: params.start_date,
          end_date: params.end_date,
          resolution: params.resolution,
          restrict_thing: params.restrict_thing,
          restrict_thingy: params.restrict_thingy,
          perspective,
        },
        notes: data.notes,
        row_headers: data.row_headers,
        count: rows.length,
        rows,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# RescueTime activity (${rows.length} rows, ${perspective})`,
          `kind: \`${params.kind}\`${params.resolution ? `, resolution: \`${params.resolution}\`` : ""}`,
          params.start_date || params.end_date
            ? `range: ${params.start_date ?? "(today)"} → ${params.end_date ?? "(today)"}`
            : "range: today",
          params.restrict_thing ? `restrict_thing: \`${params.restrict_thing}\`` : "",
          "",
        ];

        if (!rows.length) {
          lines.push("_No data returned for this query._");
        } else {
          // Pick columns to display based on what's present.
          const sample = rows[0];
          const cols: string[] = [];
          if ("date" in sample) cols.push("date");
          if ("rank" in sample) cols.push("rank");
          cols.push("time_formatted");
          if ("activity" in sample) cols.push("activity");
          if ("category" in sample) cols.push("category");
          if ("document" in sample) cols.push("document");
          if ("productivity_label" in sample) cols.push("productivity_label");
          if ("efficiency_percent" in sample) cols.push("efficiency_percent");

          lines.push(`| ${cols.join(" | ")} |`);
          lines.push(`| ${cols.map(() => "---").join(" | ")} |`);
          for (const row of rows) {
            lines.push(`| ${cols.map((c) => String(row[c] ?? "")).join(" | ")} |`);
          }
        }

        if (data.notes) {
          lines.push("", `_${data.notes}_`);
        }
        text = lines.filter((l) => l !== undefined).join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }

      const final = maybeTruncate(text, output);
      return {
        content: [{ type: "text", text: final.text }],
        structuredContent: final.payload,
      };
    } catch (error) {
      return {
        content: [{ type: "text", text: handleApiError(error) }],
        isError: true,
      };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: rescuetime_highlights
// ---------------------------------------------------------------------------

const HighlightsSchema = z
  .object({ response_format: ResponseFormatSchema })
  .strict();

server.registerTool(
  "rescuetime_highlights",
  {
    title: "Get my RescueTime highlights",
    description: `Return the most recent daily highlights — short notes the user (or a connected service) has logged about what they accomplished each day. Useful as a self-reported counterpoint to the time-tracking data ("the data says I spent 4 hours in Slack but my highlight says I shipped a feature").

Args:
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "highlights": [
      {
        "date": string,           // YYYY-MM-DD
        "description": string,    // The highlight text
        "source": string,         // Where it was logged
        "created_at": string
      }
    ]
  }`,
    inputSchema: HighlightsSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await rescueTimeRequest<RescueTimeHighlight[]>(
        "/highlights_feed",
      );
      const highlights = Array.isArray(data) ? data : [];
      const output = { count: highlights.length, highlights };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [`# RescueTime highlights (${highlights.length})`, ""];
        if (!highlights.length) {
          lines.push("_No highlights logged._");
        } else {
          for (const h of highlights) {
            lines.push(`- **${h.date ?? "?"}** — ${h.description ?? ""}${h.source ? ` _(${h.source})_` : ""}`);
          }
        }
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }

      const final = maybeTruncate(text, output);
      return {
        content: [{ type: "text", text: final.text }],
        structuredContent: final.payload,
      };
    } catch (error) {
      return {
        content: [{ type: "text", text: handleApiError(error) }],
        isError: true,
      };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: rescuetime_schedule_summary
// ---------------------------------------------------------------------------

const ScheduleSummarySchema = z
  .object({
    date: DateStringSchema.optional().describe(
      "Date to summarise, YYYY-MM-DD. Defaults to today if omitted.",
    ),
    chunk_minutes: z
      .number()
      .int()
      .refine((n) => [5, 10, 15, 30].includes(n), {
        message: "Must be 5, 10, 15, or 30",
      })
      .default(15)
      .describe("Size of each time bucket in minutes. Must be 5, 10, 15, or 30. Default 15."),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "rescuetime_schedule_summary",
  {
    title: "Get my RescueTime schedule summary (dominant activity per 15-min chunk)",
    description: `Return a timeline view of a day broken into configurable time chunks (default 15 minutes), showing the dominant activity for each chunk. Useful for reconstructing what you were doing at a specific time ("what was I working on at 2:15 pm?", "when did I switch from email to coding?").

Uses RescueTime's minute-level resolution — the finest granularity available from the API. Within each chunk the activity that consumed the most total time is shown as dominant.

Args:
  - date (string, YYYY-MM-DD, optional): the day to summarise. Defaults to today.
  - chunk_minutes (5 | 10 | 15 | 30, default 15): size of each time bucket.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "date": string,
    "chunk_minutes": number,
    "count": number,           // number of tracked chunks
    "slots": [
      {
        "slot": string,                  // e.g. "09:00–09:15"
        "dominant_activity": string,
        "dominant_seconds": number,
        "dominant_time_formatted": string,
        "productivity_label": string,
        "all_activities": [              // all activities tracked in this chunk, ranked by time
          {
            "activity": string,
            "time_seconds": number,
            "time_formatted": string,
            "productivity_label": string
          }
        ]
      }
    ]
  }

Examples:
  - "What was I doing at 2pm yesterday?" -> date=yesterday, look at "14:00–14:15" slot.
  - "When did I stop coding and start on email today?" -> scan dominant_activity transitions.
  - "Give me a 30-min-chunk view of last Tuesday" -> chunk_minutes=30, date=that Tuesday.`,
    inputSchema: ScheduleSummarySchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const date = params.date ?? new Date().toISOString().slice(0, 10);
      const chunkMinutes = params.chunk_minutes ?? 15;

      const data = await rescueTimeRequest<RescueTimeAnalyticResponse>("/data", {
        pv: "interval",
        rk: "activity",
        rs: "minute",
        rb: date,
        re: date,
      });

      const rawRows = parseAnalyticRows(data);

      // Group rows by chunk bucket. API date field at minute resolution is
      // "YYYY-MM-DDTHH:MM:SS". Multiple rows per minute are possible (split across apps).
      const byChunk = new Map<
        string,
        { label: string; rows: Array<Record<string, string | number>> }
      >();
      for (const row of rawRows) {
        const rawDate = String(row.date ?? "");
        const match = rawDate.match(/[T ](\d{2}):(\d{2})/);
        if (!match) continue;
        const h = parseInt(match[1], 10);
        const m = parseInt(match[2], 10);
        const bucketMin = Math.floor(m / chunkMinutes) * chunkMinutes;
        const endMin = bucketMin + chunkMinutes;
        const endH = endMin >= 60 ? h + 1 : h;
        const endMod = endMin % 60;
        const sortKey = `${String(h).padStart(2, "0")}:${String(bucketMin).padStart(2, "0")}`;
        const label = `${sortKey}–${String(endH % 24).padStart(2, "0")}:${String(endMod).padStart(2, "0")}`;
        if (!byChunk.has(sortKey)) byChunk.set(sortKey, { label, rows: [] });
        byChunk.get(sortKey)!.rows.push(row);
      }

      // Within each chunk, aggregate seconds per activity name (same app can appear across
      // multiple per-minute rows), then sort by time descending.
      const slots = Array.from(byChunk.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([, { label, rows }]) => {
          const actMap = new Map<string, { time_seconds: number; productivity: number }>();
          for (const row of rows) {
            const name = String(row.activity ?? row.category ?? "Unknown");
            const secs = (row.time_seconds as number) ?? 0;
            const prod = (row.productivity as number) ?? 0;
            const existing = actMap.get(name);
            if (existing) {
              existing.time_seconds += secs;
            } else {
              actMap.set(name, { time_seconds: secs, productivity: prod });
            }
          }
          const activities = Array.from(actMap.entries())
            .map(([name, d]) => ({
              activity: name,
              time_seconds: d.time_seconds,
              time_formatted: secondsToHuman(d.time_seconds),
              productivity_label:
                PRODUCTIVITY_LABELS[d.productivity] ?? String(d.productivity),
            }))
            .sort((a, b) => b.time_seconds - a.time_seconds);

          const top = activities[0];
          return {
            slot: label,
            dominant_activity: top?.activity ?? "Unknown",
            dominant_seconds: top?.time_seconds ?? 0,
            dominant_time_formatted: top ? secondsToHuman(top.time_seconds) : "0s",
            productivity_label: top?.productivity_label ?? "",
            all_activities: activities,
          };
        });

      const output = { date, chunk_minutes: chunkMinutes, count: slots.length, slots };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# RescueTime schedule — ${date} (${chunkMinutes}-min chunks)`,
          `_${slots.length} tracked chunk${slots.length !== 1 ? "s" : ""}_`,
          "",
          "| Time | Dominant activity | Time in chunk | Productivity |",
          "| --- | --- | ---: | --- |",
        ];
        for (const slot of slots) {
          lines.push(
            `| ${slot.slot} | ${slot.dominant_activity} | ${slot.dominant_time_formatted} | ${slot.productivity_label} |`,
          );
        }
        if (!slots.length) {
          lines.push("_No activity tracked on this day._");
        }
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }

      const final = maybeTruncate(text, output);
      return {
        content: [{ type: "text", text: final.text }],
        structuredContent: final.payload,
      };
    } catch (error) {
      return {
        content: [{ type: "text", text: handleApiError(error) }],
        isError: true,
      };
    }
  },
);

// ---------------------------------------------------------------------------

async function main() {
  // Validate key presence at startup so misconfiguration fails fast.
  try {
    getApiKey();
  } catch (e) {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("rescuetime-mcp-server running on stdio");
}

main().catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});
