// GSEC FX.54 p4a — Dashboard idle-warning dialog, 2 minutes before the SSO
// idle timeout (owner Q37=A: warn, then let the session time out if ignored).
//
// Keycloak's session_idle_timeout logs a user out after that much INACTIVITY,
// measured server-side — mouse moves never reset it, so this module deliberately
// listens to NO mouse/keyboard activity at all. The refresh token's `exp` IS
// that deadline (FX.54.p3d syncs the realm session lifetimes from the auth.*
// platform settings), so we schedule the dialog at deadline - 120s and the
// logout itself at the deadline.
//
// Everything time-related is injected (now/setTimer/clearTimer) so the unit
// tests can drive fake timers; js/auth.js passes the real globals.
//
// js/auth.js keeps the returned handle: it calls handle.rearm(deadlineMs) after
// every successful token refresh (a refresh slides the deadline forward) and
// handle.stop() on logout.

import { parseJwt } from './utils.js';

export const IDLE_WARN_LEAD_MS = 120 * 1000;
export const IDLE_WARN_COUNTDOWN_STEP_MS = 1000;

// BUG.2 — last-resort idle limit for remember-me (offline_access) sessions whose
// refresh token carries no usable `exp`. Mirrors the NORMAL session tier's idle
// limit: auth.sso_session_idle_seconds = 3600 s / 60 min
// (CORE/API/services/session_tier_enforcement.py:83, USER_TIER_SETTING_KEYS).
// Only used when neither /me's session_tier.idle_seconds nor the token response's
// refresh_expires_in is available.
export const IDLE_WARN_DEFAULT_IDLE_MS = 60 * 60 * 1000;
// Activity listeners are passive: they only stamp a timestamp, and at most once
// per this window, so a moving mouse cannot burn a timer per event.
export const IDLE_WARN_ACTIVITY_THROTTLE_MS = 5000;
export const IDLE_WARN_ACTIVITY_TYPES = ['mousemove', 'keydown', 'click'];
// FIX D72 — how close to a deadline/cap activity counts as "due": the caller's
// refresh path is asked for when the access token, the deadline or the session
// cap is inside this window. One attempt per due window (refreshPending guard).
export const IDLE_WARN_REFRESH_MARGIN_MS = 90 * 1000;

// BUG.2: a remember-me (Keycloak offline_access) refresh token carries no usable
// `exp` — it is absent or 0 — so its `exp` is NOT an idle deadline. Falling back
// to the ACCESS-token exp made the dialog fire on every short access-token life
// (the token is proactively refreshed, so the deadline kept sliding back). For
// those sessions the deadline is INACTIVITY-based: lastActivityMs + idleLimitMs,
// which is what the server actually enforces (session_tier_enforcement.py).
//
// Reads the deadline straight from sessionStorage — the browser-side default
// for startIdleWarning's getDeadlineMs. Refresh-token `exp` first (that IS the
// idle deadline for a normal session); a refresh token present but WITHOUT a
// usable exp (or an explicit offline_access/remember-me session) gets an IDLE
// deadline instead, never the access-token exp. Only a session with no refresh
// token at all falls back to the access-token `exp`. Returns null when nothing
// yields one.
//
// context (optional, supplied by js/auth.js from data it already has):
//   idleLimitMs          — /me's session_tier.idle_seconds * 1000 (preferred)
//   refreshExpiresInSec  — the token response's refresh_expires_in (fallback)
//   offlineAccess        — true when the session is remember-me/offline_access
//   lastActivityMs       — overrides the module's own activity tracker
// The sessionStorage the most recent deadlineFromSession() read — the sliding
// deadline and the refresh-due check re-read the tokens from it (a refresh
// writes a NEW refresh token, which moves the session cap forward).
let deadlineSessionStorage = null;

function readExpFrom(storage, key) {
    if (storage === null || storage === undefined) return null;
    const raw = storage.getItem(key);
    if (!raw) return null;
    try {
        const exp = parseJwt(raw).exp;
        return Number.isFinite(exp) && exp > 0 ? exp * 1000 : null;
    } catch (e) {
        return null;
    }
}
// The refresh token's `exp` for a NORMAL session — the Keycloak session's
// absolute cap (12 h maximum per the owner decision; the realm value today).
// Unusable (remember-me token with no exp, or no refresh token) => null.
function refreshCapMs() { return readExpFrom(deadlineSessionStorage, 'refreshToken'); }
// The access token's `exp` — activity inside its lead window means the caller's
// refresh path is due (the same check js/auth.js's checkAndRefreshToken makes).
function accessExpMs() { return readExpFrom(deadlineSessionStorage, 'accessToken'); }

export function deadlineFromSession(sessionStorage, context = {}) {
    deadlineSessionStorage = sessionStorage;

    const refreshRaw = sessionStorage.getItem('refreshToken');
    const refreshExp = readExpFrom(sessionStorage, 'refreshToken');
    if (refreshExp !== null) {
        // FIX D72 — the refresh token's `exp` is the Keycloak session's ABSOLUTE
        // cap (12 h maximum per the owner decision; login + ssoSessionIdleTimeout
        // today), NOT an idle deadline. Keycloak's session_idle_timeout is
        // measured against INACTIVITY, so the deadline is
        // min(refreshToken.exp, lastActivity + idleLimitMs): activity slides it
        // (the listeners now attach for this source too, and a refresh on
        // activity slides the cap itself), the cap still caps it, and a true
        // idle logout still fires after the idle limit of NO activity.
        // `idleLimitMs` is the value this file already resolves (/me
        // session_tier.idle_seconds -> refresh_expires_in -> the 60-min default);
        // no new platform setting.
        setDeadlineSource('refreshExp', {});
        // FIX D72 — the refresh token's `exp` is the Keycloak session's ABSOLUTE
        // cap (12 h maximum per the owner decision; login + ssoSessionIdleTimeout
        // today), NOT an idle deadline. It stays the FIRST source (preference
        // unchanged, so the existing deadline tests keep their meaning), but
        // startIdleWarning treats it as a CAP: slideDeadlineTo()/arm() keep the
        // live deadline at min(cap, lastActivity + idleLimitMs), so ACTIVITY
        // slides it (the listeners now attach for this source too) and a true
        // idle logout still fires after the idle limit of NO activity. The
        // refresh path (onActivityRefresh) slides the cap itself, so a working
        // user is never signed out at it.
        return refreshExp;
    }

    if (refreshRaw || context.offlineAccess === true) {
        // No usable refresh-token exp: this is a remember-me session. Idle
        // deadline, from the first idle limit the client already has.
        const idleLimitMs = idleLimitFromContext(context);
        if (idleLimitMs === null) return null;
        const lastActivity = Number.isFinite(context.lastActivityMs)
            ? context.lastActivityMs
            : (lastActivityMs === null ? Date.now() : lastActivityMs);
        setDeadlineSource('idle', { idleLimitMs });
        return lastActivity + idleLimitMs;
    }

    const accessExp = readExpFrom(sessionStorage, 'accessToken');
    if (accessExp !== null) setDeadlineSource('accessToken', {});
    return accessExp;
}

// Idle limit resolution order: (a) the session-tier idle value /me already
// returned, (b) the token response's refresh_expires_in, (c) the documented
// 60-min normal-tier default. Never 0/negative.
const idleLimitFromContext = (context) => {
    const idle = context.idleLimitMs;
    if (Number.isFinite(idle) && idle > 0) return idle;
    const rxi = context.refreshExpiresInSec;
    if (Number.isFinite(rxi) && rxi > 0) return rxi * 1000;
    return IDLE_WARN_DEFAULT_IDLE_MS;
};

// One-time console note naming the chosen source (not a warning per arm): the
// message logs the FIRST time a source is chosen; the source itself is tracked
// on every call so startIdleWarning can tell 'idle' from a token exp.
let lastDeadlineSource = null;
function setDeadlineSource(source, extra) {
    const changed = lastDeadlineSource !== source;
    lastDeadlineSource = source;
    if (changed) console.info('idle-warning: session deadline source', { source, ...extra });
}
// What the most recent deadlineFromSession() call used — startIdleWarning uses
// this to tell an INACTIVITY deadline apart from a token-exp one, so it can
// slide the deadline forward on activity (and never warn while the user is
// active).
export function lastDeadlineSourceKind() { return lastDeadlineSource; }
export function currentIdleLimitMs(context) { return idleLimitFromContext(context); }
// True when the most recent deadlineFromSession() used the INACTIVITY deadline
// (remember-me session) rather than a token `exp`.
function isIdleDeadline() { return lastDeadlineSourceKind() === 'idle'; }


// Passive activity tracking: only a timestamp is updated, never a timer.
let lastActivityMs = null;
export function noteActivity(ms) {
    lastActivityMs = Number.isFinite(ms) ? ms : Date.now();
}

export function startIdleWarning({
    getDeadlineMs,
    onStay,
    onSignOut,
    getIdleLimitMs,
    activityTarget,
    // FIX D72 — the caller's EXISTING refresh path (js/auth.js passes its
    // refreshAccessToken, the same one "Stay signed in" and the 5-min interval
    // use). Called on ACTIVITY when the access token / deadline / session cap is
    // inside IDLE_WARN_REFRESH_MARGIN_MS; optional, so older callers keep working.
    onActivityRefresh,
    now = Date.now,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
} = {}) {
    if (!getDeadlineMs || !onStay || !onSignOut) {
        throw new TypeError('startIdleWarning needs getDeadlineMs, onStay and onSignOut');
    }

    const state = { deadline: null, live: true, dialog: null, open: false };
    const timers = new Set();

    // INACTIVITY mode: the deadline came from lastActivity + idleLimit (remember-
    // me session, refresh token with no exp), not a token `exp`. Keycloak's
    // session_idle_timeout is measured server-side against ACTIVITY, so the
    // deadline slides forward while the user keeps working — the dialog must
    // never fire while they are active.
    let idleMode = false;
    // FIX D72 — ACTIVITY mode covers BOTH the `idle` source AND the normal
    // session's `refreshExp` deadline: the listeners must attach there too, and a
    // stamp slides that deadline up to the refresh token's `exp` (the session's
    // absolute cap). Without this the owner gets "kicked out while working".
    let activityMode = false;
    let lastActivity = null;
    let activityStamp = -IDLE_WARN_ACTIVITY_THROTTLE_MS;
    let refreshPending = false;
    const idleLimitMs = () => (getIdleLimitMs ? getIdleLimitMs() : null) || IDLE_WARN_DEFAULT_IDLE_MS;

    // The deadline slides with a stamp but never past the refresh token's `exp`
    // (the Keycloak session's absolute cap) and never into the past.
    const slideDeadlineTo = (stamp) => {
        const cap = refreshCapMs();
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
    // itself, or the session cap) is inside IDLE_WARN_REFRESH_MARGIN_MS, ask the
    // caller for its EXISTING refresh (js/auth.js passes refreshAccessToken, the
    // same call the "Stay signed in" button and the 5-min interval already use).
    // The BFF refresh grant slides the Keycloak ONLINE session, so the cap moves
    // later and the user stays in. One attempt per due window.
    const requestRefreshIfDue = (stamp) => {
        if (!onActivityRefresh || refreshPending) return;
        const accessExp = accessExpMs();
        const cap = refreshCapMs();
        const nearAccess = accessExp !== null && accessExp - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        const nearDeadline = state.deadline !== null && state.deadline - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        const nearCap = cap !== null && cap - stamp <= IDLE_WARN_REFRESH_MARGIN_MS;
        if (!nearAccess && !nearDeadline && !nearCap) return;
        refreshPending = true;
        try {
            const attempt = onActivityRefresh();
            if (attempt !== null && typeof attempt === 'object' && typeof attempt.then === 'function') {
                void attempt.then(
                    () => { refreshPending = false; },
                    () => { refreshPending = false; },
                );
            } else {
                refreshPending = false;
            }
        } catch (e) {
            refreshPending = false;
        }
    };

    const noteActivity = (stamp) => {
        if (!activityMode || !state.live) return;
        if (stamp - activityStamp < IDLE_WARN_ACTIVITY_THROTTLE_MS) return;
        activityStamp = stamp;
        lastActivity = stamp;
        requestRefreshIfDue(stamp);
        slideDeadlineTo(stamp);
    };

    // Passive activity listeners: they stamp a timestamp only — no timer, no DOM
    // mutation — and are throttled, so they can never storm the scheduler.
    let listenersAttached = false;
    const attachActivityListeners = () => {
        if (!activityMode || listenersAttached) return;
        listenersAttached = true;
        for (const type of IDLE_WARN_ACTIVITY_TYPES) {
            try {
                activityTarget.addEventListener(type, () => noteActivity(now()));
            } catch (e) { /* no listener support: activity simply isn't tracked */ }
        }
    };

    const clearAllTimers = () => {
        for (const handle of timers) {
            try { clearTimer(handle); } catch (e) { /* already fired */ }
        }
        timers.clear();
    };

    // One fake-timer-friendly scheduler: the handle is registered so stop() can
    // clear it, and every callback re-checks `live` so a cleared-but-already-
    // queued callback can never act after stop().
    const schedule = (ms, fn) => {
        let handle = null;
        handle = setTimer(() => {
            timers.delete(handle);
            if (state.live) fn();
        }, Math.max(0, ms));
        timers.add(handle);
        return handle;
    };

    const closeDialog = () => {
        if (state.dialog) {
            try { state.dialog.remove(); } catch (e) { /* detached already */ }
        }
        state.dialog = null;
        state.open = false;
    };

    const secondsLeft = () => Math.max(0, Math.ceil((state.deadline - now()) / 1000));

    const fmt = (n) => {
        const m = Math.floor(n / 60);
        const s = n % 60;
        if (m <= 0) return `${s} sec`;
        return s === 0 ? `${m} min` : `${m} min ${s} sec`;
    };

    // The countdown re-arms itself once a second; it stops as soon as the
    // dialog is closed (or the handle is stopped), so no orphan tick survives.
    const tick = () => {
        if (!state.open || !state.live || !state.dialog) return;
        const msg = state.dialog.querySelector('#idle-warning-message');
        if (msg) msg.textContent = `You will be signed out in ${fmt(secondsLeft())}.`;
        schedule(IDLE_WARN_COUNTDOWN_STEP_MS, tick);
    };

    // "Stay signed in": hand off to the caller's refresh, then re-arm against
    // the deadline the refresh produced (getDeadlineMs is re-read AFTER the
    // await, so an async refresh has already written the new tokens). The dialog
    // closes either way — if the refresh failed the deadline is unchanged and
    // arm() re-opens it once, then the deadline timer signs the user out.
    const onStayClick = async () => {
        if (!state.open) return;
        closeDialog();
        try { await onStay(); } catch (e) { console.warn('idle-warning: stay-signed-in failed', e); }
        clearAllTimers();   // the old deadline timer must not survive a refresh
        const next = getDeadlineMs();
        if (next !== null && next !== undefined) state.deadline = next;
        arm();
    };

    const onDeadline = () => {
        closeDialog();
        state.live = false;
        clearAllTimers();
        onSignOut();
    };

    // "Sign out": hand off to the caller's logout and disarm — the session is
    // over, nothing left to warn about.
    const onSignOutClick = () => {
        if (!state.open) return;
        closeDialog();
        state.live = false;
        clearAllTimers();
        onSignOut();
    };

    function arm() {
        if (!state.live || state.deadline === null) return;
        if (activityMode && activityTarget) {
            // Never warn while the user is active: if they have worked since the
            // deadline was computed, push the deadline out first — but never past
            // the refresh token's `exp` (the session's absolute cap).
            const cap = refreshCapMs();
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
    }

    const handle = {
        rearm: (deadlineMs) => {
            const next = deadlineMs === undefined || deadlineMs === null ? getDeadlineMs() : deadlineMs;
            if (!state.live) return false;
            if (next === null || next === undefined) return false;
            state.deadline = next;
            idleMode = isIdleDeadline();
            // FIX D72 — the sliding window also applies to the refreshExp source.
            activityMode = idleMode || lastDeadlineSourceKind() === 'refreshExp';
            if (idleMode && lastActivity === null) lastActivity = next - idleLimitMs();
            if (activityTarget && !listenersAttached) attachActivityListeners();
            clearAllTimers();
            arm();
            return true;
        },
        stop: () => {
            state.live = false;
            clearAllTimers();
            closeDialog();
        },
        getDeadline: () => state.deadline,
        isOpen: () => state.open,
    };

    const first = getDeadlineMs();
    if (first !== null && first !== undefined) {
        state.deadline = first;
        idleMode = isIdleDeadline();
        if (idleMode) lastActivity = first - idleLimitMs();
    }
    // FIX D72 — the listeners (and the sliding deadline) also apply to the normal
    // session's `refreshExp` source; the refresh token's `exp` still caps it.
    activityMode = idleMode || lastDeadlineSourceKind() === 'refreshExp';
    attachActivityListeners();
    arm();
    return handle;

    function openDialog() {
        if (!state.live) return;
        closeDialog();

        const modal = document.createElement('div');
        modal.id = 'idle-warning-modal';
        modal.setAttribute('role', 'alertdialog');
        modal.setAttribute('aria-modal', 'true');
        modal.setAttribute('aria-labelledby', 'idle-warning-title');

        const title = document.createElement('h3');
        title.id = 'idle-warning-title';
        title.textContent = 'Still here?';

        const msg = document.createElement('p');
        msg.id = 'idle-warning-message';
        msg.textContent = '';

        const stay = document.createElement('button');
        stay.id = 'idle-warning-stay';
        stay.type = 'button';
        stay.textContent = 'Stay signed in';

        const signOut = document.createElement('button');
        signOut.id = 'idle-warning-signout';
        signOut.type = 'button';
        signOut.textContent = 'Sign out';

        modal.appendChild(title);
        modal.appendChild(msg);
        modal.appendChild(stay);
        modal.appendChild(signOut);
        document.body.appendChild(modal);

        state.dialog = modal;
        state.open = true;
        tick();

        // Keyboard: Esc = Stay (the safe default). Focus lands on Stay so the
        // dialog is operable without a mouse.
        try { stay.focus(); } catch (e) { /* stub DOM in tests */ }
        try {
            stay.addEventListener('click', onStayClick, { once: true });
            signOut.addEventListener('click', onSignOutClick, { once: true });
        } catch (e) { /* stub DOM in tests */ }
        try {
            modal.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') onStayClick();
            });
        } catch (e) { /* stub DOM in tests */ }
    }
}
