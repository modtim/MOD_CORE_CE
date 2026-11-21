// FIX-AICONFIG-ADVANCED-IN-CE — Settings > AI configuration > Advanced on CE.
// Owner decision (owner 11-07): the edition-wide ai-config hide must go — the
// section is "not there" on CE (B57 Learning-store embedding, B58 User RAG
// embedding) and his pickers decision shows all three embedding pickers live.
// ai_config.py SHIPS in CE (open-core/manifest.yaml:801 api_allow, AA-CE
// comment :800; mounted CORE/API/main.py:2343), so the gate becomes PER-BACKEND:
// the hosted managed-AI strip (services/managed_ai_stacks.py, denied
// manifest:1506) stays hidden, the paid-only sub-tab gates survive untouched.
//
// Checks (all against the SHIPPED source text of js/settings.js):
//   1) js/settings.js parses as a valid ES module.
//   2) no AI tool tab is edition-hidden (no `tab.dataset.adminOnly = '1'`).
//   3) the slots table + knowledge sources are no longer edition-hidden
//      (the three-id hide list is gone).
//   4) the ai-config reads are really requested (`_ceStub` gone).
//   5) the three embedding picker headings are each labelled once, and the
//      stack-options paid bail is gone (route ai_config.py:3043 ships in CE).
//   6) the paid-only sub-tab gates SURVIVE (wizard-learning, resource-monitor).
//   7) the managed-AI strip stays hidden (ai-stacks-section hide list).
//   8) the stale `manifest:1468` citation is gone (re-cited to manifest:801).

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

// --- 2) no tool tab is edition-hidden -----------------------------------
assert.ok(countNeedle(jsSrc, "tab.dataset.adminOnly = '1'") === 0,
    'no AI tool tab may be edition-hidden on CE — the Advanced section must paint');

// --- 3) slots table + knowledge sources not edition-hidden --------------
assert.ok(countNeedle(jsSrc, "'ai-stacks-section', 'ai-slots-table', 'ai-knowledge-sources'") === 0,
    'the who-uses-what slots table and the knowledge-sources box must not be edition-hidden');

// --- 4) the four ai-config reads are really requested -------------------
assert.ok(countNeedle(jsSrc, '_ceStub') === 0,
    'the stubbed-fetch escape hatch must be gone — CE requests the ai-config reads');

// --- 5) the three embedding pickers + CE stack options ------------------
["'Learning-store embedding'", "'User-RAG embedding'", "'MOD-docs embedding'"].forEach((heading) => {
    assert.ok(countNeedle(jsSrc, heading) === 1,
        `the embedding picker heading ${heading} must appear exactly once`);
});
assert.ok(countNeedle(jsSrc, 'if (!await isPaidEdition()) { _stackOptions = []; return; }') === 0,
    'Stack options must ship for the CE pickers (ai_config.py:3043 is CE-mounted)');

// --- 6) the paid-only sub-tab gates survive -----------------------------
['ai-tool-tab-wizard-learning', 'ai-tool-tab-resource-monitor'].forEach((id) => {
    assert.ok(new RegExp(`getElementById\\('${id}'\\)\\s*;\\s*if \\([^)]*\\)\\s*[^}]*classList\\.add\\('hidden'\\)`)
        .test(jsSrc),
        `the paid-only ${id} sub-tab gate must still hide the tab on CE`);
});

// --- 7) the managed-AI strip stays hidden -------------------------------
assert.ok(/'ai-stacks-section'[\s\S]{0,200}classList\.add\('hidden'\)/.test(jsSrc),
    'the managed-AI ai-stacks-section must stay hidden on CE (managed_ai_stacks.py is denied)');

// --- 8) stale citation gone ---------------------------------------------
assert.ok(countNeedle(jsSrc, 'manifest:1468') === 0,
    'the stale manifest:1468 citation must be re-cited to open-core/manifest.yaml:801');

console.log('ai-config-advanced-ce-sections.check.js: OK');
