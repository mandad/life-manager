/**
 * OAuth 2.0 for Google Tasks — READ-ONLY, installed-app (loopback) flow with PKCE.
 *
 * - `npm run auth` (main() below) does the one-time interactive login: starts a tiny local
 *   HTTP listener, prints a URL for the user to open, catches the redirect, exchanges the
 *   code, and writes the token cache.
 * - `getAccessToken()` is imported by the MCP server: loads the cache and silently refreshes
 *   via the refresh_token when the access token is near expiry.
 *
 * SCOPE IS DELIBERATELY MINIMAL: `tasks.readonly` only. That is a *different OAuth scope from
 * Gmail* — granting it gives this machine the task list and no mailbox access whatsoever, even
 * though the items themselves originate as email flags. The server exposes no write path at
 * all: there is no code here that can create, modify, complete or delete a task.
 *
 * Why loopback rather than device code: Google's device-code flow supports only a restricted
 * scope set and does not cover Tasks. The loopback redirect is the supported installed-app
 * flow for arbitrary scopes, and PKCE means the client secret is not load-bearing.
 *
 * Token cache: GOOGLE_TASKS_TOKEN_PATH (default ~/.config/llm-land-mcp/google-tasks-token.json),
 * chmod 600, never printed.
 */

import { promises as fs, existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import http from "node:http";
import path from "node:path";
import crypto from "node:crypto";
import axios from "axios";

/** Mirror of the onenote server's helper: `npm run auth` runs node directly and never sources
 *  secrets.env, so self-load it. Already-set process.env always wins, so the mcp-sync launch
 *  path is unaffected. */
function loadSecretsEnv(): void {
  const file =
    process.env.LLM_LAND_SECRETS_ENV ??
    path.join(homedir(), ".config", "llm-land-mcp", "secrets.env");
  if (!existsSync(file)) return;
  for (const raw of readFileSync(file, "utf8").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const eq = line.indexOf("=");
    const key = line.slice(0, eq).replace(/^export\s+/, "").trim();
    if (!key || key in process.env) continue;
    let val = line.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'")))
      val = val.slice(1, -1);
    val = val.replace(/\$\{?(\w+)\}?/g, (_, n) => (n === "HOME" ? homedir() : process.env[n] ?? ""));
    process.env[key] = val;
  }
}
loadSecretsEnv();

export const SCOPE = "https://www.googleapis.com/auth/tasks.readonly";
const AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth";
const TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";

function tokenPath(): string {
  return (
    process.env.GOOGLE_TASKS_TOKEN_PATH ??
    path.join(homedir(), ".config", "llm-land-mcp", "google-tasks-token.json")
  );
}

interface TokenCache {
  access_token: string;
  refresh_token: string;
  expires_at: number; // epoch ms
  scope?: string;
  account_hint?: string;
  authorized_at?: number; // epoch ms of the last interactive `npm run auth` (refresh-token issuance)
}

async function readCache(): Promise<TokenCache | null> {
  try {
    return JSON.parse(await fs.readFile(tokenPath(), "utf8")) as TokenCache;
  } catch {
    return null;
  }
}

async function writeCache(c: TokenCache): Promise<void> {
  const p = tokenPath();
  await fs.mkdir(path.dirname(p), { recursive: true });
  await fs.writeFile(p, JSON.stringify(c, null, 2), { mode: 0o600 });
  await fs.chmod(p, 0o600).catch(() => {});
}

function requireClientId(): string {
  const id = process.env.GOOGLE_TASKS_CLIENT_ID;
  if (!id)
    throw new Error(
      "GOOGLE_TASKS_CLIENT_ID is not set. Add it to ~/.config/llm-land-mcp/secrets.env " +
        "(see mcp-servers/google-tasks/SETUP.md).",
    );
  return id;
}

/** Refresh if the access token is missing or expires within 2 minutes. */
export async function getAccessToken(): Promise<string> {
  const cache = await readCache();
  if (!cache?.refresh_token)
    throw new Error(
      `No Google Tasks token cache at ${tokenPath()}. Run one-time: ` +
        `cd mcp-servers/google-tasks && npm run auth`,
    );
  if (cache.access_token && Date.now() < cache.expires_at - 120_000) return cache.access_token;

  const body = new URLSearchParams({
    client_id: requireClientId(),
    refresh_token: cache.refresh_token,
    grant_type: "refresh_token",
  });
  if (process.env.GOOGLE_TASKS_CLIENT_SECRET)
    body.set("client_secret", process.env.GOOGLE_TASKS_CLIENT_SECRET);

  const { data } = await axios.post(TOKEN_ENDPOINT, body.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: 20_000,
  });
  const updated: TokenCache = {
    access_token: data.access_token,
    refresh_token: data.refresh_token ?? cache.refresh_token, // Google usually omits on refresh
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
    scope: data.scope ?? cache.scope,
    account_hint: cache.account_hint,
    authorized_at: cache.authorized_at, // preserve across silent refreshes
  };
  await writeCache(updated);
  return updated.access_token;
}

// ------------------------------------------------------------------ interactive login
function b64url(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function interactiveLogin(): Promise<void> {
  const clientId = requireClientId();
  const verifier = b64url(crypto.randomBytes(64));
  const challenge = b64url(crypto.createHash("sha256").update(verifier).digest());
  const state = b64url(crypto.randomBytes(16));

  const server = http.createServer();
  await new Promise<void>((res) => server.listen(0, "127.0.0.1", res));
  const port = (server.address() as { port: number }).port;
  const redirectUri = `http://localhost:${port}/callback`;

  const url =
    `${AUTH_ENDPOINT}?` +
    new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirectUri,
      response_type: "code",
      scope: SCOPE,
      access_type: "offline",
      prompt: "consent", // force a refresh_token even on re-auth
      code_challenge: challenge,
      code_challenge_method: "S256",
      state,
    }).toString();

  console.error("\n=== Google Tasks — one-time authorization (READ-ONLY) ===");
  console.error("Scope requested:", SCOPE);
  console.error("\nOpen this URL in a browser signed in to your WORK Google account:\n");
  console.error(url + "\n");
  console.error(
    "If your Workspace admin blocks unconfigured third-party apps, the consent screen will\n" +
      "refuse and NOTHING is granted — that is the safe failure. Ask them to allowlist this\n" +
      "OAuth client ID, then re-run.\n",
  );
  console.error("Waiting for the redirect…");

  const code = await new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timed out after 5 minutes")), 300_000);
    server.on("request", (req, res) => {
      const u = new URL(req.url ?? "/", `http://localhost:${port}`);
      if (u.pathname !== "/callback") {
        res.writeHead(404).end();
        return;
      }
      const err = u.searchParams.get("error");
      const got = u.searchParams.get("code");
      const gotState = u.searchParams.get("state");
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(
        `<html><body style="font-family:system-ui;padding:2rem">` +
          `<h2>${err ? "Authorization failed" : "Authorized — read-only Google Tasks"}</h2>` +
          `<p>${err ? err : "You can close this tab and return to the terminal."}</p>` +
          `</body></html>`,
      );
      clearTimeout(timer);
      server.close();
      if (err) reject(new Error(`authorization denied: ${err}`));
      else if (gotState !== state) reject(new Error("state mismatch — aborting"));
      else if (!got) reject(new Error("no code in redirect"));
      else resolve(got);
    });
  });

  const body = new URLSearchParams({
    client_id: clientId,
    code,
    code_verifier: verifier,
    grant_type: "authorization_code",
    redirect_uri: redirectUri,
  });
  if (process.env.GOOGLE_TASKS_CLIENT_SECRET)
    body.set("client_secret", process.env.GOOGLE_TASKS_CLIENT_SECRET);

  const { data } = await axios.post(TOKEN_ENDPOINT, body.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: 30_000,
  });
  if (!data.refresh_token)
    throw new Error(
      "Google returned no refresh_token. Re-run after revoking prior access, or confirm the " +
        "OAuth client is of type 'Desktop app'.",
    );

  await writeCache({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Date.now() + (data.expires_in ?? 3600) * 1000,
    scope: data.scope,
    authorized_at: Date.now(),
  });
  console.error(`\n✅ Token cached at ${tokenPath()} (chmod 600). Granted scope: ${data.scope}`);
  console.error("Restart Claude Code to pick the server up.\n");
}

// ------------------------------------------------------------------ auth-health probe (#50)
/**
 * `--check` auth-health probe (automation #50). Reads the token cache, does ONE live
 * tasklists.list call (the only reliable test of the refresh token), and reports
 * OK / EXPIRES-SOON / DEAD with an exit code so /daily can warn BEFORE the token silently
 * dies mid-week. The OAuth app is stuck in "Testing" publishing status (personal Cloud
 * project), so refresh tokens expire ~7 days after each interactive auth; when `authorized_at`
 * is present we also warn as that approaches.
 *   exit 0 = OK · exit 1 = OK but expiring soon (re-auth advised) · exit 2 = DEAD (re-auth now)
 */
const REAUTH = "cd mcp-servers/google-tasks && npm run auth";
export async function checkAuth(): Promise<number> {
  const cache = await readCache();
  if (!cache?.refresh_token) {
    console.log(`DEAD: no Google Tasks token cache at ${tokenPath()}. Re-auth: ${REAUTH}`);
    return 2;
  }
  let soon = "";
  if (typeof cache.authorized_at === "number") {
    const days = (Date.now() - cache.authorized_at) / 86_400_000;
    if (days >= 6)
      soon = ` (authorized ${days.toFixed(1)}d ago — Testing-mode tokens expire ~7d; re-auth soon: ${REAUTH})`;
  }
  try {
    const token = await getAccessToken();
    await axios.get("https://tasks.googleapis.com/tasks/v1/users/@me/lists", {
      headers: { Authorization: `Bearer ${token}` },
      params: { maxResults: 1 },
      timeout: 20_000,
    });
  } catch (e) {
    const msg = axios.isAxiosError(e)
      ? `${e.response?.status ?? ""} ${JSON.stringify(e.response?.data ?? e.message)}`.trim()
      : String(e);
    console.log(`DEAD: Google Tasks token rejected (${msg}). Re-auth: ${REAUTH}`);
    return 2;
  }
  if (soon) {
    console.log(`EXPIRES-SOON: token still works${soon}`);
    return 1;
  }
  console.log("OK: Google Tasks token valid.");
  return 0;
}

const isMain =
  process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href;
if (isMain) {
  const run = process.argv.includes("--check")
    ? checkAuth().then((code) => process.exit(code))
    : interactiveLogin();
  run.catch((e) => {
    console.error("\n❌ " + (e instanceof Error ? e.message : String(e)));
    process.exit(1);
  });
}
