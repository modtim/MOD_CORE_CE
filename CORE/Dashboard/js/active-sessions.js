// GSEC FX.112 p4a — active-sessions page (MOD Core Dashboard).
//
// Self-service backend (routers/me/identity_tenancy.py + routers/me/auth_apikeys.py):
//   GET    /me/sessions                  -> [{id, device_info, ip_address,
//          last_active_at, created_at, keycloak_session_id}] newest
//          last_active_at first. The API has no is_current field today, but
//          its own "keep one" policy (me_auth_apikeys_service.revoke_all_other_sessions
//          keeps sessions[0] of that same ordering) is what defines the current
//          session, so row 0 is marked "This session" — an explicit is_current
//          flag is honoured first when the API ever adds one.
//   DELETE /me/sessions/{session_id}     -> 204 (audited SESSION_SIGN_OUT_SINGLE,
//          Keycloak best-effort); 404 unknown or another user's session.
//   POST   /me/signout-everywhere        (FX.112 p4b) -> {signed_out, revoked_sessions,
//          keycloak_revoked, reauth_required} — EVERY session including this one,
//          so the next request with the old token is dead (audited
//          SESSION_SIGN_OUT_ALL).
//
// Tenant-admin backend (routers/users.py):
//   GET    /users                        (USER:view) -> the tenant's users, the picker's
//          source. There is NO admin route that lists ANOTHER user's sessions, so
//          this page never invents one: for another user only the revoke action is
//          offered.
//   POST   /users/{user_id}/signout-everywhere -> same result shape + user_id;
//          tenant admins act in their own tenant (cross-tenant -> 404) and never
//          on themselves (403 — the self-service call above is for that).
//
// Controls (per-session Sign out, Sign out everywhere, the tenant-admin picker)
// are built here: native buttons, explicit labels + aria-labels, every API
// string written with textContent (no innerHTML anywhere), and a 403 on any
// write flips the whole page into the read-only view — the same belt-and-braces
// idiom as alert-rules.js / security-alerts.js, since the nav only mirrors the
// gate. Destructive actions confirm first; revoking the session you are using
// (or signing out everywhere) also stops further writes, because the token in
// this browser is dead the moment the call lands.
import { fetchApi, me } from './api.js';

let _sessions = [];
let _users = [];
let _selfId = null;
let _currentId = null;
let _readOnly = false;
let _rows = {};       // session_id -> {btn} for the live DOM row
let _canAdminRevoke = false;
let _pickerRows = {}; // user_id -> {btn}
let _signedOut = false;

function _showError(msg) {
    const box = document.getElementById('as-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('as-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('as-notice');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('as-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail (plain string for these routers' 404s/403s; the
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

function _fmtTime(iso) {
    if (!iso) return '—';
    const s = String(iso).trim();
    const dt = new Date(s);
    if (isNaN(dt.getTime())) return s;
    return `${dt.toISOString().slice(0, 16).replace('T', ' ')} UTC`;
}

// The UA string the API stored, shortened the way account.js does it, so the
// row reads "Chrome on Windows" instead of a 500-char header.
function _deviceText(ua) {
    if (!ua) return 'Unknown device';
    let os = '';
    if (/Windows/i.test(ua)) os = 'Windows';
    else if (/Mac OS X|Macintosh/i.test(ua)) os = 'macOS';
    else if (/Android/i.test(ua)) os = 'Android';
    else if (/iPhone|iPad|iOS/i.test(ua)) os = 'iOS';
    else if (/Linux/i.test(ua)) os = 'Linux';
    let browser = '';
    if (/Edg\//i.test(ua)) browser = 'Edge';
    else if (/Chrome\//i.test(ua)) browser = 'Chrome';
    else if (/Firefox\//i.test(ua)) browser = 'Firefox';
    else if (/Safari\//i.test(ua)) browser = 'Safari';
    const parts = [browser, os].filter(Boolean);
    return parts.length ? parts.join(' on ') : String(ua).slice(0, 60);
}

// The API's own definition of "the session in use": the newest-active row,
// which is exactly the row revoke_all_other_sessions keeps. An explicit
// is_current flag wins when present.
function _currentOf(list) {
    if (!list || !list.length) return null;
    const flagged = list.find((s) => s && (s.is_current === true || s.is_current === 'true'));
    return String((flagged || list[0]).id);
}

// ---------------------------------------------------------------------------
// init + load
// ---------------------------------------------------------------------------

export async function initializeActiveSessionsPage() {
    await loadSessions();
}

async function loadSessions() {
    _clearError();
    _clearNotice();
    const tbody = document.getElementById('as-table-body');
    if (!tbody) return;
    _rows = {};
    _pickerRows = {};
    tbody.innerHTML = '';
    try {
        const res = await me.getSessions();
        _sessions = Array.isArray(res) ? res : [];
    } catch (e) {
        _sessions = [];
        _showError(_errText(e) || 'Failed to load your sessions');
        const empty = document.getElementById('as-empty');
        if (empty) empty.classList.remove('hidden');
        return;
    }
    _currentId = _currentOf(_sessions);
    try {
        const perms = await me.getPermissions();
        const caps = new Set((perms || []).map((p) => p && p.name));
        // The tenant-admin revoke route reuses the USER:deactivate model
        // (services/users_router_service.sign_out_user_everywhere), so the
        // picker is offered exactly when that capability is held.
        _canAdminRevoke = caps.has('user:deactivate');
    } catch { _canAdminRevoke = false; }
    _syncPickerPanel();
    if (_canAdminRevoke) await loadPicker();
    const count = document.getElementById('as-count');
    if (count) {
        count.textContent = `${_sessions.length} session(s) on your account`
            + (_readOnly ? ' · read-only (the API refused the write)' : '');
    }
    if (!_sessions.length) {
        const empty = document.getElementById('as-empty');
        if (empty) empty.classList.remove('hidden');
    }
    renderRows(tbody);
    wireSelfButton();
}

async function loadPicker() {
    const body = document.getElementById('as-picker-body');
    if (!body) return;
    body.innerHTML = '';
    try {
        const res = await fetchApi('/users/');
        _users = Array.isArray(res) ? res : [];
    } catch (e) {
        _users = [];
        const note = document.createElement('p');
        note.className = 'text-sm text-gray-500';
        note.textContent = `Could not load the tenant's users: ${_errText(e) || 'please try again.'}`;
        body.appendChild(note);
        return;
    }
    try {
        const self = await me.get();
        _selfId = self && self.id != null ? String(self.id) : null;
    } catch { _selfId = null; }
    if (!_users.length) {
        const none = document.createElement('p');
        none.className = 'text-sm text-gray-500';
        none.textContent = 'No other users in this tenant.';
        body.appendChild(none);
        return;
    }
    _users.forEach((u) => {
        if (!u) return;
        const uid = String(u.id);
        if (uid === _selfId) return;   // the self-service button above covers you
        body.appendChild(_pickerNode(u));
    });
}

// ---------------------------------------------------------------------------
// One row: device | IP · location | started | last active | this-session | Sign out
// ---------------------------------------------------------------------------

function _rowNode(session) {
    const sid = String(session.id);
    const idp = `as-${_idKey(sid)}`;
    const isCurrent = sid === _currentId;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';

    tr.appendChild(_td(_deviceText(session.device_info),
        'px-4 py-3 whitespace-nowrap font-semibold'));
    // The API stores ip_address only; "location" is stated as unknown unless a
    // future field ships, so the column never implies geolocation we do not have.
    const locBits = [session.ip_address,
        session.location || session.country || null].filter((x) => x != null && x !== '');
    tr.appendChild(_td(locBits.length ? locBits.join(' · ') : '—',
        'px-4 py-3 text-xs'));
    tr.appendChild(_td(_fmtTime(session.created_at), 'px-4 py-3 text-xs'));
    tr.appendChild(_td(_fmtTime(session.last_active_at), 'px-4 py-3 text-xs'));

    const curTd = document.createElement('td');
    curTd.className = 'px-4 py-3 whitespace-nowrap';
    if (isCurrent) {
        const badge = _badge('This session', 'bg-blue-100 text-blue-800');
        badge.setAttribute('aria-current', 'true');
        curTd.appendChild(badge);
    } else {
        const dash = document.createElement('span');
        dash.className = 'text-xs text-gray-400';
        dash.textContent = '—';
        curTd.appendChild(dash);
    }
    tr.appendChild(curTd);

    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 text-right whitespace-nowrap';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = `${idp}-revoke`;
    btn.dataset.asAction = 'revoke';
    btn.dataset.asSessionId = sid;
    btn.textContent = isCurrent
        ? `Sign out this session (#${sid})`
        : `Sign out session #${sid}`;
    btn.className = 'text-indigo-600 hover:underline text-sm';
    actTd.appendChild(btn);
    tr.appendChild(actTd);

    _rows[sid] = { btn };
    if (btn) {
        btn.addEventListener('click', () => { handleRevoke(btn); });
    }
    if (_readOnly) _disableRow(sid);
    return tr;
}

function renderRows(tbody) {
    _sessions.forEach((s) => {
        if (s && s.id != null) tbody.appendChild(_rowNode(s));
    });
}

function _disableRow(sid) {
    const row = _rows[sid];
    if (!row) return;
    Object.values(row).forEach((el) => {
        if (!el) return;
        el.disabled = true;
        el.setAttribute('aria-disabled', 'true');
    });
}

function _pickerNode(user) {
    const uid = String(user.id);
    const idp = `as-u-${_idKey(uid)}`;
    const wrap = document.createElement('div');
    wrap.className = 'flex items-center justify-between gap-2 pb-2 mb-2';
    const who = document.createElement('span');
    who.className = 'text-sm text-gray-800';
    who.textContent = `${user.username || user.email || 'user'} (user #${uid})`;
    wrap.appendChild(who);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = `${idp}-revoke`;
    btn.dataset.asAction = 'revoke-user';
    btn.dataset.asUserId = uid;
    btn.textContent = `Sign out everywhere for user #${uid}`;
    btn.className = 'text-indigo-600 hover:underline text-sm';
    wrap.appendChild(btn);
    if (btn) {
        btn.addEventListener('click', () => { handleRevokeUser(btn); });
    }
    _pickerRows[uid] = { btn };
    if (_readOnly) _disablePicker(uid);
    return wrap;
}

function _disablePicker(uid) {
    const row = _pickerRows[uid];
    if (!row) return;
    Object.values(row).forEach((el) => {
        if (!el) return;
        el.disabled = true;
        el.setAttribute('aria-disabled', 'true');
    });
}

// ---------------------------------------------------------------------------
// Actions. Every one confirms first (these are destructive), and after the
// browser's OWN session is revoked no further write is attempted: the token in
// this tab is dead the moment the call lands (reauth_required), so the page
// says so instead of firing requests that would only 401.
// ---------------------------------------------------------------------------

export async function handleRevoke(btn) {
    const sid = btn.dataset.asSessionId;
    const session = _sessions.find((s) => s && String(s.id) === sid);
    if (!session) { _showError(`Unknown session: ${sid}`); return; }
    if (_readOnly) {
        _showError('This page is read-only: the API refused the write (403).');
        return;
    }
    const isCurrent = sid === _currentId;
    if (!confirm(isCurrent
        ? `Sign out the session you are using now? You will have to sign in again to keep using the Dashboard.`
        : `Sign out session #${sid}? That device and browser will need to sign in again.`)) return;
    _clearError();
    try {
        await me.revokeSession(sid);
    } catch (e) {
        if (e && e.status === 403) {
            _goReadOnly('You can view your sessions, but this account cannot revoke them (the API refused the write with 403).');
            return;
        }
        _showError(_errText(e) || `Failed to sign out session #${sid}`);
        return;
    }
    if (isCurrent) {
        _signedOut = true;
        const st = document.getElementById('as-self-status');
        if (st) {
            st.className = 'text-sm text-red-700';
            st.textContent = `Session #${sid} (the one you are using) is signed out. Sign in again to continue — this page is now read-only.`;
        }
        _goReadOnly('Your current session was signed out, so this page is read-only until you sign in again.');
        return;
    }
    await loadSessions();
}

export async function handleSignOutEverywhere(btn) {
    if (_readOnly) {
        _showError('This page is read-only: the API refused the write (403).');
        return;
    }
    if (!confirm('Sign out EVERYWHERE? Every session on your account, including the one you are using now, is ended and you will have to sign in again.')) return;
    _clearError();
    try {
        const res = await fetchApi('/me/signout-everywhere', { method: 'POST' });
        const n = res && typeof res.revoked_sessions === 'number'
            ? res.revoked_sessions : 0;
        const st = document.getElementById('as-self-status');
        if (st) {
            st.className = 'text-sm text-red-700';
            st.textContent = `Signed out everywhere: ${n} session(s) ended`
                + (res && res.keycloak_revoked ? ' (Keycloak sessions included)'
                    : ' (Keycloak sessions not confirmed — best-effort)')
                + (res && res.reauth_required ? ' · you must sign in again.' : '.');
        }
        _signedOut = true;
    } catch (e) {
        if (e && e.status === 403) {
            _goReadOnly('You can view your sessions, but this account cannot sign itself out here (the API refused the write with 403).');
            return;
        }
        _showError(_errText(e) || 'Failed to sign out everywhere');
        return;
    }
    // The current session is gone, so re-listing would only 401. Mark the
    // page read-only instead of storming the API.
    _goReadOnly('Every session (including this one) is signed out. Sign in again to use the Dashboard; this page is read-only until then.');
}

export async function handleRevokeUser(btn) {
    const uid = btn.dataset.asUserId;
    if (_readOnly) {
        _showError('This page is read-only: the API refused the write (403).');
        return;
    }
    if (_selfId && uid === _selfId) {
        _showError('Use the "Sign out everywhere" button above for your own account — the per-user route refuses self-targets (403).');
        return;
    }
    if (!confirm(`Sign out every session of user #${uid}? Their account stays intact — only their logins end, on every device.`)) return;
    _clearError();
    try {
        const res = await fetchApi(`/users/${uid}/signout-everywhere`, { method: 'POST' });
        const n = res && typeof res.revoked_sessions === 'number' ? res.revoked_sessions : 0;
        const st = document.getElementById('as-self-status');
        if (st) {
            st.className = 'text-sm text-gray-600';
            st.textContent = `User #${res && res.user_id != null ? res.user_id : uid}: `
                + `${n} session(s) ended (Keycloak ${res && res.keycloak_revoked ? 'revoked' : 'not confirmed'}).`;
        }
    } catch (e) {
        if (e && e.status === 403) {
            _goReadOnly('You can view the sessions list, but this account cannot revoke other users (the API refused the write with 403).');
            return;
        }
        _showError(_errText(e) || `Failed to sign out user #${uid}`);
        return;
    }
}

// Rebuild both tables from the (possibly server-updated) state, keeping the
// read-only flag applied.
function _rerender() {
    const tbody = document.getElementById('as-table-body');
    if (tbody) {
        tbody.innerHTML = '';
        _rows = {};
        renderRows(tbody);
    }
    const pbody = document.getElementById('as-picker-body');
    if (pbody && _canAdminRevoke && _users.length) {
        pbody.innerHTML = '';
        _pickerRows = {};
        _users.forEach((u) => {
            if (!u) return;
            if (String(u.id) === _selfId) return;
            pbody.appendChild(_pickerNode(u));
        });
    }
    _syncPickerPanel();
    const count = document.getElementById('as-count');
    if (count && _readOnly) {
        count.textContent = `${_sessions.length} session(s) on your account`
            + ' · read-only (the API refused the write)';
    }
    wireSelfButton();
}

// The picker panel ships in the markup (like the self-service button) but is
// only meaningful for a tenant admin, so it is hidden when USER:deactivate is
// not held — the page never shows an empty admin section to a plain user.
function _syncPickerPanel() {
    const panel = document.getElementById('as-picker');
    if (!panel) return;
    if (_canAdminRevoke) panel.classList.remove('hidden');
    else panel.classList.add('hidden');
}

function _goReadOnly(msg) {
    _readOnly = true;
    _clearError();
    _showNotice(msg);
    _rerender();
    const btn = document.getElementById('as-signout-everywhere-btn');
    if (btn) { btn.disabled = true; btn.setAttribute('aria-disabled', 'true'); }
}

// The self-service button ships in the page markup (like account.js's
// "Log out other sessions"), so it is wired once here: dataset.wired guards
// against double-listeners across re-renders, and it stays disabled while
// there is nothing to sign out, once the page is read-only, or once this very
// browser has already been signed out (its token is dead).
export function wireSelfButton() {
    const btn = document.getElementById('as-signout-everywhere-btn');
    if (!btn) return null;
    if (!btn.dataset.wired) {
        btn.dataset.wired = '1';
        btn.addEventListener('click', () => { handleSignOutEverywhere(btn); });
    }
    const dead = !_sessions.length || _readOnly || _signedOut;
    btn.disabled = dead;
    if (dead) btn.setAttribute('aria-disabled', 'true');
    else btn.removeAttribute('aria-disabled');
    return btn;
}
