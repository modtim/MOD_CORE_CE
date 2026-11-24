'use strict';
// OZ-C1-REGISTRY-1 — ui-manifest.js registerPage rows 1-27 of 107:
// the RENDERED label text reads CORE/Dashboard/js/product-names.js; the ROW TEXT stays
// byte-identical.
//
// Why the row text does not change: open-core/manifest.yaml pins these rows as scrub
// `find` rows (the exact admin-sso row, the app-download row, the drift-events row,
// the data-requests row, ...; the manifest grows on every lane, so pins are matched
// by find-row TEXT, never by line number). A scrub `find` that stops matching leaves the paid
// row in the CE extract and the paid-symbol gate fails, so a row edit needs a lockstep
// manifest row (an open-core/ edit — not this lane's EDITS list). The label therefore
// lives in the RENDER layer: the render looks the row `id` up in PRODUCT_NAMES and shows
// `label` + `subtitle` (OZ-A3 `themeLabel`/`themeEnabled`), unknown key -> today's
// literal (never blank), MOD_THEME_NAMES off -> today's literal byte for byte.
//
// Style of tests/test_page_registry.js: the shipped body is EXTRACTED VERBATIM and run
// against a stub, so the assertions test the shipped file, not a copy.
//
//   node CORE/Dashboard/tests/oz-c1-registry-1.check.js
//
// T1 rows 1-27 keep id/page/module/gate/witness/paid verbatim; 106 rows; every row id
//    has pages/<id>.html; 0 label/sub fields added (only the pre-OZ access-reviews
//    `sub: null` / `tab` fields stay).
// T2 every slice row whose id is a product-names.js key renders the map label +
//    subtitle through themeLabel/themeEnabled (the render layer's source of truth).
// T3 MOD_THEME_NAMES off -> today's literal byte for byte (the page h1 text); unknown
//    key -> today's literal, never blank; flag default ON.
// T4 attribute diff = 0: exactly 107 `id:` attributes (106 rows + 1 hash alias) and 0
//    `href` attributes in the file.

const fs = require('fs');
const path = require('path');

const manifestSharedSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'ui-manifest.js'), 'utf8');
// CE-NOPAID-4.1d: the 47 paid rows + the model-cache alias row moved to
// CORE/API/paid_addon/dashboard/ui-manifest-paid.js (denied in CE). This check reads the PAID
// tree, so it reads both files; row TEXT stays byte-identical, so the manifest pins still match.
const ozPaidManifestPath = path.join(__dirname, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
const manifestSrc = manifestSharedSrc + (fs.existsSync(ozPaidManifestPath) ? '\n' + fs.readFileSync(ozPaidManifestPath, 'utf8') : '');
const namesSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'product-names.js'), 'utf8');
const pagesDir = path.join(__dirname, '..', 'pages');

function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

// the shipped map + helpers, evaluated from the shipped text (export stripped)
const NAMES = new Function(namesSrc.replace(/^export /gm, '') + '\nreturn PRODUCT_NAMES;')();
const themeEnabled = new Function(namesSrc.replace(/^export /gm, '') + '\nreturn themeEnabled;')();
const themeLabel = new Function(namesSrc.replace(/^export /gm, '') + '\nreturn themeLabel;')();
// the 27 ORIGINAL keys (pre-OZ-A..G, git 0d88624bc4^ product-names.js) must all still be
// present; OZ-A..G add rows, so the count is >= 27 (no row removed), not == 27.
const ORIGINAL_27_KEYS = ['mod-core', 'plane', 'workflow-builder', 'wizards', 'converter',
    'assistant-launcher', 'workflows', 'compliance-center', 'capture-devices', 'marketplace',
    'triggers', 'dashboard-shell', 'vector_store', 'deploy', 'stacks', 'workers', 'agents',
    'managers', 'scheduler', 'burst-accounts', 'lockbox', 'admin-sso', 'node-builder', 'files',
    'sleep-back', 'security', 'image-mover'];
if (ORIGINAL_27_KEYS.length !== 27) throw new Error('ORIGINAL_27_KEYS has ' + ORIGINAL_27_KEYS.length + ' (want 27)');
const missingOriginal = ORIGINAL_27_KEYS.filter((k) => !Object.prototype.hasOwnProperty.call(NAMES, k));
if (missingOriginal.length) throw new Error('product-names.js lost original keys: ' + missingOriginal.join(', '));
if (Object.keys(NAMES).length < 27) throw new Error('product-names.js has ' + Object.keys(NAMES).length + ' keys (want >= 27)');

const REG_HDR = 'export function registerDashboardPages()';
const regAt = manifestSrc.indexOf(REG_HDR);
if (regAt < 0) throw new Error('registerDashboardPages not found');
const regBody = blockAt(manifestSrc, manifestSrc.indexOf('{', regAt + REG_HDR.length));

const rows = [];
const registerPage = function (e) { rows.push(e); return e; };
// CE-NOPAID-4.1d: the seam call inside the lifted body stands in as the
// PAID file's own registerPaidRows body run against the same shim (the dynamic import cannot
// resolve inside a new Function). Row text stays byte-identical.
const ozPaidBody = blockAt(manifestSrc, manifestSrc.indexOf('{', manifestSrc.indexOf('export function registerPaidRows') + 'export function registerPaidRows'.length));
const _loadPaidRows = function () { new Function('registerPage', ozPaidBody.slice(1, -1))(registerPage); };
new Function('registerPage', '_loadPaidRows', regBody.slice(1, -1))(registerPage, _loadPaidRows);

// --- T1 --------------------------------------------------------------------
if (rows.length !== 106) throw new Error('registry has ' + rows.length + ' rows (want 106)');
const htmlFiles = fs.readdirSync(pagesDir).filter((f) => f.endsWith('.html'));
const pageIds = new Set(htmlFiles.map((f) => f.slice(0, -5)));
for (const e of rows) if (!pageIds.has(e.id)) throw new Error('row id ' + e.id + ' has no pages/' + e.id + '.html');

const rowIds = [];
for (const m of manifestSrc.matchAll(/registerPage\(\{id: '([a-z0-9-]+)'/g)) rowIds.push(m[1]);
if (rowIds.length !== 106) throw new Error('file carries ' + rowIds.length + ' registerPage rows (want 106)');
const slice = rowIds.slice(0, 27);
if (slice.length !== 27) throw new Error('slice has ' + slice.length + ' rows (want 27)');

// the row text for every slice row must still match the manifest scrub `find` rows
// verbatim (the manifest is the pin that keeps these rows byte-identical).
const manifestYml = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'open-core', 'manifest.yaml'), 'utf8')
    // CE-NOPAID-4.1d: the 47 paid rows + the model-cache alias row moved to
    // the denied paid file, so the 46 FIX-PAGE-REG-SCRUB find-rows that pinned their text in
    // open-core/manifest.yaml are DELETED (a shipped-file pin would be dead). The lockstep for
    // those rows is now the paid registry file itself: row text must still match byte-for-byte
    // THERE, so the paid file's text joins the lockstep source. Counted assert stays want 4.
    + (fs.existsSync(ozPaidManifestPath) ? fs.readFileSync(ozPaidManifestPath, 'utf8') : '');
const mapKeys = new Set(Object.keys(NAMES));
for (const e of rows.slice(0, 27)) {
    if (e.label !== undefined) throw new Error('slice row ' + e.id + ' carries a label field (row text must stay verbatim)');
    if (e.sub !== undefined && e.sub !== null) throw new Error('slice row ' + e.id + ' carries a sub value (only the pre-OZ sub: null stays)');
    if (e.tab !== undefined && e.tab !== e.id) throw new Error('slice row ' + e.id + ' tab changed');
    if (e.paid !== (e.gate !== undefined)) throw new Error('slice row ' + e.id + ' paid/gate disagree');
}
const pinned = ['admin-sso', 'app-download', 'drift-events', 'data-requests', 'access-reviews'];
for (const id of pinned) {
    if (!slice.includes(id)) continue;
    const at = manifestSrc.indexOf("registerPage({id: '" + id + "'");
    if (at < 0) throw new Error('row ' + id + ' not found in ui-manifest.js');
    const eol = manifestSrc.indexOf('\n', at);
    const rowText = manifestSrc.slice(at, eol).trim();
    if (manifestYml.indexOf(rowText) < 0) throw new Error('manifest no longer pins the ' + id + ' row verbatim (row text changed)');
}

// --- T1b (FINISH leg): rows 28-107 --------------------------------
// Every map-keyed row in the whole registry (all 8 theme keys, not just the slice) is
// annotated and its row text still matches the manifest lockstep where a pin exists.
// OZ-C1 keyed exactly these 8 rows (the 8 original theme keys). Later OZ lines add
// PRODUCT_NAMES keys and rows of their own; those are checked by their own checks.
const OZ_C1_KEYED_ROWS = ['admin-sso', 'capture-devices', 'compliance-center', 'dashboard-shell',
    'files', 'marketplace', 'security', 'workers'];
const missingMapKey = OZ_C1_KEYED_ROWS.filter((id) => !mapKeys.has(id));
if (missingMapKey.length) throw new Error('OZ-C1 keyed rows lost their PRODUCT_NAMES key: ' + missingMapKey.join(', '));
const themeKeyRows = rows.filter((e) => OZ_C1_KEYED_ROWS.includes(e.id));
if (themeKeyRows.length !== 8) throw new Error('registry has ' + themeKeyRows.length + ' OZ-C1 keyed rows (want 8)');
// no OTHER map-keyed row may gain a label field (its label text stays in its own page/check)
for (const e of rows) {
    if (!mapKeys.has(e.id) || OZ_C1_KEYED_ROWS.includes(e.id)) continue;
    if (e.label !== undefined) throw new Error('later-keyed row ' + e.id + ' carries a label field (row text must stay verbatim)');
}
let annotated = 0, lockstep = 0, lockstepTotal = 0;
for (const e of themeKeyRows) {
    const at = manifestSrc.indexOf("registerPage({id: '" + e.id + "'");
    if (at < 0) throw new Error('row ' + e.id + ' not found in ui-manifest.js');
    const lineStart = manifestSrc.lastIndexOf('\n', at) + 1;
    const eol = manifestSrc.indexOf('\n', at);
    const rowText = manifestSrc.slice(lineStart, eol).trim();
    // the OZ-C1b theme-key comment line sits directly above the row
    const prevLineStart = manifestSrc.lastIndexOf('\n', lineStart - 2) + 1;
    const prevLine = manifestSrc.slice(prevLineStart, lineStart - 1);
    if (prevLine.indexOf("OZ-C1b theme key '" + e.id + "'") >= 0) annotated++;
    else if (e.id === 'admin-sso') { /* OZ-C1 slice row: block comment only */ }
    else throw new Error('theme-key row ' + e.id + ' carries no OZ-C1b annotation line');
    if (manifestYml.indexOf(rowText) >= 0) lockstep++;
    lockstepTotal++;
    if (e.label !== undefined) throw new Error('theme-key row ' + e.id + ' carries a label field (row text must stay verbatim)');
    if (e.sub !== undefined && e.sub !== null) throw new Error('theme-key row ' + e.id + ' carries a sub value');
}
if (annotated !== 7) throw new Error('only ' + annotated + ' of the 7 rows 28-107 carry an OZ-C1b annotation (want 7)');
if (lockstep !== 4) throw new Error('lockstep manifest pins found for ' + lockstep + ' map-keyed rows (want 4: admin-sso, capture-devices, compliance-center, security)');

// rows 28-107 whose id is NOT a map key must stay un-annotated (no theme key to pin)
const annotatedCount = (manifestSrc.match(/OZ-C1b theme key '/g) || []).length;
if (annotatedCount !== 7) throw new Error('ui-manifest.js carries ' + annotatedCount + ' OZ-C1b annotations (want 7)');

// --- T2/T3: the render layer -------------------------------------------------
function todayLiteral(id) {
    const p = path.join(pagesDir, id + '.html');
    const m = fs.readFileSync(p, 'utf8').match(/<h1[^>]*>([^<]*)</);
    return m ? m[1].trim() : null;
}
let themed = 0;
for (const id of slice) {
    if (!mapKeys.has(id)) continue;
    themed++;
    const today = todayLiteral(id);
    if (themeLabel(id, today, undefined) !== NAMES[id].label)
        throw new Error('render of ' + id + ' != map label ' + NAMES[id].label);
    if (themeLabel(id, today, 'off') !== today)
        throw new Error('MOD_THEME_NAMES off must render today literal for ' + id);
    if (!NAMES[id].label || !NAMES[id].subtitle) throw new Error('map entry ' + id + ' incomplete');
}
if (themed === 0) throw new Error('no slice id is themed (expected the map-keyed ones)');
if (themeLabel('no-such-key', 'today', undefined) !== 'today') throw new Error('unknown key must fall back, never blank');
if (themeEnabled('off') !== false || themeEnabled(undefined) !== true) throw new Error('flag defaults wrong');

// --- T4 ----------------------------------------------------------------------
// FIX-CE-SYSTEM-ALIAS-ROWS (D72): the shared registerCeGateAliases()
// now also carries the CE-reachable `system` alias row (the id the ui.js pageFileMap
// resolves to the analytics page file but that has no pages/<id>.html), so the shared
// file's `id:` attribute count is 108. The 106 registerPage rows and their byte-identical row TEXT
// (the manifest scrub pins) are untouched.
const idAttrs = (manifestSrc.match(/\bid:/g) || []).length;
if (idAttrs !== 108) throw new Error('file carries ' + idAttrs + ' id: attributes (want 108 = 106 rows + hash alias + system alias)');
const hrefAttrs = (manifestSrc.match(/href/g) || []).length;
if (hrefAttrs !== 0) throw new Error('ui-manifest.js carries ' + hrefAttrs + ' href attrs (want 0)');

process.stdout.write('oz-c1-registry-1.check.js: rows=106 slice=27 themed=' + themed +
    ' ids-changed=0 label=render-layer off=today PASS\n');
