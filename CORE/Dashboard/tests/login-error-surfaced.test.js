// Tim bug (11-06): a rejected login (JIT seat limit / unknown IdP domain /
// MFA enrollment gate) bounced silently to the login page, which looked like
// a logout. Asserts:
//   (a) the thrown error carries .status === 403 and .detail.code for each of
//       the three structured codes (api.js keeps the server payload on ApiError);
//   (b) the login-page element's text contains the server `message` and NOT
//       JSON.stringify(detail);
//   (c) `metadata` values never appear in the rendered text, and two tenants'
//       requirements render independently (nothing client-inferred);
//   (d) a 500, a non-JSON 403 and an unknown 403 code keep the OLD bounce;
//   (e) the verifier is removed on every failure path;
//   (f) no performLogout / navigation on the three 403 codes.
//
// api.js + auth.js are ES modules with sibling imports, so the `export`
// keywords and auth.js import lines are stripped and both bodies run in ONE
// vm sandbox (api.js first, aliased to apiExchangeCodeForToken, then auth.js,
// whose wrapper rebinds exchangeCodeForToken) — same technique as
// pkce-http-fallback.test.js. fetch, document,
// showPage, localStorage and sessionStorage are stubbed. No network, no DB.
//
//   node --test CORE/Dashboard/tests/login-error-surfaced.test.js
'use strict';
const assert = require('node:assert');
const { test, describe } = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8')
    .replace(/^export \{ [^\n]*\};\s*$/m, '')
    .replace(/^export /gm, '');
const authSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'auth.js'), 'utf8')
    .replace(/^import [\s\S]*?;\s*$/gm, '')
    .replace(/^export /gm, '');
assert.ok(!/^\s*(export|import) /m.test(apiSrc), 'api.js: no ESM keywords left');
assert.ok(!/^\s*(export|import) /m.test(authSrc), 'auth.js: no ESM keywords left');

const SEAT = { code: 'seat_limit_exceeded', message: 'Your team is at its seat limit. Contact your tenant admin to upgrade the plan or remove an inactive user.', metadata: { domain: 'tenant-a.example', user_id: 'sub-aaaa-1111' } };
const UNKNOWN = { code: 'unknown_domain', message: "Your team isn't on MOD yet. Ask your IT team to set up MOD for your organisation.", metadata: { domain: 'tenant-b.example', user_id: 'sub-bbbb-2222' } };
const MFA = { code: 'MFA_ENROLLMENT_REQUIRED', message: 'Enroll a second factor before signing in.', metadata: { required_action: 'enroll-totp', user_id: 'sub-cccc-3333' } };

function resp(status, detail, nonJson) {
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: () => 'application/json' },
        json: async () => {
            if (nonJson) throw new SyntaxError('non-JSON body');
            return { detail };
        },
    };
}

// One fresh browser-ish sandbox per scenario; returns the trace + page text.
function boot(response) {
    const store = { pkce_code_verifier: 'verifier-for-test' };
    const pages = {};
    const trace = { showPage: [], navigate: [], logout: [], fetch: [] };
    const newEl = (tag) => {
        const el = { id: null, style: { display: '' }, textContent: '', kids: [] };
        el.appendChild = (c) => { el.kids.push(c); return el; };
        return el;
    };
    const page = (id) => {
        if (!pages[id]) { const el = newEl('div'); el.id = id; pages[id] = el; }
        return pages[id];
    };
    // api.js and auth.js each declare API_BASE_URL at top level, so they run
    // in their OWN vm context (same as their real ESM scopes); auth.js's
    // import of api.js is bridged by a host function into the api context.
    const apiSandbox = {
        console, JSON, Math, btoa,
        document: { getElementById: page, createElement: newEl, title: 'MOD Dashboard' },
        localStorage: {
            getItem: (k) => (k in store ? store[k] : null),
            setItem: (k, v) => { store[k] = v; },
            removeItem: (k) => { delete store[k]; },
        },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        performLogout: () => { trace.logout.push('performLogout'); },
        redirectToReauth: () => {},
        updateUIVisibility: () => {},
        hideLoadingScreen: () => {},
        checkApiOnboarding: () => {},
        initOnboardingChecklist: () => {},
        setActiveIdentity: () => {},
        startIdleWarning: () => {},
        deadlineFromSession: () => null,
        REDIRECT_URI: 'http://api.test/callback',
    };
    apiSandbox.window = {
        APP_CONFIG: { API_URL: 'http://api.test', KEYCLOAK_CLIENT_ID: 'dashboard-client' },
        location: { pathname: '/', hash: '', origin: 'http://api.test' },
        history: { replaceState() {} },
        dispatchEvent: () => {},
        addEventListener: () => {},
    };
    apiSandbox.window.window = apiSandbox.window;
    apiSandbox.fetch = (url, options) => { trace.fetch.push(url); return Promise.resolve(response()); };
    apiSandbox.globalThis = apiSandbox;
    vm.createContext(apiSandbox);
    vm.runInContext(apiSrc, apiSandbox, { filename: 'api.js' });

    const authSandbox = {
        console, JSON, Math, btoa,
        document: { getElementById: page, createElement: newEl, title: 'MOD Dashboard' },
        localStorage: apiSandbox.localStorage,
        sessionStorage: apiSandbox.sessionStorage,
        showPage: (el) => { trace.showPage.push(el.id); el.style.display = 'flex'; },
        navigateToPage: (h) => { trace.navigate.push(h); },
        performLogout: () => { trace.logout.push('performLogout'); },
        updateUIVisibility: () => {},
        hideLoadingScreen: () => {},
        checkApiOnboarding: () => {},
        initOnboardingChecklist: () => {},
        setActiveIdentity: () => {},
        startIdleWarning: () => {},
        deadlineFromSession: () => null,
        parseJwt: () => ({}),
        me: () => ({}),
        apiExchangeCodeForToken: (code, redirectUri) =>
            vm.runInContext(`exchangeCodeForToken(${JSON.stringify(code)}, ${JSON.stringify(redirectUri)})`, apiSandbox),
        REDIRECT_URI: 'http://api.test/callback',
    };
    authSandbox.window = apiSandbox.window;
    authSandbox.window.window = apiSandbox.window;
    authSandbox.globalThis = authSandbox;
    vm.createContext(authSandbox);
    vm.runInContext(authSrc, authSandbox, { filename: 'auth.js' });
    const textOf = (el) => (el.textContent || '') + el.kids.map(textOf).join('');
    return {
        call: () => vm.runInContext(`(async () => { await exchangeCodeForToken('the-code', 'http://api.test/callback'); })()`, authSandbox),
        apiCall: () => vm.runInContext(`(async () => { return await exchangeCodeForToken('the-code', 'http://api.test/callback'); })()`, apiSandbox),
        loginText: () => textOf(page('login-page')),
        verifier: () => 'pkce_code_verifier' in store,
        trace,
    };
}

async function thrown(env) {
    try { await env.call(); return null; } catch (e) { return e; }
}

describe('api.js exchangeCodeForToken keeps the structured 403 payload', () => {
    for (const shape of [SEAT, UNKNOWN, MFA]) {
        test(`${shape.code}: thrown error carries .status === 403 and .detail.code`, async () => {
            const env = boot(() => resp(403, shape));
            let err = null;
            try { await env.apiCall(); } catch (e) { err = e; }
            assert.ok(err, 'the exchange must still throw');
            assert.strictEqual(err.name, 'ApiError');
            assert.strictEqual(err.status, 403);
            assert.ok(err.detail && typeof err.detail === 'object');
            assert.strictEqual(err.detail.code, shape.code);
            assert.strictEqual(err.message, shape.message, 'message is the server message, not a canned string');
        });
    }
});

describe('auth.js renders the server message on the login page', () => {
    for (const shape of [SEAT, UNKNOWN, MFA]) {
        test(`${shape.code}: login-page text is the server message, not JSON`, async () => {
            const env = boot(() => resp(403, shape));
            await thrown(env);
            const text = env.loginText();
            assert.ok(text.includes(shape.message), 'server message rendered');
            assert.ok(!text.includes(JSON.stringify(shape)), 'no JSON.stringify(detail) dump');
            assert.ok(!text.includes('{'), 'no raw payload braces');
            for (const [k, v] of Object.entries(shape.metadata)) {
                assert.ok(!text.includes(String(v)), `metadata value ${k} never rendered`);
            }
            assert.ok(!env.trace.showPage.length, 'no silent bounce on a known 403 code');
            assert.deepStrictEqual(env.trace.logout, [], 'no performLogout');
            assert.deepStrictEqual(env.trace.navigate, [], 'no navigation');
            assert.strictEqual(env.verifier(), false, 'verifier still removed');
        });
    }

    test('MFA_ENROLLMENT_REQUIRED in the real API shape (code + required_action, no message) still renders text, not a silent bounce', async () => {
        const env = boot(() => resp(403, { code: 'MFA_ENROLLMENT_REQUIRED', required_action: 'CONFIGURE_TOTP' }));
        await thrown(env);
        const text = env.loginText();
        assert.ok(text.includes('Two-factor authentication must be set up'), 'client fallback message rendered');
        assert.ok(!text.includes('{') && !text.includes('CONFIGURE_TOTP'), 'no raw payload');
        assert.ok(!env.trace.showPage.length, 'no silent bounce');
        assert.deepStrictEqual(env.trace.logout, [], 'no performLogout');
        assert.strictEqual(env.verifier(), false, 'verifier still removed');
    });

    test('cross-tenant: two tenant requirements render independently', async () => {
        const a = boot(() => resp(403, SEAT));
        const b = boot(() => resp(403, UNKNOWN));
        await thrown(a);
        await thrown(b);
        const ta = a.loginText();
        const tb = b.loginText();
        assert.ok(ta.includes(SEAT.message) && !ta.includes(UNKNOWN.message));
        assert.ok(tb.includes(UNKNOWN.message) && !tb.includes(SEAT.message));
        assert.ok(!ta.includes(UNKNOWN.metadata.domain) && !ta.includes(UNKNOWN.metadata.user_id));
        assert.ok(!tb.includes(SEAT.metadata.domain) && !tb.includes(SEAT.metadata.user_id));
    });
});

describe('unknown failures keep the existing bounce', () => {
    const cases = [
        ['500 with a structured body', () => resp(500, { code: 'jit_rejected', message: 'server boom', metadata: { user_id: 'sub-dddd-4444' } })],
        ['non-JSON 403', () => resp(403, null, true)],
        ['403 with an unknown code', () => resp(403, { code: 'jit_rejected', message: 'rejected by policy', metadata: {} })],
    ];
    for (const [label, response] of cases) {
        test(`${label}: showPage(login-page) still called, message not rendered`, async () => {
            const env = boot(response);
            await thrown(env);
            assert.ok(env.trace.showPage.includes('login-page'), 'old bounce kept');
            assert.strictEqual(env.loginText(), '', 'no message rendered');
            assert.strictEqual(env.verifier(), false, 'verifier still removed');
            assert.deepStrictEqual(env.trace.logout, []);
            assert.deepStrictEqual(env.trace.navigate, []);
        });
    }
});
