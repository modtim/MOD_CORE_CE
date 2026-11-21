// Node-runnable static check for the GSEC FX.163 p3b2a tenant full-export page.
//
// Guards the FE wiring only (the API side is CORE/API/routers/tenant/export.py):
//   * the page ships the start button, the jobs table, the empty state and the error box,
//   * js/tenant-export.js exports initializeTenantExportPage and calls the TWO routes the
//     router actually has (POST + GET /tenant/export, written WITHOUT a /v1 prefix — the
//     Dashboard's API_BASE_URL already carries /v1, so a /v1/v1 path is a bug),
//   * the 202 awaiting-approval answer is rendered by the SHARED approval-pending module
//     (js/approval-pending.js), not by a hand-rolled copy,
//   * the poll is a backoff (5 s base, doubling, 60 s cap) with a real STOP condition
//     (no running job / hidden tab) and is NOT a fixed setInterval,
//   * no innerHTML anywhere: every server string goes through textContent / setAttribute.
//
//   node CORE/Dashboard/tests/tenant-export.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'tenant-export.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '..', 'js', 'tenant-export.js'), 'utf8');

// --- the shipped page -------------------------------------------------------
assert.ok(/<h1[^>]*>\s*Tenant full export/.test(html), 'the page title is missing');
assert.ok(/id="te-start-btn"/.test(html), 'the Start full export button is missing');
assert.ok(/Start full export/.test(html), 'the button label must say Start full export');
assert.ok(/every workflow, every file and output, and the audit log/i.test(html),
  'the page must explain what the export includes');
assert.ok(/id="te-table-body"/.test(html), 'the jobs table body is missing');
assert.ok(/id="te-empty"/.test(html), 'the empty state is missing');
assert.ok(/id="te-error"/.test(html), 'the error box is missing');
['Job', 'Status', 'Started', 'Size', 'Download'].forEach((h) => {
  assert.ok(new RegExp('>' + h + '</th>').test(html), `the jobs table needs a ${h} column`);
});

// --- the init export + the routes ------------------------------------------
assert.ok(/export async function initializeTenantExportPage\(/.test(js),
  'initializeTenantExportPage must be an exported async entry point');
assert.ok(/fetchApi\(\s*['"`]\/tenant\/export['"`]\s*,\s*\{\s*method:\s*'POST'/.test(js),
  'the start call must POST /tenant/export with no /v1 prefix');
assert.ok(/fetchApi\(\s*`\/tenant\/export\?/.test(js),
  'the jobs read must GET /tenant/export with no /v1 prefix');
assert.ok(!/\/v1\/v1/.test(js + html), 'no /v1/v1 path (API_BASE_URL already carries /v1)');
assert.ok(!/fetchApi\(\s*['"`]\/v1/.test(js), 'paths never carry a /v1 prefix');

// --- FX.161 p2b: the export-approvals section ------------------------------
assert.ok(/id="te-approvals"/.test(html), 'the Export approvals section is missing');
assert.ok(/id="te-approvals-body"/.test(html), 'the approvals table body is missing');
assert.ok(/id="te-approvals-empty"/.test(html), 'the approvals empty state is missing');
assert.ok(/No export requests waiting for approval/.test(html),
  'the approvals empty state must say No export requests waiting for approval');
['Requested by', 'Requested at', 'Expires', 'Status', 'Actions'].forEach((h) => {
  assert.ok(new RegExp('>' + h + '</th>').test(html), `the approvals table needs a ${h} column`);
});
assert.ok(/id="te-reject-reason"/.test(html), 'the reject-reason field is missing');
assert.ok(/Reject reason/.test(html), 'the reject-reason field must be labelled');

// the three approval routes, all WITHOUT a /v1 prefix (API_BASE_URL carries /v1)
assert.ok(/fetchApi\(\s*['"`]\/tenant\/export-approvals['"`]\s*\)/.test(js),
  'the approvals read must GET /tenant/export-approvals');
assert.ok(/\/tenant\/export-approvals\/\$\{[^}]*\}\/approve/.test(js),
  'the approve call must POST /tenant/export-approvals/{request_id}/approve');
assert.ok(/\/tenant\/export-approvals\/\$\{[^}]*\}\/reject/.test(js),
  'the reject call must POST /tenant/export-approvals/{request_id}/reject');
assert.ok(/\/v1\/tenant\/export-approvals\/\{request_id\}\/approve/.test(html),
  'the page prose must name the approve route');

// the requester's own row never gets an Approve button (the API 403s a self-approve)
assert.ok(/_isOwnRequest\(/.test(js), 'own-request detection must exist');
assert.ok(/a different tenant admin must decide it/.test(js),
  'the own-request row must explain the second-admin rule');
assert.ok(/Approve/.test(js) && /Reject/.test(js), 'pending rows need Approve / Reject buttons');
assert.ok(/decideApproval\(/.test(js), 'the decision handlers must be wired');
assert.ok(/te-reject-error/.test(js), 'a missing reject reason must be shown in the row area');
assert.ok(/await _loadApprovals\(\);/.test(js), 'the section refreshes after a decision');

// no banned blue-600 classes anywhere on the page
assert.ok(!/bg-blue-600|text-blue-600/.test(html), 'no bg-blue-600 / text-blue-600 in the page');
assert.ok(!/bg-blue-600|text-blue-600/.test(js), 'no bg-blue-600 / text-blue-600 in the js');

// --- the shared approval-pending module ------------------------------------
assert.ok(/import\s*\{[^}]*\bisAwaitingApproval\b[^}]*\}\s*from\s*'\.\/approval-pending\.js'/.test(js),
  'isAwaitingApproval must come from js/approval-pending.js');
assert.ok(/import\s*\{[^}]*\bshowAwaitingApproval\b[^}]*\}\s*from\s*'\.\/approval-pending\.js'/.test(js),
  'showAwaitingApproval must come from js/approval-pending.js');
assert.ok(/isAwaitingApproval\(/.test(js), 'the 202 answer must be detected');
assert.ok(/showAwaitingApproval\([^)]*actionLabel:\s*'Full export'/.test(js),
  'the notice must be labelled Full export');
assert.ok(/clearAwaitingApproval\(/.test(js), 'a stale notice must be cleared on retry');

// --- the poll: backoff + a stop, never a fixed setInterval ------------------
assert.ok(!/setInterval\(/.test(js), 'no setInterval: a fixed loop is the self-DDoS bug');
assert.ok(/const POLL_BASE_MS = 5000/.test(js), 'the poll starts at 5 s');
assert.ok(/const POLL_MAX_MS = 60000/.test(js), 'the poll caps at 60 s');
assert.ok(/Math\.min\(_delayMs \* 2, POLL_MAX_MS\)/.test(js), 'the delay doubles with a cap');
assert.ok(/setTimeout\(/.test(js), 'the poll is a setTimeout chain');
assert.ok(/clearTimeout\(/.test(js), 'the timer must be clearable when the poll stops');
assert.ok(/if \(!_anyRunning\(_lastJobs\) && !_anyApprovalRunning\(_approvals\)\) \{[^}]*_delayMs = POLL_BASE_MS; _polling = false; return; \}/.test(js),
  'the poll stops when no job is running and no approval request is pending');
assert.ok(/document\.visibilityState === 'hidden'/.test(js), 'the poll stops while the tab is hidden');
assert.ok(/addEventListener\('visibilitychange', _onVisibility\)/.test(js),
  'the poll resumes on visibilitychange (poller-self-heal)');

// --- no innerHTML, textContent only ----------------------------------------
assert.ok(!/innerHTML/.test(js), 'no innerHTML at all: server data must never be markup');
assert.ok(/textContent/.test(js), 'server strings go through textContent');
assert.ok(/setAttribute\('href', url\)/.test(js), 'the download URL is set via setAttribute');
assert.ok(!/document\.write|eval\(/.test(js), 'no document.write / eval');

console.log('tenant-export.check.js: OK');
