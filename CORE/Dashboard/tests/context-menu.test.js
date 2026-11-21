// FIX-DASH-WIDGETS-UNTESTED-MODULES-JEST-TESTS — CORE/Dashboard/js/context-menu.js
// (generic right-click context menu). The module is a plain browser script with
// exports, so this test extracts the body (from `let activeMenu = null;` to EOF,
// `export function` -> `function`) and runs it in a vm sandbox with a small DOM
// registry (createElement registers an element when the module assigns `.id`,
// remove() detaches it) plus a stub window viewport, then asserts behaviour:
//   (a) DOM build: one <button> per item, dividers render as their own nodes and
//       consume no button, an icon span is added only when item.icon is set, the
//       label span carries item.label verbatim;
//   (b) disabled items: cursor-not-allowed class, btn.disabled true, and NO click
//       handler (a disabled row must never fire item.action);
//   (c) click on an enabled row hides the menu then runs item.action exactly once;
//   (d) positioning clamps to the viewport: x/y beyond the max clamp to
//       innerWidth-rect.width-8 / innerHeight-rect.height-8, smaller values pass
//       through;
//   (e) dismiss handlers: a click, an Escape keydown and a scroll event each hide
//       the menu; a non-Escape keydown does not;
//   (f) showContextMenu tears down a previous menu first (no stacking);
//   (g) hideContextMenu is idempotent (a second call with nothing open is safe).
//
//   node --test CORE/Dashboard/tests/context-menu.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'context-menu.js'), 'utf8');
const start = src.indexOf('let activeMenu = null;');
assert.ok(start >= 0, 'could not locate the context-menu body');
const block = src.slice(start).replace(/export function/g, 'function');

function run(viewport) {
    const registry = {};
    const body = { children: [] };
    const listeners = {};
    const created = [];

    function mkNode(id, registered) {
        const node = { className: '', value: '', disabled: false, innerHTML: '', textContent: '', style: {}, dataset: {}, children: [] };
        let cur = id || '';
        node.registered = !!registered && !!cur;
        Object.defineProperty(node, 'id', {
            get: () => cur,
            set: (v) => { cur = v; if (v) { registry[v] = node; node.registered = true; } },
        });
        if (cur && registered) registry[cur] = node;
        node.listeners = {};
        node.classList = { add: () => {}, remove: () => {} };
        node.addEventListener = (type, fn) => { node.listeners[type] = fn; listeners[`${cur}:${type}`] = fn; };
        node.querySelectorAll = () => [];
        node.querySelector = () => null;
        // DOM textContent is the concatenation of descendant text: a <button> holding a
        // label span reads its label through textContent (the stub must roll it up).
        node.appendChild = (child) => { node.children.push(child); node.textContent += child.textContent || ''; };
        node.getBoundingClientRect = () => ({ width: 180, height: 120 });
        node.remove = () => {
            const i = body.children.indexOf(node);
            if (i >= 0) body.children.splice(i, 1);
            if (node.registered && cur) delete registry[cur];
        };
        created.push(node);
        return node;
    }

    const sandbox = {
        console: { log: () => {}, warn: () => {}, error: () => {} },
        setTimeout: (fn, ms) => { fn(); },
        window: { innerWidth: viewport.w, innerHeight: viewport.h },
        document: {
            getElementById: (id) => registry[id] || mkNode(id, false),
            createElement: (tag) => mkNode(tag, false),
            querySelectorAll: () => [],
            querySelector: () => null,
            addEventListener: (type, fn) => { listeners[`document:${type}`] = fn; },
            removeEventListener: () => {},
            body: {
                appendChild: (n) => { body.children.push(n); },
                contains: (n) => body.children.includes(n),
            },
        },
    };
    const fns = vm.runInNewContext(
        `"use strict";\n${block}\n({ showContextMenu, hideContextMenu });`,
        sandbox, { filename: 'context-menu-body' });
    return { fns, body, listeners };
}

const actions = { fired: [] };
function items() {
    return [
        { label: 'Open', icon: '<svg/>', action: () => actions.fired.push('open') },
        { divider: true },
        { label: 'Rename', disabled: true, action: () => actions.fired.push('rename') },
        { label: 'Delete', action: () => actions.fired.push('delete') },
    ];
}

test('DOM build: one button per item, dividers separate, icon span only with icon', () => {
    const r = run({ w: 1280, h: 720 });
    r.fns.showContextMenu(10, 10, items());
    const menu = r.body.children[0];
    assert.strictEqual(menu.id, 'context-menu', 'menu id missing');
    assert.ok(menu.className.includes('z-50'), 'menu classes missing');
    const buttons = menu.children.filter((n) => n.disabled === true || n.textContent);
    assert.strictEqual(buttons.length, 3, 'three non-divider items -> three buttons');
    assert.strictEqual(menu.children.length, 4, 'three buttons + one divider node');
    const divider = menu.children.find((n) => n.className.includes('border-t'));
    assert.ok(divider, 'divider node rendered');
    assert.ok(!divider.textContent, 'divider must not carry a label');

    const withIcon = buttons[0];
    assert.ok(withIcon.children.some((c) => c.innerHTML === '<svg/>'), 'icon span rendered');
    assert.strictEqual(withIcon.children[withIcon.children.length - 1].textContent, 'Open',
        'label span carries item.label verbatim');
    const noIcon = buttons[2];
    assert.strictEqual(noIcon.children.length, 1, 'no icon span when item.icon is absent');
    assert.strictEqual(noIcon.children[0].textContent, 'Delete');
});

test('disabled items: not-allowed styling, btn.disabled, no click handler', () => {
    const r = run({ w: 1280, h: 720 });
    actions.fired.length = 0;
    r.fns.showContextMenu(10, 10, items());
    const menu = r.body.children[0];
    const disabledBtn = menu.children.find((n) => n.disabled === true);
    assert.ok(disabledBtn, 'disabled item produced btn.disabled true');
    assert.ok(disabledBtn.className.includes('cursor-not-allowed'), 'not-allowed class missing');
    assert.ok(!disabledBtn.className.includes('hover:bg-gray-100'), 'disabled row must not get hover styling');
    const enabledBtn = menu.children.find((n) => n !== disabledBtn && n.textContent === 'Delete');
    assert.ok(enabledBtn.className.includes('hover:bg-gray-100'), 'enabled row styling missing');
    assert.ok(!disabledBtn.listeners.click, 'a disabled row must not get a click handler');
    assert.ok(enabledBtn.listeners.click, 'an enabled row must get a click handler');
});

test('click on an enabled row hides the menu then runs item.action once', () => {
    const r = run({ w: 1280, h: 720 });
    actions.fired.length = 0;
    r.fns.showContextMenu(10, 10, items());
    const menu = r.body.children[0];
    const enabledBtn = menu.children.find((n) => n.textContent === 'Delete');
    enabledBtn.listeners.click();
    assert.deepStrictEqual(actions.fired, ['delete'], 'action fired exactly once');
    assert.ok(!r.body.children.includes(menu), 'menu hidden before the action ran');
});

test('positioning clamps to the viewport bounds', () => {
    const r = run({ w: 1280, h: 720 });
    r.fns.showContextMenu(10, 10, items());
    const menu = r.body.children[0];
    assert.strictEqual(menu.style.left, '10px', 'small x passes through');
    assert.strictEqual(menu.style.top, '10px', 'small y passes through');

    const r2 = run({ w: 1280, h: 720 });
    r2.fns.showContextMenu(9999, 9999, items());
    const m2 = r2.body.children[0];
    assert.strictEqual(m2.style.left, '1092px', 'x clamps to innerWidth-rect.width-8');
    assert.strictEqual(m2.style.top, '592px', 'y clamps to innerHeight-rect.height-8');
});

test('dismiss handlers: click / Escape / scroll hide the menu, other keys do not', () => {
    const r = run({ w: 1280, h: 720 });
    r.fns.showContextMenu(10, 10, items());
    const menu = r.body.children[0];
    r.listeners['document:keydown']({ type: 'keydown', key: 'a' });
    assert.ok(r.body.children.includes(menu), 'a non-Escape key must not dismiss');
    r.listeners['document:scroll']({ type: 'scroll' });
    assert.ok(!r.body.children.includes(menu), 'scroll must dismiss');

    const r2 = run({ w: 1280, h: 720 });
    r2.fns.showContextMenu(10, 10, items());
    const m2 = r2.body.children[0];
    r2.listeners['document:keydown']({ type: 'keydown', key: 'Escape' });
    assert.ok(!r2.body.children.includes(m2), 'Escape must dismiss');

    const r3 = run({ w: 1280, h: 720 });
    r3.fns.showContextMenu(10, 10, items());
    const m3 = r3.body.children[0];
    r3.listeners['document:click']({ type: 'click' });
    assert.ok(!r3.body.children.includes(m3), 'a click must dismiss');
});

test('showContextMenu tears down a previous menu first (no stacking)', () => {
    const r = run({ w: 1280, h: 720 });
    r.fns.showContextMenu(10, 10, items());
    r.fns.showContextMenu(20, 20, items());
    const menus = r.body.children.filter((n) => n.id === 'context-menu');
    assert.strictEqual(menus.length, 1, 'exactly one menu after a re-open');
});

test('hideContextMenu is idempotent', () => {
    const r = run({ w: 1280, h: 720 });
    r.fns.hideContextMenu();
    r.fns.hideContextMenu();
    assert.strictEqual(r.body.children.length, 0, 'hiding nothing must be safe');
});
