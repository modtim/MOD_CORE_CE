// main.js - COMPLETE FILE
// In Dashboard/js/main.js

import { checkApiOnboarding, exchangeCodeForToken, REDIRECT_URI, getRegistrationUrl } from './auth.js';
import { attachEventListeners, showPage } from './ui.js';
import { initOnboardingChecklist } from './onboarding-checklist.js';

// BUG.B47 — single-boot guard. A browser refresh must boot the app exactly
// once. Anything that re-dispatches DOMContentLoaded (or a late script error
// that makes network-bootstrap.js reload the page after we already rendered)
// used to re-run the whole boot cascade — the "app restarts several times
// before it loads" symptom. The flag is published on window so network-bootstrap.js
// can refuse to reload once the app has booted.
document.addEventListener("DOMContentLoaded", async function () {
    if (window.__modBooted) return;
    window.__modBooted = true;

    // 1. Load the essential HTML structure for all pages first.
    const [loginHtml, onboardingHtml, dashboardHtml, loggedOutHtml, verifyEmailHtml] = await Promise.all([
        fetch('/pages/login.html').then(res => res.text()),
        fetch('/pages/onboarding.html').then(res => res.text()),
        fetch('/pages/dashboard-shell.html').then(res => res.text()),
        fetch('/pages/logged-out.html').then(res => res.text()),
        fetch('/pages/verify-email.html').then(res => res.text()),
    ]);

    document.getElementById('login-page').innerHTML = loginHtml;
    document.getElementById('onboarding-page').innerHTML = onboardingHtml;
    document.getElementById('dashboard').innerHTML = dashboardHtml;
    document.getElementById('logged-out-page').innerHTML = loggedOutHtml;
    document.getElementById('verify-email-page').innerHTML = verifyEmailHtml;

    // 2. Now that the core DOM is ready, initialize the application.
    function hideLoadingScreen() {
        const loading = document.getElementById('auth-loading-screen');
        if (loading) loading.classList.add('hidden');
    }

    async function initializeApp() {
        attachEventListeners();

        const params = new URLSearchParams(window.location.search);
        const code = params.get('code');
        const error = params.get('error');
        const state = params.get('state');
        const loggedOut = params.get('logged_out');
        const continueTo = params.get('continue_to');
        const kcActionStatus = params.get('kc_action_status');

        // Post-logout "continue to signup" handoff from the onboarding page's
        // Sign up escape hatch. At this point Keycloak has dropped the SSO
        // cookie, so redirecting to the registration URL will actually show
        // the sign-up form instead of silently SSO-ing back to the old user.
        if (continueTo === 'signup') {
            window.history.replaceState({}, document.title, window.location.pathname);
            window.location.href = await getRegistrationUrl();
            return;
        }

        // If the Keycloak info page forwarded a success flag (currently
        // fires on email verification — see keycloak-themes/.../info.ftl),
        // surface a themed success popup and start a fresh login.
        //
        // Why the aggressive param-strip + fresh-login fallback: the email
        // link typically opens in a NEW browser tab, which has no access to
        // the original tab's sessionStorage, so the PKCE code_verifier is
        // gone. Keycloak would then reject the code exchange with
        // code_verifier_missing and the dashboard would bounce the user
        // straight back to login with no feedback at all. Treating this
        // flag as an interstitial event ('show success, log in clean')
        // avoids the failed exchange entirely.
        // GY.C4.MFA — the 2FA / password action started from Profile → Security
        // returns here with kc_action_status=success. The marker lives in
        // localStorage (not sessionStorage) because the action typically
        // completes in a different tab, so the toast can name the action the
        // user actually performed instead of assuming email verification.
        let pendingAction = null;
        try { pendingAction = localStorage.getItem('mod_pending_action'); } catch (_) {}
        const actionLabel = pendingAction === 'CONFIGURE_TOTP'
            ? 'Two-factor authentication' : 'Password';

        if (kcActionStatus === 'success') {
            const url = new URL(window.location.href);
            ['kc_action_status', 'code', 'state', 'session_state', 'iss'].forEach(k => url.searchParams.delete(k));
            window.history.replaceState({}, document.title, url.pathname + (url.search || '') + url.hash);
            // Clear any leftover auth state — verifier now lives in
            // localStorage (Option C), but we also clear the old
            // sessionStorage key for safety during the transition.
            try { localStorage.removeItem('pkce_code_verifier'); } catch (_) {}
            try { sessionStorage.removeItem('pkce_code_verifier'); } catch (_) {}
            try { localStorage.removeItem('mod_pending_action'); } catch (_) {}
            // Defer to next tick so ui.js's window.showInfo has attached.
            setTimeout(() => {
                try {
                    const message = pendingAction
                        ? `${actionLabel} updated successfully. Sign in with your account to continue.`
                        : 'Your email address has been verified. Sign in with your account to continue.';
                    window.showInfo && window.showInfo(message, pendingAction ? `${actionLabel} updated` : 'Email verified');
                } catch (_) { /* fallback: silent */ }
            }, 50);
            hideLoadingScreen();
            showPage(document.getElementById("login-page"));
            return; // don't attempt the failing auth-code exchange
        }

        // Visible failure: the MOD-themed page rejected the code, the user
        // cancelled, or the action errored — say so instead of silently
        // redrawing the login form.
        if (error && pendingAction) {
            try { localStorage.removeItem('mod_pending_action'); } catch (_) {}
            setTimeout(() => {
                try {
                    window.showError && window.showError(
                        `${actionLabel} could not be completed (${error}). Sign in again and try once more.`,
                        `${actionLabel} failed`);
                } catch (_) { /* fallback: silent */ }
            }, 50);
        }

        if (loggedOut === 'true') {
            window.history.replaceState({}, document.title, window.location.pathname);
            hideLoadingScreen();
            showLoggedOutPage();
            return;
        }

        if (error) {
            console.error("Keycloak returned an error:", error);
            hideLoadingScreen();
            showPage(document.getElementById("login-page"));
            return;
        }

        if (code) {
            // Check if we have state data from an invitation flow
            if (state) {
                try {
                    const stateData = JSON.parse(atob(state));

                    // If this is an invitation flow, store the token in sessionStorage
                    if (stateData.flow === 'invitation' && stateData.invitation_token) {
                        sessionStorage.setItem('pending_invitation_token', stateData.invitation_token);
                        console.log("Restored invitation token from state parameter");
                    }
                } catch (e) {
                    console.error("Failed to parse state parameter:", e);
                }
            }

            await exchangeCodeForToken(code, REDIRECT_URI);
            hideLoadingScreen();
            await checkApiOnboarding();

        } else if (sessionStorage.getItem("accessToken")) {
            hideLoadingScreen();
            await checkApiOnboarding();
        } else {
            hideLoadingScreen();
            showPage(document.getElementById("login-page"));
        }
    }

    function showLoggedOutPage() {
        showPage(document.getElementById("logged-out-page"));

        const countdownEl = document.getElementById('logout-countdown');
        const progressBar = document.getElementById('logout-progress-bar');
        const signInBtn = document.getElementById('back-to-login-button');
        let remaining = 5;

        function goToLogin() {
            clearInterval(timer);
            showPage(document.getElementById("login-page"));
        }

        if (signInBtn) {
            signInBtn.addEventListener('click', goToLogin);
        }

        if (progressBar) {
            progressBar.style.width = '0%';
            requestAnimationFrame(() => { progressBar.style.width = '100%'; progressBar.style.transitionDuration = '5s'; });
        }

        const timer = setInterval(() => {
            remaining--;
            if (countdownEl) countdownEl.textContent = remaining;
            if (remaining <= 0) goToLogin();
        }, 1000);
    }

    initializeApp();
});