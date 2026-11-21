'use strict';
// 
// The CE overlay open-core/overlay/CORE/Dashboard/js/ai-config-view-toggle.js
// puts an Easy | Advanced switch on Settings > AI Configuration.
//   node CORE/Dashboard/tests/ai-config-view-toggle.check.js
// Same technique as ce-license-widget-wording.check.js: strip `export`, run in
// a Function sandbox against a tiny DOM stub.
//   A1 default (nothing stored) = easy: table shown, tab strip + panels hidden
//   A2 Advanced click: table hidden, tab strip + panels + intro shown
//   A3 aria-pressed follows the mode on both buttons
//   A4 the choice is written to localStorage and honoured on the next init
//   A5 a non-admin gets no toggle and nothing is hidden
//   A6 a second init does not add a second bar
//   A7 junk stored value falls back to easy
//   A8 manifest wires the import + init call and drops the old stub row
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const REPO = path.join(__dirname, '..', '..', '..');
const src = fs.readFileSync(path.join(REPO, 'open-core', 'overlay',
    'CORE', 'Dashboard', 'js', 'ai-config-view-toggle.js'), 'utf8');
assert.ok(/export\s+function\s+initAiConfigViewToggle\b/.test(src), 'exports init');
const body = src.replace(/export\s+(const|function)/g, '$1');
const mod = new Function(body + '\nreturn { initAiConfigViewToggle, AI_VIEW_KEY };')();

function makeEl(id) {
    const cls = new Set();
    return {
        id, style: { display: '' }, attrs: {}, children: [], listeners: {}, parentNode: null,
        classList: { toggle(c, on) { if (on) cls.add(c); else cls.delete(c); } },
        setAttribute(k, v) { this.attrs[k] = v; },
        addEventListener(t, f) { this.listeners[t] = f; },
        appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
        insertBefore(c) { c.parentNode = this; this.inserted = c; doc_register(c); return c; },
    };
}
let DOC;
function doc_register(bar) {
    DOC.els[bar.id] = bar;
    for (const c of bar.children) DOC.els[c.id] = c;
}
function makeDoc() {
    const root = makeEl('root');
    const els = {};
    for (const id of ['ai-slots-table', 'ai-tool-tabbar', 'ai-intro-advanced']) {
        els[id] = makeEl(id); els[id].parentNode = root;
    }
    const panels = [makeEl('p1'), makeEl('p2')];
    DOC = {
        els, panels, bars: 0,
        getElementById: (id) => els[id] || null,
        querySelectorAll: (sel) => (sel === '.ai-tool-panel' ? panels : []),
        createElement: (t) => { const e = makeEl(''); e.tag = t; return e; },
    };
    root.insertBefore = function (c) { c.parentNode = root; DOC.bars++; doc_register(c); return c; };
    return DOC;
}
function makeStore(init) {
    const m = new Map(Object.entries(init || {}));
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), m };
}
const hidden = (e) => e.style.display === 'none';

// A1
let d = makeDoc(), st = makeStore();
mod.initAiConfigViewToggle(d, st, true);
assert.ok(!hidden(d.els['ai-slots-table']), 'A1 table shown');
assert.ok(hidden(d.els['ai-tool-tabbar']), 'A1 tab strip hidden');
assert.ok(d.panels.every(hidden), 'A1 panels hidden');
assert.ok(hidden(d.els['ai-intro-advanced']), 'A1 advanced intro hidden');
assert.strictEqual(d.els['ai-view-easy'].attrs['aria-pressed'], 'true', 'A3 easy pressed');
// A2 / A3
d.els['ai-view-advanced'].listeners.click();
assert.ok(hidden(d.els['ai-slots-table']), 'A2 table hidden');
assert.ok(!hidden(d.els['ai-tool-tabbar']), 'A2 tab strip shown');
assert.ok(d.panels.every((p) => !hidden(p)), 'A2 panels shown');
assert.ok(!hidden(d.els['ai-intro-advanced']), 'A2 intro shown');
assert.strictEqual(d.els['ai-view-advanced'].attrs['aria-pressed'], 'true', 'A3');
assert.strictEqual(d.els['ai-view-easy'].attrs['aria-pressed'], 'false', 'A3');
// A4
assert.strictEqual(st.m.get(mod.AI_VIEW_KEY), 'advanced', 'A4 stored');
d = makeDoc();
mod.initAiConfigViewToggle(d, st, true);
assert.ok(hidden(d.els['ai-slots-table']) && !hidden(d.els['ai-tool-tabbar']), 'A4 remembered');
d.els['ai-view-easy'].listeners.click();
assert.strictEqual(st.m.get(mod.AI_VIEW_KEY), 'easy', 'A4 back to easy stored');
// A5
d = makeDoc();
assert.strictEqual(mod.initAiConfigViewToggle(d, makeStore(), false), null, 'A5');
assert.ok(!hidden(d.els['ai-slots-table']) && !hidden(d.els['ai-tool-tabbar']), 'A5 untouched');
assert.strictEqual(d.bars, 0, 'A5 no bar');
// A6
d = makeDoc();
mod.initAiConfigViewToggle(d, makeStore(), true);
mod.initAiConfigViewToggle(d, makeStore(), true);
assert.strictEqual(d.bars, 1, 'A6 one bar');
// A7
d = makeDoc();
mod.initAiConfigViewToggle(d, makeStore({ [mod.AI_VIEW_KEY]: 'nonsense' }), true);
assert.ok(!hidden(d.els['ai-slots-table']), 'A7 junk -> easy');
// A8
const man = fs.readFileSync(path.join(REPO, 'open-core', 'manifest.yaml'), 'utf8');
assert.ok(man.includes("import { initAiConfigViewToggle } from './ai-config-view-toggle.js';"), 'A8 import row');
assert.ok(man.includes('initAiConfigViewToggle(document, window.localStorage'), 'A8 init row');
assert.ok(!man.includes("replace: \"    /* CE: no 'Who uses what' slot table"), 'A8 old stub row gone');
console.log('ai-config-view-toggle: all assertions passed');
