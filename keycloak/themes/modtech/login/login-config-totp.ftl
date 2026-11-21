<#import "template.ftl" as layout>
<@layout.registrationLayout displayRequiredFields=false displayMessage=!messagesPerField.existsError('totp','userLabel'); section>
    <#if section = "header">
        <h2 class="modtech-form-title">${msg("loginTotpTitle")}</h2>
        <p class="modtech-form-subtitle">${realm.displayName!''}</p>
    <#elseif section = "form">
        <div class="modtech-totp-steps">
            <ol id="kc-totp-settings">
                <li>
                    <p>${msg("loginTotpStep1")}</p>
                    <ul id="kc-totp-supported-apps">
                        <#list totp.supportedApplications as app>
                            <li>${msg(app)}</li>
                        </#list>
                    </ul>
                </li>
                <#if mode?? && mode = "manual">
                    <li>
                        <p>${msg("loginTotpManualStep2")}</p>
                        <p><span id="kc-totp-secret-key">${totp.totpSecretEncoded}</span></p>
                        <p><a href="${totp.qrUrl}" id="mode-barcode" class="modtech-link">${msg("loginTotpScanBarcode")}</a></p>
                    </li>
                    <li>
                        <p>${msg("loginTotpManualStep3")}</p>
                        <ul>
                            <li id="kc-totp-type">${msg("loginTotpType")}: ${msg("loginTotp." + totp.policy.type)}</li>
                            <li id="kc-totp-algorithm">${msg("loginTotpAlgorithm")}: ${totp.policy.getAlgorithmKey()}</li>
                            <li id="kc-totp-digits">${msg("loginTotpDigits")}: ${totp.policy.digits}</li>
                            <#if totp.policy.type = "totp">
                                <li id="kc-totp-period">${msg("loginTotpInterval")}: ${totp.policy.period}</li>
                            <#elseif totp.policy.type = "hotp">
                                <li id="kc-totp-counter">${msg("loginTotpCounter")}: ${totp.policy.initialCounter}</li>
                            </#if>
                        </ul>
                    </li>
                <#else>
                    <li>
                        <p>${msg("loginTotpStep2")}</p>
                        <img id="kc-totp-secret-qr-code" src="data:image/png;base64, ${totp.totpSecretQrCode}" alt="Figure: Barcode" />
                        <p><a href="${totp.manualUrl}" id="mode-manual" class="modtech-link">${msg("loginTotpUnableToScan")}</a></p>
                    </li>
                </#if>
                <li>
                    <p>${msg("loginTotpStep3")}</p>
                    <p>${msg("loginTotpStep3DeviceName")}</p>
                </li>
            </ol>
        </div>

        <form action="${url.loginAction}" id="kc-totp-settings-form" method="post" novalidate="novalidate" class="modtech-form">
            <div class="modtech-form-group">
                <label for="totp" class="modtech-label">
                    <span>${msg("authenticatorCode")}</span><#if totp.otpCredentials?size gte 1>&nbsp;<span class="modtech-required-star" aria-hidden="true">&#42;</span></#if>
                </label>
                <input type="text" required id="totp" name="totp" autocomplete="one-time-code" inputmode="numeric" autofocus
                       class="modtech-input <#if messagesPerField.existsError('totp')>modtech-input-error</#if>"
                       aria-invalid="<#if messagesPerField.existsError('totp')>true</#if>" />
                <#if messagesPerField.existsError('totp')>
                    <span id="input-error-otp-code" class="modtech-error-message" aria-live="polite">
                        ${kcSanitize(messagesPerField.get('totp'))?no_esc}
                    </span>
                </#if>
                <input type="hidden" id="totpSecret" name="totpSecret" value="${totp.totpSecret}" />
                <#if mode??><input type="hidden" id="mode" name="mode" value="${mode}"/></#if>
            </div>

            <div class="modtech-form-group">
                <label for="userLabel" class="modtech-label">
                    <span>${msg("loginTotpDeviceName")}</span><#if totp.otpCredentials?size gte 1>&nbsp;<span class="modtech-required-star" aria-hidden="true">&#42;</span></#if>
                </label>
                <input type="text" id="userLabel" name="userLabel" autocomplete="off"
                       class="modtech-input <#if messagesPerField.existsError('userLabel')>modtech-input-error</#if>"
                       aria-invalid="<#if messagesPerField.existsError('userLabel')>true</#if>" />
                <#if messagesPerField.existsError('userLabel')>
                    <span id="input-error-otp-label" class="modtech-error-message" aria-live="polite">
                        ${kcSanitize(messagesPerField.get('userLabel'))?no_esc}
                    </span>
                </#if>
            </div>

            <div class="modtech-form-options">
                <div class="modtech-checkbox-group">
                    <input type="checkbox" id="logout-sessions" name="logout-sessions" value="on" class="modtech-checkbox">
                    <span class="modtech-checkbox-label">${msg("logoutOtherSessions")}</span>
                </div>
            </div>

            <div class="modtech-form-actions">
                <#if isAppInitiatedAction??>
                    <input type="submit" id="saveTOTPBtn" value="${msg("doSubmit")}" />
                    <button type="submit" id="cancelTOTPBtn" name="cancel-aia" value="true" class="modtech-btn modtech-btn-secondary">${msg("doCancel")}</button>
                <#else>
                    <input tabindex="1" type="submit" id="saveTOTPBtn" class="modtech-btn modtech-btn-primary" value="${msg("doSubmit")}" />
                </#if>
            </div>
        </form>
    </#if>
</@layout.registrationLayout>
