// The Create API Key modal is ~1046 px tall (scope checkboxes + stack deploy
// scopes). It sits in a `fixed inset-0` overlay, so without a height cap and an
// internal scroll the Create Key button is unreachable on any viewport shorter
// than the dialog (e2e spec dash-ce-batch12 failed at 900 px).
//
// Static DOM/CSS assertions: the dialog card is capped at 90vh and scrolls
// vertically, the button row is sticky at the bottom of the scroller, and the
// utility classes used are really emitted by the built tailwind.css.
//
//   node --test CORE/Dashboard/tests/settings-api-key-modal-scroll.test.js
'use strict';
const assert = require('node:assert');
const test = require('node:test');
const fs = require('fs');
const path = require('path');

const dash = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(dash, 'pages', 'settings.html'), 'utf8');
const css = fs.readFileSync(path.join(dash, 'tailwind.css'), 'utf8');

function classesOf(id) {
    const m = html.match(new RegExp(`<div[^>]*\\bid="${id}"[^>]*\\bclass="([^"]*)"|<div[^>]*\\bclass="([^"]*)"[^>]*\\bid="${id}"`));
    assert.ok(m, `#${id} not found in settings.html`);
    return (m[1] || m[2]).split(/\s+/).filter(Boolean);
}

function cssHas(cls) {
    // Tailwind escapes [, ], and other punctuation in selectors with a backslash.
    const sel = '.' + cls.replace(/[^A-Za-z0-9_-]/g, (c) => '\\' + c);
    return new RegExp(sel.replace(/[\\[\].]/g, '\\$&') + '[\\s{,]').test(css);
}

test('create-api-key dialog is height-capped and scrolls', () => {
    const cls = classesOf('create-api-key-dialog');
    assert.ok(cls.includes('max-h-[90vh]'), 'dialog needs max-h-[90vh]');
    assert.ok(cls.includes('overflow-y-auto'), 'dialog needs overflow-y-auto');
});

test('Create Key / Cancel row is sticky inside the scroller', () => {
    const cls = classesOf('create-api-key-footer');
    for (const c of ['sticky', 'bottom-0', 'bg-white']) assert.ok(cls.includes(c), `footer needs ${c}`);
    const dialog = html.indexOf('id="create-api-key-dialog"');
    const footer = html.indexOf('id="create-api-key-footer"');
    const submit = html.indexOf('id="submit-create-api-key"');
    assert.ok(dialog >= 0 && footer > dialog && submit > footer, 'footer + submit must live inside the dialog');
});

test('overlay stays fixed and no other modal in settings.html lacks a cap', () => {
    assert.ok(classesOf('create-api-key-modal').includes('fixed'));
    // Every `fixed inset-0` overlay in the page must have a capped, scrolling card.
    const overlays = html.match(/class="[^"]*\bfixed\b[^"]*\binset-0\b[^"]*"/g) || [];
    assert.strictEqual(overlays.length, 1, 'new overlay added: give its card max-h + overflow-y-auto and extend this test');
});

test('built tailwind.css emits the utilities the modal relies on', () => {
    for (const c of ['max-h-[90vh]', 'overflow-y-auto', 'sticky', 'bottom-0']) {
        assert.ok(cssHas(c), `tailwind.css is missing .${c}`);
    }
    assert.ok(!cssHas('max-h-[91vh]'), 'cssHas must not match absent classes');
});
