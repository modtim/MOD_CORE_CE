// GSEC FX.112 p3c — tenant-admin security alert rules (MOD Core Dashboard).
//
// Backend: CORE/API/routers/tenant/security.py (mounted at /v1/tenant), logic
// in services/tenant_alert_rules.py:
//   GET  /tenant/security/alert-rules            gates TENANT:view_settings
//        -> {rules: [{rule_key, name, description, tunable, enabled,
//        threshold, default_threshold, min_threshold, max_threshold}]}
//        (tunable is the threshold axis name — signin_failures /
//        unusual_downloads / export_anomaly — or null for on/off-only rules;
//        min/max are the platform-set bounds, group security_alerts.)
//   PUT  /tenant/security/alert-rules/{rule_key} gates TENANT:manage_settings
//        body {enabled?, threshold?} -> the new rule view; 422 when neither
//        field is sent, when a threshold is sent for a non-tunable rule, or
//        when the threshold is outside the platform bounds; 404 unknown rule.
//        One TENANT_SECURITY_ALERT_RULE_UPDATED audit row per change.
//
// One row per rule: a labelled on/off checkbox (its checked state IS the
// value Save will PUT), a labelled number input for tunable rules only (min/
// max printed next to it, bounds checked BEFORE the PUT so an out-of-bounds
// value never reaches the server), and a Save button. A 403 on any PUT flips
// the whole page into the read-only view (controls disabled + notice) — the
// same belt-and-braces idiom as log-streams.js, since the nav only mirrors
// the tenant-admin gate. Every API string is written with textContent.
import { alertRules, isPaidEdition } from './api.js';

let _rules = [];
let _readOnly = false;
let _rows = {};   // rule_key -> {toggle, thInput, btn} for the live DOM row

function _showError(msg) {
    const box = document.getElementById('ar-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('ar-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('ar-notice');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('ar-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail (plain string for this router's 422/404s; the
// standard FastAPI field-error list is flattened like the other pages).
function _errText(e) {
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (Array.isArray(detail)) {
        return detail.map((x) => {
            if (typeof x === 'string') return x;
            if (x && typeof x === 'object') {
                const loc = Array.isArray(x.loc)
                    ? x.loc.filter((p) => p !== 'body').join('.') : '';
                return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
            }
            return String(x);
        }).join('; ');
    }
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
        try { return detail.message || JSON.stringify(detail); } catch { return 'Unknown error'; }
    }
    return (e && e.message) || '';
}

function _idKey(key) {
    return String(key == null ? '' : key).replace(/[^a-z0-9_-]/gi, '_');
}

function _td(text, cls) {
    const td = document.createElement('td');
    td.className = cls;
    td.textContent = text == null ? '—' : String(text);
    return td;
}

function _badge(text, cls) {
    const span = document.createElement('span');
    span.className = `px-2 py-0.5 rounded-full text-xs ${cls}`;
    span.textContent = text;
    return span;
}

// ---------------------------------------------------------------------------
// init + load
// ---------------------------------------------------------------------------

export async function initializeAlertRulesPage() {
    // 2400UI79 — routers/tenant/security.py is CE-denied, so alert-rules
    // never exists there; skip the fetch. Fail-open (unreadable = not CE),
    // so paid keeps the old behaviour.
    if (!await isPaidEdition()) {
        // The page is still reachable from nav on CE (ui.js mirrors the
        // tenant-admin gate only), so render the empty state instead of
        // leaving the table half-drawn — same idiom as
        // the denied resource-monitor module's loadTenants().
        const tbody = document.getElementById('ar-table-body');
        const count = document.getElementById('ar-count');
        const empty = document.getElementById('ar-empty');
        if (tbody) tbody.innerHTML = '';
        if (count) count.textContent = '0 built-in rule(s) apply to this tenant';
        if (empty) empty.classList.remove('hidden');
        return;
    }
    await loadRules();
}

async function loadRules() {
    _clearError();
    const tbody = document.getElementById('ar-table-body');
    if (!tbody) return;
    _rows = {};
    tbody.innerHTML = '';
    try {
        const res = await alertRules.list();
        _rules = (res && res.rules) || [];
    } catch (e) {
        _rules = [];
        _showError(_errText(e) || 'Failed to load the alert rules');
        const empty = document.getElementById('ar-empty');
        if (empty) empty.classList.remove('hidden');
        return;
    }
    const count = document.getElementById('ar-count');
    if (count) {
        count.textContent = `${_rules.length} built-in rule(s) apply to this tenant`
            + (_readOnly ? ' · read-only (the API refused the write)' : '');
    }
    if (!_rules.length) {
        const empty = document.getElementById('ar-empty');
        if (empty) empty.classList.remove('hidden');
        return;
    }
    renderRows(tbody);
}

function renderRows(tbody) {
    _rules.forEach((rule) => {
        tbody.appendChild(_rowNode(rule));
    });
}

// ---------------------------------------------------------------------------
// One row: name | description | on/off toggle | threshold (tunable only) | Save
// Controls are labelled explicitly (label for=<id>) and carry aria-labels, so
// they are named even before the visible label is associated, and keyboard
// focus follows DOM order (no positive tabindex anywhere).
// ---------------------------------------------------------------------------

function _rowNode(rule) {
    const key = rule.rule_key;
    const idp = `ar-${_idKey(key)}`;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';

    tr.appendChild(_td(rule.name, 'px-4 py-3 whitespace-nowrap font-semibold'));
    tr.appendChild(_td(rule.description, 'px-4 py-3 text-xs'));

    // --- on/off toggle: the checkbox IS the value Save will PUT ----------
    const stateTd = document.createElement('td');
    stateTd.className = 'px-4 py-3 whitespace-nowrap';
    const label = document.createElement('label');
    label.className = 'flex items-center gap-1 text-sm text-gray-700';
    label.setAttribute('for', `${idp}-enabled`);
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.id = `${idp}-enabled`;
    toggle.checked = !!rule.enabled;
    toggle.setAttribute('aria-label',
        `Turn "${rule.name}" on or off for this tenant`);
    label.appendChild(toggle);
    label.appendChild(document.createTextNode(' on for this tenant'));
    stateTd.appendChild(label);
    const badge = _badge(rule.enabled ? 'Enabled' : 'Disabled',
        rule.enabled ? 'bg-green-100 text-green-700'
                     : 'bg-gray-100 text-gray-600');
    badge.setAttribute('aria-hidden', 'true');
    stateTd.appendChild(badge);
    tr.appendChild(stateTd);

    // --- threshold: number input for tunable rules only -----------------
    const thTd = document.createElement('td');
    thTd.className = 'px-4 py-3 whitespace-nowrap';
    let thInput = null;
    if (rule.tunable) {
        const thLabel = document.createElement('label');
        thLabel.className = 'flex items-center gap-1 text-sm text-gray-700';
        thLabel.setAttribute('for', `${idp}-threshold`);
        thInput = document.createElement('input');
        thInput.type = 'number';
        thInput.id = `${idp}-threshold`;
        thInput.setAttribute('aria-label',
            `Threshold for "${rule.name}" (${rule.tunable})`);
        if (rule.min_threshold != null) thInput.min = rule.min_threshold;
        if (rule.max_threshold != null) thInput.max = rule.max_threshold;
        thInput.value = rule.threshold != null ? rule.threshold : '';
        thLabel.appendChild(thInput);
        thLabel.appendChild(document.createTextNode(' threshold'));
        thTd.appendChild(thLabel);
        const bounds = document.createElement('span');
        bounds.className = 'text-xs text-gray-500 ml-2';
        bounds.textContent = _boundsText(rule);
        thTd.appendChild(bounds);
    } else {
        const none = document.createElement('span');
        none.className = 'text-xs text-gray-500';
        none.textContent = 'No threshold (on/off only)';
        thTd.appendChild(none);
    }
    tr.appendChild(thTd);

    // --- Save button ----------------------------------------------------
    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 text-right whitespace-nowrap';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = `${idp}-save`;
    btn.dataset.arAction = 'save';
    btn.dataset.arKey = key;
    btn.textContent = `Save "${rule.name}"`;
    btn.className = 'text-indigo-600 hover:underline text-sm';
    actTd.appendChild(btn);
    tr.appendChild(actTd);

    _rows[key] = { toggle, thInput, btn };
    if (_readOnly) _disableRow(key);
    return tr;
}

function _boundsText(rule) {
    const bits = [];
    if (rule.min_threshold != null || rule.max_threshold != null) {
        bits.push(`platform bounds ${rule.min_threshold ?? '—'}–`
            + `${rule.max_threshold ?? '—'}`);
    }
    if (rule.default_threshold != null) {
        bits.push(`platform default ${rule.default_threshold}`);
    }
    return bits.join(' · ');
}

function _disableRow(key) {
    const row = _rows[key];
    if (!row) return;
    [row.toggle, row.thInput, row.btn].forEach((el) => {
        if (!el) return;
        el.disabled = true;
        el.setAttribute('aria-disabled', 'true');
    });
}

// ---------------------------------------------------------------------------
// Save one row: read the controls, client-side bounds check, then PUT.
// The checkbox state is always sent (so "Save" is never an empty 422 body);
// the threshold is sent only when the number input carries a value.
// ---------------------------------------------------------------------------

export async function handleSave(btn) {
    const key = btn.dataset.arKey;
    const rule = _rules.find((r) => r.rule_key === key);
    if (!rule) { _showError(`Unknown alert rule: ${key}`); return; }
    if (_readOnly) {
        _showError('This page is read-only: the API refused the write (403).');
        return;
    }
    const row = _rows[key] || { toggle: null, thInput: null };
    const toggle = row.toggle;
    const thInput = row.thInput;

    const body = { enabled: toggle ? !!toggle.checked : !!rule.enabled };
    if (thInput && String(thInput.value ?? '').trim() !== '') {
        const raw = String(thInput.value).trim();
        const num = Number(raw);
        if (!Number.isInteger(num)) {
            _showError(`Threshold "${raw}" is not a whole number — nothing was saved.`);
            return;
        }
        const lo = rule.min_threshold, hi = rule.max_threshold;
        if ((lo != null && num < lo) || (hi != null && num > hi)) {
            _showError(`Threshold ${num} is outside the platform bounds `
                + `[${lo ?? '-'}, ${hi ?? '-'}] for "${rule.name}" — `
                + 'nothing was saved. Pick a value inside the bounds shown on the row.');
            return;
        }
        // Only a CHANGED threshold is PUT: the server pins threshold_override
        // when a threshold is sent, so echoing the current value back would
        // stop the rule from following the platform default.
        if (rule.threshold == null || num !== rule.threshold) body.threshold = num;
    }
    _clearError();
    try {
        const res = await alertRules.update(key, body);
        if (res && res.rule_key) {
            rule.enabled = res.enabled;
            rule.threshold = res.threshold;
            rule.min_threshold = res.min_threshold;
            rule.max_threshold = res.max_threshold;
        }
        _rerender();
    } catch (e) {
        if (e && e.status === 403) {
            _goReadOnly('You can view the alert rules, but this tenant cannot '
                + 'change them (the API refused the write with 403).');
            return;
        }
        _showError(_errText(e) || `Failed to save "${rule.name}"`);
    }
}

// Rebuild the table from the (possibly server-updated) rule state, keeping
// the read-only flag applied.
function _rerender() {
    const tbody = document.getElementById('ar-table-body');
    if (!tbody) return;
    tbody.innerHTML = '';
    _rows = {};
    renderRows(tbody);
    const count = document.getElementById('ar-count');
    if (count && _readOnly) {
        count.textContent = `${_rules.length} built-in rule(s) apply to this tenant`
            + ' · read-only (the API refused the write)';
    }
}

function _goReadOnly(msg) {
    _readOnly = true;
    _clearError();
    _showNotice(msg);
    _rerender();
}
