// OZ sub-tab theme labels (owner 11-17): the 18 page inner tabs on Settings, Help,
// Users, Permissions and Logs carry <span data-theme-key="<page>-tab-<tab>">
// around the tab text. Checks (no browser, no network; reads pages as text):
//   1) exactly 18 sub-tab hooks across the five pages, each key in PRODUCT_NAMES;
//   2) the literal text inside the span is the flag-OFF fallback (themeLabel
//      OFF === literal) and the flag-ON label is '<codename> - <sub-tab>';
//   3) the label format is '<codename> - <function>' with the parent codename
//      (Gearworks, Signpost, Citizens, The Guard, Records);
//   4) the Logs and Permissions h1 echo the themed sidebar key (logs, permissions).
// Run: r9700-run.sh nodetest Dashboard tests/oz-subtab-labels.check.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const DASH = path.resolve(__dirname, '..');
const namesSrc = fs.readFileSync(path.join(DASH, 'js', 'product-names.js'), 'utf8');
const { PRODUCT_NAMES, themeLabel } = new Function(
  namesSrc.replace(/^export /gm, '') + '\nreturn { PRODUCT_NAMES, themeLabel };'
)();

const PAGES = {
  settings: ['Gearworks', ['Integrations', 'Secrets', 'Permission Settings', 'API Keys', 'AI Configuration', 'Knowledge Base', 'Stack TLS']],
  help: ['Signpost', ['Cards', 'Mind Map', 'AI in this platform']],
  users: ['Citizens', ['Users', 'Invitations']],
  permissions: ['The Guard', ['Data', 'Secrets', 'API Keys', 'Templates']],
  logs: ['Records', ['Workflow Runs', 'System Tasks']],
};
const WANT_LABEL = {
  'AI in this platform': 'AI in this Platform',
};

function hooks(page) {
  const src = fs.readFileSync(path.join(DASH, 'pages', page + '.html'), 'utf8');
  const re = new RegExp('<span data-theme-key="(' + page + '-tab-[a-z-]+)">([^<]+)</span>', 'g');
  const out = [];
  let m;
  while ((m = re.exec(src)) !== null) out.push([m[1], m[2]]);
  return { src, out };
}

test('18 sub-tab hooks, literal fallback + themed label per tab', () => {
  let total = 0;
  for (const [page, [codename, tabs]] of Object.entries(PAGES)) {
    const { out } = hooks(page);
    const got = out.map((o) => o[1]);
    assert.deepStrictEqual(got, tabs, page + ' tab literals/order');
    for (const [key, literal] of out) {
      const entry = PRODUCT_NAMES[key];
      assert.ok(entry, 'unknown key ' + key);
      assert.strictEqual(entry.codename, codename, key + ' codename');
      assert.strictEqual(themeLabel(key, literal, 'off'), literal, key + ' OFF keeps literal');
      assert.strictEqual(themeLabel(key, literal, 'on'), entry.label, key + ' ON label');
      assert.strictEqual(entry.label, codename + ' - ' + (WANT_LABEL[literal] || literal), key + ' label format');
      total++;
    }
  }
  assert.strictEqual(total, 18, 'sub-tab hooks: ' + total);
});

test('Logs and Permissions h1 echo the themed sidebar keys', () => {
  const logs = hooks('logs').src;
  const perm = hooks('permissions').src;
  assert.ok(/<h1 [^>]*data-theme-key="logs">Logs<\/h1>/.test(logs), 'logs h1 key');
  assert.ok(/<h1 [^>]*data-theme-key="permissions">Permissions<\/h1>/.test(perm), 'permissions h1 key');
});
