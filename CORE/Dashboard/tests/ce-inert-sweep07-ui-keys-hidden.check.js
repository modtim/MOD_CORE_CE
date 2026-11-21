'use strict';
// FIX-CE-INERT-SETTING-KEYS (SWEEP-07 leg, 11-12) — SWEEP-07-SETTINGS-REGISTRY
// section A class UI found shipped registry rows whose EVERY acting module is
// manifest-denied in CE: the toggle does nothing, but the row is still VISIBLE
// and editable in the CE settings UI (a dead paid row). The fix extends the
// existing CE-only hide-set `_INERT_ON_CE_SETTING_KEYS` in js/platform-settings.js
// (CE-conditional by design: `_psrVisibleSettings()` returns the RAW list on
// paid, so paid rows and paid behaviour are untouched - this file is shared
// code and ships to paid too, with paid behaviour unchanged).
//
// 20 keys added. Six SWEEP-07 UI rows are deliberately NOT hidden and are
// asserted PRESENT here so a later lane cannot hide them by accident:
//   compliance.declared_environment  - main.py:1470 resolve_source() ships in CE
//                                        (the CE features endpoint renders the
//                                        declared values; only the map is denied)
//   compliance.profiles              - node_egress_approval.py:500 get_setting()
//                                        ships in CE (Q39=A per-tenant gov caps)
//   support.remote_session_enabled / support.remote_session_max_hours /
//   support_tickets.response_targets / oncall.sev1_ticket_paging_enabled
//                                      - support_session.py + support_tickets.py
//                                        are api_allow (CE-shipped consumers);
//                                        PROOF-CE-DENY already ordered NEEDS-GUARD
//
// Behavioural check (same technique as ce-inert-admin-mfa-hidden.check.js): the
// SHIPPED module text is loaded into a vm with a stub DOM - no re-typed copy of
// the gate - then _psrRender() runs for CE and for paid over the registry rows,
// and the rendered markup is asserted.
//
//   node --test CORE/Dashboard/tests/ce-inert-sweep07-ui-keys-hidden.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const PS = fs.readFileSync(path.join(ROOT, 'js', 'platform-settings.js'), 'utf8');
const API = fs.readFileSync(path.join(ROOT, 'js', 'api.js'), 'utf8');

const NEW = [
    'billing_basis',
    'billing.record_retention_years',
    'billing.cancellation_export_days',
    'node_trial_rate_limit_per_hour',
    'marketplace.report_autohold_threshold',
    'marketplace.mod_built_only_enforced',
    'compliance.disruptive_grace_days',
    'compliance.grace_deadlines',
    'stacks.share_platform_hf_token',
    'mesh.acl_enforced',
    'mesh.preauth_key_ttl_minutes',
    'mesh.public_control_plane',
    'auth.sso_requirement_enabled',
    'auth.require_sso',
    'auth.require_sso_break_glass_user_ids',
    'license.last_known_state',
    'license.expiry_warn_days',
    'dlp.export_approval_profiles',
    'assistant.gov_local_models_only',
    'backup.retention_days',
];
// The pre-existing hide-set rows must survive the edit (paid/CE unchanged for them).
const OLD = ['auth.mfa_required_for_admins', 'auth.mfa_required_for_all',
    'auth.mfa_enforcement_enabled', 'auth.phishing_resistant_only',
    'api.rate_limit.plan_limits', 'llm.monthly_token_quota',
    'billing.payment_grace_days_monthly', 'billing.payment_grace_days_annual',
    'billing.overage_restrict_enabled'];
// Keys that KEEP their CE row (a shipped CE consumer reads them).
const KEEP = ['compliance.declared_environment', 'compliance.profiles',
    'support.remote_session_enabled', 'support.remote_session_max_hours',
    'support_tickets.response_targets', 'oncall.sev1_ticket_paging_enabled'];
const OTHER = 'api.rate_limit.per_worker';

// --- 1) the fix exists, and reuses the existing set/helper ------------------
assert.ok(/const _INERT_ON_CE_SETTING_KEYS = new Set\(\[/.test(PS),
    'platform-settings.js must name the inert-on-CE setting set (old-file state has none)');
NEW.forEach((k) => assert.ok(PS.includes(`'${k}'`), `the set must name '${k}'`));
OLD.forEach((k) => assert.ok(PS.includes(`'${k}'`), `the set must keep the pre-existing '${k}'`));
const setBody = (PS.match(/_INERT_ON_CE_SETTING_KEYS = new Set\(\[([\s\S]*?)\]\);/) || [null, ''])[1];
assert.ok(setBody, 'the hide-set body must be readable');
KEEP.forEach((k) => assert.ok(!setBody.includes(`'${k}'`),
    `the set must NOT name the CE-shipped-consumer key '${k}'`));
assert.ok(NEW.every((k) => setBody.includes(`'${k}'`)), 'the set body must name all 20 new keys');
assert.ok(/function _psrVisibleSettings\(\)/.test(PS), 'a CE visibility filter must exist');
assert.ok(/if \(_psrPaidEdition\) return _psrSettings;/.test(PS),
    'paid must return the RAW registry list (paid rows untouched)');
assert.ok(/_psrSettings\.filter\(\(s\) => !_INERT_ON_CE_SETTING_KEYS\.has\(s\.key\)\)/.test(PS),
    'CE must filter through the SAME set (no second list invented)');
assert.ok(/import \{[^}]*\bisPaidEdition\b[^}]*\} from '\.\/api\.js';/.test(PS),
    'platform-settings.js must import the existing isPaidEdition from ./api.js');
assert.ok(/export async function isPaidEdition\(\)/.test(API), 'api.js exports isPaidEdition');
assert.ok(/_psrPaidEdition = await isPaidEdition\(\);/.test(PS),
    'the cached edition verdict must be refreshed from isPaidEdition');
assert.ok(/_psrPaidEdition = false;/.test(PS), 'unknown edition must fail closed (CE)');

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

function row(key, type) {
    return { key, type, value: type === 'int' ? 7 : true, group: 'sweep07', source: 'default',
        default: type === 'int' ? 7 : true, restart_required: false, description: '',
        env_fallback: null, tenant_overrides: [] };
}

const rows = NEW.map((k) => row(k, 'bool'))
    .concat(OLD.map((k) => row(k, 'bool')))
    .concat(KEEP.map((k) => row(k, 'bool')))
    .concat([row(OTHER, 'int')]);

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

const ceHtml = render(rows, false);
check('CE: the page still renders the CE-live rows',
    ceHtml.includes(OTHER) && KEEP.every((k) => ceHtml.includes(`data-psr-row="${k}"`)), ceHtml);
NEW.concat(OLD).forEach((k) => {
    check(`CE: '${k}' row is hidden (no toggle, no Save, no Revert)`,
        !ceHtml.includes(k), ceHtml);
    check(`CE: no data-psr-row for '${k}'`, !ceHtml.includes(`data-psr-row="${k}"`), ceHtml);
});

const paidHtml = render(rows, true);
NEW.concat(OLD).forEach((k) => {
    check(`paid: '${k}' row renders unchanged`, paidHtml.includes(`data-psr-row="${k}"`), paidHtml);
    check(`paid: '${k}' keeps its Save affordance`, paidHtml.includes(`data-psr-key="${k}"`), paidHtml);
});
check('paid: the page still renders every row (registry response unchanged)',
    NEW.length + OLD.length + KEEP.length + 1 === rows.length
    && rows.every((r) => paidHtml.includes(`data-psr-row="${r.key}"`)), paidHtml);

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
