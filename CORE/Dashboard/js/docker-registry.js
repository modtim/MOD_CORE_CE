// Dashboard/js/docker-registry.js
//
// Docker Registry page (Dashboard/js/ui.js, hash #docker-registry). Shows
// platform Harbor status + per-base-image architecture support, and lets
// tenants configure their own custom Docker registry.
//
// pages/docker-registry.html used to be a standalone full HTML document
// (own <!DOCTYPE>/<head>/<body>, its own sidebar-container/topnav-container
// placeholders, and a final inline <script> with a DOMContentLoaded
// listener that called checkAuth()/loadUI()/loadRegistryStatus()) — a
// leftover from before this page was folded into the SPA hash router.
// Two things made it dead code:
//   1. ui.js's navigateToPage() mounts every hash-routed page by fetching
//      the fragment and setting `#page-content.innerHTML = ...`. Per the
//      HTML spec, a <script> element inserted via innerHTML never executes
//      (this is the same bug class already fixed for
//      pages/payment-settings.html — see initializePaymentSettingsPage()).
//   2. Even if scripts DID execute, `checkAuth()`/`loadUI()` and
//      `js/config.js` referenced by the old inline script don't exist
//      anywhere in the current codebase (checked: grep turns up nothing),
//      and re-loading js/ui.js / js/auth.js as classic (non-module)
//      <script src> tags would throw `Unexpected token 'export'` — they're
//      ES modules. So the generic "re-execute the injected script" fix
//      (utils.js's runInjectedScripts) can't work here.
//
// Fix: move the real logic into this proper importable module (same
// pattern as pricing-config.js / payment-settings.js), invoked explicitly
// by ui.js's navigateToPage() after mounting the markup. The page's sidebar/
// topnav are already supplied once by the SPA shell (pages/dashboard-shell.html,
// mounted at app init by main.js) — this page never needed its own copies.
import { fetchApi } from './api.js';

const _esc = (t) => String(t == null ? '' : t)
    .replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export async function initializeDockerRegistryPage() {
    await loadRegistryStatus();
    await loadBaseImageArch();
}

// Gate 11 — architectures per base image.
async function loadBaseImageArch() {
    const host = document.getElementById('base-image-arch');
    if (!host) return;
    try {
        const d = await fetchApi('/platform/base-image-architectures');
        const rows = (d.images || []).map((i) => {
            // "assumed" is not "confirmed". The platform Harbor is
            // private and refuses anonymous manifest reads, so an
            // undetermined image falls back to amd64 for scheduling.
            // Rendering that as a finding would be a lie.
            const badge = !i.determined
                ? `<span class="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600" title="The registry would not answer — it is private. Treated as amd64 for scheduling, which is the safe default, but this is an assumption and not a reading.">not determined</span>`
                : i.runs_on_arm
                ? `<span class="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">runs on ARM</span>`
                : `<span class="px-2 py-0.5 rounded-full text-xs bg-amber-100 text-amber-800" title="Built for amd64 only. An ARM worker is never offered work using this image.">amd64 only</span>`;
            return `<tr class="border-t border-gray-200">
                <td class="py-2 pr-4 text-gray-800">${_esc(i.display_name)}</td>
                <td class="py-2 pr-4"><code class="text-xs text-gray-600">${_esc(i.image_tag)}</code></td>
                <td class="py-2 pr-4 text-xs text-gray-500">${_esc(i.process_type_hint)}</td>
                <td class="py-2 pr-4 text-xs text-gray-600">${_esc((i.architectures || []).join(', '))}</td>
                <td class="py-2">${badge}</td></tr>`;
        }).join('');
        const undet = d.undetermined || 0;
        host.innerHTML = `<table class="w-full text-sm"><thead>
            <tr class="text-left text-[11px] uppercase tracking-wide text-gray-400">
            <th class="pb-2 pr-4">Image</th><th class="pb-2 pr-4">Tag</th>
            <th class="pb-2 pr-4">Tier</th><th class="pb-2 pr-4">Architectures</th>
            <th class="pb-2">ARM</th></tr></thead><tbody>${rows}</tbody></table>
            <div class="mt-3 text-xs ${d.arm_ready ? 'text-green-800 bg-green-50 border-green-200' : 'text-amber-800 bg-amber-50 border-amber-200'} border rounded p-3">
                <strong>${d.arm_ready} of ${d.total} base images run on ARM.</strong>
                ${d.arm_ready === 0
                    ? 'No ARM machine can currently be given work here. Publishing the multi-arch CPU-tier images changes that.'
                    : 'An ARM worker can be offered work using those images.'}
                ${undet ? ` ${undet} could not be determined — the registry is private and refuses anonymous reads, so those are treated as amd64 for scheduling.` : ''}
            </div>
            <div class="mt-2 text-[11px] text-gray-400">Accelerator tiers (CUDA / ROCm / Intel / NPU)
                are amd64 by nature here; that is a property of the hardware, not a gap.</div>`;
    } catch (e) {
        host.innerHTML = `<span class="text-sm text-red-600">Could not load base image architectures: ${_esc(e.message || e)}</span>`;
    }
}

async function loadRegistryStatus() {
    try {
        const status = await fetchApi('/harbor/status');

        // Every id below exists in pages/docker-registry.html. They can still
        // come back null: ui.js swaps #page-content on each navigation, and
        // the user (or a double hash-route) can leave the page while
        // /harbor/status is in flight. If our markup is gone, stop quietly.
        // Before this guard, that case threw "Cannot set properties of null
        // (setting 'innerHTML')".
        const platformStatus = document.getElementById('platform-harbor-status');
        if (!platformStatus) return;
        // WS4.28: for non-platform-admins the API strips the platform
        // fields (platform_configured=false, missing secrets hidden), so a
        // false here means "you may not see platform state", NOT "the
        // platform Harbor is down".
        if (!status.is_platform_admin) {
            platformStatus.innerHTML = `
                <div class="flex items-center text-gray-500">
                    <span class="font-medium">Platform Harbor status is visible to platform administrators only.</span>
                </div>
            `;
        } else if (status.platform_configured) {
            platformStatus.innerHTML = `
                <div class="flex items-center text-green-600">
                    <svg class="w-5 h-5 mr-2" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>
                    </svg>
                    <span class="font-medium">Connected to Platform Harbor</span>
                </div>
            `;
        } else {
            platformStatus.innerHTML = `
                <div class="flex items-center text-yellow-600">
                    <svg class="w-5 h-5 mr-2" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/>
                    </svg>
                    <span class="font-medium">No platform Harbor configured. You can use your own registry below.</span>
                </div>
            `;
        }

        // Check if user can use custom registry
        if (status.can_use_custom_registry) {
            document.getElementById('custom-registry-form')?.classList.remove('hidden');
            document.getElementById('upgrade-prompt')?.classList.add('hidden');

            // Load existing custom registry config if enabled
            if (status.custom_registry_enabled) {
                const config = await fetchApi('/harbor/custom-registry');
                // Second await: the page may have been left meanwhile.
                if (!document.getElementById('custom-registry-enabled')) return;
                document.getElementById('custom-registry-enabled').checked = config.enabled;
                document.getElementById('registry-url').value = config.registry_url || '';
                document.getElementById('registry-username').value = config.username || '';

                if (config.enabled) {
                    document.getElementById('custom-registry-fields').classList.remove('hidden');
                }
            }

            // Setup event listeners
            setupCustomRegistryListeners();
        } else {
            document.getElementById('upgrade-prompt')?.classList.remove('hidden');
        }
    } catch (error) {
        console.error('Error loading registry status:', error);
        showNotification('Failed to load registry status', 'error');
    }
}

function setupCustomRegistryListeners() {
    // SPA remount: page HTML re-injects on every visit, so the wiring marker
    // lives on the checkbox element, not a module flag.
    const enabledCheckbox = document.getElementById('custom-registry-enabled');
    const customFields = document.getElementById('custom-registry-fields');
    if (!enabledCheckbox || !customFields) return;
    if (enabledCheckbox.dataset.wired) return;  // idempotent — page can re-init on repeat navigation
    enabledCheckbox.dataset.wired = '1';

    enabledCheckbox.addEventListener('change', () => {
        if (enabledCheckbox.checked) {
            customFields.classList.remove('hidden');
        } else {
            customFields.classList.add('hidden');
        }
    });

    document.getElementById('save-custom-registry-btn').addEventListener('click', saveCustomRegistry);
    document.getElementById('test-connection-btn').addEventListener('click', testConnection);
}

async function saveCustomRegistry() {
    const enabled = document.getElementById('custom-registry-enabled').checked;
    const config = {
        enabled: enabled
    };

    if (enabled) {
        config.registry_url = document.getElementById('registry-url').value.trim();
        config.username = document.getElementById('registry-username').value.trim();
        config.password = document.getElementById('registry-password').value;

        if (!config.registry_url || !config.username || !config.password) {
            showNotification('Please fill in all fields', 'error');
            return;
        }
    }

    try {
        await fetchApi('/harbor/custom-registry', {
            method: 'POST',
            body: JSON.stringify(config)
        });

        showNotification('Custom registry configuration saved successfully', 'success');

        // Clear password field for security
        document.getElementById('registry-password').value = '';
    } catch (error) {
        showNotification(error.message || 'Failed to save configuration', 'error');
    }
}

async function testConnection() {
    showNotification('Testing connection to custom registry...', 'info');
    // TODO: Implement test connection API endpoint
    setTimeout(() => {
        showNotification('Connection test not yet implemented', 'warning');
    }, 1000);
}

function showNotification(message, type = 'info') {
    const statusDiv = document.getElementById('custom-registry-status');
    if (!statusDiv) return;
    const colors = {
        success: 'bg-green-50 border-green-200 text-green-800',
        error: 'bg-red-50 border-red-200 text-red-800',
        warning: 'bg-yellow-50 border-yellow-200 text-yellow-800',
        info: 'bg-primary/10 border-primary text-primary'
    };

    statusDiv.className = `p-4 rounded-lg border ${colors[type]}`;
    statusDiv.textContent = message;
    statusDiv.classList.remove('hidden');

    setTimeout(() => {
        statusDiv.classList.add('hidden');
    }, 5000);
}
