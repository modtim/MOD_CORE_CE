// OZ Help doc cards + mind-map box labels (owner 11-17, D73): the Signpost (Help)
// doc cards carry data-theme-key hooks with label 'Signpost - <card name>', and
// the mind-map boxes/aside title use the sidebar theme name when node.id equals
// a product-names map key. Checks (no browser, no network; reads files as text):
//   1) exactly 7 doc-card/doc-link hooks on pages/help.html, each key in the map,
//      codename Signpost, literal text inside the element = flag-OFF fallback,
//      ON label = 'Signpost - ' + literal;
//   2) the paid-only FAQ doc card stays plain (no hook);
//   3) js/help-mindmap.js imports themeLabel and routes the box text and the
//      aside title through one nodeLabel(node) helper (no raw node.label left);
//   4) ids that equal a map key theme, other ids stay plain, flag OFF = literal.
// Run: r9700-run.sh nodetest Dashboard tests/oz-help-doc-cards-mindmap-labels.check.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const DASH = path.resolve(__dirname, '..');
const namesSrc = fs.readFileSync(path.join(DASH, 'js', 'product-names.js'), 'utf8');
const { PRODUCT_NAMES, themeLabel } = new Function(
  namesSrc.replace(/^export /gm, '') + '\nreturn { PRODUCT_NAMES, themeLabel };'
)();
const helpHtml = fs.readFileSync(path.join(DASH, 'pages', 'help.html'), 'utf8');
const mindmap = fs.readFileSync(path.join(DASH, 'js', 'help-mindmap.js'), 'utf8');

const CARDS = [
  ['help-card-getting-started', 'Getting Started Guide'],
  ['help-card-nodes', 'Node Library'],
  ['help-card-api-reference', 'API Reference'],
  ['help-card-worker-setup', 'Worker Setup'],
  ['help-card-troubleshooting', 'Troubleshooting'],
  ['help-card-full-docs', 'Full Documentation Site'],
  ['help-link-open-docs', 'Open docs'],
];

test('7 doc cards/links carry a Signpost hook with the literal as fallback', () => {
  const re = /<(?:span|h3)\b[^>]*data-theme-key="(help-(?:card|link)-[a-z-]+)"[^>]*>([^<]+)<\/(?:span|h3)>/g;
  const found = [];
  let m;
  while ((m = re.exec(helpHtml)) !== null) found.push([m[1], m[2]]);
  assert.deepStrictEqual(found.sort(), CARDS.slice().sort(), 'hook keys + literals');
  for (const [key, literal] of found) {
    const entry = PRODUCT_NAMES[key];
    assert.ok(entry, 'unknown key ' + key);
    assert.strictEqual(entry.codename, 'Signpost', key + ' codename');
    assert.strictEqual(entry.label, 'Signpost - ' + literal, key + ' label format');
    assert.strictEqual(themeLabel(key, literal, 'off'), literal, key + ' OFF keeps literal');
    assert.strictEqual(themeLabel(key, literal, 'on'), entry.label, key + ' ON label');
  }
  assert.strictEqual(found.length, 7, 'card hooks: ' + found.length);
});

test('the paid-only FAQ doc card stays plain (CE scrub row byte-matches it)', () => {
  const m = /<h3 class="font-semibold text-gray-800">Billing FAQ<\/h3>/.exec(helpHtml);
  assert.ok(m, 'FAQ h3 literal unchanged, no hook');
});

test('mind map routes box text and aside title through themeLabel(node.id, node.label)', () => {
  assert.ok(/import \{ themeLabel \} from '\.\/product-names\.js';/.test(mindmap), 'import');
  assert.ok(/return themeLabel\(node\.id, node\.label\);/.test(mindmap), 'helper');
  assert.strictEqual((mindmap.match(/nodeLabel\(node\)/g) || []).length, 3, 'definition + box + aside use helper');
  const raw = mindmap.split('\n').filter((l) => /node\.label/.test(l) && !/^\s*(\/\/|return themeLabel)/.test(l));
  assert.deepStrictEqual(raw, [], 'no raw node.label rendering left');
});

test('mind-map ids that equal a map key theme; others plain; OFF = literal', () => {
  const themed = ['help', 'workflow-builder', 'analytics', 'triggers'];
  for (const id of themed) {
    assert.ok(PRODUCT_NAMES[id], id + ' is a map key');
    assert.strictEqual(themeLabel(id, 'plain', 'on'), PRODUCT_NAMES[id].label, id + ' ON');
    assert.strictEqual(themeLabel(id, 'plain', 'off'), 'plain', id + ' OFF');
  }
  for (const id of ['getting-started', 'api-reference', 'legal', 'legal/privacy-policy', 'help#section']) {
    assert.strictEqual(themeLabel(id, 'Docs Label', 'on'), 'Docs Label', id + ' stays plain');
  }
});
