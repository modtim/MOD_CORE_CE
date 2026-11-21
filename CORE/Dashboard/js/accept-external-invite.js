// accept-external-invite.js (external-invite-accept-ui)
//
// Public accept page for external folder invitations. Reachable WITHOUT a
// login session — the invitation token in the URL is the credential. Reads
// ?token=<token>, looks up the invite via the public by-token endpoint to
// show what's being accepted, collects the invitee's name/username/password,
// POSTs the accept, and routes into sign-in on success.

import { externalInvites, getPlatformFeatures } from './api.js';

const els = {};
function $(id) { return document.getElementById(id); }

function show(section) {
    ['ext-loading', 'ext-error', 'ext-form-wrap', 'ext-success'].forEach(id => {
        const node = $(id);
        if (node) node.classList.toggle('hidden', id !== section);
    });
}

function showError(message) {
    const msg = $('ext-error-message');
    if (msg) msg.textContent = message;
    show('ext-error');
}

function showFormError(message) {
    const el = $('ext-form-error');
    if (!el) return;
    el.textContent = message;
    el.classList.remove('hidden');
}

// Map a failed lookup/accept to a clear, human message by status code.
function messageForStatus(status, fallback) {
    if (status === 404) return 'This invitation link is invalid, has expired, or has already been used. Ask whoever invited you to send a fresh link.';
    if (status === 409) return 'This invitation has already been accepted. Try signing in with your email instead.';
    if (status === 410) return 'This invitation has expired. Ask whoever invited you to send a fresh link.';
    return fallback || 'Something went wrong while loading this invitation. Please try again.';
}

document.addEventListener('DOMContentLoaded', async () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');

    // 2400LR-pg1 — external_invitations*.py is denied on CE (open-core/manifest.yaml :1430), so the
    // by-token lookup always 404s there. This gate fails closed ON ITS OWN (the
    // .catch below resolves CE on an unresolved entitlements lookup); ui.js:283
    // _isCeEdition is still fail-open there and is aligned by FAILOPEN-A.
    const isCe = await getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !!ent && ent.edition === 'ce' && !ent.error;
    }).catch(() => true);
    if (isCe) {
        showError('External folder invitations are not available in MOD Community Edition.');
        return;
    }

    if (!token) {
        showError('This invitation link is missing its token. Please use the exact link you were sent.');
        return;
    }

    // 1. Look up the invite so we can show what's being accepted.
    let invite;
    try {
        invite = await externalInvites.byToken(token);
    } catch (err) {
        showError(messageForStatus(err.status, err.message));
        return;
    }

    // Populate the form context.
    if ($('ext-cog-name')) $('ext-cog-name').textContent = invite.cog_name || 'a shared folder';
    if ($('ext-level')) $('ext-level').textContent = invite.permission_level || 'a collaborator';
    if ($('ext-email')) $('ext-email').value = invite.email || '';
    // Sensible username default from the email local-part.
    const usernameEl = $('ext-username');
    if (usernameEl && invite.email) usernameEl.value = invite.email.split('@')[0];

    show('ext-form-wrap');

    // 2. Wire the accept submission.
    const form = $('ext-accept-form');
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const errEl = $('ext-form-error');
        if (errEl) errEl.classList.add('hidden');

        const firstName = $('ext-first').value.trim();
        const lastName = $('ext-last').value.trim();
        const username = $('ext-username').value.trim();
        const password = $('ext-password').value; // optional

        if (!firstName || !lastName || !username) {
            showFormError('Please fill in your first name, last name, and username.');
            return;
        }

        const submit = $('ext-submit');
        submit.disabled = true;
        submit.textContent = 'Creating your account…';

        const body = { token, username, first_name: firstName, last_name: lastName };
        if (password) body.password = password;

        try {
            await externalInvites.accept(invite.id, body);
            show('ext-success');
        } catch (err) {
            submit.disabled = false;
            submit.textContent = 'Accept invitation';
            // 404/expired/used and 409/already-accepted get the full-page
            // error treatment (the invite is no longer actionable); other
            // failures (e.g. username taken, 400/422) stay inline so the
            // user can correct and retry.
            if (err.status === 404 || err.status === 409 || err.status === 410) {
                showError(messageForStatus(err.status, err.message));
            } else {
                showFormError(err.message || 'Could not accept the invitation. Please try again.');
            }
        }
    });
});
