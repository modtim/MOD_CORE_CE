// GX Q-ING=A (owner 10-08) + follow-up: public app hostnames are per-app names
// the owner adds BY HAND to his DATACENTER edge Caddy, which forwards to the
// manager over the tailnet. The System → Apps panel must therefore show, on
// each app, a read-only "Public address (edge Caddy)" box carrying the exact
// block to paste:
//
//     <app.hostname> {
//           reverse_proxy <edge upstream>
//     }
//
// plus the line "Add this to your edge Caddy and point public DNS for
// <hostname> at that box." and a Copy button.
//
// Follow-up: the upstream now comes from the platform setting apps.edge_upstream
// (registered in services/app_routing.py), surfaced read-only on the app
// payload as `edge_upstream`. Empty/unset => None => the placeholder
// <this-manager's tailnet name> plus the muted Settings → Platform note.
// The block stays READ-ONLY helper text: the platform never writes the owner's
// Caddy, and caddy-front's own sites (services/app_routing.py) stay untouched.
// ---------------------------------------------------------------------------

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');            // CORE/Dashboard
const appsJs = fs.readFileSync(path.join(ROOT, 'js', 'apps.js'), 'utf8');

// --- 1) helpers + placeholder ---------------------------------------------
assert.ok(/const EDGE_UPSTREAM_PLACEHOLDER = "<this-manager's tailnet name>";/.test(appsJs),
    'apps.js must define EDGE_UPSTREAM_PLACEHOLDER = "<this-\'manager\'s tailnet name>"');
['_edgeCaddyBlock', '_edgeCaddyNote', '_edgeCaddyBox', '_wireEdgeCaddyCopy']
    .forEach(fn => assert.ok(new RegExp(`function ${fn}\\(`).test(appsJs),
        `apps.js must define the edge-Caddy helper ${fn}()`));

// --- 2) block shape + upstream plumbing -----------------------------------
const blockFn = appsJs.slice(appsJs.indexOf('function _edgeCaddyBlock'),
    appsJs.indexOf('function _edgeCaddyNote'));
assert.ok(/return `\$\{hostname\} \{\\n      reverse_proxy \$\{upstream \|\| EDGE_UPSTREAM_PLACEHOLDER\}\\n\}`;/.test(blockFn),
    'the block must be "<hostname> {\\n      reverse_proxy <upstream or placeholder>\\n}" with a 6-space indent');
assert.ok(/function _edgeCaddyBlock\(hostname, upstream\)/.test(appsJs),
    '_edgeCaddyBlock must take (hostname, upstream)');
assert.ok(/function _edgeCaddyBox\(hostname, upstream\)/.test(appsJs),
    '_edgeCaddyBox must take (hostname, upstream) and pass it down');
assert.ok(/const block = _edgeCaddyBlock\(hostname, upstream\);/.test(appsJs),
    '_edgeCaddyBox must forward the upstream into the block');

// --- 3) note line ----------------------------------------------------------
const noteFn = appsJs.slice(appsJs.indexOf('function _edgeCaddyNote'),
    appsJs.indexOf('function _edgeCaddyBox'));
assert.ok(/Add this to your edge Caddy and point public DNS for \$\{hostname\} at that box\./.test(noteFn),
    'the note must read "Add this to your edge Caddy and point public DNS for <hostname> at that box."');

// --- 4) the View modal renders the box + wires Copy ------------------------
const viewIdx = appsJs.indexOf("openSimpleModal('app-view-modal'");
assert.ok(viewIdx > 0, "apps.js must keep the View modal (openSimpleModal('app-view-modal'…)");
const viewCall = appsJs.slice(viewIdx, appsJs.indexOf('else if (act ===', viewIdx));
assert.ok(/\$\{detail\.hostname \? _edgeCaddyBox\(detail\.hostname, detail\.edge_upstream\) : ''\}/.test(viewCall),
    'the View modal must render the edge-Caddy box with detail.edge_upstream when the app has a hostname');
assert.ok(/_wireEdgeCaddyCopy\(\);/.test(viewCall),
    'the View modal must wire the Copy button after the modal is built');

// --- 5) escaping -----------------------------------------------------------
const _boxStart = appsJs.indexOf('function _edgeCaddyBox');
const boxFn = appsJs.slice(_boxStart, _boxStart + 1600);
assert.ok(/\$\{escapeHtml\(block\)\}/.test(boxFn),
    'the block must be escaped (escapeHtml) before it reaches innerHTML');
assert.ok(/\$\{escapeHtml\(note\)\}/.test(boxFn),
    'the note must be escaped too — hostnames are owner data');
assert.ok(!/>\s*\$\{(block|note|hostname)\}/.test(boxFn),
    'no raw ${…} may sit directly inside an element body');

// --- 6) settings note only when unset + no rule violations -----------------
assert.ok(/id="app-edge-caddy-copy"/.test(appsJs), 'the box needs a Copy button id');
assert.ok(/window\.modCopyText\(pre\.textContent\)/.test(appsJs),
    'Copy must read the rendered text from the element, not rebuild it (shared copy helper)');
assert.ok(/\$\{upstream \? '' : `<p class="text-xs text-gray-400[^`]*Settings → Platform/.test(boxFn),
    'the muted Settings → Platform note must render ONLY when no upstream is set');
assert.ok(/class="text-xs text-gray-400/.test(boxFn),
    'the settings note must be muted (text-gray-400), not a loud colour');
const _edgeRegion = appsJs.slice(appsJs.indexOf('const EDGE_UPSTREAM_PLACEHOLDER'),
    appsJs.indexOf('function _fmtCreated'));
assert.ok(!/bg-blue-600|text-blue-600/.test(_edgeRegion),
    'the edge-Caddy helpers must not use bg-blue-600/text-blue-600 (use bg-primary/text-primary)');
assert.ok(!/['"`]\/v1\//.test(appsJs), 'no fetch path in apps.js may include /v1');
assert.ok(!/getElementById\([^)]*\)[^;]*;$/.test(
    appsJs.split('\n').filter(l => !/^\s/.test(l)).join('\n'), 'm'),
    'no module-level DOM access in apps.js');

// --- 7) still valid ES module ---------------------------------------------
let jsErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: appsJs, stdio: ['pipe', 'pipe', 'pipe'] });
} catch (e) {
    jsErr = String(e && (e.stderr || e.message));
}
assert.ok(!jsErr, `js/apps.js: not valid ES-module JS — ${(jsErr || '').split('\n')[0] || 'unknown'}`);
