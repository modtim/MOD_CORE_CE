// OZ-E5-QUADLINGS check (static, source-text asserts; no browser).
// The workers page h1 carries data-theme-key="workers" (product map key
// 'workers' = codename Quadlings). Its visible literal stays
// "Worker Management" (MOD_THEME_NAMES off = today's literal, byte for byte).
//
//   node CORE/Dashboard/tests/oz-e5-quadlings.check.js
//
// Checks:
//   1) js/product-names.js maps 'workers' to codename Quadlings.
//   2) pages/workers.html has exactly one h1 with data-theme-key="workers".
//   3) The h1 literal is unchanged; stripping the attribute restores the
//      pre-edit h1 line byte for byte.
//   4) id= and href= line counts equal the pre-edit baseline (ids-changed=0).
//   5) mod-worker-embedded count is unchanged (0 in the page before and after).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');

// 1) Product map entry for the workers key.
const mapSrc = read('js/product-names.js');
const entry = mapSrc.match(/'workers':\s*\{([^}]*)\}/);
assert.ok(entry, "product-names.js has no 'workers' key");
assert.ok(/codename:\s*'Quadlings'/.test(entry[1]), "'workers' codename is not Quadlings");

// 2) Exactly one themed h1 on the workers page.
const html = read('pages/workers.html');
const themed = html.split('\n').filter((l) => l.includes('<h1') && l.includes('data-theme-key="workers"'));
assert.strictEqual(themed.length, 1, 'expected one h1 with data-theme-key="workers", got ' + themed.length);
const h1Line = themed[0];
assert.ok(/^\s*<h1 class="[^"]*" data-theme-key="workers">/.test(h1Line), 'theme key must sit on the h1');

// 2b) The Workers section h2 span also carries the key; exactly once.
const h2Keyed = html.split('\n').filter((l) => /<h2\b[^>]*>\s*<span data-theme-key="workers">Workers<\/span>\s*<\/h2>/.test(l));
assert.strictEqual(h2Keyed.length, 1, 'expected one h2 span with data-theme-key="workers", got ' + h2Keyed.length);

// 3) Literal unchanged; stripping the attribute restores the original line.
const literal = h1Line.match(/>([^<]*)<\/h1>/);
assert.ok(literal && literal[1] === 'Worker Management', 'h1 literal changed');
const original = h1Line.replace(' data-theme-key="workers"', '');
assert.strictEqual(
  original.trim(),
  '<h1 class="text-3xl font-bold text-gray-800">Worker Management</h1>',
  'stripping the theme attribute must restore the pre-edit h1 line',
);

// 4) id= / href= line counts equal the current baseline (22 / 0 per grep -c).
//    21 -> 22:  commit 410b3d8d13 added id="workers-header-actions"
//    on the Workers header div (Add Worker button visible in CE); the pin follows
//    the real value, nothing else in the page changed.
const lines = html.split('\n');
const idCount = lines.filter((l) => l.includes('id=')).length;
const hrefCount = lines.filter((l) => l.includes('href=')).length;
assert.strictEqual(idCount, 22, 'id= line count changed: ' + idCount);
assert.strictEqual(hrefCount, 0, 'href= line count changed: ' + hrefCount);

// 5) Worker image name stays out of the page text (count unchanged at 0).
const embedded = lines.filter((l) => l.includes('mod-worker-embedded')).length;
assert.strictEqual(embedded, 0, 'mod-worker-embedded count changed: ' + embedded);

console.log('oz-e5-quadlings.check.js: 5 checks passed');
