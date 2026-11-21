// BUG.B93 (owner report): "Not every sidebar click highlights the page you
// are on." Root cause: navigateToPage() (js/ui.js) set the sidebar's
// active-link class by comparing each `.nav-link`'s raw `href` against the
// RAW incoming hash — which still carries any query string on deep links
// (#files?hash=.., #logs?tab=..), so it never matched a plain `href="#files"`
// link. Several "old bookmark" redirect branches (#gpu-pool -> #system,
// #logs?tab=permissions -> #compliance-center, #admin-resource-monitor /
// #workers / #webhooks -> #system, #tenant-registry -> #files) also rewrote
// window.location.hash to a DIFFERENT page without re-running the highlight
// logic (there is no hashchange listener; only popstate), leaving the
// sidebar showing no active item (or the wrong one) after landing.
//
// Fix: a single setActiveSidebarItem(hash) helper, called from the one
// place every page load goes through (navigateToPage) using the SAME
// stripped page key every dispatch branch matches on, plus explicit calls
// from each internal redirect branch with the real destination hash.
//
// Checks:
//   1) js/ui.js parses as a valid ES module.
//   2) setActiveSidebarItem() is defined once, and navigateToPage calls it
//      with the stripped page key ('#' + targetPageId), not the raw hash.
//   3) The old buggy raw-hash comparison is gone (regression guard).
//   4) Every sidebar entry's page key (href="#xxx") in dashboard-shell.html
//      maps to a page the loader knows: a `targetPageId === 'xxx'` dispatch
//      branch, a pageFileMap alias, or a pages/xxx.html file it can fetch
//      directly — logging any key that matches none of those.
//   5) Each of the known internal "old bookmark" redirect branches calls
//      setActiveSidebarItem with its real destination hash before returning.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const uiSrc = read('js/ui.js');
const shellSrc = read('pages/dashboard-shell.html');

// --- 1) ES module syntax check on js/ui.js ---------------------------------
let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: uiSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr,
    `js/ui.js: not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) setActiveSidebarItem defined + called with the stripped page key --
assert.ok(/function\s+setActiveSidebarItem\s*\(/.test(uiSrc),
    'js/ui.js: must define a single setActiveSidebarItem(hash) helper');

const navStart = uiSrc.indexOf('export async function navigateToPage(hash) {');
const helperStart = uiSrc.indexOf('function setActiveSidebarItem(hash) {');
assert.ok(navStart !== -1 && helperStart !== -1 && helperStart > navStart,
    'js/ui.js: could not isolate navigateToPage() body / setActiveSidebarItem helper');
const navBody = uiSrc.slice(navStart, helperStart);

assert.ok(navBody.includes("setActiveSidebarItem('#' + targetPageId)"),
    "navigateToPage: must highlight by the stripped page key (\"'#' + targetPageId\"), " +
    "not the raw incoming hash (which still carries any ?query on deep links)");

// --- 3) Regression guard: the old raw-hash comparison must be gone --------
assert.ok(!uiSrc.includes('getAttribute("href") === targetHash'),
    'js/ui.js: old raw-hash active-link comparison must be removed (BUG.B93 regression)');

// --- 4) Every sidebar entry's page key must map to a page the loader knows
const navLinkRe = /<a\s+href="#([a-zA-Z0-9_-]+)"[^>]*class="[^"]*\bnav-link\b/g;
const keys = new Set();
let m;
while ((m = navLinkRe.exec(shellSrc)) !== null) {
    keys.add(m[1]);
}
// Edition-aware (FIX-CE-DASH-SWEEP-B): the CE cut denies paid pages whose
// sidebar rows the shell carries (manifest deny rows + the nav-row scrub rows),
// so the CE shell has fewer distinct page keys (15 after the denied-page nav rows were scrubbed: denied-page nav rows and earlier ones) than the paid shell (25).
// Detect the edition by a paid-only module and use the matching minimum.
const paidTree = fs.existsSync(path.join(ROOT, 'js', 'seller.js'));
const minKeys = paidTree ? 25 : 15;
assert.ok(keys.size >= minKeys, `expected >=${minKeys} distinct sidebar page keys, found ${keys.size}`);

// Links whose destination is an external redirect handled by its own
// dedicated click listener (not a dashboard-rendered page) — not a page
// the loader fetches/dispatches on, by design.
const EXTERNAL_REDIRECT_KEYS = new Set(['workflows']);

const unknownKeys = [];
for (const key of keys) {
    if (EXTERNAL_REDIRECT_KEYS.has(key)) continue;
    const hasBranch = uiSrc.includes(`targetPageId === '${key}'`);
    const hasPageFileMapAlias = uiSrc.includes(`'${key}':`);
    const hasPageFile = fs.existsSync(path.join(ROOT, 'pages', `${key}.html`));
    if (!hasBranch && !hasPageFileMapAlias && !hasPageFile) {
        unknownKeys.push(key);
    }
}
if (unknownKeys.length) {
    console.log('sidebar entries with NO matching dispatch branch/alias/page file:',
        unknownKeys.join(', '));
}
assert.deepStrictEqual(unknownKeys, [],
    `sidebar keys unknown to the loader: ${unknownKeys.join(', ')}`);

// --- 5) Each internal redirect branch highlights its real destination -----
const redirectChecks = [
    { from: "window.location.hash = 'compliance-center';", to: "setActiveSidebarItem('#compliance-center')" },
    { from: "window.location.search = 'tab=settings&sub=gpu-pool';", to: "setActiveSidebarItem('#system')" },
    // Edition-aware (FIX-CE-DASH-SWEEP-B2): the admin-resource-monitor dispatch
    // branch (the 'tab=settings&sub=usage' redirect) is denied/scrubbed away on
    // the CE cut, so its marker only exists on the paid tree.
    ...(paidTree ? [{ from: "window.location.search = 'tab=settings&sub=usage';", to: "setActiveSidebarItem('#system')" }] : []),
    { from: "window.location.search = 'tab=workers';", to: "setActiveSidebarItem('#system')" },
    { from: "window.location.search = 'tab=apps';", to: "setActiveSidebarItem('#system')" },
    { from: "window.location.search = 'tab=tenant-registry';", to: "setActiveSidebarItem('#files')" },
];
for (const { from, to } of redirectChecks) {
    const idx = uiSrc.indexOf(from);
    assert.ok(idx !== -1, `js/ui.js: expected redirect marker not found: ${from}`);
    const window_ = uiSrc.slice(idx, idx + 500);
    assert.ok(window_.includes(to),
        `js/ui.js: redirect at "${from}" must call ${to} before its return`);
}

console.log('sidebar-active-highlight: all checks passed -> OK');
if (unknownKeys.length === 0) {
    console.log('(no sidebar keys with zero mapping found — "workflows" is the one ' +
        'known external-redirect exception, by design)');
}
