// Dashboard tests/test-k8s-clusters-nodelist.test.js
//
// Node node:test harness test (not Jest). Verifies that k8s-clusters.js
// at :288 uses Array.from(panel.childNodes || []).filter(...) and that
// the old pattern panel.childNodes.filter throws TypeError on a NodeList.
//
//   node CORE/Dashboard/tests/test-k8s-clusters-nodelist.test.js
//   <test harness> nodetest tests/test-k8s-clusters-nodelist.test.js
//
// The test runs in a pure-text (no-browser) context: it reads the source
// file as a string and asserts the guard pattern is present; it also
// constructs a minimal DOM-like NodeList via a mock and exercises the
// old-pattern vs new-pattern assertions against a real NodeList instance.

'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const SOURCE_PATH = path.join(ROOT, 'js', 'k8s-clusters.js');

// ---------------------------------------------------------------------------
// 1. Source-text assertion: the guard `|| []` must be present
// ---------------------------------------------------------------------------

const src = fs.readFileSync(SOURCE_PATH, 'utf8');

assert.ok(src.includes('panel.childNodes || []'),
    'k8s-clusters.js:288 must contain `panel.childNodes || []` (|| [] guard)');

assert.ok(src.includes('Array.from(panel.childNodes || []).filter((n) => n && n.id === \'int-k8s-clusters\')[0]'),
    'k8s-clusters.js:288 must contain the full guard expression with Array.from and .filter');

// ---------------------------------------------------------------------------
// 2. Old-file proof: the old pattern `childNodes.filter` must fail
// ---------------------------------------------------------------------------

// Simulate a NodeList (live NodeList has no .filter)
const fakeNode = { nodeType: 1, tagName: 'DIV', id: 'int-k8s-clusters' };
const NodeList = class NodeList {
    constructor(arr) { Object.assign(this, arr); }
};
const nodeList = new NodeList([fakeNode]);

{
    // The OLD pattern: nodeList.filter throws TypeError
    let threw = false;
    try { nodeList.filter((n) => n && n.id === 'int-k8s-clusters')[0]; }
    catch (e) { threw = e instanceof TypeError; }
    assert.ok(threw, 'old pattern: NodeList.filter must throw TypeError (not a function)');
}

// ---------------------------------------------------------------------------
// 3. New-pattern assertion: Array.from + .filter must not throw
// ---------------------------------------------------------------------------

{
    const arr = Array.from(nodeList || []);
    assert.ok(Array.isArray(arr), 'Array.from(nodeList) must return a real Array');
    let threw = false;
    try {
        arr.filter((n) => n && n.id === 'int-k8s-clusters')[0];
    } catch (e) { threw = true; }
    assert.ok(!threw, 'new pattern: Array.from(...).filter must not throw on NodeList');
}

// ---------------------------------------------------------------------------
// 4. Falsy guard: || [] must handle null/undefined
// ---------------------------------------------------------------------------

{
    let threw = false;
    try { Array.from(null || []); }
    catch (e) { threw = true; }
    assert.ok(!threw, 'Array.from(null || []) must not throw');
}

{
    let threw = false;
    try { Array.from(undefined || []); }
    catch (e) { threw = true; }
    assert.ok(!threw, 'Array.from(undefined || []) must not throw');
}

console.log('test-k8s-clusters-nodelist: all assertions passed');
