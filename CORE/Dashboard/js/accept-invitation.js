// accept-invitation.js - COMPLETE FILE
import { getRegistrationUrl, handleLogin } from './auth.js';
import { getPlatformFeatures } from './api.js';

document.addEventListener("DOMContentLoaded", async () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    const errorEl = document.getElementById('error-message');
    const acceptButton = document.getElementById('accept-button');

    // 2400LR-ceSignup-a2 — CE edition predicate: the SAME signal ui.js reads
    // (_isCeEdition, ui.js:283 — entitlements.edition off the cached
    // /platform/features) and accept-external-invite.js:48. CE ships
    // registrationAllowed:false (open-core/overlay/keycloak/core-realm.json:4),
    // so Keycloak's /openid-connect/registrations redirect below is a dead end
    // there; CE invitees POST the token to the public accept endpoint instead
    // (token in the BODY, never the URL). Fail-closed: unknown edition → block
    // today's redirect branch, byte-identical.
    const isCe = await getPlatformFeatures().then((f) => {
        const ent = f && f.entitlements;
        return !!ent && ent.edition === 'ce' && !ent.error;
    }).catch(() => true);

    if (!token) {
        errorEl.textContent = 'Invitation token is missing or invalid.';
        errorEl.classList.remove('hidden');
        acceptButton.disabled = true;
        acceptButton.classList.add('opacity-50', 'cursor-not-allowed');
        return;
    }

    // Look up the invited email on page load so we can (a) show a real
    // "invalid or expired" state for a bad link and (b) pre-fill (and lock)
    // the Keycloak registration form. A 404/410 means the token is unknown,
    // expired or already used. Network/CORS errors are non-fatal — the user
    // just types their email manually; /finalize-invited-user still rejects
    // a mismatched email.
    let invitedEmail = null;
    const prefillReady = (async () => {
        try {
            const apiBase = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            const resp = await fetch(`${apiBase}/onboarding/invitation-prefill/${encodeURIComponent(token)}`);
            if (resp.ok) {
                const data = await resp.json();
                invitedEmail = data.email || null;
            } else if (resp.status === 404 || resp.status === 410) {
                errorEl.textContent = 'This invitation link is invalid or has expired. Ask your administrator to send a new one.';
                errorEl.classList.remove('hidden');
                acceptButton.disabled = true;
                acceptButton.classList.add('opacity-50', 'cursor-not-allowed');
                return false;
            }
        } catch (_) {
            // Network/CORS — silently fall through to manual entry.
        }
        return true;
    })();

    if (isCe) {
        // --- CE accept form (2400LR-ceSignup-a2) ---------------------------------
        // Field layout + escaping copied from accept-external-invite.js:91-103 /
        // pages/accept-external-invite.html:40-64 (readonly email, first/last,
        // password). All server text enters via textContent, never innerHTML.
        const formWrap = document.getElementById('ce-form-wrap');
        const form = document.getElementById('ce-accept-form');
        if (!formWrap || !form) return; // markup missing — do nothing, no redirect

        acceptButton.style.display = 'none'; // the CE flow submits the form, not the button

        const emailEl = document.getElementById('ce-email');
        if (emailEl) emailEl.textContent = invitedEmail || 'the email your administrator invited';
        formWrap.classList.remove('hidden');

        const formErrorEl = document.getElementById('ce-form-error');
        const showFormError = (message) => {
            if (formErrorEl) {
                formErrorEl.textContent = message;
                formErrorEl.classList.remove('hidden');
            }
        };

        // Contract (2400LR-ceSignup-a1): POST /v1/onboarding/invitations/accept
        // body {token, first_name, last_name, password}. 201 {status:"accepted"};
        // 404 same text for every bad-token case; 409 {code:"account_exists"};
        // 422 KC password-policy message. Token goes in the BODY only.
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!(await prefillReady)) return; // bad/expired link already shown in #error-message
            if (formErrorEl) formErrorEl.classList.add('hidden');

            const firstName = document.getElementById('ce-first')?.value.trim() || '';
            const lastName = document.getElementById('ce-last')?.value.trim() || '';
            const password = document.getElementById('ce-password')?.value || '';
            const confirm = document.getElementById('ce-password-confirm')?.value || '';

            if (!firstName || !lastName) {
                showFormError('Please fill in your first name and last name.');
                return;
            }
            if (password !== confirm) {
                showFormError('The two passwords you typed do not match.');
                return;
            }
            if (password.length < 12) {
                // Client-side mirror of the realm policy; the server/KC is the authority.
                showFormError('Your password must be at least 12 characters.');
                return;
            }

            const submit = document.getElementById('ce-submit');
            if (submit) {
                submit.disabled = true;
                submit.textContent = 'Creating your account…';
            }

            const apiBase = window.APP_CONFIG?.API_URL || 'http://api.localhost';
            try {
                const resp = await fetch(`${apiBase}/onboarding/invitations/accept`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        token,
                        first_name: firstName,
                        last_name: lastName,
                        password,
                    }),
                });
                let data = null;
                try { data = await resp.json(); } catch (_) { /* non-JSON body */ }

                if (resp.ok) {
                    const success = document.getElementById('ce-success');
                    if (success) success.classList.remove('hidden');
                    // After 201 the invitee goes to the NORMAL login (auth.js
                    // handleLogin, auth.js:202 — getLoginUrl auth.js:100), never
                    // the registration endpoint.
                    handleLogin();
                    return;
                }
                if (submit) {
                    submit.disabled = false;
                    submit.textContent = 'Accept invitation';
                }

                // Server text via textContent only. FastAPI wraps HTTPException
                // payloads in {detail: ...}; the contract's 409 is {code:"account_exists"}.
                let payload = data;
                if (data && typeof data.detail !== 'undefined') payload = data.detail;
                const code = (payload && typeof payload === 'object') ? (payload.code || null) : null;
                const serverMsg = (typeof payload === 'string') ? payload
                    : (payload && typeof payload === 'object' && typeof payload.detail === 'string') ? payload.detail : null;

                if (resp.status === 409 && code === 'account_exists') {
                    showFormError('You already have an account — log in, then open this invite link again.');
                } else if (resp.status === 422) {
                    // KC password-policy message only (the API passes it through);
                    // never the raw KC body.
                    showFormError(serverMsg || 'Keycloak rejected that password. Please choose a stronger one and try again.');
                } else if (resp.status === 404 || resp.status === 410) {
                    // Whole-page treatment: the invite is no longer actionable.
                    const err = document.getElementById('error-message');
                    if (err) {
                        err.textContent = serverMsg || 'This invitation link is invalid or has expired. Ask your administrator to send a new one.';
                        err.classList.remove('hidden');
                    }
                    formWrap.classList.add('hidden');
                } else {
                    showFormError(serverMsg || 'Could not accept the invitation. Please try again.');
                }
            } catch (_) {
                if (submit) {
                    submit.disabled = false;
                    submit.textContent = 'Accept invitation';
                }
                showFormError('Could not reach MOD. Please check the link and try again.');
            }
        });
        return;
    }

    acceptButton.addEventListener('click', async () => {
        if (!(await prefillReady)) return;

        // Store the invitation token in the OAuth state parameter
        // This will survive the redirect to Keycloak and back
        const stateData = {
            invitation_token: token,
            flow: 'invitation'
        };

        // Encode the state data
        const state = btoa(JSON.stringify(stateData));

        // Get the registration URL with redirect back to main page (not accept-invitation page)
        const registrationUrl = await getRegistrationUrl();

        // Override redirect_uri to go back to index.html instead of accept-invitation.html
        const url = new URL(registrationUrl);
        url.searchParams.set('redirect_uri', window.location.origin + '/');
        url.searchParams.set('state', state);
        if (invitedEmail) {
            // Picked up by register.ftl's prefill JS to populate +
            // lock the email input.
            url.searchParams.set('email', invitedEmail);
        }

        window.location.href = url.toString();
    });
});
