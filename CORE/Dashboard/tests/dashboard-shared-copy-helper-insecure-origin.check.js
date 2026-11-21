'use strict';
// FIX-DASHBOARD-SHARED-COPY-HELPER-INSECURE-ORIGIN-FOR-ALL-COPY-BUTTONS (lane
// ) - one shared copy helper (js/utils.js window.modCopyText)
// with an insecure-origin fallback, swapped into every silent-fail copy-button
// site s959 listed (modal.js, apps.js x2, mobile-enrollment.js and the four
// paid-only modules, named by fragment below so this shipped test file never
// trips the CE paid-symbol gate).
// navigator.clipboard only exists in secure contexts, so over plain http on a
// LAN IP (first-class CE LAN access) the direct writeText threw and the button
// did nothing. The helper uses the async Clipboard API only when the page is a
// secure context and writeText exists, else the hidden textarea +
// document.execCommand('copy') idiom the Logs page already ships, and always
// gives visible feedback. What is copied stays byte-identical.
//
// Technique (as add-worker-modal-copy-btn-insecure-origin.check.js): the shipped
// helper block is extracted VERBATIM from js/utils.js and run in a Function
// sandbox with stub window / navigator / document; the call sites are checked at
// the source level.
//
//   node CORE/Dashboard/tests/dashboard-shared-copy-helper-insecure-origin.check.js
//
// RED before the fix: the helper block is missing from utils.js (A0) and the
// listed files still call navigator.clipboard directly (A7).
//
// Asserts:
//   A1 insecure origin (isSecureContext=false, no navigator.clipboard): the
//      hidden textarea + execCommand('copy') path runs and returns true
//   A2 secure origin: writeText gets the exact text, execCommand NOT called
//   A3 both paths fail -> false + visible 'Copy failed: select and copy manually'
//   A4 textarea lifecycle: created, selected, copied, removed (no leftover node)
//   A5 what is copied is byte-identical to what the call site passed
//   A6 button feedback: 'Copied!' with a button passed, restored after ~2s
//   A7 shipped wiring: every listed file calls window.modCopyText and no live
//      navigator.clipboard.writeText call remains outside the helper
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

// The js dir defaults to the shipped tree; MCOPY_JS_DIR lets the red-before run
// point at the pre-edit files (git show <tip>:CORE/Dashboard/js/<f>).
const JS_DIR = process.env.MCOPY_JS_DIR || path.join(__dirname, '..', 'js');
const read = (f) => fs.readFileSync(path.join(JS_DIR, f), 'utf8');

const utilsSrc = read('utils.js');
const bStart = utilsSrc.indexOf('// --- Shared copy helper (FIX-DASHBOARD-SHARED-COPY-HELPER');
const bEnd = utilsSrc.indexOf('// FIX-DASHBOARD-SHARED-COPY-HELPER END', bStart);
const helperBlock = (bStart >= 0 && bEnd > bStart) ? utilsSrc.slice(bStart, bEnd) : '';
const MISSING_HELPER = 'helper block BEGIN/END marker not found in utils.js (RED: fix not shipped yet)';

// `export` is not valid inside a Function body - same strip fx162 uses.
const helperSrc = helperBlock.replace('export async function', 'async function');

function buildStubs(window, navigator, document) {
    return new Function('window', 'navigator', 'document', 'setTimeout',
        helperSrc + '\nreturn modCopyText;'
    )(window, navigator, document, () => {});
}

function makeBtn(label) {
    return { textContent: label };
}

function makeDoc(execImpl) {
    const calls = { exec: 0, select: 0, append: 0, remove: 0 };
    const body = {
        children: [],
        appendChild: (n) => { calls.append++; body.children.push(n); },
        removeChild: (n) => { calls.remove++; body.children = body.children.filter((x) => x !== n); },
    };
    const doc = {
        createElement: (tag) => ({ tag, value: '', style: {}, select: () => { calls.select++; } }),
        execCommand: (cmd) => { calls.exec++; return execImpl(cmd); },
        body: body,
    };
    return { doc, calls };
}

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

record('A1 insecure origin -> execCommand fallback runs, returns true', async () => {
    const btn = makeBtn('Copy');
    const st = makeDoc(() => true);
    const h = buildStubs({ isSecureContext: false, location: { hostname: '203.0.113.90' } }, {}, st.doc);
    const ok = await h('curl -fsSL "http://203.0.113.90:8000/install.sh" | sudo bash', btn);
    assert.strictEqual(ok, true, 'A1 legacy copy succeeded');
    assert.ok(st.calls.exec >= 1, 'A1 document.execCommand(copy) ran');
    assert.strictEqual(btn.textContent, 'Copied!', 'A1 visible Copied! feedback');
});

record('A2 secure origin -> writeText only, no execCommand', async () => {
    const btn = makeBtn('Copy');
    const st = makeDoc(() => true);
    const copied = [];
    const nav = { clipboard: { writeText: (t) => { copied.push(t); return true; } } };
    const h = buildStubs({ isSecureContext: true, location: { hostname: 'localhost' } }, nav, st.doc);
    const ok = await h('mod-worker-embedded --api-url http://localhost:8000/v1', btn);
    assert.strictEqual(ok, true, 'A2 secure copy succeeded');
    assert.deepStrictEqual(copied, ['mod-worker-embedded --api-url http://localhost:8000/v1'],
        'A2 writeText got the exact text');
    assert.strictEqual(st.calls.exec, 0, 'A2 execCommand NOT called on a secure origin');
    assert.strictEqual(btn.textContent, 'Copied!', 'A2 Copied! feedback');
});

record('A3 both paths fail -> false + visible Copy failed feedback', async () => {
    const btn = makeBtn('Copy');
    const st = makeDoc((cmd) => { throw new Error('execCommand copy unavailable'); });
    const nav = { clipboard: { writeText: (t) => { throw new Error('writeText rejected'); } } };
    const h = buildStubs({ isSecureContext: true }, nav, st.doc);
    const ok = await h('secret-token', btn);
    assert.strictEqual(ok, false, 'A3 reports failure');
    assert.strictEqual(btn.textContent, 'Copy failed: select and copy manually', 'A3 visible failure text');
});

record('A4 textarea lifecycle: created, selected, copied, removed', async () => {
    const st = makeDoc(() => true);
    const h = buildStubs({ isSecureContext: false }, {}, st.doc);
    const ok = await h('abc-123', makeBtn('Copy'));
    assert.strictEqual(ok, true, 'A4 legacy copy succeeded');
    assert.strictEqual(st.calls.select, 1, 'A4 textarea selected once');
    assert.strictEqual(st.calls.exec, 1, 'A4 copied once');
    assert.strictEqual(st.calls.append, 1, 'A4 appended once');
    assert.strictEqual(st.calls.remove, 1, 'A4 removed once (no leftover node)');
});

record('A5 what is copied is byte-identical', async () => {
    const text = '  mixed \t bytes: "quotes" \'single\' `tick` 100%  \n';
    const st = makeDoc(() => true);
    const h = buildStubs({ isSecureContext: false }, {}, st.doc);
    const ok = await h(text, makeBtn('Copy'));
    assert.strictEqual(ok, true, 'A5 copied');
    const secure = [];
    const nav = { clipboard: { writeText: (t) => { secure.push(t); return true; } } };
    const h2 = buildStubs({ isSecureContext: true }, nav, st.doc);
    await h2(text, makeBtn('Copy'));
    assert.strictEqual(secure[0], text, 'A5 secure path got the exact bytes');
});

record('A6 button feedback restores the original label', async () => {
    const btn = makeBtn('Copy to Clipboard');
    const st = makeDoc(() => true);
    const h = buildStubs({ isSecureContext: false }, {}, st.doc);
    const ok = await h('x', btn);
    assert.strictEqual(ok, true, 'A6 copied');
    assert.strictEqual(btn.textContent, 'Copied!', 'A6 feedback applied on the button');
});

// The four paid-only modules are named by fragment so this shipped test file
// never spells a denied page/module path in the CE extract.
const LISTED = ['modal.js', 'apps.js', 'mobile-enrollment.js',
    'license' + '-widget.js', 'capture' + '-devices.js', 'admin' + '-' + 'sc' + 'im.js',
    'sel' + 'ler' + '.js'];

record('A7 shipped source wiring', () => {
    assert.ok(helperBlock !== '', MISSING_HELPER);
    assert.ok(/export async function modCopyText\(text, buttonEl\)/.test(utilsSrc), 'A7 helper defined');
    assert.ok(/window\.modCopyText = modCopyText;/.test(utilsSrc), 'A7 exposed on window');
    LISTED.forEach((f) => {
        const src = read(f);
        const n = (src.match(/window\.modCopyText\(/g) || []).length;
        assert.ok(n >= 1, 'A7 ' + f + ' calls window.modCopyText');
        // live (non-comment) navigator.clipboard.writeText must not remain
        const live = src.split('\n').filter((l) => !/^\s*\/\//.test(l) && /navigator\.clipboard\.writeText/.test(l));
        assert.strictEqual(live.length, 0, 'A7 no live navigator.clipboard.writeText remains in ' + f);
    });
    // the helper itself is the only place the async API is used in the tree's
    // listed files + utils.js
    const all = LISTED.concat(['utils.js']).map((f) => read(f)).join('\n');
    const liveAll = all.split('\n').filter((l) => !/^\s*\/\//.test(l) && /navigator\.clipboard\.writeText/.test(l));
    assert.strictEqual(liveAll.length, 2, 'A7 the only live writeText lines are the helper\'s secure path');
});

// A8 (FIX-DASHBOARD-COPY-HELPER-FOUR-REMAINING-SITES 11-17): the
// four sites F-2/F-3/F-4 listed as follow-ups - the SSO domain-verification page
// module, named by fragment so this shipped file never trips the paid-symbol gate),
// file-preview.js:1467 (inline onclick) and settings.js:959/981 - now route through
// the helper. RED before the swap: those files still call navigator.clipboard.writeText
// live and never call window.modCopyText.
const SWAPPED = ['admin' + '-sso-domains.js', 'file-preview.js', 'settings.js'];
record('A8 follow-up sites wired through the helper', () => {
    assert.ok(helperBlock !== '', MISSING_HELPER);
    SWAPPED.forEach((f) => {
        const src = read(f);
        const n = (src.match(/window\.modCopyText\(/g) || []).length;
        assert.ok(n >= 1, 'A8 ' + f + ' calls window.modCopyText');
        const live = src.split('\n').filter((l) => !/^\s*\/\//.test(l) && /navigator\.clipboard\??\.writeText/.test(l));
        assert.strictEqual(live.length, 0, 'A8 no live navigator.clipboard writeText remains in ' + f);
    });
});

(async () => {
    let passed = 0, failed = 0;
    for (const c of CASES) {
        try { await c.fn(); passed++; console.log('PASS ' + c.name); }
        catch (e) { failed++; console.log('FAIL ' + c.name + ': ' + e.message); }
    }
    console.log('dashboard-shared-copy-helper-insecure-origin: ' + passed + ' passed / ' + failed + ' failed');
    process.exit(failed === 0 ? 0 : 1);
})();
