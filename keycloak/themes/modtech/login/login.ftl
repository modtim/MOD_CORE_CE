<#import "template.ftl" as layout>
<#--
  add-self-service-sso §5.1 — email-first home-realm-discovery.

  Step 1 (initial render): email field only. Password field is in the
  DOM but display:none — necessary because submitting back to
  Keycloak's auth handler requires the password field to exist when
  the user follows the "Use password instead" escape hatch.

  Step 2: Continue click → JS calls /sso/resolve?email=<email>:
    - {redirect_url: "..."}  → window.location = redirect_url
    - {password_form: true}  → reveal password field, swap button to
                                Sign in, change tabindex order

  Escape hatch (§5.4): "Use password instead" link is visible from
  step 1, lets the user bypass the resolver entirely.

  kc_idp_hint (§5.5): Keycloak itself handles this URL parameter at
  the broker layer — when present, the user never lands on this
  page in the first place; Keycloak redirects them straight to the
  hinted IdP. Nothing for the FTL to do.

  Error-case shortcut: if Keycloak rendered THIS template with a
  pre-existing username/password error (e.g. retry after wrong
  password), we skip the email-first step entirely and show the
  full form so the user can re-enter without re-typing the email.
-->
<#assign isErrorRetry = messagesPerField.existsError('username','password') />
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('username','password') displayInfo=realm.password && realm.registrationAllowed && !registrationDisabled??; section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("loginAccountTitle")}</h2>
        <p class="modtech-form-subtitle">Sign in to continue to ${realm.displayName!''}</p>
    <#elseif section = "form">
        <form id="kc-form-login" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post" class="modtech-form">
            <#if !usernameHidden??>
                <div class="modtech-form-group">
                    <label for="username" class="modtech-label">
                        <#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>${msg("email")}</#if>
                    </label>
                    <input tabindex="1" id="username"
                           class="modtech-input <#if messagesPerField.existsError('username','password')>modtech-input-error</#if>"
                           name="username"
                           value="${(login.username!'')}"
                           type="text"
                           autofocus
                           autocomplete="username"
                           placeholder="<#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>name@company.com</#if>"
                           aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>" />
                    <#if messagesPerField.existsError('username','password')>
                        <span class="modtech-error-message" aria-live="polite">
                            ${kcSanitize(messagesPerField.getFirstError('username','password'))?no_esc}
                        </span>
                    </#if>
                </div>
            </#if>

            <#-- Password field starts hidden in the email-first flow,
                 visible when this is an error retry. -->
            <div id="mod-password-group" class="modtech-form-group"
                 <#if !isErrorRetry>style="display:none"</#if>>
                <label for="password" class="modtech-label">${msg("password")}</label>
                <input tabindex="2" id="password"
                       class="modtech-input <#if messagesPerField.existsError('username','password')>modtech-input-error</#if>"
                       name="password"
                       type="password"
                       autocomplete="current-password"
                       placeholder="Enter your password"
                       aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>" />

                <#if usernameHidden?? && messagesPerField.existsError('username','password')>
                    <span class="modtech-error-message" aria-live="polite">
                        ${kcSanitize(messagesPerField.getFirstError('username','password'))?no_esc}
                    </span>
                </#if>
            </div>

            <#-- Forgot-password + remember-me only visible alongside the
                 password field (the email-first step doesn't need them). -->
            <div id="mod-password-options" class="modtech-form-options"
                 <#if !isErrorRetry>style="display:none"</#if>>
                <#if realm.rememberMe && !usernameHidden??>
                    <div class="modtech-checkbox-group">
                        <input tabindex="3" id="rememberMe" name="rememberMe" type="checkbox" class="modtech-checkbox"
                               <#if login.rememberMe??>checked</#if>>
                        <label for="rememberMe" class="modtech-checkbox-label">${msg("rememberMe")}</label>
                    </div>
                </#if>

                <#if realm.resetPasswordAllowed>
                    <a tabindex="5" href="${url.loginResetCredentialsUrl}" class="modtech-link">${msg("doForgotPassword")}</a>
                </#if>
            </div>

            <div class="modtech-form-actions">
                <input type="hidden" id="id-hidden-input" name="credentialId" <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if>/>
                <#-- Step 1 (default): Continue button calls /sso/resolve.
                     Step 2 (after reveal): becomes a Sign in button that
                     POSTs normally. JS swaps text + behaviour. -->
                <button tabindex="4" class="modtech-btn modtech-btn-primary" name="login" id="kc-login" type="submit">
                    <#if isErrorRetry>${msg("doLogIn")}<#else>Continue</#if>
                </button>
            </div>

            <#-- §5.4 Escape hatch: "Use password instead" link, visible
                 on step 1 only. Click reveals the password form even
                 when the user's email matches an SSO domain. -->
            <#if !isErrorRetry>
                <div id="mod-escape-hatch" class="modtech-form-options" style="justify-content: center; margin-top: 12px;">
                    <a href="#" id="mod-use-password" class="modtech-link" style="font-size: 13px;">
                        Use password instead
                    </a>
                </div>
            </#if>
        </form>

        <#-- Inline script for the home-realm-discovery handshake.
             v1: small enough to inline. If this grows, split to a
             theme-resource JS file under resources/js/. -->
        <#if !isErrorRetry>
        <script>
        (function () {
            // MOD API base URL. Preferred: the explicit theme.properties
            // value modApiBaseUrl (login/theme.properties — kept in sync
            // with CORE/startup/.env PROTOCOL/API_SUBDOMAIN/BASE_DOMAIN).
            // The clean-v2 API is /v1-only, so the base carries /v1.
            // Fallback only if the property is ever removed: derive from
            // the Keycloak hostname (dev-keycloak.X → dev-api.X; the old
            // /^(stage-)?keycloak/ regex never matched dev-keycloak, so
            // the beacon was POSTed at the KEYCLOAK host and 404'd).
            var modApiBaseUrl = '<#if properties.modApiBaseUrl?has_content>${properties.modApiBaseUrl}</#if>';
            function modApiUrl() {
                if (modApiBaseUrl) {
                    return modApiBaseUrl;
                }
                var loc = window.location;
                var host = loc.hostname;
                if (host === 'localhost' || host === '127.0.0.1') {
                    return 'http://api.localhost/v1';
                }
                // <prefix->keycloak.<base-domain> → <prefix->api.<base-domain>
                // (dev-, stage-, or bare keycloak) + the /v1 API prefix. If the
                // host has no "keycloak" in it we can't derive the API host —
                // return null so callers skip (never POST at the Keycloak host).
                if (host.indexOf('keycloak') === -1) {
                    return null;
                }
                return loc.protocol + '//' + host.replace(/keycloak/, 'api') + '/v1';
            }

            // §5.8 RUM — beacon the login page's first-paint duration
            // back to the API so we can alert on slow renders. Use the
            // navigation-timing API when available; fall back to a no-op
            // on browsers that don't expose it (none in our target set,
            // but defensive).
            function emitFirstPaintBeacon() {
                try {
                    var ms = null;
                    if (window.performance && performance.getEntriesByType) {
                        var navEntries = performance.getEntriesByType('navigation');
                        if (navEntries && navEntries.length > 0) {
                            // domContentLoadedEventEnd is closer to "the user
                            // sees the form" than loadEventEnd, which waits
                            // on every transitive resource.
                            ms = Math.round(navEntries[0].domContentLoadedEventEnd);
                        }
                    }
                    if (!ms && window.performance && performance.timing) {
                        var t = performance.timing;
                        ms = t.domContentLoadedEventEnd - t.navigationStart;
                    }
                    if (!ms || ms <= 0 || ms >= 60000) return;
                    var apiBase = modApiUrl();
                    if (!apiBase) return;  // can't derive the API host — skip
                    // navigator.sendBeacon is fire-and-forget and doesn't
                    // block page nav. fetch is the fallback.
                    var body = JSON.stringify({ ms: ms });
                    // text/plain keeps this a CORS "simple request": sendBeacon
                    // never does a preflight, so a non-safelisted type like
                    // application/json would be blocked outright. The API route
                    // must parse the raw body as JSON (see sso_rum.py note).
                    if (navigator.sendBeacon) {
                        var blob = new Blob([body], { type: 'text/plain' });
                        navigator.sendBeacon(apiBase + '/sso/rum/login-first-paint', blob);
                    } else {
                        fetch(apiBase + '/sso/rum/login-first-paint', {
                            method: 'POST',
                            headers: { 'Content-Type': 'text/plain' },
                            body: body,
                            credentials: 'omit',
                            keepalive: true,
                        }).catch(function () { /* swallow */ });
                    }
                } catch (e) { /* swallow — RUM never blocks login */ }
            }
            if (document.readyState === 'complete') {
                emitFirstPaintBeacon();
            } else {
                window.addEventListener('load', emitFirstPaintBeacon);
            }

            var form = document.getElementById('kc-form-login');
            var usernameEl = document.getElementById('username');
            var passwordGroup = document.getElementById('mod-password-group');
            var passwordOptions = document.getElementById('mod-password-options');
            var passwordEl = document.getElementById('password');
            var loginBtn = document.getElementById('kc-login');
            var escapeHatch = document.getElementById('mod-escape-hatch');
            var usePasswordLink = document.getElementById('mod-use-password');
            var inEmailFirstStep = true;

            function revealPasswordForm() {
                if (!passwordGroup) return;
                passwordGroup.style.display = '';
                if (passwordOptions) passwordOptions.style.display = '';
                if (escapeHatch) escapeHatch.style.display = 'none';
                if (loginBtn) loginBtn.textContent = '${msg("doLogIn")}';
                inEmailFirstStep = false;
                if (passwordEl) passwordEl.focus();
            }

            // §5.4 escape hatch: clicking "Use password instead" jumps
            // straight to step 2 even if the email's domain has an SSO
            // match. Logged on server side as SSO_PASSWORD_BYPASS_USED
            // (§5.6, separate task).
            if (usePasswordLink) {
                usePasswordLink.addEventListener('click', function (e) {
                    e.preventDefault();
                    revealPasswordForm();
                });
            }

            // Intercept the form's first submission on step 1 — call the
            // resolver, redirect or reveal based on the answer. After
            // step 1 the form submits normally to Keycloak.
            form.addEventListener('submit', function (e) {
                if (!inEmailFirstStep) return;  // step 2: pass through
                e.preventDefault();
                var email = (usernameEl && usernameEl.value || '').trim();
                if (!email) {
                    if (usernameEl) usernameEl.focus();
                    return;
                }
                loginBtn.disabled = true;
                var apiBase = modApiUrl();
                if (!apiBase) {
                    // API host not derivable from this Keycloak host — skip
                    // the resolver handshake and just submit normally.
                    revealPasswordForm();
                    loginBtn.disabled = false;
                    return;
                }
                fetch(apiBase + '/sso/resolve?email=' + encodeURIComponent(email), {
                    method: 'GET',
                    credentials: 'omit',
                }).then(function (r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                }).then(function (data) {
                    if (data && data.redirect_url) {
                        // SSO match — redirect to Keycloak's broker
                        // endpoint for the matched IdP. Keycloak preserves
                        // the original /auth/.../auth?... state via
                        // session storage.
                        window.location.href = data.redirect_url;
                        return;
                    }
                    // No SSO match (or password_form=true). Reveal the
                    // password form so the user can complete sign-in.
                    revealPasswordForm();
                    loginBtn.disabled = false;
                }).catch(function () {
                    // Resolver unreachable / errored: fall through to
                    // password form so the login still works.
                    revealPasswordForm();
                    loginBtn.disabled = false;
                });
            });
        })();
        </script>
        </#if>
    <#elseif section = "socialProviders">
        <#if realm.password && social?? && social.providers?has_content>
            <div class="modtech-social-divider">
                <span>or continue with</span>
            </div>
            <div class="modtech-social-providers">
                <#list social.providers as p>
                    <a id="social-${p.alias}" class="modtech-social-btn" href="${p.loginUrl}">
                        <#if p.iconClasses?has_content>
                            <i class="${p.iconClasses!}" aria-hidden="true"></i>
                        </#if>
                        <span>${p.displayName!}</span>
                    </a>
                </#list>
            </div>
        </#if>
    <#elseif section = "info">
        <#if realm.password && realm.registrationAllowed && !registrationDisabled??>
            <div class="modtech-register-link">
                <span>${msg("noAccount")}</span>
                <a tabindex="6" href="${url.registrationUrl}" class="modtech-link">${msg("doRegister")}</a>
            </div>
        </#if>
    </#if>
</@layout.registrationLayout>
