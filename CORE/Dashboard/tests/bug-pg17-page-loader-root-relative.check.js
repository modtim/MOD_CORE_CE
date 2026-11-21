'use strict';
// CE-PG17P2 (owner 10-30, PG17P follow-on): the Dashboard PAGE loader in
// js/ui.js fetched `pages/<file>.html` — page-relative. The hash router's
// replaceState (main.js:55/85/121, tab-state.js:19/42, settings.js:1692,
// library.js:231, analytics.js:107/172, subscription.js:17/24) can leave a
// nested pathname, so the relative URL resolved to /pages/pages/<file>.html
// and nginx's SPA fallback (nginx.conf:71) answered index.html with a 200.
// res.ok was true, so #page-content got the whole index document.
// PG17P already fixed the MODAL loader (fetch `/modals/...`); this is the
// same latent bug in the page loader: root-relative fetch + a cheap guard
// that treats a full document as a load failure through the existing error
// path.
//
//   node CORE/Dashboard/tests/bug-pg17-page-loader-root-relative.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const uiJs = fs.readFileSync(path.join(ROOT, 'js', 'ui.js'), 'utf8');

// --- 1) ui.js still parses as an ES module --------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: uiJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/ui.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the page loader's fetch is root-relative --------------------------
// The loader is the only fetch whose template head is `pages/`; it must now
// start with `/pages/` (a leading slash), never the bare page-relative form.
const pageFetches = [...uiJs.matchAll(/fetch\(\s*`([^`]*)\$\{/g)].map(m => m[1]);
const relPageFetches = pageFetches.filter(h => h === 'pages/');
assert.ok(relPageFetches.length === 0,
    'js/ui.js: no page loader fetch may stay page-relative (`pages/${...}`) — ' +
    `found ${relPageFetches.length}`);
const rootPageFetches = pageFetches.filter(h => h === '/pages/');
assert.ok(rootPageFetches.length === 1,
    'js/ui.js: exactly one root-relative page loader fetch (`/pages/${...}`) expected — ' +
    `found ${rootPageFetches.length}`);

// --- 3) no full-document guard (deliberately dropped) ---------------------
// 10 of the 106 real page files under pages/ ARE full documents (bulk-permissions,
// geographic-pricing, seasonal-pricing, status, maintenance, audit-logs, 404, 500,
// accept-invitation, accept-external-invite — each starts `<!DOCTYPE html>` then
// `<html lang="en">`), so an unanchored "contains <html" guard rejects REAL loader
// routes and paints "Error: Page not found". The root-relative fetch above is the
// fix; the heuristic guard is removed, so assert it is gone.
assert.ok(!/\/<html\[\\s>\]\/i\.test\(/.test(uiJs),
    'js/ui.js: the full-document guard must be gone (it rejected real full-doc pages)');

// --- 4) the loader's existing error path is untouched ---------------------
assert.ok(/Error: Page not found/.test(uiJs),
    'js/ui.js: the loader error panel must still be rendered by the existing catch');
assert.ok(/Could not load page: \$\{pageFile\}\.html/.test(uiJs),
    'js/ui.js: the loader must still throw its original not-found error');

// --- 5) the modal loader (PG17P) is unchanged -----------------------------
assert.ok(/fetch\(`\/modals\//.test(uiJs),
    'js/ui.js: the modal loader must stay root-relative (`/modals/${...}`) — PG17P edit intact');

console.log('bug-pg17-page-loader-root-relative: OK');
