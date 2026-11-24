// analytics.js
// System page with System Health and Usage Stats

import { fetchApi as apiCall, getPlatformFeatures, isPaidEdition, me } from './api.js';
import { initAIChat, resetAIChat } from './ai-chat.js';
import { initializePersistentStacksPage } from './persistent-stacks.js';
import { initializePlatformSettingsPage } from './platform-settings.js';
// FX.162.p6 — compliance-declared-environment.js matches the CE deny glob
// CORE/Dashboard/js/compliance*.js (open-core/manifest.yaml), so it must not
// be a static import: on a CE image a static import of a file that is not in
// the build breaks the whole Dashboard module graph (main.js -> ui.js ->
// analytics.js would never run). Resolved through the utils.js optionalPageModule()
// guard at the switchSettingsChild('platform-settings') branch instead; on a
// paid build the very same module object is returned, so the initializer runs
// with the same (empty) argument list in the same place.
import { optionalPageModule } from './utils.js';
import { initializeChangeGateReportPanel } from './change-gate-report.js';
import { initializePlatformBackupsPanel } from './platform-backups.js';
import { dataLabelChip } from './data-label-chip.js';
import { renderBurstAccountsSection } from './burst-accounts.js';
import { initializeAppsPage } from './apps.js';
import { setActiveTab } from './tab-state.js';

let currentUser = null;
let _ceEditionAn = true; // FAILOPEN-E: edition is CE (fail closed: unknown = CE = true)
let isSuperAdmin = false;
let userRoleChecked = false;

// System → Settings — the operational sub-tabs that moved one level
// deeper (Model Cache moved in from a top-level System tab as well).
// DOM order of the #settings-subtab-* buttons in
// pages/analytics.html defines the display order; the first VISIBLE
// child (per checkUserRole's gates) becomes the default on activation.
// The old top-level ?tab=<id> deep links are normalized to
// ?tab=settings&sub=<id> below, so <id> doubles as the child key.
const SETTINGS_CHILDREN = ['usage', 'gpu-pool', 'activity', 'platform-settings', 'model-cache', 'change-gate', 'backups'];
// wizard-queue-redesign (GE.WQR.24) — cached module ref for the dynamically
// imported Activity tab (./wizard-queue.js), so we can call its
// stopWizardQueuePanel() when the tab is left without re-importing.
let _wizardQueueModule = null;

// 0b.2 redo (R37.4 part 2) — Apps panel loads once per page navigation
// (same load-once discipline as security-hub.js / marketplace.js).
let _appsPanelLoaded = false;

// Use backend API proxy instead of direct API calls to avoid CORS issues

// `initialTab` lets a caller land on a sub-tab without a ?tab= query string (used by ui.js for legacy hash links).
export async function initializeAnalyticsPage(initialTab = null) {
    console.log('Initializing System page');

    // 0b.2 redo (R37.4 part 2) — navigateToPage re-mounts this page's markup
    // (innerHTML wipe), so the Apps panel is empty again; allow one reload.
    _appsPanelLoaded = false;

    // Check user role first
    await checkUserRole();

    // Main tab switching. The Settings children (settings-subtab-*) reuse
    // the .main-tab class for styling but are wired separately in
    // setupEventListeners() — skip them here so the id prefix below can't
    // turn a child click into a bogus top-level switchToTab() call.
    document.querySelectorAll('.main-tab').forEach(tab => {
        if (tab.id.startsWith('settings-subtab-')) return;
        tab.addEventListener('click', () => switchToTab(tab.id.replace('main-tab-', '')));
    });

    // Setup other event listeners
    setupEventListeners();

    // Check for tab parameter in URL (e.g., #analytics?tab=workers)
    const urlParams = new URLSearchParams(window.location.search);
    const tabParam = urlParams.get('tab');

    // Business overview was relocated from this sub-tab into the Billing
    // section (#business). Bridge old #analytics?tab=business bookmarks.
    if (tabParam === 'business') {
        window.location.search = '';
        window.location.hash = 'business';
        return;
    }

    // resource-monitor-merge — the Resource Monitor is now part of the Usage
    // Stats panel, not its own sub-tab. Bridge old ?tab=resource-monitor deep
    // links (and old resource-monitor bookmarks routed here by ui.js) to
    // the Usage Stats tab, where the Resource Monitor block now lives.
    let resolvedTab = initialTab || tabParam;
    if (resolvedTab === 'resource-monitor') resolvedTab = 'usage';

    // System → Settings — the 4 sub-tabs (usage, gpu-pool, activity,
    // platform-settings) now live one level deeper under the Settings
    // sub-tab. Old top-level ?tab=<child> deep links are normalized to
    // ?tab=settings&sub=<child> so bookmarks keep landing on the child;
    // ?tab=settings without a sub opens the first visible child.
    let settingsChild = urlParams.get('sub');
    if (SETTINGS_CHILDREN.includes(resolvedTab)) {
        settingsChild = settingsChild || resolvedTab;
        const url = new URL(window.location.href);
        url.searchParams.set('tab', 'settings');
        url.searchParams.set('sub', settingsChild);
        window.history.replaceState(null, '', url.pathname + url.search + url.hash);
        resolvedTab = 'settings';
    } else if (resolvedTab === 'settings') {
        // A child key without the 'settings' parent (e.g. ?sub=usage) is
        // harmless — switchSettingsChild validates it below.
    }

    const wanted = resolvedTab;

    if (wanted && ['system', 'capture', 'settings', 'workers', 'burst-accounts', 'managers', 'topology', 'email', 'stacks', 'apps', 'model-cache', 'change-gate'].includes(wanted)) {
        // Platform-admin-only sub-tabs. Deep-linking one as a non-admin
        // falls back to System Health rather than opening a tab whose
        // endpoints will 403 — the reason tenant users were hitting the
        // platform-only worker endpoints before this gate existed.
        // The old per-child gates for the Settings children (gpu-pool,
        // platform-settings platform-admin; usage tenant-broadened) are
        // re-applied by _settingsChildAllowed() when the child activates.
        const adminOnly = ['workers', 'topology'];
        // GH.8b — Cloud accounts is tenant-admin, NOT platform-admin-only:
        // it is the tenant's own cloud accounts, mirroring the API's
        // _require_tenant_admin (platform admin OR parent_user_id IS NULL).
        const tenantAdminOnly = ['burst-accounts'];
        // 2400LR-pg6a: Topology/Managers (mesh family :1441) are denied on CE; fall back.
        // Tim B3 items 5+6 (11-06): Cloud (burst-accounts) and Apps are denied on CE too,
        // so their deep links fall back to System Health instead of opening the panel.
        // owner 11-11 (answer B) SUPERSEDES the Cloud half of B3 item 5: the tenant's OWN
        // cloud accounts (burst-accounts) are CE-visible and usable, so its deep link
        // stays here (the tenantAdminOnly role gate below still falls back for a
        // non-admin). Apps (B3 item 6) and the mesh family keep falling back on CE.
        if ((adminOnly.includes(wanted) && !currentUser?.is_platform_admin)
            || (_ceEditionAn && (wanted === 'topology' || wanted === 'managers' || wanted === 'apps'))) {
            console.log(`User without ${wanted} access, switching to system tab`);
            switchToTab('system');
        } else if (wanted === 'apps' && !isSuperAdmin) {
            // 0b.2 redo (R37.4 part 2) — Apps keeps the exact gate the old
            // apps-nav-link used in ui.js (tenant root only, parent_user_id IS
            // NULL), so a non-root deep link falls back to System Health.
            console.log('User without apps access, switching to system tab');
            switchToTab('system');
        } else if (tenantAdminOnly.includes(wanted) && !(currentUser?.is_platform_admin || isSuperAdmin)) {
            console.log(`User without ${wanted} access, switching to system tab`);
            switchToTab('system');
        } else {
            switchToTab(wanted, settingsChild);
        }
    } else {
        // Load System Health by default
        loadSystemHealth();
    }
}

function switchToTab(tabName, settingsChild = null) {
    const tab = document.getElementById(`main-tab-${tabName}`);
    if (!tab) return;

    // Remember the active sub-tab so a refresh restores it (#20). For
    // Settings, also persist the child in ?sub= so a refresh restores
    // System → Settings → <child>.
    setActiveTab(tabName);
    const url = new URL(window.location.href);
    if (tabName === 'settings') {
        // Re-activating Settings without a named child restores the last
        // active child from ?sub= (persisted on the previous activation),
        // falling back to the first visible child in switchSettingsChild.
        if (!settingsChild) settingsChild = url.searchParams.get('sub');
        if (settingsChild) url.searchParams.set('sub', settingsChild);
        else url.searchParams.delete('sub');
    } else if (url.searchParams.has('sub')) {
        url.searchParams.delete('sub');
    }
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);

    // Update tab styles
    document.querySelectorAll('.main-tab').forEach(t => {
        t.classList.remove('border-primary', 'text-primary');
        t.classList.add('border-transparent', 'text-gray-500');
    });
    tab.classList.add('border-primary', 'text-primary');
    tab.classList.remove('border-transparent', 'text-gray-500');

    // Switch panels
    const panelId = `main-panel-${tabName}`;
    document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById(panelId)?.classList.remove('hidden');

    // Stop live polling when leaving system health tab
    if (panelId !== 'main-panel-system') _stopCapacityPolling();
    // Stop external-orchestrator polling when leaving the External tab
    if (panelId !== 'main-panel-external') _stopExternalPolling();
    // wizard-queue-redesign (GE.WQR.24) — stop the Activity tab's list
    // poller + any open SSE/poll log streams when leaving the tab. The
    // Activity panel now lives under Settings, so "leaving" also means
    // leaving the Settings sub-tab (or its activity child).
    if (panelId !== 'main-panel-activity' && _wizardQueueModule) {
        _wizardQueueModule.stopWizardQueuePanel();
    }

    // Initialize tab content
    if (panelId === 'main-panel-system') {
        loadSystemHealth();
    } else if (panelId === 'main-panel-capture') {
        // Capture Devices — moved here from its own page as a System sub-tab.
        // Idempotent (the module guards its own bindings).
        // 2400LR-pg2: capture_field_client.py is denied on CE (manifest :1664);
        // FAILOPEN-E: fail closed - only a positive saas/pro edition (api.js isPaidEdition)
        // opens the paid Capture Devices page; an errored stub or unreachable
        // /platform/features is treated as CE, never as paid.
        // rc20-ce-nav (owner 11-04) — no-flicker: pages/analytics.html now ships
        // #main-tab-capture WITH the hidden class, so CE never shows it "briefly
        // then removes" it; the paid leg reveals it after the edition is
        // positively confirmed (saas/pro), the CE leg keeps it hidden and paints
        // the amber notice in the panel.
        isPaidEdition().catch(() => false).then((paid) => {
            if (paid) {
                document.getElementById('main-tab-capture')?.classList.remove('hidden');
                // CE: capture-devices page denied (manifest deny) - nothing to initialize
                return;
            }
            document.getElementById('main-tab-capture')?.classList.add('hidden');
            const p = document.getElementById('main-panel-capture');
            if (p) p.innerHTML = '<div class="px-6 py-4 rounded-lg bg-amber-100 text-amber-900"><h1 class="text-xl font-semibold">Not available in MOD Community Edition</h1></div>';
        });
    } else if (panelId === 'main-panel-settings') {
        // System → Settings container: no panel content of its own; the
        // nested sub-tab bar + one active child panel does the work.
        switchSettingsChild(settingsChild);
    } else if (panelId === 'main-panel-workers') {
        loadWorkersPanel();
    } else if (panelId === 'main-panel-stacks') {
        // Persistent Stacks — moved from its own sidebar entry to a System
        // sub-tab. Idempotent (guarded by _psWired inside the module).
        initializePersistentStacksPage();
    } else if (panelId === 'main-panel-burst-accounts') {
        // GH.8b — the tenant's own cloud provider accounts. Idempotent,
        // owner 11-11 (answer B): the tenant's OWN cloud accounts tab is visible
        // and usable in every edition for tenant admins (only the tenantAdminOnly
        // role gate applies; no edition gate here). The section's loaders hit the
        // tenant-admin burst-accounts routers, which CE serves too. MOD-hosted
        document.getElementById('main-tab-burst-accounts')?.classList.remove('hidden');
        renderBurstAccountsSection();
    } else if (panelId === 'main-panel-apps') {
        // 0b.2 redo (R37.4 part 2) — Apps moved from its own sidebar entry to a
        // System main tab (left of Settings, owner Q6 10-08). The child markup
        // loads verbatim from pages/apps.html into the panel (inline <script>
        // wouldn't execute through innerHTML, but js/apps.js is a real module
        // wired by the initializer below, so nothing needs re-executing), then
        // js/apps.js's own initializer runs untouched against those ids.
        // Tim B3 item 6 (11-06): Apps is not in the CE first launch, so the
        // initializer is edition-gated with the same fail-closed predicate as
        // the Capture Devices / burst-accounts legs - a CE (or unknown/errored
        // edition) activation never fetches /pages/apps.html and never calls
        // initializeAppsPage(). _appsPanelLoaded is still set BEFORE the async
        // leg so a CE activation cannot re-fetch on every tab switch.
        if (!_appsPanelLoaded) {
            _appsPanelLoaded = true;
            isPaidEdition().catch(() => false).then((paid) => {
                if (paid) {
                    document.getElementById('main-tab-apps')?.classList.remove('hidden');
                    const appsPanel = document.getElementById('main-panel-apps');
                    fetch('/pages/apps.html').then((res) => res.ok ? res.text() : null)
                        .then((html) => {
                            if (html && appsPanel) appsPanel.innerHTML = html;
                            return initializeAppsPage();
                        })
                        .catch(() => { /* js/apps.js already fails soft in-page */ });
                    return;
                }
                document.getElementById('main-tab-apps')?.classList.add('hidden');
                document.getElementById('main-panel-apps')?.classList.add('hidden');
            });
        }
    } else if (panelId === 'main-panel-managers') {
        loadManagersPanel();
    } else if (panelId === 'main-panel-topology') {
        // tailnet-plan Group 9 — mesh topology graph + posture.
        loadTopologyPanel();
    } else if (panelId === 'main-panel-external') {
        // external-executor-nodes — load on the tenant's external farms.
        // EXTFARM-3 — the add-farm dialog + farm list wiring. Guarded by
        // _extFarmsWired so re-activating the tab never double-binds the
        // button listeners (no edition gate: the tab is CE-live, see the
        // rc20-ce-external note below).
        _extWireFarmUi();
        // rc20-ce-external (owner 11-04, "the External subtab doesn't work; it
        // should, as that allows you to hook into other orchestrators like
        // Kubernetes"): hooking into external orchestrators (Kubernetes / Qube
        // / Deadline) is intended Community Edition, NOT a paid feature — so
        // there is NO edition gate here. The old 2400LR-cdu CE leg HID the tab
        // because the CE carve denied routers/system_external.py (manifest:1439)
        // + services/external_load.py (manifest:1527); both are now api_allow,
        // so GET /v1/system/external is mounted in the CE build (main.py:2430
        // _GATE4B_ROUTER_MOUNTS via optional_paid mounts it when present).
        loadExternalPanel();
    }
}

// ========================================
// System → Settings sub-tab (nested sub-tabs)
// ========================================

// Settings children that need no gate — visible to every signed-in
// role (the list endpoints are role-scoped server-side).
function _settingsChildAllowed(childId) {
    if (childId === 'usage') {
        // Same gate that revealed the old main-tab-usage: platform/tenant
        // admin, or the flag ui.js sets when it reveals the System nav.
        return !!(currentUser?.is_platform_admin || isSuperAdmin || window.__systemFullAccess);
    }
    // Activity is visible to every signed-in role (see checkUserRole's
    // comment on the old main-tab-activity reveal).
    if (childId === 'activity') return true;
    // GPU Pool + Platform Settings stay platform-admin only; every
    // endpoint is server-gated too.
    if (childId === 'platform-settings') {
        return !!currentUser?.is_platform_admin;
    }
    // 2400LR-pg6a: gpu-pool is denied on CE (admin_storage.py /system/*); fail open.
    if (childId === 'gpu-pool') {
        return !!currentUser?.is_platform_admin && !_ceEditionAn;
    }
    // FX.84 p2c — Backups (on-prem backup settings + readings): platform-
    // admin only, same gate as the router's _require_platform_admin.
    if (childId === 'backups') {
        // 2400LR-pg5: platform_backups.py is denied on CE (manifest :1263); hide the
        // sub-tab there (_ceEditionAn is set in checkUserRole; FAILOPEN-E: unknown
        // = CE = true = hidden).
        return !!currentUser?.is_platform_admin && !_ceEditionAn;
    }
    // Model Cache (GF.C4) — demoted from a top-level System tab; same
    // platform-admin gate it had there, server-gated too.
    if (childId === 'model-cache') {
        // 2400LR-pg6a: denied on CE; hidden there (fail open: unknown edition = shown).
        return !!currentUser?.is_platform_admin && !_ceEditionAn;
    }
    // 23.7/GO.8 s2 — Change-Gate Report: same platform-admin gate the
    // /wizard/variant-change-gate-weekly-report route uses (_require_platform_admin).
    if (childId === 'change-gate') {
        return !!currentUser?.is_platform_admin;
    }
    return false;
}

function _settingsVisibleChildren() {
    return SETTINGS_CHILDREN.filter(c => _settingsChildAllowed(c));
}

// Nested sub-tab bar inside the Settings panel. `requestedChild` is the
// child a deep link / click asked for; falls back to the first visible
// child when it is missing, not in the list, or not allowed for the
// current user (the old non-admin fallback was System Health — inside
// Settings, falling back to the first visible child keeps the container
// usable instead of empty).
function switchSettingsChild(requestedChild) {
    document.querySelectorAll('[id^="settings-subtab-"]').forEach(btn => {
        btn.classList.remove('border-primary', 'text-primary');
        btn.classList.add('border-transparent', 'text-gray-500');
    });

    const visible = _settingsVisibleChildren();
    let childId = visible.includes(requestedChild) ? requestedChild : visible[0];

    // No visible child — leave the bar empty (checkUserRole keeps
    // #main-tab-settings hidden in that case, so this is only reachable
    // via a stale deep link on a role change).
    if (!childId) {
        document.querySelectorAll('.settings-child-panel').forEach(p => p.classList.add('hidden'));
        return;
    }

    const btn = document.getElementById(`settings-subtab-${childId}`);
    if (btn) {
        btn.classList.add('border-primary', 'text-primary');
        btn.classList.remove('border-transparent', 'text-gray-500');
    }

    document.querySelectorAll('.settings-child-panel').forEach(p => p.classList.add('hidden'));
    document.getElementById(`main-panel-${childId}`)?.classList.remove('hidden');

    // Same stop-on-leave logic switchToTab applies at the top level, for
    // the child whose panel is about to stop being the active one:
    // leaving the activity child must stop its poller (GE.WQR.24).
    if (childId !== 'activity' && _wizardQueueModule) {
        _wizardQueueModule.stopWizardQueuePanel();
    }

    // Child content loaders — identical to what the old top-level
    // switchToTab branches fired, so first-activation lazy loads
    // (GPU Pool, Activity's dynamic import) still happen here.
    if (childId === 'usage') {
        loadUsageStats();
    } else if (childId === 'gpu-pool') {
        loadGpuPoolPanel();
    } else if (childId === 'platform-settings') {
        // GS.PSR.4 — DB-backed platform settings. Idempotent (the module
        // guards its own click delegation via _psrWired).
        initializePlatformSettingsPage();
        // GY.C0 (slice 2) — declared compliance environment section
        // (multi-select + needed controls); idempotent (guards its own
        // wiring via cdeWired) and hidden for non-admins / 404 endpoints.
        // FX.162.p6 — guarded dynamic import (CE carve). switchSettingsChild
        // is SYNCHRONOUS and the original call was fire-and-forget (the
        // async initializer was never awaited), so the smallest behaviour-
        // preserving change is the same `.then` shape the wizard-queue
        // branch right below already uses: the initializer still starts with
        // the same (empty) argument list in the same place, and on CE the
        // lookup yields null so nothing runs.
        optionalPageModule('./compliance-declared-environment.js',
            'initializeComplianceDeclaredEnvironment').then((init) => {
                if (init) init();
            });
    } else if (childId === 'activity') {
        // wizard-queue-redesign (GE.WQR.24) — Dashboard port of the unified
        // Wizard Queue (System -> Activity). Dynamically imported so the
        // module (and its SSE/backoff-poll machinery) only loads once this
        // child is actually opened.
        import('./wizard-queue.js').then(mod => {
            _wizardQueueModule = mod;
            mod.initializeWizardQueuePanel(document.getElementById('wizard-queue-root'));
        });
    } else if (childId === 'model-cache') {
        // GF.C4 — Model Cache (manager-side cache ops). Demoted from a
        // top-level System tab into Settings; idempotent (the module guards
        // its own click delegation via _mcWired), so first-activation lazy
        // load happens here.
        /* model-cache panel not in this edition */
    } else if (childId === 'change-gate') {
        // 23.7/GO.8 s2 — Change-Gate Report (read-only weekly-report readout).
        // Idempotent (the module guards its own wiring via _cgWired).
        initializeChangeGateReportPanel();
    } else if (childId === 'backups') {
        // FX.84 p2c — Backups card (settings + last-run/restore-test
        // readings). Idempotent: the module re-fetches and re-renders;
        // form wiring is guarded via form.dataset.wired.
        initializePlatformBackupsPanel(currentUser);
    }
}

// add-multi-region-mesh — the multi-region manager mesh CONTROL PLANE.
// Lists managers + their trust lifecycle and drives every admin lever
// (enroll / approve / revoke / de-enroll / handoff / promote / gossip),
// the RPO replication dial, and the ownership-transition history.
function _mgrEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let _mgrList = [];   // last loaded manager list (for handoff peer pickers)

function _mgrFlash(elId, msg, ok) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.textContent = msg;
    el.className = 'text-sm mt-2 ' + (ok ? 'text-success' : 'text-destructive');
    el.classList.remove('hidden');
}

function _mgrRelTime(iso) {
    if (!iso) return '—';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return '—';
    const s = Math.round((Date.now() - t) / 1000);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
}

function _mgrStatusBadge(m) {
    if (m.status === 'active') return '<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-success/15 text-success">active</span>';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-600">${_mgrEsc(m.status || 'unknown')}</span>`;
}

// A peer URL is non-routable across VMs when it's a host-internal alias /
// loopback / bare single-label hostname — fine on the single-host bed, wrong the
// moment a peer sits on another VM (it must be a routable IP / private DNS).
function _mgrPeerNonRoutable(url) {
    try {
        const h = new URL(url).hostname;
        if (h === 'api' || h === 'localhost' || h === '0.0.0.0' || h.startsWith('127.')) return true;
        // bare single-label hostname (no dots) and not an IP → not routable off-host
        const isIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(h);
        return !isIp && !h.includes('.');
    } catch (_) { return false; }
}

function _mgrTrustBadge(m) {
    const s = m.admin_state || 'pending';
    const cls = s === 'approved' ? 'bg-success/15 text-success'
        : s === 'pending' ? 'bg-amber-100 text-amber-700'
        : 'bg-destructive/15 text-destructive';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-semibold ${cls}">${_mgrEsc(s)}</span>`;
}

function _mgrRowActions(m) {
    if (m.is_self) return '<button data-mgr-act="edit-urls" data-mgr-id="' + _mgrEsc(m.manager_id) + '" class="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700 hover:bg-gray-200">Edit URLs</button>';
    const btn = (act, label, color) =>
        `<button data-mgr-act="${act}" data-mgr-id="${_mgrEsc(m.manager_id)}" class="px-2 py-1 text-xs rounded ${color}">${label}</button>`;
    const out = [];
    if (m.admin_state !== 'approved') out.push(btn('approve', 'Approve', 'bg-success text-white hover:bg-success/90'));
    if (m.admin_state === 'approved') out.push(btn('revoke', 'Revoke', 'bg-amber-500 text-white hover:bg-amber-600'));
    out.push(btn('work', 'Work', 'bg-gray-100 text-gray-700 hover:bg-gray-200'));
    if (m.status !== 'active') out.push(btn('promote', 'Fail over', 'bg-primary text-white hover:bg-primary/90'));
    out.push(btn('deenroll', 'Remove', 'bg-gray-100 text-destructive hover:bg-gray-200'));
    return `<div class="flex items-center justify-end gap-1 flex-wrap">${out.join('')}</div>`;
}

async function _mgrAction(act, id) {
    try {
        if (act === 'approve') { await apiCall(`/managers/${encodeURIComponent(id)}/approve`, { method: 'POST' }); }
        else if (act === 'revoke') {
            if (!confirm(`Revoke trust for ${id}? It will be excluded from gossip/handoff immediately.`)) return;
            await apiCall(`/managers/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
        } else if (act === 'deenroll') {
            if (!confirm(`Remove ${id} from the mesh registry entirely?`)) return;
            await apiCall(`/managers/${encodeURIComponent(id)}`, { method: 'DELETE' });
        } else if (act === 'promote') {
            if (!confirm(`Fail over ${id}: take over all work it owns? Only do this if it is actually down.`)) return;
            const r = await apiCall(`/managers/work/promote?dead_manager_id=${encodeURIComponent(id)}`, { method: 'POST' });
            alert(`Failed over ${r.count} work item(s) from ${id} → ${r.promoted_to}.`);
        } else if (act === 'work') { return _mgrShowWork(id); }
        else if (act === 'edit-urls') { return _mgrEditUrls(); }
        loadManagersPanel();
    } catch (e) {
        alert(`Action failed: ${e.message || e}`);
    }
}

async function _mgrEditUrls() {
    let cfg;
    try { cfg = await apiCall('/managers/self-config'); }
    catch (e) { alert(`Could not load config: ${e.message || e}`); return; }
    const curPeer = cfg.peer_url_override || '';
    const autoNote = cfg.peer_url_auto_detected
        ? `auto-detected: ${cfg.peer_url_auto_detected}`
        : (cfg.peer_url_env_pinned ? `env: ${cfg.peer_url_env_pinned}` : 'no auto value');
    const peer = window.prompt(
        "This manager's PEER URL — the routable IP/private-DNS other managers use " +
        "(bypasses the client LB). e.g. http://10.0.0.12\n" +
        `(blank = use ${autoNote})`,
        curPeer || cfg.effective_peer_url || '');
    if (peer === null) return;  // cancelled
    const curExt = cfg.external_url_override || cfg.external_url_derived || '';
    const ext = window.prompt(
        "This manager's EXTERNAL URL — the public/LB-fronted address clients use. " +
        "(blank = auto-derive from domain config)", curExt);
    if (ext === null) return;
    try {
        await apiCall('/managers/self-config', {
            method: 'PUT', body: JSON.stringify({ peer_url: peer, external_url: ext }) });
        _mgrFlash('mgr-enroll-msg', 'Updated this manager\'s addresses.', true);
        loadManagersPanel();
    } catch (e) { alert(`Update failed: ${e.message || e}`); }
}

async function _mgrShowWork(id) {
    const modal = document.getElementById('mgr-work-modal');
    const body = document.getElementById('mgr-work-body');
    document.getElementById('mgr-work-title').textContent = `Work owned by ${id}`;
    body.innerHTML = 'Loading…';
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    try {
        const data = await apiCall(`/managers/${encodeURIComponent(id)}/owned-work`);
        const work = data.work || [];
        if (!work.length) { body.innerHTML = '<p class="text-gray-500">No work currently owned by this manager.</p>'; return; }
        const peers = _mgrList.filter(m => m.admin_state === 'approved' && m.manager_id !== id);
        const peerOpts = peers.map(p => `<option value="${_mgrEsc(p.manager_id)}">${_mgrEsc(p.manager_id)} (${_mgrEsc(p.region || '?')})</option>`).join('');
        body.innerHTML = `<table class="min-w-full text-sm"><thead><tr class="text-xs text-gray-500 uppercase">
            <th class="text-left py-1">Work key</th><th class="text-left py-1">Token</th><th class="text-right py-1">Hand off to</th></tr></thead><tbody>` +
            work.map(w => `<tr class="border-t border-gray-100">
                <td class="py-2 font-mono text-gray-800">${_mgrEsc(w.work_key)}</td>
                <td class="py-2 text-gray-500">${_mgrEsc(w.fencing_token)}</td>
                <td class="py-2 text-right">${peers.length ? `<span class="inline-flex gap-1">
                    <select class="text-xs border border-gray-300 rounded px-1 py-0.5" data-handoff-key="${_mgrEsc(w.work_key)}" aria-label="Hand ${_mgrEsc(w.work_key)} to peer">${peerOpts}</select>
                    <button data-handoff-key="${_mgrEsc(w.work_key)}" class="mgr-handoff-btn px-2 py-1 text-xs bg-primary text-white rounded hover:bg-primary/90">Go</button>
                </span>` : '<span class="text-xs text-gray-400">no approved peer</span>'}</td>
            </tr>`).join('') + '</tbody></table>';
        body.querySelectorAll('.mgr-handoff-btn').forEach(b => b.addEventListener('click', async () => {
            const key = b.dataset.handoffKey;
            const sel = body.querySelector(`select[data-handoff-key="${CSS.escape(key)}"]`);
            const toId = sel && sel.value;
            if (!toId) return;
            b.disabled = true; b.textContent = '…';
            try {
                await apiCall(`/managers/work/handoff?work_key=${encodeURIComponent(key)}&to_peer_id=${encodeURIComponent(toId)}`, { method: 'POST' });
                _mgrShowWork(id);
            } catch (e) { b.disabled = false; b.textContent = 'Go'; alert(`Handoff failed: ${e.message || e}`); }
        }));
    } catch (e) {
        body.innerHTML = `<p class="text-destructive">Failed: ${_mgrEsc(e.message || String(e))}</p>`;
    }
}

// ---- RPO replication dial (folded in from the standalone mesh-rpo page) ----
const _RPO_STOPS = (() => {
    // 49 log-spaced stops from 1s to 86400s (24h).
    const min = 1, max = 86400, n = 49, out = [];
    for (let i = 0; i < n; i++) out.push(Math.round(min * Math.pow(max / min, i / (n - 1))));
    return out;
})();
function _rpoFmt(s) {
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    if (s < 86400) return `${(s / 3600).toFixed(s % 3600 ? 1 : 0)}h`;
    return `${(s / 86400).toFixed(s % 86400 ? 1 : 0)}d`;
}
function _rpoNearestStop(sec) {
    let best = 0, bd = Infinity;
    _RPO_STOPS.forEach((v, i) => { const d = Math.abs(v - sec); if (d < bd) { bd = d; best = i; } });
    return best;
}
function _rpoRender() {
    const slider = document.getElementById('mgr-rpo-slider');
    const secs = _RPO_STOPS[+slider.value];
    document.getElementById('mgr-rpo-value').textContent = _rpoFmt(secs);
    const streaming = secs <= 10;  // matches server streaming band default
    const badge = document.getElementById('mgr-rpo-mode');
    badge.textContent = streaming ? 'streaming' : 'batch';
    badge.className = 'px-2 py-0.5 rounded-full text-xs font-semibold ' +
        (streaming ? 'bg-success/15 text-success' : 'bg-amber-100 text-amber-700');
}
// CE carve: the RPO dial calls the mesh RPO router (mesh-rpo.js is
// denied on CE). Fail closed: only a positive paid edition (api.js
// isPaidEdition) shows the card and loads it; CE or an unknown edition hides
// the card and makes no /platform/mesh-replication-config call.
function _rpoGate() {
    const card = document.getElementById('mgr-rpo-slider')?.closest('.shadow-soft');
    isPaidEdition().catch(() => false).then((paid) => {
        if (card) card.classList.toggle('hidden', !paid);
        if (paid) _rpoLoad();
    });
}

async function _rpoLoad() {
    try {
        const r = await apiCall('/platform/mesh-replication-config');
        const slider = document.getElementById('mgr-rpo-slider');
        slider.value = _rpoNearestStop(r.rpo_seconds);
        _rpoRender();
        document.getElementById('mgr-rpo-mode').textContent = r.mode;
        document.getElementById('mgr-rpo-effective').textContent =
            `current: ${_rpoFmt(r.rpo_seconds)} (${r.mode}, source: ${r.source})`;
    } catch (e) {
        document.getElementById('mgr-rpo-effective').textContent = `could not load RPO: ${e.message || e}`;
    }
    _rpoLoadStatus();
}

// Actual measured replication health (per-peer lag vs the RPO target) — the
// surface that was previously missing (an RPO breach used to be invisible).
async function _rpoLoadStatus() {
    const overall = document.getElementById('mgr-repl-overall');
    const peersEl = document.getElementById('mgr-repl-peers');
    if (!overall || !peersEl) return;
    try {
        const s = await apiCall('/platform/mesh-replication-config/status');
        const peers = s.peers || [];
        if (!peers.length) {
            overall.textContent = 'single manager';
            overall.className = 'px-2 py-0.5 rounded-full text-xs font-semibold bg-gray-100 text-gray-500';
            peersEl.innerHTML = '<span class="text-gray-400">No peers — nothing to replicate.</span>';
            return;
        }
        const breach = s.any_breach;
        overall.textContent = breach ? 'BREACH' : 'within RPO';
        overall.className = 'px-2 py-0.5 rounded-full text-xs font-semibold ' +
            (breach ? 'bg-destructive/15 text-destructive' : 'bg-success/15 text-success');
        peersEl.innerHTML = peers.map(p => {
            const lag = p.lag_seconds == null ? '—' : `${p.lag_seconds}s`;
            const cls = p.breached ? 'text-destructive' : 'text-success';
            const dot = p.breached ? '●' : '○';
            return `<div class="flex items-center justify-between">
                <span><span class="${cls}">${dot}</span> ${_mgrEsc(p.manager_id)}${p.region ? ` (${_mgrEsc(p.region)})` : ''}</span>
                <span class="${cls}">lag ${lag} / target ${_rpoFmt(s.rpo_target_seconds)}${p.breached ? ' — breach' : ''}</span>
            </div><div class="text-gray-400 pl-3">${_mgrEsc(p.note)}</div>`;
        }).join('');
        if (!s.replication_enabled) {
            peersEl.innerHTML += '<div class="text-gray-400 mt-1">Replication not enabled (MESH_REPLICATION_ENABLED) — breach alerts suppressed; gauge reflects reality.</div>';
        }
    } catch (e) {
        overall.textContent = 'unavailable';
        peersEl.innerHTML = `<span class="text-amber-600">Could not load replication status: ${_mgrEsc(e.message || String(e))}</span>`;
    }
}

function _wireManagersPanel() {
    // SPA remount: page HTML re-injects on every visit, so mark the element, not the module.
    const refreshBtn = document.getElementById('managers-refresh-btn');
    if (!refreshBtn || refreshBtn.dataset.wired) return;
    refreshBtn.dataset.wired = '1';
    refreshBtn.addEventListener('click', loadManagersPanel);
    document.getElementById('managers-gossip-btn')?.addEventListener('click', async (e) => {
        e.target.disabled = true;
        try { const r = await apiCall('/managers/gossip-tick', { method: 'POST' });
            _mgrFlash('mgr-enroll-msg', `Gossip round: reached ${(r.heartbeated || []).length}, marked stale ${(r.marked_stale || []).length}.`, true);
        } catch (err) { _mgrFlash('mgr-enroll-msg', `Gossip failed: ${err.message || err}`, false); }
        finally { e.target.disabled = false; loadManagersPanel(); }
    });
    // Add a manager — generate a one-time join command (worker-parity onboarding)
    document.getElementById('mgr-add-btn')?.addEventListener('click', async () => {
        const region = (document.getElementById('mgr-add-region').value || '').trim();
        const selfUrl = (document.getElementById('mgr-add-selfurl').value || '').trim();
        const mid = (document.getElementById('mgr-add-id').value || '').trim();
        const externalUrl = (document.getElementById('mgr-add-external').value || '').trim();
        const primaryOverride = (document.getElementById('mgr-add-primary').value || '').trim();
        if (!selfUrl) { _mgrFlash('mgr-enroll-msg', "Enter the new VM's peer URL (internal IP/hostname).", false); return; }
        const body = { region: region || null, primary_url: primaryOverride || (window.APP_CONFIG && window.APP_CONFIG.API_URL) || window.location.origin };
        if (mid) body.manager_id = mid;
        if (externalUrl) body.external_url = externalUrl;
        try {
            const r = await apiCall('/managers/enrollment-tokens', { method: 'POST', body: JSON.stringify(body) });
            // Substitute the new VM's reachable URL into the generated command.
            const cmd = (r.bootstrap_command || '').replace('<THIS-VM-REACHABLE-URL>', selfUrl);
            const ta = document.getElementById('mgr-add-cmd');
            ta.value = cmd;
            document.getElementById('mgr-add-result').classList.remove('hidden');
            const exp = r.expires_at ? new Date(r.expires_at).toLocaleString() : 'soon';
            document.getElementById('mgr-add-meta').textContent =
                `Manager ${r.manager_id}${r.region ? ' · region ' + r.region : ''} · token expires ${exp} · one-time use. Run on the new VM after ./start-main-server.sh.`;
            _mgrFlash('mgr-enroll-msg', 'Join command generated — paste it on the new VM.', true);
        } catch (e) { _mgrFlash('mgr-enroll-msg', `Could not generate: ${e.message || e}`, false); }
    });
    document.getElementById('mgr-add-copy')?.addEventListener('click', () => {
        const ta = document.getElementById('mgr-add-cmd');
        ta.select();
        try { navigator.clipboard.writeText(ta.value); } catch (_) { document.execCommand('copy'); }
        const b = document.getElementById('mgr-add-copy'); const t = b.textContent;
        b.textContent = 'Copied ✓'; setTimeout(() => { b.textContent = t; }, 1500);
    });
    // event-delegated row actions
    document.getElementById('managers-tbody')?.addEventListener('click', (e) => {
        const b = e.target.closest('[data-mgr-act]');
        if (b) _mgrAction(b.dataset.mgrAct, b.dataset.mgrId);
    });
    // work modal close
    const modal = document.getElementById('mgr-work-modal');
    document.getElementById('mgr-work-close')?.addEventListener('click', () => { modal.classList.add('hidden'); modal.classList.remove('flex'); });
    modal?.addEventListener('click', (e) => { if (e.target === modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); } });
}

async function _mgrLoadEvents() {
    const tb = document.getElementById('mgr-events-tbody');
    if (!tb) return;
    try {
        const ev = await apiCall('/managers/ownership-events?limit=50');
        const list = Array.isArray(ev) ? ev : [];
        if (!list.length) { tb.innerHTML = '<tr><td colspan="6" class="px-4 py-4 text-center text-sm text-gray-400">No ownership transitions recorded.</td></tr>'; return; }
        tb.innerHTML = list.map(e => `<tr>
            <td class="px-4 py-2 text-gray-500">${_mgrRelTime(e.created_at)}</td>
            <td class="px-4 py-2"><span class="px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-700">${_mgrEsc(e.action)}</span></td>
            <td class="px-4 py-2 font-mono text-gray-800">${_mgrEsc(e.work_key)}</td>
            <td class="px-4 py-2 text-gray-600">${_mgrEsc(e.from_manager || '—')} → ${_mgrEsc(e.to_manager)}</td>
            <td class="px-4 py-2 text-gray-500">${_mgrEsc(e.fencing_token)}</td>
            <td class="px-4 py-2 text-gray-500">${_mgrEsc(e.reason || '—')}</td>
        </tr>`).join('');
    } catch (e) {
        tb.innerHTML = `<tr><td colspan="6" class="px-4 py-4 text-center text-sm text-destructive">Failed to load events: ${_mgrEsc(e.message || String(e))}</td></tr>`;
    }
}

async function loadManagersPanel() {
    _wireManagersPanel();
    const tbody = document.getElementById('managers-tbody');
    try {
        const data = await apiCall('/managers');
        const list = Array.isArray(data) ? data : [];
        _mgrList = list;
        document.getElementById('mgr-count').textContent = list.length;
        document.getElementById('mgr-active').textContent = list.filter(m => m.status === 'active' && m.admin_state === 'approved').length;
        document.getElementById('mgr-pending').textContent = list.filter(m => m.admin_state === 'pending').length;
        document.getElementById('mgr-regions').textContent = new Set(list.map(m => m.region).filter(Boolean)).size;
        if (!list.length) {
            tbody.innerHTML = '<tr><td colspan="7" class="px-4 py-6 text-center text-sm text-gray-500">No managers enrolled — this MOD is running as a single manager.</td></tr>';
        } else {
            tbody.innerHTML = list.map(m => `<tr${m.is_self ? ' class="bg-primary/5"' : ''}>
                <td class="px-4 py-3 font-mono text-gray-800">${_mgrEsc(m.manager_id)}${m.is_self ? ' <span class="text-xs text-primary">(self)</span>' : ''}</td>
                <td class="px-4 py-3 text-gray-700">${_mgrEsc(m.region || '—')}</td>
                <td class="px-4 py-3 text-gray-500 font-mono text-xs">
                    <div>${_mgrEsc(m.external_url || m.url || '—')}</div>
                    ${m.url ? `<div class="${_mgrPeerNonRoutable(m.url) ? 'text-amber-600' : 'text-gray-400'}">peer: ${_mgrEsc(m.url)}${_mgrPeerNonRoutable(m.url) ? ' ⚠ host IP not auto-detected — use Edit URLs to set this VM\'s IP' : ''}</div>` : ''}
                </td>
                <td class="px-4 py-3">${_mgrStatusBadge(m)}</td>
                <td class="px-4 py-3">${_mgrTrustBadge(m)}</td>
                <td class="px-4 py-3 text-gray-500 text-xs">${_mgrRelTime(m.last_seen)}</td>
                <td class="px-4 py-3 text-right">${_mgrRowActions(m)}</td>
            </tr>`).join('');
        }
        _mgrLoadEvents();
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="px-4 py-6 text-center text-sm text-destructive">Failed to load managers: ${_mgrEsc(e.message || String(e))}</td></tr>`;
    }
}

// ── tailnet-plan Group 9 — mesh topology (graph + posture + target selector) ──
let _cyInstance = null;

// WS4.44b — save race fix: loadTopologyPanel() awaits 3 sequential GETs
// before calling _renderTopologySelectors(), which unconditionally
// overwrote #topo-tier-select / #topo-relay-select / #topo-coord-input
// from the fetched posture. If that background load was still in flight
// when the operator changed a selector and clicked "Save target", the
// late-resolving GET clobbered their in-progress choice back to the old
// server value *before* _applyTopologyPosture() read `.value`, so the PUT
// silently re-saved the unchanged value (200 "Saved." shown, nothing
// actually changed). _topologyFormDirty tracks "user has touched a form
// field since the panel was last (re)opened"; _renderTopologySelectors
// skips repainting the form fields (summary/graph still update) while
// dirty, and _applyTopologyPosture clears it once its own save lands —
// at that point the fields already hold the just-saved values.
let _topologyFormDirty = false;

const _tierLabel = { 'disabled': 'Airgapped', 'mod-hosted': 'MOD Cloud', 'customer-owned': 'Customer-owned' };
const _relayLabel = { 'mod-derp': 'MOD DERP', 'customer-derp': 'Customer DERP', 'direct-only': 'Direct only' };
const _locLabel = { 'local': 'local', 'lan': 'LAN', 'remote': 'remote', 'cloud-burst': 'cloud burst' };

function _fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1000) return n + ' B';
    const u = ['kB', 'MB', 'GB', 'TB']; let i = -1;
    do { n /= 1000; i++; } while (n >= 1000 && i < u.length - 1);
    return n.toFixed(n < 10 ? 1 : 0) + ' ' + u[i];
}

function _wireTopologyPanel() {
    // SPA remount: page HTML re-injects on every visit, so mark a panel element, not the module.
    const topoAnchor = document.getElementById('topology-refresh-btn') || document.getElementById('topo-tier-select');
    if (!topoAnchor || topoAnchor.dataset.wired) return;
    topoAnchor.dataset.wired = '1';
    // Fresh panel instance — no in-progress edit yet (see _topologyFormDirty above).
    _topologyFormDirty = false;
    document.getElementById('topology-refresh-btn')?.addEventListener('click', loadTopologyPanel);
    const tierSel = document.getElementById('topo-tier-select');
    const relaySel = document.getElementById('topo-relay-select');
    const coordIn = document.getElementById('topo-coord-input');
    tierSel?.addEventListener('change', () => {
        _topologyFormDirty = true;
        const wrap = document.getElementById('topo-coord-wrap');
        if (wrap) wrap.classList.toggle('hidden', tierSel.value !== 'customer-owned');
        _updateTargetHint();
    });
    relaySel?.addEventListener('change', () => {
        _topologyFormDirty = true;
        _updateTargetHint();
    });
    coordIn?.addEventListener('input', () => { _topologyFormDirty = true; });
    document.getElementById('topo-apply-btn')?.addEventListener('click', _applyTopologyPosture);
    document.getElementById('topo-switch-btn')?.addEventListener('click', _switchTopologyPosture);
}

function _updateTargetHint() {
    // 9.5 — what the SELECTED tier/relay would unlock + whether it bills relay.
    const el = document.getElementById('topo-target-hint');
    if (!el) return;
    const tier = document.getElementById('topo-tier-select')?.value;
    const relay = document.getElementById('topo-relay-select')?.value;
    const caps = [];
    if (tier === 'mod-hosted' || tier === 'customer-owned') caps.push('off-manager GPU');
    if (tier === 'mod-hosted') caps.push('cloud burst', 'MOD-fronted FQDN');
    const bills = tier !== 'disabled' && relay === 'mod-derp';
    el.innerHTML = `Target unlocks: <span class="text-gray-800">${caps.length ? caps.join(', ') : 'none (fully local)'}</span>`
}

async function _switchTopologyPosture() {
    const msg = document.getElementById('topo-apply-msg');
    const out = document.getElementById('topo-switch-result');
    const tier = document.getElementById('topo-tier-select').value;
    const relay = document.getElementById('topo-relay-select').value;
    const coord = document.getElementById('topo-coord-input')?.value?.trim() || null;
    const preauth = window.prompt('Operator preauth key for the target coordinator (blank if MOD-minted or not needed):', '') || null;
    const body = { control_server_tier: tier, relay_owner: relay };
    if (coord) body.coordinator_location = coord;
    if (preauth) body.operator_preauth = preauth;
    if (msg) { msg.textContent = 'Switching…'; msg.className = 'text-xs text-gray-500'; }
    try {
        const r = await apiCall('/platform/mesh-replication-config/control-server/switch',
                                { method: 'POST', body: JSON.stringify(body) });
        if (msg) msg.textContent = '';
        if (out) {
            out.hidden = false; out.classList.remove('hidden');
            const lost = (r.placement_lost || []).map(s => s.name || s.stack_id).join(', ');
            out.innerHTML = `
                <div class="rounded-lg border ${r.ok ? 'border-emerald-300 bg-emerald-50' : 'border-amber-300 bg-amber-50'} p-3">
                    <div class="font-medium ${r.ok ? 'text-emerald-800' : 'text-amber-800'}">
                        ${r.ok ? '✓ Switched' : (r.rolled_back ? '↩ Rolled back' : '✗ Not switched')} — tier now: ${escapeHtml(r.tier_in_effect)} / relay ${escapeHtml(r.relay_in_effect)}
                    </div>
                    <div class="text-xs text-gray-600 mt-1">${escapeHtml(r.reason || '')} · re-enrolled ${r.services_switched}/${r.services_total} service(s)</div>
                    ${lost ? `<div class="text-xs text-amber-700 mt-1">Off-manager (placement lost, reported not dropped): ${escapeHtml(lost)}</div>` : ''}
                </div>`;
        }
        loadTopologyPanel();
    } catch (e) {
        if (msg) { msg.textContent = 'Switch failed: ' + (e.message || String(e)); msg.className = 'text-xs text-destructive'; }
    }
}

async function _applyTopologyPosture() {
    const msg = document.getElementById('topo-apply-msg');
    const tier = document.getElementById('topo-tier-select').value;
    const relay = document.getElementById('topo-relay-select').value;
    const coord = document.getElementById('topo-coord-input')?.value?.trim() || null;
    const body = { control_server_tier: tier, relay_owner: relay };
    if (coord) body.coordinator_location = coord;
    if (msg) { msg.textContent = 'Saving…'; msg.className = 'text-xs text-gray-500'; }
    try {
        const saved = await apiCall('/platform/mesh-replication-config/control-server',
                      { method: 'PUT', body: JSON.stringify(body) });
        if (saved && 'derp_required' in saved) _setDerpRequiredBadge(!!saved.derp_required);
        if (msg) { msg.textContent = 'Saved.'; msg.className = 'text-xs text-emerald-600'; }
        // The just-submitted values are now the server truth — safe for the
        // next background render to repaint the form again.
        _topologyFormDirty = false;
        loadTopologyPanel();
    } catch (e) {
        if (msg) { msg.textContent = 'Failed: ' + (e.message || String(e)); msg.className = 'text-xs text-destructive'; }
    }
}

function _setDerpRequiredBadge(required) {
    const el = document.getElementById('topo-derp-required');
    if (el) el.classList.toggle('hidden', !required);
}

function _renderTopologySummary(p) {
    const billing = (p && p.tier_capabilities && p.tier_capabilities.mod_relay_billing);
    const caps = (p && p.tier_capabilities) || {};
    const on = Object.entries(caps).filter(([, v]) => v).map(([k]) => k.replace(/_/g, ' '));
    const set = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
    set('topo-tier', p ? (_tierLabel[p.control_server_tier] || p.control_server_tier) : 'unknown');
    set('topo-tier-source', p ? `source: ${p.control_server_tier_source}` : '');
    set('topo-relay', p ? (p.relay_applicable ? (_relayLabel[p.relay_owner] || p.relay_owner) : 'n/a (airgapped)') : 'unknown');
    set('topo-relay-source', p && p.relay_applicable ? `source: ${p.relay_owner_source}` : '');
    const bytes = p && p.topology ? Number(p.topology.relay_total_bytes || 0) : 0;
    set('topo-billing', billing ? (bytes > 0 ? `ON — ${_fmtBytes(bytes)} this period` : 'ON — MOD relay billed') : 'off');
    set('topo-caps', on.length ? on.join(', ') : 'none');
}

function _renderTopologySelectors(p) {
    if (!p) return;
    if (_topologyFormDirty) {
        // A save race (WS4.44b): this render belongs to a GET that was
        // still in flight when the operator started editing the form (or
        // to one that raced a save). Repainting now would silently
        // discard their in-progress choice. The summary/graph elsewhere
        // in loadTopologyPanel() still update — only the editable form
        // fields are held back until the next save (or a fresh panel
        // open) clears the flag.
        return;
    }
    const tierSel = document.getElementById('topo-tier-select');
    const relaySel = document.getElementById('topo-relay-select');
    if (tierSel && p.control_server_tier) tierSel.value = p.control_server_tier;
    if (relaySel && p.relay_owner) relaySel.value = p.relay_owner;
    const wrap = document.getElementById('topo-coord-wrap');
    if (wrap) wrap.classList.toggle('hidden', p.control_server_tier !== 'customer-owned');
    const coordIn = document.getElementById('topo-coord-input');
    if (coordIn && p.coordinator_location) coordIn.value = p.coordinator_location;
    _updateTargetHint();
}

function _topologyElements(p, managers) {
    const els = [];
    const tier = p ? p.control_server_tier : null;
    const relayApplicable = p ? p.relay_applicable : true;
    const relayOwner = p ? p.relay_owner : 'direct-only';
    const hasCoord = tier !== 'disabled';

    if (hasCoord) {
        els.push({ data: { id: 'coord', label: `Coordinator\n${p ? (_tierLabel[tier] || tier) : 'unreachable'}`, kind: 'coord' } });
    } else {
        els.push({ data: { id: 'coord', label: 'Airgapped\n(no coordinator)', kind: 'airgap' } });
    }

    if (relayApplicable && relayOwner && relayOwner !== 'direct-only') {
        const billable = relayOwner === 'mod-derp';
        const bytes = p && p.topology ? Number(p.topology.relay_total_bytes || 0) : 0;
        const byteLabel = (billable && bytes > 0) ? ` · ${_fmtBytes(bytes)}` : '';
        els.push({ data: { id: 'relay', label: `DERP relay\n${_relayLabel[relayOwner]}`, kind: billable ? 'relay-mod' : 'relay-cust' } });
        els.push({ data: { source: 'coord', target: 'relay', link: 'relayed', label: (billable ? 'relayed · billed' : 'relayed') + byteLabel } });
    }

    let selfId = null;
    if (!managers.length) {
        selfId = 'mgr-self';
        els.push({ data: { id: selfId, label: 'This manager', kind: 'self' } });
        els.push({ data: { source: selfId, target: 'coord', link: 'direct' } });
    } else {
        managers.forEach((m, i) => {
            const id = 'mgr-' + (m.manager_id || i);
            if (m.is_self && !selfId) selfId = id;
            els.push({ data: { id, label: `${m.is_self ? '★ ' : ''}${m.region || m.manager_id || 'manager'}`, kind: m.is_self ? 'self' : 'peer' } });
            els.push({ data: { source: id, target: 'coord', link: 'direct' } });
        });
        if (!selfId) selfId = 'mgr-' + (managers[0].manager_id || 0);
    }

    // per-service leaf nodes, colored by location class (task 9.3)
    const services = (p && p.topology && Array.isArray(p.topology.services)) ? p.topology.services : [];
    services.slice(0, 60).forEach((s, i) => {
        const sid = 'svc-' + i;
        const name = s.magicdns_name || `${s.stack || ''}/${s.service || ''}`;
        const lost = (s.status === 'placement-lost');
        els.push({ data: { id: sid, label: `${name}\n[${_locLabel[s.location] || s.location || '?'}]${lost ? ' ⚠' : ''}`,
                           kind: lost ? 'svc-lost' : ('svc-' + (s.location || 'remote')) } });
        els.push({ data: { source: sid, target: selfId || 'coord', link: 'direct' } });
    });
    return els;
}

// B89 — a one-off failed dynamic import of the cytoscape vendor module (e.g.
// mid-deploy container swap serving a 5xx for a moment) gets cached by the
// browser's module map, so the topology graph stays broken until a hard
// refresh. _importWithRetry retries a few times with backoff, and the last
// retries bypass the import map with a cache-busting query so the browser
// can't reuse the failed module record.
async function _importWithRetry(loadFn, delays = [500, 1500]) {
    let lastErr;
    for (let attempt = 0; attempt <= delays.length; attempt++) {
        try {
            return await loadFn(attempt);
        } catch (e) {
            lastErr = e;
            if (attempt < delays.length) {
                await new Promise((resolve) => setTimeout(resolve, delays[attempt]));
            }
        }
    }
    throw lastErr;
}

async function _loadCytoscape() {
    const mod = await _importWithRetry((attempt) => (
        attempt === 0 ? import('cytoscape') : import(`/vendor/cytoscape.esm.min.js?r=${attempt}`)
    ));
    return mod.default;
}

async function _renderTopologyGraph(p, managers, statusEl) {
    const container = document.getElementById('topology-canvas');
    if (!container) return;
    let cytoscape;
    try {
        cytoscape = await _loadCytoscape();
    } catch (e) {
        container.innerHTML = `<div class="p-6 text-sm text-destructive">Graph library failed to load: ${escapeHtml(e.message || String(e))}
            <button id="topo-graph-retry-btn" type="button" class="ml-2 underline">Retry</button></div>`;
        document.getElementById('topo-graph-retry-btn')?.addEventListener('click', () => {
            _renderTopologyGraph(p, managers, statusEl);
        });
        return;
    }
    if (_cyInstance) { try { _cyInstance.destroy(); } catch (_) {} _cyInstance = null; }
    _cyInstance = cytoscape({
        container,
        elements: _topologyElements(p, managers),
        style: [
            { selector: 'node', style: {
                'label': 'data(label)', 'text-wrap': 'wrap', 'text-valign': 'center', 'text-halign': 'center',
                'color': '#fff', 'font-size': 10, 'text-max-width': 90, 'width': 74, 'height': 74,
                'background-color': '#64748b', 'text-outline-width': 0 } },
            { selector: 'node[kind="self"]', style: { 'background-color': '#2563eb', 'width': 84, 'height': 84 } },
            { selector: 'node[kind="peer"]', style: { 'background-color': '#64748b' } },
            { selector: 'node[kind="coord"]', style: { 'background-color': '#0ea5e9', 'shape': 'round-rectangle' } },
            { selector: 'node[kind="airgap"]', style: { 'background-color': '#9ca3af', 'shape': 'round-rectangle' } },
            { selector: 'node[kind="relay-mod"]', style: { 'background-color': '#f59e0b', 'shape': 'diamond' } },
            { selector: 'node[kind="relay-cust"]', style: { 'background-color': '#94a3b8', 'shape': 'diamond' } },
            // per-service leaf nodes by location class (task 9.3)
            { selector: 'node[kind^="svc-"]', style: { 'width': 46, 'height': 46, 'font-size': 8, 'shape': 'ellipse', 'background-color': '#0d9488' } },
            { selector: 'node[kind="svc-local"]', style: { 'background-color': '#0d9488' } },
            { selector: 'node[kind="svc-lan"]', style: { 'background-color': '#2563eb' } },
            { selector: 'node[kind="svc-remote"]', style: { 'background-color': '#7c3aed' } },
            { selector: 'node[kind="svc-cloud-burst"]', style: { 'background-color': '#db2777' } },
            { selector: 'node[kind="svc-lost"]', style: { 'background-color': '#dc2626', 'shape': 'octagon' } },
            { selector: 'edge', style: {
                'curve-style': 'bezier', 'width': 2, 'line-color': '#10b981',
                'label': 'data(label)', 'font-size': 8, 'color': '#6b7280', 'text-rotation': 'autorotate' } },
            { selector: 'edge[link="relayed"]', style: { 'line-color': '#f59e0b', 'line-style': 'dashed' } },
        ],
        layout: { name: 'concentric', concentric: n => (n.data('kind') === 'coord' || n.data('kind') === 'airgap') ? 10 : 1, minNodeSpacing: 40 },
        wheelSensitivity: 0.2,
    });
    if (statusEl && p) {
        statusEl.textContent = `Live · tier ${p.control_server_tier} (${p.control_server_tier_source}) · relay ${p.relay_applicable ? p.relay_owner : 'n/a'} · ${managers.length || 1} manager(s)`;
        statusEl.className = 'px-4 py-2 text-xs text-gray-500 border-t border-gray-100';
    }
}

async function loadTopologyPanel() {
    _wireTopologyPanel();
    const statusEl = document.getElementById('topology-status');
    let posture = null, managers = [];
    try {
        posture = await apiCall('/platform/mesh-replication-config/status');
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = 'Fabric unreachable — showing last-known managers only (' + (e.message || e) + ')';
            statusEl.className = 'px-4 py-2 text-xs text-amber-600 border-t border-gray-100';
        }
    }
    try { const m = await apiCall('/managers'); managers = Array.isArray(m) ? m : []; } catch (_) { managers = []; }
    try {
        const cs = await apiCall('/platform/mesh-replication-config/control-server');
        _setDerpRequiredBadge(!!(cs && cs.derp_required));
    } catch (_) {
        _setDerpRequiredBadge(false);
    }
    _renderTopologySummary(posture);
    _renderTopologySelectors(posture);
    await _renderTopologyGraph(posture, managers, statusEl);
    loadMeshAclSection();
}

// ── mod-connect-service-mesh §7.3 — mesh ACL view + per-stack egress edit ─────
const _ACL_MODES = ['none', 'api', 'hosts'];

async function loadMeshAclSection() {
    const badge = document.getElementById('acl-enforce-badge');
    const wrap = document.getElementById('acl-stacks');
    const pre = document.getElementById('acl-policy-json');
    if (!wrap) return; // panel markup not present
    document.getElementById('acl-refresh-btn')?.addEventListener('click', loadMeshAclSection, { once: true });
    wrap.innerHTML = '<div class="text-xs text-gray-400">Loading ACL…</div>';
    let data;
    try {
        data = await apiCall('/platform/mesh-acl');
    } catch (e) {
        wrap.innerHTML = `<div class="text-xs text-destructive">Could not load ACL: ${escapeHtml(e.message || String(e))}</div>`;
        if (badge) badge.textContent = '';
        return;
    }
    if (badge) {
        const on = !!data.enforcement_enabled;
        badge.innerHTML = on
            ? '<span class="px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 font-medium">enforcement ON</span>'
            : '<span class="px-2 py-0.5 rounded-full bg-gray-100 text-gray-600 font-medium">enforcement off</span>';
        if (!data.worker_api_port_ok) badge.innerHTML += ' <span class="text-amber-600">· worker API port unresolved</span>';
    }
    if (pre) pre.textContent = JSON.stringify(data.policy || {}, null, 2);

    const stacks = Array.isArray(data.stacks) ? data.stacks : [];
    if (!stacks.length) {
        wrap.innerHTML = '<div class="text-xs text-gray-400">No stacks — the ACL is the baseline worker rule only.</div>';
        return;
    }
    wrap.innerHTML = stacks.map(_aclStackCard).join('');
    stacks.forEach(s => {
        document.getElementById(`acl-save-${s.id}`)?.addEventListener('click', () => _saveStackEgress(s));
        (s.reachable_services || []).forEach(svc => {
            const sel = document.getElementById(`acl-mode-${s.id}-${_cssId(svc)}`);
            const hostsWrap = document.getElementById(`acl-hosts-${s.id}-${_cssId(svc)}`);
            sel?.addEventListener('change', () => {
                if (hostsWrap) hostsWrap.classList.toggle('hidden', sel.value !== 'hosts');
            });
        });
    });
}

function _cssId(s) { return String(s).replace(/[^a-zA-Z0-9_-]/g, '_'); }

function _aclStackCard(s) {
    const ep = s.egress_policy || {};
    const svcs = s.reachable_services || [];
    const rows = svcs.length ? svcs.map(svc => {
        const cur = ep[svc] || {};
        const mode = _ACL_MODES.includes(cur.mode) ? cur.mode : 'none';
        const hosts = Array.isArray(cur.hosts) ? cur.hosts.join(', ') : '';
        const opts = _ACL_MODES.map(m => `<option value="${m}"${m === mode ? ' selected' : ''}>${m}</option>`).join('');
        return `<div class="flex flex-wrap items-center gap-2 py-1">
            <span class="text-xs font-mono text-gray-700 w-40 truncate" title="${escapeHtml(svc)}">${escapeHtml(svc)}</span>
            <select id="acl-mode-${s.id}-${_cssId(svc)}" aria-label="Access mode for ${escapeHtml(svc)}" data-svc="${escapeHtml(svc)}" class="acl-mode-${s.id} px-2 py-1 border border-gray-300 rounded text-xs">${opts}</select>
            <span id="acl-hosts-${s.id}-${_cssId(svc)}" class="${mode === 'hosts' ? '' : 'hidden'}">
                <input id="acl-hostsin-${s.id}-${_cssId(svc)}" type="text" value="${escapeHtml(hosts)}" aria-label="Allowed hosts for ${escapeHtml(svc)}" placeholder="host1.example, host2.example"
                       class="px-2 py-1 border border-gray-300 rounded text-xs w-64" />
            </span>
        </div>`;
    }).join('') : '<div class="text-xs text-gray-400">No reachable services.</div>';
    return `<div class="border border-gray-200 rounded-lg p-3">
        <div class="flex items-center justify-between gap-2 mb-2">
            <div class="text-xs">
                <span class="font-semibold text-gray-800">${escapeHtml(s.name)}</span>
                <span class="text-gray-400">#${escapeHtml(s.id)} · tenant ${escapeHtml(s.tenant_id)} · <span class="font-mono">${escapeHtml(s.stack_tag)}</span></span>
            </div>
            <button id="acl-save-${s.id}" class="px-3 py-1 text-xs font-medium text-white bg-primary rounded hover:bg-primary/90"${svcs.length ? '' : ' disabled'}>Save</button>
        </div>
        ${rows}
    </div>`;
}

async function _saveStackEgress(s) {
    const msg = document.getElementById('acl-msg');
    const egress_policy = {};
    let bad = null;
    (s.reachable_services || []).forEach(svc => {
        const sel = document.getElementById(`acl-mode-${s.id}-${_cssId(svc)}`);
        if (!sel) return;
        const mode = sel.value;
        const entry = { mode };
        if (mode === 'hosts') {
            const raw = document.getElementById(`acl-hostsin-${s.id}-${_cssId(svc)}`)?.value || '';
            entry.hosts = raw.split(',').map(h => h.trim()).filter(Boolean);
            if (!entry.hosts.length) bad = svc;
        }
        egress_policy[svc] = entry;
    });
    if (bad) {
        if (msg) { msg.textContent = `Service "${bad}": mode 'hosts' needs at least one host.`; msg.className = 'text-xs mt-2 text-destructive'; }
        return;
    }
    if (msg) { msg.textContent = 'Saving…'; msg.className = 'text-xs mt-2 text-gray-500'; }
    try {
        await apiCall(`/platform/mesh-acl/stacks/${s.id}/egress`, { method: 'PUT', body: JSON.stringify({ egress_policy }) });
        if (msg) { msg.textContent = `Saved egress for ${s.name}.`; msg.className = 'text-xs mt-2 text-emerald-600'; }
        loadMeshAclSection();
    } catch (e) {
        if (msg) { msg.textContent = 'Save failed: ' + (e.message || String(e)); msg.className = 'text-xs mt-2 text-destructive'; }
    }
}

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Setup event listeners (called once from initializeAnalyticsPage)
function setupEventListeners() {
    // Refresh button
    document.getElementById('refresh-all-btn')?.addEventListener('click', () => {
        // Exclude #main-panel-settings: while a Settings child is active,
        // the container panel is also visible, and it comes first in DOM
        // order — it has no content of its own to refresh.
        const activePanel = document.querySelector('.main-panel:not(.hidden):not(#main-panel-settings)');
        if (activePanel.id === 'main-panel-system') {
            loadSystemHealth();
        } else if (activePanel.id === 'main-panel-usage') {
            loadUsageStats();
        } else if (activePanel.id === 'main-panel-workers') {
            loadWorkersPanel();
        }
    });

    // System → Settings nested sub-tabs — re-activate the child (same
    // idempotent loaders as the first activation).
    document.querySelectorAll('[id^="settings-subtab-"]').forEach(btn => {
        btn.addEventListener('click', () => {
            const childId = btn.id.replace('settings-subtab-', '');
            switchToTab('settings', childId);
        });
    });

    // Task logs modal close button
    document.getElementById('close-logs-modal')?.addEventListener('click', () => {
        document.getElementById('task-logs-modal').classList.add('hidden');
        resetAIChat();
    });
    document.getElementById('close-logs-btn')?.addEventListener('click', () => {
        document.getElementById('task-logs-modal').classList.add('hidden');
        resetAIChat();
    });

    // Download logs button
    document.getElementById('download-logs-btn')?.addEventListener('click', downloadTaskLogs);
}

async function checkUserRole() {
    try {
        currentUser = await me.get();
        isSuperAdmin = currentUser.parent_user_id === null;
        // FAILOPEN-E: fail closed - _ceEditionAn stays true (CE) unless api.js
        // isPaidEdition positively reports saas/pro, so an errored entitlements
        // stub or unreachable /platform/features hides the paid sub-tabs.
        _ceEditionAn = !(await isPaidEdition());

        // Workers sub-tab — platform-admin only. The previous gate also
        // revealed it to every logged-in user (`currentUser.tenant_id`
        // is truthy for everyone), which is why tenant users were
        // hitting the platform-only worker endpoints and getting 403.
        const isPlatformAdmin = currentUser.is_platform_admin;
        // Usage Stats — the System page is now visible to EVERY user, but a
        // regular member gets System Health + Capture Devices only. Usage Stats
        // stays for the broader system audience that could open the page before:
        // platform/tenant admin, or manage-permission / folder-ownership (flag
        // set by ui.js when it reveals the System nav). Now a child of the
        // System → Settings sub-tab; the Settings button reveals itself
        // below if ANY child is visible.
        if (isPlatformAdmin || isSuperAdmin || window.__systemFullAccess) {
            document.getElementById('settings-subtab-usage')?.classList.remove('hidden');
        }
        if (isPlatformAdmin) {
            document.getElementById('main-tab-workers')?.classList.remove('hidden');
        }
        // GH.8b — Cloud accounts sub-tab: the tenant's OWN cloud provider
        // accounts, not an operator lever, so it is tenant-admin gated
        // (mirrors the API's _require_tenant_admin) rather than
        // platform-admin-only.
        // The Cloud tab ships in every edition including CE (Q34/Q36).
        // Tim B3 item 5 (11-06) hid it on CE; owner 11-11 (answer B) supersedes
        // that - the tab is CE-visible, so the reveal keeps ONLY the tenant-admin
        // role gate (platform admin or tenant root), never an edition gate.
        if (isPlatformAdmin || isSuperAdmin) {
            document.getElementById('main-tab-burst-accounts')?.classList.remove('hidden');
        }
        // resource-monitor-merge — the Resource Monitor is now a block inside
        // the Usage Stats panel (above Sharing & permissions) rather than its
        // own sub-tab. Platform-admin only: unhide the block so loadUsageStats
        // will initialize it; non-admins never see it and never hit its
        // platform-admin-gated endpoints.
        if (isPlatformAdmin) {
            document.getElementById('resource-monitor-block')?.classList.add('hidden');
        }
        // Managers sub-tab (add-multi-region-mesh) — the multi-region
        // manager mesh. Platform-admin only. Sits after Resource Monitor.
        // 2400LR-pg6a: not on CE (mesh-replication-config + RPO dial are denied).
        if (isPlatformAdmin && !_ceEditionAn) {
            document.getElementById('main-tab-managers')?.classList.remove('hidden');
        }
        // Topology sub-tab (tailnet-plan) — platform-admin only, next to Managers.
        if (isPlatformAdmin && !_ceEditionAn) {
            document.getElementById('main-tab-topology')?.classList.remove('hidden');
        }
        // Show GPU Pool for platform admins only — relocated from its own
        // sidebar entry to a System page sub-tab, then under System → Settings.
        if (isPlatformAdmin && !_ceEditionAn) {
            document.getElementById('settings-subtab-gpu-pool')?.classList.remove('hidden');
        }
        // Persistent Stacks — platform-admin only (moved from a sidebar entry).
        if (isPlatformAdmin) {
            document.getElementById('main-tab-stacks')?.classList.remove('hidden');
        }
        // 0b.2 redo (R37.4 part 2, owner Q6 10-08) — Apps is a main tab on the
        // System page, immediately left of Settings. Tenant-admin only: routers/apps.py
        // gates every /apps route on the tenant manage_settings capability, so this
        // mirrors the old apps-nav-link gate in ui.js updateUIVisibility (isSuperAdmin,
        // i.e. parent_user_id IS NULL — platform admins are tenant roots too).
        // Tim B3 item 6 (11-06): "Apps is still there - we aren't launching apps
        // with ce first launch, it will come later" -> CE hides the tab too.
        if (isSuperAdmin && !_ceEditionAn) {
            document.getElementById('main-tab-apps')?.classList.remove('hidden');
        }
        // GF.C4 — Model Cache (manager-side cache ops), platform-admin only;
        // every endpoint is server-gated too. Now a Settings sub-tab.
        if (isPlatformAdmin && !_ceEditionAn) {
            document.getElementById('settings-subtab-model-cache')?.classList.remove('hidden');
        }
        // 23.7/GO.8 s2 — Change-Gate Report (weekly-sweep read-out), platform-
        // admin only; the route's _require_platform_admin gates it server-side too.
        if (isPlatformAdmin) {
            document.getElementById('settings-subtab-change-gate')?.classList.remove('hidden');
        }
        // GS.PSR.4 — Platform Settings (DB-backed tunables/flags),
        // platform-admin only; every endpoint is server-gated too.
        if (isPlatformAdmin) {
            document.getElementById('settings-subtab-platform-settings')?.classList.remove('hidden');
        }
        // FX.84 p2c — Backups (on-prem backup settings + readings),
        // platform-admin only; the router gates both endpoints too.
        // CE hides the tab (Tim B3 #8); the router is denied in the cut
        // (manifest :1445) so CE has no route either.
        if (isPlatformAdmin && !_ceEditionAn) {
            document.getElementById('settings-subtab-backups')?.classList.remove('hidden');
        }
        // wizard-queue-redesign (GE.WQR.24) — Activity (Wizard Queue) is
        // visible to every signed-in role, unlike the tabs above: the list
        // endpoint is already role-scoped server-side (platform admin sees
        // platform-level runs, tenant admin sees their tenant's runs, a
        // regular member sees only their own), so there is no admin gate here.
        document.getElementById('settings-subtab-activity')?.classList.remove('hidden');
        // System → Settings — reveal the container if ANY child is visible.
        // Activity is visible to everyone, so the Settings button is always
        // shown; the guard stays here so a future gate on Activity (or any
        // child) correctly hides the container too.
        if (_settingsVisibleChildren().length > 0) {
            document.getElementById('main-tab-settings')?.classList.remove('hidden');
        }
    } catch (error) {
        console.error('Error checking user role:', error);
    }
}

// ========================================
// System Health Tab
// ========================================

let _capacityInterval = null;

// Cosmetic map — turns the backend's lowercase `kind` field (cuda /
// rocm / intel / npu / cpu / ...) into a human-friendly label and a
// per-backend colour family for tile rendering. Unknown backends fall
// back to a neutral slate theme.
const GPU_BACKEND_PRESENTATION = {
    cuda:  { label: 'NVIDIA (CUDA)', theme: 'purple',  gradient: 'from-purple-50 to-pink-50',    border: 'border-purple-200', title: 'text-purple-700', value: 'text-purple-900', bar: 'bg-purple-600',  barBg: 'bg-purple-100',  sub: 'text-purple-600' },
    rocm:  { label: 'AMD (ROCm)',   theme: 'red',     gradient: 'from-red-50 to-rose-50',       border: 'border-red-200',    title: 'text-red-700',    value: 'text-red-900',    bar: 'bg-red-600',    barBg: 'bg-red-100',    sub: 'text-red-600' },
    intel: { label: 'Intel GPU',    theme: 'sky',     gradient: 'from-sky-50 to-blue-50',       border: 'border-sky-200',    title: 'text-sky-700',    value: 'text-sky-900',    bar: 'bg-sky-600',    barBg: 'bg-sky-100',    sub: 'text-sky-600' },
    npu:   { label: 'NPU',          theme: 'teal',    gradient: 'from-teal-50 to-cyan-50',      border: 'border-teal-200',   title: 'text-teal-700',   value: 'text-teal-900',   bar: 'bg-teal-600',   barBg: 'bg-teal-100',   sub: 'text-teal-600' },
};

function _presentationFor(kind) {
    return GPU_BACKEND_PRESENTATION[kind] || {
        label: (kind || 'GPU').toUpperCase(),
        gradient: 'from-slate-50 to-gray-50', border: 'border-slate-200',
        title: 'text-slate-700', value: 'text-slate-900',
        bar: 'bg-slate-600', barBg: 'bg-slate-100', sub: 'text-slate-600',
    };
}

// Stable render order so tiles don't shuffle positions between polls.
// Object.keys() preserves insertion order, but the backend builds
// gpu_by_backend incrementally from however the workers happen to be
// iterated — that's unstable. Pin the known backends to a fixed
// priority, and sort anything else alphabetically so new backend
// types (future hardware) slot in deterministically.
const GPU_BACKEND_ORDER = ['cuda', 'rocm', 'intel', 'npu'];
function _sortedBackendKinds(byBackend) {
    const keys = Object.keys(byBackend || {});
    return keys.sort((a, b) => {
        const ia = GPU_BACKEND_ORDER.indexOf(a);
        const ib = GPU_BACKEND_ORDER.indexOf(b);
        if (ia !== -1 && ib !== -1) return ia - ib;
        if (ia !== -1) return -1;
        if (ib !== -1) return 1;
        return a.localeCompare(b);
    });
}

function _backendTileSkeleton(kind, b) {
    const p = _presentationFor(kind);
    // Bar reflects real VRAM utilization (used/licensed). Text labels
    // match. Fall back to licensed/total if the API is old.
    const usedVram = typeof b.used_vram_gb === 'number' ? b.used_vram_gb : b.licensed_vram_gb;
    const vramLabel = typeof b.used_vram_gb === 'number'
        ? `${usedVram} / ${b.licensed_vram_gb} GB`
        : `${b.licensed_vram_gb} / ${b.total_vram_gb} GB`;
    const initialPct = typeof b.utilization_pct === 'number'
        ? b.utilization_pct
        : (b.total_vram_gb > 0 ? Math.min(100, (b.licensed_vram_gb / b.total_vram_gb) * 100) : 0);
    return `
        <div class="bg-gradient-to-br ${p.gradient} rounded-lg p-4 border ${p.border}" data-gpu-backend="${escapeHtml(kind)}" id="gpu-tile-${escapeHtml(kind)}">
            <div class="text-sm ${p.title} font-medium mb-1">${escapeHtml(p.label)}</div>
            <div class="text-2xl font-bold ${p.value}" data-role="count">${b.licensed_count} / ${b.total_count}</div>
            <div class="text-xs ${p.sub} mt-1">Registered / Total (devices)</div>
            <div class="mt-3">
                <div class="flex items-center justify-between text-xs ${p.title} mb-1">
                    <span>VRAM in use</span>
                    <span class="font-medium" data-role="vram">${vramLabel}</span>
                </div>
                <div class="w-full ${p.barBg} rounded-full h-2">
                    <div class="${p.bar} h-2 rounded-full transition-all duration-700" data-role="bar" style="width: ${Math.min(100, initialPct)}%"></div>
                </div>
            </div>
        </div>
    `;
}

// Cache of the last-rendered backend set so we only rebuild innerHTML
// when the *shape* of the grid actually changes (new backend joined,
// or all workers with a given backend went offline). Normal polls just
// patch the numbers inside the existing tiles — no innerHTML churn,
// no CSS transition re-fire, no DOM reflow, no shuffle.
let _lastRenderedBackendSignature = null;

function _renderBackendTiles(byBackend) {
    const grid = document.getElementById('cap-gpu-backend-grid');
    if (!grid) return;
    const kinds = _sortedBackendKinds(byBackend);
    const signature = kinds.join(',');

    if (kinds.length === 0) {
        if (_lastRenderedBackendSignature !== '__empty__') {
            grid.innerHTML = `
                <div class="bg-gradient-to-br from-slate-50 to-gray-50 rounded-lg p-4 border border-slate-200">
                    <div class="text-sm text-slate-700 font-medium mb-1">GPUs</div>
                    <div class="text-2xl font-bold text-slate-900">None detected</div>
                    <div class="text-xs text-slate-600 mt-1">Connect a worker with a CUDA, ROCm, Intel, or NPU device</div>
                </div>
            `;
            _lastRenderedBackendSignature = '__empty__';
        }
        return;
    }

    // Shape changed → full rebuild (rare, only on worker join/leave).
    if (signature !== _lastRenderedBackendSignature) {
        grid.innerHTML = kinds.map(k => _backendTileSkeleton(k, byBackend[k])).join('');
        _lastRenderedBackendSignature = signature;
        return;
    }

    // Shape unchanged → patch numbers in place. No DOM teardown, so
    // the CSS transition on width only fires when the value really
    // moves (which is what `transition-all` is designed for).
    for (const kind of kinds) {
        const b = byBackend[kind];
        const tile = document.getElementById(`gpu-tile-${kind}`);
        if (!tile) continue;
        const countEl = tile.querySelector('[data-role="count"]');
        const vramEl = tile.querySelector('[data-role="vram"]');
        const barEl  = tile.querySelector('[data-role="bar"]');
        if (countEl) countEl.textContent = `${b.licensed_count} / ${b.total_count}`;
        // VRAM label shows used/licensed so the numerator of the fill
        // and the text tell the same story. Falls back to licensed/total
        // if the API is old and doesn't yet emit used_vram_gb.
        if (vramEl) {
            if (typeof b.used_vram_gb === 'number') {
                vramEl.textContent = `${b.used_vram_gb} / ${b.licensed_vram_gb} GB`;
            } else {
                vramEl.textContent = `${b.licensed_vram_gb} / ${b.total_vram_gb} GB`;
            }
        }
        if (barEl) {
            // Prefer the API's utilization_pct (used/licensed, capped at
            // 100%). Older APIs emitted licensed/total which was always
            // ~100% on healthy deploys and therefore useless.
            const pct = typeof b.utilization_pct === 'number'
                ? b.utilization_pct
                : (b.total_vram_gb > 0 ? Math.min(100, (b.licensed_vram_gb / b.total_vram_gb) * 100) : 0);
            barEl.style.width = `${Math.min(100, pct)}%`;
        }
    }
}

function _updateCapacityBars(capacity) {
    if (!capacity) return;
    const lic = capacity.licensed_capacity || {};
    const u = capacity.utilization || {};

    const cpuPct = lic.cpu_cores > 0 ? ((u.cpu_cores_used || 0) / lic.cpu_cores * 100).toFixed(1) : '0.0';
    const memPct = lic.memory_gb > 0 ? Math.round((u.memory_gb_used || 0) / lic.memory_gb * 100) : 0;

    const set = (barId, labelId, pct, labelText) => {
        const bar = document.getElementById(barId);
        const label = document.getElementById(labelId);
        if (bar) bar.style.width = `${Math.min(100, pct)}%`;
        if (label) label.textContent = labelText;
    };

    set('cap-cpu-bar', 'cap-cpu-label', cpuPct, `${u.cpu_cores_used || 0} cores (${cpuPct}%)`);
    set('cap-mem-bar', 'cap-mem-label', memPct, `${u.memory_gb_used || 0} GB (${memPct}%)`);

    // Per-backend GPU tiles refresh on every poll so a worker joining
    // (or draining) is reflected without requiring a full page reload.
    _renderBackendTiles(capacity.gpu_by_backend || {});

    // Unrunnable-task banner. Shows when the scheduler's
    // unrunnable_tasks_sweep has flagged PENDING tasks whose
    // gpu_variants don't match any current worker's hardware_tags —
    // the operator either adds a worker with the right hw or edits
    // the node to skip variants they can't run.
    _renderUnrunnableBanner(capacity.unrunnable_tasks || null);
}

function _wireAdapterCacheFlushButton() {
    const btn = document.getElementById('adapter-cache-flush-btn');
    const input = document.getElementById('adapter-cache-flush-tenant');
    const result = document.getElementById('adapter-cache-flush-result');
    if (!btn || btn._wired) return;
    btn._wired = true;
    btn.addEventListener('click', async () => {
        const tenantId = parseInt((input?.value || '').trim(), 10);
        if (!tenantId || tenantId < 1) {
            if (result) {
                result.textContent = 'Enter a valid tenant ID first.';
                result.className = 'text-xs mt-2 text-red-600';
            }
            return;
        }
        btn.disabled = true;
        if (result) {
            result.textContent = 'Flushing…';
            result.className = 'text-xs mt-2 text-gray-500';
        }
        try {
            const data = await apiCall(
                `/platform/tenants/${tenantId}/adapter-cache/flush`,
                { method: 'POST' },
            );
            if (result) {
                result.textContent = data?.message ||
                    `Cache flushed for tenant ${tenantId} (salt=${data?.new_salt}).`;
                result.className = 'text-xs mt-2 text-emerald-600';
            }
        } catch (e) {
            if (result) {
                result.textContent = `Flush failed: ${e?.message || e}`;
                result.className = 'text-xs mt-2 text-red-600';
            }
        } finally {
            btn.disabled = false;
        }
    });
}

function _renderUnrunnableBanner(summary) {
    const host = document.getElementById('platform-capacity-overview');
    if (!host) return;
    let banner = document.getElementById('unrunnable-tasks-banner');
    const count = summary && summary.count ? summary.count : 0;
    if (count === 0) {
        if (banner) banner.remove();
        return;
    }
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'unrunnable-tasks-banner';
        banner.className = (
            'bg-amber-50 border-l-4 border-amber-500 text-amber-900 ' +
            'p-4 rounded-r mb-4 flex items-center justify-between'
        );
        host.prepend(banner);
    }
    const required = (summary.required_tags || []).join(', ') || '(unknown)';
    banner.innerHTML = `
        <div>
            <div class="font-medium">
                ${count} task${count === 1 ? '' : 's'} waiting for hardware you don't have
            </div>
            <div class="text-sm mt-0.5">
                Required: <span class="font-mono">${escapeHtml(required)}</span>.
                Add a worker with a matching hardware tag, or edit the
                node to skip variants you can't run.
            </div>
        </div>
    `;
}

async function _pollCapacity() {
    try {
        const capacity = await apiCall('/workers/capacity');
        _updateCapacityBars(capacity);
    } catch (e) {
        // Non-admin tenants get 403 here — stop polling rather than
        // spamming the browser network log with a 403 every 5s.
        if (e?.status === 403) _stopCapacityPolling();
    }
}

function _startCapacityPolling() {
    if (_capacityInterval) clearInterval(_capacityInterval);
    // 5s, not 1s — capacity changes on seconds-to-minutes scale.
    // The prior 1000ms poll meant 50 users → 50 req/s on this single
    // endpoint, which dominated the API's CPU budget at launch scale.
    // A small (random-jittered) start delay avoids every browser tab
    // hitting the API at exactly the same second.
    setTimeout(() => {
        _pollCapacity();
        _capacityInterval = setInterval(_pollCapacity, 5000);
    }, Math.floor(Math.random() * 1000));
}

function _stopCapacityPolling() {
    if (_capacityInterval) { clearInterval(_capacityInterval); _capacityInterval = null; }
}

// ============================================================================
// System → External: load on the tenant's external orchestrators (Kubernetes /
// Qube / Deadline). Data: GET /system/external (services.external_load). This
// is NOT MOD's own workers — it's the work MOD dispatched to external farms.
// ============================================================================
let _externalInterval = null;

function _extFmtDur(s) {
    if (s == null) return '—';
    s = Math.round(s);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
    return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}

function _extStatusBadge(st) {
    st = (st || '').toLowerCase();
    const map = {
        running: 'bg-blue-100 text-blue-800', pending_action: 'bg-blue-100 text-blue-800',
        pending: 'bg-gray-100 text-gray-600', queued: 'bg-gray-100 text-gray-600',
        claimed: 'bg-gray-100 text-gray-600', assigned: 'bg-gray-100 text-gray-600',
        success: 'bg-green-100 text-green-800', completed: 'bg-green-100 text-green-800',
        failed: 'bg-red-100 text-red-900', failure: 'bg-red-100 text-red-900',
        cancelled: 'bg-gray-100 text-gray-600',
    };
    const cls = map[st] || 'bg-gray-100 text-gray-600';
    return `<span class="px-2 py-1 text-xs font-medium rounded-full ${cls}">${escapeHtml(st || 'queued')}</span>`;
}

async function loadExternalPanel(isPoll = false) {
    try {
        // EXTFARM-3 — the farm registry rows are a separate host (#external-farms-list)
        // loaded only on activation (a 15 s poll must not re-render them, so an open
        // add-dialog and a half-typed form survive the poll).
        if (!isPoll) await _extLoadFarms();
        const data = await apiCall('/system/external');
        const execs = data.executors || [];

        const sum = (k) => execs.reduce((a, e) => a + ((e.totals && e.totals[k]) || 0), 0);
        const setText = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        setText('external-active', sum('active'));
        setText('external-queued', sum('queued'));
        setText('external-succeeded', sum('succeeded'));
        setText('external-failed', sum('failed'));
        const upd = document.getElementById('external-updated');
        if (upd && data.as_of) upd.textContent = 'Updated ' + new Date(data.as_of).toLocaleTimeString();

        const host = document.getElementById('external-executors');
        const anyConfigured = execs.some(e => e.configured);
        if (host && !anyConfigured) {
            // EXTFARM-3 empty state (MUST 6): farms can now be REGISTERED from this
            // page (the Add External Orchestrator button is in the header row and stays
            // visible in the empty state), so the text names both steps.
            host.innerHTML = `<div class="bg-white p-8 rounded-lg border border-dashed border-gray-300 text-center">
                <p class="text-gray-700 font-medium">${escapeHtml(_EXT_FARM_EMPTY_TEXT)}</p>
                <p class="text-gray-500 text-sm mt-1">Its load will show here once a matching <span class="font-medium">Submit</span> node dispatches work.</p>
            </div>`;
        } else if (host) {
            host.innerHTML = execs.map(e => {
                const t = e.totals || {};
                const targets = (e.targets || []);
                const badge = e.configured
                    ? `<span class="px-2 py-1 text-xs font-medium rounded-full bg-green-100 text-green-800">${targets.length} farm${targets.length === 1 ? '' : 's'}</span>`
                    : `<span class="px-2 py-1 text-xs font-medium rounded-full bg-gray-100 text-gray-500">not used</span>`;
                const targetsHtml = targets.length ? targets.map(tg => `
                    <div class="flex items-center justify-between py-2 border-t border-gray-100 text-sm">
                        <span class="font-mono text-gray-700 truncate" style="max-width:45%" title="${escapeHtml(tg.target)}">${escapeHtml(tg.target)}</span>
                        <span class="flex items-center gap-3 text-xs whitespace-nowrap">
                            <span class="text-blue-600">${tg.active} active</span>
                            <span class="text-gray-500">${tg.queued} queued</span>
                            <span class="text-green-600">${tg.succeeded} ok</span>
                            <span class="text-red-600">${tg.failed} fail</span>
                            <span class="text-gray-400">${tg.avg_duration_s != null ? _extFmtDur(tg.avg_duration_s) + ' avg' : ''}</span>
                        </span>
                    </div>`).join('') : '';
                return `<div class="bg-white p-5 rounded-lg border border-gray-200">
                    <div class="flex items-center justify-between">
                        <h3 class="text-md font-semibold text-gray-800">${escapeHtml(e.label)}</h3>
                        ${badge}
                    </div>
                    <div class="flex items-center gap-4 mt-2 text-sm text-gray-600">
                        <span class="text-blue-600">${t.active || 0} active</span>
                        <span>${t.queued || 0} queued</span>
                        <span class="text-green-600">${t.succeeded || 0} ok</span>
                        <span class="text-red-600">${t.failed || 0} fail</span>
                    </div>
                    ${e.configured ? `<div class="mt-2">${targetsHtml}</div>` : ''}
                </div>`;
            }).join('');
        }

        const rows = [];
        execs.forEach(e => (e.targets || []).forEach(tg => (tg.recent || []).forEach(r => {
            rows.push(Object.assign({}, r, { farm: `${e.label} · ${tg.target}` }));
        })));
        rows.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''));
        const tbody = document.getElementById('external-recent-tbody');
        if (tbody) {
            tbody.innerHTML = rows.length ? rows.slice(0, 25).map(r => `
                <tr>
                    <td class="px-4 py-2 text-sm text-gray-700 truncate" style="max-width:220px" title="${escapeHtml(r.farm)}">${escapeHtml(r.farm)}</td>
                    <td class="px-4 py-2 text-sm text-gray-500">${r.run_id ? '#' + r.run_id : ''} / ${escapeHtml(r.node_id || '')}</td>
                    <td class="px-4 py-2 text-sm">${_extStatusBadge(r.status)}</td>
                    <td class="px-4 py-2 text-sm text-right text-gray-600">${_extFmtDur(r.duration_s)}</td>
                </tr>`).join('') : `<tr><td colspan="4" class="px-4 py-4 text-center text-xs text-gray-400">No external jobs in the last 24h.</td></tr>`;
        }

        if (!isPoll) _startExternalPolling();
    } catch (e) {
        if (e && e.status === 403) { _stopExternalPolling(); return; }
        // transient — keep last rendered state
    }
}

function _startExternalPolling() {
    if (_externalInterval) clearInterval(_externalInterval);
    setTimeout(() => {
        _externalInterval = setInterval(() => loadExternalPanel(true), 15000);
    }, Math.floor(Math.random() * 1000));
}

function _stopExternalPolling() {
    if (_externalInterval) { clearInterval(_externalInterval); _externalInterval = null; }
}

// ============================================================
// EXTFARM-3 — External Orchestrator farm registry UI (owner 11-04:
// "External Orchestrators page has no way to add a farm").
// Contract (CORE/API/routers/system_external.py + services/external_farms.py):
//   GET  /system/external/farms -> [{id,kind,name,endpoint,is_active,has_credential,created_at}]
//   POST /system/external/farms  {kind,name,endpoint,credential?} -> 201 FarmSummary (no credential)
//   DELETE /system/external/farms/{id} -> {id,deleted}
//   errors {detail:{code,message}} (403 tenant_admin_required, 422 validation, 409 dup, 503 vault)
// SECRETS: the credential lives ONLY in the password-type input, is read once here,
// goes only into the POST JSON body (the backend wraps it in SecretStr and stores it
// in the Vault), and is cleared after submit success AND failure and on close. It is
// never rendered, logged, or kept client-side. The list shows only has_credential.
// ============================================================
const _EXT_FARM_KINDS = ['kubernetes', 'qube', 'deadline'];
const _EXT_FARM_NAME_MAX = 120;
const _EXT_FARM_ENDPOINT_MAX = 512;
const _EXT_FARM_CRED_MAX = 4096;
const _EXT_FARM_EMPTY_TEXT = 'No external orchestrators yet. Add a Kubernetes, Qube, or Deadline farm, then use the matching Submit node in a workflow to dispatch work to it.';
let _extFarmsWired = false;
let _extSubmitInFlight = false;

function _extDialogOpen() {
    const d = document.getElementById('external-farm-dialog');
    return !!d && !d.classList.contains('hidden');
}

function _extClearCredential() {
    const c = document.getElementById('external-farm-credential');
    if (c) c.value = '';
}

function _extCloseDialog() {
    const d = document.getElementById('external-farm-dialog');
    if (d) d.classList.add('hidden');
    _extClearCredential();
    const err = document.getElementById('external-farm-error');
    if (err) { err.textContent = ''; err.classList.add('hidden'); }
}

function _extSetError(text) {
    const err = document.getElementById('external-farm-error');
    if (!err) return;
    err.innerHTML = escapeHtml(text);
    err.classList.remove('hidden');
}

function _extClearError() {
    const err = document.getElementById('external-farm-error');
    if (err) { err.textContent = ''; err.classList.add('hidden'); }
}

// Client-side mirror of the server's guards (the server stays the authority:
// _validate_farm_endpoint in routers/system_external.py is what decides).
function _extValidateFarm(kind, name, endpoint) {
    if (!_EXT_FARM_KINDS.includes(kind)) return 'Choose a farm kind: Kubernetes, Qube or Deadline.';
    if (!name || name.length > _EXT_FARM_NAME_MAX) return `Farm name must be 1-${_EXT_FARM_NAME_MAX} characters.`;
    if (!endpoint || endpoint.length > _EXT_FARM_ENDPOINT_MAX) return 'Farm endpoint is required (max 512 characters).';
    if (!/^https?:\/\//i.test(endpoint)) return 'Farm endpoint must start with http:// or https://.';
    if (/[\s\u0000-\u001f\u007f]/.test(endpoint)) return 'Farm endpoint must not contain whitespace or control characters.';
    if (/@/.test(endpoint.split('://')[1] || '')) return 'Farm endpoint must not carry a user or password.';
    if (/#/.test(endpoint)) return 'Farm endpoint must not carry a fragment.';
    return null;
}

function _extRenderFarms(list) {
    const host = document.getElementById('external-farms-list');
    if (!host) return;
    if (!list || !list.length) {
        host.innerHTML = `<div class="bg-white p-6 rounded-lg border border-dashed border-gray-300 text-center">
            <p class="text-gray-700 font-medium">${escapeHtml(_EXT_FARM_EMPTY_TEXT)}</p>
        </div>`;
        return;
    }
    host.innerHTML = list.map(f => {
        const id = Number(f.id);
        const badge = f.has_credential
            ? `<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800">credential stored</span>`
            : `<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-500">no credential</span>`;
        const active = f.is_active === false ? 'bg-gray-100 text-gray-500' : 'bg-green-50 text-green-900';
        return `<div class="bg-white p-4 rounded-lg border border-gray-200 flex items-center justify-between gap-2">
            <span class="text-sm font-medium text-gray-800">${escapeHtml(f.name || '')}</span>
            <span class="text-xs text-gray-500 mt-1 font-mono truncate" title="${escapeHtml(f.endpoint || '')}">${escapeHtml(f.endpoint || '')}</span>
            <span class="flex items-center gap-2 mt-1">
                <span class="px-2 py-0.5 rounded-full text-xs font-medium ${active}">${escapeHtml((f.kind || '') + (f.is_active === false ? ' (inactive)' : ''))}</span>
                ${badge}
            </span>
            <button data-testid="external-farm-delete-${id}" data-farm-id="${id}" class="px-2 py-1 text-xs font-medium text-red-700 rounded border border-red-300 hover:bg-red-100" aria-label="Delete farm ${id}">Delete</button>
        </div>`;
    }).join('');
    // Bind the freshly rendered delete buttons (ids come from the server, so
    // they are re-bound on every reload; addEventListener, never inline onclick).
    host.querySelectorAll('button[data-farm-id]').forEach((b) => {
        b.addEventListener('click', () => _extDeleteFarm(Number(b.dataset.farmId)));
    });
}

async function _extLoadFarms() {
    const host = document.getElementById('external-farms-list');
    if (!host) return;
    try {
        const farms = await apiCall('/system/external/farms');
        _extRenderFarms(Array.isArray(farms) ? farms : []);
    } catch (e) {
        if (e && (e.status === 403 || e.status === 404)) {
            // 403: not allowed to view the registry; 404: routes not in this build.
            host.innerHTML = `<p class="text-xs text-gray-400">Farm registry unavailable here — ${e.status === 404 ? 'this build does not serve the farm routes' : 'only tenant admins can manage farms'}.</p>`;
            return;
        }
        // transient — keep last rendered rows
    }
}

async function _extSubmitFarm() {
    if (_extSubmitInFlight) return;   // no double create while a request is in flight
    const kindSel = document.getElementById('external-farm-kind');
    const nameIn = document.getElementById('external-farm-name');
    const endIn = document.getElementById('external-farm-endpoint');
    const credIn = document.getElementById('external-farm-credential');
    const kind = kindSel ? String(kindSel.value || '') : '';
    const name = nameIn ? String(nameIn.value || '') : '';
    const endpoint = endIn ? String(endIn.value || '') : '';
    const bad = _extValidateFarm(kind, name, endpoint);
    if (bad) { _extSetError(bad); return; }
    // The credential is read exactly here and goes nowhere else.
    const credential = credIn ? String(credIn.value || '') : '';
    if (credential && (credential.length > _EXT_FARM_CRED_MAX)) {
        _extClearCredential(); _extSetError('Farm credential must be 1-4096 characters.'); return;
    }
    const submitBtn = document.getElementById('external-farm-submit');
    if (submitBtn) submitBtn.disabled = true;
    _extSubmitInFlight = true;
    try {
        const payload = { kind, name, endpoint };
        if (credential) payload.credential = credential;
        await apiCall('/system/external/farms', { method: 'POST', body: JSON.stringify(payload) });
        _extClearCredential();
        _extCloseDialog();
        await _extLoadFarms();
        await loadExternalPanel(true);
    } catch (e) {
        _extClearCredential();   // cleared on failure too (MUST 1)
        if (e && e.status >= 500) {
            _extSetError('Could not register the farm right now. Try again later.');
        } else {
            const msg = (e && e.detail && typeof e.detail === 'object' && e.detail.message)
                ? String(e.detail.message) : String(e && e.message ? e.message : 'Could not register the farm.');
            _extSetError(msg);
        }
    } finally {
        _extSubmitInFlight = false;
        if (submitBtn) submitBtn.disabled = false;
    }
}

async function _extDeleteFarm(id) {
    const n = Number(id);
    if (!Number.isFinite(n) || n <= 0) return;
    if (!confirm(`Delete this external farm (${n})? Its Vault credential is removed too.`)) return;
    try {
        await apiCall('/system/external/farms/' + n, { method: 'DELETE' });
    } catch (e) {
        if (e && e.status >= 500) alert('Could not delete the farm right now. Try again later.');
        else alert(`Delete failed: ${String(e && e.detail && typeof e.detail === 'object' && e.detail.message ? e.detail.message : (e && e.message ? e.message : 'unknown error'))}`);
        return;
    }
    await _extLoadFarms();
    await loadExternalPanel(true);
}

function _extWireFarmUi() {
    if (_extFarmsWired) return;
    _extFarmsWired = true;
    document.getElementById('external-add-btn')?.addEventListener('click', () => {
        if (_extDialogOpen()) return;
        document.getElementById('external-farm-dialog')?.classList.remove('hidden');
        _extClearError();
        // focus moves to the first field
        document.getElementById('external-farm-name')?.focus();
    });
    document.getElementById('external-farm-submit')?.addEventListener('click', () => { void _extSubmitFarm(); });
    document.getElementById('external-farm-cancel')?.addEventListener('click', () => _extCloseDialog());
    document.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Escape' || !_extDialogOpen()) return;
        _extCloseDialog();
    });
}

// Gate 12 — resolved registries. Configuration nobody can inspect is
// configuration nobody trusts, and until the 33 literals were removed the
// environment and the behaviour did not agree. Shown to platform admins only.
async function loadRegistriesCard() {
    const card = document.getElementById('registries-card');
    const body = document.getElementById('registries-body');
    if (!card || !body) return;
    if (!currentUser?.is_platform_admin) { card.classList.add('hidden'); return; }
    card.classList.remove('hidden');
    try {
        const resp = await fetch(`${window.APP_CONFIG?.API_URL || ''}/platform/registries`, {
            headers: { Authorization: `Bearer ${sessionStorage.getItem('accessToken')}` },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const d = await resp.json();
        const esc = (t) => String(t == null ? '' : t)
            .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        const rows = (d.entries || []).map((e) => `
            <tr class="border-t border-gray-100">
                <td class="py-2 pr-4 text-gray-800">${esc(e.role)}</td>
                <td class="py-2 pr-4 font-mono text-xs text-gray-900">${esc(e.host)}</td>
                <td class="py-2 pr-4"><code class="text-[11px] text-gray-500">${esc(e.env_var)}</code></td>
                <td class="py-2 pr-4">${e.reachable === true
                    ? '<span class="px-2 py-0.5 rounded-full text-[11px] bg-green-100 text-green-700" title="Answered a registry API probe. A 401 counts — a private registry asking for credentials is a healthy one.">answering</span>'
                    : e.reachable === false
                    ? '<span class="px-2 py-0.5 rounded-full text-[11px] bg-red-100 text-red-700" title="Nothing answered at this host from the API container. See the note below the table before treating this as broken.">no answer</span>'
                    : '<span class="px-2 py-0.5 rounded-full text-[11px] bg-gray-100 text-gray-500" title="Not checked.">unchecked</span>'}</td>
                <td class="py-2 pr-4">${(() => {
                    const label = e.configured ? 'set here' : 'using default';
                    const cls = e.configured
                        ? 'bg-green-100 text-green-700 hover:bg-green-200'
                        : 'bg-amber-100 text-amber-800 hover:bg-amber-200';
                    // Clickable ONLY where the click can actually change the
                    // value. Two of these are environment variables that need
                    // a file edit and a restart; sending someone to a Secrets
                    // page with no field for them is worse than no link.
                    if (e.settable_in === 'secrets') {
                        return `<a href="#secrets" class="px-2 py-0.5 rounded-full text-[11px] ${cls} cursor-pointer underline decoration-dotted"
                                   title="Change this in Secrets (${esc(e.secret_name || '')})">${label} — edit</a>`;
                    }
                    return `<span class="px-2 py-0.5 rounded-full text-[11px] ${cls.replace(/ hover:[^ ]+/,'')}"
                                  title="Environment variable ${esc(e.env_var)} — set in .env.public and restart. Not editable from the UI.">${label}</span>`;
                })()}</td>
                <td class="py-2 text-[11px] text-gray-500">${esc(e.note)}</td>
            </tr>`).join('');
        // Stated plainly rather than as a warning icon: on a deployment sold as
        // "runs on your hardware", still pulling from MOD is a fact the operator
        // needs in words they would repeat to a customer.
        const verdict = d.depends_on_mod
            ? `<div class="mt-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded p-3">
                 <strong>This deployment still pulls images from MOD.</strong>
                 That is fine if you intend it. To run independently, point the
                 registries above at your own mirror — your tenants' own built
                 images already live on your infrastructure.</div>`
            : `<div class="mt-3 text-xs text-white bg-primary border border-primary rounded p-3">
                 <strong>No images come from MOD.</strong> Every registry above
                 resolves to infrastructure you control.</div>`;
        body.innerHTML = `<table class="w-full text-sm"><thead><tr class="text-left text-[11px] uppercase tracking-wide text-gray-400">
            <th class="pb-2 pr-4">Role</th><th class="pb-2 pr-4">Host</th><th class="pb-2 pr-4">Set by</th>
            <th class="pb-2 pr-4">Answering</th><th class="pb-2 pr-4">Source</th>
            <th class="pb-2">What it holds</th></tr></thead>
            <tbody>${rows}</tbody></table>${verdict}${
            (d.entries || []).filter((e) => e.also_read_from).map((e) => `
                <div class="mt-2 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded p-3">
                    <strong>${esc(e.role)} has two sources.</strong> It is also read from
                    ${esc(e.also_read_from)}.</div>`).join('')}
            <div class="mt-2 text-[11px] text-gray-400">Deployment mode: ${esc(d.deployment_mode)}</div>
            <div class="mt-1 text-[11px] text-gray-400">Probed from the API. Images are pulled by
            <em>workers</em>, whose network view can differ — a host that answers here may not answer
            there, and the reverse. Treat "no answer" as a lead, not a verdict.</div>`;
    } catch (e) {
        body.innerHTML = `<span class="text-destructive">Could not load registries: ${e.message || e}</span>`;
    }
}

async function loadSystemHealth() {
    console.log('Loading System Health...');
    loadRegistriesCard().catch(() => {});

    // platform-capacity-overview is platform-admin only. Hide the
    // widget container and skip both the one-shot load + the 5-second
    // poll for non-admins, otherwise this hits /platform/workers/capacity
    // every tick and 403s.
    const isPlatformAdmin = !!currentUser?.is_platform_admin;
    const capacityEl = document.getElementById('platform-capacity-overview');
    if (capacityEl && !isPlatformAdmin) {
        capacityEl.classList.add('hidden');
    }

    // Platform-wide worker capacity (the 3 worker/task cards + Worker Status
    // list) comes from /platform/workers/capacity, which is platform-admin
    // only (403 otherwise). The System page is visible to tenant owners /
    // folder-owners too, so for non-admins skip the call AND hide those
    // elements — they should never see platform worker internals. System
    // Status + Recent Workflow Runs remain (both tenant-safe).
    const _workerOnlyIds = ['card-active-workers', 'card-running-tasks',
                            'card-failed-tasks', 'worker-status-section'];
    if (!isPlatformAdmin) {
        _workerOnlyIds.forEach(id => document.getElementById(id)?.classList.add('hidden'));
    }

    const tasks = [
        loadSystemStatus(),
        loadRecentWorkflowRuns(),
    ];
    if (isPlatformAdmin) {
        tasks.push(loadWorkerStatus());
        tasks.push(loadPlatformCapacity());
    }
    await Promise.all(tasks);
    if (isPlatformAdmin) _startCapacityPolling();
}

async function loadSystemStatus() {
    try {
        // Check API health (DB-backed)
        const statusEl = document.getElementById('system-status');
        const resp = await fetch(`${window.APP_CONFIG?.API_URL || 'http://api.localhost'}/platform/features`);
        if (resp.ok) {
            statusEl.textContent = 'Healthy';
            statusEl.classList.add('text-green-600');
        } else {
            statusEl.textContent = 'Degraded';
            statusEl.classList.add('text-orange-600');
        }
    } catch (error) {
        console.error('Error loading system status:', error);
        document.getElementById('system-status').textContent = 'Unknown';
    }
}

async function loadWorkerStatus() {
    try {
        const data = await apiCall('/workers/capacity');

        // Update stats with null checks
        const runningTasksEl = document.getElementById('running-tasks');
        const activeWorkersEl = document.getElementById('active-workers');
        const failedTasks24hEl = document.getElementById('failed-tasks-24h');

        if (runningTasksEl) runningTasksEl.textContent = data.running_tasks || 0;
        // Show "Active / Pool" format for workers (e.g., "3 / 5")
        if (activeWorkersEl) {
            const active = data.active_workers || 0;
            const pool = data.total_pool || 0;
            activeWorkersEl.textContent = pool > 0 ? `${active} / ${pool}` : active;
        }
        if (failedTasks24hEl) failedTasks24hEl.textContent = data.failed_tasks_24h || 0;

        // Update worker details list
        const workerList = document.getElementById('workers-list');
        if (!workerList) {
            console.warn('workers-list element not found');
            return;
        }

        const workers = data.workers || [];

        if (workers.length > 0) {
            // Show worker details with queues
            workerList.innerHTML = workers.map(worker => {
                const queues = worker.queues || ['default'];
                const queueBadges = queues.map(q => {
                    // Color code queues
                    const queueColors = {
                        'default': 'bg-gray-100 text-gray-700',
                        'cpu-small': 'bg-primary/20 text-primary',
                        'cpu-medium': 'bg-primary/20 text-primary',
                        'cpu-large': 'bg-purple-100 text-purple-700',
                        'gpu-small': 'bg-green-100 text-green-700',
                        'gpu-medium': 'bg-emerald-100 text-emerald-700',
                        'gpu-large': 'bg-teal-100 text-teal-700',
                        'ai': 'bg-orange-100 text-orange-700'
                    };
                    const colorClass = queueColors[q] || 'bg-gray-100 text-gray-700';
                    return `<span class="px-2 py-0.5 text-xs font-medium rounded ${colorClass}">${escapeHtml(q)}</span>`;
                }).join(' ');

                return `
                    <div class="flex items-center justify-between py-3 border-b border-gray-100">
                        <div class="flex-1">
                            <p class="text-sm font-medium text-gray-900">${escapeHtml(worker.name)}</p>
                            <div class="mt-1 flex flex-wrap gap-1">
                                ${queueBadges}
                            </div>
                        </div>
                        <span class="px-2 py-1 text-xs font-medium rounded-full bg-green-100 text-green-800">
                            Active
                        </span>
                    </div>
                `;
            }).join('');
        } else {
            workerList.innerHTML = '<p class="text-sm text-gray-500 py-4 text-center">No workers detected</p>';
        }

    } catch (error) {
        console.error('Error loading worker status:', error);
        const runningTasksEl = document.getElementById('running-tasks');
        const activeWorkersEl = document.getElementById('active-workers');
        const failedTasks24hEl = document.getElementById('failed-tasks-24h');

        if (runningTasksEl) runningTasksEl.textContent = '0';
        if (activeWorkersEl) activeWorkersEl.textContent = '0';
        if (failedTasks24hEl) failedTasks24hEl.textContent = '0';
    }
}

async function loadPlatformCapacity() {
    // Only load capacity for platform admins
    if (!isSuperAdmin || !currentUser || currentUser.tenant_id !== 1) {
        return;
    }

    try {
        const capacity = await apiCall('/workers/capacity');

        // Update platform capacity overview section
        const capacityEl = document.getElementById('platform-capacity-overview');
        if (!capacityEl) return;

        const totalCap = capacity.total_capacity;
        const licensedCap = capacity.licensed_capacity;
        const utilization = capacity.utilization || {};
        const queueHealth = capacity.queue_health || [];
        const performance = capacity.performance_metrics || {};

        if (!document.getElementById('cap-cpu-bar')) {
        // Per-backend GPU tiles. The backend response groups devices by
        // `kind` (cuda / rocm / intel / npu), so one Platform Capacity
        // Overview box is drawn per backend that actually has devices.
        // An empty gpu_by_backend means no GPU-capable workers are
        // checked in yet — we render a single neutral "No GPUs detected"
        // placeholder instead of a misleading zeroed-out "GPUs: 0 / 0"
        // tile.
        const byBackend = capacity.gpu_by_backend || {};
        const backendKinds = Object.keys(byBackend);
        let gpuTilesHtml;
        if (backendKinds.length === 0) {
            gpuTilesHtml = `
                <div class="bg-gradient-to-br from-slate-50 to-gray-50 rounded-lg p-4 border border-slate-200">
                    <div class="text-sm text-slate-700 font-medium mb-1">GPUs</div>
                    <div class="text-2xl font-bold text-slate-900">None detected</div>
                    <div class="text-xs text-slate-600 mt-1">Connect a worker with a CUDA, ROCm, Intel, or NPU device</div>
                </div>
            `;
        } else {
            gpuTilesHtml = backendKinds.map(kind => {
                const b = byBackend[kind];
                const p = _presentationFor(kind);
                return `
                <div class="bg-gradient-to-br ${p.gradient} rounded-lg p-4 border ${p.border}" data-gpu-backend="${escapeHtml(kind)}">
                    <div class="text-sm ${p.title} font-medium mb-1">${escapeHtml(p.label)}</div>
                    <div class="text-2xl font-bold ${p.value}">${b.licensed_count} / ${b.total_count}</div>
                    <div class="text-xs ${p.sub} mt-1">Registered / Total (devices)</div>
                    <div class="mt-3">
                        <div class="flex items-center justify-between text-xs ${p.title} mb-1">
                            <span>VRAM</span>
                            <span class="font-medium">${b.licensed_vram_gb} / ${b.total_vram_gb} GB</span>
                        </div>
                        <div class="w-full ${p.barBg} rounded-full h-2">
                            <div class="${p.bar} h-2 rounded-full transition-all duration-700" style="width: ${b.total_vram_gb > 0 ? Math.min(100, (b.licensed_vram_gb / b.total_vram_gb) * 100) : 0}%"></div>
                        </div>
                    </div>
                </div>
                `;
            }).join('');
        }

        // CPU + Memory stay in a two-across grid at the top; per-backend
        // GPU tiles flow below so adding more backends (e.g. a machine
        // with NVIDIA + Intel) doesn't squish the CPU/Memory tiles.
        capacityEl.innerHTML = `
            <div class="bg-white rounded-lg shadow-md p-6 mb-6">
                <h2 class="text-lg font-semibold text-gray-800 mb-4">Platform Capacity Overview</h2>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
                    <!-- CPU -->
                    <div class="bg-gradient-to-br from-blue-50 to-indigo-50 rounded-lg p-4 border border-primary">
                        <div class="text-sm text-primary font-medium mb-1">CPU Cores</div>
                        <div class="text-2xl font-bold text-primary" id="cap-cpu-total">${licensedCap.cpu_cores} / ${totalCap.cpu_cores}</div>
                        <div class="text-xs text-primary mt-1">Registered / Total</div>
                        <div class="mt-3">
                            <div class="flex items-center justify-between text-xs text-primary mb-1">
                                <span>Current Usage</span>
                                <span class="font-medium" id="cap-cpu-label">—</span>
                            </div>
                            <div class="w-full bg-primary/20 rounded-full h-2">
                                <div class="bg-primary h-2 rounded-full transition-all duration-700" id="cap-cpu-bar" style="width: 0%"></div>
                            </div>
                        </div>
                    </div>
                    <!-- Memory -->
                    <div class="bg-gradient-to-br from-green-50 to-emerald-50 rounded-lg p-4 border border-green-200">
                        <div class="text-sm text-green-700 font-medium mb-1">Memory</div>
                        <div class="text-2xl font-bold text-green-900" id="cap-mem-total">${licensedCap.memory_gb} / ${totalCap.memory_gb} GB</div>
                        <div class="text-xs text-green-600 mt-1">Registered / Total</div>
                        <div class="mt-3">
                            <div class="flex items-center justify-between text-xs text-green-700 mb-1">
                                <span>Current Usage</span>
                                <span class="font-medium" id="cap-mem-label">—</span>
                            </div>
                            <div class="w-full bg-green-100 rounded-full h-2">
                                <div class="bg-green-600 h-2 rounded-full transition-all duration-700" id="cap-mem-bar" style="width: 0%"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Accelerators by Backend</div>
                <div id="cap-gpu-backend-grid" class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
                    ${gpuTilesHtml}
                </div>

                <!-- Queue Health -->
                ${queueHealth.length > 0 ? `
                <div class="mt-4">
                    <h3 class="text-md font-semibold text-gray-700 mb-3">Queue Health</h3>
                    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                        ${queueHealth.map(queue => `
                            <div class="bg-gray-50 rounded-lg p-3 border border-gray-200">
                                <div class="flex items-center justify-between mb-2">
                                    <span class="text-sm font-medium text-gray-900">${escapeHtml(queue.queue_name)}</span>
                                    <span class="px-2 py-0.5 text-xs font-medium rounded ${queue.licensed_workers > 0 ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}">
                                        ${queue.licensed_workers > 0 ? 'Active' : 'No Workers'}
                                    </span>
                                </div>
                                <div class="text-xs text-gray-600">
                                    <div><span data-theme-key="workers">Workers</span>: ${queue.licensed_workers} licensed / ${queue.workers_watching} total</div>
                                    <div>Waiting: ${queue.tasks_waiting} tasks</div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>
                ` : ''}

                <!-- Performance Metrics -->
                ${performance.tasks_analyzed > 0 ? `
                <div class="mt-6">
                    <h3 class="text-md font-semibold text-gray-700 mb-3">Performance Metrics (Last 100 Tasks)</h3>
                    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <!-- Average Completion Time -->
                        <div class="bg-primary/10 rounded-lg p-4 border border-primary">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-sm text-primary font-medium">Avg Completion Time</span>
                                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-primary">
                                    <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
                                </svg>
                            </div>
                            <div class="text-2xl font-bold text-primary">${formatDuration(performance.avg_completion_time_seconds)}</div>
                            <div class="text-xs text-primary mt-1">Time from start to completion</div>
                        </div>

                        <!-- Average Wait Time -->
                        <div class="bg-amber-50 rounded-lg p-4 border border-amber-200">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-sm text-amber-700 font-medium">Avg Queue Wait Time</span>
                                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-amber-600">
                                    <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
                                </svg>
                            </div>
                            <div class="text-2xl font-bold text-amber-900">${formatDuration(performance.avg_wait_time_seconds)}</div>
                            <div class="text-xs text-amber-600 mt-1">Time in queue before execution</div>
                        </div>

                        <!-- Success Rate -->
                        <div class="bg-green-50 rounded-lg p-4 border border-green-200">
                            <div class="flex items-center justify-between mb-2">
                                <span class="text-sm text-green-700 font-medium">Success Rate</span>
                                <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-green-600">
                                    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
                                </svg>
                            </div>
                            <div class="text-2xl font-bold text-green-900">${performance.success_rate_percent}%</div>
                            <div class="text-xs text-green-600 mt-1">${performance.successful_tasks} success / ${performance.failed_tasks} failed</div>
                        </div>
                    </div>
                </div>
                ` : ''}

                <!-- Platform admin tools: adapter cache flush per tenant.
                     Lets support clear a single tenant's cached wizard
                     adapter outputs when debugging a bad LLM result. -->
                <div class="mt-6 pt-6 border-t border-gray-200">
                    <h3 class="text-md font-semibold text-gray-700 mb-3">Adapter cache</h3>
                    <div class="flex items-end gap-3">
                        <div class="flex-1 max-w-sm">
                            <label for="adapter-cache-flush-tenant"
                                   class="block text-xs text-gray-600 mb-1">
                                Tenant ID to flush
                            </label>
                            <input id="adapter-cache-flush-tenant" type="number" min="1"
                                   class="w-full px-3 py-2 border border-gray-300 rounded text-sm"
                                   placeholder="e.g. 1"/>
                        </div>
                        <button id="adapter-cache-flush-btn"
                                class="px-4 py-2 bg-amber-600 text-white rounded hover:bg-amber-700 text-sm">
                            Flush cache
                        </button>
                    </div>
                    <p id="adapter-cache-flush-result" class="text-xs mt-2 text-gray-500"></p>
                </div>
            </div>
        `;
        // OZ-C2-P3: re-run the label hook after async render
        document.dispatchEvent(new CustomEvent('mod:page-rendered'));
        }
        _wireAdapterCacheFlushButton();
        _updateCapacityBars(capacity);

    } catch (error) {
        console.error('Error loading platform capacity:', error);
        // Don't show error for non-platform admins
        if (currentUser && currentUser.tenant_id === 1) {
            const capacityEl = document.getElementById('platform-capacity-overview');
            if (capacityEl) {
                capacityEl.innerHTML = '<p class="text-sm text-gray-500">Platform capacity information unavailable</p>';
            }
        }
    }
}

async function loadRecentWorkflowRuns() {
    try {
        const tbody = document.getElementById('workflow-runs-tbody');
        if (!tbody) {
            console.warn('workflow-runs-tbody element not found - page may not be loaded yet');
            return;
        }

        // Check if user is platform admin to show all workflow runs
        const user = await me.get();
        const isPlatformAdmin = user.is_platform_admin;

        tbody.innerHTML = '';

        if (isPlatformAdmin) {
            // Platform admins see all workflow runs (DB-backed)
            const runs = await apiCall('/road/runs?limit=10&offset=0');

            if (runs && runs.length > 0) {
                runs.forEach(run => {
                    const row = document.createElement('tr');
                    row.className = 'hover:bg-gray-50';

                    const stateColors = {
                        'completed': 'bg-green-100 text-green-800',
                        'success': 'bg-green-100 text-green-800',
                        'running': 'bg-primary/20 text-primary',
                        'failed': 'bg-red-100 text-red-900',
                        'queued': 'bg-yellow-100 text-yellow-800'
                    };

                    const state = run.status || 'unknown';
                    const stateColor = stateColors[state] || 'bg-gray-100 text-gray-800';

                    const showStopButton = state === 'running' || state === 'queued';
                    const stopButtonHtml = showStopButton ? `
                        <button
                            onclick="window.analytics.cancelWorkflowRun(${run.id})"
                            class="text-sm text-red-600 hover:text-red-800 font-medium mr-3"
                        >
                            Cancel
                        </button>
                    ` : '';

                    const displayName = run.run_name || run.workflow_name || `Run #${run.id}`;

                    row.innerHTML = `
                        <td class="py-3 px-4 text-sm text-gray-900">${escapeHtml(displayName)} ${dataLabelChip(run.data_classification, { subtleDefault: false })}</td>
                        <td class="py-3 px-4 text-sm text-gray-600">${formatTimestamp(run.started_at)}</td>
                        <td class="py-3 px-4">
                            <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">
                                ${state}
                            </span>
                        </td>
                        <td class="py-3 px-4 text-sm text-gray-600">Run #${run.id}</td>
                        <td class="py-3 px-4">
                            ${stopButtonHtml}
                        </td>
                    `;

                    tbody.appendChild(row);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="4" class="py-8 text-center text-gray-500">No recent workflow runs</td></tr>';
            }
        } else {
            // Regular users see only their workflow runs from database
            const runs = await apiCall('/road/runs?limit=10');

            if (runs && runs.length > 0) {
                runs.forEach(run => {
                    const row = document.createElement('tr');
                    row.className = 'hover:bg-gray-50';

                    const stateColors = {
                        'completed': 'bg-green-100 text-green-800',
                        'running': 'bg-primary/20 text-primary',
                        'failed': 'bg-red-100 text-red-900',
                        'queued': 'bg-yellow-100 text-yellow-800'
                    };

                    const stateColor = stateColors[run.status] || 'bg-gray-100 text-gray-800';

                    const showStopButton = run.status === 'running' || run.status === 'queued';
                    const stopButtonHtml = showStopButton ? `
                        <button
                            onclick="window.analytics.cancelWorkflowRun(${run.id})"
                            class="text-sm text-red-600 hover:text-red-800 font-medium mr-3"
                        >
                            Cancel
                        </button>
                    ` : '';

                    const displayName = run.run_name ||
                        (run.workflow_name ? `${run.workflow_name}${run.version_number ? ` v${run.version_number}` : ''}` : `Run #${run.id}`);

                    const fileCount = run.run_files ? run.run_files.length : 0;

                    row.innerHTML = `
                        <td class="py-3 px-4 text-sm text-gray-900">${escapeHtml(displayName)} ${dataLabelChip(run.data_classification, { subtleDefault: false })}</td>
                        <td class="py-3 px-4 text-sm text-gray-600">${formatTimestamp(run.started_at)}</td>
                        <td class="py-3 px-4">
                            <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">
                                ${run.status}
                            </span>
                        </td>
                        <td class="py-3 px-4 text-sm text-gray-600">${fileCount} file${fileCount !== 1 ? 's' : ''}</td>
                        <td class="py-3 px-4">
                            ${stopButtonHtml}
                            <button
                                onclick="window.analytics.viewWorkflowRunProgress(${run.id})"
                                class="text-sm text-primary hover:text-primary/80 font-medium"
                            >
                                View Progress →
                            </button>
                        </td>
                    `;

                    tbody.appendChild(row);
                });
            } else {
                tbody.innerHTML = '<tr><td colspan="4" class="py-8 text-center text-gray-500">No recent workflow runs</td></tr>';
            }
        }

    } catch (error) {
        console.error('Error loading workflow runs:', error);
        const tbody = document.getElementById('workflow-runs-tbody');
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="4" class="py-8 text-center text-red-500">Error loading workflow runs</td></tr>';
        }
    }
}

// Task logs functionality
let currentTaskLogs = null;

async function viewTaskLogs(nodeKind, dagRunId) {
    try {
        const taskInstances = await apiCall(`/road/runs?limit=1`);

        if (!taskInstances || taskInstances.length === 0) {
            showInfo('No tasks found for this workflow run');
            return;
        }

        // Get logs for the first task (or the first failed task if any)
        const failedTask = taskInstances.find(t => t.state === 'failed');
        const taskToShow = failedTask || taskInstances[0];

        await showTaskLogs(nodeKind, dagRunId, taskToShow.task_id, taskToShow.try_number || 1);

    } catch (error) {
        console.error('Error viewing task logs:', error);
        showError('Failed to load task logs: ' + error.message);
    }
}

async function showTaskLogs(nodeKind, dagRunId, taskId, tryNumber) {
    try {
        const data = await apiCall(`/workers/tasks/${taskId}/container-logs`);
        const logs = data.logs || 'No logs available';

        // Store current logs for download
        currentTaskLogs = {
            nodeKind,
            dagRunId,
            taskId,
            tryNumber,
            content: logs
        };

        // Update modal content
        document.getElementById('log-dag-id').textContent = nodeKind;
        document.getElementById('log-task-id').textContent = taskId;
        document.getElementById('log-run-id').textContent = dagRunId;
        document.getElementById('log-try-number').textContent = tryNumber;
        document.getElementById('task-log-content').textContent = logs;

        // Initialize AI chat for this workflow
        await initAIChat(nodeKind, dagRunId, 'unknown');

        // Show modal
        document.getElementById('task-logs-modal').classList.remove('hidden');

    } catch (error) {
        console.error('Error showing task logs:', error);
        showError('Failed to show task logs: ' + error.message);
    }
}

function downloadTaskLogs() {
    if (!currentTaskLogs) {
        showInfo('No logs to download');
        return;
    }

    const filename = `${currentTaskLogs.nodeKind}_${currentTaskLogs.taskId}_${currentTaskLogs.tryNumber}.log`;
    const blob = new Blob([currentTaskLogs.content], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);

    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

/**
 * Stop a workflow using workflow run ID (legacy)
 * @deprecated Use cancelWorkflowRun(runId) instead which uses our workflow runs endpoint
 */
async function stopWorkflow(nodeKind, dagRunId) {
    if (!await showConfirm(`Are you sure you want to stop workflow "${nodeKind}"?\n\nThis will mark the workflow as failed and stop all running tasks.`)) {
        return;
    }

    try {
        const response = await apiCall(`/road/workflow-runs/${dagRunId}/cancel`, {
            method: 'PATCH'
        });

        if (response.status === 'stopped') {
            showInfo('Workflow stopped successfully');
            // Reload the workflow runs table
            loadRecentWorkflowRuns();
        }
    } catch (error) {
        console.error('Error stopping workflow:', error);
        showError('Failed to stop workflow: ' + error.message);
    }
}

async function cancelWorkflowRun(runId) {
    if (!await showConfirm('Are you sure you want to cancel this workflow run?\n\nThis will stop the workflow and mark it as failed.')) {
        return;
    }

    try {
        const response = await apiCall(`/road/workflow-runs/${runId}/cancel`, {
            method: 'POST'
        });

        showInfo('Workflow cancelled successfully');
        // Reload the workflow runs table
        loadRecentWorkflowRuns();
    } catch (error) {
        console.error('Error cancelling workflow:', error);
        showError('Failed to cancel workflow: ' + error.message);
    }
}

async function viewWorkflowRunProgress(runId) {
    try {
        const run = await apiCall(`/road/workflow-runs/${runId}`);

        // Check if we have a task logs modal to show the info
        const modal = document.getElementById('task-logs-modal');
        if (!modal) {
            // Fallback to alert if modal not available
            showInfo(`Workflow Run #${runId}\n\nStatus: ${run.status}\nStarted: ${formatTimestamp(run.started_at)}\nCompleted: ${formatTimestamp(run.completed_at)}\nFiles: ${run.run_files ? run.run_files.length : 0}`);
            return;
        }

        // Build a custom view in the logs modal
        const stateColors = {
            'completed': 'bg-green-100 text-green-800',
            'running': 'bg-primary/20 text-primary',
            'failed': 'bg-red-100 text-red-900',
            'queued': 'bg-yellow-100 text-yellow-800'
        };
        const stateColor = stateColors[run.status] || 'bg-gray-100 text-gray-800';

        // Get display name
        const displayName = run.run_name ||
            (run.workflow_name ? `${run.workflow_name}${run.version_number ? ` v${run.version_number}` : ''}` : `Run #${run.id}`);

        // Calculate duration
        let duration = '--';
        if (run.started_at && run.completed_at) {
            const start = new Date(run.started_at);
            const end = new Date(run.completed_at);
            const diffMs = end - start;
            const diffMins = Math.floor(diffMs / 60000);
            const diffSecs = Math.floor((diffMs % 60000) / 1000);
            duration = `${diffMins}m ${diffSecs}s`;
        } else if (run.started_at && run.status === 'running') {
            const start = new Date(run.started_at);
            const now = new Date();
            const diffMs = now - start;
            const diffMins = Math.floor(diffMs / 60000);
            const diffSecs = Math.floor((diffMs % 60000) / 1000);
            duration = `${diffMins}m ${diffSecs}s (running)`;
        }

        // Build HTML content
        let html = `
            <div class="mb-4 p-4 bg-gray-50 rounded-lg">
                <h4 class="font-semibold text-gray-900 mb-3">${escapeHtml(displayName)}</h4>
                <div class="grid grid-cols-2 gap-3 text-sm">
                    <div><span class="font-medium">Run ID:</span> ${run.id}</div>
                    <div><span class="font-medium">Status:</span>
                        <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">${run.status}</span>
                    </div>
                    <div><span class="font-medium">Started:</span> ${formatTimestamp(run.started_at)}</div>
                    <div><span class="font-medium">Duration:</span> ${duration}</div>
                </div>
            </div>
        `;

        // Failure reason — surfaces the orchestrator-level error (pending
        // security scan, missing secret, circular dependency, etc.) when
        // a run failed before any node started executing. Without this
        // panel the user just sees status=failed with no clue.
        if (run.error_message && String(run.status).toLowerCase() === 'failed') {
            html += `
                <div class="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg">
                    <h5 class="font-semibold text-red-800 mb-1 flex items-center gap-1">
                        <span>⚠</span> Failure reason
                    </h5>
                    <div class="text-sm text-red-900 font-mono whitespace-pre-wrap break-words">${escapeHtml(run.error_message)}</div>
                </div>
            `;
        }

        // Files section
        if (run.run_files && run.run_files.length > 0) {
            html += `
                <div class="mb-4 p-4 bg-primary/10 rounded-lg">
                    <h5 class="font-semibold text-gray-900 mb-2">Files (${run.run_files.length})</h5>
                    <div class="space-y-1 max-h-32 overflow-y-auto">
                        ${run.run_files.map(file => `
                            <div class="flex justify-between items-center text-sm">
                                <span class="text-gray-700">${escapeHtml(file.title || file.cog_id)}</span>
                                <span class="px-2 py-0.5 text-xs rounded ${file.status === 'completed' ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-600'}">${file.status || 'pending'}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }

        // Node runs section
        if (run.node_runs && run.node_runs.length > 0) {
            html += `
                <div class="mb-4">
                    <h5 class="font-semibold text-gray-900 mb-2">Node Execution</h5>
                    <div class="overflow-x-auto max-h-48 overflow-y-auto">
                        <table class="min-w-full text-sm">
                            <thead class="bg-gray-100 sticky top-0">
                                <tr>
                                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500">Node</th>
                                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500">Status</th>
                                    <th class="px-3 py-2 text-left text-xs font-medium text-gray-500">Started</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y">
                                ${run.node_runs.map(node => {
                                    const nodeColor = stateColors[node.status] || 'bg-gray-100 text-gray-800';
                                    return `
                                        <tr>
                                            <td class="px-3 py-2 text-gray-800">${escapeHtml(node.node_id || `Node ${node.workflow_node_id}`)}</td>
                                            <td class="px-3 py-2"><span class="px-2 py-0.5 text-xs rounded ${nodeColor}">${node.status}</span></td>
                                            <td class="px-3 py-2 text-gray-600 text-xs">${formatTimestamp(node.started_at)}</td>
                                        </tr>
                                    `;
                                }).join('')}
                            </tbody>
                        </table>
                    </div>
                </div>
            `;
        }

        // Update modal header
        document.getElementById('log-dag-id').textContent = displayName;
        document.getElementById('log-task-id').textContent = `Run #${run.id}`;
        document.getElementById('log-run-id').textContent = run.status;
        document.getElementById('log-try-number').textContent = duration;

        // Replace logs content with our custom view
        document.getElementById('task-log-content').innerHTML = html;

        // Show modal
        modal.classList.remove('hidden');

    } catch (error) {
        console.error('Error loading workflow progress:', error);
        if (error.status === 404) {
            showInfo('No progress data available yet');
        } else {
            showError('Failed to load progress: ' + error.message);
        }
    }
}

/**
 * View workflow task instances for a DAG run
 * Fetches workflow task details and displays in modal
 */
async function viewWorkflowTasks(nodeKind, dagRunId) {
    try {
        const tasks = await apiCall(`/road/runs?limit=1`);

        const modal = document.getElementById('task-logs-modal');
        if (!modal) {
            showInfo('Task logs modal not available');
            return;
        }

        // Build task list view
        let html = `
            <div class="mb-4 p-4 bg-primary/10 rounded-lg">
                <h4 class="font-semibold text-primary mb-2">Workflow Task Details</h4>
                <p class="text-sm text-primary">DAG Run: ${escapeHtml(dagRunId)}</p>
            </div>
        `;

        if (tasks && tasks.length > 0) {
            const stateColors = {
                'success': 'bg-green-100 text-green-800',
                'running': 'bg-primary/20 text-primary',
                'failed': 'bg-red-100 text-red-900',
                'queued': 'bg-yellow-100 text-yellow-800',
                'upstream_failed': 'bg-orange-100 text-orange-800'
            };

            html += `
                <div class="space-y-2">
                    ${tasks.map(task => {
                        const stateColor = stateColors[task.state] || 'bg-gray-100 text-gray-800';
                        return `
                            <div class="flex items-center justify-between p-3 border border-gray-200 rounded-lg hover:bg-gray-50">
                                <div class="flex-1">
                                    <p class="font-medium text-gray-900">${escapeHtml(task.task_id)}</p>
                                    <p class="text-xs text-gray-500">Try ${task.try_number || 1}</p>
                                </div>
                                <div class="flex items-center gap-3">
                                    <span class="px-2 py-1 text-xs font-medium rounded-full ${stateColor}">
                                        ${task.state}
                                    </span>
                                    <button
                                        onclick="window.analytics.viewTaskLogsDetail('${nodeKind}', '${dagRunId}', '${task.task_id}', ${task.try_number || 1})"
                                        class="text-sm text-primary hover:text-primary/80"
                                    >
                                        View Logs
                                    </button>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        } else {
            html += '<p class="text-center text-gray-500 py-4">No task instances found</p>';
        }

        // Update modal
        document.getElementById('log-dag-id').textContent = nodeKind;
        document.getElementById('log-task-id').textContent = 'All Tasks';
        document.getElementById('log-run-id').textContent = dagRunId;
        document.getElementById('log-try-number').textContent = tasks ? tasks.length : 0;

        document.getElementById('task-log-content').innerHTML = html;
        modal.classList.remove('hidden');

    } catch (error) {
        console.error('Error viewing tasks:', error);
        showError('Failed to load tasks: ' + error.message);
    }
}

/**
 * View detailed logs for a specific Workflow task
 */
async function viewTaskLogsDetail(nodeKind, dagRunId, taskId, tryNumber) {
    try {
        const data = await apiCall(`/workers/tasks/${taskId}/container-logs`);
        const logs = data.logs || 'No logs available';

        // Store for download
        currentTaskLogs = {
            nodeKind,
            dagRunId,
            taskId,
            tryNumber,
            content: logs
        };

        // Update modal with actual logs
        document.getElementById('log-dag-id').textContent = nodeKind;
        document.getElementById('log-task-id').textContent = taskId;
        document.getElementById('log-run-id').textContent = dagRunId;
        document.getElementById('log-try-number').textContent = tryNumber;
        document.getElementById('task-log-content').textContent = logs;

        // Initialize AI chat
        await initAIChat(nodeKind, dagRunId, 'unknown');

    } catch (error) {
        console.error('Error viewing task logs:', error);
        showError('Failed to load task logs: ' + error.message);
    }
}

// Export functions for global access
window.analytics = {
    viewTaskLogs,
    stopWorkflow,
    cancelWorkflowRun,
    viewWorkflowRunProgress,
    viewWorkflowTasks,
    viewTaskLogsDetail
};

// ========================================
// Usage Stats Tab
// ========================================

async function loadUsageStats() {
    console.log('Loading Usage Stats...');

    // render-analytics-dashboard — the usage/billing analytics dashboard is
    // rendered alongside the existing sharing/permissions overview below.
    // Independent try/catch so a failure in one never blanks the other.
    loadUsageAnalytics();

    // resource-monitor-merge — the Resource Monitor now lives inside this
    // panel (above Sharing & permissions). Its block is only unhidden for
    // platform admins (checkUserRole), and its /system/resource-monitor/*
    // endpoints (clean-v2-api admin dissolve wave: moved off /admin) are
    // platform-admin gated, so only initialize it when the block is
    // actually visible.
    const rmBlock = document.getElementById('resource-monitor-block');
    if (rmBlock && !rmBlock.classList.contains('hidden')) {
    }

    try {
        // Tenant-wide by design: the System page is gated to owners/managers,
        // and the endpoint is tenant-scoped (no cross-tenant leak). The old
        // ?owner_id= param was a no-op — the endpoint never accepted it — so
        // it's dropped rather than left implying a per-owner scope that isn't.
        const overview = await apiCall('/analytics/permissions/overview');

        // Update summary cards (match HTML element IDs)
        const totalPermEl = document.getElementById('total-permissions');
        const sharedFoldersEl = document.getElementById('total-folders');
        const externalUsersEl = document.getElementById('external-users');
        const expiring7dEl = document.getElementById('expiring-7d');

        if (totalPermEl) totalPermEl.textContent = overview.total_permissions || 0;
        if (sharedFoldersEl) sharedFoldersEl.textContent = overview.total_folders_with_permissions || 0;
        if (externalUsersEl) externalUsersEl.textContent = overview.external_user_stats?.total_external_users || 0;
        if (expiring7dEl) expiring7dEl.textContent = overview.external_user_stats?.expiring_within_7d || 0;

        // Update top folders table
        const tbody = document.getElementById('top-folders-tbody');
        if (!tbody) {
            console.warn('usage-top-folders-tbody element not found');
            return;
        }
        tbody.innerHTML = '';

        if (overview.top_shared_folders && overview.top_shared_folders.length > 0) {
            overview.top_shared_folders.forEach(folder => {
                const row = document.createElement('tr');
                row.className = 'hover:bg-gray-50';
                row.innerHTML = `
                    <td class="py-3 px-4 font-medium text-gray-800">${escapeHtml(folder.cog_name)}</td>
                    <td class="py-3 px-4 text-gray-600">${escapeHtml(folder.owner_name)}</td>
                    <td class="py-3 px-4 text-right font-semibold">${folder.total_permissions || 0}</td>
                    <td class="py-3 px-4 text-right">${folder.external_user_count || 0}</td>
                    <td class="py-3 px-4 text-right">${folder.expiring_count || 0}</td>
                `;
                tbody.appendChild(row);
            });
        } else {
            tbody.innerHTML = '<tr><td colspan="5" class="py-8 text-center text-gray-500">No shared folders found</td></tr>';
        }

    } catch (error) {
        console.error('Error loading usage stats:', error);
    }
}

// ========================================
// Usage & Billing Analytics (render-analytics-dashboard)
// ========================================
//
// Renders a role-aware analytics dashboard in the Usage Stats sub-tab:
//   • every viewer sees their tenant's usage/credits/cost/utilization
//   • platform admins additionally see platform-wide KPIs.
// Charts reuse the same Chart.js CDN loader the GPU Pool tab uses; the
// utilization bars + funnel are inline CSS so the page still reads if the
// CDN is blocked (offline installs). All endpoint reads are individually
// guarded — a 403/500 on one card degrades to a dash, never a blank tab.

const _usageCharts = {};        // canvasId -> Chart instance (destroyed on re-render)
const _CHART_PALETTE = ['#6366f1', '#22c55e', '#f59e0b', '#ef4444', '#06b6d4', '#a855f7', '#ec4899', '#84cc16'];

// (_fmtBytes is already defined above — reused here.)

function _fmtCents(cents) {
    const v = (Number(cents) || 0) / 100;
    return v.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: v >= 100 ? 0 : 2 });
}

function _fmtNum(n) {
    return (Number(n) || 0).toLocaleString();
}

function _statCard(label, value, sub) {
    return `
        <div class="bg-white p-6 rounded-lg border border-gray-200">
            <p class="text-gray-500 text-sm font-medium">${escapeHtml(label)}</p>
            <p class="text-3xl font-bold text-gray-800 mt-2">${escapeHtml(String(value))}</p>
            ${sub ? `<p class="text-xs text-gray-500 mt-1">${escapeHtml(sub)}</p>` : ''}
        </div>`;
}

// Horizontal utilization bar. `pct` is clamped 0..100 for width; the label
// keeps the true figure. Colour shifts amber/red as the resource fills.
function _utilBar(label, usedText, pct) {
    const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
    const colour = clamped >= 90 ? 'bg-red-500' : clamped >= 70 ? 'bg-amber-500' : 'bg-primary';
    return `
        <div>
            <div class="flex items-center justify-between text-sm mb-1">
                <span class="font-medium text-gray-700">${escapeHtml(label)}</span>
                <span class="text-gray-500">${escapeHtml(usedText)}</span>
            </div>
            <div class="w-full bg-gray-100 rounded-full h-2.5">
                <div class="${colour} h-2.5 rounded-full" style="width:${clamped}%"></div>
            </div>
        </div>`;
}

function _renderChart(canvasId, config) {
    if (typeof window.Chart === 'undefined') return false;
    const canvas = document.getElementById(canvasId);
    if (!canvas) return false;
    if (_usageCharts[canvasId]) {
        try { _usageCharts[canvasId].destroy(); } catch (_) { /* already gone */ }
        delete _usageCharts[canvasId];
    }
    try {
        _usageCharts[canvasId] = new window.Chart(canvas.getContext('2d'), config);
        return true;
    } catch (e) {
        console.warn('Chart render failed for', canvasId, e);
        return false;
    }
}

async function loadUsageAnalytics() {
    // Charts are best-effort; the numbers/bars render regardless.
    try { await _loadChartJsOnce(); } catch (_) { /* CDN blocked — inline bars still render */ }

    // --- Tenant-scoped analytics (visible to every viewer of this tab) ---
    // 2400UI15 — /tenant/usage/summary lives in routers/tenant/identity_tenancy.py
    // and /platform/analytics/kpis in routers/platform_analytics.py, both denied
    // wholesale by open-core/manifest.yaml, so on a CE image each 404s on every
    // visit to this tab. Same fail-open edition predicate the shell already uses
    // (api.js isPaidEdition, off the coalesced /platform/features cache) — the
    // renderers below already take null for "no data", so hiding is free.
    // FAILOPEN-E: paidEdition is now driven by api.js isPaidEdition itself, so
    // an errored stub ({edition:'ce', error:'unresolved'}) or unreachable
    // features reads as NOT paid and the paid analytics fetches stay hidden.
    let paidEdition = false;
    try {
        paidEdition = await isPaidEdition();
    } catch { /* features unreachable — fail closed: no paid UI */ }
    // 1783 — the first three are PAID surfaces (subscriptions/billing routers,
    // stripped in CE). Gate them on /platform/features.billing_enabled, the
    // same mechanism billing.js + ui.js use, so a billing-off deployment
    // never fetches them (the hung /v1/subscriptions in 1599b's run aborted
    // the whole no-paid-UI check). /tenant/usage/summary stays ungated.
    let billingOn = true;
    try {
        const features = await getPlatformFeatures();
        billingOn = features ? features.billing_enabled !== false : true;
    } catch { /* features unreachable — keep the old best-effort behaviour */ }
    const [subUsage, billing, history] = billingOn
        ? await Promise.all([
            apiCall('/subscriptions/usage').catch(() => null),
            apiCall('/billing/usage').catch(() => null),
            apiCall('/billing/usage-history?months=6').catch(() => null),
        ])
        : [null, null, null];
    const summary = paidEdition ? await apiCall('/tenant/usage/summary').catch(() => null) : null;

    _renderTenantStatCards(subUsage, summary);
    _renderPlanBadge(subUsage);
    _renderUtilization(subUsage);
    _renderBillingStrip(billing);
    _renderCreditsChart(history);

    // --- Platform metrics (platform admins only) ---
    const section = document.getElementById('platform-kpis-section');
    if (currentUser?.is_platform_admin) {
        if (section) section.classList.remove('hidden');
        // 2400UI15 — both are served by routers/platform_analytics.py, which
        // open-core/manifest.yaml denies, so on CE neither exists.
        const [kpis, overview] = paidEdition
            ? await Promise.all([
                apiCall('/platform/analytics/kpis').catch(() => null),
                apiCall('/platform/analytics/business-overview').catch(() => null),
            ])
            : [null, null];
        _renderPlatformKpis(kpis);
        _renderPlatformCharts(overview);
    } else if (section) {
        section.classList.add('hidden');
    }
}

function _renderTenantStatCards(subUsage, summary) {
    const el = document.getElementById('usage-stat-cards');
    if (!el) return;
    const credits = subUsage?.credits || {};
    const storage = subUsage?.storage || {};
    const seats = subUsage?.seats || {};
    const compute = summary?.compute || {};

    const cards = [
        _statCard('Allowance used', _fmtNum(credits.used),
            credits.total != null ? `used this period` : ''),
        _statCard('Allowance remaining', _fmtNum(credits.remaining),
            credits.reset_at ? `resets ${new Date(credits.reset_at).toLocaleDateString()}` : ''),
        _statCard('Storage used',
            storage.used_gb != null ? `${storage.used_gb} GB` : _fmtBytes(summary?.storage_used_bytes),
            storage.limit_gb != null ? `of ${storage.limit_gb} GB` : ''),
        _statCard('Workflow runs',
            _fmtNum(summary?.workflow_runs_last_30d), 'last 30 days'),
        _statCard('Active users', _fmtNum(summary?.active_users ?? seats.used),
            seats.limit != null ? `${_fmtNum(seats.used)} team members` : ''),
        _statCard('Compute hours',
            (Number(compute.total_hours) || 0).toFixed(1),
            `${(Number(compute.gpu_hours) || 0).toFixed(1)} GPU · ${(Number(compute.cpu_hours) || 0).toFixed(1)} CPU`),
        _statCard('Files stored', _fmtNum(summary?.total_files), ''),
        _statCard('Egress',
            subUsage?.egress?.used_gb != null ? `${subUsage.egress.used_gb} GB` : '0 GB', 'this period'),
    ];
    el.innerHTML = cards.join('');
}

function _renderPlanBadge(subUsage) {
    const badge = document.getElementById('usage-plan-badge');
    if (!badge) return;
    const name = subUsage?.plan?.name || subUsage?.plan_name;
    if (!name) { badge.classList.add('hidden'); return; }
    badge.textContent = `${name} plan`;
    badge.classList.remove('hidden');
}

function _renderUtilization(subUsage) {
    const el = document.getElementById('usage-utilization');
    if (!el) return;
    if (!subUsage) {
        el.innerHTML = '<p class="text-sm text-gray-400">Utilization data unavailable.</p>';
        return;
    }
    const bars = [];
    const c = subUsage.credits || {};
    if (c.total) {
        const pct = c.total ? (Number(c.used) / Number(c.total)) * 100 : 0;
        bars.push(_utilBar('Credits', `${_fmtNum(c.used)} / ${_fmtNum(c.total)}`, pct));
    }
    const s = subUsage.storage || {};
    if (s.limit_gb != null) {
        bars.push(_utilBar('Storage', `${s.used_gb ?? 0} / ${s.limit_gb} GB`, s.percentage_used));
    }
    const seats = subUsage.seats || {};
    if (seats.limit != null) {
        bars.push(_utilBar('Seats', `${_fmtNum(seats.used)} / ${_fmtNum(seats.limit)}`, seats.percentage_used));
    }
    const proc = subUsage.processes || {};
    if (proc.limit != null) {
        bars.push(_utilBar('Concurrent processes', `${_fmtNum(proc.running)} / ${_fmtNum(proc.limit)}`, proc.percentage_used));
    }
    el.innerHTML = bars.length ? bars.join('') : '<p class="text-sm text-gray-400">No metered resources in Community Edition.</p>';
}

function _renderBillingStrip(billing) {
    const el = document.getElementById('usage-billing-strip');
    if (!el) return;
    if (!billing) {
        el.innerHTML = '<p class="text-sm text-gray-400">Billing is not part of MOD Community Edition.</p>';
        return;
    }
    const items = [
        ['Period', billing.billing_period || '—'],
        ['Credits used', _fmtNum(billing.total_credits_used)],
        ['Est. cost', _fmtCents((billing.estimated_cost_usd || 0) * 100)],
    ];
    el.innerHTML = items.map(([label, value]) => `
        <div>
            <p class="text-xs uppercase tracking-wide text-gray-400">${escapeHtml(label)}</p>
            <p class="text-lg font-semibold text-gray-800 mt-1">${escapeHtml(String(value))}</p>
        </div>`).join('');
}

function _renderCreditsChart(history) {
    const rows = Array.isArray(history?.history) ? history.history : [];
    const empty = document.getElementById('usage-credits-empty');
    const hasUsage = rows.some(r => Number(r.credits_used) > 0);
    if (empty) empty.classList.toggle('hidden', hasUsage || rows.length === 0);

    if (!rows.length) return;
    const labels = rows.map(r => r.period_display || r.period);
    const credits = rows.map(r => Number(r.credits_used) || 0);
    const cost = rows.map(r => Number(r.cost_usd) || 0);

    _renderChart('usage-credits-chart', {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: 'Credits used', data: credits, yAxisID: 'y',
                    borderColor: _CHART_PALETTE[0], backgroundColor: 'rgba(99,102,241,0.12)',
                    fill: true, tension: 0.3, pointRadius: 3,
                },
                {
                    label: 'Cost (USD)', data: cost, yAxisID: 'y1',
                    borderColor: _CHART_PALETTE[2], backgroundColor: 'rgba(245,158,11,0.10)',
                    fill: false, tension: 0.3, pointRadius: 3, borderDash: [5, 4],
                },
            ],
        },
        options: {
            responsive: true, maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: { legend: { position: 'bottom' } },
            scales: {
                y: { beginAtZero: true, title: { display: true, text: 'Credits' } },
                y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'USD' } },
            },
        },
    });
}

function _renderPlatformKpis(kpis) {
    const el = document.getElementById('platform-kpi-cards');
    if (!el) return;
    if (!kpis) {
        el.innerHTML = '<p class="text-sm text-gray-400 col-span-full">Platform metrics unavailable.</p>';
        return;
    }
    el.innerHTML = [
        _statCard('Total tenants', _fmtNum(kpis.total_tenants), `${_fmtNum(kpis.active_tenants)} active`),
        _statCard('MRR', _fmtCents(kpis.mrr_cents), 'monthly recurring'),
        _statCard('Active users', _fmtNum(kpis.active_users_7d), 'last 7 days'),
        _statCard('Runs (24h)', _fmtNum(kpis.workflow_runs_24h),
            kpis.workflow_success_rate != null ? `${kpis.workflow_success_rate}% success` : ''),
        _statCard('Storage', `${_fmtNum(kpis.total_storage_used_gb)} GB`, 'platform-wide'),
        _statCard('Credits used', _fmtNum(kpis.credits_used_this_period), 'this period'),
        _statCard('Active workers', _fmtNum(kpis.active_workers), `${_fmtNum(kpis.pending_tasks)} tasks pending`),
        _statCard('Churn (30d)', `${Number(kpis.churn_rate_30d) || 0}%`, ''),
    ].join('');
}

function _renderPlatformCharts(overview) {
    if (!overview) return;

    // Revenue trend (12 mo) — line, cents -> dollars
    const mrr = Array.isArray(overview.revenue?.mrr_trend_12mo_cents) ? overview.revenue.mrr_trend_12mo_cents : [];
    if (mrr.length) {
        _renderChart('platform-mrr-chart', {
            type: 'line',
            data: {
                labels: mrr.map(p => p.label),
                datasets: [{
                    label: 'MRR (USD)', data: mrr.map(p => (Number(p.value) || 0) / 100),
                    borderColor: _CHART_PALETTE[1], backgroundColor: 'rgba(34,197,94,0.12)',
                    fill: true, tension: 0.3, pointRadius: 2,
                }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true, ticks: { callback: v => '$' + v } } },
            },
        });
    }

    // Plan mix — doughnut
    const mix = Array.isArray(overview.revenue?.plan_mix) ? overview.revenue.plan_mix : [];
    if (mix.length) {
        _renderChart('platform-planmix-chart', {
            type: 'doughnut',
            data: {
                labels: mix.map(p => p.label),
                datasets: [{ data: mix.map(p => Number(p.value) || 0), backgroundColor: _CHART_PALETTE }],
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right' } } },
        });
    }

    // Workflow runs per week — bar
    const runs = Array.isArray(overview.engagement?.workflow_runs_per_week) ? overview.engagement.workflow_runs_per_week : [];
    if (runs.length) {
        _renderChart('platform-runs-chart', {
            type: 'bar',
            data: {
                labels: runs.map(p => p.label),
                datasets: [{ label: 'Runs', data: runs.map(p => Number(p.value) || 0), backgroundColor: _CHART_PALETTE[4] }],
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true } },
            },
        });
    }

    // Funnel — inline bars (renders without a chart lib)
    const funnel = document.getElementById('platform-funnel');
    if (funnel) {
        const g = overview.growth || {};
        const stages = [
            ['Signups', Number(g.funnel_signups) || 0],
            ['Activated', Number(g.funnel_activated) || 0],
            ['First run', Number(g.funnel_first_paid) || 0],
        ];
        const max = Math.max(1, ...stages.map(s => s[1]));
        funnel.innerHTML = stages.map(([label, n], i) => {
            const pct = (n / max) * 100;
            const conv = i === 0 ? '' : `${max ? Math.round((n / stages[0][1] || 0) * 100) : 0}% of signups`;
            return `
                <div>
                    <div class="flex items-center justify-between text-sm mb-1">
                        <span class="font-medium text-gray-700">${label}</span>
                        <span class="text-gray-500">${_fmtNum(n)}${conv ? ` · ${conv}` : ''}</span>
                    </div>
                    <div class="w-full bg-gray-100 rounded-full h-3">
                        <div class="h-3 rounded-full" style="width:${pct}%;background:${_CHART_PALETTE[i]}"></div>
                    </div>
                </div>`;
        }).join('');
    }
}

// ========================================
// Workers Tab
// ========================================

async function loadWorkersPanel() {
    console.log('Loading Workers management...');

    // Defensive: the sub-tab is hidden for non-platform-admins, but
    // direct hash navigation (#analytics?tab=workers) could still
    // reach here. Bail before fetching admin endpoints.
    if (!currentUser?.is_platform_admin) return;

    try {
        // Load workers.html content
        const response = await fetch('/pages/workers.html');
        const html = await response.text();

        // Insert into workers panel
        document.getElementById('workers-content').innerHTML = html;

        // Dynamically import and initialize workers.js
        const { initializeWorkersPageListeners } = await import('./workers.js');
        await initializeWorkersPageListeners();

    } catch (error) {
        console.error('Error loading workers panel:', error);
        document.getElementById('workers-content').innerHTML = `
            <div class="bg-red-50 border border-red-200 rounded-lg p-4">
                <p class="text-red-800 font-medium">Failed to load workers management</p>
                <p class="text-red-600 text-sm mt-2">${error.message}</p>
            </div>
        `;
    }
}

// ========================================
// GPU Pool Tab (relocated from its own sidebar entry)
// ========================================

let _chartJsLoaded = false;

/**
 * Chart.js is referenced via a <script> tag inside gpu-pool.html,
 * but innerHTML insertion makes script tags inert — the chart
 * library never loads via that path. Inject the CDN script
 * explicitly the first time the GPU Pool sub-tab activates so
 * window.Chart is available before gpu-pool.js's init runs.
 * Same pattern would apply if billing.html is ever embedded the
 * same way.
 */
function _loadChartJsOnce() {
    if (_chartJsLoaded || typeof window.Chart !== 'undefined') {
        _chartJsLoaded = true;
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = '/vendor/chart.js@4.4.0/dist/chart.umd.min.js';
        s.async = true;
        s.onload = () => { _chartJsLoaded = true; resolve(); };
        s.onerror = () => reject(new Error('Failed to load Chart.js from CDN'));
        document.head.appendChild(s);
    });
}

async function loadGpuPoolPanel() {
    console.log('Loading GPU Pool management...');

    const gpuPoolContent = document.getElementById('gpu-pool-content');

    // 0b.2 s3 (ISO: the #gpu-pool alias must still land real content) — Chart.js
    // is best-effort here, exactly like loadUsageAnalytics above. The vendored
    // /vendor/chart.js@4.4.0 asset is not in the build context (the blanket
    // `vendor/` rule in CORE/Dashboard/.gitignore leaves it untracked, so nginx
    // 404s it on a fresh image), and the old code awaited it INSIDE the try
    // below: the rejection jumped to that catch and replaced the WHOLE panel
    // with a "Failed to load GPU Pool" block, so #gpu-kpi-avg / #gpu-top-tbody
    // never existed and the #gpu-pool bookmark resolved to an error page.
    // KPI cards, the top-consumers table and the /system/gpu-pool-stats fetch
    // need no chart library; gpu-pool.js's two renderers no-op without Chart.
    try { await _loadChartJsOnce(); } catch (_) { /* vendor/CDN missing — KPIs + table still render */ }

    try {
        const response = await fetch('/pages/gpu-pool.html');
        const html = await response.text();
        // SPA remount: page HTML re-injects on every visit, so the "already loaded"
        // marker lives on the (new) container element, not a module flag.
        gpuPoolContent.innerHTML = html;
        gpuPoolContent.dataset.wired = '1';

    } catch (error) {
        console.error('Error loading GPU Pool panel:', error);
        document.getElementById('gpu-pool-content').innerHTML = `
            <div class="bg-red-50 border border-red-200 rounded-lg p-4">
                <p class="text-red-800 font-medium">Failed to load GPU Pool</p>
                <p class="text-red-600 text-sm mt-2">${error.message}</p>
            </div>
        `;
    }
}

// ========================================
// Utility Functions
// ========================================

function formatTimestamp(timestamp) {
    if (!timestamp) return '-';
    const date = new Date(timestamp);
    return date.toLocaleString();
}

function formatDuration(seconds) {
    if (seconds === 0) return '0s';

    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    const parts = [];
    if (hours > 0) parts.push(`${hours}h`);
    if (minutes > 0) parts.push(`${minutes}m`);
    if (secs > 0 || parts.length === 0) parts.push(`${secs}s`);

    return parts.join(' ');
}


// =====================================================================
// wizard-rebuild §0d — System → Wizard Info panel
// =====================================================================
// Reads Prometheus text-format metrics from /metrics + non-Prometheus
// aggregations from /platform/wizard-stats. Both endpoints are
// platform-admin-only (gated server-side; the tab visibility on the
// Dashboard side is just UX — server enforces auth).

/**
 * Parse the Prometheus text-format output into a flat map keyed by
 * `<metric>{<sorted labels>}` → value. Defensive — Prometheus's
 * text format has a handful of corner cases (HELP/TYPE lines, NaN,
 * +Inf in histograms). We extract only what we need.
 */
function _parsePrometheusText(text) {
    const out = {};
    if (!text) return out;
    const lines = text.split('\n');
    for (const raw of lines) {
        const line = raw.trim();
        if (!line) continue;
        if (line.startsWith('#')) continue;
        // Sample line shape:
        //   metric{label="value",label2="value"} 42.0
        // OR:
        //   metric 42.0
        // OR with timestamp:
        //   metric{...} 42.0 1719999999999
        const match = line.match(/^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([0-9eE+\-.]+|NaN|\+Inf|-Inf)(?:\s+\d+)?$/);
        if (!match) continue;
        const [, name, labels, valueStr] = match;
        const value = parseFloat(valueStr);
        if (Number.isNaN(value) && valueStr !== 'NaN') continue;
        out[name + '{' + (labels || '') + '}'] = value;
    }
    return out;
}

/** Return a label-filtered total for a metric. Matches against
 *  the parsed map keys with a `LIKE` semantic. */
function _sumMatching(parsed, metric, labelFilter) {
    let total = 0;
    for (const key of Object.keys(parsed)) {
        if (!key.startsWith(metric + '{')) continue;
        const labelPart = key.slice(metric.length + 1, -1);
        let match = true;
        for (const [k, v] of Object.entries(labelFilter || {})) {
            if (!labelPart.includes(`${k}="${v}"`)) { match = false; break; }
        }
        if (match) total += parsed[key];
    }
    return total;
}

/** Group rows by a label value, returning {labelValue: total}. */
function _groupBy(parsed, metric, labelName) {
    const groups = {};
    for (const key of Object.keys(parsed)) {
        if (!key.startsWith(metric + '{')) continue;
        const labelPart = key.slice(metric.length + 1, -1);
        const labelMatch = new RegExp(`${labelName}="([^"]*)"`).exec(labelPart);
        if (!labelMatch) continue;
        const labelVal = labelMatch[1];
        groups[labelVal] = (groups[labelVal] || 0) + parsed[key];
    }
    return groups;
}

/** Render a sorted-by-count breakdown table inside `containerId`. */
function _renderBreakdownTable(containerId, groups, columns) {
    const el = document.getElementById(containerId);
    if (!el) return;
    const total = Object.values(groups).reduce((a, b) => a + b, 0);
    if (total === 0) {
        el.innerHTML = '<div class="text-gray-400 italic">No data yet — counter at zero.</div>';
        return;
    }
    const sorted = Object.entries(groups).sort((a, b) => b[1] - a[1]);
    const rows = sorted.map(([label, count]) => {
        const pct = ((count / total) * 100).toFixed(1);
        const labelText = String(label).replace(/</g, '&lt;');
        return `
            <tr class="border-b border-gray-100">
                <td class="py-2 pr-4 font-mono text-xs">${labelText}</td>
                <td class="py-2 pr-4 text-right">${count.toLocaleString()}</td>
                <td class="py-2 text-right text-gray-500">${pct}%</td>
            </tr>
        `;
    }).join('');
    el.innerHTML = `
        <table class="w-full">
            <thead>
                <tr class="border-b border-gray-200 text-left text-xs text-gray-500">
                    <th class="py-2 pr-4">${columns[0]}</th>
                    <th class="py-2 pr-4 text-right">${columns[1]}</th>
                    <th class="py-2 text-right">%</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

async function loadWizardInfoPanel() {
    const setLoading = (id, txt = 'Loading…') => {
        const el = document.getElementById(id);
        if (el) el.textContent = txt;
    };
    setLoading('wiz-extraction-success-pct', '—');
    setLoading('wiz-repair-total', '—');
    setLoading('wiz-infra-retry-pct', '—');
    setLoading('wiz-llm-calls-total', '—');

    let data = null;
    try {
        data = await apiCall('/platform/wizard-stats');
    } catch (err) {
        console.warn('wizard-info: /platform/wizard-stats fetch failed', err);
        const recentEl = document.getElementById('wiz-recent-failures');
        if (recentEl) {
            const status = err && err.status;
            let msg;
            if (status === 403) {
                msg = 'Restricted — only platform admins can view wizard stats.';
            } else if (status === 401) {
                msg = 'Sign in required to view wizard stats.';
            } else {
                const detail = (err && (err.detail || err.message)) || '';
                msg = `Couldn't load wizard stats${detail ? ` (${String(detail).slice(0, 100)})` : ''}.`;
            }
            recentEl.innerHTML = `<div class="text-amber-700 italic">${msg}</div>`;
        }
        return;
    }
    if (!data) return;

    // -------- Summary cards (from DB-backed counters) --------
    const extraction = data.extraction || {};
    const extractionByLabel = extraction.by_label || {};
    const totalExtractions = extraction.total || 0;
    const successExtractions = extractionByLabel.success || 0;
    const pctSuccess = totalExtractions > 0 ? (successExtractions / totalExtractions * 100) : null;
    document.getElementById('wiz-extraction-success-pct').textContent =
        pctSuccess === null ? '—' : `${pctSuccess.toFixed(1)}%`;
    document.getElementById('wiz-extraction-success-detail').textContent =
        `${successExtractions.toLocaleString()} / ${totalExtractions.toLocaleString()} extractions`;

    const repair = data.repair || {};
    const repairByOutcome = repair.by_outcome || {};
    const totalRepairs = repair.total || 0;
    const infraRetries = repair.infra_retry_count || 0;
    const pctInfraRetry = totalRepairs > 0 ? (infraRetries / totalRepairs * 100) : null;
    document.getElementById('wiz-repair-total').textContent = totalRepairs.toLocaleString();
    document.getElementById('wiz-repair-detail').textContent =
        Object.entries(repairByOutcome)
            .filter(([, v]) => v > 0)
            .map(([k, v]) => `${k}: ${v}`)
            .join(' · ') || 'no repairs yet';
    document.getElementById('wiz-infra-retry-pct').textContent =
        pctInfraRetry === null ? '—' : `${pctInfraRetry.toFixed(1)}%`;
    const tokenSavingsLine = infraRetries > 0
        ? `${infraRetries.toLocaleString()} free retry${infraRetries === 1 ? '' : 'ies'} — saved LLM tokens`
        : 'no infra retries yet';
    document.getElementById('wiz-infra-retry-detail').textContent = tokenSavingsLine;

    const llmCalls = data.llm_calls || {};
    const llmByPurpose = llmCalls.by_label || {};
    const totalLLMCalls = llmCalls.total || 0;
    document.getElementById('wiz-llm-calls-total').textContent = totalLLMCalls.toLocaleString();
    document.getElementById('wiz-llm-calls-detail').textContent =
        Object.entries(llmByPurpose)
            .filter(([, v]) => v > 0)
            .map(([k, v]) => `${k}: ${v}`)
            .join(' · ') || 'no LLM calls yet';

    // -------- Breakdown tables --------
    _renderBreakdownTable('wiz-repair-table', repairByOutcome, ['Outcome', 'Count']);
    _renderBreakdownTable('wiz-preflight-table', (data.image_preflight || {}).by_label || {}, ['Result', 'Count']);
    _renderBreakdownTable('wiz-extraction-table', (data.extraction_by_mode || {}).by_label || {}, ['Mode', 'Count']);

    const variantTests = data.variant_tests || {};
    _renderBreakdownTable('wiz-variant-table', variantTests.by_verdict || {}, ['Verdict', 'Count']);
    _renderBreakdownTable('wiz-speculative-queue', data.speculative_queue || {}, ['Accelerator', 'Pending']);

    // -------- Recent failures --------
    const recentEl = document.getElementById('wiz-recent-failures');
    if (recentEl) {
        const failures = (data.recent_failures) || [];
        if (failures.length === 0) {
            recentEl.innerHTML = '<div class="text-gray-400 italic">No failed builds in the recent window.</div>';
        } else {
            const rows = failures.map((f) => {
                const when = f.failed_at ? new Date(f.failed_at).toLocaleString() : '?';
                const reason = (f.reason || 'unknown').slice(0, 100);
                const tenant = f.tenant_id || '?';
                const wfr = f.workflow_run_id;
                return `
                    <tr class="border-b border-gray-100">
                        <td class="py-2 pr-4 text-xs text-gray-500">${when}</td>
                        <td class="py-2 pr-4 font-mono text-xs">tenant ${tenant}</td>
                        <td class="py-2 pr-4 font-mono text-xs">${f.mode || '—'}</td>
                        <td class="py-2 pr-4 text-xs">${reason.replace(/</g, '&lt;')}</td>
                        <td class="py-2 text-right">
                            ${wfr ? `<a href="#workflows?run=${wfr}" class="text-primary hover:underline text-xs">view run</a>` : ''}
                        </td>
                    </tr>
                `;
            }).join('');
            recentEl.innerHTML = `
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-gray-200 text-left text-xs text-gray-500">
                            <th class="py-2 pr-4">When</th>
                            <th class="py-2 pr-4">Tenant</th>
                            <th class="py-2 pr-4">Mode</th>
                            <th class="py-2 pr-4">Reason</th>
                            <th class="py-2 text-right"></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `;
        }
    }
}
