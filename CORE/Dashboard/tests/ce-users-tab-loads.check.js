'use strict';
// FIX-CE-USERS-TAB-LOADS-ON-CE-1108 - the Users tab must render rows on CE.
//
// CORE/Dashboard/js/users.js:513 loadTenantsForUsersTab() is the ONLY thing
// tab activation calls for the Users panel (users.js:262 main-panel-users,
// users.js:461 super-admin default mount). Its CE branch (:515-:518) hid the
// PAID-only tenant selector and then returned bare, so loadUsers() (users.js:570,
// the only writer of `#users-table tbody`, table markup CORE/Dashboard/pages/
// users.html:126, empty state renderUsers users.js:597) never ran on CE and the
// tbody stayed empty -> e2e CORE/e2e/tests/admin-identity.spec.ts:82
// `poll(table.locator('tbody tr').count()).toBeGreaterThan(0)` was RED.
//
// Fix = the CE branch still hides the selector and never fetches /tenants
// (CE is single-tenant), but loads its OWN tenant's users before returning.
//
//   node --test CORE/Dashboard/tests/ce-users-tab-loads.check.js
//
// Cases:
//   C0 the function and its CE guard exist
//   C1 the CE branch loads users before it returns (HEAD users.js FAILS here)
//   C2 the CE branch still hides the tenant selector and never reveals it
//   C3 the CE branch never fetches /tenants and never passes a tenantId
//   C4 the PAID branch is unchanged (tenant selector + deferred load)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const usersSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');

function braceSlice(src, start) { // src[start] must be '{' -> slice through its match
    let depth = 0;
    for (let i = start; i < src.length; i++) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}

const fnStart = usersSrc.indexOf('async function loadTenantsForUsersTab()');
assert.ok(fnStart >= 0, 'loadTenantsForUsersTab must exist in users.js');
const fnBodyStart = usersSrc.indexOf('{', fnStart);
const fnBody = braceSlice(usersSrc, fnBodyStart);
assert.ok(fnBody, 'loadTenantsForUsersTab body must parse');

const guardAt = fnBody.indexOf('if (!await isPaidEdition())');
assert.ok(guardAt >= 0, 'CE guard `if (!await isPaidEdition())` must gate the tenant selector');
const ceBlock = braceSlice(fnBody, fnBody.indexOf('{', guardAt));
assert.ok(ceBlock, 'CE branch block must parse');

// comments must not satisfy the load/return assertions (a commented-out
// loadUsers() is not a call)
const ceCode = ceBlock.replace(/\/\/[^\n]*/g, '');

// --- C1: CE loads users, and does it BEFORE returning ----------------------
const loadIdx = ceCode.search(/\bloadUsers\s*\(\s*\)/);
assert.ok(loadIdx >= 0,
    'CE branch must call loadUsers() (own tenant, no tenantId) - otherwise #users-table tbody stays empty on CE');
const retIdx = ceCode.search(/\breturn\s*;/);
assert.ok(retIdx >= 0, 'CE branch must still return early (deny before the tenants fetch)');
assert.ok(loadIdx < retIdx, 'loadUsers() must run before the CE early return, not after it');

// --- C2: still CE - selector hidden, never revealed ------------------------
assert.ok(/getElementById\('users-tenant-container'\)/.test(ceBlock), 'CE branch still looks up users-tenant-container');
assert.ok(/display\s*=\s*'none'/.test(ceBlock), 'CE branch hides users-tenant-container (PAID-only selector)');
assert.ok(!/display\s*=\s*'block'/.test(ceBlock) && !/removeProperty\('display'\)/.test(ceBlock),
    'CE branch must not reveal the tenant selector');

// --- C3: no cross-tenant read on CE ---------------------------------------
assert.ok(!/fetch\(/.test(ceBlock) && !/\/tenants/.test(ceBlock),
    'CE branch must not fetch /tenants (CE is single-tenant; no tenant list)');
assert.ok(!/loadUsers\s*\(\s*[^)\s]/.test(ceCode),
    'CE loadUsers() must take no tenantId - own tenant only, never a selected tenant');

// --- C4: PAID branch unchanged --------------------------------------------
const paidPart = fnBody.slice(fnBody.indexOf('try {'));
assert.ok(/\/\/ Don't load users yet - wait for tenant selection/.test(paidPart), 'paid branch still defers the load until tenant selection');
assert.ok(/loadUsers\(selectedTenantId\)/.test(paidPart), 'paid branch still loads the selected tenant on change');
assert.ok(/tenantSelect\.addEventListener\('change'/.test(paidPart), 'paid tenant selector still has its change listener');
assert.ok(/loadUsers\(\);/.test(paidPart), 'paid non-platform-admin path still loads own tenant');

// --- C5: loadUsers signature untouched (additive call only) ---------------
assert.ok(/async function loadUsers\(tenantId = null\)/.test(usersSrc), 'loadUsers(tenantId = null) signature unchanged');
assert.ok(/querySelector\("#users-table tbody"\)/.test(usersSrc), 'loadUsers still targets #users-table tbody');

console.log('ce-users-tab-loads.check.js: all asserts passed');
