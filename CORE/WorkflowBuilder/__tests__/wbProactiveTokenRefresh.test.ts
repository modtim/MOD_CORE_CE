/**
 *  — proactive access-token refresh for the
 * Workflow Builder (owner saw 401s near the token boundary; the Dashboard has
 * had this since its "Started proactive token refresh interval").
 *
 * RED before the fix: lib/api-client.ts had NO proactive scheduler at all
 * (refresh was reactive only: authenticatedFetch 401 -> tryRefreshToken), so
 * every import below fails.
 *
 * Covers:
 *   1. accessTokenExpMs reads ONLY the JWT exp claim (never logs anything)
 *   2. refresh fires at exp - WB_PROACTIVE_REFRESH_LEAD_MS (60 s) and NOTHING
 *      fires before that boundary
 *   3. re-armed after every refresh — the next window is computed from the
 *      ROTATED token's exp (a second refresh fires at the new boundary)
 *   4. a failed refresh does not storm (no immediate re-fire loop)
 *   5. logout clears the scheduler (clearWorkflowTokens / stop)
 *   6. wake-on-visibilitychange re-arms against a fresher stored token
 *      (sibling-tab handoff); hidden leaves the pending window alone
 *
 * Injected clock: fakeNow drives the module's `now` and jest fake timers
 * advance together, so every boundary is absolute (fixed epoch T0).
 */

import {
    WB_PROACTIVE_REFRESH_LEAD_MS,
    accessTokenExpMs,
    clearWorkflowTokens,
    getAccessToken,
    saveWorkflowTokens,
    startProactiveTokenRefresh,
    stopProactiveTokenRefresh,
} from '@/lib/api-client';

const T0 = 1_700_000_000_000; // absolute epoch; fakeNow is ms from here

function makeJwt(expSeconds: number): string {
    const payload = btoa(JSON.stringify({ exp: expSeconds, sub: 'qa-owner' }));
    const b64 = payload.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

let fakeNow = T0;
let fetchCalls = 0;
let queue: number[] = [];
let vis = 'visible';

// Mock token round-trip: each call consumes the next rotated exp (seconds);
// an exhausted queue models a FAILED refresh (no access_token in the body).
function installFetchMock(): void {
    (global as any).fetch = async (): any => {
        fetchCalls += 1;
        const expSec = queue.length ? queue.shift() as number : null;
        if (expSec === null) return { ok: false, json: async () => ({}) };
        const tok = makeJwt(expSec);
        return { ok: true, json: async () => ({ access_token: tok, refresh_token: tok }) };
    };
}

async function advance(ms: number): Promise<void> {
    fakeNow += ms;
    await jest.advanceTimersByTimeAsync(ms);
    await jest.advanceTimersByTimeAsync(0); // flush microtasks (mock fetch chain)
}

function armAt(accessExpSec: number): boolean {
    sessionStorage.clear();
    saveWorkflowTokens(makeJwt(accessExpSec), makeJwt(T0 / 1000 + 7200));
    return startProactiveTokenRefresh(getAccessToken(), { now: () => fakeNow });
}

beforeEach(() => {
    jest.useFakeTimers();
    fakeNow = T0;
    fetchCalls = 0;
    queue = [];
    vis = 'visible';
    installFetchMock();
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => vis });
});

afterEach(() => {
    stopProactiveTokenRefresh();
    sessionStorage.clear();
    jest.useRealTimers();
});

describe('exp decode', () => {
    it('accessTokenExpMs reads only the exp claim', () => {
        expect(accessTokenExpMs(makeJwt(1_800_000_600))).toBe(1_800_000_600_000);
        expect(accessTokenExpMs('not-a-jwt')).toBeNull();
        expect(accessTokenExpMs(null)).toBeNull();
    });
    it('no token -> nothing is scheduled', async () => {
        sessionStorage.clear();
        expect(startProactiveTokenRefresh(getAccessToken(), { now: () => fakeNow })).toBe(false);
        await advance(100_000);
        expect(fetchCalls).toBe(0);
    });
});

describe('proactive window', () => {
    it('nothing refreshes before the boundary; refresh fires exactly 60 s before exp', async () => {
        queue = [T0 / 1000 + 1200];
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(300_000 - WB_PROACTIVE_REFRESH_LEAD_MS - 1);
        expect(fetchCalls).toBe(0);
        await advance(1);
        expect(fetchCalls).toBe(1);
    });

    it('re-armed after each refresh: second boundary comes from the rotated token', async () => {
        queue = [T0 / 1000 + 1200];
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(240_000);
        expect(fetchCalls).toBe(1);
        // rotated exp T0+1200s -> next boundary 900 s later (exp - 60 s)
        await advance(899_999);
        expect(fetchCalls).toBe(1);
        await advance(1);
        expect(fetchCalls).toBe(2);
    });

    it('a failed refresh does not storm', async () => {
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(240_000);
        expect(fetchCalls).toBe(1);
        await advance(2_000_000);
        expect(fetchCalls).toBe(1);
    });

    it('logout clears the pending refresh (clearWorkflowTokens)', async () => {
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(120_000);
        clearWorkflowTokens();
        await advance(200_000);
        expect(fetchCalls).toBe(0);
        expect(getAccessToken()).toBeNull();
    });

    it('stopProactiveTokenRefresh cancels too', async () => {
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(120_000);
        stopProactiveTokenRefresh();
        await advance(200_000);
        expect(fetchCalls).toBe(0);
    });
});

describe('wake-on-visibilitychange', () => {
    it('visible re-arms against a fresher stored token (old boundary suppressed)', async () => {
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(100_000);
        // sibling-tab handoff stores a fresher token pair
        saveWorkflowTokens(makeJwt(T0 / 1000 + 900), makeJwt(T0 / 1000 + 7200));
        document.dispatchEvent(new Event('visibilitychange'));
        await advance(139_999);
        expect(fetchCalls).toBe(0);
        await advance(1); // old boundary (240 s) would have fired here
        expect(fetchCalls).toBe(0);
        await advance(600_000); // new boundary: 840 s = fresher exp - 60 s
        expect(fetchCalls).toBe(1);
    });

    it('hidden does NOT re-arm; the pending window still fires', async () => {
        vis = 'hidden';
        queue = [T0 / 1000 + 1200];
        expect(armAt(T0 / 1000 + 300)).toBe(true);
        await advance(100_000);
        document.dispatchEvent(new Event('visibilitychange'));
        await advance(139_999);
        expect(fetchCalls).toBe(0);
        await advance(1);
        expect(fetchCalls).toBe(1);
    });
});
