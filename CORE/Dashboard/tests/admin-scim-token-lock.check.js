// Node-runnable check for the GT.D11.s2 SCIM tier hint in
// js/admin-scim.js: the create-token control (#scim-add-token-btn) is
// pre-disabled + badged when the `scim` capability (utils.entitlements
// Cap.SCIM, checked server-side by routers/admin_scim_tokens.py
// require_scim_enabled_plan → 402) is ABSENT from the /platform/features
// `entitlements` block.
//
// The real js/entitlements.js code is exercised (its import line is stripped
// and getPlatformFeatures stubbed), plus the real wiring snippet lifted out of
// js/admin-scim.js, in a fresh vm sandbox per scenario (the module caches the
// block, so each scenario needs a fresh context).
//
//   node CORE/Dashboard/tests/admin-scim-token-lock.check.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const entSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'entitlements.js'), 'utf8');
// CE carve (FIX-CE-DASH-SWEEP-C): js/admin-scim.js is denied on CE, and EVERY
// assertion in this file reads that module (the static asserts + the lifted
// wiring snippet), so on a tree without it the whole check is a SKIP.
if (!fs.existsSync(path.join(__dirname, '..', 'js', 'admin-scim.js'))) {
    console.log('SKIP admin-scim-token-lock.check.js: js/admin-scim.js absent (CE carve)');
    process.exit(0);
}
const pageSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'admin-scim.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- static assertions on the page source ----------------------------------
check('page imports entitlements.js',
    /import MODEntitlements from '\.\/entitlements\.js';/.test(pageSrc));
check('page locks with the scim capability key',
    /lockElement\(\s*addBtn,\s*'scim'/.test(pageSrc));
check('server 402 path kept (showPlanGate on POST /scim/tokens)',
    /resp\.status === 402/.test(pageSrc) && /_showPlanGate\(\)/.test(pageSrc));
check('fetch path has no /v1 prefix', !/fetch\(`\$\{apiUrl\}\/v1\//.test(pageSrc));

// Lift the wiring snippet out of initializeAdminScimPage.
const START = "const addBtn = document.getElementById('scim-add-token-btn');";
const END = "addBtn?.addEventListener('click', () => formCard.classList.remove('hidden'));";
const s = pageSrc.indexOf(START);
const e = pageSrc.indexOf(END);
if (s < 0 || e < s) {
    console.error('FAIL could not locate the addBtn wiring snippet in admin-scim.js');
    process.exit(1);
}
const snippet = pageSrc.slice(s, e + END.length);

const entBody = entSrc
    .replace(/^import \{ getPlatformFeatures \} from '\.\/api\.js';\s*$/m, '')
    .replace(/^export default .*$/m, '');

// One fresh sandbox per scenario: entitlements.js caches its block.
async function scenario(payload) {
    const badges = [];
    const sandbox = { window: {}, getPlatformFeatures: () => payload, console };
    function makeEl(id) {
        return {
            id: id || '',
            dataset: {},
            classList: { add() {}, remove() {} },
            disabled: false,
            title: '',
            children: [],
            appendChild(node) { this.children.push(node); },
            addEventListener() {},
            insertAdjacentElement(_pos, node) { badges.push(node); },
        };
    }
    sandbox.document = {
        getElementById: (id) => (sandbox._els ||= {})[id] || (sandbox._els[id] = makeEl(id)),
        createElement: (tag) => makeEl(null),
    };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    vm.runInContext(entBody, sandbox, { filename: 'entitlements.js' });
    // In the browser `window` IS the global object; in a vm sandbox it is not,
    // so entitlements.js' `window.MODEntitlements = ...` needs re-exporting.
    sandbox.MODEntitlements = sandbox.window.MODEntitlements;
    const out = await vm.runInContext(
        `(async () => { ${snippet} return { btn: addBtn }; })()`,
        sandbox,
        { filename: 'admin-scim-wiring.js' },
    );
    // `badges` lives in the Node scope; the fake DOM closure pushed into it.
    return { btn: out.btn, badges: badges.slice() };
}

(async () => {
    // 1) Entitled: capabilities include 'scim' → control untouched, no badge.
    let r = await scenario({
        entitlements: { edition: 'pro', tier_name: 'team', source: 'plan',
                        capabilities: ['scim', 'sso'], limits: {} },
    });
    check('entitled: control not disabled', r.btn.disabled === false);
    check('entitled: not marked locked', r.btn.dataset.modLocked !== '1');
    check('entitled: no badge', r.badges.length === 0);

    // 2) Not entitled: block present, 'scim' absent → locked + badge → #billing.
    r = await scenario({
        entitlements: { edition: 'ce', tier_name: 'core', source: 'baseline',
                        capabilities: ['sso'], limits: {} },
    });
    check('not entitled: control disabled', r.btn.disabled === true);
    check('not entitled: marked locked', r.btn.dataset.modLocked === '1');
    check('not entitled: exactly one badge', r.badges.length === 1);
    const anchor = r.badges[0] && r.badges[0].children[0];
    check('badge links to #billing', anchor && anchor.href === '#billing');
    check('badge text', anchor && anchor.textContent === 'Not part of this edition');

    // 3) Missing entitlements block → fail CLOSED (CATCHPAID: has() returns
    // false when the block is absent, so the control locks + badges; the
    // server 402 stays the authority either way).
    r = await scenario({ edition: 'ce' });
    check('no block: control disabled', r.btn.disabled === true);
    check('no block: exactly one badge', r.badges.length === 1);

    // 4) Features call fails (offline/401) → fail closed too (same contract
    // entitlements-has.check.js pins).
    r = await scenario(null);
    check('null payload: control disabled', r.btn.disabled === true);
    check('null payload: exactly one badge', r.badges.length === 1);

    console.log(failures ? `\n${failures} failed` : '\nall checks passed');
    process.exit(failures ? 1 : 0);
})();
