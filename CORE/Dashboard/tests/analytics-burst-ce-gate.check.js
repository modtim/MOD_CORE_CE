// Node-runnable check for the edition/role gates on the Cloud accounts tab
// (#main-tab-burst-accounts) and the Apps tab (#main-tab-apps) of the Analytics/System
// page. Tim B3 items 5+6 (11-06) hid BOTH on CE; owner 11-11 (answer B) SUPERSEDES the
// Cloud half of item 5: the tenant's OWN cloud account tab is CE-visible and usable,
// so on CE the Cloud tab is REVEALED and its initializer RENDERS (role gate only, no
// edition gate), while Apps (item 6) and the mesh family stay CE-hidden exactly as
// before. Paid editions unchanged. Same technique as burst-accounts.check.js: the gate
// code is EXTRACTED VERBATIM from the shipped source and run against a stub DOM, so
// the assertions test the shipped source, not a re-typed copy.
//
//   node CORE/Dashboard/tests/analytics-burst-ce-gate.check.js
//
// Asserts: CE -> Cloud tab revealed + renderBurstAccountsSection called + deep link
// opens for a tenant admin (a non-admin still falls back); Apps hidden + markup never
// fetched + deep link falls back; unknown/errored edition: Cloud still renders (no
// edition gate left), Apps still denies; paid -> both revealed + initialized; the
// role gates stay intact (a tenant member gains neither tab); the sibling operator
// Burst tab (#main-tab-burst, MOD-hosted spill) stays CE-hidden (gate not widened).
'use strict';
const fs = require('fs');
const path = require('path');

const analyticsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'analytics.js'), 'utf8');

// Brace-matched block whose '{' is at `open`.
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

// The whole `if (...) {...}` statement whose body holds `needle`.
function revealGate(src, needle) {
    const at = src.indexOf(needle);
    if (at < 0) throw new Error(`needle '${needle}' not found`);
    const cond = src.lastIndexOf('if (', at);
    const open = src.indexOf('{', cond);
    return src.slice(cond, open + blockAt(src, open).length);
}

// Each tab id now has TWO remove() calls (the checkUserRole reveal + the panel-init
// paid leg); the reveal is the 12-space-indented one inside checkUserRole, the init
// paid leg is indented 16, so the leading newline + indent pins the needle to the reveal.
const CLOUD_REVEAL = "\n            document.getElementById('main-tab-burst-accounts')?.classList.remove('hidden');";
const APPS_REVEAL = "\n            document.getElementById('main-tab-apps')?.classList.remove('hidden');";
const cloudGate = revealGate(analyticsSrc, CLOUD_REVEAL);
const appsGate = revealGate(analyticsSrc, APPS_REVEAL);

// --- stub DOM -----------------------------------------------------------------
function makeDom() {
    const els = {};
    const doc = {
        getElementById: (id) => {
            const e = els[id] || (els[id] = {
                classes: new Set(['hidden']),
                style: { setProperty: (k, v) => { if (k === 'display') els[id].display = v; } },
                dataset: {},
                addEventListener: () => {},
                display: null,
            });
            e.classList = {
                remove: (c) => e.classes.delete(c),
                add: (c) => e.classes.add(c),
                toggle: (c) => e.classes.has(c) ? e.classes.delete(c) : e.classes.add(c),
            };
            return e;
        },
    };
    const visible = (id) => { const e = els[id]; return !!e && e.display !== 'none' && !e.classes.has('hidden'); };
    return { doc, visible, show: (id) => { doc.getElementById(id).classList.remove('hidden'); } };
}

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// --- 1. reveal gates (analytics.js checkUserRole) ------------------------------
function runGate(gateSrc, paid, isPlatformAdmin, isSuperAdmin) {
    const dom = makeDom();
    new Function('document', 'isPlatformAdmin', 'isSuperAdmin', '_ceEditionAn',
        gateSrc)(dom.doc, isPlatformAdmin, isSuperAdmin, !paid);
    return dom;
}
// owner 11-11 answer B: the Cloud tab is CE-visible - the reveal keeps ONLY the
// tenant-admin role gate, so both admin roles see it on CE; a tenant member does not.
check('CE + platform admin: burst-accounts tab REVEALED (owner 11-11 answer B)',
    runGate(cloudGate, false, true, false).visible('main-tab-burst-accounts'));
check('CE + super admin: burst-accounts tab REVEALED (owner 11-11 answer B)',
    runGate(cloudGate, false, false, true).visible('main-tab-burst-accounts'));
check('CE + tenant member: burst-accounts tab still hidden (role gate intact)',
    !runGate(cloudGate, false, false, false).visible('main-tab-burst-accounts'));
check('paid + platform admin: burst-accounts tab revealed',
    runGate(cloudGate, true, true, false).visible('main-tab-burst-accounts'));
check('paid + super admin: burst-accounts tab revealed',
    runGate(cloudGate, true, false, true).visible('main-tab-burst-accounts'));
check('paid + tenant user: burst-accounts tab still hidden (role gate intact)',
    !runGate(cloudGate, true, false, false).visible('main-tab-burst-accounts'));
check('CE + super admin: apps tab stays hidden',
    !runGate(appsGate, false, false, true).visible('main-tab-apps'));
check('CE + platform admin: apps tab stays hidden',
    !runGate(appsGate, false, true, false).visible('main-tab-apps'));
check('paid + super admin: apps tab revealed',
    runGate(appsGate, true, false, true).visible('main-tab-apps'));
check('paid + tenant user: apps tab still hidden (role gate intact)',
    !runGate(appsGate, true, false, false).visible('main-tab-apps'));

// --- 2. panel initializers (analytics.js switchToTab branches) -----------------
function initBranch(marker) {
    const mIdx = analyticsSrc.indexOf(marker);
    if (mIdx < 0) throw new Error('panel-init branch not found');
    const open = mIdx + marker.length - 1;
    const cond = marker.slice(marker.indexOf('if (') + 4, marker.lastIndexOf(')'));
    return `if (${cond}) ${blockAt(analyticsSrc, open)}`;
}
const initBa = initBranch("} else if (panelId === 'main-panel-burst-accounts') {");
const initApps = initBranch("} else if (panelId === 'main-panel-apps') {");

// Promise stubs: `resolved` models isPaidEdition() answering (paid true/false, the
// .catch handler is skipped); `rejected` models an unreachable /platform/features,
// whose .catch(() => false) handler RESULT decides the leg - so a mutant that flips
// the default to paid is caught here.
function resolvedEdition(paid) {
    return () => { const o = { catch: () => o, then: (cb) => cb(paid) }; return o; };
}
function rejectedEdition() {
    return () => ({ catch: (h) => ({ then: (cb) => cb(h()) }), then: () => undefined });
}
function promiseLike(value) {
    return { then: (cb) => promiseLike(cb(value)), catch: (h) => promiseLike(h()) };
}

function runInitBa(paid, edition) {
    const dom = makeDom();
    dom.show('main-panel-burst-accounts');
    let fetched = 0;
    new Function('document', 'panelId', 'isPaidEdition', 'renderBurstAccountsSection',
        initBa)(dom.doc, 'main-panel-burst-accounts', edition, () => { fetched++; });
    return { fetched, dom };
}
function runInitApps(paid, edition) {
    const dom = makeDom();
    dom.show('main-panel-apps');
    let fetched = 0;
    new Function('document', 'panelId', 'isPaidEdition', 'fetch', 'initializeAppsPage',
        '_appsPanelLoaded', initApps)(dom.doc, 'main-panel-apps', edition,
        (url) => { fetched++; return promiseLike({ ok: true, text: () => '<markup/>' }); },
        () => undefined, false);
    return { fetched, dom };
}
// owner 11-11 answer B: the Cloud initializer is NOT edition-gated any more - it
// reveals and renders in every edition, so an unknown/errored edition renders too
// (the old fail-closed leg is gone; the tenant-admin role gate is the only gate).
const baCe = runInitBa(false, resolvedEdition(false));
check('CE tab activation: renderBurstAccountsSection IS called (own-cloud burst usable on CE)',
    baCe.fetched === 1);
check('CE tab activation: burst-accounts panel stays visible (never re-hidden)',
    baCe.dom.visible('main-panel-burst-accounts'));
const baUnknown = runInitBa(false, rejectedEdition());
check('unknown edition (isPaidEdition rejects): burst-accounts panel still initialized (no edition gate)',
    baUnknown.fetched === 1 && baUnknown.dom.visible('main-panel-burst-accounts'));
check('paid tab activation: burst-accounts panel initialized (unchanged)',
    runInitBa(true, resolvedEdition(true)).fetched === 1);
const appsCe = runInitApps(false, resolvedEdition(false));
check('CE tab activation: apps markup never fetched', appsCe.fetched === 0);
check('CE tab activation: apps panel hidden', !appsCe.dom.visible('main-panel-apps'));
const appsUnknown = runInitApps(false, rejectedEdition());
check('unknown edition (isPaidEdition rejects): apps markup never fetched',
    appsUnknown.fetched === 0 && !appsUnknown.dom.visible('main-panel-apps'));
check('paid tab activation: apps markup fetched',
    runInitApps(true, resolvedEdition(true)).fetched === 1);
check('apps init sets _appsPanelLoaded before the async leg (no re-fetch per tab switch)',
    /_appsPanelLoaded = true;\s*isPaidEdition\(\)\.catch\(\(\) => false\)/.test(analyticsSrc));

// --- 3. deep-link fallback (?tab=burst-accounts / ?tab=apps on CE) -------------
// owner 11-11 answer B: burst-accounts is NO LONGER in the CE fallback clause (its
// deep link opens on CE); the tenantAdminOnly role gate below still falls back.
check('deep-link gate: burst-accounts NOT in the _ceEditionAn fallback clause (CE opens it)',
    !/_ceEditionAn\s*&&\s*\([^)]*burst-accounts[^)]*\)/.test(analyticsSrc));
check('deep-link gate: apps in the _ceEditionAn fallback clause',
    /_ceEditionAn\s*&&\s*\([^)]*apps[^)]*\)/.test(analyticsSrc));
check('deep-link role gate intact: apps still falls back for non-superadmins',
    /wanted === 'apps' && !isSuperAdmin/.test(analyticsSrc));

// --- 4. reveal statements in the shipped source (rc20 R10) ---------------------
// owner 11-11 answer B: the Cloud reveal dropped the edition gate - only the role
// gate remains, so the shipped statement must read `if (isPlatformAdmin || isSuperAdmin)`.
check('burst-accounts reveal statement is role-gated (NO edition gate) in the shipped source',
    /if \(isPlatformAdmin \|\| isSuperAdmin\)\s*\{\s*document\.getElementById\('main-tab-burst-accounts'\)/.test(analyticsSrc));
check('apps reveal statement is edition-gated in the shipped source',
    /if \(isSuperAdmin && !_ceEditionAn\)\s*\{\s*document\.getElementById\('main-tab-apps'\)/.test(analyticsSrc));

// --- 5. sibling operator Burst tab must NOT have been swept up by the gate -----
function runSiblingGate(paid, isPlatformAdmin) {
    const dom = makeDom();
    const sibSrc = revealGate(analyticsSrc,
        "\n            document.getElementById('main-tab-burst')?.classList.remove('hidden');");
    new Function('document', 'isPlatformAdmin', '_ceEditionAn',
        sibSrc)(dom.doc, isPlatformAdmin, !paid);
    return dom;
}
const sibCe = runSiblingGate(false, true);
check('CE: sibling burst tab stays hidden - the gate was not widened to the operator Burst tab',
    !sibCe.visible('main-tab-burst'));
check('CE: sibling reveal never touches the burst-accounts tab',
    !sibCe.visible('main-tab-burst-accounts'));
check('paid: sibling burst tab still revealed',
    runSiblingGate(true, true).visible('main-tab-burst'));

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
