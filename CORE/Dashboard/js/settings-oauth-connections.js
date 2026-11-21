// Dashboard/js/settings-oauth-connections.js
//
// sz-4.9b — "OAuth connections" panel inside the Settings → Integrations tab.
// Lists the OAuth providers the broker knows (name + Configured / Not
// configured badge), lets a tenant admin Connect (POST /{provider}/start with
// redirect_uri = this settings page URL + '#integrations', then navigate to the
// returned authorization_url), shows this tenant's live connections as a table
// (provider, account, scopes, status, expires, created) and offers Disconnect
// (confirm() -> DELETE -> refresh). Rows whose status is NOT active — the API's
// dead statuses refresh_failed / reconnect_required (CORE/API/services/oauth_refresh.py
// :94-96) and revoked (CORE/API/routers/oauth_connections.py:502 soft-delete; models.py
// ck_oauth_connections_status allows exactly active|refresh_failed|reconnect_required
// |revoked) — get a warning badge + Reconnect. 'reauth_required' is NOT a connection
// status (it is the Keycloak session error in dependencies.py/schemas.py). A return
// navigation carrying
// ?oauth=connected&provider=x shows a success toast and strips the query.
//
// API (Dashboard API_URL already ends in /v1 — paths here must NOT add it):
//   GET  /integrations/oauth/providers    -> [{provider, configured}]
//   POST /integrations/oauth/{provider}/start  {redirect_uri} -> {authorization_url}
//   GET  /integrations/oauth/connections  -> rows (id, provider, external_account_id,
//        scopes, status, access_token_expires_at, created_at, updated_at)
//   DELETE /integrations/oauth/connections/{id}
//
// Every server string goes through textContent — this file never touches
// innerHTML. Errors show a short generic message; response bodies are never
// echoed. The container div lives in pages/settings.html; init is a no-op when
// the container is absent.

async function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function _oauthWhen(v) {
  if (v == null || v === '') return '—';
  const d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  return d.toLocaleString();
}

function _oauthNormalizeList(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload) return [];
  if (Array.isArray(payload.providers)) return payload.providers;
  if (Array.isArray(payload.connections)) return payload.connections;
  if (Array.isArray(payload.items)) return payload.items;
  return [];
}

function _oauthHref() {
  if (typeof window.location === 'string') return window.location;
  return String(window.location?.href || '');
}

function _oauthSearch() {
  const q = _oauthHref().split('#')[0].split('?')[1];
  return q || '';
}

function _oauthRedirectUri() {
  return `${_oauthHref().split('#')[0].split('?')[0]}#integrations`;
}

async function _oauthIsTenantAdmin() {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/me`, { headers: authHeader() });
    if (!resp.ok) return false;
    const me = await resp.json();
    if (!me) return false;
    return me.is_platform_admin === true || me.parent_user_id === null;
  } catch { return false; }
}

async function _oauthLoadProviders() {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/integrations/oauth/providers`, { headers: authHeader() });
    if (!resp.ok) return [];
    return _oauthNormalizeList(await resp.json());
  } catch { return []; }
}

async function _oauthLoadConnections() {
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/integrations/oauth/connections`, { headers: authHeader() });
    if (!resp.ok) return [];
    return _oauthNormalizeList(await resp.json());
  } catch { return []; }
}

let _oauthContainer = null;
let _oauthStatusEl = null;
let _oauthConnectionsHost = null;

function _oauthEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function _oauthBadge(text, warn) {
  const b = _oauthEl('span', warn ? 'text-amber-700 bg-amber-50 rounded px-2 py-1 text-xs' : 'text-green-700 bg-green-50 rounded px-2 py-1 text-xs');
  b.textContent = text;
  return b;
}

function _oauthFail(msg) {
  if (_oauthStatusEl) _oauthStatusEl.textContent = msg;
}

function _oauthClear(host) {
  const kids = Array.from(host.childNodes);
  kids.forEach((k) => host.removeChild(k));
}

function _oauthRenderProviders(host, providers, isAdmin) {
  providers.forEach((p) => {
    const name = typeof p.provider === 'string' ? p.provider : String(p.provider ?? '');
    const row = _oauthEl('div', 'oauth-provider-row flex items-center gap-2 py-1');
    row.appendChild(_oauthEl('span', 'text-sm text-gray-800', name));
    row.appendChild(_oauthBadge(p.configured ? 'Configured' : 'Not configured', !p.configured));
    if (isAdmin) {
      const btn = _oauthEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
      btn.textContent = 'Connect';
      if (!p.configured) {
        btn.disabled = true;
        btn.title = 'Not configured for this tenant — add client credentials before connecting';
      } else {
        btn.addEventListener('click', () => { _oauthConnect(name); });
      }
      row.appendChild(btn);
    }
    host.appendChild(row);
  });
}

function _oauthRenderConnections(host, rows, isAdmin) {
  const table = _oauthEl('table', 'oauth-connections-table w-full border border-gray-200 rounded-md text-sm');
  const head = _oauthEl('tr');
  const headers = ['Provider', 'Account', 'Scopes', 'Status', 'Expires', 'Created'];
  if (isAdmin) headers.push('Actions');
  headers.forEach((h) => {
    const th = _oauthEl('th', 'px-3 py-2 text-left text-xs font-medium uppercase tracking-wider');
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  // Dead statuses the API can stamp (models.py ck_oauth_connections_status allows
  // exactly active|refresh_failed|reconnect_required|revoked; oauth_refresh.py:94-96
  // defines the first three, routers/oauth_connections.py:502 stamps 'revoked').
  // Everything non-'active' warns; 'reauth_required' is not a connection status.
  const DEAD_STATUSES = ['refresh_failed', 'reconnect_required', 'revoked'];
  rows.forEach((r) => {
    const warn = DEAD_STATUSES.includes(r.status);
    const tr = _oauthEl('tr');
    const cells = [
      [typeof r.provider === 'string' ? r.provider : String(r.provider ?? ''), false],
      [r.external_account_id || '—', false],
      [Array.isArray(r.scopes) ? r.scopes.join(', ') : '—', false],
      [r.status || '—', warn],
      [_oauthWhen(r.access_token_expires_at), false],
      [_oauthWhen(r.created_at), false],
    ];
    cells.forEach((c) => {
      const td = _oauthEl('td', 'px-3 py-2');
      if (c[1]) td.appendChild(_oauthBadge(String(c[0]), true));
      else td.textContent = c[0];
      tr.appendChild(td);
    });
    if (isAdmin) {
      const td = _oauthEl('td', 'px-3 py-2');
      const btn = _oauthEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
      btn.textContent = warn ? 'Reconnect' : 'Disconnect';
      btn.addEventListener('click', () => { if (warn) _oauthConnect(String(r.provider ?? '')); else _oauthDisconnect(r.id); });
      td.appendChild(btn);
      tr.appendChild(td);
    }
    table.appendChild(tr);
  });
  host.appendChild(table);
}

async function _oauthConnect(provider) {
  const apiUrl = await getApiUrl();
  const resp = await fetch(`${apiUrl}/integrations/oauth/${encodeURIComponent(provider)}/start`, {
    method: 'POST',
    headers: { ...authHeader(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ redirect_uri: _oauthRedirectUri() }),
  });
  if (!resp.ok) { _oauthFail('Connect failed — please try again.'); return false; }
  let body = null;
  try { body = await resp.json(); } catch { body = null; }
  const url = body ? body.authorization_url : null;
  if (!url) { _oauthFail('Connect failed — unexpected response.'); return false; }
  window.location = url;
  return true;
}

async function _oauthDisconnect(id) {
  if (!window.confirm('Disconnect this OAuth connection? The account link is removed; nothing else changes.')) return false;
  const apiUrl = await getApiUrl();
  const resp = await fetch(`${apiUrl}/integrations/oauth/connections/${encodeURIComponent(String(id))}`, { method: 'DELETE', headers: authHeader() });
  if (!resp.ok) { _oauthFail('Disconnect failed — please try again.'); return false; }
  const rows = await _oauthLoadConnections();
  if (_oauthConnectionsHost) {
    _oauthClear(_oauthConnectionsHost);
    _oauthRenderConnections(_oauthConnectionsHost, rows, true);
  }
  return true;
}

async function _oauthConnectedToast() {
  const params = new URLSearchParams(_oauthSearch());
  if (params.get('oauth') !== 'connected') return false;
  const provider = params.get('provider') || 'OAuth';
  const toast = _oauthEl('div', 'oauth-connected-toast text-green-700 bg-green-50 rounded px-3 py-2 text-sm mb-3');
  toast.textContent = `Connected to ${provider}. You can close this window and return to Settings.`;
  if (_oauthContainer) _oauthContainer.appendChild(toast);
  history.replaceState(null, '', `${_oauthHref().split('?')[0]}#integrations`);
  return true;
}

export async function initializeSettingsOauthConnections() {
  const container = document.getElementById('int-oauth-connections');
  if (!container) return;
  // Idempotent: loadIntegrations() (settings.js) re-calls this after every
  // SMTP save within the same mount, not just on page navigation — clear any
  // previous render so repeated calls don't stack duplicate headings/tables.
  _oauthClear(container);
  _oauthContainer = container;
  _oauthStatusEl = _oauthEl('div', 'oauth-status text-sm text-gray-500');
  _oauthContainer.appendChild(_oauthStatusEl);
  await _oauthConnectedToast();
  const isAdmin = await _oauthIsTenantAdmin();
  const heading = _oauthEl('h3', 'text-lg font-medium text-gray-800 mb-2');
  heading.textContent = 'OAuth connections';
  _oauthContainer.appendChild(heading);
  const providersHost = _oauthEl('div', 'oauth-providers-host');
  _oauthConnectionsHost = _oauthEl('div', 'oauth-connections-host');
  _oauthContainer.appendChild(providersHost);
  _oauthContainer.appendChild(_oauthConnectionsHost);
  _oauthRenderProviders(providersHost, await _oauthLoadProviders(), isAdmin);
  _oauthRenderConnections(_oauthConnectionsHost, await _oauthLoadConnections(), isAdmin);
}

// No self-mount IIFE here: the SPA mounts pages via innerHTML (ui.js), which
// does NOT execute injected <script type="module"> tags (same bug class as
// payment-settings.html / docker-registry.html before they were fixed — see
// their comments in ui.js). settings.js imports initializeSettingsOauthConnections
// and calls it from loadIntegrations(), the same pattern used for the Mobile
// sub-tab's initializeMobileEnrollmentPage()/initializeMobileDevicesPanel().
