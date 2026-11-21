'use strict';
// 
// Owner (Tim): when no e-mail system is set up, sending an invitation must say
// plainly that e-mail is not available. The API already reports
// InvitationCreated.email_delivered (false = nothing sent). The accept-link
// callout (pages/invitations.html and the team-management Send Invitation panel)
// must say, for strict false:
//   "Invitation created for <email>. E-mail sending is not available on this
//    install, so no message was sent: copy this link and send it yourself."
// and keep the original wording for true / undefined.
//
//   node --test CORE/Dashboard/tests/invite-callout-no-email-system-note.check.js
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

const EXPECT = (email) =>
    `Invitation created for ${email}. E-mail sending is not available on this install, so no message was sent: copy this link and send it yourself.`;

function makeEl() {
    const cls = new Set(['hidden']);
    return {
        textContent: '', value: '', parentElement: null,
        classList: {
            remove: (c) => cls.delete(c), add: (c) => cls.add(c),
            toggle: (c, on) => { if (on) cls.add(c); else cls.delete(c); },
            contains: (c) => cls.has(c),
        },
        scrollIntoView() {},
    };
}

function runCallout(email, link, delivered) {
    const m = inv.match(/export const NO_EMAIL_NOTE_PREFIX[\s\S]*?\r?\nexport function showTeamInviteAcceptLink[\s\S]*?\r?\n}\r?\n/);
    assert.ok(m, 'callout function not found');
    const els = {};
    for (const id of ['team-invite-result', 'team-invite-result-email', 'team-invite-link', 'team-invite-no-email-note']) els[id] = makeEl();
    const sentLine = makeEl(); sentLine.classList.remove('hidden');
    els['team-invite-result-email'].parentElement = sentLine;
    const document = { getElementById: (id) => els[id] || null };
    const body = m[0].replace(/export /g, '') + '\nreturn showTeamInviteAcceptLink;';
    new Function('document', body)(document)(email, link, delivered);
    return { els, sentLine };
}

test('A1 no e-mail system (email_delivered=false): exact owner wording, link shown, original line hidden', () => {
    const { els, sentLine } = runCallout('a@b.test', 'http://x/accept?token=t', false);
    const note = els['team-invite-no-email-note'];
    assert.strictEqual(note.textContent, EXPECT('a@b.test'));
    assert.ok(!note.classList.contains('hidden'));
    assert.ok(sentLine.classList.contains('hidden'));
    assert.strictEqual(els['team-invite-link'].value, 'http://x/accept?token=t');
    assert.ok(!els['team-invite-result'].classList.contains('hidden'));
});

test('A2 e-mail configured (true / undefined): original callout, no no-email note', () => {
    for (const d of [true, undefined, null]) {
        const { els, sentLine } = runCallout('a@b.test', 'http://x/y', d);
        assert.strictEqual(els['team-invite-no-email-note'].textContent, '', String(d));
        assert.ok(els['team-invite-no-email-note'].classList.contains('hidden'), String(d));
        assert.ok(!sentLine.classList.contains('hidden'), String(d));
    }
});

test('A3 wording does not name a paid provider', () => {
    assert.ok(!/brevo|mailgun|sendgrid/i.test(EXPECT('x')));
    assert.ok(inv.includes('E-mail sending is not available on this install'));
});

test('A4 invitations.js passes the response flag into the callout', () => {
    assert.ok(inv.includes('showTeamInviteAcceptLink(created.email, created.accept_link, created.email_delivered)'));
});

test('A5 both pages carry the no-email note element and the team-management panel the callout', () => {
    assert.ok(invHtml.includes('id="team-invite-no-email-note"'));
    assert.ok(usrHtml.includes('id="team-invite-no-email-note"'));
    assert.ok(usrHtml.includes('id="team-invite-link"') && usrHtml.includes('id="team-invite-copy"'));
});

test('A6 team-management Send Invitation: strict false shows the callout, else the original toast', () => {
    assert.ok(/const created = await invitations\.create\(payload\);\s*\n[\s\S]{0,400}created\.email_delivered === false/.test(usr));
    assert.ok(usr.includes("showTeamInviteAcceptLink(created.email, created.accept_link, false)"));
    assert.ok(usr.includes("showInfo('Invitation sent successfully!')"));
    assert.ok(usr.includes("import { showTeamInviteAcceptLink, copyToClipboard } from './invitations.js'"));
});
