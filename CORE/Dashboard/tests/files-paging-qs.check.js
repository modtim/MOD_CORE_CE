// files-paging-qs.check.js — regression ():
// cogs.get (CORE/Dashboard/js/api.js) must put '?' before the paging query.
// With no parent the base query is empty, so the old code requested
// /v1/assets/limit=50&offset=0 (405 seen in the Dell browser console).
// Same extraction style as files-paging.check.js: the shipped source is
// extracted verbatim and run in node:vm against a stubbed fetch.
//
//   node CORE/Dashboard/tests/files-paging-qs.check.js
//
// Asserts: (a) no parent -> /assets/?limit=50&offset=0;
// (b) parent -> /assets/?parent_id=7&limit=50&offset=0;
// (c) deleted listing -> /assets/?deleted=true&limit=50&offset=0;
// (d) page 2 keeps the '?' (/assets/?limit=50&offset=50);
// (e) no requested URL has the bare form '/assets/limit='.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

// Brace-matched block whose '{' is at `open`.
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
// Statement ending at the next ';'.
function stmtAt(src, marker) {
    const at = src.indexOf(marker);
    if (at < 0) throw new Error(`marker not found: ${marker}`);
    const end = src.indexOf(';', at);
    if (end < 0) throw new Error(`no ';' after ${marker}`);
    return src.slice(at, end + 1);
}
// Function/class declaration: header text, then paren-scan to the body '{'.
function declAt(src, header) {
    const at = src.indexOf(header);
    if (at < 0) throw new Error(`decl not found: ${header}`);
    let i = at + header.length;
    const skipWs = () => { while (i < src.length && /\s/.test(src[i])) i++; };
    skipWs();
    if (src[i] === '(') {
        let depth = 0;
        for (; i < src.length; i++) {
            const ch = src[i];
            if (ch === '(') depth++;
            else if (ch === ')') { depth--; if (depth === 0) { i++; break; } }
        }
        skipWs();
    }
    if (src[i] !== '{') throw new Error(`no body brace after ${header}`);
    return src.slice(at, i) + ' ' + blockAt(src, i);
}

// ---- extract the shipped pieces (verbatim) ----

const COGS_HEADER = 'export const cogs = {';
const cogsAt = apiSrc.indexOf(COGS_HEADER);
if (cogsAt < 0) throw new Error('cogs object not found');
const cogsObj = blockAt(apiSrc, cogsAt + COGS_HEADER.length - 1);
const cogsDecl = 'const cogs = ' + cogsObj;

const shipped = [
    stmtAt(apiSrc, 'const API_BASE_URL ='),
    declAt(apiSrc, 'class ApiError extends Error'),
    stmtAt(apiSrc, 'let isRefreshing ='),
    stmtAt(apiSrc, 'let refreshPromise ='),
    declAt(apiSrc, 'async function fetchApi'),
    cogsDecl,
].join('\n');

let passes = 0;
let failures = 0;
function check(name, cond, extra) {
    if (cond) { passes++; console.log(`ok   ${name}`); }
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? String(extra) : ''); }
}

// ---- run the shipped cogs object in a vm with a stubbed fetch ----

function runCogs(fetchStub) {
    const sandbox = {
        fetch: fetchStub,
        console,
        window: {},
        localStorage: { getItem: () => null, setItem: () => {} },
        sessionStorage: { getItem: () => 'stub-token', setItem: () => {}, clear: () => {} },
    };
    // A top-level `const` in a vm script is script-scoped: expose it explicitly.
    new vm.Script(`${shipped}\n;globalThis.cogs = cogs;`, { filename: 'api.js' }).runInNewContext(sandbox);
    if (!sandbox.cogs) throw new Error('sandbox.cogs undefined — object not exposed');
    return sandbox.cogs;
}

// Page builder: page n serves counts[n] items (last page repeats the last count).
function pageFetch(counts) {
    const urls = [];
    let n = 0;
    const stub = (url) => {
        urls.push(String(url));
        const len = counts[Math.min(n, counts.length - 1)];
        n++;
        const items = [];
        for (let i = 0; i < len; i++) items.push({ id: `p${n}-i${i}`, name: `item${i}` });
        return Promise.resolve({
            status: 200,
            ok: true,
            headers: { get: (k) => k.toLowerCase() === 'content-type' ? 'application/json' : null },
            json: () => Promise.resolve(items),
            clone: () => null,
        });
    };
    return { stub, urls };
}

// The part of a requested URL from '/assets/' on (the API prefix is not under test).
const tailOf = (u) => u.slice(u.indexOf('/assets/'));

(async () => {
    // (a) no parent: first request must carry '?' before limit=.
    {
        const f = pageFetch([0]);
        await runCogs(f.stub).get();
        check('(a) no parent -> /assets/?limit=50&offset=0', f.urls.length === 1 && tailOf(f.urls[0]) === '/assets/?limit=50&offset=0', f.urls);
    }
    // (b) parent folder: '?parent_id=..&limit=..&offset=..'.
    {
        const f = pageFetch([0]);
        await runCogs(f.stub).get(7);
        check('(b) parent 7 -> /assets/?parent_id=7&limit=50&offset=0', f.urls.length === 1 && tailOf(f.urls[0]) === '/assets/?parent_id=7&limit=50&offset=0', f.urls);
    }
    // (c) deleted listing (no parent): '?deleted=true&limit=..&offset=..'.
    {
        const f = pageFetch([0]);
        await runCogs(f.stub).get(null, false, { deleted: true });
        check('(c) deleted -> /assets/?deleted=true&limit=50&offset=0', f.urls.length === 1 && tailOf(f.urls[0]) === '/assets/?deleted=true&limit=50&offset=0', f.urls);
    }
    // (d) full first page -> second page still '?'-prefixed, offset 50.
    {
        const f = pageFetch([50, 0]);
        const out = await runCogs(f.stub).get();
        check('(d) page 2 -> /assets/?limit=50&offset=50', f.urls.length === 2 && tailOf(f.urls[1]) === '/assets/?limit=50&offset=50', f.urls);
        check('(d) 50 items collected across pages', out.length === 50, out.length);
    }
    // (e) no request anywhere uses the malformed bare form.
    {
        const f = pageFetch([50, 50, 3]);
        await runCogs(f.stub).get(12, true);
        const bad = f.urls.filter((u) => /\/assets\/limit=/.test(u));
        check('(e) no bare /assets/limit= URL', bad.length === 0 && f.urls.length === 3, f.urls);
        check('(e) parent + permission info keeps ? and &', tailOf(f.urls[0]) === '/assets/?parent_id=12&include_permission_info=true&limit=50&offset=0', f.urls[0]);
    }

    console.log(`\n${passes} passed, ${failures} failed`);
    process.exit(failures ? 1 : 0);
})().catch((e) => { console.error('FAIL unexpected', e); process.exit(1); });
