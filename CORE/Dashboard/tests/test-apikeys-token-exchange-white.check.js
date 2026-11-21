// FIX-APIKEYS-TOKEN-EXCHANGE-TEXT-WHITE — static source-text check for the
// Settings > API Keys "Using API Keys (OAuth 2.0 Token Exchange)" info panel:
// every text-bearing element inside the bg-primary/10 panel must carry the
// text-white class (same fix as B42, commit c8d381ca4, which made the
// adjacent "API Key Created Successfully!" panel white on its colored
// background). No browser, no module imports (settings.html exports nothing).
//
//   node CORE/Dashboard/tests/test-apikeys-token-exchange-white.check.js
//
// Checks:
//   1) the panel container still carries bg-primary/10 (colored background).
//   2) no text-primary remains anywhere inside the panel (fails on HEAD copy).
//   3) text-white present on all 5 text-bearing elements (svg icon, h3, intro
//      p, ol, Security p) — class presence, not computed visibility.
//   4) the B42 pattern is intact on the adjacent created-key panel.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'pages', 'settings.html'), 'utf8');

const start = html.indexOf('<!-- Info Banner -->');
assert.ok(start >= 0, 'settings.html must contain the Info Banner marker');
const end = html.indexOf('<!-- New API Key Display', start);
assert.ok(end > start, 'settings.html must contain the New API Key Display marker after the banner');
const panel = html.slice(start, end);

// 1) colored background class still present (m5 bg-transparent fails this)
assert.ok(panel.includes('bg-primary/10'),
    'info panel must keep its bg-primary/10 colored background');

// 2) no dark text-primary left inside the panel (fails on the pre-fix copy)
assert.ok(!panel.includes('text-primary'),
    'info panel must not use text-primary on its bg-primary/10 background');

// 3) all five text-bearing elements carry text-white
assert.ok(panel.includes('<svg class="w-5 h-5 text-white mt-0.5 mr-3 flex-shrink-0"'),
    'info panel svg icon must use text-white');
assert.ok(panel.includes('<h3 class="text-sm font-semibold text-white mb-1">Using API Keys (OAuth 2.0 Token Exchange)</h3>'),
    'info panel h3 heading must use text-white');
assert.ok(panel.includes('<p class="text-sm text-white mb-2">'),
    'info panel intro paragraph must use text-white');
assert.ok(panel.includes('<ol class="text-sm text-white list-decimal list-inside space-y-1 mb-2">'),
    'info panel ordered list must use text-white');
assert.ok(panel.includes('<p class="text-sm text-white">') && panel.includes('<strong>Security:</strong>'),
    'info panel Security paragraph must use text-white');

// 4) B42 pattern (c8d381ca4): adjacent created-key panel uses text-white
assert.ok(html.slice(end, end + 600).includes('text-white'),
    'B42 pattern: new-api-key-display panel must use text-white');

console.log('PASS test-apikeys-token-exchange-white.check.js (panel white on bg-primary/10, B42 pattern intact)');
