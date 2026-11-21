// mesh-native-5-served-name.check.js
// sz-mesh-native.5 — the AI-Config Stack picker must present each stack by the
// name it SERVES (vLLM/llama.cpp --served-model-name), not by the stack/container
// name. String scan of CORE/Dashboard/js/settings.js only — no DOM, no eval.

const fs = require('fs');
const path = require('path');

const SRC_PATH = path.resolve(__dirname, '..', 'js', 'settings.js');
const src = fs.readFileSync(SRC_PATH, 'utf8');

let passCount = 0;
let failCount = 0;

function assert(label, condition) {
    if (condition) { passCount++; console.log(`  PASS: ${label}`); }
    else { failCount++; console.log(`  FAIL: ${label}`); }
}

// 1. The served-name labeller exists.
const labelFn = src.match(/function\s+_stackOptionLabel\s*\(/);
assert('served-name label helper _stackOptionLabel exists', !!labelFn);

// 2. It reads served_models (the served name the stack actually serves).
const labelBody = src.substring(labelFn ? labelFn.index : 0,
    (labelFn ? labelFn.index : 0) + 900);
assert('_stackOptionLabel reads served_models', /served_models/.test(labelBody));
assert('_stackOptionLabel falls back to stack name when nothing served',
    /return stopped \? `\$\{o\.name\} \(stopped\)` : o\.name;/.test(labelBody));

// 3. The Stack <select> options use the served-name label, not the raw name.
const pop = src.match(/function\s+_populateStackSelect\s*\([\s\S]{0,700}?\n\}/);
assert('_populateStackSelect exists', !!pop);
const popSrc = pop ? pop[0] : '';
assert('Stack picker option label comes from _stackOptionLabel',
    /opt\.textContent = _stackOptionLabel\(o\);/.test(popSrc));
assert('Stack picker no longer labels options with the bare stack name',
    !/opt\.textContent = o\.status === 'running' \? o\.name/.test(popSrc));

// 4. The served-model picker (multi-model stacks) still keys on the served name.
assert('served-model picker still populated from served_models',
    /dataset\.servedModels = JSON\.stringify\(o\.served_models \|\| \[\]\);/.test(src));

// 5. Selection value stays the stack_id (label change must not break saving).
assert('option value remains stack_id', /opt\.value = String\(o\.stack_id\);/.test(popSrc));

// 6. No /v1 in the AI-config fetch paths around the stack picker.
const aiSection = src.substring(src.indexOf('let _stackOptions = [];'),
    src.indexOf('let _stackOptions = [];') + 3000);
const v1 = aiSection.match(/["']\/[^"']*\/v1[^"']*["']/g);
assert('No /v1 in AI-config stack-picker fetch paths', !v1 || v1.length === 0);

console.log(`\n${passCount} passed, ${failCount} failed`);
if (failCount > 0) process.exit(1);
