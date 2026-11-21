// BUG.B41 (owner 10-08): Settings -> Integrations -> Mobile App, the "new enrollment
// code" button did nothing. Root cause: the panel's refresh button re-fetched
// GET /onboarding/mobile-enrollment, which is a PURE function of public config,
// so it returned a byte-identical code + QR every press (panel looked frozen).
//
// Fix: js/mobile-enrollment.js binds #me-refresh to issueNewCode(), which POSTs
// /onboarding/mobile-enrollment/new-code (the API adds a per-issuance nonce OUTSIDE
// the `d` payload, so the app's parser is untouched) and then re-renders BOTH the
// code text and the QR through renderEnrollment().
//
// This check verifies:
//   1) js/mobile-enrollment.js parses as a valid ES module.
//   2) #me-refresh is bound to issueNewCode (not the old load()).
//   3) issueNewCode POSTs the new-code endpoint (method POST, correct path).
//   4) the response is rendered into BOTH the code textarea and the QR element.
//   5) no fetch path in the module is /v1-prefixed (API_URL already carries /v1).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const JS_REL = 'js/mobile-enrollment.js';
const src = fs.readFileSync(path.join(ROOT, JS_REL), 'utf8');

// --- 1) ES-module parse -----------------------------------------------------
let parseErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8' });
} catch (e) {
    parseErr = String(e && (e.stderr || e.message));
}
assert.ok(!parseErr,
    `${JS_REL}: not valid as an ES module - ${(parseErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) #me-refresh bound to issueNewCode -----------------------------------
assert.ok(/getElementById\(\s*'me-refresh'\s*\)[\s\S]{0,200}?addEventListener\(\s*'click'\s*,\s*issueNewCode\s*\)/
    .test(src),
    `${JS_REL}: #me-refresh click handler must be issueNewCode, not the pure-config load()`);
assert.ok(/async\s+function\s+issueNewCode\s*\(/.test(src),
    `${JS_REL}: issueNewCode() must be defined`);

// --- 3) POST to the new-code endpoint ---------------------------------------
const post = /fetch\(\s*`\$\{[^}]*\}\/onboarding\/mobile-enrollment\/new-code`\s*,\s*\{[^}]*method:\s*'POST'/s
    .exec(src);
assert.ok(post,
    `${JS_REL}: must fetch(\`.../onboarding/mobile-enrollment/new-code\`, { method: 'POST' ... })`);

// --- 4) response re-renders code text AND QR --------------------------------
assert.ok(/function\s+renderEnrollment\s*\(/.test(src),
    `${JS_REL}: shared renderEnrollment() renderer must exist`);
assert.ok(/getElementById\(\s*'me-code'\s*\)[\s\S]{0,120}?\.value\s*=/.test(src),
    `${JS_REL}: renderEnrollment must write the code text into #me-code`);
assert.ok(/getElementById\(\s*'me-qr'\s*\)[\s\S]{0,160}?\.innerHTML\s*=/.test(src),
    `${JS_REL}: renderEnrollment must re-render the QR in #me-qr`);
const issueBody = /async\s+function\s+issueNewCode[\s\S]*?\n\}/.exec(src);
assert.ok(issueBody && /renderEnrollment\(/.test(issueBody[0]),
    `${JS_REL}: issueNewCode must call renderEnrollment() on the fresh response`);

// --- 5) no /v1-prefixed fetch paths -----------------------------------------
for (const call of src.match(/fetch\(\s*`[^`]*`/g) || []) {
    const lit = call.replace(/\$\{[^}]*\}/g, '');
    assert.ok(!/\/v1\/onboarding/.test(lit),
        `${JS_REL}: fetch path must not be /v1-prefixed - got "${lit}"`);
}

console.log('BUG.B41 check passed: new-code button POSTs a fresh code and re-renders code + QR');
