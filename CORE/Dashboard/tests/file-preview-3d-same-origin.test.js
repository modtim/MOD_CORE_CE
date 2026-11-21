// BUG-DASH-3D-GLTF-STREAM — the 3D viewer must fetch model BYTES through the
// existing same-origin authorised stream route (stream_file, tenant-scoped),
// NOT cross-origin from a presigned MinIO host (the owner's B75 retest: "PDF
// good; GLTF/GLB broken"). Static source-text asserts, same shape as
// tests/innerhtml-escape-g2.check.js:16 (reads ../js/file-preview.js with fs);
// non-vacuous because it also asserts the OLD (presigned) form is ABSENT.
//
//   node --test CORE/Dashboard/tests/file-preview-3d-same-origin.test.js
//
// Old-file proof: MCW_OVERRIDE the js copy with the HEAD save -> FAILS.
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'file-preview.js'), 'utf8');

function count(haystack, needle) {
    return haystack.split(needle).length - 1;
}

// Brace-matched function body whose '{' is at `open`.
function blockAt(s, open) {
    assert.strictEqual(s[open], '{', 'marker must sit on an open brace');
    let depth = 0;
    for (let i = open; i < s.length; i++) {
        if (s[i] === '{') depth++;
        else if (s[i] === '}') { depth--; if (!depth) return s.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}

function bodyOf(sigMarker) {
    const at = src.indexOf(sigMarker);
    assert.notEqual(at, -1, sigMarker + ' must exist');
    const open = src.indexOf('{', at + sigMarker.length);
    return blockAt(src, open);
}

const STREAM_FORM = 'assets/${cogId}/stream?token=${encodeURIComponent(token)}';

test('3D bytes come from the same-origin stream route, exactly once', () => {
    assert.strictEqual(count(src, STREAM_FORM), 1,
        'the exact stream form must appear exactly once (m2/m3: wrong route or no token)');
    assert.ok(count(src, 'assets/${cogId}/stream') >= 1,
        'the 3D path must reference /assets/${cogId}/stream');
    assert.strictEqual(count(src, 'getStreamUrl(cogId)'), 2,
        'getStreamUrl defined once + called once (URL built once, passed to the fetch only)');
    const helper = bodyOf('function getStreamUrl(cogId)');
    assert.ok(helper.includes(STREAM_FORM), 'getStreamUrl must build the token-query stream URL');
    assert.ok(!/headers\s*:/.test(helper), 'getStreamUrl must not use an Authorization header (m2/e)');
    assert.ok(!/\/stream\//.test(helper), 'must use the /stream route, not the /stream/{filename} form (m2)');
    assert.ok(!/download\?token=/.test(helper), 'must not build a /download?token= URL (m2)');
});

test('render3DPreview no longer XHRs the URL it is handed', () => {
    const fn = bodyOf('async function render3DPreview(container, url, ext, cogId, boostLighting, useStream = false)');
    assert.ok(!/loader\.load\(/.test(fn),
        'render3DPreview body must contain no loader.load( — the presigned-URL XHR is the bug (m1)');
    assert.ok(!/download_url/.test(fn), 'render3DPreview body must not reference a presigned download_url');
    assert.ok(/await fetch\(getStreamUrl\(cogId\)\)/.test(fn), 'the stream URL must be fetched, not handed to a loader');
    assert.ok(/arrayBuffer\(\)/.test(fn), 'bytes must be read as an ArrayBuffer');
    assert.ok(/!streamResp\.ok/.test(fn), 'a non-2xx stream response must throw into the catch (fail closed)');
});

test('fail-closed no-preview panel is kept', () => {
    assert.ok(/container\.innerHTML = `<div class="text-center text-red-500 py-8 text-sm">Failed to load 3D model<\/div>`;/.test(src),
        'the Failed to load 3D model panel must still be shipped (m4: rethrow instead of the panel)');
    assert.ok(src.includes("console.error('Failed to load 3D model:', err);"),
        "the token-free console.error form must stay");
});

test('no console call interpolates a token or a signed/stream URL', () => {
    const bad = src.split('\n').filter(l => /console\./.test(l) && /token/i.test(l));
    assert.strictEqual(bad.length, 0, 'no console.* line may mention token/?token= (d)');
});

test('token form matches the working PDF path (query, not header)', () => {
    assert.ok(count(src, 'pdf-view?token=${encodeURIComponent(token)}') >= 1,
        'the PDF path (the owner-tested working form) must be unchanged');
    assert.ok(src.includes(STREAM_FORM),
        'the 3D route must mirror the PDF token-query form exactly (e)');
});

test('USDZ / splat / point-cloud callers keep the URL-based loader.load path', () => {
    assert.strictEqual(count(src, "await render3DPreview(container, data.glb_url, 'glb', cogId, true);"), 1,
        'the USDZ-converted GLB caller must stay byte-identical (f)');
    assert.strictEqual(count(src, 'await render3DPreview(container, url, ext, cogId);'), 1,
        'the PLY/splat chooser caller must stay byte-identical (f)');
    assert.strictEqual(count(src, 'await render3DPreview(content, url, ext, item.id, false, true);'), 1,
        'exactly one caller (the model3d dispatch) opts into the stream route');
    assert.ok(/loader\.load\(url, resolve, undefined, reject\);/.test(src),
        'the legacy URL-based load helper must still exist for the out-of-scope paths');
});

test('file still parses as a module', () => {
    const { execFileSync } = require('node:child_process');
    let err = null;
    try { execFileSync(process.execPath, ['--input-type=module', '--check'], { input: src, encoding: 'utf8' }); }
    catch (e) { err = String(e && (e.stderr || e.message)); }
    assert.ok(!err, `file-preview.js not valid module — ${(err || '').split('\n')[0] || 'unknown'}`);
});

console.log('file-preview-3d-same-origin: all checks passed');
