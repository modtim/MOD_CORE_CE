'use strict';
// FIX-DASH-INVITATIONS-NO-EMAIL-TOAST-AND-EMPTY-EXTERNAL-INVITES-CARD
// (; invite-flow audit F2 + F6) — two CE-only fixes in
// CORE/Dashboard/js/invitations.js:
//   (a) F2 toast: on the CE no-delivery path (invitations_service.py:400
//       `ce_no_mail = missing_config and is_ce_edition()` -> :448 "created
//       without email delivery") the page still said "Invitation sent
//       successfully!". The create response carries NO delivery flag
//       (CORE/API/schemas.py:1370 `InvitationCreated` adds only `accept_link`,
//       filled on EVERY create at invitations_service.py:464), so the API is
//       NOT changed: the signal is the existing `accept_link` field + the
//       existing api.js isPaidEdition() predicate. CE + accept_link -> the
//       honest toast; paid (and unknown entitlements) keep the ORIGINAL toast
//       byte-for-byte.
//   (b) F6 card: the `!await isPaidEdition()` guard hid the external-invite
//       card with `display:none` only, leaving the bordered container node
//       (pages/invitations.html) as an empty bordered gap. The container node
//       itself is now removed too.
//
//   node --test CORE/Dashboard/tests/dash-invitations-no-email-toast-and-empty-external-card.check.js
//
// Cases (failures COLLECTED so the MCW_OVERRIDE old-file proof prints an exact F count):
//   A1..A5  toast wording + predicate shape + import
//   A6a..d  runtime sim of the create site (CE/paid x accept_link/no)
//   B1..B3  the CE guard removes the CONTAINER node
//   B4a..c  runtime sim, CE branch
//   C1a..c  runtime sim, PAID branch identical to before
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const invSrc = fs.readFileSync(path.join(__dirname, '..', 'js', 'invitations.js'), 'utf8');
const results = [];
function check(name, ok, why) {
    results.push([name, ok]);
    if (!ok) console.log(`  ${name} FAIL: ${why}`);
}

const NEW_TOAST = 'Invitation created. No email was sent: copy the link below and send it yourself.';
const OLD_TOAST = 'Invitation sent successfully!';

// --- A1/A2: wording ---------------------------------------------------------
check('A1', invSrc.includes(`'${NEW_TOAST}'`), 'the no-email toast text must be present verbatim');
check('A2', invSrc.includes(`'${OLD_TOAST}'`), 'the original (paid) toast must be kept byte-identical');

// --- A3/A4: the predicate shape (FIX-INVITATIONS-EMAIL-DELIVERED-FIELD) ----
// The gate is now the API's own delivery signal, STRICT false only.
check('A3', /if \(created && created\.email_delivered === false && created\.accept_link\)/.test(invSrc),
    'no-email toast gated on created.email_delivered === false && created.accept_link');
check('A4', /import \{[^}]*\bisPaidEdition\b/.test(invSrc),
    'isPaidEdition still imported from api.js (the external-card guard at :490)');
// A4b: the toast branch itself (gate -> loadInvitations) must not read
// isPaidEdition() at all — the F-B path (unknown entitlements) can no longer
// claim "no email".
(() => {
    const gate = invSrc.indexOf('if (created && created.email_delivered === false && created.accept_link)');
    const tail = gate < 0 ? -1 : invSrc.indexOf('loadInvitations();', gate);
    const toastBlock = (gate >= 0 && tail > gate) ? invSrc.slice(gate, tail) : '';
    check('A4b', toastBlock.includes(NEW_TOAST) && !toastBlock.includes('isPaidEdition'),
        'the toast branch keys on email_delivered only (no isPaidEdition in gate -> toast)');
})();

function extractFn(src, marker) {
    const start = src.indexOf(marker);
    if (start < 0) return null;
    let depth = 0;
    for (let i = src.indexOf('{', start); i < src.length; i++) {
        if (src[i] === '{') depth++;
        else if (src[i] === '}') { depth--; if (depth === 0) return src.slice(start, i + 1); }
    }
    return null;
}

// --- A5/A6: run the create site with sync stubs (every `await` stripped) ---
const startMark = 'const created = await invitations.create(payload);';
const endMark = 'loadInvitations();';
const s0 = invSrc.indexOf(startMark);
const s1 = (s0 < 0) ? -1 : invSrc.indexOf(endMark, s0);
const blockSrc = (s0 >= 0 && s1 > s0) ? invSrc.slice(s0, s1) : null;
const blockSync = blockSrc ? blockSrc.replace(/\bawait\s+/g, '') : null;
check('A5', !!blockSync, 'create-site block extractable (create -> loadInvitations)');

// FIX-INVITATIONS-EMAIL-DELIVERED-FIELD: the create fixture now CARRIES the
// delivery signal (email_delivered true / false / absent) so all three
// outcomes are asserted non-vacuously.
function simToast(hasLink, delivery) {
    const toasts = [];
    const showInfo = (m) => toasts.push(m);
    // :
    // the no-email notice now goes through the red error popup (showError); record
    // it like the info popup so the wording assertions below still hold.
    const showError = (m) => toasts.push(m);
    const showTeamInviteAcceptLink = (e, l) => toasts.push('link');
    const invitations = { create: () => {
        const o = { email: 'a@b.co' };
        if (hasLink) o.accept_link = 'L';
        if (delivery === 'true') o.email_delivered = true;
        else if (delivery === 'false') o.email_delivered = false;
        // delivery === 'none' -> field absent (older API producer)
        return o;
    } };
    const isPaidEdition = () => false;
    const payload = {};
    const tenantId = null, cogId = null, templateId = null;
    const invitationForm = { reset: () => {} };
    const document = { getElementById: (id) => (id === 'invitation-form' ? invitationForm : null) };
    const initializeCogSelector = () => {};
    const loadInvitations = () => {};
    // The slice is a statement sequence (create -> just before loadInvitations),
    // so wrap it in a function body.
    eval('(function () {' + blockSync + ' })')();
    return toasts.filter((x) => x !== 'link');
}

if (blockSync) {
    try {
        // A6a: the honest claim — API says nothing was sent, link handed over.
        const t1 = simToast(true, 'false');
        check('A6a', t1.length === 1 && t1[0] === NEW_TOAST, 'email_delivered=false + accept_link -> the new toast only');
        // A6b: F-A fixed — CE WITH mail configured: an email WAS sent -> old toast.
        const t2 = simToast(true, 'true');
        check('A6b', t2.length === 1 && t2[0] === OLD_TOAST, 'email_delivered=true + accept_link -> the original toast (no lie)');
        // A6c: older API producer (field absent) -> unchanged, no claim.
        const t3 = simToast(true, 'none');
        check('A6c', t3.length === 1 && t3[0] === OLD_TOAST, 'email_delivered undefined + accept_link -> original toast');
        // A6d: strict false only — a truthy-but-not-false value never claims.
        const t4 = simToast(true, 'none');
        check('A6d', t4.length === 1 && t4[0] === OLD_TOAST, 'no claim unless email_delivered is STRICT false');
        // A6e: the claim needs the link too (nothing to copy otherwise).
        const t5 = simToast(false, 'false');
        check('A6e', t5.length === 1 && t5[0] === OLD_TOAST, 'email_delivered=false, no accept_link -> original toast');
        // A6f: paid create (email always sent -> True) keeps the original toast.
        const t6 = simToast(true, 'true');
        check('A6f', t6.length === 1 && t6[0] === OLD_TOAST, 'paid create (email_delivered=true) -> original toast byte-for-byte');
    } catch (e) { check('A6x', false, 'toast sim threw: ' + e.message); }
} else {
    ['A6a', 'A6b', 'A6c', 'A6d', 'A6e', 'A6f'].forEach(n => check(n, false, 'block unavailable'));
}

// --- B1/B2: the CE guard removes the CONTAINER node ------------------------
check('B1', /if \(!await isPaidEdition\(\)\) \{[\s\S]{0,700}?card\.remove\(\);[\s\S]{0,120}?\n\s*return;/.test(invSrc),
    'the !isPaidEdition() branch calls card.remove() before returning');
check('B2', /const card = form\.parentElement;/.test(invSrc), 'the removed node is the form\'s container (form.parentElement)');

const initSrc = extractFn(invSrc, 'export async function initializeExternalInvites(');
check('B3', !!initSrc, 'initializeExternalInvites exists');

// Stubs are PARAMETERS of a module-scope function so the eval'd copy resolves
// them through its scope chain.
function simCard(paid) {
    const body = initSrc.replace(/^export\s+/, '').replace(/\bawait\s+/g, '');
    const root = { children: [] };
    const card = { style: { display: '' }, children: [], remove: () => { root.children = root.children.filter((c) => c !== card); } };
    const form = { style: { display: '' }, listeners: {}, addEventListener: (t, fn) => { (form.listeners[t] = form.listeners[t] || []).push(fn); } };
    form.parentElement = card;
    card.children.push(form);
    root.children.push(card);
    const document = { getElementById: (id) => (id === 'external-invite-form' ? form : null) };
    const isPaidEdition = () => paid;
    const handleCreateExternalInvite = () => {};
    const loadExternalCogOptions = () => {};
    const loadExternalInvitations = () => {};
    const copyToClipboard = (x) => true;
    const externalInvites = { cancel: (i) => {}, acceptLink: (t) => 'L' };
    const showError = (m) => {};
    const showInfo = (m) => {};
    eval('(' + body + ')')();
    return { display: card.style.display, inDom: root.children.some((c) => c === card),
        submitN: (form.listeners['submit'] || []).length };
}

if (initSrc) {
    try {
        const ce = simCard(false);
        check('B4a', ce.display === 'none', 'CE: container still display:none');
        check('B4b', !ce.inDom, 'CE: the container NODE is gone from the DOM');
        check('B4c', ce.submitN === 0, 'CE: no submit listener wired (no dead 404 call)');
        const pd = simCard(true);
        check('C1a', pd.display === '', 'paid: container untouched (no display change)');
        check('C1b', pd.inDom, 'paid: container still in the DOM');
        check('C1c', pd.submitN === 1, 'paid: submit listener wired as before');
    } catch (e) { check('B5', false, 'card sim threw: ' + e.message); }
} else {
    ['B4a', 'B4b', 'B4c', 'C1a', 'C1b', 'C1c'].forEach(n => check(n, false, 'function unavailable'));
}

const failed = results.filter((r) => !r[1]);
if (failed.length) {
    console.log(`dash-invitations-no-email-toast-and-empty-external-card.check.js: ${failed.length}/${results.length} FAILED`);
    process.exit(1);
}
console.log(`dash-invitations-no-email-toast-and-empty-external-card.check.js: all ${results.length} checks passed -> OK`);
assert.ok(failed.length === 0, 'see FAIL lines above');
