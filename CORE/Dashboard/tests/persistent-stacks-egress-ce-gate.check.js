// Node-runnable check: the per-stack egress badge ("no reach" / "API only" /
// "allow-listed hosts" / "reach: undeclared") is hidden on Community Edition.
// Per-stack egress enforcement is a paid layer, so on CE a stack runs with its
// default networking and the badge would promise a restriction nothing enforces.
// The gate is the existing edition predicate (api.js isPaidEdition: positive
// saas/pro only, fail-closed). The badge code is EXTRACTED VERBATIM from the
// shipped source and run against stub data.
//
//   node CORE/Dashboard/tests/persistent-stacks-egress-ce-gate.check.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

function blockAt(src, open) {
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function fnSrc(head) {
    const at = SRC.indexOf(head);
    assert.ok(at >= 0, `locate ${head}`);
    return SRC.slice(at, SRC.indexOf('{', at) + blockAt(SRC, SRC.indexOf('{', at)).length);
}

const esc = fnSrc('function _psEsc(s)');
const consts = SRC.slice(SRC.indexOf('const _PS_EGRESS_MODE_LABEL'), SRC.indexOf('// Per-stack egress enforcement is a paid layer'));
const badge = fnSrc('function _psEgressBadge(s)');

function build(paid) {
    return new Function(`${esc}\n${consts}\nlet _psPaidEdition = ${paid};\n${badge}\nreturn _psEgressBadge;`)();
}

const WITH_POLICY = { egress_policy: { web: { mode: 'hosts', hosts: ['api.example.com'] } } };
const NO_POLICY = { egress_policy: {} };

// CE / unknown edition: nothing rendered, declared or not.
assert.strictEqual(build(false)(WITH_POLICY), '');
assert.strictEqual(build(false)(NO_POLICY), '');

// Paid edition: unchanged output.
assert.ok(build(true)(WITH_POLICY).includes('allow-listed hosts'));
assert.ok(build(true)(NO_POLICY).includes('reach: undeclared'));

// Wiring: the flag is fed by the shared fail-closed predicate before rows render.
assert.ok(/import \{[^}]*\bisPaidEdition\b[^}]*\} from '\.\/api\.js'/.test(SRC), 'isPaidEdition imported');
const load = fnSrc('async function _psLoadStacks()');
assert.ok(/isPaidEdition\(\)\.catch\(\(\) => false\)/.test(load), 'predicate failure fails closed');
assert.ok(load.indexOf('_psPaidEdition = !!paid') < load.indexOf('_psStackRow('), 'flag set before rows render');
assert.ok(/let _psPaidEdition = false;/.test(SRC), 'defaults to hidden');

console.log('persistent-stacks-egress-ce-gate: OK');
