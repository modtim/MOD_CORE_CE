<#import "template.ftl" as layout>
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <#if messageHeader??>
            <h2 class="modtech-form-title">${messageHeader}</h2>
        <#else>
            <h2 class="modtech-form-title">${message.summary}</h2>
        </#if>
    <#elseif section = "form">
        <div class="modtech-info-page">
            <#-- Big, obvious success panel for the common case (email verified
                 etc). The subtle modtech-alert was easy to miss — users were
                 reporting "it just takes me to login, no confirmation". -->
            <#if message?has_content && message.type = 'success'>
                <div style="margin: 0 auto 1.5rem; padding: 1.25rem; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 12px; text-align: center;">
                    <div style="font-size: 2.5rem; line-height: 1; margin-bottom: 0.5rem;">&#9989;</div>
                    <div style="font-size: 1rem; font-weight: 600; color: #065f46;">
                        ${kcSanitize(message.summary)?no_esc}
                    </div>
                    <div style="font-size: 0.875rem; color: #047857; margin-top: 0.25rem;">
                        You can now sign in with your account.
                    </div>
                </div>
            <#elseif message?has_content>
                <div class="modtech-alert modtech-alert-${message.type}" style="margin-bottom: 1.5rem;">
                    <#if message.type = 'warning'><span class="modtech-alert-icon">&#9888;</span></#if>
                    <#if message.type = 'error'><span class="modtech-alert-icon">&#10007;</span></#if>
                    <#if message.type = 'info'><span class="modtech-alert-icon">&#8505;</span></#if>
                    <span class="modtech-alert-text">${kcSanitize(message.summary)?no_esc}</span>
                </div>
            </#if>

            <#if skipLink??>
            <#else>
                <#-- Pick whichever redirect target Keycloak provided.
                     Falls back to the realm's login URL so the
                     success page never leaves a user stranded with no
                     auto-redirect (e.g. an account-page-driven action
                     where Keycloak doesn't pass a pageRedirectUri). -->
                <#assign mtRedirectTarget = "" />
                <#if pageRedirectUri?has_content>
                    <#assign mtRedirectTarget = pageRedirectUri />
                <#elseif actionUri?has_content>
                    <#assign mtRedirectTarget = actionUri />
                <#elseif client?? && client.baseUrl?has_content>
                    <#assign mtRedirectTarget = client.baseUrl />
                <#elseif url?? && url.loginUrl?has_content>
                    <#assign mtRedirectTarget = url.loginUrl />
                </#if>

                <#-- Pass the success signal through to the dashboard so it can
                     surface a one-shot themed popup even if this info page
                     flashes by too fast. -->
                <#if message?has_content && message.type = 'success' && mtRedirectTarget?has_content>
                    <#if mtRedirectTarget?contains('?')>
                        <#assign mtRedirectTarget = mtRedirectTarget + '&kc_action_status=success' />
                    <#else>
                        <#assign mtRedirectTarget = mtRedirectTarget + '?kc_action_status=success' />
                    </#if>
                </#if>

                <#if mtRedirectTarget?has_content>
                    <div class="modtech-form-actions">
                        <a id="mt-redirect-btn" href="${mtRedirectTarget}" class="modtech-btn modtech-btn-primary">
                            Continue to login
                        </a>
                    </div>
                    <p id="mt-redirect-note" style="margin-top: 0.75rem; font-size: 0.875rem; color: #6b7280; text-align: center;">
                        Redirecting automatically in <span id="mt-redirect-countdown">5</span>s…
                    </p>
                    <script>
                      (function () {
                        var target = "${mtRedirectTarget?js_string}";
                        var remaining = 5;
                        var el = document.getElementById('mt-redirect-countdown');
                        var tick = setInterval(function () {
                            remaining--;
                            if (el) el.textContent = remaining;
                            if (remaining <= 0) {
                                clearInterval(tick);
                                window.location.href = target;
                            }
                        }, 1000);
                      })();
                    </script>
                </#if>
            </#if>
        </div>
    </#if>
</@layout.registrationLayout>
