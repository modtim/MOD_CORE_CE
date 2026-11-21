// NODE-WIZARD-NO-MODEL-MESSAGE: a wizard run that failed because no model
// endpoint is configured renders a neutral 'add a model endpoint' message with
// a Settings > AI Configuration link (data-settings-tab="ai", deep link
// ?tab=ai#settings + popstate, same form as _gotoPersistentStackRow in
// settings.js) instead of the raw API error. Every other error_message keeps
// rendering raw-escaped as before. The needle is the exact substring the API
// puts in error_message (CORE/API/services/support_chat_shared_service.py).
//
// Checks (against the SHIPPED source text of js/wizard-queue.js):
//   1) the file parses as a valid ES module.
//   2) NO_MODEL_NEEDLE is declared once with the exact API substring.
//   3) the same needle exists in the API source (skipped if not mounted).
//   4) the no-model branch: data-settings-tab="ai" and
//      data-testid="wizard-no-model" appear exactly once each.
//   5) _onRowsClick routes [data-settings-tab] BEFORE the cancel branch.
//   6) _gotoSettingsTab: replaceState to ?...#settings then one popstate.
//   7) the generic branch survives: escapeHtml(run.error_message) once.
//   8) behaviour: the no-model error yields the link html without the raw
//      text; a generic error stays escaped with no link.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function countNeedle(haystack, needle) {
    if (needle === '') throw new Error('empty needle');
    let n = 0;
    let i = haystack.indexOf(needle);
    while (i !== -1) {
        n += 1;
        i = haystack.indexOf(needle, i + needle.length);
    }
    return n;
}

function countRe(src, re) {
    return (src.match(re) || []).length;
}

let passed = 0;
let failed = 0;
function check(name, cond) {
    if (cond) { passed += 1; console.log('ok ' + name); }
    else { failed += 1; console.log('FAIL ' + name); }
}

const jsSrc = read('js/wizard-queue.js');

// --- 1) ES module syntax check ------------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
check('1 wizard-queue.js parses as an ES module', modErr === null);

// --- 2) the needle declaration ------------------------------------------
const NEEDLE = 'no endpoint is configured for the wizard tool';
check('2 NO_MODEL_NEEDLE declared exactly once with the exact API substring',
    countNeedle(jsSrc, "const NO_MODEL_NEEDLE = '" + NEEDLE + "';") === 1);

// --- 3) the needle also lives in the API source --------------------------
// The API builds the message from f-string pieces, so the full needle spans a
// line continuation in the .py text; check the contiguous fragment instead.
const apiPath = path.join(ROOT, '..', 'API', 'services', 'support_chat_shared_service.py');
if (fs.existsSync(apiPath)) {
    check('3 the needle fragment appears in support_chat_shared_service.py',
        countNeedle(fs.readFileSync(apiPath, 'utf8'), 'configured for the wizard tool') >= 1);
} else {
    console.log('api-source-skipped (support_chat_shared_service.py not mounted)');
}

// --- 4) the no-model branch markers --------------------------------------
check('4 data-settings-tab="ai" appears exactly once',
    countNeedle(jsSrc, 'data-settings-tab="ai"') === 1);
check('5 data-testid="wizard-no-model" appears exactly once',
    countNeedle(jsSrc, 'data-testid="wizard-no-model"') === 1);

// --- 5) click routing order in _onRowsClick ------------------------------
const clickWin = (() => {
    const m = /function _onRowsClick\(e\) \{[\s\S]*?\n\}/.exec(jsSrc);
    assert.ok(m, '_onRowsClick not found');
    return m[0];
})();
check('6 [data-settings-tab] handled before the cancel branch',
    clickWin.indexOf('[data-settings-tab]') !== -1 &&
    clickWin.indexOf('[data-settings-tab]') < clickWin.indexOf('.wq-cancel-btn'));

// --- 6) _gotoSettingsTab deep-link form ----------------------------------
const gotoWin = (() => {
    const m = /function _gotoSettingsTab\(tab\) \{[\s\S]*?\n\}/.exec(jsSrc);
    assert.ok(m, '_gotoSettingsTab not found');
    return m[0];
})();
check('7 replaceState to ?...#settings then one popstate dispatch',
    countRe(gotoWin, /replaceState\(/g) === 1 &&
    countRe(gotoWin, /\?\$\{p\.toString\(\)\}#settings/g) === 1 &&
    countRe(gotoWin, /new Event\('popstate'\)/g) === 1);

// --- 7) the generic branch survives --------------------------------------
check('8 escapeHtml(run.error_message) still appears once (other errors unchanged)',
    countNeedle(jsSrc, 'escapeHtml(run.error_message)') === 1);

// --- 8) behaviour test on the extracted _renderRow -----------------------
const renderM = /function _renderRow\(run\) \{[\s\S]*?\n\}/.exec(jsSrc);
const needleM = /const NO_MODEL_NEEDLE = '[^']*';/.exec(jsSrc);
const isNoM = /function _isNoModelError\(msg\) \{[^\n]*\}/.exec(jsSrc);
check('9 _renderRow + helpers sliceable from the shipped source',
    renderM && needleM && isNoM);
if (renderM && needleM && isNoM) {
    const body = needleM[0] + '\n' + isNoM[0] + '\n' + renderM[0] +
        '\nreturn _renderRow(run);';
    const sandbox = {
        escapeHtml: (s) => String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
        WIZARD_TYPE_LABEL: {},
        STATUS_BADGE_CLASS: {},
        TERMINAL_STATUSES: new Set(['failed', 'completed', 'cancelled']),
        _formatDuration: () => null,
        out1: null,
        out2: null,
    };
    const code =
        'const renderFn = new Function("run", ' + JSON.stringify(body) + ');' +
        'out1 = renderFn({wizard_type:"node",status:"failed",target_name:"t",' +
        'started_at:null,duration_seconds:null,id:"r1",retry_of_id:null,' +
        'can_cancel:false,can_retry:false,progress:0,' +
        'error_message:"Your wizard model X is unreachable: ' + NEEDLE + '. Set it"});' +
        'out2 = renderFn({wizard_type:"node",status:"failed",target_name:"t",' +
        'started_at:null,duration_seconds:null,id:"r2",retry_of_id:null,' +
        'can_cancel:false,can_retry:false,progress:0,error_message:"boom <b>"});';
    vm.runInNewContext(code, vm.createContext(sandbox));
    check('10 no-model error renders the link html, not the raw text',
        String(sandbox.out1).includes('data-testid="wizard-no-model"') &&
        String(sandbox.out1).includes('Add a model endpoint in Settings') &&
        !String(sandbox.out1).includes(NEEDLE));
    check('11 generic error stays escaped, no link',
        String(sandbox.out2).includes('&lt;b&gt;') &&
        !String(sandbox.out2).includes('wizard-no-model'));
}

console.log('wizard-no-model: ' + passed + ' checks passed, ' + failed + ' failed');
if (failed > 0) throw new Error(failed + ' check(s) failed');
