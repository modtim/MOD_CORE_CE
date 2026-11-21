// Dashboard/js/license-widget.js — CE stub (Q1-B, 10-29).
//
// The real CORE/Dashboard/js/license-widget.js is denied in
// open-core/manifest.yaml (license code is NOT shipped in CE, ~:2159).
// The build copies this overlay file over the denied path so that
// home.js's static `import { renderLicenseWidget }` still resolves on CE.
//
// Same export signature as the real widget. Makes NO fetch calls: CE is
// unlimited, so it only renders the static Community Edition card.

export async function renderLicenseWidget(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    container.innerHTML = `
        <div data-testid="ce-edition-card" class="bg-white rounded-lg shadow p-6">
            <div class="flex items-center justify-between mb-2">
                <h3 class="text-lg font-semibold text-gray-800">Community Edition</h3>
                <span class="inline-block px-2 py-1 text-xs font-semibold bg-gray-100 text-gray-800 rounded-full">CE</span>
            </div>
            <p class="text-sm text-gray-600">MOD Community Edition is free to use. No licence key is needed.</p>
        </div>`;
}
