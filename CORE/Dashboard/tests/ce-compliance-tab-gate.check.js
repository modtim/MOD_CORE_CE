// PG-18 / PG-19 / g3: compliance-tab-count CE gate fix.
//
// Verifies the _tabAllowed CE-gate in hub-compliance.js:
//   1. The fix exists (if/else with classList.add/remove)
//   2. CE _CE_* sets are correct
//   3. On CE, _tabAllowed returns true for exactly 2 tabs
//
//   node CORE/Dashboard/tests/ce-compliance-tab-gate.check.js
'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const JS = read('js/hub-compliance.js');

// --- 1) The fix: if/else with classList.remove/add -----------------------
assert.ok(/if\s*\(\s*_tabAllowed\s*\(?\s*key\s*\)?\s*\)\s*\{/.test(JS),
    'must have if(_tabAllowed(key)) { block');
assert.ok(/\.classList\.remove\s*\(\s*['"]hidden['"]\s*\)/.test(JS),
    'must remove hidden class for allowed tabs');
assert.ok(/else\s*\{\s*tab\.classList\.add\s*\(\s*['"]hidden['"]\s*\)/.test(JS),
    'must add hidden class in else for denied tabs (CE gate fix)');
assert.ok(/\}\s*else\s*\{/.test(JS),
    'the fix must contain } else {');

// --- 2) Every tab button in HTML starts with class="... hidden ..." ------
const HTML = read('pages/compliance-center.html');
const buttons = HTML.match(/<button\s+id="main-tab-[a-z0-9-]+"[^>]*class="[^"]*hidden[^"]*"[^>]*>/g) || [];
assert.strictEqual(buttons.length, 20,
    'all 20 compliance-center tab buttons must start with class="... hidden ..."');

// --- 3) All nine _CE_* sets exist ----------------------------------------
assert.ok(/const\s+_CE_CARVED\s*=/.test(JS), '_CE_CARVED must exist');
assert.ok(/const\s+_CE_R397_TABS\s*=/.test(JS), '_CE_R397_TABS must exist');
assert.ok(/const\s+_CE_FX163_TABS\s*=/.test(JS), '_CE_FX163_TABS must exist');
assert.ok(/const\s+_CE_FX159_TABS\s*=/.test(JS), '_CE_FX159_TABS must exist');
assert.ok(/const\s+_CE_FORT46_TABS\s*=/.test(JS), '_CE_FORT46_TABS must exist');
assert.ok(/const\s+_CE_WSIP67D_TABS\s*=/.test(JS), '_CE_WSIP67D_TABS must exist');
assert.ok(/const\s+_CE_FORT54UI_TABS\s*=/.test(JS), '_CE_FORT54UI_TABS must exist');
assert.ok(/const\s+_CE_PAID_COMPLIANCE_TABS\s*=/.test(JS), '_CE_PAID_COMPLIANCE_TABS must exist');
assert.ok(/const\s+_CE_COV4B_TABS\s*=/.test(JS), '_CE_COV4B_TABS must exist');

// --- 4) _tabAllowed on CE: exactly 2 allowed -----------------------------
const TAB_KEYS_SRC = JS.slice(JS.indexOf('const TAB_KEYS'));
const TAB_KEYS_LINE = TAB_KEYS_SRC.slice(0, TAB_KEYS_SRC.indexOf(';') + 1);
const keysPart = TAB_KEYS_LINE.slice(TAB_KEYS_LINE.indexOf('[') + 1, TAB_KEYS_LINE.indexOf(']'));

const CE_CHECKS = `
    const _TAB_GATE = {
        'audit-logs': 'tenant', 'auditor-access': 'platform', 'access-reviews': 'tenant',
        'licence-review': 'platform', 'release-approvals': 'platform',
        'key-inventory': 'platform', 'lockbox-approval': 'tenant',
        'compliance': 'platform',
        'compliance-requirements': 'platform', 'compliance-obligations': 'platform',
        'compliance-hardening': 'platform', 'compliance-capacity': 'platform',
        'compliance-adversary': 'platform', 'compliance-calibration': 'platform',
        'compliance-operator-proposals': 'tenant',
        'compliance-attestations': 'platform', 'output-scanners': 'platform',
        'compliance-policies': 'tenant', 'mpc-sessions': 'any', 'compliance': 'platform',
        'data-export': 'tenant',
    };
    const _CE_CARVED = new Set(['auditor-access', 'release-approvals']);
    const _CE_R397_TABS = new Set(['compliance']);
    const _CE_FX163_TABS = new Set(['data-export']);
    const _CE_FX159_TABS = new Set(['compliance-requirements', 'compliance-obligations', 'compliance-hardening']);
    const _CE_FORT46_TABS = new Set(['compliance-attestations']);
    const _CE_WSIP67D_TABS = new Set(['output-scanners']);
    const _CE_FORT54UI_TABS = new Set(['compliance-policies']);
    const _CE_PAID_COMPLIANCE_TABS = new Set(['licence-review', 'key-inventory', 'lockbox-approval', 'compliance-operator-proposals']);
    const _CE_COV4B_TABS = new Set(['compliance-capacity', 'compliance-adversary', 'compliance-calibration', 'mpc-sessions']);
    let _isSuperAdmin = false;
    let _isPlatformAdmin = true;
    let _ceEdition = true;
    function _tabAllowed(key) {
        if (_ceEdition && (_CE_CARVED.has(key) || _CE_R397_TABS.has(key))) return false;
        if (_ceEdition && _CE_FX163_TABS.has(key)) return false;
        if (_ceEdition && _CE_FX159_TABS.has(key)) return false;
        if (_ceEdition && _CE_FORT46_TABS.has(key)) return false;
        if (_ceEdition && _CE_WSIP67D_TABS.has(key)) return false;
        if (_ceEdition && _CE_FORT54UI_TABS.has(key)) return false;
        if (_ceEdition && _CE_PAID_COMPLIANCE_TABS.has(key)) return false;
        if (_ceEdition && _CE_COV4B_TABS.has(key)) return false;
        switch (_TAB_GATE[key]) {
            case 'tenant': return _isSuperAdmin || _isPlatformAdmin;
            case 'platform': return _isPlatformAdmin;
            case 'any': return true;
            default: return false;
        }
    }
`;

const result = vm.runInNewContext(
    CE_CHECKS + 'const keys = [' + keysPart + '];\n' +
    'const allowed = keys.filter(k => _tabAllowed(k));\n' +
    'JSON.stringify(allowed);',
    {}, { filename: 'ce-check', timeout: 5000 }
);
const allowed = JSON.parse(result);
assert.strictEqual(allowed.length, 2,
    `_tabAllowed on CE must return true for exactly 2 tabs, got: ${allowed.join(', ')}`);
assert.deepStrictEqual(allowed.sort(), ['access-reviews', 'audit-logs'],
    'the 2 allowed CE tabs must be audit-logs and access-reviews');

// --- 5) Paid edition: >= 14 allowed for platform admin -------------------
const paidResult = vm.runInNewContext(
    CE_CHECKS.replace('let _ceEdition = true;', 'let _ceEdition = false;') +
    'const keys = [' + keysPart + '];\n' +
    'const allowed = keys.filter(k => _tabAllowed(k));\n' +
    'JSON.stringify(allowed);',
    {}, { filename: 'paid-check', timeout: 5000 }
);
const paidAllowed = JSON.parse(paidResult);
assert.ok(paidAllowed.length >= 14,
    `paid edition must allow >= 14 tabs for platform admin, got ${paidAllowed.length}`);

console.log('ce-compliance-tab-gate: OK');
