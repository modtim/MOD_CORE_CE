// FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK check — the platform-admin assistant
// defaults block is now SHOWN IN CE TOO (owner 11-10 10:55 PM CT, explicit
// paid-to-CE approval by Tim). The rc20-B2 CE EDITION gate in
// `_initPlatformAssistantConfig` is REMOVED; the platform-admin ROLE check
// stays. The route already ships in CE unchanged (open-core/manifest.yaml
// api_allow :797 + deny_exceptions :2515 routers/assistant.py, :2521
// services/assistant_platform_config_service.py, :2522 services/assistant_router_service.py
// -> CORE/API/main.py:2527 mounts ("assistant","platform_router","")), so no
// manifest row was needed and none is proposed by this job.
//
// Same technique as admin-nodes-packages-ce-gate.check.js /
// settings-scheduler-subtab-init.check.js: the gate code is EXTRACTED VERBATIM
// from the shipped settings.js text and run against a stub DOM, so the
// assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/assistant-platform-config-ce-gate.check.js
//
// Asserts (FLIPPED legs): CE + ok GET -> the block UNHIDES (shown in CE) and the
// init path DOES fetch; CE + non-ok GET -> hidden, error line only, no throw;
// paid + ok GET -> the block unhides and the server copy paints the checkbox;
// paid + non-ok GET -> the block stays hidden.
//
// Same technique as admin-nodes-packages-ce-gate.check.js /
// settings-scheduler-subtab-init.check.js: the gate code is EXTRACTED VERBATIM
// from the shipped settings.js text and run against a stub DOM, so the
// assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/assistant-platform-config-ce-gate.check.js
//
// Asserts: CE -> the block stays hidden and is never unhidden (no dead control,
// no 404-baited panel); paid + ok GET -> the block unhides and the server copy
// paints the checkbox; paid + non-ok GET -> the block stays hidden.
'use strict';
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

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

const INIT_HDR = 'async function _initPlatformAssistantConfig()';
const REFRESH_HDR = 'async function _refreshPlatformAssistantConfig()';
const WRITE_HDR = 'function _platformAssistantWriteConfigForm(cfg)';
const initSrc = fnSrc(INIT_HDR);
const refreshSrc = fnSrc(REFRESH_HDR);
const writeSrc = fnSrc(WRITE_HDR);

// FLIPPED PIN (FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK): the CE EDITION gate must be
// GONE from _initPlatformAssistantConfig (owner 11-10 10:55 PM CT, explicit
// paid-to-CE approval), while the platform-admin ROLE check must stay.
const EDITION_NEEDLE = 'isPaidEdition';
if (initSrc.indexOf(EDITION_NEEDLE) >= 0) throw new Error('CE edition gate still present in _initPlatformAssistantConfig (change not applied)');
const ROLE_NEEDLE = 'if (!isPlatformAdmin) return;';
if (initSrc.indexOf(ROLE_NEEDLE) < 0) throw new Error('platform-admin role check must stay in _initPlatformAssistantConfig');
const hideNeedle = "document.getElementById('platform-assistant-config')?.classList.remove('hidden')";
if (refreshSrc.indexOf(hideNeedle) < 0) throw new Error('refresh does not unhide on ok GET');
if (!(refreshSrc.indexOf(hideNeedle) > refreshSrc.indexOf('if (!resp.ok)'))) throw new Error('unhide must follow the ok check');

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

function makeDom() {
    const seed = {
        'platform-assistant-config': ['hidden'],
        'platform-assistant-config-form': [],
        'platform-assistant-enabled': [],
        'platform-assistant-model-id': [],
        'platform-assistant-rate-max': [],
        'platform-assistant-rate-window': [],
        'platform-assistant-rate-effective': [],
        'platform-assistant-config-status': [],
    };
    const els = {};
    const doc = {
        getElementById: (id) => {
            if (!seed[id]) return null;
            const e = els[id] || (els[id] = { classes: new Set(seed[id]), checked: false, value: '', addEventListener: () => {} });
            e.classList = {
                add: (c) => e.classes.add(c),
                remove: (c) => e.classes.delete(c),
                toggle: (c) => e.classes.has(c) ? e.classes.delete(c) : e.classes.add(c),
            };
            return e;
        },
    };
    return { doc, els };
}
const visible = (els, id) => { const e = els[id]; return !!e && !e.classes.has('hidden'); };

async function run(paid, respOk, cfg) {
    const dom = makeDom();
    // FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK: pre-create the status element (the
    // real _platformAssistantSetStatus does getElementById first; the stub below
    // only marks an element that exists) so the CE error-line leg is observable.
    dom.doc.getElementById('platform-assistant-config-status');
    let fetched = 0;
    const resp = { ok: respOk, status: respOk ? 200 : 404, json: () => cfg, text: () => 'not found' };
    const fn = new Function(
        'document', 'currentUser', 'isPaidEdition', 'fetch', '_agentAuthHeaders', 'apiUrl', 'window',
        '_platformAssistantConfigWired', '_platformAssistantSetStatus',
        `${initSrc}\n${refreshSrc}\n${writeSrc}\nreturn _initPlatformAssistantConfig();`);
    await fn(
        dom.doc,
        { is_platform_admin: true },
        () => ({ then: (cb) => cb(paid) }),
        (url, opts) => { fetched++; return resp; },
        () => ({ Authorization: 'Bearer x' }),
        'http://api.localhost',
        { APP_CONFIG: { API_URL: 'http://api.localhost' } },
        false,
        (msg, isError) => { const el = dom.els['platform-assistant-config-status']; if (el) el.classes.add('status-set'); });
    return { fetched, dom };
}

(async function () {
    // --- FLIPPED CE LEGS (FIX-CE-SHOW-ASSISTANT-DEFAULTS-BLOCK) -------------
    // CE now SHIPS the route and SHOWS the block: a platform admin on CE gets the
    // same fetch + unhide as paid. The old legs pinned `ce.fetched === 0` and a
    // hidden block; they are inverted here (the block is shown, the fetch fires).
    const ce = await run(false, true, { enabled: true, model_id: 'm', chat_rate_limit_max: 17, chat_rate_limit_window_s: 45 });
    check('CE: block unhides after an ok GET (shown in CE, owner 11-10 10:55 PM CT)', visible(ce.dom.els, 'platform-assistant-config'));
    check('CE: the init path DOES fetch /platform/assistant/config', ce.fetched === 1);
    check('CE: server copy paints the kill switch ON', ce.dom.els['platform-assistant-enabled'].checked === true);

    const paidOk = await run(true, true, { enabled: true, model_id: 'm', chat_rate_limit_max: 17, chat_rate_limit_window_s: 45 });
    check('paid: block unhides after an ok GET', visible(paidOk.dom.els, 'platform-assistant-config'));
    check('paid: server copy paints the kill switch ON', paidOk.dom.els['platform-assistant-enabled'].checked === true);

    // Non-ok read in EITHER edition: hidden + the existing error line, no throw
    // (the block is never a dead control).
    const ce404 = await run(false, false, {});
    check('CE + non-ok GET: block stays hidden (no dead control)', !visible(ce404.dom.els, 'platform-assistant-config'));
    check('CE + non-ok GET: the existing error line is set', ce404.dom.els['platform-assistant-config-status'].classes.has('status-set'));

    const paid404 = await run(true, false, {});
    check('paid + non-ok GET: block stays hidden (no dead control)', !visible(paid404.dom.els, 'platform-assistant-config'));

    // --- helper reuse (no invented gate) ------------------------------------
    check('settings.js imports the existing isPaidEdition from ./api.js',
        /import\s*\{[^}]*\bisPaidEdition\b[^}]*\}\s*from\s*'\.\/api\.js';/.test(settingsSrc));
    check('api.js exports isPaidEdition (the SAME helper settings.js:219/:3064/:5489 use)',
        /export async function isPaidEdition\(\)/.test(apiSrc));
    check('no new edition helper invented in settings.js',
        !/function\s+_?ceEdition|const\s+_ceEdition\b/.test(settingsSrc));
    // FAILOPEN-C (working tree): api.js isPaidEdition is fail-CLOSED (unknown
    // edition = CE). The old check pinned `/return true;/`; pin the NEW shape:
    // positive saas/pro test, unknown/error -> false, no unconditional true.
    const paidHdrAt = apiSrc.indexOf('export async function isPaidEdition');
    if (paidHdrAt < 0) throw new Error('isPaidEdition not exported in api.js');
    const paidHelper = `export async function isPaidEdition ${blockAt(apiSrc, apiSrc.indexOf('{', paidHdrAt + 'export async function isPaidEdition'.length))}`;
    check('api.js isPaidEdition is fail-closed (positive saas/pro test)',
        /ent\.edition === 'saas' \|\| ent\.edition === 'pro'/.test(paidHelper));
    check('api.js isPaidEdition has no unconditional return true (fail-open shape gone)',
        !/return true;/.test(paidHelper));

    // --- no new unescaped DOM writes in the init path -----------------------
    // (the old leg sliced the edition-gate block; that gate is gone, so pin the
    // whole init body instead: it still only toggles class state, never HTML.)
    check('init path touches only classList state (no innerHTML, no fetch)',
        !/innerHTML|fetch\(/.test(initSrc));
    check('status line still writes textContent only (no innerHTML anywhere in the block)',
        !/\.innerHTML/.test(`${initSrc}${refreshSrc}${writeSrc}`));

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();
