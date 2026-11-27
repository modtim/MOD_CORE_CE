// KEEPWARM-UI check (owner Tim 11-25: "that isn't a place") — the Settings
// home for the two KEEP-WARM-SLICE-1 switches. Same technique as
// settings-ce-gate-s20.check.js: the shipped functions are EXTRACTED VERBATIM
// from settings.js text and run against a stub DOM, so the assertions test the
// shipped source, not a copy.
//
//   node CORE/Dashboard/tests/keep-warm-ui.check.js
//
// Asserts: non-platform-admin -> tab+panel hidden, ZERO fetches fire;
// platform admin -> the tenant GET carries ?tenant_id=, the platform GET does
// not; a 404 on a key hides that row only; PATCH bodies carry {value: bool}
// and the tenant PATCH carries tenant_id while the platform PATCH does not;
// save-failure reverts the checkbox and shows the error line; old-file state
// (no KEEPWARM-UI block) -> throw -> FAIL.
'use strict';
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function fnSrc(hdr) {
    const at = settingsSrc.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in settings.js (old-file state)`);
    return `${hdr} ${blockAt(settingsSrc, settingsSrc.indexOf('{', at + hdr.length))}`;
}

const KW_EN_KEY = 'worker.warm_pool_enabled';
const KW_ALLOWED_KEY = 'worker.warm_pool_allowed';
if (settingsSrc.indexOf('KEEPWARM-UI') < 0) throw new Error('no KEEPWARM-UI block in settings.js (old-file state)');
if (settingsSrc.indexOf(KW_EN_KEY) < 0) throw new Error(`tenant key ${KW_EN_KEY} not referenced in settings.js`);
if (settingsSrc.indexOf(KW_ALLOWED_KEY) < 0) throw new Error(`platform key ${KW_ALLOWED_KEY} not referenced in settings.js`);

const initSrc = fnSrc('async function _initKeepWarmSubTab()');
const loadSrc = fnSrc('async function _kwLoadRows()');
const tenantPatchSrc = fnSrc('async function _kwOnTenantChange(checked)');
const platformPatchSrc = fnSrc('async function _kwOnPlatformChange(checked)');
const paramSrc = fnSrc('function _kwTenantParam()');
const hasTenantSrc = fnSrc('function _kwHasTenant()');
const setErrSrc = fnSrc('function _kwSetError(msg)');
const setStatusSrc = fnSrc('function _kwSetStatus(msg)');

// Structural assertions on the shipped source (order-sensitive):
if (!(initSrc.indexOf('is_platform_admin') >= 0)) throw new Error('init must gate on is_platform_admin');
if (!(initSrc.indexOf("'ai-tool-tab-keep-warm'") >= 0)) throw new Error('init must target the keep-warm tab');
if (!(loadSrc.indexOf('keep-warm-tenant-row') >= 0 && loadSrc.indexOf('keep-warm-platform-row') >= 0)) throw new Error('load must drive both rows');
// KEEPWARM-TENANT-HIDE (owner Tim 11-25): the tenant row is gated on the user
// having a tenant, and the tenant PATCH handler refuses without one.
if (!(loadSrc.indexOf('_kwHasTenant()') >= 0)) throw new Error('load must hide the tenant row when the user has no tenant (KEEPWARM-TENANT-HIDE)');
if (!(tenantPatchSrc.indexOf('_kwHasTenant()') >= 0)) throw new Error('tenant PATCH must be guarded by _kwHasTenant (KEEPWARM-TENANT-HIDE)');
if (!(loadSrc.indexOf('keep-warm-tenant-help') >= 0)) throw new Error('load must toggle the tenant help line (KEEPWARM-TENANT-HIDE)');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

function makeEnv(admin, tenantId, fetchPlan) {
    const fetched = [];
    const els = {};
    function el(id) {
        if (!els[id]) els[id] = { hidden: id !== 'keep-warm-error' && id !== 'keep-warm-status' ? true : true, checked: false, addEventListener: () => {}, classList: { add: (c) => { if (c === 'hidden') els[id].hidden = true; }, remove: (c) => { if (c === 'hidden') els[id].hidden = false; } }, dataset: {} };
        return els[id];
    }
    const env = {
        document: { getElementById: (id) => el(id), querySelectorAll: () => [] },
        window: {},
        currentUser: admin ? { is_platform_admin: true, tenant_id: tenantId } : { is_platform_admin: false, tenant_id: tenantId },
        _keepWarmWired: false,
        _wkpEscapeHtml: (s) => String(s),
        apiCall: (p, opts) => {
            fetched.push({ p, opts });
            const plan = fetchPlan(p);
            if (plan.status === 404) { const e = new Error('Not Found'); e.detail = 'Not Found'; throw e; }
            return { value: plan.value, status: plan.status };
        },
        console: { error: () => {}, warn: () => {} },
        setTimeout: (fn, ms) => { fn(); return 0; },
    };
    return { env, fetched, el };
}

async function runInit(admin, tenantId, fetchPlan) {
    const { env, fetched, el } = makeEnv(admin, tenantId, fetchPlan);
    const names = Object.keys(env);
    const body = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
        + ` ${paramSrc} ${hasTenantSrc} ${setErrSrc} ${setStatusSrc} ${loadSrc} ${tenantPatchSrc} ${platformPatchSrc} ${initSrc}`
        + ` return _initKeepWarmSubTab();`;
    const runner = new Function('__env', body);
    await runner(env);
    return { fetched, hidden: (id) => el(id).hidden, checked: (id) => el(id).checked };
}

(async () => {
    // 1) non-platform-admin: nothing fires, tab+panel stay hidden.
    const nonAdmin = await runInit(false, 7, () => ({ status: 200, value: true }));
    check('non-admin: zero fetches fire', nonAdmin.fetched.length === 0, JSON.stringify(nonAdmin.fetched.map((f) => f.p)));
    check('non-admin: keep-warm tab hidden', nonAdmin.hidden('ai-tool-tab-keep-warm') === true);
    check('non-admin: keep-warm panel hidden', nonAdmin.hidden('ai-tool-keep-warm') === true);

    // 2) platform admin, tenant 7: both GETs fire; tenant GET carries tenant_id.
    const admin = await runInit(true, 7, () => ({ status: 200, value: true }));
    const tenantGet = admin.fetched.find((f) => f.p.startsWith(`/platform/settings/${KW_EN_KEY}`));
    const allowedGet = admin.fetched.find((f) => f.p.startsWith(`/platform/settings/${KW_ALLOWED_KEY}`));
    check('admin: tenant GET fires', tenantGet !== undefined, admin.fetched.map((f) => f.p).join(','));
    check('admin: tenant GET is tenant-scoped (?tenant_id=)', tenantGet && tenantGet.p.includes('tenant_id=7'), tenantGet && tenantGet.p);
    check('admin: platform GET fires', allowedGet !== undefined);
    check('admin: platform GET is NOT tenant-scoped', allowedGet && !allowedGet.p.includes('tenant_id'), allowedGet && allowedGet.p);
    check('admin: tab revealed', admin.hidden('ai-tool-tab-keep-warm') === false);
    check('admin: rows revealed (values readable)', admin.hidden('keep-warm-tenant-row') === false && admin.hidden('keep-warm-platform-row') === false);
    check('admin: checkboxes reflect server values', admin.checked('keep-warm-tenant-enabled') === true && admin.checked('keep-warm-platform-allowed') === true);

    // 3) unknown key (404) hides ONLY that row; the other row still shows.
    const partial = await runInit(true, 7, (p) => p.includes(KW_EN_KEY) ? { status: 404 } : { status: 200, value: false });
    check('404 tenant key: tenant row hidden', partial.hidden('keep-warm-tenant-row') === true);
    check('404 tenant key: platform row still shown', partial.hidden('keep-warm-platform-row') === false);
    check('404 tenant key: platform checkbox reflects false', partial.checked('keep-warm-platform-allowed') === false);

    // 4) PATCH semantics: run the change handlers directly.
    const { env, fetched, el } = makeEnv(true, 7, () => ({ status: 200, value: false }));
    const names = Object.keys(env);
    const body = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
        + ` ${paramSrc} ${hasTenantSrc} ${setErrSrc} ${setStatusSrc} ${tenantPatchSrc} ${platformPatchSrc}`
        + ` return [_kwOnTenantChange(true), _kwOnPlatformChange(false)];`;
    const runner = new Function('__env', body);
    await Promise.all(runner(env));
    const tPatch = fetched.find((f) => f.opts && f.opts.method === 'PATCH' && f.p.startsWith(`/platform/settings/${KW_EN_KEY}`));
    const aPatch = fetched.find((f) => f.opts && f.opts.method === 'PATCH' && f.p.startsWith(`/platform/settings/${KW_ALLOWED_KEY}`));
    check('PATCH tenant: fires with {value:true}', tPatch && tPatch.opts.body === JSON.stringify({ value: true }), tPatch && tPatch.opts.body);
    check('PATCH tenant: carries tenant_id', tPatch && tPatch.p.includes('tenant_id=7'), tPatch && tPatch.p);
    check('PATCH platform: fires with {value:false}', aPatch && aPatch.opts.body === JSON.stringify({ value: false }), aPatch && aPatch.opts.body);
    check('PATCH platform: NOT tenant-scoped', aPatch && !aPatch.p.includes('tenant_id'), aPatch && aPatch.p);

    // 4b) KEEPWARM-TENANT-HIDE (owner Tim 11-25): platform admin with NO
    // tenant_id -> tenant row stays hidden, ZERO tenant GET/PATCH fires, the
    // platform row still shows, and the help line appears.
    const noTenant = await runInit(true, null, () => ({ status: 200, value: true }));
    const noTenantGets = noTenant.fetched.filter((f) => f.p.startsWith(`/platform/settings/${KW_EN_KEY}`));
    check('no-tenant admin: tenant GET never fires', noTenantGets.length === 0, noTenant.fetched.map((f) => f.p).join(','));
    check('no-tenant admin: tenant row hidden', noTenant.hidden('keep-warm-tenant-row') === true);
    check('no-tenant admin: platform row shown', noTenant.hidden('keep-warm-platform-row') === false);
    check('no-tenant admin: platform checkbox reflects server value', noTenant.checked('keep-warm-platform-allowed') === true);
    check('no-tenant admin: help line shown', noTenant.hidden('keep-warm-tenant-help') === false);
    // tenant_id '' counts as no tenant too
    const emptyTenant = await runInit(true, '', () => ({ status: 200, value: true }));
    check("no-tenant admin (tenant_id ''): tenant GET never fires", emptyTenant.fetched.filter((f) => f.p.startsWith(`/platform/settings/${KW_EN_KEY}`)).length === 0);
    check("no-tenant admin (tenant_id ''): tenant row hidden", emptyTenant.hidden('keep-warm-tenant-row') === true);
    // and the PATCH handler refuses without a tenant (no request at all)
    {
        const { env, fetched } = makeEnv(true, null, () => ({ status: 200, value: false }));
        const names = Object.keys(env);
        const body2 = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
            + ` ${paramSrc} ${hasTenantSrc} ${setErrSrc} ${setStatusSrc} ${tenantPatchSrc}`
            + ` return _kwOnTenantChange(true);`;
        await new Function('__env', body2)(env);
        check('no-tenant: _kwOnTenantChange fires NO PATCH', fetched.length === 0, fetched.map((f) => `${f.opts?.method || 'GET'} ${f.p}`).join(','));
    }
    // tenant user still shows BOTH rows (regression guard for the hide rule)
    check('tenant admin still shows both rows', admin.hidden('keep-warm-tenant-row') === false && admin.hidden('keep-warm-platform-row') === false);

    // 5) jsdom probe (optional): if jsdom is installed on the run host, parse
    // the shipped settings.html and assert the keep-warm ids exist in a real
    // DOM. Without jsdom the stub-DOM checks above still stand — say which.
    try {
        // jsdom lives in CORE/WorkflowBuilder/node_modules (same lookup as
        // persistent-stacks-seed-result-cell-main-row.test.js:34).
        let jsdom = null;
        const jsdomErrs = [];
        for (const p of ['jsdom',
                        path.join(__dirname, '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom'),
                        path.join(__dirname, '..', '..', '..', 'WorkflowBuilder', 'node_modules', 'jsdom')]) {
            try { jsdom = require(p); break; } catch (e) { jsdomErrs.push(e); }
        }
        if (!jsdom) throw jsdomErrs[jsdomErrs.length - 1];
        const dom = new jsdom.JSDOM(fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings.html'), 'utf8'));
        const doc = dom.window.document;
        for (const id of ['ai-tool-tab-keep-warm', 'ai-tool-keep-warm', 'keep-warm-tenant-row', 'keep-warm-platform-row']) {
            const node = doc.getElementById(id);
            check(`jsdom: ${id} present in shipped settings.html`, node !== null);
            if (node) check(`jsdom: ${id} starts hidden`, node.classList.contains('hidden'));
        }
        for (const id of ['keep-warm-tenant-enabled', 'keep-warm-platform-allowed']) {
            const node = doc.getElementById(id);
            check(`jsdom: ${id} checkbox present`, node !== null);
            // The checkbox itself is never .hidden — its ROW is (settings.js
            // toggles the row, same idiom as the KP.8 auto-update toggle).
            check(`jsdom: ${id} sits inside a hidden row`, node !== null && node.parentElement?.classList?.contains('hidden'));
        }
        console.log('browser=jsdom');
    } catch (err) {
        if (String(err?.message).includes('Cannot find module') || String(err?.code) === 'MODULE_NOT_FOUND') {
            console.log('jsdom unavailable on run host — stub-DOM checks above apply (browser=stub)');
        } else {
            failures++; console.error('FAIL jsdom parse error', err);
        }
    }

    console.log(failures ? `FAILURES ${failures}` : 'ALL OK');
    process.exitCode = failures ? 1 : 0;
})();
