// OZ-C2-P3 check (static, source-text asserts; no browser).
// Shared dashboard JS (ships to paid too). With theme names OFF the label hook
// is a no-op, so the visible text must stay byte-identical:
//   - js/analytics.js queue panel: `Workers` becomes
//     <span data-theme-key="workers">Workers</span>; stripping the span gives
//     the original string.
//   - js/analytics.js `<em>workers</em>` (sentence, ~line 2066) STAYS unkeyed.
//   - js/capacity-report.js signal row label 'Workers' (renderSignalRow) gets
//     data-theme-key via an optional row.themeKey; the row is textContent only.
//   - both files dispatch document 'mod:page-rendered' after the async render
//     so the label hook re-runs. Only product-labels.js listens to that event
//     and it does not re-render, so no loop.
//
//   node CORE/Dashboard/tests/oz-c2-p3-analytics-js-workers.check.js
//
// Checks:
//   a) js/product-names.js maps 'workers' (do not edit; key must exist).
//   b) analytics.js has exactly one data-theme-key="workers" and it is the
//      span in the queue panel; stripping it gives the original text.
//   c) analytics.js <em>workers</em> sentence is untouched (no key).
//   d) analytics.js dispatches mod:page-rendered once, after the innerHTML.
//   e) capacity-report.js: Workers row carries themeKey 'workers', the
//      renderer copies it to data-theme-key, and dispatch follows appendChild.
//   f) id=, data-testid=, innerHTML line counts equal the pre-edit baseline
//      (analytics 22 / 1 / 74; capacity-report 0 / 0 / 5 per grep -c).
//   g) mod:page-rendered listeners: only product-labels.js (no re-render).

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const root = path.resolve(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8');
const countLines = (src, needle) => src.split('\n').filter((l) => l.includes(needle)).length;

// a) Product map key exists.
const names = read('js/product-names.js');
assert.ok(/'workers':\s*\{/.test(names), "product-names.js has no 'workers' key");

// b) analytics queue panel span.
const analytics = read('js/analytics.js');
const keyed = countLines(analytics, 'data-theme-key="workers"');
assert.strictEqual(keyed, 1, 'expected 1 data-theme-key="workers" in analytics.js, got ' + keyed);
const spanText = '<span data-theme-key="workers">Workers</span>';
const originalText = '<div>Workers: ${queue.licensed_workers} registered / ${queue.workers_watching} total</div>';
const keyedLine = '<div>' + spanText + ': ${queue.licensed_workers} licensed / ${queue.workers_watching} total</div>';
assert.ok(analytics.includes(keyedLine), 'keyed queue-panel line not found');
assert.strictEqual(keyedLine.split(spanText).join('Workers'), originalText,
  'stripping the span must give the original string');

// c) The sentence <em>workers</em> stays unkeyed.
assert.ok(analytics.includes('<em>workers</em>, whose network view'), '<em>workers</em> sentence changed');
assert.ok(!analytics.includes('<em data-theme-key'), '<em>workers</em> must not be keyed');

// d) Dispatch after the queue-panel innerHTML assignment, exactly once.
const dispatch = "document.dispatchEvent(new CustomEvent('mod:page-rendered'));";
assert.strictEqual(countLines(analytics, dispatch), 1, 'analytics.js dispatch count != 1');
assert.ok(analytics.indexOf(dispatch) > analytics.indexOf('capacityEl.innerHTML = `'),
  'analytics.js dispatch must follow the innerHTML assignment');
assert.ok(analytics.includes('OZ-C2-P3: re-run the label hook after async render'), 'analytics.js comment missing');

// e) capacity-report.js row key, renderer copy, and dispatch.
const cap = read('js/capacity-report.js');
assert.ok(cap.includes("{ label: 'Workers', themeKey: 'workers', value:"), 'Workers row themeKey missing');
assert.ok(cap.includes('if (r.themeKey) label.dataset.themeKey = r.themeKey;'), 'renderer does not copy themeKey');
const appendIdx = cap.indexOf('rows.forEach(r => container.appendChild(r));');
assert.ok(appendIdx > 0, 'renderSignals append line not found');
const capDispatchIdx = cap.indexOf(dispatch);
assert.ok(capDispatchIdx > appendIdx, 'capacity-report.js dispatch must follow the append loop');
assert.strictEqual(countLines(cap, dispatch), 1, 'capacity-report.js dispatch count != 1');

// f) Line-count baselines (same grep -c semantics as the baseline).
assert.strictEqual(countLines(analytics, 'id='), 22, 'analytics id= count changed');
assert.strictEqual(countLines(analytics, 'data-testid='), 1, 'analytics data-testid= count changed');
assert.strictEqual(countLines(analytics, 'innerHTML'), 74, 'analytics innerHTML count changed');
assert.strictEqual(countLines(cap, 'id='), 0, 'capacity-report id= count changed');
assert.strictEqual(countLines(cap, 'data-testid='), 0, 'capacity-report data-testid= count changed');
assert.strictEqual(countLines(cap, 'innerHTML'), 5, 'capacity-report innerHTML count changed');

// g) Only product-labels.js listens to mod:page-rendered.
const jsDir = path.join(root, 'js');
const listeners = fs.readdirSync(jsDir).filter((f) => f.endsWith('.js'))
  .filter((f) => read('js/' + f).includes("addEventListener('mod:page-rendered'"));
assert.deepStrictEqual(listeners, ['product-labels.js'], 'unexpected mod:page-rendered listeners: ' + listeners);

console.log('oz-c2-p3-analytics-js-workers.check.js: 7 checks passed');
