// Dashboard/js/workers.js

import { loadResourcePermissions } from './resource-permissions.js';
import { isPlatformAdmin } from './utils.js';
import { adminStorage, isPaidEdition } from './api.js';

let refreshInterval = null;
let _isAdmin = false;
// worker_name -> the row object, so the detail modal can render its cost basis.
const _workersByName = new Map();
// Set once the API returns 403 for this user, so the 5-second refresh
// interval stops self-perpetuating and we don't log the same forbidden
// error every tick.
let _accessDenied = false;
// Track worker state across refreshes so we can detect update-cycle completion
// and show a success toast when a worker returns on a new agent_version.
// Key: worker_name. Value: { update_requested, agent_version }.
let _prevWorkerState = new Map();
// Latest release metadata from /workers/latest-release. Cached
// and refreshed on each loadWorkers() tick so the Dashboard tooltips and
// detail modal can show release date + changelog link.
let _latestRelease = { version: null, released_at: null, changelog_url: null };

// FIX-ADD-WORKER-MODAL-API-URL-LAN (11-15) BEGIN
// The Add Worker modal prints commands a DIFFERENT machine runs (the worker
// curls the setup script). On a default CE install APP_CONFIG.API_URL is a
// loopback name (http://localhost:8000/v1), which only resolves on the CE host
// itself, so a worker enrolled from a second computer gets a command it cannot
// run. When the operator opened the dashboard over the LAN, window.location.hostname
// IS an address both machines can reach, so swap the loopback API host for it
// (protocol, port and path kept). Same-host browsing keeps the honest loopback
// value, and a --lan install already yields the LAN IP. No env vars, no
// hand-set variables (owner rule: CE LAN access is first-class).
// Browser-side fetches keep the raw API_URL (the browser is on that host); only
// the displayed enrollment/setup commands use this.
const ENROLL_LOOPBACK_HOSTS = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0', 'api.localhost']);
function enrollApiUrl(apiUrl) {
    try {
        const u = new URL(apiUrl);
        const pageHost = (typeof window === 'object' && window.location) ? String(window.location.hostname || '') : '';
        if (!pageHost || ENROLL_LOOPBACK_HOSTS.has(pageHost.toLowerCase())) return apiUrl;
        if (!ENROLL_LOOPBACK_HOSTS.has(String(u.hostname || '').toLowerCase())) return apiUrl;
        u.hostname = pageHost;
        return u.toString();
    } catch (_) {
        return apiUrl;
    }
}
// FIX-ADD-WORKER-MODAL-API-URL-LAN END

// FIX-DASHBOARD-COPY-BTN-INSECURE-ORIGIN (11-17) BEGIN
// The Add Worker modal's Copy buttons must work over plain http on the LAN
// (first-class CE LAN access): navigator.clipboard only exists in secure
// contexts (https/localhost), so on http://<lan-ip> writeText is undefined and
// an inline onclick throws silently - the button appears to do nothing. Use the
// async Clipboard API when the page is a secure context and writeText is
// available, else fall back to a hidden textarea + document.execCommand('copy')
// (the idiom the Logs page already ships). Always give visible feedback on the
// button: 'Copied!' or 'Copy failed: select and copy manually' - the button
// never fails silently. What is copied is unchanged.
async function copyTextToClipboard(text) {
    try {
        if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch (_) { /* fall through to the legacy path */ }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return !!ok;
    } catch (_) {
        return false;
    }
}

async function copySetupCmd(which) {
    const id = which === 'linux' ? 'setup-cmd-text' : `setup-cmd-text-${which}`;
    const btnId = which === 'linux' ? 'copy-cmd-btn' : `copy-cmd-btn-${which}`;
    const el = document.getElementById(id);
    const btn = document.getElementById(btnId);
    if (!el) return false;
    const ok = await copyTextToClipboard(el.textContent);
    if (btn) {
        btn.textContent = ok ? 'Copied!' : 'Copy failed: select and copy manually';
        btn.classList.replace('bg-primary/80', ok ? 'bg-green-500' : 'bg-red-500');
        setTimeout(() => {
            btn.textContent = 'Copy';
            btn.classList.replace(ok ? 'bg-green-500' : 'bg-red-500', 'bg-primary/80');
        }, ok ? 2000 : 6000);
    }
    return ok;
}
window.__copySetupCmd = (which) => copySetupCmd(which);
// FIX-DASHBOARD-COPY-BTN-INSECURE-ORIGIN END

window.showWorkerDetail = (...args) => showWorkerDetail(...args);
window.triggerBenchmark = (...args) => triggerBenchmark(...args);
window.deleteWorker = (...args) => deleteWorker(...args);

export async function initializeWorkersPageListeners() {
    console.log("Initializing Workers page...");

    // Initial load
    const currentUser = await loadWorkers();
    if (_accessDenied) return;
    if (currentUser?.is_platform_admin && Number(currentUser?.tenant_id) === 1) {
        await loadLicenseSummaryIfPaid();
        // wizard-rebuild §9d — variant-test pending queue depth.
        // Surfaces per-accelerator counts so operators see which
        // hardware the wizard is waiting on.
        await loadVariantTestPendingQueueDepth();
        // noah-observability — fleet-wide NOAH idle-harvest status card.
        await loadNoahFleet();
    }

    // Tenant-admin UI gating: the platform-admin-only affordances on
    // this page (Add Worker, System Queue) hide for anyone who isn't a
    // platform admin. The backing endpoints 403 either way; this just
    // avoids dead clicks.
    // scheduler-runtime-config-panel — Generate Training Data moved to
    // Settings → AI Configuration → Scheduler.
    if (!isPlatformAdmin(currentUser)) {
        document.getElementById('add-remote-worker-btn')?.classList.add('hidden');
        document.getElementById('system-queue-btn')?.classList.add('hidden');
        document.getElementById('fleet-storage-loading')?.closest('.mt-6')?.classList.add('hidden');
    } else {
        // megaplan 25.3 Q.5b — platform-admin fleet storage. Only fetched
        // here, gated behind isPlatformAdmin (this whole page is also
        // only ever loaded for platform admins — analytics.js gates the
        // 'workers' tab — this is belt-and-braces).
        await loadFleetStorage();
    }

    // Add Remote Worker button
    document.getElementById('add-remote-worker-btn')?.addEventListener('click', () => showAddWorkerModal());

    // Refresh button
    document.getElementById('refresh-workers-btn')?.addEventListener('click', async () => {
        const user = await loadWorkers();
        if (user?.is_platform_admin && Number(user?.tenant_id) === 1) {
            await loadLicenseSummaryIfPaid();
            await loadNoahFleet();
        }
    });

    // Worker row click for permissions
    document.getElementById('workers-table-body')?.addEventListener('click', (e) => {
        const row = e.target.closest('.worker-row');
        if (!row || e.target.tagName === 'BUTTON' || e.target.tagName === 'SELECT') return;
        document.querySelectorAll('.worker-row').forEach(r => r.classList.remove('bg-primary/20'));
        row.classList.add('bg-primary/20');
        loadResourcePermissions('worker-permissions-panel', 'worker', parseInt(row.dataset.workerId), row.dataset.workerName);
    });

    // Auto-refresh every 60 seconds
    if (refreshInterval) {
        clearInterval(refreshInterval);
    }
    refreshInterval = setInterval(async () => {
        // Self-terminate if the user navigated away (DOM is gone) or
        // we've already learned we don't have access. Otherwise this
        // ticks every 5s forever and spams the console.
        if (_accessDenied || !document.getElementById('workers-table-body')) {
            clearInterval(refreshInterval);
            refreshInterval = null;
            return;
        }
        const user = await loadWorkers();
        if (user?.is_platform_admin && Number(user?.tenant_id) === 1) {
            await loadLicenseSummaryIfPaid();
            await loadNoahFleet();
        }
    }, 5000);
}

/**
 * wizard-rebuild §9d — render the variant-test pending queue depth.
 *
 * Hits GET /node-definitions/wizard/variant-test-pending-status and
 * paints per-accelerator counts into the existing workers page.
 * Adds a small "Variant test queue" card so operators see at a
 * glance which accelerator hardware the wizard is waiting on.
 *
 * Graceful degrade: if the DOM container is absent (older page
 * HTML) or the API isn't reachable, the function returns silently.
 */
async function loadVariantTestPendingQueueDepth() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    let container = document.getElementById('variant-test-pending-queue');
    if (!container) {
        // Inject a card adjacent to the license summary if HTML
        // doesn't ship one yet. Keeps this self-contained.
        const summary = document.getElementById('license-summary-card')
            || document.querySelector('main, body');
        if (!summary) return;
        container = document.createElement('div');
        container.id = 'variant-test-pending-queue';
        container.className = 'mt-4 p-3 border rounded-md bg-card';
        container.innerHTML = `
            <div class="font-semibold text-sm mb-2">
                Variant test queue (awaiting hardware)
            </div>
            <div id="variant-test-pending-queue-body"
                 class="text-xs text-muted-foreground italic">
                Loading…
            </div>`;
        summary.parentNode?.insertBefore(container, summary.nextSibling);
    }
    try {
        const resp = await fetch(
            `${apiUrl}/woodsman/wizard/variant-test-pending-status`,
            {
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
            },
        );
        if (!resp.ok) return;
        const data = await resp.json();
        const body = document.getElementById('variant-test-pending-queue-body');
        if (!body) return;
        const depth = data.queue_depth_by_accelerator || {};
        const total = data.total_pending || 0;
        if (total === 0) {
            body.textContent = 'No pending tests — every variant is verified.';
            return;
        }
        const chips = Object.entries(depth)
            .map(([accel, count]) =>
                `<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200 text-xs">${accel}: ${count}</span>`,
            )
            .join(' ');
        body.innerHTML = `
            <div class="mb-1">${total} test(s) awaiting matching hardware.</div>
            <div class="flex flex-wrap gap-1">${chips}</div>`;
    } catch (e) {
        console.warn('variant-test pending queue load failed:', e);
    }
}



/**
 * noah-observability — fleet-wide NOAH idle-harvest status.
 *
 * Hits GET /platform/noah/fleet (platform-admin only) and paints a single
 * card summarising, across every NOAH-enabled worker: how many are enabled,
 * how many are available right now, total never-started tasks harvested, plus
 * a per-worker line (available/reason/last-transition/harvested). Replaces
 * having to open each worker's modal one at a time.
 *
 * Self-contained + graceful-degrade, mirroring loadVariantTestPendingQueueDepth:
 * injects its own card if the page HTML doesn't ship one, and returns silently
 * on 403 / unreachable API / missing DOM.
 */
// megaplan 25.3 Q.5b — platform-admin Storage section: per-worker disk bar
// (images/model cache/workdirs/reclaimable; "—" when null on an old worker),
// manager disk, top tenants. Follows the hide-loading/show-display-in-finally
// pattern (a stuck spinner was a recurring bug today).
function _fmtBytesW(bytes) {
    if (typeof bytes !== 'number' || !isFinite(bytes) || bytes < 0) return '—';
    if (bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
    const value = bytes / Math.pow(1024, i);
    const decimals = value >= 100 || i === 0 ? 0 : value >= 10 ? 1 : 2;
    return `${value.toFixed(decimals)} ${units[i]}`;
}
function _fmtGbW(gb) {
    if (typeof gb !== 'number' || !isFinite(gb)) return '—';
    return _fmtBytesW(gb * 1024 * 1024 * 1024);
}

// MAC.1.s13b + 25.5.s5 — the two read-only heartbeat blocks the hardware-profile
// GET carries beside the benchmark profile. Both are optional: an absent block
// means the worker never probed/reported it, so the line stays EMPTY and the
// manager is never told "the volume is fine" for a box that has not measured.
// The element itself always exists (the test asserts presence + emptiness), and
// every value goes through escapeHtml — these strings come from worker input.
function _sumCleanupBytesW(map) {
    if (!map || typeof map !== 'object') return '—';
    let total = 0;
    let any = false;
    for (const v of Object.values(map)) {
        if (typeof v === 'number' && isFinite(v) && v >= 0) {
            total += v;
            any = true;
        }
    }
    return any ? _fmtBytesW(total) : '—';
}

// Plain-text builders (no escaping here): the HTML path wraps them in
// escapeHtml(), the refresh path assigns them to textContent. Escaping once.
function _diskFloorInner(df) {
    if (!df || typeof df !== 'object') return '';
    const num = (v) => (typeof v === 'number' && isFinite(v)) ? v.toFixed(1) : '—';
    return `Disk: ${num(df.free_gb)} GB free (floor ${num(df.needed_gb)} GB) — ${String(df.state || 'unknown')}`;
}

// below_floor is the only state that reads as a warning; unknown (statfs failed
// / never measured) is deliberately not painted red — it is the gate's own
// fail-open spelling, not a proven problem.
function _diskFloorClass(df) {
    return (df && df.state === 'below_floor')
        ? 'text-xs text-red-600 font-medium'
        : 'text-xs text-gray-700';
}

function _lastCleanupInner(lc) {
    if (!lc || typeof lc !== 'object') return '';
    const when = lc.finished || lc.started;
    const fails = Array.isArray(lc.failures) ? lc.failures.length : 0;
    return `Last cleanup: ${when ? noahRelativeTime(when) : '—'} — ` +
           `reclaimed ${_sumCleanupBytesW(lc.bytes_freed)} ` +
           `(${fails === 1 ? '1 failure' : `${fails} failures`})`;
}

async function loadFleetStorage() {
    const loadingEl = document.getElementById('fleet-storage-loading');
    const displayEl = document.getElementById('fleet-storage-display');
    if (!loadingEl || !displayEl) return;

    try {
        if (!await isPaidEdition()) {
            // 2400LR-cdu: admin_storage.py denied on CE (manifest:1370); fail-open on unknown edition.
            loadingEl.closest('.mt-6')?.classList.add('hidden');
            return;
        }

        const data = await adminStorage.fleet();

        const mgr = data.manager || {};
        const rootFs = mgr.root_fs || null;
        const mgrEl = document.getElementById('fleet-storage-manager');
        if (mgrEl) {
            mgrEl.innerHTML = rootFs
                ? `Root filesystem: ${_fmtBytesW(rootFs.used)} used of ${_fmtBytesW(rootFs.total)} (${_fmtBytesW(rootFs.free)} free)` +
                  (mgr.db_bytes != null ? ` &middot; DB: ${_fmtBytesW(mgr.db_bytes)}` : '') +
                  (mgr.log_dir_bytes != null ? ` &middot; Logs: ${_fmtBytesW(mgr.log_dir_bytes)}` : '') +
                  (mgr.registry_bytes != null ? ` &middot; Registry: ${_fmtBytesW(mgr.registry_bytes)}` : '')
                : '<span class="text-gray-400 italic">Manager disk info unavailable.</span>';
        }

        const workers = data.workers || [];
        const wTbody = document.getElementById('fleet-storage-workers');
        if (wTbody) {
            wTbody.innerHTML = !workers.length
                ? `<tr><td class="text-gray-400 italic py-2">No workers reporting disk stats yet.</td></tr>`
                : workers.map(w => {
                    const pct = w.percent_used;
                    const pctColor = w.alert_level === 'crit_90' ? 'text-red-600'
                        : w.alert_level === 'warn_80' ? 'text-amber-600' : 'text-gray-600';
                    return `
                    <tr class="border-b last:border-0">
                        <td class="py-1 pr-3 font-medium text-gray-700">${escapeHtml(w.worker_name || w.hostname || '?')}</td>
                        <td class="py-1 pr-3 ${pctColor}">${pct != null ? pct.toFixed(1) + '%' : '—'}</td>
                        <td class="py-1 pr-3 text-gray-500">${_fmtBytesW(w.images_bytes)}</td>
                        <td class="py-1 pr-3 text-gray-500">${_fmtBytesW(w.model_cache_bytes)}</td>
                        <td class="py-1 pr-3 text-gray-500">${_fmtBytesW(w.workdirs_bytes)}</td>
                        <td class="py-1 pr-3 text-gray-500">${_fmtBytesW(w.reclaimable_bytes)}</td>
                    </tr>`;
                }).join('');
        }

        const tenants = data.top_tenants || [];
        const tTbody = document.getElementById('fleet-storage-top-tenants');
        if (tTbody) {
            tTbody.innerHTML = !tenants.length
                ? `<tr><td class="text-gray-400 italic py-2">No tenant storage data yet.</td></tr>`
                : tenants.map(t => `
                    <tr class="border-b last:border-0">
                        <td class="py-1 pr-3 text-gray-700">${escapeHtml(t.tenant_name || `Tenant ${t.tenant_id}`)}</td>
                        <td class="py-1 text-right text-gray-500 tabular-nums">${_fmtBytesW(t.storage_used_bytes)}</td>
                    </tr>`).join('');
        }
    } catch (error) {
        console.error('Error loading fleet storage:', error);
        const mgrEl = document.getElementById('fleet-storage-manager');
        if (mgrEl) mgrEl.innerHTML = '<span class="text-red-500">Unable to load fleet storage.</span>';
    } finally {
        loadingEl.classList.add('hidden');
        displayEl.classList.remove('hidden');
    }
}

async function loadNoahFleet() {
    // 2400LR-g3-ce-denied-404s: route ships in CE (api_allow manifest:94) but CORE/API/services/worker_management_service.py:534 require_paid("services.noah") -> 402 (noah.py denied manifest:1570); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        document.getElementById('noah-fleet-card')?.classList.add('hidden');
        document.getElementById('noah-fleet-body')?.classList.add('hidden');
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    let container = document.getElementById('noah-fleet-card');
    if (!container) {
        const anchor = document.getElementById('variant-test-pending-queue')
            || document.getElementById('license-summary-card')
            || document.querySelector('main, body');
        if (!anchor) return;
        container = document.createElement('div');
        container.id = 'noah-fleet-card';
        container.className = 'mt-4 p-3 border rounded-md bg-card';
        container.innerHTML = `
            <div class="font-semibold text-sm mb-2">NOAH idle-harvest fleet</div>
            <div id="noah-fleet-body" class="text-xs text-muted-foreground italic">Loading…</div>`;
        anchor.parentNode?.insertBefore(container, anchor.nextSibling);
    }
    try {
        const resp = await fetch(`${apiUrl}/platform/noah/fleet`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
        });
        if (resp.status === 403) { container.classList.add('hidden'); return; }
        if (!resp.ok) return;
        const data = await resp.json();
        const body = document.getElementById('noah-fleet-body');
        if (!body) return;
        const totals = data.totals || {};
        const workers = data.workers || [];
        if (!data.platform_enabled && !workers.length) {
            body.classList.remove('italic');
            body.textContent = 'NOAH is disabled platform-wide and no worker overrides it on.';
            return;
        }
        if (!workers.length) {
            body.classList.remove('italic');
            body.textContent = 'No NOAH-enabled workers.';
            return;
        }
        const rows = workers.map(w => {
            const avail = w.available
                ? `<span class="text-green-600 font-medium">available</span>`
                : `<span class="text-gray-400">idle-off</span>`;
            const reason = w.reason ? ` — ${escapeHtml(w.reason)}` : '';
            const since = w.last_transition
                ? new Date(w.last_transition).toLocaleString()
                : '—';
            return `<tr class="border-b border-border/40">
                <td class="py-1 pr-3 font-medium">${escapeHtml(w.worker_name || '?')}</td>
                <td class="py-1 pr-3">${avail}${reason}</td>
                <td class="py-1 pr-3 text-muted-foreground">${escapeHtml(since)}</td>
                <td class="py-1 text-right">${Number(w.harvested_total || 0)}</td>
            </tr>`;
        }).join('');
        body.classList.remove('italic');
        body.innerHTML = `
            <div class="mb-2 flex flex-wrap gap-2">
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200">Platform: ${data.platform_enabled ? 'enabled' : 'disabled'}</span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-100 text-blue-800 border border-blue-200">NOAH workers: ${Number(totals.noah_workers || 0)}</span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-green-100 text-green-800 border border-green-200">Available now: ${Number(totals.available_now || 0)}</span>
                <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-200">Harvested: ${Number(totals.harvested_total || 0)}</span>
            </div>
            <div class="overflow-x-auto"><table class="w-full text-xs">
                <thead><tr class="text-left text-muted-foreground">
                    <th class="py-1 pr-3 font-semibold">Worker</th>
                    <th class="py-1 pr-3 font-semibold">Availability</th>
                    <th class="py-1 pr-3 font-semibold">Last transition</th>
                    <th class="py-1 font-semibold text-right">Harvested</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table></div>`;
    } catch (e) {
        console.warn('NOAH fleet load failed:', e);
    }
}


// FIX-WORKERS-UNLIMITED-DISPLAY (cloud-h5, 11-10) BEGIN
// CE has no worker quantity caps. The API reports the UNLIMITED sentinel
// (1e15, CORE/API/utils/entitlements.py) as the limit; it must never be
// printed as a number. Any limit at or above the threshold, or missing, reads
// as "Unlimited". Finite limits (paid editions) keep the "used / limit" text.
const WORKER_LIMIT_UNLIMITED_THRESHOLD = 1e12;

function isUnlimitedWorkerLimit(n) {
    return n === null || n === undefined || n >= WORKER_LIMIT_UNLIMITED_THRESHOLD;
}

// Pure: returns the text/visibility for the capacity card.
function formatWorkerCapacity(licensed, limit, available) {
    if (!isUnlimitedWorkerLimit(limit)) {
        return { used: licensed, limit: limit, available: available, limitVisible: true };
    }
    return {
        used: `${licensed ?? 0} workers`,
        limit: 'Unlimited',
        available: 'Unlimited',
        limitVisible: false,
    };
}
// FIX-WORKERS-UNLIMITED-DISPLAY END

// Licence-card runtime guard (CE wording defence, lead 11-11): the card starts
// hidden and is only shown on a positive paid edition (saas|pro). ce, unknown,
// error and rejected all keep it hidden, and no /workers/license-summary call is
// made on CE. Same positive-paid shape as the OAuth panel gate in settings.js.
// The gate lives in this wrapper so loadLicenseSummary() itself stays verbatim.
async function loadLicenseSummaryIfPaid() {
    const summaryCard = document.getElementById('license-summary-card');
    summaryCard?.classList.add('hidden');
    if (!await isPaidEdition()) {
        // The Add Worker button lives inside the licence card; hiding the card
        // must not hide it (D74). Move the action row out, below the card.
        const actions = document.getElementById('workers-header-actions');
        if (summaryCard && actions && summaryCard.contains(actions)) {
            actions.classList.add('mb-6');
            summaryCard.insertAdjacentElement('afterend', actions);
        }
        return;
    }
    summaryCard?.classList.remove('hidden');
    await loadLicenseSummary();
}

async function loadLicenseSummary() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    try {
        const response = await fetch(`${apiUrl}/workers/license-summary`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (response.status === 403) {
            console.log('User not authorized to view license summary (not platform admin)');
            return;
        }

        if (!response.ok) {
            throw new Error(`Failed to load worker summary: ${response.status}`);
        }

        const data = await response.json();

        // Update summary counts
        const capacity = formatWorkerCapacity(
            data.workers.licensed,
            data.license.max_workers,
            data.workers.available_licenses
        );
        const elements = {
            'licensed-count': capacity.used,
            'max-workers-count': capacity.limit,
            'available-count': capacity.available,
            'unlicensed-count': data.workers.unlicensed,
            'offline-count': data.workers.offline
        };

        for (const [id, value] of Object.entries(elements)) {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        }

        // Unlimited: hide the " / limit" separator and the limit span.
        // The separator is the element just before #max-workers-count.
        const limitEl = document.getElementById('max-workers-count');
        if (limitEl) {
            const shown = capacity.limitVisible ? '' : 'none';
            limitEl.style.display = shown;
            if (limitEl.previousElementSibling) {
                limitEl.previousElementSibling.style.display = shown;
            }
        }

        // Show license details if expiring soon
        const detailsCard = document.getElementById('license-details-card');
        const modeText = document.getElementById('license-mode-text');
        const expiresText = document.getElementById('license-expires-text');

        if (detailsCard && modeText && expiresText && data.license.expires_at) {
            const expiresAt = new Date(data.license.expires_at);
            const now = new Date();
            const daysUntilExpiry = Math.ceil((expiresAt - now) / (1000 * 60 * 60 * 24));

            if (daysUntilExpiry <= 30) {
                detailsCard.classList.add('hidden');
                detailsCard.classList.remove('bg-primary/10', 'border-primary');
                detailsCard.classList.add('bg-yellow-50', 'border-yellow-600');
                modeText.classList.remove('text-primary');
                modeText.classList.add('text-yellow-900');
                expiresText.classList.remove('text-primary');
                expiresText.classList.add('text-yellow-700');

                modeText.textContent = `⚠️ Worker approval expires in ${daysUntilExpiry} days`;
                expiresText.textContent = `Expiration date: ${expiresAt.toLocaleDateString()}`;
            } else {
                detailsCard.classList.add('hidden');
            }
        } else if (detailsCard) {
            detailsCard.classList.add('hidden');
        }

    } catch (error) {
        console.error('Error loading license summary:', error);
        // Only show error toast if it's not a 403 (which we handled above, but just in case)
        if (!error.message.includes('403')) {
            showError('Failed to load worker summary');
        }
    }
}

async function loadWorkers() {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const tbody = document.getElementById('workers-table-body');

    // Get current user to determine tenant_id
    let currentUser = null;
    try {
        currentUser = await fetch(`${apiUrl}/me`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        }).then(res => res.json());
    } catch (e) {
        console.error('Error fetching user:', e);
    }

    const isPlatformAdmin = !!currentUser?.is_platform_admin;
    _isAdmin = isPlatformAdmin;

    // Platform-level cost cards render here, NOT beside the worker rows.
    // They were called after `tbody.innerHTML = ...`, which sits below two
    // early returns — an invalid tenant_id (line ~256) and an EMPTY worker
    // list (line ~397). So on a deployment whose worker table came back empty
    // the cards silently never rendered, which is exactly what happened.
    // Manager cost and overhead recovery are facts about the platform, not
    // about the rows, and must not depend on there being any.
    loadManagerCost();
    loadOverheadRecovery();
    loadInfrastructureCosts();
    const tenantId = Number(currentUser?.tenant_id);

    if (!Number.isFinite(tenantId) || tenantId < 1) {
        console.warn('Invalid or missing tenant_id from /me; cannot load tenant workers.', currentUser);
        return currentUser;
    }

    try {
        let workersDetail = [];
        let dbWorkers = [];

        // Only platform tenant (1) operators get the global capacity view. Other tenants (including
        // platform admins who "belong" to tenant 2+) must use routed workers or the tenant sees an empty list.
        if (isPlatformAdmin && tenantId === 1) {
            // Platform admin: prefer live capacity endpoint, fall back to DB-only list
            const capResponse = await fetch(`${apiUrl}/workers/capacity`, {
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            if (capResponse.ok) {
                const capacityData = await capResponse.json();
                workersDetail = capacityData.workers_detail || [];
            } else {
                console.warn(`Worker capacity unavailable (${capResponse.status}); falling back to database list only`);
            }

            const dbResponse = await fetch(`${apiUrl}/workers`, {
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            dbWorkers = dbResponse.ok ? await dbResponse.json() : [];
            if (!dbResponse.ok) {
                throw new Error(
                    `Failed to load workers from database: ${dbResponse.status}. ` +
                    `If this is 403, confirm the account has is_platform_admin and tenant_id 1 (platform operator account).`
                );
            }
        } else {
            // Tenant admin: fetch only their tenant's workers
            const response = await fetch(`${apiUrl}/workers/available-for-tenant/${tenantId}`, {
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) {
                throw new Error(`Failed to load workers: ${response.status}`);
            }

            const data = await response.json();
            // Non-admins only see workers assigned to their tenant (not shared pool)
            workersDetail = [
                ...(data.tenant_owned || []),
                ...(data.platform_assigned || []),
            ].map(w => ({
                worker_name: w.worker_name,
                name: w.worker_name,
                hostname: w.hostname,
                cpu_cores: w.hardware_specs?.cpu_cores || 0,
                memory_gb: w.hardware_specs?.ram_gb || 0,
                gpu_count: w.hardware_specs?.gpu_count || 0,
                vram_gb: w.hardware_specs?.vram_gb || 0,
                queues: w.queues || [],
                active_tasks: 0,
                reserved_tasks: 0,
                licensed: w.status === 'licensed',
                is_online: true,
                worker_id: w.id,
                status: w.status,
                licensed_at: w.licensed_at,
                licensed_by_username: w.licensed_by_username || null,
                first_seen_at: w.first_seen_at,
                notes: w.notes,
                // Passed through wholesale: getHardwareInfo() reads
                // hardware_specs.gpu_name and .hardware_tags off the worker,
                // and this mapping dropped the object entirely — so those
                // lookups were silently undefined on this path.
                hardware_specs: w.hardware_specs || {},
                arch: (w.hardware_specs || {}).arch || null,
                host_exec_status: w.host_exec_status
            }));
            dbWorkers = workersDetail; // Use the same data for dbWorkers
        }

        // Merge live data with database info
        const workers = workersDetail.map(liveWorker => {
            // Normalize field names
            const workerName = liveWorker.worker_name || liveWorker.name;
            const hostname = liveWorker.hostname || (workerName ? workerName.split('@')[1] : 'unknown');

            const dbWorker = dbWorkers.find(w => w.worker_name === workerName || w.name === workerName);
            // Merge carefully - platform workers have 'id', tenant workers have 'worker_id'
            const workerId = dbWorker?.id || dbWorker?.worker_id || liveWorker.worker_id;
            return {
                ...liveWorker,
                ...dbWorker,  // Merge database fields (id, status, licensed_at, etc.)
                id: workerId,
                worker_name: workerName,
                hostname: hostname,
                is_online: true  // If in capacity data, it's online
            };
        });

        // Add offline workers from database that aren't in live data
        dbWorkers.forEach(dbWorker => {
            if (!workersDetail.find(w => w.name === dbWorker.worker_name)) {
                workers.push({
                    ...dbWorker,
                    cpu_cores: dbWorker.hardware_specs?.pool_size || 0,
                    memory_gb: 0,
                    gpu_count: 0,
                    vram_gb: 0,
                    queues: dbWorker.queues || [],
                    active_tasks: 0,
                    reserved_tasks: 0,
                    licensed: dbWorker.status === 'licensed',
                    is_online: false
                });
            }
        });

        // Sort alphabetically by worker name.
        // Tim B3 item 4 — the embedded (manager-node) worker is the FIRST row;
        // every other pair keeps the alphabetical localeCompare below byte-identical.
        workers.sort((a, b) => {
            const _aEmbedded = a?.is_embedded === true ? 1 : 0;
            const _bEmbedded = b?.is_embedded === true ? 1 : 0;
            if (_aEmbedded !== _bEmbedded) return _bEmbedded - _aEmbedded;
            return (a.worker_name || a.name || '').localeCompare(b.worker_name || b.name || '');
        });

        if (workers.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="9" class="px-6 py-8 text-center text-gray-500">
                        <svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="mx-auto mb-2 text-gray-400">
                            <rect width="7" height="9" x="3" y="3" rx="1"/>
                            <rect width="7" height="5" x="14" y="3" rx="1"/>
                            <rect width="7" height="9" x="14" y="12" rx="1"/>
                            <rect width="7" height="5" x="3" y="16" rx="1"/>
                        </svg>
                        <p class="font-medium">No workers discovered yet</p>
                        <p class="text-sm mt-1">Workers will appear here automatically when they connect</p>
                    </td>
                </tr>
            `;
            return currentUser;
        }

        // Fetch latest release metadata (version, released_at, changelog_url)
        // once per loadWorkers() tick. Non-fatal if it fails — the button
        // states still work off the per-worker update_available flag.
        if (isPlatformAdmin) {
            try {
                const relResp = await fetch(`${apiUrl}/workers/latest-release`, {
                    headers: { 'Authorization': `Bearer ${token}` },
                });
                if (relResp.ok) {
                    _latestRelease = await relResp.json();
                }
            } catch (e) {
                console.warn('latest-release fetch failed (non-fatal)', e);
            }
        }

        // Compare current state to previous refresh: surface update-cycle
        // transitions so the operator actually sees when an Update completed.
        //   prev.update_requested=true → now.update_requested=false AND
        //   prev.agent_version != now.agent_version
        //       → show "Worker X updated to vY" toast.
        //   prev absent + now.update_requested=true → show "Updating…" toast
        //       (covers the case where the button click already showed one;
        //        we skip those by noting the click-originated toasts below).
        try {
            for (const w of workers) {
                const name = w.worker_name || w.name;
                if (!name) continue;
                const prev = _prevWorkerState.get(name);
                const nowRequested = !!w.update_requested;
                const nowVersion = w.agent_version || '';
                if (prev && prev.update_requested && !nowRequested) {
                    // Update cycle ended.
                    if (prev.agent_version && nowVersion && prev.agent_version !== nowVersion) {
                        showSuccess(`${name} updated from ${prev.agent_version} → ${nowVersion}`);
                    } else {
                        // Flag cleared but version didn't change — operator
                        // should know the cycle ended even if self-update failed
                        // (e.g., binary missing, pull failed).
                        showError(`${name}: update cycle ended but version is still ${nowVersion || 'unknown'}. Check worker logs.`);
                    }
                }
                _prevWorkerState.set(name, { update_requested: nowRequested, agent_version: nowVersion });
            }
        } catch (e) {
            console.warn('update-state diff failed (non-fatal)', e);
        }

        if (tbody) {
            // Cache the rows so the detail modal can show a worker's cost
            // basis. showWorkerDetail only receives an id and a name, and
            // re-fetching the whole list to read five fields would be silly.
            _workersByName.clear();
            for (const w of workers) {
                if (w && w.worker_name) _workersByName.set(w.worker_name, w);
            }
            tbody.innerHTML = workers.map(worker => renderWorkerRow(worker)).join('');
        }

        // Attach event listeners to action buttons
        workers.forEach(worker => {
            const assignBtn = document.getElementById(`assign-license-${worker.id}`);
            const revokeBtn = document.getElementById(`revoke-license-${worker.id}`);
            const typeSelect = document.getElementById(`type-select-${worker.id}`);
            const tenantSelect = document.getElementById(`tenant-select-${worker.id}`);

            if (assignBtn) {
                assignBtn.addEventListener('click', () => assignLicense(worker.worker_name, worker.id));
            }

            if (revokeBtn) {
                revokeBtn.addEventListener('click', () => revokeLicense(worker.worker_name));
            }

            // Handle type selector changes
            if (typeSelect) {
                // Populate tenant options
                populateTenantSelect(tenantSelect, workers);

                typeSelect.addEventListener('change', (e) => {
                    const selectedType = e.target.value;
                    if (selectedType === 'platform_assigned' || selectedType === 'tenant_owned') {
                        tenantSelect.classList.remove('hidden');
                    } else {
                        tenantSelect.classList.add('hidden');
                    }
                });
            }
        });

        return currentUser;

    } catch (error) {
        // 403 is the "user doesn't have permission" path — stop the
        // refresh interval and skip the console.error / DOM mutation.
        // Without this, non-admin tenants see this error every 5s.
        if (error?.status === 403) {
            _accessDenied = true;
            if (refreshInterval) {
                clearInterval(refreshInterval);
                refreshInterval = null;
            }
            return currentUser;
        }
        console.error('Error loading workers:', error);
        if (tbody) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="9" class="px-6 py-4 text-center text-red-600">
                        Error loading workers: ${escapeHtml(error.message)}
                    </td>
                </tr>
            `;
        }
        return currentUser;
    }
}

function renderWorkerRow(worker) {
    const statusBadge = getStatusBadge(worker.status, worker.is_online, worker.hardware_specs);
    const hardwareInfo = getHardwareInfo(worker);  // Pass whole worker object
    const queuesInfo = getQueuesInfo(worker.queues);
    const activityInfo = getActivityInfo(worker);  // New: show active/reserved tasks
    const ownerTypeInfo = getOwnerTypeInfo(worker);
    const licenseInfo = getLicenseInfo(worker);
    const actions = getActions(worker);

    return `
        <tr class="hover:bg-gray-50 cursor-pointer worker-row" data-worker-id="${worker.id}" data-worker-name="${(worker.worker_name || '').replace(/"/g, '&quot;')}">
            <td class="px-3 py-2">
                ${statusBadge}
                ${getHeartbeatWarningBadges(worker.hardware_specs)}
                ${getProfilingBadge(worker)}
            </td>
            <td class="px-3 py-2 max-w-0">
                <div class="text-sm font-medium text-gray-900 truncate" title="${escapeHtml(worker.worker_name)}">${escapeHtml(worker.worker_name)}</div>
                <div class="text-[11px] text-gray-500 truncate" title="${escapeHtml(worker.hostname)}">${escapeHtml(worker.hostname)}</div>
                ${getVersionBadge(worker)}
                ${getHostExecBadge(worker)}
            </td>
            <td class="px-3 py-2">
                <div class="text-[11px] text-gray-900 leading-tight">${hardwareInfo}</div>
                <div class="mt-1">${getArchInfo(worker)}</div>
                <div class="flex flex-wrap gap-0.5 mt-1">${queuesInfo}</div>
            </td>
            <td class="px-3 py-2">
                <div class="text-xs text-gray-900">${activityInfo}</div>
            </td>
            <td class="px-3 py-2">
                <div class="text-xs text-gray-900">${ownerTypeInfo}</div>
                <div class="text-[11px] text-gray-500 mt-0.5">${licenseInfo}</div>
            </td>
            <td class="px-3 py-2 text-right text-xs whitespace-nowrap">
                ${actions}
            </td>
        </tr>
    `;
}

// Compact inline version display so the operator sees the agent version
// directly on the worker row. During an update cycle, swap the version text
// for an animated spinner + "Updating..." so the operator knows something's
// happening without opening any detail panel.
function getVersionBadge(worker) {
    const cur = worker.agent_version || '';
    const latest = worker.latest_agent_version || '';
    // Tooltip enrichment: include release date + changelog link when we have them.
    const releasedAt = _latestRelease.released_at
        ? new Date(_latestRelease.released_at).toLocaleString()
        : '';
    const releaseContext = releasedAt ? ` (released ${releasedAt})` : '';
    const changelogHint = _latestRelease.changelog_url
        ? ` Click the Details button for the changelog link.`
        : '';

    if (worker.update_requested) {
        // SVG spinner, kept compact; tooltip shows the target version.
        return `<div class="mt-0.5 flex items-center gap-1 text-[11px] text-primary" title="Draining and self-updating to ${escapeHtml(latest || '(latest)')}${releaseContext}. Worker will auto-resume.">
            <svg class="animate-spin h-3 w-3 text-primary" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
            </svg>
            <span>Updating… ${cur ? escapeHtml(cur) + ' → ' : ''}${escapeHtml(latest || 'latest')}</span>
        </div>`;
    }
    if (!cur) {
        return `<div class="mt-0.5 text-[11px] text-gray-400" title="Worker has not reported an agent version yet.">v —</div>`;
    }
    if (latest && cur !== latest) {
        return `<div class="mt-0.5 text-[11px] text-amber-700" title="Newer version ${escapeHtml(latest)} available${releaseContext}.${changelogHint} Use the Update button to roll out.">
            <span class="font-mono">${escapeHtml(cur)}</span>
            <span class="text-amber-500">→ ${escapeHtml(latest)}</span>
        </div>`;
    }
    return `<div class="mt-0.5 text-[11px] text-gray-500 font-mono" title="Worker is on the latest version${releaseContext}.">${escapeHtml(cur)}</div>`;
}

// escapeHtml() round-trips through textContent -> innerHTML, which escapes
// `<`/`>`/`&` but NOT `"`/`'` (those are only special inside quoted attribute
// values, not in text node content, so the browser never re-encodes them).
// That is safe for text nodes but NOT for interpolating into a quoted HTML
// attribute (e.g. title="${escapeHtml(x)}") — an untrusted string containing
// a `"` can close the attribute early and inject new attributes/handlers.
// escapeAttr() additionally escapes quotes so it is safe for that context.
function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Truncate worker-reported strings before display so one long payload can't
// blow out the tooltip/label layout.
function _truncateForDisplay(text, maxLen) {
    if (text.length <= maxLen) return text;
    return text.slice(0, maxLen - 1) + '…';
}

// Host-exec availability badge. `host_exec_status` is `{available, code,
// message, fix?, mode?}` from the worker list response (993c). Absence of
// the field entirely (older API / worker predating this feature) means
// "unknown", not "unavailable" — so we render nothing, same "absence is
// not failure" convention as agent_version/arch above. message/fix are
// worker-reported (untrusted, per 993f) and are capped at 200 chars and
// escaped with escapeAttr() for the title attribute (escapeHtml() alone does
// not escape quotes and is not attribute-safe — see escapeAttr comment).
function getHostExecBadge(worker) {
    const hes = worker.host_exec_status;
    if (!hes || typeof hes !== 'object') return '';

    const MAX_LEN = 200;
    const message = hes.message ? _truncateForDisplay(String(hes.message), MAX_LEN) : '';
    const fix = hes.fix ? _truncateForDisplay(String(hes.fix), MAX_LEN) : '';
    const mode = hes.mode ? String(hes.mode) : '';

    if (hes.available) {
        const modeTxt = mode ? ` (${mode})` : '';
        const titleTxt = message || `Host-exec is available${modeTxt}.`;
        return `<div class="mt-0.5 text-[11px] text-emerald-700" title="${escapeAttr(titleTxt)}">Host-exec: available${escapeHtml(modeTxt)}</div>`;
    }

    const titleParts = [];
    if (message) titleParts.push(message);
    if (fix) titleParts.push(`Fix: ${fix}`);
    const titleTxt = titleParts.join(' ') || 'Host-exec is unavailable on this worker.';
    const labelTxt = message || 'Host-exec: unavailable';
    return `<div class="mt-0.5 text-[11px] text-amber-700" title="${escapeAttr(titleTxt)}">⚠ ${escapeHtml(labelTxt)}</div>`;
}

// Render a benchmark task's live progress step — shows exactly what's
// happening right now with elapsed time. Auto-refreshes every 5s via
// the existing loadWorkers() cycle.
function _renderTaskStep(tp, kind) {
    if (!tp) return '';
    const elapsed = tp.elapsed_s != null ? `${tp.elapsed_s.toFixed(0)}s` : '';

    const stepStyles = {
        'completed':      { icon: '✅', bg: 'bg-green-50 text-green-700', barColor: 'bg-green-500', anim: '' },
        'running':        { icon: '🔄', bg: 'bg-primary/10 text-primary',  barColor: 'bg-primary/80', anim: '' },
        'pulling':        { icon: '📦', bg: 'bg-primary/10 text-primary', barColor: 'bg-primary/80', anim: '' },
        'assigned':       { icon: '🎯', bg: 'bg-yellow-50 text-yellow-700', barColor: 'bg-yellow-500', anim: '' },
        'queued':         { icon: '⏳', bg: 'bg-gray-50 text-gray-600',  barColor: 'bg-gray-400', anim: '' },
        'not_dispatched': { icon: '⏸️', bg: 'bg-gray-50 text-gray-500',  barColor: 'bg-gray-300', anim: '' },
        'failed':         { icon: '❌', bg: 'bg-red-50 text-red-700',    barColor: 'bg-red-500', anim: '' },
    };
    const s = stepStyles[tp.step] || stepStyles['queued'];

    let barPct = 0;
    if (tp.step === 'completed' || tp.step === 'failed') barPct = 100;
    else if (tp.step === 'running') {
        const est = kind === 'system' ? 120 : 30;
        barPct = Math.min(95, Math.round(((tp.elapsed_s || 0) / est) * 100));
    }
    else if (tp.step === 'pulling') barPct = 15;
    else if (tp.step === 'assigned') barPct = 5;
    else if (tp.step === 'queued') barPct = 2;

    const substeps = {
        system: ['CPU single-core', 'CPU multi-core', 'Memory bandwidth', 'I/O sequential', 'I/O random', 'Object storage throughput', 'Docker pull', 'Container startup'],
        cuda: ['SGEMM compute', 'Memory bandwidth', 'Host-device copy'],
        rocm: ['SGEMM compute', 'Memory bandwidth', 'Host-device copy'],
        intel: ['oneMKL compute', 'Memory bandwidth', 'Host-device copy'],
        npu: ['Inference throughput', 'Memory bandwidth', 'Host-device copy'],
    };
    const steps = kind ? (substeps[kind] || substeps['system']) : null;

    let detail = '';
    if (tp.step === 'running' && steps) {
        const est = kind === 'system' ? 120 : 30;
        const pct = Math.min(95, Math.round(((tp.elapsed_s || 0) / est) * 100));
        const activeIdx = Math.min(steps.length - 1, Math.floor((tp.elapsed_s || 0) / (est / steps.length)));
        detail = `<div class="mt-1.5 space-y-0.5">`;
        for (let i = 0; i < steps.length; i++) {
            const done = i < activeIdx;
            const active = i === activeIdx;
            const cls = done ? 'text-green-600' : active ? 'text-primary font-semibold' : 'text-gray-400';
            const icon = done ? '✓' : active ? '▸' : '·';
            detail += `<div class="${cls} text-[10px] leading-tight flex items-center gap-1">
                <span class="w-2.5 text-center">${icon}</span>${steps[i]}${active ? '<span class="ml-1 opacity-60 animate-pulse">...</span>' : ''}
            </div>`;
        }
        detail += `</div>`;
    } else if (tp.step === 'not_dispatched') {
        detail = `<div class="mt-0.5 text-[10px] text-gray-400 italic">Queued — will start after the current benchmark finishes</div>`;
    }

    return `<div class="mt-1 ${s.bg} ${s.anim} rounded px-2 py-1.5 text-[11px]">
        <div class="flex items-center gap-1.5">
            <span>${s.icon}</span>
            <span class="flex-1 font-medium">${escapeHtml(tp.label)}</span>
            ${elapsed ? `<span class="font-mono text-[10px] opacity-70">${elapsed}</span>` : ''}
        </div>
        <div class="mt-1 h-1.5 bg-gray-200 rounded-full overflow-hidden">
            <div class="h-full ${s.barColor} rounded-full transition-all duration-700 ease-out" style="width: ${barPct}%"></div>
        </div>
        ${detail}
    </div>`;
}

// ----------------------------------------------------------------------
// In-place renderers for the worker detail modal.
//
// Each helper returns the HTML body for one updatable section so that
// auto-refresh and benchmark-trigger actions can swap `.innerHTML` on a
// stable hook instead of rebuilding the entire modal. Rebuilding the
// whole modal is visually jarring (scroll jumps, flashing, collapses
// the Accelerators panel), so the goal is: never touch anything the
// user can see moving.
// ----------------------------------------------------------------------

function _fmtScore(v) {
    const n = Number(v) || 0;
    const [whole, dec] = n.toFixed(2).split('.');
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + dec;
}

function _scoreBar(label, val) {
    const v = val || 0;
    const pct = Math.min(100, Math.max(0, v / 50 * 100));
    return `<div class="flex items-center gap-2 text-xs">
        <span class="w-24 text-gray-600">${label}</span>
        <div class="flex-1 bg-gray-200 rounded-full h-2"><div class="bg-primary/80 h-2 rounded-full" style="width:${pct}%"></div></div>
        <span class="w-20 text-right font-mono">${_fmtScore(v)}</span>
    </div>`;
}

// Top-level "Benchmark Scores" panel. Returns an empty string (not an
// empty styled block) when the worker hasn't completed a benchmark yet,
// so the outer hook div collapses to zero height instead of showing an
// orphan separator line.
function _renderTopScores(p) {
    if (!(p && p.benchmark_status === 'completed' && p.scores)) return '';
    let h = '<div class="border-t pt-3 space-y-1.5">';
    h += '<h4 class="text-xs font-semibold text-gray-700 mb-2">Benchmark Scores</h4>';
    h += _scoreBar('GPU Compute', p.scores.gpu_compute);
    h += _scoreBar('CPU Multi',   p.scores.cpu_multi);
    h += _scoreBar('CPU Single',  p.scores.cpu_single);
    h += _scoreBar('I/O Seq',     p.scores.io_seq);
    h += _scoreBar('I/O Random',  p.scores.io_random);
    h += _scoreBar('Memory BW',   p.scores.memory_bw);
    h += '</div>';
    return h;
}

// Accelerator card — HTML that goes *inside* the flex-1 column.
function _renderAccelCardInner(a) {
    let h = `<div class="font-medium">${escapeHtml(a.name || 'Unknown device')}</div>`;
    if (a.is_integrated) {
        const sharedRam = a.shared_ram_usage_gb ? `${Number(a.shared_ram_usage_gb).toFixed(2)} GB system RAM` : 'shared system RAM';
        h += `<div class="text-amber-600 font-medium">Integrated (${sharedRam})</div>`;
    } else if (a.vram_gb) {
        h += `<div class="text-gray-500">${Number(a.vram_gb).toFixed(2)} GB VRAM</div>`;
    }
    if (a.task_progress && a.benchmark_status !== 'completed' && a.benchmark_status !== 'failed') {
        h += `<div data-bench="${a.kind}-${a.index}">${_renderTaskStep(a.task_progress, a.kind)}</div>`;
    }
    if (a.benchmark_status === 'completed') {
        const hasScores = a.compute_score || a.memory_bw_score || a.copy_bw_score;
        if (hasScores) {
            h += `<div class="flex gap-3 mt-1 text-gray-600">`;
            if (a.compute_score)   h += `<span>Compute: <b>${_fmtScore(a.compute_score)}</b></span>`;
            if (a.memory_bw_score) h += `<span>Mem BW: <b>${_fmtScore(a.memory_bw_score)}</b></span>`;
            if (a.copy_bw_score)   h += `<span>Copy: <b>${_fmtScore(a.copy_bw_score)}</b></span>`;
            h += '</div>';
            if (a.cpu_bw_contention != null && a.cpu_bw_contention < 0.9) {
                const degradation = ((1 - a.cpu_bw_contention) * 100).toFixed(0);
                h += `<div class="text-amber-600 text-[10px] mt-0.5">CPU bandwidth contention: ${degradation}% degradation when active</div>`;
            }
        } else {
            // Post-fix: the API flips all-zero-with-errors to status=failed and
            // surfaces the real error on error_message. This branch only fires
            // when the container returned zeros *without* recording any
            // exception — genuinely ambiguous.
            h += `<div class="mt-1 text-amber-600 text-[10px]">Completed with zero scores — no benchmark output recorded</div>`;
        }
        h += `<div class="mt-0.5"><span class="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700">Completed</span></div>`;
    } else if (a.benchmark_status === 'failed') {
        h += `<div class="mt-1 text-red-500 text-[10px]">${a.error_message ? escapeHtml(a.error_message) : 'Benchmark failed'}</div>`;
        h += `<div class="mt-0.5"><span class="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700">Failed</span></div>`;
    }
    return h;
}

// SYSTEM card — HTML that goes inside the flex-1 column.
// When the benchmark has completed, renders per-field scores inline
// the same way accelerator cards render compute / mem_bw / copy_bw,
// so the user sees real numbers instead of just "Completed".
// Nullable fields (MinIO, docker_pull_seconds, container_startup_ms)
// render as "n/a" so "skipped" is visibly distinct from a measured 0.
function _renderSystemCardInner(sysBench) {
    let h = `<div class="font-medium">CPU / RAM / I/O / Object storage / Docker</div>`;
    const stepHtml = sysBench
        ? _renderTaskStep(sysBench, 'system')
        : `<div class="mt-1 text-[10px] text-gray-400 italic">Not run yet</div>`;
    h += `<div data-bench="system">${stepHtml}</div>`;

    const s = sysBench && sysBench.scores;
    if (sysBench && sysBench.step === 'completed' && s) {
        const _fmtOrNa = (v) => (v == null ? '<span class="text-gray-400">n/a</span>' : _fmtScore(v));
        const _fmtSec  = (v) => (v == null ? '<span class="text-gray-400">n/a</span>' : `${Number(v).toFixed(2)}s`);
        const _fmtMs   = (v) => (v == null ? '<span class="text-gray-400">n/a</span>' : `${Number(v).toFixed(0)}ms`);

        h += `<div class="flex flex-wrap gap-x-3 gap-y-0.5 mt-1 text-gray-600 text-[10px]">`;
        if (s.cpu_multi_score)  h += `<span>CPU multi: <b>${_fmtScore(s.cpu_multi_score)}</b></span>`;
        if (s.cpu_single_score) h += `<span>CPU single: <b>${_fmtScore(s.cpu_single_score)}</b></span>`;
        if (s.memory_bw_score)  h += `<span>Mem BW: <b>${_fmtScore(s.memory_bw_score)}</b></span>`;
        h += `</div>`;

        h += `<div class="flex flex-wrap gap-x-3 gap-y-0.5 mt-0.5 text-gray-600 text-[10px]">`;
        if (s.io_seq_score)      h += `<span>I/O seq: <b>${_fmtScore(s.io_seq_score)}</b></span>`;
        if (s.io_random_score)   h += `<span>I/O rand: <b>${_fmtScore(s.io_random_score)}</b></span>`;
        if (s.disk_seq_read_score) h += `<span>Disk read: <b>${_fmtScore(s.disk_seq_read_score)}</b></span>`;
        h += `</div>`;

        h += `<div class="flex flex-wrap gap-x-3 gap-y-0.5 mt-0.5 text-gray-600 text-[10px]">`;
        h += `<span>S3 DL: <b>${_fmtOrNa(s.minio_download_mbps)}</b></span>`;
        h += `<span>S3 UL: <b>${_fmtOrNa(s.minio_upload_mbps)}</b></span>`;
        h += `<span>Docker pull: <b>${_fmtSec(s.docker_pull_seconds)}</b></span>`;
        h += `<span>Container start: <b>${_fmtMs(s.container_startup_ms)}</b></span>`;
        h += `</div>`;
    }
    return h;
}

// One shared refresh pass used by both the auto-refresh interval and the
// post-benchmark-trigger nudge. Only writes into stable hooks; never
// rebuilds the entire modal. Returns the profile status so callers can
// decide whether to keep polling.
async function _refreshWorkerDetailSections(workerId, workerName, apiUrl, token) {
    const modal = document.getElementById('worker-detail-modal');
    if (!modal || modal.classList.contains('hidden')) return null;

    // 1. Hardware profile (top "Benchmark Scores" + "Benchmark:" status line).
    try {
        const hpResp = await fetch(`${apiUrl}/workers/${workerId}/hardware-profile`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (hpResp.ok) {
            const hpData = await hpResp.json();
            const p = hpData.hardware_profile || {};
            const scoresEl = document.getElementById('worker-detail-scores');
            if (scoresEl) scoresEl.innerHTML = _renderTopScores(p);
            const statusEl = document.getElementById('worker-detail-bench-status');
            if (statusEl) {
                statusEl.textContent = `${p.benchmark_status || '—'}${p.benchmarked_at ? ' (' + new Date(p.benchmarked_at).toLocaleDateString() + ')' : ''}`;
            }
            // MAC.1.s13b / 25.5.s5 — same stable-hook rule as the scores block:
            // the heartbeat can write a new gate reading / cleanup pass between
            // refreshes, so repaint both lines in place (empty when absent).
            const floorEl = document.getElementById('worker-detail-disk-floor');
            if (floorEl) {
                const inner = document.createElement('div');
                inner.className = _diskFloorClass(hpData.disk_floor);
                inner.textContent = _diskFloorInner(hpData.disk_floor);
                floorEl.replaceChildren(inner);
            }
            const cleanEl = document.getElementById('worker-detail-last-cleanup');
            if (cleanEl) {
                const inner = document.createElement('div');
                inner.className = 'text-xs text-gray-700';
                inner.textContent = _lastCleanupInner(hpData.last_cleanup);
                cleanEl.replaceChildren(inner);
            }
        }
    } catch (_) { /* silent */ }

    // 2. Accelerators panel — SYSTEM card + per-device cards + progress bar.
    try {
        const accelResp = await fetch(`${apiUrl}/accelerators/worker/${encodeURIComponent(workerName)}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!accelResp.ok) return null;
        const rd = await accelResp.json();

        const sysBody = document.querySelector('[data-system-body]');
        if (sysBody) sysBody.innerHTML = _renderSystemCardInner(rd.system_benchmark);

        for (const a of (rd.accelerators || [])) {
            const el = document.querySelector(`[data-accel-body="${a.kind}-${a.index}"]`);
            if (el) el.innerHTML = _renderAccelCardInner(a);
        }

        const accelDiv = document.getElementById('worker-detail-accelerators');
        if (accelDiv && rd.progress) {
            const headerEl = accelDiv.querySelector('[data-bench-header]');
            if (headerEl) headerEl.textContent = `Profiling ${rd.progress.completed}/${rd.progress.total}`;
            const barEl = accelDiv.querySelector('[data-bench-bar] > div');
            if (barEl) {
                const pct = Math.round(((rd.progress.completed || 0) / (rd.progress.total || 1)) * 100);
                barEl.style.width = `${pct}%`;
            }
        }

        return rd.profiling_status || null;
    } catch (_) {
        return null;
    }
}

function getStatusBadge(status, isOnline, hardwareSpecs) {
    // WS4.Q21 — "green but silent" worker: it keeps heartbeating (isOnline
    // stays true) while its task fetch/claim loop is wedged, so a plain
    // "Active" badge would hide exactly the failure this exists to catch.
    // The API's worker-stall watchdog (services/scheduler.py) sets
    // hardware_specs.fetch_loop_stalled when it sees a fresh heartbeat with
    // a stale fetch loop AND eligible/stuck work — surface that here instead
    // of the misleading green badge.
    if (status === 'licensed' && isOnline && hardwareSpecs && hardwareSpecs.fetch_loop_stalled) {
        const ageS = hardwareSpecs.fetch_loop_age_s;
        const ageTxt = typeof ageS === 'number' ? ` (${Math.round(ageS / 60)}m)` : '';
        return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-red-100 text-red-900" title="Heartbeating but not fetching tasks${ageTxt}">
            🛑 Stalled${ageTxt}
        </span>`;
    }
    if (status === 'licensed') {
        const onlineStatus = isOnline ? 'Active' : 'Offline';
        const badgeColor = isOnline ? 'bg-green-100 text-green-800 badge-active' : 'bg-gray-100 text-gray-800';
        return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full ${badgeColor}">
            ✅ ${onlineStatus}
        </span>`;
    } else if (status === 'unlicensed') {
        return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-yellow-100 text-yellow-800">
            ⏸️ Pending
        </span>`;
    } else if (status === 'offline') {
        return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">
            ⚠️ Offline
        </span>`;
    }
    return `<span class="px-2 inline-flex text-xs leading-5 font-semibold rounded-full bg-gray-100 text-gray-800">
        ${status}
    </span>`;
}

// 1142 — heartbeat warnings (API 1141): the worker heartbeat now stores
// hardware_specs.worker_warnings (list of strings; absent = keep previous,
// [] = cleared) plus hardware_specs.gpu_telemetry_unhealthy (bool) and
// gpu_telemetry_unhealthy_reason. A worker can be heartbeating-clean (green
// "Active") while its GPU telemetry is wedged, so surface both here. Worker
// strings are untrusted: capped via _truncateForDisplay and escaped with
// escapeAttr() — these go into a quoted title attribute, and escapeHtml()
// alone is not quote-safe (see the escapeAttr comment above).
function getHeartbeatWarningBadges(hardwareSpecs) {
    if (!hardwareSpecs || typeof hardwareSpecs !== 'object') return '';
    const MAX_LEN = 200;
    const out = [];

    const warnings = hardwareSpecs.worker_warnings;
    if (Array.isArray(warnings)) {
        const lines = warnings
            .map(w => _truncateForDisplay(String(w), MAX_LEN))
            .filter(s => s);
        if (lines.length > 0) {
            const title = escapeAttr(lines.join(' · '));
            out.push(`<div class="mt-0.5">
                <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-800 border border-amber-600" title="${title}">⚠ ${lines.length} heartbeat warning${lines.length === 1 ? '' : 's'}</span>
            </div>`);
        }
    }

    if (hardwareSpecs.gpu_telemetry_unhealthy) {
        const reason = hardwareSpecs.gpu_telemetry_unhealthy_reason
            ? _truncateForDisplay(String(hardwareSpecs.gpu_telemetry_unhealthy_reason), MAX_LEN)
            : 'GPU telemetry reports no usable VRAM';
        out.push(`<div class="mt-0.5">
            <span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-red-100 text-red-900 border border-red-600" title="${escapeAttr('GPU out of service — ' + reason)}">🚫 GPU out of service</span>
        </div>`);
    }

    return out.join('\n');
}

// Show a profiling badge when the worker is still being benchmarked.
// Only visible for workers whose benchmark_status is 'running' or 'pending'.
function getProfilingBadge(worker) {
    const bs = worker.benchmark_status;
    if (bs === 'running') {
        return `<div class="mt-1">
            <span class="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] font-semibold bg-primary/20 text-primary border border-primary">
                <svg class="animate-spin h-3 w-3" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                </svg>
                PROFILING — cannot accept tasks
            </span>
        </div>`;
    }
    if (bs === 'pending') {
        return `<div class="mt-1">
            <span class="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] font-semibold bg-red-100 text-red-900 border border-red-300">
                ⛔ NOT PROFILED — worker blocked from all tasks until benchmark runs
            </span>
        </div>`;
    }
    if (bs === 'failed') {
        return `<div class="mt-1">
            <span class="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] font-semibold bg-red-100 text-red-900 border border-red-300">
                ❌ PROFILE FAILED — worker cannot accept tasks. Click Details → Run Benchmark to retry.
            </span>
        </div>`;
    }
    return '';
}

// CPU architecture, and what it means for what this worker can be given.
//
// Shown because the scheduler now REFUSES work a worker's architecture cannot
// execute (an amd64 image on arm64 dies with `exec format error`). That is the
// right behaviour, and it has a visible consequence: an arm64 worker enrolls,
// reports healthy, and does nothing. Without a reason on screen the only
// symptom is a machine that looks fine and never picks anything up — which is
// exactly the failure this gating was added to prevent, moved one step later.
function getArchInfo(worker) {
    const raw = String(worker.arch || worker.hardware_specs?.arch || '')
        .trim().toLowerCase();
    if (!raw) {
        // Older worker binaries report nothing here. Treated as amd64 by the
        // scheduler, so say that rather than showing an alarming blank.
        return `<span class="px-1.5 py-0.5 rounded text-[10px] bg-gray-100 text-gray-500"
                      title="This worker has not reported its CPU architecture. It is treated as amd64 for scheduling.">arch unknown</span>`;
    }
    const alias = { x86_64: 'amd64', x64: 'amd64', aarch64: 'arm64', armv8: 'arm64' };
    const arch = alias[raw] || raw;
    if (arch === 'amd64') {
        return `<span class="px-1.5 py-0.5 rounded text-[10px] bg-gray-100 text-gray-700"
                      title="Runs every image in the library.">amd64</span>`;
    }
    return `<span class="px-1.5 py-0.5 rounded text-[10px] bg-amber-100 text-amber-800"
                  title="This worker only receives tasks whose image is built for ${escapeHtml(arch)}. Images are amd64-only unless they declare otherwise, so it will stay idle until multi-arch images are published. This is deliberate — the alternative is claiming work it cannot run and failing every task.">${escapeHtml(arch)} — limited work available</span>`;
}

function getHardwareInfo(worker) {
    const parts = [];

    if (worker.cpu_cores) {
        parts.push(`<span class="font-medium">${worker.cpu_cores}</span> cores`);
    }
    if (worker.memory_gb) {
        parts.push(`<span class="font-medium">${worker.memory_gb}</span> GB RAM`);
    }
    // GPU info: prefer named GPU, then count from hardware_tags (each non-cpu
    // tag = one accelerator type), then gpu_count field. Shows each accelerator
    // type explicitly (e.g. "ROCm + Intel + NPU") instead of a generic "1x GPU".
    const gpuName = worker.hardware_specs?.gpu_name;
    const hwTags = (worker.hardware_specs?.hardware_tags || []).filter(t => {
        const s = String(t).toLowerCase();
        return s !== 'cpu' && s !== 'default';
    });
    if (gpuName && String(gpuName).toLowerCase() !== 'none') {
        parts.push(`<span class="font-medium">${escapeHtml(gpuName)}</span>`);
    } else if (hwTags.length > 0) {
        const pretty = hwTags.map(t => {
            const s = String(t).toLowerCase();
            if (s === 'cuda') return 'CUDA';
            if (s === 'rocm') return 'ROCm';
            if (s === 'intel') return 'Intel';
            if (s === 'npu') return 'NPU';
            return s.toUpperCase();
        });
        parts.push(`<span class="font-medium">${pretty.length}x Accel</span> (${pretty.join(', ')})`);
    } else if (worker.gpu_count && worker.gpu_count > 0) {
        parts.push(`<span class="font-medium">${worker.gpu_count}</span>x GPU`);
    }
    if (worker.vram_gb && worker.vram_gb > 0) {
        parts.push(`<span class="font-medium">${Number(worker.vram_gb).toFixed(2)}</span> GB VRAM`);
    }

    const cloud = worker.hardware_specs?.cloud_provider;
    const instance = worker.hardware_specs?.instance_type;
    if (cloud && instance) {
        parts.push(`<span class="text-xs text-primary">${cloud.toUpperCase()} ${instance}</span>`);
    }

    // Gate 7 — what this machine costs its operator, per hour. Shown beside the
    // hardware because that is what the number describes, and because an
    // unpriced worker has to be visibly unpriced: a run costed at zero reads as
    // free rather than as unmeasured, and that error only ever goes one way.
    // Wrapped: this runs inside renderWorkerRow, which runs inside
    // workers.map(...). An exception here does not just lose the chip — it
    // takes the whole table with it, and every card rendered after it.
    try {
    if (_isAdmin) {
        const cph = worker.cost_per_hour_cents;
        const wname = worker.worker_name || '';
        if (cph === null || cph === undefined) {
            parts.push(`<a href="#" onclick="showWorkerDetail(0, ${jsAttrStr(wname)});return false;"
                class="text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700"
                title="Runs on this worker cannot be priced until it has a cost">Not priced — set cost</a>`);
        } else {
            // Both numbers, always. The billed rate alone hides whether the
            // machine is profitable; the cost alone hides what the customer
            // pays. The gap between them IS the margin and should be readable
            // at a glance.
            const billed = worker.billed_per_hour_cents;
            const pct = worker.margin_percent;
            const marginBit = (billed !== null && billed !== undefined)
                ? ` &rarr; $${(billed / 100).toFixed(2)}/hr billed${pct === null || pct === undefined ? '' : ` (+${pct}%)`}`
                : '';
            parts.push(`<a href="#" onclick="showWorkerDetail(0, ${jsAttrStr(wname)});return false;"
                class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-700"
                title="Cost to run, then what it bills at. Click to change.">$${(cph / 100).toFixed(2)}/hr cost${marginBit}</a>`);
        }
    }
    } catch (e) {
        console.warn('worker cost chip failed (non-fatal)', e);
    }

    const benchStatus = worker.benchmark_status;
    if (benchStatus === 'completed') {
        const benchDate = worker.benchmarked_at ? new Date(worker.benchmarked_at).toLocaleDateString() : '';
        parts.push(`<span class="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700">Profiled${benchDate ? ' ' + benchDate : ''}</span>`);
    } else if (benchStatus === 'running') {
        parts.push('<span class="text-[10px] px-1.5 py-0.5 rounded bg-yellow-100 text-yellow-700">Profiling...</span>');
    } else if (benchStatus === 'skipped') {
        parts.push('<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/20 text-primary">Pre-seeded profile</span>');
    } else if (benchStatus === 'failed') {
        parts.push('<span class="text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700">Profile failed</span>');
    } else if (benchStatus === 'pending') {
        parts.push('<span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">Not profiled</span>');
    } else {
        parts.push('<span class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-400">No profile</span>');
    }

    return parts.length > 0 ? parts.join('<br>') : '<span class="text-gray-400">Unknown</span>';
}

function getQueuesInfo(queues) {
    if (!queues || queues.length === 0) {
        return '<span class="text-gray-400">None</span>';
    }

    // Color code queues
    const queueColors = {
        'default': 'bg-gray-100 text-gray-800',
        'cpu-small': 'bg-primary/20 text-primary',
        'cpu-medium': 'bg-primary/20 text-primary',
        'cpu-large': 'bg-purple-100 text-purple-800',
        'gpu-small': 'bg-green-100 text-green-800',
        'gpu-medium': 'bg-emerald-100 text-emerald-800',
        'gpu-large': 'bg-teal-100 text-teal-800'
    };

    return queues.map(q => {
        const colorClass = queueColors[q] || 'bg-gray-100 text-gray-800';
        return `<span class="inline-block ${colorClass} text-xs px-2 py-1 rounded mr-1 mb-1">${escapeHtml(q)}</span>`;
    }).join('');
}

function getActivityInfo(worker) {
    if (!worker.licensed || !worker.is_online) {
        return '<span class="text-gray-400">—</span>';
    }

    const active = worker.active_tasks || 0;
    const reserved = worker.reserved_tasks || 0;

    if (active === 0 && reserved === 0) {
        return '<span class="text-gray-500">Idle</span>';
    }

    const parts = [];
    if (active > 0) {
        parts.push(`<span class="text-primary font-medium">${active}</span> running`);
    }
    if (reserved > 0) {
        parts.push(`<span class="text-yellow-700 font-medium">${reserved}</span> queued`);
    }

    return parts.join('<br>');
}

function getLicenseInfo(worker) {
    if (worker.status !== 'licensed') {
        return '<span class="text-gray-400">—</span>';
    }

    const licensedAt = formatDate(worker.licensed_at);
    const licensedBy = worker.licensed_by_username ? escapeHtml(worker.licensed_by_username) : 'Unknown';

    return `
        <div class="text-xs">
            <div>${licensedAt}</div>
            <div class="text-gray-500">by ${licensedBy}</div>
        </div>
    `;
}

async function populateTenantSelect(tenantSelect, workers) {
    if (!tenantSelect) return;

    // Fetch tenants from API
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    try {
        const response = await fetch(`${apiUrl}/tenants`, {
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            }
        });

        if (response.ok) {
            const tenants = await response.json();
            // Clear existing options except the first one
            tenantSelect.innerHTML = '<option value="">Select tenant...</option>';
            tenants.forEach(tenant => {
                const option = document.createElement('option');
                option.value = tenant.id;
                option.textContent = tenant.name || `Tenant ${tenant.id}`;
                tenantSelect.appendChild(option);
            });
        }
    } catch (error) {
        console.error('Error loading tenants:', error);
    }
}

function getOwnerTypeInfo(worker) {
    const ownerType = worker?.owner_type;
    // Tim B3 item 4 — the embedded (manager-node) worker runs all of MOD's
    // internal work and is shown as the internal system, NOT the plain
    // `Platform Pool` badge. `is_embedded` is a platform-level response key
    // from GET /workers (routers/worker_management.py `_is_embedded_worker`);
    // rows from paths that never carry it (e.g. /workers/available-for-tenant)
    // leave it undefined -> falsy -> plain badge (fail-closed).
    if (worker?.is_embedded === true) {
        return `
            <div>
                <span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium bg-indigo-100 text-indigo-800">
                    Internal system
                </span>
                <div class="text-[11px] text-gray-500 mt-1">Runs MOD's internal work on the manager node</div>
                <div class="text-[11px] text-gray-500 mt-1">Routed to: <span class="font-medium">Any tenant</span></div>
            </div>
        `;
    }
    if (!ownerType) return '<span class="text-gray-400">Not assigned</span>';

    const typeInfo = {
        'platform_only': { label: 'Platform Only', color: 'bg-purple-100 text-purple-800', desc: 'Platform exclusive' },
        'platform_pool': { label: 'Platform Pool', color: 'bg-primary/20 text-primary', desc: 'Shared pool' },
        'platform_assigned': { label: 'Platform Assigned', color: 'bg-primary/20 text-primary', desc: 'Tenant assigned' },
        'tenant_owned': { label: 'Tenant Owned', color: 'bg-green-100 text-green-800', desc: 'BYOH' }
    };

    const info = typeInfo[ownerType] || { label: ownerType, color: 'bg-gray-100 text-gray-800', desc: '' };

    // Render assignment targets (tenant IDs) so platform admins can see who this worker is routed to.
    let targetsHtml = '';
    if (ownerType === 'platform_assigned') {
        const ids = worker?.assigned_tenant_ids || [];
        targetsHtml = ids.length
            ? `<div class="text-[11px] text-gray-500 mt-1">Assigned to: <span class="font-medium">${escapeHtml(ids.join(', '))}</span></div>`
            : `<div class="text-[11px] text-gray-500 mt-1">Assigned to: <span class="font-medium">None</span></div>`;
    } else if (ownerType === 'tenant_owned') {
        const id = worker?.owner_tenant_id;
        targetsHtml = id
            ? `<div class="text-[11px] text-gray-500 mt-1">Owned by tenant: <span class="font-medium">${escapeHtml(String(id))}</span></div>`
            : `<div class="text-[11px] text-gray-500 mt-1">Owned by tenant: <span class="font-medium">None</span></div>`;
    } else if (ownerType === 'platform_pool') {
        targetsHtml = `<div class="text-[11px] text-gray-500 mt-1">Routed to: <span class="font-medium">Any tenant</span></div>`;
    } else if (ownerType === 'platform_only') {
        targetsHtml = `<div class="text-[11px] text-gray-500 mt-1">Routed to: <span class="font-medium">Platform only</span></div>`;
    }

    return `
        <div>
            <span class="inline-flex items-center px-2 py-1 rounded text-xs font-medium ${info.color}">
                ${info.label}
            </span>
            ${targetsHtml}
        </div>
    `;
}

// Auto-refresh interval for the detail modal while profiling is active.
// Cleared when the modal is closed or profiling completes.
let _detailRefreshInterval = null;

// megaplan 14.8 — per-card VRAM bar: reserved (from the live gpu_assignment
// on each running task) vs. "typical peak" (a LEARNED figure from past
// COMPLETED runs of that node_kind — never live telemetry, the worker only
// reports peak_vram_mb at task completion). A thin marker shows live_used_mb
// when the worker has exactly one GPU (only then can the heartbeat's
// worker-aggregate VRAM figure be attributed to a single card). Follows the
// same escapeHtml + defensive-null conventions as the rest of this modal.
function _fmtMbW(mb) {
    if (typeof mb !== 'number' || !isFinite(mb)) return '—';
    return _fmtBytesW(mb * 1024 * 1024);
}

const _GPU_SEGMENT_COLORS = ['bg-primary/70', 'bg-teal-500/70', 'bg-purple-500/70', 'bg-amber-500/70', 'bg-sky-500/70'];

function _renderGpuUsageSection(data) {
    const cards = data?.cards || [];
    if (!cards.length) {
        return '<div class="border-t pt-3"><h4 class="text-xs font-semibold text-gray-800 mb-2">GPU VRAM usage</h4>' +
            '<p class="text-xs text-gray-400">No schedulable GPU cards reported for this worker.</p></div>';
    }

    let html = '<div class="border-t pt-3"><h4 class="text-xs font-semibold text-gray-800 mb-2">GPU VRAM usage</h4>';
    html += '<div class="space-y-3">';
    for (const card of cards) {
        const totalMb = Number(card.total_mb) || 0;
        const tasks = card.tasks || [];
        const reservedTotal = tasks.reduce((sum, t) => sum + (Number(t.reserved_mb) || 0), 0);
        const anyOverBudget = tasks.some(t => t.over_budget);

        html += '<div class="p-2 bg-gray-50 rounded text-xs">';
        html += `<div class="flex items-center justify-between mb-1">
            <span class="font-mono px-1.5 py-0.5 rounded bg-gray-200 text-gray-700">${escapeHtml((card.kind || '?').toUpperCase())} ${escapeHtml(card.card_id || '')}</span>
            <span class="text-gray-500">${_fmtMbW(reservedTotal)} reserved of ${_fmtMbW(totalMb)}${anyOverBudget ? ' <span class="text-red-600 font-semibold">(over budget)</span>' : ''}</span>
        </div>`;

        html += '<div class="relative h-3 bg-gray-200 rounded-full overflow-hidden">';
        if (totalMb > 0 && tasks.length) {
            let offsetPct = 0;
            tasks.forEach((t, i) => {
                const widthPct = Math.max(0, Math.min(100, (Number(t.reserved_mb) || 0) / totalMb * 100));
                const color = t.over_budget ? 'bg-red-500/80' : _GPU_SEGMENT_COLORS[i % _GPU_SEGMENT_COLORS.length];
                const label = t.node_kind || `task ${t.task_id}`;
                const typical = t.typical_peak_mb != null ? _fmtMbW(t.typical_peak_mb) : 'no history yet';
                const title = `${label} (task ${t.task_id}, ${t.mode})\nReserved: ${_fmtMbW(t.reserved_mb)}\nTypical peak: ${typical}${t.over_budget ? '\nOVER BUDGET vs. reservation' : ''}`;
                html += `<div class="absolute top-0 h-full ${color}" style="left:${offsetPct}%;width:${widthPct}%" title="${escapeHtml(title)}"></div>`;
                offsetPct += widthPct;
            });
        }
        if (totalMb > 0 && card.live_used_mb != null) {
            const livePct = Math.max(0, Math.min(100, (Number(card.live_used_mb) || 0) / totalMb * 100));
            html += `<div class="absolute top-0 h-full w-0.5 bg-black" style="left:${livePct}%" title="Live used (single-GPU worker, from heartbeat): ${escapeHtml(_fmtMbW(card.live_used_mb))}"></div>`;
        }
        html += '</div>';

        if (card.live_used_mb != null) {
            html += `<div class="mt-1 text-gray-400">Live used (this worker's only GPU): ${_fmtMbW(card.live_used_mb)}</div>`;
        }

        if (!tasks.length) {
            html += '<p class="mt-1 text-gray-400 italic">No active reservations on this card.</p>';
        }
        html += '</div>';
    }
    html += '</div></div>';
    return html;
}

async function showWorkerDetail(workerId, workerName) {
    // Clear any previous refresh interval
    if (_detailRefreshInterval) {
        clearInterval(_detailRefreshInterval);
        _detailRefreshInterval = null;
    }

    const modal = document.getElementById('worker-detail-modal');
    const title = document.getElementById('worker-detail-title');
    const body = document.getElementById('worker-detail-body');
    title.textContent = workerName;
    body.innerHTML = '<p class="text-sm text-gray-400">Loading...</p>';
    modal.classList.remove('hidden');
    // FX.20 R-037: dialog semantics + focus trap + focus return.
    // dismissible:false — this modal has no Escape-close today; the × and
    // post-delete paths close it and restore focus (helper's observer).
    if (window.modA11y) window.modA11y.attach(modal, { role: 'dialog', dismissible: false });

    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const resp = await fetch(`${apiUrl}/workers/${workerId}/hardware-profile`, {
            headers: { Authorization: `Bearer ${token}` }
        });
        if (!resp.ok) throw new Error(`${resp.status}`);
        const data = await resp.json();
        const p = data.hardware_profile;
        if (!p) {
            body.innerHTML = '<p class="text-sm text-gray-500">No hardware profile yet. Worker needs to send a heartbeat first.</p>';
            return;
        }

        const fmt2 = (v) => {
            if (v == null || v === 0) return '—';
            const fixed = Number(v).toFixed(2);
            const [whole, dec] = fixed.split('.');
            const withCommas = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            return withCommas + '.' + dec;
        };
        const scoreBar = (label, val) => {
            const v = val || 0;
            const pct = Math.min(100, Math.max(0, v / 50 * 100));
            return `<div class="flex items-center gap-2 text-xs">
                <span class="w-24 text-gray-600">${label}</span>
                <div class="flex-1 bg-gray-200 rounded-full h-2"><div class="bg-primary/80 h-2 rounded-full" style="width:${pct}%"></div></div>
                <span class="w-20 text-right font-mono">${fmt2(v)}</span>
            </div>`;
        };

        // GPU / VRAM display logic. Two inputs here:
        //   - hardware_profile (p) — populated by the benchmark / specific
        //     GPU tools (nvidia-smi, rocm-smi). Carries marketing name and
        //     VRAM in GB.
        //   - hardware_specs.hardware_tags — the worker's detected
        //     accelerator list (e.g. ["cpu", "rocm", "intel"]). Populated
        //     even when no tool reports a marketing name (DRI vendor-ID
        //     fallback, or operator-provided WORKER_HARDWARE_TAGS).
        //
        // Prefer p.gpu_model when present (most specific), otherwise
        // surface the tag list so a worker with actual GPUs doesn't show
        // "None" just because rocm-smi isn't installed.
        const gpuAccelTags = (data.hardware_tags || []).filter(t => {
            const s = String(t).toLowerCase();
            return s !== 'cpu' && s !== 'default';
        });
        const hasAnyGPU = !!p.gpu_model || gpuAccelTags.length > 0;
        let serviceHostNoBench = false;
        let gpuDisplay;
        if (p.gpu_model && String(p.gpu_model).toLowerCase() !== 'none') {
            gpuDisplay = escapeHtml(p.gpu_model);
        } else if (gpuAccelTags.length > 0) {
            // Show tags as uppercased list, e.g. "ROCm + Intel".
            const pretty = gpuAccelTags.map(t => {
                const s = String(t).toLowerCase();
                if (s === 'cuda') return 'CUDA';
                if (s === 'rocm') return 'ROCm';
                if (s === 'intel') return 'Intel';
                if (s === 'npu') return 'NPU';
                return s.toUpperCase();
            });
            gpuDisplay = pretty.join(' + ') + ' <span class="text-[10px] text-gray-400">(detected by tag)</span>';
        } else {
            gpuDisplay = 'None';
        }

        let vramDisplay;
        if (p.gpu_vram_gb) {
            vramDisplay = p.gpu_vram_gb + ' GB';
        } else if (!hasAnyGPU) {
            vramDisplay = '<span class="text-gray-400">No GPU</span>';
        } else {
            // We know a GPU is there (via tags) but no tool reported
            // its VRAM number. Benchmark will fill this in.
            vramDisplay = '<span class="text-gray-500">Service host - no accelerator benchmark applicable</span>';
        }

        let html = '<div class="space-y-3">';
        html += '<div class="grid grid-cols-2 gap-2 text-sm">';
        html += `<div><span class="text-gray-500">CPU:</span> <span class="font-medium">${p.cpu_model || p.cpu_cores + ' cores'}</span></div>`;
        html += `<div><span class="text-gray-500">RAM:</span> <span class="font-medium">${p.ram_gb || '?'} GB</span></div>`;
        html += `<div><span class="text-gray-500">GPU:</span> <span class="font-medium">${gpuDisplay}</span></div>`;
        html += `<div><span class="text-gray-500">VRAM:</span> <span class="font-medium">${vramDisplay}</span></div>`;
        // Architecture sits with CPU/RAM/GPU because it is the same kind of
        // fact — what this machine IS — and because the detail panel is where
        // someone lands when asking why a worker is not picking work up.
        html += `<div><span class="text-gray-500">Architecture:</span> ${getArchInfo({ arch: data.arch })}</div>`;
        // What that architecture MEANS for this machine, filled in async.
        // "arm64" on its own is trivia; "can be given 0 of 14 base images" is
        // the answer to why a healthy-looking worker never picks anything up.
        html += `<div class="col-span-2" id="worker-detail-arch-reach"></div>`;
        if (p.cloud_provider) {
            html += `<div><span class="text-gray-500">Cloud:</span> <span class="font-medium">${p.cloud_provider.toUpperCase()} ${p.instance_type}</span></div>`;
        }
        html += `<div><span class="text-gray-500">Benchmark:</span> <span id="worker-detail-bench-status" class="font-medium">${p.benchmark_status}${p.benchmarked_at ? ' (' + new Date(p.benchmarked_at).toLocaleDateString() + ')' : ''}</span></div>`;
        // MAC.1.s13b / 25.5.s5 — the worker's own disk-gate and last-cleanup
        // readings, read-only. Empty when the profile carries no such block.
        // Stable hooks: the refresh pass below repaints them in place.
        html += `<div class="col-span-2" id="worker-detail-disk-floor" data-testid="worker-disk-floor"><div class="${_diskFloorClass(data.disk_floor)}">${escapeHtml(_diskFloorInner(data.disk_floor))}</div></div>`;
        html += `<div class="col-span-2" id="worker-detail-last-cleanup" data-testid="worker-last-cleanup"><div class="text-xs text-gray-700">${escapeHtml(_lastCleanupInner(data.last_cleanup))}</div></div>`;
        html += '</div>';

        // Wrap the top scores in a stable hook so _refreshWorkerDetailSections
        // can swap it in place as benchmarks complete without a full rebuild.
        // The helper returns its own styled block when populated and an
        // empty string otherwise, so the wrapper itself has no chrome.
        html += `<div id="worker-detail-scores">${_renderTopScores(p)}</div>`;

        // Release info panel — shows which agent version is installed on THIS
        // worker vs. the latest available in MinIO, with release date and
        // optional changelog link.
        const relVer = _latestRelease.version;
        if (relVer) {
            const relDate = _latestRelease.released_at
                ? new Date(_latestRelease.released_at).toLocaleString()
                : '—';
            const changelog = _latestRelease.changelog_url
                ? ` <a href="${escapeHtml(_latestRelease.changelog_url)}" target="_blank" rel="noopener" class="text-primary hover:underline">changelog</a>`
                : '';
            html += '<div class="border-t pt-3 text-xs text-gray-700">';
            html += '<h4 class="font-semibold text-gray-800 mb-1">Agent Version</h4>';
            html += `<div class="grid grid-cols-2 gap-1">`;
            html += `<div>Installed: <span class="font-mono">${escapeHtml(data.agent_version || '—')}</span></div>`;
            html += `<div>Latest: <span class="font-mono">${escapeHtml(relVer)}</span>${changelog}</div>`;
            html += `<div class="col-span-2 text-gray-500">Released: ${escapeHtml(relDate)}</div>`;
            if (_latestRelease.notes) {
                html += `<div class="col-span-2 text-gray-500 italic">“${escapeHtml(_latestRelease.notes)}”</div>`;
            }
            html += '</div></div>';
        }

        // Accelerators panel — per-device GPU/NPU inventory with individual scores.
        // Fetched from a separate endpoint so the hardware-profile call stays fast.
        try {
            const accelResp = await fetch(`${apiUrl}/accelerators/worker/${encodeURIComponent(workerName)}`, {
                headers: { Authorization: `Bearer ${token}` }
            });
            if (accelResp.ok) {
                const accelData = await accelResp.json();
                const accels = accelData.accelerators || [];
                serviceHostNoBench = accels.length === 0 && gpuAccelTags.length > 0;
                const progress = accelData.profiling_status;

                {
                    html += '<div id="worker-detail-accelerators" class="border-t pt-3">';
                    html += '<h4 class="text-xs font-semibold text-gray-800 mb-2">Accelerators';
                    const prog = accelData.progress || {};
                    if (progress === 'running' || progress === 'pending') {
                        const done = prog.completed || 0;
                        const total = prog.total || 1;
                        const pct = Math.round((done / total) * 100);
                        html += ` <span data-bench-header class="text-primary font-normal">Profiling ${done}/${total}</span>`;
                        html += `</h4>`;
                        html += `<div data-bench-bar class="h-2 bg-gray-200 rounded-full overflow-hidden mb-3">`;
                        html += `<div class="h-full bg-primary/80 rounded-full transition-all duration-700 ease-out" style="width: ${pct}%"></div>`;
                        html += `</div>`;
                    } else {
                        html += '</h4>';
                    }

                    // === SYSTEM BENCHMARK STATUS ===
                    // flex-1 column wrapped with a stable `data-system-body`
                    // hook so _refreshWorkerDetailSections can swap the
                    // contents in place as benchmarks progress/complete.
                const sysBench = accelData.system_benchmark;
                {
                    const sysRunning = p.benchmark_status === 'running';
                    if (accels.length === 0 && gpuAccelTags.length > 0) {
                        html += '<div class="p-2 bg-gray-50 rounded text-xs mb-2">';
                        html += '<div class="flex items-center gap-2">';
                        html += '<span class="px-1.5 py-0.5 rounded font-mono bg-gray-200 text-gray-700">SYSTEM</span>';
                        html += '<div class="flex-1" data-system-body>';
                        html += _renderSystemCardInner(sysBench);
                        html += '</div>';
                        html += '<span class="ml-auto text-xs text-gray-400">Benchmark: skipped</span>';
                        html += '</div></div>';
                    } else {
                        const sysBtn = `<button onclick="triggerBenchmark(${jsNumStr(workerId)}, 'system')" class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/25 border border-primary ${sysRunning ? 'opacity-50 cursor-not-allowed' : ''}" ${sysRunning ? 'disabled' : ''} title="Run the SYSTEM benchmark only">Run</button>`;
                        html += '<div class="p-2 bg-gray-50 rounded text-xs mb-2">';
                        html += '<div class="flex items-center gap-2">';
                        html += '<span class="px-1.5 py-0.5 rounded font-mono bg-gray-200 text-gray-700">SYSTEM</span>';
                        html += '<div class="flex-1" data-system-body>';
                        html += _renderSystemCardInner(sysBench);
                        html += '</div>';
                        html += `<div class="ml-auto">${sysBtn}</div>`;
                        html += '</div></div>';
                    }
                }

                // === PER-DEVICE ACCELERATORS ===
                if (accels.length === 0 && gpuAccelTags.length > 0) {
                    html += '<div class="space-y-1">';
                    for (const tag of gpuAccelTags) {
                        const pretty = tag === 'cuda' ? 'NVIDIA CUDA' : tag === 'rocm' ? 'AMD ROCm' : tag === 'intel' ? 'Intel GPU' : tag === 'npu' ? 'NPU' : tag.toUpperCase();
                        html += `<div class="flex items-center gap-2 p-2 bg-gray-50 rounded text-xs">
                            <span class="px-1.5 py-0.5 rounded font-mono bg-gray-100 text-gray-600">${escapeHtml(tag.toUpperCase())}</span>
                            <span class="text-gray-500">Service host - no accelerator benchmark applicable</span>
                        </div>`;
                    }
                    html += '</div>';
                } else if (accels.length === 0) {
                    html += '<p class="text-xs text-gray-400">No accelerators detected on this worker.</p>';
                } else {
                    html += '<div class="space-y-2">';
                    for (const a of accels) {
                        const kindBadge = {
                            cuda: 'bg-green-100 text-green-800',
                            rocm: 'bg-red-100 text-red-900',
                            intel: 'bg-primary/20 text-primary',
                            npu: 'bg-purple-100 text-purple-800',
                        }[a.kind] || 'bg-gray-100 text-gray-800';

                        const accelRunning = a.benchmark_status === 'running' || p.benchmark_status === 'running';
                        const accelSelector = `${a.kind}:${a.index}`;
                        const accelBtn = `<button onclick="triggerBenchmark(${jsNumStr(workerId)}, '${escapeHtml(accelSelector)}')" class="text-[10px] px-2 py-0.5 rounded bg-primary/10 text-primary hover:bg-primary/25 border border-primary ${accelRunning ? 'opacity-50 cursor-not-allowed' : ''}" ${accelRunning ? 'disabled' : ''} title="Run just this ${escapeHtml(a.kind.toUpperCase())} #${a.index} benchmark">Run</button>`;
                        html += `<div class="p-2 bg-gray-50 rounded text-xs">`;
                        html += `<div class="flex items-start gap-2">`;
                        html += `<span class="px-1.5 py-0.5 rounded font-mono ${kindBadge}">${escapeHtml(a.kind.toUpperCase())} #${a.index}</span>`;
                        // flex-1 column wrapped with stable data-accel-body
                        // hook so refresh can swap the contents in place.
                        html += `<div class="flex-1" data-accel-body="${a.kind}-${a.index}">`;
                        html += _renderAccelCardInner(a);
                        html += `</div>`;
                        html += `<div class="ml-auto">${accelBtn}</div>`;
                        html += `</div></div>`;
                    }
                    html += '</div>';
                }
                html += '</div>';
                }
            }
        } catch (accelErr) {
            console.warn('accelerator panel failed (non-fatal)', accelErr);
        }

        // megaplan 14.8 — per-card VRAM bar (reserved vs. typical peak).
        // Platform-admin only (mirrors the fleet-storage gate on this page).
        // Errors here must never block the rest of the modal from rendering.
        if (_isAdmin) {
            try {
                const gpuResp = await fetch(`${apiUrl}/workers/${workerId}/gpu-usage`, {
                    headers: { Authorization: `Bearer ${token}` }
                });
                if (gpuResp.ok) {
                    const gpuData = await gpuResp.json();
                    html += _renderGpuUsageSection(gpuData);
                }
            } catch (gpuErr) {
                console.warn('GPU usage panel failed (non-fatal)', gpuErr);
            }
        }

        html += `<div class="border-t pt-3 flex gap-2">
            ${serviceHostNoBench ? '<div class="text-xs text-gray-400">No benchmarks apply to this service host (no accelerator detected to benchmark).</div>' : `<button onclick="triggerBenchmark(${jsNumStr(workerId)})" class="px-4 py-2 bg-primary text-white text-sm rounded hover:bg-primary/90 transition-colors ${p.benchmark_status === 'running' ? 'opacity-50 cursor-not-allowed' : ''}" ${p.benchmark_status === 'running' ? 'disabled' : ''}>
                ${p.benchmark_status === 'running' ? 'Benchmarking...' : 'Run All Benchmarks'}
            </button>`}
            <button onclick="showWorkerQueueModal(${jsAttrStr(workerName)})" class="px-4 py-2 bg-primary text-white text-sm rounded hover:bg-primary/90 transition-colors">
                Queue
            </button>
            <button onclick="deleteWorker(${jsAttrStr(workerName)})" class="px-4 py-2 bg-red-600 text-white text-sm rounded hover:bg-red-700 transition-colors">
                Delete Worker
            </button>
        </div>`;
        html += '</div>';
        // Cost basis — a REAL form on the worker's own detail panel, not a
        // chain of prompt() boxes. Entering five related numbers one modal at a
        // time gives you no way to see what you already typed, no way to go
        // back, and no sight of the rate they produce.
        const _paidDetail = await isPaidEdition(); // CE (or unknown edition) hides Cost basis + NOAH idle-harvest (Tim B3 #2/#3)
        if (_paidDetail) {
            html += renderWorkerCostForm(workerName, _workersByName.get(workerName) || {});
            html += renderNoahSettingsForm(workerName);
            html += await renderNoahLendForm(workerName);
        }

        body.innerHTML = html;
        // After the panel exists, not before — the target div is created above.
        loadWorkerArchReach(data.arch);
        loadNoahSettings(workerName);
        loadNoahLend(workerName);

        // Auto-refresh: every 3s while a benchmark is running, re-fetch data
        // and update ONLY the section contents via stable hooks. No modal
        // rebuild — ever. When the run finishes we do one final refresh to
        // paint the completed/failed badges and scores, then stop polling.
        const isProfileActive = p && (p.benchmark_status === 'running' || p.benchmark_status === 'pending');
        if (isProfileActive && !_detailRefreshInterval) {
            _detailRefreshInterval = setInterval(async () => {
                if (modal.classList.contains('hidden')) {
                    clearInterval(_detailRefreshInterval);
                    _detailRefreshInterval = null;
                    return;
                }
                const status = await _refreshWorkerDetailSections(workerId, workerName, apiUrl, token);
                if (status === 'completed' || status === 'failed') {
                    // Final in-place paint — keep the modal state (scroll,
                    // expanded sections) exactly as the user had it.
                    clearInterval(_detailRefreshInterval);
                    _detailRefreshInterval = null;
                    await _refreshWorkerDetailSections(workerId, workerName, apiUrl, token);
                }
            }, 3000);
        }
        if (!isProfileActive && _detailRefreshInterval) {
            clearInterval(_detailRefreshInterval);
            _detailRefreshInterval = null;
        }
    } catch (e) {
        body.innerHTML = `<p class="text-sm text-red-500">Failed to load: ${escapeHtml(e?.message || String(e))}</p>`;
    }
}

async function triggerBenchmark(workerId, only) {
    // `only` is optional:
    //   undefined         → full benchmark (system + every accelerator)
    //   "system"          → just the system benchmark
    //   "<kind>:<index>"  → just one accelerator, e.g. "cuda:0"
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const qs = only ? `?only=${encodeURIComponent(only)}` : '';
        const resp = await fetch(`${apiUrl}/workers/${workerId}/benchmark${qs}`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}` }
        });
        if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.status); }
        const scopeLabel = only ? (only === 'system' ? 'SYSTEM benchmark' : `${only.toUpperCase()} benchmark`) : 'Full benchmark';
        showSuccess(`${scopeLabel} dispatched.`);

        // LDF-3 §8.1 — Tim reported the workers TABLE didn't reflect the
        // pending profile until manual reload. Optimistically flip the
        // row's benchmark_status to 'pending' in the cached worker rows
        // and trigger an immediate loadWorkers() refresh so the table
        // chip says "Profile queued" right away instead of waiting up
        // to 5s for the periodic auto-refresh.
        if (typeof loadWorkers === 'function') {
            try { await loadWorkers(); } catch { /* non-fatal */ }
        }

        // One immediate section-level refresh so the clicked card flips to
        // "queued/running" right away — then the interval takes over. No
        // full-modal rebuild.
        const workerName = document.getElementById('worker-detail-title').textContent;
        await _refreshWorkerDetailSections(workerId, workerName, apiUrl, token);
        // Make sure the auto-refresh interval is running; it self-starts on
        // the next call to showWorkerDetail but if this is a re-trigger
        // inside an already-open modal we need to kick it here.
        if (!_detailRefreshInterval) {
            const modal = document.getElementById('worker-detail-modal');
            if (modal && !modal.classList.contains('hidden')) {
                _detailRefreshInterval = setInterval(async () => {
                    if (modal.classList.contains('hidden')) {
                        clearInterval(_detailRefreshInterval);
                        _detailRefreshInterval = null;
                        return;
                    }
                    const status = await _refreshWorkerDetailSections(workerId, workerName, apiUrl, token);
                    if (status === 'completed' || status === 'failed') {
                        clearInterval(_detailRefreshInterval);
                        _detailRefreshInterval = null;
                        await _refreshWorkerDetailSections(workerId, workerName, apiUrl, token);
                    }
                }, 3000);
            }
        }
    } catch (e) {
        showError(`Benchmark failed: ${e.message}`);
    }
}

async function deleteWorker(workerName) {
    if (!await showConfirm(`Permanently delete worker "${workerName}"? This cannot be undone.`)) return;
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem('accessToken');
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}`, {
            method: 'DELETE',
            headers: { Authorization: `Bearer ${token}` }
        });
        if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${resp.status}`); }
        showInfo(`Worker "${workerName}" deleted.`);
        document.getElementById('worker-detail-modal')?.classList.add('hidden');
        loadWorkers();
    } catch (e) {
        showError(`Failed to delete: ${e.message}`);
    }
}

function getActions(worker) {
    if (!_isAdmin) return '<span class="text-xs text-gray-400">View only</span>';
    // Revoked enrolments: the only useful admin action is Reinstate, which
    // flips the enrolment back to PENDING so the operator can re-run
    // setup-worker.sh and approve the worker fresh.
    if (worker.enrollment_status === 'revoked') {
        return `
            <div class="flex items-center gap-2">
                <button onclick="reinstateWorker(${jsAttrStr(worker.worker_name)})" class="flex-1 px-3 py-1 bg-amber-600 text-white text-xs rounded hover:bg-amber-700 transition-colors text-center" title="Flip REVOKED enrolment to PENDING — operator then re-runs setup-worker.sh on the remote host and approves fresh.">Reinstate</button>
            </div>
        `;
    }
    // FIX-CE-WORKER-APPROVE-BUTTON BEGIN (lane gpu1-2, 11-11)
    // PENDING enrolments: CE enrolment defaults to PENDING (worker_management_service.py
    // only enrols APPROVED when the token had auto_approve=true, and the Add Worker
    // checkbox is unchecked by default), so the operator needs a way to flip
    // PENDING -> APPROVED from the page. Sibling of the 'revoked' branch above:
    // the enrolment status is the exact lowercase enum the API sends
    // (CORE/API/models.py:8187 WorkerEnrollmentStatus.PENDING = "pending"), same
    // casing the 'revoked' compare uses. NO edition gate: the approve route is
    // CE-allowed and identical on paid.
    if (worker.enrollment_status === 'pending') {
        return `
            <div class="flex items-center gap-2">
                <button onclick="approveWorker(${jsAttrStr(worker.worker_name)})" class="flex-1 px-3 py-1 bg-emerald-600 text-white text-xs rounded hover:bg-emerald-700 transition-colors text-center" title="Approve this PENDING enrolment so the worker can pull tasks.">Approve</button>
            </div>
        `;
    }
    // FIX-CE-WORKER-APPROVE-BUTTON END
    if (worker.status === 'licensed') {
        const nm = worker.worker_name;
        // can_drain=false means pausing/updating this worker would leave the
        // system with zero active workers (and it's NOT the single-node
        // embedded exception). Disable both drain-class buttons with a
        // tooltip explaining why, instead of letting the click 409.
        const blockReason = !worker.can_drain ? (worker.can_drain_reason || 'Last active worker') : '';
        const blocked = !worker.can_drain;

        let drainBtn;
        if (worker.paused) {
            // Resuming is always allowed — it adds capacity, not removes it.
            drainBtn = `<button onclick="resumeWorker(${jsAttrStr(nm)})" class="flex-1 px-3 py-1 bg-emerald-600 text-white text-xs rounded hover:bg-emerald-700 transition-colors text-center" title="Resume claiming tasks.">Resume</button>`;
        } else if (blocked) {
            drainBtn = `<button disabled class="flex-1 px-3 py-1 bg-yellow-200 text-yellow-800 text-xs rounded text-center cursor-not-allowed" title="${escapeHtml(blockReason)}">Pause</button>`;
        } else {
            drainBtn = `<button onclick="pauseWorker(${jsAttrStr(nm)})" class="flex-1 px-3 py-1 bg-yellow-600 text-white text-xs rounded hover:bg-yellow-700 transition-colors text-center" title="Drain: finish in-flight tasks, stop claiming new ones. Sticky \u2014 operator must Resume.">Pause</button>`;
        }

        let updateBtn;
        if (worker.update_requested) {
            updateBtn = `<button disabled class="flex-1 px-3 py-1 bg-primary/30 text-primary text-xs rounded text-center cursor-wait" title="Worker is draining and will self-update.">Updating\u2026</button>`;
        } else if (worker.update_available && blocked) {
            updateBtn = `<button disabled class="flex-1 px-3 py-1 bg-primary/20 text-primary text-xs rounded text-center cursor-not-allowed" title="${escapeHtml(blockReason)}">Update</button>`;
        } else if (worker.update_available) {
            updateBtn = `<button onclick="updateWorker(${jsAttrStr(nm)})" class="flex-1 px-3 py-1 bg-primary text-white text-xs rounded hover:bg-primary/90 transition-colors text-center" title="One-shot drain + self-update + auto-resume. Worker finishes in-flight tasks, swaps to v${worker.latest_agent_version}, comes back online automatically \u2014 no Resume click needed.">Update</button>`;
        } else {
            updateBtn = `<button disabled class="flex-1 px-3 py-1 bg-gray-100 text-gray-500 text-xs rounded text-center" title="Worker is already on the latest version (${worker.agent_version || 'unknown'}).">Up to date</button>`;
        }
        return `
            <div class="flex flex-col gap-1">
                <div class="flex items-center gap-2">
                    <button onclick="showWorkerDetail(${jsNumStr(worker.id)}, ${jsAttrStr(worker.worker_name || worker.name || '')})" class="flex-1 px-3 py-1 text-primary text-xs border border-primary rounded hover:bg-primary/15 transition-colors text-center">Details</button>
                    ${updateBtn}
                    ${drainBtn}
                    <button id="revoke-license-${worker.id}" class="flex-1 px-3 py-1 bg-red-600 text-white text-xs rounded hover:bg-red-700 transition-colors text-center">Remove</button>
                </div>
            </div>
        `;
    } else if (worker.status === 'unlicensed' || worker.status === 'offline') {
        return `
            <div class="flex flex-col gap-1">
                <div class="flex items-center gap-2">
                    <select id="type-select-${worker.id}" aria-label="Worker assignment type" class="flex-1 text-xs px-2 py-1 border border-gray-300 rounded bg-white" data-worker-id="${worker.id}">
                        <option value="platform_pool">Pool</option>
                        <option value="platform_only">Platform Only</option>
                        <option value="platform_assigned">Assigned</option>
                        <option value="tenant_owned">Tenant Owned</option>
                    </select>
                    <select id="tenant-select-${worker.id}" aria-label="Worker tenant" class="text-xs px-2 py-1 border border-gray-300 rounded bg-white hidden">
                        <option value="">Select tenant...</option>
                    </select>
                </div>
                <div class="flex items-center gap-2">
                    <button id="assign-license-${worker.id}" class="flex-1 px-3 py-1 bg-green-600 text-white text-xs rounded hover:bg-green-700 transition-colors text-center">Add Worker</button>
                    <button onclick="deleteWorker(${jsAttrStr(worker.worker_name || worker.name || '')})" class="flex-1 px-3 py-1 bg-red-600 text-white text-xs rounded hover:bg-red-700 transition-colors text-center">Delete</button>
                </div>
            </div>
        `;
    }
    return '';
}

async function assignLicense(workerName, workerId) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    // Get selected worker type from dropdown
    const typeSelect = document.getElementById(`type-select-${workerId}`);
    const ownerType = typeSelect ? typeSelect.value : 'platform_pool';

    // Get selected tenant if applicable
    const tenantSelect = document.getElementById(`tenant-select-${workerId}`);
    let ownerTenantId = null;
    let assignedTenantIds = [];

    if (ownerType === 'tenant_owned') {
        if (!tenantSelect || !tenantSelect.value) {
            showError('Please select a tenant for tenant-owned workers');
            return;
        }
        ownerTenantId = parseInt(tenantSelect.value);
    } else if (ownerType === 'platform_assigned') {
        if (!tenantSelect || !tenantSelect.value) {
            showError('Please select a tenant to assign this worker to');
            return;
        }
        assignedTenantIds = [parseInt(tenantSelect.value)];
    }

    const tenantLabel = ownerTenantId ? ` for tenant ${ownerTenantId}` : assignedTenantIds.length ? ` assigned to tenant ${assignedTenantIds.join(', ')}` : '';
    if (!await showConfirm(`Add worker "${workerName}" as ${ownerType.replace('_', ' ')}${tenantLabel}?`)) {
        return;
    }

    try {
        const response = await fetch(`${apiUrl}/workers/assign-license`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                worker_name: workerName,
                owner_type: ownerType,
                owner_tenant_id: ownerTenantId,
                assigned_tenant_ids: assignedTenantIds
            })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to add worker');
        }

        const result = await response.json();
        showSuccess(result.message);

        // Reload workers and summary
        await loadWorkers();
        await loadLicenseSummaryIfPaid();

    } catch (error) {
        console.error('Error assigning license:', error);
        showError(error.message);
    }
}

async function revokeLicense(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    const confirmText = `Remove worker "${workerName}" from active workers?\n\nThis will free up a slot for another worker. The worker will be paused and cannot receive tasks.`;
    if (!await showConfirm(confirmText)) {
        return;
    }

    try {
        const response = await fetch(`${apiUrl}/workers/revoke-license`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ worker_name: workerName })
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to remove worker');
        }

        const result = await response.json();
        showSuccess(result.message);

        // Reload workers and summary
        await loadWorkers();
        await loadLicenseSummaryIfPaid();

    } catch (error) {
        console.error('Error revoking license:', error);
        showError(error.message);
    }
}

// Drain a worker — worker stops claiming, finishes in-flight tasks, then
// (if update_available) self-updates its binary before resuming.
async function pauseWorker(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    if (!await showConfirm(`Pause "${workerName}"?\n\nIn-flight tasks finish, new tasks will NOT be claimed. Useful for rolling out a new worker binary or taking the host down for maintenance.`)) {
        return;
    }
    try {
        const response = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/pause`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to pause worker');
        }
        const result = await response.json();
        showSuccess(result.message);
        await loadWorkers();
    } catch (error) {
        console.error('Error pausing worker:', error);
        showError(error.message);
    }
}

async function resumeWorker(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    try {
        const response = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/resume`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to resume worker');
        }
        const result = await response.json();
        showSuccess(result.message);
        await loadWorkers();
    } catch (error) {
        console.error('Error resuming worker:', error);
        showError(error.message);
    }
}

// Reinstate a REVOKED enrolment so the operator can re-run setup-worker.sh
// on the remote host and approve the worker fresh.
async function reinstateWorker(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    if (!await showConfirm(`Reinstate enrolment for "${workerName}"?\n\nThe enrolment flips from REVOKED back to PENDING. You will still need to (1) re-run setup-worker.sh on the remote host and (2) approve the worker after it re-enrols.`)) {
        return;
    }
    try {
        const response = await fetch(`${apiUrl}/workers/enrollments/${encodeURIComponent(workerName)}/reinstate`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to reinstate worker');
        }
        const result = await response.json();
        showSuccess(result.message);
        await loadWorkers();
    } catch (error) {
        console.error('Error reinstating worker:', error);
        showError(error.message);
    }
}

// FIX-CE-WORKER-APPROVE-BUTTON BEGIN (lane gpu1-2, 11-11)
// Approve a PENDING enrolment so the worker can start pulling tasks. Modeled
// on reinstateWorker exactly: same auth helper, showConfirm gate, base-URL
// helper, showSuccess, loadWorkers() refresh, showError toast. No edition
// gate: the approve route is CE-allowed and identical on paid.
async function approveWorker(workerName) {
    // In-flight guard (FIX-NIT-APPROVE-BUTTON-3, NIT 1): a second click while
    // the first approve is still open (confirm or POST) is a no-op.
    if (window.__approveInFlight?.has(workerName)) return;
    window.__approveInFlight ??= new Set();
    window.__approveInFlight.add(workerName);
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem("accessToken");
        if (!await showConfirm(`Approve enrolment for "${workerName}"?\n\nThe worker flips from PENDING to APPROVED and can start pulling tasks.`)) {
            return;
        }
        try {
            const response = await fetch(`${apiUrl}/workers/enroll/approve/${encodeURIComponent(workerName)}`, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            });
            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Failed to approve worker');
            }
            const result = await response.json();
            showSuccess(result.status === 'approved' ? `Worker "${workerName}" approved.` : escapeHtml(result.worker_name));
            await loadWorkers();
        } catch (error) {
            console.error('Error approving worker:', error);
            showError(error.message);
        }
    } finally {
        window.__approveInFlight.delete(workerName);
    }
}
// FIX-CE-WORKER-APPROVE-BUTTON END

// Trigger a one-shot update cycle. Worker drains, self-updates, auto-resumes.
// No need to click Resume after — unlike Pause, the update_requested flag
// clears automatically when the new version heartbeats.
async function updateWorker(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    if (!await showConfirm(`Update "${workerName}" now?\n\nThe worker will finish any in-flight tasks, swap its binary to the latest version, and automatically resume claiming. No further action needed.`)) {
        return;
    }
    try {
        const response = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/update`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        });
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to request update');
        }
        const result = await response.json();
        showSuccess(result.message);
        await loadWorkers();
    } catch (error) {
        console.error('Error triggering update:', error);
        showError(error.message);
    }
}

// scheduler-runtime-config-panel — generateTrainingData moved to
// Dashboard/js/scheduler-actions.js and is invoked from
// Settings → AI Configuration → Scheduler.

// Expose for inline onclick handlers in getActions().
window.pauseWorker = pauseWorker;
window.resumeWorker = resumeWorker;
window.reinstateWorker = reinstateWorker;
window.approveWorker = approveWorker;
window.updateWorker = updateWorker;

function formatDate(dateString) {
    if (!dateString) return 'Never';

    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// jsAttrStr(): for interpolating an UNTRUSTED STRING into an inline on* handler
// attribute (onclick="fn(...)"). JSON.stringify emits a complete, quoted JS
// string literal (so the handler receives exactly the original string), and
// escapeHtml then HTML-escapes it so nothing in it can close the attribute.
// Sites use fn(${jsAttrStr(name)}) — the helper supplies the quotes.
function jsAttrStr(s) {
    return escapeHtml(JSON.stringify(String(s ?? '')));
}

// jsNumStr(): numeric counterpart for ids/amounts interpolated into the same
// attribute context — coerces to a finite number, else 0, so no string can
// reach the attribute.
function jsNumStr(v) {
    return Number.isFinite(Number(v)) ? String(Number(v)) : '0';
}

function showSuccess(message) {
    // Simple success notification (could be enhanced with a toast component)
    showInfo('✅ ' + message);
}

function showError(message) {
    // Simple error notification (could be enhanced with a toast component)
    showInfo('❌ ' + message);
}

// ---------------------------------------------------------------------------
// Add Remote Worker flow
// ---------------------------------------------------------------------------

function showAddWorkerModal() {
    const existing = document.getElementById('add-worker-modal');
    if (existing) existing.remove();

    const modalHtml = `
        <div id="add-worker-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-6 border w-full max-w-lg shadow-lg rounded-md bg-white">
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-xl font-semibold text-gray-900">Add Remote Worker</h3>
                    <button onclick="document.getElementById('add-worker-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">&times;</button>
                </div>
                <div class="mt-4 space-y-4">
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1" for="add-worker-label">Token Label <span class="text-gray-400 font-normal">(for your reference only)</span></label>
                        <input id="add-worker-label" type="text" placeholder="e.g. GPU Server #3"
                               class="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-primary"/>
                        <p class="text-xs text-gray-500 mt-1">The worker will name itself from its hostname automatically</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1" for="add-worker-max-uses">Max Uses</label>
                        <input id="add-worker-max-uses" type="number" value="1" min="1"
                               class="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-primary"/>
                        <p class="text-xs text-gray-500 mt-1">1 for a single server, higher for Cloud</p>
                    </div>
                    <div class="flex items-center space-x-6">
                        <label class="flex items-center space-x-2">
                            <input id="add-worker-auto-approve" type="checkbox" class="rounded border-gray-300 text-primary focus:ring-primary"/>
                            <span class="text-sm text-gray-700">Auto-approve</span>
                        </label>
                        <label class="flex items-center space-x-2">
                            <input id="add-worker-auto-license" type="checkbox" class="rounded border-gray-300 text-primary focus:ring-primary"/>
                            <span class="text-sm text-gray-700">Auto-assign approval</span>
                        </label>
                    </div>
                    <div class="border-t pt-3">
                        <label class="flex items-start space-x-2">
                            <input id="add-worker-air-gapped" type="checkbox" class="rounded border-gray-300 text-primary focus:ring-primary mt-0.5"/>
                            <span class="flex-1">
                                <span class="text-sm font-medium text-gray-700">Air-Gapped Network</span>
                                <span class="block text-xs text-gray-500 mt-0.5">Worker has no internet / cannot auto-update OS packages. Calibration staleness ratchet is disabled (180-day fixed threshold).</span>
                            </span>
                        </label>
                    </div>
                    <div class="border-t pt-3">
                        <label class="block text-sm font-medium text-gray-700 mb-2">Worker home</label>
                        <div class="space-y-2">
                            <label class="flex items-start space-x-2">
                                <input type="radio" name="add-worker-home" value="local" checked class="mt-0.5 text-primary focus:ring-primary"/>
                                <span class="flex-1">
                                    <span class="text-sm font-medium text-gray-700">Local (stays on-site)</span>
                                    <span class="block text-xs text-gray-500 mt-0.5">Homes to the site control plane. Keeps working with the manager when the internet is cut.</span>
                                </span>
                            </label>
                            <label class="flex items-start space-x-2">
                                <input type="radio" name="add-worker-home" value="remote" class="mt-0.5 text-primary focus:ring-primary"/>
                                <span class="flex-1">
                                    <span class="text-sm font-medium text-gray-700">Remote (roams)</span>
                                    <span class="block text-xs text-gray-500 mt-0.5">Homes to the cloud anchor. Reaches the manager through the cloud rendezvous when online.</span>
                                </span>
                            </label>
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1" for="add-worker-pattern">Name Pattern (optional)</label>
                        <input id="add-worker-pattern" type="text" placeholder="e.g. burst-aws-.*"
                               class="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-primary"/>
                        <p class="text-xs text-gray-500 mt-1">Regex to restrict worker names this token can enroll</p>
                    </div>
                    <div>
                        <label class="block text-sm font-medium text-gray-700 mb-1" for="add-worker-expires">Expires In (hours)</label>
                        <input id="add-worker-expires" type="number" value="24" min="1"
                               class="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-primary"/>
                    </div>
                </div>
                <div class="flex justify-end mt-6 space-x-2">
                    <button onclick="document.getElementById('add-worker-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm font-medium rounded hover:bg-gray-300">Cancel</button>
                    <button id="add-worker-submit-btn"
                            class="px-4 py-2 bg-green-600 text-white text-sm font-medium rounded hover:bg-green-700">Generate Setup Command</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);

    // Close on ESC
    const escHandler = (e) => {
        if (e.key === 'Escape') {
            document.getElementById('add-worker-modal')?.remove();
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    // Close on backdrop click
    document.getElementById('add-worker-modal').addEventListener('click', (e) => {
        if (e.target.id === 'add-worker-modal') e.target.remove();
    });

    // Submit
    document.getElementById('add-worker-submit-btn').addEventListener('click', async () => {
        const label = document.getElementById('add-worker-label').value.trim();
        if (!label) {
            showError('Label is required');
            return;
        }

        const maxUses = parseInt(document.getElementById('add-worker-max-uses').value) || 1;
        const autoApprove = document.getElementById('add-worker-auto-approve').checked;
        const autoLicense = document.getElementById('add-worker-auto-license').checked;
        const airGapped = document.getElementById('add-worker-air-gapped').checked;
        const pattern = document.getElementById('add-worker-pattern').value.trim() || null;
        const expiresIn = parseInt(document.getElementById('add-worker-expires').value) || 24;
        const homeAnchor = document.querySelector('input[name="add-worker-home"]:checked')?.value || 'local';

        const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
        const token = sessionStorage.getItem("accessToken");

        const btn = document.getElementById('add-worker-submit-btn');
        btn.disabled = true;
        btn.textContent = 'Generating...';

        try {
            const resp = await fetch(`${apiUrl}/workers/enrollment-tokens`, {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${token}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    label,
                    max_uses: maxUses,
                    auto_approve: autoApprove,
                    auto_license: autoLicense,
                    allowed_name_pattern: pattern,
                    expires_in_hours: expiresIn,
                    home_anchor: homeAnchor,
                }),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.detail || 'Failed to create enrollment token');
            }

            const data = await resp.json();
            document.getElementById('add-worker-modal').remove();

            // Air-gapped is a worker-side declaration: prepend the env var
            // to the setup command so the worker process inherits it. The
            // API stores nothing here — the worker reports air_gapped on
            // its first heartbeat (worker is source of truth).
            // Air-gapped adds an inline var that must also survive `env_reset`,
            // so it goes on the sudo command line alongside ENROLLMENT_TOKEN
            // (NOT as a shell-level `VAR=... ` prefix that env_reset would strip).
            const airGappedInline = airGapped ? "WORKER_AIR_GAPPED=true " : '';
            // The bootstrap ALWAYS fetches from the public API URL — the mesh/magic
            // name (dev-…mod-connect.internal) is NOT resolvable until the worker is
            // already on the tailnet, so using it for the initial curl is a
            // chicken-and-egg failure ("Could not resolve host"). The worker gets
            // everything else (home anchor, per-anchor login server + preauth key,
            // and the manager's stable mesh name to pin its api_url to for roaming)
            // from the /workers/enroll/start response inside the script — so no
            // MOD_MESH_* env vars are needed here (the script ignored them anyway).
            // Pass the token (and any air-gapped var) INLINE to sudo — sudo
            // permits `VAR=val` arguments even under a sudoers `env_reset`
            // policy that ignores `-E`, whereas `export … && sudo -E` loses the
            // env var on hosts like WSL. The setup script also accepts
            // `--enrollment-token` as a fallback for non-sudo/root invocations.
            // FIX-ADD-WORKER-MODAL-API-URL-LAN: the worker machine is usually NOT
            // the CE host, so the URL it curls must be reachable from THERE - see
            // enrollApiUrl() above. Browser-side fetches above still use apiUrl.
            const enrollUrl = enrollApiUrl(apiUrl);
            const setupCmd = `curl -fsSL "${enrollUrl}/workers/setup-script" | sudo ${airGappedInline}ENROLLMENT_TOKEN='${data.token}' bash`;

            // Windows one-liner: reuses the SAME enrollment token as
            // BOOTSTRAP_TOKEN, but needs a separate short-lived download
            // token from /workers/agent/download-token (GP.6 — the Windows
            // sibling of setup-worker.sh's install flow; worker_install.ps1
            // reads MOD_API_URL/MOD_BOOTSTRAP_TOKEN from the environment
            // since `irm | iex` can't pass named PowerShell params through
            // a pipe). Best-effort: a 403/network failure here just means
            // no Windows tab is shown — the Linux command above still works.
            // The Windows/macOS install routes live in routers/worker_install.py,
            // which ships in CE, so both editions mint these (platform-admin only;
            // a 403/404 below just hides the tab).
            let windowsCmd = null;
            let macosCmd = null;
            {
                // Windows one-liner: reuses the SAME enrollment token as
                // BOOTSTRAP_TOKEN, but needs a separate short-lived download
                // token from /workers/agent/download-token (GP.6 — the Windows
                // sibling of setup-worker.sh's install flow; worker_install.ps1
                // reads MOD_API_URL/MOD_BOOTSTRAP_TOKEN from the environment
                // since `irm | iex` can't pass named PowerShell params through
                // a pipe). Best-effort: a 403/network failure here just means
                // no Windows tab is shown — the Linux command above still works.
                try {
                    const dlResp = await fetch(`${apiUrl}/workers/agent/download-token?os_name=windows&arch=amd64`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` },
                    });
                    if (dlResp.ok) {
                        const dl = await dlResp.json();
                        windowsCmd = `$env:MOD_API_URL='${enrollUrl}'; $env:MOD_BOOTSTRAP_TOKEN='${data.token}'; iex (irm '${enrollUrl}/workers/agent/install-script?token=${dl.token}')`;
                    }
                } catch (_) {
                    // Windows install command is optional — Linux flow above is unaffected.
                }

                // macOS one-liner: GP.7, the sibling of the GP.6 Windows tab
                // above. Reuses the SAME enrollment token as BOOTSTRAP_TOKEN
                // plus a fresh short-lived download token minted for
                // os_name=darwin (arch amd64 is just the token's default —
                // worker_install_macos.sh detects Intel vs Apple Silicon itself
                // via `uname -m` and passes its own arch to /agent/download,
                // same as the Linux script). Best-effort, same as Windows: a
                // 403/network failure just means no macOS tab is shown.
                try {
                    const dlResp = await fetch(`${apiUrl}/workers/agent/download-token?os_name=darwin&arch=amd64`, {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` },
                    });
                    if (dlResp.ok) {
                        const dl = await dlResp.json();
                        macosCmd = `curl -fsSL '${enrollUrl}/workers/agent/install-script?token=${dl.token}' | bash -s -- --api-url ${enrollUrl} --token ${dl.token} --bootstrap-token ${data.token}`;
                    }
                } catch (_) {
                    // macOS install command is optional — Linux flow above is unaffected.
                }
            }

            showSetupCommandModal(label, setupCmd, windowsCmd, macosCmd, data, autoApprove, autoLicense, pattern, airGapped);

        } catch (err) {
            showError(err.message);
            btn.disabled = false;
            btn.textContent = 'Generate Setup Command';
        }
    });
}


function showSetupCommandModal(label, setupCmd, windowsCmd, macosCmd, tokenData, autoApprove, autoLicense, pattern, airGapped) {
    const existing = document.getElementById('setup-cmd-modal');
    if (existing) existing.remove();

    const infoLines = [
        `Token expires: ${new Date(tokenData.expires_at).toLocaleString()}`,
        `Max uses: ${tokenData.max_uses || 'Unlimited'}`,
        autoApprove ? 'Auto-approve: YES (worker starts immediately)' : 'Auto-approve: NO (manual approval required)',
        autoLicense ? 'Auto-assign approval: YES' : '',
        pattern ? `Name pattern: ${pattern}` : '',
        airGapped ? 'Air-Gapped: YES (calibration ratchet disabled, 180-day fixed threshold)' : '',
        /localhost|127\./.test(setupCmd) ? 'This address only works on this computer. To add a worker on another machine, re-run ./scripts/install.sh --lan first, then open Add Worker again.' : '',
        (windowsCmd || macosCmd) ? 'Windows/macOS commands are valid for 15 minutes and one use' : '',
    ].filter(Boolean);

    // Windows/macOS tabs are only offered when their download-token mint
    // succeeded (platform-admin-only endpoint; this whole modal is
    // admin-gated too, but the mint can still fail on a stale session or if
    // worker-releases has no windows/darwin binary published yet). The
    // Linux tab (plain interactive worker_setup.sh) always shows — it needs
    // no download token.
    const hasExtraTabs = !!(windowsCmd || macosCmd);
    const tabsHtml = hasExtraTabs ? `
        <div class="flex gap-2 mb-2" role="tablist">
            <button id="setup-cmd-tab-linux" onclick="window.__setupCmdShowTab('linux')"
                    class="px-3 py-1 text-xs font-medium rounded-t border border-b-0 bg-gray-900 text-white">Linux</button>
            ${windowsCmd ? `<button id="setup-cmd-tab-windows" onclick="window.__setupCmdShowTab('windows')"
                    class="px-3 py-1 text-xs font-medium rounded-t border border-b-0 bg-gray-200 text-gray-600">Windows</button>` : ''}
            ${macosCmd ? `<button id="setup-cmd-tab-macos" onclick="window.__setupCmdShowTab('macos')"
                    class="px-3 py-1 text-xs font-medium rounded-t border border-b-0 bg-gray-200 text-gray-600">macOS</button>` : ''}
        </div>
    ` : '';

    const windowsBlockHtml = windowsCmd ? `
        <div class="relative hidden" id="setup-cmd-block-windows">
            <pre id="setup-cmd-text-windows" class="whitespace-pre-wrap text-sm text-green-400 bg-gray-900 p-4 rounded border border-gray-700 select-all cursor-text font-mono">${escapeHtml(windowsCmd)}</pre>
            <button id="copy-cmd-btn-windows" onclick="window.__copySetupCmd('windows')"
                    class="absolute top-2 right-2 px-3 py-1 bg-primary/80 text-white text-xs font-medium rounded hover:bg-primary/90">Copy</button>
        </div>
        <p class="text-xs text-gray-400 mt-1 hidden" id="setup-cmd-note-windows">Run in an Administrator PowerShell — it registers a Windows service.</p>
    ` : '';

    const macosBlockHtml = macosCmd ? `
        <div class="relative hidden" id="setup-cmd-block-macos">
            <pre id="setup-cmd-text-macos" class="whitespace-pre-wrap text-sm text-green-400 bg-gray-900 p-4 rounded border border-gray-700 select-all cursor-text font-mono">${escapeHtml(macosCmd)}</pre>
            <button id="copy-cmd-btn-macos" onclick="window.__copySetupCmd('macos')"
                    class="absolute top-2 right-2 px-3 py-1 bg-primary/80 text-white text-xs font-medium rounded hover:bg-primary/90">Copy</button>
        </div>
        <p class="text-xs text-gray-400 mt-1 hidden" id="setup-cmd-note-macos">Run in Terminal — installs a launchd service (system-wide with sudo, per-user without). Unsigned dev build: Gatekeeper quarantine is cleared automatically by the script.</p>
    ` : '';

    const modalHtml = `
        <div id="setup-cmd-modal" class="fixed inset-0 bg-gray-600 bg-opacity-50 overflow-y-auto h-full w-full z-50 flex items-center justify-center">
            <div class="relative mx-auto p-6 border w-full max-w-2xl shadow-lg rounded-md bg-white">
                <div class="flex justify-between items-center pb-3 border-b">
                    <h3 class="text-xl font-semibold text-gray-900">Setup Command: ${escapeHtml(label)}</h3>
                    <button onclick="document.getElementById('setup-cmd-modal').remove()"
                            class="text-gray-400 hover:text-gray-600 text-2xl leading-none" aria-label="Close">&times;</button>
                </div>
                <div class="mt-4">
                    <p class="text-sm text-gray-600 mb-2">Run this on the target machine:</p>
                    ${tabsHtml}
                    <div class="relative" id="setup-cmd-block-linux">
                        <pre id="setup-cmd-text" class="whitespace-pre-wrap text-sm text-green-400 bg-gray-900 p-4 rounded border border-gray-700 select-all cursor-text font-mono">${escapeHtml(setupCmd)}</pre>
                        <button id="copy-cmd-btn" onclick="window.__copySetupCmd('linux')"
                                class="absolute top-2 right-2 px-3 py-1 bg-primary/80 text-white text-xs font-medium rounded hover:bg-primary/90">Copy</button>
                    </div>
                    ${windowsBlockHtml}
                    ${macosBlockHtml}
                </div>
                <div class="mt-4 text-sm text-gray-500 space-y-1">
                    ${infoLines.map(l => `<p>${escapeHtml(l)}</p>`).join('')}
                </div>
                <div class="flex justify-end mt-4">
                    <button onclick="document.getElementById('setup-cmd-modal').remove()"
                            class="px-4 py-2 bg-gray-200 text-gray-700 text-sm font-medium rounded hover:bg-gray-300">Close</button>
                </div>
            </div>
        </div>
    `;

    document.body.insertAdjacentHTML('beforeend', modalHtml);

    if (hasExtraTabs) {
        const tabNames = ['linux', windowsCmd ? 'windows' : null, macosCmd ? 'macos' : null].filter(Boolean);
        window.__setupCmdShowTab = (which) => {
            tabNames.forEach((name) => {
                const active = name === which;
                document.getElementById(`setup-cmd-block-${name}`)?.classList.toggle('hidden', !active);
                document.getElementById(`setup-cmd-note-${name}`)?.classList.toggle('hidden', !active);
                const tab = document.getElementById(`setup-cmd-tab-${name}`);
                tab?.classList.toggle('bg-gray-900', active);
                tab?.classList.toggle('text-white', active);
                tab?.classList.toggle('bg-gray-200', !active);
                tab?.classList.toggle('text-gray-600', !active);
            });
        };
    }

    const escHandler = (e) => {
        if (e.key === 'Escape') {
            document.getElementById('setup-cmd-modal')?.remove();
            document.removeEventListener('keydown', escHandler);
        }
    };
    document.addEventListener('keydown', escHandler);

    document.getElementById('setup-cmd-modal').addEventListener('click', (e) => {
        if (e.target.id === 'setup-cmd-modal') e.target.remove();
    });
}


// ---------------------------------------------------------------------
// Queue modals — "System Queue" (platform-wide pre-queue) + per-worker
// "Queue" on each worker's detail panel. Both share a simple centered-
// modal skeleton with two columns: in-flight (RUNNING/CLAIMED) vs
// waiting (PENDING/ASSIGNED). Auto-refresh every 5s while open so
// operators watching a busy queue don't have to mash F5.
// ---------------------------------------------------------------------

function _queueModalShell(id, title, subtitle) {
    document.getElementById(id)?.remove();
    const div = document.createElement('div');
    div.id = id;
    div.className = 'fixed inset-0 z-50 bg-black bg-opacity-50 flex items-center justify-center p-4';
    div.innerHTML = `
        <div class="bg-white rounded-xl shadow-xl w-full max-w-5xl max-h-[90vh] overflow-hidden flex flex-col" onclick="event.stopPropagation()">
            <div class="flex items-center justify-between px-6 py-4 border-b">
                <div>
                    <h2 class="text-lg font-semibold text-gray-900">${title}</h2>
                    ${subtitle ? `<p class="text-xs text-gray-500 mt-0.5">${subtitle}</p>` : ''}
                </div>
                <button class="text-gray-500 hover:text-gray-700" onclick="document.getElementById(${jsAttrStr(id)}).remove()" aria-label="Close">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                        <path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"/>
                    </svg>
                </button>
            </div>
            <div class="flex-1 overflow-y-auto p-6" id="${id}-body">
                <div class="text-center text-gray-500 py-10">Loading…</div>
            </div>
        </div>
    `;
    div.addEventListener('click', (e) => { if (e.target.id === id) div.remove(); });
    document.body.appendChild(div);
    const esc = (e) => {
        if (e.key === 'Escape') {
            div.remove();
            document.removeEventListener('keydown', esc);
        }
    };
    document.addEventListener('keydown', esc);
    return div;
}

function _queueTaskRow(t) {
    const age = t.created_at ? Math.round((Date.now() - new Date(t.created_at).getTime()) / 1000) : null;
    const ageText = age == null ? '' : (age < 60 ? `${age}s` : age < 3600 ? `${Math.round(age/60)}m` : `${Math.round(age/3600)}h`);
    const statusColor = {
        RUNNING: 'bg-primary/20 text-primary',
        CLAIMED: 'bg-primary/20 text-primary',
        ASSIGNED: 'bg-yellow-100 text-yellow-700',
        PENDING: 'bg-gray-100 text-gray-700',
    }[t.status] || 'bg-gray-100 text-gray-700';
    return `
        <tr class="border-b last:border-b-0 hover:bg-gray-50">
            <td class="px-2 py-1.5 text-xs font-mono text-gray-700">${t.id}</td>
            <td class="px-2 py-1.5"><span class="text-[10px] font-mono px-1.5 py-0.5 rounded ${statusColor}">${escapeHtml(t.status)}</span></td>
            <td class="px-2 py-1.5 text-xs text-gray-800">${escapeHtml(t.node_kind || '—')}</td>
            <td class="px-2 py-1.5 text-xs font-mono text-gray-600">${escapeHtml(t.queue_name || '')}</td>
            <td class="px-2 py-1.5 text-xs text-gray-600 text-right">${t.priority ?? 0}</td>
            <td class="px-2 py-1.5 text-xs text-gray-500">${ageText}</td>
            <td class="px-2 py-1.5 text-xs text-gray-500">${escapeHtml(t.claimed_by_worker || t.assigned_to_worker || '')}</td>
        </tr>
    `;
}

function _queueTable(rows, emptyLabel) {
    if (!rows || rows.length === 0) {
        return `<div class="text-sm text-gray-400 italic text-center py-6">${emptyLabel}</div>`;
    }
    return `
        <div class="overflow-x-auto border rounded-md">
            <table class="min-w-full text-sm">
                <thead class="bg-gray-50 text-[10px] uppercase text-gray-500">
                    <tr>
                        <th class="px-2 py-1.5 text-left">ID</th>
                        <th class="px-2 py-1.5 text-left">Status</th>
                        <th class="px-2 py-1.5 text-left">DAG</th>
                        <th class="px-2 py-1.5 text-left">Queue</th>
                        <th class="px-2 py-1.5 text-right">Prio</th>
                        <th class="px-2 py-1.5 text-left">Age</th>
                        <th class="px-2 py-1.5 text-left">Worker</th>
                    </tr>
                </thead>
                <tbody>${rows.map(_queueTaskRow).join('')}</tbody>
            </table>
        </div>
    `;
}

async function _fetchJSON(url) {
    const token = sessionStorage.getItem('accessToken');
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const resp = await fetch(`${apiUrl}${url}`, {
        headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    return resp.json();
}

async function _refreshSystemQueue() {
    const body = document.getElementById('system-queue-modal-body');
    if (!body) return;
    try {
        const data = await _fetchJSON('/platform/queue');
        const counts = data.counts || {};
        const byQueue = data.by_queue || {};
        const chips = Object.entries(byQueue).map(([q, n]) =>
            `<span class="text-xs px-2 py-1 bg-primary/10 text-primary rounded border border-primary font-mono">${escapeHtml(q)}: ${n}</span>`
        ).join(' ');
        body.innerHTML = `
            <div class="flex flex-wrap gap-2 mb-4 items-center">
                <span class="text-sm text-gray-500">Pending by queue:</span>
                ${chips || '<span class="text-sm text-gray-400 italic">Pre-queue empty</span>'}
                <span class="ml-auto text-xs text-gray-500">running ${counts.running || 0} · claimed ${counts.claimed || 0} · pending ${counts.pending || 0} · assigned ${counts.assigned || 0}</span>
            </div>
            <h3 class="text-sm font-semibold text-gray-700 mb-2">In flight</h3>
            ${_queueTable(data.in_flight, 'Nothing currently running.')}
            <h3 class="text-sm font-semibold text-gray-700 mt-5 mb-2">Pre Queue</h3>
            ${_queueTable(data.pending, 'Pre-queue empty — workers idle.')}
        `;
    } catch (e) {
        body.innerHTML = `<div class="text-sm text-red-600 p-4">Failed to load queue: ${escapeHtml(String(e))}</div>`;
    }
}

window.showSystemQueueModal = function showSystemQueueModal() {
    const m = _queueModalShell('system-queue-modal', 'System Queue', 'Platform-wide pre-queue and in-flight tasks');
    // Stable hook for refresh
    m.querySelector('#system-queue-modal-body').id = 'system-queue-modal-body';
    _refreshSystemQueue();
    const intervalId = setInterval(() => {
        if (!document.getElementById('system-queue-modal')) {
            clearInterval(intervalId);
            return;
        }
        _refreshSystemQueue();
    }, 5000);
};

async function _refreshWorkerQueue(workerName) {
    const body = document.getElementById('worker-queue-modal-body');
    if (!body) return;
    try {
        const data = await _fetchJSON(`/workers/${encodeURIComponent(workerName)}/queue`);
        body.innerHTML = `
            <h3 class="text-sm font-semibold text-gray-700 mb-2">Working on</h3>
            ${_queueTable(data.in_flight, 'Worker is idle.')}
            <h3 class="text-sm font-semibold text-gray-700 mt-5 mb-2">Assigned / claimed (pending)</h3>
            ${_queueTable(data.assigned, 'Nothing assigned specifically to this worker (claims come from the shared queue).')}
        `;
    } catch (e) {
        body.innerHTML = `<div class="text-sm text-red-600 p-4">Failed to load worker queue: ${escapeHtml(String(e))}</div>`;
    }
}

window.showWorkerQueueModal = function showWorkerQueueModal(workerName) {
    const m = _queueModalShell('worker-queue-modal', `Queue — ${workerName}`, 'What this worker is running and what\u2019s explicitly routed to it');
    m.querySelector('#worker-queue-modal-body').id = 'worker-queue-modal-body';
    _refreshWorkerQueue(workerName);
    const intervalId = setInterval(() => {
        if (!document.getElementById('worker-queue-modal')) {
            clearInterval(intervalId);
            return;
        }
        _refreshWorkerQueue(workerName);
    }, 5000);
};


// Clean up interval when page is changed
export function cleanupWorkersPage() {
    if (refreshInterval) {
        clearInterval(refreshInterval);
        refreshInterval = null;
    }
}


// Gate 11 — translate a worker's architecture into how much work it can be
// given. Lives on the worker detail because that is where someone lands when a
// machine is online and idle; it was briefly built on the Docker Registry page,
// which is not in the sidebar and has no route, so nobody could ever have
// reached it.
async function loadWorkerArchReach(arch) {
    const host = document.getElementById('worker-detail-arch-reach');
    if (!host) return;
    const alias = { x86_64: 'amd64', x64: 'amd64', aarch64: 'arm64', armv8: 'arm64' };
    const a = alias[String(arch || '').toLowerCase()] || String(arch || '').toLowerCase() || 'amd64';
    try {
        const apiUrl = window.APP_CONFIG?.API_URL || '';
        const resp = await fetch(`${apiUrl}/platform/base-image-architectures`, {
            headers: { Authorization: `Bearer ${sessionStorage.getItem('accessToken')}` },
        });
        if (!resp.ok) return;                   // silent: this is a nicety, not the panel
        const d = await resp.json();
        const imgs = d.images || [];
        const usable = imgs.filter((i) => (i.architectures || []).includes(a)).length;
        const undet = d.undetermined || 0;
        const bad = usable === 0 && imgs.length > 0;
        host.innerHTML = `<div class="mt-2 text-xs ${bad
                ? 'text-amber-800 bg-amber-50 border border-amber-200'
                : 'text-gray-600 bg-gray-50 border border-gray-200'} rounded p-2">
            <strong>${usable} of ${imgs.length}</strong> base images are built for
            <code>${escapeHtml(a)}</code>, so this worker can be given work using ${usable === 0 ? 'none' : 'those'} of them.
            ${bad ? ' That is why it can look healthy and never pick anything up.' : ''}
            ${undet ? ` <span class="text-gray-500">(${undet} could not be determined — the registry is private, so those are assumed amd64.)</span>` : ''}
        </div>`;
    } catch (_) { /* nicety */ }
}


// Gate 7 — set what a worker costs to run.
//
// Asks for the WORKINGS rather than a bare rate: hardware price, how long you
// amortise it over, draw, and your electricity rate. The server derives
// cents/hour from those (capital/hr + power/hr) and stores both, so the figure
// can be re-derived when electricity moves instead of being a number nobody
// can account for a year later. Leave a pair blank to price on the other half
// alone — capital-only when power is someone else's problem, power-only for
// hardware already written off.
async function setWorkerCost(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    const hw = prompt(`${workerName} — hardware cost (whole currency units, blank to skip):`);
    if (hw === null) return;
    const months = hw ? prompt('Amortise over how many months? (e.g. 36)') : null;
    if (hw && months === null) return;
    const watts = prompt('Power draw in watts under load (blank to skip):');
    if (watts === null) return;
    const kwh = watts ? prompt('Electricity cost in cents per kWh (e.g. 15)') : null;
    if (watts && kwh === null) return;
    const margin = prompt('Profit margin % on top of cost (e.g. 20 to bill 1.2x). Blank uses the platform default:');
    if (margin === null) return;

    const body = {
        hardware_cost_cents: hw ? Math.round(parseFloat(hw) * 100) : null,
        amortisation_months: months ? parseInt(months, 10) : null,
        power_watts: watts ? parseInt(watts, 10) : null,
        electricity_cents_per_kwh: kwh ? parseInt(kwh, 10) : null,
        margin_percent: margin ? parseInt(margin, 10) : null,
    };

    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/cost`, {
            method: 'PATCH',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            alert(`Could not save cost (${resp.status})`);
            return;
        }
        const saved = await resp.json();
        const rate = saved.cost_per_hour_cents;
        const billedRate = saved.billed_per_hour_cents;
        alert(rate === null || rate === undefined
            ? 'Saved, but not enough detail to derive an hourly rate — runs on this worker stay unpriced.'
            : `${workerName}: costs $${(rate / 100).toFixed(2)}/hr, bills at ` +
              `$${(billedRate / 100).toFixed(2)}/hr (+${saved.effective_margin_percent}%).`);
        if (typeof loadWorkers === 'function') loadWorkers();
    } catch (e) {
        alert('Could not save cost: ' + (e && e.message ? e.message : e));
    }
}
window.setWorkerCost = setWorkerCost;


// Gate 7 — the manager's own cost, shown beside the workers it coordinates.
//
// It lives on the Workers page rather than buried in a pricing screen because
// this is where an operator is already thinking about what machines cost. The
// manager is the one box nobody bills for: it runs 24/7 whether or not any
// work happens, so without recovering it here its hardware earns nothing —
// and if every job lands on a remote worker, the machine that makes the
// platform exist is pure loss.
async function loadManagerCost() {
    if (!_isAdmin) return;
    // 2400LR-g3: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    let card = document.getElementById('manager-cost-card');
    if (!card) {
        const anchor = document.getElementById('license-summary-card')
            || document.querySelector('main, body');
        if (!anchor) return;
        card = document.createElement('div');
        card.id = 'manager-cost-card';
        card.className = 'mt-4 p-3 border rounded-md bg-card';
        anchor.parentNode?.insertBefore(card, anchor.nextSibling);
    }

    try {
        // clean-v2-api: manager-cost is now a field (manager_cost) on the
        // merged /pricing/config document — no more standalone fragment GET.
        const resp = await fetch(`${apiUrl}/pricing/config`, {
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!resp.ok) {
            card.innerHTML = `<div class="text-xs text-red-600">Manager overhead unavailable (${resp.status})</div>`;
            return;
        }
        const m = (await resp.json()).manager_cost;
        const rate = m.manager_cost_per_hour_cents;
        const workings = (m.manager_hardware_cost_cents && m.manager_amortisation_months)
            ? `$${(m.manager_hardware_cost_cents / 100).toLocaleString()} over ${m.manager_amortisation_months} months`
              + (m.manager_power_watts ? `, ${m.manager_power_watts}W @ ${m.manager_electricity_cents_per_kwh}c/kWh` : '')
            : 'no workings recorded';
        card.innerHTML = `
            <div class="font-semibold text-sm mb-1">Manager overhead</div>
            <div class="text-xs text-muted-foreground mb-2">
                The manager runs whether or not work does. Recovered per run-hour on top of
                whichever worker executed the job.
            </div>
            <div class="text-sm">
                ${rate === null || rate === undefined
                    ? '<span class="text-amber-600 font-medium">Not priced</span> — the manager\'s cost is not being recovered'
                    : `<span class="font-medium">$${(rate / 100).toFixed(2)}/hr</span>
                       <span class="text-xs text-muted-foreground">(${workings})</span>`}
                <a href="#" onclick="setManagerCost();return false;" class="ml-2 text-xs text-primary">Edit</a>
            </div>`;
    } catch (e) {
        card.innerHTML = `<div class="text-xs text-red-600">Manager overhead failed to load: ${escapeHtml(e?.message || String(e))}</div>`;
    }
}

async function setManagerCost() {
    // 2400LR-g3: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    const hw = prompt('Manager hardware cost (whole currency units):');
    if (hw === null) return;
    const months = prompt('Amortise over how many months? (e.g. 60)');
    if (months === null) return;
    const watts = prompt('Manager power draw in watts (blank to skip):');
    if (watts === null) return;
    const kwh = watts ? prompt('Electricity cost in cents per kWh (e.g. 15)') : null;
    if (watts && kwh === null) return;

    try {
        const resp = await fetch(`${apiUrl}/pricing/config`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                manager_cost: {
                    manager_hardware_cost_cents: hw ? Math.round(parseFloat(hw) * 100) : null,
                    manager_amortisation_months: months ? parseInt(months, 10) : null,
                    manager_power_watts: watts ? parseInt(watts, 10) : null,
                    manager_electricity_cents_per_kwh: kwh ? parseInt(kwh, 10) : null,
                },
            }),
        });
        if (!resp.ok) { alert(`Could not save manager cost (${resp.status})`); return; }
        const saved = (await resp.json()).manager_cost;
        const rate = saved.manager_cost_per_hour_cents;
        alert(rate === null || rate === undefined
            ? 'Saved, but not enough detail to derive an hourly rate — manager overhead stays unrecovered.'
            : `Manager overhead: $${(rate / 100).toFixed(2)}/hr.`);
        loadManagerCost();
    } catch (e) {
        alert('Could not save manager cost: ' + (e && e.message ? e.message : e));
    }
}
window.loadManagerCost = loadManagerCost;
window.setManagerCost = setManagerCost;


// Gate 7 — the platform's housekeeping, and whether clients are covering it.
//
// Calibration, benchmarks, system maintenance and E2E probes burn real hours on
// real hardware. They cannot be billed to whichever tenant happened to trigger
// them, but leaving them unrecovered means the operator eats a genuine cost of
// doing business. This shows both sides — what housekeeping costs, and what
// uplift would actually cover it — so the rate is chosen rather than guessed.
//
// The important state is CONFIGURED BELOW REQUIRED: that is money being lost
// every hour, and it has to look like a problem rather than a number.
async function loadOverheadRecovery() {
    if (!_isAdmin) return;
    // 2400LR-g3: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    let card = document.getElementById('overhead-recovery-card');
    if (!card) {
        const anchor = document.getElementById('manager-cost-card')
            || document.getElementById('license-summary-card')
            || document.querySelector('main, body');
        if (!anchor) return;
        card = document.createElement('div');
        card.id = 'overhead-recovery-card';
        card.className = 'mt-4 p-3 border rounded-md bg-card';
        anchor.parentNode?.insertBefore(card, anchor.nextSibling);
    }

    try {
        // clean-v2-api: overhead-recovery is now a field (overhead_recovery)
        // on the merged /pricing/config document — no standalone fragment GET.
        const resp = await fetch(`${apiUrl}/pricing/config`, {
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!resp.ok) {
            card.innerHTML = `<div class="text-xs text-red-600">Overhead recovery unavailable (${resp.status})</div>`;
            return;
        }
        const m = (await resp.json()).overhead_recovery;
        const req = m.required_overhead_percent;
        const cfg = m.configured_overhead_percent;
        const reqAttr = (req === null || req === undefined) ? 'null'
            : (Number.isFinite(Number(req)) ? String(Number(req)) : '0');
        const hk = (m.housekeeping_cost_cents / 100).toFixed(2);
        const bill = (m.billable_cost_cents / 100).toFixed(2);

        let verdict;
        if (req === null || req === undefined) {
            verdict = '<span class="text-muted-foreground">No billable work yet — nothing to recover against.</span>';
        } else if (cfg < req) {
            const shortfall = (((req - cfg) / 100) * m.billable_cost_cents / 100).toFixed(2);
            verdict = `<span class="text-red-600 font-medium">Set to ${cfg}%, needs ${req}%.</span>
                       <span class="text-xs text-muted-foreground">You are absorbing about $${shortfall} of your own costs.</span>`;
        } else {
            verdict = `<span class="text-green-700 font-medium">Set to ${cfg}%, needs ${req}%.</span>
                       <span class="text-xs text-muted-foreground">Housekeeping is covered.</span>`;
        }

        card.innerHTML = `
            <div class="font-semibold text-sm mb-1">Overhead recovery</div>
            <div class="text-xs text-muted-foreground mb-2">
                Calibration, benchmarks and maintenance run on your hardware but belong to no
                customer. This uplift recovers them inside the price of billable work, before margin.
            </div>
            <div class="text-sm mb-1">${verdict}</div>
            <div class="text-xs text-muted-foreground">
                Housekeeping ${m.housekeeping_hours}h = $${hk} &nbsp;·&nbsp;
                Billable ${m.billable_hours}h = $${bill}
                <a href="#" onclick="setOverheadRecovery(${reqAttr});return false;"
                   class="ml-2 text-primary">Edit</a>
            </div>`;
    } catch (e) {
        card.innerHTML = `<div class="text-xs text-red-600">Overhead recovery failed to load: ${escapeHtml(e?.message || String(e))}</div>`;
    }
}

async function setOverheadRecovery(required) {
    // 2400LR-g3-ce-denied-404s: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const suggestion = (required === null || required === undefined) ? '' : ` (measured: ${required})`;
    const pct = prompt(`Overhead uplift %% on billable work${suggestion}. Blank recovers nothing:`);
    if (pct === null) return;
    try {
        const resp = await fetch(`${apiUrl}/pricing/config`, {
            method: 'PUT',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                overhead_recovery: { platform_overhead_percent: pct ? parseInt(pct, 10) : null },
            }),
        });
        if (!resp.ok) { alert(`Could not save overhead rate (${resp.status})`); return; }
        const m = (await resp.json()).overhead_recovery;
        alert(`Overhead uplift set to ${m.configured_overhead_percent}%. Measured need: ${m.required_overhead_percent}%.`);
        loadOverheadRecovery();
    } catch (e) {
        alert('Could not save overhead rate: ' + (e && e.message ? e.message : e));
    }
}
window.loadOverheadRecovery = loadOverheadRecovery;
window.setOverheadRecovery = setOverheadRecovery;


// Gate 7 — the cost form on a worker's detail panel.
//
// Five related figures entered together, with the derived rate shown beside
// them, because the useful question is not "what is the wattage" but "what
// does this machine cost me per hour once I have said so".
function renderWorkerCostForm(workerName, w) {
    const v = (x) => (x === null || x === undefined ? '' : x);
    const money = (c) => (c === null || c === undefined ? '—' : '$' + (c / 100).toFixed(2) + '/hr');
    const id = (f) => `wc-${f}`;
    return `
    <div class="border-t pt-3 mt-3">
        <h4 class="font-semibold text-gray-700 text-sm mb-1">Cost basis</h4>
        <p class="text-xs text-gray-500 mb-3">
            What this machine costs YOU to run. Every job that lands here is priced from it,
            so an unpriced worker makes its runs look free.
        </p>
        <div class="grid grid-cols-2 gap-3 text-xs">
            <label class="block">
                <span class="text-gray-600">Hardware cost</span>
                <input id="${id('hw')}" type="number" min="0" step="1" value="${v(w.hardware_cost_cents !== null && w.hardware_cost_cents !== undefined ? Math.round(w.hardware_cost_cents / 100) : '')}"
                       class="mt-1 w-full border rounded px-2 py-1" placeholder="24000">
            </label>
            <label class="block">
                <span class="text-gray-600">Amortise over (months)</span>
                <input id="${id('months')}" type="number" min="1" step="1" value="${v(w.amortisation_months)}"
                       class="mt-1 w-full border rounded px-2 py-1" placeholder="60">
            </label>
            <label class="block">
                <span class="text-gray-600">Power draw (watts)</span>
                <input id="${id('watts')}" type="number" min="0" step="1" value="${v(w.power_watts)}"
                       class="mt-1 w-full border rounded px-2 py-1" placeholder="450">
            </label>
            <label class="block">
                <span class="text-gray-600">Electricity (cents/kWh)</span>
                <input id="${id('kwh')}" type="number" min="0" step="1" value="${v(w.electricity_cents_per_kwh)}"
                       class="mt-1 w-full border rounded px-2 py-1" placeholder="15">
            </label>
            <label class="block">
                <span class="text-gray-600">Profit margin (%)</span>
                <input id="${id('margin')}" type="number" min="0" step="1" value="${v(w.margin_percent)}"
                       class="mt-1 w-full border rounded px-2 py-1" placeholder="20 (default)">
            </label>
            <div class="flex flex-col justify-end">
                <div class="text-gray-600">Currently</div>
                <div id="${id('current')}" class="font-medium text-sm">
                    ${money(w.cost_per_hour_cents)} cost
                    ${w.billed_per_hour_cents !== null && w.billed_per_hour_cents !== undefined
                        ? `<span class="text-gray-400">&rarr; ${money(w.billed_per_hour_cents)} billed</span>` : ''}
                </div>
            </div>
        </div>
        <div class="mt-3 flex items-center gap-2">
            <button onclick="saveWorkerCost(${jsAttrStr(workerName)})"
                    class="px-3 py-1.5 text-xs rounded bg-primary text-white">Save cost</button>
            <span id="${id('status')}" class="text-xs text-gray-500"></span>
        </div>
    </div>`;
}

async function saveWorkerCost(workerName) {
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    const num = (elId) => {
        const el = document.getElementById(elId);
        const raw = el && el.value !== '' ? el.value : null;
        return raw === null ? null : parseInt(raw, 10);
    };
    const status = document.getElementById('wc-status');
    const hw = num('wc-hw');
    const body = {
        hardware_cost_cents: hw === null ? null : hw * 100,
        amortisation_months: num('wc-months'),
        power_watts: num('wc-watts'),
        electricity_cents_per_kwh: num('wc-kwh'),
        margin_percent: num('wc-margin'),
    };
    if (status) status.textContent = 'Saving…';
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/cost`, {
            method: 'PATCH',
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) {
            if (status) status.textContent = `Failed (${resp.status})`;
            return;
        }
        const saved = await resp.json();
        const cur = document.getElementById('wc-current');
        if (cur) {
            const c = saved.cost_per_hour_cents, b = saved.billed_per_hour_cents;
            cur.innerHTML = c === null || c === undefined
                ? '<span class="text-amber-600">Not priced</span>'
                : `$${(c / 100).toFixed(2)}/hr cost <span class="text-gray-400">&rarr; $${(b / 100).toFixed(2)}/hr billed (+${saved.effective_margin_percent}%)</span>`;
        }
        if (status) status.textContent = 'Saved';
        if (typeof loadWorkers === 'function') loadWorkers();
    } catch (e) {
        if (status) status.textContent = 'Failed: ' + (e?.message || e);
    }
}
window.saveWorkerCost = saveWorkerCost;


// 24.5.s2 — the desktop owner's lend/pause switch (the server-side twin of the
// worker's own tray control: POST /noah/mode on its loopback UI). Same three
// words, one verdict: this panel and the tray write the SAME pin, so "pause
// lending (I'm using it)" from the machine and from here cannot disagree.
async function renderNoahLendForm(workerName) {
    // 2400LR-cdu: noah_mode.py denied on CE (manifest:1255); fail-open on unknown edition.
    if (!await isPaidEdition()) return '';
    return `
    <div class="mt-4 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-sm font-semibold">Lend / pause this machine</h4>
        <span id="noah-lend-eff" class="text-xs text-gray-400">—</span>
      </div>
      <p class="text-xs text-muted-foreground">
        The idle-harvest rules decide when this machine can be lent. This switch overrides
        that verdict for a moment: <b>lend</b> = take work now even though it looks busy,
        <b>pause</b> = stop lending it (I'm using it), <b>automatic</b> = hand it back to the rules.
        Pause can be time-boxed ("until 18:00") — the pin releases itself when that time passes.
      </p>
      <div class="flex items-center gap-2 flex-wrap-wrap">
        <button onclick="setNoahLend(${jsAttrStr(workerName)}, 'lend')" class="px-2 py-1 text-xs rounded border bg-gray-100">Lend now</button>
        <button onclick="setNoahLend(${jsAttrStr(workerName)}, 'pause')" class="px-2 py-1 text-xs rounded border bg-gray-100">Pause lending</button>
        <button onclick="setNoahLend(${jsAttrStr(workerName)}, 'none')" class="px-2 py-1 text-xs rounded border bg-gray-100">Automatic</button>
      </div>
      <label class="flex items-center gap-2 mt-2 text-xs">Pause until (optional, e.g. 2026-10-08T18:00:00Z)
        <input id="noah-lend-until" class="border rounded px-1 py-0.5 bg-transparent" placeholder="leave blank = until I say otherwise"></label>
      <span id="noah-lend-status" class="text-xs text-gray-400"></span>
      <div id="noah-last-handback" data-testid="noah-last-handback" class="text-xs text-gray-400 mt-2"></div>
    </div>`;
}

// 24.5 follow-up — the worker's own last hand-back report (GET /noah/mode ->
// noah_handback). Relative, not absolute: the owner cares "when did it give my
// work back", not the timestamp. Blank when the worker has never reported one.
function noahRelativeTime(iso) {
    const ts = Date.parse(iso);
    if (!Number.isFinite(ts)) return String(iso);
    const diffSec = Math.floor((Date.now() - ts) / 1000);
    if (diffSec < 60) return 'just now';
    if (diffSec < 3600) {
        const m = Math.floor(diffSec / 60);
        return `${m} minute${m === 1 ? '' : 's'} ago`;
    }
    if (diffSec < 86400) {
        const h = Math.floor(diffSec / 3600);
        return `${h} hour${h === 1 ? '' : 's'} ago`;
    }
    const d = Math.floor(diffSec / 86400);
    return `${d} day${d === 1 ? '' : 's'} ago`;
}

async function loadNoahLend(workerName) {
    // 2400LR-cdu: noah_mode.py denied on CE (manifest:1255); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    const el = document.getElementById('noah-lend-eff');
    if (!el) return;
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/noah/mode`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok) { el.textContent = 'lend/pause unavailable'; return; }
        const d = await resp.json();
        const mode = d.mode || 'none';
        const label = mode === 'lend' ? 'LEND (forced available)'
            : mode === 'pause' ? 'PAUSED (not being lent)' : 'automatic';
        const until = d.until ? ` until ${String(d.until).replace('T', ' ').replace('Z', ' UTC')}` : '';
        el.textContent = `${label}${until}`;
        const hbEl = document.getElementById('noah-last-handback');
        if (hbEl) {
            const hb = d.noah_handback;
            hbEl.innerHTML = (hb && hb.at)
                ? `Last hand-back: ${escapeHtml(noahRelativeTime(hb.at))} — ` +
                  `${escapeHtml(Number(hb.tasks_requeued || 0))} task(s) requeued`
                : '';
        }
    } catch (e) { /* non-fatal */ }
}

async function setNoahLend(workerName, mode) {
    // 2400LR-cdu: noah_mode.py denied on CE (manifest:1255); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    const status = document.getElementById('noah-lend-status');
    const untilEl = document.getElementById('noah-lend-until');
    const until = (mode === 'pause' && untilEl && untilEl.value) ? untilEl.value : null;
    if (status) status.textContent = 'Saving…';
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/noah/mode`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode, until }),
        });
        if (status) status.textContent = resp.ok ? 'Saved' : `Failed (${resp.status})`;
        if (resp.ok) loadNoahLend(workerName);
    } catch (e) { if (status) status.textContent = 'Failed: ' + (e?.message || e); }
}

// NOAH opportunistic idle-harvest — per-worker settings. Real form (override
// per-worker, or inherit the platform default), not a prompt() chain.
function renderNoahSettingsForm(workerName) {
    return `
    <div class="mt-4 rounded-lg border border-gray-200 dark:border-gray-700 p-3">
      <div class="flex items-center justify-between mb-2">
        <h4 class="text-sm font-semibold">NOAH idle-harvest</h4>
        <span id="noah-eff" class="text-xs text-gray-400">—</span>
      </div>
      <div class="grid grid-cols-2 gap-2 text-sm">
        <label class="flex items-center gap-2"><input type="checkbox" id="noah-enabled"> Enabled</label>
        <label class="flex items-center gap-2"><input type="checkbox" id="noah-plugged"> Plugged-in only</label>
        <label class="flex flex-col">Schedule start<input id="noah-start" placeholder="HH:MM" class="border rounded px-1 py-0.5 bg-transparent"></label>
        <label class="flex flex-col">Schedule end<input id="noah-end" placeholder="HH:MM" class="border rounded px-1 py-0.5 bg-transparent"></label>
        <label class="flex flex-col">Sustained quiet (s)<input id="noah-quiet" type="number" min="0" class="border rounded px-1 py-0.5 bg-transparent"></label>
        <label class="flex flex-col">Pause grace (s)<input id="noah-grace" type="number" min="0" class="border rounded px-1 py-0.5 bg-transparent"></label>
        <label class="flex flex-col">Batch appetite<input id="noah-appetite" type="number" min="0" class="border rounded px-1 py-0.5 bg-transparent"></label>
      </div>
      <div class="flex items-center gap-2 mt-2">
        <button onclick="saveNoahSettings(${jsAttrStr(workerName)})" class="px-2 py-1 text-xs rounded bg-blue-600 text-white">Save override</button>
        <button onclick="inheritNoahSettings(${jsAttrStr(workerName)})" class="px-2 py-1 text-xs rounded border">Inherit default</button>
        <span id="noah-status" class="text-xs text-gray-400"></span>
      </div>
    </div>`;
}

async function loadNoahSettings(workerName) {
    // 2400LR-g3-ce-denied-404s: route ships in CE (api_allow manifest:94) but CORE/API/services/worker_management_service.py:578 require_paid("services.noah") -> 402 (noah.py denied manifest:1570); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        document.getElementById('noah-status')?.classList.add('hidden');
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/noah`, {
            headers: { Authorization: `Bearer ${token}` }
        });
        if (!resp.ok) return;
        const d = await resp.json();
        const ov = d.overrides || {}, eff = d.effective || {};
        const val = (k) => (ov[k] === null || ov[k] === undefined ? '' : ov[k]);
        const setV = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
        const setC = (id, v) => { const el = document.getElementById(id); if (el) el.checked = !!v; };
        setC('noah-enabled', ov.enabled);
        setC('noah-plugged', ov.plugged_in_only);
        setV('noah-start', val('schedule_start'));
        setV('noah-end', val('schedule_end'));
        setV('noah-quiet', val('sustained_quiet_s'));
        setV('noah-grace', val('pause_grace_s'));
        setV('noah-appetite', val('batch_appetite'));
        const effEl = document.getElementById('noah-eff');
        if (effEl) {
            const availTxt = d.available === null ? '' : (d.available ? ' · available' : ` · unavailable (${d.reason || ''})`);
            effEl.textContent = `effective: ${eff.enabled ? 'on' : 'off'}${availTxt}`;
        }
    } catch (e) { /* non-fatal */ }
}

async function saveNoahSettings(workerName) {
    // 2400LR-g3-ce-denied-404s: route ships in CE (api_allow manifest:94) but CORE/API/services/worker_management_service.py:597 require_paid("services.noah") -> 402 (noah.py denied manifest:1570); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        document.getElementById('noah-status')?.classList.add('hidden');
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    const status = document.getElementById('noah-status');
    const numOrNull = (id) => { const el = document.getElementById(id); return el && el.value !== '' ? parseInt(el.value, 10) : null; };
    const strOrNull = (id) => { const el = document.getElementById(id); return el && el.value !== '' ? el.value : null; };
    const body = {
        inherit: false,
        enabled: document.getElementById('noah-enabled')?.checked ?? null,
        plugged_in_only: document.getElementById('noah-plugged')?.checked ?? null,
        schedule_start: strOrNull('noah-start'),
        schedule_end: strOrNull('noah-end'),
        sustained_quiet_s: numOrNull('noah-quiet'),
        pause_grace_s: numOrNull('noah-grace'),
        batch_appetite: numOrNull('noah-appetite'),
    };
    if (status) status.textContent = 'Saving…';
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/noah`, {
            method: 'PATCH',
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (status) status.textContent = resp.ok ? 'Saved' : `Failed (${resp.status})`;
        if (resp.ok) loadNoahSettings(workerName);
    } catch (e) { if (status) status.textContent = 'Failed: ' + (e?.message || e); }
}

async function inheritNoahSettings(workerName) {
    // 2400LR-g3-ce-denied-404s: route ships in CE (api_allow manifest:94) but CORE/API/services/worker_management_service.py:597 require_paid("services.noah") -> 402 (noah.py denied manifest:1570); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        document.getElementById('noah-status')?.classList.add('hidden');
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem('accessToken');
    const status = document.getElementById('noah-status');
    if (status) status.textContent = 'Resetting…';
    try {
        const resp = await fetch(`${apiUrl}/workers/${encodeURIComponent(workerName)}/noah`, {
            method: 'PATCH',
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: JSON.stringify({ inherit: true }),
        });
        if (status) status.textContent = resp.ok ? 'Inheriting default' : `Failed (${resp.status})`;
        if (resp.ok) loadNoahSettings(workerName);
    } catch (e) { if (status) status.textContent = 'Failed: ' + (e?.message || e); }
}
window.saveNoahSettings = saveNoahSettings;
window.inheritNoahSettings = inheritNoahSettings;


// Gate 7 — fixed infrastructure that no single worker accounts for.
//
// Per-worker cost prices the machines that RUN tasks; the manager gets its own
// line. Everything else that has to exist for either to work — the registry
// host, switches, the UPS, rack space, the transit bill — had nowhere to live
// and was absorbed silently. Two shapes, because infrastructure comes in two:
// CAPITAL is amortised and draws power, RECURRING is a monthly invoice.
//
// Not egress: that is usage-based and metered per tenant from MinIO already.
async function loadInfrastructureCosts() {
    if (!_isAdmin) return;
    // 2400LR-g3: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    if (!await isPaidEdition()) return;
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");

    let card = document.getElementById('infra-cost-card');
    if (!card) {
        const anchor = document.getElementById('overhead-recovery-card')
            || document.getElementById('manager-cost-card')
            || document.getElementById('license-summary-card')
            || document.querySelector('main, body');
        if (!anchor) return;
        card = document.createElement('div');
        card.id = 'infra-cost-card';
        card.className = 'mt-4 p-3 border rounded-md bg-card';
        anchor.parentNode?.insertBefore(card, anchor.nextSibling);
    }

    try {
        const resp = await fetch(`${apiUrl}/pricing/config/infrastructure-costs`, {
            headers: { 'Authorization': `Bearer ${token}` },
        });
        if (!resp.ok) {
            card.innerHTML = `<div class="text-xs text-red-600">Infrastructure costs unavailable (${resp.status})</div>`;
            return;
        }
        const data = await resp.json();
        const rows = (data.items || []).map(i => {
            const basis = i.kind === 'recurring'
                ? `$${((i.monthly_cents || 0) / 100).toFixed(2)}/month`
                : `$${((i.hardware_cost_cents || 0) / 100).toLocaleString()} over ${i.amortisation_months || '?'} mo`
                  + (i.power_watts ? `, ${i.power_watts}W` : '');
            const rate = i.cost_per_hour_cents === null || i.cost_per_hour_cents === undefined
                ? '<span class="text-amber-600">not priced</span>'
                : `$${(i.cost_per_hour_cents / 100).toFixed(2)}/hr`;
            return `<tr class="${i.is_active ? '' : 'opacity-40'}">
                <td class="py-1 pr-3">${i.name}</td>
                <td class="py-1 pr-3 text-gray-500">${i.kind}</td>
                <td class="py-1 pr-3 text-gray-500">${basis}</td>
                <td class="py-1 pr-3 font-medium">${rate}</td>
                <td class="py-1"><a href="#" class="text-red-600" onclick="deleteInfraCost(${i.id});return false;">remove</a></td>
            </tr>`;
        }).join('');

        card.innerHTML = `
            <div class="font-semibold text-sm mb-1">Infrastructure</div>
            <div class="text-xs text-muted-foreground mb-2">
                Fixed costs no single worker accounts for — the registry host, switches, the UPS,
                rack space, the transit line. Recovered per run-hour alongside the manager.
                Egress is metered separately, per tenant.
            </div>
            ${rows ? `<table class="text-xs w-full mb-2"><tbody>${rows}</tbody></table>`
                   : '<div class="text-xs text-amber-600 mb-2">Nothing recorded — supporting infrastructure is being absorbed.</div>'}
            <div class="text-sm">
                Total <span class="font-medium">$${((data.total_cost_per_hour_cents || 0) / 100).toFixed(2)}/hr</span>
                <a href="#" onclick="addInfraCost();return false;" class="ml-2 text-xs text-primary">Add</a>
            </div>`;
    } catch (e) {
        card.innerHTML = `<div class="text-xs text-red-600">Infrastructure costs failed to load: ${escapeHtml(e?.message || String(e))}</div>`;
    }
}

async function addInfraCost() {
    const name = prompt('What is it? (e.g. Harbor registry host, Internet line, Rack space)');
    if (!name) return;
    const kind = (prompt('Type: "capital" for hardware you bought, "recurring" for a monthly bill', 'capital') || '').trim();
    if (!kind) return;
    const body = { name, kind, is_active: true };
    if (kind === 'recurring') {
        const monthly = prompt('Monthly cost (whole currency units):');
        if (monthly === null) return;
        body.monthly_cents = monthly ? Math.round(parseFloat(monthly) * 100) : null;
    } else {
        const hw = prompt('Purchase cost (whole currency units):');
        if (hw === null) return;
        const months = prompt('Amortise over how many months?', '60');
        if (months === null) return;
        const watts = prompt('Power draw in watts (blank to skip):');
        if (watts === null) return;
        const kwh = watts ? prompt('Electricity cents per kWh', '15') : null;
        body.hardware_cost_cents = hw ? Math.round(parseFloat(hw) * 100) : null;
        body.amortisation_months = months ? parseInt(months, 10) : null;
        body.power_watts = watts ? parseInt(watts, 10) : null;
        body.electricity_cents_per_kwh = kwh ? parseInt(kwh, 10) : null;
    }
    await _infraWrite('POST', '', body);
}

async function deleteInfraCost(id) {
    if (!confirm('Remove this infrastructure cost? Its share stops being recovered.')) return;
    await _infraWrite('DELETE', `/${id}`, null);
}

async function _infraWrite(method, suffix, body) {
    // 2400LR-g3-ce-denied-404s: pricing.py denied on CE (manifest:1452); fail-open on unknown edition.
    let _ceDenied = false; try { _ceDenied = !(await isPaidEdition()); } catch (_) { /* unknown edition -> paid */ }
    if (_ceDenied) {
        return;
    }
    const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
    const token = sessionStorage.getItem("accessToken");
    try {
        const resp = await fetch(`${apiUrl}/pricing/config/infrastructure-costs${suffix}`, {
            method,
            headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : undefined,
        });
        if (!resp.ok) { alert(`Failed (${resp.status})`); return; }
        loadInfrastructureCosts();
    } catch (e) {
        alert('Failed: ' + (e?.message || e));
    }
}
window.loadInfrastructureCosts = loadInfrastructureCosts;
window.addInfraCost = addInfraCost;
window.deleteInfraCost = deleteInfraCost;
