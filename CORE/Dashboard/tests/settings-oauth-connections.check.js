// sz-4.9b check — js/settings-oauth-connections.js: the OAuth connections
// panel inside Settings → Integrations (providers + Connect for tenant admins,
// the credential-vault table + Disconnect, the ?oauth=connected return toast).
//
// Static guards:
//   * pages/settings.html ships exactly ONE EMPTY int-oauth-connections
//     container inside the Integrations panel and NO <script> tag for the
//     module — ui.js mounts pages via innerHTML, which never executes an
//     injected <script> tag (same bug class as payment-settings.html /
//     docker-registry.html before they were fixed), so the container must
//     stay markup-only and the module is wired from settings.js instead,
//   * settings.js imports initializeSettingsOauthConnections and calls it
//     from loadIntegrations() (same pattern as the Mobile sub-tab's
//     initializeMobileEnrollmentPage()/initializeMobileDevicesPanel()),
//   * the module never touches innerHTML (every server string is textContent),
//   * fetch paths never add /v1 (API_URL already ends in /v1).
//
// Dynamic guards (extracted bodies run against a tiny fake DOM + stubbed
// fetch, same technique as mobile-devices.check.js):
//   * providers render with Configured / Not configured badges;
//   * a NON-admin gets no Connect buttons and no Actions column;
//   * Connect POSTs {redirect_uri: <page URL>#integrations} to
//     /integrations/oauth/{provider}/start and navigates to authorization_url;
//   * ?oauth=connected&provider=x shows the toast and strips the query
//     (history.replaceState);
//   * Disconnect asks confirm() first, then DELETEs the row;
//   * rows with the API's dead statuses (refresh_failed / reconnect_required /
//     revoked — oauth_refresh.py:94-96 + routers/oauth_connections.py:502; models.py
//     ck_oauth_connections_status allows no others) get the amber warning badge +
//     a Reconnect button; 'active' rows get neither.
//   * malicious provider/account strings stay TEXT (no element injection).
//
//   node CORE/Dashboard/tests/settings-oauth-connections.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings-oauth-connections.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings.html'), 'utf8');

// --- static assertions -----------------------------------------------------
(function() {
  assert.ok(!/\.innerHTML/.test(src), 'module must never touch innerHTML');
  assert.ok(!/\/v1\//.test(src) && !/['"`]\/v1/.test(src), 'paths must not add /v1');
  assert.ok(/\/integrations\/oauth\/providers/.test(src), 'providers path present');
  assert.ok(/\/integrations\/oauth\/\$\{encodeURIComponent\(provider\)\}\/start/.test(src), 'start path present');
  assert.ok(/\/integrations\/oauth\/connections\/\$\{encodeURIComponent/.test(src), 'disconnect path present');
})();

(function() {
  const at = html.indexOf('<div id="main-panel-integrations"');
  assert.ok(at >= 0, 'Integrations panel must exist');
  const next = html.indexOf('<div id="main-panel-storage"', at);
  assert.ok(next > at, 'Integrations panel end anchor must exist');
  const panel = html.slice(at, next);
  const m = panel.match(/<div id="int-oauth-connections"([^>]*)>([\s\S]*?)<\/div>/);
  assert.ok(m, 'int-oauth-connections container must exist inside the Integrations panel');
  assert.ok(m[2].trim() === '', 'the container ships EMPTY — no <script> tag (never executes via innerHTML mount)');
  assert.ok((panel.match(/<script/g) || []).length === 0, 'no <script> tag in the Integrations panel');
})();

(function() {
  const settings = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
  assert.ok(/import\s*\{\s*initializeSettingsOauthConnections\s*\}\s*from\s*['"]\.\/settings-oauth-connections\.js['"]/.test(settings),
    'settings.js must import initializeSettingsOauthConnections from ./settings-oauth-connections.js');
  assert.ok(/initializeSettingsOauthConnections\(\)/.test(settings),
    'settings.js must call initializeSettingsOauthConnections() (from loadIntegrations())');
})();

// --- dynamic: fake DOM + stub fetch ----------------------------------------
function extract(name) {
  const m = src.match(new RegExp(`(?:export )?(?:async )?function ${name}\\([\\s\\S]*?\\n}`));
  if (!m) throw new Error(`could not extract ${name} from settings-oauth-connections.js`);
  return m[0].replace(/^export /, '');
}

const NAMES = ['getApiUrl', 'authHeader', '_oauthWhen', '_oauthNormalizeList', '_oauthHref',
  '_oauthSearch', '_oauthRedirectUri', '_oauthIsTenantAdmin', '_oauthLoadProviders',
  '_oauthLoadConnections', '_oauthEl', '_oauthBadge', '_oauthFail', '_oauthClear',
  '_oauthRenderProviders', '_oauthRenderConnections', '_oauthConnect', '_oauthDisconnect',
  '_oauthConnectedToast', 'initializeSettingsOauthConnections'];

class FakeDoc {
  constructor() { this.all = []; }
  createElement(tag) {
    const n = { tag, id: '', className: '', hidden: false, children: [], dataset: {}, type: '', listeners: {}, disabled: false, title: '' };
    n.appendChild = (c) => { c._parent = n; n.children.push(c); };
    n.addEventListener = (ev, fn) => { n.listeners[ev] = fn; };
    n.removeChild = (c) => { n.children = n.children.filter((x) => x !== c); };
    Object.defineProperty(n, 'childNodes', { get: () => n.children });
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
function textOf(n) { return (n._text || '') + n.children.map(textOf).join(' '); }
function findEl(root, pred, out = []) {
  if (pred(root)) out.push(root);
  root.children.forEach((c) => findEl(c, pred, out));
  return out;
}

let doc, fetchLog, confirmAnswer, replaceStateCalls, mod;
function resetEnv(href) {
  doc = new FakeDoc();
  doc.host = doc.createElement('div');
  doc.host.id = 'int-oauth-connections';
  doc.host.children = [];
  doc.all.push(doc.host);
  globalThis.document = doc;
  globalThis.window = { APP_CONFIG: { API_URL: 'https://mod.example.com/api/v1' }, confirm: () => confirmAnswer };
  globalThis.window.location = href;
  globalThis.sessionStorage = { getItem: () => 'tok' };
  globalThis.history = { replaceState: (a, b, url) => { replaceStateCalls.push(url); } };
  fetchLog = [];
  replaceStateCalls = [];
  mod = new Function(NAMES.map(extract).join('\n') + `\nreturn { ${NAMES.join(', ')} };`)();
}
function stubFetch(handler) {
  globalThis.fetch = async (url, opts) => {
    fetchLog.push({ url, opts });
    return handler(url, opts);
  };
}
function ok(payload) { return { ok: true, status: 200, json: async () => payload }; }
function notFound() { return { ok: false, status: 404, json: async () => ({}) }; }

const PROVIDERS = [
  { provider: 'google', configured: true },
  { provider: 'github', configured: false },
];
const CONNECTIONS = [
  { id: 7, provider: 'google', external_account_id: 'a@example.com', scopes: ['openid', 'email'], status: 'active', access_token_expires_at: '2026-11-01T00:00:00Z', created_at: '2026-10-01T00:00:00Z' },
  { id: 9, provider: 'github', external_account_id: null, scopes: null, status: 'reconnect_required', access_token_expires_at: null, created_at: '2026-09-01T00:00:00Z' },
  { id: 11, provider: 'notion', external_account_id: 'n@x', scopes: ['docs'], status: 'refresh_failed', access_token_expires_at: null, created_at: '2026-09-15T00:00:00Z' },
];

// --- the four scenarios run SEQUENTIALLY (shared doc/fetchLog) --------------
(async () => {
  // 1) providers + badges render; non-admin gets no Connect buttons
  resetEnv('https://mod.example.com/settings?tab=integrations#integrations');
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: false, parent_user_id: 3 });
    if (u.includes('/providers')) return ok(PROVIDERS);
    if (u.includes('/connections')) return ok(CONNECTIONS);
    return notFound();
  });
  await mod.initializeSettingsOauthConnections();
  const rows = findEl(doc.host, (n) => n.tag === 'div' && String(n.className).includes('oauth-provider-row'));
  assert.ok(rows.length === 2, 'two provider rows render');
  assert.ok(textOf(rows[0]).includes('google') && textOf(rows[0]).includes('Configured'), 'badge text renders');
  assert.ok(textOf(rows[1]).includes('Not configured'), 'not-configured badge');
  assert.ok(findEl(doc.host, (n) => n.tag === 'button').length === 0, 'non-admin sees no Connect/Disconnect buttons');
  const ths = findEl(doc.host, (n) => n.tag === 'th');
  assert.ok(!ths.some((t) => textOf(t) === 'Actions'), 'no Actions column for non-admin');

  // 2) admin: Connect posts the right body + navigates; Disconnect confirm->DELETE
  resetEnv('https://mod.example.com/settings?tab=integrations#integrations');
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/providers')) return ok(PROVIDERS);
    if (u.includes('/connections')) return ok(CONNECTIONS);
    if (u.includes('/start')) return ok({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?x=1' });
    return ok({ id: 7, status: 'revoked' });
  });
  await mod.initializeSettingsOauthConnections();
  const buttons = findEl(doc.host, (n) => n.tag === 'button');
  assert.ok(buttons.length > 0, 'admin sees buttons');
  const connectBtn = buttons.find((b) => textOf(b) === 'Connect' && !b.disabled);
  assert.ok(connectBtn, 'configured provider has an enabled Connect button');
  const disabledBtn = buttons.find((b) => textOf(b) === 'Connect' && b.disabled);
  assert.ok(disabledBtn && disabledBtn.title, 'not-configured provider: disabled Connect + tooltip');
  await mod._oauthConnect('google');
  const start = fetchLog.find((f) => f.url.includes('/start'));
  assert.ok(start && start.opts.method === 'POST', 'start is a POST');
  const body = JSON.parse(start.opts.body);
  assert.ok(body.redirect_uri === 'https://mod.example.com/settings#integrations' && Object.keys(body).join(',') === 'redirect_uri',
    'body is exactly {redirect_uri: <page URL>#integrations}');
  assert.ok(String(globalThis.window.location) === 'https://accounts.google.com/o/oauth2/v2/auth?x=1', 'navigates to authorization_url');
  confirmAnswer = false;
  const discBtn = buttons.find((b) => textOf(b) === 'Disconnect');
  assert.ok(discBtn, 'Disconnect button exists');
  const before = fetchLog.length;
  const denied = await mod._oauthDisconnect(7);
  assert.ok(denied === false && fetchLog.length === before, 'confirm()=false: no DELETE issued');
  confirmAnswer = true;
  const done = await mod._oauthDisconnect(7);
  assert.ok(done === true, 'disconnect returns true');
  const del = fetchLog.find((f) => f.url.includes('/connections/7') && f.opts.method === 'DELETE');
  assert.ok(del, 'DELETE /integrations/oauth/connections/7 after confirm');

  // 3) ?oauth=connected toast + query stripped
  resetEnv('https://mod.example.com/settings?oauth=connected&provider=google#integrations');
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: false, parent_user_id: 3 });
    if (u.includes('/providers')) return ok(PROVIDERS);
    if (u.includes('/connections')) return ok(CONNECTIONS);
    return notFound();
  });
  await mod.initializeSettingsOauthConnections();
  const toast = findEl(doc.host, (n) => String(n.className || '').includes('oauth-connected-toast'));
  assert.ok(toast.length === 1 && textOf(toast[0]).includes('google'), 'success toast names the provider');
  assert.ok(replaceStateCalls.length === 1 && replaceStateCalls[0] === 'https://mod.example.com/settings#integrations',
    'history.replaceState strips the oauth query');

  // 3b) dead statuses (reconnect_required / refresh_failed) -> warning badge + Reconnect;
  //     active -> plain text + Disconnect
  resetEnv('https://mod.example.com/settings#integrations');
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/providers')) return ok(PROVIDERS);
    if (u.includes('/connections')) return ok(CONNECTIONS);
    return notFound();
  });
  await mod.initializeSettingsOauthConnections();
  const statusTd = findEl(doc.host, (n) => n.tag === 'td' && (textOf(n) === 'reconnect_required' || textOf(n) === 'refresh_failed'));
  assert.ok(statusTd.length === 2, 'both dead-status rows render');
  assert.ok(statusTd.every((t) => t.children.some((c) => c.tag === 'span' && String(c.className).includes('bg-amber-50'))),
    'dead-status cells carry the amber warning badge');
  const activeTd = findEl(doc.host, (n) => n.tag === 'td' && textOf(n) === 'active');
  assert.ok(activeTd.length === 1 && activeTd[0].children.length === 0,
    'active cell is plain text, no badge');
  const reconBtns = findEl(doc.host, (n) => n.tag === 'button' && textOf(n) === 'Reconnect');
  assert.ok(reconBtns.length === 2, 'one Reconnect button per dead row');
  const discBtns = findEl(doc.host, (n) => n.tag === 'button' && textOf(n) === 'Disconnect');
  assert.ok(discBtns.length === 1, 'only the active row keeps Disconnect');

  // 4) malicious strings stay text
  resetEnv('https://mod.example.com/settings#integrations');
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/providers')) return ok([{ provider: '<img src=x onerror=alert(1)>', configured: true }]);
    if (u.includes('/connections')) return ok([{ id: 1, provider: 'google', external_account_id: '<script>alert(1)</script>', scopes: ['<b>x</b>'], status: 'active', access_token_expires_at: null, created_at: null }]);
    return notFound();
  });
  await mod.initializeSettingsOauthConnections();
  const injected = findEl(doc.host, (n) => n.tag === 'img' || n.tag === 'script' || n.tag === 'b');
  assert.ok(injected.length === 0, 'no injected elements');
  const evilRows = findEl(doc.host, (n) => n.tag === 'div' && String(n.className).includes('oauth-provider-row'));
  assert.ok(evilRows.length === 1 && textOf(evilRows[0]).includes('<img src=x'), 'malicious provider rendered as text');
  const tds = findEl(doc.host, (n) => n.tag === 'td');
  assert.ok(tds.some((t) => textOf(t).includes('<script>alert(1)</script>')), 'malicious account rendered as text');
})();

console.log('settings-oauth-connections.check.js: all assertions passed');
