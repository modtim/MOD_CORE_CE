// Bug B40c check — Settings → Integrations → Mobile app: "Build-server signing"
// card for the mobile signing inputs (GET/PUT/DELETE /mobile/signing/inputs).
//
// Requirements:
//  * the card exists in the Mobile sub-panel and ships HIDDEN (platform-admin
//    only — mobile-enrollment.js unhides it after /me confirms is_platform_admin)
//  * rows grouped Android / iOS from the manifest, state chip "Set · <fp>"/"Not set"
//  * `str` inputs render a password field, `file` inputs a file picker read
//    client-side with FileReader -> base64, guarded at 2 MB BEFORE any request
//  * fetch paths never include /v1
//  * secret values are never echoed back: no innerHTML of the value — only the
//    fingerprint chip (textContent) and the plain-input prefill (non-secret)
//
//   node --check CORE/Dashboard/tests/bug-b40c-mobile-signing-form.check.js
//   node CORE/Dashboard/tests/bug-b40c-mobile-signing-form.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'pages', 'settings.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'js', 'mobile-signing.js'), 'utf8');
const wiring = fs.readFileSync(path.join(root, 'js', 'mobile-enrollment.js'), 'utf8');

// ---------------------------------------------------------------------------
// 1. The card lives in the Mobile sub-panel and is hidden by default
// ---------------------------------------------------------------------------
const cardIdx = html.indexOf('id="me-signing-card"');
assert.ok(cardIdx >= 0, 'pages/settings.html: the "Build-server signing" card (#me-signing-card) must exist');
const mobileIdx = html.indexOf('id="int-subpanel-mobile"');
assert.ok(mobileIdx >= 0 && mobileIdx < cardIdx,
    'pages/settings.html: the signing card must live inside the Mobile app sub-panel');
const cardTag = html.slice(cardIdx, cardIdx + 120);
assert.ok(/class="hidden/.test(cardTag),
    'pages/settings.html: #me-signing-card must ship hidden (unhidden only for platform admins)');
assert.ok(html.includes('id="me-signing-groups"') && html.includes('id="me-signing-summary"'),
    'pages/settings.html: the card needs #me-signing-groups and #me-signing-summary render targets');

// ---------------------------------------------------------------------------
// 2. Wiring: the module is imported and initialized from mobile-enrollment.js
// ---------------------------------------------------------------------------
assert.ok(/import \{ initializeMobileSigningInputs \} from '\.\/mobile-signing\.js'/
    .test(wiring), 'js/mobile-enrollment.js: must import initializeMobileSigningInputs');
assert.ok(/initializeMobileSigningInputs\(\)/.test(wiring),
    'js/mobile-enrollment.js: must call initializeMobileSigningInputs()');
assert.ok(/is_platform_admin/.test(js),
    'js/mobile-signing.js: the card must be gated on is_platform_admin');

// ---------------------------------------------------------------------------
// 3. API paths: /mobile/signing/inputs, never /v1
// ---------------------------------------------------------------------------
assert.ok(/\/mobile\/signing\/inputs/.test(js),
    'js/mobile-signing.js: must call /mobile/signing/inputs');
assert.ok(!/\/v1\//.test(js) && !/['"`]\/v1/.test(js),
    'js/mobile-signing.js: fetch paths must never include /v1');
assert.ok(/method: 'PUT'/.test(js) && /method: 'DELETE'/.test(js),
    'js/mobile-signing.js: must PUT the value/file and DELETE on Clear');

// ---------------------------------------------------------------------------
// 4. Row inputs by type: password for str, file picker for file, text for plain
// ---------------------------------------------------------------------------
assert.ok(/type="password"/.test(js),
    'js/mobile-signing.js: str inputs must render a password field');
assert.ok(/type="file"/.test(js),
    'js/mobile-signing.js: file inputs must render a file picker');
assert.ok(/type="text"/.test(js),
    'js/mobile-signing.js: plain inputs must render a text field');

// ---------------------------------------------------------------------------
// 5. FileReader -> base64 with the 2 MB client-side guard
// ---------------------------------------------------------------------------
assert.ok(/_MAX_FILE_BYTES = 2 \* 1024 \* 1024/.test(js),
    'js/mobile-signing.js: the 2 MB cap must be declared client-side');
assert.ok(/file\.size > _MAX_FILE_BYTES/.test(js),
    'js/mobile-signing.js: files over 2 MB must be rejected before any request');
assert.ok(/new FileReader\(\)/.test(js) && /readAsDataURL/.test(js),
    'js/mobile-signing.js: the picked file must be read client-side with FileReader');
assert.ok(/file_b64/.test(js),
    'js/mobile-signing.js: the base64 body key must be file_b64');

// ---------------------------------------------------------------------------
// 6. Secrets never echoed back
// ---------------------------------------------------------------------------
// The only value rendered into innerHTML is the plain (non-secret) prefill.
const innerHtmlUses = js.match(/\$\{[^}]*\}/g) || [];
const secretish = innerHtmlUses.filter(s => /entry\.value|inputEl\.value|file_b64|b64/.test(s));
assert.ok(secretish.every(s => /entry\.value/.test(s)),
    `js/mobile-signing.js: only the plain prefill (${secretish.join(' ')}) may appear in innerHTML — ` +
    'str/file values must never be rendered back');
// State chips update via textContent (inert), never innerHTML.
assert.ok(/chip\.textContent = /.test(js),
    'js/mobile-signing.js: the state chip must be updated with textContent, not innerHTML');
assert.ok(/Set · |Set\$\{fp\}/.test(js) || /Set` : 'Not set'/.test(js),
    'js/mobile-signing.js: the chip must read "Set · <fingerprint>" / "Not set"');

// ---------------------------------------------------------------------------
// 7. Errors shown inline on the row
// ---------------------------------------------------------------------------
assert.ok(/detailOf\(/.test(js) && /setStatus\(statusEl, detailOf\(err\)\)/.test(js),
    'js/mobile-signing.js: API error details must be shown inline on the row');

console.log('bug-b40c-mobile-signing-form: OK');
