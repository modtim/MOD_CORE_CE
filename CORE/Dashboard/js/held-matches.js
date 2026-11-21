// GSEC FX.74 p4 — admin review page for matches HELD for review (MOD Core Dashboard).
//
// What "held" is (FX.74 p3a): an offline OFAC SDN name/company screening match at hosted
// signup NEVER blocks the account — the account is created and HELD for review, which is
// models.UserStatus.PENDING plus one PermissionAuditLog row (action security_incident_created,
// resource_type "security", audit_metadata {kind:'sanctions_sdn_screening_hold', email,
// matches:[{ent_num,score,field}]}). There is NO dedicated held-match API, so this page is
// built from the routes that already exist and invents no endpoint:
//
//   GET  /users/?tenant_id=N          (routers/users.py, require_capability(USER,'view');
//        a platform admin may name another tenant, anyone else gets their own — the service
//        says so in its 403). Rows are schemas.User: id, email, first_name, last_name,
//        tenant_id, status (pending|active|inactive|deactivated), is_platform_admin.
//   GET  /audit-logs/permissions?action=security_incident_created&resource_type=security
//        &limit=&offset=              (routers/audit_logs.py; the HOLD EVIDENCE — metadata
//        carries email + matches. Gated behind audit-integrity capability, so a 402 here means "no
//        evidence column for this tenant", not a dead page: the accounts still list.)
//   PATCH /users/{id}/reactivate     RELEASE  (status -> ACTIVE; audited ADMIN_USER_MODIFY)
//   PATCH /users/{id}/deactivate     REJECT   (soft delete + API-key revocation; audited)
//   PATCH /users/{id} {status:'pending'}
//        ESCALATE (re-holds an account that was already released. schemas.UserUpdate is a
//        STRICT model — an extra body field is a 422 — so the reason CANNOT ride here; it
//        goes into the incident below instead.)
//   POST /platform/security-incidents {severity:'high', summary:'<reason>', detected_at}
//        (routers/platform_security_incidents.py, require_platform_admin) — Escalate ALSO
//        files one so the escalation has a trackable record whose summary IS the reason. A
//        tenant admin is refused THERE by that router's own 403, shown verbatim; the re-hold
//        that already landed is reported, never swallowed.
//
// THE SERVER DECIDES, THIS PAGE ONLY REPORTS. Release/Reject/Escalate are offered by the
// state machine's own shape (a pending row can be released or rejected; an active row can be
// rejected or escalated; a deactivated row can be released or escalated) — nothing here
// guesses whether the SERVER will accept the call, and a refusal (403 no capability, 404
// unknown/cross-tenant, 409 illegal transition) is printed verbatim in hm-error.
//
// The API guard is the USER route's own: USER:view for the list and USER:reactivate /
// USER:deactivate for the decisions (platform admins bypass it inside
// services/users_router_service.resolve_target_user_for_admin). The nav mirrors the TENANT-
// admin half of that (isSuperAdmin || isPlatformAdmin) in js/ui.js, and a 403 on a write
// flips this page read-only, so a deep link (#held-matches) is never decisive.
//
// PII: the evidence columns render EXACTLY what the API returns — email, ent_num, sdn_name,
// score, field, and the row's own email/name/status/tenant. No field is invented and nothing
// is concatenated from an unrelated source, so what the operator reads is what the API said.
// Every server string is written with textContent; innerHTML never carries data.
//
// a11y (FX.68): every control is a native <button> or a native <select>/<input> inside its
// own <label>, every generated action button carries an aria-label naming the row it acts on,
// no positive tabindex anywhere, and the visible focus ring is style.css's :focus-visible rule.
import { fetchApi, me, isPaidEdition } from './api.js';
import { handleLogin } from './auth.js';
import { withButtonBusy } from './utils.js';

// The routes above, shaped like the api.js helper objects the neighbour pages use — kept HERE
// (not added to api.js) so this module stays self-contained and the shared file is untouched.
// fetchApi is the ONE central client every page passes through, which is where the GY.C4b
// reauth_required 401 is recognised.
const heldMatches = {
    listUsers: (tenantId = null) => fetchApi(
        `/users/${tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''}`),
    evidence: (limit = 200, offset = 0) => fetchApi(
        `/audit-logs/permissions?action=${encodeURIComponent('security_incident_created')}`
        + `&resource_type=${encodeURIComponent('security')}`
        + `&limit=${limit}&offset=${offset}`),
    release: (userId) => fetchApi(`/users/${userId}/reactivate`, { method: 'PATCH' }),
    reject: (userId) => fetchApi(`/users/${userId}/deactivate`, { method: 'PATCH' }),
    escalate: (userId) => fetchApi(`/users/${userId}`, {
        method: 'PATCH', body: JSON.stringify({ status: 'pending' }) }),
    incident: (payload) => fetchApi('/platform/security-incidents', {
        method: 'POST', body: JSON.stringify(payload) }),
};

let _users = [];
let _evidence = [];
let _rows = {};        // user id -> {btns:{action:btn}, tr}
let _readOnly = false;
let _evidenceBlocked = false;
let _reloginShown = false;

const PAGE_LIMIT = 200;   // the audit route's own bound is le=1000; 200 is the page window

const _stateFilter = () => {
    const sel = document.getElementById('hm-status-filter');
    const v = sel && sel.value != null ? String(sel.value).trim() : '';
    return ['pending', 'active', 'deactivated'].includes(v) ? v : '';
};

const _fieldFilter = () => {
    const sel = document.getElementById('hm-field-filter');
    const v = sel && sel.value != null ? String(sel.value).trim() : '';
    return v === 'name' || v === 'company' ? v : '';
};

function _field(id) {
    const el = document.getElementById(id);
    if (!el) return '';
    const v = el.value != null ? el.value : el.textContent;
    return String(v == null ? '' : v).trim();
}

function _showError(msg) {
    const box = document.getElementById('hm-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('hm-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('hm-notice');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('hm-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail (plain strings for these routers' 403/404/409/500s; the standard
// FastAPI field-error list is flattened like the other pages).
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

function _isReauth(e) {
    const d = e && e.detail;
    return !!d && typeof d === 'object' && d.error === 'reauth_required';
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}
// A 403 on a WRITE means this actor cannot decide holds at all (no user:reactivate /
// user:deactivate grant, or the platform-admin god-mode does not apply to them). The nav only
// mirrors the tenant-admin/platform-admin half of that gate, so a deep link can still get here:
// flip read-only (notice + every remaining action button disabled + aria-disabled), the same
// belt-and-braces idiom as elevation-requests.js / security-alerts.js.
const _WRITE_403 = /required 'user:|missing required capability|user does not have the required|platform admin only|auditor access required|only platform admins|other tenants/i;

function _goReadOnly(msg) {
    _readOnly = true;
    _clearError();
    _showNotice(msg || 'This API refused the hold decisions with 403 — you can read these held accounts, not decide them.');
    Object.keys(_rows).forEach((k) => {
        const btns = _rows[k].btns || {};
        Object.keys(btns).forEach((action) => {
            const btn = btns[action];
            btn.disabled = true;
            btn.setAttribute('aria-disabled', 'true');
        });
    });
}

function _handleReauth(what) {
    _clearError();
    const verb = { release: 'release', reject: 'reject', escalate: 'escalate',
        reading: 'read' }[what] || what;
    _showNotice(`A recent login is required to ${verb} this held account `
        + '(the fresh-auth gate, GY.C4b). The browser is being sent to sign in again — '
        + 'use the "Re-authenticate now" button below if it did not, then repeat the action.');
    const box = document.getElementById('hm-relogin');
    if (box) box.classList.remove('hidden');
    _reloginShown = true;
}

// ---------------------------------------------------------------------------
// init + load. Two reads: the tenant's users (GET /users/) and this tenant's screening-hold
// audit rows (GET /audit-logs/permissions?action=security_incident_created&resource_type=
// security). The evidence read can legitimately 402 (audit-integrity capability entitlement) — that means
// "no evidence column", so the accounts still list and the page says why the trail is empty.
// ---------------------------------------------------------------------------

export async function initializeHeldMatchesPage() {
    _wireFormButtons();
    await load();
}

function _wireFormButtons() {
    // The re-login button ships inside a hidden panel: wiring it here is harmless (a hidden
    // control is reachable by neither click nor Tab), and _handleReauth() unhides it.
    const reBtn = document.getElementById('hm-relogin-btn');
    if (reBtn && !reBtn.dataset.wired) {
        reBtn.dataset.wired = '1';
        reBtn.addEventListener('click', () => { handleLogin(); });
    }
}

async function load() {
    _clearError();
    const body = document.getElementById('hm-table-body');
    const auditBody = document.getElementById('hm-audit-body');
    if (!body || !auditBody) return;
    _users = [];
    _evidence = [];
    _rows = {};
    const tenantRaw = _field('hm-tenant');
    const tenantId = /^\d+$/.test(tenantRaw) ? tenantRaw : null;
    let evidenceErr = null;
    try {
        const [userRes, evRes, user] = await Promise.all([
            heldMatches.listUsers(tenantId),
            heldMatches.evidence(PAGE_LIMIT, 0),
            me.get(),
        ]);
        if (user && user.is_platform_admin === false && tenantId != null) {
            // The /users/ service refuses another tenant for a non-platform admin; say so with
            // its own words rather than silently listing the wrong tenant.
            _showError('Another tenant\'s users are platform-admin only — clear the tenant id to list your own.');
            return;
        }
        _users = Array.isArray(userRes) ? userRes : [];
        _evidence = Array.isArray(evRes) ? evRes : [];
    } catch (e) {
        if (_isReauth(e)) { _handleReauth('reading'); return; }
        // The evidence read is the audit-logs route: its 402 (no AUDIT_HMAC entitlement) and its
        // auditor gate are reported, but the account list is still worth showing, so a failure
        // there is remembered and the users read is retried alone.
        if (/audit|hmac|entitlement|auditor/i.test(_errText(e))) {
            evidenceErr = _errText(e);
            try {
                _users = await heldMatches.listUsers(tenantId);
                if (!Array.isArray(_users)) _users = [];
            } catch (e2) {
                if (_isReauth(e2)) { _handleReauth('reading'); return; }
                _showError(_errText(e2) || 'Failed to load the held accounts');
                _users = [];
            }
        } else {
            _showError(_errText(e) || 'Failed to load the held accounts');
            _users = [];
        }
    }
    if (evidenceErr) {
        _evidenceBlocked = true;
        _showNotice(`The screening-hold audit trail is unavailable for this tenant: ${evidenceErr} `
            + '(the audit-logs route is not available in this edition) — the accounts still list, without evidence.');
    }
    // FIX-DASH-CE-DEAD-NAV-3: resolve the edition BEFORE the rows render, so the Escalate
    // button ships only when the edition resolves positive (see _allowedActions). Awaited,
    // so a slow resolve cannot leave a row without its button; a rejected/errored resolve
    // leaves the fail-closed CE shape (no Escalate).
    await _resolvePaidEdition();
    _renderAll();
}

// The evidence the API returned for one account: the audit row whose metadata.email equals the
// user's email. Both sides are exactly what the API returned, so nothing is invented here.
function _evidenceFor(user) {
    const email = (user.email || '').toLowerCase();
    if (!email) return [];
    return _evidence.filter((row) => {
        const m = (row && row.metadata && row.metadata.email) || '';
        return String(m).toLowerCase() === email;
    });
}

function _heldState(user) {
    const st = user && user.status ? String(user.status) : '';
    return st === 'pending' || st === 'active' || st === 'deactivated' ? st : (st || 'unknown');
}

// What the API actually returned: how many accounts, how many evidence rows, the state split,
// and whether the evidence column is gated off.
function _countText() {
    const bits = [`${_held.length} held account(s)`, `${_evidence.length} screening-hold audit row(s)`];
    if (_evidenceBlocked) bits.push('evidence not available in this edition');
    const counts = {};
    _held.forEach((u) => {
        const st = _heldState(u);
        counts[st] = (counts[st] || 0) + 1;
    });
    const perState = Object.keys(counts).map((k) => `${k} ${counts[k]}`).join(', ');
    if (perState) bits.push(`by state: ${perState}`);
    if (_readOnly) bits.push('read-only (the API refused with 403)');
    return bits.join(' · ');
}

let _held = [];

function _renderAll() {
    const body = document.getElementById('hm-table-body');
    const auditBody = document.getElementById('hm-audit-body');
    const stateWanted = _stateFilter();
    const fieldWanted = _fieldFilter();
    _rows = {};
    _held = _users.filter((u) => {
        if (!u || u.id == null) return false;
        const st = _heldState(u);
        if (st === 'inactive' || st === 'unknown') return false;
        if (stateWanted && st !== stateWanted) return false;
        // A row is a HELD match when the API returned evidence for it, or when the account is
        // itself pending (held) and the evidence column is unavailable — e.g. the audit-logs
        // route is entitlement-gated. An ordinary active account with no evidence is not a
        // held match and never appears here.
        const ev = _evidenceFor(u);
        if (fieldWanted) {
            // The field filter narrows the evidence the audit row carries; an account whose
            // evidence has no such match is only listed when it is itself pending (held).
            const hit = ev.some((row) => _matches(row).some((m) => m.field === fieldWanted));
            if (!hit) return st === 'pending';
        } else if (!ev.length && st !== 'pending') {
            return false;
        }
        return true;
    });
    if (body) {
        body.innerHTML = '';
        _held.forEach((u) => { body.appendChild(_rowNode(u)); });
    }
    if (auditBody) {
        auditBody.innerHTML = '';
        _evidence.forEach((row) => { auditBody.appendChild(_auditNode(row)); });
    }
    const count = document.getElementById('hm-count');
    if (count) count.textContent = _countText();
    // Explicit add/remove (classList.toggle would flip the other classes the shipped page
    // carries on these panels).
    const empty = document.getElementById('hm-empty');
    if (empty) {
        if (_held.length) empty.classList.add('hidden');
        else empty.classList.remove('hidden');
    }
    const auditEmpty = document.getElementById('hm-audit-empty');
    if (auditEmpty) {
        if (_evidence.length) auditEmpty.classList.add('hidden');
        else auditEmpty.classList.remove('hidden');
    }
}

// The matches the audit row carries, as the API returned them (ent_num, score, field; sdn_name
// when the row has it). Never a guess.
function _matches(row) {
    const ms = row && row.metadata && Array.isArray(row.metadata.matches)
        ? row.metadata.matches : [];
    return ms.filter((m) => m && typeof m === 'object');
}
// ---------------------------------------------------------------------------
// One account row. Every server string (email, first/last name, status, the JSONB evidence)
// goes through textContent — never innerHTML — so a hostile SDN name cannot inject markup.
// Action buttons are native <button>s carrying aria-labels that name the row.
// ---------------------------------------------------------------------------

// FIX-DASH-CE-DEAD-NAV-3 — Escalate is CE-carved: the re-hold PATCH and the
// /platform/security-incidents POST it files are denied routes on CE, so the button
// used to ship dead (VISIBLE-DEAD). The edition is resolved ONCE per page load
// (async, before the rows render) into the flag below; _allowedActions stays
// SYNCHRONOUS and reads it, so the render and the click handler can never disagree.
// FAIL-CLOSED: the flag starts false (the CE shape), so a page that never resolved
// the edition offers no Escalate, and api.js isPaidEdition() reports CE on an
// unreadable /platform/features payload (it never throws), so an errored entitlements
// read hides the button too. The one branch that keeps the shipped actions is a
// sandbox that does not ship the helper at all (the node page tests run the module
// body with the imports stripped) — in the browser bundle the import is always
// present, so a CE build can never reach it; the guard is `typeof`, which cannot
// throw on an undeclared identifier.
let _hmPaid = false;

async function _resolvePaidEdition() {
    // Browser: the import at the top is static, so a missing api.js export fails module
    // linking and this body never runs; this branch is reachable only in the node
    // sandbox that strips imports (tests/held-matches-page.test.js), so it stays fail-open
    // there by design. A CE build cannot reach it.
    if (typeof isPaidEdition !== 'function') { _hmPaid = true; return; }
    try { _hmPaid = await isPaidEdition(); } catch (_) { _hmPaid = false; }
}

// The state machine's own shape, not a guess about the server: a pending (held, undecided)
// account can be released or rejected; an active account can be rejected or escalated (re-held);
// a deactivated (rejected) account can be released or escalated.
function _allowedActions(status) {
    const esc = _hmPaid ? ['escalate'] : [];
    if (status === 'pending') return ['release', 'reject'].concat(esc);
    if (status === 'active') return ['reject'].concat(esc);
    if (status === 'deactivated') return ['release'].concat(esc);
    return [];
}

function _rowNode(user) {
    const uid = String(user.id);
    const idp = `hm-${uid}`;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    const ev = _evidenceFor(user);
    const first = ev[0];
    const matches = first ? _matches(first) : [];
    const top = matches[0];

    const cells = [
        [uid, 'px-4 py-3 whitespace-nowrap font-mono text-xs'],
    ];
    cells.forEach((c) => tr.appendChild(_td(c[0], c[1])));

    // Held state: chip + the same word as plain cell text (a contrast-safe copy always exists).
    const st = _heldState(user);
    const stTd = document.createElement('td');
    stTd.className = 'px-4 py-3 whitespace-nowrap';
    const chip = _badge(st, _statusCls(st));
    chip.setAttribute('aria-hidden', 'true');
    stTd.appendChild(chip);
    const stPlain = document.createElement('span');
    stPlain.className = 'text-xs text-gray-600 ml-2';
    stPlain.textContent = st;
    stTd.appendChild(stPlain);
    tr.appendChild(stTd);

    // Evidence columns: exactly what the API returned. Email comes from the user row (the
    // account's own) and, when the audit row carries one, from the audit metadata too — the
    // page shows the account's email only, so no second copy of the same PII is invented.
    tr.appendChild(_td(user.email, 'px-4 py-3 text-xs'));
    const name = [user.first_name, user.last_name].filter((x) => x != null && x !== '').join(' ');
    tr.appendChild(_td(name, 'px-4 py-3 text-xs'));
    tr.appendChild(_td(top && top.ent_num != null ? String(top.ent_num) : null,
        'px-4 py-3 whitespace-nowrap font-mono text-xs'));
    tr.appendChild(_td(top && top.sdn_name != null ? String(top.sdn_name) : null, 'px-4 py-3 text-xs'));
    tr.appendChild(_td(top && top.score != null ? String(top.score) : null,
        'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(top && top.field != null ? String(top.field) : null,
        'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(_fmt(first && first.timestamp), 'px-4 py-3 whitespace-nowrap text-xs'));

    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 text-right whitespace-nowrap';
    const btns = {};
    const allowed = _readOnly ? [] : _allowedActions(st);
    const labels = { release: 'Release', reject: 'Reject', escalate: 'Escalate' };
    allowed.forEach((action) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.id = `${idp}-${action}`;
        btn.dataset.hmAction = action;
        btn.dataset.hmUserId = uid;
        btn.setAttribute('aria-label', `${labels[action]} held match for user ${uid}`);
        btn.textContent = labels[action];
        btn.className = action === 'release'
            ? 'text-green-700 hover:underline text-sm'
            : 'text-red-700 hover:underline text-sm';
        actTd.appendChild(btn);
        btns[action] = btn;
    });
    if (!allowed.length) {
        const done = document.createElement('span');
        done.className = 'text-xs text-gray-500';
        done.textContent = _readOnly ? 'read-only (403)' : `${st} — no decision offered`;
        actTd.appendChild(done);
    }
    tr.appendChild(actTd);

    _rows[uid] = { btns, tr, user };
    Object.keys(btns).forEach((action) => {
        btns[action].addEventListener('click', () => { handleAction(action, user.id); });
    });
    return tr;
}

// One audit-trail row: the API's own fields, nothing added.
function _auditNode(row) {
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';
    const ms = _matches(row);
    const top = ms[0];
    const cells = [
        [_fmt(row && row.timestamp), 'px-4 py-3 whitespace-nowrap text-xs'],
        [row && row.action != null ? String(row.action) : null, 'px-4 py-3 whitespace-nowrap text-xs'],
        [row && row.actor_user_id != null ? `#${row.actor_user_id}` : '—',
            'px-4 py-3 whitespace-nowrap text-xs'],
        [row && row.target_user_id != null ? `#${row.target_user_id}` : '—',
            'px-4 py-3 whitespace-nowrap text-xs'],
        [top && top.ent_num != null ? String(top.ent_num) : null,
            'px-4 py-3 whitespace-nowrap font-mono text-xs'],
        [top && top.score != null ? String(top.score) : null, 'px-4 py-3 whitespace-nowrap text-xs'],
        [top && top.field != null ? String(top.field) : null, 'px-4 py-3 whitespace-nowrap text-xs'],
        [row && row.metadata && row.metadata.email != null
            ? String(row.metadata.email) : null, 'px-4 py-3 text-xs'],
    ];
    cells.forEach((c) => tr.appendChild(_td(c[0], c[1])));
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

function _statusCls(st) {
    return st === 'pending' ? 'bg-amber-100 text-amber-800'
        : st === 'active' ? 'bg-green-100 text-green-700'
        : st === 'deactivated' ? 'bg-red-100 text-red-700'
        : 'bg-gray-200 text-gray-600';
}
// ---------------------------------------------------------------------------
// Decisions. Each is behind confirm() whose copy spells out what the call does and carries the
// reason the operator typed; the server's 403 (no capability) / 404 (unknown or cross-tenant id)
// / 409 (illegal transition) detail is then shown verbatim. Release and Reject send no body —
// those routes have no reason field, so the reason lives in the confirm copy and in the audit
// row the route itself writes (ADMIN_USER_MODIFY / ADMIN_USER_DEACTIVATE). Escalate sends the
// status only (UserUpdate is strict) and puts the reason into the incident summary it files.
// ---------------------------------------------------------------------------

async function handleAction(action, userId) {
    _clearError();
    const uid = String(userId);
    const row = _rows[uid];
    if (!row) { _showError(`Unknown held-match user id: ${userId}`); return; }
    if (_readOnly) {
        _showError('This page is read-only: the API refused the hold decisions with 403.');
        return;
    }
    // FIX-DASH-CE-DEAD-NAV-3 (defence in depth): the render already omits the Escalate
    // control when the edition is not positive, but a click must never reach the denied
    // re-hold/incident routes on CE through any other path, so re-resolve the edition here
    // and refuse. Fail-closed: an unreadable entitlements payload keeps the refusal.
    if (action === 'escalate') {
        await _resolvePaidEdition();
        if (!_hmPaid) {
            _showError('Escalate is not offered on MOD Community Edition — the re-hold and the '
                + 'security incident it files are not available in this edition.');
            return;
        }
    }
    const user = row.user;
    const st = _heldState(user);
    const reason = _field('hm-reason');
    if (reason.length < 3) {
        _showError('A reason is required for Release, Reject and Escalate — the review has to record why.');
        return;
    }
    if (_allowedActions(st).indexOf(action) < 0) {
        _showError(`No ${action === 'release' ? 'Release' : action === 'reject' ? 'Reject' : 'Escalate'} `
            + `is offered for a ${st} account.`);
        return;
    }
    const who = `user ${uid} (${user.email || 'no email'}, ${st})`;
    const labels = {
        release: `Release held match ${who}? PATCH /users/${uid}/reactivate — the account becomes ACTIVE `
            + `and the route audits it (ADMIN_USER_MODIFY). Reason: ${reason}`,
        reject: `Reject held match ${who}? PATCH /users/${uid}/deactivate — soft delete plus API-key `
            + `revocation, audited (ADMIN_USER_DEACTIVATE). Nothing is hard-deleted. Reason: ${reason}`,
        escalate: `Escalate held match ${who}? PATCH /users/${uid} re-holds it (status pending) and POSTs `
            + 'a high-severity security incident whose summary is the reason, so the escalation is '
            + 'trackable. Reason: ' + reason,
    };
    if (!confirm(labels[action])) return;
    const btn = row.btns[action];
    try {
        if (action === 'release') {
            if (btn) await withButtonBusy(btn, async () => heldMatches.release(userId));
            else await heldMatches.release(userId);
            _showNotice(`Held match ${uid} released — the account is ACTIVE and the route audited it. `
                + `Reason recorded in the confirm copy: ${reason}`);
        } else if (action === 'reject') {
            if (btn) await withButtonBusy(btn, async () => heldMatches.reject(userId));
            else await heldMatches.reject(userId);
            _showNotice(`Held match ${uid} rejected — soft-deactivated, API keys revoked, audited. `
                + `Reason: ${reason}`);
        } else {
            if (btn) await withButtonBusy(btn, async () => heldMatches.escalate(userId));
            else await heldMatches.escalate(userId);
            // The re-hold landed; the incident is a SEPARATE call on a stricter gate
            // (require_platform_admin), so its refusal is reported on its own terms and never
            // presented as a failed escalation.
            let incidentMsg = 'no incident filed';
            try {
                const inc = await heldMatches.incident({
                    severity: 'high',
                    summary: reason,
                    detected_at: new Date().toISOString(),
                });
                incidentMsg = inc && inc.id != null
                    ? `security incident ${inc.id} filed (summary: ${reason})`
                    : 'security incident filed';
            } catch (e2) {
                if (_isReauth(e2)) { _handleReauth('escalate'); return; }
                incidentMsg = `incident refused by the server: ${_errText(e2)}`;
            }
            _showNotice(`Held match ${uid} escalated — re-held as pending. ${incidentMsg}. `
                + `Reason: ${reason}`);
        }
        await load();
    } catch (e) {
        if (btn) btn.disabled = false;
        if (_isReauth(e)) { _handleReauth(action); return; }
        _showError(_errText(e) || `Failed to ${action} held match ${uid}`);
        if (_WRITE_403.test(_errText(e))) _goReadOnly(_errText(e));
    }
}

