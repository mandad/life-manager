/**
 * OAuth 2.0 device-code auth for a personal Microsoft account (consumers tenant).
 *
 * - `npm run auth` (runs main() below) performs the one-time interactive login:
 *   prints a device code + URL, polls until you approve, writes the token cache.
 * - `getAccessToken()` is imported by the MCP server: loads the cache and silently
 *   refreshes via the refresh_token when the access token is near expiry.
 *
 * No client secret (public client). Scopes: Notes.Read Notes.Create offline_access
 * (read everything + create new pages/sections/notebooks; canNOT edit or delete existing notes —
 * Notes.Create grants create-only, not modify/delete).
 * Token cache lives at ONENOTE_TOKEN_PATH (outside OneDrive), chmod 600, never printed.
 */

import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import axios from "axios";

const TENANT = "consumers"; // personal Microsoft accounts
const AUTH_BASE = `https://login.microsoftonline.com/${TENANT}/oauth2/v2.0`;
// Fully-qualify each scope to Graph so the token audience is graph.microsoft.com — the account
// has these consented on BOTH Graph and the legacy OneNote resource, and bare scope names would be
// ambiguous (could mint a token for onenote.com → 401 on Graph). Notes.Create was added 2026-06-25
// to allow page creation; re-consent (npm run auth) is required to pick up the new scope.
const SCOPES =
  "https://graph.microsoft.com/Notes.Read https://graph.microsoft.com/Notes.Create offline_access";
const EXPIRY_SKEW_MS = 120_000; // refresh 2 min early

interface TokenCache {
  access_token: string;
  refresh_token: string;
  expires_at: number; // epoch ms
}

function clientId(): string {
  const id = process.env.ONENOTE_CLIENT_ID;
  if (!id) throw new Error("ONENOTE_CLIENT_ID not set (see SETUP.md Part A).");
  return id;
}

function tokenPath(): string {
  return (
    process.env.ONENOTE_TOKEN_PATH ??
    path.join(homedir(), ".config", "llm-land", "onenote-tokens.json")
  );
}

async function readCache(): Promise<TokenCache | null> {
  try {
    const raw = await fs.readFile(tokenPath(), "utf8");
    return JSON.parse(raw) as TokenCache;
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

/** Exchange a refresh_token for a fresh access_token. */
async function refresh(refreshToken: string): Promise<TokenCache> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: clientId(),
    scope: SCOPES,
    refresh_token: refreshToken,
  });
  const { data } = await axios.post(`${AUTH_BASE}/token`, body.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: 30000,
  });
  const cache: TokenCache = {
    access_token: data.access_token,
    refresh_token: data.refresh_token ?? refreshToken, // MS may rotate or not
    expires_at: Date.now() + Number(data.expires_in) * 1000,
  };
  await writeCache(cache);
  return cache;
}

/** Valid access token for Graph calls; refreshes silently when stale. */
export async function getAccessToken(): Promise<string> {
  const cache = await readCache();
  if (!cache) {
    throw new Error(
      "No OneNote token cache. Run `npm run auth` once to log in (SETUP.md Part B).",
    );
  }
  if (Date.now() < cache.expires_at - EXPIRY_SKEW_MS) return cache.access_token;
  const refreshed = await refresh(cache.refresh_token);
  return refreshed.access_token;
}

/** Interactive one-time device-code login. Run via `npm run auth`. */
async function main(): Promise<void> {
  const cid = clientId();
  const dc = await axios.post(
    `${AUTH_BASE}/devicecode`,
    new URLSearchParams({ client_id: cid, scope: SCOPES }).toString(),
    { headers: { "Content-Type": "application/x-www-form-urlencoded" }, timeout: 30000 },
  );
  const { device_code, user_code, verification_uri, interval, expires_in, message } =
    dc.data;
  // message already contains the URL + code; print it for the user.
  console.error("\n" + (message ?? `Go to ${verification_uri} and enter code ${user_code}`) + "\n");

  const deadline = Date.now() + Number(expires_in) * 1000;
  let waitMs = (Number(interval) || 5) * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, waitMs));
    try {
      const tok = await axios.post(
        `${AUTH_BASE}/token`,
        new URLSearchParams({
          grant_type: "urn:ietf:params:oauth:grant-type:device_code",
          client_id: cid,
          device_code,
        }).toString(),
        { headers: { "Content-Type": "application/x-www-form-urlencoded" }, timeout: 30000 },
      );
      await writeCache({
        access_token: tok.data.access_token,
        refresh_token: tok.data.refresh_token,
        expires_at: Date.now() + Number(tok.data.expires_in) * 1000,
      });
      console.error("✓ Logged in. Token cached at " + tokenPath());
      return;
    } catch (e: any) {
      const err = e?.response?.data?.error;
      if (err === "authorization_pending") continue;
      if (err === "slow_down") { waitMs += 5000; continue; }
      throw new Error(`Device-code login failed: ${err ?? e.message}`);
    }
  }
  throw new Error("Device-code login timed out — re-run `npm run auth`.");
}

// Run interactively only when invoked directly (npm run auth → dist/auth.js).
if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((e) => {
    console.error(e.message);
    process.exit(1);
  });
}
