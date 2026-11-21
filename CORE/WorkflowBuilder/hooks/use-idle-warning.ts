"use client";

// GSEC FX.54 p4b — Workflow Builder idle-warning dialog, 2 minutes before the
// SSO idle timeout. Mirrors CORE/Dashboard/js/idle-warning.js (the model).
//
// Keycloak's session_idle_timeout logs a user out after that much INACTIVITY,
// measured server-side — mouse moves never reset it. The refresh token's `exp`
// IS that deadline
// (FX.54.p3d syncs the realm session lifetimes from the auth.* platform
// settings), so we schedule the dialog at deadline - 120s and the logout
// itself at the deadline.
//
// BUG.2b (owner saw it on the Dashboard first): a remember-me session's
// refresh token is a Keycloak `offline_access` token with NO usable `exp`
// (absent or 0), so falling back to the short access-token `exp` made the
// dialog fire on every access-token life — the token is refreshed
// proactively, so the deadline kept sliding back. For those sessions the
// deadline is INACTIVITY-based: lastActivityMs + idleLimitMs, which is what
// the server enforces (session_tier_enforcement.py). The idle limit comes
// from GET /me's `session_tier.idle_seconds`, then the token response's
// `refresh_expires_in`, then the documented 60-min normal-tier default.
// Passive throttled listeners (pointerdown/keydown/wheel, one update per
// 30s, timestamp only — removed on unmount) slide the deadline forward
// while the user works, and the dialog never opens while they are active.
// A refresh token WITH an `exp` keeps the original behaviour exactly.
//
// "Stay signed in" = the existing refresh (lib/api-client tryRefreshToken).
// "Sign out"      = the existing logout (broadcastLogout + forceReauth).
// At the deadline, log out. After every refresh the deadline is re-read and
// the whole schedule re-armed (a refresh slides the deadline forward).
//
// Everything time-related is injectable (now/setTimer/clearTimer) so the unit
// tests can drive a fake clock; useIdleWarning passes the real globals.

import { useEffect, useRef } from "react";
import {
    getRefreshToken,
    getAccessToken,
    tryRefreshToken,
    broadcastLogout,
    forceReauth,
    TOKEN_REFRESHED_EVENT,
    ACCESS_TOKEN_KEY,
    REFRESH_TOKEN_KEY,
} from "@/lib/api-client";
import { apiUrl } from "@/lib/runtime-config";

export const IDLE_WARN_LEAD_MS = 120 * 1000;
export const IDLE_WARN_COUNTDOWN_STEP_MS = 1000;

// BUG.2b — last-resort idle limit for remember-me (Keycloak offline_access)
// sessions whose refresh token carries no usable `exp`. Mirrors the NORMAL
// session tier's idle limit: auth.sso_session_idle_seconds = 3600 s / 60 min
// (CORE/API/services/session_tier_enforcement.py, USER_TIER_SETTING_KEYS).
// Only used when neither GET /me's session_tier.idle_seconds nor the token
// response's refresh_expires_in is available.
export const IDLE_WARN_DEFAULT_IDLE_MS = 60 * 60 * 1000;
// Activity listeners are passive: they only stamp a timestamp, never a timer,
// and at most once per this window, so a moving mouse cannot storm the
// scheduler.
export const IDLE_WARN_ACTIVITY_THROTTLE_MS = 30 * 1000;
export const IDLE_WARN_ACTIVITY_TYPES = ["pointerdown", "keydown", "wheel"];
// FIX D72 (owner: "it logged me out... I was doing work like 30 seconds before") —
// when an activity stamp finds the ACCESS token this close to its `exp`, or the
// deadline this close, the builder calls the EXISTING refresh path (lib/api-client
// tryRefreshToken -> TOKEN_REFRESHED_EVENT -> rearm). The BFF refresh is a Keycloak
// refresh grant, which slides the ONLINE session, so the refresh token's `exp`
// (the session's absolute cap) moves later and the user stays in while working.
export const IDLE_WARN_REFRESH_MARGIN_MS = 90 * 1000;

export function tokenExpMs(token: string | null): number | null {
    if (!token) return null;
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    try {
        let padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
        while (padded.length % 4 !== 0) padded += "=";
        const json = typeof atob === "function" ? atob(padded) : Buffer.from(padded, "base64").toString("utf8");
        const payload = JSON.parse(json) as Record<string, unknown>;
        const exp = payload.exp;
        return typeof exp === "number" && Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
    } catch {
        return null;
    }
}

export function tokenScope(token: string | null): string {
    if (!token) return "";
    const parts = token.split(".");
    if (parts.length !== 3) return "";
    try {
        let padded = parts[1].replace(/-/g, "+").replace(/_/g, "/");
        while (padded.length % 4 !== 0) padded += "=";
        const json = typeof atob === "function" ? atob(padded) : Buffer.from(padded, "base64").toString("utf8");
        const payload = JSON.parse(json) as Record<string, unknown>;
        return typeof payload.scope === "string" ? payload.scope : "";
    } catch {
        return "";
    }
}

export function isOfflineAccessSession(token: string | null): boolean {
    return tokenScope(token).split(" ").includes("offline_access");
}

// Optional context a caller can hand wbDeadlineMs about the current session,
// from data it already has (no extra fetches):
//   idleLimitMs          — GET /me's session_tier.idle_seconds * 1000 (preferred)
//   refreshExpiresInSec  — the token response's refresh_expires_in (fallback)
//   offlineAccess        — true when the session is remember-me/offline_access
//   lastActivityMs       — overrides the module's own activity tracker
export interface IdleDeadlineContext {
    idleLimitMs?: number | null;
    refreshExpiresInSec?: number | null;
    offlineAccess?: boolean | null;
    lastActivityMs?: number | null;
}

// Idle limit resolution order (mirrors CORE/Dashboard/js/idle-warning.js
// idleLimitFromContext): (a) GET /me's session_tier.idle_seconds * 1000,
// (b) the token response's refresh_expires_in * 1000, (c) the documented
// 60-min normal-tier default. Never 0/negative.
export function resolveIdleLimitMs(context: IdleDeadlineContext): number {
    const idle = context.idleLimitMs;
    if (typeof idle === "number" && Number.isFinite(idle) && idle > 0) return idle;
    const rxi = context.refreshExpiresInSec;
    if (typeof rxi === "number" && Number.isFinite(rxi) && rxi > 0) return rxi * 1000;
    return IDLE_WARN_DEFAULT_IDLE_MS;
}

// One-time console note naming the chosen source (not a warning per arm).
let lastDeadlineSource: string | null = null;
function setDeadlineSource(source: string | null, idleLimitMs?: number): void {
    const changed = lastDeadlineSource !== source;
    lastDeadlineSource = source;
    if (changed) {
        console.info("WB idle-warning: session deadline source",
            idleLimitMs === undefined ? { source } : { source, idleLimitMs });
    }
}
export function lastDeadlineSourceKind(): string | null {
    return lastDeadlineSource;
}

export function wbDeadlineMs(context: IdleDeadlineContext = {}): number | null {
    const refreshRaw = getRefreshToken();
    const refreshExp = tokenExpMs(refreshRaw);
    if (refreshExp !== null && !isOfflineAccessSession(refreshRaw)) {
        // FIX D72 — the refresh token's `exp` is the Keycloak session's ABSOLUTE
        // cap (login + ssoSessionIdleTimeout), not an idle deadline: the owner was
        // logged out at it while working. The deadline is
        // min(cap, lastActivity + idleLimit) — it slides with activity (the
        // listeners now attach for this source too, and a refresh on activity
        // slides the cap itself), and a true idle logout still fires after the
        // tier's idle limit of NO activity. `idleLimitMs` is the value the hook
        // already reads (GET /me session_tier.idle_seconds -> refresh_expires_in
        // -> the documented 60-min default); no new platform setting.
        const idleLimitMs = resolveIdleLimitMs(context);
        const lastActivity = typeof context.lastActivityMs === "number" && Number.isFinite(context.lastActivityMs)
            ? context.lastActivityMs
            : Date.now();
        const idleDeadline = lastActivity + idleLimitMs;
        setDeadlineSource("refreshExp");
        return idleDeadline < refreshExp ? idleDeadline : refreshExp;
    }
    // BUG.2b: a remember-me (Keycloak offline_access) refresh token carries no
    // usable `exp` — absent or 0 — so its `exp` is NOT an idle deadline, and the
    // ACCESS-token exp is not one either (the access token is refreshed
    // proactively, so that deadline kept sliding back). For those sessions the
    // deadline is INACTIVITY-based: lastActivityMs + idleLimitMs, which is what
    // the server enforces (session_tier_enforcement.py).
    if (refreshRaw !== null || context.offlineAccess === true) {
        const idleLimitMs = resolveIdleLimitMs(context);
        const lastActivity = typeof context.lastActivityMs === "number" && Number.isFinite(context.lastActivityMs)
            ? context.lastActivityMs
            : Date.now();
        setDeadlineSource("idle", idleLimitMs);
        return lastActivity + idleLimitMs;
    }
    const accessExp = tokenExpMs(getAccessToken());
    setDeadlineSource(accessExp !== null ? "accessToken" : null);
    return accessExp;
}

export function formatCountdown(seconds: number): string {
    const s = Math.max(0, Math.round(seconds));
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m <= 0) return `${r} sec`;
    return r === 0 ? `${m} min` : `${m} min ${r} sec`;
}

export interface IdleWarningOptions {
    getDeadlineMs: () => number | null;
    onStay: () => unknown;
    onSignOut: () => void;
    onOpen: (secondsLeft: number) => void;
    onTick: (secondsLeft: number) => void;
    onClose: () => void;
    getIdleLimitMs?: () => number | null | undefined;
    // FIX D72 — the caller's refresh (the existing lib/api-client tryRefreshToken
    // path). Called on ACTIVITY when the access token / deadline / session cap is
    // inside IDLE_WARN_REFRESH_MARGIN_MS; the grant slides the Keycloak session.
    onActivityRefresh?: () => unknown;
    activityTarget?: unknown;
    now?: () => number;
    setTimer?: (fn: () => void, ms: number) => unknown;
    clearTimer?: (handle: unknown) => void;
}

export interface IdleWarningHandle {
    stay: () => void;
    signOut: () => void;
    isOpen: () => boolean;
    secondsLeft: () => number;
    getDeadline: () => number | null;
    rearm: (deadlineMs?: number | null) => boolean;
    stop: () => void;
}

interface ActivityTargetLike {
    addEventListener: (type: string, listener: () => void) => void;
    removeEventListener: (type: string, listener: () => void) => void;
}

export function startIdleWarning(opts: IdleWarningOptions): IdleWarningHandle {
    const { getDeadlineMs, onStay, onSignOut, onOpen, onTick, onClose } = opts;
    const onActivityRefresh = opts.onActivityRefresh;
    const now = opts.now ?? Date.now;
    const setTimer = opts.setTimer ?? ((fn: () => void, ms: number) => (setTimeout(fn, ms) as unknown));
    const clearTimer = opts.clearTimer ?? ((h: unknown) => { void clearTimeout(h as unknown as number); });
    const activityTarget = opts.activityTarget as ActivityTargetLike | null | undefined;

    const state = { deadline: null as number | null, live: true, open: false };
    const timers = new Set<unknown>();

    // INACTIVITY mode: the deadline came from lastActivity + idleLimit (remember-
    // me session, refresh token with no usable exp), not a token `exp`. Keycloak's
    // session_idle_timeout is measured server-side against ACTIVITY, so the
    // deadline slides forward while the user keeps working — the dialog must
    // never fire while they are active.
    let idleMode = false;
    // FIX D72 — ACTIVITY mode covers BOTH idle sources AND the normal session's
    // `refreshExp` deadline: the listeners must attach there too, and a stamp must
    // slide that deadline up to the refresh token's `exp` (the Keycloak session's
    // absolute cap). Without this the owner's "kicked out while working" happens.
    let activityMode = false;
    let lastActivity: number | null = null;
    let activityStamp = -IDLE_WARN_ACTIVITY_THROTTLE_MS;
    let refreshPending = false;
    const idleLimitMs = () => (opts.getIdleLimitMs ? opts.getIdleLimitMs() : null) || IDLE_WARN_DEFAULT_IDLE_MS;

    // The refresh token's `exp` for a NORMAL session — the session's absolute cap.
    const sessionCapMs = () => {
        const rt = getRefreshToken();
        const exp = tokenExpMs(rt);
        return exp !== null && !isOfflineAccessSession(rt) ? exp : null;
    };

    // FIX D72 — the deadline is min(session cap, stamp + idle limit): it slides
    // with activity, and the refresh token's `exp` (the Keycloak session's
    // absolute cap) still caps it. A tighter idle limit may pull it back, but
    // never to the past: while the user is active it always stays ahead of now.
    const slideDeadlineTo = (stamp: number) => {
        const cap = sessionCapMs();
        let next = stamp + idleLimitMs();
        if (cap !== null && next > cap) next = cap;
        if (next <= now()) return; // already past: let the scheduled deadline fire
        if (state.deadline === null || next !== state.deadline) {
            state.deadline = next;
            clearAllTimers();
            arm();
        }
    };

    // FIX D72 — activity is proof of use: when the ACCESS token (or the deadline
    // itself) is inside the margin, call the caller's refresh (the existing
    // lib/api-client tryRefreshToken path). The grant slides the Keycloak session,
    // so the cap moves and the user stays in. One attempt per due window.
    const requestRefreshIfDue = (stamp: number) => {
        if (!onActivityRefresh || refreshPending) return;
        const accessExp = tokenExpMs(getAccessToken());
        const cap = sessionCapMs();
        const nearAccess = accessExp !== null && accessExp - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        const nearDeadline = state.deadline !== null && state.deadline - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        const nearCap = cap !== null && cap - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        if (!nearAccess && !nearDeadline && !nearCap) return;
        refreshPending = true;
        try {
            const attempt = onActivityRefresh();
            if (attempt !== null && typeof attempt === "object" && typeof (attempt as any).then === "function") {
                void (attempt as any).then(
                    () => { refreshPending = false; },
                    () => { refreshPending = false; },
                );
            } else {
                refreshPending = false;
            }
        } catch {
            refreshPending = false;
        }
    };

    const noteActivity = (stamp: number) => {
        if (!activityMode || !state.live) return;
        if (stamp - activityStamp < IDLE_WARN_ACTIVITY_THROTTLE_MS) return;
        activityStamp = stamp;
        lastActivity = stamp;
        requestRefreshIfDue(stamp);
        slideDeadlineTo(stamp);
    };

    const attachedListeners: Array<[string, () => void]> = [];
    const attachActivityListeners = () => {
        if (!activityTarget || !activityMode) return;
        for (const type of IDLE_WARN_ACTIVITY_TYPES) {
            const listener = () => { void noteActivity(now()); };
            try {
                activityTarget.addEventListener(type, listener);
                attachedListeners.push([type, listener]);
            } catch { /* no listener support: activity simply isn't tracked */ }
        }
    };
    const detachActivityListeners = () => {
        if (!activityTarget) return;
        for (const [type, listener] of attachedListeners) {
            try { activityTarget.removeEventListener(type, listener); } catch { /* already gone */ }
        }
        attachedListeners.length = 0;
    };

    const clearAllTimers = () => {
        for (const handle of timers) {
            try { clearTimer(handle); } catch { /* already fired */ }
        }
        timers.clear();
    };

    const schedule = (ms: number, fn: () => void) => {
        let handle: unknown = null;
        handle = setTimer(() => {
            timers.delete(handle);
            if (state.live) fn();
        }, Math.max(0, ms));
        timers.add(handle);
    };

    const closeDialog = () => {
        if (!state.open) return;
        state.open = false;
        try { onClose(); } catch { /* component gone */ }
    };

    const secondsLeft = () =>
        state.deadline === null ? 0 : Math.max(0, Math.ceil((state.deadline - now()) / 1000));

    const tick = () => {
        if (!state.open || !state.live) return;
        try { onTick(secondsLeft()); } catch { /* component gone */ }
        schedule(IDLE_WARN_COUNTDOWN_STEP_MS, tick);
    };

    const openDialog = () => {
        if (!state.live) return;
        state.open = true;
        try { onOpen(secondsLeft()); } catch { /* component gone */ }
        tick();
    };

    const onDeadline = () => {
        closeDialog();
        state.live = false;
        clearAllTimers();
        try { onSignOut(); } catch { /* logout threw */ }
    };

    const arm = () => {
        if (!state.live || state.deadline === null) return;
        if (activityMode && activityTarget) {
            // Never warn while the user is active: if they have worked since the
            // deadline was computed, push the deadline out first — but never past
            // the refresh token's `exp` (the session's absolute cap).
            const cap = sessionCapMs();
            if (lastActivity !== null) {
                let slid = lastActivity + idleLimitMs();
                if (cap !== null && slid > cap) slid = cap;
                if (slid > state.deadline) state.deadline = slid;
            }
            const capped = cap !== null && state.deadline >= cap;
            if (state.deadline - IDLE_WARN_LEAD_MS <= now() && lastActivity !== null
                && now() - lastActivity < idleLimitMs() - IDLE_WARN_LEAD_MS && !capped) {
                // Still comfortably active: wait until activity actually stalls.
                schedule(idleLimitMs() - IDLE_WARN_LEAD_MS - (now() - lastActivity), arm);
                return;
            }
        }
        const t0 = now();
        const lead = state.deadline - IDLE_WARN_LEAD_MS;
        if (lead > t0) {
            schedule(lead - t0, openDialog);
            schedule(state.deadline - t0, onDeadline);
        } else if (state.deadline > t0) {
            openDialog();
            schedule(state.deadline - t0, onDeadline);
        } else {
            onDeadline();
        }
    };

    const stay = async () => {
        if (!state.open) return;
        closeDialog();
        try { await onStay(); } catch { console.warn("WB idle-warning: stay-signed-in refresh failed"); }
        clearAllTimers();
        const next = getDeadlineMs();
        if (next !== null && next !== undefined) state.deadline = next;
        arm();
    };

    const signOut = () => {
        if (!state.open) return;
        closeDialog();
        state.live = false;
        clearAllTimers();
        try { onSignOut(); } catch { /* logout threw */ }
    };

    const first = getDeadlineMs();
    if (first !== null && first !== undefined) state.deadline = first;
    const sourceKind = lastDeadlineSourceKind();
    idleMode = sourceKind === "idle";
    // FIX D72 — the listeners (and the sliding deadline) also apply to the normal
    // session's `refreshExp` source; the refresh token's `exp` still caps it.
    activityMode = sourceKind === "idle" || sourceKind === "refreshExp";
    if (idleMode) lastActivity = state.deadline === null ? null : state.deadline - idleLimitMs();
    attachActivityListeners();
    arm();

    return {
        stay,
        signOut,
        isOpen: () => state.open,
        secondsLeft,
        getDeadline: () => state.deadline,
        rearm: (deadlineMs) => {
            const next = deadlineMs === undefined || deadlineMs === null ? getDeadlineMs() : deadlineMs;
            if (!state.live) return false;
            if (next === null || next === undefined) return false;
            state.deadline = next;
            const kind = lastDeadlineSourceKind();
            idleMode = kind === "idle";
            // FIX D72 — the sliding window also applies to the refreshExp source.
            activityMode = kind === "idle" || kind === "refreshExp";
            if (idleMode && lastActivity === null) lastActivity = next - idleLimitMs();
            if (activityMode && attachedListeners.length === 0) attachActivityListeners();
            clearAllTimers();
            arm();
            return true;
        },
        stop: () => {
            state.live = false;
            detachActivityListeners();
            clearAllTimers();
            closeDialog();
        },
    };
}

export interface IdleWarningRenderProps {
    onOpen: (secondsLeft: number) => void;
    onTick: (secondsLeft: number) => void;
    onClose: () => void;
}

export function useIdleWarning(
    accessToken: string | null,
    render: IdleWarningRenderProps,
): IdleWarningHandle {
    const activeRef = useRef<IdleWarningHandle | null>(null);
    const proxyRef = useRef<IdleWarningHandle | null>(null);
    if (proxyRef.current === null) {
        proxyRef.current = {
            stay: () => { activeRef.current?.stay(); },
            signOut: () => { activeRef.current?.signOut(); },
            isOpen: () => activeRef.current?.isOpen() ?? false,
            secondsLeft: () => activeRef.current?.secondsLeft() ?? 0,
            getDeadline: () => activeRef.current?.getDeadline() ?? null,
            rearm: (deadlineMs) => activeRef.current?.rearm(deadlineMs) ?? false,
            stop: () => { activeRef.current?.stop(); },
        };
    }

    useEffect(() => {
        if (!accessToken) return;
        // BUG.2b (BUG.B2c): the IDLE deadline needs the tier's idle limit, from the
        // SAME source the Dashboard uses — a single context object handed to wbDeadlineMs
        // on every call (mirrors CORE/Dashboard/js/auth.js sessionDeadlineContext +
        // deadlineFromSession(sessionStorage, sessionDeadlineContext)). GET /me already
        // returns it (`session_tier.idle_seconds`, services/session_tier_enforcement.py
        // tier_for_response) — one fetch per auth session, no polling. When it is
        // unavailable wbDeadlineMs falls back to refresh_expires_in, then to the
        // documented 60-min normal-tier default.
        const sessionDeadlineContext: IdleDeadlineContext = { idleLimitMs: null, refreshExpiresInSec: null };
        const fetchTier = async () => {
            try {
                const resp = await fetch(`${apiUrl()}/me`, {
                    headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
                });
                if (!resp.ok) return;
                const data = await resp.json();
                const idle = (data as { session_tier?: { idle_seconds?: number | null } })
                    ?.session_tier?.idle_seconds;
                if (typeof idle === "number" && Number.isFinite(idle) && idle > 0) sessionDeadlineContext.idleLimitMs = idle * 1000;
            } catch { /* settings read failure — wbDeadlineMs uses its fallbacks */ }
        };
        let handle: IdleWarningHandle | null = null;
        let tornDown = false;
        const rearmOnRefresh = () => { void handle?.rearm(); };
        // Await the tier read BEFORE arming so the very first idle deadline uses
        // the server's number, not the 60-min default.
        const armOnceTierKnown = async () => {
            await fetchTier();
            if (tornDown || handle !== null) return;
            handle = startIdleWarning({
                getDeadlineMs: () => wbDeadlineMs(sessionDeadlineContext),
                getIdleLimitMs: () => sessionDeadlineContext.idleLimitMs,
                activityTarget: typeof window === "undefined" ? null : window,
                // FIX D72 — the SAME existing refresh path the "Stay signed in"
                // button uses; the hook now also calls it on ACTIVITY when the
                // access token / deadline / session cap is inside the margin.
                onActivityRefresh: async () => { await tryRefreshToken(); },
                onStay: async () => { await tryRefreshToken(); },
                onSignOut: () => { broadcastLogout(); forceReauth(true); },
                onOpen: render.onOpen,
                onTick: render.onTick,
                onClose: render.onClose,
            });
            activeRef.current = handle;
        };
        void armOnceTierKnown();
        window.addEventListener(TOKEN_REFRESHED_EVENT, rearmOnRefresh);
        return () => {
            tornDown = true;
            window.removeEventListener(TOKEN_REFRESHED_EVENT, rearmOnRefresh);
            activeRef.current = null;
            handle?.stop();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [accessToken]);

    return proxyRef.current as IdleWarningHandle;
}
