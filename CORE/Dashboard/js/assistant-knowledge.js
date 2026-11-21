// Dashboard/js/assistant-knowledge.js
//
// tenant-assistant-knowledge-base §5 — the "Knowledge Base" section under
// Settings -> AI. Tenant-admin only. Lets an admin enable/disable the tenant
// KB (bound to knowledge_base_enabled via /assistant/config) and populate it
// from three sources — uploaded documents, pasted text, and an existing
// storage folder (Cog) — then watch each document's ingest status.
//
// Self-contained (no cross-module imports) and self-bootstrapping: importing
// this module registers a MutationObserver that wires the section whenever it
// mounts, so it survives the SPA's innerHTML page swaps. tenant_id is never
// sent from the client — the API derives it from the authenticated principal.

// 2400UI17 — this file IS an ES module (settings.js:14 `import './assistant-knowledge.js'`,
// and it has its own `export` below).
// FIX-KNOWLEDGE-BASE-IN-CE — the Knowledge Base ships on CE, so this module keeps
// NO edition import and NO edition lookup: every /assistant/* route it calls is an
// open-core manifest entry, and the real boundary is the tenant-admin guard
// (isTenantAdmin below / require_tenant_admin server-side).

const SUPPORTED_ACCEPT = '.pdf,.docx,.md,.txt,.csv';
const POLL_MS = 3000;

let _pollTimer = null;
// Consecutive list-fetch failures. A transient blip during polling must not
// permanently freeze the list on "processing" while the backend has actually
// finished — keep retrying up to this cap, then give up (so a hard 403/500
// doesn't poll forever).
let _pollFailCount = 0;
const POLL_MAX_FAILS = 5;

function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function fmtBytes(n) {
  if (n == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB']; let v = Number(n), i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
}

// The management endpoints are tenant-admin gated (403 for anyone else), so the
// whole section is hidden for non-admins — same predicate the backend enforces
// (`_is_tenant_admin`: platform admin OR tenant-root admin, parent_user_id IS
// NULL). Fail closed: any error hiding the section rather than exposing it.
async function isTenantAdmin() {
  try {
    const resp = await fetch(`${getApiUrl()}/me`, { headers: authHeader() });
    if (!resp.ok) return false;
    const me = await resp.json();
    return !!me.is_platform_admin || me.parent_user_id === null;
  } catch {
    return false;
  }
}

export async function initAssistantKnowledge() {
  // §5 — the Knowledge Base is now its OWN top-level Settings tab
  // (main-panel-knowledge); settings.js reveals main-tab-knowledge for tenant
  // admins. This module just wires the panel content once it's in the DOM.
  const panel = document.getElementById('main-panel-knowledge');
  if (!panel || panel.dataset.akbBound === '1') return;
  panel.dataset.akbBound = '1';

  // Defence in depth: the tab is already tenant-admin-gated by settings.js and
  // every management endpoint is _is_tenant_admin-gated server-side. Bail for a
  // non-admin who somehow reaches the panel.
  const admin = await isTenantAdmin();
  if (!admin) return;

  _wireEnableToggle();
  _wireUpload();
  _wirePasteText();
  _wireFolder();

  await Promise.all([_loadConfig(), _loadFolders(), _loadDocuments()]);
}

// --- Enable/disable toggle (knowledge_base_enabled) --------------------------

function _wireEnableToggle() {
  const cb = document.getElementById('akb-enabled');
  if (!cb || cb.dataset.bound === '1') return;
  cb.dataset.bound = '1';
  cb.addEventListener('change', onToggleEnabled);
}

// FIX-KNOWLEDGE-BASE-IN-CE — no CE carve here any more: the /assistant/config and
// /assistant/knowledge* routes ship in CE (open-core/manifest.yaml api_allow +
// deny_exceptions), so the panel is live on every edition. The tenant-admin guard
// above (and require_tenant_admin server-side) is the real boundary.
async function _loadConfig() {
  const cb = document.getElementById('akb-enabled');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/config`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const cfg = await resp.json();
    if (cb) cb.checked = !!cfg.knowledge_base_enabled;
    _reflectEnabledState(!!cfg.knowledge_base_enabled);
  } catch (e) {
    _setStatus('akb-enable-status', `Couldn't load setting: ${(e && e.message) || e}`, true);
  }
}

async function onToggleEnabled(e) {
  const enabled = !!e.target.checked;
  _setStatus('akb-enable-status', 'Saving…');
  // Partial update — PUT /assistant/config uses exclude_unset, so sending only
  // knowledge_base_enabled leaves the agent config (model, prompt, etc.) intact.
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/config`, {
      method: 'PUT',
      headers: { ...authHeader(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ knowledge_base_enabled: enabled }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    _setStatus('akb-enable-status', enabled ? 'Knowledge Base enabled.' : 'Knowledge Base disabled.');
    _reflectEnabledState(enabled);
  } catch (err) {
    e.target.checked = !enabled;
    _setStatus('akb-enable-status', `Save failed: ${(err && err.message) || err}`, true);
  }
}

function _reflectEnabledState(enabled) {
  const sources = document.getElementById('akb-sources');
  if (sources) sources.classList.toggle('opacity-50', !enabled);
  const note = document.getElementById('akb-disabled-note');
  if (note) note.classList.toggle('hidden', enabled);
}

// --- Upload ------------------------------------------------------------------

function _wireUpload() {
  const drop = document.getElementById('akb-dropzone');
  const input = document.getElementById('akb-file');
  const btn = document.getElementById('akb-upload');
  if (input && input.dataset.bound !== '1') {
    input.dataset.bound = '1';
    input.addEventListener('change', () => {
      const name = input.files && input.files[0] ? input.files[0].name : '';
      const label = document.getElementById('akb-file-name');
      if (label) label.textContent = name || 'No file selected';
    });
  }
  if (btn && btn.dataset.bound !== '1') {
    btn.dataset.bound = '1';
    btn.addEventListener('click', onUpload);
  }
  if (drop && drop.dataset.bound !== '1') {
    drop.dataset.bound = '1';
    ['dragover', 'dragenter'].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.add('border-primary');
    }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault();
      drop.classList.remove('border-primary');
    }));
    drop.addEventListener('drop', (e) => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f && input) {
        input.files = e.dataTransfer.files;
        const label = document.getElementById('akb-file-name');
        if (label) label.textContent = f.name;
      }
    });
  }
}

async function onUpload() {
  const input = document.getElementById('akb-file');
  const btn = document.getElementById('akb-upload');
  const file = input && input.files && input.files[0];
  if (!file) { _setStatus('akb-upload-status', 'Choose a file first.', true); return; }
  const fd = new FormData();
  fd.append('file', file);
  if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }
  _setStatus('akb-upload-status', `Uploading "${file.name}"…`);
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge/upload`, {
      method: 'POST', headers: authHeader(), body: fd,
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    _setStatus('akb-upload-status', `Queued "${esc(body.title || file.name)}" for indexing.`);
    if (input) input.value = '';
    const label = document.getElementById('akb-file-name');
    if (label) label.textContent = 'No file selected';
    await _loadDocuments();
  } catch (err) {
    _setStatus('akb-upload-status', `Upload failed: ${(err && err.message) || err}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Upload'; }
  }
}

// --- Paste text --------------------------------------------------------------

function _wirePasteText() {
  const btn = document.getElementById('akb-text-add');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  btn.addEventListener('click', onAddText);
}

async function onAddText() {
  const titleEl = document.getElementById('akb-text-title');
  const bodyEl = document.getElementById('akb-text-body');
  const btn = document.getElementById('akb-text-add');
  const title = titleEl ? titleEl.value.trim() : '';
  const text = bodyEl ? bodyEl.value.trim() : '';
  if (!title || !text) { _setStatus('akb-text-status', 'Title and text are both required.', true); return; }
  if (btn) { btn.disabled = true; btn.textContent = 'Adding…'; }
  _setStatus('akb-text-status', 'Adding…');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge/text`, {
      method: 'POST',
      headers: { ...authHeader(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, text }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    _setStatus('akb-text-status', `Queued "${esc(title)}" for indexing.`);
    if (titleEl) titleEl.value = '';
    if (bodyEl) bodyEl.value = '';
    await _loadDocuments();
  } catch (err) {
    _setStatus('akb-text-status', `Add failed: ${(err && err.message) || err}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Add text'; }
  }
}

// --- Folder picker -----------------------------------------------------------

// Reuse the tenant's own folders (Cogs) via GET /cogs. There is no shared
// folder-picker component this module can import without reaching into another
// module, so the picker is a native <select> populated here, with a plain
// cog id input as a fallback for folders the listing doesn't surface.
function _wireFolder() {
  const btn = document.getElementById('akb-folder-add');
  if (!btn || btn.dataset.bound === '1') return;
  btn.dataset.bound = '1';
  btn.addEventListener('click', onAddFolder);
}

async function _loadFolders() {
  const sel = document.getElementById('akb-folder-select');
  if (!sel) return;
  try {
    const resp = await fetch(`${getApiUrl()}/assets?all_items=true&limit=200`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const rows = await resp.json();
    const folders = (rows || []).filter((r) => String(r.cog_type || '').includes('folder'));
    sel.innerHTML = '<option value="">— select a folder —</option>';
    folders.forEach((f) => {
      const opt = document.createElement('option');
      opt.value = String(f.id);
      opt.textContent = f.name || `folder ${f.id}`;
      sel.appendChild(opt);
    });
    if (!folders.length) {
      sel.innerHTML = '<option value="">no folders found</option>';
    }
  } catch {
    sel.innerHTML = '<option value="">could not load folders</option>';
  }
}

async function onAddFolder() {
  const sel = document.getElementById('akb-folder-select');
  const manual = document.getElementById('akb-folder-id');
  const btn = document.getElementById('akb-folder-add');
  let cogId = sel && sel.value ? parseInt(sel.value, 10) : NaN;
  if (!(cogId > 0) && manual && manual.value) cogId = parseInt(manual.value, 10);
  if (!(cogId > 0)) { _setStatus('akb-folder-status', 'Pick a folder or enter a folder id.', true); return; }
  if (btn) { btn.disabled = true; btn.textContent = 'Indexing…'; }
  _setStatus('akb-folder-status', 'Indexing folder…');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge/folder`, {
      method: 'POST',
      headers: { ...authHeader(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ cog_id: cogId }),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    const n = (body.documents || []).length;
    _setStatus('akb-folder-status', n
      ? `Queued ${n} file${n === 1 ? '' : 's'} from the folder for indexing.`
      : 'No supported files found in that folder.');
    if (manual) manual.value = '';
    await _loadDocuments();
  } catch (err) {
    _setStatus('akb-folder-status', `Folder index failed: ${(err && err.message) || err}`, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Index folder'; }
  }
}

// --- Document list -----------------------------------------------------------

const _STATUS_BADGE = {
  ready: 'bg-green-100 text-green-800',
  processing: 'bg-blue-100 text-blue-800',
  pending: 'bg-gray-100 text-gray-700',
  failed: 'bg-red-100 text-red-900',
};

async function _loadDocuments() {
  const tbody = document.getElementById('akb-tbody');
  const empty = document.getElementById('akb-empty');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const rows = data.documents || [];
    if (tbody) tbody.innerHTML = '';
    if (!rows.length) {
      if (empty) empty.classList.remove('hidden');
    } else {
      if (empty) empty.classList.add('hidden');
      rows.forEach((r) => {
        if (tbody) tbody.appendChild(_renderRow(r));
      });
    }
    _pollFailCount = 0;
    _schedulePoll(rows.some((r) => r.status === 'pending' || r.status === 'processing'));
  } catch (e) {
    _setStatus('akb-status', `Could not load documents: ${(e && e.message) || e}`, true);
    // Don't let a transient failure kill the poller: retry a bounded number of
    // times so an in-flight ingest still lands as "ready" once the blip clears.
    _pollFailCount += 1;
    if (_pollFailCount <= POLL_MAX_FAILS) _schedulePoll(true);
  }
}

function _renderRow(r) {
  const tr = document.createElement('tr');
  tr.className = 'border-b border-gray-100 dark:border-gray-700 align-top';
  const badge = _STATUS_BADGE[r.status] || 'bg-gray-100 text-gray-700';
  const err = r.status === 'failed' && r.error_message
    ? `<div class="text-xs text-red-700 mt-0.5">${esc(r.error_message)}</div>` : '';
  tr.innerHTML = `
    <td class="px-4 py-2 text-sm">${esc(r.title)}${err}</td>
    <td class="px-4 py-2 text-sm"><span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-700">${esc(r.source_type)}</span></td>
    <td class="px-4 py-2 text-sm"><span class="px-2 py-0.5 rounded-full text-xs ${badge}">${esc(r.status)}</span></td>
    <td class="px-4 py-2 text-sm">${r.chunk_count == null ? '—' : r.chunk_count}</td>
    <td class="px-4 py-2 text-sm">${fmtBytes(r.bytes)}</td>`;
  const actions = document.createElement('td');
  actions.className = 'px-4 py-2 text-sm whitespace-nowrap';
  const reindex = document.createElement('button');
  reindex.type = 'button';
  reindex.className = 'text-primary hover:underline mr-3';
  reindex.textContent = 'Reindex';
  reindex.addEventListener('click', () => onReindex(r.id));
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'text-red-600 hover:underline';
  del.textContent = 'Delete';
  del.addEventListener('click', () => onDelete(r.id, r.title));
  actions.appendChild(reindex);
  actions.appendChild(del);
  tr.appendChild(actions);
  return tr;
}

async function onReindex(id) {
  _setStatus('akb-status', 'Reindexing…');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge/${id}/reindex`, {
      method: 'POST', headers: authHeader(),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    _setStatus('akb-status', 'Reindex started.');
    await _loadDocuments();
  } catch (err) {
    _setStatus('akb-status', `Reindex failed: ${(err && err.message) || err}`, true);
  }
}

async function onDelete(id, title) {
  if (!window.confirm(`Delete "${title}" from the Knowledge Base? Its chunks will be removed.`)) return;
  _setStatus('akb-status', 'Deleting…');
  try {
    const resp = await fetch(`${getApiUrl()}/assistant/knowledge/${id}`, {
      method: 'DELETE', headers: authHeader(),
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const d = body.detail;
      const msg = (d && typeof d === 'object') ? (d.message || JSON.stringify(d)) : d;
      throw new Error(msg || `HTTP ${resp.status}`);
    }
    _setStatus('akb-status', 'Deleted.');
    await _loadDocuments();
  } catch (err) {
    _setStatus('akb-status', `Delete failed: ${(err && err.message) || err}`, true);
  }
}

// Poll while anything is still ingesting; self-stops once every document has
// settled (ready/failed). One timer, cleared and re-armed.
function _schedulePoll(anyInFlight) {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  if (!anyInFlight) return;
  if (!document.getElementById('akb-tbody')) return;
  _pollTimer = setTimeout(() => { _loadDocuments(); }, POLL_MS);
}

function _setStatus(id, msg, isError) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg || '';
  el.classList.toggle('text-red-600', !!isError);
}

// --- Bootstrap ---------------------------------------------------------------

// The SPA mounts pages via innerHTML, which does NOT execute injected <script>
// tags, so this module can't rely on a tag in settings.html firing. Instead,
// once loaded it watches for the section to appear and wires it — idempotently,
// via the panel's dataset flag — on first mount and on every later re-mount.
function _bootstrap() {
  const tryInit = () => {
    if (document.getElementById('main-panel-knowledge')) {
      initAssistantKnowledge().catch((e) => console.error('assistant-knowledge init failed:', e));
    }
  };
  tryInit();
  const target = document.getElementById('page-content') || document.body;
  if (target && !target.dataset.akbObserver) {
    target.dataset.akbObserver = '1';
    const obs = new MutationObserver(() => tryInit());
    obs.observe(target, { childList: true, subtree: true });
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _bootstrap);
  } else {
    _bootstrap();
  }
}
