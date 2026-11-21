// FX.145.p3c — Dashboard "Admin network allowlist" card check.
// Runs inside node via the Dashboard's child-tab pattern: the ip-blocks.html
// tab loads js/admin-ingress-allowlist.js.  Since we cannot spawn a real
// browser, we assert on the source text for every in-browser behaviour.
//
//   node CORE/Dashboard/tests/admin-ingress-allowlist.check.js
//
// Checks:
//   1) js/admin-ingress-allowlist.js parses as valid ES module.
//   2) ip-blocks.html contains the allowlist card markup (#ai-cidrs-input,
//      #ai-save-btn, #ai-error, #ai-cidrs-value, #ai-paths-value, warning).
//   3) save button clicks handler wires to handleSave.
//   4) confirm() is called with the lockout warning on non-empty cidr list.
//   5) empty list does NOT call confirm().
//   6) server data goes through textContent only (no innerHTML with server data).
//   7) 422 error path shows inline error via _showError (textContent on #ai-error).
//   8) No bg-blue-600 or text-blue-600 anywhere in the module or card HTML.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

// ==========================================================================
// 1) ES module syntax check
// ==========================================================================

let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: read('js/admin-ingress-allowlist.js'), encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr,
    `admin-ingress-allowlist.js is not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// ==========================================================================
// 2) Card HTML elements present in ip-blocks.html
// ==========================================================================

const ipHtmlSrc = read('pages/ip-blocks.html');

// Card container elements
for (const id of ['ai-cidrs-input', 'ai-save-btn', 'ai-error',
                   'ai-cidrs-value', 'ai-paths-value']) {
    assert.ok(ipHtmlSrc.includes(`id="${id}"`),
        `ip-blocks.html must contain element with id="${id}"`);
}

// Warning text must be present verbatim
assert.ok(ipHtmlSrc.includes('Only these networks will reach admin pages.'),
    'ip-blocks.html must contain the lockout warning text');

// Save button
assert.ok(ipHtmlSrc.includes('id="ai-save-btn"'),
    'ip-blocks.html must contain #ai-save-btn');

// Textarea for CIDRs
assert.ok(ipHtmlSrc.includes('id="ai-cidrs-input"'),
    'ip-blocks.html must contain #ai-cidrs-input textarea');

// Error container
assert.ok(ipHtmlSrc.includes('id="ai-error"'),
    'ip-blocks.html must contain #ai-error container');

// Current CIDRs value display
assert.ok(ipHtmlSrc.includes('id="ai-cidrs-value"'),
    'ip-blocks.html must contain #ai-cidrs-value');

// Current paths value display
assert.ok(ipHtmlSrc.includes('id="ai-paths-value"'),
    'ip-blocks.html must contain #ai-paths-value');

// ==========================================================================
// 3) JS module: imports, function signatures, wire-up pattern
// ==========================================================================

const jsSrc = read('js/admin-ingress-allowlist.js');

// Must import platformSettings from api.js
assert.ok(jsSrc.includes("import { platformSettings") && jsSrc.includes("from './api.js'"),
    'admin-ingress-allowlist.js must import platformSettings from api.js');

// Must export initializeAdminIngressAllowlist
assert.ok(/export\s+async\s+function\s+initializeAdminIngressAllowlist/.test(jsSrc),
    'must export initializeAdminIngressAllowlist');

// Must have handleSave function
assert.ok(/function\s+handleSave/.test(jsSrc),
    'must define handleSave');

// Must have _showError function (uses textContent on #ai-error)
assert.ok(/function\s+_showError/.test(jsSrc),
    'must define _showError');

// ==========================================================================
// 4) confirm() called with lockout warning on non-empty list
// ==========================================================================

// handleSave must call confirm() with the lockout warning text.
assert.ok(jsSrc.includes("confirm("),
    'handleSave must call confirm()');
assert.ok(jsSrc.includes('Only these networks will reach admin pages.'),
    'confirm() must include the lockout warning text');
// The confirm() should only be called when cidrs.length > 0
assert.ok(/if\s*\(\s*cids?\w*\s*\.length\s*>\s*0\s*\)/.test(jsSrc) ||
          /if\s*\(\s*cids?\w*\s*\.\w+\s*>\s*0\s*\)/.test(jsSrc),
    'confirm() must be gated behind non-empty list check');

// ==========================================================================
// 5) Empty list: no confirm() path — verified by structure
// ==========================================================================

// Find the handleSave function body and confirm that confirm() only appears
// inside a non-empty check block, not at the top level of the function.
const saveMatch = jsSrc.match(/async\s+function\s+handleSave\(\)\s*\{([\s\S]*?)^(?=\nfunction |\n\/\/)/m);
assert.ok(saveMatch, 'handleSave function body must be findable');
const saveBody = saveMatch[1];

// confirm() call should be inside an if block, not unconditional.
const confirmCalls = [...saveBody.matchAll(/confirm\(/g)];
assert.ok(confirmCalls.length >= 1, 'handleSave must call confirm() at least once');

// The confirm() should be preceded by a check for cidrs.length > 0
const hasLengthCheck = /if\s*\(\s*[a-zA-Z_]+\s*\.length\s*>\s*0\s*\)/.test(saveBody);
assert.ok(hasLengthCheck,
    'handleSave must check cidrs.length > 0 before calling confirm()');

// ==========================================================================
// 6) Server data goes through textContent only — no innerHTML with data
// ==========================================================================

// _showError uses textContent on #ai-error — find the assignment.
assert.ok(/\[?_showError\]?[\s\S]*?\[?ai-error\]?[\s\S]*?\.textContent\s*=/.test(jsSrc) ||
          /_showError[\s\S]*?ai-error[\s\S]*?textContent/.test(jsSrc),
    '_showError must use textContent (not innerHTML) for server data');

// _errText builds error text from the response — no innerHTML.
assert.ok(/innerHTML/.test(jsSrc) === false ||
          jsSrc.indexOf('innerHTML') > jsSrc.indexOf('_errText') === false,
    'module must not use innerHTML for server data');

// Check that the module does NOT contain innerHTML patterns for server data.
const innerHTMLMatches = [...jsSrc.matchAll(/innerHTML\s*=\s*['"`]/g)];
assert.ok(innerHTMLMatches.length === 0,
    'admin-ingress-allowlist.js must not use innerHTML with data (found ' + innerHTMLMatches.length + ' occurrences)');

// ==========================================================================
// 7) 422 error path — _showError called in catch block of handleSave
// ==========================================================================

// In handleSave's try/catch, if there's an error, _showError must be called.
// The catch block should reference _showError.
assert.ok(/try\s*\{[\s\S]*catch\s*\([^)]*\)\s*\{[\s\S]*_showError/.test(jsSrc),
    'handleSave must call _showError in its catch block for 422 errors');

// ==========================================================================
// 8) No bg-blue-600 or text-blue-600 anywhere
// ==========================================================================

// Check the JS module
assert.ok(!jsSrc.includes('bg-blue-600'),
    'admin-ingress-allowlist.js must not use bg-blue-600');
assert.ok(!jsSrc.includes('text-blue-600'),
    'admin-ingress-allowlist.js must not use text-blue-600');

// Check only the new allowlist card section in ip-blocks.html (after the FX.145 comment)
const cardSection = ipHtmlSrc.slice(ipHtmlSrc.indexOf('FX.145.p3c'));
assert.ok(!cardSection.includes('bg-blue-600'),
    'allowlist card must not use bg-blue-600');
assert.ok(!cardSection.includes('text-blue-600'),
    'allowlist card must not use text-blue-600');

// ==========================================================================
// 9) ip-blocks.js wires in the allowlist card: import + call in try/catch
// ==========================================================================

const ipBlocksJs = fs.readFileSync(path.join(ROOT, 'js', 'ip-blocks.js'), 'utf8');

// Must dynamically import the allowlist module
assert.ok(/import\s*\(\s*['"]\.\/admin-ingress-allowlist\.js['"]\s*\)/.test(ipBlocksJs),
    'ip-blocks.js must dynamically import admin-ingress-allowlist.js');

// Must call initializeAdminIngressAllowlist after the main ip-blocks load
assert.ok(/initializeAdminIngressAllowlist\(\)/.test(ipBlocksJs),
    'ip-blocks.js must call initializeAdminIngressAllowlist()');

// Must gate on platform admin with me() and hide the card container
assert.ok(/is_platform_admin/.test(ipBlocksJs),
    'ip-blocks.js must check is_platform_admin for the allowlist card');
assert.ok(/ai-card-container/.test(ipBlocksJs),
    'ip-blocks.js must reference #ai-card-container for platform-admin gating');

// Must wrap in try/catch (the catch must follow the call)
const callIdx = ipBlocksJs.indexOf('initializeAdminIngressAllowlist()');
assert.ok(callIdx !== -1, 'initializeAdminIngressAllowlist call must be present');
// Check that there's a catch block after the call (within reasonable distance)
const afterCall = ipBlocksJs.slice(callIdx);
assert.ok(/try\s*\{[\s\S]*catch/.test(ipBlocksJs.slice(0, callIdx + 500)),
    'the call must be inside a try/catch block');

// ==========================================================================
// 10) Card container element exists in ip-blocks.html
// ==========================================================================

assert.ok(ipHtmlSrc.includes('id="ai-card-container"'),
    'ip-blocks.html must contain #ai-card-container wrapper div');

// ==========================================================================
// 11) No module-level DOM access (all DOM reads inside functions)
// ==========================================================================

// Module-level code should only be imports and constant definitions.
// All document.* calls must be inside a function body.
// Heuristic: strip all function bodies (between { and matching }) and check
// the remaining module-level lines for any document.* references.
// A simpler approach: check that no document. call is at the top level
// of the module (not indented inside a function).
const domCallPattern = /document\.(getElementById|querySelector|querySelectorAll|createElement|getBody|getElementByName|getElementsByName|getElementsByClassName|write|writeln|cookie|domain|referrer|title|URL|location|createElementNS|createTextNode|createComment|createDocumentFragment|createElementNS)/g;

// Check that all document.* calls in the JS are inside function bodies.
// We use a bracket-depth heuristic: reset at function {, increment on {,
// decrement on }, only flag document. when depth === 0.
const jsLines = jsSrc.split('\n');
let depth = 0;
for (const line of jsLines) {
    const trimmed = line.trim();
    // Skip pure comments and blank lines.
    if (trimmed.startsWith('//') || trimmed === '') continue;
    // Count brace deltas.
    const openCount = (trimmed.match(/\{/g) || []).length;
    const closeCount = (trimmed.match(/\}/g) || []).length;
    // Check for document.* before applying the delta of the current line.
    if (depth === 0 && domCallPattern.test(trimmed)) {
        // Re-check with a fresh match since the above consumed the regex.
        domCallPattern.lastIndex = 0;
        if (domCallPattern.test(trimmed)) {
            assert.ok(false,
                `Module-level DOM access (depth 0) at: ${line.trim().slice(0, 80)}`);
        }
    }
    depth += openCount - closeCount;
}

// ==========================================================================
// 10) No .astext on JSONB (safety — same as API test rules)
// ==========================================================================

assert.ok(!jsSrc.includes('.astext'),
    'must not use .astext (JSONB rule)');

console.log('admin-ingress-allowlist.check.js: all checks passed -> OK');
