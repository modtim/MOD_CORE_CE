"""GSEC FX.19 R-042 (SEC-20-003) — foreign-egress kill switches.

`docs/security-audit/EGRESS_INVENTORY.md` enumerates every path that can send
data out of a MOD install. A gov/CUI customer needs a way to *stop* the ones
that carry tenant content (NIM calls, HF weight pulls, external-LLM calls,
tenant-supplied webhooks, the wizard's internet lookups, and the license
phone-home). Today each had, at best, an admin-config gap (no key = no call)
and there was NO single master off-switch.

This module is the API-side decision point. The master switch
(`egress.sealed_mode`, default False) overrides EVERY per-category key: when
it is True, `egress_allowed` returns False for all categories. Each category
is its own boolean platform setting, defaulting to True so a deploy changes
nothing until an admin explicitly turns a switch off.

Setting reads are fail-safe by design (see `_bool`): if the master read fails
we assume NOT sealed (a settings-store outage must not silently seal a live
install and stop every outbound feature); if a per-category read fails we
fall back to that key's registered default (True for all of these).

Only the *API-side* gates live here. The frontend CDN rows (three.js wasm,
esm.sh loaders, Stripe.js, Tailwind CDN) are a separate task and are NOT
gated by this helper.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)

# The master switch. When True, every category is refused regardless of its
# own key. Registered in services/platform_settings.py under the same key.
SEALED_MODE_KEY = "egress.sealed_mode"

# Per-category keys, all bool, all defaulting to True.
CATEGORY_NIM = "egress.nim_enabled"
CATEGORY_MODEL_DOWNLOAD = "egress.model_download_enabled"
CATEGORY_EXTERNAL_LLM = "egress.external_llm_enabled"
CATEGORY_WEBHOOKS = "egress.webhooks_enabled"
CATEGORY_WIZARD_INTERNET = "egress.wizard_internet_enabled"
CATEGORY_LICENSE_PHONEHOME = "egress.license_phonehome_enabled"
# GY.C9 remainder — update-check (worker self-update advertisement +
# currency-rate fetch) and usage-telemetry (login RUM beacon) switches.
CATEGORY_UPDATE_CHECK = "egress.update_check_enabled"
CATEGORY_TELEMETRY = "egress.telemetry_enabled"

# GY.C9 remainder — in-process refusal ledger.
#
# Every egress gate that refuses a call logs ONE event here (category +
# site; site is a stable code like "sso_rum.first_paint_beacon", never a
# URL, body, key or tenant id). The compliance egress-status report
# surfaces the counts + last-refusal reasons so an operator can SEE that
# a sealed/degraded install is actually refusing egress, not silently
# skipping a gate. Bounded (per-site ring of recent reasons + total
# counters) and thread-safe; a process restart zeroes it (the DB audit
# rows are the durable record where a gate has one).
_MAX_REFUSAL_REASONS_PER_SITE = 5
_refusal_lock = threading.Lock()
_refusal_counts: Dict[str, int] = {}
_refusal_recent: Dict[str, Deque[str]] = {}


def record_refusal(category: str, site: str, reason: str) -> None:
    """Record one refused egress call. Never raises — refusal logging
    must not take the refusing caller down."""
    try:
        with _refusal_lock:
            _refusal_counts[category] = _refusal_counts.get(category, 0) + 1
            q = _refusal_recent.setdefault(category, deque(maxlen=_MAX_REFUSAL_REASONS_PER_SITE))
            q.append(f"{site}: {reason}")
    except Exception:  # noqa: BLE001 — best-effort by contract
        logger.debug("egress_policy: refusal recording failed", exc_info=True)


def refusal_counts() -> Dict[str, Dict[str, object]]:
    """Snapshot: {category: {"count": int, "recent_reasons": [str, ...]}} —
    categories with zero refusals are omitted. Metadata only (site codes +
    refusal strings), never secrets or tenant content."""
    with _refusal_lock:
        return {
            cat: {"count": cnt, "recent_reasons": list(recent)}
            for cat, cnt in _refusal_counts.items()
            for recent in (_refusal_recent.get(cat),)
            if cnt
        }

# Every category key we gate (sealed_mode consults all of them implicitly).
ALL_CATEGORY_KEYS = (
    CATEGORY_NIM,
    CATEGORY_MODEL_DOWNLOAD,
    CATEGORY_EXTERNAL_LLM,
    CATEGORY_WEBHOOKS,
    CATEGORY_WIZARD_INTERNET,
    CATEGORY_LICENSE_PHONEHOME,
    CATEGORY_UPDATE_CHECK,
    CATEGORY_TELEMETRY,
)

# The registered default for each key — the value `_bool` falls back to when
# a per-category read raises. All True: a broken read must not disable a
# feature that was never turned off.
_DEFAULTS = {
    SEALED_MODE_KEY: False,
    CATEGORY_NIM: True,
    CATEGORY_MODEL_DOWNLOAD: True,
    CATEGORY_EXTERNAL_LLM: True,
    CATEGORY_WEBHOOKS: True,
    CATEGORY_WIZARD_INTERNET: True,
    CATEGORY_LICENSE_PHONEHOME: True,
    CATEGORY_UPDATE_CHECK: True,
    CATEGORY_TELEMETRY: True,
}


def _bool(key: str, db=None, default: Optional[bool] = None) -> bool:
    """Read a boolean platform setting fail-safely.

    Returns `default` (or the registered default) when `key` is unknown or the
    read raises — the egress policy must never take a caller down because the
    settings store itself errored.
    """
    if default is None:
        default = _DEFAULTS.get(key, True)
    try:
        from services import platform_settings as ps
        return bool(ps.get_setting(key, db=db))
    except Exception as e:  # noqa: BLE001 — a settings read must never raise
        logger.warning("egress_policy: read of %s failed, using default %r: %s",
                       key, default, e)
        return bool(default)


def sealed_mode(db=None) -> bool:
    """True when the master egress switch is on. A read failure is treated as
    NOT sealed (fail open, per the module contract)."""
    return _bool(SEALED_MODE_KEY, db=db, default=False)


def egress_allowed(category: str, db=None) -> bool:
    """Master switch first, then the per-category key.

    Returns False whenever `sealed_mode` is True; otherwise returns the value
    of `category`'s own setting (default True). Unknown category keys are
    treated as allowed (True) so a typo in a future call site cannot silently
    cut a feature.
    """
    if sealed_mode(db=db):
        return False
    return _bool(category, db=db)


def refuse_reason(category: str, db=None) -> Optional[str]:
    """Human-readable refusal string when `category` is NOT allowed, else None.

    Callers use this for log lines / error details so the operator can see
    exactly which switch stopped the egress.
    """
    if sealed_mode(db=db):
        return (f"egress disabled: {SEALED_MODE_KEY} is True (sealed mode) — "
                f"blocks {category}")
    if not egress_allowed(category, db=db):
        return f"egress disabled: {category} is False"
    return None


def refuse(category: str, db=None) -> str:
    """Return the refusal reason for `category`, or an empty string when the
    egress IS allowed. Callers either log it or raise with it as the detail.

    Example::

        reason = egress_policy.refuse(egress_policy.CATEGORY_NIM, db=db)
        if reason:
            raise ServiceError(reason)
    """
    return refuse_reason(category, db=db) or ""


# ── GY.C8 — target classification (which URLs leave MOD?) ─────────────────
#
# Pure string classification, no DNS/TCP probes — safe on every request,
# never raises. Mirrors `routers.ai_config.llm_binding_is_external` (the
# paid module is not importable from CE utils, so the rules live here too).
# Fail-closed: an empty or malformed target is treated as EXTERNAL, so a
# gate can never be bypassed by a bad URL.

_LOCAL_HOST_ALIASES = frozenset({
    "localhost", "0.0.0.0", "127.0.0.1",
    "ollama", "vllm", "vllm-big", "vllm-small", "llm",
    "managed-ollama", "model-cache",
})


def url_is_external(url: str) -> bool:
    """True when `url` points outside MOD's own network.

    NOT external: MOD stack/LLM aliases, localhost, single-label container
    names, RFC1918/CGNAT/link-local IP literals, `.local` mDNS, the BASE_DOMAIN
    suffix, mesh MagicDNS (`.ts.net`) and k8s service names. Everything else
    (public hostnames, public IPs) is external.
    """
    if not url:
        return True
    try:
        import urllib.parse
        host = (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:  # noqa: BLE001 — unparseable fails closed
        return True
    if not host:
        return True
    if host in _LOCAL_HOST_ALIASES or host.endswith(".local"):
        return False
    # Single-label names (docker service / stack container aliases) only
    # resolve inside MOD's own networks.
    if "." not in host:
        return False
    import os
    _base = (os.getenv("BASE_DOMAIN") or "").lower().strip(".")
    if _base and (host == _base or host.endswith("." + _base)):
        return False
    # Mesh MagicDNS service names and k8s service names.
    if ".ts.net" in host or host.endswith(".svc.cluster.local"):
        return False
    # IPv4 literal — classify by range.
    if host.count(".") == 3 and all(p.isdigit() for p in host.split(".")):
        a, b = (int(p) for p in host.split(".")[:2])
        if a in (10, 0) or a == 172 and 16 <= b <= 31 \
                or a == 192 and b == 168 or a == 169 and b == 254:
            return False
        # CGNAT (100.64.0.0/10) is the tailnet range — MOD's own mesh.
        if a == 100 and 64 <= b <= 127:
            return False
        return True
    # IPv6 literal — private/link-local are MOD-local.
    if ":" in host:
        v6 = host.strip("[]")
        return not (v6.startswith("fe80") or v6.startswith("fc")
                    or v6.startswith("fd") or v6 == "::1")
    return True
