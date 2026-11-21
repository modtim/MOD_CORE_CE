/**
 * GSEC FX.54 p4b — WB idle-warning dialog 2 minutes before the SSO idle
 * timeout (mirrors CORE/Dashboard/js/idle-warning.js).
 *
 * Covers:
 *   1. deadline source — the Keycloak refresh token's `exp` IS the idle
 *      deadline; access-token `exp` is the fallback when it is missing
 *   2. scheduling      — the dialog opens at deadline-120s and the countdown
 *      ticks once a second (injected clock, no real timers)
 *   3. Stay signed in  — runs the existing refresh, then re-arms against the
 *      deadline that refresh produced
 *   4. deadline        — nobody acts: the existing logout runs at the deadline
 *   5. hook mount      — the real useIdleWarning wires 1-4 into the app
 */

import { act, renderHook } from '@testing-library/react';

import {
    IDLE_WARN_DEFAULT_IDLE_MS,
    IDLE_WARN_LEAD_MS,
    formatCountdown,
    startIdleWarning,
    tokenExpMs,
    wbDeadlineMs,
    useIdleWarning,
} from '@/hooks/use-idle-warning';
import {
    ACCESS_TOKEN_KEY,
    REFRESH_TOKEN_KEY,
    saveWorkflowTokens,
    clearWorkflowTokens,
    tryRefreshToken,
} from '@/lib/api-client';

const T0 = 1_700_000_000_000; // fixed epoch: every expectation is absolute

function makeJwt(expSeconds: number): string {
    const payload = btoa(JSON.stringify({ exp: expSeconds, sub: 'qa-owner' }));
    const b64 = payload.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

// BUG.2b: a remember-me (Keycloak offline_access) refresh token. `scope` says
// offline_access and `exp` is absent (or 0) — Keycloak does not put the idle
// deadline on it, so it must never be read as one.
function makeJwtOffline(expSeconds: number | null): string {
    const claims: Record<string, unknown> = { exp: expSeconds, scope: 'openid offline_access', sub: 'qa-owner' };
    if (expSeconds === null) delete claims.exp;
    const payload = btoa(JSON.stringify(claims));
    const b64 = payload.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    return `header.${b64}.sig`;
}

function setTokensAt(expSeconds: number): void {
    saveWorkflowTokens(makeJwt(expSeconds), makeJwt(expSeconds));
}

// ---------------------------------------------------------------------------
// 1. Deadline source
// ---------------------------------------------------------------------------
describe('FX.54 p4b deadline source', () => {
    it('tokenExpMs reads the JWT exp in ms', () => {
        expect(tokenExpMs(makeJwt(T0 / 1000 + 600))).toBe(T0 + 600_000);
    });

    it('refresh-token exp wins over the access-token exp', () => {
        setTokensAt(T0 / 1000 + 600);
        sessionStorage.setItem(ACCESS_TOKEN_KEY, makeJwt(T0 / 1000 + 300));
        expect(wbDeadlineMs()).toBe(T0 + 600_000);
    });

    it('BUG.2b: a refresh token with no usable exp → idle deadline, never the access exp', () => {
        sessionStorage.setItem(ACCESS_TOKEN_KEY, makeJwt(T0 / 1000 + 300));
        sessionStorage.setItem(REFRESH_TOKEN_KEY, 'not-a-jwt');
        expect(wbDeadlineMs({ lastActivityMs: T0 })).toBe(T0 + IDLE_WARN_DEFAULT_IDLE_MS);
    });

    it('the access-token exp is the deadline only when there is no refresh token at all', () => {
        sessionStorage.clear();
        sessionStorage.setItem(ACCESS_TOKEN_KEY, makeJwt(T0 / 1000 + 300));
        expect(wbDeadlineMs()).toBe(T0 + 300_000);
    });

    it('no tokens at all → no deadline', () => {
        sessionStorage.clear();
        expect(wbDeadlineMs()).toBeNull();
    });
});

// ---------------------------------------------------------------------------
// 1b. BUG.2b — remember-me (offline_access) sessions: INACTIVITY deadline
// ---------------------------------------------------------------------------
describe('BUG.2b idle deadline for remember-me sessions', () => {
    it('offline token without exp → idle deadline, not the access exp', () => {
        sessionStorage.clear();
        sessionStorage.setItem(ACCESS_TOKEN_KEY, makeJwt(T0 / 1000 + 300));
        sessionStorage.setItem(REFRESH_TOKEN_KEY, makeJwtOffline(T0 / 1000 + 300));
        expect(wbDeadlineMs({ lastActivityMs: T0 })).toBe(T0 + IDLE_WARN_DEFAULT_IDLE_MS);
    });

    it('refresh_expires_in beats the 60-min default, /me idle_seconds beats both', () => {
        sessionStorage.clear();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, makeJwtOffline(T0 / 1000 + 300));
        expect(wbDeadlineMs({ lastActivityMs: T0, refreshExpiresInSec: 1800 })).toBe(T0 + 1_800_000);
        expect(wbDeadlineMs({ lastActivityMs: T0, idleLimitMs: 900_000, refreshExpiresInSec: 1800 })).toBe(T0 + 900_000);
    });

    it('activity pushes the deadline out', () => {
        sessionStorage.clear();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, makeJwtOffline(T0 / 1000 + 300));
        expect(wbDeadlineMs({ lastActivityMs: T0 + 600_000 })).toBe(T0 + 600_000 + IDLE_WARN_DEFAULT_IDLE_MS);
    });

    it('refresh token with exp is unchanged', () => {
        sessionStorage.clear();
        setTokensAt(T0 / 1000 + 600);
        expect(wbDeadlineMs()).toBe(T0 + 600_000);
    });

    it('a refresh token WITH exp but an offline_access scope still goes idle', () => {
        sessionStorage.clear();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, makeJwtOffline(T0 / 1000 + 600));
        sessionStorage.setItem(ACCESS_TOKEN_KEY, makeJwt(T0 / 1000 + 300));
        expect(wbDeadlineMs({ lastActivityMs: T0 })).toBe(T0 + IDLE_WARN_DEFAULT_IDLE_MS);
    });
});

// ---------------------------------------------------------------------------
// 2 + 3 + 4. Scheduling, with a fully injected clock
// ---------------------------------------------------------------------------
describe('FX.54 p4b scheduling', () => {
    let clock: number;
    let queue: { at: number; fn: () => void }[];
    let opened: number[];
    let ticks: number[];
    let closed: number;
    let stays: number;
    let logouts: number;

    beforeEach(() => {
        clock = T0;
        queue = [];
        opened = [];
        ticks = [];
        closed = 0;
        stays = 0;
        logouts = 0;
    });

    const start = () =>
        startIdleWarning({
            getDeadlineMs: () => wbDeadlineMs(),
            onStay: () => { stays += 1; },
            onSignOut: () => { logouts += 1; },
            onOpen: (secondsLeft) => { opened.push(secondsLeft); },
            onTick: (secondsLeft) => { ticks.push(secondsLeft); },
            onClose: () => { closed += 1; },
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

    it('opens exactly at deadline-120s and announces 2 min', () => {
        setTokensAt(T0 / 1000 + 300);
        const h = start();
        expect(h.getDeadline()).toBe(T0 + 300_000);
        advance(300_000 - IDLE_WARN_LEAD_MS - 1);
        expect(h.isOpen()).toBe(false);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
        expect(h.isOpen()).toBe(true);
    });

    it('countdown ticks once a second while open', () => {
        setTokensAt(T0 / 1000 + 300);
        const h = start();
        advance(300_000 - IDLE_WARN_LEAD_MS);
        advance(30_000);
        expect(ticks.length).toBe(31);
        expect(ticks[0]).toBe(120);
        expect(ticks[30]).toBe(90);
        expect(formatCountdown(h.secondsLeft())).toBe('1 min 30 sec');
    });

    it('Stay signed in runs the refresh and re-arms against the new deadline', async () => {
        setTokensAt(T0 / 1000 + 300);
        // onStay stands in for tryRefreshToken: it writes fresh tokens, which
        // slide the deadline; the scheduler re-reads getDeadlineMs after it.
        const h = startIdleWarning({
            getDeadlineMs: () => wbDeadlineMs(),
            onStay: () => { stays += 1; setTokensAt(T0 / 1000 + 600); },
            onSignOut: () => { logouts += 1; },
            onOpen: (secondsLeft) => { opened.push(secondsLeft); },
            onTick: (secondsLeft) => { ticks.push(secondsLeft); },
            onClose: () => { closed += 1; },
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
        advance(300_000 - IDLE_WARN_LEAD_MS);
        expect(opened).toEqual([120]);
        await h.stay();
        expect(stays).toBe(1);
        expect(closed).toBe(1);
        expect(h.isOpen()).toBe(false);
        expect(h.getDeadline()).toBe(T0 + 600_000);
        advance(600_000 - 180_000 - 1);
        expect(opened.length).toBe(2);
        expect(opened[1]).toBe(120);
        expect(logouts).toBe(0);
    });

    it('an ignored warning logs out at the deadline', () => {
        setTokensAt(T0 / 1000 + 300);
        const h = start();
        advance(300_000 - IDLE_WARN_LEAD_MS);
        advance(180_000);
        expect(logouts).toBe(1);
        expect(h.isOpen()).toBe(false);
        expect(h.secondsLeft()).toBe(0);
        advance(60_000);
        expect(logouts).toBe(1);
    });

    it('a deadline already past logs out immediately', () => {
        setTokensAt(T0 / 1000 - 10);
        start();
        expect(logouts).toBe(1);
        expect(opened).toEqual([]);
    });

    it('remember-me session: activity keeps pushing the deadline out, no dialog while active', () => {
        sessionStorage.clear();
        sessionStorage.setItem(REFRESH_TOKEN_KEY, makeJwtOffline(T0 / 1000 + 300));
        const fire: Array<() => void> = [];
        let removed = 0;
        const target = {
            addEventListener: (_type: string, listener: () => void) => { fire.push(listener); },
            removeEventListener: (_type: string, _listener: () => void) => { removed += 1; },
        };
        const h = startIdleWarning({
            getDeadlineMs: () => wbDeadlineMs({ lastActivityMs: clock }),
            getIdleLimitMs: () => null,
            activityTarget: target,
            onStay: () => { stays += 1; },
            onSignOut: () => { logouts += 1; },
            onOpen: (secondsLeft) => { opened.push(secondsLeft); },
            onTick: (secondsLeft) => { ticks.push(secondsLeft); },
            onClose: () => { closed += 1; },
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
        expect(h.getDeadline()).toBe(T0 + IDLE_WARN_DEFAULT_IDLE_MS);
        // The owner keeps working: one activity stamp a minute for 59 minutes.
        for (let i = 0; i < 59; i += 1) {
            advance(60_000);
            for (const stamp of fire) stamp();
        }
        expect(opened).toEqual([]);
        expect(h.isOpen()).toBe(false);
        expect(h.getDeadline()).toBe(T0 + 59 * 60_000 + IDLE_WARN_DEFAULT_IDLE_MS);
        // Now they go idle: the dialog opens 2 min before the pushed-out deadline.
        advance(IDLE_WARN_DEFAULT_IDLE_MS - IDLE_WARN_LEAD_MS - 1);
        expect(opened).toEqual([]);
        advance(1);
        expect(opened).toEqual([120]);
        h.stop();
        expect(removed).toBe(fire.length);
    });
});
// ---------------------------------------------------------------------------
// 5. The real hook, mounted (jest fake timers drive the real setTimeout)
// ---------------------------------------------------------------------------
describe('FX.54 p4b useIdleWarning wiring', () => {
    let opened: number[];
    let closed: number;

    beforeEach(() => {
        jest.useFakeTimers();
        opened = [];
        closed = 0;
        sessionStorage.clear();
        // deadline = now + 150s → inside the 2-min lead, so it opens on mount
        const exp = Math.floor(Date.now() / 1000) + 150;
        saveWorkflowTokens(makeJwt(exp), makeJwt(exp));
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    const fakeRefresh = (expSeconds: number) => {
        (global as any).fetch = async () => ({
            ok: true,
            json: async () => ({
                access_token: makeJwt(expSeconds),
                refresh_token: makeJwt(expSeconds),
            }),
        });
    };

    it('mount opens at deadline-120s', async () => {
        const hook = renderHook(() =>
            useIdleWarning(
                sessionStorage.getItem(ACCESS_TOKEN_KEY),
                {
                    onOpen: (secondsLeft) => { opened.push(secondsLeft); },
                    onTick: () => {},
                    onClose: () => { closed += 1; },
                },
            ),
        );
        await act(async () => {
            await jest.advanceTimersByTimeAsync(30_000);
        });
        expect(opened).toEqual([120]);
        expect(closed).toBe(0);
    });

    it('Stay runs the refresh, re-arms, and the new deadline is honoured', async () => {
        const exp = Math.floor(Date.now() / 1000);
        fakeRefresh(exp + 600);
        const hook = renderHook(() =>
            useIdleWarning(
                sessionStorage.getItem(ACCESS_TOKEN_KEY),
                {
                    onOpen: (secondsLeft) => { opened.push(secondsLeft); },
                    onTick: () => {},
                    onClose: () => { closed += 1; },
                },
            ),
        );
        const handle = hook.result.current;
        await act(async () => {
            await jest.advanceTimersByTimeAsync(30_000);
            handle.stay();
            await jest.advanceTimersByTimeAsync(0);
        });
        expect(closed).toBe(1);
        expect(sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBe(makeJwt(exp + 600));
        await act(async () => {
            await jest.advanceTimersByTimeAsync(600_000);
        });
        expect(opened.length).toBe(2);
    });

    it('the deadline logs out through the existing logout', async () => {
        const hook = renderHook(() =>
            useIdleWarning(
                sessionStorage.getItem(ACCESS_TOKEN_KEY),
                {
                    onOpen: (secondsLeft) => { opened.push(secondsLeft); },
                    onTick: () => {},
                    onClose: () => { closed += 1; },
                },
            ),
        );
        const handle = hook.result.current;
        await act(async () => {
            await jest.advanceTimersByTimeAsync(150_000);
        });
        expect(handle.isOpen()).toBe(false);
        expect(sessionStorage.getItem(ACCESS_TOKEN_KEY)).toBeNull();
        expect(sessionStorage.getItem(REFRESH_TOKEN_KEY)).toBeNull();
    });
});
