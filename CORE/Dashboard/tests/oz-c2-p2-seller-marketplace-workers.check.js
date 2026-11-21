// OZ-C2 PART 2 check (static, source-text asserts; no browser).
// Keys five whole-word visible labels with <span data-theme-key="KEY">Word</span>:
//   seller.html      button data-kind="stacks"    -> stacks    ("Stacks")
//   seller.html      button data-kind="workflows" -> workflows ("Workflows")
//   marketplace.html button #seller-kind-tab-stacks    -> stacks
//   marketplace.html button #seller-kind-tab-workflows -> workflows
//   workers.html     h2 "Workers"                 -> workers   ("Workers")
// The span sits inside the button/h2 so applyThemeLabels only rewrites the word.
//
//   node CORE/Dashboard/tests/oz-c2-p2-seller-marketplace-workers.check.js
//
// Checks:
//   1) js/product-names.js has the three keys stacks, workflows, workers.
//   2) seller.html: 2 data-theme-key attrs; each kind-tab button holds its exact span.
//   3) marketplace.html: 3 data-theme-key attrs (1 pre-existing + 2 new); each tab holds its span.
//   4) workers.html: 2 data-theme-key="workers" (h1 OZ-D6 + this h2), h2 span exact.
//   5) id= / href= / data-kind= / data-testid= line counts equal the HEAD baseline.
//   6) Tab button id and data-testid values are unchanged.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const countLines = (lines, needle) => lines.filter((l) => l.includes(needle)).length;
const countAll = (src, needle) => src.split(needle).length - 1;

// 1) Product map keys exist.
const mapSrc = read('js/product-names.js');
for (const key of ['stacks', 'workflows', 'workers']) {
  assert.ok(new RegExp("'" + key + "':\\s*\\{").test(mapSrc), "product-names.js has no '" + key + "' key");
}

// 2) seller.html: the two kind-tab buttons each hold their keyed span.
const seller = read('pages/seller.html');
// OZ-E4-P3a (abc4e378ea) + OZ-E4-BUILDER key the stacks product words on these
// pages with the same span idiom; the counts below are the current totals
// (seller 2 -> 6: +4 stack words in the loading/empty-state prose;
//  marketplace 3 -> 9: +6 'Package a stack' x2, 'Persistent stack', loading, empty-state).
assert.strictEqual(countAll(seller, 'data-theme-key='), 6, 'seller.html data-theme-key count');
const sellerStacks = seller.match(/<button data-kind="stacks"[\s\S]*?<\/button>/);
assert.ok(sellerStacks, 'seller.html: button data-kind="stacks" not found');
assert.ok(sellerStacks[0].includes('<span data-theme-key="stacks">Stacks</span>'), 'seller stacks tab span missing or text changed');
const sellerWf = seller.match(/<button data-kind="workflows"[\s\S]*?<\/button>/);
assert.ok(sellerWf, 'seller.html: button data-kind="workflows" not found');
assert.ok(sellerWf[0].includes('<span data-theme-key="workflows">Workflows</span>'), 'seller workflows tab span missing or text changed');

// 3) marketplace.html: id-addressed tab buttons each hold their keyed span.
const mkt = read('pages/marketplace.html');
assert.strictEqual(countAll(mkt, 'data-theme-key='), 9, 'marketplace.html data-theme-key count');
const mktStacks = mkt.match(/<button id="seller-kind-tab-stacks"[\s\S]*?<\/button>/);
assert.ok(mktStacks, 'marketplace.html: #seller-kind-tab-stacks not found');
assert.ok(mktStacks[0].includes('<span data-theme-key="stacks">Stacks</span>'), 'marketplace stacks tab span missing or text changed');
const mktWf = mkt.match(/<button id="seller-kind-tab-workflows"[\s\S]*?<\/button>/);
assert.ok(mktWf, 'marketplace.html: #seller-kind-tab-workflows not found');
assert.ok(mktWf[0].includes('<span data-theme-key="workflows">Workflows</span>'), 'marketplace workflows tab span missing or text changed');

// 4) workers.html: h1 key (OZ-D6) plus exactly one new h2 key; h2 span exact.
const workers = read('pages/workers.html');
assert.strictEqual(countAll(workers, 'data-theme-key="workers"'), 2, 'workers.html data-theme-key="workers" count must be 2');
const h2Line = workers.split('\n').filter((l) => l.includes('>Workers</span></h2>'));
assert.strictEqual(h2Line.length, 1, 'expected exactly one h2 holding the Workers span');
assert.ok(
  h2Line[0].includes('<h2 class="text-xl font-semibold text-gray-800"><span data-theme-key="workers">Workers</span></h2>'),
  'workers h2 span missing or text changed',
);

// 5) Baseline line counts at HEAD (grep -c per file): id=, href=, data-kind=, data-testid=.
const baseline = {
  'pages/seller.html': { 'id=': 40, 'href=': 0, 'data-kind=': 2, 'data-testid=': 31 },
  'pages/marketplace.html': { 'id=': 60, 'href=': 0, 'data-kind=': 2, 'data-testid=': 47 },
  'pages/workers.html': { 'id=': 22, 'href=': 0, 'data-kind=': 0, 'data-testid=': 0 },  // 21 -> 22: id="workers-header-actions" added by  (410b3d8d13)
};
const sources = { 'pages/seller.html': seller, 'pages/marketplace.html': mkt, 'pages/workers.html': workers };
for (const rel of Object.keys(baseline)) {
  const lines = sources[rel].split('\n');
  for (const needle of Object.keys(baseline[rel])) {
    assert.strictEqual(countLines(lines, needle), baseline[rel][needle], rel + ' ' + needle + ' line count changed');
  }
}

// 6) Tab button ids and testids unchanged.
assert.ok(mkt.includes('id="seller-kind-tab-stacks" data-kind="stacks" data-testid="seller-kind-tab-stacks"'), 'marketplace stacks tab id/testid changed');
assert.ok(mkt.includes('id="seller-kind-tab-workflows" data-kind="workflows" data-testid="seller-kind-tab-workflows"'), 'marketplace workflows tab id/testid changed');
assert.ok(seller.includes('<button data-kind="stacks" data-testid="seller-kind-tab-stacks"'), 'seller stacks tab testid changed');
assert.ok(seller.includes('<button data-kind="workflows" data-testid="seller-kind-tab-workflows"'), 'seller workflows tab testid changed');

console.log('oz-c2-p2-seller-marketplace-workers.check.js: 6 checks passed');
