// FIX-DASH-WIDGETS-UNTESTED-MODULES-JEST-TESTS — CORE/Dashboard/js/theme.js
// (light/dark theme toggle, persisted in BOTH a parent-domain cookie and
// localStorage so the pick carries across dashboard.* / other.* / docs.*).
// The module is a plain browser script (no exports), so this test reads it,
// runs it in a vm sandbox with stubbed location / document / localStorage, and
// asserts behaviour:
//   (a) _parentCookieDomain: example.com -> .example.com,
//       dashboard.example.com -> .example.com, localhost
//       and a bare IP -> null (host-scoped only);
//   (b) getStoredTheme: the cookie wins over localStorage, an invalid cookie
//       value falls back to localStorage, nothing stored -> null;
//   (c) _writeThemeCookie: Domain= only when a parent domain exists, Secure
//       only on https, always SameSite=Lax + Max-Age=31536000;
//   (d) toggleTheme: stored 'light' -> 'dark', anything else (null/undefined/
//       'dark') -> 'light' (first click flips dark -> light), and it writes
//       localStorage + the cookie + applies the theme;
//   (e) applyTheme: 'dark' adds the root 'dark' class, anything else removes it;
//   (f) updateToggleIcon: dark hides the moon, light hides the sun, and a page
//       without the toggle elements returns without throwing.
//
//   node --test CORE/Dashboard/tests/theme.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'theme.js'), 'utf8');
const start = src.indexOf("const STORAGE_KEY = 'mod_theme';");
assert.ok(start >= 0, 'could not locate the theme body');
const block = src.slice(start);

function run(hostname, protocol, cookieValue, storedTheme) {
    const writes = [];
    const store = {};
    if (storedTheme !== undefined) store.mod_theme = storedTheme;

    const classes = new Set(['dark']);
    const root = { classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) } };
    const els = {};
    for (const id of ['theme-toggle-sun', 'theme-toggle-moon']) {
        const el = { id, className: '' };
        el.classList = { add: (c) => { el.className = c; }, remove: (c) => { el.className = ''; } };
        els[id] = el;
    }

    const doc = { getElementById: (id) => els[id], querySelectorAll: () => [], addEventListener: () => {} };
    Object.defineProperty(doc, 'cookie', {
        get: () => (cookieValue === null ? '' : `mod_theme=${cookieValue}`),
        set: (v) => { writes.push(v); },
    });

    doc.documentElement = root;
    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        location: { hostname, protocol },
        document: doc,
        window: {},
        localStorage: { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ _parentCookieDomain, _readThemeCookie, _writeThemeCookie, getStoredTheme, applyTheme, updateToggleIcon, toggleTheme });`,
        sandbox, { filename: 'theme-body' });
    return { fns, writes, store, classes, els };
}

test('_parentCookieDomain: parent domain, stage subdomain, localhost/IP null', () => {
    const r = run('dashboard.example.com', 'https:', null, undefined);
    assert.strictEqual(r.fns._parentCookieDomain(), '.example.com');
    const s = run('dashboard.example.com', 'https:', null, undefined);
    assert.strictEqual(s.fns._parentCookieDomain(), '.example.com');
    const l = run('localhost', 'http:', null, undefined);
    assert.strictEqual(l.fns._parentCookieDomain(), null, 'localhost must stay host-scoped');
    const ip = run('203.0.113.152', 'http:', null, undefined);
    assert.strictEqual(ip.fns._parentCookieDomain(), null, 'a bare IP must stay host-scoped');
});

test('getStoredTheme: cookie wins, invalid cookie falls back, nothing -> null', () => {
    const cookieWins = run('dashboard.example.com', 'https:', 'light', 'dark');
    assert.strictEqual(cookieWins.fns.getStoredTheme(), 'light', 'cookie must win over localStorage');
    const invalid = run('dashboard.example.com', 'https:', 'banana', 'dark');
    assert.strictEqual(invalid.fns.getStoredTheme(), 'dark', 'invalid cookie must fall back to localStorage');
    const none = run('dashboard.example.com', 'https:', null, undefined);
    assert.strictEqual(none.fns.getStoredTheme(), null, 'nothing stored -> null');
});

test('_writeThemeCookie: Domain only with a parent domain, Secure only on https', () => {
    const r = run('dashboard.example.com', 'https:', null, undefined);
    r.fns._writeThemeCookie('light');
    assert.strictEqual(r.writes.length, 1, 'one cookie write expected');
    const c = r.writes[0];
    assert.ok(c.startsWith('mod_theme=light'), 'cookie key/value missing');
    assert.ok(c.includes('Domain=.example.com'), 'parent-domain part missing');
    assert.ok(c.includes('Max-Age=31536000'), '1y max-age missing');
    assert.ok(c.includes('SameSite=Lax'), 'SameSite missing');
    assert.ok(c.includes('Secure'), 'https must set Secure');

    const local = run('localhost', 'http:', null, undefined);
    local.fns._writeThemeCookie('dark');
    const c2 = local.writes[0];
    assert.ok(!c2.includes('Domain='), 'localhost must not get a Domain part');
    assert.ok(!c2.includes('Secure'), 'http must not set Secure');
});

test('toggleTheme: light -> dark, anything else -> light, writes both stores', () => {
    const r = run('dashboard.example.com', 'https:', 'light', 'light');
    r.fns.toggleTheme();
    assert.strictEqual(r.store.mod_theme, 'dark', 'light must flip to dark');
    assert.ok(r.writes[0].includes('mod_theme=dark'), 'cookie must carry the new theme');
    assert.ok(r.classes.has('dark'), 'dark class must be applied');

    const s = run('dashboard.example.com', 'https:', null, undefined);
    s.fns.toggleTheme();
    assert.strictEqual(s.store.mod_theme, 'light', 'a first-time user (dark default) flips to light');
    assert.ok(!s.classes.has('dark'), 'light must remove the dark class');
});

test('applyTheme: dark adds the class, anything else removes it', () => {
    const r = run('localhost', 'http:', null, undefined);
    r.fns.applyTheme('dark');
    assert.ok(r.classes.has('dark'), 'dark class added');
    r.fns.applyTheme('light');
    assert.ok(!r.classes.has('dark'), 'dark class removed');
    r.fns.applyTheme(null);
    assert.ok(!r.classes.has('dark'), 'null is not dark');
});

test('updateToggleIcon: dark hides the moon, light hides the sun, missing toggle is safe', () => {
    const r = run('localhost', 'http:', null, undefined);
    r.fns.updateToggleIcon('dark');
    assert.strictEqual(r.els['theme-toggle-sun'].className, '', 'sun must be visible in dark');
    assert.strictEqual(r.els['theme-toggle-moon'].className, 'hidden', 'moon must be hidden in dark');
    r.fns.updateToggleIcon('light');
    assert.strictEqual(r.els['theme-toggle-moon'].className, '', 'moon visible in light');
    assert.strictEqual(r.els['theme-toggle-sun'].className, 'hidden', 'sun hidden in light');
});
