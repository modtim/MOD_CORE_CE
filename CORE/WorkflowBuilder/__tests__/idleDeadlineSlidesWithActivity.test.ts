/**
 * FIX-WB-IDLE-DEADLINE-SLIDES-WITH-ACTIVITY (D72) — owner symptom: "I was within
 * the application, specifically in the workflow builder area and it logged me out.
 * I was doing work... I was doing work like 30 seconds before."
 *
 * For a NORMAL session wbDeadlineMs() returned the refresh token's `exp`
 * (deadline source "refreshExp") = login + ssoSessionIdleTimeout — an ABSOLUTE
 * cap. The hook attached its activity listeners only for the "idle" source, so
 * mouse/keys never moved that deadline and the builder logged the user out
 * while they were working.
 *
 * Covers:
 *   1. activity slides the deadline: min(refreshToken.exp, lastActivity + idleLimit)
 *   2. activity near the access-token expiry / deadline triggers the EXISTING
 *      refresh path exactly once (the grant slides the Keycloak session)
 *   3. no activity: the deadline still fires the logout
 *   4. the refresh token's `exp` still caps the deadline (no new limit invented)
 *   5. the mounted useIdleWarning hook wires it end to end (jest fake timers)
 */

import { act, renderHook } from '@testing-library/react';

import {
    IDLE_WARN_LEAD_MS,
    IDLE_WARN_REFRESH_MARGIN_MS,
    startIdleWarning,
    wbDeadlineMs,
    useIdleWarning,
} from '@/hooks/use-idle-warning';
import {
    ACCESS_TOKEN_KEY,
    REFRESH_TOKEN_KEY,
    saveWorkflowTokens,
} from '@/lib/api-client';

const T0 = 1_700_000_000_000; // fixed epoch: every expectation is absolute (ms)
const MIN = 60_000; // one minute, in ms

function makeJwt(expSeconds: number): string {
    const payload = btoa(JSON.stringify({ exp: expSeconds, sub: 'qa-owner' }));
    const b64 = payload.replace(/\+/g, '-').replace(/\//g, '/').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

// Normal session: refresh token WITH an `exp` (the Keycloak session's absolute cap).
// `accessMin` / `refreshMin` are minutes after login (T0).
function setSession(accessMin: number, refreshMin: number): void {
    saveWorkflowTokens(
        makeJwt(T0 / 1000 + accessMin * 60),
        makeJwt(T0 / 1000 + refreshMin * 60),
    );
}

// ---------------------------------------------------------------------------
// 1 + 2 + 3 + 4. Injected clock: the deadline slides with activity
// ---------------------------------------------------------------------------
describe('FIX D72 the refreshExp deadline slides with activity', () => {
    let clock: number;
    let queue: { at: number; fn: () => void }[];
    let opened: number[];
    let logouts: number;
    let refreshes: number;
    let fire: Array<() => void>;

    beforeEach(() => {
        clock = T0;
        queue = [];
        opened = [];
        logouts = 0;
        refreshes = 0;
        fire = [];
    });

    const advance = (ms: number) => {
        const target = clock + ms;
        for (;;) {
            const due = queue.filter((q) => q.at <= target).sort((a, b) => a.at - b.at);
            if (!due.length) break;
            const next = due[0];
            clock = next.at;
            queue = queue.filter((q) => q !== next);
            next.fn();
        }
        clock = target;
    };

    const start = (onActivityRefresh: () => void, idleLimitMs?: number) =>
        startIdleWarning({
            getDeadlineMs: () => wbDeadlineMs(
                idleLimitMs === undefined
                    ? { lastActivityMs: clock }
                    : { idleLimitMs, lastActivityMs: clock },
            ),
            getIdleLimitMs: () => idleLimitMs,
            activityTarget: {
                addEventListener: (_type: string, listener: () => void) => { fire.push(listener); },
                removeEventListener: (_type: string, _listener: () => void) => {},
            },
            onActivityRefresh,
            onStay: () => {},
            onSignOut: () => { logouts += 1; },
            onOpen: (secondsLeft) => { opened.push(secondsLeft); },
            onTick: () => {},
            onClose: () => {},
            now: () => clock,
            setTimer: (fn, ms) => {
                const entry = { at: clock + ms, fn };
                queue.push(entry);
                return entry;
            },
            clearTimer: (entry) => {
                const e = entry as { at: number; fn: () => void };
                queue = queue.filter((q) => q !== e);
            },
        });

    const stamp = () => { for (const f of fire) f(); };

    it('(a) activity at t=50 min moves the deadline: no logout at t=61 min', () => {
        setSession(15, 60); // access exp login+15min, refresh exp login+60min
        const h = start(() => {
            refreshes += 1;
            // the BFF refresh grant slides the Keycloak session: the tokens it
            // returns carry expiries measured from NOW (t=50min), not from login.
            setSession(50 + 15, 50 + 60);
        });
        expect(h.getDeadline()).toBe(T0 + 60 * MIN);
        advance(50 * MIN);
        stamp();
        expect(refreshes).toBe(1);
        expect(h.getDeadline()).toBe(T0 + 110 * MIN);
        advance(11 * MIN); // t = 61 min: the old absolute cap would have logged out
        expect(logouts).toBe(0);
        expect(opened).toEqual([]);
        expect(h.secondsLeft()).toBe(49 * 60);
        // the deadline really moved: the warning now opens 2 min before it
        advance(110 * MIN - IDLE_WARN_LEAD_MS - (clock - T0) - 1);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
        advance(IDLE_WARN_LEAD_MS);
        expect(logouts).toBe(1);
    });

    it('(b) no activity at all: logout at the deadline', () => {
        setSession(15, 60);
        const h = start(() => { refreshes += 1; });
        expect(h.getDeadline()).toBe(T0 + 60 * MIN);
        advance(60 * MIN - IDLE_WARN_LEAD_MS - 1);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
        advance(IDLE_WARN_LEAD_MS);
        expect(logouts).toBe(1);
        expect(refreshes).toBe(0);
    });

    it('(c) activity triggers exactly one refresh when within the margin', () => {
        setSession(15, 60);
        const h = start(() => {
            refreshes += 1;
            setSession(50 + 15, 50 + 60);
        });
        advance(50 * MIN);
        stamp();
        advance(MIN);
        stamp();
        advance(MIN);
        stamp();
        // three activity stamps (each past the 30 s throttle), one refresh: the
        // session slid, so nothing is inside the margin any more.
        expect(refreshes).toBe(1);
        expect(h.getDeadline()).toBe(T0 + 110 * MIN);
    });

    it('activity well inside the margins triggers no refresh (no storming the BFF)', () => {
        setSession(60, 120);
        const h = start(() => { refreshes += 1; });
        expect(h.getDeadline()).toBe(T0 + 60 * MIN);
        advance(10 * MIN);
        stamp();
        expect(refreshes).toBe(0);
        // the deadline slid with activity, still under the login+120min cap
        expect(h.getDeadline()).toBe(T0 + 70 * MIN);
    });

    it('the refresh token exp still caps the deadline (no new limit invented)', () => {
        setSession(15, 60);
        const h = start(() => {}); // a refresh that slides nothing: the cap is hard
        expect(h.getDeadline()).toBe(T0 + 60 * MIN);
        advance(45 * MIN);
        stamp();
        expect(h.getDeadline()).toBe(T0 + 60 * MIN); // activity never moves past the cap
        advance(15 * MIN - IDLE_WARN_LEAD_MS - 1);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
        advance(IDLE_WARN_LEAD_MS);
        expect(logouts).toBe(1);
    });

    it('a short idle limit slides the deadline while active, then follows the stall', () => {
        setSession(15, 180); // session cap login+180min
        const h = start(() => {}, 30 * MIN); // tier idle limit 30 min (GET /me)
        expect(h.getDeadline()).toBe(T0 + 30 * MIN);
        for (let i = 0; i < 5; i += 1) {
            advance(20 * MIN);
            stamp();
        }
        // 100 min of activity: deadline = lastActivity + 30 min, still under the cap
        expect(h.getDeadline()).toBe(T0 + 130 * MIN);
        advance(30 * MIN - IDLE_WARN_LEAD_MS - 1);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
    });
});

// ---------------------------------------------------------------------------
// 5. The mounted hook (jest fake timers drive the real setTimeout)
// ---------------------------------------------------------------------------
describe('FIX D72 useIdleWarning slides the deadline on real activity', () => {
    it('activity near the access-token expiry refreshes once and no logout follows', async () => {
        jest.useFakeTimers();
        try {
            const baseNow = Date.now();
            const jwtAt = (offsetMs: number) => makeJwt(Math.floor((baseNow + offsetMs) / 1000));
            // deadline = refresh exp login+180s; access token expires at login+30s
            sessionStorage.setItem(ACCESS_TOKEN_KEY, jwtAt(30_000));
            sessionStorage.setItem(REFRESH_TOKEN_KEY, jwtAt(180_000));

            let refreshCalls = 0;
            let opened = 0;
            (global as any).fetch = async (url: string) => {
                if (String(url).endsWith('/me')) {
                    return { ok: true, json: async () => ({ session_tier: { idle_seconds: 3600 } }) };
                }
                refreshCalls += 1;
                return {
                    ok: true,
                    json: async () => ({
                        access_token: jwtAt(30_000),
                        refresh_token: jwtAt(300_000), // the grant slid the session
                    }),
                };
            };

            const hook = renderHook(() =>
                useIdleWarning(
                    sessionStorage.getItem(ACCESS_TOKEN_KEY),
                    { onOpen: () => { opened += 1; }, onTick: () => {}, onClose: () => {} },
                ),
            );
            const handle = hook.result.current;
            await act(async () => {
                await jest.advanceTimersByTimeAsync(40_000);
            });
            expect(opened).toBe(0);
            window.dispatchEvent(new Event('pointerdown'));
            await act(async () => {
                await jest.advanceTimersByTimeAsync(21_000);
            });
            expect(refreshCalls).toBe(1);
            expect(opened).toBe(0);
            // still signed in past the old login+180s cap: tokens were not cleared
            expect(typeof sessionStorage.getItem(ACCESS_TOKEN_KEY)).toBe("string");
            expect(handle.secondsLeft()).toBeGreaterThan(180);
        } finally {
            jest.useRealTimers();
        }
    });
});
