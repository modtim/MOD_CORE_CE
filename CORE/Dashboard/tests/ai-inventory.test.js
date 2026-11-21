// Node-runnable check for the FX.72 p3b "AI in this platform" page
// (CORE/Dashboard/js/ai-inventory.js). ai-inventory.js is an ES module,
// so this script extracts the body (from the first helper function to
// EOF, with the `export ` keyword stripped) and runs it in a vm sandbox
// with stubbed DOM elements (createElement/appendChild/textContent),
// aiInventory (api.js), then asserts:
//   (a) golden path: features/models/data-flow render, badges reflect
//       enabled/on-prem/encrypted flags;
//   (b) empty state: no features/models/data-flow -> empty-state line
//       shown, nothing rendered;
//   (c) error state: server detail surfaced verbatim, sections cleared;
//   (d) no innerHTML injection: a hostile feature name containing
//       "<img onerror>" never appears as raw markup — every row is
//       built with real DOM nodes + textContent, not string concatenation.
//
//   node --test CORE/Dashboard/tests/ai-inventory.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'ai-inventory.js'), 'utf8');
const start = src.indexOf('function _showError');
assert.ok(start >= 0, 'could not locate the ai-inventory body');
const block = src.slice(start).replace(/export async function/g, 'async function');

const IDS = ['ai-error', 'ai-empty', 'ai-refresh',
    'ai-features-body', 'ai-models-body', 'ai-dataflow-list'];

function freshInventory() {
    return {
        features: [
            { key: 'node_wizard', name: 'Node Wizard', purpose: 'Build node containers from a description',
              model_role: 'coder', data_sent: ['repo url', 'docs text'], stays_on_prem: true, enabled: true },
            { key: 'support_chat', name: 'Support Chat', purpose: 'Answer platform questions',
              model_role: 'reasoner', data_sent: ['chat text'], stays_on_prem: false, enabled: false },
        ],
        models: [
            { role: 'coder', model_id: 'ornith-1.5-35b-a3b-q8-0', serving_backend: 'vLLM', runs_where: 'Dell', available: true },
        ],
        data_flow: [
            { from: 'Dashboard', to: 'API', data: 'prompt text', encrypted_in_transit: true },
            { from: 'API', to: 'vLLM gateway', data: 'prompt text', encrypted_in_transit: false },
        ],
    };
}

// ---------------------------------------------------------------------------
// sandbox — a minimal but real DOM: createElement returns nodes that
// support appendChild/className/textContent, so renderFeatures/
// renderModels/renderDataFlow build a real tree instead of a string.
// ---------------------------------------------------------------------------

function makeNode(tag) {
    const node = {
        tag, children: [], className: '', textContent: '', dataset: {},
        listeners: {}, _hidden: false,
    };
    node.appendChild = (child) => { node.children.push(child); return child; };
    node.addEventListener = (type, fn) => { node.listeners[type] = fn; };
    node.classList = {
        add: (c) => { if (c === 'hidden') node._hidden = true; },
        remove: (c) => { if (c === 'hidden') node._hidden = false; },
        toggle: () => {},
    };
    // Flattened text of this node + all descendants (what a user sees).
    Object.defineProperty(node, 'allText', {
        get() {
            let out = node.textContent || '';
            for (const c of node.children) out += (c.allText !== undefined ? c.allText : c.textContent || '');
            return out;
        },
    });
    return node;
}

function run(getInventory) {
    const els = {};
    for (const id of IDS) {
        const el = makeNode(id);
        el.id = id;
        els[id] = el;
    }

    const calls = { get: 0 };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: {
            getElementById: (id) => els[id],
            createElement: (tag) => makeNode(tag),
        },
        aiInventory: {
            get: () => {
                calls.get++;
                return Promise.resolve(getInventory());
            },
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeAiInventoryPage, loadInventory });`,
        sandbox, { filename: 'ai-inventory-body' });
    return { fns, els, calls };
}

async function drain() { for (let i = 0; i < 60; i++) await Promise.resolve(); }

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

test('golden path: features/models/data-flow render with correct badges', async () => {
    const { fns, els, calls } = run(() => freshInventory());
    await fns.initializeAiInventoryPage();
    await drain();
    assert.strictEqual(calls.get, 1, 'aiInventory.get not called');

    const featuresText = els['ai-features-body'].allText;
    assert.ok(featuresText.includes('Node Wizard'), 'feature name missing');
    assert.ok(featuresText.includes('On-prem'), 'on-prem badge missing');
    assert.ok(featuresText.includes('Leaves platform'), 'leaves-platform badge missing');
    assert.ok(featuresText.includes('Enabled'), 'enabled badge missing');
    assert.ok(featuresText.includes('Disabled'), 'disabled badge missing');

    const modelsText = els['ai-models-body'].allText;
    assert.ok(modelsText.includes('ornith-1.5-35b-a3b-q8-0'), 'model id missing');
    assert.ok(modelsText.includes('vLLM'), 'serving backend missing');

    const flowText = els['ai-dataflow-list'].allText;
    assert.ok(flowText.includes('Dashboard') && flowText.includes('API'), 'data-flow endpoints missing');
    assert.ok(flowText.includes('Encrypted in transit'), 'encrypted badge missing');
    assert.ok(flowText.includes('Not encrypted'), 'unencrypted badge missing');

    assert.strictEqual(els['ai-empty']._hidden, true, 'empty state shown on non-empty data');
});

test('empty state: no features/models/data-flow -> empty-state shown, nothing rendered', async () => {
    const { fns, els } = run(() => ({ features: [], models: [], data_flow: [] }));
    await fns.initializeAiInventoryPage();
    await drain();
    assert.strictEqual(els['ai-empty']._hidden, false, 'empty state not shown');
    assert.strictEqual(els['ai-features-body'].children.length, 0, 'features rendered for empty inventory');
    assert.strictEqual(els['ai-models-body'].children.length, 0, 'models rendered for empty inventory');
    assert.strictEqual(els['ai-dataflow-list'].children.length, 0, 'data-flow rendered for empty inventory');
});

test('error state: server detail surfaced verbatim, sections cleared', async () => {
    const { fns, els } = run(() => { throw { status: 500, detail: 'Could not build the AI inventory' }; });
    await fns.initializeAiInventoryPage();
    await drain();
    assert.ok(/Could not build the AI inventory/.test(els['ai-error'].textContent), 'detail missing');
    assert.strictEqual(els['ai-error']._hidden, false, 'error box not shown');
    assert.strictEqual(els['ai-features-body'].children.length, 0, 'features left after error');
    assert.strictEqual(els['ai-models-body'].children.length, 0, 'models left after error');
    assert.strictEqual(els['ai-dataflow-list'].children.length, 0, 'data-flow left after error');
});

test('no innerHTML injection: hostile feature name cannot inject markup', async () => {
    const hostile = freshInventory();
    hostile.features[0].name = '<img src=x onerror=alert(1)>';
    const { fns, els } = run(() => hostile);
    await fns.initializeAiInventoryPage();
    await drain();
    // Every rendered node must never have had innerHTML set — only
    // textContent/appendChild. Confirm no node in the tree ever received
    // a markup string as textContent that a renderer would re-parse: the
    // hostile string must appear as plain text, and the DOM must contain
    // no element whose tag is actually "img" (which innerHTML parsing
    // would have created).
    const sawImgTag = (node) => node.tag === 'img' || node.children.some(sawImgTag);
    assert.ok(!sawImgTag(els['ai-features-body']), 'hostile name parsed into a real <img> element');
    assert.ok(els['ai-features-body'].allText.includes('<img src=x onerror=alert(1)>'),
        'hostile name not present as literal text');
});
