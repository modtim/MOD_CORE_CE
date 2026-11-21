'use strict';
// 2400LR-relinkXSS — CE Users "Re-link login" notice wrote SERVER/USER-INFLUENCED
// text straight into innerHTML (users.js ~:848-865):
//   success msg interpolates result.old_keycloak_user_id / new_keycloak_user_id /
//   matched_by (can carry an email) / reason_for_reprovision, then
//   `noticeHost.innerHTML = \`<div ...>${msg}</div>\``;
//   failure interpolates `detail` from _formatApiError the same way.
// None were escaped -> HTML-injection sink on a platform-admin page.
// Fix = escape every interpolated server value with the escaper already in scope
// (_escapeHtml, users.js:646) at BOTH innerHTML writes; the showInfo/showError
// branches (ui.js sets textContent) keep the RAW string - no double escaping.
//
//   node --test CORE/Dashboard/tests/users-relink-escape.check.js
//
// Cases:
//   E0 the escaper already in scope is _escapeHtml (no second escaper added)
//   E1 escaper turns <img src=x onerror=alert(1)> into &lt;img ... (no live tag)
//   E2 success innerHTML write escapes the msg pieces
//   E3 failure innerHTML write escapes `detail`
//   E4 showInfo/showError still get the RAW string (textContent path)
//   E5 old unescaped pattern gone (HEAD users.js fails here = old-file proof)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const usersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');
const results = [];

function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

// --- E0: exactly one escaper in users.js, the pre-existing _escapeHtml ------
const escaperDecls = (usersSrc.match(/^function _escapeHtml\(/gm) || []).length;
check('E0a', escaperDecls === 1, `users.js must keep exactly one _escapeHtml decl (got ${escaperDecls})`);
check('E0b', /function _escapeHtml\(s\) \{/.test(usersSrc), 'the in-scope escaper is _escapeHtml(s)');
check('E0c', !/function escapeHtml\(/.test(usersSrc), 'no second escaper named escapeHtml was added to users.js');

// --- extract the escaper verbatim from the shipped source (no copy-drift) ---
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

const escSrc = extractFn(usersSrc, 'function _escapeHtml(s)');
check('E0d', !!escSrc, '_escapeHtml must exist in users.js');

// --- E1: the XSS payloads the brief names are neutralised ------------------
if (escSrc) {
    const esc = eval('(' + escSrc + ')');
    const matchedBy = '<img src=x onerror=alert(1)>';
    const outMatched = esc(matchedBy);
    check('E1a', outMatched.includes('&lt;img'), `matched_by must render as &lt;img (got ${JSON.stringify(outMatched)})`);
    check('E1b', !/<img/.test(outMatched), 'no live <img tag survives escaping');
    const detail = "boom <script>alert('x')</script>";
    const outDetail = esc(detail);
    check('E1c', outDetail.includes('&lt;script&gt;'), `detail must render as &lt;script&gt; (got ${JSON.stringify(outDetail)})`);
    check('E1d', !/<script/.test(outDetail), 'no live <script tag survives escaping');
    check('E1e', esc(null) === '', 'escaper tolerates null');
    check('E1f', esc('plain text') === 'plain text', 'plain text passes through untouched');
} else {
    ['E1a', 'E1b', 'E1c', 'E1d', 'E1e', 'E1f'].forEach(n => check(n, false, 'escaper unavailable'));
}

// --- E2: success write escapes the msg pieces ------------------------------
const successWrite = /noticeHost\.innerHTML = `<div class="mt-2 p-2 bg-green-50[^`]*\$\{_escapeHtml\((msgText|msg)\)\}<\/div>`;/.test(usersSrc);
check('E2a', successWrite, 'success noticeHost.innerHTML must interpolate the msg via _escapeHtml');
check('E2b', /msgText = `Account re-linked\./.test(usersSrc), 'success msg is built as msgText (raw text for showInfo)');
check('E2c', /msgText = `New account created and emailed\./.test(usersSrc), 'the reprovision branch also builds msgText');

// --- E3: failure write escapes `detail` ------------------------------------
const failWrite = /noticeHost\.innerHTML = `<div class="mt-2 p-2 bg-red-50[^`]*Failed to re-link: \$\{_escapeHtml\(detail\)\}<\/div>`;/.test(usersSrc);
check('E3a', failWrite, 'failure noticeHost.innerHTML must interpolate detail via _escapeHtml');

// --- E4: textContent branches keep the RAW string (no double escaping) -----
check('E4a', /showInfo\(msgText, 'Re-link login'\);/.test(usersSrc), 'showInfo gets the raw msgText');
check('E4b', !/showInfo\(_escapeHtml\(/.test(usersSrc), 'showInfo must NOT be handed escaped text');
check('E4c', /showError\(`Failed to re-link: \$\{detail\}`, 'Re-link login failed'\);/.test(usersSrc),
    'showError gets the raw detail in its template');
check('E4d', !/showError\(_escapeHtml\(/.test(usersSrc), 'showError must NOT be handed escaped text');

// --- E5: the old unescaped writes are gone (HEAD users.js fails here) ------
const staleSuccess = /noticeHost\.innerHTML = `<div class="mt-2 p-2 bg-green-50[^`]*\$\{msg\}<\/div>`;/.test(usersSrc);
const staleFail = /noticeHost\.innerHTML = `<div class="mt-2 p-2 bg-red-50[^`]*Failed to re-link: \$\{detail\}<\/div>`;/.test(usersSrc);
check('E5a', !staleSuccess, 'success write must not interpolate a bare ${msg}');
check('E5b', !staleFail, 'failure write must not interpolate a bare ${detail}');

const failed = results.filter(r => !r[1]);
if (failed.length) {
    console.log(`users-relink-escape.check.js: ${failed.length}/${results.length} FAILED`);
    process.exit(1);
}
console.log(`users-relink-escape.check.js: all ${results.length} checks passed -> OK`);
