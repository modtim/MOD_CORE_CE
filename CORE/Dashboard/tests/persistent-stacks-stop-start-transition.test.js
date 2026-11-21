//  — proof that the
// Stacks row shows "stopping..." / "starting..." the moment Stop / Start is
// pressed (before the API answers), keeps it until the server reports the final
// state, and restores the previous state with the API error text on failure.
// Real source blocks from js/persistent-stacks.js (transition block, _psStackRow,
// _psStackSig, _psPatchStacks is stubbed) run in a jsdom-backed vm sandbox with a
// mocked apiCall and fake timers.
//
//   node --test CORE/Dashboard/tests/persistent-stacks-stop-start-transition.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

function slice(a, b) {
    const start = SRC.indexOf(a);
    const end = SRC.indexOf(b, start + 1);
    assert.ok(start >= 0 && end > start, `locate ${a} .. ${b}`);
    return SRC.slice(start, end);
}
function loadJSDOM() {
    const candidates = [
        path.join(__dirname, '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom'),
        path.join(__dirname, '..', '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom'),
    ];
    let last;
    for (const p of candidates) { try { return require(p); } catch (e) { last = e; } }
    throw last;
}
const JSDOM = loadJSDOM().JSDOM;

const TRANSIT = slice('// --- Stop / Start transition state', 'async function _psAct(');
const ACT = slice('async function _psAct(', '// Soft-delete a stack that was never deployed');
const ROW = slice('function _psStackRow(', 'function _psStackSig(');
const SIG = slice('function _psStackSig(', 'function _psPatchStacks(');
const STATUS_CLASS = slice('const _PS_STATUS_CLASS = {', '// HA class decides');
const CODE = [STATUS_CLASS, TRANSIT, ACT, ROW, SIG].join('\n');

// Stub every other _ps*/_PS_* helper the row refers to.
const own = new Set(['_psTransitLabel', '_psRepaintMain', '_psTransitEnd', '_psTransitSettle', '_psTransitPoll',
    '_psTransitAct', '_psAct', '_psStackRow', '_psStackSig', '_PS_STATUS_CLASS', '_psTransit', '_psLastWorkers',
    '_PS_TRANSIT_POLL_MS', '_PS_TRANSIT_MAX_MS', '_PS_TRANSIT_FINAL']);
const needed = new Set(CODE.match(/\b_(?:ps|PS_)[A-Za-z_]+/g) || []);
let stubs = '';
for (const n of needed) {
    if (own.has(n) || n === '_psById' || n === '_psPatchStacks' || n === '_psMsg' || n === '_psLoadStacks') continue;
    if (/^_PS_/.test(n)) stubs += `var ${n} = {};\n`;
    else if (n === '_psEsc') stubs += `function _psEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}\n`;
    else stubs += `function ${n}(){return '';}\n`;
}

function setup(apiImpl) {
    const dom = new JSDOM('<table><tbody id="ps-rows"></tbody></table>');
    const timers = [];
    const msgs = [];
    const calls = [];
    const ctx = {
        document: dom.window.document, Date, JSON, Number, String, Object, Array, Promise, console,
        setTimeout: (fn, ms) => { const h = { fn, ms, off: false }; timers.push(h); return h; },
        clearTimeout: (h) => { if (h) h.off = true; },
        apiCall: async (url, opts) => { calls.push(url + (opts && opts.method ? ' ' + opts.method : '')); return apiImpl(url, opts); },
        _psMsg: (t, ok) => msgs.push({ t, ok }),
        _psPatchStacks: () => {}, _psLoadStacks: () => {}, _psById: {},
    };
    vm.createContext(ctx);
    vm.runInContext(stubs + CODE, ctx);
    return { ctx, timers, msgs, calls, doc: dom.window.document };
}
const flush = () => new Promise((r) => setImmediate(r));
async function fire(env) {                       // run the next pending fake timer
    const h = env.timers.find((x) => !x.off);
    assert.ok(h, 'a timer is pending');
    h.off = true;
    h.fn();
    await flush(); await flush();
}
function seed(env, s) {
    env.ctx._psById[s.id] = s;
    env.ctx.__s = s;
    env.doc.getElementById('ps-rows').innerHTML = vm.runInContext('_psStackRow(__s, [])', env.ctx);
}
const pill = (env) => env.doc.querySelector('[data-testid="ps-status-pill"]');
const btn = (env, act) => env.doc.querySelector(`button[data-ps-act="${act}"]`);
const running = { id: 7, name: 'emeralds', status: 'running', desired_state: 'running' };
const stopping = { id: 7, name: 'emeralds', status: 'running', desired_state: 'stopped' };   // server: stop dispatched, host not reported yet
const stopped = { id: 7, name: 'emeralds', status: 'stopped', desired_state: 'stopped' };

test('stop: shows stopping BEFORE the API resolves, then the final state', async () => {
    let release;
    let phase = 'running';
    const env = setup((url, opts) => {
        if (opts && opts.method === 'POST') return new Promise((r) => { release = r; });
        return [phase === 'running' ? stopping : stopped];
    });
    seed(env, running);
    assert.strictEqual(pill(env).dataset.status, 'running');
    const p = env.ctx._psAct(7, 'stop');
    await flush();
    assert.strictEqual(pill(env).dataset.status, 'stopping', 'transition shown while POST is pending');
    assert.match(pill(env).textContent, /stopping/);
    assert.ok(btn(env, 'stop').disabled, 'stop disabled on that stack');
    assert.ok(btn(env, 'teardown').disabled, 'teardown disabled on that stack');
    release({}); await p;
    await fire(env);                                      // poll 1: still running
    assert.strictEqual(pill(env).dataset.status, 'stopping');
    phase = 'stopped';
    await fire(env);                                      // poll 2: stopped
    assert.strictEqual(vm.runInContext('Object.keys(_psTransit).length', env.ctx), 0, 'transition cleared');
    assert.ok(env.calls.filter((c) => c === '/stacks').length >= 2, 'polled the list');
    assert.ok(!env.timers.some((t) => !t.off), 'polling stopped after the final state');
});

test('stop error: restores the previous state and shows the API error text', async () => {
    const env = setup((url, opts) => { if (opts && opts.method === 'POST') throw new Error('Stack is pinned by policy'); return []; });
    seed(env, running);
    await env.ctx._psAct(7, 'stop');
    assert.strictEqual(pill(env).dataset.status, 'running');
    assert.ok(!btn(env, 'stop').disabled);
    assert.ok(env.msgs.some((m) => /Stack is pinned by policy/.test(m.t) && m.ok === false));
    assert.ok(!env.timers.some((t) => !t.off), 'no poller left behind');
});

test('start mirrors stop: starting shown first, then running', async () => {
    let phase = 'stopped';
    const env = setup((url, opts) => {
        if (opts && opts.method === 'POST') return {};
        return [phase === 'stopped' ? stopped : Object.assign({}, running)];
    });
    seed(env, stopped);
    const p = env.ctx._psAct(7, 'start');
    await flush();
    assert.strictEqual(pill(env).dataset.status, 'starting');
    assert.match(pill(env).textContent, /starting/);
    assert.ok(btn(env, 'start').disabled);
    await p;
    phase = 'running';
    await fire(env);
    assert.strictEqual(vm.runInContext('Object.keys(_psTransit).length', env.ctx), 0);
});

test('API intermediate statuses render with the same labels', () => {
    const env = setup(() => []);
    const lab = (st) => vm.runInContext(`_psTransitLabel({id: 7, status: '${st}'})`, env.ctx);
    assert.strictEqual(lab('stopping'), 'stopping');
    assert.strictEqual(lab('starting'), 'starting');
    assert.strictEqual(lab('waking'), 'starting');
    assert.strictEqual(lab('running'), '');
});

test('gives up after the 2 minute window and restores the last known state', async () => {
    const env = setup((url, opts) => (opts && opts.method === 'POST' ? {} : [stopping]));
    seed(env, running);
    await env.ctx._psAct(7, 'stop');
    vm.runInContext('_psTransit[7].deadline = 0', env.ctx);
    await fire(env);
    assert.strictEqual(pill(env).dataset.status, 'running');
    assert.ok(env.msgs.some((m) => /within 2 minutes/.test(m.t)));
});
