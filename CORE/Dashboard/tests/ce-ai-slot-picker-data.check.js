// FIX-CE-AI-SLOT-PICKER-LOAD-STACKS-HIDDEN — on CE the AI stacks DATA must load
// (slot pickers read _aiStacksLast) while #ai-stacks-section stays hidden.
// Checks: (1) source: the load is no longer behind `if (!_ceNoAiCfg)` and the
// un-hide lines are guarded; (2) behaviour: the real _renderAiStacksSection /
// _aiStacksNotice / _loadAiStacksSection run in a vm with stubs.
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

// --- 1) source pins ---
assert.ok(!/if \(!_ceNoAiCfg\) \{\s*await _loadAiStacksSection\(\);/.test(src),
    'the stacks data load must not be gated on !_ceNoAiCfg');
assert.ok(/_aiStacksKeepHidden = _ceNoAiCfg;\s*try \{\s*await _loadAiStacksSection\(\);/.test(src),
    'loadAIConfig must run the data load on CE with the keep-hidden flag set');
assert.ok(/if \(!_aiStacksKeepHidden\) box\.classList\.remove\('hidden'\)/.test(src),
    'render must not un-hide the section on CE');
assert.ok(/!had && !_aiStacksKeepHidden\) box\.classList\.remove\('hidden'\)/.test(src),
    'notice must not un-hide the section on CE');
assert.ok(/'ai-stacks-section'[\s\S]{0,200}classList\.add\('hidden'\)/.test(src),
    'the CE hide of ai-stacks-section must remain');

// --- 2) behaviour ---
function grab(startRe, endMarker) {
    const m = startRe.exec(src);
    assert.ok(m, 'cannot find ' + startRe);
    const end = src.indexOf(endMarker, m.index);
    assert.ok(end > m.index, 'cannot find end ' + endMarker);
    return src.slice(m.index, end);
}
const code = [
    grab(/function _renderAiStacksSection\(/, '// One-line inline notice'),
    grab(/function _aiStacksNotice\(/, 'async function _loadAiStacksSection'),
    grab(/async function _loadAiStacksSection\(/, '// The "Persistent Stacks row" link'),
].join('\n');

function mkBox() {
    const cls = new Set(['hidden']);
    return {
        innerHTML: '', cls,
        classList: { add: (c) => cls.add(c), remove: (c) => cls.delete(c) },
        appendChild() {}, querySelector: () => null,
    };
}

async function run({ keepHidden, ok }) {
    const box = mkBox();
    const ctx = {
        _aiStacksLast: null, _aiStacksKeepHidden: keepHidden,
        currentUser: { is_platform_admin: true },
        _authToken: () => 'tok', window: { APP_CONFIG: { API_URL: '' } },
        document: {
            getElementById: (id) => (id === 'ai-stacks-section' ? box : null),
            createElement: () => ({ setAttribute() {}, outerHTML: '<p></p>', set className(v) {}, set textContent(v) {} }),
        },
        _esc: (x) => String(x), _aiStackStatusWord: () => 's', _aiStackDot: () => 'd',
        _aiStackSlotChip: () => '', Promise, Array, String,
        // The retry delay added in 6f0f6c375b calls setTimeout; the vm sandbox
        // must provide it (shadowed to run the callback immediately, same as
        // settings-stack-picker-retry-d75.check.js).
        setTimeout: (fn, ms) => { fn(); return 0; }, clearTimeout: () => 0,
        fetch: async (url) => {
            if (!ok) return { ok: false, status: 404, json: async () => ({}) };
            const body = url.endsWith('/ai-stacks')
                ? { stacks: [{ stack_id: 1, name: 'vllm-a', model: 'm', served_models: ['m'] }] }
                : { unbound_slots: [] };
            return { ok: true, status: 200, json: async () => body };
        },
    };
    vm.createContext(ctx);
    vm.runInContext(code + '\nthis._load = _loadAiStacksSection;', ctx);
    await ctx._load();
    return { last: vm.runInContext('_aiStacksLast', ctx), hidden: box.cls.has('hidden') };
}

(async () => {
    let r = await run({ keepHidden: true, ok: true });
    assert.ok(r.last && r.last.stacks.length === 1, 'CE: data load must fill _aiStacksLast');
    assert.strictEqual(r.hidden, true, 'CE: section must stay hidden after a successful load');
    r = await run({ keepHidden: true, ok: false });
    assert.strictEqual(r.last, null, 'CE: failed fetch leaves _aiStacksLast null (no throw)');
    assert.strictEqual(r.hidden, true, 'CE: section stays hidden after a failed load');
    r = await run({ keepHidden: false, ok: true });
    assert.ok(r.last, 'paid: data loads');
    assert.strictEqual(r.hidden, false, 'paid: section is shown (unchanged)');
    r = await run({ keepHidden: false, ok: false });
    assert.strictEqual(r.hidden, false, 'paid: failure notice un-hides (unchanged)');
    console.log('ce-ai-slot-picker-data: ok');
})().catch((e) => { console.error(e); process.exit(1); });
