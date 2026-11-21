'use strict';
// BUG.B42 (owner 10-08): Dashboard -> Settings -> API Keys "API Key Created
// Successfully!" panel. Owner wants ALL text in that panel white.
// Fix: panel background must be SOLID MOD Blue (bg-primary, not a green tint),
// every text element inside carries text-white, the code/pre blocks use a dark
// bg (bg-gray-900) with white text, and no text-gray/text-green/text-blue/
// text-primary classes remain on text inside the panel.
//
// Checks:
//   1) pages/settings.html parses (panel div matched by depth).
//   2) panel background is solid bg-primary (no bg-green-*, no bg-primary/NN tint).
//   3) no forbidden text-color classes inside the panel.
//   4) text-white present on every text-bearing element (h3/h4/p/pre/code/svg/button).
//   5) js/settings.js parses as ES module + copy-flash no longer re-adds green.

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const htmlSrc = fs.readFileSync(path.join(ROOT, 'pages', 'settings.html'), 'utf8');
const jsSrc = fs.readFileSync(path.join(ROOT, 'js', 'settings.js'), 'utf8');

// --- 1) extract the panel block ------------------------------------------
const anchor = htmlSrc.indexOf('<div id="new-api-key-display"');
assert.ok(anchor !== -1, 'settings.html: new-api-key-display panel not found');

let depth = 0;
let i = anchor;
let end = -1;
for (; i < htmlSrc.length; i++) {
    const rest = htmlSrc.slice(i);
    if (/^<div[\s>]/.test(rest)) depth++;
    else if (/^<\/div>/.test(rest)) {
        depth--;
        if (depth === 0) { end = i + 6; break; }
    }
}
assert.ok(end !== -1, 'settings.html: could not match panel div');
const panel = htmlSrc.slice(anchor, end);

// --- 2) solid MOD Blue background ---------------------------------------
const openTag = panel.slice(0, panel.indexOf('>') + 1);
assert.ok(/(^|[\s"])bg-primary([\s"])/.test(openTag),
    'panel: outer div must use solid bg-primary (MOD Blue)');
assert.ok(!/bg-green-\d/.test(openTag), 'panel: outer div must not use a green bg tint');
assert.ok(!/bg-primary\/\d/.test(openTag),
    'panel: outer div bg must be solid bg-primary, not a bg-primary/NN tint');

// --- 3) no forbidden text colors anywhere in the panel -------------------
const forbidden = ['text-gray', 'text-blue', 'text-primary', 'text-green',
                   'text-emerald', 'text-slate'];
for (const cls of forbidden) {
    assert.ok(!panel.includes(cls),
        `panel: must not contain "${cls}" on any text element`);
}
assert.ok(!panel.includes('bg-green-'), 'panel: no green backgrounds left');
assert.ok(!panel.includes('border-green-'), 'panel: no green borders left');
assert.ok(!panel.includes('bg-white'), 'panel: no white code blocks (must be dark bg)');

// --- 4) text-white on every text-bearing element -------------------------
const textWhiteCount = (panel.match(/text-white/g) || []).length;
assert.ok(textWhiteCount >= 8,
    `panel: expected at least 8 text-white (found ${textWhiteCount})`);

for (const tag of ['h3', 'h4', 'pre']) {
    const m = panel.match(new RegExp(`<${tag}([^>]*)>`));
    assert.ok(m, `panel: <${tag}> not found`);
    assert.ok(m[1].includes('text-white'),
        `panel: <${tag}> must carry text-white`);
}
// paragraphs: every <p ...> inside the panel is white
const pTags = panel.match(/<p\b[^>]*>/g) || [];
assert.ok(pTags.length >= 2, 'panel: expected at least 2 <p> elements');
for (const t of pTags) {
    assert.ok(t.includes('text-white'), `panel: <p> missing text-white -> ${t}`);
}
// inline code blocks: dark bg + white text
const codeTags = panel.match(/<code\b[^>]*>/g) || [];
assert.ok(codeTags.length >= 3, 'panel: expected at least 3 <code> elements');
for (const t of codeTags) {
    assert.ok(t.includes('text-white'), `panel: <code> missing text-white -> ${t}`);
    assert.ok(t.includes('bg-gray-900'), `panel: <code> must use dark bg -> ${t}`);
}
// svg icon white
const svg = panel.match(/<svg[^>]*>/);
assert.ok(svg && svg[0].includes('text-white'), 'panel: svg icon must be text-white');
// buttons: label text readable (white)
const btns = panel.match(/<button[^>]*>/g) || [];
assert.ok(btns.length >= 3, 'panel: expected at least 3 buttons');
for (const b of btns) {
    assert.ok(b.includes('text-white'), `panel: button label must be white -> ${b}`);
}

// --- 5) js/settings.js ES module + no green re-added on copy flash -------
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: jsSrc, encoding: 'utf8' });
} catch (e) {
    modErr = String(e && (e.stderr || e.message));
}
assert.ok(!modErr, `js/settings.js: not valid ES module - ${(modErr || '').split('\n')[0] || 'unknown'}`);

const copyFn = jsSrc.slice(jsSrc.indexOf('async function copyNewApiKey()'));
const copyBlock = copyFn.slice(0, copyFn.indexOf('async function handleRevokeApiKey'));
assert.ok(copyBlock.length > 0, 'js/settings.js: copyNewApiKey block not found');
assert.ok(!copyBlock.includes('bg-green'),
    'js/settings.js: copyNewApiKey must not re-add green button classes');
assert.ok(copyBlock.includes('bg-primary'),
    'js/settings.js: copyNewApiKey must restore bg-primary on the copy button');

console.log('bug-b42-apikey-created-white: all checks passed -> OK');
