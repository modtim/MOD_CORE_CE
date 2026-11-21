// Admin resource monitor: behaviour checks for the pure formatters.
// (FIX-DASH-TIER2-UNTESTED-MODULES-JEST-TESTS)
//
// The module is a browser ES module (imports api.js/ui.js, touches the DOM
// on load), so the pure helpers are extracted from the source by brace
// matching and evaluated in a fresh vm context. Nothing from the module is
// modified. Behaviours asserted:
//   * fmtNum: null -> em dash, Infinity/NaN -> infinity sign, >=1000 grouped
//     with no decimals, >=10 one decimal, <10 two decimals
//   * fmtCredits: null -> em dash, thousands grouping (en-US)
//   * fmtRatio: null -> em dash, >=1e6 -> infinity sign, precision tiers
//   * ratioColorClass: red above threshold, amber above half threshold,
//     emerald below, gray when ratio is missing
//   * escapeHtml: escapes & < > " ' so user text cannot inject markup
//   * pill: escapes the title attribute and renders the label as given
//
//   node --test CORE/Dashboard/tests/admin-resource-monitor-format.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'js', 'admin-resource-monitor.js'), 'utf8');

// Brace-matched extraction of named top-level functions.
function extractFns(text, names) {
    return names.map((name) => {
        const re = new RegExp('(?:export\\s+)?(?:async\\s+)?function\\s+' + name + '\\s*\\(');
        const m = re.exec(text);
        if (!m) throw new Error('function not found in module: ' + name);
        const open = text.indexOf('{', m.index);
        let depth = 0;
        let j = open;
        for (; j < text.length; j++) {
            if (text[j] === '{') depth++;
            else if (text[j] === '}') {
                depth--;
                if (depth === 0) break;
            }
        }
        return text.slice(m.index, j + 1).replace(/^export\s+/, '');
    });
}

const NAMES = ['fmtNum', 'fmtCredits', 'fmtRatio', 'ratioColorClass', 'pill', 'escapeHtml'];
const code = extractFns(src, NAMES).join('\n') + '\n;({' + NAMES.join(',') + '})';
const h = vm.runInContext(code, vm.createContext({ _thresholds: null }));

test('module still exports the page initializer the shell loads', () => {
    assert.ok(/export function initializeAdminResourceMonitorPage\(/.test(src));
});

test('fmtNum renders missing and non-finite values as placeholders', () => {
    assert.strictEqual(h.fmtNum(null), '—');
    assert.strictEqual(h.fmtNum(undefined), '—');
    assert.strictEqual(h.fmtNum(Infinity), '∞');
    assert.strictEqual(h.fmtNum('not a number'), '∞');
});

test('fmtNum precision tiers: grouped integers, 1 decimal, 2 decimals', () => {
    assert.strictEqual(h.fmtNum(1234.5), '1,235');
    assert.strictEqual(h.fmtNum(1000), '1,000');
    assert.strictEqual(h.fmtNum(42.123), '42.1');
    assert.strictEqual(h.fmtNum(10), '10.0');
    assert.strictEqual(h.fmtNum(3.14159), '3.14');
    assert.strictEqual(h.fmtNum(0), '0.00');
});

test('fmtCredits: placeholder for null, en-US grouping otherwise', () => {
    assert.strictEqual(h.fmtCredits(null), '—');
    assert.strictEqual(h.fmtCredits(1234567), '1,234,567');
    assert.strictEqual(h.fmtCredits('0'), '0');
});

test('fmtRatio: placeholders, runaway ratio, and precision tiers', () => {
    assert.strictEqual(h.fmtRatio(null), '—');
    assert.strictEqual(h.fmtRatio(NaN), '∞');
    assert.strictEqual(h.fmtRatio(1e6), '∞');
    assert.strictEqual(h.fmtRatio(999999), '999999');
    assert.strictEqual(h.fmtRatio(250.4), '250');
    assert.strictEqual(h.fmtRatio(42.123), '42.1');
    assert.strictEqual(h.fmtRatio(3.14159), '3.14');
});

test('ratioColorClass: gray when missing, red over threshold, amber over half', () => {
    assert.strictEqual(h.ratioColorClass(null, 10), 'bg-gray-100 text-gray-500');
    assert.strictEqual(h.ratioColorClass(Infinity, 10), 'bg-red-100 text-red-900');
    assert.strictEqual(h.ratioColorClass(1e6, 10), 'bg-red-100 text-red-900');
    assert.strictEqual(h.ratioColorClass(11, 10), 'bg-red-100 text-red-900');
    assert.strictEqual(h.ratioColorClass(6, 10), 'bg-amber-100 text-amber-800');
    assert.strictEqual(h.ratioColorClass(5, 10), 'bg-emerald-100 text-emerald-800');
    assert.strictEqual(h.ratioColorClass(4, 10), 'bg-emerald-100 text-emerald-800');
});

test('ratioColorClass without a threshold never goes red for a finite ratio', () => {
    assert.strictEqual(h.ratioColorClass(100, undefined), 'bg-emerald-100 text-emerald-800');
    assert.strictEqual(h.ratioColorClass(100, 0), 'bg-emerald-100 text-emerald-800');
});

test('escapeHtml escapes all five markup characters and tolerates null', () => {
    assert.strictEqual(h.escapeHtml(null), '');
    assert.strictEqual(h.escapeHtml(undefined), '');
    assert.strictEqual(
        h.escapeHtml(`<img src="x" onerror='alert(1)'>&`),
        '&lt;img src=&quot;x&quot; onerror=&#39;alert(1)&#39;&gt;&amp;',
    );
});

test('pill escapes the title attribute and keeps the label and colour class', () => {
    const out = h.pill('G 1.00', 'bg-emerald-100 text-emerald-800', 'a "b" <c>');
    assert.ok(out.includes('title="a &quot;b&quot; &lt;c&gt;"'), out);
    assert.ok(out.includes('bg-emerald-100 text-emerald-800'), out);
    assert.ok(out.includes('>G 1.00</span>'), out);
    assert.ok(out.startsWith('<span class="inline-block'), out);
});
