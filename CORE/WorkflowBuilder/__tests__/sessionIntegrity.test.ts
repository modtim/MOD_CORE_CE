/**
 * Cross-origin session-integrity guard (security: cross-user session bleed).
 *
 * WB is a separate origin from the Dashboard, so a Dashboard logout / user
 * switch cannot reach WB's own-origin localStorage. On a load without a fresh
 * `?token=`, WB must confirm the stored token belongs to the currently-active
 * user (via the parent-domain identity cookie the Dashboard maintains) before
 * reusing it. These tests pin the pure decision + the JWT/cookie helpers that
 * feed it.
 */

import {
    decodeTokenSub,
    readActiveIdentityCookie,
    decideStoredTokenTrust,
    reconcileSession,
    tryRefreshToken,
    IDENTITY_LOGGED_OUT,
    ACTIVE_IDENTITY_COOKIE,
    ACCESS_TOKEN_KEY,
    REFRESH_TOKEN_KEY,
} from '@/lib/api-client';

// Build a minimal JWT (header.payload.signature) carrying the given claims,
// base64url-encoded with padding stripped, exactly as Keycloak emits.
function jwt(claims: Record<string, unknown>): string {
    const b64url = (o: unknown) =>
        Buffer.from(JSON.stringify(o)).toString('base64')
            .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `${b64url({ alg: 'RS256', typ: 'JWT' })}.${b64url(claims)}.sig`;
}

describe('decodeTokenSub', () => {
    it('extracts the sub claim from a well-formed JWT', () => {
        expect(decodeTokenSub(jwt({ sub: 'qa-owner-uuid', preferred_username: 'qa-owner' }))).toBe('qa-owner-uuid');
    });
    it('returns null for missing / malformed / sub-less tokens', () => {
        expect(decodeTokenSub(null)).toBeNull();
        expect(decodeTokenSub('not-a-jwt')).toBeNull();
        expect(decodeTokenSub('a.b')).toBeNull();
        expect(decodeTokenSub(jwt({ preferred_username: 'nobody' }))).toBeNull();
    });
});

describe('decideStoredTokenTrust', () => {
    const OWNER = 'qa-owner-uuid';
    const USER = 'qa-user-uuid';

    it('rejects when the active identity is a DIFFERENT user (the bleed case)', () => {
        // Tab authenticated as qa-owner, but the active Dashboard session is qa-user.
        expect(decideStoredTokenTrust(OWNER, USER)).toBe('reject');
    });
    it('rejects on the explicit logout sentinel', () => {
        expect(decideStoredTokenTrust(OWNER, IDENTITY_LOGGED_OUT)).toBe('reject');
    });
    it('uses the stored token when the active identity matches', () => {
        expect(decideStoredTokenTrust(OWNER, OWNER)).toBe('use');
    });
    it('falls back to use (prior /me validation) when the cookie is absent (pre-Dashboard-fix)', () => {
        expect(decideStoredTokenTrust(OWNER, null)).toBe('use');
    });
    it('uses (does NOT reject) when the stored token has no decodable sub, even with a cookie present', () => {
        // Regression guard: rejecting an unknown sub against a present cookie
        // would bounce the CURRENT user whenever the stored token simply
        // couldn't be decoded. A genuinely stale OTHER-user token is always a
        // decodable Keycloak JWT (it takes the cross-user branch and rejects);
        // an undecodable token just fails the /me validation instead.
        expect(decideStoredTokenTrust(null, USER)).toBe('use');
    });
    it('only rejects on a GENUINE cross-user mismatch (both identities known and different)', () => {
        expect(decideStoredTokenTrust(OWNER, USER)).toBe('reject');
        expect(decideStoredTokenTrust(OWNER, OWNER)).toBe('use');
        expect(decideStoredTokenTrust(null, null)).toBe('use');
    });
});

// Full initializeAuth decision chain: decode the stored token's sub, read the
// parent-domain cookie, then decide. Pins the "same user loads WB normally"
// guarantee — including a URL-encoded cookie value — so the auth bootstrap
// never wipes a valid same-user session.
describe('initializeAuth decision chain (decode + cookie read + decide)', () => {
    const OWNER = 'qa-owner-uuid';
    const USER = 'qa-user-uuid';

    afterEach(() => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    });

    function decide(storedToken: string | null): string {
        const storedSub = decodeTokenSub(storedToken);
        const cookieIdentity = readActiveIdentityCookie();
        return decideStoredTokenTrust(storedSub, cookieIdentity);
    }

    it('USES a valid same-user token when the cookie holds the RAW sub', () => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${OWNER}; path=/`;
        expect(decide(jwt({ sub: OWNER, preferred_username: 'qa-owner' }))).toBe('use');
    });

    it('USES a valid same-user token when the cookie holds the URL-ENCODED sub', () => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${encodeURIComponent(OWNER)}; path=/`;
        expect(decide(jwt({ sub: OWNER, preferred_username: 'qa-owner' }))).toBe('use');
    });

    it('USES the stored token when no cookie is present at all', () => {
        expect(decide(jwt({ sub: OWNER }))).toBe('use');
    });

    it('REJECTS a stale cross-user token (tab is qa-owner, active session is qa-user)', () => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${USER}; path=/`;
        expect(decide(jwt({ sub: OWNER }))).toBe('reject');
    });

    it('REJECTS on the explicit logout sentinel', () => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${IDENTITY_LOGGED_OUT}; path=/`;
        expect(decide(jwt({ sub: OWNER }))).toBe('reject');
    });
});

// ---------------------------------------------------------------------------
// Reactive reconciliation (the persistent-bleed fix): a WB tab left OPEN never
// re-runs initializeAuth, so a user switch AFTER the tab loaded was not caught.
// reconcileSession runs on focus / visibility / cross-tab storage change and
// decides whether the still-open tab may keep using its in-memory token.
// ---------------------------------------------------------------------------
describe('reconcileSession (already-open tab re-validation)', () => {
    const OWNER = 'qa-owner-uuid';
    const USER = 'qa-user-uuid';

    it('SAME user, cookie matches, no storage change → ok (tab stays)', () => {
        expect(reconcileSession(OWNER, OWNER, OWNER)).toBe('ok');
    });

    it('SAME user, cookie ABSENT (Dashboard predates fix) → ok (tab stays)', () => {
        expect(reconcileSession(OWNER, OWNER, null)).toBe('ok');
    });

    it('SAME user, token silently rotated in a sibling tab (same sub) → ok (caller syncs state, no bounce)', () => {
        // storedSub === inMemorySub even though the raw token string differs;
        // reconcileSession only sees subs, so this is "ok" and the hook adopts
        // the rotated token rather than bouncing.
        expect(reconcileSession(OWNER, OWNER, OWNER)).toBe('ok');
    });

    it('THE PERSISTENT BLEED: tab still using qa-owner, Dashboard now qa-user → reauth-wipe (never shows owner data)', () => {
        // Cookie flipped to qa-user after this qa-owner tab was already open.
        expect(reconcileSession(OWNER, OWNER, USER)).toBe('reauth-wipe');
    });

    it('explicit logout after the tab loaded → reauth-wipe', () => {
        expect(reconcileSession(OWNER, OWNER, IDENTITY_LOGGED_OUT)).toBe('reauth-wipe');
    });

    it('a sibling tab handed off a DIFFERENT user into shared storage → reauth-keep (stored token is the new valid session)', () => {
        // This tab still renders qa-owner in memory; a sibling handoff has
        // overwritten this origin's stored token with qa-user. The cookie has
        // not yet been observed (absent), so the stored-token trust check does
        // not fire, but the divergence between the in-memory user and the newly
        // stored user still forces a bounce — WITHOUT wiping the sibling's
        // valid qa-user token.
        expect(reconcileSession(OWNER, USER, null)).toBe('reauth-keep');
    });

    it('no in-memory token yet (mid-init) → ok (initializeAuth owns that path)', () => {
        expect(reconcileSession(null, OWNER, OWNER)).toBe('ok');
    });
});

// ---------------------------------------------------------------------------
// Expired-token UX (step 4): a routine /me 401 for the SAME user must attempt
// the held refresh token BEFORE wiping/bouncing. The gate that governs this in
// initializeAuth is decideStoredTokenTrust: a non-'reject' result means "same
// user, expired access token → refresh", a 'reject' means "wrong user → bounce".
// ---------------------------------------------------------------------------
describe('expired-token refresh gate (same-user reload is not kicked out)', () => {
    const OWNER = 'qa-owner-uuid';
    const USER = 'qa-user-uuid';

    it('same user (cookie match) with an expired access token → attempt refresh, not bounce', () => {
        expect(decideStoredTokenTrust(OWNER, OWNER)).not.toBe('reject');
    });
    it('same user (cookie absent) with an expired access token → attempt refresh, not bounce', () => {
        expect(decideStoredTokenTrust(OWNER, null)).not.toBe('reject');
    });
    it('cross-user token → reject (never attempt refresh; bounce to fresh handoff)', () => {
        expect(decideStoredTokenTrust(OWNER, USER)).toBe('reject');
    });
});

describe('tryRefreshToken (silent same-user refresh)', () => {
    const realFetch = global.fetch;
    afterEach(() => {
        global.fetch = realFetch;
        localStorage.clear();
        sessionStorage.clear();
        jest.restoreAllMocks();
    });

    it('exchanges the held refresh token and updates sessionStorage in place', async () => {
        // Default client is dashboard-client (Dashboard hand-off) → BFF path.
        sessionStorage.setItem(REFRESH_TOKEN_KEY, 'old-refresh');
        global.fetch = jest.fn().mockResolvedValue({
            ok: true,
            json: async () => ({ access_token: 'new-access', refresh_token: 'new-refresh' }),
        }) as unknown as typeof fetch;

        const result = await tryRefreshToken();
        expect(result).toBe('new-access');
        expect(sessionStorage.getItem(ACCESS_TOKEN_KEY)).toBe('new-access');
        expect(sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBe('new-refresh');
    });

    it('returns null when there is no held refresh token (→ caller wipes+bounces)', async () => {
        sessionStorage.removeItem(REFRESH_TOKEN_KEY);
        const result = await tryRefreshToken();
        expect(result).toBeNull();
    });
});

describe('readActiveIdentityCookie', () => {
    afterEach(() => {
        // Clear the cookie between cases.
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
    });

    it('returns null when the cookie is not set', () => {
        expect(readActiveIdentityCookie()).toBeNull();
    });
    it('reads and URL-decodes the cookie value', () => {
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${encodeURIComponent('qa-user-uuid')}; path=/`;
        expect(readActiveIdentityCookie()).toBe('qa-user-uuid');
    });
    it('reads the value when other cookies precede it', () => {
        document.cookie = 'other=1; path=/';
        document.cookie = `${ACTIVE_IDENTITY_COOKIE}=${IDENTITY_LOGGED_OUT}; path=/`;
        expect(readActiveIdentityCookie()).toBe(IDENTITY_LOGGED_OUT);
    });
});
