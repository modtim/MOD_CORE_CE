"""
Redis-backed rate limiter for API endpoints.

S6: Rate limiting on auth/token endpoints.
#99: Per-tenant rate limiting for SaaS (plan-aware).

Uses sliding window counter stored in Redis.
"""

import os
import time
import logging
from typing import Optional, Tuple
from fastapi import HTTPException, Request, Response, status
from utils.http_errors import ApiError

logger = logging.getLogger(__name__)

_redis = None
# GSEC R-083 — the redis-down path is deliberately fail-open (availability
# over throttle), but it must be observable: log the warning at most once
# per minute instead of once per request (which flooded the API log and
# buried everything else during a Valkey outage).
_last_redis_down_warning = 0.0
_REDIS_DOWN_WARN_INTERVAL = 60.0


def _get_redis():
    """Lazy Redis connection."""
    global _redis, _last_redis_down_warning
    if _redis is None:
        try:
            import redis
            _redis = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/2"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _redis.ping()
        except Exception as e:
            now = time.monotonic()
            if now - _last_redis_down_warning >= _REDIS_DOWN_WARN_INTERVAL:
                logger.warning(
                    "Redis rate limiter unavailable — rate limiting is "
                    "FAIL-OPEN until redis recovers: %s", e)
                _last_redis_down_warning = now
            _redis = None
    return _redis


def check_rate_limit(
    key: str,
    max_requests: int,
    window_seconds: int = 60,
    raise_on_limit: bool = True,
) -> Tuple[bool, int, int]:
    """
    Check if a rate limit key has exceeded max_requests in the window.

    Returns:
        Tuple of (allowed, remaining, reset_seconds)
    """
    r = _get_redis()
    if r is None:
        return True, max_requests, window_seconds

    try:
        now = time.time()
        pipe = r.pipeline()
        redis_key = f"ratelimit:{key}"

        pipe.zremrangebyscore(redis_key, 0, now - window_seconds)
        pipe.zadd(redis_key, {str(now): now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, window_seconds + 10)

        results = pipe.execute()
        request_count = results[2]
        remaining = max(0, max_requests - request_count)

        if request_count > max_requests:
            if raise_on_limit:
                raise ApiError(
                    429,
                    "rate_limited",
                    "Rate limit exceeded. Please try again later.",
                    retry_after=int(window_seconds),
                    headers={"Retry-After": str(window_seconds)},
                )
            return False, 0, window_seconds

        return True, remaining, window_seconds

    except HTTPException:
        raise
    except Exception as e:
        # GSEC R-083 — fail-open, but warn at most once per minute (a live
        # redis error would otherwise log once per request on every
        # rate-limited endpoint).
        global _last_redis_down_warning
        now = time.monotonic()
        if now - _last_redis_down_warning >= _REDIS_DOWN_WARN_INTERVAL:
            logger.warning("Rate limit check failed (fail-open): %s", e)
            _last_redis_down_warning = now
        return True, max_requests, window_seconds


def _rate_limit_client_ip(request: Request) -> str:
    """GSEC R-011 / FX.106 p3a — real client IP for rate-limit keys.

    Delegates to the ONE resolver, services/client_ip.py
    :func:`resolve_client_ip`: XFF / X-Real-IP / CF-Connecting-IP are
    honoured only when the direct TCP peer falls inside a trusted-proxy
    range — the union of ``net.trusted_proxy_cidrs`` and the FX.106 p3a
    ranges setting ``network.trusted_proxy_cidrs`` (the ranges the nginx
    edge renders to set_real_ip_from). See utils/client_ip for the trust
    model; the left-most XFF hop is attacker-controllable and never used."""
    # 1788b CE carve: services/client_ip is paid; the KEPT utils/client_ip
    # resolver gives the same peer/trusted-proxy answer with no paid policy, so
    # rate-limit keys stay per-IP in CE instead of collapsing everyone to
    # "unknown" (which would make the limiter global, i.e. fail-open per user).
    from utils.paid_import import optional_paid
    _client_ip = optional_paid("services.client_ip")
    if _client_ip is not None:
        return _client_ip.resolve_client_ip(request) or "unknown"
    from utils.client_ip import trusted_client_ip
    return trusted_client_ip(request) or "unknown"


def rate_limit_ip(request: Request, max_requests: int = 30, window_seconds: int = 60):
    """Rate limit by client IP address."""
    check_rate_limit(f"ip:{_rate_limit_client_ip(request)}", max_requests, window_seconds)


def rate_limit_auth(request: Request, scope: Optional[str] = None):
    """Rate limit auth endpoints more aggressively (10 req/min per IP).

    AUTH-5 (auth audit) — every auth route used to share ONE `auth:<ip>`
    bucket, so a flood on any of them throttled all the others (a password-
    strength spray could lock out refresh-token, and vice versa). `scope`
    splits the cap per route family while keeping the SAME limit: default
    None keeps today's shared key byte-identical for every other caller; a
    scope name yields `auth:<scope>:<ip>`. No new setting.
    """
    prefix = f"auth:{scope}:" if scope else "auth:"
    check_rate_limit(
        f"{prefix}{_rate_limit_client_ip(request)}",
        max_requests=10,
        window_seconds=60,
    )


def rate_limit_tenant(tenant_id: int, max_requests: int = 100, window_seconds: int = 60):
    """Rate limit per tenant (for SaaS multi-tenant isolation)."""
    check_rate_limit(f"tenant:{tenant_id}", max_requests, window_seconds)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


_ASSISTANT_CHAT_BUCKET = "assistant_chat"


def check_assistant_chat_rate_limit(
    tenant_id: int,
    user_id: Optional[int] = None,
    *,
    max_requests: Optional[int] = None,
    window_seconds: Optional[int] = None,
    raise_on_limit: bool = True,
) -> Tuple[bool, int]:
    """GE.MAA.1 (mod-assistant-agent §5.6) — a rate-limit bucket dedicated to
    POST /assistant/chat only.

    This is deliberately separate from BOTH of the other two caps that
    already exist near the assistant:
      - services/llm_rate_limit.py's ASSISTANT_TOOL concurrency+window
        ceiling, which bounds the shared model SERVER (a scarce resource
        shared by every LLM tool), not any one HTTP route.
      - check_tenant_rate_limit()'s plan-driven api_rate_limit_per_minute,
        which is the tenant's global API ceiling across every endpoint.
    Neither of those can be tuned for chat alone without also moving the
    knob for every other tool/route sharing them. This bucket exists so
    `/assistant/chat` has its own dial.

    Keyed per (tenant, user) — one chatty teammate must not lock out the
    rest of their tenant — and namespaced under `assistant_chat:` so it
    never collides with any other bucket's key (`wizard_test_run:`,
    `tenant:`, `ip:`, `auth:`).

    Limits come from ASSISTANT_CHAT_RATE_LIMIT_MAX / _WINDOW_S env vars
    (defaults 30 requests / 60s), the same env-override convention as
    every other cap in this module.

    GE.MAA.2 (mod-assistant-agent §9.7) — a platform admin can now pin a
    higher-precedence override via `PUT /platform/assistant/config`
    (`services/assistant_platform_config_service.py`), stored in the same
    Vault path as the assistant tool's other settings. Resolution order,
    highest wins: explicit `max_requests`/`window_seconds` args (tests /
    programmatic callers) > platform-admin override > env var default. A
    Vault hiccup falls through to the env default, never raises.
    """
    max_n = max_requests
    window = window_seconds
    if max_n is None or window is None:
        try:
            from services import assistant_platform_config_service as _platform_cfg
            override = _platform_cfg.get_config()
        except Exception:
            override = {}
        if max_n is None:
            max_n = override.get("chat_rate_limit_max")
            if max_n is None:
                # FIX-CE-FEATURE-THROTTLES-RAISE (owner 11-10 10:50 PM CT:
                # "raise them for CE") — the CE carve gets the x100 feature
                # throttle (services/platform_settings.py); an explicit
                # platform-admin override stays exactly as pinned.
                from services.platform_settings import feature_throttle
                max_n = feature_throttle(
                    _env_int("ASSISTANT_CHAT_RATE_LIMIT_MAX", 30))
        if window is None:
            window = override.get("chat_rate_limit_window_s")
            if window is None:
                window = _env_int("ASSISTANT_CHAT_RATE_LIMIT_WINDOW_S", 60)
    key = f"{_ASSISTANT_CHAT_BUCKET}:{tenant_id}:{user_id if user_id is not None else 'x'}"
    allowed, remaining, reset = check_rate_limit(
        key, max_requests=max_n, window_seconds=window, raise_on_limit=False,
    )
    if not allowed and raise_on_limit:
        raise ApiError(
            429,
            "rate_limited",
            (
                f"Assistant chat rate limit exceeded ({max_n} messages per "
                f"{window}s). Try again shortly."
            ),
            retry_after=int(window),
            headers={"Retry-After": str(window)},
        )
    return allowed, remaining


def check_wizard_test_run_rate_limit(
    tenant_id: int,
    *,
    max_per_hour: int = 100,
    raise_on_limit: bool = True,
) -> Tuple[bool, int]:
    """workflow-agentic-execution-loop §1.10 + Section F §10.3 —
    per-tenant rate limit on wizard sandbox test-run dispatches.
    Default 100/hour, configurable per-tenant via future
    `tenants.wizard_test_run_rate_per_hour` column.

    On 429: emits `wizard_test_run_rate_limit_hits_total{tenant_id}`
    counter so dashboards can see which tenants are over-limit.

    Returns (allowed, remaining). When `raise_on_limit=True` (default),
    raises HTTPException 429 instead of returning False.
    """
    # FIX-CE-FEATURE-THROTTLES-RAISE (owner 11-10 10:50 PM CT: "raise them
    # for CE") — CE only: x100 the per-hour budget (the 429 detail below
    # quotes the raised number so the client never sees a stale cap).
    from services.platform_settings import feature_throttle
    max_per_hour = feature_throttle(max_per_hour)
    allowed, remaining, _reset = check_rate_limit(
        f"wizard_test_run:{tenant_id}",
        max_requests=max_per_hour,
        window_seconds=3600,
        raise_on_limit=False,
    )
    if not allowed:
        try:
            from services import wizard_metrics
            wizard_metrics.record_rate_limit_hit(tenant_id)
        except Exception:
            pass
        if raise_on_limit:
            raise ApiError(
                429,
                "rate_limited",
                (
                    "Wizard test-run rate limit exceeded "
                    f"({max_per_hour}/hour). Try again in an hour."
                ),
                retry_after=3600,
                headers={"Retry-After": "3600"},
            )
    return allowed, remaining


# --- Plan-aware rate limit cache ---
_plan_limit_cache = {}
_PLAN_CACHE_TTL = 300  # 5 minutes


def _plan_limit_fallback() -> int:
    """Limit when no plan resolves: 200 paid, 0 (unlimited) on the CE edition."""
    try:
        from services.platform_settings import rate_limit_defaults
        return int(rate_limit_defaults()["no_subscription"])
    except Exception:  # noqa: BLE001
        return 200


def _ce_raise_plan_limit(limit: int) -> int:
    """CE only: the plan column never throttles (CE
    has no API rate limit), so any column value resolves to 0 = unlimited.
    Paid is returned unchanged; an erroring edition probe is paid."""
    if not limit:
        return limit
    try:
        from utils.paid_import import is_ce_edition
        if is_ce_edition():
            return 0
    except Exception:  # noqa: BLE001
        pass
    return limit


def _get_plan_rate_limit(tenant_id: int) -> int:
    """Look up rate limit for a tenant's plan. Cached for 5 minutes in Redis."""
    r = _get_redis()
    if r is None:
        return _plan_limit_fallback()  # Default if Redis unavailable

    cache_key = f"plan_limit:{tenant_id}"
    try:
        cached = r.get(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass

    try:
        from database import SessionLocal
        db = SessionLocal()
        try:
            import models
            sub = db.query(models.Subscription).filter(
                models.Subscription.tenant_id == tenant_id,
            ).first()
            if sub and sub.plan:
                limit = _ce_raise_plan_limit(
                    sub.plan.api_rate_limit_per_minute or 0)
            else:
                limit = _plan_limit_fallback()
        finally:
            db.close()

        try:
            r.setex(cache_key, _PLAN_CACHE_TTL, str(limit))
        except Exception:
            pass

        return limit
    except Exception as e:
        logger.debug("Could not look up plan rate limit: %s", e)
        return _plan_limit_fallback()


def check_tenant_rate_limit(request: Request, tenant_id: int, response: Response = None):
    """
    Plan-aware tenant rate limiting.

    Looks up the tenant's plan rate limit (cached in Redis for 5 min),
    checks the sliding window, and sets rate limit response headers.
    """
    limit = _get_plan_rate_limit(tenant_id)

    if limit == 0:
        if response:
            response.headers["X-RateLimit-Limit"] = "unlimited"
        return

    allowed, remaining, reset = check_rate_limit(
        f"tenant:{tenant_id}",
        max_requests=limit,
        window_seconds=60,
        raise_on_limit=True,
    )

    if response:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset)
