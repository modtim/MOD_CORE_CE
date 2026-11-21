// Assistant confirmation dialog (Dashboard panel).
//
// assistant-panel.js is one big IIFE, so this script slices the functions it
// needs out of the source and runs them in a vm sandbox with a stub DOM, then
// asserts:
//   (a) a confirm-required turn (needs-confirmation SSE event, or a tool-call
//       carrying needs_confirmation) opens a dialog carrying role=dialog,
//       aria-modal, aria-labelledby, tabindex=-1, the pending tool name and
//       the gate's summary;
//   (b) Confirm re-sends the SAME message with confirm_tokens (one extra
//       fetch POST) and the token is never sent twice;
//   (c) Cancel / Escape send NOTHING (no extra POST) and render
//       "Action cancelled";
//   (d) hostile tool args are textContent only — no markup is created;
//   (e) an expired/invalid confirm-token error renders verbatim with NO
//       Retry button and no dialog offered;
//   (f) the untrusted-content note renders when the gate forced the confirm.
//
//   node --test CORE/Dashboard/tests/assistant-confirm-dialog.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'assistant-panel.js'), 'utf8');

// Pull one top-level (4-space-indented) function out of the IIFE body.
function takeFn(name) {
    const re = new RegExp('\n    (?:async )?function ' + name + '\\(');
    const m = re.exec(src);
    assert.ok(m, 'assistant-panel.js is missing ' + name);
    const start = m.index + 1;
    const rest = src.slice(start + 1);
    const nxt = rest.search(/\n    (?:async )?function |\n    var |\n    const |\n    let /);
    assert.ok(nxt >= 0, 'could not find the end of ' + name);
    return src.slice(start, start + nxt);
}

const CODE = [
    '_el', '_scrollToBottom', '_renderAssistantMessage', '_appendToken',
    '_renderUserMessage', '_clearEmptyState', '_renderErrorRow', '_lastUserMessage',
    '_confirmSummary', '_openConfirmDialog', '_closeConfirmDialog',
    '_cancelConfirm', '_confirmAndResend', '_dispatchSseBlock',
    '_consumeStream', '_sendMessage',
].map(takeFn).join('\n');

// ---------------------------------------------------------------------------
// sandbox — a stub DOM good enough for the panel's DOM helpers (createElement
// returns nodes with appendChild/textContent/setAttribute/classList), plus
// stubs for fetch/sessionStorage/location/window.modA11y.
// ---------------------------------------------------------------------------

function makeNode(tag, text) {
    const node = {
        tag, children: [], className: '', textContent: text || '', attrs: {},
        listeners: {}, parent: null, _removed: false, style: {},
        value: '', disabled: false, id: '', type: '', placeholder: '',
    };
    node.appendChild = (child) => { node.children.push(child); child.parent = node; return child; };
    node.remove = () => { node._removed = true; };
    node.setAttribute = (k, v) => { node.attrs[k] = v; if (k === 'id') node.id = v; };
    node.getAttribute = (k) => Object.prototype.hasOwnProperty(node.attrs, k)
        ? node.attrs[k] : null;
    node.addEventListener = (type, fn) => { node.listeners[type] = fn; };
    node.classList = { add: (c) => { node.className += ' ' + c; }, remove: () => {} };
    node.focus = (opts) => node;
    Object.defineProperty(node, 'isConnected', { get: () => !node._removed });
    Object.defineProperty(node, 'allText', {
        get() {
            let out = node.textContent || '';
            for (const c of node.children) out += (c.allText || '') + ' ';
            return out;
        },
    });
    node.querySelectorAll = (sel) => {
        // Minimal descendant-selector support: ".a .b" matches nodes with
        // class b that have an ancestor with class a (that is what the panel
        // uses to find the last user bubble).
        const parts = sel.split(' ');
        const last = parts[parts.length - 1];
        const wantTag = /^[a-z]/.test(last) ? last.split(/[[.:#]/)[0] : null;
        const wantClass = (last.match(/\.([A-Za-z0-9_-]+)/) || [])[1];
        const wantAttr = (last.match(/\[([A-Za-z0-9_-]+)/) || [])[1];
        const ancestorCls = parts.length > 1
            ? (parts[0].match(/\.([A-Za-z0-9_-]+)/) || [])[1] : null;
        const hasAncestorCls = (n) => {
            for (let p = n.parent; p; p = p.parent) {
                if ((p.className || '').includes(ancestorCls)) return true;
            }
            return false;
        };
        const out = [];
        (function walk(n) {
            for (const c of n.children) {
                const okTag = !wantTag || c.tag === wantTag;
                const okCls = !wantClass || (c.className || '').includes(wantClass);
                const okAttr = !wantAttr || Object.prototype.hasOwnProperty(c.attrs, wantAttr);
                const okAnc = !ancestorCls || hasAncestorCls(c);
                if (okTag && okCls && okAttr && okAnc) out.push(c);
                walk(c);
            }
        })(node);
        return out;
    };
    node.querySelector = (sel) => node.querySelectorAll(sel)[0];
    node.contains = (other) => {
        for (const c of node.children) if (c === other || c.contains(other)) return true;
        return false;
    };
    return node;
}

// Panel-level state the extracted functions close over (declared in the IIFE
// above the functions we sliced out).
const PRELUDE = `
    var messagesEl = null;
    var scrollGuard = { atBottom: true };
    var jumpPillEl = null;
    var inputEl = null;
    var sendBtn = null;
    var pendingConfirms = [];
    var confirmUntrustedNote = false;
    var confirmTokens = {};
    var confirmDialogEl = null;
    var confirmKeyHandler = null;
    function _sessionId() { return "s-test"; }
    function _renderActionEmit(handle, action) {}
    function _renderCitation(handle, citation) {}
`;

// SSE fixtures keyed by test name; each POST in a test gets the next stream.
const STREAMS = {
    confirm: [
        'event: text-token\ndata: {"text":"I want to delete."}\n\n'
        + 'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{"workflow_id":42},'
        + '"needs_confirmation":true,"confirm_token":"tok-abc-123",'
        + '"summary":"assistant wants to run delete_workflow (delete): workflow_id=42",'
        + '"risk_category":"delete"}\n\n'
        + 'event: done\ndata: {}\n\n',
        'event: text-token\ndata: {"text":"Deleted."}\n\n'
        + 'event: done\ndata: {}\n\n',
    ],
    cancel: [
        'event: tool-call\ndata: {"name":"delete_workflow","args":{"workflow_id":42},'
        + '"needs_confirmation":true,"confirm_token":"tok-abc-123"}\n\n'
        + 'event: done\ndata: {}\n\n',
    ],
    hostile: [
        'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{'
        + '"name":"<img src=x onerror=alert(1)>"}, "needs_confirmation":true,'
        + '"confirm_token":"tok-2"}\n\n'
        + 'event: done\ndata: {}\n\n',
    ],
    untrusted: [
        'event: needs-confirmation\ndata: {"name":"delete_workflow","args":{"workflow_id":7},'
        + '"needs_confirmation":true,"confirm_token":"tok-3","forced_by":"untrusted_content",'
        + '"risk_category":"untrusted_context"}\n\n'
        + 'event: done\ndata: {}\n\n',
    ],
    expired: [
        'event: error\ndata: {"message":"Confirm token already used or expired."}\n\n'
        + 'event: done\ndata: {}\n\n',
    ],
};

function run(testName) {
    const posts = [];
    const docHandlers = {};
    const doc = {
        createElement: (t) => makeNode(t),
        createTextNode: (t) => makeNode('#text', t),
        body: makeNode('body'),
        getElementById: () => null,
        activeElement: null,
        querySelector: () => null,
        getComputedStyle: (e) => ({}),
    };
    const sandbox = {
        document: doc,
        docHandlers: docHandlers,
        window: {
            APP_CONFIG: { API_URL: 'http://api.test' },
            modA11y: { attach: (el) => el, detach: (el) => el },
        },
        sessionStorage: { getItem: () => 'tok', setItem: () => {} },
        location: { pathname: '/', hash: '' },
        console: { warn: () => {}, log: () => {} },
        setTimeout: setTimeout,
        TextDecoder: require('node:util').TextDecoder,
        fetch: (url, opts) => {
            posts.push(JSON.parse(opts.body));
            const sse = STREAMS[testName][Math.min(posts.length - 1,
                STREAMS[testName].length - 1)];
            // The panel reads response headers (X-MOD-AI-Generated, FX.72)
            // after the stream drains; a real fetch Response always has a
            // Headers object, so the stub must too — otherwise the panel's
            // try/catch swallows the TypeError and the confirm dialog never
            // opens.
            return Promise.resolve({
                ok: true, status: 200,
                headers: { get: () => null },
                body: makeStream(sse),
            });
        },
    };
    // document.addEventListener is recorded so the tests can invoke the
    // panel's capture-phase keydown handler (Escape = Cancel).
    doc.addEventListener = (type, fn) => { docHandlers[type] = fn; };
    doc.removeEventListener = (type, fn) => { delete docHandlers[type]; };
    const ctx = vm.createContext(sandbox);
    const wrapped = '(function () {' + PRELUDE + CODE + '\n'
        + 'return {'
        + '_sendMessage, _dispatchSseBlock, _openConfirmDialog, _closeConfirmDialog,'
        + ' _cancelConfirm, _confirmAndResend, _confirmSummary,'
        + ' getDialog: () => confirmDialogEl, getPending: () => pendingConfirms,'
        + ' getTokens: () => confirmTokens, getNote: () => confirmUntrustedNote,'
        + ' setMessagesEl: (m) => { messagesEl = m; }, setInput: (v) => { inputEl = v; },'
        + ' setSend: (v) => { sendBtn = v; }, getMessagesEl: () => messagesEl,'
        + ' getDocHandler: (t) => docHandlers[t]'
        + '}; })()';
    const api = vm.runInContext(wrapped, ctx);
    return { api, posts, sandbox, doc };
}

function makeStream(text) {
    const chunks = [Buffer.from(text, 'utf8')];
    let i = 0;
    return {
        getReader: () => ({
            read: () => i < chunks.length
                ? Promise.resolve({ value: chunks[i++], done: false })
                : Promise.resolve({ value: null, done: true }),
        }),
    };
}

// ---------------------------------------------------------------------------
// helpers to walk the stub tree
// ---------------------------------------------------------------------------
function findByClass(root, cls) {
    const out = [];
    (function walk(n) {
        for (const c of n.children) {
            if ((c.className || '').includes(cls)) out.push(c);
            walk(c);
        }
    })(root);
    return out;
}
function allTextOf(root) {
    return root.allText;
}
function hasText(root, needle) {
    const hay = typeof root === 'string' ? root : allTextOf(root);
    assert.ok(hay.includes(needle), 'text ' + JSON.stringify(needle) + ' missing from '
        + JSON.stringify(hay));
}
function buttonsOf(root) {
    const out = [];
    (function walk(n) {
        for (const c of n.children) { if (c.tag === 'button') out.push(c); walk(c); }
    })(root);
    return out;
}

// ---------------------------------------------------------------------------
// (a) confirm-required -> dialog with a11y attributes + tool + summary
// ---------------------------------------------------------------------------
test('confirm-required turn opens the accessible dialog', async () => {
    const { api, posts } = run('confirm');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete workflow 42');

    // Exactly one POST so far, and it carried no tokens.
    assert.strictEqual(posts.length, 1);
    assert.strictEqual(posts[0].confirm_tokens, undefined);

    const dialog = api.getDialog();
    assert.ok(dialog, 'the dialog did not open');
    assert.strictEqual(dialog.attrs['role'], 'dialog');
    assert.strictEqual(dialog.attrs['aria-modal'], 'true');
    assert.strictEqual(dialog.attrs['aria-labelledby'], 'mod-asst-confirm-title');
    assert.strictEqual(dialog.attrs['tabindex'], '-1');

    const text = allTextOf(dialog);
    hasText(text, 'delete_workflow');
    assert.match(text, /assistant wants to run delete_workflow/);
    assert.match(text, /Confirm before the assistant acts/);

    const btns = buttonsOf(dialog).map((b) => b.textContent);
    assert.ok(btns.includes('Confirm'), 'no Confirm button');
    assert.ok(btns.includes('Cancel'), 'no Cancel button');
});

// ---------------------------------------------------------------------------
// (b) Confirm -> exactly one more POST carrying confirm_tokens, once only
// ---------------------------------------------------------------------------
test('Confirm re-sends the same request with confirm_tokens', async () => {
    const { api, posts } = run('confirm');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete workflow 42');
    assert.ok(api.getDialog(), 'dialog must be open');
    assert.strictEqual(Object.keys(api.getTokens()).length, 1,
        'the token must be held while the dialog is open');

    // Fire the dialog's own Confirm listener (what the click calls). The
    // handler is async and chains the re-send, so await it.
    const dialog = api.getDialog();
    const confirmBtn = buttonsOf(dialog).find((b) => b.textContent === 'Confirm');
    assert.ok(confirmBtn, 'no Confirm button');
    await confirmBtn.listeners.click();
    await new Promise((r) => setTimeout(r, 20));

    // Exactly one extra POST — the re-send — and it carries the token.
    assert.strictEqual(posts.length, 2);
    assert.strictEqual(posts[1].message, 'delete workflow 42');
    assert.deepStrictEqual(posts[1].confirm_tokens, { delete_workflow: 'tok-abc-123' });
    assert.strictEqual(posts[1].current_app, 'dashboard');

    // Tokens cleared: a second Confirm fires nothing (single-use).
    confirmBtn.listeners.click();
    await new Promise((r) => setTimeout(r, 20));
    assert.strictEqual(posts.length, 2);
    assert.strictEqual(Object.keys(api.getTokens()).length, 0);
    assert.strictEqual(api.getDialog(), null, 'dialog closed by Confirm');
});

// ---------------------------------------------------------------------------
// (c) Cancel sends nothing; Escape behaves the same
// ---------------------------------------------------------------------------
test('Cancel sends nothing and shows Action cancelled', async () => {
    const { api, posts } = run('cancel');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete workflow 42');
    // The tool-call-with-needs_confirmation shape also opens the dialog.
    assert.ok(api.getDialog(), 'dialog must open from the tool-call shape');

    const cancelBtn = buttonsOf(api.getDialog()).find((b) => b.textContent === 'Cancel');
    assert.ok(cancelBtn, 'no Cancel button');
    cancelBtn.listeners.click();

    // No request at all was issued by Cancel.
    assert.strictEqual(posts.length, 1);
    assert.strictEqual(Object.keys(api.getTokens()).length, 0, 'tokens dropped on Cancel');
    assert.match(allTextOf(api.getMessagesEl()), /Action cancelled/);
    assert.strictEqual(api.getDialog(), null, 'dialog closed');
});

test('Escape closes the dialog as Cancel: no request, Action cancelled', async () => {
    const { api, posts } = run('cancel');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete workflow 42');
    const dialog = api.getDialog();
    assert.ok(dialog);
    // The panel registers a capture-phase document keydown whose Escape path
    // calls _cancelConfirm; assert that handler exists and is the cancel path.
    const handler = api.getDocHandler('keydown');
    assert.ok(handler, 'no keydown handler registered');
    handler({ key: 'Escape', stopPropagation: () => {} });

    assert.strictEqual(posts.length, 1);
    assert.match(allTextOf(api.getMessagesEl()), /Action cancelled/);
    assert.strictEqual(api.getDialog(), null);
});

// ---------------------------------------------------------------------------
// (d) hostile tool args: textContent only, no markup created
// ---------------------------------------------------------------------------
test('hostile tool args render as text, never markup', async () => {
    const { api } = run('hostile');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete it');
    const dialog = api.getDialog();
    assert.ok(dialog);
    const text = allTextOf(dialog);
    hasText(text, '<img src=x onerror=alert(1)>');
    // No img element and no onerror attribute anywhere in the dialog tree.
    assert.strictEqual(dialog.querySelectorAll('img').length, 0);
    assert.strictEqual(dialog.querySelectorAll('[onerror]').length, 0);
});

// ---------------------------------------------------------------------------
// (e) untrusted-content turn: the note renders
// ---------------------------------------------------------------------------
test('untrusted-content turn says so in the dialog', async () => {
    const { api } = run('untrusted');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete it');
    const dialog = api.getDialog();
    assert.ok(dialog);
    hasText(allTextOf(dialog), 'content from outside MOD');
    hasText(allTextOf(dialog), 'Forced by untrusted content');
});

// ---------------------------------------------------------------------------
// (f) expired/invalid token: error verbatim, nothing offered automatic
// ---------------------------------------------------------------------------
test('expired/invalid confirm token: error shown, nothing offered', async () => {
    const { api, posts } = run('expired');
    api.setMessagesEl(makeNode('div'));
    api.setInput(makeNode('input'));
    api.setSend(makeNode('button'));

    await api._sendMessage('delete it');

    hasText(allTextOf(api.getMessagesEl()),
        'Confirm token already used or expired.');
    // No Retry button next to it, and no dialog offered.
    const btns = buttonsOf(api.getMessagesEl()).map((b) => b.textContent);
    assert.ok(!btns.includes('Retry'), 'Retry must not be offered');
    assert.strictEqual(api.getDialog(), null, 'no automatic re-confirm dialog');
    assert.strictEqual(posts.length, 1, 'nothing else was sent');
});
