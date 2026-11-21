// OZ-E4-P1 check (static, source-text asserts; no browser).
// The Stacks main tab in pages/analytics.html carries the product key
// 'stacks' (codename Emeralds) on a <span> wrapping the literal word
// "Stacks". The key sits on the span, not the <button>, so the svg icon
// inside the button is never overwritten by applyThemeLabels.
// pages/stack-billing.html is OZ-D6's and is deliberately NOT asserted.
//
//   node CORE/Dashboard/tests/oz-e4-emeralds.check.js
//
// Checks:
//   1) js/product-names.js maps 'stacks' to codename Emeralds.
//   2) pages/analytics.html has exactly one element with
//      data-theme-key="stacks", and it is a <span> whose literal is Stacks.
//   3) That span sits between id="main-tab-stacks" and the next </button>,
//      and the svg icon is still inside that button.
//   4) id= / href= / main-panel-stacks line counts equal the pre-edit
//      HEAD baseline (278 / 5 / 1 per grep -c).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');

// 1) Product map entry for the stacks key.
const mapSrc = read('js/product-names.js');
const entry = mapSrc.match(/'stacks':\s*\{([^}]*)\}/);
assert.ok(entry, "product-names.js has no 'stacks' key");
assert.ok(/codename:\s*'Emeralds'/.test(entry[1]), "'stacks' codename is not Emeralds");

// 2) Exactly one themed element on the analytics page, and it is the span.
const html = read('pages/analytics.html');
const lines = html.split('\n');
const themed = lines.filter((l) => l.includes('data-theme-key="stacks"'));
assert.strictEqual(themed.length, 1, 'expected one data-theme-key="stacks", got ' + themed.length);
assert.ok(
  /<span data-theme-key="stacks">Stacks<\/span>/.test(themed[0]),
  'the stacks key must sit on a <span> whose literal is Stacks',
);

// 3) The span lives inside the main-tab-stacks button; the svg is still there.
const btnStart = html.indexOf('id="main-tab-stacks"');
assert.ok(btnStart >= 0, 'id="main-tab-stacks" not found');
const spanAt = html.indexOf('data-theme-key="stacks"');
const btnEnd = html.indexOf('</button>', btnStart);
assert.ok(spanAt > btnStart && spanAt < btnEnd, 'stacks span is outside the main-tab-stacks button');
assert.ok(html.slice(btnStart, btnEnd).includes('<svg'), 'svg icon missing from main-tab-stacks button');

// 4) Attribute and structure counts unchanged from the pre-edit baseline.
const idCount = lines.filter((l) => l.includes('id=')).length;
const hrefCount = lines.filter((l) => l.includes('href=')).length;
const panelCount = lines.filter((l) => l.includes('main-panel-stacks')).length;
assert.strictEqual(idCount, 278, 'id= line count changed: ' + idCount);
assert.strictEqual(hrefCount, 5, 'href= line count changed: ' + hrefCount);
assert.strictEqual(panelCount, 1, 'main-panel-stacks count changed: ' + panelCount);

console.log('oz-e4-emeralds.check.js: 4 checks passed');
