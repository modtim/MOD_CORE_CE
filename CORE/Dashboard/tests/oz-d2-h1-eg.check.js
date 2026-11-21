// OZ-D2-H1-EG guard: e-g pages h1 theming is additive (same rule as OZ-D1..D6).
// Static source-text asserts; no browser, no network.
//
//   node CORE/Dashboard/tests/oz-d2-h1-eg.check.js
//
// Rule: a mapped page carries its first h1 with data-theme-key="<key>", and its
// visible literal is today's literal (flag OFF renders byte for byte).
//
// Mapping (Tim picks 3 and 4, OWNER-ANSWERS-1114-2035.md):
//   files.html              -> 'files'               (h1 "Data", keyed before this job)
//   free-trials.html        -> 'free-trials'         (First Steps; h1 "Free Trial Management")
//   geographic-pricing.html -> 'geographic-pricing'  (Kingdom Map; h1 "Geographic Pricing Management")
// Left LITERAL, pending Tim's names (no key chosen here):
//   elevation-requests.html          h1 "Elevation Requests"
//   failure-prediction-calibration.html  no h1 at HEAD (asserted: still zero h1)
//   gpu-pool.html                    h1 "GPU Pool"
//   files.html second h1             "Tenant Registry" (unmapped; asserted unkeyed)
// Baselines at HEAD (line counts, same as grep -c): free-trials id= 24 / href= 0,
// geographic-pricing id= 44 / href= 0. The h1 edit adds no id= or href= text.
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const pagesDir = path.join(root, 'pages');
const names = fs.readFileSync(path.join(root, 'js', 'product-names.js'), 'utf8');

// [page, product key, today's first h1 literal at HEAD]
const MAPPED = [
  ['files.html', 'files', 'Data'],
  ['free-trials.html', 'free-trials', 'Free Trial Management'],
  ['geographic-pricing.html', 'geographic-pricing', 'Geographic Pricing Management'],
];

// [page, id= line count, href= line count] at HEAD (the two edited pages)
const BASELINE = [
  ['free-trials.html', 24, 0],
  ['geographic-pricing.html', 44, 0],
];

// Slice pages with no h1 at HEAD: exempt from the one-h1 rule, must stay h1-free.
const NO_H1 = ['failure-prediction-calibration.html'];

const SLICE_PAGES = fs
  .readdirSync(pagesDir)
  .filter((f) => /^[e-g].*\.html$/.test(f))
  .sort();

const failures = [];
function check(page, fn) {
  try {
    fn();
  } catch (err) {
    failures.push(`${page}: ${err.message}`);
  }
}

function read(page) {
  return fs.readFileSync(path.join(pagesDir, page), 'utf8');
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

// 1. Every e-g page has exactly one h1 (files.html two, NO_H1 pages zero).
let h1Total = 0;
for (const page of SLICE_PAGES) {
  check(page, () => {
    const h1s = h1sOf(read(page));
    h1Total += h1s.length;
    if (NO_H1.includes(page)) {
      assert.strictEqual(h1s.length, 0, 'h1 count changed from 0');
      return;
    }
    const want = page === 'files.html' ? 2 : 1;
    assert.strictEqual(h1s.length, want, `expected ${want} h1, found ${h1s.length}`);
  });
}

// 2. Mapped pages: exactly one h1 with data-theme-key=<key>, and it is the first h1
//    with today's literal.
for (const [page, key, literal] of MAPPED) {
  check(page, () => {
    const h1s = h1sOf(read(page));
    const keyed = h1s.filter((h) => h.attrs.includes(`data-theme-key="${key}"`));
    assert.strictEqual(keyed.length, 1, `no h1 with data-theme-key="${key}" (found ${keyed.length})`);
    assert.strictEqual(h1s[0].text, literal, `first h1 literal changed (expected "${literal}")`);
    assert.ok(h1s[0].attrs.includes(`data-theme-key="${key}"`), 'key is not on the first h1');
  });
}

// 3. files.html second h1 (Tenant Registry) stays literal and unkeyed.
check('files.html', () => {
  const h1s = h1sOf(read('files.html'));
  assert.strictEqual(h1s[1].text, 'Tenant Registry', 'second h1 literal changed');
  assert.ok(!h1s[1].attrs.includes('data-theme-key'), 'second h1 must stay unkeyed');
});

// 4. Every other e-g page: no h1 carries a data-theme-key.
for (const page of SLICE_PAGES) {
  if (MAPPED.some(([p]) => p === page)) continue;
  check(page, () => {
    const keyed = h1sOf(read(page)).filter((h) => h.attrs.includes('data-theme-key'));
    assert.strictEqual(keyed.length, 0, `unmapped page has ${keyed.length} keyed h1(s)`);
  });
}

// 5. Baselines: the edited pages keep their id= and href= line counts.
for (const [page, idLines, hrefLines] of BASELINE) {
  check(page, () => {
    const lines = read(page).split('\n');
    const ids = lines.filter((l) => l.includes('id=')).length;
    const hrefs = lines.filter((l) => l.includes('href=')).length;
    assert.strictEqual(ids, idLines, `id= line count ${ids} != baseline ${idLines}`);
    assert.strictEqual(hrefs, hrefLines, `href= line count ${hrefs} != baseline ${hrefLines}`);
  });
}

// 6. OZ-D2-P2: literal per the parked default (no key, text unchanged).
// failure-prediction-calibration has no h1 (asserted in section 2 as zero h1).
// OZ-D2-P2: literal per the parked default
const PARKED_LITERAL = [
  ['elevation-requests.html', 'Elevation Requests'],
  ['gpu-pool.html', 'GPU Pool'],
];
for (const [page, literal] of PARKED_LITERAL) {
  check(page, () => {
    const h1s = h1sOf(read(page));
    assert.ok(h1s.length >= 1, 'no h1 found');
    assert.strictEqual(h1s[0].text, literal, `first h1 literal changed (expected "${literal}")`);
    assert.ok(!h1s[0].attrs.includes('data-theme-key'), 'parked h1 must carry no data-theme-key');
  });
}

if (failures.length) {
  console.log(`oz-d2-h1-eg.check.js: FAIL ${failures.length} check(s)`);
  for (const f of failures) console.log(`  - ${f}`);
  process.exitCode = 1;
} else {
  console.log(
    `oz-d2-h1-eg.check.js: ${MAPPED.length}/${MAPPED.length} mapped ok (e-g pages=${SLICE_PAGES.length} h1s=${h1Total})`,
  );
}
