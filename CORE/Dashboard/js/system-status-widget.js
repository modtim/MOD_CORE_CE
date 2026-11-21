// Dashboard/js/system-status-widget.js
//
// home-page-finalize §3 — replaces the placeholder "System Status" card on
// the home page with real component probes. STRICT aggregation: any one
// 'degraded' → yellow; any one 'down' → red. See design.md D2.

import { isPaidEdition } from './api.js';

const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

// Tenant-scoped queue-depth thresholds. Tunable here so we can adjust
// post-launch without code review on the widget body itself.
const QUEUE_DEPTH_DEGRADED_AT = 10;
const QUEUE_DEPTH_DOWN_AT = 50;

let _refreshInFlight = false;
let _autoRefreshTimer = null;

export async function renderSystemStatusWidget(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const token = sessionStorage.getItem('accessToken');
    if (!token) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = renderSkeleton();

    let me = null;
    try {
        const meResp = await fetch(`${apiUrl}/me`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (meResp.ok) me = await meResp.json();
    } catch {}

    // Platform admin gate matches the API-side `tenant_id == 1` convention.
    const isPlatformAdmin =
        me && me.is_platform_admin === true && me.tenant_id === 1;
    const isStandardUser = me && me.parent_user_id != null;

    const headers = { Authorization: `Bearer ${token}` };

    // Standard users get an aggregate-only view: a single dot + label,
    // no per-component breakdown. They don't need to know about queue
    // depth or in-flight runs at the tenant scope. The single probe used
    // for the aggregate is workers (the one component that affects
    // whether their submitted runs will execute).
    if (isStandardUser) {
        const probes = [await probeWorkers(headers)];
        const aggregate = aggregateState(probes);
        container.innerHTML = renderCard(aggregate, [], false, true);
        attachRefreshHandler(containerId);
        return;
    }

    // Tenant admin: tenant-scoped probes (workers + queue depth + in-flight).
    const tenantProbes = await Promise.all([
        probeWorkers(headers),
        probeQueueDepth(headers),
        probeInFlightRuns(headers),
    ]);

    // Platform-admin: launch-day-followup-5 §9 — render the deep
    // accordion driven by /health?extended=true (containers,
    // resources, recent events, integrations, database, TLS, license,
    // background services). Falls back to the basic render path if
    // the extended endpoint isn't reachable.
    // 2400UI28 — /health?extended=true is CE-denied (routers/platform_health.py
    // 404s it on-prem), so skip the extended fetch in CE and fall through to
    // the basic probe path. Fail-open: unreadable payload = not CE.
    const _extendedAvailable = isPlatformAdmin && await isPaidEdition();
    if (_extendedAvailable) {
        try {
            const xresp = await fetch(`${apiUrl}/health?extended=true`, { headers });
            if (xresp.ok) {
                const data = await xresp.json();
                // First render writes the whole skeleton; subsequent
                // refreshes patch only the changed cells (dot color,
                // label text, "checked X ago", and any section body
                // whose HTML changed). Avoids replacing innerHTML on
                // every poll which caused the entire panel to flicker
                // green→empty→green every 30s.
                const isFirstRender = !container.querySelector('[data-section]');
                if (isFirstRender) {
                    container.innerHTML = renderExtendedAccordion(data);
                    attachAccordionHandlers(container);
                } else {
                    patchExtendedAccordion(container, data);
                }
                attachRefreshHandler(containerId);
                return;
            }
        } catch {}
        // fallthrough to basic if extended fails
        const adminProbes = (await Promise.all([probeDetailedHealth(headers)])).flat();
        const allProbes = [...tenantProbes, ...adminProbes];
        const aggregate = aggregateState(allProbes);
        container.innerHTML = renderCard(aggregate, allProbes, isPlatformAdmin, false);
        document.dispatchEvent(new CustomEvent('mod:page-rendered')); // OZ-C3-P10: re-run the label hook after async widget render
        attachRefreshHandler(containerId);
        return;
    }

    const aggregate = aggregateState(tenantProbes);
    container.innerHTML = renderCard(aggregate, tenantProbes, isPlatformAdmin, false);
    document.dispatchEvent(new CustomEvent('mod:page-rendered')); // OZ-C3-P10: re-run the label hook after async widget render
    attachRefreshHandler(containerId);
}

// --- Probes ---------------------------------------------------------------
// Each probe returns { name, state: 'up'|'degraded'|'down', detail }

async function probeWorkers(headers) {
    // /health is public and returns the workers component already
    // computed by the API (active count > 0 → up, else degraded).
    try {
        const resp = await fetch(`${apiUrl}/health`, { headers });
        if (!resp.ok) return { name: 'Workers', themeKey: 'workers', state: 'down', detail: `HTTP ${resp.status}` };
        const data = await resp.json();
        const state = data?.components?.workers || 'down';
        return {
            name: 'Workers',
            themeKey: 'workers',
            state,
            detail: state === 'up' ? 'all healthy' : state,
        };
    } catch (e) {
        return { name: 'Workers', themeKey: 'workers', state: 'down', detail: String(e?.message || e) };
    }
}

async function probeQueueDepth(headers) {
    try {
        const resp = await fetch(
            `${apiUrl}/road/runs?status_filter=queued&limit=${QUEUE_DEPTH_DOWN_AT + 1}`,
            { headers },
        );
        if (!resp.ok) {
            return { name: 'Queue depth', state: 'down', detail: `HTTP ${resp.status}` };
        }
        const rows = await resp.json();
        const depth = Array.isArray(rows) ? rows.length : 0;
        let state = 'up';
        if (depth >= QUEUE_DEPTH_DOWN_AT) state = 'down';
        else if (depth >= QUEUE_DEPTH_DEGRADED_AT) state = 'degraded';
        return {
            name: 'Queue depth',
            state,
            detail: `${depth} queued${depth >= QUEUE_DEPTH_DOWN_AT ? '+' : ''}`,
        };
    } catch (e) {
        return { name: 'Queue depth', state: 'down', detail: String(e?.message || e) };
    }
}

async function probeInFlightRuns(headers) {
    try {
        const resp = await fetch(
            `${apiUrl}/road/runs?status_filter=running&limit=200`,
            { headers },
        );
        if (!resp.ok) {
            return { name: 'In-flight runs', state: 'up', detail: '0 running' };
        }
        const rows = await resp.json();
        const count = Array.isArray(rows) ? rows.length : 0;
        return {
            name: 'In-flight runs',
            state: 'up',
            detail: `${count} running`,
        };
    } catch (e) {
        return { name: 'In-flight runs', state: 'up', detail: '–' };
    }
}

async function probeDetailedHealth(headers) {
    // Returns array of probes — one per component the API knows about.
    try {
        const resp = await fetch(`${apiUrl}/health?verbose=true`, { headers });
        if (!resp.ok) {
            return [
                {
                    name: 'Platform health',
                    state: 'degraded',
                    detail: `HTTP ${resp.status}`,
                },
            ];
        }
        const data = await resp.json();
        const components = data?.components || {};
        const labels = {
            database: 'Database',
            keycloak: 'Auth',
            minio: 'Storage',
            redis: 'Cache',
            harbor: 'Registry',
            // We already render Workers via the public probe above, so
            // skip the duplicate here.
        };
        const out = [];
        for (const [key, label] of Object.entries(labels)) {
            const c = components[key];
            if (!c) continue;
            const state = c.status || 'down';
            const latency =
                c.latency_ms != null ? `${c.latency_ms}ms` : null;
            const errorBit = c.error ? ` — ${c.error}` : '';
            out.push({
                name: label,
                state,
                detail:
                    state === 'up'
                        ? latency || 'healthy'
                        : `${state}${latency ? ` (${latency})` : ''}${errorBit}`,
            });
        }
        return out;
    } catch (e) {
        return [{ name: 'Platform health', state: 'down', detail: String(e?.message || e) }];
    }
}

// (probeGpuPool was removed in home-page-finalize §6 — see comment in the
// main render fn. The GPU pool's right home is /admin/gpu-pool, not the
// home-page System Status aggregate.)

// --- Aggregation -----------------------------------------------------------

function aggregateState(probes) {
    if (probes.some((p) => p.state === 'down')) {
        return { state: 'red', label: 'Outage detected' };
    }
    if (probes.some((p) => p.state === 'degraded')) {
        return { state: 'yellow', label: 'Some components degraded' };
    }
    return { state: 'green', label: 'All systems normal' };
}

// --- Render ----------------------------------------------------------------

function renderSkeleton() {
    return `
        <div class="bg-white p-6 rounded-xl shadow-md h-full">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">System Status</h2>
            </div>
            <div class="space-y-2 animate-pulse">
                <div class="flex items-center gap-2"><div class="w-3 h-3 bg-gray-200 rounded-full"></div><div class="h-3 bg-gray-200 rounded w-1/3"></div></div>
                <div class="flex items-center gap-2"><div class="w-3 h-3 bg-gray-200 rounded-full"></div><div class="h-3 bg-gray-200 rounded w-1/2"></div></div>
                <div class="flex items-center gap-2"><div class="w-3 h-3 bg-gray-200 rounded-full"></div><div class="h-3 bg-gray-200 rounded w-2/5"></div></div>
            </div>
        </div>
    `;
}

function renderCard(aggregate, probes, isPlatformAdmin, isStandardUser) {
    const dotColor = {
        green: 'bg-green-500',
        yellow: 'bg-yellow-500',
        red: 'bg-red-500',
    }[aggregate.state];
    const labelColor = {
        green: 'text-green-700',
        yellow: 'text-yellow-700',
        red: 'text-red-700',
    }[aggregate.state];

    const componentRows = probes
        .map(
            (p) => `
        <li class="flex items-center justify-between py-1 text-sm">
            <span class="flex items-center gap-2">
                <span class="w-2 h-2 rounded-full ${stateDotClass(p.state)}"></span>
                <span class="text-gray-700">${p.themeKey ? `<span data-theme-key="${escapeHtml(p.themeKey)}">${escapeHtml(p.name)}</span>` : escapeHtml(p.name)}</span>
            </span>
            <span class="text-xs text-gray-500">${escapeHtml(p.detail || '')}</span>
        </li>`,
        )
        .join('');

    const titleSuffix = isPlatformAdmin
        ? '<span class="text-xs text-gray-400 ml-2">platform view</span>'
        : '';

    // Standard users: aggregate dot + label only — no per-component
    // breakdown. They don't need infrastructure detail.
    const breakdownHtml = isStandardUser
        ? ''
        : `<ul class="divide-y divide-gray-100">${componentRows}</ul>`;

    return `
        <div class="bg-white p-6 rounded-xl shadow-md h-full">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">System Status${titleSuffix}</h2>
                <button id="system-status-refresh-btn"
                    class="text-gray-400 hover:text-primary/80 transition-colors p-1 rounded"
                    title="Refresh">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                    </svg>
                </button>
            </div>
            <div class="flex items-center gap-3 ${isStandardUser ? '' : 'mb-4'}">
                <span class="w-3 h-3 rounded-full ${dotColor}"></span>
                <span class="${labelColor} font-semibold">${escapeHtml(aggregate.label)}</span>
            </div>
            ${breakdownHtml}
        </div>
    `;
}

function stateDotClass(state) {
    return {
        up: 'bg-green-500',
        degraded: 'bg-yellow-500',
        down: 'bg-red-500',
    }[state] || 'bg-gray-400';
}

function attachRefreshHandler(containerId) {
    const btn = document.getElementById('system-status-refresh-btn');
    if (!btn || btn.dataset.refreshHandlerArmed === '1') return;
    btn.dataset.refreshHandlerArmed = '1';
    btn.addEventListener('click', async () => {
        if (_refreshInFlight) return;
        _refreshInFlight = true;
        btn.disabled = true;
        try {
            await renderSystemStatusWidget(containerId);
        } finally {
            _refreshInFlight = false;
            btn.disabled = false;
        }
    });
}

function escapeHtml(s) {
    return String(s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}


// =====================================================================
// launch-day-followup-5 §9C — Deep system status accordion (admin only)
// =====================================================================

function _aggBadge(state) {
    const cls = {
        ok:      'bg-green-100 text-green-800 border border-green-200',
        degraded:'bg-yellow-100 text-yellow-800 border border-yellow-200',
        down:    'bg-red-100 text-red-900 border border-red-200',
    }[state] || 'bg-gray-100 text-gray-800 border border-gray-200';
    const label = { ok: 'Healthy', degraded: 'Degraded', down: 'Outage' }[state] || 'Unknown';
    return `<span class="px-2 py-0.5 rounded-full text-xs font-medium ${cls}">${label}</span>`;
}

function _dot(state) {
    const cls = {
        ok: 'bg-green-500',
        running: 'bg-green-500',
        healthy: 'bg-green-500',
        degraded: 'bg-yellow-500',
        warning: 'bg-yellow-500',
        restarting: 'bg-yellow-500',
        unhealthy: 'bg-red-500',
        exited: 'bg-red-500',
        dead: 'bg-red-500',
        down: 'bg-red-500',
    }[state] || 'bg-gray-400';
    return `<span class="inline-block w-2 h-2 rounded-full ${cls}"></span>`;
}

function _pre(text) {
    return `<pre class="bg-gray-50 text-xs text-gray-700 p-2 rounded mt-1 overflow-x-auto whitespace-pre-wrap">${escapeHtml(text)}</pre>`;
}

function _fmtTimeAgo(iso) {
    if (!iso) return '–';
    const t = new Date(iso).getTime();
    if (isNaN(t)) return iso;
    const secs = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (secs < 60) return `${secs}s ago`;
    if (secs < 3600) return `${Math.floor(secs/60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs/3600)}h ago`;
    return `${Math.floor(secs/86400)}d ago`;
}

function _fmtBytes(b) {
    if (b == null) return '–';
    const units = ['B','KB','MB','GB','TB'];
    let i = 0;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return `${b.toFixed(1)} ${units[i]}`;
}

// Init / one-shot setup containers exit cleanly by design — they
// run once at startup (Vault unseal, Keycloak realm seed, MinIO
// bucket setup, Harbor cert prep) and aren't supposed to keep
// running. Don't flag them as unhealthy.
const _INIT_NAMES = new Set([
    'my_initializer','my_keycloak_init','my_minio_setup','my_minio_mc',
    'my_vault_init','my_vault_unseal','my_tenant_harbor_prepare',
]);

function _splitContainers(containers) {
    const problems = containers.filter(c => !_INIT_NAMES.has(c.name) && (c.state === 'exited' || c.state === 'dead' || c.health === 'unhealthy' || (c.state === 'restarting' && c.restart_count >= 3)));
    const ok = containers.filter(c => !problems.includes(c));
    return { problems, ok };
}

function _containerMetaText(c) {
    return `${c.state}${c.restart_count ? ` · ${c.restart_count} restarts` : ''}${c.exit_code != null ? ` · exit ${c.exit_code}` : ''}`;
}

function _renderContainerRow(c, problem) {
    const fix = problem
        ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre(`docker logs ${c.name} --tail=100\ndocker restart ${c.name}`)}</div>`
        : '';
    // data-container-row + the granular .container-* spans let
    // patchContainersSection() update individual cells without
    // touching the rest of the list — see the patcher below.
    return `<li class="py-1.5 border-b border-gray-100 last:border-0" data-container-row="${escapeHtml(c.name)}">
        <div class="flex items-center justify-between text-sm">
            <span class="flex items-center gap-2"><span class="container-dot inline-block w-2 h-2 rounded-full ${_dotClass(c.state, c.health)}"></span><span class="font-medium text-gray-800">${escapeHtml(c.name)}</span></span>
            <span class="container-meta text-xs text-gray-500">${escapeHtml(_containerMetaText(c))}</span>
        </div>
        <div class="container-fix">${fix}</div>
    </li>`;
}

function _dotClass(state, health) {
    const s = health === 'unhealthy' ? 'unhealthy' : state;
    return ({
        ok: 'bg-green-500', running: 'bg-green-500', healthy: 'bg-green-500',
        degraded: 'bg-yellow-500', warning: 'bg-yellow-500', restarting: 'bg-yellow-500',
        unhealthy: 'bg-red-500', exited: 'bg-red-500', dead: 'bg-red-500', down: 'bg-red-500',
    })[s] || 'bg-gray-400';
}

function _renderContainersSection(d) {
    const containers = (d?.containers?.containers) || [];
    if (d?.containers?.error) {
        return `<div class="text-sm text-red-700">Sidecar unreachable: ${escapeHtml(d.containers.error)}</div>
            <div class="mt-2 text-xs text-gray-600">Manual fix: <code>docker logs mod_platform_monitor --tail=100</code></div>`;
    }
    const { problems, ok } = _splitContainers(containers);
    const headerHtml = problems.length
        ? `<div class="containers-summary text-sm font-medium text-red-700 mb-1">${problems.length} container${problems.length !== 1 ? 's' : ''} unhealthy</div>`
        : `<div class="containers-summary text-sm text-green-700 mb-1">All ${containers.length} containers healthy</div>`;
    return `
        ${headerHtml}
        <ul class="containers-problems">${problems.map(c => _renderContainerRow(c, true)).join('')}</ul>
        ${ok.length ? `<details class="mt-2 containers-healthy-wrap"><summary class="text-xs text-gray-600 cursor-pointer containers-healthy-summary">Show ${ok.length} healthy</summary><ul class="containers-healthy">${ok.map(c => _renderContainerRow(c, false)).join('')}</ul></details>` : ''}
    `;
}

// Per-row patcher for the containers section. Walks the existing
// DOM by `data-container-row="<name>"` and updates only the cells
// whose value changed (dot class, meta text, manual-fix block).
// Adds new rows at the end, removes rows for containers that
// vanished. The healthy <details> open/closed state is preserved.
// This is the difference between "panel flickers every 30s" and
// "panel is invisibly correct".
function _patchContainersSection(sectionEl, d) {
    const containers = (d?.containers?.containers) || [];
    if (d?.containers?.error) {
        // Fall back to full innerHTML on error path (rare, no flicker concern).
        sectionEl.innerHTML = _renderContainersSection(d);
        return;
    }
    const { problems, ok } = _splitContainers(containers);

    // Update summary line.
    const summaryEl = sectionEl.querySelector('.containers-summary');
    const desiredSummary = problems.length
        ? `${problems.length} container${problems.length !== 1 ? 's' : ''} unhealthy`
        : `All ${containers.length} containers healthy`;
    if (summaryEl && summaryEl.textContent !== desiredSummary) {
        summaryEl.textContent = desiredSummary;
        summaryEl.className = problems.length
            ? 'containers-summary text-sm font-medium text-red-700 mb-1'
            : 'containers-summary text-sm text-green-700 mb-1';
    }

    const updateList = (listEl, rows, isProblem) => {
        if (!listEl) return;
        const existing = new Map();
        listEl.querySelectorAll('[data-container-row]').forEach(el => {
            existing.set(el.getAttribute('data-container-row'), el);
        });
        const wantedNames = new Set(rows.map(r => r.name));
        // Remove rows that vanished.
        for (const [name, el] of existing.entries()) {
            if (!wantedNames.has(name)) el.remove();
        }
        // Add or patch each desired row.
        for (const c of rows) {
            const el = existing.get(c.name);
            if (!el) {
                listEl.insertAdjacentHTML('beforeend', _renderContainerRow(c, isProblem));
                continue;
            }
            const dot = el.querySelector('.container-dot');
            const desiredDotClass = `container-dot inline-block w-2 h-2 rounded-full ${_dotClass(c.state, c.health)}`;
            if (dot && dot.className !== desiredDotClass) dot.className = desiredDotClass;
            const meta = el.querySelector('.container-meta');
            const desiredMeta = _containerMetaText(c);
            if (meta && meta.textContent !== desiredMeta) meta.textContent = desiredMeta;
            const fixDiv = el.querySelector('.container-fix');
            const desiredFix = isProblem
                ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre(`docker logs ${c.name} --tail=100\ndocker restart ${c.name}`)}</div>`
                : '';
            if (fixDiv && fixDiv.innerHTML !== desiredFix) fixDiv.innerHTML = desiredFix;
        }
    };

    updateList(sectionEl.querySelector('.containers-problems'), problems, true);
    let healthyList = sectionEl.querySelector('.containers-healthy');
    let healthyWrap = sectionEl.querySelector('.containers-healthy-wrap');
    if (ok.length === 0) {
        if (healthyWrap) healthyWrap.remove();
    } else {
        if (!healthyWrap) {
            // Healthy list didn't exist before but now does — add it.
            sectionEl.insertAdjacentHTML(
                'beforeend',
                `<details class="mt-2 containers-healthy-wrap"><summary class="text-xs text-gray-600 cursor-pointer containers-healthy-summary">Show ${ok.length} healthy</summary><ul class="containers-healthy"></ul></details>`,
            );
            healthyList = sectionEl.querySelector('.containers-healthy');
        }
        const summaryToggle = sectionEl.querySelector('.containers-healthy-summary');
        const desiredToggle = `Show ${ok.length} healthy`;
        if (summaryToggle && summaryToggle.textContent !== desiredToggle) summaryToggle.textContent = desiredToggle;
        updateList(healthyList, ok, false);
    }
}

function _renderResourcesSection(d) {
    const r = d?.resources || {};
    if (r.error) return `<div class="text-sm text-red-700">${escapeHtml(r.error)}</div>`;
    const volRows = (r.volumes || []).map(v => {
        const pct = v.percent_used || 0;
        const state = pct >= 90 ? 'down' : pct >= 80 ? 'warning' : 'ok';
        const fix = pct >= 80 ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre(`du -sh /var/lib/docker/volumes/${v.name}/_data/* | sort -h | tail`)}</div>` : '';
        return `<li class="py-1 text-sm">
            <div class="flex items-center justify-between">
                <span class="flex items-center gap-2">${_dot(state)}<span class="text-gray-800">${escapeHtml(v.name)}</span></span>
                <span class="text-xs text-gray-500">${pct.toFixed(1)}% · ${_fmtBytes(v.used_bytes)} / ${_fmtBytes(v.total_bytes)}</span>
            </div>${fix}
        </li>`;
    }).join('');
    const memState = r.memory_percent_used >= 95 ? 'warning' : 'ok';
    const swapState = r.swap_percent_used >= 50 ? 'warning' : 'ok';
    return `
        <div class="grid grid-cols-3 gap-2 text-sm mb-2">
            <div><div class="text-xs text-gray-500">CPU</div><div class="font-medium">${(r.cpu_percent||0).toFixed(1)}%</div></div>
            <div><div class="text-xs text-gray-500">Memory ${_dot(memState)}</div><div class="font-medium">${(r.memory_percent_used||0).toFixed(1)}%</div></div>
            <div><div class="text-xs text-gray-500">Swap ${_dot(swapState)}</div><div class="font-medium">${(r.swap_percent_used||0).toFixed(1)}%</div></div>
        </div>
        <ul>${volRows}</ul>
    `;
}

function _renderEventsSection(d) {
    const events = (d?.recent_events?.events) || [];
    if (d?.recent_events?.error) return `<div class="text-sm text-red-700">${escapeHtml(d.recent_events.error)}</div>`;
    if (!events.length) return `<div class="text-sm text-gray-600">No notable events in the last 24h.</div>`;
    return `<ul>${events.slice(0, 20).map(ev => `
        <li class="py-1 text-sm flex items-center justify-between">
            <span class="flex items-center gap-2">${_dot(ev.action === 'die' ? 'down' : 'warning')}<span class="text-gray-800">${escapeHtml(ev.name)}</span><span class="text-xs text-gray-500">${escapeHtml(ev.action)}${ev.exit_code != null ? ` · exit ${ev.exit_code}` : ''}</span></span>
            <span class="text-xs text-gray-500">${_fmtTimeAgo(ev.timestamp)}</span>
        </li>
    `).join('')}</ul>`;
}

function _renderIntegrationsSection(d) {
    const pm = d?.integrations?.postmark || {};
    const stripe = {};
    const pmState = pm.is_stale || pm.recent_bounces_24h > 0 || pm.users_flagged > 0 ? 'warning' : 'ok';
    const stripeState = stripe.is_stale ? 'warning' : 'ok';
    const pmFix = (pmState !== 'ok') ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre('# Check Postmark dashboard for account suspension or DKIM rotation\n# https://account.postmarkapp.com/servers')}</div>` : '';
    const stripeFix = stripeState !== 'ok' ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre('# Check Stripe webhook delivery & resend missed events\n# https://dashboard.stripe.com/webhooks')}</div>` : '';
    return `
        <ul>
            <li class="py-1 text-sm">
                <div class="flex items-center justify-between">
                    <span class="flex items-center gap-2">${_dot(pmState)}<span class="text-gray-800">Postmark email</span></span>
                    <span class="text-xs text-gray-500">last test: ${_fmtTimeAgo(pm.last_tested_at)} · bounces 24h: ${pm.recent_bounces_24h ?? 0} · flagged: ${pm.users_flagged ?? 0}</span>
                </div>${pmFix}
            </li>
            <li class="py-1 text-sm">
                <div class="flex items-center justify-between">
                    <span class="flex items-center gap-2">${_dot(stripeState)}<span class="text-gray-800">Stripe webhooks</span></span>
                    <span class="text-xs text-gray-500">last received: ${_fmtTimeAgo(stripe.last_webhook_at)} · active subs: ${stripe.active_subscriptions ?? 0}</span>
                </div>${stripeFix}
            </li>
        </ul>
    `;
}

function _renderDatabaseSection(d) {
    const pool = d?.database?.pool || {};
    const vault = d?.database?.vault_rotation || {};
    const poolState = pool.saturated ? 'warning' : 'ok';
    const poolFix = pool.saturated ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre("docker exec my_postgres_db psql -U modTech -d modTech -c \"SELECT pid, state, query_start, query FROM pg_stat_activity WHERE state != 'idle' ORDER BY query_start;\"")}</div>` : '';
    return `
        <ul>
            <li class="py-1 text-sm">
                <div class="flex items-center justify-between">
                    <span class="flex items-center gap-2">${_dot(poolState)}<span class="text-gray-800">Postgres connection pool</span></span>
                    <span class="text-xs text-gray-500">${pool.checked_out ?? 0} in use / ${(pool.size ?? 0) + (pool.overflow ?? 0)} max${pool.overflow ? ` (incl. ${pool.overflow} overflow)` : ''}</span>
                </div>${poolFix}
            </li>
            <li class="py-1 text-sm flex items-center justify-between">
                <span class="flex items-center gap-2">${_dot(vault.agent_token_present ? 'ok' : 'warning')}<span class="text-gray-800">Vault rotation</span></span>
                <span class="text-xs text-gray-500">last: ${_fmtTimeAgo(vault.last_rotated_at)}</span>
            </li>
        </ul>
    `;
}

function _renderCertsAndLicenseSection(d) {
    const certs = (d?.tls_certs?.certs) || [];
    const lic = d?.license || {};
    const certRows = certs.map(c => {
        const days = c.days_remaining;
        const state = c.error ? 'warning' : (days != null && days < 14 ? 'warning' : (days != null && days < 30 ? 'warning' : 'ok'));
        const fix = (state !== 'ok' && !c.error) ? `<div class="mt-1 text-xs text-gray-600 manual-fix"><div class="font-medium text-gray-700">Manual fix:</div>${_pre(`# Renew TLS cert for ${c.domain}\ncertbot renew --cert-name ${c.domain}\n# or via your hosting provider's TLS panel`)}</div>` : '';
        return `<li class="py-1 text-sm">
            <div class="flex items-center justify-between">
                <span class="flex items-center gap-2">${_dot(state)}<span class="text-gray-800">${escapeHtml(c.domain)}</span></span>
                <span class="text-xs text-gray-500">${c.error ? escapeHtml(c.error) : (days != null ? `${days}d remaining` : '–')}</span>
            </div>${fix}
        </li>`;
    }).join('');
    const licState = lic.expired ? 'down' : (lic.days_until_expiry != null && lic.days_until_expiry < 30 ? 'warning' : 'ok');
    const licRow = `<li class="py-1 text-sm flex items-center justify-between">
        <span class="flex items-center gap-2">${_dot(licState)}<span class="text-gray-800">Platform license</span></span>
        <span class="text-xs text-gray-500">${lic.days_until_expiry != null ? `${lic.days_until_expiry}d remaining` : (lic.status || '–')}</span>
    </li>`;
    return `<ul>${certRows}${licRow}</ul>`;
}

function _renderBackgroundSection(d) {
    const bg = d?.background_services || {};
    const jobs = (bg.scheduled_jobs && bg.scheduled_jobs.jobs) || [];
    if (!jobs.length) {
        return `<div class="text-sm text-gray-600">No scheduled jobs reported.</div>`;
    }
    return `
        <ul>
            <li class="text-xs text-gray-600 mt-1 mb-1">Scheduled jobs:</li>
            ${jobs.slice(0, 10).map(j => `
                <li class="py-1 text-sm flex items-center justify-between">
                    <span class="flex items-center gap-2 text-gray-700">${_dot('ok')}<span class="text-xs">${escapeHtml(j.id)}</span></span>
                    <span class="text-xs text-gray-500">last: ${_fmtTimeAgo(j.last_run_at)} · next: ${_fmtTimeAgo(j.next_run_at)}</span>
                </li>
            `).join('')}
        </ul>
    `;
}

function _accordionSection(id, title, body, defaultOpen = false) {
    return `
    <details class="border border-gray-200 rounded-lg p-3" ${defaultOpen ? 'open' : ''} data-section="${id}">
        <summary class="cursor-pointer select-none font-medium text-gray-800 flex items-center justify-between">
            <span>${title}</span>
            <span class="text-xs text-gray-500">click to expand</span>
        </summary>
        <div class="mt-3">${body}</div>
    </details>`;
}

function renderExtendedAccordion(data) {
    const agg = data.aggregate_status || 'ok';
    const aggDot = { ok: 'bg-green-500', degraded: 'bg-yellow-500', down: 'bg-red-500' }[agg] || 'bg-gray-400';
    const aggLabel = { ok: 'All systems normal', degraded: 'Some components degraded', down: 'Outage detected' }[agg] || 'Unknown';
    const checkedAt = _fmtTimeAgo(data.checked_at);
    // Hook classes (`system-status-aggregate-dot`, etc.) are how
    // patchExtendedAccordion() finds and updates these cells on
    // refresh without rewriting the whole panel.
    return `
    <div class="bg-white p-6 rounded-xl shadow-md h-full">
        <div class="flex items-center justify-between mb-4">
            <h2 class="text-xl font-semibold text-gray-700">System Status</h2>
            <button id="system-status-refresh-btn" class="status-refresh-btn text-xs text-gray-500 hover:text-gray-700">Refresh</button>
        </div>
        <div class="flex items-center gap-3 mb-4">
            <span class="system-status-aggregate-dot inline-block w-3 h-3 rounded-full ${aggDot}"></span>
            <span class="system-status-aggregate-label text-sm font-medium text-gray-800">${aggLabel}</span>
            <span class="system-status-checked-at text-xs text-gray-500 ml-auto">checked ${checkedAt}</span>
        </div>
        <div class="space-y-2 text-sm">
            ${_accordionSection('containers', `<span class="system-status-section-title">Containers</span> <span class="system-status-section-badge">${_aggBadge(agg === 'ok' ? 'ok' : 'degraded')}</span>`, _renderContainersSection(data), agg !== 'ok')}
            ${_accordionSection('resources', '<span class="system-status-section-title">Resources & disk</span>', _renderResourcesSection(data))}
            ${_accordionSection('events', '<span class="system-status-section-title">Recent events (24h)</span>', _renderEventsSection(data))}
            ${_accordionSection('integrations', '<span class="system-status-section-title">Integrations</span>', _renderIntegrationsSection(data))}
            ${_accordionSection('database', '<span class="system-status-section-title">Database</span>', _renderDatabaseSection(data))}
            ${_accordionSection('certs', '<span class="system-status-section-title">TLS certs & license</span>', _renderCertsAndLicenseSection(data))}
            ${_accordionSection('background', '<span class="system-status-section-title">Background services</span>', _renderBackgroundSection(data))}
        </div>
    </div>`;
}

// Diff-based update: patch only the cells whose values changed.
// Preserves every <details>'s open/closed state and avoids the
// flicker caused by full innerHTML replacement on the auto-refresh.
const _SECTION_RENDERERS = [
    // containers handled specially via _patchContainersSection
    ['resources', _renderResourcesSection],
    ['events', _renderEventsSection],
    ['integrations', _renderIntegrationsSection],
    ['database', _renderDatabaseSection],
    ['certs', _renderCertsAndLicenseSection],
    ['background', _renderBackgroundSection],
];

// Section "interesting hash" — a stable string of just the values
// that should trigger a DOM update. We deliberately exclude noisy
// fields (exact CPU% jitter, sub-minute timestamps) so the panel
// stays still when nothing meaningfully changed. If the hash is
// equal to the previous poll's, we don't touch the DOM at all.
function _round5(v) { return v == null ? null : Math.round(v / 5) * 5; }
function _bucketMin(iso) { return iso ? Math.floor(new Date(iso).getTime() / 60000) : null; }

function _sectionHash(id, data) {
    try {
        switch (id) {
            case 'containers': {
                const cs = (data?.containers?.containers) || [];
                return JSON.stringify({
                    err: data?.containers?.error || null,
                    rows: cs.map(c => [c.name, c.state, c.health || null, c.restart_count || 0, c.exit_code ?? null]).sort(),
                });
            }
            case 'resources': {
                const r = data?.resources || {};
                return JSON.stringify({
                    err: r.error || null,
                    cpu: _round5(r.cpu_percent),
                    mem: _round5(r.memory_percent_used),
                    swap: _round5(r.swap_percent_used),
                    vols: (r.volumes || []).map(v => [v.name, _round5(v.percent_used)]).sort(),
                });
            }
            case 'events': {
                const evs = (data?.recent_events?.events) || [];
                return JSON.stringify({
                    err: data?.recent_events?.error || null,
                    list: evs.slice(0, 20).map(e => [e.name, e.action, e.exit_code ?? null, _bucketMin(e.timestamp)]),
                });
            }
            case 'integrations': {
                const pm = data?.integrations?.postmark || {};
                const st = {};
                return JSON.stringify({
                    pm_stale: !!pm.is_stale,
                    pm_b: pm.recent_bounces_24h ?? 0,
                    pm_f: pm.users_flagged ?? 0,
                    pm_t: _bucketMin(pm.last_tested_at),
                    st_stale: !!st.is_stale,
                    st_w: _bucketMin(st.last_webhook_at),
                    st_a: st.active_subscriptions ?? 0,
                });
            }
            case 'database': {
                const p = data?.database?.pool || {};
                const v = data?.database?.vault_rotation || {};
                return JSON.stringify({
                    sat: !!p.saturated,
                    co: p.checked_out ?? 0,
                    sz: (p.size ?? 0) + (p.overflow ?? 0),
                    of: p.overflow ?? 0,
                    vt: !!v.agent_token_present,
                    vr: _bucketMin(v.last_rotated_at),
                });
            }
            case 'certs': {
                const certs = (data?.tls_certs?.certs) || [];
                const lic = data?.license || {};
                return JSON.stringify({
                    certs: certs.map(c => [c.domain, c.days_remaining ?? null, !!c.error]).sort(),
                    lic: [!!lic.expired, lic.days_until_expiry ?? null, lic.status || null],
                });
            }
            case 'background': {
                const jobs = (data?.background_services?.scheduled_jobs?.jobs) || [];
                return JSON.stringify(
                    jobs.slice(0, 10).map(j => [j.id, _bucketMin(j.last_run_at), _bucketMin(j.next_run_at)]).sort(),
                );
            }
        }
    } catch {
        return Math.random().toString();
    }
    return '';
}

const _patchCache = new WeakMap();

function patchExtendedAccordion(container, data) {
    const cache = _patchCache.get(container) || { sections: {}, badge: null, agg: null };

    const agg = data.aggregate_status || 'ok';
    const aggDot = { ok: 'bg-green-500', degraded: 'bg-yellow-500', down: 'bg-red-500' }[agg] || 'bg-gray-400';
    const aggLabel = { ok: 'All systems normal', degraded: 'Some components degraded', down: 'Outage detected' }[agg] || 'Unknown';
    const checkedAt = _fmtTimeAgo(data.checked_at);

    // Top header — aggregate dot + label + "checked X ago" stamp.
    // These three are surgical patches; only the value that
    // changed gets touched.
    const dotEl = container.querySelector('.system-status-aggregate-dot');
    if (dotEl) {
        const desired = `system-status-aggregate-dot inline-block w-3 h-3 rounded-full ${aggDot}`;
        if (dotEl.className !== desired) dotEl.className = desired;
    }
    const labelEl = container.querySelector('.system-status-aggregate-label');
    if (labelEl && labelEl.textContent !== aggLabel) labelEl.textContent = aggLabel;
    const checkedEl = container.querySelector('.system-status-checked-at');
    const checkedText = `checked ${checkedAt}`;
    if (checkedEl && checkedEl.textContent !== checkedText) checkedEl.textContent = checkedText;

    // Containers section header badge.
    if (cache.agg !== agg) {
        const badgeWrap = container.querySelector(
            'details[data-section="containers"] > summary .system-status-section-badge'
        );
        if (badgeWrap) badgeWrap.innerHTML = _aggBadge(agg === 'ok' ? 'ok' : 'degraded');
        cache.agg = agg;
    }

    // Containers section body — per-row patching (never replaces the
    // whole list; updates only the rows whose state changed).
    const containersHash = _sectionHash('containers', data);
    if (cache.sections.containers !== containersHash) {
        const body = container.querySelector('details[data-section="containers"] > div.mt-3');
        if (body) _patchContainersSection(body, data);
        cache.sections.containers = containersHash;
    }

    // Other sections — skip entirely if hash unchanged. When it
    // changes (e.g., CPU% crossed a 5% bucket boundary, a new event
    // arrived) we re-render that section's body. Most polls produce
    // no DOM update at all.
    for (const [id, renderer] of _SECTION_RENDERERS) {
        const hash = _sectionHash(id, data);
        if (cache.sections[id] === hash) continue;
        const body = container.querySelector(`details[data-section="${id}"] > div.mt-3`);
        if (body) {
            body.innerHTML = renderer(data);
            cache.sections[id] = hash;
        }
    }

    _patchCache.set(container, cache);
}

function attachAccordionHandlers(container) {
    const btn = container.querySelector('.status-refresh-btn');
    if (btn && btn.dataset.refreshHandlerArmed !== '1') {
        btn.dataset.refreshHandlerArmed = '1';
        btn.addEventListener('click', async () => {
            if (_refreshInFlight) return;
            _refreshInFlight = true;
            btn.disabled = true;
            try {
                await renderSystemStatusWidget(container.id);
            } finally {
                _refreshInFlight = false;
                btn.disabled = false;
            }
        });
    }
    // Auto-refresh every 60s while the panel is on screen. The
    // patcher above skips DOM writes when nothing changed, so most
    // ticks are silent — but 60s is still kinder than 30s for the
    // cases where something DID flip and we have to re-render.
    if (!container.dataset.autoRefreshArmed) {
        container.dataset.autoRefreshArmed = '1';
        // SPA remount: the dataset guard is per-element, so each re-injected
        // container would spawn a NEW 60s timer while the old ones zombie.
        // Keep a single module-level interval id: clear the prior timer before
        // starting the new one, and self-stop when the panel leaves the DOM.
        if (_autoRefreshTimer) clearInterval(_autoRefreshTimer);
        _autoRefreshTimer = setInterval(() => {
            if (!document.body.contains(container)) {
                clearInterval(_autoRefreshTimer);
                _autoRefreshTimer = null;
                return;
            }
            if (!_refreshInFlight) renderSystemStatusWidget(container.id);
        }, 60_000);
    }
}
