// Node check for the BYO Activate "Skip missing source objects?" checkbox
// (CORE/Dashboard/js/storage-connections.js + api.js storageConnections.activate).
//
//   node --test CORE/Dashboard/tests/storage-connections-skip-missing.check.js
//
// Asserts: (a) api.js appends ?skip_missing_source=true ONLY when
// opts.skipMissingSource === true (default call unchanged = fail-closed);
// (b) the Activate confirm dialog renders the checkbox UNTICKED with the
// owner-approved label + helper text, and Cancel sends nothing; (c) Activate
// with the box unticked calls storageConnections.activate with
// skipMissingSource false, ticked -> true; (d) the verified+skipped_missing
// status card shows real counts and the skipped keys, every key escaped.
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');
const scSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'storage-connections.js'), 'utf8');

// ---- (a) api.js ----------------------------------------------------------
function loadActivate() {
  const m = apiSrc.match(/activate: \(id, opts\) => fetchApi\([\s\S]*?\{ method: 'POST' \}\),/);
  assert.ok(m, 'could not locate storageConnections.activate in api.js');
  const calls = [];
  const fetchApi = (url, init) => { calls.push([url, init]); return Promise.resolve({}); };
  const obj = vm.runInNewContext(`({ ${m[0]} })`, { fetchApi });
  return { activate: obj.activate, calls };
}

test('api.activate: default and falsy opts send no query param', async () => {
  const { activate, calls } = loadActivate();
  await activate(7);
  await activate(7, {});
  await activate(7, { skipMissingSource: false });
  await activate(7, { skipMissingSource: 'yes' });
  assert.deepStrictEqual(calls.map((c) => c[0]), Array(4).fill('/storage-connections/7/activate'));
  assert.ok(calls.every((c) => c[1].method === 'POST'));
});

test('api.activate: skipMissingSource true appends ?skip_missing_source=true', async () => {
  const { activate, calls } = loadActivate();
  await activate(7, { skipMissingSource: true });
  assert.strictEqual(calls[0][0], '/storage-connections/7/activate?skip_missing_source=true');
});

// ---- module sandbox (storage-connections.js) -------------------------------
function makeEl() {
  const el = { _html: '', _nodes: {}, listeners: {}, checked: false, removed: false, children: [] };
  el.setAttribute = () => {};
  el.addEventListener = (ev, fn) => { (el.listeners[ev] = el.listeners[ev] || []).push(fn); };
  el.remove = () => { el.removed = true; };
  el.focus = () => {};
  el.fire = (ev, arg) => (el.listeners[ev] || []).forEach((f) => f(arg || { target: el }));
  el.querySelector = (sel) => {
    if (!el._nodes[sel]) el._nodes[sel] = makeEl();
    return el._nodes[sel];
  };
  Object.defineProperty(el, 'innerHTML', { get: () => el._html, set: (v) => { el._html = v; } });
  return el;
}

function loadModule(activateImpl) {
  const body = [];
  const docListeners = {};
  const document = {
    createElement: () => makeEl(),
    querySelector: () => null,
    getElementById: () => null,
    addEventListener: (ev, fn) => { docListeners[ev] = fn; },
    removeEventListener: () => {},
    body: { appendChild: (n) => body.push(n) },
  };
  const activateCalls = [];
  const storageConnections = {
    activate: (id, opts) => { activateCalls.push([id, opts]); return Promise.resolve({ migration_status: 'done' }); },
  };
  let code = scSrc.replace(/^import .*$/gm, '').replace(/^export /gm, '');
  code += '\n;globalThis.__t = { confirmActivate, _migrationRowHtml, handleRowAction };';
  const ctx = vm.createContext({
    document, storageConnections, console, alert: () => {}, confirm: () => true,
    tenantStorage: {}, isPaidEdition: () => false, loadModal: () => null, closeModal: () => {},
    setTimeout, clearTimeout, Date, Promise,
  });
  vm.runInContext(code, ctx);
  return { t: ctx.__t, body, activateCalls };
}

// ---- (b) dialog ------------------------------------------------------------
test('dialog: checkbox unticked by default with approved label + helper; Cancel resolves null', async () => {
  const { t, body } = loadModule();
  const p = t.confirmActivate();
  assert.strictEqual(body.length, 1);
  const html = body[0].innerHTML;
  assert.ok(html.includes('Skip missing source objects?'));
  assert.ok(html.includes('Leave it unticked to stop at the first missing file (default).'));
  assert.ok(html.includes('MOD copies what it can and tells you exactly which objects were already gone.'));
  assert.ok(/<input type="checkbox" id="activate-skip-missing"[^>]*>/.test(html));
  assert.ok(!/checked/.test(html.match(/<input type="checkbox"[^>]*>/)[0]), 'checkbox must be unticked');
  assert.ok(html.includes('>Cancel<') && html.includes('>Activate<'));
  body[0].querySelector('#activate-confirm-cancel').fire('click');
  assert.strictEqual(await p, null);
  assert.ok(body[0].removed);
});

// ---- (c) wiring ------------------------------------------------------------
async function runActivate(tick, cancel) {
  const { t, body, activateCalls } = loadModule();
  const p = t.handleRowAction('activate', 42);
  await Promise.resolve();
  const dlg = body[0];
  if (cancel) {
    dlg.querySelector('#activate-confirm-cancel').fire('click');
  } else {
    dlg.querySelector('#activate-skip-missing').checked = tick;
    dlg.querySelector('#activate-confirm-ok').fire('click');
  }
  await p;
  return activateCalls;
}

test('unticked Activate sends skipMissingSource false (no query param upstream)', async () => {
  const calls = await runActivate(false, false);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0][0], 42);
  assert.strictEqual(calls[0][1].skipMissingSource, false);
});

test('ticked Activate sends skipMissingSource true', async () => {
  const calls = await runActivate(true, false);
  assert.strictEqual(calls[0][1].skipMissingSource, true);
});

test('Cancel sends no activate request', async () => {
  const calls = await runActivate(false, true);
  assert.strictEqual(calls.length, 0);
});

// ---- (d) status card -------------------------------------------------------
test('status card: verified + skipped_missing shows real counts and escaped keys', () => {
  const { t } = loadModule();
  const html = t._migrationRowHtml({
    migration_status: 'verified', skipped_missing: true,
    migrated_object_count: 3, total_objects: 15, missing_source_count: 12,
    missing_source_keys: ['a/b.png', '<img src=x onerror=alert(1)>'],
  });
  assert.ok(html.includes('3 of 15 objects copied, 12 were already missing from the old backend (listed below).'));
  assert.ok(html.includes('Those files are no longer readable; the rest of your library is live.'));
  assert.ok(html.includes('<code>a/b.png</code>'));
  assert.ok(!html.includes('<img src=x'), 'raw key must not reach innerHTML');
  assert.ok(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(html.includes('Showing the first 2 of 12.'));
});

test('status card: plain verified (no skip) keeps the original wording', () => {
  const { t } = loadModule();
  const html = t._migrationRowHtml({ migration_status: 'verified', migrated_object_count: 5, total_objects: 5 });
  assert.ok(html.includes('Migration complete'));
  assert.ok(!html.includes('already missing'));
});
