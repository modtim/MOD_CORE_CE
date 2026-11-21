'use strict';
// OZ-WIRING-RUNTIME-LABEL-HOOK check (; no browser).
//
//   node CORE/Dashboard/tests/oz-wiring-label-hook.check.js
//
// Proves the ONE runtime hook every other OZ leg keys off:
//   1) the 7 reference anchors (files, workflows, marketplace, compliance-center,
//      security, workers, capture-devices) carry data-theme-key=<key> in
//      dashboard-shell.html, and the OZ-A3 guard's own anchor regex still sees
//      30 anchors with today's literals unchanged (the attribute cannot break
//      open-core/tests/test_mod_theme_names_off_equals_today_literals.py);
//   2) the 6 page h1s that exist carry data-theme-key (workflows has NO page
//      file: ui.js redirects #workflows to the external WORKFLOWS_URL);
//   3) the applier (js/product-labels.js, run in a vm sandbox over a DOM stub):
//      ON  -> renders the map labels, unknown key untouched, paidOnly row
//             (burst) hidden in CE / rendered on paid, data-today-label wins;
//      OFF -> 0 changes, every text byte-identical to today's literal;
//      re-apply on the mod:page-rendered event -> idempotent (0 changes);
//   4) ui.js imports the hook module and dispatches mod:page-rendered right
//      after the #page-content innerHTML write.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const DASH = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(DASH, rel), 'utf8');

const KEYS = ['files', 'workflows', 'marketplace', 'compliance-center', 'security', 'workers', 'capture-devices'];
const LITERALS = { files: 'Data', workflows: 'Workflow Builder', marketplace: 'Marketplace', 'compliance-center': 'Compliance', security: 'Security', workers: 'Workers', 'capture-devices': 'Capture Devices' };

// --- 1) the 7 sidebar anchors -------------------------------------------
const shell = read('pages/dashboard-shell.html');
// ONE hook (11-14 unify): data-theme-key sits on the label span whose text is the name,
// never on the <a> (a textContent write on the <a> would wipe its svg icon).
for (const key of KEYS) {
  assert.ok(new RegExp(`<a href="#${key}"[\\s\\S]{0,1200}?<span class="ml-3" data-theme-key="${key}">`).test(shell),
    `sidebar label span inside #${key} must carry data-theme-key="${key}"`);
  assert.ok(!new RegExp(`<a href="#${key}"[^>]*data-theme-key=`).test(shell),
    `anchor #${key} must NOT carry data-theme-key (hook is on the label span)`);
}
// no second hook anywhere in the dashboard pages or js
{
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((d) =>
    d.isDirectory() ? walk(path.join(dir, d.name)) : [path.join(dir, d.name)]);
  const hits = [...walk(path.join(DASH, 'pages')), ...walk(path.join(DASH, 'js'))]
    .filter((f) => /\.(html|js)$/.test(f) && fs.readFileSync(f, 'utf8').includes('data-oz-key'));
  assert.deepStrictEqual(hits.map((f) => path.relative(DASH, f)), [], 'no data-oz-key under pages/ or js/');
}
const A3_ANCHOR_RE = /<a href="#([a-z-]+)"[^>]*class="[^"]*nav-link[^"]*"/g;
const ids = [...shell.matchAll(A3_ANCHOR_RE)].map((m) => m[1]);
assert.strictEqual(ids.length, 30, `A3 guard expects 30 nav anchors, got ${ids.length}`);
for (const key of KEYS) {
  const at = shell.match(new RegExp(`<a href="#${key}"[^>]*>`));
  assert.ok(at, `anchor #${key} tag must still parse`);
  // Tolerant of another lane's per-span attribute (e.g. data-oz-key): the
  // literal text must survive, whatever sits inside the tag.
  const span = shell.slice(at.index, at.index + 1200).match(/<span class="ml-3"[^>]*>([^<]*)<\/span>/);
  assert.ok(span, `anchor #${key} label span must survive the attribute`);
  assert.strictEqual(span[1], LITERALS[key], `anchor #${key} today's literal must be unchanged`);
}

// --- 2) the page h1s ----------------------------------------------------
const H1_PAGES = ['files', 'marketplace', 'security', 'workers', 'compliance-center', 'capture-devices'];
for (const page of H1_PAGES) {
  const html = read(`pages/${page}.html`);
  const themed = html.match(/<h1[^>]*data-theme-key="[^"]+"[^>]*>[^<]*<\/h1>/g) || [];
  assert.strictEqual(themed.length, 1, `${page}: expected exactly one themed h1`);
  assert.ok(themed[0].includes(`data-theme-key="${page}"`), `${page}: h1 must carry its own key`);
  const text = themed[0].replace(/^<h1[^>]*>/, '').replace(/<\/h1>$/, '');
  assert.ok(text.length > 0, `${page}: h1 text must stay (flag OFF renders it byte for byte)`);
}
assert.ok(!fs.existsSync(path.join(DASH, 'pages', 'workflows.html')),
  'workflows has no page file (external WORKFLOWS_URL redirect) - no h1 to tag');

// --- 3) the applier, over a DOM stub -----------------------------------
const hookSrc = read('js/product-labels.js');
const body = hookSrc
  .replace(/^import \{ PRODUCT_NAMES, themeLabel, themeEnabled \} from '\.\/product-names\.js';$/m,
           'const PRODUCT_NAMES = __map; const themeEnabled = __themeEnabled; const themeLabel = __themeLabel;')
  .replace(/^import \{ isPaidEdition \} from '\.\/api\.js';$/m,
           'const isPaidEdition = __isPaidEdition;')
  .replace(/^export function /gm, 'function ');
assert.ok(!/^\s*import /m.test(body), 'no ESM import left in the sandbox body');
assert.ok(/function applyThemeLabels/.test(body), 'applier must be callable');

const NAMES = read('js/product-names.js');
const MAP = {};
for (const m of NAMES.matchAll(/^\s*'([^']+)':\s*\{([^{}]*)\}/gm)) {
  const fields = {};
  for (const f of m[2].matchAll(/(codename|label|subtitle)\s*:\s*'([^']*)'/g)) fields[f[1]] = f[2];
  if (/paidOnly:\s*true/.test(m[2])) fields.paidOnly = true;
  MAP[m[1]] = fields;
}
assert.ok(Object.keys(MAP).length >= 28, 'product map must carry 28 rows');
for (const key of KEYS) assert.ok(MAP[key] && MAP[key].label, `map must carry a label for ${key}`);
assert.strictEqual(MAP.burst.paidOnly, true, 'burst (Rainbow MOD Cloud) must stay paidOnly');

const OFF_SET = new Set(['0', 'off', 'false', 'no']);
function themeEnabled(flag) {
  if (flag === undefined || flag === null || flag === '') return true;
  return !OFF_SET.has(String(flag).trim().toLowerCase());
}
function themeLabel(key, today, flag) {
  if (!themeEnabled(flag)) return today;
  const e = MAP[key];
  return e && e.label ? e.label : today;
}

function makeEl(key, text, today) {
  const el = { dataset: { themeKey: key }, textContent: text };
  if (today !== undefined) el.dataset.todayLabel = today;
  return el;
}
function freshDoc() {
  const els = [
    makeEl('files', 'Data'),
    makeEl('workflows', 'Workflow Builder'),
    makeEl('marketplace', 'Marketplace'),
    makeEl('compliance-center', 'Compliance'),
    makeEl('security', 'Security'),
    makeEl('workers', 'Workers'),
    makeEl('capture-devices', 'Capture Devices'),
    makeEl('burst', 'Burst to MOD Cloud'),
    makeEl('stacks', 'Stacks', 'System sub-tab'),   // data-today-label feeds the map lookup; under OFF the shipped text stays
    makeEl('no-such-key', 'Untouched Widget'),      // unknown key
  ];
  const listeners = {};
  const doc = {
    querySelectorAll: () => els,
    addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
  };
  return { doc, els, listeners };
}

function run(flag, isCe) {
  const made = freshDoc();
  const sandbox = {
    __map: MAP, __themeEnabled: themeEnabled, __themeLabel: themeLabel,
    __isPaidEdition: () => Promise.resolve(isCe ? false : true),
    __ce: isCe,
    document: made.doc,
    window: { MOD_THEME_NAMES: flag },
    console, setTimeout: (fn) => fn(), Date, Math, String, Number, Object, Array, Promise, Error,
  };
  vm.createContext(sandbox);
  vm.runInContext(body, sandbox, { filename: 'product-labels.js' });
  const boot = (made.listeners['DOMContentLoaded'] || [])[0];
  const pageRendered = (made.listeners['mod:page-rendered'] || [])[0];
  assert.ok(boot, 'hook must register a DOMContentLoaded listener');
  assert.ok(pageRendered, 'hook must register a mod:page-rendered listener');
  const first = vm.runInContext('applyThemeLabels(document, window.MOD_THEME_NAMES, __ce)', sandbox);
  const texts = made.els.map((e) => e.textContent);
  return { first, texts, els: made.els, boot, pageRendered };
}

const WANT = { files: 'The Cellar - Files', workflows: 'The Road - Orchestration', marketplace: 'Kansas - Marketplace', 'compliance-center': 'Lion - Compliance', security: 'The Guard - Security', workers: 'Quadlings - Workers', 'capture-devices': 'Field - Capture' };

const on = run('on', true);
assert.strictEqual(on.first, 8, 'ON/CE must render 7 anchors + the stacks sub-tab');
for (let i = 0; i < 7; i++) {
  assert.strictEqual(on.texts[i], WANT[KEYS[i]], `ON must render the map label for ${KEYS[i]}`);
}
assert.strictEqual(on.texts[7], 'Burst to MOD Cloud', 'paidOnly burst must NOT render in CE');
assert.strictEqual(on.texts[8], 'Emeralds - Stacks', 'data-today-label feeds the map label');
assert.strictEqual(on.texts[9], 'Untouched Widget', 'unknown key stays untouched');
on.pageRendered(); // the mod:page-rendered re-apply
const afterRe = on.els.map((e) => e.textContent);
assert.deepStrictEqual(afterRe, on.texts, 're-apply on mod:page-rendered must be idempotent');

const onPaid = run('on', false);
assert.strictEqual(onPaid.texts[7], 'Rainbow MOD Cloud - Burst', 'paidOnly burst renders on paid');

const off = run('off', true);
assert.strictEqual(off.first, 0, 'OFF must change nothing');
// OFF is a pure no-op: every element keeps the text it shipped, byte for byte
// (the stacks stub shipped 'Stacks' with data-today-label='System sub-tab' -
// the hook never rewrites under OFF, so its own text stays).
const literals = ['Data', 'Workflow Builder', 'Marketplace', 'Compliance', 'Security', 'Workers', 'Capture Devices', 'Burst to MOD Cloud', 'Stacks', 'Untouched Widget'];
assert.deepStrictEqual(off.texts, literals, 'OFF must keep every shipped text byte for byte');
for (let i = 0; i < 7; i++) assert.strictEqual(off.texts[i], LITERALS[KEYS[i]], `OFF anchor ${KEYS[i]} byte-identical`);

// --- 4) ui.js wiring ----------------------------------------------------
const ui = read('js/ui.js');
assert.ok(/import \{ applyThemeLabels \} from '\.\/product-labels\.js';/.test(ui),
  'ui.js must import the hook module');
assert.ok(/document\.dispatchEvent\(new CustomEvent\('mod:page-rendered'\)\)/.test(ui),
  'ui.js must dispatch mod:page-rendered after the page mounts');
const inj = ui.indexOf("document.getElementById('page-content').innerHTML = pageContent;");
assert.ok(inj > 0 && ui.indexOf('mod:page-rendered', inj) > inj && ui.indexOf('mod:page-rendered', inj) < ui.indexOf('_loadRunbookLinks();', inj),
  'the event must fire right after the innerHTML write');

// --- 4) unit: ON writes the label span only; the anchor's svg icon survives
{
  const svg = { tagName: 'svg', textContent: '' };
  const span = makeEl('files', 'Data');
  const anchor = { tagName: 'A', children: [svg, span], textContent: 'Data' };
  const doc = {
    querySelectorAll: (sel) => (sel === '[data-theme-key]' ? [span] : []),
    addEventListener: () => {},
  };
  const sandbox = {
    __map: MAP, __themeEnabled: themeEnabled, __themeLabel: themeLabel,
    __isPaidEdition: () => Promise.resolve(false), __ce: true,
    document: doc, window: { MOD_THEME_NAMES: 'on' },
    console, setTimeout: (fn) => fn(), Date, Math, String, Number, Object, Array, Promise, Error,
  };
  vm.createContext(sandbox);
  vm.runInContext(body, sandbox, { filename: 'product-labels.js' });
  const changed = vm.runInContext('applyThemeLabels(document, window.MOD_THEME_NAMES, __ce)', sandbox);
  assert.strictEqual(changed, 1, 'one label span changed under ON');
  assert.strictEqual(span.textContent, 'The Cellar - Files', 'label span carries the ON name');
  assert.strictEqual(anchor.children[0], svg, 'svg icon stays in the anchor');
  assert.strictEqual(svg.textContent, '', 'svg untouched by the applier');
  assert.strictEqual(anchor.children.length, 2, 'anchor keeps its icon and its label');
}

process.stdout.write('oz-wiring-label-hook: anchors=7 h1s=6 on=8/8 paidOnly=ce-skip off_diff=0 reapply=idempotent\n');
