// Node-runnable check for the stack variables panel helpers
// (Dashboard/js/stack-variables.js, 878 lines, 0 test refs before this file).
// The module is an ES module (imports fetchApi from api.js), so this script
// extracts the pure helper block (from `function _svEsc` to the end of
// svMissingNames) and runs it in a vm sandbox. Asserts:
//   (a) _svEsc: five entities, null -> '' (values are never logged, but a
//       hostile placeholder name must never inject markup into a chip);
//   (b) _svSize: null/non-numeric/negative -> '', B/KB/MB/GB ladder with one
//       decimal (the chip copy users read next to a secret_file row);
//   (c) _svRel: null -> '', unparseable -> '', just now / m / h / d buckets;
//   (d) SV_KIND_LABEL + SV_KINDS: every kind the picker offers has copy, and
//       the list order is the dropdown order;
//   (e) SV_SECRET_FILE_MAX_BYTES: the 1 MiB client mirror of
//       stacks.secret_file_max_bytes (a 1 MiB file is allowed, 1 MiB+1 is not);
//   (f) _svB64FromBytes: chunked btoa survives >0x8000 bytes and round-trips;
//   (g) svMissingNames: string detail, FastAPI field-list detail, and a
//       {missing:[...]}/{names:[...]}/{message} dict all land as the panel's
//       known names only - and an empty knownNames list returns [].
//
//   node --test CORE/Dashboard/tests/stack-variables-panel.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'stack-variables.js'), 'utf8');
const start = src.indexOf('function _svEsc(s)');
const end = src.indexOf('// ============================================================================\n// "Choose from my files');
assert.ok(start >= 0 && end > start, 'could not locate the sv helper block');
const block = src.slice(start, end).replace(/^export /gm, '');

const sandbox = {
    console: { log: () => {}, error: () => {} },
    btoa: global.btoa,
};
const fns = vm.runInNewContext(
    `"use strict";\n${block}\n({ _svEsc, _svSize, _svRel, SV_KIND_LABEL, SV_KINDS, SV_SECRET_FILE_MAX_BYTES, _svB64FromBytes, svMissingNames });`,
    sandbox, { filename: 'stack-variables-body' });

const DAY = 86400000;
function isoAgo(seconds) { return new Date(Date.now() - seconds * 1000).toISOString(); }

test('_svEsc: five entities, null -> empty', () => {
    assert.strictEqual(fns._svEsc(null), '');
    assert.strictEqual(fns._svEsc('<a href="x" onerror=\'y\'>&'),
        '&lt;a href=&quot;x&quot; onerror=&#39;y&#39;&gt;&amp;');
});

test('_svSize: null/negative/non-numeric blank, B/KB/MB/GB ladder', () => {
    assert.strictEqual(fns._svSize(null), '');
    assert.strictEqual(fns._svSize(-1), '');
    assert.strictEqual(fns._svSize('abc'), '');
    assert.strictEqual(fns._svSize(0), '0 B');
    assert.strictEqual(fns._svSize(1023), '1023 B');
    assert.strictEqual(fns._svSize(1024), '1.0 KB');
    assert.strictEqual(fns._svSize(1536), '1.5 KB');
    assert.strictEqual(fns._svSize(2 * 1024 * 1024), '2.0 MB');
    assert.strictEqual(fns._svSize(2 * 1073741824), '2.0 GB');
});

test('_svRel: null/unparseable blank, just now / m / h / d buckets', () => {
    assert.strictEqual(fns._svRel(null), '');
    assert.strictEqual(fns._svRel('garbage'), '');
    assert.strictEqual(fns._svRel(isoAgo(0)), 'just now');
    assert.strictEqual(fns._svRel(isoAgo(59)), 'just now');
    assert.strictEqual(fns._svRel(isoAgo(60)), '1m ago');
    assert.strictEqual(fns._svRel(isoAgo(3600)), '1h ago');
    assert.strictEqual(fns._svRel(isoAgo(86400)), '1d ago');
});

test('SV_KIND_LABEL covers every SV_KINDS entry in order', () => {
    assert.strictEqual(JSON.stringify(fns.SV_KINDS), JSON.stringify(['plain', 'secret_env', 'secret_file', 'storage_file', 'existing']));
    for (const k of fns.SV_KINDS) {
        assert.ok(typeof fns.SV_KIND_LABEL[k] === 'string' && fns.SV_KIND_LABEL[k],
            `kind ${k} has no copy`);
    }
    assert.strictEqual(fns.SV_KIND_LABEL.secret_env, 'Secret → environment variable');
    assert.strictEqual(fns.SV_KIND_LABEL.storage_file, 'File from my storage');
});

test('SV_SECRET_FILE_MAX_BYTES is the 1 MiB client mirror', () => {
    assert.strictEqual(fns.SV_SECRET_FILE_MAX_BYTES, 1048576);
    assert.ok(fns.SV_SECRET_FILE_MAX_BYTES === 1024 * 1024, 'must match the platform default');
});

test('_svB64FromBytes: chunked, survives >0x8000 bytes, round-trips', () => {
    const bytes = new Uint8Array(0x8000 + 10);
    for (let i = 0; i < bytes.length; i++) bytes[i] = i % 256;
    const b64 = fns._svB64FromBytes(bytes);
    assert.ok(/^[A-Za-z0-9+/]+=*$/.test(b64), 'not base64');
    const back = global.Buffer.from(b64, 'base64');
    assert.strictEqual(back.length, bytes.length, 'length changed through btoa');
    assert.strictEqual(back[0x8000], bytes[0x8000], 'chunk boundary corrupted');
});

test('svMissingNames: string / FastAPI list / dict details -> known names only', () => {
    const known = ['MODEL_PATH', 'API_KEY', 'UNRELATED'];
    const eq = (got, want, msg) => assert.strictEqual(
        JSON.stringify(got), JSON.stringify(want), msg);
    eq(fns.svMissingNames({ detail: 'missing MODEL_PATH and API_KEY' }, known),
        ['MODEL_PATH', 'API_KEY'], 'string detail not matched');
    eq(fns.svMissingNames(
        { detail: [{ loc: ['body', 'MODEL_PATH'], msg: 'field required' }] }, known),
        ['MODEL_PATH'], 'FastAPI field-list detail not matched');
    eq(fns.svMissingNames({ detail: { missing: ['API_KEY'] } }, known),
        ['API_KEY'], 'missing dict not matched');
    eq(fns.svMissingNames({ detail: { names: ['MODEL_PATH'] } }, known),
        ['MODEL_PATH'], 'names dict not matched');
    eq(fns.svMissingNames({ detail: { message: 'UNRELATED is unset' } }, known),
        ['UNRELATED'], 'message dict not matched');
    eq(fns.svMissingNames({ message: 'MODEL_PATH missing' }, known),
        ['MODEL_PATH'], 'top-level message not matched');
    eq(fns.svMissingNames({ detail: 'MODEL_PATH' }, []),
        [], 'no known names -> nothing highlighted');
    eq(fns.svMissingNames(null, known), [], 'null error -> nothing highlighted');
});
