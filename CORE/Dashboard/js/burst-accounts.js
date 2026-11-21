/**
 * Cloud accounts — GH.8b.
 *
 * The tenant's OWN cloud provider accounts that MOD provisions GPU workers
 * into. Deliberately separate from ./burst.js: that page is "spill my queue
 * to MOD's SaaS"; this one is "let MOD spend inside MY cloud account, under
 * MY limits". Same rendering conventions as burst.js (esc/when helpers, the
 * `$` shorthand, no frameworks) are duplicated here rather than imported —
 * this module has no dependency on burst.js's internals, and burst.js's own
 * logic is untouched.
 *
 * Credentials: the account API (routers/burst_accounts.py) never accepts or
 * returns a raw credential — only `secret_ref`, the NAME of an existing
 * tenant secret (crud/secret_cud.py). The provider is picked from a
 * dropdown populated by GET /burst/providers (the catalog in
 * services/burst_provider_catalog.py — names, labels, and the credential
 * FIELDS each provider's factory accepts, with secret/required/multiline
 * flags). Selecting a provider renders that provider's credential fields
 * (password inputs for `secret: true`, a textarea for `multiline`, help
 * text under each). On submit the field values are validated client-side
 * (required fields before ANY request), built into a JSON object, and — if
 * any value was entered — stored as a new tenant secret under the given
 * name via the existing `secrets.create()` client (api.js) BEFORE the
 * account is created; the account POST itself only ever carries the name.
 * A blank credential section reuses an existing secret of that name. The
 * field values are cleared after every submit attempt, success or failure,
 * so they never linger in the DOM. Field VALUES are never logged anywhere.
 *
 * The sessions/ledger/summary endpoints (GH.8a, built in parallel) may not
 * be deployed yet on some installs — a 404 from any of those three shows a
 * quiet "not available yet" panel instead of an error toast.
 */
import { burstAccounts, secrets, isPaidEdition } from './api.js';

const $ = (id) => document.getElementById(id);

function _baEsc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

function _baWhen(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

// Credit amounts may or may not carry a fractional part (metered fees vs.
// flat ledger adjustments). Whole numbers print bare; anything fractional
// gets 2dp so a small per-hour fee doesn't collapse to "0".
function _baCredits(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
    const v = Number(n);
    return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

// "a, b ,,c" -> ["a", "b", "c"]. Blank/whitespace-only input -> [].
function _baParseInstanceTypes(raw) {
    if (!raw) return [];
    return String(raw)
        .split(',')
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
}

// Builds the `limits` policy block shared by the Connect form and the
// policy editor. A blank number field is OMITTED entirely (not sent as 0 or
// null) — clearing a previously-set limit isn't offered by this UI, only
// widening/narrowing a limit that has a value.
function _baLimitsFromFields(maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw) {
    const limits = {};
    if (maxInstancesRaw !== '' && maxInstancesRaw !== null && maxInstancesRaw !== undefined) {
        const n = Number(maxInstancesRaw);
        if (!Number.isNaN(n)) limits.max_instances = n;
    }
    if (maxGpuHoursRaw !== '' && maxGpuHoursRaw !== null && maxGpuHoursRaw !== undefined) {
        const n = Number(maxGpuHoursRaw);
        if (!Number.isNaN(n)) limits.max_gpu_hours_month = n;
    }
    const types = _baParseInstanceTypes(instanceTypesRaw);
    if (types.length) limits.allowed_instance_types = types;
    return limits;
}

// Builds the exact POST /burst/accounts body (BurstAccountCreate in
// routers/burst_accounts.py: provider, secret_ref, region?, limits?).
// `secretRef` is the secret's NAME — never the credential value.
function _baBuildConnectPayload({ provider, region, secretRef, maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw }) {
    const payload = {
        provider: String(provider ?? '').trim(),
        secret_ref: String(secretRef ?? '').trim(),
    };
    const trimmedRegion = String(region ?? '').trim();
    if (trimmedRegion) payload.region = trimmedRegion;
    const limits = _baLimitsFromFields(maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw);
    if (Object.keys(limits).length) payload.limits = limits;
    return payload;
}

// Builds the credential JSON value that gets stored as the tenant secret
// from a provider's catalog `fields` and the current raw input values (a
// map of field key -> string). Pure: no DOM, never logs values.
//
//   * blank values are OMITTED (the factory falls back to its defaults);
//   * a blank REQUIRED field is reported in `missing` (label or key) —
//     the caller must block the submit when `missing` is non-empty;
//   * `list: true` fields (Lambda's ssh_key_names) split on commas into a
//     JSON array; `multiline` values keep their newlines (only surrounding
//     whitespace is trimmed); other values are trimmed.
//
// Returns `{ credential: <object>, missing: [<label>...], anyFilled: bool }`.
// `anyFilled` tells the caller whether a NEW secret must be created (any
// non-blank field) or an existing secret of the given name is reused.
function _baBuildCredential(fields, values) {
    const credential = {};
    const missing = [];
    let anyFilled = false;
    (fields || []).forEach((f) => {
        const raw = values ? values[f.key] : undefined;
        const str = raw === null || raw === undefined ? '' : String(raw);
        const filled = str.trim() !== '';
        if (!filled) {
            if (f && f.required) missing.push(f.label || f.key);
            return;
        }
        anyFilled = true;
        if (f && f.list) {
            credential[f.key] = str.split(',').map((s) => s.trim()).filter((s) => s.length > 0);
        } else {
            // Multiline values keep their internal newlines; surrounding
            // whitespace is trimmed either way.
            credential[f.key] = str.trim();
        }
    });
    return { credential, missing, anyFilled };
}

// Builds the exact PATCH /burst/accounts/{id} body for the policy editor —
// `limits` only (BurstAccountUpdate treats every field as "leave
// unchanged" when omitted, and this editor never touches provider/
// secret_ref/region).
function _baBuildPolicyPayload(maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw) {
    return { limits: _baLimitsFromFields(maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw) };
}

function _baStatusBadge(status) {
    const map = {
        valid: 'text-green-700 bg-green-50 border-green-200',
        invalid: 'text-red-700 bg-red-50 border-red-200',
        pending: 'text-amber-700 bg-amber-50 border-amber-200',
    };
    const cls = map[status] || 'text-gray-700 bg-gray-50 border-gray-200';
    return `<span class="text-xs px-2 py-0.5 rounded-full border ${cls}">${_baEsc(status || 'unknown')}</span>`;
}

function _baToast(id, msg, kind = 'ok') {
    const el = $(id);
    if (!el) return;
    el.textContent = msg;
    el.className = kind === 'ok'
        ? 'text-sm text-green-700 bg-green-50 border border-green-200 rounded-lg px-4 py-2'
        : 'text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-2';
    el.classList.remove('hidden');
    setTimeout(() => el.classList.add('hidden'), 6000);
}

function _toastGlobal(msg, kind = 'ok') { _baToast('ba-toast', msg, kind); }

// Cache of the last accounts list, so "Edit policy" can seed the form
// without a second fetch.
let _accountsCache = [];

// Server-side limit defaults from the last GET /burst/summary, when the
// payload carries them (used only as placeholder text; the UI never invents
// a number).
let _summaryDefaults = {};

// The provider credential catalog (GET /burst/providers), fetched once per
// page load. Metadata only — never a credential value.
let _providerCatalog = [];

// The GH.8a sessions/ledger/summary endpoints may not be deployed yet.
// Sticky for the lifetime of one renderBurstAccountsSection() call — reset
// at the top of that function, then flipped true by the first 404 any of
// the three loaders below hits.
let _unavailable = false;

function _markAvailable(ok) {
    if (!ok) _unavailable = true;
    $('ba-not-available')?.classList.toggle('hidden', !_unavailable);
}

// ---------------------------------------------------------------------------
// Accounts table
// ---------------------------------------------------------------------------

async function _loadAccounts() {
    const body = $('ba-accounts-body');
    if (!body) return;
    try {
        const rows = await burstAccounts.list();
        _accountsCache = rows;
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="5" class="text-center text-gray-500 py-6">
                No cloud accounts connected yet.</td></tr>`;
        } else {
            body.innerHTML = rows.map((a) => `
                <tr class="border-b border-gray-100">
                    <td class="py-2 px-3">${_baEsc(a.provider)}</td>
                    <td class="py-2 px-3">${_baEsc(a.region || '—')}</td>
                    <td class="py-2 px-3">${_baStatusBadge(a.status)}</td>
                    <td class="py-2 px-3 text-sm">
                        ${_baWhen(a.last_probe_at)}
                        ${a.last_probe_error
                            ? `<span class="block text-xs text-red-600">${_baEsc(a.last_probe_error)}</span>`
                            : ''}
                    </td>
                    <td class="py-2 px-3 text-right whitespace-nowrap">
                        <button data-ba-probe="${a.id}" class="text-xs text-blue-600 hover:text-blue-800 mr-3">Probe</button>
                        <button data-ba-edit-policy="${a.id}" class="text-xs text-blue-600 hover:text-blue-800 mr-3">Edit policy</button>
                        <button data-ba-remove="${a.id}" class="text-xs text-red-600 hover:text-red-800">Remove</button>
                    </td>
                </tr>`).join('');
        }
        _wireAccountRowActions(body);
        $('ba-summary-accounts')?.replaceChildren(document.createTextNode(String(rows.length)));
    } catch (e) {
        body.innerHTML = `<tr><td colspan="5" class="text-center text-red-500 py-6">
            Could not load accounts: ${_baEsc(e.message)}</td></tr>`;
    }
}

function _wireAccountRowActions(body) {
    body.querySelectorAll('[data-ba-probe]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = 'Probing…';
            try {
                await burstAccounts.probe(btn.dataset.baProbe);
            } catch (e) {
                _toastGlobal(`Probe failed: ${e.message}`, 'err');
            }
            await _loadAccounts();
        });
    });
    body.querySelectorAll('[data-ba-edit-policy]').forEach((btn) => {
        btn.addEventListener('click', () => _openPolicyEditor(btn.dataset.baEditPolicy));
    });
    body.querySelectorAll('[data-ba-remove]').forEach((btn) => {
        btn.addEventListener('click', async () => {
            // ISO 27001 — the DELETE endpoint soft-deletes; the confirm says
            // what actually happens (workers stop being provisioned there),
            // not "delete", to match.
            if (!window.confirm('Remove this cloud account? MOD will stop provisioning workers into it.')) return;
            try {
                await burstAccounts.remove(btn.dataset.baRemove);
                _toastGlobal('Cloud account removed.');
                await _loadAccounts();
            } catch (e) {
                _toastGlobal(`Could not remove: ${e.message}`, 'err');
            }
        });
    });
}

// ---------------------------------------------------------------------------
// Connect form — provider select + credential fields
// ---------------------------------------------------------------------------

// Fills the provider <select> with the cached catalog options, preserving
// `selectedName` when it still exists in the catalog. The options keep the
// registered provider name as value and the human label as text.
function _applyProviderOptions(sel, selectedName) {
    if (!sel) return;
    const stillValid = selectedName && _providerCatalog.some((p) => p.name === selectedName);
    const selName = stillValid ? selectedName : '';
    sel.innerHTML = `<option value="" disabled ${selName ? '' : 'selected'}>Select a provider…</option>` +
        _providerCatalog.map((p) =>
            `<option value="${_baEsc(p.name)}" ${p.name === selName ? 'selected' : ''}>${_baEsc(p.label)}</option>`
        ).join('');
}

// Fetches the catalog once per page load and populates the provider
// <select>. renderBurstAccountsSection guards the first call; later tab
// re-renders (which replace the form markup with a fresh, empty select)
// repopulate it from the cache instead of re-fetching.
async function _loadProviders() {
    const sel = $('ba-provider');
    if (!sel) return;
    try {
        _providerCatalog = (await burstAccounts.providers()) || [];
    } catch (e) {
        // A failed catalog fetch leaves only the placeholder option — the
        // form is unusable but nothing else on this tab breaks. The error
        // is surfaced, not the credential values (there are none here).
        _providerCatalog = [];
        sel.innerHTML =
            `<option value="" disabled selected>Select a provider…</option>` +
            `<option value="" disabled>Failed to load providers: ${_baEsc(e.message)}</option>`;
        return;
    }
    _applyProviderOptions(sel, sel.value || '');
    // A re-render of the tab replaces the select contents, so re-render any
    // fields for the currently selected provider (if it survived).
    _renderCredentialFields(sel.value);
}

// Renders the selected provider's credential fields into
// #ba-credential-fields: password inputs for `secret: true`, a textarea for
// `multiline` fields, a plain input otherwise, each with its label and help
// text. Field values are NEVER logged (the DOM is the only place they live
// until submit, and submit clears them again).
function _renderCredentialFields(providerName) {
    const box = $('ba-credential-fields');
    if (!box) return;
    if (!providerName) {
        box.innerHTML = '<p class="text-sm text-gray-500">Select a provider to see its credential fields.</p>';
        return;
    }
    const entry = _providerCatalog.find((p) => p.name === providerName);
    if (!entry) {
        box.innerHTML = '<p class="text-sm text-gray-500">No credential fields defined for this provider.</p>';
        return;
    }
    box.innerHTML = (entry.fields || []).map((f) => {
        const inputType = f.secret ? 'password' : 'text';
        const required = f.required ? ' required' : '';
        const autocomplete = f.multiline
            ? 'off'
            : (f.secret ? 'new-password' : 'off');
        const inputEl = f.multiline
            ? `<textarea id="ba-cred-${_baEsc(f.key)}" data-ba-cred="${_baEsc(f.key)}"${required} rows="4"
                    autocomplete="off" spellcheck="false"
                    class="mt-1 w-full border border-gray-300 rounded px-2 py-1.5 text-sm font-mono"></textarea>`
            : `<input type="${inputType}" id="ba-cred-${_baEsc(f.key)}" data-ba-cred="${_baEsc(f.key)}"${required}
                    autocomplete="${autocomplete}"
                    class="mt-1 w-full border border-gray-300 rounded px-2 py-1.5 text-sm">`;
        return `
            <div>
                <label class="text-sm text-gray-600" for="ba-cred-${_baEsc(f.key)}">
                    ${_baEsc(f.label)}${f.required ? ' <span class="text-red-500">*</span>' : ''}
                </label>
                ${inputEl}
                ${f.help ? `<p class="text-xs text-gray-500 mt-1">${_baEsc(f.help)}</p>` : ''}
            </div>`;
    }).join('');
}

// The current raw values of the rendered credential fields, keyed by the
// catalog field key. Values are only ever handed to _baBuildCredential and
// then cleared — never logged.
function _baCollectCredentialValues() {
    const out = {};
    document.querySelectorAll('#ba-credential-fields [data-ba-cred]').forEach((el) => {
        out[el.dataset.baCred] = el.value;
    });
    return out;
}

// Wipes every rendered credential field value (success or failure).
function _baClearCredentialFields() {
    document.querySelectorAll('#ba-credential-fields [data-ba-cred]').forEach((el) => {
        el.value = '';
    });
}

async function _handleConnectSubmit(ev) {
    ev.preventDefault();
    const provider = $('ba-provider')?.value || '';
    const region = $('ba-region')?.value || '';
    const secretName = $('ba-secret-name')?.value || '';
    const maxInstancesRaw = $('ba-limit-max-instances')?.value ?? '';
    const maxGpuHoursRaw = $('ba-limit-max-gpu-hours')?.value ?? '';
    const instanceTypesRaw = $('ba-limit-instance-types')?.value ?? '';

    // Build the credential JSON object from the provider's catalog fields +
    // current field values, and validate REQUIRED fields client-side BEFORE
    // any request goes out.
    const credEntry = _providerCatalog.find((p) => p.name === provider);
    const { credential, missing, anyFilled } = _baBuildCredential(
        credEntry?.fields, _baCollectCredentialValues(),
    );
    const submitBtn = $('ba-connect-submit');
    if (!provider) {
        _baToast('ba-connect-toast', 'Select a provider to connect.', 'err');
        return;
    }
    if (!secretName.trim()) {
        _baToast('ba-connect-toast', 'A credential name is required.', 'err');
        return;
    }
    if (missing.length) {
        _baToast('ba-connect-toast',
            `Missing required credential field(s): ${missing.join(', ')}`, 'err');
        return;
    }

    if (submitBtn) submitBtn.disabled = true;
    try {
        if (anyFilled) {
            // Any field was filled — store the credential object as a NEW
            // tenant secret under the given name. If a secret with this name
            // already exists, the create call 409s; that surfaces to the
            // operator instead of silently overwriting an existing
            // credential. A completely blank section reuses the existing
            // secret of that name.
            await secrets.create({
                name: secretName.trim(),
                value: JSON.stringify(credential),
                description: `Cloud credential (${provider})`,
            });
        }
        const payload = _baBuildConnectPayload({
            provider, region, secretRef: secretName,
            maxInstancesRaw, maxGpuHoursRaw, instanceTypesRaw,
        });
        await burstAccounts.create(payload);
        _baToast('ba-connect-toast', 'Cloud account connected.');
        $('ba-connect-form')?.reset();
        _renderCredentialFields('');
        await _loadAccounts();
    } catch (e) {
        _baToast('ba-connect-toast', `Could not connect: ${e.message}`, 'err');
    } finally {
        // Never leave the credential values in the DOM, success or failure.
        _baClearCredentialFields();
        if (submitBtn) submitBtn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Policy editor
// ---------------------------------------------------------------------------

// Placeholder text for a blank limit field: the server's default when the
// summary payload carries one, otherwise the words "server default". The UI
// never invents a number.
function _baLimitPlaceholder(key) {
    const d = _summaryDefaults[key];
    return (d === null || d === undefined || d === '') ? 'server default' : String(d);
}

function _baSeedLimitField(inputId, currentValue, defaultKey) {
    const el = $(inputId);
    if (!el) return;
    const blank = currentValue === '' || currentValue === null || currentValue === undefined;
    el.value = blank ? '' : String(currentValue);
    el.placeholder = blank ? _baLimitPlaceholder(defaultKey) : '';
}

function _openPolicyEditor(accountId) {
    const acct = _accountsCache.find((a) => String(a.id) === String(accountId));
    const editor = $('ba-policy-editor');
    if (!editor) return;
    const limits = acct?.limits || {};
    if ($('ba-policy-account-id')) $('ba-policy-account-id').value = accountId;
    _baSeedLimitField('ba-policy-max-instances', limits.max_instances, 'max_instances');
    _baSeedLimitField('ba-policy-max-gpu-hours', limits.max_gpu_hours_month, 'max_gpu_hours_month');
    if ($('ba-policy-instance-types')) {
        $('ba-policy-instance-types').value = Array.isArray(limits.allowed_instance_types)
            ? limits.allowed_instance_types.join(', ')
            : '';
    }
    editor.classList.remove('hidden');
    editor.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function _handlePolicySave() {
    const accountId = $('ba-policy-account-id')?.value;
    if (!accountId) return;
    const payload = _baBuildPolicyPayload(
        $('ba-policy-max-instances')?.value ?? '',
        $('ba-policy-max-gpu-hours')?.value ?? '',
        $('ba-policy-instance-types')?.value ?? '',
    );
    try {
        await burstAccounts.update(accountId, payload);
        _baToast('ba-policy-toast', 'Policy saved.');
        $('ba-policy-editor')?.classList.add('hidden');
        await _loadAccounts();
    } catch (e) {
        _baToast('ba-policy-toast', `Could not save policy: ${e.message}`, 'err');
    }
}

// ---------------------------------------------------------------------------
// Live cloud workers (sessions) — GH.8a
// ---------------------------------------------------------------------------

async function _loadSessions() {
    const body = $('ba-sessions-body');
    if (!body) return;
    const showAll = !!$('ba-sessions-all-toggle')?.checked;
    try {
        const rows = await burstAccounts.sessions(showAll ? 'all' : 'active', 50);
        _markAvailable(true);
        if (!rows.length) {
            body.innerHTML = `<tr><td colspan="7" class="text-center text-gray-500 py-6">
                No ${showAll ? '' : 'active '}cloud workers.</td></tr>`;
            return;
        }
        body.innerHTML = rows.map((s) => `
            <tr class="border-b border-gray-100">
                <td class="py-2 px-3 font-mono text-xs">${_baEsc(s.worker_name || '—')}</td>
                <td class="py-2 px-3">${_baEsc(s.provider)}</td>
                <td class="py-2 px-3">${_baEsc(s.gpu_class || '—')}</td>
                <td class="py-2 px-3">${_baEsc(s.status)}</td>
                <td class="py-2 px-3 text-sm">${_baWhen(s.joined_at)}</td>
                <td class="py-2 px-3 text-sm">${_baWhen(s.left_at)}</td>
                <td class="py-2 px-3 tabular-nums">${_baCredits(s.fees_charged_credits)}</td>
            </tr>`).join('');
    } catch (e) {
        if (e.status === 404) {
            _markAvailable(false);
            body.innerHTML = `<tr><td colspan="7" class="text-center text-gray-500 py-6">Not available yet.</td></tr>`;
            return;
        }
        body.innerHTML = `<tr><td colspan="7" class="text-center text-red-500 py-6">
            Could not load cloud workers: ${_baEsc(e.message)}</td></tr>`;
    }
}

// ---------------------------------------------------------------------------
// Fee ledger — GH.8a
// ---------------------------------------------------------------------------

// The ledger section and the "fees this month" tile are hidden unless something
// proves a fee exists (CE has no MOD fee at all — Q36). Both start hidden in
// _renderBurstAccountsWiring; either loader can only ever SHOW them.
function _baLedgerSection() {
    const body = $('ba-ledger-body');
    return body ? body.closest('section') : null;
}

function _baFeesTile() {
    return $('ba-summary-fees')?.parentElement;
}

function _baShowFees() {
    _baLedgerSection()?.classList.remove('hidden');
    _baFeesTile()?.classList.remove('hidden');
}

async function _loadLedger() {
    const body = $('ba-ledger-body');
    if (!body) return;
    try {
        const rows = await burstAccounts.ledger(100);
        _markAvailable(true);
        if (!rows.length) {
            // No rows: the section stays hidden (no "no fees" filler text).
            body.innerHTML = '';
            return;
        }
        _baShowFees();
        body.innerHTML = rows.map((r) => `
            <tr class="border-b border-gray-100">
                <td class="py-2 px-3 text-sm">${_baWhen(r.created_at)}</td>
                <td class="py-2 px-3 tabular-nums">${_baCredits(r.amount_credits)}</td>
                <td class="py-2 px-3 text-sm">${_baEsc(r.description || '—')}</td>
            </tr>`).join('');
    } catch (e) {
        if (e.status === 404) {
            _markAvailable(false);
            body.innerHTML = `<tr><td colspan="3" class="text-center text-gray-500 py-6">Not available yet.</td></tr>`;
            return;
        }
        body.innerHTML = `<tr><td colspan="3" class="text-center text-red-500 py-6">
            Could not load cloud fees: ${_baEsc(e.message)}</td></tr>`;
    }
}

// ---------------------------------------------------------------------------
// Summary strip — GH.8a
// ---------------------------------------------------------------------------

async function _loadSummary() {
    try {
        const s = await burstAccounts.summary();
        _markAvailable(true);
        if ($('ba-summary-accounts')) $('ba-summary-accounts').textContent = String(s.accounts ?? _accountsCache.length);
        if ($('ba-summary-active-sessions')) $('ba-summary-active-sessions').textContent = String(s.active_sessions ?? '—');
        // The fee tile only prints when a fee actually exists (Q36: CE has no
        // MOD fee). Absent/zero stays hidden — never "0 credits".
        const fees = s.fees_this_month_credits;
        if (fees !== null && fees !== undefined && !Number.isNaN(Number(fees)) && Number(fees) > 0) {
            _baShowFees();
            if ($('ba-summary-fees')) $('ba-summary-fees').textContent = _baCredits(fees);
        }
        // Server-side limit defaults, if the payload carries them (used only as
        // placeholder text on the policy inputs; nothing is invented here).
        _summaryDefaults = {
            max_instances: s.default_max_instances,
            max_gpu_hours_month: s.default_max_gpu_hours_month,
        };
        // Refresh the blank-limit placeholders now that the defaults are known.
        _baSeedLimitField('ba-limit-max-instances', $('ba-limit-max-instances')?.value, 'max_instances');
        _baSeedLimitField('ba-limit-max-gpu-hours', $('ba-limit-max-gpu-hours')?.value, 'max_gpu_hours_month');
    } catch (e) {
        if (e.status === 404) {
            _markAvailable(false);
            if ($('ba-summary-active-sessions')) $('ba-summary-active-sessions').textContent = '—';
            return;
        }
        // Non-404 error: leave the strip's dashes rather than crash the tab.
    }
}

// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------

// OZ-E3 — themed display names (Oz theming). This page is the tenant's OWN
// public-cloud burst surface, whose NAME-MAP product is Balloon Cloud (the
// `burst-accounts` key in js/product-names.js). The visible title (the panel
// h2) and the System sub-tab entry (the #main-tab-burst-accounts button) carry
// data-theme-key="burst-accounts", so their LABELS are read from the map at
// RUN TIME: nothing here hard-codes a rename (no new static import either —
// the map module is resolved with a dynamic import, the same idiom analytics.js
// already uses for optional pages), ids/hrefs/routes/panel ids are never
// touched, and with MOD_THEME_NAMES off themeLabel returns today's literal, so
// the page renders byte for byte unchanged. Unknown/absent key -> literal
// (never blank). Only the trailing TEXT NODE of an element is replaced (the
// tab button's icon markup stays) and text nodes only — never innerHTML — so
// no provider-supplied string can ever reach the DOM as HTML.
function _baApplySurfaceLabel(el, names, labelOf) {
    if (!names || typeof labelOf !== 'function') return;
    const key = el.dataset.themeKey;
    if (!key || !names[key] || !names[key].label) return;
    const textNode = _baLastTextNode(el);
    if (!textNode) return; // no text child to theme (icon-only element)
    const today = textNode.textContent.trim();
    if (today === '') return;
    const want = labelOf(key, today);
    if (want === today) return; // flag OFF or already themed: byte-for-byte no-op
    textNode.textContent = want;
}

function _baLastTextNode(el) {
    for (let i = el.childNodes.length - 1; i >= 0; i -= 1) {
        const child = el.childNodes[i];
        if (child && child.nodeType === 3 && child.textContent.trim() !== '') return child;
    }
    return null;
}

function _baThemeSurfaceLabels() {
    // Fire-and-forget: the async pass resolves the map module at RUN TIME and
    // never rejects (every failure keeps today's literals byte for byte).
    _baThemeSurfacePassAsync().catch(() => {});
}

async function _baThemeSurfacePassAsync() {
    let mod = null;
    try {
        mod = await import('./product-names.js');
    } catch (e) {
        return; // no map module on this build: literals stay
    }
    if (!mod || !mod.PRODUCT_NAMES || typeof mod.themeLabel !== 'function') return;
    try {
        document.querySelectorAll('[data-theme-key]')
            .forEach((el) => _baApplySurfaceLabel(el, mod.PRODUCT_NAMES, mod.themeLabel));
    } catch (e) {
        // A DOM that does not carry the hook attributes (or a bare stub DOM)
        // simply keeps the literal labels.
    }
}

export function renderBurstAccountsSection() {
    // Cloud is IN the CE edition (owner decision 11-05): no edition guard here.
    // The routers are carved into the CE manifest, so the loaders below always
    // run; a route that is nonetheless missing on an install answers 404 and
    // each loader shows the #ba-not-available notice, never a thrown error.
    _renderBurstAccountsWiring();
}

function _renderBurstAccountsWiring() {
    // Idempotent wiring: this panel is a System sub-tab, so it re-runs on
    // every tab activation (see burst.js's renderBurstSection for why a
    // dataset marker is used instead of a module-level flag — the markup
    // itself is replaced by navigateToPage, so a fresh element with no
    // listener would otherwise silently stop responding on a second visit).
    const form = $('ba-connect-form');
    if (form && !form.dataset.baWired) {
        form.dataset.baWired = '1';
        form.addEventListener('submit', _handleConnectSubmit);
        // Cloud ships in every edition (CE included): no entitlement lock on Connect.
        // The lock is paid-only (owner decision 11-11): a rejected or false
        // isPaidEdition() means CE (or unknown) and leaves Connect unlocked.
        const connectBtn = $('ba-connect-submit');
        if (connectBtn) {
            isPaidEdition().then((paid) => {
                if (paid) MODEntitlements.lockElement(connectBtn, 'burst', 'Cloud burst');
            }).catch(() => {});
        }
    }
    const providerSel = $('ba-provider');
    if (providerSel && !providerSel.dataset.baWired) {
        providerSel.dataset.baWired = '1';
        providerSel.addEventListener('change', () => _renderCredentialFields(providerSel.value));
    }
    const policySave = $('ba-policy-save');
    if (policySave && !policySave.dataset.baWired) {
        policySave.dataset.baWired = '1';
        policySave.addEventListener('click', _handlePolicySave);
    }
    const policyCancel = $('ba-policy-cancel');
    if (policyCancel && !policyCancel.dataset.baWired) {
        policyCancel.dataset.baWired = '1';
        policyCancel.addEventListener('click', () => $('ba-policy-editor')?.classList.add('hidden'));
    }
    const allToggle = $('ba-sessions-all-toggle');
    if (allToggle && !allToggle.dataset.baWired) {
        allToggle.dataset.baWired = '1';
        allToggle.addEventListener('change', _loadSessions);
    }

    // OZ-E3: surface labels for this page (title + System sub-tab entry) come
    // from the theming map; ids/hrefs/routes untouched.
    try {
        _baThemeSurfaceLabels();
    } catch (e) {
        // A DOM without [data-theme-key] support simply keeps the literal text.
    }

    _unavailable = false;
    _summaryDefaults = {};
    $('ba-not-available')?.classList.add('hidden');

    // Fee surfaces start hidden (Q36: no MOD fee in CE). A loader only ever
    // shows them when the server reports a real fee.
    _baLedgerSection()?.classList.add('hidden');
    _baFeesTile()?.classList.add('hidden');

    // Blank limit fields show the server default as placeholder text.
    _baSeedLimitField('ba-limit-max-instances', $('ba-limit-max-instances')?.value, 'max_instances');
    _baSeedLimitField('ba-limit-max-gpu-hours', $('ba-limit-max-gpu-hours')?.value, 'max_gpu_hours_month');

    // Populate the provider dropdown once per page load; a tab re-render
    // replaces the form markup with a fresh empty select, so repopulate
    // it from the cache (no re-fetch) and re-render any still-selected
    // provider's credential fields.
    if (_providerCatalog.length) {
        _applyProviderOptions($('ba-provider'), '');
        _renderCredentialFields('');
    } else {
        _loadProviders();
    }
    _loadAccounts();
    _loadSummary();
    _loadSessions();
    _loadLedger();
}

export default { renderBurstAccountsSection };
