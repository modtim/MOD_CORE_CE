// Bug B37 check — Dashboard Settings → Secrets "Required Secrets" banner text color.
// The bg-primary/10 banner was blue text on a blue-tinted background (unreadable).
// Fix: all text inside the banner must be text-white.
//
//   node --check CORE/Dashboard/tests/bug-b37-secrets-required-white.check.js
//   node CORE/Dashboard/tests/bug-b37-secrets-required-white.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const pagesDir = path.join(__dirname, '..', 'pages');

// ---------------------------------------------------------------------------
// Extract the "Required Secrets" banner (the outer <div class="...bg-primary/10...">
// and its matching </div>) from an HTML file using proper depth counting.
// ---------------------------------------------------------------------------
function extractBanner(html, label) {
    // The banner uses solid bg-primary (not the /10 tint variant).
    // We find bg-primary occurrences that are NOT bg-primary/10,
    // and verify we do find bg-primary/10 elsewhere (to assert absence in the banner).
    const bareIdx = html.indexOf('class="bg-primary ');
    const bannerIdx = bareIdx !== -1 ? bareIdx : (html.indexOf('class="bg-primary"') !== -1 ? html.indexOf('class="bg-primary"') : -1);
    assert.ok(bannerIdx !== -1, label + ' must contain a solid bg-primary div for the banner');

    // Walk back to find the <div that contains the class attribute
    let tagStart = bannerIdx;
    while (tagStart > 0 && html[tagStart] !== '<') tagStart--;

    // The banner div itself must NOT mention bg-primary/10.
    assert.ok(
        html.slice(tagStart, tagStart + 500).indexOf('bg-primary/10') === -1,
        label + ' banner container must NOT have bg-primary/10 (was replaced by solid bg-primary)'
    );

    // Count div depth from tagStart to find the matching </div>
    let depth = 0;
    let i = tagStart;
    let found = false;

    while (i < html.length) {
        if (html[i] !== '<') { i++; continue; }
        const rest = html.slice(i);

        // Closing div tag
        if (/^<\/div\s*>/i.test(rest)) {
            if (found) depth--;
            if (depth === 0 && found) {
                return html.slice(tagStart, i + 7);
            }
            i += rest.indexOf('>') + 1;
            continue;
        }

        // Opening div tag (not closing)
        if (/^<div(\s|>)/i.test(rest) && !/^<\/div/i.test(rest)) {
            found = true;
            if (depth === 0) depth = 1;
            else depth++;
            i += rest.indexOf('>') + 1;
            continue;
        }

        i += rest.indexOf('>') + 1;
    }

    assert.fail(label + ' banner div was not properly closed');
}

// ---------------------------------------------------------------------------
// Assertions per banner
// ---------------------------------------------------------------------------
function checkBanner(html, label) {
    const banner = extractBanner(html, label);

    // Must contain the heading text.
    assert.ok(/Required Secrets/i.test(banner),
        label + ' banner must contain "Required Secrets"');

    // Must have text-white present for readability.
    assert.ok(banner.includes('text-white'),
        label + ' banner must include text-white for readability');

    // No blue text classes (text-blue, text-primary, text-sky) on any
    // text-bearing HTML element inside the banner.
    const textTagRe = /<(h[1-6]|p|span|strong|label|a|small|em|b)\b([^>]*)>/gi;
    let m;
    while ((m = textTagRe.exec(banner)) !== null) {
        const tag = m[1].toLowerCase();
        const attrs = m[2] || '';
        const classMatch = attrs.match(/class="([^"]*)"/);
        if (classMatch) {
            const cls = classMatch[1].split(/\s+/);
            for (const c of cls) {
                assert.ok(
                    !/^text-(blue|primary|sky)/.test(c),
                    label + ' banner <' + tag + '> must not have ' + c + ' (' + m[0] + ')'
                );
            }
        }
    }
}

// Run on both files that contain the banner.
checkBanner(
    fs.readFileSync(path.join(pagesDir, 'secrets.html'), 'utf8'),
    'secrets.html'
);
checkBanner(
    fs.readFileSync(path.join(pagesDir, 'settings.html'), 'utf8'),
    'settings.html'
);

console.log('bug-b37-secrets-required-white: all checks passed');
