// Node-runnable check for the CE edition predicate api.js isPaidEdition()
// (CORE/Dashboard/js/api.js, rc20 failopen-c class: "errored/unknown
// entitlements payload unlocks paid-only behaviour").
//
// main.py's /v1/platform/features entitlements block NEVER 500s: on failure it
// returns the stub {"edition":"ce","error":"unresolved"} (CORE/API/main.py:1530),
// and on a paid on-prem image that stub's "ce" is a LIE. So the predicate must
// treat an errored / missing / unreadable payload as UNKNOWN, and unknown must
// never unlock the paid-only surfaces (billing, plans, coupons, settings AI
// config, workers, users, admin-* ... ~90 call sites import this helper).
//
// Same technique as analytics-burst-ce-gate.check.js / admin-nodes-packages-ce-gate.check.js:
// the code is EXTRACTED VERBATIM from the shipped source text and run against a
// stub fetch, so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/ispaid-failclosed.check.js
//
// Asserts: errored stub / fetch reject / missing edition -> NOT paid (false);
// positive saas|pro success -> true (paid behaviour unchanged); positive ce -> false.
'use strict';
const fs = require('fs');
const path = require('path');

const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

// Brace-matched block whose '{' is at `open`.
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function stmtAt(src, marker) {
    const at = src.indexOf(marker);
    if (at < 0) throw new Error(`marker not found: ${marker}`);
    const end = src.indexOf(';', at);
    if (end < 0) throw new Error(`no ';' after ${marker}`);
    return src.slice(at, end + 1);
}
function fnAt(src, header) {
    const at = src.indexOf(header);
    if (at < 0) throw new Error(`fn not found: ${header}`);
    const body = blockAt(src, src.indexOf('{', at + header.length));
    return `${header.replace(/^export\s+/, '')} ${body}`;
}

const shipped = [
    stmtAt(apiSrc, 'const _featuresCache ='),
    stmtAt(apiSrc, 'const FEATURES_TTL_MS ='),
    fnAt(apiSrc, 'export function getPlatformFeatures()'),
    fnAt(apiSrc, 'export async function isPaidEdition()'),
].join('\n');

let failures = 0;
let passes = 0;
function check(name, cond, extra) {
    if (cond) { passes++; console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// Run the shipped predicate against a stub fetch. Fresh cache per call (the
// consts live inside the generated function body).
async function paidFor(payload, reject) {
    const body = `${shipped}
return isPaidEdition();`;
    const stubFetch = (url) => {
        if (reject) return Promise.reject(new TypeError('network'));
        return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(payload),
        });
    };
    try {
        return await new Function('fetch', 'API_BASE_URL', body)(stubFetch, 'http://stub');
    } catch (e) {
        return `THREW:${e}`;
    }
}

(async () => {
    // 1. main.py's entitlements FAILURE stub (the exact shape at main.py:1530).
    check('errored stub {edition:ce,error:unresolved} -> NOT paid',
        await paidFor({ entitlements: { edition: 'ce', error: 'unresolved' } }, false) === false,
        'fail-open: an errored payload unlocked the paid surfaces');

    // 2. fetch itself rejects (offline / HTTP != ok -> getPlatformFeatures throws).
    check('fetch reject -> NOT paid',
        await paidFor({}, true) === false,
        'catch returned true (paid) on an unreadable payload');

    // 3. payload with no entitlements block at all (truncated / unknown).
    check('missing entitlements block -> NOT paid',
        await paidFor({ deployment_mode: 'onprem' }, false) === false,
        'unknown edition treated as paid');

    // 4. entitlements present but no edition field (unknown).
    check('entitlements without edition -> NOT paid',
        await paidFor({ entitlements: { tier_name: 'x' } }, false) === false,
        'unknown edition treated as paid');

    // 5. positive paid editions must stay unlocked (behaviour unchanged).
    check('saas success -> paid (true)',
        await paidFor({ entitlements: { edition: 'saas' } }, false) === true,
        'paid tree regressed to CE');
    check('pro success -> paid (true)',
        await paidFor({ entitlements: { edition: 'pro' } }, false) === true,
        'paid tree regressed to CE');

    // 6. positive CE edition stays gated.
    check('ce success -> NOT paid (false)',
        await paidFor({ entitlements: { edition: 'ce' } }, false) === false,
        'CE treated as paid');

    console.log(`counts: ${passes} ok / ${failures} fail`);
    process.exit(failures ? 1 : 0);
})();
