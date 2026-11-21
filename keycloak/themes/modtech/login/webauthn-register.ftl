<#import "template.ftl" as layout>
<@layout.registrationLayout displayRequiredFields=false displayMessage=!messagesPerField.existsError('userLabel'); section>
    <#if section = "header">
        <h2 class="modtech-form-title">Add a security key</h2>
        <p class="modtech-form-subtitle">${realm.displayName!''}</p>
    <#elseif section = "form">
        <#if (isAppInitiatedAction?!) && isAppInitiatedAction && !webauthn.cancel>
        <div class="modtech-form-body">
            <p class="modtech-form-intro">
                A security key — a USB or Bluetooth key like a YubiKey, or
                your phone's built-in key — signs in without any code.
                It cannot be phished: a key only works when you are on the
                real MOD sign-in page.
            </p>
            <form action="${url.webauthnRegisterUrl}" id="webauthn-register-form" method="post" novalidate="novalidate" class="modtech-form">
                <div class="modtech-form-group">
                    <label for="userLabel" class="modtech-label">
                        <span>Name this key (optional — e.g. "Office YubiKey")</span>
                    </label>
                    <input type="text" id="userLabel" name="userLabel" autocomplete="off"
                           class="modtech-input <#if messagesPerField.existsError('userLabel')>modtech-input-error</#if>"
                           aria-invalid="<#if messagesPerField.existsError('userLabel')>true</#if>" />
                    <#if messagesPerField.existsError('userLabel')>
                        <span id="input-error-webauthn-label" class="modtech-error-message" aria-live="polite">
                            ${kcSanitize(messagesPerField.get('userLabel'))?no_esc}
                        </span>
                    </#if>
                </div>

                <input type="hidden" id="webauthn-userVerification" name="userVerification" value="${webauthn.userVerification}" />
                <input type="hidden" id="webauthn-requireAttestation" name="requireAttestation" value="${webauthn.requireAttestation}" />
                <#if webauthn.authenticatorAttachment??><input type="hidden" id="webauthn-authenticatorAttachment" name="authenticatorAttachment" value="${webauthn.authenticatorAttachment}" /></#if>
                <input type="hidden" id="webauthn-timeout" name="timeout" value="${webauthn.timeout}" />

                <div class="modtech-form-actions">
                    <button type="button" id="webauthn-button" data-url="${url.webauthnRegisterUrl}"
                            class="modtech-btn modtech-btn-primary">${msg("webAuthnButtonLabel")}</button>
                    <#if isAppInitiatedAction??>
                        <button type="submit" id="cancel-webauthn-register" name="cancel-aia" value="true"
                                class="modtech-btn modtech-btn-secondary">${msg("doCancel")}</button>
                    </#if>
                </div>
            </form>
        </div>
        </#if>
    </#if>
</@layout.registrationLayout>
