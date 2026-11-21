// SECHUB-CETABS: Security hub CE-carved tabs must be HIDDEN, not empty panels.
//
// elevation-requests (manifest:1332), abuse-queue (manifest:1290) and
// security-incidents (manifest:2002) are carved out of CE: their page JS is absent
// from the CE bundle and their routers are denied. Before the fix security-hub.js
// _tabAllowed had no CE entry for them (unlike pending-approvals at :102), so the
// tab was VISIBLE and opened an EMPTY panel — a silent half-render product red.
//
// This check loads the REAL _TAB_GATE + _tabAllowed source out of js/security-hub.js
// (no re-typed copy, so it cannot pass against a stale/edited duplicate) and evaluates
// it for CE and paid. Run:
//   node CORE/Dashboard/tests/sechub-ce-tabs-hidden.check.js
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const JS = fs.readFileSync(path.join(ROOT, 'js/security-hub.js'), 'utf8');

const CARVED = ['elevation-requests', 'abuse-queue', 'security-incidents'];
const ALREADY = 'pending-approvals';
// SAERR-FIX: the tenant Security Alerts tab calls GET /v1/tenant/security-alerts,
// which the manifest denies on CE (open-core/manifest.yaml:1393), so the panel
// showed #sa-error "Not Found". It must be HIDDEN on CE like the other carved tabs.
const SAERR = 'security-alerts';

// --- 1) The fix exists: a CE guard naming all three carved tabs -------------
const guardLine = JS.match(/if\s*\(\s*_ceEdition\s*&&\s*\([^)]*elevation-requests[^)]*\)[^)]*\)\s*return false\s*;/);
assert.ok(guardLine, 'security-hub.js must have an if (_ceEdition && (...)) return false guard for the carved tabs');
CARVED.forEach((k) => assert.ok(guardLine[0].includes(`'${k}'`),
    `the CE guard must name '${k}'`));
assert.ok(/if\s*\(\s*_ceEdition\s*&&\s*\([^)]*pending-approvals/.test(JS),
    "the pre-existing CE guard for 'pending-approvals' must still be present");

// SAERR-FIX: a CE guard hiding the tenant security-alerts tab (its GET is
// manifest-denied on CE; before the fix the tab rendered #sa-error "Not Found").
assert.ok(/if\s*\(\s*_ceEdition\s*&&\s*key === 'security-alerts'\s*\)\s*return false\s*;/.test(JS),
    "security-hub.js must have an if (_ceEdition && key === 'security-alerts') return false guard (SAERR-FIX)");

// --- 2) No new CE detection: reuse the single _ceEdition predicate ---------
assert.strictEqual((JS.match(/_ceEdition\s*=/g) || []).length, 2,
    'exactly one _ceEdition declaration + one assignment (no new CE detection added)');
assert.ok(/ent\.edition === 'ce'/.test(JS),
    '_ceEdition must stay the entitlements.edition === "ce" predicate');

// --- 3) Load the REAL gate code and evaluate it ----------------------------
const gateStart = JS.indexOf('const _TAB_GATE');
const gateEnd = JS.indexOf('function _visibleTabs');
assert.ok(gateStart >= 0 && gateEnd > gateStart, 'must locate _TAB_GATE .. _visibleTabs region');
const GATE_SRC = JS.slice(gateStart, gateEnd);

const keysStart = JS.indexOf('const TAB_KEYS');
const keysEnd = JS.indexOf('];', keysStart);
assert.ok(keysStart >= 0 && keysEnd > keysStart, 'must locate TAB_KEYS');
const keysPart = JS.slice(keysStart, keysEnd + 2);
const TAB_KEYS = vm.runInNewContext(keysPart + '\nTAB_KEYS;', {}, { filename: 'tab-keys', timeout: 5000 });
TAB_KEYS.forEach((k) => assert.ok(/^[a-z0-9-]+$/.test(k), `TAB_KEYS entry malformed: ${k}`));
CARVED.concat([ALREADY, SAERR]).forEach((k) => assert.ok(TAB_KEYS.includes(k),
    `${k} must be a Security hub tab`));

const PRELUDE = (ce) => `let _isSuperAdmin = false;\nlet _isPlatformAdmin = true;\nlet _ceEdition = ${ce};\n`;
const EVAL = 'JSON.stringify(TAB_KEYS.filter((k) => _tabAllowed(k)));';
const KEYS_DECL = 'const TAB_KEYS = [' +
    TAB_KEYS.map((k) => `'${k}'`).join(', ') + '];\n';

const allowedOn = (ce) => JSON.parse(vm.runInNewContext(
    PRELUDE(ce) + KEYS_DECL + GATE_SRC + EVAL,
    {}, { filename: 'sechub-ce-tabs-hidden', timeout: 5000 }
));

// --- 4) CE: the three carved tabs AND pending-approvals are NOT allowed ----
const ceAllowed = allowedOn('true');
CARVED.forEach((k) => assert.ok(!ceAllowed.includes(k),
    `CE must hide '${k}' (was: visible with an empty panel)`));
assert.ok(!ceAllowed.includes(ALREADY), "CE must still hide 'pending-approvals'");
assert.ok(!ceAllowed.includes(SAERR),
    "CE must hide 'security-alerts' (was: visible, calling a denied route and showing #sa-error 'Not Found')");

// --- 5) Paid: the carved tabs plus security-alerts are allowed (no over-carve) --
const paidAllowed = allowedOn('false');
CARVED.concat([ALREADY, SAERR]).forEach((k) => assert.ok(paidAllowed.includes(k),
    `paid must allow '${k}' for a platform admin`));

// --- 6) CE allowed set is exactly paid minus the pre-existing CE entries plus
//        the four carved tabs (three SECHUB + SAERR) and the three ENTSTUB tabs ---
// CE-ENTSTUB-UI: alert-rules, ip-blocks and platform-access-review are served by routers the
// CE carve does not ship, so they join the CE-hidden set (paid keeps them).
const ENTSTUB_CARVED = ['alert-rules', 'ip-blocks', 'platform-access-review'];
const PREEXISTING_CE_HIDDEN = ['tenant-idp', 'outbound-connections', 'security-lockdown']
    .concat(ENTSTUB_CARVED);
const hidden = paidAllowed.filter((k) => !ceAllowed.includes(k));
PREEXISTING_CE_HIDDEN.concat(CARVED, [ALREADY, SAERR]).forEach((k) => assert.ok(hidden.includes(k),
    `'${k}' must be hidden on CE`));
assert.deepStrictEqual(hidden.slice().sort(),
    PREEXISTING_CE_HIDDEN.concat(CARVED, [ALREADY, SAERR]).sort(),
    'CE must hide exactly the pre-existing CE entries plus the four carved tabs');

console.log(`sechub-ce-tabs-hidden: OK (${TAB_KEYS.length} tabs; CE hides ${CARVED.length + 3 + ENTSTUB_CARVED.length})`);
