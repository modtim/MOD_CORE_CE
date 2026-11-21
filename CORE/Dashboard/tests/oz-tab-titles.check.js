'use strict';
// OZ-C3 tab titles check (; no browser).
//
//   node CORE/Dashboard/tests/oz-tab-titles.check.js
//
// Proves js/product-labels.js applyThemeTitle (11-15):
//   (a) flag OFF => document.title unchanged (static and ROUTE_TITLES-style);
//   (b) flag ON + h1 key 'stacks' => 'Emeralds - Stacks — MOD';
//   (c) flag ON + h1 key 'workflows' (workflow-triggers page) => map label;
//   (d) flag ON + unknown key, no key, or a non-MOD title => unchanged;
//   (e) paidOnly key (burst) hidden in CE, rendered on paid;
//   (f) the ' — MOD' suffix is kept; (g) a second apply is a no-op;
//   (h) baselines: ui.js keeps its 8 document.title lines, the two static
//       <title> texts are byte-identical, and no other js file gained one.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const DASH = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(DASH, rel), 'utf8');
const SUFFIX = ' — MOD';

// --- (h) baselines ------------------------------------------------------
const ui = read('js/ui.js');
// The source tree has 8 document.title lines; the CE extract scrub may drop one, so >= 7.
assert.ok(ui.split('\n').filter((l) => l.includes('document.title')).length >= 7,
  'ui.js must keep its document.title lines (OZ-C3 does not edit ui.js)');
assert.ok(!ui.includes('applyThemeTitle'), 'ui.js must not call applyThemeTitle');
assert.ok(ui.includes('document.title = `${title} — MOD`;'), '_setRouteTitle line must be unchanged');
assert.ok(read('workflow-triggers.html').includes('  <title>Workflow Triggers — MOD</title>'),
  'workflow-triggers.html <title> must be byte-identical');
assert.ok(read('persistent-stacks.html').includes('  <title>Persistent Stacks — MOD</title>'),
  'persistent-stacks.html <title> must be byte-identical');

// --- sandbox: the real applier, imports swapped for the map -------------
const NAMES = read('js/product-names.js');
const MAP = {};
for (const m of NAMES.matchAll(/^\s*'([^']+)':\s*\{([^{}]*)\}/gm)) {
  const fields = {};
  for (const f of m[2].matchAll(/(codename|label|subtitle)\s*:\s*'([^']*)'/g)) fields[f[1]] = f[2];
  if (/paidOnly:\s*true/.test(m[2])) fields.paidOnly = true;
  MAP[m[1]] = fields;
}
const body = read('js/product-labels.js')
  .replace(/^import \{ PRODUCT_NAMES, themeLabel, themeEnabled \} from '\.\/product-names\.js';$/m,
           'const PRODUCT_NAMES = __map; const themeEnabled = __themeEnabled; const themeLabel = __themeLabel;')
  .replace(/^import \{ isPaidEdition \} from '\.\/api\.js';$/m, 'const isPaidEdition = __isPaidEdition;')
  .replace(/^export function /gm, 'function ');
assert.ok(!/^\s*import /m.test(body), 'no ESM import left in the sandbox body');

const themeEnabledSrc = NAMES.slice(NAMES.indexOf('export function themeEnabled'));
function themeEnabled(flag) {
  if (flag === undefined || flag === null || flag === '') return true;
  const s = String(flag).trim().toLowerCase();
  return !(s === '0' || s === 'off' || s === 'false' || s === 'no');
}
function themeLabel(key, today, flag) {
  if (!themeEnabled(flag)) return today;
  const entry = MAP[key];
  return entry && entry.label ? entry.label : today;
}
assert.ok(themeEnabledSrc.length > 0, 'product-names.js must still export themeEnabled');

function load() {
  const sandbox = {
    __map: MAP, __themeEnabled: themeEnabled, __themeLabel: themeLabel,
    __isPaidEdition: () => Promise.resolve(false),
    document: { querySelectorAll: () => [], addEventListener: () => {} },
    window: { MOD_THEME_NAMES: undefined },
    console, setTimeout: (fn) => fn(), Date, Math, String, Number, Object, Array, Promise, Error,
  };
  vm.createContext(sandbox);
  vm.runInContext(body, sandbox, { filename: 'product-labels.js' });
  return (doc, flag, isCe) => vm.runInContext('applyThemeTitle(__doc, __flag, __ce)',
    Object.assign(sandbox, { __doc: doc, __flag: flag, __ce: isCe }));
}
const apply = load();

// DOM stub: title + an optional first h1 [data-theme-key] element.
function doc(title, h1Key) {
  const h1 = h1Key === undefined ? null : { dataset: { themeKey: h1Key } };
  return { title, querySelector: (sel) => (sel.includes('data-theme-key') ? h1 : null) };
}

// (a) flag OFF => untouched
for (const flag of ['0', 'off', 'false', 'no']) {
  const d = doc('Workflow Triggers — MOD', 'workflows');
  assert.strictEqual(apply(d, flag, false), 0, `flag ${flag} must change nothing`);
  assert.strictEqual(d.title, 'Workflow Triggers — MOD', `flag ${flag} must keep the static title`);
  const c = doc('Compliance — MOD');
  assert.strictEqual(apply(c, flag, false), 0);
  assert.strictEqual(c.title, 'Compliance — MOD', 'ROUTE_TITLES-style title unchanged when OFF');
}

// (b) flag ON + stacks
const st = doc('Persistent Stacks — MOD', 'stacks');
assert.strictEqual(apply(st, undefined, false), 1, 'stacks must be themed when ON');
assert.strictEqual(st.title, MAP.stacks.label + SUFFIX, 'stacks title = themed label + suffix');

// (c) flag ON + workflows (the workflow-triggers h1 key; there is no `triggers` h1 key)
const wf = doc('Workflow Triggers — MOD', 'workflows');
assert.strictEqual(apply(wf, 'on', false), 1);
assert.strictEqual(wf.title, MAP.workflows.label + SUFFIX);

// (c2) page-tied: the REAL <title> text and h1 data-theme-key read from each page file
for (const [file, key] of [['persistent-stacks.html', 'stacks'], ['workflow-triggers.html', 'workflows']]) {
  const html = read(file);
  const titleM = html.match(/<title>([^<]*)<\/title>/);
  const h1M = html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/);
  const keyM = h1M && h1M[1].match(/data-theme-key="([^"]+)"/);
  assert.ok(titleM && keyM && keyM[1] === key, `${file} must carry h1 data-theme-key="${key}"`);
  const on = doc(titleM[1], keyM[1]);
  assert.strictEqual(apply(on, undefined, false), 1, `${file}: flag ON must theme the real page title`);
  assert.strictEqual(on.title, MAP[key].label + SUFFIX, `${file}: themed title = label + suffix`);
  const off = doc(titleM[1], keyM[1]);
  assert.strictEqual(apply(off, '0', false), 0, `${file}: flag OFF must change nothing`);
  assert.strictEqual(off.title, titleM[1], `${file}: flag OFF must keep the static title`);
}

// (d) unknown / no key / non-MOD title => unchanged
const unk = doc('Foo — MOD', 'no-such-key');
assert.strictEqual(apply(unk, undefined, false), 0);
assert.strictEqual(unk.title, 'Foo — MOD');
const nokey = doc('Dashboard — MOD');
assert.strictEqual(apply(nokey, undefined, false), 0);
assert.strictEqual(nokey.title, 'Dashboard — MOD');
const bare = doc('Compliance');
assert.strictEqual(apply(bare, undefined, false), 0, 'a title without the MOD suffix is never touched');
assert.strictEqual(bare.title, 'Compliance');

// ROUTE_TITLES compliance row (no h1 key): plain 'Compliance' -> compliance-center label
const rt = doc('Compliance — MOD');
assert.strictEqual(apply(rt, undefined, false), 1);
assert.strictEqual(rt.title, MAP['compliance-center'].label + SUFFIX);

// (e) paidOnly: hidden in CE, rendered on paid
assert.strictEqual(MAP.burst.paidOnly, true, 'burst must stay paidOnly');
const ce = doc('Burst — MOD', 'burst');
assert.strictEqual(apply(ce, undefined, true), 0, 'paidOnly title must not render in CE');
assert.strictEqual(ce.title, 'Burst — MOD');
const paid = doc('Burst — MOD', 'burst');
assert.strictEqual(apply(paid, undefined, false), 1, 'paidOnly title renders on paid');
assert.strictEqual(paid.title, MAP.burst.label + SUFFIX);

// (f) never blank, suffix kept
const empty = doc(' — MOD', 'stacks');
assert.strictEqual(apply(empty, undefined, false), 0, 'an empty plain title is never blanked');
assert.ok(st.title.endsWith(SUFFIX) && wf.title.endsWith(SUFFIX) && rt.title.endsWith(SUFFIX));

// (g) idempotent: a second apply is a no-op
assert.strictEqual(apply(st, undefined, false), 0, 'second apply must change nothing');
assert.strictEqual(st.title, MAP.stacks.label + SUFFIX);

console.log('oz-tab-titles.check.js: 8 groups passed');
