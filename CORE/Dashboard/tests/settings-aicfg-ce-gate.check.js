// rc20 aicfg-gate check — the Settings -> AI Configuration page's stack-option
// READ and the AI-tool SAVE writes sit on routes that SHIP in CE: open-core/manifest.yaml:812
// lists CORE/API/routers/ai_config.py under api_allow (AA-CE comment :811) and CORE/API/main.py:2343
// mounts the router, so GET /platform/ai-config/stack-options and PUT /platform/ai-config/{tool}
// (routers/ai_config.py:3757) answer on CE and the User-RAG / Node Wizard / Repair embedding
// picks must persist there (owner 11-08: keep VISIBLE AND WORKING in CE, tasks.md:330 BUG.B58).
//   GET /v1/platform/ai-config/stack-options  — settings.js:3614; routers/ai_config.py:3043
//   PUT /v1/platform/ai-config/{tool}         — settings.js:5001; routers/ai_config.py:3757
//   PUT /v1/platform/ai-config/critic         — settings.js:5027; ai_config.py:59 (critic pseudo-tool)
// The critic pseudo-tool is the ONLY paid-only write left on the save path, so it keeps its
// own `await isPaidEdition()` guard; the four tool PUTs fire on every edition.
//
// Same technique as settings-ce-gate-s20.check.js / admin-nodes-packages-ce-gate.check.js:
// the gate code is EXTRACTED VERBATIM from the shipped settings.js text and run
// against a stub DOM, so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/settings-aicfg-ce-gate.check.js
//
// Asserts: CE -> the four tool PUTs fire, the save status reports a save, and no critic PUT
// fires; paid -> the reads/PUTs still fire (Pro behaviour unchanged); old-file state (the
// CE bail still on the save path) -> throw -> non-zero exit = FAIL.
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
    if (at < 0) throw new Error(`${hdr} not found in settings.js`);
    return `${hdr} ${blockAt(settingsSrc, settingsSrc.indexOf('{', at + hdr.length))}`;
}

const STACK_HDR = 'async function _loadStackOptions()';
const SAVE_HDR = 'async function handleSaveAllAIConfig(e)';
const stackSrc = fnSrc(STACK_HDR);
const saveSrc = fnSrc(SAVE_HDR);

const GATE = 'await isPaidEdition()';
if (saveSrc.indexOf('not available in MOD Community Edition') >= 0) throw new Error('save path still bails on CE (old-file state)');
if (saveSrc.indexOf('/platform/ai-config/${tool}') < 0) throw new Error('save path lost the tool PUT loop (old-file state)');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

function makeEnv(paid, wizForm) {
    const fetched = [];
    const els = {};
    const status = { classes: new Set(['hidden']), textContent: '', get text() { return this.textContent; } };
    function el(id) {
        if (!els[id]) els[id] = { classes: new Set(['hidden']) };
        return els[id];
    }
    const doc = {
        getElementById: (id) => (id === 'ai-save-status' ? status : el(id)),
        querySelector: (sel) => wizForm,
        querySelectorAll: () => [],
    };
    status.classList = { add: (c) => status.classes.add(c), remove: (c) => status.classes.delete(c) };
    const env = {
        document: doc,
        window: { APP_CONFIG: { API_URL: 'http://api.test' } },
        sessionStorage: { getItem: () => 'tok' },
        isPaidEdition: () => paid,
        fetch: (u) => { fetched.push(u); return { ok: true, json: async () => [] }; },
        setTimeout: () => {},
        AI_TOOLS: ['wizard', 'adapter', 'assistant', 'repair'],
        _buildToolBody: (t) => ({ tool: t }),
        _buildCriticBody: () => ({ critic: 1 }),
        _captureCriticState: () => {},
        _aiFormState: { wizard: { critic_mode: 'reasoner' } },
        showError: (m) => { throw new Error('showError should not run: ' + m); },
    };
    return { env, fetched, status, wizForm };
}

async function run(paid, wizForm) {
    const { env, fetched, status } = makeEnv(paid, wizForm);
    const names = Object.keys(env);
    const body = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
        + ` ${stackSrc} ${saveSrc}`
        + ` return (async () => { await _loadStackOptions(); await handleSaveAllAIConfig(null); })();`;
    await (await new Function('__env', body)(env));
    return { fetched, status };
}

(async () => {
    const ce = await run(false, null);
    const ceCritic = await run(false, { querySelector: () => ({ value: 'reasoner' }) });
    check('CE: the four tool PUTs fire (the User-RAG pick persists)',
        ce.fetched.filter((u) => /\/platform\/ai-config\/(wizard|adapter|assistant|repair)$/.test(u)).length === 4, ce.fetched.join(','));
    check('CE: no critic PUT (paid-only pseudo-tool stays gated)',
        !ceCritic.fetched.some((u) => u.endsWith('/platform/ai-config/critic')), ceCritic.fetched.join(','));
    check('CE: save status reports a save, not the CE-absent line',
        ce.status.text.indexOf('Saved all AI tool settings') >= 0, ce.status.text);

    const pro = await run(true, null);
    check('paid: stack-options read still fires', pro.fetched.some((u) => u.endsWith('/platform/ai-config/stack-options')), pro.fetched.join(','));
    check('paid: the four tool PUTs still fire', pro.fetched.filter((u) => /\/platform\/ai-config\/(wizard|adapter|assistant|repair)$/.test(u)).length === 4, pro.fetched.join(','));

    const proCritic = await run(true, { querySelector: () => ({ value: 'reasoner' }) });
    check('paid + separate reasoner: critic PUT still fires', proCritic.fetched.some((u) => u.endsWith('/platform/ai-config/critic')), proCritic.fetched.join(','));

    check('settings.js imports the existing isPaidEdition from ./api.js',
        /import\s*\{[^}]*\bisPaidEdition\b[^}]*\}\s*from\s*'\.\/api\.js';/.test(settingsSrc));
    check('no new edition helper invented',
        !/function\s+_?ceEdition|const\s+_ceAicfgGate\s*=/.test(settingsSrc));

    console.log(failures ? `FAILURES ${failures}` : 'ALL OK');
    process.exitCode = failures ? 1 : 0;
})();
