/**
 * Whoop OAuth token management.
 *
 * Tokens live in tokens.json next to the project root (overridable via
 * WHOOP_TOKENS_FILE). Whoop rotates the refresh token on every refresh,
 * so each successful refresh re-writes the file.
 */

import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import axios from "axios";

export const WHOOP_AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth";
export const WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token";
export const WHOOP_API_BASE = "https://api.prod.whoop.com/developer";

export const REQUIRED_SCOPES = [
  "offline", // refresh tokens
  "read:recovery",
  "read:cycles",
  "read:sleep",
  "read:workout",
  "read:profile",
  "read:body_measurement",
] as const;

export interface StoredTokens {
  access_token: string;
  refresh_token: string;
  // Absolute epoch milliseconds at which access_token expires.
  expires_at: number;
  token_type: string;
  scope: string;
}

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number; // seconds
  token_type: string;
  scope: string;
}

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/** Default tokens file path: <project>/tokens.json */
export function defaultTokensPath(): string {
  // src/ at runtime (tsx) or dist/ (compiled). Project root is one up.
  return resolve(__dirname, "..", "tokens.json");
}

export function tokensPath(): string {
  return process.env.WHOOP_TOKENS_FILE || defaultTokensPath();
}

export function getClientCreds(): { clientId: string; clientSecret: string } {
  const clientId = process.env.WHOOP_CLIENT_ID;
  const clientSecret = process.env.WHOOP_CLIENT_SECRET;
  if (!clientId || !clientSecret) {
    throw new Error(
      "WHOOP_CLIENT_ID and/or WHOOP_CLIENT_SECRET environment variables are not set. " +
        "Register an app at https://developer.whoop.com/, copy the client credentials, and pass them via the MCP server's env block.",
    );
  }
  return { clientId, clientSecret };
}

export async function loadTokens(): Promise<StoredTokens> {
  const path = tokensPath();
  let raw: string;
  try {
    raw = await readFile(path, "utf8");
  } catch (e) {
    throw new Error(
      `No Whoop token file found at ${path}. Run \`npm run auth\` from the whoop server directory to authorize and store tokens.`,
    );
  }
  let parsed: StoredTokens;
  try {
    parsed = JSON.parse(raw) as StoredTokens;
  } catch {
    throw new Error(
      `Token file at ${path} is not valid JSON. Re-run \`npm run auth\` to regenerate it.`,
    );
  }
  if (!parsed.access_token || !parsed.refresh_token || !parsed.expires_at) {
    throw new Error(
      `Token file at ${path} is missing required fields. Re-run \`npm run auth\` to regenerate it.`,
    );
  }
  return parsed;
}

export async function saveTokens(tokens: StoredTokens): Promise<void> {
  const path = tokensPath();
  await writeFile(path, JSON.stringify(tokens, null, 2) + "\n", { mode: 0o600 });
}

export function tokensFromResponse(resp: TokenResponse): StoredTokens {
  // Refresh 60s before actual expiry to absorb clock skew & in-flight latency.
  const safetyWindowMs = 60_000;
  return {
    access_token: resp.access_token,
    refresh_token: resp.refresh_token,
    expires_at: Date.now() + resp.expires_in * 1000 - safetyWindowMs,
    token_type: resp.token_type,
    scope: resp.scope,
  };
}

export async function refreshTokens(refreshToken: string): Promise<StoredTokens> {
  const { clientId, clientSecret } = getClientCreds();
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: clientId,
    client_secret: clientSecret,
    // Whoop requires `offline` scope to be repeated on refresh to keep refresh tokens flowing.
    scope: "offline",
  });
  const { data } = await axios.post<TokenResponse>(WHOOP_TOKEN_URL, body.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: 30_000,
  });
  return tokensFromResponse(data);
}

export async function exchangeCodeForTokens(args: {
  code: string;
  codeVerifier: string;
  redirectUri: string;
}): Promise<StoredTokens> {
  const { clientId, clientSecret } = getClientCreds();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code: args.code,
    redirect_uri: args.redirectUri,
    client_id: clientId,
    client_secret: clientSecret,
    code_verifier: args.codeVerifier,
  });
  const { data } = await axios.post<TokenResponse>(WHOOP_TOKEN_URL, body.toString(), {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    timeout: 30_000,
  });
  return tokensFromResponse(data);
}

/**
 * Return a valid access token, refreshing and persisting a new one if the
 * stored token is past its expiry. Whoop rotates the refresh_token on every
 * refresh, so we always re-write the file after a refresh.
 */
export async function getValidAccessToken(): Promise<string> {
  const stored = await loadTokens();
  if (Date.now() < stored.expires_at) return stored.access_token;
  const refreshed = await refreshTokens(stored.refresh_token);
  await saveTokens(refreshed);
  return refreshed.access_token;
}
