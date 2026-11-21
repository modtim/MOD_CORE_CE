'use strict';
// 2400LR-g6rbac — CE Users -> Roles "Create Role" showed "[object Object]"
// because the 402 body is a NESTED FastAPI detail:
//   {detail: {detail: "Your plan doesn't include granular custom RBAC...", code: "rbac_custom_required"}}
// (CORE/API/services/rbac_service.py:86). users.js threw `error.detail || ...`
// straight, so the object stringified. Fix = route the /rbac/roles throws
// through the existing _formatApiError helper (users.js:186) and teach that
// helper's object branch to unwrap {detail: string} / {message: string}.
//
//   node --test CORE/Dashboard/tests/ce-rbac-role-402-message.check.js
//
// Cases:
//   H1 helper unwraps the nested 402 body -> the paid sentence, not [object Object]
//   H2 helper unchanged for string / 422-array / structured (name_collision) bodies
//   H3 all three /rbac/roles throw sites use the helper (create/update/delete)
//   H4 old pattern gone (HEAD users.js fails this file: old-file proof)
//   H5 showError renders server text via textContent, never innerHTML
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const usersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');
const uiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui.js'), 'utf8');
const results = [];

function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

const PAID = "Your plan doesn't include granular custom RBAC. Custom roles cannot be created or edited.";

// --- extract the helper verbatim from the shipped source (no copy-drift) ----
function extractFn(src, marker) {
    const start = src.indexOf(marker);
    if (start < 0) return null;
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') {
            depth--;
            if (depth === 0) return src.slice(start, i + 1);
        }
    }
    return null;
}

const fnSrc = extractFn(usersSrc, 'function _formatApiError(body, fallback)');
check('H0', !!fnSrc, '_formatApiError must exist in users.js');

let fmt = null;
if (fnSrc) fmt = eval('(' + fnSrc + ')');

// --- H1: the nested 402 body feeds the helper -> readable paid sentence -----
if (fmt) {
    const nested = { detail: { detail: PAID, code: 'rbac_custom_required' } };
    const out = fmt(nested, 'Failed to create role: 402');
    check('H1', out === PAID, `nested {detail:{detail,msg}} must yield the paid sentence (got ${JSON.stringify(out)})`);
    check('H1b', out !== '[object Object]', 'must not be [object Object]');

    // --- H2: no regressions on the other FastAPI shapes -------------------
    check('H2a', fmt({ detail: 'plain string' }, 'fb') === 'plain string', 'string detail as-is');
    const arr = fmt({ detail: [{ loc: ['body', 'name'], msg: 'invalid name', type: 'x' }] }, 'fb');
    check('H2b', arr === 'name: invalid name', '422 array still joined field: msg');
    const structured = fmt({ detail: { code: 'name_collision', existing: 7 } }, 'fb');
    check('H2c', structured === JSON.stringify({ code: 'name_collision', existing: 7 }),
        'structured payload without a string inner keeps its JSON dump');
    check('H2d', fmt({ detail: { message: PAID, code: 'rbac_custom_required' } }, 'fb') === PAID,
        '{detail:{message:string}} unwraps too');
    check('H2e', fmt(null, 'fb') === 'fb', 'null body -> fallback');
} else {
    ['H1', 'H1b', 'H2a', 'H2b', 'H2c', 'H2d', 'H2e'].forEach(n => check(n, false, 'helper unavailable'));
}

// --- H3: the /rbac/roles throw sites route through the helper --------------
const createSite = /throw new Error\(_formatApiError\(error, `Failed to create role: \$\{createResponse\.status\}`\)\)/.test(usersSrc);
const updateSite = /throw new Error\(_formatApiError\(error, `Failed to update role: \$\{updateResponse\.status\}`\)\)/.test(usersSrc);
const deleteSite = /throw new Error\(_formatApiError\(error, `Failed to delete role: \$\{response\.status\}`\)\)/.test(usersSrc);
check('H3a', createSite, 'create-role throw must use _formatApiError');
check('H3b', updateSite, 'update-role throw must use _formatApiError');
check('H3c', deleteSite, 'delete-role throw must use _formatApiError');

// --- H4: the raw-object throw is gone (HEAD users.js fails here) ----------
const stale = /throw new Error\(error\.detail \|\| `Failed to (create|update|delete) role:/.test(usersSrc);
check('H4', !stale, 'no /rbac/roles throw may pass error.detail raw');

// --- H5: server text reaches the DOM as text, never HTML ------------------
const showErrorBody = uiSrc.slice(uiSrc.indexOf('export async function showError('), uiSrc.indexOf('export async function showError(') + 1600);
check('H5a', /msgEl\.textContent = typeof message === 'string' \? message : String\(message\)/.test(showErrorBody),
    'ui.js showError sets #alert-error-message via textContent');
check('H5b', !/innerHTML/.test(showErrorBody), 'showError must not touch innerHTML');
check('H5c', /showError\(`Failed to create role: \$\{error\.message\}`\)/.test(usersSrc),
    'create-role catch still feeds showError the thrown message');

const failed = results.filter(r => !r[1]);
if (failed.length) {
    console.log(`ce-rbac-role-402-message.check.js: ${failed.length}/${results.length} FAILED`);
    process.exit(1);
}
console.log(`ce-rbac-role-402-message.check.js: all ${results.length} checks passed -> OK`);
