// Node-runnable check (FIX-DASH-STACK-HOSTPORTS-SAVED-LINE-SURVIVES-RERENDER, D73):
// after a host-ports Save, js/persistent-stacks.js _psHostPortsSave writes the
// "Saved as a new revision..." line into #ps-hp-out-<id>, then calls
// _psLoadStacks() and _psRenderEndpoints(id), which rebuild the networking panel
// with a FRESH EMPTY #ps-hp-out-<id>. Pre-fix the line was wiped, so the user got
// no visible confirmation. This script extracts the real _psHpOut and
// _psHostPortsSave (plus the notice state) from the source text and drives them
// with a fake DOM whose "render" replaces the element with a new empty one,
// exactly like the real panel rebuild. RED pre-fix, green after.
//
//   node CORE/Dashboard/tests/persistent-stacks-hostports-saved-line.check.js
//   node .../persistent-stacks-hostports-saved-line.check.js /tmp/pre-fix-persistent-stacks.js   (mutation proof)
'use strict';
const fs = require('fs');
const path = require('path');

const jsPath = process.argv[2] || path.join(__dirname, '..', 'js', 'persistent-stacks.js');
const src = fs.readFileSync(jsPath, 'utf8');

function grab(startMarker, endMarker) {
    const a = src.indexOf(startMarker);
    if (a < 0) throw new Error('missing ' + startMarker);
    const b = src.indexOf(endMarker, a + startMarker.length);
    if (b < 0) throw new Error('missing ' + endMarker);
    return src.slice(a, b);
}

let failures = 0;
function check(name, cond) {
    if (cond) { console.log(`ok   ${name}`); } else { failures++; console.error(`FAIL ${name}`); }
}

// Notice state only exists post-fix; pre-fix the block is simply absent.
const hasState = src.includes('const _psHpNotice = {};');
const stateBlock = hasState ? 'const _psHpNotice = {};' : '';
const fns = grab('function _psHpOut(', 'function _psHostPortsAdd(');
// The replay at the end of the real renderer (post-fix only).
const replay = hasState ? grab('    const kept = _psHpNotice[id];', '}\n') : '';

const SAVED = 'Saved as a new revision. The running stack is unchanged until you Deploy.';

function makeEnv(opts) {
    const dom = {};
    const fresh = (id) => { dom[id] = { textContent: '', className: '' }; };
    const body = [
        stateBlock,
        fns,
        'return { save: _psHostPortsSave, out: _psHpOut, notice: ' + (hasState ? '_psHpNotice' : '{}') + ' };',
    ].join('\n');
    const api = new Function(
        'document', 'apiCall', '_psHpCollect', '_psLoadStacks', '_psRenderEndpoints', '_psEsc',
        body
    );
    const document = {
        getElementById: (id) => (id === 'ps-backups-7' ? { dataset: {} } : dom[id] || null),
    };
    const render = async (id) => {
        // real panel rebuild: brand-new empty output element ...
        fresh('ps-hp-out-' + id);
        // ... then (post-fix) the renderer replays the kept notice
        if (hasState && replay) {
            const _psHpNotice = env.notice;
            const _psHpOut = env.out;
            new Function('id', '_psHpNotice', '_psHpOut', replay)(id, _psHpNotice, _psHpOut);
        }
    };
    const env = api(
        document,
        async () => ({}),
        () => [],
        async () => { fresh('ps-hp-out-7'); },
        opts.slowRender ? async (id) => { await new Promise((r) => setTimeout(r, 20)); await render(id); } : render,
        (s) => String(s)
    );
    fresh('ps-hp-out-7');
    return { env, dom };
}

(async () => {
    {
        const { env, dom } = makeEnv({});
        await env.save(7);
        check('saved line present on the re-rendered element', dom['ps-hp-out-7'].textContent === SAVED);
        check('saved line styled success', /text-success/.test(dom['ps-hp-out-7'].className));
    }
    {
        const { env, dom } = makeEnv({ slowRender: true });
        await env.save(7);
        check('saved line present after a slow render', dom['ps-hp-out-7'].textContent === SAVED);
    }
    if (hasState) {
        // Replay alone: a later render (e.g. a list refresh) keeps the line.
        const { env, dom } = makeEnv({});
        await env.save(7);
        dom['ps-hp-out-7'] = { textContent: '', className: '' };
        new Function('id', '_psHpNotice', '_psHpOut', replay)(7, env.notice, env.out);
        check('a further render replays the saved line', dom['ps-hp-out-7'].textContent === SAVED);
    }
    {
        // Error path stays visible and destructive-styled.
        const dom = {};
        dom['ps-hp-out-7'] = { textContent: '', className: '' };
        const body = [stateBlock, fns, 'return _psHostPortsSave;'].join('\n');
        const save = new Function('document', 'apiCall', '_psHpCollect', '_psLoadStacks', '_psRenderEndpoints', '_psEsc', body)(
            { getElementById: (id) => dom[id] || null },
            async () => { const e = new Error('boom'); e.status = 500; throw e; },
            () => [], async () => {}, async () => {}, (s) => String(s)
        );
        await save(7);
        check('error text stays visible', dom['ps-hp-out-7'].textContent === 'Save failed: boom');
        check('error text destructive-styled', /text-destructive/.test(dom['ps-hp-out-7'].className));
    }
    if (failures) { console.error(`${failures} failure(s)`); process.exit(1); }
    console.log('all ok');
})();
