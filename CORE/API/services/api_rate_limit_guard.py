"""FX.106 (owner Q93=A) — API rate limits + auto-block, app-level.

Owner decision Q93=A: enforce per-plan AND per-API-key rate limits at the
API layer (works on-prem with zero third parties), plus time-limited
auto-blocks of client IPs after repeated failed logins or repeated 429
limit hits. SaaS may additionally sit a DDoS scrubbing provider in front
(the provider is TBD; see the subprocessor draft) — nothing here calls a
third party, so the same code satisfies the gov/on-prem "no third party"
requirement (item 6).

What is reused (NOT duplicated):
  - ``utils.rate_limiter.check_rate_limit`` for the sliding-window
    counters (Valkey/Redis via REDIS_URL, bounded in-process fallback when
    the cache is down — fail-open, matching the R-083 contract).
  - ``utils.client_ip.trusted_client_ip`` for the real client IP (item 4:
    XFF / CF-Connecting-IP honoured only from ``net.trusted_proxy_cidrs``).
  - ``services.lockout_policy``'s audit seam for admin actions.
  - ``audit.log_security_event`` + ``models.AuditAction`` for every
    block/unblock row.

Limits are DB platform settings (``services.platform_settings``) — no new
env vars:

  api.rate_limit.enabled                  (bool, default True)
  api.rate_limit.per_key_per_minute       (int,  default 300)
  api.rate_limit.auto_block_enabled       (bool, default True)
  api.rate_limit.auto_block_after_429s    (int,  default 25)
  api.rate_limit.auto_block_after_login_failures (int, default 20)
  api.rate_limit.auto_block_window_min    (int,  default 15)
  api.rate_limit.auto_block_duration_min  (int,  default 60)
  api.rate_limit.auto_block_exempt_cidrs  (list, internal ranges, see
                                           is_auto_block_exempt)

FX.106 p1a adds the per-PLAN limits as registry settings (the plan column
remains the per-plan knob when the caller supplies it):

  api.rate_limit.plan_limits              (dict, plan name -> rpm)
  api.rate_limit.default_rpm              (int,  default 120)
  api.rate_limit.api_key_override_max_rpm (int,  default 20000)
  api.rate_limit.api_key_overrides        (dict, key_prefix -> rpm; the
                                           ApiKey model has no JSON metadata
                                           column of its own — capability_scope
                                           is a capability allowlist, not a
                                           place to hang an rpm — so the
                                           override lives in this tenant-scoped
                                           settings map)

Resolution order: api-key override > plan limit > default. Bucket key =
api key id when authenticated by key, else user id, else the resolved
client IP.

Exemption: the auto-block NEVER engages for a resolved client IP that
is inside ``auto_block_exempt_cidrs`` or that is itself a trusted proxy
(``net.trusted_proxy_cidrs``) — an internal-source block (worker
LAN/tailnet, WB/Dashboard server-side calls, the proxy itself,
localhost) would be a whole-fleet outage. 429s still apply to them.
Behind an edge proxy the resolved IP is the real (public) client and
stays blockable.

Per-plan limits are the pre-existing ``plans.api_rate_limit_per_minute``
column (0 = unlimited); this module layers the per-API-key cap on top and
normalises the standard ``RateLimit-*`` response headers.

Block state is a bounded in-memory set (TTL'd timestamps) — a block is a
cheap deny, and in-memory keeps the check synchronous with no extra
Valkey round-trip per request. Admin list/unblock therefore acts on the
API process; with more than one API replica a block on one replica does
not propagate (documented; the audit rows still record the event).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- Setting keys (constants so the registry + this module never spell
# them out twice; registered in services/platform_settings.py). ---
RATE_LIMIT_ENABLED_KEY = "api.rate_limit.enabled"
PER_KEY_LIMIT_KEY = "api.rate_limit.per_key_per_minute"
AUTO_BLOCK_ENABLED_KEY = "api.rate_limit.auto_block_enabled"
BLOCK_AFTER_429_KEY = "api.rate_limit.auto_block_after_429s"
BLOCK_AFTER_LOGIN_FAILURES_KEY = "api.rate_limit.auto_block_after_login_failures"
BLOCK_WINDOW_MIN_KEY = "api.rate_limit.auto_block_window_min"
BLOCK_DURATION_MIN_KEY = "api.rate_limit.auto_block_duration_min"
BLOCK_EXEMPT_CIDRS_KEY = "api.rate_limit.auto_block_exempt_cidrs"

# FX.106 p1a — per-PLAN limits as platform settings (the pre-existing
# `plans.api_rate_limit_per_minute` column stays the per-plan knob; these
# keys give the registry a plan-code map + a floor/default and the
# per-API-key override ceiling). Consumed by
# :func:`resolve_effective_limit` below.
PLAN_LIMITS_KEY = "api.rate_limit.plan_limits"
DEFAULT_RPM_KEY = "api.rate_limit.default_rpm"
API_KEY_OVERRIDE_MAX_RPM_KEY = "api.rate_limit.api_key_override_max_rpm"
API_KEY_OVERRIDES_KEY = "api.rate_limit.api_key_overrides"

RATE_LIMIT_SETTING_KEYS = (RATE_LIMIT_ENABLED_KEY, PER_KEY_LIMIT_KEY,
                           AUTO_BLOCK_ENABLED_KEY, BLOCK_AFTER_429_KEY,
                           BLOCK_AFTER_LOGIN_FAILURES_KEY,
                           BLOCK_WINDOW_MIN_KEY, BLOCK_DURATION_MIN_KEY,
                           BLOCK_EXEMPT_CIDRS_KEY,
                           PLAN_LIMITS_KEY, DEFAULT_RPM_KEY,
                           API_KEY_OVERRIDE_MAX_RPM_KEY,
                           API_KEY_OVERRIDES_KEY)

# Bounded in-process fallback counters (redis down): at most this many
# distinct buckets, evicting the oldest-inserted on overflow.
_FALLBACK_MAX_BUCKETS = 10000
_FALLBACK_BUCKETS: "Dict[str, List[float]]" = {}
_FALLBACK_LOCK = threading.Lock()

# Bounded ring of recent 429 events for the admin "recent limit hits"
# listing (item 3). In-memory + bounded on purpose: a per-429 audit row
# would flood the append-only log under a sustained flood, which is
# exactly the condition this list exists to show.
_RECENT_429_MAX = 200
_recent_429: List[Dict[str, Any]] = []
_recent_429_lock = threading.Lock()


def _remember_429(ip: Optional[str], tenant_id: Optional[int],
                  bucket: str, limit: int) -> None:
    entry = {"timestamp": _now(), "ip": ip, "tenant_id": tenant_id,
             "bucket": bucket, "limit": limit}
    with _recent_429_lock:
        _recent_429.append(entry)
        if len(_recent_429) > _RECENT_429_MAX:
            del _recent_429[:len(_recent_429) - _RECENT_429_MAX]

# ---------------------------------------------------------------------------
# Block state (in-memory, TTL'd). ip -> block expiry (epoch seconds).
# ---------------------------------------------------------------------------
_blocks: Dict[str, float] = {}
_blocks_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _purge_expired_locked() -> None:
    now = _now()
    for ip in [k for k, exp in _blocks.items() if exp <= now]:
        _blocks.pop(ip, None)


def is_ip_blocked(ip: Optional[str]) -> Optional[float]:
    """Return the block expiry (epoch) if `ip` is auto-blocked, else None."""
    if not ip:
        return None
    with _blocks_lock:
        exp = _blocks.get(ip)
        if exp is None:
            return None
        if exp <= _now():
            _blocks.pop(ip, None)
            return None
        return exp


def block_ip(ip: str, *, duration_seconds: int, reason: str) -> float:
    """Record a time-limited block. Returns the expiry timestamp."""
    exp = _now() + max(60, int(duration_seconds))
    with _blocks_lock:
        _purge_expired_locked()
        _blocks[ip] = exp
    return exp


def unblock_ip(ip: str) -> bool:
    """Clear a block. True if a live block was removed."""
    with _blocks_lock:
        return _blocks.pop(ip, None) is not None


def active_blocks() -> List[Dict[str, Any]]:
    """Snapshot of live blocks (admin listing)."""
    now = _now()
    with _blocks_lock:
        return sorted(
            ({"ip": ip, "unblocks_at": exp,
              "remaining_seconds": int(exp - now)}
             for ip, exp in _blocks.items() if exp > now),
            key=lambda row: row["unblocks_at"],
        )


def _fallback_hit(key: str, window_seconds: int) -> int:
    """Bounded in-process sliding-window counter (redis-down fallback)."""
    now = _now()
    cutoff = now - window_seconds
    with _FALLBACK_LOCK:
        bucket = _FALLBACK_BUCKETS.get(key)
        if bucket is None:
            if len(_FALLBACK_BUCKETS) >= _FALLBACK_MAX_BUCKETS:
                oldest = min(_FALLBACK_BUCKETS,
                             key=lambda k: _FALLBACK_BUCKETS[k][-1])
                _FALLBACK_BUCKETS.pop(oldest, None)
            bucket = _FALLBACK_BUCKETS[key] = []
        bucket[:] = [t for t in bucket if t > cutoff]
        bucket.append(now)
        return len(bucket)


# ---------------------------------------------------------------------------
# Settings (never raise — a read failure falls back to the registered
# default so the hot request path cannot wedge, R-083 convention).
# ---------------------------------------------------------------------------

def _rl_defaults() -> Dict[str, Any]:
    """Edition-aware fallback defaults (CE raised, paid unchanged); the
    single source is platform_settings.rate_limit_defaults(). Falls back to
    the paid numbers if that module cannot be imported."""
    try:
        from services import platform_settings as _ps
        return _ps.rate_limit_defaults()
    except Exception:  # noqa: BLE001
        # FIX-CE-UNLIMITED-F: an import failure must not
        # suddenly impose 300/120 on CE. is_ce_edition() has no
        # platform_settings dependency; if IT errors too we fail to PAID.
        try:
            from utils.paid_import import is_ce_edition
            if bool(is_ce_edition()):
                return {"per_key": 0, "default_rpm": 0, "plan_limits": {}}
        except Exception:  # noqa: BLE001
            pass
        return {"per_key": 300, "default_rpm": 120,
                "plan_limits": dict(DEFAULT_PLAN_LIMITS)}


def _ce_no_limits() -> bool:
    """True only on the CE edition (CE has no API rate
    limit). Fails to False (paid) on any probe error, so a broken edition
    probe can never loosen a paid cap. Cheap: one cached sentinel lookup."""
    try:
        from services import platform_settings as _ps
        return bool(_ps._ce_unlimited(False, True))
    except Exception:  # noqa: BLE001
        return False


def _val(key: str, default: Any, db: Any = None,
        tenant_id: Optional[int] = None) -> Any:
    try:
        from services import platform_settings as _ps
        return _ps.get_setting(key, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_guard_settings(db: Any = None,
                       tenant_id: Optional[int] = None) -> Dict[str, Any]:
    """Read + clamp all FX.106 guard settings. Never raises.

    ``tenant_id`` is passed through to the settings registry so a
    tenant-scoped override of any of these keys (including
    ``api.rate_limit.api_key_overrides``) resolves for that tenant."""
    window_min = max(1, _coerce_int(
        _val(BLOCK_WINDOW_MIN_KEY, 15, db, tenant_id), 15))
    duration_min = max(1, _coerce_int(
        _val(BLOCK_DURATION_MIN_KEY, 60, db, tenant_id), 60))
    return {
        "enabled": bool(_val(RATE_LIMIT_ENABLED_KEY, True, db, tenant_id)),
        "per_key_per_minute": max(
            0, _coerce_int(_val(PER_KEY_LIMIT_KEY, _rl_defaults()["per_key"],
                                db, tenant_id), _rl_defaults()["per_key"])),
        "auto_block_enabled": bool(_val(AUTO_BLOCK_ENABLED_KEY, True, db,
                                        tenant_id)),
        "block_after_429s": max(
            2, _coerce_int(_val(BLOCK_AFTER_429_KEY, 25, db, tenant_id), 25)),
        "block_after_login_failures": max(
            3, _coerce_int(_val(BLOCK_AFTER_LOGIN_FAILURES_KEY, 20, db,
                                tenant_id), 20)),
        "window_seconds": window_min * 60,
        "duration_seconds": duration_min * 60,
        "window_min": window_min,
        "duration_min": duration_min,
        "exempt_cidrs": _exempt_networks(db, tenant_id),
        # FX.106 p1a — per-plan map + default + per-key override ceiling.
        "plan_limits": _plan_limits_map(db, tenant_id),
        "default_rpm": max(0, _coerce_int(
            _val(DEFAULT_RPM_KEY, _rl_defaults()["default_rpm"], db,
                 tenant_id), _rl_defaults()["default_rpm"])),
        "api_key_override_max_rpm": max(
            0, _coerce_int(_val(API_KEY_OVERRIDE_MAX_RPM_KEY, 20000, db,
                                tenant_id), 20000)),
        "api_key_overrides": _api_key_overrides_map(db, tenant_id),
    }


# ---------------------------------------------------------------------------
# Internal-source exemption (owner safety follow-up to FX.106): a blocked
# internal IP (Go worker, WB/Dashboard server-side calls, the proxy
# itself, localhost) is a whole-fleet outage, so the auto-block never
# engages for a RESOLVED client IP that is (a) inside
# api.rate_limit.auto_block_exempt_cidrs or (b) itself a trusted proxy
# (net.trusted_proxy_cidrs). Rate-limit 429s still apply to them — only
# the block is suppressed. Behind an edge proxy the resolved IP is the
# real (public) client and stays blockable.
# ---------------------------------------------------------------------------

DEFAULT_EXEMPT_CIDRS = ["127.0.0.0/8", "::1/128", "10.0.0.0/8",
                        "172.16.0.0/12", "192.168.0.0/16",
                        "100.64.0.0/10", "fc00::/7"]


def _exempt_networks(db: Any = None,
                     tenant_id: Optional[int] = None) -> List[Any]:
    """The auto-block-exempt networks (never raises; falls back to the
    built-in internal ranges when the registry/store is unavailable)."""
    import ipaddress
    raw = _val(BLOCK_EXEMPT_CIDRS_KEY, DEFAULT_EXEMPT_CIDRS, db, tenant_id)
    if isinstance(raw, str):
        raw = [p for p in raw.split(",")]
    nets = []
    for part in raw or []:
        try:
            nets.append(ipaddress.ip_network(str(part).strip(),
                                             strict=False))
        except (ValueError, TypeError):
            continue
    return nets


def is_auto_block_exempt(ip: Optional[str], db: Any = None,
                         nets: Optional[List[Any]] = None) -> bool:
    """True when the auto-block must never engage for this (resolved)
    client IP: it is inside ``auto_block_exempt_cidrs`` (loopback,
    RFC1918, tailnet, private IPv6 — Go workers, WB/Dashboard
    server-side calls, switchyard/stacks, in-container traffic), OR it
    is itself a trusted proxy (``net.trusted_proxy_cidrs`` — the reverse
    proxy / nginx in front, which is what the client IP resolves to
    when no external hop is in the chain).

    NOTE: the check is on the RESOLVED client IP, not the direct TCP
    peer — behind an edge proxy the resolved IP is the real (public)
    client and must stay blockable; only when the resolution lands on
    an internal/proxy address is the source exempt. ``nets`` (from an
    already-loaded ``get_guard_settings()``) skips a re-read. Never
    raises."""
    if not ip:
        return False
    try:
        import ipaddress
        from utils.client_ip import is_trusted_peer
        from services.client_ip import trusted_proxy_networks
        if is_trusted_peer(ip, trusted_proxy_networks()):
            return True
        addr = ipaddress.ip_address(ip)
        nets = nets if nets is not None else _exempt_networks(db)
        return any(addr in n for n in nets)
    except Exception:  # noqa: BLE001 — an exemption failure must never
        # wedge the request path; fall through to the normal behaviour.
        logger.warning("FX.106 auto-block exemption check failed "
                       "(non-fatal)", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Audit seam (append-only; a failure never breaks the request path).
# ---------------------------------------------------------------------------

def _audit(db: Any, action: Any, *, ip: Optional[str] = None,
           reason: Optional[str] = None,
           extra: Optional[Dict[str, Any]] = None,
           actor_user_id: Optional[int] = None,
           tenant_id: Optional[int] = None) -> None:
    try:
        import audit
        metadata: Dict[str, Any] = {"fx106": True}
        if ip is not None:
            metadata["ip"] = ip
        if reason is not None:
            metadata["reason"] = str(reason)[:300]
        if extra:
            metadata.update(extra)
        audit.log_security_event(
            db, action,
            user_id=actor_user_id, tenant_id=tenant_id,
            metadata=metadata)
    except Exception:  # noqa: BLE001
        logger.warning("FX.106 audit emit failed (non-fatal)",
                       exc_info=True)


# ---------------------------------------------------------------------------
# Counter helpers
# ---------------------------------------------------------------------------

def _hit(key: str, max_requests: int,
         window_seconds: int) -> tuple:
    """(allowed, remaining, reset) via the shared sliding-window limiter.

    Fails open with a bounded in-process fallback when Valkey is down —
    the window count keeps working so auto-block still engages (R-083:
    availability over throttle, but still observable)."""
    try:
        from utils import rate_limiter
        allowed, remaining, reset = rate_limiter.check_rate_limit(
            key, max_requests, window_seconds=window_seconds,
            raise_on_limit=False)
        return allowed, remaining, reset
    except Exception:  # noqa: BLE001 — limiter itself raising: fallback
        pass
    count = _fallback_hit(key, window_seconds)
    remaining = max(0, max_requests - count)
    allowed = count <= max_requests
    return allowed, remaining, window_seconds


def _resolve_client_ip(request: Any) -> Optional[str]:
    """Real client IP behind a scrubbing provider (item 4 / FX.106 p3a).

    Delegates to the ONE resolver, services/client_ip.py
    :func:`resolve_client_ip`: X-Forwarded-For / X-Real-IP / CF-Connecting-IP
    are honoured only when the direct TCP peer falls inside a trusted-proxy
    range — the union of ``net.trusted_proxy_cidrs`` (the legacy str twin)
    and ``network.trusted_proxy_cidrs`` (the FX.106 p3a ranges setting, the
    same ranges the nginx edge renders to set_real_ip_from). CF-Connecting-IP
    wins when present (Cloudflare sets it to the real client even in front of
    a second proxy hop); from an untrusted peer every forwarded header is
    ignored and the peer address is the client IP (anti-spoof)."""
    from services.client_ip import resolve_client_ip
    return resolve_client_ip(request)


def check_ip_block(request: Any) -> "Optional[Response]":
    """Item 2 (middleware seam): if the client IP is auto-blocked, return
    the block 429 to short-circuit the request; else None. Kept separate
    from :func:`check_api_rate_limits` so the middleware can enforce it
    BEFORE dispatch (a blocked IP must not reach webhook receivers).

    Fail-open on ANY internal error (Valkey/setting-store outage, a bad
    setting value, IP-resolution failure, ...): the middleware must never
    500-DoS the whole API because the block check itself broke. The
    in-memory block set needs no external store, so this path is
    normally error-free, but the R-083 contract (availability over
    throttle) is enforced here explicitly, not by hope."""
    try:
        if not get_guard_settings()["auto_block_enabled"]:
            return None
        client_ip = _resolve_client_ip(request)
        exp = is_ip_blocked(client_ip)
        if exp is None:
            return None
        retry = int(max(1, exp - _now()))
        return _rate_limited_json(
            retry, "This address is temporarily blocked after repeated "
                   "failed attempts or rate-limit hits.")
    except Exception:  # noqa: BLE001 — fail-open (R-083)
        logger.warning("FX.106 check_ip_block FAILED OPEN (non-fatal)",
                       exc_info=True)
        return None


# ---------------------------------------------------------------------------
# 429 responses
# ---------------------------------------------------------------------------

def _scope_for_bucket(bucket: str) -> str:
    """Public scope word for an internal bucket key (FX.106 p1b).

    The bucket key itself (``api_key:7``, ``tenant:42``, ``ip:203.0.113.9``)
    is internal accounting — the client only ever learns WHICH dimension
    throttled it, never the id/key/IP behind the counter."""
    if bucket.startswith("api_key:"):
        return "api_key"
    if bucket.startswith("user:"):
        return "user"
    if bucket.startswith("ip:"):
        return "ip"
    return "tenant"


def _rate_limited_json(retry_after: int, detail: str, *,
                       limit: Optional[int] = None,
                       window_s: int = 60,
                       bucket: str = "") -> "Response":
    """The canonical 429 body + headers (FX.106 p1b).

    ``limit`` is the RESOLVED requests-per-window the client actually hit
    (never 0 on a throttle — 0 means unlimited and would be a lie); when it
    is unknown (an auto-block denial, which is not a throttle) the
    RateLimit-* headers are omitted rather than stamped with a fake 0.
    Nothing internal leaks: no bucket key, no ids, no IPs."""
    from fastapi import Response
    import json
    body = {
        "error": "rate_limited",
        "code": "rate_limited",
        "detail": detail,
        "retry_after": retry_after,
        "retry_after_s": retry_after,
        "window_s": int(window_s),
    }
    headers = {"Retry-After": str(retry_after)}
    if limit is not None and limit > 0:
        body["limit"] = int(limit)
        body["scope"] = _scope_for_bucket(bucket)
        headers["RateLimit-Limit"] = str(int(limit))
        headers["RateLimit-Remaining"] = "0"
        headers["RateLimit-Reset"] = str(max(0, retry_after))
    return Response(
        status_code=429,
        content=json.dumps(body),
        media_type="application/json",
        headers=headers,
    )


def _set_headers(response: "Response", limit: int, remaining: int,
                 reset: int) -> None:
    """Standard RateLimit-* headers (item 1)."""
    response.headers["RateLimit-Limit"] = str(limit)
    response.headers["RateLimit-Remaining"] = str(max(0, remaining))
    response.headers["RateLimit-Reset"] = str(max(0, reset))


# ---------------------------------------------------------------------------
# FX.106 p1a — limit RESOLUTION (api-key override > plan > default)
# ---------------------------------------------------------------------------

# FX.106 p1a — the registry default for api.rate_limit.plan_limits, keyed by
# the LAUNCH plan names (seed_launch_pricing.py — seed_plans.py was removed
# at launch, #15) lower-cased, and matching their seeded
# plans.api_rate_limit_per_minute so the map and the column agree out of the
# box. Plans NOT in the map fall through to the plan column, where 0 means unlimited.
DEFAULT_PLAN_LIMITS: dict = {}
# (the map above is the registry default; CE gets the raised map via _rl_defaults() at read time)


def _int_map(key: str, default: Dict[str, int], db: Any = None,
            tenant_id: Optional[int] = None) -> Dict[str, int]:
    """A {name: positive-int} settings map, never raising. Bad entries are
    dropped rather than repaired (the registry validator already rejects a
    malformed write, so this is the read-side safety net for rows written
    before the validator existed)."""
    raw = _val(key, default, db, tenant_id)
    if not isinstance(raw, dict):
        return dict(default)
    out: Dict[str, int] = {}
    for k, v in raw.items():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv > 0:
            out[str(k).strip().lower()] = iv
    return out


def _plan_limits_map(db: Any = None,
                     tenant_id: Optional[int] = None) -> Dict[str, int]:
    return _int_map(PLAN_LIMITS_KEY, _rl_defaults()["plan_limits"], db,
                    tenant_id)


def _api_key_overrides_map(db: Any = None,
                           tenant_id: Optional[int] = None) -> Dict[str, int]:
    return _int_map(API_KEY_OVERRIDES_KEY, {}, db, tenant_id)


def _plan_code_for_tenant(tenant_id: Optional[int],
                          db: Any = None) -> Optional[str]:
    """The tenant's plan name lower-cased (the plan "code" — ``plans.name``
    is the unique plan identity; there is no separate slug column).

    FX.106 p1a resolves the plan limit from the settings map on EVERY
    request, including the live path where the caller already supplied the
    column value, so this lookup can no longer be skipped. It is one cheap
    indexed query on the request's own pooled session; deliberately NOT
    cached in-process: a per-tenant cache would serve a stale plan to a
    tenant whose subscription just changed (plan downgrades/upgrades and
    the admin plan-limits edits must both take effect within the settings
    registry's own ~30s window)."""
    if tenant_id is None:
        return None
    session = db
    own = session is None
    if own:
        try:
            from database import SessionLocal
            session = SessionLocal()
        except Exception:  # noqa: BLE001 — fail-open, no plan code
            return None
    try:
        import models
        sub = session.query(models.Subscription).filter(
            models.Subscription.tenant_id == tenant_id).first()
        if sub is not None and sub.plan is not None:
            return str(sub.plan.name or "").strip().lower() or None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own and session is not None:
            session.close()
    return None


def resolve_effective_limit(*, api_key: Any = None,
                            tenant_id: Optional[int] = None,
                            user_id: Optional[int] = None,
                            plan_limit: Optional[int] = None,
                            client_ip: Optional[str] = None,
                            db: Any = None,
                            settings: Optional[Dict[str, Any]] = None
                            ) -> Dict[str, Any]:
    """Resolve the effective requests-per-minute for one request.

    Resolution order (FX.106 p1a):
      1. per-API-key override — ``api.rate_limit.api_key_overrides``
         (key_prefix -> rpm), capped by ``api_key_override_max_rpm``;
      2. the plan limit — ``api.rate_limit.plan_limits`` (plan name -> rpm)
         for the request's plan, which is the settings-driven knob the task
         asks for and therefore WINS over the caller's ``plan_limit`` when
         the map names that plan; the caller's ``plan_limit`` (the
         pre-existing ``plans.api_rate_limit_per_minute`` column) is the
         fallback for plans the map does not name, where 0 still means
         unlimited;
      3. ``api.rate_limit.default_rpm`` when no plan resolves at all.

    Semantics kept deliberately compatible with the pre-existing knob: a
    plan the map does not name with ``plan_limit=0`` stays unlimited and is
    NOT replaced by the default; ``plan_limit=None`` with no map entry
    falls through to the default.

    Returns {limit, bucket, source, plan_code, settings}. ``limit`` 0 =
    unlimited. Bucket key = api key id when authenticated by key, else
    user id, else the resolved client IP (the same IP the guard already
    uses for the auto-block counters).
    """
    settings = settings or get_guard_settings(db=db, tenant_id=tenant_id)

    override = 0
    if api_key is not None:
        prefix = str(getattr(api_key, "key_prefix", "") or "").strip().lower()
        if prefix:
            # Prefer the caller's settings snapshot (get_guard_settings
            # already read the registry once for this request); fall back
            # to a fresh read only when a partial dict was supplied.
            overrides = settings.get("api_key_overrides")
            if not isinstance(overrides, dict):
                overrides = _api_key_overrides_map(db, tenant_id)
            override = overrides.get(prefix, 0)
        cap = settings.get("api_key_override_max_rpm", 20000)
        if override and cap and override > cap:
            override = cap

    plan_code = _plan_code_for_tenant(tenant_id, db)
    plan_from_map = (settings.get("plan_limits", {}).get(plan_code, 0)
                     if plan_code else 0)

    if override:
        limit, source = override, "api_key_override"
    elif plan_from_map:
        # settings-driven plan limit (FX.106 p1a) — wins over the column so
        # an admin editing api.rate_limit.plan_limits moves the live cap
        # without touching plan rows.
        limit, source = plan_from_map, "plan"
    elif plan_limit is not None:
        # the pre-existing plans.api_rate_limit_per_minute column, for plans
        # plans the map does not name; 0 = unlimited.
        limit = int(plan_limit)
        if limit > 0 and _ce_no_limits():
            limit = 0  # CE: no API rate limit (FIX-CE-UNLIMITED-B2)
        source = "plan" if limit > 0 else "unlimited"
    else:
        limit, source = settings.get("default_rpm", _rl_defaults()["default_rpm"]), "default"

    if api_key is not None and getattr(api_key, "id", None) is not None:
        bucket = f"api_key:{getattr(api_key, 'id', 0)}"
    elif user_id is not None:
        bucket = f"user:{user_id}"
    else:
        bucket = f"ip:{client_ip}" if client_ip else ""

    # The plan bucket keeps the pre-existing ``tenant:{id}`` key when a
    # tenant is known (so the plan counter is shared with the pre-existing
    # per-plan limiter); otherwise the identity bucket above is used.
    plan_bucket = f"tenant:{tenant_id}" if tenant_id is not None else bucket

    return {"limit": limit, "bucket": bucket, "plan_bucket": plan_bucket,
            "source": source, "plan_code": plan_code, "settings": settings}


# ---------------------------------------------------------------------------
# Enforcement (item 1)
# ---------------------------------------------------------------------------

def check_api_rate_limits(request: Any, *, tenant_id: Optional[int],
                          api_key: Any = None,
                          plan_limit: Optional[int] = 0,
                          user_id: Optional[int] = None,
                          db: Any = None) -> "Optional[Response]":
    """Per-plan + per-API-key API rate limits. Returns a 429 Response to
    short-circuit the request, or None (and sets the standard
    RateLimit-* headers on ``request.state.fx106_response_headers`` so
    the middleware can stamp them after ``call_next``).

    - ``plan_limit`` is ``plans.api_rate_limit_per_minute`` (0 =
      unlimited, the pre-existing knob). FX.106 p1a makes the settings map
      authoritative: ``api.rate_limit.plan_limits`` (plan name -> rpm) WINS
      over this column value when the map names the request's plan — that is
      what lets an admin retune a plan through the admin settings page
      without editing plan rows. The column is the fallback for plans the
      map does not name, where 0 still means unlimited, and ``default_rpm`` applies when no plan resolves at all.
    - ``api_key`` (when the request came in with an API key) adds a
      per-key bucket on top of the plan bucket: the effective limit is
      ``min(plan_limit, per_key_per_minute)``. A per-key override from
      ``api.rate_limit.api_key_overrides`` (capped by
      ``api_key_override_max_rpm``) replaces the plan bucket's limit.
    """
    settings = get_guard_settings(db=db, tenant_id=tenant_id)
    if not settings["enabled"]:
        return None
    client_ip = _resolve_client_ip(request)

    # Auto-blocked IPs are denied before any counting (item 2): every
    # request from a blocked IP is a 429 with the block's Retry-After.
    if settings["auto_block_enabled"]:
        exp = is_ip_blocked(client_ip)
        if exp is not None:
            retry = int(max(0, exp - _now()))
            resp = _rate_limited_json(
                retry, "This address is temporarily blocked after "
                       "repeated failed attempts or rate-limit hits.")
            return resp

    resolved = resolve_effective_limit(api_key=api_key, tenant_id=tenant_id,
                                       user_id=user_id, plan_limit=plan_limit,
                                       client_ip=client_ip, db=db,
                                       settings=settings)

    # The RESOLVED plan-level limit (0 = unlimited), exposed for the legacy
    # X-RateLimit-Limit header in main.py: that header used to read the
    # plans.api_rate_limit_per_minute column through utils.rate_limiter,
    # which would now disagree with the settings-driven cap this function
    # actually enforces.
    try:
        request.state.fx106_plan_limit = resolved["limit"]
    except Exception:  # noqa: BLE001 — header bookkeeping only
        pass

    buckets: List[tuple] = []  # (redis_key, limit, window)
    if resolved["plan_bucket"] and resolved["limit"] > 0:
        buckets.append((resolved["plan_bucket"], resolved["limit"], 60))
    per_key = settings["per_key_per_minute"]
    if api_key is not None and per_key and per_key > 0:
        buckets.append((f"api_key:{getattr(api_key, 'id', 0)}", per_key, 60))
    if not buckets:
        return None  # unlimited (plan 0 / no key / disabled)

    response_headers = {}
    for key, limit, window in buckets:
        allowed, remaining, reset = _hit(key, limit, window)
        # tightest bucket wins the headers
        if ("RateLimit-Limit" not in response_headers
                or limit < int(response_headers["RateLimit-Limit"])):
            response_headers = {
                "RateLimit-Limit": str(limit),
                "RateLimit-Remaining": str(max(0, remaining)),
                "RateLimit-Reset": str(max(0, reset)),
            }
        if not allowed:
            retry = int(max(1, reset))
            _remember_429(client_ip, tenant_id, key, limit)
            _note_limit_hit(db=db, client_ip=client_ip,
                            tenant_id=tenant_id, key=key, settings=settings)
            return _rate_limited_json(
                retry, f"slow down — rate limit exceeded "
                       f"({limit} requests per {window}s).",
                limit=limit, window_s=window, bucket=key)
    try:
        request.state.fx106_response_headers = response_headers
    except Exception:  # noqa: BLE001
        pass
    return None


def note_login_failure(request: Any, db: Any = None) -> None:
    """Item 2: count a failed login for the client IP; trip the block at
    ``auto_block_after_login_failures`` within the window (audited)."""
    settings = get_guard_settings(db=db)
    if not settings["auto_block_enabled"]:
        return
    client_ip = _resolve_client_ip(request)
    if not client_ip:
        return
    if is_ip_blocked(client_ip) is not None:
        return  # already blocked — no re-audit per attempt
    if is_auto_block_exempt(client_ip, db=db,
                            nets=settings.get("exempt_cidrs")):
        return  # internal source: count, but never block (429s still
                # apply at the throttle layer)
    allowed, _remaining, _reset = _hit(
        f"fx106_login_fail:{client_ip}",
        settings["block_after_login_failures"],
        settings["window_seconds"])
    if not allowed:
        _trip_block(db, client_ip, reason="repeated_failed_logins",
                    settings=settings, tenant_id=None)


def _note_limit_hit(db: Any, client_ip: Optional[str], tenant_id: Optional[int],
                    key: str, settings: Dict[str, Any]) -> None:
    """Count a 429 for the client IP; trip the block at
    ``auto_block_after_429s`` within the window (audited)."""
    if not settings["auto_block_enabled"] or not client_ip:
        return
    if is_ip_blocked(client_ip) is not None:
        return
    if is_auto_block_exempt(client_ip, db=db,
                            nets=settings.get("exempt_cidrs")):
        return  # internal source: 429s apply, but never the block
    allowed, _remaining, _reset = _hit(
        f"fx106_429:{client_ip}",
        settings["block_after_429s"],
        settings["window_seconds"])
    if not allowed:
        _trip_block(db, client_ip, reason="repeated_rate_limit_hits",
                    settings=settings, tenant_id=tenant_id)


def _trip_block(db: Any, ip: str, *, reason: str,
                settings: Dict[str, Any],
                tenant_id: Optional[int] = None) -> None:
    """Trip a time-limited auto-block (audited). Last-line gate: an IP
    inside the exempt CIDRs, or a trusted-proxy IP, is NEVER blocked —
    even if a stale counter tripped after the setting changed. The
    counter is not consumed (harmless; the window slides on)."""
    if is_auto_block_exempt(ip, db=db,
                            nets=settings.get("exempt_cidrs")):
        logger.warning(
            "FX.106 auto-block SUPPRESSED (exempt internal source): "
            "ip=%s reason=%s", ip, reason)
        return
    exp = block_ip(ip, duration_seconds=settings["duration_seconds"],
                   reason=reason)
    logger.warning(
        "FX.106 auto-block: ip=%s reason=%s for %ss",
        ip, reason, settings["duration_seconds"])
    session = db
    own = db is None
    if own:
        try:
            from database import SessionLocal
            session = SessionLocal()
        except Exception:  # noqa: BLE001
            session = None
    try:
        if session is not None:
            import models
            _audit(session, models.AuditAction.API_AUTO_BLOCK,
                   ip=ip, reason=reason,
                   extra={"unblocks_at": exp,
                          "duration_seconds":
                              settings["duration_seconds"]},
                   tenant_id=tenant_id)
    finally:
        if own and session is not None:
            session.close()


def record_unblock_audit(db: Any, *, ip: str, actor_user_id: Optional[int],
                         tenant_id: Optional[int] = None,
                         reason: str = "") -> None:
    """Item 3: the admin unblock audit row."""
    try:
        import models
        _audit(db, models.AuditAction.API_AUTO_UNBLOCK,
               ip=ip, reason=reason,
               actor_user_id=actor_user_id, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001
        pass


def recent_limit_hits(db: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """Item 3: recent limit activity for the admin listing.

    Two sources merged, newest first:
      - block / unblock AUDIT rows (append-only, the compliance trail);
      - the bounded in-memory ring of individual 429 events (a per-429
        audit row would flood the log under a sustained flood — the ring
        is the operational view, the audit rows are the legal one)."""
    out: List[Dict[str, Any]] = []
    with _recent_429_lock:
        for e in list(_recent_429):
            out.append({
                "kind": "rate_limit_hit",
                "timestamp": e["timestamp"],
                "ip_address": e.get("ip"),
                "tenant_id": e.get("tenant_id"),
                "detail": f"429 on bucket {e.get('bucket')} "
                          f"(limit {e.get('limit')}/60s)",
            })
    try:
        import models
        from sqlalchemy import desc
        rows = (
            db.query(models.PermissionAuditLog)
            .filter(models.PermissionAuditLog.action.in_([
                models.AuditAction.API_AUTO_BLOCK,
                models.AuditAction.API_AUTO_UNBLOCK,
            ]))
            .order_by(desc(models.PermissionAuditLog.timestamp))
            .limit(max(1, limit // 2))
            .all()
        )
        for r in rows:
            kind = "auto_block"
            if r.action is not None and r.action is \
                    models.AuditAction.API_AUTO_UNBLOCK:
                kind = "auto_unblock"
            out.append({
                "kind": kind,
                "timestamp": r.timestamp.timestamp()
                             if r.timestamp else None,
                "ip_address": r.ip_address,
                "tenant_id": r.tenant_id or None,
                "detail": (r.audit_metadata or {}).get("reason", ""),
            })
    except Exception:  # noqa: BLE001 — the audit read is best-effort
        logger.warning("FX.106 recent_limit_hits audit read failed "
                       "(non-fatal)", exc_info=True)
    out.sort(key=lambda row: row.get("timestamp") or 0.0, reverse=True)
    return out[:limit]
