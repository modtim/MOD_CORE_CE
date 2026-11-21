'use strict';
// CE-NOPAID-4.4 - CORE/Dashboard/tests/test_ce_extract_no_paid_page_ids.check.js
//
// Runs against a CE EXTRACT Dashboard tree (harness: nodetest --tree <extract>
// Dashboard tests/<this file>; __dirname then resolves inside the extract).
// Three assertions:
//   (a) no paid page id is present in the extract Dashboard js/ files. The paid
//       id list is the checked-in CORE/API/paid_addon/dashboard/paid-pages.js PAID_PAGE_IDS
//       (authoritative; resolution order in the item-1 block below). It is never
//       derived from the tree under test, and it fails loudly when unresolved.
//       The `_CE_*PAGES` Set literals themselves are stripped before the scan: they
//       are the CE gate that renders the not-available panel, so carrying the ids
//       there is expected; a paid page id elsewhere in js/ is a leak.
//       The scrub row open-core/manifest.yaml:5354 keeps the `_CE_PAID_HUB_PAGES` gate
//       (it "replaces the whole compliance-center hub shell with the CE notice"), so
//       those ids stay in the extract's Set literals and are stripped before the scan.
//   (b) every relative `import ... from './x.js'` in the extract js/ files
//       resolves to an existing file.
//   (c) deep link to a paid id on CE shows the not-available panel: delegates to
//       the existing stub test test_navigate_to_paid_page.js (child process,
//       exit code 0 = panel rendered for paid ids on CE).
//
// Item 2 fix: a hit is a WHOLE string literal equal to the id or '#'+id, after comments
//   are stripped (findPaidIdRefs). Prose ('business day'), API paths ('/compliance/list')
//   and comments never count (CE-NOPAID-4.4 fix a4; self-test (a0), mutation-checked).
// Item 3 fix: on the SOURCE tree (paid registerPage rows still present in js/ui-manifest.js)
//   the file prints `# SKIP: not an extract tree` and exits 0, so `npm run test:checks`
//   (node --test tests/*.check.js) stays green on source; only an extract asserts.
//
// Env vars (documented):
//   CE_PAID_ID_SOURCE_ROOT - absolute root of the PRIVATE source tree. When set, the
//     paid-id list is read from <root>/CORE/API/paid_addon/dashboard/paid-pages.js if present,
//     else from <root>/CORE/Dashboard/js/{ui.js,ui-manifest.js}. Local runs only: the
//     harness nodetest container mounts /app + /API + /open-core, not the lane clone,
//     so under the harness this var is unset and the floor assert carries the guard.
//   CE_EXTRACT_TREE - absolute root of a CE extract tree (the dir that holds CORE/).
//     When set, the scan runs against <root>/CORE/Dashboard and the source-tree skip
//     is bypassed (use it to run this file outside the harness).
//
//   node --test CORE/Dashboard/tests/test_ce_extract_no_paid_page_ids.check.js

const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');
const assert = require('node:assert');
const { test } = require('node:test');

// --- CE-NOPAID-4.4 documented host-page allowlist (FIX-...-KEEP-HOST-PAGE-ALLOWLIST) ---
// 18 paid ids legitimately stay in the extract js: a CE host page routes to them
// through the ui.js alias/route maps or a retired-panel id (AUDIT-RUN-4-4 KEEP=18;
// DIAG-RECONCILE-54: real_leaks=0 over the 59 ids). Self-checking: every id below
// must appear as a KEY or VALUE in the SHIPPED ui.js alias/route maps or a `_CE_*`
// Set literal, so an id that stops being routed can never hide here. The extract
// tree IS the shipped text (build_public_repo applies the manifest scrub rows before
// shipping), so uiSrc below is exactly what CE ships.
const KEEP_HOST_PAGE = new Set([
    'billing',              // hub stub: dashboard hub hides it (ui.js HUB set + shell map key)
    'marketplace',          // CE host page (marketplace ships in CE)
    'security',             // CE host page (Security hub ships)
    'compliance-center',    // CE host page (Compliance hub ships; alias targets live here)
    'capture-devices',      // hub stub: hub hides it (System sub-tab hidden on CE)
    'admin-plans',          // hub stub: hub hides it (no plans on CE)
    'subscription',         // hub stub: hub hides it (plan/upgrade page paid-only)
    'gpu-pool',             // retired panel: _CE_RETIRED_ADMIN_PAGES + pinned -> gpu-kpi-avg
    'pricing',              // retired panel: pinned -> free-tier-row
    'access-reviews',       // retired panel + alias -> compliance-center hub tab
    'admin-scim',           // retired panel: pinned -> ce-retired-admin-scim
    'payment-settings',     // retired panel: pinned -> stripe-status-badge
    'release-approvals',    // alias -> compliance-center hub tab
    'lockbox-approval',     // alias -> compliance-center hub tab
    'auditor-access',       // alias -> compliance-center hub tab
    'parental-consent',     // alias -> users host page
    'security-lockdown',    // alias -> Security hub tab
    'outbound-connections', // alias -> Security hub tab
]);

// --- CE-NOPAID-4.4 hidden-by-design exemptions (FIX-TEST-...-EXEMPT-THE-27-HIDDEN-BY-DESIGN) ---
// Paid ids that the CE extract js still spells, but which CE never shows. Evidence: real
// staged CE extract on gpu3 (RUN-CE-NOPAID-4-4-P2: 31 of the 60 list ids present) + the
// DIAG-RECONCILE-54 classification. Each row is [allowed files, mechanism]. The exemption is
// PER FILE: a hit of the same id in any OTHER file still fails (a new leak spot is caught).
// Mechanisms: TAB = hub tab key hidden by a `_CE_*_TABS` Set in hub-compliance.js (tab module
// is manifest-denied and loaded via optionalPageModule); PAGE = ui.js `_CE_*_PAGES` Set member
// + the alias/route/title-map/dead-branch text next to it (the deep-link gate fires first and
// shows the "not available" panel); GATE = runtime `_ceEdition` gate in the shipped file;
// LABEL = product-names.js `paidOnly: true` row (product-labels.js never renders it on CE).
const HIDDEN_BY_DESIGN = {
    'admin-dsar-requests': [['ui.js', 'users.js'], 'PAGE ui.js _CE_COV4B_PAGES + GATE users.js:108 child tab hidden on CE'],
    'admin-sso': [['ui.js', 'users.js', 'product-names.js'], 'PAGE ui.js _CE_COV4B_PAGES; users.js dead fallback key into the scrubbed-empty IDENTITY_PAGES map (no paid page loads); LABEL paidOnly row'],
    'business': [['analytics.js', 'ui.js'], 'PAGE ui.js _CE_COV4B_PAGES + title map; analytics.js ?tab=business bookmark redirect (page denied, gate panel)'],
    'compliance': [['hub-compliance.js', 'ui.js', 'product-labels.js'], 'TAB hub-compliance.js _CE_R397_TABS; ui.js _CE_COMPLIANCE_PAGES; product-labels.js ROUTE_TITLE_KEYS title-key map only (document.title theme label; no navigation, no gate; key compliance-center is not paidOnly)'],
    'compliance-adversary': [['hub-compliance.js'], 'TAB _CE_COV4B_TABS'],
    'compliance-attestations': [['hub-compliance.js', 'ui-manifest.js'], 'TAB _CE_FORT46_TABS; registry row paid:true'],
    'compliance-calibration': [['hub-compliance.js'], 'TAB _CE_COV4B_TABS'],
    'compliance-capacity': [['hub-compliance.js'], 'TAB _CE_COV4B_TABS'],
    'compliance-hardening': [['hub-compliance.js', 'ui-manifest.js'], 'TAB _CE_FX159_TABS; registry row paid:true'],
    'compliance-obligations': [['hub-compliance.js', 'ui-manifest.js'], 'TAB _CE_FX159_TABS; registry row paid:true'],
    'compliance-operator-proposals': [['hub-compliance.js', 'ui.js'], 'TAB _CE_PAID_COMPLIANCE_TABS; ui.js _CE_PAID_COMPLIANCE_PAGES'],
    'compliance-policies': [['hub-compliance.js', 'ui-manifest.js'], 'TAB _CE_FORT54UI_TABS; registry row paid:true'],
    'compliance-requirements': [['hub-compliance.js', 'ui-manifest.js'], 'TAB _CE_FX159_TABS; registry row paid:true'],
    'consent-register': [['ui.js', 'users.js'], 'PAGE ui.js _CE_PAID_COMPLIANCE_PAGES; GATE users.js:105 child tab hidden on CE'],
    'data-export': [['hub-compliance.js'], 'TAB _CE_FX163_TABS'],
    'free-trials': [['ui.js', 'product-names.js'], 'PAGE ui.js _CE_* Set; LABEL paidOnly row'],
    'geographic-pricing': [['ui.js', 'product-names.js'], 'PAGE ui.js _CE_* Set; LABEL paidOnly row'],
    'key-inventory': [['hub-compliance.js', 'ui.js'], 'TAB _CE_PAID_COMPLIANCE_TABS; ui.js _CE_PAID_COMPLIANCE_PAGES'],
    'licence-review': [['hub-compliance.js', 'ui.js'], 'TAB _CE_PAID_COMPLIANCE_TABS; ui.js _CE_PAID_COMPLIANCE_PAGES'],
    'model-cache': [['analytics.js', 'ui.js'], 'GATE analytics.js sub-tab gated on !_ceEditionAn; ui.js retired-page Set'],
    'mpc-sessions': [['hub-compliance.js', 'ui.js'], 'TAB _CE_COV4B_TABS; ui.js _CE_COV4B_PAGES'],
    'operator-proposals': [['ui.js'], 'PAGE ui.js _CE_PAID_COMPLIANCE_PAGES + _CE_PAID_HUB_PAGES + alias/title map'],
    'output-scanners': [['hub-compliance.js'], 'TAB _CE_WSIP67D_TABS'],
    'payments-admin': [['ui.js', 'product-names.js'], 'PAGE ui.js _CE_COV4B_PAGES; LABEL paidOnly row'],
    'pending-approvals': [['ui.js', 'security-hub.js', 'approval-pending.js'], 'PAGE ui.js _CE_PAID_COMPLIANCE_PAGES; GATE security-hub.js:156-162 tab hidden on CE (approval-pending.js only builds a #hash link)'],
    'pricing-config': [['ui.js'], 'PAGE ui.js _CE_COV4B_PAGES; dead branch (page manifest-denied)'],
    'promo-codes': [['ui.js', 'product-names.js'], 'PAGE ui.js _CE_* Set; LABEL paidOnly row'],
    'seasonal-pricing': [['ui.js', 'product-names.js'], 'PAGE ui.js _CE_* Set; LABEL paidOnly row'],
    'stack-billing': [['ui.js'], 'PAGE ui.js _CE_COV4B_PAGES; title map + dead branch (page manifest-denied)'],
    'tenant-idp': [['ui.js', 'security-hub.js'], 'PAGE ui.js _CE_COV4B_PAGES; GATE security-hub.js:156 tab hidden on CE'],
    // paidOnly row: product-labels.js never renders it on CE (the check asserts the flag, see test (a))
    'drift-events': [['product-names.js'], 'LABEL paidOnly row'],
};

function ceSetBodies(src) {
    // body text of every `_CE_*` Set literal in src (bracket-balanced from the '[')
    const out = [];
    const rx = /const\s+_CE_[A-Z0-9_]+\s*=\s*new\s*Set\(\s*\[/g;
    let m;
    while ((m = rx.exec(src))) {
        let i = m.index + m[0].length, depth = 1;
        while (i < src.length && depth) {
            if (src[i] === '[') depth += 1;
            else if (src[i] === ']') depth -= 1;
            i += 1;
        }
        out.push(src.slice(m.index + m[0].length, i - 1));
    }
    return out;
}

function routedInUiJs(id, src, bodies) {
    // KEY or VALUE of an alias/route map entry, or a member of any `_CE_*` Set literal
    const esc = id.replace(/-/g, '\\-');
    if (new RegExp("'\\s*" + esc + "\\s*'\\s*:").test(src)) return true;
    if (new RegExp(":\\s*'" + esc + "'").test(src)) return true;
    for (const b of bodies) if (new RegExp("'\\s*" + esc + "\\s*'").test(b)) return true;
    return false;
}

const dashRoot = process.env.CE_EXTRACT_TREE
    ? path.join(process.env.CE_EXTRACT_TREE, 'CORE', 'Dashboard')
    : path.join(__dirname, '..');
const jsDir = path.join(dashRoot, 'js');
const uiPath = path.join(jsDir, 'ui.js');
const manifestPath = path.join(jsDir, 'ui-manifest.js');

function setBlocks(src) {
    // returns { ids: Set of ids inside the _CE_*PAGES Set literals, stripped: src with
    // those Set literals removed (the CE gate itself is not a leak) }
    const ids = new Set();
    const lines = src.split('\n');
    const keep = [];
    let i = 0;
    while (i < lines.length) {
        if (/^\s*const\s+_CE_[A-Z0-9_]*PAGES\s*=\s*new\s*Set\(\[\s*$/.test(lines[i])) {
            while (i < lines.length && !lines[i].trim().startsWith(']);')) {
                const s = lines[i].trim();
                if (!s.startsWith('//')) {
                    for (const m of s.matchAll(/'([^'\n]+)'/g)) {
                        if (/^[a-z0-9][a-z0-9-]*$/.test(m[1])) ids.add(m[1]);
                    }
                }
                keep.push('');
                i += 1;
            }
            keep.push('');
            continue;
        }
        keep.push(lines[i]);
        i += 1;
    }
    return { ids, stripped: keep.join('\n') };
}

// Strip // and /* */ comments from JS source. Quote-aware: a comment marker inside a
// ' " or ` literal is kept; a ' or " literal ends at the line end (JS forbids a raw newline).
function stripJsComments(src) {
    let out = '';
    let i = 0;
    let q = null;
    while (i < src.length) {
        const c = src[i];
        const d = src[i + 1];
        if (q) {
            out += c;
            if (c === '\\') { out += d === undefined ? '' : d; i += 2; continue; }
            if (c === q || (c === '\n' && q !== '`')) q = null;
            i += 1;
            continue;
        }
        if (c === '/' && d === '/') {
            while (i < src.length && src[i] !== '\n') i += 1;
            continue;
        }
        if (c === '/' && d === '*') {
            i += 2;
            while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) {
                if (src[i] === '\n') out += '\n';
                i += 1;
            }
            i += 2;
            out += ' ';
            continue;
        }
        if (c === "'" || c === '"' || c === '`') q = c;
        out += c;
        i += 1;
    }
    return out;
}

// Contents of every ' or " string literal in the comment-free source.
function jsStringLiterals(src) {
    const code = stripJsComments(src);
    const out = [];
    const rx = /'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"/g;
    let m;
    while ((m = rx.exec(code)) !== null) out.push(m[1] !== undefined ? m[1] : m[2]);
    return out;
}

// Hits for a paid page id = string literals that EQUAL the id or '#'+id (whole literal).
// Prose ('business day'), API paths ('/compliance/list') and comments never count.
const litCache = new Map();
function findPaidIdRefs(text, id) {
    if (!litCache.has(text)) litCache.set(text, jsStringLiterals(text));
    return litCache.get(text).filter((s) => s === id || s === '#' + id).length;
}

test('(a0) matcher ignores prose and API paths', () => {
    assert.strictEqual(findPaidIdRefs('// pricing-config note', 'pricing-config'), 0);
    assert.strictEqual(findPaidIdRefs("x = '/compliance/list'", 'compliance'), 0);
    assert.strictEqual(findPaidIdRefs("'business day'", 'business'), 0);
    assert.strictEqual(findPaidIdRefs("['admin-nodes']", 'admin-nodes'), 1);
});

function paidManifestIds(src) {
    const out = [];
    for (const m of src.matchAll(/register(?:Page|Alias)\(\{id: '([a-z0-9-]+)'[^}]*paid: true\}/g)) out.push(m[1]);
    return out;
}

// CE-NOPAID-4.1d: the paid rows moved to the denied paid file; the
// source-tree scan reads both so the paidRowsInTree extract detector stays 48 on paid.
const manifestPaidPath = path.join(jsDir, '..', '..', 'API', 'paid_addon', 'dashboard', 'ui-manifest-paid.js');
const manifestSrc = fs.readFileSync(manifestPath, 'utf8')
    + (fs.existsSync(manifestPaidPath) ? '\n' + fs.readFileSync(manifestPaidPath, 'utf8') : '');
const uiSrc = fs.readFileSync(uiPath, 'utf8');

// --- item 3: source tree => SKIP, exit 0 (keeps `npm run test:checks` green on source) ---
// Detect on the paid rows themselves (a comment mentioning `paid: true` must not count).
const paidRowsInTree = paidManifestIds(manifestSrc).length;
const isExtract = paidRowsInTree < 10;
// (a0) above runs everywhere; (a)/(a1)/(b)/(c) are test-level skips here (not process.exit,
// which would kill the node:test queue before (a0) runs).
const SKIP = !isExtract;
if (SKIP) {
    console.log('# SKIP: not an extract tree (js/ui-manifest.js still carries ' + paidRowsInTree +
        ' paid: true registerPage rows; run via nodetest --tree <extract>, or set CE_EXTRACT_TREE)');
}

// --- item 1: the paid-id list is NOT the tree-under-test manifest (scrub empties it) ---
// Resolution order (first existing file wins): CE_PAID_PAGES_JS (explicit file; also the
// mutation hook), <CE_PAID_ID_SOURCE_ROOT>/CORE/API/paid_addon/dashboard/paid-pages.js, then the
// repo found by walking up from __dirname (the dir holding open-core/manifest.yaml).
// CORE/API/paid_addon/ is absent from the CE extract by design. There is NO fallback to the tree
// under test's own _CE_* Sets: if nothing resolves, test (a) fails loudly.
// The list is authoritative: its PAID_PAGE_IDS array = source union (ui.js _CE_* Sets +
// `paid: true` rows) plus 11 hub-compliance TAB ids (paid surfaces, kept on purpose).
function findRepoRoot(start) {
    for (let d = start; ; d = path.dirname(d)) {
        if (fs.existsSync(path.join(d, 'open-core', 'manifest.yaml'))) return d;
        if (path.dirname(d) === d) return '';
    }
}
const sourceRoot = process.env.CE_PAID_ID_SOURCE_ROOT || '';
const repoRoot = sourceRoot || findRepoRoot(__dirname);
// CE-NOPAID-5.2 part 1 (owner D3): the add-on has ONE home, CORE/API/paid_addon/, so the
// repo-root candidate is gone. Under the harness nodetest container /app is only CORE/<dir>,
// and the run tree's CORE/API is mounted at /API - that is where the checked-in list lives
// there (the old /paid-addon mount is dead: the repo-root spelling no longer exists).
const paidPagesCandidates = [
    process.env.CE_PAID_PAGES_JS || '',
    repoRoot ? path.join(repoRoot, 'CORE', 'API', 'paid_addon', 'dashboard', 'paid-pages.js') : '',
    '/API/paid_addon/dashboard/paid-pages.js',
].filter(Boolean);
const paidPagesPath = paidPagesCandidates.find((p) => fs.existsSync(p)) || '';

let paidIds = new Set();
let listFrom = paidPagesPath || 'none resolved';
if (paidPagesPath) {
    const listSrc = fs.readFileSync(paidPagesPath, 'utf8');
    const block = /export const PAID_PAGE_IDS = \[([\s\S]*?)\];/.exec(listSrc);
    if (block) for (const m of block[1].matchAll(/'([a-z0-9][a-z0-9-]*)'/g)) paidIds.add(m[1]);
}

// Floor: the list must hold at least the 49 source-derived ids (HEAD: list 60, union 49).
const MIN_PAID_IDS = 49;

const jsFiles = fs.readdirSync(jsDir).filter((f) => f.endsWith('.js')).map((f) => path.join(jsDir, f));
const stripped = {};
for (const f of jsFiles) stripped[f] = setBlocks(fs.readFileSync(f, 'utf8')).stripped;

const UNAVAILABLE = 'paid id list unavailable: set CE_PAID_PAGES_JS, or run from a repo that has CORE/API/paid_addon/dashboard/paid-pages.js';

test('(a) extract js/ carries no paid page id', { skip: SKIP }, () => {
    if (!paidPagesPath) assert.fail(UNAVAILABLE);
    if (!(paidIds.size >= MIN_PAID_IDS)) assert.fail('paid-id list too small (vacuous): ' + paidIds.size + ' < ' + MIN_PAID_IDS);
    // self-checking allowlist: it stays small and every id in it must still be routed
    if (!(KEEP_HOST_PAGE.size <= 18)) assert.fail('allowlist too large: ' + KEEP_HOST_PAGE.size + ' > 18');
    const setBodies = ceSetBodies(uiSrc);
    for (const id of KEEP_HOST_PAGE) {
        if (!routedInUiJs(id, uiSrc, setBodies)) {
            assert.fail('allowlisted id no longer routed in shipped ui.js alias/route maps or _CE_* Sets: ' + id);
        }
    }
    const failing = [];
    for (const id of paidIds) {
        if (KEEP_HOST_PAGE.has(id)) continue; // documented host/retired/alias ids (see block above)
        const ex = HIDDEN_BY_DESIGN[id];
        if (ex) {
            if (!ex[1]) assert.fail('hidden-by-design exemption needs a mechanism reason: ' + id);
            if (!paidIds.has(id)) assert.fail('exempted id is not in the paid list: ' + id);
        }
        const allowedFiles = ex ? ex[0] : [];
        // product-names.js is exempt only for rows that carry `paidOnly: true`
        // (product-labels.js never renders such a row on CE): a row without the flag fails.
        if (allowedFiles.includes('product-names.js')) {
            const pn = jsFiles.find((f) => path.basename(f) === 'product-names.js');
            const rowRx = new RegExp("^\\s*'" + id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + "'\\s*:.*$", 'gm');
            const rows = pn ? (fs.readFileSync(pn, 'utf8').match(rowRx) || []) : [];
            if (rows.some((r) => !/paidOnly\s*:\s*true/.test(r))) {
                failing.push(id + ' @product-names.js (row lacks paidOnly: true)');
                continue;
            }
        }
        for (const f of jsFiles) {
            if (allowedFiles.includes(path.basename(f))) continue;
            if (findPaidIdRefs(stripped[f], id) > 0) { failing.push(id + ' @' + path.basename(f)); break; }
        }
    }
    console.log('paid ids checked = ' + paidIds.size + ' (from ' + listFrom + '); ids present in extract js = ' + failing.length);
    if (failing.length) console.log('first 10 failing: ' + failing.slice(0, 10).join(', '));
    console.log('all failing: ' + failing.join(', '));
    assert.strictEqual(failing.length, 0, failing.length + ' paid page ids present in extract js');
});

// Sanity (a1): the matcher must still see known real leaks, so (a) cannot pass vacuously.
// (a2) SUBSET, not skipped on source: every paid id the tree under test declares (ui.js
// _CE_* Sets + ui-manifest.js `paid: true` rows) must be in the list. The list may carry
// extras (the hub-compliance TAB ids); they are printed as info, never failed. Mutation
// proof: CE_PAID_PAGES_JS=<copy of the list with one union id removed> turns this red.
test('(a2) every paid id in the tree under test is in the checked-in list (subset)', () => {
    if (!paidPagesPath) assert.fail(UNAVAILABLE);
    const union = new Set([...setBlocks(uiSrc).ids, ...paidManifestIds(manifestSrc)]);
    const missing = [...union].filter((id) => !paidIds.has(id));
    const extras = [...paidIds].filter((id) => !union.has(id));
    console.log('# info (a2): union=' + union.size + ' list=' + paidIds.size + ' list-only=' + extras.length +
        ' (' + extras.join(',') + ') list=' + listFrom);
    assert.deepStrictEqual(missing, [], 'paid ids in the tree missing from the list: ' + missing.join(','));
});

test('(a1) matcher still reports known leaks on the extract', { skip: SKIP }, () => {
    // Premise fixed (CE-NOPAID-4.4): admin-nodes is scrubbed from the extract, so the sanity
    // ids are ones the extract still spells by design (hub stub, hidden tab, dead branch).
    for (const id of ['billing', 'key-inventory', 'pricing-config']) {
        const hits = jsFiles.reduce((n, f) => n + findPaidIdRefs(stripped[f], id), 0);
        assert.ok(hits >= 1, 'matcher reports no hit for known leak ' + id);
    }
});

test('(b) every relative js import resolves inside the extract', { skip: SKIP }, () => {
    const broken = [];
    const rx = /^\s*(?:import|export)\s+[^'"]*?from\s+['"](\.[^'"]+)['"]/gm;
    const rxSide = /^\s*import\s+['"](\.[^'"]+)['"]/gm;
    for (const f of jsFiles) {
        const src = fs.readFileSync(f, 'utf8');
        for (const re of [rx, rxSide]) {
            for (const m of src.matchAll(re)) {
                const target = path.resolve(path.dirname(f), m[1]);
                if (!fs.existsSync(target)) broken.push(path.basename(f) + ' -> ' + m[1]);
            }
        }
    }
    console.log('relative imports broken = ' + broken.length);
    if (broken.length) console.log('first 10 broken: ' + broken.slice(0, 10).join(', '));
    assert.strictEqual(broken.length, 0, broken.length + ' unresolved relative imports');
});

test('(c) deep link to a paid id shows the not-available panel on CE', { skip: SKIP }, () => {
    const stub = path.join(dashRoot, 'tests', 'test_navigate_to_paid_page.js');
    if (!fs.existsSync(stub)) assert.fail('stub test missing: test_navigate_to_paid_page.js');
    const r = spawnSync(process.execPath, [stub], { encoding: 'utf8', timeout: 120000 });
    console.log('navigate-to-paid stub rc = ' + r.status);
    if (r.status !== 0) console.log((r.stdout || '').slice(-400) + (r.stderr || '').slice(-400));
    assert.strictEqual(r.status, 0, 'paid deep link did not show the not-available panel (rc=' + r.status + ')');
});
