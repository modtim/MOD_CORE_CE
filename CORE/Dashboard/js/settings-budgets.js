// Dashboard/js/settings-budgets.js
//
// sz-1.x — "Workflow budgets" panel (cost caps) in Settings.
// Table: the tenant default row (workflow_id NULL) + one row per workflow
// override (workflow name, per-run cap, per-period cap, window, Edit).
// Cap semantics (bug 233): null/blank = NO cap ("No cap"), 0 = block all
// spend ("0 (blocks spend)"). There is NO DELETE anywhere: "remove cap" is
// a PUT with nulls (ISO 27001 — the row stays, the caps clear, audited).
//
// API (Dashboard API_URL already ends in /v1 — paths here must NOT add it;
// same base/prefix rule settings-oauth-connections.js uses for /integrations/oauth/...):
//   GET  /budgets                          -> {tenant_id, default|null, overrides[]}
//        row: {workflow_id, workflow_name, max_cost_per_run_credits,
//              max_cost_per_period_credits, period_window, updated_at}
//   PUT  /budgets/default                  -> tenant admin only; non-admin 403
//        -> the default row stays read-only + "Only tenant admins can change the default"
//   PUT  /budgets/workflows/{workflow_id}  -> 404 unknown/hidden workflow,
//        403 no edit capability; body {max_cost_per_run_credits|null,
//        max_cost_per_period_credits|null, period_window: daily|weekly|monthly|quarterly}
//   GET  /road?limit=100              -> "Add override" picker list — the
//        SAME endpoint + shape the Dashboard's seller.js loadWorkflowsForPicker() uses
//
// Caps are validated CLIENT-SIDE before any fetch (empty -> null, whole numbers
// 0..10,000,000, window in the set) so an out-of-range value never reaches the
// network. Server 4xx bodies show their `detail` in the status line via textContent
// only — this file never touches innerHTML. The container ships EMPTY in pages/
// settings.html and settings.js imports initBudgetsPanel and calls it: ui.js
// mounts pages via innerHTML, which never executes an injected <script> tag
// (see the comment block at pages/settings.html 1657-1665 for the OAuth panel).

async function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const CAP_MAX = 10000000;
const BUDGET_WINDOWS = ['daily', 'weekly', 'monthly', 'quarterly'];

function _budgetsEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function _budgetsCapText(v) {
  if (v === null || v === undefined) return 'No cap';
  const n = Number(v);
  if (n === 0) return '0 (blocks spend)';
  return n.toLocaleString();
}

function _budgetsFail(statusEl, msg) {
  if (statusEl) statusEl.textContent = msg;
}

function _budgetsDetailText(payload) {
  const d = payload && payload.detail !== undefined ? payload.detail : null;
  if (d === null || d === undefined) return '';
  if (typeof d === 'string') return d;
  if (typeof d === 'object') return String(d.message || d.code || '');
  return String(d);
}

async function _budgetsLoadList() {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/budgets`, { headers: authHeader() });
    if (!resp.ok) return null;
    return await resp.json();
  } catch { return null; }
}

async function _budgetsLoadWorkflows() {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/road?limit=100`, { headers: authHeader() });
    if (!resp.ok) return [];
    const rows = await resp.json();
    return Array.isArray(rows) ? rows : [];
  } catch { return []; }
}

function _budgetsValidateCaps(runRaw, periodRaw, windowRaw) {
  const parseCap = (raw) => {
    const s = (raw === null || raw === undefined) ? '' : String(raw).trim();
    if (s === '') return { ok: true, value: null };
    if (!/^\d+$/.test(s)) return { ok: false, error: 'Cap must be a whole number of credits (blank = no cap).' };
    const n = parseInt(s, 10);
    if (n > CAP_MAX) return { ok: false, error: `Cap must be between 0 and ${CAP_MAX.toLocaleString()}.` };
    return { ok: true, value: n };
  };
  const run = parseCap(runRaw);
  if (!run.ok) return run;
  const period = parseCap(periodRaw);
  if (!period.ok) return period;
  const w = (windowRaw === null || windowRaw === undefined) ? '' : String(windowRaw).trim().toLowerCase();
  const window = w === '' ? 'monthly' : w;
  if (BUDGET_WINDOWS.indexOf(window) === -1) return { ok: false, error: 'Period window must be daily, weekly, monthly or quarterly.' };
  return { ok: true, body: { max_cost_per_run_credits: run.value, max_cost_per_period_credits: period.value, period_window: window } };
}

async function _budgetsPut(path, body, statusEl) {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}${path}`, {
      method: 'PUT',
      headers: { ...authHeader(), 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    let payload = null;
    try { payload = await resp.json(); } catch { payload = null; }
    if (!resp.ok) {
      _budgetsFail(statusEl, _budgetsDetailText(payload) || 'Save failed — please try again.');
      return { ok: false, status: resp.status, payload: payload };
    }
    _budgetsFail(statusEl, '');
    return { ok: true, status: resp.status, payload: payload };
  } catch {
    _budgetsFail(statusEl, 'Save failed — please try again.');
    return { ok: false, status: 0, payload: null };
  }
}

function _budgetsFindRows(host) {
  const out = [];
  const stack = Array.from(host.childNodes);
  while (stack.length) {
    const n = stack.pop();
    if (n.dataset && n.dataset.rowKey != null) out.push(n);
    Array.from(n.childNodes).forEach((k) => stack.push(k));
  }
  return out;
}

function _budgetsRowKey(row) {
  return row.workflow_id === null || row.workflow_id === undefined ? 'default' : `wf:${row.workflow_id}`;
}

function _budgetsRenderTable(host, list, isAdmin, statusEl) {
  const rows = [];
  if (list && list.default) rows.push({ row: list.default, wf: false });
  if (list && Array.isArray(list.overrides)) list.overrides.forEach((r) => rows.push({ row: r, wf: true }));
  const table = _budgetsEl('table', 'budgets-table w-full border border-gray-200 rounded-md text-sm');
  const head = _budgetsEl('tr');
  ['Scope', 'Per-run cap', 'Per-period cap', 'Window', 'Updated', 'Actions'].forEach((h) => {
    const th = _budgetsEl('th', 'px-3 py-2 text-left text-xs font-medium uppercase tracking-wider');
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  rows.forEach((entry) => {
    const r = entry.row;
    const isDefault = entry.wf !== true;
    const editable = isDefault ? isAdmin : true;
    const tr = _budgetsEl('tr');
    tr.dataset.rowKey = _budgetsRowKey(r);
    const scope = isDefault ? 'Tenant default' : (typeof r.workflow_name === 'string' ? r.workflow_name : String(r.workflow_name ?? ''));
    const cells = [
      scope,
      _budgetsCapText(r.max_cost_per_run_credits),
      _budgetsCapText(r.max_cost_per_period_credits),
      r.period_window || '—',
      r.updated_at ? String(r.updated_at) : '—',
    ];
    cells.forEach((c) => {
      const td = _budgetsEl('td', 'px-3 py-2');
      td.textContent = c;
      tr.appendChild(td);
    });
    const td = _budgetsEl('td', 'px-3 py-2');
    if (editable) {
      const btn = _budgetsEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
      btn.textContent = 'Edit';
      btn.addEventListener('click', async () => {
        const fresh = await _budgetsOpenForm(isDefault ? null : r.workflow_id, statusEl);
        if (fresh) {
          Array.from(host.childNodes).forEach((k) => host.removeChild(k));
          _budgetsRenderTable(host, fresh, isAdmin, statusEl);
        }
      });
      td.appendChild(btn);
    } else {
      const note = _budgetsEl('span', 'text-gray-500 text-xs');
      note.textContent = 'Only tenant admins can change the default';
      td.appendChild(note);
    }
    tr.appendChild(td);
    table.appendChild(tr);
  });
  host.appendChild(table);
}

async function _budgetsOpenForm(workflowId, statusEl) {
  const runRaw = window.prompt ? window.prompt('Per-run cap in credits (blank = no cap, 0 = block all spend)') : '';
  const periodRaw = window.prompt ? window.prompt('Per-period cap in credits (blank = no cap)') : '';
  const windowRaw = window.prompt ? window.prompt('Period window: daily, weekly, monthly or quarterly') : '';
  const checked = _budgetsValidateCaps(runRaw, periodRaw, windowRaw);
  if (!checked.ok) { _budgetsFail(statusEl, checked.error); return false; }
  const path = workflowId === null ? '/budgets/default' : `/budgets/workflows/${encodeURIComponent(String(workflowId))}`;
  const put = await _budgetsPut(path, checked.body, statusEl);
  if (!put.ok) return false;
  const list = await _budgetsLoadList();
  return list;
}

async function _budgetsAddOverride(statusEl) {
  const workflows = await _budgetsLoadWorkflows();
  if (!workflows.length) { _budgetsFail(statusEl, 'No workflows available to add an override for.'); return null; }
  const pick = window.prompt ? window.prompt(`Add an override for which workflow? (${workflows.map((w) => w.id).join(', ')})`) : '';
  const wfId = parseInt(String(pick || ''), 10);
  if (!Number.isFinite(wfId) || wfId <= 0) { _budgetsFail(statusEl, 'Pick a workflow from the list above.'); return null; }
  const runRaw = window.prompt ? window.prompt('Per-run cap in credits (blank = no cap)') : '';
  const periodRaw = window.prompt ? window.prompt('Per-period cap in credits (blank = no cap)') : '';
  const windowRaw = window.prompt ? window.prompt('Period window: daily, weekly, monthly or quarterly') : '';
  const checked = _budgetsValidateCaps(runRaw, periodRaw, windowRaw);
  if (!checked.ok) { _budgetsFail(statusEl, checked.error); return null; }
  const put = await _budgetsPut(`/budgets/workflows/${encodeURIComponent(String(wfId))}`, checked.body, statusEl);
  if (!put.ok) return null;
  return await _budgetsLoadList();
}

export async function initBudgetsPanel(container) {
  if (!container) return;
  // Idempotent: settings.js may re-call this within one mount (like the OAuth
  // panel's loadIntegrations() re-entry) — rebuild the DOM from scratch.
  Array.from(container.childNodes).forEach((k) => container.removeChild(k));
  container.hidden = false;
  // GET /budgets is gated server-side by require_rollout("cost_economics") —
  // a rollout-off tenant (or any tenant otherwise denied) gets 403/404. That
  // is NOT a load failure: hide the container quietly (no error banner, no
  // console spam) instead of rendering a broken/empty-looking panel.
  let gateResp;
  try {
    gateResp = await fetch(`${await getApiUrl()}/budgets`, { headers: authHeader() });
  } catch {
    gateResp = null;
  }
  if (gateResp && (gateResp.status === 403 || gateResp.status === 404)) {
    container.hidden = true;
    return;
  }
  const statusEl = _budgetsEl('div', 'budgets-status text-sm text-gray-500');
  container.appendChild(statusEl);
  const heading = _budgetsEl('h3', 'text-lg font-medium text-gray-800 mb-2');
  heading.textContent = 'Workflow budgets';
  container.appendChild(heading);
  const help = _budgetsEl('p', 'text-sm text-gray-500 mb-2');
  help.textContent = 'Blank = no cap; 0 = block all spend. Removing a cap clears it (no delete) — the change is audited.';
  container.appendChild(help);
  const list = gateResp && gateResp.ok ? await gateResp.json().catch(() => null) : null;
  if (list === null) { _budgetsFail(statusEl, 'Could not load budgets — please try again.'); return; }
  const isAdmin = await (async () => {
    try {
      const resp = await fetch(`${await getApiUrl()}/me`, { headers: authHeader() });
      if (!resp.ok) return false;
      const me = await resp.json();
      return !!me && (me.is_platform_admin === true || me.parent_user_id === null);
    } catch { return false; }
  })();
  const host = _budgetsEl('div', 'budgets-table-host');
  container.appendChild(host);
  _budgetsRenderTable(host, list, isAdmin, statusEl);
  if (!list.default || !(Array.isArray(list.overrides) && list.overrides.length)) {
    const empty = _budgetsEl('p', 'text-sm text-gray-500 mt-2');
    empty.textContent = 'No caps set yet — every workflow can spend without a limit.';
    container.appendChild(empty);
  }
  const addBtn = _budgetsEl('button', 'px-3 py-1 text-xs rounded border-gray-300 mt-2');
  addBtn.textContent = 'Add override';
  addBtn.addEventListener('click', async () => {
    const fresh = await _budgetsAddOverride(statusEl);
    if (fresh) {
      Array.from(host.childNodes).forEach((k) => host.removeChild(k));
      _budgetsRenderTable(host, fresh, isAdmin, statusEl);
    }
  });
  container.appendChild(addBtn);
}

// No self-mount IIFE: ui.js mounts pages via innerHTML, which never executes an
// injected <script type="module"> tag (pages/settings.html 1657-1665 comment
// block). settings.js imports initBudgetsPanel and calls it with the empty
// <div id="budgets-panel"> container, exactly like the OAuth panel.
