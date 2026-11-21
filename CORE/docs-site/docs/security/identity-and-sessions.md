---
sidebar_position: 28
slug: /security/identity-and-sessions
title: Identity and Sessions
description: Keycloak session lifetimes (AC-11/AC-12), the IA-2 one-time-password settings CE registers but does not enforce and its exemptions, the AC-8 login-banner text CE stores, pushes and renders with its shipped login theme, the x509 smart-card seam, how to re-apply the identity policy to a live realm, and how to verify each item.
---

# Identity and Sessions

MOD *supports* identity and session controls in Keycloak: bounded session
lifetimes (NIST AC-11/AC-12), one-time-password enrollment for platform admins
(NIST IA-2), an AC-8 system-use notice, and a dormant x509 smart-card seam. In
Community Edition the session bounds are applied by the API and the notice text
is stored, pushed to the realm and rendered by the login theme CE ships; the
OTP enrollment is what an operator configures in stock Keycloak, because CE
ships no code that enforces it — see the sections below for exactly what CE does. MOD
enables operators to configure these controls; it does not
make a customer compliant with any framework, and enabling a control here does
not by itself satisfy the corresponding control — enrollment coverage, key
custody, and the operator's own policy remain the operator's responsibility.

## Session lifetimes (AC-11 / AC-12)

The realm's online session lifetimes are converged by the API at runtime —
`CORE/API/services/session_tier_enforcement.py` re-applies the live platform
settings to the realm on every pass. The CE realm import
(`keycloak/core-realm.json`, rendered by `keycloak/render-realm.sh` and
imported by the `keycloak` service on first start) carries 3600 s (60 min) / 43200 s (12 h)
`ssoSessionIdleTimeout` / `ssoSessionMaxLifespan` bounds of its own, which the
API's convergence replaces from the first settings pass onward:

| Setting | CE default | Control |
|---|---|---|
| `accessTokenLifespan` | 900 s (15 min) | AC-12 access-token ceiling |
| `ssoSessionIdleTimeout` | 3600 s (60 min) | AC-11 inactivity termination for the most permissive tier the realm serves (the human-user tier) |
| `ssoSessionMaxLifespan` | 43200 s (12 h) | AC-12 hard session ceiling for that same tier |

These mirror the platform settings `auth.access_token_lifespan_seconds`,
`auth.sso_session_idle_seconds` and `auth.sso_session_max_lifetime_seconds`
(registered in `CORE/API/services/platform_settings.py`). The realm baseline is
pinned at import time because the `keycloak` service starts before the API DB
has any settings to read — so the realm baseline and the registry must be kept
in sync, and the API's runtime convergence is what makes the live values
authoritative after the first settings pass.

Former 30-day client-level session overrides on the dashboard and
WorkflowBuilder clients are converged to the same values, so a client can no
longer extend a session past the realm ceiling. Offline (refresh-token)
lifetimes deliberately stay unbounded — bounding them requires the
exchange-token persistence fix first (token rotation would otherwise invalidate
every stored token on first use and break long-running workflows).

## Admin MFA enforcement (IA-2)

CE registers the platform setting `auth.mfa_required_for_admins` (Admin →
Platform settings, `auth` group, default ON) but ships **no enforcement** of it.
Nothing in the Community Edition queues a `CONFIGURE_TOTP` required action on
anyone: the queueing that setting describes lives in
`CORE/startup/keycloak-init.sh`, which CE does not ship, and the CE realm
import (`keycloak/core-realm.json`) carries no admin group and no queued
required actions for any seeded user. On a CE install the setting is stored
in the platform-settings registry and otherwise inert — changing it changes
nothing in Keycloak. This matches "How to re-apply" below: CE has no
init script, and the API's runtime convergence never touches required actions.

An operator who wants a one-time password for admins configures it in the stock
Keycloak admin console, as standard Keycloak behaviour (MOD ships no tooling
that does it for them):

- **realm-wide** — `Authentication → Required actions` on the realm, which
  enrolls every user;
- **per user** — the required-actions entry on that user's detail page, which
  enrolls one account.

2400MFA2 (CE): the CE realm now ships the realm-wide TOTP policy
(`otpPolicy*`: TOTP / HmacSHA256 / 6 digits / 30 s) and the `CONFIGURE_TOTP`
required action registered with `defaultAction: false` — opt-in only, nobody is
enrolled out of the box. Users enroll via the stock Keycloak account console;
an admin can reset an enrolled user's 2FA from Users → **Reset 2FA**
(`admin_mfa` routers ship in CE since 251b0f28e, and the Users page's identity
status column now reads live on CE). Automatic enforcement
(`auth.mfa_required_for_admins` acting on Keycloak) is not part of this edition.

Once `CONFIGURE_TOTP` is queued, Keycloak's stock Conditional-OTP step shows
the enrollment screen (QR/code) on the next browser login and asks for the
6-digit code on every login after that.

The managed version — MOD queueing the action for platform admins and skipping
the accounts listed under "Exemptions" — is not part of this edition; `auth.mfa_required_for_all`
is registered in CE (default OFF) and extends the same enforcement to every user in the realm,
but that enforcement is not shipped here.

## Exemptions

These rules belong to the managed enforcement, which is not part of this edition — on a
CE install nothing is queued, so nothing is skipped and every account keeps its
realm-default required actions. They matter only if an operator sets required
actions by hand in the Keycloak admin console, and then must observe them
manually:

- **Members of the `e2e_service_principals` group** — the exemption exists so a
  non-interactive harness principal is never locked out by a pending OTP action.
  CE ships no E2E harness and seeds no principals into that group, so on a CE
  install it is empty.
- **Machine users** — usernames matching `service-account-*`, `*@service.local`,
  `api_service`, and `platform-service`. These are client_credentials / direct-grant
  only and never pass the browser flow; a queued `CONFIGURE_TOTP` would lock them
  out for no gain. The CE realm seeds one such user
  (`service-account-fastapi-admin-service`); it ships with `requiredActions: []`
  and CE never writes to that field.

## MFA for all users

Neither MFA setting is enforced in CE. `auth.mfa_required_for_all` (TOTP for
every user in the realm) is registered in CE (default OFF); its
enforcement is not shipped; `auth.mfa_required_for_admins`
is registered in CE but, as above, nothing in CE acts on it. Neither is reported anywhere in CE: compliance-profile
reporting is not part of this edition and CE ships none of it, so on a CE install these two
settings exist only in the platform-settings registry.

## AC-8 login banner

The platform setting `auth.login_banner_text` (default empty = no banner) holds
the AC-8 system-use notice. On a settings write the API pushes the text to the
realm **localization** entry `en/modtechUseNotice` (`PUT` the text, `DELETE`
when the setting is empty), deferred to after the settings transaction commits
so the external call never pins a PgBouncer worker; a failed push never fails
the write — it is logged and recorded as a `SECURITY_CONFIG_ERROR` audit row.

Pushing is all CE does, and the theme it lands on is already there: CE ships
the `modtech` login theme (`keycloak/themes/modtech/login/template.ftl`, mounted
by the `keycloak` service; the realm's `loginTheme` is `modtech`), and that theme
**does** read `modtechUseNotice` and renders it as a `modtech-use-notice` block
(`template.ftl:68-73`). So with `auth.login_banner_text` set to a non-empty notice
the banner renders on a CE install; with the default empty setting nothing renders. (The `themes.login`
theme property some installs use for this is not a working mechanism here — the
admin API rejects that realm `PUT` with a 400, so the API never writes it.)

No custom theme is needed on CE for a visible notice: the shipped `modtech`
theme already renders the localization key, so setting `auth.login_banner_text`
to a non-empty notice is all an operator has to do. A custom `template.ftl`
(with `msg("modtechUseNotice")`) is only needed for a different look — that is
stock Keycloak theming, and MOD ships no tooling to install one. See Community Edition vs. Paid.

Failure mode: the push stores the value as plain `text/plain`, so nothing in CE
interprets markup in the setting; how a custom theme renders it — escaped or as
markup — is that theme's own behaviour, so keep the notice plain text.

## x509 / smart-card seam

`CORE/API/keycloak_auth.py` carries an `x509_piv_login_enabled` seam that is a
no-op while the platform setting `auth.x509_enabled` is `false` (the default).
Beyond that seam, no x509/PIV smart-card login path exists in the sources this
page covers — flipping the setting is a future enablement, not a working feature
today.

## Session + re-authentication controls (GY.C4b)

Beyond session *lifetimes*, MOD Core supports three per-account controls, all
tunable as platform settings (`Admin → Platform settings`, `auth` group) and all
**off by default** so an existing install behaves exactly as before:

### Concurrent-session cap (`auth.session_max_concurrent`)

Integer; `0` = unlimited (the default). When set > 0, MOD installs and enables
Keycloak's **user-session-limits** authenticator in the realm *browser*
flow (via `CORE/API/services/session_controls.py`, applied the moment the
setting changes — no Keycloak restart). `auth.session_limit_behaviour` chooses
what happens when a user exceeds the cap: `deny_new` (refuse the new login, the
default) or `terminate_oldest` (end the oldest session to make room).

- **No home-grown auth**: the limit itself lives in Keycloak; MOD only configures
  the authenticator through the admin REST API.
- **Exempt by construction**: service accounts, client-credentials clients and
  e2e principals never traverse the browser flow, so they are not capped.
- **Boot-time install is disabled-by-default**: on installs that pin the realm
  at boot, an init step idempotently installs the execution (requirement
  `DISABLED`) so a restart re-converges the shape; the API then flips it on/off
  to match the live settings. CE has no boot-time init step, so the API is the
  only writer. (Keycloak 26.3.x ships the authenticator; the install logs and
  moves on only if a build/custom realm does not have it — a defensive path.)

### Inactive-account auto-disable (`auth.inactive_disable_days`)

Integer; `0` = off (the default). CE registers the setting and **nothing in CE
acts on it**: the sweep that would soft-disable accounts idle past N days runs
under the job `inactive_account_disable`, which is not registered in CE: on a CE install no job runs, no account is disabled for inactivity, and the dry-run preview endpoint is not served (its path answers 404). The
companion setting `auth.inactive_disable_exempt_emails` (the break-glass /
monitored-service carve-out) is registered the same way and is equally inert
while nothing consumes it.

An operator who wants accounts idle past N days turned off does it in the stock
Keycloak admin console: list the realm's users, compare each one's last-seen
activity against your own cutoff, and set `enabled=false` on the ones you want
out (disable, never delete). MOD ships no tooling that does this for them on CE.
The managed version — the scheduled sweep, the exempt-emails carve-out and the
dry-run preview — is not part of this edition.

### Re-authentication for privileged actions (`auth.privileged_reauth_max_age_s`)

Integer (seconds); `0` = off (the default). Bounds how long a login remains
acceptable for **privileged** actions — platform-settings writes, user role
changes, and API-key creation (the CE routers that carry `require_fresh_auth`).
The check
reads the Keycloak token's `auth_time` claim (when the user actually logged
in), so a fresh token on a stale login is still rejected. A stale token returns
`401 {"error": "reauth_required", "reauth_max_age_s": N}`; the Dashboard
intercepts that error (in `CORE/Dashboard/js/api.js`) and redirects the browser
through Keycloak with `prompt=login&max_age=N` to force a fresh credential
check, after which the operator retries the action. Service / api-key channels
carry no human `auth_time` and are unaffected.

> Wording note: MOD **supports** these controls; it does not claim or certify
> compliance with any specific framework.

## Sign-in protection

Keycloak brute-force protection is enabled in the shipped realm
(`bruteForceProtected: true`, `failureFactor: 5`, `maxFailureWaitSeconds: 900`).
The API rate limiter is a convenience layer: if its Redis store is unavailable it lets requests through rather than blocking sign-in. Keycloak lockout counts failures per user, not per address.

## How to re-apply

CE has no separate Keycloak init container and no init script to re-run. The
realm is applied **once**, by the `keycloak` service itself: its
`entrypoint` (`keycloak/render-realm.sh`) substitutes the realm's client-secret
placeholders from the install's `.env`, then execs the stock Keycloak entrypoint
with `start --import-realm`, importing `keycloak/core-realm.json`. Everything
that can change after that first import is converged **at runtime by the API**,
not by a script: the session bounds come from platform settings the API
re-applies to the realm on its own pass, and the login banner is pushed to the
realm's localization entry by the same settings write. The MFA settings are the
exception — CE has no code that acts on them, so changing one re-applies nothing;
required actions are set in the Keycloak admin console instead. So "re-apply" on
CE means changing the setting for the session bounds and the banner push, and
using the admin console for OTP enrollment.

The one thing that is genuinely import-only is the realm **structure** itself
(the realm, its clients and its seeded users). To re-import it, wipe the realm
and bring the services back up — this **DELETES ALL KEYCLOAK USERS**:

```bash
docker compose rm -sfv keycloak keycloak-db
docker volume rm mod-core-ce_keycloak_db_data
docker compose up
```

The compose file pins the project name to `mod-core-ce`, so that volume name is
the same from any clone directory; confirm with
`docker volume ls | grep keycloak_db_data` before removing it.

Do **not** hand-run the Keycloak image against the live realm with `docker run`
and bind-mounted env files: CE ships no env-file split (the compose file carries
its own `environment:` block and requires each credential from
`scripts/first-run-secrets.sh`), so there is nothing to source and no init
entrypoint to exec.

## How to verify each item

1. **Session bounds** — realm API. `$ADMIN_TOKEN` is an admin access token for the
   master realm. Get it with the stock `admin-cli` password grant (the admin user name
   and password are the compose variables `KEYCLOAK_ADMIN_USER` and
   `KEYCLOAK_ADMIN_PASSWORD`; never type the password into the command):
   ```bash
   ADMIN_TOKEN="$(curl -s -X POST \
     -d client_id=admin-cli -d grant_type=password \
     -d "username=${KEYCLOAK_ADMIN_USER:-admin}" \
     -d "password=${KEYCLOAK_ADMIN_PASSWORD:?run scripts/first-run-secrets.sh}" \
     "http://${CE_BIND_ADDR:-127.0.0.1}:${KEYCLOAK_HOST_PORT:-8080}/realms/master/protocol/openid-connect/token" \
     | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
   ```
   With `install.sh --lan` use the address you set in `CE_BIND_ADDR` instead of
   `127.0.0.1`. The hostname `keycloak` resolves only inside the Docker compose
   network, so run the call from the host against the published port
   (`KEYCLOAK_HOST_PORT`, default 8080; bound to `CE_BIND_ADDR`, default
   `127.0.0.1`):
   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
     "http://${CE_BIND_ADDR:-127.0.0.1}:${KEYCLOAK_HOST_PORT:-8080}/admin/realms/modtex" \
     | jq '{accessTokenLifespan, ssoSessionIdleTimeout, ssoSessionMaxLifespan}'
   ```
   → `900 / 3600 / 43200` (the CE defaults above). Browser: sign in, leave the
   tab idle past the configured idle ceiling, then click an API action → bounced
   page (idle expiry); stay active >12 h → hard-expiry forces re-login. Access
   tokens in devtools expire at 15 min.
2. **MFA** — CE: the setting is present and nothing enforces it. Admin →
   Platform settings shows `auth.mfa_required_for_admins` = `true`, yet
   `GET /admin/realms/modtex/users/<admin-id>` → `requiredActions` stays `[]`
   for every user, signed-in or not, and no OTP prompt ever appears. To verify
   the operator path instead: set the required action on one user in the Keycloak
   admin console, sign in → the stock Conditional-OTP enrollment screen appears
   before the dashboard loads and later logins ask for the code; clear it and
   the prompt is gone.
3. **Banner** — set `auth.login_banner_text` to a non-empty notice, then check
   the push landed: `GET /admin/realms/modtex/localization/en/modtechUseNotice`
   → `200` with the text (empty setting → `404`, the entry is deleted). With the
   shipped `modtech` theme the notice renders on the login page as a
   `modtech-use-notice` block (`template.ftl:68-73`); with the default empty
   setting nothing renders. A custom `template.ftl` is only needed for a
   different look — there `msg("modtechUseNotice")` falls back to the key name
   when the setting is empty, which that theme must treat as "no banner".
4. **x509 seam** — Admin → Platform settings shows `auth.x509_enabled` = `false`
   and logins are unchanged.
5. **Machine users** — CE ships no E2E harness and applies no MFA, so there is
   nothing to re-check after an apply. Verify instead the machine user the CE
   realm actually seeds, `service-account-fastapi-admin-service`:
   `GET /admin/realms/modtex/users` → its `requiredActions` is `[]` and the API
   keeps obtaining service tokens non-interactively — true on CE out of the box,
   and it must stay true if an operator sets required actions by hand. (The
   other machine-user usernames the managed exemptions list — `api@service.local`
   and friends — are not seeded on CE.)
