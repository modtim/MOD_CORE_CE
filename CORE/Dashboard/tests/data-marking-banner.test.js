// FX.79 p3a — data-marking banner contract for the Dashboard asset
// preview panel (js/data-marking-banner.js).
//
// The module is an ES module, so this extracts its body (from the
// `const API_BASE_URL` state declaration to EOF, with the `export`
// keywords stripped) and runs it in a vm sandbox with tiny DOM/storage
// stubs — same pattern as tests/alert-rules-page.test.js. Then it
// asserts:
//   (a) the label ranking MATCHES CORE/API/services/marking_stamps.py
//       (LABEL_RANK table, custom labels at 40, worst wins, ties by code);
//   (b) the banner wording is the canonical `CONTROLLED: <HEADER> —
//       handle per policy` (em dash) with the LABEL/DISSEMINATION join;
//   (c) fetchMarkingStamp is silent on every failure mode (no token,
//       non-OK, network throw, malformed body) — never throws, returns
//       null, and the preview is never blocked;
//   (d) renderAssetMarkingBanner puts role="note" + aria-label on the
//       banner element, escapes hostile label codes, and clears on null.
//
//   node --test CORE/Dashboard/tests/data-marking-banner.test.js

'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'data-marking-banner.js'), 'utf8');
const start = src.indexOf('const API_BASE_URL');
assert.ok(start >= 0, 'could not locate the data-marking-banner body');
// FX.163 p3b4a — the banner module imports the shared approval helpers, so the
// harness runs approval-pending.js's body in the SAME sandbox first (its
// top-level `function` declarations become globals the banner body can see).
const apSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'approval-pending.js'), 'utf8');
const apStart = apSrc.indexOf('export function approvalIdOf');
assert.ok(apStart >= 0, 'could not locate the approval-pending body');
const apBody = apSrc.slice(apStart);
const body = src.slice(start)
    .replace(/^export /gm, '')
    + '\n;globalThis.__exports__ = { LABEL_RANK, CUSTOM_RANK, rankLabel, '
    + 'worstMarking, markingBannerText, fetchMarkingStamp, '
    + 'renderAssetMarkingBanner, clearAssetMarkingBanner, '
    + 'loadAssetMarkingBanner, clearAssetMarkingLabel, refreshAssetMarkingBanner, '
    + 'bindAssetMarkingClearButtons, CLEAR_BUTTON_CLASS, CLEAR_ERROR_CLASS };\n';
const fullBody = apBody.replace(/^export /gm, '') + '\n' + body;

// A tiny element factory: attributes, textContent, innerHTML (which WIPES
// children, like the real DOM), appendChild, remove(), listeners and
// class/id selector lookup. No innerHTML PARSING anywhere — assigning
// innerHTML just stores a string — so any markup-building in the modules
// under test stays visible as a raw string, never a live node.
function mkEl(tag, attrs = {}) {
    const el = {
        tag,
        _attrs: attrs,
        children: [],
        _text: '',
        _html: '',
        _listeners: {},
        className: '',
        setAttribute: (k, v) => { attrs[k] = v; },
        removeAttribute: (k) => { delete attrs[k]; },
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        appendChild: (c) => { el.children.push(c); return c; },
        remove: () => { if (el._parent) el._parent.children = el._parent.children.filter((c) => c !== el); },
        addEventListener: (type, fn) => { el._listeners[type] = fn; },
        querySelector: (sel) => el.querySelectorAll(sel)[0] || null,
        querySelectorAll: (sel) => _collect(el, sel),
    };
    Object.defineProperty(el, 'textContent', { get: () => el._text, set: (v) => { el._text = v; } });
    Object.defineProperty(el, 'innerHTML', {
        get: () => el._html,
        set: (v) => { el._html = v; el.children = []; },
    });
    return el;
}

function _matches(el, sel) {
    if (sel.startsWith('.')) return String(el._attrs.class ?? '').split(/\s+/).includes(sel.slice(1));
    if (sel.startsWith('#')) return el._attrs.id === sel.slice(1);
    return el.tag === sel;
}

function _collect(root, sel) {
    const out = [];
    const walk = (el) => {
        for (const c of el.children) {
            c._parent = el;
            if (_matches(c, sel)) out.push(c);
            walk(c);
        }
    };
    walk(root);
    return out;
}

function mkBannerEl() {
    return mkEl('div', {});
}

function runSandbox({ token = 'tok', items = [{ label: 'CUI' }], status = 200,
    fetchImpl = null, deleteImpl = null, confirmAnswer = true, banner = mkBannerEl() } = {}) {
    const fetchCalls = [];
    const fetchMock = async (url, opts) => {
        fetchCalls.push({ url, opts });
        if (fetchImpl) return fetchImpl(url, opts);
        if (opts && opts.method === 'DELETE') {
            if (deleteImpl) return deleteImpl(url, opts);
            return { ok: status >= 200 && status < 300, status, json: async () => ({}) };
        }
        const payload = items === null
            ? { bad: true }
            : { items, total: Array.isArray(items) ? items.length : 0 };
        return { ok: status >= 200 && status < 300, status, json: async () => payload };
    };
    const sandbox = {
        window: { APP_CONFIG: { API_URL: 'http://api.test/v1' } },
        fetch: fetchMock,
        confirm: () => confirmAnswer,
        document: {
            getElementById: (id) => (id === 'asset-marking-banner' ? banner : null),
            createElement: (tag) => mkEl(tag),
        },
        console: { log() {}, warn() {}, error() {}, debug() {} },
    };
    sandbox.sessionStorage = {
        _v: token,
        getItem: function (k) { return k === 'accessToken' ? this._v : null; },
    };
    vm.createContext(sandbox);
    vm.runInContext(fullBody, sandbox);
    return Object.assign({}, sandbox.__exports__, sandbox, { banner, fetchCalls });
}

test('ranking matches marking_stamps.py LABEL_RANK; worst wins', () => {
    const { LABEL_RANK, CUSTOM_RANK, worstMarking } = runSandbox();
    // key-by-key: deepStrictEqual trips on cross-realm prototypes (vm)
    const expected = {
        ITAR: 90,
        'CUI//SP-EXPT': 80,
        CUI: 70,
        EXPORT_CONTROLLED: 60,
        EAR99: 55,
        PROPRIETARY: 50,
    };
    assert.deepStrictEqual(Object.keys(LABEL_RANK).sort(), Object.keys(expected).sort());
    for (const [k, v] of Object.entries(expected)) assert.strictEqual(LABEL_RANK[k], v);
    assert.strictEqual(CUSTOM_RANK, 40);
    // worst (highest) rank wins
    assert.strictEqual(
        worstMarking([{ label: 'CUI' }, { label: 'ITAR' }, { label: 'ACME-X' }]).label, 'ITAR');
    assert.strictEqual(
        worstMarking([{ label: 'EAR99' }, { label: 'CUI//SP-EXPT' }]).label, 'CUI//SP-EXPT');
    // custom (40) loses to every system label (>= 50)
    assert.strictEqual(
        worstMarking([{ label: 'PROPRIETARY' }, { label: 'ACME-CUSTOM' }]).label, 'PROPRIETARY');
    // tie between customs broken by label code, like the python sort
    assert.strictEqual(
        worstMarking([{ label: 'ACME-B' }, { label: 'ACME-A' }]).label, 'ACME-A');
    // case-insensitive lookup, stored case preserved
    assert.strictEqual(
        worstMarking([{ label: 'itar' }, { label: 'CUI' }]).label, 'itar');
    // empty / blank rows
    assert.strictEqual(worstMarking([]), null);
    assert.strictEqual(worstMarking(null), null);
    assert.strictEqual(worstMarking([{ label: '  ' }, { label: null }]), null);
});

test('banner wording is the canonical em-dash sentence', () => {
    const { worstMarking, markingBannerText } = runSandbox();
    const s = worstMarking([{ label: 'CUI', dissemination: 'NOFORN' }]);
    assert.strictEqual(s.header, 'CUI/NOFORN');
    assert.strictEqual(markingBannerText(s), 'CONTROLLED: CUI/NOFORN — handle per policy');
    const plain = worstMarking([{ label: 'ITAR', dissemination: null }]);
    assert.strictEqual(markingBannerText(plain), 'CONTROLLED: ITAR — handle per policy');
});

test('fetchMarkingStamp: happy path + silent failure modes', async () => {
    const ok = runSandbox({ items: [{ label: 'CUI', dissemination: null }] });
    const stamp = await ok.fetchMarkingStamp('asset', 'cog-7');
    assert.strictEqual(stamp.label, 'CUI');
    assert.strictEqual(ok.fetchCalls.length, 1);
    assert.strictEqual(ok.fetchCalls[0].url,
        'http://api.test/v1/tenant/data-markings/asset/cog-7');
    assert.match(ok.fetchCalls[0].opts.headers.Authorization, /^Bearer tok$/);

    // non-OK (403 capability / 404 CE carve) → null, silent
    const bad = runSandbox({ status: 403, items: [] });
    assert.strictEqual(await bad.fetchMarkingStamp('asset', 1), null);

    // network throw → null, silent
    const net = runSandbox({ fetchImpl: async () => { throw new Error('down'); } });
    assert.strictEqual(await net.fetchMarkingStamp('asset', 1), null);

    // malformed body (no items) → null, silent
    const mal = runSandbox({ items: null });
    assert.strictEqual(await mal.fetchMarkingStamp('asset', 1), null);

    // no token → null WITHOUT hitting the network
    const noTok = runSandbox({ token: null });
    assert.strictEqual(await noTok.fetchMarkingStamp('asset', 1), null);
    assert.strictEqual(noTok.fetchCalls.length, 0);
});

test('renderAssetMarkingBanner: role=note, aria-label, escaping, clear', async () => {
    const { renderAssetMarkingBanner, clearAssetMarkingBanner,
        fetchMarkingStamp, banner } = runSandbox({ items: [{ label: 'CUI' }] });

    const stamp = await fetchMarkingStamp('asset', 5);
    renderAssetMarkingBanner(stamp);
    assert.strictEqual(banner.getAttribute('role'), 'note');
    assert.strictEqual(banner.getAttribute('aria-label'), 'CONTROLLED: CUI — handle per policy');
    assert.strictEqual(banner.innerHTML, 'CONTROLLED: CUI — handle per policy');
    assert.match(banner.className, /bg-red-900 text-red-50/);

    clearAssetMarkingBanner();
    assert.strictEqual(banner.getAttribute('role'), null);
    assert.strictEqual(banner.getAttribute('aria-label'), null);
    assert.strictEqual(banner.innerHTML, '');
});

test('hostile label codes are escaped in the banner', async () => {
    const { renderAssetMarkingBanner, worstMarking, banner } = runSandbox();
    const stamp = worstMarking([{ label: '<img src=x onerror=alert(1)>' }]);
    renderAssetMarkingBanner(stamp);
    assert.ok(!banner.innerHTML.includes('<img'), 'markup must not leak through');
    assert.ok(banner.innerHTML.includes('&lt;img'), 'code must be HTML-escaped');
});

test('loadAssetMarkingBanner drops stale results after a file switch', async () => {
    // cog-3 carries ITAR, cog-1/cog-2 carry CUI — a late cog-3 result must
    // never paint over whatever is actually previewed.
    const { loadAssetMarkingBanner, clearAssetMarkingBanner, banner } = runSandbox({
        fetchImpl: async (url) => ({
            ok: true,
            status: 200,
            json: async () => ({
                items: [String(url).includes('cog-3') ? { label: 'ITAR' } : { label: 'CUI' }],
                total: 1,
            }),
        }),
    });

    let current = 'cog-1';
    const p1 = loadAssetMarkingBanner('cog-1', () => current === 'cog-1');
    current = 'cog-2'; // user switched files mid-flight
    const p2 = loadAssetMarkingBanner('cog-2', () => current === 'cog-2');
    await Promise.all([p1, p2]);
    // only the still-current cog (cog-2, CUI) applied
    assert.strictEqual(banner.getAttribute('role'), 'note');
    assert.strictEqual(banner.getAttribute('aria-label'),
        'CONTROLLED: CUI — handle per policy');

    // switch away to "nothing previewed" — a late cog-3 (ITAR) result must
    // not paint the panel
    current = null;
    await loadAssetMarkingBanner('cog-3', () => current === 'cog-3');
    assert.strictEqual(banner.getAttribute('aria-label'),
        'CONTROLLED: CUI — handle per policy');

    // closePreview's explicit clear empties it
    clearAssetMarkingBanner();
    assert.strictEqual(banner.getAttribute('role'), null);
});

// ---------------------------------------------------------------------------
// FX.163 p3b4a — the banner's "Clear" button (DELETE
// /v1/tenant/data-markings/{resource_type}/{resource_id}/{label}, maker-
// checker gated: 202 {status:'awaiting_approval', approval_id} while the
// second-approver switch is ON, 204 when the clear really ran).
// ---------------------------------------------------------------------------

function bannerButtons(banner) {
    return banner.children.filter((c) => String(c._attrs.class ?? '').includes('asset-marking-clear-button'));
}

test('Clear button hidden by default and for non-admins', async () => {
    const { renderAssetMarkingBanner, worstMarking, banner } = runSandbox({
        items: [{ label: 'CUI' }, { label: 'ITAR' }],
    });
    const stamp = worstMarking([{ label: 'CUI' }, { label: 'ITAR' }]);

    // caller knows nothing → default false
    renderAssetMarkingBanner(stamp);
    assert.strictEqual(bannerButtons(banner).length, 0);
    // explicit non-admin
    renderAssetMarkingBanner(stamp, { canClear: false });
    assert.strictEqual(bannerButtons(banner).length, 0);

    // loadAssetMarkingBanner without the canClear arg → no buttons
    await runSandbox({ banner }).loadAssetMarkingBanner('cog-1', () => true);
    assert.strictEqual(bannerButtons(banner).length, 0);
});

test('Clear button shown for tenant admin / superadmin, one per label', async () => {
    const { renderAssetMarkingBanner, worstMarking, banner } = runSandbox({
        items: [{ label: 'CUI' }, { label: 'ITAR' }],
    });
    const stamp = worstMarking([{ label: 'CUI' }, { label: 'ITAR' }]);
    renderAssetMarkingBanner(stamp, { canClear: true });
    const buttons = bannerButtons(banner);
    assert.strictEqual(buttons.length, 2);
    assert.deepStrictEqual(buttons.map((b) => b.getAttribute('data-label')), ['CUI', 'ITAR']);
    for (const b of buttons) {
        assert.strictEqual(b.textContent, 'Clear');
        assert.strictEqual(b.tag, 'button');
    }
    // hostile label reaches the DOM only through setAttribute/textContent
    const hostile = worstMarking([{ label: '<img src=x onerror=alert(1)>' }]);
    renderAssetMarkingBanner(hostile, { canClear: true });
    const hBtn = bannerButtons(banner)[0];
    // the raw code is stored verbatim via setAttribute (no markup parsing) and
    // no element from it ever becomes a live node
    assert.strictEqual(hBtn.getAttribute('data-label'), '<img src=x onerror=alert(1)>');
    assert.strictEqual(hBtn.textContent, 'Clear');
    assert.strictEqual(banner.children.filter((c) => c.tag === 'img').length, 0);
});

test('202 awaiting_approval: notice shown, marking kept, no re-fetch', async () => {
    const { renderAssetMarkingBanner, clearAssetMarkingLabel, worstMarking, banner, fetchCalls } = runSandbox({
        items: [{ label: 'CUI' }],
        deleteImpl: async () => ({ ok: true, status: 202, json: async () => ({ status: 'awaiting_approval', approval_id: 77 }) }),
    });
    renderAssetMarkingBanner(worstMarking([{ label: 'CUI' }]), { canClear: true });
    const before = banner.getAttribute('aria-label');
    assert.strictEqual(before, 'CONTROLLED: CUI — handle per policy');

    const out = await clearAssetMarkingLabel('asset', 'cog-9', 'CUI', { confirm: true });
    assert.strictEqual(out.awaiting, true);
    // exactly one network call: the DELETE. No re-fetch, so the banner still
    // advertises the marking that is still live.
    assert.strictEqual(fetchCalls.length, 1);
    assert.strictEqual(fetchCalls[0].opts.method, 'DELETE');
    assert.match(fetchCalls[0].url, /\/tenant\/data-markings\/asset\/cog-9\/CUI$/);

    const notice = banner.children.find((c) => c._attrs.id === 'awaiting-approval-notice');
    assert.ok(notice, 'pending notice must be rendered');
    assert.strictEqual(notice.getAttribute('role'), 'note');
    assert.strictEqual(notice.getAttribute('aria-live'), 'polite');
    assert.strictEqual(notice.getAttribute('aria-label'),
        'Clear data marking needs a second approver. Request #77 is waiting in Security › Pending approvals');
    assert.strictEqual(banner.getAttribute('aria-label'), before);
});

test('204 clear: notice cleared, banner re-fetched/re-rendered', async () => {
    let gets = 0;
    const { loadAssetMarkingBanner, bindAssetMarkingClearButtons, refreshAssetMarkingBanner,
        worstMarking, banner, fetchCalls } = runSandbox({
        items: [{ label: 'ITAR' }, { label: 'CUI' }],
        fetchImpl: async (url, opts) => {
            if (opts && opts.method === 'DELETE') return { ok: true, status: 204, json: async () => null };
            gets += 1;
            // the second GET (after the clear) sees ITAR gone
            const items = gets === 1 ? [{ label: 'ITAR' }, { label: 'CUI' }] : [{ label: 'CUI' }];
            return { ok: true, status: 200, json: async () => ({ items, total: items.length }) };
        },
    });
    let current = 'cog-9';
    await loadAssetMarkingBanner('cog-9', () => current === 'cog-9', true);
    bindAssetMarkingClearButtons('asset', 'cog-9', () => current === 'cog-9', true);
    const btn = bannerButtons(banner).find((b) => b.getAttribute('data-label') === 'ITAR');
    assert.ok(btn);

    await btn._listeners.click();
    // load GET, then the click's DELETE, then the re-render GET
    assert.strictEqual(fetchCalls.length, 3);
    assert.strictEqual(fetchCalls[1].opts.method, 'DELETE');
    assert.match(fetchCalls[1].url, /\/tenant\/data-markings\/asset\/cog-9\/ITAR$/);
    assert.notStrictEqual(fetchCalls[2].opts && fetchCalls[2].opts.method, 'DELETE');
    assert.match(fetchCalls[2].url, /\/tenant\/data-markings\/asset\/cog-9$/);
    // the second approver had nothing pending → notice gone, banner refreshed
    assert.strictEqual(banner.children.filter((c) => c._attrs.id === 'awaiting-approval-notice').length, 0);
    assert.strictEqual(banner.getAttribute('aria-label'), 'CONTROLLED: CUI — handle per policy');
    // the cleared label is gone, the survivor keeps its Clear button
    assert.deepStrictEqual(bannerButtons(banner).map((b) => b.getAttribute('data-label')), ['CUI']);
});

test('confirm() cancel: no DELETE, marking untouched', async () => {
    const { renderAssetMarkingBanner, clearAssetMarkingLabel, worstMarking, banner, fetchCalls } = runSandbox({
        items: [{ label: 'CUI' }], confirmAnswer: false,
    });
    renderAssetMarkingBanner(worstMarking([{ label: 'CUI' }]), { canClear: true });
    const out = await clearAssetMarkingLabel('asset', 'cog-9', 'CUI', { confirm: false });
    assert.strictEqual(out, null);
    assert.strictEqual(fetchCalls.length, 0);
    assert.strictEqual(banner.getAttribute('aria-label'), 'CONTROLLED: CUI — handle per policy');
});

test('clear errors are inline textContent only', async () => {
    const { renderAssetMarkingBanner, clearAssetMarkingLabel, worstMarking, banner, fetchCalls } = runSandbox({
        items: [{ label: 'CUI' }],
        deleteImpl: async () => { throw new Error('network down'); },
    });
    renderAssetMarkingBanner(worstMarking([{ label: 'CUI' }]), { canClear: true });
    const out = await clearAssetMarkingLabel('asset', 'cog-9', 'CUI', { confirm: true });
    assert.strictEqual(out.ok, false);
    const err = banner.children.find((c) => String(c._attrs.class ?? '').includes('asset-marking-clear-error'));
    assert.ok(err);
    assert.match(err.textContent, /Could not clear "CUI"/);
    assert.ok(!banner.innerHTML.includes('<p'), 'no markup built');
    // a later success clears the stale error slot
    const ok = runSandbox({ items: [{ label: 'CUI' }] });
    ok.renderAssetMarkingBanner(ok.worstMarking([{ label: 'CUI' }]));
    assert.strictEqual(ok.banner.children.filter((c) => String(c._attrs.class ?? '').includes('asset-marking-clear-error')).length, 0);
});
