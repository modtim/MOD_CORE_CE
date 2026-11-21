// OZ System page tab theme labels (owner 11-17, owner report on CE PIN4, D74).
// pages/analytics.html: the CE-visible main tabs and Settings sub-tabs carry
// <span data-theme-key="system-tab-<tab>"> around the tab text (icons stay
// outside the span). Checks (no browser, no network; reads the page as text):
//   1) the 7 new hooks exist with their literal fallback text, in DOM order;
//   2) each key is in PRODUCT_NAMES, codename 'The City', label
//      'The City - <tab>', flag OFF returns the literal, flag ON the label;
//   3) the Workers and Stacks tabs keep their own page keys;
//   4) only the 7 CE-visible tab buttons (of the 18 on the page) carry a key;
//      the CE-hidden (paid-only) tabs stay unthemed.
// Run: r9700-run.sh nodetest Dashboard tests/oz-system-tabs.check.js

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const DASH = path.resolve(__dirname, '..');
const namesSrc = fs.readFileSync(path.join(DASH, 'js', 'product-names.js'), 'utf8');
const { PRODUCT_NAMES, themeLabel } = new Function(
  namesSrc.replace(/^export /gm, '') + '\nreturn { PRODUCT_NAMES, themeLabel };'
)();
const html = fs.readFileSync(path.join(DASH, 'pages', 'analytics.html'), 'utf8');

const WANT = [
  ['system-tab-system-health', 'System Health', 'System Health'],
  ['system-tab-external', 'External', 'External'],
  ['system-tab-settings', 'Settings', 'Settings'],
  ['system-tab-usage', 'Usage Analytics', 'Usage Analytics'],
  ['system-tab-activity', 'Activity', 'Activity'],
  ['system-tab-platform-settings', 'Platform Settings', 'Platform Settings'],
  ['system-tab-change-gate', 'Change-Gate Report', 'Change-Gate Report'],
];

test('7 System page tab hooks: literal fallback + themed label', () => {
  const re = /<span data-theme-key="(system-tab-[a-z-]+)">([^<]+)<\/span>/g;
  const got = [];
  let m;
  while ((m = re.exec(html)) !== null) got.push([m[1], m[2]]);
  assert.deepStrictEqual(got, WANT.map((w) => [w[0], w[1]]), 'hooks in DOM order');
  for (const [key, literal, name] of WANT) {
    const e = PRODUCT_NAMES[key];
    assert.ok(e, 'unknown key ' + key);
    assert.strictEqual(e.codename, 'The City', key + ' codename');
    assert.strictEqual(e.label, 'The City - ' + name, key + ' label');
    assert.ok(e.subtitle && e.subtitle.length > 0, key + ' subtitle');
    assert.strictEqual(themeLabel(key, literal, 'off'), literal, key + ' OFF keeps literal');
    assert.strictEqual(themeLabel(key, literal, 'on'), e.label, key + ' ON label');
  }
});

test('usage panel h2 echoes the usage tab key', () => {
  assert.ok(/<h2 [^>]*data-theme-key="system-tab-usage">Usage Analytics<\/h2>/.test(html));
});

test('Workers and Stacks keep their own page keys (the Cloud tab is pinned by oz-e3-balloon)', () => {
  assert.ok(/<span data-theme-key="workers">Workers<\/span>/.test(html));
  assert.ok(/<span data-theme-key="stacks">Stacks<\/span>/.test(html));
});

test('only the CE-visible tabs carry a system-tab key', () => {
  const ok = new Set(['main-tab-system', 'main-tab-external', 'main-tab-settings', 'settings-subtab-usage',
    'settings-subtab-activity', 'settings-subtab-platform-settings', 'settings-subtab-change-gate']);
  const btns = html.match(/<button id="(?:main-tab|settings-subtab)-[a-z-]+"[\s\S]*?<\/button>/g) || [];
  assert.strictEqual(btns.length, 18, 'tab buttons on the page: ' + btns.length);
  let keyed = 0;
  for (const b of btns) {
    const id = /<button id="([^"]+)"/.exec(b)[1];
    if (/data-theme-key="system-tab-/.test(b)) { keyed++; assert.ok(ok.has(id), id + ' must stay unthemed (hidden on CE)'); }
  }
  assert.strictEqual(keyed, 7, 'system-tab keyed buttons: ' + keyed);
});
