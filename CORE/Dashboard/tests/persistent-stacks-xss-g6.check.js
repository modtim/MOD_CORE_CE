// persistent-stacks-xss-g6.check.js — static SECURITY check for the rc20e batch-6 XSS
// fixes in js/persistent-stacks.js (same extraction style as persistent-stacks-xss-g5.check.js:
// sources read from the RUN TREE via ROOT = dirname(dirname(__dirname)); the old-file
// proof is the MCW_OVERRIDE run, not a second set of reads).
//
// Four raw interpolations of server data remained after the g5 fix:
//  1) _psGrantRow          — <td ...>${g.user_id}</td> (TEXT cell, sink body.innerHTML).
//  2) _psBackups           — value="${sch.interval_hours ?? ''}" (double-quoted attribute).
//  3) _psHostPortEntry     — value="${e.target ?? ''}" and the host-port ternary (attributes).
//  4) _psDefRevisionOptions — <option value="${r.revision_no}"> + `rev ${r.revision_no}`
//     (numeric revision id: Number() coercion; e2e reads the option value / parseInt()s it,
//     and integer ids render byte-identically).
// Per site: escaped/coerced form present, raw form absent, payload survives as entities,
// no raw <img reaches innerHTML, numeric values byte-identical.
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SRC = readFileSync(join(ROOT, 'js', 'persistent-stacks.js'), 'utf8');

function fnBody(head) {
    const m = new RegExp(head).exec(SRC);
    assert.ok(m, `locate ${head}`);
    const open = SRC.indexOf('{', m.index);
    let depth = 0;
    for (let i = open; i < SRC.length; i += 1) {
        if (SRC[i] === '{') depth += 1;
        else if (SRC[i] === '}') { depth -= 1; if (depth === 0) return SRC.slice(m.index, i + 1); }
    }
    assert.ok(false, 'unterminated function body');
}

const escStart = SRC.indexOf('function _psEsc(s) {');
assert.ok(escStart >= 0, 'persistent-stacks.js must define the _psEsc helper');
const escEnd = SRC.indexOf('\nfunction _psMsg');
assert.ok(escEnd > escStart, 'the _psEsc helper body must be extractable');
const escBlock = SRC.slice(escStart, escEnd);
assert.ok(/&quot;/.test(escBlock) && /&#39;/.test(escBlock),
    '_psEsc must map " to &quot; and \' to &#39; (attribute contexts)');

const EVIL = '<img src=x onerror=alert(1)>';
const esc = new Function('s', escBlock + '\nreturn _psEsc(s);');
assert.ok(!/[<>]/.test(esc(EVIL)) && esc(EVIL).includes('&lt;img'),
    `_psEsc must turn ${EVIL} into entities`);

const grant = fnBody('function _psGrantRow\\(g\\)');
const backups = fnBody('async function _psBackups\\(id\\)');
const hp = fnBody('function _psHostPortEntry\\(id, i, e\\)');
const rev = fnBody('function _psDefRevisionOptions\\(id\\)');

const SITES = [
    { fn: '_psGrantRow user_id', body: grant, head: 'text-gray-500',
      good: '<td class="px-4 py-2 text-gray-500">${_psEsc(g.user_id)}</td>',
      bad: /\$\{\s*g\.user_id\s*\}/ },
    { fn: '_psBackups interval_hours', body: backups, head: 'ps-sched',
      good: "value=\"${_psEsc(sch.interval_hours ?? '')}\"",
      bad: /value="\$\{\s*sch\.interval_hours\s*\?\?\s*''\s*\}/ },
    { fn: '_psHostPortEntry target', body: hp, head: 'hp-field="target"',
      good: "value=\"${_psEsc(e.target ?? '')}\"",
      bad: /value="\$\{\s*e\.target\s*\?\?\s*''\s*\}/ },
    { fn: '_psHostPortEntry host', body: hp, head: 'hp-field="host-port"',
      good: "value=\"${_psEsc((e.host !== 'any' && e.host != null) ? e.host : '')}\"",
      bad: /value="\$\{\s*\(e\.host !== 'any' && e\.host != null\) \? e\.host : ''\s*\}/ },
    { fn: '_psDefRevisionOptions revNo', body: rev, head: 'const revNo',
      good: 'const revNo = Number(r.revision_no);',
      bad: /value="\$\{\s*r\.revision_no\s*\}"/ },
];
for (const s of SITES) {
    test(`${s.fn}: escaped/coerced form present`, () => {
        assert.ok(s.body.includes(s.good), `${s.fn}: ${s.good} must be present`);
    });
    test(`${s.fn}: raw form absent`, () => {
        assert.ok(!s.bad.test(s.body), `${s.fn}: the raw \${...} form must not remain`);
    });
}
test('_psDefRevisionOptions: option/text use the coerced revNo', () => {
    assert.ok(rev.includes('<option value="${revNo}">') && /rev \$\{revNo\} ·/.test(rev),
        'both the option value and the rev label must use revNo');
    assert.ok(!/rev \$\{\s*r\.revision_no/.test(rev), 'the raw rev label form must not remain');
});

// 1) render the real _psGrantRow with a hostile user_id.
const gRender = new Function('g', escBlock + '\n' + grant.toString() + '\nreturn _psGrantRow(g);');
const GOUT = gRender({ user_id: EVIL, expires_at: null, active: true, id: 23 });
test('_psGrantRow: hostile user_id survives as entities', () => {
    assert.ok(GOUT.includes('&lt;img src=x onerror=alert(1)&gt;'), 'payload must survive escaped');
});
test('_psGrantRow: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(GOUT), 'no raw <img may reach innerHTML');
});
test('_psGrantRow: integer user_id byte-identical', () => {
    const out = gRender({ user_id: 42, expires_at: null, active: true, id: 23 });
    assert.ok(out.includes('<td class="px-4 py-2 text-gray-500">42</td>'), 'user id must stay 42');
});

// 2) render the real schedule statement from _psBackups.
const sStmt = /host\.innerHTML = `<td colspan="8"[\s\S]*?<\/td>`;/.exec(backups);
assert.ok(sStmt, 'the _psBackups schedule statement must be extractable');
const sRender = new Function('id', 'd', 'r', 'sch', 'snapRows',
    escBlock + '\nconst host = { innerHTML: "" };\n' + sStmt.toString() + '\nreturn host.innerHTML;');
const SOUT = sRender(1, {}, {}, { interval_hours: EVIL }, '');
test('_psBackups: hostile interval_hours survives as entities', () => {
    assert.ok(SOUT.includes('value="&lt;img src=x onerror=alert(1)&gt;"'), 'payload must survive escaped');
});
test('_psBackups: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(SOUT), 'no raw <img may reach innerHTML');
});
test('_psBackups: numeric interval byte-identical', () => {
    assert.ok(sRender(1, {}, {}, { interval_hours: 24 }, '').includes('value="24"'), '24 must stay 24');
    assert.ok(sRender(1, {}, {}, { interval_hours: null }, '').includes('value=""'), 'null stays empty');
});

// 3) render the real _psHostPortEntry with hostile target/host.
const hRender = new Function('id', 'i', 'e', escBlock + '\n' + hp.toString() + '\nreturn _psHostPortEntry(id, i, e);');
const HOUT = hRender('7', 0, { service: EVIL, target: EVIL, host: EVIL, bind: EVIL,
    protocol: 'tcp', allocation: null, pending: false, legacy: false });
test('_psHostPortEntry: hostile target/host survive as entities', () => {
    assert.ok(HOUT.includes('value="&lt;img src=x onerror=alert(1)&gt;"'), 'payload must survive escaped');
});
test('_psHostPortEntry: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(HOUT), 'no raw <img may reach innerHTML');
});
test('_psHostPortEntry: numeric ports byte-identical', () => {
    const out = hRender('7', 0, { service: 'web', target: 8000, host: 8123, bind: '',
        protocol: 'tcp', allocation: null, pending: false, legacy: false });
    assert.ok(out.includes('value="8000"') && out.includes('value="8123"'), 'ports must stay 8000/8123');
    const any = hRender('7', 0, { service: 'web', target: 8000, host: 'any', bind: '',
        protocol: 'tcp', allocation: null, pending: false, legacy: false });
    assert.ok(any.includes('value=""'), 'host=any stays an empty value');
});

// 4) render the real _psDefRevisionOptions with a hostile revision_no.
const rRender = new Function('id', '_psDefState', escBlock + '\n' + rev.toString()
    + '\nreturn _psDefRevisionOptions(id);');
const ROUT = rRender('s', { s: { revisions: [{ revision_no: EVIL, origin: EVIL, message: EVIL }] } });
test('_psDefRevisionOptions: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(ROUT), 'no raw <img may reach innerHTML');
});
test('_psDefRevisionOptions: integer revision byte-identical', () => {
    const out = rRender('s', { s: { revisions: [{ revision_no: 3, origin: 'o', message: '' }] } });
    assert.ok(out.includes('value="3"') && out.includes('rev 3 ·'), 'revision 3 must stay 3');
});

test('persistent-stacks.js still parses as an ES module', () => {
    let err = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'], { input: SRC, encoding: 'utf8' });
    } catch (e) { err = String(e && (e.stderr || e.message)); }
    assert.ok(!err, `not valid module — ${(err || '').split('\n')[0] || 'unknown'}`);
});

console.log('persistent-stacks-xss-g6: all checks passed');
