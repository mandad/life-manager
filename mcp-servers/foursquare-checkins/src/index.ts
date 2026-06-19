#!/usr/bin/env node
/**
 * MCP server for Foursquare/Swarm personal check-in history.
 *
 * Wraps the Foursquare v2 user API (api.foursquare.com/v2) which is the
 * surface that exposes a user's own check-ins. Auth is the user's OAuth
 * token, supplied via FOURSQUARE_OAUTH_TOKEN.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";

const API_BASE_URL = "https://api.foursquare.com/v2";
const API_VERSION = "20240101";
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

interface FoursquareCategory {
  id?: string;
  name?: string;
  shortName?: string;
  pluralName?: string;
  primary?: boolean;
}

interface FoursquareLocation {
  address?: string;
  crossStreet?: string;
  city?: string;
  state?: string;
  postalCode?: string;
  country?: string;
  cc?: string;
  lat?: number;
  lng?: number;
  formattedAddress?: string[];
}

interface FoursquareVenue {
  id?: string;
  name?: string;
  location?: FoursquareLocation;
  categories?: FoursquareCategory[];
  url?: string;
  stats?: { checkinsCount?: number; usersCount?: number; tipCount?: number };
  rating?: number;
  ratingSignals?: number;
  price?: { tier?: number; message?: string };
  contact?: { phone?: string; formattedPhone?: string; twitter?: string };
  verified?: boolean;
  hours?: unknown;
}

interface FoursquareCheckin {
  id: string;
  createdAt: number;
  type?: string;
  shout?: string;
  timeZoneOffset?: number;
  source?: { name?: string; url?: string };
  venue?: FoursquareVenue;
  photos?: { count?: number; items?: Array<{ id?: string; prefix?: string; suffix?: string; visibility?: string }> };
  comments?: { count?: number; items?: Array<{ id?: string; text?: string; createdAt?: number; user?: { firstName?: string; lastName?: string } }> };
  likes?: { count?: number };
  score?: { total?: number; scores?: Array<{ message?: string; points?: number; icon?: string }> };
  with?: Array<{ id?: string; firstName?: string; lastName?: string }>;
  event?: { id?: string; name?: string };
}

interface FoursquareApiResponse<T> {
  meta: {
    code: number;
    requestId?: string;
    errorType?: string;
    errorDetail?: string;
  };
  response: T;
}

function getOAuthToken(): string {
  const token = process.env.FOURSQUARE_OAUTH_TOKEN;
  if (!token) {
    throw new Error(
      "FOURSQUARE_OAUTH_TOKEN environment variable is not set. " +
        "Generate a user OAuth token at https://foursquare.com/developers and export it before starting the server.",
    );
  }
  return token;
}

async function foursquareRequest<T>(
  endpoint: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const token = getOAuthToken();
  const cleanParams: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") cleanParams[k] = v;
  }

  const response = await axios.get<FoursquareApiResponse<T>>(
    `${API_BASE_URL}${endpoint}`,
    {
      params: {
        oauth_token: token,
        v: API_VERSION,
        ...cleanParams,
      },
      timeout: DEFAULT_TIMEOUT_MS,
      headers: { Accept: "application/json" },
    },
  );

  if (response.data.meta.code >= 400) {
    throw new FoursquareApiError(
      response.data.meta.code,
      response.data.meta.errorType ?? "unknown_error",
      response.data.meta.errorDetail ?? "Foursquare API returned an error",
    );
  }
  return response.data.response;
}

class FoursquareApiError extends Error {
  constructor(
    public code: number,
    public errorType: string,
    public detail: string,
  ) {
    super(`Foursquare API error ${code} (${errorType}): ${detail}`);
    this.name = "FoursquareApiError";
  }
}

function handleApiError(error: unknown): string {
  if (error instanceof FoursquareApiError) {
    if (error.errorType === "invalid_auth" || error.code === 401) {
      return (
        "Error: Foursquare rejected the OAuth token (invalid_auth). " +
        "Re-issue a user token at https://foursquare.com/developers and update FOURSQUARE_OAUTH_TOKEN."
      );
    }
    if (error.code === 403) {
      return `Error: Permission denied by Foursquare (${error.errorType}). The token lacks scope for this resource.`;
    }
    if (error.code === 404) {
      return "Error: Resource not found. Verify the check-in or venue ID is correct and belongs to this user.";
    }
    if (error.code === 429) {
      return "Error: Foursquare rate limit hit. Wait a minute and retry, or reduce request volume.";
    }
    return `Error: ${error.message}`;
  }
  if (axios.isAxiosError(error)) {
    if (error.code === "ECONNABORTED") {
      return "Error: Request to Foursquare timed out after 30s. Try again or reduce limit.";
    }
    if (error.response) {
      return `Error: HTTP ${error.response.status} from Foursquare: ${JSON.stringify(error.response.data).slice(0, 300)}`;
    }
    return `Error: Network error reaching Foursquare: ${error.message}`;
  }
  return `Error: ${error instanceof Error ? error.message : String(error)}`;
}

function unixToIso(seconds: number | undefined): string | undefined {
  if (typeof seconds !== "number" || !isFinite(seconds)) return undefined;
  return new Date(seconds * 1000).toISOString();
}

function summarizeVenue(venue: FoursquareVenue | undefined) {
  if (!venue) return undefined;
  const cats = (venue.categories ?? [])
    .map((c) => c.shortName ?? c.name)
    .filter((s): s is string => !!s);
  return {
    id: venue.id,
    name: venue.name,
    categories: cats,
    address: venue.location?.formattedAddress?.join(", ") ?? venue.location?.address,
    city: venue.location?.city,
    state: venue.location?.state,
    country: venue.location?.country,
    lat: venue.location?.lat,
    lng: venue.location?.lng,
  };
}

function summarizeCheckin(c: FoursquareCheckin) {
  return {
    id: c.id,
    created_at_unix: c.createdAt,
    created_at_iso: unixToIso(c.createdAt),
    timezone_offset_minutes: c.timeZoneOffset,
    type: c.type,
    shout: c.shout,
    source: c.source?.name,
    venue: summarizeVenue(c.venue),
    photo_count: c.photos?.count ?? 0,
    comment_count: c.comments?.count ?? 0,
    like_count: c.likes?.count ?? 0,
    score: c.score?.total,
    with_count: c.with?.length ?? 0,
    event: c.event?.name,
  };
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
    truncation_message: `Response exceeded ${CHARACTER_LIMIT} characters. Reduce 'limit', narrow the time range, or page with 'offset'.`,
  };
  const truncatedText = JSON.stringify(truncated, null, 2).slice(0, CHARACTER_LIMIT);
  return { text: truncatedText, payload: truncated };
}

const server = new McpServer({
  name: "foursquare-checkins-mcp-server",
  version: "1.0.0",
});

const ListCheckinsSchema = z
  .object({
    limit: z
      .number()
      .int()
      .min(1)
      .max(250)
      .default(50)
      .describe("Number of check-ins to return (Foursquare max 250)"),
    offset: z
      .number()
      .int()
      .min(0)
      .default(0)
      .describe("Number of check-ins to skip for pagination"),
    sort: z
      .enum(["newestfirst", "oldestfirst"])
      .default("newestfirst")
      .describe("Sort order"),
    after_timestamp: z
      .number()
      .int()
      .min(0)
      .optional()
      .describe(
        "Only return check-ins created at or after this Unix timestamp (seconds). Use to filter to a date range.",
      ),
    before_timestamp: z
      .number()
      .int()
      .min(0)
      .optional()
      .describe(
        "Only return check-ins created at or before this Unix timestamp (seconds). Use to filter to a date range.",
      ),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "foursquare_list_checkins",
  {
    title: "List my Foursquare check-ins",
    description: `List the authenticated user's Foursquare/Swarm check-in history, newest first by default.

This is the primary tool for browsing check-in activity. Each item includes the venue name, categories, location, timestamp, and counts for photos/comments/likes. Returns a flattened summary by default — use foursquare_get_checkin to fetch the full record (comments text, photos URLs, etc.) for one ID.

Args:
  - limit (number, 1-250, default 50): Page size.
  - offset (number, default 0): Skip this many check-ins for pagination.
  - sort ('newestfirst' | 'oldestfirst', default 'newestfirst').
  - after_timestamp (number, optional): Unix seconds. Only return check-ins at or after this time.
  - before_timestamp (number, optional): Unix seconds. Only return check-ins at or before this time.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "total": number,                // Total check-ins in this query window
    "count": number,                // Number returned in this page
    "offset": number,
    "has_more": boolean,
    "next_offset": number | undefined,
    "checkins": [
      {
        "id": string,                       // Use with foursquare_get_checkin
        "created_at_unix": number,
        "created_at_iso": string,           // ISO 8601 UTC
        "timezone_offset_minutes": number,
        "type": string,                     // e.g. 'checkin', 'shout'
        "shout": string | null,             // User's text on the check-in
        "source": string | null,            // Client app
        "venue": {
          "id": string,
          "name": string,
          "categories": string[],           // Short category names
          "address": string | null,
          "city": string | null,
          "state": string | null,
          "country": string | null,
          "lat": number,
          "lng": number
        } | null,
        "photo_count": number,
        "comment_count": number,
        "like_count": number,
        "score": number | null,             // Foursquare points awarded
        "with_count": number,               // Friends checked in with
        "event": string | null
      }
    ]
  }

Examples:
  - "What did I do last weekend?" -> after_timestamp/before_timestamp around the weekend.
  - "List my last 100 check-ins." -> limit=100.
  - "Walk me back through 2024 chronologically." -> sort='oldestfirst', filter by year boundaries.

Errors:
  - 'invalid_auth' -> token expired/wrong; reissue at foursquare.com/developers.
  - 429 -> rate limited; wait and retry.`,
    inputSchema: ListCheckinsSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await foursquareRequest<{
        checkins: { count: number; items: FoursquareCheckin[] };
      }>("/users/self/checkins", {
        limit: params.limit,
        offset: params.offset,
        sort: params.sort,
        afterTimestamp: params.after_timestamp,
        beforeTimestamp: params.before_timestamp,
      });

      const items = data.checkins?.items ?? [];
      const total = data.checkins?.count ?? items.length;
      const checkins = items.map(summarizeCheckin);
      const hasMore = total > params.offset + items.length;

      const output: Record<string, unknown> = {
        total,
        count: items.length,
        offset: params.offset,
        has_more: hasMore,
        next_offset: hasMore ? params.offset + items.length : undefined,
        checkins,
      };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Check-ins (showing ${items.length} of ${total})`,
          "",
        ];
        if (!items.length) lines.push("_No check-ins in this window._");
        for (const c of checkins) {
          const venue = c.venue;
          const where = venue
            ? `**${venue.name}**${venue.city ? ` (${venue.city}${venue.state ? ", " + venue.state : ""})` : ""}`
            : "_unknown venue_";
          const cats = venue?.categories?.length ? ` — ${venue.categories.join(", ")}` : "";
          lines.push(`## ${c.created_at_iso} — ${where}${cats}`);
          lines.push(`- id: \`${c.id}\``);
          if (c.shout) lines.push(`- shout: ${c.shout}`);
          if (venue?.address) lines.push(`- address: ${venue.address}`);
          if (c.photo_count || c.comment_count || c.like_count) {
            lines.push(
              `- photos: ${c.photo_count}, comments: ${c.comment_count}, likes: ${c.like_count}`,
            );
          }
          lines.push("");
        }
        if (hasMore)
          lines.push(`_More results available — call again with offset=${output.next_offset}._`);
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

const GetCheckinSchema = z
  .object({
    checkin_id: z
      .string()
      .min(1)
      .describe("Foursquare check-in ID (from foursquare_list_checkins)"),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "foursquare_get_checkin",
  {
    title: "Get a Foursquare check-in by ID",
    description: `Fetch full detail for one check-in: shout, photos (with URLs), comments (with text), the user(s) checked in with, and the full venue object.

Use this when foursquare_list_checkins gives you a candidate ID and you need richer data (e.g. the photo URL, the full comment text).

Args:
  - checkin_id (string): The check-in ID.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent): the raw Foursquare check-in object under 'checkin', plus a flattened 'summary' with the same shape as items in foursquare_list_checkins.

Photo URLs: combine photo.prefix + size + photo.suffix, e.g. \`\${prefix}original\${suffix}\` or \`\${prefix}300x300\${suffix}\`.

Errors:
  - 404 -> ID not found or not visible to this token.`,
    inputSchema: GetCheckinSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await foursquareRequest<{ checkin: FoursquareCheckin }>(
        `/checkins/${encodeURIComponent(params.checkin_id)}`,
      );
      const checkin = data.checkin;
      const summary = summarizeCheckin(checkin);
      const output = { summary, checkin };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines: string[] = [
          `# Check-in ${checkin.id}`,
          `- when: ${summary.created_at_iso}`,
          `- venue: ${summary.venue?.name ?? "_unknown_"}${summary.venue?.address ? ` — ${summary.venue.address}` : ""}`,
        ];
        if (summary.shout) lines.push(`- shout: ${summary.shout}`);
        if (summary.score != null) lines.push(`- score: ${summary.score} pts`);
        if (checkin.with?.length) {
          lines.push(
            `- with: ${checkin.with.map((u) => `${u.firstName ?? ""} ${u.lastName ?? ""}`.trim()).join(", ")}`,
          );
        }
        if (checkin.photos?.items?.length) {
          lines.push("", "## Photos");
          for (const p of checkin.photos.items) {
            if (p.prefix && p.suffix) lines.push(`- ${p.prefix}original${p.suffix}`);
          }
        }
        if (checkin.comments?.items?.length) {
          lines.push("", "## Comments");
          for (const c of checkin.comments.items) {
            const who = `${c.user?.firstName ?? ""} ${c.user?.lastName ?? ""}`.trim() || "someone";
            lines.push(`- **${who}** (${unixToIso(c.createdAt) ?? "?"}): ${c.text ?? ""}`);
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

const GetSelfSchema = z
  .object({ response_format: ResponseFormatSchema })
  .strict();

server.registerTool(
  "foursquare_get_self",
  {
    title: "Get my Foursquare profile",
    description: `Return the authenticated user's profile: name, home city, lifetime check-in count, badges count, and other top-level stats. Useful for context ("how many check-ins do I have?", "when did I sign up?").

Args:
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent): the full Foursquare user object under 'user', plus a 'summary' with id, name, home_city, total_checkins, total_friends, total_tips, created_at_iso.`,
    inputSchema: GetSelfSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await foursquareRequest<{
        user: {
          id?: string;
          firstName?: string;
          lastName?: string;
          homeCity?: string;
          gender?: string;
          createdAt?: number;
          checkins?: { count?: number };
          friends?: { count?: number };
          tips?: { count?: number };
          photos?: { count?: number };
          badges?: { count?: number };
          contact?: { email?: string };
        };
      }>("/users/self");
      const u = data.user;
      const summary = {
        id: u.id,
        name: `${u.firstName ?? ""} ${u.lastName ?? ""}`.trim(),
        home_city: u.homeCity,
        total_checkins: u.checkins?.count,
        total_friends: u.friends?.count,
        total_tips: u.tips?.count,
        total_photos: u.photos?.count,
        total_badges: u.badges?.count,
        created_at_iso: unixToIso(u.createdAt),
      };
      const output = { summary, user: u };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        text = [
          `# ${summary.name || "Foursquare user"}`,
          `- id: ${summary.id}`,
          `- home: ${summary.home_city ?? "_unset_"}`,
          `- joined: ${summary.created_at_iso ?? "_unknown_"}`,
          `- check-ins: ${summary.total_checkins ?? "?"}`,
          `- tips: ${summary.total_tips ?? "?"}`,
          `- photos: ${summary.total_photos ?? "?"}`,
          `- badges: ${summary.total_badges ?? "?"}`,
        ].join("\n");
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

const GetVenueSchema = z
  .object({
    venue_id: z.string().min(1).describe("Foursquare venue ID"),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "foursquare_get_venue",
  {
    title: "Get a Foursquare venue by ID",
    description: `Fetch full detail for a venue: name, categories, address, hours, rating, contact, stats. Use the venue.id surfaced by foursquare_list_checkins or foursquare_search_venues.

Args:
  - venue_id (string): Foursquare venue ID.
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent): { 'summary': flattened venue, 'venue': raw Foursquare venue object }.`,
    inputSchema: GetVenueSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      const data = await foursquareRequest<{ venue: FoursquareVenue }>(
        `/venues/${encodeURIComponent(params.venue_id)}`,
      );
      const venue = data.venue;
      const summary = summarizeVenue(venue);
      const output = { summary, venue };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines = [
          `# ${venue.name ?? "Venue"} (${venue.id ?? "?"})`,
          summary?.address ? `- address: ${summary.address}` : "",
          summary?.categories?.length ? `- categories: ${summary.categories.join(", ")}` : "",
          venue.rating != null ? `- rating: ${venue.rating} (${venue.ratingSignals ?? 0} signals)` : "",
          venue.stats?.checkinsCount != null
            ? `- check-ins: ${venue.stats.checkinsCount}, unique users: ${venue.stats.usersCount ?? "?"}`
            : "",
          venue.contact?.formattedPhone ? `- phone: ${venue.contact.formattedPhone}` : "",
          venue.url ? `- url: ${venue.url}` : "",
        ]
          .filter(Boolean)
          .join("\n");
        text = lines;
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

const SearchVenuesSchema = z
  .object({
    ll: z
      .string()
      .regex(
        /^-?\d+(\.\d+)?,-?\d+(\.\d+)?$/,
        "Must be 'lat,lng' decimal numbers, e.g. '40.7128,-74.0060'",
      )
      .optional()
      .describe("Latitude,longitude string. Provide either 'll' or 'near'."),
    near: z
      .string()
      .min(1)
      .max(200)
      .optional()
      .describe(
        "Free-text place name to anchor the search, e.g. 'Brooklyn, NY'. Provide either 'll' or 'near'.",
      ),
    query: z
      .string()
      .max(200)
      .optional()
      .describe("Search term, e.g. 'coffee', 'pizza', a venue name"),
    radius: z
      .number()
      .int()
      .min(1)
      .max(100000)
      .optional()
      .describe("Search radius in meters (max 100000). Foursquare picks a default if omitted."),
    limit: z
      .number()
      .int()
      .min(1)
      .max(50)
      .default(20)
      .describe("Max venues to return (1-50)"),
    response_format: ResponseFormatSchema,
  })
  .strict();

server.registerTool(
  "foursquare_search_venues",
  {
    title: "Search Foursquare venues",
    description: `Search the global Foursquare venue database near a location. Useful for analysis tasks like "have I checked in here?" — get a candidate venue ID, then cross-reference against foursquare_list_checkins.

Args:
  - ll (string, optional): 'lat,lng' (e.g. '40.7128,-74.0060').
  - near (string, optional): Free-text place ('Brooklyn, NY'). Provide either 'll' or 'near'.
  - query (string, optional): Search term ('coffee', a venue name).
  - radius (number, optional): Meters. Default chosen by Foursquare if omitted.
  - limit (number, 1-50, default 20).
  - response_format ('markdown' | 'json', default 'markdown').

Returns (structuredContent):
  {
    "count": number,
    "venues": [{ id, name, categories, address, city, state, country, lat, lng }]
  }

Note: Uses Foursquare v2 venue search. For deep venue analytics use the v3 Places API separately.`,
    inputSchema: SearchVenuesSchema.shape,
    annotations: {
      readOnlyHint: true,
      destructiveHint: false,
      idempotentHint: true,
      openWorldHint: true,
    },
  },
  async (params) => {
    try {
      if (!params.ll && !params.near) {
        return {
          content: [
            {
              type: "text",
              text: "Error: provide either 'll' (lat,lng like '40.7128,-74.0060') or 'near' (place name like 'Brooklyn, NY').",
            },
          ],
          isError: true,
        };
      }
      const data = await foursquareRequest<{ venues: FoursquareVenue[] }>(
        "/venues/search",
        {
          ll: params.ll,
          near: params.near,
          query: params.query,
          radius: params.radius,
          limit: params.limit,
          intent: "browse",
        },
      );
      const venues = (data.venues ?? []).map(summarizeVenue);
      const output = { count: venues.length, venues };

      let text: string;
      if (params.response_format === ResponseFormat.MARKDOWN) {
        const lines = [
          `# Venue search (${venues.length} results)`,
          params.query ? `Query: \`${params.query}\`` : "",
          params.ll ? `Near: ${params.ll}` : params.near ? `Near: ${params.near}` : "",
          "",
        ];
        for (const v of venues) {
          lines.push(
            `- **${v?.name ?? "?"}** (${v?.id ?? "?"})${v?.categories?.length ? ` — ${v.categories.join(", ")}` : ""}${v?.address ? ` — ${v.address}` : ""}`,
          );
        }
        text = lines.filter(Boolean).join("\n");
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
  // Validate token presence at startup so misconfiguration fails fast.
  try {
    getOAuthToken();
  } catch (e) {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  }

  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("foursquare-checkins-mcp-server running on stdio");
}

main().catch((error) => {
  console.error("Server error:", error);
  process.exit(1);
});
