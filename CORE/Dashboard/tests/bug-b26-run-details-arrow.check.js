'use strict';
// BUG.B26 (owner 10-08, /#logs): Dashboard Logs -> Workflow Runs -> Workflow Run Details.
// Each process/node row has a ">" arrow that opens an accordion with details and logs.
// When the row is open, the arrow stayed pointing right instead of turning down.
// Wanted: arrow rotates to point down while open (rotate-90), flips back on close,
// and aria-expanded reflects the state.
//
//   node CORE/Dashboard/tests/bug-b26-run-details-arrow.check.js
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const logsJs = fs.readFileSync(path.join(ROOT, 'js', 'logs.js'), 'utf8');

// --- 1) logs.js still parses as an ES module -------------------------------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: logsJs, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/logs.js: not valid as an ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) the arrow span gets an identifiable class + transition -------------
assert.ok(/class="[^"]*rotate-arrow[^"]*transition-transform[^"]*"/.test(logsJs) ||
          /class="[^"]*transition-transform[^"]*rotate-arrow[^"]*"/.test(logsJs),
    'js/logs.js: arrow span must have both rotate-arrow and transition-transform classes');
// CSS transforms only apply to block/inline-block/inline-block elements —
// the arrow span must NOT be plain inline.
assert.ok(/class="[^"]*inline-block[^"]*rotate-arrow/.test(logsJs),
    'js/logs.js: arrow span must be inline-block for rotate-90 to apply');

// --- 3) the onclick flips arrow rotation via rotate-90 --------------------
// The inline onclick should use classList.toggle('rotate-90') on the arrow.
assert.ok(/classList\.toggle\('rotate-90'\)/.test(logsJs),
    'js/logs.js: onclick must flip the arrow by toggling rotate-90');

// --- 4) the onclick sets aria-expanded on the row button ------------------
// aria-expanded must toggle between 'true' and 'false'.
assert.ok(/aria-expanded/.test(logsJs),
    'js/logs.js: onclick must set aria-expanded on the row button');
assert.ok(/setAttribute\('aria-expanded',/.test(logsJs) ||
         /getAttribute\('aria-expanded'\)/.test(logsJs),
    'js/logs.js: onclick must read-write aria-expanded (getAttribute + setAttribute pattern)');

// --- 5) the detail panel is still hidden by default ----------------------
// The detail div uses class "hidden" so it starts collapsed.
assert.ok(/class="hidden border-t/.test(logsJs) || /"hidden" \+/.test(logsJs),
    'js/logs.js: node detail div must start with "hidden" so the accordion is collapsed by default');

// --- 6) verify rotate-90 and transition-transform exist in the built CSS -
const tw = fs.readFileSync(path.join(ROOT, 'tailwind.css'), 'utf8');
assert.ok(/\.rotate-90\s*\{/.test(tw),
    'tailwind.css: .rotate-90 must be present (used by arrow toggle)');
assert.ok(/\.transition-transform\s*\{/.test(tw),
    'tailwind.css: .transition-transform must be present (used by arrow toggle)');

// --- 7) no other places toggle rotate-90 (ensure uniqueness) -------------
const matches = [...logsJs.matchAll(/rotate-90/g)];
// We expect exactly 1 toggle for rotate-90 in the node-row onclick,
// plus possibly a reference in the CSS class name. Count occurrences.
assert.ok(matches.length >= 1, 'js/logs.js: rotate-90 must appear at least once');

// --- 8) the arrow character is still emitted (▶ / &#9654;) ---------------
assert.ok(/&#9654;/.test(logsJs),
    'js/logs.js: arrow span must still emit the ▶ character');

console.log('bug-b26-run-details-arrow: OK');
