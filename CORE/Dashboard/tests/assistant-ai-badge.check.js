'use strict';
// FX.72 — "AI output labels in UI" (decomp/gsec-fix-wave.md:100).
//
// GY.V10 already ships the `X-MOD-AI-Generated: true` response header on every
// assistant/wizard reply (CORE/API/routers/assistant.py, routers/support_chat/),
// but no frontend read it: `grep -rl X-MOD-AI-Generated CORE/Dashboard
// CORE/WorkflowBuilder` was empty. assistant-panel.js now reads the header off
// the fetch response right after _consumeStream resolves and appends a small
// muted badge inside the assistant message `wrap` (NOT the bubble, so it can
// never interleave with streamed text).
//
// Same style as assistant-panel-progress.check.js: static assertions + a node:vm
// harness that evaluates the panel's REAL helpers against a hand-rolled DOM shim
// (innerHTML is a throwing accessor, so any evaluated path that reached for it
// fails loudly).
//
//   node CORE/Dashboard/tests/assistant-ai-badge.check.js
//
// Checks:
//   1. _sendMessage reads resp.headers.get("X-MOD-AI-Generated") === "true"
//   2. the badge is _el("span", "mod-asst-ai-badge", "AI-generated") and is
//      appended to handle.wrap, never handle.bubble
//   3. a .mod-asst-ai-badge CSS rule exists, muted/small (no innerHTML, no
//      bg-blue-600 highlight)
//   4. behaviour: badge lands in wrap with literal text, bubble untouched,
//      HTML in a reply stays escaped text

const assert = require('node:assert');
const fs = require('fs');
const vm = require('node:vm');
const path = require('path');

// CLASS-A fix (checkjs-triage-a-1030: RUNNER-GAP): gate:32 read PANEL_REL as a repo-root
// literal, but the nodetest sandbox mounts CORE/Dashboard at /app (r9700-run.sh:1306), so
// the read ENOENTed and the gate never saw the product file (the badge landed in 97f59a78c).
// Resolve the panel relative to THIS file — same pattern as admin-sso-lock.test.js:18 and
// apps-auth-config-chip.check.js:19 — so it works from the repo tree AND the /app-rooted
// container. PANEL_REL stays as the display label in the messages below.
const PANEL_REL = 'CORE/Dashboard/js/assistant-panel.js';
const PANEL_ABS = path.join(__dirname, '..', 'js', 'assistant-panel.js');
const panelSrc = fs.readFileSync(process.argv[2] || PANEL_ABS, 'utf8');

// --- 1) the header is actually read ----------------------------------------
assert.ok(/resp\.headers\.get\(["']X-MOD-AI-Generated["']\)\s*===\s*["']true["']/.test(panelSrc),
    'assistant-panel.js must read the X-MOD-AI-Generated response header');

// --- 2) the badge is built with _el and lands on wrap, not bubble ----------
const badgeRe = /_el\(\s*"span",\s*"mod-asst-ai-badge",\s*"AI-generated"\s*\)/;
assert.ok(badgeRe.test(panelSrc),
    'badge must be _el("span", "mod-asst-ai-badge", "AI-generated")');
assert.ok(/handle\.wrap\.appendChild\(\s*_el\(\s*"span",\s*"mod-asst-ai-badge"/.test(panelSrc),
    'the badge must be appended to handle.wrap');
assert.ok(!/handle\.bubble\.appendChild\(\s*_el\(\s*"span",\s*"mod-asst-ai-badge"/.test(panelSrc),
    'the badge must NOT be appended to handle.bubble (streamed text lives there)');

// the append must happen after the stream finished, not before it started
const streamIdx = panelSrc.indexOf('await _consumeStream(resp.body, handle, sink)');
const badgeIdx = panelSrc.indexOf('mod-asst-ai-badge", "AI-generated');
assert.ok(streamIdx >= 0 && badgeIdx > streamIdx,
    'the badge must be appended after _consumeStream resolves (reply fully rendered)');

// --- 3) styling + the hard no-innerHTML rule -------------------------------
assert.ok(!/innerHTML/.test(panelSrc),
    'assistant-panel.js must never touch innerHTML (server text is untrusted)');
assert.ok(!/bg-blue-600/.test(panelSrc),
    'the badge must not use a bg-blue-600 highlight');
const cssRule = panelSrc.slice(panelSrc.indexOf('.mod-asst-ai-badge'));
assert.ok(/\.mod-asst-ai-badge\s*\{/.test(panelSrc),
    'a .mod-asst-ai-badge CSS rule must ship next to the other .mod-asst-* rules');
assert.ok(/font-size:\s*10px/.test(cssRule) && /opacity:\s*\.\d/.test(cssRule),
    'the badge must be small and muted (font-size 10px + opacity)');
assert.ok(/--muted-foreground/.test(cssRule),
    'the badge must use the muted-foreground token, like .mod-asst-thinking');

// --- 4) behaviour harness --------------------------------------------------
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

const src = ['_el', '_renderAssistantMessage'].map(bodyOf).join('\n');

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
vm.runInNewContext(STUBS + src + '\nvar _h = null;\n', ctx, { filename: PANEL_REL });
const R = function (expr) { return vm.runInContext(expr, ctx); };

// a) a fresh assistant message carries no badge until the header says so
R('_h = _renderAssistantMessage()');
assert.ok(R('_h.wrap.kids.filter(function (k) { return k.className === "mod-asst-ai-badge"; }).length') === 0,
    'no badge may exist before the header is checked');

// b) header true -> badge appended to wrap, after the bubble, textContent only
R('_h.wrap.appendChild(_el("span", "mod-asst-ai-badge", "AI-generated"))');
const badge = R('_h.wrap.kids.filter(function (k) { return k.className === "mod-asst-ai-badge"; })');
assert.strictEqual(badge.length, 1, 'exactly one badge per assistant reply');
assert.strictEqual(R('_h.wrap.kids[3].textContent'), 'AI-generated');
assert.strictEqual(R('_h.wrap.kids[3].text'), 'AI-generated',
    'the badge label must land via textContent, never markup');
assert.strictEqual(R('_h.bubble.kids.filter(function (k) { return k.className === "mod-asst-ai-badge"; }).length'), 0,
    'the bubble must stay badge-free (streamed text only)');

// c) streamed text + badge coexist without interleaving
R('_h.bubble.textContent = "Hello there"');
assert.strictEqual(R('_h.wrap.textContent'), 'Hello thereAI-generated',
    'the badge must trail the reply, not split it');

// d) HTML in a reply stays literal text (no innerHTML path reached)
R('_h = _renderAssistantMessage()');
R('_h.bubble.textContent = "<img src=x onerror=alert(1)>"');
R('_h.wrap.appendChild(_el("span", "mod-asst-ai-badge", "AI-generated"))');
assert.strictEqual(R('_h.bubble.text'), '<img src=x onerror=alert(1)>',
    'server text must land as literal text, not markup');

console.log('assistant-ai-badge.check.js: all assertions passed');
