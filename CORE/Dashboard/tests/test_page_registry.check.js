'use strict';
// CE-NOPAID-4.2 leg A — proof for CORE/Dashboard/js/page-registry.js.
//
// PAGE_REGISTRY / registerPage moved out of ui-manifest.js; ui-manifest.js imports them
// from page-registry.js and re-exports the SAME names. The moved code is unchanged, so
// registerPage still PUSHES (the CE no-op is registerPageCeStub, which never pushes).
//
// The ES modules are evaluated here without a loader: `export` keywords are stripped and
// the one `import` line of ui-manifest.js is served from a deps map, so both modules
// share one PAGE_REGISTRY array object (that identity is what the re-export test checks).

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const test = require('node:test');

const JS = path.join(__dirname, '..', 'js');
const registrySrc = fs.readFileSync(path.join(JS, 'page-registry.js'), 'utf8');
const manifestSrc = fs.readFileSync(path.join(JS, 'ui-manifest.js'), 'utf8');

function loadEsm(src, deps) {
    const declared = [...src.matchAll(/^export (?:const|function) (\w+)/gm)].map((m) => m[1]);
    let body = src.replace(/^import \{ ([^}]*) \} from '([^']+)';$/gm,
        (m, names, spec) => `const {${names}} = __deps[${JSON.stringify(spec)}];`);
    body = body.replace(/^export \{ ([^}]*) \};$/gm, (m, names) => `var __reexp = {${names}};`);
    body = body.replace(/^export (const|function) /gm, '$1 ');
    body += `\nreturn {${declared.join(', ')}, __reexp: (typeof __reexp === 'undefined' ? {} : __reexp)};`;
    const out = new Function('__deps', body)(deps);
    return { exp: out, reexp: out.__reexp };
}

function freshRegistry() {
    const pr = loadEsm(registrySrc, {}).exp;
    pr.PAGE_REGISTRY.length = 0;
    return pr;
}

test('page-registry: registerPage pushes the row and returns the same entry', () => {
    const pr = freshRegistry();
    const row = { id: 'demo-free', page: 'demo', module: null, nav: null, navGroup: null, navRole: 'any', paid: false };
    assert.strictEqual(pr.registerPage(row), row);
    assert.strictEqual(pr.PAGE_REGISTRY.length, 1);
    assert.strictEqual(pr.PAGE_REGISTRY[0], row);
});

test('page-registry: getPageEntry of an unknown id is undefined', () => {
    const pr = freshRegistry();
    pr.registerPage({ id: 'known', paid: false });
    assert.strictEqual(pr.getPageEntry('no-such-page'), undefined);
    assert.strictEqual(pr.getPageEntry('known').id, 'known');
});

test('page-registry: isPaidPage is true only for a paid:true row', () => {
    const pr = freshRegistry();
    pr.registerPage({ id: 'paid-one', paid: true });
    pr.registerPage({ id: 'free-one', paid: false });
    assert.strictEqual(pr.isPaidPage('paid-one'), true);
    assert.strictEqual(pr.isPaidPage('free-one'), false);
    assert.strictEqual(pr.isPaidPage('no-such-page'), false);
});

test('ui-manifest: source imports the moved names and no longer declares PAGE_REGISTRY', () => {
    assert.ok(manifestSrc.includes("import { PAGE_REGISTRY, registerPage } from './page-registry.js';"));
    assert.ok(manifestSrc.includes('export { PAGE_REGISTRY, registerPage };'));
    assert.ok(!/^const PAGE_REGISTRY = \[\];/m.test(manifestSrc), 'PAGE_REGISTRY still declared in ui-manifest.js');
    assert.ok(!/^export function registerPage\(/m.test(manifestSrc), 'registerPage still defined in ui-manifest.js');
});

test('ui-manifest: re-exports the SAME PAGE_REGISTRY array object and registerPage function', () => {
    const pr = freshRegistry();
    const { exp, reexp } = loadEsm(manifestSrc, { './page-registry.js': pr });
    assert.strictEqual(reexp.PAGE_REGISTRY, pr.PAGE_REGISTRY, 're-export is not the same array object');
    assert.strictEqual(reexp.registerPage, pr.registerPage, 're-export is not the same function');
    assert.strictEqual(typeof exp.registerDashboardPages, 'function');
});

test('ui-manifest: registerDashboardPages still fills the shared registry (106 rows)', () => {
    const pr = freshRegistry();
    const { exp } = loadEsm(manifestSrc, { './page-registry.js': pr });
    exp.registerDashboardPages();
    // CE-NOPAID-4.1d: the paid rows load via the dynamic seam, which a
    // lifted new Function cannot resolve; load the paid file's rows explicitly (same shim).
    const paidSrc = fs.readFileSync(path.join(JS, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js'), 'utf8');
    const paidExp = loadEsm(paidSrc, {}).exp;
    paidExp.registerPaidRows(pr.registerPage);
    assert.strictEqual(pr.PAGE_REGISTRY.length, 106);
    assert.strictEqual(pr.isPaidPage('access-reviews'), true);
    assert.strictEqual(pr.isPaidPage('account'), false);
});

test('ui-manifest: registerPageCeStub is still the no-op (never pushes)', () => {
    const pr = freshRegistry();
    const { exp } = loadEsm(manifestSrc, { './page-registry.js': pr });
    const row = { id: 'ce-stub-probe', paid: true };
    assert.strictEqual(exp.registerPageCeStub(row), row);
    assert.strictEqual(pr.PAGE_REGISTRY.length, 0);
});
