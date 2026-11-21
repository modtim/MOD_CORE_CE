// FIX-KNOWLEDGE-BASE-IN-CE (part 2) — the Knowledge Base panel must WORK on CE.
// Behavioural (no browser, no npm deps, no DB): the REAL gated functions are EXTRACTED
// VERBATIM from the shipped js/assistant-knowledge.js (marker + brace-match) and EXECUTED
// in a fresh node:vm sandbox per scenario (fetch stubbed + recorded, DOM stubbed), so the
// decision to call /assistant/* is made by RUNNING the shipped code, not by grepping it.
// Style of knowledge-base-ce-tab-present.check.js / failopen-d.check.js.
//
//   node CORE/Dashboard/tests/knowledge-base-ce-panel.check.js
//
// Pinned contract after the fix:
//   assistant-knowledge.js: NO `if (await _ceEdition()) return;` anywhere (0 occurrences),
//     no isPaidEdition import; the tenant-admin bail in initAssistantKnowledge
//     (`const admin = await isTenantAdmin();` -> `if (!admin) return;`) is INTACT.
//   Backend: routers/assistant_knowledge.py + assistant.py ship in CE (open-core/manifest
//   api_allow + deny_exceptions); every route is require_tenant_admin / require_active_and_onboarded_user
//   only. tenant_id is never sent by the client (the API derives it from the principal).
//
// Scenarios: 1-10 the eight calls fire on CE / SAAS / PRO + admin; 11-12 a tenant MEMBER
// produces ZERO /assistant/* fetches; 13 an errored entitlements lookup cannot disable the
// panel; 14 no request carries `tenant`; 15-16 shipped-file needles.
//
// RED on the HEAD (old) assistant-knowledge.js: every /assistant/* call was guarded by
// `if (await _ceEdition()) return;`, so on CE the panel returns early -> scenarios 1-10
// record ZERO /assistant/* fetches -> rc=1.

'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const JS = 'js/assistant-knowledge.js';
const SRC = fs.readFileSync(path.join(ROOT, JS), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// Verbatim slice of one function: indexOf the marker, brace-match to the closing brace.
function fn(marker) {
    let a = SRC.indexOf(marker);
    if (a < 0) throw new Error(`${marker} not found in ${JS}`);
    if (a >= 6 && SRC.slice(a - 6, a) === 'async ') a -= 6; // keep the async keyword
    let depth = 0;
    for (let i = SRC.indexOf('{', a); i < SRC.length; i++) {
        const c = SRC[i];
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (!depth) return SRC.slice(a, i + 1); }
    }
    throw new Error(`unbalanced braces from ${marker}`);
}

const NAMES = ['getApiUrl', 'authHeader', 'esc', 'fmtBytes', 'isTenantAdmin',
    'initAssistantKnowledge', '_loadFolders', '_loadConfig', 'onToggleEnabled',
    'onUpload', 'onAddText', 'onAddFolder', '_loadDocuments', 'onReindex', 'onDelete',
    '_renderRow', '_reflectEnabledState', '_setStatus'];
const EXTRACTED = NAMES.map((n) => fn('function ' + n + '(')).join('\n');
// The CE helper is prepended ONLY when the shipped file still defines it, so the OLD file
// genuinely runs the bail (clean red) and the NEW file needs no stub.
const HELPER = SRC.includes('async function _ceEdition() {') ? fn('function _ceEdition(') : '';
const PRE = "let _pollTimer = null; let _pollFailCount = 0; const POLL_MAX_FAILS = 5; const POLL_MS = 3000; const _STATUS_BADGE = { ready: 'b', processing: 'b', pending: 'b', failed: 'b' };\n"
    + 'function _wireEnableToggle() {}\nfunction _wireUpload() {}\nfunction _wirePasteText() {}\nfunction _wireFolder() {}\nfunction _schedulePoll(x) {}\n'
    + HELPER + '\n' + EXTRACTED + '\n';

const DOCS = [{ id: 9, title: 'Doc <script>One & co', source_type: 'upload', status: 'ready', chunk_count: 3, bytes: 1234 },
    { id: 10, title: 'Two', source_type: 'text', status: 'processing', chunk_count: 0, bytes: 2048 }];

function payload(url, method, member) {
    if (url.includes('/assistant/knowledge/upload')) return { title: 'Report & <b>One' };
    if (url.endsWith('/reindex')) return { ok: true };
    if (method === 'DELETE') return { ok: true };
    if (url.includes('/assistant/knowledge/folder')) return { documents: [{}, {}, {}] };
    if (url.includes('/assistant/knowledge/text')) return { id: 11 };
    if (url.includes('/assistant/config')) return method === 'PUT' ? { ok: true } : { knowledge_base_enabled: true };
    if (url.includes('/assistant/knowledge')) return { documents: DOCS };
    if (url.includes('/assets')) return [{ id: 5, cog_type: 'folder', name: 'Cog One' }];
    if (url.includes('/me')) return member ? { is_platform_admin: false, parent_user_id: 7 } : { is_platform_admin: false, parent_user_id: null };
    return {};
}

async function scenario(opts) {
    const els = {};
    const calls = [];
    const mk = (id) => {
        const e = { id, _cls: new Set(), textContent: '', innerHTML: '', value: '', checked: false, disabled: false, dataset: {}, _children: [] };
        e.classList = { add: (c) => e._cls.add(c), remove: (c) => e._cls.delete(c), contains: (c) => e._cls.has(c), toggle: (c, force) => { if (force) e._cls.add(c); else e._cls.delete(c); } };
        e.appendChild = (c) => { e._children.push(c); return c; };
        e.addEventListener = () => {};
        if (id === 'akb-text-title') e.value = 'Doc Title';
        if (id === 'akb-text-body') e.value = 'hello body';
        if (id === 'akb-folder-select') e.value = '5';
        if (id === 'akb-file') e.files = [{ name: 'report.pdf' }];
        return e;
    };
    const el = (id) => els[id] || (els[id] = mk(id));
    const fetchStub = (url, o) => {
        const method = o && o.method || 'GET';
        let body = null;
        if (o && o.body != null) body = (typeof o.body === 'string') ? (() => { try { return JSON.parse(o.body); } catch { return o.body; } })() : '[FormData]';
        calls.push({ method, url, body });
        const p = payload(url, method, opts.member);
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(p) });
    };
    const sandbox = {
        fetch: fetchStub,
        document: { getElementById: el, createElement: (t) => mk('#' + t) },
        window: { APP_CONFIG: { API_URL: 'https://api.test' }, confirm: () => true },
        sessionStorage: { getItem: () => 'tok' },
        setTimeout: setTimeout, console: console, FormData: FormData,
        parseInt: parseInt, String: String, Number: Number, JSON: JSON,
        isPaidEdition: () => opts.reject ? Promise.reject(new Error('entitlements lookup failed')) : Promise.resolve(opts.edition !== 'ce'),
    };
    vm.createContext(sandbox);
    const driver = opts.driver;
    let vmErr = null;
    try { await vm.runInContext('(async () => {' + PRE + driver + '})()', sandbox, { filename: 'assistant-knowledge' }); }
    catch (vmErr2) { vmErr = vmErr2; console.log('VMREJ ' + (vmErr2 && (vmErr2.stack || vmErr2.message))); }
    await new Promise((done) => setTimeout(done, 0));
    return { els, calls, vmErr };
}

const asCalls = (r) => r.calls.filter((c) => c.url.includes('/assistant/'));
const has = (r, m, p) => r.calls.some((c) => c.method === m && c.url.includes(p));
const INIT = 'await initAssistantKnowledge(); ';

(async () => {
    const all = [];
    // 1) CE + tenant admin -> the document list LOADS and paints rows (the owner fix).
    let r = await scenario({ edition: 'ce', admin: true, driver: INIT });
    all.push(r);
    check('kb ce admin: GET /assistant/knowledge fires', has(r, 'GET', '/assistant/knowledge') && !has(r, 'POST', '/assistant/knowledge/'));
    check('kb ce admin: 2 rows painted in #akb-tbody', (r.els['akb-tbody'] || { _children: [] })._children.length === 2);
    // 2-6) CE + admin -> upload / paste-text / folder / reindex / delete all POST.
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + 'await onUpload(); ' });
    all.push(r);
    check('kb ce admin: POST /assistant/knowledge/upload fires', has(r, 'POST', '/assistant/knowledge/upload'));
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + 'await onAddText(); ' });
    all.push(r);
    check('kb ce admin: POST /assistant/knowledge/text fires', has(r, 'POST', '/assistant/knowledge/text'));
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + 'await onAddFolder(); ' });
    all.push(r);
    check('kb ce admin: POST /assistant/knowledge/folder fires', has(r, 'POST', '/assistant/knowledge/folder'));
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + 'await onReindex(9); ' });
    all.push(r);
    check('kb ce admin: POST /assistant/knowledge/9/reindex fires', has(r, 'POST', '/assistant/knowledge/9/reindex'));
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + 'await onDelete(9, "Doc One"); ' });
    all.push(r);
    check('kb ce admin: DELETE /assistant/knowledge/9 fires', has(r, 'DELETE', '/assistant/knowledge/9'));
    // 7) CE + admin -> /assistant/config GETs and knowledge_base_enabled is reflected.
    r = await scenario({ edition: 'ce', admin: true, driver: INIT });
    all.push(r);
    check('kb ce admin: GET /assistant/config fires', has(r, 'GET', '/assistant/config'));
    check('kb ce admin: #akb-enabled checked from knowledge_base_enabled', (r.els['akb-enabled'] || { checked: false }).checked === true);
    check('kb ce admin: _reflectEnabledState ran (note hidden, sources opaque-off)', r.els['akb-disabled-note'] && r.els['akb-disabled-note']._cls.has('hidden') && !r.els['akb-sources']._cls.has('opacity-50'));
    // 8) CE + admin -> the enable toggle PUTs /assistant/config.
    r = await scenario({ edition: 'ce', admin: true, driver: INIT + "document.getElementById('akb-enabled').checked = true; await onToggleEnabled({ target: document.getElementById('akb-enabled') }); " });
    all.push(r);
    check('kb ce admin: PUT /assistant/config fires', has(r, 'PUT', '/assistant/config'));
    // 9,10) SAAS + PRO + admin -> the paid path is unchanged (same calls).
    r = await scenario({ edition: 'saas', admin: true, driver: INIT + 'await onUpload(); await onReindex(9); ' });
    all.push(r);
    check('kb saas admin: upload + reindex still fire', has(r, 'POST', '/assistant/knowledge/upload') && has(r, 'POST', '/assistant/knowledge/9/reindex'));
    r = await scenario({ edition: 'pro', admin: true, driver: INIT + 'await onAddText(); await onDelete(9, "Two"); ' });
    all.push(r);
    check('kb pro admin: text + delete still fire', has(r, 'POST', '/assistant/knowledge/text') && has(r, 'DELETE', '/assistant/knowledge/9'));
    // 11,12) tenant MEMBER (isTenantAdmin false) -> the init bail holds: ZERO /assistant/* calls.
    r = await scenario({ edition: 'ce', member: true, driver: INIT });
    all.push(r);
    check('kb member ce: zero /assistant/* fetches', asCalls(r).length === 0);
    r = await scenario({ edition: 'saas', member: true, driver: INIT });
    all.push(r);
    check('kb member saas: zero /assistant/* fetches', asCalls(r).length === 0);
    // 13) errored entitlements lookup + admin -> no edition gate left, calls still fire.
    r = await scenario({ reject: true, admin: true, driver: INIT + 'await onUpload(); ' });
    all.push(r);
    check('kb entitlements-reject admin: calls still fire', has(r, 'GET', '/assistant/knowledge') && has(r, 'POST', '/assistant/knowledge/upload'));
    // 14) no recorded request carries `tenant` in its url or body (API derives it).
    const leak = all.reduce((n, rr) => n + rr.calls.filter((c) => String(c.url).includes('tenant') || String(JSON.stringify(c.body || '')).includes('tenant')).length, 0);
    check('no recorded /assistant request carries tenant', leak === 0);
    // 15,16) the shipped file: every CE bail gone, the tenant-admin guard intact.
    check('shipped assistant-knowledge.js has 0 CE bails', (SRC.match(/if \(await _ceEdition\(\)\) return;/g) || []).length === 0);
    check('shipped assistant-knowledge.js keeps isTenantAdmin + the member bail', SRC.includes('isTenantAdmin') && SRC.includes('if (!admin) return;'));
    check('shipped assistant-knowledge.js has no edition import', !SRC.includes('import { isPaidEdition }'));

    console.log(`\nknowledge-base-ce-panel.check.js: ${failures ? 'FAILED (' + failures + ')' : 'all asserts passed'}`);
    process.exit(failures ? 1 : 0);
})().catch((err) => { console.log('CAUGHT typeof=' + typeof err + ' msg=' + (err && err.message) + ' stack=' + (err && err.stack)); process.exit(1); });
