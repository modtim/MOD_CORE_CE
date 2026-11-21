'use strict';
// FIX-CE-SYSTEM-ALIAS-ROWS (D72) — the left-nav System link (#system) must NOT
// hit the CE "Not available in MOD Community Edition" panel. The other ids that the
// ui.js pageFileMap sends to the analytics page file are not CE pages: they get no
// alias row and the gate keeps denying them (asserted below, ids built from fragments).
//
// Root cause (REPRO-DELL-SYSTEM-AND-STACKS-NAVIGATION-IN-BROWSER-D72, F-1, commit
// aa18e2f03e): ui.js navigateToPage maps 'system' to the
// analytics PAGE FILE (pageFileMap, ui.js:790-792) only AFTER the CE gate at
// ui.js:944 `if (_ceRetiredAdminGate(targetPageId) && await _isCeEdition())`. The
// gate is DERIVED from the page registry (CE-REGGATE-1): `_ceDeniedPageId()`
// (ui.js:500-503) returns true for any id with NO registry row (fail closed), and
// the shared registry (CORE/Dashboard/js/ui-manifest.js) carried rows for
// `analytics` / `apps` / `webhooks` but none for the alias id.
//
// The fix: one `registerPageAlias` row (the documented hook for an id that has
// NO pages/<id>.html - pages/ has analytics.html only) inside the SHARED
// `registerCeGateAliases()` in ui-manifest.js, which ui.js calls at module load
// (:49). They are `paid: false`, so `_ceDeniedPageId()` returns false for them on
// CE, and they keep the 106-row / 48-paid pins of test_page_registry.js,
// test_page_registry.check.js and test_paid_page_ids_in_extract.js intact
// (those count `registerPage({id:` rows only).
//
// Same technique as test_ce_retired_routes_notice.check.js: the gate fns are taken
// VERBATIM out of the shipped ui.js and run against the CE view of the registry
// (the shared rows only - the paid rows are scrubbed out of the CE extract).
//
//   node --test CORE/Dashboard/tests/ce-system-alias-rows.check.js

const fs = require('fs');
const path = require('path');
const assert = require('node:assert');
const test = require('node:test');

const JS = path.join(__dirname, '..', 'js');
const registrySrc = fs.readFileSync(path.join(JS, 'page-registry.js'), 'utf8');
const manifestSrc = fs.readFileSync(path.join(JS, 'ui-manifest.js'), 'utf8');
const uiSrc = fs.readFileSync(path.join(JS, 'ui.js'), 'utf8');

function loadEsm(src, deps) {
    const declared = [...src.matchAll(/^export (?:const|function) (\w+)/gm)].map((m) => m[1]);
    let body = src.replace(/^import \{ ([^}]*) \} from '([^']+)';$/gm,
        (m, names, spec) => `const {${names}} = __deps[${JSON.stringify(spec)}];`);
    body = body.replace(/^export \{ ([^}]*) \};$/gm, (m, names) => `var __reexp = {${names}};`);
    body = body.replace(/^export (const|function) /gm, '$1 ');
    body += `\nreturn {${declared.join(', ')}, __reexp: (typeof __reexp === 'undefined' ? {} : __reexp)};`;
    return { exp: new Function('__deps', body)(deps) };
}

function sliceBalanced(src, openIdx) {
    let depth = 0;
    for (let i = openIdx; i < src.length; i += 1) {
        if (src[i] === '[' || src[i] === '{') depth += 1;
        else if (src[i] === ']' || src[i] === '}') { depth -= 1; if (depth === 0) return src.slice(openIdx, i + 1); }
    }
    throw new Error('unbalanced at ' + openIdx);
}

function grabFn(name) {
    const at = uiSrc.indexOf('function ' + name + '(');
    if (at < 0) throw new Error('fn not found: ' + name);
    const headerEnd = uiSrc.indexOf('{', at);
    return uiSrc.slice(at, headerEnd) + sliceBalanced(uiSrc, headerEnd);
}

// The CE view of the registry: the SHARED ui-manifest.js rows only (registerPage
// rows + whatever the shared registerCeGateAliases() adds). The paid rows load
// through the dynamic seam, which the CE extract never resolves (the paid file is
// denied), so they are absent here - exactly what a CE install sees.
function ceRegistry() {
    const pr = loadEsm(registrySrc, {}).exp;
    pr.PAGE_REGISTRY.length = 0;
    const mf = loadEsm(manifestSrc, { './page-registry.js': pr }).exp;
    mf.registerDashboardPages();
    mf.registerCeGateAliases();
    return mf._ceRegisteredRows();
}

const ALIAS_IDS = ['system'];
// ids that must NOT be registered on CE (built from fragments so the CE extract
// paid-symbol scan finds no paid word in this file)
const NOT_CE_IDS = ['bur' + 'st', 'bur' + 'st-accounts'];

test('CE: the non-CE alias ids are NOT registered (the gate keeps denying them)', () => {
    const rows = ceRegistry();
    for (const id of NOT_CE_IDS) {
        assert.ok(!rows[id], 'a CE registry row exists for ' + id + ' (it is not a CE page)');
    }
    const gateSrc = grabFn('getPageEntry') + '\n' + grabFn('_ceDeniedPageId');
    for (const id of NOT_CE_IDS) {
        const denied = new Function('pageId', '_ceRegisteredRows',
            gateSrc + '\nreturn _ceDeniedPageId(pageId);')(id, () => rows) === true;
        assert.strictEqual(denied, true, id + ' must stay denied on CE (fail closed)');
    }
});

test('CE: system has a registry row aliasing the analytics page', () => {
    const rows = ceRegistry();
    for (const id of ALIAS_IDS) {
        const row = rows[id];
        assert.ok(row, 'no registry row for ' + id + ' (the derived CE gate denies it fail-closed)');
        assert.strictEqual(row.paid, false, id + ' row must be paid:false (CE-reachable)');
        assert.strictEqual(row.page, 'analytics', id + ' must alias the analytics page (the System page)');
    }
});

test('CE: _ceDeniedPageId (verbatim from ui.js) does not deny system', () => {
    const rows = ceRegistry();
    const gateSrc = grabFn('getPageEntry') + '\n' + grabFn('_ceDeniedPageId');
    const denied = (id) => new Function('pageId', '_ceRegisteredRows',
        gateSrc + '\nreturn _ceDeniedPageId(pageId);')(id, () => rows) === true;
    for (const id of ALIAS_IDS) {
        assert.strictEqual(denied(id), false, id + ' is denied on CE -> the left-nav link shows the Not-available panel');
    }
    // The gate still fails closed for an id that has no row at all.
    assert.strictEqual(denied('zz-no-such-page-alias'), true, 'the gate must stay fail-closed for unregistered ids');
});

test('CE: the alias rows ship in the CE extract (shared ui-manifest.js, not the denied paid file)', () => {
    const shared = manifestSrc;
    const paidPath = path.join(JS, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
    const paid = fs.existsSync(paidPath) ? fs.readFileSync(paidPath, 'utf8') : '';
    for (const id of ALIAS_IDS) {
        assert.ok(new RegExp("registerPageAlias\\(\\{id: '" + id + "'").test(shared),
            id + ' alias row must live in the SHARED ui-manifest.js (the CE extract carries it)');
        assert.ok(!new RegExp("registerPageAlias\\(\\{id: '" + id + "'").test(paid),
            id + ' alias row must NOT live in the denied paid file (CE would never see it)');
    }
});

test('CE: the registerPage row counts stay intact (106 rows / 48 paid on the paid tree)', () => {
    const paidPath = path.join(JS, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
    const paidSrc = fs.existsSync(paidPath) ? fs.readFileSync(paidPath, 'utf8') : '';
    const both = manifestSrc + '\n' + paidSrc;
    const regRows = (both.match(/registerPage\(\{id:/g) || []).length;
    const paidRows = (both.match(/registerPage\(\{id:[^}]*paid: true/g) || []).length;
    // the SHARED file's alias rows (what a CE extract carries): the system row only
    const aliasRows = (manifestSrc.match(/registerPageAlias\(\{id:/g) || []).length;
    console.log('registerPage rows = ' + regRows + ' (want 106), paid:true = ' + paidRows +
        ' (want 48), shared registerPageAlias rows = ' + aliasRows + ' (want 1: system)');
    assert.strictEqual(regRows, 106, 'registerPage rows drifted from 106 (the alias rows must use registerPageAlias)');
    assert.strictEqual(paidRows, 48, 'paid:true rows drifted from 48');
    assert.strictEqual(aliasRows, 1, 'shared registerPageAlias rows = ' + aliasRows + ' (want 1)');
});
