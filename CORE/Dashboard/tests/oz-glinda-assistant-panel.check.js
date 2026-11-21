// FIX-THEME-GLINDA (owner 11-17, D75): 'MOD Assistant' as a product NAME renders
// 'Glinda - Mod Assistant' under MOD_THEME_NAMES ON; flag OFF keeps the literal
// byte for byte. Checks (no browser, no network; reads shipped files as text):
//   1) the Dashboard map carries key 'assistant-panel' -> codename 'Glinda',
//      label exactly 'Glinda - Mod Assistant' (themeLabel ON/OFF both sides);
//   2) the Python twin CORE/API/product_names.py carries the same key with the
//      same three fields (skipped when not in this mount - the pytest twin
//      guard test_product_names_js_python_map_agree.py does the deep agree);
//   3) the WorkflowBuilder copy (lib/product-names.ts) carries the same entry
//      AND the wb-nav family carries 'wb-nav-assistant' -> the exact label;
//   4) the Dashboard panel title span sets data-theme-key="assistant-panel" and
//      keeps the literal 'MOD Assistant' as the flag-OFF fallback; the panel
//      re-dispatches mod:page-rendered after mounting (the hook is document-wide
//      and idempotent); the Help CTA visible text carries the same hook span;
//   5) the WB panel header reads wbNavLabel('wb-nav-assistant', 'MOD Assistant').
// Run: r9700-run.sh nodetest Dashboard tests/oz-glinda-assistant-panel.check.js

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

const WANT_KEY = 'assistant-panel';
const WANT_LABEL = 'Glinda - Mod Assistant';
const WANT_CODENAME = 'Glinda';
const LITERAL = 'MOD Assistant';

test('Dashboard map carries assistant-panel with the exact owner-named label', () => {
  const entry = PRODUCT_NAMES[WANT_KEY];
  assert.ok(entry, 'assistant-panel key missing in product-names.js');
  assert.strictEqual(entry.codename, WANT_CODENAME, 'assistant-panel codename');
  assert.strictEqual(entry.label, WANT_LABEL, 'assistant-panel label');
  assert.ok(entry.subtitle && entry.subtitle.length > 3, 'assistant-panel subtitle non-blank');
  assert.strictEqual(themeLabel(WANT_KEY, LITERAL, 'off'), LITERAL, 'OFF keeps literal byte for byte');
  assert.strictEqual(themeLabel(WANT_KEY, LITERAL, 'on'), WANT_LABEL, 'ON renders the owner name');
});

test('Python twin carries the same key with the exact text (when in this mount)', () => {
  const pyPath = path.join(REPO, 'CORE', 'API', 'product_names.py');
  if (!fs.existsSync(pyPath)) {
    console.warn('oz-glinda: CORE/API/product_names.py not in this mount - twin covered by the pytest guard');
    return;
  }
  const py = fs.readFileSync(pyPath, 'utf8');
  const block = new RegExp(
    '"assistant-panel":\\s*\\{[^}]*"codename":\\s*"Glinda"[^}]*"label":\\s*"Glinda - Mod Assistant"' +
    '[^}]*"subtitle":\\s*"[^"]+"'
  );
  assert.ok(block.test(py), 'product_names.py missing assistant-panel row with exact label');
});

test('WB copy carries the same map entry and the wb-nav-assistant label (when in this mount)', () => {
  const tsPath = path.join(REPO, 'CORE', 'WorkflowBuilder', 'lib', 'product-names.ts');
  if (!fs.existsSync(tsPath)) {
    console.warn('oz-glinda: CORE/WorkflowBuilder not in this mount - covered by the WB jest drift test');
    return;
  }
  const ts = fs.readFileSync(tsPath, 'utf8');
  assert.ok(
    /'assistant-panel':\s*\{codename:\s*'Glinda',\s*label:\s*'Glinda - Mod Assistant',\s*subtitle:\s*'[^']+'/.test(ts),
    'WB product-names.ts missing assistant-panel entry'
  );
  assert.ok(/'wb-nav-assistant':\s*'Glinda - Mod Assistant'/.test(ts), 'WB wb-nav-assistant label missing');
});

test('surfaces carry the data-theme-key hook with the literal fallback intact', () => {
  const panel = fs.readFileSync(path.join(DASH, 'js', 'assistant-panel.js'), 'utf8');
  assert.ok(
    /setAttribute\("data-theme-key", "assistant-panel"\)/.test(panel),
    'panel title span must set data-theme-key="assistant-panel"'
  );
  assert.ok(/_el\("span", "mod-asst-title", "MOD Assistant"\)/.test(panel),
    'panel title literal fallback must stay "MOD Assistant"');
  assert.ok(/dispatchEvent\(new CustomEvent\('mod:page-rendered'\)\)/.test(panel),
    'panel must re-dispatch mod:page-rendered after mounting so the hook sees the title');
  const help = fs.readFileSync(path.join(DASH, 'pages', 'help.html'), 'utf8');
  assert.ok(
    /<span data-theme-key="assistant-panel">Open MOD Assistant<\/span>/.test(help),
    'help.html CTA visible text must carry the assistant-panel hook span'
  );
});

test('WB panel header reads the wb-nav-assistant label (when in this mount)', () => {
  const wbPath = path.join(REPO, 'CORE', 'WorkflowBuilder', 'components', 'assistant-panel.tsx');
  if (!fs.existsSync(wbPath)) {
    console.warn('oz-glinda: CORE/WorkflowBuilder not in this mount - covered by the WB jest suite');
    return;
  }
  const wb = fs.readFileSync(wbPath, 'utf8');
  assert.ok(/import \{ wbNavLabel \} from "@\/lib\/product-names"/.test(wb), 'wbNavLabel import missing');
  assert.ok(/wbNavLabel\('wb-nav-assistant', 'MOD Assistant'\)/.test(wb), 'WB header must read wbNavLabel');
});
