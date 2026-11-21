// Bug B5b2 check — Re-link login button on the admin users page.
//
// The API (admin_mfa.py) exposes:
//   GET  /admin/users/{user_id}/identity/status
//   POST /admin/users/{user_id}/identity/relink
//
// The Dashboard (users.js) must:
//   - Call identity/status per row and only render the "Re-link login" button
//     when linked === false; otherwise show "Login linked" in muted text.
//   - On click, open a confirm modal with explanation, a reason textarea
//     (required, min 5 chars), and an "I understand" checkbox that gates
//     Submit.
//   - On 200 show the action result in plain English.
//   - On errors show the API detail inline.
//   - Paths must NEVER include /v1 (api.js owns the mount point).
//
//   node --check CORE/Dashboard/tests/bug-b5b2-relink-button.check.js
//   node CORE/Dashboard/tests/bug-b5b2-relink-button.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const jsDir = path.join(__dirname, '..', 'js');
const cssPath = path.join(__dirname, '..', 'tailwind.css');

// ===========================================================================
// 1) API paths: no /v1 prefix in Dashboard JS code
// ===========================================================================
const apiSrc = fs.readFileSync(path.join(jsDir, 'api.js'), 'utf8');

// Must not contain /v1 in any admin/users identity path.
assert.ok(
    !/\/v1\/admin\/users.*identity/.test(apiSrc),
    'api.js must not hardcode /v1 in identity paths'
);

// Must define both methods.
assert.ok(
    /identityStatus/.test(apiSrc),
    'api.js must export identityStatus'
);
assert.ok(
    /relinkIdentity/.test(apiSrc),
    'api.js must export relinkIdentity'
);

// The identity/status path must appear.
assert.ok(
    /\/admin\/users\/\$\{userId\}\/identity\/status/.test(apiSrc),
    'api.js must call GET /admin/users/{userId}/identity/status'
);

// The identity/relink path must appear with POST + body.
assert.ok(
    /\/admin\/users\/\$\{userId\}\/identity\/relink/.test(apiSrc),
    'api.js must call POST /admin/users/{userId}/identity/relink'
);
assert.ok(
    /relinkIdentity[\s\S]*?POST/.test(apiSrc),
    'relinkIdentity must use POST'
);

// ===========================================================================
// 2) users.js: row rendering gates button on linked===false
// ===========================================================================
const usersSrc = fs.readFileSync(path.join(jsDir, 'users.js'), 'utf8');

// Must call identityStatus for the relink button.
assert.ok(
    /identityStatus/.test(usersSrc),
    'users.js must call identityStatus'
);

// Must show "Re-link login" button (bg-sky-600).
assert.ok(
    /Re-link login/.test(usersSrc),
    'users.js must render "Re-link login" button text'
);
assert.ok(
    /bg-sky-600.*text-white|text-white.*bg-sky-600/.test(usersSrc),
    'Re-link login button must use bg-sky-600 + text-white'
);

// Must show "Login linked" (muted) when linked is false → no, when linked IS true.
assert.ok(
    /Login linked/.test(usersSrc),
    'users.js must show "Login linked" for already-linked users'
);

// Must branch on linked status.
assert.ok(
    /linked.*===.*false|linked.*!==.*true|!.*linked/.test(usersSrc),
    'users.js must gate the button on linked === false'
);

// ===========================================================================
// 3) Modal: reason textarea + checkbox gating
// ===========================================================================
assert.ok(
    /relink-reason/.test(usersSrc),
    'users.js must render a reason textarea (relink-reason)'
);
assert.ok(
    /minlength="5"/.test(usersSrc) || /minlength=5/.test(usersSrc),
    'reason textarea must have minlength 5'
);
assert.ok(
    /relink-confirm/.test(usersSrc),
    'users.js must render an "I understand" checkbox (relink-confirm)'
);
assert.ok(
    /I understand/.test(usersSrc),
    'checkbox must say "I understand"'
);
assert.ok(
    /submitBtn.*disabled|disabled.*submitBtn|submitBtn\.disabled/.test(usersSrc),
    'submit must be gated by validation (checkbox + reason length)'
);
assert.ok(
    /validate/.test(usersSrc),
    'users.js must have a validate function for gating'
);

// ===========================================================================
// 4) Error rendering: API errors surfaced inline, not swallowed
// ===========================================================================
assert.ok(
    /error\.detail/.test(usersSrc),
    'must surface API error.detail'
);
assert.ok(
    /Failed to re-link/.test(usersSrc),
    'error path must show "Failed to re-link"'
);
// Must use _formatApiError for API error rendering.
assert.ok(
    /_formatApiError/.test(usersSrc),
    'must use _formatApiError for error rendering'
);
// Must also use showError as fallback.
assert.ok(
    /showError/.test(usersSrc),
    'must use showError as fallback for error display'
);

// ===========================================================================
// 5) No /v1 in users.js fetch paths
// ===========================================================================
// users.js must never call fetchApi with /v1 in the path.
const codeNoComments = usersSrc
    .split('\n')
    .filter(l => !/^\s*\/\//.test(l))
    .join('\n');
assert.ok(
    !/\/v1\/admin\/users.*identity/.test(codeNoComments),
    'users.js must not hardcode /v1 in identity paths'
);

// ===========================================================================
// 6) Re-link button colour: the button must stay bg-sky-600, not bg-blue-600
// ===========================================================================
const cssSrc = fs.readFileSync(cssPath, 'utf8');
// bg-blue-600 is legitimately in the built CSS now (88d753dd3 rebuilt
// tailwind.css for the billing usage bars). The gate's real intent is that the
// Re-link button's own markup must not use it.
const relinkMarkup = usersSrc.match(/<[^>]*relink-login-button[^>]*>/g) || [];
assert.ok(
    relinkMarkup.length > 0,
    'users.js must render the Re-link login button markup'
);
assert.ok(
    !relinkMarkup.some(m => /bg-blue-600/.test(m)),
    'the Re-link login button must not use bg-blue-600'
);
// bg-sky-600 must be present (used by the Re-link button).
assert.ok(
    /\.bg-sky-600/.test(cssSrc),
    'tailwind.css must define .bg-sky-600'
);
// text-white must be present (used with bg-sky-600).
assert.ok(
    /\.text-white/.test(cssSrc),
    'tailwind.css must define .text-white'
);

// ===========================================================================
// 7) Action result text in success path
// ===========================================================================
assert.ok(
    /relinked/.test(usersSrc),
    'must display relinked action result'
);
assert.ok(
    /New account created/.test(usersSrc) || /new account/.test(usersSrc.toLowerCase()),
    'must display new account created result'
);
assert.ok(
    /email/.test(usersSrc.toLowerCase()),
    'result text must mention email'
);

// ===========================================================================
// 8) relink-status placeholder class in row template
// ===========================================================================
assert.ok(
    /relink-status/.test(usersSrc),
    'row template must have relink-status placeholder'
);
assert.ok(
    /data-linked/.test(usersSrc),
    'relink-status must carry data-linked attribute'
);

console.log('bug-b5b2-relink-button: all checks passed');
