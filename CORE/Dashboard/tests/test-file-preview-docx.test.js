// BUG-DASH-DOCX-PREVIEW (B75) — client-side .docx preview on the Dashboard
// file-preview panel. file-preview.js is an ES module, so this script slices
// the module's ext/MIME/categorize region and its DOCX region out of the
// source, runs them in a vm sandbox with a tiny DOM + fetch stub, and asserts:
//   (a) MIME_EXT_MAP resolves the OOXML wordprocessingml MIME to 'docx';
//   (b) DOCX_EXTS + categorize route 'docx'/'doc' to the 'docx' category
//       (the root cause: docx used to fall through to 'other' -> blank panel);
//   (c) the dispatch renders an iframe whose srcdoc is SANITIZED and whose
//       sandbox attribute never grants allow-scripts;
//   (d) sanitizer: <script> blocks, on* handlers, javascript:/data:/file: URLs
//       and non-allow-listed tags are stripped while the text survives;
//   (e) fail-closed: fetch failure, empty file, >2 MB file, mammoth missing
//       and conversion throw all fall back to the no-preview panel;
//   (f) the only network call is the authorized /assets/{id}/download?token=
//       route and the lib URL is the vendored /vendor/mammoth.browser.min.js.
//
//   node --test CORE/Dashboard/tests/test-file-preview-docx.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'file-preview.js'), 'utf8');

// Region extraction is tolerant on the HEAD tree (no DOCX region at all): the
// subtests then fail on their own assertions instead of the whole file dying at
// load, which is what the old-file proof and mutants m1/m2 need.
const docxStart = src.indexOf('const MAMMOTH_LIB_URL');
const docxEnd = docxStart >= 0 ? src.indexOf('// CAD preview — STEP', docxStart) : -1;
const docxBlock = (docxStart >= 0 && docxEnd > docxStart)
    ? src.slice(docxStart, docxEnd).replace(/^export /gm, '')
    : 'var MAMMOTH_LIB_URL = null; var DOCX_MAX_BYTES = null; var categorize = null;'
    + ' var resolveExt = null; var renderDocxPreview = null; var _sanitizeDocxHtml = null;\n';

const catStart = src.indexOf('const IMAGE_EXTS = new Set(');
assert.ok(catStart >= 0, 'could not locate IMAGE_EXTS');
const catEnd = src.indexOf('function formatBytes(bytes)');
assert.ok(catEnd > catStart, 'could not locate categorize end');
const catBlock = src.slice(catStart, catEnd).replace(/^export /gm, '');

const DOCX_MIME = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

function mkEl(tag) {
    const el = { tag, attrs: {}, children: [], style: {}, textContent: '' };
    el.setAttribute = (k, v) => { el.attrs[k] = v; };
    el.getAttribute = (k) => el.attrs[k];
    el.appendChild = (child) => { el.children.push(child); if (tag === 'head' && child.tag === 'script' && child.onload) child.onload(); };
    return el;
}

function run(opts) {
    const els = {};
    const calls = { fetch: 0, other: 0 };
    const container = mkEl('div');
    const head = mkEl('head');

    const document = {
        head,
        createElement: (tag) => {
            const el = mkEl(tag);
            if (tag === 'iframe') els.iframe = el;
            return el;
        },
    };

    const sandbox = {
        document,
        API_BASE_URL: 'http://api.test',
        sessionStorage: { getItem: () => 'tok-abc' },
        window: {},
        renderOtherPreview: (c, filename, ext) => {
            calls.other++;
            els.other = { filename, ext };
        },
        console: { error: () => {} },
    };

    sandbox.fetch = (url, stubOpts) => {
        calls.fetch++;
        sandbox.lastFetchUrl = url;
        sandbox.lastHeaders = stubOpts && stubOpts.headers;
        const f = opts.fetch;
        if (!f) return { ok: true, arrayBuffer: () => Buffer.from([1, 2, 3]) };
        return f(url, stubOpts);
    };

    const code = catBlock + '\n' + docxBlock + '\n'
        + '({ categorize, resolveExt, renderDocxPreview, sanitize: _sanitizeDocxHtml, '
        + 'MAMMOTH_LIB_URL, DOCX_MAX_BYTES })\n';
    const ctx = vm.createContext(sandbox);
    const fns = new vm.Script(code).runInContext(ctx);
    return { fns, els, calls, container, sandbox };
}

test.describe('file-preview.js DOCX preview (B75)', () => {
    test('MIME_EXT_MAP resolves the OOXML docx MIME', async () => {
        const { fns } = run({});
        assert.strictEqual(fns.resolveExt({ name: 'report', mime_type: DOCX_MIME }), 'docx',
            'docx MIME must map to the docx ext');
        assert.strictEqual(fns.resolveExt({ name: 'legacy.doc', mime_type: 'application/msword' }), 'doc');
    });

    test('categorize routes docx/doc to the docx category', async () => {
        const { fns } = run({});
        assert.strictEqual(fns.categorize('docx'), 'docx', 'docx must not fall through to other');
        assert.strictEqual(fns.categorize('doc'), 'docx');
        assert.strictEqual(fns.categorize('pdf'), 'pdf', 'pdf routing unchanged');
        assert.strictEqual(fns.categorize('xyzzy'), 'other', 'unknown ext still other');
    });

    test('golden path: sanitized sandboxed iframe', async () => {
        const hostile = '<p>Amp&amp;Co</p><script>alert(1)</script>'
            + '<img src="x" onerror="alert(1)"/><a href="javascript:alert(1)">link</a>'
            + '<iframe src="evil"></iframe><marquee>tick</marquee>';
        const r = run({ fetch: () => ({ ok: true, arrayBuffer: () => Buffer.from('PKfakezip') }) });
        r.sandbox.window.mammoth = {
            convertToHtml: () => ({ value: hostile }),
            images: { imgElement: () => '' },
        };
        await r.fns.renderDocxPreview(r.container, 'unused', 'report.docx', 'cog-7');
        const iframe = r.els.iframe;
        assert.ok(iframe, 'docx preview must render an iframe');
        assert.strictEqual(iframe.attrs.sandbox, 'allow-same-origin', 'no allow-scripts');
        const srcdoc = iframe.attrs.srcdoc;
        assert.ok(!/<script/i.test(srcdoc), 'no script tag in srcdoc');
        assert.ok(!/onerror/i.test(srcdoc), 'no inline handler');
        assert.ok(!/javascript:/i.test(srcdoc), 'no javascript: url');
        assert.ok(!/<iframe/i.test(srcdoc), 'no nested iframe');
        assert.ok(srcdoc.includes('Amp&amp;Co'), 'text survives');
        assert.ok(srcdoc.includes('>link<'), 'link text survives');
        assert.ok(r.sandbox.lastFetchUrl.includes('/assets/cog-7/download?token='),
            'authorized asset route with token');
        assert.strictEqual(r.calls.other, 0, 'no fallback on the golden path');
    });

    test('sanitizer strips hostile markup, keeps text', async () => {
        const { fns } = run({});
        const out = fns.sanitize('<p>hi<script>bad()</script></p>'
            + '<b onmouseover="x()">bold</b>'
            + '<a href="javascript:evil()">j</a>'
            + '<a href="data:text/html;base64,AA">d</a>'
            + '<object data="x"/>gone</object>'
            + '<img src="file:///etc/passwd"/>');
        assert.ok(!/<script/i.test(out), 'script removed');
        assert.ok(!/onmouseover/i.test(out), 'handler attr removed');
        assert.ok(!/javascript:/i.test(out), 'javascript url removed');
        assert.ok(!/data:text\/html/i.test(out), 'data url removed');
        assert.ok(!/<object/i.test(out), 'disallowed tag removed');
        assert.ok(!/file:\/\//i.test(out), 'file url removed');
        assert.ok(out.includes('hi'), 'text kept');
        assert.ok(out.includes('bold'), 'allowed tag kept');
        assert.ok(out.includes('gone'), 'text of removed tag kept');
    });

    test('conversion error falls back, never throws', async () => {
        const r = run({ fetch: () => ({ ok: true, arrayBuffer: () => Buffer.from('PK') }) });
        r.sandbox.window.mammoth = {
            convertToHtml: () => { throw new Error('corrupt central directory'); },
            images: { imgElement: () => '' },
        };
        await r.fns.renderDocxPreview(r.container, 'unused', 'broken.docx', 'cog-8');
        assert.strictEqual(r.calls.other, 1, 'fell back to the no-preview panel');
        assert.ok(!r.els.iframe, 'no iframe on failure');
    });

    test('oversize + empty + fetch failure all fail closed', async () => {
        const big = run({ fetch: () => ({ ok: true, arrayBuffer: () => Buffer.alloc(3 * 1024 * 1024) }) });
        await big.fns.renderDocxPreview(big.container, 'u', 'big.docx', 'cog-9');
        assert.strictEqual(big.calls.other, 1, '2 MB cap enforced before parsing');
        assert.ok(!big.els.iframe, 'no iframe for oversize');

        const empty = run({ fetch: () => ({ ok: true, arrayBuffer: () => Buffer.alloc(0) }) });
        await empty.fns.renderDocxPreview(empty.container, 'u', 'empty.docx', 'cog-10');
        assert.strictEqual(empty.calls.other, 1, 'empty file falls back');

        const dead = run({ fetch: () => ({ ok: false, status: 500 }) });
        await dead.fns.renderDocxPreview(dead.container, 'u', 'gone.docx', 'cog-11');
        assert.strictEqual(dead.calls.other, 1, 'HTTP failure falls back');
        assert.strictEqual(dead.calls.fetch, 1, 'exactly one fetch');
    });

    test('missing mammoth library falls back', async () => {
        const r = run({ fetch: () => ({ ok: true, arrayBuffer: () => Buffer.from('PK') }) });
        await r.fns.renderDocxPreview(r.container, 'u', 'nolib.docx', 'cog-12');
        assert.strictEqual(r.calls.other, 1, 'no library -> no preview');
        assert.ok(!r.els.iframe, 'no iframe without the library');
        assert.strictEqual(r.fns.MAMMOTH_LIB_URL, '/vendor/mammoth.browser.min.js', 'vendored lib, no CDN');
        assert.strictEqual(r.fns.DOCX_MAX_BYTES, 2 * 1024 * 1024, '2 MB cap constant');
    });
});
