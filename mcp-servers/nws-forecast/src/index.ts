#!/usr/bin/env node
/**
 * MCP server for the US National Weather Service forecast API (api.weather.gov).
 *
 * Exposes textual forecast, hourly forecast, and active alerts for a lat/lon.
 * NWS API is free and unauthenticated but requires a User-Agent header
 * identifying the application + contact email.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";

const API_BASE_URL = "https://api.weather.gov";
const USER_AGENT =
  process.env.NWS_USER_AGENT ?? "LLM-Land-NWS-MCP/1.0 (accounts@dmanda.com)";
const CHARACTER_LIMIT = 25000;
const DEFAULT_TIMEOUT_MS = 30000;

const httpClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: DEFAULT_TIMEOUT_MS,
  headers: {
    "User-Agent": USER_AGENT,
    Accept: "application/geo+json",
  },
});

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

const LatLonSchema = {
  lat: z
    .number()
    .min(-90)
    .max(90)
    .describe("Latitude in decimal degrees (NWS coverage: US + territories only)"),
  lon: z
    .number()
    .min(-180)
    .max(180)
    .describe("Longitude in decimal degrees"),
};

interface PointResponse {
  properties: {
    gridId: string;
    gridX: number;
    gridY: number;
    forecast: string;
    forecastHourly: string;
    forecastZone: string;
    county: string;
    fireWeatherZone: string;
    timeZone: string;
    radarStation: string;
    relativeLocation?: {
      properties?: { city?: string; state?: string };
    };
  };
}

interface ForecastPeriod {
  number: number;
  name: string;
  startTime: string;
  endTime: string;
  isDaytime: boolean;
  temperature: number;
  temperatureUnit: string;
  temperatureTrend?: string | null;
  probabilityOfPrecipitation?: { value: number | null };
  dewpoint?: { value: number | null; unitCode?: string };
  relativeHumidity?: { value: number | null };
  windSpeed: string;
  windDirection: string;
  icon?: string;
  shortForecast: string;
  detailedForecast?: string;
}

interface ForecastResponse {
  properties: {
    updated: string;
    units: string;
    forecastGenerator: string;
    generatedAt: string;
    updateTime: string;
    validTimes: string;
    elevation: { value: number; unitCode: string };
    periods: ForecastPeriod[];
  };
}

interface AlertFeature {
  id: string;
  properties: {
    id: string;
    areaDesc: string;
    sent: string;
    effective: string;
    onset?: string;
    expires: string;
    ends?: string;
    status: string;
    messageType: string;
    category: string;
    severity: string;
    certainty: string;
    urgency: string;
    event: string;
    sender: string;
    senderName: string;
    headline: string;
    description: string;
    instruction?: string;
    response?: string;
  };
}

interface AlertsResponse {
  features: AlertFeature[];
  title?: string;
  updated?: string;
}

// --- Marine zone forecast (Coastal Waters Forecast text product) ---

interface MarineZonesResponse {
  features: { properties: { id: string; name: string } }[];
}

interface ZoneMetaResponse {
  properties: { id: string; name: string; forecastOffices?: string[]; cwa?: string[] };
}

interface ProductsListResponse {
  "@graph"?: { id: string; issuanceTime: string }[];
}

interface ProductResponse {
  productText: string;
  issuanceTime?: string;
}

class NwsApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(`NWS API ${status}: ${detail}`);
    this.name = "NwsApiError";
  }
}

/** Plain-text/HTML GET against an absolute URL, outside the api.weather.gov base client.
 *  Used only by the marine-forecast shmrn.php fallback (automation #37, 2026-08-12) for the
 *  Arctic 15-85 nm zones, which exist in no api.weather.gov text product. */
async function fetchText(url: string, timeoutMs = 15000): Promise<string> {
  const response = await axios.get<string>(url, {
    timeout: timeoutMs,
    responseType: "text",
    headers: { "User-Agent": USER_AGENT, Accept: "text/html,text/plain" },
  });
  return typeof response.data === "string" ? response.data : String(response.data);
}

async function nwsRequest<T>(path: string, accept?: string): Promise<T> {
  try {
    const response = await httpClient.get<T>(
      path,
      accept ? { headers: { Accept: accept } } : undefined,
    );
    return response.data;
  } catch (error) {
    if (axios.isAxiosError(error) && error.response) {
      const detail =
        (error.response.data as { detail?: string })?.detail ??
        error.response.statusText ??
        "no detail";
      throw new NwsApiError(error.response.status, detail);
    }
    throw error;
  }
}

function handleApiError(error: unknown): string {
  if (error instanceof NwsApiError) {
    if (error.status === 404) {
      return (
        "Error: NWS has no data for that point. Verify the lat/lon is inside US coverage " +
        "(50 states + territories); marine zones use different endpoints."
      );
    }
    if (error.status === 500 || error.status === 503) {
      return `Error: NWS upstream issue (${error.status}). Retry shortly.`;
    }
    return `Error: ${error.message}`;
  }
  if (axios.isAxiosError(error)) {
    if (error.code === "ECONNABORTED") {
      return "Error: Request to NWS timed out after 30s.";
    }
    return `Error: Network error reaching NWS: ${error.message}`;
  }
  return `Error: ${error instanceof Error ? error.message : String(error)}`;
}

const pointCache = new Map<
  string,
  { value: PointResponse["properties"]; expiresAt: number }
>();
const POINT_CACHE_TTL_MS = 24 * 60 * 60 * 1000;

async function resolvePoint(
  lat: number,
  lon: number,
): Promise<PointResponse["properties"]> {
  const key = `${lat.toFixed(4)},${lon.toFixed(4)}`;
  const cached = pointCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.value;

  const data = await nwsRequest<PointResponse>(`/points/${key}`);
  const props = data.properties;
  pointCache.set(key, { value: props, expiresAt: Date.now() + POINT_CACHE_TTL_MS });
  return props;
}

function maybeTruncate(
  textContent: string,
  payload: Record<string, unknown>,
): { text: string; payload: Record<string, unknown> } {
  if (textContent.length <= CHARACTER_LIMIT) return { text: textContent, payload };
  const truncated = {
    ...payload,
    truncated: true,
    truncation_message: `Response exceeded ${CHARACTER_LIMIT} characters. Reduce 'periods' or filter alerts.`,
  };
  return {
    text: JSON.stringify(truncated, null, 2).slice(0, CHARACTER_LIMIT),
    payload: truncated,
  };
}

function summarizePeriod(p: ForecastPeriod) {
  return {
    name: p.name,
    start: p.startTime,
    end: p.endTime,
    is_daytime: p.isDaytime,
    temperature_f: p.temperature,
    temperature_trend: p.temperatureTrend ?? null,
    precip_prob_percent: p.probabilityOfPrecipitation?.value ?? null,
    relative_humidity_percent: p.relativeHumidity?.value ?? null,
    wind: `${p.windDirection} ${p.windSpeed}`,
    short: p.shortForecast,
    detailed: p.detailedForecast,
  };
}

const server = new McpServer({
  name: "nws-forecast-mcp-server",
  version: "1.0.0",
});

const ForecastSchema = z
  .object({
    ...LatLonSchema,
    periods: z
      .number()
      .int()
      .min(1)
      .max(14)
      .default(7)
      .describe(
        "How many forecast periods to return. NWS gives 14 periods (7 days × day/night). Default 7 = next ~3.5 days.",
      ),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "nws_get_forecast",
  {
    title: "Get NWS textual forecast for a point",
    description: `Fetch the multi-day NWS textual forecast for a US lat/lon. Each period covers roughly 12 hours (day or night) and includes temperature, wind, precipitation probability, and a short + detailed text forecast.

Use this for "what's the weather like at REFTRA tomorrow?", "is the next 3 days going to be cold in Kodiak?", general planning.

Args:
  - lat (number, -90..90): decimal latitude.
  - lon (number, -180..180): decimal longitude.
  - periods (number, 1-14, default 7): how many of NWS's day/night periods to return.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "location": { "city": string, "state": string, "office": string, "grid": "x,y", "timezone": string },
    "generated_at": string,
    "periods": [
      {
        "name": string,                       // e.g. "Tonight", "Tuesday", "Tuesday Night"
        "start": string,                      // ISO 8601
        "end": string,
        "is_daytime": boolean,
        "temperature_f": number,
        "temperature_trend": string | null,
        "precip_prob_percent": number | null,
        "relative_humidity_percent": number | null,
        "wind": string,                       // direction + speed text
        "short": string,                      // short forecast
        "detailed": string                    // detailed paragraph
      }
    ]
  }

Coverage: NWS only covers US + territories. Outside the US, returns 404.

Errors:
  - 404 -> point outside NWS coverage; verify lat/lon.
  - 500/503 -> upstream issue; retry shortly.`,
    inputSchema: ForecastSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const point = await resolvePoint(params.lat, params.lon);
      const forecast = await nwsRequest<ForecastResponse>(
        `/gridpoints/${point.gridId}/${point.gridX},${point.gridY}/forecast`,
      );
      const periods = forecast.properties.periods.slice(0, params.periods);
      const summary = periods.map(summarizePeriod);

      const output = {
        location: {
          city: point.relativeLocation?.properties?.city,
          state: point.relativeLocation?.properties?.state,
          office: point.gridId,
          grid: `${point.gridX},${point.gridY}`,
          timezone: point.timeZone,
        },
        generated_at: forecast.properties.generatedAt,
        periods: summary,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Forecast — ${output.location.city ?? "?"}, ${output.location.state ?? "?"} (${point.gridId} ${output.location.grid})`,
          `Generated: ${output.generated_at}`,
          "",
        ];
        for (const p of summary) {
          lines.push(`## ${p.name} — ${p.short}`);
          const bits: string[] = [`${p.temperature_f}°F`];
          if (p.precip_prob_percent != null) bits.push(`precip ${p.precip_prob_percent}%`);
          if (p.relative_humidity_percent != null)
            bits.push(`humidity ${p.relative_humidity_percent}%`);
          bits.push(`wind ${p.wind}`);
          lines.push(`- ${bits.join(" · ")}`);
          if (p.detailed) lines.push(`- ${p.detailed}`);
          lines.push("");
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

const HourlySchema = z
  .object({
    ...LatLonSchema,
    hours: z
      .number()
      .int()
      .min(1)
      .max(156)
      .default(24)
      .describe(
        "How many hourly periods to return. NWS provides up to ~156 hours (~6.5 days). Default 24 = next day.",
      ),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "nws_get_hourly_forecast",
  {
    title: "Get NWS hourly forecast for a point",
    description: `Fetch the hourly NWS forecast for a US lat/lon. Each period is one hour and includes temperature, precipitation probability, humidity, wind, and a short conditions phrase.

Use this for granular planning: "should I run outside at 6 PM?", "when does the rain start tomorrow?".

Args:
  - lat (number): decimal latitude.
  - lon (number): decimal longitude.
  - hours (number, 1-156, default 24): how many hours to return.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "location": { "city": string, "state": string, "office": string, "grid": "x,y", "timezone": string },
    "generated_at": string,
    "hours": [
      {
        "start": string,                      // ISO 8601 (with TZ offset)
        "temperature_f": number,
        "precip_prob_percent": number | null,
        "relative_humidity_percent": number | null,
        "wind": string,
        "short": string
      }
    ]
  }

Errors:
  - 404 -> point outside NWS coverage.
  - 500/503 -> upstream issue.`,
    inputSchema: HourlySchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const point = await resolvePoint(params.lat, params.lon);
      const forecast = await nwsRequest<ForecastResponse>(
        `/gridpoints/${point.gridId}/${point.gridX},${point.gridY}/forecast/hourly`,
      );
      const periods = forecast.properties.periods.slice(0, params.hours);
      const summary = periods.map((p) => ({
        start: p.startTime,
        temperature_f: p.temperature,
        precip_prob_percent: p.probabilityOfPrecipitation?.value ?? null,
        relative_humidity_percent: p.relativeHumidity?.value ?? null,
        wind: `${p.windDirection} ${p.windSpeed}`,
        short: p.shortForecast,
      }));

      const output = {
        location: {
          city: point.relativeLocation?.properties?.city,
          state: point.relativeLocation?.properties?.state,
          office: point.gridId,
          grid: `${point.gridX},${point.gridY}`,
          timezone: point.timeZone,
        },
        generated_at: forecast.properties.generatedAt,
        hours: summary,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Hourly forecast — ${output.location.city ?? "?"}, ${output.location.state ?? "?"}`,
          `Generated: ${output.generated_at}`,
          "",
          "| Time | Temp | Precip | RH | Wind | Conditions |",
          "|---|---|---|---|---|---|",
        ];
        for (const h of summary) {
          const time = h.start.slice(0, 16).replace("T", " ");
          lines.push(
            `| ${time} | ${h.temperature_f}°F | ${h.precip_prob_percent ?? "—"}% | ${h.relative_humidity_percent ?? "—"}% | ${h.wind} | ${h.short} |`,
          );
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

const AlertsSchema = z
  .object({
    ...LatLonSchema,
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "nws_get_alerts",
  {
    title: "Get active NWS alerts for a point",
    description: `Fetch active NWS alerts (watches, warnings, advisories) covering a lat/lon. Returns the empty list if nothing is active.

Use this before outdoor plans, before flights, or to surface conditions worth knowing about during /daily.

Args:
  - lat (number): decimal latitude.
  - lon (number): decimal longitude.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "alerts": [
      {
        "id": string,
        "event": string,                      // e.g. "Severe Thunderstorm Warning"
        "severity": string,                   // Extreme | Severe | Moderate | Minor | Unknown
        "urgency": string,                    // Immediate | Expected | Future | Past | Unknown
        "certainty": string,
        "headline": string,
        "area": string,
        "effective": string,
        "expires": string,
        "description": string,
        "instruction": string | null
      }
    ]
  }`,
    inputSchema: AlertsSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await nwsRequest<AlertsResponse>(
        `/alerts/active?point=${params.lat.toFixed(4)},${params.lon.toFixed(4)}`,
      );
      const alerts = (data.features ?? []).map((f) => ({
        id: f.properties.id,
        event: f.properties.event,
        severity: f.properties.severity,
        urgency: f.properties.urgency,
        certainty: f.properties.certainty,
        headline: f.properties.headline,
        area: f.properties.areaDesc,
        effective: f.properties.effective,
        expires: f.properties.expires,
        description: f.properties.description,
        instruction: f.properties.instruction ?? null,
      }));
      const output = { count: alerts.length, alerts };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        if (!alerts.length) {
          text = `# NWS alerts — none active for ${params.lat},${params.lon}`;
        } else {
          const lines: string[] = [
            `# NWS alerts — ${alerts.length} active for ${params.lat},${params.lon}`,
            "",
          ];
          for (const a of alerts) {
            lines.push(`## ${a.event} (${a.severity}/${a.urgency})`);
            lines.push(`- area: ${a.area}`);
            lines.push(`- effective ${a.effective} → expires ${a.expires}`);
            if (a.headline) lines.push(`- ${a.headline}`);
            if (a.instruction) lines.push(`- instruction: ${a.instruction}`);
            lines.push("");
          }
          text = lines.join("\n");
        }
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

async function resolveMarineZone(
  lat: number,
  lon: number,
): Promise<{ id: string; name: string } | null> {
  const data = await nwsRequest<MarineZonesResponse>(
    `/zones?type=marine&point=${lat.toFixed(4)},${lon.toFixed(4)}`,
  );
  const f = data.features?.[0];
  return f ? { id: f.properties.id, name: f.properties.name } : null;
}

function officeFromUrl(url: string): string {
  // "https://api.weather.gov/offices/ALU" -> "ALU"
  return url.split("/").filter(Boolean).pop() ?? "";
}

/**
 * A Coastal Waters Forecast product covers many zones, each block separated by "$$".
 * Pull the block for one zone, matching by zone name (most reliable) then the UGC code suffix.
 */
function extractZoneSegment(
  productText: string,
  zoneId: string,
  zoneName: string,
): string | null {
  const suffix = zoneId.replace(/^[A-Za-z]{3}/, ""); // PKZ767 -> 767
  const nameLc = zoneName.toLowerCase();
  for (const seg of productText.split("$$")) {
    const head = seg.slice(0, 400).toLowerCase();
    if (
      head.includes(nameLc) ||
      head.includes(zoneId.toLowerCase()) ||
      head.includes(`${suffix}-`) ||
      head.includes(`-${suffix}-`) ||
      head.includes(`>${suffix}-`)
    ) {
      return seg.trim();
    }
  }
  return null;
}

const MarineSchema = z
  .object({
    ...LatLonSchema,
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "nws_get_marine_forecast",
  {
    title: "Get NWS marine (Coastal Waters) forecast for an offshore point",
    description: `Fetch the NWS marine forecast — wind + seas — for a lat/lon at sea, via the Coastal Waters Forecast (CWF) text product for the containing marine zone. Use this for a SHIP underway; the land 'nws_get_forecast' tool snaps offshore points to the nearest coastal town (e.g. a Bering Sea point lands on Mekoryuk ~150 nm away) and does not give seas.

Flow: lat/lon -> marine zone (e.g. PKZ767 "Saint Matthew Island Waters") -> issuing office (e.g. ALU) -> latest CWF product -> the zone's block. Also returns active marine alerts (gale/storm/heavy-freezing-spray warnings) for that zone.

Args:
  - lat (number, -90..90): decimal latitude (must be over water).
  - lon (number, -180..180): decimal longitude.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "zone": { "id": string, "name": string, "office": string },
    "issued": string,                 // product issuance time text
    "forecast_text": string | null,   // the zone's CWF block (period-by-period wind + seas)
    "alerts": [ { "event","severity","headline","effective","expires" } ]
  }

Notes:
  - If the point resolves to LAND (no marine zone), returns a note suggesting you nudge offshore or use nws_get_forecast.
  - AK marine zones publish as text (CWF), not gridpoint periods — forecast_text is the raw zone block, already concise.
  - Pairs with nws_get_alerts (point-based). For a ship, marine alerts here are the authoritative safety product.`,
    inputSchema: MarineSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const zone = await resolveMarineZone(params.lat, params.lon);
      if (!zone) {
        const msg =
          `No NWS marine zone contains ${params.lat},${params.lon} — it likely resolves to land ` +
          `(or is outside US marine coverage). Nudge the point a few miles offshore, or use nws_get_forecast for a coastal land grid.`;
        return {
          content: [{ type: "text", text: msg }],
          structuredContent: { zone: null, issued: "", forecast_text: null, alerts: [] },
        };
      }

      const meta = await nwsRequest<ZoneMetaResponse>(`/zones/marine/${zone.id}`);
      const office = officeFromUrl(meta.properties.forecastOffices?.[0] ?? "");

      const alertsData = await nwsRequest<AlertsResponse>(
        `/alerts/active?zone=${zone.id}`,
      );
      const alerts = (alertsData.features ?? []).map((f) => ({
        event: f.properties.event,
        severity: f.properties.severity,
        headline: f.properties.headline,
        effective: f.properties.effective,
        expires: f.properties.expires,
      }));

      // Marine text products are keyed by AWIPS *location*, which is NOT always the issuing
      // office. ALU/AJK issue CWF under their own code and AFG issues OFF for the Arctic
      // offshore zones — but AFG's COASTAL zones (PKZ80x/85x) are published under location
      // **WCZ** ("CWFWCZ", Northwest Alaska Coastal Waters), and AFG itself lists NO CWF
      // products at all. An office-only lookup therefore returned null text for every
      // nearshore Arctic zone, which cost five consecutive /daily runs a manual hand-pull
      // (automation #37; PKZ857 on 8/07 and 8/10, PKZ854 on 8/11, PKZ805 on 8/12).
      // Fix: try the office first, then a known set of sibling locations, then — for
      // non-Alaska zones — a bounded sweep of the product type's own location list.
      const AK_MARINE_LOCATIONS = ["WCZ", "ALU", "AER", "AJK", "AFG"];
      const GENERIC_SWEEP_CAP = 8;

      let forecastText: string | null = null;
      let issued = "";
      let productType = "";
      let productLocation = "";
      const tried = new Set<string>();

      const tryLocation = async (loc: string): Promise<boolean> => {
        if (!loc || tried.has(loc)) return false;
        tried.add(loc);
        for (const ptype of ["CWF", "OFF"]) {
          let list: ProductsListResponse;
          try {
            list = await nwsRequest<ProductsListResponse>(
              `/products/types/${ptype}/locations/${loc}`,
              "application/ld+json",
            );
          } catch {
            continue; // a location that issues no product of this type 404s — not an error
          }
          const latest = list["@graph"]?.[0];
          if (!latest) continue;
          const prod = await nwsRequest<ProductResponse>(
            `/products/${latest.id}`,
            "application/ld+json",
          );
          const seg = extractZoneSegment(prod.productText, zone.id, zone.name);
          if (seg) {
            forecastText = seg;
            issued = prod.issuanceTime ?? latest.issuanceTime ?? "";
            productType = ptype;
            productLocation = loc;
            return true;
          }
        }
        return false;
      };

      let found = await tryLocation(office);
      if (!found && zone.id.startsWith("PK")) {
        for (const loc of AK_MARINE_LOCATIONS) {
          if (await tryLocation(loc)) { found = true; break; }
        }
      }
      if (!found) {
        // Last resort, bounded: ask each product type which locations it publishes under and
        // sweep the ones not already tried. Capped so a miss can never turn into 40+ fetches.
        for (const ptype of ["CWF", "OFF"]) {
          if (found) break;
          let locs: Record<string, string | null> = {};
          try {
            const resp = await nwsRequest<{ locations?: Record<string, string | null> }>(
              `/products/types/${ptype}/locations`,
              "application/ld+json",
            );
            locs = resp.locations ?? {};
          } catch {
            continue;
          }
          let swept = 0;
          for (const loc of Object.keys(locs)) {
            if (tried.has(loc)) continue;
            if (swept >= GENERIC_SWEEP_CAP) break;
            swept += 1;
            if (await tryLocation(loc)) { found = true; break; }
          }
        }
      }

      if (!found) {
        // Tier 3 — the zone exists but is in NO api.weather.gov text product. Verified
        // 2026-08-12: CWFWCZ carries PKZ801-856 and OFF/AFG carries only PKZ500/505/510, so
        // the 15-85 nm Arctic band (PKZ857/858/859) is published ONLY on the public
        // marine-forecast page. Scrape it — it is the same NWS text, just the other outlet.
        try {
          const html = await fetchText(
            `https://forecast.weather.gov/shmrn.php?mz=${zone.id}`,
          );
          const plain = html
            .replace(/<br\s*\/?>/gi, "\n")
            .replace(/<[^>]+>/g, " ")
            .replace(/&nbsp;/g, " ")
            .replace(/&amp;/g, "&");
          // the block runs from the zone's UGC header line to the product terminator
          const start = plain.search(new RegExp(`${zone.id}-\\d{6}-`));
          if (start >= 0) {
            const rest = plain.slice(start);
            const end = rest.indexOf("$$");
            const block = (end > 0 ? rest.slice(0, end) : rest)
              .split("\n")
              .map((l: string) => l.replace(/\s+/g, " ").trim())
              .filter(Boolean)
              .join("\n")
              .trim();
            if (block) {
              forecastText = block;
              productType = "SHMRN";
              productLocation = "forecast.weather.gov";
              found = true;
            }
          }
        } catch {
          // leave forecastText null; the message below reports what was searched
        }
      }

      const output = {
        zone: { id: zone.id, name: zone.name, office },
        product_type: productType,
        product_location: productLocation,
        issued,
        forecast_text: forecastText,
        alerts,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Marine forecast — ${zone.name} (${zone.id})`,
          `Office: ${office || "?"}${productType ? ` · ${productType}${productLocation && productLocation !== office ? `${productLocation}` : ""}` : ""} · Issued: ${issued || "?"}`,
        ];
        if (!alerts.length) {
          lines.push(`Marine alerts: none active`);
        } else {
          lines.push(`Marine alerts: ${alerts.length} ACTIVE`);
          for (const a of alerts) {
            lines.push(`- ⚠️ ${a.event} (${a.severity}) — ${a.headline ?? ""}`);
          }
        }
        lines.push("");
        lines.push(
          forecastText ??
            `(no zone text block found — searched CWF/OFF products at ${[...tried].join(", ") || "no locations"}; this zone may genuinely carry no text segment. For an inport use the land grid via nws_get_forecast.)`,
        );
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

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(
    `nws-forecast-mcp-server running on stdio (User-Agent: ${USER_AGENT})`,
  );
}

main().catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});
