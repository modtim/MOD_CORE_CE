// rc20-s20 check — CE edition gates on the AI-Config sub-surfaces whose whole
// backend is CE-denied, so their reads used to fire 404s the e2e net-guard
// flags (dash-settings.spec.ts row 31, sweep result-08):
//   /v1/system/wizard-ml/*            — Wizard Learning sub-tab; services/wizard_ml/**
//     is in open-core/manifest.yaml deny:, so main.py's try-mount of routers/wizard_ml_admin.py
//     (:2970) is skipped in the CE tree and every read 404s.
//   /v1/platform/managed-ai/setting   — master-bar tier read; routers/ai_config.py
//     (:2660) is CE-denied (the same denial loadAIConfig already fail-opens on at
//     settings.js:3347 _ceNoAiCfg).
//   /v1/platform/settings/wizard_knowledge_auto_update_enabled — KP.7 auto-update row;
//     services/wizard_knowledge_settings.py is CE-denied (manifest deny:, :1246), so
//     routers/platform_settings.py GET /{key} 404s the unknown key (:457).
//
// Same technique as assistant-platform-config-ce-gate.check.js: the gate code is
// EXTRACTED VERBATIM from the shipped settings.js text and run against a stub DOM,
// so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/settings-ce-gate-s20.check.js
//
// Asserts: CE -> no fetch fires for any of the three paths, the tab/row stay hidden;
// paid -> the fetches DO fire (the gate is edition-scoped, nothing weakened);
// old-file state (no gate needle) -> throw -> FAIL.
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

const WL_HDR = 'async function _initWizardLearningSubTab()';
const MS_HDR = 'async function _fetchManagedAiSetting()';
const WKP_HDR = 'async function _wkpLoadAutoUpdate()';
const wlSrc = fnSrc(WL_HDR);
const msSrc = fnSrc(MS_HDR);
const wkpSrc = fnSrc(WKP_HDR);

const GATE = 'await isPaidEdition()';
if (wlSrc.indexOf(GATE) < 0) throw new Error('no CE edition gate in _initWizardLearningSubTab (old-file state)');
if (msSrc.indexOf(GATE) < 0) throw new Error('no CE edition gate in _fetchManagedAiSetting (old-file state)');
if (wkpSrc.indexOf(GATE) < 0) throw new Error('no CE edition gate in _wkpLoadAutoUpdate (old-file state)');
if (!(wlSrc.indexOf(GATE) < wlSrc.indexOf("'ai-tool-tab-wizard-learning'"))) throw new Error('WL gate must precede the tab reveal');
if (!(msSrc.indexOf(GATE) < msSrc.indexOf("_masterFetch('/platform/managed-ai/setting')"))) throw new Error('managed-ai gate must precede the fetch');
if (!(wkpSrc.indexOf(GATE) < wkpSrc.indexOf("apiCall('/platform/settings/wizard_knowledge_auto_update_enabled')"))) throw new Error('KP gate must precede the fetch');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// Stub DOM: elements track hidden state and accept listeners.
function makeEnv(paid) {
    const fetched = [];
    const els = {};
    function el(id) {
        if (!els[id]) els[id] = { hidden: false, addEventListener: () => {}, classList: { add: (c) => { if (c === 'hidden') els[id].hidden = true; }, remove: (c) => { if (c === 'hidden') els[id].hidden = false; } }, dataset: {} };
        return els[id];
    }
    const env = {
        document: { getElementById: (id) => el(id) },
        window: { APP_CONFIG: { API_URL: 'http://api.test' } },
        isPaidEdition: () => paid,
        _authToken: () => 'tok',
        currentUser: { is_platform_admin: true },
        _wizardLearningWired: false,
        _wizardKnowledgePackWired: false,
        _managedAiSetting: { tier: null },
        fetch: (u) => { fetched.push(u); return { ok: false, status: 404 }; },
        _masterFetch: (p) => { fetched.push(p); return { tier: 't1' }; },
        apiCall: (p) => { fetched.push(p); return { value: true }; },
        _wlReloadAll: () => Promise.resolve(), _wlSave: () => {}, _wlRevert: () => {},
        _wlRefreshRegistry: () => {}, _wlPurgeSynthetic: () => {}, _wlGenerateSynthetic: () => {},
        _wlSaveAllowSynthetic: () => {}, _wlMarkDirty: () => {}, _wlOnPresetChange: () => {},
        _wkpCheckNow: () => {}, _wkpUploadPackFile: () => {}, _wkpOnAutoUpdateChange: () => {},
        _wkpSetStatus: () => {}, _handleTrainingToggle: () => {},
    };
    return { env, fetched, el };
}

async function run(paid) {
    const { env, fetched, el } = makeEnv(paid);
    const names = Object.keys(env);
    const body = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
        + ` ${wlSrc} ${msSrc} ${wkpSrc}`
        + ` return [_initWizardLearningSubTab(), _fetchManagedAiSetting(), _wkpLoadAutoUpdate()];`;
    const runner = new Function('__env', body);
    await Promise.all(runner(env));
    return { fetched, hidden: (id) => el(id).hidden };
}

(async () => {
    const ce = await run(false);
    check('CE: zero fetches fire (no 404-baited reads)', ce.fetched.length === 0, ce.fetched.join(','));
    check('CE: wizard-learning tab hidden', ce.hidden('ai-tool-tab-wizard-learning') === true);
    check('CE: KP auto-update row hidden', ce.hidden('wizard-knowledge-pack-auto-update-row') === true);

    const pro = await run(true);
    check('paid: managed-ai setting fetch fires', pro.fetched.some((u) => u === '/platform/managed-ai/setting'), pro.fetched.join(','));
    check('paid: KP auto-update fetch fires', pro.fetched.some((u) => u === '/platform/settings/wizard_knowledge_auto_update_enabled'), pro.fetched.join(','));
    check('paid: wizard-learning tab revealed', pro.hidden('ai-tool-tab-wizard-learning') === false);

    console.log(failures ? `FAILURES ${failures}` : 'ALL OK');
    process.exitCode = failures ? 1 : 0;
})();
