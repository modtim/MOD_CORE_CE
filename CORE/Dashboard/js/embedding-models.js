// Dashboard/js/embedding-models.js
//
// Platform-admin: upload embedding models into MinIO (platform-models/embeddings)
// so the platform never needs HuggingFace at build/deploy. Upload a .tar.gz of a
// sentence-transformers model dir (config.json at the root).

async function getApiUrl() {
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

export async function initializeEmbeddingModelsPage() {
  const form = document.getElementById('em-form');
  if (form && !form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', onUpload);
  }
  await loadList();
  pollMigration();
}

// Poll the dimension-migration job (re-embed triggered by changing the AI
// Assistant's embedding model) and surface progress. Self-stops when idle/done.
let _migTimer = null;
async function pollMigration() {
  const box = document.getElementById('em-migration');
  const text = document.getElementById('em-migration-text');
  if (!box) return;
  const apiUrl = await getApiUrl();
  try {
    const s = await fetch(`${apiUrl}/embedding-models/migration-status`, { headers: authHeader() }).then(r => r.json());
    if (s.state === 'running') {
      box.classList.remove('hidden');
      const store = { user_rag: 'User RAG', wizard: 'Node Wizard', docs: 'MOD docs' }[s.target] || s.target || '';
      const model = s.model ? ` → ${esc(s.model)}` : '';
      // Only the user-RAG store migrates per-tenant; wizard/docs are one op.
      const progress = (s.target === 'user_rag' && s.total_tenants)
        ? `: ${s.done_tenants}/${s.total_tenants} tenants` : '…';
      if (text) text.textContent = `Re-embedding ${esc(store)}${model}${progress}`;
      clearTimeout(_migTimer);
      _migTimer = setTimeout(pollMigration, 2500);
    } else if (s.state === 'done' && s.finished_at && (Date.now() / 1000 - s.finished_at) < 30) {
      // Briefly show completion, then hide.
      box.classList.remove('hidden');
      const errs = (s.results || []).filter(r => r.status === 'error').length;
      if (text) text.textContent = `Re-embed complete: ${s.done_tenants} tenants${errs ? `, ${errs} failed` : ''}.`;
      clearTimeout(_migTimer);
      _migTimer = setTimeout(() => box.classList.add('hidden'), 8000);
    } else {
      box.classList.add('hidden');
    }
  } catch {
    box.classList.add('hidden');
  }
}

async function loadList() {
  const apiUrl = await getApiUrl();
  const tbody = document.getElementById('em-tbody');
  const empty = document.getElementById('em-empty');
  try {
    const resp = await fetch(`${apiUrl}/embedding-models`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const rows = await resp.json();
    if (tbody) tbody.innerHTML = '';
    if (!rows.length) { if (empty) empty.classList.remove('hidden'); return; }
    if (empty) empty.classList.add('hidden');
    rows.forEach((r) => {
      const tr = document.createElement('tr');
      tr.className = 'border-b border-gray-100 dark:border-gray-700';
      // §5.4/§5.5 — the TYPE is shown, because a consumer picking "a model"
      // must not be handed the wrong kind. A dimension is an embedding
      // property; a language model has none, so the column reads '—' rather
      // than implying a missing value.
      const dim = (r.dimension == null) ? '—' : `${r.dimension}-d`;
      const isLlm = r.type === 'llm';
      const typeBadge = isLlm
        ? '<span class="px-2 py-0.5 rounded-full text-xs bg-indigo-100 text-indigo-800">language model</span>'
        : '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-700">embedding</span>';
      // §4.7 — a stored model nothing in the fleet can host is worth knowing
      // here, not at deploy time.
      let host = '';
      if (isLlm && r.servable === false) {
        host = `<div class="text-xs text-red-700 mt-0.5">${esc(r.unservable_reason || 'No worker can host this model.')}</div>`;
      } else if (isLlm && r.servable === true) {
        host = '<div class="text-xs text-green-700 mt-0.5">This fleet can host it.</div>';
      }
      tr.innerHTML = `<td class="px-4 py-2 text-sm font-mono">${esc(r.name)}${host}</td>
        <td class="px-4 py-2 text-sm">${typeBadge}</td>
        <td class="px-4 py-2 text-sm">${dim}</td>
        <td class="px-4 py-2 text-sm">${r.files}</td>
        <td class="px-4 py-2 text-sm">${fmtBytes(r.bytes)}</td>`;
      if (tbody) tbody.appendChild(tr);
    });
  } catch (e) {
    const status = document.getElementById('em-status');
    if (status) status.textContent = `Could not load models: ${(e && e.message) || e}`;
  }
}

async function onUpload(e) {
  e.preventDefault();
  const apiUrl = await getApiUrl();
  const name = document.getElementById('em-name')?.value?.trim();
  const file = document.getElementById('em-file')?.files?.[0];
  const status = document.getElementById('em-status');
  const btn = document.getElementById('em-upload');
  if (!name || !file) { if (status) status.textContent = 'Name + a .tar.gz are required.'; return; }
  const fd = new FormData();
  fd.append('name', name);
  fd.append('archive', file);
  // §5.4 — the upload declares what KIND of model this is. The API requires it:
  // validation and storage differ by kind, and a wrong guess offers an
  // embedding model where a language model is wanted, which fails on a worker
  // at serve time rather than here.
  const typeSel = document.getElementById('em-model-type');
  fd.append('model_type', typeSel ? typeSel.value : 'embedding');
  if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }
  if (status) status.textContent = 'Uploading + validating the model…';
  try {
    const resp = await fetch(`${apiUrl}/embedding-models`, {
      method: 'POST', headers: authHeader(), body: fd,
    });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
    const dimTxt = (body.dimension == null) ? 'dimension unknown' : `${body.dimension}-d`;
    if (status) {
      const warns = (body.warnings || []);
      status.innerHTML =
        `Uploaded "${esc(body.name)}" — ${body.files} files, ${fmtBytes(body.bytes)}, ${dimTxt}.` +
        (warns.length
          ? `<ul class="mt-2 list-disc list-inside text-amber-600 dark:text-amber-400">${
              warns.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`
          : '');
    }
    const nameEl = document.getElementById('em-name'); if (nameEl) nameEl.value = '';
    const fileEl = document.getElementById('em-file'); if (fileEl) fileEl.value = '';
    await loadList();
  } catch (err) {
    if (status) status.textContent = `Upload failed: ${(err && err.message) || err}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Upload model'; }
  }
}
