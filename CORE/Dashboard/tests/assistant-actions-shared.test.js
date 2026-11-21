// rc20 sz-mod-assistant-agent.2 (Dashboard half) — the Settings -> AI -> Agent
// action-allowlist UX renders its checkboxes from the SHARED action runtime
// (js/shared/assistant-actions.js), which re-exports the canonical
// js/vendor/assistant-nav.js (generated from navActions.ts), instead of
// settings.js carrying its own hardcoded vocabulary that can drift from the
// runtime's KNOWN_ACTION_TYPES (a stale name renders a checkbox the user can
// tick, and the client then drops that action silently — a dead admin control).
//
// Same technique as assistant-confirm-dialog.test.js / settings-aicfg-ce-gate.check.js:
// the shipped sources are READ FROM DISK and run in a node:vm sandbox with a stub
// window, so the assertions test what ships, not a copy.
//
//   node --test CORE/Dashboard/tests/assistant-actions-shared.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');
const child = require('node:child_process');

const VENDOR_REL = path.join('js', 'vendor', 'assistant-nav.js');
const SHARED_REL = path.join('js', 'shared', 'assistant-actions.js');
const SETTINGS_REL = path.join('js', 'settings.js');
const vendorSrc = fs.readFileSync(path.join(__dirname, '..', VENDOR_REL), 'utf8');
const sharedSrc = fs.readFileSync(path.join(__dirname, '..', SHARED_REL), 'utf8');
const settingsSrc = fs.readFileSync(path.join(__dirname, '..', SETTINGS_REL), 'utf8');

// The shared module is an ES module (settings.js imports it as a side effect), so
// its `export` keywords are the only thing that stops a vm script from running it:
// strip them and run the shipped bodies verbatim.
const sharedScript = sharedSrc.replace(/^export function /gm, 'function ');

// node --check, module mode (the bug-b2-held-matches-import.check.js idiom).
function checkModule(src, label) {
    let err = null;
    try {
        child.execFileSync(process.execPath,
            ['--input-type=module', '--check'],
            { input: src, encoding: 'utf8' });
    } catch (e) {
        err = String(e && (e.stderr || e.message));
    }
    assert.ok(!err, label + ': not valid as an ES module — ' + (err || 'unknown').split('\n')[0]);
}

// A sandbox whose `window` carries the vendor runtime, then the shared module.
function loadRuntime() {
    const stubWindow = {};
    const ctx = vm.createContext({ window: stubWindow, console: { warn: () => {} } });
    vm.runInNewContext(vendorSrc, ctx);
    assert.ok(stubWindow.ModAssistantNav, 'vendor did not set window.ModAssistantNav');
    vm.runInNewContext(sharedScript, ctx);
    assert.ok(stubWindow.ModAssistantActions, 'shared module did not publish window.ModAssistantActions');
    return { shared: stubWindow.ModAssistantActions, canonical: stubWindow.ModAssistantNav };
}

// A sandbox with NO vendor runtime — the shared module must fail closed.
function loadWithoutRuntime() {
    const stubWindow = {};
    const ctx = vm.createContext({ window: stubWindow, console: { warn: () => {} } });
    vm.runInNewContext(sharedScript, ctx);
    return stubWindow.ModAssistantActions;
}

// Slice one top-level (col-0) function out of settings.js.
function takeSettingsFn(header) {
    const at = settingsSrc.indexOf(header);
    assert.ok(at >= 0, 'settings.js is missing ' + header);
    const rest = settingsSrc.slice(at + header.length);
    const nxt = rest.search(/\n(?:async )?function |\nconst |\nlet |\nvar /);
    assert.ok(nxt >= 0, 'could not find the end of ' + header);
    return settingsSrc.slice(at, at + nxt);
}

// ---------------------------------------------------------------------------
test('shared module is valid JS and exposes the surface', async () => {
    checkModule(sharedSrc, SHARED_REL);
    child.execFileSync(process.execPath, ['--check', path.join(__dirname, '..',
        'tests', 'assistant-actions-shared.test.js')]);
    const { shared } = loadRuntime();
    for (const name of ['actionVocabulary', 'validateAction', 'isKnownActionType',
        'executeAction', 'available']) {
        assert.ok(typeof shared[name] === 'function',
            'window.ModAssistantActions is missing ' + name);
    }
});

test('vocabulary comes from the canonical runtime, not a local list', async () => {
    const { shared, canonical } = loadRuntime();
    // Realm-safe compare: the array comes from the vm sandbox, so compare its
    // joined contents, not the array object itself.
    assert.strictEqual(shared.actionVocabulary().join(','),
        'navigate,open-new-tab,open-dialog,open-tab,scroll-to,highlight,chain',
        'the toggleable vocabulary must be the runtime set minus support-contacted');
    for (const name of shared.actionVocabulary()) {
        assert.ok(canonical.isKnownActionType(name),
            'every shipped name must be known to the canonical runtime: ' + name);
    }
    assert.ok(canonical.isKnownActionType('support-contacted'),
        'support-contacted is runtime-known but must not be offered to the admin');
    assert.ok(!shared.actionVocabulary().includes('support-contacted'),
        'a non-toggleable receipt payload must not render a checkbox');
});

test('unknown action names are rejected', async () => {
    const { shared } = loadRuntime();
    const vocab = shared.actionVocabulary();
    for (const hostile of ['delete-everything', 'navigate; alert(1)', 'javascript:']) {
        assert.ok(!vocab.includes(hostile), 'vocabulary must not carry ' + hostile);
    }
    assert.strictEqual(shared.isKnownActionType('delete-everything'), false,
        'unknown action type must be rejected');
});

test('validateAction fail-closed without the runtime', async () => {
    const shared = loadWithoutRuntime();
    assert.strictEqual(shared.available(), false, 'no runtime -> not available');
    assert.strictEqual(shared.validateAction({ action: 'navigate', url: '/billing' }), null,
        'no runtime -> no action');
    assert.strictEqual(shared.executeAction({ action: 'navigate', url: '/billing' }), null,
        'no runtime -> no execution');
    assert.strictEqual(shared.actionVocabulary().length, 0,
        'no runtime -> empty vocabulary (caller falls back)');
});

test('hostile navigate payloads are dropped', async () => {
    const { shared } = loadRuntime();
    assert.strictEqual(shared.validateAction({ action: 'navigate', url: '//evil.example.com/x' }),
        null, 'protocol-relative route must be dropped');
    assert.strictEqual(shared.validateAction({ action: 'navigate', url: 'https://evil/' }),
        null, 'absolute/scheme route must be dropped');
    assert.strictEqual(shared.validateAction({ action: 'navigate', url: 'javascript:alert(1)' }),
        null, 'javascript: url must be dropped');
    assert.deepStrictEqual(shared.validateAction({ action: 'navigate', url: '/billing' }),
        { action: 'navigate', url: '/billing' }, 'same-origin route passes through unchanged');
});

test('chain children are pruned, not trusted', async () => {
    const { shared } = loadRuntime();
    const out = shared.validateAction({
        action: 'chain',
        actions: [
            { action: 'navigate', url: '/billing' },
            { action: 'navigate', url: '//evil' },
        ],
    });
    assert.ok(out, 'a chain with one valid child survives');
    assert.strictEqual(out.actions.length, 1, 'the invalid child must be pruned, not passed through');
    assert.deepStrictEqual(out.actions[0], { action: 'navigate', url: '/billing' });
});

test('settings.js uses the shared vocabulary and has no CE gate (owner 11-10: Agent tab shown in CE)', async () => {
    const block = takeSettingsFn('async function _initAgentSubTab()');
    assert.ok(block.includes('window.ModAssistantActions'),
        'the call site must go through the shared runtime');
    assert.ok(block.includes('actionVocabulary()'), 'must ask the runtime for the list');
    assert.ok(block.includes('_AGENT_ACTION_VOCABULARY_FALLBACK'),
        'the only literal list must be the named fallback');
    assert.ok(!block.includes('isPaidEdition'),
        'the Agent sub-tab must no longer carry a CE edition gate (owner 11-10 "Show them in CE")');
    assert.ok(!block.includes('deliberateHide'),
        'the Agent sub-tab must no longer stamp the deliberate-hide marker');
    assert.ok(!/\.innerHTML/.test(block), 'the agent sub-tab renders text only');
    assert.ok(settingsSrc.includes("import './shared/assistant-actions.js';"),
        'settings.js must side-effect import the shared module');
    assert.ok(fs.existsSync(path.join(__dirname, '..', SHARED_REL)),
        'js/shared/assistant-actions.js must ship');
});
