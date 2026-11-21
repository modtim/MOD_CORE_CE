'use strict';
// FIX-DASH-CE-DEAD-NAV-3 — three Dashboard CE carves, one node check.
//
//   1) #admin-nodes (the Nodes page) has no CE route (GET /nodes/admin/system is
//      denied), so a deep link used to render the paid table with dead Grant/Revoke
//      buttons. Fix: 'admin-nodes' joins _CE_COV4B_PAGES (ui.js) -> the generic
//      "Not available in MOD Community Edition" panel, returned BEFORE the fetch.
//   2) #model-cache (the Model Cache Settings sub-tab) called the denied
//      /system/model-cache/* routes and painted the red "API not available" stats
//      row. Fix: 'model-cache' joins _CE_RETIRED_ADMIN_PAGES + the pinned-id map
//      (the page's own sweep witness container id, analytics.html:1520 <div id="mc-stats">),
//      so the amber panel ships that id and _hideCeRetiredAdminNav hides the nav row.
//   3) js/held-matches.js _allowedActions shipped an Escalate button on CE whose
//      re-hold PATCH + /platform/security-incidents POST are denied routes. Fix: the
//      edition is resolved once per load into _hmPaid (fail-closed: starts false,
//      api.js isPaidEdition reports CE on an unreadable payload) and the escalate
//      entry is appended only when positive; handleAction re-checks (defence in depth).
//
// Style of tests/drift-events-ce-retired.check.js and tests/sechub-ce-tabs-hidden.check.js:
// the REAL set + panel fn + edition predicate + nav helper + getPlatformFeatures are
// LIFTED out of js/ui.js and EXECUTED in a fresh node:vm sandbox per scenario with
// `fetch` stubbed — no browser, no npm deps, no re-implementation. The held-matches
// body is lifted the way tests/held-matches-page.test.js does and run with isPaidEdition
// stubbed per edition.
//
//   node CORE/Dashboard/tests/ce-dead-nav-3.check.js
//
// Cases:
//   T1 static: 'admin-nodes' in _CE_COV4B_PAGES; 'model-cache' in the retired set
//      pinned to mc-stats; the nav helper runs; held-matches.js imports isPaidEdition
//      and gates escalate on _hmPaid in BOTH _allowedActions and handleAction
//   T2 CE -> #admin-nodes ships the panel, admin-nodes.js loader never reached
//   T3 paid -> gate does not fire, the Nodes loader IS reached (paid unchanged)
//   T4 unreadable payload -> fail-closed CE
//   T5 nav: CE hides #model-cache-nav-link; paid keeps the role gate; unknown hides
//   T6 CE -> #model-cache ships the amber panel carrying mc-stats, model-cache.js
//      never fetches the denied routes
//   T7 paid -> #model-cache untouched, the panel loader IS reached
//   T8 held-matches: CE / unknown -> no 'escalate' for pending|active|deactivated;
//      paid -> the shipped three arrays are unchanged
//   T9 held-matches handleAction: CE -> an escalate click sends nothing

const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const ui = fs.readFileSync(path.join(ROOT, 'js', 'ui.js'), 'utf8');
const api = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');
const hm = fs.readFileSync(path.join(ROOT, 'js', 'held-matches.js'), 'utf8');

// CE-extract bail (FIX-CE-DASH-SWEEP-D): this check lifts the retired/cov4b page
// mechanism for pages whose loaders the CE cut denies — js/model-cache.js (and the
// drift/overage loaders) are absent from the extract, so the liftable shape no longer
// carries the gate this check drives. Bail ONLY when that source is absent; on the paid
// tree every assert below runs unchanged.
if (!fs.existsSync(path.join(ROOT, 'js', 'model-cache.js'))) {
    console.log('SKIP ce-dead-nav-3: js/model-cache.js is not in this tree '
        + '(CE cut: the page is denied, the mechanism is not in the liftable shape)');
    process.exit(0);
}

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
function liftGate(src, marker) {
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const j = src.indexOf('\n        return;\n    }', i);
    if (j < 0) return null;
    return src.slice(i, j + '\n        return;\n    }'.length);
}

// FIX-CE-RETIRED-GATE-LIFT (sweep D 854a33e635): paid ui.js now gates the retired-admin
// carve through the DERIVED helper — `if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())`
// (ui.js:878) — so the old literal marker below is gone and liftGate() returns null. Re-point:
// when the old marker is absent, lift the NEW form instead, together with the helper defs it
// calls (_ceDeniedPageId / _ceRetiredAdminGate, brace-matched from their exact headers, ui.js:469-485)
// and a registry stand-in for _ceRegisteredRows() built from the SAME ui.js Sets the helper's
// scrub fallback reads (the paid tree's ui-manifest.js rows are those same rows; the CE extract
// never imports the manifest). The whole block goes INSIDE the sandbox gate fn, so it shadows
// nothing and the evaluated gate behaves exactly as with the old literal marker.
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
    // CE-NOPAID-4.2 (leg B3): ui.js now reads the registry through the local
    // getPageEntry() wrapper over _ceRegisteredRows(); stand it in the SAME shape.
    return 'const _ceRegisteredRows = () => ({' + rows.join(', ') + '});\n'
        + 'const getPageEntry = (id) => _ceRegisteredRows()[id];\n'
        + decls.join('\n') + '\n' + deniedFn + '\n' + helperFn + '\n' + gate;
}

const cov4bSet = liftSet(ui, '_CE_COV4B_PAGES');
const retiredSet = liftSet(ui, '_CE_RETIRED_ADMIN_PAGES');
const panelFn = lift(ui, 'function _ceRetiredAdminPanelHtml(pageId)');
const paidPanelFn = lift(ui, 'function _cePaidPagePanelHtml()');
const isCeFn = lift(ui, 'function _isCeEdition()');
const stateFn = lift(ui, 'function _ceEditionState()');
let navFn = lift(ui, 'function _hideCeRetiredAdminNav()');
const apiLift = lift(api, 'const _featuresCache = {');
const retiredGateOld = liftGate(ui, 'if (_CE_RETIRED_ADMIN_PAGES.has(targetPageId)');
const retiredGateNew = _liftRetiredGateNewForm(ui);
const retiredGate = retiredGateOld || retiredGateNew;

// FIX-CE-RETIRED-GATE-LIFT: the nav helper's ids are now DERIVED too — _hideCeRetiredAdminNav
// walks _ceRetiredAdminNavIds() (ui.js:527), which unions _CE_RETIRED_ADMIN_PAGES with
// _ceDeniedPageIds() (ui-manifest.js, never imported on CE). Lift the nav-ids fn and stand in
// the denied-ids fn from the SAME ui.js Sets (the paid registry rows are those rows), so the
// nav scenario evaluates as it does in ui.js. Unused on a tree that still has the old shape.
function _navShim(src) {
    const navIdsFn = _liftFn(src, 'function _ceRetiredAdminNavIds() {');
    if (!navIdsFn) return '';
    const classes = ['_CE_COV4B_PAGES', '_CE_PAID_HUB_PAGES', '_CE_COMPLIANCE_PAGES',
        '_CE_PAID_COMPLIANCE_PAGES', '_CE_RETIRED_ADMIN_PAGES'];
    const seen = [];
    for (const nm of classes) {
        const ids = _setIds(src, nm);
        if (!ids) continue;
        for (const id of ids) if (!seen.includes(id)) seen.push(id);
    }
    return 'const _ceDeniedPageIds = () => [' + seen.map((x) => `'${x}'`).join(', ') + '];\n'
        + navIdsFn;
}
const navShim = _navShim(ui);
const cov4bGate = liftGate(ui, 'if (_CE_COV4B_PAGES.has(targetPageId)');
if (!cov4bSet || !retiredSet || !panelFn || !paidPanelFn || !isCeFn || !stateFn
    || !apiLift || !retiredGate || !cov4bGate) {
    console.error('FAIL could not lift the CE sets/panel fns/predicate/nav helper/'
        + 'getPlatformFeatures/the gates — the mechanism is not in the liftable shape');
    process.exit(1);
}
if (!navFn) { navFn = 'function _hideCeRetiredAdminNav() { return Promise.resolve(null); }'; }

// --- the page-gate scenario runner (fresh sandbox per scenario: api.js caches the payload) --
async function pageScenario(payload, pageId, loaderName) {
    const RESULT = { panel: null, loaderCalled: false, title: null };
    const pageContent = { innerHTML: null };
    const sandbox = {
        console: console,
        document: {
            title: '',
            getElementById: (id) => (id === 'page-content' ? pageContent : { style: {} }),
        },
        window: { location: { href: `http://dash.localhost/#${pageId}` } },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
    };
    sandbox[loaderName] = () => { RESULT.loaderCalled = true; };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + cov4bSet + '\n' + retiredSet + '\n' + panelFn + '\n' + paidPanelFn + '\n'
        + stateFn + '\n' + isCeFn + '\n' + apiLift + '\n'
        + `(async () => {
              const targetPageId = '${pageId}';
              async function _retiredGate() { ${retiredGate} }
              async function _cov4bGate() { ${cov4bGate} }
              await _retiredGate();
              await _cov4bGate();
              RESULT.panel = pageContent.innerHTML;
              RESULT.title = document.title;
              if (RESULT.panel === null) { ${loaderName}(); }
           })()`;
    sandbox.RESULT = RESULT;
    sandbox.pageContent = pageContent;
    try {
        await vm.runInContext(src, sandbox, { filename: `ce-dead-nav-3-${pageId}.js` });
    } catch (e) {
        RESULT.error = String(e && (e.message || e));
        console.error(`# runner-error(${pageId}): ${RESULT.error}`);
    }
    return RESULT;
}

async function navScenario(payload, roleGate) {
    const links = { 'model-cache-nav-link': { style: { display: roleGate } } };
    const sandbox = {
        console: console,
        document: { getElementById: (id) => links[id] },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        _syncNavGroupHeadings: () => {},
    };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + retiredSet + '\n' + stateFn + '\n' + isCeFn + '\n' + navFn + '\n' + navShim + '\n' + apiLift + '\n'
        + '_hideCeRetiredAdminNav();';
    try {
        await vm.runInContext(src, sandbox, { filename: 'ce-dead-nav-3-nav.js' });
    } catch (e) {
        console.error(`# nav-runner-error: ${String(e && (e.message || e))}`);
    }
    return links['model-cache-nav-link'].style.display;
}

// --- the held-matches _allowedActions (REAL source, flag injected per scenario) --
const hmMarker = hm.indexOf('function _allowedActions(status)');
const hmEnd = hm.indexOf('\n}', hmMarker);
if (hmMarker < 0 || hmEnd < 0) {
    console.error('FAIL could not lift the held-matches _allowedActions');
    process.exit(1);
}
const HM_SRC = hm.slice(hmMarker, hmEnd + 2);

function actionsOn(paid) {
    return vm.runInNewContext(
        `let _hmPaid = ${paid ? 'true' : 'false'};\n` + HM_SRC
        + '\n({ pending: _allowedActions("pending"),'
        + ' active: _allowedActions("active"), deactivated: _allowedActions("deactivated") });',
        {}, { filename: 'held-matches-allowed-actions', timeout: 5000 });
}

const CE = { entitlements: { edition: 'ce' } };
const PAID = { entitlements: { edition: 'saas' } };
const UNKNOWN = { entitlements: { edition: 'ce', error: 'unresolved' } };
const SHIPPED_PAID = {
    pending: ['release', 'reject', 'escalate'],
    active: ['reject', 'escalate'],
    deactivated: ['release', 'escalate'],
};

(async () => {
    // --- T1 static ------------------------------------------------------------
    check('T1a _CE_COV4B_PAGES carries admin-nodes', /'admin-nodes'/.test(cov4bSet));
    check('T1b _CE_RETIRED_ADMIN_PAGES carries model-cache', /'model-cache'/.test(retiredSet));
    check('T1c the retired panel pins model-cache to mc-stats',
        /'model-cache':\s*'mc-stats'/.test(ui));
    check('T1d the retired nav helper runs inside updateUIVisibility',
        ui.indexOf('_hideCeRetiredAdminNav();') > ui.indexOf('export async function updateUIVisibility()'));
    check('T1e held-matches.js imports isPaidEdition from ./api.js',
        /^import \{[^}]*\bisPaidEdition\b[^}]*\} from '\.\/api\.js';/m.test(hm));
    check('T1f _allowedActions gates escalate on _hmPaid',
        /const esc = _hmPaid \? \['escalate'\] : \[\];/.test(hm)
            && /return \['release', 'reject'\]\.concat\(esc\);/.test(hm));
    check('T1g handleAction re-checks escalate (defence in depth)',
        /if \(action === 'escalate'\) \{\s*await _resolvePaidEdition\(\);\s*if \(!_hmPaid\) \{/.test(hm));
    check('T1h the flag starts fail-closed (CE)', /let _hmPaid = false;/.test(hm));

    // --- T2 admin-nodes on CE -------------------------------------------------
    const nodesCe = await pageScenario(CE, 'admin-nodes', 'initializeAdminNodesPage');
    check('T2a CE ships the admin-nodes panel', !!nodesCe.panel);
    check('T2b the panel says Not available in MOD Community Edition',
        !!nodesCe.panel && nodesCe.panel.includes('Not available in MOD Community Edition'));
    check('T2c CE ships NO grant/revoke buttons',
        !!nodesCe.panel && !nodesCe.panel.includes('grant') && !nodesCe.panel.includes('revoke'));
    check('T2d CE never reaches initializeAdminNodesPage() (no denied fetch)',
        !nodesCe.loaderCalled);

    // --- T3 admin-nodes paid: untouched ---------------------------------------
    const nodesPaid = await pageScenario(PAID, 'admin-nodes', 'initializeAdminNodesPage');
    check('T3a paid does NOT fire the CE gate', nodesPaid.panel === null);
    check('T3b paid reaches the Nodes loader', nodesPaid.loaderCalled);

    // --- T4 unreadable payload -> fail-closed ---------------------------------
    const nodesUnknown = await pageScenario(UNKNOWN, 'admin-nodes', 'initializeAdminNodesPage');
    check('T4a unknown entitlements still carve admin-nodes', !!nodesUnknown.panel);
    check('T4b unknown entitlements never run the paid loader', !nodesUnknown.loaderCalled);

    // --- T5 nav: model-cache sidebar row --------------------------------------
    check('T5a CE hides #model-cache-nav-link', (await navScenario(CE, 'flex')) === 'none');
    check('T5b paid keeps the role gate', (await navScenario(PAID, 'flex')) === 'flex');
    check('T5c unknown entitlements hide the nav (fail-closed)',
        (await navScenario(UNKNOWN, 'flex')) === 'none');

    // --- T6 model-cache on CE -------------------------------------------------
    const mcCe = await pageScenario(CE, 'model-cache', 'initializeModelCachePanel');
    check('T6a CE ships the retired model-cache panel', !!mcCe.panel);
    check('T6b the panel carries the pinned witness mc-stats',
        !!mcCe.panel && mcCe.panel.includes('<div id="mc-stats"></div>'));
    check('T6c the panel explains the page',
        !!mcCe.panel && mcCe.panel.includes('Not available in MOD Community Edition'));
    check('T6d CE never reaches the Model Cache loader', !mcCe.loaderCalled);

    // --- T7 model-cache paid: untouched ---------------------------------------
    const mcPaid = await pageScenario(PAID, 'model-cache', 'initializeModelCachePanel');
    check('T7a paid does NOT fire the retired gate', mcPaid.panel === null);

    // --- T8 held-matches actions per edition ----------------------------------
    const ceActions = actionsOn(false);
    ['pending', 'active', 'deactivated'].forEach((st) => {
        check(`T8a CE offers no 'escalate' for ${st}`, !ceActions[st].includes('escalate'));
        check(`T8b CE keeps the release/reject decisions for ${st}`,
            ceActions[st].every((a) => a === 'release' || a === 'reject'));
    });
    const paidActions = actionsOn(true);
    Object.keys(SHIPPED_PAID).forEach((st) => check(
        `T8c paid keeps the shipped three arrays for ${st} (unchanged)`,
        JSON.stringify(paidActions[st]) === JSON.stringify(SHIPPED_PAID[st])));

    // --- T9 held-matches handleAction refuses on CE ---------------------------
    const hmBody = hm.slice(hm.indexOf('async function handleAction(action, userId) {'));
    const refuseIdx = hmBody.indexOf('Escalate is not offered on MOD Community Edition');
    const callIdx = hmBody.indexOf('heldMatches.escalate(userId)');
    check('T9a the CE refusal is reported before the escalate call',
        refuseIdx >= 0 && callIdx > refuseIdx);

    const total = 8 + 4 + 2 + 2 + 3 + 4 + 1 + 6 + 3 + 1;
    console.log(`\n  ce-dead-nav-3: ${total - failures}/${total} passed`);
    if (failures) { process.exitCode = 1; }
})();

