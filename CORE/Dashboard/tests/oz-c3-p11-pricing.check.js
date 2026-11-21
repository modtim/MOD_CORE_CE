// OZ-C3 PART 11 check (static, source-text asserts; no browser).
// Two KEYABLE spots in js/pricing.js (the `Concurrent Workflows` feature label
// rendered by renderPlanCard): only the word "Workflows" is wrapped in
// <span data-theme-key="workflows">. Visible text stays byte-identical with
// theming off. Also adds one mod:page-rendered dispatch after the async
// plan-card render so the label hook re-runs.
// id= / innerHTML / data-testid= counts equal the pre-edit baseline.
//
//   node CORE/Dashboard/tests/oz-c3-p11-pricing.check.js
//
// Checks:
//   1) js/product-names.js maps 'workflows' (read only, not edited).
//   2) pricing.js: exactly two spans keyed workflows, each wrapping only
//      "Workflows" inside the label "Concurrent ...".
//   3) Stripping the span tags restores the pre-edit label lines byte for byte.
//   4) The mod:page-rendered dispatch appears exactly once, after
//      attachPlanEventListeners(plans) in the success path.
//   5) innerHTML / id= / data-testid= counts equal the baseline.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const count = (text, needle) => text.split(needle).length - 1;
const linesWith = (text, needle) => text.split('\n').filter((l) => l.includes(needle)).length;

const PR = 'js/pricing.js';
const PN = 'js/product-names.js';

const src = read(PR);
const names = read(PN);

// Baselines captured before the edit (grep -c on HEAD).
const BASE_INNERHTML = 8;
const BASE_ID = 14;
const BASE_TESTID = 0;

// 1) product-names.js maps the workflows key
assert.ok(/'workflows':\s*\{/.test(names), "product-names.js must map 'workflows'");

// 2) exactly two keyed spots with the right surrounding text
const KEYED = '<span data-theme-key="workflows">Workflows</span>';
assert.strictEqual(count(src, 'data-theme-key="workflows"'), 2, 'expected exactly 2 workflows keys in pricing.js');
assert.strictEqual(count(src, KEYED), 2, 'expected the keyed span exactly twice');
const LABEL_CUSTOM = "{ label: 'Concurrent " + KEYED + "', value: 'Custom' },";
const LABEL_PLAN = "{ label: 'Concurrent " + KEYED + "', value: plan.max_concurrent_processes },";
assert.strictEqual(count(src, LABEL_CUSTOM), 1, 'enterprise label line must be keyed once');
assert.strictEqual(count(src, LABEL_PLAN), 1, 'plan label line must be keyed once');

// 3) stripping the span restores the pre-edit lines byte for byte
const stripped = src.split(KEYED).join('Workflows');
assert.strictEqual(count(stripped, "{ label: 'Concurrent Workflows', value: 'Custom' },"), 1,
  'stripped enterprise line must equal the pre-edit line');
assert.strictEqual(count(stripped, "{ label: 'Concurrent Workflows', value: plan.max_concurrent_processes },"), 1,
  'stripped plan line must equal the pre-edit line');
assert.strictEqual(count(stripped, 'data-theme-key'), 0, 'stripping must remove every theme key from pricing.js');

// 4) one page-rendered dispatch, after the success-path listener attach
const DISPATCH = "document.dispatchEvent(new CustomEvent('mod:page-rendered'));";
assert.strictEqual(count(src, DISPATCH), 1, 'mod:page-rendered dispatch must appear exactly once');
assert.strictEqual(count(src, 'OZ-C3-P11: re-run the label hook after async render'), 1,
  'dispatch needs the one-line OZ-C3-P11 comment');
const attachAt = src.indexOf('attachPlanEventListeners(plans);');
const dispatchAt = src.indexOf(DISPATCH);
assert.ok(attachAt > 0 && dispatchAt > attachAt, 'dispatch must follow attachPlanEventListeners(plans)');

// 5) DOM-write counts equal the baseline
assert.strictEqual(count(src, 'innerHTML'), BASE_INNERHTML, 'innerHTML count changed');
assert.strictEqual(linesWith(src, 'id='), BASE_ID, 'id= line count changed');
assert.strictEqual(count(src, 'data-testid='), BASE_TESTID, 'data-testid= count changed');

console.log('oz-c3-p11-pricing.check.js: all assertions passed');
