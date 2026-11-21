// BUG.B2 (owner 10-08): Dashboard Users → Held Matches tab throws
// "fetchApi is not defined" at runtime. Root cause: held-matches.js line 50
// imported only { me } from './api.js' while lines 59-70 called fetchApi()
// — a bare reference that never resolved.
//
// Fix: export fetchApi from api.js; import fetchApi in held-matches.js.
//
// This check verifies the three invariants that fix the bug:
//   1) held-matches.js imports fetchApi (syntax check as ES module passes).
//   2) Every fetchApi path in held-matches.js does NOT start with /v1/
//      (API_BASE_URL already carries /v1, so double-prefix would 404).
//   3) held-matches.js parses as a valid ES module (node --input-type=module
//      --check via stdin).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// --- 1) module-mode syntax: ES module parse ---------------------------------
const heldMatchesSrc = read('js/held-matches.js');
const apiSrc = read('js/api.js');
for (const [name, src] of [['js/api.js', apiSrc], ['js/held-matches.js', heldMatchesSrc]]) {
    let err = null;
    try {
        execFileSync(process.execPath,
            ['--input-type=module', '--check'],
            { input: src, encoding: 'utf8' });
    } catch (e) {
        err = String(e && (e.stderr || e.message));
    }
    assert.ok(!err,
        `${name}: not valid as an ES module — ${(err || '').split('\n')[0] || 'unknown'}`);
}

// --- 2) fetchApi IS imported from api.js -----------------------------------
assert.ok(/\bexport\s+\{\s*fetchApi\s*\}/.test(apiSrc),
    'js/api.js: fetchApi must be re-exported at bottom of file');
assert.ok(/\bimport\s+\{[^}]*fetchApi[^}]*\}/.test(heldMatchesSrc),
    'js/held-matches.js: must import fetchApi from ./api.js');

// --- 3) no fetchApi path starts with /v1/ -----------------------------------
const fetchCalls = heldMatchesSrc.match(
    /fetchApi\s*\(\s*`([^`]+)`/g
) || [];
for (const call of fetchCalls) {
    const match = call.match(/fetchApi\s*\(\s*`([^`]+)`/);
    const rawPath = match ? match[1] : '';
    // Strip template expressions (e.g. `${userId}`) then check the literal prefix.
    const literalPath = rawPath.replace(/\$\{[^}]+\}/g, '');
    assert.ok(!literalPath.startsWith('/v1/'),
        `js/held-matches.js: fetchApi path must not start with /v1/ — got "${literalPath}" in: ${call}`);
}

// --- 4) api.js still exports all the named objects used by neighbour pages --
const expectedExports = [
    'exchangeCodeForToken', 'getPlatformFeatures', 'me', 'cogs', 'tags',
    'locks', 'users', 'invitations', 'externalInvites', 'secrets',
    'webhooks', 'storageConnections', 'tenantStorage', 'adminStorage',
    'tenantDsar', 'platformDsarRequests', 'securityNotices', 'accessReviews',
    'complianceProfiles', 'permissions', 'permissionRequests', 'apiKeys',
    'burst', 'burstAccounts', 'notifications', 'files',
];
for (const name of expectedExports) {
    const re = new RegExp(`\\bexport\\b[\\s\\S]{0,80}?\\b${name}\\b`);
    assert.ok(re.test(apiSrc),
        `js/api.js: ${name} must still be exported`);
}

// PG-14 (megaplan 1113): the `destinations` export was dead — no caller and no
// `/destinations/` router. It must be gone; logStreams is the replacement.
assert.ok(!/\bexport\s+const\s+destinations\s*=/m.test(apiSrc),
    'js/api.js: dead `destinations` export must be removed (PG-14)');
assert.ok(/\bexport\s+const\s+logStreams\b/.test(apiSrc),
    'js/api.js: `logStreams` export must remain (replaces destinations)');

// --- 5) held-matches.js still imports utils correctly ----------------------
assert.ok(/\bimport\s+\{\s*withButtonBusy\s*\}\s+from\s+['"]\.\/utils\.js['"]/.test(heldMatchesSrc),
    'js/held-matches.js: must import withButtonBusy from ./utils.js');

console.log('bug-b2-held-matches-import: module OK, fetchApi imported, no /v1/ prefix, '
    + `${expectedExports.length} api.js exports intact -> OK`);
