'use strict';
// D75 (): a failed stack-list fetch must NOT leave the pickers
// empty with a 'No stacks yet' lie. Both fetch sites (the create-key modal's
// /stacks read and the AI-config ai-stacks read that feeds the slot pickers)
// now retry up to 3 times 1.5 s apart, never cache a failed read as [], and
// only say 'No stacks yet' when the read SUCCEEDED with zero stacks.
//
// Same technique as settings-aicfg-easy-stack-picker.check.js: the shipped code
// is EXTRACTED VERBATIM from settings.js and run against a stub DOM, so the
// assertions test the shipped source, not a copy. setTimeout is shadowed so the
// 1.5 s backoff runs instantly in the test.
//
//   node CORE/Dashboard/tests/settings-stack-picker-retry-d75.check.js
const fs = require('fs');
const path = require('path');
const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

let failures = 0;
function check(name, cond, extra) {
    if (cond) console.log(`ok   ${name}`);
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
        if (src[i] === '{') depth += 1;
        else if (src[i] === '}') { depth -= 1; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function fnSrc(hdr) {
    const at = settingsSrc.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in settings.js`);
    return `${hdr} ${blockAt(settingsSrc, settingsSrc.indexOf('{', at + hdr.length))}`;
}

const RETRY_MSG = 'Could not load your stacks: retrying';
const FAIL_MSG = 'Could not load your stacks. Reload to try again.';
const NONE_MSG = 'No stacks yet: create one on the Stacks tab, then pick it here.';

// A0 red-before guard: the unedited tree has none of these.
check('A0.1 shipped source carries the could-not-load hint', settingsSrc.includes(FAIL_MSG), FAIL_MSG);
check('A0.2 shipped source carries the retrying hint', settingsSrc.includes(RETRY_MSG), RETRY_MSG);
check('A0.3 the no-stacks line branches on a failed read', settingsSrc.includes('_aiStacksLast === null'), 'renderOne');
check('A0.4 a 3-attempt retry loop is present', settingsSrc.includes('attempt <= 3'), 'both fetch sites');

const populateSrc = fnSrc('async function populateApiKeyStackOptions()');
const loadStacksSrc = fnSrc('async function _loadAiStacksSection()');

const HINT_START = '        if (!stackOptions) {';
const HINT_END = '            row.appendChild(none);\n        }';
const hs = settingsSrc.indexOf(HINT_START);
const he = settingsSrc.indexOf(HINT_END);
check('A1 the no-stacks block is present verbatim', hs >= 0 && he > hs, 'renderOne');
const hintBlock = hs >= 0 && he > hs ? settingsSrc.slice(hs, he + HINT_END.length) : '';

const names = ['apiCall', 'setApiKeyStackHint', 'setTimeout', 'document', 'fetch', 'window',
    '_aiStacksLast', '_aiStacksNotice', '_renderAiStacksSection', '_aiStacksKeepHidden',
    'currentUser', '_authToken', 'Promise'];
const prelude = names.map((n) => `var ${n} = __env.${n};`).join(' ');

function mkEl(tag) {
    const e = { tag, dataset: {}, className: '', textContent: '', value: '', options: [], children: [] };
    e.appendChild = (c) => { e.children.push(c); if (tag === 'select') e.options.push(c); };
    e.querySelectorAll = () => [];
    e.setAttribute = (k, v) => { e.dataset[k] = v; };
    e.classList = { add: () => {}, remove: () => {} };
    e.querySelector = () => null;
    e.firstChild = undefined;
    e.removeChild = () => {};
    e.outerHTML = '<p></p>';
    return e;
}
const mkSelect = () => mkEl('select');
const mkDiv = () => mkEl('div');
function mkDoc(sel) {
    return {
        getElementById: (id) => (id === 'api-key-stack-id' ? sel : id === 'ai-stacks-section' ? mkEl('div') : null),
        createElement: (tag) => mkEl(tag),
        querySelectorAll: () => [],
    };
}

(async () => {
    const runPop = new Function('__env', `${prelude} ${populateSrc} return populateApiKeyStackOptions;`);
    const runLoad = new Function('__env', `${prelude} ${loadStacksSrc} return (async () => { await _loadAiStacksSection(); return _aiStacksLast; })();`);
    const runHint = new Function('__env', `${prelude} var row = __env.row; var stackOptions = 0; ${hintBlock}`);

    // --- case 1: /stacks fails twice then succeeds -> select ends populated.
    let calls = 0;
    const sel1 = mkSelect();
    const env1 = { apiCall: async () => { calls += 1; if (calls <= 2) throw new Error('HTTP 500'); return [{ id: 7, name: 'Alpha', status: 'running' }, { id: 8, name: 'Beta', status: 'torn_down' }]; },
        setApiKeyStackHint: (t) => { env1.hint = t; }, setTimeout: (fn, ms) => { fn(); return 0; },
        document: mkDoc(sel1), Promise, window: {}, fetch: () => {}, currentUser: null, _authToken: () => '',
        _aiStacksLast: null, _aiStacksNotice: () => {}, _renderAiStacksSection: () => {}, _aiStacksKeepHidden: false };
    await runPop(env1)();
    check('C1 a twice-failed then ok read retries then populates the select',
        calls === 3 && sel1.options.length === 2 && sel1.options[1].value === '7' && sel1.options[0].value === '',
        `calls=${calls} opts=${sel1.options.map((o) => o.value)}`);
    check('C1b the retry line was shown between attempts', env1.hint === '', 'cleared after success');

    // --- case 2: always fails -> failure message, never 'No stacks yet'.
    calls = 0;
    const sel2 = mkSelect();
    const env2 = { apiCall: async () => { calls += 1; throw new Error('offline'); },
        setApiKeyStackHint: (t) => { env2.hint = t; }, setTimeout: (fn, ms) => { fn(); return 0; },
        document: mkDoc(sel2), Promise, window: {}, fetch: () => {}, currentUser: null, _authToken: () => '',
        _aiStacksLast: null, _aiStacksNotice: () => {}, _renderAiStacksSection: () => {}, _aiStacksKeepHidden: false };
    await runPop(env2)();
    check('C2 an always-failing read retries 3 times', calls === 3, `calls=${calls}`);
    check('C2b the hint is the could-not-load line', env2.hint === FAIL_MSG, env2.hint);
    check('C2c never a no-stacks lie on a failed read', env2.hint !== NONE_MSG, env2.hint);

    // --- case 3: succeeds with zero stacks -> one call, no error hint.
    calls = 0;
    const sel3 = mkSelect();
    const env3 = { apiCall: async () => { calls += 1; return []; },
        setApiKeyStackHint: (t) => { env3.hint = t; }, setTimeout: (fn, ms) => { fn(); return 0; },
        document: mkDoc(sel3), Promise, window: {}, fetch: () => {}, currentUser: null, _authToken: () => '',
        _aiStacksLast: null, _aiStacksNotice: () => {}, _renderAiStacksSection: () => {}, _aiStacksKeepHidden: false };
    await runPop(env3)();
    check('C3 an empty success is one call', calls === 1, `calls=${calls}`);
    check('C3b no error hint on an empty success', env3.hint === '', env3.hint);

    // --- ai-stacks read: fails twice then ok -> _aiStacksLast set, never [].
    let f = 0;
    const env4 = { fetch: (u, o) => { if (!u.includes('ai-stacks')) return { ok: false, status: 404 }; f += 1; if (f <= 2) throw new Error('ECONNRESET'); return { ok: true, json: async () => ({ stacks: [{ stack_id: 3, name: 'A', kind: 'chat', status: 'running' }] }) }; },
        setTimeout: (fn, ms) => { fn(); return 0; },
        window: { APP_CONFIG: { API_URL: '' } },
        currentUser: { is_platform_admin: true }, _authToken: () => 'tok',
        _aiStacksNotice: (d) => { env4.notice = d; }, _renderAiStacksSection: () => {},
        _aiStacksKeepHidden: true, _aiStacksLast: null, Promise, document: mkDoc(), apiCall: async () => [],
        setApiKeyStackHint: () => {} };
    const last4 = await runLoad(env4);
    check('C4 a twice-failed then ok ai-stacks read sets _aiStacksLast',
        last4 && Array.isArray(last4.stacks) && last4.stacks.length === 1, last4);

    // --- ai-stacks read always fails -> _aiStacksLast stays null (never []).
    f = 0;
    const env5 = { fetch: () => { f += 1; throw new Error('down'); },
        setTimeout: (fn, ms) => { fn(); return 0; },
        window: { APP_CONFIG: { API_URL: '' } },
        currentUser: { is_platform_admin: true }, _authToken: () => 'tok',
        _aiStacksNotice: (d) => { env5.notice = d; }, _renderAiStacksSection: () => {},
        _aiStacksKeepHidden: true, _aiStacksLast: null, Promise, document: mkDoc(), apiCall: async () => [],
        setApiKeyStackHint: () => {} };
    const last5 = await runLoad(env5);
    check('C5 a failed ai-stacks read is never cached as empty', last5 === null, last5);
    check('C5b the notice is not blank', typeof env5.notice === 'string' && env5.notice.length > 0, env5.notice);

    // --- the Easy-table line: failed read vs true empty.
    const env6 = { row: mkDiv(), _aiStacksLast: null, document: mkDoc() };
    runHint(env6);
    const none6 = env6.row.children.find((c) => c.dataset.role === 'slot-no-stacks');
    check('C6 a failed read says could-not-load, not no-stacks-yet',
        none6 && none6.textContent === FAIL_MSG, none6 && none6.textContent);
    const env7 = { row: mkDiv(), _aiStacksLast: { stacks: [] }, document: mkDoc() };
    runHint(env7);
    const none7 = env7.row.children.find((c) => c.dataset.role === 'slot-no-stacks');
    check('C7 a true empty read still says no stacks yet',
        none7 && none7.textContent === NONE_MSG, none7 && none7.textContent);

    console.log(failures ? `FAILURES ${failures}` : 'settings-stack-picker-retry-d75: all assertions passed');
    process.exitCode = failures ? 1 : 0;
})();
