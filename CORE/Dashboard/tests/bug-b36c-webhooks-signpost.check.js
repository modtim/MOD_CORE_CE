'use strict';
// Bug B36c check — Settings → Integrations → Outbound Webhooks sub-tab RETIRED.
// B36a/B36b already built ONE "Webhooks" section on the Apps tab
// (System → Apps → Webhooks). B36c removes the duplicate Integrations sub-tab
// and leaves a signpost. Asserts:
//   * Integrations shows only Email + Mobile sub-tabs, plus the signpost line
//   * no int-subpanel-webhooks / int-tab-webhooks anywhere (html or js)
//   * the #webhooks hash route redirects to the Apps Webhooks tab (ui.js)
//   * settings.js has no initializeWebhooksPage call (import removed too)
//
//   node --check CORE/Dashboard/tests/bug-b36c-webhooks-signpost.check.js
//   node CORE/Dashboard/tests/bug-b36c-webhooks-signpost.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const settingsHtml = fs.readFileSync(path.join(root, 'pages', 'settings.html'), 'utf8');
const settingsJs = fs.readFileSync(path.join(root, 'js', 'settings.js'), 'utf8');
const uiJs = fs.readFileSync(path.join(root, 'js', 'ui.js'), 'utf8');
const appsHtml = fs.readFileSync(path.join(root, 'pages', 'apps.html'), 'utf8');

// 1. Integrations panel: Email + Mobile sub-tabs only, no webhooks tab/panel.
assert.ok(settingsHtml.includes('id="int-tab-email"'), 'settings.html: Email sub-tab must remain');
assert.ok(settingsHtml.includes('id="int-tab-mobile"'), 'settings.html: Mobile sub-tab must remain');
['int-tab-webhooks', 'int-subpanel-webhooks'].forEach((id) => {
    assert.ok(!settingsHtml.includes(`id="${id}"`),
        `settings.html: retired id ${id} must not be present`);
    assert.ok(!settingsJs.includes(id),
        `settings.js: retired id ${id} must not be referenced`);
});

// 2. The signpost: one line pointing at System → Apps → Webhooks.
assert.ok(/Outbound Webhooks moved to/.test(settingsHtml),
    'settings.html: Integrations must carry the "Outbound Webhooks moved to" signpost');
assert.ok(/href="#analytics\?tab=apps"/.test(settingsHtml),
    'settings.html: signpost must link to the Apps Webhooks tab (#analytics?tab=apps)');

// 2b. CE: the Apps tab is hidden on CE (no CE webhooks screen yet), so the
// signpost is hidden there by the fail-closed isPaidEdition() gate in
// loadIntegrations(); paid keeps the text.
assert.ok(/<p id="int-webhooks-signpost"[^>]*>\s*Outbound Webhooks moved to/.test(settingsHtml),
    'settings.html: signpost must carry id="int-webhooks-signpost" so CE can hide it');
const loadInt = settingsJs.slice(settingsJs.search(/async function loadIntegrations\s*\(\s*\)\s*\{/));
assert.ok(/if \(!await isPaidEdition\(\)\) \{\s*document\.getElementById\('int-webhooks-signpost'\)\?\.classList\.add\('hidden'\);/.test(loadInt),
    'settings.js: loadIntegrations() must hide the webhooks signpost when not a paid edition');

// 3. #webhooks route redirects instead of initializing in place.
const route = uiJs.match(/targetPageId === 'webhooks'[\s\S]{0,700}?\n        \} else if/);
assert.ok(route, "ui.js: must keep a #webhooks dispatch branch");
assert.ok(/window\.location\.hash = 'analytics'/.test(route[0]),
    'ui.js: #webhooks route must redirect to the System/Analytics page');
assert.ok(/window\.location\.search = 'tab=apps'/.test(route[0]),
    'ui.js: #webhooks route must land on the Apps tab (?tab=apps)');
assert.ok(!/initializeWebhooksPage\s*\(/.test(uiJs),
    'ui.js: must not call initializeWebhooksPage()');
assert.ok(!/import \{ initializeWebhooksPage \} from '\.\/webhooks\.js';/.test(uiJs),
    "ui.js: must not import initializeWebhooksPage from './webhooks.js'");

// 4. settings.js: no initializeWebhooksPage call and no tenant-root tab gate.
assert.ok(!/initializeWebhooksPage\s*\(/.test(settingsJs),
    'settings.js: must not call initializeWebhooksPage()');
assert.ok(!/import \{ initializeWebhooksPage \} from '\.\/webhooks\.js';/.test(settingsJs),
    "settings.js: must not import initializeWebhooksPage from './webhooks.js'");
assert.ok(!/isTenantRoot/.test(settingsJs),
    'settings.js: the isTenantRoot webhooks-tab gate must be gone');

// 5. The new home still exists (B36a/B36b untouched).
assert.ok(/>\s*Webhooks\s*<\/h2>/.test(appsHtml),
    'apps.html: the ONE Apps-tab "Webhooks" section must still exist');
assert.ok(appsHtml.includes('id="webhook-rows"'),
    'apps.html: webhook-rows list must still exist');

console.log('bug-b36c-webhooks-signpost: OK');
