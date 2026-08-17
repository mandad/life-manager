#!/usr/bin/env node
/**
 * Google Tasks MCP server — READ-ONLY (automation #34).
 *
 * WHY: the vault has never been able to see the user's actual work task list. It sees command
 * work only through what he jots, what OneNote captures, and what telemetry implies. He manages
 * work tasks in Google Tasks on his NOAA account, flagging items out of email into it. His
 * constraint, stated twice: *"I don't want to give full access to my inbox, but I do want to
 * monitor tasks."*
 *
 * That constraint is satisfiable by construction, not by promise: `tasks.readonly` is a
 * SEPARATE OAuth scope from Gmail. Granting it yields the task list and **no mailbox access at
 * all**, even though the items got there as email flags. This server requests nothing else and
 * implements no write path — there is no code here capable of creating, editing, completing or
 * deleting a task.
 *
 * TOOLS
 *   google_tasks_list_tasklists  — the account's task lists
 *   google_tasks_list_tasks      — tasks in a list, with due/updated filters
 *   google_tasks_search          — client-side search across lists (the API has no search)
 *
 * Auth + token cache live in auth.ts. One-time: `npm run auth`.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import axios, { AxiosError } from "axios";
import { getAccessToken, SCOPE } from "./auth.js";

const API = "https://tasks.googleapis.com/tasks/v1";
const MAX_CHARS = 40_000;

interface TaskList {
  id: string;
  title: string;
  updated?: string;
}
interface Task {
  id: string;
  title?: string;
  notes?: string;
  status?: string; // needsAction | completed
  due?: string; // RFC3339, date-only semantics
  completed?: string;
  updated?: string;
  parent?: string;
  position?: string;
  hidden?: boolean;
  deleted?: boolean;
  webViewLink?: string;
}

async function api<T>(path: string, params?: Record<string, string | number | boolean>): Promise<T> {
  const token = await getAccessToken();
  try {
    const { data } = await axios.get<T>(`${API}${path}`, {
      headers: { Authorization: `Bearer ${token}` },
      params,
      timeout: 30_000,
    });
    return data;
  } catch (e) {
    const err = e as AxiosError<{ error?: { message?: string; status?: string } }>;
    if (err.response) {
      const detail = err.response.data?.error?.message ?? err.response.statusText;
      if (err.response.status === 403)
        throw new Error(
          `Google Tasks API returned 403: ${detail}. Common causes: the Tasks API is not enabled ` +
            `on the Cloud project, or your Workspace admin has not allowlisted this OAuth client.`,
        );
      if (err.response.status === 401)
        throw new Error(
          `Google Tasks API returned 401: ${detail}. The token may have been revoked — re-run ` +
            `\`npm run auth\` in mcp-servers/google-tasks.`,
        );
      throw new Error(`Google Tasks API ${err.response.status}: ${detail}`);
    }
    throw e;
  }
}

/** Follow nextPageToken to completion, with a hard cap so a runaway list can't hang /daily. */
async function pageAll<T>(
  path: string,
  key: "items",
  params: Record<string, string | number | boolean>,
  cap = 500,
): Promise<T[]> {
  const out: T[] = [];
  let pageToken: string | undefined;
  for (let i = 0; i < 20 && out.length < cap; i++) {
    const data = await api<{ items?: T[]; nextPageToken?: string }>(path, {
      ...params,
      maxResults: 100,
      ...(pageToken ? { pageToken } : {}),
    });
    out.push(...(data[key] ?? []));
    pageToken = data.nextPageToken;
    if (!pageToken) break;
  }
  return out.slice(0, cap);
}

function truncate(text: string, structured: unknown): { text: string; structured: unknown } {
  if (text.length <= MAX_CHARS) return { text, structured };
  return {
    text: text.slice(0, MAX_CHARS) + `\n\n…truncated at ${MAX_CHARS} chars.`,
    structured,
  };
}

function fmtTask(t: Task, listTitle?: string): string {
  const box = t.status === "completed" ? "[x]" : "[ ]";
  const due = t.due ? ` · due ${t.due.slice(0, 10)}` : "";
  const where = listTitle ? ` · _${listTitle}_` : "";
  const sub = t.parent ? "  " : "";
  const notes = t.notes ? `\n${sub}  ${t.notes.replace(/\n+/g, " ").slice(0, 300)}` : "";
  return `${sub}- ${box} **${t.title || "(untitled)"}**${due}${where}${notes}`;
}

const server = new McpServer({ name: "google-tasks", version: "0.1.0" });

server.registerTool(
  "google_tasks_list_tasklists",
  {
    title: "List Google Tasks task lists",
    description:
      "The task lists on the connected Google account (READ-ONLY). Start here to get list ids " +
      "for google_tasks_list_tasks. Returns { count, lists: [{id, title, updated}] }.",
    inputSchema: {
      response_format: z.enum(["markdown", "json"]).default("markdown"),
    },
  },
  async ({ response_format }) => {
    const lists = await pageAll<TaskList>("/users/@me/lists", "items", {});
    const structured = { count: lists.length, lists };
    const md =
      `# Google Tasks — ${lists.length} list(s)\n\n` +
      (lists.length
        ? lists.map((l) => `- **${l.title}** · \`${l.id}\`${l.updated ? ` · updated ${l.updated.slice(0, 10)}` : ""}`).join("\n")
        : "_No task lists on this account._");
    const t = truncate(response_format === "json" ? JSON.stringify(structured, null, 2) : md, structured);
    return { content: [{ type: "text", text: t.text }], structuredContent: structured };
  },
);

server.registerTool(
  "google_tasks_list_tasks",
  {
    title: "List tasks in a Google Tasks list",
    description:
      "Tasks in one list (READ-ONLY). Defaults to open tasks only. `due_min`/`due_max` and " +
      "`updated_min` take RFC3339 timestamps (e.g. 2026-08-01T00:00:00Z) — use them for a /daily " +
      "sweep rather than pulling everything. Set list_id to '@default' for the primary list.",
    inputSchema: {
      list_id: z.string().describe("Task list id from google_tasks_list_tasklists, or '@default'"),
      show_completed: z.boolean().default(false),
      show_hidden: z.boolean().default(false),
      due_min: z.string().optional(),
      due_max: z.string().optional(),
      updated_min: z.string().optional(),
      response_format: z.enum(["markdown", "json"]).default("markdown"),
    },
  },
  async ({ list_id, show_completed, show_hidden, due_min, due_max, updated_min, response_format }) => {
    const params: Record<string, string | number | boolean> = {
      showCompleted: show_completed,
      showHidden: show_hidden,
    };
    if (show_completed) params.showCompleted = true;
    if (due_min) params.dueMin = due_min;
    if (due_max) params.dueMax = due_max;
    if (updated_min) params.updatedMin = updated_min;

    const tasks = await pageAll<Task>(`/lists/${encodeURIComponent(list_id)}/tasks`, "items", params);
    const open = tasks.filter((t) => t.status !== "completed").length;
    const structured = { list_id, count: tasks.length, open, tasks };
    const md =
      `# Tasks — list \`${list_id}\`\n${tasks.length} returned · ${open} open\n\n` +
      (tasks.length ? tasks.map((t) => fmtTask(t)).join("\n") : "_No tasks match._");
    const t = truncate(response_format === "json" ? JSON.stringify(structured, null, 2) : md, structured);
    return { content: [{ type: "text", text: t.text }], structuredContent: structured };
  },
);

server.registerTool(
  "google_tasks_search",
  {
    title: "Search tasks across all lists",
    description:
      "Client-side search across every task list (READ-ONLY) — the Google Tasks API has no " +
      "search endpoint, so this enumerates lists and matches on title and notes. AND of all " +
      "terms, case-insensitive. Use for 'is X already tracked on the work list?' checks.",
    inputSchema: {
      query: z.string().describe("Space-separated terms; all must match (title or notes)"),
      show_completed: z.boolean().default(false),
      response_format: z.enum(["markdown", "json"]).default("markdown"),
    },
  },
  async ({ query, show_completed, response_format }) => {
    const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
    const lists = await pageAll<TaskList>("/users/@me/lists", "items", {});
    const hits: Array<Task & { list: string }> = [];
    for (const l of lists) {
      const tasks = await pageAll<Task>(`/lists/${encodeURIComponent(l.id)}/tasks`, "items", {
        showCompleted: show_completed,
        showHidden: false,
      });
      for (const t of tasks) {
        const hay = `${t.title ?? ""} ${t.notes ?? ""}`.toLowerCase();
        if (terms.every((term) => hay.includes(term))) hits.push({ ...t, list: l.title });
      }
    }
    const structured = { query, lists_scanned: lists.length, count: hits.length, tasks: hits };
    const md =
      `# Task search — "${query}"\n${hits.length} hit(s) across ${lists.length} list(s)\n\n` +
      (hits.length ? hits.map((t) => fmtTask(t, t.list)).join("\n") : "_No matches._");
    const t = truncate(response_format === "json" ? JSON.stringify(structured, null, 2) : md, structured);
    return { content: [{ type: "text", text: t.text }], structuredContent: structured };
  },
);

async function main(): Promise<void> {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error(`google-tasks MCP server running on stdio (READ-ONLY, scope: ${SCOPE})`);
}

main().catch((e) => {
  console.error("Fatal:", e);
  process.exit(1);
});
