// Dashboard/js/key-inventory.js - CE no-op stub (FIX-CE-DENY-9, 11-14; precedent = item 124 licence-review).
//
// The real CORE/Dashboard/js/key-inventory.js is denied in open-core/manifest.yaml (FIX-CE-DENY-9
// deny rows citing ef8651f732 / DIAG-10). The build copies this overlay file over the denied
// path so the static import of initializeKeyInventoryPage in the shipped importers (manifest deny-row comment:
// hub-compliance.js / ui.js / security-hub.js / users.js) still resolves on CE.
//
// Same export name as the real module. Does nothing: no DOM writes, no fetch calls, no
// network. Every page that would call it is hidden on CE (registerPage scrub rows).

export async function initializeKeyInventoryPage() {
    return undefined;
}
