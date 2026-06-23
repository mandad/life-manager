#!/usr/bin/env node
/**
 * MCP server for read-only Microsoft OneNote via the Graph API (personal account).
 *
 * Phase 1 (this file): notebooks / sections / pages listing, typed-text extraction
 * from a page, and full-text search. Phase 2 (TODO): onenote_get_page_image renders
 * a page's ink (InkML) to a PNG so Claude can OCR handwriting.
 *
 * Auth: OAuth 2.0 device-code, no client secret (see auth.ts + SETUP.md).
 * Scope Notes.Read — cannot modify or delete notes.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";
import { getAccessToken } from "./auth.js";
import { extractInkml, parseTraces, renderStrokesToPng } from "./ink.js";

const GRAPH = "https://graph.microsoft.com/v1.0";
const CHARACTER_LIMIT = 25000;
const TIMEOUT_MS = 30000;
const INK_TIMEOUT_MS = 90000; // ink multipart can be multi-MB

async function graphGet(url: string, opts: { raw?: boolean } = {}): Promise<any> {
  const token = await getAccessToken();
  const res = await axios.get(url.startsWith("http") ? url : `${GRAPH}${url}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: TIMEOUT_MS,
    responseType: opts.raw ? "text" : "json",
    transformResponse: opts.raw ? [(d) => d] : undefined,
  });
  return res.data;
}

/** Raw GET returning body + content-type header (for multipart ink). */
async function graphGetRaw(url: string): Promise<{ body: string; contentType: string }> {
  const token = await getAccessToken();
  const res = await axios.get(`${GRAPH}${url}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: INK_TIMEOUT_MS,
    responseType: "text",
    transformResponse: [(d) => d],
    maxContentLength: 64 * 1024 * 1024,
  });
  return { body: res.data, contentType: String(res.headers["content-type"] ?? "") };
}

function graphError(e: unknown): string {
  const ax = e as AxiosError<any>;
  if (ax.response) {
    const code = ax.response.status;
    const msg = ax.response.data?.error?.message ?? ax.response.statusText;
    if (code === 401)
      return `401 Unauthorized — token expired or revoked. Re-run \`npm run auth\`. (${msg})`;
    return `Graph error ${code}: ${msg}`;
  }
  return `Request failed: ${(e as Error).message}`;
}

/** Strip OneNote page HTML down to readable plain text. */
function htmlToText(html: string): string {
  return html
    .replace(/<\s*(script|style|head)[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi, "")
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\s*\/\s*(p|div|li|h[1-6]|tr|table)\s*>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&quot;/gi, '"')
    .replace(/\r/g, "")
    .replace(/\t+/g, " ")
    .replace(/ {2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

const fmt = z
  .enum(["markdown", "json"])
  .default("markdown")
  .describe("Output format");

function out(markdown: string, json: unknown, format: "markdown" | "json") {
  const text = format === "json" ? JSON.stringify(json, null, 2) : markdown;
  return {
    content: [
      {
        type: "text" as const,
        text:
          text.length > CHARACTER_LIMIT
            ? text.slice(0, CHARACTER_LIMIT) + "\n…[truncated]"
            : text,
      },
    ],
  };
}

const server = new McpServer({ name: "onenote", version: "0.1.0" });

server.tool(
  "onenote_list_notebooks",
  "List the user's OneNote notebooks (id, name, last modified), newest first.",
  { response_format: fmt },
  async ({ response_format }) => {
    try {
      const data = await graphGet(
        "/me/onenote/notebooks?$select=id,displayName,lastModifiedDateTime&$orderby=lastModifiedDateTime desc",
      );
      const items = (data.value ?? []).map((n: any) => ({
        id: n.id,
        name: n.displayName,
        last_modified: n.lastModifiedDateTime,
      }));
      const md =
        items.map((n: any) => `- **${n.name}** — ${n.last_modified}\n  \`${n.id}\``).join("\n") ||
        "(no notebooks)";
      return out(md, items, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_list_sections",
  "List sections in a notebook (by notebook id).",
  {
    notebook_id: z.string().describe("Notebook id from onenote_list_notebooks"),
    response_format: fmt,
  },
  async ({ notebook_id, response_format }) => {
    try {
      const data = await graphGet(
        `/me/onenote/notebooks/${encodeURIComponent(notebook_id)}/sections?$select=id,displayName,lastModifiedDateTime&$orderby=lastModifiedDateTime desc`,
      );
      const items = (data.value ?? []).map((s: any) => ({
        id: s.id,
        name: s.displayName,
        last_modified: s.lastModifiedDateTime,
      }));
      const md =
        items.map((s: any) => `- **${s.name}** — ${s.last_modified}\n  \`${s.id}\``).join("\n") ||
        "(no sections)";
      return out(md, items, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_browse_notebook",
  "Walk a notebook's full tree — direct sections AND nested section groups (e.g. 'FA CO') with their sections, recursively. Use this to find a section id when the section lives inside a section group (onenote_list_sections only returns a notebook's top-level sections).",
  {
    notebook_id: z.string().describe("Notebook id from onenote_list_notebooks"),
    response_format: fmt,
  },
  async ({ notebook_id, response_format }) => {
    try {
      const pick = (v: any[]) =>
        (v ?? []).map((x: any) => ({ id: x.id, name: x.displayName }));

      const walkGroup = async (groupId: string, depth: number): Promise<any> => {
        if (depth > 5) return { sections: [], groups: [] };
        const [secs, groups] = await Promise.all([
          graphGet(`/me/onenote/sectionGroups/${encodeURIComponent(groupId)}/sections?$select=id,displayName`),
          graphGet(`/me/onenote/sectionGroups/${encodeURIComponent(groupId)}/sectionGroups?$select=id,displayName`),
        ]);
        const childGroups = [];
        for (const grp of pick(groups.value)) {
          childGroups.push({ ...grp, ...(await walkGroup(grp.id, depth + 1)) });
        }
        return { sections: pick(secs.value), groups: childGroups };
      };

      const [directSecs, topGroups] = await Promise.all([
        graphGet(`/me/onenote/notebooks/${encodeURIComponent(notebook_id)}/sections?$select=id,displayName`),
        graphGet(`/me/onenote/notebooks/${encodeURIComponent(notebook_id)}/sectionGroups?$select=id,displayName`),
      ]);
      const groups = [];
      for (const grp of pick(topGroups.value)) {
        groups.push({ ...grp, ...(await walkGroup(grp.id, 0)) });
      }
      const tree = { notebook_id, sections: pick(directSecs.value), groups };

      const render = (node: any, indent: string): string => {
        const lines: string[] = [];
        for (const s of node.sections ?? [])
          lines.push(`${indent}📄 ${s.name}  \`${s.id}\``);
        for (const g of node.groups ?? []) {
          lines.push(`${indent}📁 ${g.name}`);
          lines.push(render(g, indent + "  "));
        }
        return lines.filter(Boolean).join("\n");
      };
      return out(render(tree, ""), tree, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_list_pages",
  "List pages in a section (by section id), newest first. Optional `since` ISO date filters by last-modified.",
  {
    section_id: z.string().describe("Section id from onenote_list_sections"),
    since: z
      .string()
      .optional()
      .describe("ISO 8601 date/time; only pages modified at or after this"),
    top: z.number().int().min(1).max(100).default(50).describe("Max pages to return"),
    response_format: fmt,
  },
  async ({ section_id, since, top, response_format }) => {
    try {
      let url = `/me/onenote/sections/${encodeURIComponent(section_id)}/pages?$select=id,title,createdDateTime,lastModifiedDateTime&$top=${top}&$orderby=lastModifiedDateTime desc`;
      if (since) url += `&$filter=lastModifiedDateTime ge ${encodeURIComponent(since)}`;
      const data = await graphGet(url);
      const items = (data.value ?? []).map((p: any) => ({
        id: p.id,
        title: p.title,
        created: p.createdDateTime,
        modified: p.lastModifiedDateTime,
      }));
      const md =
        items
          .map((p: any) => `- **${p.title || "(untitled)"}** — ${p.modified}\n  \`${p.id}\``)
          .join("\n") || "(no pages)";
      return out(md, items, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_get_page",
  "Get a page's typed text (extracted from its HTML). Sets has_ink=true when the page also contains handwritten ink — use onenote_get_page_image for those to OCR the handwriting.",
  {
    page_id: z.string().describe("Page id from onenote_list_pages"),
    response_format: fmt,
  },
  async ({ page_id, response_format }) => {
    try {
      // includeinkML=true returns multipart (HTML + InkML) when ink is present;
      // for phase 1 we read the HTML body and flag ink via a marker check.
      const html = (await graphGet(
        `/me/onenote/pages/${encodeURIComponent(page_id)}/content?includeIDs=true`,
        { raw: true },
      )) as string;
      const text = htmlToText(html);
      // OneNote injects data-render-* / ink fallback markers when strokes exist.
      const has_ink = /data-render-(original-src|src)|application\/inkml|<ink/i.test(html);
      const payload = { page_id, has_ink, text };
      const md =
        `**Page** \`${page_id}\`${has_ink ? "  ⚠️ has ink — run onenote_get_page_image to OCR handwriting" : ""}\n\n${text || "(no typed text)"}`;
      return out(md, payload, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_search",
  "Full-text search across the user's OneNote pages.",
  {
    query: z.string().describe("Search text"),
    top: z.number().int().min(1).max(100).default(25).describe("Max results"),
    response_format: fmt,
  },
  async ({ query, top, response_format }) => {
    try {
      const data = await graphGet(
        `/me/onenote/pages?$search=${encodeURIComponent(query)}&$select=id,title,lastModifiedDateTime&$top=${top}`,
      );
      const items = (data.value ?? []).map((p: any) => ({
        id: p.id,
        title: p.title,
        modified: p.lastModifiedDateTime,
      }));
      const md =
        items
          .map((p: any) => `- **${p.title || "(untitled)"}** — ${p.modified}\n  \`${p.id}\``)
          .join("\n") || "(no matches)";
      return out(md, items, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

// Render a page's handwritten ink to PNG tile(s) so the caller (Claude) can OCR it.
// Read-only: fetches includeinkML, never modifies the page.
server.tool(
  "onenote_get_page_image",
  "Render a page's handwritten INK to PNG image tile(s) for OCR. Returns one or more images (tall pages are split into vertical tiles with overlap). OCR the images yourself, using surrounding context — this is the way to read handwritten pages (e.g. the JO 1:1 notes). Read-only; does not alter the page.",
  {
    page_id: z.string().describe("Page id from onenote_list_pages"),
    target_width: z.number().int().min(600).max(2400).default(1500).describe("px width the ink maps to"),
  },
  async ({ page_id, target_width }) => {
    try {
      const { body, contentType } = await graphGetRaw(
        `/me/onenote/pages/${encodeURIComponent(page_id)}/content?includeinkML=true`,
      );
      const inkmls = extractInkml(body, contentType);
      const strokes = inkmls.flatMap((x) => parseTraces(x));
      if (!strokes.length) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No ink found on this page (likely typed-only). Use onenote_get_page for its text.",
            },
          ],
        };
      }
      const { tiles, width, height, strokeCount } = renderStrokesToPng(strokes, {
        targetWidth: target_width,
      });
      const content: Array<
        { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
      > = [
        {
          type: "text" as const,
          text: `Rendered ${strokeCount} ink strokes → ${tiles.length} tile(s), ${width}×${height}px. OCR the image(s) below top-to-bottom; tiles overlap slightly. Use context (it's a CO's JO/command 1:1 notes) to disambiguate unclear handwriting; flag anything you're unsure of rather than guessing.`,
        },
      ];
      for (const t of tiles) {
        content.push({ type: "image" as const, data: t.toString("base64"), mimeType: "image/png" });
      }
      return { content };
    } catch (e) {
      return {
        content: [{ type: "text" as const, text: graphError(e) }],
      };
    }
  },
);

async function main() {
  console.error("onenote MCP server starting (stdio)…");
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});
