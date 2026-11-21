// Secrets page: behaviour checks for the helpers that decide which secrets
// are Harbor-managed, what each one is described as, and the empty-list
// state of the table. (FIX-DASH-TIER2-UNTESTED-MODULES-JEST-TESTS, )
//
// secrets.js is a browser ES module (imports api.js, ui.js, resource-
// permissions.js). The helpers are extracted from the source by brace
// matching and evaluated in a fresh vm context with a stub `document`.
// Nothing from the module is modified. Behaviours asserted:
//   * isHarborSecret: true only for the three Harbor secret names
//   * getSecretDescription: fixed text for Harbor names, "Custom secret" else
//   * renderSecrets: empty list shows the empty-state row; missing table is a no-op
//   * the page initializer is still exported for the shell
//
//   node --test CORE/Dashboard/tests/secrets-helpers.check.js
'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const test = require('node:test');

const ROOT = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(ROOT, 'js', 'secrets.js'), 'utf8');

function extractFns(text, names) {
    return names.map((name) => {
        const re = new RegExp('(?:export\\s+)?(?:async\\s+)?function\\s+' + name + '\\s*\\(');
        const m = re.exec(text);
        if (!m) throw new Error('function not found in module: ' + name);
        const open = text.indexOf('{', m.index);
        let depth = 0;
        let j = open;
        for (; j < text.length; j++) {
            if (text[j] === '{') depth++;
            else if (text[j] === '}') {
                depth--;
                if (depth === 0) break;
            }
        }
        return text.slice(m.index, j + 1).replace(/^export\s+/, '');
    });
}

function makeContext(tbody) {
    const ctx = {
        document: {
            querySelector: (sel) => (sel === '#secrets-table tbody' ? tbody : null),
        },
    };
    return vm.createContext(ctx);
}

const NAMES = ['isHarborSecret', 'getSecretDescription', 'renderSecrets'];
const code = extractFns(src, NAMES).join('\n') + '\n;({' + NAMES.join(',') + '})';

test('module still exports the page loader the shell calls', () => {
    assert.ok(/export async function loadSecrets\(/.test(src));
});

test('isHarborSecret matches exactly the three Harbor secret names', () => {
    const tbody = { innerHTML: '' };
    const h = vm.runInContext(code, makeContext(tbody));
    assert.strictEqual(h.isHarborSecret('harbor_registry'), true);
    assert.strictEqual(h.isHarborSecret('harbor_username'), true);
    assert.strictEqual(h.isHarborSecret('harbor_password'), true);
    assert.strictEqual(h.isHarborSecret('harbor_token'), false);
    assert.strictEqual(h.isHarborSecret('HARBOR_PASSWORD'), false);
    assert.strictEqual(h.isHarborSecret('openai_api_key'), false);
    assert.strictEqual(h.isHarborSecret(''), false);
});

test('getSecretDescription: Harbor names get their fixed text, others a default', () => {
    const h = vm.runInContext(code, makeContext({ innerHTML: '' }));
    assert.strictEqual(h.getSecretDescription('harbor_registry'), 'Harbor container registry URL');
    assert.strictEqual(
        h.getSecretDescription('harbor_username'),
        'Harbor username for container registry access',
    );
    assert.strictEqual(
        h.getSecretDescription('harbor_password'),
        'Harbor password for container registry access',
    );
    assert.strictEqual(h.getSecretDescription('my_custom'), 'Custom secret');
    // NOTE (finding, not asserted): the lookup is a plain object literal, so
    // prototype names such as 'constructor' return an inherited value, not
    // 'Custom secret'. Left out of this test because the module must not be
    // edited here; recorded in the result file for a follow-up fix.
});

test('renderSecrets with an empty list shows the empty-state row', () => {
    const tbody = { innerHTML: 'stale' };
    const h = vm.runInContext(code, makeContext(tbody));
    h.renderSecrets([]);
    assert.ok(tbody.innerHTML.includes('No secrets have been created yet.'), tbody.innerHTML);
    assert.ok(tbody.innerHTML.includes('colspan="4"'), tbody.innerHTML);
    assert.ok(!tbody.innerHTML.includes('stale'), 'previous rows must be cleared');
});

test('renderSecrets is a no-op when the table is not on the page', () => {
    const h = vm.runInContext(code, makeContext(null));
    assert.doesNotThrow(() => h.renderSecrets([{ name: 'x' }]));
});
