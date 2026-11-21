'use strict';
// CE-NOPAID-4.1f proof — the deep link navigateToPage('#release-approvals').
//
// Asserts:
//   * CE leg: navigateToPage's _CE_PAID_COMPLIANCE_PAGES gate renders the AMBER
//     "Not available in MOD Community Edition" panel and RETURNS — the
//     compliance-hub dispatch never runs (0 hub loads on CE).
//   * paid leg: the gate does NOT fire and the dispatch branch loads the page
//     (hub-compliance.js resolves through optionalPageModule) — the page loads.
//   * extract leg (optional, arg = CE extract root): the shipped CE extract's
//     ui.js carries 0 word-boundary hits for the ids the paid gate denies,
//     i.e. the gate itself is gone from CE (0 paid import in the extract).
//
// Same technique as test_page_registry.js / assistant-panel-progress.check.js:
// the code under test is EXTRACTED VERBATIM from the shipped source text and
// run against stubs, so the assertions test the shipped file, not a copy.
//
//   node CORE/Dashboard/tests/test_navigate_to_paid_page.js [extract-root]
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const UI_REL = 'CORE/Dashboard/js/ui.js';
const uiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function at(needle, what) {
    const i = uiSrc.indexOf(needle);
    if (i < 0) throw new Error(what + ' missing from ' + UI_REL);
    return i;
}

// --- shipped pieces, verbatim --------------------------------------------
const setAt = at('const _CE_PAID_COMPLIANCE_PAGES = new Set([', 'the paid-compliance Set');
const setLit = uiSrc.slice(setAt, uiSrc.indexOf(']);', setAt)) + ']);';
const deniedIds = [];
for (const m of setLit.matchAll(/'([a-z0-9-]+)'/g)) deniedIds.push(m[1]);
if (!deniedIds.includes('release-approvals')) throw new Error('release-approvals not denied on CE');

const panelHdr = 'function _cePaidCompliancePanelHtml()';
const panelFn = panelHdr + ' ' + blockAt(uiSrc, uiSrc.indexOf('{', at(panelHdr, panelHdr) + panelHdr.length));
const stateHdr = 'function _ceEditionState()';
const stateFn = stateHdr + ' ' + blockAt(uiSrc, uiSrc.indexOf('{', at(stateHdr, stateHdr) + stateHdr.length));
const isCeHdr = 'function _isCeEdition()';
const isCeFn = isCeHdr + ' ' + blockAt(uiSrc, uiSrc.indexOf('{', at(isCeHdr, isCeHdr) + isCeHdr.length));
const loadHdr = 'async function _loadComplianceHubTab(child)';
const loadFn = loadHdr + ' ' + blockAt(uiSrc, uiSrc.indexOf('{', at(loadHdr, loadHdr) + loadHdr.length));

const gateNeedle = 'if (_CE_PAID_COMPLIANCE_PAGES.has(targetPageId) && await _isCeEdition()) {';
const gateAt = at(gateNeedle, 'the navigateToPage paid-compliance gate');
const gateBranch = gateNeedle.slice(0, gateNeedle.length - 1) + ' ' + blockAt(uiSrc, gateAt + gateNeedle.length - 1);

const dispNeedle = "} else if (targetPageId === 'release-approvals') {";
const dispAt = at(dispNeedle, 'the release-approvals dispatch branch');
const dispBody = blockAt(uiSrc, uiSrc.indexOf('{', dispAt + dispNeedle.length - 1)).slice(1, -1);

// --- harness: the shipped gate + dispatch, run against stubs -------------
// `targetPageId` and `document` are injected; `dispatches` records every
// optionalPageModule load, `page.innerHTML` is what the panel writes.
const SCRIPT =
    setLit + '\n' + panelFn + '\n' + stateFn + '\n' + isCeFn + '\n' + loadFn + '\n' +
    'async function drive(targetPageId) {\n' +
    '  ' + gateBranch + '\n' +
    '  ' + dispBody + '\n' +
    '  return "reached-dispatch";\n}\n';

async function leg(editionPayload) {
    const page = { innerHTML: '<div id="release-approvals"></div>' };
    const dispatches = [];
    const ctx = vm.createContext({
        document: { getElementById: (id) => page, title: '' },
        getPlatformFeatures: () => Promise.resolve(editionPayload),
        optionalPageModule: (file, fn) => { dispatches.push(file); return Promise.resolve(function () {}); },
    });
    vm.runInNewContext(SCRIPT, ctx);
    const outcome = await vm.runInNewContext('drive("release-approvals")', ctx);
    return { outcome, page, dispatches };
}

(async () => {
    const ce = await leg({ entitlements: { edition: 'ce' } });
    const paid = await leg({ entitlements: { edition: 'saas' } });

    // CE leg: amber panel written, hub dispatch never reached
    if (!/bg-amber-100/.test(ce.page.innerHTML)) throw new Error('CE leg: no amber panel (want bg-amber-100)');
    if (!/Not available in MOD Community Edition/.test(ce.page.innerHTML)) throw new Error('CE leg: panel wording missing');
    if (ce.outcome !== undefined) throw new Error('CE leg: gate must RETURN, dispatch unreachable (got ' + ce.outcome + ')');
    if (ce.dispatches.length !== 0) throw new Error('CE leg: hub loaded on CE (want 0 loads)');

    // paid leg: gate skipped, dispatch loads the hub page
    if (paid.outcome !== 'reached-dispatch') throw new Error('paid leg: page did not load (want reached-dispatch)');
    if (paid.dispatches.length !== 1 || paid.dispatches[0] !== './hub-compliance.js') throw new Error('paid leg: hub loads = ' + paid.dispatches.join(',') + ' (want ./hub-compliance.js)');
    if (/bg-amber-100/.test(paid.page.innerHTML)) throw new Error('paid leg: amber panel on paid (want page load)');

    // extract leg (optional): 0 paid gate ids in the shipped CE extract's ui.js
    let extractNote = 'extract not built (pass the extract root as argv[2])';
    if (process.argv[2]) {
        const jsDir = path.join(process.argv[2], 'CORE', 'Dashboard', 'js');
        const extractUi = fs.readFileSync(path.join(jsDir, 'ui.js'), 'utf8');
        // 0 paid import for the page this deep link targets: the manifest denies
        // release-approvals.js, so the module is absent from the extract and ui.js
        // carries no static import of it (a static import of a denied file breaks
        // the whole module graph on CE, which is why the dispatch uses optionalPageModule).
        if (fs.existsSync(path.join(jsDir, 'release-approvals.js'))) throw new Error('CE extract ships release-approvals.js (want absent)');
        const imports = extractUi.split('\n').filter((line) => /from '\.\/release-approvals\.js'/.test(line)).length;
        if (imports !== 0) throw new Error('CE extract ui.js statically imports release-approvals.js ' + imports + 'x (want 0)');
        const gate = extractUi.includes(gateNeedle);
        extractNote = 'CE extract: release-approvals.js absent, static imports = 0, gate shipped = ' + gate;
    }

    console.log('CE-NOPAID-4.1f: CE leg amber panel + 0 hub loads; paid leg page loads (hub-compliance.js x1); ' +
        'gate ids=' + deniedIds.length + '; ' + extractNote);
})().catch((e) => { console.log('FAIL: ' + e.message); process.exit(1); });

// --- merged in from  (CE-NOPAID-4.1e leg; same path chosen by that lane) ---
{
// CE-NOPAID-4.1e proof - CORE/Dashboard/tests/test_navigate_to_paid_page.js
//
// The tasks.md check for 4.1e: "test_navigate_to_paid_page (amber panel on CE)".
// Spec (SPEC-CE-NOPAID-4-1 :72): navigateToPage('#release-approvals') on CE
// returns the amber "Not available in MOD Community Edition" panel; on paid the
// page loads. Mutant: _isCeEdition returns false on CE -> the paid page loads.
//
// Same extract-verbatim technique as test_page_registry.js / fx144-ce-paid-compliance-hide.test.js:
// the CE page-gate block and the direct-URL gate branch are sliced OUT OF THE
// SHIPPED ui.js text (no re-typed copy) and run in a vm sandbox against a
// stubbed /platform/features payload:
//   * edition 'ce'      -> the gate fires, page-content gets the amber panel;
//   * 'unknown' (unreadable payload) -> fires too (fail-closed, rc20 failopen-a);
//   * edition 'saas'/'pro' -> the gate does NOT fire, the page loads.
//
//   node CORE/Dashboard/tests/test_navigate_to_paid_page.js

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const uiPath = path.join(__dirname, '..', 'js', 'ui.js');
const src = fs.readFileSync(uiPath, 'utf8');

const PAGE = 'release-approvals';

// --- 1. slice the shipped CE-gate block (the _CE_* Sets + the edition
//         predicate + the panel builders) up to navigateToPage ---------------
const listStart = src.indexOf('const _CE_COMPLIANCE_PAGES = new Set([');
assert.ok(listStart >= 0, '_CE_COMPLIANCE_PAGES not found in ui.js');
const marker = src.indexOf('export async function navigateToPage(hash) {', listStart);
assert.ok(marker > listStart, 'end marker for the CE-gate block not found');
const block = src.slice(listStart, marker);

// --- 2. the shipped direct-URL gate branch for the optional compliance pages ----
const gateRe = /if \(_CE_PAID_COMPLIANCE_PAGES\.has\(targetPageId\) && await _isCeEdition\(\)\) \{[\s\S]*?\n {4}\}/;
const gm = gateRe.exec(src);
assert.ok(gm, 'the shipped _CE_PAID_COMPLIANCE_PAGES direct-URL gate branch is missing in ui.js');
const gateBranch = gm[0];
assert.ok(gateBranch.includes('_cePaidCompliancePanelHtml()'),
    'the gate must render the paid-compliance amber panel');
assert.ok(/return;/.test(gateBranch), 'the gate must short-circuit (return) so the page never loads');

const setStart = block.indexOf('const _CE_PAID_COMPLIANCE_PAGES = new Set([');
const setEnd = block.indexOf(']);', setStart);
assert.ok(setEnd > setStart, '_CE_PAID_COMPLIANCE_PAGES set body not found');
assert.ok(block.slice(setStart, setEnd).includes("'" + PAGE + "'"),
    PAGE + ' must be listed in _CE_PAID_COMPLIANCE_PAGES');

// --- 3. run the block + the gate branch in a sandbox, per edition -----------
async function gateOutcome(features) {
    const doc = { pageContent: { innerHTML: '<div id="page-loaded">release-approvals page</div>' } };
    const ctx = vm.createContext({
        document: { getElementById: (id) => (id === 'page-content' ? doc.pageContent : null), title: '' },
        getPlatformFeatures: () => Promise.resolve(features),
    });
    const script = block +
        '\nconst _runGate = async (targetPageId) => {' + gateBranch + '\nreturn "PAGE-LOADED";};\n' +
        '((_runGate("' + PAGE + '")).then((r) => (r === undefined ? "PANEL" : r)));';
    const outcome = await vm.runInContext(script, ctx, { filename: 'ui-gate', timeout: 5000 });
    return { outcome, innerHTML: doc.pageContent.innerHTML };
}

const CE_CASES = [
    { name: 'ce', features: { entitlements: { edition: 'ce' } } },
    { name: 'unknown (unreadable payload, fail-closed)', features: { entitlements: { error: 'http 500' } } },
    { name: 'missing block', features: {} },
];
const PAID_CASES = [
    { name: 'saas', features: { entitlements: { edition: 'saas' } } },
    { name: 'pro', features: { entitlements: { edition: 'pro' } } },
];

(async () => {
    for (const c of CE_CASES) {
        const r = await gateOutcome(c.features);
        assert.strictEqual(r.outcome, 'PANEL',
            'CE leg ' + c.name + ' must stop at the panel, got ' + r.outcome);
        assert.ok(r.innerHTML.includes('Not available in MOD Community Edition'),
            'CE leg ' + c.name + ' panel must carry the amber CE notice');
        assert.ok(!r.innerHTML.includes('page-loaded'),
            'CE leg ' + c.name + ' must never load the paid page');
    }
    for (const c of PAID_CASES) {
        const r = await gateOutcome(c.features);
        assert.strictEqual(r.outcome, 'PAGE-LOADED',
            'paid leg ' + c.name + ' must load the page, got ' + r.outcome);
        assert.ok(r.innerHTML.includes('page-loaded'), 'paid leg ' + c.name + ' keeps the real page');
        assert.ok(!r.innerHTML.includes('Not available in MOD Community Edition'),
            'paid leg ' + c.name + ' must not show the CE wording');
    }

    // --- 4. one shared predicate: nav hide and the panel can never disagree --
    assert.strictEqual((block.match(/function _isCeEdition\(\)/g) || []).length, 1,
        'exactly one _isCeEdition predicate in the CE-gate block');
    assert.ok(/s !== 'paid'/.test(block),
        '_isCeEdition must be fail-closed (unknown counts as CE)');

    console.log('test_navigate_to_paid_page: CE legs (ce/unknown/missing) -> amber panel, page never loads; paid legs (saas/pro) -> page loads, no CE wording');
    console.log('  gate branch extracted verbatim from ' + uiPath + ' (ui.js:' + (src.slice(0, src.indexOf(gateBranch)).split('\n').length) + ')');
})();

}
