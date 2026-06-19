#!/usr/bin/env node
/**
 * One-time CLI to walk through the Whoop OAuth authorization-code flow
 * (with PKCE) and write tokens.json. Run via `npm run auth`.
 *
 * Steps:
 *   1. Generate a PKCE verifier/challenge.
 *   2. Print the authorization URL for the user to open in a browser.
 *   3. Start a local HTTP listener on REDIRECT_PORT to catch the callback.
 *   4. Exchange the returned code for access + refresh tokens.
 *   5. Save tokens.json with mode 0600.
 */

import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { createHash, randomBytes } from "node:crypto";
import { URL } from "node:url";
import {
  WHOOP_AUTH_URL,
  REQUIRED_SCOPES,
  exchangeCodeForTokens,
  getClientCreds,
  saveTokens,
  tokensPath,
} from "./auth.js";

const REDIRECT_PORT = parseInt(process.env.WHOOP_REDIRECT_PORT || "3456", 10);
const REDIRECT_URI =
  process.env.WHOOP_REDIRECT_URI || `http://localhost:${REDIRECT_PORT}/callback`;

function base64UrlEncode(buf: Buffer): string {
  return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function generatePkcePair(): { verifier: string; challenge: string } {
  const verifier = base64UrlEncode(randomBytes(32));
  const challenge = base64UrlEncode(createHash("sha256").update(verifier).digest());
  return { verifier, challenge };
}

async function main() {
  // Verify creds early so the user isn't surprised after authorizing.
  getClientCreds();

  const { verifier, challenge } = generatePkcePair();
  const state = base64UrlEncode(randomBytes(16));

  const authUrl = new URL(WHOOP_AUTH_URL);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("client_id", process.env.WHOOP_CLIENT_ID!);
  authUrl.searchParams.set("redirect_uri", REDIRECT_URI);
  // WHOOP_SCOPES overrides the default scope set (space-separated), e.g. to
  // diagnose invalid_scope errors by requesting a subset.
  const scopes = process.env.WHOOP_SCOPES?.split(/\s+/).filter(Boolean) ?? REQUIRED_SCOPES;
  authUrl.searchParams.set("scope", scopes.join(" "));
  authUrl.searchParams.set("state", state);
  authUrl.searchParams.set("code_challenge", challenge);
  authUrl.searchParams.set("code_challenge_method", "S256");

  console.log("");
  console.log("=".repeat(72));
  console.log("Whoop OAuth — open this URL in your browser to authorize:");
  console.log("");
  console.log(authUrl.toString());
  console.log("");
  console.log(`Listening for the callback on ${REDIRECT_URI}`);
  console.log(
    `(Make sure ${REDIRECT_URI} is registered as an allowed redirect URI on your Whoop app at developer.whoop.com.)`,
  );
  console.log("=".repeat(72));
  console.log("");

  const result = await new Promise<{ code: string }>((resolveResult, rejectResult) => {
    const server = createServer(async (req: IncomingMessage, res: ServerResponse) => {
      try {
        if (!req.url) {
          res.statusCode = 400;
          res.end("Missing URL");
          return;
        }
        const url = new URL(req.url, `http://localhost:${REDIRECT_PORT}`);
        if (url.pathname !== "/callback") {
          res.statusCode = 404;
          res.end("Not found");
          return;
        }
        const error = url.searchParams.get("error");
        if (error) {
          const desc = url.searchParams.get("error_description") || "";
          res.statusCode = 400;
          res.end(`Authorization failed: ${error} ${desc}`);
          server.close();
          rejectResult(new Error(`Whoop returned error: ${error} ${desc}`));
          return;
        }
        const returnedState = url.searchParams.get("state");
        const code = url.searchParams.get("code");
        if (returnedState !== state) {
          res.statusCode = 400;
          res.end("State mismatch — aborting.");
          server.close();
          rejectResult(new Error("OAuth state mismatch — possible CSRF, aborting."));
          return;
        }
        if (!code) {
          res.statusCode = 400;
          res.end("No code returned.");
          server.close();
          rejectResult(new Error("No authorization code returned by Whoop."));
          return;
        }

        res.statusCode = 200;
        res.setHeader("Content-Type", "text/html; charset=utf-8");
        res.end(
          "<html><body><h2>Whoop authorization complete.</h2><p>You can close this tab and return to the terminal.</p></body></html>",
        );
        server.close();
        resolveResult({ code });
      } catch (e) {
        res.statusCode = 500;
        res.end("Internal error");
        server.close();
        rejectResult(e instanceof Error ? e : new Error(String(e)));
      }
    });

    server.on("error", (err) => rejectResult(err));
    server.listen(REDIRECT_PORT, "127.0.0.1");
  });

  console.log("Authorization code received. Exchanging for tokens...");
  const tokens = await exchangeCodeForTokens({
    code: result.code,
    codeVerifier: verifier,
    redirectUri: REDIRECT_URI,
  });
  await saveTokens(tokens);

  console.log("");
  console.log(`Tokens saved to ${tokensPath()}`);
  console.log("Granted scopes:", tokens.scope);
  console.log("Refresh token rotation is in effect — re-run this only if tokens are lost.");
}

main().catch((err) => {
  console.error("");
  console.error("Auth failed:", err instanceof Error ? err.message : String(err));
  process.exit(1);
});
