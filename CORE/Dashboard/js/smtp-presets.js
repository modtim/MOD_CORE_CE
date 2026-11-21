// Email (SMTP) provider presets for Settings > Integrations > Email.
//
// UI-only sugar: a preset fills host / port / encryption and shows a hint. What
// is SAVED to the API is provider='custom' (or 'postmark') plus host/port/
// security, so the backend provider enum needs no new values. Pure data + a few
// tiny functions, no imports, so a node check can load it directly.
//
// Every value below was read from the provider's OWN help page (URL kept in
// `doc`); see CORE/docs/EMAIL_SMTP_PROVIDERS.md for the verification table. A
// preset marked `oauthOnly` fills nothing: password SMTP does not work there.
// OAuth2 / XOAUTH2 is NOT supported by the platform mailer.

export const SMTP_SECURITY_OPTIONS = ['auto', 'starttls', 'ssl', 'none'];

export const SMTP_PRESETS = {
    postmark: {
        label: 'Postmark',
        host: 'smtp.postmarkapp.com', port: 587, security: 'auto',
        hint: 'Postmark: paste your <strong>Server Token</strong> from the Postmark dashboard (Servers → your server → API Tokens).',
    },
    google: {
        label: 'Google (Gmail / Google Workspace)',
        host: 'smtp.gmail.com', port: 587, security: 'starttls',
        userPlaceholder: 'full Gmail / Workspace address',
        doc: 'https://support.google.com/a/answer/176600',
        hint: 'Host <strong>smtp.gmail.com</strong>, port <strong>587 STARTTLS</strong> (or 465 SSL/TLS). '
            + 'Username = the <strong>full Gmail / Workspace address</strong>. Use an <strong>app password</strong> '
            + '(needs 2-Step Verification; your normal Google password will not work, and a Workspace admin can disable app passwords). '
            + 'The From address must be that address or a verified alias. Limits: about 500 messages/day for personal Gmail, 2,000/day for Workspace. '
            + 'Google recommends OAuth for third-party apps, which is not supported here yet: if app passwords are blocked use the Workspace SMTP relay preset.',
    },
    'google-relay': {
        label: 'Google Workspace SMTP relay',
        host: 'smtp-relay.gmail.com', port: 587, security: 'starttls', noAuth: true,
        userPlaceholder: 'leave blank (IP allowlist)',
        doc: 'https://support.google.com/a/answer/2956491',
        hint: 'Host <strong>smtp-relay.gmail.com</strong>, port <strong>587</strong> STARTTLS, <strong>no password</strong>: '
            + 'leave username and password empty. This server\'s public IP must be allowlisted in the Workspace Admin console '
            + '(Apps &gt; Google Workspace &gt; Gmail &gt; Routing &gt; SMTP relay service). Up to 10,000 messages/day per user.',
    },
    outlook: {
        label: 'Outlook.com / Hotmail (OAuth2 only: not supported)',
        oauthOnly: true,
        doc: 'https://support.microsoft.com/en-us/office/pop-imap-and-smtp-settings-for-outlook-com-d088b986-291d-42b8-9564-9c414e2aa040',
        hint: '<strong>Not available.</strong> Microsoft\'s own Outlook.com settings page lists OAuth2 (modern auth) as the only SMTP sign-in method '
            + 'for personal Outlook.com / Hotmail accounts, and OAuth2 SMTP is not supported by this platform yet. '
            + 'Use the <em>Custom</em> or <em>My own mail server</em> preset with a relay you control, or another provider. '
            + 'Microsoft 365 business tenants: see the Microsoft 365 preset.',
    },
    microsoft365: {
        label: 'Microsoft 365 / Exchange Online (password login being retired)',
        host: 'smtp.office365.com', port: 587, security: 'starttls',
        userPlaceholder: 'full mailbox address',
        doc: 'https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/how-to-set-up-a-multifunction-device-or-application-to-send-email-using-microsoft-365-or-office-365',
        hint: 'Host <strong>smtp.office365.com</strong>, port <strong>587 STARTTLS</strong>, username = the licensed mailbox (it is also the sender). '
            + '<strong>Heads-up:</strong> Microsoft is retiring password (basic) SMTP AUTH: unchanged until the end of December 2026, then '
            + '<em>disabled by default</em> for existing Microsoft 365 organizations (an admin can re-enable it), unavailable for new organizations, final removal date to be announced '
            + 'for the second half of 2027. After that, use a relay you control '
            + '(Custom / My own mail server); OAuth2 is not supported here yet.',
    },
    icloud: {
        label: 'Apple iCloud Mail',
        host: 'smtp.mail.me.com', port: 587, security: 'starttls',
        userPlaceholder: 'full iCloud address (admin@example.com)',
        doc: 'https://support.apple.com/en-us/102525',
        hint: 'Host <strong>smtp.mail.me.com</strong>, port <strong>587 STARTTLS</strong>. Username = your <strong>full iCloud address</strong>. '
            + 'Password = an <strong>app-specific password</strong> (Apple Account sign-in &amp; security; needs two-factor authentication on the Apple Account). '
            + 'Your normal Apple password will not work.',
    },
    fastmail: {
        label: 'Fastmail',
        host: 'smtp.fastmail.com', port: 465, security: 'ssl',
        userPlaceholder: 'full Fastmail address',
        doc: 'https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports',
        hint: 'Host <strong>smtp.fastmail.com</strong>, port <strong>465 SSL/TLS</strong> (or 587 STARTTLS). Username = your full Fastmail address. '
            + 'Password = an <strong>app password</strong>; your regular Fastmail password cannot be used. '
            + 'Fastmail Basic plans do not include SMTP for third-party apps.',
    },
    proton: {
        label: 'Proton Mail (SMTP submission)',
        host: 'smtp.protonmail.ch', port: 587, security: 'starttls',
        userPlaceholder: 'your custom-domain Proton address',
        doc: 'https://proton.me/support/smtp-submission',
        hint: 'Host <strong>smtp.protonmail.ch</strong>, port <strong>587 STARTTLS</strong>. Needs a <strong>paid Proton plan with a custom-domain address</strong>. '
            + 'Create an <strong>SMTP token</strong> (Settings → IMAP/SMTP → SMTP tokens) and use <strong>that token as the password</strong> and the paired custom-domain address as the username. '
            + 'Your Proton login password will not work.',
    },
    yahoo: {
        label: 'Yahoo Mail',
        host: 'smtp.mail.yahoo.com', port: 465, security: 'ssl',
        userPlaceholder: 'full Yahoo address',
        doc: 'https://help.yahoo.com/kb/SLN4075.html',
        hint: 'Host <strong>smtp.mail.yahoo.com</strong>, port <strong>465 SSL/TLS</strong> (Yahoo lists 465 or 587, both with SSL). Username = your full Yahoo address. '
            + 'Password = an <strong>app password</strong> (Yahoo Account Security → Create app password); your normal password will not work.',
    },
    zoho: {
        label: 'Zoho Mail',
        host: 'smtp.zoho.com', port: 465, security: 'ssl',
        userPlaceholder: 'full Zoho address',
        doc: 'https://www.zoho.com/mail/help/zoho-smtp.html',
        hint: 'Host <strong>smtp.zoho.com</strong> (personal / free) or <strong>smtppro.zoho.com</strong> (paid organisation with your own domain), '
            + 'port <strong>465 SSL</strong> (or 587 TLS/STARTTLS). Username = your full Zoho address; it must be the account address or one of its aliases. '
            + 'If two-factor authentication is on, use an <strong>application-specific password</strong>. '
            + 'Accounts in another data centre (EU, India, Australia, Japan, …): use the host shown in Zoho Mail → Settings → Mail Accounts → Server Configuration Details '
            + '(usually the same name ending .eu / .in / .com.au / .jp: unverified: confirm with your provider).',
    },
    'mailbox-org': {
        label: 'mailbox.org',
        host: 'smtp.mailbox.org', port: 465, security: 'ssl',
        userPlaceholder: 'full mailbox.org address',
        doc: 'https://kb.mailbox.org/en/business/e-mail/e-mail-configuration/',
        hint: 'Host <strong>smtp.mailbox.org</strong>, port <strong>465 SSL/TLS</strong> (or 587 STARTTLS). Username = your main mailbox.org address. '
            + 'Password = your mailbox password, or a <strong>mail app password</strong> if two-factor authentication is on (allow SMTP when you create it).',
    },
    migadu: {
        label: 'Migadu',
        host: 'smtp.migadu.com', port: 465, security: 'ssl',
        userPlaceholder: 'full mailbox address',
        doc: 'https://www.migadu.com/guides/',
        hint: 'Host <strong>smtp.migadu.com</strong>, port <strong>465 SSL/TLS</strong>. Username = the <strong>full mailbox address</strong>, password = that mailbox\'s password.',
    },
    gmx: {
        label: 'GMX',
        host: 'mail.gmx.net', port: 587, security: 'starttls',
        userPlaceholder: 'full GMX address',
        doc: 'https://hilfe.gmx.net/pop-imap/imap/imap-serverdaten.html',
        hint: 'Host <strong>mail.gmx.net</strong> (accounts under gmx.com: mail.gmx.com), port <strong>587 STARTTLS</strong> (or 465 SSL/TLS). Username = your full GMX address. '
            + 'GMX\'s help says POP3/IMAP access must be switched on in the GMX settings for external programs (unverified: confirm with your provider that this also covers sending), '
            + 'and with two-factor authentication use an <strong>application-specific password</strong>.',
    },
    webde: {
        label: 'WEB.DE',
        host: 'smtp.web.de', port: 587, security: 'starttls',
        userPlaceholder: 'full WEB.DE address',
        doc: 'https://hilfe.web.de/pop-imap/imap/imap-serverdaten.html',
        hint: 'Host <strong>smtp.web.de</strong>, port <strong>587 STARTTLS</strong> (or 465 SSL). Username = your full WEB.DE address. '
            + 'WEB.DE\'s help says POP3/IMAP access must be switched on in the WEB.DE settings for external programs (unverified: confirm with your provider that this also covers sending).',
    },
    selfhosted: {
        label: 'My own mail server (Mailcow, Mailu, docker-mailserver, Postfix, Synology MailPlus)',
        host: '', port: 587, security: 'starttls',
        userPlaceholder: 'a mailbox on your server',
        hint: 'Enter <strong>your server\'s host name</strong>. Default is <strong>port 587 STARTTLS</strong> with a mailbox login. '
            + 'A LAN-only relay may use <strong>port 25 or 2525 with Encryption = None</strong> and no login. '
            + 'If the server uses a <strong>self-signed or private-CA certificate</strong>, tick <em>Allow self-signed certificate</em> '
            + '(encryption stays on, the certificate is not verified).',
    },
    local: {
        label: 'Local test relay (Mailpit / MailHog)',
        host: 'mailpit', port: 1025, security: 'none', noAuth: true,
        userPlaceholder: 'leave blank (no login)',
        doc: 'https://mailpit.axllent.org/docs/install/',
        hint: 'A catch-all test inbox: <strong>port 1025</strong>, Encryption <strong>None</strong>, <strong>no username or password</strong>. '
            + 'The host is the Mailpit / MailHog container name (default <em>mailpit</em>) or its LAN IP: <em>127.0.0.1</em> would be the API container itself. Nothing is delivered to real recipients.',
    },
    custom: {
        label: 'Custom SMTP (any other server: SES, Mailgun, SendGrid, Brevo, …)',
        host: '', port: 587, security: 'auto', keep: true,
        hint: 'Custom SMTP: host, port, encryption, username and password from your provider. '
            + '<strong>Auto</strong> picks the encryption from the port (465 SSL/TLS, 25 none, anything else STARTTLS).',
    },
};

export const SMTP_PRESET_ORDER = [
    'postmark', 'google', 'google-relay', 'outlook', 'microsoft365', 'icloud', 'fastmail',
    'proton', 'yahoo', 'zoho', 'mailbox-org', 'migadu', 'gmx', 'webde', 'selfhosted', 'local', 'custom',
];

const HOST_TO_PRESET = {
    'smtp.gmail.com': 'google',
    'smtp-relay.gmail.com': 'google-relay',
    'smtp.office365.com': 'microsoft365',
    'smtp.mail.me.com': 'icloud',
    'smtp.fastmail.com': 'fastmail',
    'smtp.protonmail.ch': 'proton',
    'smtp.mail.yahoo.com': 'yahoo',
    'smtp.zoho.com': 'zoho',
    'smtppro.zoho.com': 'zoho',
    'smtp.mailbox.org': 'mailbox-org',
    'smtp.migadu.com': 'migadu',
    'mail.gmx.net': 'gmx',
    'mail.gmx.com': 'gmx',
    'smtp.web.de': 'webde',
};

// Which preset the saved config looks like (display only: the saved provider
// stays 'custom'). Unknown hosts are 'custom'.
export function inferSmtpPreset(host, provider) {
    const h = String(host || '').trim().toLowerCase();
    if (provider === 'postmark' || h.includes('postmarkapp.com')) return 'postmark';
    return HOST_TO_PRESET[h] || 'custom';
}

// What the form fields should become when `key` is picked. `fill` is false for
// 'custom' (keep what the operator typed) and for OAuth-only entries (nothing to fill).
export function smtpPresetFields(key) {
    const p = SMTP_PRESETS[key];
    if (!p) return null;
    const fill = !p.keep && !p.oauthOnly;
    return {
        fill,
        host: p.host,
        port: p.port,
        security: p.security || 'auto',
        noAuth: !!p.noAuth,
        oauthOnly: !!p.oauthOnly,
        userPlaceholder: p.userPlaceholder || 'your-email@example.com',
        hint: p.hint,
    };
}

// The provider value the API gets: only Postmark keeps its own value.
export function smtpProviderForSave(key) {
    return key === 'postmark' ? 'postmark' : 'custom';
}
