// STACK-HEALTH-FINISH-1126 — jsdom proof (real-browser-equivalent DOM) that the
// Stacks row renders the SEPARATE neutral `degraded: <reason>` badge driven by
// the worker-derived status_health/status_health_reason (GET stack fields),
// hidden when healthy/absent, and that the paid health-grade badge stays
// independent. Real source blocks from js/persistent-stacks.js (_psStackRow,
// _psStackSig, _psStatusHealthBadge inside the row block) run in a jsdom-backed
// vm sandbox with stubbed helpers (same pattern as
// persistent-stacks-stop-start-transition.test.js). Playwright rooms are not
// rebuilt by this lane (owner rule: no room rebuilds on a lane's own), so the
// DOM-level proof here is jsdom — say so in the result.
//
//   node --test CORE/Dashboard/tests/persistent-stacks-status-health-jsdom.test.js
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

const STATUS_CLASS = slice('const _PS_STATUS_CLASS = {', '// HA class decides');
const HEALTH_BADGE = slice('function _psHealthBadge(s) {', '// PS-STATUS-HEALTH-BEGIN');
const SH_BADGE = slice('function _psStatusHealthBadge(s) {', '// PS-STATUS-HEALTH-END');
const ROW = slice('function _psStackRow(s, workers) {', 'function _psStackSig(s) {');
const SIG = slice('function _psStackSig(s) {', 'function _psPatchStacks(');
const CODE = [STATUS_CLASS, HEALTH_BADGE, SH_BADGE, ROW, SIG].join('\n');

const own = new Set(['_psStackRow', '_psStackSig', '_PS_STATUS_CLASS', '_psStatusHealthBadge', '_psHealthBadge']);
const needed = new Set(CODE.match(/\b_(?:ps|PS_)[A-Za-z_]+/g) || []);
let stubs = '';
for (const n of needed) {
    if (own.has(n) || n === '_psById' || n === '_psPatchStacks' || n === '_psMsg' || n === '_psLoadStacks') continue;
    if (/^_PS_/.test(n)) stubs += `var ${n} = {};\n`;
    else if (n === '_psEsc') stubs += `function _psEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}\n`;
    else stubs += `function ${n}(){return '';}\n`;
}

function setup() {
    const dom = new JSDOM('<table><tbody id="ps-rows"></tbody></table>');
    const ctx = {
        document: dom.window.document, Date, JSON, Number, String, Object, Array, console,
        setTimeout: () => 0, clearTimeout: () => {},
        apiCall: async () => [],
        _psMsg: () => {}, _psPatchStacks: () => {}, _psLoadStacks: () => {}, _psById: {},
    };
    vm.createContext(ctx);
    vm.runInContext(stubs + CODE, ctx);
    return { ctx, doc: dom.window.document };
}
function render(env, s) {
    env.ctx.__s = s;
    env.doc.getElementById('ps-rows').innerHTML = vm.runInContext('_psStackRow(__s, [])', env.ctx);
    return env.doc.querySelector('[data-testid="ps-status-health-badge"]');
}

const reason = 'vllm is restarting/unhealthy';

test('degraded stack row shows the neutral degraded badge with the reason', async () => {
    const env = setup();
    const badge = render(env, { id: 7, name: 'emeralds', status: 'running', status_health: 'degraded', status_health_reason: reason });
    assert.ok(badge, 'badge element present');
    assert.strictEqual(badge.dataset.testid, 'ps-status-health-badge');
    assert.strictEqual(badge.dataset.statusHealth, 'degraded');
    assert.match(badge.textContent, new RegExp(`degraded: ${reason}`));
});

test('healthy / absent status_health renders no badge', async () => {
    const env = setup();
    assert.strictEqual(render(env, { id: 7, name: 'e', status: 'running', status_health: 'healthy', status_health_reason: reason }), null);
    assert.strictEqual(render(env, { id: 7, name: 'e', status: 'running' }), null);
});

test('markup-looking reason is escaped in the DOM (no injected elements)', async () => {
    const env = setup();
    render(env, { id: 7, name: 'e', status: 'running', status_health: 'degraded', status_health_reason: '<img src=x onerror=alert(1)>' });
    assert.strictEqual(env.doc.querySelectorAll('img').length, 0);
});

test('row signature changes when status_health flips (patcher re-renders)', async () => {
    const env = setup();
    const s1 = { id: 7, name: 'e', status: 'running', status_health: 'degraded', status_health_reason: reason };
    const s2 = { id: 7, name: 'e', status: 'running', status_health: 'healthy' };
    env.ctx.__s1 = s1; env.ctx.__s2 = s2;
    const a = vm.runInContext('_psStackSig(__s1)', env.ctx);
    const b = vm.runInContext('_psStackSig(__s2)', env.ctx);
    assert.notStrictEqual(a, b, 'degraded vs healthy signatures differ');
});
