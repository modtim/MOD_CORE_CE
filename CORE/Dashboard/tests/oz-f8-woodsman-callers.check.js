// OZ-F8 (launch-completion-megaplan Group OZ) - Woodsman Converter callers.
//
// owner 11-15 8:00 AM CT round 10: the /v1/nodes/builds family is a HARD rename
// IN PLACE to /v1/woodsman (tag "Woodsman Converter"), NO aliases; the old path
// 404s. Every caller must therefore use the new path form. This check is a
// source-pin (same style as CORE/WorkflowBuilder/__tests__/wizardClusterCapabilities.test.tsx):
// it reads the three Dashboard caller files and asserts
//   * 0 'nodes/builds' route literals remain in them, and
//   * each carries the new /woodsman path form. Dashboard calls go through
//     apiCall()/apiUrl which already carry /v1, so the form is
//     '/woodsman/...' - never a doubled /v1.
//
//   node --test CORE/Dashboard/tests/oz-f8-woodsman-callers.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');

const CALLERS = [
    { file: 'js/change-gate-report.js', needle: "apiCall('/woodsman/wizard/variant-change-gate-weekly-report')" },
    { file: 'js/workers.js', needle: '${apiUrl}/woodsman/wizard/variant-test-pending-status' },
    { file: 'MANUAL_TEST_WIZARD_QUEUE.md', needle: 'woodsman/wizard/repair-artifacts' },
];

for (const c of CALLERS) {
    test.describe(`OZ-F8 Dashboard caller ${c.file}`, () => {
        const src = fs.readFileSync(path.join(ROOT, c.file), 'utf8');
        test('no nodes/builds route literal survives', () => {
            assert.strictEqual(src.includes('nodes/builds'), false,
                `${c.file} still carries a nodes/builds literal`);
        });
        test('new /woodsman path form is present', () => {
            assert.ok(src.includes(c.needle), `${c.file} missing ${c.needle}`);
        });
        test('no doubled /v1 prefix', () => {
            assert.strictEqual(src.includes('/v1/v1/woodsman'), false, 'doubled /v1 prefix');
        });
    });
}
