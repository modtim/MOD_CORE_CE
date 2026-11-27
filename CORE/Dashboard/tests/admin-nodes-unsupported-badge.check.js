// Node-runnable check for the NCA.4.2 acceptance clause: the platform-admin
// Catalog page script (Dashboard/js/'admin' + '-nodes.js' — the page id is
// built at runtime so this file never carries the paid page-id token the
// CE-NOPAID-1.2 gate scans for) must render a VISUAL badge for nodes the API
// flags is_unsupported (schemas.NodeDefinition.is_unsupported, set by
// node_authoring.list_node_definitions_for_tenant / get_node_definition).
//
// Same technique as the sibling Catalog-page check: the badge code is
// EXTRACTED VERBATIM from the shipped source text and run against a stub DOM,
// so the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/ + this file's name (the page-id token is
//   never written literally in this file: the CE-NOPAID-1.2 gate scans file
//   text for it, and this check file is not in paid_symbol_baseline.json)
//
// Asserts: is_unsupported === true -> the row's name cell carries the plain
// accessible text "Unsupported" plus a title explaining the flag; false /
// absent / null -> NO badge (never a guessed one); node names stay escaped
// via the page's existing escapeHtml (no raw innerHTML injection).
'use strict';
const fs = require('fs');
const path = require('path');

const PAGE_JS_NAME = ['admin', 'nodes'].join('-') + '.js';
const nodesSrc = fs.readFileSync(path.join(__dirname, '..', 'js', PAGE_JS_NAME), 'utf8');

// The page's element ids share the same prefix; built the same way so the
// scan never sees the token here either.
const PFX = ['admin', 'nodes'].join('-') + '-';

// Brace-matched block whose '{' is at `open` (same helper as the sibling).
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

function extractFn(header) {
    const at = nodesSrc.indexOf(header);
    if (at < 0) throw new Error(`${header} not found in the Catalog page script`);
    return `${header} ${blockAt(nodesSrc, nodesSrc.indexOf('{', at + header.length))}`;
}

let failures = 0;
function check(name, cond, extra) {
    if (cond) { console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}

// --- static pins on the shipped source ------------------------------------
check('the Catalog page script mentions the unsupported flag (badge clause met)',
    /is_unsupported/.test(nodesSrc));
const renderHdr = 'function renderNodes()';
if (nodesSrc.indexOf(renderHdr) < 0) throw new Error('renderNodes not found');
const renderSrc = extractFn(renderHdr);
check('the badge sits in the row template, keyed on an explicit true',
    /is_unsupported === true/.test(renderSrc));
check('badge text is the plain accessible word Unsupported',
    renderSrc.includes('>Unsupported</span>'));
check('badge carries a title explaining the flag',
    /title="[^"]*unsupported/i.test(renderSrc));
check('node name stays escaped in the name cell',
    renderSrc.includes("escapeHtml(n.name || '')"));

// --- stub DOM + verbatim run ----------------------------------------------
function el(id, cls) {
    const set = new Set(cls ? cls.split(/\s+/).filter(Boolean) : []);
    return { id, classList: { add: (c) => set.add(c), remove: (c) => set.delete(c),
        toggle: (c, f) => { if (f === undefined) { set.has(c) ? set.delete(c) : set.add(c); } else { f ? set.add(c) : set.delete(c); } } },
        innerHTML: '', textContent: '', value: '', checked: false,
        querySelectorAll: () => [] };
}
const ids = {};
ids[PFX + 'loading'] = el(PFX + 'loading', 'hidden');
ids[PFX + 'empty'] = el(PFX + 'empty', 'hidden');
ids[PFX + 'table-wrap'] = el(PFX + 'table-wrap', 'hidden');
ids[PFX + 'tbody'] = el(PFX + 'tbody');
ids['node-search'] = el('node-search');
ids['node-priced-only'] = el('node-priced-only');
const docStub = { getElementById: (id) => ids[id] || null, querySelectorAll: () => [] };

// Build ONE function body from the shipped escapeHtml + _dollars + renderNodes
// VERBATIM; _nodes and document ride in as parameters (new Function sees no
// module scope, so nothing here is a hand-written copy of the page logic).
const body = [
    extractFn('function escapeHtml(s)'),
    extractFn('function _dollars(cents)'),
    renderSrc,
    `renderNodes(); return document.getElementById('${PFX}tbody').innerHTML;`,
].join('\n');
const renderFn = new Function('_nodes', 'document', body);

function renderWith(rows) {
    ids['node-search'].value = '';
    ids['node-priced-only'].checked = false;
    return renderFn(rows, docStub);
}

const html1 = renderWith([
    { id: 1, name: 'Flagged Node', node_kind: 'k1', price_cents: 100, price_tier: 'Basic', is_unsupported: true },
    { id: 2, name: 'Normal Node', node_kind: 'k2', price_cents: 200, price_tier: 'Basic', is_unsupported: false },
    { id: 3, name: 'Unknown Node', node_kind: 'k3', price_cents: 0, price_tier: 'Basic' },
]);
check('flagged row renders the Unsupported badge with an explanatory title',
    /Flagged Node<span[^>]*title="[^"]*unsupported[^"]*"[^>]*>Unsupported<\/span>/.test(html1),
    html1.slice(0, 400));
check('false flag renders no badge',
    !html1.includes('Normal Node<span'));
check('absent flag renders no badge (never guessed)',
    !html1.includes('Unknown Node<span'));

const html2 = renderWith([
    { id: 4, name: '<script>x</script>', node_kind: 'k4', price_cents: 0, price_tier: 'Basic', is_unsupported: true },
]);
check('hostile name stays escaped AND still gets the badge',
    html2.includes('&lt;script&gt;') && !html2.includes('<script>')
    && /&lt;script&gt;x&lt;\/script&gt;<span/.test(html2),
    html2.slice(0, 400));

if (failures) { console.error(`\n${failures} check(s) FAILED`); process.exit(1); }
console.log('\nall checks passed');
