'use strict';
// rc20-overcap-fix — overage-caps is a CE-DENIED admin hash alias: its backing
// /admin/overage-cap/* + /billing/overage-cap/* routes 404 on CE, so the page used
// to ship paid markup (#default-cap-input / #save-default-cap-btn) and paint
// "Failed to load: HTTP 404" into #default-cap-status. The CORRECT denied shape is
// the house _CE_RETIRED_ADMIN_PAGES panel (ui.js:414 set + :699 gate, which returns
// BEFORE the initializeOverageCapsPage() branch at :969), pinned to the page's own
// container id default-cap-status (CORE/Dashboard/pages/overage-caps.html:46).
//
// Style of tests/failopen-ui-gates.check.js: the REAL gate + panel + edition
// predicate are LIFTED out of js/ui.js and EXECUTED in a fresh node:vm sandbox per
// scenario with `fetch` stubbed — no browser, no npm deps, no re-implementation.
//
//   node CORE/Dashboard/tests/overage-caps-ce-retired.check.js
//
// Cases:
//   T1 static: 'overage-caps' is in _CE_RETIRED_ADMIN_PAGES and pinned to
//      'default-cap-status' in the panel's map; the gate precedes the loader branch
//   T2 CE payload  -> gate fires (panel ships default-cap-status, NO paid controls)
//      and initializeOverageCapsPage() is NEVER reached
//   T3 paid payload (saas) -> gate does NOT fire; the loader branch is reached
//   T4 unreadable payload -> CE (fail-closed): retired, loader never reached
//
// RED on the pre-fix ui.js (T1/T2/T4: 'overage-caps' absent from the set, so the
// gate never fires and the paid loader runs on CE).

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

// FIX-CE-RETIRED-GATE-LIFT (sweep D 854a33e635; same re-point as
// ce-dead-nav-3 / drift-events-ce-retired): paid ui.js now gates the retired-admin
// carve through the DERIVED helper — `if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())`
// (ui.js:878, helper at ui.js:475) — so the literal marker above is gone and liftGate()
// returns null. Re-point: when the old marker is absent, lift the NEW gate form plus the
// helper defs it calls (_ceDeniedPageId / _ceRetiredAdminGate, brace-matched from their exact
// headers) and a registry stand-in for _ceRegisteredRows() built from the SAME ui.js `_CE_*`
// Sets the helper's scrub fallback reads (the paid ui-manifest.js rows are those same rows;
// the CE extract never imports the manifest). The block goes INSIDE the sandbox gate fn.
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
const apiLift = lift(api, 'const _featuresCache = {');
// CE-extract bail (FIX-CE-DASH-SWEEP-D): #overage-caps is denied in the CE cut — its
// loader js/overage-caps.js and pages/overage-caps.html are absent from the extract, so
// the retired-page gate this check drives is no longer in the liftable shape. Bail ONLY
// when that source is absent; on the paid tree every assert below runs unchanged.
if (!fs.existsSync(path.join(ROOT, 'js', 'overage-caps.js'))) {
    console.log('SKIP overage-caps-ce-retired: js/overage-caps.js is not in this tree '
        + '(CE cut: the overage-caps page is denied, the mechanism is not in the liftable shape)');
    process.exit(0);
}

const gateLift = liftGate(ui) || _liftRetiredGateNewForm(ui);

if (!retiredSet || !panelFn || !isCeFn || !stateFn || !apiLift || !gateLift) {
    console.error('FAIL could not lift _CE_RETIRED_ADMIN_PAGES/_ceRetiredAdminPanelHtml/'
        + '_isCeEdition/_ceEditionState/getPlatformFeatures/the CE gate — '
        + 'the retired-page gate is not in the liftable shape');
    process.exit(1);
}

// --- T1: static pins ----------------------------------------------------------
check('T1a _CE_RETIRED_ADMIN_PAGES carries overage-caps',
    /'overage-caps'/.test(retiredSet));
check('T1b the panel pins overage-caps to default-cap-status',
    /'overage-caps':\s*'default-cap-status'/.test(ui));
// FIX-CE-RETIRED-GATE-LIFT: the gate moved to the derived helper, so accept either form.
const retiredGatePos = ui.indexOf('if (_CE_RETIRED_ADMIN_PAGES.has(targetPageId)');
const retiredGatePosNew = ui.indexOf('if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())');
const retiredGatePosAny = retiredGatePos >= 0 ? retiredGatePos : retiredGatePosNew;
check('T1c the CE gate runs before the initializeOverageCapsPage branch',
    retiredGatePosAny >= 0 && retiredGatePosAny < ui.indexOf('initializeOverageCapsPage();'));

// --- the scenario runner ------------------------------------------------------
async function scenario(payload) {
    const RESULT = { panel: null, loaderCalled: false, title: null };
    const pageContent = { innerHTML: null };
    const sandbox = {
        console: console,
        document: {
            title: '',
            getElementById: (id) => (id === 'page-content' ? pageContent : { style: {} }),
        },
        window: { location: { href: 'http://dash.localhost/#overage-caps' } },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        initializeOverageCapsPage: () => { RESULT.loaderCalled = true; },
    };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + retiredSet + '\n' + panelFn + '\n' + stateFn + '\n' + isCeFn + '\n'
        + apiLift + '\n'
        + `(async () => {
              const targetPageId = 'overage-caps';
              async function _ceGate() { ${gateLift} }
              await _ceGate();
              RESULT.panel = pageContent.innerHTML;
              RESULT.title = document.title;
              if (RESULT.panel === null) { initializeOverageCapsPage(); }
           })()`;
    sandbox.RESULT = RESULT;
    sandbox.pageContent = pageContent;
    try {
        await vm.runInContext(src, sandbox, { filename: 'overage-caps-ce-retired.js' });
    } catch (e) {
        RESULT.error = String(e && (e.message || e));
        console.error(`# runner-error: ${RESULT.error}`);
    }
    return RESULT;
}

const CE = { entitlements: { edition: 'ce' } };
const PAID = { entitlements: { edition: 'saas' } };

(async () => {
    // --- T2: CE -> retired panel, loader never reached ------------------------
    const ce = await scenario(CE);
    check('T2a CE ships the retired panel', !!ce.panel);
    check('T2b CE panel carries the pinned witness default-cap-status',
        !!ce.panel && ce.panel.includes('<div id="default-cap-status"></div>'));
    check('T2c CE panel explains the page',
        !!ce.panel && ce.panel.includes('Not available in MOD Community Edition'));
    check('T2d CE panel ships NO paid cap controls',
        !!ce.panel && !ce.panel.includes('default-cap-input') && !ce.panel.includes('save-default-cap-btn'));
    check('T2e CE never reaches initializeOverageCapsPage()', !ce.loaderCalled);

    // --- T3: paid -> untouched -----------------------------------------------
    const paid = await scenario(PAID);
    check('T3a paid does NOT fire the retired gate', paid.panel === null);
    check('T3b paid reaches the overage-caps loader', paid.loaderCalled);

    // --- T4: unreadable payload -> fail-closed (CE) --------------------------
    const unknown = await scenario({ entitlements: { edition: 'ce', error: 'unresolved' } });
    check('T4a unknown entitlements still retire the page', !!unknown.panel);
    check('T4b unknown entitlements never run the paid loader', !unknown.loaderCalled);

    const total = 3 + 5 + 2 + 2;
    console.log(`\n  ${total - failures}/${total} passed`);
    if (failures) { process.exitCode = 1; }
})();
