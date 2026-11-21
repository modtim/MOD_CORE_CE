'use strict';
// FIX-ADD-WORKER-MODAL-DEFAULT-API-URL-NOT-LOCALHOST-LAN-ACCESS-INSTALL-SHOWS-LAN-URL
// - the Workers -> Add Worker modal builds the command
// a DIFFERENT machine runs (the worker curls the setup script). On a default CE
// install APP_CONFIG.API_URL is a loopback name (http://localhost:8000/v1), which
// only resolves on the CE host, so a worker enrolled from a second computer gets
// a command it cannot run. Owner rule: CE LAN access is first-class - no hand-set
// variables. window.location.hostname IS the address the browser used, so swap a
// loopback API host for it (protocol, port and path kept).
//
// Technique (as workers-unlimited-display.check.js): the shipped helper block is
// EXTRACTED VERBATIM from js/workers.js and run in a Function sandbox with a stub
// `window`; the call sites are checked at the source level.
//
//   node CORE/Dashboard/tests/add-worker-modal-lan-url.check.js
//
// Asserts:
//   A1 API_URL loopback + page host 203.0.113.67 -> command has
//      http://203.0.113.67:8000/v1/workers/setup-script and NOT "localhost"
//   A2 API_URL loopback + page host localhost    -> command keeps localhost
//   A3 API_URL already non-loopback              -> unchanged, byte for byte
//   A4 Windows + macOS one-liners use the same LAN URL (MOD_API_URL, irm, --api-url)
//   A5 loopback fallbacks (127.0.0.1, ::1, api.localhost) substitute; page host
//      0.0.0.0 / empty keeps the original; a malformed URL keeps the original
//   A6 the shipped source defines enrollApiUrl and wires it into setupCmd /
//      windowsCmd / macosCmd only (browser fetches still use raw apiUrl)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'workers.js'), 'utf8');

// --- extract the shipped helper block verbatim ------------------------------
const bStart = src.indexOf('// FIX-ADD-WORKER-MODAL-API-URL-LAN (11-15) BEGIN');
assert.ok(bStart >= 0, 'helper block BEGIN marker not found in workers.js');
const bEnd = src.indexOf('// FIX-ADD-WORKER-MODAL-API-URL-LAN END', bStart);
assert.ok(bEnd > bStart, 'helper block END marker not found');
const helperBlock = src.slice(bStart, bEnd);

const build = new Function(
    'window',
    helperBlock + '\nreturn { enrollApiUrl, ENROLL_LOOPBACK_HOSTS };'
);

function makeWindow(hostname) {
    return { location: { hostname: hostname } };
}

const CASES = [];
function record(name, fn) { CASES.push({ name, fn }); }

record('A1 loopback API_URL + LAN page host -> LAN URL in command', () => {
    const w = makeWindow('203.0.113.67');
    const apiUrl = 'http://localhost:8000/v1';
    const enrollUrl = build(w).enrollApiUrl(apiUrl);
    assert.strictEqual(enrollUrl, 'http://203.0.113.67:8000/v1', 'A1 substituted host, kept proto/port/path');
    const setupCmd = `curl -fsSL "${enrollUrl}/workers/setup-script" | sudo ENROLLMENT_TOKEN='tok' bash`;
    assert.ok(setupCmd.includes('http://203.0.113.67:8000/v1/workers/setup-script'), 'A1 command carries LAN URL');
    assert.ok(!setupCmd.includes('localhost'), 'A1 command does NOT mention localhost');
});

record('A2 both localhost -> keeps localhost', () => {
    const w = makeWindow('localhost');
    const apiUrl = 'http://localhost:8000/v1';
    const enrollUrl = build(w).enrollApiUrl(apiUrl);
    assert.strictEqual(enrollUrl, apiUrl, 'A2 unchanged (honest same-host value)');
    const setupCmd = `curl -fsSL "${enrollUrl}/workers/setup-script" | sudo ENROLLMENT_TOKEN='tok' bash`;
    assert.ok(setupCmd.includes('http://localhost:8000/v1/workers/setup-script'), 'A2 keeps localhost command');
});

record('A3 API_URL already non-loopback -> unchanged', () => {
    const w = makeWindow('203.0.113.67');
    const apiUrl = 'http://example.com:8000/v1';
    const enrollUrl = build(w).enrollApiUrl(apiUrl);
    assert.strictEqual(enrollUrl, apiUrl, 'A3 byte-for-byte unchanged');
    const w2 = makeWindow('203.0.113.67');
    const apiUrl2 = 'http://203.0.113.67:8000/v1';
    assert.strictEqual(build(w2).enrollApiUrl(apiUrl2), apiUrl2, 'A3b LAN API_URL + LAN page host unchanged');
});

record('A4 Windows + macOS one-liners use the LAN URL', () => {
    const w = makeWindow('203.0.113.67');
    const enrollUrl = build(w).enrollApiUrl('http://localhost:8000/v1');
    const windowsCmd = `$env:MOD_API_URL='${enrollUrl}'; $env:MOD_BOOTSTRAP_TOKEN='e'; iex (irm '${enrollUrl}/workers/agent/install-script?token=d')`;
    const macosCmd = `curl -fsSL '${enrollUrl}/workers/agent/install-script?token=d' | bash -s -- --api-url ${enrollUrl} --token t --bootstrap-token b`;
    assert.ok(windowsCmd.includes("$env:MOD_API_URL='http://203.0.113.67:8000/v1'"), 'A4 MOD_API_URL is LAN');
    assert.ok(windowsCmd.includes("irm 'http://203.0.113.67:8000/v1/workers/agent/install-script"), 'A4 irm URL is LAN');
    assert.ok(macosCmd.includes("--api-url http://203.0.113.67:8000/v1"), 'A4 --api-url is LAN');
    assert.ok(!windowsCmd.includes('localhost') && !macosCmd.includes('localhost'), 'A4 no localhost left');
});

record('A5 loopback aliases + fallbacks', () => {
    const w = makeWindow('203.0.113.67');
    const f = build(w).enrollApiUrl;
    assert.strictEqual(f('http://127.0.0.1:8000/v1'), 'http://203.0.113.67:8000/v1', 'A5 127.0.0.1 substitutes');
    assert.strictEqual(f('http://api.localhost:8000/v1'), 'http://203.0.113.67:8000/v1', 'A5 api.localhost substitutes');
    assert.strictEqual(f('http://localhost:8000/v1?x=1#y'), 'http://203.0.113.67:8000/v1?x=1#y', 'A5 query+fragment preserved');
    const w2 = makeWindow('0.0.0.0');
    assert.strictEqual(build(w2).enrollApiUrl('http://localhost:8000/v1'), 'http://localhost:8000/v1', 'A5 page host 0.0.0.0 is not a usable name -> unchanged');
    const w3 = makeWindow('');
    assert.strictEqual(build(w3).enrollApiUrl('http://localhost:8000/v1'), 'http://localhost:8000/v1', 'A5 empty page host -> unchanged');
    assert.strictEqual(build(w).enrollApiUrl('not a url'), 'not a url', 'A5 malformed URL -> original string');
});

record('A6 shipped source wiring', () => {
    assert.ok(/function enrollApiUrl\(apiUrl\)/.test(src), 'A6 helper defined');
    assert.ok(src.includes('const enrollUrl = enrollApiUrl(apiUrl);'), 'A6 enrollUrl computed once');
    assert.ok(src.includes('curl -fsSL "${enrollUrl}/workers/setup-script"'), 'A6 setupCmd uses enrollUrl');
    assert.ok(src.includes("$env:MOD_API_URL='${enrollUrl}'"), 'A6 windowsCmd uses enrollUrl');
    assert.ok(src.includes('--api-url ${enrollUrl}'), 'A6 macosCmd uses enrollUrl');
    assert.ok(src.includes('fetch(`${apiUrl}/workers/enrollment-tokens`'), 'A6 browser fetch still uses raw apiUrl');
    assert.ok(src.includes('fetch(`${apiUrl}/workers/agent/download-token'), 'A6 token mint fetch still uses raw apiUrl');
    const decls = (src.match(/const apiUrl = window\.APP_CONFIG\?\.API_URL/g) || []).length;
    assert.ok(decls >= 14, 'A6 the other apiUrl declarations untouched (>=14)');
});

let passed = 0, failed = 0;
for (const c of CASES) {
    try { c.fn(); passed++; console.log('PASS ' + c.name); }
    catch (e) { failed++; console.log('FAIL ' + c.name + ': ' + e.message); }
}
console.log('add-worker-modal-lan-url: ' + passed + ' passed / ' + failed + ' failed');
process.exit(failed === 0 ? 0 : 1);
