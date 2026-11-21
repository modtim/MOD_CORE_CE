'use strict';
// 2400LR-BUG-1027b — CE team "Send Invitation" gave the admin NO accept link,
// and with SMTP off (CORE/API/utils/email_sender.py — email is optional on CE)
// the invitee could never get in. Fix: the create response now carries
// `accept_link` (schemas.InvitationCreated, routers/invitations.py) and
// Dashboard/js/invitations.js shows it in a callout (#team-invite-result) with
// a Copy button, mirroring the external-invite callout.
//
//   node --test CORE/Dashboard/tests/team-invite-link.check.js
//
// Cases:
//   A1 stubbed create -> link input .value === accept_link, callout un-hidden
//   A2 email containing markup reaches the span as literal text, no element created
//   A3 no accept_link (older API) -> callout untouched, today's toast only
//   A4 Copy button wired to copyToClipboard; the four new ids exist in the page
//   A5 old-file proof: run this file against HEAD's invitations.js (A1/A4 fail)
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const invSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'invitations.js'), 'utf8');
const pageSrc = fs.readFileSync(path.join(__dirname, '..', 'pages', 'invitations.html'), 'utf8');
const results = [];

function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

function extractFn(src, marker) {
    const start = src.indexOf(marker);
    if (start < 0) return null;
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') {
            depth--;
            if (depth === 0) return src.slice(start, i + 1);
        }
    }
    return null;
}

// --- tiny DOM stand-in: textContent is a plain property, so a markup string //
// can only ever land as text; innerHTML would have pushed child nodes.       //
const els = {};
function makeEl(id) {
    return {
        id,
        textContent: '',
        value: '',
        children: [],
        classList: { _hidden: true, remove: (c) => { if (c === 'hidden') els[id].classList._hidden = false; } },
        scrollIntoView: () => {},
    };
}
global.document = { getElementById: (id) => els[id] || null };

const LINK = 'http://localhost:3000/accept-invitation.html?token=t1';

const fnSrc = extractFn(invSrc, 'function showTeamInviteAcceptLink(');
check('A0', !!fnSrc, 'showTeamInviteAcceptLink must exist in invitations.js');

if (fnSrc) {
    const show = eval('(' + fnSrc.replace(/^export\s+/, '') + ')');
    ['team-invite-result', 'team-invite-result-email', 'team-invite-link'].forEach(id => els[id] = makeEl(id));

    show('teammate@example.com', LINK);
    check('A1a', els['team-invite-link'].value === LINK,
        `link input .value must equal accept_link (got ${JSON.stringify(els['team-invite-link'].value)})`);
    check('A1b', !els['team-invite-result'].classList._hidden, 'callout must not stay hidden');
    check('A1c', els['team-invite-result-email'].textContent === 'teammate@example.com', 'email span text set');

    const XSS = '<img src=x onerror=1>';
    show(XSS, LINK);
    check('A2a', els['team-invite-result-email'].textContent === XSS, 'markup email lands as literal text');
    check('A2b', els['team-invite-result-email'].children.length === 0, 'no element created from the email string');
    check('A2c', !/innerHTML/.test(fnSrc), 'showTeamInviteAcceptLink must not touch innerHTML');
    check('A2d', /emailEl\.textContent = email/.test(fnSrc) && /linkEl\.value = acceptLink/.test(fnSrc),
        'email via textContent, link via .value');
} else {
    ['A1a', 'A1b', 'A1c', 'A2a', 'A2b', 'A2c', 'A2d'].forEach(n => check(n, false, 'function unavailable'));
}

// --- A3: older API (no accept_link) keeps today's toast only ---------------
const guard = /if \(created && created\.accept_link\)/.test(invSrc);
check('A3a', guard, 'create site must guard on created.accept_link before showing the callout');
check('A3b', /showInfo\('Invitation sent successfully!'\)/.test(invSrc), 'the original toast is kept');

// --- A4: Copy button + the four new ids ------------------------------------
const copyWire = /getElementById\('team-invite-copy'\)[\s\S]{0,400}copyToClipboard\(link\)/.test(invSrc);
check('A4a', copyWire, 'team-invite-copy reuses copyToClipboard');
['team-invite-result', 'team-invite-result-email', 'team-invite-link', 'team-invite-copy'].forEach(id =>
    check('A4b:' + id, pageSrc.includes(`id="${id}"`), `pages/invitations.html must define #${id}`));
check('A4c', /If email delivery is off, this link is the only way they receive the invite\. It expires in 7 days\./.test(pageSrc),
    'callout states the email-off consequence + 7-day expiry');
check('A4d', /const created = await invitations\.create\(payload\)/.test(invSrc),
    'handleSendInvitation must capture the create response');

const failed = results.filter(r => !r[1]);
if (failed.length) {
    console.log(`team-invite-link.check.js: ${failed.length}/${results.length} FAILED`);
    process.exit(1);
}
console.log(`team-invite-link.check.js: all ${results.length} checks passed -> OK`);
