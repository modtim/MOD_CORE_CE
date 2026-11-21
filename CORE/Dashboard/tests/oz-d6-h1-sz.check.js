// OZ-D6-H1-SZ guard: s-z pages h1 theming is additive (same rule as OZ-D1..D5).
// Static source-text asserts; no browser, no network.
//
//   node CORE/Dashboard/tests/oz-d6-h1-sz.check.js
//
// Rule: a page whose NAME-MAP surface names a product key in js/product-names.js
// carries exactly one h1 with data-theme-key="<key>", and its visible literal is
// today's literal (flag OFF renders byte for byte).
//
// Mapping (NAME-MAP.md rows, judgement where noted in the result file):
//   security.html, security-alerts.html, security-incidents.html,
//   security-lockdown.html  -> 'security'  (row "Security hub / audit")
//   secrets.html            -> 'lockbox'   (row "Secrets / Vault / OpenBao")
//   workers.html            -> 'workers'   (row "Workers")
//   stack-billing.html      -> 'stacks'    (row "Stacks", "stack pages"; judgement)
//   seasonal-pricing.html   -> 'seasonal-pricing' (Tim pick 5: Harvest Moon)
//   webhooks.html           -> 'webhooks'  (Tim pick 6: Message Bell)
//   settings.html           -> 'settings'  (Tim pick 8: Gearworks)
// Every other s-z page has no NAME-MAP key and is only checked for one h1.
//
// Non-zero exit when any mapped page lacks its key: the test is NOT weakened.
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const pagesDir = path.join(root, 'pages');
const namesPath = path.join(root, 'js', 'product-names.js');
const names = fs.readFileSync(namesPath, 'utf8');

// [page, product key, today's h1 literal at HEAD]
const MAPPED = [
  ['security.html', 'security', 'Security'],
  ['security-alerts.html', 'security', 'Security Alerts'],
  ['security-incidents.html', 'security', 'Security incidents'],
  ['security-lockdown.html', 'security', 'Sign-in lockdown'],
  ['secrets.html', 'lockbox', 'Secrets'],
  ['workers.html', 'workers', 'Worker Management'],
  ['stack-billing.html', 'stacks', 'Stack Billing'],
  ['seasonal-pricing.html', 'seasonal-pricing', 'Seasonal Pricing Management'],
  ['webhooks.html', 'webhooks', 'Outbound Webhooks'],
  ['settings.html', 'settings', 'Settings'],
];

// [page, id= count, href= count] measured at HEAD before PART 2 (attribute-only edit).
const COUNTS = [
  ['seasonal-pricing.html', 35, 0],
  ['webhooks.html', 7, 0],
  ['settings.html', 291, 1],
];

// Every s-z page on disk (names starting s..z, .html).
const SZ_PAGES = fs
  .readdirSync(pagesDir)
  .filter((f) => /^[s-z].*\.html$/.test(f))
  .sort();

const failures = [];
function check(page, fn) {
  try {
    fn();
  } catch (err) {
    failures.push(`${page}: ${err.message}`);
  }
}

// Collect every <h1 ...>text</h1> with its attribute string.
function h1sOf(html) {
  const out = [];
  const re = /<h1\b([^>]*)>([\s\S]*?)<\/h1>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    out.push({ attrs: m[1], text: m[2].trim() });
  }
  return out;
}

// 0. Every mapped key has a label in product-names.js (read at run time).
for (const [, key] of MAPPED) {
  const entry = names.match(new RegExp(`'${key}':\\s*\\{[^}]*label:\\s*'([^']*)'`));
  assert.ok(entry, `product-names.js has no '${key}' entry`);
}

// 1. Every s-z page with an h1 carries exactly one h1 (verify-email has none).
let h1Total = 0;
for (const page of SZ_PAGES) {
  check(page, () => {
    const h1s = h1sOf(fs.readFileSync(path.join(pagesDir, page), 'utf8'));
    h1Total += h1s.length;
    if (page === 'verify-email.html') {
      assert.strictEqual(h1s.length, 0, 'verify-email.html h1 count changed from 0');
      return;
    }
    assert.strictEqual(h1s.length, 1, `expected exactly 1 h1, found ${h1s.length}`);
  });
}

// 2. Mapped pages: one h1 with data-theme-key=<key>, literal unchanged.
for (const [page, key, literal] of MAPPED) {
  check(page, () => {
    const html = fs.readFileSync(path.join(pagesDir, page), 'utf8');
    const keyed = h1sOf(html).filter((h) => h.attrs.includes(`data-theme-key="${key}"`));
    assert.strictEqual(keyed.length, 1, `no h1 with data-theme-key="${key}" (found ${keyed.length})`);
    assert.strictEqual(keyed[0].text, literal, `h1 literal changed (expected "${literal}")`);
  });
}

// 3. id= / href= counts on the keyed pages unchanged (attribute-only edit).
for (const [page, ids, hrefs] of COUNTS) {
  check(page, () => {
    const html = fs.readFileSync(path.join(pagesDir, page), 'utf8');
    const nIds = (html.match(/id=/g) || []).length;
    const nHrefs = (html.match(/href=/g) || []).length;
    assert.strictEqual(nIds, ids, `id= count ${nIds}, expected ${ids}`);
    assert.strictEqual(nHrefs, hrefs, `href= count ${nHrefs}, expected ${hrefs}`);
  });
}

if (failures.length) {
  console.log(`oz-d6-h1-sz.check.js: FAIL ${failures.length}/${MAPPED.length} mapped pages missing key`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(`oz-d6-h1-sz.check.js: ${MAPPED.length}/${MAPPED.length} mapped ok (s-z pages=${SZ_PAGES.length} h1s=${h1Total})`);
}
