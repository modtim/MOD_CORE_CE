// PKCE on plain-HTTP LAN installs — Dashboard/js/pkce-crypto.js + js/auth.js.
//
// Owner bug: on http://203.0.113.150:3000 the login button threw
// `TypeError: Cannot read properties of undefined (reading 'digest')` because
// crypto.subtle is undefined in insecure contexts. Asserts:
//   (a) the pure-JS SHA-256 fallback matches node crypto.createHash for
//       '', 'abc' and a 1000-char string;
//   (b) the fallback challenge equals the crypto.subtle challenge for the
//       same verifier (base64url identical);
//   (c) with crypto.subtle ABSENT (insecure-context emulation) auth.js
//       getLoginUrl still returns a valid S256 URL and the verifier is
//       stashed for the exchange;
//   (d) uuid4() works without crypto.randomUUID (getRandomValues only).
//
// auth.js is an ESM with sibling imports, so its import lines are stripped
// and the body runs in a vm sandbox whose `crypto` global mimics an
// insecure browser context (getRandomValues present, subtle/randomUUID
// absent) — exactly what Chrome does on http://<LAN-IP>.
//
//   node --test CORE/Dashboard/tests/pkce-http-fallback.test.js
'use strict';
const assert = require('node:assert');
const { test, describe } = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const nodeCrypto = require('node:crypto');

const hexOf = (bytes) => Buffer.from(bytes).toString('hex');
const nodeSha = (s) => nodeCrypto.createHash('sha256').update(s).digest('hex');
const b64urlBuf = (buf) => Buffer.from(buf).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

function loadModule(rel) {
    const src = fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');
    const body = src.replace(/^export /gm, '');
    assert.ok(!/^\s*import /m.test(body), 'no imports left to strip');
    return body;
}

// --- sandbox factory: `crypto` mimics a browser context; insecure = no subtle
function makeSandbox({ insecure }) {
    const sandbox = { btoa, console, Math, JSON, TextEncoder, URLSearchParams };
    const web = nodeCrypto.webcrypto;
    sandbox.crypto = insecure
        ? { getRandomValues: web.getRandomValues.bind(web) } // insecure context: subtle/randomUUID absent
        : web;
    // localStorage stand-in so getLoginUrl can stash the verifier.
    const store = {};
    sandbox.localStorage = {
        getItem: (k) => (k in store ? store[k] : null),
        setItem: (k, v) => { store[k] = String(v); },
        removeItem: (k) => { delete store[k]; },
    };
    sandbox._store = store;
    sandbox.window = {
        APP_CONFIG: { KEYCLOAK_URL: 'http://203.0.113.150:8080' },
        location: { origin: 'http://203.0.113.150:3000', pathname: '/', hash: '' },
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    return sandbox;
}

const runIn = (sandbox, code) => vm.runInContext(code, sandbox);

// ---------------------------------------------------------------------------
// (a) known-answer vectors + (b) fallback == subtle, via pkce-crypto.js alone
// ---------------------------------------------------------------------------
describe('pkce-crypto sha256 fallback', () => {
    const body = loadModule('js/pkce-crypto.js');
    const insecure = makeSandbox({ insecure: true });
    const secure = makeSandbox({ insecure: false });
    runIn(insecure, body);
    runIn(secure, body);

    for (const [label, vec] of [
        ['empty string', ''],
        ['abc', 'abc'],
        ['1000-char string', 'a'.repeat(1000)],
        ['non-ASCII string', 'héllo — 日本語 🎃'],
    ]) {
        test(`fallback matches node crypto.createHash for ${label}`, async () => {
            const digest = await runIn(insecure, `sha256Digest(${JSON.stringify(vec)})`);
            assert.strictEqual(hexOf(digest), nodeSha(vec));
        });
    }

    test('fallback challenge equals crypto.subtle challenge for same verifier', async () => {
        const verifier = 'a'.repeat(128); // PKCE verifiers are ASCII
        const expr = `(async () => b64urlFromBytes(await sha256Digest(${JSON.stringify(verifier)})))()`;
        const fallback = await runIn(insecure, expr);
        const native = await runIn(secure, expr);
        assert.strictEqual(fallback, native);
        assert.strictEqual(fallback, b64urlBuf(nodeCrypto.createHash('sha256').update(verifier).digest()));
        assert.match(fallback, /^[A-Za-z0-9_-]{43}$/);
    });

    test('uuid4 works without crypto.randomUUID', () => {
        const u = runIn(insecure, 'uuid4()');
        assert.match(u, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    });
});

// ---------------------------------------------------------------------------
// (c) auth.js getLoginUrl with crypto.subtle absent (the owner's crash path)
// ---------------------------------------------------------------------------
describe('auth.js getLoginUrl in an insecure context', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'auth.js'), 'utf8');
    // Strip every import (siblings are stubbed by the sandbox) and keep
    // exports (harmless in vm: `export function` -> `function`).
    const body = src
        .replace(/^import [\s\S]*?;\s*$/gm, '')
        .replace(/^export /gm, '');

    const insecure = makeSandbox({ insecure: true });
    // auth.js imports sha256Digest/b64urlFromBytes from pkce-crypto.js; the
    // import line is stripped, so define the helper in the same sandbox.
    runIn(insecure, loadModule('js/pkce-crypto.js'));
    runIn(insecure, body);

    test('getLoginUrl returns a valid S256 URL with crypto.subtle absent', async () => {
        const url = await runIn(insecure, 'getLoginUrl()');
        assert.ok(url.startsWith('http://203.0.113.150:8080/realms/modtex/protocol/openid-connect/auth?'));
        assert.ok(url.includes('code_challenge_method=S256'));
        const m = url.match(/[?&]code_challenge=([A-Za-z0-9_-]+)/);
        assert.ok(m, 'code_challenge present and base64url');
        assert.strictEqual(m[1].length, 43);
        const verifier = insecure._store['pkce_code_verifier'];
        assert.ok(verifier && verifier.length === 128, 'verifier stashed for the exchange');
        // The challenge must be the real SHA-256 of the verifier.
        assert.strictEqual(
            m[1],
            b64urlBuf(nodeCrypto.createHash('sha256').update(verifier).digest()),
        );
    });
});
