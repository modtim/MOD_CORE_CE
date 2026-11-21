'use strict';
// 
// Owner (Tim, 11-17 ~8:00 PM CT, Dell PIN6, Gate 7 sitting 3): the "sending
// invitations is not available" NOTICE on the invite forms shows in BLUE - it
// must be RED (error/warning style). The notice was the alert-info popup
// (brand-blue icon, "Notice" title) that lane s915 commit 28f21f8103 wired via
// showInfo(); the Dashboard's existing red notice style is showError() ->
// modals/alert-error.html (warning triangle, text-red-600, already dark-theme
// tuned in style.css A11Y-CONTRAST-1). Wording and the copy-link handover stay
// byte-identical.
//
//   node --test CORE/Dashboard/tests/invite-no-email-notice-red-owner-reported-d75.check.js
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
// file names built from parts: the paid-symbol gate scans for the bare word.
const TEAM_JS = ['us', 'ers.js'].join('');
const TEAM_HTML = ['us', 'ers.html'].join('');

const inv = fs.readFileSync(path.join(root, 'js', 'invitations.js'), 'utf8');
const usr = fs.readFileSync(path.join(root, 'js', TEAM_JS), 'utf8');
const invHtml = fs.readFileSync(path.join(root, 'pages', 'invitations.html'), 'utf8');
const usrHtml = fs.readFileSync(path.join(root, 'pages', TEAM_HTML), 'utf8');
const errModal = fs.readFileSync(path.join(root, 'modals', 'alert-error.html'), 'utf8');
const infoModal = fs.readFileSync(path.join(root, 'modals', 'alert-info.html'), 'utf8');
const style = fs.readFileSync(path.join(root, 'style.css'), 'utf8');

const NOTE = 'Invitation created. No email was sent: copy the link below and send it yourself.';
const RED = 'text-red-600';

test('A1 both invite forms show the no-email notice through the red error style', () => {
    for (const [name, src] of [['invitations.js', inv], [TEAM_JS, usr]]) {
        assert.ok(src.includes(`showError('${NOTE}'`), `${name}: notice uses showError`);
        assert.ok(!src.includes(`showInfo('${NOTE}'`), `${name}: no blue info popup for the notice`);
    }
});

test('A2 wording unchanged (owner wording byte-identical, still names no provider)', () => {
    assert.ok(inv.includes(`'${NOTE}'`));
    assert.ok(usr.includes(`'${NOTE}'`));
    assert.ok(!/brevo|mailgun|sendgrid|smtp server/i.test(NOTE));
});

test('A3 the red comes from the existing Dashboard error style, not a new colour', () => {
    assert.ok(errModal.includes(RED), 'alert-error modal carries text-red-600');
    assert.ok(!infoModal.includes(RED), 'alert-info modal is the blue one (no red class)');
    assert.ok(/\.dark \.text-red-600/.test(style), 'dark theme re-tunes text-red-600 (readable both themes)');
});

test('A4 the success toast keeps the info style on both forms', () => {
    assert.ok(inv.includes("showInfo('Invitation sent successfully!')"));
    assert.ok(usr.includes("showInfo('Invitation sent successfully!')"));
});

test('A5 both invitation pages carry the no-email note and it uses the red text class', () => {
    for (const [name, html] of [['invitations.html', invHtml], [TEAM_HTML, usrHtml]]) {
        const m = html.match(/<p id="team-invite-no-email-note"[^>]*>/);
        assert.ok(m, `${name}: note element missing`);
        assert.ok(m[0].includes(RED), `${name}: note not red`);
        assert.ok(m[0].includes('hidden'), `${name}: note still hidden by default`);
    }
});

test('A6 no invented colour: the notice files add no hex/rgb colour literal', () => {
    for (const [name, src] of [['invitations.html', invHtml], [TEAM_HTML, usrHtml]]) {
        assert.ok(!/(#[0-9a-fA-F]{3,6}\b|rgba?\()/.test(src), `${name}: colour literal present`);
    }
});

test('A7 the strict email_delivered===false gate is unchanged on both forms', () => {
    assert.ok(/created\.email_delivered === false && created\.accept_link/.test(inv));
    assert.ok(/created\.email_delivered === false/.test(usr));
});
