// innerhtml-escape-g3.check.js — static check for the G3 innerHTML escaping fix
// in js/permission-templates.js, js/tenant-registry.js and js/ai-chat.js.
//
// Sibling convention (innerhtml-escape-g2.check.js,
// persistent-stacks-compose-error-escape.check.js): the audited sources are read
// from the RUN TREE via ROOT = path.join(__dirname, '..') — never from absolute
// /tmp paths and never cwd-relative. The old-file proof is the MCW_OVERRIDE run
// (the old copies swapped into the run tree), not a second set of reads.
//
// Per audited site:
//   1) the escaped form is present in the innerHTML template.
//   2) the raw (unescaped) form is ABSENT in that same template — non-vacuous:
//      it fails on the pre-fix copies.
//   3) the file's escape helper exists.
//   4) the helper is extracted from the source text and evaluated on an
//      <img src=x onerror=alert(1)> payload through the site's own template:
//      the markup must survive as entities, no raw '<img' may reach innerHTML.
//   5) the file still parses as an ES module (all three use import/export).
'use strict';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
function read(rel) { return readFileSync(join(ROOT, rel), 'utf8'); }

const SRC = {
    perm: read('js/permission-templates.js'),
    tenant: read('js/tenant-registry.js'),
    chat: read('js/ai-chat.js'),
};

// ---------------------------------------------------------------------------
// Template scanner: the innerHTML template literal that contains `marker`.
// Handles ${...} interpolation and nested template literals.
// ---------------------------------------------------------------------------
function templateEnd(src, open) {
    let i = open + 1;
    let brace = 0;
    while (i < src.length) {
        const c = src[i];
        if (c === '\\') { i += 2; continue; }
        if (brace === 0) {
            if (c === '`') return i;
            if (c === '$' && src[i + 1] === '{') { brace = 1; i += 2; continue; }
            i += 1;
            continue;
        }
        if (c === '`') { const e = templateEnd(src, i); if (e < 0) return -1; i = e + 1; continue; }
        if (c === '{') { brace += 1; i += 1; continue; }
        if (c === '}') { brace -= 1; i += 1; continue; }
        i += 1;
    }
    return -1;
}
function siteTemplate(src, marker) {
    const re = /innerHTML\s*=\s*/g;
    const hits = [];
    let m;
    while ((m = re.exec(src))) {
        const bt = src.indexOf('`', m.index);
        if (bt < 0 || bt > m.index + 40) continue;
        const end = templateEnd(src, bt);
        if (end > bt) {
            const tpl = src.slice(bt, end + 1);
            if (tpl.includes(marker)) hits.push(tpl);
        }
    }
    return hits.length === 1 ? hits[0] : null;
}

// ---------------------------------------------------------------------------
// Helper extraction (brace-matched from `function escapeHtml(`)
// ---------------------------------------------------------------------------
function helperSrc(src) {
    const m = /function\s+escapeHtml\s*\(/.exec(src);
    if (!m) return null;
    const open = src.indexOf('{', m.index);
    if (open < 0) return null;
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth += 1;
        else if (src[i] === '}') { depth -= 1; if (depth === 0) return src.slice(m.index, i + 1); }
    }
    return null;
}

// Faithful model of the browser contract ai-chat's helper relies on:
// assigning to div.textContent and reading div.innerHTML escapes markup.
const SHIM = `const document = { createElement: function (t) {
    const d = {}; let stored = '';
    Object.defineProperty(d, 'textContent', { set: function (v) { stored = String(v); }, get: function () { return stored; } });
    Object.defineProperty(d, 'innerHTML', { get: function () { return stored.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); } });
    return d;
} };`;

const EVIL = '<img src=x onerror=alert(1)>';

const SITES = [
    { label: 'permission-templates.js', src: SRC.perm, file: 'js/permission-templates.js',
      marker: 'Error loading templates: ', var: 'error',
      escaped: 'escapeHtml(error.message)', raw: /\$\{\s*error\.message\s*\}/ },
    { label: 'tenant-registry.js', src: SRC.tenant, file: 'js/tenant-registry.js',
      marker: 'Failed to load scan detail: ', var: 'e',
      escaped: 'escapeHtml(e?.message || e)', raw: /\$\{\s*e\??\.message\s*\|\|\s*e\s*\}/ },
    { label: 'ai-chat.js', src: SRC.chat, file: 'js/ai-chat.js',
      marker: 'No AI workers available. ', var: 'status',
      escaped: 'escapeHtml(status.message)', raw: /\$\{\s*status\.message\s*\}/ },
];

for (const s of SITES) {
    const tpl = siteTemplate(s.src, s.marker);
    assert.ok(tpl, `locate the innerHTML template for ${s.label}`);

    test(`${s.label}: escaped form present`, () => {
        assert.ok(tpl.includes(s.escaped),
            `${s.label} site must wrap the error string in escapeHtml (${s.escaped})`);
    });

    test(`${s.label}: raw form absent in that innerHTML template`, () => {
        assert.ok(!s.raw.test(tpl),
            `${s.label} innerHTML template must not interpolate the raw ${'$'}{...message...}`);
    });

    test(`${s.label}: defines an escapeHtml helper`, () => {
        assert.ok(/function\s+escapeHtml\s*\(/.test(s.src),
            `${s.label} must define an escapeHtml helper`);
    });

    const helper = helperSrc(s.src);
    test(`${s.label}: payload renders as entities`, () => {
        assert.ok(helper, `${s.label} helper must be extractable`);
        const out = new Function(s.var, helper + '\n' + SHIM + '\nreturn ' + tpl + ';')({ message: EVIL });
        assert.ok(out.includes('&lt;img src=x onerror=alert(1)&gt;'),
            `${s.label}: the payload must survive as escaped entities`);
    });

    test(`${s.label}: no raw <img reaches innerHTML`, () => {
        assert.ok(helper, `${s.label} helper must be extractable`);
        const out = new Function(s.var, helper + '\n' + SHIM + '\nreturn ' + tpl + ';')({ message: EVIL });
        assert.ok(!/<img/i.test(out),
            `${s.label}: no raw <img element may be produced from the payload`);
    });

    test(`${s.label}: still parses as an ES module`, () => {
        let err = null;
        try {
            execFileSync(process.execPath, ['--input-type=module', '--check'],
                { input: s.src, encoding: 'utf8' });
        } catch (e) { err = String(e && (e.stderr || e.message)); }
        assert.ok(!err, `${s.label} not valid module — ${(err || '').split('\n')[0] || 'unknown'}`);
    });
}

console.log('innerhtml-escape-g3: all checks passed');
