'use strict';
// BUG.B47 (owner): pressing browser refresh makes the Dashboard "restart"
// several times before it loads.
//
// Boot path traced statically (file:line at 2026-10-08):
//   index.html:10   <script src="js/network-bootstrap.js">   (runs first)
//   index.html:58   <script src="js/main.js" type="module" defer>
//   main.js:8       DOMContentLoaded -> fetch the page-shell list -> initializeApp()
//   auth.js:472     checkApiOnboarding() -> /me
//   auth.js:425     showPage(#dashboard)             <- render #1
//   auth.js:432     navigateToPage(hash)             <- render #2 (page mount)
//   ui.js:2116      popstate -> navigateToPage(hash) <- render #3 (OAuth return)
//
// Distinct reload / re-render triggers in ONE refresh:
//   T1 network-bootstrap.js:39  location.reload() — HARD reload, up to MAX_RELOADS
//      times. MAX_RELOADS was 2 => THREE full boots per refresh = the symptom.
//   T2 network-bootstrap.js:51  capture-phase 'error' listener on any <script src>
//      failure (a 404/aborted module in the cascade) -> feeds T1.
//   T3 network-bootstrap.js:60  'unhandledrejection' listener on a dynamic-import
//      failure -> feeds T1.
//   T4 ui.js:2116  popstate re-mount (fires on the OAuth return and on every
//      location.hash write).
//   T5 auth.js:432 + ui.js:2091  second navigateToPage() after showPage().
//   T6 ui.js:596/638/744/749/1033  bookmark branches write location.hash +
//      location.search -> synchronous navigation -> popstate (T4).
//
// Only T1 visibly restarts the app, so the fix bounds it: single-boot guard in
// main.js published on window, MAX_RELOADS 2 -> 1, booted check in triggerReload().
// auth.js:402 already cleans the ?code=&state= fragment with history.replaceState,
// which performs no history traversal and therefore never fires popstate.
//
//   node CORE/Dashboard/tests/bug-b47-single-boot.check.js

process.on('unhandledRejection', () => {});

const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const mainJs = read('js/main.js');
const bootJs = read('js/network-bootstrap.js');
const authJs = read('js/auth.js');
const uiJs = read('js/ui.js');

// --- 1) single-boot guard: present, first, and effective ---------------------
assert.match(
    mainJs,
    /document\.addEventListener\("DOMContentLoaded",\s*async function \(\)\s*\{[\s\S]{0,700}?if \(window\.__modBooted\) return;\s*\n\s*window\.__modBooted = true;/,
    'main.js must guard DOMContentLoaded with window.__modBooted',
);
const guardIdx = mainJs.indexOf('window.__modBooted = true;');
const firstFetchIdx = mainJs.indexOf("fetch('/pages/login.html')");
assert.notEqual(guardIdx, -1, 'guard must exist');
assert.ok(firstFetchIdx >= 0, 'main.js must fetch /pages/login.html (the boot list must be present)');
assert.ok(firstFetchIdx > guardIdx, 'guard must run before any boot work');

// --- 2) no reload survives in the refresh path ------------------------------
const reloadFiles = [];
for (const [name, src] of [['js/main.js', mainJs], ['js/auth.js', authJs], ['js/ui.js', uiJs]]) {
    if (/location\.reload\(\)/.test(src)) reloadFiles.push(name);
}
assert.deepEqual(reloadFiles, [], 'main/auth/ui must never reload — refresh must not restart the app');
assert.equal((bootJs.match(/location\.reload\(\)/g) || []).length, 1,
    'exactly one reload site in the whole boot cascade (the bounded recovery reload)');
assert.match(bootJs, /if \(window\.__modBooted\) return;/,
    'triggerReload() must refuse to reload once the app has booted');
assert.match(bootJs, /var MAX_RELOADS = 1;/,
    'reload budget must be 1 (was 2 => three boots per refresh)');
assert.equal((bootJs.match(/MAX_RELOADS = \d/g) || []).length, 1, 'MAX_RELOADS declared once');

// --- 3) auth fragment cleanup must not re-trigger the router ----------------
assert.match(
    authJs,
    /window\.history\.replaceState\(\{\}, document\.title, window\.location\.pathname \+ window\.location\.hash\);/,
    'auth.js must strip ?code=&state= with history.replaceState (no popstate)',
);
const cleanupIdx = authJs.indexOf(
    'window.history.replaceState({}, document.title, window.location.pathname + window.location.hash);');
assert.ok(cleanupIdx > authJs.indexOf('startTokenRefreshInterval();'),
    'fragment cleanup runs after the token exchange, inside exchangeCodeForToken');
assert.equal((authJs.match(/location\.hash\s*=[^=]/g) || []).length, 0,
    'auth.js must not write location.hash (that fires popstate -> re-mount)');

// --- 4) the router is mounted exactly once ---------------------------------
assert.match(uiJs, /window\.addEventListener\("popstate", \(\) => navigateToPage\(window\.location\.hash\)\);/,
    'popstate router stays (back/forward support)');
assert.equal((uiJs.match(/addEventListener\("popstate"/g) || []).length, 1,
    'exactly one popstate listener — no duplicate router re-init');

// --- 5) simulation: run the REAL bootstrap + the REAL main.js handler -------
// Two DOMContentLoaded dispatches (refresh + late re-dispatch) and one late
// <script> error after boot. Expect: one boot (exactly one fetch per page shell
// in main.js's boot list), zero reloads.
let fetches = 0;
let reloads = 0;
const docHandlers = [];
const winHandlers = [];

const sandbox = {
    console: { warn() {}, log() {}, error() {} },
    fetch: () => { fetches++; return { ok: true, text: () => '', then: (f) => f({ ok: true, text: () => '' }) }; },
    document: {
        title: 'MOD Dashboard',
        body: { classList: { toggle() {} } },
        getElementById: () => ({ innerHTML: '', classList: { add() {} }, style: {} }),
        addEventListener: (type, fn) => { if (type === 'DOMContentLoaded') docHandlers.push(fn); },
    },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {}, clear: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { serviceWorker: { register: () => ({ catch: () => {} }) } },
    location: { reload() { reloads++; }, search: '', hash: '#analytics', pathname: '/', href: '', origin: 'https://app.test', hostname: 'app.test', protocol: 'https:' },
    attachEventListeners: () => {},
    showPage: () => {},
    initOnboardingChecklist: () => {},
    checkApiOnboarding: () => {},
    exchangeCodeForToken: () => {},
    getRegistrationUrl: () => '',
    REDIRECT_URI: '',
};
const ctx = vm.createContext(sandbox);
sandbox.window = {
    __modBooted: undefined,
    location: sandbox.location,
    history: { replaceState() {} },
    addEventListener: (type, fn) => winHandlers.push([type, fn]),
    crypto: { subtle: { digest: () => new Uint8Array(32) } },
    APP_CONFIG: {},
};

vm.runInContext(bootJs, ctx);

const cut = mainJs.indexOf('    initializeApp();');
const prologue = mainJs.slice(0, cut + '    initializeApp();'.length) + '\n});';
const stripped = prologue.replace(/^\s*import\s*\{[^}]*\}\s*from\s*['"][^'"]*['"];?\s*$/gm, '');
vm.runInContext(stripped, ctx);

// Expected fetches derived from the boot list itself, not a magic number:
// commit 21809f485 (chore(launch-followup-6): drop the first-run gate) removed
// the first-run shell, so the boot list is 5 shells now and can change again.
const bootFetches = (stripped.match(/fetch\(/g) || []).length;
assert.ok(bootFetches > 0, 'main.js boot list must fetch at least one page shell');

assert.equal(docHandlers.length, 1, 'main.js registered exactly one DOMContentLoaded handler');

// dispatch DOMContentLoaded twice, then a late <script> error
for (const fn of docHandlers) fn();
for (const fn of docHandlers) fn();
const errHandler = winHandlers.filter(([t]) => t === 'error').map(([, fn]) => fn);
for (const fn of errHandler) fn({ target: { tagName: 'SCRIPT', src: 'js/some-page-module.js' } });

assert.equal(fetches, bootFetches, 'exactly one boot worth of page-shell fetches (one per shell in main.js boot list) across two dispatches');
assert.equal(reloads, 0, 'no reload fired on the refresh path (boot flag blocks the late script error)');

console.log('BUG.B47 single-boot check: OK');
