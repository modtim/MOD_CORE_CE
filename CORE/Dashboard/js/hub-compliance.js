// compliance-hub.js
// 0b.2 redo (R37.4, owner 10-08: "tabs put within the main tab", not sidebar
// groups) — the Compliance hub: ONE sidebar entry (#compliance-center, label
// "Compliance") whose page hosts an in-page tab strip over the existing
// compliance pages. Each tab panel holds the child page's existing markup
// fetched verbatim from pages/<child>.html and its existing initializer runs
// unchanged. Old #auditor-access / #access-reviews / ... hashes redirect here
// through ui.js pageFileMap + initializeComplianceHubPage('<child>').
//
// R39.7 (owner 10-08): the standalone Compliance-profiles page is now the
// SEVENTH tab of this hub — its own sidebar row is gone from dashboard-shell.html
// and old #compliance bookmarks land here through the ui.js pageFileMap alias.
// The tab keeps the old row's role gate (platform admin only — the PUT is
// hard-gated server-side) and its CE hide (open-core/manifest.yaml denies
// pages/compliance.html + js/compliance*.js, and ui.js's _CE_COMPLIANCE_PAGES
// still shows the amber panel for a direct #compliance URL on CE).
//
// CE gates: auditor-access (FX.162.p3) and release-approvals (FX.144 p5) hide
// auditor-access (FX.162.p3) and release-approvals (FX.144 p5) hide their tab
// on Community Edition — the SAME predicate ui.js uses for the nav links and
// navigateToPage's direct-URL amber panel.

import { me, getPlatformFeatures } from './api.js';
import { setActiveTab, getActiveTab } from './tab-state.js';
import { optionalPageModule } from './utils.js';
import { initializeLicenceReviewPage } from './licence-review.js';
import { initializeKeyInventoryPage } from './key-inventory.js';
// GSEC FX.163 p3b2b — Tenant full export is a tab of THIS hub (owner rule: hub
// TABS inside main pages, never new sidebar rows). The FE child (pages/tenant-export.html
// + js/tenant-export.js) is CE-clean in open-core/manifest.yaml, so a plain static
// import is safe; the BACKING router CORE/API/routers/tenant/*.py (which covers
// routers/tenant/export.py) is denied, so the tab itself is CE-hidden below.
import { initializeTenantExportPage } from './tenant-export.js';
// BUG.B29a Part A — Audit log page initializer (hosts the standalone audit-logs).
import { initializeAuditLogsPage } from './audit-logs.js';
// BUG.B84 — FX.159.p4a/p4b + FX.157.p3b tab initializers: open-core/manifest.yaml
// denies the js/compliance*.js glob, so compliance-requirements.js, compliance-obligations.js
// and compliance-hardening.js are ABSENT on a CE build. A static `import ... from` of a file
// that is not in the build breaks the whole ES-module graph (main.js -> ui.js -> analytics.js
// never runs), so the hub no longer imports them at all: it reaches each one through the
// utils.js optionalPageModule guard in _CHILD_INIT below (null on CE -> nothing runs) and
// hides the tab via _CE_FX159_TABS.
// FX.143.p3b — Capacity report tab initializer (standalone capacity page).
import { initializeCapacityReportPage } from './capacity-report.js';

// WSIP 6.9 (UI slice 2) — Adversary findings tab initializer (standalone
// findings page). open-core/manifest.yaml denies neither pages/adversary-
// findings.html nor js/adversary-findings.js, so a plain static import is
// safe on a CE build (the tab is still platform-admin gated below).
import { initializeAdversaryFindingsPage } from './adversary-findings.js';
// WSIP 3.7 (UI slice 2) — Failure prediction calibration tab initializer.
import { initializeFailurePredictionCalibrationPage } from './failure-prediction-calibration.js';

// WSIP 7.6 (UI slice 2) — Operator proposals tab initializer (standalone
// proposals page). open-core/manifest.yaml denies neither pages/operator-
// proposals.html nor js/operator-proposals.js, so a plain static import is
// safe on a CE build (the tab is tenant gated below; the router 403s /
// feature-disables per tenant, which the module renders as op-disabled).
import { initializeOperatorProposalsPage } from './operator-proposals.js';

// GSEC FX.163 p3b2b — 'data-export' is the 8th tab (Tenant full export folded in,
// layout rule: hub TABS inside main pages, never new sidebar rows). It is NOT one of
// the R37.4 children, so ui.js needs no hash alias for it; a refresh restores it
// through tab-state (?tab=) like every other tab of this hub. It sits BEFORE
// 'compliance' so the R39.7 TAB_KEYS literal the r374 hub check pins stays intact.
// GSEC compliance-fortress 4.6 — Attestations is the 13th tab (before 'compliance'
// so the R39.7 TAB_KEYS literal the r374 hub check pins stays intact). Its page
// module js/compliance-attestations.js is CE-denied by the js/compliance*.js glob
// (and its backing router CORE/API/routers/compliance_attestations.py by the
// routers/compliance_*.py glob), so it is reached through the optionalPageModule
// guard below, never a static import, and hidden on CE via _CE_FORT46_TABS.
const TAB_KEYS = ['audit-logs', 'auditor-access', 'access-reviews', 'licence-review',
    'release-approvals', 'compliance-operator-proposals', 'key-inventory', 'lockbox-approval', 'data-export',
    'compliance-requirements', 'compliance-obligations', 'compliance-hardening',
    'compliance-capacity', 'compliance-adversary', 'compliance-calibration', 'compliance-attestations',
    'output-scanners', 'compliance-policies', 'mpc-sessions', 'compliance'];

// CE-carved children (open-core/manifest.yaml denies their page + js files):
// resolved through optionalPageModule so nothing runs on CE.
const _CE_CARVED = new Set(['auditor-access', 'release-approvals']);

// R39.7 — the Compliance-profiles tab is CE-denied too (pages/compliance.html +
// the js/compliance*.js glob), but it comes from ui.js's FX.162.p3 list rather
// than the two surfaces above, so it gets its own set: the _CE_CARVED literal
// above stays exactly as the r374 check asserts it.
const _CE_R397_TABS = new Set(['compliance']);

// GSEC FX.163 p3b2b — Tenant full export: the FE child (pages/tenant-export.html +
// js/tenant-export.js) is CE-clean, but its BACKING router CORE/API/routers/tenant/export.py
// is denied by the `CORE/API/routers/tenant/*.py` glob in open-core/manifest.yaml, so the
// tab must hide on CE. Its own set: the _CE_CARVED and _CE_R397_TABS literals the r374
// hub check pins must stay byte-identical.
const _CE_FX163_TABS = new Set(['data-export']);

// BUG.B84 — the three tabs whose page module the open-core/manifest.yaml js/compliance*.js
// glob denies (compliance-requirements.js / compliance-obligations.js / compliance-hardening.js)
// are absent on a CE build, so their tabs must hide there exactly like the _CE_CARVED and
// _CE_R397_TABS ones do. Its own set: the _CE_CARVED / _CE_R397_TABS / _CE_FX163_TABS
// literals the r374 hub check pins byte-for-byte stay untouched.
const _CE_FX159_TABS = new Set(['compliance-requirements', 'compliance-obligations',
    'compliance-hardening']);

// GSEC compliance-fortress 4.6 — Attestations tab: open-core/manifest.yaml denies
// BOTH its page module (js/compliance*.js glob) and its backing router
// (CORE/API/routers/compliance_*.py glob), so the tab must hide on CE exactly
// like the _CE_FX159_TABS ones. Its own set: the literals above stay byte-identical.
const _CE_FORT46_TABS = new Set(['compliance-attestations']);

// WSIP 6.7d — Output scanners tab: also CE-denied by the js/compliance*.js glob
// (the backing router CORE/API/routers/compliance_output_scanners.py is denied by
// the routers/compliance_*.py glob), so it hides on CE the same way.
// Its own set: the _CE_FORT46_TABS literal above stays byte-identical.
const _CE_WSIP67D_TABS = new Set(['output-scanners']);

// GSEC compliance-fortress 5.4 UI slice 2 — Tenant policies tab: CE-denied by
// the js/compliance*.js glob (js/compliance-policies.js) and by the
// routers/compliance_*.py glob (CORE/API/routers/compliance_policies.py), so
// it hides on CE exactly like the _CE_WSIP67D_TABS one. Its own set: the
// literals above stay byte-identical.
const _CE_FORT54UI_TABS = new Set(['compliance-policies']);

// FX.144 p5 (compliance is paid) — licence-review, key-inventory,
// lockbox-approval, and compliance-operator-proposals tabs: the backing
// routers are denied on CE in open-core/manifest.yaml. Its own set.
const _CE_PAID_COMPLIANCE_TABS = new Set(['licence-review', 'key-inventory',
    'lockbox-approval', 'compliance-operator-proposals']);

const _CHILD_INIT = {
    // BUG.B29a Part A — Audit log tab: the standalone audit-logs.js exports its
    // init function so the hub can call it on tab activation.
    'audit-logs': () => initializeAuditLogsPage(),
    'auditor-access': async () => (await optionalPageModule('./compliance-auditor-access.js',
        'initializeAuditorAccessPage'))?.(),
    'licence-review': () => initializeLicenceReviewPage(),
    'release-approvals': async () => (await optionalPageModule('./release-approvals.js',
        'initializeReleaseApprovalsPage'))?.(),
    'key-inventory': () => initializeKeyInventoryPage(),
    // R39.7 — Compliance profiles: the same guarded import ui.js used for its
    // standalone route (CE-denied js/compliance.js -> null, nothing runs).
    'compliance': async () => (await optionalPageModule('./compliance.js',
        'initializeCompliancePage'))?.(),
    // BUG.B84 — FX.159.p4a Compliance requirements: open-core/manifest.yaml denies the
    // js/compliance*.js glob, so this module is ABSENT on CE. Resolved through the same
    // utils.js guard the auditor-access / release-approvals / compliance tabs use: the
    // initializer name is picked off the resolved module and the tab is only ever run when
    // it is non-null (on CE optionalPageModule yields null, `?.()` runs nothing).
    'compliance-requirements': async () => (await optionalPageModule('./compliance-requirements.js',
        'initializeComplianceRequirementsPage'))?.(),
    // BUG.B84 — FX.159.p4b Contract obligations: same CE-denied glob, same guard.
    'compliance-obligations': async () => (await optionalPageModule('./compliance-obligations.js',
        'initializeComplianceObligationsPage'))?.(),
    // BUG.B84 — FX.157.p3b Host hardening results: same CE-denied glob, same guard.
    'compliance-hardening': async () => (await optionalPageModule('./compliance-hardening.js',
        'initializeComplianceHardeningPage'))?.(),
    // FX.143.p3b — Capacity report: the standalone page's own initializer,
    // self-contained, not CE-carved; its markup loads verbatim from
    // pages/capacity-report.html through the explicit fetch below (the file
    // name differs from the tab key, like data-export).
    'compliance-capacity': () => initializeCapacityReportPage(),
    // WSIP 6.9 (UI slice 2) — Adversary findings: the standalone page's own
    // initializer, self-contained and not CE-carved; its markup loads verbatim
    // from pages/adversary-findings.html through the explicit fetch below (the
    // file name differs from the tab key, like capacity-report).
    'compliance-adversary': () => initializeAdversaryFindingsPage(),
    // WSIP 3.7 (UI slice 2) — Failure prediction calibration: the standalone
    // page's own initializer, self-contained and not CE-carved; its markup
    // loads verbatim from pages/failure-prediction-calibration.html through the
    // explicit fetch below (file name differs from the tab key, like capacity).
    'compliance-calibration': () => initializeFailurePredictionCalibrationPage(),
    // WSIP 7.6 (UI slice 2) — Operator proposals: the standalone page's own
    // initializer, self-contained and not CE-carved; its markup loads verbatim
    // from pages/operator-proposals.html through the explicit fetch below (the
    // file name differs from the tab key, like adversary-findings).
    'compliance-operator-proposals': () => initializeOperatorProposalsPage(),
    // GSEC compliance-fortress 4.6 — Attestations: CE-denied page module
    // (js/compliance*.js glob), so the same optionalPageModule guard the other
    // compliance tabs use (null on CE -> nothing runs).
    'compliance-attestations': async () => (await optionalPageModule('./compliance-attestations.js',
        'initializeComplianceAttestationsPage'))?.(),
    // WSIP 6.7d — Output scanners: CE-denied page module (js/compliance*.js glob),
    // so the same optionalPageModule guard the other compliance tabs use (null on CE
    // -> nothing runs). Its markup loads from pages/compliance-output-scanners.html
    // which differs from the tab key, so the switchToTab fetch below reaches it.
    'output-scanners': async () => (await optionalPageModule('./compliance-output-scanners.js',
        'initializeComplianceOutputScannersPage'))?.(),
    // GSEC compliance-fortress 5.4 UI slice 2 — Tenant policies: CE-denied page
    // module (js/compliance*.js glob), so the same optionalPageModule guard the
    // other compliance tabs use (null on CE -> nothing runs). Its markup loads
    // from pages/compliance-policies.html, which matches the tab key, so the
    // generic pages/<key>.html fetch below reaches it (no explicit block).
    'compliance-policies': async () => (await optionalPageModule('./compliance-policies.js',
        'initializeCompliancePoliciesPage'))?.(),
    // FX.163 p3b2b — Tenant full export: the child's own initializer, unchanged.
    // Its markup loads verbatim from pages/tenant-export.html by the generic
    // fetch(`pages/${key}.html`) below; the module is CE-clean so no guarded
    // import is needed (the tab itself is CE-hidden via _CE_FX163_TABS).
    'data-export': () => initializeTenantExportPage(),
    // Fortress 6.8 — MPC session participant page: same optionalPageModule
    // guard as output-scanners above (harmless if the module is ever CE-denied
    // later). Its markup loads from pages/mpc-sessions.html, which matches the
    // tab key, so the generic pages/<key>.html fetch below reaches it.
    'mpc-sessions': async () => (await optionalPageModule('./mpc-sessions.js',
        'initializeMpcSessionsPage'))?.(),
};

// Role gates mirrored 1:1 from the per-child nav-link gates in ui.js
// updateUIVisibility ('tenant' = isSuperAdmin || isPlatformAdmin, the tenant-
// admin shape; 'platform' = platform-admin only).
const _TAB_GATE = {
    // BUG.B29a Part A — Audit log: tenant-gated (same as access-reviews + lockbox).
    // Visible on CE and paid; the backing router CORE/API/routers/audit-logs.py is
    // not denied in open-core/manifest.yaml, so no CE carve is needed.
    'audit-logs': 'tenant',
    'auditor-access': 'platform',
    'access-reviews': 'tenant',
    'licence-review': 'platform',
    'release-approvals': 'platform',
    'key-inventory': 'platform',
    'lockbox-approval': 'tenant',
    // R39.7 — the old #compliance sidebar row was display:flex for
    // isPlatformAdmin only (ui.js:1269), mirroring the router's platform-admin
    // gate on the PUT.
    'compliance': 'platform',
    // FX.159.p4a — Requirements tab shares the compliance-profiles data;
    // platform-admin only.
    'compliance-requirements': 'platform',
    // FX.159.p4b — Contract obligations is platform-admin only (admin router).
    'compliance-obligations': 'platform',
    // FX.157.p3b — Host hardening results: platform-admin only (admin router).
    'compliance-hardening': 'platform',
    // FX.143.p3b — Capacity report: platform-admin only (admin router).
    'compliance-capacity': 'platform',
    // WSIP 6.9 (UI slice 2) — Adversary findings: platform-admin only (the
    // /admin/adversary-findings router 403s everyone else; the page also
    // renders the 403 as the "Platform administrators only" notice).
    'compliance-adversary': 'platform',
    // WSIP 3.7 (UI slice 2) — Failure prediction calibration: platform-admin only
    // (the /admin/failure-prediction/calibration router 403s everyone else).
    'compliance-calibration': 'platform',
    // WSIP 7.6 (UI slice 2) — Operator proposals is a TENANT-admin tab (the
    // hub's 'tenant' gate = superadmin OR platform admin, the tenant-admin
    // shape access-reviews + lockbox-approval + data-export use). The real
    // boundary is server-side: the proposals router 403s / 404 feature_disabled
    // when wsip.autonomous_operator_propose_enabled is OFF for the caller's
    // tenant, which operator-proposals.js renders as op-disabled.
    'compliance-operator-proposals': 'tenant',
    // GSEC compliance-fortress 4.6 — Attestations: platform-admin only, mirroring
    // the router's require_auditor_or_admin gate (auditors stay read-only there).
    'compliance-attestations': 'platform',
    // WSIP 6.7d — Output scanners: platform-admin only (the backing router is
    // an admin router; same role gate as the compliance-* child tabs).
    'output-scanners': 'platform',
    // GSEC compliance-fortress 5.4 UI slice 2 — Tenant policies is a TENANT-
    // admin tab (the router's read gate is auditor-or-admin, its write gate is
    // _require_tenant_admin = tenant-root admin / platform admin), i.e. the
    // hub's 'tenant' gate shape the operator-proposals / data-export tabs use.
    'compliance-policies': 'tenant',
    // GSEC FX.163 p3b2b — Tenant full export is a TENANT-admin tab: the hub's
    // existing 'tenant' gate (_isSuperAdmin || _isPlatformAdmin, i.e. tenant
    // admin / superadmin), the same shape access-reviews + lockbox-approval use.
    // The real boundary is server-side (/tenant/export 403s for non-tenant-admins).
    'data-export': 'tenant',
    // GSEC compliance-fortress 6.8a (reviewer 931 fix) — MPC participation is
    // keyed on tenant_id, not role: CORE/API/routers/mpc_sessions.py gates every
    // route with require_active_and_onboarded_user and compares the caller's
    // own current_user.tenant_id against the session's participant list (see
    // _participant_is_present), so an ordinary (non-admin) member of a
    // participant tenant can legitimately ack/accept/reject. A 'tenant'
    // (admin-only) gate would hide the tab from exactly those members. Use
    // 'any' (signed-in + onboarded, same population the router accepts) and
    // let the router be the real boundary: it 403s reads/writes for callers
    // whose tenant_id isn't in the session's participants, which
    // mpc-sessions.js renders as an error / the mpc-writes-note.
    'mpc-sessions': 'any',
};

// 2400LR-cov4b — Capacity report, Adversary findings, Prediction calibration and MPC
// sessions: the page files ship on CE but their backing routers
// (admin_capacity_report.py, admin_adversary_findings.py,
// failure_prediction_calibration.py, mpc_sessions.py) are denied in
// open-core/manifest.yaml, so every load-time fetch would 404 on CE. Its own set:
// the literals above stay byte-identical.
const _CE_COV4B_TABS = new Set(['compliance-capacity', 'compliance-adversary',
    'compliance-calibration', 'mpc-sessions']);

// bugARtab — Access Reviews: unlike the sets above, its FE child (js/access-reviews.js
// + pages/access-reviews.html) is CE-CLEAN (no deny glob matches it; the js/compliance*.js
// and pages/compliance-*.html globs do not reach those filenames), so the page module loads
// and its static import at the top of this file stays. What is dead is its BACKING router:
// CORE/API/routers/tenant/access_reviews.py is denied by the `CORE/API/routers/tenant/*.py`
// glob (open-core/manifest.yaml:1391) and main.py mounts the whole tenant package through
// optional_paid (main.py:2315 -> the ("tenant", "router", "/tenant") entry at main.py:2337),
// so every /v1/tenant/access-reviews/* call 404s on CE. access-reviews.js:100 catches that
// ApiError and renders "Could not load campaigns", i.e. CE users got a dead, broken tab.
// Same mechanism as _CE_FX163_TABS (CE-clean FE child, denied tenant/*.py router).
const _CE_BUGARATAB_TABS = new Set(['access-reviews']);

let _isSuperAdmin = false;
let _isPlatformAdmin = false;
let _ceEdition = false;

function _tabAllowed(key) {
    if (_ceEdition && (_CE_CARVED.has(key) || _CE_R397_TABS.has(key))) return false;
    // FX.163 p3b2b — separate line so the predicate above stays byte-identical to
    // what the r374 hub check pins. Tenant full export is CE-hidden because its
    // backing router (CORE/API/routers/tenant/*.py) is denied on CE.
    if (_ceEdition && _CE_FX163_TABS.has(key)) return false;
    // BUG.B84 — separate line so the two predicates above stay byte-identical to what
    // the r374 hub check pins. The three compliance*.js tabs are CE-hidden because
    // open-core/manifest.yaml denies their page module.
    if (_ceEdition && _CE_FX159_TABS.has(key)) return false;
    // GSEC compliance-fortress 4.6 — separate line so the predicates above stay
    // byte-identical to what the r374 hub check pins.
    if (_ceEdition && _CE_FORT46_TABS.has(key)) return false;
    // WSIP 6.7d — separate line: output-scanners is CE-denied by the
    // js/compliance*.js glob.
    if (_ceEdition && _CE_WSIP67D_TABS.has(key)) return false;
    // GSEC compliance-fortress 5.4 UI slice 2 — separate line: compliance-
    // policies is CE-denied by the js/compliance*.js + routers/compliance_*.py
    // globs.
    if (_ceEdition && _CE_FORT54UI_TABS.has(key)) return false;
    // FX.144 p5 (compliance is paid) — licence-review, key-inventory,
    // lockbox-approval, and compliance-operator-proposals tabs: the backing
    // routers are denied on CE in open-core/manifest.yaml.
    if (_ceEdition && _CE_PAID_COMPLIANCE_TABS.has(key)) return false;
    if (_ceEdition && _CE_COV4B_TABS.has(key)) return false;
    // bugARtab — separate line so every predicate above stays byte-identical to what
    // the r374 hub check pins. Access Reviews is CE-hidden because its backing router
    // (CORE/API/routers/tenant/access_reviews.py) is denied by the tenant/*.py glob.
    if (_ceEdition && _CE_BUGARATAB_TABS.has(key)) return false;
    switch (_TAB_GATE[key]) {
        case 'tenant': return _isSuperAdmin || _isPlatformAdmin;
        case 'platform': return _isPlatformAdmin;
        // 'any' — any signed-in, active+onboarded user (the hub only reaches
        // _tabAllowed after _checkRole's /me call succeeds); used where the
        // backing router's own gate (e.g. per-tenant participation) is the
        // real boundary, not an admin role. See mpc-sessions above.
        case 'any': return true;
        default: return false;
    }
}

function _visibleTabs() {
    return TAB_KEYS.filter((k) => _tabAllowed(k));
}

async function _checkRole() {
    try {
        const currentUser = await me.get();
        _isSuperAdmin = currentUser.parent_user_id === null;
        _isPlatformAdmin = !!currentUser.is_platform_admin;
    } catch (_) { /* stay hidden — fail closed on an unreadable /me */ }
    // FIX-CE-HUB-COMPLIANCE-FAIL-OPEN: the old positive-CE test (`!!ent && ent.edition ===
    // 'ce' && !ent.error`) resolved PAID for main.py:1544's entitlements FAILURE stub
    // ({"edition":"ce","error":"unresolved"}), so a failed /platform/features call left the
    // compliance hub showing its paid tabs on CE. Adopt the fail-CLOSED shape security-hub.js
    // :238-243 ships verbatim (positive paid-edition test + `.catch(() => true)`): CE iff NOT
    // a positive paid edition, so an unknown/errored entitlements block hides the CE-denied
    // tabs (their routes 404/402 anyway) and a successful saas/pro payload keeps the paid path
    // byte-identical. Tab lists untouched.
    _ceEdition = await getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        const paidEdition = !!ent && !ent.error
            && (ent.edition === 'saas' || ent.edition === 'pro');
        return !paidEdition;
    }).catch(() => true);
}

let _loadedOnce = {};

export async function initializeComplianceHubPage(initialTab = null) {
    _loadedOnce = {};
    await _checkRole();

    document.querySelectorAll('.main-tab').forEach(tab => {
        const key = tab.id.replace('main-tab-', '');
        if (!TAB_KEYS.includes(key)) return;
        if (_tabAllowed(key)) {
            tab.classList.remove('hidden');
        } else {
            tab.classList.add('hidden');
        }
        tab.addEventListener('click', () => switchToTab(key));
    });

    const wanted = initialTab || getActiveTab();
    const visible = _visibleTabs();
    switchToTab(visible.includes(wanted) ? wanted : visible[0]);
}

function switchToTab(tabName) {
    const visible = _visibleTabs();
    let key = visible.includes(tabName) ? tabName : visible[0];
    if (!key) return;

    setActiveTab(key);

    document.querySelectorAll('.main-tab').forEach(t => {
        t.classList.remove('border-primary', 'text-primary');
        t.classList.add('border-transparent', 'text-gray-500');
    });
    const btn = document.getElementById(`main-tab-${key}`);
    if (btn) {
        btn.classList.remove('border-transparent', 'text-gray-500');
        btn.classList.add('border-primary', 'text-primary');
    }

    document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
    const panel = document.getElementById(`main-panel-${key}`);
    panel?.classList.remove('hidden');

    // GSEC FX.163 p3b2b — Tenant full export: its markup is the standalone child
    // pages/tenant-export.html, whose file name differs from the tab key, so the
    // generic pages/<key>.html fetch below cannot reach it. Same mechanism, spelled
    // out for the one key whose child file is not <key>.html — and it claims the
    // load-once flag FIRST, so the generic block below skips this tab (never a
    // 404 fetch, never a double init: the child re-binds its own listeners).
    // FX.143.p3b — Capacity report: the standalone child pages/capacity-report.html,
    // whose file name differs from the tab key ('compliance-capacity'), so the
    // generic pages/<key>.html fetch below cannot reach it. Same mechanism as
    // data-export: claim the load-once flag FIRST (never a 404 fetch, never a
    // double init), load the child markup verbatim, then run the child's own init.
    if (key === 'compliance-capacity' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/capacity-report.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    // WSIP 6.9 (UI slice 2) — Adversary findings: the standalone child
    // pages/adversary-findings.html, whose file name differs from the tab key
    // ('compliance-adversary'), so the generic pages/<key>.html fetch below
    // cannot reach it. Same mechanism as capacity-report / data-export.
    if (key === 'compliance-adversary' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/adversary-findings.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    // WSIP 3.7 (UI slice 2) — Failure prediction calibration: the standalone
    // child pages/failure-prediction-calibration.html, whose file name differs
    // from the tab key ('compliance-calibration'), so the generic pages/<key>.html
    // fetch below cannot reach it. Same mechanism as adversary-findings / capacity.
    if (key === 'compliance-calibration' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/failure-prediction-calibration.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    // WSIP 7.6 (UI slice 2) — Operator proposals: the standalone child
    // pages/operator-proposals.html, whose file name differs from the tab key
    // ('compliance-operator-proposals'), so the generic pages/<key>.html fetch
    // below cannot reach it. Same mechanism as adversary-findings / capacity-report.
    if (key === 'compliance-operator-proposals' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/operator-proposals.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    // WSIP 6.7d — Output scanners: the standalone child pages/compliance-output-scanners.html,
    // whose file name differs from the tab key ('output-scanners'), so the generic
    // pages/<key>.html fetch below cannot reach it. Same mechanism as adversary-findings.
    if (key === 'output-scanners' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/compliance-output-scanners.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    if (key === 'data-export' && panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        fetch('/pages/tenant-export.html').then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }

    if (panel && !_loadedOnce[key]) {
        _loadedOnce[key] = true;
        // Child markup loads verbatim from its own page file (404 on a CE-denied
        // file leaves the panel empty — the tab is hidden on CE anyway).
        fetch(`/pages/${key}.html`).then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && panel) panel.innerHTML = html;
                const init = _CHILD_INIT[key];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }
}
