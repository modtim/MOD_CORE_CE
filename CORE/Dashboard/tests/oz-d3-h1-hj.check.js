// OZ-D3-H1-HJ guard: h-j pages h1 theming is additive.
// - help.html h1 carries data-theme-key="help" (NAME-MAP: Help = Signpost, owner 11-14; Glinda is assistant-launcher, not Help).
// - Its visible literal stays "Help & Documentation" (MOD_THEME_NAMES off = today's literal).
// - product-names.js key 'help' has codename "Signpost" (read at run time).
// - id= and href= attribute list of help.html is unchanged (31 attrs, md5 prefix 9520f0075157).
'use strict';

const assert = require('node:assert');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const helpPath = path.join(root, 'pages', 'help.html');
const namesPath = path.join(root, 'js', 'product-names.js');

const help = fs.readFileSync(helpPath, 'utf8');
const names = fs.readFileSync(namesPath, 'utf8');

// 1. h1 with the theme key exists and its literal is unchanged.
const h1Match = help.match(/<h1 class="text-3xl font-bold text-gray-800" data-theme-key="help">([^<]*)<\/h1>/);
assert.ok(h1Match, 'help.html h1 with data-theme-key="help" missing');
assert.strictEqual(h1Match[1], 'Help & Documentation', 'h1 literal changed (flag-off must stay today literal)');

// 2. product-names.js label for the key is the one the h1 maps to.
const entry = names.match(/'help':\s*\{codename:\s*'([^']*)'/);
assert.ok(entry, "product-names.js has no 'help' entry");
assert.strictEqual(entry[1], 'Signpost', 'product-names.js help codename drifted from NAME-MAP');

// 3. id= and href= attributes unchanged (31 attrs, fixed digest).
const attrs = (help.match(/(?:id|href)="[^"]*"/g) || []).join('\n') + '\n';
const digest = crypto.createHash('md5').update(attrs).digest('hex').slice(0, 12);
assert.strictEqual(attrs.split('\n').length - 1, 31, 'id/href attribute count changed');
assert.strictEqual(digest, '9520f0075157', 'id/href attribute digest changed');

// 4. The theme attribute is the only addition on the h1 line.
const stripped = help.replace(' data-theme-key="help"', '');
assert.ok(
  stripped.includes('<h1 class="text-3xl font-bold text-gray-800">Help & Documentation</h1>'),
  'stripping the theme attribute must restore the original h1 line',
);

// 5. The four other h-j maintenance pages stay literal (owner rule: no product key
//    for maintenance pages, same as the OZ-D4 precedent). Each has one h1 whose full
//    line is unchanged from HEAD and carries no data-theme-key attribute.
const literalH1 = {
  'held-matches.html': '<h1 class="text-3xl font-bold text-gray-800">Held Matches</h1>',
  'invitations.html': '<h1 class="text-3xl font-bold text-gray-800 mb-6">Invitations</h1>',
  'ip-blocks.html': '<h1 class="text-3xl font-bold text-gray-800">Blocked IPs</h1>',
};
for (const [page, line] of Object.entries(literalH1)) {
  const src = fs.readFileSync(path.join(root, 'pages', page), 'utf8');
  assert.ok(src.includes(line), `${page}: h1 line missing or literal text changed`);
  const h1Tags = src.match(/<h1\b[^>]*>/g) || [];
  assert.ok(h1Tags.length >= 1, `${page}: no h1 found`);
  for (const tag of h1Tags) {
    assert.ok(!tag.includes('data-theme-key'), `${page}: h1 must stay literal (no data-theme-key)`);
  }
}

// 6. home.html: owner 11-17 (page main titles match the sidebar theme name): the h1 now carries
//    data-theme-key="home" on a span; the literal text stays as the flag-OFF fallback.
const homeSrc = fs.readFileSync(path.join(root, 'pages', 'home.html'), 'utf8');
assert.ok(
  homeSrc.includes('<h1 class="text-3xl font-bold text-gray-800 mb-6"><span data-theme-key="home">Welcome!</span></h1>'),
  'home.html: h1 must wrap the literal in <span data-theme-key="home">',
);

console.log('oz-d3-h1-hj.check.js: 6/6 ok (h1s=1 ids-changed=0 maintenance-h1s-literal=3 home-h1-themed=1)');
