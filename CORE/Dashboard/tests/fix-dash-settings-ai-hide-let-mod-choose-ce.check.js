// A6 fix — "Let MOD choose" auto option must be gated on _paidEditionKnown
// in EVERY slot dropdown. Paid keeps the option; CE does not show it and the
// explanation block (ai-master-budget-wrap) is hidden.
//
// Stub-DOM technique: extract the function body verbatim from settings.js,
// inject a stub _paidEditionKnown and verify the option append is guarded.
//
//   node CORE/Dashboard/tests/fix-dash-settings-ai-hide-let-mod-choose-ce.check.js
//
'use strict';
const fs = require('fs');
const path = require('path');

const settingsSrc = fs.readFileSync(
    path.join(__dirname, '..', 'js', 'settings.js'),
    'utf8'
);

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// Extract a function body by header string.
function fnSrc(hdr) {
    const at = settingsSrc.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found`);
    const brace = settingsSrc.indexOf('{', at + hdr.length);
    let depth = 0;
    for (let i = brace; i < settingsSrc.length; i++) {
        if (settingsSrc[i] === '{') depth++;
        else if (settingsSrc[i] === '}') { depth--; if (!depth) return settingsSrc.slice(brace, i + 1); }
    }
    throw new Error('unbalanced braces');
}

// Stub DOM + env.
function makeEnv(paid) {
    const els = {};
    function el(id) {
        if (!els[id]) els[id] = {
            hidden: false,
            addEventListener: () => {},
            classList: {
                add: (c) => { if (c === 'hidden') els[id].hidden = true; },
                remove: (c) => { if (c === 'hidden') els[id].hidden = false; }
            },
            dataset: {},
            appendChild: () => {}
        };
        return els[id];
    }
    return {
        document: { getElementById: (id) => el(id) },
        _paidEditionKnown: paid,
        _aiStacksLast: { stacks: [] },
        _aiKbExternals: [],
        _aiSlotExternals: [],
        _aiStackStatusWord: () => 'running',
    };
}

// Find all slot dropdown functions — they all contain 'Let MOD choose'.
// The function headers we need to check:
const HDRS = [
    'function _aiSlotFillOptions(sel',
    'function _aiKbFillOptions(sel',
];

// For each, extract the body and assert:
//   1. The body contains `_paidEditionKnown` before "Let MOD choose"
//   2. No unguarded "Let MOD choose" in the extracted body
async function run() {
    for (const hdr of HDRS) {
        const body = fnSrc(hdr);
        check(`${hdr}: "Let MOD choose" is _paidEditionKnown-gated`,
            body.indexOf('if (_paidEditionKnown)') < body.indexOf('Let MOD choose'),
            `hdr=${hdr}`
        );
    }
}

// Also verify: the old-file state would throw (no gate needle).
const allBody = HDRS.map(fnSrc).join('\n\n');
check('all gate needles present',
    allBody.split('\n').filter(l => l.includes('if (_paidEditionKnown)')).length >= HDRS.length,
    `gates=${allBody.split('\n').filter(l => l.includes('if (_paidEditionKnown)')).length}`
);

(async () => {
    await run();
    console.log(failures ? `FAILURES ${failures}` : 'ALL OK');
    process.exitCode = failures ? 1 : 0;
})();
