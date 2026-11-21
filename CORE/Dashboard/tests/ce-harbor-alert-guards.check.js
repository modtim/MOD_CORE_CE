// FIX-DASH-HARBOR-ALERT-GUARDS-CE-EDITION-CHECK-IN-UI-JS (owner 11-12): CE ships no
// Harbor, so the Dashboard must NEVER show a Harbor alert. The two alert sites in
// js/ui.js (the /harbor/status 404 branch: "Harbor Setup Required", and the catch
// branch: "Unable to Verify Harbor Credentials") must each sit inside an
// `if (!(await _isCeEdition())) {` guard (same predicate/style as the other CE
// gates in ui.js; paid keeps its original wording, the check is false on paid).
//
// Text-reading check (style of admin-ingress-allowlist.check.js / the source-pin
// asserts in tenant-registry-quota-ce-hidden.check.js): asserts on the SHIPPED
// ui.js text, so an unguarded site = FAIL.
//
//   node CORE/Dashboard/tests/ce-harbor-alert-guards.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const uiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');

// The guard text the sites must carry, in the file's own style.
const GUARD = 'if (!(await _isCeEdition())) {';

function blockFrom(src, anchor) {
    // Slice the brace-matched block whose '{' follows `anchor`.
    const at = src.indexOf(anchor);
    assert.ok(at >= 0, `anchor not found in ui.js: ${anchor}`);
    const open = src.indexOf('{', at + anchor.length);
    assert.ok(open > at, `no '{' after ${anchor}`);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(at, i + 1); }
    }
    throw new Error('unbalanced braces after ' + anchor);
}

// The workflow-builder click handler holds both Harbor alert sites.
const handlerAnchor = 'document.getElementById("workflow-builder-link")?.addEventListener("click", async function(e) {';
const handlerAt = uiSrc.indexOf(handlerAnchor);
assert.ok(handlerAt >= 0, 'workflow-builder click handler not found in ui.js');

// --- site 1: the 404 branch ----------------------------------------------
const s404 = blockFrom(uiSrc, 'if (response.status === 404) {');
const site1Title = "title: 'Registry Setup Required'";
assert.ok(s404.includes(site1Title), 'site 1 alert present in 404 branch');
assert.ok(s404.includes(GUARD), 'FAIL: site 1 (404 alert) not inside an _isCeEdition guard');
assert.ok(s404.indexOf(GUARD) < s404.indexOf(site1Title),
    'FAIL: guard must WRAP the showAlertWithAction call (guard before the alert)');

// --- site 2: the catch branch ---------------------------------------------
const catchAnchor = "console.error('Error checking registry status:', error);";
const catchAt = uiSrc.indexOf(catchAnchor);
assert.ok(catchAt >= 0, 'catch branch not found in ui.js');
const tail = uiSrc.slice(catchAt);
const site2Title = "title: 'Unable to Verify Registry Credentials'";
assert.ok(tail.includes(site2Title), 'site 2 alert present in catch branch');
const guardInCatch = tail.match(new RegExp(GUARD.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
assert.ok(guardInCatch, 'FAIL: site 2 (catch alert) not inside an _isCeEdition guard');
assert.ok(tail.indexOf(GUARD) < tail.indexOf(site2Title),
    'FAIL: catch guard must WRAP the alert call');
// The guard must close before the handler's tail (the guard block contains the
// alert, nothing else grew in the catch): brace-match the guard block.
const guardBlock = blockFrom(tail, GUARD);
assert.ok(guardBlock.includes(site2Title), 'FAIL: site 2 alert not INSIDE the guard block');

// Exactly the two Harbor showAlertWithAction titles exist in ui.js (no third
// ungarded Harbor alert was added elsewhere).
const harborTitles = (uiSrc.match(/title: '[^']*Harbor[^']*'/g) || []);
assert.strictEqual(harborTitles.length, 2, `expected 2 Harbor alert titles, got ${harborTitles.length}`);

// The predicate exists and is fail-closed on unknown (an errored entitlements
// payload counts as CE, so CE never shows the alert).
const predAt = uiSrc.indexOf('function _isCeEdition() {');
assert.ok(predAt >= 0, '_isCeEdition not found in ui.js');
let depth = 0;
for (let i = predAt + 'function _isCeEdition() '.length; i < uiSrc.length; i++) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') { depth--; if (!depth) { var predEnd = i; break; } }
}
const predSrc = uiSrc.slice(predAt, predEnd + 1);
// Fail-closed: CE == "not positively paid", so an errored/unreadable
// entitlements payload counts as CE and can never reveal the alert.
assert.ok(/return _ceEditionState\(\)\.then\(\(s\) => s !== 'paid'\);/.test(predSrc),
    'FAIL: _isCeEdition must be fail-closed (CE = not positively paid)');

console.log('ok  ce-harbor-alert-guards: both Harbor alert sites guarded by !(await _isCeEdition())');
