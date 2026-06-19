#!/usr/bin/env node
/**
 * MCP server for personal Whoop health-tracker data.
 *
 * Wraps the Whoop API v2 (api.prod.whoop.com/developer/v2) using OAuth 2.0.
 * Tokens are loaded from tokens.json (see auth.ts) and refreshed automatically.
 * Read-only.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";
import {
  WHOOP_API_BASE,
  getClientCreds,
  getValidAccessToken,
  loadTokens,
} from "./auth.js";

const CHARACTER_LIMIT = 25_000;
const DEFAULT_TIMEOUT_MS = 30_000;
// Whoop v2 collection endpoints cap at 25 records per page.
const WHOOP_PAGE_MAX = 25;
// Safety cap for auto-pagination so a runaway query can't stall the agent.
const AUTO_PAGE_RECORD_CAP = 500;

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

// Accept either YYYY-MM-DD or full ISO 8601. Converted to ISO at request time.
const DateOrIsoSchema = z
  .string()
  .regex(
    /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/,
    "Must be YYYY-MM-DD or full ISO 8601 (e.g. 2026-04-30T14:30:00Z)",
  );

function toIsoStart(input: string): string {
  // Bare date -> start-of-day UTC.
  if (/^\d{4}-\d{2}-\d{2}$/.test(input)) return `${input}T00:00:00.000Z`;
  return input;
}

function toIsoEnd(input: string): string {
  // Bare date -> end-of-day UTC.
  if (/^\d{4}-\d{2}-\d{2}$/.test(input)) return `${input}T23:59:59.999Z`;
  return input;
}

class WhoopApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`Whoop API error ${status}: ${detail}`);
    this.name = "WhoopApiError";
  }
}

interface PagedResponse<T> {
  records: T[];
  next_token?: string | null;
}

async function whoopRequest<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const accessToken = await getValidAccessToken();
  const cleanParams: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") cleanParams[k] = v;
  }
  try {
    const response = await axios.get<T>(`${WHOOP_API_BASE}${path}`, {
      params: cleanParams,
      timeout: DEFAULT_TIMEOUT_MS,
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: "application/json",
      },
    });
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response) {
      const body =
        typeof error.response.data === "string"
          ? error.response.data.slice(0, 500)
          : JSON.stringify(error.response.data).slice(0, 500);
      throw new WhoopApiError(error.response.status, body);
    }
    throw error;
  }
}

async function whoopPaginated<T>(
  path: string,
  params: {
    start?: string;
    end?: string;
    limit: number;
    all_pages: boolean;
  },
): Promise<{ records: T[]; pages: number; truncated: boolean }> {
  const records: T[] = [];
  let pages = 0;
  let nextToken: string | undefined = undefined;
  let truncated = false;

  while (true) {
    const pageLimit = Math.min(WHOOP_PAGE_MAX, params.limit - records.length);
    if (pageLimit <= 0) break;
    const page: PagedResponse<T> = await whoopRequest<PagedResponse<T>>(path, {
      start: params.start,
      end: params.end,
      limit: pageLimit,
      nextToken,
    });
    pages += 1;
    const got = Array.isArray(page.records) ? page.records : [];
    records.push(...got);
    nextToken = page.next_token || undefined;

    if (records.length >= AUTO_PAGE_RECORD_CAP) {
      truncated = true;
      break;
    }
    if (records.length >= params.limit) break;
    if (!params.all_pages) break;
    if (!nextToken) break;
  }
  return { records, pages, truncated };
}

function handleApiError(error: unknown): string {
  if (error instanceof WhoopApiError) {
    if (error.status === 401) {
      return (
        "Error: Whoop rejected the access token. " +
        "If this persists, the refresh token may have been revoked — re-run `npm run auth` from the whoop server directory to re-authorize."
      );
    }
    if (error.status === 403) {
      return (
        "Error: Whoop denied access (403). The granted scopes may not cover this resource — re-authorize with all required scopes via `npm run auth`."
      );
    }
    if (error.status === 429) {
      return "Error: Whoop rate limit hit. Wait and retry.";
    }
    return `Error: ${error.message}`;
  }
  if (axios.isAxiosError(error)) {
    if ((error as AxiosError).code === "ECONNABORTED") {
      return "Error: Request to Whoop timed out after 30s. Try a narrower date range.";
    }
    return `Error: Network error reaching Whoop: ${error.message}`;
  }
  if (error instanceof Error && /WHOOP_CLIENT_ID/.test(error.message)) {
    return `Error: ${error.message}`;
  }
  if (error instanceof Error && /token file/.test(error.message)) {
    return `Error: ${error.message}`;
  }
  return `Error: ${error instanceof Error ? error.message : String(error)}`;
}

function maybeTruncate(
  textContent: string,
  payload: Record<string, unknown>,
): { text: string; payload: Record<string, unknown> } {
  if (textContent.length <= CHARACTER_LIMIT) return { text: textContent, payload };
  const truncated = {
    ...payload,
    truncated: true,
    truncation_message: `Response exceeded ${CHARACTER_LIMIT} characters. Narrow the date range or pass response_format='json' for the full payload via structuredContent.`,
  };
  const truncatedText = JSON.stringify(truncated, null, 2).slice(0, CHARACTER_LIMIT);
  return { text: truncatedText, payload: truncated };
}

// ---------------------------------------------------------------------------
// Whoop record types (representative — Whoop returns additional nested fields
// inside `score` that we pass through unchanged).
// ---------------------------------------------------------------------------

interface CycleRecord {
  id: number | string;
  user_id: number;
  created_at: string;
  updated_at: string;
  start: string;
  end: string | null;
  timezone_offset: string;
  score_state: string;
  score?: {
    strain?: number;
    kilojoule?: number;
    average_heart_rate?: number;
    max_heart_rate?: number;
  };
}

interface RecoveryRecord {
  cycle_id: number | string;
  sleep_id: string;
  user_id: number;
  created_at: string;
  updated_at: string;
  score_state: string;
  score?: {
    user_calibrating?: boolean;
    recovery_score?: number;
    resting_heart_rate?: number;
    hrv_rmssd_milli?: number;
    spo2_percentage?: number;
    skin_temp_celsius?: number;
  };
}

interface SleepRecord {
  id: string;
  v1_id?: number;
  user_id: number;
  created_at: string;
  updated_at: string;
  start: string;
  end: string;
  timezone_offset: string;
  nap: boolean;
  score_state: string;
  score?: {
    stage_summary?: {
      total_in_bed_time_milli?: number;
      total_awake_time_milli?: number;
      total_no_data_time_milli?: number;
      total_light_sleep_time_milli?: number;
      total_slow_wave_sleep_time_milli?: number;
      total_rem_sleep_time_milli?: number;
      sleep_cycle_count?: number;
      disturbance_count?: number;
    };
    sleep_needed?: {
      baseline_milli?: number;
      need_from_sleep_debt_milli?: number;
      need_from_recent_strain_milli?: number;
      need_from_recent_nap_milli?: number;
    };
    respiratory_rate?: number;
    sleep_performance_percentage?: number;
    sleep_consistency_percentage?: number;
    sleep_efficiency_percentage?: number;
  };
}

interface WorkoutRecord {
  id: string;
  v1_id?: number;
  user_id: number;
  created_at: string;
  updated_at: string;
  start: string;
  end: string;
  timezone_offset: string;
  sport_id?: number;
  sport_name?: string;
  score_state: string;
  score?: {
    strain?: number;
    average_heart_rate?: number;
    max_heart_rate?: number;
    kilojoule?: number;
    percent_recorded?: number;
    distance_meter?: number;
    altitude_gain_meter?: number;
    altitude_change_meter?: number;
    zone_durations?: Record<string, number>;
  };
}

interface BodyMeasurementRecord {
  height_meter?: number;
  weight_kilogram?: number;
  max_heart_rate?: number;
}

interface ProfileRecord {
  user_id: number;
  email?: string;
  first_name?: string;
  last_name?: string;
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtNum(n: number | undefined | null, digits = 1): string {
  if (n === undefined || n === null || !isFinite(n)) return "—";
  return n.toFixed(digits);
}

function msToHm(ms: number | undefined): string {
  if (!ms || !isFinite(ms)) return "—";
  const totalMin = Math.round(ms / 60_000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return `${h}h${m.toString().padStart(2, "0")}m`;
}

function shortDate(iso: string | undefined | null): string {
  if (!iso) return "—";
  return iso.slice(0, 16).replace("T", " ");
}

// ---------------------------------------------------------------------------

const server = new McpServer({
  name: "whoop-mcp-server",
  version: "1.0.0",
});

// ---------------------------------------------------------------------------
// Shared range schema for collection tools
// ---------------------------------------------------------------------------

const RangeShape = {
  start: DateOrIsoSchema.optional().describe(
    "Start of date range (inclusive). YYYY-MM-DD treated as 00:00:00 UTC. If omitted, Whoop defaults to ~24h ago.",
  ),
  end: DateOrIsoSchema.optional().describe(
    "End of date range (inclusive). YYYY-MM-DD treated as 23:59:59 UTC. If omitted, Whoop defaults to now.",
  ),
  limit: z
    .number()
    .int()
    .min(1)
    .max(AUTO_PAGE_RECORD_CAP)
    .default(50)
    .describe(
      `Maximum number of records to return across all pages. Whoop's per-page max is ${WHOOP_PAGE_MAX}; auto-pagination keeps fetching until this cap (or ${AUTO_PAGE_RECORD_CAP}) is reached.`,
    ),
  all_pages: z
    .boolean()
    .default(true)
    .describe(
      `If true (default), follow next_token until the limit or ${AUTO_PAGE_RECORD_CAP}-record safety cap is reached. If false, only one page is returned.`,
    ),
  response_format: ResponseFormatSchema,
};

function effectiveRange(start?: string, end?: string) {
  return {
    start: start ? toIsoStart(start) : undefined,
    end: end ? toIsoEnd(end) : undefined,
  };
}

// ---------------------------------------------------------------------------
// Tool: whoop_get_cycles
// ---------------------------------------------------------------------------

server.registerTool(
  "whoop_get_cycles",
  {
    title: "Get my Whoop physiological cycles",
    description: `List physiological cycles in a date range. A cycle is Whoop's day-equivalent unit (typically ~24h, anchored on sleep) and carries the day's strain score, calorie burn (kilojoules), and average/max heart rate.

Use this for questions about daily strain trend, exertion load, or "how hard did my body work last week?".

Args:
  - start (string, YYYY-MM-DD or ISO 8601, optional): inclusive range start.
  - end (string, YYYY-MM-DD or ISO 8601, optional): inclusive range end.
  - limit (int, default 50): max records across all pages (cap ${AUTO_PAGE_RECORD_CAP}).
  - all_pages (bool, default true): follow next_token automatically.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "pages_fetched": number,
    "cycles": [
      {
        "id": string|number,
        "start": string,        // ISO 8601
        "end": string|null,
        "score_state": string,  // 'SCORED' | 'PENDING_SCORE' | 'UNSCORABLE'
        "score": {
          "strain": number,             // 0–21 logarithmic
          "kilojoule": number,
          "average_heart_rate": number,
          "max_heart_rate": number
        }
      }
    ]
  }`,
    inputSchema: RangeShape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const { start, end } = effectiveRange(params.start, params.end);
      const { records, pages, truncated } = await whoopPaginated<CycleRecord>(
        "/v2/cycle",
        { start, end, limit: params.limit, all_pages: params.all_pages },
      );
      const output = {
        count: records.length,
        pages_fetched: pages,
        truncated_at_safety_cap: truncated,
        range: { start, end },
        cycles: records,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Whoop cycles (${records.length})`,
          start || end ? `range: ${start ?? "(default)"} → ${end ?? "(now)"}` : "range: default",
          "",
          "| Start | End | Strain | Avg HR | Max HR | kJ | State |",
          "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ];
        for (const c of records) {
          lines.push(
            `| ${shortDate(c.start)} | ${shortDate(c.end)} | ${fmtNum(c.score?.strain, 2)} | ${fmtNum(c.score?.average_heart_rate, 0)} | ${fmtNum(c.score?.max_heart_rate, 0)} | ${fmtNum(c.score?.kilojoule, 0)} | ${c.score_state} |`,
          );
        }
        if (!records.length) lines.push("_No cycles in range._");
        if (truncated) lines.push("", `_Hit the ${AUTO_PAGE_RECORD_CAP}-record safety cap; narrow the range._`);
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }
      const final = maybeTruncate(text, output);
      return { content: [{ type: "text", text: final.text }], structuredContent: final.payload };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_recovery
// ---------------------------------------------------------------------------

server.registerTool(
  "whoop_get_recovery",
  {
    title: "Get my Whoop recovery scores",
    description: `List recovery records in a date range. Each recovery is computed at the end of a sleep and ties to a cycle. Includes recovery score (0–100), resting heart rate, HRV (heart rate variability, RMSSD ms), SpO2, and skin temperature.

Use this for questions about readiness, recovery trend, HRV/RHR baselines, or "did I sleep well enough to push hard today?".

Args:
  - start (string, YYYY-MM-DD or ISO 8601, optional): inclusive range start.
  - end (string, YYYY-MM-DD or ISO 8601, optional): inclusive range end.
  - limit (int, default 50): max records across all pages (cap ${AUTO_PAGE_RECORD_CAP}).
  - all_pages (bool, default true): follow next_token automatically.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "pages_fetched": number,
    "recoveries": [
      {
        "cycle_id": string|number,
        "sleep_id": string,
        "score_state": string,
        "score": {
          "recovery_score": number,         // 0–100
          "resting_heart_rate": number,     // bpm
          "hrv_rmssd_milli": number,        // ms
          "spo2_percentage": number,
          "skin_temp_celsius": number,
          "user_calibrating": boolean       // true if Whoop is still building the user's baseline
        }
      }
    ]
  }`,
    inputSchema: RangeShape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const { start, end } = effectiveRange(params.start, params.end);
      const { records, pages, truncated } = await whoopPaginated<RecoveryRecord>(
        "/v2/recovery",
        { start, end, limit: params.limit, all_pages: params.all_pages },
      );
      const output = {
        count: records.length,
        pages_fetched: pages,
        truncated_at_safety_cap: truncated,
        range: { start, end },
        recoveries: records,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Whoop recovery (${records.length})`,
          start || end ? `range: ${start ?? "(default)"} → ${end ?? "(now)"}` : "range: default",
          "",
          "| Created | Score | RHR | HRV (ms) | SpO2 % | Skin °C | State |",
          "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ];
        for (const r of records) {
          lines.push(
            `| ${shortDate(r.created_at)} | ${fmtNum(r.score?.recovery_score, 0)} | ${fmtNum(r.score?.resting_heart_rate, 0)} | ${fmtNum(r.score?.hrv_rmssd_milli, 1)} | ${fmtNum(r.score?.spo2_percentage, 1)} | ${fmtNum(r.score?.skin_temp_celsius, 1)} | ${r.score_state} |`,
          );
        }
        if (!records.length) lines.push("_No recovery records in range._");
        if (truncated) lines.push("", `_Hit the ${AUTO_PAGE_RECORD_CAP}-record safety cap; narrow the range._`);
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }
      const final = maybeTruncate(text, output);
      return { content: [{ type: "text", text: final.text }], structuredContent: final.payload };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_sleep
// ---------------------------------------------------------------------------

server.registerTool(
  "whoop_get_sleep",
  {
    title: "Get my Whoop sleep sessions",
    description: `List sleep sessions in a date range. Includes both main sleeps and naps (use the 'nap' field to distinguish). For each session: stage breakdown (light, slow-wave/SWS, REM, awake), respiratory rate, sleep performance/consistency/efficiency percentages, and sleep-need calculation.

Use this for questions about sleep duration/quality, deep+REM patterns, sleep debt, "did I get enough deep sleep last night?".

Args:
  - start (string, YYYY-MM-DD or ISO 8601, optional): inclusive range start.
  - end (string, YYYY-MM-DD or ISO 8601, optional): inclusive range end.
  - limit (int, default 50): max records across all pages (cap ${AUTO_PAGE_RECORD_CAP}).
  - all_pages (bool, default true): follow next_token automatically.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "pages_fetched": number,
    "sleeps": [
      {
        "id": string,
        "start": string,
        "end": string,
        "nap": boolean,
        "score_state": string,
        "score": {
          "stage_summary": {
            "total_in_bed_time_milli": number,
            "total_awake_time_milli": number,
            "total_light_sleep_time_milli": number,
            "total_slow_wave_sleep_time_milli": number,
            "total_rem_sleep_time_milli": number,
            "sleep_cycle_count": number,
            "disturbance_count": number
          },
          "sleep_needed": {
            "baseline_milli": number,
            "need_from_sleep_debt_milli": number,
            "need_from_recent_strain_milli": number
          },
          "respiratory_rate": number,
          "sleep_performance_percentage": number,
          "sleep_consistency_percentage": number,
          "sleep_efficiency_percentage": number
        }
      }
    ]
  }`,
    inputSchema: RangeShape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const { start, end } = effectiveRange(params.start, params.end);
      const { records, pages, truncated } = await whoopPaginated<SleepRecord>(
        "/v2/activity/sleep",
        { start, end, limit: params.limit, all_pages: params.all_pages },
      );
      const output = {
        count: records.length,
        pages_fetched: pages,
        truncated_at_safety_cap: truncated,
        range: { start, end },
        sleeps: records,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Whoop sleep (${records.length})`,
          start || end ? `range: ${start ?? "(default)"} → ${end ?? "(now)"}` : "range: default",
          "",
          "| Start | Type | In bed | Light | SWS | REM | Awake | Perf % | Eff % |",
          "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ];
        for (const s of records) {
          const stages = s.score?.stage_summary;
          lines.push(
            `| ${shortDate(s.start)} | ${s.nap ? "nap" : "main"} | ${msToHm(stages?.total_in_bed_time_milli)} | ${msToHm(stages?.total_light_sleep_time_milli)} | ${msToHm(stages?.total_slow_wave_sleep_time_milli)} | ${msToHm(stages?.total_rem_sleep_time_milli)} | ${msToHm(stages?.total_awake_time_milli)} | ${fmtNum(s.score?.sleep_performance_percentage, 0)} | ${fmtNum(s.score?.sleep_efficiency_percentage, 0)} |`,
          );
        }
        if (!records.length) lines.push("_No sleep records in range._");
        if (truncated) lines.push("", `_Hit the ${AUTO_PAGE_RECORD_CAP}-record safety cap; narrow the range._`);
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }
      const final = maybeTruncate(text, output);
      return { content: [{ type: "text", text: final.text }], structuredContent: final.payload };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_workouts
// ---------------------------------------------------------------------------

server.registerTool(
  "whoop_get_workouts",
  {
    title: "Get my Whoop workouts",
    description: `List workouts in a date range. Each workout has a strain score (0–21, log-scale), HR-zone time breakdown, distance, altitude change, kilojoules burned, and a sport classification (sport_id / sport_name when available).

Use this for questions about exercise load, sport mix, intensity zones, "what was my hardest workout this month?".

Args:
  - start (string, YYYY-MM-DD or ISO 8601, optional): inclusive range start.
  - end (string, YYYY-MM-DD or ISO 8601, optional): inclusive range end.
  - limit (int, default 50): max records across all pages (cap ${AUTO_PAGE_RECORD_CAP}).
  - all_pages (bool, default true): follow next_token automatically.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "pages_fetched": number,
    "workouts": [
      {
        "id": string,
        "start": string,
        "end": string,
        "sport_id": number,
        "sport_name": string,
        "score_state": string,
        "score": {
          "strain": number,                  // 0–21
          "average_heart_rate": number,
          "max_heart_rate": number,
          "kilojoule": number,
          "percent_recorded": number,
          "distance_meter": number,
          "altitude_gain_meter": number,
          "zone_durations": { "zone_zero_milli": number, "zone_one_milli": number, ... }
        }
      }
    ]
  }`,
    inputSchema: RangeShape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const { start, end } = effectiveRange(params.start, params.end);
      const { records, pages, truncated } = await whoopPaginated<WorkoutRecord>(
        "/v2/activity/workout",
        { start, end, limit: params.limit, all_pages: params.all_pages },
      );
      const output = {
        count: records.length,
        pages_fetched: pages,
        truncated_at_safety_cap: truncated,
        range: { start, end },
        workouts: records,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Whoop workouts (${records.length})`,
          start || end ? `range: ${start ?? "(default)"} → ${end ?? "(now)"}` : "range: default",
          "",
          "| Start | Sport | Strain | Avg HR | Max HR | kJ | Distance (m) |",
          "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ];
        for (const w of records) {
          lines.push(
            `| ${shortDate(w.start)} | ${w.sport_name ?? `id:${w.sport_id ?? "?"}`} | ${fmtNum(w.score?.strain, 2)} | ${fmtNum(w.score?.average_heart_rate, 0)} | ${fmtNum(w.score?.max_heart_rate, 0)} | ${fmtNum(w.score?.kilojoule, 0)} | ${fmtNum(w.score?.distance_meter, 0)} |`,
          );
        }
        if (!records.length) lines.push("_No workouts in range._");
        if (truncated) lines.push("", `_Hit the ${AUTO_PAGE_RECORD_CAP}-record safety cap; narrow the range._`);
        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }
      const final = maybeTruncate(text, output);
      return { content: [{ type: "text", text: final.text }], structuredContent: final.payload };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_body_measurement
// ---------------------------------------------------------------------------

const SimpleSchema = z.object({ response_format: ResponseFormatSchema }).strict();

server.registerTool(
  "whoop_get_body_measurement",
  {
    title: "Get my Whoop body measurements",
    description: `Return current body measurements: height (m), weight (kg), maximum heart rate (bpm). These come from the user's profile and rarely change.

Args:
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "height_meter": number,
    "weight_kilogram": number,
    "max_heart_rate": number
  }`,
    inputSchema: SimpleSchema.shape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const data = await whoopRequest<BodyMeasurementRecord>("/v2/user/measurement/body");
      const text =
        params.response_format === ResponseFormat.MARKDOWN
          ? [
              "# Whoop body measurements",
              "",
              `- Height: ${fmtNum(data.height_meter, 2)} m`,
              `- Weight: ${fmtNum(data.weight_kilogram, 1)} kg`,
              `- Max HR: ${fmtNum(data.max_heart_rate, 0)} bpm`,
            ].join("\n")
          : JSON.stringify(data, null, 2);
      return {
        content: [{ type: "text", text }],
        structuredContent: data as unknown as Record<string, unknown>,
      };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_profile
// ---------------------------------------------------------------------------

server.registerTool(
  "whoop_get_profile",
  {
    title: "Get my Whoop profile",
    description: `Return basic profile info (user ID, email, first/last name).

Args:
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "user_id": number,
    "email": string,
    "first_name": string,
    "last_name": string
  }`,
    inputSchema: SimpleSchema.shape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const data = await whoopRequest<ProfileRecord>("/v2/user/profile/basic");
      const text =
        params.response_format === ResponseFormat.MARKDOWN
          ? [
              "# Whoop profile",
              "",
              `- User ID: ${data.user_id}`,
              `- Name: ${data.first_name ?? "—"} ${data.last_name ?? ""}`.trim(),
              `- Email: ${data.email ?? "—"}`,
            ].join("\n")
          : JSON.stringify(data, null, 2);
      return {
        content: [{ type: "text", text }],
        structuredContent: data as unknown as Record<string, unknown>,
      };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------
// Tool: whoop_get_today
// ---------------------------------------------------------------------------

const TodaySchema = z
  .object({
    date: z
      .string()
      .regex(/^\d{4}-\d{2}-\d{2}$/, "Must be YYYY-MM-DD")
      .optional()
      .describe("Date to summarise, YYYY-MM-DD. Defaults to today (UTC)."),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "whoop_get_today",
  {
    title: "Get my Whoop snapshot for today (or a chosen date)",
    description: `Convenience tool: fetch the cycle, recovery, sleep, and workouts for a single day in one call. Useful for daily routine analysis ("what did Whoop say about today so far?").

The Whoop "day" is anchored on the cycle, which usually starts after the previous night's main sleep. To capture both, this tool queries the range [date 00:00 UTC, date+1 00:00 UTC). Recovery/sleep that began the previous evening will appear here.

Args:
  - date (string, YYYY-MM-DD, optional): defaults to today (UTC).
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "date": string,
    "cycles": CycleRecord[],
    "recoveries": RecoveryRecord[],
    "sleeps": SleepRecord[],
    "workouts": WorkoutRecord[]
  }`,
    inputSchema: TodaySchema.shape,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  },
  async (params) => {
    try {
      const date = params.date ?? new Date().toISOString().slice(0, 10);
      const start = `${date}T00:00:00.000Z`;
      const nextDay = new Date(`${date}T00:00:00.000Z`);
      nextDay.setUTCDate(nextDay.getUTCDate() + 1);
      const end = nextDay.toISOString();

      const [cycles, recoveries, sleeps, workouts] = await Promise.all([
        whoopPaginated<CycleRecord>("/v2/cycle", { start, end, limit: 25, all_pages: true }),
        whoopPaginated<RecoveryRecord>("/v2/recovery", { start, end, limit: 25, all_pages: true }),
        whoopPaginated<SleepRecord>("/v2/activity/sleep", { start, end, limit: 25, all_pages: true }),
        whoopPaginated<WorkoutRecord>("/v2/activity/workout", { start, end, limit: 25, all_pages: true }),
      ]);

      const output = {
        date,
        range: { start, end },
        cycles: cycles.records,
        recoveries: recoveries.records,
        sleeps: sleeps.records,
        workouts: workouts.records,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [`# Whoop — ${date}`, ""];

        const cycle = cycles.records[0];
        const rec = recoveries.records[0];
        const mainSleep = sleeps.records.find((s) => !s.nap) ?? sleeps.records[0];

        lines.push("## Recovery & cycle");
        if (rec) {
          lines.push(
            `- Recovery: **${fmtNum(rec.score?.recovery_score, 0)}** / 100`,
            `- HRV: ${fmtNum(rec.score?.hrv_rmssd_milli, 1)} ms · RHR: ${fmtNum(rec.score?.resting_heart_rate, 0)} bpm`,
            `- SpO2: ${fmtNum(rec.score?.spo2_percentage, 1)} % · Skin temp: ${fmtNum(rec.score?.skin_temp_celsius, 1)} °C`,
          );
        } else {
          lines.push("- _No recovery yet._");
        }
        if (cycle) {
          lines.push(
            `- Day strain: **${fmtNum(cycle.score?.strain, 2)}** / 21 (${cycle.score_state})`,
            `- Avg HR: ${fmtNum(cycle.score?.average_heart_rate, 0)} · Max HR: ${fmtNum(cycle.score?.max_heart_rate, 0)} · ${fmtNum(cycle.score?.kilojoule, 0)} kJ`,
          );
        }

        lines.push("", "## Sleep");
        if (mainSleep) {
          const stages = mainSleep.score?.stage_summary;
          lines.push(
            `- ${shortDate(mainSleep.start)} → ${shortDate(mainSleep.end)}${mainSleep.nap ? " (nap)" : ""}`,
            `- In bed: ${msToHm(stages?.total_in_bed_time_milli)} · Light ${msToHm(stages?.total_light_sleep_time_milli)} · SWS ${msToHm(stages?.total_slow_wave_sleep_time_milli)} · REM ${msToHm(stages?.total_rem_sleep_time_milli)} · Awake ${msToHm(stages?.total_awake_time_milli)}`,
            `- Performance: ${fmtNum(mainSleep.score?.sleep_performance_percentage, 0)}% · Efficiency: ${fmtNum(mainSleep.score?.sleep_efficiency_percentage, 0)}% · Consistency: ${fmtNum(mainSleep.score?.sleep_consistency_percentage, 0)}%`,
            `- Disturbances: ${mainSleep.score?.stage_summary?.disturbance_count ?? "—"}`,
          );
          if (sleeps.records.length > 1) {
            lines.push(`- _${sleeps.records.length - 1} additional sleep record(s) in range._`);
          }
        } else {
          lines.push("- _No sleep records yet._");
        }

        lines.push("", "## Workouts");
        if (workouts.records.length) {
          for (const w of workouts.records) {
            lines.push(
              `- **${w.sport_name ?? `sport ${w.sport_id ?? "?"}`}** ${shortDate(w.start)} — strain ${fmtNum(w.score?.strain, 2)}, ${fmtNum(w.score?.kilojoule, 0)} kJ`,
            );
          }
        } else {
          lines.push("- _None._");
        }

        text = lines.join("\n");
      } else {
        text = JSON.stringify(output, null, 2);
      }
      const final = maybeTruncate(text, output);
      return { content: [{ type: "text", text: final.text }], structuredContent: final.payload };
    } catch (error) {
      return { content: [{ type: "text", text: handleApiError(error) }], isError: true };
    }
  },
);

// ---------------------------------------------------------------------------

async function main() {
  // Fail fast if creds missing or tokens.json is unreadable.
  try {
    getClientCreds();
    await loadTokens();
  } catch (e) {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("whoop-mcp-server running on stdio");
}

main().catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});
