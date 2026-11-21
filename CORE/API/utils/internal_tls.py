"""GSEC FX.13 R-059 — internal-hop TLS enforcement plumbing.

The API talks to several in-cluster services (Qdrant, vLLM/LLM stacks,
Vault, Keycloak, ...) over the docker bridge, currently in the clear.
The rollout (design: docs/security-audit/design/R-056-R-059-transport-tls.md
§ R-059 step 1-2) issues one leaf per service from the platform CA and
flips each hop's URL scheme in compose. The client-side half is this
module: it derives the `verify=` value for requests/httpx on internal
service hops from the platform setting
`gsec.transport.internal_tls_enforce` (off | report | require).

Inert by default: mode `off` (the default) returns exactly today's
behaviour — `verify=True` and no URL refusal — so wiring a call site
changes nothing until an admin flips the setting.

`MOD_INTERNAL_CA_BUNDLE` is a bootstrap path (where the shared CA bundle
is bind-mounted), NOT a behavioural flag: the env only points at the
bundle, the mode decides what it does.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from services import platform_settings

logger = logging.getLogger(__name__)

INTERNAL_TLS_SETTING = "gsec.transport.internal_tls_enforce"

# Hosts we have already reported a plain-http hop for (report mode logs
# once per host, not once per request).
_reported_hosts = set()


class InternalTLSConfigError(RuntimeError):
    """Raise when a mode demands resources that are absent (e.g. the CA
    bundle in `require` mode) or a URL the mode forbids is used (plain
    http:// in `require` mode)."""


def _mode(db=None) -> str:
    """Current enforcement mode; a settings-store read fault degrades to
    `off` so this plumbing can never take internal hops down."""
    try:
        value = platform_settings.get_setting(
            INTERNAL_TLS_SETTING, db=db,
        )
    except Exception:  # noqa: BLE001 — fail-open to today's behaviour
        logger.warning(
            "R-059: failed to read %s; defaulting to off",
            INTERNAL_TLS_SETTING, exc_info=True)
        return "off"
    if value not in ("off", "report", "require"):
        return "off"
    return value


def internal_ca_bundle() -> Optional[str]:
    """Path of the shared internal CA bundle, or None.

    Bootstrap env `MOD_INTERNAL_CA_BUNDLE` names the bind-mounted bundle;
    it returns a path only when the file actually exists. This is a
    bootstrap path, not a behavioural flag — `off` mode never consults
    it (verify stays True) and `require` mode raises if it is missing.
    """
    path = os.getenv("MOD_INTERNAL_CA_BUNDLE", "")
    if path and os.path.isfile(path):
        return path
    return None


def internal_verify_arg(db=None):
    """The value to pass as requests/httpx `verify=` for an internal
    service hop, per the current enforcement mode:

    - off     -> True (current behaviour, byte-identical)
    - report  -> the CA bundle path when present, else True
    - require -> the CA bundle path (raises InternalTLSConfigError when
                 the bundle is missing)
    """
    mode = _mode(db)
    if mode == "off":
        # Current behaviour, byte-identical: never consult the bundle.
        return True
    if mode == "require":
        bundle = internal_ca_bundle()
        if bundle is None:
            raise InternalTLSConfigError(
                "R-059: gsec.transport.internal_tls_enforce=require but no "
                "internal CA bundle is available — set "
                "MOD_INTERNAL_CA_BUNDLE to a path containing the platform "
                "CA and retry, or set the setting back to 'report'/'off'")
        return bundle
    # report: CA bundle when present, else today's behaviour (True).
    bundle = internal_ca_bundle()
    return bundle if bundle is not None else True


def check_internal_url(url: str, mode: Optional[str] = None) -> None:
    """Validate an internal-service URL against an enforcement mode.

    Raises InternalTLSConfigError when the mode is `require` and the URL
    is plain http:// (a hop that would run in the clear). `off` and
    `report` allow it; in `report` mode the plain-http hop is logged once
    per host so a soak can see exactly which services still run clear.
    """
    if mode is None:
        mode = _mode()
    if mode == "off":
        return
    if not url.lower().startswith("http://"):
        return
    host = urlsplit(url).netloc or url
    if mode == "require":
        raise InternalTLSConfigError(
            "R-059: plain http:// internal URL refused under "
            "gsec.transport.internal_tls_enforce=require: "
            f"{host} — flip the service URL to https (R-059 step 2) or "
            "dial the setting back to 'report'")
    if mode == "report" and host not in _reported_hosts:
        _reported_hosts.add(host)
        logger.warning(
            "R-059 report: internal hop still plaintext (http): %s — "
            "schedule the https flip (R-059 step 2)", host)


def reset_reported_hosts() -> None:
    """Test hook: clear the once-per-host report set."""
    _reported_hosts.clear()


def internal_client_kwargs(db=None, url: Optional[str] = None) -> dict:
    """GY.C6.s3 — the client kwargs an internal-service hop needs.

    The seam that turns the inert plumbing on: a call site builds its
    client through this instead of hard-coding `verify=True`, so flipping
    `gsec.transport.internal_tls_enforce` to `require` actually refuses
    and re-verifies the hop. Additive only:

    - off     -> {} (the call site keeps today's shape byte-identically;
                  the `off` branch is never touched)
    - report  -> {"verify": <bundle>} when the bundle is present, else {}
    - require -> {"verify": <bundle>} and raises InternalTLSConfigError
                  when the bundle is missing or `url` is plain http://

    Never emits `verify=False` and never adds a clear/plaintext fallback.
    `url` is optional so a call site that cannot name its hop still gets
    the bundle; a call site that CAN name it gets the refusal too, which
    is what makes `require` enforce instead of merely report.
    """
    mode = _mode(db)
    if mode == "off":
        # Current behaviour, byte-identical: no kwargs at all.
        return {}
    kwargs: dict = {}
    if mode == "require":
        bundle = internal_ca_bundle()
        if bundle is None:
            raise InternalTLSConfigError(
                "R-059: gsec.transport.internal_tls_enforce=require but no "
                "internal CA bundle is available — set "
                "MOD_INTERNAL_CA_BUNDLE to a path containing the platform "
                "CA and retry, or set the setting back to 'report'/'off'")
        kwargs["verify"] = bundle
    else:
        bundle = internal_ca_bundle()
        if bundle is not None:
            kwargs["verify"] = bundle
    if url:
        check_internal_url(url, mode=mode)
    return kwargs
