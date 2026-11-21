// 
// The stack row's Keep running checkbox lives inside the row's More menu (PIN11).
// The PIN13 final slice (results-1110/PIN13-FINAL-SLICE-R3-D76.md, room ce-cleanroom-
// cr33@gpu3) reproduced dash-stack-keep-running E3 red TWICE (4p/1f, identical first
// error): after cb.uncheck() with the panel held open, #ps-msg never showed
// 'free to be reclaimed' within 30 s.
//
// Playwright's locator.uncheck() is a CLICK on the checkbox (see _setChecked: it
// calls _click, then re-reads the state). The browser fires BOTH a `click` (which
// bubbles to #ps-rows) and a `change` on the input. The PIN13 tree wires ONLY the
// click leg, and its own comment says the checkbox's value change "fires `change`,
// not click" — nothing listens for it. When the row is rebuilt (the 5 s list poll,
// or the toggle's own _psLoadStacks) between Playwright's hit-test and the event,
// the click leg is LOST while the input still flips, so no PUT and no message.
//
// This check drives the REAL extracted blocks (the ps-rows click dispatcher, the
// ps-rows `change` listener, _psKeepRunningSet, _psMsg) over a fake DOM and asserts:
//   (1) a `change`-only arrival (the click leg lost) still PUTs and paints;
//   (2) the click leg alone still PUTs and paints (both directions);
//   (3) BOTH legs of ONE user action send exactly ONE PUT and paint once;
//   (4) a refused PUT reverts the checkbox and sends NO second PUT.
// RED on the PIN13 tree (no `change` wiring), GREEN on the fix.
//
//   node CORE/Dashboard/tests/persistent-stacks-keep-running-message.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

function blockBody(head) {   // the {...} body after `head`
    const i = SRC.indexOf(head);
    if (i < 0) return null;
    const open = SRC.indexOf('{', i);
    if (open < 0) return null;
    let depth = 0;
    for (let j = open; j < SRC.length; j += 1) {
        if (SRC[j] === '{') depth += 1;
        else if (SRC[j] === '}') { depth -= 1; if (!depth) return SRC.slice(open + 1, j); }
    }
    return null;
}
function fnBody(head) {
    const m = new RegExp(head).exec(SRC);
    if (!m) return null;
    const open = SRC.indexOf('{', m.index);
    let depth = 0;
    for (let i = open; i < SRC.length; i += 1) {
        if (SRC[i] === '{') depth += 1;
        else if (SRC[i] === '}') { depth -= 1; if (depth === 0) return SRC.slice(m.index, i + 1); }
    }
    return null;
}

const DISP_HEAD = "document.getElementById('ps-rows')?.addEventListener('click', (e) => {";
const CHG_HEAD = "document.getElementById('ps-rows')?.addEventListener('change', (e) => {";
const DISP_BODY = blockBody(DISP_HEAD);
const CHG_BODY = blockBody(CHG_HEAD);
const KR_FN = fnBody('async function _psKeepRunningSet\\(id, enabled\\)');
const MSG_FN = fnBody('function _psMsg\\(text, ok\\)');
check('(0) ps-rows click dispatcher extractable', !!DISP_BODY);
check('(0) ps-rows change listener extractable', !!CHG_BODY);
check('(0) _psKeepRunningSet extractable', !!KR_FN);
check('(0) _psMsg extractable', !!MSG_FN);
if (!DISP_BODY || !CHG_BODY || !KR_FN || !MSG_FN) { console.error('ABORT extract'); process.exit(1); }

// stub every _psXxx the two handlers call, except the real ones we drive
const ALL_BODY = DISP_BODY + CHG_BODY;
const REAL_HELPERS = /^_psMore/;
const PROVIDED = ['_psKeepRunningSet', '_psMsg', '_psLoadStacks', '_psCancelLogPoller'];
const STUBS = [...new Set(ALL_BODY.match(/_ps[A-Za-z]+/g) || [])]
    .filter((n) => !REAL_HELPERS.test(n) && PROVIDED.indexOf(n) < 0
        && new RegExp(n + '\\s*\\(').test(ALL_BODY))
    .map((n) => `function ${n}(){ return null; }`).join('\n');
const VARS = [...new Set(ALL_BODY.match(/_ps[A-Za-z]+/g) || [])]
    .filter((n) => !REAL_HELPERS.test(n) && PROVIDED.indexOf(n) < 0
        && !new RegExp(n + '\\s*\\(').test(ALL_BODY) && n.indexOf('_psKr') !== 0)
    .map((n) => `var ${n} = null;`).join('\n');

const CODE = [
    'var msgs = []; var puts = []; var loads = 0; var API_OK = true;',
    'function _camel(a){var p=a.slice(5).split("-");return p[0]+p.slice(1).map(function(w){return w[0].toUpperCase()+w.slice(1);}).join("");}',
    'function _matches(el,sel){var re=/\\[([a-z-]+)(?:="([^"]*)")?\\]/g;var m;while((m=re.exec(sel))){if(m[2]===undefined){if(!(m[1] in el.attrs))return false;}else if(el.attrs[m[1]]!==m[2])return false;}return true;}',
    'function _mk(tag,attrs,parent){var el={tag:tag,attrs:attrs,parent:parent,children:[],style:{},dataset:{},checked:false};for(var k in attrs){if(k.indexOf("data-")===0)el.dataset[_camel(k)]=attrs[k];}el.setAttribute=function(k,v){el.attrs[k]=v;};el.closest=function(sel){for(var n=el;n;n=n.parent){if(_matches(n,sel))return n;}return null;};el.matches=function(sel){return el.tag===sel||(sel.indexOf("[")===0?_matches(el,sel):false);};return el;}',
    'var cell = _mk("td",{},null);',
    'var panel = _mk("div",{"data-testid":"ps-more-menu-7","data-ps-menu":"7","aria-hidden":"false"},cell); panel.style.display = "block";',
    'var cb = _mk("input",{"type":"checkbox","id":"ps-keep-running-7","data-ps-act":"keep-running-toggle","data-ps-id":"7","data-testid":"ps-keep-running-7"},panel);',
    'var msgEl = _mk("p",{"id":"ps-msg"},null); msgEl.classList = { toggle: function(){} };',
    'var ALL = [panel, cb, msgEl, cell];',
    'var document = { getElementById: function(id){ if(id==="ps-rows")return tbody; if(id==="ps-msg")return msgEl; return null; }, querySelector: function(sel){ for(var i=0;i<ALL.length;i+=1){ if(_matches(ALL[i],sel)) return ALL[i]; } return null; }, addEventListener: function(){}, activeElement: null };',
    'var tbody = _mk("tbody",{"id":"ps-rows"},null);',
    'function apiCall(p,o){ puts.push(o&&o.body?JSON.parse(o.body):null); if(!API_OK){ var e=new Error("forbidden"); e.status=403; return new Promise(function(f,r){ r(e); }); } return new Promise(function(f,r){ f({}); }); }',
    'async function _psLoadStacks(){ loads++; return {}; }',
    'function _psCancelLogPoller(id){}',
    'var _psMoreOpen = 7;',
    'var _psKrKey = null; var _psKrRevert = null; var _psKrBusy = new Set();',
    STUBS, VARS,
    KR_FN,
    MSG_FN,
    'var _PS_ONCLICK = (e) => {' + DISP_BODY + '};',
    'var _PS_ONCHANGE = (e) => {' + CHG_BODY + '};',
    // record every #ps-msg write (the real _psMsg sets el.textContent)
    'Object.defineProperty(msgEl, "textContent", { set: function(v){ msgs.push(String(v)); }, get: function(){ return ""; } });',
    'sandbox.cb = cb; sandbox.panel = panel; sandbox.msgEl = msgEl; sandbox.puts = puts; sandbox.msgs = msgs;',
    'sandbox.setApi = function(v){ API_OK = v; };',
    'sandbox.click = function(){ _PS_ONCLICK({ target: cb }); };',
    'sandbox.change = function(){ _PS_ONCHANGE({ target: cb }); };',
].join('\n');

const sandbox = {};
new Function('sandbox', CODE)(sandbox);

async function drain() { for (let i = 0; i < 60; i += 1) await new Promise((r) => r()); }
function reset() { sandbox.puts.length = 0; sandbox.msgs.length = 0; }

(async () => {
    const OFF = 'free to be reclaimed';
    const ON = 'pinned running';

    // (1) the CHANGE leg alone: the click event was lost (the row was rebuilt
    //     between Playwright's hit-test and the event), the input still flipped.
    reset();
    sandbox.cb.checked = false;
    sandbox.change();
    await drain();
    check('(1) change-only OFF: PUT enabled:false sent', sandbox.puts.some((p) => p && p.enabled === false));
    check(`(1) change-only OFF: #ps-msg got '${OFF}'`, sandbox.msgs.some((t) => t.includes(OFF)));

    reset();
    sandbox.cb.checked = true;
    sandbox.change();
    await drain();
    check('(1b) change-only ON: PUT enabled:true sent', sandbox.puts.some((p) => p && p.enabled === true));
    check(`(1b) change-only ON: #ps-msg got '${ON}'`, sandbox.msgs.some((t) => t.includes(ON)));

    // (2) the CLICK leg alone still works (both directions)
    reset();
    sandbox.cb.checked = false;
    sandbox.click();
    await drain();
    check('(2) click-only OFF: exactly ONE PUT', sandbox.puts.length === 1);
    check(`(2) click-only OFF: #ps-msg got '${OFF}'`, sandbox.msgs.some((t) => t.includes(OFF)));

    reset();
    sandbox.cb.checked = true;
    sandbox.click();
    await drain();
    check('(2b) click-only ON: exactly ONE PUT', sandbox.puts.length === 1);
    check(`(2b) click-only ON: #ps-msg got '${ON}'`, sandbox.msgs.some((t) => t.includes(ON)));

    // (3) BOTH legs of ONE user action (the real browser fires click + change)
    reset();
    sandbox.cb.checked = false;
    sandbox.click();
    sandbox.change();
    await drain();
    check('(3) click+change OFF: exactly ONE PUT', sandbox.puts.length === 1);
    check(`(3) click+change OFF: #ps-msg got '${OFF}'`, sandbox.msgs.some((t) => t.includes(OFF)));

    reset();
    sandbox.cb.checked = true;
    sandbox.click();
    sandbox.change();
    await drain();
    check('(3b) click+change ON: exactly ONE PUT', sandbox.puts.length === 1);
    check(`(3b) click+change ON: #ps-msg got '${ON}'`, sandbox.msgs.some((t) => t.includes(ON)));

    // (4) a refused PUT: the checkbox reverts, and the revert's own `change`
    //     must NOT send a second PUT.
    reset();
    sandbox.setApi(false);
    sandbox.cb.checked = false;
    sandbox.click();
    sandbox.change();
    await drain();
    check('(4) refused PUT: exactly ONE PUT attempted', sandbox.puts.length === 1);
    check('(4) refused PUT: checkbox reverted to checked=true', sandbox.cb.checked === true);
    sandbox.change();   // the revert's own change event
    await drain();
    check('(4) refused PUT: the revert sent NO second PUT', sandbox.puts.length === 1);

    console.log(`\npersistent-stacks-keep-running-message.check.js: ${failures} failure(s)`);
    process.exit(failures ? 1 : 0);
})();
