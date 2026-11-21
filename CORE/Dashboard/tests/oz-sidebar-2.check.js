// OZ-B2 (11-14): dashboard-shell.html nav anchors 11-20 of 30.
// Checks (no browser, no network; reads the page and product-names.js as text):
//   1) the page still has exactly 30 `nav-link` anchors and anchors 11-20 keep
//      their id and href attributes byte for byte (ids-changed = 0).
//   2) only anchors whose key exists in PRODUCT_NAMES carry data-theme-key;
//      each such key equals the anchor's href id (no loose mapping).
//   3) for every keyed anchor: themeLabel(key, literal, 'off') === today's
//      literal, and themeLabel(key, literal, 'on') === PRODUCT_NAMES[key].label.
// Run: r9700-run.sh nodetest Dashboard tests/oz-sidebar-2.check.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const DASH = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(DASH, 'pages', 'dashboard-shell.html'), 'utf8');
const namesSrc = fs.readFileSync(path.join(DASH, 'js', 'product-names.js'), 'utf8');

// product-names.js has no imports: strip `export` and evaluate it in isolation.
const names = new Function(
  namesSrc.replace(/^export /gm, '') + '\nreturn { PRODUCT_NAMES, themeLabel };'
)();
const { PRODUCT_NAMES, themeLabel } = names;

// Every `<a ... class="nav-link ...">` block, in document order, with its
// attribute string and the inner HTML up to </a>.
function navAnchors(src) {
  const out = [];
  const re = /<a\s([^>]*class="nav-link[^"]*"[^>]*)>([\s\S]*?)<\/a>/g;
  let m;
  while ((m = re.exec(src)) !== null) {
    out.push({ attrs: m[1], inner: m[2] });
  }
  return out;
}

const attr = (attrs, name) => {
  const m = attrs.match(new RegExp('(?:^|\\s)' + name + '="([^"]*)"'));
  return m ? m[1] : null;
};

const anchors = navAnchors(html);
// ONE hook (11-14 unify): the key sits on the label span inside the anchor, not on the <a>.
const hookKey = (a) => {
  const span = a.inner.match(/<span class="ml-3" data-theme-key="([^"]+)">/);
  return span ? span[1] : null;
};

// Pre-edit id/href pairs for anchors 11-20 (captured from HEAD before OZ-B2).
const EXPECTED_11_20 = [
  ['workers-nav-link', '#workers'],
  ['capture-devices-nav-link', '#capture-devices'],
  ['billing-nav-link', '#billing'],
  ['pricing-nav-link', '#pricing'],
  ['admin-billing-nav-link', '#admin-plans'],
  ['admin-plans-nav-link', '#admin-plans'],
  ['gpu-pool-nav-link', '#gpu-pool'],
  ['pricing-config-nav-link', '#pricing-config'],
  ['stack-billing-nav-link', '#stack-billing'],
  ['admin-nodes-nav-link', '#admin-nodes'],
];

test('page still has 30 nav-link anchors', () => {
  assert.strictEqual(anchors.length, 30);
});

test('anchors 11-20 keep id and href (ids-changed = 0)', () => {
  const got = anchors.slice(10, 20).map((a) => [attr(a.attrs, 'id'), attr(a.attrs, 'href')]);
  const want = EXPECTED_11_20.map(([id, href]) => [id, href]);
  assert.deepStrictEqual(got, want);
});

test('data-theme-key is only on anchors 11-20 whose key is in PRODUCT_NAMES', () => {
  const keyed = anchors.slice(10, 20).filter((a) => hookKey(a) !== null);
  for (const a of keyed) {
    const key = hookKey(a);
    assert.ok(PRODUCT_NAMES[key], 'unknown key ' + key);
    assert.strictEqual(attr(a.attrs, 'href'), '#' + key, 'key must equal href id for ' + key);
  }
  // Anchors 11-20 whose href has no map key must NOT be tagged.
  // 11-17: a map key whose PAGE is paid-only or CE-denied keeps
  // its sidebar label literal - the label never ships as a CE themed label, so the
  // anchor stays untagged on the honest side of the cut. This is still a live
  // assertion: an untagged anchor is only allowed when its page is paid-only
  // (billing, admin-plans: paidOnly in the map) or its page js is denied in the
  // CE cut (admin-nodes: open-core/manifest.yaml deny row). An untagged anchor
  // whose page SHIPS in CE and has a map key is still a red.
  const untagged = anchors.slice(10, 20).filter((a) => hookKey(a) === null);
  const manifestPath = path.resolve(DASH, '..', '..', 'open-core', 'manifest.yaml');
  const manifestSrc = fs.existsSync(manifestPath) ? fs.readFileSync(manifestPath, 'utf8') : '';
  const deniedInCe = (key) => manifestSrc.includes('- CORE/Dashboard/js/' + key + '.js');
  const removedInCe = (anchorId) => new RegExp('id=\\\\?"' + anchorId + '\\\\?"[^\\n]*replace: ""').test(manifestSrc);
  for (const a of untagged) {
    const href = attr(a.attrs, 'href').slice(1);
    const entry = PRODUCT_NAMES[href];
    if (entry === undefined) continue;
    assert.ok(
      entry.paidOnly === true || deniedInCe(href) || removedInCe(attr(a.attrs, 'id')),
      'map key exists but anchor untagged and the page ships in CE: ' + href,
    );
  }
});

test('keyed labels: OFF renders today literal, ON renders map label', () => {
  const keyed = anchors.slice(10, 20).filter((a) => hookKey(a) !== null);
  assert.ok(keyed.length >= 1, 'expected at least one keyed anchor in 11-20');
  for (const a of keyed) {
    const key = hookKey(a);
    const lit = a.inner.match(/<span class="ml-3"(?: [^>]*)?>([^<]*)<\/span>/);
    assert.ok(lit, 'no ml-3 label span for ' + key);
    const literal = lit[1];
    assert.strictEqual(themeLabel(key, literal, 'off'), literal, 'OFF must be literal ' + key);
    assert.strictEqual(themeLabel(key, literal, 'on'), PRODUCT_NAMES[key].label, 'ON label ' + key);
    assert.strictEqual(themeLabel(key, literal, undefined), PRODUCT_NAMES[key].label, 'default ON ' + key);
  }
});

test('workers and capture-devices are the two keyed anchors in 11-20', () => {
  const keyed = anchors.slice(10, 20)
    .map((a) => hookKey(a))
    .filter((k) => k !== null);
  assert.deepStrictEqual(keyed, ['workers', 'capture-devices']);
});
