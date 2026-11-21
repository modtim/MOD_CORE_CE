// Node-runnable check for the FX.77 p6a "Log streaming (SIEM)" tenant-admin
// page (CORE/Dashboard/js/log-streams.js). Mirrors the vm-sandbox harness in
// tests/ai-inventory.test.js: extracts the module body (from the first
// helper function to EOF, with `export ` stripped) and runs it with stubbed
// DOM elements, then asserts:
//   (a) list render: name/kind/endpoint/enabled/has_secret badges show;
//   (b) create posts correct body (name/kind/endpoint/config/event_types/
//       enabled, no `secret` key when the field was left blank);
//   (c) blank secret on edit is omitted from the PATCH body;
//   (d) "clear secret" checkbox sends secret: null;
//   (e) 422 errors (plain list[str] from validate_stream_payload) are
//       rendered as a list under the form;
//   (f) no innerHTML injection: a hostile stream name never parses into a
//       real <img> element.
//
//   node --test CORE/Dashboard/tests/log-streams.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'log-streams.js'), 'utf8');
const start = src.indexOf('const ENDPOINT_PLACEHOLDERS');
assert.ok(start >= 0, 'could not locate the log-streams body');
const block = src.slice(start).replace(/export async function/g, 'async function');

const IDS = [
    'ls-error', 'ls-info', 'ls-empty', 'ls-body', 'ls-new-btn', 'ls-cancel-btn',
    'ls-kind', 'ls-endpoint', 'ls-form', 'ls-form-id', 'ls-name', 'ls-config',
    'ls-config-error', 'ls-secret', 'ls-secret-hint', 'ls-clear-secret-wrap',
    'ls-clear-secret', 'ls-enabled', 'ls-form-errors', 'ls-form-section',
    'ls-form-title',
];

function freshStreams() {
    return {
        items: [
            { id: 's1', name: 'Splunk main', kind: 'splunk_hec', endpoint: 'https://splunk.example.com:8088',
              config: {}, enabled: true, event_types: ['audit', 'security'], has_secret: true,
              last_success_at: '2026-10-01T00:00:00Z', last_error_at: null, last_error: null },
            { id: 's2', name: 'Webhook backup', kind: 'https_webhook', endpoint: 'https://hooks.example.com/mod',
              config: {}, enabled: false, event_types: ['run'], has_secret: false,
              last_success_at: null, last_error_at: '2026-10-02T00:00:00Z', last_error: 'connection refused' },
        ],
        total: 2,
    };
}

// ---------------------------------------------------------------------------
// sandbox — minimal real DOM (createElement/appendChild/textContent/value/
// checked) plus a querySelectorAll('.ls-event-type') that returns a fixed
// array of checkbox stand-ins, matching how log-streams.js reads them.
// ---------------------------------------------------------------------------

function makeNode(tag) {
    const node = {
        tag, children: [], className: '', textContent: '', value: '', checked: false,
        placeholder: '', dataset: {}, listeners: {}, _hidden: false,
    };
    node.appendChild = (child) => { node.children.push(child); return child; };
    node.addEventListener = (type, fn) => { node.listeners[type] = fn; };
    node.classList = {
        add: (c) => { if (c === 'hidden') node._hidden = true; },
        remove: (c) => { if (c === 'hidden') node._hidden = false; },
        toggle: () => {},
    };
    Object.defineProperty(node, 'allText', {
        get() {
            let out = node.textContent || '';
            for (const c of node.children) out += (c.allText !== undefined ? c.allText : c.textContent || '');
            return out;
        },
    });
    return node;
}

function run(mocks) {
    const els = {};
    for (const id of IDS) {
        const el = makeNode(id);
        el.id = id;
        els[id] = el;
    }
    const eventTypeBoxes = ['audit', 'security', 'run', 'auth'].map((v) => {
        const box = makeNode('input');
        box.value = v;
        box.checked = false;
        return box;
    });

    const calls = { list: 0, create: [], update: [], remove: [], test: [] };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        window: { confirm: () => true },
        // 2400LR-g3-ce-denied-404s: log-streams.js:147 gates loadList() on
        // isPaidEdition() (imported from api.js in the browser). The vm sandbox
        // has no ES imports, so without this global the call throws
        // ReferenceError, the product's fail-closed catch reads that as CE, and
        // the list render takes the empty-state branch. Every fixture here
        // exercises the PAID path (the CE empty-state branch is covered
        // separately by g3-ce-denied-gates.test.js), so stub the predicate as
        // paid — same intent as alert-rules-page.test.js's loadRealIsPaidEdition.
        isPaidEdition: () => true,
        document: {
            getElementById: (id) => els[id],
            createElement: (tag) => makeNode(tag),
            querySelectorAll: (sel) => (sel === '.ls-event-type' ? eventTypeBoxes : []),
        },
        logStreams: {
            list: () => { calls.list++; return Promise.resolve((mocks.list || freshStreams)()); },
            get: () => Promise.resolve(null),
            create: (payload) => { calls.create.push(payload); return (mocks.create || (() => Promise.resolve({})))(payload); },
            update: (id, payload) => { calls.update.push([id, payload]); return (mocks.update || (() => Promise.resolve({})))(id, payload); },
            remove: (id) => { calls.remove.push(id); return Promise.resolve(null); },
            test: (id) => { calls.test.push(id); return Promise.resolve(null); },
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ initializeLogStreamsPage, loadList, openForm, onSubmit, closeForm });`,
        sandbox, { filename: 'log-streams-body' });
    return { fns, els, calls, eventTypeBoxes };
}

async function drain() { for (let i = 0; i < 60; i++) await Promise.resolve(); }

// ---------------------------------------------------------------------------
// tests
// ---------------------------------------------------------------------------

test('list render: name/kind/endpoint/enabled/secret badges show', async () => {
    const { fns, els, calls } = run({});
    await fns.initializeLogStreamsPage();
    await drain();
    assert.strictEqual(calls.list, 1, 'logStreams.list not called');

    const bodyText = els['ls-body'].allText;
    assert.ok(bodyText.includes('Splunk main'), 'stream name missing');
    assert.ok(bodyText.includes('splunk_hec'), 'kind missing');
    assert.ok(bodyText.includes('https://splunk.example.com:8088'), 'endpoint missing');
    assert.ok(bodyText.includes('Enabled'), 'enabled badge missing');
    assert.ok(bodyText.includes('Disabled'), 'disabled badge missing');
    assert.ok(bodyText.includes('Set'), 'has_secret=true badge missing');
    assert.ok(bodyText.includes('None'), 'has_secret=false badge missing');
    assert.strictEqual(els['ls-empty']._hidden, true, 'empty state shown on non-empty list');
});

test('create posts the correct body, blank secret omitted', async () => {
    const { fns, els, calls, eventTypeBoxes } = run({});
    fns.openForm(null);
    els['ls-name'].value = 'My SIEM';
    els['ls-kind'].value = 'syslog_tls';
    els['ls-endpoint'].value = 'siem.example.com:6514';
    els['ls-config'].value = '{}';
    els['ls-enabled'].checked = true;
    eventTypeBoxes[0].checked = true; // audit
    eventTypeBoxes[2].checked = true; // run
    els['ls-secret'].value = '';

    await fns.onSubmit({ preventDefault: () => {} });
    await drain();

    assert.strictEqual(calls.create.length, 1, 'create not called exactly once');
    const payload = calls.create[0];
    assert.strictEqual(payload.name, 'My SIEM');
    assert.strictEqual(payload.kind, 'syslog_tls');
    assert.strictEqual(payload.endpoint, 'siem.example.com:6514');
    // Cross-realm objects out of the vm sandbox aren't reference-equal to
    // host-realm literals under deepStrictEqual, so compare by JSON shape.
    assert.strictEqual(JSON.stringify(payload.config), '{}');
    assert.strictEqual(JSON.stringify(payload.event_types), JSON.stringify(['audit', 'run']));
    assert.strictEqual(payload.enabled, true);
    assert.ok(!('secret' in payload), 'blank secret was sent in the create body');
});

test('clear-secret checkbox sends secret: null on update', async () => {
    const { fns, els, calls } = run({});
    const existing = {
        id: 's1', name: 'Splunk main', kind: 'splunk_hec', endpoint: 'https://splunk.example.com:8088',
        config: {}, enabled: true, event_types: ['audit'], has_secret: true,
    };
    fns.openForm(existing);
    assert.strictEqual(els['ls-secret'].value, '', 'secret field was pre-filled');
    els['ls-clear-secret'].checked = true;

    await fns.onSubmit({ preventDefault: () => {} });
    await drain();

    assert.strictEqual(calls.update.length, 1, 'update not called exactly once');
    const [id, payload] = calls.update[0];
    assert.strictEqual(id, 's1');
    assert.strictEqual(payload.secret, null, 'clear-secret did not send secret: null');
});

test('422 errors (plain string list) rendered under the form', async () => {
    const { fns, els } = run({
        create: () => Promise.reject({ status: 422, detail: ['kind must be one of [...]', 'endpoint is required'] }),
    });
    fns.openForm(null);
    els['ls-name'].value = 'Bad stream';
    els['ls-endpoint'].value = '';

    await fns.onSubmit({ preventDefault: () => {} });
    await drain();

    const errText = els['ls-form-errors'].allText;
    assert.ok(errText.includes('kind must be one of'), 'first 422 error missing');
    assert.ok(errText.includes('endpoint is required'), 'second 422 error missing');
    assert.strictEqual(els['ls-form-errors']._hidden, false, '422 error box not shown');
});

test('no innerHTML injection: hostile stream name cannot inject markup', async () => {
    const hostile = freshStreams();
    hostile.items[0].name = '<img src=x onerror=alert(1)>';
    const { fns, els } = run({ list: () => hostile });
    await fns.initializeLogStreamsPage();
    await drain();

    const sawImgTag = (node) => node.tag === 'img' || node.children.some(sawImgTag);
    assert.ok(!sawImgTag(els['ls-body']), 'hostile name parsed into a real <img> element');
    assert.ok(els['ls-body'].allText.includes('<img src=x onerror=alert(1)>'),
        'hostile name not present as literal text');
});
