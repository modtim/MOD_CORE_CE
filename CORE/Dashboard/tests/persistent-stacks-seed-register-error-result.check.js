// Node-runnable check: when the seed register POST fails, the error must be
// written to the ps-seed-result line as well as the ps-seed-status line. The
// e2e dash-stack-seed-upload spec waits on ps-seed-result; without this the
// user sees no result line on a failed register.
//
//   node CORE/Dashboard/tests/persistent-stacks-seed-register-error-result.check.js
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

const start = SRC.indexOf('reg = await registerStackSeed(id, body);');
assert.ok(start >= 0, 'locate registerStackSeed(id, body) call');
const end = SRC.indexOf('const seedId', start);
assert.ok(end > start, 'locate const seedId after the register call');
const block = SRC.slice(start, end);

assert.ok(block.includes('} catch (e) {'), 'register call has a catch block');
assert.ok(
    block.includes("_psSeedText(host, 'ps-seed-status', _psSeedErr(e));"),
    'register catch still writes the ps-seed-status line',
);
assert.ok(
    block.includes('_psSeedResultText(host, _psSeedErr(e));'),
    'register catch also writes the ps-seed-result line',
);
console.log('persistent-stacks-seed-register-error-result: all assertions passed');
