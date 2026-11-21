// Node-runnable check: every element id js/persistent-stacks.js looks up for
// the compose-result line and the GA.ASW.6 "no backup plan" acknowledgement
// must exist in the page that actually carries that flow — pages/analytics.html
// (the System > Stacks tab; the e2e room reaches it via dashGoto 'analytics' +
// #main-tab-stacks) — with the element TYPE the JS expects, and the ack box
// hidden by default. It also asserts the standalone page persistent-stacks.html
// (driven by js/persistent-stacks-standalone.js, which has NO compose or
// backup-ack flow) carries NONE of those ids: dead ids there are a wrong-file
// edit, and the standalone page's own ids must all exist with the right type.
//
//   node CORE/Dashboard/tests/persistent-stacks-backup-ack-ids.check.js
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const ROOT = path.join(__dirname, '..');
function read(rel) { return fs.readFileSync(path.join(ROOT, rel), 'utf8'); }

const stackHtml = read('pages/analytics.html');
const standaloneHtml = read('persistent-stacks.html');
const js = read('js/persistent-stacks.js');
const standaloneJs = read('js/persistent-stacks-standalone.js');

let passes = 0;
let failures = 0;
function check(cond, msg) {
    if (cond) { passes++; console.log(`ok   ${msg}`); }
    else { failures++; console.error(`FAIL ${msg}`); }
}

// 1) The JS really looks these ids up (so the check is about real lookups).
const lookups = [
    ['ps-compose-result', /function _psComposeResult\(html, ok\) \{[\s\S]{0,120}getElementById\('ps-compose-result'\)/],
    ['ps-backup-ack', /getElementById\('ps-backup-ack'\)/],
    ['ps-backup-ack-text', /getElementById\('ps-backup-ack-text'\)/],
    ['ps-backup-ack-checkbox', /getElementById\('ps-backup-ack-checkbox'\)/],
    ['ps-backup-ack-confirm', /getElementById\('ps-backup-ack-confirm'\)/],
    ['ps-backup-ack-cancel', /getElementById\('ps-backup-ack-cancel'\)/],
];
for (const [id, re] of lookups) {
    check(re.test(js), `js/persistent-stacks.js looks up #${id}`);
}

// 2) Element types in pages/analytics.html.
const types = [
    ['ps-compose-result', /<div[^>]*id="ps-compose-result"/, 'div'],
    ['ps-backup-ack', /<div[^>]*id="ps-backup-ack"/, 'div'],
    ['ps-backup-ack-text', /<[a-z]+[^>]*id="ps-backup-ack-text"/, 'text container'],
    ['ps-backup-ack-checkbox', /<input[^>]*id="ps-backup-ack-checkbox"[^>]*>/, 'input'],
    ['ps-backup-ack-confirm', /<button[^>]*id="ps-backup-ack-confirm"/, 'button'],
    ['ps-backup-ack-cancel', /<button[^>]*id="ps-backup-ack-cancel"/, 'button'],
];
for (const [id, re, kind] of types) {
    const m = stackHtml.match(re);
    check(m, `pages/analytics.html has #${id} as a ${kind}`);
}
const cbTag = stackHtml.match(/<input[^>]*id="ps-backup-ack-checkbox"[^>]*>/);
check(cbTag && /type="checkbox"/.test(cbTag[0]), 'ps-backup-ack-checkbox is <input type="checkbox"> (a tick, not a text field)');
const confirmTag = stackHtml.match(/<button[^>]*id="ps-backup-ack-confirm"[^>]*>/);
check(confirmTag && /\bdisabled\b/.test(confirmTag[0]), 'ps-backup-ack-confirm ships disabled (checkbox gates it)');
check(/id="ps-backup-ack"[^>]*class="[^"]*\bhidden\b/.test(stackHtml), 'ps-backup-ack starts hidden (JS unhides it only on a requires_ack 409)');
check(/id="ps-compose-result"[^>]*class="[^"]*\bhidden\b/.test(stackHtml), 'ps-compose-result starts hidden');

// 3) The safety gate: the checkbox drives the confirm button's disabled state,
// and confirm resends the SAME pending create with acknowledge_no_backup.
check(/getElementById\('ps-backup-ack-checkbox'\)[\s\S]{0,200}?addEventListener\('change'[\s\S]{0,200}?btn\.disabled = !e\.target\.checked/.test(js),
    'checkbox change handler sets confirm.disabled = !checked (js:3972-3975)');
check(/getElementById\('ps-backup-ack-confirm'\)\?\.addEventListener\('click', _psBackupAckConfirmClick\)/.test(js),
    'confirm click routes through _psBackupAckConfirmClick');
check(/function _psBackupAckConfirmClick\(\) \{[\s\S]{0,160}?if \(!_psPendingBackupAck\) return;/.test(js),
    'confirm is a no-op unless a create is pending');
check(/acknowledge_no_backup: true/.test(js), 'confirm re-POSTs with acknowledge_no_backup: true');
check(/if \(cb\) cb\.checked = false;[\s\S]{0,80}?if \(confirmBtn\) confirmBtn\.disabled = true;/.test(js),
    'showing the ack resets the checkbox and re-disables confirm (js:657-658)');

// 4) The standalone page must NOT carry these ids — its JS never looks them up.
for (const [id] of lookups) {
    check(!new RegExp(`id="${id}"`).test(standaloneHtml),
        `persistent-stacks.html must not carry #${id} (dead markup: persistent-stacks-standalone.js never uses it)`);
}

// 5) The standalone page's own ids must all exist (the page it really serves).
const standaloneIds = ['msg', 'tpl', 'tplName', 'tplParams', 'tplDesc', 'rows',
    'btn-create', 'btn-save-idle', 'btn-save-threshold', 'idleEnabled', 'idleAfter',
    'idleWarm', 'idleSource', 'idlePreview', 'rbEnabled', 'rbThreshold', 'rbSource'];
for (const id of standaloneIds) {
    check(new RegExp(`id="${id}"`).test(standaloneHtml), `persistent-stacks.html has #${id} used by persistent-stacks-standalone.js`);
}
check(!/getElementById\(['"]ps-compose-result['"]\)/.test(standaloneJs)
    && !/ps-backup-ack/.test(standaloneJs),
    'persistent-stacks-standalone.js has no compose-result or backup-ack flow');

console.log(`\n${passes} passed, ${failures} failed`);
process.exit(failures ? 1 : 0);
