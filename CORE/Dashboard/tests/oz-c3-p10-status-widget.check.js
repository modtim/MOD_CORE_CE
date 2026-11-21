// OZ-C3 PART 10 check (static, source-text asserts; no browser).
// Keyable spot in js/system-status-widget.js: the "Workers" probe row.
//   a) The three probe objects named 'Workers' (the /health probe success
//      path, its non-ok return and its catch return) carry themeKey 'workers'.
//   b) renderCard wraps the probe name in <span data-theme-key="workers">
//      only when a probe carries themeKey; every other probe row is unchanged.
//   c) Two success-path assignments of renderCard output (extended path and
//      basic path) are followed by one mod:page-rendered dispatch each, so the
//      label hook re-runs after the async widget render.
// Visible text stays byte-identical with theming off (the span has no text
// of its own). id= / innerHTML= / data-testid= counts equal the baseline.
//
//   node CORE/Dashboard/tests/oz-c3-p10-status-widget.check.js
//
// Checks:
//   1) js/product-names.js maps the 'workers' key.
//   2) exactly three probe objects carry themeKey: 'workers'.
//   3) renderCard keys the name only when p.themeKey is set (one keyed span).
//   4) stripping the conditional span restores the pre-edit row line byte for byte.
//   5) exactly two mod:page-rendered dispatches, each with the OZ-C3-P10 comment.
//   6) innerHTML / id= / data-testid= counts equal the baseline.
//   7) the 'Workers' product word is still the rendered text (no literal tags).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const count = (text, needle) => text.split(needle).length - 1;

const SW = 'js/system-status-widget.js';
const PN = 'js/product-names.js';
const src = read(SW);

// Baselines measured on HEAD before the OZ-C3-P10 edit.
const BASE_INNERHTML = 14;
const BASE_ID = 2;
const BASE_TESTID = 0;

// 1) product key exists (do not edit product-names.js).
assert.ok(count(read(PN), "'workers': {") === 1, "product-names.js must map 'workers' once");

// 2) three Workers probe objects are keyed.
assert.strictEqual(count(src, "themeKey: 'workers'"), 3, 'three Workers probe objects keyed');
assert.strictEqual(count(src, "name: 'Workers'"), 3, "three probe objects still named 'Workers'");

// 3) renderCard keys the name conditionally, once.
const RENDER_KEYED =
    '<span class="text-gray-700">${p.themeKey ? `<span data-theme-key="${escapeHtml(p.themeKey)}">${escapeHtml(p.name)}</span>` : escapeHtml(p.name)}</span>';
assert.strictEqual(count(src, RENDER_KEYED), 1, 'renderCard row keyed exactly once');
assert.strictEqual(count(src, 'data-theme-key="${escapeHtml(p.themeKey)}"'), 1, 'one data-theme-key render site');

// 4) stripping the conditional span restores the pre-edit row line.
const PRE_ROW = '<span class="text-gray-700">${escapeHtml(p.name)}</span>';
const stripped = RENDER_KEYED.replace(
    '${p.themeKey ? `<span data-theme-key="${escapeHtml(p.themeKey)}">${escapeHtml(p.name)}</span>` : escapeHtml(p.name)}',
    '${escapeHtml(p.name)}',
);
assert.strictEqual(stripped, PRE_ROW, 'stripped row equals pre-edit row byte for byte');

// 5) dispatch lines: exactly two, each with the OZ-C3-P10 comment.
const DISPATCH =
    "document.dispatchEvent(new CustomEvent('mod:page-rendered')); // OZ-C3-P10: re-run the label hook after async widget render";
assert.strictEqual(count(src, DISPATCH), 2, 'two page-rendered dispatches with the P10 comment');
assert.strictEqual(count(src, "CustomEvent('mod:page-rendered')"), 2, 'no other page-rendered dispatch added');

// 6) attribute-level counts equal the baseline.
assert.strictEqual(count(src, 'innerHTML'), BASE_INNERHTML, 'innerHTML count unchanged');
assert.strictEqual(count(src, 'id='), BASE_ID, 'id= count unchanged');
assert.strictEqual(count(src, 'data-testid='), BASE_TESTID, 'data-testid= count unchanged');

// 7) the probe name is never rendered as a literal tag string.
assert.ok(!src.includes("name: '<span"), 'probe name must stay plain text, not markup');

console.log('oz-c3-p10-status-widget.check.js: all checks passed');
