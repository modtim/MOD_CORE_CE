'use strict';
// D76 (owner Tim, PIN7 sitting 4): "Dashboard -> AI Configuration, Easy mode.
// Each slot row says 'Use a stack...' but the dropdown never lists the stacks,
// so no stack can be selected (it does not poll the stacks)".
//
// Root cause: the Easy picker filled ONLY from §7.1's ai-stacks read
// (`_aiStacksLast`), and routers/ai_config.py:3318 drops a stack that serves
// no model and is bound by nothing (`if not mine and not served: continue`).
// The Dell CE install has exactly one stack ("asdrfasd": running, its compose
// command declares no --served-model-name, zero bindings) -> ai-stacks returns
// stacks: [] while /stacks and /platform/ai-config/stack-options both return
// it. So the row said "Use a stack..." over an empty dropdown.
//
// Same technique as settings-aicfg-easy-stack-picker.check.js: the shipped
// code is EXTRACTED VERBATIM from settings.js and run against a stub DOM, so
// the assertions test the shipped source, not a copy.
//
//   node CORE/Dashboard/tests/settings-easy-stack-picker-owns-stacks-d76.check.js
const fs = require('fs');
const path = require('path');
const settingsSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

let failures = 0;
function check(name, cond, extra) {
    if (cond) console.log(`ok   ${name}`);
    else { failures++; console.error(`FAIL ${name}`, extra !== undefined ? extra : ''); }
}
function fnSrc(hdr) {
    const at = settingsSrc.indexOf(hdr);
    if (at < 0) throw new Error(`${hdr} not found in settings.js`);
    const brace = settingsSrc.indexOf('{', at + hdr.length);
    let depth = 0;
    for (let i = brace; i < settingsSrc.length; i += 1) {
        if (settingsSrc[i] === '{') depth += 1;
        else if (settingsSrc[i] === '}') { depth -= 1; if (!depth) return `${hdr} ${settingsSrc.slice(brace, i + 1)}`; }
    }
    throw new Error('unbalanced braces');
}

// A0 — red-before guard: the unedited tree fills the picker from
// `_aiStacksLast` ONLY, so it carries none of these needles.
const NEEDLES = [
    "typeof _stackOptions === 'undefined' ? [] : _stackOptions",
    'if (!byId.has(sid)) byId.set(sid, o);',
];
NEEDLES.forEach((p, i) => check(`A0.${i + 1} shipped source merges the stack-options read`,
    settingsSrc.includes(p), p));

const fillSrc = fnSrc('function _aiSlotFillOptions(sel, capability)');
const statusSrc = fnSrc('function _aiStackStatusWord(status)');
const labelSrc = fnSrc('function _aiSlotValueLabel(value)');

function mkSel() {
    const e = { tag: 'select', dataset: {}, className: '', value: '', options: [] };
    e.appendChild = (child) => { e.options.push(child); };
    return e;
}
function mkDoc() {
    return {
        createElement: (tag) => {
            const e = { tag, dataset: {}, className: '', textContent: '', value: '' };
            e.appendChild = (c) => { e.children = (e.children || []).concat([c]); };
            return e;
        },
    };
}

// The Dell's exact shape: ai-stacks answers ZERO stacks, stack-options answers
// the one stack the user owns (kind "chat" from the API's own stack_kind read,
// served_models empty because the compose command declares no model).
const env = {
    _aiStacksLast: { stacks: [] },
    _stackOptions: [{ stack_id: 1, name: 'asdrfasd', status: 'running', kind: 'chat', served_models: [] }],
    _aiSlotExternals: [],
    _paidEditionKnown: false,
    document: mkDoc(),
};
// The shipped status-word mapper, handed to the stub env as a function.
env._aiStackStatusWord = new Function(`${statusSrc} return _aiStackStatusWord;`)();

const names = Object.keys(env);
const prelude = names.map((n) => `var ${n} = __env[${JSON.stringify(n)}];`).join(' ');

(async () => {
    const runFill = new Function('__env', '__mkSel', `${prelude} ${fillSrc}
        return (async (capability) => { const sel = __mkSel(); _aiSlotFillOptions(sel, capability); return sel; });`);
    const sel = await runFill(env, mkSel)('chat');
    const opts = sel.options.filter((o) => o.value);
    check('A1 the Easy chat picker offers the stack the user owns (stack:1)',
        opts.some((o) => o.value === 'stack:1'), opts.map((o) => o.value));
    check('A2 the option is labelled with the stack name and its status',
        opts.some((o) => String(o.textContent).startsWith('asdrfasd — running')), opts.map((o) => o.textContent));

    // The return value drives the "no stacks" line in renderOne
    // (`const stackOptions = _aiSlotFillOptions(sel, capability); if (!stackOptions) {...}`).
    const runCount = new Function('__env', '__mkSel', `${prelude} ${fillSrc}
        return (async (capability) => { const sel = __mkSel(); return _aiSlotFillOptions(sel, capability); });`);
    const chatCount = await runCount(env, mkSel)('chat');
    check('A3 fill returns 1 for the unbound stack, so the row drops the no-stacks line',
        chatCount === 1, chatCount);
    const embCount = await runCount(env, mkSel)('embedding');
    check('A4 the API kind read still filters: a chat stack never enters an embedding slot',
        embCount === 0, embCount);

    const runLabel = new Function('__env', `${prelude} ${statusSrc} ${labelSrc}
        return (async (v) => _aiSlotValueLabel(v));`);
    const lbl = await runLabel(env)('stack:1');
    check('A5 the chain line labels the unbound stack by name, not stack #1',
        String(lbl).startsWith('asdrfasd — running'), lbl);

    // A6: the honest-empty case must stay honest — a user with NO stacks at all
    // still gets zero options (no invented option, no "no stacks yet" lie).
    const env2 = { _aiStacksLast: { stacks: [] }, _stackOptions: [], _aiSlotExternals: [], _paidEditionKnown: false, document: mkDoc() };
    const noneCount = await runCount(env2, mkSel)('chat');
    check('A6 a user with no stacks still gets zero options', noneCount === 0, noneCount);

    check('A7 no new endpoint, no licence gate, no cap in the fill',
        !fillSrc.includes('fetch(') && !fillSrc.includes('402') && !fillSrc.includes('plan_')
        && !fillSrc.includes('licensed'), fillSrc);

    console.log(failures ? `FAILURES ${failures}` : 'settings-easy-stack-picker-owns-stacks-d76: all assertions passed');
    process.exitCode = failures ? 1 : 0;
})();
