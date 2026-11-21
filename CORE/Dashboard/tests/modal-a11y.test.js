// Node-runnable check for the shared dialog a11y helper
// (Dashboard/js/modal.js, 417 lines, 18 loaders - the highest blast radius
// in the Dashboard, and 0 test refs before this file). modal.js has no
// imports, so the whole file is evaluated in a vm sandbox over a stub DOM
// (no jsdom, no timers: requestAnimationFrame is left undefined so attach
// focuses synchronously, and the MutationObserver/window blocks are skipped
// by their own typeof guards). Asserts:
//   (a) getDialogContainer: card inside a fixed inset-0 overlay resolves to
//       the overlay; the overlay resolves to itself; null / non-element /
//       no-fixed-ancestor resolve correctly;
//   (b) modA11yAttach: role/aria-modal/tabindex set, aria-labelledby wired to
//       the title heading (the heading gets a generated id), returns
//       {container, detach};
//   (c) attach/detach symmetry: 5 open/close cycles leak no keydown
//       listener and leave no _modA11y state;
//   (d) Escape-to-close: dismissible attach stops propagation, clears the
//       inline display the opener set and adds `hidden`; dismissible:false
//       leaves the overlay alone (the caller owns its Escape path);
//   (e) focus trap: Tab on the last focusable wraps to the first with
//       preventDefault; Shift+Tab on the first wraps to the last;
//   (f) isDialogVisible: hidden class / hidden attribute / display:none all
//       read as not visible;
//   (g) escapeHtml: all five entities (showCopyableModal interpolates user
//       text twice - title and message).
//
//   node --test CORE/Dashboard/tests/modal-a11y.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'modal.js'), 'utf8');
const block = src.replace(/^export /gm, '');

function mkEl(tag, cls = '', attrs = {}) {
    const el = {
        nodeType: 1, tagName: tag, id: attrs.id || '', attrs: Object.assign({}, attrs),
        style: { display: '' }, children: [], parentElement: null, isConnected: true,
        offsetWidth: 10, offsetHeight: 10, focusCount: 0, removed: false,
    };
    el.classList = { list: cls.split(/\s+/).filter(Boolean) };
    el.classList.contains = (c) => el.classList.list.includes(c);
    el.classList.add = (c) => { if (!el.classList.list.includes(c)) el.classList.list.push(c); };
    el.classList.remove = (c) => { el.classList.list = el.classList.list.filter((x) => x !== c); };
    el.classList.toggle = (c, force) => {
        const has = el.classList.list.includes(c);
        if (force === true || (force === false && has)) return;
        if (force === false || !has) { el.classList.list = el.classList.list.filter((x) => x !== c); return; }
    };
    el.getAttribute = (n) => el.attrs[n] === undefined ? null : el.attrs[n];
    el.setAttribute = (n, v) => { el.attrs[n] = v; };
    el.focus = () => { el.focusCount++; };
    el.remove = () => { el.removed = true; el.isConnected = false; };
    el.addEventListener = (type, fn) => { el.listeners.push(fn); };
    el.querySelectorAll = (sel) => collect(el).filter((d) => d.focusable);
    el.querySelector = (sel) => collect(el).find((d) => d.match(sel));
    el.contains = (other) => collect(el).includes(other);
    el.closest = (sel) => {
        let p = el;
        while (p) {
            const role = p.getAttribute('role');
            if (role === 'dialog' || role === 'alertdialog') return p;
            p = p.parentElement;
        }
        return null;
    };
    el.match = (sel) => false;
    return el;
}

function collect(el) {
    const out = [];
    for (const c of el.children) { out.push(c); for (const g of collect(c)) out.push(g); }
    return out;
}

function link(parent, ...kids) {
    for (const k of kids) { parent.children.push(k); k.parentElement = parent; }
    return parent;
}

function run(display) {
    const overlay = mkEl('div', 'fixed inset-0 bg-gray-600 z-50', { id: 'copyable-modal' });
    const card = mkEl('div', 'relative mx-auto rounded-md bg-white');
    const title = mkEl('h3', 'text-xl font-semibold text-gray-900');
    title.match = (sel) => /h1|h2|h3|h4|font-semibold|font-bold/.test(sel);
    const btn1 = mkEl('button', 'px-4 py-2 rounded'); btn1.focusable = true;
    btn1.match = (sel) => /button/.test(sel);
    const btn2 = mkEl('button', 'px-4 py-2 rounded'); btn2.focusable = true;
    btn2.match = (sel) => /button/.test(sel);
    link(overlay, card); link(card, title, btn1, btn2);
    if (display) overlay.style.display = display;

    const doc = {
        listeners: [], activeElement: null,
        getElementById: (id) => (id === 'copyable-modal' ? overlay : null),
        querySelectorAll: () => [], querySelector: () => null,
        addEventListener: (type, fn, capture) => { doc.listeners.push(fn); },
        removeEventListener: (type, fn, capture) => {
            const i = doc.listeners.indexOf(fn);
            if (i >= 0) doc.listeners.splice(i, 1);
        },
    };
    const sandbox = {
        console: { log: () => {}, error: () => {} },
        document: doc,
        navigator: { clipboard: { writeText: () => {} } },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ getDialogContainer, modA11yAttach, modA11yDetach, modA11y, isDialogVisible, escapeHtml, getDialogTitle, getFocusables });`,
        sandbox, { filename: 'modal-body' });
    return { fns, overlay, card, title, btn1, btn2, doc };
}

test('getDialogContainer: card resolves to the fixed inset-0 overlay', async () => {
    const { fns, overlay, card } = run('');
    assert.strictEqual(fns.getDialogContainer(card), overlay, 'card did not resolve to the overlay');
    assert.strictEqual(fns.getDialogContainer(overlay), overlay, 'overlay resolves to itself');
    assert.strictEqual(fns.getDialogContainer(null), null);
    assert.strictEqual(fns.getDialogContainer({ nodeType: 8 }), null, 'a text node is not a dialog');
    const orphan = mkEl('div', 'relative');
    assert.strictEqual(fns.getDialogContainer(orphan), orphan, 'no fixed ancestor -> itself');
});

test('modA11yAttach: role/aria-modal/tabindex + aria-labelledby wired to the title', async () => {
    const { fns, overlay, card } = run('block');
    const att = fns.modA11yAttach(card, { role: 'alertdialog' });
    assert.ok(att, 'attach returned nothing');
    assert.strictEqual(att.container, overlay, 'container is the overlay root');
    assert.strictEqual(overlay.attrs.role, 'alertdialog');
    assert.strictEqual(overlay.attrs['aria-modal'], 'true');
    assert.strictEqual(overlay.attrs.tabindex, '-1');
    const heading = fns.getDialogTitle(overlay);
    assert.ok(/^mod-a11y-title-\d+$/.test(heading.id), 'title got no generated id');
    assert.strictEqual(overlay.attrs['aria-labelledby'], heading.id);
});

test('attach/detach symmetry: 5 open/close cycles leak no keydown listener', async () => {
    const r = run('block');
    const before = r.doc.listeners.length;
    for (let i = 0; i < 5; i++) {
        const att = r.fns.modA11yAttach(r.overlay);
        assert.ok(att && att.container === r.overlay, 'attach did not return this container');
        r.fns.modA11yDetach(r.card);
    }
    assert.strictEqual(r.doc.listeners.length, before, 'keydown listeners leaked across cycles');
    assert.strictEqual(r.overlay._modA11y, null, 'detach left _modA11y state behind');
});

test('Escape-to-close: dismissible hides the overlay; dismissible:false does not', async () => {
    const { fns, overlay } = run('block');
    const att = fns.modA11yAttach(overlay, { dismissible: true });
    const evt = { key: 'Escape', shiftKey: false, stopped: false, prevented: false };
    evt.stopPropagation = () => { evt.stopped = true; };
    evt.preventDefault = () => { evt.prevented = true; };
    att.keyHandler(evt);
    assert.ok(evt.stopped, 'Escape did not stop propagation');
    assert.strictEqual(overlay.style.display, '', 'inline display the opener set was not cleared');
    assert.ok(overlay.classList.contains('hidden'), 'hidden class not restored');
    assert.strictEqual(overlay._modA11y, null, 'state not cleared by the Escape path');

    const r = run('block');
    const att2 = r.fns.modA11yAttach(r.overlay, { dismissible: false });
    const evt2 = { key: 'Escape', shiftKey: false, stopped: false, prevented: false };
    evt2.stopPropagation = () => { evt2.stopped = true; };
    evt2.preventDefault = () => { evt2.prevented = true; };
    att2.keyHandler(evt2);
    assert.ok(!evt2.stopped, 'dismissible:false must not stop propagation');
    assert.ok(!r.overlay.classList.contains('hidden'), 'dismissible:false hid the overlay anyway');
});

test('focus trap: Tab on the last focusable wraps to the first; Shift+Tab wraps back', async () => {
    const r = run('block');
    const att = r.fns.modA11yAttach(r.overlay);
    r.doc.activeElement = r.btn2;
    const tab = { key: 'Tab', shiftKey: false, prevented: false };
    tab.preventDefault = () => { tab.prevented = true; };
    att.keyHandler(tab);
    assert.ok(tab.prevented, 'Tab on the last focusable was not trapped');
    assert.ok(r.btn1.focusCount > 0, 'focus did not wrap to the first focusable');

    r.doc.activeElement = r.btn1;
    const shift = { key: 'Tab', shiftKey: true, prevented: false };
    shift.preventDefault = () => { shift.prevented = true; };
    att.keyHandler(shift);
    assert.ok(shift.prevented, 'Shift+Tab on the first focusable was not trapped');
    assert.ok(r.btn2.focusCount > 0, 'Shift+Tab did not wrap to the last focusable');
});

test('isDialogVisible: hidden class / hidden attr / display none all read false', async () => {
    const { fns } = run('');
    const hiddenCls = mkEl('div', 'hidden fixed inset-0');
    assert.strictEqual(fns.isDialogVisible(hiddenCls), false);
    const hiddenAttr = mkEl('div', 'fixed inset-0');
    hiddenAttr.attrs.hidden = '';
    assert.strictEqual(fns.isDialogVisible(hiddenAttr), false);
    const displayNone = mkEl('div', 'fixed inset-0');
    displayNone.style.display = 'none';
    assert.strictEqual(fns.isDialogVisible(displayNone), false);
    const visible = mkEl('div', 'fixed inset-0');
    assert.strictEqual(fns.isDialogVisible(visible), true);
    assert.strictEqual(fns.isDialogVisible(null), false);
});

test('escapeHtml: all five entities (the copyable shell interpolates user text)', async () => {
    const { fns } = run('');
    assert.strictEqual(fns.escapeHtml(`<a href="x" onerror='y'>&`),
        '&lt;a href=&quot;x&quot; onerror=&#039;y&#039;&gt;&amp;');
    assert.ok(!/<[a-z]/.test(fns.escapeHtml('<script>alert(1)</script>')), 'script tag survived');
});
