// Dashboard/js/quick-stats-widget.js
//
// home-page-finalize §1 — replaces the placeholder "Quick Stats" card on the
// home page with role-tiered KPI tiles. Pure client-side composition over
// existing endpoints (no new server endpoints — see design.md D1).
//
// Role tiers:
//   - Standard user (non-parent under a tenant admin): 2 own-only tiles
//     (My runs 30d, My files)
//   - Tenant admin (parent user, not platform admin): 4 tenant-wide tiles
//     (Total workflows, Successful runs 30d, Total files, Active triggers)
//   - Platform admin: tenant-wide tiles + 4 platform-wide (Total tenants,
//     Active users, Workers, Platform runs 30d)

const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';
const COUNT_FETCH_LIMIT = 500;
let _refreshInFlight = false;

export async function renderQuickStatsWidget(containerId) {
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

    // Role tiering — flag fields:
    //   - is_platform_admin && tenant_id === 1: platform admin (matches the
    //     API-side gate; `tenant_id == 1` is the "Platform" tenant)
    //   - parent_user_id == null && !is_platform_admin: tenant admin
    //   - parent_user_id != null: standard user (under a tenant admin)
    const isPlatformAdmin =
        me && me.is_platform_admin === true && me.tenant_id === 1;
    const isStandardUser = me && me.parent_user_id != null;

    const headers = { Authorization: `Bearer ${token}` };

    let tiles;
    if (isStandardUser) {
        tiles = await fetchStandardUserTiles(headers, me.id);
    } else {
        tiles = await fetchTenantAdminTiles(headers);
    }

    const adminTiles = isPlatformAdmin ? await fetchPlatformAdminTiles(headers) : [];

    container.innerHTML = renderCard({
        tiles,
        adminTiles,
        isPlatformAdmin,
        isStandardUser,
    });
    attachRefreshHandler(containerId);
}

// --- Tile data fetchers ----------------------------------------------------

// Standard user: minimal — only their own activity.
async function fetchStandardUserTiles(headers, myUserId) {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;

    const [myRunsCount, myFilesCount] = await Promise.all([
        countFromList(
            `${apiUrl}/road/runs?status_filter=completed&include_details=false&limit=${COUNT_FETCH_LIMIT}`,
            headers,
            (rows) =>
                rows.filter((r) => {
                    const ts = r.completed_at || r.started_at;
                    return (
                        r.user_id === myUserId &&
                        ts &&
                        new Date(ts).getTime() >= thirtyDaysAgo
                    );
                }),
        ),
        countFromList(
            `${apiUrl}/assets/?all_items=true&limit=${COUNT_FETCH_LIMIT}`,
            headers,
            (rows) =>
                rows.filter(
                    (c) => c.cog_type === 'file' && c.owner_id === myUserId,
                ),
        ),
    ]);

    return [
        { label: 'My runs (30d)', value: myRunsCount, color: 'green' },
        { label: 'My files', value: myFilesCount, color: 'orange' },
    ];
}

// Tenant admin: tenant-wide totals.
async function fetchTenantAdminTiles(headers) {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;

    const [workflowsCount, recentRunsCount, filesCount, activeTriggersCount] =
        await Promise.all([
            countFromList(`${apiUrl}/road/?limit=${COUNT_FETCH_LIMIT}`, headers),
            countFromList(
                `${apiUrl}/road/runs?status_filter=completed&include_details=false&limit=${COUNT_FETCH_LIMIT}`,
                headers,
                (rows) =>
                    rows.filter((r) => {
                        const ts = r.completed_at || r.started_at;
                        return ts && new Date(ts).getTime() >= thirtyDaysAgo;
                    }),
            ),
            countFromList(
                `${apiUrl}/assets/?all_items=true&limit=${COUNT_FETCH_LIMIT}`,
                headers,
                (rows) => rows.filter((c) => c.cog_type === 'file'),
            ),
            countFromList(
                `${apiUrl}/road/triggers/?limit=${COUNT_FETCH_LIMIT}`,
                headers,
                (rows) => rows.filter((t) => t.is_active !== false),
            ),
        ]);

    return [
        { label: 'Total workflows', value: workflowsCount, color: 'brand' },
        { label: 'Successful runs (30d)', value: recentRunsCount, color: 'green' },
        { label: 'Total files', value: filesCount, color: 'orange' },
        { label: 'Active triggers', value: activeTriggersCount, color: 'purple' },
    ];
}

// Platform admin: extra platform-wide tiles on top of the tenant tier.
async function fetchPlatformAdminTiles(headers) {
    const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;

    const [tenantsCount, activeUsersCount, workersCount, platformRunsCount] =
        await Promise.all([
            countFromList(`${apiUrl}/tenants`, headers),
            countFromList(
                `${apiUrl}/users/?limit=${COUNT_FETCH_LIMIT}`,
                headers,
                (rows) =>
                    rows.filter(
                        (u) => u.status === 'active' || u.status === 'ACTIVE',
                    ),
            ),
            countFromList(`${apiUrl}/workers`, headers),
            countFromList(
                `${apiUrl}/road/runs?include_details=false&limit=${COUNT_FETCH_LIMIT}`,
                headers,
                (rows) =>
                    rows.filter((r) => {
                        const ts = r.completed_at || r.started_at;
                        return ts && new Date(ts).getTime() >= thirtyDaysAgo;
                    }),
            ),
        ]);

    return [
        { label: 'Total tenants', value: tenantsCount, color: 'brand' },
        { label: 'Active users', value: activeUsersCount, color: 'green' },
        { label: 'Workers', value: workersCount, color: 'orange' },
        { label: 'Platform runs (30d)', value: platformRunsCount, color: 'purple' },
    ];
}

// --- Helpers ---------------------------------------------------------------

async function countFromList(url, headers, postFilter) {
    try {
        const resp = await fetch(url, { headers });
        if (!resp.ok) return '–';
        const rows = await resp.json();
        if (!Array.isArray(rows)) return '–';
        const filtered = postFilter ? postFilter(rows) : rows;
        if (rows.length >= COUNT_FETCH_LIMIT) {
            return `${COUNT_FETCH_LIMIT}+`;
        }
        return filtered.length.toLocaleString();
    } catch {
        return '–';
    }
}

// --- Render ----------------------------------------------------------------

function renderSkeleton() {
    return `
        <div class="bg-white p-6 rounded-xl shadow-md">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">Quick Stats</h2>
            </div>
            <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                ${[0, 1, 2, 3]
                    .map(
                        () => `
                    <div class="bg-gray-50 p-4 rounded-lg animate-pulse">
                        <div class="h-3 bg-gray-200 rounded w-2/3 mb-2"></div>
                        <div class="h-6 bg-gray-200 rounded w-1/2"></div>
                    </div>`,
                    )
                    .join('')}
            </div>
        </div>
    `;
}

function renderCard({ tiles, adminTiles, isPlatformAdmin, isStandardUser }) {
    const tileCount = tiles.length;
    const baseGridCols =
        tileCount <= 2 ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-2 md:grid-cols-4';

    const tilesHtml = tiles
        .map(
            (t) => `
        <div class="bg-gray-50 p-4 rounded-lg border-l-4 border-${t.color}-500">
            <p class="text-xs text-gray-600 mb-1">${escapeHtml(t.label)}</p>
            <p class="text-2xl font-bold text-gray-900">${escapeHtml(String(t.value))}</p>
        </div>`,
        )
        .join('');

    const adminSectionHtml =
        isPlatformAdmin && adminTiles.length > 0
            ? `<div class="mt-4 pt-4 border-t border-gray-200">
                <p class="text-xs uppercase tracking-wide text-gray-500 mb-3">Platform-wide</p>
                <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
                    ${adminTiles
                        .map(
                            (t) => `
                        <div class="bg-gray-50 p-4 rounded-lg border-l-4 border-${t.color}-500">
                            <p class="text-xs text-gray-600 mb-1">${escapeHtml(t.label)}</p>
                            <p class="text-2xl font-bold text-gray-900">${escapeHtml(String(t.value))}</p>
                        </div>`,
                        )
                        .join('')}
                </div>
            </div>`
            : '';

    const titleSuffix = isStandardUser
        ? '<span class="text-xs text-gray-400 ml-2">your activity</span>'
        : '';

    return `
        <div class="bg-white p-6 rounded-xl shadow-md">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-gray-700">Quick Stats${titleSuffix}</h2>
                <button id="quick-stats-refresh-btn"
                    class="text-gray-400 hover:text-primary/80 transition-colors p-1 rounded"
                    title="Refresh">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
                            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
                    </svg>
                </button>
            </div>
            <div class="grid ${baseGridCols} gap-4">
                ${tilesHtml}
            </div>
            ${adminSectionHtml}
        </div>
    `;
}

function attachRefreshHandler(containerId) {
    const btn = document.getElementById('quick-stats-refresh-btn');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        if (_refreshInFlight) return;
        _refreshInFlight = true;
        btn.disabled = true;
        try {
            await renderQuickStatsWidget(containerId);
        } finally {
            _refreshInFlight = false;
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
