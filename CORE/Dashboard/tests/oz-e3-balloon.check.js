// OZ-E3-BALLOON — static guard for the public-cloud burst surface (the tenant's
// OWN cloud-account page: #main-tab-burst-accounts + its panel h2).
// Owner rule: this surface is named Balloon Cloud (NAME-MAP row 27, product
// names.js key `burst-accounts`) and SHIPS IN CE; the paid MOD-hosted burst
// surface (`burst`, Rainbow MOD Cloud) is a different product and stays
// hidden/denied in CE, so it is never themed on this page.
//
// Same technique as oz-d3-h1-hj.check.js / ce-cloud-tab-visible.check.js: the
// shipped code is read from source and run against a stub DOM, so the
// assertions test what ships, not a re-typed copy.
//
//   node CORE/Dashboard/tests/oz-e3-balloon.check.js
'use strict';
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'pages', 'analytics.html'), 'utf8');
const js = fs.readFileSync(path.join(__dirname, '..', 'js', 'burst-accounts.js'), 'utf8');
const namesSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'product-names.js'), 'utf8');

let failures = 0;
function check(name, cond) { if (cond) console.log(`ok   ${name}`); else { failures++; console.error(`FAIL ${name}`); } }

const TODAY_BUTTON = '<button id="main-tab-burst-accounts" class="main-tab flex-1 px-6 py-4 text-sm font-medium border-b-2 border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 hidden">';
const TODAY_H2 = '<h2 class="text-xl font-semibold text-gray-800">Cloud</h2>';

// --- 1) ids, hrefs, routes untouched; only a data-theme-key added -----------
check('button id main-tab-burst-accounts present exactly once',
    (html.match(/id="main-tab-burst-accounts"/g) || []).length === 1);
check('panel id main-panel-burst-accounts present exactly once',
    (html.match(/id="main-panel-burst-accounts"/g) || []).length === 1);
const buttonTag = /<button id="main-tab-burst-accounts"[^>]*>/.exec(html);
check('button tag == today\'s tag + data-theme-key (one attribute, nothing else)',
    buttonTag && buttonTag[0].replace(' data-theme-key="burst-accounts"', '') === TODAY_BUTTON);
check('button carries data-theme-key="burst-accounts"',
    buttonTag && buttonTag[0].includes('data-theme-key="burst-accounts"'));

const H2_RE = /<h2 class="text-xl font-semibold text-gray-800" data-theme-key="burst-accounts">([^<]*)<\/h2>/;
const h2Tag = H2_RE.exec(html);
check('panel h2 carries data-theme-key="burst-accounts"', !!h2Tag);
check('panel h2 minus the attribute == today\'s h2 byte for byte (OFF renders today)',
    !!h2Tag && h2Tag[0].replace(' data-theme-key="burst-accounts"', '') === TODAY_H2 &&
    !!h2Tag && h2Tag[1] === 'Cloud');

// --- 3) map rows: burst-accounts themed (CE), burst paidOnly ---------------
const entry = namesSrc.match(/'burst-accounts':\s*\{([^}]*)\}/);
check("product-names.js has a 'burst-accounts' entry", !!entry);
const label = entry && (entry[1].match(/label:\s*'([^']*)'/) || [])[1];
check('map label for burst-accounts is "Balloon Cloud"', label === 'Balloon Cloud - Burst Accounts');
check('burst-accounts entry is NOT paidOnly (ships in CE)', entry && !/paidOnly/.test(entry[1]));

// --- 4) the shipped runtime pass, extracted verbatim and run on a stub DOM --
function extractFrom(src, name) {
    const m = src.match(new RegExp(`function ${name}\\([\\s\\S]*?\\n}`));
    if (!m) throw new Error(`could not extract ${name}`);
    return m[0];
}
const namesPlain = namesSrc.replace(/^export /gm, '');
const mapDecl = namesPlain.match(/const PRODUCT_NAMES = \{[\s\S]*?\n\};/);
if (!mapDecl) throw new Error('PRODUCT_NAMES declaration not found');
const themeFns = ['themeEnabled', 'themeLabel'].map((n) => extractFrom(namesPlain, n)).join('\n');
const helperNames = ['_baApplySurfaceLabel', '_baLastTextNode', '_baThemeSurfaceLabels', '_baThemeSurfacePassAsync'];
const helperSrc = {};
helperNames.forEach((n) => { helperSrc[n] = extractFrom(js, n); });
const allHelpers = helperNames.map((n) => helperSrc[n]).join('\n');
check('the pass resolves the map module at RUN TIME (dynamic import)',
    /await import\('\.\/product-names\.js'\)/.test(js));
check('no new STATIC import of the map module (vm-sandbox tests strip static imports only)',
    !/^import .*product-names\.js;/m.test(js));
check('the pass is invoked from the panel wiring (every System sub-tab activation)',
    /_baThemeSurfaceLabels\(\);/.test(js));
check('the pass never rejects (a missing module or stub DOM keeps today\'s literals)',
    /_baThemeSurfacePassAsync\(\)\.catch\(\(\) => \{\}\);/.test(js));
check('the theme pass writes no HTML (textContent only; provider strings never become markup)',
    !/innerHTML|insertAdjacent|outerHTML/.test(allHelpers));
const codeOnly = js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
check('no "Balloon Cloud" string literal in shipped burst-accounts.js code',
    !/['"`]Balloon Cloud['"`]/.test(codeOnly));

function buildStub() {
    const mkText = (s) => ({ nodeType: 3, textContent: s });
    const mkSvg = (txt) => ({ nodeType: 1, tagName: 'svg', childNodes: [mkText(txt)] });
    const button = { nodeType: 1, dataset: { themeKey: 'burst-accounts' }, childNodes: [mkSvg('\n                    '), mkText('\n                    Cloud\n                ')] };
    const panelH2 = { nodeType: 1, dataset: { themeKey: 'burst-accounts' }, childNodes: [mkText('Cloud')] };
    const other = { nodeType: 1, dataset: { themeKey: 'no-such-key' }, childNodes: [mkText('Settings')] };
    const els = [button, panelH2, other];
    return { doc: { querySelectorAll: () => els }, button, panelH2, other };
}

function runPass(flag, stubIn) {
    if (flag === 'off') process.env.MOD_THEME_NAMES = 'off';
    else delete process.env.MOD_THEME_NAMES;
    const stub = stubIn || buildStub();
    const run = new Function('document',
        mapDecl[0] + '\n' + themeFns + '\n' + helperSrc._baApplySurfaceLabel +
        '\n' + helperSrc._baLastTextNode +
        '\nconst els = document.querySelectorAll("[data-theme-key]");' +
        '\nfor (const el of els) _baApplySurfaceLabel(el, PRODUCT_NAMES, themeLabel);');
    run(stub.doc);
    return {
        button: stub.button.childNodes[stub.button.childNodes.length - 1].textContent.trim(),
        iconKept: stub.button.childNodes[0].tagName === 'svg',
        h2: stub.panelH2.childNodes[0].textContent,
        other: stub.other.childNodes[0].textContent,
    };
}

const off = runPass('off');
const on = runPass('on');
check('MOD_THEME_NAMES=off: panel h2 renders today\'s "Cloud" (off_diff=0)', off.h2 === 'Cloud');
check('MOD_THEME_NAMES=off: tab button renders today\'s "Cloud"', off.button === 'Cloud');
check('MOD_THEME_NAMES on (default): panel h2 renders map label "Balloon Cloud - Burst Accounts"', on.h2 === 'Balloon Cloud - Burst Accounts');
check('MOD_THEME_NAMES on: tab button renders map label "Balloon Cloud - Burst Accounts"', on.button === 'Balloon Cloud - Burst Accounts');
check('unknown theme key falls back to today\'s literal (never blank)', on.other === 'Settings' && off.other === 'Settings');
check('tab button icon child survives the theme pass (only the text node changes)', on.iconKept && off.iconKept);
const onTwice = runPass('on');
check('theme pass is idempotent on re-render (tab re-activation)', onTwice.h2 === on.h2 && onTwice.button === on.button);
// Flag OFF straight through the real helper chain (real map + real themeLabel).
const realLabel = new Function('key', 'today',
    mapDecl[0] + '\n' + themeFns + '\nreturn themeLabel(key, today);');
process.env.MOD_THEME_NAMES = 'off';
let offLiteral = null;
try { offLiteral = realLabel('burst-accounts', 'Cloud'); } catch (e) { offLiteral = null; }
delete process.env.MOD_THEME_NAMES;
check('themeLabel with MOD_THEME_NAMES=off returns today\'s literal byte for byte',
    offLiteral === 'Cloud');
let unknownLiteral = null;
try { unknownLiteral = realLabel('no-such-key', 'Cloud'); } catch (e) { unknownLiteral = null; }
check('themeLabel of an unknown key falls back to the literal (never blank)',
    unknownLiteral === 'Cloud');
let themedLabel = null;
try { themedLabel = realLabel('burst-accounts', 'Cloud'); } catch (e) { themedLabel = null; }
check('themeLabel with the flag ON (default) returns the map label',
    themedLabel === 'Balloon Cloud - Burst Accounts');

if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
console.log('oz-e3-balloon: all checks passed');
