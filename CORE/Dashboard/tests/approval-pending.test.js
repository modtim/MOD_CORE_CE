// FX.163 p3b0 — shared "awaiting approval" notice (js/approval-pending.js).
//
// Asserts:
//   (a) detection: the 202 body fetchApi hands callers is recognised, and
//       ordinary payloads (200 shapes, null, error strings, status 202-shaped
//       strings) are NOT;
//   (b) the notice text names the action label AND the request id, and carries
//       the '#pending-approvals' link (js/ui.js routes that hash to the
//       Security hub's Pending approvals page);
//   (c) no markup smuggling: the stub DOM has NO innerHTML at all (the getter
//       throws), so any markup-building in the module would blow up here, and
//       hostile server values (an approval_id that is a markup string, an
//       actionLabel with tags) land as literal text + escaped attribute values;
//   (d) idempotent: a second call replaces the notice, it never stacks;
//   (e) clearAwaitingApproval removes it, and show() returns null (renders
//       nothing) for a non-awaiting response.
//
//   node --test CORE/Dashboard/tests/approval-pending.test.js
import assert from 'node:assert';
import test from 'node:test';
import fs from 'node:fs';
import { isAwaitingApproval, showAwaitingApproval, clearAwaitingApproval, awaitingApprovalText } from '../js/approval-pending.js';

// --- stub DOM: textContent + setAttribute only, innerHTML deliberately absent
class El {
    constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.parent = null; this.text = null; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    appendChild(c) { this.children.push(c); c.parent = this; return c; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; }
    get textContent() { return this.text ?? ''; }
    set textContent(v) { this.text = String(v); }
    get innerHTML() { throw new Error('no innerHTML allowed: server values must go through textContent'); }
    set innerHTML(v) { throw new Error('no innerHTML allowed: server values must go through textContent'); }
    querySelector(sel) {
        const want = sel.replace(/^#/, '');
        const walk = (el) => el.children.flatMap((c) => [c, ...walk(c)]);
        return walk(this).find((c) => c.attrs.id === want);
    }
}
function newContainer() { return new El('div'); }
globalThis.document = { body: new El('body'), createElement: (t) => new El(t), getElementById: () => null };

function findNotice(container) { return container.children.find((c) => c.attrs.id === 'awaiting-approval-notice'); }
function texts(el) {
    const out = [];
    const walk = (n) => { out.push(n.textContent); n.children.forEach(walk); };
    walk(el);
    return out;
}

const AWAITING = { status: 'awaiting_approval', approval_id: 4407 };

// --- (a) detection ----------------------------------------------------------
test('a: detection true/false cases', () => {
    assert.ok(isAwaitingApproval(AWAITING), 'the 202 body fetchApi returns is awaiting');
    assert.ok(isAwaitingApproval({ status: 202, body: AWAITING }), 'raw {status, body} envelope counts');
    assert.ok(isAwaitingApproval({ status: 'awaiting_approval' }), 'status alone is enough (no id yet)');
    assert.equal(isAwaitingApproval(null), false, 'null is not awaiting');
    assert.equal(isAwaitingApproval(undefined), false, 'undefined is not awaiting');
    assert.equal(isAwaitingApproval({ status: 'ok', approval_id: 3 }), false, 'a normal 200 payload is not awaiting');
    assert.equal(isAwaitingApproval({ status: 'pending', approval_id: 9 }), false, 'file-preview 202 pending is NOT maker-checker');
    assert.equal(isAwaitingApproval('awaiting_approval'), false, 'a bare string is not awaiting');
    assert.equal(isAwaitingApproval(202), false, 'a bare status number is not awaiting');
    assert.equal(isAwaitingApproval({ status: 404 }), false, 'a non-202 status is not awaiting');
});

// --- (b) the notice names the action, the id, and links to the queue -------
test('b: notice carries the id and the #pending-approvals link', () => {
    const container = newContainer();
    const notice = showAwaitingApproval(container, AWAITING, { actionLabel: 'Role grant' });
    assert.ok(notice, 'a notice element is returned');
    assert.equal(container.children.length, 1, 'exactly one child mounted');
    const body = texts(notice).join(' ');
    assert.match(body, /needs a second approver/);
    assert.match(body, /Request #4407/, 'the approval_id is named');
    assert.equal(notice.getAttribute('role'), 'note');
    assert.equal(notice.getAttribute('aria-live'), 'polite');
    const link = notice.children.find((c) => c.tag === 'a');
    assert.ok(link, 'there is a link');
    assert.equal(link.getAttribute('href'), '#pending-approvals');
    assert.equal(link.textContent, 'Open Pending approvals');
    assert.equal(notice.getAttribute('aria-label'), awaitingApprovalText('Role grant', 4407));
});

// --- (c) escaping: hostile server values stay inert ------------------------
test('c: hostile server values cannot smuggle markup', () => {
    const container = newContainer();
    const hostile = {
        status: 'awaiting_approval',
        approval_id: '<img src=x onerror=alert(1)>',
    };
    const notice = showAwaitingApproval(container, hostile, { actionLabel: '<script>alert(1)</script> Export' });
    assert.ok(notice, 'still renders');
    const line = notice.children[0];
    assert.ok(line.textContent.includes('<img src=x onerror=alert(1)>'), 'the id is literal text, not parsed');
    assert.ok(line.textContent.includes('<script>alert(1)</script>'), 'the label is literal text, not parsed');
    assert.equal(notice.children.filter((c) => c.tag === 'img' || c.tag === 'script').length, 0, 'no smuggled elements');
    assert.equal(notice.getAttribute('aria-label'), awaitingApprovalText('<script>alert(1)</script> Export', '<img src=x onerror=alert(1)>'));
    // source-level: the module never touches innerHTML
    const src = fs.readFileSync(new URL('../js/approval-pending.js', import.meta.url), 'utf8');
    const code = src.replace(/^\s*\/\/.*$/gm, '');
    assert.ok(!/innerHTML/.test(code), 'module builds no markup strings');
    assert.match(code, /textContent/, 'module uses textContent');
});

// --- (d) idempotent -------------------------------------------------------
test('d: repeat calls replace, never stack', () => {
    const container = newContainer();
    showAwaitingApproval(container, AWAITING, { actionLabel: 'Role grant' });
    showAwaitingApproval(container, { status: 'awaiting_approval', approval_id: 4408 }, { actionLabel: 'BYOK revoke' });
    assert.equal(container.children.length, 1, 'still one notice');
    const notice = findNotice(container);
    assert.ok(notice, 'notice present');
    assert.match(texts(notice).join(' '), /#4408/, 'shows the newest request');
    assert.ok(!/#4407/.test(texts(notice).join(' ')), 'the stale notice is gone');
});

// --- (e) clear + no-op paths ---------------------------------------------
test('e: clear removes it; non-awaiting renders nothing', () => {
    const container = newContainer();
    showAwaitingApproval(container, AWAITING, { actionLabel: 'Data-marking clear' });
    clearAwaitingApproval(container);
    assert.equal(findNotice(container), undefined, 'notice removed');
    assert.equal(container.children.length, 0, 'container empty');
    assert.equal(showAwaitingApproval(container, { status: 'ok' }, { actionLabel: 'X' }), null, 'nothing rendered');
    assert.equal(container.children.length, 0, 'still empty');
    assert.equal(showAwaitingApproval(null, AWAITING, { actionLabel: 'X' }), null, 'null container is safe');
    const noLabel = showAwaitingApproval(newContainer(), { status: 'awaiting_approval', approval_id: 12 });
    assert.match(noLabel.children[0].textContent, /^This action needs a second approver/, 'missing label falls back');
    const noId = showAwaitingApproval(newContainer(), { status: 'awaiting_approval' }, { actionLabel: 'Full export' });
    assert.match(noId.children[0].textContent, /This request is waiting/, 'missing id falls back');
});
