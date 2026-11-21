'use strict';
// CE-PAGEFETCH6 (owner 10-30, same PG-17 bug class as CE-PAGEFETCH2/4/5): two
// more Dashboard page loaders fetched `pages/<file>.html` — page-relative. The
// hash router's replaceState carries window.location.pathname through, so on a
// nested pathname the relative URL resolves to /pages/pages/<file>.html and
// nginx's SPA fallback (nginx.conf `try_files $uri $uri/ /index.html`) answers
// index.html with a 200 — res.ok is true, so the panel gets the whole index
// document. Fix: root-relative `/pages/...` at every fetch.
// Sites: js/settings.js:7438 (tenant BYOK tab), js/users.js:51 (identity
//        subpage), js/users.js:169 (per-child). Counts are derived from the
//        file, never hardcoded, so the gate stays honest.
//
//   node CORE/Dashboard/tests/bug-pagefetch6-root-relative.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const TARGETS = ['js/settings.js', 'js/users.js'];

for (const rel of TARGETS) {
    const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');

    // Comments still narrate the old page-relative form, so only real call sites count.
    const code = src.split('\n').filter((line) => !/^\s*\/\//.test(line)).join('\n');

    const relHits = [...code.matchAll(/fetch\(\s*(['"`])pages\//g)];
    assert.ok(relHits.length === 0,
        `${rel}: no fetch may stay page-relative (found ${relHits.length})`);

    const expected = [...code.matchAll(/fetch\(\s*(['"`])\/?pages\//g)].length;
    const rootHits = [...code.matchAll(/fetch\(\s*(['"`])\/pages\//g)];
    assert.ok(rootHits.length === expected && expected > 0,
        `${rel}: expected ${expected} root-relative page fetches (/pages/) — found ${rootHits.length}`);

    console.log(`${rel}: ${rootHits.length} root-relative, 0 page-relative`);
}

console.log('bug-pagefetch6-root-relative: OK');
