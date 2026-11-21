// Node-runnable check for the CE edition gate on the two 'Preview rendered'
// buttons of the Persistent Stacks page (Dashboard/js/persistent-stacks.js,
// ).
//
// The buttons (create-card data-testid ps-render-preview-btn-create, injected in
// initializePersistentStacksPage; edit-panel data-testid ps-render-preview-btn-edit
// in the _psRenderEditDefinition template) call POST /stacks/render-preview, which
// is paid-only on CE (CORE/API/routers/persistent_stacks/persistent_stacks.py:2905
// _require_paid("services.llm_serving_render", "LLM serving gateway render")). On CE
// that surfaced a red 'Preview failed: Not part of this edition: LLM serving
// gateway render.' line = a paid feature's UI on CE (owner rule).
//
// Same technique as the sibling CE-gate checks in this tests dir (extract the
// gate code VERBATIM from the shipped source text and run it against a stub DOM,
// EXTRACTED VERBATIM from the shipped source text and run against a stub DOM, so
// the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/persistent-stacks-render-preview-ce-gate.check.js
//   node <check> <alt-src>   -> run against another persistent-stacks.js text
//      (used to prove the check is RED on the pre-fix blob:
//       `git show <pre-fix sha>:CORE/Dashboard/js/persistent-stacks.js > /tmp/pre.js`)
//
// Asserts: CE -> neither render-preview button is rendered (create card nor edit
// panel); paid edition -> both still rendered, wording unchanged.
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = process.argv[2]
    ? path.resolve(process.argv[2])
    : path.join(__dirname, '..', 'js', 'persistent-stacks.js');
const psSrc = fs.readFileSync(SRC, 'utf8');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// --- the CE gate helper (absent on the pre-fix tree -> throw -> red) ---------
const WIRE_HDR = 'async function _psWireRenderPreview(anchorBtn)';
const wireAt = psSrc.indexOf(WIRE_HDR);
if (wireAt < 0) throw new Error('FIX not present: _psWireRenderPreview missing (pre-fix file state)');
const wireSrc = `${WIRE_HDR} ${blockAt(psSrc, psSrc.indexOf('{', wireAt + WIRE_HDR.length))}`;

const HIDDEN_HDR = 'function _psCeRenderPreviewHidden()';
const hiddenAt = psSrc.indexOf(HIDDEN_HDR);
if (hiddenAt < 0) throw new Error('FIX not present: _psCeRenderPreviewHidden missing');
const hiddenSrc = `${HIDDEN_HDR} ${blockAt(psSrc, psSrc.indexOf('{', hiddenAt + HIDDEN_HDR.length))}`;

// The edit-panel button site must be the conditional (pre-fix: a bare <button>).
const BTN_MARK = 'data-ps-act="def-render-preview"';
const btnAt = psSrc.indexOf(BTN_MARK);
if (btnAt < 0) throw new Error('edit-panel render-preview button not found');
const lineStart = psSrc.lastIndexOf('\n', btnAt) + 1;
const lineEnd = psSrc.indexOf('\n', btnAt);
const btnLine = psSrc.slice(lineStart, lineEnd);
if (!btnLine.includes('_psCeRenderPreviewHidden()')) throw new Error('edit-panel button not CE-gated (pre-fix file state)');

// --- stub DOM (elements are finished at creation: the wiring inserts during
//     the run, so insertAdjacentElement/setAttribute must already exist) ------
function makeDom() {
    const els = {};
    const created = [];
    function mk(id) {
        const e = { id, classes: new Set(), style: {}, dataset: {}, attrs: {} };
        e.classList = { add: (c) => e.classes.add(c), remove: (c) => e.classes.delete(c) };
        e.setAttribute = (k, v) => { e.attrs[k] = v; };
        e.addEventListener = () => {};
        e.insertAdjacentElement = (pos, el) => { (e.siblings || (e.siblings = [])).push(el); return el; };
        return e;
    }
    const doc = {
        getElementById: (id) => id ? (els[id] || (els[id] = mk(id))) : null,
        createElement: (tag) => { const e = mk(tag); created.push(e); return e; },
    };
    doc._all = () => Object.keys(els).map((k) => els[k]).concat(created);
    return doc;
}

async function runWire(ceEdition) {
    const dom = makeDom();
    const fn = new Function('document', '_psIsCeEdition', 'anchorBtn',
        `${wireSrc}\nreturn _psWireRenderPreview(anchorBtn);`);
    await fn(dom, () => ({ then: (cb) => cb(ceEdition) }), dom.getElementById('ps-compose-preview'));
    const all = dom._all();
    const btn = all.filter((e) => e.attrs['data-testid'] === 'ps-render-preview-btn-create').length;
    const panel = all.filter((e) => e.attrs['data-testid'] === 'ps-render-preview-panel-create').length;
    return { btn, panel };
}

(async function () {
    // --- create-card button: CE -> not injected ------------------------------
    const ce = await runWire(true);
    check('CE: create-card render-preview button NOT rendered', ce.btn === 0, `btn=${ce.btn}`);
    check('CE: create-card render-preview panel NOT rendered', ce.panel === 0, `panel=${ce.panel}`);

    // --- create-card button: paid -> still rendered (unchanged) --------------
    const paid = await runWire(false);
    check('paid: create-card render-preview button still rendered', paid.btn === 1, `btn=${paid.btn}`);
    check('paid: create-card render-preview panel still rendered', paid.panel === 1, `panel=${paid.panel}`);
    check('paid: create-card button label unchanged ("Preview rendered")',
        /btn\.textContent = 'Preview rendered';/.test(wireSrc));

    // --- edit-panel button: evaluate the shipped conditional verbatim -------
    const q = btnLine.indexOf('${');
    const z = btnLine.lastIndexOf('}');
    if (q < 0 || z < 0) throw new Error('no template expression around the button');
    const tmplExpr = btnLine.slice(q + 2, z);
    const renderBtn = (hidden) => {
        // the expression itself is a template literal when hidden=false, so
        // evaluate it as an expression (no extra backticks around it).
        const f = new Function('_psCeRenderPreviewHidden', 'id', `return (${tmplExpr});`);
        return f(() => hidden, 'st-1');
    };
    check('CE: edit-panel render-preview button NOT rendered', renderBtn(true).indexOf(BTN_MARK) < 0);
    check('paid: edit-panel render-preview button still rendered', renderBtn(false).indexOf(BTN_MARK) >= 0);
    check('paid: edit-panel button label unchanged ("Preview rendered")',
        renderBtn(false).includes('>Preview rendered</button>'));

    // --- the 'Preview' import button (CE-served) stays working ---------------
    // The button itself is static page markup (pages/analytics.html:1284); the JS
    // only wires it. Both must still be there after the fix.
    const pageSrc = fs.readFileSync(path.join(__dirname, '..', 'pages', 'analytics.html'), 'utf8');
    check('the CE-served "Preview" import button markup untouched (pages/analytics.html)',
        /id="ps-compose-preview"/.test(pageSrc));
    check('the JS still wires the "Preview" import button (getElementById)',
        /getElementById\('ps-compose-preview'\)/.test(psSrc));
    check('the import-preview handler still calls /stacks/import-compose (not render-preview)',
        /_psComposePreviewClick[\s\S]{0,600}\/stacks\/import-compose/.test(psSrc));

    // --- no new edition helper invented; the file's own predicate is reused --
    check('persistent-stacks.js reuses its existing _psIsCeEdition predicate',
        /function _psIsCeEdition\(\)/.test(psSrc) && /_psCeEditionCached = await _psIsCeEdition\(\);/.test(psSrc));
    const refreshAt = psSrc.indexOf('async function _psRefreshCeEditionCache');
    if (refreshAt < 0) throw new Error('FIX not present: _psRefreshCeEditionCache missing');
    const refreshSrc = `async function _psRefreshCeEditionCache ${blockAt(psSrc, psSrc.indexOf('{', refreshAt + 'async function _psRefreshCeEditionCache'.length))}`;
    check('the edit-panel cache refresher reuses _psIsCeEdition (no second edition fetch)',
        /_psIsCeEdition/.test(refreshSrc) && !/getPlatformFeatures/.test(refreshSrc));
    check('_psCeRenderPreviewHidden is fail-open (only a cached CE verdict hides)',
        /return _psCeEditionCached === true;/.test(hiddenSrc));
    check('_psLoadDefinition refreshes the verdict before each edit-panel render',
        /await _psRefreshCeEditionCache\(\);\s*\n\s*_psRenderEditDefinition\(id\);/.test(psSrc));

    // --- the edit-panel panel host div stays (harmless, empty on CE) --------
    check('edit-panel render-preview panel host div still present (empty on CE)',
        /id="ps-def-render-preview-\$\{id\}"/.test(psSrc));

    if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
    console.log('\nall checks passed');
})();
