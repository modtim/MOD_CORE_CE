<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("logoutConfirmTitle")}</h2>
        <p class="modtech-form-subtitle">Are you sure you want to sign out?</p>
    <#elseif section = "form">
        <div class="modtech-logout-confirm">
            <form class="modtech-form" action="${url.logoutConfirmAction}" method="POST">
                <input type="hidden" name="session_code" value="${logoutConfirm.code}">
                <div class="modtech-form-actions">
                    <button class="modtech-btn modtech-btn-primary" name="confirmLogout" id="kc-logout" type="submit">
                        ${msg("doLogout")}
                    </button>
                </div>
            </form>

            <#if logoutConfirm.skipLink?? && !logoutConfirm.skipLink>
                <div class="modtech-register-link" style="margin-top: 1.5rem; text-align: center;">
                    <#if client?? && client.baseUrl?has_content>
                        <a href="${client.baseUrl}" class="modtech-link">Cancel and return to application</a>
                    </#if>
                </div>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
