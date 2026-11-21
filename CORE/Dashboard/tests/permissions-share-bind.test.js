// FIX-PERMISSIONS-SHARE-BUTTON (, D72): the Share buttons on
// the Permissions page are static in permissions.html and are enabled by a row
// click as soon as one item renders, but permissions.js bound their click
// listeners only AFTER `await me.get()` + `await loadFolderTree()` in
// initializePermissionsPage(). A click in that window was a silent no-op (no
// share request fired, no #bulk-grant-modal) - found by d06071c64c
// (dash-permissions race). This test simulates a NEVER-RESOLVING loadFolderTree
// and asserts the Share click listeners (and the product's own
// data-share-bound marker) are already attached while that promise is still
// pending: red on the pre-fix file, green after the fix.
//   node --test CORE/Dashboard/tests/permissions-share-bind.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const psSrc = fs.readFileSync(path.join(root, 'js', 'permissions.js'), 'utf8');

test('(a) permissions.js parses and exports the init fn', () => {
    assert.match(psSrc, /export async function initializePermissionsPage\(/);
    const classic = psSrc.replace(/^import[^\n]*$/gm, '').replace(/^export /gm, '');
    assert.doesNotThrow(() => new vm.Script(classic, { filename: 'permissions.js' }));
});

test('(b) Share click listeners bind before the first await (never-resolving loadFolderTree)', async () => {
    const body = psSrc.replace(/^import[^\n]*$/gm, '').replace(/^export /gm, '');

    const bound = [];   // "<target>:<event>" for every addEventListener call
    const calls = [];   // every apiCall url attempted
    const byId = {};
    function mkEl(key) {
        const el = { key, dataset: {}, disabled: true, textContent: '', value: '', innerHTML: '' };
        el.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
        el.addEventListener = (type) => { bound.push(key + ':' + type); };
        el.querySelector = () => null;
        el.querySelectorAll = () => [];
        el.appendChild = () => {};
        el.remove = () => {};
        el.style = { borderColor: '' };
        return el;
    }
    const shareBtns = [mkEl('.resource-share-btn'), mkEl('.resource-share-btn'), mkEl('.resource-share-btn')];
    shareBtns[0].dataset.tab = 'tab1';
    shareBtns[1].dataset.tab = 'secrets';
    shareBtns[2].dataset.tab = 'apikeys';

    const sandbox = {
        window: { APP_CONFIG: { API_URL: 'http://x' }, location: { origin: 'http://x' } },
        document: {
            getElementById: (id) => byId[id] || (byId[id] = mkEl('#' + id)),
            querySelector: (sel) => mkEl('q:' + sel),
            querySelectorAll: (sel) => {
                if (sel === '.resource-share-btn') return shareBtns;
                if (sel === '.perm-tab') return [mkEl('.perm-tab')];
                return [];
            },
            createElement: (tag) => mkEl('new:' + tag),
        },
        sessionStorage: { getItem: () => null, setItem: () => {} },
        console: { log() {}, error() {} },
        parseInt, parseFloat, isNaN, Array, Date, String, JSON, Error, Set, Map, setTimeout,
        // api.js imports: fetchApi (as apiCall) + me
        apiCall: (url) => {
            calls.push(String(url));
            // the folder tree NEVER resolves - the init must be stuck at its await
            if (String(url).includes('/assets')) return new Promise(() => {});
            return Promise.resolve([]);
        },
        me: { get: () => Promise.resolve({ id: 1, is_platform_admin: false }) },
        // share-modal.js / resource-permissions.js / permission-templates.js imports
        openShareModal: () => {}, loadResourcePermissions: () => {},
        initializePermissionTemplatesPage: () => {}, refreshTemplatesGrid: () => {},
    };
    vm.createContext(sandbox);
    vm.runInContext(body + '\nglobalThis.__init = initializePermissionsPage;', sandbox);

    // Start init; do NOT await it - it hangs at `await loadFolderTree()`.
    sandbox.__init();
    await new Promise((r) => setTimeout(r, 50));

    // Still stuck at the tree load: the post-tree badge fetch never ran.
    assert.ok(calls.some((u) => u.includes('/assets')), 'tree load never started');
    assert.ok(!calls.some((u) => u.includes('permission-requests/pending')),
        'init COMPLETED - the tree promise resolved, so this is not a before-await proof');

    // The Share listeners (and the data-share-bound marker) are already attached.
    assert.ok(bound.includes('#share-folder-btn:click'),
        'share-folder-btn click listener not bound before the first await');
    assert.strictEqual(bound.filter((x) => x === '.resource-share-btn:click').length, 3,
        'resource Share click listeners not bound before the first await: ' + bound.join(','));
    for (const b of shareBtns) {
        assert.strictEqual(b.dataset.shareBound, '1', 'data-share-bound marker not stamped when bound');
    }
});
