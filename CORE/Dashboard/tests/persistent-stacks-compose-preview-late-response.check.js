// Node-runnable check: a Preview response that lands AFTER Create finished
// must not overwrite the "Created stack ..." line in #ps-compose-result
// (FIX-COMPOSE-PREVIEW; the user-visible half of the root cause f860f30768
// found in dash-stacks-compose-import).
// js/persistent-stacks.js paints #ps-compose-result from BOTH _psComposePreviewClick
// (after its await) and the create onCreated. A slow preview POST + a Create
// click means the preview reply arrives AFTER the create reply and repaints
// the element with "Parsed N service(s) ..." - the stack IS created, the
// created message is lost. This script extracts the real _psComposeResult,
// the sequence state, _psComposePreviewClick and _psComposeCreateClick from
// the source text and drives them with a fake DOM and a controllable apiCall:
// Preview click, Create click (completes first), then deliver the delayed
// Preview reply. RED on the pre-fix JS (the created text is overwritten),
// green after the fix.
//
//   node CORE/Dashboard/tests/persistent-stacks-compose-preview-late-response.check.js
//   node .../persistent-stacks-compose-preview-late-response.check.js /tmp/pre-fix-persistent-stacks.js   (mutation proof)
'use strict';
const fs = require('fs');
const path = require('path');

const jsPath = process.argv[2] || path.join(__dirname, '..', 'js', 'persistent-stacks.js');
const src = fs.readFileSync(jsPath, 'utf8');

function grabFn(startMarker, endMarker) {
    const a = src.indexOf(startMarker);
    if (a < 0) throw new Error('missing ' + startMarker);
    const b = src.indexOf(endMarker, a + startMarker.length);
    if (b < 0) throw new Error('missing ' + endMarker);
    return src.slice(a, b);
}

const stateBlock = grabFn('let _psComposePreview = null;', 'function _psRenderComposePreview');
const previewFn = grabFn('async function _psComposePreviewClick() {', 'function _psComposeName() {');
const createFn = grabFn('async function _psComposeCreateClick() {', 'async function _psAct(');

const body = [
    'let _psEsc = (s) => String(s);',
    stateBlock,
    previewFn,
    createFn,
    'let _delivered = { preview: null, finalize: null };',
    "async function apiCall(url, opt) { return await (url.indexOf('finalize') >= 0 || url.indexOf('secrets') >= 0 ? _delivered.finalize : _delivered.preview); }",
    'function _psRenderComposePreview(p) { _psComposeResult("Parsed " + p.tag + " service(s)", true); }',
    'function _psComposeName() { return "e2e-stack"; }',
    'function _psMountCreateSecretsSection(x) {}',
    'function _psComposeVarsPayload() { return {}; }',
    'let _psComposeVarsCtrl = null;',
    'function _psCollectSecretsEdits(k, h, s) { return { changes: { env: [], files: [] }, newSecrets: [], errors: [] }; }',
    'function svMissingNames(e, n) { return []; }',
    'function _psClearSecretSec(k) {}',
    'function _psLoadStacks() {}',
    'function _psBackupAckShow() {}',
    'const document = { getElementById: (id) => ({',
    "  'ps-compose-yaml': { value: 'services:\\n  app:\\n    run: x\\n' },",
    "  'ps-compose-name': { value: 'e2e-stack' },",
    "  'ps-compose-secrets': null,",
    "  'ps-compose-result': _resultEl,",
    '}[id]) };',
    'return { previewClick: _psComposePreviewClick, createClick: _psComposeCreateClick, delivered: _delivered };',
].join('\n');

const resultEl = { innerHTML: '', className: '', classList: { toggle: () => {} } };
const S = new Function('_resultEl', body)(resultEl);
const resultText = () => resultEl.innerHTML;

let passes = 0, failures = 0;
function check(cond, msg) {
    if (cond) { passes++; console.log('ok   ' + msg); }
    else { failures++; console.error('FAIL ' + msg); }
}

// A request handle that stays undelivered until the driver flips `ready`.
// The wait uses setTimeout (macrotasks) so it does not starve the await-null
// microtask pump below.
function makePending() {
    const box = { ready: false, value: null };
    const p = (async () => {
        while (!box.ready) await new Promise((res) => setTimeout(res, 10));
        return box.value;
    })();
    return { box, p };
}

(async () => {
    // --- 1) THE RACE: Preview in flight, Create completes, late Preview reply
    const slowPrev = makePending();
    S.delivered.preview = slowPrev.p;
    const previewReq = S.previewClick();           // awaits the slow preview
    S.delivered.finalize = Promise.resolve({ name: 'e2e-stack', id: 94, status: 'created' });
    await S.createClick();                         // paints "Created stack ..."
    check(/Created stack "e2e-stack"/.test(resultText()), 'create paints the created line');
    slowPrev.box.ready = true;
    slowPrev.box.value = { tag: 1, needs_confirmation: [], compose_spec: { services: { app: {} } } };
    await new Promise((r) => setTimeout(r, 200));  // let the preview await resume
    check(/Created stack "e2e-stack"/.test(resultText()),
        'late Preview reply did NOT overwrite the created line');
    check(!/Parsed 1 service/.test(resultText()), 'no preview text in the result after the race');
    previewReq.catch(() => {});

    // --- 2) Preview still works when Create is NOT pressed
    S.delivered.preview = Promise.resolve({ tag: 2, needs_confirmation: [], compose_spec: { services: { app: {} } } });
    await S.previewClick();
    check(/Parsed 2 service/.test(resultText()), 'Preview alone still renders the preview line');

    // --- 3) Two Previews, first slow: the older reply must not clobber the newer
    const slow2 = makePending();
    S.delivered.preview = slow2.p;
    const slowReq = S.previewClick();              // token N
    S.delivered.preview = Promise.resolve({ tag: 4, needs_confirmation: [], compose_spec: { services: { app: {} } } });
    await S.previewClick();                        // token N+1 renders
    check(/Parsed 4 service/.test(resultText()), 'newest Preview rendered');
    slow2.box.ready = true;
    slow2.box.value = { tag: 3, needs_confirmation: [], compose_spec: { services: { app: {} } } };
    await new Promise((r) => setTimeout(r, 200));
    check(!/Parsed 3 service/.test(resultText()), 'older Preview reply dropped (newest wins)');
    slowReq.catch(() => {});

    // --- 4) Create failure path still paints its own error line
    S.delivered.finalize = Promise.reject(new Error('boom'));
    await S.createClick();
    check(/Create failed: boom/.test(resultText()), 'create error line still paints');

    console.log('\n' + passes + ' passed, ' + failures + ' failed');
    process.exit(failures ? 1 : 0);
})();
