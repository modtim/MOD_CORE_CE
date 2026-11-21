<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=true displayMessage=!messagesPerField.existsError('username'); section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("emailForgotTitle")}</h2>
        <p class="modtech-form-subtitle">Enter your email and we'll send you a reset link</p>
    <#elseif section = "form">
        <form id="kc-reset-password-form" class="modtech-form" action="${url.loginAction}" method="post">
            <div class="modtech-form-group">
                <label for="username" class="modtech-label">
                    <#if !realm.loginWithEmailAllowed>${msg("username")}<#elseif !realm.registrationEmailAsUsername>${msg("usernameOrEmail")}<#else>${msg("email")}</#if>
                </label>
                <input type="text" id="username" name="username"
                       class="modtech-input <#if messagesPerField.existsError('username')>modtech-input-error</#if>"
                       autofocus
                       value="${(auth.attemptedUsername!'')}"
                       placeholder="<#if !realm.loginWithEmailAllowed>${msg("username")}<#else>name@company.com</#if>"
                       aria-invalid="<#if messagesPerField.existsError('username')>true</#if>"/>
                <#if messagesPerField.existsError('username')>
                    <span class="modtech-error-message">
                        ${kcSanitize(messagesPerField.get('username'))?no_esc}
                    </span>
                </#if>
            </div>

            <div class="modtech-form-actions">
                <button class="modtech-btn modtech-btn-primary" type="submit">
                    ${msg("doSubmit")}
                </button>
            </div>

            <div class="modtech-register-link" style="margin-top: 1.5rem; text-align: center;">
                <a href="${url.loginUrl}" class="modtech-link">&larr; Back to sign in</a>
            </div>
        </form>
    <#elseif section = "info">
        <#if realm.duplicateEmailsAllowed>
            ${msg("emailInstructionUsername")}
        <#else>
            ${msg("emailInstruction")}
        </#if>
    </#if>
</@layout.registrationLayout>
