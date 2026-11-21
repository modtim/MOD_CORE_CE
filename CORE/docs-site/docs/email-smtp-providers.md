---
sidebar_position: 5.8
title: Email / SMTP providers
description: Connect platform email to Gmail, Google Workspace, iCloud, Fastmail, Proton, Yahoo, Zoho, mailbox.org, Migadu, GMX, your own mail server, or a local test relay.
---

# Email / SMTP providers

Platform email (invitations, verification, password resets, alerts) uses one SMTP
server, set in **Dashboard → Settings → Integrations → Email (SMTP)**. Pick a
**Provider** preset to fill SMTP Host, Port and Encryption, enter the credentials, press
**Send Test Email**, then **Save SMTP**. Presets are only a convenience: what is
saved is the host, port, encryption and credentials (`provider` is `custom`, or
`postmark` for Postmark). Anything not listed works through **Custom SMTP**
(Amazon SES, Mailgun, SendGrid, Brevo, …).

:::caution OAuth2 is not supported
The mailer signs in with **SMTP username + password (or app password / token)
only**. **OAuth2 / XOAUTH2 for Gmail and Microsoft 365 / Outlook.com is NOT
supported yet** (planned follow-up). Where a provider only accepts OAuth2, the
preset says so instead of offering a password login.
:::

## Encryption: Auto / STARTTLS / SSL-TLS / None

| Setting | What it does |
| --- | --- |
| **Auto** (default, what every existing install uses) | Chosen from the port: **465 → SSL/TLS**, **25 → none**, **any other port → STARTTLS**. |
| **STARTTLS** | Plain connect, then upgrade to TLS (usually port 587). |
| **SSL/TLS** | TLS from the first byte (usually port 465). |
| **None** | No encryption. Only for a trusted LAN relay or a test inbox (for example Mailpit on port 1025 or a relay on 2525). A login over *None* is sent in clear text. |

An explicit choice always wins over the port. The test button, the platform
mailer and the Keycloak realm (password reset / verification mail) all use the
same rule.

| Port | Auto (before and after this change) | Explicit STARTTLS | Explicit SSL/TLS | Explicit None |
| --- | --- | --- | --- | --- |
| 25 | none | STARTTLS | SSL/TLS | none |
| 465 | SSL/TLS | STARTTLS | SSL/TLS | none |
| 587 | STARTTLS | STARTTLS | SSL/TLS | none |
| 2525 / 1025 | STARTTLS | STARTTLS | SSL/TLS | none |

## Allow self-signed certificate

Tick **Allow self-signed certificate** for an internal relay that presents a
self-signed or private-CA certificate. The connection **stays encrypted**, but
the server certificate and host name are **not verified**, so anyone who can
intercept traffic between this server and the relay could impersonate it. Use it
only on a network you control; prefer installing a certificate from a trusted CA.
A warning is written to the API log the first time a message is sent this way.
The option applies to the mailer and the test button. **Keycloak** (password-reset
mail) keeps its own trust store, so a private-CA relay must also be trusted
inside Keycloak.

## Presets and verified settings

Settings below were read from each provider's own help page on **2026-11-09**.
Provider rules change (Google, Microsoft and Yahoo have been retiring password
logins): if a login that used to work starts failing, check the linked page first.

| Preset | Host | Port / encryption | Credential | Prerequisites and notes | Official page | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| **Google (Gmail / Workspace)** | `smtp.gmail.com` | 587 STARTTLS, or 465 SSL/TLS | **App password** (16 characters), username = full address | 2-Step Verification must be on; a Workspace admin can disable app passwords. Google says it recommends OAuth and that Workspace no longer accepts normal username + password sign-in from third-party apps since 1 May 2025; app passwords are still documented for `smtp.gmail.com`. Limits: ~500/day personal Gmail, 2,000/day Workspace. | [Workspace](https://support.google.com/a/answer/176600), [app passwords](https://support.google.com/mail/answer/185833), [limits](https://support.google.com/a/answer/166852) | Host, ports, credential, limits: yes. From rule (signed-in address or verified alias): from experience, not from the page. |
| **Google Workspace SMTP relay** | `smtp-relay.gmail.com` | 587 STARTTLS (25/465 also exist; SMTP auth needs TLS) | **None**: the server's public IP is allowlisted | Admin console → Apps → Google Workspace → Gmail → Routing → SMTP relay service; choose allowed senders, tick *Only accept mail from the specified IP addresses*. 10,000 messages/day per user. | [Route outgoing SMTP relay](https://support.google.com/a/answer/2956491), [printer/app options](https://support.google.com/a/answer/176600) | Yes |
| **Outlook.com / Hotmail** | `smtp-mail.outlook.com` | 587 STARTTLS | **OAuth2 only**: no preset offered | Microsoft's settings page lists OAuth2 as the SMTP method and says Basic authentication is no longer available for Outlook accounts (since 16 Sep 2024); it also mentions app passwords for "smart devices" without a clear SMTP promise, so password SMTP is not offered. Use Custom or your own relay. | [Outlook.com settings](https://support.microsoft.com/en-us/office/pop-imap-and-smtp-settings-for-outlook-com-d088b986-291d-42b8-9564-9c414e2aa040), [basic-auth end](https://support.microsoft.com/en-us/office/modern-authentication-methods-now-needed-to-continue-syncing-outlook-email-in-non-microsoft-email-apps-c5d65390-9676-4763-b41f-d7986499a90d) | Yes (OAuth2-only reading) |
| **Microsoft 365 / Exchange Online** | `smtp.office365.com` | 587 STARTTLS (TLS 1.2/1.3) | Mailbox password (basic SMTP AUTH) | **Retiring.** Unchanged until end of Dec 2026; then SMTP AUTH basic is **disabled by default** for existing Microsoft 365 organizations (an admin can re-enable it) and unavailable for new organizations; final removal date to be announced in H2 2027. Needs a licensed mailbox with SMTP AUTH enabled; the mailbox address is the sender; 10,000 recipients/day, 30 messages/minute. Alternative: your own relay (Custom). | [Send from a device or app](https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/how-to-set-up-a-multifunction-device-or-application-to-send-email-using-microsoft-365-or-office-365), [timeline](https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835) | Yes |
| **Apple iCloud Mail** | `smtp.mail.me.com` | 587 STARTTLS | **App-specific password**, username = full iCloud address | Apple Account must have two-factor authentication. | [Server settings](https://support.apple.com/en-us/102525), [app-specific passwords](https://support.apple.com/en-us/102654) | Yes |
| **Fastmail** | `smtp.fastmail.com` | 465 SSL/TLS (587 STARTTLS if needed) | **App password**; the regular password cannot be used | Basic plans have no SMTP for third-party apps. | [Server names and ports](https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports), [app passwords](https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords) | Yes |
| **Proton Mail** | `smtp.protonmail.ch` | 587 STARTTLS | **SMTP token** as password, username = the custom-domain address it is paired with | Paid Proton plan **with a custom-domain address**; generate the token in Settings → IMAP/SMTP → SMTP tokens. The Proton login password does not work. Sent mail is not end-to-end encrypted. | [SMTP submission](https://proton.me/support/smtp-submission) | Yes |
| **Yahoo Mail** | `smtp.mail.yahoo.com` | 465 SSL/TLS (Yahoo lists "465 or 587", SSL required) | **App password**, username = full address | Create it under Yahoo Account Security → Create app password. | [SMTP settings](https://help.yahoo.com/kb/SLN4075.html), [app passwords](https://help.yahoo.com/kb/SLN15241.html) | Host, ports, credential: yes. That 587 is STARTTLS: assumed from the standard. |
| **Zoho Mail** | `smtp.zoho.com` (personal / free org), `smtppro.zoho.com` (paid org, own domain) | 465 SSL, or 587 TLS (STARTTLS) | Account password; **application-specific password** when two-factor is on | The From address must be the account address or one of its aliases. Other data centres (EU, India, Australia, Japan, …): use the host shown in Zoho Mail → Settings → Mail Accounts → Server Configuration Details; the `.eu` / `.in` / `.com.au` / `.jp` names are only given by third-party guides (unverified). | [Zoho SMTP](https://www.zoho.com/mail/help/zoho-smtp.html) | `.com` hosts: yes. Regional hosts: **unverified, confirm with your provider**. |
| **mailbox.org** | `smtp.mailbox.org` | 465 SSL/TLS, or 587 STARTTLS | Mailbox password; **mail app password** when two-factor is on (allow SMTP) | | [Email configuration](https://kb.mailbox.org/en/business/e-mail/e-mail-configuration/), [app passwords](https://kb.mailbox.org/en/private/security-and-privacy/e-mail-password/) | Yes |
| **Migadu** | `smtp.migadu.com` | 465 SSL/TLS | Mailbox password, username = full mailbox address | | [Guides](https://www.migadu.com/guides/) | Yes |
| **GMX** | `mail.gmx.net` (gmx.com accounts: `mail.gmx.com`) | 587 STARTTLS, or 465 SSL/TLS | Account password; **application-specific password** with two-factor | GMX's help says POP3/IMAP access must be switched on for external programs; that it also covers sending is **unverified**. | [IMAP and SMTP data (.net)](https://hilfe.gmx.net/pop-imap/imap/imap-serverdaten.html), [(.com)](https://support.gmx.com/pop-imap/imap/server.html), [app passwords](https://support.gmx.com/security/2fa/application-specific-passwords.html) | Host, ports: yes. The enable-access rule: unverified. |
| **WEB.DE** | `smtp.web.de` | 587 STARTTLS (465 SSL alternative) | Account password | Same enable-access note as GMX. | [IMAP and SMTP data](https://hilfe.web.de/pop-imap/imap/imap-serverdaten.html) | Host, ports: yes. |
| **My own mail server** (Mailcow, Mailu, docker-mailserver, Postfix, Synology MailPlus) | your host | 587 STARTTLS by default | A mailbox login | A LAN-only relay may use port 25 or 2525 with **None** and no login. Private-CA / self-signed certificate: tick *Allow self-signed certificate*. | your server's documentation | n/a |
| **Local test relay** (Mailpit / MailHog) | container name (default `mailpit`) | 1025, **None** | none | Mailpit listens on 1025 (SMTP) and 8025 (web). `127.0.0.1` would be the API container itself, so use the Mailpit container name or the host's LAN IP. | [Mailpit](https://mailpit.axllent.org/docs/install/) | Yes |
| **Postmark** | `smtp.postmarkapp.com` | 587 STARTTLS | Server token (user and password) | Unchanged. | Postmark dashboard | not re-checked here |

## Google, step by step

**Gmail or Google Workspace mailbox (app password)**

1. Turn on **2-Step Verification** on the account (Google Account → Security).
2. Open [App passwords](https://myaccount.google.com/apppasswords), create one named for this server and copy the 16 characters (it is shown once). If the option is missing, the account has no 2-Step Verification, uses only security keys, has Advanced Protection, or is a work account whose admin disabled it.
3. In MOD choose **Google**: host `smtp.gmail.com`, 587 + STARTTLS (or 465 + SSL/TLS), username = the full address, Password / App Password = the app password.
4. Set **From Email** to that address or to a *Send mail as* alias verified in Gmail. Gmail rewrites any other From to the signed-in address.
5. Press **Send Test Email**. If you see "Gmail needs an app password", the normal password was used, 2-Step Verification is off, or the admin blocked app passwords.

**Google Workspace SMTP relay (no password)**

1. Admin console → **Apps → Google Workspace → Gmail → Routing → SMTP relay service → Configure**.
2. Allowed senders: *Only addresses in my domains* is the usual choice. Authentication: *Only accept mail from the specified IP addresses* and add this server's **public** IP (keep the range small). Optionally require TLS.
3. Wait for the change to propagate (up to 24 hours, usually minutes).
4. In MOD choose **Google Workspace SMTP relay**: host `smtp-relay.gmail.com`, port 587, STARTTLS, **leave username and password empty**, From = an address in your domain.

## Friendly errors

**Send Test Email** shows one sentence of advice followed by the server's own reply
(never the password): app password needed (Google, Yahoo, iCloud, Fastmail, Zoho,
mailbox.org, GMX), Proton SMTP token, Microsoft basic authentication disabled,
STARTTLS required, port/encryption mismatch (587 STARTTLS vs 465 SSL/TLS),
certificate not trusted, sender not allowed, relay denied, host unreachable.

## Upgrading

New installs and existing ones need no action: the two new settings default to
**Auto** and *self-signed off*, which reproduces the previous behaviour for every
port. The schema change is `migrations/20261109b_tenant_email_smtp_security.py`
(additive, `--commit` to apply; the API also adds the two columns on start if the
migration has not run yet).

## Not supported yet

- OAuth2 / XOAUTH2 sign-in for Gmail, Google Workspace, Microsoft 365 and Outlook.com.
- Provider HTTP APIs (SES, Mailgun, SendGrid, Brevo API): use their SMTP endpoint through Custom SMTP.
