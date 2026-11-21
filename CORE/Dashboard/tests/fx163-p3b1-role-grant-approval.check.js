'use strict';
// GSEC FX.163 p3b1 — the Dashboard role-grant surface must show the
// "awaiting approval" state.
//
// PUT /rbac/users/{id}/assign-role (CORE/API/routers/rbac.py:199) answers 202
// {status:'awaiting_approval', approval_id} while approvals.require_second_approver
// is ON for the target tenant. js/api.js's fetchApi does not throw on 2xx, so the
// ONLY thing that can tell the user the grant did not run is the JS caller
// branching on the returned body. The single call site is the role-assign
// dropdown handler in js/users.js (js/api.js:512 assignRole -> users.js).
//
// Guards, per call site:
//   * it imports BOTH isAwaitingApproval and showAwaitingApproval from
//     ./approval-pending.js,
//   * it branches on isAwaitingApproval(resp) and renders
//     showAwaitingApproval(<container>, resp, {actionLabel:'Role grant'}),
//   * the awaiting branch RETURNS before the success path, so the row is not
//     re-fetched / highlighted as if the grant had applied.
//
//   node CORE/Dashboard/tests/fx163-p3b1-role-grant-approval.check.js

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const users = fs.readFileSync(path.join(__dirname, '..', 'js', 'users.js'), 'utf8');

// 1) the import brings BOTH halves of the shared helper.
const importLines = users.match(/^import\s*\{[^}]*\}\s*from\s*'\.\/approval-pending\.js';/gm) || [];
assert.ok(importLines.length >= 1,
  'users.js must import the approval-pending helper for the role-grant surface');
assert.ok(importLines.some(l => /isAwaitingApproval/.test(l)),
  'users.js must import isAwaitingApproval from ./approval-pending.js');
assert.ok(importLines.some(l => /showAwaitingApproval/.test(l)),
  'users.js must import showAwaitingApproval from ./approval-pending.js');

// 2) the role-assign dropdown handler branches on the 202 body.
const handlerAt = users.indexOf("querySelector('.role-assign-select')");
assert.ok(handlerAt >= 0, 'role-assign dropdown handler missing from users.js');
const handler = users.slice(handlerAt, handlerAt + 1600);

assert.ok(/=\s*await users\.assignRole\(/.test(handler),
  'the handler must keep the assignRole response so it can inspect the 202 body');
assert.ok(/if\s*\(\s*isAwaitingApproval\(/.test(handler),
  'the handler must branch on isAwaitingApproval(resp)');
assert.ok(/showAwaitingApproval\([^)]*actionLabel:\s*'Role grant'/.test(handler),
  "the awaiting branch must render showAwaitingApproval(..., {actionLabel: 'Role grant'})");

// 3) the awaiting branch must not fall through to the success path: it returns
// before the green highlight + loadUsers() re-fetch that proves a stuck grant.
const awaitingIdx = handler.search(/if\s*\(\s*isAwaitingApproval\(/);
const successIdx = handler.search(/select\.style\.borderColor\s*=\s*'#22c55e'/);
assert.ok(successIdx > awaitingIdx,
  'the success highlight must come after the awaiting branch');
const awaitingWindow = handler.slice(awaitingIdx, successIdx);
assert.ok(/return;/.test(awaitingWindow),
  'the awaiting branch must return: no success message, no re-fetch as if the grant applied');
assert.ok(!/loadUsers\(/.test(awaitingWindow.split(/return;/)[0]),
  'the awaiting branch must not re-fetch the users table before returning');

console.log('fx163-p3b1 role-grant approval wiring OK');
