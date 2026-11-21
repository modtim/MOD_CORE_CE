'use strict';
// FIX-DASH-OPERATOR-PROPOSALS-DEEPLINK — the #operator-proposals deep link on CE.
//
//   #operator-proposals (ui.js:1242, the WSIP 7.6 notify-email deep link) routes to
//   the Compliance hub's "Operator proposals" tab via _loadComplianceHubTab('compliance-operator-proposals').
//   'operator-proposals' was NOT in _CE_PAID_HUB_PAGES, so on CE the hash skipped the
//   "Not available" panel and initialised the PAID Compliance hub shell instead (a bypass
//   of the #compliance-center paid-hub gate); the hub then fell back to visible[0].
//   Fix (F1, CHECK-CE-DEAD-NAV-ENTRIES-D): 'operator-proposals' joins _CE_PAID_HUB_PAGES,
//   so the existing paid-hub gate ships the panel and returns BEFORE the branch.
//
// Style of tests/ce-dead-nav-3.check.js: the REAL set + panel fn + edition predicate +
// paid-hub gate + the hub-tab loader + the operator-proposals branch call are LIFTED out
// of js/ui.js and EXECUTED in a fresh node:vm sandbox per scenario with `fetch` stubbed
// and utils.js optionalPageModule() spied — no browser, no npm deps, no re-implementation.
//
//   node CORE/Dashboard/tests/ce-operator-proposals-deeplink.check.js
//
// Cases:
//   T1 static: the set carries operator-proposals (with its comment block), keeps the
//      shipped members, the gate + branch are liftable, no double-carve in _CE_COV4B_PAGES
//   T2 CE -> #operator-proposals ships the panel, the hub loader is never entered
//   T3 paid -> gate does not fire, the hub IS initialised with 'compliance-operator-proposals'
//   T4 unreadable entitlements -> fail-closed CE (panel, no hub)
//   T5 siblings unchanged: CE #compliance-center still panelled, CE #marketplace still not
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
// The paid-hub gate: `if` at 4-space indent, closed by a 4-space `}` (the inner lines
// are 8-space indented, so the first `\n    }` after the marker is the gate's own close).
function liftHubGate(src) {
    const i = src.indexOf('if (_CE_PAID_HUB_PAGES.has(targetPageId)');
    if (i < 0) return null;
    const j = src.indexOf('\n    }', i);
    if (j < 0) return null;
    return src.slice(i, j + '\n    }'.length);
}
const BRANCH_CALL = "await _loadComplianceHubTab('compliance-operator-proposals');";
function liftBranchCall(src) {
    const i = src.indexOf(BRANCH_CALL);
    if (i < 0) return null;
    return src.slice(i, i + BRANCH_CALL.length);
}

const paidHubSet = liftSet(ui, '_CE_PAID_HUB_PAGES');
const complianceSet = liftSet(ui, '_CE_PAID_COMPLIANCE_PAGES');
const cov4bSet = liftSet(ui, '_CE_COV4B_PAGES');
const paidPanelFn = lift(ui, 'function _cePaidPagePanelHtml()');
const isCeFn = lift(ui, 'function _isCeEdition()');
const stateFn = lift(ui, 'function _ceEditionState()');
const hubTabFn = lift(ui, 'async function _loadComplianceHubTab(child)');
const apiLift = lift(api, 'const _featuresCache = {');
const hubGate = liftHubGate(ui);
const branchCall = liftBranchCall(ui);
if (!paidHubSet || !cov4bSet || !paidPanelFn || !isCeFn || !stateFn || !hubTabFn
    || !apiLift || !hubGate || !branchCall) {
    console.error('FAIL could not lift the paid-hub set/panel fn/predicate/loader/'
        + 'getPlatformFeatures/the gate/the branch call — the mechanism is not in the liftable shape');
    process.exit(1);
}

async function hubScenario(payload, pageId) {
    const RESULT = { panel: null, title: null, spyCalls: [], hubInits: [], error: null };
    const pageContent = { innerHTML: null };
    const sandbox = {
        console: console,
        document: {
            title: '',
            getElementById: (id) => (id === 'page-content' ? pageContent : { style: {} }),
        },
        window: { location: { href: `http://dash.localhost/#${pageId}` } },
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }),
        // utils.js optionalPageModule() spy: records the resolution and hands back a
        // tracker standing in for hub-compliance.js's initializeComplianceHubPage export.
        optionalPageModule: (modPath, exportName) => {
            RESULT.spyCalls.push(`${modPath}/${exportName}`);
            return () => { RESULT.hubInits.push(exportName); };
        },
    };
    vm.createContext(sandbox);
    const src = 'const API_BASE_URL = "http://api.localhost";\n'
        + paidHubSet + '\n' + paidPanelFn + '\n' + stateFn + '\n' + isCeFn + '\n'
        + apiLift + '\n' + hubTabFn + '\n'
        + `(async () => {
              const targetPageId = '${pageId}';
              async function _hubGate() { ${hubGate} }
              await _hubGate();
              RESULT.panel = pageContent.innerHTML;
              RESULT.title = document.title;
              if (RESULT.panel === null) { ${branchCall} }
           })()`;
    sandbox.RESULT = RESULT;
    sandbox.pageContent = pageContent;
    try {
        await vm.runInContext(src, sandbox, { filename: `ce-operator-proposals-deeplink-${pageId}.js` });
    } catch (e) {
        RESULT.error = String(e && (e.message || e));
        console.error(`# runner-error(${pageId}): ${RESULT.error}`);
    }
    return RESULT;
}

const CE = { entitlements: { edition: 'ce' } };
const PAID = { entitlements: { edition: 'saas' } };
const UNKNOWN = { entitlements: { edition: 'ce', error: 'unresolved' } };
const SHIPPED = ['marketplace', 'security', 'compliance-center', 'billing',
    'admin-plans', 'capture-devices', 'subscription'];

(async () => {
    // --- T1 static ------------------------------------------------------------
    check('T1a _CE_PAID_HUB_PAGES carries operator-proposals', /'operator-proposals'/.test(paidHubSet));
    check('T1b the carve carries its CE-OPSPROP-DEEPLINK comment block',
        /CE-OPSPROP-DEEPLINK/.test(paidHubSet));
    check('T1c the set still carries every shipped member',
        SHIPPED.every((p) => new RegExp(`'${p}'`).test(paidHubSet)));
    check('T1d no double-carve: operator-proposals is NOT in _CE_COV4B_PAGES',
        !/'operator-proposals'/.test(cov4bSet));
    check('T1e the paid-hub gate is present in the liftable shape', !!hubGate);
    check('T1f the #operator-proposals branch still routes to the Compliance hub child',
        /targetPageId === 'operator-proposals'/.test(ui) && !!branchCall);
    check('T1g operator-proposals is also in _CE_PAID_COMPLIANCE_PAGES (the gate that panels it first)',
        complianceSet !== null && /'operator-proposals'/.test(complianceSet));

    // --- T2 CE -> the panel, never the hub ------------------------------------
    const ce = await hubScenario(CE, 'operator-proposals');
    check('T2a CE ships the operator-proposals panel', !!ce.panel);
    check('T2b the panel says Not available in MOD Community Edition',
        !!ce.panel && ce.panel.includes('Not available in MOD Community Edition'));
    check('T2c CE ships NO Compliance hub shell (no tab markup)',
        !!ce.panel && !ce.panel.includes('main-tab-') && !ce.panel.includes('compliance-operator-proposals'));
    check('T2d CE never enters _loadComplianceHubTab (no hub-compliance resolution)',
        ce.spyCalls.length === 0);
    check('T2e CE never calls initializeComplianceHubPage', ce.hubInits.length === 0);
    check('T2f the CE tab title is the Not-available title',
        !!ce.title && ce.title.indexOf('Not available') === 0);

    // --- T3 paid: unchanged ---------------------------------------------------
    const paid = await hubScenario(PAID, 'operator-proposals');
    check('T3a paid does NOT fire the CE gate', paid.panel === null);
    check('T3b paid resolves ./hub-compliance.js/initializeComplianceHubPage',
        paid.spyCalls.includes('./hub-compliance.js/initializeComplianceHubPage'));
    check('T3c paid initialises the hub with the operator-proposals child',
        paid.hubInits.includes('initializeComplianceHubPage'));
    check('T3d paid leaves the page title untouched', paid.title === '');

    // --- T4 unreadable payload -> fail-closed ---------------------------------
    const unknown = await hubScenario(UNKNOWN, 'operator-proposals');
    check('T4a unknown entitlements still carve operator-proposals', !!unknown.panel);
    check('T4b unknown entitlements never run the paid hub', unknown.hubInits.length === 0);
    check('T4c the unknown carve carries the CE wording',
        !!unknown.panel && unknown.panel.includes('Not available in MOD Community Edition'));

    // --- T5 siblings unchanged ------------------------------------------------
    const ccCe = await hubScenario(CE, 'compliance-center');
    check('T5a CE #compliance-center still panelled (sibling unchanged)', !!ccCe.panel);
    check('T5b CE #compliance-center still never initialises the hub', ccCe.hubInits.length === 0);
    const mpCe = await hubScenario(CE, 'marketplace');
    check('T5c CE #marketplace keeps its documented exception (no panel)', mpCe.panel === null);

    const total = 7 + 6 + 4 + 3 + 3;
    console.log(`\n  ce-operator-proposals-deeplink: ${total - failures}/${total} passed`);
    if (failures) { process.exitCode = 1; }
})();
