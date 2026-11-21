// Dashboard/js/assistant-model-picker.js
//
// FIX-CE-ASSISTANT-MODEL-PICKER-P2 — the Settings -> AI picker that WRITES
// `assistant.model_choice` (the key the reader already ships: registered in
// routers/support_chat/_shared.py, consulted by `_llm_env` BEFORE the slot
// chain). A person picks ONE of the models THEY actually have deployed — this
// tenant's stacks and its external LLM connections — for the assistant /
// support surface.
//
// Self-contained and self-bootstrapping (same pattern as
// assistant-knowledge.js): importing this module registers a MutationObserver
// that mounts the picker whenever the AI Configuration panel appears, so it
// survives the SPA's innerHTML page swaps. settings.html ships no markup for
// it — this module renders its own DOM inside `main-panel-ai`.
//
// Tenant isolation: the ONLY read is the tenant-scoped DTO from
// GET /platform/ai-config/assistant-model-choice (the API derives the tenant
// from the authenticated principal; the client never sends a tenant). No
// platform-wide list is ever read, so a tenant member can never see another
// tenant's stacks.
//
// Fail closed: an errored read, an empty options list (nothing deployed) or a
// malformed token leaves the pick UNWRITTEN and shows the same sentence the
// reader ships, so the assistant keeps its no-model answer instead of dialling
// a dead alias. Tenant-admin only (same predicate the backend enforces); a
// tenant member renders the hint and issues zero writes. No edition branch:
// the picker renders identically on every edition — the boundary is the
// tenant-admin gate, server-side and client-side.

const AMC_ENDPOINT = '/platform/ai-config/assistant-model-choice';
const AMC_HINT = 'Pick one of your stacks here, or fill in the Provider, Model and Base URL on the AI Assistant tab and save.';

let _amcOptions = [];
let _amcWired = false;
let _amcLoaded = false;

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

// Same predicate the backend enforces (`_is_tenant_admin`). Fail closed: any
// error hides the write path rather than guessing.
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

// The token grammar the key itself declares: `stack:<id>` / `external:<url>`,
// optionally `|<model>`. Anything else is malformed and is never offered.
function _amcTokenOk(token) {
  const t = String(token == null ? '' : token);
  if (t === '') return false;
  const at = t.indexOf(':');
  if (at <= 0) return false;
  const kind = t.slice(0, at).trim().toLowerCase();
  if (kind !== 'stack' && kind !== 'external') return false;
  let rest = t.slice(at + 1);
  const bar = rest.indexOf('|');
  if (bar >= 0) rest = rest.slice(0, bar);
  rest = rest.trim();
  if (!rest) return false;
  if (kind === 'external' && !/^https?:\/\//i.test(rest)) return false;
  return true;
}

// The server's own `detail`, verbatim — never a client-invented sentence.
async function _amcSurface(resp) {
  if (!resp || resp.ok) return '';
  try {
    const p = await resp.json();
    return String(p && p.detail != null ? p.detail : '');
  } catch {
    return '';
  }
}

// `isError` puts the sentence on the error line and leaves the shipped hint
// alone; otherwise the hint is the configure sentence the reader also shows.
function _setHint(msg, isError) {
  const h = document.getElementById('amc-hint');
  if (!isError && h) h.textContent = msg || AMC_HINT;
  const e = document.getElementById('amc-error');
  if (e) e.textContent = isError ? String(msg || '') : '';
}

function _mountPicker(panel) {
  const box = document.createElement('div');
  box.dataset.amcPicker = '1';
  panel.appendChild(box);
  const sel = document.createElement('select');
  sel.id = 'amc-choice';
  box.appendChild(sel);
  const hintEl = document.createElement('p');
  hintEl.id = 'amc-hint';
  box.appendChild(hintEl);
  const errEl = document.createElement('p');
  errEl.id = 'amc-error';
  box.appendChild(errEl);
  return sel;
}

// Rows come from the DTO only; a malformed token is dropped, never offered.
function _paint(options) {
  const list = (Array.isArray(options) ? options : []).filter((o) => o && _amcTokenOk(o.token));
  _amcOptions = list.slice(0, 40);
  const parts = ['<option value="">Choose a model</option>'];
  _amcOptions.forEach((o) => parts.push(`<option value="${esc(o.token)}">${esc(o.label || o.token)}</option>`));
  const sel = document.getElementById('amc-choice');
  if (sel) sel.innerHTML = parts.join('');
}

function _wireSelect() {
  if (_amcWired) return;
  const sel = document.getElementById('amc-choice');
  if (!sel) return;
  _amcWired = true;
  sel.addEventListener('change', () => onPickChange(sel));
}

async function _loadChoice() {
  try {
    const resp = await fetch(`${getApiUrl()}${AMC_ENDPOINT}`, { headers: authHeader() });
    const detail = await _amcSurface(resp);
    if (detail) { _setHint(detail, true); return null; }
    const dto = await resp.json();
    if (!dto) { _setHint(AMC_HINT, true); return null; }
    return dto;
  } catch {
    _setHint(AMC_HINT, true);
    return null;
  }
}

async function _putChoice(token) {
  try {
    const resp = await fetch(`${getApiUrl()}${AMC_ENDPOINT}`, {
      method: 'PUT',
      headers: authHeader(),
      body: JSON.stringify({ token: String(token || '') }),
    });
    const detail = await _amcSurface(resp);
    if (detail) { _setHint(detail, true); return false; }
    const dto = await resp.json();
    _paint(dto && dto.options ? dto.options : []);
    _setHint(AMC_HINT, false);
    return true;
  } catch {
    _setHint(AMC_HINT, true);
    return false;
  }
}

// A pick is only honoured when it names something this tenant was offered;
// anything else is refused client-side (no request, no write).
async function onPickChange(sel) {
  if (!_amcLoaded) return;
  const token = String(sel && sel.value || '');
  if (token !== '' && !_amcOptions.some((o) => o.token === token)) { _setHint(AMC_HINT, true); return; }
  await _putChoice(token);
}

export async function initAssistantModelPicker() {
  const panel = document.getElementById('main-panel-ai');
  if (!panel || panel.dataset.amcBound === '1') return;
  panel.dataset.amcBound = '1';
  const sel = _mountPicker(panel);
  if (!sel) return;
  _setHint(AMC_HINT, false);
  const admin = await isTenantAdmin();
  if (!admin) return;
  const dto = await _loadChoice();
  if (!dto) return;
  _paint(dto.options || []);
  _wireSelect();
  _amcLoaded = true;
}

function _bootstrap() {
  const tryInit = () => {
    if (document.getElementById('main-panel-ai')) {
      initAssistantModelPicker().catch((e) => console.error('assistant-model-picker init failed:', e));
    }
  };
  tryInit();
  const target = document.getElementById('page-content') || document.body;
  if (target && !target.dataset.amcObserver) {
    target.dataset.amcObserver = '1';
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
