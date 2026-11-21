---
sidebar_position: 26.75
slug: /security/mfa-reset-and-security-contact
title: MFA reset
description: How a platform admin resets a user's MFA through the admin API (who may call it, what the reset does, what is audited and notified), and the last-admin recovery review routes that Community Edition ships.
---

# MFA reset

This page covers two things: the admin route that resets a user's MFA, and
the last-admin recovery review routes for the case where the only admin of a
tenant loses MFA.

## Admin MFA reset

`POST /v1/admin/users/{user_id}/mfa/reset` with the optional body
`{"reason": "..."}`. The reason is stored with the audit row and may be up to
500 characters (`CORE/API/routers/admin_mfa.py:45-46`, route at `:70`).

Who may call it (`CORE/API/services/users_router_service.py`,
`resolve_target_user_for_admin`):

- A **platform admin** can reset the MFA of a user in any tenant.
- Any other user needs the `USER` `deactivate` capability and can reset only
  users in their own tenant. A target outside the caller's tenant returns 404.
- A user id that does not exist returns 404.

What the reset does (`CORE/API/services/mfa_service.py`, `reset_user_mfa`):

- Removes the target's one-time-password and WebAuthn credentials in Keycloak.
- Queues re-enrolment (`CONFIGURE_TOTP`), so the user sets up MFA again on the
  next login.
- Does not sign the user out of existing sessions.

Rules the route enforces:

- A service account is refused with 409 before Keycloak is touched.
- If Keycloak fails, the route returns 502 ("could not reset 2FA in the
  identity provider"). The audit row is written only after the Keycloak call
  succeeds, so a failed reset does not appear as a success.

After a reset through the admin API route above, Community Edition emails the
affected user and the tenant admins only when the tenant has SMTP configured
(Settings > Email). Without SMTP no message is sent and the reset still
completes. The reset writes an append-only `AUTH_MFA_RESET` audit row that
records the target, the actor and the reason. It also creates an in-app
notification for each active platform admin (`mfa_service.py:275-300` and
`:367-370`).

A platform admin can reset a user's MFA through the admin API route above.

## Last-admin recovery review

When the only active admin of a tenant is locked out of MFA, a platform admin
can review and approve a recovery request through the routes below
(`CORE/API/routers/admin_mfa_recovery.py`). All of them require a platform
admin (`require_platform_admin`, `:36-39`).

Community Edition does not ship the public request form
(`POST /v1/public/mfa-recovery-requests`). The API therefore does not create
recovery requests in this edition.

The review steps are:

1. **List requests** — `GET /v1/admin/mfa-recovery-requests` (`:62`).
2. **Record a callback** — `POST /v1/admin/mfa-recovery-requests/{request_id}/callback-verified`
   (`:71`). A platform admin verifies the request out of band, for example by
   calling back over a channel they already have, and records it with this
   call. The call stores the flag, the verifying admin's identity, the time
   and a note. It does not place a call.
3. **Approve** — `POST /v1/admin/mfa-recovery-requests/{request_id}/approve`
   (`:86`). The approver must be a *different* platform admin from the one who
   recorded the callback; otherwise the route returns 409. Approval resets the
   user's MFA credentials in Keycloak, revokes the user's sessions
   (`services/mfa_recovery.py`, `approve_request`) and writes an
   `AUTH_MFA_RESET` audit row.
4. **Reject** — `POST /v1/admin/mfa-recovery-requests/{request_id}/reject`
   (`:113`) closes a request without a reset.

Requests expire after a fixed window. An expired request can be read but
cannot be approved (409).

## Security contact

To report a security vulnerability in Community Edition, email
**`admin@modtechlabs.com`**, the security contact address. This is an address
you write to. Community Edition sends no email to this address.

See `SECURITY.md` in the repository root for how to report a vulnerability,
what is in scope, and what to include in a report.
