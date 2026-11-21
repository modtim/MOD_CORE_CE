// FIX-DASH-IDLE-DEADLINE-SLIDES-WITH-ACTIVITY (D72) — the Dashboard mirror of the
// Workflow Builder fix (c368d8a196). Owner decision (Tim, 11-17): idle sign-out =
// 1 hour of NO ACTIVITY (activity resets it), 12 hour maximum.
//
// For a NORMAL session deadlineFromSession() returns the refresh token's `exp`
// (source `refreshExp`) = login + ssoSessionIdleTimeout — an ABSOLUTE cap. The
// module's noteActivity() bailed out unless the source was `idle`, so mouse/keys
// never moved that deadline and the owner was signed out while working (the
// session only survived because js/auth.js refreshes every 5 minutes).
//
// Asserts:
//   (a) activity at t=50 min slides the deadline: NO logout at t=61 min, logout
//       at the slid deadline;
//   (b) no activity at all -> logout at the deadline (a true idle logout still
//       fires after the idle limit of NO activity);
//   (c) activity triggers EXACTLY ONE refresh when inside the margin (the
//       caller's existing refresh path, one attempt per due window);
//   (d) activity well inside the margins triggers NO refresh (no storming the BFF);
//   (e) the refresh token's `exp` still CAPS the deadline (no new limit invented);
//   (f) a shorter tier idle limit slides with activity and follows the stall.
//
// Fake timers: now/setTimer/clearTimer are injected (same style as
// tests/idle-warning.test.js), so a virtual clock drives the whole session.
//
//   node --test CORE/Dashboard/tests/idleDeadlineSlidesWithActivity.check.js
import assert from 'node:assert';
import test from 'node:test';
import { startIdleWarning, deadlineFromSession } from '../js/idle-warning.js';

const MIN = 60_000;
const T0 = 1_700_000_000_000; // fixed epoch: every expectation is absolute (ms)

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
globalThis.document = { body: new El('body'), createElement: (t) => new El(t), getElementById: (id) => null, listeners: {},
    addEventListener: (type, fn) => { globalThis.document.listeners[type] = fn; } };
globalThis.window = { atob: (s) => atob(s) };
function findDialog() { return document.body.children.find((c) => c.attrs.id === 'idle-warning-modal'); }
function simulateActivity() { for (const fn of Object.values(globalThis.document.listeners)) fn({ key: 'm' }); }

function makeJwt(expSeconds) {
    const b64 = (o) => Buffer.from(JSON.stringify(o)).toString('base64').replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
    return `${b64({ alg: 'none' })}.${b64({ exp: expSeconds })}.sig`;
}
// minutes-after-login -> a JWT whose `exp` is that moment (the module reads exp*1000).
const jwtAt = (min) => makeJwt(T0 / 1000 + min * 60);

// sessionStorage stand-in handed to deadlineFromSession (the module never touches
// the real browser sessionStorage). A refresh grant writes NEWER tokens into it,
// which is what slides the session cap forward.
const sessionStorage = { _s: {}, getItem: (k) => sessionStorage._s[k] };
let ctx = {};

let refreshes = 0;
let logouts = 0;
let opened = [];

function seedSession(accessMin, refreshMin) {
    sessionStorage._s = { accessToken: jwtAt(accessMin), refreshToken: jwtAt(refreshMin) };
}
function fresh(onActivityRefresh, idleLimitMs) {
    clock = T0;
    queue.length = 0;
    document.body.children = [];
    refreshes = 0; logouts = 0; opened = [];
    ctx = { idleLimitMs };
    const h = startIdleWarning({
        getDeadlineMs: () => deadlineFromSession(sessionStorage, ctx),
        getIdleLimitMs: () => ctx.idleLimitMs,
        activityTarget: document,
        onActivityRefresh,
        onStay: () => {},
        onSignOut: () => { logouts += 1; },
        now, setTimer, clearTimer,
    });
    return h;
}

// ---------------------------------------------------------------------------
// (a) activity slides the deadline: no logout at the old absolute cap
// ---------------------------------------------------------------------------
test('a: activity at t=50 min moves the deadline: no logout at t=61 min', () => {
    seedSession(15, 60); // access exp login+15min, refresh exp login+60min (the cap)
    const h = fresh(() => {
        refreshes += 1;
        // the BFF refresh grant re-issues tokens measured from NOW (t=50min):
        // the Keycloak ONLINE session slid, so the cap moved later.
        seedSession(50 + 15, 50 + 60);
    }, 60 * MIN); // /me session_tier.idle_seconds = 3600
    assert.strictEqual(h.getDeadline(), T0 + 60 * MIN, 'deadline starts at the refresh-token cap');

    advance(50 * MIN);
    assert.ok(!findDialog(), 'no dialog yet (the warning lead is 2 min before the deadline)');
    simulateActivity();               // activity at t=50 min
    assert.strictEqual(refreshes, 1, 'activity inside the margin asked for one refresh');
    assert.strictEqual(h.getDeadline(), T0 + 110 * MIN, 'deadline slid to lastActivity + idle limit');

    advance(11 * MIN);                // t = 61 min: the OLD absolute cap would have logged out
    assert.strictEqual(logouts, 0, 'no logout at the old cap while active');
    assert.ok(!findDialog(), 'no dialog at 61 min');

    advance(110 * MIN - 120_000 - (clock - T0) - 1); // one ms before the slid deadline - 120s
    assert.ok(!findDialog(), 'nothing one ms before the slid lead');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at the SLID deadline - 120s');
    advance(120_000);
    assert.strictEqual(logouts, 1, 'logout at the slid deadline');
});

// ---------------------------------------------------------------------------
// (b) no activity at all: the deadline still fires the logout
// ---------------------------------------------------------------------------
test('b: no activity -> logout at the deadline, zero refreshes', () => {
    seedSession(15, 60);
    const h = fresh(() => { refreshes += 1; }, 60 * MIN);
    assert.strictEqual(h.getDeadline(), T0 + 60 * MIN);
    advance(60 * MIN - 120_000 - 1);
    assert.ok(!findDialog(), 'nothing before the lead');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at deadline - 120s');
    advance(120_000);
    assert.strictEqual(logouts, 1, 'logout at the deadline');
    assert.strictEqual(refreshes, 0, 'no refresh without activity');
});

// ---------------------------------------------------------------------------
// (c) activity triggers EXACTLY ONE refresh when within the margin
// ---------------------------------------------------------------------------
test('c: three activity stamps inside the margin -> exactly one refresh', () => {
    seedSession(15, 60);
    const h = fresh(() => {
        refreshes += 1;
        seedSession(50 + 15, 50 + 60);
    }, 60 * MIN);
    advance(50 * MIN);
    simulateActivity();
    advance(MIN);
    simulateActivity();
    advance(MIN);
    simulateActivity();
    assert.strictEqual(refreshes, 1, 'one refresh per due window (throttle + refreshPending)');
    // the refresh at t=50 min re-issued a token expiring at 110 min, so the
    // deadline slid to that cap and the later stamps cannot pass it.
    assert.strictEqual(h.getDeadline(), T0 + 110 * MIN, 'deadline slid once, one refresh');
    assert.strictEqual(logouts, 0, 'still signed in');
});

// ---------------------------------------------------------------------------
// (d) activity well inside the margins triggers NO refresh
// ---------------------------------------------------------------------------
test('d: activity far from every deadline triggers no refresh', () => {
    seedSession(60, 120);
    const h = fresh(() => { refreshes += 1; }, 60 * MIN);
    assert.strictEqual(h.getDeadline(), T0 + 120 * MIN);
    advance(10 * MIN);
    simulateActivity();
    assert.strictEqual(refreshes, 0, 'nothing inside the 90s margin: no BFF storming');
    assert.strictEqual(h.getDeadline(), T0 + 70 * MIN, 'the deadline still slid with activity');
});

// ---------------------------------------------------------------------------
// (e) the refresh token `exp` still CAPS the deadline (no new limit invented)
// ---------------------------------------------------------------------------
test('e: activity never moves the deadline past the refresh-token cap', () => {
    seedSession(15, 60);
    const h = fresh(() => {}, 60 * MIN); // a refresh that slides nothing: the cap is hard
    assert.strictEqual(h.getDeadline(), T0 + 60 * MIN);
    advance(45 * MIN);
    simulateActivity();
    assert.strictEqual(h.getDeadline(), T0 + 60 * MIN, 'activity cannot push past the cap');
    advance(15 * MIN - 120_000 - 1);
    assert.ok(!findDialog(), 'nothing before the cap lead');
    advance(1);
    assert.ok(findDialog(), 'dialog opened at the cap - 120s');
    advance(120_000);
    assert.strictEqual(logouts, 1, 'logout at the cap');
});

// ---------------------------------------------------------------------------
// (f) activity INSIDE the deadline margin (not the access-token one) slides it
// ---------------------------------------------------------------------------
test('f: activity 90s before the deadline refreshes and no logout follows', () => {
    seedSession(15, 60);
    const h = fresh(() => {
        refreshes += 1;
        // the grant re-issues tokens measured from NOW: the session slid.
        seedSession(58 + 15, 58 + 60);
    }, 60 * MIN);
    assert.strictEqual(h.getDeadline(), T0 + 60 * MIN);
    advance(58 * MIN);
    simulateActivity();               // 2 min before the deadline: inside the margin
    assert.strictEqual(refreshes, 1, 'the deadline margin asked for one refresh');
    assert.strictEqual(h.getDeadline(), T0 + 118 * MIN, 'the deadline slid with the refreshed cap');
    advance(3 * MIN);                 // t = 61 min: the old cap would have logged out
    assert.strictEqual(logouts, 0, 'no logout at the old cap while active');
});
