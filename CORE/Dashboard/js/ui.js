// In Dashboard/js/ui.js

// --- Imports ---
import { initializeSettingsPage } from './settings.js';
import { initializeAccountPage } from './account.js';
import { me, getPlatformFeatures, isPaidEdition, fetchApi } from './api.js';
import { securityNotices } from './api.js';
// --- THIS IS THE FIX ---
// The 'handleForgotPassword' import has been removed.
import { handleLogin, handleSignup, handleLogout, handleOnboardingSubmit } from './auth.js';
// --- END FIX ---
import { initializeFilesPageListeners } from './files.js';
import { initializeAnalyticsPage } from './analytics.js';
// FX.160 p5 (owner R36.5=A) — Runbook links on the admin pages that perform
// the operation (System panels, Secrets, SSO, Key Inventory). Static import:
// runbook-links.js is a plain Dashboard file (not a compliance carve), so it
// ships in every edition and needs no optionalPageModule() guard.
import { _loadRunbookLinks } from './runbook-links.js';
// OZ-WIRING (11-14) — the runtime label hook (js/product-labels.js). The import
// is for its SIDE EFFECT: the module registers a DOMContentLoaded listener (the
// sidebar shell main.js mounts at boot) and a `mod:page-rendered` listener
// (the per-page nav below dispatches it). Inline <script> inside innerHTML
// never executes (ui.js:1050), so the applier must live in a module loaded
// once — never in a page file. Ships in every edition: no page ids, routes or
// manifest rows change; OFF is a byte-identical no-op (OZ-A3 guard).
import { applyThemeLabels } from './product-labels.js';
import { initializeLogsPage } from './logs.js';
import { clearActiveTab, getActiveTab } from './tab-state.js';
import { modA11y } from './modal.js';
import { initializePermissionsPage } from './permissions.js';
import { initializeUsersPageListeners, loadCustomPlans, loadTenantsForCustomPlans } from './users.js';
import { initializeInvitationsPageListeners } from './invitations.js';
import { initializeBulkPermissionsPage } from './bulk-permissions.js';
import { registerDashboardPages, registerCeGateAliases, _ceRegisteredRows, _ceDeniedPageIds } from './ui-manifest.js';
// CE-REGGATE-1 (design of record, commit 52870a35c0) — the CE gate is DERIVED from the
// page registry, not from a hard-coded deny-Set. Populate the registry at module load
// BEFORE the first navigateToPage so _ceDeniedPageId()/_ceDeniedPageIds() can read it.
// On CE the extract carries only the free rows (every paid:true registerPage row is
// scrubbed out), so an unregistered/denied id fails closed; on paid the predicate is
// never consulted (every CE gate short-circuits on await _isCeEdition() === false).
registerDashboardPages();
registerCeGateAliases();
import { initializeDockerRegistryPage } from './docker-registry.js';
// FX.162.p6 — the CE carve (open-core/manifest.yaml deny) strips
// CORE/Dashboard/js/compliance*.js, security-lockdown.js and outbound-connections.js
// from a Community Edition image. A STATIC `import ... from` of one of those
// files makes the whole Dashboard module graph fail to load on CE (ui.js would
// never run), so the five compliance page modules are loaded on demand through
// the utils.js optionalPageModule() guard at the exact spot their initializer
// used to be called: on CE the lookup yields null and the call site does
// nothing (navigateToPage's _CE_COMPLIANCE_PAGES gate already rendered the
// "Not available in MOD Community Edition" panel); on a paid build the lookup
// returns the very same module object the static import yielded, so the
// initializer runs with the same arguments in the same order.
import { optionalPageModule } from './utils.js';
// dashboard-marketplace-tabs — Library, My Listings (store-page) and the
// on-prem "Browse" remote-catalog preview are now tabs of one combined
// Marketplace page; js/marketplace.js owns their init calls (see the
// 'store-page'/'store-page'/'marketplace' branches below).
import { initializeHomePage } from './home.js';
import { initializeWorkersPageListeners } from './workers.js';
// BUG.B36c — no initializeWebhooksPage import: the Settings → Integrations →
// Outbound Webhooks sub-tab is retired, so ui.js never initializes the standalone
// webhooks page any more. The #webhooks bookmark redirects to the Apps tab's ONE
// "Webhooks" section (B36a/B36b). js/webhooks.js stays for js/apps.js's endpoint
// helpers (listEndpoints / createEndpoint / removeEndpoint).
import { initializeNotifications, cleanupNotifications } from './notifications.js';
import { loadSecrets } from './secrets.js';
import { initializeStorageConnectionsPage } from './storage-connections.js';
import { initializeAppsPage } from './apps.js';
import { initializeStaffRegisterPage } from './staff-register.js';
// R39.8 (owner 10-08) — Platform access review is now the 11th tab of the Security
// hub; js/security-hub.js owns its initializer, so ui.js no longer imports it.
import { initializeKeyInventoryPage } from './key-inventory.js';
// FX.162.p6 — denied on CE (manifest.yaml); loaded via optionalPageModule().
import { initializeSupportTicketsPage } from './support-tickets.js';
import { initializeAbuseQueuePage } from './abuse-queue.js';
// CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Listing Review page module
import { initializeSecurityIncidentsPage } from './security-incidents.js';
import { initializeAiInventoryPage } from './ai-inventory.js';
// FX.144 p5 (owner 10-07, Q157=A: compliance is paid-only) — the release-approval
// queue is a COMPLIANCE surface, so open-core/manifest.yaml denies
// CORE/Dashboard/js/release-approvals.js in the CE carve. A STATIC import of a
// denied file breaks the whole Dashboard module graph on CE (ui.js never runs),
// so this initializer is resolved through the same utils.js optionalPageModule()
// guard FX.162.p6 uses for the five compliance page modules: null on CE (the
// dispatch branch below does nothing — navigateToPage's CE gate already rendered
// the "Not available in MOD Community Edition" panel), the same module on paid.
import { initializeIpBlocksPage } from './ip-blocks.js';
import { initializeLogStreamsPage } from './log-streams.js';
import { initializeAlertRulesPage } from './alert-rules.js';
import { initializeTenantIdpPage } from './tenant-idp.js';
import { initializeSecurityAlertsPage } from './security-alerts.js';
import { initializeActiveSessionsPage } from './active-sessions.js';
import { initializeElevationRequestsPage } from './elevation-requests.js';
import { initializeHeldMatchesPage } from './held-matches.js';
import { initializeConsentRegisterPage } from './consent-register.js';
// FX.162.p6 — security-lockdown.js and compliance.js are denied on CE
// (open-core/manifest.yaml), so neither is imported statically; both are
// resolved through utils.js optionalPageModule() where navigateToPage()
// dispatches them (see the 'security-lockdown' / 'compliance' branches).
import { initializeChangelogPage } from './changelog.js';
import { initializeHelpPage } from './help.js';
// 0b.2 redo (R37.4, owner 10-08) — the Security and Compliance hub pages. Their
// child page modules stay imported above (the hubs import them too, harmlessly);
// the hub initializer is what a child hash's dispatch branch calls so the old
// bookmark lands on parent+tab.
// 0b.2 redo (R37.4, owner 10-08) — the Security hub page (its child modules
// stay imported above; the two CE-carved ones resolve through optionalPageModule
// inside the hub itself).
import { initializeSecurityHubPage } from './security-hub.js';
// js/hub-compliance.js (named to dodge the `compliance*.js` deny glob) is still
// resolved through optionalPageModule at each dispatch site.
// Tenant Registry is now a tab inside the Files (Data) page; its init
// is lazy-imported from files.js when the user switches to that tab.

// --- State ---
let userPermissions = new Set();

// --- Core UI Functions ---
export function showPage(pageToShow) {
    document.getElementById('login-page').style.display = 'none';
    document.getElementById('dashboard').style.display = 'none';
    document.getElementById('onboarding-page').style.display = 'none';
    document.getElementById('logged-out-page').style.display = 'none';
    document.getElementById('verify-email-page').style.display = 'none';

    const flexPages = ['login-page', 'onboarding-page', 'logged-out-page', 'verify-email-page'];
    const displayStyle = flexPages.includes(pageToShow.id) ? 'flex' : 'block';
    pageToShow.style.display = displayStyle;

    // Mirror the active page onto <body> so CSS can target it
    // reliably. Was previously trying to do this via
    // `:has(#dashboard:not(.hidden))`, but #dashboard starts life
    // with a `class="hidden"` and showPage above only overrides the
    // *inline display style* — it never strips the class — so the
    // selector never matched and the html/body overflow lock that
    // depended on it was a silent no-op. Toggling a body class here
    // is the simplest reliable signal.
    document.body.classList.toggle('dashboard-active', pageToShow.id === 'dashboard');
}

// Monotonic navigation id. Post-login restore (auth.js) and a hashchange/
// nav click can start overlapping navigateToPage() calls; without this the
// older call still swaps #page-content and re-inits the page after the newer
// one did, leaving duplicate fetches/POSTs and stale element references.
let _navGeneration = 0;

// 976d — the page file the LAST navigateToPage() run resolved to. Used to
// tell "the page we're leaving" from "the page we're landing on" so the
// hash-route / popstate / dashGoto paths (which never pass through the
// sidebar click handler that already calls clearActiveTab()) can drop a
// stale ?sub= left behind by the previous page. null until the first
// navigation, so a fresh bookmark (/?tab=settings&sub=gpu-pool#analytics)
// keeps its params.
let _lastPageFile = null;

// R-130 — per-route document titles. Keyed by the RESOLVED page file name
// (after the pageFileMap redirect above), so #store/#store share the
// Marketplace title and #system/#burst share the System title. Any page
// not listed falls back to its file name, title-cased.
const ROUTE_TITLES = {
    'home': 'Home',
    'files': 'Data',
    'analytics': 'System',
    'users': 'Users',
    'invitations': 'Invitations',
    'permissions': 'Permissions',
    'settings': 'Settings',
    'subscription': 'Subscription',
    'billing': 'Billing',
    'pricing': 'Pricing',
    'admin-plans': 'Plans',
    'tenant-idp': 'Tenant Identity Providers',
    'admin-scim': 'SCIM',
    'docker-registry': 'Docker Registry',
    'logs': 'Logs',
    'audit-logs': 'Audit Logs',
    'changelog': 'Changelog',
    'marketplace': 'Marketplace',
    'business': 'Business',
    'payment-settings': 'Payment Settings',
    'stack-billing': 'Stack Billing',
    'gpu-pool': 'GPU Pool',
    'workers': 'System',
    'webhooks': 'Webhooks',
    'secrets': 'Secrets',
    'storage': 'Storage',
    'apps': 'Apps',
    'data-requests': 'Data Requests',
    'access-reviews': 'Access Reviews',
    'consent-register': 'Likeness Consent Register',
    'abuse-queue': 'Abuse Queue',
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Listing Review title
    'security-incidents': 'Security Incidents',
    'licence-review': 'Not available',
    'ai-inventory': 'AI in This Platform',
    'release-approvals': 'Release Approvals',
    'operator-proposals': 'Operator Proposals',
    'ip-blocks': 'Blocked IPs',
    'log-streams': 'Log Streaming (SIEM)',
    'alert-rules': 'Security Alert Rules',
    'security-alerts': 'Security Alerts',
    'active-sessions': 'Active Sessions',
    'elevation-requests': 'Elevation Requests',
    'held-matches': 'Held Matches',
    'platform-access-review': 'Access Review',
    'key-inventory': 'Key Inventory',
    'outbound-connections': 'Outbound Connections',
    'support-tickets': 'Support',
    'lockbox-approval': 'Lockbox Access Requests',
    'security-lockdown': 'Sign-in Lockdown',
    'compliance': 'Compliance',
    'account': 'Account',
    'help': 'Help',
    'capture-devices': 'Capture Devices',
    'auditor-access': 'Auditor Access',
    'parental-consent': 'Parental Consent',
    'platform-settings': 'Platform Settings',
    'tenant-registry': 'Data',
    'bulk-permissions': 'Bulk Permissions',
    'permission-requests': 'Permission Requests',
    'model-cache': 'Model Cache',
    'status': 'Status',
    'onboarding': 'Onboarding',
    'license-required': 'License Required',
    'logged-out': 'Signed Out',
    'login': 'Sign In',
    'verify-email': 'Verify Email',
    'maintenance': 'Maintenance'
};

function _setRouteTitle(pageFile) {
    const title = ROUTE_TITLES[pageFile]
        || pageFile.replace(/-/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    document.title = `${title} — MOD`;
}

// --- FX.162.p3 (owner Q157: MOD CE must not do anything with compliance) ---
// ONE helper pair used by ALL FIVE compliance nav items AND all five direct
// page links (#compliance, #auditor-access, #parental-consent,
// #security-lockdown, #outbound-connections). The edition comes from the
// already-coalesced 30s /platform/features cache (js/api.js:321) — the
// `entitlements.edition` field main.py derives from utils.paid_import
// (sentinel services/wizard_ml, which open-core/manifest.yaml denies
// wholesale, so the CE carve strips it). FAIL-CLOSED (rc20 failopen-a): an
// unreadable/errored payload counts as CE, so it can never reveal a page the
// CE carve denies. Only a positive 'paid' edition (saas/pro) shows it.
const _CE_COMPLIANCE_PAGES = new Set([
    'compliance',
    'auditor-access',
    'parental-consent',
    'security-lockdown',
    'outbound-connections',
]);

function _isCeEdition() {
    // FAIL-CLOSED (rc20 failopen-a): unknown counts as CE, so an errored
    // entitlements payload can never reveal a CE-denied surface. Only a
    // POSITIVE 'paid' edition (saas/pro) counts as paid; the FAILURE stub
    // (edition "ce" + error) and any unreadable/missing block are unknown.
    return _ceEditionState().then((s) => s !== 'paid');
}

function _ceEditionState() {
    // 'ce' | 'paid' | 'unknown'. Positive reports only; anything else is unknown.
    return getPlatformFeatures().then((features) => {
        const ent = features && features.entitlements;
        if (!ent || ent.error) return 'unknown';
        if (ent.edition === 'ce') return 'ce';
        return (ent.edition === 'saas' || ent.edition === 'pro') ? 'paid' : 'unknown';
    }).catch(() => 'unknown');
}

function _hideCeComplianceNav() {
    // ONE helper hiding ALL FIVE compliance nav items on CE (the per-item
    // blocks in updateUIVisibility keep their role gates; this runs after
    // them and only ever hides, so a CE build can never reveal one). Same
    // predicate as navigateToPage's page gate (_isCeEdition), so the nav
    // and the direct-URL panel can never disagree.
    return _isCeEdition().then((isCe) => {
        if (!isCe) return;
        for (const pageId of _CE_COMPLIANCE_PAGES) {
            const link = document.getElementById(`${pageId}-nav-link`);
            if (link) link.style.display = 'none';
        }
        _syncNavGroupHeadings();
    });
}

function _ceCompliancePanelHtml() {
    // Short panel shown for a direct URL to a compliance page on CE. In a
    // CE build the page file itself is denied by open-core/manifest.yaml,
    // so without this the visitor would land on the generic "Page not
    // found" error instead of an explanation.
    return `<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900">
            <h1 class="text-xl font-semibold text-amber-900">Not available in MOD Community Edition</h1>
            <p class="text-sm">Compliance profiles, auditor access, parental consent, sign-in lockdown and outbound-connection controls are not available in this edition. MOD Community Edition ships the audit log and no other compliance surfaces, jobs or reports.</p>
        </div>`;
}

function _cePaidPagePanelHtml() {
    // Short panel for non-compliance paid pages (business, payments-admin,
    // admin-sso, SCIM, pricing, mpc-sessions, …) visited by hash on CE.
    // Replaces _ceCompliancePanelHtml on those pages so the message is about
    // "this page" rather than about compliance. Static string only.
    return `<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900">
            <h1 class="text-xl font-semibold text-amber-900">Not available in MOD Community Edition</h1>
            <p class="text-sm">This page is not available in this edition.</p>
            <a href="#home" class="mt-2 text-sm underline hover:no-underline">Back to Dashboard</a>
        </div>`;
}

// FIX-CE-TRIALS-OFF (owner 11-15 ~8:15 PM CT popup answer "Hide in paid too"): trials are OFF
// in the launch build for EVERYONE (CE AND paid). Neutral panel for the #free-trials hash
// route - edition-neutral wording, so no paid-tier text ships and no CE-only claim is made
// on a paid tree. Static string only.
function _trialsOffPanelHtml() {
    return `<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900">
            <h1 class="text-xl font-semibold text-amber-900">Trials are off in this build</h1>
            <p class="text-sm">This build does not offer the trials page. Nothing was removed from your system.</p>
            <a href="#home" class="mt-2 text-sm underline hover:no-underline">Back to Dashboard</a>
        </div>`;
}

// --- FX.144 p5 (owner 10-07, Q157=A: compliance is PAID-only) ---
// The release-approval queue (production change control + the emergency/review
// path) is a COMPLIANCE feature, so it is carved out of CE exactly like the five
// FX.162.p3 compliance pages: SAME edition predicate (_isCeEdition off the
// already-coalesced /platform/features entitlements.edition line, fail-closed (rc20 failopen-a)),
// its own page set + panel wording, its own nav-hide helper called from
// updateUIVisibility right after _hideCeComplianceNav(), and its page/js files
// denied in open-core/manifest.yaml (so the initializer is loaded through
// optionalPageModule above instead of a static import).
const _CE_PAID_COMPLIANCE_PAGES = new Set([
    'release-approvals',
    'licence-review',
    'operator-proposals',
    'key-inventory',
    'lockbox-approval',
    'pending-approvals',
    'consent-register',
]);

// 2400LR-pg1 — the four standalone children of the cov4b hub tabs (capacity-report,
// adversary-findings, failure-prediction-calibration, mpc-sessions). Their page files
// ship on CE but the backing routers are denied (open-core/manifest.yaml :1228, :1225,
// :1244, :1253), and hub-compliance.js hides the TABS (_CE_COV4B_TABS) only. A direct
// #hash visit gets the same "Not available in MOD Community Edition" panel as the
// compliance pages (same _isCeEdition predicate, fail-closed rc20 failopen-a). Own set: the literals
// above stay byte-identical.
const _CE_COV4B_PAGES = new Set([
    // FIX-DASH-CE-DEAD-NAV-3: the Nodes page has no CE route (GET /nodes/admin/system
    // is denied), so a #admin-nodes deep link gets the generic "Not available in MOD
    // Community Edition" panel and returns before the fetch — same shape as
    // admin-dsar-requests. The sidebar row is already display:none (dashboard-shell.html).
    'admin-nodes',
    // 2400LR-pg6a: pricing.py :1386 and GET /platform/dsar-requests (:1240) are denied;
    // these three ship with hash routes only (no sidebar link / hidden tab).
    'admin-dsar-requests',
    'pricing-config',
    'stack-billing',
    'capacity-report',
    'adversary-findings',
    'failure-prediction-calibration',
    'mpc-sessions',
    // 2400LR-pg2: routers sso_*.py :1285, billing.py :1209, platform_analytics.py :1216,
    // capture_field_client.py :1664 are denied; these pages shipped and 404'd.
    'admin-sso',
    'admin-sso-domains',
    'tenant-idp',
    'business',
    'payments-admin',
    'capture-devices',
    // 2400LR-pg4: scim_v2.py :1286, admin_scim_*.py :1287 (+ scim_*.py :1435) are denied;
    // the three SCIM admin pages call only /scim/* (tokens, mappings, activity, webhooks).
    'admin-scim',
    'admin-scim-mapping',
    'admin-scim-activity',
    // 2400LR-pg4 sweep recheck (templated ${apiUrl}/... paths): payment-settings calls only
    // /payments/settings* (admin_plans_payment_settings.py); the denied mobile app page calls /mobile/*
    // (mobile.py, **/mobile*.py :1562). Both routers denied.
    'payment-settings',
    // CE-COUPONSCE: coupons.js carries no edition guard and every /coupons route
    // is denied (admin_plans_payment_settings.py, manifest :1281/:1445) -> without
    // this key the page rendered its static markup and the loader catch painted a
    // red "Failed to load listing coupons" row instead of the amber panel.
    'coupons',
    // CE-ENTSTUB-UI: the five billing admin pages the pricing router backs
    // (/pricing/*, /payments/* and the admin plan editor are all denied on CE) had no
    // CE gate of their own, so a #hash visit rendered the page and its forms called
    // routes that do not exist. Same predicate and panel as every id above.
    'custom-plans',
    'promo-codes',
    'free-trials',
    'geographic-pricing',
    'seasonal-pricing',
]);

// rc20 (10-31 CE live-fail, dash-admin-nav-regroup sweep 1-5/5) — five hash aliases
// whose backing routers open-core/manifest.yaml denies on CE: gpu-pool (admin_gpu_pool.py
// :1354 + the gpu_pool globs), pricing (pricing.py :1452), admin-scim (scim_v2.py /
// admin_scim_*.py — already in _CE_COV4B_PAGES, but that gate's panel does not carry the
// container id the sweep pins), payment-settings (admin_plans_payment_settings.py) and
// access-reviews (routers/tenant/access_reviews.py, a Compliance-hub tab whose content
// comes from that denied tenant router). On CE the sweep's container witness (ar-panel-
// campaigns / gpu-kpi-avg / free-tier-row / the scim add-token control / stripe-status-badge) was
// ABSENT: the page markup ships, but the loader that fills/keeps the pinned id never
// resolves against a 404 route. Same predicate as every other CE gate here (_isCeEdition,
// fail-closed (rc20 failopen-a) on an unreadable /platform/features payload), own set so the literals above
// stay byte-identical, and the panel SHIPS the pinned id — the CE leg keeps a POSITIVE
// witness (the page is explained, not blank) and the paid legs are untouched.
// CE-REGGATE-1: this is a WITNESS-SHAPE key list only — the gate never reads it.
// The gate is `_ceDeniedPageId()` (registry-derived); this list only names the ids
// whose panel ships a pinned sweep witness (the map below). The denied drift view
// is deliberately ABSENT here: its registry row is scrubbed out of the CE extract,
// so the derived predicate denies it, and its panel resolves its witness through
// the `ce-retired-admin-<id>` fallback — deleting an entry can never fail open.
// (Name kept as `_CE_RETIRED_ADMIN_PAGES` so the registry tests' five-Set derivation
// stays intact; it is a witness/nav list, NOT a gate input.)
const _CE_RETIRED_ADMIN_PAGES = new Set([
    'gpu-pool',
    'pricing',
    'admin-scim',
    'payment-settings',
    'access-reviews',
    // FIX-DASH-CE-DEAD-NAV-3: the Model Cache panel calls GET /system/model-cache/*
    // (routers/model_cache_admin.py, denied on CE), so a #model-cache visit painted the
    // red "API not available" stats row. Retired like the denied drift view, pinned to the page's
    // own sweep witness container id (analytics.html:1520 <div id="mc-stats">).
    'model-cache',
]);

// CE-REGGATE-1 — the DERIVED gate. `_ceDeniedPageId()` reads the page registry
// (ui-manifest.js): a page is CE-denied when its registry row is marked `paid:
// true` (the paid-tree view; `ceReachable: true` rows stay reachable, exactly the
// marketplace carve-out the paid-hub gate keeps below) OR when it has NO row at
// all — FAIL CLOSED, so deleting an entry from any list above can never offer a
// denied page. The retired-admin CLASS (which panel ships) is the registry row's
// `gate` label when the row survives the scrub, else the witness list plus "not
// owned by another gate class below" — never a hard-coded paid page id in the
// gate itself. Every branch stays guarded by `await _isCeEdition()`, so on paid
// this predicate is never consulted and behaviour is byte-identical.
// CE-NOPAID-4.2 — thin LOCAL wrappers over the ONE registry seam (`_ceRegisteredRows()`
// from ui-manifest.js): page-registry.js exports the same names for api.js/hub-compliance.js
// consumers; ui.js keeps its own copies so the CE extract never imports page-registry.js.
function getPageEntry(pageId) {
    return _ceRegisteredRows()[pageId];
}
function isPaidPage(pageId) {
    const entry = getPageEntry(pageId);
    return !!(entry && entry.paid === true);
}
function _ceDeniedPageId(pageId) {
    const row = getPageEntry(pageId);
    if (row) return row.paid === true && row.ceReachable !== true;
    return true; // unregistered -> denied (fail closed)
}
function _ceRetiredAdminGate(pageId) {
    if (!_ceDeniedPageId(pageId)) return false;
    const row = getPageEntry(pageId);
    if (row) return row.gate === 'retired-admin';
    // Row scrubbed out of the CE extract (e.g. the denied drift view): retired
    // unless one of the other gate classes below owns its panel.
    return !(_CE_COV4B_PAGES.has(pageId) || _CE_PAID_HUB_PAGES.has(pageId)
        || _CE_COMPLIANCE_PAGES.has(pageId) || _CE_PAID_COMPLIANCE_PAGES.has(pageId));
}

function _ceRetiredAdminPanelHtml(pageId) {
    // Amber notice + the one container id the route sweep pins for this page, so a CE
    // visitor sees an explanation and the sweep still proves the route RESOLVED (the id
    // lives in the page file on a paid build; here it ships empty inside the notice).
    // admin-scim is the exception: its sweep-pinned id is the Add-Token CONTROL, which
    // admin-identity.spec.ts asserts is ABSENT on CE (security claim — the panel must not
    // ship it), so there the notice carries the non-actionable witness id
    // ce-retired-admin-scim instead; the sweep's CE leg expects that.
    const pinned = {
        'gpu-pool': 'gpu-kpi-avg',
        'pricing': 'free-tier-row',
        'admin-scim': 'ce-retired-admin-scim',
        'payment-settings': 'stripe-status-badge',
        'access-reviews': 'ar-panel-campaigns',
        // FIX-DASH-CE-DEAD-NAV-3: the Model Cache stats container (analytics.html:1520
        // <div id="mc-stats">, filled by js/model-cache.js _mcRenderStats) — the CE leg
        // keeps a positive witness that the route resolved, shipped empty.
        'model-cache': 'mc-stats',
    }[pageId] ?? `ce-retired-admin-${pageId}`; // CE-REGGATE-1 fallback: a denied id
    // with no pinned row (its map row scrubbed out, e.g. the denied drift view) still
    // ships a non-actionable witness, so removing a map row can never blank the sweep.
    return `<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900">
            <h1 class="text-xl font-semibold text-amber-900">Not available in MOD Community Edition</h1>
            <p class="text-sm">This page is not available in this edition.</p>
            <div id="${pinned}"></div>
        </div>`;
}

// rc20-uijs-drift — ONE helper hiding the SIDEBAR entry of every CE-retired admin alias
// in the set above, so the nav and the direct-URL panel can never disagree (same shape as
// _hideCeComplianceNav/_hideCePaidComplianceNav: runs AFTER the per-item role gates in
// updateUIVisibility, only ever hides, and HIDES when the edition is unknown — FAIL-CLOSED,
// rc20 failopen-a, so an unreadable entitlements payload can never offer a CE-denied page).
// gpu-pool / pricing / payment-settings rows are already hard-hidden by their own blocks, so
// this is inert for them; the rows that still carry a live role gate are the denied drift view
// (ui.js:1768, the WSIP 6.16 tenant-admin idiom) and access-reviews (ui.js:1437).
// CE-REGGATE-1 — the ids whose SIDEBAR row this helper hides are DERIVED: every id the
// registry marks paid (`_ceDeniedPageIds()`; on CE the scrub removes some of those rows,
// which is why the witness list above is unioned in — a row the scrub deleted still gets
// its row hidden) plus the retired aliases. Only ever hides; unknown edition HIDES.
function _ceRetiredAdminNavIds() {
    const ids = new Set(_CE_RETIRED_ADMIN_PAGES);
    for (const id of _ceDeniedPageIds()) ids.add(id);
    return ids;
}
function _hideCeRetiredAdminNav() {
    return _isCeEdition().then((isCe) => {
        if (!isCe) return;
        for (const pageId of _ceRetiredAdminNavIds()) {
            const link = document.getElementById(`${pageId}-nav-link`);
            if (link) link.style.display = 'none';
        }
        _syncNavGroupHeadings();
    });
}

// 2400LR-pg3 — ONE helper hiding in-page links (static tab bars etc.) whose #target is a
// CE-gated page (_CE_COV4B_PAGES), so a CE user never lands on the "not available" panel.
// Only ever hides; FAIL-CLOSED (rc20 failopen-a: unknown edition HIDES — an
// unreadable payload can never reveal a CE-denied surface). Nav-sidebar links are
// handled by their own helpers. Called after each page load; hubs may call it on `root`.
function _hideCeGatedPageLinks(root) {
    return _isCeEdition().then((isCe) => {
        if (!isCe) return;
        (root || document.getElementById('page-content') || document)
            .querySelectorAll('a[href^="#"]').forEach((a) => {
                const id = (a.getAttribute('href') || '').slice(1).split(/[?/]/)[0];
                if (_CE_COV4B_PAGES.has(id)) a.style.display = 'none';
            });
    });
}

function _hideCePaidComplianceNav() {
    // Same shape as _hideCeComplianceNav: only ever hides, runs after the per-item
    // role gates in updateUIVisibility, and HIDES when the edition is
    // unknown (FAIL-CLOSED, rc20 failopen-a).
    return _isCeEdition().then((isCe) => {
        if (!isCe) return;
        for (const pageId of _CE_PAID_COMPLIANCE_PAGES) {
            const link = document.getElementById(`${pageId}-nav-link`);
            if (link) link.style.display = 'none';
        }
        _syncNavGroupHeadings();
    });
}

// FIX-CE-DASH-HIDE-STORE-NAV (owner 11-16 ~2:15 PM CT: "Marketplace and Library in
// CE: HIDE THE NAV ENTRIES (not the Not-available panel; keep the panel from ui.js
// a2043cbbc6 only as the deep-link fallback)") — CE has NO store scope (no
// Marketplace, no Library, no Seller / stack publishing), so the FOUR store NAV
// ENTRIES are hidden on CE by their own helper, gated with the SAME _isCeEdition()
// predicate every CE gate here uses (FAIL-CLOSED, rc20 failopen-a: unknown counts
// as CE, so an unreadable entitlements payload can never reveal a store entry).
// The helper only ever HIDES; the paid reveal stays exactly where it was
// (_applyCePaidHubNav, positive saas/pro + role gate), so paid nav and wording are
// byte-identical. The deep-link fallback is NOT removed: a direct #marketplace /
// #store / #store / #store hash still renders the standard
// "Not available in MOD Community Edition" panel from a2043cbbc6 (the gate below,
// _CE_PAID_HUB_PAGES + _cePaidPagePanelHtml). The ui-manifest.js registerPage rows
// (ceReachable/paid fields, the 106-row count, the OZ-C1b theme rows) are untouched.
const _CE_STORE_NAV_PAGES = new Set([
    'marketplace',
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Library page id
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Seller page id
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Listing Review page id
]);
function _hideCeStoreNav() {
    return _isCeEdition().then((isCe) => {
        if (!isCe) return;
        for (const pageId of _CE_STORE_NAV_PAGES) {
            const link = document.getElementById(`${pageId}-nav-link`);
            if (link) link.style.display = 'none';
        }
        _syncNavGroupHeadings();
    });
}

// rc20-ce-nav (owner 11-04, CE live-fail) — the FOUR paid-edition hub pages must
// not be OFFERED in the CE sidebar: Marketplace (marketplace-nav-link, which the
// shell ships with NO display:none / no toggle at all — "Intentionally no
// display:none / admin toggle"), Security (security-nav-link, set display:'flex'
// unconditionally in updateUIVisibility), Compliance (compliance-center-nav-link,
// gated only by isSuperAdmin || isPlatformAdmin) and Billing — BOTH the tenant
// Upgrade row (billing-nav-link) and the platform-admin consolidated Billing row
// (admin-billing-nav-link, href #admin-plans, gated only by isPlatformAdmin, the
// row Tim actually sees on his CE install). The manifest denies the backing
// routers (billing.py, admin_plans*.py, marketplace routers) and the compliance
// hub shell, but the hub page/js files themselves ship (security-hub.js,
// hub-compliance.js, billing.js, marketplace.js), so the nav offered them and a
// direct #security / #marketplace / #compliance-center / #billing / #admin-plans
// hash rendered the page. SAME fail-closed predicate as every CE gate here
// (_isCeEdition, unknown counts as CE); only ever hides, runs AFTER the role
// gates above, so a CE build can never reveal one and paid behaviour is
// untouched. Child deep links (#audit-logs, #active-sessions, …) keep their
// existing per-tab CE gates — the CE-core tabs stay reachable.
const _CE_PAID_HUB_PAGES = new Set([
    'marketplace',
    'security',
    'compliance-center',
    'billing',
    'admin-plans',
    // rc20-ce-nav (owner 11-04 bug 1) — Capture Devices is NOT on CE (capture_field_client
    // routers denied, manifest :1750): its sidebar row and its System sub-tab now ship
    // hidden and only a POSITIVE paid edition reveals them; the standalone #capture-devices
    // hash lands on the "Not available" panel through this set.
    'capture-devices',
    // FIX-CE-SUBSCRIPTION-PAGE-GUARD: the plan/upgrade page is paid-only; CE has no plans (Tim: no licence gates, no paid surfaces)
    'subscription',
    // CE-OPSPROP-DEEPLINK: #operator-proposals is also in _CE_PAID_COMPLIANCE_PAGES
    // (ui.js:352, gate :810 runs first); this entry is defence-in-depth so the paid-hub
    // gate agrees if that set is ever trimmed.
    'operator-proposals',
    // owner 11-16 store-deny decision: CE has no store scope (no Marketplace, Library, Seller,
    // stack publishing); #store / #store / #store land on the "Not available" panel.
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Library page id
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Seller page id
    // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Listing Review page id
]);

// rc20-ce-nav — NO-FLICKER rule (owner 11-04): a paid row must never be painted
// and then removed. Every row in the list below starts display:none in
// dashboard-shell.html, so the ONLY thing that may reveal it is a POSITIVE paid
// edition (saas/pro) plus its role gate; CE and unknown keep them hidden
// permanently. _applyCePaidHubNav runs after the synchronous role gates and
// either hides (CE/unknown) or re-reveals (paid) with the SAME role expression
// the gates above used, so paid behaviour is byte-equivalent and CE never
// renders-then-removes anything.
function _applyCePaidHubNav(isSuperAdmin, isPlatformAdmin) {
    // ONE pass over the paid hub rows: reveal ONLY on a POSITIVE paid edition
    // plus the row's own role gate (billing additionally keeps the existing
    // features.billing_enabled gate, so onprem/paid behaviour is unchanged);
    // CE and unknown keep every row display:none from first paint (the shell
    // now ships marketplace-nav-link hidden too), so nothing is ever painted
    // and then removed.
    return getPlatformFeatures().then((features) => {
        const ent = features && features.entitlements;
        const paid = !!ent && !ent.error
            && (ent.edition === 'saas' || ent.edition === 'pro');
        const billingEnabled = !!(features && features.billing_enabled);
        const reveal = {
            'marketplace-nav-link': true,
            'security-nav-link': true,
            'compliance-center-nav-link': (isSuperAdmin || isPlatformAdmin),
            'billing-nav-link': (billingEnabled && !isPlatformAdmin && isSuperAdmin),
            'admin-billing-nav-link': isPlatformAdmin,
        };
        for (const navId of Object.keys(reveal)) {
            const link = document.getElementById(navId);
            if (!link) continue;
            link.style.display = (paid && reveal[navId]) ? 'flex' : 'none';
        }
        _syncNavGroupHeadings();
    }).catch(() => {
        // Unreadable payload = unknown = CE: hide, never reveal.
        for (const navId of ['marketplace-nav-link', 'security-nav-link',
            'compliance-center-nav-link', 'billing-nav-link', 'admin-billing-nav-link']) {
            const link = document.getElementById(navId);
            if (link) link.style.display = 'none';
        }
    });
}

// 0b.2 redo (R37.4) — the Compliance hub ships as js/hub-compliance.js (NOT
// js/compliance-hub.js: fnmatch's `compliance*.js` deny glob in open-core/manifest.yaml
// would carve it out of CE, and the hub must SHIP so it can host the four CE-visible
// tabs). It is still resolved through optionalPageModule at each dispatch site, the
// same one-liner helper the six child branches call.
async function _loadComplianceHubTab(child) {
    const init = await optionalPageModule('./hub-compliance.js', 'initializeComplianceHubPage');
    if (init) await init(child);
}

// 0b.2 s1 (R37.4) — admin nav regroup: a group div (nav-group) shows a heading,
// so when EVERY member link inside it is hidden (CE edition hides, or the role
// gates hide) the group itself must hide — a CE/non-admin user never sees an
// empty heading. Only ever hides whole groups; member links keep their own
// per-link role gates untouched.
function _syncNavGroupHeadings() {
    for (const group of document.querySelectorAll('.nav-group')) {
        const visible = Array.from(group.querySelectorAll('.nav-link'))
            .some((link) => link.style.display !== 'none');
        group.style.display = visible ? '' : 'none';
    }
}

function _cePaidCompliancePanelHtml() {
    // Direct-URL panel (#release-approvals bookmark on a CE build, whose page
    // file the manifest denies) — same amber notice shape as the compliance
    // panel, worded for this surface.
    return `<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900">
            <h1 class="text-xl font-semibold text-amber-900">Not available in MOD Community Edition</h1>
            <p class="text-sm">Production release approvals, the emergency change path and the auto change log are not available in this edition. MOD Community Edition ships the audit log and no other compliance surfaces, jobs or reports.</p>
        </div>`;
}

export async function navigateToPage(hash) {
    const navId = ++_navGeneration;
    const targetHash = hash || "#home";
    let targetPageId = targetHash.substring(1);

    // Deep links carry a query string after the page id, e.g.
    // "#changelog?slug=xxx", "#logs?event_name=...", "#workflows?run=...",
    // "#files?hash=...". Strip it before targetPageId is used for the
    // pageFile lookup / page-html fetch / dispatch equality checks below —
    // left un-stripped, the fetch below requests the wrong URL (e.g.
    // "pages/changelog?slug=xxx.html", which 404s) and NONE of the
    // `targetPageId === 'changelog'`-style branches match, so the deep
    // link fails to load the page at all rather than just failing to
    // focus the right item. Individual page modules that need the query
    // (e.g. changelog.js's slug focus) read it themselves off
    // `window.location.hash`/`.search`, which still has it — only this
    // local variable is normalized.
    targetPageId = targetPageId.split('?')[0];

    // BUG.B29b1 — the sub-tab a deep link asks for, read from EITHER URL shape:
    // tab-state.js keeps it in the real query string (/?tab=x#page — the form a
    // refresh restores, see getActiveTab below) while a hand-written bookmark
    // carries it inside the hash (#page?tab=x, which the strip above just
    // normalized away). Both must be honoured so #logs?tab=permissions redirects
    // whichever way the user typed/bookmarked it.
    const _qIdx = targetHash.indexOf('?');
    const _requestedTab = getActiveTab()
        || (_qIdx >= 0
            ? new URLSearchParams(targetHash.substring(_qIdx + 1)).get('tab')
            : null);

    // Map 'system' to 'analytics' page. 'burst' rides the same map:
    // Burst became a System sub-tab rather than a page of its own, and
    // pages/burst.html is gone, so an existing #burst link has to resolve
    // to the System page and land on that tab (see below). 'burst-accounts'
    // (GH.8b — the tenant's own cloud accounts) is a System sub-tab too,
    // never its own page, so it rides the same map from the start.
    const pageFileMap = {
        'system': 'analytics',
        'burst': 'analytics',
        'burst-accounts': 'analytics',
        // dashboard-marketplace-tabs — Library and My Listings (store-page)
        // are now tabs of the combined pages/marketplace.html rather than
        // their own page files; old #store / #store bookmarks land on
        // the matching tab (see the dispatch branches below).
        // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Library bookmark map
        // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Seller bookmark map
        // 0b.2 redo (R37.4, owner 10-08: "tabs put within the main tab", NOT
        // sidebar segments) — the Security and Compliance surfaces are tabs of
        // one hub page each (pages/security.html + js/security-hub.js,
        // pages/compliance-center.html + js/hub-compliance.js). Their old
        // #<child> bookmarks resolve to the hub page and land on their tab
        // through the dispatch branches below (initializeSecurityHubPage /
        // initializeComplianceHubPage). R39.8 (owner 10-08) added #platform-access-review
        // as the 11th Security-hub tab (it wasn't in the R37.4 list), so it aliases here
        // too.
        'security': 'security',
        'security-alerts': 'security',
        'alert-rules': 'security',
        'active-sessions': 'security',
        'tenant-idp': 'security',
        'elevation-requests': 'security',
        'abuse-queue': 'security',
        'security-incidents': 'security',
        'ip-blocks': 'security',
        'outbound-connections': 'security',
        'security-lockdown': 'security',
        // R39.8 (owner 10-08) — Platform access review is now the 11th tab of the
        // Security hub (its own sidebar row is gone from dashboard-shell.html).
        'platform-access-review': 'security',
        // FX.163 p3a2 (owner 10-08) — Pending approvals is the 12th tab of the Security
        // hub (no sidebar row of its own — the owner wants tabs inside the main tab).
        'pending-approvals': 'security',
        'compliance-center': 'compliance-center',
        'auditor-access': 'compliance-center',
        'access-reviews': 'compliance-center',
        'licence-review': 'compliance-center',
        'release-approvals': 'compliance-center',
        'operator-proposals': 'compliance-center',
        'key-inventory': 'compliance-center',
        'lockbox-approval': 'compliance-center',
        // BUG.B29b1 (owner 10-08) — the Audit log moved to the Compliance hub's
        // "Audit log" tab (pages/compliance-center.html + js/hub-compliance.js,
        // tab key 'audit-logs'), so the old standalone #audit-logs bookmark
        // aliases to the hub page and lands on that tab through the dispatch
        // branch below (same _loadComplianceHubTab one-liner the other children
        // call). No sidebar row is added — hub TABS only.
        'audit-logs': 'compliance-center',
        // R39.7 (owner 10-08) — Compliance profiles is now the 7th tab of the
        // Compliance hub (its own sidebar row is gone from dashboard-shell.html).
        'compliance': 'compliance-center',
        // 0b.2 redo (R37.4 part 2, owner Q6 10-08) — Apps is a main tab on the
        // System page (immediately left of Settings), not its own sidebar entry.
        // Old #apps bookmarks resolve to pages/analytics.html and land on the
        // Apps tab through initializeAnalyticsPage('apps') below.
        'apps': 'analytics',
        // BUG.B36c — the retired Settings → Integrations → Outbound Webhooks sub-tab:
        // the old #webhooks bookmark rides the same analytics page file as #apps and
        // lands on the Apps tab's ONE "Webhooks" section through the dispatch branch
        // below (which redirects hash + ?tab=apps instead of initializing webhooks).
        'webhooks': 'analytics',
        // 0b.2 redo (R37.4 part 2, owner 10-08: "tabs put within the main tab",
        // NOT sidebar segments) — the six Users-group members are tabs of the
        // Users page (pages/users.html + js/users.js). Old #<child> bookmarks
        // resolve to pages/users.html and land on their tab through
        // initializeUsersPageListeners('<child>') below; a refresh restores the
        // tab via ?tab= (tab-state.js). NOTE the nav-id trap: the DSAR child's
        // sidebar link was dsar-requests-nav-link but its hash/page file is
        // admin-dsar-requests — the tab key follows the page file.
        'held-matches': 'users',
        'parental-consent': 'users',
        'data-requests': 'users',
        'admin-dsar-requests': 'users',
        'staff-register': 'users',
        'consent-register': 'users',
        // 0b.2 redo (R37.4 part 2) — Log Streaming is a tab of the Logs page
        // (pages/logs.html + js/logs.js), not its own sidebar entry. Old
        // #log-streams bookmarks resolve to pages/logs.html and land on the
        // Log Streaming tab through initializeLogsPage('log-streams') below.
        'log-streams': 'logs',
        // 0b.2 redo (R37.4 part 2) — Listing review is a tab of the
        // Marketplace page (pages/marketplace.html + js/marketplace.js).
        // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2) - no Listing Review bookmark map
        // 0b.2 redo (R37.4 part 2) — "AI in this platform" and Support are
        // tabs of the Help page (pages/help.html + js/help.js, its own
        // help-tab / help-panel-<key> + localStorage 'help.activeTab'
        // convention). Old #ai-inventory / #support-tickets bookmarks resolve
        // to pages/help.html and land on their tab through
        // initializeHelpPage('<child>') below (help.js honours initialTab over
        // the saved choice; the tab choice persists in localStorage, so the
        // ?tab= tail assertion the hub sweeps use does not apply here).
        'ai-inventory': 'help',
        'support-tickets': 'help'
    };
    const pageFile = pageFileMap[targetPageId] || targetPageId;

    // 976d — the hash-route / alias navigation path (popstate, dashGoto,
    // auth.js post-login restore) never passes through the sidebar click
    // handler above, which is the only place that used to call clearActiveTab()
    // before setting window.location.hash. So navigating from a tabbed page
    // (System → GPU Pool leaves ?tab=settings&sub=gpu-pool) to an old alias
    // (#held-matches) kept the stale ?sub= on the URL: the destination page's
    // own tab writer (users.js via tab-state.js setActiveTab) then rewrote
    // ?tab= but left ?sub=gpu-pool, landing unrelated hubs on the wrong
    // sub-tab. Drop the param here when it came from the PREVIOUS page —
    // i.e. the resolved page differs from the one we're leaving AND the
    // incoming URL itself doesn't ask for a sub (a deep link / alias branch
    // that intentionally sets one, e.g. #gpu-pool -> ?tab=settings&sub=gpu-
    // pool or a bookmark's ?sub=, is honoured, not cleared). Reuses the
    // existing tab-state helper; no URL logic duplicated here.
    if (_lastPageFile !== null && _lastPageFile !== pageFile
        && new URLSearchParams(window.location.search).has('sub')) {
        // The stale sub is only a problem when the URL still carries one;
        // clearActiveTab() also drops ?tab=, which the destination page
        // rewrites anyway — same as the sidebar path above does.
        clearActiveTab();
    }
    _lastPageFile = pageFile;

    // FX.162.p3 (owner Q157: MOD CE must not do anything with compliance) —
    // ONE gate for all five compliance pages. A direct URL (#compliance, a
    // bookmark, an auditor deep link) on a CE build shows a short "Not
    // available in MOD Community Edition" panel instead of the page (and
    // instead of the generic "Page not found" error, since
    // open-core/manifest.yaml denies the page files themselves in the CE
    // carve). The nav items are hidden by the same predicate in
    // updateUIVisibility below. Awaitable + fail-closed (as _isCeEdition): an unreadable
    // /platform/features payload counts as CE, so the compliance panel shows rather than the page.
    if (_CE_COMPLIANCE_PAGES.has(targetPageId) && await _isCeEdition()) {
        document.getElementById('page-content').innerHTML = _ceCompliancePanelHtml();
        document.title = `Not available — MOD`;
        return;
    }

    // FX.144 p5 (owner 10-07, Q157=A) — same gate for the paid-only compliance
    // surfaces added since FX.162.p3 (the release-approval queue). Same
    // predicate, so the nav hide and this direct-URL panel can never disagree.
    if (_CE_PAID_COMPLIANCE_PAGES.has(targetPageId) && await _isCeEdition()) {
        document.getElementById('page-content').innerHTML = _cePaidCompliancePanelHtml();
        document.title = `Not available — MOD`;
        return;
    }

    // rc20 — the five retired admin hash aliases (gpu-pool / pricing / admin-scim /
    // payment-settings / access-reviews). Their backing routers are CE-denied, and two
    // of them (admin-scim, payment-settings) also sit in _CE_COV4B_PAGES, whose generic
    // panel does NOT carry the container id the route sweep pins — so this gate runs
    // FIRST and ships the panel with that id (a POSITIVE witness on the CE leg: the
    // route resolved and is explained, not blank). It also writes the tab tail the paid
    // branch for the two aliased ids produces (#gpu-pool -> System sub-tab,
    // #access-reviews -> Compliance-hub tab), via replaceState rather than a
    // location.search assignment (which reloads the whole SPA).
    if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition()) {
        document.getElementById('page-content').innerHTML = _ceRetiredAdminPanelHtml(targetPageId);
        document.title = `Not available — MOD`;
        const tail = {
            'gpu-pool': ['settings', 'gpu-pool'],
            'access-reviews': ['access-reviews', null],
        }[targetPageId];
        if (tail) {
            try {
                const url = new URL(window.location.href);
                url.searchParams.set('tab', tail[0]);
                if (tail[1]) url.searchParams.set('sub', tail[1]);
                window.history.replaceState(null, '', url.pathname + url.search + url.hash);
            } catch (_) { /* non-fatal */ }
        }
        return;
    }

    // 2400LR-pg1 — direct visit to a cov4b child page on CE (backing router denied).
    if (_CE_COV4B_PAGES.has(targetPageId) && await _isCeEdition()) {
        document.getElementById('page-content').innerHTML = _cePaidPagePanelHtml();
        document.title = `Not available — MOD`;
        return;
    }

    // FIX-CE-TRIALS-OFF (owner 11-15 ~8:15 PM CT popup answer "Hide in paid too"): the
    // trials page is hidden for EVERYONE in the launch build (CE AND paid) - run-time
    // hide only: the page ids, rows, API routes and the committed trial fix 623c6b12db
    // (trial payload carries capabilities) are untouched; nothing is deleted.
    if (targetPageId === 'free-trials') {
        document.getElementById('page-content').innerHTML = _trialsOffPanelHtml();
        document.title = `Trials are off — MOD`;
        return;
    }

    // rc20-ce-nav (owner 11-04) — direct visit to one of the FOUR paid hub pages on
    // CE (#marketplace / #security / #compliance-center / #billing / #admin-plans).
    // The hub page+js files SHIP on CE (the manifest denies the backing routers and
    // the compliance hub shell, not security-hub.js / hub-compliance.js / billing.js /
    // marketplace.js), so without this gate the whole page rendered. Same predicate as
    // _hideCePaidHubNav above, so the sidebar and this panel can never disagree. Child
    // bookmarks (#audit-logs, #active-sessions, …) are NOT in this set: they keep their
    // existing per-tab CE gates and still land on the hub's CE-visible tabs.
    // FIX-STOREPUB-CE-SELLER-TAB (11-06, Q-STORE-PUB=B answered 10-26): the marketplace PAGE is
    // CE-reachable - CE ships stack publishing for tenant admins only, and that affordance lives
    // inside #main-panel-my-listings (the store-page.js CE branch keeps the workflow-publish + invite-codes
    // blocks hidden; _applyCePaidHubNav keeps the nav row display:none, so only a direct hash reaches it).
    // owner 11-16: the marketplace exemption (FIX-STOREPUB-CE-SELLER-TAB) is removed - CE shows
    // the "Not available" panel for Marketplace, Library and Seller; paid is untouched (CE-only branch).
    if (_CE_PAID_HUB_PAGES.has(targetPageId) && await _isCeEdition()) {
        document.getElementById('page-content').innerHTML = _cePaidPagePanelHtml();
        document.title = `Not available — MOD`;
        return;
    }

    try {
        // CE-PG17P2: root-relative, NOT page-relative. The hash router's
        // replaceState (main.js:55/85/121, tab-state.js:19/42, settings.js:1692,
        // store-page.js:231, analytics.js:107/172, subscription.js:17/24) can leave a
        // nested pathname, where a page-relative fetch resolved to /pages/pages/x.html
        // and nginx's SPA fallback (nginx.conf:67-69) answered index.html with a 200 —
        // res.ok was true, so page-content got the whole index document.
        const pageContent = await fetch(`/pages/${pageFile}.html`).then(res => {
            if (!res.ok) throw new Error(`Could not load page: ${pageFile}.html`);
            return res.text();
        });
        if (navId !== _navGeneration) return;
        document.getElementById('page-content').innerHTML = pageContent;
        // OZ-WIRING (11-14) — the page just mounted carries the ONE
        // data-theme-key attribute on its h1 (OZ-D1 pattern). Re-apply the
        // label hook over the whole document (sidebar + this page): the
        // custom `mod:page-rendered` event is what the other OZ legs key off;
        // the direct call is the same idempotent applier for engines where
        // CustomEvent is unavailable. OFF (MOD_THEME_NAMES=off) is a
        // byte-identical no-op, so the OZ-A3 guard stays green.
        document.dispatchEvent(new CustomEvent('mod:page-rendered'));
        try { applyThemeLabels(document, window.MOD_THEME_NAMES, !(await isPaidEdition())); }
        catch (_ozErr) { applyThemeLabels(document, window.MOD_THEME_NAMES, true); }

        // is known to have loaded (skipped on the error path below).
        _setRouteTitle(pageFile);
    } catch (error) {
        console.error(error);
        document.getElementById('page-content').innerHTML = `<h1 class="text-3xl font-bold text-red-600">Error: Page not found</h1>`;
        document.title = `Not Found — MOD`;
        return;
    }

    // FX.160 p5 (owner R36.5=A) — fill the Runbook-link hrefs on the page that
    // just loaded (docs-site URLs from APP_CONFIG.DOCS_URL). Runs before the
    // page initializer below so nothing re-renders the header first.
    _loadRunbookLinks();
    _hideCeGatedPageLinks();

    // 2400UI15 — CE carve for the PAID-only banner endpoints. Six of the
    // banners below are fed by routers that open-core/manifest.yaml denies
    // wholesale (billing/welcome-bonus-state -> routers/billing.py:771,
    // sso/banner-state -> routers/sso_banner.py, sso/link-decline-state ->
    // routers/sso_link_retry.py, platform/license/status -> routers/
    // platform_license.py, tenant/warnings + tenant/security-notices ->
    // routers/tenant/*.py), so on a CE image every one of them 404s on
    // EVERY page nav. Await the edition once here (the coalesced 30s
    // /platform/features cache — no extra HTTP) and skip the fetches in
    // CE; each banner element ships class="hidden" in
    // pages/dashboard-shell.html, so hiding it needs no other change.
    // Fail-open: an unreadable payload is NOT CE, so paid/SaaS keeps the
    // exact same calls in the exact same order.
    const _paidEdition = await isPaidEdition();

    // First-month welcome-bonus banner
    if (_paidEdition) _loadWelcomeBonusBanner();

    // Load usage warnings (#102/#104)
    if (_paidEdition) _loadUsageWarnings();

    // add-self-service-sso §5.7 — first-paint SSO migration banner
    if (_paidEdition) _loadSsoMigrationBanner();

    // add-self-service-sso §6.6 — post-decline link-retry banner
    if (_paidEdition) _loadSsoLinkDeclineBanner();

    // WS1.LIC.1 — edition status warning banner (platform admins only)
    if (_paidEdition) _loadLicenseExpiryBanner();

    // GT.C2 — dispatch drain-mode banner (platform admins only)
    _loadDrainModeBanner();

    // Q9 — Vault sealed banner (platform admins only)
    _loadVaultSealedBanner();

    // GSEC FX.53 (Q36=A) — MFA enforcement countdown banner
    _loadMfaEnforcementBanner();

    // GSEC FX.50 p3b — security-notice banner (tenant admins)
    if (_paidEdition) _loadSecurityNoticeBanner();

    // Wrap the initialization logic in a timeout to ensure the DOM is ready after innerHTML is set.
    setTimeout(async () => {
        if (targetPageId === 'home') {
            initializeHomePage();
        } else if (targetPageId === 'files') {
            initializeFilesPageListeners();
        } else if (targetPageId === 'users') {
            initializeUsersPageListeners();
        } else if (targetPageId === 'invitations') {
            initializeInvitationsPageListeners();
        } else if (targetPageId === 'settings') {
            initializeSettingsPage();
        } else if (targetPageId === 'burst') {
            // B5b — the MOD-servers Burst sub-tab stays hidden on CE, so a
            // #burst bookmark must land on the System tab instead of
            // initializing that panel. Same CE predicate as every other
            // gate here (_isCeEdition, fail-closed on unknown).
            if (await _isCeEdition()) {
                await initializeAnalyticsPage();
            } else {
                await initializeAnalyticsPage('burst');
            }
        } else if (targetPageId === 'burst-accounts') {
            await initializeAnalyticsPage('burst-accounts');
        } else if (targetPageId === 'analytics' || targetPageId === 'system') {
            await initializeAnalyticsPage();
        } else if (targetPageId === 'logs' && _requestedTab === 'permissions') {
            // BUG.B29b1 (owner 10-08) — the Logs page's "Audit" tab (old deep
            // link #logs?tab=permissions) is gone: the audit log now lives as the
            // Compliance hub's "Audit log" tab. Same redirect idiom the relocated
            // Workers / Usage Stats / tenant-registry pages use: rewrite the hash
            // to the hub page and the query to the hub's tab key, then let the
            // fresh navigation run (the pageFileMap alias + the 'audit-logs'
            // dispatch branch above land it on #main-panel-audit-logs). No
            // sidebar row is added.
            window.location.hash = 'compliance-center';
            window.location.search = 'tab=audit-logs';
            setActiveSidebarItem('#compliance-center');
            return;
        } else if (targetPageId === 'logs') {
            initializeLogsPage();
        } else if (targetPageId === 'permissions') {
            initializePermissionsPage();
        } else if (targetPageId === 'subscription') {
            // CE: subscription page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'billing') {
            // CE: billing page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'pricing') {
            // CE: pricing page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'admin-plans') {
            // CE: admin-plans page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'docker-registry') {
            // pages/docker-registry.html used to be a standalone full HTML
            // document with its own inline <script> that never executed
            // once folded into this router (innerHTML-inserted scripts
            // never run — same bug class as payment-settings.html below).
            // Moved to a real module; see docker-registry.js's header.
            initializeDockerRegistryPage();
        } else if (targetPageId === 'admin-scim') {
            // CE: admin-scim page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'capture-devices') {
            // CE: capture-devices page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'gpu-pool') {
            // GPU Pool was relocated to a System page sub-tab.
            // Bridge old #gpu-pool bookmarks to the new location so
            // links from docs / emails / saved tabs keep working.
            window.location.hash = 'analytics';
            window.location.search = 'tab=settings&sub=gpu-pool';
            // The sidebar's System link is href="#system", not "#analytics"
            // (#analytics is an internal alias handled by the same dispatch
            // branch as #system below) — highlight by the link's real href.
            setActiveSidebarItem('#system');
            return;
        } else if (targetPageId === 'pricing-config') {
            // CE: pricing-config page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'bulk-permissions') {
            initializeBulkPermissionsPage();
        } else if (targetPageId === 'stack-billing') {
            // CE: stack-billing page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'auditor-access') {
            // GY.C16 — time-boxed auditor grants (admin, platform-admin
            // only; the router gates every /compliance/auditor-grants
            // route on is_platform_admin). Grants are read-only,
            // time-limited access for an external assessor — never
            // deleted, only revoked (status flip, ISO 27001).
            // FX.162.p6 — dynamic guarded import (CE carve): null on CE, so
            // nothing runs; on paid this is the same function, same (empty)
            // argument list, same place in the branch. NOTE: the original
            // call was synchronous; the guarded import is async, so the
            // module load adds one await boundary before it (the
            // initializer's own return value is still not awaited, exactly
            // as before).
            await _loadComplianceHubTab('auditor-access');
        } else if (targetPageId === 'parental-consent') {
            // GY.V5 follow-up — COPPA parental consent + minor data label
            // (admin, platform-admin only; the router gates every
            // /platform/consent route on is_platform_admin). Consent
            // history is append-only: revoke adds a "revoked" row and
            // never edits or deletes the grant.
            // FX.162.p6 — dynamic guarded import (CE carve): null on CE, so
            // nothing runs; on paid this is the same function, same (empty)
            // argument list, same place in the branch. NOTE: the original
            // call was synchronous; the guarded import is async, so the
            // module load adds one await boundary before it (the
            // initializer's own return value is still not awaited, exactly
            // as before).
            // R37.4 part 2 — Parental consent is a tab of the Users page now;
            // the guarded (CE-carved) module import lives in users.js _CHILD_INIT.
            await initializeUsersPageListeners('parental-consent');
        } else if (targetPageId === 'payment-settings') {
            // Stripe Settings tab. JS used to live inline in the page
            // as an IIFE, but inline <script> tags don't execute when
            // a page is mounted via innerHTML — the Save button was a
            // silent no-op. Module + explicit init is the right
            // pattern (same as every other admin page in this router).
            // CE: payment-settings page denied (manifest deny) - nothing to initialize
        } else if (false && targetPageId === 'storeLibrary') {  // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2)
            // dashboard-marketplace-tabs — old #store bookmark; lands on
            // the Marketplace page's Library tab (user-facing view of
            // everything the tenant owns, GET /v1/store-page, + redeem box
            // POST /v1/codes/redeem). Ungated: any tenant user sees it.
// CE: marketplace page denied (manifest deny) - nothing to load
        } else if (false && targetPageId === 'storeSeller') {  // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2)
            // dashboard-marketplace-tabs — old #store bookmark; lands on
            // the Marketplace page's My Listings tab (GG.UI.7 tenant
            // publisher view). Ungated (any tenant user), but the
            // server-side gates decide what the page can actually do:
            // workflow publish/visibility work for the publishing
            // tenant's admin; stack publish/unlist is platform-admin only
            // (tab renders read-only).
            // 2400LR-cdu: catalog.py denied on CE (manifest:1502); fail-closed: unknown/errored edition is treated as CE.
            // store-page.js hides the my-listings tab on CE, so land on the default store-page tab
            // instead of an empty hidden panel.
            if (await isPaidEdition()) {
// CE: marketplace page denied (manifest deny) - nothing to load
            } else {
// CE: marketplace page denied (manifest deny) - nothing to load
            }
        } else if (targetPageId === 'marketplace') {
            // dashboard-marketplace-tabs — combined Marketplace page:
            // Library (default) + My Listings + the GG.RS.6 on-prem
            // "Browse" remote-catalog tab (reads the PUBLIC catalog
            // through the Core API proxy, GET /v1/marketplace/remote-catalog;
            // hidden for SaaS tenants, revealed by marketplace.js when
            // /platform/features reports deployment_mode != 'saas').
            // Defaults to the ?tab= query param if present.
// CE: marketplace page denied (manifest deny) - nothing to load
        } else if (targetPageId === 'business') {
            // Business overview — relocated from the System page's
            // "Business" sub-tab into the Billing tab bar (after Coupons).
            // Platform-admin KPI dashboard sourced from
            // /platform/analytics/business-overview.
            // CE: business page denied (manifest deny) - nothing to initialize
        } else if (targetPageId === 'workers') {
            // Workers tab was moved to System page (analytics) - redirect there
            window.location.hash = 'analytics';
            window.location.search = 'tab=workers';
            setActiveSidebarItem('#system');
            return;
        } else if (targetPageId === 'webhooks') {
            // BUG.B36c — the #webhooks bookmark no longer initializes a page of
            // its own (the Settings → Integrations → Outbound Webhooks sub-tab is
            // retired). Redirect to the Apps tab's ONE "Webhooks" section
            // (System → Apps → Webhooks, B36a/B36b) — same landing the #apps
            // bookmark uses below.
            window.location.hash = 'analytics';
            window.location.search = 'tab=apps';
            setActiveSidebarItem('#system');
            return;
        } else if (targetPageId === 'secrets') {
            loadSecrets();
        } else if (targetPageId === 'storage') {
            initializeStorageConnectionsPage();
        } else if (targetPageId === 'apps') {
            // GX.A4 — Apps. 0b.2 redo (R37.4 part 2, owner Q6 10-08): no longer
            // its own page/sidebar entry — it is a main tab on the System page,
            // immediately left of Settings. The old #apps bookmark lands on the
            // System page + Apps tab; analytics.js runs initializeAppsPage()
            // against pages/apps.html fetched verbatim into the panel, and
            // falls back to System Health for a non-tenant-root user (same
            // isSuperAdmin gate the old apps-nav-link used).
            await initializeAnalyticsPage('apps');
        } else if (targetPageId === 'access-reviews') {
            // GY.C10 — access-review campaigns + SoD conflict report.
            // Tenant-admin only (same gate as Data Requests).
            // CE: access-reviews page denied (manifest deny) - nothing to load
        } else if (targetPageId === 'staff-register') {
            // GSEC FX.153 p3 — staff register + training-due view (covers
            // the FX.71 annual training checklist). Platform-admin only:
            // every /platform/staff-records route is gated server-side on
            // _require_platform_admin, so the nav mirrors that gate.
            // R37.4 part 2 — Staff register is a tab of the Users page now.
            await initializeUsersPageListeners('staff-register');
        } else if (targetPageId === 'abuse-queue') {
            // GSEC FX.93 p3 — Trust & Safety abuse-report queue. Platform-admin
            // only: routers/abuse_reports.py gates every /admin/abuse/reports
            // route on require_platform_admin, so the nav mirrors that gate.
            await initializeSecurityHubPage('abuse-queue');
        } else if (false && targetPageId === 'storeListingReview') {  // CE: store page denied (FIX-CE-STORE-API-DENY-PART-2)
            // GSEC FX.95 p3 — Marketplace store-page queue. Platform-admin
            // only: routers/admin_listing_review.py gates every /admin/store-page
            // route on require_platform_admin, so the nav mirrors that gate.
            // 0b.2 redo (R37.4 part 2) — Listing review is now a tab of the
            // Marketplace page; the old #store bookmark lands there
            // (js/marketplace.js re-applies the same platform-admin gate and
            // falls back to Library for anyone else).
// CE: marketplace page denied (manifest deny) - nothing to load
        } else if (targetPageId === 'security-incidents') {
            // GSEC FX.50 p3a — Breach-notification incident tracker. Platform-admin
            // only: routers/platform_security_incidents.py gates every /platform/security-incidents
            // route on require_platform_admin, so the nav mirrors that gate.
            await initializeSecurityHubPage('security-incidents');
        } else if (targetPageId === 'licence-review') {
            // GSEC FX.73 p2 (Q56=A) — Licence manual-review queue. Platform-admin
            // only: routers/platform_licence_review.py gates every
            // /platform/licence-review route on require_platform_admin, so the
            // nav mirrors the security-incidents gate.
            await _loadComplianceHubTab('licence-review');
        } else if (targetPageId === 'ai-inventory') {
            // GSEC FX.72 p3b (Q55=C) — "AI in this platform" transparency
            // page. Platform-admin only: routers/platform_ai_inventory.py
            // gates /platform/ai-inventory on require_platform_admin, so
            // the nav mirrors the licence-review gate.
            // 0b.2 redo (R37.4 part 2, owner 10-08): it is now a TAB of the
            // Help page, not its own sidebar entry — the old #ai-inventory
            // bookmark lands on pages/help.html + that tab (help.js applies
            // the same platform-admin gate to the tab strip and loads
            // pages/ai-inventory.html verbatim into help-panel-ai-inventory).
            await initializeHelpPage('ai-inventory');
        } else if (targetPageId === 'release-approvals') {
            // GSEC FX.144 p4 — Release-approval queue (production releases
            // awaiting the owner's one-click approve + the emergency/review
            // path). Platform-admin only: routers/release_approvals.py gates
            // every /platform/release-approvals route on require_platform_admin,
            // so the nav mirrors the ai-inventory gate.
            // FX.144 p5 (owner 10-07, Q157=A): compliance is paid-only, so the
            // page module is denied in the CE carve — resolved through optionalPageModule
            // (null on CE, so nothing runs; on paid this is the same function, same
            // (empty) argument list, same place in the branch, one await boundary).
            await _loadComplianceHubTab('release-approvals');
        } else if (targetPageId === 'operator-proposals') {
            // WSIP 7.6 — deep link from the autonomous-proposal notify emails
            // (`#operator-proposals?proposal=<id>`). The proposals live on the
            // Compliance hub's "Operator proposals" tab, keyed
            // 'compliance-operator-proposals' in hub-compliance.js's children
            // map (tenant-gated there). operator-proposals.js reads the
            // `proposal=` param itself off window.location.hash and focuses
            // that proposal's detail card.
            await _loadComplianceHubTab('compliance-operator-proposals');
        } else if (targetPageId === 'ip-blocks') {
            // GSEC FX.106 p2b — auto-blocked client IPs (list + unblock).
            // Platform-admin only: routers/platform_ip_blocks.py gates every
            // /platform/security/ip-blocks route on require_platform_admin
            // (same gate shape as platform_security_incidents.py), so the nav
            // mirrors the release-approvals gate.
            await initializeSecurityHubPage('ip-blocks');
        } else if (targetPageId === 'log-streams') {
            // GSEC FX.77 p6a — tenant log streaming (SIEM export)
            // destinations. Tenant-admin only: routers/tenant/log_streams.py
            // gates writes on TENANT:manage_settings (reads on
            // view_settings), so the nav mirrors the security-lockdown gate.
            // 0b.2 redo (R37.4 part 2) — Log Streaming is now a tab of the
            // Logs page; the old #log-streams bookmark lands on that page's
            // tab (initializeLogsPage honours initialTab over the ?tab= param
            // and the hub writes ?tab=log-streams so a refresh restores it).
            await initializeLogsPage('log-streams');
        } else if (targetPageId === 'alert-rules') {
            // GSEC FX.112 p3c — tenant-admin security alert rules (built-in
            // FX.156 engine rules: on/off + threshold inside the platform
            // bounds). Tenant-admin only: routers/tenant/security.py gates the
            // GET on TENANT:view_settings and the PUT on
            // TENANT:manage_settings, so the nav mirrors the log-streams gate;
            // a viewer without manage permission still gets the page (deep
            // link) and js/alert-rules.js drops to read-only on the 403.
            await initializeSecurityHubPage('alert-rules');
        } else if (targetPageId === 'tenant-idp') {
            // GSEC FX.78 p5 — tenant IdP admin page over the EXISTING /sso/idp
            // surface (routers/sso_idp.py; the FX.78.p4 BYO-IdP redo extended it
            // with Vault-backed OIDC client credentials, and the parallel
            // routers/tenant/identity_providers.py WIP was deleted). Tenant-
            // admin only: every route sits behind require_sso_enabled_plan +
            // require_tenant_admin, so the nav mirrors the alert-rules gate;
            // a viewer without write permission still deep-links and js/
            // tenant-idp.js drops to read-only on the 403. Secrets are write-
            // only here — the server never echoes one, only has_secret.
            await initializeSecurityHubPage('tenant-idp');
        } else if (targetPageId === 'security-alerts') {
            // GSEC FX.112 p3d — this tenant's security alerts (list +
            // acknowledge). Tenant-admin only: routers/tenant/security_alerts.py
            // gates the GET on require_capability(TENANT, "view_settings") and
            // the ack POST on require_capability(TENANT, "manage_settings"),
            // so the nav mirrors the alert-rules gate; a viewer without manage
            // permission still gets the page (deep link) and js/security-alerts.js
            // drops to read-only on the 403.
            await initializeSecurityHubPage('security-alerts');
        } else if (targetPageId === 'active-sessions') {
            // GSEC FX.112 p4a — active-sessions page (your own sessions, per-session
            // sign-out, sign out everywhere, and — for tenant admins — a picker that
            // revokes another user's sessions). Self-service: routers/me/identity_tenancy.py
            // GET+DELETE /me/sessions and routers/me/auth_apikeys.py POST /me/signout-everywhere
            // need no admin capability (unlike the tenant-admin security-alerts branch
            // above), so everyone gets the page; the tenant-admin picker inside js/active-sessions.js
            // is offered only when USER:deactivate is held (the gate services/users_router_service.sign_out_user_everywhere
            // reuses), and any 403 on a write drops the page to read-only.
            await initializeSecurityHubPage('active-sessions');
        } else if (targetPageId === 'elevation-requests') {
            // GSEC FX.120 p5a — JIT production-admin elevation requests (file
            // one + the pending queue every platform admin must be able to
            // decide). Platform-admin only: routers/platform_elevation.py gates
            // all six /platform/elevation-requests routes on require_platform_admin
            // (NOT the tenant-admin gate the security-alerts branch above uses),
            // and approve/deny/revoke additionally carry require_fresh_auth
            // (GY.C4b) — js/elevation-requests.js surfaces that reauth_required
            // 401 with the re-login path auth.js handleLogin provides.
            await initializeSecurityHubPage('elevation-requests');
        } else if (targetPageId === 'held-matches') {
            // GSEC FX.74 p4 — admin review of accounts an offline OFAC SDN screening match
            // HELD for review (FX.74 p3a: a match never blocks, it holds — UserStatus.PENDING
            // plus a security_incident_created PermissionAuditLog row carrying the evidence).
            // No dedicated held-match API exists, so this page reads the routes that already do:
            // GET /users/ (require_capability USER:view) + GET /audit-logs/permissions?action=
            // security_incident_created&resource_type=security (the evidence, audit-integrity capability
            // gated — a 402 there means "no evidence column", the accounts still list), and
            // decides with the existing user routes: PATCH /users/{id}/reactivate (Release),
            // PATCH /users/{id}/deactivate (Reject), PATCH /users/{id} {status:'pending'} +
            // POST /platform/security-incidents (Escalate). Gated like that guard's TENANT half:
            // resolve_target_user_for_admin lets a platform admin act anywhere while a tenant
            // admin needs the user:reactivate/user:deactivate grant, so the nav mirrors the
            // Users/Data Requests gate (isSuperAdmin || isPlatformAdmin) — NOT the platform-
            // admin-only gate the elevation branch above uses. A 403 on a write drops the page
            // read-only, so a deep link (#held-matches) is safe but never decisive.
            // R37.4 part 2 — Held Matches is a tab of the Users page now.
            await initializeUsersPageListeners('held-matches');
        } else if (targetPageId === 'platform-access-review') {
            // FX.71 p2 — platform staff quarterly access review (owner
            // confirm/revoke). Platform-admin only: routers/admin_platform_access_review.py
            // gates every /admin/platform-access-reviews route on require_platform_admin,
            // so the nav mirrors the staff-register gate.
            // R39.8 (owner 10-08) — no longer its own page: it is the 11th tab of the
            // Security hub (pages/security.html + js/security-hub.js). Old #platform-access-
            // review bookmarks land here through the pageFileMap alias above and select the
            // tab; the hub applies the same platform-admin gate (_TAB_GATE) and persists it
            // across a refresh via ?tab= (tab-state.js).
            // CE: platform-access-review page denied (manifest deny) - nothing to load
        } else if (targetPageId === 'pending-approvals') {
            // FX.163 p3a2 — second-admin pending-approvals decisions (routers/pending_approvals.py
            // gates every route on require_capability(TENANT, "manage_settings"), so non-admins
            // 403 server-side). It is the 12th tab of the Security hub — no sidebar row, the
            // hub applies the 'tenant' gate (_TAB_GATE) and persists it via ?tab= (tab-state.js).
            await initializeSecurityHubPage('pending-approvals');
        } else if (targetPageId === 'key-inventory') {
            // FX.125 p3 — platform key inventory (read-only). Platform-admin
            // only: routers/admin_keys.py gates /admin/keys/inventory on
            // require_platform_admin (403 + audit for non-admins), so the nav
            // mirrors the platform-access-review gate.
            await _loadComplianceHubTab('key-inventory');
        } else if (targetPageId === 'outbound-connections') {
            // GSEC FX.63 p3 — outbound-connections preview + toggles. Platform
            // admin only: routers/admin_telemetry.py gates GET /admin/telemetry/preview
            // on require_platform_admin (403 + audit for non-admins); the toggles
            // write via PATCH /platform/settings/{key} (same platform-admin gate),
            // so the nav mirrors the key-inventory gate.
            // FX.162.p6 — dynamic guarded import (CE carve): null on CE, so
            // nothing runs; on paid this is the same async function, awaited
            // exactly as before, same (empty) argument list.
            await initializeSecurityHubPage('outbound-connections');
        } else if (targetPageId === 'support-tickets') {
            // GSEC FX.70 p3 — tenant support tickets (open + list). The router
            // gates both routes on require_active_and_onboarded_user only (no
            // role gate), so any signed-in, onboarded tenant user can open and
            // read this tenant's tickets; respond/resolve live platform-admin
            // side. The nav mirrors that: visible to every signed-in user.
            // 0b.2 redo (R37.4 part 2, owner 10-08): Support is now a TAB of
            // the Help page (ungated tab, same as the old nav link), so the
            // old #support-tickets bookmark lands there.
            await initializeHelpPage('support-tickets');
        } else if (targetPageId === 'consent-register') {
            // FX.98 p3 — likeness consent register. Tenant-admin only:
            // every /tenant/likeness-consents route is gated server-side
            // on the shared _is_tenant_admin idiom, so the nav mirrors
            // the Users/Data Requests gate (isSuperAdmin || isPlatformAdmin).
            // R37.4 part 2 — Consent register is a tab of the Users page now.
            await initializeUsersPageListeners('consent-register');
        } else if (targetPageId === 'lockbox-approval') {
            // FX.67 p2 — customer lockbox inbox (tenant side). Tenant-admin
            // only: every /tenant/lockbox/requests route is gated server-side
            // on the shared _is_tenant_admin idiom (manage_settings for the
            // decisions), so the nav mirrors the consent-register gate
            // (isSuperAdmin || isPlatformAdmin).
            // CE: lockbox-approval page denied (manifest deny) - nothing to load
        } else if (targetPageId === 'security-lockdown') {
            // FX.101 p4 — sign-in lockdown (tenant side). Tenant-admin only:
            // /tenant/security/ip-allowlist is gated on manage_settings for the
            // tenant admin (platform admin OR the tenant's root admin), so the
            // nav mirrors the consent-register gate (isSuperAdmin ||
            // isPlatformAdmin). The SSO posture and the gov-requirement tables
            // degrade in-page (402 SSO envelope / 403 auditor detail) rather
            // than hiding the page.
            // FX.162.p6 — dynamic guarded import (CE carve): null on CE, so
            // nothing runs; on paid this is the same async function, awaited
            // exactly as before, same (empty) argument list.
            await initializeSecurityHubPage('security-lockdown');
        } else if (targetPageId === 'compliance') {
            // GY.C1 — compliance profiles (platform admin; GETs also serve
            // time-boxed auditors read-only, PUT is platform-admin only).
            // compliance.js hides Save + disables the toggles when the
            // session lacks platform-admin rights.
            // R39.7 (owner 10-08) — no longer its own page: it is the 7th tab
            // of the Compliance hub, so the old #compliance bookmark lands on
            // the hub with that tab selected (hub-compliance.js runs the same
            // guarded initializeCompliancePage into #main-panel-compliance).
            // On CE, navigateToPage's _CE_COMPLIANCE_PAGES gate above already
            // returned the amber panel, so this branch never runs there.
            await _loadComplianceHubTab('compliance');
        } else if (targetPageId === 'security') {
            // 0b.2 redo (R37.4) — the Security hub itself (#security sidebar
            // entry): lands on the first tab the user's role gates allow.
            await initializeSecurityHubPage();
        } else if (targetPageId === 'audit-logs') {
            // BUG.B29b1 (owner 10-08) — the Audit log is no longer its own page:
            // it is the FIRST tab of the Compliance hub (js/hub-compliance.js
            // TAB_KEYS[0] = 'audit-logs', which fetches pages/audit-logs.html
            // verbatim into #main-panel-audit-logs). The pageFileMap alias above
            // already resolved the markup to pages/compliance-center.html; this
            // branch lands the hub on that tab through the same guarded
            // _loadComplianceHubTab one-liner every other child calls. No
            // sidebar row is added (layout rule: hub TABS inside main pages).
            await _loadComplianceHubTab('audit-logs');
        } else if (targetPageId === 'compliance-center') {
            await _loadComplianceHubTab(null);
        } else if (targetPageId === 'account') {
            initializeAccountPage();
        } else if (targetPageId === 'changelog') {
            initializeChangelogPage();
        } else if (targetPageId === 'help') {
            // 0b.2 redo (R37.4 part 2): the Help page now hosts four tabs
            // (Cards / Mind Map / AI in this platform / Support); no initial
            // tab -> help.js restores the user's saved choice.
            await initializeHelpPage();
        } else if (targetPageId === 'tenant-registry') {
            // Tenant Registry is now a tab inside the Data page (so "all
            // data the tenant owns on the platform — files + images —
            // lives in one place"). Mirror the workers-redirect pattern:
            // flip the hash to #files and stash the active tab in the
            // query string so the page code can pick it up.
            window.location.hash = 'files';
            window.location.search = 'tab=tenant-registry';
            setActiveSidebarItem('#files');
            return;
        }
    }, 0);

    // BUG.B93 — highlight by the stripped page key (same key every
    // `targetPageId === 'x'` dispatch branch above matches on), not the
    // raw incoming hash. Deep links carry a query string after the page
    // id (#files?hash=.., #logs?tab=.., #changelog?slug=..) — comparing
    // the RAW hash against a sidebar link's plain `href="#files"` never
    // matched, so those entries never lit up. See setActiveSidebarItem.
    setActiveSidebarItem('#' + targetPageId);
}

// BUG.B93 — single source of truth for the sidebar's active-link
// highlight, called from the one place every page load goes through
// (navigateToPage, above) plus from the internal "old bookmark" redirect
// branches in the dispatch above that rewrite window.location.hash to a
// *different* page than the one requested (e.g. #gpu-pool -> #system,
// #logs?tab=permissions -> #compliance-center) — those never re-run
// navigateToPage (there's no hashchange listener; only popstate), so
// without an explicit call here they left the sidebar showing no active
// item, or the wrong one, after landing.
function setActiveSidebarItem(hash) {
    document.querySelectorAll(".nav-link").forEach(link => {
        link.classList.toggle("active-link", link.getAttribute("href") === hash);
    });
}

export async function updateUIVisibility() {
    try {
        const user = await me.get();
        const permissions = await me.getPermissions();
        userPermissions = new Set(permissions.map(p => p.name));

        // Update user display in header
        updateUserDisplay(user);

        // Check if user should see Permissions tab
        const isSuperAdmin = user.parent_user_id === null;
        // #15 (round 7) — separate from isSuperAdmin: a tenant admin is
        // also `parent_user_id === null` (root of their tenant), but
        // they are NOT a platform admin. Use is_platform_admin for any
        // gating that's specifically about "runs the SaaS, never
        // subscribes" (Plans / Pricing / Upgrade visibility).
        const isPlatformAdmin = !!user.is_platform_admin;
        // Check for any manage_permissions capability across resource types
        // Capability names are now in format "resource_type:capability_name"
        const hasManagePermission = Array.from(userPermissions).some(p =>
            p.endsWith(':manage_permissions') ||
            p.endsWith(':manage') ||
            p === 'user:change_role' ||
            p === 'role:edit'
        );
        const hasFolderOwnership = await checkIfUserOwnsAnyFolders(user.id);

        // Show/hide Permissions tab
        const permissionsNavLink = document.getElementById('permissions-nav-link');
        if (permissionsNavLink) {
            if (isSuperAdmin || hasManagePermission || hasFolderOwnership) {
                permissionsNavLink.style.display = 'flex';
            } else {
                permissionsNavLink.style.display = 'none';
            }
        }

        // System page is now visible to EVERY user: a regular member gets
        // System Health + Capture Devices only, while Usage Stats and the admin
        // sub-tabs stay gated inside analytics.js. Record whether this user has
        // the broader access (super admin / manage / folder ownership) that
        // unlocks Usage Stats there.
        window.__systemFullAccess = !!(isSuperAdmin || hasManagePermission || hasFolderOwnership);
        const systemNavLink = document.getElementById('system-nav-link');
        if (systemNavLink) {
            systemNavLink.style.display = 'flex';
        }

        // Show/hide Logs tab (same criteria as Permissions)
        const logsNavLink = document.getElementById('logs-nav-link');
        if (logsNavLink) {
            if (isSuperAdmin || hasManagePermission || hasFolderOwnership) {
                logsNavLink.style.display = 'flex';
            } else {
                logsNavLink.style.display = 'none';
            }
        }

        // GY.C16 — Auditor access nav. Platform-admin only: the
        // /compliance/auditor-grants routes are hard-gated on
        // is_platform_admin server-side, so anyone else would only see
        // 403s.
        const auditorAccessNavLink = document.getElementById('auditor-access-nav-link');
        if (auditorAccessNavLink) {
            auditorAccessNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // GY.V5 follow-up — Parental consent: 0b.2 redo (R37.4 part 2, owner
        // 10-08) moved it from a sidebar link to a tab of the Users page, so
        // this id is gone from dashboard-shell.html and the block below is
        // inert. Its gate (isPlatformAdmin) now lives in users.js _CHILD_GATE
        // (plus the CE carve, which users.js applies via _childAllowed).

        // Users nav — tenant admins (parent_user_id === null) and
        // platform admins only. The prior gate also revealed it to any
        // user with `:manage` permissions or folder ownership; those
        // users hit 403s on every Users-page admin endpoint
        // (/tenants, /rbac/roles, /users/invitations/…), spamming the
        // console. The Permissions and System nav entries still use
        // the broader gate because they expose folder-scoped surfaces.
        const usersNavLink = document.getElementById('users-nav-link');
        if (usersNavLink) {
            if (isSuperAdmin || isPlatformAdmin) {
                usersNavLink.style.display = 'flex';
            } else {
                usersNavLink.style.display = 'none';
            }
        }

        // GSEC FX.74 p4 — Held Matches nav (review of accounts an offline OFAC SDN screening
        // match HELD for review). Mirrors the API guard js/held-matches.js reads, TENANT half
        // included: routers/users.py lists on require_capability(USER,'view') and
        // services/users_router_service.resolve_target_user_for_admin lets a platform admin act
        // in ANY tenant while a tenant admin decides inside their own via the user:reactivate /
        // user:deactivate grant — so this is the Users/Data Requests gate (isSuperAdmin ||
        // isPlatformAdmin), not the platform-admin-only gate the elevation nav above uses.
        // js/held-matches.js drops read-only on a 403, so a deep link is never decisive.
        // R37.4 part 2 — Held Matches is now a tab of the Users page (gate isSuperAdmin
        // || isPlatformAdmin lives in users.js _CHILD_GATE); this sidebar id is gone
        // from dashboard-shell.html, so the block below is inert.

        // GSEC R-120 + GY.C10 — Data Requests (tenant DSAR export / erasure)
        // and Access Reviews (campaigns + SoD report). Both route families
        // are tenant-admin-or-platform-admin only server-side, so mirror the
        // Users nav gate; anything else would only ever see 403s.
        // R37.4 part 2 — Data Requests is a Users-page tab (gate isSuperAdmin ||
        // isPlatformAdmin lives in users.js _CHILD_GATE); sidebar id gone, inert.
        const accessReviewsNavLink = document.getElementById('access-reviews-nav-link');
        if (accessReviewsNavLink) {
            accessReviewsNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // GSEC FX.153 p3 — Staff register nav. Platform-admin only (stricter
        // than the Users/Data Requests nav gate): routers/staff_register.py
        // gates every /platform/staff-records route on _require_platform_admin.
        // R37.4 part 2 — Staff register is a Users-page tab (gate isPlatformAdmin
        // lives in users.js _CHILD_GATE); sidebar id gone, inert.

        // GSEC FX.93 p3 — Abuse-report queue nav. Platform-admin only: routers/abuse_reports.py
        // gates every /admin/abuse/reports route on require_platform_admin, so the nav mirrors
        // the staff-register gate.
        const abuseQueueNavLink = document.getElementById('abuse-queue-nav-link');
        if (abuseQueueNavLink) {
            abuseQueueNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // 0b.2 redo (R37.4 part 2) — Listing review is a tab of the Marketplace
        // page, not a sidebar child: store-page-nav-link is gone from
        // dashboard-shell.html, so this block is inert. js/marketplace.js
        // applies the SAME role expression that used to live here (isPlatformAdmin)
        // to the tab strip; the parent Marketplace entry keeps its own gate.

        // GSEC FX.50 p3a — Security-incident tracker nav. Platform-admin only:
        // routers/platform_security_incidents.py gates every /platform/security-incidents
        // route on require_platform_admin, so the nav mirrors the staff-register gate.
        const securityIncidentsNavLink = document.getElementById('security-incidents-nav-link');
        if (securityIncidentsNavLink) {
            securityIncidentsNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // GSEC FX.73 p2 (Q56=A) — Licence manual-review queue nav. Platform-admin
        // only: routers/platform_licence_review.py gates every
        // /platform/licence-review route on require_platform_admin, so the nav
        // mirrors the security-incidents gate.
        const licenceReviewNavLink = document.getElementById('licence-review-nav-link');
        if (licenceReviewNavLink) {
            licenceReviewNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // 0b.2 redo (R37.4 part 2, owner 10-08): "AI in this platform" is no
        // longer a sidebar link — it is a tab of the Help page. js/help.js
        // applies the SAME role gate (is_platform_admin off /me) to its tab
        // strip, so this per-link block is inert (the id is gone from
        // dashboard-shell.html).

        // GSEC FX.144 p4 — Release-approval queue nav. Platform-admin only:
        // routers/release_approvals.py gates every /platform/release-approvals
        // route on require_platform_admin, so the nav mirrors the ai-inventory
        // gate.
        const releaseApprovalsNavLink = document.getElementById('release-approvals-nav-link');
        if (releaseApprovalsNavLink) {
            releaseApprovalsNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // GSEC FX.106 p2b — Blocked-IPs nav. Platform-admin only:
        // routers/platform_ip_blocks.py gates every /platform/security/ip-blocks
        // route on require_platform_admin, so the nav mirrors the release-approvals
        // gate.
        const ipBlocksNavLink = document.getElementById('ip-blocks-nav-link');
        if (ipBlocksNavLink) {
            ipBlocksNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // FX.71 p2 — Platform access-review campaigns nav. R39.8 (owner 10-08): the
        // standalone #platform-access-review sidebar row is GONE from dashboard-shell.html
        // — Platform access review is now the 11th tab of the Security hub. The role gate
        // this row used (display:flex for isPlatformAdmin only, mirroring the router's
        // platform-admin gate on every /admin/platform-access-reviews route) now lives in
        // js/security-hub.js _TAB_GATE['platform-access-review'] = 'platform'. This block
        // is inert (id gone from the shell).

        // FX.125 p3 — Platform key inventory nav. Platform-admin only:
        // routers/admin_keys.py gates GET /admin/keys/inventory on require_platform_admin
        // (non-admins 403 + audit), so the nav mirrors the platform-access-review gate.
        const keyInventoryNavLink = document.getElementById('key-inventory-nav-link');
        if (keyInventoryNavLink) {
            keyInventoryNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // GSEC FX.63 p3 — Outbound connections nav. Platform-admin only:
        // routers/admin_telemetry.py gates GET /admin/telemetry/preview on
        // require_platform_admin (non-admins 403 + audit), so the nav mirrors
        // the key-inventory gate.
        const outboundConnectionsNavLink = document.getElementById('outbound-connections-nav-link');
        if (outboundConnectionsNavLink) {
            outboundConnectionsNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // GSEC FX.70 p3 — Support tickets nav. NOT role-gated: routers/tenant/
        // support_tickets.py gates both /tenant/support-tickets routes on
        // require_active_and_onboarded_user only, so every signed-in tenant
        // user sees the link (the page itself surfaces "Tenant not found" for
        // an account with no tenant_id).
        // R37.4 part 2 — Support is a Help-page tab (ungated gate lives in
        // help.js _TAB_GATE); sidebar id gone, inert.

        // FX.98 p3 — Likeness consent register nav. Tenant-admin only: the
        // /tenant/likeness-consents routes are hard-gated server-side on
        // the shared _is_tenant_admin idiom, so mirror the Users nav gate;
        // anything else would only ever see 403s.
        // R37.4 part 2 — Consent register is a Users-page tab (gate isSuperAdmin ||
        // isPlatformAdmin lives in users.js _CHILD_GATE); sidebar id gone, inert.

        // FX.67 p2 — Lockbox access requests nav. Tenant-admin only: the
        // /tenant/lockbox/requests routes are hard-gated server-side on the
        // shared _is_tenant_admin idiom, so mirror the consent-register gate;
        // anything else would only ever see 403s.
        const lockboxApprovalNavLink = document.getElementById('lockbox-approval-nav-link');
        if (lockboxApprovalNavLink) {
            lockboxApprovalNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // FX.101 p4 — Sign-in lockdown nav. Tenant-admin only: the write
        // route PUT /tenant/security/ip-allowlist is gated server-side on
        // require_capability(TENANT, "manage_settings") (platform admin OR the
        // tenant's root admin), so mirror the consent-register gate; anything
        // else would only ever see 403s.
        const securityLockdownNavLink = document.getElementById('security-lockdown-nav-link');
        if (securityLockdownNavLink) {
            securityLockdownNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // 0b.2 redo (R37.4 part 2) — Log Streaming is a tab of the Logs page,
        // not a sidebar child: log-streams-nav-link is gone from
        // dashboard-shell.html, so this block is inert. The tab's visibility
        // is gated in js/logs.js with the SAME role expression that used to
        // live here: (isSuperAdmin || isPlatformAdmin). The parent Logs entry
        // keeps its own gate (logs-nav-link, unchanged).

        // GSEC FX.112 p3c — Security Alert Rules nav. Tenant-admin only:
        // routers/tenant/security.py gates the GET on
        // require_capability(TENANT, "view_settings") and the PUT on
        // require_capability(TENANT, "manage_settings"), so mirror the
        // log-streams gate; anything else would only ever see 403s on the
        // writes (js/alert-rules.js also drops to read-only on a 403).
        const alertRulesNavLink = document.getElementById('alert-rules-nav-link');
        if (alertRulesNavLink) {
            alertRulesNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // GSEC FX.112 p3d — Security Alerts nav (the alerts the rules above
        // actually raised, plus the ack). Tenant-admin only: routers/tenant/
        // security_alerts.py gates the GET on require_capability(TENANT,
        // "view_settings") and the ack POST on require_capability(TENANT,
        // "manage_settings"), so this mirrors the alert-rules gate exactly —
        // a viewer without manage permission can still deep-link (#security-
        // alerts) and js/security-alerts.js drops to read-only on the 403.
        const securityAlertsNavLink = document.getElementById('security-alerts-nav-link');
        if (securityAlertsNavLink) {
            securityAlertsNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // GSEC FX.112 p4a — Active Sessions nav. NOT gated: the page is self-service
        // (GET/DELETE /me/sessions + POST /me/signout-everywhere need no admin
        // capability), so every signed-in user manages their own sessions; the
        // tenant-admin user picker inside the page is what is capability-gated
        // (js/active-sessions.js checks USER:deactivate off /me/permissions).
        const activeSessionsNavLink = document.getElementById('active-sessions-nav-link');
        if (activeSessionsNavLink) {
            activeSessionsNavLink.style.display = 'flex';
        }

        // GSEC FX.78 p5 — Tenant Identity Providers nav. Tenant-admin only:
        // routers/sso_idp.py gates every /sso/idp route behind
        // require_sso_enabled_plan + services/sso_idp_service.require_tenant_admin
        // (cross-tenant reads 404, never 403), so mirror the alert-rules /
        // security-alerts gate exactly — a viewer without write permission can
        // still deep-link (#tenant-idp) and js/tenant-idp.js drops to read-only
        // on the 403. The plan gate (402) is the server's, surfaced as an error
        // line, not a hidden link.
        const tenantIdpNavLink = document.getElementById('tenant-idp-nav-link');
        if (tenantIdpNavLink) {
            tenantIdpNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }

        // GSEC FX.120 p5a — Elevation Requests nav (the JIT production-admin
        // elevations + the pending queue). Platform-admin ONLY: routers/
        // platform_elevation.py gates all six routes on require_platform_admin
        // (the same gate platform_ip_blocks / platform_security_alerts use), so
        // this mirrors the dsar-requests gate BELOW it, not the tenant-admin
        // gate the security-alerts link above uses — a tenant admin would only
        // ever see 403s. js/elevation-requests.js also drops read-only on a
        // 403, so a deep link (#elevation-requests) is safe but never decisive.
        const elevationRequestsNavLink = document.getElementById('elevation-requests-nav-link');
        if (elevationRequestsNavLink) {
            elevationRequestsNavLink.style.display = isPlatformAdmin ? 'flex' : 'none';
        }

        // FX.42 — Data Subject Requests (platform DSAR tracker).
        // Platform-admin only: the router is hard-gated server-side.
        // R37.4 part 2 — Data subject requests is a Users-page tab (gate isPlatformAdmin
        // lives in users.js _CHILD_GATE); sidebar id gone, inert.

        // GY.C1 — Compliance (profiles) nav. R39.7 (owner 10-08): the standalone
        // #compliance sidebar row is GONE from dashboard-shell.html — Compliance
        // profiles is now the 7th tab of the Compliance hub. The role gate that
        // row used (display:flex for isPlatformAdmin only, mirroring the router's
        // platform-admin gate on the PUT) now lives in hub-compliance.js
        // _TAB_GATE['compliance'] = 'platform', and the CE hide that _hideCeComplianceNav
        // applied to this id (now null here) lives in the hub's _CE_R397_TABS.
        // Auditors still reach the read-only view via the #compliance deep link,
        // which aliases to hub+tab above; compliance.js still gates Save on
        // me.get().is_platform_admin. This block is inert (id gone from the shell).

        // 0b.2 redo (R37.4, owner 10-08: "tabs put within the main tab", NOT
        // sidebar segments) — Security and Compliance each get ONE sidebar
        // entry whose page hosts an in-page tab strip (js/security-hub.js +
        // js/hub-compliance.js). The per-child nav-link blocks above are now
        // inert (those ids no longer exist in dashboard-shell.html — the
        // children live as tabs); the hubs read /me + /platform/features
        // themselves and apply the SAME role/CE gates to their tab strip, so
        // these two links only need "does the hub have anything to show".
        // Security: Active Sessions is ungated self-service, so the hub always
        // has a visible tab for a signed-in user (R39.8 added Platform access review
        // as the 11th tab — platform-admin-gated, so it never makes the hub empty).
        // Compliance: all seven tabs are admin-gated (R39.7 added Compliance profiles
        // as the 7th), so the entry follows the tenant/platform-admin shape
        // (a CE build hides the two carved tabs but four remain visible).
        // rc20-ce-nav (owner 11-04) — NO-FLICKER: these two hub rows are NOT
        // revealed here any more. They start display:none in dashboard-shell.html
        // and _applyCePaidHubNav() (below, after the edition resolves) is the ONLY
        // thing that reveals them — and only on a POSITIVE paid edition plus the
        // role gate the old sync block used (Security: always for a signed-in user;
        // Compliance: tenant/platform admin). Painting them 'flex' here and hiding
        // them after the async edition check is exactly the "shows up then
        // disappears" flicker Tim saw.

        // FX.162.p3 (owner Q157: MOD CE must not do anything with compliance)
        // — the FIFTH of the five compliance nav items is gated here too, but
        // the EDITION check is one shared helper (_hideCeComplianceNav, next
        // to the _CE_COMPLIANCE_PAGES list used by navigateToPage's page gate)
        // so the five role gates above stay untouched and CE hides all five
        // links in one place. Non-blocking + FAIL-CLOSED (rc20 failopen-a):
        // the links start display:none in dashboard-shell.html, so a late
        // hide is harmless, and an unreadable payload HIDES all five.
        _hideCeComplianceNav();

        // FX.144 p5 (owner 10-07, Q157=A) — the release-approval queue nav is
        // hidden on CE by the SAME edition predicate, through its own helper
        // (its role gate above stays untouched; this only ever hides).
        _hideCePaidComplianceNav();

        // Capture Devices moved INTO the System page as a sub-tab (visible to
        // every user there — enrol still gates on require_active_and_onboarded_user).
        // Hide the standalone sidebar entry.
        const captureNavLink = document.getElementById('capture-devices-nav-link');
        if (captureNavLink) {
            captureNavLink.style.display = 'none';
        }

        // Show/hide Settings tab (super admin only)
        const settingsNavLink = document.getElementById('settings-nav-link');
        if (settingsNavLink) {
            if (isSuperAdmin) {
                settingsNavLink.style.display = 'flex';
            } else {
                settingsNavLink.style.display = 'none';
            }
        }

        // Outbound Webhooks — BUG.B36c: the Settings → Integrations → Outbound
        // Webhooks sub-tab is RETIRED. Every webhook lives in the ONE "Webhooks"
        // section on the Apps tab (System → Apps → Webhooks, B36a/B36b). The
        // standalone sidebar link stays hidden and the #webhooks hash route now
        // redirects to that Apps tab instead of initializing a page in place.
        const webhooksNavLink = document.getElementById('webhooks-nav-link');
        if (webhooksNavLink) {
            webhooksNavLink.style.display = 'none';  // retired → Apps tab (BUG.B36c)
        }

        // Storage Connections (BYO-S3) — consolidated into the Settings →
        // Storage tab. Hide the standalone sidebar link; the #storage hash
        // route still resolves for existing bookmarks.
        const storageNavLink = document.getElementById('storage-nav-link');
        if (storageNavLink) {
            storageNavLink.style.display = 'none';
        }

        // GX.A4 — Apps: 0b.2 redo (R37.4 part 2, owner Q6 10-08) demoted it from
        // its own sidebar entry to a main tab on the System page (left of
        // Settings). This nav-link id is GONE from dashboard-shell.html, so the
        // block above is inert; the SAME isSuperAdmin gate now lives in
        // analytics.js checkUserRole() (reveals #main-tab-apps) and in the
        // System page's deep-link fallback, so the Apps tab stays hidden for
        // non-tenant-root users. The System sidebar link itself is ungated.

        // Burst has no sidebar entry — it is a System sub-tab, between
        // Workers and Resource Monitor, gated to platform admins by
        // checkUserRole() in analytics.js.

        // Consolidated admin Billing entry (Plans / Payments / Custom
        // Plans / Stripe Settings tabs). Plat-admin only. The legacy
        // standalone `admin-plans-nav-link` and `payments-admin-nav-link`
        // are hidden — their hash routes still resolve via the page tabs
        // so existing bookmarks survive. Distinct from the existing
        // `billing-nav-link` (which is the tenant-facing Upgrade link).
        // rc20-ce-nav (owner 11-04) — NO-FLICKER: this row is NOT revealed here any
        // more. It starts display:none in dashboard-shell.html and _applyCePaidHubNav()
        // (below, after the edition resolves) is the ONLY thing that reveals it — and
        // only on a POSITIVE paid edition plus the role gate the old sync block used
        // ('admin-billing-nav-link': isPlatformAdmin). Painting it 'flex' here and
        // hiding it after the async edition check is exactly the "shows up then
        // disappears" flicker Tim saw on his CE install (the paid hub row offered on
        // CE); CE and unknown editions keep it display:none from first paint forever.
        // node-ownership-purchases — Nodes & Packages admin (price nodes, demos,
        // bundles). Relocated from a standalone sidebar entry into a tab on the
        // consolidated Billing page (after Stack Billing). Hide the sidebar
        // link; the #admin-nodes hash route still resolves via the page tab so
        // existing bookmarks survive. Access is via the Billing entry, which is
        // already platform-admin gated.
        const adminNodesNavLink = document.getElementById('admin-nodes-nav-link');
        if (adminNodesNavLink) {
            adminNodesNavLink.style.display = 'none';  // consolidated into #billing
        }
        // service-stack-builder Group G — reservation-rate editor. Relocated
        // from a standalone sidebar entry into a tab on the consolidated
        // Billing page (after Overage Caps). Hide the sidebar link; the
        // #stack-billing hash route still resolves via the page tab so
        // existing bookmarks survive.
        const stackBillingNavLink = document.getElementById('stack-billing-nav-link');
        if (stackBillingNavLink) {
            stackBillingNavLink.style.display = 'none';  // consolidated into #billing
        }
        // WSIP 6.16 (task 618) — Drift Events sidebar entry. There is no drift
        // key in /platform/features (the tenant switch lives in the platform-
        // settings registry: drift.drift_detection_enabled, read server-side by
        // routers/drift_events.py). routers/drift_events.py gates its routes on
        // require_bearer_or_api_key only (no role check beyond tenant_id scope),
        // so the nav mirrors the consent-register/lockbox-approval idiom for
        // tenant-accessible admin-facing routes: isSuperAdmin || isPlatformAdmin.
        // The page itself still answers "not enabled for this tenant" when the
        // tenant switch is OFF.
        const driftEventsNavLink = null; // CE: nav row denied
        if (driftEventsNavLink) {
            driftEventsNavLink.style.display = (isSuperAdmin || isPlatformAdmin) ? 'flex' : 'none';
        }
        // rc20-uijs-drift — the CE-retired admin aliases (gpu-pool / pricing / admin-scim /
        // payment-settings / access-reviews / the denied drift view) must not be OFFERED
        // in the sidebar on CE either: the denied drift view's row above (the WSIP 6.16 tenant-admin idiom)
        // and access-reviews' (ui.js:1437) still carry live role gates, so ONE shared helper
        // hides every denied row (the DERIVED `_ceDeniedPageIds()` set) with the SAME fail-closed predicate the
        // direct-URL panel gate uses (ui.js:701) — nav and panel can never disagree. Runs AFTER
        // all the role gates above, only ever hides, and HIDES when the edition is unknown.
        _hideCeRetiredAdminNav();
        const paymentsAdminNavLink = document.getElementById('payments-admin-nav-link');
        if (paymentsAdminNavLink) {
            paymentsAdminNavLink.style.display = 'none';  // consolidated into #billing
        }

        // add-self-service-sso §3.9 — SSO sidebar entry. Tenant
        // admins (parent_user_id IS NULL) + platform admins see it.
        // The server-side plan gate (402 from /sso/idp) handles the
        // Pro-tier requirement; the JS in admin-sso.js surfaces a
        // friendly "not included in this plan" state in that case so the link
        // is still informative on lower tiers.
        // Identity & Access — single sidebar entry covering SSO and
        // SCIM. The in-page tab nav lets the user pivot. Tenant admins
        // (parent_user_id IS NULL) + platform admins see it. The
        // per-feature server-side plan gates still apply when the user
        // navigates into a specific tab.

        // #15 (round 8) — Tenants see plans + checkout under "Upgrade"
        // (combined in billing.html). The standalone "Pricing" nav
        // entry from round 6 is dead; keep it hidden.
        const pricingNavLink = document.getElementById('pricing-nav-link');
        const adminPlansNavLink = document.getElementById('admin-plans-nav-link');
        if (pricingNavLink) {
            pricingNavLink.style.display = 'none';
        }
        if (adminPlansNavLink) {
            adminPlansNavLink.style.display = 'none';  // consolidated into #billing
        }

        // GPU Pool was relocated to a System page sub-tab — hide the
        // standalone sidebar entry. The hash route `#gpu-pool` still
        // resolves below via the redirect in the navigation block, so
        // existing bookmarks keep working.
        const gpuPoolNavLink = document.getElementById('gpu-pool-nav-link');
        if (gpuPoolNavLink) {
            gpuPoolNavLink.style.display = 'none';
        }

        // Show Upgrade/Billing + Marketplace in the sidebar only when:
        //   - SaaS billing is enabled (features.billing_enabled)
        //   - The user is the TENANT admin (isSuperAdmin — i.e.
        //     parent_user_id is null, the root of their tenant);
        //     invited users within a tenant don't choose plans or
        //     install marketplace items.
        //   - The user is NOT a platform admin (they run the SaaS,
        //     they're not a tenant on a plan).
        const billingNavLink = document.getElementById('billing-nav-link');
        // dashboard-load-speed: /platform/features only decides the
        // billing-nav-link visibility (hidden on onprem anyway), so it must
        // NOT sit on the blank-screen critical path (updateUIVisibility is
        // awaited before showPage(dashboard) in auth.js). Fire it
        // non-blocking via the coalesced 30s cache and flip the nav when it
        // resolves — the link starts hidden, so a late reveal is harmless.
        if (billingNavLink) {
            getPlatformFeatures().then((features) => {
                window._platformFeatures = features;
                // rc20-ce-nav (owner 11-04) — NO-FLICKER: the fetch above stays (it is
                // the only writer of window._platformFeatures, read by users.js), but
                // this row is NOT revealed here. It starts display:none in dashboard-
                // shell.html and _applyCePaidHubNav() is the ONLY revealer — on a
                // POSITIVE paid edition plus ('billing-nav-link': (billingEnabled &&
                // !isPlatformAdmin && isSuperAdmin)). Any CE install reporting
                // billing_enabled true would otherwise flicker this row in.
                // Marketplace moved to the WorkflowBuilder functions menu.
            }).catch(() => { /* leave hidden */ });
        }

        // dashboard-marketplace-tabs — the Marketplace nav link is now
        // always visible (it hosts Library + My Listings, both ungated
        // for every tenant user). The GG.RS.6 on-prem "Browse" remote-
        // catalog tab inside that page is still gated on deployment_mode
        // != 'saas', but that check now lives in js/marketplace.js
        // (toggling the Browse tab button), not here.

        // Credit balance indicator in the top header. Non-blocking for the
        // same reason as above — it must never delay first paint.
        renderHeaderCredits(isPlatformAdmin);

        // Note: Workers tab was moved to be a tab within System page (analytics.html)
        // Keeping the Workers nav link hidden since it's now accessible via System > Workers tab
        // The Workers tab visibility is now controlled by analytics.js

        // Note: Pricing-related pages (Pricing Config, Promo Codes, Free Trials, Geographic, Seasonal)
        // are now tabs within the Billing page, not separate sidebar items
        // Their visibility is controlled by billing.js

        // Show/Hide Workflow Builder link based on workflow:create permission
        const workflowBuilderLink = document.getElementById('workflow-builder-link');
        if (workflowBuilderLink) {
            const hasWorkflowCreate = Array.from(userPermissions).some(p =>
                p === 'workflow:create' || p === 'workflow:edit' || p === 'workflow:manage_permissions'
            );
            if (hasWorkflowCreate) {
                workflowBuilderLink.style.display = 'flex';
                // node-monetization-finish 4.3 — "N new nodes to buy" nudge.
                // Marketplace lives inside the WorkflowBuilder functions menu
                // now (see the billing/marketplace note above), so the badge
                // sits on the sidebar link that opens it. Platform admins run
                // the store, they don't buy from it, so they never see it.
                if (!isPlatformAdmin) {
                    _updateMarketplaceNudgeBadge(workflowBuilderLink);
                } else {
                    _clearMarketplaceNudgeBadge(workflowBuilderLink);
                }
            } else {
                workflowBuilderLink.style.display = 'none';
            }
        }

        // rc20-ce-nav (owner 11-04) — reveal the paid hub rows ONLY on a positive
        // paid edition (plus each row's role gate); CE/unknown keep them hidden
        // from first paint (no render-then-remove flicker). Runs after every
        // synchronous role gate above and before the final group-heading sync.
        _applyCePaidHubNav(isSuperAdmin, isPlatformAdmin);

        // FIX-CE-DASH-HIDE-STORE-NAV (owner 11-16) — runs right after the paid-hub
        // reveal so the CE store entries (marketplace-nav-link and the store-page /
        // store-page / store-page ids, which ship no anchor today but stay covered
        // if one is ever added) end display:none on CE and unknown editions. Only
        // ever hides, fail-closed, so paid behaviour is byte-identical.
        _hideCeStoreNav();

        // 0b.2 s1 (R37.4) — after every synchronous role gate above has run, hide
        // any nav group whose links are ALL hidden (CE/non-admin users never see
        // an empty heading). The two CE helpers re-run this when they hide.
        _syncNavGroupHeadings();
    } catch(e) {
        console.error("Could not update UI visibility", e);
    }
}

// node-monetization-finish 4.3 — badge on the Workflow Builder sidebar link
// showing how many marketplace nodes are new + unowned since the tenant last
// opened the store (GET /node-definitions/new-count). Cleared server-side by
// The store dialog's Browse tab (POST /node-definitions/marketplace/seen),
// so this just reflects whatever the backend currently reports — no local
// dismiss state to keep in sync. Injected purely in JS (no matching element
// in dashboard-shell.html) so it fails silently wherever the link is absent.
async function _updateMarketplaceNudgeBadge(link) {
    // FIX-CE-DASH-HIDE-STORE-NAV (owner 11-16, no store scope in CE) — the "N new
    // nodes to buy in the Marketplace" nudge is a store OFFER, so it must never
    // paint on CE. SAME fail-closed predicate as every CE gate here (_isCeEdition:
    // unknown counts as CE), checked before the fetch, so CE never issues the
    // /nodes/new-count request and never appends the badge. Paid (positive saas/
    // pro) keeps the byte-identical path below; the paid callsite is untouched.
    try {
        if (await _isCeEdition()) { _clearMarketplaceNudgeBadge(link); return; }
    } catch (e) { _clearMarketplaceNudgeBadge(link); return; }
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        if (!token) { _clearMarketplaceNudgeBadge(link); return; }
        const resp = await fetch(`${apiUrl}/nodes/new-count`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { _clearMarketplaceNudgeBadge(link); return; }
        const { new_count } = await resp.json();
        if (new_count > 0) {
            let badge = link.querySelector('.marketplace-new-badge');
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'marketplace-new-badge ml-auto inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full bg-red-500 text-white text-[11px] font-semibold';
                link.appendChild(badge);
            }
            badge.textContent = new_count > 99 ? '99+' : String(new_count);
            badge.title = `${new_count} new node${new_count === 1 ? '' : 's'} available in the Marketplace`;
        } else {
            _clearMarketplaceNudgeBadge(link);
        }
    } catch (e) {
        _clearMarketplaceNudgeBadge(link);
    }
}

function _clearMarketplaceNudgeBadge(link) {
    const badge = link.querySelector('.marketplace-new-badge');
    if (badge) badge.remove();
}

async function checkIfUserOwnsAnyFolders(userId) {
    try {
        // Check if user owns any folders
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/assets?owner_id=${userId}`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            return false;
        }

        const folders = await response.json();
        return folders && folders.length > 0;
    } catch (error) {
        console.error("Error checking folder ownership:", error);
        return false;
    }
}


// --- Header credit balance indicator ---
// Always-visible credit balance in the top header. Onprem plans have no
// credit metering (features.credits_enabled === false) so we show
// "Unlimited"; on SaaS we show the remaining credit count from
// /subscriptions (the same endpoint the usage widget already calls). All
// work is non-blocking and fails silently — the pill stays hidden if we
// can't resolve a value, never a broken/blank number.
async function renderHeaderCredits(isPlatformAdmin) {
    const wrap = document.getElementById('header-credit-indicator');
    const valueEl = document.getElementById('header-credit-value');
    if (!wrap || !valueEl) return;
    // CE: credit metering is a paid concept, so the pill stays hidden (no
    // "Unlimited" text, no /subscriptions fetch). Paid editions fall through.
    if (await _isCeEdition()) return;

    const show = (text, title) => {
        valueEl.textContent = text;
        if (title) wrap.setAttribute('title', title);
        wrap.classList.remove('hidden');
        wrap.classList.add('flex');
    };

    try {
        const features = await getPlatformFeatures();
        // Credits not metered (onprem, or SaaS with credits disabled) →
        // effectively unlimited. Platform admins also never consume credits.
        if (!features || features.credits_enabled !== true || isPlatformAdmin) {
            // No billing page to deep-link to when billing is disabled.
            if (!features || features.billing_enabled !== true) {
                wrap.removeAttribute('href');
                wrap.classList.add('cursor-default');
            }
            show('Unlimited', 'Credits: unlimited on this deployment');
            return;
        }

        // Credits enabled → show remaining balance from /subscriptions.
        const sub = await fetchApi('/subscriptions');
        if (sub && typeof sub.credits_remaining === 'number') {
            const remaining = sub.credits_remaining.toLocaleString();
            const total = typeof sub.credits_total === 'number'
                ? sub.credits_total.toLocaleString() : null;
            show(`${remaining} credits`,
                total ? `${remaining} of ${total} credits remaining` : `${remaining} credits remaining`);
        }
        // No subscription / unexpected shape → leave the pill hidden.
    } catch (e) {
        // Non-fatal — leave the indicator hidden rather than show a broken value.
    }
}

// --- User Display ---
function updateUserDisplay(user) {
    // Get display name (prefer full name, fallback to username/email)
    const displayName = user.first_name && user.last_name
        ? `${user.first_name} ${user.last_name}`
        : user.username || user.email || 'User';
    const email = user.email || '';

    // Get initials for avatar
    const initials = getInitials(displayName);

    // Determine role label
    const isSuperAdmin = user.parent_user_id === null;
    let roleLabel = user.role?.name || 'User';
    if (isSuperAdmin) {
        roleLabel = 'Super Admin';
    }

    // Update header display
    const displayNameEl = document.getElementById('user-display-name');
    const displayEmailEl = document.getElementById('user-display-email');
    const avatarInitialsEl = document.getElementById('user-avatar-initials');

    if (displayNameEl) displayNameEl.textContent = displayName;
    if (displayEmailEl) displayEmailEl.textContent = email;
    if (avatarInitialsEl) avatarInitialsEl.textContent = initials;

    // Update dropdown menu
    const menuNameEl = document.getElementById('menu-user-name');
    const menuEmailEl = document.getElementById('menu-user-email');
    const roleBadgeEl = document.getElementById('user-role-badge');
    const tenantBadgeEl = document.getElementById('user-tenant-badge');

    if (menuNameEl) menuNameEl.textContent = displayName;
    if (menuEmailEl) menuEmailEl.textContent = email;
    if (roleBadgeEl) {
        roleBadgeEl.textContent = roleLabel;
        // Color based on role
        if (isSuperAdmin) {
            roleBadgeEl.className = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-purple-100 text-purple-800';
        } else if (roleLabel === 'Admin') {
            roleBadgeEl.className = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-primary/20 text-primary';
        } else {
            roleBadgeEl.className = 'inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-800';
        }
    }
    if (tenantBadgeEl) {
        tenantBadgeEl.textContent = user.tenant?.name || `Tenant ${user.tenant_id}`;
    }
}

function getInitials(name) {
    if (!name) return '?';
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return name.substring(0, 2).toUpperCase();
}

function setupUserMenuListeners() {
    const menuButton = document.getElementById('user-menu-button');
    const dropdown = document.getElementById('user-menu-dropdown');
    const menuLogout = document.getElementById('user-menu-logout');

    if (menuButton && dropdown) {
        // Toggle dropdown on button click
        menuButton.addEventListener('click', (e) => {
            e.stopPropagation();
            dropdown.classList.toggle('hidden');
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!dropdown.contains(e.target) && e.target !== menuButton) {
                dropdown.classList.add('hidden');
            }
        });
    }

    // Logout from menu
    if (menuLogout) {
        menuLogout.addEventListener('click', handleLogout);
    }
}


// --- Generic Modal Functionality ---
// FX.20 R-037: fragments that need a user decision before continuing
// (confirmations / alerts) are announced as alertdialog; form-style
// modals stay role="dialog".
const ALERTDIALOG_MODAL_NAMES = new Set([
    'confirm-action',
    'alert-error',
    'alert-info',
    'alert-action',
    'delete-confirm'
]);

export async function loadModal(modalName) {
    const modalContainer = document.getElementById("modal-container");
    try {
        // CE-PG17: root-relative, NOT page-relative. The hash router's
        // replaceState can leave a nested pathname (e.g. /pages/...), where a
        // page-relative fetch resolved to /pages/modals/x.html; nginx's SPA
        // fallback then returned index.html (200, no modal element), so loadModal
        // returned null and every showInfo/showError fell back to window.alert.
        const modalContent = await fetch(`/modals/${modalName}.html`).then(res => res.text());
        modalContainer.innerHTML = modalContent;
        const modalId = `${modalName}-modal`;
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.style.display = 'block';
            // Dialog semantics + focus trap + focus return. dismissible:false
            // keeps the caller's own Escape/overlay/button close paths (which
            // route through closeModal → modA11y.detach) as the single owner
            // of dismissal; the helper only adds role/aria, the trap, and
            // focus return.
            modA11y.attach(modal, {
                role: ALERTDIALOG_MODAL_NAMES.has(modalName) ? 'alertdialog' : 'dialog',
                dismissible: false
            });
            return modal; // Return the modal element so other scripts can attach listeners to it.
        }
    } catch (error) {
        console.error(`Failed to load modal: ${modalName}`, error);
    }
    return null;
}

export function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modA11y.detach(modal);
        modal.style.display = 'none';
    }
    const modalContainer = document.getElementById("modal-container");
    if (modalContainer && modalContainer.contains(modal)) {
        modalContainer.innerHTML = '';
    }
}


// --- Themed error popup ---
// Drop-in replacement for browser `showInfo()` in error paths. Presents a
// centered card matching the Add Secret modal's look (white card, rounded,
// shadow, gray overlay). Resolves the returned Promise when the user
// dismisses the modal so callers can `await showError(...)` if they need
// to sequence UI after the acknowledgement — most won't.
export async function showError(message, title) {
    const modal = await loadModal('alert-error');
    if (!modal) {
        // Fallback to browser alert if modal couldn't load (offline/cache).
        // Plain alert, NOT window.showInfo: that IS showInfo, so a modal that
        // can't load (Dashboard down) recursed forever, refetching the modal
        // ~5 req/s until nginx rate-limited the whole site (2026-09-26).
        window.alert(typeof message === 'string' ? message : String(message));
        return;
    }
    const titleEl = modal.querySelector('#alert-error-title');
    const msgEl   = modal.querySelector('#alert-error-message');
    const okBtn   = modal.querySelector('#alert-error-ok');
    if (titleEl && title) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = typeof message === 'string' ? message : String(message);
    return new Promise(resolve => {
        const dismiss = () => {
            closeModal('alert-error-modal');
            resolve();
        };
        if (okBtn) okBtn.addEventListener('click', dismiss, { once: true });
        // Dismiss on overlay click too
        modal.addEventListener('click', (e) => {
            if (e.target === modal) dismiss();
        }, { once: true });
        // Escape key
        const onKey = (e) => {
            if (e.key === 'Escape') {
                document.removeEventListener('keydown', onKey);
                dismiss();
            }
        };
        document.addEventListener('keydown', onKey);
    });
}

// Themed info/success popup — same shape as showError, neutral styling.
// Drop-in replacement for `showInfo()` in non-error paths. All content
// goes through textContent so messages can't inject HTML.
export async function showInfo(message, title) {
    const modal = await loadModal('alert-info');
    if (!modal) {
        // Plain alert, NOT window.showInfo: that IS showInfo, so a modal that
        // can't load (Dashboard down) recursed forever, refetching the modal
        // ~5 req/s until nginx rate-limited the whole site (2026-09-26).
        window.alert(typeof message === 'string' ? message : String(message));
        return;
    }
    const titleEl = modal.querySelector('#alert-info-title');
    const msgEl   = modal.querySelector('#alert-info-message');
    const okBtn   = modal.querySelector('#alert-info-ok');
    if (titleEl && title) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = typeof message === 'string' ? message : String(message);
    return new Promise(resolve => {
        const dismiss = () => {
            closeModal('alert-info-modal');
            resolve();
        };
        if (okBtn) okBtn.addEventListener('click', dismiss, { once: true });
        modal.addEventListener('click', (e) => {
            if (e.target === modal) dismiss();
        }, { once: true });
        const onKey = (e) => {
            if (e.key === 'Escape') {
                document.removeEventListener('keydown', onKey);
                dismiss();
            }
        };
        document.addEventListener('keydown', onKey);
    });
}

// Themed replacement for browser `confirm()`. Returns a Promise<boolean>
// — true if the user clicked OK, false on Cancel / overlay / Esc.
// Callers must `await`:
//     if (!(await showConfirm('Delete this?'))) return;
export async function showConfirm(message, title) {
    const modal = await loadModal('confirm-action');
    if (!modal) {
        return window.confirm(typeof message === 'string' ? message : String(message));
    }
    const titleEl  = modal.querySelector('#confirm-action-title');
    const msgEl    = modal.querySelector('#confirm-action-message');
    const okBtn    = modal.querySelector('#confirm-action-ok');
    const cancelBtn = modal.querySelector('#confirm-action-cancel');
    if (titleEl && title) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = typeof message === 'string' ? message : String(message);
    return new Promise(resolve => {
        const done = (value) => {
            closeModal('confirm-action-modal');
            resolve(value);
        };
        if (okBtn) okBtn.addEventListener('click', () => done(true), { once: true });
        if (cancelBtn) cancelBtn.addEventListener('click', () => done(false), { once: true });
        modal.addEventListener('click', (e) => {
            if (e.target === modal) done(false);
        }, { once: true });
        const onKey = (e) => {
            if (e.key === 'Escape') {
                document.removeEventListener('keydown', onKey);
                done(false);
            } else if (e.key === 'Enter') {
                document.removeEventListener('keydown', onKey);
                done(true);
            }
        };
        document.addEventListener('keydown', onKey);
    });
}

// Themed alert with an optional secondary action button. Matches the
// Add New Secret modal style (flat title + subtitle, no icon, indigo
// primary / gray-300 secondary). Use this when an alert needs to give
// the user a one-click escape hatch to a related page (e.g. "Open Secrets
// Settings" from a Platform Configuration Required dialog).
//
// Returns a Promise that resolves to 'action' if the user clicked the
// action button, or 'ok' for any dismiss path (OK / overlay / Esc).
//
// Single-modal: assumes no other modal is open when called. The onAction
// handler is invoked synchronously before the Promise resolves so navigation
// can run immediately.
export async function showAlertWithAction({ title, message, actionLabel, onAction } = {}) {
    const modal = await loadModal('alert-action');
    if (!modal) {
        window.alert(typeof message === 'string' ? message : String(message));
        return 'ok';
    }
    const titleEl  = modal.querySelector('#alert-action-title');
    const msgEl    = modal.querySelector('#alert-action-message');
    const okBtn    = modal.querySelector('#alert-action-ok');
    const actionBtn = modal.querySelector('#alert-action-action');

    if (titleEl && title) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = typeof message === 'string' ? message : String(message);

    const hasAction = !!(actionLabel && typeof onAction === 'function');
    if (okBtn) {
        // When an action button is shown, the OK button becomes the secondary
        // "Close" affordance (gray-300) and the action button is the indigo
        // primary — mirroring add-secret.html's Cancel/Save pair. With no
        // action button, OK is the only control and stays indigo.
        if (hasAction) {
            okBtn.textContent = 'Close';
            okBtn.className = 'bg-gray-300 text-gray-700 px-4 py-2 rounded-md';
        } else {
            okBtn.textContent = 'OK';
            okBtn.className = 'bg-primary text-white px-4 py-2 rounded-md';
        }
    }
    if (actionBtn) {
        if (hasAction) {
            actionBtn.textContent = actionLabel;
            actionBtn.removeAttribute('hidden');
        } else {
            actionBtn.setAttribute('hidden', '');
        }
    }

    return new Promise(resolve => {
        let onKey;
        const cleanup = () => {
            closeModal('alert-action-modal');
            if (onKey) document.removeEventListener('keydown', onKey);
        };
        const dismiss = () => { cleanup(); resolve('ok'); };
        const act = () => {
            try { onAction(); } finally { cleanup(); resolve('action'); }
        };
        if (okBtn) okBtn.addEventListener('click', dismiss, { once: true });
        if (actionBtn && actionLabel && typeof onAction === 'function') {
            actionBtn.addEventListener('click', act, { once: true });
        }
        modal.addEventListener('click', (e) => {
            if (e.target === modal) dismiss();
        }, { once: true });
        onKey = (e) => {
            if (e.key === 'Escape') dismiss();
        };
        document.addEventListener('keydown', onKey);
    });
}

// Globally exposed so plain <script> pages can call window.showError(...)
// without importing ui.js. Kept even when ui.js is imported as a module —
// the module attaches it too so both call styles work.
if (typeof window !== 'undefined') {
    window.showError = showError;
    window.showInfo = showInfo;
    window.showConfirm = showConfirm;
    window.showAlertWithAction = showAlertWithAction;
}


// Mobile sidebar drawer helpers. The sidebar element starts with
// `-translate-x-full` so it's off-screen on mobile until toggled;
// `md:translate-x-0` keeps it docked on desktop regardless of state.
function openSidebarOnMobile() {
    const sidebar = document.getElementById("sidebar");
    const backdrop = document.getElementById("sidebar-backdrop");
    const toggle = document.getElementById("sidebar-toggle");
    if (!sidebar) return;
    sidebar.classList.remove("-translate-x-full");
    sidebar.classList.add("translate-x-0");
    if (backdrop) backdrop.classList.remove("hidden");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
}

function closeSidebarOnMobile() {
    const sidebar = document.getElementById("sidebar");
    const backdrop = document.getElementById("sidebar-backdrop");
    const toggle = document.getElementById("sidebar-toggle");
    if (!sidebar) return;
    sidebar.classList.add("-translate-x-full");
    sidebar.classList.remove("translate-x-0");
    if (backdrop) backdrop.classList.add("hidden");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
}

// R301-r2 — FAIL-CLOSED Sign Up gate (replaces the fail-open CE gate below).
// The old shape hid #signup-button only when _isCeEdition() resolved true, so an
// errored/unreadable entitlements block left Sign Up clickable on CE — the
// dash-ce-batch6.spec.ts:59 count()==0 assert was flaky on exactly that path.
// New rule: the button starts HIDDEN and is revealed only when the server's
// /platform/features payload POSITIVELY reports registration is allowed
// (entitlements.registration_allowed, the KC realm flag; edition guessing is
// the last resort and only ever reveals a positively-paid edition).
function _registrationAllowed() {
    // true = the server says self sign-up works; false = it says it does not;
    // null = unknown (never reveals, but a later payload can).
    return getPlatformFeatures().then((features) => {
        const ent = features && features.entitlements;
        if (!ent || ent.error) return null;
        if (typeof ent.registration_allowed === 'boolean') return ent.registration_allowed;
        return ent.edition === 'saas' || ent.edition === 'pro';
    }).catch(() => null);
}

function _revealSignupButtonWhenRegistrationAllowed() {
    document.getElementById("signup-button")?.style.setProperty('display', 'none');
    _registrationAllowed().then((allowed) => {
        if (allowed !== true) return;
        const btn = document.getElementById("signup-button");
        if (!btn) return;
        btn.style.removeProperty('display');
        document.getElementById("signup-button")?.addEventListener("click", handleSignup);
    });
}

// --- Event Listeners ---
export function attachEventListeners() {
    // These listeners are attached once on page load to static elements.
    document.getElementById("login-button")?.addEventListener("click", handleLogin);
    // 2400LR-ceSignup-a2 — CE ships registrationAllowed:false (open-core/overlay/
    // keycloak/core-realm.json:4), so Sign Up is a dead end there: the button is
    // shipped (pages/login.html:13) but hidden and listener-less. R301-r2: gated
    // fail-closed via _revealSignupButtonWhenRegistrationAllowed (above), NOT via
    // _isCeEdition() alone — the OLD predicate was fail-open (an errored
    // entitlements block hid nothing), which made the CE posture nondeterministic.
    // (rc20 failopen-a has since made _isCeEdition fail-closed too; this gate stays
    // registration_allowed-based, which is the precise signal.)
    _revealSignupButtonWhenRegistrationAllowed();
    document.getElementById("logout-button")?.addEventListener("click", handleLogout);
    document.getElementById("onboarding-form")?.addEventListener("submit", handleOnboardingSubmit);

    // User menu dropdown
    setupUserMenuListeners();

    // Sidebar navigation is handled via event delegation.
    document.getElementById("sidebar-nav")?.addEventListener("click", function (e) {
        const link = e.target.closest(".nav-link");
        if (link) {
            const href = link.getAttribute("href");
            if (href && href.startsWith("#")) {
                e.preventDefault();
                // Drop any stale ?tab= from the page we're leaving so the
                // destination page opens on its default tab. A refresh (F5)
                // does not pass through here, so the sub-tab is preserved
                // and restored in that case (#20).
                clearActiveTab();
                window.location.hash = href;
                navigateToPage(href);
                // Auto-close the sidebar drawer on mobile after a nav
                // pick, otherwise the new page is hidden behind it.
                closeSidebarOnMobile();
            }
        }
    });

    // Mobile-only sidebar drawer (launch-day-followup-6 §10.5). md+
    // viewports use the sidebar's static layout; below md it slides
    // in/out of the left edge via the `-translate-x-full` ↔
    // `translate-x-0` toggle and a backdrop scrim.
    const sidebarToggle = document.getElementById("sidebar-toggle");
    const sidebarBackdrop = document.getElementById("sidebar-backdrop");
    if (sidebarToggle) {
        sidebarToggle.addEventListener("click", openSidebarOnMobile);
    }
    if (sidebarBackdrop) {
        sidebarBackdrop.addEventListener("click", closeSidebarOnMobile);
    }
    document.getElementById("logout-button")?.addEventListener(
        "click", closeSidebarOnMobile,
    );

    // Handle browser back/forward buttons.
    window.addEventListener("popstate", () => navigateToPage(window.location.hash));

    // Handle Workflow Builder access
    document.getElementById("workflow-builder-link")?.addEventListener("click", async function(e) {
        e.preventDefault();

        // Check Harbor credentials before allowing access
        const token = sessionStorage.getItem("accessToken");
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

        try {
            const response = await fetch(`${apiUrl}/harbor/status`, {
                headers: {
                    'Authorization': `Bearer ${token}`
                }
            });

            if (!response.ok) {
                // Handle specific error cases
                if (response.status === 404) {
                    // CE ships no Harbor (same edition predicate as the carve below,
                    // fail-closed): never show a Harbor alert on CE. Paid unchanged.
                    if (!(await _isCeEdition())) {
                        showAlertWithAction({
                            title: 'Registry Setup Required',
                            message:
                                'Harbor credentials check is unavailable. The API service may need a restart.\n\n' +
                                'You can still open Workflow Builder, but configure Harbor credentials in Secrets before running workflows.',
                            actionLabel: 'Open Secrets Settings',
                            onAction: () => { window.location.hash = 'secrets'; },
                        });
                    }
                    // Allow access anyway
                } else if (response.status === 401 || response.status === 403) {
                    showAlertWithAction({
                        title: 'Authentication Error',
                        message: 'Please refresh the page and login again.',
                    });
                    return;
                } else {
                    throw new Error(`API returned status ${response.status}`);
                }
            } else {
                const harborStatus = await response.json();

                // 1900Y1p3 — CE carve: CE ships no Harbor by design, so
                // platform_configured=false there is expected, not a user
                // error, and the notice must not appear or block the handoff.
                // The edition signal is the one the UI already reads: _isCeEdition()
                // off the coalesced /platform/features entitlements.edition
                // (utils.paid_import sentinel), FAIL-CLOSED (rc20 failopen-a) — an
                // unreadable/errored payload counts as CE, so the notice is
                // SUPPRESSED (never a false "configuration required" block). Only a
                // positive saas/pro edition shows the notice + return.
                const ceEdition = await _isCeEdition();
                if (!harborStatus.platform_configured && !ceEdition) {
                    if (harborStatus.is_platform_admin) {
                        // Platform admin: show alert with one-click jump to Secrets.
                        const missingSecrets = harborStatus.missing_platform_secrets.join('\n  - ');
                        showAlertWithAction({
                            title: 'Platform Configuration Required',
                            message: `Missing platform credentials:\n  - ${missingSecrets}`,
                            actionLabel: 'Open Secrets Settings',
                            onAction: () => { window.location.hash = 'secrets'; },
                        });
                        return;
                    } else {
                        // Regular tenant user: show friendly message, allow access (no action button — they can't fix this).
                        showAlertWithAction({
                            title: 'Platform Setup In Progress',
                            message:
                                'The platform is being configured by the system administrator.\n\n' +
                                'You can still explore the Workflow Builder, but some features may be unavailable until the setup is complete.\n\n' +
                                'If this issue persists, please contact your platform administrator.',
                        });
                        // Don't return - allow them to access workflow builder anyway
                    }
                }
            }
        } catch (error) {
            console.error('Error checking registry status:', error);
            // CE ships no Harbor: same guard as the 404 alert above — never a
            // Harbor alert on CE; paid wording unchanged.
            if (!(await _isCeEdition())) {
                showAlertWithAction({
                    title: 'Unable to Verify Registry Credentials',
                    message:
                        'This may be a network issue or the API service needs a restart.\n\n' +
                        'Required secrets: harbor_registry, harbor_username, harbor_password.\n\n' +
                        'You can still open Workflow Builder, but workflows may fail without proper Harbor configuration.',
                    actionLabel: 'Open Secrets Settings',
                    onAction: () => { window.location.hash = 'secrets'; },
                });
            }
            // Allow access anyway with warning
        }

        // Harbor is configured, proceed to workflow builder
        const refreshToken = sessionStorage.getItem("refreshToken");
        const workflowsUrl = window.APP_CONFIG?.WORKFLOWS_URL || 'http://workflows.localhost';

        if (token) {
            // Pass both access token and refresh token in the URL FRAGMENT
            // (GSEC R-074): fragments are never sent to the server, so they
            // never land in nginx/uvicorn access logs. Refresh token is
            // needed for long-running workflows to access user secrets.
            let url = `${workflowsUrl}#token=${encodeURIComponent(token)}`;
            if (refreshToken) {
                url += `&refresh_token=${encodeURIComponent(refreshToken)}`;
            }
            window.location.href = url;
        } else {
            // No token, just navigate (will redirect back to dashboard for login)
            window.location.href = workflowsUrl;
        }
    });
}

// Check Harbor credentials (no visual badge, just for blocking workflow builder access)
export async function checkHarborStatus() {
    // This function is kept for potential future use but doesn't display badges
    return;
}

// #102/#104: Load and display usage warnings
async function _loadUsageWarnings() {
    const banner = document.getElementById('usage-warnings-banner');
    if (!banner) return;

    // 2400UI24 — /tenant/warnings is PAID-only: it 404s on CE, so bail before
    // the fetch. Same self-guard prologue as _loadLicenseExpiryBanner (ui.js:2469).
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/tenant/warnings`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }

        const data = await resp.json();
        const warnings = data.warnings || [];

        if (warnings.length === 0) {
            banner.classList.add('hidden');
            return;
        }

        const levelColors = {
            error: 'bg-red-50 border-red-200 text-red-800',
            warning: 'bg-yellow-50 border-yellow-200 text-yellow-800',
            info: 'bg-primary/10 border-primary text-primary',
        };
        const levelIcons = {
            error: '<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>',
            warning: '<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>',
            info: '<path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/>',
        };

        banner.innerHTML = warnings.map(w => {
            const colors = levelColors[w.level] || levelColors.info;
            const icon = levelIcons[w.level] || levelIcons.info;
            return `<div class="mx-6 mt-2 p-3 border rounded-lg ${colors} flex items-center">
                <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">${icon}</svg>
                <span class="text-sm">${w.message}</span>
            </div>`;
        }).join('');
        banner.classList.remove('hidden');
    } catch (e) {
        banner.classList.add('hidden');
    }
}

// add-self-service-sso §5.7 — first-paint SSO migration banner.
// Shown to users whose email's domain has just been routed to an
// active IdP but who haven't dismissed the banner yet. The check
// runs on every shell-page navigation; the per-user dismissal flag
// is persisted via POST /sso/banner-dismiss → user_settings.
async function _loadSsoMigrationBanner() {
    const banner = document.getElementById('sso-migration-banner');
    if (!banner) return;

    // 2400UI24 — /sso/banner-state is PAID-only: it 404s on CE, so bail before
    // the fetch. Same self-guard prologue as _loadLicenseExpiryBanner (ui.js:2469).
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/sso/banner-state`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const data = await resp.json();
        if (!data || data.show !== true) {
            banner.classList.add('hidden');
            return;
        }

        banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-primary/10 border-primary text-primary flex items-center justify-between">
                <div class="flex items-center">
                    <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/>
                    </svg>
                    <span class="text-sm">Your team now uses single sign-on. Next time you sign in, your password won't be needed — your identity provider will let you in automatically.</span>
                </div>
                <button id="sso-banner-dismiss" class="ml-4 text-xs font-medium underline hover:no-underline" aria-label="Dismiss SSO banner">Got it</button>
            </div>`;
        banner.classList.remove('hidden');

        const dismissBtn = document.getElementById('sso-banner-dismiss');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', async () => {
                try {
                    await fetch(`${apiUrl}/sso/banner-dismiss`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` }
                    });
                } catch (e) { /* non-fatal — hide locally anyway */ }
                banner.classList.add('hidden');
            });
        }
    } catch (e) {
        banner.classList.add('hidden');
    }
}

// WS1.LIC.1 — edition status warning banner. Reuses the same admin-gated
// GET /platform/license/status endpoint the license-widget.js card already
// calls (its `license_expires_in_days`/`license_expiry_warning`/`license_type`
// fields are computed in services/platform_license_service.py:
// build_license_status_payload for ANY licence type that has an expiry —
// trial, commercial signed, or internal signed). Non-admins get a 403 and the
// banner just stays hidden, same as the other _loadXBanner() functions here.
// Dismissal is localStorage-only (keyed by expires_at, so a renewed licence
// with a new expiry re-triggers the banner) — no new backend endpoint/DB
// column for this per WS1.LIC.1's "no models.py edits" constraint.
async function _loadLicenseExpiryBanner() {
    const banner = document.getElementById('license-expiry-banner');
    if (!banner) return;
    const token = sessionStorage.getItem('accessToken');
    if (!token) { banner.classList.add('hidden'); return; }

    try {
        // 2400UI15 — routers/platform_license.py is CE-denied, so this
        // would 404 on every nav; the banner ships class="hidden".
        if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/platform/license/status`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const data = await resp.json();
        const license = (data && data.license) || {};
        if (!license.license_expiry_warning) {
            banner.classList.add('hidden');
            return;
        }

        const expiresAt = license.expires_at || null;
        const dismissKey = `mod_license_expiry_dismissed_${expiresAt || 'unknown'}`;
        if (expiresAt && localStorage.getItem(dismissKey) === 'true') {
            banner.classList.add('hidden');
            return;
        }

        const days = license.license_expires_in_days;
        const isInternal = license.license_type === 'internal';
        let daysText;
        if (days === null || days === undefined) {
            daysText = 'soon';
        } else if (days < 0) {
            daysText = `${Math.abs(days)} day${Math.abs(days) === 1 ? '' : 's'} ago`;
        } else if (days === 0) {
            daysText = 'today';
        } else {
            daysText = `in ${days} day${days === 1 ? '' : 's'}`;
        }
        const verb = (days !== null && days !== undefined && days < 0) ? 'expired' : 'expires';
        const consequence = isInternal
            ? 'This is an internal licence — features stay enabled, but please renew it.'
            : 'Renew before it lapses to avoid a feature cutoff.';

        banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-yellow-50 border-yellow-200 text-yellow-800 flex items-center justify-between">
                <div class="flex items-center">
                    <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                    </svg>
                    <span class="text-sm">Your licence ${verb} ${daysText}. ${consequence}</span>
                </div>
                <button id="license-expiry-banner-dismiss" class="ml-4 text-xs font-medium underline hover:no-underline" aria-label="Dismiss licence expiry banner">Dismiss</button>
            </div>`;
        banner.classList.remove('hidden');

        const dismissBtn = document.getElementById('license-expiry-banner-dismiss');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', () => {
                if (expiresAt) localStorage.setItem(dismissKey, 'true');
                banner.classList.add('hidden');
            });
        }
    } catch (e) {
        banner.classList.add('hidden');
    }
}

// GT.C2 — dispatch drain-mode banner (platform admins only). Polls
// GET /v1/system/drain-status every 60s; the endpoint is admin-only, so a
// 403/401 hides the banner for everyone but platform admins. While
// `paused` is true the banner tells admins that new runs are paused.
//
// FIX-DASHBOARD-SKIP-ADMIN-ONLY-DRAIN-STATUS-POLL (11-17, owner Tim 7:35 AM
// CT: fix before launch): a NON-platform-admin session used to fire the first
// poll unconditionally and learn only from the 403 that it is not an admin —
// one browser-console `Failed to load resource ... 403 (Forbidden)` per page
// for every tenant user on every nav. The role is already known client-side:
// `me.get()` is the SAME cached GET /me the page fetches on every nav (api.js
// `me.get: _meCacheGet('profile')`, coalesced + 30s TTL, so awaiting it here
// adds NO extra HTTP and no new endpoint), and `ui.js:1637 updateUIVisibility`
// already reads exactly this `!!user.is_platform_admin` flag. Await it before
// the first tick and skip the whole poller when the session is known not to be
// a platform admin. Platform-admin sessions are byte-identical to before (same
// banner, same 60s interval, same hide-and-stop-on-403 safety). Fail-open: if
// /me is unreadable the role is UNKNOWN, so keep the old behaviour (poll, then
// stop on 403) — never hide the banner because a lookup failed.
let _drainBannerTimer = null;
async function _loadDrainModeBanner() {
    const banner = document.getElementById('drain-mode-banner');
    if (!banner) return;

    // Known-not-an-admin sessions never poll (and never 403-console-noise).
    // `isPlatformAdminRole === false` only when /me answered and said so.
    let isPlatformAdminRole = null;
    try {
        const _drainUser = await me.get();
        if (_drainUser) isPlatformAdminRole = !!_drainUser.is_platform_admin;
    } catch (_drainRoleErr) {
        isPlatformAdminRole = null;
    }
    if (isPlatformAdminRole === false) { banner.classList.add('hidden'); return; }

    const render = (data) => {
        if (data && data.paused) {
            banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-blue-50 border-blue-200 text-blue-800 flex items-center">
                <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                </svg>
                <span class="text-sm">Maintenance: new runs are paused</span>
            </div>`;
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    };

    let notAdmin = false;
    const tick = async () => {
        const token = sessionStorage.getItem('accessToken');
        if (!token) { banner.classList.add('hidden'); return; }
        try {
            const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            const resp = await fetch(`${apiUrl}/system/drain-status`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (resp.ok) render(await resp.json());
            else {
                banner.classList.add('hidden');
                // 403/401 → not a platform admin: stop polling for good.
                if (resp.status === 401 || resp.status === 403) {
                    notAdmin = true;
                    if (_drainBannerTimer) { clearInterval(_drainBannerTimer); _drainBannerTimer = null; }
                }
            }
        } catch (e) {
            banner.classList.add('hidden');
        }
    };

    await tick();
    if (_drainBannerTimer) clearInterval(_drainBannerTimer);
    if (!notAdmin) _drainBannerTimer = setInterval(tick, 60000);
}

// Q9 — Vault sealed banner (platform admins only). Polls
// GET /health?extended=true every 60s; that endpoint is admin-only, so a
// 403/401 hides the banner for everyone but platform admins (stop polling).
// Shows while `vault_sealed` is true — a definitively sealed Vault means
// secrets are unavailable until key holders run the unseal runbook. An
// unreachable Vault is `vault_sealed: false` on the API side (unknown is
// never reported as sealed), so no banner. This banner is NOT dismissible:
// it reflects live platform state and stays up until Vault is unsealed.
let _vaultSealedBannerTimer = null;
async function _loadVaultSealedBanner() {
    const banner = document.getElementById('vault-sealed-banner');
    if (!banner) return;

    // 2400UI31 — /health?extended=true is PAID-only: it 404s on CE (deliberate),
    // so bail before the 60s poller ever starts. Same self-guard prologue as
    // _loadLicenseExpiryBanner (ui.js:2469) / the other 2400UI24 banner guards.
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const render = (data) => {
        if (data && data.vault_sealed === true) {
            const docsBase = (window.APP_CONFIG && window.APP_CONFIG.DOCS_URL) || 'https://example.com';
            banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-red-50 border-red-200 text-red-800 flex items-center">
                <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                </svg>
                <span class="text-sm">Vault is sealed — secrets are unavailable until key holders unseal it.
                    <a href="${docsBase}/security/vault-unseal-runbook" target="_blank" rel="noopener" class="font-medium underline hover:no-underline">See the unseal runbook.</a>
                </span>
            </div>`;
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    };

    let notAdmin = false;
    const tick = async () => {
        const token = sessionStorage.getItem('accessToken');
        if (!token) { banner.classList.add('hidden'); return; }
        try {
            const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            const resp = await fetch(`${apiUrl}/health?extended=true`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (resp.ok) render(await resp.json());
            else {
                banner.classList.add('hidden');
                // 403/401 → not a platform admin: stop polling for good.
                if (resp.status === 401 || resp.status === 403) {
                    notAdmin = true;
                    if (_vaultSealedBannerTimer) { clearInterval(_vaultSealedBannerTimer); _vaultSealedBannerTimer = null; }
                }
            }
        } catch (e) {
            banner.classList.add('hidden');
        }
    };

    await tick();
    if (_vaultSealedBannerTimer) clearInterval(_vaultSealedBannerTimer);
    if (!notAdmin) _vaultSealedBannerTimer = setInterval(tick, 60000);
}

// GSEC FX.50 p3b — in-app security-notice banner. When a breach incident
// reaches status `notified`, the affected tenant's admins see it here too
// (GET /v1/tenant/security-notices returns their own + platform-wide
// notified incidents; only id/summary/notified_at cross this boundary).
// Dismissible PER SESSION ONLY (sessionStorage, not a server flag): a
// security notice is not something an org can silently ignore forever —
// a reload/next login shows it again. Non-admins 403 → banner hidden.
async function _loadSecurityNoticeBanner() {
    const banner = document.getElementById('security-notice-banner');
    if (!banner) return;

    // 2400UI24 — /tenant/security-notices is PAID-only: it 404s on CE, so bail
    // before the call. Same self-guard prologue as _loadLicenseExpiryBanner (ui.js:2469).
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    const token = sessionStorage.getItem('accessToken');
    if (!token) { banner.classList.add('hidden'); return; }
    if (sessionStorage.getItem('securityNoticeDismissed') === '1') return;

    try {
        const data = await securityNotices.list();
        const items = (data && Array.isArray(data.items)) ? data.items : [];
        if (!items.length) { banner.classList.add('hidden'); return; }
        banner.innerHTML = `
        <div class="mx-6 mt-2 p-3 border rounded-lg bg-red-50 border-red-200 text-red-800">
            <span class="text-sm font-semibold">
                ${items.map((n) => `<div>Security notice: ${esc(n && n.summary)} — see your email for details.</div>`).join('')}
            </span>
            <button type="button" class="float-right my-1 px-2 py-1 rounded border-amber-300 text-red-800 cursor-pointer"
                onclick="sessionStorage.setItem('securityNoticeDismissed','1');document.getElementById('security-notice-banner').classList.add('hidden');"
                aria-label="Dismiss security notices">Dismiss</button>
        </div>`;
        banner.classList.remove('hidden');
    } catch (e) {
        // 403 (not a tenant admin) / offline / dead API: no banner.
        banner.classList.add('hidden');
    }
}

// GSEC FX.53 (Q36=A) — MFA enforcement countdown banner. Shown while the
// install requires a 2FA factor from this user AND the user is not yet
// enrolled AND the 7-day grace period has not expired:
// "Set up two-factor sign-in — N days left". The "Set up" link is the
// MOD-branded enrollment URL from GET /me/mfa (kc_action=CONFIGURE_TOTP,
// which also offers the security-key alternative — GY.C3b). After the
// grace period Keycloak's queued required action forces setup at login,
// so the banner hides itself (grace_days_left=0).
async function _loadMfaEnforcementBanner() {
    const banner = document.getElementById('mfa-enforcement-banner');
    if (!banner) return;

    const render = (data) => {
        const show = (data && data.required && !data.enrolled
            && !data.exempt && typeof data.grace_days_left === 'number'
            && data.grace_days_left > 0);
        if (show) {
            const days = data.grace_days_left;
            banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-amber-50 border-amber-300 text-amber-900 flex items-center">
                <svg class="w-5 h-5 mr-2 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                    <path fill-rule="evenodd" d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z" clip-rule="evenodd"/>
                </svg>
                <span class="text-sm">
                    <span class="font-semibold">Set up two-factor sign-in — ${days} day${days === 1 ? '' : 's'} left.</span>
                    <a href="#account" class="ml-2 font-medium underline hover:no-underline">Set up now</a>
                </span>
            </div>`;
            banner.classList.remove('hidden');
        } else {
            banner.classList.add('hidden');
        }
    };

    const tick = async () => {
        const token = sessionStorage.getItem('accessToken');
        if (!token) { banner.classList.add('hidden'); return; }
        try {
            const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            const resp = await fetch(`${apiUrl}/me/mfa/enforcement`, {
                headers: { 'Authorization': `Bearer ${token}` },
            });
            if (resp.ok) render(await resp.json());
            else banner.classList.add('hidden');
        } catch (e) {
            banner.classList.add('hidden');
        }
    };
    await tick();
}

// First-month welcome-bonus banner. Warm welcome + makes clear the bonus
// credits are first-month-only, and pushes the user straight into the Workflow
// Wizard / Node Wizard (running a workflow is the activation moment).
async function _loadWelcomeBonusBanner() {
    const banner = document.getElementById('welcome-bonus-banner');
    if (!banner) return;

    // 2400UI24 — /billing/welcome-bonus-state is PAID-only: it 404s on CE, so bail
    // before the fetch. Same self-guard prologue as _loadLicenseExpiryBanner (ui.js:2469).
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/billing/welcome-bonus-state`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const data = await resp.json();
        if (!data || data.show !== true) { banner.classList.add('hidden'); return; }
        const credits = (data.bonus_credits || 0).toLocaleString();
        const days = data.days_remaining || 0;
        banner.innerHTML = `
            <div class="mx-6 mt-2 p-4 border rounded-lg bg-gradient-to-r from-primary/10 to-emerald-50 border-primary/40">
                <div class="flex items-start justify-between gap-4">
                    <div>
                        <div class="text-base font-semibold text-gray-900">🎉 Welcome — we're so glad you're here!</div>
                        <p class="text-sm text-gray-700 mt-1">
                            We think you're awesome and we can't wait to see what you build. We've dropped
                            <strong>${credits} bonus credits</strong> into your account to explore with — on us. 💚
                        </p>
                        <p class="text-sm text-gray-700 mt-1">
                            ⏳ Heads up: these are <strong>just for your first month</strong> (${days} day${days === 1 ? '' : 's'} left),
                            so dive in while they're hot. The magic happens the moment you run your first workflow —
                            jump into the <strong>Workflow Wizard</strong> and <strong>Node Wizard</strong> and go make something amazing.
                        </p>
                        <div class="mt-3 flex flex-wrap gap-2">
                            <button id="welcome-bonus-cta" class="px-4 py-2 bg-primary text-white text-sm rounded-md hover:bg-primary/90 font-medium">Open the Workflow Wizard →</button>
                            <button id="welcome-bonus-cta-node" class="px-4 py-2 bg-white border border-primary text-primary text-sm rounded-md hover:bg-primary/10 font-medium">Build a Node</button>
                        </div>
                    </div>
                    <button id="welcome-bonus-dismiss" class="text-xs text-gray-500 underline hover:no-underline flex-shrink-0" aria-label="Dismiss welcome banner">Got it</button>
                </div>
            </div>`;
        banner.classList.remove('hidden');

        // Both CTAs launch the workflow builder (home of both wizards) via the
        // existing nav link so the auth/token + harbor checks are reused.
        const launch = () => document.getElementById('workflow-builder-link')?.click();
        document.getElementById('welcome-bonus-cta')?.addEventListener('click', launch);
        document.getElementById('welcome-bonus-cta-node')?.addEventListener('click', launch);
        document.getElementById('welcome-bonus-dismiss')?.addEventListener('click', async () => {
            try {
                await fetch(`${apiUrl}/billing/welcome-bonus-dismiss`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${token}` }
                });
            } catch (e) { /* non-fatal — hide locally anyway */ }
            banner.classList.add('hidden');
        });
    } catch (e) {
        banner.classList.add('hidden');
    }
}

// add-self-service-sso §6.6 — post-decline link-retry banner.
// Surfaces when the user previously declined an SSO link via the FBL
// consent screen (§6.3, observed by §6.4 collector). Two actions:
// "Link my accounts" → POSTs /sso/link-retry, gets a redirect URL,
// sends the user back through the Keycloak broker; "Not now" hides
// the banner permanently via /sso/link-decline-dismiss.
async function _loadSsoLinkDeclineBanner() {
    const banner = document.getElementById('sso-link-decline-banner');
    if (!banner) return;

    // 2400UI24 — /sso/link-decline-state is PAID-only: it 404s on CE, so bail
    // before the fetch. Same self-guard prologue as _loadLicenseExpiryBanner (ui.js:2469).
    if (!await isPaidEdition()) { banner.classList.add('hidden'); return; }

    const token = sessionStorage.getItem('accessToken');
    if (!token) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/sso/link-decline-state`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!resp.ok) { banner.classList.add('hidden'); return; }
        const data = await resp.json();
        if (!data || data.show !== true) {
            banner.classList.add('hidden');
            return;
        }

        banner.innerHTML = `
            <div class="mx-6 mt-2 p-3 border rounded-lg bg-accent/10 border-accent text-foreground flex items-center justify-between">
                <div class="flex items-center">
                    <svg class="w-5 h-5 mr-2 flex-shrink-0 text-accent" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                    </svg>
                    <span class="text-sm">You skipped linking your single sign-on provider. Want to link it now so you can use one login next time?</span>
                </div>
                <div class="flex items-center gap-3 ml-4">
                    <button id="sso-link-retry-btn" class="text-xs font-medium underline hover:no-underline">Link my accounts</button>
                    <button id="sso-link-decline-dismiss" class="text-xs text-foreground/60 hover:text-foreground" aria-label="Dismiss">Not now</button>
                </div>
            </div>`;
        banner.classList.remove('hidden');

        const retryBtn = document.getElementById('sso-link-retry-btn');
        if (retryBtn) {
            retryBtn.addEventListener('click', async () => {
                try {
                    const retryResp = await fetch(`${apiUrl}/sso/link-retry`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` }
                    });
                    if (!retryResp.ok) throw new Error('retry failed');
                    const retryData = await retryResp.json();
                    if (retryData && retryData.redirect_url) {
                        window.location.href = retryData.redirect_url;
                    }
                } catch (e) {
                    banner.classList.add('hidden');
                }
            });
        }

        const dismissBtn = document.getElementById('sso-link-decline-dismiss');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', async () => {
                try {
                    await fetch(`${apiUrl}/sso/link-decline-dismiss`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` }
                    });
                } catch (e) { /* non-fatal */ }
                banner.classList.add('hidden');
            });
        }
    } catch (e) {
        banner.classList.add('hidden');
    }
}