// GSEC FX.163 p3b2b — Tenant full export as a hub TAB. RETARGETED.
//
// This file used to guard the fold inside the SETTINGS page. Owner decision 10-08 moved
// the Data export tab out of Settings and into the COMPLIANCE CENTER hub, so the Settings
// target was reverted and the real fold is guarded by tests/compliance-data-export-tab.check.js.
// The tab was never missing — it lives at:
//   CORE/Dashboard/pages/compliance-center.html:80   <button id="main-tab-data-export" ... hidden>
//   CORE/Dashboard/pages/compliance-center.html:82   label 'Data export'
//   CORE/Dashboard/pages/compliance-center.html:179  <div id="main-panel-data-export" class="main-panel hidden"></div>
//   CORE/Dashboard/js/hub-compliance.js:35   import { initializeTenantExportPage } from './tenant-export.js'
//   CORE/Dashboard/js/hub-compliance.js:201  'data-export': () => initializeTenantExportPage()   (_CHILD_INIT)
//   CORE/Dashboard/js/hub-compliance.js:487  first-activation gate; :489 fetch('/pages/tenant-export.html') verbatim
//   CORE/Dashboard/js/hub-compliance.js:266  'data-export': 'tenant'  (hub tenant gate)
//   CORE/Dashboard/js/hub-compliance.js:95   _CE_FX163_TABS = new Set(['data-export'])  — the CE carve
//     (open-core/manifest.yaml:1392 denies CORE/API/routers/tenant/*.py, which covers
//     CORE/API/routers/tenant/export.py; the FE child pages/tenant-export.html + js/tenant-export.js
//     is CE-clean, so only the tab is hidden).
// The child page itself stays guarded by tests/tenant-export.check.js.
//
// What this file now guards, so the move cannot silently regress:
//   * the tab really does live in the hub (hidden button + empty hidden panel + hub wiring),
//   * the CE carve is asserted on the hub,
//   * the Settings page (settings.html + js/settings.js) carries NO trace of the tab — the
//     earlier target stays reverted,
//   * dashboard-shell.html gained no sidebar row (owner rule: hub tabs, never new sidebar rows).
//
//   node CORE/Dashboard/tests/settings-data-export-tab.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const hub = fs.readFileSync(path.join(__dirname, '..', 'pages', 'compliance-center.html'), 'utf8');
const hubJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'hub-compliance.js'), 'utf8');
const setHtml = fs.readFileSync(path.join(__dirname, '..', 'pages', 'settings.html'), 'utf8');
const setJs = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
const shell = fs.readFileSync(path.join(__dirname, '..', 'pages', 'dashboard-shell.html'), 'utf8');

// --- the tab lives in the hub: hidden button + empty hidden panel --------------------
assert.ok(/<button id="main-tab-data-export"[^>]*\bhidden\b/.test(hub),
  'the Data export tab button is missing from compliance-center.html (the hub is the tab\'s home)');
assert.ok(/>\s*Data export\s*</.test(hub), 'the hub tab button label must say Data export');
assert.ok(/id="main-panel-data-export"[^>]*\bhidden\b/.test(hub), 'the hub Data export panel is missing');
assert.ok(/id="main-panel-data-export"[^>]*><\/div>/.test(hub),
  'the hub Data export panel must be EMPTY in the markup — the child page is fetched at runtime');

// --- hub wiring: import + child initializer + verbatim lazy fetch --------------------
assert.ok(/import\s*\{[^}]*\binitializeTenantExportPage\b[^}]*\}\s*from\s*'\.\/tenant-export\.js'/.test(hubJs),
  'hub-compliance.js must import initializeTenantExportPage from js/tenant-export.js');
assert.ok(/'data-export': \(\) => initializeTenantExportPage\(\)/.test(hubJs),
  "_CHILD_INIT must run the child's own initializer for 'data-export'");
assert.ok(/key === 'data-export' && panel && !_loadedOnce\[key\]/.test(hubJs),
  'the child fetch must be gated on first activation of the data-export tab (load once)');
assert.ok(/fetch\('\/pages\/tenant-export\.html'\)/.test(hubJs),
  'the panel must be filled by a fetch of /pages/tenant-export.html');
assert.ok(/'data-export':\s*'tenant',/.test(hubJs),
  'the hub Data export tab must use the hub tenant gate');

// --- the CE carve: the tab is hidden on CE -----------------------------------------
assert.ok(/_CE_FX163_TABS = new Set\(\['data-export'\]\)/.test(hubJs),
  'the Data export tab must be CE-hidden (its router CORE/API/routers/tenant/*.py is denied)');
assert.ok(/_ceEdition && _CE_FX163_TABS\.has\(key\)/.test(hubJs),
  'the CE hide must be enforced inside _tabAllowed');

// --- the reverted target: Settings carries no trace of the tab --------------------
assert.ok(!/data-export|tenant-export/.test(setHtml),
  'settings.html must carry no Data export tab — the tab lives in the compliance hub');
assert.ok(!/data-export|tenant-export/.test(setJs),
  'js/settings.js must carry no Data export wiring — the tab lives in the compliance hub');

// --- the owner rule: NO new sidebar row -------------------------------------------
assert.ok(!/data-export|tenant-export|Tenant full export/i.test(shell),
  'dashboard-shell.html must NOT gain a sidebar link for the export — hub tabs only');

console.log('settings-data-export-tab.check.js: OK (retargeted to the compliance-center hub)');
