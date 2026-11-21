'use strict';
// BUG.B36b (owner Q-B36=A): System → Apps must carry ONE "Webhooks" section —
// one list with a Scope column ("All workflows" = WebhookEndpoint rows via
// /webhooks/, "One workflow" = run_webhook_subscriptions rows via the B36a
// tenant-wide GET /road/webhook-subscriptions) and ONE create form with a
// Scope selector. The old standalone "Run-completion Webhooks" block is gone;
// js/webhooks.js keeps its wh-* helpers exported (the legacy #webhooks hash
// route still works until BUG.B36c turns it into a signpost).
//
// Checks:
//   1) pages/apps.html has the merged section ids (add-webhook-button,
//      refresh-webhooks-button, webhook-rows) and NO old run-webhook block ids.
//   2) The Scope selector offers BOTH options (all + one).
//   3) js/apps.js calls both backends: /road/webhook-subscriptions (GET)
//      and /webhooks/ (via the webhooks.js helpers), and imports them.
//   4) No fetch path in apps.js or webhooks.js starts with /v1 (fetchApi base
//      already includes /v1).
//   5) js/apps.js + js/webhooks.js parse as ES modules.
//   6) webhooks.js still exports initializeWebhooksPage (legacy route) and the
//      reused helpers (listEndpoints/createEndpoint/removeEndpoint).

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const appsHtml = read('pages/apps.html');
const appsJs = read('js/apps.js');
const whJs = read('js/webhooks.js');

// --- 1) merged section ids in apps.html ------------------------------------
['add-webhook-button', 'refresh-webhooks-button', 'webhook-rows'].forEach(id =>
    assert.ok(appsHtml.includes(`id="${id}"`),
        `pages/apps.html must contain the merged Webhooks id #${id}`));
assert.ok(/>\s*Webhooks\s*<\/h2>/.test(appsHtml),
    'pages/apps.html must carry a "Webhooks" section heading');
['run-webhook-workflow', 'add-run-webhook-button', 'run-webhook-subscriptions']
    .forEach(id =>
        assert.ok(!appsHtml.includes(`id="${id}"`),
            `pages/apps.html must no longer contain the old run-webhook id #${id} (replaced by the merged section)`));

// --- 2) Scope selector with both options -----------------------------------
assert.ok(appsJs.includes('id="wh-scope"'),
    'apps.js create form must have the wh-scope Scope selector');
assert.ok(/value="all"/.test(appsJs) && /value="one"/.test(appsJs),
    'the Scope selector must offer BOTH "all" (All workflows) and "one" (One workflow)');
assert.ok(/All workflows/.test(appsJs) && /One workflow/.test(appsJs),
    'the Scope selector option labels must name both scopes');

// --- 3) both backends called from apps.js ----------------------------------
assert.ok(/apiCall\(['`]\/road\/webhook-subscriptions/.test(appsJs),
    'apps.js must call the tenant-wide GET /road/webhook-subscriptions (B36a)');
assert.ok(/apiCall\(['`]\/road\/\$\{[^}]+\}\/webhook-subscriptions['`]?\s*,\s*{\s*method:\s*'POST'/.test(appsJs)
    || /apiCall\(`\/road\/\$\{wfId\}\/webhook-subscriptions`/.test(appsJs),
    'apps.js must POST per-workflow creates to /road/{id}/webhook-subscriptions');
assert.ok(/listEndpoints\(\)|createEndpoint\(|removeEndpoint\(/.test(appsJs),
    'apps.js must reuse the /webhooks/ helpers exported by webhooks.js');
assert.ok(/import \{ webhooks \} from '\.\/api\.js';/.test(whJs)
    && /webhooks\.list\(\)|webhooks\.create\(|webhooks\.remove\(/.test(whJs),
    'webhooks.js must keep the /webhooks/ backend (api.js webhooks.* client)');
const apiJs = read('js/api.js');
assert.ok(/fetchApi\('\/webhooks\/'/.test(apiJs),
    'api.js must keep the /webhooks/ tenant-wide backend paths');

// --- 4) no /v1-prefixed fetch paths ----------------------------------------
[appsJs, whJs].forEach((src, i) => {
    const bad = /fetchApi\(\s*['"`]\/v1/.exec(src);
    assert.ok(!bad, `${i ? 'webhooks.js' : 'apps.js'} must not fetch a path starting with /v1 (base already includes /v1)`);
});

// --- 5) both modules parse -------------------------------------------------
[['js/apps.js', appsJs], ['js/webhooks.js', whJs]].forEach(([rel, src]) => {
    let jsErr = null;
    try {
        execFileSync(process.execPath, ['--input-type=module', '--check'],
            { input: src, stdio: ['pipe', 'pipe', 'pipe'] });
    } catch (e) {
        jsErr = String(e && (e.stderr || e.message));
    }
    assert.ok(!jsErr, `${rel}: not valid ES-module JS — ${(jsErr || '').split('\n')[0] || 'unknown'}`);
});

// --- 6) webhooks.js exports ------------------------------------------------
assert.ok(/export async function listEndpoints/.test(whJs),
    'webhooks.js must export listEndpoints for the merged UI');
assert.ok(/export async function createEndpoint/.test(whJs),
    'webhooks.js must export createEndpoint for the merged UI');
assert.ok(/export async function removeEndpoint/.test(whJs),
    'webhooks.js must export removeEndpoint (soft delete) for the merged UI');
assert.ok(/export function initializeWebhooksPage/.test(whJs),
    'webhooks.js must keep initializeWebhooksPage for the legacy #webhooks route');
assert.ok(/import \{[^}]*listEndpoints[^}]*\} from '\.\/webhooks\.js';/.test(appsJs),
    'apps.js must import the webhooks.js helpers');

// --- 7) BUG.B36b: the create response carries the secret under the JS gate key
const schemasPy = read('../API/schemas.py');
const whRouterPy = read('../API/routers/webhooks.py');

assert.ok(/@router\.post\("\/",\s*response_model=schemas\.WebhookEndpointCreated/.test(whRouterPy),
    'the POST /webhooks/ create route must answer with WebhookEndpointCreated (the list model drops the secret)');
const createdModel = /class WebhookEndpointCreated\(WebhookEndpoint\)([\s\S]*?)\n\n/.exec(schemasPy);
assert.ok(createdModel, 'schemas.py must declare WebhookEndpointCreated (the create-only response model)');
const createdFields = createdModel[1];
assert(/secret:\s*str/.test(createdFields),
    'WebhookEndpointCreated must declare a `secret: str` field — the key the one-time key modal reads');
assert(!/^\s*signing_secret\s*:/m.test(createdFields),
    'the create response field must be named `secret`, not `signing_secret` (the JS contract)');
assert(/secret=db_webhook\.signing_secret/.test(whRouterPy),
    'the create route must map secret=<row>.signing_secret explicitly (from_attributes would not map the names)');

const listModel = /class WebhookEndpoint\(WebhookEndpointBase\)([\s\S]*?)\nclass /.exec(schemasPy);
assert.ok(listModel, 'schemas.py must keep the WebhookEndpoint list/get model');
assert(!/secret/.test(listModel[1]),
    'the list/get model must carry NO secret field — the secret is shown once, at create only');

const gate = /if \(created && created\.(\w+)\)/.exec(appsJs);
assert.ok(gate, 'apps.js must gate the one-time key modal on a created.* key');
assert(gate[1] === 'secret',
    `apps.js gate key must equal the API create field name (secret); got ${gate[1]}`);
assert(!/created\.signing_secret/.test(appsJs),
    'apps.js must not read created.signing_secret — the API create response key is secret');
assert(/openOneTimeKeyModal\([^)]*created\.secret\)/.test(appsJs),
    'apps.js must pass created.secret to openOneTimeKeyModal (shown once)');

// no secret value may reach a log on either side
[[appsJs, whJs, apiJs], [whRouterPy]].forEach((srcs, i) => {
    srcs.forEach(src => {
        const leak = i ? /(^|\n)\s*(print|logging|logger)[^\n]*secret/
                       : /console\.(log|error|info|warn)[^\n]*secret/i;
        assert(!leak.test(src), 'a signing secret value must never be logged (neither JS nor the route)');
    });
});

console.log('bug-b36b-merged-webhooks: OK');
