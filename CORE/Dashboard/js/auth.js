// In Dashboard/js/auth.js

import { exchangeCodeForToken as apiExchangeCodeForToken, me } from './api.js';
import { showPage, navigateToPage, updateUIVisibility } from './ui.js';
import { parseJwt } from './utils.js';
import { initializeNotifications, cleanupNotifications } from './notifications.js';
import { initOnboardingChecklist } from './onboarding-checklist.js';
// GSEC FX.54 p4a (Q37=A): deadline-driven idle-warning dialog 2 min before the
// SSO idle timeout. The deadline is the refresh token's `exp` (FX.54.p3d syncs
// the realm session lifetimes from the auth.* settings), so no new endpoint.
import { startIdleWarning, deadlineFromSession } from './idle-warning.js';
// PKCE-over-plain-HTTP fix: crypto.subtle is undefined in insecure contexts
// (http://<LAN-IP>), so the S256 challenge threw on every LAN install. This
// helper keeps the native path when present and falls back to pure-JS SHA-256.
import { sha256Digest, b64urlFromBytes } from './pkce-crypto.js';

// --- PKCE Helper Functions ---
// Function to generate a random string for the code verifier.
function generateRandomString(length) {
    const possible = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';
    let text = '';
    for (let i = 0; i < length; i++) {
        text += possible.charAt(Math.floor(Math.random() * possible.length));
    }
    return text;
}

// Function to SHA-256 hash and then Base64-URL-encode a string.
async function generateCodeChallenge(verifier) {
    // sha256Digest uses crypto.subtle when available (secure contexts) and a
    // pure-JS SHA-256 fallback otherwise — identical base64url output, so
    // PKCE S256 works on plain-HTTP LAN installs (owner bug: 'digest' of
    // undefined at auth.js:28).
    const digest = await sha256Digest(verifier);
    return b64urlFromBytes(digest);
}


// --- OIDC Configuration (Updated for Keycloak) ---
const API_BASE_URL = window.APP_CONFIG?.API_URL || "http://api.localhost";
const KEYCLOAK_URL = window.APP_CONFIG?.KEYCLOAK_URL || "http://localhost";
const KEYCLOAK_REALM = "modtex"; // From your .env file
const KEYCLOAK_CLIENT_ID = "dashboard-client";

export const REDIRECT_URI = window.location.origin + window.location.pathname;

// --- Cross-origin session-bleed guard (mod_active_identity) ---
// WorkflowBuilder lives on a separate origin but shares a stored token across
// the parent domain. To stop it resurrecting a stale/other session, the
// Dashboard publishes the CURRENT authenticated identity (the Keycloak `sub`)
// in a parent-domain cookie that WB reads before reusing any stored token.
// The sentinel value `__logged_out__` signals an explicit logout so WB won't
// silently re-hydrate the old session. Cookie is NOT HttpOnly (WB JS must
// read it) and carries only the opaque `sub` — no token, no secret.
const ACTIVE_IDENTITY_COOKIE = 'mod_active_identity';
const LOGGED_OUT_SENTINEL = '__logged_out__';

// Derive the shared parent domain from the current hostname: the last two
// labels (e.g. example.com -> .example.com). Returns null for a
// bare host with no dot (localhost / an IP) where a Domain attribute is
// invalid — the cookie is then written host-only.
function _parentCookieDomain() {
    const host = window.location.hostname || '';
    const labels = host.split('.').filter(Boolean);
    if (labels.length < 2) return null;
    return '.' + labels.slice(-2).join('.');
}

function _writeActiveIdentityCookie(value) {
    let cookie = `${ACTIVE_IDENTITY_COOKIE}=${value}; path=/; SameSite=Lax`;
    const domain = _parentCookieDomain();
    if (domain) cookie += `; domain=${domain}`;
    if (window.location.protocol === 'https:') cookie += '; Secure';
    document.cookie = cookie;
}

// Publish the identity of a freshly stored access token so WB trusts it.
export function setActiveIdentity(token) {
    try {
        const sub = parseJwt(token)?.sub;
        if (sub) _writeActiveIdentityCookie(sub);
    } catch (e) {
        console.warn('Could not set active-identity cookie:', e);
    }
}

// Write the logout sentinel so WB won't resurrect the old session.
export function clearActiveIdentity() {
    _writeActiveIdentityCookie(LOGGED_OUT_SENTINEL);
}

// --- URL Builders (Updated for Keycloak) ---
// Stash the deep-link the user was trying to reach (e.g. the mobile app page from the
// mobile QR) before we bounce to Keycloak — the OAuth redirect drops the URL
// fragment, so we restore it after the token exchange (see checkApiOnboarding).
function _rememberPostLoginHash() {
    const h = window.location.hash;
    if (h && h !== '#home' && h !== '#login') {
        localStorage.setItem('post_login_hash', h);
    }
}

export async function getLoginUrl() {
    _rememberPostLoginHash();
    const codeVerifier = generateRandomString(128);
    // localStorage (not sessionStorage): the auth callback may land in a
    // different browser tab (email-verification links, restored URLs) which
    // has no access to the originating tab's sessionStorage. localStorage
    // is shared across tabs on the same origin, so the verifier survives.
    // Single-use — removed after successful exchange in api.js.
    localStorage.setItem('pkce_code_verifier', codeVerifier);
    const codeChallenge = await generateCodeChallenge(codeVerifier);

    const params = new URLSearchParams({
        client_id: KEYCLOAK_CLIENT_ID,
        redirect_uri: REDIRECT_URI,
        response_type: "code",
        scope: "openid profile email groups",  // 2400LR-b1j: offline_access dropped, it ends the KC SSO session
        code_challenge: codeChallenge,
        code_challenge_method: "S256",
    });
    return `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/auth?${params.toString()}`;
}

// GY.C4.MFA — MOD-branded application-initiated-action URLs (2FA enrollment,
// password change).
//
// The API returns the Keycloak authorization URL with kc_action=CONFIGURE_TOTP
// (rendered by the modtech login theme, login-config-totp.ftl). We rebuild the
// query here so the redirect_uri is EXACTLY this page's REDIRECT_URI and the
// PKCE verifier is the one api.js will exchange with — the API-side URL alone
// would exchange against a different redirect_uri and fail.
//
// The Keycloak account console is never used: enrollment, device replacement,
// password change and admin reset all happen on MOD pages or the MOD-themed
// login pages.
export async function getKcActionUrl(action, apiActionUrl) {
    _rememberPostLoginHash();
    const codeVerifier = generateRandomString(128);
    localStorage.setItem('pkce_code_verifier', codeVerifier);
    const codeChallenge = await generateCodeChallenge(codeVerifier);
    // main.js reads this on return so the toast names the action the user
    // performed (2FA / password) instead of assuming email verification.
    try { localStorage.setItem('mod_pending_action', action); } catch (_) {}

    let base = `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/auth`;
    if (apiActionUrl) {
        try {
            const parsed = new URL(apiActionUrl);
            base = `${parsed.origin}${parsed.pathname}`;
        } catch (_) { /* fall back to the configured Keycloak origin */ }
    }

    const params = new URLSearchParams({
        client_id: KEYCLOAK_CLIENT_ID,
        redirect_uri: REDIRECT_URI,
        response_type: "code",
        scope: "openid profile email",
        kc_action: action,
        code_challenge: codeChallenge,
        code_challenge_method: "S256",
    });
    return `${base}?${params.toString()}`;
}

export const getMfaSetupUrl = (apiSetupUrl) =>
    getKcActionUrl("CONFIGURE_TOTP", apiSetupUrl);

export async function getRegistrationUrl() {
    _rememberPostLoginHash();
    const codeVerifier = generateRandomString(128);
    // See note in getLoginUrl — localStorage so the verifier survives
    // a callback that lands in a different tab (e.g. the email magic link).
    localStorage.setItem('pkce_code_verifier', codeVerifier);
    const codeChallenge = await generateCodeChallenge(codeVerifier);

    const params = new URLSearchParams({
        client_id: KEYCLOAK_CLIENT_ID,
        redirect_uri: REDIRECT_URI,
        response_type: "code",
        scope: "openid profile email groups",
        code_challenge: codeChallenge,
        code_challenge_method: "S256",
    });
    return `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/registrations?${params.toString()}`;
}

function getLogoutUrl(postLogoutFlag = 'logged_out=true') {
    const idToken = sessionStorage.getItem("idToken");
    const postLogoutUri = REDIRECT_URI + (REDIRECT_URI.includes('?') ? '&' : '?') + postLogoutFlag;

    const params = new URLSearchParams({
        client_id: KEYCLOAK_CLIENT_ID,
        post_logout_redirect_uri: postLogoutUri,
    });

    if (idToken) {
        params.set("id_token_hint", idToken);
    }

    return `${KEYCLOAK_URL}/realms/${KEYCLOAK_REALM}/protocol/openid-connect/logout?${params.toString()}`;
}

// --- Handlers ---
export async function handleLogin() {
    console.log("Redirecting to Keycloak for login...");
    window.location.href = await getLoginUrl();
}

export async function handleSignup() {
    console.log("Redirecting to Keycloak for registration...");
    window.location.href = await getRegistrationUrl();
}

export function handleLogout() {
    console.log("Clearing session and logging out...");
    stopIdleWarning();            // FX.54 p4a: disarm the idle-warning timers
    stopTokenRefreshInterval();  // Stop proactive token refresh
    cleanupNotifications(); // Clean up notification polling
    sessionStorage.clear();
    clearActiveIdentity();  // Tell WB this session is over (logout sentinel)
    window.location.href = getLogoutUrl();
}

// Full Keycloak logout → bounce back to the dashboard with a flag that
// main.js converts into an immediate redirect to the Keycloak registration
// page. Needed on the incomplete-onboarding screen: a plain signup redirect
// wouldn't work because Keycloak's SSO cookie would silently re-authenticate
// the partially-onboarded user and land them right back where they started.
export function handleLogoutAndSignup() {
    console.log("Logging out, then starting a fresh signup...");
    stopTokenRefreshInterval();
    cleanupNotifications();
    sessionStorage.clear();
    clearActiveIdentity();  // Tell WB this session is over (logout sentinel)
    window.location.href = getLogoutUrl('continue_to=signup');
}

// Proactive token refresh interval (check every 5 minutes)
let tokenRefreshInterval = null;

// Check if token will expire soon (within 5 minutes) and refresh proactively
async function checkAndRefreshToken() {
    const token = sessionStorage.getItem("accessToken");
    const refreshToken = sessionStorage.getItem("refreshToken");

    if (!token || !refreshToken) return;

    try {
        const payload = parseJwt(token);
        const expiryTime = payload.exp * 1000; // Convert to milliseconds
        const now = Date.now();
        const timeUntilExpiry = expiryTime - now;

        // Refresh if token expires in less than 5 minutes
        if (timeUntilExpiry < 5 * 60 * 1000) {
            console.log(`Token expires in ${Math.round(timeUntilExpiry / 1000)}s, refreshing proactively...`);
            await refreshAccessToken();
        }
    } catch (error) {
        console.warn('Error checking token expiry:', error);
    }
}

// FX.54 p4a — the live idle-warning handle (one per session). Started in
// _showAuthenticatedApp once login is confirmed; re-armed after every
// successful refresh because a refresh slides the Keycloak idle deadline
// forward (the new refresh token's `exp`).
let idleWarningHandle = null;

// BUG.2 — remember-me (offline_access) refresh tokens have no usable `exp`, so
// the idle deadline needs an IDLE limit instead. Everything here comes from data
// the client already has: /me's session_tier.idle_seconds (routers/me/__init__.py
// adds it), the token response's refresh_expires_in, and the offline_access
// scope. No new endpoint, no new request.
const sessionDeadlineContext = { idleLimitMs: null, refreshExpiresInSec: null, offlineAccess: false };

function startIdleWarningDialog() {
    if (idleWarningHandle) return;
    try {
        idleWarningHandle = startIdleWarning({
            getDeadlineMs: () => deadlineFromSession(sessionStorage, sessionDeadlineContext),
            getIdleLimitMs: () => sessionDeadlineContext.idleLimitMs,
            activityTarget: document,
            onStay: refreshAccessToken,   // the existing proactive-refresh path
            onActivityRefresh: refreshAccessToken,  // same refresh, on user activity within the 90 s margin
            onSignOut: handleLogout,      // the existing logout
        });
    } catch (e) {
        console.warn('idle-warning: could not start', e);
        idleWarningHandle = null;
    }
}

function rearmIdleWarning() {
    if (!idleWarningHandle) return;
    idleWarningHandle.rearm(deadlineFromSession(sessionStorage, sessionDeadlineContext));
}

function stopIdleWarning() {
    if (!idleWarningHandle) return;
    idleWarningHandle.stop();
    idleWarningHandle = null;
}

// Attempt to refresh the access token via the BFF. Since dashboard-client
// is a confidential client (Option C), the client_secret must be attached
// to the refresh call — the browser cannot hold it, so the API proxies the
// request and injects the secret server-side.
async function refreshAccessToken() {
    const refreshToken = sessionStorage.getItem("refreshToken");
    if (!refreshToken) return false;

    try {
        const response = await fetch(`${API_BASE_URL}/auth/refresh-token`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                refresh_token: refreshToken,
                client_id: KEYCLOAK_CLIENT_ID,
            }),
        });

        if (!response.ok) {
            console.warn('Token refresh failed:', response.status);
            return false;
        }

        const data = await response.json();
        console.log('Access token refreshed proactively');

        sessionStorage.setItem('accessToken', data.access_token);
        if (data.refresh_token) {
            sessionStorage.setItem('refreshToken', data.refresh_token);
        }
        if (data.refresh_expires_in) sessionDeadlineContext.refreshExpiresInSec = data.refresh_expires_in;
        setActiveIdentity(data.access_token);  // Keep WB's identity cookie fresh

        rearmIdleWarning();  // FX.54 p4a: the new refresh token moved the idle deadline

        window.dispatchEvent(new CustomEvent("mod:auth:token-refreshed", {
            detail: { source: "proactive-refresh" }
        }));

        return true;
    } catch (error) {
        console.error('Error refreshing token:', error);
        return false;
    }
}

// Start proactive token refresh checking
function startTokenRefreshInterval() {
    if (tokenRefreshInterval) return; // Already running

    // Check immediately
    checkAndRefreshToken();

    // Then check every 5 minutes
    tokenRefreshInterval = setInterval(checkAndRefreshToken, 5 * 60 * 1000);
    console.log('Started proactive token refresh interval');
}

// Stop proactive token refresh checking
function stopTokenRefreshInterval() {
    if (tokenRefreshInterval) {
        clearInterval(tokenRefreshInterval);
        tokenRefreshInterval = null;
        console.log('Stopped proactive token refresh interval');
    }
}

export async function exchangeCodeForToken(code, redirectUri) {
    try {
        console.log("Attempting to exchange authorization code for token...");
        const data = await apiExchangeCodeForToken(code, redirectUri);
        console.log("Successfully exchanged code for token.");

        sessionStorage.setItem("accessToken", data.access_token);
        sessionStorage.setItem("refreshToken", data.refresh_token);
        if (data.id_token) {
            sessionStorage.setItem("idToken", data.id_token);
        }
        // BUG.2: remember-me sessions (offline_access scope) get an INACTIVITY
        // deadline, not the access-token exp — remember what the token response
        // told us.
        sessionDeadlineContext.refreshExpiresInSec = data.refresh_expires_in ?? null;
        sessionDeadlineContext.offlineAccess =
            String(data.scope || '').includes('offline_access');
        setActiveIdentity(data.access_token);  // Publish identity for WB on login

        // The assistant chrome polls sessionStorage for ~3s then parks itself
        // on this event. On a fresh login the code exchange usually outlasts
        // that budget, so without this the panel stays dead until a reload.
        window.dispatchEvent(new CustomEvent("mod:auth:token-refreshed", {
            detail: { source: "code-exchange" }
        }));

        // Clean up the verifier after it has been used (localStorage — see
        // getLoginUrl / getRegistrationUrl for why it's stored there).
        localStorage.removeItem('pkce_code_verifier');

        // Start proactive token refresh
        startTokenRefreshInterval();

        window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);
    } catch(error) {
        console.error("Token exchange failed:", error);
        // Clean up failed verifier attempt
        localStorage.removeItem('pkce_code_verifier');

        // Tim bug (11-06): /auth/exchange-code answers a REJECTED login with a
        // structured 403 — the JIT seat limit, the unknown IdP domain and the
        // MFA enrollment gate (CORE/API/routers/auth.py, codes in
        // CORE/API/sso/jit_provisioning.py). api.js now carries that payload on
        // ApiError.detail, so show the server's OWN message on the login page
        // instead of silently bouncing (the bounce looked like a logout).
        // Render ONLY detail.message: detail.metadata carries tenant/domain
        // identifiers and must never reach the page, and nothing here is a
        // client-side guess. Any other status/code keeps the old bounce.
        const structured = (error && typeof error === 'object' && typeof error.detail === 'object' && error.detail !== null)
            ? error.detail
            : null;
        const loginErrorCode = structured ? structured.code : null;
        // The MFA gate's 403 carries code + required_action but no message, so it
        // gets a fixed client string instead of a silent bounce.
        const loginErrorMessage = (typeof structured?.message === 'string' && structured.message)
            ? structured.message
            : (loginErrorCode === 'MFA_ENROLLMENT_REQUIRED'
                ? 'Two-factor authentication must be set up for this account before you can sign in. Ask an administrator to reset it, or enroll when Keycloak prompts you.'
                : '');
        if (structured && error && error.status === 403 &&
            (loginErrorCode === 'seat_limit_exceeded' || loginErrorCode === 'unknown_domain' || loginErrorCode === 'MFA_ENROLLMENT_REQUIRED') &&
            loginErrorMessage) {
            const loginPage = document.getElementById("login-page");
            const errorNode = document.createElement("div");
            errorNode.id = 'login-error-message';
            errorNode.textContent = loginErrorMessage;
            loginPage.appendChild(errorNode);
            loginPage.style.display = 'flex';
            return;
        }

        showPage(document.getElementById("login-page"));
    }
}


// Reveal the app now that /me has confirmed the user's state. Factored out of
// checkApiOnboarding so the transient-error retry path can reuse the exact
// same reveal logic instead of duplicating it.
async function _showAuthenticatedApp(userProfile) {
    // /me already returned the caller's effective session tier (routers/me/
    // __init__.py 'session_tier'); feed its idle_seconds to the deadline logic.
    if (userProfile?.session_tier?.idle_seconds) {
        sessionDeadlineContext.idleLimitMs = userProfile.session_tier.idle_seconds * 1000;
    }
    startIdleWarningDialog();  // FX.54 p4a (Q37=A): warn at refresh-token exp - 2 min
    if (userProfile.onboarding_complete) {
        console.log("User has completed onboarding. Showing dashboard.");
        await updateUIVisibility();
        showPage(document.getElementById("dashboard"));
        initializeNotifications();
        initOnboardingChecklist();
        // Restore the pre-login deep-link (the OAuth redirect dropped the
        // fragment). Consumed once; falls back to the current hash / home.
        const postLoginHash = localStorage.getItem('post_login_hash');
        if (postLoginHash) localStorage.removeItem('post_login_hash');
        navigateToPage(postLoginHash || window.location.hash || "#home");
    } else {
        console.log("User has NOT completed onboarding. Showing onboarding page.");
        showPage(document.getElementById("onboarding-page"));
    }
}

// Bounded retry for a transient (non-auth) /me failure during boot. A 5xx or
// network drop does NOT invalidate the session, so the token stays in
// sessionStorage and we re-poll /me for up to ~30s before falling back to the
// login page. This is the fix for the boot race where a momentary API 502
// reached the old else-branch, triggered handleLogout() (clearing a valid
// session + redirecting to Keycloak logout) and left #dashboard hidden
// forever — the failure that flaked dash-home-content.spec.ts.
async function _retryOnTransientBootError() {
    const DEADLINE_MS = 30000;
    const start = Date.now();
    let attempt = 0;
    while (Date.now() - start < DEADLINE_MS) {
        attempt += 1;
        await new Promise((r) => setTimeout(r, Math.min(5000, 800 * attempt)));
        try {
            const recoveredProfile = await me.get();
            console.log(`/me recovered after transient error (boot retry ${attempt}).`);
            await _showAuthenticatedApp(recoveredProfile);
            return;
        } catch (e) {
            if (e && e.name === 'ApiError' && e.status === 401) {
                // The session genuinely expired mid-retry; fetchApi has already
                // kicked off the logout redirect. Stop retrying.
                console.warn("Session expired during boot retry; stopping.");
                return;
            }
            console.warn(`Boot /me retry ${attempt} failed:`, e);
        }
    }
    console.warn("API still unreachable after boot retries; showing login page (session preserved).");
    showPage(document.getElementById("login-page"));
}

export async function checkApiOnboarding() {
    console.log("Checking user onboarding status...");
    const token = sessionStorage.getItem("accessToken");
    if (!token) {
        console.log("No access token found. User needs to log in.");
        stopTokenRefreshInterval();
        showPage(document.getElementById("login-page"));
        return;
    }

    // Refresh WB's identity cookie from the stored token on authenticated
    // startup (covers reloads / restored sessions where no fresh login ran).
    setActiveIdentity(token);

    // Start proactive token refresh when we have a stored token
    startTokenRefreshInterval();

    try {
        console.log("Access token found. Calling /me endpoint...");
        const userProfile = await me.get();
        console.log("API call to /me successful. User profile:", userProfile);
        await _showAuthenticatedApp(userProfile);

    } catch (error) {
        console.error("Error checking onboarding status:", error);

        if (error.name === 'ApiError' && error.status === 403 && errorCode === "EMAIL_NOT_VERIFIED") {
            console.log("Email not verified. Showing verify-email page.");
            showPage(document.getElementById("verify-email-page"));

            // Surface the actual address in the instructions so the user
            // knows which inbox to check — we pull it from the access
            // token claims because /me is 403 in this exact state.
            try {
                const t = sessionStorage.getItem("accessToken");
                const claims = t ? parseJwt(t) : null;
                const addr = claims?.email || claims?.preferred_username || claims?.sub;
                const addrEl = document.getElementById('verify-email-address');
                if (addr && addrEl) addrEl.textContent = addr;
            } catch (_) { /* non-fatal */ }

            // Auto-trigger the send on first arrival to this page.
            // The realm has verifyEmail=false (so Keycloak doesn't
            // send the verification email at registration time), and
            // our API gate is the one that pushed the user here — so
            // we're responsible for kicking off the send ourselves.
            // sessionStorage flag debounces so a navigation away +
            // back doesn't spam Keycloak.
            const t = sessionStorage.getItem("accessToken");
            if (t && !sessionStorage.getItem('verify_email_auto_sent')) {
                sessionStorage.setItem('verify_email_auto_sent', '1');
                fetch(`${API_BASE_URL}/auth/send-verification-email`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${t}` },
                }).then(r => r.json()).then(data => {
                    console.log('verify-email auto-send:', data);
                }).catch(e => console.warn('verify-email auto-send failed:', e));
            }

            document.getElementById('check-verification-btn')?.addEventListener('click', async () => {
                const status = document.getElementById('verify-status');
                if (status) {
                    status.textContent = 'Checking…';
                    status.classList.remove('hidden');
                }
                // email_verified is baked into the access token at login, so
                // re-checking /me with the SAME token loops forever even after
                // the user verifies. Re-mint the token first (Keycloak re-reads
                // emailVerified on refresh); if refresh is unavailable, fall back
                // to a full re-login which always yields fresh claims.
                const refreshed = await refreshAccessToken();
                if (!refreshed) {
                    if (status) status.textContent = 'Refreshing your session…';
                    window.location.href = await getLoginUrl();
                    return;
                }
                await checkApiOnboarding();
            });

            document.getElementById('resend-verification-btn')?.addEventListener('click', async () => {
                const status = document.getElementById('verify-status');
                if (status) {
                    status.textContent = 'Sending…';
                    status.classList.remove('hidden');
                }
                const t = sessionStorage.getItem("accessToken");
                try {
                    const resp = await fetch(`${API_BASE_URL}/auth/send-verification-email`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${t}` },
                    });
                    const data = await resp.json();
                    if (status) {
                        status.textContent = data.sent
                            ? 'Verification email sent. Check your inbox (and spam folder).'
                            : `Could not send verification email: ${data.reason || 'unknown error'}.`;
                    }
                } catch (e) {
                    if (status) status.textContent = `Could not send verification email: ${e.message}`;
                }
            });

            // Escape hatches so the user can always leave this page.
            document.getElementById('verify-email-signout-btn')?.addEventListener('click', () => {
                handleLogout();
            });
            document.getElementById('verify-email-different-account-btn')?.addEventListener('click', () => {
                // Clear the current session, then send the user back through
                // the login page — from there they can pick a different
                // account or hit the Sign Up button to start fresh.
                stopTokenRefreshInterval();
                cleanupNotifications();
                sessionStorage.clear();
                window.location.href = '/';
            });
            return;
        }

        if (error.name === 'ApiError' && error.status === 403 && errorCode === "REGISTRATION_INCOMPLETE") {
            console.log("API returned REGISTRATION_INCOMPLETE. User is new. Showing onboarding page.");

            const tokenData = parseJwt(token);
            const onboardingFirstNameInput = document.getElementById("onboarding-first-name");
            const onboardingLastNameInput = document.getElementById("onboarding-last-name");

            if (onboardingFirstNameInput && tokenData.given_name) {
                onboardingFirstNameInput.value = tokenData.given_name;
            }
            if (onboardingLastNameInput && tokenData.family_name) {
                onboardingLastNameInput.value = tokenData.family_name;
            }

            const invitationToken = sessionStorage.getItem('pending_invitation_token');
            const companyNameField = document.getElementById('company-name-field');
            const companyNameInput = document.getElementById('onboarding-company-name');
            const planSection = document.getElementById('plan-selection-section');
            if (invitationToken) {
                console.log("Pending invitation token found. Hiding company name + plan fields for invited user.");
                if(companyNameField) companyNameField.style.display = 'none';
                if(companyNameInput) companyNameInput.removeAttribute('required');
                if(planSection) planSection.style.display = 'none';
            } else {
                console.log("No invitation token. This is a new tenant signup. Showing company name field.");
                if(companyNameField) companyNameField.style.display = 'block';
                if(companyNameInput) companyNameInput.setAttribute('required', 'required');
            }

            showPage(document.getElementById("onboarding-page"));
            loadOnboardingPlans();

            document.getElementById('onboarding-signout-btn')?.addEventListener('click', () => {
                handleLogout();
            });
            document.getElementById('onboarding-signup-btn')?.addEventListener('click', () => {
                handleLogoutAndSignup();
            });
        } else if (
            // Transient server / network failure — the session token is still
            // valid in sessionStorage. The old code called handleLogout()
            // here, which cleared the session and bounced to Keycloak logout;
            // on a momentary 502 the whole app then stayed on the login
            // screen with #dashboard hidden (the flaked boot). Retry /me with
            // backoff and fall back to a soft login page (no session wipe).
            // (error.name === 'TypeError' is a fetch() network failure —
            // DNS / connection refused / tab offline — never an ApiError.)
            (error.name === 'ApiError' && (error.status >= 500 || error.status === 408 || error.status === 429 || error.status === 0)) ||
            error.name === 'TypeError'
        ) {
            console.warn("Transient API error during boot; retrying /me instead of logging out.", error);
            await _retryOnTransientBootError();
        } else {
            console.error("An unexpected error occurred. Logging out for safety.", error);
            handleLogout();
        }
    }
}

export async function loadOnboardingPlans() {
    // Everyone starts on free tier (Sandbox). No plan selection needed.
    // Plan upgrades happen from the Billing/Upgrade page after signup.
    const planSection = document.getElementById('plan-selection-section');
    if (planSection) planSection.style.display = 'none';

    const isSaaS = window.APP_CONFIG?.DEPLOYMENT_MODE === 'saas';

    // Hide TOS for on-prem deploys. The pre-signup PLATFORM_ADMIN_EMAIL
    // check was removed when INITIAL_USER_EMAIL went away — at this
    // point in the flow we genuinely don't know whether the signing-up
    // user will become platform admin (the backend's first-signup
    // bootstrap decides), so we default to showing TOS in SaaS. Hard-
    // ship: if the very first SaaS signup IS the platform admin, they
    // tick the box once. Acceptable.
    if (!isSaaS) {
        const tosSection = document.getElementById('tos-checkbox')?.closest('.border-t');
        if (tosSection) tosSection.style.display = 'none';
        const tosCheckbox = document.getElementById('tos-checkbox');
        if (tosCheckbox) tosCheckbox.removeAttribute('required');

        // GSEC FX.108 p1 (owner Q95=A): the age attestation is HOSTED-only.
        // On self-hosted deploys the school's own authority governs under-13
        // use, so the age box is hidden and not required — an on-prem sign-up
        // that never sends an attestation is unchanged on the API side too.
        const ageSection = document.getElementById('age-attestation-checkbox')?.closest('.border-t');
        if (ageSection) ageSection.style.display = 'none';
        const ageCheckbox = document.getElementById('age-attestation-checkbox');
        if (ageCheckbox) ageCheckbox.removeAttribute('required');
    }
}

function selectPlanCard(card) {
    // Kept for backwards compatibility — plan selection is now hidden
    document.getElementById('selected-plan-id').value = card?.dataset?.planId || '';
}

export async function handleOnboardingSubmit(event) {
    event.preventDefault();
    console.log("Onboarding form submitted.");
    const onboardingError = document.getElementById("onboarding-error");
    onboardingError.classList.add('hidden');
    const token = sessionStorage.getItem("accessToken");
    const invitationToken = sessionStorage.getItem('pending_invitation_token');

    // Get the submit button and disable it
    const submitButton = document.getElementById("complete-setup-button");
    if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = 'Processing...';
        submitButton.classList.add('opacity-50', 'cursor-not-allowed');
    }

    if (!token) {
        console.error("No access token during onboarding submission. Logging out.");
        handleLogout();
        return;
    }

    const formData = new FormData(event.target);
    const data = Object.fromEntries(formData.entries());

    const endpoint = invitationToken ? '/onboarding/finalize-invited-user' : '/onboarding/create-new-tenant';
    console.log(`Submitting onboarding data to: ${endpoint}`);

    if (invitationToken) {
        data.invitation_token = invitationToken;
    }

    try {
        const response = await fetch(`${API_BASE_URL}${endpoint}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify(data)
        });

        if (!response.ok) {
            const errorData = await response.json();
            // LDF-4 follow-up — `detail` may be:
            //   * a string (FastAPI HTTPException with str detail)
            //   * an object {detail, error_id} (LDF-4 §1.6 500 envelope)
            //   * a Pydantic 422 array [{type, loc, msg, ...}, ...]
            // Passing the raw shape into `new Error(...)` stringifies
            // it to "[object Object]"; coerce to a real message first.
            let msg;
            const d = errorData.detail;
            if (typeof d === 'string') {
                msg = d;
            } else if (Array.isArray(d) && d.length > 0 && d[0]?.msg) {
                msg = String(d[0].msg).replace(/^Value error,\s*/i, '');
            } else if (d && typeof d === 'object' && typeof d.detail === 'string') {
                // LDF-4 §1.6 500 envelope — show the inner message + error_id
                msg = d.error_id ? `${d.detail} (error_id: ${d.error_id})` : d.detail;
            } else {
                msg = 'Failed to complete setup.';
            }
            throw new Error(msg);
        }

        console.log("Onboarding successful.");

        if (invitationToken) {
            sessionStorage.removeItem('pending_invitation_token');
        }

        // Everyone starts on free tier — go straight to dashboard
        await checkApiOnboarding();
    } catch (error) {
        console.error("Onboarding submission failed:", error);
        onboardingError.textContent = error.message;
        onboardingError.classList.remove('hidden');

        // Re-enable the button on error
        if (submitButton) {
            submitButton.disabled = false;
            submitButton.textContent = 'Complete Setup';
            submitButton.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}