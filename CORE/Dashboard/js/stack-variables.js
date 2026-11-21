/**
 * GX.S10c — Stack variables & secrets (Dashboard side).
 *
 * Two mounts for persistent-stacks.js:
 *   mountVariablesPanel(hostEl, getComposeText, {stackId?})
 *       Live "Variables & secrets" panel for Create-from-compose / Edit
 *       definition. Polls the compose text, calls POST
 *       /stacks/compose/variables/detect (debounced 400 ms) and renders one
 *       row per placeholder with a type dropdown + value widget. Exposes
 *       isComplete() / getVariablesPayload() for the save-path gating.
 *       If the detect endpoint 404s the panel degrades to a one-liner and
 *       the payload stays empty (older API, nothing to send).
 *   mountSecretsTab(hostEl, stackId, {onDeployed?})
 *       "Secrets & variables" list for a deployed stack: GET
 *       /stacks/{id}/variables, Set/Rotate via PUT
 *       /stacks/{id}/variables/{name}, then a "Redeploy to apply" banner
 *       that posts the same /stacks/{id}/deploy the Deploy button uses.
 *
 * Contract: the internal design notes
 * gx-s10-stack-variables-secrets.md §3 + §5. Values are never logged.
 */
import { fetchApi as apiCall } from './api.js';

function _svEsc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g,
        c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _svSize(bytes) {
    if (bytes == null) return '';
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return '';
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
    return `${(n / 1073741824).toFixed(1)} GB`;
}

function _svRel(iso) {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return '';
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
}

const SV_KIND_LABEL = {
    plain: 'Plain value',
    secret_env: 'Secret → environment variable',
    secret_file: 'Secret → file',
    storage_file: 'File from my storage',
    existing: 'Use existing secret',
};

const SV_KINDS = ['plain', 'secret_env', 'secret_file', 'storage_file', 'existing'];

// GX.S10c — client-side mirror of the platform setting `stacks.secret_file_max_bytes`
// (default 1 MiB). The SERVER is authoritative: this only avoids reading/uploading
// a file the server would reject anyway; a client that skips it still gets a 422.
const SV_SECRET_FILE_MAX_BYTES = 1048576;

function _svB64FromBytes(bytes) {
    // Chunked so `String.fromCharCode.apply` never blows the arg limit and no
    // giant string is built with `+=` before btoa.
    const CHUNK = 0x8000;
    const parts = [];
    for (let i = 0; i < bytes.length; i += CHUNK) {
        parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK)));
    }
    return btoa(parts.join(''));
}

// ============================================================================
// Variables panel
// ============================================================================

export function mountVariablesPanel(hostEl, getComposeText, opts = {}) {
    const state = {
        rows: {},          // name -> row state
        order: [],         // names in detect order
        loaded: false,     // detect has succeeded at least once
        unsupported: false,
        missing: new Set(),
        lastText: null,
        deb: null,
        poll: null,
        catalog: null,
    };

    const rowDefaults = (v) => ({
        name: v.name,
        syntax: v.syntax || 'angle',
        occurrences: Array.isArray(v.occurrences) ? v.occurrences : [],
        default: (v.default != null ? String(v.default) : ''),
        declared: v.declared === true,
        kind: v.suggested_kind === 'secret_env' ? 'secret_env' : 'plain',
        value: '',
        file: `/run/secrets/${String(v.name).toLowerCase()}`,
        mode: '0400',
        fromCog: null,     // {cog_id, name, size, folder} for secret_file snapshot
        b64: false,        // GX.S10c: value holds BASE64 of a picked binary file
        fileName: '',      // display only — never the content
        fileSize: null,    // display only
        cog: null,         // {cog_id, name, size, folder} for storage_file
        mount: '',         // storage_file mount path, default /data/<name>
        existingPath: '',
    });

    const text = () => {
        try { return getComposeText() || ''; } catch (_) { return ''; }
    };

    const rowServices = (r) => [...new Set(r.occurrences.map(o => o && o.service).filter(Boolean))];

    function rowResolved(r) {
        if (r.declared) return true;
        switch (r.kind) {
            case 'plain':
            case 'secret_env':
                return r.value !== '';
            case 'secret_file':
                return r.value !== '' || !!r.fromCog;
            case 'storage_file':
                return !!r.cog;
            case 'existing':
                return !!r.existingPath;
            default:
                return false;
        }
    }

    function payloadFor(r) {
        const services = rowServices(r);
        switch (r.kind) {
            case 'plain':
                return { kind: 'plain', value: r.value };
            case 'secret_env':
                return { kind: 'secret_env', value: r.value, services: services.length ? services : undefined };
            case 'secret_file': {
                const p = {
                    kind: 'secret_file',
                    file: r.file || `/run/secrets/${String(r.name).toLowerCase()}`,
                    mode: r.mode || '0400',
                    services: services.length ? services : undefined,
                };
                if (r.fromCog) p.from_cog_id = r.fromCog.cog_id;
                else { p.value = r.value; if (r.b64) p.b64 = true; }
                return p;
            }
            case 'storage_file':
                return {
                    kind: 'storage_file',
                    cog_id: r.cog.cog_id,
                    file: r.mount || `/data/${r.cog.name}`,
                    services: services.length ? services : undefined,
                };
            case 'existing':
                return { kind: 'secret_env', existing_path: r.existingPath };
            default:
                return null;
        }
    }

    // ---- rendering ---------------------------------------------------------

    function render() {
        const scope = hostEl;
        const rows = state.order.map(n => state.rows[n]).filter(Boolean);
        const nSecret = rows.filter(r => ['secret_env', 'secret_file', 'existing'].includes(r.kind)).length;
        const nFile = rows.filter(r => r.kind === 'storage_file').length;
        const nPlain = rows.filter(r => r.kind === 'plain').length;

        let summary;
        if (!state.loaded) {
            summary = '<span class="text-gray-400">Scan your compose for &lt;PLACEHOLDERS&gt;…</span>';
        } else if (!rows.length) {
            summary = '<span class="text-gray-500">No placeholders found — nothing to fill in.</span>';
        } else {
            const bits = [];
            if (nSecret) bits.push(`${nSecret} secret${nSecret === 1 ? '' : 's'}`);
            if (nFile) bits.push(`${nFile} file${nFile === 1 ? '' : 's'}`);
            if (nPlain) bits.push(`${nPlain} plain`);
            summary = `<span class="text-gray-600">${_svEsc(bits.join(', '))}</span>`;
        }

        let rowsHtml = '';
        for (const r of rows) {
            const resolved = rowResolved(r);
            const missing = state.missing.has(r.name);
            const services = rowServices(r);
            const ring = missing ? 'border-destructive bg-red-50'
                : (resolved ? 'border-gray-200' : 'border-amber-300 bg-amber-50');
            const chips = services.length
                ? services.map(s => `<span class="px-1.5 py-0.5 rounded bg-gray-100 text-gray-600">${_svEsc(s)}</span>`).join(' ')
                : '';
            rowsHtml += `
            <div data-sv-row data-testid="ps-var-row" data-var-name="${_svEsc(r.name)}" class="mb-2 rounded-lg border p-2 ${ring}">
                <div class="flex flex-wrap items-center gap-2">
                    <code class="text-sm font-semibold text-gray-800">${_svEsc(r.name)}</code>
                    ${r.declared ? '<span class="px-1.5 py-0.5 rounded bg-green-100 text-green-700 text-xs" title="Already set in this compose\'s x-mod.variables">already set</span>'
                        : (resolved ? '<span class="px-1.5 py-0.5 rounded bg-green-100 text-green-700 text-xs">set</span>'
                        : `<span class="px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-xs font-semibold" data-sv-required>required</span>`)}
                    ${missing ? '<span class="px-1.5 py-0.5 rounded bg-destructive text-white text-xs">missing on save — fill this in</span>' : ''}
                    <span class="text-xs text-gray-500">used in:</span> ${chips || '<span class="text-xs text-gray-400">—</span>'}
                    <select data-sv-kind data-var-name="${_svEsc(r.name)}" aria-label="${_svEsc(r.name)} variable type" class="ml-auto px-2 py-1 text-xs border border-gray-300 rounded-lg bg-white">
                        ${SV_KINDS.map(k => `<option value="${k}" ${k === r.kind ? 'selected' : ''}>${_svEsc(SV_KIND_LABEL[k])}</option>`).join('')}
                    </select>
                </div>
                <div class="mt-2" data-sv-widget>${widgetHtml(r)}</div>
            </div>`;
        }

        let advHtml = '';
        for (const r of rows) {
            if (['secret_env', 'secret_file', 'existing'].includes(r.kind)) {
                advHtml += `<div class="mb-1"><code>${_svEsc(r.name)}</code> → Vault <code>…/vars/${_svEsc(r.name)}</code> (path managed by MOD)</div>`;
            }
            if (r.kind === 'secret_file') advHtml += `<div class="mb-1"><code>${_svEsc(r.name)}</code> mounts at <code>${_svEsc(r.file)}</code> mode ${_svEsc(r.mode)}</div>`;
            if (r.kind === 'storage_file' && r.cog) advHtml += `<div class="mb-1"><code>${_svEsc(r.name)}</code> cog ${_svEsc(r.cog.cog_id)} mounts at <code>${_svEsc(r.mount || '/data/' + r.cog.name)}</code></div>`;
        }
        advHtml = advHtml || '<div class="text-xs text-gray-400">Nothing to show.</div>';

        scope.innerHTML = `
            <div class="text-xs text-gray-500 mb-2">Secrets are stored in Vault and never shown again. Files are mounted read-only inside the container.</div>
            ${state.unsupported
                ? '<div class="text-sm text-amber-700 bg-amber-50 border border-amber-300 rounded-lg px-3 py-2">Variables need a newer API. The compose will be saved as-is without placeholder resolution.</div>'
                : `<div data-testid="ps-vars-summary" class="flex flex-wrap items-center gap-2 mb-2 text-xs">${summary}</div>
                   <div data-sv-rows>${rowsHtml}</div>
                   <details class="mt-2"><summary class="cursor-pointer select-none text-xs text-gray-500">Advanced</summary><div class="mt-1 text-xs text-gray-500 font-mono pl-2">${advHtml}</div></details>`}
            <div data-sv-msg class="mt-2 text-xs text-destructive hidden"></div>`;
    }

    function catalogListHtml(r) {
        const q = (r._catalogFilter || '').toLowerCase();
        const items = (state.catalog || []).filter(c =>
            !q || (String(c.name) + ' ' + String(c.path) + ' ' + String(c.stack_name || '')).toLowerCase().includes(q));
        return items.slice(0, 50).map(c => `
            <div data-sv-catalog-item data-path="${_svEsc(c.path)}" class="px-2 py-1 hover:bg-gray-100 cursor-pointer text-xs">
                <code>${_svEsc(c.name)}</code> <span class="text-gray-400">(${_svEsc(c.stack_name || '?')})${c.updated_at ? ` · ${_svEsc(_svRel(c.updated_at))}` : ''}</span>
            </div>`).join('') || '<div class="px-2 py-1 text-xs text-gray-400">No existing secrets found.</div>';
    }

    function widgetHtml(r) {
        if (r.declared) {
            return '<div class="text-xs text-gray-400">This name is already declared in the compose\'s x-mod.variables block — nothing to enter.</div>';
        }
        switch (r.kind) {
            case 'plain':
                return `<input data-sv-value data-var-name="${_svEsc(r.name)}" type="text" aria-label="${_svEsc(r.name)} value" value="${_svEsc(r.value)}"
                    placeholder="value" class="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-lg bg-white" />`;
            case 'secret_env': {
                const eye = `<button type="button" data-sv-eye data-var-name="${_svEsc(r.name)}" aria-label="Show or hide ${_svEsc(r.name)} secret value" class="ml-1 px-2 py-1 text-xs bg-gray-100 rounded hover:bg-gray-200" title="Show / hide">👁</button>`;
                return `<div class="flex items-center">
                    <input data-sv-value data-var-name="${_svEsc(r.name)}" type="password" aria-label="${_svEsc(r.name)} secret value" value="${_svEsc(r.value)}"
                        autocomplete="off" placeholder="secret value" class="w-full px-2 py-1.5 text-sm border border-gray-300 rounded-lg bg-white" />${eye}</div>`;
            }
            case 'secret_file': {
                // GX.S10c: a file picked from disk is kept as base64 in r.value
                // (r.b64). Its content NEVER goes into the DOM — text or an
                // attribute — so the textarea is replaced by a read-only line.
                const widget = r.b64
                    ? `<div data-sv-loaded data-var-name="${_svEsc(r.name)}" class="text-xs text-gray-700">${_svEsc(r.fileName)} (${_svSize(r.fileSize)}) loaded — <button type="button" data-sv-clear-file data-var-name="${_svEsc(r.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200" aria-label="Clear loaded file">Clear</button></div>`
                    : `<textarea data-sv-value data-var-name="${_svEsc(r.name)}" aria-label="${_svEsc(r.name)} file content" rows="2" spellcheck="false"
                    placeholder="file content (or pick a file below)" class="w-full px-2 py-1.5 text-xs font-mono border border-gray-300 rounded-lg bg-white">${_svEsc(r.value)}</textarea>`;
                return `
                ${widget}
                <div class="flex flex-wrap items-center gap-2 mt-1">
                    <button type="button" data-sv-upload data-var-name="${_svEsc(r.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Upload file…</button>
                    <input type="file" data-sv-fileinput data-var-name="${_svEsc(r.name)}" aria-label="${_svEsc(r.name)} file upload" class="hidden" />
                    <button type="button" data-sv-pick data-var-name="${_svEsc(r.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Choose from my files…</button>
                    ${r.fromCog ? `<span class="text-xs text-gray-600">snapshot source: <strong>${_svEsc(r.fromCog.name)}</strong> (${_svEsc(_svSize(r.fromCog.size))}) from ${_svEsc(r.fromCog.folder)} — copied into Vault at save time</span>` : ''}
                    ${r.fromCog ? '<span class="text-xs text-gray-400">(pasted/typed content is ignored while a source file is chosen)</span>' : ''}
                    <label class="ml-auto text-xs text-gray-600">path</label>
                    <input data-sv-secret-path data-var-name="${_svEsc(r.name)}" type="text" aria-label="${_svEsc(r.name)} mount path" value="${_svEsc(r.file)}"
                        class="w-56 px-2 py-1 text-xs font-mono border border-gray-300 rounded-lg bg-white" />
                </div>
                <div class="text-xs text-gray-400 mt-1">Mounted read-only at the path above. "Choose from my files…" copies the file's bytes into Vault as a snapshot.</div>`;
            }
            case 'storage_file':
                return `
                <div class="flex flex-wrap items-center gap-2">
                    <button type="button" data-sv-pick data-var-name="${_svEsc(r.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Choose from my files…</button>
                    ${r.cog ? `<span class="text-xs text-gray-700">selected: <strong>${_svEsc(r.cog.name)}</strong> (${_svEsc(_svSize(r.cog.size))}) in ${_svEsc(r.cog.folder)}</span>`
                           : '<span class="text-xs text-gray-500">No file chosen — the stack deploys the file\'s CURRENT version each time.</span>'}
                    <label class="ml-auto text-xs text-gray-600">mount</label>
                    <input data-sv-path data-var-name="${_svEsc(r.name)}" type="text" aria-label="${_svEsc(r.name)} mount path" value="${_svEsc(r.mount)}"
                        placeholder="/data/&lt;file name&gt;" class="w-56 px-2 py-1 text-xs font-mono border border-gray-300 rounded-lg bg-white" />
                </div>
                <div class="text-xs text-gray-400 mt-1">Mounted read-only; the placeholder is replaced with the path inside the container.</div>`;
            case 'existing':
                return `
                <div class="flex items-center gap-2">
                    <input data-sv-catalog-q data-var-name="${_svEsc(r.name)}" type="text" aria-label="Search existing secrets for ${_svEsc(r.name)}" placeholder="search existing secrets…"
                        value="${_svEsc(r._catalogFilter || '')}" class="w-56 px-2 py-1 text-xs border border-gray-300 rounded-lg bg-white" />
                    ${r.existingPath ? `<span class="text-xs text-gray-700">chosen: <code>${_svEsc(r.existingPath)}</code> <button type="button" data-sv-catalog-clear data-var-name="${_svEsc(r.name)}" class="text-xs text-gray-400 hover:text-destructive" aria-label="Clear">✕</button></span>`
                                    : '<span class="text-xs text-gray-500">pick one from your tenant\'s existing secrets</span>'}
                </div>
                <div data-sv-catalog-list data-var-name="${_svEsc(r.name)}" class="max-h-40 overflow-y-auto border border-gray-200 rounded-lg mt-1 bg-white">${catalogListHtml(r)}</div>`;
            default:
                return '';
        }
    }

    // ---- detect ------------------------------------------------------------

    async function detect() {
        const compose = text();
        if (!compose.trim()) {
            state.rows = {}; state.order = []; state.loaded = false;
            render();
            return;
        }
        try {
            const r = await apiCall('/stacks/compose/variables/detect', {
                method: 'POST', body: JSON.stringify({ compose }),
            });
            if (text() !== compose) return; // compose moved on; stale response
            const vars = (r && Array.isArray(r.variables)) ? r.variables : [];
            const next = {};
            const order = [];
            for (const v of vars) {
                if (!v || !v.name) continue;
                const prev = state.rows[v.name];
                next[v.name] = prev || rowDefaults(v);
                // Refresh the metadata but keep anything the operator entered.
                const row = next[v.name];
                row.syntax = v.syntax || row.syntax;
                row.occurrences = Array.isArray(v.occurrences) ? v.occurrences : row.occurrences;
                row.declared = v.declared === true;
                if (!row.declared && v.default != null && row.value === '') row.value = String(v.default);
                if (!row.mount && row.kind === 'storage_file' && row.cog) row.mount = `/data/${row.cog.name}`;
                order.push(v.name);
            }
            state.rows = next;
            state.order = order;
            state.loaded = true;
            state.unsupported = false;
            render();
        } catch (e) {
            if (e && e.status === 404) {
                state.loaded = false;
                state.unsupported = true;
            }
            render();
        }
        if (opts.onStateChange) opts.onStateChange();
    }

    function scheduleDetect() {
        clearTimeout(state.deb);
        state.deb = setTimeout(detect, 400);
    }

    // ---- events ------------------------------------------------------------

    function rowOf(el) {
        const name = el && (el.dataset.varName || el.closest('[data-sv-row]')?.dataset.varName);
        return name ? state.rows[name] : null;
    }

    hostEl.addEventListener('input', (e) => {
        const el = e.target;
        const r = rowOf(el);
        if (!r) return;
        if (el.hasAttribute('data-sv-value')) {
            r.value = el.value;
            // A typed value is TEXT — drop any loaded-file state it came with.
            if (r.b64 && el.tagName === 'TEXTAREA') {
                r.b64 = false; r.fileName = ''; r.fileSize = null;
            }
        }
        else if (el.hasAttribute('data-sv-secret-path')) r.file = el.value;
        else if (el.hasAttribute('data-sv-path')) r.mount = el.value;
        else if (el.hasAttribute('data-sv-catalog-q')) {
            // Re-render only the list, keeping the search input focused.
            r._catalogFilter = el.value.toLowerCase();
            const list = hostEl.querySelector(`[data-sv-catalog-list][data-var-name="${CSS.escape(r.name)}"]`);
            const wrap = hostEl.querySelector(`[data-sv-row][data-var-name="${CSS.escape(r.name)}"] [data-sv-widget]`);
            const target = list || (wrap && wrap.querySelector('[data-sv-catalog-list]'));
            if (target) target.innerHTML = catalogListHtml(r);
            return;
        }
        if (state.missing.has(r.name) && rowResolved(r)) state.missing.delete(r.name);
        // Light touch: keep focus in the input, refresh the row's ring + chip
        // in place. A full render() here (as the old change-handler did on
        // blur) would replace this very input mid-fill and drop the value.
        const rowsEl = hostEl.querySelector('[data-sv-rows]');
        if (rowsEl) {
            const wrap = rowsEl.querySelector(`[data-sv-row][data-var-name="${CSS.escape(r.name)}"]`);
            if (wrap) {
                const resolved = rowResolved(r);
                const ring = state.missing.has(r.name) ? 'border-destructive bg-red-50'
                    : (resolved ? 'border-gray-200' : 'border-amber-300 bg-amber-50');
                wrap.className = `mb-2 rounded-lg border p-2 ${ring}`;
                const chip = wrap.querySelector('[data-sv-required]');
                if (resolved) { if (chip) chip.outerHTML = '<span class="px-1.5 py-0.5 rounded bg-green-100 text-green-700 text-xs">set</span>'; }
                else if (!chip) {
                    const setChip = wrap.querySelector('.bg-green-100');
                    if (setChip) setChip.outerHTML = '<span class="px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-xs font-semibold" data-sv-required>required</span>';
                }
            }
        }
        if (opts.onStateChange) opts.onStateChange();
    });

    hostEl.addEventListener('change', (e) => {
        const el = e.target;
        const r = rowOf(el);
        if (!r) return;
        if (el.hasAttribute('data-sv-kind')) {
            r.kind = el.value;
            // A loaded binary file (base64) only makes sense as secret_file; never carry it into another kind's input.
            if (r.b64 && r.kind !== 'secret_file') { r.value = ''; r.b64 = false; r.fileName = ''; r.fileSize = null; }
            if (r.kind === 'storage_file' && !r.mount) r.mount = r.cog ? `/data/${r.cog.name}` : '';
            render();
        }
        // Only the kind select repaints here. A value input firing 'change'
        // (its blur) is already handled by the 'input' handler, which updates
        // state + the chip in place; re-rendering on blur would replace the
        // input mid-fill (Playwright focuses the next field before setting its
        // value) and silently drop the value just typed.
        if (opts.onStateChange) opts.onStateChange();
    });

    hostEl.addEventListener('click', (e) => {
        const el = e.target;
        if (el.hasAttribute('data-sv-eye')) {
            const r = rowOf(el);
            const inp = r && hostEl.querySelector(`[data-sv-value][data-var-name="${CSS.escape(r.name)}"]`);
            if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';
            return;
        }
        if (el.hasAttribute('data-sv-upload')) {
            const fi = hostEl.querySelector(`[data-sv-fileinput][data-var-name="${CSS.escape(el.dataset.varName)}"]`);
            if (fi) fi.click();
            return;
        }
        if (el.hasAttribute('data-sv-pick')) {
            const r = rowOf(el);
            if (!r) return;
            if (r.kind === 'storage_file') {
                _svOpenFilePicker((pick) => {
                    r.cog = pick;
                    if (!r.mount) r.mount = `/data/${pick.name}`;
                    render();
                    if (opts.onStateChange) opts.onStateChange();
                });
            } else {
                _svOpenFilePicker((pick) => {
                    r.fromCog = pick;
                    render();
                    if (opts.onStateChange) opts.onStateChange();
                });
            }
            return;
        }
        if (el.hasAttribute('data-sv-catalog-item')) {
            const r = rowOf(el);
            if (!r) return;
            r.existingPath = el.dataset.path || '';
            render();
            if (opts.onStateChange) opts.onStateChange();
            return;
        }
        if (el.hasAttribute('data-sv-catalog-clear')) {
            const r = rowOf(el);
            if (!r) return;
            r.existingPath = '';
            render();
            if (opts.onStateChange) opts.onStateChange();
            return;
        }
        if (el.hasAttribute('data-sv-clear-file')) {
            const r = rowOf(el);
            if (!r) return;
            r.value = ''; r.b64 = false; r.fileName = ''; r.fileSize = null;
            render();
            if (opts.onStateChange) opts.onStateChange();
            return;
        }
    });

    hostEl.addEventListener('change', (e) => {
        const el = e.target;
        if (!el.hasAttribute('data-sv-fileinput')) return;
        const r = rowOf(el);
        const f = el.files && el.files[0];
        if (!r || !f) return;
        const msg = hostEl.querySelector('[data-sv-msg]');
        // GX.S10c: cap BEFORE reading (the server re-checks on save), and read
        // as BYTES — a binary license/key file must not be corrupted to U+FFFD
        // by readAsText. We store base64 + r.b64; the content never hits the DOM.
        if (Number(f.size) > SV_SECRET_FILE_MAX_BYTES) {
            if (msg) {
                msg.textContent =
                    `${f.name} is ${_svSize(f.size)}; secret files are capped at ` +
                    `${_svSize(SV_SECRET_FILE_MAX_BYTES)} (stacks.secret_file_max_bytes).`;
                msg.classList.remove('hidden');
            }
            return;
        }
        if (msg) msg.classList.add('hidden');
        const reader = new FileReader();
        reader.onload = () => {
            const buf = reader.result;
            r.value = _svB64FromBytes(
                buf instanceof Uint8Array ? buf : new Uint8Array(buf || []));
            r.b64 = true;
            r.fileName = f.name;
            r.fileSize = f.size;
            render();
            if (opts.onStateChange) opts.onStateChange();
        };
        reader.onerror = () => {
            if (msg) { msg.textContent = 'Could not read that file.'; msg.classList.remove('hidden'); }
        };
        reader.readAsArrayBuffer(f);
    }, true);

    // Lazy-load the catalog only when a row is on the "existing" widget, so a
    // 404 from an older API on this extra endpoint can't break the main panel.
    async function ensureCatalog() {
        if (state.catalog !== null) return state.catalog;
        try {
            const r = await apiCall('/stacks/secrets/catalog');
            state.catalog = Array.isArray(r) ? r : (r && Array.isArray(r.secrets) ? r.secrets : []);
        } catch (_) {
            state.catalog = [];
        }
        return state.catalog;
    }

    // ---- lifecycle ---------------------------------------------------------

    // The compose textarea is edited (or written programmatically, e.g. Copy…)
    // outside this module's control, so poll its text cheaply and debounce the
    // detect call; self-clear once the panel row is torn out of the DOM.
    const iv = setInterval(() => {
        if (!hostEl.isConnected) { clearInterval(iv); return; }
        const t = text();
        if (t !== state.lastText) {
            state.lastText = t;
            scheduleDetect();
        }
        if (state.order.some(n => state.rows[n] && state.rows[n].kind === 'existing' && state.catalog === null)) {
            ensureCatalog().then(() => { render(); });
        }
    }, 400);
    state.poll = iv;

    render();
    scheduleDetect();

    const ctrl = {
        host: hostEl,
        isComplete: () => {
            if (!state.loaded || state.unsupported) return true;
            return state.order.every(n => {
                const r = state.rows[n];
                return r && rowResolved(r);
            });
        },
        getVariablesPayload: () => {
            if (!state.loaded || state.unsupported) return {};
            const out = {};
            for (const n of state.order) {
                const r = state.rows[n];
                if (!r || r.declared || !rowResolved(r)) continue;
                const p = payloadFor(r);
                if (p) out[n] = p;
            }
            return out;
        },
        knownNames: () => state.order.slice(),
        markMissing: (names) => {
            if (!names || !names.length) return;
            for (const n of names) if (state.rows[n]) state.missing.add(n);
            const msg = hostEl.querySelector('[data-sv-msg]');
            if (msg) {
                msg.textContent = `The API says these are still unresolved: ${names.join(', ')}`;
                msg.classList.remove('hidden');
            }
            render();
            if (opts.onStateChange) opts.onStateChange();
        },
        destroy: () => { clearInterval(iv); clearTimeout(state.deb); hostEl.innerHTML = ''; },
    };
    return ctrl;
}

// Pull the missing variable names out of a save 422, matching them against the
// panel's known names so whatever shape the API sends (string, FastAPI field
// list, or a dict) lands as row highlights.
export function svMissingNames(err, knownNames) {
    if (!knownNames || !knownNames.length) return [];
    const d = err && err.detail;
    const names = new Set();
    const hay = [];
    if (Array.isArray(d)) {
        for (const item of d) hay.push(JSON.stringify((item && item.loc) || []), (item && item.msg) || '');
    } else if (d && typeof d === 'object') {
        hay.push(d.missing ? JSON.stringify(d.missing) : '', d.names ? JSON.stringify(d.names) : '', d.message || '');
    } else if (typeof d === 'string') {
        hay.push(d);
    }
    hay.push((err && err.message) || '');
    const text = hay.join(' ');
    for (const n of knownNames) if (text.includes(n)) names.add(n);
    return [...names];
}

// ============================================================================
// "Choose from my files…" picker — reuses the Files page's listing endpoint
// (GET /assets/ with optional parent_id) but renders its own lightweight
// modal: folders navigate, files are picked.
// ============================================================================

function _svOpenFilePicker(onPick) {
    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center';
    overlay.innerHTML = `
        <div class="relative mx-auto p-5 border w-11/12 max-w-2xl shadow-lg rounded-md bg-white">
            <div class="flex justify-between items-center pb-3 border-b border-gray-200">
                <h3 class="text-lg font-semibold text-gray-900">Choose from my files…</h3>
                <button type="button" data-sv-pick-close class="text-gray-400 hover:text-gray-600 text-2xl leading-none" title="Close" aria-label="Close">&times;</button>
            </div>
            <div data-sv-pick-crumb class="text-sm text-gray-700 mt-3 mb-2 font-medium"></div>
            <div data-sv-pick-list class="max-h-80 overflow-y-auto border border-gray-200 rounded-lg text-sm"></div>
            <p class="text-xs text-gray-500 mt-2">Click a folder to open it; click a file to choose it.</p>
        </div>`;
    document.body.appendChild(overlay);

    let trail = [];
    const close = () => {
        document.removeEventListener('keydown', esc);
        overlay.remove();
    };
    const esc = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', esc);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    overlay.querySelector('[data-sv-pick-close]').addEventListener('click', close);

    const isFolder = (i) => (i && (i.cog_type || '').includes('folder'));

    async function render(parentId) {
        const list = overlay.querySelector('[data-sv-pick-list]');
        const crumb = overlay.querySelector('[data-sv-pick-crumb]');
        list.innerHTML = '<div class="p-3 text-gray-500 text-xs">Loading…</div>';
        crumb.textContent = trail.length ? 'Home / ' + trail.map(f => f.name).join(' / ') : 'Home';
        let items = [];
        try {
            items = (await apiCall(parentId ? `/assets/?parent_id=${parentId}` : '/assets/')) || [];
            if (!Array.isArray(items)) items = [];
        } catch (_) {
            list.innerHTML = '<div class="p-3 text-xs text-destructive">Could not load your files.</div>';
            return;
        }
        const folders = items.filter(isFolder);
        const files = items.filter(i => !isFolder(i));
        let html = '';
        if (trail.length) html += '<div data-sv-pick-up class="px-3 py-2 hover:bg-gray-100 cursor-pointer text-gray-600">↑ …</div>';
        for (const f of folders) {
            html += `<div data-sv-pick-folder data-id="${_svEsc(f.id)}" class="px-3 py-2 hover:bg-gray-100 cursor-pointer">📁 ${_svEsc(f.name)}</div>`;
        }
        for (const f of files) {
            html += `<div data-sv-pick-file data-id="${_svEsc(f.id)}" class="px-3 py-2 hover:bg-gray-100 cursor-pointer flex items-center justify-between gap-2">
                <span class="truncate">📄 ${_svEsc(f.name)}</span>
                <span class="text-xs text-gray-400 flex-shrink-0">${_svSize(f.size_bytes)}</span></div>`;
        }
        if (!folders.length && !files.length) {
            html += '<div class="p-3 text-xs text-gray-500">Nothing here.</div>';
        }
        list.innerHTML = html;

        const up = list.querySelector('[data-sv-pick-up]');
        if (up) up.addEventListener('click', () => {
            trail.pop();
            render(trail.length ? trail[trail.length - 1].id : null);
        });
        list.querySelectorAll('[data-sv-pick-folder]').forEach((el) => {
            el.addEventListener('click', () => {
                const f = folders.find(x => String(x.id) === el.dataset.id);
                if (!f) return;
                trail.push({ id: f.id, name: f.name });
                render(f.id);
            });
        });
        list.querySelectorAll('[data-sv-pick-file]').forEach((el) => {
            el.addEventListener('click', () => {
                const f = files.find(x => String(x.id) === el.dataset.id);
                if (!f) return;
                const folder = trail.length ? trail.map(t => t.name).join('/') : 'Home';
                close();
                onPick({ cog_id: f.id, name: f.name, size: (f.size_bytes != null ? f.size_bytes : null), folder });
            });
        });
    }

    render(null);
}

// ============================================================================
// Secrets & variables tab (stack detail)
// ============================================================================

export function mountSecretsTab(hostEl, stackId, opts = {}) {
    let needsDeploy = false;
    // GX.S10c: the tab's rows carry `kind`, so a secret_file row can rotate
    // from a file too (the tab knows the kind — see renderList below).
    const rowKinds = {};

    async function load() {
        hostEl.innerHTML = '<div class="text-sm text-gray-500">Loading…</div>';
        let rows;
        try {
            const r = await apiCall(`/stacks/${stackId}/variables`);
            rows = Array.isArray(r) ? r : (r && Array.isArray(r.variables) ? r.variables : []);
        } catch (e) {
            if (e && e.status === 404) {
                hostEl.innerHTML = '<div class="text-sm text-amber-700 bg-amber-50 border border-amber-300 rounded-lg px-3 py-2">Secrets &amp; variables need a newer API.</div>';
                return;
            }
            hostEl.innerHTML = `<div class="text-sm text-destructive">Could not load variables: ${_svEsc(e && e.message || String(e))}</div>`;
            return;
        }
        renderList(rows);
    }

    function renderList(rows) {
        let banner = '';
        if (needsDeploy) {
            banner = `
                <div class="mb-3 flex items-center gap-3 bg-blue-50 border border-blue-300 rounded-lg px-3 py-2">
                    <span class="text-xs text-blue-800">A secret changed — redeploy to apply it to the running stack.</span>
                    <button type="button" data-sv-tab-redeploy class="px-3 py-1 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Redeploy</button>
                    <button type="button" data-sv-tab-dismiss class="ml-auto text-xs text-gray-500 hover:text-gray-700">Dismiss</button>
                </div>`;
        }
        if (!rows.length) {
            hostEl.innerHTML = banner + '<div class="text-sm text-gray-500">No variables on this stack. Add placeholders to its compose and save a new revision.</div>';
            wireTab();
            return;
        }
        let html = banner + `
            <table class="w-full text-sm">
                <thead><tr class="text-left text-xs text-gray-500 border-b border-gray-200">
                    <th class="py-1.5 pr-3">Name</th><th class="py-1.5 pr-3">Type</th>
                    <th class="py-1.5 pr-3">Used by</th><th class="py-1.5 pr-3">Value</th>
                    <th class="py-1.5"></th>
                </tr></thead>
                <tbody>`;
        for (const r of rows) {
            const kind = r.kind || 'plain';
            rowKinds[String(r.name)] = kind;
            const services = Array.isArray(r.services) ? r.services.join(', ') : '';
            const by = r.updated_by ? ` by ${_svEsc(r.updated_by)}` : '';
            const val = r.has_value
                ? `•••• set ${_svEsc(_svRel(r.updated_at) || '')}${by}`
                : '<span class="text-red-600">not set</span>';
            const verb = r.has_value ? 'Rotate' : 'Set';
            html += `<tr data-sv-tab-row data-name="${_svEsc(r.name)}" class="border-b border-gray-100">
                <td class="py-1.5 pr-3 font-mono text-xs">${_svEsc(r.name)}</td>
                <td class="py-1.5 pr-3 text-xs text-gray-600">${_svEsc(SV_KIND_LABEL[kind] || kind)}</td>
                <td class="py-1.5 pr-3 text-xs text-gray-600">${_svEsc(services) || '—'}</td>
                <td class="py-1.5 pr-3 text-xs text-gray-700">${val}</td>
                <td class="py-1.5 text-right">
                    <button type="button" data-sv-tab-edit data-name="${_svEsc(r.name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">${verb}</button>
                </td></tr>`;
        }
        html += '</tbody></table>';
        hostEl.innerHTML = html;
        wireTab();
    }

    function wireTab() {
        hostEl.querySelector('[data-sv-tab-redeploy]')?.addEventListener('click', async () => {
            try {
                await apiCall(`/stacks/${stackId}/deploy`, { method: 'POST' });
                needsDeploy = false;
                if (opts.onDeployed) opts.onDeployed();
                load();
            } catch (e) {
                hostEl.insertAdjacentHTML('beforeend',
                    `<div class="mt-2 text-xs text-destructive">Redeploy failed: ${_svEsc(e && e.message || String(e))}</div>`);
            }
        });
        hostEl.querySelector('[data-sv-tab-dismiss]')?.addEventListener('click', () => {
            needsDeploy = false;
            load();
        });
        hostEl.querySelectorAll('[data-sv-tab-edit]').forEach((btn) => {
            btn.addEventListener('click', () => showEditor(btn.dataset.name));
        });
    }

    async function putValue(name, body) {
        try {
            await apiCall(`/stacks/${stackId}/variables/${encodeURIComponent(name)}`, {
                method: 'PUT', body: JSON.stringify(body),
            });
            needsDeploy = true;
            load();
        } catch (e) {
            // apiCall's ApiError.message already carries the server `detail`
            // verbatim (api.js:214) — the detail never contains the value.
            const row = hostEl.querySelector(`[data-sv-tab-row][data-name="${CSS.escape(name)}"]`);
            const td = row ? row.querySelector('td:last-child') : hostEl;
            td.insertAdjacentHTML('beforeend',
                `<div class="mt-1 text-xs text-destructive text-right">${_svEsc(e && e.message || String(e))}</div>`);
        }
    }

    async function showEditor(name) {
        const row = hostEl.querySelector(`[data-sv-tab-row][data-name="${CSS.escape(name)}"]`);
        if (!row) return;
        const td = row.querySelector('td:last-child');
        const isFile = rowKinds[String(name)] === 'secret_file';
        td.innerHTML = `<div class="flex items-center justify-end gap-1">
            <input data-sv-tab-value type="password" aria-label="New secret value for ${_svEsc(name)}" autocomplete="off" placeholder="new value"
                class="w-48 px-2 py-1 text-xs border border-gray-300 rounded-lg bg-white" />
            <button type="button" data-sv-tab-eye aria-label="Show or hide secret value" class="px-2 py-1 text-xs bg-gray-100 rounded hover:bg-gray-200" title="Show / hide">👁</button>
            ${isFile ? `<button type="button" data-sv-tab-file aria-label="Rotate from a file for ${_svEsc(name)}" class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">From file…</button>
                <input type="file" data-sv-tab-fileinput data-name="${_svEsc(name)}" aria-label="Pick a file for ${_svEsc(name)}" class="hidden" />` : ''}
            <button type="button" data-sv-tab-save class="px-2 py-1 text-xs font-medium text-white bg-primary rounded-lg hover:bg-primary/90">Save</button>
            <button type="button" data-sv-tab-cancel class="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200">Cancel</button>
        </div>`;
        const inp = td.querySelector('[data-sv-tab-value]');
        inp.focus();
        td.querySelector('[data-sv-tab-eye]').addEventListener('click', () => {
            inp.type = inp.type === 'password' ? 'text' : 'password';
        });
        td.querySelector('[data-sv-tab-cancel]').addEventListener('click', load);
        td.querySelector('[data-sv-tab-save]').addEventListener('click', async () => {
            const value = inp.value;
            if (value === '') return;
            await putValue(name, { value });
        });
        if (!isFile) return;
        // GX.S10c: rotate a file secret from disk — same binary-safe read + cap
        // as the compose panel (an <input type="file">, whose `change` event
        // hands us the real File). The bytes never enter the DOM.
        td.querySelector('[data-sv-tab-file]').addEventListener('click', () => {
            const fi = td.querySelector('[data-sv-tab-fileinput]');
            if (fi) fi.click();
        });
    }

    hostEl.addEventListener('change', (e) => {
        const el = e.target;
        if (!el.hasAttribute('data-sv-tab-fileinput')) return;
        const f = el.files && el.files[0];
        if (!f) return;
        const td = el.closest('td:last-child') || hostEl;
        if (Number(f.size) > SV_SECRET_FILE_MAX_BYTES) {
            td.insertAdjacentHTML('beforeend',
                `<div class="mt-1 text-xs text-destructive text-right">${_svEsc(`${f.name} is ${_svSize(f.size)}; secret files are capped at ${_svSize(SV_SECRET_FILE_MAX_BYTES)} (stacks.secret_file_max_bytes).`)}</div>`);
            return;
        }
        const reader = new FileReader();
        reader.onload = () => {
            const buf = reader.result;
            const b64 = _svB64FromBytes(
                buf instanceof Uint8Array ? buf : new Uint8Array(buf || []));
            void putValue(el.dataset.name, { value: b64, b64: true });
        };
        reader.onerror = () => {
            td.insertAdjacentHTML('beforeend',
                '<div class="mt-1 text-xs text-destructive text-right">Could not read that file.</div>');
        };
        reader.readAsArrayBuffer(f);
    });

    load();
    return { host: hostEl };
}
