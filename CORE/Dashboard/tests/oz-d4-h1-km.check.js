// OZ-D4-H1-KM guard: k-m pages h1 theming is additive (pattern of oz-d3-h1-hj.check.js).
// Per page in the slice (names starting k to m that carry an h1):
// - the h1 carries data-theme-key="<key>" where the key is the one NAME-MAP names
//   (the internal design notes). A page with key=null has NO NAME-MAP row:
//   it fails as UNMAPPED until the owner/mapping decision is made (never weakened).
// - the visible h1 literal is unchanged (flag-off = today's literal).
// - id= and href= attribute list is unchanged (count + md5 prefix taken at HEAD before OZ-D4).
// Pages with no h1 (login.html, logged-out.html, mpc-sessions.html) are outside the h1 rule.
// No network. Plain fs reads only.
'use strict';

const assert = require('node:assert');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { test } = require('node:test');

const pagesDir = path.join(__dirname, '..', 'pages');

// page, expected key (wholeSystem = owner-approved whole-system page: NO product key, must stay unkeyed),
// key source, h1 literal at HEAD, id/href count, md5 prefix at HEAD.
// Keys approved by owner 11-14 ~10:00 PM CT (OWNER-ITEMS/OZ-D-UNMAPPED-PAGES-KEYS-FOR-TIM.md +
// OWNER-ANSWERS-1114-2035.md): Library/Listing review -> Marketplace; Key inventory/Lockbox -> The Belt;
// Logs/Log Streaming/Permissions/Permission requests -> The Guard; Licence Review -> Lion;
// License Required -> Counting House (plans-and-spending product, owner 11-14 ~10:00 PM CT);
// maintenance -> whole system, no product key (allowlist below).
const SLICE = [
  { page: 'key-inventory', key: 'lockbox', src: 'The Belt (owner approved 11-14)', literal: 'Key inventory', attrs: 10, digest: '00ac20bbbfbe' },
  { page: 'library', key: 'marketplace', src: 'Kansas (owner approved 11-14)', literal: 'Library', attrs: 0, digest: '68b329da9893' },
  // licence-review: page denied on CE (manifest licence-review deny) — not in extract, CE test skips it
  { page: 'license-required', key: 'counting-house', src: 'Counting House (owner approved 11-14)', literal: 'License Required', attrs: 8, digest: '2d30726c6f99' },
  { page: 'listing-review', key: 'marketplace', src: 'Kansas (owner approved 11-14)', literal: 'Listing review', attrs: 10, digest: '352ce3741f4b' },
  { page: 'lockbox-approval', key: 'lockbox', src: 'The Belt (lockbox)', literal: 'Lockbox access requests', attrs: 15, digest: 'b28417c7f8c6' },
  { page: 'log-streams', key: 'security', src: 'The Guard (owner approved 11-14)', literal: 'Log Streaming (SIEM)', attrs: 25, digest: 'a815f4a5ca9c' },
  { page: 'logs', key: 'logs', src: 'Records (Tim named the Logs sidebar entry 11-17; the h1 echoes it, was The Guard / security from 11-14)', literal: 'Logs', attrs: 33, digest: '84f53e29f180' },
  { page: 'maintenance', wholeSystem: true, src: 'whole system, no product key (owner approved 11-14)', literal: "We're upgrading", attrs: 2, digest: 'd971ceeb020e' },
  { page: 'marketplace', key: 'marketplace', src: 'Kansas', literal: 'Marketplace', attrs: 105, digest: 'bbc8abd842d9' },
];

// Pages the CE extract does not ship (open-core/manifest.yaml denies them). A row named here is skipped
// ONLY when its page file is really absent (positive absence witness); when the file is present (paid, private
// tree) the row is checked in full. Any page not named here that is missing still fails.
const CE_DENIED_PAGES = ['key-inventory', 'library', 'licence-review', 'license-required', 'listing-review', 'lockbox-approval', 'marketplace'];
// Pages CE ships but scrubs (open-core scrub rules rewrite a link / drop an element), so the CE id/href digest
// differs from paid. A row named here accepts the paid pair OR this CE pair.
const CE_SCRUBBED_ATTRS = { logs: { attrs: 32, digest: '247f6794795b' } };

function absentInCe(page) {
  return CE_DENIED_PAGES.includes(page) && !fs.existsSync(path.join(pagesDir, page + '.html'));
}

function readPage(name) {
  return fs.readFileSync(path.join(pagesDir, name + '.html'), 'utf8');
}

function h1Of(html) {
  const m = html.match(/<h1\b[^>]*>([^<]*)<\/h1>/);
  return m ? m : null;
}

function keyOf(html) {
  const tag = html.match(/<h1\b[^>]*>/);
  if (!tag) return null;
  const k = tag[0].match(/\sdata-theme-key="([^"]+)"/);
  return k ? k[1] : null;
}

function attrDigest(html) {
  const attrs = html.match(/(?:id|href)="[^"]*"/g) || [];
  const text = attrs.join('\n') + '\n';
  return { count: attrs.length, digest: crypto.createHash('md5').update(text).digest('hex').slice(0, 12) };
}

for (const row of SLICE) {
  test(`${row.page}.html h1 keyed (${row.key || 'UNMAPPED'}), literal and id/href unchanged`, () => {
    if (absentInCe(row.page)) {
      assert.ok(!fs.existsSync(path.join(pagesDir, row.page + '.html')), `${row.page}.html must be absent`);
      console.log(`SKIP-CE-DENIED ${row.page}`);
      return;
    }
    const html = readPage(row.page);

    const h1 = h1Of(html);
    assert.ok(h1, `${row.page}.html has no h1`);
    assert.strictEqual(h1[1], row.literal, `${row.page}.html h1 literal changed (flag-off must stay today literal)`);

    if (row.wholeSystem) {
      // Allowlist: the maintenance page belongs to the whole system (OZ), not to a product.
      // owner approved 11-14: no product key on this page. It must stay UNKEYED.
      assert.strictEqual(keyOf(html), null, `${row.page}.html carries a product key; the maintenance page is whole-system (no product)`);
      return;
    }
    const got = keyOf(html);
    assert.strictEqual(got, row.key, `${row.page}.html h1 data-theme-key is ${got === null ? 'MISSING' : JSON.stringify(got)}, expected ${JSON.stringify(row.key)} (${row.src})`);

    const d = attrDigest(html);
    const ce = CE_SCRUBBED_ATTRS[row.page];
    const isCe = ce && d.count === ce.attrs && d.digest === ce.digest;
    if (!isCe) {
      assert.strictEqual(d.count, row.attrs, `${row.page}.html id/href attribute count changed`);
      assert.strictEqual(d.digest, row.digest, `${row.page}.html id/href attribute digest changed`);
    }
  });
}

test('slice inventory: every page file in the slice exists', () => {
  for (const row of SLICE) {
    if (absentInCe(row.page)) continue;
    assert.ok(fs.existsSync(path.join(pagesDir, row.page + '.html')), `${row.page}.html missing`);
  }
});
