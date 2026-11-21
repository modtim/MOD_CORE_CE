<#macro registrationLayout bodyClass="" displayInfo=false displayMessage=true displayRequiredFields=false>
<!DOCTYPE html>
<html class="${properties.kcHtmlClass!}">
<head>
    <meta charset="utf-8">
    <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
    <meta name="robots" content="noindex, nofollow">
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <#if properties.meta?has_content>
        <#list properties.meta?split(' ') as meta>
            <meta name="${meta?split('==')[0]}" content="${meta?split('==')[1]}"/>
        </#list>
    </#if>

    <title>${msg("loginTitle",(realm.displayName!''))}</title>
    <link rel="icon" type="image/png" href="${url.resourcesPath}/img/favicon.png" />

    <#if properties.stylesCommon?has_content>
        <#list properties.stylesCommon?split(' ') as style>
            <link href="${url.resourcesCommonPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>

    <#if properties.styles?has_content>
        <#list properties.styles?split(' ') as style>
            <link href="${url.resourcesPath}/${style}" rel="stylesheet" />
        </#list>
    </#if>

    <#if properties.scripts?has_content>
        <#list properties.scripts?split(' ') as script>
            <script src="${url.resourcesPath}/${script}" type="text/javascript"></script>
        </#list>
    </#if>

    <#if scripts??>
        <#list scripts as script>
            <script src="${script}" type="text/javascript"></script>
        </#list>
    </#if>

    <!-- Manrope font is self-hosted via @font-face in resources/css/styles.css -->
</head>

<body class="modtech-login-body">
    <div class="modtech-login-container">
        <!-- Logo/Brand Header -->
        <div class="modtech-login-header">
            <a href="https://modtechlabs.com" class="modtech-logo" aria-label="${realm.displayName!'Mod Tech Labs'}">
                <img src="${url.resourcesPath}/img/wordmark.png" alt="${realm.displayName!'Mod Tech Labs'}" />
            </a>
        </div>

        <!-- Main Login Card -->
        <div class="modtech-login-card">
            <#-- GSEC R-043 (AC-8 system-use notification) — operator-set
                 banner from the platform setting auth.login_banner_text:
                 keycloak-init.sh pushes it (plain text) into the realm
                 localization text en/modtechUseNotice, which msg() resolves
                 here. The setting is plain text: FreeMarker auto-escaping
                 escapes it on output (the legacy escaping operator is not
                 allowed with auto-escaping on), and newlines are rendered as
                 line breaks by CSS white-space: pre-line — no raw HTML can be
                 injected.
                 When the entry is absent msg() falls back to the key itself,
                 which means no banner rendered. -->
            <#assign modtech_use_notice = msg("modtechUseNotice")>
            <#if modtech_use_notice?has_content && modtech_use_notice != "modtechUseNotice">
                <div class="modtech-use-notice" role="note">
                    ${modtech_use_notice}
                </div>
            </#if>

            <#if displayMessage && message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
                <div class="modtech-alert modtech-alert-${message.type}">
                    <#if message.type = 'success'><span class="modtech-alert-icon">&#10003;</span></#if>
                    <#if message.type = 'warning'><span class="modtech-alert-icon">&#9888;</span></#if>
                    <#if message.type = 'error'><span class="modtech-alert-icon">&#10007;</span></#if>
                    <#if message.type = 'info'><span class="modtech-alert-icon">&#8505;</span></#if>
                    <span class="modtech-alert-text">${kcSanitize(message.summary)?no_esc}</span>
                </div>
            </#if>

            <#nested "header">

            <div class="modtech-form-container">
                <#nested "form">
            </div>

            <#if auth?has_content && auth.showTryAnotherWayLink()>
                <div class="modtech-try-another">
                    <form id="kc-select-try-another-way-form" action="${url.loginAction}" method="post">
                        <input type="hidden" name="tryAnotherWay" value="on"/>
                        <a href="#" id="try-another-way" onclick="document.forms['kc-select-try-another-way-form'].submit();return false;">
                            ${msg("doTryAnotherWay")}
                        </a>
                    </form>
                </div>
            </#if>

            <#nested "socialProviders">

            <#if displayInfo>
                <div class="modtech-info-section">
                    <#nested "info">
                </div>
            </#if>
        </div>

        <!-- Footer -->
        <div class="modtech-login-footer">
            <#if realm.internationalizationEnabled && locale.supported?size gt 1>
                <div class="modtech-locale-selector">
                    <select id="locale-select" onchange="window.location.href=this.value">
                        <#list locale.supported as l>
                            <option value="${l.url}" <#if l.label == locale.current>selected</#if>>${l.label}</option>
                        </#list>
                    </select>
                </div>
            </#if>
        </div>
    </div>

    <#-- Idle-expiry banner. Keycloak's action token (the `execution`
         param in the URL) expires after `accessCodeLifespanLogin`
         seconds, default 300s. A user who walks away from this page
         and tries to submit later gets an opaque "expired_code" error.
         Surface a friendlier non-blocking banner after 4 minutes of
         zero interaction, with a one-click "Refresh sign-in" CTA that
         hits Keycloak's restart-flow endpoint (which preserves the
         original redirect_uri and reissues a fresh execution token). -->
    <#if url?? && url.loginRestartFlowUrl?? && url.loginRestartFlowUrl?has_content>
        <div id="modtech-idle-banner" class="modtech-idle-banner" hidden role="alert" aria-live="polite">
            <div class="modtech-idle-banner-inner">
                <span class="modtech-idle-banner-icon" aria-hidden="true">&#9888;</span>
                <span class="modtech-idle-banner-text">
                    For security, this sign-in page expires after a few minutes
                    of inactivity. Click below to refresh and try again.
                </span>
                <a href="${url.loginRestartFlowUrl}" class="modtech-btn modtech-btn-primary modtech-idle-banner-btn">
                    Refresh sign-in
                </a>
            </div>
        </div>
        <script>
          (function () {
              var IDLE_MS = 4 * 60 * 1000;
              var banner = document.getElementById('modtech-idle-banner');
              if (!banner) return;
              var timer;
              function arm() {
                  clearTimeout(timer);
                  timer = setTimeout(function () { banner.hidden = false; }, IDLE_MS);
              }
              ['keydown', 'click', 'touchstart', 'focusin'].forEach(function (evt) {
                  document.addEventListener(evt, arm, { passive: true });
              });
              arm();
          })();
        </script>
    </#if>
</body>
</html>
</#macro>
