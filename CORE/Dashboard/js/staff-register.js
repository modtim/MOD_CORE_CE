// GSEC FX.153 p3 — Staff register page (platform admin only; also covers
// the FX.71 annual training checklist via the "Training due" view).
//
// Backend: routers/staff_register.py (/v1/platform/staff-records*).
// Table columns come from the real DTO (services row_to_dict): person_name,
// role, staff_type, email, training_completed_at, nda_signed_at, aup_signed_at,
// gov_cleared / us_person_confirmed flags, leaver_at + leaver_open_systems,
// created_at. There is no "background check" or "access review date" field
// server-side — the gov-cleared/US-person flags and the NDA/AUP sign-off
// dates are the closest real fields and are labelled as such.
//
// Add (POST /staff-records), Edit (PATCH), Leaver (POST /leaver), Delete
// (POST /delete — soft, ISO 27001). "Training due" toggle lists people whose
// next training date (training_completed_at + staff.training_interval_days,
// read off the /due payload) is overdue or within 30 days — the server /due
// endpoint only reports already-overdue, so the 30-day window is client-side
// date math per the task.
import { staffRegister, me, isPaidEdition } from './api.js';
import { withButtonBusy } from './utils.js';

const DUE_WINDOW_DAYS = 30;
let _intervalDays = 365;      // staff.training_interval_days (default)
let _dueOnly = false;
let _includeDeleted = false;

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 10);
}

function _showError(msg) {
    const box = document.getElementById('sr-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('sr-error');
    if (box) box.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// BUG.B63 — field sanity validators for the Add-record form. Pure functions
// (no DOM): each returns null when the value looks sane for its type, or an
// inline warning string when it looks like junk. Exported so the check test
// can exercise them without a browser.
// ---------------------------------------------------------------------------

const SR_TYPES = ['employee', 'contractor'];

export function _nameWarning(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) return null; // required-field check is separate
    const msg = "This doesn't look like a real name";
    if (v.length < 2) return msg;
    if (!/^[A-Za-zÀ-ɏ' .-]+$/.test(v)) return msg; // letters, spaces, . ' -
    if (!/[A-Za-zÀ-ɏ]/.test(v)) return msg;        // no letters at all
    const first = v[0];
    if ([...v].every(c => c === first)) return msg;         // all-same-char ("aaa")
    return null;
}

export function _emailWarning(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) return null;
    const msg = "This doesn't look like a real email";
    if (!/^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z]{2,})+$/.test(v)) return msg;
    if (/[._-]@[._-]|[._-]$/.test(v.split('@')[0])) return msg; // "foo.@bar" / ".foo"
    return null;
}

export function _roleWarning(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) return null;
    const msg = "This doesn't look like a real role";
    if (v.length < 2) return msg;
    if (!/^[A-Za-z0-9 ()/.,&'-]+$/.test(v)) return msg;
    if (!/[A-Za-z]/.test(v)) return msg;
    const first = v[0];
    if ([...v].every(c => c === first)) return msg;
    return null;
}

export function _staffTypeWarning(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) return null;
    return SR_TYPES.includes(v) ? null : 'Pick employee or contractor';
}

export function _dateWarning(value) {
    const v = String(value == null ? '' : value).trim();
    if (!v) return null;
    const msg = "This doesn't look like a real date";
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (!m) return msg;
    const y = Number(m[1]);
    const mo = Number(m[2]);
    const d = Number(m[3]);
    if (y < 1900 || y > 2199) return msg;
    if (mo < 1 || mo > 12) return msg;
    if (d < 1 || d > new Date(y, mo, 0).getDate()) return msg; // real calendar day
    return null;
}

function _setWarning(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    if (msg) { el.textContent = msg; el.classList.remove('hidden'); }
    else { el.textContent = ''; el.classList.add('hidden'); }
}

// ---------------------------------------------------------------------------
// BUG.B62 — pill/badge colours. Failure-style pills used same-colour text on a
// same-colour tint (red text on a red-tint background), which is unreadable.
// Every pill is now a solid -600 background with text-white, matching the
// BUG.B52 fix. Every class below exists in the purged tailwind.css; blue uses
// bg-sky-600 because bg-blue-600 is not in the purge.
// ---------------------------------------------------------------------------
const PILL_BASE = 'px-2 py-0.5 rounded-full text-xs';
const PILL_COLORS = {
    red: 'bg-red-600 text-white',
    amber: 'bg-amber-600 text-white',
    green: 'bg-green-600 text-white',
    gray: 'bg-gray-600 text-white',
    blue: 'bg-sky-600 text-white',
};

function pill(kind, base = PILL_BASE) {
    return `${base} ${PILL_COLORS[kind] || PILL_COLORS.gray}`;
}

// Client-side due math: next training = training_completed_at + interval.
// Returns {label, cls, days} (days = days until due; negative = overdue).
function _dueInfo(row) {
    if (row.deleted_at) return { days: null, label: '—', cls: PILL_COLORS.gray };
    if (!row.training_completed_at) {
        return { days: null, label: 'Never recorded', cls: PILL_COLORS.red };
    }
    const start = new Date(row.training_completed_at);
    if (isNaN(start.getTime())) {
        return { days: null, label: 'Date unreadable', cls: PILL_COLORS.amber };
    }
    const dueMs = start.getTime() + _intervalDays * 86400000;
    const days = Math.ceil((dueMs - Date.now()) / 86400000);
    if (days < 0) return { days, label: `Overdue ${-days}d`, cls: PILL_COLORS.red };
    if (days <= DUE_WINDOW_DAYS) return { days, label: `Due in ${days}d`, cls: PILL_COLORS.amber };
    return { days, label: `OK · ${new Date(dueMs).toISOString().slice(0, 10)}`, cls: PILL_COLORS.green };
}

function _isDue(row) {
    if (row.deleted_at) return false;
    const info = _dueInfo(row);
    return info.days === null || info.days <= DUE_WINDOW_DAYS;
}

// ---------------------------------------------------------------------------
// init + controls
// ---------------------------------------------------------------------------

export async function initializeStaffRegisterPage() {
    const dueToggle = document.getElementById('sr-due-toggle');
    if (dueToggle) {
        dueToggle.addEventListener('click', () => {
            _dueOnly = !_dueOnly;
            dueToggle.classList.toggle('bg-amber-100');
            loadTable();
        });
    }
    const delToggle = document.getElementById('sr-deleted-toggle');
    if (delToggle) {
        delToggle.addEventListener('click', () => {
            _includeDeleted = !_includeDeleted;
            delToggle.classList.toggle('bg-gray-200');
            loadTable();
        });
    }
    const addBtn = document.getElementById('sr-add-btn');
    if (addBtn) addBtn.addEventListener('click', openAddModal);
    await loadTable();
}

async function loadTable() {
    // 2400LR-g3-ce-denied-404s: staff_register.py denied on CE (manifest:1341); fail closed on unknown or errored edition.
    let paid = true;
    try { paid = await isPaidEdition(); } catch (_) { paid = false; }   // fail closed: an errored lookup reads as CE
    if (!paid) {
        const tbody = document.getElementById('sr-table-body');
        const count = document.getElementById('sr-count');
        if (tbody) tbody.innerHTML = '';
        if (count) count.textContent = '';
        ['sr-list', 'sr-empty', 'sr-count', 'sr-add-btn', 'sr-add-card', 'sr-edit-card',
            'sr-due-toggle', 'sr-deleted-toggle'].forEach(id => document.getElementById(id)?.classList.add('hidden'));
        return;
    }
    _clearError();
    const tbody = document.getElementById('sr-table-body');
    const empty = document.getElementById('sr-empty');
    if (!tbody) return;
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '<tr><td colspan="9" class="px-4 py-6 text-gray-500">Loading…</td></tr>';
    try {
        const [list, due, user] = await Promise.all([
            staffRegister.list({ include_deleted: _includeDeleted, limit: 500 }),
            staffRegister.due(),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false) {
            // The nav hides the page for non-admins; a deep link still must
            // not render the register (the API 403s anyway — belt and braces).
            tbody.innerHTML = '';
            _showError('Staff register is platform-admin only.');
            return;
        }
        _intervalDays = Number(due && due.training_interval_days) || _intervalDays;
        let items = (list && list.items) || [];
        if (_dueOnly) items = items.filter(_isDue);
        const count = document.getElementById('sr-count');
        if (count) {
            const dueN = items.filter(_isDue).length;
            const total = (list && list.total != null) ? list.total : items.length;
            count.textContent = _dueOnly
                ? `${dueN} training-due of ${total} record(s)`
                : `${total} record(s) · ${dueN} training-due`;
        }
        if (!items.length) {
            tbody.innerHTML = '';
            if (empty) empty.classList.remove('hidden');
            return;
        }
        tbody.innerHTML = items.map(rowHtml).join('');
        tbody.querySelectorAll('[data-sr-action]').forEach(btn => {
            btn.addEventListener('click', () =>
                handleAction(btn.dataset.srAction, Number(btn.dataset.srId)));
        });
    } catch (e) {
        tbody.innerHTML = '';
        _showError(_errText(e) || 'Failed to load the staff register');
    }
}

function _errText(e) {
    // Verbatim server detail (string as-is; FastAPI 422 arrays joined as
    // "field: msg" pairs — same shape as account.js _formatScError).
    if (!e) return '';
    const detail = e.detail !== undefined ? e.detail : e.message;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
        return detail.map(x => {
            if (!x || typeof x !== 'object') return String(x);
            const loc = Array.isArray(x.loc) ? x.loc.filter(p => p !== 'body').join('.') : '';
            return loc ? `${loc}: ${x.msg || 'invalid'}` : (x.msg || JSON.stringify(x));
        }).join('; ');
    }
    if (detail && typeof detail === 'object') {
        try { return JSON.stringify(detail); } catch { return 'Unknown error'; }
    }
    return (e && e.message) || 'Unknown error';
}

function rowHtml(r) {
    const due = _dueInfo(r);
    const flagBase = 'px-1 py-0.5 rounded-full text-xs';
    const flags = `${r.gov_cleared ? `<span class="${pill('blue', flagBase)}">gov-cleared</span> ` : ''}${r.us_person_confirmed ? `<span class="${pill('blue', flagBase)}">US-person</span>` : ''}`;
    const leaver = r.leaver_at
        ? `<span class="${pill(r.leaver_complete ? 'green' : 'red')}">leaver ${_fmt(r.leaver_at)}${r.leaver_open_systems && r.leaver_open_systems.length ? ` · open: ${esc(r.leaver_open_systems.join(', '))}` : ' · complete'}</span>`
        : '—';
    const signoffs = `${r.nda_signed_at ? `NDA ${_fmt(r.nda_signed_at)} ` : 'NDA — '}${r.aup_signed_at ? `· AUP ${_fmt(r.aup_signed_at)}` : '· AUP —'}`;
    const actions = r.deleted_at
        ? `<span class="text-xs text-gray-400">deleted ${_fmt(r.deleted_at)}</span>`
        : [
            `<button type="button" data-sr-action="edit" data-sr-id="${r.id}" class="text-blue-700 hover:underline text-sm">Edit</button>`,
            `<button type="button" data-sr-action="leaver" data-sr-id="${r.id}" class="text-amber-700 hover:underline text-sm">Start leaver</button>`,
            `<button type="button" data-sr-action="delete" data-sr-id="${r.id}" class="text-red-700 hover:underline text-sm">Delete</button>`,
        ].join(' ');
    return `<tr class="hover:bg-gray-50">
        <td class="px-4 py-3 font-mono text-xs">${r.id}</td>
        <td class="px-4 py-3">${esc(r.person_name)}${r.email ? `<br><span class="text-xs text-gray-400">${esc(r.email)}</span>` : ''}</td>
        <td class="px-4 py-3">${esc(r.role || '—')}</td>
        <td class="px-4 py-3">${esc(r.staff_type || '—')}</td>
        <td class="px-4 py-3 whitespace-nowrap">${_fmt(r.created_at)}</td>
        <td class="px-4 py-3">${flags || '—'}</td>
        <td class="px-4 py-3 whitespace-nowrap">${signoffs}</td>
        <td class="px-4 py-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded-full text-xs ${due.cls}">${esc(due.label)}</span></td>
        <td class="px-4 py-3">${leaver}</td>
        <td class="px-4 py-3 text-right whitespace-nowrap">${actions}</td>
    </tr>`;
}

// ---------------------------------------------------------------------------
// Add record (modal card, same inline-modal pattern as access-reviews.js)
// ---------------------------------------------------------------------------

function openAddModal() {
    const card = document.getElementById('sr-add-card');
    if (!card) return;
    card.classList.remove('hidden');
    ['sr-add-name', 'sr-add-email', 'sr-add-role', 'sr-add-type', 'sr-add-training']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
    ['sr-warn-name', 'sr-warn-email', 'sr-warn-role', 'sr-warn-type', 'sr-warn-training']
        .forEach(id => _setWarning(id, null));
    const confirm = document.getElementById('sr-add-confirm');
    if (confirm) confirm.checked = false;
    const save = document.getElementById('sr-add-save');
    if (save && !save.dataset.wired) {
        save.dataset.wired = '1';
        save.addEventListener('click', handleAdd);
    }
    if (save) save.disabled = false;
    const cancel = document.getElementById('sr-add-cancel');
    if (cancel && !cancel.dataset.wired) {
        cancel.dataset.wired = '1';
        cancel.addEventListener('click', () => card.classList.add('hidden'));
    }
}

async function handleAdd() {
    _clearError();
    const name = (document.getElementById('sr-add-name')?.value || '').trim();
    if (!name) { _showError('Person name is required.'); return; }
    const email = (document.getElementById('sr-add-email')?.value || '').trim();
    const role = (document.getElementById('sr-add-role')?.value || '').trim();
    const type = (document.getElementById('sr-add-type')?.value || 'employee');
    const training = (document.getElementById('sr-add-training')?.value || '').trim();
    const warnings = [
        ['sr-warn-name', _nameWarning(name)],
        ['sr-warn-email', _emailWarning(email)],
        ['sr-warn-role', _roleWarning(role)],
        ['sr-warn-type', _staffTypeWarning(type)],
        ['sr-warn-training', _dateWarning(training)],
    ];
    warnings.forEach(([id, msg]) => _setWarning(id, msg));
    const junk = warnings.filter(([, msg]) => msg);
    const confirm = document.getElementById('sr-add-confirm');
    if (junk.length && !(confirm && confirm.checked)) {
        _showError(`${junk.length} field(s) look wrong — fix them, or tick "Save anyway — I confirm this data is correct" to save as typed.`);
        return;
    }
    const btn = document.getElementById('sr-add-save');
    try {
        const payload = {
            person_name: name,
            email: email || null,
            role: role || null,
            staff_type: type,
            training_completed_at: training || null,
        };
        await withButtonBusy(btn, async () => {
            await staffRegister.create(payload);
        });
        document.getElementById('sr-add-card')?.classList.add('hidden');
        if (window.showInfo) window.showInfo(`Staff record for ${name} created.`, 'Staff register');
        await loadTable();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || 'Failed to create staff record');
    }
}

// ---------------------------------------------------------------------------
// Row actions: edit (PATCH), leaver (POST /leaver), delete (POST /delete)
// ---------------------------------------------------------------------------

async function handleAction(action, id) {
    _clearError();
    if (action === 'leaver') {
        try {
            await staffRegister.startLeaver(id, null);
            if (window.showInfo) window.showInfo(`Leaver checklist started for record #${id}.`, 'Staff register');
        } catch (e) {
            _showError(_errText(e) || `Failed to start leaver checklist for record #${id}`);
            return;
        }
        await loadTable();
        return;
    }
    if (action === 'delete') {
        const ok = confirm(`Soft-delete staff record #${id}? The row stays in the compliance record (ISO 27001 — nothing is hard-deleted).`);
        if (!ok) return;
        try {
            await staffRegister.softDelete(id);
            if (window.showInfo) window.showInfo(`Staff record #${id} soft-deleted.`, 'Staff register');
        } catch (e) {
            _showError(_errText(e) || `Failed to delete record #${id}`);
            return;
        }
        await loadTable();
        return;
    }
    if (action === 'edit') openEditCard(id);
}

function openEditCard(id) {
    const card = document.getElementById('sr-edit-card');
    if (!card) return;
    card.classList.remove('hidden');
    const idEl = document.getElementById('sr-edit-id');
    if (idEl) idEl.textContent = `Record #${id}`;
    ['sr-edit-name', 'sr-edit-email', 'sr-edit-role', 'sr-edit-type', 'sr-edit-training']
        .forEach(fid => { const el = document.getElementById(fid); if (el) el.value = ''; });
    const save = document.getElementById('sr-edit-save');
    if (save) save.disabled = false;
    const cancel = document.getElementById('sr-edit-cancel');
    if (cancel && !cancel.dataset.wired) {
        cancel.dataset.wired = '1';
        cancel.addEventListener('click', () => card.classList.add('hidden'));
    }
    if (save && !save.dataset.wired) {
        save.dataset.wired = '1';
        save.addEventListener('click', () => handleEditSave(id));
    }
}

async function handleEditSave(id) {
    _clearError();
    const payload = {};
    const name = (document.getElementById('sr-edit-name')?.value || '').trim();
    if (name) payload.person_name = name;
    const email = (document.getElementById('sr-edit-email')?.value || '').trim();
    if (email) payload.email = email;
    const role = (document.getElementById('sr-edit-role')?.value || '').trim();
    if (role) payload.role = role;
    const type = (document.getElementById('sr-edit-type')?.value || '').trim();
    if (type) payload.staff_type = type;
    const training = (document.getElementById('sr-edit-training')?.value || '').trim();
    if (training) payload.training_completed_at = training;
    if (!Object.keys(payload).length) { _showError('Nothing to change — fill at least one field.'); return; }
    const btn = document.getElementById('sr-edit-save');
    try {
        await withButtonBusy(btn, async () => {
            await staffRegister.update(id, payload);
        });
        document.getElementById('sr-edit-card')?.classList.add('hidden');
        if (window.showInfo) window.showInfo(`Staff record #${id} updated.`, 'Staff register');
        await loadTable();
    } catch (e) {
        if (btn) btn.disabled = false;
        _showError(_errText(e) || `Failed to update record #${id}`);
    }
}
