// Node-runnable check for the compose-import error escaping in
// js/persistent-stacks.js (rc20e SECURITY fix).
// The Stacks page renders the compose Preview/Copy/Create error lines into
// #ps-compose-result via innerHTML (_psComposeResult :713). The message
// interpolates e.message — the API's 422 detail, which carries raw PyYAML
// parser text echoing the operator's uploaded compose. Unescaped, that text
// is parsed as HTML: <block end> is eaten as a tag and <img src=x onerror=…>
// executes. This script extracts _psEsc plus the four catch-line templates
// from the source text (same pattern as ps-gateway-metrics.check.js) and
// asserts an error message containing markup renders as literal text — no
// element-producing markup survives — for all four sites.
//
//   node CORE/Dashboard/tests/persistent-stacks-compose-error-escape.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'persistent-stacks.js'), 'utf8');

const escStart = src.indexOf('function _psEsc(s) {');
const escEnd = src.indexOf('\nfunction _psMsg');
if (escStart < 0 || escEnd < 0) {
    console.error('FAIL could not locate _psEsc in persistent-stacks.js');
    process.exit(1);
}
const escBlock = src.slice(escStart, escEnd);

// Pull the whole template literal around a marker (opening backtick .. closing
// backtick). Markers are unique to the compose-import catch sites.
function grabTemplateAround(marker) {
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const a = src.indexOf('`', i);
    if (a < 0) return null;
    const b = src.indexOf('`', a + 1);
    if (b < 0 || b <= a) return null;
    return src.slice(a, b + 1);
}

const sites = [
    ['import', '`Import failed: '],
    ['copy', '`Copy failed: '],
    ['secret', '`Could not create secret "'],
    ['create', '_psComposeResult(`Create failed: '],
];
const tpls = {};
for (const [name, marker] of sites) {
    const t = grabTemplateAround(marker);
    if (!t) {
        console.error(`FAIL could not locate the ${name} compose-error template`);
        process.exit(1);
    }
    tpls[name] = t;
}

const body = escBlock + '\nreturn {'
    + sites.map(([name]) => `${name}: (e, ns) => (${tpls[name]})`).join(', ')
    + '};';
const F = new Function(body)();

let failures = 0;
let passes = 0;
function check(name, cond) {
    if (cond) { passes++; console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`); }
}

// The exact shape of the bug: a 422 detail echoing the uploaded YAML, with
// markup that the browser used to parse as tags (the <block end> case from
// the r2 repro ate the tag; the img case is the XSS).
const evil = 'expected an inline flow mapping but found a block end '
    + '<block end> at line 3 — <img src=x onerror=alert(1)> in value';
const err = { message: evil };
const ns = { name: 'TOP_SECRET' };

const rendered = {
    import: F.import(err, ns),
    copy: F.copy(err, ns),
    secret: F.secret(err, ns),
    create: F.create(err, ns),
};

for (const [name, out] of Object.entries(rendered)) {
    // The literal text must survive as escaped entities, not as markup.
    check(`${name}: escaped <block end> present`, out.includes('&lt;block end&gt;'));
    check(`${name}: escaped <img> present`, out.includes('&lt;img src=x onerror=alert(1)&gt;'));
    // No raw '<img' / '<block' may reach innerHTML — that is what would create
    // an element (or eat the tag).
    check(`${name}: no img element produced`, !/<img/i.test(out));
    check(`${name}: no block element produced`, !/<block/i.test(out));
    check(`${name}: prefix intact`, out.startsWith('Import failed: ') || out.startsWith('Copy failed: ')
        || out.startsWith('Could not create secret "') || out.startsWith('Create failed: '));
}

// The secret site must still escape the secret NAME (unchanged behaviour).
check('secret: name escaped too', F.secret({ message: 'x' }, { name: '<b>' }).includes('&lt;b&gt;'));

// Plain messages render verbatim (no double-escaping surprises).
const plain = F.import({ message: 'name is required' }, ns);
check('plain: renders as-is', plain === 'Import failed: name is required');

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
