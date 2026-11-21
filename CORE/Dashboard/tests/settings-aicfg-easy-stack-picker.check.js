'use strict';
// D75 (owner Tim, Gate 7 sitting 3, 11-17 ~8:00 PM CT): "in EASY mode there is
// no way to hook directly into a stack". The Easy "Who uses what" rows must
// READ as a stack picker and must save through the SAME binding call the
// Advanced tab's forms use (PUT /platform/ai-config/bindings/{slot}).
//
// Same technique as settings-aicfg-ce-gate.check.js: the shipped code is
// EXTRACTED VERBATIM from settings.js and run against a stub DOM, so the
// assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/settings-aicfg-easy-stack-picker.check.js
//
// Asserts: one labelled picker per slot row; options come from the §7.1 stacks
// read filtered by the API's own kind (an embedding slot never offers a chat
// stack); a row with no stack serving it says what to do; choosing a stack
// calls _aiSlotSave, which PUTs the slot with target_kind 'stack' + stack_id.
const fs = require('fs');
const path = require('path');
const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

let failures = 0;
function check(name, cond, extra) {
    if (cond) console.log(`ok   ${name}`);
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}
function blockAt(src, open) {
    if (src[open] !== '{') throw new Error('not an open brace');
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
        if (src[i] === '{') depth += 1;
        else if (src[i] === '}') { depth -= 1; if (!depth) return src.slice(open, i + 1); }
    }
    throw new Error('unbalanced braces');
}
function fnSrc(hdr) {
    const at = settingsSrc.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in settings.js`);
    return `${hdr} ${blockAt(settingsSrc, settingsSrc.indexOf('{', at + hdr.length))}`;
}

// A0 — red-before guard: the old file (no affordance) fails here.
const PINS = [
    "pickLabel.dataset.role = 'slot-picker-label';",
    "pickLabel.textContent = 'Use a stack…';",
    "none.dataset.role = 'slot-no-stacks';",
    "'No stacks yet: create one on the Stacks tab, then pick it here.'",
    'const stackOptions = _aiSlotFillOptions(sel, capability);',
];
PINS.forEach((p, i) => check(`A0.${i + 1} shipped source carries the picker affordance`,
    settingsSrc.includes(p), p));

const PICK_START = "        const pickLabel = document.createElement('div');";
const PICK_END = "            row.appendChild(none);\n        }";
const pickStart = settingsSrc.indexOf(PICK_START);
const pickEnd = settingsSrc.indexOf(PICK_END);
check('A1 the picker block is present verbatim', pickStart >= 0 && pickEnd > pickStart, 'renderOne');

const fillSrc = fnSrc('function _aiSlotFillOptions(sel, capability)');
const statusSrc = fnSrc('function _aiStackStatusWord(status)');
const saveSrc = fnSrc('async function _aiSlotSave(slot, value, priority = 0)');
check('A2 the save helper is the existing bindings PUT',
    saveSrc.includes('/platform/ai-config/bindings/${encodeURIComponent(slot)}')
    && saveSrc.includes("body.target_kind = 'stack'") && saveSrc.includes('body.stack_id'),
    'no new endpoint');

const pickerBlock = pickStart >= 0 && pickEnd > pickStart
    ? settingsSrc.slice(pickStart, pickEnd + PICK_END.length) : '';

// --- stub DOM + stub state; the shipped code runs against these.
const puts = [];
const env = {
    _aiStacksLast: { stacks: [
        { stack_id: 41, name: 'My Chat Stack', kind: 'chat', status: 'running', served_models: ['a', 'b'] },
        { stack_id: 77, name: 'My Embed Stack', kind: 'embedding', status: 'stopped', served_models: ['e'] },
    ] },
    _aiSlotExternals: [],
    _aiSlotChosen: {},
    _aiSlotChosenAt: {},
    _paidEditionKnown: false,
    _masterFetch: (url, opts) => { puts.push({ url, opts }); return { ok: true }; },
    _aiSlotApplyResolution: () => {},
    _aiSlotsNotice: (d) => { env.notice = d; },
    document: {
        createElement: (tag) => {
            const e = { tag, dataset: {}, className: '', textContent: '', options: [], value: '' };
            e.children = [];
            e.appendChild = (child) => { e.children.push(child); if (tag === 'select') e.options.push(child); };
            e.querySelectorAll = () => [];
            e.classList = { add: (c) => { e.className += ' ' + c; }, remove: () => {} };
            return e;
        },
    },
};
const names = Object.keys(env);
const prelude = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ');
const mkRow = () => env.document.createElement('div');
const saveFn = new Function('__env', `${prelude} ${statusSrc} ${fillSrc} ${saveSrc}
    return (async (slot, value, priority) => await _aiSlotSave(slot, value, priority));`);

(async () => {
    if (pickStart < 0 || pickEnd < 0) {
        console.log(`FAILURES ${failures}`); process.exitCode = 1; return;
    }
    const runPicker = new Function('__env', `${prelude} ${statusSrc} ${fillSrc}
        return (async (row, slot, capability) => { ${pickerBlock} });`);

    const row = mkRow('div');
    await runPicker(env)(row, 'assistant', 'chat');
    const label = row.children.find((c) => c.dataset.role === 'slot-picker-label');
    const sel = row.children.find((c) => c.tag === 'select');
    const none = row.children.find((c) => c.dataset.role === 'slot-no-stacks');
    check('A3 the row carries the Use a stack… label',
        label && label.textContent === 'Use a stack…', label && label.textContent);
    check('A4 the picker is tagged with the slot', sel && sel.dataset.slot === 'assistant'
        && sel.dataset.field === 'slot_binding', sel && sel.dataset);
    const chatOpts = sel.options.filter((o) => o.value && o.value !== 'auto');
    check('A5 chat slot offers only chat stacks',
        chatOpts.length === 1 && chatOpts[0].value === 'stack:41', chatOpts.map((o) => o.value));
    check('A6 no stack line absent when stacks exist', !none, 'assistant row');

    const row2 = mkRow('div');
    await runPicker(env)(row2, 'assistant_knowledge_embed', 'embedding');
    const sel2 = row2.children.find((c) => c.tag === 'select');
    const embOpts = sel2.options.filter((o) => o.value && o.value !== 'auto');
    check('A7 embedding slot offers only embedding stacks',
        embOpts.length === 1 && embOpts[0].value === 'stack:77', embOpts.map((o) => o.value));

    env._aiStacksLast = { stacks: [] };
    const row3 = mkRow('div');
    await runPicker(env)(row3, 'wizard', 'chat');
    const none3 = row3.children.find((c) => c.dataset.role === 'slot-no-stacks');
    check('A8 empty state says what to do',
        none3 && none3.textContent === 'No stacks yet: create one on the Stacks tab, then pick it here.',
        none3 && none3.textContent);

    const aiSlotSave = saveFn(env);
    await aiSlotSave('assistant', 'stack:41', 0);
    const p = puts.find((x) => x.url.includes('/platform/ai-config/bindings/assistant'));
    check('A9 choosing a stack PUTs the slot binding',
        p && p.opts.method === 'PUT' && p.opts.body.includes('"target_kind":"stack"')
        && p.opts.body.includes('"stack_id":41'), p && p.opts.body);
    check('A10 no api_key or key_ref ever sent from here',
        !puts.some((x) => x.opts.body.includes('api_key') || x.opts.body.includes('key_ref')), 'save path');

    console.log(failures ? `FAILURES ${failures}` : 'settings-aicfg-easy-stack-picker: all assertions passed');
    process.exitCode = failures ? 1 : 0;
})();
