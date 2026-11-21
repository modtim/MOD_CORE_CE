// Bug B70 check — Compliance hub → Audit log: the bottom (events) table ran
// off the screen. Long values (actor/target emails, "resource_type #id",
// metadata JSON) are single-line (whitespace-nowrap) cells, so the table grew
// wider than the viewport and the page scrolled sideways.
//
// Fix: the table keeps its overflow-x-auto wrapper (scroll the TABLE, never the
// page), is w-full instead of min-w-full, and the long columns wrap
// (max-w-xs break-words) or truncate (max-w-xs truncate) with the full value
// kept in a title attribute.
//
//   node --check CORE/Dashboard/tests/bug-b70-audit-table-fits.check.js
//   node CORE/Dashboard/tests/bug-b70-audit-table-fits.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'pages', 'audit-logs.html'), 'utf8');
const js = fs.readFileSync(path.join(root, 'js', 'audit-logs.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'tailwind.css'), 'utf8');

// Purged CSS escapes the dot in fractional utilities (`.py-1\.5`) and keeps
// combinators/pseudo-classes after the name (`.divide-y > :not([hidden])`).
function hasClass(c) {
    const esc = c.replace(/[.:/]/g, '\\\\$&');
    return new RegExp(`\\.${esc}(?![a-z0-9-])`).test(css);
}

// ---------------------------------------------------------------------------
// 1. The events table must sit inside an overflow-x-auto wrapper
// ---------------------------------------------------------------------------
const wrapper = html.match(/<div class="[^"]*overflow-x-auto[^"]*">\s*<table/);
assert.ok(wrapper,
    'pages/audit-logs.html: the events table must be wrapped in <div class="overflow-x-auto">');

// The table must be w-full (bounded by its container), not min-w-full
// (min-w-full forces the table at least as wide as its content = off-screen).
assert.ok(/<table class="[^"]*\bw-full\b[^"]*"/.test(html),
    'pages/audit-logs.html: the events table must carry w-full');
assert.ok(!/<table class="[^"]*min-w-full/.test(html),
    'pages/audit-logs.html: the events table must NOT use min-w-full (it forces overflow)');

// The page container must stay width-bounded so the page itself cannot scroll
// sideways.
assert.ok(/id="audit-logs-container"[^>]*\bmax-w-7xl\b/.test(html),
    'pages/audit-logs.html: #audit-logs-container must stay max-w-7xl (page must not scroll horizontally)');

// ---------------------------------------------------------------------------
// 2. Long columns in the rendered rows must wrap or truncate
// ---------------------------------------------------------------------------
const tds = js.match(/<td[^>]*>[\s\S]*?<\/td>/g) || [];
assert.ok(tds.length >= 7, 'js/audit-logs.js: expected the 7 event-table cells to be rendered');

const longCells = tds.filter(t => /actor_email|target_email|resource_type|metadata/.test(t));
assert.ok(longCells.length >= 4,
    'js/audit-logs.js: the long columns (actor, target, resource, details/JSON) must be rendered');
longCells.forEach(cell => {
    assert.ok(/max-w-xs/.test(cell) && (/break-words/.test(cell) || /truncate/.test(cell)),
        `js/audit-logs.js: long cell must use max-w-xs + break-words or truncate — found ${cell}`);
});

// The short columns (timestamp, action pill, level) may stay nowrap.
const nowrapCells = tds.filter(t => /whitespace-nowrap/.test(t));
nowrapCells.forEach(cell => assert.ok(!/max-w-xs/.test(cell),
    `js/audit-logs.js: a cell cannot be both whitespace-nowrap and max-w-xs — found ${cell}`));

// ---------------------------------------------------------------------------
// 3. Full values must be reachable via title attributes
// ---------------------------------------------------------------------------
assert.ok(/function titleAttr\(/.test(js),
    'js/audit-logs.js: must define a titleAttr() helper for the full cell value');
assert.ok(/function escapeAttr\(/.test(js),
    'js/audit-logs.js: titleAttr must escape quotes — escapeAttr() helper required');
assert.ok(/function cellText\(/.test(js),
    'js/audit-logs.js: must define a cellText() helper for the shortened cell value');

const titleUses = js.match(/\$\{titleAttr\(/g) || [];
assert.ok(titleUses.length >= 4,
    'js/audit-logs.js: each long column must render a title attribute with the full value');
assert.ok(/title="\$\{escapeAttr\(/.test(js) || /titleAttr\(/.test(js),
    'js/audit-logs.js: title attributes must carry the escaped full value');

// The truncation helper must actually shorten (and mark it with an ellipsis).
const cellTextBody = js.match(/function cellText\(value\)\s*\{[\s\S]*?\n\s*\}/);
assert.ok(cellTextBody, 'js/audit-logs.js: cellText() body missing');
assert.ok(/slice\(/.test(cellTextBody[0]) && /…/.test(cellTextBody[0]),
    'js/audit-logs.js: cellText() must slice the value and append an ellipsis');

// ---------------------------------------------------------------------------
// 4. Every class used by the fix must exist in the purged tailwind.css
// ---------------------------------------------------------------------------
const USED = ['overflow-x-auto', 'w-full', 'max-w-xs', 'break-words', 'truncate',
    'px-6', 'py-4', 'text-sm', 'text-gray-900', 'text-primary', 'divide-y',
    'divide-gray-200', 'bg-white', 'bg-gray-50', 'max-w-7xl', 'mx-auto'];
USED.forEach(c => assert.ok(hasClass(c),
    `tailwind.css: purged CSS is missing .${c} used by the audit events table`));

// table-fixed is NOT in the purged CSS — the fix must not claim it.
assert.ok(!/table-fixed/.test(html) && !/table-fixed/.test(js),
    'audit-logs: table-fixed is not in the purged tailwind.css — must not be used');

console.log('bug-b70-audit-table-fits: all checks passed');
