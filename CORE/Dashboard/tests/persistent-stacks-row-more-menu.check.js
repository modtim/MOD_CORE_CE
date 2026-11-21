//  — the stack row shows THREE
// visible buttons plus one grouped "More" menu (owner 11-18: "action buttons on
// the stacks are CRAZY"). Like the other persistent-stacks checks this extracts
// the real source blocks (the More-menu helpers, _psStackRow, _psStackSig, the
// status/HA class maps) from js/persistent-stacks.js, runs them as pure
// functions over fake stacks, and asserts the rendered markup.
//
//   node CORE/Dashboard/tests/persistent-stacks-row-more-menu.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
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

// The helper block now runs through _psMoreReapply (the post-render re-apply,
// ); fall back
// to _psMoreToggle if a tree has no re-apply helper yet.
const HELPERS = (() => {
    const a = SRC.indexOf('let _psMoreOpen = null;');
    if (a < 0) return null;
    const tail = fnBody('function _psMoreReapply\\(\\)') || fnBody('function _psMoreToggle\\(sid\\)');
    if (!tail) return null;
    return SRC.slice(a, SRC.indexOf(tail, a) + tail.length);
})();
const ROW = fnBody('function _psStackRow\\(s, workers\\)');
const SIG = fnBody('function _psStackSig\\(s\\)');
const STATUS_CLASS = (() => { const m = /const _PS_STATUS_CLASS = \{/.exec(SRC); if (!m) return null; const o = SRC.indexOf('{', m.index); let d = 0; for (let i = o; i < SRC.length; i++) { if (SRC[i] === '{') d++; else if (SRC[i] === '}') { d--; if (!d) return SRC.slice(m.index, i + 1); } } return null; })();

check('More-menu helper block present', !!HELPERS);
check('_psStackRow extractable', !!ROW);
check('_psStackSig extractable', !!SIG);
check('_PS_STATUS_CLASS extractable', !!STATUS_CLASS);
if (!HELPERS || !ROW || !SIG || !STATUS_CLASS) { console.error(`ABORT ${failures}`); process.exit(1); }

// --- sandbox: real row renderer + real More-menu state ----------------------
const STUBS = [
    `function _psEsc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}`,
    "function _psTransitLabel(s){return '';}",
    "function _psAddrCell(s){return '';}",
    "function _psGatewayLine(g){return '';}",
    "function _psWorkerSelect(s,w){return '';}",
    "function _psEgressBadge(s){return '';}",
    "function _psBackupBadge(s){return '';}",
    "function _psHealthBadge(s){return '';}",
    "function _psIdentityLine(s){return '—';}",
    // FIX-DASH-STACK-PORT-WARNINGS added a status-reason line
    // inside the row; use the real one when present so this sandbox never
    // ReferenceErrors, else stub it.
    (() => {
        const a = SRC.indexOf('// PS-STATUS-REASON-BEGIN');
        const b = SRC.indexOf('// PS-STATUS-REASON-END');
        return a >= 0 && b > a ? SRC.slice(a, b + '// PS-STATUS-REASON-END'.length)
            : "function _psStatusReason(s){return '';}";
    })(),
    "var _PS_HA_CLASS_STYLE = {}; var _PS_HA_CLASS_LABEL = {}; var _PS_HA_CLASS_DESC = {};",
    // : the
    // More-menu helpers touch the DOM (_psMoreMenuEl/_psMorePosition). Give the
    // sandbox an empty document so the REAL toggle/close/position code runs
    // (querySelector finds nothing -> the helpers take their no-element path);
    // `window` stays absent, which _psMorePosition handles via typeof.
    "var document = { querySelector: function(){ return null; }, getElementById: function(){ return null; }, addEventListener: function(){}, activeElement: null };",
].join('\n');

const CODE = [STUBS, STATUS_CLASS, HELPERS, ROW, SIG].join('\n');
const sandbox = { render: null, open: null };
const fn = new Function('sandbox', CODE + `
    sandbox.render = (s) => _psStackRow(s, []);
    sandbox.getOpen = () => _psMoreOpen;
    sandbox.setOpen = (v) => { _psMoreOpen = v; };
    sandbox.toggle = (sid) => { _psMoreOpen = sid; };
    sandbox.close = () => { _psMoreOpen = null; };
    // the REAL helpers (the shortcuts above only move the state var)
    sandbox.realToggle = (sid) => _psMoreToggle(sid);
    sandbox.realClose = () => _psMoreClose();
    sandbox.realReapply = () => _psMoreReapply();
`);
fn(sandbox);

const MENU_START = '<div data-testid="ps-more-menu-';
const CELL_END = '</div></td>';
// : the open/
// closed state is read from the panel's INLINE style (display:none / display:
// block), never from the `hidden` attribute or a class name — that is the
// property the real browser checks (getComputedStyle(panel).display).
function panelStyle(html) {
    const i = html.indexOf(MENU_START);
    if (i < 0) return '';
    const gt = html.indexOf('>', i);
    const m = /style="([^"]*)"/.exec(html.slice(i, gt));
    return m ? m[1] : '';
}
function render(s, openSid) {
    sandbox.setOpen(openSid);
    const html = sandbox.render(s);
    const i = html.indexOf(MENU_START);
    if (i < 0) return { visible: html, menu: '', open: false, style: '' };
    const j = html.indexOf(CELL_END, i);
    const menu = html.slice(i, j < 0 ? html.length : j);
    const style = panelStyle(html);
    return { visible: html.slice(0, i), menu, open: /display:block/.test(style), style };
}
function actsIn(html) {
    const out = [];
    const re = /data-ps-act="([a-z-]+)"/g;
    let m; while ((m = re.exec(html)) !== null) out.push(m[1]);
    return out;
}
function topActs(html) {
    // the visible cell: buttons/labels between the action-cell div and the menu
    const i = html.indexOf('<div class="relative flex flex-wrap gap-1 justify-end"');
    const body = i < 0 ? html : html.slice(i, html.indexOf(MENU_START) < 0 ? html.length : html.indexOf(MENU_START));
    return actsIn(body);
}

const running = { id: 7, name: 'live', status: 'running', desired_state: 'running' };
const stopped = { id: 8, name: 'down', status: 'stopped', desired_state: 'running' };
const needsRedeploy = { id: 9, name: 'dirty', status: 'running', desired_state: 'running', needs_redeploy: true };
const deliberatelyStopped = { id: 10, name: 'off', status: 'stopped', desired_state: 'stopped' };
const pinned = { id: 11, name: 'pinned', status: 'running', desired_state: 'running', pinned_worker: 'gpu1', keep_running: true };
const draft = { id: 12, name: 'draft', status: 'created', target_worker: null };

// --- (a) a running stack row shows exactly Stop, Logs, Edit, More -----------
const rRun = render(running, null);
check('(a) running row: exactly 4 visible buttons', topActs(rRun.visible).join(',') === 'stop,logs,edit-def,more');
check('(a) running row: main button is Stop', /data-ps-act="stop"[^>]*>Stop</.test(rRun.visible));
check('(a) running row: Deploy moved into More', actsIn(rRun.menu).includes('deploy'));
check('(a) More button carries data-testid + aria-haspopup', /data-ps-act="more"[^>]*data-testid="ps-more-btn"[^>]*aria-haspopup="menu"/.test(rRun.visible));
check('(a) More button aria-expanded=false when closed', /data-ps-act="more"[^>]*aria-expanded="false"/.test(rRun.visible));

// --- (b) a stopped stack shows Deploy as main; needs_redeploy -> amber Redeploy
const rStop = render(stopped, null);
check('(b) stopped row: main button is Deploy', /data-ps-act="deploy" data-ps-id="8" class="px-2 py-1 text-xs bg-primary text-white rounded hover:bg-primary\/90">Deploy</.test(rStop.visible));
check('(b) stopped row: Stop lives in More', /data-ps-act="stop" data-ps-id="8"/.test(rStop.menu));
const rRedeploy = render(needsRedeploy, null);
check('(b) needs_redeploy: main button is amber Redeploy', /data-testid="ps-redeploy-btn"[^>]*bg-amber-500[^>]*>Redeploy</.test(rRedeploy.visible));
check('(b) needs_redeploy: Redeploy keeps data-ps-act="deploy"', /data-ps-act="deploy" data-ps-id="9" data-testid="ps-redeploy-btn"/.test(rRedeploy.visible));
const rDelib = render(deliberatelyStopped, null);
check('(b) deliberately stopped: main button is Deploy, Start in More',
    /data-ps-act="deploy" data-ps-id="10" class="px-2 py-1 text-xs bg-primary/.test(rDelib.visible)
    && /data-ps-act="start" data-ps-id="10"/.test(rDelib.menu));

// --- (c) every previous act is still present, inside the menu --------------
const PRE_CHANGE_ACTS = ['deploy', 'edit-def', 'secrets', 'login-gate', 'clone', 'backup', 'backups',
    'seed', 'restore', 'migrate', 'expose', 'endpoints', 'keep-running-toggle', 'pin', 'gpu-pin',
    'gpu-unpin', 'stop', 'start', 'logs', 'delete', 'teardown'];
const rPin = render(pinned, null);
const rDraft = render(draft, null);
const allAfter = actsIn(rPin.visible) .concat(actsIn(rPin.menu));
const allDraft = actsIn(rDraft.visible).concat(actsIn(rDraft.menu));
for (const act of ['deploy', 'edit-def', 'secrets', 'login-gate', 'clone', 'backup', 'backups', 'seed',
    'restore', 'migrate', 'expose', 'endpoints', 'keep-running-toggle', 'logs', 'teardown']) {
    check(`(c) act ${act} still rendered (pinned row)`, allAfter.includes(act));
}
check('(c) gpu-pin/gpu-unpin only when pinned', allAfter.includes('gpu-pin') && allAfter.includes('gpu-unpin'));
check('(c) unpin replaces pin when pinned', allAfter.includes('unpin') && !allAfter.includes('pin'));
check('(c) pin rendered when not pinned', allDraft.includes('pin') && !allDraft.includes('unpin'));
check('(c) Delete only for created + no host', allDraft.includes('delete') && !allAfter.includes('delete'));
check('(c) keep-running checkbox id/testid unchanged', /id="ps-keep-running-11"[^>]*data-testid="ps-keep-running-11"/.test(rPin.menu));
check('(c) ps-seed-btn / ps-endpoints-btn / ps-delete-btn testids still exist',
    rPin.menu.includes('data-testid="ps-seed-btn"') && rPin.menu.includes('data-testid="ps-endpoints-btn"')
    && rDraft.menu.includes('data-testid="ps-delete-btn"'));
check('(c) no act lost: pinned row covers the pre-change act set',
    PRE_CHANGE_ACTS.filter((a) => !allAfter.includes(a) && a !== 'delete' && a !== 'start' && a !== 'pin').length === 0);

// --- (d) Tear down and Delete are in the Danger group, last -----------------
const dangerIdx = rDraft.menu.indexOf('Danger');
check('(d) Danger group heading present', dangerIdx >= 0);
check('(d) Tear down is the last group', rDraft.menu.indexOf('data-ps-act="teardown"') > dangerIdx);
check('(d) Delete follows Tear down in Danger', rDraft.menu.indexOf('data-ps-act="delete"') > rDraft.menu.indexOf('data-ps-act="teardown"'));
check('(d) Danger is the last group in the menu',
    dangerIdx > rDraft.menu.indexOf('Placement') && dangerIdx > rDraft.menu.indexOf('>Data<'));
check('(d) Danger group is border-separated and red-texted',
    /border-t border-gray-200[^"]*"[^>]*>\s*<div class="text-\[10px\] font-semibold text-destructive">Danger</.test(rDraft.menu));
check('(d) group order Run < Configure < Data < Placement < Danger',
    rDraft.menu.indexOf('>Run<') < rDraft.menu.indexOf('>Configure<')
    && rDraft.menu.indexOf('>Configure<') < rDraft.menu.indexOf('>Data<')
    && rDraft.menu.indexOf('>Data<') < rDraft.menu.indexOf('>Placement<')
    && rDraft.menu.indexOf('>Placement<') < dangerIdx);

// --- (e) a poll re-render with the menu open keeps it open ------------------
const rOpen = render(running, 7);
check('(e) menu open: hidden attribute dropped', /data-testid="ps-more-menu-7" data-ps-menu="7" aria-hidden="false"/.test(rOpen.menu));
check('(e) More button aria-expanded=true', /data-ps-act="more" data-ps-id="7"[^>]*aria-expanded="true"/.test(rOpen.visible));
const rReRender = render(running, 7);   // the auto-refresh patcher rebuilds the row
check('(e) re-render keeps the menu open', rReRender.open === true);
check('(e) re-render keeps aria-expanded=true', /aria-expanded="true"/.test(rReRender.visible));
check('(e) only one menu open at a time (other rows stay closed)', render(stopped, 7).open === false);

// --- (g) INLINE-STYLE VISIBILITY (the real-browser gap, PIN12-D76) ----------
// The closed panel must be hidden by an inline `display:none` (inline style
// beats every class rule), and the open panel by inline `display:block`; no
// visibility may depend on a class name or the `hidden` attribute.
const rClosed = render(running, null);
check('(g) closed panel inline style has display:none', /display:none/.test(rClosed.style));
check('(g) closed panel inline style has no display:block', !/display:block/.test(rClosed.style));
check('(g) open panel inline style has display:block', /display:block/.test(rOpen.style));
check('(g) open panel inline style has no display:none', !/display:none/.test(rOpen.style));
check('(g) panel inline style is positioned (absolute/fixed)', /position:(absolute|fixed)/.test(rOpen.style));
check('(g) panel inline style anchors right:0/top:100% + z-index', /right:0/.test(rOpen.style) && /top:100%/.test(rOpen.style) && /z-index:50/.test(rOpen.style));
check('(g) panel inline style carries min-width + opaque background', /min-width:14rem/.test(rOpen.style) && /background:#fff/.test(rOpen.style));
check('(g) panel inline style carries border/radius/shadow/padding', /border:1px solid #e5e7eb/.test(rOpen.style) && /border-radius:0\.5rem/.test(rOpen.style) && /box-shadow:0 4px 12px rgba\(0,0,0,\.15\)/.test(rOpen.style) && /padding:0\.25rem 0/.test(rOpen.style));
check('(g) closed panel is display:none regardless of classes', !/display:block/.test(rClosed.style) && /display:none/.test(rClosed.style));
check('(g) no visibility from the `hidden` attribute', !/ hidden[ >]/.test(rClosed.menu) && !/ hidden[ >]/.test(rOpen.menu));
check('(g) min-w-64 (absent from the compiled CSS) no longer used', !/min-w-64/.test(SRC));
// items: every menu item is an inline-styled block filling the panel width
const itemStyled = (rOpen.menu.match(/<(button|label) style="display:block;width:100%;text-align:left"/g) || []).length;
check('(g) every menu item is an inline block (display:block;width:100%;text-align:left)',
    itemStyled === (rOpen.menu.match(/data-ps-act="/g) || []).length && itemStyled > 0);
// the More button itself stays inline-visible (never hidden by the panel rule)
check('(g) More button has no display:none', !/data-ps-act="more"[^>]*display:none/.test(rOpen.visible));
// toggle semantics through the REAL helpers (not the sandbox shortcuts)
sandbox.setOpen(null);
sandbox.realToggle(7);
check('(g) real toggle opens: getOpen=7', sandbox.getOpen() === 7);
sandbox.realToggle(7);
check('(g) real toggle again closes: getOpen=null', sandbox.getOpen() === null);
sandbox.realToggle(7); sandbox.realClose();
check('(g) real close() clears state', sandbox.getOpen() === null);
sandbox.realToggle(7); sandbox.realToggle(9);
check('(g) opening another row closes the first (one menu at a time)', sandbox.getOpen() === 9);
sandbox.realReapply();
check('(g) real reapply keeps the open state', sandbox.getOpen() === 9);
check('(g) close() renders the panel display:none', /display:none/.test(render(running, null).style));
// _psMoreReapply re-applies the open state after a rebuild
const REAPPLY = fnBody('function _psMoreReapply\\(\\)');
check('(g) _psMoreReapply exists and re-positions the open menu',
    !!REAPPLY && /_psMorePosition\(_psMoreOpen\)/.test(REAPPLY));
check('(g) _psPatchStacks re-applies the open menu after the reconcile',
    /_psMoreReapply\(\);\n\}/.test(SRC));
check('(g) _psLoadStacks re-applies the open menu after the tbody rewrite',
    /_psMoreReapply\(\);/.test(SRC));
check('(g) _psMorePosition sets inline display + fixed geometry',
    /m\.style\.display = 'block'/.test(SRC) && /m\.style\.position = 'fixed'/.test(SRC)
    && /m\.style\.display = 'none'/.test(SRC));
check('(g) window read safely (pure-function sandboxes have none)',
    /typeof window === 'object'/.test(SRC));

// --- (h) menu-item click closes the panel BEFORE the action runs (PIN13-D76)
//  (PIN12 Dell
// browser proof letter (g) FAILED): clicking "Endpoints..." opened its dialog
// while the More panel stayed display:block over it. The dispatcher drives most
// acts with an early `return _psXxx(id)` that opens a dialog / awaits a fetch,
// so a close placed at the bottom never runs for them. This case drives the REAL
// dispatcher (the ps-rows click handler extracted from the source) over a fake
// DOM whose action stubs return a never-resolving promise and record the panel's
// INLINE display at the moment the action runs: with the fix it is already
// 'none' (close first), so the action can never see/leave an open panel.
const DISP_BODY = (() => {
    const head = "document.getElementById('ps-rows')?.addEventListener('click', (e) => {";
    const i = SRC.indexOf(head);
    if (i < 0) return null;
    const open = i + head.length - 1;
    let d = 0;
    for (let j = open; j < SRC.length; j += 1) {
        if (SRC[j] === '{') d += 1;
        else if (SRC[j] === '}') { d -= 1; if (!d) return SRC.slice(open + 1, j); }
    }
    return null;
})();
check('(h) ps-rows click dispatcher extractable', !!DISP_BODY);
if (!DISP_BODY) { console.error('ABORT h'); process.exit(1); }
const REAL_HELPERS = /^_psMore/;
const DISP_NAMES = [...new Set(DISP_BODY.match(/_ps[A-Za-z]+/g) || [])]
    .filter((n) => !REAL_HELPERS.test(n));
const DISP_FN_NAMES = DISP_NAMES.filter((n) => new RegExp(n + '\\s*\\(').test(DISP_BODY));
const DISP_VAR_NAMES = DISP_NAMES.filter((n) => DISP_FN_NAMES.indexOf(n) < 0);
const CODE2 = [
    'var log = []; var panel = null;',
    'function _camel(a){var p=a.slice(5).split("-");return p[0]+p.slice(1).map(function(w){return w[0].toUpperCase()+w.slice(1);}).join("");}',
    'function _matches(el,sel){var re=/\\[([a-z-]+)(?:="([^"]*)")?\\]/g;var m;while((m=re.exec(sel))){if(m[2]===undefined){if(!(m[1] in el.attrs))return false;}else if(el.attrs[m[1]]!==m[2])return false;}return true;}',
    'function _mk(tag,attrs,parent){var el={tag:tag,attrs:attrs,parent:parent,children:[],style:{},dataset:{},checked:false};for(var k in attrs){if(k.indexOf("data-")===0)el.dataset[_camel(k)]=attrs[k];}el.setAttribute=function(k,v){el.attrs[k]=v;};el.closest=function(sel){for(var n=el;n;n=n.parent){if(_matches(n,sel))return n;}return null;};return el;}',
    'var cell = _mk("td",{"data-ps-row":"7"},null);',
    'panel = _mk("div",{"data-testid":"ps-more-menu-7","data-ps-menu":"7","aria-hidden":"false"},cell); panel.style.display = "block";',
    'var btns = {};',
    'function _addItem(act){var b=_mk("button",{"data-ps-act":act,"data-ps-id":"7"},panel);btns[act]=b;return b;}',
    'var moreBtn = _mk("button",{"data-ps-act":"more","data-ps-id":"7"},cell); moreBtn.getBoundingClientRect = function(){return null;};',
    'var tbody = _mk("tbody",{"id":"ps-rows"},null);',
    'var document = { getElementById: function(id){ return id === "ps-rows" ? tbody : null; }, querySelector: function(sel){ var m=/\\[data-testid="([^"]*)"\\]/.exec(sel); return (m && m[1] === "ps-more-menu-7") ? panel : null; }, addEventListener: function(){}, activeElement: null };',
    'function confirm(){ log.push("confirm:" + (panel ? panel.style.display : "")); return true; }',
    DISP_FN_NAMES.map((n) => `function ${n}(){ log.push("${n}:" + (panel ? panel.style.display : "")); return new Promise(function(){ return "never"; }); }`).join('\n'),
    DISP_VAR_NAMES.map((n) => `var ${n} = null;`).join('\n'),
    HELPERS,
    `var _PS_ONCLICK = (e) => {${DISP_BODY}};`,
    'sandbox2.dispatch = (e) => _PS_ONCLICK(e);',
    'sandbox2.btns = btns; sandbox2.panel = panel; sandbox2.log = log;',
    'sandbox2.addItem = (act) => _addItem(act);',
    'sandbox2.setOpen = (v) => { _psMoreOpen = v; };',
].join('\n');
const sandbox2 = {};
new Function('sandbox2', CODE2)(sandbox2);
function clickItem(act) {
    sandbox2.setOpen(7);
    sandbox2.panel.style.display = 'block';
    sandbox2.panel.attrs['aria-hidden'] = 'false';
    sandbox2.log.length = 0;
    const b = sandbox2.addItem(act);
    sandbox2.dispatch({ target: b });
    return sandbox2.log.slice();
}
for (const act of ['endpoints', 'secrets', 'login-gate', 'seed', 'backup', 'backups', 'deploy', 'stop',
    'start', 'expose', 'clone', 'migrate', 'restore', 'pin', 'unpin', 'gpu-pin', 'gpu-unpin', 'teardown',
    'delete', 'edit-def', 'def-save', 'def-revert', 'hp-save', 'ha-save', 'set-schedule', 'clear-schedule',
    'copylogs', 'logs-now', 'logs-live', 'expose-close', 'endpoints-close', 'seed-close', 'secrets-close']) {
    const log = clickItem(act);
    check(`(h) ${act}: panel display:none synchronously after the click`, sandbox2.panel.style.display === 'none');
    check(`(h) ${act}: action ran with the panel already closed`, log.some((l) => l.startsWith('_ps') && l.endsWith(':none')));
}
check('(h) teardown confirm dialog saw the panel closed', (() => { clickItem('teardown'); return sandbox2.log.some((l) => l === 'confirm:none'); })());
check('(h) Keep running checkbox keeps the menu open', (() => { const log = clickItem('keep-running-toggle'); return sandbox2.panel.style.display === 'block' && log.some((l) => l.startsWith('_psKeepRunningSet:block')); })());
const DISP_CODE = DISP_BODY.replace(/\/\/[^\n]*/g, '').replace(/`[^`]*`/g, '``');
const ORDER_OK = (() => { const ci = DISP_CODE.indexOf('_psMoreClose();'); const fi = DISP_CODE.search(/return _ps(?!More)[A-Za-z]+\(/); return ci >= 0 && fi >= 0 && ci < fi; })();
check('(h) close call precedes the first act dispatch in the dispatcher', ORDER_OK);

// --- (f) Escape closes it ---------------------------------------------------
// --- (j) window resize re-applies the open panel immediately (PIN13-D76) ----
const RESIZE_BLOCKS = SRC.match(/window\.addEventListener\('resize'[\s\S]*?\}\);/g) || [];
check('(j) exactly one window resize listener in the file', RESIZE_BLOCKS.length === 1);
check('(j) resize listener re-applies the open menu or closes it',
    RESIZE_BLOCKS.length === 1 && /_psMoreReapply\(\)/.test(RESIZE_BLOCKS[0])
    && /_psMoreClose\(\)/.test(RESIZE_BLOCKS[0]));
check('(j) resize listener no-ops when no menu is open',
    RESIZE_BLOCKS.length === 1 && /_psMoreOpen == null/.test(RESIZE_BLOCKS[0]));
check('(j) resize listener re-measures before re-applying (stale-geometry guard)',
    RESIZE_BLOCKS.length === 1 && /getBoundingClientRect\(\)/.test(RESIZE_BLOCKS[0])
    && /window\.innerWidth/.test(RESIZE_BLOCKS[0]));
check('(j) resize listener is inside the one-time wired guard (registered once)',
    (() => { const w = SRC.indexOf('if (psNeedsWire)'); const rz = SRC.indexOf("window.addEventListener('resize'"); return w >= 0 && rz > w; })());
check('(f) Escape keydown closer wired', /addEventListener\('keydown'[\s\S]{0,120}e\.key === 'Escape'[\s\S]{0,80}_psMoreClose\(\)/.test(SRC));
check('(f) outside-click closer wired', /addEventListener\('click'[\s\S]{0,400}_psMoreClose\(\)/.test(SRC));
check('(f) menu-item click closes the menu before the action runs (PIN13 order)', ORDER_OK);
check('(f) more act delegated, not sent to the API', /if \(act === 'more'\) return _psMoreToggle\(Number\(id\)\);/.test(SRC));
sandbox.setOpen(7); sandbox.close();
check('(f) close() hides the menu on the next render', render(running, null).open === false);

console.log(`\npersistent-stacks-row-more-menu.check.js: ${failures} failure(s)`);
process.exit(failures ? 1 : 0);
