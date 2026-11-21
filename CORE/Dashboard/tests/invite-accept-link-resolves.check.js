'use strict';
// D74 (Tim, Dell PIN4, BUG-1027b): the team-invite accept link opened to a dead end.
// Link = <DASHBOARD_URL>/accept-invitation.html?token=... (API services/invitations_service.py).
// Cause: accept-invitation.js POSTed `${API_URL}/v1/onboarding/invitations/accept` while
// API_URL already ends in /v1 (inject-config.sh) => /v1/v1/... => 404 {"detail":"Not Found"}.
//
//   node --test CORE/Dashboard/tests/invite-accept-link-resolves.check.js
//
// Cases:
//   L1 the link path built by the API is a page the Dashboard image ships (Dockerfile COPY)
//      and nginx serves it directly (location = ..., no SPA fallback)
//   L2 the page file and its script exist
//   L3 every ${apiBase}/... fetch in the accept page uses a path with no second /v1,
//      given API_URL ends in /v1 (inject-config.sh)
//   L4 the accept + prefill paths are routes the API router serves (onboarding router)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const dash = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(p, 'utf8');
const results = [];
function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

const apiRoot = path.join(dash, '..', 'API');
const svcPath = path.join(apiRoot, 'services', 'invitations_service.py');
const haveApi = fs.existsSync(svcPath);

const docker = read(path.join(dash, 'Dockerfile'));
const nginx = read(path.join(dash, 'nginx.conf'));
const inject = read(path.join(dash, 'inject-config.sh'));
const js = read(path.join(dash, 'js', 'accept-invitation.js'));

let linkPage = 'accept-invitation.html';
if (haveApi) {
    const m = read(svcPath).match(/accept_link":\s*f"\{dashboard_url\.rstrip\('\/'\)\}\/([A-Za-z0-9_.\-]+)\?token=/);
    check('L1a API link builder found', !!m, 'accept_link f-string not found in invitations_service.py');
    if (m) linkPage = m[1];
}
check('L1b Dockerfile ships the linked page at the web root',
    new RegExp(`COPY --from=builder /app/pages/${linkPage.replace(/\./g, '\\.')} \\.`).test(docker),
    `no COPY of pages/${linkPage} to the nginx root`);
check('L1c nginx serves the linked page directly (no SPA fallback)',
    new RegExp(`location = /${linkPage.replace(/\./g, '\\.')} \\{[^}]*try_files \\$uri =404`).test(nginx),
    `no exact location for /${linkPage}`);
check('L2a page file exists', fs.existsSync(path.join(dash, 'pages', linkPage)), linkPage);
check('L2b page loads its script', /js\/accept-invitation\.js/.test(read(path.join(dash, 'pages', linkPage))), 'script tag');

check('L3a API_URL ends in /v1 in both inject-config branches', (inject.match(/API_URL="[^"]*\/v1"/g) || []).length >= 2, 'inject-config.sh');
const fetches = js.split('\n').filter((l) => /fetch\(`\$\{apiBase\}/.test(l));
check('L3b the accept page has apiBase fetches', fetches.length >= 2, `found ${fetches.length}`);
check('L3c no apiBase fetch repeats /v1', fetches.every((l) => !/\$\{apiBase\}\/v1\//.test(l)), fetches.join(' | '));

if (haveApi) {
    const router = read(path.join(apiRoot, 'routers', 'onboarding.py'));
    check('L4a router serves GET /invitation-prefill/{token}', /"\/invitation-prefill\/\{token\}"/.test(router), 'route');
    check('L4b router serves POST /invitations/accept', /"\/invitations\/accept"/.test(router), 'route');
}

for (const [n, ok] of results) console.log(`${ok ? 'PASS' : 'FAIL'} ${n}`);
const failed = results.filter(([, ok]) => !ok);
console.log(`${results.length - failed.length}/${results.length} passed`);
assert.strictEqual(failed.length, 0, `${failed.length} checks failed`);
