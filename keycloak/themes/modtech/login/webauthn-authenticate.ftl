<#import "template.ftl" as layout>
<@layout.registrationLayout; section>
    <#if section = "header">
        <h2 class="modtech-form-title">Sign in with your security key</h2>
        <p class="modtech-form-subtitle">${realm.displayName!''}</p>
    <#elseif section = "form">
        <div class="modtech-form-body">
            <#if !webauthn.error>
                <p class="modtech-form-intro">
                    Insert your security key and approve the sign-in — or
                    tap the key when it is near the device. Your security
                    key only works on the real MOD sign-in page, so nobody
                    can trick it into approving a copy.
                </p>
                <div class="modtech-form-group">
                    <button type="button" id="webauthn-button" data-url="${url.webauthnAuthenticateUrl}"
                            class="modtech-btn modtech-btn-primary">${msg("authenticateWebAuthnButton")}</button>
                </div>
                <#if messagesPerField.existsError('userLabel')>
                    <span id="input-error-webauthn" class="modtech-error-message" aria-live="polite">
                        ${kcSanitize(messagesPerField.get('userLabel'))?no_esc}
                    </span>
                </#if>
            <#else>
                <p class="modtech-form-intro">We could not read the security key. Try again — make sure the key is
                inserted and unlocked, or that your phone is nearby with notifications on.</p>
                <div class="modtech-form-actions">
                    <button type="button" id="webauthn-button" data-url="${url.webauthnAuthenticateUrl}"
                            class="modtech-btn modtech-btn-primary">${msg("authenticateWebAuthnButton")}</button>
                </div>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
