'use strict';
// 
//
// Owner report (Dell CE): "there is no way to make the ui change for the user".
// Diagnosis DIAG-DELL-ACCOUNT-PAGE-NOTHING-EDITABLE-D72.md F-1: the Edit profile
// form EXISTS and ships on CE, but it sat two cards below the read-only
// Account Information card, and on CE the two cards above it are hidden, so the
// form was easy to miss; the no-op save message read like a dead button.
//
// Fix (layout/UX only, no API change): the Edit profile card moves directly
// under the Account Information card, the info card gets a read-only hint, and
// the no-op message names the fields. Every id and handler stays.
//
//   node CORE/Dashboard/tests/account-edit-profile-under-info-card.check.js
//
// Asserts:
//   A1 card order: Account Information card, then the Edit profile card, then
//      the org-settings card (its old position) - the CE gap above the form is
//      gone, and nothing else was shoved into it.
//   A2 the read-only hint sits on the Account Information card, exactly once.
//   A3 ids: every id on the page is unique, and every id account.js looks up
//      still exists (no handler lost, no id renamed).
//   A4 account.js no-op message is the new wording; the old string is gone.
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const ACCOUNT = fs.readFileSync(path.join(ROOT, 'pages', 'account.html'), 'utf8');
const JS = fs.readFileSync(path.join(ROOT, 'js', 'account.js'), 'utf8');

// --- A1 card order ----------------------------------------------------------
const infoAt = ACCOUNT.indexOf('Account Information</h2>');
const editAt = ACCOUNT.indexOf('<!-- Edit Profile Card');
const orgAt = ACCOUNT.indexOf('id="org-settings-card"');
const ssoAt = ACCOUNT.indexOf('id="sso-linked-methods-card"');
assert.ok(infoAt >= 0 && editAt >= 0 && orgAt >= 0 && ssoAt >= 0, 'all anchor cards present');
assert.ok(infoAt < editAt, 'the Edit profile card must follow the Account Information card');
assert.ok(editAt < orgAt,
    'the Edit profile card must sit DIRECTLY under the info card, above the CE-hidden cards ' +
    `(old order had it after id="org-settings-card" at ${orgAt})`);
assert.ok(orgAt < ssoAt, 'the CE-hidden cards keep their own relative order');

// the block between the info card and the org-settings card is exactly the
// Edit profile card (nothing else was moved into the gap).
const gap = ACCOUNT.slice(editAt, orgAt);
assert.ok(/<form id="profile-edit-form"/.test(gap), 'the Edit profile form is in the gap');
assert.ok(!/id="(org-settings-card|sso-linked-methods-card|account-permissions)"/.test(gap),
    'no other card moved into the gap');

// --- A2 the read-only hint --------------------------------------------------
const HINT = 'To change these details, use Edit profile below.';
const hintCount = ACCOUNT.split(HINT).length - 1;
assert.ok(hintCount === 1, `the info card must carry the hint exactly once (found ${hintCount})`);
assert.ok(ACCOUNT.indexOf(HINT) < orgAt, 'the hint belongs to the Account Information card');

// --- A3 ids unchanged -------------------------------------------------------
const ids = [...ACCOUNT.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
const dupes = ids.filter((x, i) => ids.indexOf(x) !== i);
assert.ok(dupes.length === 0, `no id may be duplicated on the page (duplicates: ${dupes})`);
assert.ok(ids.length >= 40, 'the page keeps its full set of ids');

// every id account.js looks up that the page used to carry must still be there
// (getElementById lookups are by id, so order cannot matter; this proves no id
// was renamed or dropped).
const jsRefs = [...JS.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]);
assert.ok(jsRefs.length >= 30, 'account.js id lookups found');
const pageIds = new Set(ids);
for (const ref of new Set(jsRefs)) {
    if (ref === 'page-content') continue; // shared shell element, not on this page
    assert.ok(pageIds.has(ref), `account.js looks up #${ref}; account.html must still carry it`);
}
for (const must of ['profile-edit-form', 'profile-first-name', 'profile-last-name',
    'profile-username', 'profile-save-btn', 'profile-edit-status', 'account-name',
    'account-username', 'account-avatar']) {
    assert.ok(ids.includes(must), `id "${must}" must be present`);
}

// --- A4 the no-op message ---------------------------------------------------
const NEWMSG = 'Nothing changed yet. Edit a field above, then Save.';
assert.ok(JS.includes(NEWMSG), 'account.js must show the new no-op message');
assert.ok(!/No changes to save\./.test(JS), 'the old "No changes to save." wording must be gone');

console.log('account-edit-profile-under-info-card.check.js: OK');
