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
import { extractImages, prepareImage } from "./images.js";

const GRAPH = "https://graph.microsoft.com/v1.0";
const CHARACTER_LIMIT = 25000;
const TIMEOUT_MS = 30000;
const INK_TIMEOUT_MS = 90000; // ink multipart can be multi-MB
const SEARCH_CONCURRENCY = 6; // OneNote throttles hard; keep request fan-out modest

// OneNote/Graph throttles aggressively (429), often WITHOUT a Retry-After header. Retry
// retryable statuses honoring Retry-After when present, else exponential backoff + jitter.
const RETRYABLE = new Set([429, 503, 504]);

async function axiosGetRetry(url: string, cfg: Record<string, unknown>, maxRetries = 5): Promise<any> {
  for (let attempt = 0; ; attempt++) {
    try {
      return await axios.get(url, cfg);
    } catch (e) {
      const ax = e as AxiosError;
      const status = ax.response?.status;
      if (!status || !RETRYABLE.has(status) || attempt >= maxRetries) throw e;
      const ra = Number(ax.response?.headers?.["retry-after"]);
      const waitMs =
        Number.isFinite(ra) && ra > 0
          ? ra * 1000
          : Math.min(32000, 1000 * 2 ** attempt) + Math.floor(Math.random() * 750);
      await new Promise((r) => setTimeout(r, waitMs));
    }
  }
}

async function graphGet(url: string, opts: { raw?: boolean } = {}): Promise<any> {
  const token = await getAccessToken();
  const res = await axiosGetRetry(url.startsWith("http") ? url : `${GRAPH}${url}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: TIMEOUT_MS,
    responseType: opts.raw ? "text" : "json",
    transformResponse: opts.raw ? [(d: any) => d] : undefined,
  });
  return res.data;
}

/** Raw GET returning body + content-type header (for multipart ink). */
async function graphGetRaw(url: string): Promise<{ body: string; contentType: string }> {
  const token = await getAccessToken();
  const res = await axiosGetRetry(`${GRAPH}${url}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: INK_TIMEOUT_MS,
    responseType: "text",
    transformResponse: [(d: any) => d],
    maxContentLength: 64 * 1024 * 1024,
  });
  return { body: res.data, contentType: String(res.headers["content-type"] ?? "") };
}

/** Raw GET of a binary resource (image $value) → Buffer. */
async function graphGetBinary(url: string): Promise<Buffer> {
  const token = await getAccessToken();
  const res = await axiosGetRetry(url.startsWith("http") ? url : `${GRAPH}${url}`, {
    headers: { Authorization: `Bearer ${token}` },
    timeout: INK_TIMEOUT_MS,
    responseType: "arraybuffer",
    maxContentLength: 64 * 1024 * 1024,
  });
  return Buffer.from(res.data);
}

/** Run `fn` over items with bounded concurrency, preserving order. */
async function mapPool<T, R>(items: T[], limit: number, fn: (item: T, idx: number) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length || 1) }, async () => {
    while (true) {
      const idx = next++;
      if (idx >= items.length) break;
      results[idx] = await fn(items[idx], idx);
    }
  });
  await Promise.all(workers);
  return results;
}

/** Every section across all notebooks, including nested section groups, with a readable path.
 * BFS by level with bounded concurrency — a sequential recursive walk is the dominant cost on
 * accounts with many section groups (observed ~70s of a 130s search). */
async function allSections(): Promise<Array<{ id: string; name: string; path: string }>> {
  const notebooks = (await graphGet("/me/onenote/notebooks?$select=id,displayName")).value ?? [];
  const sections: Array<{ id: string; name: string; path: string }> = [];
  type Container = { kind: "notebooks" | "sectionGroups"; id: string; path: string };
  let frontier: Container[] = notebooks.map((nb: any) => ({
    kind: "notebooks" as const,
    id: nb.id,
    path: nb.displayName,
  }));
  for (let depth = 0; frontier.length && depth < 8; depth++) {
    const expanded = await mapPool(frontier, SEARCH_CONCURRENCY, async (c) => {
      const [secs, groups] = await Promise.all([
        graphGet(`/me/onenote/${c.kind}/${encodeURIComponent(c.id)}/sections?$select=id,displayName`).catch(() => ({ value: [] })),
        graphGet(`/me/onenote/${c.kind}/${encodeURIComponent(c.id)}/sectionGroups?$select=id,displayName`).catch(() => ({ value: [] })),
      ]);
      return {
        secs: (secs.value ?? []).map((s: any) => ({ id: s.id, name: s.displayName, path: `${c.path} / ${s.displayName}` })),
        next: (groups.value ?? []).map((g: any) => ({ kind: "sectionGroups" as const, id: g.id, path: `${c.path} / ${g.displayName}` })),
      };
    });
    frontier = [];
    for (const e of expanded) {
      sections.push(...e.secs);
      frontier.push(...e.next);
    }
  }
  return sections;
}

/** All pages in a section (id, title, modified), following nextLink so sections with >100 pages
 * aren't truncated. Bounded hops as a runaway guard. */
async function listSectionPages(sectionId: string): Promise<Array<{ id: string; title: string; modified: string }>> {
  const acc: Array<{ id: string; title: string; modified: string }> = [];
  let url: string | null =
    `/me/onenote/sections/${encodeURIComponent(sectionId)}/pages?$select=id,title,lastModifiedDateTime&$top=100&$orderby=lastModifiedDateTime desc`;
  for (let hop = 0; url && hop < 6; hop++) {
    const data: any = await graphGet(url);
    for (const p of data.value ?? []) acc.push({ id: p.id, title: p.title ?? "", modified: p.lastModifiedDateTime });
    url = data["@odata.nextLink"] ?? null;
  }
  return acc;
}

function graphError(e: unknown): string {
  const ax = e as AxiosError<any>;
  if (ax.response) {
    const code = ax.response.status;
    const msg = ax.response.data?.error?.message ?? ax.response.statusText;
    if (code === 401)
      return `401 Unauthorized — token expired or revoked. Re-run \`npm run auth\`. (${msg})`;
    if (code === 429)
      return `429 throttled — OneNote rate-limits hard and the backoff retries were exhausted. Wait a few minutes, and scope searches with \`notebook\` / use title-only (deep=false) to cut request volume. (${msg})`;
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
  "Get a page's typed text (extracted from its HTML). Sets has_ink=true when the page also contains handwritten ink (use onenote_get_page_image to OCR it) and has_images=true when the page embeds raster images / screenshots whose text is NOT in the HTML (use onenote_get_page_screenshots to OCR those).",
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
      // Embedded raster images (screenshots / pasted pictures). Their OCR text never
      // appears in the HTML, so flag them for the screenshot OCR tool.
      const images = extractImages(html);
      const has_images = images.length > 0;
      const payload = {
        page_id,
        has_ink,
        has_images,
        image_count: images.length,
        images: images.map((i) => ({ ord: i.ord, mime: i.mime, width: i.width, height: i.height })),
        text,
      };
      const flags =
        (has_ink ? "  ⚠️ has ink — run onenote_get_page_image to OCR handwriting" : "") +
        (has_images
          ? `  🖼️ ${images.length} embedded image(s) — run onenote_get_page_screenshots to OCR their text`
          : "");
      const md = `**Page** \`${page_id}\`${flags}\n\n${text || "(no typed text)"}`;
      return out(md, payload, response_format);
    } catch (e) {
      return out(graphError(e), { error: graphError(e) }, response_format);
    }
  },
);

server.tool(
  "onenote_search",
  "Search the user's OneNote pages. Graph's native $search is unsupported on this (consumer) account, so this enumerates pages per-section and matches client-side: ALL whitespace-separated terms must be present (case-insensitive AND). Matches page titles by default; pass deep=true to also scan page body text. The account is large (~2300 pages / ~190 sections) so an unscoped search takes a while — pass `notebook` to scope to one notebook/section-group by name (much faster, and the only practical way to run a thorough deep search). Results are newest-first with the notebook/section path. NOTE: text inside screenshots/images is NOT matched (it isn't in the page HTML) — open a hit with onenote_get_page_screenshots to read those.",
  {
    query: z.string().describe("Search text; all words must match (AND)"),
    notebook: z
      .string()
      .optional()
      .describe("Scope to notebooks/section-groups whose path contains this text (case-insensitive), e.g. 'NOAA Work' or 'FA CO'"),
    deep: z
      .boolean()
      .default(false)
      .describe("Also scan page body text, not just titles (slower — fetches each page's content). Scope with `notebook` first."),
    max_scan: z
      .number()
      .int()
      .min(10)
      .max(2500)
      .default(400)
      .describe("deep mode only: cap on how many (newest) pages to fetch + scan"),
    top: z.number().int().min(1).max(100).default(25).describe("Max results to return"),
    response_format: fmt,
  },
  async ({ query, notebook, deep, max_scan, top, response_format }) => {
    try {
      const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
      if (!terms.length) return out("(empty query)", { error: "empty query" }, response_format);
      const titleHit = (t: string) => {
        const lt = t.toLowerCase();
        return terms.every((w) => lt.includes(w));
      };

      let sections = await allSections();
      const totalSections = sections.length;
      if (notebook) {
        const nl = notebook.toLowerCase();
        sections = sections.filter((s) => s.path.toLowerCase().includes(nl));
      }
      if (!sections.length) {
        return out(
          `No sections match notebook scope \`${notebook}\` (of ${totalSections} sections).`,
          { error: "no sections in scope", total_sections: totalSections },
          response_format,
        );
      }

      // Per-section page listing (the global /me/onenote/pages endpoint 400s on accounts
      // with many sections, and $search is rejected outright — so enumerate + filter here).
      const perSection = await mapPool(sections, SEARCH_CONCURRENCY, async (sec) => {
        try {
          return (await listSectionPages(sec.id)).map((p) => ({ ...p, section: sec.path }));
        } catch {
          return [];
        }
      });
      const pages = perSection.flat().sort((a, b) => (a.modified < b.modified ? 1 : -1));

      let matches: Array<any>;
      let scanned = 0;
      let capped = false;
      if (!deep) {
        matches = pages.filter((p) => titleHit(p.title)).map((p) => ({ ...p, matched: "title" }));
      } else {
        const scanList = pages.slice(0, max_scan);
        capped = pages.length > max_scan;
        scanned = scanList.length;
        const scored = await mapPool(scanList, SEARCH_CONCURRENCY, async (p) => {
          if (titleHit(p.title)) return { ...p, matched: "title" };
          try {
            const html = (await graphGet(
              `/me/onenote/pages/${encodeURIComponent(p.id)}/content?includeIDs=true`,
              { raw: true },
            )) as string;
            const body = htmlToText(html).toLowerCase();
            if (terms.every((w) => body.includes(w))) return { ...p, matched: "body" };
          } catch {
            /* skip unreadable page */
          }
          return null;
        });
        matches = scored.filter(Boolean) as any[];
      }

      const results = matches.slice(0, top);
      const scope = notebook ? ` in \`${notebook}\` (${sections.length}/${totalSections} sections)` : ` (${sections.length} sections)`;
      const header =
        `**Search** \`${query}\`${scope} — ${matches.length} match(es) across ${pages.length} pages` +
        (deep
          ? ` (deep-scanned ${scanned}${capped ? `, capped at ${max_scan} — raise max_scan or narrow \`notebook\` to reach older pages` : ""})`
          : ` (title-only — pass deep=true to search body text)`);
      const body =
        results
          .map(
            (p) =>
              `- **${p.title || "(untitled)"}** — ${p.modified}  _[${p.matched}]_\n  ${p.section}\n  \`${p.id}\``,
          )
          .join("\n") || "(no matches)";
      const json = {
        query,
        notebook: notebook ?? null,
        deep,
        sections_searched: sections.length,
        total_sections: totalSections,
        total_pages: pages.length,
        scanned: deep ? scanned : pages.length,
        capped,
        match_count: matches.length,
        results,
      };
      return out(`${header}\n\n${body}`, json, response_format);
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

// Fetch a page's embedded raster images (screenshots / pasted pictures) as image blocks for OCR.
// OneNote OCRs images server-side but never exposes that text via the page HTML or $search, so
// vision-OCR of the fetched binary is the only way to read a screenshot's text. Read-only.
server.tool(
  "onenote_get_page_screenshots",
  "Fetch a page's embedded raster images (screenshots, pasted pictures, diagrams) as PNG image block(s) so you can OCR/read them. OneNote stores these as binary resources whose text is NOT in the page HTML and NOT searchable — this is the only way to read a screenshot's contents. Distinct from onenote_get_page_image, which renders handwritten INK. Big images are downscaled to keep them legible. Read-only; does not alter the page.",
  {
    page_id: z.string().describe("Page id from onenote_list_pages"),
    max_images: z.number().int().min(1).max(25).default(12).describe("Max images to return (document order)"),
    max_width: z
      .number()
      .int()
      .min(600)
      .max(2400)
      .default(1568)
      .describe("Downscale images wider than this many px (keeps OCR legible, payload small)"),
  },
  async ({ page_id, max_images, max_width }) => {
    try {
      const html = (await graphGet(
        `/me/onenote/pages/${encodeURIComponent(page_id)}/content?includeIDs=true`,
        { raw: true },
      )) as string;
      const images = extractImages(html);
      if (!images.length) {
        return {
          content: [
            {
              type: "text" as const,
              text: "No embedded images on this page. Use onenote_get_page for its typed text, or onenote_get_page_image if it has handwritten ink.",
            },
          ],
        };
      }
      const slice = images.slice(0, max_images);
      const content: Array<
        { type: "text"; text: string } | { type: "image"; data: string; mimeType: string }
      > = [
        {
          type: "text" as const,
          text:
            `Page has ${images.length} embedded image(s); returning ${slice.length} in document order. ` +
            `OCR/read each one — their text is invisible to onenote_get_page. Transcribe verbatim where it's text (notes, slides, tables), or describe the content where it's a diagram/photo; flag anything illegible rather than guessing.`,
        },
      ];
      const notes: string[] = [];
      for (const im of slice) {
        try {
          const raw = await graphGetBinary(im.url);
          const prepared = await prepareImage(raw, im.mime, max_width);
          content.push({
            type: "image" as const,
            data: prepared.buf.toString("base64"),
            mimeType: prepared.mime,
          });
          notes.push(
            `#${im.ord + 1}: ${prepared.width || "?"}×${prepared.height || "?"}px${prepared.downscaled ? " (downscaled)" : ""}`,
          );
        } catch (e) {
          notes.push(`#${im.ord + 1}: fetch failed — ${graphError(e)}`);
        }
      }
      content.push({ type: "text" as const, text: "Images: " + notes.join("; ") });
      return { content };
    } catch (e) {
      return { content: [{ type: "text" as const, text: graphError(e) }] };
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
