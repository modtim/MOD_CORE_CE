<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('firstName','lastName','email','username','password','password-confirm') displayInfo=false; section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("registerTitle")}</h2>
        <p class="modtech-form-subtitle">Create your account to get started</p>
    <#elseif section = "form">
        <form id="kc-register-form" class="modtech-form" action="${url.registrationAction}" method="post">
            <div class="modtech-form-row">
                <div class="modtech-form-group modtech-form-group-half">
                    <label for="firstName" class="modtech-label">${msg("firstName")}</label>
                    <input type="text" id="firstName"
                           class="modtech-input <#if messagesPerField.existsError('firstName')>modtech-input-error</#if>"
                           name="firstName"
                           value="${(register.formData.firstName!'')}"
                           placeholder="First name"
                           aria-invalid="<#if messagesPerField.existsError('firstName')>true</#if>" />
                    <#if messagesPerField.existsError('firstName')>
                        <span class="modtech-error-message">${kcSanitize(messagesPerField.get('firstName'))?no_esc}</span>
                    </#if>
                </div>

                <div class="modtech-form-group modtech-form-group-half">
                    <label for="lastName" class="modtech-label">${msg("lastName")}</label>
                    <input type="text" id="lastName"
                           class="modtech-input <#if messagesPerField.existsError('lastName')>modtech-input-error</#if>"
                           name="lastName"
                           value="${(register.formData.lastName!'')}"
                           placeholder="Last name"
                           aria-invalid="<#if messagesPerField.existsError('lastName')>true</#if>" />
                    <#if messagesPerField.existsError('lastName')>
                        <span class="modtech-error-message">${kcSanitize(messagesPerField.get('lastName'))?no_esc}</span>
                    </#if>
                </div>
            </div>

            <div class="modtech-form-group">
                <label for="email" class="modtech-label">${msg("email")}</label>
                <input type="email" id="email"
                       class="modtech-input <#if messagesPerField.existsError('email')>modtech-input-error</#if>"
                       name="email"
                       value="${(register.formData.email!'')}"
                       autocomplete="email"
                       placeholder="name@company.com"
                       aria-invalid="<#if messagesPerField.existsError('email')>true</#if>" />
                <#if messagesPerField.existsError('email')>
                    <span class="modtech-error-message">${kcSanitize(messagesPerField.get('email'))?no_esc}</span>
                </#if>
            </div>

            <#if !realm.registrationEmailAsUsername>
                <div class="modtech-form-group">
                    <label for="username" class="modtech-label">${msg("username")}</label>
                    <input type="text" id="username"
                           class="modtech-input <#if messagesPerField.existsError('username')>modtech-input-error</#if>"
                           name="username"
                           value="${(register.formData.username!'')}"
                           autocomplete="username"
                           placeholder="Choose a username"
                           aria-invalid="<#if messagesPerField.existsError('username')>true</#if>" />
                    <#if messagesPerField.existsError('username')>
                        <span class="modtech-error-message">${kcSanitize(messagesPerField.get('username'))?no_esc}</span>
                    </#if>
                </div>
            </#if>

            <#if passwordRequired??>
                <div class="modtech-form-group">
                    <label for="password" class="modtech-label">${msg("password")}</label>
                    <input type="password" id="password"
                           class="modtech-input <#if messagesPerField.existsError('password')>modtech-input-error</#if>"
                           name="password"
                           autocomplete="new-password"
                           placeholder="Create a password"
                           aria-invalid="<#if messagesPerField.existsError('password')>true</#if>" />
                    <#if messagesPerField.existsError('password')>
                        <span class="modtech-error-message">${kcSanitize(messagesPerField.get('password'))?no_esc}</span>
                    </#if>
                    <!--
                        KEEP IN SYNC WITH security/password_policy.py
                        MIN_LENGTH = 15
                        MAX_LENGTH = 128
                        Identity rules: not contains firstName / lastName / email
                        (HIBP breach + zxcvbn enforced server-side at submit)
                    -->
                    <ul class="modtech-password-rules" id="modtech-password-rules" aria-live="polite">
                        <li class="modtech-rule-unmet" data-rule="length-min">At least 15 characters</li>
                        <li class="modtech-rule-unmet" data-rule="length-max">At most 128 characters</li>
                        <li class="modtech-rule-unmet" data-rule="no-first-name">Doesn't contain your first name</li>
                        <li class="modtech-rule-unmet" data-rule="no-last-name">Doesn't contain your last name</li>
                        <li class="modtech-rule-unmet" data-rule="no-email">Doesn't contain your email</li>
                    </ul>
                </div>

                <div class="modtech-form-group">
                    <label for="password-confirm" class="modtech-label">${msg("passwordConfirm")}</label>
                    <input type="password" id="password-confirm"
                           class="modtech-input <#if messagesPerField.existsError('password-confirm')>modtech-input-error</#if>"
                           name="password-confirm"
                           autocomplete="new-password"
                           placeholder="Confirm your password"
                           aria-invalid="<#if messagesPerField.existsError('password-confirm')>true</#if>" />
                    <#if messagesPerField.existsError('password-confirm')>
                        <span class="modtech-error-message">${kcSanitize(messagesPerField.get('password-confirm'))?no_esc}</span>
                    </#if>
                </div>
            </#if>

            <#if recaptchaRequired??>
                <div class="modtech-form-group">
                    <div class="g-recaptcha" data-size="compact" data-sitekey="${recaptchaSiteKey}"></div>
                </div>
            </#if>

            <div class="modtech-form-actions">
                <button class="modtech-btn modtech-btn-primary" type="submit">
                    ${msg("doRegister")}
                </button>
            </div>

            <div class="modtech-register-link" style="margin-top: 1.5rem; text-align: center;">
                <span>Already have an account?</span>
                <a href="${url.loginUrl}" class="modtech-link">${msg("doLogIn")}</a>
            </div>
        </form>

        <script>
            // KEEP IN SYNC WITH security/password_policy.py
            // MIN_LENGTH = 15, MAX_LENGTH = 128, identity-similarity ≥ 3 chars.
            (function () {
                var pwd = document.getElementById('password');
                if (!pwd) { return; }
                var firstName = document.getElementById('firstName');
                var lastName = document.getElementById('lastName');
                var email = document.getElementById('email');
                var submitBtn = document.querySelector('#kc-register-form button[type="submit"]');

                function setRule(key, met) {
                    var li = document.querySelector('[data-rule="' + key + '"]');
                    if (!li) { return; }
                    li.classList.toggle('modtech-rule-met', met);
                    li.classList.toggle('modtech-rule-unmet', !met);
                }

                function check() {
                    var v = pwd.value || '';
                    var fn = (firstName && firstName.value || '').trim().toLowerCase();
                    var ln = (lastName && lastName.value || '').trim().toLowerCase();
                    var em = (email && email.value || '').trim().toLowerCase();
                    var vLower = v.toLowerCase();
                    setRule('length-min', v.length >= 15);
                    setRule('length-max', v.length > 0 && v.length <= 128);
                    setRule('no-first-name', !fn || fn.length < 3 || !vLower.includes(fn));
                    setRule('no-last-name', !ln || ln.length < 3 || !vLower.includes(ln));
                    var emLocal = em.indexOf('@') >= 0 ? em.split('@')[0] : em;
                    setRule('no-email', !emLocal || emLocal.length < 3 || !vLower.includes(emLocal));

                    var unmet = document.querySelectorAll('.modtech-password-rules .modtech-rule-unmet').length;
                    if (submitBtn) {
                        submitBtn.disabled = (unmet > 0);
                    }
                }

                pwd.addEventListener('input', check);
                if (firstName) { firstName.addEventListener('input', check); }
                if (lastName) { lastName.addEventListener('input', check); }
                if (email) { email.addEventListener('input', check); }
                check();
            })();

            // Invitation pre-fill. The Dashboard's accept-invitation
            // flow appends `?email=<invited-address>` to the
            // registration URL when the user arrives via an invite
            // link. We populate the email input and lock it (readonly)
            // so the user can't accidentally sign up with a typo'd
            // address that would later fail the
            // /finalize-invited-user equality check on the API side.
            // Only fires when the field is currently empty (don't
            // clobber a Keycloak-driven repopulation after a form
            // validation error).
            (function () {
                try {
                    var params = new URLSearchParams(window.location.search);
                    var emailFromInvite = params.get('email');
                    if (!emailFromInvite) { return; }
                    var emailInput = document.getElementById('email');
                    if (!emailInput) { return; }
                    if (!emailInput.value) { emailInput.value = emailFromInvite; }
                    emailInput.readOnly = true;
                    emailInput.setAttribute('aria-readonly', 'true');
                    emailInput.style.backgroundColor = '#f1f5f9';
                    emailInput.style.cursor = 'not-allowed';
                    var hint = document.createElement('div');
                    hint.textContent = 'From your invitation — can’t be changed here.';
                    hint.style.fontSize = '0.75rem';
                    hint.style.color = '#64748b';
                    hint.style.marginTop = '0.25rem';
                    emailInput.insertAdjacentElement('afterend', hint);
                } catch (_) {
                    // Non-fatal; user types email manually.
                }
            })();
        </script>
    </#if>
</@layout.registrationLayout>
