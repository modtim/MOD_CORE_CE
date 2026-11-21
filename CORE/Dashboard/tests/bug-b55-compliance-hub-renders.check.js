// BUG.B55 — the Dashboard Compliance page (/#compliance-center) shows nothing.
//
// Root cause: js/audit-logs.js still ran its prev/next pagination listeners and
// its first loadAuditLogs() at MODULE LEVEL. hub-compliance.js now statically
// imports that module (BUG.B29a Part A), so the statements ran at IMPORT time,
// before the hub had injected pages/audit-logs.html into #main-panel-audit-logs.
// document.getElementById('prev-page') is null then -> TypeError during module
// evaluation -> audit-logs.js fails to evaluate -> hub-compliance.js fails to
// evaluate -> ui.js's optionalPageModule('./hub-compliance.js', ...) swallows it
// and returns null -> the Compliance hub renders nothing at all.
//
// Verifies:
//   1. audit-logs.js evaluates with a null-returning getElementById (the harness)
//   2. no module-level unguarded DOM wiring survives in audit-logs.js
//   3. the wiring still exists, guarded, inside _wire()
//   4. hub-compliance.js still statically imports audit-logs.js (the trigger)
//   5. ui.js still resolves the hub through optionalPageModule (the swallow)
//
//   node CORE/Dashboard/tests/bug-b55-compliance-hub-renders.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const auditJs = read('js/audit-logs.js');
const hubJs = read('js/hub-compliance.js');
const uiJs = read('js/ui.js');

// --- 1) The harness: evaluate audit-logs.js as if nothing were rendered ----
// Strip the ES-module surface (imports -> stubs, export -> plain decl) so the
// BODY runs standalone, exactly the way module evaluation runs it. The stub DOM
// returns null for every getElementById, which is the state the browser is in
// when hub-compliance.js imports audit-logs.js before injecting its markup.
function bodyOf(src) {
    let out = src.replace(/^\s*import\s*\{[^}]*\}\s*from\s*['"][^'"]*['"];?\s*$/gm, '');
    out = out.replace(/^\s*export\s+(async\s+function|function|const|let|class)/gm, '$1');
    return out;
}

// The stub DOM: every getElementById returns null, which is exactly the state the
// browser is in when hub-compliance.js imports audit-logs.js — the hub has not yet
// injected pages/audit-logs.html into #main-panel-audit-logs, so #prev-page /
// #next-page / #audit-logs-container do not exist.
const STUBS = `
    class _EmptyDom {
        getElementById() { return null; }
        querySelectorAll() { return []; }
        querySelector() { return null; }
        createElement() { return null; }
        addEventListener() {}
        getItem() { return null; }
    }
    const fetchApi = async () => null;
    const withButtonBusy = async () => null;
    const document = new _EmptyDom();
    const window = new _EmptyDom();
    const sessionStorage = new _EmptyDom();
`;

function evaluateBodyWithEmptyDom(rel, src) {
    const code = STUBS + bodyOf(src);
    vm.runInNewContext(code, vm.createContext({}), { filename: rel });
}

let evalErr = null;
try {
    evaluateBodyWithEmptyDom('js/audit-logs.js', auditJs);
} catch (e) {
    evalErr = e;
}
assert.ok(!evalErr,
    'BUG.B55: audit-logs.js must evaluate with an EMPTY DOM (hub imports it before '
    + 'injecting pages/audit-logs.html). A module-level getElementById(...).addEventListener '
    + 'or loadAuditLogs() throws here: '
    + (evalErr && evalErr.message ? String(evalErr.message).split('\n')[0] : 'unknown'));

// --- 2) No module-level unguarded DOM wiring survives ---------------------
// Indentation cannot tell module level from _wire()'s body (both are 8 spaces
// here), so walk the file tracking brace depth: depth 0 IS module level.
const codeOnly = auditJs
    .replace(/\/\*[\s\S]*?\*\//g, '')            // block comments
    .replace(/\/\/[^\n]*/g, '')                  // line comments
    .replace(/(['"`])(?:\\.|(?!\1)[^\\\n])*\1/g, '""'); // string literals
let depth = 0;
const offenders = [];
for (let i = 0; i < codeOnly.length; i++) {
    const ch = codeOnly[i];
    if (ch === '{' || ch === '(' || ch === '[') { depth++; continue; }
    if (ch === '}' || ch === ')' || ch === ']') { depth--; continue; }
    if (depth !== 0) continue;
    if (/[A-Za-z_$]/.test(ch)) {
        const stmt = codeOnly.slice(i);
        const m = /^(document|window)\.getElementById\s*\([^()]*\)\s*\./.exec(stmt);
        if (m) offenders.push(i + ': ' + m[0]);
        if (/^loadAuditLogs\s*\(\s*\)\s*;/.test(stmt)) offenders.push(i + ': loadAuditLogs();');
    }
}
assert.ok(offenders.length === 0,
    'BUG.B55: audit-logs.js has module-level DOM statements (they run at import time '
    + 'when hub-compliance.js statically imports the module): ' + offenders.join(' | '));

// --- 3) The wiring still exists, guarded, inside _wire() ------------------
assert.ok(/function\s+_wire\s*\(\s*\)\s*\{/.test(auditJs),
    'audit-logs.js must define _wire()');
const wireBody = auditJs.slice(auditJs.indexOf('function _wire()'));
assert.ok(/getElementById\('prev-page'\)/.test(wireBody),
    'audit-logs.js _wire() must still wire #prev-page');
assert.ok(/getElementById\('next-page'\)/.test(wireBody),
    'audit-logs.js _wire() must still wire #next-page');
assert.ok(/btnPrev\)\s*btnPrev\.addEventListener/.test(wireBody),
    'audit-logs.js #prev-page listener must be null-guarded (btnPrev)');
assert.ok(/btnNext\)\s*btnNext\.addEventListener/.test(wireBody),
    'audit-logs.js #next-page listener must be null-guarded (btnNext)');
// 2400LR-cdu: the export no longer wires directly — audit-logs.js:577 CE-wraps
// it in `return isPaidEdition().then((paid) => { if (paid) { _wire(); ... })`.
// Accept either shape, but _wire() must still be INVOKED from the export (and
// only unconditionally or under the paid branch), so a CE-guard that dropped the
// wiring, or a wiring moved into a dead branch, still goes red.
const initIdx = auditJs.search(/export\s+function\s+initializeAuditLogsPage\s*\(\s*\)\s*\{/);
assert.ok(initIdx >= 0,
    'audit-logs.js must export initializeAuditLogsPage()');
const initBody = auditJs.slice(initIdx, initIdx + 1200);
assert.ok(/_wire\(\);/.test(initBody),
    'initializeAuditLogsPage() must invoke _wire() (directly, or under the CE isPaidEdition paid branch)');
// rc20e: the CE hide was wrong (audit-logs is the CE-core tab), so the export now
// wires straight from the opening brace. Accept that shape too, but still reject a
// wiring moved into a dead/negated branch.
assert.ok(/if\s*\(\s*paid\s*\)\s*\{\s*_wire\(\);/.test(initBody) || /;\s*_wire\(\);/.test(initBody)
          || /\{(?:\s*\/\/[^\n]*)*\s*_wire\(\);/.test(initBody),
    'initializeAuditLogsPage() must invoke _wire() unconditionally or under the CE paid branch (not in a dead/negated branch)');

// --- 4) the trigger is still there: hub statically imports audit-logs.js ---
assert.ok(/import\s*\{[^}]*initializeAuditLogsPage[^}]*\}\s*from\s*['"]\.\/audit-logs\.js['"]/.
    test(hubJs),
    'hub-compliance.js must statically import initializeAuditLogsPage from audit-logs.js');
assert.ok(/'audit-logs':\s*\(\)\s*=>\s*initializeAuditLogsPage\(\)/.test(hubJs),
    "hub-compliance.js _CHILD_INIT must map 'audit-logs' to initializeAuditLogsPage()");

// --- 5) the swallow is still there: ui.js resolves the hub guardedly -------
assert.ok(/optionalPageModule\(\s*'\.\/hub-compliance\.js'\s*,\s*'initializeComplianceHubPage'\s*\)/.
    test(uiJs),
    'ui.js must resolve the hub through optionalPageModule (a failed module evaluation '
    + 'becomes null here, which is why the hub silently renders nothing)');

console.log('bug-b55-compliance-hub-renders: OK');
