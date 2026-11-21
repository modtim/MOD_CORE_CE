/**
 *  — bound the Workflow
 * Builder token-refresh fetches and cache the OIDC discovery document.
 *
 * Owner saw ~10 s waits on every builder click on the Dell; the Dell log
 * showed 401 bursts + refreshes. The refresh path (tryRefreshToken ->
 * _bffRefresh / _keycloakDirectRefresh) had NO timeout on any of its three
 * fetches, and every 401-ing request awaits the SAME refreshInFlight promise,
 * so one stalled refresh froze every click. It also fetched the discovery
 * document on EVERY direct refresh (two sequential round trips per 401).
 *
 * RED before the fix: lib/api-client.ts exports neither REFRESH_FETCH_TIMEOUT_MS
 * nor resetDiscoveryCache, so the imports below throw
 * `TypeError: (0 , _apiclient.resetDiscoveryCache) is not a function`.
 *
 * Covers:
 *   1. a BFF refresh fetch that NEVER resolves is aborted at 5 s and the caller
 *      gets the existing failure path (null), not a hang
 *   2. a stalled direct discovery fetch -> null and the cache stays empty
 *      (the next refresh re-discovers)
 *   3. a stalled direct token-endpoint fetch -> failure path and the cached
 *      endpoint drops, so the next refresh re-discovers
 *   4. two consecutive direct refreshes fetch the discovery document ONCE
 *      (the token endpoint is still fetched twice)
 *   5. a failed token-endpoint response drops the cache -> next refresh
 *      re-discovers (a drifting LAN IP is never cached forever)
 *
 * Fake timers + a mocked global fetch: nothing real is fetched, no network.
 * Every refresh is awaited only AFTER the fake timers were advanced past the
 * 5 s watchdog, so a stalled fetch surfaces as a bounded failure, never a
 * deadlocked test.
 */

import {
    REFRESH_FETCH_TIMEOUT_MS,
    clearWorkflowTokens,
    getAccessToken,
    resetDiscoveryCache,
    saveWorkflowTokens,
    tryRefreshToken,
} from '@/lib/api-client';

const T0 = 1_700_000_000_000;

function makeJwt(expSeconds: number): string {
    const payload = btoa(JSON.stringify({ exp: expSeconds, sub: 'qa-owner' }));
    const b64 = payload.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

let fetchLog: string[] = [];
let endpointOk = true;

// Mocked transport: records every URL it is asked for. `hang` returns a
// promise that never resolves (a stalled connection); `endpointOk` toggles the
// Keycloak token endpoint between ok and failed.
function installFetchMock(opts: { hang: boolean }): void {
    (global as any).fetch = async (url: string): any => {
        fetchLog.push(url);
        if (opts.hang) return new Promise(() => {}); // stalled connection: never resolves
        if (url.includes('/auth/discovery')) {
            return { ok: true, json: async () => ({ token_endpoint: 'https://kc.example/token' }) };
        }
        if (url.includes('/auth/refresh-token')) {
            return { ok: true, json: async () => ({ access_token: makeJwt(1_800_000_600), refresh_token: 'rt-new' }) };
        }
        if (!endpointOk) return { ok: false, json: async () => ({}) };
        return { ok: true, json: async () => ({ access_token: makeJwt(1_800_000_900), refresh_token: 'rt-new' }) };
    };
}

async function advance(ms: number): Promise<void> {
    await jest.advanceTimersByTimeAsync(ms);
    await jest.advanceTimersByTimeAsync(0); // flush the mock-fetch microtask chain
}

// Start a refresh, let the fake clock pass the 5 s watchdog, THEN await it.
function runRefresh(): Promise<string | null> {
    const pending = tryRefreshToken();
    return advanceThenAwait(pending, REFRESH_FETCH_TIMEOUT_MS + 100);
}

async function advanceThenAwait(pending: Promise<string | null>, ms: number): Promise<string | null> {
    await advance(ms);
    return await pending;
}

function armTokens(client: string): void {
    sessionStorage.clear();
    saveWorkflowTokens(makeJwt(1_800_000_600), 'rt-stored', client);
}

function countDiscovery(): number {
    return fetchLog.filter((u) => u.includes('/auth/discovery')).length;
}

// This jest build has no `notToBeNull` matcher; assert non-null explicitly.
function expectNonNull(value: unknown): void {
    expect(value === null || value === undefined).toBe(false);
}

beforeEach(() => {
    jest.useFakeTimers();
    fetchLog = [];
    endpointOk = true;
    installFetchMock({ hang: false });
    resetDiscoveryCache();
});

afterEach(() => {
    clearWorkflowTokens();
    resetDiscoveryCache();
    sessionStorage.clear();
    jest.useRealTimers();
});

describe('refresh fetches are bounded', () => {
    it('REFRESH_FETCH_TIMEOUT_MS is 5 s and the cache resetter exists', () => {
        expect(REFRESH_FETCH_TIMEOUT_MS).toBe(5_000);
        expect(typeof resetDiscoveryCache).toBe('function');
    });

    it('a BFF refresh fetch that never resolves is aborted at 5 s -> failure path', async () => {
        armTokens('dashboard-client');
        installFetchMock({ hang: true });
        const pending = tryRefreshToken();
        await advance(REFRESH_FETCH_TIMEOUT_MS - 1);
        expect(fetchLog.filter((u) => u.includes('/auth/refresh-token'))).toHaveLength(1);
        expect(getAccessToken()).toBe(makeJwt(1_800_000_600)); // unchanged: nothing saved
        await advance(1);
        expect(await pending).toBeNull();
        expect(getAccessToken()).toBe(makeJwt(1_800_000_600));
    });

    it('a stalled direct discovery fetch -> null and the cache stays empty', async () => {
        armTokens('workflow-builder-client');
        installFetchMock({ hang: true });
        expect(await runRefresh()).toBeNull();
        // the cache must still be empty: the NEXT refresh re-discovers
        installFetchMock({ hang: false });
        expectNonNull(await runRefresh());
        expect(countDiscovery()).toBe(2);
    });

    it('a stalled direct token-endpoint fetch -> failure path and the cache drops', async () => {
        armTokens('workflow-builder-client');
        installFetchMock({ hang: false });
        expectNonNull(await runRefresh());
        installFetchMock({ hang: true });
        expect(await runRefresh()).toBeNull();
        // the cached endpoint must not be reused after a stall
        installFetchMock({ hang: false });
        expectNonNull(await runRefresh());
        expect(countDiscovery()).toBe(2);
    });
});

describe('discovery document is cached per tab', () => {
    it('two consecutive direct refreshes fetch discovery ONCE', async () => {
        armTokens('workflow-builder-client');
        expectNonNull(await runRefresh());
        expectNonNull(await runRefresh());
        expect(countDiscovery()).toBe(1);
        expect(fetchLog.filter((u) => !u.includes('/auth/discovery'))).toHaveLength(2);
    });

    it('a failed token-endpoint response drops the cache -> next refresh re-discovers', async () => {
        armTokens('workflow-builder-client');
        endpointOk = false;
        expect(await runRefresh()).toBeNull();
        endpointOk = true;
        expectNonNull(await runRefresh());
        expect(countDiscovery()).toBe(2);
    });
});
