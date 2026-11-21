<#import "template.ftl" as layout>
<#--
  add-self-service-sso §6.3 — link-existing-account consent screen.

  Rendered by Keycloak's built-in "Confirm Link Existing Account"
  execution inside our custom First Broker Login flow (provisioned
  by services/system_tasks.py:_configure_sso_first_broker_login_flow,
  alias = "first-broker-login-consent").

  Default Keycloak template name: `login-idp-link-confirm.ftl` —
  dropping a file with this name at the theme path overrides the
  bundled one. The spec §6.3 calls the file "link-existing-account.ftl"
  by intent; Keycloak's naming wins because that's what the flow
  renderer looks up.

  Trigger condition: a user has just authenticated via an IdP whose
  asserted email matches an existing MOD-side user. The default
  Keycloak behaviour is silent link — but we tightened
  `Confirm Link Existing Account` to REQUIRED in §2.8 so this consent
  screen always renders, satisfying the spec's "users must see they
  are being linked" requirement.

  Two explicit buttons:
    * "Link to my existing account" (submit name=submitAction
      value=linkAccount) — confirms the link, the next request continues
      down the FBL flow to email re-verification (§6.5) if needed.
    * "Use a separate account" (submit name=submitAction
      value=updateProfile) — proceeds to update-profile screen which
      lets the user supply a different email; if they keep the IdP's
      email, Keycloak will refuse to create a duplicate. v1 treats
      this as the "decline link" path; the realm-event listener (§6.4)
      records SSO_LINK_DECLINED so the Dashboard can surface the
      manual-merge banner (§6.6).
-->
<@layout.registrationLayout displayMessage=false; section>
    <#if section = "header">
        <h2 class="modtech-form-title">An account already exists for ${idpDisplayName!"your provider"}</h2>
        <p class="modtech-form-subtitle">You're being signed in via <strong>${idpDisplayName!"your identity provider"}</strong>.</p>
    <#elseif section = "form">
        <div class="modtech-form" id="kc-idp-link-confirm">
            <div class="modtech-alert modtech-alert-info" style="margin-bottom: 1.25rem;">
                <span class="modtech-alert-icon">&#8505;</span>
                <span class="modtech-alert-text">
                    An account on ${realm.displayName!''} already uses
                    <strong>${(user.email)!''}</strong>.
                    Choose whether to link <strong>${idpDisplayName!"your provider"}</strong>
                    to that account or sign in separately.
                </span>
            </div>

            <#-- Both buttons live in the same form so they POST back to
                 Keycloak's auth handler with the right submitAction
                 differentiator. Continue is the primary CTA. -->
            <form id="kc-idp-link-form" action="${url.loginAction}" method="post" class="modtech-form">
                <div class="modtech-form-actions" style="display:flex; flex-direction:column; gap:8px;">
                    <button tabindex="1" type="submit"
                            class="modtech-btn modtech-btn-primary"
                            name="submitAction" id="kc-idp-link-continue"
                            value="linkAccount">
                        Link to my existing account
                    </button>
                    <button tabindex="2" type="submit"
                            class="modtech-btn modtech-btn-secondary"
                            name="submitAction" id="kc-idp-link-cancel"
                            value="updateProfile">
                        Use a separate account
                    </button>
                </div>
            </form>

            <#-- Explainer block — the consent text users actually read.
                 Keep this short and concrete: what we'll do, what they
                 can undo later. -->
            <p style="color: var(--modtech-gray-600); font-size: 0.8125rem; margin-top: 1rem; text-align: center;">
                Linking lets you sign in with either method from now on.
                You can disconnect ${idpDisplayName!"the provider"} later
                from your profile page.
            </p>
        </div>
    </#if>
</@layout.registrationLayout>
