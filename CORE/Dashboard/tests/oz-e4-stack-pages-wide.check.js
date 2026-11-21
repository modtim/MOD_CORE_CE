// OZ-E4-P2 wide stack-pages check (static, source-text; no browser).
// Walks every CORE/Dashboard/pages/*.html and classifies each stack word that
// sits in a text node (between > and <) or in a title / aria-label attribute:
//   KEYED    on a line that carries data-theme-key="stacks" (Emeralds)
//   OWNED    stack-billing.html (OZ-D6) and the 'Stack Billing' nav label (OZ-D6)
//   EXEMPT   'Stack any number ...' (verb) and code scope ids such as stack:deploy
//   UNKEYED  a product word with no Emeralds key; must be in ALLOW below
// A NEW unkeyed stack word, or a changed allowlisted count, fails.
// 'Stackable' (add-on adjective) is excluded by the word boundary.
//
//   node CORE/Dashboard/tests/oz-e4-stack-pages-wide.check.js

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');

const pagesDir = path.resolve(__dirname, '..', 'pages');
const STACK = /\bstacks?\b/i;
const norm = (s) => s.replace(/\s+/g, ' ').trim();

// Frozen UNKEYED allowlist: [file, exact trimmed text or attr="value", count, reason].
// OZ-E4-BUILDER (): every one of the 60 words P2 froze here is now
// handled - the 42 rows below were removed when their pages were keyed with
// <span data-theme-key="stacks">..</span> (product words: labels, tabs, headings,
// buttons, loading text, empty-state prose, help prose). The list is EMPTY:
// any new unkeyed stack word fails.
const ALLOW = [
];

// Attribute hits (title / aria-label) are EXEMPT, not keyed: no data-theme-title-key
// or data-theme-aria-key mechanism exists in the shipped theming code (js/product-names.js
// + the [data-theme-key] DOM walk theme only element text nodes), and inventing one
// here is out of scope. Accessible names and tooltips are not themed text.
const ATTR_EXEMPT = new Set([
  'analytics.html\u0000aria-label="Stack name"',
  'analytics.html\u0000aria-label="Stack compose YAML"',
  'analytics.html\u0000title="Reservation metering rate — usage charged per hour while this stack holds its reserved CPU/memory/GPU"',
  'settings.html\u0000aria-label="Managed model stack"',
  'settings.html\u0000aria-label="Review model stack"',
  'settings.html\u0000aria-label="Stack this deploy key is pinned to"',
]);
// analytics.html text words are EXEMPT, not keyed: oz-e4-emeralds.check.js pins
// analytics.html at exactly ONE line carrying data-theme-key="stacks" and oz-c2-p1
// pins 0..1; both are outside this brief's EDITS, so keying these words here would
// turn those neighbours red. Follow-up (OZ-E4 closer) lifts the pins, then keys them.
const TEXT_EXEMPT = new Set([
  'analytics.html\u0000The whole-tailnet, default-deny policy is generated from each reachable stack\'s per-service egress posture. Edit a stack\'s posture below to change what it may reach; changes persist and are re-pushed to the coordinator when enforcement is on.',
  'analytics.html\u0000Runbook: deploy and recover a persistent stack',
  'analytics.html\u0000\u26a0 This stack\'s data will not be backed up',
  'analytics.html\u0000Stack name',
  'analytics.html\u0000New AI stack',
  'analytics.html\u0000Your stacks',
  'analytics.html\u0000= new-format stacks that request fixed or',
  'analytics.html\u0000Stack rebalancer',
  'analytics.html\u0000Stack authoring grants',
  'analytics.html\u0000Grant stack authoring',
]);

const hits = { keyed: [], owned: [], exempt: [], unkeyed: [], subtab: [] };
const unkeyedCount = new Map();
const addUnkeyed = (file, key, where) => {
  const k = `${file}\u0000${key}`;
  unkeyedCount.set(k, (unkeyedCount.get(k) || 0) + 1);
  hits.unkeyed.push(`${where} ${key}`);
};

const files = fs.readdirSync(pagesDir).filter((f) => f.endsWith('.html')).sort();
for (const file of files) {
  const lines = fs.readFileSync(path.join(pagesDir, file), 'utf8').split('\n');
  lines.forEach((line, i) => {
    const where = `${file}:${i + 1}`;
    const keyedLine = line.includes('data-theme-key="stacks"');
    const textRe = />([^<]*)</g;
    let m;
    while ((m = textRe.exec(line))) {
      const t = norm(m[1]);
      if (!STACK.test(t)) continue;
      if (/^Stack any number/.test(t) || /^stack:[a-z:]+$/.test(t) || TEXT_EXEMPT.has(`${file}\u0000${t}`)) hits.exempt.push(where);
      else if (keyedLine) hits.keyed.push(where);
      // Settings sub-tab 'Stack TLS' is keyed settings-tab-stack-tls (Gearworks sub-tab names, owner 11-17).
      else if (line.includes('data-theme-key="settings-tab-stack-tls"')) hits.subtab.push(where);
      else if (file === 'stack-billing.html' || t === 'Stack Billing') hits.owned.push(where);
      else addUnkeyed(file, t, where);
    }
    const attrRe = /(title|aria-label)="([^"]*)"/g;
    while ((m = attrRe.exec(line))) {
      const key = `${m[1]}="${m[2]}"`;
      if (!STACK.test(key)) continue;
      if (ATTR_EXEMPT.has(`${file}\u0000${key}`)) hits.exempt.push(where);
      else addUnkeyed(file, key, where);
    }
  });
}

// 1) Keyed hits are exactly the four Emeralds-keyed product labels.
assert.deepStrictEqual(
  [...new Set(hits.keyed.map((w) => w.split(':')[0]))].sort(),
  ['admin-nodes.html', 'analytics.html', 'coupons.html', 'key-inventory.html', 'marketplace.html', 'pricing-config.html', 'seller.html', 'settings.html', 'stack-billing.html'],
  'keyed stack labels moved: ' + hits.keyed.join(', '),
);
assert.strictEqual(hits.keyed.length, 54, 'keyed count changed: ' + hits.keyed.length);

// 2) Every unkeyed hit is allowlisted with the exact count; nothing new, nothing gone.
const allowMap = new Map(ALLOW.map(([f, k, n]) => [`${f}\u0000${k}`, n]));
const drift = [];
for (const [k, n] of unkeyedCount) {
  if (!allowMap.has(k)) drift.push(`NEW unkeyed stack word: ${k.replace('\u0000', ' :: ')} (x${n})`);
}
for (const [k, n] of allowMap) {
  const got = unkeyedCount.get(k) || 0;
  if (got !== n) drift.push(`allowlist drift: ${k.replace('\u0000', ' :: ')} expected ${n} got ${got}`);
}
assert.deepStrictEqual(drift, [], 'stack-word drift:\n' + drift.join('\n'));

// 3) Frozen counts for the classes that must not move silently.
assert.strictEqual(hits.owned.length, 11, 'owned count changed: ' + hits.owned.length);
assert.strictEqual(hits.exempt.length, 25, 'exempt count changed: ' + hits.exempt.length);
assert.strictEqual(hits.unkeyed.length, 0, 'unkeyed count changed: ' + hits.unkeyed.length);
assert.strictEqual(hits.subtab.length, 1, 'settings Stack TLS sub-tab hook count changed: ' + hits.subtab.length);

console.log('UNKEYED (allowlisted, OZ-E4-P3 input):');
hits.unkeyed.forEach((u) => console.log('  ' + u.slice(0, 120)));
console.log(
  `oz-e4-stack-pages-wide.check.js: files=${files.length} keyed=${hits.keyed.length} ` +
    `exempt=${hits.exempt.length} owned=${hits.owned.length} unkeyed=${hits.unkeyed.length} drift=0 - 3 checks passed`,
);
