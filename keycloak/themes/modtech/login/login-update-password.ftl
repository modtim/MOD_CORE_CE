<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('password','password-new','password-confirm'); section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("updatePasswordTitle")}</h2>
        <p class="modtech-form-subtitle">Please enter a new password</p>
    <#elseif section = "form">
        <form id="kc-passwd-update-form" class="modtech-form" action="${url.loginAction}" method="post">
            <input type="text" id="username" name="username" value="${username}" autocomplete="username"
                   readonly="readonly" style="display:none;"/>
            <input type="password" id="password" name="password" autocomplete="current-password" style="display:none;"/>

            <div class="modtech-form-group">
                <label for="password-new" class="modtech-label">${msg("passwordNew")}</label>
                <input type="password" id="password-new" name="password-new"
                       class="modtech-input <#if messagesPerField.existsError('password','password-new')>modtech-input-error</#if>"
                       autofocus autocomplete="new-password"
                       placeholder="Enter new password"
                       aria-invalid="<#if messagesPerField.existsError('password','password-new')>true</#if>"/>
                <#if messagesPerField.existsError('password')>
                    <span class="modtech-error-message">
                        ${kcSanitize(messagesPerField.get('password'))?no_esc}
                    </span>
                </#if>
                <#if messagesPerField.existsError('password-new')>
                    <span class="modtech-error-message">
                        ${kcSanitize(messagesPerField.get('password-new'))?no_esc}
                    </span>
                </#if>
                <!--
                    KEEP IN SYNC WITH security/password_policy.py
                    MIN_LENGTH = 15
                    MAX_LENGTH = 128
                    Identity rules (not contains firstName/lastName/email) are
                    enforced server-side; this form has no identity inputs.
                    HIBP breach + zxcvbn also enforced server-side at submit.
                -->
                <ul class="modtech-password-rules" id="modtech-password-rules" aria-live="polite">
                    <li class="modtech-rule-unmet" data-rule="length-min">At least 15 characters</li>
                    <li class="modtech-rule-unmet" data-rule="length-max">At most 128 characters</li>
                </ul>
            </div>

            <div class="modtech-form-group">
                <label for="password-confirm" class="modtech-label">${msg("passwordConfirm")}</label>
                <input type="password" id="password-confirm" name="password-confirm"
                       class="modtech-input <#if messagesPerField.existsError('password-confirm')>modtech-input-error</#if>"
                       autocomplete="new-password"
                       placeholder="Confirm new password"
                       aria-invalid="<#if messagesPerField.existsError('password-confirm')>true</#if>"/>
                <#if messagesPerField.existsError('password-confirm')>
                    <span class="modtech-error-message">
                        ${kcSanitize(messagesPerField.get('password-confirm'))?no_esc}
                    </span>
                </#if>
            </div>

            <div class="modtech-form-actions">
                <#if isAppInitiatedAction??>
                    <button class="modtech-btn modtech-btn-primary" type="submit">${msg("doSubmit")}</button>
                    <button class="modtech-btn modtech-btn-secondary" type="submit" name="cancel-aia" value="true" style="margin-top: 0.5rem;">${msg("doCancel")}</button>
                <#else>
                    <button class="modtech-btn modtech-btn-primary" type="submit">${msg("doSubmit")}</button>
                </#if>
            </div>
        </form>

        <script>
            // KEEP IN SYNC WITH security/password_policy.py
            // MIN_LENGTH = 15, MAX_LENGTH = 128. Identity rules and zxcvbn /
            // HIBP gates run server-side at submit on this form.
            (function () {
                var pwd = document.getElementById('password-new');
                if (!pwd) { return; }
                // The primary submit (doSubmit). The cancel-aia button has
                // name="cancel-aia" and should remain enabled at all times.
                var submitBtn = document.querySelector('#kc-passwd-update-form button[type="submit"]:not([name="cancel-aia"])');

                function setRule(key, met) {
                    var li = document.querySelector('[data-rule="' + key + '"]');
                    if (!li) { return; }
                    li.classList.toggle('modtech-rule-met', met);
                    li.classList.toggle('modtech-rule-unmet', !met);
                }

                function check() {
                    var v = pwd.value || '';
                    setRule('length-min', v.length >= 15);
                    setRule('length-max', v.length > 0 && v.length <= 128);
                    var unmet = document.querySelectorAll('.modtech-password-rules .modtech-rule-unmet').length;
                    if (submitBtn) {
                        submitBtn.disabled = (unmet > 0);
                    }
                }

                pwd.addEventListener('input', check);
                check();
            })();
        </script>
    </#if>
</@layout.registrationLayout>
