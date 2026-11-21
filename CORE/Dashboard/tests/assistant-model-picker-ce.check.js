// FIX-CE-ASSISTANT-MODEL-PICKER-P2 — the Settings -> AI assistant-model picker
// must WORK on CE and list ONLY the models THIS tenant actually has deployed.
// Behavioural (no browser, no npm deps, no DB): the REAL shipped functions are
// EXTRACTED VERBATIM from CORE/Dashboard/js/assistant-model-picker.js (marker +
// brace-match) and EXECUTED in a fresh node:vm sandbox per scenario (fetch
// stubbed + recorded, DOM stubbed), so the decision to GET/PUT
// /platform/ai-config/assistant-model-choice is made by RUNNING the shipped
// code, not by grepping it. Style of knowledge-base-ce-panel.check.js.
//
//   node CORE/Dashboard/tests/assistant-model-picker-ce.check.js
//
// Pinned contract after the fix:
//   assistant-model-picker.js: NO isPaidEdition import, no CE bail; the
//     tenant-admin bail in initAssistantModelPicker is INTACT; the options come
//     ONLY from the tenant DTO (GET /platform/ai-config/assistant-model-choice)
//     — never the platform-wide /platform/ai-config/ai-stacks list; a malformed
//     DTO token is refused client-side (zero PUTs); no request carries tenant.
//   settings.js: exactly ONE `import './assistant-model-picker.js';`.
//
// Scenarios: 1-4 CE/SAAS/PRO admin GETs + PUTs (incl. the "" clear); 5-6 a
//   tenant MEMBER renders the hint and issues ZERO PUTs; 7 the rows come from
//   the DTO only (tenant B's stacks paint in the B sandbox, never in A); 8 an
//   errored GET renders the hint and ZERO PUTs; 9 no request carries tenant;
//   10 a malformed DTO token is refused client-side; 11 no edition branch; 12
//   the settings.js import line exactly once; 13 empty options -> hint + zero
//   PUTs; 14 the server detail surfaces verbatim; 15 no secret/key_ref painted.
//
// RED on the pre-fix tree: js/assistant-model-picker.js does not exist, so the
// readFileSync at the top throws ENOENT and the check exits rc=1 BEFORE any
// scenario runs (a brand-new file has no HEAD blob, so no MCW_OVERRIDE applies).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const JS = 'js/assistant-model-picker.js';
const SRC = fs.readFileSync(path.join(ROOT, JS), 'utf8');
const SETTINGS = fs.readFileSync(path.join(ROOT, 'js', 'settings.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

function fn(marker) {
    let a = SRC.indexOf(marker);
    if (a < 0) throw new Error(`${marker} not found in ${JS}`);
    if (a >= 6 && SRC.slice(a - 6, a) === 'async ') a -= 6;
    let depth = 0;
    for (let i = SRC.indexOf('{', a); i < SRC.length; i++) {
        const c = SRC[i];
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (!depth) return SRC.slice(a, i + 1); }
    }
    throw new Error(`unbalanced braces from ${marker}`);
}

function constLine(name) {
    const at = SRC.indexOf('const ' + name + ' = ');
    if (at < 0) throw new Error(`${name} not found in ${JS}`);
    const end = SRC.indexOf(';\n', at);
    if (end < 0) throw new Error(`no end for ${name}`);
    return SRC.slice(at, end + 1);
}

const NAMES = ['getApiUrl', 'authHeader', 'esc', 'isTenantAdmin', '_amcTokenOk',
    '_amcSurface', '_setHint', '_mountPicker', '_paint', '_wireSelect',
    '_loadChoice', '_putChoice', 'initAssistantModelPicker', 'onPickChange'];
const PRE = constLine('AMC_ENDPOINT') + '\n' + constLine('AMC_HINT') + '\n'
    + 'let _amcOptions = []; let _amcWired = false; let _amcLoaded = false;\n'
    + NAMES.map((n) => fn('function ' + n + '(')).join('\n') + '\n';

const DTO_A = { configured: '', options: [
    { token: 'stack:101', label: 'Alpha Stack One', kind: 'stack', model: 'qwen', status: 'ready' },
    { token: 'external:http://api.invalid/v1', label: 'Alpha External', kind: 'external', model: '', status: 'ready' }] };
const DTO_B = { configured: 'stack:202', options: [
    { token: 'stack:202', label: 'Bravo Stack Only', kind: 'stack', model: 'qwen', status: 'ready' }] };
const DTO_BAD = { configured: '', options: [
    { token: 'wat', label: 'Malformed One', kind: 'stack', model: '', status: 'ready' },
    { token: 'external:', label: 'Empty External', kind: 'external', model: '', status: 'ready' },
    { token: 'stack:101', label: 'Alpha Stack One', kind: 'stack', model: 'qwen', status: 'ready' }] };
const DTO_EMPTY = { configured: '', options: [] };
const PLATFORM_DTO = { configured: '', options: [
    { token: 'stack:101', label: 'Alpha Stack One', kind: 'stack', model: 'qwen', status: 'ready' },
    { token: 'stack:202', label: 'Bravo Stack Only', kind: 'stack', model: 'qwen', status: 'ready' }] };
const GET_DETAIL = 'Only the tenant admin can pick the assistant model.';
const PUT_DETAIL = 'Token rejected: not a stack or external connection this tenant can use.';

async function scenario(opts) {
    const els = {};
    const calls = [];
    const mk = (id) => {
        const e = { id, dataset: {}, innerHTML: '', textContent: '', value: '', _children: [] };
        e.appendChild = (c) => { e._children.push(c); return c; };
        e.addEventListener = () => {};
        return e;
    };
    const el = (id) => els[id] || (els[id] = mk(id));
    const respond = (url, method) => {
        if (url.includes('/me')) {
            const me = opts.member ? { is_platform_admin: false, parent_user_id: 7 } : { is_platform_admin: false, parent_user_id: null };
            return { ok: true, status: 200, json: () => Promise.resolve(me) };
        }
        if (url.includes('ai-stacks')) return { ok: true, status: 200, json: () => Promise.resolve(PLATFORM_DTO) };
        if (method === 'GET' && opts.getErr) return { ok: false, status: 403, json: () => Promise.resolve({ detail: GET_DETAIL }) };
        if (method === 'PUT' && opts.putErr) return { ok: false, status: 422, json: () => Promise.resolve({ detail: PUT_DETAIL }) };
        return { ok: true, status: 200, json: () => Promise.resolve(opts.dto) };
    };
    const fetchStub = (url, o) => {
        const method = o && o.method || 'GET';
        let body = null;
        if (o && o.body != null) body = (typeof o.body === 'string') ? (() => { try { return JSON.parse(o.body); } catch { return o.body; } })() : '[FormData]';
        calls.push({ method, url, body });
        return Promise.resolve(respond(url, method));
    };
    const sandbox = {
        fetch: fetchStub,
        document: { getElementById: el, createElement: (t) => mk('#' + t), body: el('body') },
        window: { APP_CONFIG: { API_URL: 'https://api.test' } },
        sessionStorage: { getItem: () => 'tok' },
        setTimeout: setTimeout, console: console, JSON: JSON, Array: Array, String: String, Number: Number,
    };
    vm.createContext(sandbox);
    let vmErr = null;
    try { await vm.runInContext('(async () => {' + PRE + opts.driver + '})()', sandbox, { filename: 'assistant-model-picker' }); }
    catch (vmErr2) { vmErr = vmErr2; console.log('VMREJ ' + (vmErr2 && (vmErr2.stack || vmErr2.message))); }
    await new Promise((done) => setTimeout(done, 0));
    return { els, calls, vmErr };
}

const AMC = (r) => r.calls.filter((c) => c.url.includes('assistant-model-choice'));
const puts = (r) => AMC(r).filter((c) => c.method === 'PUT');
const gets = (r) => AMC(r).filter((c) => c.method === 'GET');
const rows = (r) => (r.els['amc-choice'] || { innerHTML: '' }).innerHTML;
const nOpts = (r) => (rows(r).match(/<option/g) || []).length;
const hint = (r) => (r.els['amc-hint'] || { textContent: '' }).textContent;
const err = (r) => (r.els['amc-error'] || { textContent: '' }).textContent;
const INIT = 'await initAssistantModelPicker(); ';
const PICK = "const _sel = document.getElementById('amc-choice'); _sel.value = 'stack:101'; await onPickChange(_sel); ";
const CLEAR = "const _sel = document.getElementById('amc-choice'); _sel.value = ''; await onPickChange(_sel); ";
const PICK_BAD = "const _sel = document.getElementById('amc-choice'); _sel.value = 'external:'; await onPickChange(_sel); ";

(async () => {
    const all = [];
    let r = await scenario({ edition: 'ce', dto: DTO_A, driver: INIT + PICK });
    all.push(r);
    check('1 ce admin: GET + PUT assistant-model-choice fire', gets(r).length === 1 && puts(r).length === 1);
    check('1 ce admin: PUT body is {token} only', puts(r).some((c) => c.body && Object.keys(c.body).length === 1 && c.body.token === 'stack:101'));
    r = await scenario({ edition: 'saas', dto: DTO_A, driver: INIT + PICK });
    all.push(r);
    check('2 saas admin: GET + PUT fire (no edition branch)', gets(r).length === 1 && puts(r).length === 1);
    r = await scenario({ edition: 'pro', dto: DTO_A, driver: INIT + PICK });
    all.push(r);
    check('3 pro admin: GET + PUT fire', gets(r).length === 1 && puts(r).length === 1);
    r = await scenario({ edition: 'ce', dto: DTO_A, driver: INIT + CLEAR });
    all.push(r);
    check('4 ce admin: the "" clear PUTs once', puts(r).length === 1 && puts(r).every((c) => c.body.token === ''));

    r = await scenario({ edition: 'ce', member: true, dto: DTO_A, driver: INIT + PICK });
    all.push(r);
    check('5 ce member: ZERO PUT fetches', puts(r).length === 0);
    check('5 ce member: the hint still renders', hint(r) === 'Pick one of your stacks here, or fill in the Provider, Model and Base URL on the AI Assistant tab and save.');
    r = await scenario({ edition: 'saas', member: true, dto: DTO_B, driver: INIT + PICK });
    all.push(r);
    check('6 saas member: ZERO PUT fetches', puts(r).length === 0 && gets(r).length === 0);

    const a = await scenario({ edition: 'ce', dto: DTO_A, driver: INIT });
    const b = await scenario({ edition: 'ce', dto: DTO_B, driver: INIT });
    all.push(a); all.push(b);
    check('7 tenant A paints 2 DTO options', nOpts(a) === 3);
    check('7 tenant A never paints tenant B rows', rows(a).includes('Alpha Stack One') && !rows(a).includes('Bravo Stack Only'));
    check('7 tenant B paints only its own stack', rows(b).includes('Bravo Stack Only') && !rows(b).includes('Alpha Stack One'));
    check('7 the DTO is the only read (no ai-stacks fetch)', AMC(a).length === 1 && a.calls.every((c) => !c.url.includes('ai-stacks')));

    r = await scenario({ edition: 'ce', getErr: true, dto: DTO_A, driver: INIT + PICK });
    all.push(r);
    check('8 errored GET: hint renders', hint(r) === 'Pick one of your stacks here, or fill in the Provider, Model and Base URL on the AI Assistant tab and save.');
    check('8 errored GET: ZERO PUTs', puts(r).length === 0);
    check('8 errored GET: the server detail surfaces verbatim', err(r) === GET_DETAIL);

    r = await scenario({ edition: 'ce', dto: DTO_BAD, driver: INIT + PICK_BAD });
    all.push(r);
    check('10 malformed DTO tokens are not offered', !rows(r).includes('Malformed One') && !rows(r).includes('Empty External') && nOpts(r) === 2);
    check('10 malformed pick refused client-side: ZERO PUTs', puts(r).length === 0);

    r = await scenario({ edition: 'ce', dto: DTO_EMPTY, driver: INIT + PICK });
    all.push(r);
    check('13 no models deployed: hint renders', hint(r) === 'Pick one of your stacks here, or fill in the Provider, Model and Base URL on the AI Assistant tab and save.');
    check('13 no models deployed: ZERO PUTs', puts(r).length === 0);

    r = await scenario({ edition: 'ce', dto: DTO_A, putErr: true, driver: INIT + PICK });
    all.push(r);
    check('14 rejected PUT: server detail verbatim, no retry loop', err(r) === PUT_DETAIL && puts(r).length === 1);

    const leak = all.reduce((n, rr) => n + rr.calls.filter((c) => String(c.url).includes('tenant') || String(JSON.stringify(c.body || '')).includes('tenant')).length, 0);
    check('9 no recorded request carries tenant/tenant_id', leak === 0);
    check('11 shipped module has no isPaidEdition import and no CE bail', !SRC.includes('isPaidEdition') && !SRC.includes('_ceEdition()'));
    check('12 shipped settings.js imports the module exactly once', (SETTINGS.match(/import '\.\/assistant-model-picker\.js';/g) || []).length === 1);
    check('15 no secret/key_ref/Authorization is painted', !rows(a).includes('key_ref') && !rows(a).includes('Bearer') && !err(a).includes('Bearer'));
    check('16 the module never reads the platform-wide ai-stacks list', SRC.indexOf('ai-stacks') < 0);
    check('17 the module renders its own DOM in main-panel-ai', SRC.includes("getElementById('main-panel-ai')") && SRC.includes('amcBound'));

    console.log(`\nassistant-model-picker-ce.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'} (${all.length} scenarios)`);
    process.exit(failures ? 1 : 0);
})().catch((err2) => { console.log('CAUGHT typeof=' + typeof err2 + ' msg=' + (err2 && err2.message) + ' stack=' + (err2 && err2.stack)); process.exit(1); });
