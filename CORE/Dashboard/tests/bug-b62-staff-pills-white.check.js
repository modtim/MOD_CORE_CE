// Bug B62 check — Staff Register pill/status-badge text colour.
// Failure-style pills used same-colour text on a same-colour tint
// (bg-red-100 text-red-700 = red text on a red background = unreadable).
// Fix: solid -600 background with text-white, as BUG.B52 did for "Granted"
// (bg-green-600 text-white) in js/compliance-parental-consent.js.
//
//   node --check CORE/Dashboard/tests/bug-b62-staff-pills-white.check.js
//   node CORE/Dashboard/tests/bug-b62-staff-pills-white.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const jsDir = path.join(__dirname, '..', 'js');
const file = path.join(jsDir, 'staff-register.js');
const cssFile = path.join(__dirname, '..', 'tailwind.css');
const src = fs.readFileSync(file, 'utf8');
const css = fs.readFileSync(cssFile, 'utf8');

// ---------------------------------------------------------------------------
// 1. No colour-on-same-colour-tint combos in any pill context
//    (plain text buttons such as the red Delete link are not pills).
// ---------------------------------------------------------------------------
const pillContext = src.match(/<span class="[^"]*rounded-full[^"]*"/g) || [];
const helperSrc = (src.match(/const PILL_COLORS = \{[\s\S]*?\};/) || [''])[0];
const pillText = pillContext.join('\n') + '\n' + helperSrc;

const TINT_PAIRS = [
    ['bg-red-100', 'text-red-700'],
    ['bg-red-100', 'text-red-800'],
    ['bg-green-100', 'text-green-700'],
    ['bg-green-100', 'text-green-800'],
    ['bg-amber-100', 'text-amber-700'],
    ['bg-blue-100', 'text-blue-700'],
    ['bg-gray-100', 'text-gray-500'],
];
TINT_PAIRS.forEach(([bg, fg]) => {
    assert.ok(
        !(pillText.includes(bg) && pillText.includes(fg)),
        `staff-register.js: pill must not use ${bg} + ${fg} (colour on its own tint)`
    );
});

// Any pill (rounded-full span) must carry text-white.
assert.ok(pillContext.length > 0, 'staff-register.js: expected pill spans to be present');
pillContext.forEach(span => {
    const viaHelper = span.includes('${'); // class comes from pill()/PILL_COLORS
    assert.ok(span.includes('text-white') || viaHelper,
        `staff-register.js: pill span must use text-white — found ${span}`);
});

// ---------------------------------------------------------------------------
// 2. The pill helper itself: solid -600 backgrounds + text-white
// ---------------------------------------------------------------------------
const helper = src.match(/const PILL_COLORS = \{[\s\S]*?\};/);
assert.ok(helper, 'staff-register.js: must define a PILL_COLORS pill helper');
const body = helper[0];

assert.ok(body.includes('bg-red-600 text-white'),
    'staff-register.js: red (failure/overdue/never-recorded) pill must be bg-red-600 text-white');
assert.ok(body.includes('bg-green-600 text-white'),
    'staff-register.js: green (OK/complete) pill must be bg-green-600 text-white');
assert.ok(body.includes('bg-amber-600 text-white'),
    'staff-register.js: amber (due soon) pill must be bg-amber-600 text-white');
assert.ok(body.includes('bg-gray-600 text-white'),
    'staff-register.js: gray (deleted) pill must be bg-gray-600 text-white');
assert.ok(body.includes('text-white'),
    'staff-register.js: pill helper must use text-white for readability');

// The leaver pill and the due pill must go through the helper.
assert.ok(/pill\(r\.leaver_complete \? 'green' : 'red'\)/.test(src),
    'staff-register.js: leaver pill must use the pill helper (red when open)');
assert.ok(/class="[^"]*rounded-full[^"]*\$\{due\.cls\}"/.test(src),
    'staff-register.js: training-due pill must render via _dueInfo().cls');

// _dueInfo must return the solid-red classes for its failure states.
const dueInfo = src.match(/function _dueInfo\(row\)[\s\S]*?\n\}/);
assert.ok(dueInfo, 'staff-register.js: _dueInfo must exist');
assert.ok(dueInfo[0].includes('PILL_COLORS.red'),
    'staff-register.js: _dueInfo failure states must use the red (solid) pill colour');

// ---------------------------------------------------------------------------
// 3. Every class used must exist in the purged tailwind.css
// ---------------------------------------------------------------------------
// Purged CSS escapes the dot in fractional utilities (`.py-0\.5`), so accept
// either spelling.
function hasClass(c) {
    const esc = c.replace('.', '\\.');
    return css.includes(`.${c} {`) || css.includes(`.${esc} {`);
}

const USED = ['bg-red-600', 'bg-green-600', 'bg-amber-600', 'bg-gray-600', 'bg-sky-600',
    'text-white', 'rounded-full', 'text-xs', 'px-2', 'py-0.5', 'px-1'];
USED.forEach(c => assert.ok(hasClass(c),
    `tailwind.css: purged CSS is missing .${c} used by staff-register.js pills`));

console.log('bug-b62-staff-pills-white: all checks passed');
