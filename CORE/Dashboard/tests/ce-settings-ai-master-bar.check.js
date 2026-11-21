// FIX-DASH-SETTINGS-AI-MASTER-BAR-CE — on CE the System-managed master bar and
// the managed intro are paid surfaces: Settings > AI configuration is
// ADVANCED-ONLY there. Checks _applyMasterMode (extracted from the SHIPPED
// js/settings.js text and evaluated in a vm sandbox with a fake DOM):
//   CE (flag false = CE / unknown / isPaidEdition error, fail closed):
//     bar hidden, managed intro hidden, advanced intro visible, tool tab strip
//     NOT hidden even when the stored mode is 'managed' (the default).
//   PAID: bar visible; managed -> managed intro shown / advanced hidden;
//     advanced -> reverse; every touched element has class lists IDENTICAL to
//     the pre-fix function (frozen copy below) on the same fixture.
//   ORDER RISK: flag false at first apply, then true and re-applied -> correct.
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');
// Frozen copy of _applyMasterMode as of the commit before this fix (git show HEAD:CORE/Dashboard/js/settings.js at 11-11).
const OLD_SRC = "function _applyMasterMode(mode) {\n    const managed = mode !== 'advanced';\n    document.getElementById('ai-master-budget-wrap')\n        ?.classList.toggle('hidden', !managed);\n    // FIX-AICONFIG-IDLE-APPLY-CE — the residency wrap is hidden in CE too, not\n    // only in Advanced: its Apply writes the wholly-Paid /stacks/llm-idle/\n    // settings route, which 402s on CE, so a visible toggle could only half-\n    // apply and then report a Pro-feature error. `_paidEditionKnown` is the\n    // cached verdict refreshed by the loaders (fail closed: unknown = hidden).\n    document.getElementById('ai-master-warm-wrap')\n        ?.classList.toggle('hidden', !managed || !_paidEditionKnown);\n    document.getElementById('ai-master-advanced-note')\n        ?.classList.toggle('hidden', managed);\n\n    // Per-tool controls the master bar now owns. Left visible in System\n    // managed they are a second control for a decision already made, and two\n    // controls for one decision is how they end up disagreeing about what\n    // was actually applied.\n    document.querySelectorAll('[data-advanced-only]').forEach((el) => {\n        el.classList.toggle('hidden', managed);\n    });\n\n    // ai-configuration-page redesign — the four AI tools are now tabs in the\n    // strip (tools first), alongside the infrastructure tabs. The strip belongs\n    // to Advanced; System managed hides it and every panel entirely.\n    document.getElementById('ai-tool-tabbar')?.classList.toggle('hidden', managed);\n    document.querySelectorAll('.ai-tool-panel').forEach((el) => {\n        el.classList.add('hidden');\n    });\n    document.getElementById('ai-intro-managed')?.classList.toggle('hidden', !managed);\n    document.getElementById('ai-intro-advanced')?.classList.toggle('hidden', managed);\n\n    const modeHelp = document.getElementById('ai-master-mode-help');\n    if (modeHelp) {\n        modeHelp.textContent = managed\n            ? 'MOD picks the models and points every tool at them.'\n            : 'Choose a model per tool yourself. Every tab is shown.';\n    }\n    // The hardware tier stays READABLE in managed mode — the user chose a\n    // budget and should see what it bought — but not editable, because the\n    // master bar is where that choice is made.\n    document.querySelectorAll('.ai-tool-form [data-field=\"local_preset\"], '\n        + '.ai-tool-form [data-field=\"local_role\"]').forEach((el) => {\n        el.disabled = managed;\n        el.classList.toggle('bg-gray-100', managed);\n        el.title = managed\n            ? 'Set by the System managed selection above.' : '';\n    });\n\n    _ADVANCED_ONLY_TABS.forEach((tool) => {\n        const btn = document.querySelector(`.ai-tool-tab[data-tool=\"${tool}\"]`)\n            || document.getElementById(`ai-tool-tab-${tool}`);\n        if (!btn) return;\n        if (managed) {\n            btn.classList.add('hidden');\n        } else if (!btn.dataset.adminOnly && !btn.dataset.deliberateHide\n            && (tool !== 'resource-monitor' || _paidEditionKnown)) {\n            // Admin-gated tabs are revealed by their own init; this must not\n            // un-hide a tab the role check deliberately hid.\n            btn.classList.remove('hidden');\n        }\n    });\n\n    // Never leave an empty bordered strip: if the role check hid every tab, a\n    // user could see an empty bordered strip. Hide it unless something shows.\n    if (!managed) {\n        const tb = document.getElementById('ai-tool-tabbar');\n        const anyTab = tb && tb.querySelector('.ai-tool-tab:not(.hidden)');\n        tb?.classList.toggle('hidden', !anyTab);\n        // Default to the first visible tab (Node Wizard) so a panel is shown\n        // rather than a bare tab strip over empty space.\n        if (anyTab && !document.querySelector('.ai-tool-panel:not(.hidden)')) {\n            const first = tb.querySelector('.ai-tool-tab:not(.hidden)');\n            if (first) _switchTab(first.dataset.tool);\n        }\n    }\n\n    localStorage.setItem(AI_MASTER_MODE_KEY, mode);\n}\n";

function extractFn(src, name) {
    const start = src.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `${name} not found`);
    const end = src.indexOf('\n}\n', start);
    assert.ok(end > start, `${name} end not found`);
    return src.slice(start, end + 3);
}
const NEW_SRC = extractFn(SRC, '_applyMasterMode');
const TABS = ['wizard-learning', 'resource-monitor'];

function el(id, cls) {
    const set = new Set(cls ? cls.split(' ') : []);
    return {
        id, dataset: {}, disabled: false, title: '', textContent: '',
        classList: {
            add: (c) => set.add(c), remove: (c) => set.delete(c),
            toggle(c, force) {
                const on = force === undefined ? !set.has(c) : !!force;
                if (on) set.add(c); else set.delete(c);
                return on;
            },
            contains: (c) => set.has(c),
        },
        snapshot: () => [...set].sort().join(' '),
    };
}

// Fixture DOM matching settings.html defaults: bar and managed intro visible,
// advanced intro + tool tab strip hidden, one visible tab, one panel.
function makeDom() {
    const byId = {};
    ['ai-master-bar', 'ai-intro-managed', 'ai-master-budget-wrap',
        'ai-master-warm-wrap', 'ai-master-mode-help'].forEach((i) => { byId[i] = el(i, ''); });
    ['ai-intro-advanced', 'ai-tool-tabbar', 'ai-master-advanced-note'].forEach((i) => { byId[i] = el(i, 'hidden'); });
    const advOnly = el('adv1', '');
    const panels = [el('p1', 'ai-tool-panel')];
    const tabs = {};
    TABS.forEach((t) => { tabs[t] = el('tab-' + t, 'ai-tool-tab hidden'); tabs[t].dataset.tool = t; });
    const allTabs = Object.values(tabs);
    const tabbar = byId['ai-tool-tabbar'];
    tabbar.querySelector = (sel) => {
        assert.strictEqual(sel, '.ai-tool-tab:not(.hidden)');
        return allTabs.find((t) => !t.classList.contains('hidden')) || null;
    };
    const document = {
        getElementById: (i) => byId[i] || null,
        querySelectorAll(sel) {
            if (sel === '[data-advanced-only]') return [advOnly];
            if (sel === '.ai-tool-panel') return panels;
            return [];
        },
        querySelector(sel) {
            if (sel === '.ai-tool-panel:not(.hidden)') return panels.find((p) => !p.classList.contains('hidden')) || null;
            const m = /data-tool="([^"]+)"/.exec(sel);
            return m ? (tabs[m[1]] || null) : null;
        },
    };
    return { byId, advOnly, panels, tabs, document };
}

function run(fnSrc, mode, paid, preMode) {
    const dom = makeDom();
    const store = {};
    const switched = [];
    const sb = {
        document: dom.document,
        localStorage: { setItem: (k, v) => { store[k] = v; }, getItem: (k) => store[k] },
        AI_MASTER_MODE_KEY: 'mod_ai_master_mode',
        _ADVANCED_ONLY_TABS: TABS,
        _paidEditionKnown: false,
        _switchTab: (t) => { switched.push(t); },
    };
    vm.createContext(sb);
    vm.runInContext(fnSrc, sb);
    if (preMode !== undefined) { sb._paidEditionKnown = preMode; vm.runInContext(`_applyMasterMode(${JSON.stringify(mode)})`, sb); }
    sb._paidEditionKnown = paid;
    vm.runInContext(`_applyMasterMode(${JSON.stringify(mode)})`, sb);
    return { dom, store, switched };
}
function snap(dom) {
    const o = {};
    Object.keys(dom.byId).forEach((k) => { o[k] = dom.byId[k].snapshot(); });
    o.advOnly = dom.advOnly.snapshot();
    o.panel = dom.panels[0].snapshot();
    TABS.forEach((t) => { o['tab:' + t] = dom.tabs[t].snapshot(); });
    return o;
}
const hid = (d, id) => d.byId[id].classList.contains('hidden');
let n = 0;
const ok = (c, m) => { assert.ok(c, m); n += 1; };

// --- CE: stored mode managed (default) and advanced; fail closed ------------
['managed', 'advanced'].forEach((mode) => {
    const { dom } = run(NEW_SRC, mode, false);
    ok(hid(dom, 'ai-master-bar'), `CE/${mode}: master bar must be hidden`);
    ok(hid(dom, 'ai-intro-managed'), `CE/${mode}: managed intro must be hidden`);
    ok(!hid(dom, 'ai-intro-advanced'), `CE/${mode}: advanced intro must show`);
    ok(!hid(dom, 'ai-tool-tabbar'), `CE/${mode}: tool tab strip must NOT be hidden`);
    ok(!dom.advOnly.classList.contains('hidden'), `CE/${mode}: advanced-only controls show`);
    ok(!dom.tabs['wizard-learning'].classList.contains('hidden'), `CE/${mode}: tool tab shown`);
});
// resource-monitor tab stays hidden on CE (existing paid-only tab gate)
ok(run(NEW_SRC, 'managed', false).dom.tabs['resource-monitor'].classList.contains('hidden'),
    'CE: resource-monitor tab stays hidden');

// --- PAID behaviour --------------------------------------------------------
let p = run(NEW_SRC, 'managed', true).dom;
ok(!hid(p, 'ai-master-bar'), 'paid/managed: bar visible');
ok(!hid(p, 'ai-intro-managed') && hid(p, 'ai-intro-advanced'), 'paid/managed: managed intro shown');
ok(hid(p, 'ai-tool-tabbar'), 'paid/managed: tab strip hidden (system managed)');
p = run(NEW_SRC, 'advanced', true).dom;
ok(!hid(p, 'ai-master-bar'), 'paid/advanced: bar visible');
ok(hid(p, 'ai-intro-managed') && !hid(p, 'ai-intro-advanced'), 'paid/advanced: advanced intro shown');
ok(!hid(p, 'ai-tool-tabbar'), 'paid/advanced: tab strip shown');

// --- paid DOM identical to the pre-fix function ----------------------------
['managed', 'advanced'].forEach((mode) => {
    const a = run(OLD_SRC, mode, true);
    const b = run(NEW_SRC, mode, true);
    assert.deepStrictEqual(snap(b.dom), snap(a.dom), `paid/${mode}: class lists differ from old function`);
    assert.deepStrictEqual(b.store, a.store, `paid/${mode}: stored mode differs`);
    assert.deepStrictEqual(b.switched, a.switched, `paid/${mode}: tab switch differs`);
    n += 3;
});

// --- order risk: flag false at the first apply, resolved true, re-applied --
['managed', 'advanced'].forEach((mode) => {
    const early = run(NEW_SRC, mode, true, false);  // false apply, then true re-apply
    const ref = run(NEW_SRC, mode, true);
    assert.deepStrictEqual(snap(early.dom), snap(ref.dom), `order/${mode}: paid not correct after re-apply`);
    n += 1;
});
// and the re-apply is really wired into the change handler in the shipped source
ok(/_wasKnownPaid/.test(SRC) && /if \(_paidNow && !_wasKnownPaid\) _applyMasterMode\(newMode\)/.test(SRC),
    'change handler must re-apply once when the paid flag first resolves');
// gate lines present in the shipped source
ok(/getElementById\('ai-master-bar'\)\?\.classList\.toggle\('hidden', !_paidEditionKnown\)/.test(SRC), 'bar gate line present');
console.log(`ce-settings-ai-master-bar.check: ${n} assertions passed`);
