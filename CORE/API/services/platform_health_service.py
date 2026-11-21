"""Platform health-check business logic (Gate 2b migration of
routers/platform_health.py).

Owns the actual probing/orchestration (component checks, the TTL cache, the
overall-status rollup) that the router used to inline. The one piece of data
access (the licensed-worker count) goes through `crud/platform_health.py`.

Everything here is re-exported from `routers/platform_health.py` (the module
path `tests/test_status_page.py` imports from) so the public import surface is
unchanged — see the rule about preserving external import paths.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from crud.platform_health import count_licensed_workers

KEYCLOAK_INTERNAL_URL = os.getenv("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "master")
# Harbor in this stack runs as `tenant-harbor-core:8080` (the docker-compose
# service for the tenant Harbor deployment). The earlier default
# `http://harbor:80` was a leftover from a different Harbor topology and
# resolved to nothing in this stack — every health probe reported Harbor
# as down with "Temporary failure in name resolution".
HARBOR_URL = os.getenv("HARBOR_URL", "http://tenant-harbor-core:8080")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/2")
# GF.C0b.8 — MOD's own HF model cache (CORE/DockerContainers/mod-model-cache),
# compose service `model-cache` listening on :8080. Same internal-network
# name the compose file uses; override for a non-default deployment.
MODEL_CACHE_URL = os.getenv("MODEL_CACHE_URL", "http://model-cache:8080")


SERVICES = {
    "api": {"name": "API", "critical": True},
    "database": {"name": "Database", "critical": True},
    "keycloak": {"name": "Authentication", "critical": True},
    "minio": {"name": "Object Storage", "critical": True},
    "redis": {"name": "Cache", "critical": False},
    "harbor": {"name": "Container Registry", "critical": False},
    "workers": {"name": "Workers", "critical": False},
    # Not critical: it's a cache in front of huggingface.co, not core
    # platform infrastructure — an outage degrades model downloads, it
    # doesn't take down auth/workflows/billing.
    "model_cache": {"name": "Model Cache", "critical": False},
    # GY.C2a — the crypto known-answer self-test. Not critical: a stock
    # (non-FIPS) install is expected to report `up`; it only degrades when
    # crypto.fips_mode_required is true and the self-test failed.
    "crypto_self_test": {"name": "Crypto Self-Test", "critical": False},
}


class ComponentStatus(BaseModel):
    status: str
    latency_ms: Optional[int] = None
    error: Optional[str] = None

class PublicStatusResponse(BaseModel):
    status: str
    components: dict[str, str]
    checked_at: str

class DetailedStatusResponse(BaseModel):
    status: str
    components: dict[str, ComponentStatus]
    checked_at: str


async def _check_database(db: Session) -> tuple[str, int, Optional[str]]:
    try:
        start = time.monotonic()
        db.execute(text("SELECT 1"))
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return "up", elapsed_ms, None
    except Exception as e:
        return "down", 0, str(e)


async def _check_keycloak() -> tuple[str, int, Optional[str]]:
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0)
        ) as client:
            start = time.monotonic()
            url = f"{KEYCLOAK_INTERNAL_URL}/realms/{KEYCLOAK_REALM}/.well-known/openid-configuration"
            response = await client.get(url)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if response.status_code == 200:
                return "up", elapsed_ms, None
            return "degraded", elapsed_ms, f"HTTP {response.status_code}"
    except Exception as e:
        return "down", 0, str(e)


def _s3_health_url() -> str:
    """Resolve the S3/object-storage internal URL for the ACTIVE backend.

    The old default `minio:9000` was a dead DNS name in this stack (the S3
    server is now versitygw), so every probe spent ~7s in getaddrinfo before
    httpx capped it at ~5s and reported a false "Object Storage down / outage".
    Derive the endpoint from the same resolver the main API storage client uses
    (`utils.storage._resolve_endpoints`) so the health probe always tracks the
    real backend; fall back to explicit env, then the versitygw default.
    """
    try:
        from utils.storage import _resolve_endpoints
        internal_endpoint, _ext, internal_secure, _es = _resolve_endpoints()
        scheme = "https" if internal_secure else "http"
        return f"{scheme}://{internal_endpoint}"
    except Exception:  # noqa: BLE001
        return os.getenv("VERSITYGW_INTERNAL_URL") or os.getenv(
            "S3_INTERNAL_URL") or os.getenv(
            "MINIO_INTERNAL_URL", "http://versitygw:7070")


async def _check_minio() -> tuple[str, int, Optional[str]]:
    """Liveness probe for the S3 object-storage server (versitygw by default,
    minio in the legacy profile). Both answer an unauthenticated request to `/`
    with an S3 error (403 AccessDenied) — any HTTP response < 500 means the
    server is alive, so treat that as "up" rather than requiring a 200 from a
    MinIO-specific /minio/health/live path that versitygw does not serve."""
    try:
        import httpx
        base = _s3_health_url()
        # Short connect budget so a dead host fails fast instead of hanging.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0)
        ) as client:
            start = time.monotonic()
            response = await client.get(f"{base}/")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if response.status_code < 500:
                return "up", elapsed_ms, None
            return "degraded", elapsed_ms, f"HTTP {response.status_code}"
    except Exception as e:
        return "down", 0, str(e)


async def _check_redis() -> tuple[str, int, Optional[str]]:
    try:
        import redis
        start = time.monotonic()
        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        r.ping()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        r.close()
        return "up", elapsed_ms, None
    except Exception as e:
        return "down", 0, str(e)


async def _check_harbor() -> tuple[str, int, Optional[str]]:
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0)
        ) as client:
            start = time.monotonic()
            response = await client.get(f"{HARBOR_URL}/api/v2.0/ping")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if response.status_code == 200:
                return "up", elapsed_ms, None
            return "degraded", elapsed_ms, f"HTTP {response.status_code}"
    except Exception as e:
        return "down", 0, str(e)


async def _check_model_cache() -> tuple[str, int, Optional[str]]:
    """Liveness probe for the MOD model cache (`model-cache`/`my_model_cache`,
    CORE/DockerContainers/mod-model-cache). It exposes a plain `/health` ->
    `{"status":"ok"}`, same shape as every other MOD FastAPI service."""
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(3.0, connect=1.0)
        ) as client:
            start = time.monotonic()
            response = await client.get(f"{MODEL_CACHE_URL}/health")
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if response.status_code == 200:
                return "up", elapsed_ms, None
            return "degraded", elapsed_ms, f"HTTP {response.status_code}"
    except Exception as e:
        return "down", 0, str(e)


async def _check_workers(db: Session) -> tuple[str, int, Optional[str]]:
    try:
        start = time.monotonic()
        # The WorkerStatus enum is {UNLICENSED, LICENSED, OFFLINE} — there
        # is no `ACTIVE` member. The earlier `WorkerStatus.ACTIVE` here
        # raised AttributeError on every probe, dropping into the except
        # branch and returning ("down", 0, "ACTIVE"). Net effect: every
        # /health/status response said workers were down even when they
        # were licensed and serving tasks. Use LICENSED — that's the
        # "this worker is healthy and assigned a license" status.
        licensed_count = count_licensed_workers(db)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if licensed_count > 0:
            return "up", elapsed_ms, None
        return "degraded", elapsed_ms, "No workers registered"
    except Exception as e:
        return "down", 0, str(e)


def _check_crypto_self_test(db: Session) -> tuple[str, int, Optional[str]]:
    """GY.C2a — crypto known-answer self-test (SHA-256 / HMAC-SHA256 /
    AES-256-GCM / ECDSA P-384). In-process, cached, no network. ``up`` when
    the self-test passes, or when it failed but crypto.fips_mode_required is
    false (a stock non-FIPS install is not unhealthy). ``degraded`` only when
    the setting requires FIPS and the self-test failed."""
    try:
        from utils.paid_import import optional_paid

        crypto_status = optional_paid("services.crypto_status")
        if crypto_status is None:
            # CE: no FIPS readiness requirement; nothing to report.
            return "up", 0, None
        readiness = crypto_status.readiness(db)
        if readiness["status"] == "unhealthy":
            return "degraded", 0, readiness["reason"]
        return "up", 0, None
    except Exception as e:
        return "down", 0, str(e)


async def _run_all_checks(db: Session) -> dict[str, tuple[str, int, Optional[str]]]:
    results = {}
    results["api"] = ("up", 1, None)
    results["database"] = await _check_database(db)

    keycloak_task = asyncio.create_task(_check_keycloak())
    minio_task = asyncio.create_task(_check_minio())
    redis_task = asyncio.create_task(_check_redis())
    harbor_task = asyncio.create_task(_check_harbor())
    model_cache_task = asyncio.create_task(_check_model_cache())

    results["keycloak"] = await keycloak_task
    results["minio"] = await minio_task
    results["redis"] = await redis_task
    results["harbor"] = await harbor_task
    results["model_cache"] = await model_cache_task
    results["workers"] = await _check_workers(db)
    # GY.C2a — in-process cached self-test verdict, no network probe.
    results["crypto_self_test"] = _check_crypto_self_test(db)

    return results


# Short server-side TTL cache shared by /health/status and
# /platform/health/detailed. Both fan out live probes across keycloak, S3,
# redis, harbor and the DB on EVERY call; the status page (and a polling
# health widget) can hit these several times a second. A ~12s cache collapses
# that into at most one real fan-out per interval while staying fresh enough
# to surface an outage quickly. An asyncio.Lock prevents a thundering herd of
# concurrent fan-outs when the cache expires under load.
_HEALTH_CHECK_TTL_SEC = 12.0
_health_cache: dict = {"checks": None, "checked_at": 0.0}
_health_cache_lock = asyncio.Lock()


def get_cached_component_check(component: str) -> Optional[tuple[str, int, Optional[str]]]:
    """The last-computed `(status, latency_ms, error)` for one `SERVICES` key,
    from the TTL cache above, WITHOUT triggering a new probe — `None` if
    nothing has populated the cache yet (e.g. right after API startup, before
    any `/health` request has run).

    GF.C1b's model-cache registry reads `component="model_cache"` through
    this rather than importing `_health_cache` directly, and rather than
    probing the manager cache itself — that keeps it free of outbound HTTP
    calls of its own (it also must never be called from inside an open DB
    transaction, which this only avoids because it does no I/O at all)."""
    checks = _health_cache.get("checks") or {}
    return checks.get(component)


async def _get_cached_checks(db: Session) -> dict[str, tuple[str, int, Optional[str]]]:
    now = time.monotonic()
    cached = _health_cache["checks"]
    if cached is not None and (now - _health_cache["checked_at"]) < _HEALTH_CHECK_TTL_SEC:
        return cached
    async with _health_cache_lock:
        # Re-check inside the lock: another request may have refreshed while we
        # waited, in which case reuse its result instead of probing again.
        now = time.monotonic()
        cached = _health_cache["checks"]
        if cached is not None and (now - _health_cache["checked_at"]) < _HEALTH_CHECK_TTL_SEC:
            return cached
        checks = await _run_all_checks(db)
        _health_cache["checks"] = checks
        _health_cache["checked_at"] = time.monotonic()
        return checks


def _overall_status(checks: dict) -> str:
    critical = [k for k, v in SERVICES.items() if v["critical"]]
    for svc in critical:
        status = checks.get(svc, ("down", 0, None))[0]
        if status == "down":
            return "outage"
    if any(checks.get(k, ("up", 0, None))[0] == "degraded" for k in checks):
        return "degraded"
    return "operational"


async def get_public_status(db: Session) -> PublicStatusResponse:
    """Build the `/health/status` response body (no auth)."""
    from datetime import datetime
    checks = await _get_cached_checks(db)
    return PublicStatusResponse(
        status=_overall_status(checks),
        components={k: checks[k][0] for k in SERVICES},
        checked_at=datetime.utcnow().isoformat() + "Z",
    )


async def get_detailed_status(db: Session) -> DetailedStatusResponse:
    """Build the `/platform/health/detailed` response body (platform admin)."""
    from datetime import datetime
    checks = await _get_cached_checks(db)
    return DetailedStatusResponse(
        status=_overall_status(checks),
        components={
            k: ComponentStatus(
                status=checks[k][0],
                latency_ms=checks[k][1],
                error=checks[k][2],
            )
            for k in SERVICES
        },
        checked_at=datetime.utcnow().isoformat() + "Z",
    )
