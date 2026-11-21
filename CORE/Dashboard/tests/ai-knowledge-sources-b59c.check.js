// BUG.B59c (owner Q-B59=A) — Dashboard AI Config "Knowledge sources" picker.
// The assistant's knowledge embedding is a picker over knowledge sources
// (MOD Docs / tenant files / uploads), each with its OWN embedding model.
//
// Checks:
//   1) js/settings.js parses as a valid ES module.
//   2) settings.html ships the #ai-knowledge-sources container exactly once,
//      INSIDE the Knowledge Base tab panel (main-panel-knowledge) — moved out
//      of the AI Config panel (owner Q-B59=A) — and never after #ai-slots-table
//      any more; it still ships hidden until its read succeeds.
//   3) settings.js labels the three source kinds (MOD Docs / Tenant files / Uploads).
//   4) settings.js writes BOTH scopes: the platform PUT and the tenant PUT.
//   5) "Turn off" is the API's SOFT delete — a PUT body carrying enabled:false.
//   6) no fetch path in the knowledge-sources code contains '/v1' (v2 API only).
//   7) options are capability-filtered to embedding stacks (a chat stack can
//      never be picked into an embedding row).
//   8) the Knowledge Base tab click hydrates the picker (the panel has a branch).
//   9) cross-tenant source-of-truth: the tenant PUT handler scopes the write to
//      current_user.tenant_id and takes no tenant_id from the caller.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const htmlSrc = read('pages/settings.html');
const jsSrc = read('js/settings.js');

// --- 1) ES module syntax check ------------------------------------------
let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr,
    `js/settings.js: not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the container exists once, inside the Knowledge Base tab --------
const kbId = 'id="ai-knowledge-sources"';
let kbCount = 0;
for (let at = htmlSrc.indexOf(kbId); at !== -1; at = htmlSrc.indexOf(kbId, at + 1)) kbCount++;
assert.ok(kbCount === 1,
    `settings.html must contain the #ai-knowledge-sources container exactly once (found ${kbCount})`);
const kbPos = htmlSrc.indexOf(kbId);
assert.ok(kbPos !== -1, 'settings.html must contain the #ai-knowledge-sources container');
const kbTag = htmlSrc.slice(htmlSrc.lastIndexOf('<div', kbPos), htmlSrc.indexOf('>', kbPos) + 1);
assert.ok(/class="[^"]*\bhidden\b/.test(kbTag),
    'the knowledge-sources box must ship hidden until its read succeeds');
const kbPanelPos = htmlSrc.indexOf('id="main-panel-knowledge"');
const kbPanelEnd = htmlSrc.indexOf('id="main-panel-tls"');
assert.ok(kbPanelPos !== -1, 'settings.html must contain the Knowledge Base tab panel');
assert.ok(kbPanelEnd !== -1, 'the Knowledge Base panel must end before the next tab panel');
assert.ok(kbPos > kbPanelPos && kbPos < kbPanelEnd,
    'the knowledge-sources box must live inside the Knowledge Base tab panel');
const slotsPos = htmlSrc.indexOf('id="ai-slots-table"');
assert.ok(slotsPos !== -1, 'settings.html must contain the #ai-slots-table container');
assert.ok(kbPos < slotsPos,
    'the knowledge-sources box must NOT sit after the who-uses-what slots table any more (owner Q-B59=A: it belongs on the Knowledge Base tab)');

// --- 3) the three labels -----------------------------------------------
['MOD Docs', 'Tenant files', 'Uploads'].forEach((label) => {
    assert.ok(jsSrc.includes(label),
        `settings.js must label the source "${label}"`);
});
assert.ok(/_AI_KB_ROWS\s*=\s*\[[^\]]*mod_docs[^\]]*tenant_files[^\]]*uploads/.test(jsSrc),
    'settings.js must carry the three source kinds mod_docs / tenant_files / uploads');

// --- 4) both write scopes ----------------------------------------------
assert.ok(jsSrc.includes('/platform/ai-config/knowledge-sources-tenant/'),
    'settings.js must PUT the tenant-scope knowledge-source override');
assert.ok(/['"`]\/platform\/ai-config\/knowledge-sources\/\$\{/.test(jsSrc)
    || jsSrc.includes('/platform/ai-config/knowledge-sources/${encodeURIComponent(kind)}'),
    'settings.js must PUT the platform-scope knowledge-source binding');
assert.ok(/is_platform_admin[\s\S]{0,400}knowledge-sources-tenant/.test(jsSrc),
    'the write scope must be chosen from platform-admin-ness (admin -> platform, tenant -> tenant)');

// --- 5) "Turn off" = soft delete (enabled:false) ------------------------
assert.ok(jsSrc.includes('Turn off'),
    'the row must offer an explicit "Turn off" action');
assert.ok(/JSON\.stringify\(\{\s*enabled:\s*false\s*\}\)/.test(jsSrc),
    'turning a knowledge source off must PUT enabled:false (the API soft delete)');
assert.ok(/retired/.test(jsSrc),
    'the retired state must be surfaced (the row is never hard-deleted)');
assert.ok(/Off —/.test(jsSrc),
    'a retired knowledge source must render as "Off"');

// --- 6) no /v1 in any fetch path ---------------------------------------
const kbStart = jsSrc.indexOf('_AI_KB_ROWS');
assert.ok(kbStart !== -1, 'settings.js must define the knowledge-source rows');
const kbChunk = jsSrc.slice(kbStart, kbStart + 14000);
assert.ok(!/\/v1\b/.test(kbChunk),
    'the knowledge-sources code must not fetch a /v1 path — the v2 API is /platform/... only');
assert.ok(/window\.APP_CONFIG\?\.API_URL/.test(kbChunk),
    'the knowledge-sources read must use the same API base the slots table uses');

// --- 7) capability-filtered options ------------------------------------
assert.ok(/_aiKbFillOptions[\s\S]{0,600}kind === 'embedding'/.test(jsSrc),
    'knowledge-source options must be filtered to embedding-capable stacks');
assert.ok(/serves chat models, not embedding/.test(jsSrc)
    || /text-red-600/.test(kbChunk),
    'a refused pick (e.g. a chat stack) must be shown inline, not swallowed');

// --- 8) the Knowledge Base tab click hydrates the picker ---------------
const branch = jsSrc.indexOf("panelId === 'main-panel-knowledge'");
assert.ok(branch !== -1,
    "the Settings tab click handler must have a branch for 'main-panel-knowledge'");
assert.ok(/_loadAiKnowledgeSources\(\)/.test(jsSrc.slice(branch, branch + 1200)),
    'the Knowledge Base tab branch must hydrate the picker (_loadAiKnowledgeSources)');
assert.ok(/_wireAiKnowledgeSources\(\)/.test(jsSrc.slice(branch, branch + 1200)),
    'the Knowledge Base tab branch must wire the picker dropdowns (_wireAiKnowledgeSources)');

// --- 9) cross-tenant source-of-truth (ai_config.py) --------------------
const pySrc = read('../API/routers/ai_config.py');
const tStart = pySrc.indexOf('def put_ai_config_knowledge_sources_tenant(');
assert.ok(tStart !== -1, 'ai_config.py must define the tenant knowledge-source PUT');
const tSigEnd = pySrc.indexOf('\n):', tStart);
assert.ok(tSigEnd !== -1, 'the tenant knowledge-source PUT signature must close');
const tSig = pySrc.slice(tStart, tSigEnd);
assert.ok(!/tenant_id\s*:/.test(tSig),
    'the tenant knowledge-source PUT must take NO tenant_id from the caller — the tenant is derived server-side');
const tBody = pySrc.slice(tSigEnd, pySrc.indexOf('\ndef ', tSigEnd + 1));
assert.ok(/_write_knowledge_binding\([\s\S]{0,300}current_user\.tenant_id/.test(tBody),
    'the tenant knowledge-source PUT must scope the write to current_user.tenant_id');
assert.ok(/current_user\.tenant_id\s+is\s+None/.test(tBody),
    'a user with no tenant must be refused before any override is written');

console.log('ai-knowledge-sources-b59c.check.js: OK');
