// persistent-stacks-xss-g4.check.js — static SECURITY check for the rc20e XSS fix
// in js/persistent-stacks.js (same style as innerhtml-escape-g3.check.js and
// persistent-stacks-compose-error-escape.check.js: sources read from the RUN
// TREE via ROOT = dirname(dirname(__dirname)), never absolute /tmp paths; the
// old-file proof is the MCW_OVERRIDE run, not a second set of reads).
//
// Two innerHTML sinks interpolate server data raw:
//  A) _psLoadNetworkingUsage (GET /stacks/networking/usage) — the legacy/managed
//     rows echo compose-authored service names, network names and stack ids
//     (persistent_stacks.py:1240, persistent_stacks_shared_service.py:273-301).
//     A compose service/network name is operator-authored free text, so
//     `<img src=x onerror=…>` in it executed as markup.
//  B) _psStackRow — the numeric stack id interpolated 25x into the row (text
//     cells, attribute values, element ids). Coerced with Number(...) so a
//     hostile id can never carry markup.
// Per site: escaped/coerced form present, raw form absent, helper exists and
// escapes quotes (attribute contexts), payload survives as entities, file parses.
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

// Brace-matched function body so each assertion is scoped to its own function.
function fnBody(head) {
    const m = new RegExp(head).exec(SRC);
    assert.ok(m, `locate ${head}`);
    const open = SRC.indexOf('{', m.index);
    let depth = 0;
    for (let i = open; i < SRC.length; i++) {
        if (SRC[i] === '{') depth += 1;
        else if (SRC[i] === '}') { depth -= 1; if (depth === 0) return SRC.slice(m.index, i + 1); }
    }
    assert.ok(false, 'unterminated function body');
}

const usage = fnBody('async function _psLoadNetworkingUsage\\(\\)');
const row = fnBody('function _psStackRow\\(s, workers\\)');

// ---------------------------------------------------------------------------
// The escaper: exists, and escapes BOTH quote characters (the attribute
// contexts at :3321/:3350/:3359 need " and ' to be entities).
// ---------------------------------------------------------------------------
const escStart = SRC.indexOf('function _psEsc(s) {');
assert.ok(escStart >= 0, 'persistent-stacks.js must define the _psEsc helper');
const escEnd = SRC.indexOf('\nfunction _psMsg');
assert.ok(escEnd > escStart, 'the _psEsc helper body must be extractable');
const escBlock = SRC.slice(escStart, escEnd);
for (const q of ['"', "'"]) {
    assert.ok(escBlock.includes(q), `_psEsc body must handle the ${q} quote character`);
}
assert.ok(/&quot;/.test(escBlock) && /&#39;/.test(escBlock),
    '_psEsc must map " to &quot; and \' to &#39; (attribute contexts)');

const EVIL = '<img src=x onerror=alert(1)>';
const esc = new Function('s', escBlock + '\nreturn _psEsc(s);');
assert.ok(!/[<>]/.test(esc(EVIL)) && esc(EVIL).includes('&lt;img'),
    `_psEsc must turn ${EVIL} into entities`);

// ---------------------------------------------------------------------------
// Site A — _psLoadNetworkingUsage.
// ---------------------------------------------------------------------------
const A = [
    { label: 'legacy service name (:3814)', good: '${_psEsc(p.service)}:', bad: /\$\{\s*p\.service\s*\}/ },
    { label: 'legacy network name (:3815)', good: '${_psEsc(s2.network)}=', bad: /\$\{\s*s2\.network\s*\}/ },
    { label: 'legacy stack id (:3816)', good: '(id ${Number(r.id)}, legacy)', bad: /\(id \$\{\s*r\.id\s*\}, legacy\)/ },
    { label: 'managed service name (:3821)', good: '${_psEsc(p.service)} ', bad: /`\$\{\s*p\.service\s*\}/ },
    { label: 'managed host (:3821)', good: ':${_psEsc(String(p.host))}', bad: /`:\$\{\s*p\.host\s*\}`/ },
    { label: 'managed protocol (:3821)', good: '(${_psEsc(p.protocol', bad: /\(\$\{\s*p\.protocol\s*\|\| 'tcp'\s*\}/ },
    { label: 'managed stack id (:3822)', good: '(id ${Number(r.id)}, managed)', bad: /\(id \$\{\s*r\.id\s*\}, managed\)/ },
];
for (const s of A) {
    test(`networking-usage ${s.label}: escaped/coerced form present`, () => {
        assert.ok(usage.includes(s.good), `${s.label}: ${s.good} must be in _psLoadNetworkingUsage`);
    });
    test(`networking-usage ${s.label}: raw form absent`, () => {
        assert.ok(!s.bad.test(usage), `${s.label}: the raw \${...} form must not remain`);
    });
}

// Non-vacuous: run the REAL render block (const legacy .. body.innerHTML = html)
// with hostile compose-authored names/ids — no element-producing markup may
// reach innerHTML, and the id cells must be numbers.
const tail = /const legacy = d\.legacy \|\| \[\];[\s\S]*?body\.innerHTML = html;/.exec(usage);
assert.ok(tail, 'the networking-usage render block must be extractable');
const render = new Function('d', escBlock + '\nconst body = { innerHTML: "" }; const card = { classList: { remove: () => {} } };\n'
    + tail.toString() + '\nreturn body.innerHTML;');
const EVILROW = { id: EVIL, name: EVIL, host_ports: [{ service: EVIL, port: EVIL, host: EVIL, protocol: EVIL, bind: EVIL }],
                   subnets: [{ network: EVIL, subnet: EVIL }] };
const OUT = render({ legacy: [EVILROW], managed: [EVILROW] });
test('networking-usage: hostile names render as entities', () => {
    assert.ok(OUT.includes('&lt;img src=x onerror=alert(1)&gt;'),
        'the payload must survive as escaped text');
});
test('networking-usage: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(OUT), 'no raw <img may reach innerHTML');
});
test('networking-usage: both id cells are coerced numbers', () => {
    assert.ok(OUT.includes('(id NaN, legacy)') && OUT.includes('(id NaN, managed)'),
        'the stack id cells must be Number(...) coerced');
});

// ---------------------------------------------------------------------------
// Site B — _psStackRow: every \${s.id} becomes the coerced \${sid}.
// ---------------------------------------------------------------------------
test('_psStackRow: numeric coercion of the stack id', () => {
    assert.ok(/const sid = Number\(s\.id\);/.test(row),
        '_psStackRow must coerce the id once: const sid = Number(s.id);');
});
test('_psStackRow: no raw \${s.id} remains in the row template', () => {
    assert.ok(!/\$\{\s*s\.id\s*\}/.test(row),
        'every id interpolation in _psStackRow must use the coerced sid');
});
test('_psStackRow: coerced id is actually used', () => {
    const n = (row.match(/\$\{sid\}/g) || []).length;
    assert.ok(n >= 25, `_psStackRow must interpolate \${sid} at every former site (got ${n})`);
});
test('_psStackRow: text cell and attribute contexts both use it', () => {
    assert.ok(/data-ps-main="\$\{sid\}"/.test(row), 'attribute context must use the coerced id');
    assert.ok(/text-gray-500">\$\{sid\}</.test(row), 'text cell must use the coerced id');
});

// ---------------------------------------------------------------------------
// The file must still parse as an ES module (it imports from ./api.js).
// ---------------------------------------------------------------------------
test('persistent-stacks.js still parses as an ES module', () => {
    let err = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'], { input: SRC, encoding: 'utf8' });
    } catch (e) { err = String(e && (e.stderr || e.message)); }
    assert.ok(!err, `not valid module — ${(err || '').split('\n')[0] || 'unknown'}`);
});

console.log('persistent-stacks-xss-g4: all checks passed');
