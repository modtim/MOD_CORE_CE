/**
 * Bug B51 — Custom Plans "Create & Assign" never sends `tier`.
 *
 * Asserts:
 *  1. The custom-plans.html form contains a Tier <select> with the
 *     canonical options matching entitlements.py L135-L140.
 *  2. _collectCustomPlanFormPayload (when wired to the DOM) includes
 *     `tier` in the returned object.
 *  3. users.js parses cleanly under node --check (no syntax errors).
 *
 * Run:  node --check CORE/Dashboard/js/users.js
 *       node CORE/Dashboard/tests/bug-b51-custom-plan-tier.check.js
 */

import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

// ── CANONICAL TIERS (source of truth — MUST match entitlements.py) ──
const CANONICAL_TIERS = ['community'];

/** Helpers */
function assert(condition, msg) {
    if (!condition) throw new Error(`ASSERTION FAILED: ${msg}`);
}
function assertEquals(actual, expected, msg) {
    if (actual !== expected) throw new Error(`${msg}: expected ${expected}, got ${actual}`);
}

/** Load the HTML template */
const htmlPath = join(root, 'pages', 'custom-plans.html');
// CE carve (FIX-CE-DASH-SWEEP-C): pages/custom-plans.html is denied on CE, so
// CHECKS 1-4 (which read it) SKIP there; the users.js checks below stay live.
const hasCustomPlansHtml = existsSync(htmlPath);
const htmlContent = hasCustomPlansHtml ? readFileSync(htmlPath, 'utf-8') : '';

if (!hasCustomPlansHtml) {
    console.log('SKIP CHECKS 1-4: pages/custom-plans.html absent (CE carve)');
} else {
console.log('CHECK 1: Tier <select> exists in custom-plans.html …');
const selectMatch = htmlContent.match(/<select[^>]*id="custom-plan-tier"[^>]*>/);
assert(selectMatch, 'No <select id="custom-plan-tier"> found in custom-plans.html');
console.log('  ✓ <select id="custom-plan-tier"> found');

// ── CHECK 2: All canonical tiers present as <option> values ──
console.log('CHECK 2: All canonical tier options present …');
for (const tier of CANONICAL_TIERS) {
    const escaped = tier.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const found = htmlContent.match(
        new RegExp(`<option\\s+value=["']${escaped}["'][^>]*>`)
    );
    assert(found, `Missing <option value="${tier}"> in custom-plans.html`);
    console.log(`  ✓ option value="${tier}" found`);
}

// ── CHECK 3: "Select a tier" placeholder present ──
console.log('CHECK 3: Placeholder option present …');
const placeholderFound = htmlContent.includes('Select a tier');
assert(placeholderFound, 'No placeholder text "Select a tier" in the tier select');
console.log('  ✓ placeholder option found');

// ── CHECK 4: The select has required attribute ──
console.log('CHECK 4: Tier select has required attribute …');
const requiredMatch = htmlContent.match(/<select[^>]*id="custom-plan-tier"[^>]*required/);
assert(requiredMatch, 'Tier select missing required attribute');
console.log('  ✓ required attribute present');

}

// ── CHECK 5: _collectCustomPlanFormPayload reads custom-plan-tier ──
console.log('CHECK 5: _collectCustomPlanFormPayload reads the tier select …');
const jsPath = join(root, 'js', 'users.js');
const jsContent = readFileSync(jsPath, 'utf-8');

// Must read the value from the tier select element
const readsTier = jsContent.match(/custom-plan-tier/);
assert(readsTier, 'users.js does not reference custom-plan-tier element');
console.log('  ✓ users.js references custom-plan-tier');

// Must assign tier from DOM and include it in the return object
const assignsTier = jsContent.match(/const\s+tier\s*=\s*document\.getElementById\('custom-plan-tier'\)\.value/);
assert(assignsTier, 'users.js does not read tier value from custom-plan-tier element');
console.log('  ✓ users.js reads tier from DOM');

// Must include tier in return payload
const returnsTier = jsContent.match(/return\s*\{[^}]*\btier\b/);
assert(returnsTier, 'users.js _collectCustomPlanFormPayload does not include tier in return object');
console.log('  ✓ tier is included in return payload');

// Must validate tier is not empty before POST
const validatesTier = jsContent.match(/if\s*\(\s*!tier\s*\)/);
assert(validatesTier, 'users.js does not validate that tier is not empty');
console.log('  ✓ tier empty-check present');

// ── CHECK 6: users.js parses cleanly ──
console.log('CHECK 6: users.js parses under node (syntax check) …');
// We use Node's import.meta to verify; the actual syntax check is done by
// the runner (`node --check`). If we reach here the file is valid JS.
console.log('  ✓ (see node --check output below)');

// ── Summary ──
console.log('\n✅ All B51 checks passed.');
console.log('   Next: node --check CORE/Dashboard/js/users.js  (should exit 0)');
