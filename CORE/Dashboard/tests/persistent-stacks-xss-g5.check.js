// persistent-stacks-xss-g5.check.js — static SECURITY check for the rc20 batch-5 XSS
// fixes in js/persistent-stacks.js (same style as persistent-stacks-xss-g4.check.js:
// sources read from the RUN TREE via ROOT = dirname(dirname(__dirname)); the old-file
// proof is the MCW_OVERRIDE run, not a second set of reads).
//
// Five raw interpolations into innerHTML sinks remained after the g4 fix:
//  1) _psComposeCreateClick — `(id ${r.id}, ${r.status})` into _psComposeResult innerHTML.
//  2) _psWorkerSelect       — data-ps-id="${s.id}".
//  3) _psLoadStacks         — <tr id="ps-backups-${s.id}">.
//  4) _psGrantRow           — data-ps-grant-revoke="${g.id}".
//  5) _psStackRow data-ps-refs — the old form escaped only ' inside the single-quoted
//     attribute, leaving " and < live; the whole JSON value now goes through _psEsc,
//     which is entity-decoded on read-back (b.dataset.psRefs), so the decoded value
//     is unchanged.
// Per site: escaped/coerced form present, raw form absent, payload survives as
// entities, no raw <img reaches innerHTML, DOM ids byte-identical for integer ids.
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

const compose = fnBody('async function _psComposeCreateClick\\(\\)');
const worker = fnBody('function _psWorkerSelect\\(s, workers\\)');
const load = fnBody('async function _psLoadStacks\\(\\)');
const grant = fnBody('function _psGrantRow\\(g\\)');
const row = fnBody('function _psStackRow\\(s, workers\\)');

const SITES = [
    { fn: 'compose create', body: compose, head: 'let haLine',
      good: '(id ${Number(r.id)}, ${_psEsc(r.status)}).',
      bad: /\(id \$\{\s*r\.id\s*\}, \$\{\s*r\.status\s*\}\)/ },
    { fn: '_psWorkerSelect', body: worker, head: 'data-ps-worker-select',
      good: 'data-ps-id="${Number(s.id)}"',
      bad: /data-ps-id="\$\{\s*s\.id\s*\}"/ },
    { fn: '_psLoadStacks', body: load, head: 'ps-backups',
      good: '<tr id="ps-backups-${Number(s.id)}"></tr>',
      bad: /<tr id="ps-backups-\$\{\s*s\.id\s*\}"/ },
    { fn: '_psGrantRow', body: grant, head: 'data-ps-grant-revoke',
      good: 'data-ps-grant-revoke="${Number(g.id)}"',
      bad: /data-ps-grant-revoke="\$\{\s*g\.id\s*\}"/ },
    { fn: '_psStackRow refs', body: row, head: 'const refs',
      good: 'const refs = _psEsc(JSON.stringify(s.storage_refs || {}));',
      bad: /const refs = JSON\.stringify\(s\.storage_refs \|\| \{\}\)\.replace/ },
];
for (const s of SITES) {
    test(`${s.fn}: escaped/coerced form present`, () => {
        assert.ok(s.body.includes(s.good), `${s.fn}: ${s.good} must be present`);
    });
    test(`${s.fn}: raw form absent`, () => {
        assert.ok(!s.bad.test(s.body), `${s.fn}: the raw \${...} form must not remain`);
    });
}

// 1) render the real create-result statement with a hostile id/status.
const cTail = /let haLine = '';[\s\S]*?, true\);/.exec(compose);
assert.ok(cTail, 'the create-result statement must be extractable');
const cRender = new Function('r', escBlock
    + '\nlet out = "";\nfunction _psComposeResult(html, ok) { out = html; }\n'
    + cTail.toString() + '\nreturn out;');
const COUT = cRender({ name: EVIL, id: EVIL, status: EVIL, ha_class: EVIL, ha_class_reason: EVIL });
test('create result: payload survives as entities', () => {
    assert.ok(COUT.includes('&lt;img src=x onerror=alert(1)&gt;'), 'payload must survive escaped');
});
test('create result: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(COUT), 'no raw <img may reach innerHTML');
});
test('create result: integer id stays byte-identical', () => {
    const out = cRender({ name: 'n', id: 42, status: 'running' });
    assert.ok(out.includes('(id 42, running).'), 'Number(42) must render exactly 42');
});

// 2) render the real _psWorkerSelect with a hostile stack id.
const wRender = new Function('s', 'workers', escBlock
    + '\nfunction _psWorkerOption(n, s, sel) { return `<option value="${_psEsc(n)}">${_psEsc(n)}</option>`; }\n'
    + worker.toString() + '\nreturn _psWorkerSelect(s, workers);');
const WOUT = wRender({ id: EVIL, pinned_worker: EVIL, pinned_reason: EVIL, target_worker: EVIL, hardware_ineligible: {} }, [EVIL]);
test('_psWorkerSelect: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(WOUT), 'no raw <img may reach innerHTML');
});
test('_psWorkerSelect: integer id byte-identical', () => {
    const out = wRender({ id: 7, pinned_worker: 'wk', pinned_reason: '', target_worker: 'wk', hardware_ineligible: {} }, ['wk']);
    assert.ok(out.includes('data-ps-id="7"'), 'data-ps-id must stay 7');
});

// 3) render the real _psLoadStacks row map.
const lTail = /rows\.innerHTML = stacks\.map\(s =>[\s\S]*?\)\.join\(''\);/.exec(load);
assert.ok(lTail, 'the _psLoadStacks row map must be extractable');
const lRender = new Function('stacks', 'workers', escBlock
    + '\nconst rows = { innerHTML: "" };\nfunction _psStackRow(s, workers) { return ""; }\n'
    + lTail.toString() + '\nreturn rows.innerHTML;');
const LOUT = lRender([{ id: EVIL }, { id: 11 }], []);
test('_psLoadStacks: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(LOUT), 'no raw <img may reach innerHTML');
});
test('_psLoadStacks: integer id byte-identical', () => {
    assert.ok(LOUT.includes('<tr id="ps-backups-11"></tr>'), 'host must stay ps-backups-11');
});

// 4) render the real _psGrantRow.
const gRender = new Function('g', escBlock + '\n' + grant.toString() + '\nreturn _psGrantRow(g);');
// g.user_id is a separate raw text-cell interpolation (not one of the five
// sites under review) — keep it benign so the assertion targets the id only.
const GOUT = gRender({ user_id: 'u-1', expires_at: null, active: true, id: EVIL });
test('_psGrantRow: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(GOUT), 'no raw <img may reach innerHTML');
});
test('_psGrantRow: integer id byte-identical', () => {
    const out = gRender({ user_id: 'u', expires_at: null, active: true, id: 23 });
    assert.ok(out.includes('data-ps-grant-revoke="23"'), 'revoke key must stay 23');
});

// 5) data-ps-refs: whole value through _psEsc; the single-quoted attribute is
// entity-decoded on read-back (b.dataset.psRefs), so the decoded JSON is unchanged.
test('data-ps-refs: read back via dataset.psRefs (entity-decoded)', () => {
    assert.ok(/b\.dataset\.psRefs/.test(SRC), 'the read-back site must stay a dataset read');
});
const rStmt = /const refs = _psEsc\(JSON\.stringify\(s\.storage_refs \|\| \{\}\)\);/.exec(row);
const rBtn = /<button data-ps-act="restore"[\s\S]*?<\/button>/.exec(row);
assert.ok(rStmt && rBtn, 'the refs statement and restore button must be extractable');
const rRender = new Function('s', escBlock + '\nconst sid = Number(s.id);\n'
    + rStmt.toString() + '\nreturn `' + rBtn.toString() + '`;');
const HOSTILE = { [EVIL]: EVIL };
const ROUT = rRender({ id: 3, storage_refs: HOSTILE });
test('data-ps-refs: no raw <img element produced', () => {
    assert.ok(!/<img/i.test(ROUT), 'no raw <img may reach innerHTML');
});
test('data-ps-refs: decoded value unchanged', () => {
    const m = /data-ps-refs='([^']*)'/.exec(ROUT);
    assert.ok(m, 'the refs attribute must be present');
    const decoded = m[1].replace(/&quot;/g, '"').replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    assert.ok(decoded === JSON.stringify(HOSTILE),
        `decoded refs must equal the original JSON (got ${decoded})`);
});

test('persistent-stacks.js still parses as an ES module', () => {
    let err = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'], { input: SRC, encoding: 'utf8' });
    } catch (e) { err = String(e && (e.stderr || e.message)); }
    assert.ok(!err, `not valid module — ${(err || '').split('\n')[0] || 'unknown'}`);
});

console.log('persistent-stacks-xss-g5: all checks passed');
