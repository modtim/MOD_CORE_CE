/**
 * Persistent Stacks page (add-persistent-stacks §8).
 *
 * SPA module — create / deploy / back up / restore / migrate / tear down
 * persistent docker-compose stacks. Tenant-scoped via the bearer token that
 * fetchApi attaches; the §8.2 rebalancer threshold dial is platform-admin and
 * is revealed only when that endpoint is reachable.
 */
import { fetchApi as apiCall, me, getPlatformFeatures, isPaidEdition } from './api.js';
// GX.S10c — stack variables & secrets: the live panel + the stack-detail
// secrets tab live in js/stack-variables.js; this file only mounts them and
// gates the create/save paths on their completeness.
import { mountVariablesPanel, mountSecretsTab, svMissingNames } from './stack-variables.js';
// cUI-5 — the three seed wrappers + the bare presigned PUT (api.js, same block).
import { registerStackSeed, getStackSeedPartUrls, commitStackSeed, putSeedPart } from './api.js';

function _psEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Stack-row identity LINE — facts only, from the row the server already
// already scoped server-side (schemas.py:4166-4167 identity_app_id / identity_minted_at,
// copied server-side from the stack's mint record at _shared.py:2191-2193). No
// client-side staleness claim here (D2): the browser cannot see the server's
// reasons (stack_identity.py:657-686), so an amber badge on this row would
// lie. The ISO mint string is passed through _psEsc verbatim — never
// reformatted, never new Date() (a bad value must not throw).
function _psIdentityLine(s) {
    if (!s || !('identity_app_id' in s)) return 'identity: unknown';
    if (s.identity_app_id === null) return 'identity: stack-only';
    if (!Number.isInteger(s.identity_app_id)) return 'identity: unknown';
    const at = s.identity_minted_at;
    const atPart = (typeof at === 'string' && at) ? ` · minted ${_psEsc(at)}` : '';
    return `identity: App #${_psEsc(String(s.identity_app_id))}${atPart}`;
}

function _psMsg(text, ok) {
    const el = document.getElementById('ps-msg');
    if (!el) return;
    el.textContent = text || '';
    el.className = 'text-sm mb-4 ' + (ok ? 'text-success' : 'text-destructive');
    el.classList.toggle('hidden', !text);
}

// ============================================================================
// 13.5d — tenant-secrets helpers (pure — extracted by tests/ps-secrets.check.js)
//
// A stack's env/file references to TENANT secrets use the 13.5a wire forms:
//   - env:  the whole environment value is `${secret:NAME}` (the server
//     normalizes it into x-mod.secrets {ENV: {secret: NAME}} at save time);
//   - file: top-level `secrets: {NAME: {file: "/run/secrets/NAME"}}` plus the
//     service's `secrets: [NAME]` list (or [{source: NAME, target: PATH}]),
//     normalized into x-mod.secrets {NAME: {secret: NAME, type: "file"}}.
// The UI writes those literal forms into the compose it already holds (the
// normalized preview spec / the redacted definition spec) and lets the API
// normalize — there is no YAML parser in the Dashboard. The tenant secret
// NAME is always rendered; a secret VALUE is never rendered, logged, or
// persisted by this file.
// ============================================================================

// Matches a WHOLE-VALUE tenant secret reference `${secret:NAME}` (NAME follows
// the server's _SECRET_REF_RE: [A-Za-z0-9] then [A-Za-z0-9_.-]). Partial
// interpolation (PRE${secret:X}) is deliberately NOT a reference — the server
// refuses those, so the UI must not offer them either.
const PS_SECRET_REF_RE = /^\$\{secret:([A-Za-z0-9][A-Za-z0-9_.\-]*)\}$/;

function psParseSecretRef(v) {
    if (typeof v !== 'string') return null;
    const m = PS_SECRET_REF_RE.exec(v.trim());
    return m ? m[1] : null;
}

function psSecretName(ref) {
    if (ref == null) return '';
    if (typeof ref === 'string') return ref;
    const n = ref.secret;
    return (typeof n === 'string' ? n : String(n == null ? '' : n)).trim();
}

function psIsFileRef(ref) {
    return !!(ref && typeof ref === 'object' && ref.type === 'file');
}

function psFileTarget(ref) {
    const t = ref && typeof ref === 'object' ? ref.target : null;
    return typeof t === 'string' && t.trim() !== '' ? t : null;
}

function psDefaultFileTarget(name) {
    return `/run/secrets/${String(name || '').toLowerCase()}`;
}

function psValidSecretName(name) {
    return /^[A-Za-z0-9][A-Za-z0-9_.\-]*$/.test(String(name || '').trim());
}

// Per-ref UI status. `names` = tenant secret names the caller can see (from
// GET /secrets); `known` = true when that list actually loaded (an API failure
// must read as "unknown", not as "missing").
function psSecretStatus(ref, names, known) {
    const set = new Set(Array.isArray(names) ? names.map(String) : []);
    const named = psSecretName(ref);
    if (named !== '') return set.has(named) ? 'resolved' : (known ? 'missing' : 'unknown');
    return 'plain';
}

// Applies the UI's collected secret edits to a (deep-copied) compose spec.
// `changes` = {env: [{service, env, kind: 'plain'|'secret'|'new', value?, name?}],
//              files: [{service, name, target}]}. Plain values are the literal
// the user typed; 'secret'/'new' write the `${secret:NAME}` whole-value form.
// File rows are declared in the compose-native form (top-level `secrets` + the
// service's `secrets` list); the API's 13.5a normalization converts both forms
// to the stored x-mod.secrets shape, so no x-mod surgery happens client-side.
// Mutates and returns `spec`.
function psApplySecretChanges(spec, changes) {
    if (!spec || typeof spec !== 'object') return spec;
    let services = spec.services;
    if (!services || typeof services !== 'object') services = spec.services = {};
    for (const c of (changes && changes.env) || []) {
        if (!c || !c.service || !c.env) continue;
        const svc = services[c.service];
        if (!svc || typeof svc !== 'object') continue;
        // Re-binding a key normalization already MOVED into x-mod.secrets must
        // drop the stale named ref: normalize_tenant_secret_refs setdefaults, so
        // a surviving old entry would silently win over the new `${secret:…}`.
        const xm = (svc['x-mod'] && typeof svc['x-mod'] === 'object') ? svc['x-mod'] : null;
        const sb = xm && xm.secrets && typeof xm.secrets === 'object' ? xm.secrets : null;
        if (sb && !psIsFileRef(sb[c.env])) delete sb[c.env];
        if (svc.environment && Array.isArray(svc.environment)) {
            svc.environment = svc.environment.filter(
                (e) => !(typeof e === 'string' && e.split('=', 1)[0].trim() === c.env));
        }
        if (!svc.environment || typeof svc.environment !== 'object') svc.environment = {};
        if (c.kind === 'plain') svc.environment[c.env] = c.value == null ? '' : String(c.value);
        // String concatenation (NOT a template literal): a literal `${secret:…}`
        // written inside backticks is a parse error (the substitution would be
        // the non-expression `secret:`).
        else if (c.kind === 'secret' || c.kind === 'new') svc.environment[c.env] = '${secret:' + c.name + '}';
    }
    for (const f of (changes && changes.files) || []) {
        if (!f || !f.service || !f.name) continue;
        const target = f.target && String(f.target).trim() ? String(f.target).trim()
            : psDefaultFileTarget(f.name);
        if (!spec.secrets || typeof spec.secrets !== 'object') spec.secrets = {};
        if (!spec.secrets[f.name]) spec.secrets[f.name] = { file: target };
        const svc = services[f.service];
        if (!svc || typeof svc !== 'object') continue;
        // A file row the operator re-targets already lives in x-mod.secrets
        // (normalization setdefaults), so the compose-native re-declaration
        // below would be ignored — drop the stale entry so the new target
        // re-normalizes from the declaration we are about to push.
        if (svc['x-mod'] && typeof svc['x-mod'] === 'object' && svc['x-mod'].secrets
            && typeof svc['x-mod'].secrets === 'object' && psIsFileRef(svc['x-mod'].secrets[f.name])) {
            delete svc['x-mod'].secrets[f.name];
        }
        const list = Array.isArray(svc.secrets) ? svc.secrets : (svc.secrets = []);
        const has = list.some((e) => e === f.name || (e && typeof e === 'object'
            && (e.source === f.name || e.secret === f.name || e.name === f.name)));
        if (!has) list.push({ source: f.name, target });
    }
    return spec;
}

// Builds the data the Secrets section renders: one entry per service with its
// env keys (each carrying its current binding: a plain value, a named tenant
// secret, or the redaction sentinel) plus its file-type secrets. PURE — reads
// only `spec` (which is already redacted server-side: secret-looking env values
// arrive as ***redacted***, so no value can leak here) and the caller's visible
// tenant secret `names`. Extracted by tests/ps-secrets.check.js.
function psSecretsView(spec, names, known) {
    const set = new Set(Array.isArray(names) ? names.map(String) : []);
    const services = {};
    const statusOf = (ref) => psSecretStatus(ref, names, known);
    const envMapOf = (svc) => {
        const out = {};
        const env = svc.environment;
        if (env && typeof env === 'object' && !Array.isArray(env)) {
            for (const k of Object.keys(env)) out[k] = env[k];
        } else if (Array.isArray(env)) {
            for (const e of env) {
                // `split('=', 1)` limits to ONE element, so [1] is undefined —
                // cut at the first '=' instead (the value may itself contain '=').
                if (typeof e === 'string' && e.includes('=')) {
                    const i = e.indexOf('=');
                    out[e.slice(0, i)] = e.slice(i + 1);
                }
            }
        }
        return out;
    };
    for (const svcName of Object.keys((spec && spec.services) || {})) {
        const svc = spec.services[svcName];
        if (!svc || typeof svc !== 'object') continue;
        const xmod = (svc['x-mod'] && typeof svc['x-mod'] === 'object') ? svc['x-mod'] : {};
        const xsecrets = (xmod.secrets && typeof xmod.secrets === 'object') ? xmod.secrets : {};
        const envs = [];
        const envMap = envMapOf(svc);
        // The env keys the section must list: the literal environment keys PLUS
        // every non-file x-mod.secrets entry — normalization MOVES a bound key
        // out of `environment` into x-mod.secrets[ENV_KEY] = {secret: NAME}, so
        // without that union an already-bound row would vanish from the panel
        // and could never be re-bound or reported resolved/missing.
        const envKeys = Object.keys(envMap);
        for (const k of Object.keys(xsecrets)) {
            if (!psIsFileRef(xsecrets[k]) && !envKeys.includes(k)) envKeys.push(k);
        }
        for (const key of envKeys) {
            const raw = envMap[key];
            const asStr = (typeof raw === 'string') ? raw : null;
            let refName = asStr ? psParseSecretRef(asStr) : null;
            let isFile = false;
            const xref = xsecrets[key];
            // A named env ref may live in x-mod.secrets (value already removed
            // by normalization) rather than as a literal ${secret:...}.
            if (!refName && xref && !psIsFileRef(xref) && psSecretName(xref)) {
                refName = psSecretName(xref);
            } else if (xref && psIsFileRef(xref)) {
                isFile = true; // this env key is really a file secret's binding
            }
            const redacted = asStr === '***redacted***';
            const hasRef = !!refName;
            envs.push({
                key,
                // Never echo a real value: secret-bound keys carry their NAME,
                // redacted keys carry the sentinel (rendered as empty), plain
                // keys carry their (non-secret) value.
                value: (hasRef || redacted) ? '' : (asStr == null ? '' : String(asStr)),
                redacted,
                refName: hasRef ? refName : null,
                status: hasRef ? statusOf({ secret: refName }) : 'plain',
            });
        }
        const files = [];
        for (const fname of Object.keys(xsecrets)) {
            const fref = xsecrets[fname];
            if (!psIsFileRef(fref)) continue;
            files.push({ name: fname, target: psFileTarget(fref) || '', status: statusOf(fref) });
        }
        services[svcName] = { envs, files };
    }
    return { services };
}

let _psTemplates = [];

// ============================================================================
// 13.5d — Secrets section (create/edit)
//
// Rendered into a host div inside the compose box (create) and the edit
// definition panel (edit). Per service it lists every environment key with a
// binding (plain value / from an existing tenant secret / a NEW tenant secret
// the operator types in a masked box) plus a file-secret row. The tenant
// secret list (names only — GET /secrets never returns values) drives the
// dropdowns and the resolved/missing status. A secret VALUE is only ever held
// transiently in a masked input and is POSTed to /secrets/ on save; it is never
// rendered, logged, or kept after submit.
//
// Pending edits live in _psSecretSecs[sectionKey] (module state) so a re-render
// (SPA remount / re-fetch) can restore the operator's in-flight choices.
// ============================================================================
const _psSecretSecs = {};
let _psTenantSecretNames = [];
let _psTenantSecretsKnown = false;

async function _psLoadTenantSecrets() {
    try {
        const rows = await apiCall('/secrets/');
        const list = Array.isArray(rows) ? rows : [];
        _psTenantSecretNames = list.map((s) => s && s.name).filter(Boolean);
        _psTenantSecretsKnown = true;
    } catch (_) {
        _psTenantSecretNames = [];
        _psTenantSecretsKnown = false; // an API failure reads as "unknown", not "missing"
    }
    return { names: _psTenantSecretNames, known: _psTenantSecretsKnown };
}

// A fresh (or restored) section record for `key`.
function _psSecretSec(key) {
    if (!_psSecretSecs[key]) {
        _psSecretSecs[key] = { env: {}, files: [], newSecrets: {} };
    }
    return _psSecretSecs[key];
}

// Per-service env-mode default: if the key already carries a ${secret:NAME} or
// a named x-mod ref, start in 'secret'; a redacted (***redacted***) value is
// 'plain' (the operator re-binds it explicitly — we never echo the value).
function _psEnvRowDefaults(viewSvc, key) {
    const env = viewSvc.envs.find((e) => e.key === key);
    if (!env) return { mode: 'plain', value: '', name: '' };
    if (env.refName) return { mode: 'secret', value: '', name: env.refName };
    if (env.redacted) return { mode: 'plain', value: '', name: '' };
    return { mode: 'plain', value: env.value, name: '' };
}

function _psStatusBadge(status) {
    if (status === 'resolved')
        return '<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700" data-testid="ps-sec-status" data-status="resolved">resolved</span>';
    if (status === 'missing')
        return '<span class="px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700" data-testid="ps-sec-status" data-status="missing">missing</span>';
    if (status === 'unknown')
        return '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600" data-testid="ps-sec-status" data-status="unknown">unknown</span>';
    return '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500" data-testid="ps-sec-status" data-status="plain">plain</span>';
}

function _psSecretOptions(selectedName) {
    // A row already bound to a secret the tenant list doesn't have (deleted /
    // not yet visible) must still SHOW that name — otherwise the browser snaps
    // the select to (none) and the row reads "missing" with nothing selected.
    const names = _psTenantSecretNames.slice();
    if (selectedName && names.indexOf(selectedName) < 0) names.push(selectedName);
    let opts = '<option value="">(none)</option>';
    for (const n of names) {
        opts += `<option value="${_psEsc(n)}" ${n === selectedName ? 'selected' : ''}>${_psEsc(n)}</option>`;
    }
    return opts;
}

// Build the section's HTML string for `key` given a (redacted) compose spec.
// `names`/`known` default to the module tenant-secret cache.
function _psSecretsSectionHtml(key, spec, names, known) {
    const nm = (names != null) ? names : _psTenantSecretNames;
    const kn = (known != null) ? known : _psTenantSecretsKnown;
    const view = psSecretsView(spec, nm, kn);
    const sec = _psSecretSec(key);
    const svcNames = Object.keys(view.services);
    if (!svcNames.length) {
        return '<div class="text-xs text-gray-500">No services parsed yet — Preview the compose first.</div>';
    }
    let html = '<div class="border-t border-gray-200 pt-3 mt-3" data-ps-sec-root data-sec-key="' + _psEsc(key) + '">';
    html += '<div class="flex items-center justify-between mb-1"><span class="text-sm font-semibold text-gray-800">Secrets</span>'
        + '<span class="text-xs text-gray-500">Bind env values to tenant secrets. Values are never shown again once saved.</span></div>';
    for (const svc of svcNames) {
        const vs = view.services[svc];
        html += `<div class="mt-3" data-ps-sec-svc="${_psEsc(svc)}">`;
        html += `<div class="text-xs font-semibold text-gray-700 mb-1">${_psEsc(svc)}</div>`;
        // env rows
        html += '<table class="w-full text-xs border border-gray-200">';
        html += '<thead><tr class="text-left text-gray-500 bg-gray-50"><th class="px-2 py-1">Env</th><th class="px-2 py-1">Binding</th><th class="px-2 py-1"></th><th class="px-2 py-1">Status</th></tr></thead><tbody>';
        for (const e of vs.envs) {
            const d = _psEnvRowDefaults(vs, e.key);
            // Restore any in-flight operator choice for this row.
            const rk = svc + '\u0000' + e.key;
            if (sec.env[rk]) { d.mode = sec.env[rk].mode; d.value = sec.env[rk].value || ''; d.name = sec.env[rk].name || ''; }
            // Status reflects the CURRENTLY selected binding.
            let status = 'plain';
            if (d.mode === 'secret' && d.name) status = nm.indexOf(d.name) >= 0 ? 'resolved' : (kn ? 'missing' : 'unknown');
            else if (d.mode === 'new') status = nm.indexOf(d.name) >= 0 ? 'resolved' : (d.name ? (kn ? 'missing' : 'unknown') : 'plain');
            const modeOpts = ['plain', 'secret', 'new'].map((m) =>
                `<option value="${m}" ${m === d.mode ? 'selected' : ''}>${m === 'plain' ? 'plain value' : (m === 'secret' ? 'from secret' : 'new secret…')}</option>`).join('');
            let widget = '';
            if (d.mode === 'plain') {
                widget = `<input type="text" data-ps-sec-plain value="${_psEsc(d.value)}" aria-label="${_psEsc(e.key)} value" class="px-2 py-1 text-xs border border-gray-300 rounded w-52 bg-white" placeholder="value" />`;
            } else if (d.mode === 'secret') {
                widget = `<select data-ps-sec-name aria-label="${_psEsc(e.key)} secret" class="px-2 py-1 text-xs border border-gray-300 rounded w-52 bg-white">${_psSecretOptions(d.name)}</select>`;
            } else { // new
                widget = `<div class="flex items-center gap-1">
                    <input type="text" data-ps-sec-newname value="${_psEsc(d.name)}" aria-label="${_psEsc(e.key)} new secret name" class="px-2 py-1 text-xs border border-gray-300 rounded w-32 bg-white" placeholder="NAME" />
                    <input type="password" autocomplete="off" data-ps-sec-newvalue aria-label="${_psEsc(e.key)} new secret value" class="px-2 py-1 text-xs border border-gray-300 rounded w-40 bg-white" placeholder="value (masked)" />
                </div>`;
            }
            html += `<tr data-ps-sec-env-row data-svc="${_psEsc(svc)}" data-key="${_psEsc(e.key)}">
                <td class="px-2 py-1 font-mono whitespace-nowrap">${_psEsc(e.key)}</td>
                <td class="px-2 py-1"><select data-ps-sec-mode aria-label="${_psEsc(e.key)} secret mode" class="px-1 py-1 text-xs border border-gray-300 rounded bg-white">${modeOpts}</select></td>
                <td class="px-2 py-1">${widget}</td>
                <td class="px-2 py-1" data-ps-sec-status-cell>${_psStatusBadge(status)}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        // file-secret rows (existing x-mod file refs + the operator's additions)
        // Operator edits (by name) first so a re-targeted existing row shows the
        // new path, but the EXISTING row wins the de-dupe — it is the one that
        // must keep the no-remove-button flag and the x-mod backing truth.
        const editsByName = {};
        for (const f of sec.files.filter((f) => f.svc === svc)) if (f.name) editsByName[f.name] = f;
        const uniqFiles = vs.files.map((f) => ({
            svc, name: f.name,
            target: (editsByName[f.name] && editsByName[f.name].target) || f.target,
            _existing: true,
        }));
        for (const f of Object.keys(editsByName).map((k) => editsByName[k])) {
            if (!vs.files.some((v) => v.name === f.name)) {
                uniqFiles.push({ svc, name: f.name, target: f.target });
            }
        }
        html += `<div class="mt-1 flex flex-wrap items-center gap-2" data-ps-sec-files data-svc="${_psEsc(svc)}">`;
        html += '<span class="text-xs text-gray-600">file secrets:</span>';
        for (const f of uniqFiles) {
            const fstat = f._existing ? (nm.indexOf(f.name) >= 0 ? 'resolved' : (kn ? 'missing' : 'unknown')) : (f.name ? (_psTenantSecretNames.indexOf(f.name) >= 0 ? 'resolved' : (kn ? 'missing' : 'unknown')) : 'plain');
            html += `<span class="inline-flex items-center gap-1 border border-gray-200 rounded px-1.5 py-1 bg-gray-50" data-ps-sec-file-row data-name="${_psEsc(f.name)}">
                <span class="font-mono text-xs">${_psEsc(f.name)}</span>
                <input type="text" data-ps-sec-filetarget value="${_psEsc(f.target || '')}" aria-label="${_psEsc(f.name)} target path" class="px-1.5 py-0.5 text-xs border border-gray-300 rounded bg-white" placeholder="/run/secrets/…" title="target path in the container" />
                ${_psStatusBadge(fstat)}
                ${f._existing ? '' : '<button type="button" data-ps-sec-file-remove class="text-xs text-gray-500 hover:text-red-600 px-1" title="remove" aria-label="Remove">×</button>'}
            </span>`;
        }
        html += `<span class="inline-flex items-center gap-1">
            <select data-ps-sec-file-add aria-label="Add a file secret" class="px-1.5 py-0.5 text-xs border border-gray-300 rounded bg-white"><option value="">+ add file secret…</option>${_psSecretOptions('')}</select>
            <input type="text" data-ps-sec-file-addtarget aria-label="New file secret target path" class="px-1.5 py-0.5 text-xs border border-gray-300 rounded bg-white" placeholder="/run/secrets/…" />
        </span>`;
        html += '</div></div>';
    }
    html += '</div>';
    return html;
}

// Mount the section into `host` for `key`, loading tenant secrets first.
async function _psRenderSecretsSection(key, host, spec) {
    if (!host) return;
    await _psLoadTenantSecrets();
    host.innerHTML = _psSecretsSectionHtml(key, spec);
    _psWireSecretsSection(key, host, spec);
}

// Re-render in place (after a mode change / add) without reloading secrets.
function _psRerenderSecretsSection(key, host, spec) {
    if (!host) return;
    host.innerHTML = _psSecretsSectionHtml(key, spec);
    _psWireSecretsSection(key, host, spec);
}

function _psWireSecretsSection(key, host, spec) {
    const root = host.querySelector('[data-ps-sec-root]');
    if (!root) return;
    const svcNames = Object.keys(psSecretsView(spec, _psTenantSecretNames, _psTenantSecretsKnown).services);
    // mode / name / add changes → re-render that row's status live (cheap).
    root.addEventListener('change', (e) => {
        const t = e.target;
        const rk = t.closest('[data-ps-sec-env-row]');
        if (t.dataset.psSecMode !== undefined && rk) {
            const svc = rk.dataset.svc, k = rk.dataset.key;
            const sec = _psSecretSec(key);
            const prev = sec.env[svc + '\u0000' + k] || {};
            const mode = t.value;
            sec.env[svc + '\u0000' + k] = { mode, value: prev.value || '', name: prev.name || '' };
            _psRerenderSecretsSection(key, host, spec);
            return;
        }
        if (t.dataset.psSecName !== undefined && rk) {
            const svc = rk.dataset.svc, k = rk.dataset.key;
            const sec = _psSecretSec(key);
            const prev = sec.env[svc + '\u0000' + k] || { mode: 'secret', value: '' };
            sec.env[svc + '\u0000' + k] = { mode: 'secret', value: '', name: t.value };
            _psRerenderSecretsSection(key, host, spec);
            return;
        }
        if (t.dataset.psSecFileAdd != null) {
            const svcRow = t.closest('[data-ps-sec-files]');
            const svc = svcRow ? svcRow.dataset.svc : (svcNames[0] || '');
            const name = t.value;
            if (name && svc) {
                const sec = _psSecretSec(key);
                if (!sec.files.some((f) => f.name === name)) {
                    const tgt = (svcRow.querySelector('[data-ps-sec-file-addtarget]') || {}).value || '';
                    sec.files.push({ svc, name, target: tgt.trim() });
                    _psRerenderSecretsSection(key, host, spec);
                }
            }
        }
    });
    root.addEventListener('input', (e) => {
        const t = e.target;
        const rk = t.closest('[data-ps-sec-env-row]');
        if (t.dataset.psSecPlain !== undefined && rk) {
            const svc = rk.dataset.svc, k = rk.dataset.key;
            const sec = _psSecretSec(key);
            const prev = sec.env[svc + '\u0000' + k] || { mode: 'plain', name: '' };
            sec.env[svc + '\u0000' + k] = { mode: 'plain', value: t.value, name: prev.name || '' };
        } else if (t.dataset.psSecNewname !== undefined && rk) {
            const svc = rk.dataset.svc, k = rk.dataset.key;
            const sec = _psSecretSec(key);
            const prev = sec.env[svc + '\u0000' + k] || { mode: 'new', value: '' };
            sec.env[svc + '\u0000' + k] = { mode: 'new', value: prev.value || '', name: t.value };
            const cell = rk.querySelector('[data-ps-sec-status-cell]');
            if (cell) cell.innerHTML = _psStatusBadge(_psTenantSecretNames.indexOf(t.value) >= 0 ? 'resolved' : (t.value ? (kn => kn ? 'missing' : 'unknown')(_psTenantSecretsKnown) : 'plain'));
        } else if (t.dataset.psSecNewvalue !== undefined && rk) {
            const svc = rk.dataset.svc, k = rk.dataset.key;
            const sec = _psSecretSec(key);
            const prev = sec.env[svc + '\u0000' + k] || { mode: 'new', name: '' };
            sec.env[svc + '\u0000' + k] = { mode: 'new', value: t.value, name: prev.name || '' };
        } else if (t.dataset.psSecFiletarget !== undefined) {
            const row = t.closest('[data-ps-sec-file-row]');
            const name = row ? row.dataset.name : '';
            if (!name) return;
            const svcRow = t.closest('[data-ps-sec-files]');
            const svc = svcRow ? svcRow.dataset.svc : '';
            const sec = _psSecretSec(key);
            const existing = sec.files.find((f) => f.name === name);
            // An EXISTING x-mod file row isn't in sec.files at all — without
            // recording the edit here, re-targeting it is silently dropped on
            // save (the row then keeps its old path while the UI shows the new
            // one).
            if (existing) existing.target = t.value;
            else if (svc) sec.files.push({ svc, name, target: t.value });
        }
    });
    root.addEventListener('click', (e) => {
        const t = e.target;
        if (t.dataset.psSecFileRemove !== undefined) {
            const row = t.closest('[data-ps-sec-file-row]');
            const name = row ? row.dataset.name : '';
            const sec = _psSecretSec(key);
            sec.files = sec.files.filter((f) => f.name !== name);
            _psRerenderSecretsSection(key, host, spec);
        }
    });
}

// Gather the section's edits into the {env:[], files:[]} shape for
// psApplySecretChanges. Returns {changes, newSecrets:[{name,value}], errors:[]}.
// `newSecrets` are NOT created here — the save path POSTs /secrets/ for each
// and clears the masked value afterward.
function _psCollectSecretsEdits(key, host, spec) {
    const sec = _psSecretSec(key) || { env: {}, files: [] };
    const view = psSecretsView(spec, _psTenantSecretNames, _psTenantSecretsKnown);
    const envChanges = [];
    const newSecrets = [];
    const errors = [];
    for (const svc of Object.keys(view.services)) {
        for (const e of view.services[svc].envs) {
            const rk = svc + '\u0000' + e.key;
            const d = sec.env[rk] || _psEnvRowDefaults(view.services[svc], e.key);
            // Skip rows the operator never touched (matches the default binding
            // → no change to record; keeps untouched secret refs intact).
            if (sec.env[rk] == null) continue;
            if (d.mode === 'plain') {
                envChanges.push({ service: svc, env: e.key, kind: 'plain', value: d.value });
            } else if (d.mode === 'secret') {
                if (!d.name) { errors.push(`${svc}.${e.key}: pick a secret`); continue; }
                envChanges.push({ service: svc, env: e.key, kind: 'secret', name: d.name });
            } else if (d.mode === 'new') {
                if (!psValidSecretName(d.name)) { errors.push(`${svc}.${e.key}: new-secret name is invalid`); continue; }
                if (d.value === '' || d.value == null) { errors.push(`${svc}.${e.key}: enter a value for new secret "${d.name}"`); continue; }
                envChanges.push({ service: svc, env: e.key, kind: 'new', name: d.name, value: d.value });
                newSecrets.push({ name: d.name, value: d.value });
            }
        }
    }
    const fileChanges = [];
    for (const f of (sec.files || [])) {
        if (!f.name) continue;
        if (!psValidSecretName(f.name)) { errors.push(`file secret name "${f.name}" is invalid`); continue; }
        fileChanges.push({ service: f.svc, name: f.name, target: f.target });
        // A file secret the operator added must exist as a tenant secret — if it
        // was typed as a NEW name we'd need its value; file adds reuse existing
        // secrets (the add dropdown only lists them), so no newSecret here.
    }
    return { changes: { env: envChanges, files: fileChanges }, newSecrets, errors };
}

// Clear the masked values + in-flight edits for a section after a successful
// submit (a secret value must not linger in the DOM or module state).
function _psClearSecretSec(key) {
    _psSecretSecs[key] = { env: {}, files: [], newSecrets: {} };
}

// Last-loaded stacks keyed by id, so the expose panel (opened from a button in
// the row) can read the stack's current exposure/mesh state without threading it
// all through data-attributes. Refreshed on every _psLoadStacks and after an
// exposure toggle.
let _psById = {};

// delegated-stack-authoring §6.3 — a stack's declared reach, shown wherever its
// accelerator demand already is (the Cost/hr cell), since "what does this cost"
// and "what can this reach" are the same kind of question about a stack an
// admin is scanning the list for. `egress_policy` is {service: {mode, hosts?}};
// summarised to the distinct modes present rather than every service, since the
// list is scanned at a glance, not read service-by-service here.
const _PS_EGRESS_MODE_LABEL = { none: 'no reach', api: 'API only', hosts: 'allow-listed hosts' };
const _PS_EGRESS_MODE_STYLE = {
    none: 'bg-gray-100 text-gray-600',
    api: 'bg-blue-100 text-blue-700',
    hosts: 'bg-amber-100 text-amber-700',
};
// Per-stack egress enforcement is a paid layer: on Community Edition (and while
// the edition is unknown — isPaidEdition is fail-closed) a stack runs with its
// default networking, so a "no reach" / "allow-listed hosts" badge would promise
// a restriction nothing enforces. Set by _psLoadStacks before the rows render.
let _psPaidEdition = false;

function _psEgressBadge(s) {
    if (!_psPaidEdition) return '';
    const policy = s.egress_policy;
    if (!policy || typeof policy !== 'object' || !Object.keys(policy).length) {
        return '<div class="mt-1"><span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500" title="No service on this stack declares x-mod.egress — undeclared reach">reach: undeclared</span></div>';
    }
    const modes = [...new Set(Object.values(policy).map(p => (p && p.mode) || 'none'))];
    const title = Object.entries(policy)
        .map(([svc, p]) => `${svc}: ${_PS_EGRESS_MODE_LABEL[p && p.mode] || (p && p.mode) || 'none'}${
            p && p.mode === 'hosts' && Array.isArray(p.hosts) ? ` (${p.hosts.join(', ')})` : ''}`)
        .join('\n');
    return '<div class="mt-1 flex flex-wrap gap-1">' + modes.map(m =>
        `<span class="px-2 py-0.5 rounded-full text-xs ${_PS_EGRESS_MODE_STYLE[m] || 'bg-gray-100 text-gray-600'}" title="${_psEsc(title)}">${_psEsc(_PS_EGRESS_MODE_LABEL[m] || m)}</span>`
    ).join('') + '</div>';
}

function _psHealthBadge(s) {
    const h = s.health;
    if (!h || typeof h !== 'object' || !h.grade) return '';
    const style = h.grade === 'unhealthy' ? 'bg-red-100 text-red-700'
        : h.grade === 'pending' ? 'bg-amber-100 text-amber-700'
        : h.grade === 'unavailable' ? 'bg-gray-100 text-gray-600'
        : 'bg-emerald-100 text-emerald-700';
    const title = h.reason ? `Health grade: ${h.grade} — ${h.reason}` : `Health grade: ${h.grade}`;
    return `<span class="px-2 py-0.5 rounded-full text-xs font-medium ${style}" data-testid="ps-health-badge" data-grade="${_psEsc(h.grade)}" title="${_psEsc(title)}">${_psEsc(h.grade)}</span>`;
}

// PS-STATUS-HEALTH-BEGIN — STACK-HEALTH-FINISH-1126 (F-2): a SEPARATE neutral
// badge for the worker-derived `status_health` (CORE/API/services/stack_observation.py
// streak; GET stack fields status_health + status_health_reason). The paid
// `_psHealthBadge` above (health.grade) is untouched — this is the operator
// signal "the loop watched this stack crash N beats in a row". Neutral wording,
// CE-safe: hidden when healthy/absent, rendered only for `degraded`, text
// `degraded: <reason>` (reason omitted when the record has none).
function _psStatusHealthBadge(s) {
    if (s.status_health !== 'degraded') return '';
    const reason = (typeof s.status_health_reason === 'string') ? s.status_health_reason.trim() : '';
    const label = reason ? `degraded: ${reason}` : 'degraded';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-800" data-testid="ps-status-health-badge" data-status-health="degraded" title="${_psEsc(`status_health=degraded${reason ? ` — ${reason}` : ''}`)}">${_psEsc(label)}</span>`;
}
// PS-STATUS-HEALTH-END

function _psCurrentTpl() {
    const id = document.getElementById('ps-tpl')?.value;
    return _psTemplates.find(t => t.id === id);
}

function _psRenderTplParams() {
    const t = _psCurrentTpl();
    const params = document.getElementById('ps-tpl-params');
    const desc = document.getElementById('ps-tpl-desc');
    if (!t) { if (params) params.innerHTML = ''; return; }
    if (desc) desc.textContent = t.description || '';
    params.innerHTML = (t.params || []).map(p => `
        <div>
            <label class="block text-xs font-medium text-gray-500 mb-1">${_psEsc(p.label || p.key)}</label>
            <input data-key="${_psEsc(p.key)}" aria-label="${_psEsc(p.label || p.key)}" value="${_psEsc(p.default || '')}" class="px-3 py-2 text-sm border border-gray-300 rounded-lg w-40" />
        </div>`).join('');
}

async function _psLoadTemplates() {
    try {
        _psTemplates = await apiCall('/stacks/templates');
        const sel = document.getElementById('ps-tpl');
        sel.innerHTML = _psTemplates.map(t => `<option value="${_psEsc(t.id)}">${_psEsc(t.name)}</option>`).join('');
        _psRenderTplParams();
    } catch (e) { _psMsg(`Templates: ${e.message || e}`, false); }
}

// --- GA.ASW.6 — "no backup plan" acknowledgement ----------------------------
// A create call (from-template or import-compose/finalize) that 409s with
// { requires_ack: true, reason, services } means the compose runs a known
// database image (postgres/mysql/mariadb/mongo — see
// services/stack_logical_backup.backup_ack_required) with no backup posture
// declared. Shown as this inline warning instead of a plain error; confirming
// re-POSTs the SAME endpoint/body with acknowledge_no_backup: true appended —
// one extra click, no re-typing.
let _psPendingBackupAck = null;  // { endpoint, body, onSuccess } | null

function _psBackupAckShow(detail, endpoint, body, onSuccess) {
    _psPendingBackupAck = { endpoint, body, onSuccess };
    const box = document.getElementById('ps-backup-ack');
    const text = document.getElementById('ps-backup-ack-text');
    const cb = document.getElementById('ps-backup-ack-checkbox');
    const confirmBtn = document.getElementById('ps-backup-ack-confirm');
    if (!box || !text) return;
    const services = Array.isArray(detail && detail.services) ? detail.services : [];
    const names = services.map(s => `${_psEsc(s.service)} (${_psEsc(s.image)})`).join(', ');
    text.textContent = names
        ? `${names} — ${(detail && detail.reason) || 'no backup posture declared.'}`
        : ((detail && detail.reason) || 'No backup posture declared for this stack.');
    if (cb) cb.checked = false;
    if (confirmBtn) confirmBtn.disabled = true;
    box.classList.remove('hidden');
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function _psBackupAckHide() {
    _psPendingBackupAck = null;
    document.getElementById('ps-backup-ack')?.classList.add('hidden');
}

async function _psBackupAckConfirmClick() {
    if (!_psPendingBackupAck) return;
    const { endpoint, body, onSuccess } = _psPendingBackupAck;
    try {
        const r = await apiCall(endpoint, {
            method: 'POST', body: JSON.stringify({ ...body, acknowledge_no_backup: true }),
        });
        _psBackupAckHide();
        onSuccess(r);
    } catch (e) {
        _psBackupAckHide();
        _psMsg(`Create failed: ${e.message || e}`, false);
    }
}

async function _psCreateFromTemplate() {
    const t = _psCurrentTpl();
    if (!t) return;
    const params = {};
    document.querySelectorAll('#ps-tpl-params input').forEach(i => { params[i.dataset.key] = i.value; });
    const name = (document.getElementById('ps-tpl-name').value || '').trim() || `${t.id}-stack`;
    const payload = { template_id: t.id, name, params };
    const onCreated = () => {
        _psMsg(`Created "${name}".`, true);
        document.getElementById('ps-tpl-name').value = '';
        _psLoadStacks();
    };
    try {
        await apiCall('/stacks/from-template', { method: 'POST', body: JSON.stringify(payload) });
        onCreated();
    } catch (e) {
        if (e && e.status === 409 && e.detail && e.detail.requires_ack) {
            return _psBackupAckShow(e.detail, '/stacks/from-template', payload, onCreated);
        }
        _psMsg(`Create failed: ${e.message || e}`, false);
    }
}

// --- Create from compose (paste your own) — stack-wizard-compose-import -----
// Preview → confirm → finalize against the existing /stacks/import-compose
// endpoints. The preview shows exactly what the import inferred (accelerator,
// egress, readiness) so the operator confirms what MOD will enforce rather
// than trusting a black box. The last preview's confirmations are carried
// into Create so a Preview→Create pair is one round of judgment.
let _psComposePreview = null;
// FIX-COMPOSE-PREVIEW (owner asked 11-17): Preview and Create both paint
// #ps-compose-result when their OWN response lands, so a slow Preview reply
// arriving after Create finished overwrote the "Created stack ..." line (the
// stack was still created — the message was lost). Every Preview click takes
// the next sequence token; a Preview response renders only while its token is
// still the latest and no Create has started. Create claims the sequence, so
// anything already in flight from Preview can no longer repaint the result.
let _psComposeSeq = 0;
let _psCreateStarted = false;
function _psComposeStale(seq) {
    return _psCreateStarted || seq !== _psComposeSeq;
}

function _psComposeResult(html, ok) {
    const el = document.getElementById('ps-compose-result');
    if (!el) return;
    el.innerHTML = html || '';
    el.classList.toggle('hidden', !html);
    el.className = 'mt-3 text-xs ' + (ok === false ? 'text-destructive' : 'text-gray-600');
}

function _psRenderComposePreview(p) {
    const needs = p.needs_confirmation || [];
    let html = `<div class="mb-2">Parsed <strong>${_psEsc(Object.keys((p.compose_spec || {}).services || {}).length)}</strong> service(s) · storage class <strong>${_psEsc(p.storage_class)}</strong></div>`;
    if (!needs.length) {
        html += '<div class="text-success">No x-mod confirmations needed — ready to create.</div>';
    } else {
        html += '<div class="font-medium text-gray-700 mb-1">Confirm before creating:</div><ul class="list-disc pl-4 space-y-0.5">';
        for (const c of needs) {
            const where = c.service ? `${_psEsc(c.service)}: ` : '';
            const opts = Array.isArray(c.options) && c.options.length
                ? ` <em>(one of: ${c.options.map(o => _psEsc(String(o))).join(', ')})</em>` : '';
            html += `<li><code>${where}${_psEsc(c.field)}</code> → <strong>${_psEsc(c.suggested !== null && typeof c.suggested === 'object' ? JSON.stringify(c.suggested) : String(c.suggested))}</strong>${opts} — ${_psEsc(c.reason || '')}</li>`;
        }
        html += '</ul>';
    }
    // gw-stack-networking GW.1/GW.4 — the import-preview notes for stripped
    // ports/subnets + the suggested front door, at a glance rather than buried
    // in the confirmations list (which still carries them for Create).
    const netNotes = needs.filter(n => n.field === 'network.ports' || n.field === 'network.subnet');
    const ingressNote = needs.find(n => n.field === 'ingress');
    if (netNotes.length || ingressNote) {
        html += '<div class="mt-2 border-t border-gray-200 pt-2 font-medium text-gray-700">Networking:</div><ul class="list-disc pl-4 space-y-0.5">';
        netNotes.forEach(n => { html += `<li>${_psEsc(n.reason)}</li>`; });
        if (ingressNote) {
            const ing = ingressNote.suggested || {};
            html += `<li>Suggested front door: <code>${_psEsc(ing.service || '?')}:${_psEsc(String(ing.port || '?'))}</code> — choose it in Expose… before exposing.</li>`;
        }
        html += '</ul>';
    }
    // PS-IMPORT-WARNINGS-BEGIN — FIX-DASH-STACK-PORT-WARNINGS (owner asked 11-18):
    // the import-compose Preview now returns `warnings` (strings; D76: a requested
    // FIXED host port is already in use on every licensed worker, and the message
    // names the holder). Show them as amber notes so the operator sees the port
    // problem before pressing Create. Advisory only — Create is NOT blocked, and
    // no warnings renders nothing.
    const _psWarnList = Array.isArray(p.warnings)
        ? p.warnings.filter(w => typeof w === 'string' && w.trim()) : [];
    if (_psWarnList.length) {
        html = `<div class="mt-2 text-xs bg-amber-50 border border-amber-200 rounded-md px-2 py-1" data-testid="ps-import-warnings">` +
            _psWarnList.map(w => `<div class="text-amber-800">${_psEsc(w)}</div>`).join('') +
            `</div>` + html;
    }
    // PS-IMPORT-WARNINGS-END
    _psComposeResult(html, true);
}

async function _psComposePreviewClick() {
    const yaml = document.getElementById('ps-compose-yaml')?.value || '';
    if (!yaml.trim()) return _psComposeResult('Paste a docker-compose YAML first.', false);
    _psComposePreview = null;
    _psCreateStarted = false;
    const seq = ++_psComposeSeq;
    try {
        const r = await apiCall('/stacks/import-compose', {
            method: 'POST', body: JSON.stringify({ name: _psComposeName(), compose: yaml }),
        });
        if (_psComposeStale(seq)) return;
        _psComposePreview = r;
        _psRenderComposePreview(r);
        // 13.5d — the Secrets section renders from the preview's normalized
        // (redacted) compose_spec; its section key is 'create'.
        // TODO 13.5c inline_secrets — pre-seed the section from r.inline_secrets
        _psMountCreateSecretsSection(r.compose_spec);
    } catch (e) { if (!_psComposeStale(seq)) _psComposeResult(`Import failed: ${_psEsc(e.message || e)}`, false); }
}

function _psComposeName() {
    return (document.getElementById('ps-compose-name')?.value || '').trim();
}

// GX.S10c — the live Variables & secrets panel for the Create-from-compose box
// (also serves Copy…, which just writes into the same textarea). Mounted once
// from the init wiring; the panel polls the textarea and debounces detect.
let _psComposeVarsCtrl = null;

function _psMountComposeVariables() {
    let host = document.getElementById('ps-compose-variables');
    if (!host) {
        const result = document.getElementById('ps-compose-result');
        if (!result) return;
        host = document.createElement('div');
        host.id = 'ps-compose-variables';
        host.className = 'border-t border-gray-200 pt-3 mt-4';
        result.insertAdjacentElement('beforebegin', host);
    }
    // SPA remount re-injects the page HTML, so the injected host (and any
    // earlier ctrl bound to it) is gone — remount whenever the host changes.
    const prev = _psComposeVarsCtrl;
    if (prev && prev.host === host) return;
    if (prev) { try { prev.destroy(); } catch (_) {} }
    const ta = () => document.getElementById('ps-compose-yaml');
    _psComposeVarsCtrl = mountVariablesPanel(host, () => ta()?.value || '', {
        onStateChange: _psSyncComposeCreateBtn,
    });
    _psSyncComposeCreateBtn();
}

// 13.5d — the create-flow Secrets section, mounted next to #ps-compose-result
// the same way _psMountComposeVariables mounts the variables panel (a host div
// the page HTML re-injection destroys, re-created on every successful Preview).
function _psMountCreateSecretsSection(spec) {
    const result = document.getElementById('ps-compose-result');
    if (!result || !spec) return;
    let host = document.getElementById('ps-compose-secrets');
    if (!host || !result.contains(host) && host !== result.nextElementSibling) {
        if (host) host.remove();
        host = document.createElement('div');
        host.id = 'ps-compose-secrets';
        host.dataset.testid = 'ps-secrets-section-create';
        host.className = 'border-t border-gray-200 pt-3 mt-4';
        result.insertAdjacentElement('afterend', host);
    }
    _psRenderSecretsSection('create', host, spec);
}

// TODO 13.5c inline_secrets — the preview's `inline_secrets` ("Move to
// Secrets") selection still needs its own affordance here; today the operator
// achieves the same result by switching an env row's binding to "new secret…"
// in the section above.

function _psSyncComposeCreateBtn() {
    const btn = document.getElementById('ps-compose-create');
    if (!btn) return;
    btn.disabled = !!(_psComposeVarsCtrl && !_psComposeVarsCtrl.isComplete());
}

function _psComposeVarsPayload() {
    return (_psComposeVarsCtrl && _psComposeVarsCtrl.isComplete())
        ? _psComposeVarsCtrl.getVariablesPayload()
        : {};
}

// "Preview rendered" — shared by both compose editors on this page (the
// "Create from compose" paste box and the per-stack edit-definition panel).
// POSTs the editor's CURRENT YAML to /stacks/render-preview and shows the
// server-rendered compose in a collapsible read-only panel under the textarea.
// View-only: it never writes the textarea, so the operator still saves the
// source YAML and the server renders it on save.
async function _psRenderPreview(textareaEl, panelEl, btnEl) {
    const yaml = (textareaEl && textareaEl.value) || '';
    if (!panelEl) return;
    if (!yaml.trim()) {
        panelEl.innerHTML = '<div class="mt-2 text-xs text-destructive">Paste a compose YAML first.</div>';
        return;
    }
    if (btnEl) btnEl.disabled = true;
    panelEl.innerHTML = '<div class="mt-2 text-xs text-gray-400">Rendering…</div>';
    try {
        const r = await apiCall('/stacks/render-preview', {
            method: 'POST', body: JSON.stringify({ compose_yaml: yaml }),
        });
        let body;
        if (r && r.rendered) {
            const n = Array.isArray(r.services) ? r.services.length : 0;
            const names = Array.isArray(r.services) ? r.services.map(s => _psEsc(String(s))).join(', ') : '';
            body = `<div class="text-xs font-medium text-gray-700 mb-1">Rendered from x-mod.serving → ${n} service${n === 1 ? '' : 's'}: ${names}</div>
                <pre readonly class="text-xs text-gray-100 whitespace-pre-wrap font-mono rounded p-2 max-h-64 overflow-auto" style="background-color:#111827">${_psEsc(r.compose_yaml || '')}</pre>`;
        } else {
            body = '<div class="text-xs text-gray-600">No x-mod.serving profile — deployed as written</div>';
        }
        _psRenderPreviewPanel(panelEl, body);
    } catch (e) {
        let msg;
        if (e && e.status === 403) msg = 'Platform admin required to preview';
        else if (e && e.status === 422) msg = (e && e.message) || String(e);
        else msg = `Preview failed: ${(e && e.message) || String(e)}`;
        _psRenderPreviewPanel(panelEl, `<div class="text-xs text-destructive">${_psEsc(msg)}</div>`);
    } finally {
        if (btnEl) btnEl.disabled = false;
    }
}

// FIX-CE-HIDE-RENDER-PREVIEW (D76): CE gate for the create-card
// 'Preview rendered' button. The button calls POST /stacks/render-preview, which is
// paid-only on CE (CORE/API/routers/persistent_stacks/persistent_stacks.py:2905
// _require_paid("services.llm_serving_render", "LLM serving gateway render")) - on CE it
// surfaced a red 'Preview failed: Not part of this edition: LLM serving gateway render.'
// line, i.e. a paid feature's UI on CE (owner rule). On CE neither the button nor its
// result panel is injected; the paid wiring below is byte-identical to the old code.
// Same edition predicate as the grants-card hide above (_psIsCeEdition, fail-open: an
// unknown/errored entitlements payload keeps the paid UI, matching that neighbour).
async function _psWireRenderPreview(anchorBtn) {
    _psCeEditionCached = await _psIsCeEdition();
    if (_psCeEditionCached) return;
    const btn = document.createElement('button');
    btn.id = 'ps-compose-render-preview';
    btn.setAttribute('data-testid', 'ps-render-preview-btn-create');
    btn.className = 'px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 border border-gray-300 rounded-lg hover:bg-gray-200';
    btn.title = "Show the compose the server would actually deploy from this YAML's x-mod.serving profile";
    btn.textContent = 'Preview rendered';
    anchorBtn.insertAdjacentElement('afterend', btn);
    const ta = document.getElementById('ps-compose-yaml');
    const panel = document.createElement('div');
    panel.id = 'ps-compose-render-preview-panel';
    panel.setAttribute('data-testid', 'ps-render-preview-panel-create');
    ta?.insertAdjacentElement('afterend', panel);
    btn.addEventListener('click', () => {
        _psRenderPreview(ta, panel, btn);
    });
}

// Cached edition verdict for the SYNCHRONOUS edit-definition renderer: the panel
// re-renders on ticks/refreshes long after the init wiring resolved, so the
// verdict is cached here (null = unknown -> fail-open, keep the paid UI, same
// direction as _psIsCeEdition's fail-open). _psLoadDefinition refreshes it
// before each render.
let _psCeEditionCached = null;

function _psCeRenderPreviewHidden() {
    return _psCeEditionCached === true;
}

async function _psRefreshCeEditionCache() {
    try { _psCeEditionCached = await _psIsCeEdition(); } catch (_) { /* keep previous verdict */ }
}

function _psRenderPreviewPanel(panelEl, bodyHtml) {
    panelEl.innerHTML = `<details open class="mt-2 rounded-lg border border-gray-300 bg-white">
        <summary class="cursor-pointer select-none px-2 py-1.5 text-xs font-semibold text-gray-700">Rendered preview (read-only — your YAML is unchanged)</summary>
        <div class="px-2 pb-2">${bodyHtml}</div>
    </details>`;
}

// Copy an existing stack: pull its stored compose_spec into the raw-compose
// box so the operator edits and re-creates it. Reuses the import path entirely
// — a clone is just a raw create seeded from another stack. JSON is valid YAML,
// so the spec drops straight into the textarea with no serializer. Secret env
// values come back from /definition as ***redacted*** (the edit-def contract),
// so a naive create would try to store those literally; we flag it and block
// Create until they're replaced, rather than silently baking placeholders in.
async function _psCloneStack(id, srcName) {
    const nameEl = document.getElementById('ps-compose-name');
    const yamlEl = document.getElementById('ps-compose-yaml');
    if (!nameEl || !yamlEl) return;
    try {
        const def = await apiCall(`/stacks/${id}/definition`);
        const spec = def.compose_spec || {};
        yamlEl.value = JSON.stringify(spec, null, 2);
        nameEl.value = `${srcName || 'stack'}-copy`;
        _psComposePreview = null;
        // Bring the compose card into view and focus the name so the next
        // action is obvious after the button click scrolls away from it.
        yamlEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        nameEl.focus();
        const redacted = JSON.stringify(spec).includes('***redacted***');
        if (redacted) {
            _psComposeResult('Copied from ' + _psEsc(srcName) + '. ⚠ Secret env values came back as '
                + '<code>***redacted***</code> — replace them with real values before creating, or the copy '
                + 'will store the placeholder. Then press Preview.', false);
        } else {
            _psComposeResult('Copied from ' + _psEsc(srcName) + '. Review the YAML and name, then press Preview.', true);
        }
    } catch (e) {
        _psComposeResult(`Copy failed: ${_psEsc(e.message || e)}`, false);
    }
}

async function _psComposeCreateClick() {
    // FIX-COMPOSE-PREVIEW — Create claims the compose-result sequence
    // immediately, so a Preview response still in flight can no longer
    // repaint #ps-compose-result over the "Created stack ..." line.
    _psCreateStarted = true;
    const yaml = document.getElementById('ps-compose-yaml')?.value || '';
    const name = _psComposeName();
    if (!name) return _psComposeResult('Give the stack a name first.', false);
    if (!yaml.trim()) return _psComposeResult('Paste a docker-compose YAML first.', false);
    if (yaml.includes('***redacted***')) {
        return _psComposeResult('This compose still has <code>***redacted***</code> placeholders (from a copied '
            + 'stack). Replace them with real secret values before creating.', false);
    }
    // Carry the preview's confirmations through: the server re-normalizes the
    // raw YAML, so a stale preview cannot smuggle a different spec in — it
    // only answers the questions Preview already asked.
    const confirmations = {};
    for (const c of ((_psComposePreview || {}).needs_confirmation || [])) {
        if (c.scope === 'service' && c.service) {
            confirmations.services = confirmations.services || {};
            confirmations.services[c.service] = confirmations.services[c.service] || {};
            confirmations.services[c.service][c.field] = c.suggested;
        } else if (c.scope === 'stack') {
            confirmations[c.field] = c.suggested;
        }
    }
    // GX.S10c — unresolved placeholders block the create (the button is gated
    // the same way), so the payload only carries the variables when every
    // detected row is resolved.
    const payload = { name, compose: yaml, confirmations };
    const variables = _psComposeVarsPayload();
    if (Object.keys(variables).length) payload.variables = variables;
    // 13.5d — gather the create-flow Secrets section's edits (key 'create').
    // With zero edits the payload is exactly today's (raw compose re-normalized
    // server-side); with edits we send the preview's compose_spec with the
    // changes applied instead — finalize_import ignores `compose` when
    // `compose_spec` is present, so the operator's bindings are what lands.
    let secEdits = { changes: { env: [], files: [] }, newSecrets: [], errors: [] };
    const secHost = document.getElementById('ps-compose-secrets');
    const prevSpec = (_psComposePreview || {}).compose_spec;
    if (secHost && prevSpec) {
        secEdits = _psCollectSecretsEdits('create', secHost, prevSpec);
        if (secEdits.errors.length) {
            return _psComposeResult('Secrets: ' + secEdits.errors.map(_psEsc).join('<br>'), false);
        }
        const nEdits = secEdits.changes.env.length + secEdits.changes.files.length;
        if (nEdits > 0) {
            // Create each NEW tenant secret first (write-only POST — the value
            // never comes back), then clear the masked inputs so the value does
            // not linger in the DOM past the submit.
            for (const ns of secEdits.newSecrets) {
                try {
                    await apiCall('/secrets/', {
                        method: 'POST', body: JSON.stringify({ name: ns.name, value: ns.value }),
                    });
                } catch (e) {
                    _psClearSecretSec('create');
                    return _psComposeResult(
                        `Could not create secret "${_psEsc(ns.name)}": ${_psEsc(e.message || e)}`, false);
                }
            }
            if (secHost) {
                secHost.querySelectorAll('input[data-ps-sec-newvalue]')
                    .forEach((i) => { i.value = ''; });
            }
            payload.compose_spec = psApplySecretChanges(
                JSON.parse(JSON.stringify(prevSpec)), secEdits.changes);
            delete payload.compose;
        }
    }
    const onCreated = (r) => {
        // GR.FEN.3b — the create/finalize response carries ha_class +
        // ha_class_reason (the create response is the only place the reason
        // appears; there is no column). Show why the class was assigned so the
        // operator sees e.g. "cache_backed — only model-cache mounts".
        let haLine = '';
        if (r && r.ha_class) {
            haLine = `<div>HA class: <code>${_psEsc(r.ha_class)}</code>${
                r.ha_class_reason ? ` — ${_psEsc(r.ha_class_reason)}` : ''}</div>`;
        }
        _psComposeResult(`Created stack "${_psEsc(r.name)}" (id ${Number(r.id)}, ${_psEsc(r.status)}).${haLine}`, true);
        // 13.5d — drop the in-flight section state so no masked value or
        // binding choice outlives the submit.
        _psClearSecretSec('create');
        _psLoadStacks();
    };
    try {
        const r = await apiCall('/stacks/import-compose/finalize', {
            method: 'POST', body: JSON.stringify(payload),
        });
        onCreated(r);
    } catch (e) {
        if (e && e.status === 409 && e.detail && e.detail.requires_ack) {
            return _psBackupAckShow(e.detail, '/stacks/import-compose/finalize', payload, onCreated);
        }
        if (e && e.status === 422 && _psComposeVarsCtrl) {
            // A save-time 422 names the unresolved placeholders — light up
            // exactly those rows so the operator sees what to fix.
            const missing = svMissingNames(e, _psComposeVarsCtrl.knownNames());
            if (missing.length) _psComposeVarsCtrl.markMissing(missing);
        }
        _psComposeResult(`Create failed: ${_psEsc(e.message || e)}`, false);
    }
}

// --- Stop / Start transition state -----------------------------------------
// Stop and Start only DISPATCH a task; the stack keeps its old status until the
// host reports back, so the row used to look unchanged and then jump. While a
// stop/start is in flight the row shows "stopping" / "starting" in the same
// status-pill styling, with that stack's controls disabled, until the server
// reports the final state (polled every 2.5 s for up to 2 minutes).
const _PS_TRANSIT_POLL_MS = 2500;
const _PS_TRANSIT_MAX_MS = 120000;
const _psTransit = {};           // stack id -> { verb, deadline, timer }
let _psLastWorkers = [];
const _PS_TRANSIT_FINAL = {
    stop: ['stopped', 'failed', 'torn_down', 'created', 'unreachable'],
    start: ['running', 'failed', 'torn_down', 'created', 'unreachable'],
};

// 'stopping' | 'starting' | '' — client-side pending verb first, then the
// intermediate statuses the API may report itself (waking reads as starting).
function _psTransitLabel(s) {
    const t = _psTransit[Number(s.id)];
    if (t) return t.verb === 'stop' ? 'stopping' : 'starting';
    if (s.status === 'stopping') return 'stopping';
    if (s.status === 'starting' || s.status === 'waking') return 'starting';
    return '';
}

function _psRepaintMain(id) {
    const s = _psById[id];
    const main = document.querySelector(`#ps-rows tr[data-ps-main="${Number(id)}"]`);
    if (!s || !main) return;
    const tmp = document.createElement('tbody');
    tmp.innerHTML = _psStackRow(s, _psLastWorkers);
    if (tmp.firstElementChild) main.replaceWith(tmp.firstElementChild);
}

function _psTransitEnd(id) {
    const t = _psTransit[id];
    if (t && t.timer) clearTimeout(t.timer);
    delete _psTransit[id];
}

// Drop every pending transition whose final state has arrived in `stacks`.
function _psTransitSettle(stacks) {
    for (const s of stacks) {
        const t = _psTransit[Number(s.id)];
        if (!t || !t.acked) continue;   // a list fetched before the POST landed proves nothing
        const wanted = t.verb === 'stop' ? 'stopped' : 'running';
        const done = _PS_TRANSIT_FINAL[t.verb].includes(s.status)
            || (s.desired_state && s.desired_state !== wanted);
        if (done) _psTransitEnd(Number(s.id));
    }
}

async function _psTransitPoll(id) {
    const t = _psTransit[id];
    if (!t) return;
    let stacks = null;
    try { stacks = await apiCall('/stacks'); } catch (_) { stacks = null; }
    if (_psTransit[id] !== t) return;          // ended meanwhile
    if (stacks) {
        stacks.forEach((s) => { _psById[s.id] = s; });
        _psTransitSettle(stacks);
        if (!_psTransit[id]) { _psPatchStacks(stacks, _psLastWorkers); _psRepaintMain(id); return; }
    }
    if (Date.now() >= t.deadline) {
        _psTransitEnd(id);
        _psRepaintMain(id);
        _psMsg(`Stack ${id} did not report ${t.verb === 'stop' ? 'stopped' : 'running'} within 2 minutes — showing its last known state.`, false);
        return;
    }
    t.timer = setTimeout(() => _psTransitPoll(id), _PS_TRANSIT_POLL_MS);
}

async function _psTransitAct(id, verb, q) {
    id = Number(id);
    if (_psTransit[id]) return;                 // already changing
    _psTransit[id] = { verb, deadline: Date.now() + _PS_TRANSIT_MAX_MS, timer: null };
    _psRepaintMain(id);                          // optimistic: before the API answers
    try {
        await apiCall(`/stacks/${id}/${verb}${q}`, { method: 'POST' });
    } catch (e) {
        _psTransitEnd(id);
        _psRepaintMain(id);                      // restore the previous state
        _psMsg(`${verb} failed: ${e.message || e}`, false);
        return;
    }
    _psMsg(`${verb} dispatched for stack ${id}.`, true);
    const t = _psTransit[id];
    if (t) { t.acked = true; t.timer = setTimeout(() => _psTransitPoll(id), _PS_TRANSIT_POLL_MS); }
}

async function _psAct(id, verb, q = '') {
    if (verb === 'stop' || verb === 'start') return _psTransitAct(id, verb, q);
    try {
        await apiCall(`/stacks/${id}/${verb}${q}`, { method: 'POST' });
        _psMsg(`${verb} dispatched for stack ${id}.`, true);
        _psLoadStacks();
    } catch (e) { _psMsg(`${verb} failed: ${e.message || e}`, false); }
}

// Soft-delete a stack that was never deployed (status "created", no host). The
// API refuses (409) anything that has a live deploy — the operator must tear
// down first, and the API's detail message says so; we surface it verbatim
// through _psMsg (textContent, never innerHTML).
async function _psDelete(id) {
    if (!confirm(`Delete stack ${id}? It was never deployed, so nothing has to be torn down first.`)) return;
    try {
        await apiCall(`/stacks/${id}`, { method: 'DELETE' });
        _psMsg(`Stack ${id} deleted.`, true);
        _psLoadStacks();
    } catch (e) { _psMsg(`Delete failed: ${e.message || e}`, false); }
}

// Inspect a stack's container output — the "get into the container" affordance.
// Asks the host that runs the stack for its compose log tail and shows it inline
// in the expandable row under the stack. Refresh re-fetches.
// --- Live stack logs --------------------------------------------------------
// The logs endpoint is NOT a cheap read: POST /stacks/{id}/logs dispatches a
// task to the host worker and waits for it to return the container's logs, and
// it 409s when the stack has no host yet (never deployed). So "auto-updating"
// cannot be a fixed-interval timer firing regardless of whether the last call
// returned — that is exactly the no-backoff setInterval storm that once
// self-DDoS'd the platform. Instead this is a SEQUENTIAL poller: fetch →
// render → wait a gap → fetch again, so a new request never starts until the
// previous one finished. It backs off on error, pauses while the tab is
// hidden, stops on 409/close, and only one runs per stack at a time.
const _PS_LOG_BASE_MS = 5000;   // gap after a successful fetch completes
const _PS_LOG_MAX_MS = 30000;   // backoff ceiling on repeated errors
const _psLogPollers = {};       // stackId -> { live, delayMs, timer, gen }
let _psShowMeshSidecars = false;   // "Show mesh sidecars" toggle, per page session
const _psLastLogPayload = {};      // stackId -> { r, err } so the toggle can re-render without a refetch

function _psCancelLogPoller(id) {
    const st = _psLogPollers[id];
    if (st) {
        if (st.timer) clearTimeout(st.timer);
        st.gen++;            // invalidate any in-flight tick's scheduling
        delete _psLogPollers[id];
    }
}

function _psCancelAllLogPollers() {
    for (const id of Object.keys(_psLogPollers)) _psCancelLogPoller(id);
}

// Mesh sidecar services (ts-*/tsegress-*/ingress-*) crash-loop into a
// restart storm and flood the 4KB log tail, pushing the stack's own app logs
// out of it. This is the deterministic filter behind the "Show mesh sidecars"
// toggle. Sidecar names carry an optional project prefix (compose renders
// `<project>-<service>-<n>`), so the sidecar prefixes are matched at a
// dash boundary rather than the line/section start.
function _psIsSidecarName(name) {
    return /(?:^|[--])(?:ts|tsegress|ingress)-/.test(String(name || ''));
}

// Pure: given the raw log tail, return { text, hidden } — the text the pane
// should show with sidecar output removed (or untouched when show is true),
// and how many lines were dropped. Handles both worker shapes:
//   (a) a `compose ps -a` status table, then one `=== <service> ===` section
//       per service (newer workers) — whole sidecar sections are dropped;
//   (b) one combined stream, each line prefixed `<service>-<n>  | ` (older
//       workers) — sidecar-prefixed lines are dropped.
function _psFilterSidecarLogs(text, show) {
    const lines = String(text == null ? '' : text).split('\n');
    if (show) return { text: lines.join('\n'), hidden: 0 };
    const out = [];
    let hidden = 0;
    let inSidecarSection = false;
    for (const line of lines) {
        const hdr = line.match(/^===\s+(.+?)\s*===\s*$/);
        if (hdr) {
            inSidecarSection = _psIsSidecarName(hdr[1]);
            if (inSidecarSection) hidden++; else out.push(line);
            continue;
        }
        if (inSidecarSection) { hidden++; continue; }
        const pre = line.match(/^([^\s|]+?)-\d+\s*\|/);
        if (pre && _psIsSidecarName(pre[1])) { hidden++; continue; }
        out.push(line);
    }
    // Dropped sections leave the newlines between sections stacked up;
    // collapse runs of blank lines so the remaining output reads normally.
    return { text: out.join('\n').replace(/\n{3,}/g, '\n\n'), hidden };
}

// Update just the <pre> + status line (not the whole panel) so the Live toggle,
// scroll position and selection survive a refresh. Sticks to the bottom only
// when the viewer was already at the bottom, so scrolling up to read holds.
// Statuses where no container is (or will soon be) running, so an empty log
// is final rather than "still loading". Returns the status or ''.
function _psLogsDeadStatus(id, r) {
    const st = String((r && r.stack_status) || (_psById[id] || {}).status || '').toLowerCase();
    return ['failed', 'stopped', 'torn_down', 'teardown', 'deleted', 'error', 'unplaceable'].includes(st) ? st : '';
}

function _psRenderLogBody(id, r, err) {
    const pre = document.getElementById(`ps-logs-pre-${id}`);
    if (!pre) return false;   // panel gone — caller should stop the poller
    // Remember the last payload so "Show mesh sidecars" can re-render without
    // dispatching another worker task.
    _psLastLogPayload[id] = { r, err };
    const raw = ((r && r.logs) || '').trim();
    const { text: logs, hidden: hiddenLines } = _psFilterSidecarLogs(raw, _psShowMeshSidecars);
    const note = document.getElementById(`ps-logs-sidernote-${id}`);
    if (note) {
        if (raw && hiddenLines > 0) {
            note.textContent = `${hiddenLines} sidecar line${hiddenLines === 1 ? '' : 's'} hidden`;
            note.classList.remove('hidden');
        } else {
            note.textContent = '';
            note.classList.add('hidden');
        }
    }
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 6;
    // A running/deploying stack with no output yet is the scary case: a blank
    // panel reads as "broken" when it's just the load window. Say plainly that
    // silence is expected and the view is live, instead of a bare "(no output)".
    if (logs) {
        pre.textContent = logs;
        pre.classList.remove('opacity-60', 'italic');
    } else if (raw && !logs) {
        // The tail was 100% mesh-sidecar output: hiding it leaves nothing, and
        // the important thing to say is that the app's own logs were pushed
        // out of the tail by the sidecar spam.
        pre.textContent = `Only mesh-sidecar output was returned — the app's own logs were pushed out of the tail. Toggle 'Show mesh sidecars' to see it.`;
        pre.classList.remove('opacity-60');
        pre.classList.add('italic');
    } else if (!err && _psLogsDeadStatus(id, r)) {
        // A stack whose deploy failed (or that is stopped / torn down) has no
        // container to log — "waiting" would be a lie that never resolves. Show
        // the reason the platform recorded instead (stack 34, 09-29: an invalid
        // compose project, surfaced only as an eternal "Waiting…").
        const st = _psLogsDeadStatus(id, r);
        const reason = (r && r.stack_reason) || (_psById[id] || {}).status_reason || '';
        pre.textContent = `No container output — the stack is ${st}, so there is no running container to log.`
            + (reason ? `\n\nReason recorded for this stack:\n${reason}` : '')
            + '\n\nFix the cause (Edit…) and Deploy again; this view stays live and will pick up output once a container starts.';
        pre.classList.remove('opacity-60', 'italic');
    } else if (!err) {
        pre.textContent = 'Waiting for the container to log…\n\n'
            + 'This is normal right after a deploy: pulling the image and loading a '
            + 'large model into GPU memory can take several minutes with no output. '
            + 'The view is live — lines will appear here on their own as soon as the '
            + 'container prints them.';
        pre.classList.add('opacity-60', 'italic');
    } else {
        pre.textContent = '';
        pre.classList.remove('opacity-60', 'italic');
    }
    if (atBottom && logs) pre.scrollTop = pre.scrollHeight;
    const statusEl = document.getElementById(`ps-logs-status-${id}`);
    if (statusEl) {
        statusEl.textContent = `stack ${id}${r && r.worker ? ` · ${r.worker}` : ''}${r && r.status ? ` · ${r.status}` : ''}`;
    }
    const errEl = document.getElementById(`ps-logs-err-${id}`);
    if (errEl) { errEl.textContent = err || ''; errEl.classList.toggle('hidden', !err); }
    return true;
}

// "Show mesh sidecars" flip: flip the session flag and re-render from the last
// fetched payload (no new worker task — the same tail, re-filtered).
function _psToggleShowSidecars(id, checked) {
    _psShowMeshSidecars = !!checked;
    const p = _psLastLogPayload[id];
    if (p) _psRenderLogBody(id, p.r, p.err);
}

function _psSetLogLiveLabel(id) {
    const st = _psLogPollers[id];
    const btn = document.getElementById(`ps-logs-live-${id}`);
    if (btn) btn.textContent = (st && st.live) ? '⏸ Pause' : '↻ Live';
}

async function _psLogPollTick(id) {
    const st = _psLogPollers[id];
    if (!st) return;
    const gen = st.gen;
    // A hidden tab must not keep dispatching worker tasks; recheck shortly.
    if (document.hidden) {
        st.timer = setTimeout(() => _psLogPollTick(id), 2000);
        return;
    }
    let ok = false;
    try {
        // Shorter server-side wait than the manual 60s: a live tick should not
        // block a worker for a minute, it should come back and try again.
        const r = await apiCall(`/stacks/${id}/logs?timeout_s=20`, { method: 'POST' });
        if (!_psLogPollers[id] || _psLogPollers[id].gen !== gen) return; // cancelled mid-flight
        const present = _psRenderLogBody(id, r, r && r.error);
        if (!present) { _psCancelLogPoller(id); return; }
        ok = true;
        st.delayMs = _PS_LOG_BASE_MS;
    } catch (e) {
        if (!_psLogPollers[id] || _psLogPollers[id].gen !== gen) return;
        const msg = (e && e.message) || String(e);
        // 409 = no host on record (never deployed). Nothing will change until a
        // deploy, so stop polling and say so rather than hammer a 409 forever.
        if (/^409/.test(msg)) {
            _psRenderLogBody(id, { status: 'not deployed' }, 'Not deployed yet — deploy this stack to stream its container logs.');
            const btn = document.getElementById(`ps-logs-live-${id}`);
            if (btn) btn.classList.add('hidden');
            _psCancelLogPoller(id);
            return;
        }
        _psRenderLogBody(id, null, `Logs fetch failed: ${msg} — retrying…`);
        st.delayMs = Math.min((st.delayMs || _PS_LOG_BASE_MS) * 2, _PS_LOG_MAX_MS);
    }
    if (!_psLogPollers[id] || _psLogPollers[id].gen !== gen) return;
    if (st.live) st.timer = setTimeout(() => _psLogPollTick(id), st.delayMs);
}

function _psToggleLogLive(id) {
    const st = _psLogPollers[id];
    if (!st) return;
    st.live = !st.live;
    _psSetLogLiveLabel(id);
    if (st.live) {
        if (st.timer) clearTimeout(st.timer);
        _psLogPollTick(id);   // resume immediately
    } else if (st.timer) {
        clearTimeout(st.timer);
        st.timer = null;
    }
}

function _psLogs(id) {
    const row = document.getElementById(`ps-backups-${id}`);
    if (!row) return;
    _psCancelLogPoller(id);   // one poller per stack; re-open restarts it
    row.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-900">
            <div class="flex items-center justify-between mb-2">
                <span class="text-xs font-semibold text-gray-300">Container logs — <span id="ps-logs-status-${id}">stack ${id}</span> <span class="text-emerald-400" title="Auto-updating">● live</span>
                    <label class="ml-3 inline-flex items-center gap-1 font-normal text-gray-400 cursor-pointer select-none" title="Mesh sidecar services (ts-*/tsegress-*/ingress-*) crash-loop into the tail and drown the app's own output — hidden by default">
                        <input type="checkbox" data-ps-act="logs-show-sidecars" data-ps-id="${id}" id="ps-logs-show-sidecars-${id}" ${_psShowMeshSidecars ? 'checked' : ''} />
                        Show mesh sidecars
                    </label>
                </span>
                <span>
                    <button id="ps-logs-live-${id}" data-ps-act="logs-live" data-ps-id="${id}" class="text-xs text-gray-400 hover:text-white mr-3">⏸ Pause</button>
                    <button data-ps-act="copylogs" data-ps-id="${id}" class="text-xs text-gray-400 hover:text-white mr-3">⧉ Copy</button>
                    <button data-ps-act="logs-now" data-ps-id="${id}" class="text-xs text-gray-400 hover:text-white mr-3" title="Fetch once now">↻ Now</button>
                    <button data-ps-act="logs-close" data-ps-id="${id}" class="text-xs text-gray-400 hover:text-white">✕ Close</button>
                </span>
            </div>
            <div id="ps-logs-err-${id}" class="text-xs text-red-400 mb-2 hidden"></div>
            <div id="ps-logs-sidernote-${id}" class="text-xs text-amber-400 mb-1 hidden"></div>
            <pre id="ps-logs-pre-${id}" class="text-xs text-gray-100 whitespace-pre-wrap max-h-96 overflow-auto font-mono leading-relaxed">Fetching logs from the host…</pre>
        </td>`;
    _psLogPollers[id] = { live: true, delayMs: _PS_LOG_BASE_MS, timer: null, gen: 0 };
    _psLogPollTick(id);
}

// §8.1 — a host pin, exposed. The column, the endpoint and the rebalancer's
// respect for it all shipped; nothing ever called it from the UI, so the only
// way to stop the rebalancer moving one stack was to disable it for every
// stack. A reason is asked for rather than required: a pin outlives whoever
// set it, and "why can this never move" should not need archaeology.
// Worker names are typed by nobody. A free-text prompt asks the operator to
// reproduce an exact identifier from memory, and the endpoint 404s on a typo —
// so the only outcomes are "right" and "rejected for no visible reason". The
// fleet is a known, short list; it belongs in a select.
async function _psWorkerNames() {
    try {
        // Shape is {workers: [{worker, ...}]} — mapping the envelope itself
        // yields nothing and would silently produce an empty dropdown.
        const resp = await apiCall('/stacks/accelerator-capacity');
        const names = ((resp && resp.workers) || [])
            .map((r) => r.worker || r.worker_name).filter(Boolean);
        if (names.length) return names;
    } catch (_) { /* fall through */ }
    return [];
}

// Library "Open" deep link (?stack=<id>): flash-highlight the matching row so
// a just-installed stack is findable in the list. One-shot — consumed on the
// first render after navigation, and harmless when the id is absent/unknown
// (a non-admin never reaches this point; init returns early).
function _psHighlightStackFromQuery() {
    const stackId = new URLSearchParams(window.location.search).get('stack');
    if (!stackId) return;
    const row = document.querySelector(`#ps-rows tr[data-stack-id="${stackId}"]`);
    if (!row) return;
    row.style.transition = 'background-color 300ms ease';
    row.style.backgroundColor = 'rgba(59,130,246,0.18)';
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.setTimeout(() => { row.style.backgroundColor = ''; }, 3000);
}

// A worker whose hardware can NEVER run this stack (API: hardware_ineligible —
// wrong GPU kind, too few or too small cards, or its only card of that kind is
// out of service) is shown but disabled, marked "(can't run)". The full reason
// goes in the title and in the note under the pin dialog — not in the option
// text: a native <select> is as wide as its longest option, so long reasons
// blew the Worker column off the page. No inline colours: the option inherits
// the select's, so it stays readable on light and dark backgrounds.
function _psWorkerOption(name, stack, selected) {
    const why = (stack && stack.hardware_ineligible || {})[name];
    const label = why ? `${name} (can't run)` : name;
    return `<option value="${_psEsc(name)}"${selected ? ' selected' : ''}${why ? ' disabled' : ''}`
        + ` title="${_psEsc(why || '')}">${_psEsc(label)}</option>`;
}

// "worker: reason" lines for the systems that can't run the stack, so the
// reasons are readable somewhere other than a (possibly tooltip-less) option.
function _psIneligibleNote(names, stack) {
    const bad = (stack && stack.hardware_ineligible) || {};
    const rows = names.filter((n) => bad[n])
        .map((n) => `<div><span class="font-semibold">${_psEsc(n)}</span>: ${_psEsc(bad[n])}</div>`);
    return rows.length
        ? `<div class="mt-1 text-xs text-gray-500" style="max-width:28rem;white-space:normal">Can't run this stack:${rows.join('')}</div>`
        : '';
}

async function _psPin(id, currentWorker) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    const names = await _psWorkerNames();
    // Whatever the stack already runs on stays offerable even if the capacity
    // call could not enumerate the fleet — otherwise a transient API failure
    // leaves an empty dropdown and no way to pin at all.
    if (currentWorker && !names.includes(currentWorker)) names.unshift(currentWorker);
    const stack = _psById[id] || {};
    const bad = stack.hardware_ineligible || {};
    // Preselect the current host only if it can actually run the stack;
    // otherwise the first capable one. A disabled option must never be the
    // silent default, or "Pin" would submit a choice the API refuses.
    const pick = [currentWorker, ...names].find((n) => n && names.includes(n) && !bad[n]) || '';

    host.dataset.open = '1';
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="text-sm font-semibold mb-2 text-gray-900">Pin stack ${id} to a host</div>
        <div class="text-xs mb-3 text-gray-700">The rebalancer will leave it alone.
            Pinning does <strong>not</strong> move the stack — use Migrate for that, then pin.</div>
        <div class="flex flex-wrap items-end gap-3">
            <div>
                <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-pin-worker-${id}">Worker</label>
                <select id="ps-pin-worker-${id}" class="px-3 py-2 text-sm border border-gray-400 rounded-lg bg-white"
                       >
                    ${names.length
                        ? names.map((n) => _psWorkerOption(n, stack, n === pick)).join('')
                        : '<option value="">No workers found</option>'}
                </select>
                ${names.length && !pick
                    ? '<div class="mt-1 text-xs" style="color:#b91c1c">None of these systems has the hardware this stack needs.</div>'
                    : ''}
                ${_psIneligibleNote(names, stack)}
            </div>
            <div class="flex-1 min-w-48">
                <!-- Colours set INLINE, not by class. Utility classes were being
                     beaten by something in the cascade and the field rendered
                     near-white text in a white box — you could not read what you
                     had typed. An inline style has nothing left to lose to. -->
                <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-pin-reason-${id}">Why is it pinned? <span class="font-normal text-gray-700">(optional — it is what someone reads in six months)</span></label>
                <input id="ps-pin-reason-${id}" class="px-3 py-2 text-sm border border-gray-400 rounded-lg w-full bg-white"
                      
                       placeholder="e.g. holds the card deliberately" />
            </div>
            <button data-ps-act="pin-confirm" data-ps-id="${id}" class="px-3 py-2 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"${pick ? '' : ' disabled'}>Pin</button>
            <button data-ps-act="pin-cancel" data-ps-id="${id}" class="px-3 py-2 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">Cancel</button>
        </div></td>`;
}

function _psPinCancel(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (host) { host.innerHTML = ''; host.dataset.open = ''; }
}

async function _psPinConfirm(id) {
    const worker = document.getElementById(`ps-pin-worker-${id}`)?.value || '';
    const reason = document.getElementById(`ps-pin-reason-${id}`)?.value.trim() || null;
    if (!worker) { _psMsg('Pin cancelled: no worker selected.', false); return; }
    try {
        const r = await apiCall(`/stacks/${id}/host-pin`, {
            method: 'PUT',
            body: JSON.stringify({ worker, reason }),
        });
        _psPinCancel(id);
        // A pin to a host that cannot currently host it is allowed on purpose —
        // pinning ahead of hardware is legitimate — so the warning is surfaced
        // rather than swallowed or turned into a refusal.
        _psMsg(r.warning
            ? `Pinned to ${r.pinned_worker}. Warning: ${r.warning}`
            : `Stack ${id} pinned to ${r.pinned_worker}.`, !r.warning);
        _psLoadStacks();
    } catch (e) { _psMsg(`Pin failed: ${e.message || e}`, false); }
}

async function _psUnpin(id) {
    try {
        await apiCall(`/stacks/${id}/host-pin`, {
            method: 'PUT', body: JSON.stringify({ clear: true }),
        });
        _psMsg(`Stack ${id} unpinned — the rebalancer may move it again.`, true);
        _psLoadStacks();
    } catch (e) { _psMsg(`Unpin failed: ${e.message || e}`, false); }
}

// GPUCAP P1b — the GPU CARD pin, exposed. The host pin above says WHICH system
// runs the stack; this second, nested affordance says WHICH of that system's
// cards the stack's GPU services may use. It is a different control on a
// different endpoint and is never merged into the host pin.
// It rides the EXISTING authoring path PUT /stacks/{id}/gpu-pin (admin-gated,
// NOT paid-gated): no new route, no new body shape, no new status. Everything
// the client offers is what the server accepts — services come from the stack's
// own definition (a service with no x-mod.accelerator declaration 422s
// pin_service_unknown, so it is never offered), cards come ONLY from
// /accelerators/worker/<this stack's host> (never a fleet-wide list: the
// server's tenant fence and the per-worker hardware_profile scoping stay the
// only authority), and the declared `count` floor plus the 16/16 GpuPinIn
// bounds are enforced before any request. Card ids follow the server's own
// fallback order (uuid, else worker:kind:index).
// Hidden, not disabled: a stack with no host pin has no buttons at all (the
// API 409s no_host_pin there), and the whole page is already admin-gated.
async function _psGpuPin(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'gpu-pin') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        return;
    }
    const s = _psById[id] || {};
    const workerName = s.pinned_worker || '';
    if (!workerName) {
        _psMsg('Pin this stack to a host first: card ids name devices on one specific worker.', false);
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'gpu-pin';
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-gray-500">Loading this host's cards…</td>`;

    let plan = [];
    let cards = [];
    let loadErr = '';
    try {
        const def = await apiCall(`/stacks/${id}/definition`);
        const services = (def && def.compose_spec && def.compose_spec.services) || {};
        for (const name of Object.keys(services)) {
            const acc = ((services[name] || {})['x-mod'] || {}).accelerator;
            if (!acc) continue;
            plan.push({
                name,
                count: Math.max(1, Number(acc.count || 1)),
                kind: String(acc.kind || 'cuda').trim().toLowerCase(),
                minVram: acc.min_vram_gb == null ? null : Number(acc.min_vram_gb),
            });
        }
    } catch (e) { loadErr = `Could not read this stack's GPU services: ${e.message || e}`; }
    try {
        const acc = await apiCall(`/accelerators/worker/${encodeURIComponent(s.pinned_worker)}`);
        cards = ((acc && acc.accelerators) || []).map((a) => {
            const kind = String(a.kind || '').trim().toLowerCase();
            return {
                id: String(a.uuid || `${workerName}:${kind}:${a.index}`),
                kind,
                vram: a.vram_gb,
                usable: !a.admin_disabled && !!a.schedulable && !!a.vram_gb,
            };
        }).filter((c) => c.usable);
    } catch (e) { if (loadErr) loadErr = loadErr; else loadErr = `Could not read this host's cards: ${e.message || e}`; }
    if (loadErr) _psMsg(loadErr, false);

    const offered = plan.slice(0, 16);
    host.dataset.psGpuPlan = JSON.stringify(offered);
    const rows = offered.map((p) => {
        const mine = cards.filter((c) => c.kind === p.kind
            && (p.minVram == null || Number(c.vram) + 0.5 >= p.minVram)).slice(0, 16);
        return `<div class="flex flex-wrap items-center gap-2 mb-2">
            <span class="text-xs font-semibold text-gray-700">${_psEsc(p.name)}</span>
            <span class="text-xs text-gray-500">needs ${p.count} card(s) of ${p.kind}</span>
            ${mine.length
                ? mine.map((c) => `<label class="inline items-center gap-1 text-xs text-gray-700">
                    <input type="checkbox" data-ps-act="gpu-pin-toggle" data-ps-id="${id}" data-ps-gpu="${_psEsc(p.name)}" data-ps-gpu-card="${_psEsc(c.id)}" class="px-2 py-1 border border-gray-400 rounded bg-white" />
                    <span>${_psEsc(c.id)}</span>
                </label>`).join('')
                : `<span class="text-xs" style="color:#b91c1c">no capable card on this host</span>`}
        </div>`;
    }).join('');
    const canSubmit = offered.length > 0 && cards.length > 0 && !loadErr;
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="text-sm font-semibold mb-2 text-gray-900">Pin stack ${id} to GPU cards</div>
        <div class="text-xs mb-3 text-gray-700">The pin is an <strong>allowed set</strong>, not an assignment: the next Deploy picks from these cards and the planner's rules still apply. Nothing moves now.</div>
        ${rows}
        <div class="flex flex-wrap items-end gap-3">
            <div class="flex-1 min-w-48">
                <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-gpu-pin-reason-${id}">Why these cards? <span class="font-normal text-gray-700">(optional)</span></label>
                <input id="ps-gpu-pin-reason-${id}" class="px-3 py-2 text-sm border border-gray-400 rounded-lg w-full bg-white" placeholder="e.g. the only card with the headroom" />
            </div>
            <button data-ps-act="gpu-pin-confirm" data-ps-id="${id}" class="px-3 py-2 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"${canSubmit ? '' : ' disabled'}>Save pin</button>
            <button data-ps-act="gpu-pin-cancel" data-ps-id="${id}" class="px-3 py-2 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">Cancel</button>
        </div></td>`;
}

function _psGpuPinCancel(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (host) { host.innerHTML = ''; host.dataset.open = ''; host.dataset.openAct = ''; }
}

async function _psGpuPinConfirm(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    let plan = [];
    try { plan = JSON.parse((host && host.dataset.psGpuPlan) || '[]'); } catch (_) { plan = []; }
    const s = _psById[id] || {};
    if (!s.pinned_worker) { _psMsg('Card pin cancelled: this stack has no host pin.', false); return; }
    if (!plan.length) { _psMsg('Nothing to pin: this stack declares no GPU services.', false); return; }
    if (plan.length > 16) { _psMsg('Card pin not sent: too many GPU services for one request (16 max).', false); return; }
    const pin = {};
    for (const p of plan) {
        const boxes = document.querySelectorAll(`[data-ps-act="gpu-pin-toggle"][data-ps-id="${id}"][data-ps-gpu="${p.name}"]`);
        const ids = [];
        for (const b of boxes) { if (b.checked) ids.push(b.dataset.psGpuCard); }
        if (ids.length < p.count) {
            _psMsg(`Card pin not sent: ${p.name} needs at least ${p.count} card(s) — select ${p.count}.`, false);
            return;
        }
        if (ids.length > 16) { _psMsg(`Card pin not sent: ${p.name} has more than 16 cards selected (16 max).`, false); return; }
        pin[p.name] = ids;
    }
    if (!Object.keys(pin).length) { _psMsg('Card pin cancelled: no card selected.', false); return; }
    const reason = document.getElementById(`ps-gpu-pin-reason-${id}`)?.value.trim() || null;
    try {
        const r = await apiCall(`/stacks/${id}/gpu-pin`, {
            method: 'PUT',
            body: JSON.stringify({ pin, reason }),
        });
        _psGpuPinCancel(id);
        _psMsg(r.warning
            ? `Card pin saved for stack ${id}. Warning: ${r.warning}`
            : `Card pin saved for stack ${id}.`, !r.warning);
        _psLoadStacks();
    } catch (e) { _psMsg(`Card pin failed: ${e.message || e}`, false); }
}

async function _psGpuUnpin(id) {
    try {
        const r = await apiCall(`/stacks/${id}/gpu-pin`, {
            method: 'PUT',
            body: JSON.stringify({ clear: true, reason: null }),
        });
        _psMsg(`Stack ${id} card pin cleared.`, true);
        _psLoadStacks();
    } catch (e) { _psMsg(`Card pin clear failed: ${e.message || e}`, false); }
}

// The Worker column is where a stack is PUT on a system. Choosing a worker
// pins the stack there (PUT /host-pin), which every later deploy honours
// (deploy_stack targets pinned_worker) and the rebalancer respects; "Any
// worker" clears the pin and leaves placement to claim time. A pin never moves
// a running stack by itself, so when one is up elsewhere the operator is
// offered the move explicitly rather than having it happen as a side effect.
const _PS_ACTIVE = new Set(['running', 'deploying', 'migrating', 'unreachable']);

function _psWorkerSelect(s, workers) {
    const names = [...(workers || [])];
    // The stack's own host and pin stay selectable even when the fleet call
    // failed or that worker has since dropped out of the capacity report.
    [s.pinned_worker, s.target_worker].forEach((w) => {
        if (w && !names.includes(w)) names.push(w);
    });
    const chosen = s.pinned_worker || '';
    const title = s.pinned_worker
        ? (s.pinned_reason || `Pinned to ${s.pinned_worker} — deploys go here and the rebalancer will not move it`)
        : 'Any worker — placed on whichever worker can host it';
    const bad = s.hardware_ineligible || {};
    // The current pin stays selected even if its host can't run the stack —
    // hiding it would misreport what the row says — but is flagged below.
    const opts = [`<option value=""${chosen ? '' : ' selected'}>Any worker (auto)</option>`]
        .concat(names.map((n) => (n === chosen && bad[n])
            ? `<option value="${_psEsc(n)}" selected>${_psEsc(n)}</option>`
            : _psWorkerOption(n, s, n === chosen)));
    const pinBad = chosen && bad[chosen]
        ? `<div class="mt-1 text-xs text-destructive" style="max-width:16rem;white-space:normal">${_psEsc(chosen)} can't run this stack: ${_psEsc(bad[chosen])}. Pick another system.</div>`
        : '';
    // Where it actually runs is shown only when that differs from the choice —
    // "pinned to A, running on B" is the state someone needs to notice.
    const runningOn = s.target_worker && s.target_worker !== chosen
        ? `<div class="mt-1 text-xs text-gray-500">${chosen ? 'currently on' : 'on'} ${_psEsc(s.target_worker)}</div>`
        : '';
    return `<select data-ps-worker-select data-ps-id="${Number(s.id)}" title="${_psEsc(title)}" aria-label="${_psEsc(title)}"
            class="px-3 py-2 text-sm border border-gray-300 rounded-lg" style="max-width:16rem">${opts.join('')}</select>${pinBad}${runningOn}`;
}

async function _psSetWorker(id, worker, sel) {
    const s = _psById[id] || {};
    if (sel) sel.disabled = true;
    try {
        const r = await apiCall(`/stacks/${id}/host-pin`, {
            method: 'PUT',
            body: JSON.stringify(worker ? { worker, reason: 'Chosen in the Worker dropdown' } : { clear: true }),
        });
        if (!worker) {
            _psMsg(`Stack ${id} can run on any worker.`, true);
        } else if (_PS_ACTIVE.has(s.status) && s.target_worker && s.target_worker !== worker) {
            if (confirm(`Stack ${id} is running on ${s.target_worker}. Move it to ${worker} now?`)) {
                await apiCall(`/stacks/${id}/migrate?to_worker=${encodeURIComponent(worker)}`, { method: 'POST' });
                _psMsg(`Stack ${id} is moving to ${worker}.`, true);
            } else {
                _psMsg(`Stack ${id} set to ${worker}; it stays on ${s.target_worker} until moved or redeployed.`, true);
            }
        } else {
            // Warning = the chosen host cannot currently fit it. Allowed on
            // purpose (pinning ahead of hardware), so surfaced, not refused.
            _psMsg(r && r.warning
                ? `Stack ${id} set to ${worker}. Warning: ${r.warning}`
                : `Stack ${id} set to ${worker} — Deploy puts it there.`, !(r && r.warning));
        }
    } catch (e) {
        _psMsg(`Could not set worker: ${e.message || e}`, false);
    }
    _psLoadStacks();
}

async function _psBackup(id) {
    try {
        const r = await apiCall(`/stacks/${id}/backup`, { method: 'POST' });
        _psMsg(`Backup ${r.snapshot_id} dispatched for stack ${id}.`, true);
        _psLoadStacks();
    } catch (e) { _psMsg(`Backup failed: ${e.message || e}`, false); }
}

// §9.5/§9.6/§9.7/§9.9 — everything about whether this stack's data is actually
// safe, in one place.
//
// Rendered into the row rather than a modal on purpose: this is a state to sit
// and read, and the numbers that matter (how far back the copies reach, which
// one goes next, whether any has ever been proven to restore) are the kind an
// operator wants beside the stack they are looking at.
async function _psBackups(id) {
    let d;
    try {
        d = await apiCall(`/stacks/${id}/backups`);
    } catch (e) {
        _psMsg(`Could not load backups: ${e.message || String(e)}`, false);
        return;
    }
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1') {          // toggle closed
        host.innerHTML = '';
        host.dataset.open = '';
        return;
    }
    host.dataset.open = '1';

    const r = d.retention || {};
    const sch = d.schedule || {};

    const snapRows = (d.snapshots || []).map((s) => {
        const badge = s.verification === 'verified'
            ? '<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">verified</span>'
            : s.verification === 'failed'
                ? `<span class="px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700" title="${_psEsc(s.verify_error || '')}">FAILED to restore</span>`
                // Not "unverified" — that word reads like a minor flag. Nobody
                // has ever tried to restore it, and that is the honest wording.
                : '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600">never tested</span>';
        const doomed = r.next_to_go && r.next_to_go.snapshot_id === s.snapshot_id;
        return `<tr class="${doomed ? 'bg-amber-50' : ''}">
            <td class="px-2 py-1 font-mono text-xs">${_psEsc(s.snapshot_id)}</td>
            <td class="px-2 py-1 text-xs text-gray-600">${_psEsc(s.created_at || '')}</td>
            <td class="px-2 py-1 text-xs text-gray-500">${_psEsc(s.source || '')}</td>
            <td class="px-2 py-1">${badge}${doomed ? ' <span class="text-xs text-amber-800">deleted by the next backup</span>' : ''}</td>
            <td class="px-2 py-1 text-right">
                <button data-ps-act="rehearse" data-ps-id="${id}" data-ps-snap="${_psEsc(s.snapshot_id)}"
                    class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                    title="Restore into a scratch volume to prove it works. The live stack is not touched.">Rehearse restore</button>
            </td>
        </tr>`;
    }).join('');

    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="space-y-3 text-sm">
            <div class="flex flex-wrap items-center gap-2">
                <span class="font-medium text-gray-800">Schedule:</span>
                <span class="text-gray-700">${_psEsc(sch.note || '')}</span>
                <input type="number" min="0" id="ps-sched-${id}" aria-label="Schedule interval in hours" value="${_psEsc(sch.interval_hours ?? '')}"
                    placeholder="hours" class="w-24 px-2 py-1 text-xs border border-gray-300 rounded" />
                <button data-ps-act="set-schedule" data-ps-id="${id}"
                    class="px-2 py-1 text-xs bg-primary text-white rounded hover:bg-primary/90">Save</button>
                <button data-ps-act="clear-schedule" data-ps-id="${id}"
                    class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">No schedule</button>
            </div>
            ${d.last_error ? `<div class="text-red-700 text-xs">Last scheduled backup failed
                (${_psEsc(d.last_error_at || '')}): ${_psEsc(d.last_error)}</div>` : ''}
            ${d.recoverability_warning ? `<div class="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                ${_psEsc(d.recoverability_warning)}</div>` : ''}
            <div class="text-xs text-gray-700">
                Keeping the newest ${_psEsc(String(r.retention_count ?? ''))} snapshot(s).
                ${r.snapshot_count ? `${_psEsc(String(r.snapshot_count))} stored, covering
                    ${_psEsc(String(r.covers_days ?? 0))} day(s) back to ${_psEsc(r.oldest_created_at || '')}.` : ''}
                ${r.note ? `<div class="mt-1 ${r.at_risk || r.last_snapshot_protected ? 'text-amber-800' : 'text-gray-600'}">${_psEsc(r.note)}</div>` : ''}
            </div>
            ${d.weight_cache_note ? `<div class="text-xs text-gray-600">${_psEsc(d.weight_cache_note)}</div>` : ''}
            ${snapRows ? `<table class="min-w-full"><tbody>${snapRows}</tbody></table>`
                       : '<div class="text-xs text-gray-500">No snapshots stored.</div>'}
        </div>
    </td>`;
}

async function _psRehearse(id, snapshotId) {
    try {
        const r = await apiCall(
            `/stacks/${id}/rehearse?snapshot_id=${encodeURIComponent(snapshotId)}`,
            { method: 'POST' });
        _psMsg(r.note || `Rehearsal of ${snapshotId} dispatched.`, true);
    } catch (e) {
        _psMsg(`Rehearsal failed to start: ${e.message || String(e)}`, false);
    }
}

async function _psSetSchedule(id, clear) {
    const input = document.getElementById(`ps-sched-${id}`);
    const raw = input ? input.value.trim() : '';
    const body = clear || raw === ''
        ? { clear: true }
        : { interval_hours: parseInt(raw, 10) };
    try {
        const r = await apiCall(`/stacks/${id}/backup-schedule`,
            { method: 'PUT', body: JSON.stringify(body) });
        _psMsg(r.note || 'Schedule updated.', true);
        await _psLoadStacks();
    } catch (e) {
        _psMsg(`Could not set the schedule: ${e.message || String(e)}`, false);
    }
}

function _psRestore(id, refs) {
    const snaps = Object.keys((refs && refs.snapshots) || {});
    if (!snaps.length) { _psMsg('No snapshots for this stack — back it up first.', false); return; }
    _psAct(id, 'restore', `?snapshot_id=${encodeURIComponent(snaps[snaps.length - 1])}`);
}

// §9.2 — the at-a-glance answer to "which of my stacks is unprotected".
//
// "No schedule" is styled as a warning, not as neutral information. It is the
// default state of every stack that has ever existed here, which is exactly why
// it needs to be conspicuous: a quiet grey dash reads as "fine" and is the
// reason nobody notices until the restore.
//
// A last error outranks the schedule. A stack scheduled every 6h whose last
// four backups failed is worse off than one with no schedule at all, because it
// looks protected.
function _psBackupBadge(s) {
    if (s.backup_last_error) {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700"
                      title="${_psEsc(s.backup_last_error)}">last backup failed</span>`;
    }
    const h = s.backup_interval_hours;
    if (h === null || h === undefined) {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-800"
                      title="No backup schedule has been set. This stack is backed up only when someone remembers to ask.">no schedule</span>`;
    }
    if (h === 0) {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600"
                      title="Scheduled backups are deliberately off for this stack.">off</span>`;
    }
    const last = s.backup_last_run_at
        ? new Date(s.backup_last_run_at).toLocaleString()
        : 'never run';
    return `<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700"
                  title="Last backup: ${_psEsc(last)}">every ${_psEsc(String(h))}h</span>`;
}

const _PS_STATUS_CLASS = {
    running: 'bg-success/15 text-success', deploying: 'bg-amber-100 text-amber-700',
    migrating: 'bg-amber-100 text-amber-700', torn_down: 'bg-gray-100 text-gray-500',
    failed: 'bg-destructive/15 text-destructive', created: 'bg-gray-100 text-gray-600',
    stopped: 'bg-gray-100 text-gray-600',
    // Not 'failed'. The deploy worked; its host stopped reporting it. Styled
    // as a warning rather than an error because the usual fix is a host, not
    // the stack, and the stack itself may be perfectly fine elsewhere.
    unreachable: 'bg-amber-100 text-amber-700',
};

// HA class decides whether MOD may re-place this stack on another host after
// its own fails. Shown because the default is the CONSERVATIVE one: an
// undeclared stack is `external` and will never be moved automatically, which
// is safe but is not what someone expects from a stack they think is
// highly available. Making it visible is what stops that being a surprise
// discovered during an outage.
// GR.FEN.3b: the badge shows the class NAME (stateless / cache_backed /
// snapshot_restore / external) — the value the operator also picks in the edit
// panel — not a paraphrase, so the row and the control always agree. "external"
// was once taken to mean the stack runs on an external machine — a reasonable
// reading, and wrong: these run on the manager's own built-in worker. The field
// is about RECOVERY, not location; the tooltip (_PS_HA_CLASS_DESC) says that.
const _PS_HA_CLASS_LABEL = {
    stateless: 'Stateless — MOD may re-place this on another host after a failure',
    cache_backed: 'Cache-backed — re-placed after a failure, then a cold start',
    snapshot_restore: 'Snapshot — re-placed after a failure, losing work since the last snapshot',
    external: 'MOD will NOT move this stack after a host failure. Nothing to do '
        + 'with where it runs — this one is on the built-in worker. It is the '
        + 'conservative default: recovery is not attempted until someone says '
        + 'what losing this stack costs.',
};
const _PS_HA_CLASS_STYLE = {
    stateless: 'bg-success/15 text-success',
    cache_backed: 'bg-success/15 text-success',
    snapshot_restore: 'bg-amber-100 text-amber-700',
    external: 'bg-gray-100 text-gray-600',
};
// GR.FEN.3b — one-line explanation per class, shown in the row badge tooltip
// and next to the select in the edit panel. Mirrors the server-side docstring
// on PUT /stacks/{id}/ha-class (the statement of what the service keeps).
const _PS_HA_CLASS_DESC = {
    stateless: 'no state; safe to restart or move anywhere',
    cache_backed: 'only model/cache data; keeps running if the manager is briefly unreachable, can be re-placed',
    snapshot_restore: 'state is snapshotted and restored when moved',
    external: 'holds its own data; stopped when the manager is unreachable, never moved',
};

// caddy-stack-ingress (workstream D) — the address surface.
//
// A stack's STABLE INTERNAL address is the mesh name (`t<tid>-<stackid>-<svc>`,
// keyed on ids, collision-proof), served in `mesh_endpoints[svc].magicdns_name`.
// It is what makes an internal-only stack usable: you wire a workflow/service to
// `t5-42-web`. The container port is not carried on the endpoint (a reachable
// service drops its published ports on the worker copy), so it is read back off
// the stored compose spec's `expose` / `ports` for the same service — the stored
// spec is left untouched at deploy, so it still has them.
function _psContainerPort(svc) {
    if (!svc || typeof svc !== 'object') return null;
    const exp = svc.expose;
    if (Array.isArray(exp) && exp.length) {
        const p = String(exp[0]).split('/')[0].trim();
        if (p) return p;
    }
    const ports = svc.ports;
    if (Array.isArray(ports) && ports.length) {
        const p0 = ports[0];
        if (p0 && typeof p0 === 'object') {
            if (p0.target != null) return String(p0.target);
        } else if (p0 != null) {
            // "host:container", "ip:host:container" or "container" (+ optional /proto)
            const parts = String(p0).split('/')[0].split(':');
            const container = parts[parts.length - 1].trim();
            if (container) return container;
        }
    }
    return null;
}

// [{svc, name, port}] for every reachable service, from `mesh_endpoints`.
function _psInternalAddrs(s) {
    const eps = s.mesh_endpoints;
    if (!eps || typeof eps !== 'object') return [];
    const services = ((s.compose_spec || {}).services) || {};
    return Object.keys(eps).map((svc) => {
        const ep = eps[svc] || {};
        return { svc, name: ep.magicdns_name || '', port: _psContainerPort(services[svc]) };
    }).filter((a) => a.name);
}

function _psCopyBtn(text, label) {
    return `<button data-ps-act="copy" data-ps-copy="${_psEsc(text)}" title="Copy ${_psEsc(label)}"
        class="text-gray-400 hover:text-gray-700 ml-1">⧉</button>`;
}

// The address block under the stack name. Order: public front door (when
// exposed), then the stable internal address(es), then legacy worker-local
// endpoint, then the honest "internal only" note for a stack with no reachable
// service and no published port.
function _psAddrCell(s) {
    const parts = [];
    if (s.exposed && s.public_hostname) {
        const url = `https://${s.public_hostname}`;
        parts.push(`<div class="text-xs font-normal mt-0.5" title="Public, TLS-terminated front door — MOD-run, wildcard-routed, follows the stack across workers">
            🌐 <a href="${_psEsc(url)}" target="_blank" rel="noopener" class="text-primary hover:underline font-medium">${_psEsc(s.public_hostname)} ↗</a>${_psCopyBtn(url, 'public URL')}</div>`);
        if (s.custom_domain) {
            const curl = `https://${s.custom_domain}`;
            parts.push(`<div class="text-xs font-normal mt-0.5" title="Your custom domain (CNAME it to the MOD FQDN)">
                🔗 <a href="${_psEsc(curl)}" target="_blank" rel="noopener" class="text-primary hover:underline">${_psEsc(s.custom_domain)} ↗</a>${_psCopyBtn(curl, 'custom domain URL')}</div>`);
        }
    }
    _psInternalAddrs(s).forEach((a) => {
        const full = a.port ? `${a.name}:${a.port}` : a.name;
        parts.push(`<div class="text-xs font-normal text-gray-500 mt-0.5 font-mono" title="Stable internal address on the mesh — wire a workflow/service to this. Collision-proof and follows the stack across workers.">${_psEsc(full)}${_psCopyBtn(full, 'internal address')}</div>`);
    });
    if (parts.length) return parts.join('');
    if (s.lb_endpoint) {
        return /^https?:\/\//.test(s.lb_endpoint)
            ? `<div class="text-xs font-normal mt-0.5"><a href="${_psEsc(s.lb_endpoint)}" target="_blank" rel="noopener" class="text-primary hover:underline">${_psEsc(s.lb_endpoint)} ↗</a></div>`
            : `<div class="text-xs font-normal text-gray-500 mt-0.5 font-mono" title="Internal address on the stack network — not published to a public URL">${_psEsc(s.lb_endpoint)}</div>`;
    }
    return `<div class="text-xs font-normal text-gray-400 mt-0.5" title="No reachable service and no published port. Mark a service x-mod.reachable to give it a stable internal address, then 'Expose…' for a public URL — use Logs to inspect it meanwhile.">internal only</div>`;
}

// The per-stack public-exposure panel, rendered into the stack's expandable row
// (the same `ps-backups-${id}` slot Pin / Backups use). Reads live state from
// `_psById`; the toggle + custom-domain controls call workstream C's
// PUT /stacks/{id}/exposure.
function _psRenderExpose(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    const s = _psById[id] || {};
    const exposed = !!s.exposed;
    const pub = s.public_hostname || '';
    const custom = s.custom_domain || '';
    const friendly = s.friendly_fqdn || '';
    const edge = s.edge_target || '';
    const addrs = _psInternalAddrs(s);

    const internalList = addrs.length
        ? addrs.map((a) => {
            const full = a.port ? `${a.name}:${a.port}` : a.name;
            return `<code class="px-1.5 py-0.5 rounded bg-white border border-gray-300 text-xs text-gray-700">${_psEsc(full)}</code>${_psCopyBtn(full, 'internal address')}`;
        }).join(' &nbsp; ')
        : '<span class="text-xs text-gray-700">No reachable service — mark one <code>x-mod.reachable</code> to get a stable internal address.</span>';

    const publicBlock = exposed ? `
        <div class="mt-3 space-y-2">
            <div class="text-xs text-gray-700">Public URL <span class="text-gray-700">(MOD-run, TLS, wildcard-routed — no edge config needed)</span>:
                ${pub
                    ? `<a href="https://${_psEsc(pub)}" target="_blank" rel="noopener" class="text-primary hover:underline font-medium">https://${_psEsc(pub)} ↗</a>${_psCopyBtn('https://' + pub, 'public URL')}`
                    : '<span class="text-gray-700">(pending)</span>'}
            </div>
            <div class="flex flex-wrap items-end gap-2">
                <div>
                    <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-expose-domain-${id}">Custom domain
                        <span class="font-normal text-gray-700">(optional — CNAME it to the MOD FQDN)</span></label>
                    <input id="ps-expose-domain-${id}" value="${_psEsc(custom)}" placeholder="api.acme.com"
                        class="px-3 py-2 text-sm border border-gray-400 rounded-lg bg-white" />
                </div>
                <button data-ps-act="expose-domain" data-ps-id="${id}"
                    class="px-3 py-2 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save domain</button>
                ${custom ? `<button data-ps-act="expose-domain-clear" data-ps-id="${id}"
                    class="px-3 py-2 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">Remove domain</button>` : ''}
            </div>
            <div class="flex flex-wrap items-end gap-2">
                <div>
                    <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-expose-friendly-${id}">Friendly FQDN
                        <span class="font-normal text-gray-700">(optional — a human-friendly public name aliased to this stack)</span></label>
                    <input id="ps-expose-friendly-${id}" value="${_psEsc(friendly)}" placeholder="app.example.com"
                        class="px-3 py-2 text-sm border border-gray-400 rounded-lg bg-white" />
                </div>
                <button data-ps-act="expose-friendly" data-ps-id="${id}"
                    class="px-3 py-2 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save FQDN</button>
                ${friendly ? `<button data-ps-act="expose-friendly-clear" data-ps-id="${id}"
                    class="px-3 py-2 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">Remove FQDN</button>` : ''}
            </div>
            ${edge ? `<div class="text-xs text-gray-700">Edge target:
                <code class="px-1.5 py-0.5 rounded bg-white border border-gray-300 text-xs text-gray-700">${_psEsc(edge)}</code>${_psCopyBtn(edge, 'edge target')}
                <div class="mt-0.5">Point your DNS (CNAME) at this, then your name will serve over HTTPS automatically.</div>
                <div class="mt-0.5 text-gray-500">MOD does not change your DNS — you create the record in your own zone.</div>
            </div>` : ''}
        </div>`
        : '<div class="mt-2 text-xs text-gray-700">Internal-only. Turn on to publish a public HTTPS URL the stack\'s own users can reach.</div>';

    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="text-sm font-semibold mb-2 text-gray-900">Public exposure — stack ${id}</div>
        <label class="flex items-center gap-2 text-xs mb-2 text-gray-700">
            <input type="checkbox" id="ps-expose-on-${id}" ${exposed ? 'checked' : ''} data-ps-act="expose-toggle" data-ps-id="${id}" />
            <span>Expose publicly ${exposed ? '<strong>(on)</strong>' : '(off)'}</span>
        </label>
        <div class="text-xs mb-1 text-gray-700">Stable internal address${addrs.length === 1 ? '' : 'es'}: ${internalList}</div>
        ${publicBlock}
        <div class="mt-3"><button data-ps-act="expose-close" data-ps-id="${id}" class="text-xs text-gray-500 hover:text-gray-800">✕ Close</button></div>
    </td>`;
}

function _psExpose(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1') { host.innerHTML = ''; host.dataset.open = ''; return; }
    host.dataset.open = '1';
    _psRenderExpose(id);
}

// Call C's exposure endpoint, refresh the list, then re-open the panel on the
// fresh state so the public URL / internal address reflect the change in place.
async function _psExposeSet(id, exposed, opts = {}) {
    const body = { exposed };
    if (exposed) {
        if (opts.clearCustom) body.clear_custom_domain = true;
        else if (opts.customDomain != null) body.custom_domain = opts.customDomain;
        // Friendly FQDN is independent of the custom domain — sending only its own
        // field leaves the other untouched (backend "omit = leave unchanged").
        if (opts.clearFriendly) body.clear_friendly_fqdn = true;
        else if (opts.friendlyFqdn != null) body.friendly_fqdn = opts.friendlyFqdn;
    }
    try {
        const updated = await apiCall(`/stacks/${id}/exposure`, {
            method: 'PUT', body: JSON.stringify(body),
        });
        if (updated && updated.id != null) _psById[updated.id] = updated;
        _psMsg(exposed
            ? `Stack ${id} exposed${updated && updated.public_hostname ? ` at ${updated.public_hostname}` : ''}.`
            : `Stack ${id} is internal-only again.`, true);
        await _psLoadStacks();
        const host = document.getElementById(`ps-backups-${id}`);
        if (host) { host.dataset.open = '1'; _psRenderExpose(id); }
    } catch (e) { _psMsg(`Exposure failed: ${e.message || e}`, false); }
}

// cUI-2b (30.6.15): the "Keep running" toggle. Server-side truth is
// PUT /stacks/{id}/keep-running {enabled: bool}; the row (and its badge) is
// re-rendered from the fresh fetch, so the checkbox never drifts from the DB.
// 
// state for the two dispatch legs of one toggle (see the wiring comments here
// and on the ps-rows `change` listener): `_psKrBusy` holds the stack ids whose
// keep-running request is in flight, so the OTHER leg of the SAME user action
// (and the checkbox revert below, which fires `change` again with the old value)
// never sends a second PUT while the first request is still running.
const _psKrBusy = new Set();
// `_psKrRevert` is the stack id whose checkbox we just flipped BACK after a
// refused PUT; that flip fires `change` again on the live node, and it must not
// send another PUT — the server already refused it.
let _psKrRevert = null;
async function _psKeepRunningSet(id, enabled) {
    const on = !!enabled;
    // The click handler passes the raw dataset string, the `change` listener a
    // number: normalise once so the busy-set and the key compare correctly.
    const sid = Number(id);
    //  (lane
    // ): the checkbox lives inside the row's More panel, so the
    // toggle can arrive on EITHER leg (the ps-rows click handler, or the input's
    // own `change` event — see the wiring comment on the ps-rows `change`
    // listener). One request per (stack, value) at a time: whichever leg arrives
    // first wins, the other is a no-op, and the checkbox revert below (a refused
    // PUT) cannot re-enter while this request is still in flight.
    if (_psKrBusy.has(sid)) return;
    _psKrBusy.add(sid);
    try {
        await apiCall(`/stacks/${sid}/keep-running`, {
            method: 'PUT', body: JSON.stringify({ enabled: on }),
        });
        _psMsg(on
            ? `Stack ${id} is pinned running — it will not be put to sleep automatically.`
            : `Stack ${id} can be put to sleep automatically again.`, true);
        await _psLoadStacks();
    } catch (e) {
        // Revert the checkbox so the UI never claims a state the server refused.
        const cb = document.querySelector(`[data-ps-act="keep-running-toggle"][data-ps-id="${id}"]`);
        if (cb) { _psKrRevert = sid; cb.checked = !on; }
        if (e && e.status === 403) {
            _psMsg(`Only the stack's owners can change this.`, false);
            return;
        }
        const d = e && e.detail;
        const detail = (d && typeof d === 'object')
            ? (d.message || d.detail || '')
            : (typeof d === 'string' ? d : '');
        _psMsg(detail || `Could not change Keep running: ${e.message || String(e)}`, false);
    } finally {
        _psKrBusy.delete(sid);
    }
}

// gw-stack-networking GW.4 — the per-row Endpoints view: mesh names + ports,
// the public URL when exposed (front-door service/port + CNAME target), and
// host-port requests (fixed vs `any`). Host ports are editable here — the
// fixed/`any` choice is the whole question GW.1 left to the operator. Data
// comes from GET /stacks/{id}/networking; saves go to PUT /stacks/{id}/host-ports.
// Same expandable-row idiom as Expose/Pin/Backups (one shared ps-backups-<id>
// slot), so the panel follows its row through list refreshes. The fetched
// networking payload is kept per stack (mirrors _psLastLogPayload) so the
// "add host port" preselect can reuse the service names without a refetch.
const _psEndpointsPayload = {};
// Last host-ports save/error line per stack. The networking panel is rebuilt
// (fresh empty #ps-hp-out-<id>) after every save, so the line is replayed at the
// end of each render and kept until the next edit or a fresh panel open.
const _psHpNotice = {};
function _psEndpoints(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'endpoints') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'endpoints';
    delete _psHpNotice[id];
    host.innerHTML = '<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-gray-500">Loading endpoints…</td>';
    _psRenderEndpoints(id);
}

function _psHostPortEntry(id, i, e) {
    const protoSel = ['tcp', 'udp'].map(p =>
        `<option value="${p}"${(e.protocol || 'tcp') === p ? ' selected' : ''}>${p}</option>`).join('');
    const legacy = e.legacy
        ? `<div class="text-xs text-amber-700">Legacy (pre-MOD-owned ports) — re-import or edit the definition to manage it here.</div>`
        : '';
    const alloc = (e.allocation && (e.allocation.host_port != null || e.allocation.allocated != null))
        ? e.allocation
        : null;
    const allocText = alloc
        ? `→ ${_psEsc(alloc.host_port != null ? alloc.host_port : alloc.allocated)} on the worker`
        : (e.pending ? '→ pending (worker will pick a free port)' : '');
    return `<div class="flex flex-wrap items-end gap-2 rounded border border-gray-300 bg-white p-2 text-gray-700">
        <div>
            <label class="block text-[11px] font-semibold text-gray-700">service</label>
            <input data-ps-hp-field="service" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port service name" value="${_psEsc(e.service || '')}"
                class="w-32 px-2 py-1 text-xs border border-gray-400 rounded bg-white" />
        </div>
        <div>
            <label class="block text-[11px] font-semibold text-gray-700">container port</label>
            <input data-ps-hp-field="target" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port container port" type="number" min="1" max="65535" value="${_psEsc(e.target ?? '')}"
                class="w-24 px-2 py-1 text-xs border border-gray-400 rounded bg-white" />
        </div>
        <div>
            <label class="block text-[11px] font-semibold text-gray-700">host port</label>
            <select data-ps-hp-field="host-mode" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port host binding mode"
                class="px-2 py-1 text-xs border border-gray-400 rounded bg-white">
                <option value="any"${e.host === 'any' ? ' selected' : ''}>any (worker picks)</option>
                <option value="fixed"${e.host !== 'any' ? ' selected' : ''}>fixed…</option>
            </select>
        </div>
        <div data-ps-hp-fixedwrap="${i}">
            <label class="block text-[11px] font-semibold text-gray-700">port</label>
            <input data-ps-hp-field="host-port" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port host port" type="number" min="1" max="65535" value="${_psEsc((e.host !== 'any' && e.host != null) ? e.host : '')}"
                class="w-24 px-2 py-1 text-xs border border-gray-400 rounded bg-white" />
        </div>
        <div>
            <label class="block text-[11px] font-semibold text-gray-700">proto</label>
            <select data-ps-hp-field="protocol" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port protocol"
                class="px-2 py-1 text-xs border border-gray-400 rounded bg-white">${protoSel}</select>
        </div>
        <div>
            <label class="block text-[11px] font-semibold text-gray-700">bind (opt.)</label>
            <input data-ps-hp-field="bind" data-ps-hp-idx="${i}" data-ps-hp-id="${id}" aria-label="Host-port bind address" value="${_psEsc(e.bind || '')}" placeholder="0.0.0.0"
                class="w-24 px-2 py-1 text-xs border border-gray-400 rounded bg-white" />
        </div>
        <button data-ps-act="hp-remove" data-ps-id="${id}" data-ps-hp-idx="${i}"
            class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Remove this host-port request">Remove</button>
        ${allocText ? `<div class="w-full text-xs text-success">${allocText}</div>` : ''}
        ${legacy}
    </div>`;
}

async function _psRenderEndpoints(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host || host.dataset.openAct !== 'endpoints') return;
    let d;
    try {
        d = await apiCall(`/stacks/${id}/networking`);
    } catch (e) {
        host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-destructive">
            Could not load endpoints: ${_psEsc(e.message || String(e))}</td>`;
        return;
    }
    _psEndpointsPayload[id] = d;
    const s = _psById[id] || {};
    const meshRows = (d.mesh || []).map((m) =>
        `<div class="font-mono text-xs text-gray-700" title="Stable internal address on the mesh">
            ${_psEsc(m.full || m.name || '')}${_psCopyBtn(m.full || m.name || '', 'internal address')}</div>`).join('');
    const pub = d.public;
    const pubBlock = pub
        ? `<div class="space-y-1 text-xs text-gray-700">
            <div>🌐 <a href="https://${_psEsc(pub.public_hostname || '')}" target="_blank" rel="noopener"
                class="text-primary hover:underline font-medium">${_psEsc(pub.public_hostname || '')} ↗</a>${_psCopyBtn('https://' + (pub.public_hostname || ''), 'public URL')}
                <span class="text-gray-500">front door: ${_psEsc((pub.ingress && pub.ingress.service) || '?')}:${_psEsc(String((pub.ingress && pub.ingress.port) || '?'))}</span></div>
            ${pub.custom_domain ? `<div>🔗 ${_psEsc(pub.custom_domain)}</div>` : ''}
            ${pub.friendly_fqdn ? `<div>🏷 ${_psEsc(pub.friendly_fqdn)}</div>` : ''}
            ${pub.edge_target ? `<div class="text-gray-600">CNAME your domain at <code class="px-1 py-0.5 rounded bg-white border border-gray-300">${_psEsc(pub.edge_target)}</code>${_psCopyBtn(pub.edge_target, 'edge target')}
                <span class="text-gray-500">(MOD does not change your DNS — you create the record in your own zone.)</span></div>` : ''}
            </div>`
        : '<div class="text-xs text-gray-700">Not exposed — use Expose… for a public front door.</div>';
    const hp = d.host_ports || [];
    const hpBlock = hp.length
        ? hp.map((e, i) => _psHostPortEntry(id, i, e)).join('')
        : '<div class="text-xs text-gray-700">No host ports requested — every service is mesh-reachable only.</div>';
    const subnets = (d.subnets || []).map((n) =>
        `<div class="text-xs text-amber-700">Pinned subnet: <code>${_psEsc(n.network)} = ${_psEsc(n.subnet)}</code> (legacy — MOD now allocates subnets; re-import to drop it)</div>`).join('');
    // GW.4c — the ports the worker actually bound for `any` requests
    // (storage_refs.host_port_mappings, reported by the worker on deploy).
    // One read-only row per allocation; a host port is only meaningful on the
    // worker it landed on, so the row carries the stack's target worker.
    const workerHost = d.target_worker || '?';
    const allocRows = (d.allocated_host_ports || []).map((a) =>
        `<div class="font-mono text-xs text-gray-700" data-ps-alloc-row
            title="Host port the worker picked for this 'any' request">
            ${_psEsc(a.service || '?')}: <code class="px-1 py-0.5 rounded bg-white border border-gray-300">${_psEsc(workerHost)}:${_psEsc(String(a.host))}</code>
            → container ${_psEsc(a.target != null ? String(a.target) : '?')}/${_psEsc(a.protocol || 'tcp')}
            <span class="text-gray-500">auto-assigned</span></div>`).join('');
    const allocBlock = allocRows
        ? `<div class="text-xs font-semibold mt-3 mb-1 text-gray-900">Worker-allocated host ports</div>
            <div class="space-y-1">${allocRows}</div>`
        : '';
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="text-sm font-semibold mb-2 text-gray-900">Endpoints — stack ${id}</div>
        <div class="text-xs font-semibold mb-1 text-gray-900">Internal (mesh)</div>
        ${meshRows || '<div class="text-xs text-gray-700">No reachable service — mark one x-mod.reachable.</div>'}
        <div class="text-xs font-semibold mt-3 mb-1 text-gray-900">Public front door</div>
        ${pubBlock}
        <div class="flex items-center gap-2 mt-3">
            <div class="text-xs font-semibold text-gray-900">Host ports</div>
            ${(!s.exposed) ? '' : ''}
            <button data-ps-act="hp-add" data-ps-id="${id}"
                class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200"
                title="Request a host publish for a service (fixed port, or 'any' for the worker to pick)">+ Add host port</button>
        </div>
        <div class="mt-2 space-y-2">${hpBlock}</div>
        ${allocBlock}
        ${subnets}
        <div class="flex flex-wrap items-center gap-2 mt-3">
            <button data-ps-act="hp-save" data-ps-id="${id}"
                class="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90" data-testid="ps-hostports-save-btn">Save host ports</button>
            <button data-ps-act="endpoints-close" data-ps-id="${id}"
                class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200">Close</button>
            <div class="text-xs text-gray-500">Takes effect on the next Deploy.</div>
        </div>
        <div id="ps-hp-out-${id}" class="text-xs mt-2"></div>
    </td>`;
    host.querySelectorAll('[data-ps-hp-field="host-mode"]').forEach((sel) => {
        sel.addEventListener('change', () => {
            const w = host.querySelector(`[data-ps-hp-fixedwrap="${sel.dataset.psHpIdx}"]`);
            if (w) w.style.display = sel.value === 'fixed' ? '' : 'none';
        });
    });
    host.querySelectorAll('[data-ps-hp-field="host-mode"][value]')
        .forEach((sel) => {
            const w = host.querySelector(`[data-ps-hp-fixedwrap="${sel.dataset.psHpIdx}"]`);
            if (w && sel.value !== 'fixed') w.style.display = 'none';
        });
    const kept = _psHpNotice[id];
    if (kept) _psHpOut(id, kept.text, kept.ok);
}

function _psHpCollect(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return null;
    const out = [];
    host.querySelectorAll('[data-ps-hp-field="service"]').forEach((svcEl) => {
        const i = svcEl.dataset.psHpIdx;
        const f = (name) => host.querySelector(`[data-ps-hp-field="${name}"][data-ps-hp-idx="${i}"]`);
        const mode = f('host-mode').value;
        const entry = {
            service: f('service').value.trim(),
            target: parseInt(f('target').value, 10),
            host: mode === 'any' ? 'any' : parseInt(f('host-port').value, 10),
            protocol: f('protocol').value,
        };
        const bind = f('bind').value.trim();
        if (bind) entry.bind = bind;
        out.push(entry);
    });
    return out;
}

function _psHpOut(id, text, ok) {
    const el = document.getElementById(`ps-hp-out-${id}`);
    _psHpNotice[id] = { text, ok };
    if (el) { el.textContent = text; el.className = 'text-xs mt-2 ' + (ok ? 'text-success' : 'text-destructive'); }
}

async function _psHostPortsSave(id) {
    const entries = _psHpCollect(id);
    if (entries == null) return;
    _psHpOut(id, 'Saving…', true);
    try {
        await apiCall(`/stacks/${id}/host-ports`, {
            method: 'PUT', body: JSON.stringify({ host_ports: entries }),
        });
        _psHpOut(id, 'Saved as a new revision. The running stack is unchanged until you Deploy.', true);
        await _psLoadStacks();
        const host = document.getElementById(`ps-backups-${id}`);
        if (host) { host.dataset.open = '1'; host.dataset.openAct = 'endpoints'; await _psRenderEndpoints(id); }
        // The render above replaced the panel (empty #ps-hp-out); draw the line again.
        _psHpOut(id, 'Saved as a new revision. The running stack is unchanged until you Deploy.', true);
    } catch (e) {
        const d = (e && e.detail) || {};
        let msg;
        if (e && e.status === 409) msg = (typeof d === 'string' && d) ? d : 'This stack predates MOD-owned host ports — re-import it first.';
        else if (e && e.status === 422) msg = (typeof d === 'string' && d) ? d : `Invalid host port: ${(e && e.message) || String(e)}`;
        else msg = `Save failed: ${(e && e.message) || String(e)}`;
        _psHpOut(id, msg, false);
    }
}

function _psHostPortsAdd(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    delete _psHpNotice[id];
    // Service names from the cached networking payload (reachable mesh
    // services ∪ already-requested host-port services) — the list response
    // carries no compose_spec, and a service not in this set cannot be
    // mesh-reached or port-mapped usefully anyway.
    const d = _psEndpointsPayload[id] || {};
    const services = [...new Set([
        ...((d.mesh || []).map((m) => m.service)),
        ...((d.host_ports || []).map((e) => e.service)),
    ].filter(Boolean))];
    const idx = host.querySelectorAll('[data-ps-hp-field="service"]').length;
    const frag = document.createElement('template');
    frag.innerHTML = _psHostPortEntry(id, idx, {
        service: services[0] || '', target: null, host: 'any', protocol: 'tcp', bind: null,
        pending: false, legacy: false,
    });
    const wrap = host.querySelector('.mt-2.space-y-2') || host;
    const emptyNote = wrap.querySelector('div.text-xs');
    if (emptyNote && !wrap.querySelector('[data-ps-hp-field]')) wrap.innerHTML = '';
    wrap.appendChild(frag.content.firstElementChild);
}

function _psHostPortsRemove(id, idx) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    delete _psHpNotice[id];
    host.querySelector(`[data-ps-hp-field="service"][data-ps-hp-idx="${idx}"]`)
        ?.closest('div.rounded')?.remove();
}

// stack-definition-edit — everyone-facing stack composition+config editor.
// Same expandable-row idiom as Pin/Backups/Expose above (one shared
// `ps-backups-${id}` slot toggled open/closed) rather than a new page/dialog —
// this IS the existing "list your stacks" surface, so "open an existing stack
// to edit it" belongs right here. Gated server-side by RBAC (stack:author +
// tenant ownership) + the baseline `stack_edit` capability — free in every
// edition, NOT the paid `stack_wizard` capability (402 surfaces as a plain
// message, same as any other gated action on this page, but in practice
// stack_edit is always granted).
let _psDefState = {};

function _psDefRevisionOptions(id) {
    const st = _psDefState[id] || {};
    const revs = st.revisions || [];
    return revs.slice().reverse().map((r) => {
        const revNo = Number(r.revision_no);
        return `<option value="${revNo}">
        rev ${revNo} · ${_psEsc(r.origin)}${r.message ? ' — ' + _psEsc(r.message) : ''}
    </option>`;
    }).join('');
}

// GR.FEN.3b — HA class control in the stack edit panel. The class is a
// statement of what the stack keeps (see the server docstring on the endpoint),
// and it decides what the worker does on manager loss: only `stateless` and
// `cache_backed` are ever auto-re-placed. Save goes to the audited
// PUT /stacks/{id}/ha-class (ha_class as a query param — that endpoint takes
// no body) and needs a redeploy to reach the containers, hence the note.
function _psHaClassControl(id, current) {
    const desc = _PS_HA_CLASS_DESC[current] || '';
    const options = Object.keys(_PS_HA_CLASS_DESC).map((k) =>
        `<option value="${k}"${k === current ? ' selected' : ''}>${k} — ${_PS_HA_CLASS_DESC[k]}</option>`).join('');
    return `<div class="mb-3">
        <div class="flex flex-wrap items-end gap-2">
            <div>
                <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-ha-select-${id}">HA class (what the stack keeps / how MOD recovers it)</label>
                <select id="ps-ha-select-${id}" data-ps-ha-select="${id}" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg bg-white text-gray-800" data-testid="ps-ha-select">
                    ${options}
                </select>
            </div>
            <button data-ps-act="ha-save" data-ps-id="${id}" class="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90" data-testid="ps-ha-save-btn">Save HA class</button>
            <span class="text-xs text-gray-500">Takes effect on next redeploy.</span>
        </div>
        <div class="text-xs text-gray-500 mt-1" id="ps-ha-desc-${id}" data-ha-desc="${_psEsc(current)}">${_psEsc(desc)}</div>
        <div id="ps-ha-out-${id}" class="text-xs mt-1"></div>
    </div>`;
}

function _psHaOut(id, text, ok) {
    const el = document.getElementById(`ps-ha-out-${id}`);
    if (el) { el.textContent = text; el.className = 'text-xs mt-1 ' + (ok ? 'text-success' : 'text-destructive'); }
}

function _psHaDescFollow(id) {
    const sel = document.getElementById(`ps-ha-select-${id}`);
    const d = document.getElementById(`ps-ha-desc-${id}`);
    if (!sel || !d) return;
    const k = sel.value;
    d.textContent = _PS_HA_CLASS_DESC[k] || '';
}

async function _psHaSave(id) {
    const sel = document.getElementById(`ps-ha-select-${id}`);
    if (!sel) return;
    const val = sel.value;
    _psHaOut(id, 'Saving…', true);
    try {
        await apiCall(`/stacks/${id}/ha-class?ha_class=${encodeURIComponent(val)}`, { method: 'PUT' });
        const okMsg = `HA class set to ${val}. Takes effect on next redeploy.`;
        if (_psDefState[id]) _psDefState[id].ha_class = val;
        await _psLoadStacks();
        // _psLoadStacks rewrites the tbody and drops the panel we're in —
        // re-render it in place (same idiom as _psDefSave) so the operator
        // still sees the panel with the updated class pre-selected, then
        // re-state the confirmation on the fresh panel.
        await _psEditDefinition_refresh(id);
        _psHaOut(id, okMsg, true);
    } catch (e) {
        const d = (e && e.detail);
        let msg;
        if (e && e.status === 403) msg = 'Setting the HA class requires platform-admin permission.';
        else if (e && e.status === 422) msg = typeof d === 'string' && d ? d : `Invalid HA class: ${(e && e.message) || String(e)}`;
        else msg = `Save failed: ${(e && e.message) || String(e)}`;
        _psHaOut(id, msg, false);
    }
}

// GX.S10c — per-stack variables panels for Edit definition. The panel host is
// re-created on every panel render, so the ctrl is keyed by stack id and
// remounted (its state is transient — the compose text is the source of truth
// and detect re-derives the rows).
const _psDefVarsCtrls = {};

function _psMountDefVariables(id) {
    const host = document.getElementById(`ps-def-variables-${id}`);
    const ta = document.getElementById(`ps-def-compose-${id}`);
    if (!host || !ta) return;
    const prev = _psDefVarsCtrls[id];
    if (prev && prev.host === host) return;
    if (prev) { try { prev.destroy(); } catch (_) {} }
    _psDefVarsCtrls[id] = mountVariablesPanel(host, () => ta.value || '', {
        onStateChange: () => _psSyncDefSaveBtns(id),
    });
    _psSyncDefSaveBtns(id);
}

function _psSyncDefSaveBtns(id) {
    const ctrl = _psDefVarsCtrls[id];
    const complete = !ctrl || ctrl.isComplete();
    for (const act of ['def-save', 'def-save-deploy']) {
        const b = document.querySelector(`[data-ps-act="${act}"][data-ps-id="${id}"]`);
        if (b) b.disabled = !complete;
    }
}

function _psDefVarsPayload(id) {
    const ctrl = _psDefVarsCtrls[id];
    if (!ctrl || !ctrl.isComplete()) return null;
    const p = ctrl.getVariablesPayload();
    return Object.keys(p).length ? p : null;
}

function _psRenderEditDefinition(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    const st = _psDefState[id] || {};
    // compose_yaml is what the API renders the (redacted) compose_spec to;
    // fall back to JSON (valid YAML) if an older API response lacks it.
    const yamlText = st.compose_yaml != null
        ? st.compose_yaml
        : JSON.stringify(st.compose_spec || {}, null, 2);
    // Record the textarea's original (initially-rendered) value on the paired
    // main row so the auto-refresh patcher knows when the operator has actually
    // edited it and must not be clobbered by a tick.
    const mainRow = _psMainRowOf(host);
    if (mainRow) mainRow.dataset.defOrigin = yamlText;
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="text-sm font-semibold mb-2 text-gray-800">Edit definition — Stack ${id}</div>
        <div class="text-xs mb-3 text-gray-600">Composition + config, as the compose project MOD stores. Saving
            appends a <strong>new revision</strong> — nothing is overwritten, and you can revert to any prior one. This
            does not redeploy on its own; use "Save &amp; redeploy" or Deploy afterward.</div>
        <div class="flex flex-wrap items-end gap-2 mb-2">
            <div>
                <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-def-revs-${id}">Revision history</label>
                <select id="ps-def-revs-${id}" class="px-2 py-1.5 text-xs border border-gray-300 rounded-lg bg-white text-gray-800">
                    ${_psDefRevisionOptions(id) || '<option value="">(none yet)</option>'}
                </select>
                <button data-ps-act="def-diff" data-ps-id="${id}" class="px-2 py-1.5 text-xs bg-gray-100 text-gray-700 rounded hover:bg-card">Diff vs current</button>
                <button data-ps-act="def-revert" data-ps-id="${id}" class="px-2 py-1.5 text-xs bg-gray-100 text-gray-700 rounded hover:bg-card" title="Append a new revision equal to the selected one">Revert to this</button>
        </div>
        <div id="ps-def-diff-${id}"></div>
        ${_psHaClassControl(id, st.ha_class || 'external')}
        <label class="block text-xs font-semibold mb-1 text-gray-700">docker-compose (YAML — JSON also accepted) — secret-looking env values show as ***redacted***; leaving them untouched keeps the real value</label>
        <textarea id="ps-def-compose-${id}" aria-label="docker-compose YAML" rows="14" spellcheck="false"
            class="w-full px-2 py-2 text-xs font-mono border border-gray-300 rounded-lg bg-white text-gray-800"
           >${_psEsc(yamlText)}</textarea>
        <div id="ps-def-variables-${id}" data-testid="ps-def-variables-${_psEsc(id)}"></div>
        <div id="ps-def-secrets-${id}" data-testid="ps-secrets-section-edit"></div>
        <div id="ps-def-render-preview-${id}" data-testid="ps-render-preview-panel-edit"></div>
        <div class="flex flex-wrap items-center gap-2 mt-2">
            ${_psCeRenderPreviewHidden() ? '' : `<button data-ps-act="def-render-preview" data-ps-id="${id}" data-testid="ps-render-preview-btn-edit" class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-card" title="Show the compose the server would actually deploy from this YAML's x-mod.serving profile">Preview rendered</button>`}
            <input id="ps-def-msg-${id}" aria-label="Revision message" placeholder="what changed? (optional)"
                class="flex-1 min-w-48 px-2 py-1.5 text-xs border border-gray-300 rounded-lg bg-white text-gray-800" />
            <button data-ps-act="def-save" data-ps-id="${id}" class="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save as new revision</button>
            <button data-ps-act="def-save-deploy" data-ps-id="${id}" class="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save &amp; redeploy</button>
            <button data-ps-act="def-close" data-ps-id="${id}" class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-card">Close</button>
        </div>
        <div id="ps-def-out-${id}" class="text-xs mt-2"></div>
    </td>`;
    _psMountDefVariables(id);
    // 13.5d — the edit-flow Secrets section, keyed `def-<id>` off the stored
    // (redacted) compose_spec; sits directly after the variables block.
    const secHost = document.getElementById(`ps-def-secrets-${id}`);
    if (secHost) _psRenderSecretsSection(`def-${id}`, secHost, st.compose_spec || {});
}

async function _psEditDefinition(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'edit-def') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'edit-def';
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-gray-500">Loading definition…</td>`;
    try {
        const def = await apiCall(`/stacks/${id}/definition`);
        _psDefState[id] = {
            compose_spec: def.compose_spec,
            compose_yaml: def.compose_yaml,
            revisions: def.revisions || [],
            current_revision_no: def.current_revision_no,
            // The definition response is the composition; the HA class is a
            // stack-level attribute, read from the list cache (GR.FEN.3b).
            ha_class: (_psById[id] || {}).ha_class || 'external',
        };
        // FIX-CE-HIDE-RENDER-PREVIEW: refresh the cached CE verdict right before
        // this (synchronous) render so the edit-panel button gate matches the
        // edition even when the init wiring never ran (deep link / remount).
        await _psRefreshCeEditionCache();
        _psRenderEditDefinition(id);
    } catch (e) {
        const msg = /entitlement_required|402/.test(String(e.message || e))
            ? "Editing a stack's composition isn't available on this install."
            : `Could not load definition: ${e.message || String(e)}`;
        host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-destructive">${_psEsc(msg)}</td>`;
    }
}

// GX.S10c — "Secrets & vars" panel for a stack: the list from GET
// /stacks/{id}/variables plus Set/Rotate, mounted in the same expandable-row
// slot as the other panels. Re-deploying reuses the plain /stacks/{id}/deploy
// POST the Deploy button already uses.
async function _psSecrets(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'secrets') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'secrets';
    const tab = document.createElement('div');
    tab.id = `ps-secrets-tab-${id}`;
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-semibold text-gray-800">Secrets &amp; variables — Stack ${_psEsc(id)}</div>
            <button data-ps-act="secrets-close" data-ps-id="${id}" class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-card">Close</button>
        </div>
        <div class="text-xs mb-2 text-gray-600">Values are stored in Vault and never shown again. Rotating a secret marks the stack for redeploy.</div>
        ${tab.outerHTML}
    </td>`;
    mountSecretsTab(document.getElementById(`ps-secrets-tab-${id}`), id, {
        onDeployed: _psLoadStacks,
    });
}


// ============================================================================
// 2400GX-cUI-5 (30.6.15 / 30.6.4) — "Seed data": seed a NEW stack from an
// existing pg_dump custom-format dump / volume archive, from the browser,
// through the c1 seed routes (routers/persistent_stacks/stack_seed.py:66
// register 202 / :82 parts window / :100 commit; logic services/stack_seed.py:180
// register_seed / :257 part_urls / :289 commit_seed). Render pattern copied
// from the Secrets section's open/close + render helpers — the same
// expandable-row idiom into the shared ps-backups-<id> slot, actions dispatched
// by the click handler at :3624.
//
// COMMIT IS SERVER-MODE (cUI-5a, sha 5335803eb): the body is
// {"parts": {<label>: "server"}} for every label register returned, and the
// server lists the chunks of the upload IT minted with S3 ListParts
// (services/stack_seed.py:90 _server_listed_parts). The browser therefore NEVER
// reads the PUT's ETag response header — versitygw v1.7.0 only ships
// --cors-allow-origin (no --cors-expose-headers), so Access-Control-Expose-Headers
// cannot be relied on.
//
// Presigned URLs (api.js getStackSeedPartUrls -> services/stack_seed.py:275-285,
// utils.storage.get_presigned_client bound to the EXTERNAL endpoint) live ONLY
// in local variables inside _psSeedUpload: never module state, never
// localStorage/sessionStorage, never console.*, never the DOM, never in an error
// message. The PUT itself is api.js putSeedPart — a bare fetch with NO headers,
// so no preflight (upload_part signs only uploadId + partNumber,
// services/stack_seed.py:280-284) and no Authorization to the storage host.
//
// Limits come from the platform-settings registry (services/stack_seed.py:69
// SETTINGS): stack.seed.part_bytes = 64 MB chunks, max_part_bytes = 64 GB per
// part, max_total_bytes = 128 GB total, <=16 parts (_MAX_SEED_PARTS :43). The
// server re-checks every one of them at register AND commit (S5); the client-side
// pre-checks below only save a wasted upload.
//
// Gate: identical to Backups/Secrets — the whole stacks page (and
// every row action button) renders only after the platform-admin check at
// :4052 (`me.get()` -> `u.is_platform_admin !== true` returns early; the same
// check the Secrets-section comment cites as :3686, which has shifted
// down this file since cUI-4 landed). No new role check invented.
// role check invented. The API itself 403s via the router-wide STACK_AUTHOR
// gate (routers/persistent_stacks/stack_seed.py:4-7).
//
// Cancel: there is NO abort/cleanup route on the seed path (cUI-5 found none in
// stack_seed.py), so parts already PUT stay with the server's upload slot; the
// lead logs orphan cleanup as a follow-up, out of scope here.
// ============================================================================

const _PS_SEED_PART_BYTES = 64 * 1024 * 1024;          // stack.seed.part_bytes
const _PS_SEED_MAX_PART_BYTES = 64 * 1024 ** 3;        // stack.seed.max_part_bytes
const _PS_SEED_MAX_TOTAL_BYTES = 128 * 1024 ** 3;      // stack.seed.max_total_bytes
const _PS_SEED_PGDMP = [80, 71, 68, 77, 80];           // _MAGIC_PGDMP (stack_seed.py:63)
const _PS_SEED_GZIP = [31, 139];                       // _MAGIC_TARGZ (stack_seed.py:64)

let _psSeedCancel = false;   // Cancel button -> stop before the NEXT PUT

function _psSeedSlot(id) {
    return document.getElementById(`ps-backups-${id}`);
}

// ps-backups-<id> is a <tr> (every panel writes ONE <td colspan=8> into it —
// _psBackups:1527, _psRenderEditDefinition:2257), so the result line goes into
// the stack's MAIN row, not the panel row, and survives the Backups refresh that
// replaces the slot's own <td>. FIX-DASH-PS-SEED-RESULT-CELL-MAIN-ROW-D8: the
// main row is resolved by the stack id the slot's own id carries (ps-backups-<id>
// -> the data-ps-main / data-stack-id tag _psStackRow:3302 stamps), NOT by
// previousElementSibling — that sibling walk is null when the panel row is the
// tbody's first row (and can land on another stack's row after the auto-refresh
// patcher at :3430 re-orders pairs), so the ps-seed-result span was never
// created and the seed result line stayed invisible in the UI.
// : the ONE shared
// locator for the MAIN row paired with a panel row (ps-backups-<id>). The
// sibling walk it replaces is host.parentElement.previousElementSibling = the
// TBODY's previous sibling (a <thead>, or null when the panel row is first),
// never the main row, and it lands on another stack's row once the auto-refresh
// patcher re-orders pairs. The main row carries the stack id in its own tag
// (_psStackRow:3302 stamps data-ps-main="<sid>" / data-stack-id="<id>"), so
// resolve by that id, scoped to the panel row's own tbody. Both _psRenderEditDefinition
// (defOrigin) and _psSeedResultCell (ps-seed-result span) call this helper.
function _psMainRowOf(host) {
    const panelId = (host && host.id && host.id.indexOf('ps-backups-') === 0)
        ? host.id.slice('ps-backups-'.length) : null;
    if (!panelId) return null;
    const tbody = host.parentElement;
    if (!tbody) return null;
    return tbody.querySelector(`tr[data-ps-main="${Number(panelId)}"]`)
        || tbody.querySelector(`tr[data-stack-id="${panelId}"]`);
}

function _psSeedResultCell(host) {
    const mainRow = _psMainRowOf(host);
    if (!mainRow) return null;
    const existing = mainRow.querySelector('[data-testid="ps-seed-result"]');
    if (existing) return existing;
    const cell = mainRow.querySelector('td');
    if (!cell) return null;
    const span = document.createElement('span');
    span.setAttribute('data-testid', 'ps-seed-result');
    cell.appendChild(span);
    return span;
}

function _psSeedResultText(host, text) {
    const el = _psSeedResultCell(host);
    if (el) el.textContent = text;   // textContent, never innerHTML — server text is inert
}

function _psSeedText(host, testid, text) {
    const el = host && host.querySelector(`[data-testid="${testid}"]`);
    if (el) el.textContent = text;   // textContent, never innerHTML — server text is inert
}

function _psSeedPick(host, attr) {
    const input = host && host.querySelector(`[${attr}]`);
    const files = input ? input.files : null;
    return (files && files.length) ? files[0] : null;
}

async function _psSeedHead(file, n) {
    const buf = await file.slice(0, n).arrayBuffer();
    return Array.from(new Uint8Array(buf));
}

function _psSeedHeadOk(head, want) {
    if (!head || head.length < want.length) return false;
    for (let i = 0; i < want.length; i += 1) { if (head[i] !== want[i]) return false; }
    return true;
}

function _psSeedSize(bytes) {
    const b = Number(bytes) || 0;
    if (b >= 1024 ** 3) return `${(b / (1024 ** 3)).toFixed(2)} GB`;
    if (b >= 1024 ** 2) return `${(b / (1024 ** 2)).toFixed(2)} MB`;
    if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${b} B`;
}

function _psSeedRender(id) {
    const host = _psSeedSlot(id);
    if (!host) return;
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-semibold text-gray-800">Seed data — Stack ${_psEsc(id)}</div>
            <button data-ps-act="seed-close" data-ps-id="${id}" class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-card">Close</button>
        </div>
        <div class="text-xs mb-2 text-gray-600">Seed this stack from an existing database dump or volume archive.
            Pick either file (not both empty), then press "Upload seed". The upload becomes a
            <strong>snapshot</strong> — the running stack is not touched until you Restore it.</div>
        <div data-testid="ps-seed-status" class="text-xs text-gray-700">Select a file, then press "Upload seed".</div>
        <div data-testid="ps-seed" class="text-xs text-gray-700">
            <span data-testid="ps-seed-logical">Database dump (pg_dump custom format): none selected</span>
            <span data-testid="ps-seed-volumes">Volume archive (.tar.gz): none selected</span>
        </div>
        <progress data-testid="ps-seed-progress" class="w-full">not started</progress>
        <div class="flex flex-wrap items-center gap-2 mt-2">
            <input type="file" data-ps-seed-logical aria-label="Database dump (pg_dump custom format)" class="text-xs w-full box-border border-box rounded bg-white" />
            <input type="file" data-ps-seed-volumes aria-label="Volume archive (.tar.gz)" class="text-xs w-full box-border border-box rounded bg-white" />
            <button data-ps-act="seed-upload" data-ps-id="${id}" class="px-2 py-1 text-xs bg-primary text-white rounded hover:bg-primary/90">Upload seed</button>
            <button data-ps-act="seed-cancel" data-ps-id="${id}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Cancel</button>
        </div>
    </td>`;
    const cancel = host.querySelector('[data-ps-act="seed-cancel"]');
    if (cancel) {
        cancel.addEventListener('click', () => {
            _psSeedCancel = true;   // the upload loop checks this before each next PUT
        });
    }
}

async function _psSeed(id) {
    const host = _psSeedSlot(id);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'seed') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        _psSeedCancel = false;
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'seed';
    _psSeedCancel = false;
    _psSeedRender(id);
}

function _psSeedLabelOf(kind, name) {
    // The server's own part labels (services/stack_seed.py:205 "logical:db",
    // :217 `volume:{vol}`) — the commit body must name exactly these.
    return kind === 'logical' ? 'logical:db' : `volume:${name}`;
}

async function _psSeedUpload(id) {
    const host = _psSeedSlot(id);
    if (!host) return;
    const logicalFile = _psSeedPick(host, 'data-ps-seed-logical');
    const volumeFile = _psSeedPick(host, 'data-ps-seed-volumes');
    if (!logicalFile && !volumeFile) {
        _psSeedText(host, 'ps-seed-status', 'Select a file first — a seed must declare at least one part.');
        return;
    }

    // The part NAMES come from the stack's own composition (the same
    // /stacks/{id}/definition read _psEditDefinition:2310 uses): the service
    // that declares `x-mod.backup: logical` (services/stack_seed.py:155-177
    // _logical_part_identity) and the compose-declared named volumes
    // (services/stack_seed.py:206 _stack_volumes, routers/persistent_stacks/
    // _shared.py:4964 — project-prefixed server-side, so the body names the
    // bare compose key).
    let services = {};
    let declaredVolumes = [];
    try {
        const def = await apiCall(`/stacks/${id}/definition`);
        services = (def.compose_spec || {}).services || {};
        declaredVolumes = Object.keys((def.compose_spec || {}).volumes || {});
    } catch (e) {
        _psSeedText(host, 'ps-seed-status', `Could not read this stack's composition: ${e.message || e}`);
        return;
    }
    const logicalServices = [];
    for (const svcName in services) {
        const xmod = services[svcName] && services[svcName]['x-mod'];
        if (typeof xmod === 'object' && xmod.backup === 'logical') logicalServices.push(svcName);
    }

    const body = {};
    const files = [];
    if (logicalFile) {
        if (!logicalServices.length) {
            _psSeedText(host, 'ps-seed-status',
                'No database dump: this stack declares no service with x-mod.backup: logical, so a logical part would be rejected.');
            return;
        }
        const head = await _psSeedHead(logicalFile, 5);
        if (!_psSeedHeadOk(head, _PS_SEED_PGDMP)) {
            _psSeedText(host, 'ps-seed-status',
                'Database dump rejected: the file does not start with the pg_dump custom-format magic "PGDMP". Nothing was sent.');
            return;
        }
        if (logicalFile.size > _PS_SEED_MAX_PART_BYTES) {
            _psSeedText(host, 'ps-seed-status',
                `Database dump too large: ${_psSeedSize(logicalFile.size)} exceeds the 64 GB per-part cap. Nothing was sent.`);
            return;
        }
        body.logical = { service: logicalServices[0], format: 'pg_dump_custom', size_bytes: logicalFile.size };
        files.push({ label: _psSeedLabelOf('logical', ''), file: logicalFile });
    }
    if (volumeFile) {
        const lower = String(volumeFile.name || '').toLowerCase();
        if (!(lower.endsWith('.tar.gz') || lower.endsWith('.tgz'))) {
            _psSeedText(host, 'ps-seed-status',
                'Volume archive rejected: the name must end in .tar.gz or .tgz. Nothing was sent.');
            return;
        }
        const head = await _psSeedHead(volumeFile, 2);
        if (!_psSeedHeadOk(head, _PS_SEED_GZIP)) {
            _psSeedText(host, 'ps-seed-status',
                'Volume archive rejected: the file does not start with the gzip magic 1f 8b. Nothing was sent.');
            return;
        }
        if (volumeFile.size > _PS_SEED_MAX_PART_BYTES) {
            _psSeedText(host, 'ps-seed-status',
                `Volume archive too large: ${_psSeedSize(volumeFile.size)} exceeds the 64 GB per-part cap. Nothing was sent.`);
            return;
        }
        if (declaredVolumes.length !== 1) {
            _psSeedText(host, 'ps-seed-status',
                `Volume archive rejected: this stack declares ${declaredVolumes.length} named volumes and one archive can only seed one of them. Nothing was sent.`);
            return;
        }
        body.volumes = { [declaredVolumes[0]]: { size_bytes: volumeFile.size } };
        files.push({ label: _psSeedLabelOf('volume', declaredVolumes[0]), file: volumeFile });
    }

    const totalBytes = files.reduce((a, f) => a + f.file.size, 0);
    const gb = (totalBytes / (1024 ** 3));
    const sizeText = gb >= 0.01 ? `${gb.toFixed(2)} GB` : _psSeedSize(totalBytes);
    const name = (_psById[id] || {}).name || String(id);
    // TRUTHFUL per services/stack_seed.py:289-376 commit_seed — it writes a NEW
    // snapshot record into storage_refs["snapshots"] (:366) and marks the seed
    // committed (:368); it never touches the live stack. restore_stack
    // (persistent_stacks.py:1711) is what applies it, and only when asked.
    // ui.js:2130 showConfirm(message, title) -> Promise<boolean> (Cancel/Esc =
    // false); ui.js:2243 hangs it on window, which is how billing.js:2928 and
    // library.js:437 reach it from a module that does not import ui.js.
    const seedMsg = `Seed ${name}? This uploads ${sizeText} and replaces nothing until you restore the snapshot.`;
    const confirmed = window.showConfirm
        ? await window.showConfirm(seedMsg, 'Seed stack data')
        : window.confirm(seedMsg);   // ui.js:2133's own fallback, for a page without ui.js wired
    if (!confirmed) return;

    let reg;
    try {
        reg = await registerStackSeed(id, body);
    } catch (e) {
        _psSeedText(host, 'ps-seed-status', _psSeedErr(e));
        _psSeedResultText(host, _psSeedErr(e));
        return;
    }
    const seedId = (reg && reg.seed_id) ? String(reg.seed_id) : '';
    if (!seedId) { _psSeedText(host, 'ps-seed-status', 'Register returned no seed_id.'); return; }
    const partBytes = Math.max(1, Number((reg && reg.part_bytes) || _PS_SEED_PART_BYTES));

    // Progress + upload, one part at a time, at most 2 PUTs in flight.
    _psSeedText(host, 'ps-seed-status', 'Uploading…');
    _psSeedCancel = false;
    let doneBytes = 0;
    try {
        for (const entry of files) {
            const file = entry.file;
            const n = Math.max(1, Math.ceil(file.size / partBytes));
            for (let from = 1; from <= n; from += 100) {
                // URLs in windows of <=100 (route 2; services/stack_seed.py:269
                // rejects count > 100). The window response is LOCAL only.
                const win = await getStackSeedPartUrls(id, seedId,
                    { part: entry.label, from_part: from, count: Math.min(100, n - from + 1) });
                const urls = (win && win.urls) || [];
                if (urls.length !== Math.min(100, n - from + 1)) {
                    throw new Error(`part window short: ${urls.length} url(s) for ${Math.min(100, n - from + 1)} chunk(s)`);
                }
                for (let i = 0; i < urls.length; i += 2) {
                    if (_psSeedCancel) {
                        _psSeedText(host, 'ps-seed-status', 'Cancelled; nothing was committed');
                        _psSeedResultText(host, 'Cancelled; nothing was committed');
                        _psSeedProgress(host, 0, 1, 'cancelled');
                        return;
                    }
                    const pair = urls.slice(i, i + 2);
                    const puts = pair.map((w) => {
                        const blob = file.slice((Number(w.part_number) - 1) * partBytes,
                            Number(w.part_number) * partBytes);
                        return putSeedPart(w.url, blob);   // url is a local value only
                    });
                    const results = await Promise.all(puts);
                    const at = (files.slice(0, files.indexOf(entry)).reduce((a, f) => a + f.file.size, 0))
                        + Math.min(file.size, (i + pair.length) * partBytes);
                    for (const r of results) {
                        if (!r || !r.ok) {
                            _psSeedText(host, 'ps-seed-status',
                                `Upload failed at part ${i + pair.length} of ${entry.label}. Nothing was committed.`);
                            _psSeedResultText(host,
                                `Upload failed at part ${i + pair.length} of ${entry.label}. Nothing was committed.`);
                            _psSeedProgress(host, doneBytes / (totalBytes || 1), 1, 'failed');
                            return;
                        }
                    }
                    doneBytes = Math.min(totalBytes, at);
                    _psSeedProgress(host, doneBytes / (totalBytes || 1), totalBytes,
                        `${entry.label}: part ${i + pair.length}/${n}`);
                }
            }
        }
    } catch (e) {
        _psSeedText(host, 'ps-seed-status', `Upload stopped: ${_psSeedErr(e)}`);
        return;
    }

    // SERVER-MODE commit (cUI-5a, 5335803eb): every label register returned maps
    // to the literal "server" (services/stack_seed.py:307 _SERVER_LIST -> :90
    // _server_listed_parts ListParts). No ETag is ever read in the browser.
    const parts = {};
    const uploads = (reg && reg.uploads) || [];
    uploads.forEach((u) => { parts[String(u.part)] = 'server'; });
    if (!Object.keys(parts).length) files.forEach((f) => { parts[f.label] = 'server'; });
    try {
        const res = await commitStackSeed(id, seedId, { parts });
        const snap = (res && res.snapshot_id) ? String(res.snapshot_id) : '';
        _psSeedText(host, 'ps-seed-status', snap ? `Seeded. Snapshot ${snap}` : 'Seeded.');
        _psSeedResultText(host, snap ? `Seeded. Snapshot ${snap}` : 'Seeded.');
        _psSeedProgress(host, 1, 1, 'done');
    } catch (e) {
        _psSeedText(host, 'ps-seed-status', _psSeedErr(e));
        _psSeedResultText(host, _psSeedErr(e));
        return;
    }
    // Refresh the Backups list: close this slot, then re-open Backups over it.
    host.innerHTML = '';
    host.dataset.open = '';
    host.dataset.openAct = '';
    await _psBackups(id);
}

function _psSeedProgress(host, frac, total, text) {
    if (!host) return;
    const el = host.querySelector('[data-testid="ps-seed-progress"]');
    if (!el) return;
    try { el.setAttribute('value', String(Math.max(0, Math.min(1, Number(frac))))); } catch (_) { /* non-numeric */ }
    el.textContent = text || '';
}

function _psSeedErr(e) {
    // body.code + detail as text (routers/persistent_stacks/stack_seed.py:55-60
    // _err -> HTTPException(detail={"detail": msg, "code": code}); fetchApi:221
    // keeps that dict on ApiError.detail).
    if (e && e.detail && typeof e.detail === 'object') {
        return `${e.detail.detail || ''} (${e.detail.code || e.status || ''})`;
    }
    return String((e && (e.message || e)) || 'request failed');
}

// login gate through the EXISTING GET|PUT /stacks/{id}/ingress-auth
// (persistent_stacks.py:1529-1585; validated server-side by
// app_forward_auth.validate_config:162-183). No SCIM groups fetch — /scim/v2/Groups
// is manifest-DENIED in CE, so group names are typed by hand. The PUT applies on
// the next ingress reload; a RUNNING stack also needs a redeploy for the login-gated
// host-port rebinding, which we only announce (never auto-redeploy).
async function _psLoginGate(id) {
    const host = document.getElementById(`ps-backups-${id}`);
    if (!host) return;
    if (host.dataset.open === '1' && host.dataset.openAct === 'login-gate') {
        host.innerHTML = '';
        host.dataset.open = '';
        host.dataset.openAct = '';
        return;
    }
    host.dataset.open = '1';
    host.dataset.openAct = 'login-gate';
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-gray-500">Loading login gate…</td>`;
    let cfg;
    try {
        cfg = await apiCall(`/stacks/${id}/ingress-auth`);
    } catch (e) {
        const denied = e && (e.status === 402 || e.status === 403 || e.status === 404);
        host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50 text-sm text-gray-700">${_psEsc(
            denied ? "You can't edit this stack's login gate."
                   : `Could not load the login gate: ${e && e.message || String(e)}`)}</td>`;
        return;
    }
    const requireLogin = !(cfg && cfg.require_login === false);
    const groups = (cfg && cfg.allowed_groups) || [];
    const paths = (cfg && cfg.public_paths) || [];
    host.innerHTML = `<td colspan="8" class="px-4 py-3 bg-gray-50">
        <div class="flex items-center justify-between mb-2">
            <div class="text-sm font-semibold text-gray-800">Login gate — Stack ${_psEsc(id)}</div>
            <button data-ps-act="login-gate-close" data-ps-id="${id}" class="px-3 py-1.5 text-xs bg-gray-100 text-gray-700 rounded-lg hover:bg-card">Close</button>
        </div>
        <div class="text-xs mb-2 text-gray-600">Who has to sign in before this stack's web front door answers. Saved values take effect on the next ingress reload.</div>
        <div class="flex flex-wrap items-center gap-2 mb-2">
            <label class="text-xs font-semibold text-gray-700" for="ps-lg-req-${id}">
                <input type="checkbox" id="ps-lg-req-${id}" data-ps-lg-req value="${_psEsc(id)}" ${requireLogin ? 'checked' : ''}
                    class="py-1 px-1 border border-gray-300 rounded bg-white" />
                Require login
            </label>
        </div>
        <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-lg-groups-${id}">Allowed groups (one per line; empty = any signed-in user of this tenant)</label>
        <textarea id="ps-lg-groups-${id}" data-ps-lg-groups rows="4" spellcheck="false" ${requireLogin ? '' : 'disabled'}
            class="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-lg bg-white text-gray-800"
           >${_psEsc(groups.join('\n'))}</textarea>
        <label class="block text-xs font-semibold mb-1 text-gray-700" for="ps-lg-paths-${id}">Public paths (one per line, must start with /)</label>
        <textarea id="ps-lg-paths-${id}" data-ps-lg-paths rows="4" spellcheck="false" ${requireLogin ? '' : 'disabled'}
            class="w-full px-2 py-1.5 text-xs border border-gray-300 rounded-lg bg-white text-gray-800"
           >${_psEsc(paths.join('\n'))}</textarea>
        <div class="flex flex-wrap items-center gap-2 mt-2">
            <button data-ps-act="login-gate-save" data-ps-id="${id}" class="px-3 py-1.5 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save</button>
        </div>
        <div id="ps-lg-out-${id}" class="text-xs mt-2"></div>
    </td>`;
}

function _psLoginGateLines(value) {
    return String(value == null ? '' : value).split(/[\n,]/).map((s) => s.trim()).filter((s) => s !== '');
}

function _psLoginGateOut(id, text, ok) {
    const el = document.getElementById(`ps-lg-out-${id}`);
    if (el) { el.textContent = text; el.className = 'text-xs mt-2 ' + (ok ? 'text-success' : 'text-destructive'); }
}

async function _psLoginGateSave(id) {
    const req = document.getElementById(`ps-lg-req-${id}`);
    const groupsTa = document.getElementById(`ps-lg-groups-${id}`);
    const pathsTa = document.getElementById(`ps-lg-paths-${id}`);
    if (!req || !groupsTa || !pathsTa) return;
    const requireLogin = req.checked;
    const allowedGroups = _psLoginGateLines(groupsTa.value);
    const publicPaths = _psLoginGateLines(pathsTa.value);
    for (const p of publicPaths) {
        if (!p.startsWith('/')) { _psLoginGateOut(id, `Public path "${p}" must start with /`, false); return; }
        if (p.includes('..')) { _psLoginGateOut(id, `Public path "${p}" must not contain ..`, false); return; }
        if (/\s/.test(p)) { _psLoginGateOut(id, `Public path "${p}" must not contain whitespace`, false); return; }
        if (p === '/' || p === '/*') { _psLoginGateOut(id, `Public path "${p}" would make the whole app public — use 'Require login' off instead`, false); return; }
    }
    try {
        await apiCall(`/stacks/${id}/ingress-auth`, {
            method: 'PUT',
            body: JSON.stringify({ require_login: requireLogin, allowed_groups: allowedGroups, public_paths: publicPaths }),
        });
        _psLoginGateOut(id, 'Saved. Takes effect on the next ingress reload; redeploy a running stack to apply it now.', true);
    } catch (e) {
        const denied = e && (e.status === 402 || e.status === 403 || e.status === 404);
        _psLoginGateOut(id, denied ? "You can't edit this stack's login gate."
                                  : (e && e.message) || String(e), false);
    }
}

function _psDefOut(id, text, ok) {
    const el = document.getElementById(`ps-def-out-${id}`);
    if (el) { el.textContent = text; el.className = 'text-xs mt-2 ' + (ok ? 'text-success' : 'text-destructive'); }
}

async function _psDefSave(id, { redeploy } = {}) {
    // No client-side parsing — the API is YAML-native (compose_yaml) and
    // JSON is valid YAML, so a pasted docker-compose.yml or a pasted JSON
    // compose_spec both go straight through; any parse problem comes back
    // from the API as a 422 with the reason (incl. YAML line/column).
    const ta = document.getElementById(`ps-def-compose-${id}`);
    const msgEl = document.getElementById(`ps-def-msg-${id}`);
    if (!ta) return;
    const body = { compose_yaml: ta.value };
    const msg = (msgEl && msgEl.value || '').trim();
    if (msg) body.message = msg;
    // GX.S10c — carry the resolved variables (the save buttons are disabled
    // while any detected placeholder is unresolved).
    const variables = _psDefVarsPayload(id);
    if (variables) body.variables = variables;
    // 13.5d — the edit-flow Secrets section (key `def-<id>`). Zero edits →
    // today's behaviour (compose_yaml re-normalized server-side). With edits
    // we send compose_spec (stored spec + changes) INSTEAD of compose_yaml —
    // PUT .../definition accepts exactly one of the three, and the stored spec
    // is the redacted source of truth (***redacted*** sentinels unredact
    // server-side against the current row).
    const secKey = `def-${id}`;
    const secHost = document.getElementById(`ps-def-secrets-${id}`);
    const storedSpec = (_psDefState[id] || {}).compose_spec;
    let secEdits = { changes: { env: [], files: [] }, newSecrets: [], errors: [] };
    if (secHost && storedSpec) {
        secEdits = _psCollectSecretsEdits(secKey, secHost, storedSpec);
        if (secEdits.errors.length) {
            return _psDefOut(id, 'Secrets: ' + secEdits.errors.join(' · '), false);
        }
        const nEdits = secEdits.changes.env.length + secEdits.changes.files.length;
        if (nEdits > 0) {
            for (const ns of secEdits.newSecrets) {
                try {
                    await apiCall('/secrets/', {
                        method: 'POST', body: JSON.stringify({ name: ns.name, value: ns.value }),
                    });
                } catch (e) {
                    _psClearSecretSec(secKey);
                    return _psDefOut(id, `Could not create secret "${ns.name}": ${e.message || e}`, false);
                }
            }
            secHost.querySelectorAll('input[data-ps-sec-newvalue]')
                .forEach((i) => { i.value = ''; });
            body.compose_spec = psApplySecretChanges(
                JSON.parse(JSON.stringify(storedSpec)), secEdits.changes);
            delete body.compose_yaml;
        }
    }
    try {
        await apiCall(`/stacks/${id}/definition`, { method: 'PUT', body: JSON.stringify(body) });
        if (redeploy) {
            await apiCall(`/stacks/${id}/deploy`, { method: 'POST' });
            _psDefOut(id, 'Saved as a new revision and redeployed.', true);
        } else {
            _psDefOut(id, 'Saved as a new revision. The running stack is unchanged until you redeploy.', true);
        }
        // 13.5d — clear the masked values + in-flight section state; the
        // re-rendered panel then shows the saved bindings with empty values.
        _psClearSecretSec(secKey);
        await _psLoadStacks();
        await _psEditDefinition_refresh(id);
    } catch (e) {
        if (e && e.status === 422) {
            const ctrl = _psDefVarsCtrls[id];
            if (ctrl) {
                const missing = svMissingNames(e, ctrl.knownNames());
                if (missing.length) ctrl.markMissing(missing);
            }
        }
        const text = /entitlement_required|402/.test(String(e.message || e))
            ? "Editing a stack's composition isn't available on this install."
            : `Save failed: ${e.message || String(e)}`;
        _psDefOut(id, text, false);
    }
}

// Re-fetch + re-render the panel content in place, WITHOUT toggling it closed
// (the toggle-on-same-action guard in _psEditDefinition would close it here).
async function _psEditDefinition_refresh(id) {
    try {
        const def = await apiCall(`/stacks/${id}/definition`);
        _psDefState[id] = {
            compose_spec: def.compose_spec,
            compose_yaml: def.compose_yaml,
            revisions: def.revisions || [],
            current_revision_no: def.current_revision_no,
        };
        _psRenderEditDefinition(id);
    } catch (_) { /* keep the just-saved panel state on screen */ }
}

async function _psDefDiff(id) {
    const sel = document.getElementById(`ps-def-revs-${id}`);
    const revNo = sel && sel.value;
    const out = document.getElementById(`ps-def-diff-${id}`);
    if (!revNo || !out) return;
    const current = (_psDefState[id] || {}).current_revision_no;
    out.innerHTML = `<div class="text-xs text-gray-500 mb-2">Loading diff…</div>`;
    try {
        const r = await apiCall(`/stacks/${id}/revisions/${current}/diff?against=${encodeURIComponent(revNo)}`);
        const text = (r.diff && r.diff.unified_diff) || '(no textual diff)';
        out.innerHTML = `<div class="rounded border border-gray-300 mb-2" style="background-color:#111827">
            <div class="flex items-center justify-between px-2 py-1">
                <span class="text-xs text-gray-300">Diff: rev ${_psEsc(String(revNo))} → current (rev ${_psEsc(String(current))})</span>
                <button data-ps-act="def-diff-close" data-ps-id="${id}" class="text-xs text-gray-400 hover:text-white" aria-label="Close">✕</button>
            </div>
            <pre class="text-xs text-gray-100 whitespace-pre-wrap max-h-56 overflow-auto font-mono px-2 pb-2">${_psEsc(text)}</pre>
        </div>`;
    } catch (e) {
        out.innerHTML = `<div class="text-xs text-destructive mb-2">Diff failed: ${_psEsc(e.message || String(e))}</div>`;
    }
}

async function _psDefRevert(id) {
    const sel = document.getElementById(`ps-def-revs-${id}`);
    const revNo = sel && sel.value;
    if (!revNo) return;
    if (!confirm(`Revert stack ${id} to revision ${revNo}? This appends a new revision equal to it — history is kept.`)) return;
    try {
        await apiCall(`/stacks/${id}/revert`, {
            method: 'POST', body: JSON.stringify({ target_revision_no: parseInt(revNo, 10) }),
        });
        _psMsg(`Stack ${id} reverted to revision ${revNo} (as a new revision).`, true);
        await _psLoadStacks();
        await _psEditDefinition_refresh(id);
    } catch (e) {
        const text = /entitlement_required|402/.test(String(e.message || e))
            ? "Editing a stack's composition isn't available on this install."
            : `Revert failed: ${e.message || String(e)}`;
        _psMsg(text, false);
    }
}

async function _psCopy(text) {
    try { await navigator.clipboard?.writeText(text); _psMsg(`Copied: ${text}`, true); }
    catch (_) { _psMsg(`Copy failed — select and copy manually: ${text}`, false); }
}

// Copy a stack's container-log tail. Reads the rendered <pre> (the live source
// of truth) and uses the async Clipboard API with a legacy execCommand fallback
// for insecure-origin / older browsers, briefly flipping the button to "Copied".
async function _psCopyLogs(id, btn) {
    const pre = document.getElementById(`ps-logs-pre-${id}`);
    const text = (pre && pre.textContent) || '';
    if (!text.trim() || text.trim() === '(no log output returned)') { _psMsg('No logs to copy', false); return; }
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const ta = document.createElement('textarea');
            ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
            document.body.appendChild(ta); ta.select(); document.execCommand('copy'); document.body.removeChild(ta);
        }
        if (btn) { const o = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => { btn.textContent = o; }, 1500); }
    } catch (_) { _psMsg('Could not copy logs to clipboard', false); }
}

async function _psLoadStacks() {
    const rows = document.getElementById('ps-rows');
    if (!rows) return;
    // Re-rendering the table destroys any open log panels (their <pre> is gone),
    // so stop every live poller first — otherwise a tick would keep dispatching
    // worker log-fetch tasks against a panel nobody can see.
    _psCancelAllLogPollers();
    try {
        // Fleet fetched alongside the list so every row's Worker dropdown
        // renders populated in one pass (a failure yields [] — each select
        // still offers the stack's own host, see _psWorkerSelect).
        const [stacks, workers, paid] = await Promise.all([
            apiCall('/stacks'), _psWorkerNames(), isPaidEdition().catch(() => false)]);
        _psPaidEdition = !!paid;
        _psById = {};
        stacks.forEach((s) => { _psById[s.id] = s; });
        _psLastWorkers = workers;
        _psTransitSettle(stacks);
        if (!stacks.length) {
            rows.innerHTML = '<tr><td colspan="8" class="px-4 py-6 text-center text-sm text-gray-400">No stacks yet. A stack runs a service such as an AI model on one of your workers: add a worker first (Dashboard > Workers > Add Worker), then click New AI stack.</td></tr>';
            return;
        }
        rows.innerHTML = stacks.map(s =>
            _psStackRow(s, workers) + `<tr id="ps-backups-${Number(s.id)}"></tr>`
        ).join('');
        // : the
        // freshly parsed panel carries the inline display of whatever state the
        // render saw; re-apply the ONE open menu's inline display + geometry so
        // a rewrite of the whole tbody cannot close or misplace it.
        _psMoreReapply();
    } catch (e) {
        rows.innerHTML = `<tr><td colspan="8" class="px-4 py-6 text-center text-sm text-destructive">Failed to load: ${_psEsc(e.message || String(e))}</td></tr>`;
    }
}

// One compact line of LLM-gateway queue metrics shown under a stack's name.
// Pure on purpose: given the gateway_metrics object from the /stacks row
// (null when the stack has no gateway, or the worker predates the feature),
// it returns an HTML string or '' with no DOM access, so it can be unit-
// tested standalone.
// 27.5b: the gateway snapshot OMITS unknown fields (the API only ever emits
// keys it could determine), so every read defaults to null and the metric is
// left out of the line entirely — never a fabricated 0 that would read as an
// empty queue. A real 0 (empty queue, 0% cache hits) does print 0.
function _psGwNum(gm, key) {
    const v = gm[key];
    if (v == null || v === '' || v === true || v === false) return null;
    const n = Number(v);
    return isFinite(n) ? n : null;
}

function _psGwWait(v) {
    return v == null ? '–' : `${v.toFixed(1)}s`;
}

function _psGwCache(v) {
    if (v == null) return '–';
    const pct = Math.max(0, Math.min(100, Math.round(v * 100)));
    if (pct === 0) return v > 0 ? '<1%' : '0%';
    return pct < 1 ? '<1%' : `${pct}%`;
}

function _psGwPerWorker(gm) {
    if (!Array.isArray(gm.per_worker)) return [];
    return gm.per_worker.filter((w) => w && typeof w === 'object');
}

function _psGatewayLine(gm) {
    if (!gm || typeof gm !== 'object') return '';
    const dash = '–';
    const queue = _psGwNum(gm, 'queue_depth');
    const p50 = _psGwNum(gm, 'wait_p50_s');
    const p95 = _psGwNum(gm, 'wait_p95_s');
    const cache = _psGwNum(gm, 'cache_hit_rate');
    const total = _psGwNum(gm, 'replicas_total');
    const healthy = _psGwNum(gm, 'replicas_healthy');
    const workers = _psGwPerWorker(gm);
    // Only the fields the snapshot actually knows are shown — an unknown
    // metric is simply left out (never a 0 that would read as "empty queue").
    const segs = [];
    if (queue != null) {
        const amber = total != null && queue >= 2 * total;
        segs.push(`queue ${amber
            ? `<span class="text-amber-600 font-semibold">${_psEsc(queue)}</span>`
            : _psEsc(queue)}`);
    }
    if (p50 != null) segs.push(`p50 ${_psEsc(_psGwWait(p50))}`);
    if (p95 != null) segs.push(`p95 ${_psEsc(_psGwWait(p95))}`);
    if (total != null || healthy != null) {
        const reps = `${healthy == null ? dash : healthy}/${total == null ? dash : total}`;
        const amber = healthy != null && total != null && healthy < total;
        segs.push(`replicas ${amber
            ? `<span class="text-amber-600 font-semibold">${_psEsc(reps)}</span>`
            : _psEsc(reps)}`);
    }
    if (cache != null) segs.push(`cache ${_psEsc(_psGwCache(cache))}`);
    if (workers.length) {
        const inflight = workers.map((w) => (w.active == null || !isFinite(Number(w.active))) ? dash : String(Number(w.active)));
        segs.push(`in-flight ${_psEsc(inflight.join(' · '))}`);
    }
    if (!segs.length) return '';
    const title = workers.length
        ? _psEsc(workers.map((w) => `${w.url != null ? w.url : dash}: active ${w.active != null ? w.active : dash} (${w.healthy ? 'healthy' : 'down'})`).join(' · '))
        : '';
    return `<div data-testid="ps-gateway-metrics"${title ? ` title="${title}"` : ''} class="mt-0.5 text-xs text-gray-500">Gateway: ${segs.join(' · ')}</div>`;
}

// --- Stack-row "More" menu (owner 11-18: the row had ~20 buttons; three stay
// visible, the rest live in one grouped menu). Only ONE menu is open at a time;
// its stack id lives in _psMoreOpen, and _psStackRow re-applies that open state
// on every render — the auto-refresh patcher rebuilds the whole <tr> on a poll
// tick, so an open menu must survive that rebuild (the live-refresh trap).
let _psMoreOpen = null;
function _psMoreMenuEl(sid) {
    return document.querySelector(`[data-testid="ps-more-menu-${sid}"]`);
}
//  (owner 11-18,
// real browser on the Dell): the closed panel was hidden ONLY by the `hidden`
// attribute, and CORE/Dashboard/tailwind.css carries `.flex { display: flex; }`
// AFTER `[hidden]:where(:not([hidden="until-found"])) { display: none; }` — both
// specificity (0,1,0) (`:where()` adds 0), so the later `.flex` utility won and
// the closed menu rendered inline: every group and item visible at all times.
// Visibility is therefore driven by an INLINE style only (inline style beats any
// class rule): closed = display:none, open = display:block. `hidden` /
// `aria-hidden` stay as a11y state but are never what hides the panel.
// The stacks table sits inside an `overflow-hidden` card (CORE/Dashboard/pages/
// analytics.html:1306), so an absolutely-positioned panel would be CLIPPED by
// that card. When the menu is open it is therefore lifted to `position:fixed`
// and placed from the More button's getBoundingClientRect, clamped into the
// viewport (the brief's preferred form for an overflow container). With no
// layout information (detached node / jsdom) it keeps the cell-anchored
// absolute form from the markup instead.
function _psMorePosition(sid) {
    const m = _psMoreMenuEl(sid);
    if (!m) return;
    if (_psMoreOpen !== sid) { m.style.display = 'none'; m.setAttribute('aria-hidden', 'true'); return; }
    m.style.display = 'block';
    m.setAttribute('aria-hidden', 'false');
    const btn = document.querySelector(`[data-ps-act="more"][data-ps-id="${sid}"]`);
    // `window` is absent in the pure-function test sandboxes (the *.check.js
    // harnesses run these helpers as a bare function body), so read it safely.
    const win = (typeof window === 'object' && window) ? window : null;
    const vw = win && win.innerWidth || 0;
    const vh = win && win.innerHeight || 0;
    const r = btn ? btn.getBoundingClientRect() : null;
    if (!r || !r.width || !vw || !vh) return;   // markup's absolute/right:0/top:100% stays
    const w = m.offsetWidth || 224;
    const h = m.offsetHeight || 0;
    let left = r.right - w;
    if (left < 8) left = 8;
    if (vw - w - 8 > 8 && left > vw - w - 8) left = vw - w - 8;
    let top = r.bottom + 4;
    if (h && top + h > vh - 8) {
        const up = r.top - h - 4;
        top = up > 8 ? up : Math.max(8, vh - h - 8);
    }
    m.style.position = 'fixed';
    m.style.left = `${Math.round(left)}px`;
    m.style.top = `${Math.round(top)}px`;
    m.style.right = 'auto';
    m.style.bottom = 'auto';
}
function _psMoreClose() {
    if (_psMoreOpen == null) return;
    const m = _psMoreMenuEl(_psMoreOpen);
    if (m) { m.style.display = 'none'; m.setAttribute('aria-hidden', 'true'); }
    _psMoreOpen = null;
}
function _psMoreToggle(sid) {
    if (_psMoreOpen != null && _psMoreOpen !== sid) _psMoreClose();
    if (_psMoreOpen === sid) {
        const m = _psMoreMenuEl(sid);
        if (m) { m.style.display = 'none'; m.setAttribute('aria-hidden', 'true'); }
        _psMoreOpen = null;
        return;
    }
    _psMoreOpen = sid;
    _psMorePosition(sid);
}
// After any re-render (the auto-refresh patcher rebuilds the whole <tr>, and
// _psLoadStacks rewrites the tbody) re-apply the one open menu's inline display
// and viewport geometry, so an open menu survives the poll tick.
function _psMoreReapply() {
    if (_psMoreOpen == null) return;
    _psMorePosition(_psMoreOpen);
}

// PS-STATUS-REASON-BEGIN — FIX-DASH-STACK-PORT-WARNINGS (owner asked 11-18): a
// stack row carries the reason it is in its current status (`status_reason`,
// CORE/API/models.py:8400; the D76 busy-host-port case sets it to
// "Host port <n>/tcp is already used by <project>" and after
// stacks.host_port_conflict_give_up_s the stack ends `failed` with the same
// reason). Show it under the status badge so the operator can read WHY the stack
// is stuck instead of hunting the worker by hand. Amber while deploying, red once
// failed; empty reason renders nothing.
function _psStatusReason(s) {
    const r = (typeof s.status_reason === 'string') ? s.status_reason.trim() : '';
    if (!r) return '';
    if (s.status !== 'deploying' && s.status !== 'failed' && s.status !== 'created') return '';
    const cls = s.status === 'failed' ? 'text-red-700' : 'text-amber-700';
    return `<div class="mt-1 text-xs ${cls}" data-testid="ps-status-reason" title="${_psEsc(r)}">${_psEsc(r)}</div>`;
}
// PS-STATUS-REASON-END

// A stack's main data row as a single reusable unit, tagged with the stack id
// (data-ps-main) and a cheap signature of everything the row shows (data-ps-sig)
// so the auto-refresh patcher can rebuild just this one <tr> in place when its
// data changes — without rebuilding the tbody and losing scroll position or the
// state of any open panel row that follows it.
function _psStackRow(s, workers) {
    const sid = Number(s.id); // rc20e SECURITY: numeric coercion — the row interpolates the id into innerHTML 25x
    const sig = _psStackSig(s);
    const transit = _psTransitLabel(s);
    const cls = transit ? 'bg-amber-100 text-amber-700' : (_PS_STATUS_CLASS[s.status] || 'bg-gray-100 text-gray-600');
    const dis = transit ? ' disabled aria-disabled="true"' : '';
    // : three visible buttons + a
    // grouped "More" menu (owner 11-18). Every moved button keeps its exact
    // data-ps-act / data-ps-id / data-testid / title / label so the delegated
    // click handler and the existing tests keep working; only placement changes.
    const moreOpen = _psMoreOpen === sid;
    // rc20e SECURITY: the whole JSON value goes through _psEsc (the old form
    // escaped only ' inside the single-quoted attribute, leaving " and < live).
    const refs = _psEsc(JSON.stringify(s.storage_refs || {}));
    const ha = s.ha_class || 'external';
    const haCls = _PS_HA_CLASS_STYLE[ha] || 'bg-gray-100 text-gray-600';
    // GR.FEN.3b: the badge shows the class name itself (stateless /
    // cache_backed / snapshot_restore / external) — the API value the operator
    // also sees in the edit panel — with the one-line explanation as tooltip.
    const haLong = _PS_HA_CLASS_LABEL[ha] || ha;
    const haDesc = _PS_HA_CLASS_DESC[ha] || '';
    const haTitle = haDesc ? `${haLong} — ${haDesc}` : haLong;
    const haBadge = ha;
    // Desired state is shown only when it DISAGREES with status. A
    // stack that is running and meant to run needs no annotation; one
    // that is down on purpose must not be mistaken for one that
    // crashed, which is the whole reason the field exists.
    const wantsRunning = (s.desired_state || 'running') === 'running';
    // cUI-2b (30.6.15): the pinned-running badge. Rendered ONLY when the
    // server says keep_running — never from a client-side guess. Neutral
    // classes only (house rule: no saturated blue fills/text).
    const keepRunningBadge = s.keep_running
        ? ` <span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600" data-testid="ps-keep-running-badge-${sid}" title="Keep running is on — this stack will not be put to sleep automatically">pinned running</span>`
        : '';
    const intent = transit ? '' : (!wantsRunning && s.status !== 'torn_down')
        ? ` <span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600" title="Stopped deliberately — MOD will not restart it">stopped on purpose</span>`
        : (wantsRunning && (s.status === 'failed' || s.status === 'stopped')
            ? ` <span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-700" title="Meant to be running but is not">wants running</span>`
            : '');
    // --- Row actions: THREE visible buttons + one grouped "More" menu ------
    // (, owner 11-18: "action
    // buttons on the stacks are CRAZY"). Every button keeps its exact
    // data-ps-act / data-ps-id / data-testid / title / label text and the same
    // state conditions as today, so the delegated click handler and the
    // existing tests keep working; only the placement changes (shared code:
    // paid and CE alike).
    const deployBtn = `<button data-ps-act="deploy" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-primary text-white rounded hover:bg-primary/90">Deploy</button>`;
    const redeployBtn = `<button data-ps-act="deploy" data-ps-id="${sid}" data-testid="ps-redeploy-btn" class="px-2 py-1 text-xs bg-amber-500 text-white rounded hover:bg-amber-600" title="Secrets/variables changed since the last deploy — redeploy to apply the new values">Redeploy</button>`;
    const stopBtn = `<button data-ps-act="stop" data-ps-id="${sid}"${dis} class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Stop without destroying data">Stop</button>`;
    const startBtn = `<button data-ps-act="start" data-ps-id="${sid}"${dis} class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Resume this stack">Start</button>`;
    const logsBtn = `<button data-ps-act="logs" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="View this stack's container logs">Logs</button>`;
    const editBtn = `<button data-ps-act="edit-def" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Edit this stack's services/config (composition), revisioned">Edit (Stack)…</button>`;
    const moreBtn = `<button data-ps-act="more" data-ps-id="${sid}" data-testid="ps-more-btn" aria-haspopup="menu" aria-expanded="${moreOpen ? 'true' : 'false'}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="More stack actions: run, configure, data, placement, danger">More …</button>`;
    const runningNow = s.status === 'running' || s.status === 'starting';
    const mainBtn = s.needs_redeploy ? redeployBtn
        : (!runningNow ? deployBtn : (wantsRunning ? stopBtn : startBtn));
    const runBtns = `${mainBtn === deployBtn ? '' : deployBtn}${
        (wantsRunning ? stopBtn : startBtn) === mainBtn ? '' : (wantsRunning ? stopBtn : startBtn)}`;
    const cfgBtns = `<button data-ps-act="secrets" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="View and rotate this stack's variables & secrets">Secrets &amp; vars</button>
                        <button data-ps-act="login-gate" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Who has to sign in before this stack's web UI answers (login gate + public paths)">Login gate…</button>
                        <button data-ps-act="endpoints" data-ps-id="${sid}" data-testid="ps-endpoints-btn" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Mesh names + ports, public URL, host-port requests">Endpoints…</button>
                        <button data-ps-act="expose" data-ps-id="${sid}" class="px-2 py-1 text-xs ${s.exposed ? 'bg-primary/10 text-primary' : 'bg-gray-100 text-gray-700'} rounded hover:bg-gray-200" title="${s.exposed ? 'Public front door is ON — manage the URL/custom domain or turn it off' : 'Give this stack a public, TLS front door its own users can reach'}">${s.exposed ? '🌐 Exposed' : 'Expose…'}</button>
                        <button data-ps-act="clone" data-ps-id="${sid}" data-ps-name="${_psEsc(s.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Copy this stack's compose into the 'Create from compose' box so you can tweak it and create a new stack">Copy…</button>`;
    const dataBtns = `<button data-ps-act="backup" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Back up</button>
                        <button data-ps-act="backups" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Schedule, retention and restore rehearsal">Backups…</button>
                        <button data-ps-act="seed" data-ps-id="${sid}" data-testid="ps-seed-btn" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Seed this stack from an existing database dump or volume archive (becomes a snapshot; the live stack is untouched until you Restore)">Seed data…</button>
                        <button data-ps-act="restore" data-ps-id="${sid}" data-ps-refs='${refs}' class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Restore</button>
                        <button data-ps-act="migrate" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Migrate</button>`;
    const placeBtns = `${s.pinned_worker
        ? `<button data-ps-act="unpin" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-indigo-100 text-indigo-700 rounded hover:bg-indigo-200" title="Let the rebalancer move this stack again">Unpin</button>`
        : `<button data-ps-act="pin" data-ps-id="${sid}" data-ps-worker="${_psEsc(s.target_worker || '')}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Fix this stack to its host so the rebalancer leaves it alone">Pin</button>`}${s.pinned_worker ? `<button data-ps-act="gpu-pin" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-indigo-100 text-indigo-700 rounded hover:bg-indigo-200" title="Choose which of this host's GPU cards the stack's GPU services may use">Cards…</button><button data-ps-act="gpu-unpin" data-ps-id="${sid}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" title="Clear this stack's GPU card pin">Clear card pin</button>` : ''}
                    <label class="flex items-center gap-1 text-xs text-gray-700">
                        <input type="checkbox" id="ps-keep-running-${sid}" ${s.keep_running ? 'checked' : ''} data-ps-act="keep-running-toggle" data-ps-id="${sid}" data-testid="ps-keep-running-${sid}" title="Keep this stack running: it will not be put to sleep automatically." />
                        Keep running
                    </label>`;
    const dangerBtns = `<button data-ps-act="teardown" data-ps-id="${sid}"${dis} class="px-2 py-1 text-xs bg-gray-100 text-destructive rounded hover:bg-gray-200">Tear down</button>${s.status === 'created' && !s.target_worker
            ? `<button data-ps-act="delete" data-ps-id="${sid}" data-testid="ps-delete-btn" class="px-2 py-1 text-xs bg-gray-100 text-destructive rounded hover:bg-gray-200" title="Delete this stack — it was never deployed, so there is nothing to tear down first">Delete</button>`
            : ''}`;
    // : the
    // panel's visibility and its position are INLINE styles, never Tailwind
    // classes or the `hidden` attribute (see the helper comment above for the
    // cascade proof). `hidden`/`aria-hidden` remain a11y state only.
    // : every
    // menu item is an inline-styled block (width:100%, left-aligned) so the
    // panel reads as a vertical menu without any Tailwind utility. Named
    // WITHOUT the _ps prefix on purpose: the neighbouring row tests auto-stub
    // every `_ps*` identifier they see, which would erase a helper's markup.
    const psMenuItem = (h) => h.replace(/<(button|label)(?=[\s>])/g, '<$1 style="display:block;width:100%;text-align:left"');
    const moreMenu = `<div data-testid="ps-more-menu-${sid}" data-ps-menu="${sid}" aria-hidden="${moreOpen ? 'false' : 'true'}" class="rounded border border-gray-200 bg-white shadow-lg text-xs text-gray-700 flex flex-col items-start p-2 gap-1" style="position:absolute;right:0;top:100%;z-index:50;min-width:14rem;background:#fff;border:1px solid #e5e7eb;border-radius:0.5rem;box-shadow:0 4px 12px rgba(0,0,0,.15);padding:0.25rem 0;display:${moreOpen ? 'block' : 'none'}">
                        <div class="flex flex-col gap-1">
                            <div class="text-[10px] font-semibold text-gray-500">Run</div>
                            ${psMenuItem(runBtns)}
                        </div>
                        <div class="flex flex-col gap-1">
                            <div class="text-[10px] font-semibold text-gray-500">Configure</div>
                            ${psMenuItem(cfgBtns)}
                        </div>
                        <div class="flex flex-col gap-1">
                            <div class="text-[10px] font-semibold text-gray-500">Data</div>
                            ${psMenuItem(dataBtns)}
                        </div>
                        <div class="flex flex-col gap-1">
                            <div class="text-[10px] font-semibold text-gray-500">Placement</div>
                            ${psMenuItem(placeBtns)}
                        </div>
                        <div class="flex flex-col gap-1 border-t border-gray-200 pt-2">
                            <div class="text-[10px] font-semibold text-destructive">Danger</div>
                            ${psMenuItem(dangerBtns)}
                        </div>
                    </div>`;
    return `<tr data-ps-main="${sid}" data-stack-id="${_psEsc(s.id)}" data-ps-sig="${_psEsc(sig)}">
                <td class="px-4 py-2 text-gray-500">${sid}</td>
                <td class="px-4 py-2 font-medium text-gray-800" style="max-width:16rem;overflow-wrap:anywhere">${_psEsc(s.name)}${_psAddrCell(s)}${_psGatewayLine(s.gateway_metrics)}</td>
                <td class="px-4 py-2">
                    <span class="px-2 py-0.5 rounded-full text-xs font-semibold ${cls}" data-testid="ps-status-pill" data-status="${_psEsc(transit || s.status)}">${_psEsc(transit ? transit + '…' : s.status)}</span>${intent}${keepRunningBadge}${_psHealthBadge(s) ? ' ' + _psHealthBadge(s) : ''}${_psStatusHealthBadge(s) ? ' ' + _psStatusHealthBadge(s) : ''}${_psStatusReason(s)}
                    <div class="mt-1 flex items-center gap-1 flex-wrap"><span class="px-2 py-0.5 rounded-full text-xs font-mono ${haCls}" data-testid="ps-ha-badge" data-ha-class="${_psEsc(ha)}" title="${_psEsc(haTitle)}">${_psEsc(haBadge)}</span>${
                        s.ha_class_mismatch
                            ? `<span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-700" data-testid="ps-ha-mismatch" title="The workers are running this stack with ha class ${_psEsc(s.ha_class_observed || '?')} — the stored ${_psEsc(ha)} takes effect on the next redeploy">redeploy needed to apply</span>`
                            : ''
                    }${
                        s.needs_redeploy
                            ? `<span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-700" data-testid="ps-redeploy-badge" title="This stack's secrets or variables changed after its last deploy — the running containers still hold the old values. Press Redeploy to apply them.">Restart needed</span>`
                            : ''
                    }<span class="px-2 py-0.5 rounded-full text-xs font-mono bg-gray-100 text-gray-600" data-testid="ps-identity-line">${_psIdentityLine(s)}</span></div>
                </td>
                <td class="px-4 py-2 text-gray-600">${_psWorkerSelect(s, workers)}</td>
                <td class="px-4 py-2 text-gray-500">${_psEsc(s.storage_class)}</td>
                <td class="px-4 py-2 text-gray-600 whitespace-nowrap" title="Usage charged per hour for this stack's reserved CPU/memory/GPU">${
                    s.reservation_cost_per_hour ? `${Number(s.reservation_cost_per_hour).toFixed(2)} cr/hr` : '—'}${_psEgressBadge(s)}</td>
                <td class="px-4 py-2">${_psBackupBadge(s)}</td>
                <td class="px-4 py-2"><div class="relative flex flex-wrap gap-1 justify-end" style="position:relative;max-width:20rem;margin-left:auto">
                    ${mainBtn}${logsBtn}${editBtn}${moreBtn}${moreMenu}
                </div></td>
            </tr>`;
}

// A compact signature of everything a stack's row displays live, so the
// auto-refresh patcher can compare a fresh fetch against what's on screen and
// only rebuild a row whose visible content actually changed. Cheap and stable
// in key order.
function _psStackSig(s) {
    const wantsRunning = (s.desired_state || 'running') === 'running';
    return JSON.stringify({
        name: s.name, status: s.status, transit: _psTransitLabel(s),
        ha: s.ha_class || 'external', haM: s.ha_class_mismatch ? (s.ha_class_observed || '?') : null,
        storage_class: s.storage_class,
        pinned_worker: s.pinned_worker || null, target_worker: s.target_worker || null,
        pinned_reason: s.pinned_reason || null, selected: s.pinned_worker || '',
        hardware_ineligible: s.hardware_ineligible || {},
        cost: s.reservation_cost_per_hour,
        backup: s.backup_last_error, backupH: s.backup_interval_hours, backupLast: s.backup_last_run_at,
        egress: s.egress_policy, exposed: !!s.exposed, wantsRunning,
        keepRunning: !!s.keep_running,
        needsRedeploy: !!s.needs_redeploy,
        // PS-STATUS-REASON: the reason text is now visible on the row, so a
        // changed reason must change the signature and re-render the row.
        sr: (typeof s.status_reason === 'string') ? s.status_reason : null,
        // PS-STATUS-HEALTH: the degraded badge is visible on the row, so a
        // changed status_health/reason must change the signature too.
        sh: s.status_health || null, shr: s.status_health_reason || null,
        identityApp: s.identity_app_id ?? null, identityAt: s.identity_minted_at || null,
        gw: s.gateway_metrics || null,
        health: (s.health && s.health.grade) ? { grade: s.health.grade, reason: s.health.reason || null } : null,
    });
}

// In-place reconcile of the stack list against a fresh /stacks fetch. Rebuilds
// only the rows whose signature changed, inserts new stacks in order, removes
// stacks that disappeared, and never touches the panel rows that follow a stack
// row (edit-def / logs / backups), the user's scroll position, or the live log
// pollers — unlike _psLoadStacks, which rewrites the whole tbody.
// _psStackRow returns markup; the patcher needs the <tr> node. A <template>
// parses table rows correctly (a <div> would drop the <tr>/<td> tags).
function _psStackRowEl(s, workers) {
    const t = document.createElement('template');
    t.innerHTML = _psStackRow(s, workers).trim();
    return t.content.firstElementChild;
}

function _psPatchStacks(stacks, workers) {
    const tbody = document.getElementById('ps-rows');
    if (!tbody) return;
    _psLastWorkers = workers;
    _psTransitSettle(stacks);
    // Keyed by string id: the DOM's data-ps-main / ps-backups-<id> give strings,
    // and a number key would never match an existing row.
    const newById = new Map();
    stacks.forEach((s) => { newById.set(String(s.id), s); });
    const newIds = stacks.map((s) => String(s.id));
    if (!newIds.length) {
        // Nothing left — clear every main row, its panel, and any leftover
        // placeholder/error row. (No stack means no panel to preserve.)
        tbody.querySelectorAll('tr').forEach((t) => t.remove());
        return;
    }
    // Keep the live-state cache fresh too: functions that read _psById (the
    // worker-select title, the expose panel, the pin dialog, the worker dropdown)
    // must see the current stack, so a poll tick — which never went through
    // _psLoadStacks — can't leave them stale against the table it just patched.
    _psById = {};
    stacks.forEach((s) => { _psById[s.id] = s; });

    // Snapshot the current DOM state: main rows by id, panel rows by id.
    const mainById = new Map();
    const panelById = new Map();
    tbody.querySelectorAll('tr[data-ps-main]').forEach((t) => { mainById.set(t.dataset.psMain, t); });
    tbody.querySelectorAll('tr[id^="ps-backups-"]').forEach((t) => { panelById.set(t.id.slice('ps-backups-'.length), t); });

    newIds.forEach((id, i) => {
        const s = newById.get(id);
        const main = mainById.get(id);
        if (!main) {
            // New stack: build both rows and insert the pair in list order,
            // between its neighbours, so the panel always follows its own main.
            const mainEl = _psStackRowEl(s, workers);
            const panelEl = document.createElement('tr');
            panelEl.id = 'ps-backups-' + id;
            const prevPanel = (i > 0 && panelById.has(newIds[i - 1])) ? panelById.get(newIds[i - 1]) : null;
            const nextPanel = (i < newIds.length - 1 && panelById.has(newIds[i + 1])) ? panelById.get(newIds[i + 1]) : null;
            // Place the new pair in list order: after the previous stack's panel,
            // else before the next stack's panel, else at the end. In every case
            // the panel follows its own main row.
            if (prevPanel) {
                tbody.insertBefore(mainEl, prevPanel.nextSibling);
                tbody.insertBefore(panelEl, mainEl.nextSibling);
            } else if (nextPanel) {
                tbody.insertBefore(mainEl, nextPanel);
                tbody.insertBefore(panelEl, mainEl.nextSibling);
            } else {
                tbody.appendChild(mainEl);
                tbody.appendChild(panelEl);
            }
            mainById.set(id, mainEl);
            panelById.set(id, panelEl);
            return;
        }
        // Existing row: decide whether it changed, and whether we must skip.
        const panel = panelById.get(id) || null;
        const ae = document.activeElement;
        const busy = (main.contains && main.contains(ae)) || (panel && panel.contains && panel.contains(ae));
        const ta = ae && ae.id === 'ps-def-compose-' + id ? ae
            : (panel ? panel.querySelector('textarea#ps-def-compose-' + id) : null);
        const defDirty = ta && main.dataset.defOrigin != null && ta.value !== main.dataset.defOrigin;
        if (busy || defDirty) return;   // leave the row + panel untouched this tick
        if (main.dataset.psSig === _psStackSig(s)) { mainById.set(id, main); return; }
        const newMain = _psStackRowEl(s, workers);
        tbody.replaceChild(newMain, main);
        mainById.set(id, newMain);
    });

    // Remove rows for stacks that vanished, plus any stray placeholder/error
    // row left over from a prior no-stacks/failed load (but not panel rows).
    for (const id of Array.from(mainById.keys())) {
        if (!newById.has(id)) { mainById.get(id).remove(); if (panelById.has(id)) panelById.get(id).remove(); }
    }
    tbody.querySelectorAll('tr').forEach((t) => {
        if (!t.dataset.psMain && t.id !== 'ps-rows' && !t.id.startsWith('ps-backups-')) t.remove();
    });
    // : rebuilt
    // rows arrive closed (their inline display follows the render-time state);
    // re-apply the one open menu's inline display + viewport geometry after the
    // reconcile so an open menu survives every poll tick.
    _psMoreReapply();
}

// --- Persistent-stack list auto-refresh --------------------------------------
// A stack's status is live (workers deploy, the rebalancer migrates, backups
// fire) but the list page never noticed it — it only refreshed on the user's
// own actions, and a manual reload dropped everything open (edit-def panel,
// log viewers, backups) and the scroll position. This patcher, modelled on the
// sequential live-log poller above, re-reads /stacks on a gentle timer and
// reconciles the table in place, so the list updates on its own without
// disturbing any open panel, focus, or scroll.
//
// It is SEQUENTIAL (a setTimeout chain: one fetch completes before the next
// starts), so it can never pile up requests or re-render the whole tbody faster
// than the user can look. It backs off to the ceiling on error and resets to the
// base interval on the next success, pauses while the tab is hidden, and only
// runs while the stacks table is actually visible in the DOM. It never calls
// _psLoadStacks (which would destroy open panels) and never _psCancelAllLogPollers.
const _PS_LIST_BASE_MS = 5000;   // gap after a successful patch
const _PS_LIST_MAX_MS = 30000;   // backoff ceiling on repeated errors
let _PS_LIST_DELAY = _PS_LIST_BASE_MS;
let _psListGen = 0;              // bumped on every stop so in-flight ticks abort
let _psListTimer = null;         // the single in-flight setTimeout handle

// Whether the stacks table is currently in the visible stacks tab (not a
// hidden sibling panel) and present in the DOM. The panel is display:none when
// the user is on another sub-tab, and the list shouldn't be hammering /stacks
// or clobbering focus in a table nobody can see.
function _psStacksTableVisible() {
    const rows = document.getElementById('ps-rows');
    const panel = document.getElementById('main-panel-stacks');
    return !!rows && !!panel && panel.classList.contains('hidden') === false;
}

async function _psListTick(gen) {
    if (_psStacksTableVisible() === false) return;
    let ok = false;
    try {
        const [stacks, workers] = await Promise.all([apiCall('/stacks'), _psWorkerNames()]);
        // Aborted (stopped) mid-flight? Nothing to do.
        if (gen !== _psListGen) return;
        _psPatchStacks(stacks, workers);
        ok = true;
    } catch (_) {
        if (gen !== _psListGen) return;
        // A transient failure (or the list being briefly unavailable) should
        // just slow us down and retry — never spin, never throw to the user.
    }
    if (gen !== _psListGen) return;
    // Back off after errors, reset to base after a success, chain the next tick.
    const delay = ok ? _PS_LIST_BASE_MS : Math.min((_PS_LIST_DELAY || _PS_LIST_BASE_MS) * 2, _PS_LIST_MAX_MS);
    _PS_LIST_DELAY = delay;
    _psListTimer = setTimeout(() => {
        _psListTimer = null;
        if (document.hidden) { _psListTimer = setTimeout(() => _psListTick(gen), 2000); return; }
        _psListTick(gen);
    }, delay);
}

function _psStartListPoll() {
    if (_psListTimer) return;                 // already running
    _PS_LIST_DELAY = _PS_LIST_BASE_MS;        // reset backoff for a fresh start
    _psListGen++;
    const gen = _psListGen;
    _psListTimer = setTimeout(() => {
        _psListTimer = null;
        _psListTick(gen);
    }, 100);
}

function _psStopListPoll() {
    if (_psListTimer) { clearTimeout(_psListTimer); _psListTimer = null; }
    _psListGen++;                             // abort any in-flight tick
}

// stack-ha-reconciliation — is anything watching these stacks, and what
// disagrees? Gate 9's frontend deliverable, in three parts.
//
// The failure this exists to prevent is quiet. A reconciler pass reporting
// "0 gaps" because no worker has sent an observation in an hour reads exactly
// like a pass over a healthy fleet, and the /observations endpoint's own
// docstring names it: a worker that never reports is indistinguishable from one
// whose stacks all vanished, if you only read the drift summary. So the count
// of REPORTING workers leads, and zero is an alarm rather than an empty list.
//
// Deliberately NOT /reconcile/preview, which is the obvious source and the
// wrong one: it runs a full dry-run pass, and `reconcile_statuses` mutates
// stack status even in dry run. Polling it from a page render would let opening
// this tab demote a stack.
async function _psLoadHealth() {
    const body = document.getElementById('ps-health-body');
    const flag = document.getElementById('ps-health-flag');
    if (!body) return;

    const get = async (path) => {
        try { return await apiCall(path); } catch (_) { return null; }
    };
    // CE (GT.D5): the paid fleet recovery views are not served here; state it, request nothing.
    body.innerHTML = '<p class="text-xs text-gray-500">Automatic recovery reporting is not part of this deployment.</p>';
    return;

    // 1 — is automatic recovery actually running. Stated as prominently as a
    // failure, because an operator seeing a stack list with no alarms concludes
    // the platform is holding it up. If the reconciler is off, a stack that
    // dies stays dead, and that assumption turns a short outage into a long one.
    const rc = (placement && placement.reconciler) || {};
    const recovering = rc.enabled && rc.live_mode;
    if (flag) {
        flag.innerHTML = recovering
            ? '<span class="text-success font-medium">Automatic recovery on</span>'
            : '<span class="text-destructive font-medium">Automatic recovery OFF</span>';
    }

    // Platform-admin toggles for the recovery reconciler (DB-backed). Non-admins
    // see the status but no controls; the PUT is admin-gated regardless. These
    // write the exact fields the flag above reads (rc.enabled / rc.live_mode),
    // so the control the operator clicks is the one that changes the state —
    // unlike the rebalancer toggle, which is a different subsystem.
    let _reconAdmin = false;
    try { const u = await me.get(); _reconAdmin = u && u.is_platform_admin === true; } catch (_) { /* not admin */ }
    const reconControls = _reconAdmin ? `
        <div class="rounded border border-gray-200 p-2 mb-2 space-y-1">
            <label class="flex items-center gap-2 text-xs text-gray-700">
                <input type="checkbox" id="ps-recon-enabled" ${rc.enabled ? 'checked' : ''}/>
                <span>Enable automatic recovery (run the reconciler)</span>
            </label>
            <label class="flex items-center gap-2 text-xs text-gray-700">
                <input type="checkbox" id="ps-recon-live" ${rc.live_mode ? 'checked' : ''}/>
                <span>Live mode — actually act on failures (off = dry run, reports only)</span>
            </label>
        </div>` : '';

    const last = rc.last_scheduled_run;
    // Published to Redis with a 1h expiry, so "nothing here" means the job has
    // not run within the hour — itself the answer to "is reconciliation
    // running", and said rather than left blank.
    const lastTxt = last && last.at
        ? `Last pass ${_psEsc(String(last.at))}`
          + (last.dry_run ? ' <span class="text-gray-500">(dry run — decided, did not act)</span>' : '')
          + ` · ${_psEsc(String(last.gaps ?? 0))} gap(s) · ${_psEsc(String((last.enqueued || []).length))} re-placed`
        : 'No reconciliation pass recorded in the last hour.';

    // 2 — who is reporting, and how fresh. Staleness measured against the
    // SERVER's grace, never a number chosen here: a UI with its own threshold
    // would disagree with the reconciler about which host has gone quiet.
    const rows = (obs && obs.observations) || [];
    const graceS = (obs && obs.grace_seconds) ?? (drift && drift.grace_seconds) ?? null;
    const nowMs = Date.now();
    const byWorker = new Map();
    rows.forEach((o) => {
        const cur = byWorker.get(o.worker_name) || { t: 0, rows: 0, running: 0 };
        cur.rows += 1;
        cur.running += (o.running || 0);
        const t = Date.parse(o.observed_at);
        if (t > cur.t) cur.t = t;
        byWorker.set(o.worker_name, cur);
    });
    const watchRows = [...byWorker.entries()].map(([w, c]) => {
        const age = Math.round((nowMs - c.t) / 1000);
        const stale = graceS != null && age > graceS;
        return `<div class="text-xs ${stale ? 'text-amber-800' : 'text-gray-700'}">
            <span class="font-medium">${_psEsc(w)}</span> — ${_psEsc(String(c.rows))} project(s),
            ${_psEsc(String(c.running))} container(s) running · last reported ${_psEsc(String(age))}s ago
            ${stale ? '<span class="font-medium">(stale — past the observation grace)</span>' : ''}
        </div>`;
    }).join('');

    // 3 — the three drift shapes, kept apart because they need opposite
    // responses: one needs a host looked at, one needs containers reaped, and
    // one needs a decision nobody can make automatically.
    const cno = (drift && drift.claimed_not_observed) || [];
    const onc = (drift && drift.observed_not_claimed) || [];
    const rws = (drift && drift.running_while_stopped) || [];
    const obt = (drift && drift.observed_but_torn_down) || [];
    const driftBits = [
        ...cno.map((s) => `<div class="text-xs text-destructive">
            <span class="font-medium">${_psEsc(s.name)}</span> says running — no worker sees it.
            ${s.last_observed_at
                ? `Last seen ${_psEsc(String(s.last_observed_at))} on ${_psEsc(String(s.last_observed_on || '?'))}.`
                : 'It has never been observed.'}</div>`),
        ...onc.map((o) => `<div class="text-xs text-amber-800">
            <span class="font-medium">${_psEsc(o.project)}</span> is running on ${_psEsc(o.worker_name)}
            (${_psEsc(String(o.running))}/${_psEsc(String(o.total))}) and no stack claims it —
            usually a teardown whose worker task never landed. Surfaced, not reaped.</div>`),
        ...rws.map((s) => `<div class="text-xs text-amber-800">
            <span class="font-medium">${_psEsc(s.name)}</span> is ${_psEsc(s.status)} while marked stopped${
                s.target_worker ? ` on ${_psEsc(s.target_worker)}` : ''}.
            <span class="text-gray-600">${_psEsc(s.note || '')}</span></div>`),
        // The host named here is where the container actually is, NOT the
        // stack's target_worker — for a re-placed stack those differ, and the
        // target is the one host you do not need to visit.
        ...obt.map((s) => `<div class="text-xs text-destructive">
            <span class="font-medium">${_psEsc(s.name)}</span> is torn down but still
            running on ${_psEsc((s.observed_on || []).join(', ') || '?')}.
            <span class="text-gray-600">${_psEsc(s.note || '')}</span></div>`),
    ].join('');

    // GE.AIC.6(b) (2026-09-29): moved here from AI Configuration's "Placement
    // & recovery" panel, which was removed — the Stacks page already fetches
    // this same `/stacks/placement` response for the reconciler/drift report
    // above, and the rebalancer's per-worker score breakdown belongs beside
    // it rather than on a page about model/tool configuration. Say which
    // decisions are informed by run history and which are still free capacity
    // alone — a move made on a week of data and one made on a snapshot are
    // different claims, and an operator cannot tell them apart unless it is
    // written down.
    const rb = (placement && placement.rebalancer) || {};
    const rbThin = rb.history_thin_for || [];
    const rbScores = rb.worker_scores || [];
    const rebalancerBlock = (rb.note || rb.enabled || rbScores.length) ? `
        <div>
            <div class="text-xs font-semibold text-gray-600 mb-1">Relocation (rebalancer)</div>
            ${rb.note ? `<div class="text-xs text-gray-700">${_psEsc(rb.note)}</div>` : ''}
            ${rb.enabled ? `<div class="text-xs text-gray-700">On · moves only when a host is ${_psEsc(String(Math.round((rb.threshold ?? 0) * 100)))}% better.</div>` : ''}
            ${rbScores.length ? `<div class="text-xs text-gray-700 mt-1">${rbScores.map((s) =>
                `${_psEsc(s.worker)}: free ${_psEsc(String(Math.round((s.free ?? 0) * 100) / 100))}` +
                (s.history_thin
                    ? ' <span class="text-gray-500">(no run history yet — decided on free capacity alone)</span>'
                    : ` · reliability ×${_psEsc(String(Math.round((s.health_factor ?? 1) * 100) / 100))}` +
                      ` over ${_psEsc(String(s.history_stack_days ?? 0))} stack-days, ${_psEsc(String(s.history_failures ?? 0))} failure(s)`)
            ).join('<br>')}</div>` : ''}
            ${rbThin.length && rbThin.length === rbScores.length && rbScores.length
                ? '<div class="text-xs text-gray-600 mt-1">No worker has enough run history yet, so relocation is still decided on free capacity.</div>'
                : ''}
        </div>` : '';

    // Stacks this reconciler has actually moved. Kept because "it is enabled"
    // and "it has ever done anything" are different claims.
    const replaced = ((placement && placement.stacks) || [])
        .filter((s) => s.last_reconcile)
        .map((s) => `<div class="text-xs text-gray-700">
            <span class="font-medium">${_psEsc(s.name)}</span> was re-placed automatically —
            ${_psEsc(String(s.last_reconcile.at || ''))}${
                s.last_reconcile.reason ? ` · ${_psEsc(String(s.last_reconcile.reason))}` : ''}
            ${s.last_reconcile.last_host
                ? `<span class="text-gray-500">(previous host: ${_psEsc(String(s.last_reconcile.last_host))})</span>`
                : ''}</div>`).join('');

    body.innerHTML = `
        ${reconControls}
        ${rc.note ? `<div class="text-xs text-destructive">${_psEsc(rc.note)}</div>` : ''}
        <div class="text-xs text-gray-700">${lastTxt}</div>
        <div>
            <div class="text-xs font-semibold text-gray-600 mb-1">Watching (stack observations)</div>
            ${byWorker.size ? watchRows : `<div class="text-xs text-destructive">
                No worker is reporting stack observations. Recovery cannot detect a failure it
                cannot see, so the gap count above means nothing. Check the workers are on a
                binary new enough to send <code>hosted_stacks</code>.</div>`}
        </div>
        <div>
            <div class="text-xs font-semibold text-gray-600 mb-1">Drift (expected vs observed)</div>
            ${!drift ? '<div class="text-xs text-gray-500">Could not read drift.</div>'
                     : (driftBits || `<div class="text-xs text-success">No drift — every running
                        stack is observed, and nothing is running unclaimed.</div>`)}
        </div>
        ${rebalancerBlock}
        ${replaced ? `<div>
            <div class="text-xs font-semibold text-gray-600 mb-1">Recent automatic re-placements</div>
            ${replaced}</div>` : ''}
    `;

    if (_reconAdmin) {
        document.getElementById('ps-recon-enabled')?.addEventListener('change', _psSaveReconciler);
        document.getElementById('ps-recon-live')?.addEventListener('change', _psSaveReconciler);
    }
}

async function _psSaveReconciler() {
    try {
        const enabled = document.getElementById('ps-recon-enabled')?.checked;
        const live_mode = document.getElementById('ps-recon-live')?.checked;
        const r = await apiCall('/stacks/reconciler-config', {
            method: 'PUT', body: JSON.stringify({ enabled, live_mode }),
        });
        _psMsg(`Automatic recovery ${r.enabled ? 'on' : 'off'}`
            + (r.enabled ? (r.live_mode ? ' · live (acts on failures)' : ' · dry run (reports only)') : '')
            + '.', true);
        _psLoadHealth();  // re-render flag + toggles from the new state
    } catch (e) {
        _psMsg('Could not update recovery config: ' + (e?.message || e), false);
    }
}

async function _psLoadRebalancer() {
    // CE (GT.D5): the paid rebalancer dial is not served; leave it hidden, request nothing.
}

// gw-stack-networking GW.4 — the fleet host-port / subnet usage card. Admin-only
// endpoint; same "reveal only if reachable" idiom as the rebalancer card.
async function _psLoadNetworkingUsage() {
    let u;
    try { u = await me.get(); } catch (_) { return; }
    if (!u || u.is_platform_admin !== true) return;
    const body = document.getElementById('ps-networking-usage-body');
    const card = document.getElementById('ps-networking-usage');
    if (!body || !card) return;
    let d;
    try { d = await apiCall('/stacks/networking/usage'); } catch (_) { return; }
    card.classList.remove('hidden');
    const legacy = d.legacy || [];
    const managed = d.managed || [];
    if (!legacy.length && !managed.length) {
        body.innerHTML = '<div class="text-success">No host ports or subnets in use — nothing to collide.</div>';
        return;
    }
    let html = '';
    legacy.forEach((r) => {
        const ports = (r.host_ports || []).map((p) => `${_psEsc(p.service)}: ${_psEsc(String(p.port))}`).join(', ');
        const subs = (r.subnets || []).map((s2) => `${_psEsc(s2.network)}=${_psEsc(s2.subnet)}`).join(', ');
        html += `<div class="text-amber-800"><span class="font-semibold">${_psEsc(r.name)}</span> (id ${Number(r.id)}, legacy): `
            + [ports ? `publishes ${ports}` : '', subs ? `pins ${subs}` : ''].filter(Boolean).join(' · ') + '</div>';
    });
    managed.forEach((r) => {
        const hps = (r.host_ports || []).map((p) =>
            `${_psEsc(p.service)} ${p.host === 'any' ? '→ any' : `:${_psEsc(String(p.host))}`} (${_psEsc(p.protocol || 'tcp')}${p.bind ? ` on ${_psEsc(p.bind)}` : ''})`).join(', ');
        html += `<div class="text-gray-700"><span class="font-semibold">${_psEsc(r.name)}</span> (id ${Number(r.id)}, managed): ${hps}</div>`;
    });
    body.innerHTML = html;
}

async function _psSaveThreshold() {
    // CE (GT.D5): the paid rebalancer dial is not served; nothing to save.
}

// delegated-stack-authoring §6.3 — the Dashboard grant surface for platform
// admins. Thin wrapper over the existing /platform-capabilities grant/revoke
// endpoints (Group 1.4), placed on this page because "stack:author" is the
// capability every control up above already assumes the caller holds — an
// admin managing stack authoring is the admin who wants to hand it to someone
// else, right here, without a separate detour through a permissions screen.
const _PS_CAPABILITY = 'stack:author';

function _psGrantRow(g) {
    const expiry = g.expires_at ? new Date(g.expires_at).toLocaleString() : 'never';
    const status = g.active
        ? '<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">active</span>'
        : '<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500">revoked</span>';
    return `<tr>
        <td class="px-4 py-2 text-gray-500">${_psEsc(g.user_id)}</td>
        <td class="px-4 py-2">${status}</td>
        <td class="px-4 py-2 text-gray-500">${_psEsc(expiry)}</td>
        <td class="px-4 py-2 text-right">${g.active
            ? `<button data-ps-grant-revoke="${Number(g.id)}" class="px-2 py-1 text-xs bg-gray-100 text-destructive rounded hover:bg-gray-200">Revoke</button>`
            : ''}</td>
    </tr>`;
}

// 2400LR-pg6c: /platform-capabilities is CE-denied (manifest.yaml:1332), so the
// grants card is hidden on CE. Fail-open: unknown edition / errored entitlements
// block / features fetch error all keep the card and its load.
function _psIsCeEdition() {
    return getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !!ent && ent.edition === 'ce' && !ent.error;
    }).catch(() => false);
}

async function _psLoadGrants() {
    const body = document.getElementById('ps-grants-rows');
    if (!body) return;
    if (await _psIsCeEdition()) {
        const card = document.getElementById('ps-grants');
        if (card) card.style.display = 'none';
        return;
    }
    try {
        const rows = await apiCall(`/platform-capabilities?include_inactive=true`);
        const filtered = rows.filter(r => r.capability === _PS_CAPABILITY);
        body.innerHTML = filtered.length
            ? filtered.map(_psGrantRow).join('')
            : '<tr><td colspan="4" class="px-4 py-4 text-center text-xs text-gray-400">No stack-authoring grants yet.</td></tr>';
    } catch (e) {
        body.innerHTML = `<tr><td colspan="4" class="px-4 py-4 text-center text-xs text-destructive">Failed to load: ${_psEsc(e.message || String(e))}</td></tr>`;
    }
}

async function _psGrantCapability() {
    const uidEl = document.getElementById('ps-grant-user-id');
    const expEl = document.getElementById('ps-grant-expiry');
    const uid = parseInt(uidEl?.value, 10);
    if (!uid) { _psMsg('Enter a user id to grant stack authoring to.', false); return; }
    const body = { user_id: uid, capability: _PS_CAPABILITY };
    if (expEl?.value) body.expires_at = new Date(expEl.value).toISOString();
    try {
        await apiCall('/platform-capabilities', { method: 'POST', body: JSON.stringify(body) });
        _psMsg(`Granted stack authoring to user ${uid}.`, true);
        if (uidEl) uidEl.value = '';
        if (expEl) expEl.value = '';
        _psLoadGrants();
    } catch (e) { _psMsg(`Grant failed: ${e.message || e}`, false); }
}

async function _psRevokeGrant(grantId) {
    try {
        await apiCall(`/platform-capabilities/${grantId}`, { method: 'DELETE' });
        _psMsg(`Grant ${grantId} revoked.`, true);
        _psLoadGrants();
    } catch (e) { _psMsg(`Revoke failed: ${e.message || e}`, false); }
}

export function initializePersistentStacksPage() {
    // SPA remount: page HTML re-injects on every visit, so the wiring marker
    // lives on an element of the (new) DOM, not a module flag. The data load
    // at the bottom still runs on every visit.
    const psAnchor = document.getElementById('ps-refresh') || document.getElementById('ps-rows');
    const psNeedsWire = !psAnchor || !psAnchor.dataset.wired;
    if (psAnchor) psAnchor.dataset.wired = '1';
    if (psNeedsWire) {
        document.getElementById('ps-refresh')?.addEventListener('click', () => {
            _psLoadStacks();
            // Refreshed together: an operator pressing Refresh during an
            // incident wants the health state re-read too, and a stale
            // "no drift" beside a freshly-loaded list is worse than no card.
            _psLoadHealth();
        });
        document.getElementById('ps-tpl')?.addEventListener('change', _psRenderTplParams);
        document.getElementById('ps-create')?.addEventListener('click', _psCreateFromTemplate);
        document.getElementById('ps-compose-preview')?.addEventListener('click', _psComposePreviewClick);
        // "Preview rendered" for the paste-your-own card. The card itself is
        // static page markup this file doesn't own, so the button and its
        // result panel are injected here next to it (idempotent on remount).
        const _psComposePreviewBtn = document.getElementById('ps-compose-preview');
        if (_psComposePreviewBtn) _psWireRenderPreview(_psComposePreviewBtn);
        document.getElementById('ps-compose-create')?.addEventListener('click', _psComposeCreateClick);
        document.getElementById('ps-new-ai-stack')?.addEventListener('click', _psNewAIStack);
        // GX.S10c — live Variables & secrets panel under the paste box (the
        // Copy… path lands in the same box, so it is covered for free). The
        // panel host is injected by _psMountComposeVariables next to
        // #ps-compose-result, so gate on THAT element — the host does not
        // exist in the static markup.
        const _psComposeResultEl = document.getElementById('ps-compose-result');
        if (_psComposeResultEl) _psMountComposeVariables();
        // GA.ASW.6 — the "no backup plan" acknowledgement box: the checkbox
        // gates the confirm button, confirm resends the pending create with
        // acknowledge_no_backup: true, cancel discards it with no request.
        document.getElementById('ps-backup-ack-checkbox')?.addEventListener('change', (e) => {
            const btn = document.getElementById('ps-backup-ack-confirm');
            if (btn) btn.disabled = !e.target.checked;
        });
        document.getElementById('ps-backup-ack-confirm')?.addEventListener('click', _psBackupAckConfirmClick);
        document.getElementById('ps-backup-ack-cancel')?.addEventListener('click', _psBackupAckHide);
        document.getElementById('ps-rb-save')?.addEventListener('click', _psSaveThreshold);
        document.getElementById('ps-grant-save')?.addEventListener('click', _psGrantCapability);
        document.getElementById('ps-grants-rows')?.addEventListener('click', (e) => {
            const b = e.target.closest('[data-ps-grant-revoke]');
            if (!b) return;
            _psRevokeGrant(b.dataset.psGrantRevoke);
        });
        document.getElementById('ps-rows')?.addEventListener('change', (e) => {
            const sel = e.target.closest('[data-ps-worker-select]');
            if (sel) _psSetWorker(sel.dataset.psId, sel.value, sel);
            const haSel = e.target.closest('[data-ps-ha-select]');
            if (haSel) _psHaDescFollow(haSel.dataset.psHaSelect);
            // 
            // (, PIN13 R3 real red: after cb.uncheck() inside the
            // More panel #ps-msg never painted 'free to be reclaimed'): the
            // checkbox now lives inside the row's More panel, and the row can be
            // rebuilt (the 5 s list poll / the toggle's own _psLoadStacks) between
            // the click's hit-test and the event, so the click leg can be LOST
            // while the browser still flips the input. A checkbox value change
            // always fires `change` on the live node, so the toggle is also driven
            // from here: whichever leg arrives first dispatches exactly once
            // (`_psKrClicked` is the click leg's claim; the `change` leg skips
            // only when the click leg already claimed THIS stack id).
            const kr = e.target.matches('input') && e.target.dataset.psAct === 'keep-running-toggle'
                ? e.target : null;
            if (kr) {
                const kid = Number(kr.dataset.psId);
                // Our own revert (a refused PUT) flips the checkbox back and
                // fires `change` again: never send a second PUT for it.
                if (_psKrRevert === kid) { _psKrRevert = null; return; }
                _psKeepRunningSet(kid, kr.checked);
            }
        });
        // The auto-refresh list poller only makes sense when the stacks sub-panel
        // is the visible one (the SPA hides other sub-panels with display:none, so
        // #ps-rows is always in the DOM). Start it when we open the tab and stop it
        // when we leave, so it never polls /stacks from a hidden panel.
        const stacksPanel = () => document.getElementById('main-panel-stacks');
        function _psTabChanged() {
            if (_psStacksTableVisible()) _psStartListPoll(); else _psStopListPoll();
        }
        if (stacksPanel()) {
            new MutationObserver(_psTabChanged).observe(stacksPanel(), { attributes: true, attributeFilter: ['class'] });
            _psTabChanged();
        }
        document.getElementById('ps-rows')?.addEventListener('click', (e) => {
            const b = e.target.closest('[data-ps-act]');
            if (!b) return;
            const id = b.dataset.psId;
            const act = b.dataset.psAct;
            // "More …" toggles the row's grouped menu; it is not an API action
            // and must not disturb an open log panel.
            if (act === 'more') return _psMoreToggle(Number(id));
            // 
            // (owner 11-18 row design, PIN12 Dell browser proof letter (g) FAILED):
            // a menu-item click runs its action AND closes that menu, and the
            // CLOSE MUST HAPPEN FIRST. Most acts below are dispatched with an
            // early `return _psXxx(id)` that opens a dialog (Endpoints, Secrets,
            // Login gate, Seed, Backups, Definition, Expose) or awaits a fetch,
            // so a close placed at the bottom of this handler never runs for
            // them and the panel stayed `display:block` behind/over the dialog.
            // Close synchronously here, before any act is dispatched, for every
            // element inside the one open More panel. The Keep running checkbox
            // is the documented exception (its value change fires `change`, not
            // click, and a click on it lands on the label): the menu stays open
            // while the user works the toggle; the outside-click/Escape closers
            // still close it.
            if (_psMoreOpen != null && act !== 'keep-running-toggle'
                && b.closest(`[data-ps-menu="${_psMoreOpen}"]`)) _psMoreClose();
            // Every panel except the log viewer itself renders into the same
            // ps-backups-<id> row slot, so any non-log action destroys an open
            // log panel's <pre>. Stop that stack's poller up front (a no-op when
            // none is running) so it can't keep dispatching worker log-fetch
            // tasks against a node the user can no longer see. Log-panel actions
            // manage their own poller lifecycle and are excluded.
            if (!act.startsWith('logs')) _psCancelLogPoller(id);
            if (act === 'backup') return _psBackup(id);
            if (act === 'backups') return _psBackups(id);
            if (act === 'edit-def') return _psEditDefinition(id);
            if (act === 'secrets') return _psSecrets(id);
            if (act === 'login-gate') return _psLoginGate(id);
            if (act === 'seed') return _psSeed(id);
            if (act === 'seed-upload') return _psSeedUpload(id);
            // The Cancel button's own listener (in _psSeedRender) already set
            // _psSeedCancel; the bubble-phase dispatcher must do nothing more.
            if (act === 'seed-cancel') return;
            if (act === 'seed-close') {
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; h.dataset.openAct = ''; }
                _psSeedCancel = false;
                return;
            }
            if (act === 'login-gate-save') return _psLoginGateSave(id);
            if (act === 'login-gate-close') {
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; h.dataset.openAct = ''; }
                return;
            }
            if (act === 'secrets-close') {
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; h.dataset.openAct = ''; }
                return;
            }
            if (act === 'ha-save') return _psHaSave(id);
            if (act === 'clone') return _psCloneStack(id, b.dataset.psName || '');
            if (act === 'def-render-preview') {
                return _psRenderPreview(
                    document.getElementById(`ps-def-compose-${id}`),
                    document.getElementById(`ps-def-render-preview-${id}`),
                    b);
            }
            if (act === 'def-save') return _psDefSave(id, { redeploy: false });
            if (act === 'def-save-deploy') return _psDefSave(id, { redeploy: true });
            if (act === 'def-diff') return _psDefDiff(id);
            if (act === 'def-revert') return _psDefRevert(id);
            if (act === 'def-diff-close') {
                const o = document.getElementById(`ps-def-diff-${id}`);
                if (o) o.innerHTML = '';
                return;
            }
            if (act === 'def-close') {
                _psCancelLogPoller(id);
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; h.dataset.openAct = ''; }
                return;
            }
            if (act === 'logs') return _psLogs(id);
            if (act === 'logs-show-sidecars') return _psToggleShowSidecars(id, b.checked);
            if (act === 'logs-live') return _psToggleLogLive(id);
            if (act === 'logs-now') return _psLogPollTick(id);
            if (act === 'logs-close') {
                _psCancelLogPoller(id);
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) h.innerHTML = '';
                return;
            }
            if (act === 'copylogs') return _psCopyLogs(id, b);
            if (act === 'rehearse') return _psRehearse(id, b.dataset.psSnap);
            if (act === 'set-schedule') return _psSetSchedule(id, false);
            if (act === 'clear-schedule') return _psSetSchedule(id, true);
            if (act === 'restore') {
                let refs = {};
                try { refs = JSON.parse(b.dataset.psRefs || '{}'); } catch (_) {}
                return _psRestore(id, refs);
            }
            if (act === 'pin') return _psPin(id, b.dataset.psWorker || '');
            if (act === 'pin-confirm') return _psPinConfirm(id);
            if (act === 'pin-cancel') return _psPinCancel(id);
            if (act === 'unpin') return _psUnpin(id);
            if (act === 'gpu-pin') return _psGpuPin(id);
            if (act === 'gpu-pin-confirm') return _psGpuPinConfirm(id);
            if (act === 'gpu-pin-cancel') return _psGpuPinCancel(id);
            if (act === 'gpu-unpin') return _psGpuUnpin(id);
            if (act === 'gpu-pin-toggle') return;
            if (act === 'copy') return _psCopy(b.dataset.psCopy || '');
            if (act === 'expose') return _psExpose(id);
            if (act === 'expose-toggle') return _psExposeSet(id, b.checked);
            //  (lane
            // ): the click leg claims this stack id so the input's own
            // `change` event (the same user action) does not send a second PUT.
            if (act === 'keep-running-toggle') return _psKeepRunningSet(id, b.checked);
            if (act === 'endpoints') return _psEndpoints(id);
            if (act === 'endpoints-close') {
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; h.dataset.openAct = ''; }
                return;
            }
            if (act === 'hp-save') return _psHostPortsSave(id);
            if (act === 'hp-add') return _psHostPortsAdd(id);
            if (act === 'hp-remove') return _psHostPortsRemove(id, b.dataset.psHpIdx);
            if (act === 'expose-close') {
                const h = document.getElementById(`ps-backups-${id}`);
                if (h) { h.innerHTML = ''; h.dataset.open = ''; }
                return;
            }
            if (act === 'expose-domain') {
                const v = document.getElementById(`ps-expose-domain-${id}`)?.value.trim() || '';
                return _psExposeSet(id, true, v === '' ? { clearCustom: true } : { customDomain: v });
            }
            if (act === 'expose-domain-clear') return _psExposeSet(id, true, { clearCustom: true });
            if (act === 'expose-friendly') {
                const v = document.getElementById(`ps-expose-friendly-${id}`)?.value.trim() || '';
                return _psExposeSet(id, true, v === '' ? { clearFriendly: true } : { friendlyFqdn: v });
            }
            if (act === 'expose-friendly-clear') return _psExposeSet(id, true, { clearFriendly: true });
            if (act === 'delete') return _psDelete(id);
            if (act === 'more') return _psMoreToggle(Number(id));
            if (act === 'teardown' && !confirm(`Tear down stack ${id}? Managed storage is reclaimed.`)) return;
            _psAct(id, act);
        });
        // The "More" menu closer: a click anywhere outside the open menu (or
        // Escape) closes it. On `document` so it also catches clicks on the
        // rest of the dashboard, not just the stacks table.
        document.addEventListener('click', (e) => {
            if (_psMoreOpen == null) return;
            const t = e.target;
            if (t && (t.closest(`[data-ps-menu="${_psMoreOpen}"]`) || t.closest(`[data-ps-act="more"][data-ps-id="${_psMoreOpen}"]`))) return;
            _psMoreClose();
        });
        document.addEventListener('keydown', (e) => {
            if (_psMoreOpen != null && e.key === 'Escape') _psMoreClose();
        });
        // ,
        // letter (j) (PIN12 Dell browser proof): after a window resize the open
        // panel kept its stale `position:fixed` coordinates - possibly OUTSIDE
        // the viewport - until the next 5 s poll re-applied it. Re-apply
        // immediately on resize (registered once, inside the same `wired` guard
        // as the listeners above); if the new geometry cannot be computed the
        // menu is closed instead of left floating off-screen.
        window.addEventListener('resize', () => {
            if (_psMoreOpen == null) return;
            const btn = document.querySelector(`[data-ps-act="more"][data-ps-id="${_psMoreOpen}"]`);
            const r = btn ? btn.getBoundingClientRect() : null;
            if (r && r.width && window.innerWidth && window.innerHeight) _psMoreReapply();
            else _psMoreClose();
        });
    }

    // ========================================================================
    // 7.3 — "New AI stack" modal: pick model → placement verdict → deploy
    //
    // Page-level button (beside "Create from compose") opens a modal that
    // fetches the model catalog (GET /platform/ai-config/model-catalog), shows
    // a dropdown, calls the placement-check (POST /stacks/placement-check)
    // when a model is chosen, and provisions a managed-AI stack via
    // POST /platform/managed-ai/apply with the selected preset.
    //
    // The verdict uses _managed_ai_placement logic: placeable → green
    // "Fits on <host>" + Deploy; not placeable → shortfall_gb + fits_instead
    // suggestions + Deploy disabled.
    // ========================================================================
    let _psAiCatalog = null;
    let _psAiLastVerdict = null;

    function _psCloseAiModal() {
        const el = document.getElementById('ps-ai-modal');
        if (el) el.remove();
        _psAiLastVerdict = null;
    }

    function _psShowAiModal(verdictHtml, catalog) {
        const overlay = document.createElement('div');
        overlay.id = 'ps-ai-modal';
        overlay.className = 'fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50';
        overlay.innerHTML = `<div class="relative top-12 mx-auto p-5 border w-full max-w-lg shadow-lg rounded-lg bg-white">
            <div class="flex items-center justify-between mb-4">
                <h3 class="text-lg font-semibold text-gray-700">New AI stack</h3>
                <button class="px-2 py-1 text-sm text-gray-500 hover:text-gray-700" id="ps-ai-close" aria-label="Close">&times;</button>
            </div>
            <div class="space-y-3">
                <div>
                    <label class="block text-xs font-medium text-gray-500 mb-1">Model</label>
                    <select id="ps-ai-model" aria-label="Model" class="w-full px-3 py-2 text-sm border border-gray-300 rounded-lg">
                        <option value="">— select a model —</option>
                        ${catalog.map(m => `<option value="${_psEsc(m.key)}" data-vram="${_psEsc(String(m.vram_floor_gb || m.vram_est_gb || 0))}">${_psEsc(m.label)}${m.unavailable_reason ? ' (unavailable: ' + _psEsc(m.unavailable_reason) + ')' : ''}</option>`).join('')}
                    </select>
                </div>
                <div id="ps-ai-verdict" class="text-sm hidden"></div>
                <div id="ps-ai-deploy-warning" class="hidden text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2">
                    Deploying switches the managed AI stack to <strong id="ps-ai-deploy-preset-label"></strong>; the current managed model is replaced.
                </div>
                <div class="flex gap-2 justify-end">
                    <button id="ps-ai-cancel" class="px-3 py-1.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-100">Cancel</button>
                    <button id="ps-ai-deploy" class="px-3 py-1.5 text-sm font-medium text-white bg-primary rounded-lg hover:bg-primary/90" disabled>Deploy</button>
                </div>
            </div>
        </div>`;
        overlay.addEventListener('click', (e) => { if (e.target === overlay) _psCloseAiModal(); });
        overlay.querySelector('#ps-ai-close').addEventListener('click', _psCloseAiModal);
        overlay.querySelector('#ps-ai-cancel').addEventListener('click', _psCloseAiModal);
        document.body.appendChild(overlay);

        // Populate the model selector
        const sel = overlay.querySelector('#ps-ai-model');
        sel.addEventListener('change', () => _psAiPlacementCheck(overlay));

        // Deploy handler
        overlay.querySelector('#ps-ai-deploy').addEventListener('click', () => _psAiDeploy(overlay));

        // Show the verdict HTML from the caller (if any, e.g. cached from catalog)
        if (verdictHtml) {
            const vEl = overlay.querySelector('#ps-ai-verdict');
            vEl.innerHTML = verdictHtml;
            vEl.classList.remove('hidden');
        }
    }

    async function _psAiPlacementCheck(overlay) {
        const sel = overlay.querySelector('#ps-ai-model');
        const selected = sel.value;
        const vEl = overlay.querySelector('#ps-ai-verdict');
        const deployBtn = overlay.querySelector('#ps-ai-deploy');

        if (!selected) {
            vEl.classList.add('hidden');
            deployBtn.disabled = true;
            return;
        }

        deployBtn.disabled = true;
        vEl.textContent = 'Checking placement…';
        vEl.classList.remove('hidden');
        vEl.className = 'text-sm text-gray-400';

        // Find model info from catalog
        const model = _psAiCatalog?.find(m => m.key === selected);
        if (!model) {
            vEl.textContent = 'Placement check unavailable: unknown model key';
            vEl.className = 'text-sm text-destructive';
            return;
        }

        const vramGb = parseFloat(model.vram_floor_gb || model.vram_est_gb || 0) || 1;

        // Show deploy warning when this model maps to a preset.
        const preset = model.preset || null;
        const warningEl = overlay.querySelector('#ps-ai-deploy-warning');
        if (warningEl) {
            if (preset) {
                overlay.querySelector('#ps-ai-deploy-preset-label').textContent = preset;
                warningEl.classList.remove('hidden');
            } else {
                warningEl.classList.add('hidden');
            }
        }

        try {
            const r = await apiCall('/stacks/placement-check', {
                method: 'POST',
                body: JSON.stringify({
                    services: [{
                        name: selected,
                        accelerator: { kind: 'any', count: 1, min_vram_gb: vramGb }
                    }]
                }),
            });
            _psAiLastVerdict = r;

            if (r.placeable && r.fits_on?.length) {
                vEl.innerHTML = `<span class="text-success">Fits on ${r.fits_on.map(_psEsc).join(', ')}</span>`;
                vEl.className = 'text-sm';
                deployBtn.disabled = !preset;
            } else {
                const shortfall = r.shortfall_gb != null ? ` <strong>${r.shortfall_gb} GB</strong>` : '';
                let html = `<span class="text-destructive">Not placeable${shortfall}</span>`;
                if (r.fits_instead) {
                    const names = r.fits_instead.map(f => _psEsc(f.preset || f.name || '?')).join(', ');
                    html += ` <span class="text-gray-500">(${names} would fit)</span>`;
                }
                if (r.reason) {
                    html += ` <br><span class="text-gray-400">${_psEsc(r.reason)}</span>`;
                }
                vEl.innerHTML = html;
                vEl.className = 'text-sm';
                deployBtn.disabled = true;
            }
        } catch (e) {
            vEl.textContent = `Placement check unavailable: ${e.message || e}`;
            vEl.className = 'text-sm text-destructive';
        }
    }

    async function _psAiDeploy(overlay) {
        const sel = overlay.querySelector('#ps-ai-model');
        const selectedKey = sel.value;
        if (!selectedKey) return;

        const model = _psAiCatalog?.find(m => m.key === selectedKey);
        const preset = model?.preset || null;

        const deployBtn = overlay.querySelector('#ps-ai-deploy');
        deployBtn.disabled = true;
        deployBtn.textContent = 'Deploying…';

        try {
            if (preset) {
                await apiCall('/platform/managed-ai/apply', {
                    method: 'POST',
                    body: JSON.stringify({ preset }),
                });
                _psMsg(`AI stack provisioned for ${_psEsc(selectedKey)} (preset ${_psEsc(preset)}).`, true);
            } else {
                _psMsg('Per-model deploy is not available yet — use \'Create from compose\' with the model\'s stack.', false);
                deployBtn.disabled = false;
                deployBtn.textContent = 'Deploy';
                return;
            }
            _psCloseAiModal();
            _psLoadStacks();
        } catch (e) {
            _psMsg(`Deploy failed: ${e.message || e}`, false);
            deployBtn.disabled = false;
            deployBtn.textContent = 'Deploy';
        }
    }

    async function _psNewAIStack() {
        // Owner decision 11-10: CE shows this button, but the paid managed-AI one-click
        // provisioner does not ship there. On CE the click opens the existing 'Create from
        // compose' flow — the SAME handler the compose button calls (no duplicated logic,
        // no paid endpoint). Paid keeps the modal below, byte-identical.
        let ce = false;
        try {
            ce = await _psIsCeEdition();
        } catch (e) {
            ce = false;
        }
        if (ce) {
            _psMsg('Community Edition provisions AI stacks from a compose file: paste one in the \'Create from compose\' box, then click New AI stack again.', true);
            return _psComposeCreateClick();
        }
        try {
            if (!_psAiCatalog) {
                const cat = await apiCall('/platform/ai-config/model-catalog');
                _psAiCatalog = Array.isArray(cat) ? cat : (cat && cat.models) || [];
            }
            if (!Array.isArray(_psAiCatalog) || _psAiCatalog.length === 0) {
                _psMsg('Model catalog is empty — no models available.', false);
                return;
            }
            _psShowAiModal(null, _psAiCatalog);
        } catch (e) {
            _psMsg('Model catalog unavailable: ' + (e.message || e), false);
        }
    }

    // Platform-admin only. The nav link is hidden and the backend 403s; this
    // also skips the data fetches on a non-admin direct hash-nav so the page
    // shows a clean restricted note instead of 403 error spam.
    me.get().then(u => {
        if (!u || u.is_platform_admin !== true) {
            _psMsg('Persistent stacks are restricted to platform administrators.', false);
            return;
        }
        _psMsg('', true);
        // 2400LR-cdu (owner decision 11-10): the "New AI stack" button stays VISIBLE on CE —
        // nothing here hides or re-points it. Its click handler (_psNewAIStack) routes CE to
        // the existing 'Create from compose' flow instead of the paid managed-AI modal, so the
        // paid deploy path (POST /platform/managed-ai/apply) is never reached from CE.
        _psLoadTemplates();
        _psLoadStacks().then(() => _psHighlightStackFromQuery());
        _psLoadHealth();
        _psLoadRebalancer();
        _psLoadGrants();
        _psLoadNetworkingUsage();
    }).catch(() => {});
}
