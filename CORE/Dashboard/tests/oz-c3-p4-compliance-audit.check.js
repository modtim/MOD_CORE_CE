// OZ-C3 PART 4 check (static, source-text asserts; no browser).
// Two KEYABLE spots from the OZ-C3 P2 disposition table:
//   a) pages/compliance-center.html tab button "Compliance profiles":
//      the button holds an svg, so only the word "Compliance" is wrapped in
//      <span data-theme-key="compliance-center">. The button itself is NOT keyed.
//   b) pages/audit-logs.html <option value="workflow">Workflows</option>:
//      option has no child elements, so the option itself is keyed with
//      data-theme-key="workflows" (product map key 'workflows').
// Visible literals stay byte-identical with theming off.
// CNSA wording is untouched (per-file CNSA count equals baseline).
// id= / href= / data-testid= counts equal the pre-edit baseline.
//
//   node CORE/Dashboard/tests/oz-c3-p4-compliance-audit.check.js
//
// Checks:
//   1) js/product-names.js maps 'compliance-center' and 'workflows'.
//   2) compliance-center.html: exactly one span keyed compliance-center,
//      wrapping only "Compliance"; the tab button is not keyed.
//   3) Stripping the span restores the pre-edit tab line byte for byte.
//   4) audit-logs.html: exactly one option keyed workflows, text "Workflows".
//   5) Stripping the attribute restores the pre-edit option line byte for byte.
//   6) id= / href= / data-testid= counts equal the baseline in both files.
//   7) CNSA count equals the baseline (0) in both files.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const count = (text, needle) => text.split(needle).length - 1;

const CC = 'pages/compliance-center.html';
const AL = 'pages/audit-logs.html';

const PRE_TAB = '                    Compliance profiles';
const POST_TAB = '                    <span data-theme-key="compliance-center">Compliance</span> profiles';
const PRE_OPT = '<option value="workflow">Workflows</option>';
const POST_OPT = '<option value="workflow" data-theme-key="workflows">Workflows</option>';

const BASE = {
  [CC]: { id: 40, href: 0, testid: 0, cnsa: 0 },
  [AL]: { id: 23, href: 2, testid: 0, cnsa: 0 },
};

const names = read('js/product-names.js');
const cc = read(CC);
const al = read(AL);

function checkCounts(rel, text) {
  const b = BASE[rel];
  assert.strictEqual(count(text, 'id='), b.id, `${rel} id= count`);
  assert.strictEqual(count(text, 'href='), b.href, `${rel} href= count`);
  assert.strictEqual(count(text, 'data-testid='), b.testid, `${rel} data-testid= count`);
  assert.strictEqual(count(text, 'CNSA'), b.cnsa, `${rel} CNSA count`);
}

// 1) product keys exist (do not edit product-names.js)
assert.ok(/'compliance-center':\s*\{/.test(names), 'product-names.js has compliance-center');
assert.ok(/'workflows':\s*\{/.test(names), 'product-names.js has workflows');

// 2) compliance-center: exactly one keyed span, wrapping only "Compliance"
const KEY_CC = '<span data-theme-key="compliance-center">';
assert.strictEqual(count(cc, KEY_CC), 1, 'compliance-center.html one keyed span');
assert.strictEqual(count(cc, 'data-theme-key="compliance-center"'), 2,
  'compliance-center.html keyed compliance-center = h1 + tab span');
assert.ok(cc.includes(POST_TAB), 'tab line has span around Compliance only');
assert.ok(!/<button[^>]*data-theme-key=/.test(cc.slice(cc.indexOf('main-tab-compliance') - 40, cc.indexOf('main-tab-compliance') + 400)),
  'tab button itself is not keyed');

// 3) stripping the span restores the pre-edit tab line byte for byte
assert.strictEqual(cc.replace(POST_TAB, PRE_TAB).includes(PRE_TAB), true, 'pre-edit tab line restorable');

// 4) audit-logs: exactly one keyed option, text unchanged
assert.strictEqual(count(al, POST_OPT), 1, 'audit-logs.html one keyed workflows option');
assert.strictEqual(count(al, 'data-theme-key="workflows"'), 1, 'audit-logs.html keyed workflows count');
assert.ok(al.includes('>Workflows</option>'), 'option visible text unchanged');

// 5) stripping the attribute restores the pre-edit option line byte for byte
assert.strictEqual(al.replace(POST_OPT, PRE_OPT).includes(PRE_OPT), true, 'pre-edit option line restorable');

// 6) and 7) counts equal baseline
checkCounts(CC, cc);
checkCounts(AL, al);

console.log('oz-c3-p4-compliance-audit.check.js: 7 checks passed');
