'use strict';
// FIX-CE-HIDE-BYO check (FIND-2, owner verdict A) — the Settings -> Stack TLS
// page's 'Bring your own certificate' radio is hidden on CE ONLY. No
// certificate is ever loaded in CE (caddy-entrypoint.sh:444-466 loads none;
// the cert-watch sidecar files are not shipped), so the radio must not be
// offered; paid keeps its original wording and behaviour.
//
// Same technique as settings-aicfg-ce-gate.check.js: the hide block is
// EXTRACTED VERBATIM from the shipped settings.js text (between the FIX-CE-
// HIDE-BYO comment marker and the closing brace) and run against a stub DOM,
// so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/settings-byo-ce-hide.check.js
//
// Asserts: CE -> the byo radio AND its label get class 'hidden'; paid ->
// neither gets it (old-file state: no marker at all -> throw -> non-zero exit).
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

const MARKER = 'FIX-CE-HIDE-BYO';
const markerAt = settingsSrc.indexOf(MARKER);
if (markerAt < 0) throw new Error('marker not found in settings.js (old-file state)');
const ifAt = settingsSrc.indexOf('if (!await isPaidEdition()) {', markerAt);
if (ifAt < 0) throw new Error('hide guard not found after marker (old-file state)');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
const guardSrc = `if (!await isPaidEdition()) ${blockAt(settingsSrc, ifAt + 'if (!await isPaidEdition()) '.length)}`;

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

async function run2(paid) {
    const els = {};
    const get = (key) => {
        const k = 'el:' + key;
        if (!els[k]) els[k] = { classes: new Set() };
        const v = els[k];
        if (!v.classList) v.classList = { add: (c) => v.classes.add(c), remove: (c) => v.classes.delete(c) };
        return v;
    };
    const doc = { getElementById: (id) => get(id), querySelector: (sel) => get(sel), querySelectorAll: () => [] };
    const env = { document: doc, isPaidEdition: () => paid };
    const names = Object.keys(env);
    const body = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ')
        + ` return (async () => { ${guardSrc} })();`;
    await (await new Function('__env', body)(env));
    return els;
}

(async () => {
    const ce = await run2(false);
    const radio = ce['el:stack-tls-mode-byo'];
    const label = ce['el:label[for="stack-tls-mode-byo"]'];
    check('CE: byo radio hidden', radio && radio.classes.has('hidden'), radio ? [...radio.classes].join(',') : 'missing');
    check('CE: byo label hidden', label && label.classes.has('hidden'), label ? [...label.classes].join(',') : 'missing');

    const pro = await run2(true);
    const proRadio = pro['el:stack-tls-mode-byo'];
    const proLabel = pro['el:label[for="stack-tls-mode-byo"]'];
    // On paid the guard block never runs, so the stub elements are never even
    // created — that IS the original behaviour (nothing hidden, nothing touched).
    check('paid: byo radio NOT hidden (guard block skipped)', !proRadio || !proRadio.classes.has('hidden'), proRadio ? [...proRadio.classes].join(',') : 'missing = block skipped (ok)');
    check('paid: byo label NOT hidden', !proLabel || !proLabel.classes.has('hidden'), proLabel ? [...proLabel.classes].join(',') : 'missing = block skipped (ok)');

    if (failures) { console.error(`${failures} failed`); process.exit(1); }
    console.log('settings-byo-ce-hide: 4 ok / 0 failed');
})();
