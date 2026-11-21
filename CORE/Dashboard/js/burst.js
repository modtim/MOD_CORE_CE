// Dashboard/js/burst.js — CE stub (11-09).
//
// The real CORE/Dashboard/js/burst.js drives burst-to-MOD (pairing an install
// to a MOD account and spilling queued runs to MOD-hosted compute). That runs
// on MOD infrastructure, so it is NOT shipped in CE: the file is denied in
// open-core/manifest.yaml and the build copies this overlay over the path so
// analytics.js's static `import { renderBurstSection }` still resolves.
//
// Makes NO fetch calls. The Burst sub-tab stays hidden on CE (analytics.js),
// so this is never reached from the UI. The own-cloud Cloud tab lives in
// burst-accounts.js and is unaffected.

export function renderBurstSection() {
    _renderBurstWiring();
}

function _renderBurstWiring() {}

export default { renderBurstSection };
