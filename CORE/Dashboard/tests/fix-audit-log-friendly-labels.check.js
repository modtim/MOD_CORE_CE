'use strict';
// FIX-AUDIT-LOG-FRIENDLY-LABELS (Tim option A, 11-15 ~10:35 PM CT; proposal D16
// 7a629ff1e2).  The Audit Logs page showed raw METHOD:/road... keys: formatAction
// mapped only the 7 permission_* keys and the detail view printed log.action raw.
// This check guards the applied fix:
//   * roadActionLabel() exists and maps METHOD:/road... (+ the legacy /workflows
//     alias) to the 7 CE-neutral labels, ids ignored,
//   * formatAction falls through actionMap -> roadActionLabel -> raw gray pill,
//   * the detail view now uses formatAction(log.action) (no raw <p> form left),
//   * the export builder still copies the stored key verbatim (action: l.action /
//     action: e.event_name),
//   * the module still parses as an ES module.
//
// The file under test is js/audit-logs.js next to this check, unless
// AUDIT_LOGS_JS names another path (the mutant scratch copy).
//
//   node CORE/Dashboard/tests/fix-audit-log-friendly-labels.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const JS_PATH = process.env.AUDIT_LOGS_JS || path.join(ROOT, 'js', 'audit-logs.js');
const js = fs.readFileSync(JS_PATH, 'utf8');

// --- 1) module parses -------------------------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: js, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `${JS_PATH}: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) slice the two functions by name, evaluate them ---------------------
function extractFn(src, name) {
    const re = new RegExp('function\\s+' + name + '\\s*\\(');
    const start = js.search(re);
    assert.ok(start >= 0, `${JS_PATH}: no function ${name} found`);
    let i = js.indexOf('{', start);
    let depth = 0;
    for (let j = i; j < js.length; j++) {
        const c = js[j];
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) return js.slice(start, j + 1); }
    }
    assert.ok(false, `${JS_PATH}: unbalanced braces for function ${name}`);
}

const srcFormat = extractFn(js, 'formatAction');
const srcRoad = extractFn(js, 'roadActionLabel');
assert.ok(/actionMap\[action\]/.test(srcFormat), 'formatAction: no actionMap lookup');
assert.ok(/roadActionLabel\(action\)/.test(srcFormat),
    'formatAction: fallback chain does not call roadActionLabel(action)');
assert.ok(/label:\s*action/.test(srcFormat),
    'formatAction: raw-key fallback ({ label: action }) missing');

// roadActionLabel must be in scope for formatAction, so compile both together.
const fns = new Function(srcRoad + '\n' + srcFormat + '\nreturn [roadActionLabel, formatAction];')();
const road = fns[0];
const format = fns[1];
assert.ok(typeof road === 'function' && typeof format === 'function', 'both functions evaluated');

// --- 3) the 7-label table over the representative keys ---------------------
// [key, label, color]
const EXPECT = [
    ['POST:/road', 'Workflow created', 'blue'],
    ['POST:/road/42/execute', 'Workflow run', 'green'],
    ['PUT:/road/42', 'Workflow updated', 'yellow'],
    ['DELETE:/road/42', 'Workflow removed', 'red'],
    ['POST:/road/plan-from-examples', 'Plan generated', 'blue'],
    ['POST:/road/plan-from-outcome', 'Plan generated', 'blue'],
    ['POST:/road/runs/7/files', 'Run files updated', 'yellow'],
    ['GET:/road/42', 'Workflow viewed', 'gray'],
    // legacy /workflows alias (pre-rename rows)
    ['POST:/workflows', 'Workflow created', 'blue'],
    ['POST:/workflows/42/execute', 'Workflow run', 'green'],
    ['PUT:/workflows/42', 'Workflow updated', 'yellow'],
    // the 7 permission_* keys keep their old labels
    ['permission_granted', 'Granted', 'green'],
    ['permission_revoked', 'Revoked', 'red'],
    ['permission_updated', 'Updated', 'yellow'],
    ['permission_requested', 'Requested', 'blue'],
    ['permission_request_approved', 'Approved', 'green'],
    ['permission_request_denied', 'Denied', 'red'],
    ['permission_request_cancelled', 'Cancelled', 'gray'],
];
const seen = [];
for (const [key, label, color] of EXPECT) {
    const html = format(key);
    const want = `<span class="px-2 py-1 rounded-full text-xs font-medium bg-${color}-100 text-${color}-800">${label}</span>`;
    assert.ok(html === want,
        `key ${JSON.stringify(key)} rendered ${JSON.stringify(html)} not ${JSON.stringify(want)}`);
    seen.push(key);
}

// --- 4) unmapped keys render verbatim, gray, never blank -------------------
for (const raw of ['foo:bar', '', 'GET:/somewhere/else']) {
    const html = format(raw);
    assert.ok(html.includes(raw), `unmapped key ${JSON.stringify(raw)} rendered blank or mangled`);
    assert.ok(/bg-gray-100 text-gray-800/.test(html),
        `unmapped key ${JSON.stringify(raw)} is not a gray pill`);
}

// --- 5) detail view uses formatAction, raw form gone -----------------------
assert.ok(/<p class="text-gray-900">\$\{formatAction\(log\.action\)\}<\/p>/.test(js),
    'detail view: Action <p> does not use formatAction(log.action)');
assert.ok(!/<p class="text-gray-900">\$\{log\.action\}<\/p>/.test(js),
    'detail view: the raw <p class="text-gray-900">${log.action}</p> form is still there');

// --- 6) export builder stays raw ------------------------------------------
assert.ok(/action:\s*l\.action,/.test(js), 'export builder: action: l.action was changed');
assert.ok(/action:\s*e\.event_name,/.test(js), 'export builder: action: e.event_name was changed');

process.stdout.write(`fix-audit-log-friendly-labels: ${seen.length + 3} keys checked, file ${JS_PATH}\n`);
