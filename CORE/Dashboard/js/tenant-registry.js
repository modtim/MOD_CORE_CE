// Tenant Registry tab — per-tenant view of tenant Harbor images + scans
// + storage quota. Pulls data from the API's /tenant-harbor/* endpoints
// (which proxy Harbor and always scope to the current user's tenant).
//
// Wired into the Dashboard router in ui.js. No iframe of Harbor's own
// UI — we render natively so auth + styling match the rest of the
// Dashboard.

import * as api from './api.js';

function humanBytes(n) {
    if (!n || n <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

// P9f.3 — per-variant verification dot.
//
//   verified      green — smoke probe passed on a matching-hardware worker
//   pending       amber — smoke task queued or waiting for worker claim
//   failed        red   — smoke verification ran and returned non-zero
//   build_failed  red   — image never built (P9b's incompatible_reasons path)
//   null/unknown  grey  — image isn't mapped to a node's gpu_variants (legacy
//                         tags, manual pushes, or platform nodes w/o tenant)
function verificationDot(status) {
    if (!status) {
        return '<span class="inline-block w-2.5 h-2.5 rounded-full bg-slate-300" title="not mapped to a tracked variant"></span>';
    }
    if (status === 'verified') {
        return '<span class="inline-flex items-center gap-1.5" title="Smoke verified — module imports and forward-pass ran on real hardware"><span class="inline-block w-2.5 h-2.5 rounded-full bg-green-500"></span><span class="text-xs text-green-700">Verified</span></span>';
    }
    if (status === 'pending') {
        return '<span class="inline-flex items-center gap-1.5" title="Smoke probe queued or awaiting worker"><span class="inline-block w-2.5 h-2.5 rounded-full bg-amber-400 animate-pulse"></span><span class="text-xs text-amber-700">Pending</span></span>';
    }
    if (status === 'failed') {
        return '<span class="inline-flex items-center gap-1.5" title="Smoke verification failed — check variant_smoke task log_tail"><span class="inline-block w-2.5 h-2.5 rounded-full bg-red-500"></span><span class="text-xs text-red-700">Failed</span></span>';
    }
    if (status === 'build_failed') {
        return '<span class="inline-flex items-center gap-1.5" title="Docker build for this variant exited non-zero"><span class="inline-block w-2.5 h-2.5 rounded-full bg-red-500"></span><span class="text-xs text-red-700">Build failed</span></span>';
    }
    return `<span class="inline-flex items-center gap-1.5" title="Unknown status: ${status}"><span class="inline-block w-2.5 h-2.5 rounded-full bg-slate-300"></span><span class="text-xs text-slate-500">${status}</span></span>`;
}

// Order for sort-by-status: worst→best so failures rise to the top.
const VERIFICATION_RANK = {
    failed: 0,
    build_failed: 1,
    pending: 2,
    verified: 3,
    null: 4,
    undefined: 4,
};

let _trImagesCache = [];

function sortImages(images, mode) {
    const copy = images.slice();
    switch (mode) {
        case 'pushed_asc':
            return copy.sort((a, b) => (Date.parse(a.pushed_at || 0) || 0) - (Date.parse(b.pushed_at || 0) || 0));
        case 'repo':
            return copy.sort((a, b) => (a.repository || '').localeCompare(b.repository || ''));
        case 'verification':
            return copy.sort((a, b) => {
                const ra = VERIFICATION_RANK[a.verification_status] ?? 4;
                const rb = VERIFICATION_RANK[b.verification_status] ?? 4;
                return ra - rb;
            });
        case 'pushed_desc':
        default:
            return copy.sort((a, b) => (Date.parse(b.pushed_at || 0) || 0) - (Date.parse(a.pushed_at || 0) || 0));
    }
}

function renderImagesTable(images) {
    const tbody = document.getElementById('tr-images-tbody');
    const sortMode = document.getElementById('tr-sort-select')?.value || 'pushed_desc';
    const sorted = sortImages(images, sortMode);

    // Per-tenant aggregated total size — surfaces in the count badge
    // up top so platform admins see the storage footprint at a glance.
    const totalBytes = sorted.reduce((sum, img) => sum + (img.size || 0), 0);
    const countEl = document.getElementById('tr-image-count');
    if (countEl) {
        countEl.textContent =
            `${sorted.length} image${sorted.length === 1 ? '' : 's'} · ${humanBytes(totalBytes)} total`;
    }

    tbody.innerHTML = sorted.map(img => {
        // Harbor's scan_overview is keyed by MIME type — e.g.
        // {"application/vnd.security.vulnerability.report; version=1.1":
        //   {scan_status, severity, summary}}.
        // Unwrap to the first inner report. Pre-fix this branch read
        // .scan_status / .severity straight off the outer object, got
        // undefined, and the badge silently fell into the "Scanning…"
        // branch — even though Harbor had finished hours ago.
        const scanOverview = img.scan_overview || {};
        const scan = Object.values(scanOverview)[0] || {};
        const severity = scan.severity || '';
        const counts = (scan.summary && scan.summary.summary) || scan.summary || {};
        const pushed = img.pushed_at ? new Date(img.pushed_at).toLocaleString() : '—';
        const variantCell = img.variant
            ? `<span class="inline-block px-2 py-0.5 text-xs font-mono bg-slate-100 text-slate-700 rounded">${img.variant}</span>`
            : '<span class="text-xs text-slate-400">—</span>';
        const repo = img.repository || '';
        const tag = img.tag || 'latest';
        const sizeBytes = img.size || 0;
        const digestAttr = img.digest || '';
        return `<tr class="border-t hover:bg-slate-50"
                    data-digest="${digestAttr}"
                    data-repo="${repo}"
                    data-tag="${tag}"
                    data-size="${sizeBytes}">
            <td class="px-6 py-3 font-medium text-gray-800 cursor-pointer tr-scan-cell">${repo || '—'}</td>
            <td class="px-6 py-3 text-gray-600 cursor-pointer tr-scan-cell">${tag}</td>
            <td class="px-6 py-3 cursor-pointer tr-scan-cell">${variantCell}</td>
            <td class="px-6 py-3 cursor-pointer tr-scan-cell">${verificationDot(img.verification_status)}</td>
            <td class="px-6 py-3 text-gray-600 cursor-pointer tr-scan-cell">${humanBytes(sizeBytes)}</td>
            <td class="px-6 py-3 text-gray-600 cursor-pointer tr-scan-cell">${pushed}</td>
            <td class="px-6 py-3 cursor-pointer tr-scan-cell">${scanBadge(scan.scan_status || severity, counts)}</td>
            <td class="px-6 py-3 text-right">
                <button class="tr-soft-delete-btn px-2 py-1 text-xs rounded border border-red-200 text-red-700 hover:bg-red-50"
                        title="Soft-delete this image (ISO retention applies)">
                    Delete
                </button>
            </td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('.tr-scan-cell').forEach(cell => {
        cell.addEventListener('click', (e) => {
            const tr = e.currentTarget.closest('tr');
            openScanDrawer(tr.dataset.repo, tr.dataset.digest);
        });
    });
    tbody.querySelectorAll('.tr-soft-delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const tr = e.currentTarget.closest('tr');
            await softDeleteImage({
                repository: tr.dataset.repo,
                tag: tr.dataset.tag,
                digest: tr.dataset.digest,
                size_bytes: parseInt(tr.dataset.size, 10) || null,
            });
        });
    });
}

async function softDeleteImage(img) {
    let confirmed;
    try {
        const { showConfirm } = await import('./ui.js');
        confirmed = await showConfirm(
            `Soft-delete "${img.repository}:${img.tag}"? It will be hidden from the registry immediately and permanently removed from Harbor after the ISO retention window (default 30 days).`,
            'Soft-delete tenant image',
        );
    } catch {
        confirmed = window.confirm(`Soft-delete ${img.repository}:${img.tag}?`);
    }
    if (!confirmed) return;

    const params = new URLSearchParams({
        repository: img.repository,
        tag: img.tag,
    });
    if (img.digest) params.set('digest', img.digest);
    if (img.size_bytes) params.set('size_bytes', String(img.size_bytes));

    try {
        await api.fetchApi(`/harbor/tenant-images/soft-delete?${params}`, {
            method: 'POST',
        });
        // Refresh listing — the soft-deleted row will drop out.
        loadImages();
        loadQuota();
    } catch (e) {
        console.error('soft-delete failed:', e);
        try {
            const { showError } = await import('./ui.js');
            showError(`Failed to delete image: ${e.message || e}`);
        } catch {
            alert(`Failed to delete image: ${e.message || e}`);
        }
    }
}

function scanBadge(status, counts) {
    // status is one of 'Success', 'Failed', 'Running', 'Pending' or the
    // MOD-side scan_status rollup from our own _poll_harbor_scan.
    const c = counts || {};
    const critical = c.Critical || c.CRITICAL || 0;
    const high = c.High || c.HIGH || 0;
    if (critical > 0) {
        return `<span class="px-2 py-1 text-xs font-medium rounded bg-red-100 text-red-700">CRITICAL × ${critical}</span>`;
    }
    if (high > 0) {
        return `<span class="px-2 py-1 text-xs font-medium rounded bg-amber-100 text-amber-700">HIGH × ${high}</span>`;
    }
    if (status === 'Success') {
        return '<span class="px-2 py-1 text-xs font-medium rounded bg-green-100 text-green-700">Clean</span>';
    }
    if (status === 'Running' || status === 'Pending' || status === '') {
        return '<span class="px-2 py-1 text-xs font-medium rounded bg-slate-100 text-slate-500">Scanning…</span>';
    }
    if (status === 'Failed') {
        return '<span class="px-2 py-1 text-xs font-medium rounded bg-slate-200 text-slate-600">Scan failed</span>';
    }
    return '<span class="px-2 py-1 text-xs font-medium rounded bg-slate-100 text-slate-500">—</span>';
}

async function loadQuota() {
    // FIX-CE-HIDE-ADMIN-STORAGE-QUOTA (owner 11-11): the per-plan Harbor storage quota
    // bar is a paid-plan view. Positive-paid gate (isPaidEdition is false for CE, for an
    // unknown edition and for an errored payload): hide the whole Storage card and make
    // no /harbor/tenant-quota call. Paid (saas/pro) behaviour below is unchanged.
    if (!await api.isPaidEdition()) {
        const card = document.getElementById('tr-quota-text')?.closest('.bg-white');
        if (card) card.classList.add('hidden');
        return;
    }
    try {
        const q = await api.fetchApi('/harbor/tenant-quota');
        const used = q.used_bytes || 0;
        const hard = q.hard_quota_bytes || -1;
        const text = document.getElementById('tr-quota-text');
        const bar = document.getElementById('tr-quota-bar');
        const warn = document.getElementById('tr-quota-warning');
        if (hard > 0) {
            const pct = Math.min(100, (used / hard) * 100);
            text.textContent = `${humanBytes(used)} / ${humanBytes(hard)} (${pct.toFixed(1)}%)`;
            bar.style.width = `${pct}%`;
            if (pct >= 80) {
                bar.classList.remove('bg-primary/80');
                bar.classList.add(pct >= 95 ? 'bg-red-500' : 'bg-amber-500');
                warn.classList.add('hidden');
            }
        } else {
            text.textContent = `${humanBytes(used)} (unlimited)`;
            bar.style.width = '0%';
        }
    } catch (e) {
        const text = document.getElementById('tr-quota-text');
        if (text) text.textContent = 'Unavailable';
        console.warn('tenant-registry: quota fetch failed', e);
    }
}

async function loadImages() {
    const loading = document.getElementById('tr-images-loading');
    const empty = document.getElementById('tr-images-empty');
    const table = document.getElementById('tr-images-table');
    const tbody = document.getElementById('tr-images-tbody');
    const countEl = document.getElementById('tr-image-count');
    loading.classList.remove('hidden');
    empty.classList.add('hidden');
    table.classList.add('hidden');

    try {
        const images = await api.fetchApi('/harbor/tenant-images') || [];
        loading.classList.add('hidden');
        countEl.textContent = `${images.length} image${images.length === 1 ? '' : 's'}`;
        if (images.length === 0) {
            empty.classList.remove('hidden');
            return;
        }
        _trImagesCache = images;
        renderImagesTable(_trImagesCache);
        table.classList.remove('hidden');
    } catch (e) {
        loading.classList.add('hidden');
        empty.textContent = 'Failed to load images from Harbor. Check the API logs.';
        empty.classList.remove('hidden');
        console.warn('tenant-registry: images fetch failed', e);
    }
}

async function openScanDrawer(repo, digest) {
    const drawer = document.getElementById('tr-scan-drawer');
    const body = document.getElementById('tr-scan-body');
    body.textContent = 'Loading scan detail…';
    drawer.classList.remove('translate-x-full');
    if (!digest) {
        body.textContent = 'No digest — image may still be pushing.';
        return;
    }
    try {
        const scan = await api.fetchApi(`/harbor/tenant-scans/${encodeURIComponent(digest)}?repo=${encodeURIComponent(repo)}`);
        // Harbor returns a dict keyed by MIME type; first value has the
        // report. Render a straightforward CVE list.
        const first = scan && Object.values(scan)[0];
        const vulns = (first && first.vulnerabilities) || [];
        if (vulns.length === 0) {
            body.innerHTML = `<p class="text-gray-500">No vulnerabilities reported for
                <code class="text-xs">${repo}</code>.</p>`;
            return;
        }
        body.innerHTML = `
            <p class="text-xs text-gray-500 mb-3">${vulns.length} vuln${vulns.length === 1 ? '' : 's'} in
                <code>${repo}</code></p>
            <ul class="space-y-3">
                ${vulns.slice(0, 100).map(v => {
                    const sev = (v.severity || '').toUpperCase();
                    const sevColor = {
                        CRITICAL: 'text-red-700 bg-red-50',
                        HIGH: 'text-amber-700 bg-amber-50',
                        MEDIUM: 'text-yellow-700 bg-yellow-50',
                        LOW: 'text-slate-600 bg-slate-50',
                        UNKNOWN: 'text-slate-500 bg-slate-50',
                    }[sev] || 'text-slate-500 bg-slate-50';
                    return `<li class="border rounded p-2 text-xs">
                        <div class="flex items-center justify-between">
                            <span class="font-mono text-gray-700">${v.id || '—'}</span>
                            <span class="px-2 py-0.5 rounded ${sevColor}">${sev}</span>
                        </div>
                        <div class="mt-1 text-gray-600"><strong>${v.package || '?'}</strong> ${v.version || ''}
                            ${v.fix_version ? `→ <span class="text-green-700">${v.fix_version}</span>` : '<em>(no fix)</em>'}
                        </div>
                        ${v.description ? `<div class="mt-1 text-gray-500">${v.description.slice(0, 200)}</div>` : ''}
                    </li>`;
                }).join('')}
            </ul>
            ${vulns.length > 100 ? `<p class="mt-3 text-xs text-gray-400">… and ${vulns.length - 100} more. See Harbor UI for the full list.</p>` : ''}`;
    } catch (e) {
        body.innerHTML = `<p class="text-red-600">Failed to load scan detail: ${escapeHtml(e?.message || e)}</p>`;
    }
}

export function initializeTenantRegistryPage() {
    document.getElementById('tr-refresh-btn')?.addEventListener('click', () => {
        loadQuota();
        loadImages();
    });
    document.getElementById('tr-scan-close')?.addEventListener('click', () => {
        document.getElementById('tr-scan-drawer')?.classList.add('translate-x-full');
    });
    document.getElementById('tr-sort-select')?.addEventListener('change', () => {
        if (_trImagesCache.length) renderImagesTable(_trImagesCache);
    });
    loadQuota();
    loadImages();
}

/**
 * Escape HTML to prevent XSS in innerHTML.
 * Pattern: .replace(/[&<>"']/g, ...)
 */
function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (m) {
        switch (m) {
            case '&': return '&amp;';
            case '<': return '&lt;';
            case '>': return '&gt;';
            case '"': return '&quot;';
            case "'": return '&#39;';
        }
        return m;
    });
}
