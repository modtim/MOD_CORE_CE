// Dashboard/js/recent-activity-widget.js
//
// home-page-finalize §2 — replaces the placeholder "Recent Activity" card on
// the home page with a live, role-scoped event feed composed CLIENT-SIDE from
// existing endpoints (no new server aggregator — see design.md D1).

// GY.C7 — visible data-label markings (shared, CSP-clean chip + colour map).
import { dataLabelChip } from './data-label-chip.js';
import { isPaidEdition } from './api.js';

const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
const PER_SOURCE_LIMIT = 10;
const HARD_CAP_ROWS = 20;
let _refreshInFlight = false;

export async function renderRecentActivityWidget(containerId) {
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

    // Platform admin gate matches the API-side `tenant_id == 1` convention
    // (tenant 1 is the "Platform" tenant; platform admins must live there
    // to access /platform/* endpoints).
    const isPlatformAdmin =
        me && me.is_platform_admin === true && me.tenant_id === 1;
    // Standard users have parent_user_id !== null. Tenant admins are the
    // root of their tenant tree and have parent_user_id == null.
    const isParent = me && me.parent_user_id == null;
    const isStandardUser = me && me.parent_user_id != null;
    const myUserId = me && me.id;

    const headers = { Authorization: `Bearer ${token}` };

    // Standard users see ONLY their own activity — runs they triggered + files
    // they own. The `ownOnly` parameter triggers the per-user filter inside
    // each fetcher.
    const sourcePromises = [
        fetchRunRows(headers, isStandardUser ? myUserId : null),
        fetchUploadRows(headers, isStandardUser ? myUserId : null),
    ];
    // Standard users (non-parent) lack capability to read /users/invitations/ — skip
    // to avoid the 403 noise (per spec scenario "Standard user sees runs and
    // uploads only"). Tenant admins see tenant-wide invitations.
    if (isParent) {
        sourcePromises.push(fetchInvitationRows(headers));
    }
    if (isPlatformAdmin) {
        sourcePromises.push(fetchTenantSignupRows(headers));
        // 2400UI15 — routers/platform_license.py is CE-denied, so the
        // license feed row would 404 on every render; drop the source.
        if (await isPaidEdition()) sourcePromises.push(fetchLicenseEventRow(headers));
    }

    const results = await Promise.allSettled(sourcePromises);
    const rows = results.flatMap((r) => (r.status === 'fulfilled' ? r.value : []));

    rows.sort((a, b) => (b.tsMs || 0) - (a.tsMs || 0));
    const capped = rows.slice(0, HARD_CAP_ROWS);

    container.innerHTML = renderCard(capped);
    attachRefreshHandler(containerId);
}

// --- Per-source fetchers (return ActivityRow[]) ----------------------------

async function fetchRunRows(headers, ownOnlyUserId) {
    // Fetch a wider window when filtering own-only so we still surface
    // enough rows after the client-side filter strips other users' runs.
    const limit = ownOnlyUserId ? 50 : PER_SOURCE_LIMIT;
    const resp = await fetch(
        `${apiUrl}/road/runs?limit=${limit}`,
        { headers },
    );
    if (!resp.ok) return [];
    const rows = await resp.json();
    if (!Array.isArray(rows)) return [];
    const filtered = ownOnlyUserId
        ? rows.filter((r) => r.user_id === ownOnlyUserId)
        : rows;
    return filtered.slice(0, PER_SOURCE_LIMIT).map((r) => {
        const ts = r.completed_at || r.started_at;
        return {
            kind: 'run',
            tsMs: ts ? new Date(ts).getTime() : 0,
            tsIso: ts || null,
            title: r.run_name || r.workflow_name || `Run #${r.id}`,
            subtitle: r.status || 'unknown',
            statusKind: statusBadgeColor(r.status),
            href: `#workflow-runs/${r.id}`,
            // GY.C7 — effective data-classification label (server-derived).
            label: r.data_classification || null,
        };
    });
}

async function fetchUploadRows(headers, ownOnlyUserId) {
    const limit = ownOnlyUserId ? 50 : PER_SOURCE_LIMIT;
    const resp = await fetch(
        `${apiUrl}/assets/?all_items=true&sort_by=created_at&order=desc&limit=${limit}`,
        { headers },
    );
    if (!resp.ok) return [];
    const rows = await resp.json();
    if (!Array.isArray(rows)) return [];
    return rows
        .filter((c) => c.cog_type === 'file')
        .filter((c) => !ownOnlyUserId || c.owner_id === ownOnlyUserId)
        .slice(0, PER_SOURCE_LIMIT)
        .map((c) => ({
            kind: 'upload',
            tsMs: c.created_at ? new Date(c.created_at).getTime() : 0,
            tsIso: c.created_at || null,
            title: c.name || `File #${c.id}`,
            subtitle: c.size_bytes ? formatBytes(c.size_bytes) : 'file',
            href: `#data-files/${c.parent_id || ''}?focus=${c.id}`,
            // GY.C7 — the asset's own data-classification label.
            label: c.data_classification || null,
        }));
}

async function fetchInvitationRows(headers) {
    // clean-v2-api identity wave: /invitations moved to /users/invitations.
    const resp = await fetch(`${apiUrl}/users/invitations/?limit=${PER_SOURCE_LIMIT}`, {
        headers,
    });
    if (!resp.ok) return [];
    const rows = await resp.json();
    if (!Array.isArray(rows)) return [];
    return rows.map((inv) => ({
        kind: 'invitation',
        tsMs: inv.created_at ? new Date(inv.created_at).getTime() : 0,
        tsIso: inv.created_at || null,
        title: inv.email || `Invitation #${inv.id}`,
        subtitle: 'pending invitation',
        href: `#users-invitations`,
    }));
}

async function fetchTenantSignupRows(headers) {
    const resp = await fetch(
        `${apiUrl}/tenants?sort_by=created_at&order=desc&limit=${PER_SOURCE_LIMIT}`,
        { headers },
    );
    if (!resp.ok) return [];
    const rows = await resp.json();
    if (!Array.isArray(rows)) return [];
    const paid = await isPaidEdition(); // 2400LR-pg3: #payments-admin is CE-gated; fail-open
    return rows.map((t) => ({
        kind: 'tenant',
        tsMs: t.created_at ? new Date(t.created_at).getTime() : 0,
        tsIso: t.created_at || null,
        title: t.name || `Tenant #${t.id}`,
        subtitle: `${formatBytes(t.storage_used_bytes || 0)} used`,
        href: paid ? `#payments-admin?tenant=${t.id}` : '#home',
    }));
}

async function fetchLicenseEventRow(headers) {
    const resp = await fetch(`${apiUrl}/platform/license/status`, { headers });
    if (!resp.ok) return [];
    const data = await resp.json();
    const lic = data && data.license;
    if (!lic) return [];
    // Surface the most recent state-defining moment as a single feed row.
    // Prefer expiry-related dates (most common state changes); fall back
    // to "Validated" timestamp if present.
    const ts = lic.expires_at || lic.hard_cutoff_at || null;
    if (!ts) return [];
    let title = 'License';
    if (!lic.valid) title = 'Platform status';
    else if (lic.in_grace_period) title = 'Platform status';
    else if (lic.days_until_expiry != null && lic.days_until_expiry <= 14)
        title = 'Platform status';
    else title = `Platform status ${new Date(ts).toLocaleDateString()}`;
    return [
        {
            kind: 'license',
            tsMs: new Date(ts).getTime(),
            tsIso: ts,
            title,
            subtitle: lic.license_type || 'license',
            href: '#home',
        },
    ];
}

// --- Render ----------------------------------------------------------------

function renderSkeleton() {
    return `
        <div class="bg-white p-6 rounded-xl shadow-md h-full">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">Recent Activity</h2>
            </div>
            <div class="space-y-3">
                ${[0, 1, 2, 3]
                    .map(
                        () => `
                    <div class="flex items-center gap-3 animate-pulse">
                        <div class="w-8 h-8 bg-gray-200 rounded-full"></div>
                        <div class="flex-1">
                            <div class="h-3 bg-gray-200 rounded w-3/4 mb-1"></div>
                            <div class="h-2 bg-gray-200 rounded w-1/2"></div>
                        </div>
                    </div>`,
                    )
                    .join('')}
            </div>
        </div>
    `;
}

function renderCard(rows) {
    const body =
        rows.length === 0
            ? `<p class="text-sm text-gray-500 italic">No recent activity yet.</p>`
            : `<ul class="divide-y divide-gray-100">
                ${rows.map(renderRow).join('')}
            </ul>`;

    return `
        <div class="bg-white p-6 rounded-xl shadow-md h-full">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">Recent Activity</h2>
                <button id="recent-activity-refresh-btn"
                    class="text-gray-400 hover:text-primary/80 transition-colors p-1 rounded"
                    title="Refresh">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                    </svg>
                </button>
            </div>
            ${body}
        </div>
    `;
}

function renderRow(row) {
    const icon = kindIcon(row.kind);
    const subtitleHtml =
        row.kind === 'run'
            ? `<span class="text-xs px-2 py-0.5 rounded-full ${statusBadgeClasses(row.statusKind)}">${escapeHtml(row.subtitle)}</span>`
            : `<span class="text-xs text-gray-500">${escapeHtml(row.subtitle)}</span>`;
    // GY.C7 — data-label chip next to the subtitle (public/internal render
    // subtly; controlled labels loudly). '' when nothing to show.
    const labelHtml = row.label ? dataLabelChip(row.label, { subtleDefault: false }) : '';

    return `
        <li>
            <a href="${escapeHtml(row.href)}"
               class="flex items-center gap-3 py-2 hover:bg-gray-50 rounded px-2 -mx-2 transition-colors">
                <div class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-100 flex items-center justify-center text-gray-600">
                    ${icon}
                </div>
                <div class="flex-1 min-w-0">
                    <p class="text-sm font-medium text-gray-900 truncate">${escapeHtml(row.title)}</p>
                    <div class="flex items-center gap-2 mt-0.5">
                        ${subtitleHtml}
                        ${labelHtml ? `<span class="shrink-0">${labelHtml}</span>` : ''}
                        <span class="text-xs text-gray-400">·</span>
                        <span class="text-xs text-gray-400">${relativeTime(row.tsMs)}</span>
                    </div>
                </div>
            </a>
        </li>
    `;
}

function attachRefreshHandler(containerId) {
    const btn = document.getElementById('recent-activity-refresh-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        if (_refreshInFlight) return;
        _refreshInFlight = true;
        btn.disabled = true;
        try {
            await renderRecentActivityWidget(containerId);
        } finally {
            _refreshInFlight = false;
        }
    });
}

// --- Helpers ---------------------------------------------------------------

function kindIcon(kind) {
    const svgs = {
        run: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
        upload: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/></svg>',
        invitation: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>',
        tenant: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>',
        license: '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>',
    };
    return svgs[kind] || svgs.run;
}

function statusBadgeColor(status) {
    if (!status) return 'gray';
    const s = String(status).toLowerCase();
    if (s.includes('complete') || s.includes('success')) return 'green';
    if (s.includes('fail') || s.includes('error') || s.includes('cancel'))
        return 'red';
    if (s.includes('queue') || s.includes('pending')) return 'yellow';
    if (s.includes('run')) return 'brand';
    return 'gray';
}

function statusBadgeClasses(kind) {
    const map = {
        green: 'bg-green-100 text-green-800',
        red: 'bg-red-100 text-red-900',
        yellow: 'bg-yellow-100 text-yellow-800',
        brand: 'bg-primary/20 text-primary',
        gray: 'bg-gray-100 text-gray-800',
    };
    return map[kind] || map.gray;
}

function relativeTime(tsMs) {
    if (!tsMs) return 'unknown';
    const diffSec = Math.floor((Date.now() - tsMs) / 1000);
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

function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function escapeHtml(s) {
    return String(s)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}
