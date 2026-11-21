// ai-binding-7-3a-new-ai-stack.check.js
// Validates the two defect fixes in the "New AI stack" feature:
//
// Defect 1 — correct placement:
//   The "New AI stack" button must appear exactly once at the page level
//   (beside "Create from compose") and must NOT appear in any row template.
//
// Defect 2 — per-model deploy:
//   _psAiDeploy must NOT call /managed-ai/provision (the sweep endpoint).
//   It must either call /platform/managed-ai/apply with a preset body,
//   or keep Deploy disabled and show the "not available yet" message.
//
// Rules:
//   No module-level DOM access (no document.querySelector at top-level).
//   Only string scanning of the source file — no eval/exec.
//   No /v1 in path strings (uses the platform-prefixed paths).

const fs = require('fs');
const path = require('path');

const SRC_PATH = path.resolve(__dirname, '..', 'js', 'persistent-stacks.js');
const HTML_PATH = path.resolve(__dirname, '..', 'pages', 'analytics.html');
const src = fs.readFileSync(SRC_PATH, 'utf8');
const html = fs.readFileSync(HTML_PATH, 'utf8');

let passCount = 0;
let failCount = 0;

function assert(label, condition) {
    if (condition) { passCount++; console.log(`  PASS: ${label}`); }
    else { failCount++; console.log(`  FAIL: ${label}`); }
}

// --- Defect 1: correct button placement ---

// 1a. Exactly one new-ai-stack button at page level (in HTML template)
const newAiBtnMatches = html.match(/id\s*=\s*["']ps-new-ai-stack["']/g);
assert(
    'Exactly one page-level "ps-new-ai-stack" button',
    newAiBtnMatches && newAiBtnMatches.length === 1
);

// 1b. No data-ps-act="new-ai-stack" in row template (per-row button removed)
const rowActMatches = src.match(/data-ps-act="new-ai-stack"/g);
assert(
    'No per-row data-ps-act="new-ai-stack" button',
    !rowActMatches || rowActMatches.length === 0
);

// 1c. No row dispatch branch for 'new-ai-stack'
assert(
    'No row dispatch for new-ai-stack act',
    !src.includes("if (act === 'new-ai-stack')")
);

// 1d. The page-level button has a click handler wired to _psNewAIStack
assert(
    'Page-level button wired to _psNewAIStack',
    src.includes("document.getElementById('ps-new-ai-stack')?.addEventListener('click', _psNewAIStack)")
);

// --- CE posture (owner decision 11-10): the button is VISIBLE on CE and routes
//     to the existing 'Create from compose' flow; the paid managed-AI deploy stays
//     paid-only. (FIX-CE-NEW-AI-STACK-OPENS-COMPOSE) ---

// 1e. No CE hide block for the button (the old _psIsCeEdition().then(...) display:none
//     block is gone — nothing sets display:none on #ps-new-ai-stack).
assert(
    'No CE display:none hide for #ps-new-ai-stack',
    !/getElementById\(\s*['"]ps-new-ai-stack['"]\s*\)[\s\S]{0,200}?display\s*=\s*['"]none['"]/.test(src)
);

// 1f. _psNewAIStack branches on CE and delegates to the SAME compose handler the
//     compose button calls (no duplicated create logic).
const newAiStackBody = src.substring(
    src.indexOf('async function _psNewAIStack()'),
    src.indexOf('async function _psNewAIStack()') + 1600
);
assert(
    '_psNewAIStack checks CE edition',
    /async function _psNewAIStack\(\)[\s\S]{0,900}_psIsCeEdition\(\)/.test(src)
);
assert(
    '_psNewAIStack CE leg calls _psComposeCreateClick (the existing compose flow)',
    newAiStackBody.includes('return _psComposeCreateClick();')
);

// 1g. The CE leg returns BEFORE the paid modal/catalog fetch, so CE never opens
//     #ps-ai-modal and never reaches the paid deploy endpoint.
assert(
    'CE leg returns before the modal path',
    newAiStackBody.indexOf('return _psComposeCreateClick();') < newAiStackBody.indexOf('_psShowAiModal(')
);

// 1h. Paid path unchanged: the modal + apply endpoint still exist.
assert(
    'Paid modal path still present (_psShowAiModal)',
    src.includes('_psShowAiModal(null, _psAiCatalog);')
);

// --- Defect 2: per-model deploy ---

// 2a. No call to /managed-ai/provision from _psAiDeploy
const provisionCall = src.match(/apiCall\(["']\/?managed-ai\/provision["']/g);
assert(
    '_psAiDeploy does NOT call /managed-ai/provision sweep endpoint',
    !provisionCall || provisionCall.length === 0
);

// 2b. Calls /platform/managed-ai/apply with preset body
const applyCall = src.match(/apiCall\(["']\/platform\/managed-ai\/apply["'][\s\S]*?preset[\s\S]*?\}/g);
assert(
    '_psAiDeploy calls /platform/managed-ai/apply with preset body',
    !!applyCall && applyCall.length >= 1
);

// 2c. Has a message for when per-model deploy is not available
assert(
    'Shows gap message when model has no preset mapping',
    src.includes("Per-model deploy is not available yet")
);

// 2d. No hard-coded model-to-preset map (server supplies .preset on catalog entries).
// The strings 'devstral-small-2' and 'gpt-oss-20b' must NOT appear in the
// client-side source as hard-coded preset mappings — the server is the
// authoritative source of truth (GET /platform/ai-config/model-catalog).
assert(
    'No hard-coded devstral-small-2 preset in client',
    !src.includes("devstral-small-2")
);
assert(
    'No hard-coded gpt-oss-20b preset in client',
    !src.includes("gpt-oss-20b")
);

// --- General: no /v1 in API paths used by AI stack functions ---

// Extract the AI stack section (from _psNewAIStack through _psCloseAiModal)
const aiSection = src.substring(src.indexOf('async function _psNewAIStack()'),
    src.indexOf('Platform-admin only') + 40);

const v1Paths = aiSection.match(/["']\/[^"']*\/v1[^"']*["']/g);
assert(
    'No /v1 in AI stack API path strings',
    !v1Paths || v1Paths.length === 0
);

// --- Summary ---

console.log(`\n${passCount} passed, ${failCount} failed`);
if (failCount > 0) process.exit(1);
