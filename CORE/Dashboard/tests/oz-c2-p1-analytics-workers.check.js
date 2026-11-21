// OZ-C2-P1 check (static, source-text asserts; no browser).
// The analytics page Workers tab label (button id="main-tab-workers") carries
// a <span data-theme-key="workers">Workers</span> so that, with names on, the
// label reads Quadlings - Workers (product map key 'workers' = codename
// Quadlings). The button, its id, classes and svg stay byte-identical.
//
//   node CORE/Dashboard/tests/oz-c2-p1-analytics-workers.check.js
//
// Checks:
//   a) js/product-names.js maps 'workers' to codename Quadlings.
//   b) pages/analytics.html has 1..3 elements with data-theme-key="workers",
//      each a <span> whose literal text is exactly "Workers".
//   c) The first one sits inside button id="main-tab-workers" (before its
//      closing </button>) and the svg is still inside that button.
//   d) id=, href= and main-panel-workers line counts equal the pre-edit
//      baseline on HEAD (278 / 5 / 1 per grep -c).
//   e) data-theme-key="stacks" count is 0 or 1 (OZ-E4 owns that span).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');

// a) Product map entry for the workers key.
const mapSrc = read('js/product-names.js');
const entry = mapSrc.match(/'workers':\s*\{([^}]*)\}/);
assert.ok(entry, "product-names.js has no 'workers' key");
assert.ok(/codename:\s*'Quadlings'/.test(entry[1]), "'workers' codename is not Quadlings");

// b) One to three themed spans, each with the exact literal Workers.
const html = read('pages/analytics.html');
const themedRe = /<span[^>]*data-theme-key="workers"[^>]*>([^<]*)<\/span>/g;
const themed = [...html.matchAll(themedRe)];
const allThemed = html.split('\n').filter((l) => l.includes('data-theme-key="workers"'));
assert.ok(themed.length >= 1 && themed.length <= 3,
  'expected 1..3 spans with data-theme-key="workers", got ' + themed.length);
assert.strictEqual(allThemed.length, themed.length,
  'every data-theme-key="workers" element must be a <span> with literal text');
for (const m of themed) {
  assert.strictEqual(m[1], 'Workers', 'themed span literal must be exactly Workers, got ' + m[1]);
}

// c) First span sits inside the Workers tab button, svg still inside it.
const btnStart = html.indexOf('id="main-tab-workers"');
assert.ok(btnStart > 0, 'button id="main-tab-workers" missing');
const btnEnd = html.indexOf('</button>', btnStart);
assert.ok(btnEnd > btnStart, 'no closing </button> after main-tab-workers');
const btnBody = html.slice(btnStart, btnEnd);
const firstSpanAt = html.indexOf(themed[0][0], btnStart);
assert.ok(firstSpanAt > btnStart && firstSpanAt < btnEnd,
  'workers span must sit inside button id="main-tab-workers"');
assert.ok(/<svg[\s\S]*<\/svg>/.test(btnBody), 'svg must remain inside the Workers tab button');
assert.ok(btnBody.indexOf('<svg') < btnBody.indexOf('data-theme-key="workers"'),
  'svg must precede the themed span inside the button');

// d) id= / href= / main-panel-workers counts equal the HEAD baseline.
const lines = html.split('\n');
const idCount = lines.filter((l) => l.includes('id=')).length;
const hrefCount = lines.filter((l) => l.includes('href=')).length;
const panelCount = lines.filter((l) => l.includes('main-panel-workers')).length;
assert.strictEqual(idCount, 278, 'id= line count changed: ' + idCount);
assert.strictEqual(hrefCount, 5, 'href= line count changed: ' + hrefCount);
assert.strictEqual(panelCount, 1, 'main-panel-workers line count changed: ' + panelCount);

// e) Stacks span (OZ-E4) is not disturbed: 0 or 1 occurrences.
const stacksCount = lines.filter((l) => l.includes('data-theme-key="stacks"')).length;
assert.ok(stacksCount === 0 || stacksCount === 1, 'data-theme-key="stacks" count: ' + stacksCount);

console.log('oz-c2-p1-analytics-workers.check.js: 5 checks passed');
