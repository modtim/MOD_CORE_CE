// 2400LR-ceA11y-b13r2 — notification-prefs ready-signal check (static, source-text asserts;
// no browser). Style of tests/admin-ingress-allowlist.check.js.
//
//   node CORE/Dashboard/tests/notification-prefs-ready-signal.check.js
//
// Checks:
//   1) js/account.js parses as a valid ES module.
//   2) every in-app checkbox carries an accessible name (aria-labelledby -> visible label <p>).
//   3) the sr-only input sits inside a visible <label> switch (the element a user clicks).
//   4) account.js stamps data-loaded="1" on each checkbox after the GET result is applied.
//   5) the data-loaded stamp happens BEFORE the change listener is attached (no click race).
//   6) the save path is unchanged: change -> setNotificationPreferences, revert on failure.

'use strict';
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const js = read('js/account.js');
const html = read('pages/account.html');

// 1) ES module syntax
let modErr = null;
try {
    execFileSync(process.execPath, ['--input-type=module', '--check'], { input: js, encoding: 'utf8' });
} catch (e) { modErr = String(e && (e.stderr || e.message)); }
assert.ok(!modErr, `account.js not valid ES module — ${(modErr || '').split('\n')[0] || 'unknown'}`);

// 2) accessible names on the in-app checkboxes
const CATS = ['workflow', 'billing', 'product'];
CATS.forEach((cat) => {
    const id = `notif-pref-${cat}-in_app`;
    const re = new RegExp(`<input[^>]*id="${id}"[^>]*aria-labelledby="notif-pref-${cat}-label"`);
    assert.ok(re.test(html), `pages/account.html input #${id} must carry aria-labelledby -> #notif-pref-${cat}-label`);
    assert.ok(html.includes(`id="notif-pref-${cat}-label"`), `visible label #notif-pref-${cat}-label must exist`);
});

// 3) sr-only input inside a visible label switch (click target for a real user)
CATS.forEach((cat) => {
    const id = `notif-pref-${cat}-in_app`;
    const re = new RegExp(`<label[^>]*cursor-pointer[^>]*>\\s*<input[^>]*id="${id}"[^>]*class="sr-only`);
    assert.ok(re.test(html), `#${id} must be wrapped by the visible cursor-pointer <label> switch`);
});

// 4) ready signal stamped after the GET result is applied
assert.ok(/el\.checked = !!prefs\?\.\[category\]\?\.in_app;\s*\n\s*el\.dataset\.loaded = '1';/.test(js),
    'account.js must stamp el.dataset.loaded right after applying the GET result');
assert.ok(/el\.dataset\.loaded = '1';[^\n]*\n\s*\}\);/.test(js), 'stamp must be inside the per-toggle apply loop');

// 5) stamp precedes listener attach (the race the spec would otherwise hit)
const stampIdx = js.indexOf("el.dataset.loaded = '1';");
const attachIdx = js.indexOf("el.addEventListener('change'");
assert.ok(stampIdx >= 0 && attachIdx > stampIdx,
    'data-loaded must be stamped before the change listener is attached (ready signal implies wired listener)');

// 6) save path intact
assert.ok(/await me\.setNotificationPreferences\(\{ \[category\]: \{ in_app: el\.checked \} \}\);/.test(js),
    'change handler must still PUT the per-category in_app value');
assert.ok(/el\.checked = !el\.checked; \/\/ revert on failure/.test(js), 'failed save must revert the checkbox');
assert.ok(/Notification preferences saved\./.test(js), 'success status text unchanged');

console.log('notification-prefs-ready-signal.check.js: all asserts passed');
