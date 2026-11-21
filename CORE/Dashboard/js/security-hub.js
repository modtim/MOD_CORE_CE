// security-hub.js
// 0b.2 redo (R37.4, owner 10-08: "tabs put within the main tab", not sidebar
// groups) — the Security hub: ONE sidebar entry (#security) whose page hosts an
// in-page tab strip over the existing security pages. Each tab panel holds the
// child page's existing markup fetched verbatim from pages/<child>.html and its
// existing initializer runs unchanged (same fetch+innerHTML+init mechanism ui.js
// uses for standalone pages, scoped to the tab's main-panel-<child> container).
//
// Old hashes keep working via ui.js: #security-alerts / #alert-rules / ... map
// to this page file (pageFileMap) and land on their tab through
// initializeSecurityHubPage('<child>'); a refresh restores the tab via ?tab=
// (tab-state.js). Tabs the user's role/CE gates hide stay hidden — the SAME
// gates the per-child sidebar nav links used in updateUIVisibility.
//
// SECURITY TABS (owner 11-03): five of those tabs (Security Alerts / Security
// Alert Rules / Security Incidents / Blocked IPs / Abuse Queue) moved one level
// deeper under a new Issues container tab, same nested shape as System →
// Settings (pages/analytics.html settings-subtab-* + js/analytics.js
// switchSettingsChild + tab-state.js ?sub=). TAB_KEYS stays FLAT (the vm-
// extracted gate checks in CORE/Dashboard/tests/sechub-ce-tabs-hidden.check.js
// evaluate the real _TAB_GATE/_tabAllowed over these keys); PARENT_OF maps a
// child key to its container, and a bare ?tab=<child> / #<child> deep link
// normalizes to ?tab=issues&sub=<child> so bookmarks keep landing on the same
// main-panel-<child> panel. The Requests container (brief B, main-tab-requests
// / requests-subtab-*) nests Pending Approvals / Platform Access Review /
// Elevation Requests the same way and extends PARENT_OF with those three keys;
// the SAME helpers drive both containers, so CE carve-outs stay untouched
// (on CE the Requests container shows only Platform Access Review, hidden for
// everyone else).

import { me, getPlatformFeatures } from './api.js';
import { setActiveTab, getActiveTab } from './tab-state.js';
import { optionalPageModule } from './utils.js';
import { initializeSecurityAlertsPage } from './security-alerts.js';
import { initializeAlertRulesPage } from './alert-rules.js';
import { initializeActiveSessionsPage } from './active-sessions.js';
import { initializeTenantIdpPage } from './tenant-idp.js';
import { initializeElevationRequestsPage } from './elevation-requests.js';
import { initializeAbuseQueuePage } from './abuse-queue.js';
import { initializeSecurityIncidentsPage } from './security-incidents.js';
import { initializeIpBlocksPage } from './ip-blocks.js';
import { initializePendingApprovalsPage } from './pending-approvals.js';

const TAB_KEYS = ['security-alerts', 'alert-rules', 'active-sessions', 'tenant-idp',
    'elevation-requests', 'abuse-queue', 'security-incidents', 'ip-blocks',
    'outbound-connections', 'security-lockdown',
    // R39.8 (owner 10-08): the standalone Platform access-review page is now the
    // 11th tab of this hub — its own sidebar row is gone from dashboard-shell.html
    // and old #platform-access-review bookmarks land here through the ui.js pageFileMap
    // alias. The tab keeps the old row's role gate (platform admin only — the router
    // hard-gates every /admin/platform-access-reviews route). The page and js files ship
    // on CE (open-core/manifest.yaml denies neither), so its initializer is a plain
    // static import, but the router is not shipped, so _tabAllowed hides the tab on CE.
    // FX.163 p3a2 (owner 10-08): Pending approvals is the 12th tab of this hub — the
    // second-admin decision list (routers/pending_approvals.py) every tenant admin
    // sees. No sidebar row was ever added for it (the owner wants tabs inside the
    // main tab, never new sidebar rows), and open-core/manifest.yaml denies neither
    // pages/pending-approvals.html nor js/pending-approvals.js, so its initializer is
    // a plain static import too.
    'pending-approvals', 'platform-access-review'];

// Child initializer per tab. The two CE-carved modules (manifest.yaml denies
// outbound-connections.js / security-lockdown.js) resolve through optionalPageModule
// — null on CE, so nothing runs there; the hub also hides those tabs on CE.
const _CHILD_INIT = {
    'security-alerts': () => initializeSecurityAlertsPage(),
    'alert-rules': () => initializeAlertRulesPage(),
    'active-sessions': () => initializeActiveSessionsPage(),
    'tenant-idp': () => initializeTenantIdpPage(),
    'elevation-requests': () => initializeElevationRequestsPage(),
    'abuse-queue': () => initializeAbuseQueuePage(),
    'security-incidents': () => initializeSecurityIncidentsPage(),
    'ip-blocks': () => initializeIpBlocksPage(),
    // R39.8 — Platform access review: the same initializer ui.js used for its
    // standalone route (not CE-carved, so a plain static call, not optionalPageModule).
    // FX.163 p3a2 — Pending approvals: the same initializer the standalone route
    // would have used (not CE-carved, so a plain static call, not optionalPageModule).
    'pending-approvals': () => initializePendingApprovalsPage(),
};

// SECURITY TABS (owner 11-03) — Issues is a CONTAINER tab whose panel hosts a
// nested sub-tab bar (pages/security.html issues-subtab-* buttons + child
// panels carrying the security-child-panel marker). TAB_KEYS stays FLAT above
// (the vm-extracted gate checks evaluate _TAB_GATE/_tabAllowed over these keys
// — nesting them would break sechub-ce-tabs-hidden.check.js), so the parent
// link lives here. Brief B extends PARENT_OF + REQUESTS_CHILDREN with the
// three Requests children; the same helpers drive both containers.
const PARENT_OF = {
    'security-alerts': 'issues',
    'alert-rules': 'issues',
    'security-incidents': 'issues',
    'ip-blocks': 'issues',
    'abuse-queue': 'issues',
    'pending-approvals': 'requests',
    'platform-access-review': 'requests',
    'elevation-requests': 'requests',
};

// DOM order of the #issues-subtab-* buttons in pages/security.html defines the
// display order; the first VISIBLE child becomes the default on activation
// (analytics.js SETTINGS_CHILDREN discipline).
const ISSUES_CHILDREN = ['security-alerts', 'alert-rules', 'security-incidents',
    'ip-blocks', 'abuse-queue'];

// DOM order of the #requests-subtab-* buttons in pages/security.html defines the
// display order; the first VISIBLE child becomes the default on activation
// (same analytics.js SETTINGS_CHILDREN discipline as ISSUES_CHILDREN above).
// On CE the pre-existing _tabAllowed carve-outs hide pending-approvals and
// elevation-requests, so this container shows only Platform Access Review.
const REQUESTS_CHILDREN = ['pending-approvals', 'platform-access-review',
    'elevation-requests'];

// The two nested containers this hub hosts, keyed by container tab id
// (main-tab-issues / main-tab-requests). One shared switcher (switchChild)
// drives both bars; the DOM id prefixes stay distinct per container
// (`<parent>-subtab-`) and both containers' child panels share the
// security-child-panel marker.
const CONTAINER_CHILDREN = {
    issues: ISSUES_CHILDREN,
    requests: REQUESTS_CHILDREN,
};

// Role gates mirrored 1:1 from the per-child nav-link gates in ui.js
// updateUIVisibility (isPlatformAdmin / isSuperAdmin||isPlatformAdmin / ungated
// for the self-service Active Sessions page).
let _isSuperAdmin = false;
let _isPlatformAdmin = false;
let _ceEdition = false;

// 'tenant' = the tenant-admin shape ui.js uses for those nav links
// (isSuperAdmin || isPlatformAdmin); 'platform' = platform-admin only;
// 'all' = ungated (self-service Active Sessions).
const _TAB_GATE = {
    'security-alerts': 'tenant',
    'alert-rules': 'tenant',
    'active-sessions': 'all',
    'tenant-idp': 'tenant',
    'elevation-requests': 'platform',
    'abuse-queue': 'platform',
    'security-incidents': 'platform',
    'ip-blocks': 'platform',
    'outbound-connections': 'platform',
    'security-lockdown': 'tenant',
    // R39.8 — the old #platform-access-review sidebar row was display:flex for
    // isPlatformAdmin only (ui.js:1163), mirroring routers/admin_platform_access_review.py.
    'platform-access-review': 'platform',
    // FX.163 p3a2 — 'tenant' (isSuperAdmin || isPlatformAdmin): routers/pending_approvals.py
    // gates GET/POST /v1/pending-approvals on require_capability(TENANT, "manage_settings"),
    // i.e. the tenant-admin shape, and the server 403s everyone else.
    'pending-approvals': 'tenant',
};

function _tabAllowed(key) {
    if (_ceEdition && (key === 'outbound-connections' || key === 'security-lockdown' || key === 'pending-approvals')) return false;
    // 2400LR-pg2: /sso/idp lives in routers/sso_idp.py, denied (manifest :1285) on CE.
    if (_ceEdition && key === 'tenant-idp') return false;
    // SECHUB-CETABS: the Security hub shows three tabs whose API routers are carved
    // out of CE (manifest-denied — elevation-requests :1332, abuse-queue/abuse_reports
    // :1290, security-incidents :2002), so every call from their page JS 404s. Without a CE entry
    // here the tab is visible and opens an EMPTY panel (a silent half-render). Gate them
    // exactly like pending-approvals (:102) using the SAME _ceEdition predicate — no new
    // detection.
    if (_ceEdition && (key === 'elevation-requests' || key === 'abuse-queue' || key === 'security-incidents')) return false;
    // SAERR-FIX: GET /v1/tenant/security-alerts is manifest-denied on CE
    // (open-core/manifest.yaml:1393 `CORE/API/routers/tenant/*.py`), so the tab
    // rendered #sa-error "Not Found" — a denied-route call + error box on CE UI.
    // Hide the tab with the SAME _ceEdition predicate as :102/:104 — no new
    // detection; paid path unchanged.
    if (_ceEdition && key === 'security-alerts') return false;
    // CE-ENTSTUB-UI: alert-rules (/tenant/security/alert-rules), ip-blocks
    // (/platform/security/ip-blocks, /admin/security/rate-limit/blocks) and
    // platform-access-review (/admin/platform-access-reviews/*) are served by routers
    // the CE carve does not ship. They stayed visible on CE and rendered an empty
    // or erroring panel; same _ceEdition predicate, paid path unchanged.
    if (_ceEdition && (key === 'alert-rules' || key === 'ip-blocks' || key === 'platform-access-review')) return false;
    switch (_TAB_GATE[key]) {
        case 'all': return true;
        case 'tenant': return _isSuperAdmin || _isPlatformAdmin;
        case 'platform': return _isPlatformAdmin;
        default: return false;
    }
}

// Nested-container gates — analytics.js _settingsChildAllowed shape, but the
// child gates are NOT re-typed here: a child's role/CE gate is exactly its own
// _tabAllowed result (the same predicate the flat strip used before the move),
// so no new CE detection and no duplicated carve-outs. Both containers
// (Issues / Requests) share these helpers via CONTAINER_CHILDREN.
function _childrenOf(parent) {
    return CONTAINER_CHILDREN[parent] || [];
}

function _childAllowed(child) {
    return _tabAllowed(child);
}

function _visibleChildren(parent) {
    return _childrenOf(parent).filter((c) => _childAllowed(c));
}

function _visibleTabs() {
    return TAB_KEYS.filter((k) => _tabAllowed(k));
}

// The container tab is visible iff ANY of its children is (analytics.html
// :236 discipline) — on CE every Issues and Requests child is carved (their
// routers are not shipped), so both containers hide and the hub keeps only the
// self-service Active Sessions tab.
function _containerVisible(parent) {
    return _visibleChildren(parent).length > 0;
}

async function _checkRole() {
    try {
        const currentUser = await me.get();
        _isSuperAdmin = currentUser.parent_user_id === null;
        _isPlatformAdmin = !!currentUser.is_platform_admin;
    } catch (_) { /* stay hidden — fail closed on an unreadable /me */ }
    // FIX-CE-NAV-REGROUP-CLASS-A (11-14): this read used to be FAIL-OPEN —
    // the old body resolved TRUE only for `ent.edition === 'ce' && !ent.error` and .catch(() =>
    // false). The CE entitlements FAILURE stub (CORE/API/main.py:1544 returns
    // {"edition": "ce", "error": "unresolved"}) and any rejected /platform/features call
    // therefore left _ceEdition FALSE, so the CE carve-outs in _tabAllowed above never fired and
    // the alert-rules / ip-blocks / platform-access-review sub-tab buttons fell through to their
    // role gate and RENDERED — the dash-admin-nav-regroup e2e sweep saw exactly that (both
    // buttons in the DOM without the `hidden` class). Adopt the fail-CLOSED shape analytics.js
    // already ships (analytics.js:29 `_ceEditionAn = true` + :1299 `= !(await isPaidEdition())`;
    // api.js:473 rc20 failopen-c): CE iff NOT a POSITIVE paid edition, so an unknown/errored
    // entitlements block hides the carved tabs (their routes 404/402 anyway) and a successful
    // saas/pro payload keeps the paid path byte-identical (paid still shows these tabs, so the
    // spec's paid legs and their counts hold).
    _ceEdition = await getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        const paidEdition = !!ent && !ent.error
            && (ent.edition === 'saas' || ent.edition === 'pro');
        return !paidEdition;
    }).catch(() => true);
}

let _loadedOnce = {};

// Read the nested sub-tab (?sub=issues child key) the URL requested, if any
// (analytics.js `url.searchParams.get('sub')` shape; tab-state.js clearActiveTab
// already strips ?sub= when leaving the page).
function _getSubTab() {
    try {
        return new URLSearchParams(window.location.search).get('sub');
    } catch (_) {
        return null;
    }
}

export async function initializeSecurityHubPage(initialTab = null) {
    _loadedOnce = {};
    await _checkRole();

    document.querySelectorAll('.main-tab').forEach(tab => {
        // The nested sub-tab buttons reuse the .main-tab visual classes but are
        // wired in switchIssuesChild's own click pass below — skip them here so
        // a child click can't turn into a bogus top-level switchToTab() call
        // (analytics.js :65-72 discipline). They ARE revealed here, though, iff
        // their own gate allows them (analytics.js checkUserRole :1267-1349
        // discipline — the buttons ship `hidden` in pages/security.html).
        if (tab.id.startsWith('issues-subtab-') || tab.id.startsWith('requests-subtab-')) {
            const childKey = tab.id.replace(/^(?:issues|requests)-subtab-/, '');
            if (_childAllowed(childKey)) tab.classList.remove('hidden');
            return;
        }
        const key = tab.id.replace('main-tab-', '');
        if (key === 'issues' || key === 'requests') {
            // The container reveals iff ANY child is visible.
            if (_containerVisible(key)) tab.classList.remove('hidden');
            tab.addEventListener('click', () => switchToTab(key));
            return;
        }
        if (!TAB_KEYS.includes(key)) return;
        if (_tabAllowed(key)) tab.classList.remove('hidden');
        tab.addEventListener('click', () => switchToTab(key));
    });

    // Deep-link normalization (analytics.js :101-117 shape): a bare ?tab=<child>
    // (old bookmarks, and the ui.js dispatch calls initializeSecurityHubPage('<child>'))
    // resolves to parent+sub so it keeps landing on the same main-panel-<child>.
    let wanted = initialTab || getActiveTab();
    const parent = PARENT_OF[wanted];
    if (parent) {
        _persistSub(parent, wanted);
        wanted = parent;
    }

    const visible = _visibleTabs();
    if (CONTAINER_CHILDREN[wanted]) {
        // A bare ?tab=issues / ?tab=requests (or a normalized deep link) opens
        // its container iff visible, else falls back to the first visible flat
        // tab.
        if (_containerVisible(wanted)) switchToTab(wanted);
        else switchToTab(visible[0]);
        return;
    }
    switchToTab(visible.includes(wanted) ? wanted : visible[0]);
}

// Persist ?tab=<parent>&sub=<child> so a refresh restores Issues → <child>
// (analytics.js :157-168 shape; setActiveTab keeps writing ?tab=).
function _persistSub(parent, child) {
    setActiveTab(parent);
    try {
        const url = new URL(window.location.href);
        url.searchParams.set('tab', parent);
        url.searchParams.set('sub', child);
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
    } catch (_) { /* non-fatal */ }
}

function switchToTab(tabName) {
    const visible = _visibleTabs();
    // A click on a hidden tab (or a stale deep link after a role change) falls
    // back to the first visible tab; no visible tab -> leave the strip empty.
    // The Issues container is not a TAB_KEYS entry (TAB_KEYS stays flat for the
    // vm-extracted gate checks), so it validates against its own visibility
    // instead of the flat list.
    // A container tab (issues / requests) is not a TAB_KEYS entry (TAB_KEYS
    // stays flat for the vm-extracted gate checks), so it validates against its
    // own visibility instead of the flat list.
    let key = (CONTAINER_CHILDREN[tabName] && _containerVisible(tabName)) ? tabName
        : (visible.includes(tabName) ? tabName : visible[0]);
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

    if (CONTAINER_CHILDREN[key]) {
        // Neither container has a page file of its own — never fetch
        // /pages/issues.html or /pages/requests.html. Its nested bar picks
        // exactly one child, which loads its own markup + initializer below.
        switchChild(key, _getSubTab());
        return;
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

// Nested sub-tab bar inside a container panel (Issues / Requests) — ported from
// analytics.js switchSettingsChild (:326+), generalized so ONE switcher drives
// both bars (brief B). `parent` picks the bar by its DOM id prefix
// (`issues-subtab-` / `requests-subtab-`); `requestedChild` is the child a deep
// link / click asked for; falls back to the first visible child when it is
// missing, not in the list, or not allowed for the current user.
function switchChild(parent, requestedChild) {
    document.querySelectorAll(`[id^="${parent}-subtab-"]`).forEach(btn => {
        btn.classList.remove('border-primary', 'text-primary');
        btn.classList.add('border-transparent', 'text-gray-500');
        // Wire child clicks once per activation pass (analytics.js :1224 shape)
        // — { once: true } keeps repeated switchChild calls from stacking
        // handlers on the same button.
        btn.addEventListener('click',
            () => switchChild(parent, btn.id.replace(`${parent}-subtab-`, '')), { once: true });
    });

    const visible = _visibleChildren(parent);
    let childId = visible.includes(requestedChild) ? requestedChild : visible[0];

    // No visible child — leave the bar empty (initializeSecurityHubPage keeps
    // the container tab hidden in that case, so this is only reachable via a
    // stale deep link on a role change).
    if (!childId) {
        document.querySelectorAll('.security-child-panel').forEach(p => p.classList.add('hidden'));
        return;
    }

    const btn = document.getElementById(`${parent}-subtab-${childId}`);
    if (btn) {
        btn.classList.add('border-primary', 'text-primary');
        btn.classList.remove('border-transparent', 'text-gray-500');
    }

    // The marker is SHARED by both containers' child panels, so one pass hides
    // every child panel and only the chosen one is revealed below.
    document.querySelectorAll('.security-child-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById(`main-panel-${childId}`)?.classList.remove('hidden');

    _persistSub(parent, childId);

    // Child content loaders — identical to what the old top-level switchToTab
    // branch fired for these keys (verbatim fetch + _CHILD_INIT, guarded by the
    // existing _loadedOnce so a child initializes once per page navigation).
    if (!_loadedOnce[childId]) {
        _loadedOnce[childId] = true;
        const childPanel = document.getElementById(`main-panel-${childId}`);
        fetch(`/pages/${childId}.html`).then((res) => res.ok ? res.text() : null)
            .then((html) => {
                if (html && childPanel) childPanel.innerHTML = html;
                const init = _CHILD_INIT[childId];
                return init ? init() : null;
            })
            .catch(() => { /* child loaders already fail-soft in-page */ });
    }
}
