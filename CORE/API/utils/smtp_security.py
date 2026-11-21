"""Shared SMTP transport-security rules (pure: no DB, no network, no app imports).

One place decides how the platform talks to an SMTP server, so the mailer
(`utils/email_sender._send_email_smtp`), the Settings "Send test" endpoint
(`routers/admin_email_config`) and the Keycloak realm sync
(`keycloak_auth.update_realm_smtp`) can never disagree.

`security` is the operator's explicit "Encryption" choice stored on
`tenant_email_configs.smtp_security`: 'starttls' | 'ssl' | 'none', or
NULL / 'auto' meaning "guess from the port".  'auto' reproduces the historical
port rule EXACTLY so installs that never touched the new field behave as before:

    465 -> ssl   25 -> none   anything else -> starttls

Explicit values always win (a local relay on 2525/1025 with no TLS is `none`).

OAuth2 / XOAUTH2 (Gmail, Microsoft 365) is NOT supported: SMTP AUTH with a
username + (app) password only.
"""
from __future__ import annotations

import re
import ssl
from typing import Iterable, Optional

SECURITY_CHOICES = ("auto", "starttls", "ssl", "none")
MODES = ("ssl", "starttls", "none")

DEFAULT_PORT = 587


def normalize_security(value) -> Optional[str]:
    """Validate an operator-supplied security value.

    Returns None for auto (None / '' / 'auto') or the lower-case explicit value.
    Raises ValueError for anything else (the API turns that into a 422).
    """
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("", "auto"):
        return None
    if v in MODES:
        return v
    raise ValueError(f"smtp_security must be one of {', '.join(SECURITY_CHOICES)}")


def _port_int(port) -> int:
    try:
        return int(port)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def resolve_smtp_mode(port, security=None) -> str:
    """Return 'ssl' | 'starttls' | 'none' for this port + operator choice.

    'auto' / None / unrecognised -> the legacy port rule (465 ssl, 25 none,
    else starttls). Explicit 'starttls' / 'ssl' / 'none' win over the port.
    """
    try:
        explicit = normalize_security(security)
    except ValueError:
        explicit = None
    if explicit:
        return explicit
    p = _port_int(port)
    if p == 465:
        return "ssl"
    if p == 25:
        return "none"
    return "starttls"


def keycloak_smtp_flags(port, security, user) -> dict:
    """Keycloak realm `smtpServer` auth / starttls / ssl strings ('true'/'false').

    'auto': byte-for-byte what keycloak_auth emitted before this helper
    (auth = user present; starttls only for 587/465; ssl only for 465), so an
    existing install's realm config is unchanged when re-synced.
    Explicit modes use one consistent mapping derived from the mailer's mode:
    ssl -> ssl only, starttls -> starttls only, none -> neither.
    """
    auth = "true" if user else "false"
    try:
        explicit = normalize_security(security)
    except ValueError:
        explicit = None
    if not explicit:
        p = _port_int(port)
        return {
            "auth": auth,
            "starttls": "true" if p in (587, 465) else "false",
            "ssl": "true" if p == 465 else "false",
        }
    return {
        "auth": auth,
        "starttls": "true" if explicit == "starttls" else "false",
        "ssl": "true" if explicit == "ssl" else "false",
    }


def build_ssl_context(allow_selfsigned: bool = False) -> ssl.SSLContext:
    """Default verifying context; only when `allow_selfsigned` is true, one that
    skips certificate AND hostname verification (encryption without trust)."""
    ctx = ssl.create_default_context()
    if allow_selfsigned:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def config_is_complete(host, user, password, sender_email, security=None) -> bool:
    """Whether a platform email config can send.

    Historical rule (kept for 'auto'): host + user + password + sender are all
    required. An EXPLICIT security choice also allows an unauthenticated server
    (local relay, smtp-relay.gmail.com): host + sender, and a password only if a
    user is given.
    """
    if not (host and sender_email):
        return False
    if user:
        return bool(password)
    try:
        return normalize_security(security) is not None
    except ValueError:
        return False


# ----------------------------------------------------------------------
# Friendly errors
# ----------------------------------------------------------------------

def _is_gmail(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return h.endswith("gmail.com") or h.endswith("googlemail.com") or h.endswith(".google.com")


_FAMILY_NAMES = {
    "yahoo": "Yahoo",
    "icloud": "iCloud",
    "fastmail": "Fastmail",
    "zoho": "Zoho",
    "mailbox": "mailbox.org",
    "gmx": "GMX / web.de",
}


def _family(host: str) -> str:
    """Coarse provider family from the SMTP host name (for friendly errors only)."""
    h = (host or "").strip().lower().rstrip(".")
    if h.endswith(("outlook.com", "office365.com", "hotmail.com", "live.com", "microsoft.com")):
        return "microsoft"
    if h.endswith(("protonmail.ch", "proton.me")):
        return "proton"
    if "yahoo." in h or h.endswith("ymail.com"):
        return "yahoo"
    if h.endswith(("me.com", "icloud.com", "mac.com")):
        return "icloud"
    if "fastmail." in h:
        return "fastmail"
    if h.startswith(("smtp.zoho", "smtppro.zoho")) or ".zoho." in h or h.endswith(".zohocloud.ca"):
        return "zoho"
    if h.endswith("mailbox.org"):
        return "mailbox"
    if h.endswith(("gmx.net", "gmx.com", "gmx.de", "web.de")):
        return "gmx"
    return ""


def _is_google_relay(host: str) -> bool:
    return (host or "").strip().lower().startswith("smtp-relay.") and _is_gmail(host)


def _clean(text: str, secrets: Iterable[str], limit: int = 160) -> str:
    out = " ".join(str(text).split())
    for s in secrets or ():
        if s:
            out = out.replace(s, "***")
    return out[:limit]


def _code_and_text(exc_or_text):
    code = getattr(exc_or_text, "smtp_code", None)
    err = getattr(exc_or_text, "smtp_error", None)
    if isinstance(err, bytes):
        err = err.decode("utf-8", errors="replace")
    if code is not None or err is not None:
        text = f"{code or ''} {err or ''}".strip()
    else:
        text = str(exc_or_text)
        m = re.search(r"\b([45]\d\d)\b", text)
        code = int(m.group(1)) if m else None
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, text


def _isa(exc, *names: str) -> bool:
    """Class-name match over the MRO, so this module needs no smtplib/socket import."""
    if not isinstance(exc, BaseException):
        return False
    return any(c.__name__ in names for c in type(exc).__mro__)


def friendly_smtp_error(exc_or_text, host, port, mode=None, *, secrets: Iterable[str] = ()) -> str:
    """Map a failure to short, actionable advice (never includes a password).

    `exc_or_text` is the exception (preferred) or its text. `secrets` are values
    (e.g. the SMTP password) to scrub from the echoed server detail, as a second
    line of defence. Unknown failures return 'SMTP error: <scrubbed text>'.
    """
    code, text = _code_and_text(exc_or_text)
    low = text.lower()
    host_s = host or "the server"
    hp = f"{host_s}:{port}" if port else host_s
    p = _port_int(port)
    mode = mode or resolve_smtp_mode(port, None)
    detail = _clean(text, secrets)
    suffix = f" (server said: {detail})" if detail else ""
    exc = exc_or_text if isinstance(exc_or_text, BaseException) else None

    gmail = _is_gmail(host_s)

    # 1. Certificate trust.
    if (
        isinstance(exc, ssl.SSLCertVerificationError)
        or "certificate_verify_failed" in low
        or "certificate verify failed" in low
        or "self-signed certificate" in low
        or "self signed certificate" in low
        or "hostname mismatch" in low
    ):
        return (
            "The server certificate is not trusted: use a trusted certificate "
            "or tick 'Allow self-signed certificate'." + suffix
        )

    # 2. TLS / port mismatch.
    if (
        "wrong_version_number" in low
        or "wrong version number" in low
        or "unexpected eof" in low
        or "eof occurred in violation" in low
        or "unexpectedly closed" in low
        or _isa(exc, "SMTPServerDisconnected")
        or (isinstance(exc, ssl.SSLError) and not isinstance(exc, ssl.SSLCertVerificationError))
    ):
        return (
            f"The TLS handshake with {hp} failed: port 587 uses STARTTLS, port 465 "
            "uses SSL/TLS (and some relays use neither); set Encryption to match the port." + suffix
        )

    # 3. STARTTLS required / not offered.
    if "must issue a starttls" in low or (
        "starttls" in low and "not supported" not in low and "extension" not in low
    ):
        return "The server requires encryption: choose Encryption = STARTTLS (usually port 587)." + suffix
    if _isa(exc, "SMTPNotSupportedError") or "extension not supported" in low:
        if "auth" in low:
            return (
                f"{host_s} does not accept a login on this connection: clear the username and "
                "password (IP-allowlisted relays need none), or enable encryption first." + suffix
            )
        return (
            f"{host_s} does not offer STARTTLS: choose Encryption = None (or SSL/TLS on port 465)." + suffix
        )

    # 4. Authentication.
    ms_basic_off = (
        "5.7.139" in low
        or "5.7.57" in low
        or "5.7.30" in low
        or "basic authentication is disabled" in low
        or "basic authentication is not supported" in low
        or "smtpclientauthentication is disabled" in low
    )
    family = _family(host_s)
    auth_failed = (
        _isa(exc, "SMTPAuthenticationError")
        or code in (534, 535)
        or "5.7.8" in low
        or "5.7.9" in low
        or "authentication failed" in low
        or "authentication unsuccessful" in low
        or "username and password not accepted" in low
        or "application-specific password" in low
        or ms_basic_off
    )
    if auth_failed:
        if ms_basic_off or family == "microsoft":
            return (
                "Microsoft is retiring password (basic) SMTP sign-in: use an app password if the "
                "account allows it, or switch to a relay (Custom / self-hosted); OAuth2 is not "
                "supported yet." + suffix
            )
        if _is_google_relay(host_s):
            return (
                "smtp-relay.gmail.com normally needs no username or password (it trusts the "
                "server's allowlisted public IP): clear them, or use an app password." + suffix
            )
        if gmail:
            return (
                "Gmail needs an app password (Google Account > Security > 2-Step Verification > "
                "App passwords); your normal Google password will not work. Workspace admins "
                "can disable app passwords." + suffix
            )
        if family == "proton":
            return (
                "Proton needs the SMTP submission token (Settings > IMAP/SMTP > SMTP tokens) as "
                "the password and the custom-domain address as the username, not your login "
                "password." + suffix
            )
        if family in ("yahoo", "icloud", "fastmail", "zoho", "mailbox", "gmx"):
            return (
                f"{_FAMILY_NAMES[family]} needs an app-specific password for SMTP (create one in "
                "the account's security settings); your normal password will not work when "
                "two-factor authentication is on, and the username must be the full address." + suffix
            )
        return "SMTP login failed: check the username and password." + suffix
    if code == 530:
        if mode == "none" or "5.7.0" in low and "authentication" not in low:
            return "The server requires encryption: choose Encryption = STARTTLS (usually port 587)." + suffix
        return "The server requires a login: enter the SMTP username and password." + suffix

    # 5. Relay denied.
    if "relay" in low and ("denied" in low or "not permitted" in low or "not allowed" in low):
        if _is_google_relay(host_s):
            return (
                "Google refused to relay: add this server's public IP in Workspace Admin > Apps > "
                "Google Workspace > Gmail > Routing > SMTP relay service." + suffix
            )
        return (
            "The server refused to relay this mail: sign in with a username and password, or ask "
            "its admin to allow this server's IP." + suffix
        )

    # 6. Sender rejected.
    sender_refused = _isa(exc, "SMTPSenderRefused") or (
        code in (550, 553) and not _isa(exc, "SMTPRecipientsRefused")
    )
    if sender_refused or "5.7.60" in low:
        if family == "microsoft" or "5.7.60" in low:
            return (
                "Microsoft only sends as the signed-in mailbox or an address it has Send As "
                "permission for: set the From address accordingly." + suffix
            )
        if gmail:
            return (
                "Gmail only sends as the signed-in address or a verified alias: set the From "
                "address to the account's address or a verified alias." + suffix
            )
        return (
            "The server rejected the From address: use an address on a domain the provider has verified." + suffix
        )
    if _isa(exc, "SMTPRecipientsRefused"):
        return "The server refused the recipient address." + suffix

    # 7. Reachability.
    if (
        _isa(exc, "ConnectionRefusedError", "gaierror", "timeout", "TimeoutError")
        or "connection refused" in low
        or "timed out" in low
        or "name or service not known" in low
        or "nodename nor servname" in low
        or "temporary failure in name resolution" in low
        or "getaddrinfo failed" in low
        or "network is unreachable" in low
        or "no route to host" in low
        or _isa(exc, "ConnectionError")
    ):
        if p == 465 and mode != "ssl" and ("timed out" in low or _isa(exc, "timeout", "TimeoutError")):
            return (
                f"No answer from {hp}: port 465 expects SSL/TLS from the first byte, so set "
                "Encryption = SSL/TLS (or use port 587 with STARTTLS)." + suffix
            )
        if "refused" in low or _isa(exc, "ConnectionRefusedError"):
            why = "connection refused"
        elif "name" in low or "getaddrinfo" in low or _isa(exc, "gaierror"):
            why = "host name not found"
        else:
            why = "connection timed out or blocked"
        return (
            f"Cannot reach {hp} ({why}): check the host name, the port and any firewall; "
            "many hosts block outbound ports 25/465/587." + suffix
        )

    return f"SMTP error: {detail}" if detail else "SMTP error: unknown failure"
