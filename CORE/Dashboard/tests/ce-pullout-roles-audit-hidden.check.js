'use strict';
// Owner 11-09 pull-out (custom RBAC, audit export): the Dashboard must not offer
// these paid features on CE, using the existing edition predicate (isPaidEdition,
// fail closed).
//
//   node --test CORE/Dashboard/tests/ce-pullout-roles-audit-hidden.check.js
//
//   P1 users.js: CE hides the custom-roles editor tab and panel (set from the
//      fail-closed `_ceEdition = !(await isPaidEdition())`), and a click on it is a no-op
//   P2 users.js: the paid path still reveals the Roles tab for super admins
//   P3 audit-logs.js: the CSV export drops the integrity_valid column on CE only
//   P4 hub-compliance.js: the Access Reviews tab stays CE-hidden
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const read = (f) => fs.readFileSync(path.join(__dirname, '..', 'js', f), 'utf8');
const users = read('users.js');
const audit = read('audit-logs.js');
const hub = read('hub-compliance.js');

function braceSlice(src, start) {
    let depth = 0;
    for (let i = start; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}
const stripComments = (s) => s.replace(/\/\/[^\n]*/g, '');

test('P1: CE hides the custom-roles editor tab + panel and ignores a click on it', () => {
    const at = users.indexOf('async function checkUserRoleAndConfigureTabs()');
    assert.ok(at >= 0, 'checkUserRoleAndConfigureTabs must exist');
    const body = stripComments(braceSlice(users, users.indexOf('{', at)));
    const ceSet = body.indexOf('_ceEdition = !(await isPaidEdition())');
    assert.ok(ceSet >= 0, '_ceEdition must come from the fail-closed isPaidEdition()');
    assert.match(body, /if \(_ceEdition\) document\.getElementById\('main-tab-roles'\)\?\.style\.setProperty\('display', 'none'\)/);
    assert.match(body, /if \(_ceEdition\) document\.getElementById\('main-panel-roles'\)\?\.classList\.add\('hidden'\)/);
    // the hide runs after _ceEdition is resolved
    assert.ok(body.indexOf("getElementById('main-tab-roles')?.style.setProperty('display', 'none')") > ceSet,
        'the CE hide must run after _ceEdition is set');
    assert.match(stripComments(users), /if \(_ceEdition && _tabKey === 'roles'\) return;/,
        'a click on the hidden Roles tab must be ignored on CE');
});

test('P2: paid still reveals the Roles tab for super admins', () => {
    const at = users.indexOf('async function checkUserRoleAndConfigureTabs()');
    const body = stripComments(braceSlice(users, users.indexOf('{', at)));
    const reveal = body.indexOf("getElementById('main-tab-roles')?.style.removeProperty('display')");
    assert.ok(reveal >= 0, 'the super-admin reveal of the Roles tab must remain');
});

test('P3: CSV export drops integrity_valid on CE only', () => {
    const at = audit.indexOf('function _recordsToCsv(records)');
    assert.ok(at >= 0, '_recordsToCsv must exist');
    const fn = braceSlice(audit, audit.indexOf('{', at));
    const csvCell = braceSlice(audit, audit.indexOf('{', audit.indexOf('function _csvCell')));
    const run = vm.runInNewContext(
        `function _csvCell(v) ${csvCell}\nfunction _recordsToCsv(records) ${fn}\n_recordsToCsv;`);
    const mk = () => [{ source: 'permission_audit_log', id: 1, integrity_valid: true, details: 'd' }];
    const ce = mk(); ce.omitIntegrity = true;
    assert.ok(!run(ce).split('\r\n')[0].includes('integrity_valid'), 'CE header has no integrity_valid');
    assert.ok(run(mk()).split('\r\n')[0].includes('integrity_valid'), 'paid header keeps integrity_valid');
    const dl = braceSlice(audit, audit.indexOf('{', audit.indexOf('async function downloadExport(format)')));
    assert.match(dl, /records\.omitIntegrity = !\(await isPaidEdition\(\)\);/,
        'the export decides from the fail-closed isPaidEdition()');
});

test('P4: the Access Reviews tab stays CE-hidden', () => {
    assert.match(hub, /const _CE_BUGARATAB_TABS = new Set\(\['access-reviews'\]\);/);
    assert.match(hub, /_ceEdition && _CE_BUGARATAB_TABS\.has\(key\)/);
});
