// Dashboard/js/mobile-signing.js
//
// BUG.B40c — "Build-server signing" section of the Settings → Integrations →
// Mobile app panel (platform admin only). Renders the signing-input manifest
// from GET /mobile/signing/inputs as two groups (Android / iOS), one row per
// input: label, type badge, required-for badges, a state chip
// ("Set · <fingerprint>" / "Not set") and an input:
//   plain -> text field prefilled with the (non-secret) current value
//   str   -> password field (value never rendered back by the API)
//   file  -> file picker, read CLIENT-SIDE with FileReader -> base64
//            (>2 MB rejected before any request; the server 413s too)
// Save / Clear buttons per row. Secret values are NEVER echoed back — only the
// fingerprint chip. API errors are shown inline on the row.

import { withButtonBusy } from './utils.js';

const _MAX_FILE_BYTES = 2 * 1024 * 1024; // mirrors the server's 2 MB / 413 cap

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function apiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}

function slug(key) {
  return String(key).replace(/[^A-Za-z0-9]+/g, '-').toLowerCase();
}

function authHeaders() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function detailOf(err) {
  if (!err) return 'Request failed';
  const d = err.detail || err.error || err.message;
  if (typeof d === 'string') return d;
  if (d && typeof d === 'object') return JSON.stringify(d);
  return 'Request failed';
}

function setStatus(el, text) {
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('text-red-600', !!text);
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function chipHtml(entry, id) {
  const set = entry.state === 'set';
  const fp = entry.fingerprint ? ` · ${esc(entry.fingerprint)}` : '';
  const cls = set ? 'bg-emerald-50 text-emerald-600' : 'bg-red-50 text-red-700';
  const txt = set ? `Set${fp}` : 'Not set';
  return `<span id="ms-chip-${id}" class="inline-flex items-center gap-1 rounded px-2 py-1 text-xs ${cls}">${txt}</span>`;
}

function rowHtml(entry) {
  const id = slug(entry.key);
  const type = entry.type;
  const reqBadges = (entry.required_for || [])
    .map(r => `<span class="inline-flex rounded px-1 py-0 text-xs uppercase text-gray-500">${esc(r)}</span>`)
    .join(' ');
  let field = '';
  if (type === 'plain') {
    field = `<input id="ms-in-${id}" aria-label="${esc(entry.label || entry.key)}" type="text" value="${esc(entry.value || '')}"
      class="w-full rounded border border-gray-300 text-sm p-1 font-mono"></input>`;
  } else if (type === 'str') {
    field = `<input id="ms-in-${id}" aria-label="${esc(entry.label || entry.key)}" type="password" placeholder="••••••"
      class="w-full rounded border border-gray-300 text-sm p-1 font-mono"></input>`;
  } else {
    field = `<input id="ms-in-${id}" aria-label="${esc(entry.label || entry.key)}" type="file" accept=".p12,.jks,.json,.cer,.p7b,.p8"
      class="w-full rounded border border-gray-300 text-sm"></input>`;
  }
  return `<div id="ms-row-${id}" data-ms-key="${esc(entry.key)}" class="pb-2 pt-2 border-b border-gray-100 flex flex-wrap items-center gap-3">
    <span class="text-sm text-gray-800">${esc(entry.label || entry.key)}</span>
    <span class="inline-flex rounded px-1 py-0 text-xs uppercase text-gray-400">${esc(type)}</span>
    ${reqBadges}
    ${chipHtml(entry, id)}
    ${field}
    <span id="ms-save-${id}" role="button" tabindex="0" class="inline-flex rounded px-2 py-1 text-xs border border-gray-300 hover:bg-gray-50">Save</span>
    <span id="ms-clear-${id}" role="button" tabindex="0" class="inline-flex rounded px-2 py-1 text-xs border border-gray-300 hover:bg-gray-50">Clear</span>
    <span id="ms-status-${id}" class="block text-xs text-gray-400"></span>
  </div>`;
}

function groupHtml(title, entries) {
  const rows = entries.map(rowHtml).join('');
  return `<div class="bg-white border border-gray-200 rounded p-4">
    <h5 class="text-sm font-semibold uppercase text-gray-600 mb-1">${esc(title)}</h5>
    <div class="flex flex-col">${rows}</div>
  </div>`;
}

function summaryText(inputs) {
  const caps = ['debug', 'release', 'store', 'push'];
  const parts = caps.map(cap => {
    const need = inputs.filter(i => (i.required_for || []).includes(cap));
    if (!need.length) return `${cap}: n/a`;
    return `${cap}: ${need.every(i => i.state === 'set') ? 'ready' : 'not ready'}`;
  });
  return `Signing readiness — ${parts.join(' · ')}. iOS final signing needs a Mac/iOS build host.`;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
export async function initializeMobileSigningInputs() {
  const card = document.getElementById('me-signing-card');
  if (!card) return;
  const isAdmin = await isPlatformAdmin();
  if (!isAdmin) return; // card stays hidden for non-platform-admins
  card.classList.remove('hidden');
  await Promise.all([render(), bindRows()]);
}

async function isPlatformAdmin() {
  try {
    const resp = await fetch(`${await apiUrl()}/me`, { headers: authHeaders() });
    if (!resp.ok) return false;
    const me = await resp.json();
    return !!me && me.is_platform_admin === true;
  } catch {
    return false;
  }
}

async function render() {
  const groupsEl = document.getElementById('me-signing-groups');
  const summaryEl = document.getElementById('me-signing-summary');
  if (!groupsEl) return;
  try {
    const resp = await fetch(`${await apiUrl()}/mobile/signing/inputs`, { headers: authHeaders() });
    if (!resp.ok) {
      if (summaryEl) summaryEl.textContent = `Signing inputs unavailable: ${resp.status}`;
      return;
    }
    const manifest = await resp.json();
    const inputs = (manifest && manifest.inputs) || [];
    const android = inputs.filter(i => /android/i.test(i.key));
    const ios = inputs.filter(i => /ios|apple/i.test(i.key));
    const other = inputs.filter(i => !/android|ios|apple/i.test(i.key));
    groupsEl.innerHTML =
      groupHtml('Android', android) + groupHtml('iOS', ios) +
      (other.length ? groupHtml('Other', other) : '');
    if (summaryEl) summaryEl.textContent = summaryText(inputs);
  } catch { /* leave the card empty on a failed load */ }
}

async function bindRows() {
  const groupsEl = document.getElementById('me-signing-groups');
  if (!groupsEl) return;
  for (const row of groupsEl.querySelectorAll('[id^="ms-row-"]')) {
    const id = row.id.replace(/^ms-row-/, '');
    const key = row.dataset.msKey || '';
    const saveBtn = document.getElementById(`ms-save-${id}`);
    const clearBtn = document.getElementById(`ms-clear-${id}`);
    const inputEl = document.getElementById(`ms-in-${id}`);
    const statusEl = document.getElementById(`ms-status-${id}`);
    if (saveBtn && !saveBtn.dataset.bound) {
      saveBtn.dataset.bound = '1';
      saveBtn.addEventListener('click', () => saveRow(key, id, inputEl, statusEl));
    }
    if (clearBtn && !clearBtn.dataset.bound) {
      clearBtn.dataset.bound = '1';
      clearBtn.addEventListener('click', () => clearRow(key, id, inputEl, statusEl));
    }
    if (inputEl && inputEl.type === 'file' && !inputEl.dataset.bound) {
      inputEl.dataset.bound = '1';
      inputEl.addEventListener('change', () => uploadFile(key, id, inputEl, statusEl));
    }
  }
}

// ---------------------------------------------------------------------------
// Row actions — PUT /mobile/signing/inputs/{key}, DELETE same path.
// fetchApi paths never include /v1.
// ---------------------------------------------------------------------------
async function putValue(key, id, body, statusEl) {
  const url = `${await apiUrl()}/mobile/signing/inputs/${encodeURIComponent(key)}`;
  const resp = await fetch(url, {
    method: 'PUT',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    let err = {};
    try { err = await resp.json(); } catch { err = { detail: `HTTP ${resp.status}` }; }
    setStatus(statusEl, detailOf(err));
    return;
  }
  const out = await resp.json();
  refreshChip(id, out);
  setStatus(statusEl, '');
}

async function deleteValue(key, id, statusEl) {
  const url = `${await apiUrl()}/mobile/signing/inputs/${encodeURIComponent(key)}`;
  const resp = await fetch(url, { method: 'DELETE', headers: authHeaders() });
  if (!resp.ok) {
    let err = {};
    try { err = await resp.json(); } catch { err = { detail: `HTTP ${resp.status}` }; }
    setStatus(statusEl, detailOf(err));
    return;
  }
  const out = await resp.json();
  refreshChip(id, out);
  setStatus(statusEl, '');
}

function refreshChip(id, entry) {
  if (!entry) return;
  const chip = document.getElementById(`ms-chip-${id}`);
  if (!chip) return;
  const set = entry.state === 'set';
  const fp = entry.fingerprint ? ` · ${entry.fingerprint}` : '';
  chip.textContent = set ? `Set${fp}` : 'Not set';
  chip.classList.toggle('bg-emerald-50', set);
  chip.classList.toggle('text-emerald-600', set);
  chip.classList.toggle('bg-red-50', !set);
  chip.classList.toggle('text-red-700', !set);
}

async function saveRow(key, id, inputEl, statusEl) {
  if (!key || !inputEl) return;
  if (inputEl.type === 'file') {
    setStatus(statusEl, 'Pick the file above (the picker uploads it).');
    return;
  }
  const value = inputEl.value;
  if (value == null || value === '') { setStatus(statusEl, 'Type a value first.'); return; }
  const btn = document.getElementById(`ms-save-${id}`);
  await withButtonBusy(btn, async () => {
    await putValue(key, id, { value }, statusEl);
  });
}

async function clearRow(key, id, inputEl, statusEl) {
  if (!key) return;
  const btn = document.getElementById(`ms-clear-${id}`);
  await withButtonBusy(btn, async () => {
    await deleteValue(key, id, statusEl);
    if (inputEl && inputEl.type === 'text') inputEl.value = '';
  });
}

async function uploadFile(key, id, inputEl, statusEl) {
  if (!key) return;
  const file = inputEl.files && inputEl.files[0]; // <input type="file"> change event
  if (!file) { setStatus(statusEl, 'Select a file above.'); return; }
  if (file.size > _MAX_FILE_BYTES) {
    setStatus(statusEl, `Too large: ${file.name} is over the 2 MB limit.`);
    return;
  }
  const btn = document.getElementById(`ms-save-${id}`);
  await withButtonBusy(btn, async () => {
    const b64 = await readFileAsBase64(file);
    if (b64 == null) { setStatus(statusEl, 'Could not read the file.'); return; }
    await putValue(key, id, { file_b64: b64 }, statusEl);
  });
}

// FileReader -> base64 (strip the data: URL prefix). Never displayed back.
async function readFileAsBase64(file) {
  try {
    const dataUrl = await new Promise((promise) => {
      const reader = new FileReader();
      reader.onload = () => promise(reader.result);
      reader.onerror = () => promise(null);
      reader.readAsDataURL(file);
    });
    if (!dataUrl) return null;
    const comma = dataUrl.indexOf(',');
    return comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
  } catch {
    return null;
  }
}
