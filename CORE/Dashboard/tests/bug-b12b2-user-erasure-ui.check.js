// BUG.B12b2 — Dashboard "Data requests" → Person (user) erasure UI.
//
// Verifies:
//   1) js/api.js + js/data-requests.js parse as valid ES modules.
//   2) tenantDsar.requestUserErasure / confirmUserErasure hit the exact
//      /tenant/dsar/users/{id}/erasure[/{rid}/confirm] paths.
//   3) The card exists: select id="dr-user-select", typed email confirm,
//      request + confirm buttons, countdown, explanatory line.
//   4) The single-use token is never written into the DOM (no
//      `.textContent = ` / innerHTML assignment involving the token var),
//      and it lives in a module variable only.
//   5) Buttons are disabled until the typed email matches; 409 legal-hold
//      message is surfaced.
//   6) The existing bug-b12a tenant-picker check still passes.

'use strict';

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function parseModule(rel) {
    const src = read(rel);
    try {
        execFileSync(process.execPath,
            ['--input-type=module', '--check'],
            { input: src, encoding: 'utf8' });
        return '';
    } catch (e) {
        return String(e && (e.stderr || e.message));
    }
}

// --- 1) both modules parse -------------------------------------------------
const apiErr = parseModule('js/api.js');
assert.ok(!apiErr,
    `js/api.js: not valid ES module — ${(apiErr || '').split('\n').filter(Boolean)[0] || 'unknown'}`);

const jsSrc = read('js/data-requests.js');
const jsErr = parseModule('js/data-requests.js');
assert.ok(!jsErr,
    `js/data-requests.js: not valid ES module — ${(jsErr || '').split('\n').filter(Boolean)[0] || 'unknown'}`);

const apiSrc = read('js/api.js');
const htmlSrc = read('pages/data-requests.html');

// --- 2) api.js helpers hit the exact user-erasure paths ---------------------
assert.ok(apiSrc.includes('requestUserErasure:'),
    'js/api.js: tenantDsar must expose requestUserErasure');
assert.ok(apiSrc.includes('confirmUserErasure:'),
    'js/api.js: tenantDsar must expose confirmUserErasure');
assert.ok(/requestUserErasure:\s*\(userId\)\s*=>\s*\n?\s*fetchApi\(`\/tenant\/dsar\/users\/\$\{userId\}\/erasure`/.test(apiSrc),
    'js/api.js: requestUserErasure must POST /tenant/dsar/users/{user_id}/erasure');
assert.ok(/confirmUserErasure:\s*\(userId,\s*requestId,\s*token\)\s*=>\s*\n?\s*fetchApi\(`\/tenant\/dsar\/users\/\$\{userId\}\/erasure\/\$\{requestId\}\/confirm`/.test(apiSrc),
    'js/api.js: confirmUserErasure must POST /tenant/dsar/users/{user_id}/erasure/{request_id}/confirm');
assert.ok(apiSrc.includes("requestUserErasure: (userId) =>") &&
          apiSrc.includes("method: 'POST'"),
    'js/api.js: requestUserErasure must be a POST via fetchApi');
assert.ok(apiSrc.includes('users as usersApi') ||
          jsSrc.includes('users as usersApi'),
    'js/data-requests.js: must import users from api.js as usersApi');
assert.ok(jsSrc.includes('usersApi.get('),
    'js/data-requests.js: must load users via usersApi.get()');
assert.ok(jsSrc.includes('tenantDsar.requestUserErasure(') &&
          jsSrc.includes('tenantDsar.confirmUserErasure('),
    'js/data-requests.js: must call both user-erasure helpers');

// --- 3) card + select + typed confirm exist --------------------------------
assert.ok(htmlSrc.includes('id="dr-user-select"'),
    'pages/data-requests.html: must have user picker select id="dr-user-select"');
assert.ok(htmlSrc.includes('id="dr-user-email-input"'),
    'pages/data-requests.html: must have typed-confirm email input');
assert.ok(htmlSrc.includes('id="dr-user-erasure-btn"'),
    'pages/data-requests.html: must have Request erasure button');
assert.ok(htmlSrc.includes('id="dr-user-confirm-btn"'),
    'pages/data-requests.html: must have Confirm erasure button');
assert.ok(htmlSrc.includes('id="dr-user-countdown"'),
    'pages/data-requests.html: must have countdown span');
assert.ok(htmlSrc.includes('id="dr-user-pending"'),
    'pages/data-requests.html: must have pending section container');
const emailInputTag = htmlSrc.match(/id="dr-user-email-input"[^>]*>/s);
assert.ok(emailInputTag, 'email input tag must exist');
assert.ok(emailInputTag[0].includes('autocomplete="off"'),
    'typed-confirm email input must keep autocomplete="off"');
assert.ok(!emailInputTag[0].includes('list='),
    'typed-confirm email input must NOT have a datalist');
assert.ok(htmlSrc.includes('Person (user) erasure'),
    'pages/data-requests.html: card heading must be "Person (user) erasure"');
assert.ok(htmlSrc.includes('Removes this person') &&
          htmlSrc.includes('anonymised form (ISO 27001)') &&
          htmlSrc.includes('Legal holds block erasure'),
    'pages/data-requests.html: explanatory line must mention anonymised records + legal holds');

// --- 4) token never written to the DOM --------------------------------------
const tokenDomWrite = /(\.textContent\s*=\s*[^;]*\b(token|_userPending\.(token)))/
    .test(jsSrc) || /(\.innerHTML\s*=\s*[^;]*\b(token|_userPending\.(token)))/.test(jsSrc);
assert.ok(!tokenDomWrite,
    'js/data-requests.js: the confirm token must NEVER be written into the DOM');
assert.ok(!/console\.(log|error|info|warn)\([^)]*_userPending\.token/.test(jsSrc),
    'js/data-requests.js: the token must never be logged');
assert.ok(jsSrc.includes('let _userPending = null;'),
    'js/data-requests.js: token must be kept in the _userPending module variable');
assert.ok(jsSrc.includes('token: res.token,'),
    'js/data-requests.js: request response token stored into _userPending');

// --- 5) disabled until typed email matches; 409 legal-hold message ----------
assert.ok(jsSrc.includes('dr-user-email-input'),
    'js/data-requests.js: must wire the typed email confirm input');
assert.ok(jsSrc.includes('_userEmailExpected'),
    'js/data-requests.js: must track the expected email for the typed confirm');
assert.ok(/erasureBtn\.disabled\s*=\s*!matches \|\| _userPending !== null/.test(jsSrc),
    'js/data-requests.js: Request button must stay disabled until the typed email matches');
assert.ok(jsSrc.includes('legal hold') && jsSrc.includes('409'),
    'js/data-requests.js: 409 legal-hold must get a dedicated message');
assert.ok(jsSrc.includes('err.status === 403 || err.status === 404'),
    'js/data-requests.js: 403/404 not-allowed must be surfaced');
assert.ok(jsSrc.includes("getElementById('dr-user-confirm-btn')"),
    'js/data-requests.js: countdown must toggle the user Confirm button');
assert.ok(jsSrc.includes('tickUserCountdown'),
    'js/data-requests.js: user countdown must mirror the tenant countdown');

// --- 6) existing bug-b12a check still passes --------------------------------
const b12a = path.join(ROOT, 'tests', 'bug-b12a-tenant-erasure-picker.check.js');
assert.ok(fs.existsSync(b12a), 'bug-b12a check file must still exist');
let b12aOut = '';
try {
    b12aOut = execFileSync(process.execPath, [b12a], { encoding: 'utf8' });
} catch (e) {
    assert.ok(false,
        `bug-b12a-tenant-erasure-picker.check.js must still pass — ${(String(e.stderr || e.message)).split('\n').slice(0, 6).join(' | ')}`);
}
assert.ok(b12aOut.includes('all assertions passed'),
    'bug-b12a check output must still report OK');

console.log('bug-b12b2-user-erasure-ui: OK');
