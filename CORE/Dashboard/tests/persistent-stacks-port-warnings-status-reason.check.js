// Node-runnable check for FIX-DASH-STACK-PORT-WARNINGS-AND-STATUS-REASON (D76).
// The Stacks page must (1) render the import-compose Preview's `warnings` list
// (a fixed host port already in use on every licensed worker, naming the holder)
// as amber notes above the Create button, and (2) show a stack row's `status_reason`
// (CORE/API/models.py:8400; the busy-port case ends the stack `failed` with the
// same reason) under the status badge — amber while deploying, red once failed —
// and carry it in the row's compact signature so a changed reason re-renders the
// row. This script extracts _psEsc plus the two marker-delimited blocks
// (PS-IMPORT-WARNINGS / PS-STATUS-REASON) and _psStackSig from the source text
// (same pattern as persistent-stacks-compose-error-escape.check.js) and asserts:
// warnings render escaped, none -> no element; deploying/failed rows show the
// reason (red only when failed); no reason -> no element; a changed reason
// changes the signature; markup-looking text never produces elements.
//
//   node CORE/Dashboard/tests/persistent-stacks-port-warnings-status-reason.check.js
//   node <this> <alt-src-path>   (mutation proof against a scratch copy)
'use strict';
const fs = require('fs');
const path = require('path');

const srcPath = process.argv[2] || path.join(__dirname, '..', 'js', 'persistent-stacks.js');
const src = fs.readFileSync(srcPath, 'utf8');

function grabBetween(startMarker, endMarker, label) {
    const a = src.indexOf(startMarker);
    const b = src.indexOf(endMarker);
    if (a < 0 || b < 0 || b <= a) {
        console.error(`FAIL could not locate the ${label} block in persistent-stacks.js`);
        process.exit(1);
    }
    return src.slice(a, b);
}

const escStart = src.indexOf('function _psEsc(s) {');
const escEnd = src.indexOf('\nfunction _psMsg');
if (escStart < 0 || escEnd < 0) {
    console.error('FAIL could not locate _psEsc in persistent-stacks.js');
    process.exit(1);
}
const escBlock = src.slice(escStart, escEnd);

const warnBlock = grabBetween('// PS-IMPORT-WARNINGS-BEGIN', '// PS-IMPORT-WARNINGS-END', 'import-warnings');
const reasonBlock = grabBetween('function _psStatusReason(s) {', '// PS-STATUS-REASON-END', 'status-reason');

// _psStackSig: from its header to the first column-0 closing brace.
const sigStart = src.indexOf('function _psStackSig(s) {');
const sigEnd = sigStart < 0 ? -1 : src.indexOf('\n}', sigStart);
if (sigStart < 0 || sigEnd < 0) {
    console.error('FAIL could not locate _psStackSig in persistent-stacks.js');
    process.exit(1);
}
const sigBlock = src.slice(sigStart, sigEnd + 2);

const warnFn = new Function('p', 'html', escBlock + '\n' + warnBlock + '\nreturn html;');
const F = new Function(escBlock + '\n' + reasonBlock + '\n' + sigBlock +
    '\nconst _psTransitLabel = (s) => null;' +
    '\nreturn { reason: _psStatusReason, sig: _psStackSig };')();

let failures = 0;
let passes = 0;
function check(name, cond) {
    if (cond) { passes++; console.log(`ok   ${name}`); }
    else { failures++; console.log(`FAIL ${name}`); }
}

// (a) warnings render as amber notes, one child per warning, above the base html
const holder = 'Host port 8080/tcp is already used by proj-old-server';
const out2 = warnFn({ warnings: [holder, 'second note'], compose_spec: {} }, 'BASE');
check('warnings render element', out2.includes('data-testid="ps-import-warnings"'));
check('one child per warning', (out2.match(/<div class="text-amber-800">/g) || []).length === 2);
check('notes above base html', out2.startsWith('<div class="mt-2') && out2.endsWith('BASE'));

// (a2) no warnings -> nothing rendered (base html unchanged)
const out0 = warnFn({ compose_spec: {} }, 'BASE');
check('no warnings -> no element', out0 === 'BASE' && !out0.includes('ps-import-warnings'));
check('empty list -> no element', warnFn({ warnings: [] }, 'BASE') === 'BASE');

// (e) an HTML-looking warning is escaped
const evilW = '<img src=x onerror=alert(1)> on port 80';
const outEvil = warnFn({ warnings: [evilW] }, 'BASE');
check('warning markup escaped', !outEvil.includes('<img') && outEvil.includes('&lt;img'));

// (b) deploying row shows the reason in amber
const dep = F.reason({ status: 'deploying', status_reason: holder });
check('deploying shows reason', dep.includes('data-testid="ps-status-reason"') && dep.includes('text-amber-700') && dep.includes(holder));

// (c) failed row shows the reason in red
const fail = F.reason({ status: 'failed', status_reason: holder });
check('failed shows reason red', fail.includes('data-testid="ps-status-reason"') && fail.includes('text-red-700') && fail.includes(holder));

// created also shows it (amber)
check('created shows reason', F.reason({ status: 'created', status_reason: holder }).includes('ps-status-reason'));

// (c2) no reason -> no element
check('no reason -> no element', F.reason({ status: 'failed', status_reason: '' }) === '' && F.reason({ status: 'deploying' }) === '');
// other statuses do not paint the reason
check('running hides reason', F.reason({ status: 'running', status_reason: holder }) === '');

// markup-looking reason is escaped
const evilR = '<b>port 8080</b> <img src=x onerror=alert(1)>';
const outEvilR = F.reason({ status: 'failed', status_reason: evilR });
check('reason markup escaped', !outEvilR.includes('<b>') && outEvilR.includes('&lt;b&gt;'));

// (d) changing the reason changes the signature; same reason does not
const s1 = { id: 7, name: 'n', status: 'deploying', status_reason: holder };
const s2 = { id: 7, name: 'n', status: 'deploying', status_reason: 'Host port 9090/tcp is already used by other' };
const s1b = { id: 7, name: 'n', status: 'deploying', status_reason: holder };
check('changed reason changes sig', F.sig(s1) !== F.sig(s2));
check('same reason same sig', F.sig(s1) === F.sig(s1b));

// the row renderer actually calls the helper in the Status cell, and the sig
// carries the reason key
check('row calls helper', src.includes('${_psStatusReason(s)}'));
check('sig carries reason key', /sr: \(typeof s\.status_reason/.test(src));

console.log(`RESULT ${passes}/${failures}`);
process.exit(failures ? 1 : 0);
