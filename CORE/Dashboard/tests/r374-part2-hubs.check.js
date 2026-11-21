// Node-runnable check for the 0b.2 REDO (R37.4 part 2, owner 10-08: "tabs put
// within the main tab", NOT sidebar segments) — the ex-nav-group pages become
// in-page tabs of their parent page. Modelled on
// r374-security-compliance-hubs.check.js (part 1: Security + Compliance).
//
//   node CORE/Dashboard/tests/r374-part2-hubs.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const shell = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'dashboard-shell.html'), 'utf8');
const ui = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

// =========================================================================
// USERS — six children folded into pages/users.html + js/users.js
// =========================================================================
const USERS_CHILDREN = ['held-matches', 'parental-consent', 'data-requests',
    'admin-dsar-requests', 'staff-register', 'consent-register'];

const usersHtml = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'users.html'), 'utf8');
const usersJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');

// --- sidebar: one Users entry, no segmented group, no child links ---------
assert.ok(/href="#users"[^>]*id="users-nav-link"/.test(shell),
    'users-nav-link must stay and point at #users');
assert.ok(!/id="nav-group-users"/.test(shell),
    'nav-group-users segmented group must be gone (owner 10-08)');
for (const child of USERS_CHILDREN) {
    assert.ok(!new RegExp(`id="${child}-nav-link"`).test(shell),
        `${child} must not keep its own sidebar link`);
}
// The nav-id trap: the DSAR child's sidebar link was dsar-requests-nav-link
// (not admin-dsar-requests-nav-link) — assert the OLD id is gone too.
assert.ok(!/id="dsar-requests-nav-link"/.test(shell),
    'dsar-requests-nav-link (old nav id for #admin-dsar-requests) must be gone');

// --- users.html: tab strip + one panel per child --------------------------
{
    const nav = usersHtml.match(/<nav class="-mb-px flex">[\s\S]*?<\/nav>/);
    assert.ok(nav, 'Users page: <nav> tab bar not found');
    const order = [...nav[0].matchAll(/id="(main-tab-[a-z-]+)"/g)].map((m) => m[1]);
    const expected = ['main-tab-users', 'main-tab-invitations', 'main-tab-tenants',
        'main-tab-roles', 'main-tab-identity'].concat(
        USERS_CHILDREN.map((c) => `main-tab-${c}`));
    assert.deepStrictEqual(order, expected,
        'Users page: tab strip order/content mismatch');
    for (const c of USERS_CHILDREN) {
        assert.ok(new RegExp(`id="main-panel-${c}" class="main-panel`).test(usersHtml),
            `Users page: #main-panel-${c} missing`);
    }
}

// --- ui.js: every child hash aliases to the Users page --------------------
// CE carve (FIX-CE-DASH-SWEEP-C): a child whose page is denied on CE has no
// dispatch branch left in ui.js (the scrub removes the whole branch), so that
// child's dispatch assert SKIPs; the alias row and every present child stay
// live. On a tree that ships the page nothing changes.
const childShipped = (c) => fs.existsSync(
    path.join(__dirname, '..', 'pages', `${c}.html`));
for (const child of USERS_CHILDREN) {
    assert.ok(new RegExp(`'${child}':\\s*'users'[,\n]`).test(ui),
        `ui.js pageFileMap must alias #${child} -> users page`);
    if (childShipped(child)) {
        assert.ok(new RegExp(`initializeUsersPageListeners\\('${child}'\\)`).test(ui),
            `ui.js dispatch must land #${child} on the Users tab`);
    } else {
        console.log(`SKIP ui.js dispatch for #${child}: pages/${child}.html absent (CE carve)`);
    }
}

// --- users.js: role gates + CE carve + load-once fetch-verbatim wiring ----
assert.ok(/setActiveTab\(key\)/.test(usersJs),
    'users.js must persist the active tab as ?tab= via tab-state');
assert.ok(/getActiveTab\(\)/.test(usersJs),
    'users.js must restore the tab on a refresh (?tab=)');
assert.ok(/fetch\(`\/pages\/\$\{key\}\.html`\)/.test(usersJs),
    'users.js must fetch the child page markup verbatim');
assert.ok(/_childLoadedOnce/.test(usersJs),
    'users.js child init must run once per page mount');
assert.ok(/_childAllowed\(key\)/.test(usersJs),
    'users.js tabs must be role-gated');
// Gates copied 1:1 from the old per-child nav-link gates in ui.js.
for (const [child, gate] of [['held-matches', 'tenant'],
                             ['parental-consent', 'platform'],
                             ['data-requests', 'tenant'],
                             ['admin-dsar-requests', 'platform'],
                             ['staff-register', 'platform'],
                             ['consent-register', 'tenant']]) {
    assert.ok(new RegExp(`'${child}': '${gate}',`).test(usersJs),
        `users.js _CHILD_GATE for ${child} must be ${gate}`);
}
// parental-consent is CE-carved (manifest denies js/compliance-parental-consent.js): guarded import + hidden tab on CE.
assert.ok(/optionalPageModule\(\s*'\.\/compliance-parental-consent\.js',\s*'initializeParentalConsentPage'\s*\)/.test(usersJs),
    'users.js must guard the CE-denied parental-consent module');
assert.ok(/_ceEdition && \(?key === 'parental-consent'/.test(usersJs),
    'users.js must hide the parental-consent tab on CE');
// ui.js must not keep the old direct child inits (they now run in users.js).
for (const child of USERS_CHILDREN) {
    assert.ok(!new RegExp(`await initialize(?:HeldMatches|ParentalConsent|DataRequests|AdminDsarRequests|StaffRegister|ConsentRegister)Page\(\)`).test(ui),
        `ui.js must no longer init #${child} standalone`);
}

// =========================================================================
// HELP — ai-inventory + support-tickets folded into pages/help.html + js/help.js
// (help.js already ships its OWN tab strip on a third convention: help-tab /
// help-panel-<key>, localStorage 'help.activeTab' — NOT ?tab= — so the sweep's
// search-tail assertion does not apply; the children ride that convention.)
// =========================================================================
const HELP_CHILDREN = ['ai-inventory', 'support-tickets'];
const helpHtml = fs.readFileSync(path.join(__dirname, '..', 'pages', 'help.html'), 'utf8');
const helpJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'help.js'), 'utf8');

// --- sidebar: one Help entry, no segmented group, no child links ----------
assert.ok(/href="#help"[^>]*id="help-nav-link"/.test(shell),
    'help-nav-link must stay and point at #help');
assert.ok(!/id="nav-group-help"/.test(shell),
    'nav-group-help segmented group must be gone (owner 10-08)');
for (const child of HELP_CHILDREN) {
    assert.ok(!new RegExp(`id="${child}-nav-link"`).test(shell),
        `${child} must not keep its own sidebar link`);
}

// --- help page: tab buttons + one panel per child -------------------------
for (const child of HELP_CHILDREN) {
    assert.ok(new RegExp(`data-help-tab="${child}"`).test(helpHtml),
        `Help: #help-tab-${child} button missing`);
    assert.ok(new RegExp(`id="help-panel-${child}" class="help-panel`).test(helpHtml),
        `Help: #help-panel-${child} panel missing`);
}
assert.ok(/VALID_TABS = new Set\(\[[^\]]*'cards'[^\]]*\]/.test(helpJs),
    'help.js VALID_TABS must still include cards');
for (const child of HELP_CHILDREN) {
    assert.ok(new RegExp(`'${child}'`).test(helpJs.match(/VALID_TABS = new Set\(\[([\s\S]*?)\]\)/)[1]),
        `help.js VALID_TABS must include ${child}`);
}

// --- help.js: fetch-verbatim child markup + load-once + role gate ---------
assert.ok(/import \{ initializeAiInventoryPage \} from '\.\/ai-inventory\.js';/.test(helpJs),
    'help.js must import the AI-inventory initializer');
assert.ok(/import \{ initializeSupportTicketsPage \} from '\.\/support-tickets\.js';/.test(helpJs),
    'help.js must import the Support-tickets initializer');
assert.ok(/_childLoaded/.test(helpJs), 'help.js: child init must run once per page load');
assert.ok(/fetch\(`\/pages\/\$\{_CHILD_PAGE\[name\]\}\.html`\)/.test(helpJs),
    'help.js must fetch the child page markup verbatim');
assert.ok(/_TAB_GATE = \{[\s\S]*'ai-inventory': 'platform'/.test(helpJs),
    'help.js must gate the AI-inventory tab (platform admin only)');
assert.ok(/_TAB_GATE = \{[\s\S]*'support-tickets': 'paid'/.test(helpJs),
    'help.js must gate the Support tab to paid editions (routers/tenant CE-denied, manifest:1327; Q53=A)');
assert.ok(/_isPaidEdition = await isPaidEdition\(\)/.test(helpJs),
    'help.js must read paid edition via isPaidEdition() — fail-open on unknown');
assert.ok(/_isPlatformAdmin = !!currentUser\.is_platform_admin/.test(helpJs),
    'help.js must read the platform-admin role off /me');
assert.ok(/export async function initializeHelpPage\(initialTab = null\)/.test(helpJs),
    'help.js must accept an initialTab so old bookmarks land on the tab');

// --- ui.js: old hashes alias to the Help page + land on its tab ----------
for (const child of HELP_CHILDREN) {
    assert.ok(new RegExp(`'${child}':\\s*'help'[,\n]`).test(ui),
        `ui.js pageFileMap must alias #${child} -> help page`);
    assert.ok(new RegExp(`await initializeHelpPage\\('${child}'\\)`).test(ui),
        `ui.js dispatch must land #${child} on the Help tab`);
}
// the removed child nav-link lookups must be gone from ui.js
for (const child of HELP_CHILDREN) {
    assert.ok(!new RegExp(`getElementById\\('${child}-nav-link'\\)`).test(ui),
        `ui.js must no longer look up the removed ${child}-nav-link`);
}

// =========================================================================
// SYSTEM -> APPS — apps folded into the System/Analytics page (pages/analytics.html
// + js/analytics.js); owner Q6 10-08: the Apps tab sits IMMEDIATELY LEFT of
// Settings in the main-tab strip.
// =========================================================================
const analyticsHtml = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'analytics.html'), 'utf8');
const analyticsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'analytics.js'), 'utf8');

// --- sidebar: one System entry, no segmented group, no child link ---------
assert.ok(/href="#system"[^>]*id="system-nav-link"/.test(shell),
    'system-nav-link must stay and point at #system');
assert.ok(!/id="nav-group-system"/.test(shell),
    'nav-group-system segmented group must be gone (owner 10-08)');
assert.ok(!/id="apps-nav-link"/.test(shell),
    'apps-nav-link must not keep its own sidebar link');

// --- analytics.html: apps button immediately left of settings + panel -----
{
    const nav = analyticsHtml.match(/<nav class="-mb-px flex">[\s\S]*?<\/nav>/);
    assert.ok(nav, 'System page: <nav> tab bar not found');
    const order = [...nav[0].matchAll(/id="(main-tab-[a-z-]+)"/g)].map((m) => m[1]);
    const settingsIdx = order.indexOf('main-tab-settings');
    assert.ok(settingsIdx > 0, 'System page: main-tab-settings not found in the strip');
    assert.strictEqual(order[settingsIdx - 1], 'main-tab-apps',
        'System page: main-tab-apps must sit immediately left of main-tab-settings (owner Q6 10-08)');
    assert.ok(/id="main-panel-apps" class="main-panel/.test(analyticsHtml),
        'System page: #main-panel-apps missing');
}

// --- analytics.js: whitelist + fetch-verbatim + load-once + role gate -----
assert.ok(/import \{ initializeAppsPage \} from '\.\/apps\.js';/.test(analyticsJs),
    'analytics.js must import the Apps initializer');
assert.ok(/'apps'/.test(analyticsJs),
    'analytics.js must mention the apps tab key');
assert.ok(/\[[^\]]*'apps'[^\]]*\]\.includes\(wanted\)/.test(analyticsJs),
    'analytics.js tab-param whitelist must include apps');
assert.ok(/panelId === 'main-panel-apps'/.test(analyticsJs),
    'analytics.js switchToTab must have a main-panel-apps branch');
assert.ok(/fetch\('\/pages\/apps\.html'\)/.test(analyticsJs),
    'analytics.js must fetch the Apps page markup verbatim');
assert.ok(/_appsPanelLoaded/.test(analyticsJs),
    'analytics.js Apps init must run once per page mount');
assert.ok(/return initializeAppsPage\(\);/.test(analyticsJs),
    'analytics.js must run the Apps initializer against the panel');
// Gate copied 1:1 from the old apps-nav-link gate in ui.js (superadmin only), ANDed
// with the CE edition gate (Tim B3 item 6 11-06: Apps is not in the CE first launch).
assert.ok(/if \(isSuperAdmin && !_ceEditionAn\) \{\s*document\.getElementById\('main-tab-apps'\)/.test(analyticsJs),
    'analytics.js must reveal the apps tab only to superadmins, and only on paid editions');

// --- ui.js: old hash aliases to the System page + lands on its tab -------
assert.ok(/'apps':\s*'analytics'[,\n]/.test(ui),
    'ui.js pageFileMap must alias #apps -> analytics page');
assert.ok(/await initializeAnalyticsPage\('apps'\)/.test(ui),
    'ui.js dispatch must land #apps on the System tab');
assert.ok(!/getElementById\('apps-nav-link'\)/.test(ui),
    'ui.js must no longer look up the removed apps-nav-link');

// =========================================================================
// LOGS -> LOG STREAMS (log-streams tab) — folded into pages/logs.html + js/logs.js
// (logs.html uses the LOCAL `logs-tab-<key>` / `logs-panel-<key>` convention)
// =========================================================================
const logsHtml = fs.readFileSync(path.join(__dirname, '..', 'pages', 'logs.html'), 'utf8');
const logsJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'logs.js'), 'utf8');

// --- sidebar: one Logs entry, no segmented group, no child link -----------
assert.ok(/href="#logs"[^>]*id="logs-nav-link"/.test(shell),
    'logs-nav-link must stay and point at #logs');
assert.ok(!/id="nav-group-logs"/.test(shell),
    'nav-group-logs segmented group must be gone (owner 10-08)');
assert.ok(!/id="log-streams-nav-link"/.test(shell),
    'log-streams-nav-link must not keep its own sidebar link');

// --- logs.html: tab button + panel ---------------------------------------
{
    const nav = logsHtml.match(/<nav class="-mb-px flex">[\s\S]*?<\/nav>/);
    assert.ok(nav, 'Logs page: <nav> tab bar not found');
    const order = [...nav[0].matchAll(/id="(logs-tab-[a-z-]+)"/g)].map((m) => m[1]);
    assert.ok(order.includes('logs-tab-log-streams'),
        'Logs page: #logs-tab-log-streams button missing');
    assert.ok(/id="logs-panel-log-streams" class="logs-panel/.test(logsHtml),
        'Logs page: #logs-panel-log-streams panel missing');
}

// --- logs.js: import + call + initialTab + load-once + role gate ---------
assert.ok(/import \{ initializeLogStreamsPage \} from '\.\/log-streams\.js';/.test(logsJs),
    'logs.js must import the Log-streams initializer');
assert.ok(/return initializeLogStreamsPage\(\);/.test(logsJs),
    'logs.js must run the Log-streams initializer against the panel');
assert.ok(/export async function initializeLogsPage\(initialTab = null\)/.test(logsJs),
    'logs.js initializeLogsPage must accept an initialTab for deep links');
assert.ok(/initialTab \|\| getActiveTab\(\)/.test(logsJs),
    'logs.js must prefer the explicit initialTab over the ?tab= param');
assert.ok(/panelId === 'logs-panel-log-streams'/.test(logsJs),
    'logs.js tab wiring must have a logs-panel-log-streams branch');
assert.ok(/fetch\('\/pages\/log-streams\.html'\)/.test(logsJs),
    'logs.js must fetch the Log-streams page markup verbatim');
assert.ok(/_logStreamsPanelLoaded/.test(logsJs),
    'logs.js Log-streams init must run once per page mount');
// Gate copied 1:1 from the old log-streams-nav-link gate (super || platform admin).
assert.ok(/isPlatformAdmin\(currentUser\) \|\| currentUser\?\.parent_user_id === null/.test(logsJs),
    'logs.js must reveal the log-streams tab only to super/platform admins');

// --- ui.js: old hash aliases to the Logs page + lands on its tab ---------
assert.ok(/'log-streams':\s*'logs'[,\n]/.test(ui),
    'ui.js pageFileMap must alias #log-streams -> logs page');
assert.ok(/await initializeLogsPage\('log-streams'\)/.test(ui),
    'ui.js dispatch must land #log-streams on the Logs tab');
assert.ok(!/getElementById\('log-streams-nav-link'\)/.test(ui),
    'ui.js must no longer look up the removed log-streams-nav-link');

// =========================================================================
// MARKETPLACE -> LISTING REVIEW (listing-review tab) — folded into
// pages/marketplace.html + js/marketplace.js (main-tab convention)
// CE carve (FIX-CE-DASH-SWEEP-C): the Marketplace page + module are denied on
// CE, so this whole section SKIPs when they are absent (the ui.js alias row
// below is guarded too - the scrub removes it with the page).
// =========================================================================
if (!fs.existsSync(path.join(__dirname, '..', 'pages', 'marketplace.html'))
    || !fs.existsSync(path.join(__dirname, '..', 'js', 'marketplace.js'))) {
    console.log('SKIP Marketplace section: pages/marketplace.html + js/marketplace.js absent (CE carve)');
} else {
const marketplaceHtml = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'marketplace.html'), 'utf8');
const marketplaceJs = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'marketplace.js'), 'utf8');

// --- sidebar: one Marketplace entry, no segmented group, no child link ----
assert.ok(/href="#marketplace"[^>]*id="marketplace-nav-link"/.test(shell),
    'marketplace-nav-link must stay and point at #marketplace');
assert.ok(!/id="nav-group-marketplace"/.test(shell),
    'nav-group-marketplace segmented group must be gone (owner 10-08)');
assert.ok(!/id="listing-review-nav-link"/.test(shell),
    'listing-review-nav-link must not keep its own sidebar link');

// --- marketplace.html: tab button + panel ---------------------------------
{
    const nav = marketplaceHtml.match(/<nav class="-mb-px flex">[\s\S]*?<\/nav>/);
    assert.ok(nav, 'Marketplace page: <nav> tab bar not found');
    const order = [...nav[0].matchAll(/id="(main-tab-[a-z-]+)"/g)].map((m) => m[1]);
    assert.ok(order.includes('main-tab-listing-review'),
        'Marketplace page: #main-tab-listing-review button missing');
    assert.ok(/id="main-panel-listing-review" class="main-panel/.test(marketplaceHtml),
        'Marketplace page: #main-panel-listing-review panel missing');
}

// --- marketplace.js: TAB_KEYS + import + call + load-once + role gate -----
assert.ok(/TAB_KEYS = \[[^\]]*'listing-review'[^\]]*\]/.test(marketplaceJs),
    'marketplace.js TAB_KEYS must include listing-review');
assert.ok(/import \{ initializeListingReviewPage \} from '\.\/listing-review\.js';/.test(marketplaceJs),
    'marketplace.js must import the Listing-review initializer');
assert.ok(/return initializeListingReviewPage\(\);/.test(marketplaceJs),
    'marketplace.js must run the Listing-review initializer against the panel');
assert.ok(/tabName === 'listing-review'/.test(marketplaceJs),
    'marketplace.js switchToTab must have a listing-review branch');
assert.ok(/fetch\('\/pages\/listing-review\.html'\)/.test(marketplaceJs),
    'marketplace.js must fetch the Listing-review page markup verbatim');
assert.ok(/_listingReviewLoaded/.test(marketplaceJs),
    'marketplace.js Listing-review init must run once per page mount');
// Gate copied 1:1 from the old listing-review-nav-link gate (platform admin only).
assert.ok(/currentUser && isPlatformAdmin\(currentUser\)/.test(marketplaceJs),
    'marketplace.js must reveal the listing-review tab only to platform admins');

// --- ui.js: old hash aliases to the Marketplace page + lands on its tab --
assert.ok(/'listing-review':\s*'marketplace'[,\n]/.test(ui),
    'ui.js pageFileMap must alias #listing-review -> marketplace page');
assert.ok(/await initializeMarketplaceTabsPage\('listing-review'\)/.test(ui),
    'ui.js dispatch must land #listing-review on the Marketplace tab');
assert.ok(!/getElementById\('listing-review-nav-link'\)/.test(ui),
    'ui.js must no longer look up the removed listing-review-nav-link');
}

console.log('r374-part2-hubs.check.js: all assertions passed');
