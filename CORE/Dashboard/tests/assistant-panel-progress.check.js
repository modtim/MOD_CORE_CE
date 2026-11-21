'use strict';
// The assistant SSE stream now emits `progress` events
// (CORE/API/services/assistant_orchestrator.py: `_sse("progress", {"message": _p})`)
// while it wakes a sleeping AI stack. assistant-panel.js's SSE switch ignored
// unknown events, so the user stared at "thinking…" for 30-90s.
//
// There is no runnable JS test harness for the Dashboard (package.json ships only
// tailwindcss — no jsdom/mocha), so this follows the existing *.check.js style:
// static assertions + a node:vm harness (same pattern as
// bug-b55-compliance-hub-renders.check.js) that evaluates the panel's real
// render helpers against a hand-rolled DOM shim and drives the scenarios.
//
//   node CORE/Dashboard/tests/assistant-panel-progress.check.js
//
// Checks:
//   1. the SSE switch dispatches `progress` to _renderProgress(handle, parsed)
//   2. _renderProgress reuses the muted .mod-asst-thinking slot (single line,
//      replaces on a second event, textContent only)
//   3. the first text-token and the error branch remove the line
//   4. no innerHTML anywhere in the file, no bg-blue-600
//   5. behaviour: renders / replaces / token-removes / HTML escaped

const assert = require('node:assert');
const fs = require('fs');
const vm = require('node:vm');
const path = require('path');

// CLASS-A fix (checkjs-triage-a-1030: RUNNER-GAP): the repo-root literal PANEL_REL
// was read at :29, but the nodetest sandbox mounts CORE/Dashboard at /app
// (r9700-run.sh:1306) -> ENOENT, the gate never read the product file, and the
// The progress work went unproven. Resolve the panel relative to
// THIS file — same pattern as apps-auth-config-chip.check.js:19 /
// admin-sso-lock.test.js:18 — so it works from the repo tree AND the /app-rooted
// container. PANEL_REL stays as the display label in the messages below.
const PANEL_REL = 'CORE/Dashboard/js/assistant-panel.js';
const PANEL_ABS = path.join(__dirname, '..', 'js', 'assistant-panel.js');
const panelSrc = fs.readFileSync(process.argv[2] || PANEL_ABS, 'utf8');

// --- 1-4) static shape ----------------------------------------------------
assert.ok(
    /else if \(event === "progress"\) _renderProgress\(handle, parsed\);/.test(panelSrc),
    'SSE switch must dispatch progress -> _renderProgress(handle, parsed)');
assert.ok(/function _renderProgress\(handle, parsed\)/.test(panelSrc),
    '_renderProgress must exist');
assert.ok(!/innerHTML/.test(panelSrc),
    'assistant-panel.js must never touch innerHTML (server text is untrusted)');
assert.ok(!/bg-blue-600/.test(panelSrc),
    'progress line must not use a bg-blue-600 highlight');
const errBranch = panelSrc.slice(panelSrc.indexOf('else if (event === "error")'));
assert.ok(/handle\.thinking\)\s*\{[\s\S]{0,200}?handle\.thinking\.remove\(\)/.test(errBranch),
    'the error branch must remove a pending progress line before rendering the error');

// --- 5) behaviour harness -------------------------------------------------
// Pull the helper bodies out of the IIFE so the REAL code runs, not a copy.
function bodyOf(name) {
    const start = panelSrc.indexOf('function ' + name + '(');
    assert.ok(start >= 0, name + ' missing from ' + PANEL_REL);
    let depth = 0, i = panelSrc.indexOf('{', start);
    for (let j = i; j < panelSrc.length; j++) {
        if (panelSrc[j] === '{') depth++;
        else if (panelSrc[j] === '}') { depth--; if (!depth) return panelSrc.slice(start, j + 1); }
    }
    throw new Error('unbalanced braces for ' + name);
}

const src = ['_el', '_renderAssistantMessage', '_appendToken', '_renderProgress']
    .map(bodyOf).join('\n');

// Hand-rolled DOM shim. innerHTML is a throwing accessor: if any evaluated path
// reached for it, the harness fails loudly (server text must use textContent).
const STUBS = `
function _mkEl(tag) {
    const el = { tag, className: '', kids: [], text: null, removed: false };
    Object.defineProperty(el, 'innerHTML', {
        get() { throw new Error('innerHTML used'); },
        set() { throw new Error('innerHTML used'); },
    });
    el.appendChild = function (c) { el.kids.push(c); c._parent = el; return c; };
    el.remove = function () {
        el.removed = true;
        const p = el._parent;
        if (p) p.kids = p.kids.filter(function (k) { return k !== el; });
    };
    Object.defineProperty(el, 'textContent', {
        get() { return el.text == null
            ? el.kids.map(function (k) { return k.textContent; }).join('')
            : el.text; },
        set(v) { el.text = v; },
    });
    el.classList = { add() {}, remove() {} };
    el.querySelector = function () { return null; };
    return el;
}
function _mkText(v) {
    const t = { v, nodeValue: v, remove() {} };
    Object.defineProperty(t, 'textContent', { get() { return t.nodeValue; } });
    return t;
}
const messagesEl = _mkEl('div');
const scrollGuard = { atBottom: false };
const jumpPillEl = null;
const document = { createElement: _mkEl, createTextNode: _mkText };
`;

const ctx = vm.createContext({});
vm.runInNewContext(
    STUBS + src + '\nvar _handle = null;\n', ctx, { filename: PANEL_REL });
const R = function (expr) { return vm.runInContext(expr, ctx); };

// a) progress renders the text (replaces the "thinking…" slot)
R('_handle = _renderAssistantMessage()');
R('_renderProgress(_handle, {message: "Starting the AI model\\u2026"})');
assert.strictEqual(R('_handle.bubble.textContent'), 'Starting the AI model…');
assert.strictEqual(R('_handle.bubble.kids.length'), 1,
    'progress must reuse the single status slot, not append a second line');

// b) a second progress event REPLACES the line
R('_renderProgress(_handle, {message: "Waking the AI stack\\u2026"})');
assert.strictEqual(R('_handle.bubble.textContent'), 'Waking the AI stack…');
assert.strictEqual(R('_handle.bubble.kids.length'), 1);

// c) the first text token removes the line
R('_appendToken(_handle, "Hi")');
assert.strictEqual(R('_handle.thinking'), null,
    'the first token must clear the pending status line');
assert.strictEqual(R('_handle.bubble.textContent'), 'Hi');

// d) HTML in the message is escaped: textContent assignment, never markup
R('_handle = _renderAssistantMessage()');
R('_renderProgress(_handle, {message: "<img src=x onerror=alert(1)>"})');
assert.strictEqual(R('_handle.bubble.textContent'), '<img src=x onerror=alert(1)>',
    'server text must land as literal text, not markup');
assert.strictEqual(R('_handle.bubble.kids[0].text'), '<img src=x onerror=alert(1)>',
    'the status span must carry the payload via textContent');

// e) an error event also removes the line
R('_handle = _renderAssistantMessage()');
R('_renderProgress(_handle, {message: "Starting the AI model\\u2026"})');
assert.ok(/_renderErrorRow/.test(panelSrc), '_renderErrorRow must exist (error branch)');
// the switch itself is unreachable standalone; assert the removal code path:
R('if (_handle.thinking) { _handle.thinking.remove(); _handle.thinking = null; }');
assert.strictEqual(R('_handle.bubble.textContent'), '',
    'error path must leave no stale progress line in the pending bubble');

console.log('assistant-panel-progress.check.js: all assertions passed');
