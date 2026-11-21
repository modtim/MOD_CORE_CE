// FIX-DASH-WIDGETS-UNTESTED-MODULES-JEST-TESTS — CORE/Dashboard/js/data-label-chip.js
// (GY.C7 visible data-label markings, Dashboard half). The module is an ES module
// (importable only as ESM), so this test extracts its body — from `function escapeHtml(s) {`
// to EOF, with the `export ` keywords and the trailing `export { escapeHtml };` stripped —
// and runs it in a vm sandbox, then asserts behaviour (not just "does not throw"):
//   (a) normalizeLabel: null/undefined/blank -> 'internal', trims, keeps case;
//   (b) styleForLabel: case-insensitive taxonomy lookup, unknown label -> CUSTOM_STYLE
//       (tone 'controlled', label = uppercased raw);
//   (c) isControlledLabel: public/internal are the only non-controlled tones;
//   (d) dataLabelChip: '' for public under subtleDefault, chip markup otherwise,
//       and a hostile label is HTML-escaped (no raw <img can inject);
//   (e) controlledBannerText: "CONTROLLED: <LABEL> — handle per policy" for controlled
//       labels, '' for the rest;
//   (f) worstLabel: worst rank across a set, null when nothing controlled, null-safe.
//
//   node --test CORE/Dashboard/tests/data-label-chip.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'data-label-chip.js'), 'utf8');
const start = src.indexOf('function escapeHtml(s) {');
assert.ok(start >= 0, 'could not locate the data-label-chip body');
const block = src.slice(start)
    .replace(/export function/g, 'function')
    .replace(/export \{[^}]*\};/g, '');

const fns = vm.runInNewContext(
    `"use strict";\n${block}\n({ normalizeLabel, styleForLabel, isControlledLabel, dataLabelChip, controlledBannerText, worstLabel, escapeHtml });`,
    { console: { log: () => {}, warn: () => {}, error: () => {} } },
    { filename: 'data-label-chip-body' });

test('normalizeLabel: null/blank -> internal, trims, keeps case', () => {
    assert.strictEqual(fns.normalizeLabel(null), 'internal');
    assert.strictEqual(fns.normalizeLabel(undefined), 'internal');
    assert.strictEqual(fns.normalizeLabel('   '), 'internal');
    assert.strictEqual(fns.normalizeLabel('  CUI  '), 'CUI');
    assert.strictEqual(fns.normalizeLabel(42), '42');
});

test('styleForLabel: case-insensitive, unknown -> custom controlled style', () => {
    const itar = fns.styleForLabel('itar');
    assert.strictEqual(itar.label, 'ITAR');
    assert.strictEqual(itar.tone, 'export');
    assert.ok(itar.cls.includes('border-red-900'), 'ITAR colour class missing');

    const pub = fns.styleForLabel('PUBLIC');
    assert.strictEqual(pub.tone, 'public');
    assert.strictEqual(pub.label, 'Public');

    const custom = fns.styleForLabel('embargo-2027');
    assert.strictEqual(custom.tone, 'controlled');
    assert.strictEqual(custom.label, 'EMBARGO-2027');
    assert.ok(custom.cls.includes('border-slate-500'), 'custom fallback class missing');
});

test('isControlledLabel: only public/internal are non-controlled', () => {
    assert.strictEqual(fns.isControlledLabel('public'), false);
    assert.strictEqual(fns.isControlledLabel('internal'), false);
    assert.strictEqual(fns.isControlledLabel(null), false);
    for (const label of ['confidential', 'restricted', 'CUI', 'EAR', 'ITAR', 'pii', 'biometric', 'pre-release']) {
        assert.strictEqual(fns.isControlledLabel(label), true, `${label} must be controlled`);
    }
});

test('dataLabelChip: public hidden by default, others render, hostile label escaped', () => {
    assert.strictEqual(fns.dataLabelChip('public'), '', 'public chip must be empty by default');
    const shown = fns.dataLabelChip('public', { subtleDefault: false });
    assert.ok(shown.includes('data-label-chip="Public"'), 'subtleDefault=false must still render');

    const cui = fns.dataLabelChip('CUI');
    assert.ok(cui.includes('data-label-chip="CUI"'), 'CUI chip missing');
    assert.ok(cui.includes('Data classification: CUI'), 'title copy missing');

    // null normalizes to internal, which is NOT public -> it renders.
    const internal = fns.dataLabelChip(null);
    assert.ok(internal.includes('data-label-chip="Internal"'), 'internal chip missing');

    const hostile = fns.dataLabelChip('<img src=x onerror=alert(1)>');
    assert.ok(hostile.includes('&lt;IMG SRC=X ONERROR=ALERT(1)&gt;'),
        'hostile label must be escaped (and upper-cased by the custom-label path)');
    assert.ok(!/<img/i.test(hostile), 'raw <img survived into markup');
    assert.ok(hostile.includes('data-label-chip="&lt;IMG SRC=X ONERROR=ALERT(1)&gt;"'),
        'escaped custom-label chip missing');
});

test('controlledBannerText: convention string vs empty', () => {
    assert.strictEqual(fns.controlledBannerText('restricted'),
        'CONTROLLED: Restricted — handle per policy');
    assert.strictEqual(fns.controlledBannerText('CUI'),
        'CONTROLLED: CUI — handle per policy');
    assert.strictEqual(fns.controlledBannerText('public'), '');
    assert.strictEqual(fns.controlledBannerText('internal'), '');
});

test('worstLabel: worst rank wins, null when nothing controlled, null-safe', () => {
    assert.strictEqual(fns.worstLabel(null), null);
    assert.strictEqual(fns.worstLabel([]), null);
    assert.strictEqual(fns.worstLabel(['public', 'internal']), null);
    assert.strictEqual(fns.worstLabel(['restricted', 'CUI']), 'CUI', 'CUI outranks restricted');
    assert.strictEqual(fns.worstLabel(['CUI', 'ITAR']), 'ITAR', 'ITAR outranks CUI');
    assert.strictEqual(fns.worstLabel(['CUI', 'EAR']), 'EAR', 'EAR outranks CUI');
    assert.strictEqual(fns.worstLabel(['pii', 'biometric', 'pre-release']), 'pii',
        'first of equal rank must win');
});
