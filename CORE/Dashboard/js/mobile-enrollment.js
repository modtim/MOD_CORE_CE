// Dashboard/js/mobile-enrollment.js
//
// mobile-runtime-enrollment 3.2 — "Mobile app enrollment" panel. Shows this MOD
// Core instance's config-only enrollment code + QR. A user scans it (or pastes
// the code) to point the mobile app at THIS server; login then happens against
// this instance's own Keycloak. The code carries no secret.

import { withButtonBusy } from './utils.js';
import { initializeMobileSigningInputs } from './mobile-signing.js';

async function getApiUrl() {
  return window.APP_CONFIG?.API_URL || 'http://api.localhost';
}
function authHeader() {
  const token = sessionStorage.getItem('accessToken');
  return token ? { Authorization: `Bearer ${token}` } : {};
}
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Poll a workflow run to completion, updating `statusEl` live. Calls
// onDone(ok) once terminal. Platform admins can read any run's status.
const _RUN_OK = new Set(['completed', 'success']);
const _RUN_BAD = new Set(['failed', 'failure']);
function watchRun(apiUrl, runId, statusEl, label, onDone) {
  if (!runId) { if (onDone) onDone(false); return; }
  const started = Date.now();
  let stopped = false;
  const fmt = () => {
    const secs = Math.round((Date.now() - started) / 1000);
    const m = Math.floor(secs / 60), r = secs % 60;
    return m ? `${m}m ${r}s` : `${r}s`;
  };
  const tick = async () => {
    if (stopped) return;
    try {
      const resp = await fetch(`${apiUrl}/road/workflow-runs/${runId}/status`, { headers: authHeader() });
      if (resp.ok) {
        const st = ((await resp.json()).status || '').toLowerCase();
        if (_RUN_OK.has(st)) {
          stopped = true;
          if (statusEl) statusEl.textContent = `${label} finished ✓ (${fmt()}).`;
          if (onDone) onDone(true);
          return;
        }
        if (_RUN_BAD.has(st)) {
          stopped = true;
          if (statusEl) statusEl.textContent = `${label} failed after ${fmt()} — check the run logs.`;
          if (onDone) onDone(false);
          return;
        }
        if (statusEl) statusEl.textContent = `${label} — ${st || 'running'}… (${fmt()})`;
      }
    } catch { /* transient network — keep polling */ }
    if (!stopped) setTimeout(tick, 4000);
  };
  tick();
}

// Persist in-flight rebuilds so a page refresh RE-ATTACHES to the progress
// instead of losing it. One localStorage key per kind; cleared when the run
// reaches a terminal state.
const WATCH = {
  app: { key: 'mod_mobile_build_app', btn: 'me-rebuild', status: 'me-rebuild-status', label: 'App build', orig: 'Rebuild app' },
  toolchain: { key: 'mod_mobile_build_toolchain', btn: 'me-rebuild-toolchain', status: 'me-toolchain-status', label: 'Toolchain build', orig: 'Rebuild toolchain image' },
};
const _activeWatches = new Set();

async function startWatch(kind, runId) {
  if (!runId || _activeWatches.has(runId)) return;  // dedup across tab re-opens
  _activeWatches.add(runId);
  const cfg = WATCH[kind];
  const apiUrl = await getApiUrl();
  const btn = document.getElementById(cfg.btn);
  const status = document.getElementById(cfg.status);
  try { localStorage.setItem(cfg.key, String(runId)); } catch { /* private mode */ }
  if (btn) { btn.disabled = true; btn.textContent = 'Building…'; }
  watchRun(apiUrl, runId, status, cfg.label, (ok) => {
    _activeWatches.delete(runId);
    try { localStorage.removeItem(cfg.key); } catch { /* ignore */ }
    if (btn) { btn.textContent = cfg.orig; btn.disabled = false; }
    if (ok) refreshDownload();
  });
}

// After a reload, re-attach watchers to any build that was in flight.
function resumeWatches() {
  for (const kind of Object.keys(WATCH)) {
    let runId = null;
    try { runId = localStorage.getItem(WATCH[kind].key); } catch { /* ignore */ }
    if (runId) startWatch(kind, parseInt(runId, 10));
  }
}

export async function initializeMobileEnrollmentPage() {
  // Bind listeners once — this panel now lives inside Settings → Integrations,
  // whose loader re-runs every time the tab is opened.
  const loadBtn = document.getElementById('me-refresh');
  if (loadBtn && !loadBtn.dataset.bound) {
    loadBtn.dataset.bound = '1';
    loadBtn.addEventListener('click', issueNewCode);
    const copyBtn = document.getElementById('me-copy');
    if (copyBtn) copyBtn.addEventListener('click', copyCode);
    const dlBtn = document.getElementById('me-download');
    if (dlBtn) dlBtn.addEventListener('click', downloadApk);
    const rebuildBtn = document.getElementById('me-rebuild');
    if (rebuildBtn) {
      rebuildBtn.addEventListener('click', rebuildApp);
      // BUG.B40e — build-target picker (Android / iOS / Both) next to "Rebuild app".
      // Built here, never at module level: the button itself is static markup in
      // settings.html, so the select is inserted as its sibling to stay in the
      // same flex row. Starts hidden; gateRebuild reveals it for platform admins.
      if (!document.getElementById('me-rebuild-target')) {
        const targetSel = document.createElement('select');
        targetSel.id = 'me-rebuild-target';
        targetSel.className = 'hidden text-xs px-2 py-1 border border-gray-300 rounded text-gray-700';
        [['android', 'Android'], ['ios', 'iOS'], ['both', 'Both']].forEach(([value, label]) => {
          const opt = document.createElement('option');
          opt.value = value;
          opt.textContent = label;
          targetSel.appendChild(opt);
        });
        targetSel.value = 'android';
        rebuildBtn.parentNode.insertBefore(targetSel, rebuildBtn.nextSibling);
      }
      // Where the per-build result lines (and the amber iOS refusal) go. Kept
      // separate from me-rebuild-status, which the run watcher overwrites while
      // it polls.
      if (!document.getElementById('me-rebuild-detail')) {
        const statusSpan = document.getElementById('me-rebuild-status');
        const detail = document.createElement('div');
        detail.id = 'me-rebuild-detail';
        detail.className = 'text-xs text-gray-600';
        const anchor = statusSpan || rebuildBtn;
        anchor.parentNode.insertBefore(detail, anchor.nextSibling);
      }
    }
    const toolchainBtn = document.getElementById('me-rebuild-toolchain');
    if (toolchainBtn) toolchainBtn.addEventListener('click', rebuildToolchain);
    const infoToggle = document.getElementById('me-toolchain-info-toggle');
    if (infoToggle) infoToggle.addEventListener('click', () => {
      const info = document.getElementById('me-toolchain-info');
      if (info) info.classList.toggle('hidden');
    });
  }
  await Promise.all([load(), refreshDownload(), gateRebuild()]);
  // BUG.B40c — "Build-server signing" inputs (platform admin only; the module
  // gates itself on /me and leaves its card hidden otherwise).
  try { await initializeMobileSigningInputs(); }
  catch (e) { console.error('mobile signing inputs init failed:', e); }
}

// Rebuild actions are platform-admin only: reveal the primary "Rebuild app"
// button and the secondary (advanced) toolchain block for them. The endpoints
// are gated server-side regardless.
async function gateRebuild() {
  const btn = document.getElementById('me-rebuild');
  const advanced = document.getElementById('me-advanced');
  const targetSel = document.getElementById('me-rebuild-target');
  if (!btn && !advanced) return;
  try {
    const apiUrl = await getApiUrl();
    const resp = await fetch(`${apiUrl}/me`, { headers: authHeader() });
    if (!resp.ok) return;
    const me = await resp.json();
    if (me && me.is_platform_admin === true) {
      if (btn) btn.classList.remove('hidden');
      if (advanced) advanced.classList.remove('hidden');
      if (targetSel) targetSel.classList.remove('hidden');
      // Re-attach to any build that was in flight before a page refresh.
      resumeWatches();
    }
  } catch { /* leave hidden on error */ }
}

async function rebuildToolchain() {
  const apiUrl = await getApiUrl();
  const btn = document.getElementById('me-rebuild-toolchain');
  const status = document.getElementById('me-toolchain-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  if (status) status.textContent = '';
  try {
    const resp = await fetch(`${apiUrl}/mobile/build-image`, { method: 'POST', headers: authHeader() });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${resp.status}`);
    }
    const r = await resp.json();
    if (status) status.textContent = `Toolchain build started (run #${r.workflow_run_id})…`;
    // Persist + watch; survives a page refresh.
    startWatch('toolchain', r.workflow_run_id);
  } catch (e) {
    if (status) status.textContent = `Toolchain rebuild failed: ${(e && e.message) || e}`;
    if (btn) { btn.textContent = 'Rebuild toolchain image'; btn.disabled = false; }
  }
}

async function rebuildApp() {
  const apiUrl = await getApiUrl();
  const btn = document.getElementById('me-rebuild');
  const status = document.getElementById('me-rebuild-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
  if (status) status.textContent = '';
  // BUG.B40e — the build target comes from the select created in
  // initializeMobileEnrollmentPage. Absent (or anything unexpected) falls back
  // to android, i.e. exactly the old no-target call.
  const sel = document.getElementById('me-rebuild-target');
  const picked = sel ? String(sel.value || '').trim().toLowerCase() : '';
  const target = ['ios', 'both'].includes(picked) ? picked : 'android';
  try {
    const resp = await fetch(`${apiUrl}/mobile/rebuild?target=${encodeURIComponent(target)}`, {
      method: 'POST',
      headers: authHeader(),
    });
    if (!resp.ok) {
      const detail = await resp.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${resp.status}`);
    }
    const r = await resp.json();
    renderBuildTargets(r);
    if (status) status.textContent = `Build started (run #${r.workflow_run_id})…`;
    // Persist + watch; survives a page refresh.
    startWatch('app', r.workflow_run_id);
  } catch (e) {
    if (status) status.textContent = `Rebuild failed: ${(e && e.message) || e}`;
    if (btn) { btn.textContent = 'Rebuild app'; btn.disabled = false; }
  }
}

// BUG.B40e — one line per build (target + run id), plus the amber refusal note
// when the iOS half had no macOS worker online. The API already normalised an
// unknown target to android; the route rejects it with 422 before we get here.
function renderBuildTargets(r) {
  const detail = document.getElementById('me-rebuild-detail');
  if (!detail) return;
  detail.innerHTML = '';
  const builds = Array.isArray(r.builds) && r.builds.length
    ? r.builds
    : (r.workflow_run_id ? [{ target: r.target || 'android', workflow_run_id: r.workflow_run_id }] : []);
  builds.forEach((b, i) => {
    const line = document.createElement('div');
    line.className = 'flex items-center gap-1 text-xs text-gray-700';
    line.textContent = `${i + 1}. ${b.target || 'android'} — run #${b.workflow_run_id}`;
    detail.appendChild(line);
  });
  if (r.ios_refused) {
    const warn = document.createElement('div');
    warn.className = 'mt-1 inline-flex items-center gap-1 px-2 py-1 bg-amber-100 rounded text-xs text-amber-700';
    warn.textContent = r.ios_refusal_reason || 'iOS build not started.';
    detail.appendChild(warn);
  }
}

async function refreshDownload() {
  const apiUrl = await getApiUrl();
  const btn = document.getElementById('me-download');
  const status = document.getElementById('me-download-status');
  try {
    const resp = await fetch(`${apiUrl}/mobile/status`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const s = await resp.json();
    // Gate "Rebuild app" on the toolchain being source-as-input ready. The
    // button is hidden for non-admins (gateRebuild); when visible, disable +
    // explain if the toolchain image must be rebuilt first.
    const rebuildBtn = document.getElementById('me-rebuild');
    const rebuildStatus = document.getElementById('me-rebuild-status');
    if (rebuildBtn && !rebuildBtn.classList.contains('hidden')) {
      const ready = s.toolchain_ready !== false;
      rebuildBtn.disabled = !ready;
      if (!ready && rebuildStatus && !rebuildStatus.textContent) {
        rebuildStatus.textContent = s.toolchain_reason || 'Rebuild the toolchain image first.';
      }
    }
    const qrWrap = document.getElementById('me-download-qr-wrap');
    const qrBox = document.getElementById('me-download-qr');
    if (s.android_available) {
      if (btn) btn.disabled = false;
      const mb = s.size_bytes ? ` (${(s.size_bytes / 1048576).toFixed(1)} MB)` : '';
      const when = s.built_at ? new Date(s.built_at).toLocaleString() : '';
      if (status) status.textContent = `Latest build${mb}${when ? ' · ' + when : ''}`;
      // Scannable download QR (points at the public download URL).
      if (qrBox && s.download_qr_svg) qrBox.innerHTML = s.download_qr_svg;
      if (qrWrap) qrWrap.classList.toggle('hidden', !s.download_qr_svg);
    } else {
      if (btn) btn.disabled = true;
      if (status) status.textContent = 'No build yet — run the Build Mobile App workflow.';
      if (qrWrap) qrWrap.classList.add('hidden');
    }
  } catch (e) {
    if (status) status.textContent = `Could not check build status: ${(e && e.message) || e}`;
  }
}

async function downloadApk() {
  const apiUrl = await getApiUrl();
  const btn = document.getElementById('me-download');
  const status = document.getElementById('me-download-status');
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Downloading…'; }
  try {
    // Auth-gated endpoint → fetch with the bearer token, then trigger a
    // client-side download (a plain <a> wouldn't send the Authorization header).
    const resp = await fetch(`${apiUrl}/mobile/download/android`, { headers: authHeader() });
    if (!resp.ok) throw new Error(resp.status === 404 ? 'No build available yet.' : `HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'mod.apk';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    if (status) status.textContent = `Download failed: ${(e && e.message) || e}`;
  } finally {
    if (btn) { btn.textContent = orig; btn.disabled = false; }
  }
}

let _code = '';

// Shared renderer for the enrollment code + QR (initial GET + "new code" POST).
// The code string itself is only ever written into the `me-code` textarea —
// never logged, never innerHTML'd anywhere else.
function renderEnrollment(data) {
  _code = (data && data.enroll_code) || '';
  const codeEl = document.getElementById('me-code');
  if (codeEl) codeEl.value = _code;
  const qrEl = document.getElementById('me-qr');
  if (qrEl) qrEl.innerHTML = (data && data.qr_svg) || '<p class="text-sm text-gray-500">QR unavailable (segno not installed) — use the code.</p>';
  const detailsEl = document.getElementById('me-details');
  if (detailsEl) {
    const p = (data && data.payload) || {};
    detailsEl.innerHTML = `
        <div><dt>Server</dt><dd class="font-mono break-all">${esc(p.api)}</dd></div>
        <div><dt>Client</dt><dd class="font-mono">${esc(p.client_id)}</dd></div>
        <div><dt>Realm</dt><dd class="font-mono">${esc(p.realm)}</dd></div>
        <div><dt>Org</dt><dd>${esc(p.org)}</dd></div>`;
  }
}

async function load() {
  const apiUrl = await getApiUrl();
  const status = document.getElementById('me-status');
  if (status) status.textContent = 'Loading…';
  try {
    const resp = await fetch(`${apiUrl}/onboarding/mobile-enrollment`, { headers: authHeader() });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    renderEnrollment(await resp.json());
    if (status) status.textContent = '';
  } catch (e) {
    if (status) status.textContent = `Could not load enrollment code: ${(e && e.message) || e}`;
  }
}

// BUG.B41 — "new enrollment code" button. The GET above is a pure function of
// public config, so re-fetching it returns a byte-identical code and the panel
// looked frozen; this POSTs /mobile-enrollment/new-code, which carries a fresh
// per-issuance nonce, then re-renders BOTH the code text and the QR. withButtonBusy
// gives the spinner/disable lockout; failures go to the shared error modal.
async function issueNewCode() {
  const btn = document.getElementById('me-refresh');
  const status = document.getElementById('me-status');
  let issued = null;
  try {
    await withButtonBusy(btn, async () => {
      const apiUrl = await getApiUrl();
      const resp = await fetch(`${apiUrl}/onboarding/mobile-enrollment/new-code`, {
        method: 'POST',
        headers: authHeader(),
      });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${resp.status}`);
      }
      issued = await resp.json();
    }, { busyLabel: 'Issuing…' });
  } catch (e) {
    const msg = `Could not issue a new enrollment code: ${(e && e.message) || e}`;
    if (status) status.textContent = msg;
    if (window.showError) await window.showError(msg, 'Mobile enrollment');
    return;
  }
  if (!issued) return;  // busy lockout: another press was already in flight
  renderEnrollment(issued);
  if (status) status.textContent = '';
}

async function copyCode() {
  // Shared copy helper (js/utils.js): execCommand fallback on plain http
  // (LAN installs); the field stays selectable as the manual fallback.
  const b = document.getElementById('me-copy');
  await window.modCopyText(_code, b);
}
