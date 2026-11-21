// Embed run page (public run link): behaviour checks for js/embed-run.js.
// (FIX-DASH-TIER2-STACK-BILLING-AND-EMBED-RUN-JEST-TESTS)
//
// embed-run.js is a self-executing IIFE with no exports. It is run in a vm
// sandbox with a minimal DOM stub (createElement/innerHTML/firstChild,
// appendChild, addEventListener) and a stubbed fetch. The module is not
// modified. Behaviours asserted:
//   (a) missing ?t= token -> "Missing run link token." and no request;
//   (b) describe request goes to the api host (dashboard -> api) with the token;
//   (c) 404 -> "This run link is no longer available."; other non-2xx ->
//       "This run link is unavailable."; network failure -> "Could not reach";
//   (d) password-protected app shows the gate; the label is HTML-escaped;
//   (e) empty password sends nothing; wrong password re-shows the gate with
//       "Incorrect password — try again." and sends X-Run-Password.
// The correct-password path calls render(), which needs a full DOM; it is not
// covered here (see the result file).
//
//   node --test CORE/Dashboard/tests/embed-run-page.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const test = require('node:test');
const { URLSearchParams } = require('url');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'embed-run.js'), 'utf8');

function makeNode(tag, html) {
    const n = {
        tag, children: [], listeners: {}, value: '', disabled: false,
        textContent: '', onclick: null, dataset: {}, _html: html || '',
    };
    Object.defineProperty(n, 'innerHTML', {
        get() { return this._html; },
        set(v) { this._html = String(v); this.children = []; },
    });
    Object.defineProperty(n, 'firstChild', {
        get() { return makeNode('node', this._html); },
    });
    n.appendChild = (c) => { n.children.push(c); return c; };
    n.addEventListener = (type, fn) => { n.listeners[type] = fn; };
    n.querySelectorAll = () => [];
    n.classList = { add() {}, remove() {}, toggle() {} };
    return n;
}

function findById(node, id) {
    return node.children.find((c) => (c._html || '').includes('id="' + id + '"')) || null;
}

// Boots the IIFE. respond(url, opts, n) returns the fetch promise for call n.
function boot(search, respond) {
    const app = makeNode('div', '');
    const calls = [];
    const doc = {
        documentElement: { dataset: {} },
        getElementById: (id) => (id === 'app' ? app : findById(app, id)),
        createElement: (tag) => makeNode(tag, ''),
    };
    const sandbox = {
        location: { search, origin: 'https://dashboard.example.test' },
        URLSearchParams,
        document: doc,
        console: { log() {}, error() {} },
        fetch: (url, opts) => {
            calls.push({ url, opts: opts || {} });
            return respond(url, opts || {}, calls.length);
        },
    };
    vm.runInNewContext(src, sandbox);
    return { app, calls, doc };
}

const resp = (status, body) => Promise.resolve({
    status, ok: status >= 200 && status < 300, json: async () => body || {},
});
const flush = () => new Promise((r) => setTimeout(r, 0));
const blockedText = (app) => app._html;

test('missing token shows the missing-link copy and sends no request', async () => {
    const h = boot('', () => resp(200, {}));
    await flush();
    assert.ok(blockedText(h.app).includes('Missing run link token.'));
    assert.ok(blockedText(h.app).includes('class="blocked"'));
    assert.strictEqual(h.calls.length, 0);
});

test('describe request goes to the api host with the token and no password header', async () => {
    const h = boot('?t=tok123', () => resp(404, {}));
    await flush();
    assert.strictEqual(h.calls.length, 1);
    assert.strictEqual(h.calls[0].url, 'https://api.example.test/embed/run/tok123');
    assert.strictEqual(h.calls[0].opts.headers, undefined);
});

test('404 shows the no-longer-available copy', async () => {
    const h = boot('?t=tok123', () => resp(404, {}));
    await flush();
    assert.ok(blockedText(h.app).includes('This run link is no longer available.'));
});

test('other non-2xx shows the generic unavailable copy', async () => {
    const h = boot('?t=tok123', () => resp(500, {}));
    await flush();
    assert.ok(blockedText(h.app).includes('This run link is unavailable.'));
    assert.ok(!blockedText(h.app).includes('no longer available'));
});

test('network failure shows the could-not-reach copy', async () => {
    const h = boot('?t=tok123', () => Promise.reject(new Error('offline')));
    await flush();
    assert.ok(blockedText(h.app).includes('Could not reach the service.'));
});

test('password-protected app shows the gate with an escaped label', async () => {
    const desc = { requires_password: true, unlocked: false,
        label: '<b>"x"&\'</b>' };
    const h = boot('?t=tok123', () => resp(200, desc));
    await flush();
    const h1 = h.app.children[0];
    assert.ok(h1._html.includes('&lt;b&gt;&quot;x&quot;&amp;&#39;&lt;/b&gt;'),
        'label must be escaped, got: ' + h1._html);
    assert.ok(!h1._html.includes('<b>'), 'raw label markup must not be emitted');
    assert.ok(h.app.children.some((c) => c._html.includes('This app is password protected.')));
    assert.ok(h.app.children.some((c) => c._html.includes('id="unlock"')));
});

test('gate with no label falls back to "Protected app"', async () => {
    const h = boot('?t=tok123', () => resp(200, { requires_password: true, unlocked: false }));
    await flush();
    assert.ok(h.app.children[0]._html.includes('Protected app'));
});

test('empty password sends nothing', async () => {
    const h = boot('?t=tok123', () => resp(200, { requires_password: true, unlocked: false }));
    await flush();
    const btn = h.app.children.find((c) => c._html.includes('id="unlock"'));
    btn.onclick();
    await flush();
    assert.strictEqual(h.calls.length, 1, 'no password check request for an empty field');
});

test('wrong password re-shows the gate and sends X-Run-Password', async () => {
    const h = boot('?t=tok123', () => resp(200, { requires_password: true, unlocked: false }));
    await flush();
    h.doc.getElementById('pw').value = 'wrong-pw';
    const btn = h.app.children.find((c) => c._html.includes('id="unlock"'));
    btn.onclick();
    await flush();
    assert.strictEqual(h.calls.length, 2);
    assert.strictEqual(h.calls[1].opts.headers['X-Run-Password'], 'wrong-pw');
    assert.ok(h.app.children.some((c) => c._html.includes('Incorrect password — try again.')));
    assert.ok(h.app.children.some((c) => c._html.includes('id="unlock"')), 'gate is shown again');
});
