// OZ-C3-P5 check (static, source-text asserts; no browser).
// Two async-rendered dashboard widgets carry theme keys so the label hook
// (js/product-labels.js applyThemeLabels) can relabel them when
// MOD_THEME_NAMES is on:
//   js/usage-widget.js   'Running Workflows'  -> data-theme-key="workflows"
//   js/license-widget.js 'Processing Workers' -> data-theme-key="workers"
// Each widget also re-dispatches mod:page-rendered right after its
// success-path innerHTML assignment so the hook re-runs after the fetch.
// With MOD_THEME_NAMES off the hook is a no-op: visible text is unchanged.
//
//   node CORE/Dashboard/tests/oz-c3-p5-widgets.check.js
//
// Checks:
//   1) js/product-names.js has keys 'workflows' and 'workers' (not edited).
//   2) usage-widget.js: exactly one data-theme-key="workflows" span, in the
//      h3 line "Running <span ...>Workflows</span></h3>".
//   3) Stripping the span tags restores the pre-edit h3 line byte for byte.
//   4) license-widget.js: exactly one data-theme-key="workers" span, in the
//      p line "Processing <span ...>Workers</span></p>".
//   5) Stripping the span tags restores the pre-edit p line byte for byte.
//   6) Each file has exactly one mod:page-rendered dispatch, placed on the
//      line directly after its success-path innerHTML assignment.
//   7) innerHTML / id= / data-testid= counts equal the HEAD baseline in both
//      files; line counts are baseline + 1 (the dispatch line only).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const countLines = (text, needle) => text.split('\n').filter((l) => l.includes(needle)).length;
const DISPATCH = "document.dispatchEvent(new CustomEvent('mod:page-rendered'));";

// HEAD f8978532f7 baselines (computed before the edit)
const BASE = {
  usage: { innerHTML: 5, id: 0, testid: 0, lines: 208 },
  license: { innerHTML: 7, id: 13, testid: 4, lines: 626 }, // 625 -> 626: fa82e042f0 (modCopyText copy-helper swap) added one net line to license-widget.js (7 insertions / 6 deletions); baseline is HEAD-relative, the assert stays lines+1
};

const USAGE = 'js/usage-widget.js';
const LICENSE = 'js/license-widget.js';
const usageText = read(USAGE);
const licenseText = read(LICENSE);

// 1) keys exist in product-names.js
const names = read('js/product-names.js');
assert.ok(/'workflows':\s*\{/.test(names), "product-names.js must define 'workflows'");
assert.ok(/'workers':\s*\{/.test(names), "product-names.js must define 'workers'");

// 2) + 3) usage-widget.js keyed h3
const USAGE_KEYED = '<h3 class="text-sm font-semibold text-gray-700">Running <span data-theme-key="workflows">Workflows</span></h3>';
const USAGE_PRE = '<h3 class="text-sm font-semibold text-gray-700">Running Workflows</h3>';
assert.strictEqual(countLines(usageText, 'data-theme-key="workflows"'), 1, 'usage: one workflows key');
assert.strictEqual(countLines(usageText, USAGE_KEYED), 1, 'usage: keyed h3 line present once');
assert.strictEqual(
  countLines(usageText.replace(/<span data-theme-key="workflows">Workflows<\/span>/, 'Workflows'), USAGE_PRE),
  1,
  'usage: stripping the span restores the pre-edit h3 line',
);

// 4) + 5) license-widget.js keyed p
const LIC_KEYED = '<p class="text-xs text-gray-600 mb-1">Processing <span data-theme-key="workers">Workers</span></p>';
const LIC_PRE = '<p class="text-xs text-gray-600 mb-1">Processing Workers</p>';
assert.strictEqual(countLines(licenseText, 'data-theme-key="workers"'), 1, 'license: one workers key');
assert.strictEqual(countLines(licenseText, LIC_KEYED), 1, 'license: keyed p line present once');
assert.strictEqual(
  countLines(licenseText.replace(/<span data-theme-key="workers">Workers<\/span>/, 'Workers'), LIC_PRE),
  1,
  'license: stripping the span restores the pre-edit p line',
);

// 6) dispatch exactly once, directly after the success-path assignment
function checkDispatch(text, assignLine, label) {
  const lines = text.split('\n');
  const idx = lines.findIndex((l) => l.includes(assignLine));
  assert.ok(idx >= 0, `${label}: success-path assignment present`);
  assert.strictEqual(countLines(text, DISPATCH), 1, `${label}: exactly one dispatch`);
  assert.ok(lines[idx + 1].includes(DISPATCH), `${label}: dispatch directly after assignment`);
  assert.ok(lines[idx + 1].includes('OZ-C3-P5'), `${label}: dispatch carries OZ-C3-P5 comment`);
}
checkDispatch(usageText, 'container.innerHTML = renderUsageCards(data, creditsEnabled);', 'usage');
checkDispatch(licenseText, 'container.innerHTML = renderLicenseCard(licenseData);', 'license');

// 7) baseline counts unchanged; line count = baseline + 1 (dispatch only)
const cases = [
  ['usage', usageText, BASE.usage],
  ['license', licenseText, BASE.license],
];
for (const [label, text, base] of cases) {
  assert.strictEqual(countLines(text, 'innerHTML'), base.innerHTML, `${label}: innerHTML count`);
  assert.strictEqual(countLines(text, 'id='), base.id, `${label}: id= count`);
  assert.strictEqual(countLines(text, 'data-testid='), base.testid, `${label}: data-testid= count`);
  assert.strictEqual((text.match(/\n/g) || []).length, base.lines + 1, `${label}: line count = baseline + 1`);
}

console.log('oz-c3-p5-widgets.check.js: 7/7 groups passed');
