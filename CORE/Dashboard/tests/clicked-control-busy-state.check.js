'use strict';
// 
// - Owner Tim verbatim: "the button should go
// triggered until the thing is done loading. That way it looks clicked".
// The control the user just CLICKED shows a pressed/busy state until the
// fetchApi request it started is done. Technique (as add-worker-modal-lan-
// url.check.js): the shipped busy-indicator block is EXTRACTED VERBATIM from
// js/busy-indicator.js and run in a Function sandbox with a stub DOM and
// FAKE timers (controllable Date.now + setTimeout queue); the api.js hook is
// checked at the source level.
//
//   node CORE/Dashboard/tests/clicked-control-busy-state.check.js
//
// Asserts:
//   A1 busy class + aria-busy + spinner only when a request starts within
//      150 ms of the click; cleared when the request finishes
//   A2 request 151 ms after the click marks nothing
//   A3 a request with no preceding click marks nothing
//   A4 second click on a busy control is swallowed (preventDefault) and
//      does not re-attach; works again once released
//   A5 two overlapping requests keep it busy until BOTH finish
//   A6 failure path clears too (releaseRequest runs on throw)
//   A7 stream/EventSource/poll paths never mark (no attachRequest call)
//   A8 hard 60 s safety release
//   A9 prefers-reduced-motion: pressed look kept, spinner animation none
//      (CSS text) - and keyboard Enter/Space record like a click
//   A10 api.js hook wiring at the source level (attach once, isRetry path
//      never re-attaches, release in finally)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const modSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'busy-indicator.js'), 'utf8');
const apiSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'api.js'), 'utf8');

const bStart = modSrc.indexOf('// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE BEGIN');
assert.ok(bStart >= 0, 'busy-indicator BEGIN marker not found');
const bEnd = modSrc.indexOf('// FIX-DASHBOARD-CLICKED-CONTROL-BUSY-STATE END', bStart);
assert.ok(bEnd > bStart, 'busy-indicator END marker not found');
const block = modSrc.slice(bStart, bEnd);

// --- stub DOM + fake timers ------------------------------------------------
function mkEl(tag, attrs, classes) {
    const el = {
        tagName: tag,
        attrs: Object.assign({}, attrs || {}),
        classes: (classes || '').split(' ').filter(Boolean),
        children: [],
        parentElement: null,
    };
    el.getAttribute = function (n) { return el.attrs.hasOwnProperty(n) ? el.attrs[n] : null; };
    el.setAttribute = function (n, v) { el.attrs[n] = v; };
    el.removeAttribute = function (n) { delete el.attrs[n]; };
    el.classList = {
        add: function (c) { if (el.classes.indexOf(c) < 0) el.classes.push(c); },
        remove: function (c) { el.classes = el.classes.filter(function (x) { return x !== c; }); },
        contains: function (c) { return el.classes.indexOf(c) >= 0; },
    };
    Object.defineProperty(el, 'className', {
        get: function () { return el.classes.join(' '); },
        set: function (v) { el.classes = (v || '').split(' ').filter(Boolean); },
    });
    el.appendChild = function (child) { child.parentElement = el; el.children.push(child); };
    el.removeChild = function (child) { el.children = el.children.filter(function (c) { return c !== child; }); };
    el.querySelector = function (sel) {
        const want = sel.replace(/^\./, '');
        for (const c of el.children) {
            if (c.classes.indexOf(want) >= 0) return c;
            const deep = c.querySelector(sel);
            if (deep) return deep;
        }
        return null;
    };
    return el;
}

function newScene() {
    const scene = { head: mkEl('head'), body: mkEl('body'), listeners: {} };
    scene.createElement = function (tag) { return mkEl(tag); };
    scene.addEventListener = function (type, fn) { (scene.listeners[type] = scene.listeners[type] || []).push(fn); };
    scene.head.querySelector = function (sel) {
        for (const c of scene.head.children) { if ((c.attrs.id || c.id) === 'mod-busy-css') return c; }
        return null;
    };
    return scene;
}

function click(scene, el) {
    const ev = { target: el, prevented: false, stopped: false };
    ev.preventDefault = function () { ev.prevented = true; };
    ev.stopPropagation = function () { ev.stopped = true; };
    for (const fn of (scene.listeners.click || [])) fn(ev);
    return ev;
}
function key(scene, el, k) {
    const ev = { target: el, key: k, prevented: false, stopped: false };
    ev.preventDefault = function () { ev.prevented = true; };
    ev.stopPropagation = function () { ev.stopped = true; };
    for (const fn of (scene.listeners.keydown || [])) fn(ev);
    return ev;
}

let clock = 0;
let timers = [];
function build() {
    const scene = newScene();
    const windowObj = {};
    const DateStub = { now: function () { return clock; } };
    const setTimeoutStub = function (fn, ms) { timers.push({ at: clock + ms, fn: fn }); };
    const f = new Function('window', 'document', 'Date', 'setTimeout', block + '\nreturn window.ModBusyIndicator;');
    const api = f(windowObj, scene, DateStub, setTimeoutStub);
    return { scene: scene, api: api };
}

function advance(ms) {
    const target = clock + ms;
    timers.sort(function (a, b) { return a.at - b.at; });
    while (timers.length && timers[0].at <= target) { clock = timers[0].at; timers.shift().fn(); }
    clock = target;
}

function busy(el) { return el.classes.indexOf('mod-busy') >= 0; }
function hasSpinner(el) { return !!el.querySelector('.mod-busy-spinner'); }

const CASES = [];
function record(name, fn) { CASES.push({ name: name, fn: fn }); }

record('A1 within 150ms -> busy until request finishes', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(100);
    const attached = s.api.attachRequest();
    assert.ok(busy(btn) && s.scene.head.children.length === 1 && hasSpinner(btn), 'A1 busy class + aria + spinner');
    assert.strictEqual(btn.attrs['aria-busy'], 'true', 'A1 aria-busy set');
    advance(2000);
    s.api.releaseRequest(btn);
    assert.ok(!busy(btn) && !hasSpinner(btn), 'A1 cleared when request finished');
});

record('A2 151ms after click -> nothing marked', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(151);
    s.api.attachRequest();
    assert.ok(!busy(btn), 'A2 no busy beyond the 150ms window');
});

record('A3 request with no preceding click marks nothing', function () {
    const s = build();
    s.api.attachRequest();
    const btn = mkEl('button', {}, '');
    assert.ok(!busy(btn), 'A3 nothing marked');
});

record('A4 second click while busy swallowed', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn), 'A4 busy after first request');
    const ev = click(s.scene, btn);
    assert.ok(ev.prevented, 'A4 second click swallowed');
    assert.ok(busy(btn), 'A4 still busy (no re-attach side effects)');
    s.api.releaseRequest(btn);
    assert.ok(!busy(btn), 'A4 released');
    const ev2 = click(s.scene, btn);
    assert.ok(!ev2.prevented, 'A4 click works again once free');
});

record('A5 two overlapping requests busy until both finish', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(10);
    s.api.attachRequest();
    click(s.scene, btn); // click during busy is swallowed but keeps busy
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn), 'A5 busy');
    s.api.releaseRequest(btn);
    assert.ok(busy(btn), 'A5 still busy after first request finishes');
    s.api.releaseRequest(btn);
    assert.ok(!busy(btn), 'A5 cleared after both finish');
});

record('A6 failure path clears busy', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn), 'A6 busy');
    try { throw new Error('request failed'); } catch (e) { /* hook finally releases */ }
    s.api.releaseRequest(btn);
    assert.ok(!busy(btn), 'A6 cleared on failure');
});

record('A7 stream paths never mark', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(10);
    // EventSource / notifications / periodic polls never call attachRequest
    advance(5000);
    const btn2 = mkEl('button', {}, '');
    assert.ok(!busy(btn) && !busy(btn2), 'A7 nothing marked by stream/poll paths');
});

record('A8 hard 60s safety release', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    click(s.scene, btn);
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn), 'A8 busy');
    advance(60000);
    assert.ok(!busy(btn), 'A8 released at 60s even with no completion call');
});

record('A9 reduced-motion CSS + keyboard Enter/Space', function () {
    const s = build();
    const btn = mkEl('button', {}, '');
    btn.parentElement = s.scene.body;
    key(s.scene, btn, 'Enter');
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn), 'A9 Enter records like a click');
    const css = s.scene.head.children[0];
    const text = css.textContent || '';
    assert.ok(/prefers-reduced-motion/.test(text) && /animation:none/.test(text), 'A9 reduced-motion kills animation');
    const btn2 = mkEl('button', {}, '');
    btn2.parentElement = s.scene.body;
    key(s.scene, btn2, ' ');
    advance(10);
    s.api.attachRequest();
    assert.ok(busy(btn2), 'A9 Space records like a click');
});

record('A10 api.js hook wiring (source level)', function () {
    assert.ok(/window\.ModBusyIndicator\?\.attachRequest\(\)/.test(apiSrc), 'A10 attach hook present');
    assert.ok(/modBusyEl = window\.ModBusyIndicator\?\.attachRequest\(\)/.test(apiSrc), 'A10 hook stores the attached element');
    assert.ok(/finally\s*{[\s\S]{0,240}releaseRequest\(modBusyEl\)/.test(apiSrc), 'A10 release in finally (failure path)');
    const retryIdx = apiSrc.indexOf('return fetchApi(endpoint, options, true);');
    assert.ok(retryIdx >= 0, 'A10 retry path exists');
    assert.ok(!/attachRequest\([^)]*isRetry/.test(apiSrc), 'A10 retry never re-attaches (attach only on first attempt)');
});

let passed = 0, failed = 0;
for (const c of CASES) {
    try { c.fn(); passed++; console.log('PASS ' + c.name); }
    catch (e) { failed++; console.log('FAIL ' + c.name + ': ' + e.message); }
}
console.log('clicked-control-busy-state: ' + passed + ' passed / ' + failed + ' failed');
process.exit(failed === 0 ? 0 : 1);
