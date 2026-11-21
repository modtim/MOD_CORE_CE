// CE-NOPAID-4.1c step 0: characterisation pin of TODAY's hub-compliance.js tab gate.
//
// Loads the REAL _CE_* Set literals, _TAB_GATE and _tabAllowed source out of
// js/hub-compliance.js (no re-typed copy) and evaluates it for CE and paid.
// It pins which compliance tabs are hidden on CE (each _CE_ set) and that every
// tab is shown on paid, so the later registry refactor is provably
// behaviour-identical. TEST ONLY: hub-compliance.js is not edited here.
// Run:  node CORE/Dashboard/tests/test_hub_tabs_hidden.check.js
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const JS = fs.readFileSync(path.join(__dirname, '..', 'js', 'hub-compliance.js'), 'utf8');

// Today's member list of each CE-hidden set (order as in the source literal).
const EXPECTED_SETS = {
    _CE_CARVED: ['auditor-access', 'release-approvals'],
    _CE_R397_TABS: ['compliance'],
    _CE_FX163_TABS: ['data-export'],
    _CE_FX159_TABS: ['compliance-requirements', 'compliance-obligations', 'compliance-hardening'],
    _CE_FORT46_TABS: ['compliance-attestations'],
    _CE_WSIP67D_TABS: ['output-scanners'],
    _CE_FORT54UI_TABS: ['compliance-policies'],
    _CE_PAID_COMPLIANCE_TABS: ['licence-review', 'key-inventory', 'lockbox-approval', 'compliance-operator-proposals'],
    _CE_COV4B_TABS: ['compliance-capacity', 'compliance-adversary', 'compliance-calibration', 'mpc-sessions'],
    _CE_BUGARATAB_TABS: ['access-reviews'],
};
const SET_STMT = /const (_CE_[A-Z0-9_]+) = new Set\(\[[^\]]*\]\);/g;

// 1) Each set literal parses to exactly its pinned member list.
const parsed = {};
[...JS.matchAll(SET_STMT)].forEach((m) => {
    parsed[m[1]] = [...m[0].matchAll(/'([a-z0-9-]+)'/g)].map((x) => x[1]);
});
assert.deepStrictEqual(Object.keys(parsed).sort(), Object.keys(EXPECTED_SETS).sort(),
    'hub-compliance.js must declare exactly the 10 pinned _CE_ sets');
Object.keys(EXPECTED_SETS).forEach((name) => assert.deepStrictEqual(parsed[name], EXPECTED_SETS[name],
    `${name} members changed (expected ${EXPECTED_SETS[name].join(', ')})`));

// 2) Load the REAL sets, TAB_KEYS, _TAB_GATE and _tabAllowed; strip the top-level
//    lets (the prelude supplies them) and the set statements (re-added as SETS_SRC).
const SETS_SRC = [...JS.matchAll(SET_STMT)].map((m) => m[0]).join('\n') + '\n';
const keysStart = JS.indexOf('const TAB_KEYS');
const keysEnd = JS.indexOf('];', keysStart);
assert.ok(keysStart >= 0 && keysEnd > keysStart, 'must locate TAB_KEYS');
const KEYS_SRC = JS.slice(keysStart, keysEnd + 2);
const gateStart = JS.indexOf('const _TAB_GATE');
const visStart = JS.indexOf('function _visibleTabs');
assert.ok(gateStart >= 0 && visStart > gateStart, 'must locate _TAB_GATE .. _visibleTabs region');
const GATE_SRC = JS.slice(gateStart, visStart)
    .replace(SET_STMT, '')
    .replace(/^let _(isSuperAdmin|isPlatformAdmin|ceEdition) = .*$/gm, '');

// JSON round-trip: arrays made inside the vm context have a foreign prototype, which makes
// assert.deepStrictEqual fail on identical contents ("same structure but not reference-equal").
const TAB_KEYS = JSON.parse(vm.runInNewContext(KEYS_SRC + '\nJSON.stringify(TAB_KEYS);', {}, { filename: 'tab-keys', timeout: 5000 }));
const allowedOn = (ce, platformAdmin) => JSON.parse(vm.runInNewContext(
    'let _isSuperAdmin = false;\n' +
    `let _isPlatformAdmin = ${platformAdmin};\n` +
    `let _ceEdition = ${ce};\n` +
    KEYS_SRC + '\n' + SETS_SRC + GATE_SRC +
    'JSON.stringify(TAB_KEYS.filter((k) => _tabAllowed(k)));',
    {}, { filename: 'hub-compliance-gate', timeout: 5000 }));
const ceAllowed = allowedOn('true', true);
const paidAllowed = allowedOn('false', true);

// 3) Per set: on CE every member is hidden; on paid every member is shown.
const ALL_HIDDEN = [].concat(...Object.values(EXPECTED_SETS));
Object.keys(EXPECTED_SETS).forEach((name) => {
    const members = EXPECTED_SETS[name];
    const shownOnCe = members.filter((k) => ceAllowed.includes(k));
    assert.deepStrictEqual(shownOnCe, [],
        `${name}: CE must hide ${members.join(', ')} (shown on CE: ${shownOnCe.join(', ')})`);
    const hiddenOnPaid = members.filter((k) => !paidAllowed.includes(k));
    assert.deepStrictEqual(hiddenOnPaid, [],
        `${name}: paid must show ${members.join(', ')} (hidden on paid: ${hiddenOnPaid.join(', ')})`);
});

// 4) Whole-hub characterisation: CE hides exactly the union of the sets, paid hides nothing.
assert.deepStrictEqual(TAB_KEYS.filter((k) => !ceAllowed.includes(k)).sort(), ALL_HIDDEN.slice().sort(),
    'CE must hide exactly the union of the 10 _CE_ sets');
assert.deepStrictEqual(paidAllowed.slice().sort(), TAB_KEYS.slice().sort(),
    'paid (platform admin) must show every hub tab');

console.log(`test_hub_tabs_hidden: OK (${TAB_KEYS.length} tabs; CE hides ${ALL_HIDDEN.length}, paid hides 0)`);
