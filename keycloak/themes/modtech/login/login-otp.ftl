<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=!messagesPerField.existsError('totp'); section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("doLogIn")}</h2>
        <p class="modtech-form-subtitle">${realm.displayName!''}</p>
    <#elseif section = "form">
        <form id="kc-otp-login-form" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post" class="modtech-form">
            <input id="selectedCredentialId" type="hidden" name="selectedCredentialId" value="${otpLogin.selectedCredentialId!''}">

            <#if otpLogin.userOtpCredentials?size gt 1>
                <div class="modtech-form-group">
                    <div id="kc-otp-credential-list" class="modtech-otp-credential-list">
                        <#list otpLogin.userOtpCredentials as otpCredential>
                            <div id="kc-otp-credential-${otpCredential?index}" class="modtech-otp-credential"
                                    onclick="toggleOTP(${otpCredential?index}, '${otpCredential.id}')">
                                <span class="modtech-otp-credential-title">${otpCredential.userLabel}</span>
                            </div>
                        </#list>
                    </div>
                </div>
            </#if>

            <div class="modtech-form-group">
                <label for="otp" class="modtech-label">
                    <span>${msg("loginOtpOneTime")}</span>
                </label>
                <input type="text" id="otp" name="otp" autocomplete="one-time-code" inputmode="numeric" autofocus
                       class="modtech-input <#if messagesPerField.existsError('totp')>modtech-input-error</#if>"
                       aria-invalid="<#if messagesPerField.existsError('totp')>true</#if>" />
                <#if messagesPerField.existsError('totp')>
                    <span id="input-error-otp-code" class="modtech-error-message" aria-live="polite">
                        ${kcSanitize(messagesPerField.get('totp'))?no_esc}
                    </span>
                </#if>
            </div>

            <div class="modtech-form-actions">
                <button tabindex="1" class="modtech-btn modtech-btn-primary" name="login" id="kc-login" type="submit">
                    ${msg("doLogIn")}
                </button>
            </div>
        </form>
        <script>
            function toggleOTP(index, value) {
                document.getElementById("selectedCredentialId").value = value;
                Array.from(document.getElementsByClassName("modtech-otp-credential-selected")).map(i => i.classList.remove("modtech-otp-credential-selected"));
                document.getElementById("kc-otp-credential-" + index).classList.add("modtech-otp-credential-selected");
            }
        </script>
    </#if>
</@layout.registrationLayout>
