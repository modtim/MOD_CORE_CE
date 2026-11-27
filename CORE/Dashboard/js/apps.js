// GX.A4 — Dashboard "Apps" page (tenant admin).
//
// Backend: routers/apps.py (mounted /v1/apps). An app pairs a hostname +
// OIDC client + API key with a target (a persistent-stack service or an
// external URL). The plaintext key is returned exactly ONCE — on create
// (AppCreatedOut.access_key) and on rotate (AppKeyOut.access_key) — so it
// is rendered in a copy-to-clipboard box that says it will never be shown
// again, and is NEVER logged anywhere.
//
// GX.R3 (optional, appended below) — run-completion webhook subscriptions
// (routers/workflows_domains/workflow_webhook_subscriptions.py,
// /road/{id}/webhook-subscriptions). The signing secret is also
// shown-once (create / rotate-secret) and never re-read.
//
// Modals are built inline (injected into #modal-container, cleared with
// closeModal) to follow the loadModal pattern without adding modal files.
import { apiCall } from './api.js';
import { closeModal } from './ui.js';
import { listEndpoints, createEndpoint, removeEndpoint } from './webhooks.js';

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Last successful app list (row actions re-read from here instead of
// re-fetching per action).
let _apps = [];
let _stacks = [];

// ---------------------------------------------------------------------------
// init
// ---------------------------------------------------------------------------

export async function initializeAppsPage() {
    const addWhBtn = document.getElementById('add-webhook-button');
    if (addWhBtn) addWhBtn.addEventListener('click', () => openAddWebhookModal());

    const refreshWhBtn = document.getElementById('refresh-webhooks-button');
    if (refreshWhBtn) refreshWhBtn.addEventListener('click', () => loadWebhooks());

    await Promise.all([loadApps(), loadWebhooks()]);

    const registerBtn = document.getElementById('register-app-button');
    if (registerBtn) registerBtn.addEventListener('click', () => openRegisterAppModal());
}

// ---------------------------------------------------------------------------
// apps list
// ---------------------------------------------------------------------------

function _hostnameSuffix() {
    // The API derives hostnames as <slug>.<tenant base domain>. The dashboard
    // does not know the base domain, so borrow it from an existing row's
    // hostname when possible for the slug preview.
    for (const a of _apps) {
        if (a.hostname && a.slug && a.hostname.startsWith(a.slug + '.')) {
            return a.hostname.slice(a.slug.length);
        }
    }
    return '';
}

function _statusBadge(status, failureDetail) {
    const detail = failureDetail && failureDetail.error
        ? ` title="${escapeHtml(failureDetail.error)}"` : '';
    if (status === 'active') {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">active</span>`;
    }
    if (status === 'provisioning_failed') {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-red-100 text-red-700"${detail}>provisioning_failed</span>`;
    }
    if (status === 'provisioning') {
        return `<span class="px-2 py-0.5 rounded-full text-xs bg-blue-100 text-blue-700">provisioning</span>`;
    }
    return `<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-500">${escapeHtml(status || 'unknown')}</span>`;
}

// ---------------------------------------------------------------------------
// 2400GX-cUI-1 (30.6.15) — per-App forward-auth posture on the Apps panel.
// GET /apps/ already carries auth_config (routers/apps.py:410) and edge_ready
// (:411); PUT /apps/{id}/auth-config (apps.py:1255-1289) is the write. The
// edge ask token itself is NEVER read, rendered or logged here — only the bool
// (app_forward_auth.py:210-219). fail_closed is TRI-STATE: the "follow" choice
// OMITS the key so the server derives it (app_forward_auth.py:148-156).
// ---------------------------------------------------------------------------

function _authChip(authConfig, edgeReady) {
    const fc = (authConfig && typeof authConfig.fail_closed === 'boolean')
        ? authConfig.fail_closed : null;
    const posture = fc === null ? 'unknown' : (fc ? 'fail-closed' : 'fail-open');
    const edge = (edgeReady === true) ? 'edge can verify'
        : (edgeReady === false) ? 'edge cannot verify' : 'unknown';
    const postureCls = fc === null ? 'bg-gray-100 text-gray-500'
        : fc ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700';
    const edgeCls = edgeReady === true ? 'bg-sky-100 text-sky-700'
        : edgeReady === false ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-medium ${postureCls}">${posture}</span>`
        + ` <span class="px-2 py-0.5 rounded-full text-xs font-medium ${edgeCls}">${edge}</span>`;
}

// Stack-identity banner for the App side. The server's STATUS WORD only
// (apps.py:303-307 returns exactly {status, app_id}; push_rotated_identity
// knows the reasons — no-worker / legacy-file-form — the browser cannot, so
// nothing here derives staleness). Values are an int app_id and a word; both
// go through escapeHtml. Missing/other status → 'unknown', never blank.
function _identityParts(identity) {
    const status = identity && typeof identity.status === 'string' ? identity.status : null;
    const word = status === 'redeploy_required' ? 'identity stale — redeploy the stack to apply it'
        : status === 'pushed' ? 'identity applied'
        : status === 'none' ? 'no stack identity' : 'unknown';
    const cls = status === 'redeploy_required' ? 'bg-amber-100 text-amber-700'
        : status === 'pushed' ? 'bg-green-100 text-green-700'
        : 'bg-gray-100 text-gray-500';
    const app = identity && Number.isInteger(identity.app_id) ? identity.app_id : null;
    return { word, cls, app };
}

function _identityBanner(identity) {
    const p = _identityParts(identity);
    const appPart = p.app === null ? '' : ` · App #${escapeHtml(String(p.app))}`;
    return `<span data-testid="app-identity-banner" class="px-2 py-0.5 rounded-full text-xs font-medium ${p.cls}">${escapeHtml(p.word)}${appPart}</span>`;
}

// Plain-text form for the submit box: the box is filled with textContent, so
// no server value ever reaches innerHTML (S5).
function _identityBannerText(identity) {
    const p = _identityParts(identity);
    return p.app === null ? p.word : `${p.word} · App #${p.app}`;
}

// Persistent host for the post-save banner. closeModal() clears #modal-
// container, so the box lives in #apps-section (renderApps only rewrites
// #apps-table tbody, so it survives) and falls back to <body>.
function _identityBannerBox() {
    const existing = document.getElementById('app-identity-banner-box');
    if (existing) return existing;
    const host = document.getElementById('apps-section') || document.body;
    const box = document.createElement('div');
    box.id = 'app-identity-banner-box';
    box.setAttribute('class', 'hidden mt-2 text-xs font-medium px-2 py-1 rounded bg-gray-100 text-gray-500');
    host.appendChild(box);
    return box;
}

// The posture word alone (no chip markup) for the modal's fail_closed row.
function _authPosture(fc) {
    if (fc === true) return 'fail-closed';
    if (fc === false) return 'fail-open';
    return 'unknown (follows require_login)';
}

// Comma-separated owner input -> list (trimmed, blanks dropped).
function _authList(value) {
    return String(value || '').split(',').map(s => s.trim()).filter(s => s !== '');
}

// Cheap client mirror of app_forward_auth.validate_config (:191-201): groups
// non-empty after trim, paths Caddy-`path`-matcher-safe (_PATH_TOKEN_RE :140).
// Returns the reason string, or null when the payload looks acceptable.
function _authValidate(groups, paths) {
    for (const g of groups) {
        if (!g) return 'allowed_groups entries must be non-empty strings';
    }
    for (const p of paths) {
        if (p.charAt(0) !== '/' || /[\s{}]/.test(p)) {
            return `public_paths entry ${JSON.stringify(p)} is not a path prefix (must start with '/' and contain no spaces/braces)`;
        }
    }
    return null;
}

function _targetLabel(a) {
    if (a.target_kind === 'external-url') {
        return escapeHtml(a.external_url || '—');
    }
    const stack = _stacks.find(s => s.id === a.stack_id);
    const stackName = stack ? (stack.name || `#${stack.id}`) : (a.stack_id != null ? `#${a.stack_id}` : '?');
    const svc = a.stack_service != null ? a.stack_service : '?';
    const port = a.stack_port != null ? `:${a.stack_port}` : '';
    return `${escapeHtml(stackName)} / ${escapeHtml(svc)}${escapeHtml(port)}`;
}

// ---------------------------------------------------------------------------
// public address (edge Caddy) — owner Q-ING=A (10-08)
//
// Public app hostnames are per-app names the owner adds BY HAND to his
// DATACENTER edge Caddy, which forwards to the manager over the tailnet.
// The API exposes no edge/manager tailnet name anywhere (AppOut in
// routers/apps.py carries hostname only), so the upstream renders as a
// placeholder until a platform setting carries it — see the follow-up note.
// The block is READ-ONLY helper text: the platform never writes the owner's
// Caddy, and caddy-front's own sites (services/app_routing.py) stay untouched.
// ---------------------------------------------------------------------------

const EDGE_UPSTREAM_PLACEHOLDER = "<this-manager's tailnet name>";

// PLAIN-HTTP-APP-SITE (owner Tim 11-25): the platform setting
// apps.edge_plain_http decides which edge block the box prints. Read once per
// page load from GET /platform/settings/apps.edge_plain_http; a non-admin
// tenant page gets 403 -> stays false -> the https form (the live-proven
// default). When true the owner's edge forwards PLAIN http to the manager's
// :80, so the block must target :80 (MOD also renders an explicit http://
// site for the hostname then, so no 308 loop).
let _edgePlainHttp = false;

function _edgeCaddyBlock(hostname, upstream) {
    if (!hostname) return '';
    const up = upstream || EDGE_UPSTREAM_PLACEHOLDER;
    if (_edgePlainHttp) {
        return `${hostname} {\n      reverse_proxy ${up}:80\n}`;
    }
    // The https form proven live 11-25: the edge reaches the manager's tailnet
    // :443 (tailscale-serve.json forwards TCP 443 -> caddy-front:443), so the
    // reverse_proxy targets the manager over TLS and re-presents the app
    // hostname ({host}) to it. Port 443 must be reachable on the tailnet.
    return `${hostname} {\n      reverse_proxy https://${up}:443 {\n        header_up Host {host}\n        transport http {\n          tls_server_name ${hostname}\n          tls_insecure_skip_verify\n        }\n      }\n}`;
}

function _edgeCaddyNote(hostname) {
    if (_edgePlainHttp) {
        return `Add this to your edge Caddy and point public DNS for ${hostname} at that box. Your edge forwards plain http to the manager's :80 (apps.edge_plain_http is on, so MOD serves this hostname on plain :80 too — no 308 loop).`;
    }
    return `Add this to your edge Caddy and point public DNS for ${hostname} at that box. The manager's tailnet port 443 must be reachable (TCP 443 -> caddy-front:443).`;
}

// Clipboard wiring for the box above. openSimpleModal rebuilds #modal-
// container innerHTML, so the listener is attached AFTER the modal is built
// (same order the one-time-key box uses).
function _wireEdgeCaddyCopy() {
    const pre = document.getElementById('app-edge-caddy-block');
    const copyBtn = document.getElementById('app-edge-caddy-copy');
    if (!pre || !copyBtn) return;
    copyBtn.addEventListener('click', async () => {
        // Shared copy helper (js/utils.js): works over plain http on the LAN
        // (execCommand fallback). The old navigator.clipboard.writeText(pre.textContent)
        // read the rendered text from the element itself, never rebuilt it -
        // the helper receives exactly that same text.
        const ok = await window.modCopyText(pre.textContent);
        copyBtn.textContent = ok ? 'Copied' : 'Select + copy manually';
    });
}

function _edgeCaddyBox(hostname, upstream) {
    const block = _edgeCaddyBlock(hostname, upstream);
    const note = _edgeCaddyNote(hostname);
    return `
        <div class="mt-4">
            <h4 class="text-xs font-semibold uppercase tracking-wide text-gray-500">Public address (edge Caddy)</h4>
            <pre id="app-edge-caddy-block" class="text-xs font-mono px-3 py-2 border border-gray-300 rounded-md bg-gray-50 text-gray-700 whitespace-pre-wrap select-all">${escapeHtml(block)}</pre>
            <button type="button" id="app-edge-caddy-copy"
                    class="bg-primary text-white px-3 py-2 rounded-md text-sm hover:bg-primary/90">Copy</button>
            <p class="text-xs text-gray-500 mt-2">${escapeHtml(note)}</p>
            ${upstream ? '' : `<p class="text-xs text-gray-400 mt-1">The upstream is your manager's tailnet name — set it in Settings → Platform (apps.edge_upstream).</p>`}
        </div>`;
}

// Save path for the auth modal. openSimpleModal rebuilds innerHTML, so the
// submit listener is wired AFTER the modal is built (same order as the
// edge-Caddy Copy button). fail_closed is TRI-STATE: "follow" OMITS the key so
// the server derives it (app_forward_auth.py:148-156). On any error nothing is
// written and the list is NOT reloaded.
function _wireAuthSave(appId) {
    const form = document.getElementById('app-auth-form');
    const errBox = document.getElementById('app-auth-error');
    if (!form || !errBox) return;
    const $ = (id) => document.getElementById(id);
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        errBox.classList.add('hidden');
        const requireLogin = $('app-auth-require-login').checked;
        const groups = _authList($('app-auth-groups').value);
        const paths = _authList($('app-auth-paths').value);
        const bad = _authValidate(groups, paths);
        if (bad) {
            errBox.textContent = bad;
            errBox.classList.remove('hidden');
            return;
        }
        const fcSel = $('app-auth-fail-closed').value;
        const body = {
            require_login: requireLogin,
            allowed_groups: groups,
            public_paths: paths,
            ...(fcSel === 'on' ? { fail_closed: true }
                : fcSel === 'off' ? { fail_closed: false } : {}),
        };
        try {
            await apiCall(`/apps/${appId}/auth-config`, {
                method: 'PUT', body: JSON.stringify(body),
            });
            closeModal('app-auth-modal');
            await loadApps();
        } catch (err) {
            const status = (err && typeof err === 'object' && err.status) || 0;
            const reason = (err && (err.detail || err.message)) || String(err);
            errBox.textContent = status === 409
                ? `${reason} Provision the ingress ask token (vault-init.sh:1297-1305) or leave fail_closed off.`
                : reason;
            errBox.classList.remove('hidden');
        }
    });
}

function _fmtCreated(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d.getTime()) ? '—' : d.toLocaleDateString();
}

async function loadApps() {
    const loading = document.getElementById('apps-loading');
    const errBox = document.getElementById('apps-error');
    const empty = document.getElementById('apps-empty');
    const section = document.getElementById('apps-section');
    try {
        _apps = (await apiCall('/apps')) || [];
        // Stacks feed the target column + the register/edit modals. A failure
        // here only degrades the dropdowns, not the list.
        try {
            _stacks = (await apiCall('/stacks')) || [];
        } catch {
            _stacks = [];
        }
        // PLAIN-HTTP-APP-SITE: the edge-block variant switch. Platform admins
        // read it; anyone else (or any failure) keeps the https default.
        try {
            const r = await apiCall('/platform/settings/apps.edge_plain_http');
            _edgePlainHttp = !!r && r.value === true;
        } catch {
            _edgePlainHttp = false;
        }
        renderApps();
    } catch (err) {
        console.error('Error loading apps:', err);
        if (errBox) {
            errBox.textContent = `Could not load apps: ${(err && err.message) || err}`;
            errBox.classList.remove('hidden');
        }
    } finally {
        if (loading) loading.classList.add('hidden');
    }
}

function renderApps() {
    const tbody = document.querySelector('#apps-table tbody');
    const empty = document.getElementById('apps-empty');
    const section = document.getElementById('apps-section');
    if (!tbody) return;
    if (!_apps.length) {
        if (empty) empty.classList.remove('hidden');
        if (section) section.classList.add('hidden');
        return;
    }
    if (empty) empty.classList.add('hidden');
    if (section) section.classList.remove('hidden');

    tbody.innerHTML = _apps.map(a => {
        const hostCell = a.hostname
            ? `<a href="https://${escapeHtml(a.hostname)}" target="_blank" rel="noopener" class="text-primary hover:underline">${escapeHtml(a.hostname)}</a>`
            : '<span class="text-gray-400">—</span>';
        const isFailed = a.status === 'provisioning_failed';
        const isRevoked = a.status === 'revoked';
        const retryBtn = isFailed
            ? `<button data-act="retry" data-id="${a.id}" class="text-blue-600 hover:underline mr-3">Retry</button>`
            : '';
        const revokeBtn = isRevoked
            ? ''
            : `<button data-act="revoke" data-id="${a.id}" class="text-red-600 hover:underline">Revoke</button>`;
        return `
        <tr class="border-b">
            <td class="py-2 px-4">${escapeHtml(a.name || a.slug || '')}</td>
            <td class="py-2 px-4 text-sm text-gray-600">${hostCell}</td>
            <td class="py-2 px-4 text-sm text-gray-600">${_targetLabel(a)}</td>
            <td class="py-2 px-4 text-center">${_statusBadge(a.status, a.failure_detail)}</td>
            <td class="py-2 px-4 text-center whitespace-nowrap" data-testid="app-auth-chip-${a.id}">${_authChip(a.auth_config, a.edge_ready)}</td>
            <td class="py-2 px-4 text-sm text-gray-500 whitespace-nowrap">${_fmtCreated(a.created_at)}</td>
            <td class="py-2 px-4 text-right whitespace-nowrap">
                <button data-act="view" data-id="${a.id}" class="text-primary hover:underline mr-3">View</button>
                ${isRevoked ? '' : `<button data-act="rotate" data-id="${a.id}" class="text-gray-700 hover:underline mr-3">Rotate key</button>`}
                ${isRevoked ? '' : `<button data-act="edit" data-id="${a.id}" class="text-gray-600 hover:underline mr-3">Edit</button>`}
                ${isRevoked ? '' : `<button data-act="auth" data-id="${a.id}" class="text-gray-600 hover:underline mr-3">Auth config</button>`}
                ${retryBtn}${revokeBtn}
            </td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('button[data-act]').forEach(btn => {
        btn.addEventListener('click', () => handleRowAction(btn.dataset.act, parseInt(btn.dataset.id, 10)));
    });
}

// ---------------------------------------------------------------------------
// row actions
// ---------------------------------------------------------------------------

function _appById(id) {
    return _apps.find(a => a.id === id) || null;
}

async function handleRowAction(act, id) {
    const app = _appById(id);
    if (!app) return;
    try {
        if (act === 'view') {
            const detail = await apiCall(`/apps/${id}`);
            const keyLine = detail.key_prefix
                ? `Key: ${escapeHtml(detail.key_prefix)}…`
                : 'Key: (none minted)';
            openSimpleModal('app-view-modal', 'App details', `
                ${detail.hostname ? _edgeCaddyBox(detail.hostname, detail.edge_upstream) : ''}
                <dl class="text-sm space-y-2">
                    <div><dt class="font-semibold text-gray-700">Client (slug)</dt><dd class="text-gray-600">${escapeHtml(detail.slug || '—')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">Hostname</dt><dd class="text-gray-600">${escapeHtml(detail.hostname || '—')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">${keyLine}</dt></div>
                    <div><dt class="font-semibold text-gray-700">Status</dt><dd class="text-gray-600">${escapeHtml(detail.status || '—')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">Scope</dt><dd class="text-gray-600">workflows: ${escapeHtml((detail.allowed_workflow_ids || []).join(', ') || 'all')} · folders: ${escapeHtml((detail.allowed_folder_ids || []).join(', ') || 'all')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">Identity</dt><dd class="text-gray-600" data-testid="app-identity-view">${escapeHtml(_identityBannerText(detail.identity))}</dd></div>
                </dl>`, () => closeModal('app-view-modal'));
            _wireEdgeCaddyCopy();
        } else if (act === 'rotate') {
            if (!confirm('Rotate the API key? The current key stops working immediately. The new key is shown once.')) return;
            const res = await apiCall(`/apps/${id}/rotate-key`, { method: 'POST' });
            await loadApps();
            openOneTimeKeyModal('Rotate key — ' + (app.name || app.slug), res.access_key, res.key_prefix);
        } else if (act === 'edit') {
            openRegisterAppModal(app);
        } else if (act === 'auth') {
            const detail = await apiCall(`/apps/${id}`);
            const cfg = detail.auth_config || {};
            const groups = (cfg.allowed_groups || []).join(', ');
            const paths = (cfg.public_paths || []).join(', ');
            openSimpleModal('app-auth-modal', 'Auth config', `
                <dl class="text-sm space-y-2">
                    <div><dt class="font-semibold text-gray-700">App</dt><dd class="text-gray-600">${escapeHtml(detail.name || detail.slug || `#${id}`)}</dd></div>
                    <div><dt class="font-semibold text-gray-700">require_login</dt><dd class="text-gray-600">${cfg.require_login === true ? 'true' : cfg.require_login === false ? 'false' : 'unknown'}</dd></div>
                    <div><dt class="font-semibold text-gray-700">fail_closed</dt><dd class="text-gray-600">${_authPosture(cfg.fail_closed)}</dd></div>
                    <div><dt class="font-semibold text-gray-700">allowed_groups</dt><dd class="text-gray-600 font-mono">${escapeHtml(groups || '(none)')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">public_paths</dt><dd class="text-gray-600 font-mono">${escapeHtml(paths || '(none)')}</dd></div>
                    <div><dt class="font-semibold text-gray-700">edge_ready</dt><dd class="text-gray-600">${_authChip({}, detail.edge_ready)}</dd></div>
                </dl>
                <p class="text-xs text-gray-500 mt-2">fail_closed omitted ⇒ follows require_login (app_forward_auth.py:148-156).</p>
                <form id="app-auth-form" class="mt-4" novalidate>
                    <div class="mt-3">
                        <label class="inline-flex items-center gap-2 text-sm text-gray-700">
                            <input id="app-auth-require-login" type="checkbox" ${cfg.require_login === true ? 'checked' : ''} />
                            <span>require_login</span>
                        </label>
                    </div>
                    <div class="mt-3">
                        <label class="block text-sm font-medium text-gray-700" for="app-auth-fail-closed">fail_closed</label>
                        <select id="app-auth-fail-closed" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm">
                            <option value="follow" ${cfg.fail_closed === true || cfg.fail_closed === false ? '' : 'selected'}>follow require_login (omit the key)</option>
                            <option value="on" ${cfg.fail_closed === true ? 'selected' : ''}>on — fail-closed</option>
                            <option value="off" ${cfg.fail_closed === false ? 'selected' : ''}>off — fail-open</option>
                        </select>
                    </div>
                    <div class="mt-3">
                        <label class="block text-sm font-medium text-gray-700" for="app-auth-groups">allowed_groups (comma-separated)</label>
                        <input type="text" id="app-auth-groups" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                               placeholder="blank = none" value="${escapeHtml(groups)}" />
                    </div>
                    <div class="mt-3">
                        <label class="block text-sm font-medium text-gray-700" for="app-auth-paths">public_paths (comma-separated)</label>
                        <input type="text" id="app-auth-paths" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                               placeholder="blank = none" value="${escapeHtml(paths)}" />
                    </div>
                    <div id="app-auth-error" class="mt-3 hidden text-sm text-red-600"></div>
                    <div class="mt-6 flex justify-between">
                        <button type="button" id="app-auth-cancel" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-md">Cancel</button>
                        <button type="submit" data-testid="app-auth-save" class="bg-primary text-white px-4 py-2 rounded-md">Save</button>
                    </div>
                </form>`, () => closeModal('app-auth-modal'));
            _wireAuthSave(id);
        } else if (act === 'retry') {
            await apiCall(`/apps/${id}/retry-provisioning`, { method: 'POST' });
            await loadApps();
        } else if (act === 'revoke') {
            openRevokeModal(app);
        }
    } catch (err) {
        console.error(`apps: ${act} failed:`, err);
        window.alert(`Action failed: ${(err && err.message) || err}`);
    }
}

// ---------------------------------------------------------------------------
// shared modal plumbing (inline markup -> #modal-container)
// ---------------------------------------------------------------------------

function openSimpleModal(modalId, title, bodyHtml, onClose) {
    const container = document.getElementById('modal-container');
    if (!container) return null;
    container.innerHTML = `
        <div id="${modalId}" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50">
            <div class="relative top-10 mx-auto p-5 border w-[32rem] max-w-[90vw] shadow-lg rounded-md bg-white">
                <h3 class="text-lg font-medium text-gray-900">${escapeHtml(title)}</h3>
                <div class="mt-4">${bodyHtml}</div>
                <div class="mt-6 flex justify-end">
                    <button type="button" id="${modalId}-close" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-md">Close</button>
                </div>
            </div>
        </div>`;
    document.getElementById(`${modalId}-close`).addEventListener('click', () => {
        closeModal(modalId);
        if (typeof onClose === 'function') onClose();
    });
    return document.getElementById(modalId);
}

// One-time secret box (used by app keys AND run-webhook signing secrets).
// The secret is rendered into the DOM but NEVER console.logged.
function openOneTimeKeyModal(title, secret, prefix) {
    if (!secret) {
        openSimpleModal(`${title.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-empty`, title,
            '<p class="text-sm text-red-600">The API did not return a key. Check the app status and retry.</p>',
            null);
        return;
    }
    const prefixLine = prefix
        ? `<p class="text-xs text-gray-400 mb-1">Key prefix: ${escapeHtml(prefix)}…</p>` : '';
    openSimpleModal('app-one-time-key-modal', title, `
        ${prefixLine}
        <p class="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-3 py-2 mb-3">
            Copy this key now — <strong>you won't see it again</strong>.
        </p>
        <div class="flex items-center gap-2">
            <input id="app-one-time-key-value" type="text" readonly aria-label="One-time app API key" value="${escapeHtml(secret)}"
                   class="flex-1 px-3 py-2 text-sm font-mono border border-gray-300 rounded-md bg-gray-50 select-all" />
            <button type="button" id="app-one-time-key-copy"
                    class="bg-primary text-white px-3 py-2 rounded-md text-sm hover:bg-primary/90">Copy</button>
        </div>`, null);
    const copyBtn = document.getElementById('app-one-time-key-copy');
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            const input = document.getElementById('app-one-time-key-value');
            // Shared copy helper (js/utils.js): works over plain http on the LAN
            // (execCommand fallback); the input stays select-all for a manual copy.
            const ok = await window.modCopyText(input ? input.value : secret);
            copyBtn.textContent = ok ? 'Copied' : 'Select + copy manually';
        });
    }
}

// ---------------------------------------------------------------------------
// revoke (typed-name confirm — the wording mirrors the API's soft-revoke:
// history is kept, the app just can't be used)
// ---------------------------------------------------------------------------

function openRevokeModal(app) {
    const displayName = app.name || app.slug || String(app.id);
    openSimpleModal('app-revoke-modal', `Revoke ${displayName}`, `
        <p class="text-sm text-gray-600 mb-3">
            Revoked apps can't be used; history is kept. Its hostname stops
            resolving and its API key is disabled immediately.
        </p>
        <label class="block text-sm font-medium text-gray-700 mb-1" for="app-revoke-confirm-input">
            Type <strong>${escapeHtml(displayName)}</strong> to confirm
        </label>
        <input type="text" id="app-revoke-confirm-input" autocomplete="off"
               class="block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm text-sm" />
        <div class="mt-6 flex justify-between">
            <button type="button" id="app-revoke-cancel" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-md">Cancel</button>
            <button type="button" id="app-revoke-confirm" disabled
                    class="bg-red-600 text-white px-4 py-2 rounded-md disabled:opacity-40 disabled:cursor-not-allowed">Revoke app</button>
        </div>`, null);
    const input = document.getElementById('app-revoke-confirm-input');
    const confirmBtn = document.getElementById('app-revoke-confirm');
    input.addEventListener('input', () => {
        confirmBtn.disabled = input.value.trim() !== displayName;
    });
    document.getElementById('app-revoke-cancel').addEventListener('click', () => closeModal('app-revoke-modal'));
    confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true;
        try {
            await apiCall(`/apps/${app.id}`, { method: 'DELETE' });
            closeModal('app-revoke-modal');
            await loadApps();
        } catch (err) {
            console.error('apps: revoke failed:', err);
            window.alert(`Revoke failed: ${(err && err.message) || err}`);
            confirmBtn.disabled = false;
        }
    });
    input.focus();
}

// ---------------------------------------------------------------------------
// register / edit target modal
// ---------------------------------------------------------------------------

function _stackOptions(selectedStackId) {
    if (!_stacks.length) {
        return '<option value="">(no stacks)</option>';
    }
    return _stacks.map(s =>
        `<option value="${s.id}" ${s.id === selectedStackId ? 'selected' : ''}>${escapeHtml(s.name || `Stack #${s.id}`)}</option>`
    ).join('');
}

function _servicesForStack(stack) {
    const services = (stack && stack.compose_spec && typeof stack.compose_spec === 'object')
        ? stack.compose_spec.services : null;
    return services && typeof services === 'object' ? Object.keys(services) : [];
}

// First published port of a service in a compose spec, as a number (or null).
function _firstPortForService(stack, service) {
    const svc = (stack && stack.compose_spec && stack.compose_spec.services) || {};
    const ports = svc[service] && svc[service].ports;
    if (!Array.isArray(ports)) return null;
    for (const p of ports) {
        if (typeof p === 'string') {
            const parts = p.split(':');
            const last = parseInt(parts[parts.length - 1], 10);
            if (!isNaN(last)) return last;
        } else if (p && typeof p === 'object' && p.target != null) {
            return parseInt(p.target, 10);
        } else if (typeof p === 'number') {
            return p;
        }
    }
    return null;
}

function _scopeIds(value) {
    return (value || '').split(',').map(v => parseInt(v.trim(), 10)).filter(n => !isNaN(n) && n > 0);
}

function _targetPayload(kind) {
    const $ = (id) => document.getElementById(id);
    const payload = { target_kind: kind };
    if (kind === 'stack-service') {
        payload.stack_id = parseInt($('app-target-stack').value, 10) || null;
        payload.stack_service = $('app-target-service').value || null;
        const port = parseInt($('app-target-port').value, 10);
        payload.stack_port = (!isNaN(port) && port > 0) ? port : null;
    } else {
        payload.external_url = $('app-target-url').value.trim() || null;
    }
    return payload;
}

async function openRegisterAppModal(app) {
    // Refresh stacks at modal-open time so stacks created after page load
    // appear in the target dropdown (loadApps() only ran once at init).
    try {
        _stacks = await apiCall('/stacks') || [];
    } catch {
        _stacks = [];
    }
    const isEdit = !!app;
    const suffix = _hostnameSuffix();
    openSimpleModal('app-register-modal', isEdit ? `Edit ${app.name || app.slug}` : 'Register app', `
        <form id="app-register-form" class="mt-4" novalidate>
            <div>
                <label class="block text-sm font-medium text-gray-700" for="app-register-name">Name</label>
                <input type="text" id="app-register-name" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="My external tool" value="${isEdit ? escapeHtml(app.name || '') : ''}" required />
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="app-register-slug">Slug (hostname label)</label>
                <input type="text" id="app-register-slug" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       pattern="^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$" placeholder="myapp" value="${isEdit ? escapeHtml(app.slug || '') : ''}" ${isEdit ? 'disabled' : 'required'} />
                <p class="text-xs text-gray-400 mt-1">Hostname preview: <span id="app-register-hostname-preview" class="font-mono text-gray-600">—</span>${isEdit ? ' (fixed at registration)' : ''}</p>
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="app-target-kind">Target</label>
                <select id="app-target-kind" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm">
                    <option value="stack-service" ${(!app || app.target_kind === 'stack-service') ? 'selected' : ''}>Persistent Stack service</option>
                    <option value="external-url" ${app && app.target_kind === 'external-url' ? 'selected' : ''}>External URL</option>
                </select>
            </div>
            <div id="app-target-stack-fields" class="mt-3 ${app && app.target_kind === 'external-url' ? 'hidden' : ''}">
                <label class="block text-sm font-medium text-gray-700" for="app-target-stack">Stack</label>
                <select id="app-target-stack" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm">${_stackOptions(app ? app.stack_id : null)}</select>
                <div class="grid grid-cols-2 gap-3 mt-2">
                    <div>
                        <label class="block text-sm font-medium text-gray-700" for="app-target-service">Service</label>
                        <input type="text" id="app-target-service" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                               placeholder="web" value="${app ? escapeHtml(app.stack_service || '') : ''}" required />
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700" for="app-target-port">Port</label>
                        <input type="number" id="app-target-port" min="1" max="65535" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                               placeholder="8000" value="${app && app.stack_port ? app.stack_port : ''}" required />
                    </div>
                </div>
                <p class="text-xs text-gray-400 mt-1">Service names: <span id="app-target-service-hints" class="font-mono"></span></p>
            </div>
            <div id="app-target-url-fields" class="mt-3 ${app && app.target_kind !== 'external-url' ? 'hidden' : ''}">
                <label class="block text-sm font-medium text-gray-700" for="app-target-url">External URL</label>
                <input type="url" id="app-target-url" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="https://api.example.com" value="${app ? escapeHtml(app.external_url || '') : ''}" required />
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="app-register-workflows">Scope — allowed workflow IDs</label>
                <input type="text" id="app-register-workflows" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="blank = all" value="${isEdit ? escapeHtml((app.allowed_workflow_ids || []).join(', ')) : ''}" />
                <p class="text-xs text-gray-400 mt-1">Comma-separated workflow IDs; blank means no workflow restriction.</p>
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="app-register-folders">Scope — allowed folder IDs</label>
                <input type="text" id="app-register-folders" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="blank = all" value="${isEdit ? escapeHtml((app.allowed_folder_ids || []).join(', ')) : ''}" />
            </div>
            <div id="app-register-error" class="mt-3 hidden text-sm text-red-600"></div>
            <div class="mt-6 flex justify-between">
                <button type="button" id="app-register-cancel" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-md">Cancel</button>
                <button type="submit" class="bg-primary text-white px-4 py-2 rounded-md">${isEdit ? 'Save target' : 'Register'}</button>
            </div>
        </form>`, null);

    const $ = (id) => document.getElementById(id);
    const kindSel = $('app-target-kind');
    const slugInput = $('app-register-slug');

    const updatePreview = () => {
        const slug = slugInput.value.trim().toLowerCase();
        $('app-register-hostname-preview').textContent = isEdit
            ? (app.hostname || '—')
            : (slug ? slug + suffix : '—' + (suffix || ' (tenant domain)'));
        updateServiceHints();
    };
    const updateServiceHints = () => {
        const stackId = parseInt(kindSel.value === 'stack-service' ? $('app-target-stack').value : '', 10);
        const stack = _stacks.find(s => s.id === stackId);
        const names = _servicesForStack(stack);
        const hints = $('app-target-service-hints');
        hints.textContent = names.length ? names.join(', ') : '';
        if (names.length && !$('app-target-service').value) {
            $('app-target-service').value = names[0];
            const port = _firstPortForService(stack, names[0]);
            if (port && !isEdit) $('app-target-port').value = port;
        }
    };

    if (!isEdit) slugInput.addEventListener('input', updatePreview);
    kindSel.addEventListener('change', () => {
        const isStack = kindSel.value === 'stack-service';
        $('app-target-stack-fields').classList.toggle('hidden', !isStack);
        $('app-target-url-fields').classList.toggle('hidden', isStack);
        updateServiceHints();
    });
    $('app-target-stack').addEventListener('change', updateServiceHints);

    updatePreview();

    $('app-register-cancel').addEventListener('click', () => closeModal('app-register-modal'));
    $('app-register-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errBox = $('app-register-error');
        errBox.classList.add('hidden');
        const kind = kindSel.value;
        const body = {
            target_kind: kind,
            allowed_workflow_ids: _scopeIds($('app-register-workflows').value),
            allowed_folder_ids: _scopeIds($('app-register-folders').value),
            ..._targetPayload(kind),
        };
        try {
            if (isEdit) {
                const updated = await apiCall(`/apps/${app.id}/update-target`, { method: 'POST', body: JSON.stringify(body) });
                closeModal('app-register-modal');
                // Render-only banner for the server's verdict; a non-2xx threw
                // above, so nothing here runs on a failed save. Silent on 'pushed'.
                const box = _identityBannerBox();
                box.classList.add('hidden');
                if (updated && updated.identity && updated.identity.status !== 'pushed') {
                    const p = _identityParts(updated.identity);
                    box.textContent = _identityBannerText(updated.identity);
                    box.setAttribute('class',
                        `mt-2 text-xs font-medium px-2 py-1 rounded ${p.cls}`);
                    box.classList.remove('hidden');
                }
                await loadApps();
            } else {
                body.name = $('app-register-name').value.trim() || null;
                body.slug = slugInput.value.trim();
                const created = await apiCall('/apps', { method: 'POST', body: JSON.stringify(body) });
                closeModal('app-register-modal');
                await loadApps();
                openOneTimeKeyModal('App registered — ' + (created.name || created.slug), created.access_key, created.key_prefix);
            }
        } catch (err) {
            console.error('apps: save failed:', err);
            errBox.textContent = (err && err.message) || String(err);
            errBox.classList.remove('hidden');
        }
    });
}

// ---------------------------------------------------------------------------
// BUG.B36b (owner Q-B36=A) — merged "Webhooks" section: ONE list, ONE create
// form, two API backends.
//   Scope "All workflows" → WebhookEndpoint rows (GET /webhooks/, helpers
//     exported by js/webhooks.js; events = subscribed_events).
//   Scope "One workflow"  → run_webhook_subscriptions rows via the B36a
//     tenant-wide GET /road/webhook-subscriptions (never iterate
//     workflows). Signing secrets come back ONCE (create / rotate-secret) and
//     go through openOneTimeKeyModal; they are never re-read or logged.
//   Deletes are the existing soft-delete endpoints only.
// If the B36a list 404s/422s (the API has not been restarted yet), the
// "One workflow" rows render as an "unavailable — reload later" notice and the
// "All workflows" list still works.
// ---------------------------------------------------------------------------

const _WH_EVENTS = ['workflow.completed', 'workflow.succeeded',
                    'workflow.failed', 'workflow.canceled', '*'];

async function loadWebhooks() {
    const box = document.getElementById('webhook-rows');
    if (!box) return;
    box.innerHTML = '<span class="text-sm text-gray-400">Loading webhooks…</span>';

    let endpoints = null;
    let endpointErr = null;
    try {
        endpoints = await listEndpoints();
    } catch (err) {
        endpointErr = (err && err.message) || String(err);
    }

    let subs = null;
    let subsErr = null;
    try {
        const res = await apiCall('/road/webhook-subscriptions?limit=200&skip=0');
        subs = Array.isArray(res) ? res : (res && res.items) || [];
    } catch (err) {
        const status = (err && typeof err === 'object' && err.status) || 0;
        if (status === 404 || status === 422) {
            subsErr = 'unavailable';   // B36a not live yet (pre-restart)
        } else {
            subsErr = (err && err.message) || String(err);
        }
    }

    if (endpoints === null && subs === null) {
        box.innerHTML = `<span class="text-sm text-red-600">Could not load webhooks: ${escapeHtml(endpointErr || '')}${endpointErr && subsErr ? ' / ' : ''}${escapeHtml(subsErr === 'unavailable' ? 'one-workflow list not live yet' : (subsErr || ''))}</span>`;
        return;
    }

    const rows = [];
    (endpoints || []).forEach(w => rows.push({
        scope: 'all', id: w.id, url: w.url, description: w.description,
        is_active: w.is_active, events: w.subscribed_events || [],
        workflow_name: null,
    }));
    (subs || []).forEach(s => rows.push({
        scope: 'one', id: s.id, url: s.url, description: s.description,
        is_active: s.is_active, events: null, workflow_name: s.workflow_name,
        workflow_id: s.workflow_id,
    }));

    if (!rows.length && subsErr === 'unavailable') {
        box.innerHTML = '<span class="text-sm text-gray-400">No webhooks yet.</span>';
    } else if (!rows.length) {
        box.innerHTML = '<span class="text-sm text-gray-400">No webhooks yet.</span>';
    }

    const scopeBadge = (s) => s === 'all'
        ? '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-sky-100 text-sky-700">All workflows</span>'
        : '<span class="px-2 py-0.5 rounded-full text-xs font-medium bg-amber-100 text-amber-700">One workflow</span>';

    const unavailableNote = subsErr === 'unavailable'
        ? `<div class="text-xs text-gray-400 mt-2">"One workflow" subscriptions are unavailable — the new API list is not live yet. Reload this page later.</div>`
        : (subsErr ? `<div class="text-xs text-red-600 mt-2">Could not load "One workflow" subscriptions: ${escapeHtml(subsErr)}</div>` : '');

    box.innerHTML = `
        <table class="w-full text-sm">
            <thead><tr>
                <th class="text-left py-1 px-2 font-semibold text-gray-600">Scope</th>
                <th class="text-left py-1 px-2 font-semibold text-gray-600">URL</th>
                <th class="text-left py-1 px-2 font-semibold text-gray-600">Events / Workflow</th>
                <th class="text-left py-1 px-2 font-semibold text-gray-600">Description</th>
                <th class="text-center py-1 px-2 font-semibold text-gray-600">Status</th>
                <th class="text-right py-1 px-2 font-semibold text-gray-600">Actions</th>
            </tr></thead>
            <tbody>
                ${rows.map(r => {
                    const detail = r.scope === 'all'
                        ? (r.events.length
                            ? r.events.map(e => `<code class="bg-gray-100 px-1 rounded text-xs">${escapeHtml(e)}</code>`).join(' ')
                            : '<span class="text-gray-400">no events</span>')
                        : `<span class="text-gray-500">${escapeHtml(r.workflow_name || `Workflow #?`)}</span>`;
                    const acts = r.scope === 'all'
                        ? `<button data-wh-act="delete" data-id="${r.id}" data-url="${escapeHtml(r.url)}" class="text-red-600 hover:underline">Delete</button>`
                        : `<button data-wh-act="rotate" data-wf="${r.workflow_id}" data-id="${r.id}" data-url="${escapeHtml(r.url)}" class="text-gray-700 hover:underline mr-3">Rotate secret</button><button data-wh-act="delete" data-wf="${r.workflow_id}" data-id="${r.id}" data-url="${escapeHtml(r.url)}" class="text-red-600 hover:underline">Delete</button>`;
                    return `
                <tr class="border-b">
                    <td class="py-1 px-2">${scopeBadge(r.scope)}</td>
                    <td class="py-1 px-2 text-gray-600 break-all">${escapeHtml(r.url)}</td>
                    <td class="py-1 px-2">${detail}</td>
                    <td class="py-1 px-2 text-gray-500">${escapeHtml(r.description || '—')}</td>
                    <td class="py-1 px-2 text-center">${r.is_active === false
                        ? '<span class="text-gray-400">inactive</span>'
                        : '<span class="text-green-600">active</span>'}</td>
                    <td class="py-1 px-2 text-right whitespace-nowrap">${acts}</td>
                </tr>`;
                }).join('')}
            </tbody>
        </table>${unavailableNote}`;

    box.querySelectorAll('button[data-wh-act]').forEach(btn => {
        btn.addEventListener('click', () => handleWebhookAction(btn));
    });
}

// Row actions. A row carrying data-wf is a "One workflow" subscription (its
// rotate + delete endpoints are workflow-scoped); a row without it is an
// "All workflows" WebhookEndpoint (js/webhooks.js helpers, soft delete only).
async function handleWebhookAction(btn) {
    const act = btn.dataset.whAct;
    const subId = parseInt(btn.dataset.id, 10);
    const url = btn.dataset.url || '';
    const wfId = parseInt(btn.dataset.wf, 10);
    try {
        if (act === 'rotate') {
            if (!wfId) return;
            if (!confirm(`Rotate the signing secret for ${url}? The old secret stops verifying immediately. The new secret is shown once.`)) return;
            const res = await apiCall(`/road/${wfId}/webhook-subscriptions/${subId}/rotate-secret`, { method: 'POST' });
            openOneTimeKeyModal('Signing secret rotated — ' + url, res.secret);
        } else if (act === 'delete') {
            if (!confirm(`Delete the webhook for ${url}?`)) return;
            if (wfId) await apiCall(`/road/${wfId}/webhook-subscriptions/${subId}`, { method: 'DELETE' });
            else await removeEndpoint(subId);
            await loadWebhooks();
        }
    } catch (err) {
        console.error('apps: webhook action failed:', err);
        window.alert(`Action failed: ${(err && err.message) || err}`);
    }
}

// ONE create form, Scope selector ("All workflows" | "One workflow").
// all  → POST /webhooks/ (js/webhooks.js createEndpoint) + event checkboxes.
// one  → workflow select + POST /road/{id}/webhook-subscriptions; the
//        signing secret is returned once and shown in the one-time key modal.
async function openAddWebhookModal() {
    let workflows = [];
    try {
        const data = await apiCall('/road');
        workflows = Array.isArray(data) ? data : (data && data.items) || [];
    } catch (err) {
        console.error('apps: workflow list failed:', err);
    }
    const wfOptions = workflows.length
        ? workflows.map(w => `<option value="${w.id}">${escapeHtml(w.name || w.title || `Workflow #${w.id}`)}</option>`).join('')
        : '<option value="">(no workflows)</option>';

    openSimpleModal('webhook-add-modal', 'Add webhook', `
        <form id="webhook-add-form" class="mt-4">
            <div class="mt-2">
                <span class="block text-sm font-medium text-gray-700 mb-1">Scope</span>
                <select id="wh-scope" aria-label="Scope" class="block w-full px-3 py-2 border border-gray-300 rounded-md text-sm">
                    <option value="all">All workflows (tenant-wide workflow.* events)</option>
                    <option value="one">One workflow (run-completion for a single workflow)</option>
                </select>
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="wh-add-url">URL</label>
                <input type="url" id="wh-add-url" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="https://hooks.example.com/mod" required />
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="wh-add-desc">Description (optional)</label>
                <input type="text" id="wh-add-desc" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm"
                       placeholder="ops notify" />
            </div>
            <div class="mt-3">
                <label class="block text-sm font-medium text-gray-700" for="wh-add-workflow">Workflow (only for "One workflow")</label>
                <select id="wh-add-workflow" class="block w-full mt-1 px-3 py-2 border border-gray-300 rounded-md text-sm">${wfOptions}</select>
            </div>
            <div class="mt-3">
                <span class="block text-sm font-medium text-gray-700 mb-1">Events (only for "All workflows")</span>
                <div class="flex flex-wrap gap-x-4 gap-y-2 text-sm text-gray-700">
                    ${_WH_EVENTS.map(ev => `<label class="inline-flex items-center gap-2"><input type="checkbox" class="wh-new-event" value="${ev}"><span><code class="bg-gray-100 px-1 rounded text-xs">${escapeHtml(ev)}</code></span></label>`).join('')}
                </div>
            </div>
            <div class="mt-3">
                <label class="inline-flex items-center gap-2 text-sm text-gray-700">
                    <input id="wh-add-active" type="checkbox" checked />
                    <span>Active</span>
                </label>
            </div>
            <div id="wh-add-error" class="mt-3 hidden text-sm text-red-600"></div>
            <div class="mt-6 flex justify-end">
                <button type="button" id="wh-add-cancel" class="bg-gray-300 text-gray-700 px-4 py-2 rounded-md mr-2">Cancel</button>
                <button type="submit" class="bg-primary text-white px-4 py-2 rounded-md">Add</button>
            </div>
        </form>`, null);
    document.getElementById('wh-add-cancel').addEventListener('click', () => closeModal('webhook-add-modal'));
    document.getElementById('webhook-add-form').addEventListener('submit', async (e) => {
        e.preventDefault();
        const errBox = document.getElementById('wh-add-error');
        errBox.classList.add('hidden');
        const scope = (document.getElementById('wh-scope')?.value || 'all');
        const url = (document.getElementById('wh-add-url')?.value || '').trim();
        const description = (document.getElementById('wh-add-desc')?.value || '').trim();
        const is_active = !!document.getElementById('wh-add-active')?.checked;
        if (!url) {
            errBox.textContent = 'A destination URL is required.';
            errBox.classList.remove('hidden');
            return;
        }
        try {
            let created = null;
            if (scope === 'one') {
                const wfId = parseInt(document.getElementById('wh-add-workflow')?.value || '', 10);
                if (!wfId) {
                    errBox.textContent = 'Pick a workflow for the "One workflow" scope.';
                    errBox.classList.remove('hidden');
                    return;
                }
                created = await apiCall(`/road/${wfId}/webhook-subscriptions`, {
                    method: 'POST',
                    body: JSON.stringify({ url, description: description || null }),
                });
            } else {
                const events = Array.from(document.querySelectorAll('.wh-new-event'))
                    .filter(cb => cb.checked).map(cb => cb.value);
                if (!events.length) {
                    errBox.textContent = 'Select at least one event for the "All workflows" scope.';
                    errBox.classList.remove('hidden');
                    return;
                }
                created = await createEndpoint({
                    url, description: description || null,
                    subscribed_events: events, is_active,
                });
            }
            closeModal('webhook-add-modal');
            await loadWebhooks();
            if (created && created.secret) {
                openOneTimeKeyModal('Webhook signing secret — shown once', created.secret);
            }
        } catch (err) {
            console.error('apps: add webhook failed:', err);
            errBox.textContent = (err && err.message) || String(err);
            errBox.classList.remove('hidden');
        }
    });
}
