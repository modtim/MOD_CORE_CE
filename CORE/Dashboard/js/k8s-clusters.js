// Dashboard/js/k8s-clusters.js
//
// GT.D8 (launch-completion-megaplan) — "Kubernetes clusters" card inside
// Settings -> Integrations. Lists the tenant's registered k8s clusters
// (name, Active badge, In-cluster / kubeconfig-ref mode, probe summary:
// GPU vendor + node count + GPU memory from cluster_caps), offers Add
// cluster (name + Vault REFERENCE of the kubeconfig, or an in-cluster
// checkbox), Probe (POST /{id}/probe) and Remove (confirm() -> soft
// DELETE -> refresh). A rejected create returns
// 402 and the card shows "This cluster could not be added".
// The whole card hides itself when the API returns 404/403 (router not
// mounted / not permitted for this tenant) - no empty panel, no error.
//
// API (Dashboard API_URL already ends in /v1 - paths here must NOT add it):
//   GET    /k8s/clusters            -> {items:[{id,name,kubeconfig_vault_ref,
//                                        in_cluster,active}], active_count}
//   POST   /k8s/clusters            {name, kubeconfig_vault_ref?, in_cluster}
//   POST   /k8s/clusters/{id}/probe -> {id,name,cluster_caps,caps_probed_at,
//                                        probe_error}
//   DELETE /k8s/clusters/{id}       -> soft delete (tombstone)
//
// SECURITY: only a Vault REFERENCE is ever accepted or shown - never raw
// kubeconfig text. The form rejects anything that looks like kubeconfig
// content or a secret before it reaches the API (routers/k8s_clusters.py
// stores kubeconfig_vault_ref only; services/k8s_cluster_registry.py).
// Every server string goes through textContent - this file never touches
// innerHTML, so no server string can inject markup. Error bodies are never
// echoed; only fixed, safe messages are shown (the 402 detail is mapped to
// one fixed sentence). Auth uses the same sessionStorage accessToken +
// authHeader() helper the other settings panels use.
//
// No self-mount IIFE: ui.js mounts pages via innerHTML, which never executes
// an injected <script type="module"> tag. settings.js imports
// initializeK8sClustersPage and calls it from loadIntegrations().

async function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function _k8sWhen(v) {
  if (v == null || v === '') return '—';
  const d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  return d.toLocaleString();
}

function _k8sNormalizeList(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.items)) return payload.items;
  return [];
}

// Client-side guard: the field holds a Vault path (e.g.
// "vault://mod/k8s/gpu2-kubeconfig"), never the kubeconfig itself. Anything
// that smells like file content, YAML, a PEM block or a secret header is
// refused here so raw kubeconfig text is never sent AND never rendered.
function _k8sLooksRaw(value) {
  if (value == null) return false;
  const v = String(value);
  if (v.length > 512) return true;
  if (/[\r\n]/.test(v)) return true;
  const NEEDLES = ['api_key', 'api_endpoint', 'endpoint:', '-----begin',
    'bearer ', 'authorization', 'access_token', 'refresh_token',
    'private key', 'kind:', 'services:', 'insecureuse', 'ssh-rsa',
    'client-key-data', 'client-certificate-data', 'certificate-authority-data',
    'token:', 'password:', 'kind: config'];
  const low = v.toLowerCase();
  return NEEDLES.some((n) => low.includes(n));
}

function _k8sEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function _k8sBadge(text, warn) {
  const b = _k8sEl('span', warn ? 'text-amber-700 bg-amber-50 rounded px-2 py-1 text-xs'
                                  : 'text-green-700 bg-green-50 rounded px-2 py-1 text-xs');
  b.textContent = text;
  return b;
}

function _k8sFail(msg) {
  if (_k8sStatusEl) _k8sStatusEl.textContent = msg;
}

function _k8sClear(host) {
  const kids = Array.from(host.childNodes);
  kids.forEach((k) => host.removeChild(k));
}
async function _k8sLoadClusters() {
  // Returns {rows, hidden}: hidden = the router is not mounted / not
  // permitted for this tenant (404/403) -> the whole card hides.
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/k8s/clusters`, { headers: authHeader() });
    if (!resp.ok) return { rows: [], hidden: resp.status === 404 || resp.status === 403 };
    let payload = null;
    try { payload = await resp.json(); } catch { payload = null; }
    return { rows: _k8sNormalizeList(payload), hidden: false };
  } catch { return { rows: [], hidden: false }; }
}

// cluster_caps is NOT in the list response model (ClusterOut in
// routers/k8s_clusters.py) - only POST /{id}/probe returns it. Cache probe
// results client-side keyed by row id so the summary survives the refresh.
let _k8sCapsCache = {};

function _k8sCapsText(caps) {
  // Probe summary from cluster_caps (services/k8s_cluster_caps.py): vendor,
  // node count, GPU memory. Every value is rendered as text, never markup.
  if (!caps || typeof caps !== 'object') return 'not probed';
  const parts = [];
  if (caps.vendor) parts.push(`vendor ${caps.vendor}`);
  const nodes = Array.isArray(caps.nodes) ? caps.nodes.length : null;
  if (nodes != null) parts.push(`nodes ${nodes}`);
  if (caps.gpu_memory_gb != null) parts.push(`GPU ${caps.gpu_memory_gb} GB`);
  if (caps.kai === true) parts.push('KAI');
  if (parts.length === 0) return 'probed (no GPU caps)';
  return parts.join(' · ');
}

function _k8sRenderClusters(host, rows, isAdmin) {
  const table = _k8sEl('table', 'k8s-clusters-table w-full border border-gray-200 rounded-md text-sm');
  const head = _k8sEl('tr');
  const headers = ['Cluster', 'Mode', 'Probe', 'Probed at'];
  if (isAdmin) headers.push('Actions');
  headers.forEach((h) => {
    const th = _k8sEl('th', 'px-3 py-2 text-left text-xs font-medium uppercase tracking-wider');
    th.textContent = h;
    head.appendChild(th);
  });
  table.appendChild(head);
  rows.forEach((r) => {
    const tr = _k8sEl('tr');
    const name = typeof r.name === 'string' ? r.name : String(r.name ?? '');
    const mode = r.in_cluster ? 'in-cluster' : (r.kubeconfig_vault_ref ? 'kubeconfig (Vault ref)' : '—');
    const cells = [
      [name, false],
      [mode, false],
      [_k8sCapsText(r.cluster_caps || _k8sCapsCache[r.id]), false],
      [_k8sWhen(r.caps_probed_at || _k8sCapsCache[`${r.id}_at`]), false],
    ];
    cells.forEach((c) => {
      const td = _k8sEl('td', 'px-3 py-2');
      td.textContent = c[0];
      tr.appendChild(td);
    });
    if (!r.active) {
      const td = _k8sEl('td', 'px-3 py-2');
      td.appendChild(_k8sBadge('Inactive', true));
      tr.appendChild(td);
    }
    if (isAdmin) {
      const td = _k8sEl('td', 'px-3 py-2');
      const probeBtn = _k8sEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
      probeBtn.textContent = 'Probe';
      probeBtn.addEventListener('click', () => { _k8sProbe(r.id); });
      td.appendChild(probeBtn);
      const rmBtn = _k8sEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
      rmBtn.textContent = 'Remove';
      rmBtn.addEventListener('click', () => { _k8sRemove(r.id); });
      td.appendChild(rmBtn);
      tr.appendChild(td);
    }
    table.appendChild(tr);
  });
  host.appendChild(table);
}

function _k8sRenderForm(host) {
  host.appendChild(_k8sEl('h4', 'text-md font-medium text-gray-700 mt-4 mb-2', 'Add cluster'));
  host.appendChild(_k8sEl('p', 'text-xs text-gray-500 mb-2',
    'Store the kubeconfig in Vault first; paste only the reference (e.g. vault://mod/k8s/gpu2-kubeconfig). Raw kubeconfig text is never accepted or shown.'));
  const nameInput = _k8sEl('input', 'w-full border border-gray-300 rounded-md px-3 py-2 text-sm');
  nameInput.id = 'k8s-cluster-name';
  nameInput.type = 'text';
  host.appendChild(nameInput);
  host.appendChild(_k8sEl('label', 'text-xs text-gray-500', 'Cluster name'));
  const refInput = _k8sEl('input', 'w-full border border-gray-300 rounded-md px-3 py-2 text-sm');
  refInput.id = 'k8s-cluster-vault-ref';
  refInput.type = 'text';
  host.appendChild(refInput);
  host.appendChild(_k8sEl('label', 'text-xs text-gray-500', 'Vault reference of the kubeconfig'));
  const inClusterInput = _k8sEl('input', 'px-2 py-1');
  inClusterInput.id = 'k8s-cluster-in-cluster';
  inClusterInput.type = 'checkbox';
  host.appendChild(inClusterInput);
  host.appendChild(_k8sEl('label', 'text-xs text-gray-500', 'or use the API process in-cluster ServiceAccount (no kubeconfig)'));
  const btn = _k8sEl('button', 'px-3 py-1 text-xs rounded border-gray-300');
  btn.textContent = 'Add cluster';
  btn.addEventListener('click', () => { _k8sAdd(); });
  host.appendChild(btn);
}

async function _k8sAdd() {
  const nameInput = document.getElementById('k8s-cluster-name');
  const refInput = document.getElementById('k8s-cluster-vault-ref');
  const inClusterInput = document.getElementById('k8s-cluster-in-cluster');
  const name = nameInput ? String(nameInput.value || '').trim() : '';
  const vaultRef = refInput ? String(refInput.value || '').trim() : '';
  const inCluster = inClusterInput ? (inClusterInput.type === 'checkbox' ? inClusterInput.checked : String(inClusterInput.value || '').toLowerCase() === 'true') : false;
  if (!name) { _k8sFail('Enter a cluster name.'); return false; }
  if (!inCluster && !vaultRef) { _k8sFail('Paste the Vault reference of the kubeconfig, or check "use the in-cluster ServiceAccount".'); return false; }
  if (inCluster && vaultRef) { _k8sFail('Choose one: a Vault reference OR in-cluster.'); return false; }
  if (vaultRef && _k8sLooksRaw(vaultRef)) { _k8sFail('That looks like kubeconfig content, not a Vault reference. Store the kubeconfig in Vault first; paste only the reference.'); return false; }
  const apiUrl = await getApiUrl();
  const resp = await fetch(`${apiUrl}/k8s/clusters`, {
    method: 'POST',
    headers: { ...authHeader(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, in_cluster: inCluster, ...(vaultRef ? { kubeconfig_vault_ref: vaultRef } : {}) }),
  });
  if (resp.status === 402) { _k8sFail('This cluster could not be added.'); return false; }
  if (resp.status === 409) { _k8sFail('A cluster with that name already exists.'); return false; }
  if (!resp.ok) { _k8sFail('Add cluster failed — please try again.'); return false; }
  const rows = await _k8sLoadClusters();
  if (rows.hidden) return false;
  if (_k8sListHost) {
    _k8sClear(_k8sListHost);
    _k8sRenderClusters(_k8sListHost, rows.rows, true);
  }
  _k8sFail('');
  return true;
}

async function _k8sProbe(id) {
  const apiUrl = await getApiUrl();
  const resp = await fetch(`${apiUrl}/k8s/clusters/${encodeURIComponent(String(id))}/probe`, { method: 'POST', headers: authHeader() });
  if (!resp.ok) { _k8sFail('Probe failed — please try again.'); return false; }
  let body = null;
  try { body = await resp.json(); } catch { body = null; }
  if (body && body.id != null) {
    _k8sCapsCache[body.id] = body.cluster_caps || null;
    _k8sCapsCache[`${body.id}_at`] = body.caps_probed_at;
  }
  // A failed probe returns 200 with conservative caps + probe_error
  // (ClusterProbeOut in routers/k8s_clusters.py): show a fixed hint, never
  // the server string.
  if (body && body.probe_error != null && String(body.probe_error) !== '') {
    _k8sFail('Probe failed - capabilities shown are a conservative default.');
  }
  const rows = await _k8sLoadClusters();
  if (_k8sListHost) {
    _k8sClear(_k8sListHost);
    _k8sRenderClusters(_k8sListHost, rows.rows, true);
  }
  return true;
}

async function _k8sRemove(id) {
  if (!window.confirm('Remove this Kubernetes cluster? The registry row is soft-deleted (its Vault reference is kept in Vault; nothing in Vault is touched).')) return false;
  const apiUrl = await getApiUrl();
  const resp = await fetch(`${apiUrl}/k8s/clusters/${encodeURIComponent(String(id))}`, { method: 'DELETE', headers: authHeader() });
  if (!resp.ok) { _k8sFail('Remove failed — please try again.'); return false; }
  const rows = await _k8sLoadClusters();
  if (_k8sListHost) {
    _k8sClear(_k8sListHost);
    _k8sRenderClusters(_k8sListHost, rows.rows, true);
  }
  return true;
}

let _k8sContainer = null;
let _k8sStatusEl = null;
let _k8sListHost = null;

export async function initializeK8sClustersPage() {
  // pages/settings.html ships no int-k8s-clusters container (the brief allows
  // editing only the settings.js import/call lines), so the module builds its
  // own host and appends it to the Integrations panel; an existing container
  // wins if markup ever adds one.
  let container = document.getElementById('int-k8s-clusters');
  if (!container) {
    const panel = document.getElementById('main-panel-integrations');
    if (!panel) return;
    // Idempotent: loadIntegrations() re-calls this init after every SMTP save
    // in the same mount, so reuse the host we appended on a previous call
    // instead of stacking a second one.
    // panel.childNodes is a live NodeList: array-like but has no .filter, so
    // copy it to a real Array first (`|| []` never fires - a NodeList is truthy).
    const existing = Array.from(panel.childNodes || []).filter((n) => n && n.id === 'int-k8s-clusters')[0];
    if (existing) { container = existing; }
    else {
      container = document.createElement('div');
      container.id = 'int-k8s-clusters';
      container.className = 'k8s-clusters-panel';
      panel.appendChild(container);
    }
  }
  const first = await _k8sLoadClusters();
  if (first.hidden) {
    // Router not mounted / not permitted: hide the whole card (no empty
    // panel, no error text).
    container.classList.add('hidden');
    return;
  }
  _k8sClear(container);
  _k8sContainer = container;
  _k8sStatusEl = _k8sEl('div', 'k8s-status text-sm text-gray-500');
  _k8sContainer.appendChild(_k8sStatusEl);
  const heading = _k8sEl('h3', 'text-lg font-medium text-gray-800 mb-2');
  heading.textContent = 'Kubernetes clusters';
  _k8sContainer.appendChild(heading);
  const intro = _k8sEl('p', 'text-sm text-gray-500 mb-3');
  intro.textContent = 'Register the Kubernetes cluster your workers dispatch on. The kubeconfig itself stays in Vault — this page only stores its reference.';
  _k8sContainer.appendChild(intro);
  _k8sListHost = _k8sEl('div', 'k8s-clusters-host');
  _k8sContainer.appendChild(_k8sListHost);
  _k8sRenderClusters(_k8sListHost, first.rows, true);
  const formHost = _k8sEl('div', 'k8s-add-form-host');
  _k8sContainer.appendChild(formHost);
  _k8sRenderForm(formHost);
}

// No self-mount IIFE: ui.js mounts pages via innerHTML, which never executes
// an injected <script type="module"> tag (same bug class as
// payment-settings.html / docker-registry.html). settings.js imports
// initializeK8sClustersPage and calls it from loadIntegrations().
