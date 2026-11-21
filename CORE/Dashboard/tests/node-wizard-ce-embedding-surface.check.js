// FIX-B57 — Settings > AI configuration > Advanced > Node Wizard on CE.
// Owner decision (owner 11-08, tasks.md:329 BUG.B57): the Learning-store embedding
// picker must be reachable on CE. It is built by _injectEmbeddingSelect for the
// 'wizard' tool (EMBEDDING_TOOLS = ['wizard','assistant']); it was "not there"
// because _applyMasterMode's reveal pass UN-DID the deliberate CE hides —
// `agent` (routers/assistant.py denied on CE) shows as a dead panel, and the
// assistant block loses its own identity. The fix is additive: a tab's own init
// stamps data-deliberate-hide='1' when it deliberately hides a tab, and the
// reveal pass honours that marker beside adminOnly. Edition semantics, role
// gates, and the paid-only sub-tab gates are unchanged.
//
// Checks (all against the SHIPPED source text of js/settings.js):
//   1) js/settings.js parses as a valid ES module.
//   2) the wizard tab/panel is NOT edition-hidden (the B57 regression must not
//      be re-invented).
//   3) 'Learning-store embedding' appears once and the CE-free
//      _injectEmbeddingSelect guard is intact.
//   4) the CE hide of the `agent` sub-tab SURVIVES the master-bar reveal pass.
//   5) the paid-only sub-tab gates survive (wizard-learning, resource-monitor).
//   6) ai-stacks-section stays hidden on CE.
//   7) the wizard tab is still revealed in Advanced and hidden in System managed.
//   8) CROSS-TENANT: the CE reveal widens no role gate; a tenant user and a
//      tenant-admin user both keep the agent hide; no CE path reveals a paid-only
//      sub-tab; the marker is never defaulted to visible (fail closed).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function countNeedle(haystack, needle) {
    if (needle === '') throw new Error('empty needle');
    let n = 0;
    let i = haystack.indexOf(needle);
    while (i !== -1) {
        n += 1;
        i = haystack.indexOf(needle, i + needle.length);
    }
    return n;
}

function countRe(re, src) {
    return (src.match(re) || []).length;
}

// windowAfter: first match of `openRe` plus the next `span` chars.
function windowAfter(src, openRe, span) {
    const m = openRe.exec(src);
    assert.ok(m, `expected site matching ${openRe} was not found`);
    return src.slice(m.index, m.index + span);
}

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

// --- 2) the wizard tab/panel is NOT edition-hidden ----------------------
assert.ok(countRe(/ai-tool-tab-wizard(?![-a-z])/, jsSrc) === 0,
    'FIX-B57: nothing may name the bare ai-tool-tab-wizard id — a CE hide of it is the regression');
assert.ok(countNeedle(jsSrc, "getElementById('ai-tool-wizard')") === 0,
    'FIX-B57: the ai-tool-wizard panel must not be addressed by id (nothing hides it on CE)');
assert.ok(countNeedle(jsSrc, "document.getElementById('ai-tool-tabbar')?.classList.toggle('hidden', managed);") === 1,
    'the tabbar must be toggled by the master-mode value alone (no edition term)');
const panelWindow = windowAfter(jsSrc, /document\.querySelectorAll\('\.ai-tool-panel'\)\.forEach\(\(el\)/, 140);
assert.ok(/classList\.add\('hidden'\)/.test(panelWindow) && !/isPaidEdition/.test(panelWindow),
    'the .ai-tool-panel blanket hide must stay edition-free (Advanced must paint on CE)');
assert.ok(countNeedle(jsSrc, "const _ceNoAiCfg = !(await isPaidEdition());") === 1,
    'exactly one CE AI-config verdict exists (it gates only the managed-AI strip)');

// --- 3) the picker itself ships for CE ---------------------------------
assert.ok(countNeedle(jsSrc, "'Learning-store embedding'") === 1,
    "the heading 'Learning-store embedding' must appear exactly once");
assert.ok(countNeedle(jsSrc, '_injectEmbeddingSelect(') === 2,
    '_injectEmbeddingSelect must have exactly one definition and one call site');
assert.ok(countNeedle(jsSrc, "const EMBEDDING_TOOLS = ['wizard', 'assistant'];") === 1,
    "'wizard' must stay an embedding-relevant tool (the picker's gate)");
const injectBody = windowAfter(jsSrc, /function _injectEmbeddingSelect\(/, 700);
assert.ok(/if \(!EMBEDDING_TOOLS\.includes\(tool\) \|\| !form\) return;/.test(injectBody),
    'the _injectEmbeddingSelect guard (tool list + form) must be intact');
assert.ok(!/isPaidEdition/.test(injectBody),
    'the embedding picker must not gain an edition gate — ai_config.py ships in CE');

// --- 4) the agent sub-tab is no longer hidden on CE (owner 11-10) --------
// FIX-CE-SHOW-ASSISTANT: 'Show them in CE'. routers/assistant.py ships in CE
// (manifest api_allow + deny_exceptions), so the agent hide + marker are gone.
assert.ok(countRe(/getElementById\('ai-tool-tab-agent'\)/, jsSrc) === 0,
    'settings.js must no longer hide the agent sub-tab (no ai-tool-tab-agent lookup in the CE gate)');
assert.ok(countNeedle(jsSrc, "dataset.deliberateHide = '1'") === 1,
    'exactly one init stamps the marker now (wizard-learning) - a widened marker is a CE reveal of paid surfaces');
const revealWindow = windowAfter(jsSrc, /_ADVANCED_ONLY_TABS\.forEach\(/, 620);
assert.ok(/!btn\.dataset\.deliberateHide/.test(revealWindow),
    'the reveal loop must still honour the deliberate-hide marker (wizard-learning)');
assert.ok(/btn\.classList\.remove\('hidden'\)/.test(revealWindow),
    'the reveal loop must still reveal the ordinary Advanced tabs');

// --- 5) paid-only sub-tab gates survive --------------------------------
['ai-tool-tab-wizard-learning', 'ai-tool-tab-resource-monitor'].forEach((id) => {
    assert.ok(new RegExp(`getElementById\\('${id}'\\)\\s*;\\s*if \\([^)]*\\)\\s*[^}]*classList\\.add\\('hidden'\\)`)
        .test(jsSrc),
        `the paid-only ${id} sub-tab gate must still hide the tab on CE`);
    assert.ok(countRe(new RegExp(`getElementById\\('${id}'\\)\\s*;\\s*if \\([^)]*\\)\\s*\\{?[^}]*classList\\.add\\('hidden'\\)`), jsSrc) === 1,
        `the paid-only ${id} tab may have exactly one hide site`);
    assert.ok(!new RegExp(`getElementById\\('${id}'\\)\\);?\\s*return;[\\s\\S]{0,80}classList\\.remove\\('hidden'\\)`).test(jsSrc),
        `no CE path may reveal the paid-only ${id} sub-tab to any tenant`);
});
assert.ok(countRe(/getElementById\('ai-tool-tab-wizard-learning'\)/g, jsSrc) === 2,
    'wizard-learning: one CE hide site + one paid reveal site');
assert.ok(countRe(/getElementById\('ai-tool-tab-resource-monitor'\)/g, jsSrc) === 2,
    'resource-monitor: one hide site + one paid reveal site');
const rmRevealGuard = windowAfter(jsSrc, /_ADVANCED_ONLY_TABS\.forEach\(/, 420);
assert.ok(/tool !== 'resource-monitor' \|\| _paidEditionKnown/.test(rmRevealGuard),
    'the resource-monitor reveal must stay fail-closed on an unknown edition');

// --- 6) the managed-AI strip stays hidden on CE ------------------------
const stacksHide = windowAfter(jsSrc, /if \(_ceNoAiCfg\) \{/, 260);
assert.ok(/ai-stacks-section/.test(stacksHide) && /classList\.add\('hidden'\)/.test(stacksHide),
    'ai-stacks-section must be hidden under the CE verdict');

// --- 7) wizard revealed in Advanced, hidden in System managed ----------
assert.ok(/const _ADVANCED_ONLY_TABS = \[[\s\S]{0,400}?'wizard'/.test(jsSrc),
    "'wizard' must stay in _ADVANCED_ONLY_TABS (Advanced-only)");
const managedHide = windowAfter(jsSrc, /_ADVANCED_ONLY_TABS\.forEach\(/, 300);
assert.ok(/if \(managed\) \{\s*btn\.classList\.add\('hidden'\);/.test(managedHide),
    'System managed must still hide every Advanced-only tab (no behaviour flip)');

// --- 8) CROSS-TENANT: no role gate widened -----------------------------
assert.ok(countNeedle(jsSrc, "tab.dataset.adminOnly = '1'") === 0,
    'no AI tool tab may be edition-hidden via adminOnly (sibling rule) — the marker replaces nothing');
assert.ok(countRe(/!btn\.dataset\.adminOnly/, jsSrc) === 1,
    'the reveal loop keeps exactly one adminOnly reader — no CE path reveals an admin-gated tab');
assert.ok(countNeedle(jsSrc, 'parent_user_id === null') === 3,
    'the tenant-admin gates are untouched (tenant user vs tenant-admin both keep the agent hide)');
assert.ok(countNeedle(jsSrc, 'is_platform_admin') === 8,
    'the platform-admin gates are untouched by this reveal change');
const stacksIds = stacksHide.match(/\[([^\]]*)\]/);
assert.ok(stacksIds && stacksIds[1].trim() === "'ai-stacks-section'",
    'CE may hide ONLY the managed-AI section — it must not hide any tab for a tenant');

console.log('node-wizard-ce-embedding-surface.check.js: OK');
