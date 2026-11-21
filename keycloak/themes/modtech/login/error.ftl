<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <h2 class="modtech-form-title">
            <#if message?has_content>
                <#if message.type = 'error'>Oops! Something went wrong</#if>
                <#if message.type = 'warning'>Warning</#if>
                <#if message.type = 'info'>Information</#if>
                <#if message.type = 'success'>Success</#if>
            <#else>
                Error
            </#if>
        </h2>
    <#elseif section = "form">
        <div class="modtech-error-page">
            <#if message?has_content>
                <div class="modtech-alert modtech-alert-${message.type}" style="margin-bottom: 1.5rem;">
                    <#if message.type = 'success'><span class="modtech-alert-icon">&#10003;</span></#if>
                    <#if message.type = 'warning'><span class="modtech-alert-icon">&#9888;</span></#if>
                    <#if message.type = 'error'><span class="modtech-alert-icon">&#10007;</span></#if>
                    <#if message.type = 'info'><span class="modtech-alert-icon">&#8505;</span></#if>
                    <span class="modtech-alert-text">${kcSanitize(message.summary)?no_esc}</span>
                </div>
            </#if>

            <#if skipLink??>
            <#else>
                <#if client?? && client.baseUrl?has_content>
                    <div class="modtech-form-actions">
                        <a href="${client.baseUrl}" class="modtech-btn modtech-btn-primary">
                            Return to Application
                        </a>
                    </div>
                </#if>
            </#if>

            <div class="modtech-register-link" style="margin-top: 1.5rem; text-align: center;">
                <a href="${url.loginUrl}" class="modtech-link">&larr; Back to sign in</a>
            </div>
        </div>
    </#if>
</@layout.registrationLayout>
