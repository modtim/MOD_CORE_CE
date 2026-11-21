'use strict';
// CE-PAGEFETCH2 (owner 10-30, PG-17 bug class): the Dashboard page loaders in
// js/hub-compliance.js and js/billing.js fetched `pages/<file>.html` — page-
// relative. The hash router's replaceState (main.js:55/85/121, tab-state.js:19/42,
// settings.js:1692, library.js:231, analytics.js:107/172, subscription.js:17/24)
// carries window.location.pathname through, so a nested pathname makes the
// relative URL resolve to /pages/pages/<file>.html; nginx's SPA fallback
// (nginx.conf:71 `try_files $uri $uri/ /index.html`) then answers index.html
// with a 200, res.ok is true, and the panel gets the whole index document.
// PG-17 fixed the loader in js/ui.js; this is the same latent bug in the two
// per-child loaders. Fix: root-relative `/pages/...` at every fetch.
//
// CE-PAGEFETCH4 (same class, 3 more loaders): js/analytics.js:245 (apps),
// :3122 (workers), :3191 (gpu-pool), js/help.js:140 (per-child) and
// js/marketplace.js:112 (listing-review). Same fix, same gate.
//
//   node CORE/Dashboard/tests/bug-pagefetch2-root-relative.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

// `expected` is the count of root-relative page fetches the file must carry.
// null = derive it from the file (every page-fetch site must be root-relative),
// so the gate stays honest when a new loader lands.
const TARGETS = [
    { rel: 'js/hub-compliance.js', abs: path.join(ROOT, 'js', 'hub-compliance.js'), expected: 7, shape: 'chain' },
    { rel: 'js/billing.js', abs: path.join(ROOT, 'js', 'billing.js'), expected: 6, shape: 'try' },
    { rel: 'js/analytics.js', abs: path.join(ROOT, 'js', 'analytics.js'), expected: null, shape: 'mixed' },
    { rel: 'js/help.js', abs: path.join(ROOT, 'js', 'help.js'), expected: null, shape: 'chain' },
    { rel: 'js/marketplace.js', abs: path.join(ROOT, 'js', 'marketplace.js'), expected: null, shape: 'chain' },
];

for (const t of TARGETS) {
    // CE carve (FIX-CE-DASH-SWEEP-C): some targets (js/billing.js) are denied on
    // CE; a missing file has nothing to assert, so SKIP it and keep the rest live.
    if (!fs.existsSync(t.abs)) {
        console.log(`SKIP ${t.rel}: file absent (CE carve)`);
        continue;
    }
    const src = fs.readFileSync(t.abs, 'utf8');

    // --- 1) the file still parses as an ES module -------------------------
    let modErr = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'],
            { input: src, encoding: 'utf8' });
    } catch (e) {
        modErr = String(e && (e.stderr || e.message));
    }
    assert.ok(!modErr, `${t.rel}: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

    // --- 2) no page-relative fetch remains --------------------------------
    // A `fetch(` immediately followed by a quote that opens `pages/` (no
    // leading slash) is the bug. Comments may still narrate the old form
    // (hub-compliance.js:199), so scan a copy with whole-line comments
    // removed — only real call sites count.
    const code = src.split('\n').filter((line) => !/^\s*\/\//.test(line)).join('\n');
    const relHits = [...code.matchAll(/fetch\(\s*(['"`])pages\//g)];
    assert.ok(relHits.length === 0,
        `${t.rel}: no fetch may stay page-relative (fetch('pages/...') / fetch(\`pages/\${...}\` found ${relHits.length})`);

    // --- 3) every page fetch is root-relative ------------------------------
    const rootHits = [...code.matchAll(/fetch\(\s*(['"`])\/pages\//g)];
    const expected = t.expected === null
        ? [...code.matchAll(/fetch\(\s*(['"`])\/?pages\//g)].length   // derived: all page-fetch sites
        : t.expected;
    assert.ok(rootHits.length === expected,
        `${t.rel}: expected ${expected} root-relative page fetches (\`/pages/\`) — found ${rootHits.length}`);

    // --- 4) the existing error paths are untouched -------------------------
    // The loaders keep their original shapes — only the URL changed.
    if (t.shape === 'chain' || t.shape === 'mixed') {
        assert.ok(/\.then\(\(res\)\s*=>\s*res\.ok\s*\?\s*res\.text\(\)\s*:\s*null\)/.test(src),
            `${t.rel}: page loaders must keep the res.ok ? res.text() : null chain`);
    }
    if (t.shape === 'try' || t.shape === 'mixed') {
        assert.ok(/await\s+response\.text\(\)/.test(src),
            `${t.rel}: page loaders must keep the await response.text() body read`);
    }
    switch (t.rel) {
        case 'js/hub-compliance.js':
            assert.ok(/\.catch\(\(\)\s*=>/.test(src),
                'js/hub-compliance.js: page loaders must keep the fail-soft catch');
            break;
        case 'js/billing.js':
            assert.ok(/catch\s+\(error\)/.test(src),
                'js/billing.js: page loaders must keep the try/catch error panel path');
            break;
        case 'js/analytics.js':
            assert.ok(/document\.getElementById\('workers-content'\)\s*\.innerHTML\s*=\s*html/.test(src),
                'js/analytics.js: workers panel must still be filled from the fetched html');
            assert.ok(/gpuPoolContent\.innerHTML\s*=\s*html/.test(src),
                'js/analytics.js: gpu-pool panel must still be filled from the fetched html');
            break;
        case 'js/help.js':
            assert.ok(/fetch\(\s*`\s*\/pages\/\$\{_CHILD_PAGE\[name\]\}\.html`/.test(src),
                'js/help.js: per-child loader must fetch /pages/${_CHILD_PAGE[name]}.html root-relative');
            break;
        case 'js/marketplace.js':
            assert.ok(/getElementById\('main-panel-listing-review'\)/.test(src),
                'js/marketplace.js: listing-review panel id must be untouched');
            break;
    }
}

console.log('bug-pagefetch2-root-relative: OK');
