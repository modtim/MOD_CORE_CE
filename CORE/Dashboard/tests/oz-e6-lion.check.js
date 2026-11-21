// OZ-E6 check (static, source-text asserts; no browser).
// The compliance page h1 (pages/compliance.html) carries
// data-theme-key="compliance-center" (product map key 'compliance-center' =
// codename Lion). Its visible literal stays "Compliance"
// (MOD_THEME_NAMES off = today's literal, byte for byte).
// CNSA wording on every compliance*.html page is untouched.
//
//   node CORE/Dashboard/tests/oz-e6-lion.check.js
//
// Checks:
//   1) js/product-names.js maps 'compliance-center' to codename Lion.
//   2) pages/compliance.html has exactly one h1 and it carries
//      data-theme-key="compliance-center" with literal text Compliance.
//   3) Stripping the attribute restores the pre-edit h1 line byte for byte.
//   4) pages/compliance-center.html still has its keyed h1.
//   5) The other six compliance-*.html pages: h1 count 0, no data-theme-key.
//   6) compliance.html id= and href= line counts equal the pre-edit baseline.
//   7) Per-file CNSA count of every compliance*.html equals the baseline
//      (all 0), and the 'CNSA 2.0' count in compliance.html is unchanged.

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const countLines = (text, needle) => text.split('\n').filter((l) => l.includes(needle)).length;

const PRE_H1 = '        <h1 class="text-3xl font-bold text-gray-800">Compliance</h1>';
const KEY = 'data-theme-key="compliance-center"';
const OTHERS = [
  'compliance-attestations.html',
  'compliance-hardening.html',
  'compliance-obligations.html',
  'compliance-output-scanners.html',
  'compliance-policies.html',
  'compliance-requirements.html',
];
const CNSA_BASELINE = {
  'compliance.html': 0,
  'compliance-center.html': 0,
  'compliance-attestations.html': 0,
  'compliance-hardening.html': 0,
  'compliance-obligations.html': 0,
  'compliance-output-scanners.html': 0,
  'compliance-policies.html': 0,
  'compliance-requirements.html': 0,
};

// 1) Product map entry for the compliance-center key.
const mapSrc = read('js/product-names.js');
const entry = mapSrc.match(/'compliance-center':\s*\{([^}]*)\}/);
assert.ok(entry, "product-names.js has no 'compliance-center' key");
assert.ok(/codename:\s*'Lion'/.test(entry[1]), "'compliance-center' codename is not Lion");

// 2) Exactly one h1 on compliance.html, and it carries the key.
const html = read('pages/compliance.html');
const lines = html.split('\n');
assert.strictEqual(countLines(html, '<h1'), 1, 'compliance.html must have exactly one h1');
const themed = lines.filter((l) => l.includes(KEY));
assert.strictEqual(themed.length, 1, 'expected one line with ' + KEY + ', got ' + themed.length);
const h1Line = themed[0];
assert.ok(/^\s*<h1 class="[^"]*" data-theme-key="compliance-center">/.test(h1Line), 'theme key must sit on the h1');
const literal = h1Line.match(/>([^<]*)<\/h1>/);
assert.ok(literal && literal[1] === 'Compliance', 'h1 literal changed');

// 3) Stripping the attribute restores the pre-edit h1 line byte for byte.
const restored = h1Line.replace(' ' + KEY, '');
assert.strictEqual(restored, PRE_H1, 'stripping the theme attribute must restore the pre-edit h1 line');

// 4) compliance-center.html keeps its keyed h1.
const cc = read('pages/compliance-center.html');
assert.ok(
  cc.split('\n').some((l) => /<h1[^>]*data-theme-key="compliance-center"/.test(l)),
  'compliance-center.html lost its keyed h1',
);

// 5) The other compliance pages stay untouched: no h1, no theme key.
for (const name of OTHERS) {
  const src = read('pages/' + name);
  assert.strictEqual(countLines(src, '<h1'), 0, name + ' gained an h1');
  assert.strictEqual(countLines(src, 'data-theme-key'), 0, name + ' gained a data-theme-key');
}

// 6) id= / href= line counts on compliance.html equal the baseline.
const idCount = countLines(html, 'id=');
const hrefCount = countLines(html, 'href=');
assert.strictEqual(idCount, 17, 'id= line count changed: ' + idCount);
assert.strictEqual(hrefCount, 0, 'href= line count changed: ' + hrefCount);

// 7) CNSA wording untouched: per-file counts equal the baseline.
for (const [name, expected] of Object.entries(CNSA_BASELINE)) {
  const src = read('pages/' + name);
  assert.strictEqual(countLines(src, 'CNSA'), expected, 'CNSA count changed in ' + name);
}
assert.strictEqual(countLines(html, 'CNSA 2.0'), 0, "'CNSA 2.0' count changed in compliance.html");

console.log('oz-e6-lion.check.js: 7 checks passed');
