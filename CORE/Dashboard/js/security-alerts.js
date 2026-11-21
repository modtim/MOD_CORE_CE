// GSEC FX.112 p3d — this tenant's security alerts: list + acknowledge
// (MOD Core Dashboard).
//
// Backend: CORE/API/routers/tenant/security_alerts.py (mounted at /v1/tenant),
// logic services/security_alert_delivery.ack_alert:
//   GET  /tenant/security-alerts?acked=&limit=&offset=  gates TENANT:view_settings
//        -> {items:[{id,rule_key,severity,tenant_id,subject,details,
//        first_seen_at,last_seen_at,count,acked_at,acked_by}],total,limit,
//        offset}. THIS tenant only (another tenant's rows are invisible),
//        soft-deleted rows excluded (ISO 27001 — the row stays in the DB, it
//        leaves the list), ordered NEWEST FIRST (first_seen_at desc, then id
//        desc — the server's own order, which this page keeps as-is). The
//        ONLY server filters are `acked` (true=acked only / false=open only /
//        omitted=all) and the limit/offset page window (limit 1..200, offset
//        >=0). There is NO severity param, so the severity radio narrows the
//        loaded window in the browser and the page says so.
//   POST /tenant/security-alerts/{id}/ack  gates TENANT:manage_settings
//        and takes NO body — the endpoint has no note field, so this page
//        never invents one. Answers {alert_id,already_acked,acked_at,acked_by}
//        and stamps acked_at/acked_by + one SECURITY_ALERT_ACKED audit row.
//        Idempotent: re-acking returns the existing stamps with no second
//        audit row; another tenant's id 404s (never an existence leak).
//
// One row per alert: first/last seen, rule_key, severity chip, subject,
// details (evidence), trip count, status chip (Open / Acked), acked-by/acked-
// at, and an Acknowledge button on OPEN rows only. Every server string —
// subject, rule_key, severity, and the JSONB `details` evidence — is written
// with textContent (never innerHTML), so hostile evidence cannot inject
// markup. A 403 on an ack flips the whole page into the read-only view
// (notice + every button disabled + aria-disabled), the same belt-and-braces
// idiom as alert-rules.js, since the nav only mirrors the tenant-admin gate.
// After a successful ack the row is updated IN PLACE (status chip -> Acked,
// acked-by/acked-at filled, Acknowledge button removed) without a re-GET.
import { securityAlerts } from './api.js';

let _alerts = [];
let _readOnly = false;
let _rows = {};   // alert id -> {btn} for the live DOM row
let _status = 'all';      // 'all' | 'open' | 'acked' -> the API's `acked` param
let _severity = 'all';    // client-side narrowing only (no server param)

const PAGE_LIMIT = 50;      // inside the API's 1..200 window
const MAX_PAGES = 8;        // 8*50 = 400 rows is plenty for a tenant dashboard

function _showError(msg) {
    const box = document.getElementById('sa-error');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearError() {
    const box = document.getElementById('sa-error');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

function _showNotice(msg) {
    const box = document.getElementById('sa-notice');
    if (box) { box.textContent = msg; box.classList.remove('hidden'); }
}

function _clearNotice() {
    const box = document.getElementById('sa-notice');
    if (box) { box.textContent = ''; box.classList.add('hidden'); }
}

// Verbatim server detail (plain strings for this router's 404/500s; the
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

function _idKey(id) {
    return String(id == null ? '' : id).replace(/[^a-z0-9_-]/gi, '_');
}

function _fmt(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toISOString().slice(0, 19).replace('T', ' ');
}

function _td(text, cls) {
    const td = document.createElement('td');
    td.className = cls;
    td.textContent = text == null ? '—' : String(text);
    return td;
}

// Severity chip. The dark-theme contrast-safe classes from style.css (FX.68
// p2) are what make these readable on slate-800: bg-red-100 -> red-900 with
// text-red-700 -> red-400, bg-green-100 -> green-900 with text-green-700 ->
// green-400. critical/high get the red chip, medium the amber one (amber is
// not in the FX.68 override set, so it keeps its light-theme pairing and is
// never used for text below the 4.5:1 floor — the chip text is the same
// string as the plain cell text next to it, so a reader always has a
// contrast-safe copy), low/unknown the gray chip.
function _sevCls(sev) {
    const s = String(sev == null ? '' : sev).toLowerCase();
    if (s === 'critical' || s === 'high') return 'bg-red-100 text-red-700';
    if (s === 'medium') return 'bg-amber-100 text-amber-700';
    return 'bg-gray-100 text-gray-600';
}

function _badge(text, cls) {
    const span = document.createElement('span');
    span.className = `px-2 py-0.5 rounded-full text-xs ${cls}`;
    span.textContent = text;
    return span;
}

// ---------------------------------------------------------------------------
// Filter values. The status buttons (sa-filter-all/open/acked, baked in the
// page) map to the API's own `acked` param; the severity buttons
// (sa-filter-sev-*) are browser-side narrowing only — the router has no
// severity query param, so they never change the query string.
// ---------------------------------------------------------------------------

const _STATUS_CHOICES = [
    { value: 'all', label: 'All statuses' },
    { value: 'open', label: 'Open only' },
    { value: 'acked', label: 'Acknowledged only' },
];
const _SEV_CHOICES = ['all', 'low', 'medium', 'high', 'critical'];

// ---------------------------------------------------------------------------
// init + load (paged: the API answers a limit/offset window, so the page
// walks windows until it has the server's own `total`, then applies the
// browser-side severity narrowing over what it collected)
// ---------------------------------------------------------------------------

export async function initializeSecurityAlertsPage() {
    _wireFilterButtons();
    await loadAlerts();
}

function _wireFilterButtons() {
    _STATUS_CHOICES.forEach((c) => {
        const btn = document.getElementById(`sa-filter-${c.value}`);
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = '1';
        btn.addEventListener('click', () => {
            _status = c.value;
            _syncActiveFilter('status', c.value);
            loadAlerts();
        });
    });
    _SEV_CHOICES.forEach((v) => {
        const btn = document.getElementById(`sa-filter-sev-${v}`);
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = '1';
        btn.addEventListener('click', () => {
            _severity = v;
            _syncActiveFilter('severity', v);
            // severity is browser-side only: re-render the collected rows,
            // never re-GET (the API has no severity param to ask for).
            _rerender();
        });
    });
}

// Only the chosen button in each group carries the active chip (explicit add/
// remove — classList.toggle() would flip every button in the group).
function _syncActiveFilter(group, active) {
    const ids = group === 'status'
        ? _STATUS_CHOICES.map((c) => `sa-filter-${c.value}`)
        : _SEV_CHOICES.map((v) => `sa-filter-sev-${v}`);
    ids.forEach((id) => {
        const btn = document.getElementById(id);
        if (!btn) return;
        if (id.endsWith(`-${active}`)) btn.classList.add('bg-slate-200');
        else btn.classList.remove('bg-slate-200');
    });
}

// The API's `acked` param for the current status filter ('all' -> omitted).
function _ackedParam() {
    if (_status === 'open') return false;
    if (_status === 'acked') return true;
    return null;
}

async function loadAlerts() {
    _clearError();
    const tbody = document.getElementById('sa-table-body');
    if (!tbody) return;
    const empty = document.getElementById('sa-empty');
    if (empty) empty.classList.add('hidden');
    tbody.innerHTML = '';
    _rows = {};
    const acked = _ackedParam();
    const collected = [];
    let total = null;
    let offset = 0;
    try {
        for (let page = 0; page < MAX_PAGES; page += 1) {
            const res = await securityAlerts.list({
                acked: acked, limit: PAGE_LIMIT, offset: offset });
            const items = (res && res.items) || [];
            for (const it of items) collected.push(it);
            total = (res && typeof res.total === 'number') ? res.total : null;
            if (!items.length) break;
            offset += items.length;
            // The server's own `total` is authoritative; only when it is
            // missing does a short page end the walk.
            if (total != null) {
                if (offset >= total) break;
            } else if (items.length < PAGE_LIMIT) {
                break;
            }
        }
    } catch (e) {
        _showError(_errText(e) || 'Failed to load the security alerts');
        _alerts = [];
        if (empty) empty.classList.remove('hidden');
        return;
    }
    _alerts = collected;
    _renderAll(tbody);
}

// The count line states what the API actually did: the server's own `total`
// for the current `acked` filter, and — when severity narrowed things
// browser-side — how many rows that left on screen.
function _countText() {
    const shown = _visibleAlerts().length;
    const acked = _ackedParam();
    const scope = acked === null ? 'status filter: all'
        : (acked === false ? 'status filter: open only' : 'status filter: acknowledged only');
    let bits = [`${shown} alert(s) for this tenant (${scope})`];
    if (_severity !== 'all') {
        bits.push(`severity "${_severity}" narrowed in the browser — the API has no severity param`);
    }
    if (_readOnly) bits.push('read-only (the API refused the ack with 403)');
    return bits.join(' · ');
}

function _visibleAlerts() {
    if (_severity === 'all') return _alerts;
    return _alerts.filter((a) => String(a.severity == null ? '' : a.severity)
        .toLowerCase() === _severity);
}

function _renderAll(tbody) {
    _rows = {};
    tbody.innerHTML = '';
    _visibleAlerts().forEach((a) => {
        tbody.appendChild(_rowNode(a));
    });
    const count = document.getElementById('sa-count');
    if (count) count.textContent = _countText();
    const empty = document.getElementById('sa-empty');
    if (empty && !_visibleAlerts().length) empty.classList.remove('hidden');
}

function _rerender() {
    const tbody = document.getElementById('sa-table-body');
    if (!tbody) return;
    _renderAll(tbody);
}

// ---------------------------------------------------------------------------
// One row. Every server string (rule_key, severity, subject, the JSONB
// details evidence) goes through textContent — never innerHTML — so a hostile
// evidence payload cannot inject markup. Acknowledge exists only on OPEN
// rows; after a successful ack the row is updated in place.
// ---------------------------------------------------------------------------

function _rowNode(alert) {
    const idp = `sa-${_idKey(alert.id)}`;
    const tr = document.createElement('tr');
    tr.className = 'hover:bg-gray-50';

    tr.appendChild(_td(_fmt(alert.first_seen_at), 'px-4 py-3 whitespace-nowrap font-semibold'));
    tr.appendChild(_td(_fmt(alert.last_seen_at), 'px-4 py-3 whitespace-nowrap text-xs'));
    tr.appendChild(_td(alert.rule_key, 'px-4 py-3 whitespace-nowrap text-xs'));

    // Severity: chip + the same word as plain cell text, so the contrast-safe
    // copy always exists even if a chip colour fails in the dark theme.
    const sevTd = document.createElement('td');
    sevTd.className = 'px-4 py-3 whitespace-nowrap';
    const sevChip = _badge(alert.severity || '—', _sevCls(alert.severity));
    sevChip.setAttribute('aria-hidden', 'true');
    sevTd.appendChild(sevChip);
    const sevPlain = document.createElement('span');
    sevPlain.className = 'text-xs text-gray-600 ml-2';
    sevPlain.textContent = alert.severity || '—';
    sevTd.appendChild(sevPlain);
    tr.appendChild(sevTd);

    tr.appendChild(_td(alert.subject, 'px-4 py-3 whitespace-nowrap text-xs'));

    // Evidence: the JSONB details payload printed as escaped text only.
    tr.appendChild(_td(_evidenceText(alert.details), 'px-4 py-3 text-xs'));
    tr.appendChild(_td(alert.count, 'px-4 py-3 whitespace-nowrap text-xs'));

    const acked = alert.acked_at != null;
    const stTd = document.createElement('td');
    stTd.className = 'px-4 py-3 whitespace-nowrap';
    const stChip = _badge(acked ? 'Acked' : 'Open',
        acked ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700');
    stChip.setAttribute('aria-hidden', 'true');
    stTd.appendChild(stChip);
    const stPlain = document.createElement('span');
    stPlain.className = 'text-xs text-gray-600 ml-2';
    stPlain.textContent = acked ? 'Acked' : 'Open';
    stTd.appendChild(stPlain);
    tr.appendChild(stTd);

    const byTd = document.createElement('td');
    byTd.className = 'px-4 py-3 whitespace-nowrap text-xs';
    if (acked) {
        byTd.textContent = `${alert.acked_by ?? '—'} · ${_fmt(alert.acked_at)}`;
    } else {
        byTd.textContent = '—';
    }
    tr.appendChild(byTd);

    const actTd = document.createElement('td');
    actTd.className = 'px-4 py-3 text-right whitespace-nowrap';
    let btn = null;
    if (!acked && !_readOnly) {
        btn = document.createElement('button');
        btn.type = 'button';
        btn.id = `${idp}-ack`;
        btn.dataset.saAction = 'ack';
        btn.dataset.saId = String(alert.id);
        btn.textContent = `Acknowledge alert ${alert.id}`;
        btn.className = 'text-red-600 hover:underline text-sm';
        actTd.appendChild(btn);
    } else if (acked) {
        const done = document.createElement('span');
        done.className = 'text-xs text-gray-500';
        done.textContent = 'Already acknowledged';
        actTd.appendChild(done);
    }
    tr.appendChild(actTd);

    _rows[alert.id] = { btn, tr, alert };
    if (btn) {
        btn.addEventListener('click', () => { handleAck(btn); });
    }
    return tr;
}

// The JSONB evidence, flattened to one escaped line. Keys and values are
// printed as text; nothing here is ever parsed as markup.
function _evidenceText(details) {
    if (details == null) return '—';
    if (typeof details === 'string') return details;
    if (typeof details !== 'object') return String(details);
    const parts = [];
    const walk = (obj, depth) => {
        if (depth > 3) return;
        Object.keys(obj).forEach((k) => {
            const v = obj[k];
            if (v != null && typeof v === 'object') {
                walk(v, depth + 1);
            } else {
                parts.push(`${k}=${v == null ? '—' : String(v)}`);
            }
        });
    };
    walk(details, 0);
    return parts.length ? parts.join(' · ') : '—';
}

// ---------------------------------------------------------------------------
// Acknowledge one alert. The endpoint takes NO body (no note field exists
// server-side), so nothing is invented here. On success the row is updated
// IN PLACE from the server's own stamps — no re-GET, no re-render — and the
// Acknowledge button is removed (the alert is no longer open). A 403 flips
// the page into the read-only view.
// ---------------------------------------------------------------------------

export async function handleAck(btn) {
    const rawId = btn.dataset.saId;
    const alertId = Number(rawId);
    const alert = _alerts.find((a) => a.id === alertId);
    if (!alert) { _showError(`Unknown alert id: ${rawId}`); return; }
    if (_readOnly) {
        _showError('This page is read-only: the API refused the ack (403).');
        return;
    }
    if (alert.acked_at != null) {
        // Re-acking is idempotent server-side, but the row already says
        // Acked — say so instead of POSTing a no-op.
        _showNotice(`Alert ${alertId} is already acknowledged `
            + `(${alert.acked_by ?? '—'} · ${_fmt(alert.acked_at)}).`);
        return;
    }
    _clearError();
    try {
        const res = await securityAlerts.ack(alertId);
        if (res && res.acked_at != null) {
            alert.acked_at = res.acked_at;
            alert.acked_by = res.acked_by;
        } else if (res && res.already_acked) {
            // The server says it was already acked (someone else got there
            // first); the row still needs the stamps, so re-read the window.
            await loadAlerts();
            return;
        }
        _updateRowInPlace(alert);
    } catch (e) {
        if (e && e.status === 403) {
            _goReadOnly('You can view this tenant\'s security alerts, but you '
                + 'cannot acknowledge them (the API refused the ack with 403).');
            return;
        }
        _showError(_errText(e) || `Failed to acknowledge alert ${alertId}`);
    }
}

// Update the live DOM row for one alert without touching the others: status
// chip Open -> Acked, acked-by/acked-at filled, Acknowledge button replaced
// by a plain "Already acknowledged" note.
function _updateRowInPlace(alert) {
    const row = _rows[alert.id];
    if (!row || !row.tr) { _rerender(); return; }
    const tds = [];
    const kids = row.tr.children;   // live HTMLCollection in the browser
    for (let i = 0; i < kids.length; i += 1) {
        const el = kids[i];
        const name = (el.tag || el.tagName || '').toLowerCase();
        if (name === 'td') tds.push(el);
    }
    // Column order: first_seen, last_seen, rule, severity, subject, evidence,
    // trips, status, acked-by/at, action.
    const stTd = tds[7];
    const byTd = tds[8];
    const actTd = tds[9];
    const acked = alert.acked_at != null;
    if (stTd) {
        stTd.innerHTML = '';
        const chip = _badge(acked ? 'Acked' : 'Open',
            acked ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700');
        chip.setAttribute('aria-hidden', 'true');
        stTd.appendChild(chip);
        const stPlain = document.createElement('span');
        stPlain.className = 'text-xs text-gray-600 ml-2';
        stPlain.textContent = acked ? 'Acked' : 'Open';
        stTd.appendChild(stPlain);
    }
    if (byTd) {
        byTd.innerHTML = '';
        byTd.textContent = acked
            ? `${alert.acked_by ?? '—'} · ${_fmt(alert.acked_at)}` : '—';
    }
    if (actTd) {
        actTd.innerHTML = '';
        const done = document.createElement('span');
        done.className = 'text-xs text-gray-500';
        done.textContent = 'Already acknowledged';
        actTd.appendChild(done);
    }
    const count = document.getElementById('sa-count');
    if (count) count.textContent = _countText();
}

function _goReadOnly(msg) {
    _readOnly = true;
    _clearError();
    _showNotice(msg);
    // Every remaining Acknowledge button is disabled + aria-disabled.
    _alerts.forEach((a) => {
        const row = _rows[a.id];
        if (!row || !row.btn) return;
        row.btn.disabled = true;
        row.btn.setAttribute('aria-disabled', 'true');
    });
    const count = document.getElementById('sa-count');
    if (count) count.textContent = _countText();
}
