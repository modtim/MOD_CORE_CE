// OZ-B1-SIDEBAR-1: nav anchors 1-10 of dashboard-shell.html carry a
// data-oz-key hook that resolves in js/product-names.js. Today's literal text,
// ids and hrefs are unchanged; with MOD_THEME_NAMES off the label is today's
// literal byte for byte. Page ids/hrefs/routes never change.
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const HTML = fs.readFileSync(path.join(__dirname, '..', 'pages', 'dashboard-shell.html'), 'utf8');
const MAP_SRC = path.join(__dirname, '..', 'js', 'product-names.js');

// The 10 first nav anchors in document order: [href, id|null, today literal, oz key|null]
const FIRST_10 = [
  ['#home', null, 'Home', 'home'],
  ['#files', null, 'Data', 'files'],
  ['#workflows', 'workflow-builder-link', 'Workflow Builder', 'workflows'],
  ['#marketplace', 'marketplace-nav-link', 'Marketplace', 'marketplace'],
  ['#system', 'system-nav-link', 'System', 'system'],
  ['#logs', 'logs-nav-link', 'Logs', 'logs'],
  ['#permissions', 'permissions-nav-link', 'Permissions', 'permissions'],
  ['#compliance-center', 'compliance-center-nav-link', 'Compliance', 'compliance-center'],
  ['#security', 'security-nav-link', 'Security', 'security'],
  ['#users', 'users-nav-link', 'Users', 'users'],
];

function anchors() {
  const out = [];
  const re = /<a href="([^"]+)"([^>]*class="nav-link[^"]*"[^>]*)>([\s\S]*?)<\/a>/g;
  let m;
  while ((m = re.exec(HTML))) {
    const id = (m[2].match(/\sid="([^"]+)"/) || [])[1] || null;
    const span = m[3].match(/<span class="ml-3"([^>]*)>([^<]*)<\/span>/);
    const key = span ? (span[1].match(/data-theme-key="([^"]+)"/) || [])[1] || null : null;
    out.push({href: m[1], id, text: span ? span[2] : null, key});
  }
  return out;
}

let mod;
test('load product-names.js', async () => {
  mod = await import('file://' + MAP_SRC);
  assert.ok(mod.PRODUCT_NAMES && mod.themeLabel);
});

test('first 10 anchors: href/id/today literal unchanged', () => {
  const a = anchors().slice(0, 10);
  assert.strictEqual(a.length, 10);
  FIRST_10.forEach((e, i) => {
    assert.strictEqual(a[i].href, e[0], 'href ' + i);
    assert.strictEqual(a[i].id, e[1], 'id ' + i);
    assert.strictEqual(a[i].text, e[2], 'literal ' + i);
    assert.strictEqual(a[i].key, e[3], 'key ' + i);
  });
});

test('every hook key resolves in the map; ON = map label, OFF = today literal', () => {
  const a = anchors().slice(0, 10);
  let hooked = 0;
  a.forEach((x) => {
    if (!x.key) return;
    hooked++;
    const entry = mod.PRODUCT_NAMES[x.key];
    assert.ok(entry && entry.label && entry.subtitle, 'map has ' + x.key);
    assert.ok(!entry.paidOnly, x.key + ' is not paid-only');
    assert.strictEqual(mod.themeLabel(x.key, x.text, 'on'), entry.label);
    assert.strictEqual(mod.themeLabel(x.key, x.text, 'off'), x.text);
  });
  assert.strictEqual(hooked, 10);
});

// IMPL-OZ-NAMES-LEG-A (11-14): the 10 sidebar entries for the
// Leg-0 keys (every Leg-0 key except counting-house) now carry data-theme-key on
// their label span, so this "later anchors untouched" guard must allow exactly
// those 10 keys and nothing else.
const LEG_A_KEYS = new Set([
  'payments-admin', 'promo-codes', 'free-trials', 'geographic-pricing',
  'seasonal-pricing', 'help', 'storage', 'webhooks', 'drift-events', 'settings',
]);

test('only anchors 1-10 are hooked here; later anchors untouched by B1-1', () => {
  const rest = anchors().slice(10);
  // workers / capture-devices carry the OZ-WIRING data-theme-key on their label span (checked in oz-wiring-label-hook)
  assert.ok(rest.every((x) => x.key === null || x.key === 'workers' || x.key === 'capture-devices' || LEG_A_KEYS.has(x.key)));
  assert.strictEqual(anchors().length, 30);
});

// IMPL-OZ-NAMES-LEG-A: the 10 sidebar entries for the Leg-0 keys (all Leg-0 keys
// except counting-house) carry data-theme-key on their label span; each key
// resolves in js/product-names.js; OFF renders today's literal byte for byte.
const LEG_A = [
  ['#payments-admin', 'payments-admin', 'Payments'],
  ['#promo-codes', 'promo-codes', 'Promo Codes'],
  ['#free-trials', 'free-trials', 'Free Trials'],
  ['#geographic-pricing', 'geographic-pricing', 'Geographic Pricing'],
  ['#seasonal-pricing', 'seasonal-pricing', 'Seasonal Pricing'],
  ['#help', 'help', 'Help'],
  ['#storage', 'storage', 'Storage'],
  ['#webhooks', 'webhooks', 'Webhooks'],
  ['#drift-events', 'drift-events', 'Drift Events'],
  ['#settings', 'settings', 'Settings'],
];

test('IMPL-OZ-NAMES-LEG-A: 10 sidebar anchors carry their key and the key is in the map', () => {
  const byHref = {};
  anchors().forEach((x) => byHref[x.href] = x);
  LEG_A.forEach(([href, key, literal]) => {
    const a = byHref[href];
    assert.ok(a, 'anchor ' + href + ' exists');
    assert.strictEqual(a.text, literal, href + ' today literal unchanged');
    assert.strictEqual(a.key, key, href + ' carries data-theme-key="' + key + '"');
    const entry = mod.PRODUCT_NAMES[key];
    assert.ok(entry && entry.label && entry.subtitle, 'product-names.js has key ' + key);
    assert.strictEqual(mod.themeLabel(key, literal, 'off'), literal, 'OFF = today literal for ' + key);
    assert.strictEqual(mod.themeLabel(key, literal, 'on'), entry.label, 'ON = map label for ' + key);
  });
  const keyed = anchors().filter((x) => x.key && LEG_A_KEYS.has(x.key)).map((x) => x.key);
  assert.strictEqual(keyed.length, 10, 'exactly 10 new keyed anchors');
  assert.deepStrictEqual(keyed.sort(), LEG_A.map((e) => e[1]).sort());
});
