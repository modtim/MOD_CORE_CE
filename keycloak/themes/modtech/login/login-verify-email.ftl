<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=true; section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("emailVerifyTitle")}</h2>
        <p class="modtech-form-subtitle">Check your inbox for a verification link</p>
    <#elseif section = "form">
        <div class="modtech-verify-email">
            <div class="modtech-alert modtech-alert-info" style="margin-bottom: 1.5rem;">
                <span class="modtech-alert-icon">&#8505;</span>
                <span class="modtech-alert-text">${msg("emailVerifyInstruction1",user.email)}</span>
            </div>

            <p style="color: var(--modtech-gray-600); font-size: 0.875rem; text-align: center; margin-bottom: 1.5rem;">
                ${msg("emailVerifyInstruction2")}
                <br/>
                <a href="${url.loginAction}" class="modtech-link">${msg("doClickHere")}</a> ${msg("emailVerifyInstruction3")}
            </p>
        </div>
    <#elseif section = "info">
        <p style="color: var(--modtech-gray-500); font-size: 0.8125rem;">
            Didn't receive the email? Check your spam folder or click the link above to resend.
        </p>
    </#if>
</@layout.registrationLayout>
