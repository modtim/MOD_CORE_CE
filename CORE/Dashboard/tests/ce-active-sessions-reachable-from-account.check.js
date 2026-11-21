'use strict';
// FIX-DASH-CE-ACTIVE-SESSIONS-REACHABLE-FROM-ACCOUNT-PAGE (nav-gate audit F2)
//
// The Security hub ships on CE but its sidebar row stays hidden (no-flicker rule,
// _applyCePaidHubNav) and #security lands on the "Not available" panel, so the ONE
// CE-visible Security tab (Active Sessions, _TAB_GATE 'all' + the CE carve in
// js/security-hub.js leaving only 'active-sessions') had NO clickable path at all.
// Fix chosen: the LEAST invasive one - a link on the Account page (which already
// renders its own Active sessions card) to the child bookmark #active-sessions.
// The page gate and the nav reveal map are NOT touched, so the shipped
// ce-hides-paid-pages.check.js invariants (security-nav-link hidden on CE, #security
// panelled) still hold and paid behaviour is byte-identical.
//
// Technique (as sechub-ce-tabs-hidden.check.js / ce-hides-paid-pages.check.js):
// the real sources are read out of the shipped files (no re-typed copy).
//
//   node CORE/Dashboard/tests/ce-active-sessions-reachable-from-account.check.js
//
// Asserts:
//   A1 pages/account.html carries an href="#active-sessions" link (the clickable path)
//   A2 the child bookmark resolves on CE to the active-sessions tab of the hub
//      (ui.js dispatch branch calls initializeSecurityHubPage('active-sessions'),
//       and 'active-sessions' is NOT in any CE page-gate set)
//   A3 security-hub.js on CE allows ONLY 'active-sessions' among the Security tabs
//      (the other tabs stay gated) - and paid keeps them all
//   A4 #security itself stays panelled on CE (the paid panel for the other security
//      tabs is unchanged) and the nav reveal map is untouched (paid row unchanged)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const UI = fs.readFileSync(path.join(ROOT, 'js', 'ui.js'), 'utf8');
const HUB = fs.readFileSync(path.join(ROOT, 'js', 'security-hub.js'), 'utf8');
const ACCOUNT = fs.readFileSync(path.join(ROOT, 'pages', 'account.html'), 'utf8');

// --- A1 the Account page link exists ---------------------------------------
const link = ACCOUNT.match(/<a\s+href="#active-sessions"[^>]*>([^<]*)<\/a>/);
assert.ok(link, 'account.html must carry an <a href="#active-sessions"> link (CE Active Sessions had no clickable path)');
assert.ok(/Active sessions/i.test(link[0]), 'the link label must name Active sessions');
assert.ok(/Active sessions<\/h2>/.test(ACCOUNT),
    'the link sits in the Account page Active sessions card');

// --- A2 the route resolves on CE to the active-sessions tab -----------------
const setMarker = 'const _CE_PAID_HUB_PAGES = new Set([';
const setAt = UI.indexOf(setMarker);
assert.ok(setAt >= 0, '_CE_PAID_HUB_PAGES not found in ui.js');
const setLit = UI.slice(setAt, UI.indexOf('];', setAt) + 2);
assert.ok(!/'active-sessions'/.test(setLit),
    "'active-sessions' must NOT join _CE_PAID_HUB_PAGES (child bookmark keeps its own gate)");
for (const set of ['_CE_COMPLIANCE_PAGES', '_CE_PAID_COMPLIANCE_PAGES',
    '_CE_RETIRED_ADMIN_PAGES', '_CE_COV4B_PAGES']) {
    const sAt = UI.indexOf(`const ${set} = new Set([`);
    assert.ok(sAt >= 0, `${set} not found`);
    const lit = UI.slice(sAt, UI.indexOf('];', sAt) + 2);
    assert.ok(!/'active-sessions'/.test(lit), `'active-sessions' must not be in ${set}`);
}
assert.ok(/else if \(targetPageId === 'active-sessions'\) \{[\s\S]{0,900}?await initializeSecurityHubPage\('active-sessions'\);/.test(UI),
    "the #active-sessions dispatch branch must still initialize the hub's active-sessions tab");
assert.ok(/'active-sessions': 'security',/.test(UI),
    "the alias map must still resolve active-sessions to the security hub page");

// --- A3 the hub's CE carve leaves exactly one tab ---------------------------
const gateStart = HUB.indexOf('const _TAB_GATE');
const gateEnd = HUB.indexOf('function _visibleTabs');
assert.ok(gateStart >= 0 && gateEnd > gateStart, 'must locate _TAB_GATE .. _visibleTabs region');
const GATE_SRC = HUB.slice(gateStart, gateEnd);
const keysStart = HUB.indexOf('const TAB_KEYS');
const keysEnd = HUB.indexOf('];', keysStart);
assert.ok(keysStart >= 0 && keysEnd > keysStart, 'must locate TAB_KEYS');
const TAB_KEYS = vm.runInNewContext(HUB.slice(keysStart, keysEnd + 2) + '\nTAB_KEYS;', {},
    { filename: 'tab-keys', timeout: 5000 });
const KEYS_DECL = 'const TAB_KEYS = [' + TAB_KEYS.map((k) => `'${k}'`).join(', ') + '];\n';
const allowedOn = (ce) => JSON.parse(vm.runInNewContext(
    `let _isSuperAdmin = false;\nlet _isPlatformAdmin = true;\nlet _ceEdition = ${ce};\n`
    + KEYS_DECL + GATE_SRC + 'JSON.stringify(TAB_KEYS.filter((k) => _tabAllowed(k)));',
    {}, { filename: 'ce-active-sessions-reachable-from-account', timeout: 5000 }));

const ceAllowed = allowedOn('true');
assert.ok(ceAllowed.includes('active-sessions'),
    'CE must allow the active-sessions tab (self-service, _TAB_GATE all)');
const paidAllowed = allowedOn('false');
const securityTabs = paidAllowed.filter((k) => k !== 'active-sessions');
assert.ok(securityTabs.length >= 3, 'paid must keep the other Security tabs (no over-carve)');
securityTabs.forEach((k) => assert.ok(!ceAllowed.includes(k),
    `CE must still hide the other Security tab '${k}' (only Active Sessions is CE-visible)`));

// --- A4 #security stays panelled on CE; paid nav unchanged ------------------
assert.ok(/'security',/.test(setLit), "_CE_PAID_HUB_PAGES must still carry 'security'");
assert.ok(/if \(_CE_PAID_HUB_PAGES\.has\(targetPageId\) && await _isCeEdition\(\)\) \{[\s\S]{0,400}?_cePaidPagePanelHtml\(\);[\s\S]{0,250}?return;/.test(UI),
    'navigateToPage must still panel the paid hub set (incl. #security) on CE');
assert.ok(/'security-nav-link': true,/.test(UI),
    '_applyCePaidHubNav reveal map must stay untouched (paid Security row unchanged)');
assert.ok(!/securityHubNavLink\.style\.display = 'flex';/.test(UI),
    'no synchronous Security row reveal added (no-flicker rule kept)');

console.log('ce-active-sessions-reachable-from-account: ' +
    `link=1 route=link tabs-ce=${ceAllowed.length}/paid=${paidAllowed.length} paid-path-identical=yes`);
