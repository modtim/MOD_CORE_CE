// Dashboard/js/mobile-devices.js
//
// CUIP.14b — "Enrolled phones" panel (Settings → Integrations → Mobile app,
// the SAME sub-tab as js/mobile-enrollment.js). Lists this tenant's enrolled
// phones (platform, integrity tier, last seen, active shelf count), expands
// each row to the phone's shelf items, and offers a Revoke button per phone.
// Revoke is a SOFT state change (ISO 27001): the row stays visible as revoked.
//
// API (Dashboard API_URL already ends in /v1 — paths here must NOT add it):
//   GET  /mobile-devices?include_revoked=false -> {items|devices: [...]}
//        (also tolerates a bare array); rows carry device_id, platform,
//        app_build, integrity_tier, created_at, last_seen_at, revoked_at,
//        revoked, active_shelf_items.
//   GET  /mobile-devices/{device_id}/shelf     -> {device_id, items: [...]}
//   POST /mobile-devices/{device_id}/revoke    -> 200 {applied, purge_client,
//                                                   purged_items} | 202 when
//                                                   a second approver is due.
//
// The container is BUILT HERE (never in pages/settings.html) so the panel
// stays in the Mobile sub-tab without new markup or a sidebar row. Every
// server string goes through textContent — this file never touches innerHTML.

async function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function _mdWhen(v) {
  if (v == null || v === '') return '—';
  const d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  return d.toLocaleString();
}

function _mdNormalizeList(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload) return [];
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload.devices)) return payload.devices;
  return [];
}

function _mdShelfLine(item) {
  if (!item) return '';
  const type = item.resource_type || 'item';
  const rid = item.resource_id || '';
  const staged = item.staged_at ? `staged ${_mdWhen(item.staged_at)}` : '';
  const exp = item.expires_at ? `expires ${_mdWhen(item.expires_at)}` : '';
  return [`${type} ${rid}`.trim(), staged, exp].filter((s) => s).join(' · ');
}

function _mdRevokeOutcome(status, body) {
  const b = body || {};
  if (status === 202) return { text: 'Revoke requested — awaiting second approver', revoked: false };
  const bits = [];
  if (b.purged_items) bits.push(`purged ${b.purged_items} shelf item(s)`);
  if (b.purge_client) bits.push('client purge requested');
  const base = b.applied === false ? 'Already revoked' : 'Revoked';
  return { text: bits.length ? `${base} — ${bits.join(', ')}` : base, revoked: true };
}

// One row per phone. DOM ids key on encodeURIComponent(device_id) so odd ids
// stay valid selectors; the delegated click handler decodes them back.
function renderRows(containerEl, devices) {
  containerEl.textContent = '';
  (devices || []).forEach((d) => {
    const id = String(d.device_id == null ? '' : d.device_id);
    if (!id) return;
    const key = encodeURIComponent(id);
    const row = document.createElement('div');
    row.id = 'md-row-' + key;
    row.className = 'grid grid-cols-1 gap-1 py-2';
    const cell = (cls, text) => {
      const s = document.createElement('span');
      s.className = cls;
      s.textContent = text;
      row.appendChild(s);
    };
    cell('text-sm font-mono text-gray-800', id);
    cell('text-sm text-gray-800', d.platform || '—');
    cell('text-xs text-gray-500', d.integrity_tier || '—');
    cell('text-xs text-gray-500', _mdWhen(d.last_seen_at));
    cell('text-xs text-gray-500', d.active_shelf_items == null ? 'shelf —' : `shelf ${d.active_shelf_items}`);
    const state = document.createElement('span');
    state.id = 'md-state-' + key;
    state.className = 'text-xs text-gray-500';
    state.textContent = d.revoked ? 'Revoked' : '';
    row.appendChild(state);
    const shelfBtn = document.createElement('button');
    shelfBtn.type = 'button';
    shelfBtn.id = 'md-shelf-' + key;
    shelfBtn.className = 'px-2 py-1 border border-gray-300 text-xs rounded-md text-gray-700';
    shelfBtn.textContent = 'Shelf';
    row.appendChild(shelfBtn);
    const revokeBtn = document.createElement('button');
    revokeBtn.type = 'button';
    revokeBtn.id = 'md-revoke-' + key;
    revokeBtn.className = 'px-2 py-1 border border-gray-300 text-xs rounded-md text-gray-700';
    revokeBtn.textContent = 'Revoke';
    row.appendChild(revokeBtn);
    const shelfList = document.createElement('div');
    shelfList.id = 'md-shelf-list-' + key;
    shelfList.className = 'text-xs text-gray-600';
    shelfList.hidden = true;
    row.appendChild(shelfList);
    containerEl.appendChild(row);
  });
}

async function load(containerEl) {
  const status = document.getElementById('md-status');
  if (status) status.textContent = 'Loading…';
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/mobile-devices?include_revoked=false`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const devices = _mdNormalizeList(await resp.json());
    renderRows(containerEl, devices);
    devices.forEach((d) => _wireRow(String(d.device_id)));
    if (status) status.textContent = devices.length ? '' : 'No phones enrolled yet.';
  } catch (e) {
    const msg = `Could not load enrolled phones: ${(e && e.message) || e}`;
    if (status) status.textContent = msg;
    if (window.showError) await window.showError(msg, 'Enrolled phones');
  }
}

function _wireRow(key) {
  const enc = encodeURIComponent(key);
  const shelfBtn = document.getElementById('md-shelf-' + enc);
  if (shelfBtn && !shelfBtn.dataset.bound) {
    shelfBtn.dataset.bound = '1';
    shelfBtn.addEventListener('click', async () => { await toggleShelf(key); });
  }
  const revokeBtn = document.getElementById('md-revoke-' + enc);
  if (revokeBtn && !revokeBtn.dataset.bound) {
    revokeBtn.dataset.bound = '1';
    revokeBtn.addEventListener('click', () => { _revokeFlow(key); });
  }
}

async function toggleShelf(key) {
  const listEl = document.getElementById('md-shelf-list-' + encodeURIComponent(key));
  if (!listEl) return;
  if (!listEl.hidden) { listEl.hidden = true; return; }  // second click collapses
  const apiUrl = await getApiUrl();
  try {
    const resp = await fetch(`${apiUrl}/mobile-devices/${encodeURIComponent(key)}/shelf`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const items = Array.isArray(data.items) ? data.items : [];
    listEl.textContent = '';
    items.forEach((it) => {
      const line = document.createElement('div');
      line.className = 'text-xs text-gray-600';
      line.textContent = _mdShelfLine(it);
      listEl.appendChild(line);
    });
    if (!items.length) {
      const none = document.createElement('div');
      none.className = 'text-xs text-gray-500';
      none.textContent = 'No active shelf items.';
      listEl.appendChild(none);
    }
    listEl.hidden = false;
  } catch (e) {
    listEl.textContent = '';
    const err = document.createElement('div');
    err.className = 'text-xs text-red-700';
    err.textContent = `Shelf unavailable: ${(e && e.message) || e}`;
    listEl.appendChild(err);
    listEl.hidden = false;
  }
}

async function _revokeFlow(key) {
  const btn = document.getElementById('md-revoke-' + encodeURIComponent(key));
  if (!btn) return;
  if (!window.confirm(`Revoke phone "${key}"? Its enrollment ends and its shelf items are purged. The row stays visible as revoked.`)) return;
  const apiUrl = await getApiUrl();
  let status = 0;
  let body = null;
  try {
    const resp = await fetch(`${apiUrl}/mobile-devices/${encodeURIComponent(key)}/revoke`, {
      method: 'POST',
      headers: authHeader(),
    });
    status = resp.status;
    if (!resp.ok && status !== 202) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${status}`);
    }
    body = await resp.json().catch(() => ({}));
  } catch (e) {
    const msg = `Could not revoke phone "${key}": ${(e && e.message) || e}`;
    const state = document.getElementById('md-state-' + encodeURIComponent(key));
    if (state) state.textContent = msg;
    if (window.showError) await window.showError(msg, 'Enrolled phones');
    return;
  }
  const outcome = _mdRevokeOutcome(status, body);
  const state = document.getElementById('md-state-' + encodeURIComponent(key));
  if (state) state.textContent = outcome.text;
  if (btn) btn.textContent = outcome.revoked ? 'Revoke' : 'Revoke (pending)';
}

// Mount: build the container inside the Mobile sub-tab of Settings →
// Integrations (same section as the enrollment panel), then hydrate it.
// No DOM lookups at module level — ui.js re-injects page HTML on every
// navigation, so everything here runs per-open and is idempotent.
export async function initializeMobileDevicesPanel() {
  const host = document.getElementById('int-subpanel-mobile');
  if (!host) return;  // panel not mounted — no-op
  let container = document.getElementById('md-rows');
  if (!container) {
    const card = document.createElement('div');
    card.id = 'md-card';
    card.className = 'mt-6 bg-white border border-gray-200 rounded-lg p-6';
    const h = document.createElement('h4');
    h.className = 'text-base font-semibold text-gray-900';
    h.textContent = 'Enrolled phones';
    card.appendChild(h);
    const status = document.createElement('p');
    status.id = 'md-status';
    status.className = 'text-sm text-gray-500 mt-1';
    card.appendChild(status);
    container = document.createElement('div');
    container.id = 'md-rows';
    container.className = 'grid grid-cols-1 gap-3 mt-2';
    card.appendChild(container);
    host.appendChild(card);
  }
  await load(container);
}
