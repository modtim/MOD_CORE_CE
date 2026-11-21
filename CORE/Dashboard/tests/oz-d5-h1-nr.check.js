// OZ-D5-H1-NR guard: h1 theming for pages/*.html names n-r (same rule as OZ-D3).
// - Each page in the slice must carry data-theme-key on its first <h1>.
// - That key must exist in js/product-names.js (the NAME-MAP registry).
// - A page with no h1, no key, or an unknown key FAILS. Do not weaken this check.
// - Deterministic: reads files only, no network.
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const pagesDir = path.join(root, 'pages');
const namesPath = path.join(root, 'js', 'product-names.js');

const names = fs.readFileSync(namesPath, 'utf8');
const registryKeys = new Set();
for (const m of names.matchAll(/^\s*'([a-z0-9-]+)':\s*\{/gm)) {
  registryKeys.add(m[1]);
}

// Pages with NO <h1> element: outside the h1 rule (Tim's list 11-14, confirmed on disk).
// They must keep 0 h1s; the rule below is not weakened for them.
const NO_H1 = ['onboarding.html', 'operator-proposals.html', 'payment-settings.html'];

const slice = fs
  .readdirSync(pagesDir)
  .filter((f) => /^[n-r].*\.html$/.test(f))
  .sort();

test('n-r slice is non-empty and the registry parsed', () => {
  assert.ok(slice.length > 0, 'no pages/*.html names n-r found');
  assert.ok(registryKeys.size > 0, 'product-names.js registry parsed to zero keys');
});

for (const file of slice) {
  if (NO_H1.includes(file)) {
    test(`${file}: no h1 (out of scope for the h1 rule)`, () => {
      const html = fs.readFileSync(path.join(pagesDir, file), 'utf8');
      assert.strictEqual(html.match(/<h1\b/g) === null ? 0 : html.match(/<h1\b/g).length, 0,
        `${file}: gained an <h1>; the no-h1 pages are out of scope and must stay h1-free`);
    });
    continue;
  }
  test(`${file}: first h1 carries a registry data-theme-key`, () => {
    const html = fs.readFileSync(path.join(pagesDir, file), 'utf8');
    const h1 = html.match(/<h1\b[^>]*>/);
    assert.ok(h1, `${file}: no <h1> element`);
    const key = h1[0].match(/data-theme-key="([^"]+)"/);
    assert.ok(key, `${file}: h1 has no data-theme-key (no NAME-MAP key for this page)`);
    assert.ok(
      registryKeys.has(key[1]),
      `${file}: data-theme-key "${key[1]}" is not in product-names.js`,
    );
  });
}
