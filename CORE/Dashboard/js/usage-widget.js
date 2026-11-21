// Dashboard/js/usage-widget.js

import { getPlatformFeatures } from './api.js';

const apiUrl = window.APP_CONFIG?.API_URL || 'http://api.localhost';

export async function renderUsageWidget(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const token = sessionStorage.getItem('accessToken');
    if (!token) {
        container.innerHTML = '';
        return;
    }

    try {
        // Check if credits are enabled (coalesced 30s cache — shared with
        // the header indicator and the nav gating in ui.js).
        let creditsEnabled = false;
        let billingDisabled = false;
        try {
            const features = await getPlatformFeatures();
            creditsEnabled = features.credits_enabled === true;
            billingDisabled = features.billing_enabled === false;
        } catch (e) { /* ignore — default to disabled */ }

        // 1783 — CE/onprem billing-off deployments must never fetch the paid
        // /subscriptions surface (CE strips the router; the call can hang or
        // 404). Same gate as billing.js/ui.js: /platform/features.billing_enabled.
        if (billingDisabled) {
            container.innerHTML = '';
            return;
        }

        const response = await fetch(`${apiUrl}/subscriptions`, {
            headers: {
                'Authorization': `Bearer ${token}`
            }
        });

        if (!response.ok) {
            // No subscription - don't show widget
            container.innerHTML = '';
            return;
        }

        const data = await response.json();
        container.innerHTML = renderUsageCards(data, creditsEnabled);
        document.dispatchEvent(new CustomEvent('mod:page-rendered')); // OZ-C3-P5: re-run the label hook after async widget render
    } catch (error) {
        console.error('Error loading usage widget:', error);
        container.innerHTML = '';
    }
}

function renderUsageCards(data, creditsEnabled = false) {
    const {
        credits_remaining,
        credits_total,
        storage_used_bytes,
        storage_limit_bytes,
        harbor_used_bytes = 0,
        plan,
        seats_used,
        seats_limit,
        concurrent_processes_running,
        concurrent_processes_limit,
        egress_used_bytes = 0,
        egress_included_gb,
        egress_limit_bytes,
        egress_overage_price_cents
    } = data;

    const creditsUsed = credits_total - credits_remaining;
    const creditsPercentage = credits_total > 0 ? Math.min((creditsUsed / credits_total) * 100, 100) : 0;
    const creditsColor = creditsPercentage > 90 ? 'red' : creditsPercentage > 75 ? 'yellow' : 'blue';

    // Storage = MinIO file storage + Harbor node-image storage. Node images ARE
    // storage and are enforced against the same quota (the Harbor project quota
    // = the plan's storage limit), so show one combined number.
    const totalStorageBytes = storage_used_bytes + harbor_used_bytes;
    // storage_limit_bytes === null is the "unlimited" sentinel (platform admin).
    const storageUnlimited = storage_limit_bytes === null || storage_limit_bytes === undefined;
    const storagePercentage = (!storageUnlimited && storage_limit_bytes > 0)
        ? Math.min((totalStorageBytes / storage_limit_bytes) * 100, 100)
        : 0;
    // LDF-4 §8.2 — yellow / orange / red banner thresholds. Matches
    // the email cadence in utils/usage_alerts.py (80%/95%/100%).
    const storageColor = storageUnlimited ? 'green'
        : storagePercentage >= 100 ? 'red'
        : storagePercentage >= 95 ? 'orange'
        : storagePercentage >= 80 ? 'yellow'
        : 'green';
    const storageLimitGB = storageUnlimited ? null : Math.round(storage_limit_bytes / (1024 * 1024 * 1024));

    const seatsPercentage = seats_limit > 0 ? Math.min((seats_used / seats_limit) * 100, 100) : 0;
    const seatsColor = seatsPercentage > 90 ? 'red' : seatsPercentage > 75 ? 'yellow' : 'purple';

    const processesPercentage = concurrent_processes_limit > 0
        ? Math.min((concurrent_processes_running / concurrent_processes_limit) * 100, 100) : 0;
    const processesColor = processesPercentage > 90 ? 'red' : processesPercentage > 75 ? 'yellow' : 'orange';

    // Egress (add-egress-billing). Decimal GB (10^9) to match billing. Shown only
    // when the plan prices/allows egress or the tenant has used some.
    const egressBilled = egress_overage_price_cents !== null && egress_overage_price_cents !== undefined;
    const showEgress = egressBilled || (egress_included_gb > 0) || (egress_used_bytes > 0);
    const egressUnlimited = egress_limit_bytes === null || egress_limit_bytes === undefined;
    const egressPct = (!egressUnlimited && egress_limit_bytes > 0)
        ? Math.min((egress_used_bytes / egress_limit_bytes) * 100, 100) : 0;
    const egressColor = egressUnlimited ? 'green'
        : egressPct >= 100 ? 'red' : egressPct >= 95 ? 'orange' : egressPct >= 80 ? 'yellow' : 'green';
    const egressUsedGB = (egress_used_bytes / 1e9);

    // 3 base cards (storage/seats/workflows) + credits + egress; cap grid at 4
    // columns (extra cards wrap) so we never depend on a grid-cols-5 utility.
    const gridCols = (creditsEnabled || showEgress) ? 'lg:grid-cols-4' : 'lg:grid-cols-3';

    return `
        <div class="grid grid-cols-1 md:grid-cols-2 ${gridCols} gap-4">
            ${creditsEnabled ? `
            <!-- Credits Card -->
            <div class="bg-white p-4 rounded-lg shadow-md border-l-4 border-${creditsColor}-500">
                <div class="flex items-center justify-between mb-2">
                    <h3 class="text-sm font-semibold text-gray-700">Credits</h3>
                    <svg class="w-5 h-5 text-${creditsColor}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                </div>
                <p class="text-2xl font-bold text-gray-900">${credits_remaining.toLocaleString()}</p>
                <p class="text-xs text-gray-600 mt-1">of ${credits_total.toLocaleString()} remaining</p>
                <div class="w-full bg-gray-200 rounded-full h-2 mt-2">
                    <div class="bg-${creditsColor}-500 h-2 rounded-full" style="width: ${100 - creditsPercentage}%"></div>
                </div>
            </div>
            ` : ''}

            <!-- Storage Card -->
            <div class="bg-white p-4 rounded-lg shadow-md border-l-4 border-${storageColor}-500">
                <div class="flex items-center justify-between mb-2">
                    <h3 class="text-sm font-semibold text-gray-700">Storage</h3>
                    <svg class="w-5 h-5 text-${storageColor}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z"></path>
                    </svg>
                </div>
                <p class="text-2xl font-bold text-gray-900">${formatBytes(totalStorageBytes)}</p>
                <p class="text-xs text-gray-600 mt-1">${storageUnlimited ? 'used — files + node images' : `of ${storageLimitGB.toLocaleString()} GB used (files + node images)`}</p>
                <div class="w-full bg-gray-200 rounded-full h-2 mt-2">
                    <div class="bg-${storageColor}-500 h-2 rounded-full" style="width: ${storagePercentage}%"></div>
                </div>
            </div>

            ${showEgress ? `
            <!-- Egress (bandwidth) Card -->
            <div class="bg-white p-4 rounded-lg shadow-md border-l-4 border-${egressColor}-500">
                <div class="flex items-center justify-between mb-2">
                    <h3 class="text-sm font-semibold text-gray-700">Bandwidth (egress)</h3>
                    <svg class="w-5 h-5 text-${egressColor}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"></path>
                    </svg>
                </div>
                <p class="text-2xl font-bold text-gray-900">${egressUsedGB.toFixed(2)} GB</p>
                <p class="text-xs text-gray-600 mt-1">${egressUnlimited ? 'used (unlimited)' : `of ${(egress_included_gb || 0).toLocaleString()} GB included`}${egressBilled ? ` · $${(egress_overage_price_cents / 100).toFixed(2)}/GB over` : ''}</p>
                <div class="w-full bg-gray-200 rounded-full h-2 mt-2">
                    <div class="bg-${egressColor}-500 h-2 rounded-full" style="width: ${egressPct}%"></div>
                </div>
            </div>
            ` : ''}

            <!-- Seats Card -->
            <div class="bg-white p-4 rounded-lg shadow-md border-l-4 border-${seatsColor}-500">
                <div class="flex items-center justify-between mb-2">
                    <h3 class="text-sm font-semibold text-gray-700">Seats</h3>
                    <svg class="w-5 h-5 text-${seatsColor}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z"></path>
                    </svg>
                </div>
                <p class="text-2xl font-bold text-gray-900">${seats_used}</p>
                <p class="text-xs text-gray-600 mt-1">of ${seats_limit.toLocaleString()} seats used</p>
                <div class="w-full bg-gray-200 rounded-full h-2 mt-2">
                    <div class="bg-${seatsColor}-500 h-2 rounded-full" style="width: ${seatsPercentage}%"></div>
                </div>
            </div>

            <!-- Workflows Card -->
            <div class="bg-white p-4 rounded-lg shadow-md border-l-4 border-${processesColor}-500">
                <div class="flex items-center justify-between mb-2">
                    <h3 class="text-sm font-semibold text-gray-700">Running <span data-theme-key="workflows">Workflows</span></h3>
                    <svg class="w-5 h-5 text-${processesColor}-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                </div>
                <p class="text-2xl font-bold text-gray-900">${concurrent_processes_running}</p>
                <p class="text-xs text-gray-600 mt-1">of ${concurrent_processes_limit.toLocaleString()} concurrent</p>
                <div class="w-full bg-gray-200 rounded-full h-2 mt-2">
                    <div class="bg-${processesColor}-500 h-2 rounded-full" style="width: ${processesPercentage}%"></div>
                </div>
            </div>
        </div>
    `;
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}
