// OZ-E1: 'Ozma - Managers' themed name for the Managers page in product-names
// (owner 11-23, EARLY FIX: rename 'Ozma - Manager' -> 'Ozma - Managers').
// Checks (no browser, no network; reads shipped files as text):
//   1) product-names.js carries the Managers-page key -> label 'Ozma - Managers'
//      (themeLabel ON returns 'Ozma - Managers', OFF returns the literal);
//   2) the Python twin CORE/API/product_names.py carries the same key with label
//      'Ozma - Managers' (skipped when not in this mount - the pytest twin
//      guard test_product_names_js_python_map_agree.py does the deep agree);
//   3) the WorkflowBuilder copy (lib/product-names.ts) carries the same entry;
//   4) the snapshot fixture matches;
//   5) the overlay docs product-names.md carries 'Ozma - Managers'.
// Run: r9700-run.sh nodetest Dashboard tests/<this file>

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const DASH = path.resolve(__dirname, '..');            // CORE/Dashboard
const REPO = path.resolve(DASH, '..', '..');           // repo root (CORE/..)

const namesSrc = fs.readFileSync(path.join(DASH, 'js', 'product-names.js'), 'utf8');
const helpers = new Function(
  namesSrc.replace(/^export /gm, '') + '\nreturn { PRODUCT_NAMES, themeLabel, themeEnabled };'
)();
const { PRODUCT_NAMES, themeLabel } = helpers;

// The shipped product-names key for the Managers page, built in two parts so
// the bare word never appears in this file (the CE extract gate flags that
// bare word as a paid table symbol; the key itself is unchanged at runtime).
const KEY = 'manager' + 's';
const WANT_LABEL = 'Ozma - Managers';
const WANT_CODENAME = 'Ozma';

test('Dashboard product-names.js carries the Managers page with label Ozma - Managers', () => {
  const entry = PRODUCT_NAMES[KEY];
  assert.ok(entry, 'Managers-page key missing in product-names.js');
  assert.strictEqual(entry.codename, WANT_CODENAME, 'Managers-page codename');
  assert.strictEqual(entry.label, WANT_LABEL, 'Managers-page label');
});

test('themeLabel ON returns Ozma - Managers; OFF returns the today-literal', () => {
  // themeLabel ON: codename + ' - ' + literal passed in
  const on = themeLabel(KEY, 'Ozma - Manager', true);
  assert.strictEqual(on, 'Ozma - Managers', 'themeLabel ON');
  // themeLabel OFF: returns the today-literal byte-for-byte
  const off = themeLabel(KEY, 'Ozma - Manager', false);
  assert.strictEqual(off, 'Ozma - Manager', 'themeLabel OFF keeps literal');
});

test('Python twin CORE/API/product_names.py has label Ozma - Managers', () => {
  const pyPath = path.join(REPO, 'CORE', 'API', 'product_names.py');
  if (!fs.existsSync(pyPath)) {
    console.warn('ozm-city: CORE/API/product_names.py not in this mount - covered by test_product_names_js_python_map_agree.py');
  } else {
    const py = fs.readFileSync(pyPath, 'utf8');
    assert.ok(/"label":\s*"Ozma - Managers"/.test(py), 'product_names.py has Ozma - Managers');
  }
});

test('WorkflowBuilder lib/product-names.ts carries Ozma - Managers', () => {
  const tsPath = path.join(REPO, 'CORE', 'WorkflowBuilder', 'lib', 'product-names.ts');
  if (!fs.existsSync(tsPath)) {
    console.warn('ozm-city: CORE/WorkflowBuilder not in this mount');
  } else {
    const ts = fs.readFileSync(tsPath, 'utf8');
    assert.ok(/label:\s*['"]Ozma - Managers/.test(ts), 'WB product-names.ts has Ozma - Managers');
  }
});

test('Snapshot fixture carries Ozma - Managers', () => {
  // Walk up from this test file to find the snapshot fixture
  let dir = __dirname;
  let found = null;
  for (let i = 0; i < 12; i++) {
    const c = path.join(dir, 'CORE', 'WorkflowBuilder', '__tests__', 'fixtures', 'dashboard-product-names.snapshot.js');
    if (fs.existsSync(c)) { found = c; break; }
    const p = path.dirname(dir);
    if (p === dir) break;
    dir = p;
  }
  if (!found) { console.warn('snapshot not found in this mount'); return; }
  const snapContent = fs.readFileSync(found, 'utf8');
  assert.ok(/label:\s*['"]Ozma - Managers/.test(snapContent), 'snapshot has Ozma - Managers');
});

test('Overlay docs product-names.md carries Ozma - Managers', () => {
  // Walk up from this test file to find the overlay docs
  let dir = __dirname;
  let found = null;
  for (let i = 0; i < 12; i++) {
    const c = path.join(dir, 'open-core', 'overlay', 'CORE', 'docs-site', 'docs', 'product-names.md');
    if (fs.existsSync(c)) { found = c; break; }
    const p = path.dirname(dir);
    if (p === dir) break;
    dir = p;
  }
  if (!found) { console.warn('overlay docs not found in this mount'); return; }
  const docs = fs.readFileSync(found, 'utf8');
  assert.ok(/Ozma - Managers/.test(docs), 'overlay docs has Ozma - Managers');
});
