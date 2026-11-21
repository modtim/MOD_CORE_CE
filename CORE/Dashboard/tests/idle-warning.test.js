// FX.54 p4a — Dashboard idle-warning dialog (js/idle-warning.js).
//
// Asserts:
//   (a) the dialog opens exactly at deadline - 120s (deadline = refresh-token
//       `exp`, the Keycloak SSO-idle deadline FX.54.p3d syncs from auth.*);
//   (b) "Stay signed in" calls the caller's refresh (onStay) and re-arms against
//       the NEW deadline the refresh produced;
//   (c) ignoring the dialog → at the deadline the caller's logout (onSignOut)
//       fires;
//   (d) handle.stop() clears every pending timer and nothing fires afterwards;
//   (e) Esc = Stay, and the dialog is role=alertdialog with focus on Stay;
//   (f) with no refresh token, the deadline falls back to the access-token exp.
//
// Fake timers: now/setTimer/clearTimer are injected, so a virtual clock drives
// the whole session. The stub DOM has NO innerHTML — any markup-building in the
// module would throw here, which is how "escape all text" is enforced.
//
//   node --test CORE/Dashboard/tests/idle-warning.test.js
import assert from 'node:assert';
import test from 'node:test';
import { startIdleWarning, deadlineFromSession } from '../js/idle-warning.js';

const MIN = 60_000;

// --- fake clock -------------------------------------------------------------
let clock = 0;
const queue = [];
let timerId = 0;
const setTimer = (fn, ms) => { queue.push({ at: clock + Math.max(0, ms), id: ++timerId, fn }); return timerId; };
const clearTimer = (id) => { const i = queue.findIndex((q) => q.id === id); if (i >= 0) queue.splice(i, 1); };
const now = () => clock;
function advance(ms) {
    const target = clock + ms;
    for (;;) {
        const due = queue.filter((q) => q.at <= target).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        clearTimer(due.id);
        clock = due.at;
        due.fn();
    }
    clock = target;
}
const pending = () => queue.length;

// --- stub DOM (textContent only; no innerHTML exists) -----------------------
class El {
    constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.parent = null; this.listeners = {}; }
    set id(v) { this.attrs.id = v; }
    get id() { return this.attrs.id; }
    setAttribute(k, v) { this.attrs[k] = v; }
    appendChild(c) { this.children.push(c); c.parent = this; return c; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; }
    focus() { this.focused = true; }
    addEventListener(type, fn) { this.listeners[type] = fn; }
    get textContent() { return this.text ?? ''; }
    set textContent(v) { this.text = v; }
    querySelector(sel) {
        const want = sel.replace(/^#/, '');
        const walk = (el) => el.children.flatMap((c) => [c, ...walk(c)]);
        return walk(this).find((c) => c.attrs.id === want);
    }
}
function findDialog() { return document.body.children.find((c) => c.attrs.id === 'idle-warning-modal'); }

function makeJwt(exp) {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64').replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
    return `${b64({ alg: 'none' })}.${b64({ exp })}.sig`;
}

// sessionStorage stand-in handed to deadlineFromSession (the module never
// touches the real browser sessionStorage itself).
const sessionStorage = { _s: {}, getItem: (k) => sessionStorage._s[k] };

// Deadline context (BUG.2): what js/auth.js hands deadlineFromSession from data
// it already has — /me's session_tier.idle_seconds*1000, the token response's
// refresh_expires_in, the offline_access scope, and (tests only) an explicit
// last-activity stamp.
let ctx = {};

// The stub `document` gets passive activity listeners so the tests can drive
// the same mousemove/keydown/click hooks js/auth.js relies on.
const docListeners = {};
globalThis.document = { body: new El('body'), createElement: (t) => new El(t), getElementById: (id) => null,
    addEventListener: (type, fn) => { docListeners[type] = fn; } };
globalThis.window = { atob: (s) => atob(s) };
function simulateActivity() { for (const fn of Object.values(docListeners)) fn({ key: 'm' }); }

// Each test seeds sessionStorage._s BEFORE calling fresh(); fresh() only resets
// the virtual clock and the timer queue so the tests stay independent.
function fresh() {
    clock = 0;
    queue.length = 0;
    document.body.children = [];   // no stale dialog leaks between tests
    const calls = { stay: 0, signOut: 0 };
    const h = startIdleWarning({
        getDeadlineMs: () => deadlineFromSession(sessionStorage, ctx),
        getIdleLimitMs: () => ctx.idleLimitMs,
        activityTarget: document,
        // Mirrors refreshAccessToken: a successful refresh writes a NEW
        // refresh token (longer-lived) before returning, which slides the
        // idle deadline forward.
        onStay: () => { calls.stay++; sessionStorage._s.refreshToken = makeJwt(NEXT_DEADLINE / 1000); return true; },
        onSignOut: () => { calls.signOut++; },
        now, setTimer, clearTimer,
    });
    return { h, calls };
}

const DEADLINE = 600_000;   // refresh-token exp, 10 min from t0
const NEXT_DEADLINE = DEADLINE + 300_000;   // a refresh mints a longer-lived token

test('a: dialog opens exactly at deadline - 120s', () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    const { calls } = fresh();
    advance(DEADLINE - 120_000 - 1);
    assert.ok(!findDialog(), 'no dialog before T-120s');
    advance(1);
    const dlg = findDialog();
    assert.ok(dlg, 'dialog opened at T-120s');
    assert.strictEqual(dlg.attrs.role, 'alertdialog');
    assert.strictEqual(dlg.attrs['aria-modal'], 'true');
    const msg = dlg.querySelector('#idle-warning-message');
    assert.ok(msg.textContent.includes('signed out in 2 min'), msg.textContent);
    assert.strictEqual(calls.signOut, 0);
});

test('b: Stay calls onStay, then re-arms against the refreshed deadline', async () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    const { h, calls } = fresh();
    advance(DEADLINE - 120_000);
    const dlg = findDialog();
    assert.ok(dlg, 'dialog open');
    const stay = dlg.querySelector('#idle-warning-stay');
    assert.ok(stay, 'stay button present');
    assert.strictEqual(stay.focused, true, 'focus starts on Stay');
    await stay.listeners.click();
    assert.strictEqual(calls.stay, 1, 'onStay called once');
    assert.ok(!findDialog(), 'dialog closed by Stay');
    // the refresh minted a longer-lived refresh token → deadline slides forward
    sessionStorage._s.refreshToken = makeJwt((DEADLINE + 300_000) / 1000);
    h.rearm();
    assert.strictEqual(h.getDeadline(), DEADLINE + 300_000);
    assert.ok(pending() > 0, 're-armed timers pending');
    assert.strictEqual(calls.signOut, 0, 'no logout yet');
});

test('c: ignoring the dialog logs out at the deadline', async () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    const { h, calls } = fresh();
    advance(DEADLINE - 120_000);
    assert.ok(findDialog(), 'dialog open at T-120s');
    const dlg = findDialog();
    const msg = dlg.querySelector('#idle-warning-message');
    assert.ok(msg.textContent.includes('2 min'), msg.textContent);
    advance(120_000);
    assert.strictEqual(calls.signOut, 1, 'onSignOut fired at the deadline');
    assert.ok(!findDialog(), 'dialog closed at the deadline');
    assert.strictEqual(pending(), 0, 'nothing left pending');
});

test('d: stop() clears every pending timer', async () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    const { h, calls } = fresh();
    advance(DEADLINE - 120_000);
    assert.ok(pending() > 0, 'timers pending before stop');
    h.stop();
    assert.strictEqual(pending(), 0, 'all timers cleared');
    assert.ok(!findDialog(), 'dialog removed by stop');
    advance(DEADLINE * 2);
    assert.strictEqual(calls.signOut, 0, 'no logout after stop');
    assert.strictEqual(calls.stay, 0, 'no stay after stop');
});

test('e: Esc = Stay', async () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    const { calls } = fresh();
    advance(DEADLINE - 120_000);
    const dlg = findDialog();
    assert.ok(dlg, 'dialog open');
    dlg.listeners.keydown({ key: 'Escape' });
    await Promise.resolve();   // let the async onStayClick settle
    assert.strictEqual(calls.stay, 1, 'Esc behaved as Stay');
    assert.ok(!findDialog(), 'dialog closed by Esc');
});

test('f: no refresh token → falls back to the access-token exp', () => {
    sessionStorage._s = { accessToken: makeJwt(DEADLINE / 1000) };
    const { calls } = fresh();
    advance(DEADLINE - 120_000 - 1);
    assert.ok(!findDialog(), 'nothing before T-120s');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at access-exp - 120s');
});

// --- BUG.2: remember-me (offline_access) sessions -----------------------------
// The offline refresh token carries no usable `exp` (absent or 0). The deadline
// must come from the IDLE limit (lastActivity + idleLimitMs), NOT the short,
// proactively-refreshed access-token exp.

test('g: offline refresh token without exp → idle-limit deadline, not access exp', () => {
    // refresh exp = 0 (unusable); access exp = 2 min (the trap: far too early)
    sessionStorage._s = { refreshToken: makeJwt(0), accessToken: makeJwt(120_000 / 1000) };
    ctx = { idleLimitMs: 600_000, lastActivityMs: 0 };   // /me session_tier.idle_seconds
    const { calls } = fresh();
    advance(600_000 - 120_000 - 1);
    assert.ok(!findDialog(), 'no dialog at access-exp (120s), long past it');
    assert.strictEqual(calls.signOut, 0, 'no logout at access-exp');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at idle deadline (0 + 600s) - 120s');
});

test('h: activity pushes the idle deadline out (never fires while active)', () => {
    sessionStorage._s = { refreshToken: makeJwt(0), accessToken: makeJwt(120_000 / 1000) };
    ctx = { idleLimitMs: 300_000, lastActivityMs: 0 };
    const { calls } = fresh();
    // Without activity the dialog would open at 300s - 120s = 180s.
    advance(179_999);
    assert.ok(!findDialog(), 'no dialog before 180s');
    simulateActivity();               // activity at t=179.999s
    advance(1);                       // t=180s: the no-activity dialog point
    assert.ok(!findDialog(), 'activity slid the deadline to 479.999s: no dialog at 180s');
    assert.strictEqual(calls.signOut, 0, 'no logout while active');
    advance(179_999);                 // t=359.999s = slid deadline (479.999s) - 120s
    assert.ok(findDialog(), 'dialog opened at the SLID deadline - 120s');
});

test('i: a normal refresh token WITH exp still uses the exp', () => {
    sessionStorage._s = { refreshToken: makeJwt(DEADLINE / 1000) };
    ctx = { idleLimitMs: 300_000, lastActivityMs: 0 };   // idle limit would say 300s
    const { calls } = fresh();
    advance(300_000 - 120_000 - 1);
    assert.ok(!findDialog(), 'no dialog at the idle-limit lead (180s) — the exp wins');
    advance(DEADLINE - 300_000);      // t = DEADLINE - 120s - 1
    assert.ok(!findDialog(), 'still nothing one ms before the exp lead');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at refresh-exp - 120s');
});
