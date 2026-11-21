// FIX-MFA-HIDE (owner Q-MFA = A, 11-10: "hide the inert admin MFA setting on
// CE now; paid unchanged") — the Admin > Analytics > Settings > Platform
// settings page (js/platform-settings.js) renders the DB-backed registry, and
// the CE tree still REGISTERS the three admin-MFA switches:
//   auth.mfa_required_for_admins   CORE/API/services/platform_settings.py:2853
//   auth.mfa_required_for_all      CORE/API/services/platform_settings.py:2865
//   auth.mfa_enforcement_enabled   CORE/API/services/platform_settings.py:2875
// Every CE-side CONSUMER is stripped or guarded, so on CE the rows are inert:
//   services/compliance_gate_task_start.py:95-100 (enforced-MFA login gate) —
//     denied (manifest.yaml:1329 region); routers/auth.py:93 reaches it through
//     optional_paid and the CE fallback is {"forced": False} (ALLOW, a no-op).
//   services/mfa_enforcement.py:207/:214 (FX.53 sweep + countdown) — manifest:1329
//     denies it; platform_settings.py:1028 `_on_mfa_enforcement_after_commit`
//     optional_paid-skips ("SKIPPED (paid module absent in CE)").
//   services/conformance/checks.py:366 — manifest:2167 denies services/conformance/**.
//   services/soc2_evidence.py:270/:275 — manifest:2220 denies it.
// The one CE-visible reader is services/mfa_service.py:161-162 (GET /me/mfa
// `required`, the module ships in CE, manifest.yaml:130) — which is exactly why
// the row must be HIDDEN, not merely greyed: a CE Save there changes the Keycloak
// browser flow OUTSIDE MOD (keycloak-init.sh:1421-1422 mirrors the two scope keys
// into realm-side defaults) while nothing MOD-side acts on it.
//
// Behavioural check (same technique as assistant-platform-config-ce-gate.check.js
// / sechub-ce-tabs-hidden.check.js): the SHIPPED module text is loaded into a vm
// with a stub DOM — no re-typed copy of the gate — then _psrRender() is run for
// CE and for paid, and the rendered markup is asserted.
//
//   node --test CORE/Dashboard/tests/ce-inert-admin-mfa-hidden.check.js
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const PS = fs.readFileSync(path.join(ROOT, 'js', 'platform-settings.js'), 'utf8');
const API = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');

const INERT = ['auth.mfa_required_for_admins', 'auth.mfa_required_for_all', 'auth.mfa_enforcement_enabled',
    'api.rate_limit.plan_limits', 'llm.monthly_token_quota', 'billing.payment_grace_days_monthly',
    'billing.payment_grace_days_annual', 'billing.overage_restrict_enabled'];
const OTHER = 'api.rate_limit.per_worker';

// --- 1) the fix exists, and reuses the existing edition helper --------------
assert.ok(/const _INERT_ON_CE_SETTING_KEYS = new Set\(\[/.test(PS),
    'platform-settings.js must name the inert-on-CE setting set (old-file state has none)');
INERT.forEach((k) => assert.ok(PS.includes(`'${k}'`), `the set must name '${k}'`));
assert.ok(/function _psrVisibleSettings\(\)/.test(PS), 'a CE visibility filter must exist');
assert.ok(/for \(const s of _psrVisibleSettings\(\)\)/.test(PS), '_psrRender must render the FILTERED list');
assert.ok(/import \{[^}]*\bisPaidEdition\b[^}]*\} from '\.\/api\.js';/.test(PS),
    'platform-settings.js must import the existing isPaidEdition from ./api.js');
assert.ok(/export async function isPaidEdition\(\)/.test(API), 'api.js exports isPaidEdition');
assert.ok(/_psrPaidEdition = await isPaidEdition\(\);/.test(PS),
    'the cached edition verdict must be refreshed from isPaidEdition');
assert.ok(/_psrPaidEdition = false;/.test(PS), 'unknown edition must fail closed (CE)');
assert.ok(!/function _?ceEdition|const _ceEdition\b/.test(PS), 'no new edition helper invented');

// --- 2) behavioural: run the SHIPPED module against a stub DOM --------------
const moduleSrc = PS
    .replace(/import \{[^}]*\} from '\.\/api\.js';/, '')   // stubbed below
    .replace(/\bexport (async function)/g, '$1');
const harness = `
function __setSettings(arr) { _psrSettings = arr; }
function __setEdition(v) { _psrPaidEdition = v; }
`;

function makeDom() {
    const container = { innerHTML: '', classList: { add: () => {}, remove: () => {} } };
    const doc = { getElementById: (id) => (id === 'psr-settings-container' ? container : null) };
    return { doc, container };
}

function render(settings, edition) {
    const dom = makeDom();
    const sandbox = {
        document: dom.doc,
        apiCall: () => ({}),
        me: { get: () => ({}) },
        showConfirm: () => true,
        CSS: { escape: (s) => s },
        setTimeout: (f, ms) => 0,
        clearTimeout: () => {},
        console: { log: () => {}, error: () => {}, warn: () => {} },
        JSON: JSON,
    };
    vm.runInNewContext(moduleSrc + harness, sandbox, { filename: 'platform-settings.js' });
    sandbox.__setSettings(settings);
    sandbox.__setEdition(edition);
    sandbox._psrRender();
    return dom.container.innerHTML;
}

const rows = INERT.map((key) => ({ key, type: 'bool', value: true, group: 'auth', source: 'default', default: true, restart_required: false, description: '', env_fallback: null, tenant_overrides: [] }))
    .concat([{ key: OTHER, type: 'int', value: 5, group: 'api', source: 'db', default: 5, restart_required: false, description: '', env_fallback: null, tenant_overrides: [] }]);

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

const ceHtml = render(rows, false);
check('CE: the whole page still renders (other settings keep their rows)',
    ceHtml.includes(OTHER), ceHtml);
INERT.forEach((k) => {
    check(`CE: '${k}' row is hidden (no toggle, no Save, no Revert)`,
        !ceHtml.includes(k), ceHtml);
    check(`CE: no data-psr-row for '${k}'`, !ceHtml.includes(`data-psr-row="${k}"`), ceHtml);
});

const paidHtml = render(rows, true);
INERT.forEach((k) => {
    check(`paid: '${k}' row renders unchanged`, paidHtml.includes(`data-psr-row="${k}"`), paidHtml);
    check(`paid: '${k}' keeps its Save affordance`,
        paidHtml.includes(`data-psr-key="${k}"`), paidHtml);
});
check('paid: the page still renders the other settings', paidHtml.includes(OTHER), paidHtml);

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
