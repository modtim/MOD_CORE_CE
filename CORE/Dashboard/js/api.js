// In Dashboard/js/api.js

// PKCE-over-plain-HTTP fix: crypto.subtle is undefined in insecure contexts
// (http://<LAN-IP>), so the S256 challenge threw on every LAN install. auth.js
// uses the pkce-crypto.js module; api.js CANNOT import it (vm tests parse api.js
// as a classic script — ES `import` and dynamic `import()` both break there), so
// the same pure-JS SHA-256 fallback is carried inline below (copy, do not
// cross-import — same pattern as WorkflowBuilder/lib/pkce-crypto.ts).

const API_BASE_URL = window.APP_CONFIG?.API_URL || "http://api.localhost";

// A custom error class to provide more context on API failures.
class ApiError extends Error {
    constructor(message, status, headers, detail) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.headers = headers;
        // Preserve the parsed `detail` payload so structured 4xx
        // responses (e.g., the rename/move name_collision modal)
        // can read the {code, existing, target_folder} object the
        // server returned without re-parsing the message string.
        this.detail = detail;
    }
}

// Flag to prevent multiple concurrent refresh attempts
let isRefreshing = false;
let refreshPromise = null;

// Attempt to refresh the access token via the BFF endpoint at
// `${API_BASE_URL}/auth/refresh-token`. Direct browser-to-Keycloak
// refresh CANNOT work for this realm: dashboard-client is a confidential
// client (publicClient=false, requires client_secret), and the secret
// must never live in browser code. The BFF endpoint proxies the refresh
// server-side with the secret attached.
//
// This used to call Keycloak directly without the secret — Keycloak
// returned 401 on every refresh attempt, which the 401-retry path in
// fetchApi() interpreted as "session dead" and logged the user out.
// Combined with the laptop-sleep / tab-throttle case (proactive timer
// in auth.js stalls, access token expires, first wake-up request 401s),
// users were getting bounced to the login screen well before the
// real Keycloak SSO session expired.
// CPU4-UI-REPRO 11-25: status of the last failed refresh. A 429 means the
// per-IP refresh-token bucket is momentarily full (another tab or the Builder
// hammered it); the session is still valid, so it must NOT log the user out.
let _lastRefreshStatus = 0;

async function refreshAccessToken() {
    _lastRefreshStatus = 0;
    const refreshToken = sessionStorage.getItem("refreshToken");
    if (!refreshToken) {
        console.warn('No refresh token available');
        return null;
    }

    const CLIENT_ID = window.APP_CONFIG?.KEYCLOAK_CLIENT_ID || 'dashboard-client';

    try {
        console.log('Attempting to refresh access token via BFF...');
        const response = await fetch(`${API_BASE_URL}/auth/refresh-token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                refresh_token: refreshToken,
                client_id: CLIENT_ID,
            }),
        });

        if (!response.ok) {
            console.warn('Token refresh failed:', response.status);
            _lastRefreshStatus = response.status;
            return null;
        }

        const data = await response.json();
        console.log('Access token refreshed successfully');

        sessionStorage.setItem('accessToken', data.access_token);
        if (data.refresh_token) {
            sessionStorage.setItem('refreshToken', data.refresh_token);
        }

        return data.access_token;
    } catch (error) {
        console.error('Error refreshing token:', error);
        return null;
    }
}

// Helper to perform logout
function performLogout() {
    sessionStorage.clear();
    const KEYCLOAK_URL = window.APP_CONFIG?.KEYCLOAK_URL || 'http://keycloak.localhost';
    const KEYCLOAK_REALM = window.APP_CONFIG?.KEYCLOAK_REALM || 'modtex';
    const params = new URLSearchParams({
        client_id: window.APP_CONFIG?.KEYCLOAK_CLIENT_ID || 'dashboard-client',
        post_logout_redirect_uri: window.location.origin
    });
    window.location.href = `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/logout?${params.toString()}`;
}

// GY.C4b — build a fresh-login Keycloak URL (prompt=login, max_age=N) and
// redirect the browser to it. Mirrors auth.js getLoginUrl() (same PKCE
// setup so the return flow exchanges cleanly) plus the OIDC params that
// force Keycloak to re-verify credentials even though a session exists:
//   prompt=login   -> always show the login form (ignores SSO session)
//   max_age=N      -> only accept a login newer than N seconds (KC applies
//                     it against the session's auth time)
// Pure-JS SHA-256 (RFC 6234) for insecure contexts. Copy of
// Dashboard/js/pkce-crypto.js — api.js is loaded as a CLASSIC script by the
// vm-based Dashboard tests, so it cannot `import` the module (ES `import` and
// dynamic `import()` are both SyntaxError/undefined there). Kept byte-identical
// so the S256 challenge matches crypto.subtle exactly.
const _PKCE_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
function _pkceRotr(x, n) { return (x >>> n) | (x << (32 - n)); }
// UTF-8 encode without TextEncoder (vm sandboxes lack it; PKCE verifiers are
// ASCII but keep full UTF-8 for correctness). Same bytes as TextEncoder.
function _pkceBytes(str) {
    if (typeof TextEncoder === 'function') return new TextEncoder().encode(str);
    const out = [];
    for (let i = 0; i < str.length; i++) {
        let cp = str.charCodeAt(i);
        if (cp >= 0xd800 && cp <= 0xdbff && i + 1 < str.length
            && str.charCodeAt(i + 1) >= 0xdc00 && str.charCodeAt(i + 1) <= 0xdfff) {
            cp = 0x10000 + ((cp - 0xd800) << 10) + (str.charCodeAt(++i) - 0xdc00);
        }
        if (cp < 0x80) out.push(cp);
        else if (cp < 0x800) out.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
        else if (cp < 0x10000) out.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
        else out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
    }
    return Uint8Array.from(out);
}

function _sha256Digest(bytes) {
    const subtle = globalThis.crypto?.subtle;
    if (subtle) {
        try { return subtle.digest('SHA-256', bytes); } catch (_) { /* fall through */ }
    }
    const b = Uint8Array.from(bytes);
    const len = b.length;
    const buf = new Uint8Array((((len + 8) >> 6) + 1) << 6);
    for (let i = 0; i < len; i++) buf[i] = b[i];
    buf[len] = 0x80;
    const dv = new DataView(buf.buffer);
    dv.setUint32(buf.length - 8, (len >>> 29) & 0xffffffff);
    dv.setUint32(buf.length - 4, (len << 3) & 0xffffffff);
    let h0 = 0x6a09e667, h1 = 0xbb67ae85, h2 = 0x3c6ef372, h3 = 0xa54ff53a;
    let h4 = 0x510e527f, h5 = 0x9b05688c, h6 = 0x1f83d9ab, h7 = 0x5be0cd19;
    const w = new Uint32Array(64);
    const rotr = _pkceRotr;
    for (let off = 0; off < buf.length; off += 64) {
        for (let t = 0; t < 16; t++) w[t] = dv.getUint32(off + (t << 2));
        for (let t = 16; t < 64; t++) {
            const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
            const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
            w[t] = (w[t - 16] + s1 + w[t - 7] + s0) & 0xffffffff;
        }
        let a = h0, bb = h1, c = h2, d = h3, e = h4, f = h5, g = h6, hh = h7;
        for (let t = 0; t < 64; t++) {
            const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            const ch = (e & f) ^ (~e & g);
            const t1 = (hh + S1 + ch + _PKCE_K[t] + w[t]) & 0xffffffff;
            const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            const maj = (a & bb) ^ (a & c) ^ (bb & c);
            const t2 = (S0 + maj) & 0xffffffff;
            hh = g; g = f; f = e;
            e = (d + t1) & 0xffffffff; d = c; c = bb; bb = a;
            a = (t1 + t2) & 0xffffffff;
        }
        h0 = (h0 + a) & 0xffffffff; h1 = (h1 + bb) & 0xffffffff;
        h2 = (h2 + c) & 0xffffffff; h3 = (h3 + d) & 0xffffffff;
        h4 = (h4 + e) & 0xffffffff; h5 = (h5 + f) & 0xffffffff;
        h6 = (h6 + g) & 0xffffffff; h7 = (h7 + hh) & 0xffffffff;
    }
    const out = new Uint8Array(32);
    const odv = new DataView(out.buffer);
    [h0, h1, h2, h3, h4, h5, h6, h7].forEach((v, i) => odv.setUint32(i << 2, v));
    return out;
}

function redirectToReauth(maxAgeSeconds) {
    let maxAge = Number(maxAgeSeconds) || 0;
    // Inline construction (no cross-module import — api.js is loaded first):
    const KEYCLOAK_URL = window.APP_CONFIG?.KEYCLOAK_URL || 'http://keycloak.localhost';
    const KEYCLOAK_REALM = window.APP_CONFIG?.KEYCLOAK_REALM || 'modtex';
    const CLIENT_ID = window.APP_CONFIG?.KEYCLOAK_CLIENT_ID || 'dashboard-client';
    const REDIRECT_URI = window.location.origin + window.location.pathname;
    const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    let codeChallenge = '';
    let codeVerifier = '';
    try {
        codeVerifier = b64url(crypto.getRandomValues(new Uint8Array(64)));
        // crypto.subtle is undefined in insecure contexts (plain http on a LAN
        // IP) — _sha256Digest uses it when present, pure JS otherwise. jsdom
        // has no TextEncoder, so encode with the same manual UTF-8 helper.
        Promise.resolve(_sha256Digest(_pkceBytes(codeVerifier)))
            .then((digest) => {
                codeChallenge = b64url(digest);
                const params = new URLSearchParams({
                    client_id: CLIENT_ID,
                    redirect_uri: REDIRECT_URI,
                    response_type: 'code',
                    scope: 'openid profile email groups',  // 2400LR-b1j: no offline_access (ends the KC SSO session)
                    prompt: 'login',
                });
                if (maxAge > 0) params.set('max_age', String(maxAge));
                params.set('code_challenge', codeChallenge);
                params.set('code_challenge_method', 'S256');
                try {
                    // api.js's exchange path reads the single-use verifier.
                    localStorage.setItem('pkce_code_verifier', codeVerifier);
                } catch (_) { /* storage unavailable */ }
                window.location.assign(
                    `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/auth?${params.toString()}`
                );
            });
    } catch (e) {
        console.error('reauth redirect setup failed', e);
    }
}

// A centralized fetch function to handle authentication and error handling.
// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE (11-17) ONE hook:
// attach this request to the control the user just clicked (js/busy-
// indicator.js remembers the last clicked interactive element; a request
// starting within 150 ms of a click marks it busy until this call returns,
// also on failure - the release is in `finally`). The 401-refresh-retry
// recursion below passes isRetry=true, so it never re-attaches: the retry
// counts as the ONE request already attached.
async function fetchApi(endpoint, options = {}, isRetry = false) {
    let modBusyEl = null;
    if (!isRetry) modBusyEl = window.ModBusyIndicator?.attachRequest();
    try {
    const token = sessionStorage.getItem("accessToken");
    const headers = {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
        ...options.headers,
    };

    const response = await fetch(`${API_BASE_URL}${endpoint}`, { ...options, headers });

    if (response.status === 204) return null;

    // GY.C4b — re-authentication for privileged actions. The API answers a
    // STALE login (token auth_time older than auth.privileged_reauth_max_age_s)
    // with 401 {"error": "reauth_required", "reauth_max_age_s": N} on the
    // privileged routes. That is NOT a dead session — the refresh token is
    // still valid, so we must NOT take the refresh/logout path above. Instead
    // bounce the browser through Keycloak with prompt=login&max_age=N (which
    // forces a fresh credential check) and let the user retry the action.
    // Recognised here in the ONE central place every Dashboard call passes
    // through, so no individual caller special-cases the 401.
    if (response.status === 401) {
        let _detail = null;
        try { _detail = (await response.clone().json()).detail; } catch (_) { /* non-JSON */ }
        if (_detail && typeof _detail === 'object' && _detail.error === 'reauth_required') {
            console.warn('Privileged action needs a fresh login (reauth_required). Redirecting to Keycloak...');
            redirectToReauth(_detail.reauth_max_age_s || 0);
            throw new ApiError('A recent login is required for this action. Redirecting to re-authenticate...', 401, response.headers, _detail);
        }
    }

    // Handle authentication errors - try to refresh token first
    if (response.status === 401 && !isRetry) {
        console.warn('Access token expired. Attempting refresh...');

        // Prevent multiple concurrent refresh attempts
        if (!isRefreshing) {
            isRefreshing = true;
            refreshPromise = refreshAccessToken().finally(() => {
                isRefreshing = false;
                refreshPromise = null;
            });
        }

        const newToken = await refreshPromise;

        if (newToken) {
            // Retry the original request with the new token
            console.log('Retrying request with refreshed token...');
            return fetchApi(endpoint, options, true);
        } else {
            // Rate-limited refresh (429): keep the session, ask for a retry.
            if (_lastRefreshStatus === 429) {
                console.warn('Token refresh rate-limited (429); keeping the session.');
                throw new ApiError('Too many requests. Please wait a minute and try again.', 429, response.headers);
            }
            // Refresh failed - log out
            console.warn('Token refresh failed. Redirecting to logout...');
            performLogout();
            throw new ApiError('Session expired. Redirecting to login...', 401, response.headers);
        }
    }

    // If retry also failed or refresh didn't work, log out
    if (response.status === 401) {
        console.warn('Authentication failed after retry. Redirecting to logout...');
        performLogout();
        throw new ApiError('Session expired. Redirecting to login...', 401, response.headers);
    }

    if (!response.ok) {
        let errorMessage = `API request failed with status ${response.status}`;
        let detail = null;
        try {
            const errorData = await response.json();
            detail = errorData.detail;
            if (Array.isArray(detail)) {
                errorMessage = detail.map(err => `${err.msg} (in ${err.loc[1]})`).join(', ');
            } else if (typeof detail === 'object' && detail !== null) {
                // Structured 4xx (e.g., name_collision) — keep the dict
                // on .detail and surface its message for legacy
                // string-only consumers.
                errorMessage = detail.message || JSON.stringify(detail);
            } else if (detail) {
                errorMessage = detail;
            }
        } catch (e) {
            errorMessage = response.statusText;
        }
        throw new ApiError(errorMessage, response.status, response.headers, detail);
    }

    const contentType = response.headers.get("content-type");
    if (contentType && contentType.includes("application/json")) {
        return response.json();
    }
    return null;
    } finally {
        // FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE: release the clicked
        // control's busy state when this call returns, success OR failure.
        if (modBusyEl) window.ModBusyIndicator?.releaseRequest(modBusyEl);
    }
}


export async function exchangeCodeForToken(code, redirectUri) {
    const payload = {
        code,
        redirect_uri: redirectUri,
        // Verifier is in localStorage (shared across tabs on the same origin)
        // so email-verification / new-tab callbacks still find it.
        code_verifier: localStorage.getItem('pkce_code_verifier'),
        client_id: window.APP_CONFIG?.KEYCLOAK_CLIENT_ID || 'dashboard-client',
    };

    const response = await fetch(`${API_BASE_URL}/auth/exchange-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    // Tim bug (11-06): the login 403s from /auth/exchange-code (JIT seat
    // limit, unknown IdP domain, MFA enrollment gate) carry a structured
    // `detail` {code, message, metadata} the login page must show. The bare
    // `new Error(...)` here threw the status AND the detail away, so a 403
    // was indistinguishable from a dead session and the user saw a silent
    // logout bounce. Parse the body ONCE (a fetch body can only be read once)
    // and hand the ApiError its `.status`/`.detail` — the same class every
    // other route already throws. Non-JSON bodies keep the generic string.
    let errorData = null;
    let bodyParsed = false;
    try {
        errorData = await response.json();
        bodyParsed = true;
    } catch {
        // non-JSON body — fall back to the generic message below
    }
    if (!response.ok) {
        let detail = (bodyParsed && errorData && typeof errorData === 'object')
            ? errorData.detail
            : null;
        if (detail === undefined) detail = null;
        let errorMessage = 'Failed to exchange code for token';
        if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message) {
            errorMessage = detail.message;
        } else if (typeof detail === 'string' && detail) {
            errorMessage = detail;
        }
        throw new ApiError(errorMessage, response.status, response.headers, detail);
    }
    if (!bodyParsed) throw new ApiError('Failed to exchange code for token', response.status, response.headers, null);
    return errorData;
}

// /me and /me/permissions are called by ~13 dashboard JS files on every
// page nav (auth.js, ui.js, analytics.js, settings.js, …). Each call
// hits 6+ DB queries server-side, and the result almost never changes
// within a single page-load burst. Cache them client-side for 30s with
// request-coalescing — multiple parallel callers in the same tick share
// one in-flight promise instead of each firing their own HTTP request.
//
// Mutations that change the cached state (profile update, role change,
// onboarding completion, sign-out) must call `me.invalidate()` so the
// next read refetches.
const _meCache = {
    profile: null,
    profileFetchedAt: 0,
    profileInFlight: null,
    permissions: null,
    permissionsFetchedAt: 0,
    permissionsInFlight: null,
};
const ME_TTL_MS = 30 * 1000;

function _meCacheGet(kind) {
    const now = Date.now();
    if (kind === 'profile') {
        if (_meCache.profile && (now - _meCache.profileFetchedAt) < ME_TTL_MS) {
            return Promise.resolve(_meCache.profile);
        }
        if (_meCache.profileInFlight) return _meCache.profileInFlight;
        _meCache.profileInFlight = fetchApi('/me').then((data) => {
            _meCache.profile = data;
            _meCache.profileFetchedAt = Date.now();
            _meCache.profileInFlight = null;
            return data;
        }).catch((err) => {
            _meCache.profileInFlight = null;
            throw err;
        });
        return _meCache.profileInFlight;
    }
    // permissions
    if (_meCache.permissions && (now - _meCache.permissionsFetchedAt) < ME_TTL_MS) {
        return Promise.resolve(_meCache.permissions);
    }
    if (_meCache.permissionsInFlight) return _meCache.permissionsInFlight;
    _meCache.permissionsInFlight = fetchApi('/me/permissions').then((data) => {
        _meCache.permissions = data;
        _meCache.permissionsFetchedAt = Date.now();
        _meCache.permissionsInFlight = null;
        return data;
    }).catch((err) => {
        _meCache.permissionsInFlight = null;
        throw err;
    });
    return _meCache.permissionsInFlight;
}

// /platform/features is a PUBLIC (no-auth) endpoint hit by ui.js on every
// page nav (billing-nav-link gating + the header credit indicator) plus the
// usage / billing / users surfaces. Its payload only changes on a server
// redeploy, so coalesce + cache it client-side for 30s exactly like /me so
// it isn't refetched on every nav. Plain fetch (NOT fetchApi): the endpoint
// needs no token and must never trip fetchApi's 401 -> logout path.
const _featuresCache = { data: null, fetchedAt: 0, inFlight: null };
const FEATURES_TTL_MS = 30 * 1000;

export function getPlatformFeatures() {
    const now = Date.now();
    if (_featuresCache.data && (now - _featuresCache.fetchedAt) < FEATURES_TTL_MS) {
        return Promise.resolve(_featuresCache.data);
    }
    if (_featuresCache.inFlight) return _featuresCache.inFlight;
    _featuresCache.inFlight = fetch(`${API_BASE_URL}/platform/features`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((data) => {
            _featuresCache.data = data;
            _featuresCache.fetchedAt = Date.now();
            _featuresCache.inFlight = null;
            return data;
        })
        .catch((err) => {
            _featuresCache.inFlight = null;
            throw err;
        });
    return _featuresCache.inFlight;
}

// 2400UI15 — CE edition predicate for the shell's edition-gated API calls. Reads
// the SAME signal ui.js already uses (_isCeEdition, ui.js:282): the
// entitlements.edition field of the coalesced 30s /platform/features cache
// above (main.py derives it from the edition provider). true ONLY when the payload
// POSITIVELY reports a paid edition ("saas"/"pro"); false for CE AND for unknown
// (rc20 failopen-c: the old shape was fail-open — `!(edition === 'ce' && !error)`
// resolved TRUE for an errored payload, and main.py's entitlements FAILURE stub
// (CORE/API/main.py:1530) reports edition "ce" + error "unresolved", so a
// transient entitlements failure UNLOCKED every paid-only surface: billing,
// plans, coupons, settings AI config, workers, users, admin-*, ~90 call sites).
// Unknown now gates like CE (the surfaces 402/404 on CE anyway, so hiding is the
// safe direction); a successful paid payload keeps every call and its timing
// unchanged. Same fail-closed shape as ui.js's _registrationAllowed (ui.js:2373
// positive-edition test). Lives here rather than in ui.js because api.js
// imports nothing: analytics.js and settings.js can read it without adding a
// ui.js <-> them import cycle.
export async function isPaidEdition() {
    try {
        const features = await getPlatformFeatures();
        const ent = features && features.entitlements;
        if (!ent || ent.error) return false;
        return ent.edition === 'saas' || ent.edition === 'pro';
    } catch {
        return false;
    }
}

export const me = {
    get: () => _meCacheGet('profile'),
    getPermissions: () => _meCacheGet('permissions'),
    // activation-lifecycle-emails: lifecycle (getting-started) email opt-out.
    getEmailPreferences: () => fetchApi('/me/email-preferences'),
    setEmailPreferences: (optedOut) => fetchApi('/me/email-preferences', {
        method: 'PUT',
        body: JSON.stringify({ lifecycle_emails_opted_out: !!optedOut }),
    }),
    // notification-preferences (WS4.Q22): per-category (workflow/billing/
    // product), per-channel (in_app/email) delivery toggles. GET returns
    // every category with both channels (missing rows default true). PUT
    // takes a partial body, e.g. { workflow: { in_app: false } }.
    getNotificationPreferences: () => fetchApi('/me/notification-preferences'),
    setNotificationPreferences: (updates) => fetchApi('/me/notification-preferences', {
        method: 'PUT',
        body: JSON.stringify(updates),
    }),
    // --- My Account controls ---
    // Profile name fields (PUT /me/profile → ProfileUpdate {first_name, last_name}).
    updateProfile: (firstName, lastName) => fetchApi('/me/profile', {
        method: 'PUT',
        body: JSON.stringify({ first_name: firstName, last_name: lastName }),
    }),
    // Username (PATCH /me → UserProfileUpdate {username}).
    updateUsername: (username) => fetchApi('/me', {
        method: 'PATCH',
        body: JSON.stringify({ username }),
    }),
    // GY.C4.MFA — 2FA status for the signed-in user (GET /me/mfa). Returns
    // {enrolled, credential_created_at, device_label, required, exempt,
    // setup_url}. setup_url is the MOD-branded enrollment link (Keycloak
    // application-initiated action on the modtech login theme) — never the
    // Keycloak account console. No secret is ever returned.
    getMfa: () => fetchApi('/me/mfa'),
    // Active sessions (GET returns array of {id, device_info, ip_address,
    // last_active_at, created_at}).
    getSessions: () => fetchApi('/me/sessions'),
    // Log out every other session, keeping the current one
    // (DELETE /me/sessions → {revoked, kept_session_id}).
    revokeOtherSessions: () => fetchApi('/me/sessions', { method: 'DELETE' }),
    // Revoke a single session by id.
    revokeSession: (sessionId) => fetchApi(`/me/sessions/${sessionId}`, { method: 'DELETE' }),
    // GDPR Art. 20 — personal-data export (returns a JSON document).
    exportData: () => fetchApi('/me/data-export'),
    // GDPR Art. 17 — request account deletion / anonymization
    // (DELETE /me/account → {status, message, ...}).
    deleteAccount: () => fetchApi('/me/account', { method: 'DELETE' }),
    // Force the next .get() / .getPermissions() to refetch. Call after
    // profile updates, role changes, onboarding completion, sign-out.
    invalidate: () => {
        _meCache.profile = null;
        _meCache.profileFetchedAt = 0;
        _meCache.permissions = null;
        _meCache.permissionsFetchedAt = 0;
    },
};

export const cogs = {
    get: async (parentId = null, includePermissions = false, opts = {}) => {
        const base = [];
        // deleted-files-filter (#4): opts.deleted lists the tenant's
        // soft-deleted cogs (GET /cogs/?deleted=true) instead of a folder's
        // live contents. It is a distinct view, not scoped to a parent folder.
        if (opts.deleted) {
            base.push('deleted=true');
        } else if (parentId) {
            base.push(`parent_id=${parentId}`);
        }
        if (includePermissions) base.push('include_permission_info=true');
        const baseQs = base.length ? `?${base.join('&')}` : '';
        let offset = 0;
        const results = [];
        for (let i = 0; i < 20; i++) {
            const qs = (baseQs ? baseQs + '&' : '?') + `limit=50&offset=${offset}`;
            const page = await fetchApi(`/assets/${qs}`);
            if (!page || page.length === 0) break;
            results.push(...page);
            if (page.length < 50) break;
            offset += 50;
        }
        return results;
    },
    // deleted-files-filter (#4): restore a soft-deleted cog. After it resolves
    // the file returns to the normal (non-deleted) listing.
    restore: (cogId) => fetchApi(`/assets/${cogId}/restore`, { method: 'POST' }),
    search: (query) => fetchApi(`/assets/search/recursive?q=${encodeURIComponent(query)}`),
    // The tenant's root MASTER_FOLDER ("Home") — get-or-create. The Data page
    // navigates into this so files/asset-folders live in a real, visible root.
    getRoot: () => fetchApi('/assets/root'),
    create: (data) => fetchApi('/assets/', { method: 'POST', body: JSON.stringify(data) }),
    update: (cogId, data) => fetchApi(`/assets/${cogId}`, { method: 'PATCH', body: JSON.stringify(data) }),
    delete: (cogId) => fetchApi(`/assets/${cogId}`, { method: 'DELETE' }),
    copy: (cogId, destinationParentId, opts = {}) => fetchApi(
        `/assets/${cogId}/copy`,
        {
            method: 'POST',
            body: JSON.stringify({
                destination_parent_id: destinationParentId,
                ...(opts.new_name ? { new_name: opts.new_name } : {}),
                ...(opts.replace ? { replace: true } : {}),
            }),
        },
    ),
};

// Tenant tags + per-cog tag attachment.
//   listForCog → tags currently attached to a cog
//   listAll    → every tag defined for the tenant (used to resolve an
//                existing tag id when create returns 409 duplicate)
//   create     → create a new tenant tag (409 if the name already exists)
//   attach/detach → link/unlink a tag id to/from a cog
export const tags = {
    listForCog: (cogId) => fetchApi(`/assets/${cogId}/tags`),
    listAll: (limit = 500) => fetchApi(`/tags/?limit=${limit}`),
    create: (name) => fetchApi('/tags/', { method: 'POST', body: JSON.stringify({ name }) }),
    attach: (cogId, tagId) => fetchApi(`/assets/${cogId}/tags/${tagId}`, { method: 'POST' }),
    detach: (cogId, tagId) => fetchApi(`/assets/${cogId}/tags/${tagId}`, { method: 'DELETE' }),
};

// Resource locks. For cogs, target_type is "cog".
//   getStatus     → the active lock, or null when unlocked (backend 404 → null)
//   acquire       → take the lock (409 if already held); optional reason note
//   renew         → extend the lock the current user holds (403 if not holder)
//   release       → drop the lock (403 if not held by the current user)
//   forceRelease  → admin/owner override-release someone else's lock (audited)
export const locks = {
    getStatus: async (targetType, targetId) => {
        try {
            return await fetchApi(`/locks/${targetType}/${targetId}`);
        } catch (e) {
            if (e && e.status === 404) return null;
            throw e;
        }
    },
    acquire: (targetType, targetId, reason = null) => fetchApi('/locks/', {
        method: 'POST',
        body: JSON.stringify({ target_type: targetType, target_id: targetId, ...(reason ? { reason } : {}) }),
    }),
    renew: (targetType, targetId, opts = {}) => fetchApi(`/locks/${targetType}/${targetId}/renew`, {
        method: 'POST',
        body: JSON.stringify({
            ...(opts.reason != null ? { reason: opts.reason } : {}),
            ...(opts.ttlMinutes != null ? { ttl_minutes: opts.ttlMinutes } : {}),
        }),
    }),
    release: (targetType, targetId) => fetchApi(`/locks/${targetType}/${targetId}`, { method: 'DELETE' }),
    forceRelease: (targetType, targetId) => fetchApi(`/locks/${targetType}/${targetId}/force`, { method: 'DELETE' }),
};

export const users = {
    get: (tenantId = null) => fetchApi(`/users/${tenantId ? `?tenant_id=${tenantId}` : ''}`),
    deactivate: (userId) => fetchApi(`/users/${userId}/deactivate`, { method: 'PATCH' }),
    reactivate: (userId) => fetchApi(`/users/${userId}/reactivate`, { method: 'PATCH' }),
    delete: (userId) => fetchApi(`/users/${userId}`, { method: 'DELETE' }),
    // GY.C4.MFA — admin 2FA reset (POST /admin/users/{id}/mfa/reset). Removes
    // the target's authenticator in Keycloak and queues re-enrollment on the
    // MOD-branded login page; audited (auth.mfa_reset) + admin notification.
    // Exempt service accounts are refused with 409.
    resetMfa: (userId, reason) => fetchApi(`/admin/users/${userId}/mfa/reset`, {
        method: 'POST',
        body: JSON.stringify({ reason: reason || '' }),
    }),
    // BUG.B5b2 — identity status for the admin users page.
    identityStatus: (userId) => fetchApi(
        `/admin/users/${userId}/identity/status`,
    ),
    // BUG.B5b2 — relink a disconnected identity.
    relinkIdentity: (userId, reason) => fetchApi(
        `/admin/users/${userId}/identity/relink`, {
            method: 'POST',
            body: JSON.stringify({ reason, confirm: true }),
        }),
    // GSEC R-096 — platform-admin locked-account (brute-force) listing.
    lockedAccounts: () => fetchApi(`/admin/security/locked-accounts`),
    // GSEC R-096 — platform-admin unlock (clears the KC brute-force state).
    unlockAccount: (keycloakUserId, reason) => fetchApi(
        `/admin/security/locked-accounts/${keycloakUserId}`, {
            method: 'DELETE',
            body: JSON.stringify({ reason: reason || '' }),
        }),
    // FX.106 — platform-admin rate-limit auto-block listing
    // (active blocks + recent 429/block/unblock activity).
    rateLimitBlocks: () => fetchApi(`/admin/security/rate-limit/blocks`),
    // FX.106 — platform-admin early unblock of an auto-blocked client IP
    // (audited). ip must be URL-safe; the endpoint returns 404 when the IP
    // is not currently blocked on this API process.
    unblockRateLimitIp: (ip, reason) => fetchApi(
        `/admin/security/rate-limit/blocks/${encodeURIComponent(ip)}` +
        (reason ? `?reason=${encodeURIComponent(reason)}` : ''), {
            method: 'DELETE',
        }),
    // Assign a (system or custom) role to an existing user. A platform admin
    // may target a user in any tenant (the tenant selector); the role must
    // live in that user's tenant.
    assignRole: (userId, roleId) =>
        fetchApi(`/rbac/users/${userId}/assign-role?role_id=${roleId}`, { method: 'PUT' }),
    // Tenant roles (system + custom) for the assign dropdown. tenantId is
    // honoured only for platform admins; others always get their own tenant.
    roles: (tenantId = null) =>
        fetchApi(`/rbac/roles${tenantId ? `?tenant_id=${tenantId}` : ''}`),
};

// clean-v2-api identity wave: moved from bare /invitations to
// /users/invitations (design.md — "users (+ invitations as
// users/invitations)"). Same backend handlers, only the path changed.
export const invitations = {
    get: (tenantId = null) => fetchApi(`/users/invitations/${tenantId ? `?tenant_id=${tenantId}` : ''}`),
    create: (data) => fetchApi('/users/invitations/', { method: 'POST', body: JSON.stringify(data) }),
    cancel: (invitationId) => fetchApi(`/users/invitations/${invitationId}`, { method: 'DELETE' }),
};

// External invitations — invite someone OUTSIDE the tenant to a single
// folder (cog) at viewer/editor level. create() returns an
// `invitation_token`; SMTP is off, so the UI surfaces a copyable accept
// link (/accept-invite?token=<token>) that the invitee opens to
// provision their account. The accept + by-token lookups are PUBLIC
// (token-authed) and therefore call fetch() directly rather than
// fetchApi() — the invitee has no session/Bearer token yet.
export const externalInvites = {
    // Owner-side management (session-authed).
    listPending: () => fetchApi('/external-invitations/pending'),
    create: (data) => fetchApi('/external-invitations/', { method: 'POST', body: JSON.stringify(data) }),
    cancel: (invitationId) => fetchApi(`/external-invitations/${invitationId}`, { method: 'DELETE' }),

    // Public accept flow (no auth — the token IS the credential).
    byToken: async (token) => {
        const resp = await fetch(`${API_BASE_URL}/public/external-invitations/by-token/${encodeURIComponent(token)}`, {
            headers: { 'Content-Type': 'application/json' },
        });
        if (!resp.ok) {
            let detail = resp.statusText;
            try { detail = (await resp.json()).detail || detail; } catch (_) { /* non-JSON */ }
            throw new ApiError(detail, resp.status, resp.headers);
        }
        return resp.json();
    },
    accept: async (invitationId, body) => {
        const resp = await fetch(`${API_BASE_URL}/public/external-invitations/${invitationId}/accept`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            let detail = resp.statusText;
            try { detail = (await resp.json()).detail || detail; } catch (_) { /* non-JSON */ }
            throw new ApiError(detail, resp.status, resp.headers);
        }
        return resp.json();
    },

    // The link an owner hands to an invitee. Absolute so it survives a
    // copy-paste out of the dashboard into email/chat.
    acceptLink: (token) => `${window.location.origin}/accept-invite?token=${encodeURIComponent(token)}`,
};

export const secrets = {
    get: () => fetchApi('/secrets/'),
    create: (data) => fetchApi('/secrets/', { method: 'POST', body: JSON.stringify(data) }),
    delete: (secretId) => fetchApi(`/secrets/${secretId}`, { method: 'DELETE' }),
};

// Outbound webhook endpoints (WebhookEndpoint). Fired on workflow completion by
// services/outbound_webhooks.py. Trailing slash on list/create is deliberate —
// the router mounts POST/GET at "/webhooks/", so a slash-less call would 307.
export const webhooks = {
    list: () => fetchApi('/webhooks/'),
    create: (data) => fetchApi('/webhooks/', { method: 'POST', body: JSON.stringify(data) }),
    remove: (id) => fetchApi(`/webhooks/${id}`, { method: 'DELETE' }),
};

// remove-minio-versitygw Gate 5 — per-tenant BYO-S3 storage connections.
// The secret key is write-only: sent on create/update, NEVER returned by list/get.
export const storageConnections = {
    list: () => fetchApi('/storage-connections/'),
    get: (id) => fetchApi(`/storage-connections/${id}`),
    create: (data) => fetchApi('/storage-connections/', { method: 'POST', body: JSON.stringify(data) }),
    update: (id, data) => fetchApi(`/storage-connections/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    remove: (id) => fetchApi(`/storage-connections/${id}`, { method: 'DELETE' }),
    test: (id) => fetchApi(`/storage-connections/${id}/test`, { method: 'POST' }),
    testConfig: (data) => fetchApi('/storage-connections/test-config', { method: 'POST', body: JSON.stringify(data) }),
    // opts.skipMissingSource === true opts in to ?skip_missing_source=true (copy
    // what exists, report the objects already gone). Default stays fail-closed:
    // no query param, so the server stops at the first missing source object.
    activate: (id, opts) => fetchApi(
        `/storage-connections/${id}/activate${opts && opts.skipMissingSource === true ? '?skip_missing_source=true' : ''}`,
        { method: 'POST' }),
    deactivate: (id) => fetchApi(`/storage-connections/${id}/deactivate`, { method: 'POST' }),
    migrationStatus: (id) => fetchApi(`/storage-connections/${id}/migration-status`),
    // IMPL-BYO-MOVE-OUT-C-DASHBOARD — the move-out routes (design
    // results-1110/DESIGN-BYO-MOVE-OUT-FLOW.md s3, IMPL-A final shapes). The
    // router refuses `target` + `target_connection_id` together, so a
    // conn-to-conn move sends ONLY target_connection_id (the server's `target`
    // default then stays unset) and a MOD-storage move sends ONLY target.
    moveOut: (id, opts) => fetchApi(
        `/storage-connections/${id}/move-out`,
        { method: 'POST', body: JSON.stringify(opts && opts.target_connection_id != null
            ? { target_connection_id: opts.target_connection_id }
            : { target: 'mod_storage' }) }),
    moveStatus: (id) => fetchApi(`/storage-connections/${id}/move-status`),
    moveOutCancel: (id) => fetchApi(`/storage-connections/${id}/move-out/cancel`, { method: 'POST' }),
    corsRequirements: () => fetchApi('/storage-connections/cors-requirements'),
};

// megaplan 25.3 Q.5b — tenant storage usage (used/quota/breakdown) +
// platform-admin fleet view. Read-only, no mutation.
export const tenantStorage = {
    get: () => fetchApi('/tenant/storage'),
};

export const adminStorage = {
    fleet: () => fetchApi('/admin/storage/fleet'),
};

// GSEC R-120 — tenant-level DSAR (org-scoped export + two-step erasure).
// Backend: routers/tenant/dsar.py (mounted /v1/tenant/dsar/*). Tenant-admin
// or platform-admin only; the erasure confirm token is returned ONCE and is
// never logged or persisted. Any active legal hold refuses erasure with
// 409; the export is unaffected by holds.
export const tenantDsar = {
    // GDPR Art. 20 at the org level: every member's personal export plus
    // the tenant-level records manifest. tenantId is honoured only for
    // platform admins; tenant admins are always scoped to their own tenant.
    exportBundle: (tenantId = null) =>
        fetchApi(`/tenant/dsar/export${tenantId ? `?tenant_id=${tenantId}` : ''}`),
    // Step 1 of tenant erasure (Art. 17): creates the pending request and
    // returns {status, request_id, token, tenant_id, confirm_after, message}.
    requestErasure: (tenantId = null) =>
        fetchApi('/tenant/dsar/erasure', {
            method: 'POST',
            body: JSON.stringify(tenantId ? { tenant_id: tenantId } : {}),
        }),
    // Step 2: single-use confirm token + enforced delay (privacy.
    // tenant_erasure_confirm_delay_minutes platform setting).
    confirmErasure: (requestId, token) =>
        fetchApi(`/tenant/dsar/erasure/${requestId}/confirm`, {
            method: 'POST',
            body: JSON.stringify({ token }),
        }),
    // BUG.B12b2 — same two-step erasure flow, but for ONE person (GDPR Art. 17
    // right to be forgotten for an individual). Step 1 returns
    // {status, request_id, token, user_id, confirm_after, message}; 409 = a
    // legal hold covers that user, 403/404 = not allowed / unknown user.
    requestUserErasure: (userId) =>
        fetchApi(`/tenant/dsar/users/${userId}/erasure`, {
            method: 'POST',
            body: JSON.stringify({}),
        }),
    // Step 2 for the person flow: same single-use token + delay contract.
    confirmUserErasure: (userId, requestId, token) =>
        fetchApi(`/tenant/dsar/users/${userId}/erasure/${requestId}/confirm`, {
            method: 'POST',
            body: JSON.stringify({ token }),
        }),
};

// FX.42 — platform DSAR request tracker (platform admin only).
// Backend: routers/dsar_tracker.py (/v1/platform/dsar-requests/*).
export const platformDsarRequests = {
    list: (params = {}) => {
        const qs = new URLSearchParams(
            Object.entries(params).filter(([, v]) => v !== null && v !== '')
        ).toString();
        return fetchApi(`/platform/dsar-requests${qs ? `?${qs}` : ''}`);
    },
    get: (requestId) =>
        fetchApi(`/platform/dsar-requests/${requestId}`),
    create: ({ requestType, requesterName, requesterEmail,
               userId, tenantId } = {}) =>
        fetchApi('/platform/dsar-requests', {
            method: 'POST',
            body: JSON.stringify({
                request_type: requestType,
                requester_name: requesterName,
                requester_email: requesterEmail || null,
                user_id: userId || null,
                tenant_id: tenantId || null,
            }),
        }),
    acknowledge: (requestId) =>
        fetchApi(`/platform/dsar-requests/${requestId}/acknowledge`,
                 { method: 'POST' }),
    extend: (requestId, reason, noticeSent = false) =>
        fetchApi(`/platform/dsar-requests/${requestId}/extend`, {
            method: 'POST',
            body: JSON.stringify({ reason, notice_sent: !!noticeSent }),
        }),
    close: (requestId, note = '') =>
        fetchApi(`/platform/dsar-requests/${requestId}/close`, {
            method: 'POST',
            body: JSON.stringify({ note }),
        }),
};

// GSEC FX.50 p3b — tenant-facing security notices (breach notifications).
// Backend: routers/tenant/security_notices.py (GET /v1/tenant/security-notices),
// tenant-admin only. Items carry ONLY id / summary / notified_at — the
// severity, deadline clocks and follow-up ledger stay on the platform-admin
// surface (routers/platform_security_incidents.py). ui.js renders summary
// through esc(), so incident text can never inject markup.
export const securityNotices = {
    list: () => fetchApi('/tenant/security-notices'),
};

// GY.C10 — access-review campaigns + SoD conflict report.
// Backend: routers/tenant/access_reviews.py (/v1/tenant/access-reviews/*),
// tenant-admin only. Revoking an item goes through the existing audited
// permission-revocation path and REQUIRES a comment.
export const accessReviews = {
    listCampaigns: () => fetchApi('/tenant/access-reviews/campaigns'),
    get: (campaignId) => fetchApi(`/tenant/access-reviews/campaigns/${campaignId}`),
    create: ({ title, scope, schedule_due_at } = {}) =>
        fetchApi('/tenant/access-reviews/campaigns', {
            method: 'POST',
            body: JSON.stringify({
                title,
                scope,
                ...(schedule_due_at ? { schedule_due_at } : {}),
            }),
        }),
    approveItem: (campaignId, itemId, comment = null) =>
        fetchApi(`/tenant/access-reviews/campaigns/${campaignId}/items/${itemId}/approve`, {
            method: 'POST',
            body: JSON.stringify(comment ? { comment } : {}),
        }),
    revokeItem: (campaignId, itemId, comment) =>
        fetchApi(`/tenant/access-reviews/campaigns/${campaignId}/items/${itemId}/revoke`, {
            method: 'POST',
            body: JSON.stringify({ comment }),
        }),
    close: (campaignId, comment = null) =>
        fetchApi(`/tenant/access-reviews/campaigns/${campaignId}/close`, {
            method: 'POST',
            body: JSON.stringify(comment ? { comment } : {}),
        }),
    sodReport: () => fetchApi('/tenant/access-reviews/sod-report'),
};

// GY.C1 — compliance profiles (platform admin). Backend:
// routers/compliance_profiles.py (/v1/platform/compliance/*). The two GETs
// are also served to time-boxed auditors (read-only); the PUT is
// platform-admin only. update() never changes any setting itself — the
// response's weaker_settings lists what is below the profiles' required
// values and must be applied through the normal audited settings path
// (or the GY.C0 preview->apply flow once it lands).
export const complianceProfiles = {
    list: () => fetchApi('/platform/compliance/profiles'),
    status: () => fetchApi('/platform/compliance/status'),
    update: (profiles) =>
        fetchApi('/platform/compliance/profiles', {
            method: 'PUT',
            body: JSON.stringify({ profiles }),
        }),
};

// GSEC FX.157.p3b — host hardening results (platform admin only, read-only).
// Mirrors the admin router at /admin/hardening-results.
export const hardeningResults = {
    list: () => fetchApi('/admin/hardening-results'),
    forHost: (host) => fetchApi(`/admin/hardening-results/${encodeURIComponent(host)}`),
};

// FX.143.p3a — capacity report + 90-day forecast (platform admin only).
// FX.165.p2c — task wait times (queued → claimed → started, p50/p90 per queue).
export const capacityReport = {
    report: (month) => fetchApi(month ? `/admin/capacity-report?month=${encodeURIComponent(month)}` : '/admin/capacity-report'),
    forecast: () => fetchApi('/admin/capacity-forecast'),
    taskWaitMetrics: (sinceHours = 24) => fetchApi(`/admin/task-wait-metrics?since_hours=${encodeURIComponent(sinceHours)}`),
};

// wsip 6.9 (UI slice 1) — adversary findings operator view (platform admin
// only, 403 otherwise). Mirrors the admin router at /admin/adversary-findings.
// Filters: tenant_id, target_workflow_id, min_severity, since, limit (<=200),
// offset. Empty filter values are skipped; URLSearchParams encodes the rest.
const afQuery = (params) => {
    const allowed = ['tenant_id', 'target_workflow_id', 'min_severity', 'since', 'limit', 'offset'];
    const q = new URLSearchParams();
    allowed.forEach((key) => {
        const value = params?.[key];
        if (value === null || value === undefined || value === '') return;
        q.append(key, value);
    });
    return q.toString() ? `?${q.toString()}` : '';
};

export const adversaryFindings = {
    list: (params = {}) => fetchApi(`/admin/adversary-findings${afQuery(params)}`),
    summary: (params = {}) => fetchApi(`/admin/adversary-findings/summary${afQuery(params)}`),
    get: (id) => fetchApi(`/admin/adversary-findings/${encodeURIComponent(id)}`),
};

// WSIP 7.6 (UI slice 1) — operator proposals (human review of what the
// autonomous operator proposed). Mirrors CORE/API/routers/autonomous_proposals.py
// at /road/autonomous-proposals (mounted under /v1 by the lead).
// list filters: status (exact, in the migration's CHECK vocabulary), workflow_id,
// limit (<=100), offset. While wsip.autonomous_operator_propose_enabled is OFF
// for the caller's tenant every route answers 404 {"error": "feature_disabled"}
// (404, not 403: the surface does not exist) — operator-proposals.js turns that
// into the "not enabled for this tenant" state. review() never deploys: 8.x
// (canary + promote) is what turns an approved proposal into a version; a row
// already decided answers 409 {"error": "not_reviewable"}.
const opQuery = (params) => {
    const allowed = ['status', 'workflow_id', 'limit', 'offset'];
    const q = new URLSearchParams();
    allowed.forEach((key) => {
        const value = params?.[key];
        if (value === null || value === undefined || value === '') return;
        q.append(key, value);
    });
    return q.toString() ? `?${q.toString()}` : '';
};

export const autonomousProposals = {
    list: (params = {}) => fetchApi(`/road/autonomous-proposals${opQuery(params)}`),
    get: (id) => fetchApi(`/road/autonomous-proposals/${encodeURIComponent(id)}`),
    review: (id, body = {}) => fetchApi(
        `/road/autonomous-proposals/${encodeURIComponent(id)}/review`,
        { method: 'POST', body: JSON.stringify(body) }),
};

// NB2 UI — per-workflow priority class. Mirrors the PUT/GET pair the
// NB2 API brief shipped (mounted at /v1/road/{workflow_id}/priority-
// class, same shape as the placement pair). The classes order runs
// held at the capacity limit (interactive > high > normal > low) and
// route interactive tasks through the lightweight placement; they do
// NOT make an ordinary run start faster.
// classes order runs held at the capacity limit (interactive > high >
// normal > low) and route interactive tasks through the lightweight
// placement; they do NOT make an ordinary run start faster.
export const workflowPriority = {
    get: (workflowId) => fetchApi(`/road/${encodeURIComponent(workflowId)}/priority-class`),
    set: (workflowId, priorityClass) => fetchApi(
        `/road/${encodeURIComponent(workflowId)}/priority-class`,
        { method: 'PUT', body: JSON.stringify({ priority_class: priorityClass }) }),
    clear: (workflowId) => fetchApi(
        `/road/${encodeURIComponent(workflowId)}/priority-class`,
        { method: 'PUT', body: JSON.stringify({ clear: true }) }),
};

// BUG.B69b — per-setting PATCH from the Compliance page controls.
// Mirrors platform-settings.js:175 pattern (platform admin gate,
// 409 when a compliance profile locks the key).
export const platformSettings = {
    patch: (key, value) => fetchApi(
        `/platform/settings/${encodeURIComponent(key)}`,
        { method: 'PATCH', body: JSON.stringify({ value }) }),
};

export const permissions = {
    listForCog: (cogId) => fetchApi(`/permissions/cogs/${cogId}`),
    listInherited: (cogId) => fetchApi(`/permissions/cogs/${cogId}/inherited`),
    getMyPermission: (cogId) => fetchApi(`/permissions/my-permissions/cogs/${cogId}`),
    grant: (cogId, data) => fetchApi(`/permissions/cogs/${cogId}`, { method: 'POST', body: JSON.stringify(data) }),
    update: (permId, data) => fetchApi(`/permissions/${permId}`, { method: 'PATCH', body: JSON.stringify(data) }),
    revoke: (permId) => fetchApi(`/permissions/${permId}`, { method: 'DELETE' }),
};

export const permissionRequests = {
    create: (data) => fetchApi('/permission-requests', { method: 'POST', body: JSON.stringify(data) }),
    myRequests: (status = null) => fetchApi(`/permission-requests/my-requests${status ? `?status_filter=${status}` : ''}`),
    pending: () => fetchApi('/permission-requests/pending'),
    review: (requestId, data) => fetchApi(`/permission-requests/${requestId}/review`, { method: 'POST', body: JSON.stringify(data) }),
    cancel: (requestId) => fetchApi(`/permission-requests/${requestId}`, { method: 'DELETE' }),
};

export const apiKeys = {
    get: () => fetchApi('/api-keys/'),
    create: (data) => fetchApi('/api-keys/', { method: 'POST', body: JSON.stringify(data) }),
    revoke: (apiKeyId) => fetchApi(`/api-keys/${apiKeyId}`, { method: 'DELETE' }),
    // FX.152 p4 — POST /api-keys/{id}/rotate; the new key is returned exactly once.
    rotate: (apiKeyId) => fetchApi(`/api-keys/${apiKeyId}/rotate`, { method: 'POST' }),
    // Token exchange is a public endpoint (no Bearer token needed)
    exchangeForToken: async (apiKey) => {
        const response = await fetch(`${API_BASE_URL}/api-keys/token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: apiKey }),
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Token exchange failed');
        }
        return response.json();
    },
};

// 2400GX-cUI-4 (30.6.15) — per-stack DEPLOY tokens (c3-J3: routers/persistent_stacks/
// deploy_tokens.py:41 mint / :83 list / :96 revoke). Same request helper every
// stack call uses — fetchApi (api.js:137), which persistent-stacks.js:9 imports as
// apiCall. The mint 201 carries the plaintext `token` exactly once; the GET never
// returns it (stack_deploy_token.py:142-157 returns key_prefix only).
export const listStackDeployTokens = (stackId) => fetchApi(`/stacks/${stackId}/deploy-tokens`);
export const createStackDeployToken = (stackId, body) => fetchApi(
    `/stacks/${stackId}/deploy-tokens`, { method: 'POST', body: JSON.stringify(body) });
export const revokeStackDeployToken = (stackId, tokenId) => fetchApi(
    `/stacks/${stackId}/deploy-tokens/${tokenId}`, { method: 'DELETE' });

// 2400GX-cUI-5 (30.6.15 / 30.6.4) — stack SEED upload (external snapshot) wrappers,
// next to the deploy-token block above. Routes: routers/persistent_stacks/stack_seed.py
// :66 register (202 SeedRegister -> {seed_id,part_bytes,uploads[]}) / :82 parts window
// (SeedPartWindow{part,from_part,count<=100} -> {part,urls[]}) / :100 commit
// (SeedCommit{parts} -> {snapshot_id}, 409 seed_already_committed). Logic:
// services/stack_seed.py:180 register_seed / :257 part_urls / :289 commit_seed.
// Same request helper every stack call uses — fetchApi (api.js:137).
export const registerStackSeed = (stackId, body) => fetchApi(
    `/stacks/${stackId}/seed`, { method: 'POST', body: JSON.stringify(body) });
export const getStackSeedPartUrls = (stackId, seedId, body) => fetchApi(
    `/stacks/${stackId}/seed/${encodeURIComponent(seedId)}/parts`,
    { method: 'POST', body: JSON.stringify(body) });
export const commitStackSeed = (stackId, seedId, body) => fetchApi(
    `/stacks/${stackId}/seed/${encodeURIComponent(seedId)}/commit`,
    { method: 'POST', body: JSON.stringify(body) });

// The presigned part PUT must NOT go through fetchApi: fetchApi attaches the
// Dashboard's Authorization bearer and prefixes API_BASE_URL, and neither may
// ever be sent to the storage host (the URL's own signature is the only
// credential; a bearer to a third-party origin leaks the session). NO headers
// at all — a Content-Type header would make the browser preflight, and
// upload_part signs no content-type (services/stack_seed.py:280-284 adds only
// uploadId + partNumber to the query). The URL stays a parameter: never module
// state, never storage, never the DOM.
export async function putSeedPart(url, blob) {
    return await fetch(url, { method: 'PUT', body: blob });
}

// GH.8b - the tenant's OWN cloud provider accounts that MOD provisions GPU
// workers into.
// Backed by routers/burst_accounts.py (/v1/burst/accounts/*, tenant-admin
// only) plus the GH.8a sessions/ledger/summary read endpoints, which may not
// be deployed yet — callers should treat a 404 from those three as "not
// available yet", not an error.
export const burstAccounts = {
    // Provider credential catalog for the Connect form (metadata only —
    // never any secret value). Each entry: {name, label, region_hint,
    // fields: [{key, label, secret, required, multiline, help, list?}]}.
    providers: () => fetchApi('/burst/providers'),
    list: () => fetchApi('/burst/accounts'),
    create: (data) => fetchApi('/burst/accounts', { method: 'POST', body: JSON.stringify(data) }),
    update: (id, data) => fetchApi(`/burst/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
    remove: (id) => fetchApi(`/burst/accounts/${id}`, { method: 'DELETE' }),
    probe: (id) => fetchApi(`/burst/accounts/${id}/probe`, { method: 'POST' }),

    // GH.8a — live workers + fee ledger. `status` is 'active' or 'all'.
    sessions: (status = 'active', limit = 50) =>
        fetchApi(`/burst/sessions?status=${status}&limit=${limit}`),
    ledger: (limit = 100) => fetchApi(`/burst/ledger?limit=${limit}`),
    summary: () => fetchApi('/burst/summary'),
};

export const notifications = {
    getEmailConfig: () => fetchApi('/notifications/email-config/'),
    updateEmailConfig: (data) => fetchApi('/notifications/email-config/', { method: 'POST', body: JSON.stringify(data) }),
    testEmailConfig: (data) => fetchApi('/notifications/test-email/', { method: 'POST', body: JSON.stringify(data) }),
};

// PG-14 (megaplan 1113): the `destinations` export that used to sit here was
// dead — no caller anywhere in the repo and no `/destinations/` router behind
// it. SIEM export destinations are the GSEC FX.77 p6a `logStreams` export
// below (api.js) against routers/tenant/log_streams.py (/tenant/log-streams*).

// Web Worker for SHA-256 hashing — keeps the UI thread responsive
// while we hash the file before the dedupe check (#20). Native
// crypto.subtle.digest doesn't support progressive update, so we
// hand the whole File to the worker and let it pull bytes via
// File.arrayBuffer() inside the worker thread.
//
// Files larger than HASH_DEDUP_MAX_BYTES skip the pre-flight
// dedupe and go straight to upload — no memory blow-up.
const HASH_DEDUP_MAX_BYTES = 500 * 1024 * 1024;  // 500 MB
const _SHA256_WORKER_SRC = `
self.onmessage = async (e) => {
    try {
        const buf = await e.data.file.arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', buf);
        const hex = Array.from(new Uint8Array(digest))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('');
        self.postMessage({ ok: true, hex });
    } catch (err) {
        self.postMessage({ ok: false, error: String(err && err.message || err) });
    }
};
`;
let _sha256Worker = null;
// crypto.randomUUID() only exists in secure contexts (HTTPS / localhost), so
// an on-prem install served over plain http on a LAN IP threw here and every
// upload failed. getRandomValues() is available in any context.
function _uuid4() {
    if (globalThis.crypto?.randomUUID) {
        try { return crypto.randomUUID(); } catch { /* non-secure context */ }
    }
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function _getSha256Worker() {
    if (!_sha256Worker) {
        const blob = new Blob([_SHA256_WORKER_SRC], { type: 'application/javascript' });
        _sha256Worker = new Worker(URL.createObjectURL(blob));
    }
    return _sha256Worker;
}
/**
 * Hash a File with SHA-256 in a Web Worker. Returns the hex digest
 * or null if the file is too large for a pre-flight dedupe (caller
 * falls back to standard upload). Errors propagate as null too —
 * dedupe is best-effort, never blocks an upload.
 */
async function hashFileSha256(file) {
    if (!file || file.size > HASH_DEDUP_MAX_BYTES) return null;
    if (typeof Worker === 'undefined' || !crypto?.subtle) return null;
    return new Promise((resolve) => {
        const w = _getSha256Worker();
        const onMsg = (e) => {
            w.removeEventListener('message', onMsg);
            resolve(e.data && e.data.ok ? e.data.hex : null);
        };
        w.addEventListener('message', onMsg);
        try {
            w.postMessage({ file });
        } catch {
            w.removeEventListener('message', onMsg);
            resolve(null);
        }
    });
}

export const files = {
    // opts: { replace?: bool, name?: string } — bug-2 collision handling. The
    // caller (files.js) retries with name (rename) or replace=true after the
    // backend returns a 409 name_collision and the resolve modal is answered.
    upload: async (file, parentId, onUploadProgress, opts = {}) => {
        const token = sessionStorage.getItem("accessToken");
        const MULTIPART_THRESHOLD = 100 * 1024 * 1024; // 100MB — use multipart above this

        // Pre-flight dedupe (#20): if the tenant already has this
        // content, link a new file record and skip the byte transfer
        // entirely. Best-effort — any failure path falls through to
        // the standard upload.
        try {
            const dedupResult = await files._tryDedupByHash(
                file, parentId, token, onUploadProgress, opts,
            );
            if (dedupResult) return dedupResult;
        } catch (e) {
            // A name collision must reach the caller's resolve modal — don't
            // swallow it as a "dedup miss". Other dedup failures fall through.
            if (e && e.status === 409) throw e;
            console.debug('Dedupe pre-flight failed (non-fatal):', e);
        }

        if (file.size > MULTIPART_THRESHOLD) {
            return await files._multipartUpload(file, parentId, onUploadProgress, token, opts);
        } else {
            return await files._simpleUpload(file, parentId, onUploadProgress, token, opts);
        }
    },

    // Hash the file in a worker, ask the API if it already has the
    // content, and on a hit create a new file record linked to the
    // existing blob — bytes never re-transfer. Returns the new cog
    // on a hit, null on a miss / unsupported / too-large file.
    _tryDedupByHash: async (file, parentId, token, onUploadProgress, opts = {}) => {
        const hex = await hashFileSha256(file);
        if (!hex) return null;
        const checkParams = new URLSearchParams({
            content_hash: hex,
            size: String(file.size),
        });
        const checkResp = await fetch(
            `${API_BASE_URL}/assets/uploads/check?${checkParams}`,
            { method: 'POST', headers: { 'Authorization': `Bearer ${token}` } },
        );
        if (!checkResp.ok) return null;
        const check = await checkResp.json();
        if (!check.exists) return null;

        // We have a hit — create the file record server-side without
        // any byte transfer. Surface 100% progress for UX parity
        // with the upload flow.
        if (typeof onUploadProgress === 'function') {
            try { onUploadProgress({ loaded: file.size, total: file.size, lengthComputable: true }); } catch {}
        }

        const linkParams = new URLSearchParams({
            filename: opts.name || file.name,
            content_hash: hex,
            content_type: file.type || 'application/octet-stream',
        });
        if (parentId) linkParams.set('parent_id', String(parentId));
        if (opts.replace) linkParams.set('replace', 'true');
        const linkResp = await fetch(
            `${API_BASE_URL}/assets/uploads/from-hash?${linkParams}`,
            { method: 'POST', headers: { 'Authorization': `Bearer ${token}` } },
        );
        // A 409 name-collision must NOT be swallowed as "dedup miss" — surface it
        // so the caller's resolve modal handles it (rename/replace). Other
        // failures fall through to the standard upload path (return null).
        if (linkResp.status === 409) {
            const body = await linkResp.json().catch(() => ({}));
            throw new ApiError(
                (body.detail && body.detail.message) || 'Name collision',
                409, null, body.detail,
            );
        }
        if (!linkResp.ok) return null;
        const cog = await linkResp.json();
        // Tag the response so callers can show "deduped" feedback.
        cog._deduped = true;
        return cog;
    },

    // Simple presigned PUT for files under 100MB
    _simpleUpload: async (file, parentId, onUploadProgress, token, opts = {}) => {
        const tempUploadId = _uuid4();
        const uploadName = opts.name || file.name;

        const params = new URLSearchParams({
            filename: uploadName,
            upload_id: tempUploadId, file_size: file.size,
            content_type: file.type || 'application/octet-stream',
        });
        if (parentId) params.set('parent_id', parentId);

        const reqResp = await fetch(`${API_BASE_URL}/assets/request-upload?${params}`, {
            method: 'POST', headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!reqResp.ok) throw new ApiError((await reqResp.json().catch(() => ({}))).detail || 'Upload request failed', reqResp.status, null);
        const info = await reqResp.json();

        if (info.upload_url) {
            await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open('PUT', info.upload_url, true);
                xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
                xhr.upload.onprogress = onUploadProgress;
                xhr.onload = () => xhr.status < 300 ? resolve() : reject(new ApiError('S3 upload failed', xhr.status, null));
                xhr.onerror = () => reject(new ApiError('Network error', 0, null));
                xhr.send(file);
            });
        }

        const confirmParams = new URLSearchParams({
            filename: uploadName,
            upload_id: tempUploadId, file_size: file.size,
            content_type: file.type || 'application/octet-stream',
        });
        if (parentId) confirmParams.set('parent_id', parentId);
        if (opts.replace) confirmParams.set('replace', 'true');
        const confirmResp = await fetch(`${API_BASE_URL}/assets/confirm-upload?${confirmParams}`, {
            method: 'POST', headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!confirmResp.ok) {
            // Pass the structured detail as the 4th arg so isNameCollisionError
            // / resolveCollision can read the 409 name_collision payload.
            const body = await confirmResp.json().catch(() => ({}));
            const msg = typeof body.detail === 'string'
                ? body.detail : (body.detail && body.detail.message) || 'Confirm failed';
            throw new ApiError(msg, confirmResp.status, null, body.detail);
        }
        const confirmData = await confirmResp.json();

        // Poll for worker task completion (hashing + registration)
        if (confirmData.task_id) {
            for (let i = 0; i < 600; i++) {
                await new Promise(r => setTimeout(r, 2000));
                try {
                    const statusResp = await fetch(`${API_BASE_URL}/workers/tasks/${confirmData.task_id}/status`, {
                        headers: { 'Authorization': `Bearer ${token}` },
                    });
                    if (statusResp.ok) {
                        const s = await statusResp.json();
                        const st = (s.status || '').toUpperCase();
                        if (st === 'COMPLETED' || st === 'SUCCESS') return s;
                        if (st === 'FAILED' || st === 'FAILURE') throw new ApiError('File processing failed: ' + (s.error_message || ''), 500, null);
                    }
                } catch (e) { if (e instanceof ApiError) throw e; }
            }
            throw new ApiError('File processing timed out', 504, null);
        }
        return confirmData;
    },

    // Multipart upload for files over 100MB — supports resume, parallel chunks
    _multipartUpload: async (file, parentId, onUploadProgress, token, opts = {}) => {
        const PART_SIZE = 64 * 1024 * 1024; // 64MB chunks
        const MAX_CONCURRENT = 4; // Parallel uploads
        const uploadName = opts.name || file.name;

        // Step 1: Initiate multipart upload
        const initParams = new URLSearchParams({
            filename: uploadName,
            file_size: file.size, content_type: file.type || 'application/octet-stream',
            part_size: PART_SIZE,
        });
        if (parentId) initParams.set('parent_id', parentId);
        const initResp = await fetch(`${API_BASE_URL}/assets/uploads/initiate?${initParams}`, {
            method: 'POST', headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!initResp.ok) throw new ApiError((await initResp.json().catch(() => ({}))).detail || 'Initiate failed', initResp.status, null);
        const uploadInfo = await initResp.json();

        // Save to localStorage for resume support
        const resumeKey = `upload_${file.name}_${file.size}`;
        localStorage.setItem(resumeKey, JSON.stringify({
            upload_id: uploadInfo.upload_id,
            s3_upload_id: uploadInfo.s3_upload_id,
            part_urls: uploadInfo.part_urls,
            filename: file.name,
            parent_id: parentId,
            file_size: file.size,
            content_type: file.type,
            completed_parts: [],
        }));

        // Step 2: Upload parts with parallel execution + retry
        const partSize = uploadInfo.part_size;
        const completedParts = [];
        let totalUploaded = 0;

        const uploadPart = async (partInfo, retries = 3) => {
            const partNum = partInfo.part_number;
            const start = (partNum - 1) * partSize;
            const end = Math.min(start + partSize, file.size);
            const blob = file.slice(start, end);

            for (let attempt = 0; attempt <= retries; attempt++) {
                try {
                    const resp = await fetch(partInfo.url, {
                        method: 'PUT',
                        body: blob,
                        headers: { 'Content-Type': file.type || 'application/octet-stream' },
                    });

                    if (!resp.ok) {
                        if (attempt === retries) throw new Error(`Part ${partNum} failed: HTTP ${resp.status}`);
                        await new Promise(r => setTimeout(r, 1000 * (attempt + 1))); // Backoff
                        continue;
                    }

                    const etag = resp.headers.get('ETag') || resp.headers.get('etag') || '';
                    totalUploaded += (end - start);

                    if (onUploadProgress) {
                        onUploadProgress({ loaded: totalUploaded, total: file.size, lengthComputable: true });
                    }

                    return { part_number: partNum, etag: etag.replace(/"/g, '') };
                } catch (e) {
                    if (attempt === retries) throw e;
                    await new Promise(r => setTimeout(r, 1000 * (attempt + 1)));
                }
            }
        };

        // Upload in batches of MAX_CONCURRENT
        const partUrls = uploadInfo.part_urls;
        for (let i = 0; i < partUrls.length; i += MAX_CONCURRENT) {
            const batch = partUrls.slice(i, i + MAX_CONCURRENT);
            const results = await Promise.all(batch.map(p => uploadPart(p)));
            completedParts.push(...results);

            // Update localStorage for resume
            const saved = JSON.parse(localStorage.getItem(resumeKey) || '{}');
            saved.completed_parts = completedParts;
            localStorage.setItem(resumeKey, JSON.stringify(saved));
        }

        // Step 3: Complete multipart upload
        const completeParams = new URLSearchParams({
            filename: uploadName,
            file_size: file.size, s3_upload_id: uploadInfo.s3_upload_id,
            content_type: file.type || 'application/octet-stream',
        });
        if (parentId) completeParams.set('parent_id', parentId);
        if (opts.replace) completeParams.set('replace', 'true');
        const completeResp = await fetch(`${API_BASE_URL}/assets/uploads/${uploadInfo.upload_id}/complete?${completeParams}`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(completedParts),
        });

        // Clean up localStorage
        localStorage.removeItem(resumeKey);

        if (!completeResp.ok) throw new ApiError((await completeResp.json().catch(() => ({}))).detail || 'Complete failed', completeResp.status, null);
        return await completeResp.json();
    },

    downloadMultiple: async (cogIds) => {
        const token = sessionStorage.getItem("accessToken");
        const response = await fetch(`${API_BASE_URL}/assets/download-multiple`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(cogIds)
        });

        if (!response.ok) {
            let errorMessage = `Download failed with status ${response.status}`;
            try {
                const errorData = await response.json();
                errorMessage = errorData.detail || errorMessage;
            } catch (e) {
                errorMessage = response.statusText;
            }
            throw new ApiError(errorMessage, response.status, response.headers);
        }

        const data = await response.json();

        // Download from presigned URL
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = data.download_url;
        a.download = data.filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    },

    download: async (cogId, isFolder = false) => {
        const token = sessionStorage.getItem("accessToken");
        const endpoint = isFolder ? `/assets/${cogId}/download-folder` : `/assets/${cogId}/download`;

        const response = await fetch(`${API_BASE_URL}${endpoint}`, {
            method: 'GET',
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (!response.ok) {
            let errorMessage = `Download failed with status ${response.status}`;
            try {
                const errorData = await response.json();
                errorMessage = errorData.detail || errorMessage;
            } catch (e) {
                errorMessage = response.statusText;
            }
            throw new ApiError(errorMessage, response.status, response.headers);
        }

        // Check if response is JSON (presigned URL) or binary (direct download)
        const contentType = response.headers.get('Content-Type');

        if (contentType && contentType.includes('application/json')) {
            // Large file or folder - get presigned URL
            const data = await response.json();

            // Download from presigned URL
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = data.download_url;
            a.download = data.filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        } else {
            // Small file - stream directly
            const contentDisposition = response.headers.get('Content-Disposition');
            let filename = 'download';
            if (contentDisposition) {
                const filenameMatch = contentDisposition.match(/filename="(.+)"/);
                if (filenameMatch) {
                    filename = filenameMatch[1];
                }
            }

            // Download the file
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
        }
    }
};

// FX.89 — tenant security contact (breach/subprocessor/SIEM notification
// target). Backend: routers/tenant/security_contact.py (GET gates on
// TENANT:view_settings, PUT on TENANT:manage_settings).
export const securityContact = {
    get: () => fetchApi('/tenant/security-contact'),
    update: (payload) => fetchApi('/tenant/security-contact', { method: 'PUT', body: JSON.stringify(payload) }),
};

// GSEC FX.77 p6a — tenant log streaming (SIEM export) destinations.
// Backend: routers/tenant/log_streams.py (/v1/tenant/log-streams*), GET
// gates on TENANT:view_settings, write routes on TENANT:manage_settings.
// `secret` is write-only (never returned; responses carry `has_secret`
// instead) — passing `secret: null` on update clears it.
export const logStreams = {
    list: () => fetchApi('/tenant/log-streams'),
    get: (id) => fetchApi(`/tenant/log-streams/${id}`),
    create: (payload) => fetchApi('/tenant/log-streams', { method: 'POST', body: JSON.stringify(payload) }),
    update: (id, payload) => fetchApi(`/tenant/log-streams/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
    remove: (id) => fetchApi(`/tenant/log-streams/${id}`, { method: 'DELETE' }),
    test: (id) => fetchApi(`/tenant/log-streams/${id}/test`, { method: 'POST' }),
};

// GSEC FX.153 p3 — staff register (MOD staff; platform admin only).
// Backend: routers/staff_register.py (/v1/platform/staff-records*).
// GET /due reports training-overdue + leaver-checklist-overdue plus
// staff.training_interval_days (the training cycle the page uses for
// its client-side due-within-30-days math).
export const staffRegister = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.include_deleted) q.set('include_deleted', 'true');
        if (opts.staff_type) q.set('staff_type', opts.staff_type);
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/platform/staff-records${qs ? `?${qs}` : ''}`);
    },
    create: (payload) => fetchApi('/platform/staff-records',
        { method: 'POST', body: JSON.stringify(payload) }),
    update: (recordId, payload) => fetchApi(
        `/platform/staff-records/${encodeURIComponent(recordId)}`,
        { method: 'PATCH', body: JSON.stringify(payload) }),
    softDelete: (recordId) => fetchApi(
        `/platform/staff-records/${encodeURIComponent(recordId)}/delete`,
        { method: 'POST' }),
    startLeaver: (recordId, leaverAt) => fetchApi(
        `/platform/staff-records/${encodeURIComponent(recordId)}/leaver`,
        { method: 'POST', body: JSON.stringify({ leaver_at: leaverAt || null }) }),
    due: () => fetchApi('/platform/staff-records/due'),
};

// FX.71 p2 — platform access-review campaigns (platform admin only).
// Backend: routers/admin_platform_access_review.py (/v1/admin/platform-access-reviews*).
// ensure is idempotent for THIS quarter; revoke REQUIRES a comment (the decision
// is recorded, nothing hard-deleted); close records the campaign sign-off.
export const platformAccessReview = {
    listCampaigns: () => fetchApi('/admin/platform-access-reviews/campaigns'),
    ensure: () => fetchApi('/admin/platform-access-reviews/campaigns/ensure',
        { method: 'POST' }),
    listItems: (campaignId) => fetchApi(
        `/admin/platform-access-reviews/campaigns/${encodeURIComponent(campaignId)}/items`),
    confirmItem: (campaignId, itemId, comment) => fetchApi(
        `/admin/platform-access-reviews/campaigns/${encodeURIComponent(campaignId)}/items/${encodeURIComponent(itemId)}/confirm`,
        { method: 'POST', body: JSON.stringify({ comment: comment }) }),
    revokeItem: (campaignId, itemId, comment) => fetchApi(
        `/admin/platform-access-reviews/campaigns/${encodeURIComponent(campaignId)}/items/${encodeURIComponent(itemId)}/revoke`,
        { method: 'POST', body: JSON.stringify({ comment: comment }) }),
    closeCampaign: (campaignId, comment) => fetchApi(
        `/admin/platform-access-reviews/campaigns/${encodeURIComponent(campaignId)}/close`,
        { method: 'POST', body: JSON.stringify({ comment: comment }) }),
};

// FX.125 p3 — platform key inventory (platform admin only, READ-ONLY).
// Backend: routers/admin_keys.py (GET /v1/admin/keys/inventory) ->
// services/key_inventory.py. Payload: {as_of, schedules: {keys.*}, summary,
// items: [{item, kind, setting, rotation_days, status, created_at, age_days,
// next_rotation_due, overdue, reason?}]} — every source fails soft, metadata
// only. Rotation jobs are part 2, so there is no rotate call here.
export const keyInventory = {
    get: () => fetchApi('/admin/keys/inventory'),
};

// GSEC FX.63 p3 — outbound-connections preview (platform admin only).
// Backend: routers/admin_telemetry.py (GET /v1/admin/telemetry/preview) ->
// services/telemetry_payload.py. Payload: {allowed: {telemetry.update_check,
// telemetry.usage_stats}, blocked_by_profile: str|null, payload: {version,
// edition, counts}}. set() writes through the EXISTING platform-settings path
// (PATCH /v1/platform/settings/{key}, routers/platform_settings.py — platform
// admin gate, 409 when a compliance profile locks the key). Nothing sends.
export const telemetryPreview = {
    get: () => fetchApi('/admin/telemetry/preview'),
    set: (key, value) => fetchApi(`/platform/settings/${encodeURIComponent(key)}`,
        { method: 'PATCH', body: JSON.stringify({ value }) }),
};

// GSEC FX.70 p3 — tenant support tickets (open + list, tenant-scoped).
// Backend: routers/tenant/support_tickets.py (/v1/tenant/support-tickets*).
// create body is exactly SupportTicketCreate: severity sev1|sev2|sev3|sev4,
// subject 3..255, body optional <=20000 (extra="forbid" -> send only these).
// list maps to the GET query params (status open|in_progress|resolved|closed,
// limit 1..200, offset >=0); every read is filtered to this tenant server-side.
export const supportTickets = {
    create: (payload) => fetchApi('/tenant/support-tickets',
        { method: 'POST', body: JSON.stringify(payload) }),
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.status) q.set('status', opts.status);
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/tenant/support-tickets${qs ? `?${qs}` : ''}`);
    },
};

// GSEC FX.93 p3 — abuse-report queue (platform admin only).
// Backend: routers/abuse_reports.py (/v1/admin/abuse/reports*).
// list maps to the GET query params (status, overdue_only, limit, offset);
// triage POSTs {action, note} with action in dismiss|suspend_target|actioned.
export const abuseQueue = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.status) q.set('status', opts.status);
        if (opts.overdue_only) q.set('overdue_only', 'true');
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/admin/abuse/reports${qs ? `?${qs}` : ''}`);
    },
    get: (reportId) => fetchApi(
        `/admin/abuse/reports/${encodeURIComponent(reportId)}`),
    triage: (reportId, action, note = null) => fetchApi(
        `/admin/abuse/reports/${encodeURIComponent(reportId)}/triage`,
        { method: 'POST',
          body: JSON.stringify({ action, note: note || null }) }),
};

// GSEC FX.95 p3 — marketplace store-page queue (platform admin only).
// Backend: routers/admin_listing_review.py (/v1/admin/store-page*).
// list maps to the GET query params (kind, review_status, limit, offset);
// approve/reject POST {kind} / {kind, reason} — kind is workflow|stack and is
// required (listing_id alone cannot disambiguate the two listing tables);
// reject's reason is mandatory server-side.
export const listingReview = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.kind) q.set('kind', opts.kind);
        if (opts.review_status) q.set('review_status', opts.review_status);
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/admin/store-page/queue${qs ? `?${qs}` : ''}`);
    },
    approve: (listingId, kind) => fetchApi(
        `/admin/store-page/${encodeURIComponent(listingId)}/approve`,
        { method: 'POST', body: JSON.stringify({ kind }) }),
    reject: (listingId, kind, reason) => fetchApi(
        `/admin/store-page/${encodeURIComponent(listingId)}/reject`,
        { method: 'POST', body: JSON.stringify({ kind, reason }) }),
};

// GSEC FX.50 p3a — security-incident tracker (platform admin only).
// Backend: routers/platform_security_incidents.py, mounted at /v1.
// list maps to the GET query params (status, limit, offset); create POSTs
// {tenant_id,severity,summary,detected_at}; confirm/close send optional
// bodies; follow-ups use the /follow-ups[/{id}/complete] subroutes.
export const securityIncidents = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.status) q.set('status', opts.status);
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/platform/security-incidents${qs ? `?${qs}` : ''}`);
    },
    get: (incidentId) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}`),
    create: (payload) => fetchApi('/platform/security-incidents',
        { method: 'POST', body: JSON.stringify(payload) }),
    confirm: (incidentId, confirmedAt = null) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/confirm`,
        { method: 'POST', body: JSON.stringify({ confirmed_at: confirmedAt }) }),
    followUps: (incidentId) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/follow-ups`),
    addFollowUp: (incidentId, payload) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/follow-ups`,
        { method: 'POST', body: JSON.stringify(payload) }),
    completeFollowUp: (incidentId, followUpId) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/follow-ups/${encodeURIComponent(followUpId)}/complete`,
        { method: 'POST' }),
    notify: (incidentId) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/notify`,
        { method: 'POST' }),
    close: (incidentId, note = null) => fetchApi(
        `/platform/security-incidents/${encodeURIComponent(incidentId)}/close`,
        { method: 'POST', body: JSON.stringify({ note: note }) }),
};

// GSEC FX.73 p2 (owner decision Q56=A) — licence manual-review queue
// (platform admin only). Backend: routers/platform_licence_review.py,
// mounted at /v1. list maps to the GET query params (status,
// subject_type, limit, offset); decide POSTs {commercial_use, spdx_id?,
// attribution_required?, note?}.
export const licenceReview = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.status) q.set('status', opts.status);
        if (opts.subject_type) q.set('subject_type', opts.subject_type);
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/platform/licence-review${qs ? `?${qs}` : ''}`);
    },
    decide: (recordId, payload) => fetchApi(
        `/platform/licence-review/${encodeURIComponent(recordId)}/decide`,
        { method: 'POST', body: JSON.stringify(payload) }),
};

export const rbac = {
    getRoles: () => fetchApi('/rbac/roles'),
};

// GSEC FX.72 p3b (owner decision Q55=C) — "AI in this platform"
// transparency inventory (platform admin only). Backend:
// routers/platform_ai_inventory.py, mounted at /v1.
// GET /platform/ai-inventory -> {features, models, data_flow}.
export const aiInventory = {
    get: () => fetchApi('/platform/ai-inventory'),
};

// FX.84 p2c — on-prem Backups admin surface (platform-admin only).
// Backend: routers/platform_backups.py — GET /platform/backups (backup.*
// settings + backup_status.json last-run + restore-test ledger lines),
// PUT /platform/backups/settings (the seven backup.* keys only).
export const platformBackups = {
    get: () => fetchApi('/platform/backups'),
    update: (payload) => fetchApi('/platform/backups/settings', { method: 'PUT', body: JSON.stringify(payload) }),
};

export const aiAssistant = {
    getStatus: () => fetchApi('/assistant/status'),
    startConversation: (data) => fetchApi('/assistant/conversations', { method: 'POST', body: JSON.stringify(data) }),
    getConversation: (conversationId) => fetchApi(`/assistant/conversations/${conversationId}`),
    sendMessage: (conversationId, data) => fetchApi(`/assistant/conversations/${conversationId}/messages`, { method: 'POST', body: JSON.stringify(data) }),
    listConversations: (limit = 50) => fetchApi(`/assistant/conversations?limit=${limit}`),
    deleteConversation: (conversationId) => fetchApi(`/assistant/conversations/${conversationId}`, { method: 'DELETE' }),
};

export const downloadJobs = {
    create: (cogIds) => fetchApi('/assets/download-jobs', { method: 'POST', body: JSON.stringify({ cog_ids: cogIds }) }),
    getStatus: (jobId) => fetchApi(`/assets/download-jobs/${jobId}`),
};

export const capture = {
    listDevices: () => fetchApi('/capture/field-client/devices'),
    enrolDevice: (name) => fetchApi('/capture/field-client/devices', {
        method: 'POST', body: JSON.stringify({ device_name: name || null }) }),
    revokeDevice: (deviceId) => fetchApi(
        `/capture/field-client/devices/${encodeURIComponent(deviceId)}/revoke`,
        { method: 'POST' }),
    listVerdicts: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.deviceId) q.set('device_id', opts.deviceId);
        if (opts.limit) q.set('limit', opts.limit);
        const qs = q.toString();
        return fetchApi(`/capture/field-client/verdicts${qs ? '?' + qs : ''}`);
    },
};

// FX.98 p3 — likeness consent register (tenant admin).
// Backend: routers/likeness_consent.py (/v1/tenant/likeness-consents*).
// Tenant-admin only (platform admin OR the tenant's root admin — the shared
// _is_tenant_admin idiom). list pages with limit/offset and filters on
// include_revoked; expiring returns {total, window_days, items} for the
// platform's reminder window. No CSV export endpoint exists.
export const likenessConsents = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.includeRevoked) q.set('include_revoked', 'true');
        if (opts.limit) q.set('limit', opts.limit);
        if (opts.offset) q.set('offset', opts.offset);
        const qs = q.toString();
        return fetchApi(`/tenant/likeness-consents${qs ? '?' + qs : ''}`);
    },
    get: (consentId) => fetchApi(`/tenant/likeness-consents/${consentId}`),
    create: (body) => fetchApi('/tenant/likeness-consents', { method: 'POST', body: JSON.stringify(body) }),
    expiring: () => fetchApi('/tenant/likeness-consents/expiring'),
};

// FX.67 p2 — customer lockbox, tenant side (tenant admin).
// Backend: routers/tenant/lockbox.py (/v1/tenant/lockbox/requests*).
// list returns {requests: [...]} (pending + approved first, resolved rows
// included for the decision record). approve/deny/revoke are POSTs with no
// body: deny takes NO reason server-side, revoke ends an approved grant.
export const lockboxRequests = {
    list: () => fetchApi('/tenant/lockbox/requests'),
    get: (lockboxId) => fetchApi(`/tenant/lockbox/requests/${lockboxId}`),
    approve: (lockboxId) => fetchApi(
        `/tenant/lockbox/requests/${lockboxId}/approve`, { method: 'POST' }),
    deny: (lockboxId) => fetchApi(
        `/tenant/lockbox/requests/${lockboxId}/deny`, { method: 'POST' }),
    revoke: (lockboxId) => fetchApi(
        `/tenant/lockbox/requests/${lockboxId}/revoke`, { method: 'POST' }),
};

// FX.101 p4 — sign-in lockdown (tenant admin). Three backends, all read as
// reported: routers/tenant/security.py GET/PUT /tenant/security/ip-allowlist
// ({enabled, allowlist}; PUT takes {cidrs}, manage_settings gate, 400 on a
// malformed CIDR, 409 if the new list would exclude the caller's own IP);
// GET /sso/domains + GET /sso/idp (routers/sso_domains.py, sso_idp.py —
// entitlement-gated, 402 SSO envelope {"detail","code"} on a plan without
// SSO); GET /platform/compliance/profiles (routers/compliance_profiles.py,
// platform admin OR time-boxed auditor — 403 otherwise).
export const tenantSecurityLockdown = {
    get: () => fetchApi('/tenant/security/ip-allowlist'),
    put: (cidrs) => fetchApi('/tenant/security/ip-allowlist', {
        method: 'PUT',
        body: JSON.stringify({ cidrs: cidrs || [] }),
    }),
    domains: () => fetchApi('/sso/domains'),
    idps: () => fetchApi('/sso/idp'),
    govProfiles: () => fetchApi('/platform/compliance/profiles'),
};

// GSEC FX.112 p3c — tenant-admin security alert rules (built-in FX.156 engine
// rules as they apply to THIS tenant). Backend: CORE/API/routers/tenant/
// security.py (mounted at /v1/tenant), logic services/tenant_alert_rules.py.
// list maps to GET /tenant/security/alert-rules (TENANT:view_settings) ->
// {rules:[{rule_key,name,description,tunable,enabled,threshold,default_
// threshold,min_threshold,max_threshold}]}; update PUTs
// /tenant/security/alert-rules/{rule_key} (TENANT:manage_settings) with
// {enabled?, threshold?} — the server 422s an empty body, a threshold sent
// for a non-tunable rule, or a threshold outside the platform bounds
// (security_alerts.tenant_* platform settings), and 404s an unknown rule.
export const alertRules = {
    list: () => fetchApi('/tenant/security/alert-rules'),
    update: (ruleKey, payload) => fetchApi(
        `/tenant/security/alert-rules/${encodeURIComponent(ruleKey)}`,
        { method: 'PUT', body: JSON.stringify(payload) }),
};

// GSEC FX.112 p3d — THIS TENANT's security alerts (list + acknowledge).
// Backend: CORE/API/routers/tenant/security_alerts.py (mounted at /v1), logic
// services/security_alert_delivery.ack_alert. list maps to
// GET /tenant/security-alerts?acked=&limit=&offset= (TENANT:view_settings)
// -> {items:[{id,rule_key,severity,tenant_id,subject,details,first_seen_at,
// last_seen_at,count,acked_at,acked_by}],total,limit,offset} ordered newest
// first (first_seen_at desc, id desc). The ONLY server filters are `acked`
// (true=acked only, false=open only, omitted=all) and the limit/offset page
// window — there is no severity param, so severity narrowing is client-side
// and is labelled as such. ack POSTs to
// /tenant/security-alerts/{id}/ack (TENANT:manage_settings) with NO body —
// the endpoint takes no note — and answers {alert_id,already_acked,acked_at,
// acked_by}; re-acking is idempotent (no second audit row); another tenant's
// id 404s (never an existence leak).
export const securityAlerts = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.acked !== undefined && opts.acked !== null) {
            q.set('acked', opts.acked ? 'true' : 'false');
        }
        if (opts.limit) q.set('limit', String(opts.limit));
        if (opts.offset) q.set('offset', String(opts.offset));
        const qs = q.toString();
        return fetchApi(`/tenant/security-alerts${qs ? `?${qs}` : ''}`);
    },
    ack: (alertId) => fetchApi(
        `/tenant/security-alerts/${encodeURIComponent(alertId)}/ack`,
        { method: 'POST' }),
};

// GSEC FX.144 p4 — release-approval queue (platform admin only).
// Backend: CORE/API/routers/release_approvals.py, mounted at /v1. list maps to
// the GET query params (status, limit); create POSTs {release_ref,rollback_ref,
// image_digests,evidence,reason} and answers 202 (nothing ships until the owner
// approves); approve/reject send {decision_note?,evidence?}; emergency sends
// {reason REQUIRED, deployed_at?}; review sends {outcome?,decision_note?};
// changeLog maps to /change-log?release_ref=&limit=.
export const releaseApprovals = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.status) q.set('status', opts.status);
        if (opts.limit) q.set('limit', String(opts.limit));
        const qs = q.toString();
        return fetchApi(`/platform/release-approvals${qs ? `?${qs}` : ''}`);
    },
    get: (approvalId) => fetchApi(
        `/platform/release-approvals/${encodeURIComponent(approvalId)}`),
    create: (payload) => fetchApi('/platform/release-approvals',
        { method: 'POST', body: JSON.stringify(payload) }),
    approve: (approvalId, body = {}) => fetchApi(
        `/platform/release-approvals/${encodeURIComponent(approvalId)}/approve`,
        { method: 'POST', body: JSON.stringify(body || {}) }),
    reject: (approvalId, body = {}) => fetchApi(
        `/platform/release-approvals/${encodeURIComponent(approvalId)}/reject`,
        { method: 'POST', body: JSON.stringify(body || {}) }),
    emergency: (approvalId, body = {}) => fetchApi(
        `/platform/release-approvals/${encodeURIComponent(approvalId)}/emergency`,
        { method: 'POST', body: JSON.stringify(body || {}) }),
    review: (approvalId, body = {}) => fetchApi(
        `/platform/release-approvals/${encodeURIComponent(approvalId)}/review`,
        { method: 'POST', body: JSON.stringify(body || {}) }),
    overdue: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.limit) q.set('limit', String(opts.limit));
        const qs = q.toString();
        return fetchApi(`/platform/release-approvals/overdue${qs ? `?${qs}` : ''}`);
    },
    changeLog: (releaseRef = null, opts = {}) => {
        const q = new URLSearchParams();
        if (releaseRef) q.set('release_ref', releaseRef);
        if (opts.from_ref) q.set('from_ref', opts.from_ref);
        if (opts.to_ref) q.set('to_ref', opts.to_ref);
        q.set('limit', String(opts.limit || 20));
        return fetchApi(`/platform/release-approvals/change-log?${q}`);
    },
};

// GSEC FX.106 p2b — auto-blocked client IPs (platform admin only).
// Backend: CORE/API/routers/platform_ip_blocks.py (mounted at /v1). The block
// store is IN-MEMORY PER API PROCESS (services/api_rate_limit_guard.py), so
// every payload carries scope:'this_process' and the page prints it — this is
// never presented as a cluster-wide block. list maps to the GET (?limit=);
// unblock POSTs {reason REQUIRED} to /{ip}/unblock (the reason is the audit
// row's "why"; the server refuses an unblock without one).
export const ipBlocks = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.limit) q.set('limit', String(opts.limit));
        const qs = q.toString();
        return fetchApi(`/platform/security/ip-blocks${qs ? `?${qs}` : ''}`);
    },
    unblock: (ip, reason) => fetchApi(
        `/platform/security/ip-blocks/${encodeURIComponent(ip)}/unblock`,
        { method: 'POST', body: JSON.stringify({ reason }) }),
};

// GSEC FX.159.p4b — contract-obligations tracker (platform admin only).
// Backend: routers/admin_contract_obligations.py (/v1/admin/contract-obligations*).
// list/maps to the GET query params (tenant_id, obligation_type, status);
// create POSTs {contract_ref, obligation_type, obligation, owner, due_date, recurrence, evidence_ref, parent_id};
// transition POSTs {evidence_ref?} to the per-id state-machine endpoints
// (acknowledge|satisfy|breach); archive soft-deletes (DELETE with {reason}).
export const contractObligations = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.tenant_id) q.set('tenant_id', String(opts.tenant_id));
        if (opts.obligation_type) q.set('obligation_type', opts.obligation_type);
        if (opts.status) q.set('status', opts.status);
        const qs = q.toString();
        return fetchApi(`/admin/contract-obligations${qs ? `?${qs}` : ''}`);
    },
    create: (payload) => fetchApi(
        '/admin/contract-obligations',
        { method: 'POST', body: JSON.stringify(payload) }),
    transition: (id, toStatus) => fetchApi(
        `/admin/contract-obligations/${encodeURIComponent(id)}/${toStatus}`,
        { method: 'POST', body: JSON.stringify({}) }),
    archive: (id, reason) => fetchApi(
        `/admin/contract-obligations/${encodeURIComponent(id)}`,
        { method: 'DELETE', body: JSON.stringify({ reason }) }),
};

// Export fetchApi as apiCall for compatibility with pages that expect it
export { fetchApi as apiCall };

// Export fetchApi directly as well
export { fetchApi };