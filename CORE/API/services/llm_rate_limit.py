"""Per-tenant bounds on the shared model server.

WHY THIS IS NOT A SPEND CAP. All five tools — wizard, adapter, assistant,
support, repair — talk to ONE model server, and its `concurrent_users` budget is
finite per preset (16 on compact, 48 on balanced). An unbounded tenant therefore
does not merely consume its own money; it occupies slots other tenants need, and
a chat turn can queue somebody's node build behind it. The scarce resource is
slots, so that is what this bounds.

TWO CAPS, BECAUSE THE FAILURE MODES DIFFER. A burst of parallel requests
exhausts slots immediately. A steady loop exhausts the day. A single
calls-per-day cap permits the burst; a concurrency cap alone permits the loop.

WHY REDIS AND NOT PROCESS MEMORY. The API runs several uvicorn workers
(entrypoint.sh: `--workers "$API_WORKERS"`, sized to the machine). An in-process
counter would give each tenant `cap x N_workers` in practice, and the number
would drift with hardware. The counters live in Redis so every worker sees one
shared view.

FAIL OPEN, DELIBERATELY — BUT LOUDLY. If Redis is unreachable this module allows
the call. A rate limiter that takes the product down when its own bookkeeping
store hiccups has converted a soft problem into an outage; the failure it exists
to prevent, contention, is strictly less bad than that.

The danger of failing open is that it is SILENT: limits appear configured, and
nothing is actually enforced, possibly for weeks. So every fail-open is counted
and alerted through the same logger + audit-event path pool_alerts uses, with a
cooldown so a Redis outage produces one alert rather than one per request.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Redis key namespace. `conc` holds a sorted set of in-flight call ids scored by
# start time; `win` is a plain counter with a TTL.
_CONC_KEY = "mod:llm:rl:conc:{tenant}"
_WIN_KEY = "mod:llm:rl:win:{tenant}:{tool_class}"

# A HELD SLOT IS A LEASE, NOT A TIMESTAMP.
#
# The first version scored each in-flight call by its START time and swept
# anything older than a fixed age. That judged how LONG a call had been running,
# not whether it was still running — so a legitimately slow call had its slot
# taken away while it was still using it, and the tenant could then exceed its
# own concurrency cap. That is worse than the leak it was written to prevent,
# because it fires on healthy traffic rather than on crashes. Measured evidence
# it was wrong: a wizard-class call took over 20 minutes on the CPU tier and had
# not finished, which a 15-minute sweep would have reclaimed mid-flight.
#
# Now the score is LAST HEARTBEAT. A live holder refreshes it on a timer; a dead
# one stops. Staleness therefore means "nobody is tending this", which is the
# thing we actually wanted to detect, and it is detected in ~90s rather than 15
# minutes. A call may now run for hours and keep its slot for exactly as long as
# it is genuinely alive.
SLOT_HEARTBEAT_SECONDS = 30

# Three missed heartbeats. Two would make a single GC pause or a slow Redis
# round-trip look like a death.
STALE_SLOT_SECONDS = SLOT_HEARTBEAT_SECONDS * 3

# Tools whose prompts are large and whose calls are billable. The assistant is
# deliberately NOT here: it is free, and it carries its own ceiling.
ASSISTANT_TOOL = "assistant"


_client_lock = threading.Lock()
_client_cached = None
_client_url: Optional[str] = None


def _client():
    """One shared client per process, not one per operation.

    `redis.from_url` builds a fresh connection POOL each time it is called.
    This module calls into Redis on every acquire, every release, and on a
    30-second timer for every in-flight call — so constructing a client per
    operation would churn pools and sockets in proportion to load, which is
    exactly backwards for a component whose job is to behave well under load.

    Cached on the resolved URL so a changed REDIS_URL still takes effect, and
    still a plain module-level function so tests can monkeypatch it wholesale.
    """
    global _client_cached, _client_url
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    if _client_cached is not None and _client_url == url:
        return _client_cached
    with _client_lock:
        if _client_cached is None or _client_url != url:
            import redis  # local import — pytest can pre-mock the module
            _client_cached = redis.from_url(
                url, socket_connect_timeout=2, socket_timeout=2)
            _client_url = url
    return _client_cached


# ---------------------------------------------------------------------------
# Settings resolution: tenant override -> PLAN -> platform row -> environment
# ---------------------------------------------------------------------------
#
# VOLUME LIVES ON THE PLAN. Calls-per-window is "what the customer bought", so
# it sits on `plans` next to credits_per_month and api_rate_limit_per_minute
# rather than in a second, parallel limiter. Running two independent volume
# caps means an admin who gets refused must work out which one refused them,
# and a plan upgrade would raise one ceiling but not the other.
#
# CONCURRENCY IS NOT PRIMARILY A PLAN CONCEPT. It protects the shared model
# server from a burst regardless of what anyone bought, so it defaults from the
# platform row — though a plan may raise it.
#
# The platform row remains the fallback for deployments with no plans at all
# (on-prem), and NULL on a tenant column still means INHERIT, never zero.


# A plan value of 0 means unlimited, matching api_rate_limit_per_minute. Rather
# than special-case "unlimited" through every comparison, it resolves to a
# number no real tenant reaches.
UNLIMITED_CALLS = 1_000_000_000


@dataclass(frozen=True)
class Limits:
    enabled: bool
    concurrent_max: int
    window_seconds: int
    window_max_calls: int
    assistant_window_max_calls: int


# FIX-CE-UNLIMITED-E (CE has NO quantity caps). The two
# window volumes are registered in the platform-settings registry
# (`llm.window_max_calls` / `llm.assistant_window_max_calls`, services/
# platform_settings.py) through the shared `_ce_unlimited` helper, so the
# DEFAULT is 0 = UNLIMITED on CE and byte-for-byte the old number on Paid.
# The LLM_RATE_LIMIT_* env vars stay as an override (they are not new
# behaviour knobs — they existed before this change).
WINDOW_CALLS_DEFAULT = 1200
ASSISTANT_WINDOW_CALLS_DEFAULT = 600
WINDOW_CALLS_KEY = "llm.window_max_calls"
ASSISTANT_WINDOW_CALLS_KEY = "llm.assistant_window_max_calls"


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, "").strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _registered_window_default(key: str, paid_default: int, db=None) -> int:
    """The registry-resolved default for one window volume.

    With a session available the registry key is read through the normal
    resolution chain (platform row > env fallback > default), so an operator
    can raise/lower it in the settings layer; without one only the CE-aware
    default is evaluated (never a DB of its own). Any failure falls back to
    the PAID number — fail-closed, a broken settings layer must not loosen a
    cap.

    The registry's UNLIMITED sentinel is 0 (same as every other CE-unlimited
    key); this module's own convention is a number no tenant reaches
    (:data:`UNLIMITED_CALLS`), so 0 is translated here. Only the registry
    path is translated — a `platform_llm_rate_limits` / `tenant_llm_rate_limits`
    DB row keeps its pre-existing meaning byte-for-byte.
    """
    try:
        from services import platform_settings as psr  # local import: no cycle
        value = (psr.get_setting(key, db=db) if db is not None
                 else psr._ce_unlimited(paid_default))  # noqa: SLF001
        value = int(value)
    except Exception:  # noqa: BLE001 — fail-closed to the paid default
        logger.debug("llm rate limit: registry default unreadable", exc_info=True)
        return paid_default
    return UNLIMITED_CALLS if value == 0 else value


def _env_defaults(db=None) -> Limits:
    # No p95 data will ever exist pre-launch (the metering table is empty until
    # real traffic runs), so these are chosen as a RUNAWAY/ABUSE safety net, not
    # a usage cap — credits are the real economic control. They are set generous
    # enough that a normal multi-user tenant never touches them; only a stuck
    # tool-loop or scripted abuse does. Per-tenant, per hour. The limiter
    # fails OPEN (see _fail_open), so turning it on cannot take the product down.
    # Override any of these per deployment via the LLM_RATE_LIMIT_* env vars, or
    # per plan via plans.llm_calls_per_hour.
    return Limits(
        enabled=os.getenv("LLM_RATE_LIMIT_ENABLED", "true").strip().lower() == "true",
        concurrent_max=_env_int("LLM_RATE_LIMIT_CONCURRENT", 6),
        window_seconds=_env_int("LLM_RATE_LIMIT_WINDOW_S", 3600),
        window_max_calls=_env_int(
            "LLM_RATE_LIMIT_WINDOW_CALLS",
            _registered_window_default(WINDOW_CALLS_KEY,
                                       WINDOW_CALLS_DEFAULT, db=db)),
        assistant_window_max_calls=_env_int(
            "LLM_RATE_LIMIT_ASSISTANT_CALLS",
            _registered_window_default(ASSISTANT_WINDOW_CALLS_KEY,
                                       ASSISTANT_WINDOW_CALLS_DEFAULT, db=db)),
    )


def _platform_row(db):
    try:
        import models
        return db.query(models.PlatformLLMRateLimits).filter(
            models.PlatformLLMRateLimits.id == 1).first()
    except Exception:
        logger.debug("llm rate limit: platform row unavailable", exc_info=True)
        return None


def _plan_for(db, tenant_id: Optional[int]):
    """The tenant's active plan, or None. Absent on on-prem and for tenants
    with no subscription, both of which must fall through rather than fail."""
    if tenant_id is None:
        return None
    try:
        import models
        sub = (db.query(models.Subscription)
               .filter(models.Subscription.tenant_id == tenant_id)
               .order_by(models.Subscription.id.desc()).first())
        return getattr(sub, "plan", None) if sub else None
    except Exception:
        logger.debug("llm rate limit: plan lookup unavailable", exc_info=True)
        return None


def _tenant_row(db, tenant_id: Optional[int]):
    if tenant_id is None:
        return None
    try:
        import models
        return db.query(models.TenantLLMRateLimits).filter(
            models.TenantLLMRateLimits.tenant_id == tenant_id).first()
    except Exception:
        logger.debug("llm rate limit: tenant row unavailable", exc_info=True)
        return None


def resolve_limits(db, tenant_id: Optional[int] = None) -> Limits:
    """Effective limits for a tenant. Tenant override wins, then the platform
    row, then the environment — so a deployment that configures nothing behaves
    exactly as it did before this module existed."""
    env = _env_defaults(db)
    plat = _platform_row(db)
    ten = _tenant_row(db, tenant_id)
    plan = _plan_for(db, tenant_id)

    def pick(attr: str, fallback):
        for row in (ten, plat):
            if row is not None:
                v = getattr(row, attr, None)
                if v is not None:
                    return v
        return fallback

    # Volume: tenant override, then the plan, then platform, then env. A plan
    # value of 0 means UNLIMITED — the same convention as
    # api_rate_limit_per_minute — so it is translated to a very large number
    # rather than to "no calls allowed".
    plan_calls = getattr(plan, "llm_calls_per_hour", None) if plan else None
    ten_calls = getattr(ten, "window_max_calls", None) if ten else None
    if ten_calls is not None:
        window_max = int(ten_calls)
    elif plan_calls is not None:
        window_max = UNLIMITED_CALLS if int(plan_calls) == 0 else int(plan_calls)
    else:
        window_max = int(pick("window_max_calls", env.window_max_calls))

    plan_conc = getattr(plan, "llm_concurrent_max", None) if plan else None
    ten_conc = getattr(ten, "concurrent_max", None) if ten else None
    if ten_conc is not None:
        concurrent = int(ten_conc)
    elif plan_conc is not None:
        concurrent = int(plan_conc)
    else:
        concurrent = int(pick("concurrent_max", env.concurrent_max))

    return Limits(
        enabled=bool(pick("enabled", env.enabled)),
        concurrent_max=concurrent,
        window_seconds=int(pick("window_seconds", env.window_seconds)),
        window_max_calls=window_max,
        assistant_window_max_calls=int(
            pick("assistant_window_max_calls", env.assistant_window_max_calls)),
    )


# ---------------------------------------------------------------------------
# Exemptions — identical to the credit-charging exemptions
# ---------------------------------------------------------------------------
#
# Kept in step with _charge_node_creation_credits deliberately. Internal and
# system-dispatched traffic must never be throttled by a tenant-facing control,
# and having two divergent notions of "who is exempt" is the kind of drift that
# only shows up during an incident.


def is_exempt(user) -> bool:
    if user is None:
        return True
    if getattr(user, "username", None) == "api_service":
        return True
    if getattr(user, "is_service_account", False) is True:
        return True
    if getattr(user, "is_platform_admin", False):
        return True
    if getattr(user, "tenant_id", None) is None:
        return True
    return False


class RateLimited(Exception):
    """Raised when a call exceeds a cap. `cap` is which one, so the caller can
    say something specific and the rejection can be recorded."""

    def __init__(self, cap: str, message: str, retry_after_s: int):
        super().__init__(message)
        self.cap = cap
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass
class Slot:
    """A held concurrency slot, kept alive by a heartbeat until released.

    Always release it — see acquire(). Releasing stops the heartbeat; if the
    process dies instead, the heartbeat stops on its own and the lease expires,
    which is the whole point of making this a lease rather than a timestamp.
    """
    tenant_id: int
    call_id: str
    held: bool
    _stop: Optional[threading.Event] = field(default=None, repr=False)


def _start_heartbeat(tenant_id: int, call_id: str) -> threading.Event:
    """Refresh this slot's lease until told to stop.

    Daemon thread so it can never hold up interpreter shutdown. One thread per
    in-flight call, and in-flight calls are capped per tenant by definition, so
    the count is bounded by the limit itself.
    """
    stop = threading.Event()
    key = _CONC_KEY.format(tenant=tenant_id)

    def _beat():
        while not stop.wait(SLOT_HEARTBEAT_SECONDS):
            try:
                r = _client()
                # Only refresh a member that is still present. If a sweep has
                # already removed it we must not silently resurrect the slot.
                if r.zscore(key, call_id) is None:
                    return
                r.zadd(key, {call_id: time.time()})
                r.expire(key, STALE_SLOT_SECONDS * 4)
            except Exception:
                # A missed beat is survivable — three in a row expires the
                # lease, which is the correct outcome if Redis is truly gone.
                logger.debug("llm rate limit: heartbeat failed", exc_info=True)

    threading.Thread(target=_beat, name=f"llm-rl-hb-{call_id[:8]}",
                     daemon=True).start()
    return stop


def _record_rejection(db, tenant_id: int, tool: str, cap: str) -> None:
    """Best-effort. A rejection we failed to record is still a rejection; it
    must not turn into a 500."""
    try:
        import models
        db.add(models.LLMRateLimitRejection(tenant_id=tenant_id, tool=tool, cap=cap))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("llm rate limit: could not record rejection", exc_info=True)


# ---------------------------------------------------------------------------
# Fail-open alerting
# ---------------------------------------------------------------------------
#
# Failing open is the right call and a dangerous one, because it is invisible:
# the admin sees limits configured and assumes they bind. This makes the gap
# audible. Cooldown is process-local and best-effort — an outage should produce
# an alert, not a stream of them, and precision here is not worth a round trip.

_FAILOPEN_COOLDOWN_S = 15 * 60
_last_failopen_alert: float = 0.0
_failopen_count: int = 0


def _alert_fail_open(db, reason: str) -> None:
    """Record that limiting was bypassed. Never raises — this runs on the path
    that is already degraded."""
    global _last_failopen_alert, _failopen_count
    _failopen_count += 1
    now = time.time()
    if now - _last_failopen_alert < _FAILOPEN_COOLDOWN_S:
        return
    _last_failopen_alert = now
    n, _failopen_count = _failopen_count, 0

    logger.error(
        "llm rate limit: FAILING OPEN — limits are NOT being enforced "
        "(%d call(s) since last alert). Reason: %s", n, reason)
    try:
        import models
        # Field names matter here: AuditEvent has event_name/details, NOT the
        # event_type/actor_email/resource_* shape used elsewhere in the
        # codebase. Passing the wrong ones raises TypeError, which a
        # best-effort try/except then swallows — the alert looks emitted and
        # no row is ever written. utils/pool_alerts.py has that exact bug.
        db.add(models.AuditEvent(
            event_name="LLM_RATE_LIMIT_FAIL_OPEN",
            tenant_id=None,
            user_id=None,
            details={
                "calls_since_last_alert": n,
                "reason": reason[:500],
                "resource": "llm_rate_limit",
                "actor": "system@platform",
            },
        ))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("llm rate limit: fail-open audit event failed", exc_info=True)


def fail_open_count() -> int:
    """Calls allowed through since the last alert. Read by the admin surface so
    'is limiting actually working right now' is answerable."""
    return _failopen_count


def acquire(db, user, tool: str) -> Slot:
    """Reserve a slot for one LLM call, or raise RateLimited.

    ALWAYS pair with release(), including on the failure path. A slot that is
    taken and never given back permanently reduces that tenant's ceiling, and
    because the loss is invisible it compounds silently until the tenant cannot
    make any call at all.
    """
    tenant_id = getattr(user, "tenant_id", None)
    if is_exempt(user):
        return Slot(tenant_id=tenant_id or 0, call_id="", held=False)

    limits = resolve_limits(db, tenant_id)
    if not limits.enabled:
        return Slot(tenant_id=tenant_id, call_id="", held=False)

    try:
        r = _client()
        now = time.time()

        is_assistant = (tool == ASSISTANT_TOOL)
        tool_class = "assistant" if is_assistant else "billable"

        # --- concurrency cap ----------------------------------------------
        # Checked BEFORE the window counter is incremented, and the order is
        # the whole point. The window is a SUSTAINED budget; a call refused for
        # burst concurrency never reached a model and must not spend from it.
        # With the increment first, a user bouncing off the burst limit quietly
        # burned their hourly allowance on calls that never happened — and the
        # tighter the concurrency cap, the faster that drained.
        #
        # This check has no side effects, so ordering it first is free: nothing
        # is written until a slot is actually granted below.
        ckey = _CONC_KEY.format(tenant=tenant_id)
        # Expire LEASES, not long calls. The score is the holder's last
        # heartbeat, so anything this stale has stopped being tended — the
        # process died. A slow-but-alive call keeps refreshing and keeps its
        # slot however long it runs.
        r.zremrangebyscore(ckey, 0, now - STALE_SLOT_SECONDS)
        in_flight = r.zcard(ckey)
        if in_flight >= limits.concurrent_max:
            _record_rejection(db, tenant_id, tool, "concurrent")
            if is_assistant:
                # Same reasoning as the window ceiling below: the assistant is
                # free, so nothing about being refused may imply a purchase
                # would help. This is the cap a burst actually hits — several
                # colleagues asking at once — because an assistant turn holds
                # its slot for the whole stream, so this message is at least as
                # user-facing as the window one.
                message = (
                    f"The assistant is busy for your organisation right now — "
                    f"{limits.concurrent_max} requests are already in progress. "
                    f"This is a capacity limit, not a charge: the assistant is "
                    f"free and nothing has been billed. Try again in a few "
                    f"seconds, or contact support if you need help now.")
            else:
                message = (f"Too many concurrent AI requests: limit is "
                           f"{limits.concurrent_max}. Retry shortly.")
            raise RateLimited(cap="concurrent", message=message,
                              retry_after_s=5)

        # --- window cap ---------------------------------------------------
        # The assistant has its own ceiling so it can be loosened without
        # loosening the billable tools, and vice versa.
        win_max = (limits.assistant_window_max_calls if is_assistant
                   else limits.window_max_calls)
        wkey = _WIN_KEY.format(tenant=tenant_id, tool_class=tool_class)

        used = r.incr(wkey)
        if used == 1:
            r.expire(wkey, limits.window_seconds)
        if used > win_max:
            ttl = r.ttl(wkey)
            ttl = int(ttl) if isinstance(ttl, int) and ttl > 0 else limits.window_seconds
            _record_rejection(db, tenant_id, tool,
                              "assistant_window" if is_assistant else "window")
            if is_assistant:
                # The assistant is deliberately free, so this message must not
                # read as a paywall. A ceiling that says "rate limit" next to a
                # product where everything else deducts credits invites exactly
                # the wrong inference — that paying would lift it. Nothing here
                # is purchasable, and the message says so rather than leaving
                # the user to guess. It also points at escalation, which stays
                # reachable at the ceiling.
                message = (
                    f"You've reached the assistant's usage limit for now — "
                    f"{win_max} questions per {limits.window_seconds // 60 or 1} "
                    f"minutes. This is a volume limit, not a charge: the "
                    f"assistant is free and nothing has been billed. It resets "
                    f"in {ttl}s. If you need help before then, you can still "
                    f"contact support.")
            else:
                message = (f"Rate limit reached: {win_max} calls per "
                           f"{limits.window_seconds}s. Resets in {ttl}s.")
            raise RateLimited(
                cap="assistant_window" if is_assistant else "window",
                message=message,
                retry_after_s=ttl,
            )

        call_id = uuid.uuid4().hex
        r.zadd(ckey, {call_id: now})
        # Bound the key's own lifetime so an idle tenant's set cannot linger.
        r.expire(ckey, STALE_SLOT_SECONDS * 4)
        stop = _start_heartbeat(tenant_id, call_id)
        return Slot(tenant_id=tenant_id, call_id=call_id, held=True, _stop=stop)

    except RateLimited:
        raise
    except Exception as e:  # noqa: BLE001
        # Redis down, or any other bookkeeping failure. Allow the call: a
        # limiter that fails closed turns its own outage into a product outage.
        # Alert, because an unenforced limit that LOOKS enforced is the worst
        # of both worlds.
        _alert_fail_open(db, f"{type(e).__name__}: {e}")
        return Slot(tenant_id=tenant_id or 0, call_id="", held=False)


def release(slot: Optional[Slot]) -> None:
    """Give the slot back. Safe to call with None, with an unheld slot, and
    twice — every one of those happens on some error path.

    Stopping the heartbeat is the important half: even if the Redis delete
    below fails, the lease then expires on its own within STALE_SLOT_SECONDS.
    """
    if slot is None:
        return
    if slot._stop is not None:
        slot._stop.set()
        slot._stop = None
    if not slot.held or not slot.call_id:
        return
    slot.held = False
    try:
        _client().zrem(_CONC_KEY.format(tenant=slot.tenant_id), slot.call_id)
    except Exception:
        # Heartbeat is already stopped, so the lease lapses within
        # STALE_SLOT_SECONDS. A failed release costs one slot for ~90s rather
        # than leaking it forever.
        logger.debug("llm rate limit: release failed; lease will lapse",
                     exc_info=True)
