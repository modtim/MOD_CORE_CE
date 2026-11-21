/**
 * BUG.B2c — the WB idle deadline must take its idle limit from the SAME source
 * the Dashboard uses (CORE/Dashboard/js/idle-warning.js + js/auth.js):
 *   (a) GET /me's session_tier.idle_seconds * 1000 (preferred)
 *   (b) the token response's refresh_expires_in * 1000 (fallback)
 *   (c) the documented 60-min normal-tier default (IDLE_WARN_DEFAULT_IDLE_MS)
 *
 * The owner (admin tier, 30 min idle) saw the Dashboard log idleLimitMs 1800000
 * while the WB logged 3600000 — the hook passed wbDeadlineMs WITHOUT its context,
 * so the fetched tier number never reached the deadline.
 */

import { renderHook } from '@testing-library/react';

import {
    IDLE_WARN_DEFAULT_IDLE_MS,
    lastDeadlineSourceKind,
    resolveIdleLimitMs,
    wbDeadlineMs,
    useIdleWarning,
} from '@/hooks/use-idle-warning';
import {
    ACCESS_TOKEN_KEY,
    REFRESH_TOKEN_KEY,
    clearWorkflowTokens,
} from '@/lib/api-client';

const T0 = 1_700_000_000_000; // fixed epoch: every expectation is absolute

function jwt(payload: Record<string, unknown>): string {
    const raw = btoa(JSON.stringify(payload));
    const b64 = raw.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

// A remember-me (Keycloak offline_access) refresh token: no usable `exp`, so the
// deadline is INACTIVITY-based and NEEDS the tier's idle limit.
function offlineJwt(): string {
    return jwt({ scope: 'openid offline_access', sub: 'qa-owner' });
}

describe('BUG.B2c idle-limit source (Dashboard parity)', () => {
    it('session_tier.idle_seconds=1800 -> idleLimitMs 1800000', () => {
        expect(resolveIdleLimitMs({ idleLimitMs: 1_800_000 })).toBe(1_800_000);
    });

    it('missing tier -> falls back to refresh_expires_in, like the Dashboard', () => {
        expect(resolveIdleLimitMs({ idleLimitMs: null, refreshExpiresInSec: 1800 })).toBe(1_800_000);
    });

    it('missing tier and refresh_expires_in -> the 60-min default', () => {
        expect(resolveIdleLimitMs({})).toBe(IDLE_WARN_DEFAULT_IDLE_MS);
    });

    it('wbDeadlineMs uses the tier number when handed the context', () => {
        clearWorkflowTokens();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, offlineJwt());
        expect(wbDeadlineMs({ idleLimitMs: 1_800_000, lastActivityMs: T0 })).toBe(T0 + 1_800_000);
        expect(lastDeadlineSourceKind()).toBe('idle');
    });

    it('wbDeadlineMs without the tier number falls back, like the Dashboard', () => {
        clearWorkflowTokens();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, offlineJwt());
        expect(wbDeadlineMs({ refreshExpiresInSec: 1800, lastActivityMs: T0 })).toBe(T0 + 1_800_000);
        expect(wbDeadlineMs({ lastActivityMs: T0 })).toBe(T0 + IDLE_WARN_DEFAULT_IDLE_MS);
    });
});

describe('BUG.B2c useIdleWarning takes /me session_tier.idle_seconds', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        clearWorkflowTokens();
        // offline_access refresh token (no exp) -> INACTIVITY deadline
        sessionStorage.setItem(REFRESH_TOKEN_KEY, offlineJwt());
        sessionStorage.setItem(ACCESS_TOKEN_KEY, jwt({ exp: Math.floor(Date.now() / 1000) + 900 }));
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    const mount = async () => {
        const hook = renderHook(() =>
            useIdleWarning(
                sessionStorage.getItem(ACCESS_TOKEN_KEY),
                { onOpen: () => {}, onTick: () => {}, onClose: () => {} },
            ),
        );
        // let the hook's /me fetch + arm microtasks complete
        await jest.advanceTimersByTimeAsync(10);
        return hook.result.current;
    };

    it('admin tier 30m: the mounted deadline is lastActivity + 1800000', async () => {
        (global as any).fetch = async () => ({
            ok: true,
            json: async () => ({ session_tier: { idle_seconds: 1800, tier: 'admin' } }),
        });
        const handle = await mount();
        const before = Date.now();
        const deadline = handle.getDeadline();
        expect(deadline).toBeGreaterThan(before);
        expect(deadline! - before).toBeCloseTo(1_800_000, -3);
        expect(lastDeadlineSourceKind()).toBe('idle');
    });

    it('no session_tier from /me -> the 60-min default still applies', async () => {
        (global as any).fetch = async () => ({ ok: true, json: async () => ({}) });
        const handle = await mount();
        const before = Date.now();
        const deadline = handle.getDeadline();
        expect(deadline).toBeGreaterThan(before);
        expect(deadline! - before).toBeCloseTo(IDLE_WARN_DEFAULT_IDLE_MS, -3);
    });
});
