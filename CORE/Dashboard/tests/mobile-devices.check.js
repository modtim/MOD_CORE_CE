// CUIP.14b check — js/mobile-devices.js: enrolled-phones panel (Settings →
// Integrations → Mobile app) with per-phone shelf expansion + Revoke.
//
// Requirements:
//  * rows render from GET /mobile-devices (wrapper key items OR devices OR a
//    bare array): platform, tier, last seen, shelf count
//  * Revoke 200 updates the row state (applied/purge text); 202 shows the
//    "awaiting second approver" text and does NOT mark the phone revoked
//  * every server string is rendered as TEXT — a device_id/platform carrying
//    <script> must never become markup (the module never touches innerHTML)
//  * fetch paths never include /v1 (API_URL already ends in /v1)
//  * the container is built by the module (no pages/settings.html markup),
//    mounted in the Mobile sub-tab, and init is a no-op when unmounted
//
// Technique: the module is an ES module, so its function bodies are extracted
// from source text (same as burst-accounts.check.js) and run against a tiny
// fake DOM + stubbed fetch.
//
//   node --test CORE/Dashboard/tests/mobile-devices.check.js
'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'mobile-devices.js'), 'utf8');

// --- static assertions -----------------------------------------------------
test('module never uses innerHTML', () => assert.ok(!/\.innerHTML/.test(src), 'no innerHTML allowed'));
test('fetch paths never include /v1', () => {
  assert.ok(!/\/v1\//.test(src) && !/['"`]\/v1/.test(src), 'paths must not add /v1');
  assert.ok(/\/mobile-devices\?include_revoked=false/.test(src), 'list path present');
  assert.ok(/\/mobile-devices\/\$\{encodeURIComponent\(key\)\}\/shelf/.test(src), 'shelf path present');
  assert.ok(/\/mobile-devices\/\$\{encodeURIComponent\(key\)\}\/revoke/.test(src), 'revoke path present');
});
test('settings.js mounts the panel in the Mobile sub-tab', () => {
  const settings = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
  assert.ok(/import \{ initializeMobileDevicesPanel \} from '\.\/mobile-devices\.js'/.test(settings), 'must import');
  assert.ok(/initializeMobileDevicesPanel\(\)/.test(settings), 'must call');
});

// --- dynamic: run the extracted bodies against a fake DOM + stub fetch -----
function extract(name) {
  const m = src.match(new RegExp(`(?:export )?(?:async )?function ${name}\\([\\s\\S]*?\\n}`));
  if (!m) throw new Error(`could not extract ${name} from mobile-devices.js`);
  return m[0].replace(/^export /, '');
}

const NAMES = ['getApiUrl', 'authHeader', '_mdWhen', '_mdNormalizeList', '_mdShelfLine',
  '_mdRevokeOutcome', 'renderRows', 'load', '_wireRow', 'toggleShelf', '_revokeFlow',
  'initializeMobileDevicesPanel'];

class FakeDoc {
  constructor() { this.all = []; this.host = null; }
  createElement(tag) {
    const n = { tag, id: '', className: '', hidden: false, children: [], dataset: {}, type: '', listeners: {} };
    n.appendChild = (c) => { c._parent = n; n.children.push(c); };
    n.addEventListener = (ev, fn) => { n.listeners[ev] = fn; };
    Object.defineProperty(n, 'textContent', {
      get: () => (n._text || '') + n.children.map((c) => c._text || '').join(' '),
      set: (v) => { n._text = v; n.children = []; },
    });
    Object.defineProperty(n, 'parentNode', { get: () => n._parent });
    this.all.push(n);
    return n;
  }
  getElementById(id) { return this.all.find((n) => n.id === id) || null; }
}
function appendChild(parent, child) { child._parent = parent; parent.children.push(child); }
function textOf(n) {
  return (n._text || '') + n.children.map(textOf).join(' ');
}

let doc, fetchLog, confirmAnswer;
function resetEnv() {
  doc = new FakeDoc();
  doc.host = doc.createElement('div');
  doc.host.id = 'int-subpanel-mobile';
  doc.host.children = [];
  doc.all.push(doc.host);
  globalThis.document = doc;
  globalThis.window = { APP_CONFIG: { API_URL: 'https://mod.example.com/api/v1' }, showError: async () => {}, confirm: () => confirmAnswer };
  globalThis.sessionStorage = { getItem: () => 'tok' };
  fetchLog = [];
}

const mod = new Function(NAMES.map(extract).join('\n') + `\nreturn { ${NAMES.join(', ')} };`)();

function stubFetch(handler) {
  globalThis.fetch = async (url, opts) => {
    fetchLog.push({ url, method: opts && opts.method });
    const r = handler(url, opts);
    return { ok: r.ok, status: r.status, json: async () => r.body };
  };
}
const DEVICES = [
  { device_id: 'phone-1', platform: 'android', app_build: 'v1.0.71', integrity_tier: 'AAL2',
    created_at: '2026-10-01T10:00:00Z', last_seen_at: '2026-10-08T09:00:00Z',
    revoked_at: null, revoked: false, active_shelf_items: 2 },
  { device_id: '<script>alert(1)</script>', platform: '<img src=x onerror=alert(1)>', app_build: 'b',
    integrity_tier: null, created_at: null, last_seen_at: null, revoked_at: null, revoked: false },
];

test('renders rows from a stubbed list (devices wrapper)', async () => {
  resetEnv();
  stubFetch((url) => ({ ok: true, status: 200, body: { devices: DEVICES } }));
  await mod.initializeMobileDevicesPanel();
  const row = doc.getElementById('md-row-phone-1');
  assert.ok(row, 'row for phone-1 exists');
  const t = textOf(row);
  assert.ok(t.includes('android'), 'platform rendered');
  assert.ok(t.includes('AAL2'), 'tier rendered');
  assert.ok(t.includes('shelf 2'), 'shelf count rendered');
  assert.ok(t.includes('Revoke'), 'revoke button rendered');
  assert.ok(doc.getElementById('md-shelf-list-phone-1').hidden === true, 'shelf list starts collapsed');
  assert.ok(doc.all.some((n) => n.id === 'md-status'), 'status element built by the module');
});

test('server strings with <script> render as text', async () => {
  resetEnv();
  stubFetch((url) => ({ ok: true, status: 200, body: { items: DEVICES } }));
  await mod.initializeMobileDevicesPanel();
  const evil = doc.all.filter((n) => n.id.startsWith('md-row-')).find((n) => textOf(n).includes('onerror'));
  assert.ok(evil, 'the hostile row exists');
  assert.ok(textOf(evil).includes('<script>alert(1)</script>'), 'device_id kept as literal text');
  assert.ok(!doc.all.some((n) => n.tag === 'script' || n.tag === 'img'), 'no markup was created from server strings');
});

test('shelf expand renders items, second click collapses', async () => {
  resetEnv();
  stubFetch((url, opts) => {
    if (url.includes('/revoke')) return { ok: true, status: 200, body: {} };
    return { ok: true, status: 200, body: { device_id: 'phone-1', items: [
      { item_id: 7, device_id: 'phone-1', resource_type: 'file', resource_id: 'abc', staged_at: '2026-10-02T08:00:00Z', expires_at: null },
    ] } };
  });
  await mod.initializeMobileDevicesPanel();
  const btn = doc.getElementById('md-shelf-phone-1');
  assert.ok(btn.listeners.click, 'shelf button wired');
  await btn.listeners.click();
  const list = doc.getElementById('md-shelf-list-phone-1');
  assert.ok(list.hidden === false && textOf(list).includes('file abc'), 'items rendered as text lines');
  await btn.listeners.click();
  assert.ok(list.hidden === true, 'second click collapses the shelf');
});

test('revoke 200 updates the row', async () => {
  resetEnv();
  confirmAnswer = true;
  stubFetch((url, opts) => (opts && opts.method) === 'POST'
    ? { ok: true, status: 200, body: { device_id: 'phone-1', applied: true, purge_client: true, purged_items: 3 } }
    : { ok: true, status: 200, body: { items: DEVICES } });
  await mod.initializeMobileDevicesPanel();
  await mod._revokeFlow('phone-1');
  const state = doc.getElementById('md-state-phone-1');
  assert.ok(textOf(state).startsWith('Revoked'), 'row state updated');
  assert.ok(textOf(state).includes('purged 3 shelf item(s)'), 'purge count surfaced');
  assert.ok(textOf(state).includes('client purge requested'), 'purge_client surfaced');
  assert.ok(fetchLog.some((f) => f.method === 'POST' && f.url.endsWith('/revoke')), 'POST issued');
});

test('revoke 202 shows awaiting-second-approver text', async () => {
  resetEnv();
  confirmAnswer = true;
  stubFetch((url, opts) => (opts && opts.method) === 'POST'
    ? { ok: false, status: 202, body: { detail: { code: 'approval_required' } } }
    : { ok: true, status: 200, body: { items: DEVICES } });
  await mod.initializeMobileDevicesPanel();
  await mod._revokeFlow('phone-1');
  const state = doc.getElementById('md-state-phone-1');
  assert.ok(textOf(state) === 'Revoke requested — awaiting second approver', 'awaiting text, not revoked');
});

test('revoke failure shows error text', async () => {
  resetEnv();
  confirmAnswer = true;
  stubFetch((url, opts) => (opts && opts.method) === 'POST'
    ? { ok: false, status: 403, body: { detail: { code: 'revoke_not_allowed', message: 'nope' } } }
    : { ok: true, status: 200, body: { items: DEVICES } });
  await mod.initializeMobileDevicesPanel();
  await mod._revokeFlow('phone-1');
  const state = doc.getElementById('md-state-phone-1');
  assert.ok(textOf(state).includes('Could not revoke phone'), 'error text on the row');
});

test('fetch paths never add /v1', async () => {
  resetEnv();
  confirmAnswer = true;
  stubFetch((url, opts) => ({ ok: true, status: opts.method === 'POST' ? 200 : 200, body: { items: DEVICES } }));
  await mod.initializeMobileDevicesPanel();
  await mod.toggleShelf('phone-1');
  await mod._revokeFlow('phone-1');
  const urls = fetchLog.map((f) => f.url);
  assert.ok(urls[0] === 'https://mod.example.com/api/v1/mobile-devices?include_revoked=false', 'list path');
  assert.ok(urls.some((u) => u === 'https://mod.example.com/api/v1/mobile-devices/phone-1/shelf'), 'shelf path');
  assert.ok(urls.some((u) => u === 'https://mod.example.com/api/v1/mobile-devices/phone-1/revoke'), 'revoke path');
  assert.ok(!urls.some((u) => u.includes('/v1/v1') || u.includes('/v1/v1/')), 'no doubled /v1');
});

test('init is a no-op when the panel is not mounted', async () => {
  resetEnv();
  doc.all = [];  // no int-subpanel-mobile in the DOM
  stubFetch(() => ({ ok: true, status: 200, body: { items: [] } }));
  await mod.initializeMobileDevicesPanel();
  assert.ok(fetchLog.length === 0, 'no fetch when unmounted');
});
