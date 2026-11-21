// OZ-C3-P3 check (static, source-text asserts; no browser).
// Two KEYABLE h1 spots from the OZ-C3-P2 disposition table:
//   pages/workflow-triggers.html  <h1>  word "Workflow"  -> key 'workflows'
//   pages/persistent-stacks.html  <h1>  word "Stacks"    -> key 'stacks'
// Each key sits on a <span> wrapping ONLY the word; the rest of the
// visible text is unchanged. The two <title> spots (lines 11 and 14) are
// NOT keyed: <title> is RCDATA, so a span inside it would show as literal
// markup in the tab. They stay as-is and are reported in the result file.
//
//   node CORE/Dashboard/tests/oz-c3-p3-triggers-stacks.check.js
//
// Checks:
//   1) js/product-names.js has keys 'workflows' and 'stacks'.
//   2) workflow-triggers.html has exactly one data-theme-key="workflows",
//      a <span> whose literal is Workflow, inside the h1 with " Triggers".
//   3) persistent-stacks.html has exactly one data-theme-key="stacks",
//      a <span> whose literal is Stacks, inside the h1 after "Persistent ".
//   4) id= / href= / data-testid= line counts equal the pre-edit HEAD
//      baseline (workflow-triggers 9, persistent-stacks 17 per grep -c).
//   5) Visible h1 text is unchanged once tags are stripped.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');

// 1) Product keys exist in the map (map itself is not edited here).
const mapSrc = read('js/product-names.js');
assert.ok(/^\s*'workflows':\s*\{/m.test(mapSrc), "product-names.js has no 'workflows' key");
assert.ok(/^\s*'stacks':\s*\{/m.test(mapSrc), "product-names.js has no 'stacks' key");

const countOf = (lines, key) => lines.filter((l) => l.includes(key)).length;
const stripTags = (s) => s.replace(/<[^>]*>/g, '');
// Same semantics as grep -c 'id=\|href=\|data-testid=' (data-testid= contains id=).
const attrLines = (lines) => lines.filter((l) => /id=|href=/.test(l)).length;

// 2) workflow-triggers.html
const wt = read('workflow-triggers.html');
const wtLines = wt.split('\n');
const wtThemed = wtLines.filter((l) => l.includes('data-theme-key="workflows"'));
assert.strictEqual(wtThemed.length, 1, 'workflow-triggers: expected 1 workflows key, got ' + wtThemed.length);
assert.ok(
  wtThemed[0].includes('<h1><span data-theme-key="workflows">Workflow</span> Triggers</h1>'),
  'workflows key must be a <span> around the word Workflow inside the h1',
);
assert.strictEqual(
  stripTags(wtThemed[0].trim()),
  'Workflow Triggers',
  'workflow-triggers h1 visible text changed',
);

// 3) persistent-stacks.html
const ps = read('persistent-stacks.html');
const psLines = ps.split('\n');
const psThemed = psLines.filter((l) => l.includes('data-theme-key="stacks"'));
assert.strictEqual(psThemed.length, 1, 'persistent-stacks: expected 1 stacks key, got ' + psThemed.length);
assert.ok(
  psThemed[0].includes('<header><h1>Persistent <span data-theme-key="stacks">Stacks</span></h1></header>'),
  'stacks key must be a <span> around the word Stacks inside the h1',
);
assert.strictEqual(
  stripTags(psThemed[0].trim()).replace(/\s+/g, ' ').trim(),
  'Persistent Stacks',
  'persistent-stacks h1 visible text changed',
);

// 4) Attribute counts equal the HEAD baseline (workflow 9, persistent 17).
assert.strictEqual(attrLines(wtLines), 9, 'workflow-triggers id/href/testid count changed from baseline 9');
assert.strictEqual(attrLines(psLines), 17, 'persistent-stacks id/href/testid count changed from baseline 17');

// 5) Title spots stay unkeyed (RCDATA; see header comment).
assert.ok(!/<title>[^<]*<span/.test(wt), 'workflow-triggers title must not hold a span');
assert.ok(!/<title>[^<]*<span/.test(ps), 'persistent-stacks title must not hold a span');

console.log('oz-c3-p3-triggers-stacks: 2 spots keyed, all asserts passed');
