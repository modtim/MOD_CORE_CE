// Dashboard/js/platform-settings.js
//
// GS.PSR.4 — Dashboard Admin > Platform settings page. Renders the
// DB-backed platform-settings registry (GET /platform/settings) grouped by
// `group`, with typed inputs (bool → toggle, int/float → number, str → text,
// json → textarea with parse check), a source badge (DB / ENV / DEFAULT), a
// "restart required" tag, per-row Save (PATCH) and "Revert to default"
// (DELETE, only when source === 'db'). Inline errors (including the 422
// validator message) and a success toast. Platform-admin only — the server
// gates every endpoint; the tab is also hidden for non-admins (analytics.js).

import { fetchApi as apiCall, isPaidEdition, me } from './api.js';

// FIX-MFA-HIDE (owner Q-MFA = A, 11-10: "hide the inert admin MFA setting on
// CE now; paid unchanged") — the three admin-MFA platform settings are INERT
// on CE, so their rows must not render there. They are NOT a Dashboard-only
// dead end: the CE tree still registers them (CORE/API/services/platform_settings.py
// :2853 auth.mfa_required_for_admins, :2865 auth.mfa_required_for_all, :2875
// auth.mfa_enforcement_enabled) and keycloak-init.sh:1421-1422 mirrors the two
// scope keys into realm-side env defaults, so a CE operator who flipped one
// changed the Keycloak browser flow OUTSIDE MOD (the OTP step then prompts at
// sign-in while every MOD-side consumer of the setting is absent) — a setting
// that does nothing the Dashboard can show, and a half-applied realm change.
// Every CE-side reader is stripped or guarded:
//   services/mfa_service.py:161-162 (GET /me/mfa `required`) — the module SHIPS
//     in CE (open-core/manifest.yaml:130 api_allow), so this is the ONE place a
//     CE value could still reach the UI: the account page's "required" line and
//     the login gate's MFA_ENROLLMENT_REQUIRED text (auth.js:428) follow it.
//   services/compliance_gate_task_start.py:95-100 (the enforced-MFA login gate)
//     — manifest:1329-… denies it; routers/auth.py:93 reaches it through
//     optional_paid and the CE fallback is {"forced": False} (ALLOW, a no-op).
//   services/mfa_enforcement.py:207/:214 (the FX.53 sweep + countdown) —
//     manifest:1329 denies it; platform_settings.py:1028 `_on_mfa_enforcement_after_commit`
//     optional_paid-skips with "SKIPPED (paid module absent in CE)".
//   services/conformance/checks.py:366 (the conformance check) — manifest:2167
//     denies services/conformance/**.
//   services/soc2_evidence.py:270/:275 — manifest:2220 denies it.
// So on CE the rows are hidden (never a Save/Revert affordance for a switch that
// cannot take effect); on paid the rows render exactly as before, untouched.
const _INERT_ON_CE_SETTING_KEYS = new Set([
    'auth.mfa_required_for_admins',   // OTP enrollment for platform admins
    'auth.mfa_required_for_all',      // OTP enrollment for all realm users
    'auth.mfa_enforcement_enabled',   // FX.53 enforcement sweep master switch
    'auth.phishing_resistant_only',   // phishing-resistant-only sign-in: no enforcement site on CE (owner 11-12: hide)
    // Paid-only quantity caps and plan wording: CE has no quantity caps and no
    // plans (by design). Each is paid-only by its own registry description, and
    // its module is denied in CE (manifest.yaml:1656 billing*.py, :1666 monthly
    // billing processor). Hidden on CE; paid keeps the rows unchanged.
    'api.rate_limit.plan_limits',           // FX.106 per-PLAN requests/minute caps
    'llm.monthly_token_quota',              // PAID only: CE is not metered
    'billing.payment_grace_days_monthly',   // downgrade-to-free-tier grace (monthly plans)
    'billing.payment_grace_days_annual',    // downgrade-to-free-tier grace (annual plans)
    'billing.overage_restrict_enabled',     // post-downgrade plan-limit enforcement
    // FIX-CE-INERT-SETTING-KEYS (SWEEP-07 leg, 11-12) — the 20 UI-class rows
    // SWEEP-07-SETTINGS-REGISTRY found: each registers in this (shipped) registry,
    // but EVERY module that acts on it is manifest-denied in CE
    // (run_costing/billing_retention/cancellation_lifecycle/node_marketplace_paid/
    // listing_review/gov_mod_built_only/compliance_profiles/managed_ai_stacks/
    // mesh_acl/headscale_client/mesh_public_vhost_nginx/sso_requirement/
    // sso_break_glass/license_expiry_check/export_approval/assistant_gov_models/
    // platform_backup_runner all deny; the allow-side mentions in
    // worker_tasks.py, scheduler.py, main.py, worker_management.py,
    // persistent_stacks/_shared.py and models.py are comments/strings behind an
    // optional_paid() guard that is None on CE). So on CE the toggle does nothing
    // = a dead paid row the user can still edit; hidden here (CE-conditional by
    // design: _psrVisibleSettings skips the filter on paid, so paid rows and
    // paid behaviour stay EXACTLY as before - this file ships to paid too, shared).
    // Six SWEEP-07 UI rows are DELIBERATELY NOT here: compliance.declared_environment
    // and compliance.profiles have shipped CE readers (main.py resolve_source;
    // node_egress_approval.py get_setting), and the support set
    // (support.remote_session_enabled / support.remote_session_max_hours /
    // support_tickets.response_targets / oncall.sev1_ticket_paging_enabled) rides
    // the PROOF-CE-DENY NEEDS-GUARD set whose modules (support_session.py /
    // support_tickets.py) are api_allow.
    'billing_basis',                        // run-costing basis (run_costing.py deny)
    'billing.record_retention_years',       // billing retention (billing_retention.py deny)
    'billing.cancellation_export_days',     // cancellation window (cancellation_lifecycle.py deny)
    'node_trial_rate_limit_per_hour',       // marketplace trial rate (node_marketplace_paid.py deny)
    'marketplace.report_autohold_threshold', // listing review (listing_review.py deny)
    'marketplace.mod_built_only_enforced',  // gov mod-built gate (gov_mod_built_only.py deny)
    'compliance.disruptive_grace_days',     // compliance profiles (compliance_profiles.py deny)
    'compliance.grace_deadlines',           // same family (compliance_profiles.py deny)
    'stacks.share_platform_hf_token',       // managed-AI stacks (managed_ai_stacks.py deny)
    'mesh.acl_enforced',                    // mesh ACL (mesh_acl.py deny)
    'mesh.preauth_key_ttl_minutes',         // headscale client (headscale_client.py deny)
    'mesh.public_control_plane',            // mesh public vhost (mesh_public_vhost_nginx.py deny)
    'auth.sso_requirement_enabled',         // SSO REQUIRE enforcement (sso_requirement.py deny)
    'auth.require_sso',                     // same enforcement (sso_requirement.py deny)
    'auth.require_sso_break_glass_user_ids', // break-glass list (sso_break_glass.py deny)
    'license.last_known_state',             // licence server (license_expiry_check.py deny)
    'license.expiry_warn_days',             // same family (license_expiry_check.py deny)
    'dlp.export_approval_profiles',         // export approvals (export_approval.py deny)
    'assistant.gov_local_models_only',      // gov-model policy (assistant_gov_models.py deny)
    'backup.retention_days',                // platform backups (platform_backup_runner.py deny)
]);

let _psrSettings = [];

// ---------------------------------------------------------------------------
// HTML helpers
// ---------------------------------------------------------------------------

function _psrEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _psrSourceBadge(source) {
    const styles = {
        db: 'bg-primary/15 text-primary',
        env: 'bg-amber-100 text-amber-800',
        default: 'bg-gray-100 text-gray-600',
    };
    const title = {
        db: 'Currently stored in the database (overrides env/default)',
        env: 'Currently resolved from an environment variable',
        default: 'Currently resolved from the registry default',
    }[source] || '';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-medium ${styles[source] || styles.default}" title="${_psrEsc(title)}">${_psrEsc((source || 'default').toUpperCase())}</span>`;
}

function _psrValueText(v) {
    if (v === null || v === undefined) return '';
    return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

function _psrInput(s) {
    const key = _psrEsc(s.key);
    const val = s.value;
    if (s.type === 'bool') {
        return `
            <label class="relative inline-flex items-center cursor-pointer" title="Toggle (Save applies the change)">
                <input type="checkbox" aria-label="Toggle ${key}" class="psr-input sr-only peer" data-psr-key="${key}" data-psr-type="bool" ${val ? 'checked' : ''}>
                <div class="w-11 h-6 bg-gray-200 rounded-full peer-checked:bg-primary transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-transform peer-checked:after:translate-x-5"></div>
            </label>`;
    }
    if (s.type === 'int' || s.type === 'float') {
        return `<input aria-label="Value for ${_psrEsc(s.key)}" type="number" class="psr-input px-3 py-2 text-sm border border-gray-300 rounded-lg w-40" data-psr-key="${key}" data-psr-type="${_psrEsc(s.type)}" value="${_psrEsc(_psrValueText(val))}" ${s.type === 'int' ? 'step="1"' : 'step="any"'}>`;
    }
    if (s.type === 'json') {
        const text = typeof val === 'string' ? val : JSON.stringify(val, null, 2);
        return `<textarea aria-label="Value for ${_psrEsc(s.key)}" class="psr-input px-3 py-2 text-sm border border-gray-300 rounded-lg w-96 font-mono" data-psr-key="${key}" data-psr-type="json" rows="3">${_psrEsc(text)}</textarea>`;
    }
    // str — the API does not expose a choices list, so a validated str
    // renders as a text input (the server validator rejects bad values).
    return `<input aria-label="Value for ${_psrEsc(s.key)}" type="text" class="psr-input px-3 py-2 text-sm border border-gray-300 rounded-lg w-56" data-psr-key="${key}" data-psr-type="str" value="${_psrEsc(_psrValueText(val))}">`;
}

function _psrRow(s) {
    const key = _psrEsc(s.key);
    const restartTag = s.restart_required
        ? `<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-orange-100 text-orange-800" title="Changing this value requires an API restart to take full effect">restart required</span>`
        : '';
    const overrides = Array.isArray(s.tenant_overrides) && s.tenant_overrides.length
        ? `<div class="mt-1 text-xs text-amber-700">Tenant overrides: ${s.tenant_overrides.map(t => `#${t}`).join(', ')}</div>`
        : '';
    const metaBits = [`default: <code class="text-xs text-gray-500">${_psrEsc(_psrValueText(s.default))}</code>`];
    if (s.env_fallback) metaBits.push(`env: <code class="text-xs text-gray-500">${_psrEsc(s.env_fallback)}</code>`);
    const revertBtn = s.source === 'db'
        ? `<button type="button" data-psr-act="revert" data-psr-key="${key}" class="text-sm text-red-600 hover:text-red-800">Revert to default</button>`
        : '';
    return `
        <div class="psr-row py-4 border-b border-gray-100 last:border-0" data-psr-row="${key}">
            <div class="flex items-center gap-3 flex-wrap">
                ${_psrInput(s)}
                <code class="text-xs text-gray-500">${key}</code>
                ${_psrSourceBadge(s.source)}
                ${restartTag}
            </div>
            ${s.description ? `<p class="text-sm text-gray-600 mt-1">${_psrEsc(s.description)}</p>` : ''}
            <p class="mt-1 text-xs text-gray-400">${metaBits.join(' · ')}</p>
            ${overrides}
            <div class="psr-error mt-1 hidden text-sm text-red-600"></div>
            <div class="mt-2 flex items-center gap-4">
                <button type="button" data-psr-act="save" data-psr-key="${key}" class="px-3 py-1.5 text-sm font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save</button>
                ${revertBtn}
            </div>
        </div>`;
}

function _psrGroupTitle(group) {
    if (!group) return 'General';
    return group.charAt(0).toUpperCase() + group.slice(1);
}

// FIX-MFA-HIDE — cached edition verdict, the SAME fail-closed shape settings.js
// uses for its CE hides (`_paidEditionKnown`, settings.js:4361: an unknown/errored
// entitlements read counts as CE, so a dead control is never shown). Refreshed in
// initializePlatformSettingsPage; _psrRender is also called synchronously from
// _psrSave/_psrRevert, which is why the verdict is cached, not awaited there.
let _psrPaidEdition = false;

function _psrVisibleSettings() {
    // CE: drop the inert admin-MFA rows entirely — no toggle, no Save, no Revert
    // for a switch whose CE effect cannot be shown or applied by MOD.
    if (_psrPaidEdition) return _psrSettings;
    return _psrSettings.filter((s) => !_INERT_ON_CE_SETTING_KEYS.has(s.key));
}

function _psrRender() {
    const container = document.getElementById('psr-settings-container');
    if (!container) return;
    const groups = [];
    for (const s of _psrVisibleSettings()) {
        const g = s.group || '';
        let entry = groups.find(x => x.group === g);
        if (!entry) { entry = { group: g, settings: [] }; groups.push(entry); }
        entry.settings.push(s);
    }
    if (!groups.length) {
        container.innerHTML = `<div class="bg-white rounded-lg shadow-sm p-6 text-gray-600">No platform settings are registered.</div>`;
        return;
    }
    container.innerHTML = groups.map(g => `
        <div class="bg-white rounded-lg shadow-sm p-6 mb-4">
            <h3 class="text-lg font-semibold text-gray-900 mb-2">${_psrEsc(_psrGroupTitle(g.group))}</h3>
            ${g.settings.map(_psrRow).join('')}
        </div>`).join('');
}

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

function _psrToast(msg, ok = true) {
    const el = document.getElementById('psr-toast');
    if (!el) return;
    el.textContent = msg;
    el.className = `fixed bottom-6 right-6 z-50 px-4 py-2 rounded-lg text-sm shadow-lg ${ok ? 'bg-primary text-white' : 'bg-red-600 text-white'}`;
    clearTimeout(_psrToast._t);
    _psrToast._t = setTimeout(() => el.classList.add('hidden'), 5000);
}

// ---------------------------------------------------------------------------
// Row actions
// ---------------------------------------------------------------------------

function _psrRowEl(key) {
    return document.querySelector(`.psr-row[data-psr-row="${CSS.escape(key)}"]`);
}

function _psrShowError(key, message) {
    const row = _psrRowEl(key);
    const err = row && row.querySelector('.psr-error');
    if (err) {
        err.textContent = message;
        err.classList.remove('hidden');
    }
    _psrToast('Save failed: ' + message, false);
}

function _psrReadValue(input) {
    const t = input.dataset.psrType;
    if (t === 'bool') return input.checked;
    if (t === 'int') {
        const n = Number(input.value);
        if (input.value.trim() === '' || !Number.isInteger(n)) throw new Error('Enter a whole number');
        return n;
    }
    if (t === 'float') {
        const n = Number(input.value);
        if (input.value.trim() === '' || Number.isNaN(n)) throw new Error('Enter a number');
        return n;
    }
    if (t === 'json') {
        try { return JSON.parse(input.value); }
        catch (e) { throw new Error('Invalid JSON: ' + e.message); }
    }
    return input.value;
}

async function _psrSave(key) {
    const row = _psrRowEl(key);
    const input = row && row.querySelector('.psr-input');
    const saveBtn = row && row.querySelector('[data-psr-act="save"]');
    if (!input || !saveBtn) return;

    let value;
    try {
        value = _psrReadValue(input);
    } catch (e) {
        _psrShowError(key, e.message);
        return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';
    try {
        const res = await apiCall(`/platform/settings/${encodeURIComponent(key)}`, {
            method: 'PATCH',
            body: JSON.stringify({ value }),
        });
        const s = _psrSettings.find(x => x.key === key);
        if (s) { s.value = res.value; s.source = res.source; }
        _psrRender();
        _psrToast(`Saved ${key}`);
    } catch (e) {
        // No re-render here — the value didn't change, and re-rendering
        // would wipe the inline error we just showed.
        _psrShowError(key, e.message || String(e));
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
    }
}

async function _psrRevert(key) {
    if (!await showConfirm(`Revert "${key}" to its env/default value? The stored value is kept in the audit trail.`)) {
        return;
    }
    const row = _psrRowEl(key);
    const revertBtn = row && row.querySelector('[data-psr-act="revert"]');
    if (revertBtn) { revertBtn.disabled = true; revertBtn.textContent = 'Reverting…'; }
    try {
        const res = await apiCall(`/platform/settings/${encodeURIComponent(key)}`, { method: 'DELETE' });
        const s = _psrSettings.find(x => x.key === key);
        if (s) { s.value = res.value; s.source = res.source; }
        _psrRender();
        _psrToast(`Reverted ${key} to ${res.source}`);
    } catch (e) {
        // No re-render — the revert failed, nothing changed, and a re-render
        // would wipe the inline error.
        _psrShowError(key, e.message || String(e));
        if (revertBtn) { revertBtn.disabled = false; revertBtn.textContent = 'Revert to default'; }
    }
}

// ---------------------------------------------------------------------------
// Init (idempotent — safe to re-run on every sub-tab switch)
// ---------------------------------------------------------------------------

export async function initializePlatformSettingsPage() {
    const container = document.getElementById('psr-settings-container');
    const loadingEl = document.getElementById('psr-loading');
    if (!container || !loadingEl) return;

    const showFatal = (html) => {
        loadingEl.classList.add('hidden');
        container.innerHTML = html;
        container.classList.remove('hidden');
    };

    try {
        const user = await me.get();
        if (!user?.is_platform_admin) {
            showFatal(`<div class="bg-white rounded-lg shadow-sm p-6 text-gray-600">Platform settings can only be viewed by platform administrators.</div>`);
            return;
        }
        const data = await apiCall('/platform/settings');
        _psrSettings = (data && data.settings) || [];
    } catch (e) {
        const status = e && e.status;
        if (status === 404) {
            showFatal(`<div class="bg-white rounded-lg shadow-sm p-6 text-gray-600">The platform settings API is not available on this deployment yet (the API needs a restart to expose /platform/settings).</div>`);
        } else if (status === 401 || status === 403) {
            showFatal(`<div class="bg-white rounded-lg shadow-sm p-6 text-gray-600">Platform settings can only be viewed by platform administrators.</div>`);
        } else {
            showFatal(`<div class="bg-white rounded-lg shadow-sm p-6 text-red-600">Failed to load platform settings: ${_psrEsc(e.message || e)}</div>`);
        }
        return;
    }

    // Marker on the element, not the module: analytics.html is re-injected
    // per visit, so a module flag would leave later visits' containers
    // unwired.
    if (!container.dataset.psrWired) {
        container.dataset.psrWired = '1';
        container.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-psr-act]');
            if (!btn || btn.disabled) return;
            const key = btn.dataset.psrKey;
            if (!key) return;
            if (btn.dataset.psrAct === 'save') _psrSave(key);
            else if (btn.dataset.psrAct === 'revert') _psrRevert(key);
        });
    }

    // FIX-MFA-HIDE — refresh the cached edition verdict before rendering (api.js's
    // isPaidEdition is served from the cached getPlatformFeatures, so this adds no
    // round trip; it never throws — an unreadable/errored entitlements block is
    // false = CE = the inert rows stay hidden).
    try {
        _psrPaidEdition = await isPaidEdition();
    } catch (_) {
        _psrPaidEdition = false;
    }

    _psrRender();
    loadingEl.classList.add('hidden');
    container.classList.remove('hidden');

    document.getElementById('psr-refresh')?.addEventListener('click', () => {
        loadingEl.classList.remove('hidden');
        container.classList.add('hidden');
        initializePlatformSettingsPage();
    }, { once: true });
}
