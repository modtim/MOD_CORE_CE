// files-paging.check.js — regression: cogs.get paginates the assets list
// (CORE/Dashboard/js/api.js). Style: ispaid-failclosed.check.js — the shipped
// source text is EXTRACTED VERBATIM (brace-matched, no fixed line numbers, no
// 'restore:' marker) and run in node:vm against a stubbed fetch, so the
// assertions test the shipped source, not a hand-copied one.
//
//   node CORE/Dashboard/tests/files-paging.check.js
//
// Asserts: (a) 107 items over 3 pages (50/50/7) -> 107 results, offsets 0/50/100;
// (b) a short first page -> exactly 1 fetch; (c) a fetch error -> the same
// rejection the caller used to see (no swallow, no partial list).
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

function makeSandbox(fetchStub) {
    return {
        fetch: fetchStub,
        console,
        window: {},
        localStorage: { getItem: () => null, setItem: () => {} },
        sessionStorage: { getItem: () => 'stub-token', setItem: () => {}, clear: () => {} },
    };
}

function runCogs(fetchStub) {
    const sandbox = makeSandbox(fetchStub);
    // Expose the object on the sandbox: a top-level `const` in a vm script is
    // script-scoped, so it never lands on the global object by itself.
    new vm.Script(`${shipped}\n;globalThis.cogs = cogs;`, { filename: 'api.js' }).runInNewContext(sandbox);
    if (!sandbox.cogs) throw new Error('sandbox.cogs undefined — object not exposed');
    return sandbox.cogs;
}

// Page builder: page n serves counts[n] items (last page repeats the last count).
function pageFetch(counts) {
    const urls = [];
    let n = 0;
    const stub = (url) => {
        urls.push(url);
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

(async () => {
    // (a) 107 items across 3 pages (50 + 50 + 7).
    {
        const p = pageFetch([50, 50, 7]);
        const cogs = runCogs(p.stub);
        const items = await cogs.get(123, true);
        check('a1 107 items across 3 pages', items.length === 107, `got ${items.length}`);
        check('a2 exactly 3 fetches', p.urls.length === 3, `got ${p.urls.length}`);
        check('a3 offsets requested 0/50/100',
            p.urls.length === 3 && p.urls[0].includes('offset=0') && p.urls[1].includes('offset=50') && p.urls[2].includes('offset=100'),
            p.urls.join(' | '));
        check('a4 every page asked for limit=50', p.urls.every((u) => u.includes('limit=50')), p.urls.join(' | '));
    }

    // (b) short first page -> exactly 1 fetch.
    {
        const p = pageFetch([37]);
        const cogs = runCogs(p.stub);
        const items = await cogs.get(456, true);
        check('b1 short page returns its 37 items', items.length === 37, `got ${items.length}`);
        check('b2 exactly 1 fetch', p.urls.length === 1, `got ${p.urls.length}`);
    }
    // (b2) empty first page -> exactly 1 fetch, no second call.
    {
        const p = pageFetch([0]);
        const cogs = runCogs(p.stub);
        const items = await cogs.get(789);
        check('b3 empty listing -> 0 items', items.length === 0, `got ${items.length}`);
        check('b4 empty listing -> 1 fetch', p.urls.length === 1, `got ${p.urls.length}`);
    }

    // (c) fetch error -> same rejection as before (propagates, not swallowed).
    {
        const boom = new TypeError('network down');
        let calls = 0;
        const cogs = runCogs((url) => { calls++; return Promise.reject(boom); });
        let err = null;
        try { await cogs.get(123, true); } catch (e) { err = e; }
        check('c1 fetch error still propagates', err !== null, 'get() resolved despite a rejecting fetch');
        check('c2 same error object as the fetch threw', err === boom, `${err && err.message}`);
        check('c3 same error type/message as before', err instanceof TypeError && err.message === 'network down', `${err && err.message}`);
        check('c4 no retry storm on error', calls === 1, `got ${calls}`);
    }

    console.log(`counts: ${passes} ok / ${failures} fail`);
    process.exit(failures ? 1 : 0);
})();
