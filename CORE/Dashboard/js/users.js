// users.js
// Handles the unified User Management page functionality

import { users, rbac, invitations, cogs, me, getPlatformFeatures, isPaidEdition } from './api.js';
import { fetchApi as apiCall } from './api.js';
// FX.163 p3b1 — PUT /rbac/users/{id}/assign-role answers 202
// {status:'awaiting_approval', approval_id} while the second-approver switch is
// ON for the target tenant (CORE/API/routers/rbac.py:199). fetchApi does not
// throw on 2xx, so the caller must branch on the body itself.
import { isAwaitingApproval, showAwaitingApproval, clearAwaitingApproval } from './approval-pending.js';
import { showCopyableModal } from './modal.js';
import { showTeamInviteAcceptLink, copyToClipboard } from './invitations.js';
// 0b.2 redo (R37.4 part 2, owner 10-08: "tabs put within the main tab", NOT
// sidebar segments) — the six Users-group members are tabs of THIS page. Their
// existing modules run unchanged; the panel markup is fetched verbatim from
// pages/<key>.html (same mechanism ui.js uses for standalone pages).
import { initializeHeldMatchesPage } from './held-matches.js';
import { initializeStaffRegisterPage } from './staff-register.js';
import { initializeConsentRegisterPage } from './consent-register.js';
import { setActiveTab, getActiveTab } from './tab-state.js';
import { optionalPageModule, withButtonBusy } from './utils.js';

// Identity & Access (SSO + SCIM) is a Users sub-tab that embeds the 5 existing
// admin pages. Those pages share one nav bar (Identity Providers / Domains /
// Tokens / Group Mapping / Activity) whose <a href="#admin-…"> links normally
// route the whole SPA away. Here we intercept them and swap the sub-page IN
// PLACE so the user stays inside the Users tab. Each page's own HTML marks its
// active nav item, so styling comes for free on swap.
const IDENTITY_PAGES = {
};
const _identityCache = {};
let _identityDelegated = false;

async function _loadIdentitySubpage(key) {
    const host = document.getElementById('identity-embed');
    if (!host) return;
    const entry = IDENTITY_PAGES[key] || IDENTITY_PAGES['admin-sso'];
    try {
        if (!_identityCache[key]) {
            const r = await fetch(`/pages/${entry.file}`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            _identityCache[key] = await r.text();
        }
        host.innerHTML = _identityCache[key];
        // Delegate once on the persistent host: keep the shared SSO/SCIM nav
        // inside this sub-tab instead of letting it route the whole SPA away.
        if (!_identityDelegated) {
            _identityDelegated = true;
            host.addEventListener('click', (e) => {
                const a = e.target.closest('a[href^="#admin-"]');
                if (!a) return;
                e.preventDefault();
                e.stopPropagation();
                const k = a.getAttribute('href').slice(1).split('?')[0];
                _loadIdentitySubpage(k);
            });
        }
        entry.init();
    } catch (e) {
        host.innerHTML = `<p class="text-sm text-red-600">Could not load Identity &amp; Access: ${_escapeHtml(e.message)}</p>`;
    }
}

function _loadIdentityTab() {
    // Always (re)enter on the Identity Providers page.
    _loadIdentitySubpage('admin-sso');
}

// ========================================
// 0b.2 redo (R37.4 part 2) — Users group members are tabs of THIS page
// ========================================
const USERS_CHILD_KEYS = ['held-matches', 'parental-consent', 'data-requests',
    'admin-dsar-requests', 'staff-register', 'consent-register'];

// Child initializer per tab, unchanged from the standalone pages. parental-consent
// is CE-carved (open-core/manifest.yaml denies js/compliance-parental-consent.js), so it resolves through optionalPageModule —
// null on CE, so nothing runs there; the tab is hidden on CE anyway.
const _CHILD_INIT = {
    'held-matches': () => initializeHeldMatchesPage(),
    'staff-register': () => initializeStaffRegisterPage(),
    'consent-register': () => initializeConsentRegisterPage(),
    'parental-consent': async () => (await optionalPageModule(
        './compliance-parental-consent.js', 'initializeParentalConsentPage'))?.(),
};

// Role gates copied 1:1 from the per-child nav-link gates in ui.js
// updateUIVisibility ('platform' = isPlatformAdmin only, 'tenant' = the
// tenant-admin shape isSuperAdmin || isPlatformAdmin).
const _CHILD_GATE = {
    'held-matches': 'tenant',
    'parental-consent': 'platform',
    'data-requests': 'tenant',
    'admin-dsar-requests': 'platform',
    'staff-register': 'platform',
    'consent-register': 'tenant',
};

let _ceEdition = false;

// The tab this page mount should land on: an explicit initialTab from ui.js
// (old #held-matches / #data-requests / ... bookmarks) or the ?tab= param a
// refresh (F5) carries back. Resolved once the role check has run.
let _pendingUsersTab = null;

function _childAllowed(key) {
    if (_ceEdition && (key === 'parental-consent' || key === 'consent-register')) return false;
    // 2400LR-cov4b: the DSAR tracker router (routers/dsar_tracker.py) is denied in
    // open-core/manifest.yaml, so this tab would only 404 on CE.
    if (_ceEdition && key === 'admin-dsar-requests') return false;
    // bugDSARtab: the tenant DSAR router is denied in open-core/manifest.yaml
    // (:1391 `CORE/API/routers/tenant/*.py`) and main.py:2337 mounts
    // ("tenant","router","/tenant") only on the paid tier, so every /tenant/dsar/*
    // route answers 404 on CE — the tab's actions would all fail, so hide it.
    if (_ceEdition && key === 'data-requests') return false;
    // Tim B3 #13 (Users -> Staff Register) + #14 (Users -> Held Matches): default while Q-B3-STAFF-REGISTER and Q-B3-HELD-MATCHES are OPEN is to hide both on CE; delete this line to restore either.
    if (_ceEdition && (key === 'staff-register' || key === 'held-matches')) return false;
    switch (_CHILD_GATE[key]) {
        case 'platform': return isPlatformAdmin;
        case 'tenant': return isSuperAdmin || isPlatformAdmin;
        default: return false;
    }
}

let _childLoadedOnce = {};

// navigateToPage re-mounts this page's markup on every hash visit, which wipes
// the panel innerHTML, so the loaded-once map is reset per page mount.
function _resetChildLoads() {
    _childLoadedOnce = {};
}

async function _activateUsersChildTab(key) {
    // Never open a tab the role/CE gate hides (a stale deep link after a role
    // change, or a hand-clicked hidden button, falls back to nothing).
    if (!_childAllowed(key)) return;
    const tab = document.getElementById(`main-tab-${key}`);
    const panel = document.getElementById(`main-panel-${key}`);
    if (!tab || !panel) return;

    setActiveTab(key);

    document.querySelectorAll('.main-tab').forEach(t => {
        t.classList.remove('border-primary', 'text-primary');
        t.classList.add('border-transparent', 'text-gray-500');
    });
    tab.classList.remove('border-transparent', 'text-gray-500');
    tab.classList.add('border-primary', 'text-primary');

    document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
    panel.classList.remove('hidden');

    if (_childLoadedOnce[key]) return;
    _childLoadedOnce[key] = true;
    // Child markup loads verbatim from its own page file; a 404 (a CE-denied
    // file) leaves the panel empty — the tab is hidden on CE anyway.
    try {
        const res = await fetch(`/pages/${key}.html`);
        if (res.ok) panel.innerHTML = await res.text();
    } catch (_) { /* child loaders already fail-soft in-page */ }
    const init = _CHILD_INIT[key];
    if (init) {
        try {
            await init();
        } catch (err) { /* child loaders already fail-soft in-page */ }
    }
}

let currentUser = null;
let isSuperAdmin = false;
// GSEC R-096 — the Locked accounts section is platform-admin only.
let isPlatformAdmin = false;

// Format a FastAPI error body for human display. `detail` may be:
//   - a plain string (HTTPException(detail="..."))
//   - an array of Pydantic validation errors: [{loc, msg, type, input}, ...]
//   - an object with `detail` itself a string
// Without this helper, the array case stringifies as
// "[object Object],[object Object]" which is what surfaced to the user.
function _formatApiError(body, fallback) {
    if (!body) return fallback || 'Unknown error';
    const detail = body.detail !== undefined ? body.detail : body;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(e => {
            if (!e || typeof e !== 'object') return String(e);
            const loc = Array.isArray(e.loc) ? e.loc.filter(p => p !== 'body').join('.') : '';
            return loc ? `${loc}: ${e.msg || 'invalid'}` : (e.msg || JSON.stringify(e));
        }).join('; ');
    }
    if (typeof detail === 'object') {
        // 2400LR-g6rbac: FastAPI routers also raise nested details —
        // HTTPException(detail={'code': 'rbac_custom_required', 'detail': msg})
        // (CORE/API/routers/rbac.py). Stringifying showed the user "[object
        // Object]". Unwrap {detail: string} / {message: string} first; keep the
        // JSON dump only for genuinely structured payloads (name_collision).
        const inner = detail.detail !== undefined ? detail.detail : detail.message;
        if (typeof inner === 'string' && inner) return inner;
        try { return JSON.stringify(detail); } catch { return fallback || 'Unknown error'; }
    }
    return String(detail);
}

export async function initializeUsersPageListeners(initialTab = null) {
    console.log('Initializing unified Users page');

    // R37.4 part 2 — navigateToPage re-mounts this page's markup on every hash
    // visit (innerHTML wipe), so the child panels are empty again: allow one
    // reload per mount (security-hub.js _loadedOnce pattern).
    _resetChildLoads();
    _pendingUsersTab = initialTab || getActiveTab();

    // Check user role and configure tabs
    checkUserRoleAndConfigureTabs();

    // Main tab switching
    document.querySelectorAll('.main-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            // R37.4 part 2 — the Users-group children own their own activation
            // (fetch-verbatim panel + their existing initializer, load-once), so
            // delegate before the inline-panel branches below.
            const _tabKey = tab.id.replace('main-tab-', '');
            if (USERS_CHILD_KEYS.includes(_tabKey)) {
                _activateUsersChildTab(_tabKey);
                return;
            }
            // MT-CE-3C-E: CE is single-tenant — the Tenants tab (create-tenant
            // form + All Tenants table) is the cross-tenant surface. Deny the
            // click BEFORE the reveal, the panel un-hide and the GET /tenants
            // fetch, so a hand-clicked hidden button or stale deep link falls
            // back to nothing (Users stays: users.html:82 main-panel-users is
            // the only panel without `hidden` in markup).
            if (_ceEdition && _tabKey === 'tenants') return;
            // Same for the custom-roles editor: CE has no role write routes.
            if (_ceEdition && _tabKey === 'roles') return;
            // Update tab styles
            document.querySelectorAll('.main-tab').forEach(t => {
                t.classList.remove('border-primary', 'text-primary');
                t.classList.add('border-transparent', 'text-gray-500');
            });
            tab.classList.add('border-primary', 'text-primary');
            tab.classList.remove('border-transparent', 'text-gray-500');

            // Switch panels
            const panelId = tab.id.replace('main-tab-', 'main-panel-');
            document.querySelectorAll('.main-panel').forEach(p => p.classList.add('hidden'));
            document.getElementById(panelId).classList.remove('hidden');

            // Initialize tab content if needed
            if (panelId === 'main-panel-users') {
                loadTenantsForUsersTab(); // Load tenant selector for platform admins
            } else if (panelId === 'main-panel-invitations') {
                // Load tenant selector first for platform admins
                // loadInvitations() will be triggered after they select a tenant
                loadTenantsForPlatformAdmin(); // This handles both platform admin and regular users
                loadPermissionTemplates();
                initializeCogSelector();
                maybeLoadSignupHealth(); // relocated from System — platform-admin only
            } else if (panelId === 'main-panel-tenants') {
                loadTenants();
            } else if (panelId === 'main-panel-roles') {
                loadRolesForPanel();
            } else if (panelId === 'main-panel-custom-plans') {
                loadCustomPlans();
                loadTenantsForCustomPlans();
            } else if (panelId === 'main-panel-identity') {
                _loadIdentityTab();
            }
        });
    });

    // GSEC R-096 — Locked accounts (platform admin): refresh + per-row unlock.
    const lockedTable = document.getElementById("locked-accounts-table");
    if (lockedTable) {
        lockedTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('unlock-account-button')) {
                handleUnlockAccount(e);
            }
        });
    }
    const lockedRefresh = document.getElementById('locked-accounts-refresh');
    if (lockedRefresh) {
        lockedRefresh.addEventListener('click', () => loadLockedAccounts());
    }

    // FX.106 — rate-limit auto-blocks (platform admin): refresh + per-row unblock.
    const rlBlocksTable = document.getElementById("rate-limit-blocks-table");
    if (rlBlocksTable) {
        rlBlocksTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('unblock-ip-button')) {
                handleUnblockRateLimitIp(e);
            }
        });
    }
    const rlBlocksRefresh = document.getElementById('rate-limit-blocks-refresh');
    if (rlBlocksRefresh) {
        rlBlocksRefresh.addEventListener('click', () => loadRateLimitBlocks());
    }

    // Users tab event listeners
    const goToInvitationsLink = document.getElementById('go-to-invitations-link');
    if (goToInvitationsLink) {
        goToInvitationsLink.addEventListener('click', (e) => {
            e.preventDefault();
            // Switch to invitations tab
            document.getElementById('main-tab-invitations').click();
        });
    }

    const usersTable = document.getElementById("users-table");
    if (usersTable) {
        usersTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('deactivate-user-button')) {
                handleDeactivateUser(e);
            } else if (e.target.classList.contains('reactivate-user-button')) {
                handleReactivateUser(e);
            } else if (e.target.classList.contains('reset-mfa-button')) {
                handleResetMfaUser(e);
            } else if (e.target.classList.contains('relink-login-button')) {
                handleRelinkLogin(e);
            } else if (e.target.classList.contains('delete-user-button')) {
                handleDeleteUser(e);
            }
        });
    }

    // Invitations tab event listeners
    const invitationForm = document.getElementById('invitation-form');
    if (invitationForm) {
        invitationForm.addEventListener('submit', handleSendInvitation);
    }
    // Copy button of the accept-link callout under the Send Invitation form.
    const teamCopyBtn = document.getElementById('team-invite-copy');
    if (teamCopyBtn) {
        teamCopyBtn.addEventListener('click', async () => {
            const ok = await copyToClipboard(document.getElementById('team-invite-link')?.value || '');
            teamCopyBtn.textContent = ok ? 'Copied!' : 'Copy failed';
            setTimeout(() => { teamCopyBtn.textContent = 'Copy link'; }, 2000);
        });
    }

    const invitationsTable = document.getElementById("invitations-table");
    if (invitationsTable) {
        invitationsTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('cancel-invitation-button')) {
                handleCancelInvitation(e);
            }
        });
    }

    // Tenants tab event listeners
    // CE hide: the Create New Tenant form is PAID-only (mirrors the tenant-select guards); the wiring below stays for the optional path.
    if (!await isPaidEdition()) {
        const createTenantFormCe = document.getElementById('create-tenant-form');
        if (createTenantFormCe) createTenantFormCe.style.display = 'none';
    }
    const createTenantForm = document.getElementById('create-tenant-form');
    if (createTenantForm) {
        createTenantForm.addEventListener('submit', handleCreateTenant);
    }

    // Custom Plans tab event listeners
    const createCustomPlanForm = document.getElementById('create-custom-plan-form');
    if (createCustomPlanForm) {
        createCustomPlanForm.addEventListener('submit', handleCreateCustomPlan);
        _wireUnlimitedCheckboxes();
    }

    const createAndAssignBtn = document.getElementById('create-and-assign-btn');
    if (createAndAssignBtn) {
        createAndAssignBtn.addEventListener('click', handleCreateAndAssignPlan);
    }

    const refreshCustomPlansBtn = document.getElementById('refresh-custom-plans-btn');
    if (refreshCustomPlansBtn) {
        refreshCustomPlansBtn.addEventListener('click', loadCustomPlans);
    }

    const customPlansTable = document.getElementById("custom-plans-table");
    if (customPlansTable) {
        customPlansTable.addEventListener('click', (e) => {
            if (e.target.classList.contains('assign-plan-button')) {
                handleAssignPlanToTenant(e);
            } else if (e.target.classList.contains('delete-plan-button')) {
                handleDeleteCustomPlan(e);
            }
        });
    }

    // Load appropriate default tab based on role
    // (Will be called after role check completes)
}

async function checkUserRoleAndConfigureTabs() {
    try {
        // Get current user information
        currentUser = await me.get();
        isSuperAdmin = currentUser.parent_user_id === null;
        isPlatformAdmin = currentUser.is_platform_admin === true;

        // 2400LR-cdu: admin_lockout.py (locked-accounts) and admin_api_blocks.py
        // (rate-limit blocks) are denied on CE (manifest:1912 / 1227); fail-open on unknown edition.
        const paidEdition = await isPaidEdition();

        // GSEC R-096 — platform admins get the Locked accounts list.
        const lockedContainer = document.getElementById('locked-accounts-container');
        if (lockedContainer) {
            if (isPlatformAdmin && paidEdition) {
                lockedContainer.style.removeProperty('display');
                loadLockedAccounts();
            } else {
                lockedContainer.style.display = 'none';
            }
        }
        // FX.106 — platform admins also get the rate-limit auto-blocks panel.
        const rlBlocksContainer = document.getElementById('rate-limit-blocks-container');
        if (rlBlocksContainer) {
            if (isPlatformAdmin && paidEdition) {
                rlBlocksContainer.style.removeProperty('display');
                loadRateLimitBlocks();
            } else {
                rlBlocksContainer.style.display = 'none';
            }
        }

        if (isSuperAdmin) {
            // Super admins see Users and Invitations tabs
            document.getElementById('main-tab-users')?.classList.remove('hidden');
            document.getElementById('main-tab-invitations')?.classList.remove('hidden');

            // Identity & Access (SSO + SCIM) — tenant-admin or platform-admin.
            // Moved here from its own sidebar entry; the embedded page self-gates
            // by plan.
            document.getElementById('main-tab-identity')?.style.removeProperty('display');

            // Super admins see the Roles tab. Permission Templates moved to the
            // Permissions page (single implementation) — keep this duplicate hidden.
            document.getElementById('main-tab-roles')?.style.removeProperty('display');

            // Platform admins also see Tenants tab
            if (isPlatformAdmin) {
                document.getElementById('main-tab-tenants')?.style.removeProperty('display');

                // Custom Plans tab only visible when billing is enabled (SaaS mode)
                const features = window._platformFeatures;
                if (features && features.billing_enabled) {
                    document.getElementById('main-tab-custom-plans')?.style.removeProperty('display');
                } else {
                    try {
                        const feat = await getPlatformFeatures();
                        if (feat.billing_enabled) {
                            document.getElementById('main-tab-custom-plans')?.style.removeProperty('display');
                        }
                    } catch (e) { /* leave hidden */ }
                }
            }

            // Load users tab (default for super admins)
            loadTenantsForUsersTab();  // This will show tenant selector for platform admins
        } else {
            // Non-super-admins: hide Users tab, show only Invitations
            document.getElementById('main-tab-users')?.classList.add('hidden');
            document.getElementById('main-tab-invitations')?.classList.remove('hidden');

            // Make Invitations tab active by default for non-super-admins —
            // unless this mount is landing on one of the R37.4 child tabs.
            if (!(_pendingUsersTab && USERS_CHILD_KEYS.includes(_pendingUsersTab))) {
                document.getElementById('main-tab-invitations')?.click();
            }
        }

        // R37.4 part 2 — reveal the six ex-sidebar Users-group tabs with the
        // exact gate each old nav-link had in ui.js updateUIVisibility, then
        // land on the tab this mount asked for (deep link / ?tab= refresh).
        // Fail closed: only a positive 'saas'/'pro' edition counts as paid; an
        // unknown/errored entitlements payload keeps the CE carve (tabs hidden).
        _ceEdition = !(await isPaidEdition());
        // MT-CE-3C-D: CE is single-tenant — hide the create-tenant panel; isPaidEdition() is
        // fail-closed (api.js:473 returns false on error), so _ceEdition stays true and the panel
        // stays hidden when the edition cannot be determined. Paid (_ceEdition false) shows it.
        if (_ceEdition) document.getElementById('create-tenant-panel')?.style.setProperty('display', 'none');
        // 2400LR-pg2: Identity & Access (SSO + SCIM) routers are denied on CE
        // (manifest :1285-:1287), so the tab would only 404.
        if (_ceEdition) document.getElementById('main-tab-identity')?.style.setProperty('display', 'none');
        // FIX-CE-HIDE-TENANTS-TAB: hide the ENTIRE Tenants tab in CE — the All
        // Tenants table (ID/Name/Credits/Storage/Harbor override/Created/Actions)
        // is the cross-tenant surface and the create form is PAID-only. Fail
        // closed: _ceEdition is true on an unknown/errored entitlements block
        // (api.js:473 isPaidEdition returns false), so the tab stays hidden.
        if (_ceEdition) document.getElementById('main-tab-tenants')?.style.setProperty('display', 'none');
        if (_ceEdition) document.getElementById('main-panel-tenants')?.classList.add('hidden');
        // Custom roles (create / edit / delete) are not part of CE: the API ships only the
        // built-in system roles (GET /rbac/roles) and assign-role. Hide the editor tab.
        if (_ceEdition) document.getElementById('main-tab-roles')?.style.setProperty('display', 'none');
        if (_ceEdition) document.getElementById('main-panel-roles')?.classList.add('hidden');
        for (const key of USERS_CHILD_KEYS) {
            if (_childAllowed(key)) {
                document.getElementById(`main-tab-${key}`)?.classList.remove('hidden');
            }
        }
        if (_pendingUsersTab && USERS_CHILD_KEYS.includes(_pendingUsersTab)) {
            await _activateUsersChildTab(_pendingUsersTab);
        }
    } catch (error) {
        console.error('Error checking user role:', error);
        // Default to showing invitations on error
        loadInvitations();
    }
}

// ========================================
// Users Functions
// ========================================

async function loadTenantsForUsersTab() {
    // CE hide: tenant select is PAID-only
    if (!await isPaidEdition()) {
        const tc = document.getElementById('users-tenant-container');
        if (tc) tc.style.display = 'none';
        // CE is single-tenant: the tenant selector stays hidden and the tenants
        // list is never fetched, but the Users table still has to render its own
        // tenant's rows (e2e admin-identity.spec.ts:82 polls tbody tr > 0).
        // loadUsers() with no tenantId = the caller's own tenant only.
        loadUsers();
        return;
    }

    try {
        // Try to fetch tenants (only works for platform admins)
        const response = await fetch(`${window.APP_CONFIG?.API_URL || 'http://api.localhost'}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem('accessToken')}`
            }
        });

        if (response.status === 200) {
            // Platform admin - show tenant selector
            const tenants = await response.json();
            const tenantContainer = document.getElementById('users-tenant-container');
            const tenantSelect = document.getElementById('users-tenant-select');

            if (tenantContainer && tenantSelect) {
                tenantContainer.style.display = 'block';

                // Populate tenant dropdown
                tenantSelect.innerHTML = '<option value="">Select a tenant...</option>';
                tenants.forEach(tenant => {
                    const option = document.createElement('option');
                    option.value = tenant.id;
                    option.textContent = `${tenant.name} (ID: ${tenant.id})`;
                    tenantSelect.appendChild(option);
                });

                // Add change listener
                tenantSelect.addEventListener('change', () => {
                    const selectedTenantId = tenantSelect.value ? parseInt(tenantSelect.value, 10) : null;
                    loadUsers(selectedTenantId);
                });
            }

            // Don't load users yet - wait for tenant selection
        } else {
            // Not platform admin - load users from own tenant
            loadUsers();
        }
    } catch (error) {
        console.error('Error loading tenants for users tab:', error);
        // Fallback: load users from own tenant
        loadUsers();
    }
}

// Roles for the tenant currently shown in the Users table (system + custom),
// used to populate each row's role-assign dropdown. Refreshed by loadUsers.
let _usersTableRoles = [];

async function loadUsers(tenantId = null) {
    const usersTableBody = document.querySelector("#users-table tbody");
    if (!usersTableBody) return;

    try {
        // Fetch the users and the tenant's assignable roles (system + custom)
        // in parallel. Roles power the per-row assign dropdown; a role-load
        // failure (e.g. missing entitlement) degrades to an empty dropdown
        // rather than blocking the user list.
        const [userList, roles] = await Promise.all([
            users.get(tenantId),
            users.roles(tenantId).catch(() => []),
        ]);
        _usersTableRoles = Array.isArray(roles) ? roles : [];
        renderUsers(userList, tenantId);
    } catch (error) {
        console.error("Error loading users:", error);
        usersTableBody.innerHTML = `<tr><td colspan="3" class="text-center text-red-500 py-4">Could not load users.</td></tr>`;
    }
}

function renderUsers(userList, tenantId = null) {
    const usersTableBody = document.querySelector("#users-table tbody");
    if (!usersTableBody) return;

    usersTableBody.innerHTML = '';
    if (userList.length === 0) {
        usersTableBody.innerHTML = `<tr><td colspan="3" class="text-center text-gray-500 py-4">No users found.</td></tr>`;
        return;
    }

    userList.forEach(user => {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50';
        // Role dropdown: list ALL assignable roles for the tenant (system +
        // custom), pre-selecting the user's current role so a re-fetch reflects
        // what actually persisted (previously the dropdown always reset to
        // "No role", which read as "the change didn't stick").
        const currentRoleId = user.custom_role && user.custom_role.id != null
            ? user.custom_role.id : '';
        const roleOptions = ['<option value="">No role</option>']
            .concat(_usersTableRoles.map(r =>
                `<option value="${r.id}" ${String(r.id) === String(currentRoleId) ? 'selected' : ''}>${_escapeHtml(r.name)}</option>`
            )).join('');
        // FX.88 p4 — the Reset 2FA route needs a platform admin (any tenant)
        // or a tenant admin holding the user:deactivate capability, so the
        // button is rendered only for those roles (same predicate as the
        // tenant-admin-only panels elsewhere). A tenant admin without the
        // capability still gets a visible 403 detail from the API.
        const canResetMfa = isPlatformAdmin || isSuperAdmin;
        const resetMfaButton = canResetMfa
            ? `<button class="reset-mfa-button text-blue-700 hover:text-blue-900 font-medium" data-id="${user.id}">Reset 2FA</button>`
            : '';
        // BUG.B5b2 — Re-link login button gated on linked===false.
        // The status is fetched later (see _fetchIdentityStatuses).
        let relinkActionCell = '';
        if (canResetMfa) {
            relinkActionCell = `<span class="relink-status" data-user-id="${user.id}" data-linked="">Loading…</span>`;
        }
        const isActive = String(user.status).toLowerCase() === 'active';
        const activationButton = isActive
            ? `<button class="deactivate-user-button text-yellow-600 hover:text-yellow-800" data-id="${user.id}">Deactivate</button>`
            : `<button class="reactivate-user-button text-green-600 hover:text-green-800" data-id="${user.id}">Reactivate</button>`;
        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_escapeHtml(user.username || '-')}${isActive ? '' : ' <span class="ml-1 px-1.5 py-0.5 text-xs bg-gray-100 text-gray-600 rounded">inactive</span>'}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_escapeHtml(user.email || '')}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm">
                <select aria-label="Role for ${_escapeHtml(user.email || user.username || 'user ' + user.id)}" class="role-assign-select border border-gray-300 rounded px-2 py-1 text-sm" data-user-id="${user.id}">
                    ${roleOptions}
                </select>
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm space-x-2">
                ${activationButton}
                ${resetMfaButton}
                ${relinkActionCell}
                <button class="delete-user-button text-red-600 hover:text-red-800 font-medium" data-id="${user.id}">Delete</button>
            </td>
        `;
        usersTableBody.appendChild(row);

        const select = row.querySelector('.role-assign-select');
        select.addEventListener('change', async () => {
            const roleId = select.value;
            if (!roleId) return;
            // FX.163 p3b1 — the notice lands next to the users table, the
            // nearest result container that can hold a block element.
            const noticeHost = usersTableBody.closest('table')?.parentElement || usersTableBody;
            try {
                const resp = await users.assignRole(user.id, parseInt(roleId, 10));
                if (isAwaitingApproval(resp)) {
                    // GSEC FX.163 (owner Q150=A): the grant did NOT run — a
                    // different tenant admin has to approve it. Show the
                    // pending notice, keep the dropdown un-highlighted, and
                    // do NOT re-fetch as if the role had stuck.
                    showAwaitingApproval(noticeHost, resp, { actionLabel: 'Role grant' });
                    return;
                }
                clearAwaitingApproval(noticeHost);
                // Re-fetch so the row reflects the persisted role (proves it stuck).
                select.style.borderColor = '#22c55e';
                await loadUsers(_currentUsersTenantId());
            } catch (e) {
                showError('Failed to assign role: ' + (e?.message || 'Unknown error'));
                await loadUsers(_currentUsersTenantId());
            }
        });
    });
    // BUG.B5b2 — fetch identity status inline (all platform-admin rows).
    _fetchIdentityStatuses();
}

// Minimal HTML escaper for values interpolated into the users table.
function _escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Platform admins view users one tenant at a time via the tenant selector.
// After any action on a user we need to reload THAT SAME tenant, not the
// admin's own tenant (the default of `loadUsers()` with no args).
function _currentUsersTenantId() {
    const sel = document.getElementById('users-tenant-select');
    return (sel && sel.value) ? parseInt(sel.value, 10) : null;
}

// BUG.B5b2 — fetch identity status for all platform-admin-visible users and
// update the inline placeholder with either the "Re-link login" button or
// a "Login linked" muted label.  (2400MFA2R: admin_mfa ships in the CE
// manifest (251b0f28e), so identity status is fetched on CE too — no gate.)
async function _fetchIdentityStatuses() {
    const notices = document.querySelectorAll('.relink-status');
    if (!notices.length) return;
    const statuses = {};
    await Promise.allSettled(
        Array.from(notices).map(async (el) => {
            const uid = el.getAttribute('data-user-id');
            if (statuses[uid] !== undefined) return;
            try {
                const resp = await users.identityStatus(uid);
                statuses[uid] = resp;
            } catch {
                // Non-fatal — keep the placeholder as-is.
            }
        }),
    );
    notices.forEach((el) => {
        const uid = el.getAttribute('data-user-id');
        const info = statuses[uid];
        if (info && info.linked === false) {
            el.innerHTML = `<button class="relink-login-button bg-sky-600 text-white px-2 py-1 rounded text-xs font-medium hover:bg-sky-700" data-id="${uid}">Re-link login</button>`;
        } else {
            el.textContent = 'Login linked';
            el.classList.add('text-gray-400');
        }
    });
}

async function handleDeactivateUser(event) {
    const userId = event.target.getAttribute('data-id');
    if (await showConfirm('Are you sure you want to deactivate this user? They will remain in the system but cannot log in.')) {
        try {
            await users.deactivate(userId);
            await loadUsers(_currentUsersTenantId());
        } catch (error) {
            console.error("Error deactivating user:", error);
            showError("Failed to deactivate user.");
        }
    }
}

async function handleReactivateUser(event) {
    const userId = event.target.getAttribute('data-id');
    if (await showConfirm('Reactivate this user? They will be able to log in again. Any previously-revoked API keys stay revoked.')) {
        try {
            await users.reactivate(userId);
            await loadUsers(_currentUsersTenantId());
        } catch (error) {
            console.error("Error reactivating user:", error);
            showError("Failed to reactivate user.");
        }
    }
}

async function handleResetMfaUser(event) {
    const button = event.target;
    const userId = button.getAttribute('data-id');
    // GY.C4.MFA — admin 2FA reset. Removes the user's authenticator in the
    // identity provider and queues re-enrollment on the MOD-branded sign-in
    // page. Audited (auth.mfa_reset) + platform-admin notification. Service
    // accounts are refused (409) — they are exempt from 2FA.
    //
    // FX.88 p4 — the button is disabled for the whole request (confirm +
    // round-trip) so a second click cannot queue a second reset; the finally
    // puts it back so a failed attempt can be retried.
    button.disabled = true;
    button.classList.add('opacity-50');
    try {
        const message = 'Reset this user\'s two-factor authenticator? Their device '
            + 'will be removed and they will be asked to set up 2FA again on their '
            + 'next sign-in. This action is audited.';
        if (await showConfirm(message, 'Reset 2FA')) {
            try {
                const result = await users.resetMfa(userId, 'admin reset from Users page');
                const waCount = (result.webauthn_credentials_removed || []).length;
                showInfo(`2FA reset for user ${result.user_id}. Authenticator${waCount ? ' and security key' + (waCount > 1 ? 's' : '') : ''} removed; re-enrollment queued.`, '2FA reset');
                await loadUsers(_currentUsersTenantId());
            } catch (error) {
                console.error("Error resetting user 2FA:", error);
                // The API's own detail (403 missing capability, 409 exempt
                // service account, 502 provider failure) is what the operator
                // needs, so surface it verbatim rather than a generic line.
                const detail = error.detail != null
                    ? _formatApiError({ detail: error.detail }, error.message)
                    : (error?.message || 'please try again.');
                showError(`Failed to reset 2FA: ${detail}`, 'Reset 2FA failed');
            }
        }
    } finally {
        button.disabled = false;
        button.classList.remove('opacity-50');
    }
}

// BUG.B5b2 — Re-link login: inline confirm modal with reason + checkbox gating.
function _renderRelinkModal(userId) {
    // Re-use the same a11y-safe overlay pattern as ui.js's showConfirm.
    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 z-50 bg-black/50 flex items-center justify-center';
    overlay.setAttribute('role', 'alertdialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-labelledby', 'relink-modal-title');
    overlay.innerHTML = `
        <div class="bg-white rounded-lg shadow-xl max-w-lg w-full mx-4 p-6">
            <h3 id="relink-modal-title" class="text-lg font-semibold text-gray-900 mb-3">Re-link login</h3>
            <p class="text-sm text-gray-700 mb-4">
                This user's sign-in account is missing or disconnected. MOD will reconnect it to
                the matching verified account, or create a fresh one and email the user a
                set-password link.
            </p>
            <label class="block text-sm font-medium text-gray-700 mb-1">Reason <span class="text-red-600">*</span></label>
            <textarea id="relink-reason" aria-label="Reason" class="w-full border border-gray-300 rounded px-3 py-2 text-sm" rows="3" minlength="5" maxlength="500" placeholder="e.g. User migrated from legacy auth provider"></textarea>
            <div id="relink-reason-error" class="text-xs text-red-600 mt-1 hidden"></div>
            <label class="flex items-start gap-2 mt-3">
                <input type="checkbox" id="relink-confirm" class="mt-0.5">
                <span class="text-sm text-gray-700">I understand this will change the user's sign-in identity.</span>
            </label>
            <div class="flex justify-end gap-2 mt-5">
                <button id="relink-cancel" class="px-4 py-2 text-sm text-gray-700 bg-gray-100 rounded hover:bg-gray-200">Cancel</button>
                <button id="relink-submit" class="px-4 py-2 text-sm bg-sky-600 text-white rounded hover:bg-sky-700 disabled:opacity-50" disabled>Submit</button>
            </div>
        </div>
    `;
    const form = overlay.querySelector('#relink-modal-title').parentElement;
    const reasonInput = overlay.querySelector('#relink-reason');
    const confirmInput = overlay.querySelector('#relink-confirm');
    const submitBtn = overlay.querySelector('#relink-submit');
    const reasonError = overlay.querySelector('#relink-reason-error');
    let resolved = null;

    function validate() {
        const reason = reasonInput.value.trim();
        const valid = reason.length >= 5 && reason.length <= 500 && confirmInput.checked;
        submitBtn.disabled = !valid;
        if (reason.length < 5 && reason.length > 0) {
            reasonError.textContent = 'Reason must be at least 5 characters.';
            reasonError.classList.remove('hidden');
        } else if (reason.length === 0) {
            reasonError.textContent = 'Reason is required.';
            reasonError.classList.remove('hidden');
        } else {
            reasonError.classList.add('hidden');
        }
    }
    reasonInput.addEventListener('input', validate);
    confirmInput.addEventListener('change', validate);

    overlay.querySelector('#relink-cancel').addEventListener('click', () => {
        document.body.removeChild(overlay);
        if (resolved) resolved(false);
    });
    submitBtn.addEventListener('click', () => {
        if (resolved) { resolved(true); resolved = null; }
        document.body.removeChild(overlay);
    });
    overlay.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            document.body.removeChild(overlay);
            if (resolved) resolved(false);
        }
    });
    document.body.appendChild(overlay);
    reasonInput.focus();

    return new Promise((resolve) => {
        resolved = resolve;
    });
}

async function handleRelinkLogin(event) {
    const button = event.target;
    const userId = button.getAttribute('data-id');
    // Find a host for the inline result (next to the users table).
    const noticeHost = document.querySelector('.relink-status')
        ?.closest('table')?.parentElement || document.querySelector('#users-notice');
    try {
        const ok = await _renderRelinkModal(userId);
        if (!ok) return;
        const reason = document.getElementById('relink-reason')?.value.trim() || '';
        button.disabled = true;
        button.classList.add('opacity-50');
        try {
            const result = await users.relinkIdentity(userId, reason);
            // SECURITY (2400LR): server/user-influenced values (matched_by can carry
            // an email, ids and reason strings are server text) must be escaped before
            // they go into innerHTML. msgText stays raw for the textContent path.
            let msgText;
            if (result.action === 'relinked') {
                msgText = `Account re-linked. Old Keycloak user: ${result.old_keycloak_user_id || '(unknown)'} → New: ${result.new_keycloak_user_id || '(unknown)'}${result.matched_by ? ' (matched by: ' + result.matched_by + ')' : ''}.`;
            } else {
                msgText = `New account created and emailed. Keycloak user: ${result.new_keycloak_user_id || '(unknown)'}${result.reason_for_reprovision ? ' (' + result.reason_for_reprovision + ')' : ''}.`;
            }
            if (noticeHost) {
                noticeHost.innerHTML = `<div class="mt-2 p-2 bg-green-50 border border-green-200 rounded text-sm text-green-800">${_escapeHtml(msgText)}</div>`;
            } else {
                showInfo(msgText, 'Re-link login');
            }
        } catch (error) {
            console.error("Error relinking login:", error);
            const detail = error.detail != null
                ? _formatApiError({ detail: error.detail }, error.message)
                : (error?.message || 'please try again.');
            if (noticeHost) {
                noticeHost.innerHTML = `<div class="mt-2 p-2 bg-red-50 border border-red-200 rounded text-sm text-red-800">Failed to re-link: ${_escapeHtml(detail)}</div>`;
            } else {
                showError(`Failed to re-link: ${detail}`, 'Re-link login failed');
            }
        }
    } finally {
        button.disabled = false;
        button.classList.remove('opacity-50');
    }
}

// ========================================
// Locked accounts (GSEC R-096, platform admin)
// ========================================

function _fmtEpochMs(ms) {
    if (ms == null) return '-';
    try { return new Date(Number(ms)).toLocaleString(); }
    catch { return '-'; }
}

async function loadLockedAccounts() {
    if (!await isPaidEdition()) {
        // 2400LR-cdu: admin_lockout.py denied on CE (manifest:1998); fail-open on unknown edition.
        return;
    }
    const tbody = document.querySelector('#locked-accounts-table tbody');
    if (!tbody) return;
    try {
        const data = await users.lockedAccounts();
        const rows = Array.isArray(data) ? data : (data?.locked_users || []);
        if (rows.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-4 text-center text-sm text-gray-500">No locked accounts.</td></tr>`;
            return;
        }
        tbody.innerHTML = rows.map(u => {
            const id = u.keycloak_user_id != null ? u.keycloak_user_id : (u.id ?? '');
            return `<tr>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_escapeHtml(u.username || '-')}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${u.failed_attempts != null ? u.failed_attempts : '-'}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_fmtEpochMs(u.last_failed)}</td>
                <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_fmtEpochMs(u.temporary_lockout_until)}</td>
                <td class="px-6 py-4 whitespace-nowrap text-right text-sm">
                    <button class="unlock-account-button text-green-700 hover:text-green-900 font-medium" data-kc-id="${_escapeHtml(String(id))}">Unlock</button>
                </td>
            </tr>`;
        }).join('');
    } catch (error) {
        console.error("Error loading locked accounts:", error);
        tbody.innerHTML = `<tr><td colspan="5" class="px-6 py-4 text-center text-sm text-red-500">Could not load locked accounts.</td></tr>`;
    }
}

async function handleUnlockAccount(event) {
    const kcId = event.target.getAttribute('data-kc-id');
    if (!kcId) return;
    const message = 'Unlock this account? This clears the failed-signin lockout in the identity provider. This action is audited.';
    if (await showConfirm(message, 'Unlock account')) {
        try {
            await users.unlockAccount(kcId, 'admin unlock from Users page');
            showInfo(`Account ${kcId} unlocked.`, 'Account unlocked');
            await loadLockedAccounts();
        } catch (error) {
            console.error("Error unlocking account:", error);
            showError(`Failed to unlock account: ${error?.message || 'please try again.'}`);
        }
    }
}

// ========================================
// FX.106 — rate-limit auto-blocks (platform admin)
// ========================================

function _fmtRateLimitRemaining(sec) {
    if (sec == null) return '-';
    const s = Math.max(0, Math.floor(Number(sec)));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

async function loadRateLimitBlocks() {
    if (!await isPaidEdition()) {
        // 2400LR-cdu: admin_lockout.py denied on CE (manifest:1998); fail-open on unknown edition.
        return;
    }
    const tbody = document.querySelector('#rate-limit-blocks-table tbody');
    if (!tbody) return;
    const hitsEl = document.getElementById('rate-limit-recent-hits');
    const settingsEl = document.getElementById('rate-limit-blocks-settings');
    try {
        const data = await users.rateLimitBlocks();
        const blocks = data?.active_blocks || [];
        const recent = data?.recent_hits || [];
        const s = data?.settings || {};
        if (settingsEl) {
            settingsEl.textContent =
                ` Per-key cap: ${s.per_key_per_minute ?? '-'} req/min · ` +
                `block after ${s.block_after_login_failures ?? '-'} failed logins / ` +
                `${s.block_after_429s ?? '-'} rate-limit hits (window ${s.window_min ?? '-'}min, ` +
                `duration ${s.duration_min ?? '-'}min).`;
        }
        if (blocks.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-4 text-center text-sm text-gray-500">No active auto-blocks.</td></tr>`;
        } else {
            tbody.innerHTML = blocks.map(b => {
                const ts = b.unblocks_at ? new Date(b.unblocks_at * 1000).toLocaleString() : '-';
                return `<tr>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900 font-mono">${_escapeHtml(b.ip || '-')}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${ts}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_fmtRateLimitRemaining(b.remaining_seconds)}</td>
                    <td class="px-6 py-4 whitespace-nowrap text-sm text-right">
                        <button class="unblock-ip-button text-green-700 hover:text-green-900 font-medium" data-ip="${_escapeHtml(b.ip || '')}">Unblock</button>
                    </td>
                </tr>`;
            }).join('');
        }
        if (hitsEl) {
            if (recent.length === 0) {
                hitsEl.textContent = 'No recent limit activity.';
            } else {
                hitsEl.innerHTML = recent.map(h => {
                    const ts = h.timestamp ? new Date(h.timestamp * 1000).toLocaleString() : '-';
                    const kind = _escapeHtml(h.kind || '');
                    return `<div class="flex items-center gap-3 py-1">
                        <span class="w-28 shrink-0 text-xs text-gray-400">${ts}</span>
                        <span class="w-32 shrink-0 text-xs font-medium">${kind}</span>
                        <span class="font-mono text-xs">${_escapeHtml(h.ip_address || '-')}</span>
                        <span class="text-xs text-gray-500 truncate">${_escapeHtml(h.detail || '')}</span>
                    </div>`;
                }).join('');
            }
        }
    } catch (error) {
        console.error("Error loading rate-limit blocks:", error);
        tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-4 text-center text-sm text-red-500">Could not load auto-blocks.</td></tr>`;
    }
}

async function handleUnblockRateLimitIp(event) {
    const ip = event.target.getAttribute('data-ip');
    if (!ip) return;
    const message = `Unblock client IP ${ip} early? This is audited. The block would otherwise expire on its own.`;
    if (await showConfirm(message, 'Unblock IP')) {
        try {
            await users.unblockRateLimitIp(ip, 'admin unblock from Users page');
            showInfo(`IP ${ip} unblocked.`, 'IP unblocked');
            await loadRateLimitBlocks();
        } catch (error) {
            console.error("Error unblocking IP:", error);
            showError(`Failed to unblock IP: ${error?.message || 'please try again.'}`);
        }
    }
}

async function handleDeleteUser(event) {
    const userId = event.target.getAttribute('data-id');
    if (await showConfirm('This user will be soft-deleted: login disabled, PII scrubbed, row kept for audit. Continue?', 'Delete user')) {
        try {
            await users.delete(userId);
            await loadUsers(_currentUsersTenantId());
        } catch (error) {
            console.error("Error deleting user:", error);
            showError("Failed to delete user.");
        }
    }
}

// ========================================
// Invitations Functions
// ========================================

async function loadInvitations() {
    try {
        // Check if platform admin has selected a tenant (uses same selector as invitation form)
        const tenantSelect = document.getElementById('invitation-tenant');
        const selectedTenantId = tenantSelect && tenantSelect.value ? parseInt(tenantSelect.value, 10) : null;

        const invs = await invitations.get(selectedTenantId);
        renderInvitations(invs);
    } catch (error) {
        // 403 — user lacks invitation-read permission. The fallback
        // would also 403; skip it and stay quiet (this fires on every
        // navigation for non-admin users).
        if (error?.status === 403) return;
        console.error("Error loading invitations:", error);
        // Fallback: try to load all invitations (backend will filter)
        try {
            const invs = await invitations.get();
            renderInvitations(invs);
        } catch (e) {
            if (e?.status === 403) return;
            console.error("Error loading invitations fallback:", e);
        }
    }
}

function renderInvitations(invs) {
    const invitationsTableBody = document.querySelector("#invitations-table tbody");
    if (!invitationsTableBody) return;

    invitationsTableBody.innerHTML = '';
    if (invs.length === 0) {
        invitationsTableBody.innerHTML = `<tr><td colspan="3" class="text-center text-gray-500 py-4">No pending invitations.</td></tr>`;
        return;
    }

    invs.forEach(invitation => {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50';
        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_escapeHtml(invitation.email)}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${new Date(invitation.expires_at).toLocaleString()}</td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm">
                <button class="cancel-invitation-button text-red-600 hover:text-red-800" data-id="${invitation.id}">Cancel</button>
            </td>
        `;
        invitationsTableBody.appendChild(row);
    });
}

async function handleCancelInvitation(event) {
    const invitationId = event.target.getAttribute('data-id');
    if (await showConfirm('Are you sure you want to cancel this invitation?')) {
        try {
            await invitations.cancel(invitationId);
            loadInvitations(); // Refresh the list
        } catch (error) {
            console.error("Error canceling invitation:", error);
            showError("Failed to cancel invitation.");
        }
    }
}

async function handleSendInvitation(event) {
    event.preventDefault();
    const email = document.getElementById('invitation-email').value;
    const templateId = document.getElementById('invitation-template').value;
    const customRoleId = document.getElementById('invitation-role').value;
    const tenantSelect = document.getElementById('invitation-tenant');
    // LDF-3 §11.3 fix — the visibility gate lives on the tenant
    // CONTAINER (`#invitation-tenant-container`), not the <select>
    // itself. Reading `tenantSelect.style.display` is always '' (the
    // select is never hidden directly), so the previous check fired
    // for tenant admins and blocked submit (Tim's #19 regression).
    const tenantContainer = document.getElementById('invitation-tenant-container');
    const isPickerVisible = tenantContainer && tenantContainer.style.display !== 'none' &&
                            !tenantContainer.classList.contains('hidden');
    const tenantId = (isPickerVisible && tenantSelect) ? tenantSelect.value : null;

    const selectors = document.querySelectorAll('.cog-dropdown');
    const lastSelector = selectors[selectors.length - 1];
    const cogId = lastSelector ? lastSelector.value : "";

    if (!customRoleId) {
        showInfo("Please select a role to assign to the user.");
        return;
    }

    // Only enforce tenant selection when the picker is actually
    // visible (platform admin). Tenant admins fall through; the API
    // uses their own tenant_id.
    if (isPickerVisible && !tenantId) {
        showInfo("Please select a tenant for this invitation.");
        return;
    }

    try {
        // Build the request based on whether a specific folder/file was chosen.
        const payload = {
            email: email,
            custom_role_id: parseInt(customRoleId, 10)
        };

        // Add tenant_id for platform admins
        if (tenantId) {
            payload.tenant_id = parseInt(tenantId, 10);
        }

        // Only add folder/file permissions if both cog AND template are selected
        if (cogId && templateId) {
            payload.cog_id = parseInt(cogId, 10);
            payload.template_id = parseInt(templateId, 10);
        }

        const created = await invitations.create(payload);

        // email_delivered === false (strict): no e-mail system is set up, nothing was
        // sent; show the accept link callout with the plain no-email wording.
        if (created && created.accept_link && created.email_delivered === false) {
            showTeamInviteAcceptLink(created.email, created.accept_link, false);
            showError('Invitation created. No email was sent: copy the link below and send it yourself.', 'E-mail sending is not available');
        } else {
            showInfo('Invitation sent successfully!');
        }
        loadInvitations();
        document.getElementById('invitation-form').reset();
        initializeCogSelector();
    } catch (error) {
        console.error("Error sending invitation:", error);
        // Use copyable modal for configuration errors
        showCopyableModal(
            'Failed to Send Invitation',
            error.message || 'An unexpected error occurred'
        );
    }
}

async function loadRolesForInvitation(tenantId = null) {
    const roleSelector = document.getElementById('invitation-role');
    if (!roleSelector) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const url = tenantId
            ? `${apiUrl}/rbac/roles?tenant_id=${tenantId}`
            : `${apiUrl}/rbac/roles`;

        console.log("Loading roles from:", url);

        const response = await fetch(url, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) throw new Error(`Failed to load roles: ${response.status}`);

        const roles = await response.json();
        console.log("Loaded roles:", roles);

        if (roles.length === 0) {
            roleSelector.innerHTML = '<option value="">No roles available - create roles first</option>';
            console.warn("No roles found for tenant. User must create roles first.");
            return;
        }

        roleSelector.innerHTML = '<option value="">Select a role...</option>';
        roles.forEach(role => {
            const option = document.createElement('option');
            option.value = role.id;
            option.textContent = role.name;
            roleSelector.appendChild(option);
        });
    } catch (error) {
        // 403 — user lacks role-read permission. Show a neutral
        // empty-state instead of "Failed to load roles" and skip the
        // loud console.error (this is expected for non-admin users).
        if (error?.status === 403 || /\b403\b/.test(error?.message || '')) {
            roleSelector.innerHTML = '<option value="">No roles available</option>';
            return;
        }
        console.error("Error loading roles for invitation:", error);
        roleSelector.innerHTML = '<option value="">Failed to load roles</option>';
    }
}

async function loadTenantsForPlatformAdmin() {
    // CE hide: the Invitations panel tenant select is PAID-only (mirrors loadTenantsForUsersTab and the invitations.js copy) - deny before the container lookup, the tenants fetch and the reveal below.
    if (!await isPaidEdition()) {
        const tc = document.getElementById('invitation-tenant-container');
        if (tc) tc.style.display = 'none';
        // CE is single-tenant: no tenant list, but the role dropdown and the
        // invitations list still load for the caller's own tenant (the early
        // return before these two calls left #invitation-role empty on CE).
        loadRolesForInvitation();
        loadInvitations();
        return;
    }

    const tenantSelectorContainer = document.getElementById('invitation-tenant-container');
    if (!tenantSelectorContainer) {
        console.log("Tenant selector container not found in DOM");
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        console.log("Attempting to fetch tenants from:", `${apiUrl}/tenants`);

        const response = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        console.log("Tenants API response status:", response.status);

        if (!response.ok) {
            console.log("User is not platform admin (403) or API error - hiding tenant selector");
            tenantSelectorContainer.style.display = 'none';
            // Load roles and invitations for regular user (they don't have tenant selector)
            loadRolesForInvitation();
            loadInvitations();
            return;
        }

        const tenants = await response.json();
        console.log("Loaded tenants:", tenants);

        // BUG.4: the /tenants fetch can land after the user has navigated to
        // another route, which replaces #page-content — the <select> this
        // populates is already gone, so writing into it would throw a console
        // TypeError. Look the target up and bail quietly.
        const tenantSelector = document.getElementById('invitation-tenant');
        if (!tenantSelector) return;
        tenantSelectorContainer.style.display = 'block';
        console.log("Showing tenant selector for platform admin");

        tenantSelector.innerHTML = '<option value="">Select a tenant...</option>';
        tenants.forEach(tenant => {
            const option = document.createElement('option');
            option.value = tenant.id;
            option.textContent = `${tenant.name} (ID: ${tenant.id})`;
            tenantSelector.appendChild(option);
        });

        // Show instruction message for roles
        const roleSelector = document.getElementById('invitation-role');
        if (roleSelector) {
            roleSelector.innerHTML = '<option value="">Select a tenant first...</option>';
        }

        // Add event listener to reload roles and invitations when tenant changes
        tenantSelector.addEventListener('change', (e) => {
            const selectedTenantId = e.target.value;
            console.log("Tenant selected:", selectedTenantId);
            if (selectedTenantId) {
                loadRolesForInvitation(parseInt(selectedTenantId, 10));
                loadInvitations(); // Reload invitations for selected tenant
            } else {
                const roleSelector = document.getElementById('invitation-role');
                if (roleSelector) {
                    roleSelector.innerHTML = '<option value="">Select a tenant first</option>';
                }
                loadInvitations(); // Reload invitations (will show platform admin's own tenant)
            }
        });
    } catch (error) {
        console.error("Error loading tenants:", error);
        tenantSelectorContainer.style.display = 'none';
        // Load roles for regular user on error
        loadRolesForInvitation();
    }
}

async function loadPermissionTemplates() {
    const templateSelector = document.getElementById('invitation-template');
    if (!templateSelector) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/permissions/templates`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });
        if (!response.ok) throw new Error('Failed to load permission templates');

        const templates = await response.json();
        templateSelector.innerHTML = '<option value="">None - No initial folder/file access</option>';
        templates.forEach(template => {
            const option = document.createElement('option');
            option.value = template.id;
            option.textContent = `${template.name} (${template.permission_level}${template.duration_days ? ', ' + template.duration_days + ' days' : ''})`;
            option.dataset.description = template.description;
            templateSelector.appendChild(option);
        });
    } catch (error) {
        console.error("Error loading permission templates:", error);
    }
}

export async function initializeCogSelector() {
    const cogSelectorContainer = document.getElementById("cog-selector-container");
    if (!cogSelectorContainer) return;

    cogSelectorContainer.innerHTML = '<label class="block text-sm font-medium text-gray-700">Grant Initial Access To (Optional)</label>'; // Reset but keep label
    await createCogDropdown(null, 0);
}

async function createCogDropdown(parentId, level) {
    const cogSelectorContainer = document.getElementById("cog-selector-container");
    if (!cogSelectorContainer) return;

    try {
        // Filter folders by owner for non-super-admins
        let cogsResult;
        if (!isSuperAdmin && currentUser) {
            // Non-super-admins only see folders they own
            const params = new URLSearchParams({ owner_id: currentUser.id });
            if (parentId) params.append('parent_id', parentId);
            cogsResult = await apiCall(`/assets?${params.toString()}`);
        } else {
            // Super admins see all folders
            cogsResult = await cogs.get(parentId);
        }
        const select = document.createElement('select');
        select.className = 'cog-dropdown mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm';
        select.dataset.level = level;

        // Default option represents "All"
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'All';
        select.appendChild(placeholder);

        cogsResult.forEach(cog => {
            const option = document.createElement('option');
            option.value = cog.id;
            option.textContent = cog.name;
            option.dataset.isFolder = cog.cog_type.includes('folder');
            select.appendChild(option);
        });

        select.addEventListener('change', (e) => {
            const selectedOption = e.target.options[e.target.selectedIndex];
            const nextLevel = level + 1;
            document.querySelectorAll(`.cog-dropdown`).forEach(dropdown => {
                if (parseInt(dropdown.dataset.level) >= nextLevel) dropdown.remove();
            });
            if (selectedOption.dataset.isFolder === 'true' && selectedOption.value) {
                createCogDropdown(selectedOption.value, nextLevel);
            }
        });
        cogSelectorContainer.appendChild(select);
    } catch (error) {
        console.error("Error creating cog dropdown:", error);
    }
}

// ========================================
// Tenants Functions (Platform Admin Only)
// ========================================

async function loadTenants() {
    const tenantsTableBody = document.querySelector("#tenants-table tbody");
    if (!tenantsTableBody) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            throw new Error(`Failed to load tenants: ${response.status}`);
        }

        const tenants = await response.json();
        renderTenants(tenants);
    } catch (error) {
        console.error("Error loading tenants:", error);
        tenantsTableBody.innerHTML = `<tr><td colspan="5" class="text-center text-red-500 py-4">Could not load tenants: ${_escapeHtml(error.message)}</td></tr>`;
    }
}

function renderTenants(tenants) {
    const tenantsTableBody = document.querySelector("#tenants-table tbody");
    if (!tenantsTableBody) return;

    tenantsTableBody.innerHTML = '';
    if (tenants.length === 0) {
        tenantsTableBody.innerHTML = `<tr><td colspan="7" class="text-center text-gray-500 py-4">No tenants found.</td></tr>`;
        return;
    }

    tenants.forEach(tenant => {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50';

        // Format storage (bytes to MB/GB)
        const storage = tenant.storage_used_bytes || 0;
        let storageDisplay = '0 B';
        if (storage < 1024 * 1024) {
            storageDisplay = `${(storage / 1024).toFixed(2)} KB`;
        } else if (storage < 1024 * 1024 * 1024) {
            storageDisplay = `${(storage / (1024 * 1024)).toFixed(2)} MB`;
        } else {
            storageDisplay = `${(storage / (1024 * 1024 * 1024)).toFixed(2)} GB`;
        }

        // Format date
        const createdDate = tenant.created_at ? new Date(tenant.created_at).toLocaleDateString() : 'N/A';

        // GG.HQ.2 — Harbor storage quota override. null = plan-derived
        // (blank input), -1 = unlimited (∞ checked), >0 = exact cap in GB.
        const overrideBytes = tenant.harbor_storage_quota_override_bytes;
        const isUnlimited = overrideBytes === -1;
        const overrideGb = (overrideBytes != null && !isUnlimited)
            ? String(overrideBytes / (1024 * 1024 * 1024))
            : '';
        const overrideCell = `
            <div class="flex items-center gap-1">
                <input type="number" min="0" step="1" aria-label="Harbor storage override (GB)" class="harbor-override-gb w-20 px-2 py-1 text-sm border border-gray-300 rounded-md"
                    data-tenant-id="${tenant.id}" data-unlimited="false"
                    placeholder="plan" value="${overrideGb}" ${isUnlimited ? 'disabled' : ''}>
                <label class="flex items-center gap-1 text-xs text-gray-500 whitespace-nowrap" title="Unlimited Harbor storage for this tenant (∞)">
                    <input type="checkbox" aria-label="Unlimited Harbor storage" class="harbor-override-unlimited" data-tenant-id="${tenant.id}" ${isUnlimited ? 'checked' : ''}>∞
                </label>
                <button class="harbor-override-save text-primary hover:text-primary/80 text-sm font-medium"
                    data-tenant-id="${tenant.id}">Save</button>
            </div>
        `;

        // tenant id=1 is the platform tenant and can't be deleted — hide the
        // button for that row so it isn't misleading.
        const deleteBtn = tenant.id === 1
            ? '<span class="text-xs text-gray-400 italic">protected</span>'
            : `<button class="delete-tenant-button text-red-600 hover:text-red-800 font-medium" data-id="${tenant.id}" data-name="${_escapeHtml(tenant.name)}">Delete</button>`;

        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${tenant.id}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">${_escapeHtml(tenant.name)}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${tenant.credit_balance?.toLocaleString() || 0}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${storageDisplay}</td>
            <td class="px-6 py-4 whitespace-nowrap">${overrideCell}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">${createdDate}</td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm">${deleteBtn}</td>
        `;
        tenantsTableBody.appendChild(row);
    });

    // Delegate click handler for tenant-delete buttons.
    tenantsTableBody.querySelectorAll('.delete-tenant-button').forEach(btn => {
        btn.addEventListener('click', handleDeleteTenant);
    });

    // Harbor override: toggle the GB field's disabled state with ∞, and
    // save via the platform-admin endpoint.
    tenantsTableBody.querySelectorAll('.harbor-override-unlimited').forEach(cb => {
        cb.addEventListener('change', () => {
            const gb = tenantsTableBody.querySelector(
                `.harbor-override-gb[data-tenant-id="${cb.dataset.tenantId}"]`
            );
            if (cb.checked) { gb.value = ''; gb.disabled = true; }
            else { gb.disabled = false; }
        });
    });
    tenantsTableBody.querySelectorAll('.harbor-override-save').forEach(btn => {
        btn.addEventListener('click', () => handleHarborOverrideSave(btn));
    });
}

async function handleHarborOverrideSave(button) {
    const tenantId = button.dataset.tenantId;
    const tbody = button.closest('tbody');
    const gbInput = tbody.querySelector(
        `.harbor-override-gb[data-tenant-id="${tenantId}"]`
    );
    const unlimited = tbody.querySelector(
        `.harbor-override-unlimited[data-tenant-id="${tenantId}"]`
    );

    // ∞ checked → unlimited. Blank GB (and no ∞) → clear back to plan.
    let body;
    if (unlimited.checked) {
        body = { override_gb: null, unlimited: true };
    } else {
        const raw = gbInput.value.trim();
        if (raw === '') {
            body = { override_gb: null, unlimited: false };
        } else {
            const gb = Number(raw);
            if (!Number.isFinite(gb) || gb <= 0) {
                showError('Harbor storage override must be a positive number of GB (or leave blank for the plan default, or check ∞).');
                return;
            }
            body = { override_gb: Math.round(gb), unlimited: false };
        }
    }

    button.disabled = true;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const resp = await fetch(`${apiUrl}/tenants/${tenantId}/harbor-storage-quota-override`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        showInfo(`✅ Harbor storage override saved for tenant ${tenantId}.`);
        loadTenants();
    } catch (e) {
        showError(`Failed to save Harbor storage override: ${e.message}`);
    } finally {
        button.disabled = false;
    }
}

async function handleDeleteTenant(event) {
    const tenantId = event.currentTarget.getAttribute('data-id');
    const tenantName = event.currentTarget.getAttribute('data-name');
    const confirmed = await showConfirm(
        `Soft-delete tenant "${tenantName}"? All users in this tenant will also be soft-deleted (PII scrubbed, login disabled). The tenant row is kept for audit attribution per ISO 27001. Continue?`,
        'Delete tenant'
    );
    if (!confirmed) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const resp = await fetch(`${apiUrl}/tenants/${tenantId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }
        await loadTenants();
    } catch (e) {
        showError(`Failed to delete tenant: ${e.message}`);
    }
}

async function handleCreateTenant(event) {
    event.preventDefault();

    const tenantName = document.getElementById('tenant-name').value;
    const creditBalance = parseInt(document.getElementById('tenant-credits').value, 10);
    const submitBtn = document.querySelector('#create-tenant-form [type="submit"]');

    if (!tenantName) {
        showInfo("Please enter a tenant name.");
        return;
    }

    const formAction = async () => {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/tenants`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                name: tenantName,
                credit_balance: creditBalance
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || `Failed to create tenant: ${response.status}`);
        }

        const newTenant = await response.json();
        showInfo(`✅ Tenant "${newTenant.name}" created successfully!\n\nTenant ID: ${newTenant.id}\n\nYou can now invite users to this tenant from the Invitations tab.`);

        // Reload tenants and reset form
        loadTenants();
        document.getElementById('create-tenant-form').reset();

        // Also reload tenant selector in invitations tab
        loadTenantsForPlatformAdmin();
    };

    await withButtonBusy(submitBtn, formAction, { busyLabel: 'Creating…' });
}

// ========================================
// Custom Plans Functions (Platform Admin Only)
// ========================================

export async function loadTenantsForCustomPlans() {
    const tenantSelect = document.getElementById('custom-plan-tenant');
    if (!tenantSelect) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) return;

        const tenants = await response.json();
        tenantSelect.innerHTML = '<option value="">Select a tenant...</option>';
        tenants.forEach(tenant => {
            const option = document.createElement('option');
            option.value = tenant.id;
            option.textContent = `${tenant.name} (ID: ${tenant.id})`;
            tenantSelect.appendChild(option);
        });
    } catch (error) {
        console.error("Error loading tenants for custom plans:", error);
    }
}

export async function loadCustomPlans() {
    const customPlansTableBody = document.getElementById("custom-plans-tbody");
    if (!customPlansTableBody) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/plans?include_custom=true`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            throw new Error(`Failed to load custom plans: ${response.status}`);
        }

        const plans = await response.json();
        const customPlans = plans.filter(p => p.is_custom_plan);

        // Also load tenants to get tenant names
        const tenantsResponse = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });
        const tenants = tenantsResponse.ok ? await tenantsResponse.json() : [];

        renderCustomPlans(customPlans, tenants);
    } catch (error) {
        console.error("Error loading custom plans:", error);
        customPlansTableBody.innerHTML = `<tr><td colspan="6" class="text-center text-red-500 py-4">Could not load custom plans: ${_escapeHtml(error.message)}</td></tr>`;
    }
}

function renderCustomPlans(customPlans, tenants) {
    const customPlansTableBody = document.getElementById("custom-plans-tbody");
    if (!customPlansTableBody) return;

    customPlansTableBody.innerHTML = '';
    if (customPlans.length === 0) {
        customPlansTableBody.innerHTML = `<tr><td colspan="6" class="text-center text-gray-500 py-4">No custom plans created yet.</td></tr>`;
        return;
    }

    customPlans.forEach(plan => {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50';

        // Find tenant name
        const tenant = tenants.find(t => t.id === plan.custom_for_tenant_id);
        const tenantName = tenant ? tenant.name : `Tenant ${plan.custom_for_tenant_id}`;

        // Format price
        const price = plan.price_in_cents > 0 ? `$${(plan.price_in_cents / 100).toFixed(2)}/mo` : 'Free';

        // Format limits
        const limits = `${plan.max_seats} seats, ${formatStorage(plan.max_storage_gb)}, ${plan.credits_per_month.toLocaleString()} credits`;

        // A plan is assigned when its bound tenant's active subscription
        // points at this plan (tenant.current_plan_id comes from /tenants).
        const isAssigned = tenant && tenant.current_plan_id === plan.id;
        const statusBadge = isAssigned
            ? `<span class="px-2 py-1 text-xs rounded bg-green-100 text-green-700">Assigned</span>`
            : '<span class="px-2 py-1 text-xs rounded bg-gray-100 text-gray-700">Not Assigned</span>';

        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">${_escapeHtml(plan.name)}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${_escapeHtml(tenantName)}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-900">${price}</td>
            <td class="px-6 py-4 text-sm text-gray-600">${limits}</td>
            <td class="px-6 py-4 whitespace-nowrap text-sm">${statusBadge}</td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm space-x-2">
                <button class="assign-plan-button text-primary hover:text-primary/80 font-medium" data-plan-id="${plan.id}" data-tenant-id="${plan.custom_for_tenant_id ?? ''}" data-plan-name="${_escapeHtml(plan.name || '')}">${plan.custom_for_tenant_id ? 'Assign' : 'Assign to…'}</button>
                <button class="delete-plan-button text-red-600 hover:text-red-800 font-medium" data-plan-id="${plan.id}">Delete</button>
            </td>
        `;
        customPlansTableBody.appendChild(row);
    });
}

function formatStorage(gb) {
    if (gb >= 1024) {
        return `${(gb / 1024).toFixed(0)} TB`;
    }
    return `${gb} GB`;
}

async function handleCreateCustomPlan(event) {
    event.preventDefault();

    const payload = _collectCustomPlanFormPayload();
    if (!payload) return;

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/plans/custom`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            let body = null;
            try { body = await response.json(); } catch {}
            throw new Error(_formatApiError(body, `Failed to create custom plan: ${response.status}`));
        }

        const newPlan = await response.json();
        showInfo(`✅ Custom plan "${newPlan.name}" created successfully!\n\nPlan ID: ${newPlan.id}\n\nYou can now assign it to the tenant.`);

        // Reload plans and reset form
        loadCustomPlans();
        document.getElementById('create-custom-plan-form').reset();
    } catch (error) {
        console.error("Error creating custom plan:", error);
        showError(`Failed to create custom plan: ${error.message}`);
    }
}

// Read all the custom-plan form fields, validate them, and return the
// JSON payload ready for POST. Returns null (and surfaces a user-facing
// message) if validation fails.
//
// "Unlimited" checkboxes per cap-field translate to `null` in the
// payload — the API treats `null` as the unlimited sentinel and `0` as
// a real "zero allowance" cap (used to soft-suspend a tenant). Without
// the checkbox the user would have no way to express "no cap" since
// the input is required.
function _collectCustomPlanFormPayload() {
    const tenantId = parseInt(document.getElementById('custom-plan-tenant').value, 10);
    const name = document.getElementById('custom-plan-name').value;
    const tier = document.getElementById('custom-plan-tier').value;
    const priceInDollars = parseFloat(document.getElementById('custom-plan-price').value);
    const allowCustomRegistry = document.getElementById('custom-plan-registry').checked;
    const ssoEnabled = !!document.getElementById('custom-plan-sso')?.checked;
    const scimEnabled = !!document.getElementById('custom-plan-scim')?.checked;
    const description = document.getElementById('custom-plan-description').value;

    // Read cap fields as plain integers. The Unlimited checkboxes fill
    // the paired input with UNLIMITED_SENTINEL (1Q) before this runs,
    // so there's no "null" path — payload is always a clean int.
    const maxSeats = parseInt(document.getElementById('custom-plan-seats').value, 10);
    const maxStorageGb = parseInt(document.getElementById('custom-plan-storage').value, 10);
    const maxConcurrentProcesses = parseInt(document.getElementById('custom-plan-processes').value, 10);
    const creditsPerMonth = parseInt(document.getElementById('custom-plan-credits').value, 10);

    const missing = [];
    // Target tenant is optional — null means "template, assign later"
    if (!name) missing.push('Plan name');
    if (!Number.isFinite(priceInDollars)) missing.push('Price');
    if (!Number.isFinite(maxSeats)) missing.push('Max seats');
    if (!Number.isFinite(maxStorageGb)) missing.push('Max storage');
    if (!Number.isFinite(creditsPerMonth)) missing.push('Credits/month');
    if (!Number.isFinite(maxConcurrentProcesses)) missing.push('Max concurrent processes');
    if (!tier) missing.push('Commercial Tier');
    if (missing.length) {
        showInfo(`Please fill in: ${missing.join(', ')}.`);
        return null;
    }

    // Per-field upper bounds — must match the `le=...` ceilings on
    // PlanCreate in schemas.py (which cap inputs under Postgres int4).
    // Catching this client-side means the user gets a clear popup
    // naming the field + its max instead of a 422 round-trip.
    const PRICE_MAX_DOLLARS = 20_000_000;
    const tooLarge = [];
    if (Number.isFinite(priceInDollars) && priceInDollars > PRICE_MAX_DOLLARS) {
        tooLarge.push(`• Price — max $${PRICE_MAX_DOLLARS.toLocaleString()}/month (you entered $${priceInDollars.toLocaleString()})`);
    }
    // UNLIMITED_SENTINEL = 1Q. The Unlimited checkbox auto-fills this
    // exact value; anything above it is rejected (so a paste of a
    // typo'd 20-digit number still bounces).
    const CAP_MAX = 1_000_000_000_000_000;
    if (maxSeats > CAP_MAX) {
        tooLarge.push(`• Max Seats — max ${CAP_MAX.toLocaleString()} (you entered ${maxSeats.toLocaleString()}). Use the Unlimited checkbox.`);
    }
    if (maxStorageGb > CAP_MAX) {
        tooLarge.push(`• Max Storage — max ${CAP_MAX.toLocaleString()} GB (you entered ${maxStorageGb.toLocaleString()}). Use the Unlimited checkbox.`);
    }
    if (creditsPerMonth > CAP_MAX) {
        tooLarge.push(`• Credits/Month — max ${CAP_MAX.toLocaleString()} (you entered ${creditsPerMonth.toLocaleString()}). Use the Unlimited checkbox.`);
    }
    if (maxConcurrentProcesses > CAP_MAX) {
        tooLarge.push(`• Max Concurrent Processes — max ${CAP_MAX.toLocaleString()} (you entered ${maxConcurrentProcesses.toLocaleString()}). Use the Unlimited checkbox.`);
    }
    if (tooLarge.length) {
        showError(`Value${tooLarge.length === 1 ? '' : 's'} too large:\n\n${tooLarge.join('\n')}`);
        return null;
    }

    // Lower-bound check — should be caught by `min="0"` on the inputs,
    // but a paste / scripted value could slip past.
    const negative = [];
    if (Number.isFinite(priceInDollars) && priceInDollars < 0) negative.push('Price');
    if (maxSeats < 0) negative.push('Max Seats');
    if (maxStorageGb < 0) negative.push('Max Storage');
    if (creditsPerMonth < 0) negative.push('Credits/Month');
    if (maxConcurrentProcesses < 0) negative.push('Max Concurrent Processes');
    if (negative.length) {
        showError(`These fields can't be negative: ${negative.join(', ')}.`);
        return null;
    }

    return {
        name,
        tier,
        // null = template (assign later); a number = bound to that tenant.
        custom_for_tenant_id: Number.isFinite(tenantId) ? tenantId : null,
        price_in_cents: Math.round(priceInDollars * 100),
        max_seats: maxSeats,
        max_storage_gb: maxStorageGb,
        credits_per_month: creditsPerMonth,
        max_concurrent_processes: maxConcurrentProcesses,
        allow_custom_registry: allowCustomRegistry,
        // Identity & Access — include SSO/SCIM in a negotiated custom plan.
        sso_enabled: ssoEnabled,
        scim_enabled: scimEnabled,
        custom_description: description || null,
    };
}

// Wire up the "Unlimited" checkboxes so checking one fills the paired
// numeric input with UNLIMITED_SENTINEL (1 quadrillion). Backing
// columns are BIGINT post-migration so this sentinel can never
// overflow even at multi-datacenter scale. The API surface stays
// NULL-free (PlanCreate fields are all required ints) which removes
// "NULL = bypass billing" as an injection surface.
const _UNLIMITED_SENTINEL = '1000000000000000';
function _wireUnlimitedCheckboxes() {
    const pairs = [
        // [inputId, checkboxId]
        ['custom-plan-seats',     'custom-plan-seats-unlimited'],
        ['custom-plan-storage',   'custom-plan-storage-unlimited'],
        ['custom-plan-processes', 'custom-plan-processes-unlimited'],
        ['custom-plan-credits',   'custom-plan-credits-unlimited'],
    ];
    for (const [inputId, cbId] of pairs) {
        const input = document.getElementById(inputId);
        const cb = document.getElementById(cbId);
        if (!input || !cb || cb.dataset.wired === '1') continue;
        cb.dataset.wired = '1';
        const apply = () => {
            // When checked: stash the user's typed value, set the input
            // to the sentinel, and disable so they can't edit. Wire
            // payload reads input.value as a plain int — no null path.
            // When unchecked: restore their prior typed value (or empty)
            // and re-enable.
            input.disabled = cb.checked;
            if (cb.checked) {
                input.dataset.prevValue = input.value;
                input.value = _UNLIMITED_SENTINEL;
            } else {
                input.value = input.dataset.prevValue || '';
            }
        };
        cb.addEventListener('change', apply);
    }
}

async function handleCreateAndAssignPlan(event) {
    event.preventDefault();

    const payload = _collectCustomPlanFormPayload();
    if (!payload) return;
    const tenantId = payload.custom_for_tenant_id;
    // "Create & Assign" needs an actual target tenant — that's the
    // whole point of the button. Plain "Create" supports null
    // (template). Surfacing this here gives a clear popup instead
    // of the generic "Failed to assign" downstream.
    if (!Number.isFinite(tenantId)) {
        showInfo('Pick a Target Tenant before clicking "Create & Assign". Use the plain "Create Custom Plan" button if you want a reusable template.');
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

        // Step 1: Create the custom plan
        const createResponse = await fetch(`${apiUrl}/plans/custom`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload),
        });

        if (!createResponse.ok) {
            let body = null;
            try { body = await createResponse.json(); } catch {}
            throw new Error(_formatApiError(body, `Failed to create custom plan: ${createResponse.status}`));
        }

        const newPlan = await createResponse.json();

        // Step 2: Assign the plan to the tenant
        const assignResponse = await fetch(`${apiUrl}/plans/${newPlan.id}/assign/${tenantId}`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!assignResponse.ok) {
            let body = null;
            try { body = await assignResponse.json(); } catch {}
            throw new Error(_formatApiError(body, `Failed to assign plan: ${assignResponse.status}`));
        }

        showInfo(`✅ Custom plan "${newPlan.name}" created and assigned to tenant successfully!\n\nThe tenant now has an active subscription with this custom plan.`);

        // Reload plans and reset form
        loadCustomPlans();
        document.getElementById('create-custom-plan-form').reset();
    } catch (error) {
        console.error("Error creating and assigning custom plan:", error);
        showError(`Failed to create and assign custom plan: ${error.message}`);
    }
}

// Modal picker for the "assign this template plan to a tenant" flow.
// Resolves to the picked tenant id (number) or null on cancel.
async function _promptTenantForAssignment(planLabel) {
    let tenants = [];
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const resp = await fetch(`${apiUrl}/tenants`, {
            headers: { 'Authorization': `Bearer ${sessionStorage.getItem('accessToken')}` },
        });
        if (resp.ok) tenants = await resp.json();
    } catch (e) {
        console.error('Failed to load tenants for assign picker:', e);
    }
    if (!tenants.length) {
        showError('No tenants available to assign this plan to.');
        return null;
    }

    return await new Promise((resolve) => {
        const backdrop = document.createElement('div');
        backdrop.className = 'fixed inset-0 z-50 bg-black/40 flex items-center justify-center';
        backdrop.innerHTML = `
            <div class="bg-white rounded-lg shadow-xl w-full max-w-md p-6 m-4" role="dialog" aria-modal="true">
                <h3 class="text-lg font-semibold text-gray-800 mb-2">Assign ${planLabel}</h3>
                <p class="text-xs text-gray-600 mb-4">Pick a tenant. This creates an active subscription on this plan. Tenants already on a custom plan will get rejected by the API; clear the existing custom plan first if you need to replace it.</p>
                <select aria-label="Tenant" class="assign-picker-select w-full border border-gray-300 rounded-md px-3 py-2 text-sm mb-4">
                    <option value="">Select a tenant…</option>
                    ${tenants.map(t => `<option value="${t.id}">${t.name} (ID: ${t.id})</option>`).join('')}
                </select>
                <div class="flex justify-end gap-2">
                    <button class="assign-picker-cancel px-4 py-2 text-sm border border-gray-300 rounded-md hover:bg-gray-50">Cancel</button>
                    <button class="assign-picker-ok px-4 py-2 text-sm bg-primary text-white rounded-md hover:bg-primary/90 disabled:opacity-50" disabled>Assign</button>
                </div>
            </div>
        `;
        document.body.appendChild(backdrop);

        const select = backdrop.querySelector('.assign-picker-select');
        const okBtn = backdrop.querySelector('.assign-picker-ok');
        const cancelBtn = backdrop.querySelector('.assign-picker-cancel');
        const cleanup = () => backdrop.remove();

        select.addEventListener('change', () => {
            okBtn.disabled = !select.value;
        });
        okBtn.addEventListener('click', () => {
            const v = parseInt(select.value, 10);
            cleanup();
            resolve(Number.isFinite(v) ? v : null);
        });
        cancelBtn.addEventListener('click', () => { cleanup(); resolve(null); });
        backdrop.addEventListener('click', (e) => {
            if (e.target === backdrop) { cleanup(); resolve(null); }
        });
    });
}

async function handleAssignPlanToTenant(event) {
    const planId = parseInt(event.target.getAttribute('data-plan-id'), 10);
    let tenantId = parseInt(event.target.getAttribute('data-tenant-id'), 10);
    const planName = event.target.getAttribute('data-plan-name') || `plan ${planId}`;

    if (!Number.isFinite(planId)) {
        showError('Bad plan reference — refresh the page and try again.');
        return;
    }

    // Template plan (no fixed target tenant) → prompt for one. This is
    // the "negotiated once, applied to several tenants under the same
    // customer" workflow. A bound plan skips the prompt and confirms.
    if (!Number.isFinite(tenantId)) {
        const picked = await _promptTenantForAssignment(planName);
        if (picked == null) return;     // user cancelled
        tenantId = picked;
    } else {
        if (!await showConfirm(`Assign "${planName}" to tenant ${tenantId}? This creates an active subscription.`)) {
            return;
        }
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/plans/${planId}/assign/${tenantId}`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            let body = null;
            try { body = await response.json(); } catch {}
            throw new Error(_formatApiError(body, `Failed to assign plan: ${response.status}`));
        }

        showInfo('✅ Plan assigned to tenant successfully!');
        loadCustomPlans(); // Refresh the list
    } catch (error) {
        console.error("Error assigning plan to tenant:", error);
        showError(`Failed to assign plan: ${error.message}`);
    }
}

async function handleDeleteCustomPlan(event) {
    const planId = parseInt(event.target.getAttribute('data-plan-id'), 10);

    if (!await showConfirm('⚠️ WARNING: This will permanently delete this custom plan. This cannot be undone if no subscriptions are using it. Continue?')) {
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/plans/${planId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            let body = null;
            try { body = await response.json(); } catch {}
            throw new Error(_formatApiError(body, `Failed to delete plan: ${response.status}`));
        }

        showInfo('✅ Custom plan deleted successfully!');
        loadCustomPlans(); // Refresh the list
    } catch (error) {
        console.error("Error deleting custom plan:", error);
        showError(`Failed to delete plan: ${error.message}`);
    }
}

// ========================================
// Roles Management Functions (with Granular Capabilities)
// ========================================

let allRoles = [];
let allCapabilities = []; // Grouped capabilities from API
let rolesListenersInitialized = false;

function initializeRolesListeners() {
    if (rolesListenersInitialized) return;
    rolesListenersInitialized = true;

    // Create role form
    const createRoleForm = document.getElementById('create-role-form');
    if (createRoleForm) {
        createRoleForm.addEventListener('submit', handleCreateRole);
    }

    // Refresh button
    const refreshBtn = document.getElementById('refresh-roles-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadRolesForPanel);
    }

    // Edit modal buttons
    document.getElementById('close-edit-role-modal')?.addEventListener('click', closeEditRoleModal);
    document.getElementById('cancel-edit-role')?.addEventListener('click', closeEditRoleModal);

    const editRoleForm = document.getElementById('edit-role-form');
    if (editRoleForm) {
        editRoleForm.addEventListener('submit', handleUpdateRole);
    }
}

function closeEditRoleModal() {
    document.getElementById('edit-role-modal').classList.add('hidden');
}

async function loadRolesForPanel() {
    initializeRolesListeners();

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

        // Load roles and capabilities in parallel
        const [rolesResponse, capabilitiesResponse] = await Promise.all([
            fetch(`${apiUrl}/rbac/roles`, {
                headers: {
                    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                    'Content-Type': 'application/json'
                }
            }),
            fetch(`${apiUrl}/capabilities/grouped`, {
                headers: {
                    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                    'Content-Type': 'application/json'
                }
            })
        ]);

        if (!rolesResponse.ok) {
            throw new Error(`Failed to load roles: ${rolesResponse.status}`);
        }

        const roles = await rolesResponse.json();
        allRoles = roles;

        if (capabilitiesResponse.ok) {
            const capabilities = await capabilitiesResponse.json();
            allCapabilities = capabilities;
            renderCapabilitiesSelector('capabilities-container', []);
        }

        renderRoles();
    } catch (error) {
        console.error('Error loading roles:', error);
        const tbody = document.getElementById('roles-tbody');
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-4 text-center text-red-500">Error loading roles: ${_escapeHtml(error.message)}</td></tr>`;
        }
    }
}

function renderCapabilitiesSelector(containerId, selectedCapabilityIds) {
    const container = document.getElementById(containerId);
    if (!container) return;

    if (allCapabilities.length === 0) {
        container.innerHTML = '<p class="text-gray-500 text-sm p-4">No capabilities available</p>';
        return;
    }

    container.innerHTML = '';

    allCapabilities.forEach((group, groupIndex) => {
        const resourceType = group.resource_type;
        const displayName = group.resource_type_display;
        const categories = group.categories;

        // Create collapsible section for each resource type
        const section = document.createElement('div');
        section.className = 'border-b border-gray-200 last:border-b-0';

        // Count selected in this group
        let selectedCount = 0;
        let totalCount = 0;
        Object.values(categories).forEach(caps => {
            totalCount += caps.length;
            caps.forEach(cap => {
                if (selectedCapabilityIds.includes(cap.id)) selectedCount++;
            });
        });

        section.innerHTML = `
            <button type="button" class="capability-group-toggle w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-50 focus:outline-none" data-group="${resourceType}">
                <div class="flex items-center">
                    <svg class="capability-chevron w-5 h-5 text-gray-400 mr-2 transform transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7"></path>
                    </svg>
                    <span class="font-medium text-gray-900">${_escapeHtml(displayName)}</span>
                </div>
                <span class="text-xs text-gray-500">${selectedCount}/${totalCount} selected</span>
            </button>
            <div class="capability-group-content hidden px-4 pb-4" data-group="${resourceType}">
                <!-- Categories will be added here -->
            </div>
        `;

        const contentDiv = section.querySelector('.capability-group-content');

        // Add categories
        Object.entries(categories).forEach(([categoryName, caps]) => {
            const categoryDiv = document.createElement('div');
            categoryDiv.className = 'mt-3';
            categoryDiv.innerHTML = `
                <div class="flex items-center justify-between mb-2">
                    <h4 class="text-sm font-medium text-gray-700">${_escapeHtml(categoryName)}</h4>
                    <button type="button" class="select-all-category text-xs text-green-600 hover:text-green-700" data-category="${resourceType}-${_escapeHtml(categoryName)}">
                        Select All
                    </button>
                </div>
                <div class="grid grid-cols-1 md:grid-cols-2 gap-2" data-category-container="${resourceType}-${_escapeHtml(categoryName)}">
                </div>
            `;

            const capsContainer = categoryDiv.querySelector('[data-category-container]');

            caps.forEach(cap => {
                const isChecked = selectedCapabilityIds.includes(cap.id);
                const label = document.createElement('label');
                label.className = 'flex items-start p-2 bg-white rounded border border-gray-200 hover:bg-gray-50 cursor-pointer text-sm';
                label.innerHTML = `
                    <input aria-label="Capability: ${_escapeHtml(cap.display_name)}" type="checkbox" name="capability" value="${cap.id}" ${isChecked ? 'checked' : ''}
                           class="mt-0.5 mr-2 text-green-600 focus:ring-green-500 rounded" data-cap-id="${cap.id}">
                    <div>
                        <span class="text-gray-900">${_escapeHtml(cap.display_name)}</span>
                        ${cap.description ? `<p class="text-xs text-gray-500 mt-0.5">${_escapeHtml(cap.description)}</p>` : ''}
                        ${cap.is_sensitive ? '<span class="inline-block mt-1 px-1.5 py-0.5 text-xs bg-red-100 text-red-700 rounded">Sensitive</span>' : ''}
                    </div>
                `;
                capsContainer.appendChild(label);
            });

            contentDiv.appendChild(categoryDiv);
        });

        container.appendChild(section);
    });

    // Add toggle listeners
    container.querySelectorAll('.capability-group-toggle').forEach(toggle => {
        toggle.addEventListener('click', () => {
            const group = toggle.dataset.group;
            const content = container.querySelector(`.capability-group-content[data-group="${group}"]`);
            const chevron = toggle.querySelector('.capability-chevron');

            content.classList.toggle('hidden');
            chevron.classList.toggle('rotate-90');
        });
    });

    // Add select all listeners
    container.querySelectorAll('.select-all-category').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const category = btn.dataset.category;
            const categoryContainer = container.querySelector(`[data-category-container="${category}"]`);
            const checkboxes = categoryContainer.querySelectorAll('input[type="checkbox"]');
            const allChecked = Array.from(checkboxes).every(cb => cb.checked);

            checkboxes.forEach(cb => {
                cb.checked = !allChecked;
            });

            btn.textContent = allChecked ? 'Select All' : 'Deselect All';
            updateCapabilityCount(container);
        });
    });

    // Update counts when checkboxes change
    container.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', () => updateCapabilityCount(container));
    });
}

function updateCapabilityCount(container) {
    container.querySelectorAll('.capability-group-toggle').forEach(toggle => {
        const group = toggle.dataset.group;
        const content = container.querySelector(`.capability-group-content[data-group="${group}"]`);
        const checkboxes = content.querySelectorAll('input[type="checkbox"]');
        const checkedCount = Array.from(checkboxes).filter(cb => cb.checked).length;
        const totalCount = checkboxes.length;

        const countSpan = toggle.querySelector('span:last-child');
        countSpan.textContent = `${checkedCount}/${totalCount} selected`;
    });
}

function renderRoles() {
    const tbody = document.getElementById('roles-tbody');
    if (!tbody) return;

    if (allRoles.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" class="px-6 py-4 text-center text-gray-500">No roles found. The built-in roles (Admin, Standard User, Guest, External Collaborator) are assigned by your administrator.</td></tr>`;
        return;
    }

    tbody.innerHTML = '';
    allRoles.forEach(role => {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50';

        // Format capabilities as badges (show every capability, no truncation)
        const capabilities = role.capabilities || [];
        let capabilityBadges = '';
        if (capabilities.length === 0) {
            capabilityBadges = '<span class="text-gray-400">No capabilities</span>';
        } else {
            capabilityBadges = capabilities
                .map(c => `<span class="inline-block px-2 py-1 text-xs bg-primary text-white rounded mr-1 mb-1">${_escapeHtml(c.display_name || c.name)}</span>`)
                .join('');
        }

        row.innerHTML = `
            <td class="px-6 py-4 whitespace-nowrap text-sm font-medium text-gray-900">${_escapeHtml(role.name)}</td>
            <td class="px-6 py-4 text-sm text-gray-600">${_escapeHtml(role.description || '-')}</td>
            <td class="px-6 py-4 text-sm">
                <div class="flex flex-wrap max-w-md">
                    ${capabilityBadges}
                </div>
            </td>
            <td class="px-6 py-4 whitespace-nowrap text-right text-sm space-x-2">
                <button class="edit-role-btn text-green-600 hover:text-green-800 font-medium" data-role-id="${role.id}">Edit</button>
                <button class="delete-role-btn text-red-600 hover:text-red-800 font-medium" data-role-id="${role.id}">Delete</button>
            </td>
        `;

        // Attach event listeners
        row.querySelector('.edit-role-btn').addEventListener('click', () => openEditRoleModal(role));
        row.querySelector('.delete-role-btn').addEventListener('click', () => deleteRole(role.id));

        tbody.appendChild(row);
    });
}

async function handleCreateRole(event) {
    event.preventDefault();

    const name = document.getElementById('role-name').value.trim();
    const description = document.getElementById('role-description').value.trim();

    if (!name) {
        showInfo('Please enter a role name');
        return;
    }

    // Get selected capabilities
    const checkboxes = document.querySelectorAll('#capabilities-container input[name="capability"]:checked');
    const capabilityIds = Array.from(checkboxes).map(cb => parseInt(cb.value));

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

        // Step 1: Create the role
        const createResponse = await fetch(`${apiUrl}/rbac/roles`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                name,
                description: description || null,
                capability_ids: capabilityIds
            })
        });

        if (!createResponse.ok) {
            const error = await createResponse.json();
            throw new Error(_formatApiError(error, `Failed to create role: ${createResponse.status}`));
        }

        const newRole = await createResponse.json();

        // Step 2: Assign capabilities to the role
        if (capabilityIds.length > 0) {
            const capResponse = await fetch(`${apiUrl}/roles/${newRole.id}/capabilities`, {
                method: 'PUT',
                headers: {
                    'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    capability_ids: capabilityIds
                })
            });

            if (!capResponse.ok) {
                console.warn('Failed to assign capabilities to role, but role was created');
            }
        }

        showInfo('✅ Role created successfully!');

        // Reset form
        document.getElementById('create-role-form').reset();
        renderCapabilitiesSelector('capabilities-container', []);

        // Reload roles
        loadRolesForPanel();
    } catch (error) {
        console.error('Error creating role:', error);
        showError(`Failed to create role: ${error.message}`);
    }
}

async function openEditRoleModal(role) {
    document.getElementById('edit-role-id').value = role.id;
    document.getElementById('edit-role-name').value = role.name;
    document.getElementById('edit-role-description').value = role.description || '';

    // Load capabilities for this role
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/roles/${role.id}/capabilities`, {
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        let currentCapabilityIds = [];
        if (response.ok) {
            const capabilities = await response.json();
            currentCapabilityIds = capabilities.map(c => c.id);
        }

        renderCapabilitiesSelector('edit-capabilities-container', currentCapabilityIds);
    } catch (error) {
        console.error('Error loading role capabilities:', error);
        // Fallback to using role.capabilities if available
        const currentCapabilityIds = (role.capabilities || []).map(c => c.id);
        renderCapabilitiesSelector('edit-capabilities-container', currentCapabilityIds);
    }

    document.getElementById('edit-role-modal').classList.remove('hidden');
}

async function handleUpdateRole(event) {
    event.preventDefault();

    const roleId = document.getElementById('edit-role-id').value;
    const name = document.getElementById('edit-role-name').value.trim();
    const description = document.getElementById('edit-role-description').value.trim();

    if (!name) {
        showInfo('Please enter a role name');
        return;
    }

    // Get selected capabilities
    const checkboxes = document.querySelectorAll('#edit-capabilities-container input[name="capability"]:checked');
    const capabilityIds = Array.from(checkboxes).map(cb => parseInt(cb.value));

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

        // Step 1: Update role name/description
        const updateResponse = await fetch(`${apiUrl}/rbac/roles/${roleId}`, {
            method: 'PATCH',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                name,
                description: description || null
            })
        });

        if (!updateResponse.ok) {
            const error = await updateResponse.json();
            throw new Error(_formatApiError(error, `Failed to update role: ${updateResponse.status}`));
        }

        // Step 2: Update capabilities
        const capResponse = await fetch(`${apiUrl}/roles/${roleId}/capabilities`, {
            method: 'PUT',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                capability_ids: capabilityIds
            })
        });

        if (!capResponse.ok) {
            console.warn('Failed to update capabilities, but role was updated');
        }

        showInfo('✅ Role updated successfully!');
        closeEditRoleModal();
        loadRolesForPanel();
    } catch (error) {
        console.error('Error updating role:', error);
        showError(`Failed to update role: ${error.message}`);
    }
}

async function deleteRole(roleId) {
    if (!await showConfirm('Are you sure you want to delete this role? Users with this role will need to be assigned a new role.')) {
        return;
    }

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const response = await fetch(`${apiUrl}/rbac/roles/${roleId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${sessionStorage.getItem("accessToken")}`,
                'Content-Type': 'application/json'
            }
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(_formatApiError(error, `Failed to delete role: ${response.status}`));
        }

        showInfo('✅ Role deleted successfully!');
        loadRolesForPanel();
    } catch (error) {
        console.error('Error deleting role:', error);
        showError(`Failed to delete role: ${error.message}`);
    }
}


// ---------------------------------------------------------------------
// Signup Health — relocated from the System page into the Users /
// Invitations panel (after Pending Invitations). Platform-admin only.
// Lists accounts whose welcome email never landed; Resend re-runs the
// send path. Source: /platform/signup-orphans.
// ---------------------------------------------------------------------
function _shEsc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// CE: panel hidden (route is paid-only, manifest.yaml:1552)
async function maybeLoadSignupHealth() {
    const section = document.getElementById('signup-health-section');
    if (!section) return;
    try {
        if (!(await isPaidEdition())) { section.style.display = 'none'; return; }
        const u = await me.get();
        if (!u || u.is_platform_admin !== true) { section.style.display = 'none'; return; }
        section.style.display = '';
        loadSignupOrphansPanel();
    } catch (_) {
        section.style.display = 'none';
    }
}

async function loadSignupOrphansPanel() {
    const tbody = document.getElementById('signup-orphans-tbody');
    const countEl = document.getElementById('signup-orphans-count');
    const refreshBtn = document.getElementById('signup-orphans-refresh');
    if (!tbody) return;

    if (refreshBtn && !refreshBtn.dataset.wired) {
        refreshBtn.dataset.wired = '1';
        refreshBtn.addEventListener('click', loadSignupOrphansPanel);
    }

    tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-sm text-gray-500">Loading…</td></tr>';

    let data;
    try {
        data = await apiCall('/platform/signup-orphans');
    } catch (err) {
        console.warn('signup-orphans fetch failed', err);
        tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-6 text-center text-sm text-amber-700">Couldn't load — ${_shEsc(err.message || err)}</td></tr>`;
        return;
    }

    const items = (data && data.items) || [];
    if (countEl) countEl.textContent = items.length;

    if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-sm text-gray-500">No orphan signups. 🎉</td></tr>';
        return;
    }

    tbody.innerHTML = items.map(row => {
        const name = [row.first_name, row.last_name].filter(Boolean).join(' ') || '—';
        const ageText = row.age_hours >= 24
            ? `${(row.age_hours / 24).toFixed(1)}d`
            : `${row.age_hours.toFixed(1)}h`;
        const errCell = row.welcome_email_error
            ? `<span class="text-xs text-red-700">${_shEsc(row.welcome_email_error.slice(0, 120))}</span>`
            : '<span class="text-xs text-gray-400">—</span>';
        return `
            <tr data-user-id="${row.user_id}">
                <td class="px-4 py-3 text-sm font-mono text-gray-800">${_shEsc(row.email)}</td>
                <td class="px-4 py-3 text-sm text-gray-600">${_shEsc(name)}</td>
                <td class="px-4 py-3 text-sm text-gray-600">${_shEsc(row.tenant_name || `tenant-${row.tenant_id}`)}</td>
                <td class="px-4 py-3 text-sm text-gray-600">${ageText}</td>
                <td class="px-4 py-3">${errCell}</td>
                <td class="px-4 py-3 text-right">
                    <button class="signup-orphan-resend px-3 py-1 text-xs bg-primary text-white rounded hover:bg-primary/90"
                            data-user-id="${row.user_id}">
                        Resend
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    tbody.querySelectorAll('.signup-orphan-resend').forEach(btn => {
        btn.addEventListener('click', async () => {
            const userId = btn.dataset.userId;
            const originalText = btn.textContent;
            btn.disabled = true;
            btn.textContent = 'Sending…';
            try {
                const res = await apiCall(`/platform/signup-orphans/${userId}/resend`, { method: 'POST' });
                if (res && res.ok) {
                    btn.textContent = 'Sent ✓';
                    setTimeout(() => loadSignupOrphansPanel(), 800);
                } else {
                    btn.textContent = 'Failed';
                    btn.title = (res && res.error) || 'Unknown error';
                    btn.disabled = false;
                    setTimeout(() => { btn.textContent = originalText; }, 3000);
                }
            } catch (err) {
                btn.textContent = 'Failed';
                btn.title = err.message || String(err);
                btn.disabled = false;
                setTimeout(() => { btn.textContent = originalText; }, 3000);
            }
        });
    });
}
