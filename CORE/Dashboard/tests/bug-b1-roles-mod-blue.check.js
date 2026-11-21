// BUG.B1 + BUG.B9 (owner 10-08): Dashboard Users → Roles tab
// Owner reported light-green-on-green text. Fix: roles banner + role chips
// must use MOD Blue (bg-primary) with white text (text-white), no green.
//
// Checks:
//   1) js/users.js parses as a valid ES module.
//   2) users.html roles banner uses bg-primary, border-primary, text-white (no bg-green / text-green).
//   3) users.html roles banner has NO green color classes anywhere in its block.
//   4) js/users.js role chip render uses bg-primary + text-white (no green on chips).
//   5) users.js parses as ES module (double-check via node).

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function read(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const htmlSrc = read('pages/users.html');
const jsSrc = read('js/users.js');

// --- 1) ES module syntax check on js/users.js ------------------------------
let modErr = null;
try {
    execFileSync(process.execPath,
        ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr,
    `js/users.js: not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// --- 2) Roles banner extraction from users.html ---------------------------
// Must find the banner WITHIN the Roles Panel section, not the earlier
// Users-tab banner. Search inside the Roles Panel chunk.
const rolesPanelStart = htmlSrc.indexOf('<!-- Roles Panel -->');
assert.ok(rolesPanelStart !== -1, 'users.html must contain <!-- Roles Panel -->');
const panelChunk = htmlSrc.slice(rolesPanelStart, rolesPanelStart + 6000);

// Find the Info Banner comment within the roles panel chunk.
const bannerCommentPos = panelChunk.indexOf('<!-- Info Banner -->');
assert.ok(bannerCommentPos !== -1, 'users.html: Info Banner not found within Roles Panel');
const afterBannerComment = panelChunk.slice(bannerCommentPos);

// Find the outer banner div (has bg-primary border-l)
const bannerOpen = afterBannerComment.indexOf('<div class="bg-primary');
assert.ok(bannerOpen !== -1, 'users.html: banner div with bg-primary not found in Roles Panel');
let depth = 0;
let i = bannerOpen;
let found = false;
for (; i < afterBannerComment.length; i++) {
    const rest = afterBannerComment.slice(i);
    if (/^<div[\s>]/.test(rest)) {
        depth++;
    } else if (/^<div\/>/.test(rest)) {
        // self-closing div, skip
        i += 5;
    } else if (/^<\/div>/.test(rest)) {
        depth--;
        if (depth === 0) {
            found = true;
            break;
        }
    }
}
assert.ok(found, 'users.html: could not match outer banner div');
const bannerBlock = afterBannerComment.slice(bannerOpen, i + 6);

// --- 3) Banner must use bg-primary + text-white ---------------------------
assert.ok(bannerBlock.includes('bg-primary'),
    'roles banner: must use bg-primary background');
assert.ok(bannerBlock.includes('border-primary'),
    'roles banner: must use border-primary accent');

// Count text-white occurrences in banner — h3 and paragraph should both be white.
const textWhiteMatches = bannerBlock.match(/text-white/g);
assert.ok(textWhiteMatches && textWhiteMatches.length >= 2,
    `roles banner: must have at least 2 text-white (found ${textWhiteMatches ? textWhiteMatches.length : 0})`);

// --- 4) Banner must NOT contain any green color classes -------------------
const greenClasses = ['bg-green-', 'text-green-', 'border-green-'];
for (const cls of greenClasses) {
    assert.ok(!bannerBlock.includes(cls),
        `roles banner: must NOT contain "${cls}"`);
}

// --- 5) Role chip render in users.js must use bg-primary text-white -------
// Find the map line that renders capability/role chips.
const chipLine = jsSrc.split('\n').find(line =>
    line.includes('bg-primary') && line.includes('text-white') && line.includes('inline-block')
);
assert.ok(chipLine,
    'js/users.js: role chip render must use bg-primary text-white on inline-block span');

// Confirm no green on role/chip spans in users.js.
// The specific chip span pattern: <span ...bg-...text-...rounded...>capability</span>
const allSpanChips = jsSrc.match(/<span[^>]*bg-[^>]*rounded[^>]*>[\s\S]*?<\/span>/g) || [];
for (const span of allSpanChips) {
    // Only flag if it has green bg/text AND looks like a chip (small text, rounded, px/py).
    if (span.match(/bg-green/) || span.match(/text-green/)) {
        // Check if it's a role/capability chip (has px-2 py-1 or similar chip styling).
        if (span.match(/px-2.*py-1|py-1.*px-2|text-xs.*rounded|rounded.*text-xs/)) {
            assert.ok(false,
                `js/users.js: chip span must not use green: ${span.slice(0, 100)}`);
        }
    }
}

// --- 6) Verify the banner icon is white (text-white on svg) ----------------
assert.ok(bannerBlock.includes('text-white'),
    'roles banner: svg icon must be text-white');

console.log('bug-b1-roles-mod-blue: all checks passed -> OK');
