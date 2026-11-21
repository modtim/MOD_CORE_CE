// GSEC FX.77 p6a — tenant log streaming (SIEM export) destinations.
// Backend: routers/tenant/log_streams.py, mounted at /v1/tenant/log-streams:
//   GET /tenant/log-streams          -> {items: [LogStreamOut], total}
//   GET /tenant/log-streams/{id}     -> LogStreamOut
//   POST /tenant/log-streams         -> LogStreamOut (201)
//   PATCH /tenant/log-streams/{id}   -> LogStreamOut
//   DELETE /tenant/log-streams/{id}  -> 204 (soft delete)
//   POST /tenant/log-streams/{id}/test -> 501 {"detail": "Planned: ..."}
// LogStreamOut fields: id, name, kind (syslog_tls|https_webhook|splunk_hec|
// s3_export), endpoint, config, enabled, event_types[] (audit|security|run|
// auth), has_secret, last_success_at, last_error_at, last_error,
// created_at, updated_at. `secret` is write-only: never present on any
// response, only `has_secret`. On update, omitting `secret` entirely keeps
// it unchanged; sending `secret: null` clears it (see validate_stream_payload /
// _check_duplicate_name for the 422/409 shapes this page surfaces). Every
// value from the API is written with textContent (never innerHTML), same
// rule as js/ai-inventory.js.
import { logStreams, isPaidEdition } from './api.js';

const ENDPOINT_PLACEHOLDERS = {
    syslog_tls: 'siem.example.com:6514',
    https_webhook: 'https://hooks.example.com/mod-siem',
    splunk_hec: 'https://splunk.example.com:8088/services/collector',
    s3_export: 's3://my-siem-bucket/mod-exports/',
};

let _streams = [];
let _editingId = null;

function _showError(msg) {
    const box = document.getElementById('ls-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('ls-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showInfo(msg) {
    const box = document.getElementById('ls-info');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearInfo() {
    const box = document.getElementById('ls-info');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail. 422s from this router are a plain list[str]
// (validate_stream_payload), not pydantic field-error objects, but this
// also tolerates the standard FastAPI shape for consistency with the
// other tenant-admin pages (same idiom as ai-inventory.js _errText).
function _errList(e) {
    if (!e) return [];
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (Array.isArray(detail)) {
        return detail.map((x) => {
            if (typeof x === 'string') return x;
            if (x && typeof x === 'object') {
                const loc = Array.isArray(x.loc) ? x.loc.filter((p) => p !== 'body').join('.') : '';
                return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
            }
            return String(x);
        });
    }
    if (typeof detail === 'string') return [detail];
    if (detail && typeof detail === 'object') {
        try { return [JSON.stringify(detail)]; } catch { return ['Unknown error']; }
    }
    return [(e && e.message) || 'Unknown error'];
}

function _showFormErrors(errs) {
    const box = document.getElementById('ls-form-errors');
    if (!box) return;
    box.textContent = '';
    if (!errs || !errs.length) { box.classList.add('hidden'); return; }
    const ul = document.createElement('ul');
    ul.className = 'list-disc pl-5';
    errs.forEach((msg) => {
        const li = document.createElement('li');
        li.textContent = msg;
        ul.appendChild(li);
    });
    box.appendChild(ul);
    box.classList.remove('hidden');
}

function _clearFormErrors() {
    const box = document.getElementById('ls-form-errors');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _td(text) {
    const td = document.createElement('td');
    td.className = 'px-4 py-3';
    td.textContent = text == null ? '—' : String(text);
    return td;
}

function _badge(text, cls) {
    const span = document.createElement('span');
    span.className = `px-2 py-0.5 rounded-full text-xs ${cls}`;
    span.textContent = text;
    return span;
}

function _badgeTd(badge) {
    const td = document.createElement('td');
    td.className = 'px-4 py-3 whitespace-nowrap';
    td.appendChild(badge);
    return td;
}

function _actionButton(label, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.className = 'text-indigo-600 hover:underline text-xs mr-3';
    btn.addEventListener('click', onClick);
    return btn;
}

// ---------------------------------------------------------------------------
// init + load
// ---------------------------------------------------------------------------

export async function initializeLogStreamsPage() {
    const newBtn = document.getElementById('ls-new-btn');
    if (newBtn) newBtn.addEventListener('click', () => openForm(null));
    const cancelBtn = document.getElementById('ls-cancel-btn');
    if (cancelBtn) cancelBtn.addEventListener('click', closeForm);
    const kindSelect = document.getElementById('ls-kind');
    if (kindSelect) kindSelect.addEventListener('change', updateEndpointPlaceholder);
    const form = document.getElementById('ls-form');
    if (form) form.addEventListener('submit', onSubmit);
    const secretField = document.getElementById('ls-secret');
    if (secretField) secretField.addEventListener('input', updateClearSecretVisibility);
    updateEndpointPlaceholder();
    await loadList();
}

async function loadList() {
    // 2400LR-g3-ce-denied-404s: routers/tenant/*.py (log_streams.py) denied on CE (manifest:1391); fail closed on unknown or errored edition.
    let paid = true;
    try { paid = await isPaidEdition(); } catch (_) { paid = false; }   // fail closed: an errored lookup reads as CE
    if (!paid) {
        const body = document.getElementById('ls-body');
        if (body) body.textContent = '';
        ['ls-list-section', 'ls-empty', 'ls-form-section', 'ls-new-btn'].forEach(id => document.getElementById(id)?.classList.add('hidden'));
        return;
    }
    _clearError();
    const body = document.getElementById('ls-body');
    const empty = document.getElementById('ls-empty');
    if (!body) return;
    if (empty) empty.classList.add('hidden');
    body.textContent = '';
    try {
        const res = await logStreams.list();
        _streams = (res && res.items) || [];
        if (!_streams.length) {
            if (empty) empty.classList.remove('hidden');
            return;
        }
        renderList(body, _streams);
    } catch (e) {
        body.textContent = '';
        _showError(_errList(e).join('; ') || 'Failed to load log streams');
    }
}

function renderList(tbody, streams) {
    streams.forEach((s) => {
        const tr = document.createElement('tr');
        tr.className = 'hover:bg-gray-50';
        tr.appendChild(_td(s.name));
        tr.appendChild(_td(s.kind));
        tr.appendChild(_td(s.endpoint));
        tr.appendChild(_badgeTd(_badge(
            s.enabled ? 'Enabled' : 'Disabled',
            s.enabled ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600')));
        tr.appendChild(_badgeTd(_badge(
            s.has_secret ? 'Set' : 'None',
            s.has_secret ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600')));
        tr.appendChild(_td(s.last_success_at));
        tr.appendChild(_td(s.last_error));

        const actionsTd = document.createElement('td');
        actionsTd.className = 'px-4 py-3 whitespace-nowrap';
        actionsTd.appendChild(_actionButton('Edit', () => openForm(s)));
        actionsTd.appendChild(_actionButton(s.enabled ? 'Disable' : 'Enable', () => toggleEnabled(s)));
        actionsTd.appendChild(_actionButton('Test connection', () => testConnection(s)));
        actionsTd.appendChild(_actionButton('Delete', () => deleteStream(s)));
        tr.appendChild(actionsTd);

        tbody.appendChild(tr);
    });
}

async function toggleEnabled(stream) {
    _clearError();
    try {
        await logStreams.update(stream.id, { enabled: !stream.enabled });
        await loadList();
    } catch (e) {
        _showError(_errList(e).join('; ') || 'Failed to update the log stream');
    }
}

async function testConnection(stream) {
    _clearError();
    _clearInfo();
    try {
        await logStreams.test(stream.id);
        _showInfo('Connection test succeeded.');
    } catch (e) {
        if (e && e.status === 501) {
            _showInfo(_errList(e)[0] || 'Planned: connection test not yet implemented');
        } else {
            _showError(_errList(e).join('; ') || 'Connection test failed');
        }
    }
}

async function deleteStream(stream) {
    _clearError();
    if (!window.confirm(`Delete the log stream "${stream.name}"? This cannot be undone.`)) return;
    try {
        await logStreams.remove(stream.id);
        await loadList();
    } catch (e) {
        _showError(_errList(e).join('; ') || 'Failed to delete the log stream');
    }
}

// ---------------------------------------------------------------------------
// create / edit form
// ---------------------------------------------------------------------------

function updateEndpointPlaceholder() {
    const kindSelect = document.getElementById('ls-kind');
    const endpointField = document.getElementById('ls-endpoint');
    if (!kindSelect || !endpointField) return;
    endpointField.placeholder = ENDPOINT_PLACEHOLDERS[kindSelect.value] || '';
}

function updateClearSecretVisibility() {
    const wrap = document.getElementById('ls-clear-secret-wrap');
    const secretField = document.getElementById('ls-secret');
    if (!wrap || !secretField) return;
    // Typing a new secret and clearing the secret are mutually exclusive.
    if (secretField.value) {
        wrap.classList.add('hidden');
        const clearBox = document.getElementById('ls-clear-secret');
        if (clearBox) clearBox.checked = false;
    }
}

function openForm(stream) {
    _clearFormErrors();
    _clearInfo();
    _editingId = stream ? stream.id : null;

    const title = document.getElementById('ls-form-title');
    if (title) title.textContent = stream ? `Edit ${stream.name}` : 'New log stream';

    document.getElementById('ls-form-id').value = stream ? stream.id : '';
    document.getElementById('ls-name').value = stream ? stream.name : '';
    document.getElementById('ls-kind').value = stream ? stream.kind : 'syslog_tls';
    document.getElementById('ls-endpoint').value = stream ? stream.endpoint : '';
    document.getElementById('ls-config').value = stream ? JSON.stringify(stream.config || {}, null, 2) : '{}';
    document.getElementById('ls-enabled').checked = stream ? !!stream.enabled : false;

    // Secret is write-only — never pre-filled, even when editing.
    const secretField = document.getElementById('ls-secret');
    secretField.value = '';
    const secretHint = document.getElementById('ls-secret-hint');
    const clearWrap = document.getElementById('ls-clear-secret-wrap');
    const clearBox = document.getElementById('ls-clear-secret');
    if (stream && stream.has_secret) {
        secretHint.classList.remove('hidden');
        clearWrap.classList.remove('hidden');
    } else {
        secretHint.classList.add('hidden');
        clearWrap.classList.add('hidden');
    }
    if (clearBox) clearBox.checked = false;

    const checkedTypes = new Set(stream ? (stream.event_types || []) : []);
    document.querySelectorAll('.ls-event-type').forEach((box) => {
        box.checked = checkedTypes.has(box.value);
    });

    updateEndpointPlaceholder();
    document.getElementById('ls-config-error').classList.add('hidden');
    document.getElementById('ls-form-section').classList.remove('hidden');
}

function closeForm() {
    _editingId = null;
    _clearFormErrors();
    document.getElementById('ls-form-section').classList.add('hidden');
}

async function onSubmit(ev) {
    ev.preventDefault();
    _clearFormErrors();
    _clearError();

    const configText = document.getElementById('ls-config').value;
    const configErrorEl = document.getElementById('ls-config-error');
    let config;
    try {
        config = configText.trim() === '' ? {} : JSON.parse(configText);
    } catch {
        configErrorEl.classList.remove('hidden');
        return;
    }
    configErrorEl.classList.add('hidden');

    const eventTypes = Array.from(document.querySelectorAll('.ls-event-type'))
        .filter((box) => box.checked)
        .map((box) => box.value);

    const payload = {
        name: document.getElementById('ls-name').value,
        kind: document.getElementById('ls-kind').value,
        endpoint: document.getElementById('ls-endpoint').value,
        config,
        event_types: eventTypes,
        enabled: document.getElementById('ls-enabled').checked,
    };

    const secretValue = document.getElementById('ls-secret').value;
    const clearSecret = document.getElementById('ls-clear-secret').checked;
    if (secretValue) {
        payload.secret = secretValue;
    } else if (clearSecret) {
        payload.secret = null;
    }
    // else: omit `secret` entirely so an edit never touches the stored one.

    try {
        if (_editingId) {
            await logStreams.update(_editingId, payload);
        } else {
            await logStreams.create(payload);
        }
        closeForm();
        await loadList();
    } catch (e) {
        if (e && e.status === 409) {
            _showFormErrors(_errList(e));
        } else if (e && e.status === 422) {
            _showFormErrors(_errList(e));
        } else {
            _showError(_errList(e).join('; ') || 'Failed to save the log stream');
        }
    }
}
