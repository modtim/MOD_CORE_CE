// 2400LR-g5b: the Bulk Permissions page never ran its JS because ui.js mounts
// page HTML via innerHTML (scripts inert) and had no 'bulk-permissions' init
// branch. Fix = exported initializeBulkPermissionsPage() called by ui.js.
//   node --test CORE/Dashboard/tests/bulk-permissions-init.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const uiSrc = fs.readFileSync(path.join(root, 'js', 'ui.js'), 'utf8');
const bpSrc = fs.readFileSync(path.join(root, 'js', 'bulk-permissions.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'pages', 'bulk-permissions.html'), 'utf8');

test('(a) ui.js imports and calls initializeBulkPermissionsPage for bulk-permissions', () => {
    assert.match(uiSrc, /import\s*\{\s*initializeBulkPermissionsPage\s*\}\s*from\s*'\.\/bulk-permissions\.js'/);
    assert.match(uiSrc,
        /targetPageId === 'bulk-permissions'\s*\)\s*\{\s*initializeBulkPermissionsPage\(/);
});

test('(b) module exports the init fn, parses, and has no top-level await', () => {
    assert.match(bpSrc, /export async function initializeBulkPermissionsPage\(/);
    // Strip `export ` and parse as a CLASSIC script: a top-level await (the
    // original bug) would be a SyntaxError here, as would a broken edit.
    const classic = bpSrc.replace(/^export /gm, '');
    assert.doesNotThrow(() => new vm.Script(classic, { filename: 'bulk-permissions.js' }));
    const fnStart = bpSrc.indexOf('export async function initializeBulkPermissionsPage(');
    const lastAwait = bpSrc.lastIndexOf('await loadPermissions();');
    const closeBrace = bpSrc.trimEnd().lastIndexOf('}');
    assert.ok(fnStart >= 0 && lastAwait > fnStart, 'trailing awaits must be inside the function');
    assert.ok(closeBrace > lastAwait, 'function must close after the last await');
});

test('(c) page html no longer carries the dead script tag', () => {
    assert.ok(!/<script[^>]*src=["']\/js\/bulk-permissions\.js["']/.test(html));
});

function sliceEsc() {
    const s = bpSrc.indexOf('function esc(');
    assert.ok(s >= 0, 'esc not found');
    const e = bpSrc.indexOf('\n}\n', s);
    return vm.runInNewContext(bpSrc.slice(s, e + 3) + '\nesc');
}

test('(d) esc neutralises markup and tolerates null', () => {
    const esc = sliceEsc();
    const out = esc('<img src=x onerror=1>');
    assert.ok(!out.includes('<') && !out.includes('>'), out);
    assert.strictEqual(esc(null), '');
    assert.strictEqual(esc('"\''), '&quot;&#39;');
});

test('(e) no server-derived value is interpolated into HTML without esc()', () => {
    const bare = /\$\{\s*(perm\.\w+(\s*\|\|[^}]*)?|result\.(total_requested|successful|failed)|r\.(status|error_message))\s*\}/;
    assert.ok(!bare.test(bpSrc), 'bare interpolation of server data: ' + (bpSrc.match(bare) || [])[0]);
    for (const needle of ['esc(perm.target_id)', 'esc(perm.id)', 'esc(perm.level)',
        'esc(result.total_requested)', 'esc(result.successful)', 'esc(result.failed)',
        'esc(r.status)', 'esc(r.error_message)']) {
        assert.ok(bpSrc.includes(needle), 'missing ' + needle);
    }
});

test('(f) initializeBulkPermissionsPage runs and fetches /assets and /users', async () => {
    const calls = [];
    const mkEl = () => ({
        innerHTML: '', textContent: '', value: '', options: [], selectedOptions: [],
        className: '', dataset: {}, id: '',
        classList: { add() {}, remove() {} },
        addEventListener() {}, appendChild() {}, remove() {},
    });
    const sandbox = {
        window: { APP_CONFIG: { API_URL: 'http://x' }, location: { origin: 'http://x' } },
        document: {
            getElementById: () => mkEl(), querySelectorAll: () => [],
            createElement: mkEl,
        },
        sessionStorage: { getItem: () => 't' },
        fetch: async (url) => {
            calls.push(String(url));
            return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => [] };
        },
        console: { error() {}, log() {} }, alert() {}, confirm: () => true,
        parseInt, Array, Date, String, JSON, Promise, Error,
    };
    vm.createContext(sandbox);
    vm.runInContext(bpSrc.replace(/^export /gm, '') + '\nglobalThis.__init = initializeBulkPermissionsPage;', sandbox);
    await sandbox.__init();
    assert.ok(calls.some(u => u.includes('/assets')), calls.join(','));
    assert.ok(calls.some(u => u.includes('/users')), calls.join(','));
});
