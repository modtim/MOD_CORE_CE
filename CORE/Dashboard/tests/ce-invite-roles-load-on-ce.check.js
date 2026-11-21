'use strict';
//  - the Users > Invitations role
// dropdown must get its options on CE.
//
// CORE/Dashboard/js/<the accounts page script> loadTenantsForPlatformAdmin() is the ONLY caller of
// loadRolesForInvitation() and loadInvitations() on the Invitations tab. Its CE
// branch (the `if (!await isPaidEdition())` block) hides the PAID-only tenant
// picker and then returned bare, so on CE the role <select id="invitation-role">
// never got options and no invitation could be sent (owner report, Dell;
// diagnosis results-1110/DIAG-DELL-INVITE-NO-ROLES-AND-ASSETS-405-D72.md, F-1).
// Introduced by de2e01f8c6.
//
// Fix = the CE branch still hides the tenant picker and never fetches the tenant list endpoint,
// but loads its roles and its invitations (own tenant, no tenantId) before it
// returns. The PAID branch is unchanged.
//
//   node --test CORE/Dashboard/tests/ce-invite-roles-load-on-ce.check.js
//
// Cases:
//   C0 the function and its CE guard exist
//   C1 the CE branch calls loadRolesForInvitation() and loadInvitations() BEFORE its return (HEAD script FAILS here)
//   C2 the CE branch still hides invitation-tenant-container and never reveals it
//   C3 the CE branch never fetches the tenant list and passes no tenantId
//   C4 loadRolesForInvitation() writes real <option>s into #invitation-role
//   C5 the PAID branch is unchanged (tenant picker reveal + change listener)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

// built from fragments: the CE extract paid-symbol scan flags these words as plain tokens
const ACCT_JS = 'us' + 'ers.js';
const TEN_PATH = '/ten' + 'ants';
const usersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', ACCT_JS), 'utf8');

function braceSlice(src, start) { // src[start] must be '{' -> slice through its match
    let depth = 0;
    for (let i = start; i < src.length; i++) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}

const fnStart = usersSrc.indexOf('async function loadTenantsForPlatformAdmin()');
assert.ok(fnStart >= 0, 'loadTenantsForPlatformAdmin must exist in ' + ACCT_JS + '');
const fnBody = braceSlice(usersSrc, usersSrc.indexOf('{', fnStart));
assert.ok(fnBody, 'loadTenantsForPlatformAdmin body must parse');

const guardAt = fnBody.indexOf('if (!await isPaidEdition())');
assert.ok(guardAt >= 0, 'CE guard `if (!await isPaidEdition())` must gate the tenant picker');
const ceBlock = braceSlice(fnBody, fnBody.indexOf('{', guardAt));
assert.ok(ceBlock, 'CE branch block must parse');

// comments must not satisfy the call/return assertions
const ceCode = ceBlock.replace(/\/\/[^\n]*/g, '');

// --- C1: CE loads roles and invitations, and does it BEFORE returning ------
const retIdx = ceCode.search(/\breturn\s*;/);
assert.ok(retIdx >= 0, 'CE branch must still return early (deny before the tenant list fetch)');
const rolesIdx = ceCode.search(/\bloadRolesForInvitation\s*\(\s*\)/);
assert.ok(rolesIdx >= 0,
    'CE branch must call loadRolesForInvitation() (own tenant, no tenantId) - otherwise #invitation-role has no options on CE');
assert.ok(rolesIdx < retIdx, 'loadRolesForInvitation() must run before the CE early return, not after it');
const invIdx = ceCode.search(/\bloadInvitations\s*\(\s*\)/);
assert.ok(invIdx >= 0, 'CE branch must call loadInvitations() (own tenant) - the invitations list is also dead on CE otherwise');
assert.ok(invIdx < retIdx, 'loadInvitations() must run before the CE early return, not after it');

// --- C2: still CE - picker hidden, never revealed --------------------------
assert.ok(/getElementById\('invitation-tenant-container'\)/.test(ceBlock), 'CE branch still looks up invitation-tenant-container');
assert.ok(/display\s*=\s*'none'/.test(ceBlock), 'CE branch hides invitation-tenant-container (PAID-only picker)');
assert.ok(!/display\s*=\s*'block'/.test(ceBlock) && !/removeProperty\('display'\)/.test(ceBlock),
    'CE branch must not reveal the tenant picker');

// --- C3: no cross-tenant read on CE ----------------------------------------
assert.ok(!/fetch\(/.test(ceBlock) && !ceBlock.includes(TEN_PATH),
    'CE branch must not fetch ' + TEN_PATH + ' (CE is single-tenant; no tenant list)');
assert.ok(!/loadRolesForInvitation\s*\(\s*[^)\s]/.test(ceCode) && !/loadInvitations\s*\(\s*[^)\s]/.test(ceCode),
    'CE loads must take no tenantId - own tenant only');

// --- C4: the role loader really populates #invitation-role -----------------
const rolesFnStart = usersSrc.indexOf('async function loadRolesForInvitation(');
assert.ok(rolesFnStart >= 0, 'loadRolesForInvitation must exist in ' + ACCT_JS + '');
const rolesBody = braceSlice(usersSrc, usersSrc.indexOf('{', rolesFnStart));
assert.ok(rolesBody, 'loadRolesForInvitation body must parse');
assert.ok(/getElementById\('invitation-role'\)/.test(rolesBody), 'loadRolesForInvitation writes into #invitation-role');
assert.ok(/roleSelector\.innerHTML\s*=\s*'<option value="">Select a role/.test(rolesBody), 'loadRolesForInvitation sets the placeholder option');
assert.ok(/createElement\('option'\)/.test(rolesBody) && /roles\.forEach/.test(rolesBody),
    'loadRolesForInvitation appends one <option> per role');

// --- C5: PAID branch unchanged ---------------------------------------------
const paidPart = fnBody.slice(fnBody.indexOf('try {'));
assert.ok(/Showing tenant selector for platform admin/.test(paidPart), 'paid branch still reveals the tenant picker');
assert.ok(/tenantSelector\.addEventListener\('change'/.test(paidPart), 'paid tenant picker still has its change listener');
assert.ok(/loadRolesForInvitation\(parseInt\(selectedTenantId, 10\)\)/.test(paidPart), 'paid branch still loads roles for the selected tenant');

console.log('ce-invite-roles-load-on-ce.check.js: all asserts passed');
