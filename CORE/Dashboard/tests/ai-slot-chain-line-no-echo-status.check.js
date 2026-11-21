'use strict';
// 
//
// On Settings > AI Configuration ("Who uses what") one resolver failure was
// printed TWICE for a slot: the chain line under the dropdown
// (`#0 primary - <label> · <attempt.message>`, CORE/Dashboard/js/settings.js
// _aiSlotChainLine) and the red status line under the row
// (`data-role="slot-status"`, res.message). With a single binding
// attempt.message === res.message, so the same sentence ("Nothing answers at
// ollama:11434 - the external endpoint ...") appeared twice and the page read
// as if it were asking the same question twice (owner report; diagnosis
// results-1110/DIAG-CE-SETTINGS-AI-PAGE-DUPLICATE-AND-STALE-TEXT-AT-PIN3B-D72.md
// section (c), finding F-2).
//
// Fix (shared Dashboard JS, ships to paid too - display text only): in
// _aiSlotChainLine, when an attempt carries EXACTLY the status sentence and is
// not 'ready', print 'not answering' in the chain line instead of repeating the
// full text. Distinct fallback causes keep their own message; 'ready' still
// prints 'answering'.
//
//   node --test CORE/Dashboard/tests/ai-slot-chain-line-no-echo-status.check.js
//
// Cases:
//   C1 one binding, attempt.message === res.message -> chain says 'not answering', full sentence 0x in the chain
//   C2 several fallbacks with DIFFERENT messages -> each keeps its own text (no regression)
//   C3 outcome 'ready' -> 'answering' (unchanged)
//   C4 attempt message differs from res.message -> full attempt text still printed
//   C5 the status line renderer is untouched (still prints res.message once)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('node:vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'js', 'settings.js'), 'utf8');

// --- extract the real _aiSlotChainLine (+ its label helper) ------------------
function grab(startRe, endMarker) {
    const m = startRe.exec(src);
    assert.ok(m, 'cannot find ' + startRe);
    const end = src.indexOf(endMarker, m.index);
    assert.ok(end > m.index, 'cannot find end ' + endMarker);
    return src.slice(m.index, end);
}
const code = [
    grab(/function _aiSlotValueLabel\(/, '\nfunction _aiSlotChainLine('),
    grab(/function _aiSlotChainLine\(/, '\n// The "Add fallback" button'),
].join('\n');

// --- fake DOM: every created node records its textContent -------------------
function runChain(res, attempts) {
    const texts = [];
    function mkNode() {
        const node = { children: [], text: null };
        node.className = '';
        node.dataset = {};
        Object.defineProperty(node, 'textContent', {
            set(v) { node.text = String(v); texts.push(String(v)); },
            get() { return node.text; },
        });
        node.appendChild = (c) => { node.children.push(c); };
        node.addEventListener = () => {};
        return node;
    }
    const ctx = {
        _aiSlotChosenAt: {}, _aiSlotExternals: {}, _aiStacksLast: null,
        _aiSlotValueLabel: (v) => String(v),
        _aiSlotChainAct: () => {},
        document: { createElement: () => mkNode() },
        String, Number, Array, Math, JSON,
        _tRes: res, _tAtt: attempts,
    };
    vm.createContext(ctx);
    vm.runInContext(code, ctx);
    vm.runInContext(`_aiSlotChainLine('wizard', _tRes, _tAtt)`, ctx);
    return texts;
}

const MSG = 'Nothing answers at ollama:11434 - the external endpoint bound to the wizard slot is not reachable from here.';

// --- C1: single binding, chain message === status message -------------------
let texts = runChain(
    { outcome: 'unreachable', message: MSG, attempts: [{ priority: 0, outcome: 'unreachable', message: MSG }] },
    [{ priority: 0, outcome: 'unreachable', message: MSG }]);
const chain1 = texts.join('\n');
assert.ok(chain1.includes('not answering'),
    'chain line must say "not answering" when its attempt repeats the status sentence');
assert.strictEqual(chain1.split(MSG).length - 1, 0,
    'the full status sentence must NOT be repeated in the chain line (it is printed once, on the status line)');
assert.ok(chain1.includes('#0 primary'), 'chain line still names the primary binding');

// --- C2: several fallbacks, each its own cause ------------------------------
texts = runChain(
    { outcome: 'unreachable', message: MSG,
      attempts: [{ priority: 0, outcome: 'unreachable', message: MSG },
                 { priority: 1, outcome: 'timeout', message: 'model took too long to answer' }] },
    [{ priority: 0, outcome: 'unreachable', message: MSG },
     { priority: 1, outcome: 'timeout', message: 'model took too long to answer' }]);
const chain2 = texts.join('\n');
assert.ok(chain2.includes('model took too long to answer'),
    'a fallback with its OWN cause keeps that text in the chain line');
assert.strictEqual(chain2.split(MSG).length - 1, 0,
    'only the status-repeating attempt collapses; the rest keep their own text');
assert.ok(chain2.includes('not answering'), 'the repeating attempt collapses');

// --- C3: ready stays 'answering' -------------------------------------------
texts = runChain(
    { outcome: 'ready', message: MSG, attempts: [{ priority: 0, outcome: 'ready', message: MSG }] },
    [{ priority: 0, outcome: 'ready', message: MSG }]);
assert.ok(texts.join('\n').includes('answering'), 'a ready attempt still prints "answering"');

// --- C4: attempt message differs from the status message -------------------
texts = runChain(
    { outcome: 'unreachable', message: MSG,
      attempts: [{ priority: 0, outcome: 'unreachable', message: 'a different cause line' }] },
    [{ priority: 0, outcome: 'unreachable', message: 'a different cause line' }]);
assert.ok(texts.join('\n').includes('a different cause line'),
    'an attempt whose message is NOT the status sentence keeps its full text');

// --- C5: the status line renderer is untouched (prints res.message once) ---
const statusHits = (src.match(/dataset\.role = 'slot-status'/g) || []).length;
assert.ok(statusHits >= 1, 'the slot-status line must still exist');
assert.ok(/foot\.innerHTML = `<span class="text-red-600">\$\{_esc\(cause\)\}<\/span>`/.test(src),
    'the status line still prints res.message in full (the single full copy)');

console.log('ai-slot-chain-line-no-echo-status.check.js: all asserts passed');
