"use client";
import { apiUrl, dashboardUrl } from "@/lib/runtime-config";
// Side-effect import installs the clicked-control busy listeners (one-line mount).
import { attachBusyRequest, releaseBusyRequest } from "@/lib/busy-indicator";

// Shared authenticated fetch with silent token-refresh.
//
// Background: the workflow-builder hook (`useWorkflowApi`) has its own
// authenticatedFetch baked into React state. That's fine for the hook's
// own callers, but secondary components (BuildQueueRow's poll loops,
// live-log fetcher, etc.) fire outside the hook closure and were using
// raw `fetch()` — so when the Keycloak access token expired mid-session
// their polls started 401'ing forever and the Node Queue went blank.
//
// This module is the single source of truth for "fetch with retry on
// expired-token". Both the hook and the row import it. On 401 we hit
// /auth/refresh-token with the stored refresh token, update localStorage
// in place, dispatch a `mod:auth:token-refreshed` event so React
// consumers can sync their own state, and retry the original request.

const API_URL = apiUrl();
const DASHBOARD_URL = dashboardUrl();

export const ACCESS_TOKEN_KEY = "workflow_access_token";
export const REFRESH_TOKEN_KEY = "workflow_refresh_token";
export const TOKEN_REFRESHED_EVENT = "mod:auth:token-refreshed";
// Dispatched when authenticatedFetch exhausted the silent-refresh path
// and would otherwise hard-logout (but hardLogoutOnFailure was false,
// i.e. a background poll). The workflow-builder listens for this and
// shows an inline "Workers unavailable / connection issue" banner
// rather than blasting the user out mid-wizard-edit.
export const AUTH_CONNECTION_LOST_EVENT = "mod:auth:connection-lost";
// Fired when a retry succeeds after the connection-lost event — used
// to clear the banner automatically.
export const AUTH_CONNECTION_RESTORED_EVENT = "mod:auth:connection-restored";

// --------------------------------------------------------------------------
// Token storage lives in sessionStorage, NOT localStorage (GSEC R-077).
//
// localStorage is persistent and shared across ALL tabs of an origin, so a
// WB access/refresh token kept there survived browser restarts and bled
// across tabs — an operator could close the laptop, reopen days later, and
// still be silently authenticated as whatever user logged in last. session
// storage is per-tab and cleared when the tab is closed, which matches the
// "session" the token actually represents. Any tokens a previous release
// left in localStorage are NOT copied over — migrateLegacyLocalStorageTokens
// deletes them outright on load, so a fresh tab re-authenticates (silently
// via SSO, or through the normal Dashboard login).
export function getAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return sessionStorage.getItem(ACCESS_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return sessionStorage.getItem(REFRESH_TOKEN_KEY);
  } catch {
    return null;
  }
}

// The Keycloak client that minted the current WB token. Keycloak refresh
// tokens are CLIENT-BOUND, so a refresh must be made against the same client
// that issued the token. Dashboard hand-offs carry a `dashboard-client`
// token; the silent-SSO path mints a `workflow-builder-client` token. We
// record which one is active so tryRefreshToken uses the right client_id.
export const TOKEN_CLIENT_KEY = "workflow_token_client";
export const DEFAULT_TOKEN_CLIENT = "dashboard-client";

// Persist a freshly-minted (handoff, PKCE-exchange, or refresh-rotated) token
// pair for THIS tab. `refresh` is optional because a silent refresh may
// return a new access token while reusing the same refresh token. `clientId`
// records the minting Keycloak client so later refreshes target it.
export function saveWorkflowTokens(
  access: string,
  refresh?: string | null,
  clientId?: string | null,
): void {
  if (typeof window === "undefined") return;
  try {
    sessionStorage.setItem(ACCESS_TOKEN_KEY, access);
    if (refresh) sessionStorage.setItem(REFRESH_TOKEN_KEY, refresh);
    if (clientId) sessionStorage.setItem(TOKEN_CLIENT_KEY, clientId);
  } catch {
    /* storage full or unavailable — best effort */
  }
}

export function getTokenClient(): string {
  if (typeof window === "undefined") return DEFAULT_TOKEN_CLIENT;
  try {
    return sessionStorage.getItem(TOKEN_CLIENT_KEY) || DEFAULT_TOKEN_CLIENT;
  } catch {
    return DEFAULT_TOKEN_CLIENT;
  }
}

// One-time migration (GSEC R-077): drop any token keys a prior release left
// in localStorage. They are deleted, NOT copied — the user re-authenticates
// cleanly. Idempotent and stateless (WB no longer WRITES token keys to
// localStorage, so re-running it can never remove a live value); call it on
// every load.
export function migrateLegacyLocalStorageTokens(): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    // Legacy generic keys hardLogout once cleared from localStorage as well.
    localStorage.removeItem("accessToken");
    localStorage.removeItem("refreshToken");
  } catch {
    /* localStorage unavailable (private mode) — nothing to migrate */
  }
}

// --------------------------------------------------------------------------
// Cross-origin session integrity (security: cross-user session bleed).
//
// WB is a SEPARATE ORIGIN from the Dashboard, so a Dashboard logout or
// user-switch cannot reach WB's own-origin localStorage. Without a check, a
// WB load without a fresh `?token=` silently reuses whatever
// workflow_access_token is stored — even the PREVIOUS user's token in a tab
// the operator now believes is a different user (open WB as qa-owner →
// Dashboard logout → log in as qa-user → reload WB ⇒ qa-owner's workflows).
//
// The Dashboard writes the currently-authenticated identity (the token's
// Keycloak `sub`) to a cookie on the shared parent domain (.example.com),
// and sets the logged-out sentinel on logout. WB reads it here and refuses to
// resurrect a stored token whose identity doesn't match the active session —
// so a user switch or a logout can no longer bleed the prior session.
export const ACTIVE_IDENTITY_COOKIE = "mod_active_identity";
export const IDENTITY_LOGGED_OUT = "__logged_out__";

// Extract the Keycloak `sub` claim from a JWT without an API round-trip.
// base64url → base64 with padding restored (JWT payloads strip `=`); returns
// null for missing/malformed tokens so callers can treat that as "unknown".
export function decodeTokenSub(token: string | null | undefined): string | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    let padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4 !== 0) padded += "=";
    const json =
      typeof atob === "function"
        ? atob(padded)
        : Buffer.from(padded, "base64").toString("utf8");
    const payload = JSON.parse(json) as Record<string, unknown>;
    const sub = payload.sub;
    return sub == null ? null : String(sub);
  } catch {
    return null;
  }
}

// : read ONLY the JWT `exp` claim (never a
// token, never logged) so the proactive refresher below can schedule against
// it. Same decode shape as decodeTokenSub / hooks/use-idle-warning.ts tokenExpMs.
export function accessTokenExpMs(token: string | null): number | null {
  if (!token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    let padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    while (padded.length % 4 !== 0) padded += "=";
    const json =
      typeof atob === "function"
        ? atob(padded)
        : Buffer.from(padded, "base64").toString("utf8");
    const payload = JSON.parse(json) as Record<string, unknown>;
    const exp = payload.exp;
    return typeof exp === "number" && Number.isFinite(exp) && exp > 0
      ? exp * 1000
      : null;
  } catch {
    return null;
  }
}

// The Dashboard has refreshed access tokens proactively for a long release
// ("Started proactive token refresh interval", CORE/Dashboard/js/auth.js);
// the builder never did — refresh was REACTIVE only (authenticatedFetch 401
// -> tryRefreshToken). Owner saw 401s in the Workflow Builder near the
// 5-minute token boundary on the Dell: a request that raced the expiry got
// the dead token, and parallel requests storm it. Fire the EXISTING refresh
// (tryRefreshToken, which routes by the minting client and rotates storage
// via saveWorkflowTokens) ~60 s before the access token's exp, re-arm after
// every refresh (a failed one does not storm: its window is gone and the
// next one only comes with a new token/visibility event), cancel on logout,
// and wake-on-visibilitychange so a tab hidden by Chrome's timer throttling
// still refreshes when it comes back (and a sibling tab's fresher handoff
// suppresses our older stale window).
// `now`/`setTimeout_` are injectable for the Jest suites (same shape as
// hooks/use-idle-warning.ts startIdleWarning); production passes the globals.
export const WB_PROACTIVE_REFRESH_LEAD_MS = 60_000;

let _proactiveTimer: number | null = null;

let _proactiveListener: (() => void) | null = null;

function _proactiveRefreshOnce(
  token: string,
  now: () => number,
  setTimeout_: (fn: () => void, ms: number) => unknown,
): void {
  // ONE pending window per tab: a re-arm replaces it, so an old exp's
  // schedule can never fire ahead of a fresher token's window.
  if (_proactiveTimer !== null) {
    try {
      clearTimeout(_proactiveTimer as unknown as number);
    } catch {
      /* already fired */
    }
    _proactiveTimer = null;
  }
  if (!token) return;
  const expMs = accessTokenExpMs(token);
  const wait = expMs === null ? -1 : expMs - now() - WB_PROACTIVE_REFRESH_LEAD_MS;
  if (expMs === null || wait <= 0) return; // opaque token, or already inside the window
  _proactiveTimer = setTimeout_(() => {
    _proactiveTimer = null;
    void tryRefreshToken().then(() => {
      const cur = getAccessToken();
      if (cur) void _proactiveRefreshOnce(cur, now, setTimeout_); // re-arm on the rotated exp
    });
  }, wait) as unknown as number;
}

function _proactiveOnVisibility(
  now: () => number,
  setTimeout_: (fn: () => void, ms: number) => unknown,
): void {
  // Chrome throttles timers in hidden/backgrounded tabs to <=1/min, so a
  // scheduled window may never fire while the tab sleeps. On wake, recompute
  // from whatever token is CURRENT (a sibling tab's fresher handoff replaces
  // our older stale window). A hidden event leaves the pending window alone.
  if (typeof document === "undefined" || document.visibilityState !== "visible") return;
  void _proactiveRefreshOnce(getAccessToken(), now, setTimeout_);
}

export function startProactiveTokenRefresh(
  token: string | null,
  opts: { now?: () => number; setTimeout?: (fn: () => void, ms: number) => unknown } = {},
): boolean {
  const t = token ?? getAccessToken();
  if (!t) return false;
  const now = opts.now ?? Date.now;
  const setTimeout_: (fn: () => void, ms: number) => unknown =
    opts.setTimeout ?? ((fn, ms) => setTimeout(fn, ms) as unknown);
  void _proactiveRefreshOnce(t, now, setTimeout_);
  if (typeof document !== "undefined" && _proactiveListener === null) {
    _proactiveListener = () => void _proactiveOnVisibility(now, setTimeout_);
    try {
      document.addEventListener("visibilitychange", _proactiveListener);
    } catch {
      /* listener optional: tests dispatch the event directly */
    }
  }
  return _proactiveTimer !== null;
}

export function stopProactiveTokenRefresh(): void {
  if (_proactiveListener !== null) {
    if (typeof document !== "undefined") {
      try {
        document.removeEventListener("visibilitychange", _proactiveListener);
      } catch {
        /* never attached */
      }
    }
    _proactiveListener = null;
  }
  if (_proactiveTimer === null) return;
  try {
    clearTimeout(_proactiveTimer);
  } catch {
    /* already fired */
  }
  _proactiveTimer = null;
}

// Read the parent-domain identity marker the Dashboard maintains. Returns
// null when absent (e.g. a Dashboard that predates this fix, or a browser
// that has never authenticated through it).
export function readActiveIdentityCookie(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${ACTIVE_IDENTITY_COOKIE}=([^;]*)`),
  );
  if (!m) return null;
  // The Dashboard writes the raw Keycloak `sub` (a UUID), but tolerate a
  // URL-encoded value too. decodeURIComponent THROWS on a stray/percent-
  // malformed value; a throw here would propagate out of initializeAuth and
  // strand WB on <Loading…>, so fall back to the raw cookie value instead of
  // letting the whole auth bootstrap die.
  try {
    return decodeURIComponent(m[1]);
  } catch {
    return m[1];
  }
}

export type StoredTokenTrust = "use" | "reject";

// Decide whether a stored WB token may be silently reused on a load that
// carried NO fresh `?token=`. Pure + exported so it is unit-testable.
//   - the explicit logout sentinel → reject (wipe tokens + fresh handoff);
//   - a GENUINE cross-user mismatch — we know BOTH identities (the stored
//     token decodes to a sub AND the cookie names a user) and they differ →
//     reject (wipe tokens + force a fresh Dashboard handoff);
//   - cookie absent → "use" (fall back to the prior /me validation; the
//     transitional path until the Dashboard writes the cookie, and keeps
//     today's reload behaviour from regressing);
//   - cookie present AND matching the stored token's sub → same user, reuse;
//   - stored token undecodable (storedSub === null) → "use" and let the /me
//     validation below decide. Crucially we do NOT reject here: rejecting an
//     unknown sub against a present cookie would bounce the CURRENT user on a
//     load where the token simply couldn't be decoded, and a genuinely stale
//     other-user token is always a decodable Keycloak JWT (so it takes the
//     cross-user branch above) — an undecodable token just fails /me.
export function decideStoredTokenTrust(
  storedSub: string | null,
  cookieIdentity: string | null,
): StoredTokenTrust {
  if (cookieIdentity === IDENTITY_LOGGED_OUT) return "reject";
  if (cookieIdentity && storedSub && cookieIdentity !== storedSub) return "reject";
  return "use";
}

// --------------------------------------------------------------------------
// Cold deep-link decisions (Q9=B: the Workflow Builder must show the
// "access denied" screen when a workflow is opened by a cold `/?workflow=<id>`
// link, instead of bouncing to the Dashboard and losing the id).
//
// Everything here is PURE (no fetch, no logging, no token touched) and
// exported so the Jest suites can pin every row of the truth table.
//
// Strict id parse: only a bare positive integer with no more than 15 digits
// counts. NOT `Number(raw)` — "1e3", " 7", "0x10" must all be null, so a
// junk value can never reach a redirect target.
export function parseWorkflowIdParam(search: string): number | null {
  const raw = new URLSearchParams(search).get("workflow");
  if (raw === null || !/^[1-9][0-9]{0,14}$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : null;
}

export type ColdLinkOutcome = "denied" | "login";

// "Stale login" = a `?workflow=<id>` id is present, the identity cookie is
// NOT the logout sentinel, and either this tab held a stored WB token that
// proved dead (priorLogin) or this browser has logged in through the
// Dashboard (the parent-domain identity cookie is present and non-empty).
// Anything else with an id is a never-logged-in visitor -> "login". An
// explicit logout (`__logged_out__`) is NOT stale: it goes to login.
export function decideColdLinkOutcome(
  a: { pendingWorkflowId: number | null; priorLogin: boolean; identityCookie: string | null },
): ColdLinkOutcome {
  if (a.pendingWorkflowId === null) return "login";
  if (a.identityCookie === IDENTITY_LOGGED_OUT) return "login";
  if (a.priorLogin || (a.identityCookie !== null && a.identityCookie !== "")) return "denied";
  return "login";
}

// Pending deep-link id (Q9 case 2: a never-logged-in visitor is sent to the
// Dashboard to log in, then must come back to `/?workflow=<id>`). Stored in
// sessionStorage as the BARE integer string — never a token, never a URL —
// so it survives clearWorkflowTokens (which only wipes the token keys).
const PENDING_OPEN_ID_KEY = "workflow_pending_open_id";

export function stashPendingWorkflowId(id: number): void {
  if (!Number.isSafeInteger(id) || id <= 0) return;
  try {
    sessionStorage.setItem(PENDING_OPEN_ID_KEY, String(id));
  } catch {
    /* private mode / storage disabled: nothing to stash */
  }
}

export function peekPendingWorkflowId(): number | null {
  try {
    const v = sessionStorage.getItem(PENDING_OPEN_ID_KEY);
    if (v === null) return null;
    // Re-validate on read: a hand-poisoned value must never be trusted.
    if (!/^[1-9][0-9]{0,14}$/.test(v)) return null;
    const n = Number(v);
    return Number.isSafeInteger(n) ? n : null;
  } catch {
    return null;
  }
}

export function consumePendingWorkflowId(): number | null {
  const n = peekPendingWorkflowId();
  try {
    sessionStorage.removeItem(PENDING_OPEN_ID_KEY);
  } catch {
    /* best effort */
  }
  return n;
}

// --------------------------------------------------------------------------
// Reactive session reconciliation (the persistent-bleed fix).
//
// decideStoredTokenTrust is the COLD-LOAD guard: it runs once in
// initializeAuth's mount effect. But a WB tab that is left OPEN never re-runs
// that effect, so the original edb1e30 guard could not catch a user switch
// that happened AFTER the tab loaded:
//
//   1. open WB as qa-owner (?token=)      → tab renders qa-owner, cookie=owner
//   2. in the Dashboard, log in as qa-user → cookie flips to qa-user, and a new
//      WB handoff overwrites this origin's stored token with qa-user's
//   3. the qa-owner tab from step 1 is STILL OPEN — it never reloads, so it
//      keeps polling /road/ with the qa-owner token it holds in React
//      state and keeps showing qa-owner's workflows. The operator believes it
//      is a "qa-user" tab → cross-user bleed.
//
// reconcileSession runs on focus / visibility / cross-tab storage changes and
// compares the token THIS TAB is actively using (inMemorySub) against both the
// active-identity cookie and the token now in localStorage:
//   - "reauth-wipe": the token this tab uses no longer matches the active
//     Dashboard identity (user switch or explicit logout) → the tab is stale;
//     wipe WB tokens and force a fresh handoff before it can fetch again.
//   - "reauth-keep": a sibling tab has since handed off a DIFFERENT user into
//     this origin's storage. That stored token is the new valid session (do NOT
//     wipe it), but this tab is still rendering the previous user → bounce it
//     through a fresh handoff so nothing of the old user survives on screen.
//   - "ok": same user (a token rotation is handled by the caller syncing state).
export type SessionReconcileAction = "ok" | "reauth-wipe" | "reauth-keep";

export function reconcileSession(
  inMemorySub: string | null,
  storedSub: string | null,
  cookieIdentity: string | null,
): SessionReconcileAction {
  // The token this tab is actively using is wrong for the active identity
  // (logout sentinel, or a genuinely different user is now active).
  if (decideStoredTokenTrust(inMemorySub, cookieIdentity) === "reject") {
    return "reauth-wipe";
  }
  // A different identity took over this origin's stored token since the tab
  // loaded. The stored token is the new valid session — bounce, but keep it.
  if (inMemorySub && storedSub && inMemorySub !== storedSub) {
    return "reauth-keep";
  }
  return "ok";
}

// Wipe only the WB session tokens (WB's own identity-invalidation). Tokens
// live in sessionStorage (R-077); the localStorage removeItems are belt-and-
// braces against any legacy stragglers from a prior release.
export function clearWorkflowTokens() {
  stopProactiveTokenRefresh(); // no scheduled refresh survives a logout
  try {
    sessionStorage.removeItem(ACCESS_TOKEN_KEY);
    sessionStorage.removeItem(REFRESH_TOKEN_KEY);
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  } catch {
    /* best effort */
  }
}

// Force a fresh Dashboard handoff. On an identity mismatch or logout sentinel
// we bounce to the Dashboard, which re-issues a `?token=` for the CURRENTLY
// authenticated user. `wipeTokens` is true when this origin's stored token is
// itself the stale/offending one (so it is not replayed on the next load); it
// is false when a sibling tab already stored the correct new-user token and we
// only need to reload this stale tab.
export function forceReauth(wipeTokens: boolean) {
  if (wipeTokens) clearWorkflowTokens();
  if (typeof window !== "undefined") {
    window.location.href = DASHBOARD_URL;
  }
}

// Dedupe parallel refresh attempts: if multiple background polls
// fire 401 at the same moment, they all wait on one refresh request.
let refreshInFlight: Promise<string | null> | null = null;

interface RefreshResult {
  access: string;
  refresh: string;
}

// Refresh via the API BFF. The BFF injects the server-held client_secret,
// which it only knows for the CONFIDENTIAL clients (dashboard-client,
// fastapi-app). This is the right path for tokens handed off from the
// Dashboard (minted by dashboard-client).
//  (owner saw ~10 s waits on
// every builder click on the Dell; the Dell log showed 401 bursts + refreshes).
// The refresh path had NO timeout on any of its three fetches, and every 401-
// ing request awaits the SAME `refreshInFlight` promise — so ONE stalled
// refresh froze every click until the browser's own connect timeout. Every
// refresh fetch now carries a 5 s AbortController signal; a timeout is treated
// EXACTLY like a failed refresh (the existing hard re-login path), so the
// worst case for any click is bounded, never a hang.
export const REFRESH_FETCH_TIMEOUT_MS = 5_000;

// fetch() with a hard watchdog. The `signal` is what a modern browser honours;
// the racing 5 s promise is what makes the await itself bounded (older engines
// and test mocks ignore `signal`), so the caller always resumes on time.
// Returns null on timeout or any network/parse throw.
async function _fetchWithRefreshTimeout(
  url: string,
  opts: { method?: string; headers?: Record<string, string>; body?: string } = {},
  timeoutMs: number = REFRESH_FETCH_TIMEOUT_MS,
): Promise<any> {
  const controller = new AbortController();
  let handle: unknown = null;
  const timeoutPromise = new Promise<any>((resolve) => {
    handle = setTimeout(() => {
      controller.abort();
      resolve(null);
    }, timeoutMs);
  });
  try {
    const resp = await Promise.race([
      fetch(url, { ...opts, signal: controller.signal } as any),
      timeoutPromise,
    ]);
    return resp === null ? null : resp;
  } catch {
    return null;
  } finally {
    try {
      if (handle) clearTimeout(handle as any);
    } catch {
      /* best effort */
    }
  }
}

async function _bffRefresh(rt: string, clientId: string): Promise<RefreshResult | null> {
  const resp = await _fetchWithRefreshTimeout(`${API_URL}/auth/refresh-token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      refresh_token: rt,
      client_id: clientId,
    }),
  });
  if (!resp || !resp.ok) return null;
  const data = await resp.json();
  const access = data.access_token || data.accessToken;
  if (!access) return null;
  const refresh = data.refresh_token || data.refreshToken || rt;
  return { access, refresh };
}

// Refresh a PUBLIC-client token directly against Keycloak. WB's silent-SSO
// path mints workflow-builder-client tokens, whose refresh tokens the BFF
// cannot honor (it holds no secret for that public client). Public clients
// may call the token endpoint themselves; the form-encoded body is a
// "simple" request, so no CORS preflight is triggered (Keycloak allows the
// WB origin via the client's webOrigins).
// The OIDC discovery document never changes for the life of a tab, yet the
// direct refresh used to fetch it on EVERY refresh (two sequential round trips
// per 401). Cache the token endpoint in memory for the tab: fetched once,
// reused; refetched only after a failure (a dead/stale endpoint must be
// re-discovered, e.g. when the Dell's LAN IP drifts).
let _discoveryTokenEndpoint: string | null = null;

export function resetDiscoveryCache(): void {
  _discoveryTokenEndpoint = null;
}

async function _keycloakTokenEndpoint(): Promise<string | null> {
  if (_discoveryTokenEndpoint) return _discoveryTokenEndpoint;
  const disc = await _fetchWithRefreshTimeout(`${API_URL}/auth/discovery`);
  if (!disc || !disc.ok) {
    _discoveryTokenEndpoint = null;
    return null;
  }
  const { token_endpoint: tokenEndpoint } = await disc.json();
  if (typeof tokenEndpoint !== "string" || !tokenEndpoint) {
    _discoveryTokenEndpoint = null;
    return null;
  }
  _discoveryTokenEndpoint = tokenEndpoint;
  return tokenEndpoint;
}

async function _keycloakDirectRefresh(rt: string, clientId: string): Promise<RefreshResult | null> {
  const tokenEndpoint = await _keycloakTokenEndpoint();
  if (!tokenEndpoint) return null;
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: clientId,
    refresh_token: rt,
  });
  const resp = await _fetchWithRefreshTimeout(tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!resp || !resp.ok) {
    // The endpoint we cached is dead/stale: drop it so the NEXT refresh
    // re-discovers (never cache a failing address).
    _discoveryTokenEndpoint = null;
    return null;
  }
  const data = await resp.json();
  const access = data.access_token;
  if (!access) return null;
  const refresh = data.refresh_token || rt;
  return { access, refresh };
}

export async function tryRefreshToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;
  const rt = getRefreshToken();
  if (!rt) return null;
  refreshInFlight = (async () => {
    try {
      const clientId = getTokenClient();
      const result =
        clientId === DEFAULT_TOKEN_CLIENT
          ? await _bffRefresh(rt, clientId)
          : await _keycloakDirectRefresh(rt, clientId);
      if (!result) return null;
      saveWorkflowTokens(result.access, result.refresh);
      window.dispatchEvent(
        new CustomEvent(TOKEN_REFRESHED_EVENT, {
          detail: { accessToken: result.access, refreshToken: result.refresh },
        }),
      );
      return result.access;
    } catch {
      return null;
    } finally {
      // Clear after a microtask so concurrent 401s that were waiting
      // on this refresh all resolve with the same value first.
      Promise.resolve().then(() => {
        refreshInFlight = null;
      });
    }
  })();
  return refreshInFlight;
}

// Hard-logout behaviour used by the primary hook. Components doing
// background polling (BuildQueueRow) do NOT want to redirect the whole
// page on a transient auth error — they pass `hardLogoutOnFailure:
// false` and just get the failing Response back. The hook passes
// `true` to preserve the existing session-expired UX for user-initiated
// actions.
export interface AuthFetchOptions extends RequestInit {
  hardLogoutOnFailure?: boolean;
}

// Cross-tab logout (GSEC R-077). Tokens live in sessionStorage now, which is
// per-tab and never fires 'storage' events in sibling tabs — so a logout in
// one WB tab would leave every other WB tab silently authenticated. The tab
// that logs out (or the Dashboard-driven identity sentinel) calls this to tell
// its siblings; each tab clears its own tokens and re-bounces.
export const AUTH_LOGOUT_EVENT = "mod:auth:logout";
export const AUTH_LOGOUT_CHANNEL = "mod-auth-sync";

let _logoutChannel: BroadcastChannel | null = null;
function _getLogoutChannel(): BroadcastChannel | null {
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") {
    return null;
  }
  if (!_logoutChannel) {
    _logoutChannel = new BroadcastChannel(AUTH_LOGOUT_CHANNEL);
  }
  return _logoutChannel;
}

// Tell every OTHER tab we just logged out. Siblings that listen on the channel
// (or fall back to a localStorage storage-event sentinel for browsers without
// BroadcastChannel) will clear their tokens and go to the logged-out state.
export function broadcastLogout(): void {
  if (typeof window === "undefined") return;
  const ch = _getLogoutChannel();
  if (ch) {
    try {
      ch.postMessage({ type: "logout" });
    } catch {
      /* channel closed — fall through to the storage fallback */
    }
  }
  // Storage-event fallback: localStorage DOES fire cross-tab 'storage' events,
  // so writing a sentinel reaches tabs on browsers lacking BroadcastChannel.
  try {
    const stamp = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem("mod_wb_logout_sentinel", stamp);
  } catch {
    /* best effort */
  }
}

function hardLogout() {
  stopProactiveTokenRefresh(); // no scheduled refresh survives a logout
  try {
    sessionStorage.clear();
    localStorage.removeItem("accessToken");
    localStorage.removeItem("refreshToken");
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  } catch {
    /* best effort */
  }
  broadcastLogout();
  window.location.href = DASHBOARD_URL;
}

// If the caller didn't set Authorization, inject the latest token.
// If they did, leave their header — but still replace it on retry
// with the fresh token so the retry isn't re-using the dead one.
function withAuthHeader(init: RequestInit, token: string | null): RequestInit {
  if (!token) return init;
  const headers = new Headers(init.headers as HeadersInit | undefined);
  if (!headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return { ...init, headers };
}

function withReplacedAuthHeader(init: RequestInit, token: string): RequestInit {
  const headers = new Headers(init.headers as HeadersInit | undefined);
  headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

// Track whether we've already dispatched a connection-lost event so
// the banner stays persistent until recovery — a second soft failure
// shouldn't spam multiple events, and a retry succeeding should emit
// exactly one restored event.
let _connectionLostDispatched = false;

function _emitConnectionLost(reason: string) {
  if (_connectionLostDispatched) return;
  _connectionLostDispatched = true;
  try {
    window.dispatchEvent(
      new CustomEvent(AUTH_CONNECTION_LOST_EVENT, { detail: { reason } }),
    );
  } catch {
    /* best effort */
  }
}

function _emitConnectionRestored() {
  if (!_connectionLostDispatched) return;
  _connectionLostDispatched = false;
  try {
    window.dispatchEvent(new CustomEvent(AUTH_CONNECTION_RESTORED_EVENT));
  } catch {
    /* best effort */
  }
}

// Public entry: marks the control the user just clicked busy for the whole
// call (the 401 refresh-retry inside counts as ONE request); released in
// finally so a failure or throw never leaves the control stuck.
export async function authenticatedFetch(
  url: string,
  init: AuthFetchOptions = {},
): Promise<Response> {
  const busyToken = attachBusyRequest(url);
  try {
    return await _authenticatedFetchInner(url, init);
  } finally {
    releaseBusyRequest(busyToken);
  }
}

async function _authenticatedFetchInner(
  url: string,
  init: AuthFetchOptions = {},
): Promise<Response> {
  const { hardLogoutOnFailure = false, ...fetchInit } = init;

  // Network-level failures (server down, DNS blip, browser offline)
  // surface as a thrown Error, not a 401 Response. Treat those the
  // same as a soft auth failure: emit connection-lost so the UI
  // shows a banner instead of an uncaught error.
  let first: Response;
  try {
    first = await fetch(url, withAuthHeader(fetchInit, getAccessToken()));
  } catch (err) {
    if (!hardLogoutOnFailure) {
      _emitConnectionLost(`network: ${(err as Error)?.message || String(err)}`);
    }
    throw err;
  }

  if (first.status !== 401) {
    // Successful response (any 2xx/3xx/4xx other than 401). If we had
    // previously flagged connection-lost, this is proof the API is
    // reachable again — clear the banner.
    _emitConnectionRestored();
    return first;
  }

  const fresh = await tryRefreshToken();
  if (fresh) {
    // Silent refresh worked — retry with the new token. A successful
    // retry also restores the connection state.
    const retried = await fetch(url, withReplacedAuthHeader(fetchInit, fresh));
    if (retried.ok) _emitConnectionRestored();
    return retried;
  }

  if (hardLogoutOnFailure) {
    // User-initiated actions (Save, Execute) still hard-redirect on
    // full auth failure — the wizard's "connection lost" UX is for
    // background polls, not explicit user actions.
    hardLogout();
    throw new Error("Session expired");
  }
  // Soft path: emit connection-lost so the WorkflowBuilder banner
  // appears, then return the 401 so the caller can stay on the page.
  _emitConnectionLost("401 after refresh");
  return first;
}

// ---------------------------------------------------------------------
// Stream tickets (WS5.34b) — short-lived, single-use tokens for
// EventSource connections, which can't send an Authorization header.
// Callers mint a fresh one via normal Bearer auth (this goes through
// authenticatedFetch, so silent token-refresh still applies) immediately
// before EACH `new EventSource(...)` — a ticket is single-use, so it
// cannot be reused across reconnects. Returns null on any failure
// (network, non-2xx, malformed body); callers should fall back to
// polling rather than opening an EventSource with no ticket.
// ---------------------------------------------------------------------
export type StreamTicketScope = "notifications" | "wizard-run";

export async function mintStreamTicket(
  apiUrl: string,
  scope: StreamTicketScope,
  runId?: string,
): Promise<string | null> {
  try {
    const resp = await authenticatedFetch(
      `${apiUrl.replace(/\/+$/, "")}/stream-tickets`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          scope === "wizard-run" ? { scope, run_id: runId } : { scope },
        ),
      },
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    return typeof data?.ticket === "string" ? data.ticket : null;
  } catch {
    return null;
  }
}
