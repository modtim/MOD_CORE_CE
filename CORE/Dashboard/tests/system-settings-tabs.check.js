// Node-runnable check for the System page tab layout in
// pages/analytics.html + js/analytics.js (Settings after Stacks; Model
// Cache demoted from a top-level tab to a Settings sub-tab).
// Same spirit as burst-accounts.check.js / entitlements.test.js — the
// Dashboard has no JS test runner, so this asserts on the source text.
//
//   node CORE/Dashboard/tests/system-settings-tabs.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(
    path.join(__dirname, '..', 'pages', 'analytics.html'), 'utf8');
const js = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'analytics.js'), 'utf8');

// --- top-level tab bar order -------------------------------------------
// The main tab bar is the first <nav ...>...</nav> in the page; only the
// #main-tab-* buttons inside it count as top-level System tabs.
const nav = html.match(/<nav class="-mb-px flex">[\s\S]*?<\/nav>/);
assert.ok(nav, 'top-level <nav> tab bar not found in analytics.html');
const topOrder = [...nav[0].matchAll(/id="(main-tab-[a-z-]+)"/g)].map(m => m[1]);

const stacksIdx = topOrder.indexOf('main-tab-stacks');
const appsIdx = topOrder.indexOf('main-tab-apps');
const settingsIdx = topOrder.indexOf('main-tab-settings');
assert.ok(stacksIdx !== -1, 'Stacks tab missing from top-level bar');
assert.ok(appsIdx !== -1, 'Apps tab missing from top-level bar (0b.2 redo part 2)');
assert.ok(settingsIdx !== -1, 'Settings tab missing from top-level bar');
// 0b.2 redo (R37.4 part 2, owner Q6 10-08): Apps is a System main tab placed
// IMMEDIATELY TO THE LEFT of Settings, so the order is Stacks -> Apps -> Settings.
assert.strictEqual(
    appsIdx, stacksIdx + 1,
    `Apps must sit immediately after Stacks (order: ${topOrder.join(', ')})`);
assert.strictEqual(
    settingsIdx, appsIdx + 1,
    `Settings must sit immediately after Apps (order: ${topOrder.join(', ')})`);

assert.ok(
    !topOrder.includes('main-tab-model-cache'),
    'Model Cache must NOT be a top-level System tab');
assert.ok(
    !/id="main-tab-model-cache"/.test(html),
    'main-tab-model-cache element must be removed from analytics.html');

// --- Model Cache as a Settings sub-tab ----------------------------------
// The settings sub-tab bar is the <nav> nested inside #main-panel-settings.
const settingsPanel = html.match(
    /<div id="main-panel-settings"[\s\S]*?<\/div>\s*<\/div>/);
assert.ok(settingsPanel, '#main-panel-settings not found');
const subOrder = [...settingsPanel[0].matchAll(/id="(settings-subtab-[a-z-]+)"/g)]
    .map(m => m[1]);
assert.ok(
    subOrder.includes('settings-subtab-model-cache'),
    'settings-subtab-model-cache button missing from the Settings sub-tab bar');
for (const c of ['usage', 'gpu-pool', 'activity', 'platform-settings']) {
    assert.ok(subOrder.includes(`settings-subtab-${c}`),
        `existing settings sub-tab ${c} missing`);
}

// The model-cache panel must be a settings child panel so
// switchSettingsChild() can show/hide it.
assert.ok(
    /<div id="main-panel-model-cache" class="main-panel settings-child-panel hidden">/.test(html),
    '#main-panel-model-cache must carry the settings-child-panel class');

// --- analytics.js wiring -------------------------------------------------
const children = js.match(/SETTINGS_CHILDREN = \[([^\]]*)\]/);
assert.ok(children, 'SETTINGS_CHILDREN not found in analytics.js');
assert.ok(
    children[1].includes("'model-cache'"),
    "SETTINGS_CHILDREN must include 'model-cache'");
assert.ok(
    js.includes("childId === 'model-cache'"),
    "analytics.js must gate 'model-cache' as a Settings child");
assert.ok(
    js.includes("initializeModelCachePanel()"),
    'Model Cache loader call missing from analytics.js');
assert.ok(
    !/main-tab-model-cache/.test(js),
    'analytics.js must not reference the old main-tab-model-cache');
assert.ok(
    js.includes("settings-subtab-model-cache"),
    'analytics.js must reveal settings-subtab-model-cache for admins');

console.log('system-settings-tabs.check.js: all assertions passed');
console.log('  top-level order :', topOrder.join(' → '));
console.log('  settings children:', subOrder.join(' → '));
