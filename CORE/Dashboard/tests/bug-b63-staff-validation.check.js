// BUG.B63 (owner 10-08): Dashboard Users → Staff Register → "Add staff record"
// accepted any junk in every field. The form now sanity-checks each field
// (emails must look like emails, names like names, dates real calendar dates,
// role/type sane for their type), shows an inline amber warning under the
// offending field, and refuses to submit until the "Save anyway — I confirm
// this data is correct" checkbox is ticked. Empty required fields still block.
//
// This check verifies:
//   1) js/staff-register.js parses as a valid ES module.
//   2) The add-form markup has an amber warning span per field + the override
//      checkbox, and only classes that exist in tailwind.css are used.
//   3) The exported validators actually reject junk and accept good input
//      (extracted and run — they are pure, no DOM).
//   4) handleAdd gates submit: junk + unticked checkbox => no POST; junk +
//      ticked => POST; clean => POST; empty name => no POST.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8');

const src = read('js/staff-register.js');
const html = read('pages/staff-register.html');
const css = read('tailwind.css');

// --- 1) module-mode syntax check -------------------------------------------
let err = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'],
        { input: src, encoding: 'utf8' });
} catch (e) {
    err = String(e && (e.stderr || e.message));
}
assert.ok(!err, `js/staff-register.js: not a valid ES module — ${(err || '').split('\n')[0] || 'unknown'}`);

// --- 2) markup: warning span per field + override checkbox -----------------
const WARN_IDS = ['sr-warn-name', 'sr-warn-email', 'sr-warn-role', 'sr-warn-type', 'sr-warn-training'];
WARN_IDS.forEach(id => {
    assert.ok(html.includes(`id="${id}"`), `pages/staff-register.html: missing warning span #${id}`);
    assert.ok(new RegExp(`id="${id}" class="[^"]*bg-amber-100[^"]*text-amber-700`).test(html),
        `pages/staff-register.html: #${id} must be an amber (bg-amber-100 text-amber-700) span`);
});
assert.ok(/<input type="checkbox" id="sr-add-confirm"/.test(html),
    'pages/staff-register.html: override must be <input type="checkbox" id="sr-add-confirm">');
assert.ok(/Save anyway — I confirm this data is correct/.test(html),
    'pages/staff-register.html: override checkbox label must read "Save anyway — I confirm this data is correct"');

// every class on the new spans/checkbox must exist in the built tailwind css
const NEW_CLASSES = ['hidden', 'block', 'text-xs', 'italic', 'bg-amber-100', 'text-amber-700',
    'rounded', 'p-1', 'px-2', 'mt-1', 'h-4', 'w-4', 'font-medium'];
NEW_CLASSES.forEach(c => assert.ok(css.includes(`.${c} {`),
    `tailwind.css: class .${c} used by the new markup is not defined`));

// --- 3) validators: pure functions, extracted and run ----------------------
function extractFn(rel, name) {
    const s = read(rel);
    const start = s.indexOf(`export function ${name}(`);
    assert.ok(start >= 0, `js/staff-register.js: no 'export function ${name}'`);
    const tail = s.indexOf('\n}', start);
    assert.ok(tail > start, `js/staff-register.js: unterminated function ${name}`);
    return s.slice(start + 'export '.length, tail + 2); // strip export: new Function body
}

const VALIDATORS = ['_nameWarning', '_emailWarning', '_roleWarning', '_staffTypeWarning', '_dateWarning'];
const FN_BODIES = VALIDATORS.map(n => extractFn('js/staff-register.js', n)).join('\n');
const SR_TYPES = read('js/staff-register.js').match(/^const SR_TYPES = \[[^\]]*\];/m);
assert.ok(SR_TYPES, 'js/staff-register.js: SR_TYPES whitelist must be a module-level const');
const V = new Function(`${SR_TYPES[0]}\n${FN_BODIES}\nreturn {` +
    VALIDATORS.map(n => ` ${n}: ${n}`).join(',') + ` };`)();

const NAME_MSG = "This doesn't look like a real name";
const EMAIL_MSG = "This doesn't look like a real email";

// names
assert.ok(V._nameWarning('Mary-Jane O\'Brien Van Der Hooten') === null, 'name: letters/space/hyphen/apostrophe/period must pass');
assert.ok(V._nameWarning('Dr. Susanna Müller') === null, 'name: accented letters + period must pass');
assert.ok(V._nameWarning('a') === NAME_MSG, 'name: <2 chars must be rejected');
assert.ok(V._nameWarning('aaaa') === NAME_MSG, 'name: all-same-char must be rejected');
assert.ok(V._nameWarning('12345') === NAME_MSG, 'name: digits must be rejected');
assert.ok(V._nameWarning('!!!') === NAME_MSG, 'name: no letters must be rejected');
assert.ok(V._nameWarning('Bob<svg onload=1>') === NAME_MSG, 'name: markup/punctuation must be rejected');
assert.ok(V._nameWarning('') === null, 'name: blank is not a warning (required-field check owns it)');

// emails
assert.ok(V._emailWarning('admin@example.com') === null, 'email: normal email must pass');
assert.ok(V._emailWarning('ann.marie+trainee@sub.dept.ac.uk') === null, 'email: dotted/+/multi-part must pass');
assert.ok(V._emailWarning('notanemail') === EMAIL_MSG, 'email: no @ must be rejected');
assert.ok(V._emailWarning('a@b') === EMAIL_MSG, 'email: no TLD must be rejected');
assert.ok(V._emailWarning('a@b.c') === EMAIL_MSG, 'email: 1-char TLD must be rejected');
assert.ok(V._emailWarning('foo bar@baz.com') === EMAIL_MSG, 'email: space must be rejected');
assert.ok(V._emailWarning('foo.@bar.com') === EMAIL_MSG, 'email: trailing dot before @ must be rejected');
assert.ok(V._emailWarning('@bar.com') === EMAIL_MSG, 'email: empty local part must be rejected');

// role / type / date
assert.ok(V._roleWarning('Compliance Officer (Senior)') === null, 'role: normal role must pass');
assert.ok(V._roleWarning('????') === "This doesn't look like a real role", 'role: punctuation-only must be rejected');
assert.ok(V._roleWarning('zzz') === "This doesn't look like a real role", 'role: all-same-char must be rejected');
assert.ok(V._staffTypeWarning('employee') === null && V._staffTypeWarning('contractor') === null, 'type: whitelist must pass');
assert.ok(V._staffTypeWarning('wizard') === 'Pick employee or contractor', 'type: off-whitelist must be rejected');
assert.ok(V._dateWarning('2025-02-28') === null && V._dateWarning('2024-02-29') === null, 'date: real calendar dates must pass');
assert.ok(V._dateWarning('2025-02-30') === "This doesn't look like a real date", 'date: 2025-02-30 must be rejected');
assert.ok(V._dateWarning('2025-13-01') === "This doesn't look like a real date", 'date: month 13 must be rejected');
assert.ok(V._dateWarning('02/28/2025') === "This doesn't look like a real date", 'date: non-ISO must be rejected');
assert.ok(V._dateWarning('9999-01-01') === "This doesn't look like a real date", 'date: absurd year must be rejected');

// --- 4) the checkbox gates submit ------------------------------------------
const ADD_SRC = (() => {
    const s = src;
    const start = s.indexOf('async function handleAdd()');
    assert.ok(start >= 0, 'js/staff-register.js: no async handleAdd');
    const tail = s.indexOf('\n}', start);
    assert.ok(tail > start, 'js/staff-register.js: unterminated handleAdd');
    return s.slice(start, tail + 2);
})();
assert.ok(/if \(junk\.length && !\(confirm && confirm\.checked\)\)/.test(src),
    'js/staff-register.js: submit must be blocked when fields look wrong and the checkbox is unticked');
assert.ok(/getElementById\('sr-add-confirm'\)/.test(ADD_SRC),
    'js/staff-register.js: handleAdd must read the sr-add-confirm checkbox');

// new Function bodies only see globals, so the sinks live on globalThis.
globalThis.CREAT = { created: [], errs: [], warns: {} };
globalThis.FIELDS = {};
globalThis.mkDoc = () => ({ getElementById: (id) => ({
    value: FIELDS[id], checked: FIELDS[`__checked:${id}`] === true,
    classList: { add: () => {}, remove: () => {} } }) });
const STUBS = `
    ${SR_TYPES[0]}
    ${FN_BODIES}
    const document = mkDoc();
    const window = { showInfo: (m, t) => {} };
    const console = { log: () => {}, warn: () => {}, error: () => {} };
    const _clearError = () => {};
    const _showError = (m) => { CREAT.errs.push(m); };
    const _setWarning = (id, msg) => { CREAT.warns[id] = msg; };
    const _errText = (e) => 'err';
    const withButtonBusy = (btn, fn) => fn();
    const loadTable = async () => {};
    const staffRegister = { create: (payload) => { CREAT.created.push(payload); return { id: 1 }; } };
`;
async function runAdd(fields, confirmed) {
    for (const k in FIELDS) delete FIELDS[k];
    Object.assign(FIELDS, fields);
    FIELDS['__checked:sr-add-confirm'] = confirmed;
    globalThis.CREAT = { created: [], errs: [], warns: {} };
    const f = new Function(`${STUBS}\n${ADD_SRC}\nreturn handleAdd;`)();
    await f();
    return { created: CREAT.created, errs: CREAT.errs, warns: CREAT.warns };
}

(async () => {
    // junk everywhere, checkbox unticked -> blocked, warnings rendered
    let r = await runAdd({ 'sr-add-name': 'aaaa', 'sr-add-email': 'nope',
        'sr-add-role': '???', 'sr-add-type': 'wizard', 'sr-add-training': '2025-13-45' }, false);
    assert.ok(r.created.length === 0, 'gate: junk + unticked checkbox must NOT create a record');
    assert.ok(r.errs.length === 1 && /Save anyway/.test(r.errs[0]), 'gate: blocked submit must name the override checkbox');
    assert.ok(['sr-warn-name', 'sr-warn-email', 'sr-warn-role', 'sr-warn-type', 'sr-warn-training']
        .every(k => r.warns[k]), 'gate: every junk field must get an inline warning');

    // same junk, checkbox ticked -> override saves as typed
    r = await runAdd({ 'sr-add-name': 'aaaa', 'sr-add-email': 'nope',
        'sr-add-role': '???', 'sr-add-type': 'employee', 'sr-add-training': '2025-13-45' }, true);
    assert.ok(r.created.length === 1, 'override: ticked checkbox must let the junk record through');
    assert.ok(r.created[0].person_name === 'aaaa' && r.created[0].email === 'nope',
        'override: the typed values must be saved verbatim');

    // clean data, checkbox untouched -> saves
    r = await runAdd({ 'sr-add-name': "Mary-Jane O'Brien", 'sr-add-email': 'mj@example.com',
        'sr-add-role': 'Compliance Officer', 'sr-add-type': 'contractor', 'sr-add-training': '2024-02-29' }, false);
    assert.ok(r.created.length === 1 && r.errs.length === 0, 'clean: good data saves with no warnings');
    assert.ok(Object.values(r.warns).every(v => !v), 'clean: no warnings rendered');

    // empty required name still blocks, even with the checkbox ticked
    r = await runAdd({ 'sr-add-name': '', 'sr-add-email': 'a@bb.com' }, true);
    assert.ok(r.created.length === 0 && r.errs.some(m => /required/i.test(m)),
        'required: empty name must block even when the override is ticked');

    console.log('bug-b63-staff-validation: module OK, amber spans + override checkbox, validators reject junk, checkbox gates submit -> OK');
})();
