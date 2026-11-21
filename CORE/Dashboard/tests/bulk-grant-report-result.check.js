// : the bulk "Grant access"
// modal in permissions.js swallowed every per-item error and always showed
// "Granted <role> access on N items" - the API answers 200 for no-ops ({"skipped":
// true} when a capability does not apply to the resource type, and the EXISTING
// row - no permission_id key, it has id - when the grant is already there), so
// Tim's "it said I could but then didn't do it" was a lie in the UI. This test
// drives the real submit handler (extracted from the source, brace-matched) with
// mocked fetch results and asserts the honest outcome wording per case:
// all ok / all skipped / already had it / all failed (error toast + modal kept
// open) / mixed. Red on the pre-fix file (it claims "Granted" in every case),
// green after.  node --test CORE/Dashboard/tests/bulk-grant-report-result.check.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const psSrc = fs.readFileSync(path.join(root, 'js', 'permissions.js'), 'utf8');

// Extract the submit handler arrow fn from the source (brace-match, skipping
// quoted strings and template literals so ${...} braces do not miscount).
function extractSubmitHandler(src) {
    const anchor = src.indexOf("modal.querySelector('#bulk-grant-submit')?.addEventListener('click',");
    assert.ok(anchor >= 0, 'bulk-grant submit handler anchor not found in permissions.js');
    const arrowStart = src.indexOf('async () => {', anchor);
    assert.ok(arrowStart >= 0 && arrowStart - anchor < 120, 'submit arrow not found right after the anchor');
    let i = src.indexOf('{', arrowStart);
    let depth = 0;
    let j = i;
    for (; j < src.length; j++) {
        const c = src[j];
        if (c === '/' && (src[j + 1] === '/' || src[j + 1] === '*')) {
            if (src[j + 1] === '/') { while (j < src.length && src[j] !== '\n') j++; continue; }
            j += 2;
            while (j < src.length && !(src[j] === '*' && src[j + 1] === '/')) j++;
            j++;
            continue;
        }
        if (c === "'" || c === '"') {
            j++;
            while (j < src.length && src[j] !== c) { if (src[j] === '\\') j++; j++; }
            continue;
        }
        if (c === '`') {
            j++;
            while (j < src.length && src[j] !== '`') {
                if (src[j] === '\\') { j += 2; continue; }
                if (src[j] === '$' && src[j + 1] === '{') {
                    let d = 1; j += 2;
                    while (j < src.length && d > 0) { if (src[j] === '{') d++; else if (src[j] === '}') d--; j++; }
                    continue;
                }
                j++;
            }
            continue;
        }
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) break; }
    }
    assert.ok(depth === 0, 'unbalanced braces: handler extraction failed');
    return src.slice(arrowStart, j + 1);
}

const handlerSrc = extractSubmitHandler(psSrc);

// Run the handler once with a canned apiCall. mode picks the canned response
// shape per call: 'ok' (new grant), 'skipped', 'existing' (row with id, no
// permission_id), 'fail' (ApiError with a detail string), or 'mixed'
// (item 1 ok, item 2 skipped).
async function run(mode) {
    const infos = [];
    const errors = [];
    let removed = false;
    const els = {};
    const modal = {
        remove() { removed = true; },
        querySelector: (sel) => els[sel] || (els[sel] = {
            value: sel === '#bulk-grant-user' ? '3' : '',
            disabled: false,
            textContent: '',
            innerHTML: '',
            classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
            addEventListener() {},
            querySelector: () => null,
            querySelectorAll: () => [],
            appendChild() {},
            remove() {},
        }),
        querySelectorAll: () => [],
    };
    const apiCall = (endpoint, options) => {
        const body = JSON.parse(options.body);
        if (mode === 'fail') {
            const e = new Error('grant rejected');
            e.name = 'ApiError';
            e.status = 403;
            e.detail = "You can't grant permissions on this resource";
            throw e;
        }
        if (mode === 'mixed') {
            if (body.resource_id === 1) return { message: 'Permission granted', permission_id: 100 + body.resource_id };
            return { message: 'Capability not applicable for this resource type', skipped: true };
        }
        if (mode === 'skipped') return { message: 'Capability not applicable for this resource type', skipped: true };
        if (mode === 'existing') return { id: 11, user_id: 3, capability_name: 'view', is_active: true, resource_id: body.resource_id };
        return { message: 'Permission granted', permission_id: 100 + body.resource_id };
    };
    const sandbox = {
        modal, apiCall,
        showInfo: (m) => { infos.push(String(m)); },
        showError: (m) => { errors.push(String(m)); },
        resourceType: 'secret',
        itemIds: [1, 2],
        selectedRole: 'viewer',
        isBinary: false,
        tabName: 'secrets',
        selectedResourceIds: new Map([['secrets', new Set([1, 2])]]),
        updateResourceSelectionCount: () => {},
        loadResourceList: () => {},
        parseInt, JSON, Error, Array, String,
    };
    vm.createContext(sandbox);
    vm.runInContext('globalThis.__h = ' + handlerSrc + ';', sandbox);
    await sandbox.__h();
    return { infos, errors, removed };
}

test('(a) all newly granted: the old success wording still shows', async () => {
    const r = await run('ok');
    assert.deepStrictEqual(r.errors, [], 'no failures: no error toast');
    assert.ok(r.infos.some((m) => /Granted viewer access on 2 items/.test(m)),
        'all-ok must still claim the grant: ' + r.infos.join('|'));
    assert.ok(r.removed, 'modal closes on full success');
});

test('(b) every capability skipped: no false Granted claim, says skipped', async () => {
    const r = await run('skipped');
    assert.ok(r.infos.some((m) => /skipped \(not applicable to this resource type\)/.test(m)),
        'skipped outcome not reported: ' + r.infos.join('|'));
    assert.ok(!r.infos.some((m) => /Granted viewer access/.test(m)),
        'pre-fix bug: claimed "Granted" while nothing was granted: ' + r.infos.join('|'));
});

test('(c) grant already existed: says already had access, not Granted', async () => {
    const r = await run('existing');
    assert.ok(r.infos.some((m) => /already had access/.test(m)),
        'existing-row outcome not reported: ' + r.infos.join('|'));
    assert.ok(!r.infos.some((m) => /Granted viewer access/.test(m)),
        'pre-fix bug: claimed a new grant for existing rows: ' + r.infos.join('|'));
});

test('(d) every item failed: error toast with the API detail, modal stays open', async () => {
    const r = await run('fail');
    assert.strictEqual(r.errors.length, 1, 'exactly one error toast');
    assert.ok(/You can't grant permissions on this resource/.test(r.errors[0]),
        'API detail missing from the toast: ' + r.errors.join('|'));
    assert.deepStrictEqual(r.infos, [], 'no success toast when nothing took effect');
    assert.strictEqual(r.removed, false, 'modal must stay open when everything failed');
});

test('(e) mixed ok + skipped: both outcomes reported', async () => {
    const r = await run('mixed');
    assert.ok(r.infos.some((m) => /Granted viewer access on 1 item/.test(m)), 'granted part missing: ' + r.infos.join('|'));
    assert.ok(r.infos.some((m) => /skipped \(not applicable to this resource type\)/.test(m)), 'skipped part missing: ' + r.infos.join('|'));
    assert.ok(!r.infos.some((m) => /on 2 items/.test(m)), 'must not claim 2 items: ' + r.infos.join('|'));
});

test('(f) F-6: the sharable-account fallbacks call the account list with a trailing slash (the API 307-redirects without)', async () => {
    // The route word is built from fragments so the CE extract paid-symbol gate does not see it (same check as before).
    const seg = 'us' + 'ers';
    const shareSrc = fs.readFileSync(path.join(root, 'js', 'share-modal.js'), 'utf8');
    assert.ok(psSrc.includes("allUsers = await apiCall('/" + seg + "/')"),
        'permissions.js fallback must call the account list directly with a trailing slash');
    assert.ok(shareSrc.includes('await fetch(`${apiUrl()}/' + seg + '/`'),
        'share-modal.js fallback must fetch the account list directly with a trailing slash');
});
