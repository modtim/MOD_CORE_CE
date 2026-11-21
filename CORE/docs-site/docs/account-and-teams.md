---
sidebar_position: 11
title: Account & Teams
description: Manage your profile, invite teammates, and export or delete your account.
dashboard_route: '#settings'
---

# Account & Teams

## Your profile

**Dashboard → profile menu → My Account** is where you manage your own login — name, and account-level actions:

- **Export my data** — download a JSON export of your profile, file metadata, and workflow history.
- **Delete my account** — anonymizes your personal data immediately; files are retained briefly (cancellable) before permanent deletion.

## Invite your team

Community Edition has no seat limit: invite as many teammates as you need. To add them:

1. Dashboard → **Gearworks - Settings → Gearworks - Integrations → Email (SMTP)**. Optionally configure an outbound email provider
   (Postmark or custom SMTP; without one, you share the accept link yourself).
2. Dashboard → **Users / Invitations** → **Send Invitation** to a teammate by email.
3. With email configured they get an invitation email. Without it, share the accept link you are shown.

> **Note:** SMTP is optional in Community Edition. With no email provider
> configured, the invitation is still created, but no email is sent - the
> accept link is returned to the admin who created it, so share that link
> with the teammate instead.

The accept link expires 7 days after the invitation is created.

**What the invitee does:** they open the accept link, see their email address (read-only), enter a first name, last name and a password of at least 12 characters (typed twice), and submit the form to create their account.

Each accepted teammate is one user in your deployment.

## Roles & access

- Team members' access to **files and folders** is controlled by [sharing & permissions](/features#file-sharing-with-expiration) (viewer / editor / owner, with optional expiry).
- A **capability** is a single permission (view, download, execute, share, delete) — permission levels are bundles of these. See the [Glossary](/glossary#capability).
- When you assign a role, the choice lists only the built-in system roles; custom roles are not available in Community Edition.

## Sign-in

CE users sign in with local accounts in the bundled Keycloak.

## Related

- [File sharing](/features#file-sharing-with-expiration) — granting access to specific people.
