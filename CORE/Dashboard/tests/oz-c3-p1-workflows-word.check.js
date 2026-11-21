// OZ-C3 PART 1 check (static, source-text asserts; no browser).
// The Workflows word on subscription.html, billing.html and permissions.html
// carries data-theme-key="workflows" (product map key 'workflows' = codename
// The Road). The visible literal stays "Workflows" (names off = today).
//
//   node CORE/Dashboard/tests/oz-c3-p1-workflows-word.check.js
//
// Checks:
//   1) js/product-names.js maps 'workflows' to codename 'The Road'.
//   2) subscription.html has exactly 1 data-theme-key="workflows" span, the
//      span holds the literal Workflows and sits after "Running ".
//   3) billing.html has exactly 1 such span, same shape.
//   4) permissions.html has exactly 2 keyed spots: a span inside the
//      perm-tab button (svg kept) and an h3 with the key.
//   5) permissions.html h1 echoes the themed key "permissions" (was "security" until the 11-17 sub-tab pass).
//   6) id= / href= / data-tab= / data-testid= line counts equal the HEAD
//      baselines (subscription 18/1, billing 145/5, permissions 64/0/13/0).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const KEY = 'data-theme-key="workflows"';
const countLines = (lines, s) => lines.filter((l) => l.includes(s)).length;

// 1) Product map entry for the workflows key.
const mapSrc = read('js/product-names.js');
const entry = mapSrc.match(/'workflows':\s*\{([^}]*)\}/);
assert.ok(entry, "product-names.js has no 'workflows' key");
assert.ok(/codename:\s*'The Road'/.test(entry[1]), "'workflows' codename is not The Road");

// 2) subscription.html: one keyed span, literal Workflows after Running.
const sub = read('pages/subscription.html');
const subLines = sub.split('\n');
const subKeyed = subLines.filter((l) => l.includes(KEY));
assert.strictEqual(subKeyed.length, 1, 'subscription keyed lines: ' + subKeyed.length);
assert.ok(
  /<span class="[^"]*">Running <span data-theme-key="workflows">Workflows<\/span><\/span>/.test(subKeyed[0]),
  'subscription keyed span shape changed',
);
assert.strictEqual(countLines(subLines, 'id='), 18, 'subscription id= count changed');
assert.strictEqual(countLines(subLines, 'href='), 1, 'subscription href= count changed');

// 3) billing.html: one keyed span, same shape.
const bil = read('pages/billing.html');
const bilLines = bil.split('\n');
const bilKeyed = bilLines.filter((l) => l.includes(KEY));
assert.strictEqual(bilKeyed.length, 1, 'billing keyed lines: ' + bilKeyed.length);
assert.ok(
  /<span class="[^"]*">Running <span data-theme-key="workflows">Workflows<\/span><\/span>/.test(bilKeyed[0]),
  'billing keyed span shape changed',
);
assert.strictEqual(countLines(bilLines, 'id='), 145, 'billing id= count changed');
assert.strictEqual(countLines(bilLines, 'href='), 5, 'billing href= count changed');

// 4) permissions.html: span inside the perm-tab button (svg kept) and an h3.
const perm = read('pages/permissions.html');
const permLines = perm.split('\n');
const permKeyed = permLines.filter((l) => l.includes(KEY));
assert.strictEqual(permKeyed.length, 2, 'permissions keyed lines: ' + permKeyed.length);
const tabSpan = permKeyed.find((l) => l.includes('<span '));
assert.ok(tabSpan && tabSpan.trim() === '<span data-theme-key="workflows">Workflows</span>', 'tab span shape changed');
const tabIdx = permLines.indexOf(tabSpan);
assert.ok(permLines[tabIdx - 1].includes('<svg'), 'tab span must follow the svg inside the button');
assert.ok(/<button class="perm-tab /.test(permLines.slice(0, tabIdx).reverse().find((l) => l.includes('<button')) || ''),
  'tab span must sit inside a perm-tab button');
const h3 = permKeyed.find((l) => l.includes('<h3'));
assert.ok(h3 && /<h3 class="[^"]*" data-theme-key="workflows">Workflows<\/h3>/.test(h3), 'h3 key shape changed');

// 5) permissions h1 echoes the themed sidebar name (key permissions, 11-17 sub-tab pass; was security).
const secCount = permLines.filter((l) => l.includes('data-theme-key="security"')).length;
assert.strictEqual(secCount, 0, 'permissions security key count: ' + secCount);
const permH1 = permLines.filter((l) => l.includes('<h1 ') && l.includes('data-theme-key="permissions"')).length;
assert.strictEqual(permH1, 1, 'permissions h1 permissions key count: ' + permH1);

// 6) permissions id/href/data-tab/data-testid baselines.
assert.strictEqual(countLines(permLines, 'id='), 64, 'permissions id= count changed');
assert.strictEqual(countLines(permLines, 'href='), 0, 'permissions href= count changed');
assert.strictEqual(countLines(permLines, 'data-tab='), 13, 'permissions data-tab= count changed');
assert.strictEqual(countLines(permLines, 'data-testid='), 0, 'permissions data-testid= count changed');

console.log('oz-c3-p1-workflows-word.check.js: 6 checks passed');
