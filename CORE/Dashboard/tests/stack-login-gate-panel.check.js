// 2400GX-n3ib check — persistent-stacks.js "Login gate" panel (GET|PUT
// /stacks/{id}/ingress-auth). No SCIM groups fetch: group names are typed.
//
//   node --test CORE/Dashboard/tests/stack-login-gate-panel.check.js
//
// P1 button renders per row; click → GET /stacks/<id>/ingress-auth; fields
//    filled from the stub reply.
// P2 save → PUT body exactly {require_login:true, allowed_groups:["a","b"],
//    public_paths:["/health"]} from textarea input with blanks/whitespace.
// P3 path `/../x`, `relative`, `/` → no PUT, inline error.
// P4 400 {"detail":"<b>x</b>"} → shown as literal TEXT (no <b> element).
// P5 checkbox off → groups/paths disabled and PUT sends require_login:false.
//
// Technique (as in mobile-devices.check.js): the page is an ES module, so the
// login-gate function bodies are extracted from source text and run against a
// tiny DOM registry + stubbed apiCall. Old-file proof: MCW_OVERRIDE the HEAD
// version of persistent-stacks.js into the run tree and this file throws
// (extraction finds nothing) → P1 fails.
'use strict';
const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

function extract(name) {
    const m = src.match(new RegExp(`(?:export )?(?:async )?function ${name}\\([\\s\\S]*?\\n}`));
    if (!m) throw new Error(`could not extract ${name} from persistent-stacks.js`);
    return m[0].replace(/^export /, '');
}
const NAMES = ['_psEsc', '_psLoginGate', '_psLoginGateLines', '_psLoginGateOut', '_psLoginGateSave'];

// --- static assertions on the new code --------------------------------------
// 036d63b9a (cUI-5 seed upload) replaced the section's first comment line
// ("2400GX-n3ib — \"Login gate\" panel") with its own header; the rest of the
// block survived, so anchor on the line that did not change.
const section = src.slice(src.indexOf('// login gate through the EXISTING GET|PUT'));
const sectionEnd = section.indexOf('function _psDefOut');
const newCode = section.slice(0, sectionEnd < 0 ? section.length : sectionEnd);
test('S1 no console.* in the new code', () => assert.ok(!/console\./.test(newCode), 'no logging allowed'));
test('S2 no SCIM groups fetch', () => {
    const sites = newCode.match(/apiCall\(/g) || [];
    const targets = newCode.match(/apiCall\(`[^`]*`/g) || [];
    assert.strictEqual(sites.length, targets.length, 'every apiCall target is a literal path');
    assert.strictEqual(targets.length, 2, 'GET + PUT only');
    for (const t of targets) assert.ok(/^apiCall\(`\/stacks\/\$\{id\}\/ingress-auth`/.test(t), `off-target call: ${t}`);
    assert.ok(!/scim|\/Groups/i.test(newCode.replace(/^\/\/.*$/gm, '')), 'no groups fetch anywhere outside comments');
});
test('S3 row button is additive and uses data-ps-act', () => {
    // d0aa34d4c (xss g5) coerces the row id to `sid` before interpolating it
    // into innerHTML; the button is the same, just keyed on the coerced id.
    assert.ok(/data-ps-act="login-gate" data-ps-id="\$\{(s\.id|sid)\}"/.test(src), 'row button present');
    assert.ok(/if \(act === 'login-gate'\) return _psLoginGate\(id\);/.test(src), 'dispatch wired');
});
test('S4 every API value into innerHTML is _psEsc-wrapped', () => {
    const lits = newCode.match(/innerHTML = `[\s\S]*?`;/g) || [];
    for (const h of lits) {
        for (const m of h.match(/\$\{[^}]*\}/g) || []) {
            const expr = m.slice(2, -1);
            const safe = /_psEsc\(/.test(expr) || /^\s*(id|requireLogin|groups\.join|paths\.join)/.test(expr)
                || /_psEsc\(id\)/.test(expr);
            assert.ok(safe, `unescaped interpolation ${m} in innerHTML`);
        }
    }
});

// --- fake DOM ---------------------------------------------------------------
const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link']);
const ENT = { lt: '<', gt: '>', amp: '&', quot: '"', '#39': "'" };
function decode(s) {
    return s.replace(/&([a-z]+|#\d+);/g, (m, e) => ({}[e] || ENT[e]) || m);
}
function parseAttrs(s) {
    const a = {};
    for (const m of s.match(/([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|(\S+)))?/g) || []) {
        const q = /^([\w-]+)(?:=(?:"([^"]*)"|'([^']*)'|(\S+)))?$/.exec(m);
        if (!q) continue;
        a[q[1]] = q[2] != null ? q[2] : q[3] != null ? q[3] : q[4] != null ? q[4] : true;
    }
    return a;
}
function mkNode(tag, attrs) {
    const n = { tag, attrs, children: [], _html: '', _val: attrs.value || '', _text: '' };
    n.checked = attrs.checked === true;
    n.disabled = attrs.disabled === true;
    n.className = attrs.class || '';
    Object.defineProperty(n, 'value', { get: () => n._val, set: (v) => { n._val = v; } });
    Object.defineProperty(n, 'textContent', { get: () => n._text, set: (v) => { n._text = v; } });
    return n;
}
function parseAll(html) {
    const out = [];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:\s+[^<>]*?)?)(\/?)>|([^<]+)/g;
    let m;
    while ((m = re.exec(html))) {
        if (m[1] === '/') continue;
        const node = mkNode(m[2], parseAttrs(m[3] || ''));
        out.push(node);
        if (node.tag === 'textarea') {
            const start = re.lastIndex;
            const end = html.indexOf('</textarea>', start);
            node._val = end < 0 ? '' : decode(html.slice(start, end));
            if (end >= 0) re.lastIndex = end + '</textarea>'.length;
        }
    }
    return out;
}
let registry;
const HOST_ID = 'ps-backups-7';
const host = mkNode('tr', { id: HOST_ID });
host.dataset = {};
function resetRegistry() { registry = { [HOST_ID]: host }; }
function setInner(node, html) {
    node._html = html;
    node.children = parseAll(html);
    for (const n of node.children) if (n.attrs.id) registry[n.attrs.id] = n;
}
function getEl(id) { return registry[id] || null; }
const doc = { getElementById: (id) => getEl(id), createElement: (t) => ({ tag: t, attrs: {}, children: [], text: '' }) };
Object.defineProperty(host, 'innerHTML', { set: (v) => { setInner(host, v); }, get: () => host._html });

let calls, apiReply, apiThrow;
function apiCall(endpoint, options = {}) {
    calls.push({ endpoint, method: options.method || 'GET', body: options.body });
    if (apiThrow) { const e = apiThrow; apiThrow = null; throw e; }
    return Promise.resolve(apiReply);
}
function newDoc() { resetRegistry(); host.children = []; host._html = ''; host.dataset = {}; host.text = ''; }
function run() {
    globalThis.document = doc;
    globalThis.apiCall = apiCall;
    const fns = NAMES.map(extract).join('\n') + '\nreturn { ' + NAMES.join(', ') + ' };';
    return new Function(fns)();
}

function openGate(mod, reply, throwErr) {
    newDoc();
    calls = []; apiReply = reply; apiThrow = throwErr;
    return mod._psLoginGate('7');
}
function findNode(tag, act) {
    return host.children.find((n) => n.tag === tag && n.attrs['data-ps-act'] === act);
}

test('P1 button renders per row; click GETs ingress-auth; fields filled', async () => {
    const mod = run();
    await openGate(mod, { require_login: true, allowed_groups: ['admins', 'film-crew'], public_paths: ['/health'] });
    assert.deepStrictEqual(calls, [{ endpoint: '/stacks/7/ingress-auth', method: 'GET', body: undefined }]);
    assert.strictEqual(host.dataset.open, '1');
    assert.strictEqual(host.dataset.openAct, 'login-gate');
    const cb = getEl('ps-lg-req-7');
    assert.ok(cb && cb.checked === true, 'checkbox reflects require_login');
    assert.strictEqual(getEl('ps-lg-groups-7').value, 'admins\nfilm-crew');
    assert.strictEqual(getEl('ps-lg-paths-7').value, '/health');
    assert.ok(findNode('button', 'login-gate-save'), 'save button present');
    assert.ok(findNode('button', 'login-gate-close'), 'close button present');
    // re-clicking the row button closes the panel (neighbour idiom)
    await mod._psLoginGate('7');
    assert.strictEqual(host.children.length, 0);
    assert.strictEqual(host.dataset.open, '');
});

test('P2 save PUT body exact from textarea input with blanks/whitespace', async () => {
    const mod = run();
    await openGate(mod, { require_login: true, allowed_groups: [], public_paths: [] });
    getEl('ps-lg-groups-7').value = ' a ,\n b\n\n';
    getEl('ps-lg-paths-7').value = ' /health \n';
    calls = [];
    await mod._psLoginGateSave('7');
    assert.strictEqual(calls.length, 1);
    assert.strictEqual(calls[0].method, 'PUT');
    assert.strictEqual(calls[0].endpoint, '/stacks/7/ingress-auth');
    assert.deepStrictEqual(JSON.parse(calls[0].body),
        { require_login: true, allowed_groups: ['a', 'b'], public_paths: ['/health'] });
    const out = getEl('ps-lg-out-7');
    assert.strictEqual(out.textContent,
        "Saved. Takes effect on the next ingress reload; redeploy a running stack to apply it now.");
    assert.ok(/text-success/.test(out.className));
});

test('P3 bad paths: no PUT, inline error', async () => {
    const mod = run();
    for (const bad of ['/../x', 'relative', '/', '/*']) {
        await openGate(mod, { require_login: true, allowed_groups: [], public_paths: [] });
        getEl('ps-lg-paths-7').value = bad;
        calls = [];
        await mod._psLoginGateSave('7');
        assert.strictEqual(calls.length, 0, `no PUT for ${bad}`);
        const out = getEl('ps-lg-out-7');
        assert.ok(out.textContent.length > 0, `inline error for ${bad}`);
        assert.ok(/text-destructive/.test(out.className));
        if (bad === '/*' || bad === '/') assert.ok(/use 'Require login' off instead/.test(out.textContent));
        if (bad === 'relative') assert.ok(/must start with \//.test(out.textContent));
        if (bad === '/../x') assert.ok(/must not contain \.\./.test(out.textContent));
    }
});

test('P4 400 detail shown as literal text, no element created', async () => {
    const mod = run();
    await openGate(mod, { require_login: true, allowed_groups: [], public_paths: [] });
    getEl('ps-lg-paths-7').value = '/health';
    calls = [];
    apiThrow = { status: 400, message: '<b>x</b>', detail: '<b>x</b>' };
    await mod._psLoginGateSave('7');
    assert.strictEqual(calls.length, 1, 'PUT was attempted');
    const out = getEl('ps-lg-out-7');
    assert.strictEqual(out.textContent, '<b>x</b>');
    assert.ok(!host.children.some((n) => n.tag === 'b'), 'no <b> element created');
    assert.ok(/text-destructive/.test(out.className));
});

test('P5 checkbox off: groups/paths disabled, PUT require_login:false', async () => {
    const mod = run();
    await openGate(mod, { require_login: false, allowed_groups: ['ignored'], public_paths: [] });
    const cb = getEl('ps-lg-req-7');
    assert.ok(cb && cb.checked === false, 'checkbox off');
    assert.strictEqual(getEl('ps-lg-groups-7').disabled, true, 'groups textarea disabled');
    assert.strictEqual(getEl('ps-lg-paths-7').disabled, true, 'paths textarea disabled');
    calls = [];
    await mod._psLoginGateSave('7');
    assert.strictEqual(calls.length, 1);
    assert.deepStrictEqual(JSON.parse(calls[0].body),
        { require_login: false, allowed_groups: ['ignored'], public_paths: [] });
});

test('P6 denied GET/PUT wording', async () => {
    const mod = run();
    calls = [];
    await openGate(mod, null, { status: 402, message: 'entitlement_required', detail: null });
    const shown = decode(host._html);
    assert.ok(/You can't edit this stack's login gate\./.test(shown), 'generic denial text');
    assert.ok(!/entitlement_required/.test(shown), 'raw server code not shown');
});
