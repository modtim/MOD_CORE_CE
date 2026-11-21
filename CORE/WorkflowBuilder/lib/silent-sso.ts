"use client";
import { apiUrl } from "@/lib/runtime-config";
import { b64urlFromBytes, sha256Digest, uuid4 } from "@/lib/pkce-crypto";

// ---------------------------------------------------------------------------
// Silent SSO re-auth (GSEC R-077 / Q27=A).
//
// WB tokens now live in sessionStorage, so a FRESH tab (new tab, or a tab
// whose previous session ended) starts with no token. Instead of immediately
// dumping the user on a login screen, we try a SILENT Keycloak re-auth: an
// authorization request with `prompt=none` against the existing PKCE login
// path. If a Keycloak SSO session cookie is present (the user is logged in to
// the Dashboard on the same parent domain) Keycloak redirects straight back
// with a `code`, no UI. If not, it returns `error=login_required`, and the
// caller falls back to the normal interactive login.
//
// Flow (two hops, no loop):
//   1. Load WB, no token  → startSilentSso() redirects to Keycloak
//      (prompt=none, client_id=workflow-builder-client, PKCE).
//   2. Keycloak redirects to `${origin}/auth/callback?code=...` (success) or
//      `?error=login_required` (no SSO session).
//   3. The /auth/callback page exchanges the code for tokens via the BFF
//      (POST /auth/exchange-code — the SAME endpoint the Dashboard PKCE path
//      uses) and stores them in sessionStorage, then bounces back to the app.
//      On `login_required` it redirects to the Dashboard for a normal login.
//
// The PKCE verifier is kept in sessionStorage (NOT localStorage) so it dies
// with the tab, consistent with R-077. It survives the Keycloak round-trip
// because the callback lands in the SAME tab (a full redirect, not a popup).
// ---------------------------------------------------------------------------

// The Keycloak client that authorizes the WorkflowBuilder origin. It carries
// an `fastapi-app` audience mapper so the API accepts the resulting tokens,
// and a redirect URI of `${WORKFLOWS_URL}/auth/callback` (see
// startup/keycloak-init.sh) — which is exactly `window.location.origin +
// "/auth/callback"` when WB is served at WORKFLOWS_URL.
export const SSO_CLIENT_ID = "workflow-builder-client";
export const SSO_CALLBACK_PATH = "/auth/callback";

const PKCE_VERIFIER_KEY = "workflow_sso_code_verifier";
const SSO_RETURN_TO_KEY = "workflow_sso_return_to";

export const PKCE_VERIFIER_STORAGE_KEY = PKCE_VERIFIER_KEY;
export const SSO_RETURN_TO_STORAGE_KEY = SSO_RETURN_TO_KEY;

// Q9=B (cold deep link): two booleans ride alongside the silent-SSO round
// trip — nothing else is stored here (no token, no query string, no state).
//   prior login : this tab held a stored WB token when the SSO started, i.e.
//                 the user had a login that just proved dead (stale login).
//   attempted   : one-shot loop guard. Set when /auth/callback bounces back
//                 to the WB link after Keycloak said `login_required`; the
//                 very next WB load consumes it and must NOT start silent
//                 SSO a second time (callback -> WB -> SSO -> callback would
//                 otherwise loop forever).
const SSO_PRIOR_LOGIN_KEY = "workflow_sso_prior_login";
const SSO_ATTEMPTED_KEY = "workflow_sso_attempted";

export const SSO_PRIOR_LOGIN_STORAGE_KEY = SSO_PRIOR_LOGIN_KEY;
export const SSO_ATTEMPTED_STORAGE_KEY = SSO_ATTEMPTED_KEY;

// b64urlFromBytes comes from @/lib/pkce-crypto (shared with the challenge path).
function generateCodeVerifier(): string {
  const arr = new Uint8Array(64);
  crypto.getRandomValues(arr);
  return b64urlFromBytes(arr);
}

// crypto.subtle is undefined in insecure contexts (plain http on a LAN IP),
// so the silent-SSO S256 challenge threw on LAN installs (same owner bug as
// the Dashboard login). sha256Digest keeps the native path when present and
// falls back to pure-JS SHA-256 with byte-identical base64url output.
export async function generateCodeChallenge(verifier: string): Promise<string> {
  const digest = await sha256Digest(verifier);
  return b64urlFromBytes(digest);
}

// Pure URL builder — exported for unit testing. Mirrors the Dashboard's
// PKCE auth params (scope WITHOUT offline_access: on Keycloak 26.3 a code->token
// that requests it ends the browser SSO session, 2400LR-b1j) plus `prompt=none`
// for the silent attempt.
export function buildSilentSsoAuthUrl(
  authEndpoint: string,
  redirectUri: string,
  codeChallenge: string,
): string {
  const params = new URLSearchParams({
    client_id: SSO_CLIENT_ID,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: "openid profile email groups",
    prompt: "none",
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
  });
  return `${authEndpoint}?${params.toString()}`;
}

// The callback URI must be an absolute URL on the WB origin. Derived from the
// live origin so it always matches the registered `${WORKFLOWS_URL}/auth/callback`.
export function callbackUri(): string {
  if (typeof window === "undefined") return SSO_CALLBACK_PATH;
  return `${window.location.origin}${SSO_CALLBACK_PATH}`;
}

// Fetch the Keycloak authorization endpoint. WB has no APP_CONFIG (unlike the
// Dashboard); the API's public /auth/discovery is the single source of the
// realm's authorization_endpoint, so we read it there instead of hard-coding.
async function fetchAuthEndpoint(): Promise<string | null> {
  try {
    const resp = await fetch(`${apiUrl()}/auth/discovery`);
    if (!resp.ok) return null;
    const data = await resp.json();
    return typeof data?.authorization_endpoint === "string" ? data.authorization_endpoint : null;
  } catch {
    return null;
  }
}

// Attempt a silent SSO re-auth for THIS tab. Redirects the browser away to
// Keycloak (prompt=none) — the caller (initializeAuth) should treat a call to
// this as terminal for the current page load. Returns true if the redirect
// was initiated; false if we could not build the auth URL (caller should fall
// back to the normal login path immediately).
export async function startSilentSso(
  returnTo?: string,
  opts?: { priorLogin?: boolean },
): Promise<boolean> {
  if (typeof window === "undefined") return false;
  const authEndpoint = await fetchAuthEndpoint();
  if (!authEndpoint) return false;
  try {
    const verifier = generateCodeVerifier();
    const challenge = await generateCodeChallenge(verifier);
    sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
    if (returnTo) sessionStorage.setItem(SSO_RETURN_TO_KEY, returnTo);
    if (opts) {
      if (opts.priorLogin) sessionStorage.setItem(SSO_PRIOR_LOGIN_KEY, "1");
      else sessionStorage.removeItem(SSO_PRIOR_LOGIN_KEY);
    }
    window.location.assign(buildSilentSsoAuthUrl(authEndpoint, callbackUri(), challenge));
    return true;
  } catch {
    return false;
  }
}

// Read + consume the stashed PKCE verifier (used by /auth/callback).
export function consumePkceVerifier(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const v = sessionStorage.getItem(PKCE_VERIFIER_KEY);
    if (v) sessionStorage.removeItem(PKCE_VERIFIER_KEY);
    return v;
  } catch {
    return null;
  }
}

// Read + clear the return path the user was on before the silent redirect.
export function consumeReturnTo(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const v = sessionStorage.getItem(SSO_RETURN_TO_KEY);
    if (v) sessionStorage.removeItem(SSO_RETURN_TO_KEY);
    return v;
  } catch {
    return null;
  }
}

// Read + clear BOTH silent-SSO context keys (return-to + prior-login flag)
// in one call, for /auth/callback's login_required fallback (Q9=B). Same
// read-then-remove pattern as consumeReturnTo; the booleans are all that is
// kept — no token, no query string, no `state`.
export function consumeSsoContext(): { returnTo: string | null; priorLogin: boolean } {
  if (typeof window === "undefined") return { returnTo: null, priorLogin: false };
  let returnTo: string | null = null;
  let priorLogin = false;
  try {
    const v = sessionStorage.getItem(SSO_RETURN_TO_KEY);
    if (v) sessionStorage.removeItem(SSO_RETURN_TO_KEY);
    returnTo = v;
  } catch {
    /* best effort */
  }
  try {
    priorLogin = sessionStorage.getItem(SSO_PRIOR_LOGIN_KEY) === "1";
    sessionStorage.removeItem(SSO_PRIOR_LOGIN_KEY);
  } catch {
    /* best effort */
  }
  return { returnTo, priorLogin };
}

// One-shot loop guard (COMMON MUST 4): callback -> WB link -> silent SSO ->
// callback must not loop. Set the marker when the callback bounces back to
// the WB deep link; the very next WB load consumes it and must NOT start
// silent SSO again.
export function markSsoAttempted(priorLogin: boolean): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(SSO_ATTEMPTED_KEY, priorLogin ? "1" : "0");
  } catch {
    /* private mode / storage disabled */
  }
}

export function consumeSsoAttempted(): { priorLogin: boolean } | null {
  if (typeof window === "undefined") return null;
  try {
    const v = sessionStorage.getItem(SSO_ATTEMPTED_KEY);
    if (v === null) return null;
    sessionStorage.removeItem(SSO_ATTEMPTED_KEY);
    return { priorLogin: v === "1" };
  } catch {
    return null;
  }
}
