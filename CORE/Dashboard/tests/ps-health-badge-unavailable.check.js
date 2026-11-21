// Node-runnable check: _psHealthBadge renders 'unavailable' as gray, not emerald.
//
//   node CORE/Dashboard/tests/ps-health-badge-unavailable.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// --- 1. _psHealthBadge exists and uses _psEsc -----------------------------
check('_psHealthBadge exists', /function _psHealthBadge\(/.test(src));
check('uses _psEsc for grade', /_psEsc\(h\.grade\)/.test(src));
check('uses _psEsc for title', /_psEsc\(title\)/.test(src));
check('data-grade attr', /data-grade="\$\{_psEsc\(h\.grade\)\}"/.test(src));

// --- 2. Style ternary: verify the exact chain ----------------------------
// The style assignment is a single expression spanning the line(s) after "const style ="
const styleLine = src.match(/const style = [^;]+/);
check('style ternary present', !!styleLine);
const styleText = styleLine ? styleLine[0] : '';

check('unhealthy -> red', /grade === 'unhealthy' \? 'bg-red-100 text-red-700'/.test(styleText));
check('pending -> amber', /grade === 'pending' \? 'bg-amber-100 text-amber-700'/.test(styleText));
check('unavailable -> gray', /grade === 'unavailable' \? 'bg-gray-100 text-gray-600'/.test(styleText));
check('catch-all emerald (after unavailable)', /: 'bg-emerald-100 text-emerald-700'/.test(styleText));

// Verify order: unavailable comes before the emerald catch-all
const unavailableIdx = styleText.indexOf('unavailable');
const emeraldIdx = styleText.indexOf('emerald-100');
check('unavailable appears before emerald catch-all', unavailableIdx < emeraldIdx);

// --- 3. Title includes reason -------------------------------------------
const titleLine = src.match(/const title = h\.reason \?[^\n]+/);
check('title uses grade', /grade/.test(titleLine ? titleLine[0] : ''));
check('title uses reason', /reason/.test(titleLine ? titleLine[0] : ''));

console.log(failures === 0 ? '\nPASS' : `\nFAIL: ${failures} assertion(s)`);
process.exit(failures);