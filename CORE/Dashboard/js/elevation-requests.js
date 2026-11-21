// GSEC FX.120 p5a — privileged production-elevation requests (platform admin
// only), the page over the p2a data layer + p2b approval policy.
//
// Backend: CORE/API/routers/platform_elevation.py (mounted at /v1):
//   POST /platform/elevation-requests            {scope, reason, ttl_minutes?}
//        scope = host_shell|db_admin|vault_admin|docker (the SERVICE names the
//        four in its 422, so scope is sent as a plain string), reason >= 10
//        chars (the DB's own CHECK), ttl_minutes optional 1..10080 (the
//        tighter platform max security.elevation_max_ttl_minutes is enforced
//        server-side too). Files a 'pending' row + bells the OTHER platform
//        admins.
//   GET  /platform/elevation-requests            (?all=&limit=)
//        mine by default -> {mode:'mine', items[], counts{}};
//        ?all=true -> {mode:'pending_for_approval', ...} — every LIVE pending
//        row filed by platform staff, the queue the maker-checker needs.
//   POST /platform/elevation-requests/{id}/approve   (no body)
//   POST /platform/elevation-requests/{id}/deny      {reason REQUIRED}
//   POST /platform/elevation-requests/{id}/revoke    {reason?} — the body
//        field is optional so a bare POST is expressible, but the SERVICE
//        requires one, so a missing reason is a 422.
// Row shape (services/privileged_elevation._row_to_dict): id, requested_by_
// user_id, scope, reason, requested_ttl_minutes, status (pending|approved|
// denied|active|expired|revoked), approved_by_user_id, self_approved,
// approved_at, activated_at, expires_at, revoked_at, revoke_reason, created_at.
//
// THE SERVER DECIDES, THIS PAGE ONLY REPORTS. The maker-checker (the approver/
// decider must be a DIFFERENT platform admin unless security.elevation_solo_mode
// is on) is a server rule: a refused self-approval is a 403 whose detail is
// shown VERBATIM in el-error — nothing here guesses whether a decision is
// legal, and no button is hidden because of a client-side hunch (pending rows
// get Approve/Deny, approved/active rows get Revoke, which is just the
// state machine's own shape).
//
// Fresh auth (GY.C4b): approve/deny/revoke also carry require_fresh_auth, so a
// stale login answers 401 {detail:{error:'reauth_required', reauth_max_age_s}}.
// js/api.js already recognises that inside the ONE central fetchApi() every
// call passes through — it bounces the browser to Keycloak with prompt=login
// and throws an ApiError whose message says a recent login is required — so
// this page surfaces that message and unhides the "Re-authenticate now"
// button, which is wired to auth.js's handleLogin, the same re-login path
// ui.js uses for the "login-button".
//
// A 403 that says the actor is not platform staff flips the whole page read-
// only (notice + every remaining action button disabled + aria-disabled), the
// same belt-and-braces idiom as security-alerts.js / alert-rules.js, because
// the nav only mirrors the require_platform_admin gate and a deep link
// (#elevation-requests) still reaches this module.
//
// a11y (FX.68): every control is a native <button> (keyboard-operable, no
// tabindex), every generated action button carries an aria-label naming the
// row it acts on, and the visible focus ring is style.css's :focus-visible
// rule. Every server string (reason, scope, revoke_reason, ids) is written
// with textContent — innerHTML never carries data.
import { fetchApi, me } from './api.js';
import { handleLogin } from './auth.js';
import { withButtonBusy } from './utils.js';

// The router's six routes, shaped exactly like the api.js helper objects the
// neighbour pages use (ipBlocks / releaseApprovals / securityAlerts) — kept
// HERE rather than added to api.js so this module stays self-contained and
// the shared api.js file is not touched. fetchApi is the ONE central client
// every page passes through, which is where the GY.C4b reauth_required 401
// is recognised (see the header above).
const elevationRequests = {
    list: (opts = {}) => {
        const q = new URLSearchParams();
        if (opts.all) q.set('all', 'true');
        if (opts.limit) q.set('limit', String(opts.limit));
        const qs = q.toString();
        return fetchApi(`/platform/elevation-requests${qs ? `?${qs}` : ''}`);
    },
    get: (requestId) => fetchApi(
        `/platform/elevation-requests/${encodeURIComponent(requestId)}`),
    create: (payload) => fetchApi('/platform/elevation-requests',
        { method: 'POST', body: JSON.stringify(payload) }),
    approve: (requestId) => fetchApi(
        `/platform/elevation-requests/${encodeURIComponent(requestId)}/approve`,
        { method: 'POST' }),
    deny: (requestId, reason) => fetchApi(
        `/platform/elevation-requests/${encodeURIComponent(requestId)}/deny`,
        { method: 'POST', body: JSON.stringify({ reason }) }),
    revoke: (requestId, reason = null) => fetchApi(
        `/platform/elevation-requests/${encodeURIComponent(requestId)}/revoke`,
        { method: 'POST', body: JSON.stringify({ reason }) }),
};

let _mine = [];
let _pending = [];
let _readOnly = false;
let _rows = {};        // `${queue}:${id}` -> {btns:{action:btn}, tr}
let _reloginShown = false;

const PAGE_LIMIT = 200;   // the router's own limit bound is 1..500

function _showError(msg) {
    const box = document.getElementById('el-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('el-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('el-notice');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('el-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail (plain strings for this router's 403/404/409/500s;
// the standard FastAPI field-error list is flattened like the other pages).
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

// The GY.C4b fresh-auth answer, recognised off the ApiError api.js throws
// (api.js has already started the Keycloak prompt=login bounce by then).
function _isReauth(e) {
    const d = e && e.detail;
    return !!d && typeof d === 'object' && d.error === 'reauth_required';
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}

// The window a row actually runs: 'activated_at' is the start (the moment the
// grant took effect), falling back to the decision clock on an approved-but-
// not-yet-active row; 'expires_at' is the end. A pending row has no window yet.
function _window(row) {
    const start = row.activated_at || (row.status === 'pending' ? null : row.approved_at);
    return { start, end: row.expires_at };
}

// Human "time remaining" for the live window only. Nothing is invented: no
// expires_at (pending / denied / revoked / expired) -> no countdown.
function _remaining(row) {
    if (!row.expires_at) return { text: '—', cls: 'text-gray-500' };
    const end = new Date(row.expires_at);
    if (isNaN(end.getTime())) return { text: 'unreadable', cls: 'text-gray-500' };
    const mins = Math.round((end.getTime() - Date.now()) / 60000);
    if (mins <= 0) return { text: 'window lapsed', cls: 'text-red-700' };
    if (mins < 60) return { text: `${mins}m left`, cls: 'text-gray-700' };
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return { text: `${hrs}h ${mins % 60}m left`, cls: 'text-gray-700' };
    return { text: `${Math.floor(hrs / 24)}d ${hrs % 24}h left`, cls: 'text-gray-700' };
}

function _statusCls(status) {
    return status === 'pending' ? 'bg-amber-100 text-amber-800'
        : status === 'approved' || status === 'active' ? 'bg-green-100 text-green-700'
        : status === 'denied' || status === 'expired' ? 'bg-red-100 text-red-700'
        : status === 'revoked' ? 'bg-orange-100 text-orange-800'
        : 'bg-gray-200 text-gray-600';
}

// Page text for the row's action cell: pending rows can be decided (approve/
// deny), approved-or-active rows can be revoked early, terminal rows (denied/
// expired/revoked) can do nothing — that is the state machine's own shape,
// not a guess about whether the SERVER will accept the call.
function _allowedActions(status) {
    if (status === 'pending') return ['approve', 'deny'];
    if (status === 'approved' || status === 'active') return ['revoke'];
    return [];
}

// ---------------------------------------------------------------------------
// init + load. Two reads, both platform-admin only: the operator's own rows
// (GET with no params -> mode 'mine') and the pending-for-approval queue
// (GET ?all=true -> mode 'pending_for_approval'). The page shows the SERVER's
// `mode` in its count line rather than assuming which queue it got.
// ---------------------------------------------------------------------------

export async function initializeElevationRequestsPage() {
    _wireFormButtons();
    await loadRows();
}

function _wireFormButtons() {
    const newBtn = document.getElementById('el-new-btn');
    if (newBtn && !newBtn.dataset.wired) {
        newBtn.dataset.wired = '1';
        newBtn.addEventListener('click', () => { handleCreate(); });
    }
    // The re-login button ships inside a hidden panel: wiring it here is
    // harmless (a hidden control is not reachable by click or Tab), and
    // _handleReauth() is what unhides the panel once a decision actually
    // answers reauth_required.
    const reBtn = document.getElementById('el-relogin-btn');
    if (reBtn && !reBtn.dataset.wired) {
        reBtn.dataset.wired = '1';
        reBtn.addEventListener('click', () => { handleLogin(); });
    }
}

// The page's own field readers (the inputs live inside their <label>s).
function _field(id) {
    const el = document.getElementById(id);
    if (!el) return '';
    const v = el.value != null ? el.value : el.textContent;
    return String(v == null ? '' : v).trim();
}

async function loadRows() {
    _clearError();
    const mineBody = document.getElementById('el-mine-body');
    const pendBody = document.getElementById('el-pending-body');
    if (!mineBody || !pendBody) return;
    [_mine, _pending] = [[], []];
    _rows = {};
    let modes = {};
    try {
        const [mineRes, allRes, user] = await Promise.all([
            elevationRequests.list({ limit: PAGE_LIMIT }),
            elevationRequests.list({ all: true, limit: PAGE_LIMIT }),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false) {
            // The nav hides the page for non-admins; a deep link still must
            // not render the queue (the API 403s anyway — belt and braces).
            mineBody.innerHTML = '';
            pendBody.innerHTML = '';
            _showError('Production elevation requests are platform-admin only.');
            return;
        }
        _mine = (mineRes && mineRes.items) || [];
        _pending = (allRes && allRes.items) || [];
        modes = { mine: mineRes && mineRes.mode, all: allRes && allRes.mode };
    } catch (e) {
        if (_isReauth(e)) { _handleReauth('reading'); return; }
        // The router's own gate words ("Platform admin only") and the service's
        // ("production elevation is platform-admin only") both mean the same
        // thing here: this actor cannot decide elevations at all.
        if (/platform[\s -]admin only/i.test(_errText(e))) { _goReadOnly(_errText(e)); return; }
        _showError(_errText(e) || 'Failed to load the elevation requests');
        _mine = [];
        _pending = [];
    }
    _renderAll(modes);
}

function _renderAll(modes) {
    const mineBody = document.getElementById('el-mine-body');
    const pendBody = document.getElementById('el-pending-body');
    _rows = {};
    if (mineBody) {
        mineBody.innerHTML = '';
        _mine.forEach((r) => { mineBody.appendChild(_rowNode('mine', r)); });
    }
    if (pendBody) {
        pendBody.innerHTML = '';
        // The pending queue is the queue every platform admin needs to SEE in
        // order to decide it, so it is rendered for the same rows the API put
        // in it — including rows the operator filed themselves (they are the
        // ones the maker-checker will refuse to self-approve, and the server's
        // 403 says so).
        _pending.forEach((r) => { pendBody.appendChild(_rowNode('pending', r)); });
    }
    const count = document.getElementById('el-count');
    if (count) count.textContent = _countText(modes);
    // Explicit add/remove (classList.toggle would flip the hidden state of
    // every other class the shipped page carries on these panels).
    // The empty panels show only when their own table has no rows (explicit
    // add/remove — classList.toggle would flip the other classes the shipped
    // page carries on these panels).
    const emptyMine = document.getElementById('el-empty-mine');
    if (emptyMine) {
        if (_mine.length) emptyMine.classList.add('hidden');
        else emptyMine.classList.remove('hidden');
    }
    const emptyPend = document.getElementById('el-empty-pending');
    if (emptyPend) {
        if (_pending.length) emptyPend.classList.add('hidden');
        else emptyPend.classList.remove('hidden');
    }
}

// What the API actually returned, including its own `mode` for each read, so
// the operator can see the pending queue really was the ?all=true read.
function _countText(modes = {}) {
    const bits = [`${_mine.length} own row(s) (mode ${modes.mine || 'mine'})`,
        `${_pending.length} pending(s) awaiting a decision (mode `
        + `${modes.all || 'pending_for_approval'})`];
    const counts = {};
    _mine.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
    const perState = Object.keys(counts).map((k) => `${k} ${counts[k]}`).join(', ');
    if (perState) bits.push(`own rows by state: ${perState}`);
    if (_readOnly) bits.push('read-only (the API refused with 403)');
    return bits.join(' · ');
}

// ---------------------------------------------------------------------------
// One row. Every server string (reason, scope, revoke_reason, ids) goes
// through textContent — never innerHTML — so a hostile reason string cannot
// inject markup. Action buttons are native <button>s carrying aria-labels
// that name the row, so a screen reader announces "Approve elevation 41"
// rather than a bare "Approve".
// ---------------------------------------------------------------------------

function _rowNode(queue, row) {
    const idp = `el-${queue}-${row.id}`;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    const w = _window(row);
    const rem = _remaining(row);

    const cells = [
        [String(row.id), 'px-4 py-3 whitespace-nowrap font-mono text-xs'],
        [row.scope, 'px-4 py-3 whitespace-nowrap text-xs'],
    ];
    cells.forEach((c) => tr.appendChild(_td(c[0], c[1])));

    // State: chip + the same word as plain cell text (a contrast-safe copy
    // always exists even if a chip colour fails in the dark theme).
    const stTd = document.createElement('td');
    stTd.className = 'px-4 py-3 whitespace-nowrap';
    const chip = _badge(row.status || '—', _statusCls(row.status));
    chip.setAttribute('aria-hidden', 'true');
    stTd.appendChild(chip);
    const stPlain = document.createElement('span');
    stPlain.className = 'text-xs text-gray-600 ml-2';
    stPlain.textContent = row.status || '—';
    stTd.appendChild(stPlain);
    if (row.self_approved) {
        const flag = document.createElement('span');
        flag.className = 'text-xs text-gray-600 ml-2';
        flag.textContent = 'self-approved';
        stTd.appendChild(flag);
    }
    tr.appendChild(stTd);

    tr.appendChild(_td(row.reason, 'px-4 py-3 text-xs'));
    tr.appendChild(_td(row.requested_by_user_id != null
        ? `#${row.requested_by_user_id}` : '—', 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(row.approved_by_user_id != null
        ? `#${row.approved_by_user_id}` : '—', 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(_fmt(w.start), 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(_fmt(w.end), 'px-4 py-3 whitespace-nowrap text-xs'));

    const remTd = document.createElement('td');
    remTd.className = `px-4 py-3 whitespace-nowrap ${rem.cls}`;
    remTd.textContent = rem.text;
    tr.appendChild(remTd);

    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 text-right whitespace-nowrap';
    const btns = {};
    const allowed = _readOnly ? [] : _allowedActions(row.status);
    const labels = { approve: 'Approve', deny: 'Deny', revoke: 'Revoke' };
    allowed.forEach((action) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.id = `${idp}-${action}`;
        btn.dataset.elAction = action;
        btn.dataset.elId = String(row.id);
        btn.dataset.elQueue = queue;
        btn.setAttribute('aria-label', `${labels[action]} elevation request ${row.id}`);
        btn.textContent = labels[action];
        btn.className = action === 'approve'
            ? 'text-green-700 hover:underline text-sm'
            : 'text-red-700 hover:underline text-sm';
        actTd.appendChild(btn);
        btns[action] = btn;
    });
    if (!allowed.length) {
        const done = document.createElement('span');
        done.className = 'text-xs text-gray-500';
        done.textContent = row.status === 'pending'
            ? 'read-only (403)' : `${row.status || '—'} — decided`;
        actTd.appendChild(done);
    }
    tr.appendChild(actTd);

    _rows[`${queue}:${row.id}`] = { btns, tr, row };
    Object.keys(btns).forEach((action) => {
        btns[action].addEventListener('click', () => { handleAction(action, row.id); });
    });
    return tr;
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
// Filing a request. The body is exactly the create DTO: {scope, reason,
// ttl_minutes?}. ttl_minutes is omitted when the field is blank so the
// platform default (security settings) applies; a non-numeric entry is
// refused HERE (the DTO would 422 it as a string) with a plain message.
// ---------------------------------------------------------------------------

async function handleCreate() {
    _clearError();
    if (_readOnly) {
        _showError('This page is read-only: the API refused elevation with 403.');
        return;
    }
    const scope = _field('el-new-scope');
    const reason = _field('el-new-reason');
    const ttlRaw = _field('el-new-ttl');
    if (!scope) { _showError('Pick a scope: host_shell, db_admin, vault_admin or docker.'); return; }
    if (reason.length < 10) {
        _showError('A reason is required (at least 10 characters) — the server refuses a blank one.');
        return;
    }
    let ttl = null;
    if (ttlRaw) {
        if (!/^\d+$/.test(ttlRaw)) {
            _showError('Minutes held must be a whole number (or leave it blank for the platform default).');
            return;
        }
        ttl = parseInt(ttlRaw, 10);
    }
    const body = { scope, reason };
    if (ttl != null) body.ttl_minutes = ttl;
    const btn = document.getElementById('el-new-btn');
    const label = `File an elevation request for ${scope} (${reason})${ttl != null ? ` held ${ttl} minutes` : ' for the platform-default window'}? `
        + 'It opens as pending and only a DIFFERENT platform admin can approve it.';
    if (!confirm(label)) return;
    try {
        const row = btn
            ? await withButtonBusy(btn, async () => elevationRequests.create(body))
            : await elevationRequests.create(body);
        if (row && row.id != null) {
            _showNotice(`Elevation request ${row.id} filed — pending, waiting for a `
                + `different platform admin. ${_fmt(row.created_at)}.`);
        }
        await loadRows();
    } catch (e) {
        if (btn) btn.disabled = false;
        if (_isReauth(e)) { _handleReauth('filing'); return; }
        // 422 (bad scope / short reason / TTL over the platform max) and 403
        // (not platform staff) are the server's own words, shown verbatim.
        _showError(_errText(e) || 'Failed to file the elevation request');
        if (/platform[\s -]admin only/i.test(_errText(e))) _goReadOnly(_errText(e));
    }
}

// ---------------------------------------------------------------------------
// Row decisions. Each is behind confirm() whose copy spells out the maker-
// checker rule so the operator sees the refusal BEFORE clicking; the server's
// 403 (self-approval refused, solo mode off) / 409 (illegal transition) /
// 404 (unknown or soft-deleted id) detail is then shown verbatim. approve
// sends NO body (the click is the decision); deny sends the REQUIRED reason;
// revoke sends a reason too (the service requires one even though the DTO
// field is optional).
// ---------------------------------------------------------------------------

async function handleAction(action, id) {
    _clearError();
    const row = _mine.find((r) => r.id === id) || _pending.find((r) => r.id === id);
    if (!row) { _showError(`Unknown elevation request id: ${id}`); return; }
    if (_readOnly) {
        _showError('This page is read-only: the API refused elevation with 403.');
        return;
    }
    const reason = _field('el-decision-reason');
    if ((action === 'deny' || action === 'revoke') && reason.length < 3) {
        _showError('A reason is required for Deny and Revoke — the server refuses a decision without one.');
        return;
    }
    const labels = {
        approve: `Approve elevation request ${id} (${row.scope}, requested by #${row.requested_by_user_id})? `
            + 'The approver must be a DIFFERENT platform admin — the server refuses a self-approval unless security.elevation_solo_mode is on.',
        deny: `Deny elevation request ${id} (${row.scope})? Denied is terminal. Reason: ${reason}`,
        revoke: `Revoke elevation request ${id} (${row.scope}) EARLY — the row leaves 'active' and the elevation ends now. Reason: ${reason}`,
    };
    if (!confirm(labels[action])) return;
    const key = `${_rowKeyFor(row, id)}:${id}`;
    const btn = _rows[key] && _rows[key].btns[action];
    try {
        if (action === 'approve') {
            if (btn) await withButtonBusy(btn, async () => elevationRequests.approve(id));
            else await elevationRequests.approve(id);
        } else if (action === 'deny') {
            if (btn) await withButtonBusy(btn, async () => elevationRequests.deny(id, reason));
            else await elevationRequests.deny(id, reason);
        } else {
            if (btn) await withButtonBusy(btn, async () => elevationRequests.revoke(id, reason));
            else await elevationRequests.revoke(id, reason);
        }
        const done = {
            approve: `Elevation request ${id} approved — the grant's window starts on activation.`,
            deny: `Elevation request ${id} denied (terminal).`,
            revoke: `Elevation request ${id} revoked — the elevation is over.`,
        };
        _showNotice(done[action]);
        await loadRows();
    } catch (e) {
        if (btn) btn.disabled = false;
        if (_isReauth(e)) { _handleReauth(action); return; }
        // Self-approval refusal (403), an illegal transition (409), an
        // unknown id (404): the server's own sentence, never swallowed.
        _showError(_errText(e) || `Failed to update elevation request ${id}`);
        if (/platform[\s -]admin only/i.test(_errText(e))) _goReadOnly(_errText(e));
    }
}

// The row may be rendered in either table (own rows appear in both when they
// are pending), so find the table that actually carries the button.
function _rowKeyFor(row, id) {
    const mine = _rows[`mine:${id}`];
    if (mine && mine.btns) return 'mine';
    return 'pending';
}

// GY.C4b: the fresh-auth gate answered reauth_required. api.js has already
// bounced the browser to Keycloak with prompt=login; say so in the operator's
// own words, unhide the re-login button (wired to auth.js handleLogin, the
// same path ui.js uses for the login button), and retry nothing.
function _handleReauth(what) {
    _clearError();
    const verb = { approve: 'approve', deny: 'deny', revoke: 'revoke',
        reading: 'read', filing: 'file' }[what] || what;
    _showNotice(`A recent login is required to ${verb} this production elevation `
        + '(the fresh-auth gate, GY.C4b). The browser is being sent to sign in again — '
        + 'use the "Re-authenticate now" button below if it did not, then repeat the action.');
    const box = document.getElementById('el-relogin');
    if (box) box.classList.remove('hidden');
    _reloginShown = true;
}

// A 403 that says the actor is not platform staff: the nav only mirrors that
// gate, so a deep link can still get here. Flip read-only (notice + every
// remaining action button disabled + aria-disabled), same idiom as
// security-alerts.js / alert-rules.js.
function _goReadOnly(msg) {
    _readOnly = true;
    _clearError();
    _showNotice(msg || 'Production elevation is platform-admin only — you can read these rows, not decide them.');
    Object.keys(_rows).forEach((k) => {
        const btns = _rows[k].btns || {};
        Object.keys(btns).forEach((action) => {
            const btn = btns[action];
            btn.disabled = true;
            btn.setAttribute('aria-disabled', 'true');
        });
    });
    const count = document.getElementById('el-count');
    if (count) count.textContent = _countText({});
}
