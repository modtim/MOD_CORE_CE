'use strict';
// rc20-uijs-drift — #drift-events is a CE-DENIED admin-facing hash alias: its backing
// router (CORE/API/routers/drift_events.py, denied by the open-core/manifest.yaml glob)
// 404s on CE, so a CE visit used to fetch GET /v1/drift/events and js/drift-events.js
// painted the red API_ERROR_STATE row (#drift-api-error, "Drift events API not
// available on this deployment") instead of the house amber panel, and the sidebar
// entry stayed offered (ui.js:1766 role gate only, no edition branch).
//
// The CORRECT denied shape is the SAME mechanism every other retired admin alias uses:
// 'drift-events' in _CE_RETIRED_ADMIN_PAGES (ui.js:414 set) + pinned to the page's own
// sweep witness container id 'drift-api-error' (CORE/Dashboard/pages/drift-events.html:12
// — the one id the drift sweep + dash-ce-batch7 CE leg both look for, so the CE leg keeps
// a POSITIVE witness: the notice SHIPS the id, empty) + the gate at ui.js:701, which
// returns BEFORE the `targetPageId === 'drift-events' -> initializeDriftEventsPage()`
// branch (~:880), + _hideCeRetiredAdminNav() hiding #drift-events-nav-link on CE with the
// same fail-closed _isCeEdition predicate.
//
// Style of tests/overage-caps-ce-retired.check.js (its sibling): the REAL set + panel fn
// + edition predicate + nav helper + getPlatformFeatures are LIFTED out of js/ui.js and
// EXECUTED in a fresh node:vm sandbox per scenario with `fetch` stubbed — no browser, no
// npm deps, no re-implementation.
//
//   node CORE/Dashboard/tests/drift-events-ce-retired.check.js
//
// Cases:
//   T1 static: 'drift-events' is in _CE_RETIRED_ADMIN_PAGES and pinned to
//      'drift-api-error'; the nav helper exists and runs inside updateUIVisibility;
//      the CE gate precedes the initializeDriftEventsPage branch
//   T2 CE payload  -> gate fires (panel ships drift-api-error, explains the page,
//      ships NO paid drift list controls) and initializeDriftEventsPage() is NEVER
//      reached (no denied fetch)
//   T3 paid payload (saas) -> gate does NOT fire; the loader branch IS reached
//   T4 unreadable payload -> CE (fail-closed): retired, loader never reached
//   T5 nav: CE hides #drift-events-nav-link; paid leaves the role gate's value;
//      unknown hides (fail-closed)
//
// RED on the pre-fix ui.js (T1/T2/T4/T5: 'drift-events' absent from the set and the
// pinned map, no nav helper, so the gate never fires and the paid loader runs on CE).

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const ui = fs.readFileSync(path.join(ROOT, 'js', 'ui.js'), 'utf8');
const api = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

function liftSet(src, name) {
    const i = src.indexOf(`const ${name} = new Set([`);
    if (i < 0) return null;
    const j = src.indexOf('\n]);', i);
    if (j < 0) return null;
    return src.slice(i, j + 4);
}
function lift(src, marker) {
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const j = src.indexOf('\n}', i);
    if (j < 0) return null;
    return src.slice(i, j + 2).replace(/^export /gm, '');
}
function liftGate(src) {
    const i = src.indexOf('if (_CE_RETIRED_ADMIN_PAGES.has(targetPageId)');
    if (i < 0) return null;
    const j = src.indexOf('\n        return;\n    }', i);
    if (j < 0) return null;
    return src.slice(i, j + '\n        return;\n    }'.length);
}

// FIX-CE-RETIRED-GATE-LIFT (sweep D 854a33e635): paid ui.js now gates the retired-admin
// carve through the DERIVED helper — `if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())`
// (ui.js:878) — so the literal marker above is gone and liftGate() returns null. Re-point: when
// the old marker is absent, lift the NEW gate form plus the helper defs it calls (_ceDeniedPageId
// / _ceRetiredAdminGate, brace-matched from their exact headers) and a registry stand-in for
// _ceRegisteredRows() built from the SAME ui.js Sets the helper's scrub fallback reads (the paid
// ui-manifest.js rows are those same rows; the CE extract never imports the manifest). The block
// goes INSIDE the sandbox gate fn, so the evaluated gate behaves as with the old literal marker.
function _braceEnd(src, from) {
    let depth = 0;
    for (let k = from; k < src.length; k++) {
        const c = src[k];
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) return k + 1; }
    }
    return -1;
}
function _liftFn(src, header) {
    const i = src.indexOf(header);
    if (i < 0) return null;
    const open = src.indexOf('{', i);
    if (open < 0) return null;
    const end = _braceEnd(src, open);
    if (end < 0) return null;
    return src.slice(i, end);
}
function _setIds(src, name) {
    const s = liftSet(src, name);
    if (!s) return null;
    const clean = s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
    return [...clean.matchAll(/'([a-z0-9-]+)'/g)].map((m) => m[1]);
}
function _liftRetiredGateNewForm(src) {
    const i = src.indexOf('if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())');
    if (i < 0) return null;
    const j = src.indexOf('\n        return;\n    }', i);
    if (j < 0) return null;
    const gate = src.slice(i, j + '\n        return;\n    }'.length);
    const deniedFn = _liftFn(src, 'function _ceDeniedPageId(pageId) {');
    const helperFn = _liftFn(src, 'function _ceRetiredAdminGate(pageId) {');
    if (!deniedFn || !helperFn) return null;
    const classes = ['_CE_COV4B_PAGES', '_CE_PAID_HUB_PAGES', '_CE_COMPLIANCE_PAGES',
        '_CE_PAID_COMPLIANCE_PAGES'];
    const ids = {};
    for (const nm of classes) ids[nm] = _setIds(src, nm);
    const retired = _setIds(src, '_CE_RETIRED_ADMIN_PAGES');
    if (!retired) return null;
    const rows = retired.map((id) => `'${id}': {id: '${id}', gate: 'retired-admin', paid: true}`);
    for (const nm of classes) {
        if (!ids[nm]) continue;
        for (const id of ids[nm]) {
            if (!retired.includes(id)) rows.push(`'${id}': {id: '${id}', gate: '${nm}', paid: true}`);
        }
    }
    const decls = classes.filter((nm) => ids[nm])
        .map((nm) => `const ${nm} = new Set([${ids[nm].map((x) => `'${x}'`).join(', ')}]);`);
    // CE-NOPAID-4.2 (leg B3): ui.js reads the registry through the local getPageEntry()
    // wrapper over _ceRegisteredRows(); stand it in the SAME shape.
    return 'const _ceRegisteredRows = () => ({' + rows.join(', ') + '});\n'
        + 'const getPageEntry = (id) => _ceRegisteredRows()[id];\n'
        + decls.join('\n') + '\n' + deniedFn + '\n' + helperFn + '\n' + gate;
}

const retiredSet = liftSet(ui, '_CE_RETIRED_ADMIN_PAGES');
const panelFn = lift(ui, 'function _ceRetiredAdminPanelHtml(pageId)');
const isCeFn = lift(ui, 'function _isCeEdition()');
const stateFn = lift(ui, 'function _ceEditionState()');
let navFn = lift(ui, 'function _hideCeRetiredAdminNav()');
const apiLift = lift(api, 'const _featuresCache = {');
// CE-extract bail (FIX-CE-DASH-SWEEP-D): #drift-events is denied in the CE cut — its
// loader js/drift-events.js and pages/drift-events.html are absent from the extract, so
// the retired-page gate this check drives is no longer in the liftable shape. Bail ONLY
// when that source is absent; on the paid tree every assert below runs unchanged.
if (!fs.existsSync(path.join(ROOT, 'js', 'drift-events.js'))) {
    console.log('SKIP drift-events-ce-retired: js/drift-events.js is not in this tree '
        + '(CE cut: the drift-events page is denied, the mechanism is not in the liftable shape)');
    process.exit(0);
}

const gateLift = liftGate(ui) || _liftRetiredGateNewForm(ui);

// FIX-CE-RETIRED-GATE-LIFT: the nav helper's ids are now DERIVED too — _hideCeRetiredAdminNav
// walks _ceRetiredAdminNavIds() (ui.js:527), which unions _CE_RETIRED_ADMIN_PAGES with
// _ceDeniedPageIds() (ui-manifest.js, never imported on CE). Lift the nav-ids fn and stand in
// the denied-ids fn from the SAME ui.js Sets (the paid registry rows are those rows), so the
// nav scenario evaluates as it does in ui.js. Unused on a tree that still has the old shape.
function _navShim(src) {
    const navIdsFn = _liftFn(src, 'function _ceRetiredAdminNavIds() {');
    if (!navIdsFn) return '';
    const seen = [];
    const add = (x) => { if (!seen.includes(x)) seen.push(x); };
    // The REAL denied ids on the paid tree: ui-manifest.js rows marked `paid: true`
    // (exactly what _ceDeniedPageIds() returns). Fall back to the ui.js Sets when the
    // manifest is absent (the CE extract never imports it — these checks SKIP there).
    const mfPath = path.join(ROOT, 'js', 'ui-manifest.js');
    const mfPaidPath = path.join(ROOT, '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
    if (fs.existsSync(mfPath)) {
        const mf = fs.readFileSync(mfPath, 'utf8')
            + (fs.existsSync(mfPaidPath) ? '\n' + fs.readFileSync(mfPaidPath, 'utf8') : '');
        for (const m of mf.matchAll(/registerPage(?:Alias)?\(\{id: '([a-z0-9-]+)'[^}]*paid: true\}/g)) add(m[1]);
    }
    const classes = ['_CE_COV4B_PAGES', '_CE_PAID_HUB_PAGES', '_CE_COMPLIANCE_PAGES',
        '_CE_PAID_COMPLIANCE_PAGES', '_CE_RETIRED_ADMIN_PAGES'];
    for (const nm of classes) {
        const ids = _setIds(src, nm);
        if (!ids) continue;
        for (const id of ids) add(id);
    }
    return 'const _ceDeniedPageIds = () => [' + seen.map((x) => `'${x}'`).join(', ') + '];\n'
        + navIdsFn;
}
const navShim = _navShim(ui);

// The nav helper is NEW in this job, so it is legitimately absent on the pre-fix copy:
// stub it (a no-op) instead of aborting, so the OLD copy still runs every scenario and
// fails on the drift assertions themselves (T1d/T2*/T4*/T5*), not on a lift error.
if (!navFn) { navFn = 'function _hideCeRetiredAdminNav() { return Promise.resolve(null); }'; }
if (!retiredSet || !panelFn || !isCeFn || !stateFn || !apiLift || !gateLift) {
    console.error('FAIL could not lift _CE_RETIRED_ADMIN_PAGES/_ceRetiredAdminPanelHtml/'
        + '_isCeEdition/_ceEditionState/_hideCeRetiredAdminNav/getPlatformFeatures/the CE gate — '
        + 'the retired-page mechanism is not in the liftable shape');
    process.exit(1);
}

// --- T1: static pins ----------------------------------------------------------
// FIX-CE-RETIRED-GATE-LIFT: CE-REGGATE-1 turned the set into a witness-shape list and the
// gate into the registry-derived predicate, so the denied drift view is DELIBERATELY absent
// from _CE_RETIRED_ADMIN_PAGES (its registry row denies it). Accept either form.
const mfPathForDrift = path.join(ROOT, 'js', 'ui-manifest.js');
const mfPaidPathForDrift = path.join(ROOT, '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
const mfForDrift = (fs.existsSync(mfPathForDrift) ? fs.readFileSync(mfPathForDrift, 'utf8') : '')
    + (fs.existsSync(mfPaidPathForDrift) ? '\n' + fs.readFileSync(mfPaidPathForDrift, 'utf8') : '');
check('T1a _CE_RETIRED_ADMIN_PAGES carries drift-events',
    /'drift-events'/.test(retiredSet)
    || /registerPage\(\{id: 'drift-events'[^}]*gate: 'retired-admin'[^}]*paid: true\}/.test(mfForDrift));
check('T1b the panel pins drift-events to drift-api-error',
    /'drift-events':\s*'drift-api-error'/.test(ui));
// FIX-CE-RETIRED-GATE-LIFT: the gate moved to the derived helper, so accept either form.
const retiredGatePos = ui.indexOf('if (_CE_RETIRED_ADMIN_PAGES.has(targetPageId)');
const retiredGatePosNew = ui.indexOf('if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())');
const retiredGatePosAny = retiredGatePos >= 0 ? retiredGatePos : retiredGatePosNew;
check('T1c the CE gate runs before the initializeDriftEventsPage branch',
    retiredGatePosAny >= 0 && retiredGatePosAny < ui.indexOf('initializeDriftEventsPage();'));
check('T1d the nav helper runs inside updateUIVisibility after the drift role gate',
    ui.indexOf('export async function updateUIVisibility()') > 0
    && ui.indexOf('_hideCeRetiredAdminNav();') > ui.indexOf("getElementById('drift-events-nav-link')")
    && ui.indexOf('_hideCeRetiredAdminNav();') > ui.indexOf('export async function updateUIVisibility()'));

// --- the page-gate scenario runner -------------------------------------------
async function scenario(payload) {
    const RESULT = { panel: null, loaderCalled: false, title: null };
    const pageContent = { innerHTML: null };
    const sandbox = {
        console: console,
        document: {
            title: '',
            getElementById: (id) => (id === 'page-content' ? pageContent : { style: {} }),
        },
        window: { location: { href: 'http://dash.localhost/#drift-events' } },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        initializeDriftEventsPage: () => { RESULT.loaderCalled = true; },
    };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + retiredSet + '\n' + panelFn + '\n' + stateFn + '\n' + isCeFn + '\n'
        + apiLift + '\n'
        + `(async () => {
              const targetPageId = 'drift-events';
              async function _ceGate() { ${gateLift} }
              await _ceGate();
              RESULT.panel = pageContent.innerHTML;
              RESULT.title = document.title;
              if (RESULT.panel === null) { initializeDriftEventsPage(); }
           })()`;
    sandbox.RESULT = RESULT;
    sandbox.pageContent = pageContent;
    try {
        await vm.runInContext(src, sandbox, { filename: 'drift-events-ce-retired.js' });
    } catch (e) {
        RESULT.error = String(e && (e.message || e));
        console.error(`# runner-error: ${RESULT.error}`);
    }
    return RESULT;
}

// --- the nav scenario runner (role gate already set the link, then the helper) ---
async function navScenario(payload, roleGate) {
    const links = { 'drift-events-nav-link': { style: { display: roleGate } } };
    const sandbox = {
        console: console,
        document: { getElementById: (id) => links[id] },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        // The helper re-syncs group headings after hiding; that DOM walk is out of
        // this check's scope (it only proves the row's display flips), so it is stubbed.
        _syncNavGroupHeadings: () => {},
    };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + retiredSet + '\n' + stateFn + '\n' + isCeFn + '\n' + navFn + '\n' + navShim + '\n'
        + apiLift + '\n'
        + '_hideCeRetiredAdminNav();';
    try {
        await vm.runInContext(src, sandbox, { filename: 'drift-events-nav-ce.js' });
    } catch (e) {
        console.error(`# nav-runner-error: ${String(e && (e.message || e))}`);
    }
    return links['drift-events-nav-link'].style.display;
}

const CE = { entitlements: { edition: 'ce' } };
const PAID = { entitlements: { edition: 'saas' } };

(async () => {
    // --- T2: CE -> retired panel, loader never reached ------------------------
    const ce = await scenario(CE);
    check('T2a CE ships the retired panel', !!ce.panel);
    check('T2b CE panel carries the pinned witness drift-api-error',
        !!ce.panel && ce.panel.includes('<div id="drift-api-error"></div>'));
    check('T2c CE panel explains the page',
        !!ce.panel && ce.panel.includes('Not available in MOD Community Edition'));
    check('T2d CE panel ships NO paid drift list controls',
        !!ce.panel && !ce.panel.includes('drift-loading')
        && !ce.panel.includes('drift-content') && !ce.panel.includes('drift-disabled'));
    check('T2e CE never reaches initializeDriftEventsPage()', !ce.loaderCalled);

    // --- T3: paid -> untouched -----------------------------------------------
    const paid = await scenario(PAID);
    check('T3a paid does NOT fire the retired gate', paid.panel === null);
    check('T3b paid reaches the drift-events loader', paid.loaderCalled);

    // --- T4: unreadable payload -> fail-closed (CE) --------------------------
    const unknown = await scenario({ entitlements: { edition: 'ce', error: 'unresolved' } });
    check('T4a unknown entitlements still retire the page', !!unknown.panel);
    check('T4b unknown entitlements never run the paid loader', !unknown.loaderCalled);

    // --- T5: nav --------------------------------------------------------------
    check('T5a CE hides #drift-events-nav-link',
        (await navScenario(CE, 'flex')) === 'none');
    check('T5b paid keeps the role gate (flex for a platform admin)',
        (await navScenario(PAID, 'flex')) === 'flex');
    check('T5c unknown entitlements hide the nav (fail-closed)',
        (await navScenario({ entitlements: { edition: 'ce', error: 'unresolved' } }, 'flex')) === 'none');

    const total = 4 + 5 + 2 + 2 + 3;
    console.log(`\n  ${total - failures}/${total} passed`);
    if (failures) { process.exitCode = 1; }
})();
