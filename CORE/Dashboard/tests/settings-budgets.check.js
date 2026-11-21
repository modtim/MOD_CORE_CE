// sz-1.x check — js/settings-budgets.js: the Settings "Workflow budgets" (cost
// caps) panel.
//
// Static guards:
//   * the module never touches innerHTML (every server string is textContent),
//   * fetch paths never add /v1 (API_URL already ends in /v1 — same base/prefix
//     rule settings-oauth-connections.js uses for /integrations/oauth/...),
//   * GET /budgets, PUT /budgets/default and PUT /budgets/workflows/{id} are the
//     only routes used, and the module NEVER issues a DELETE (removing a cap is
//     a PUT with nulls — ISO 27001, no hard deletes),
//   * pages/settings.html: if the budgets-panel container is already mounted it
//     must be EMPTY and the panel must carry no <script> tag (ui.js mounts pages
//     via innerHTML, which never executes an injected module tag). Pre-mount the
//     lead adds the container + the settings.js import/call, so a missing
//     container is reported, not failed.
//
// Dynamic guards (extracted bodies against a tiny fake DOM + stubbed fetch,
// same technique as settings-oauth-connections.check.js):
//   * the default row + one row per override render,
//   * null caps render "No cap", 0 renders "0 (blocks spend)" (bug 233 semantics),
//   * blank form fields PUT real nulls,
//   * an out-of-range / non-numeric cap is blocked CLIENT-SIDE (no fetch at all),
//   * a non-admin gets the read-only note on the default row (no Edit button),
//   * a server 4xx shows its `detail` in the status line as TEXT — an injected
//     "<img onerror=...>" string stays text, no element is created,
//   * loading / empty states render.
//
//   node CORE/Dashboard/tests/settings-budgets.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings-budgets.js'), 'utf8');

// --- static assertions -----------------------------------------------------
(function() {
  assert.ok(!/\.innerHTML/.test(src), 'module must never touch innerHTML');
  assert.ok(!/\/v1\//.test(src) && !/['"`]\/v1/.test(src), 'paths must not add /v1');
  assert.ok(/fetch\(\`\$\{apiUrl\}\/budgets\`/.test(src), 'GET /budgets path present');
  assert.ok(/'\/budgets\/default'/.test(src), 'PUT /budgets/default path present');
  assert.ok(/\/budgets\/workflows\/\$\{encodeURIComponent/.test(src), 'PUT /budgets/workflows/{id} path present');
  assert.ok(/\/road\?limit=100/.test(src), 'workflow picker reuses the Dashboard /road?limit=100 list');
  assert.ok(!/['"]DELETE['"]/.test(src) && !/method:\s*`?DELETE/.test(src), 'module never issues a DELETE method');
  assert.ok(/export async function initBudgetsPanel\(container\)/.test(src), 'exported initBudgetsPanel(container) present');
})();

// sz-4.x mount assertions (the lead wires the panel — these are HARD now):
//   * pages/settings.html carries id="int-budgets" EXACTLY once, as a plain
//     empty div (class budgets-panel mt-6),
//   * settings.js imports initBudgetsPanel and calls it INSIDE loadIntegrations(),
//     passing document.getElementById('int-budgets'), with a try/catch AND a
//     .catch() on the returned promise (initBudgetsPanel is async).
(function() {
  const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings.html'), 'utf8');
  const n = (html.match(/id="int-budgets"/g) || []).length;
  assert.ok(n === 1, 'pages/settings.html must contain id="int-budgets" exactly once (found ' + n + ')');
  assert.ok(/<div id="int-budgets" class="budgets-panel mt-6"><\/div>/.test(html),
    'int-budgets container must be a plain empty div with class budgets-panel mt-6');
  const settings = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
  assert.ok(/import\s*\{\s*initBudgetsPanel\s*\}\s*from\s*['"]\.\/settings-budgets\.js['"]/.test(settings),
    'settings.js must import initBudgetsPanel from ./settings-budgets.js');
  const at = settings.search(/async function loadIntegrations\s*\(\s*\)\s*\{/);
  assert.ok(at >= 0, 'loadIntegrations() present in settings.js');
  const end = settings.indexOf('\n}', at);
  assert.ok(end > at, 'loadIntegrations body found');
  const body = settings.slice(at, end);
  assert.ok(/initBudgetsPanel\(document\.getElementById\('int-budgets'\)\)/.test(body),
    'loadIntegrations() must call initBudgetsPanel(document.getElementById(\'int-budgets\'))');
  assert.ok(/initBudgetsPanel\([\s\S]*?\)\s*\.catch\(/.test(body),
    'the async initBudgetsPanel promise must have .catch() attached (no unhandled rejection)');
  assert.ok(/try\s*\{\s*initBudgetsPanel\(/.test(body),
    'the initBudgetsPanel call must sit inside a try/catch');
})();

// --- dynamic: fake DOM + stub fetch ----------------------------------------
function extract(name) {
  const m = src.match(new RegExp(`(?:export )?(?:async )?function ${name}\\([\\s\\S]*?\\n}`));
  if (!m) throw new Error(`could not extract ${name} from settings-budgets.js`);
  return m[0].replace(/^export /, '');
}

const NAMES = ['getApiUrl', 'authHeader', '_budgetsEl', '_budgetsCapText', '_budgetsFail',
  '_budgetsDetailText', '_budgetsLoadList', '_budgetsLoadWorkflows', '_budgetsValidateCaps',
  '_budgetsPut', '_budgetsFindRows', '_budgetsRowKey', '_budgetsRenderTable',
  '_budgetsOpenForm', '_budgetsAddOverride', 'initBudgetsPanel'];

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
function textOf(n) { return (n._text || '') + n.children.map(textOf).join(' '); }
function findEl(root, pred, out = []) {
  if (pred(root)) out.push(root);
  root.children.forEach((c) => findEl(c, pred, out));
  return out;
}
function statusDiv() {
  const hits = findEl(doc.host, (n) => String(n.className || '').includes('budgets-status'));
  return hits[0];
}

let doc, fetchLog, mod, prompts;
function resetEnv(href, admin) {
  doc = new FakeDoc();
  doc.host = doc.createElement('div');
  doc.host.id = 'budgets-panel';
  doc.host.children = [];
  doc.all.push(doc.host);
  globalThis.document = doc;
  prompts = [];
  globalThis.window = {
    APP_CONFIG: { API_URL: 'https://mod.example.com/api/v1' },
    prompt: (q) => prompts.shift(),
    location: href,
  };
  globalThis.sessionStorage = { getItem: () => 'tok' };
  fetchLog = [];
  const consts = (src.match(/^const .*;$/gm) || []).join('\n');
  mod = new Function(consts + '\n' + NAMES.map(extract).join('\n') + `\nreturn { ${NAMES.join(', ')} };`)();
}
function stubFetch(handler) {
  globalThis.fetch = async (url, opts) => {
    fetchLog.push({ url, opts });
    return handler(url, opts);
  };
}
function ok(payload) { return { ok: true, status: 200, json: async () => payload }; }
function err(status, payload) { return { ok: false, status, json: async () => payload }; }

const LIST = {
  tenant_id: 3,
  default: { workflow_id: null, workflow_name: null, max_cost_per_run_credits: null, max_cost_per_period_credits: 5000, period_window: 'weekly', updated_at: '2026-10-01T00:00:00Z' },
  overrides: [
    { workflow_id: 11, workflow_name: 'Upscale HD', max_cost_per_run_credits: 0, max_cost_per_period_credits: null, period_window: 'monthly', updated_at: null },
    { workflow_id: 12, workflow_name: '<img src=x onerror=alert(1)>', max_cost_per_run_credits: 250, max_cost_per_period_credits: 10000000, period_window: 'daily', updated_at: null },
  ],
};
const WORKFLOWS = [{ id: 11, name: 'Upscale HD' }, { id: 12, name: '<img src=x onerror=alert(1)>' }];

(async () => {
  // 1) admin: default + overrides render, cap semantics, Edit buttons present
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: false, parent_user_id: null });
    if (u.includes('/budgets')) return ok(LIST);
    if (u.includes('/road')) return ok(WORKFLOWS);
    return err(404, { detail: 'Budget target not found' });
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  const trs = findEl(doc.host, (n) => n.tag === 'tr');
  assert.ok(trs.length === 4, 'header + default + 2 overrides render');
  const defRow = trs[1];
  assert.ok(textOf(defRow).includes('Tenant default'), 'default row labelled');
  assert.ok(textOf(defRow).includes('No cap'), 'null per-run cap -> No cap');
  assert.ok(textOf(defRow).includes('5,000'), 'per-period cap renders with separators');
  assert.ok(textOf(defRow).includes('weekly'), 'window renders');
  const wfRow = trs[2];
  assert.ok(textOf(wfRow).includes('Upscale HD') && textOf(wfRow).includes('0 (blocks spend)'), '0 cap -> blocks spend');
  assert.ok(textOf(wfRow).includes('No cap'), 'null per-period cap -> No cap');
  const evilRow = trs[3];
  assert.ok(textOf(evilRow).includes('<img src=x onerror=alert(1)>'), 'malicious workflow name stays TEXT');
  assert.ok(findEl(doc.host, (n) => n.tag === 'img' || n.tag === 'script').length === 0, 'no injected elements');
  const editBtns = findEl(doc.host, (n) => n.tag === 'button' && textOf(n) === 'Edit');
  assert.ok(editBtns.length === 3, 'admin: Edit on default + both overrides');
  const addBtns = findEl(doc.host, (n) => n.tag === 'button' && textOf(n) === 'Add override');
  assert.ok(addBtns.length === 1, 'Add override button present');

  // 2) non-admin: default row read-only with the note, overrides still editable
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', false);
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: false, parent_user_id: 3 });
    if (u.includes('/budgets')) return ok(LIST);
    if (u.includes('/road')) return ok(WORKFLOWS);
    return err(404, { detail: 'Budget target not found' });
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  const note = findEl(doc.host, (n) => n.tag === 'span' && textOf(n).includes('Only tenant admins can change the default'));
  assert.ok(note.length === 1, 'non-admin: read-only note on the default row');
  const nonAdminEdits = findEl(doc.host, (n) => n.tag === 'button' && textOf(n) === 'Edit');
  assert.ok(nonAdminEdits.length === 2, 'non-admin: Edit only on the two overrides');

  // 3) blank -> nulls in the PUT body; out-of-range blocked client-side (no fetch)
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
  stubFetch((u) => {
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/budgets')) return ok(LIST);
    if (u.includes('/road')) return ok(WORKFLOWS);
    return err(404, { detail: 'Budget target not found' });
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  const before = fetchLog.length;
  prompts = ['', '', ''];
  const cleared = await mod._budgetsOpenForm(11, statusDiv());
  assert.ok(cleared !== false && cleared !== null, 'blank fields clear the caps');
  const puts = fetchLog.slice(before).filter((f) => String(f.opts.method) === 'PUT');
  assert.ok(puts.length === 1 && puts[0].url.includes('/budgets/workflows/11'), 'PUT /budgets/workflows/11');
  const body = JSON.parse(puts[0].opts.body);
  assert.ok(body.max_cost_per_run_credits === null && body.max_cost_per_period_credits === null && body.period_window === 'monthly',
    'blank -> real nulls, window defaults to monthly');
  const beforeBad = fetchLog.length;
  prompts = ['10000001', '', ''];
  const bad = await mod._budgetsOpenForm(11, statusDiv());
  assert.ok(bad === false && fetchLog.length === beforeBad, 'out-of-range cap: blocked client-side, NO fetch');
  const beforeBad2 = fetchLog.length;
  prompts = ['abc', '', ''];
  const bad2 = await mod._budgetsOpenForm(11, statusDiv());
  assert.ok(bad2 === false && fetchLog.length === beforeBad2, 'non-numeric cap: blocked client-side, NO fetch');

  // 4) server 4xx: detail shown as TEXT in the status line (injection stays text)
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
  stubFetch((u, opts) => {
    if (String(opts && opts.method) === 'PUT') return err(403, { detail: { code: 'workflow_edit_required', message: '<img onerror=alert(1)> edit needed' } });
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/budgets')) return ok(LIST);
    if (u.includes('/road')) return ok(WORKFLOWS);
    return err(404, { detail: 'Budget target not found' });
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  prompts = ['5', '50', 'quarterly'];
  const rejected = await mod._budgetsOpenForm(99, statusDiv());
  assert.ok(rejected === false, '403 surfaces as false');
  const statusLine = findEl(doc.host, (n) => String(n.className || '').includes('budgets-status'));
  assert.ok(statusLine.length === 1 && textOf(statusLine[0]).includes('<img onerror=alert(1)> edit needed'),
    'server detail message shown via textContent');
  assert.ok(findEl(doc.host, (n) => n.tag === 'img').length === 0, 'detail injection stays TEXT — no img element created');

  // 5) add-override picker + empty state + no DELETE ever
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
  stubFetch((u, opts) => {
    if (String(opts && opts.method) === 'PUT') return ok({ id: 40, workflow_id: 12, max_cost_per_run_credits: 7, max_cost_per_period_credits: null, period_window: 'quarterly', changed: true });
    if (u.includes('/me')) return ok({ is_platform_admin: true, parent_user_id: null });
    if (u.includes('/budgets')) return ok({ tenant_id: 3, default: null, overrides: [] });
    if (u.includes('/road')) return ok(WORKFLOWS);
    return err(404, { detail: 'Budget target not found' });
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  const empty = findEl(doc.host, (n) => n.tag === 'p' && textOf(n).includes('No caps set yet'));
  assert.ok(empty.length === 1, 'empty state renders');
  const beforeAdd = fetchLog.length;
  prompts = ['12', '7', '', 'quarterly'];
  const added = await mod._budgetsAddOverride(statusDiv());
  assert.ok(added !== null, 'add override succeeds');
  const addPuts = fetchLog.slice(beforeAdd).filter((f) => String(f.opts.method) === 'PUT');
  assert.ok(addPuts.length === 1 && addPuts[0].url.includes('/budgets/workflows/12'), 'picker PUTs the chosen workflow');
  const addBody = JSON.parse(addPuts[0].opts.body);
  assert.ok(addBody.max_cost_per_run_credits === 7 && addBody.max_cost_per_period_credits === null && addBody.period_window === 'quarterly',
    'picker body: number + null + chosen window');
  assert.ok(fetchLog.filter((f) => String(f.opts.method) === 'DELETE').length === 0, 'no DELETE request ever made');
  assert.ok(fetchLog.filter((f) => f.url.includes('/road?limit=100')).length === 1, 'picker reuses /road?limit=100');

  // 6) loading/error state: GET /budgets fails -> status line, no table crash
  resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
  stubFetch((u) => {
    if (u.includes('/budgets')) return err(500, {});
    return err(404, {});
  });
  await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
  const errLine = findEl(doc.host, (n) => String(n.className || '').includes('budgets-status'));
  assert.ok(errLine.length === 1 && textOf(errLine[0]).includes('Could not load budgets'), 'load failure shows a status line');
  assert.ok(findEl(doc.host, (n) => n.tag === 'table').length === 0, 'no table rendered on load failure');

  // 7) rollout-off (403) / not-permitted (404): GET /budgets gated by
  // require_rollout("cost_economics") -> quiet hidden state, no error
  // banner, no console spam. (sz-4.x)
  for (const status of [403, 404]) {
    resetEnv('https://mod.example.com/settings?tab=budgets#budgets', true);
    stubFetch(() => err(status, { detail: 'feature not enabled' }));
    const origErr = console.error;
    let errCalls = 0;
    console.error = () => { errCalls += 1; };
    try {
      await mod.initBudgetsPanel(doc.getElementById('budgets-panel'));
    } finally {
      console.error = origErr;
    }
    const container = doc.getElementById('budgets-panel');
    assert.ok(container.hidden === true, `status ${status}: container must be hidden, not a broken panel`);
    assert.ok(container.children.length === 0, `status ${status}: container stays empty — no error banner`);
    assert.ok(errCalls === 0, `status ${status}: no console spam`);
  }

  console.log('settings-budgets.check.js: all assertions passed');
})();
