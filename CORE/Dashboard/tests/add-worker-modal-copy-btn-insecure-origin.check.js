'use strict';
// FIX-DASHBOARD-ADD-WORKER-COPY-BUTTON-INSECURE-ORIGIN-CLIPBOARD-FALLBACK (lane
// ) - the Workers -> Add Worker modal's Copy buttons (Linux /
// Windows / macOS setup command) used an inline onclick calling
// navigator.clipboard.writeText directly. The async Clipboard API is only
// exposed in secure contexts (https or localhost), so over plain http on a LAN
// IP (first-class CE LAN access, e.g. http://203.0.113.90:3000) navigator.clipboard
// is undefined, the handler throws, and the button silently does nothing.
//
// Technique (as add-worker-modal-lan-url.check.js): the shipped helper block is
// extracted VERBATIM from js/workers.js and run in a Function sandbox with stub
// window / navigator / document; the call sites are checked at the source level.
//
//   node CORE/Dashboard/tests/add-worker-modal-copy-btn-insecure-origin.check.js
//
// Asserts:
//   A1 insecure origin (isSecureContext=false, no navigator.clipboard): the
//      hidden textarea + document.execCommand('copy') path runs and the button
//      flips to 'Copied!'
//   A2 secure origin (isSecureContext=true, clipboard.writeText present):
//      writeText is called with the exact text and execCommand is NOT called
//   A3 both paths fail -> returns false and the button shows the visible
//      'Copy failed: select and copy manually' feedback (never silent)
//   A4 the textarea is created, selected, copied and removed (no leftover node)
//   A5 what is copied is unchanged: the element's textContent is passed through
//   A6 shipped source wiring: helper block present, the three buttons call
//      window.__copySetupCmd, no inline navigator.clipboard onclick remains
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');

const bStart = src.indexOf('// FIX-DASHBOARD-COPY-BTN-INSECURE-ORIGIN (11-17) BEGIN');
const bEnd = src.indexOf('// FIX-DASHBOARD-COPY-BTN-INSECURE-ORIGIN END', bStart);
const helperBlock = (bStart >= 0 && bEnd > bStart) ? src.slice(bStart, bEnd) : '';
const MISSING_HELPER = 'helper block BEGIN/END marker not found in workers.js (RED: fix not shipped yet)';

function buildStubs(window, navigator, document) {
    return new Function('window', 'navigator', 'document',
        helperBlock + '\nreturn { copyTextToClipboard, copySetupCmd };'
    )(window, navigator, document);
}

function makeBtn(classes) {
    const cl = { classes: classes.slice() };
    cl.replace = (a, b) => { const i = cl.classes.indexOf(a); if (i >= 0) cl.classes[i] = b; else cl.classes.push(b); };
    return { textContent: 'Copy', classList: cl };
}

function makeDoc(byId, execImpl) {
    const calls = { exec: 0, select: 0, append: 0, remove: 0 };
    const body = {
        children: [],
        appendChild: (n) => { calls.append++; body.children.push(n); },
        removeChild: (n) => { calls.remove++; body.children = body.children.filter((x) => x !== n); },
    };
    const doc = {
        getElementById: (id) => byId[id],
        createElement: (tag) => ({ tag, value: '', style: {}, select: () => { calls.select++; }, textContent: '' }),
        execCommand: (cmd) => { calls.exec++; return execImpl(cmd); },
        body: body,
    };
    return { doc, calls };
}

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

record('A1 insecure origin -> execCommand fallback runs, button says Copied!', async () => {
    const cmd = 'curl -fsSL "http://203.0.113.90:8000/v1/workers/setup-script" | sudo bash';
    const el = { textContent: cmd };
    const btn = makeBtn(['bg-primary/80', 'text-white']);
    const st = makeDoc({ 'setup-cmd-text': el, 'copy-cmd-btn': btn }, () => true);
    const w = { isSecureContext: false, location: { hostname: '203.0.113.90' } };
    const nav = {}; // insecure origin: navigator.clipboard is not exposed
    const h = buildStubs(w, nav, st.doc);
    const ok = await h.copySetupCmd('linux');
    assert.strictEqual(ok, true, 'A1 fallback copy succeeded');
    assert.ok(st.calls.exec >= 1, 'A1 document.execCommand(copy) ran');
    assert.strictEqual(btn.textContent, 'Copied!', 'A1 visible Copied! feedback');
    assert.ok(btn.classList.classes.includes('bg-green-500'), 'A1 button turns green');
});

record('A2 secure origin -> navigator.clipboard.writeText only, no execCommand', async () => {
    const cmd = 'curl -fsSL "http://localhost:8000/v1/workers/setup-script" | sudo bash';
    const el = { textContent: cmd };
    const btn = makeBtn(['bg-primary/80']);
    const st = makeDoc({ 'setup-cmd-text': el, 'copy-cmd-btn': btn }, () => true);
    const copied = [];
    const nav = { clipboard: { writeText: (t) => { copied.push(t); return true; } } };
    const w = { isSecureContext: true, location: { hostname: 'localhost' } };
    const h = buildStubs(w, nav, st.doc);
    const ok = await h.copySetupCmd('linux');
    assert.strictEqual(ok, true, 'A2 secure copy succeeded');
    assert.deepStrictEqual(copied, [cmd], 'A2 writeText got the exact element text');
    assert.strictEqual(st.calls.exec, 0, 'A2 execCommand NOT called on a secure origin');
    assert.strictEqual(btn.textContent, 'Copied!', 'A2 Copied! feedback');
});

record('A3 both paths fail -> false + visible Copy failed feedback', async () => {
    const el = { textContent: 'mod-worker-embedded --api-url http://203.0.113.90:8000/v1' };
    const btn = makeBtn(['bg-primary/80']);
    const st = makeDoc({ 'setup-cmd-text-windows': el, 'copy-cmd-btn-windows': btn },
        (cmd) => { throw new Error('execCommand copy unavailable'); });
    const nav = { clipboard: { writeText: (t) => { throw new Error('writeText rejected'); } } };
    const w = { isSecureContext: true };
    const h = buildStubs(w, nav, st.doc);
    const ok = await h.copySetupCmd('windows');
    assert.strictEqual(ok, false, 'A3 reports failure');
    assert.strictEqual(btn.textContent, 'Copy failed: select and copy manually', 'A3 visible failure text');
    assert.ok(btn.classList.classes.includes('bg-red-500'), 'A3 button turns red');
});

record('A4 textarea lifecycle: created, selected, copied, removed', async () => {
    const el = { textContent: 'curl -fsSL "http://203.0.113.90:8000/v1/workers/setup-script" | sudo bash' };
    const btn = makeBtn(['bg-primary/80']);
    const st = makeDoc({ 'setup-cmd-text-macos': el, 'copy-cmd-btn-macos': btn }, () => true);
    const h = buildStubs({ isSecureContext: false }, {}, st.doc);
    const ok = await h.copySetupCmd('macos');
    assert.strictEqual(ok, true, 'A4 legacy copy succeeded');
    assert.strictEqual(st.calls.select, 1, 'A4 textarea selected once');
    assert.strictEqual(st.calls.exec, 1, 'A4 copied once');
    assert.strictEqual(st.calls.append, 1, 'A4 appended once');
    assert.strictEqual(st.calls.remove, 1, 'A4 removed once (no leftover node)');
    assert.strictEqual(btn.textContent, 'Copied!', 'A4 Copied! feedback');
});

record('A5 what is copied is unchanged', async () => {
    const cmd = 'curl -fsSL "$URL/workers/setup-script" | sudo ENROLLMENT_TOKEN bash';
    const el = { textContent: cmd };
    const btn = makeBtn([]);
    const st = makeDoc({ 'setup-cmd-text': el, 'copy-cmd-btn': btn }, () => true);
    const h = buildStubs({ isSecureContext: false }, {}, st.doc);
    const ok = await h.copySetupCmd('linux');
    assert.strictEqual(ok, true, 'A5 copied');
    assert.strictEqual(el.textContent, cmd, 'A5 element text untouched');
    assert.strictEqual(btn.textContent, 'Copied!', 'A5 feedback only on the button');
});

record('A6 shipped source wiring', () => {
    assert.ok(/async function copyTextToClipboard\(text\)/.test(src), 'A6 helper defined');
    assert.ok(/async function copySetupCmd\(which\)/.test(src), 'A6 copySetupCmd defined');
    assert.ok(src.includes('window.__copySetupCmd = (which) => copySetupCmd(which);'), 'A6 exposed on window');
    const wired = (src.match(/onclick="window\.__copySetupCmd\('/g) || []).length;
    assert.strictEqual(wired, 3, 'A6 all three modal Copy buttons call the helper');
    const leftover = (src.match(/onclick="navigator\.clipboard/g) || []).length;
    assert.strictEqual(leftover, 0, 'A6 no inline navigator.clipboard onclick remains in workers.js');
    ['copy-cmd-btn-windows', 'copy-cmd-btn-macos', 'copy-cmd-btn'].forEach((i) =>
        assert.ok(src.includes('id="' + i + '"'), 'A6 button ' + i + ' still present'));
});

(async () => {
    let passed = 0, failed = 0;
    for (const c of CASES) {
        try { await c.fn(); passed++; console.log('PASS ' + c.name); }
        catch (e) { failed++; console.log('FAIL ' + c.name + ': ' + e.message); }
    }
    console.log('add-worker-modal-copy-btn-insecure-origin: ' + passed + ' passed / ' + failed + ' failed');
    process.exit(failed === 0 ? 0 : 1);
})();
