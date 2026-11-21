// CE-NOPAID-4.1a (11-12) — the page registry hook.
//
// Owner rule: no paid code ships in CE. This module SHIPS in both trees, but on
// CE `registerPage` is a no-op stub (it never touches PAGE_REGISTRY and never
// calls `_isCeEdition`), so the CE tree carries ZERO paid entries. Paid imports
// this module and registers its pages, and `navigateToPage` (ui.js) reads
// PAGE_REGISTRY as the lookup table that replaces the five `_CE_*` page Sets.
//
// `ui-manifest.js` is never imported statically on CE (CE keeps its own
// `_CE_*` Sets until CE-NOPAID-4.2 replaces them); the paid tree imports it.

// OZ-C1 (11-14) — the RENDERED label text for registerPage rows
// 1-27 of 107 reads CORE/Dashboard/js/product-names.js at RUN TIME (OZ-A3 `themeLabel`
// / `themeEnabled`): the render layer looks the row `id` up in PRODUCT_NAMES and shows
// `label` + `subtitle`; unknown key -> today's literal (never blank); MOD_THEME_NAMES
// off -> today's literal byte for byte. The ROW TEXT HERE IS DELIBERATELY UNCHANGED:
// open-core/manifest.yaml pins these rows as scrub `find` rows (e.g. the exact admin-sso
// row, the app-download row, the drift-events row; line numbers drift with every manifest
// edit, so the pins are matched by find-row text, never by line number), so editing a row's text
// without a lockstep manifest row would leak a paid row into the CE extract and fail
// the paid-symbol gate. ids / page / module / gate / witness / paid and the 106-row
// count stay byte-identical; no label/sub field is added to any row.
// Proof: CORE/Dashboard/tests/oz-c1-registry-1.check.js.

// OZ-C1b (11-14) — the same annotation for registerPage rows 28-107:
// exactly 7 more rows have an id that is a PRODUCT_NAMES key (a THEME KEY), so their
// RENDERED label also reads product-names.js at run time through themeLabel/themeEnabled:
//   theme key capture-devices   (the `capture-devices` row, gate cov4b, manifest-pinned)
//   theme key compliance-center (the `compliance-center` row, gate paid-hub, manifest-pinned)
//   theme key dashboard-shell   (the `dashboard-shell` row, free row, not manifest-scrubbed)
//   theme key files             (the `files` row, free row)
//   theme key marketplace       (the `marketplace` row, ceReachable, free row)
//   theme key security          (the `security` row, gate paid-hub, manifest-pinned)
//   theme key workers           (the `workers` row, free row)
// Row text stays byte-identical: the three manifest-pinned rows keep their lockstep scrub
// `find` rows in open-core/manifest.yaml (FIX-PAGE-REG-SCRUB capture-devices / compliance-center
// / security), matched by find-row text; the four free rows are not scrubbed at all, so a row
// edit there would still be a paid-wording leak with no pin to update. ids / page / module /
// gate / also / ceReachable / paid and the 106-row count are untouched; no label/sub field
// added. Proof: CORE/Dashboard/tests/oz-c1-registry-1.check.js (pins all 8 theme-key rows).
// CE-NOPAID-4.2 leg A: PAGE_REGISTRY + registerPage live in page-registry.js now;
// re-exported here so every existing importer keeps the same names.
import { PAGE_REGISTRY, registerPage } from './page-registry.js';
export { PAGE_REGISTRY, registerPage };

// CE leg: the stub. CE never imports `ui-manifest.js` (no static import in the
// CE extract), so `registerPage` is never called there and PAGE_REGISTRY stays
// empty — 0 paid entries on CE. The stub below is the no-op CE would call: it
// appends to a throwaway array and never calls `_isCeEdition`.
const _CE_NOOP_REGISTRY = [];

export function registerPageCeStub(entry) {
    return entry;
}

// CE-NOPAID-4.1b (11-12) — hash aliases: a page id the CE gates deny that has NO
// pages/<id>.html, so it is not one of the 106 rows registerDashboardPages() writes
// below (test_page_registry.js asserts exactly 106 rows, one per page file, and 48 of them paid:true).
// asserts exactly 106 rows, one per page file, and 48 of them paid:true).
// `model-cache` is such an alias: ui.js `_CE_RETIRED_ADMIN_PAGES` denies the
// #model-cache hash and the route sweep pins the analytics.html:1520 <div id="mc-stats">
// container as its witness (js/model-cache.js _mcRenderStats fills it on paid).
// the denied drift view needs NO alias row: its page file exists, so its 106-row
// already carries the retired-admin gate + the drift-api-error witness.
// Separate hook so the 106-row / 48-paid counts and test_page_registry.js stay intact.
export function registerPageAlias(entry) {
    PAGE_REGISTRY.push(entry);
    return entry;
}

// FIX-CE-SYSTEM-ALIAS-ROWS (D72) — an id the ui.js
// `pageFileMap` (ui.js:790-792) resolves to the analytics page FILE but that has
// NO pages/<id>.html: `system` (the left-nav System link). The other ids of that
// map are not CE pages and get no alias row here (the gate denies them, fail closed).
// The derived CE gate (CE-REGGATE-1) denies any id with
// no registry row (fail closed), and the gate at ui.js:944 runs BEFORE the alias
// map is applied, so with no row the sidebar's own System link rendered the
// "Not available in MOD Community Edition" panel on every CE build
// (REPRO-DELL-SYSTEM-AND-STACKS-NAVIGATION-IN-BROWSER-D72, F-1, aa18e2f03e).
// These rows are the documented alias hook (an id with no page file), they are
// `paid: false`, and they live in the SHARED file so the CE extract carries them
// (ui.js calls this fn at module load, ui.js:49). They are NOT `registerPage` rows,
// so the 106-row / 48-paid pins (test_page_registry.js, test_page_registry.check.js,
// test_paid_page_ids_in_extract.js) stay intact. SHARED CODE: this file ships to
// paid too; paid never consults the CE gate (every CE branch short-circuits on
// `await _isCeEdition()` === false), so paid behaviour is unchanged; no licence
// gate and no cap added. Proof: CORE/Dashboard/tests/ce-system-alias-rows.check.js.
export function registerCeGateAliases() {
    registerPageAlias({id: 'system', page: 'analytics', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
}

// CE-NOPAID-4.1d (11-15) - the THIN SEAM. The 47 paid registerPage rows +
// the model-cache alias row live in CORE/API/paid_addon/dashboard/ui-manifest-paid.js, which
// the CE build DENIES (CORE/API/paid_addon/**), so the file is absent on a CE extract and
// this dynamic import rejects: the paid ids stay UNREGISTERED and _ceDeniedPageId() returns
// true for them (fail closed - never a false allow, including while the import is pending:
// a row either exists or the id is denied). The import is DYNAMIC (not 'import ... from'), so
// test_ce_extract_dashboard_static_imports_resolve never sees it and stays green.
export function _loadPaidRows() {
    // Two homes for the SAME file: the paid IMAGE ships it next to this module (/app/CORE/Dashboard/js/,
    // the paid layer's COPY line), the PRIVATE tree keeps it in the denied add-on home (CORE/API/paid_addon/
    // dashboard/), which the nodetest container mounts at /API. On a CE extract BOTH reject: the paid ids
    // stay unregistered and _ceDeniedPageId() denies them (fail closed, never a false allow - also while a
    // load is still pending, because an id with no row is denied).
    const load = (spec) => import(spec)
        .then((m) => { m.registerPaidRows(registerPage); m.registerCeGateAliases(registerPageAlias); });
    try {
        void load('./ui-manifest-paid.js').catch(() => load('../../API/paid_addon/dashboard/ui-manifest-paid.js').catch(() => undefined));
    } catch (err) {
        // CE: both specs are absent/denied (or a test lifted this fn out of module context) - fail closed.
    }
}

export function _paidRegistryEntries() {
    return PAGE_REGISTRY.filter((e) => e.paid);
}

// CE-REGGATE-1 (design of record, commit 52870a35c0) — the DERIVED CE gate inputs.
// `_ceRegisteredRows()` is the id -> row lookup the CE gate reads: on CE the extract
// carries ONLY the free rows (every `paid: true` registerPage row is scrubbed out by
// the manifest), so an id with no row here is a CE-denied page; on paid it carries
// all 106 rows + the hash aliases, and the gate is never consulted (every CE branch
// short-circuits on `await _isCeEdition()` === false).
export function _ceRegisteredRows() {
    const rows = {};
    for (const entry of PAGE_REGISTRY) rows[entry.id] = entry;
    return rows;
}

// The ids the REGISTRY marks paid (the paid-tree view; on CE the scrub leaves this
// empty, and ui.js unions it with the retired-admin witness aliases so an id the
// scrub removed still gets its sidebar row hidden — only ever hides, fail-closed).
export function _ceDeniedPageIds() {
    return PAGE_REGISTRY.filter((e) => e.paid === true).map((e) => e.id);
}

// The 106 pages of the dashboard, exactly as they exist in the tree
// (CORE/Dashboard/pages/*.html). `paid: true` marks the pages the CE
// entitlement denies — the same ids the five `_CE_*` Sets in ui.js list
// (`_CE_COMPLIANCE_PAGES`, `_CE_PAID_COMPLIANCE_PAGES`, `_CE_COV4B_PAGES`,
// `_CE_RETIRED_ADMIN_PAGES`, `_CE_PAID_HUB_PAGES`). CE registers NONE of
// them (stub); paid registers all 106.
export function registerDashboardPages() {
    registerPage({id: '404', page: '404', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: '500', page: '500', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'abuse-queue', page: 'security', module: './abuse-queue.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'accept-external-invite', page: 'accept-external-invite', module: './accept-external-invite.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'accept-invitation', page: 'accept-invitation', module: './accept-invitation.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'account', page: 'account', module: './account.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'active-sessions', page: 'security', module: './active-sessions.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'ai-inventory', page: 'help', module: './ai-inventory.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'alert-rules', page: 'security', module: './alert-rules.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'analytics', page: 'analytics', module: './analytics.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'apps', page: 'analytics', module: './apps.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'audit-logs', page: 'compliance-center', module: './audit-logs.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'bulk-permissions', page: 'bulk-permissions', module: './bulk-permissions.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'changelog', page: 'changelog', module: './changelog.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-attestations', page: 'compliance-attestations', module: './compliance-attestations.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-hardening', page: 'compliance-hardening', module: './compliance-hardening.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-obligations', page: 'compliance-obligations', module: './compliance-obligations.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-output-scanners', page: 'compliance-output-scanners', module: './compliance-output-scanners.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-policies', page: 'compliance-policies', module: './compliance-policies.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'compliance-requirements', page: 'compliance-requirements', module: './compliance-requirements.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    // OZ-C1b theme key 'dashboard-shell' -> product-names.js label + subtitle (render layer; row text unchanged)
    registerPage({id: 'dashboard-shell', page: 'dashboard-shell', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'docker-registry', page: 'docker-registry', module: './docker-registry.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'elevation-requests', page: 'security', module: './elevation-requests.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    // OZ-C1b theme key 'files' -> product-names.js label + subtitle (render layer; row text unchanged)
    registerPage({id: 'files', page: 'files', module: './files.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'held-matches', page: 'users', module: './held-matches.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'help', page: 'help', module: './help.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'home', page: 'home', module: './home.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'invitations', page: 'invitations', module: './invitations.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'ip-blocks', page: 'security', module: './ip-blocks.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'store-page', page: 'marketplace', module: './store-page.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'license-required', page: 'license-required', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'store-page', page: 'marketplace', module: './store-page.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'log-streams', page: 'logs', module: './log-streams.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'logged-out', page: 'logged-out', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'login', page: 'login', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'logs', page: 'logs', module: './logs.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'maintenance', page: 'maintenance', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    // OZ-C1b theme key 'marketplace' -> product-names.js label + subtitle (render layer; row text unchanged)
    registerPage({id: 'marketplace', page: 'marketplace', module: './marketplace.js', nav: null, navGroup: null, navRole: 'any',  gate: 'paid-hub', paid: true}); // CE-REACHABLE-FALSE 11-16: carve-out removed - the derived gate denies marketplace on CE (manifest deny CORE/Dashboard/pages/marketplace.html); paid keeps gate+paid unchanged
    registerPage({id: 'onboarding', page: 'onboarding', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'permission-requests', page: 'permission-requests', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'permissions', page: 'permissions', module: './permissions.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'platform-access-review', page: 'security', module: './platform-access-review.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'secrets', page: 'secrets', module: './secrets.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'security-alerts', page: 'security', module: './security-alerts.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'security-incidents', page: 'security', module: './security-incidents.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'store-page', page: 'marketplace', module: './store-page.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'settings', page: 'settings', module: './settings.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'staff-register', page: 'users', module: './staff-register.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'status', page: 'status', module: './status.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'storage', page: 'storage', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'support-tickets', page: 'help', module: './support-tickets.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'tenant-byok', page: 'tenant-byok', module: './tenant-byok.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'tenant-export', page: 'tenant-export', module: './tenant-export.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'tenant-registry', page: 'tenant-registry', module: './tenant-registry.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'users', page: 'users', module: './users.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'verify-email', page: 'verify-email', module: null, nav: null, navGroup: null, navRole: 'any', paid: false});
    registerPage({id: 'webhooks', page: 'analytics', module: './webhooks.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    // OZ-C1b theme key 'workers' -> product-names.js label + subtitle (render layer; row text unchanged)
    registerPage({id: 'workers', page: 'workers', module: './workers.js', nav: null, navGroup: null, navRole: 'any', paid: false});
    _loadPaidRows();   // CE-NOPAID-4.1d seam: paid rows load from the denied paid file
}
