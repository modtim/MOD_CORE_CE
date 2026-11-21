# CORE/database.py
#
# Dynamic database credentials via Vault database secrets engine.
# The API never holds a static password — it requests short-lived Postgres
# credentials from Vault and rotates them automatically at 50% TTL.

import os
import logging
import socket
import threading
import time
import fcntl

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker

import models
from security import hash_api_key

logger = logging.getLogger(__name__)

from services.debug_mode import is_debug_mode

Base = declarative_base()

# ---------------------------------------------------------------------------
# Connection parameters (non-secret, from env)
# ---------------------------------------------------------------------------
_PG_HOST = os.getenv("POSTGRES_HOST", "postgres-app")
_PG_PORT = os.getenv("POSTGRES_PORT", "5432")
_PG_DB = os.getenv("POSTGRES_DB", "modTech")
# The STATIC role that must own every table. The app connects as a
# rotating Vault credential, and anything it creates is owned by that
# ephemeral role — un-migratable the moment it rotates.
_PG_OWNER_ROLE = os.getenv("POSTGRES_USER", "modTech")
# The API's main SQLAlchemy engine connects through PgBouncer (transaction
# pooling) when POSTGRES_POOLER_HOST is set, so the 20 uvicorn workers' pools
# (each under a distinct Vault dynamic role) fold onto a bounded set of real
# Postgres backends. Unset -> direct to POSTGRES_HOST. Session-pinned consumers
# (the LISTEN/NOTIFY trigger listener) always bypass the pooler via
# get_direct_engine() — a pooled LISTEN loses its subscription on checkin.
_PG_POOLER_HOST = os.getenv("POSTGRES_POOLER_HOST", _PG_HOST)
_PG_POOLER_PORT = os.getenv("POSTGRES_POOLER_PORT", _PG_PORT)

# ---------------------------------------------------------------------------
# Boot-window DNS for the DB host (FIX-API-DB-HOST-DNS-RETRY, F21/F-3 half 2).
# The compose dependency (api waits for openbao-unseal service_healthy, 9edc2e5877)
# removed most of the boot race, but compose has no postgres-app health gate for
# WORKERS (:1246), so a cold start can still ask for postgres-app before its
# name is in the resolver. create_engine does not touch the network, so the
# failure surfaces later as an OperationalError per request ("could not translate
# host name") — the 503 handler (main.py, 7803d29343) already makes those cheap,
# but the BOOT window before the first engine is built gets a bounded wait here
# instead. Constants only: no new env var, no platform setting.
# ---------------------------------------------------------------------------
_DNS_RETRY_ATTEMPTS = 6      # getaddrinfo tries before giving up
_DNS_RETRY_BASE_DELAY = 1.0  # s; doubles per try...
_DNS_RETRY_MAX_DELAY = 5.0   # ...capped here
_DNS_RETRY_MAX_TOTAL = 30.0  # hard cap on total waiting (s)


def _wait_for_db_host(host: str, port: str,
                      attempts: int = _DNS_RETRY_ATTEMPTS,
                      base_delay: float = _DNS_RETRY_BASE_DELAY,
                      max_total: float = _DNS_RETRY_MAX_TOTAL) -> bool:
    """Resolve `host` (name only, never a DSN) before an engine is built.

    Returns True as soon as getaddrinfo answers. On an unresolvable host it
    sleeps min(base_delay * 2**i, _DNS_RETRY_MAX_DELAY) between tries, never
    waiting more than `max_total` in total, then logs ONE warning (host name
    only — no password, no DSN) and returns False. It NEVER raises: the caller
    proceeds, pool_pre_ping and the existing 503/Retry-After handler path then
    behave exactly as before.
    """
    waited = 0.0
    for i in range(attempts):
        try:
            socket.getaddrinfo(host, port)
            return True
        except socket.gaierror:
            if i == attempts - 1:
                break
            delay = min(base_delay * (2 ** i), _DNS_RETRY_MAX_DELAY)
            if waited + delay > max_total:
                break
            time.sleep(delay)
            waited += delay
    logger.warning(
        "DB host %s did not resolve after %d attempts (%.1fs waited); "
        "continuing — DB requests get 503 until it resolves",
        host, attempts, waited,
    )
    return False

# ---------------------------------------------------------------------------
# Pooled-engine sizing (through PgBouncer) — env-configurable.
# ---------------------------------------------------------------------------
# The main app engine connects through PgBouncer (transaction pooling), so these
# are CHEAP client connections that fold onto a bounded set of real Postgres
# backends. The true ceilings live downstream, NOT here:
#   * pgbouncer.ini  max_client_conn      = 2000  (client conns it will accept)
#   * pgbouncer.ini  max_db_connections   = 150   (real PG backends it will open)
#   * postgres       max_connections      = 300   (workers x 3 DIRECT + 150 < 300)
# Because PgBouncer multiplexes, a generous app pool never oversubscribes
# Postgres — server backends stay capped at max_db_connections regardless. The
# OLD defaults (pool_size=2, max_overflow=8 => 10/worker) were far too small:
# under a full node rescan the scanner's self-calls + the per-minute background
# loops + real WorkflowBuilder traffic exhausted a worker's 10 slots, and
# SQLAlchemy raised "QueuePool limit ... connection timed out" — the WB then saw
# the API as "down". 10 + 20 = 30 slots/worker gives real headroom while the
# PgBouncer caps above remain the authoritative backstop.
_DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
_DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
_DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "5"))  # FAIL FAST: a busy pool must
# not make requests hang (was 30s) — a 30s wait stalled all 8 workers at once and froze the
# whole API under load. 5s means a burst slows a few requests, never freezes the platform.

# Server-side backstop against transaction/session leaks. Any pooled backend a
# client leaves "idle in transaction" longer than this is terminated by Postgres,
# freeing the connection instead of pinning it forever. Applied per-connection via
# a SQLAlchemy `connect` listener (PgBouncer ignores the `options` startup param,
# so it cannot be passed that way). 0 disables. 5 min is well clear of any
# legitimate slow-but-progressing transaction, so it only ever reaps real leaks.
_DB_IDLE_IN_TXN_TIMEOUT_MS = int(os.getenv("DB_IDLE_IN_TXN_TIMEOUT_MS", "300000"))

# OpenBao config (VAULT_ADDR kept verbatim: the OpenBao client/binary reads it natively)
_VAULT_ADDR = os.getenv("VAULT_ADDR", "")
_VAULT_ROLE_ID = os.getenv("OPENBAO_APPROLE_ROLE_ID") or os.getenv("VAULT_APPROLE_ROLE_ID", "")
_VAULT_SECRET_ID = os.getenv("OPENBAO_APPROLE_SECRET_ID") or os.getenv("VAULT_APPROLE_SECRET_ID", "")
_VAULT_DB_ROLE = os.getenv("OPENBAO_DB_ROLE") or os.getenv("VAULT_DB_ROLE", "api-readwrite")

# OpenBao Agent writes a valid token to this file (auto-renewed)
_VAULT_AGENT_TOKEN_PATH = os.getenv("OPENBAO_AGENT_TOKEN_PATH") or os.getenv("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
# MOD-MIGRATE-OPTIONS: how long _vault_login waits for the agent token file,
# seconds. Default 30 = the historical 6x5s loop (behaviour unchanged); clamped
# 0..120. mod_migrate.py sets 0 for migration subprocesses ONLY when the parent
# already waited once at runner start, so ~374 steps stop paying the 30s wait.
def _agent_token_wait_s() -> int:
    try:
        raw = int(os.getenv("OPENBAO_AGENT_TOKEN_WAIT_S") or os.getenv("VAULT_AGENT_TOKEN_WAIT_S", "30"))
    except (TypeError, ValueError):
        raw = 30
    return max(0, min(120, raw))


_VAULT_AGENT_TOKEN_WAIT_S = _agent_token_wait_s()

# J163: per-process memo. Once the wait loop below has timed out once, this
# process has no Vault Agent sidecar (CE: no agent, and the migrate path +
# renewal rotations each re-entered _vault_login, paying 30s every time).
# Later calls skip the wait and go straight to the AppRole fallback. The
# token file is still checked first on EVERY call (top of _vault_login), so
# an agent that starts later is still picked up; behaviour with a real agent
# present (token appears within the window) is unchanged.
_agent_wait_exhausted = False

# B91 (startup-time, 10-12) — cross-process single-flight for the DDL half of
# create_tables(). `uvicorn --workers 8` boots 8 processes and each ran the whole
# bootstrap at MODULE IMPORT: 9x Vault reads + lease rotations, 9x `migrate:
# schema version ... already applied` version-gate reads, 8x seed_service_user
# SELECTs, all serialized against one DB while the workers also contend on
# connection setup (measured 10-12: 60.7s before the first worker exists).
# A FILE lock is required here, not Redis: this runs during module import, before
# any Redis client exists, and must not add a network dependency. The holder runs
# the DDL; the other 7 WAIT (bounded poll) — unlike the launch heal, a worker
# that skipped DDL while the holder was mid-DDL would import routers against
# missing tables, so waiting is correct.
_DB_BOOTSTRAP_LOCK_PATH = "/run/mod-db-bootstrap.lock"
_DB_BOOTSTRAP_WAIT_MAX_S = 30.0
_DB_BOOTSTRAP_POLL_INTERVAL_S = 0.5
# Written ONLY by the DDL holder on success (pid/ts, no secrets); lets a waiter
# that acquires the lock after a timeout tell "holder finished" apart from
# "holder crashed mid-DDL" (see _bootstrap_ddl_single_flight).
_DB_BOOTSTRAP_DONE_MARKER_PATH = "/run/mod-db-bootstrap.done"

# B91 (10-12) — pod-level marker so only the FIRST process to fetch Vault DB
# credentials logs the rotation line; the other 7 log it at debug. Marker only
# (pid), NEVER the credentials: a true per-pod credential cache would put dynamic
# Postgres creds in a shared file, which is a security change → NEEDS-OWNER.
_DB_CREDS_MARKER_PATH = "/run/mod-db-creds-marker"

# ---------------------------------------------------------------------------
# Module-level engine + session factory (swapped atomically on rotation)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_engine = None
_SessionLocal = None
_renewal_thread = None
_vault_token = None
_lease_id = None
_lease_duration = 0
_cred_fetched_at = 0.0

# Current Vault DB creds (kept so get_direct_engine can build a direct-to-Postgres
# engine for session-pinned consumers that PgBouncer's transaction pooling breaks).
_cur_username = None
_cur_password = None
_direct_engine = None
_direct_creds_key = None
_direct_lock = threading.Lock()

# B91 (10-12): per-process record that this worker already ran the DDL half of
# create_tables(), so a second call in the same process does not re-take the lock.
_bootstrap_ddl_done = False

# Force-rotate single-flight guard. Vault can revoke a lease before the
# scheduled 50%-TTL rotation fires (max_lease_ttl hit, manual revoke,
# Vault restart). When that happens, pool connections survive but the
# underlying PG role lost grants — every query returns SQLSTATE 42501.
# The handle_error listener (_on_engine_handle_error) and the renewal-
# loop grants probe both call _try_force_rotate; the cooldown collapses
# a burst of concurrent failures into a single rotation.
_force_rotate_lock = threading.Lock()
_last_force_rotate_attempt_at = 0.0
_FORCE_ROTATE_COOLDOWN_S = 10.0
_GRANTS_PROBE_INTERVAL_S = 30.0


def _use_vault() -> bool:
    """Check if Vault dynamic credentials are available or will be (agent starting)."""
    if not _VAULT_ADDR:
        return False
    # If we have AppRole creds or the agent token file exists, we're good
    if _VAULT_ROLE_ID or os.path.exists(_VAULT_AGENT_TOKEN_PATH):
        return True
    # On fresh start, Vault Agent may still be starting — assume Vault mode
    # if VAULT_ADDR is set (the retry loop in initialize() will wait)
    return True


def _read_agent_token() -> str:
    """Read the Vault token written by Vault Agent sidecar."""
    try:
        with open(_VAULT_AGENT_TOKEN_PATH, "r") as f:
            token = f.read().strip()
        if token:
            logger.info("Using OpenBao token from Agent (%s)", _VAULT_AGENT_TOKEN_PATH)
            return token
    except (FileNotFoundError, PermissionError):
        pass
    return ""


def _vault_login() -> str:
    """
    Get a valid Vault token.
    Priority: 1) Agent-provided token (auto-renewed), 2) Direct AppRole login.
    Waits up to 30s for the agent token file to appear on fresh starts.
    """
    # Try agent token first (preferred — agent handles renewal)
    agent_token = _read_agent_token()
    if agent_token:
        return agent_token

    # Wait for agent token file (Vault Agent may still be starting) — once per
    # process (see _agent_wait_exhausted).
    global _agent_wait_exhausted
    _wait_iters = (_VAULT_AGENT_TOKEN_WAIT_S + 4) // 5 if not _agent_wait_exhausted else 0
    for i in range(_wait_iters):
        logger.info("Waiting for OpenBao Agent token (%s)... (%d/%d)", _VAULT_AGENT_TOKEN_PATH, i + 1, _wait_iters)
        time.sleep(5)
        agent_token = _read_agent_token()
        if agent_token:
            return agent_token
    if _wait_iters:
        _agent_wait_exhausted = True

    # Fallback: direct AppRole login
    if _VAULT_ROLE_ID and _VAULT_SECRET_ID:
        from utils.vault_client import approle_login
        logger.info("OpenBao Agent token not available, using direct AppRole login")
        return approle_login(_VAULT_ROLE_ID, _VAULT_SECRET_ID)

    raise RuntimeError("No OpenBao authentication available (no agent token after 30s, no AppRole credentials)")


def _fetch_dynamic_creds(token: str) -> dict:
    """Request short-lived Postgres credentials from Vault database engine."""
    from utils.vault_client import _request
    payload = _request("GET", f"/v1/database/creds/{_VAULT_DB_ROLE}", token=token, timeout=10)
    return payload


# GSEC R-057 (inert): bootstrap-only Postgres TLS knobs. When both are unset
# these produce {} — byte-identical connect behaviour to before.
_PG_SSLMODE_VALUES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
_invalid_pg_sslmode_warned = False


def _pg_ssl_connect_args() -> dict:
    """psycopg2/SQLAlchemy connect args for Postgres TLS (GSEC R-057).

    Connection bootstrap env (deploy-time, not a runtime tunable):
      POSTGRES_SSLMODE     one of disable|allow|prefer|require|verify-ca|verify-full
      POSTGRES_SSLROOTCERT path to the CA bundle for verify-ca / verify-full
    Unset -> {} (no sslmode passed, behaviour unchanged). Invalid sslmode is
    ignored with a one-time warning. Used by every engine/connection builder
    that hits a Postgres platform DB.
    """
    global _invalid_pg_sslmode_warned
    args = {}
    mode = os.getenv("POSTGRES_SSLMODE", "").strip()
    if mode:
        if mode in _PG_SSLMODE_VALUES:
            args["sslmode"] = mode
        elif not _invalid_pg_sslmode_warned:
            logger.warning(
                "POSTGRES_SSLMODE %r is invalid (expected one of %s); ignoring",
                mode, ", ".join(sorted(_PG_SSLMODE_VALUES)),
            )
            _invalid_pg_sslmode_warned = True
    rootcert = os.getenv("POSTGRES_SSLROOTCERT", "").strip()
    if rootcert:
        args["sslrootcert"] = rootcert
    return args


def _build_engine(username: str, password: str, *, host: str = None, port: str = None,
                  pool_size: int = None, max_overflow: int = None):
    """Create a new SQLAlchemy engine with the given credentials.

    Defaults target the PgBouncer pooler (POSTGRES_POOLER_HOST) and use the
    env-configurable _DB_POOL_SIZE / _DB_MAX_OVERFLOW (see their definition
    above for why the pooled pool can be generous — PgBouncer multiplexes these
    client connections onto a bounded set of real Postgres backends).

    Pass host/port + explicit tiny pool_size/max_overflow to build a DIRECT
    engine (see get_direct_engine): those connections bypass PgBouncer and hit
    Postgres directly, so they ARE budgeted against max_connections and must
    stay small.
    """
    from urllib.parse import quote_plus
    from sqlalchemy import event
    host = host or _PG_POOLER_HOST
    port = port or _PG_POOLER_PORT
    if pool_size is None:
        pool_size = _DB_POOL_SIZE
    if max_overflow is None:
        max_overflow = _DB_MAX_OVERFLOW
    # Boot-window DNS: give the DB host a bounded chance to appear in the
    # resolver before the first engine is built (returns False, never raises).
    _wait_for_db_host(host, port)
    url = f"postgresql://{quote_plus(username)}:{quote_plus(password)}@{host}:{port}/{_PG_DB}"
    engine = create_engine(url, pool_pre_ping=True, pool_recycle=1800,
                           pool_size=pool_size, max_overflow=max_overflow,
                           pool_timeout=_DB_POOL_TIMEOUT,
                           connect_args=_pg_ssl_connect_args())
    event.listen(engine, "handle_error", _on_engine_handle_error)

    # Leak backstop: stamp idle_in_transaction_session_timeout on every backend
    # this engine opens. A handler that queries then stalls (slow external call,
    # forgotten commit) leaves the backend "idle in transaction"; without this it
    # stays pinned indefinitely — the ~23 idle-in-transaction backends observed
    # live. Postgres reaps anything past the timeout, freeing the connection.
    if _DB_IDLE_IN_TXN_TIMEOUT_MS > 0:
        @event.listens_for(engine, "connect")
        def _set_idle_in_txn_timeout(dbapi_conn, _rec):  # noqa: ANN001
            try:
                cur = dbapi_conn.cursor()
                cur.execute(
                    "SET idle_in_transaction_session_timeout = %s"
                    % int(_DB_IDLE_IN_TXN_TIMEOUT_MS)
                )
                cur.close()
                dbapi_conn.commit()
            except Exception:
                # Never let a backstop-tuning failure block a real connection.
                try:
                    dbapi_conn.rollback()
                except Exception:
                    pass
    return engine


def _pgcode_from_exception(exc) -> str | None:
    """Best-effort SQLSTATE extraction. psycopg2 surfaces it on the
    exception itself (`pgcode`); SQLAlchemy wrapping pushes it onto `.orig`
    or `.diag.sqlstate` — check all the spots.
    """
    pgcode = getattr(exc, "pgcode", None)
    if pgcode:
        return pgcode
    orig = getattr(exc, "orig", None)
    if orig is not None:
        pgcode = getattr(orig, "pgcode", None)
        if pgcode:
            return pgcode
    diag = getattr(exc, "diag", None) or (getattr(orig, "diag", None) if orig else None)
    if diag is not None:
        return getattr(diag, "sqlstate", None)
    return None


def _try_force_rotate(reason: str) -> bool:
    """Trigger one-off credential rotation outside the 50%-TTL schedule.

    Single-flight via cooldown so a burst of failing queries collapses
    into a single rotation. Returns True if rotation ran, False if
    suppressed by cooldown or non-Vault mode.
    """
    global _last_force_rotate_attempt_at
    if not _use_vault():
        return False
    now = time.time()
    with _force_rotate_lock:
        if now - _last_force_rotate_attempt_at < _FORCE_ROTATE_COOLDOWN_S:
            return False
        _last_force_rotate_attempt_at = now
    logger.warning("Forcing Vault credential rotation: %s", reason)
    _rotate_credentials()
    return True


def _on_engine_handle_error(ctx):
    """SQLAlchemy handle_error listener — react to SQLSTATE 42501.

    When Vault revokes the current lease early, every query on the
    surviving pool connections returns 42501 until the next scheduled
    rotation (up to 30 min later). Detect the first 42501, kick a
    rotation on a background thread; the next request lands on the
    new engine.
    """
    exc = getattr(ctx, "original_exception", None)
    if exc is None:
        return
    if _pgcode_from_exception(exc) != "42501":
        return
    threading.Thread(
        target=_try_force_rotate,
        args=("query returned SQLSTATE 42501 (likely Vault lease revoked)",),
        daemon=True,
    ).start()


def _grants_probe_ok() -> bool:
    """Quick query that requires SELECT on `users`. Detects revoked grants
    even while the API is idle.

    Returns False only on 42501 — transient connection errors are treated
    as 'ok' (we don't want to thrash on a network blip). On 42501 the
    caller fires _try_force_rotate.
    """
    engine = get_engine()
    if engine is None:
        return True
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1 FROM users WHERE FALSE")
        return True
    except Exception as exc:
        return _pgcode_from_exception(exc) != "42501"


def _install_soft_delete_filter(SessionCls):
    """Install a global do_orm_execute listener that injects
    `deleted_at IS NULL` into every query of a soft-delete-enabled model.

    Covers every place in the codebase that selects from `users` or
    `tenants` without having to audit ~160 individual callsites. Any caller
    that intentionally needs soft-deleted rows (e.g., the tenant-delete
    endpoint looking up the tenant it's about to delete, or an audit
    viewer) can opt out by setting `execution_options(include_deleted=True)`
    on the query — the listener respects that flag.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import with_loader_criteria

    # Imported lazily so database.py doesn't need to import models at module load time.
    def _get_soft_delete_models():
        import models as _m
        return [
            _m.Tenant,
            _m.User,
            # wizard-rebuild §11b — ISO 27001 soft-delete on the five
            # wizard-managed tables. SoftDeleteWithActorMixin gives them
            # a `deleted_at` column; this listener filters live SELECTs.
            _m.NodeDefinition,
            _m.BuildSpecRecord,
            _m.WorkflowRun,
            _m.NodeVariantVerification,
            _m.RepoDocsExtract,
            # iso-soft-delete-sweep (2026-09-22) — business/audit models
            # converted from hard-delete to soft-delete in crud/. Their live
            # SELECTs must hide tombstoned rows. Any caller that intentionally
            # needs deleted rows (reactivation-on-recreate, retention GC,
            # version-number allocation) passes execution_options(
            # include_deleted=True); those sites are updated in the same sweep.
            _m.ChangelogEntry,
            _m.Manager,
            _m.Notification,
            _m.TenantKnowledgeDocument,
            _m.PermissionTemplate,
            _m.SMTPSettings,
            _m.StorageConnection,
            _m.WorkflowVersion,
            _m.WorkflowRunFile,
            _m.WebhookEndpoint,
            _m.TenantNodeEntitlement,
            _m.Invitation,
            _m.AIConversation,
            _m.AcceleratorBenchmarkStrategy,
            # iso-soft-delete-sweep (bulk-delete pass, 2026-09-22d)
            _m.WorkflowTriggerRule,
        ]

    @event.listens_for(SessionCls, "do_orm_execute")
    def _add_soft_delete_filter(execute_state):
        # Only touch real user-issued selects — skip relationship loads,
        # column loads, and callers that explicitly opted in to seeing
        # deleted rows.
        if (execute_state.is_column_load
            or execute_state.is_relationship_load
            or execute_state.execution_options.get("include_deleted", False)
        ):
            return
        try:
            models_with_soft_delete = _get_soft_delete_models()
        except Exception:
            return
        for cls in models_with_soft_delete:
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(cls, lambda c: c.deleted_at.is_(None), include_aliases=True)
            )


def _swap_engine(new_engine):
    """Atomically replace the module-level engine and session factory."""
    global _engine, _SessionLocal
    old_engine = _engine
    with _lock:
        _engine = new_engine
        _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=new_engine)
        _install_soft_delete_filter(_SessionLocal)
    # Dispose old connections after swap — in-flight sessions finish on existing connections,
    # the pool drains naturally. Old Vault-created Postgres roles stay valid until their TTL.
    if old_engine is not None and old_engine is not new_engine:
        try:
            old_engine.dispose()
        except Exception:
            pass


def _creds_rotation_first_in_pod() -> bool:
    """True only for the FIRST process in the pod that logs a rotation (B91, 10-12).

    Each of the 8 uvicorn workers needs its own engine, so `initialize()` stays
    per-process and the Vault fetch itself cannot be shared — putting dynamic
    Postgres creds in a shared file would be a security change (NEEDS-OWNER). This
    is a pid marker only, so the 7 later workers log the rotation at debug instead
    of INFO. Best-effort: an unusable marker path falls back to logging as before.
    """
    try:
        with open(_DB_CREDS_MARKER_PATH, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            if os.path.getsize(_DB_CREDS_MARKER_PATH) == 0:
                f.write(str(os.getpid()))
                f.flush()
                return True
            return False
    except OSError:
        return True


def _rotate_credentials():
    """Fetch new dynamic credentials from Vault and swap the engine."""
    global _vault_token, _lease_id, _lease_duration, _cred_fetched_at
    global _cur_username, _cur_password

    try:
        # Re-login to Vault (token may have expired)
        _vault_token = _vault_login()

        payload = _fetch_dynamic_creds(_vault_token)
        data = payload.get("data", {})
        username = data.get("username")
        password = data.get("password")

        if not username or not password:
            logger.error("Vault returned empty database credentials")
            return False

        _lease_id = payload.get("lease_id", "")
        _lease_duration = payload.get("lease_duration", 3600)
        _cred_fetched_at = time.time()

        with _lock:
            _cur_username, _cur_password = username, password

        new_engine = _build_engine(username, password)
        _swap_engine(new_engine)

        if _creds_rotation_first_in_pod():
            logger.info(
                "Database credentials rotated (lease TTL: %ds, next rotation in ~%ds)",
                _lease_duration,
                _lease_duration // 2,
            )
        else:
            logger.debug(
                "Database credentials rotated (lease TTL: %ds, next rotation in ~%ds) "
                "[pod already logged a rotation]",
                _lease_duration,
                _lease_duration // 2,
            )
        return True

    except Exception as e:
        logger.error("Failed to rotate database credentials: %s", e)
        return False


def _renewal_loop():
    """Background thread: rotate credentials at 50% of lease TTL, and
    poll a grants probe every _GRANTS_PROBE_INTERVAL_S in the meantime.

    The probe catches early lease revocations (max_lease_ttl, manual
    revoke, Vault restart) that strip grants from the current role —
    without it, the API stays broken until the next scheduled rotation.
    """
    global _lease_duration, _cred_fetched_at

    while True:
        try:
            elapsed = time.time() - _cred_fetched_at
            remaining = _lease_duration - elapsed
            sleep_time = max(remaining * 0.5, 30)  # At least 30s between rotations
            wake_at = time.time() + sleep_time

            while True:
                chunk = min(_GRANTS_PROBE_INTERVAL_S, wake_at - time.time())
                if chunk <= 0:
                    break
                time.sleep(chunk)
                if not _grants_probe_ok():
                    _try_force_rotate("renewal-loop grants probe failed (SQLSTATE 42501)")
                    break  # exit probe loop; outer loop recalculates timing

            _rotate_credentials()

        except Exception as e:
            logger.error("Credential renewal loop error: %s", e)
            time.sleep(60)  # Back off on error


def _inherited_cred_payload() -> dict | None:
    """P18-F2 — read parent-minted migration creds from an INHERITED fd.

    mod_migrate.py mints the dynamic DB creds ONCE in the parent and hands each
    step subprocess the fd NUMBER only (env MOD_MIGRATE_CRED_FD, the fd opened
    with os.pipe() and passed via pass_fds). The child reads one JSON line
    {user, password, lease_expires_at} and closes it: no Vault login, no
    credential fetch, no renewal thread — the parent owns the lease. The creds
    themselves are NEVER in env/argv/files/logs (env is world-readable via
    /proc/<pid>/environ, argv via ps), so an fd is the only channel.

    Returns None — i.e. fall back to the existing Vault path, never fail closed
    into broken creds — on ANY read/parse error. Always pops the env key so a
    grandchild never inherits an already-spent fd.
    """
    import json  # local: keeps the module import list untouched (no extra hunk)

    raw = os.environ.pop("MOD_MIGRATE_CRED_FD", None)
    if raw is None:
        return None
    try:
        fd = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        with open(fd, "rb", closefd=False) as f:
            line = f.readline()
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        payload = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    user = payload.get("user")
    password = payload.get("password")
    if not user or not password:
        return None
    try:
        expires_at = float(payload.get("lease_expires_at", 0.0))
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at and time.time() >= expires_at:
        # The parent's lease died between mint and exec — do not use dead creds.
        return None
    return {"user": user, "password": password, "lease_expires_at": expires_at}


def initialize():
    """
    Initialize the database connection.
    Called once at startup (from main.py startup event).

    - If Vault is configured: fetch dynamic credentials, start renewal thread
    - No static DATABASE_URL fallback — Vault is the only path to credentials
    - P18-F2: a migration-step child of tools/mod_migrate.py inherits the
      parent's already-minted creds on an fd (MOD_MIGRATE_CRED_FD) and skips
      the Vault login, the credential fetch AND the renewal thread (the parent
      owns the lease). No fd -> behaviour byte-identical to before.
    """
    global _engine, _SessionLocal, _renewal_thread
    global _cur_username, _cur_password, _cred_fetched_at, _lease_duration

    inherited = _inherited_cred_payload()
    if inherited:
        # Values are never logged; only the fact that this process inherited
        # creds is logged.
        logger.info("Using parent-minted migration credentials (inherited fd; "
                    "no Vault round-trip in this process)")
        new_engine = _build_engine(inherited["user"], inherited["password"])
        _swap_engine(new_engine)
        with _lock:
            _cur_username, _cur_password = inherited["user"], inherited["password"]
        _cred_fetched_at = time.time()
        _lease_duration = (
            int(inherited["lease_expires_at"] - _cred_fetched_at)
            if inherited["lease_expires_at"] else 3600
        )
        return  # the parent owns the lease: no renewal thread in a step child

    if _use_vault():
        logger.info("Using OpenBao dynamic database credentials (role: %s)", _VAULT_DB_ROLE)

        # Retry with backoff — Vault Agent or vault-init may still be starting
        max_attempts = 10
        for attempt in range(1, max_attempts + 1):
            if _rotate_credentials():
                break
            if attempt == max_attempts:
                raise RuntimeError(
                    "Failed to obtain database credentials from OpenBao after "
                    f"{max_attempts} attempts. Check that OpenBao is unsealed, "
                    f"OpenBao Agent is running, and database role '{_VAULT_DB_ROLE}' exists."
                )
            wait = min(attempt * 5, 30)
            logger.warning(
                "Vault database creds not ready (attempt %d/%d), retrying in %ds...",
                attempt, max_attempts, wait
            )
            time.sleep(wait)

        # Start background renewal thread
        _renewal_thread = threading.Thread(target=_renewal_loop, daemon=True, name="vault-db-renewal")
        _renewal_thread.start()

    elif os.getenv("DATABASE_URL"):
        logger.info("Using static DATABASE_URL (non-Vault mode)")
        # Boot-window DNS for the static path too (it does not pass through
        # _build_engine): the host is whatever DATABASE_URL names; no '@' ->
        # nothing parseable, so no wait (sqlite/local URLs included).
        _static_url = os.getenv("DATABASE_URL", "")
        if "@" in _static_url and "sqlite" not in _static_url:
            _static_host = _static_url.rsplit("@", 1)[-1].split("/", 1)[0].split(":", 1)[0]
            if _static_host:
                _wait_for_db_host(_static_host, _PG_POOLER_PORT)
        _static_connect_args = _pg_ssl_connect_args()
        if "sqlite" in os.getenv("DATABASE_URL", ""):
            _static_connect_args["check_same_thread"] = False
        static_engine = create_engine(
            os.getenv("DATABASE_URL"),
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=_static_connect_args,
        )
        _swap_engine(static_engine)

    else:
        raise RuntimeError(
            "No database credentials available. "
            "Set VAULT_ADDR + OPENBAO_APPROLE_ROLE_ID + OPENBAO_APPROLE_SECRET_ID for OpenBao "
            "(the old VAULT_APPROLE_* names are still accepted), "
            "or DATABASE_URL for static mode."
        )


def get_engine():
    """Get the current engine (thread-safe)."""
    with _lock:
        return _engine


def get_direct_engine():
    """Engine connecting DIRECTLY to Postgres (bypassing PgBouncer) for the few
    session-pinned consumers transaction pooling would break — notably the
    LISTEN/NOTIFY trigger listener, whose subscription is dropped when a pooled
    connection is returned to PgBouncer between transactions.

    Built lazily from the current Vault credentials with a tiny pool, so it costs
    nothing unless something actually asks for a direct connection. Rebuilt when
    the credentials rotate. Falls back to the pooled engine in static/non-Vault
    mode (no separate creds to build with).
    """
    global _direct_engine, _direct_creds_key
    with _lock:
        user, pw = _cur_username, _cur_password
    if not user or not pw:
        return get_engine()
    key = (user, _PG_HOST, _PG_PORT)
    with _direct_lock:
        if _direct_engine is None or _direct_creds_key != key:
            old = _direct_engine
            _direct_engine = _build_engine(user, pw, host=_PG_HOST, port=_PG_PORT,
                                           pool_size=1, max_overflow=2)
            _direct_creds_key = key
            if old is not None:
                try:
                    old.dispose()
                except Exception:
                    pass
        return _direct_engine


def SessionLocal():
    """Create a new session from the current session factory."""
    with _lock:
        factory = _SessionLocal
    if factory is None:
        raise RuntimeError("Database not initialized. Call database.initialize() first.")
    return factory()


def seed_service_user():
    """Create the platform-service user and API key for internal service-to-service calls."""
    db = SessionLocal()
    try:
        service_username = os.getenv("PLATFORM_SERVICE_USERNAME", "platform-service")
        service_api_key = os.getenv("PLATFORM_SERVICE_API_KEY")

        if not service_api_key:
            logger.warning("PLATFORM_SERVICE_API_KEY not found in environment. Skipping service user creation.")
            return

        service_user = db.query(models.User).filter(models.User.username == service_username).first()

        if not service_user:
            tenant = db.query(models.Tenant).first()
            if not tenant:
                tenant_name = os.getenv('INITIAL_TENANT_NAME', 'ModTech Core').strip('"').strip("'")
                initial_credits = int(os.getenv('INITIAL_TENANT_CREDITS', '10000'))
                tenant = models.Tenant(
                    name=tenant_name, credit_balance=initial_credits, storage_used_bytes=0,
                )
                db.add(tenant)
                db.flush()  # allocate the PK before wrapping (per-tenant Vault key name)
                # 2400CR2i-F3: services.key_provider is denied in the CE carve
                # (open-core/manifest.yaml: CORE/API/services/key_*.py), so the
                # bare import above raised ImportError on every CE boot and the
                # service user was never created. Use the shipped CE-allowed
                # equivalent (services/tenant_key_local.py — same optional_paid
                # pattern as create_tenant.py / crud/secret_cud.py): the local
                # Fernet path is byte-compatible with key_provider's local
                # provider, so DEKs wrap/unwrap identically on both editions.
                # Absence of the PAID module is the only thing we tolerate here.
                from utils.paid_import import optional_paid
                from services import tenant_key_local
                key_provider = optional_paid("services.key_provider") or tenant_key_local
                _tenant_key, tenant.encryption_key = key_provider.generate_and_wrap_tenant_key(
                    tenant.id, db=db)
                db.commit()
                db.refresh(tenant)
                logger.info(f"Created tenant: {tenant.name}")

            # Seed the 4 system roles (Admin / Standard User / Guest /
            # External Collaborator) for this bootstrap tenant. The
            # wizard-driven tenant-creation path in crud.tenant_cud
            # already calls this; the startup-bootstrap path here used
            # to skip it, so a fresh `start-main-server` came up with
            # zero roles in the Platform tenant and the dashboard's
            # role picker was empty. Idempotent — safe on re-entry and
            # never overwrites tenant-customised capability lists.
            try:
                from crud.tenant_cud import ensure_tenant_system_roles
                ensure_tenant_system_roles(db, tenant.id)
                logger.info(f"Seeded system roles for tenant {tenant.id}")
            except Exception as e:
                if is_debug_mode():
                    logger.warning(f"Failed to seed system roles for tenant {tenant.id}: {e}")
                else:
                    logger.warning(f"Failed to seed system roles for tenant {tenant.id}")

            service_user = models.User(
                username=service_username,
                email=f"{service_username}@internal.local",
                tenant_id=tenant.id,
                status=models.UserStatus.ACTIVE,
                onboarding_complete=True,
                first_name="Platform",
                last_name="Service",
                # This account owns PLATFORM_SERVICE_API_KEY and drives the
                # internal node scanner (services/node_scanner). It MUST be
                # flagged as a service account so the node-registration
                # endpoint honours the scanner's ``tenant_id=None`` payload for
                # system metadata instead of falling back to this row's own
                # tenant_id. Without the flag every system node gets a duplicate
                # tenant-scoped twin on each full rescan (routers/node_definitions
                # create_or_update_node_definition, effective_tenant_id logic).
                is_service_account=True,
            )
            db.add(service_user)
            db.commit()
            db.refresh(service_user)
            logger.info(f"Created service user: {service_username}")

        # Heal already-bootstrapped systems whose platform-service row pre-dates
        # the is_service_account fix above. Older deployments created this user
        # with the column defaulting to FALSE, so the node scanner's system
        # metadata (tenant_id=None) was mis-attributed to this row's tenant,
        # producing a duplicate tenant-scoped twin of every system node on each
        # full rescan. Flip it to TRUE idempotently on every startup so the
        # fix self-applies on the next API restart and can't silently regress.
        try:
            if getattr(service_user, "is_service_account", False) is not True:
                service_user.is_service_account = True
                db.add(service_user)
                db.commit()
                logger.info(
                    "Healed platform-service account: is_service_account=True "
                    "(prevents duplicate tenant-scoped system-node twins on rescan)"
                )
        except Exception as e:
            db.rollback()
            if is_debug_mode():
                logger.warning(f"Failed to heal platform-service is_service_account flag: {e}")
            else:
                logger.warning("Failed to heal platform-service is_service_account flag")

        # Heal already-bootstrapped systems whose Platform tenant pre-dates
        # the seeding-on-bootstrap fix above (the service user exists, the
        # tenant exists, but custom_roles is empty). ensure_tenant_system_roles
        # is idempotent — no-op when the 4 roles are already present, fills
        # gaps when partially seeded, never overwrites tenant-customised
        # capability lists. Runs on every startup so a future regression
        # in the bootstrap path self-heals on the next API restart.
        try:
            from crud.tenant_cud import ensure_tenant_system_roles
            ensure_tenant_system_roles(db, service_user.tenant_id)
        except Exception as e:
            if is_debug_mode():
                logger.warning(f"Startup system-role heal failed for tenant {service_user.tenant_id}: {e}")
            else:
                logger.warning(f"Startup system-role heal failed for tenant {service_user.tenant_id}")

        key_prefix = service_api_key[:8]
        existing_key = db.query(models.ApiKey).filter(
            models.ApiKey.user_id == service_user.id,
            models.ApiKey.key_prefix == key_prefix,
            models.ApiKey.deleted_at.is_(None)
        ).first()

        if not existing_key:
            hashed_key = hash_api_key(service_api_key)
            api_key = models.ApiKey(
                user_id=service_user.id,
                key_prefix=key_prefix,
                hashed_key=hashed_key,
                description="Platform service account API key for internal calls"
            )
            db.add(api_key)
            db.commit()
            if is_debug_mode():
                logger.info(f"Created API key for service user (prefix: {key_prefix[:4]}****)")
            else:
                logger.info("Created API key for service user (key redacted - set DEBUG_MODE=true to view)")
        else:
            logger.info("Service API key already exists")

    except Exception as e:
        if is_debug_mode():
            logger.error(f"Error creating service user: {e}")
        else:
            # 2400CR2i-F3: name the missing module for ImportErrors — the bare
            # type name made CE-boot ImportErrors undiagnosable in the logs.
            # A module path is not a secret; nothing else about the exception
            # (message, args) is echoed, so no env/secret values can leak.
            missing = getattr(e, "name", None)
            if missing and isinstance(e, (ImportError, ModuleNotFoundError)):
                logger.error("Error creating service user (%s: %s)", type(e).__name__, missing)
            else:
                logger.error("Error creating service user (%s)", type(e).__name__)
        db.rollback()
        return False  # create_tables() retries (bounded) — never swallowed silently
    finally:
        db.close()


_SCHEMA_MIGRATION_VERSION = "2026-10-15c"  # BUMP this whenever _migrate_schema_impl changes


def _migrate_schema(engine):
    """Run the schema migration AT MOST ONCE PER VERSION, under a cross-process lock.

    Two failure modes this guards against, both of which repeatedly took the API
    down (platform-wide 502 storm):
      1. `create_tables()` runs at MODULE IMPORT (main.py), so all 8 uvicorn
         workers ran the migration on every boot.
      2. The migration's `ALTER TABLE node_definitions DROP CONSTRAINT ...` takes
         ACCESS EXCLUSIVE. It queues behind the WorkflowBuilder's constant
         node_definitions polling, and its pending exclusive request then blocks
         every NEW reader — a self-sustaining lock convoy that never drains while
         the UI keeps polling, exhausting the connection pool.
    Fix: a `mod_schema_version` marker gates the DDL. The FIRST run for a given
    version applies it (serialized by a Postgres advisory lock so only one worker
    does it); every subsequent worker/boot sees the marker and returns WITHOUT any
    DDL — no exclusive lock, no convoy. Bump _SCHEMA_MIGRATION_VERSION to re-run.
    """
    import logging
    log = logging.getLogger(__name__)
    from sqlalchemy import text as sa_text

    _MIGRATE_LOCK_KEY = 918273645  # constant, unique to schema-migration serialization
    lock_conn = None
    try:
        lock_conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        lock_conn.execute(sa_text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATE_LOCK_KEY})
    except Exception as e:  # never let lock trouble block startup
        log.warning("migrate: advisory lock unavailable (%s); proceeding unlocked", e)
        if lock_conn is not None:
            try:
                lock_conn.close()
            except Exception:
                pass
        lock_conn = None
    try:
        # Version gate — skip ALL migration DDL when this version is already
        # applied (the marker table is tiny and never touches node_definitions,
        # so reading it can't convoy). This is what stops the per-boot ALTER.
        try:
            with engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as _vc:
                # Elevate to the static owner role on THIS connection only, and
                # RESET it on the SAME connection before it returns to the pool
                # (plain SET ROLE is session-scoped and survives rollback-on-
                # checkin, so resetting a different connection would leak the
                # elevated role into whichever request reuses this one next).
                _role_ok = False
                if _PG_OWNER_ROLE:
                    try:
                        _vc.execute(sa_text(
                            f'SET ROLE "{_PG_OWNER_ROLE}"'))
                        _role_ok = True
                    except Exception:
                        pass  # best-effort; continue as current role
                try:
                    _vc.execute(sa_text(
                        "CREATE TABLE IF NOT EXISTS mod_schema_version "
                        "(version text PRIMARY KEY, applied_at timestamptz DEFAULT now())"))
                    _done = _vc.execute(sa_text(
                        "SELECT 1 FROM mod_schema_version WHERE version=:v"),
                        {"v": _SCHEMA_MIGRATION_VERSION}).first()
                finally:
                    if _role_ok:
                        try:
                            _vc.execute(sa_text("RESET ROLE"))
                        except Exception:
                            pass
            if _done is not None:
                log.info("migrate: schema version %s already applied — skipping DDL",
                         _SCHEMA_MIGRATION_VERSION)
                return
        except Exception as e:
            log.warning("migrate: version gate check failed (%s); running migration", e)
        _migrate_schema_impl(engine)
        try:
            with engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as _vc:
                _rec_role_ok = False
                if _PG_OWNER_ROLE:
                    try:
                        _vc.execute(sa_text(
                            f'SET ROLE "{_PG_OWNER_ROLE}"'))
                        _rec_role_ok = True
                    except Exception:
                        pass  # best-effort; continue as current role
                try:
                    _vc.execute(sa_text(
                        "INSERT INTO mod_schema_version(version) VALUES (:v) "
                        "ON CONFLICT DO NOTHING"), {"v": _SCHEMA_MIGRATION_VERSION})
                finally:
                    if _rec_role_ok:
                        try:
                            _vc.execute(sa_text("RESET ROLE"))
                        except Exception:
                            pass
            log.info("migrate: applied + recorded schema version %s", _SCHEMA_MIGRATION_VERSION)
        except Exception as e:
            log.warning("migrate: could not record schema version (%s)", e)
    finally:
        if lock_conn is not None:
            try:
                lock_conn.execute(sa_text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATE_LOCK_KEY})
            except Exception:
                pass
            try:
                lock_conn.close()
            except Exception:
                pass


def _migrate_schema_impl(engine):
    """Add new columns/enum values to existing tables that create_all won't touch.

    All operations are wrapped in try/except so migration failures are non-fatal.
    New deployments get the full schema from create_all(); this only matters for
    existing databases that need the new columns/enum values added incrementally.

    IMPORTANT: ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
    Enum additions use autocommit connections. Column additions use individual
    transactions so one failure doesn't cascade to others.
    """
    import logging
    log = logging.getLogger(__name__)

    from sqlalchemy import inspect, text as sa_text

    try:
        insp = inspect(engine)
    except Exception:
        return  # Can't inspect — skip migration

    # Check if api_task_queue table exists at all (skip on fresh deploy)
    if 'api_task_queue' not in insp.get_table_names():
        return

    # --- Phase 1: Enum additions (must run outside transactions / autocommit) ---
    #
    # NOTE (foot-gun): the `apitaskstatus` enum stores the UPPERCASE Python enum
    # MEMBER NAMES (PENDING/ASSIGNED/CLAIMED/... — SQLAlchemy's Enum() default),
    # which is why 'ASSIGNED' is added below. The lowercase 'cancelled' label is
    # a STRAY DUPLICATE of the canonical 'CANCELLED' — harmless data but a live
    # trap: raw SQL that writes/compares lowercase status literals ('assigned',
    # 'pending', ...) fails with InvalidTextRepresentation. Always use
    # `ApiTaskStatus.<MEMBER>.name` in raw SQL, never `.value`. (See the NOAH
    # release enum-case bug fixed in services/noah.py.)
    enum_additions = [
        ("apitaskstatus", "ASSIGNED", "BEFORE 'CLAIMED'"),
        ("apitaskstatus", "cancelled", None),  # stray lowercase dup — see NOTE above
        ("taskstatus", "blocked", None),
    ]
    for type_name, value, position in enum_additions:
        try:
            with engine.connect() as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                exists = conn.execute(sa_text(
                    f"SELECT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = :val "
                    f"AND enumtypid = (SELECT oid FROM pg_type WHERE typname = :typ))"
                ), {"val": value, "typ": type_name}).scalar()
                if not exists:
                    pos_clause = f" {position}" if position else ""
                    conn.execute(sa_text(
                        f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'{pos_clause}"
                    ))
                    log.info("Schema migration: added '%s' to %s", value, type_name)
        except Exception as e:
            log.debug("Schema migration: could not add '%s' to %s (non-fatal): %s", value, type_name, e)

    # --- Phase 2: Column additions (each in its own transaction so failures are isolated) ---
    def _run_migration(label, sql_statements):
        """Run migration step in its own transaction. Failures don't cascade.

        A PERMISSION failure is escalated to WARNING rather than swallowed at
        DEBUG. Found 2026-08-15: the API runs as a Vault DYNAMIC role, so any
        table it creates is owned by an ephemeral credential
        (`v-approle-api-read-...`). Once that rotates, the next role cannot
        ALTER what the previous one made, and every future migration against
        that table fails with `must be owner of table`. Three tables were in
        that state and nobody could have known, because the failure was logged
        at DEBUG as "non-fatal" — which it is not: it means the schema silently
        stopped tracking the models.
        """
        try:
            with engine.begin() as conn:
                # Switch to the static owner role before running migration DDL.
                # Vault's creation_statements grant every dynamic role `IN ROLE
                # "<POSTGRES_USER>"` (openbao-init.sh / vault-init.sh), so
                # SET LOCAL ROLE is a best-effort elevation: if the role doesn't
                # exist or the current role isn't a member, we fall through to
                # the current role (dev / sqlite / unexpected setups).
                # IMPORTANT: a failed SET LOCAL ROLE inside this engine.begin()
                # transaction marks the WHOLE transaction aborted in Postgres —
                # a bare try/except around just this statement does NOT recover
                # it; every statement below would then fail with "current
                # transaction is aborted" and get silently logged as
                # "non-fatal". Run it inside its own SAVEPOINT so a failure
                # rolls back only the SET LOCAL ROLE, leaving the outer
                # transaction usable for the real migration statements.
                if _PG_OWNER_ROLE:
                    try:
                        with conn.begin_nested():
                            conn.execute(sa_text(
                                f'SET LOCAL ROLE "{_PG_OWNER_ROLE}"'))
                    except Exception:
                        pass  # best-effort; continue as current role
                for stmt in sql_statements:
                    conn.execute(sa_text(stmt))
                log.info("Schema migration: %s", label)
        except Exception as e:
            if "must be owner" in str(e) or "InsufficientPrivilege" in type(e).__name__:
                log.warning(
                    "Schema migration BLOCKED BY OWNERSHIP: %s — the table is "
                    "owned by a rotated Vault role, so this migration can "
                    "never succeed until ownership is reassigned to the static "
                    "role. Run: ALTER TABLE <t> OWNER TO \"%s\". Details: %s",
                    label, _PG_OWNER_ROLE, e)
            else:
                log.debug("Schema migration: %s (non-fatal): %s", label, e)

    # DB-level connection backstop — set as a DATABASE default so it applies to
    # EVERY backend regardless of the (Vault-rotated) role AND survives pgbouncer
    # transaction pooling, which strips the per-session SET the engine connect
    # event issues (that SET never reaches the server backend, so it cannot be
    # relied on here). idle_in_transaction_session_timeout reaps a transaction
    # that has been opened and then left IDLE — the leak that repeatedly
    # exhausted the pool and took the API down ("backend down" 502 storm) when a
    # handler held its get_db session across a slow/blocking call. It only ever
    # kills an IDLE-in-transaction backend: active queries, plain-idle pooled
    # connections, and long-running worker TASKS (which hold no DB transaction)
    # are untouched. Deliberately NO global statement_timeout here — that would
    # cap legitimately long queries / backfill migrations. Idempotent, every start.
    try:
        _dbname = engine.url.database
        if _dbname:
            # 120s, not 10s: 10s guillotined legitimately-slow-but-recovering
            # requests under load (a query would run, then the API worker was too
            # starved to COMMIT within 10s → "server closed the connection" 500s
            # on /runs). 120s still reaps a genuinely-leaked idle txn while giving
            # a busy handler room. The real cure is not holding a txn across slow
            # work (e.g. the runs-list query is now lightweight); this is a backstop.
            _iit_ms = int(os.getenv("DB_IDLE_IN_TXN_TIMEOUT_MS", "120000"))
            with engine.begin() as conn:
                conn.execute(sa_text(
                    f'ALTER DATABASE "{_dbname}" SET idle_in_transaction_session_timeout = \'{_iit_ms}\''))
            log.info("Schema migration: DB-level idle_in_transaction backstop set (%dms)", _iit_ms)
    except Exception as e:
        log.warning("Could not set DB-level idle-in-transaction backstop (non-fatal): %s", e)

    # Refresh inspector after enum additions
    try:
        insp = inspect(engine)
    except Exception:
        return

    tables = insp.get_table_names()

    def _get_cols(table):
        if table in tables:
            return {c['name'] for c in insp.get_columns(table)}
        return set()

    # api_task_queue columns
    atq_cols = _get_cols('api_task_queue')
    for col_name, col_def in {
        'assigned_to_worker': "VARCHAR(255) REFERENCES worker_licenses(worker_name)",
        'start_after': "TIMESTAMPTZ",
        'phase_plan': "JSONB",
        'scheduling_metadata': "JSONB",
        'scheduling_version': "INTEGER DEFAULT 0 NOT NULL",
        # Phase 2 (variant-aware scheduler): PENDING tasks get
        # flagged unrunnable=true by the 5-min sweep when no active
        # worker's hardware_tags intersect the task's gpu_variants
        # keys. The Dashboard System Health tab banners on this.
        'unrunnable': "BOOLEAN NOT NULL DEFAULT FALSE",
        'unrunnable_reason': "TEXT",
    }.items():
        if col_name not in atq_cols:
            _run_migration(f"added {col_name} to api_task_queue",
                           [f"ALTER TABLE api_task_queue ADD COLUMN {col_name} {col_def}"])

    # Scheduler index
    _run_migration("scheduler index",
                   ["CREATE INDEX IF NOT EXISTS ix_api_task_queue_assigned "
                    "ON api_task_queue (status, assigned_to_worker, start_after)"])
    _run_migration("unrunnable task sweep index",
                   ["CREATE INDEX IF NOT EXISTS ix_api_task_queue_unrunnable "
                    "ON api_task_queue (unrunnable)"])

    # node_definitions columns
    nd_cols = _get_cols('node_definitions')
    for col_name, col_def in {
        'accepted_formats': "JSONB",
        'gpu_variants': "JSONB",
        'allowed_egress': "JSONB",
        'image_size_bytes': "BIGINT DEFAULT 0 NOT NULL",
        # P9b — per-variant port/build outcomes + smoke verification
        'incompatible_reasons': "JSONB",
        'variant_verification_status': "JSONB",
        'not_recommended_variants': "JSONB",
        'variant_performance': "JSONB",
        # Tenant-internal marketplace lifecycle: creator submits a private
        # draft for tenant-admin review -> admin accepts -> published.
        'review_requested': "BOOLEAN NOT NULL DEFAULT FALSE",
    }.items():
        if col_name not in nd_cols:
            _run_migration(f"added {col_name} to node_definitions",
                           [f"ALTER TABLE node_definitions ADD COLUMN {col_name} {col_def}"])

    # node_runs.error_message
    nr_cols = _get_cols('node_runs')
    if 'error_message' not in nr_cols:
        _run_migration("added error_message to node_runs",
                       ["ALTER TABLE node_runs ADD COLUMN error_message TEXT"])

    # api_keys scope columns (unified-authorization Gate 34). Additive + nullable/
    # defaulted, so existing keys stay UNSCOPED (full owner perms) — a read-only
    # key is refused every mutating capability in authorize() (Gate 35/36), and
    # capability_scope is an optional "RESOURCE_TYPE:capability" allowlist.
    ak_cols = _get_cols('api_keys')
    for col_name, col_def in {
        'is_read_only': "BOOLEAN NOT NULL DEFAULT FALSE",
        'capability_scope': "JSONB",
    }.items():
        if col_name not in ak_cols:
            _run_migration(f"added {col_name} to api_keys",
                           [f"ALTER TABLE api_keys ADD COLUMN {col_name} {col_def}"])

    # tenant_email_configs explicit SMTP encryption + self-signed opt-in (migration
    # 20261109b). Same additive DDL as the migration, so an API that boots before
    # mod-migrate has run does not 500 on the first email-config read/send.
    # NULL / FALSE reproduce the old port-based behaviour.
    tec_cols = _get_cols('tenant_email_configs')
    if tec_cols:
        for col_name, col_def in {
            'smtp_security': "VARCHAR(16)",
            'smtp_allow_selfsigned': "BOOLEAN NOT NULL DEFAULT FALSE",
        }.items():
            if col_name not in tec_cols:
                _run_migration(f"added {col_name} to tenant_email_configs",
                               [f"ALTER TABLE tenant_email_configs ADD COLUMN IF NOT EXISTS {col_name} {col_def}"])

    # resource_permissions grant-metadata columns (unified-authorization Gate 30).
    # Additive + nullable/defaulted, so existing rows are unaffected. These carry
    # the sharing-grant metadata the legacy Permission table held (level, external
    # flag, invitation context, group grantee), making the grant losslessly
    # reconstructable from ResourcePermission so the legacy table can be retired.
    rp_cols = _get_cols('resource_permissions')
    for col_name, col_def in {
        'keycloak_group_id': "VARCHAR(255)",
        'permission_level': "permissionlevel",  # existing enum type (shared with legacy permissions.level)
        'is_external_user': "BOOLEAN NOT NULL DEFAULT FALSE",
        'invitation_context': "JSONB",
    }.items():
        if col_name not in rp_cols:
            _run_migration(f"added {col_name} to resource_permissions",
                           [f"ALTER TABLE resource_permissions ADD COLUMN {col_name} {col_def}"])

    # Gate 30: RETIRE the legacy `permissions` table. Every grant now lives in
    # ResourcePermission; the Permission model class is DELETED so create_all()
    # no longer materialises the table. This runs ONLY while the legacy table is
    # still present (a fresh existing-deploy), so it is a true one-time migration
    # and never errors on later boots once the table is gone.
    if 'permissions' in tables:
        # STEP 1 (must precede the drop): carry the sharing-grant metadata the
        # legacy table held onto the ResourcePermission rows the old dual-write
        # mirrored but left without a level. Without this, EXISTING shares on a
        # real deployment would lose their level/external/invitation metadata (the
        # read model surfaces only rows whose permission_level is set) the moment
        # the table is dropped — access would still resolve, but the sharing UI
        # would stop showing/managing pre-existing grants. Raw SQL because the ORM
        # model is deleted. Matches grantee (user/team/group) + cog; only rows not
        # already carrying a level (i.e. not written by the new RP-only path).
        _run_migration("backfill grant metadata from legacy permissions (Gate 30)", [
            """
            UPDATE resource_permissions rp
               SET permission_level  = p.level,
                   is_external_user   = COALESCE(p.is_external_user, false),
                   invitation_context = p.invitation_context,
                   granted_by_user_id = COALESCE(rp.granted_by_user_id, p.granted_by_user_id)
              FROM permissions p
             WHERE rp.resource_type = 'COG'
               AND rp.permission_level IS NULL
               AND rp.is_active AND rp.revoked_at IS NULL
               AND p.target_type = 'COG'
               AND p.deleted_at IS NULL
               AND rp.resource_id = p.target_id
               AND (
                     (p.user_id IS NOT NULL AND rp.user_id = p.user_id)
                  OR (p.team_id IS NOT NULL AND rp.team_id = p.team_id)
                  OR (p.keycloak_group_id IS NOT NULL AND rp.keycloak_group_id = p.keycloak_group_id)
               )
            """,
        ])
        # STEP 2: drop the two FK constraints that referenced it (their columns now
        # hold read-model grant ids), then the table + its implicit composite type.
        _run_migration("retire legacy permissions table (Gate 30)", [
            "ALTER TABLE IF EXISTS permission_requests "
            "DROP CONSTRAINT IF EXISTS permission_requests_granted_permission_id_fkey",
            "ALTER TABLE IF EXISTS permission_audit_logs "
            "DROP CONSTRAINT IF EXISTS permission_audit_logs_permission_id_fkey",
            "DROP TABLE IF EXISTS permissions CASCADE",
            "DROP TYPE IF EXISTS permissions CASCADE",
        ])

    # workflow_run_events table — orchestrator / traffic-cop event
    # log surfaced in the run-detail Logs panel (#12,
    # launch-day-followup-2). On fresh DBs create_all() materializes
    # the table; this migration covers existing deploys. Uses the
    # IF NOT EXISTS form so re-running is a no-op.
    _run_migration("create workflow_run_events table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS workflow_run_events ("
        "  id BIGSERIAL PRIMARY KEY, "
        "  workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs(id), "
        "  ts TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(), "
        "  source VARCHAR(32) NOT NULL, "
        "  level VARCHAR(16) NOT NULL DEFAULT 'info', "
        "  message TEXT NOT NULL, "
        "  event_metadata JSONB)",
        "CREATE INDEX IF NOT EXISTS idx_workflow_run_events_run "
        "  ON workflow_run_events(workflow_run_id, ts)",
    ])

    # changelog_entries.audience — 'user_facing' rows surface in the
    # tenant What's-New widget; 'internal' rows are admin-only.
    # Default 'internal' so unset rows never leak. Pre-existing rows
    # land in 'internal' too — admins re-promote real release notes
    # via Dashboard → What's New (the prior auto-stamp to 'user_facing'
    # surfaced Playwright smoke residue to tenants).
    ce_cols = _get_cols('changelog_entries')
    if ce_cols and 'audience' not in ce_cols:
        _run_migration("added audience to changelog_entries", [
            "ALTER TABLE changelog_entries ADD COLUMN audience VARCHAR(20) NOT NULL DEFAULT 'internal'",
        ])

    # bucket-e §6 — widen prediction_logs.inference_mode from VARCHAR(10)
    # to VARCHAR(50). The original 10-char limit predates the
    # `synthetic-default` (17-char) and other longer mode strings the
    # scheduler now writes. The varchar(10) ceiling caused INSERTs to
    # error with `StringDataRightTruncation`, blocking every
    # prediction_logs row write (Tim flagged 23,640 errors over 5h).
    # Idempotent: the migration is a no-op once the column is already
    # 50-wide.
    if 'prediction_logs' in tables:
        try:
            for col in insp.get_columns('prediction_logs'):
                if col['name'] == 'inference_mode':
                    if col.get('type') is not None and getattr(col['type'], 'length', None) and col['type'].length < 50:
                        _run_migration(
                            "widen prediction_logs.inference_mode to varchar(50)",
                            ["ALTER TABLE prediction_logs ALTER COLUMN inference_mode TYPE VARCHAR(50)"],
                        )
                    break
        except Exception as e:
            log.debug("Schema migration: prediction_logs inference_mode check (non-fatal): %s", e)

    # Workflow.is_system — suppress platform-internal workflows (Node
    # Wizard build pipelines, system maintenance like HarborScanFollowup)
    # from the user-facing load-workflow picker. Idempotent column add +
    # one-shot backfill for existing rows based on the conventions we
    # know today: NULL owner (system maintenance) or
    # `build-<safe>-<10+digit-timestamp>` name (Node Wizard build).
    if 'workflows' in tables:
        try:
            wf_cols = {c['name'] for c in insp.get_columns('workflows')}
            if 'is_system' not in wf_cols:
                _run_migration("workflows.is_system column + backfill", [
                    "ALTER TABLE workflows ADD COLUMN is_system BOOLEAN NOT NULL DEFAULT FALSE",
                    # Backfill: NULL-owner workflows are pure system maintenance.
                    "UPDATE workflows SET is_system = TRUE WHERE created_by_user_id IS NULL",
                    # Backfill: Node Wizard build pipeline workflows
                    # (name = 'build-<safe>-<epoch-seconds>', where epoch
                    # seconds is at least 10 digits since year 2001).
                    "UPDATE workflows SET is_system = TRUE "
                    "WHERE name ~ '^build-.+-[0-9]{10,}$' AND is_system = FALSE",
                ])
            if 'schedule_paused' not in wf_cols:
                _run_migration("workflows.schedule_paused column (DAG schedule pause)", [
                    "ALTER TABLE workflows ADD COLUMN schedule_paused BOOLEAN NOT NULL DEFAULT FALSE",
                ])
        except Exception as e:
            log.debug("Schema migration: workflows.is_system check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §wizard-agentic-loop — wizard_test_runs
    # lineage table. One row per iteration of the wizard's agentic loop.
    # CREATE TABLE IF NOT EXISTS is idempotent on fresh deploys; on
    # rotated-Vault deploys where the current role lacks ownership of
    # an existing wizard_test_runs table, the migration is a no-op so
    # it doesn't 401 the API startup.
    _run_migration("create wizard_test_runs table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS wizard_test_runs ("
        " id BIGSERIAL PRIMARY KEY,"
        " wizard_session_id VARCHAR(36) NOT NULL REFERENCES support_chat_sessions(id) ON DELETE CASCADE,"
        " iteration_n INTEGER NOT NULL,"
        " candidate_graph_json JSONB NOT NULL,"
        " test_input_descriptor JSONB,"
        " workflow_run_id BIGINT,"
        " output_critic_verdict VARCHAR(32),"
        " output_critic_reason TEXT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT uq_wizard_test_runs_session_iter UNIQUE (wizard_session_id, iteration_n)"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_wizard_test_runs_session_iter "
        "ON wizard_test_runs (wizard_session_id, iteration_n)",
        "CREATE INDEX IF NOT EXISTS ix_wizard_test_runs_workflow_run "
        "ON wizard_test_runs (workflow_run_id)",
    ])

    # wizard-library-profiler — learned per-library GPU-requirement
    # facts (cache layer over PyPI / curated seed / declared deps).
    # The wizard's analyze step consults this before pre-filling
    # process_type so a user importing nerfstudio gets CUDA instead
    # of CPU. Refresh logic in services/library_profiler.py.
    _run_migration("create library_profiles table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS library_profiles ("
        " name VARCHAR(255) PRIMARY KEY,"
        " gpu_required VARCHAR(16) NOT NULL,"
        " evidence VARCHAR(500),"
        " learned_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")",
    ])
    # Move C — install_recipe_by_accel column for known-working install
    # steps per (library, accelerator). Idempotent ALTER (PG-only ADD
    # COLUMN IF NOT EXISTS) so fresh deploys + existing deployments
    # both end up with the column present.
    _run_migration("add library_profiles.install_recipe_by_accel column (idempotent)", [
        "ALTER TABLE library_profiles "
        "ADD COLUMN IF NOT EXISTS install_recipe_by_accel JSONB",
    ])
    # Move D — install_docs_excerpt + fetch timestamp for README-derived
    # install hints. Cold-start fallback for the repair LLM when no
    # known-working recipe is captured yet.
    _run_migration("add library_profiles.install_docs columns (idempotent)", [
        "ALTER TABLE library_profiles "
        "ADD COLUMN IF NOT EXISTS install_docs_excerpt TEXT",
        "ALTER TABLE library_profiles "
        "ADD COLUMN IF NOT EXISTS install_docs_fetched_at TIMESTAMPTZ",
    ])
    # stack-ha-reconciliation §2 — desired state + HA class on persistent_stacks.
    #
    # Both defaults are chosen so an upgrade changes nothing about how existing
    # stacks behave. `desired_state` defaults to running because every stack
    # that predates the column was meant to be running — nothing had been able
    # to express otherwise. `ha_class` defaults to `external`, the one class
    # that is never auto-re-placed: the platform cannot know what someone
    # else's stack keeps, and an upgrade that silently made existing stacks
    # eligible for automatic movement could move a database.
    _run_migration("add stack_observations.last_running_at (idempotent)", [
        "ALTER TABLE stack_observations "
        "ADD COLUMN IF NOT EXISTS last_running_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_stack_observations_last_running_at "
        "ON stack_observations (last_running_at)",
        # Seed from what we already know. A row currently reporting running>0
        # IS running, so its last-running time is its observation time.
        # Without this, every stack already up at upgrade time reads
        # "never seen running", and the reconciler falls through to its
        # never-deployed branch instead of its gap branch — so the first real
        # failure after an upgrade would go unnoticed until the row happened to
        # be observed running again.
        "UPDATE stack_observations SET last_running_at = observed_at "
        "WHERE running > 0 AND last_running_at IS NULL",
    ])

    # Per-stack serving VRAM the worker samples each beat (stack-training-and-
    # prewarm — the real FIT_GATE_MEASURED_VRAM writer). Nullable with no
    # default: NULL is "unmeasured" (no GPU / older worker / not running), which
    # must stay distinct from a genuine 0, so the fit-gate can degrade to the
    # declared VRAM. No backfill — there is nothing to seed it from until the
    # first beat off a VRAM-sampling worker lands.
    _run_migration("add stack_observations.observed_vram_gb (idempotent)", [
        "ALTER TABLE stack_observations "
        "ADD COLUMN IF NOT EXISTS observed_vram_gb DOUBLE PRECISION",
    ])

    # LLM-gateway (SMG) queue metrics captured per beat (Group R 27.5a).
    # Nullable, no backfill: NULL means "no gateway metrics reported yet" —
    # non-LLM stacks and pre-upgrade workers simply never populate it.
    _run_migration("add stack_observations.gateway_metrics (idempotent)", [
        "ALTER TABLE stack_observations "
        "ADD COLUMN IF NOT EXISTS gateway_metrics JSONB",
    ])

    _stack_cols = _get_cols('persistent_stacks')
    _run_migration("add persistent_stacks desired_state + ha_class (idempotent)", [
        "ALTER TABLE persistent_stacks "
        "ADD COLUMN IF NOT EXISTS desired_state VARCHAR(20) NOT NULL DEFAULT 'running'",
        "ALTER TABLE persistent_stacks "
        "ADD COLUMN IF NOT EXISTS ha_class VARCHAR(20) NOT NULL DEFAULT 'external'",
    ])
    # One-time backfill, gated on the column having just been created.
    #
    # A stack already torn down is not desired running; without this the
    # reconciler would read every historical teardown as a gap and redeploy
    # them all on its first pass.
    #
    # Gated because this must NOT re-run on later boots. An operator who sets a
    # failed stack's desired_state back to running — the ordinary way to ask
    # for a retry — would otherwise have it silently flipped back to stopped by
    # the next API restart, and the retry would never happen.
    if 'desired_state' not in _stack_cols:
        _run_migration("backfill persistent_stacks.desired_state (one-time)", [
            "UPDATE persistent_stacks SET desired_state = 'stopped' "
            "WHERE status IN ('torn_down', 'failed')",
        ])

    # caddy-stack-ingress §2 — opt-in PUBLIC exposure on persistent_stacks.
    #
    # Additive and inert: `exposed` defaults FALSE, so an upgrade exposes no
    # existing stack — the magic front door is opt-in per stack. `public_hostname`
    # is the computed MOD-run FQDN (t<tid>-<stackid>.apps.<BASE_DOMAIN>), populated
    # only while a stack is exposed and cleared when exposure is turned off;
    # `custom_domain` is an optional BYO domain. Both nullable — nothing is
    # exposed until an operator flips it.
    _run_migration("add persistent_stacks exposure columns (idempotent)", [
        "ALTER TABLE persistent_stacks "
        "ADD COLUMN IF NOT EXISTS exposed BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE persistent_stacks "
        "ADD COLUMN IF NOT EXISTS public_hostname VARCHAR(255)",
        "ALTER TABLE persistent_stacks "
        "ADD COLUMN IF NOT EXISTS custom_domain VARCHAR(255)",
    ])

    # Item #10 — recipe confidence scoring (success count per accelerator).
    _run_migration("add library_profiles.successful_build_count column (idempotent)", [
        "ALTER TABLE library_profiles "
        "ADD COLUMN IF NOT EXISTS successful_build_count_by_accel JSONB",
    ])

    # workflow-agentic-execution-loop §workflow-node-output-cache —
    # content-addressable memoization keyed by (image_digest,
    # input_content_hash, config_hash, tenant_id). Per-tenant scoping
    # prevents cross-tenant cache hits.
    _run_migration("create node_output_cache table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS node_output_cache ("
        " id BIGSERIAL PRIMARY KEY,"
        " image_digest VARCHAR(80) NOT NULL,"
        " input_content_hash VARCHAR(64) NOT NULL,"
        " config_hash VARCHAR(64) NOT NULL,"
        " tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
        " output_pointer VARCHAR(512) NOT NULL,"
        " output_content_hash VARCHAR(64) NOT NULL,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT uq_node_output_cache_key UNIQUE (image_digest, input_content_hash, config_hash, tenant_id)"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_node_output_cache_tenant "
        "ON node_output_cache (tenant_id)",
        "CREATE INDEX IF NOT EXISTS ix_node_output_cache_last_accessed "
        "ON node_output_cache (last_accessed_at)",
    ])

    # workflow-agentic-execution-loop §workflow-run-execution — add `purpose`
    # column to workflow_runs distinguishing production runs from sandbox
    # test-runs from event-triggered runs. Idempotent.
    if 'workflow_runs' in tables:
        try:
            wr_cols = {c['name'] for c in insp.get_columns('workflow_runs')}
            if 'purpose' not in wr_cols:
                _run_migration("workflow_runs.purpose column + backfill", [
                    "ALTER TABLE workflow_runs ADD COLUMN purpose VARCHAR(32) NOT NULL DEFAULT 'production'",
                    "UPDATE workflow_runs SET purpose = 'production' WHERE purpose IS NULL",
                    "CREATE INDEX IF NOT EXISTS ix_workflow_runs_purpose ON workflow_runs (purpose)",
                ])
            if 'paused' not in wr_cols:
                _run_migration("workflow_runs.paused column (Airflow-style mid-run pause)", [
                    "ALTER TABLE workflow_runs ADD COLUMN paused BOOLEAN NOT NULL DEFAULT FALSE",
                ])
            if 'drain_nodes' not in wr_cols:
                _run_migration("workflow_runs.drain_nodes column (cancel-downstream drain)", [
                    "ALTER TABLE workflow_runs ADD COLUMN drain_nodes JSONB",
                ])
            if 'rerun_nodes' not in wr_cols:
                _run_migration("workflow_runs.rerun_nodes column (re-run + downstream)", [
                    "ALTER TABLE workflow_runs ADD COLUMN rerun_nodes JSONB",
                ])
            if 'paused_nodes' not in wr_cols:
                _run_migration("workflow_runs.paused_nodes column (per-node pause)", [
                    "ALTER TABLE workflow_runs ADD COLUMN paused_nodes JSONB",
                ])
        except Exception as e:
            log.debug("Schema migration: workflow_runs.purpose check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §workflow-node-output-cache —
    # per-tenant gate for the node output cache read-short-circuit.
    # Default false; writes happen always-on regardless so flipping
    # the flag on picks up immediate benefit from accumulated cache.
    if 'tenants' in tables:
        try:
            tenant_cols = {c['name'] for c in insp.get_columns('tenants')}
            if 'output_cache_enabled' not in tenant_cols:
                _run_migration("tenants.output_cache_enabled column", [
                    "ALTER TABLE tenants ADD COLUMN output_cache_enabled "
                    "BOOLEAN NOT NULL DEFAULT FALSE",
                ])
        except Exception as e:
            log.debug("Schema migration: tenants.output_cache_enabled check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §port-type-enforcement — per-tenant
    # gate for auto-bridging incompatible edges with adapter nodes.
    # Default false; the wizard still hard-rejects type mismatches when
    # off, so existing behaviour is unchanged.
    if 'tenants' in tables:
        try:
            tenant_cols = {c['name'] for c in insp.get_columns('tenants')}
            if 'port_type_enforcement_enabled' not in tenant_cols:
                _run_migration("tenants.port_type_enforcement_enabled column", [
                    "ALTER TABLE tenants ADD COLUMN port_type_enforcement_enabled "
                    "BOOLEAN NOT NULL DEFAULT FALSE",
                ])
        except Exception as e:
            log.debug("Schema migration: tenants.port_type_enforcement_enabled check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §control-flow-primitives —
    # per-tenant gate for executing conditional / fan_out_over_list /
    # retry_with_params nodes. Default false; the orchestrator
    # rejects workflows containing these primitives when off.
    if 'tenants' in tables:
        try:
            tenant_cols = {c['name'] for c in insp.get_columns('tenants')}
            if 'control_flow_primitives_enabled' not in tenant_cols:
                _run_migration("tenants.control_flow_primitives_enabled column", [
                    "ALTER TABLE tenants ADD COLUMN control_flow_primitives_enabled "
                    "BOOLEAN NOT NULL DEFAULT FALSE",
                ])
        except Exception as e:
            log.debug("Schema migration: tenants.control_flow_primitives_enabled check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §wizard-agentic-loop — per-tenant
    # gate for the wizard agent loop driver (compose → test-input →
    # dispatch → critic → iterate). Default false; legacy wizard
    # behaviour preserved when off. WIZARD_AGENT_MAX_ITERATIONS bound
    # lives in services/wizard_agent.py as a module constant.
    if 'tenants' in tables:
        try:
            tenant_cols = {c['name'] for c in insp.get_columns('tenants')}
            if 'wizard_agentic_loop_enabled' not in tenant_cols:
                _run_migration("tenants.wizard_agentic_loop_enabled column", [
                    "ALTER TABLE tenants ADD COLUMN wizard_agentic_loop_enabled "
                    "BOOLEAN NOT NULL DEFAULT FALSE",
                ])
        except Exception as e:
            log.debug("Schema migration: tenants.wizard_agentic_loop_enabled check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §port-type-enforcement — flag
    # NodeDefinitions that the wizard may auto-insert as single-hop
    # adapters between incompatible ports. Default false; system-shared
    # adapters seeded by data migration below.
    if 'node_definitions' in tables:
        try:
            nd_cols = {c['name'] for c in insp.get_columns('node_definitions')}
            if 'is_adapter' not in nd_cols:
                _run_migration("node_definitions.is_adapter column", [
                    "ALTER TABLE node_definitions ADD COLUMN is_adapter "
                    "BOOLEAN NOT NULL DEFAULT FALSE",
                    "CREATE INDEX IF NOT EXISTS ix_node_definitions_is_adapter "
                    "ON node_definitions(is_adapter) WHERE is_adapter = TRUE",
                ])
        except Exception as e:
            log.debug("Schema migration: node_definitions.is_adapter check (non-fatal): %s", e)

    # workflow-agentic-execution-loop §port-type-enforcement — mark the
    # initial set of system-shared NodeDefinitions as adapters. These
    # are existing converter nodes whose (input_type, output_type) pair
    # bridges two distinct port types. Idempotent — only flips rows
    # currently is_adapter=FALSE; once flipped, the data migration is a
    # no-op. Operators can manually un-flag via UPDATE if needed.
    if 'node_definitions' in tables:
        try:
            nd_cols = {c['name'] for c in insp.get_columns('node_definitions')}
            if 'is_adapter' in nd_cols:
                _SYSTEM_ADAPTER_KINDS = (
                    'aec_excel_parser',
                    'aec_report_generator',
                    'text_to_image',
                    'cad_converter',
                    'usd_converter',
                    'hdr_converter',
                    'image_converter',
                    'mesh_converter',
                    'audio_converter',
                    'video_converter',
                )
                kinds_sql = "', '".join(_SYSTEM_ADAPTER_KINDS)
                _run_migration("node_definitions: seed system adapters", [
                    f"UPDATE node_definitions SET is_adapter = TRUE "
                    f"WHERE tenant_id IS NULL AND is_adapter = FALSE "
                    f"AND node_kind IN ('{kinds_sql}')",
                ])
        except Exception as e:
            log.debug("Schema migration: node_definitions adapter seed (non-fatal): %s", e)

    # workflow-agentic-execution-loop §quality-signal — extend the
    # wizard_feedback_event.action CHECK constraint to allow the new
    # post-hoc signal types (`downstream_edited`, `deleted`,
    # `debug_fix_applied` — WSIP 4.4). The original constraint accepts
    # {accepted, dismissed_without_action, rejected_*}. Idempotent:
    # DROP-IF-EXISTS + ADD rebuilds the constraint to the current
    # vocabulary every pass.
    if 'wizard_feedback_event' in tables:
        try:
            with engine.connect() as conn:
                con_def_row = conn.execute(sa_text(
                    "SELECT pg_get_constraintdef(c.oid) "
                    "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname='wizard_feedback_event' "
                    "AND c.conname='wizard_feedback_event_action_check'"
                )).fetchone()
                con_def = con_def_row[0] if con_def_row else None
            needs_extend = (
                con_def is None
                or "downstream_edited" not in con_def
                or "debug_fix_applied" not in con_def
            )
            if needs_extend:
                _run_migration("wizard_feedback_event.action: allow downstream_edited + deleted + debug_fix_applied", [
                    "ALTER TABLE wizard_feedback_event "
                    "DROP CONSTRAINT IF EXISTS wizard_feedback_event_action_check",
                    "ALTER TABLE wizard_feedback_event ADD CONSTRAINT "
                    "wizard_feedback_event_action_check CHECK ("
                    "  action = 'accepted'"
                    "  OR action = 'dismissed_without_action'"
                    "  OR action = 'downstream_edited'"
                    "  OR action = 'deleted'"
                    "  OR action = 'debug_fix_applied'"
                    "  OR action LIKE 'rejected_%'"
                    ")",
                ])
        except Exception as e:
            log.debug("Schema migration: wizard_feedback_event.action extend (non-fatal): %s", e)

    # workflow-agentic-execution-loop §quality-signal — add column
    # linking each apply feedback row to the workflow_version it
    # materialised. The downstream-edit detector reads this to diff
    # the applied graph vs the workflow's current latest version.
    # Nullable — older rows + rejected/dismissed apply attempts have
    # no version to link to.
    if 'wizard_feedback_event' in tables:
        try:
            wfe_cols = {c['name'] for c in insp.get_columns('wizard_feedback_event')}
            if 'applied_workflow_version_id' not in wfe_cols:
                _run_migration("wizard_feedback_event.applied_workflow_version_id column", [
                    "ALTER TABLE wizard_feedback_event ADD COLUMN "
                    "applied_workflow_version_id INTEGER",
                    "CREATE INDEX IF NOT EXISTS ix_wfe_applied_workflow_version_id "
                    "ON wizard_feedback_event(applied_workflow_version_id)",
                ])
        except Exception as e:
            log.debug("Schema migration: wizard_feedback_event.applied_workflow_version_id (non-fatal): %s", e)

    # workflow-agentic-execution-loop §wizard-agentic-loop — add
    # `wizard_test_run_id` column to `wizard_feedback_event` so each
    # apply/reject/edit feedback row can reference the terminal iteration
    # of the agentic loop that produced the workflow. Nullable since
    # pre-agentic-loop wizard apply events have no lineage to attach to.
    # Idempotent — only fires when the table exists AND the column doesn't.
    if 'wizard_feedback_event' in tables:
        try:
            wfe_cols = {c['name'] for c in insp.get_columns('wizard_feedback_event')}
            if 'wizard_test_run_id' not in wfe_cols:
                _run_migration("wizard_feedback_event.wizard_test_run_id column", [
                    "ALTER TABLE wizard_feedback_event ADD COLUMN wizard_test_run_id BIGINT",
                    "CREATE INDEX IF NOT EXISTS ix_wfe_test_run_id ON wizard_feedback_event(wizard_test_run_id)",
                ])
        except Exception as e:
            log.debug("Schema migration: wizard_feedback_event.wizard_test_run_id check (non-fatal): %s", e)

    # wizard-learning-from-feedback §11.1 — add `feature_set_version`
    # column to `wizard_feedback_event` so the cross-tenant baseline
    # trainer can reject rows written under a different (or unknown)
    # feature schema instead of silently mixing them into training
    # data. Nullable — existing/legacy rows predate this column and
    # are treated as "unknown feature set" (excluded by the trainer,
    # not backfilled). The table is RANGE-partitioned by created_at;
    # ALTER TABLE on the parent propagates to all partitions.
    # Idempotent — only fires when the table exists AND the column doesn't.
    if 'wizard_feedback_event' in tables:
        try:
            wfe_cols = {c['name'] for c in insp.get_columns('wizard_feedback_event')}
            if 'feature_set_version' not in wfe_cols:
                _run_migration("wizard_feedback_event.feature_set_version column", [
                    "ALTER TABLE wizard_feedback_event ADD COLUMN "
                    "feature_set_version VARCHAR(32)",
                ])
        except Exception as e:
            log.debug("Schema migration: wizard_feedback_event.feature_set_version check (non-fatal): %s", e)

    # megaplan 14.6c — the learned-p95 VRAM lookup reads the last 50
    # COMPLETED tasks of a node_kind on every GPU enqueue; without this
    # partial index a rare kind scans the whole queue backwards by id.
    if 'api_task_queue' in tables:
        try:
            _run_migration("api_task_queue node_kind completed index", [
                "CREATE INDEX IF NOT EXISTS ix_api_task_queue_nodekind_completed "
                "ON api_task_queue ((task_payload->>'node_kind'), id DESC) "
                "WHERE status = 'COMPLETED'",
            ])
        except Exception as e:
            log.debug("Schema migration: api_task_queue node_kind index (non-fatal): %s", e)

    # scheduler-runtime-config-panel — singleton table for the
    # platform-admin scheduler aggressiveness knobs (capacity_floor_pct,
    # packer_full_above_util_pct, variance_penalty_s + preset_name).
    # services.scheduler_runtime_cache loads from this table at startup;
    # an empty table falls back to env vars + in-code defaults so the
    # very first deploy behaves exactly like today.
    _run_migration("create scheduler_runtime_config table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS scheduler_runtime_config ("
        " singleton_id INTEGER PRIMARY KEY DEFAULT 1,"
        " preset_name VARCHAR(20) NOT NULL DEFAULT 'normal',"
        " capacity_floor_pct SMALLINT NOT NULL DEFAULT 30,"
        " packer_full_above_util_pct SMALLINT NOT NULL DEFAULT 70,"
        " variance_penalty_s SMALLINT NOT NULL DEFAULT 15,"
        " background_max_fleet_pct SMALLINT NOT NULL DEFAULT 40,"
        " updated_by_user_id INTEGER REFERENCES users(id),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " audit_event_id BIGINT REFERENCES audit_events(id),"
        " CONSTRAINT ck_scheduler_runtime_config_singleton CHECK (singleton_id = 1),"
        " CONSTRAINT ck_scheduler_runtime_config_capacity_floor_range CHECK (capacity_floor_pct BETWEEN 0 AND 90),"
        " CONSTRAINT ck_scheduler_runtime_config_packer_full_above_range CHECK (packer_full_above_util_pct BETWEEN 10 AND 100),"
        " CONSTRAINT ck_scheduler_runtime_config_variance_penalty_range CHECK (variance_penalty_s BETWEEN 1 AND 60),"
        " CONSTRAINT ck_scheduler_runtime_config_background_max_fleet_range CHECK (background_max_fleet_pct BETWEEN 5 AND 90),"
        " CONSTRAINT ck_scheduler_runtime_config_preset_name CHECK (preset_name IN ('aggressive', 'normal', 'light', 'custom'))"
        ")"
    ])

    # add-background-cap-slider: the shared ongoing cap for background work
    # (calibration + variant-build backfill). Existing deployments get the
    # column + constraint added idempotently; fresh deploys get it from the
    # CREATE above.
    _src_cols = _get_cols('scheduler_runtime_config')
    if 'background_max_fleet_pct' not in _src_cols:
        _run_migration("scheduler_runtime_config.background_max_fleet_pct column", [
            "ALTER TABLE scheduler_runtime_config "
            "ADD COLUMN background_max_fleet_pct SMALLINT NOT NULL DEFAULT 40",
            "ALTER TABLE scheduler_runtime_config "
            "ADD CONSTRAINT ck_scheduler_runtime_config_background_max_fleet_range "
            "CHECK (background_max_fleet_pct BETWEEN 5 AND 90)",
        ])

    # launch-day-followup-2 #16: trigger consolidation. Non-cron
    # triggers (webhook, file_event, workflow_completion,
    # log_pattern_alert, queue_empty) are deactivated en-masse — the
    # API rejects new ones, and the evaluation loop already skips
    # is_active=False rows. The trigger_type column itself stays so
    # the audit trail of what each row used to be is preserved.
    # Idempotent — UPDATE matches zero rows once the migration has run.
    _run_migration("deactivate non-cron workflow triggers (cron-only consolidation)", [
        # Cast to text — the Postgres enum 'workflowtriggertype' stores
        # the Python member name (SCHEDULE), not the .value, and a bare
        # `!= 'schedule'` makes psycopg2 try to coerce 'schedule' into
        # the enum and fail with InvalidTextRepresentation.
        "UPDATE workflow_triggers "
        "SET is_active = false "
        "WHERE trigger_type::text != 'SCHEDULE' "
        "AND is_active = true "
        "AND deleted_at IS NULL",
    ])

    # Heal stuck platform-node scan_status. There are two shapes:
    #
    # 1. The system row itself (tenant_id IS NULL, or name LIKE 'system.%')
    #    — vetted at build time, should always be approved.
    #
    # 2. Tenant clones of system rows. When a tenant is onboarded the
    #    seed cloning step copies every system row into tenant_id=N,
    #    same name. These clones are byte-identical to the system parent
    #    but the dispatcher was flipping them to 'pending_scan' and
    #    leaving them stuck — which is what produced the launch-day
    #    "Node 'Image Input' cannot be used" error.
    #
    # Both shapes auto-heal to 'approved' on every API startup. Tenant
    # clones inherit by name match against an approved system parent.
    _run_migration("heal stuck platform-shipped node scan_status (own rows)", [
        "UPDATE node_definitions "
        "SET scan_status = 'approved' "
        "WHERE (tenant_id IS NULL OR name LIKE 'system.' || '%') "
        "AND scan_status IN ('pending_scan', 'scanning', 'failed')",
    ])
    _run_migration("heal stuck platform-shipped node scan_status (tenant clones)", [
        "UPDATE node_definitions tn "
        "SET scan_status = 'approved' "
        "FROM node_definitions sn "
        "WHERE sn.tenant_id IS NULL "
        "AND sn.scan_status = 'approved' "
        "AND tn.tenant_id IS NOT NULL "
        "AND tn.name = sn.name "
        "AND tn.scan_status IN ('pending_scan', 'scanning', 'failed')",
    ])

    # plans columns
    plan_cols = _get_cols('plans')
    for col_name, col_def in {
        'overage_credit_price_cents': "INTEGER",
        'max_cpu_hours_monthly': "INTEGER DEFAULT 0 NOT NULL",
        'free_trial_days': "INTEGER DEFAULT 0 NOT NULL",
        'api_rate_limit_per_minute': "INTEGER DEFAULT 200 NOT NULL",
        'allow_custom_nodes': "BOOLEAN DEFAULT FALSE NOT NULL",
        'show_timing': "BOOLEAN DEFAULT FALSE NOT NULL",
        'overage_cap_cents': "INTEGER",
    }.items():
        if col_name not in plan_cols:
            _run_migration(f"added {col_name} to plans",
                           [f"ALTER TABLE plans ADD COLUMN {col_name} {col_def}"])

    # Per-plan default overage spend cap (cents/period) — runaway-bill
    # backstop for paid metered tiers (~1.5× plan price). Seeded only where
    # the cap is still NULL so an admin's per-plan change isn't clobbered.
    # Unlimited tiers (Enterprise/Platform) and Free stay uncapped (NULL).
    if 'overage_cap_cents' in _get_cols('plans'):
        _run_migration("seed per-plan overage spend caps", [
            "UPDATE plans SET overage_cap_cents = 3000  WHERE name LIKE '%Starter%'  AND overage_cap_cents IS NULL",
            "UPDATE plans SET overage_cap_cents = 15000 WHERE name LIKE '%Pro%'      AND overage_cap_cents IS NULL",
            "UPDATE plans SET overage_cap_cents = 30000 WHERE name LIKE '%Business%' AND overage_cap_cents IS NULL",
        ])

    # activation-lifecycle-emails: per-user lifecycle opt-out + the
    # idempotency log for sent lifecycle emails.
    if 'lifecycle_emails_opted_out' not in _get_cols('users'):
        _run_migration("added lifecycle_emails_opted_out to users", [
            "ALTER TABLE users ADD COLUMN lifecycle_emails_opted_out BOOLEAN NOT NULL DEFAULT FALSE",
        ])
    if 'lifecycle_email_log' not in tables:
        _run_migration("created lifecycle_email_log table", [
            """CREATE TABLE lifecycle_email_log (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                email_key VARCHAR(40) NOT NULL,
                sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE UNIQUE INDEX uq_lifecycle_email_tenant_key ON lifecycle_email_log (tenant_id, email_key)",
            "CREATE INDEX ix_lifecycle_email_log_tenant_id ON lifecycle_email_log (tenant_id)",
        ])

    # embeddable-run-surface: tokenized public run links + run mapping.
    if 'run_surface_links' not in tables:
        _run_migration("created run_surface_links table", [
            """CREATE TABLE run_surface_links (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                workflow_version_id INTEGER NOT NULL REFERENCES workflow_versions(id) ON DELETE CASCADE,
                created_by_user_id INTEGER REFERENCES users(id),
                token VARCHAR(64) NOT NULL,
                label VARCHAR(255),
                expires_at TIMESTAMPTZ,
                max_total_runs INTEGER,
                runs_used INTEGER NOT NULL DEFAULT 0,
                max_total_credits INTEGER,
                credits_used INTEGER NOT NULL DEFAULT 0,
                rate_limit_per_hour INTEGER,
                exposed_inputs JSONB,
                password_hash VARCHAR(255),
                frame_ancestors TEXT NOT NULL DEFAULT '*',
                revoked_at TIMESTAMPTZ,
                visit_count INTEGER NOT NULL DEFAULT 0,
                last_run_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE UNIQUE INDEX uq_run_surface_links_token ON run_surface_links (token)",
            "CREATE INDEX ix_run_surface_links_tenant_id ON run_surface_links (tenant_id)",
            "CREATE INDEX ix_run_surface_links_version ON run_surface_links (workflow_version_id)",
        ])
    if 'run_surface_runs' not in tables:
        _run_migration("created run_surface_runs table", [
            """CREATE TABLE run_surface_runs (
                id SERIAL PRIMARY KEY,
                link_id INTEGER NOT NULL REFERENCES run_surface_links(id) ON DELETE CASCADE,
                workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                client_ip VARCHAR(64),
                estimated_credits INTEGER NOT NULL DEFAULT 0,
                released BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE INDEX ix_run_surface_runs_link ON run_surface_runs (link_id)",
            "CREATE INDEX ix_run_surface_runs_link_created ON run_surface_runs (link_id, created_at)",
            "CREATE INDEX ix_run_surface_runs_wfrun ON run_surface_runs (workflow_run_id)",
        ])
    if 'run_surface_runs' in tables and 'released' not in _get_cols('run_surface_runs'):
        _run_migration("added released to run_surface_runs", [
            "ALTER TABLE run_surface_runs ADD COLUMN released BOOLEAN NOT NULL DEFAULT FALSE",
        ])

    # subscription columns
    sub_cols = _get_cols('subscriptions')
    for col_name, col_def in {
        'allow_overage': "BOOLEAN DEFAULT false NOT NULL",
        'pending_plan_id': "INTEGER REFERENCES plans(id)",
        'pending_plan_change_at': "TIMESTAMPTZ",
    }.items():
        if col_name not in sub_cols:
            _run_migration(f"added {col_name} to subscriptions",
                           [f"ALTER TABLE subscriptions ADD COLUMN {col_name} {col_def}"])

    # resource_patterns.gpu_model
    rp_cols = _get_cols('resource_patterns')
    if 'gpu_model' not in rp_cols:
        _run_migration("added gpu_model to resource_patterns",
                       ["ALTER TABLE resource_patterns ADD COLUMN gpu_model VARCHAR(100)",
                        "CREATE INDEX IF NOT EXISTS ix_resource_patterns_gpu ON resource_patterns (gpu_model)"])

    # changelog_entries — What's New publishing extension (#5)
    cl_cols = _get_cols('changelog_entries')
    for col_name, col_def in {
        'summary': "VARCHAR(500)",
        'hero_image_url': "TEXT",
        'tags': "JSONB",
        'slug': "VARCHAR(160)",
        'scheduled_for': "TIMESTAMPTZ",
    }.items():
        if col_name not in cl_cols:
            _run_migration(f"added {col_name} to changelog_entries",
                           [f"ALTER TABLE changelog_entries ADD COLUMN {col_name} {col_def}"])
    if 'slug' not in cl_cols:
        _run_migration("changelog_entries slug unique index",
                       ["CREATE UNIQUE INDEX IF NOT EXISTS ix_changelog_entries_slug "
                        "ON changelog_entries (slug)"])
    if 'scheduled_for' not in cl_cols:
        _run_migration("changelog_entries scheduled_for index",
                       ["CREATE INDEX IF NOT EXISTS ix_changelog_entries_scheduled_for "
                        "ON changelog_entries (scheduled_for)"])

    # workflow_triggers columns
    wt_cols = _get_cols('workflow_triggers')
    for col_name, col_def in {
        'created_at': "TIMESTAMPTZ DEFAULT now()",
        'last_triggered_at': "TIMESTAMPTZ",
        'trigger_count': "INTEGER DEFAULT 0",
    }.items():
        if col_name not in wt_cols:
            _run_migration(f"added {col_name} to workflow_triggers",
                           [f"ALTER TABLE workflow_triggers ADD COLUMN {col_name} {col_def}"])

    # cogs.data_classification
    cog_cols = _get_cols('cogs')
    if 'data_classification' not in cog_cols:
        _run_migration("added data_classification to cogs",
                       ["ALTER TABLE cogs ADD COLUMN data_classification VARCHAR(32) DEFAULT 'internal' NOT NULL"])

    enrollment_cols = _get_cols('worker_enrollments')
    if enrollment_cols and 'enrollment_token_id' not in enrollment_cols:
        _run_migration("added enrollment_token_id to worker_enrollments",
                       ["ALTER TABLE worker_enrollments ADD COLUMN enrollment_token_id INTEGER REFERENCES worker_enrollment_tokens(id)"])

    # P8f: is_archived soft-hide on notifications. The Archive action
    # in the redesigned bell flips this true; list queries default to
    # include_archived=false so archived rows drop out of the popover.
    notif_cols = _get_cols('notifications')
    if notif_cols and 'is_archived' not in notif_cols:
        _run_migration("added is_archived to notifications", [
            "ALTER TABLE notifications ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT FALSE",
            "CREATE INDEX IF NOT EXISTS ix_notifications_is_archived ON notifications (is_archived)",
        ])

    license_cols = _get_cols('worker_licenses')
    if license_cols and 'paused' not in license_cols:
        _run_migration("added paused flag to worker_licenses", [
            "ALTER TABLE worker_licenses ADD COLUMN paused BOOLEAN NOT NULL DEFAULT FALSE",
            "CREATE INDEX IF NOT EXISTS ix_worker_licenses_paused ON worker_licenses (paused)",
        ])
    if license_cols and 'update_requested' not in license_cols:
        _run_migration("added update_requested flag to worker_licenses", [
            "ALTER TABLE worker_licenses ADD COLUMN update_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "CREATE INDEX IF NOT EXISTS ix_worker_licenses_update_requested ON worker_licenses (update_requested)",
        ])

    # Gate 7 — what this machine costs to run, per operator.
    #
    # Per WORKER rather than per hardware profile or a platform-wide table:
    # the same card in two buildings has different electricity behind it, and
    # an on-prem customer's costs are theirs, not ours. An operator adds a
    # worker and states what it costs them; every run on it prices from that.
    #
    # The breakdown is stored alongside the rate so the number is explainable
    # a year later — "£0.42/hr" with no workings is a figure nobody can audit
    # or update when electricity moves. cost_per_hour_cents stays the single
    # source of truth for pricing, whether typed directly or computed.
    if license_cols and 'cost_per_hour_cents' not in license_cols:
        _run_migration("added cost basis to worker_licenses", [
            "ALTER TABLE worker_licenses ADD COLUMN cost_per_hour_cents INTEGER",
            "ALTER TABLE worker_licenses ADD COLUMN hardware_cost_cents BIGINT",
            "ALTER TABLE worker_licenses ADD COLUMN amortisation_months INTEGER",
            "ALTER TABLE worker_licenses ADD COLUMN power_watts INTEGER",
            "ALTER TABLE worker_licenses ADD COLUMN electricity_cents_per_kwh INTEGER",
        ])

    # Its OWN guard, not appended to the block above. That one keys on
    # cost_per_hour_cents, which by then already exists, so anything added
    # inside it silently never runs on a database that has had the earlier
    # migration — the column is missing and every query on the table dies.
    if license_cols and 'margin_percent' not in license_cols:
        _run_migration("added margin to worker_licenses", [
            # Margin sits beside cost because that is where the operator is
            # already thinking about the machine. NULL falls back to the
            # platform default rather than meaning "sell at cost".
            "ALTER TABLE worker_licenses ADD COLUMN margin_percent INTEGER",
        ])

    if billing_cols_pre := _get_cols('billing_settings'):
        if 'platform_overhead_percent' not in billing_cols_pre:
            _run_migration("added platform overhead recovery to billing_settings", [
                "ALTER TABLE billing_settings ADD COLUMN platform_overhead_percent INTEGER",
            ])

    _CAPTURE_TABLE_INDEXES = {
        'capture_threshold_sets': (
            # Partial unique: one ACTIVE set per pair, superseded rows kept.
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_capture_thresholds_active_pair "
            "ON capture_threshold_sets (detector, working_resolution_px) WHERE is_active",
        ),
        'capture_devices': (
            "CREATE INDEX IF NOT EXISTS ix_capture_devices_tenant ON capture_devices (tenant_id)",
        ),
        'capture_verdicts': (
            "CREATE INDEX IF NOT EXISTS ix_capture_verdicts_tenant ON capture_verdicts (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_capture_verdicts_verdict_id ON capture_verdicts (verdict_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_capture_verdicts_tenant_verdict "
            "ON capture_verdicts (tenant_id, verdict_id)",
        ),
        'capture_preflight_reports': (
            "CREATE INDEX IF NOT EXISTS ix_capture_preflight_tenant ON capture_preflight_reports (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_capture_preflight_node_run ON capture_preflight_reports (node_run_id)",
            # One report per run — upserted on (re-)completion.
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_capture_preflight_run "
            "ON capture_preflight_reports (workflow_run_id)",
        ),
    }

    for _tbl, _ddl in (
        ('capture_threshold_sets', """CREATE TABLE IF NOT EXISTS capture_threshold_sets (
            id SERIAL PRIMARY KEY,
            config_id VARCHAR(64) NOT NULL UNIQUE,
            detector VARCHAR(32) NOT NULL,
            working_resolution_px INTEGER NOT NULL,
            rules JSONB NOT NULL,
            derived_from VARCHAR(64),
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            is_active BOOLEAN NOT NULL DEFAULT TRUE
        )"""),
        ('capture_devices', """CREATE TABLE IF NOT EXISTS capture_devices (
            id SERIAL PRIMARY KEY,
            device_id VARCHAR(64) NOT NULL UNIQUE,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            enrolled_by_user_id INTEGER REFERENCES users(id),
            device_name VARCHAR(255),
            token_hash VARCHAR(128) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ
        )"""),
        ('capture_verdicts', """CREATE TABLE IF NOT EXISTS capture_verdicts (
            id SERIAL PRIMARY KEY,
            -- NOT globally unique. verdict_id is chosen by the DEVICE, so a
            -- global constraint lets one tenant's id collide with another's
            -- and the client reads the duplicate ack as success. Uniqueness is
            -- (tenant_id, verdict_id), created below.
            verdict_id VARCHAR(64) NOT NULL,
            device_id VARCHAR(64) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            capture_session_id VARCHAR(64) NOT NULL,
            sequence INTEGER NOT NULL,
            produced_at TIMESTAMPTZ NOT NULL,
            thresholds_config_id VARCHAR(64) NOT NULL,
            thresholds_stale BOOLEAN NOT NULL DEFAULT FALSE,
            overall VARCHAR(20) NOT NULL,
            summary TEXT,
            image_count INTEGER NOT NULL,
            rules JSONB NOT NULL,
            untexturable_images JSONB,
            unmatched_images JSONB,
            weak_regions JSONB,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""),
        ('capture_preflight_reports', """CREATE TABLE IF NOT EXISTS capture_preflight_reports (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id),
            workflow_run_id INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            node_run_id INTEGER,
            verdict VARCHAR(20) NOT NULL,
            advisory BOOLEAN NOT NULL DEFAULT TRUE,
            summary JSONB,
            findings JSONB,
            weak_regions JSONB,
            thresholds JSONB,
            database_object_key VARCHAR(512),
            sparse_ply_object_key VARCHAR(512),
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""),
    ):
        if _tbl not in tables:
            _run_migration(f"created {_tbl}", [
                _ddl,
                # Hand ownership to the STATIC role immediately. The creating
                # role is a rotating Vault credential; a table left owned by it
                # becomes un-migratable the moment it rotates. Doing this in
                # the same transaction as the CREATE means the window is zero.
                f'ALTER TABLE {_tbl} OWNER TO "{_PG_OWNER_ROLE}"',
                *_CAPTURE_TABLE_INDEXES.get(_tbl, ()),
            ])

    # Capture tables — constraint corrections found in review.
    #
    # verdict_id was globally UNIQUE. It is chosen by the DEVICE, so a global
    # constraint lets one tenant's id collide with another's, and the client
    # reads the resulting duplicate ack as success and drops its outbox entry.
    # Scoped to (tenant_id, verdict_id).
    #
    # The "one active threshold set per pair" invariant was documented and not
    # enforced, so two active rows would have been accepted and the newest
    # silently chosen.
    if 'capture_verdicts' in tables:
        _run_migration("scoped capture verdict uniqueness to tenant", [
            "ALTER TABLE capture_verdicts DROP CONSTRAINT IF EXISTS capture_verdicts_verdict_id_key",
            # The unique index SQLAlchemy generated for `unique=True, index=True`
            # is named ix_..., not the constraint name — dropping the constraint
            # alone left it in place and cross-tenant collisions still 500'd.
            "DROP INDEX IF EXISTS ix_capture_verdicts_verdict_id",
            "CREATE INDEX IF NOT EXISTS ix_capture_verdicts_verdict_id ON capture_verdicts (verdict_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_capture_verdicts_tenant_verdict "
            "ON capture_verdicts (tenant_id, verdict_id)",
        ])
    if 'capture_threshold_sets' in tables:
        _run_migration("enforced one active threshold set per pair", [
            "DROP INDEX IF EXISTS ix_capture_thresholds_pair",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_capture_thresholds_active_pair "
            "ON capture_threshold_sets (detector, working_resolution_px) WHERE is_active",
        ])

    if 'infrastructure_costs' not in tables:
        _run_migration("created infrastructure_costs", [
            """CREATE TABLE IF NOT EXISTS infrastructure_costs (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                kind VARCHAR(20) NOT NULL DEFAULT 'capital',
                hardware_cost_cents BIGINT,
                amortisation_months INTEGER,
                power_watts INTEGER,
                electricity_cents_per_kwh INTEGER,
                monthly_cents BIGINT,
                cost_per_hour_cents INTEGER,
                notes TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT now()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_infrastructure_costs_active ON infrastructure_costs (is_active)",
        ])

    run_cols = _get_cols('workflow_runs')
    if run_cols and 'cost_cents' not in run_cols:
        _run_migration("added run costing to workflow_runs", [
            "ALTER TABLE workflow_runs ADD COLUMN cost_cents INTEGER",
            "ALTER TABLE workflow_runs ADD COLUMN billed_cents INTEGER",
        ])

    billing_cols = _get_cols('billing_settings')
    if billing_cols and 'manager_cost_per_hour_cents' not in billing_cols:
        _run_migration("added manager cost basis to billing_settings", [
            "ALTER TABLE billing_settings ADD COLUMN manager_hardware_cost_cents BIGINT",
            "ALTER TABLE billing_settings ADD COLUMN manager_amortisation_months INTEGER",
            "ALTER TABLE billing_settings ADD COLUMN manager_power_watts INTEGER",
            "ALTER TABLE billing_settings ADD COLUMN manager_electricity_cents_per_kwh INTEGER",
            "ALTER TABLE billing_settings ADD COLUMN manager_cost_per_hour_cents INTEGER",
        ])

    # GH.9 — burst platform fee table (NULL = services/burst_pricing.py defaults).
    if billing_cols and 'burst_fee_credits_per_gpu_hour' not in billing_cols:
        _run_migration("added burst fee table to billing_settings", [
            "ALTER TABLE billing_settings ADD COLUMN burst_fee_credits_per_gpu_hour JSONB",
        ])

    accel_cols = _get_cols('accelerator_profiles')
    if accel_cols and 'is_integrated' not in accel_cols:
        _run_migration("added integrated GPU fields to accelerator_profiles", [
            "ALTER TABLE accelerator_profiles ADD COLUMN is_integrated BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE accelerator_profiles ADD COLUMN shared_ram_usage_gb FLOAT",
            "ALTER TABLE accelerator_profiles ADD COLUMN cpu_bw_contention FLOAT",
        ])

    # Operator quarantine flag — one-click "disable this accelerator" that
    # excludes a device from scheduling without destroying its benchmark scores.
    if accel_cols and 'admin_disabled' not in accel_cols:
        _run_migration("added admin_disabled to accelerator_profiles", [
            "ALTER TABLE accelerator_profiles ADD COLUMN admin_disabled BOOLEAN NOT NULL DEFAULT FALSE",
        ])

    hp_cols = _get_cols('hardware_profiles')
    if hp_cols and 'benchmark_hmac_key' not in hp_cols:
        _run_migration("added benchmark_hmac_key to hardware_profiles", [
            "ALTER TABLE hardware_profiles ADD COLUMN benchmark_hmac_key VARCHAR(128)",
        ])

    # system_only flag on node_definitions — hides system nodes (benchmarks,
    # LLM inference, trivy, calibration) from the workflow builder palette
    # while keeping them fully integrated with the metadata dispatch pipeline.
    nd_cols = _get_cols('node_definitions')
    if nd_cols and 'system_only' not in nd_cols:
        _run_migration("added system_only + container_timeout to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN system_only BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE node_definitions ADD COLUMN container_timeout INTEGER NOT NULL DEFAULT 1800",
        ])

    # Drop the legacy global unique constraint on node_definitions.name and
    # ensure the per-tenant / system-node uniques exist in their soft-delete-
    # aware form (wizard-rebuild §11b — without `deleted_at IS NULL` in the
    # predicate, soft-deleted tombstones squat names and block recreation).
    # Idempotent: each step is `IF EXISTS` / `IF NOT EXISTS`. The
    # 20260525_soft_delete_columns.py migration handles the from-existing-
    # constraints transition; this block is the from-fresh-schema path that
    # runs at every startup against the auto-create_all bootstrap.
    if 'node_definitions' in tables:
        _run_migration("node_definitions: ensure soft-delete-aware name uniques", [
            "ALTER TABLE node_definitions DROP CONSTRAINT IF EXISTS node_definitions_name_key",
            "ALTER TABLE node_definitions DROP CONSTRAINT IF EXISTS uq_node_def_tenant_name",
            "ALTER TABLE node_definitions DROP CONSTRAINT IF EXISTS uq_node_def_tenant_user_name",
            "DROP INDEX IF EXISTS uq_system_node_def_name",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_node_def_live_tenant_user_name "
            "ON node_definitions (tenant_id, user_id, name) "
            "WHERE deleted_at IS NULL",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_system_live_node_def_name "
            "ON node_definitions (name) "
            "WHERE tenant_id IS NULL AND deleted_at IS NULL",
        ])

    # Per-device accelerator profiles (one row per GPU/NPU/etc per worker)
    if 'accelerator_profiles' not in tables:
        _run_migration("created accelerator_profiles table", ["""
            CREATE TABLE accelerator_profiles (
                id SERIAL PRIMARY KEY,
                hardware_profile_id INTEGER NOT NULL REFERENCES hardware_profiles(id),
                kind VARCHAR(16) NOT NULL,
                index INTEGER NOT NULL,
                name VARCHAR(255),
                vendor_id VARCHAR(8),
                device_id VARCHAR(8),
                uuid VARCHAR(64),
                pci_bus_id VARCHAR(32),
                driver_version VARCHAR(64),
                vram_gb FLOAT,
                compute_score FLOAT,
                memory_bw_score FLOAT,
                copy_bw_score FLOAT,
                benchmark_results JSONB,
                benchmark_status VARCHAR(16) NOT NULL DEFAULT 'pending',
                benchmarked_at TIMESTAMP WITH TIME ZONE,
                error_message TEXT,
                last_strategy_id INTEGER,
                benchmark_hmac_key VARCHAR(128),
                admin_disabled BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                UNIQUE (hardware_profile_id, kind, index)
            )
        """,
            "CREATE INDEX IF NOT EXISTS ix_accel_kind_vendor_device ON accelerator_profiles (kind, vendor_id, device_id)",
            "CREATE INDEX IF NOT EXISTS ix_accel_hw_profile ON accelerator_profiles (hardware_profile_id)",
        ])

    # Strategy catalog — one per (kind, vendor, device_family). Seeded rows
    # are curated; new rows arrive via LLM generation for unknown kinds.
    if 'accelerator_benchmark_strategies' not in tables:
        _run_migration("created accelerator_benchmark_strategies table", ["""
            CREATE TABLE accelerator_benchmark_strategies (
                id SERIAL PRIMARY KEY,
                kind VARCHAR(16) NOT NULL,
                vendor_id VARCHAR(8),
                device_id_pattern VARCHAR(32),
                device_family VARCHAR(64),
                image_ref VARCHAR(255) NOT NULL,
                script_url VARCHAR(255),
                script_content TEXT,
                env_template JSONB,
                expected_schema JSONB,
                source VARCHAR(16) NOT NULL DEFAULT 'seeded',
                auto_approved BOOLEAN NOT NULL DEFAULT FALSE,
                approved_by_user_id INTEGER REFERENCES users(id),
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                runs_succeeded INTEGER NOT NULL DEFAULT 0,
                runs_failed INTEGER NOT NULL DEFAULT 0,
                last_success_at TIMESTAMP WITH TIME ZONE,
                last_anomaly_at TIMESTAMP WITH TIME ZONE,
                notes TEXT,
                UNIQUE (kind, vendor_id, device_id_pattern)
            )
        """,
            "CREATE INDEX IF NOT EXISTS ix_strategy_kind ON accelerator_benchmark_strategies (kind)",
        ])

    # Seed the four default strategies for known accelerator kinds.
    # These point to curated benchmark images and are auto-approved.
    # Runs unconditionally (not inside the "table not in tables" guard above)
    # so deployments that already had the table from an earlier schema version
    # still get the rows — without this, resolve_strategy() returns None, the
    # dispatcher falls back to accelerator-benchmark-generic:latest, and our
    # per-kind image fixes are silently ignored (chased this for hours).
    # ON CONFLICT DO NOTHING makes it a no-op once seeded.
    # Idempotent via explicit NOT EXISTS — Postgres treats NULL != NULL in
    # UNIQUE constraints, so `ON CONFLICT DO NOTHING` against the
    # (kind, vendor_id, device_id_pattern) unique doesn't fire when vendor_id
    # and device_id_pattern are both NULL (which is every seed row). This
    # caused 20+ duplicate rows per kind on restart.
    # Explicit runs_succeeded/runs_failed=0 because the earliest schema
    # version of this table on some deployments didn't have DEFAULT 0 on
    # those columns; letting the DB pick a default NULL fails NOT NULL.
    # Registry resolved at call time so a deployment pointing DOCKER_REGISTRY
    # at its own mirror seeds refs it can actually pull. These are inside a
    # triple-quoted SQL string, which is why the hardcode guard did not see
    # them: to that check they looked like prose.
    from utils.registries import platform_image as _pi
    _bench = {k: _pi(f"accelerator-benchmark-{k}:latest")
              for k in ("cuda", "rocm", "intel", "npu")}
    _run_migration("seed default benchmark strategies (idempotent)", [
        f"""INSERT INTO accelerator_benchmark_strategies
            (kind, image_ref, source, auto_approved, notes, runs_succeeded, runs_failed)
        SELECT v.kind, v.image_ref, 'seeded', TRUE, v.notes, 0, 0
        FROM (VALUES
            ('cuda',  '{_bench['cuda']}',  'NVIDIA CUDA compute + memory bandwidth'),
            ('rocm',  '{_bench['rocm']}',  'AMD ROCm compute + memory bandwidth'),
            ('intel', '{_bench['intel']}', 'Intel oneAPI compute + memory bandwidth'),
            ('npu',   '{_bench['npu']}',   'NPU inference throughput')
        ) AS v(kind, image_ref, notes)
        WHERE NOT EXISTS (
            SELECT 1 FROM accelerator_benchmark_strategies s
            WHERE s.kind = v.kind
              AND s.source = 'seeded'
              AND s.vendor_id IS NULL
              AND s.device_id_pattern IS NULL
        )""",
    ])

    # Soft-delete column on users. ISO 27001 / GDPR compliance: hard-delete
    # would break every FK referencing the user in audit_events / ledgers /
    # workflow_runs / etc. Keep the row, null-flag `deleted_at`, scrub PII
    # on delete (see crud.delete_user_permanently).
    user_cols = _get_cols('users')
    if user_cols and 'deleted_at' not in user_cols:
        _run_migration("added deleted_at to users (soft delete)", [
            "ALTER TABLE users ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE",
            "CREATE INDEX IF NOT EXISTS ix_users_deleted_at ON users (deleted_at)",
        ])
    # Partial-signup tracking. welcome_email_sent_at stamps when the
    # signup welcome email made it out the door; welcome_email_error
    # captures the SMTP / template error string when it didn't. The
    # signup path commits the user row BEFORE the email send (see
    # routers/onboarding.py), so a silent failure leaves a half-formed
    # account with no way for the user to log in or for ops to spot it.
    # /platform/signup-orphans lists rows where sent_at IS NULL AND
    # created_at older than 1h, with a Resend button per row.
    if user_cols and 'welcome_email_sent_at' not in user_cols:
        _run_migration("added welcome_email_sent_at to users (signup tracking)", [
            "ALTER TABLE users ADD COLUMN welcome_email_sent_at TIMESTAMP WITH TIME ZONE",
            "CREATE INDEX IF NOT EXISTS ix_users_welcome_email_sent_at "
            "ON users (welcome_email_sent_at) WHERE welcome_email_sent_at IS NULL",
        ])
    if user_cols and 'welcome_email_error' not in user_cols:
        _run_migration("added welcome_email_error to users (signup tracking)", [
            "ALTER TABLE users ADD COLUMN welcome_email_error TEXT",
        ])

    # Permission-update email digest queue. Per-event sends for
    # workflow-complete + permission-expiration emails were spamming
    # operators (one email per event). These events now INSERT here
    # and a 15-min APScheduler job drains rows once per user per day
    # in the 09:00 user-local window. Time-sensitive emails (billing,
    # permission requests, invitations, welcome) bypass this queue.
    if 'pending_permission_emails' not in tables:
        _run_migration("create pending_permission_emails table (idempotent)", [
            "CREATE TABLE IF NOT EXISTS pending_permission_emails ("
            "  id BIGSERIAL PRIMARY KEY,"
            "  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
            "  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,"
            "  event_type VARCHAR(64) NOT NULL,"
            "  event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,"
            "  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),"
            "  sent_at TIMESTAMP WITH TIME ZONE NULL"
            ")",
            # Hot index: the digest scheduler scans by (user, sent_at IS NULL)
            "CREATE INDEX IF NOT EXISTS ix_pending_perm_emails_user_unsent "
            "ON pending_permission_emails (user_id) WHERE sent_at IS NULL",
            # Backfill / sweep index — find old unsent rows
            "CREATE INDEX IF NOT EXISTS ix_pending_perm_emails_created "
            "ON pending_permission_emails (created_at) WHERE sent_at IS NULL",
        ])

    # Same soft-delete pattern for tenants — hard-delete cascades would
    # destroy every workflow run, credit ledger entry, and audit event
    # attributed to the tenant. See routers/users.py delete_tenant for
    # the scrub + cascade-soft-delete-users logic.
    tenant_cols = _get_cols('tenants')
    if tenant_cols and 'deleted_at' not in tenant_cols:
        _run_migration("added deleted_at to tenants (soft delete)", [
            "ALTER TABLE tenants ADD COLUMN deleted_at TIMESTAMP WITH TIME ZONE",
            "CREATE INDEX IF NOT EXISTS ix_tenants_deleted_at ON tenants (deleted_at)",
        ])
    if tenant_cols and 'hard_delete_after' not in tenant_cols:
        _run_migration("added hard_delete_after to tenants (retention)", [
            "ALTER TABLE tenants ADD COLUMN hard_delete_after TIMESTAMP WITH TIME ZONE",
            "CREATE INDEX IF NOT EXISTS ix_tenants_hard_delete_after "
            "ON tenants (hard_delete_after)",
        ])
    if tenant_cols and 'retention_days' not in tenant_cols:
        _run_migration("added retention_days to tenants (retention)", [
            "ALTER TABLE tenants ADD COLUMN retention_days INTEGER DEFAULT 90 NOT NULL",
        ])
    if tenant_cols and 'quota_push_blocked' not in tenant_cols:
        _run_migration("added quota_push_blocked to tenants (registry quota gate)", [
            "ALTER TABLE tenants ADD COLUMN quota_push_blocked BOOLEAN DEFAULT FALSE NOT NULL",
        ])
    # launch-pricing-lockdown §5 — add-on SKU bonuses (credit pack +
    # storage block). Default 0; incremented when the tenant subscribes
    # to an add-on Stripe product.
    if tenant_cols and 'bonus_credits' not in tenant_cols:
        _run_migration("added bonus_credits to tenants (add-on SKU)", [
            "ALTER TABLE tenants ADD COLUMN bonus_credits INTEGER NOT NULL DEFAULT 0",
        ])
    if tenant_cols and 'bonus_storage_gb' not in tenant_cols:
        _run_migration("added bonus_storage_gb to tenants (add-on SKU)", [
            "ALTER TABLE tenants ADD COLUMN bonus_storage_gb INTEGER NOT NULL DEFAULT 0",
        ])

    # launch-pricing-lockdown §4 — annual billing variant on plans.
    # 17% discount, encoded as 10× monthly. Nullable on tiers without
    # an annual offering (Free, Enterprise).
    plans_cols = _get_cols('plans')
    if plans_cols and 'stripe_price_id_annual' not in plans_cols:
        _run_migration("added stripe_price_id_annual to plans (annual billing)", [
            "ALTER TABLE plans ADD COLUMN stripe_price_id_annual VARCHAR(255) UNIQUE",
        ])

    # error_message on workflow_runs — orchestrator failures (e.g. a
    # pending_scan node blocking dispatch) were previously only in API
    # stderr, so the dashboard showed runs as "failed" with zero
    # detail. Now the orchestrator's except branch stores the real
    # exception message here and the API surfaces it to the UI.
    wr_cols = _get_cols('workflow_runs')
    if wr_cols and 'error_message' not in wr_cols:
        _run_migration("added error_message to workflow_runs", [
            "ALTER TABLE workflow_runs ADD COLUMN error_message TEXT",
        ])
    # LDF-4 §7 — capacity-queued flag + reason
    if wr_cols and 'is_capacity_queued' not in wr_cols:
        _run_migration("added capacity-queued fields to workflow_runs", [
            "ALTER TABLE workflow_runs ADD COLUMN is_capacity_queued BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE workflow_runs ADD COLUMN capacity_queue_reason VARCHAR(200)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_runs_capacity_queued "
            "ON workflow_runs (is_capacity_queued, tenant_id) WHERE is_capacity_queued = true",
        ])

    secret_cols = _get_cols('secrets')
    if secret_cols and 'shared_with_all_tenants' not in secret_cols:
        _run_migration("added sharing fields to secrets", [
            "ALTER TABLE secrets ADD COLUMN shared_with_all_tenants BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE secrets ADD COLUMN shared_with_tenant_ids TEXT",
        ])

    node_cols = _get_cols('node_definitions')
    if node_cols and 'scan_status' not in node_cols:
        _run_migration("added scan fields to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN scan_status VARCHAR(20) NOT NULL DEFAULT 'approved'",
            "ALTER TABLE node_definitions ADD COLUMN scan_result JSONB",
            "ALTER TABLE node_definitions ADD COLUMN scanned_at TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE node_definitions ADD COLUMN rejection_reason TEXT",
        ])

    # Calibration fields on node_definitions
    if node_cols and 'calibration_status' not in node_cols:
        _run_migration("added calibration fields to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN calibration_status VARCHAR(20) NOT NULL DEFAULT 'uncalibrated'",
            "ALTER TABLE node_definitions ADD COLUMN calibration_progress JSONB",
            "ALTER TABLE node_definitions ADD COLUMN last_calibrated_at TIMESTAMP WITH TIME ZONE",
        ])

    # Manifest sidecar pointer + image digest (Layer-4 / node manifest).
    # manifest_ref is the MinIO object key where the signed manifest JSON
    # lives; image_digest is the sha256 digest of the pushed image — lets
    # us detect tag-rewrite drift between approval and dispatch.
    if node_cols and 'manifest_ref' not in node_cols:
        _run_migration("added manifest_ref + image_digest to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN manifest_ref VARCHAR(500)",
            "ALTER TABLE node_definitions ADD COLUMN image_digest VARCHAR(100)",
        ])

    # Opt-in gVisor runtime per node (Layer-3 / runtime sandbox tier).
    if node_cols and 'use_sandbox_runtime' not in node_cols:
        _run_migration("added use_sandbox_runtime to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN use_sandbox_runtime BOOLEAN NOT NULL DEFAULT FALSE",
        ])

    # Albedo 4th artifact — the rendered METADATA BLOCK .py source.
    if node_cols and 'metadata_file_source' not in node_cols:
        _run_migration("added metadata_file_source to node_definitions", [
            "ALTER TABLE node_definitions ADD COLUMN metadata_file_source TEXT",
        ])

    # size_tier on resource_patterns
    rp_cols = _get_cols('resource_patterns')
    if rp_cols and 'size_tier' not in rp_cols:
        _run_migration("added size_tier to resource_patterns", [
            "ALTER TABLE resource_patterns ADD COLUMN size_tier VARCHAR(20) NOT NULL DEFAULT 'any'",
            "DROP INDEX IF EXISTS ix_resource_pattern_node_kind",
            "CREATE UNIQUE INDEX ix_resource_pattern_node_kind ON resource_patterns (node_kind, queue_name, size_tier)",
        ])

    # hardware_profiles table
    if 'hardware_profiles' not in tables:
        _run_migration("created hardware_profiles table", [
            """CREATE TABLE hardware_profiles (
                id SERIAL PRIMARY KEY,
                fingerprint VARCHAR(64) NOT NULL UNIQUE,
                cpu_model VARCHAR(255),
                cpu_cores INTEGER,
                gpu_model VARCHAR(255),
                gpu_vram_gb FLOAT,
                ram_gb FLOAT,
                cloud_provider VARCHAR(20),
                instance_type VARCHAR(100),
                gpu_compute_score FLOAT,
                cpu_multi_score FLOAT,
                cpu_single_score FLOAT,
                io_seq_score FLOAT,
                io_random_score FLOAT,
                memory_bw_score FLOAT,
                benchmark_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                benchmarked_at TIMESTAMP WITH TIME ZONE,
                benchmark_results JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
            )""",
        ])

    # hardware_profile_id on worker_licenses (no FK constraint — Vault role lacks REFERENCES)
    wl_cols = _get_cols('worker_licenses')
    if wl_cols and 'hardware_profile_id' not in wl_cols:
        _run_migration("added hardware_profile_id to worker_licenses", [
            "ALTER TABLE worker_licenses ADD COLUMN hardware_profile_id INTEGER",
        ])

    # New benchmark score columns on hardware_profiles
    hp_cols = _get_cols('hardware_profiles')
    for col_name, col_def in {
        'minio_download_mbps': "FLOAT",
        'minio_upload_mbps': "FLOAT",
        'docker_pull_seconds': "FLOAT",
        'container_startup_ms': "FLOAT",
        'gpu_memory_bw_score': "FLOAT",
        'disk_seq_read_score': "FLOAT",
    }.items():
        if hp_cols and col_name not in hp_cols:
            _run_migration(f"added {col_name} to hardware_profiles",
                           [f"ALTER TABLE hardware_profiles ADD COLUMN {col_name} {col_def}"])

    # New sample fields on task_resource_samples
    trs_cols = _get_cols('task_resource_samples')
    for col_name, col_def in {
        'single_thread_detected': "BOOLEAN",
        'container_disk_read_mb': "FLOAT",
        'container_disk_write_mb': "FLOAT",
        'vram_alloc_rate_mb_s': "FLOAT",
        'process_count': "INTEGER",
        'gpu_clock_mhz': "FLOAT",
        'swap_used_mb': "FLOAT",
    }.items():
        if trs_cols and col_name not in trs_cols:
            _run_migration(f"added {col_name} to task_resource_samples",
                           [f"ALTER TABLE task_resource_samples ADD COLUMN {col_name} {col_def}"])

    # Set Pro+ feature gates for existing plans
    if plan_cols and 'allow_custom_nodes' in _get_cols('plans'):
        _run_migration("enable custom nodes on all plans (+ Pro timing)", [
            # Custom nodes are available to EVERYONE, always. Re-asserted on
            # every boot so it survives plan re-seeds / start-main-server.
            "UPDATE plans SET allow_custom_nodes = TRUE WHERE allow_custom_nodes = FALSE",
            "UPDATE plans SET show_timing = TRUE WHERE name IN ('Platform', 'Professional', 'Enterprise')",
            "UPDATE plans SET show_timing = FALSE WHERE name IN ('Sandbox', 'Essential', 'Advanced')",
        ])

    # Identity & Access (SSO + SCIM) plan gates. The catalog plan carrying them + the internal
    # Platform plan get both; every other catalog plan stays off. Custom plans
    # are flipped per-plan by a platform admin via PATCH /admin/plans/{id}.
    # Name-stable for the current catalog — the original model-less migrations
    # targeted 'Pro%'/'%Enterprise%', which never matched 'MOD Core Business'
    # or 'Platform', so the flags were false everywhere. We only SET TRUE here
    # (never SET FALSE), so an admin's custom enablement on any other plan is
    # preserved across reboots.
    _pcols = _get_cols('plans')
    if plan_cols and 'sso_enabled' in _pcols and 'scim_enabled' in _pcols:
        _run_migration("set Identity & Access (SSO/SCIM) plan gates", [
            "UPDATE plans SET sso_enabled = TRUE, scim_enabled = TRUE "
            "WHERE name = 'Platform' OR name ILIKE 'MOD Core Business%'",
        ])

    # Welcome bonus: one-time signup credits column on billing_settings.
    # create_all() handles fresh DBs; existing deploys need the idempotent ALTER.
    if 'billing_settings' in tables and 'signup_welcome_bonus_credits' not in _get_cols('billing_settings'):
        _run_migration("add billing_settings.signup_welcome_bonus_credits", [
            "ALTER TABLE billing_settings ADD COLUMN IF NOT EXISTS "
            "signup_welcome_bonus_credits INTEGER NOT NULL DEFAULT 2000",
        ])

    # Per-tenant pricing overrides table (Pricing Config → Tenant Pricing).
    if 'tenant_pricing_overrides' not in tables:
        _run_migration("create tenant_pricing_overrides table", [
            """CREATE TABLE IF NOT EXISTS tenant_pricing_overrides (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
                override_postpaid_price_cents INTEGER,
                override_overage_price_cents INTEGER,
                reason TEXT,
                valid_until TIMESTAMPTZ,
                created_by_user_id INTEGER REFERENCES users(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE INDEX IF NOT EXISTS ix_tenant_pricing_overrides_tenant_id ON tenant_pricing_overrides (tenant_id)",
        ])

    # Seat + Egress add-on blocks — bonus counters + SKU price mappings.
    _tcols = _get_cols('tenants')
    if _tcols and 'bonus_seats' not in _tcols:
        _run_migration("add tenants.bonus_seats / bonus_egress_gb", [
            "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bonus_seats INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS bonus_egress_gb BIGINT NOT NULL DEFAULT 0",
        ])
    if 'platform_addon_skus' in tables and 'seat_block_price_id' not in _get_cols('platform_addon_skus'):
        _run_migration("add seat/egress block SKU columns", [
            "ALTER TABLE platform_addon_skus ADD COLUMN IF NOT EXISTS seat_block_price_id VARCHAR(255)",
            "ALTER TABLE platform_addon_skus ADD COLUMN IF NOT EXISTS seat_block_seats_per_unit INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE platform_addon_skus ADD COLUMN IF NOT EXISTS egress_block_price_id VARCHAR(255)",
            "ALTER TABLE platform_addon_skus ADD COLUMN IF NOT EXISTS egress_block_gb_per_unit INTEGER NOT NULL DEFAULT 100",
        ])

    # Enterprise sales leads (pricing page "Contact Sales").
    if 'enterprise_leads' not in tables:
        _run_migration("create enterprise_leads table", [
            """CREATE TABLE IF NOT EXISTS enterprise_leads (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER REFERENCES tenants(id),
                user_id INTEGER REFERENCES users(id),
                name VARCHAR(255),
                email VARCHAR(255) NOT NULL,
                company VARCHAR(255),
                message TEXT,
                status VARCHAR(32) NOT NULL DEFAULT 'new',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
        ])

    # Set per-plan rate limits for existing plans (after column is added)
    if plan_cols and 'api_rate_limit_per_minute' in _get_cols('plans'):
        _run_migration("set per-plan rate limits", [
            "UPDATE plans SET api_rate_limit_per_minute = 0 WHERE name = 'Platform'",
            "UPDATE plans SET api_rate_limit_per_minute = 60 WHERE name = 'Sandbox'",
            "UPDATE plans SET api_rate_limit_per_minute = 200 WHERE name = 'Essential'",
            "UPDATE plans SET api_rate_limit_per_minute = 500 WHERE name = 'Advanced'",
            "UPDATE plans SET api_rate_limit_per_minute = 1000 WHERE name = 'Professional'",
            "UPDATE plans SET api_rate_limit_per_minute = 0 WHERE name = 'Enterprise'",
        ])

    # Invitation model: accepted_at + metadata for external invitations
    inv_cols = _get_cols('invitations')
    if inv_cols and 'accepted_at' not in inv_cols:
        _run_migration("added accepted_at to invitations",
                       ["ALTER TABLE invitations ADD COLUMN accepted_at TIMESTAMPTZ"])
    if inv_cols and 'metadata' not in inv_cols:
        _run_migration("added metadata to invitations",
                       ["ALTER TABLE invitations ADD COLUMN metadata JSONB"])

    # changelog_entries table (new in Phase 5 LOW)
    if 'changelog_entries' not in tables:
        _run_migration("created changelog_entries table", [
            """CREATE TABLE changelog_entries (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                body TEXT NOT NULL,
                version VARCHAR(50),
                is_published BOOLEAN NOT NULL DEFAULT FALSE,
                published_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                created_by_user_id INTEGER REFERENCES users(id)
            )""",
        ])

    # build_remediations: Tier-B trust scoring columns. The table itself
    # is created by Base.metadata.create_all(); these add the columns
    # that landed in Tier B after the initial Tier-4-cache release.
    br_cols = _get_cols('build_remediations')
    if br_cols and 'failure_count' not in br_cols:
        _run_migration("added failure_count to build_remediations",
                       ["ALTER TABLE build_remediations ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"])
    if br_cols and 'last_failure_at' not in br_cols:
        _run_migration("added last_failure_at to build_remediations",
                       ["ALTER TABLE build_remediations ADD COLUMN last_failure_at TIMESTAMPTZ"])
    if br_cols and 'promoted_at' not in br_cols:
        _run_migration("added promoted_at to build_remediations",
                       ["ALTER TABLE build_remediations ADD COLUMN promoted_at TIMESTAMPTZ"])

    # Tier-E federation: federated_remediations is materialised by
    # create_all() on fresh deployments; existing deployments that
    # predate Tier E need the index sanity-check (everything else is
    # column defaults, which work for new rows).
    if 'federated_remediations' in tables:
        # No additive column migrations to run yet — the model lands
        # whole. Reserved here in case a future column is added.
        pass

    # user_sessions table (multi-session management)
    if 'user_sessions' not in tables:
        _run_migration("created user_sessions table", [
            """CREATE TABLE user_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                keycloak_session_id VARCHAR(255),
                device_info VARCHAR(500),
                ip_address VARCHAR(45),
                last_active_at TIMESTAMPTZ DEFAULT NOW() NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
            )""",
            "CREATE INDEX ix_user_sessions_user_id ON user_sessions (user_id)",
            "CREATE INDEX ix_user_sessions_keycloak ON user_sessions (keycloak_session_id)",
        ])

    # tenant-resource-monitor — singleton config table + per-task
    # aggregate table. Both materialized by Base.metadata.create_all()
    # on fresh deployments; the idempotent CREATE TABLE IF NOT EXISTS
    # below covers existing deploys.
    #
    # Pre-launch schema reshape: the original draft dollarized
    # resource consumption (usd_cents_per_* rates + dollar threshold).
    # The native-units approach below shows GPU-sec / CPU-sec /
    # RAM-GB-sec / IO-MB directly so operators see *which* resource
    # is being abused instead of a single rolled-up dollar number.
    # If a deploy is upgrading from the dollarized draft, the
    # additive migrations below add the new columns and the
    # ALTER ... DROP below drops the obsolete ones in a separate
    # transaction (best-effort, non-fatal if perms block it).
    _run_migration("create resource_monitor_config table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS resource_monitor_config ("
        " singleton_id INTEGER PRIMARY KEY DEFAULT 1,"
        " gpu_seconds_per_credit_threshold DOUBLE PRECISION NOT NULL DEFAULT 60,"
        " cpu_core_seconds_per_credit_threshold DOUBLE PRECISION NOT NULL DEFAULT 600,"
        " ram_gb_seconds_per_credit_threshold DOUBLE PRECISION NOT NULL DEFAULT 300,"
        " io_mb_per_credit_threshold DOUBLE PRECISION NOT NULL DEFAULT 1000,"
        " min_alert_credits INTEGER NOT NULL DEFAULT 10,"
        " enabled BOOLEAN NOT NULL DEFAULT TRUE,"
        " updated_by_user_id INTEGER REFERENCES users(id),"
        " audit_event_id BIGINT REFERENCES audit_events(id),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT ck_resource_monitor_config_singleton CHECK (singleton_id = 1),"
        " CONSTRAINT ck_resource_monitor_config_gpu_threshold_pos CHECK (gpu_seconds_per_credit_threshold > 0),"
        " CONSTRAINT ck_resource_monitor_config_cpu_threshold_pos CHECK (cpu_core_seconds_per_credit_threshold > 0),"
        " CONSTRAINT ck_resource_monitor_config_ram_threshold_pos CHECK (ram_gb_seconds_per_credit_threshold > 0),"
        " CONSTRAINT ck_resource_monitor_config_io_threshold_pos CHECK (io_mb_per_credit_threshold > 0),"
        " CONSTRAINT ck_resource_monitor_config_min_alert_credits_nonneg CHECK (min_alert_credits >= 0)"
        ")",
        # Seed the singleton row — ON CONFLICT keeps a re-run idempotent.
        "INSERT INTO resource_monitor_config (singleton_id) VALUES (1) ON CONFLICT (singleton_id) DO NOTHING",
    ])

    # Additive migrations for deploys that already have the dollarized
    # draft schema — add the new columns then drop the obsolete ones.
    # Each step is its own transaction; failures (e.g. dynamic Vault
    # role doesn't own the table) are non-fatal because the live
    # singleton was seeded above and the dropped columns are harmless.
    rmc_cols = _get_cols('resource_monitor_config')
    for col_name, col_def in {
        'gpu_seconds_per_credit_threshold':     "DOUBLE PRECISION NOT NULL DEFAULT 60",
        'cpu_core_seconds_per_credit_threshold': "DOUBLE PRECISION NOT NULL DEFAULT 600",
        'ram_gb_seconds_per_credit_threshold':   "DOUBLE PRECISION NOT NULL DEFAULT 300",
        'io_mb_per_credit_threshold':            "DOUBLE PRECISION NOT NULL DEFAULT 1000",
        'min_alert_credits':                     "INTEGER NOT NULL DEFAULT 10",
    }.items():
        if rmc_cols and col_name not in rmc_cols:
            _run_migration(f"added {col_name} to resource_monitor_config",
                           [f"ALTER TABLE resource_monitor_config ADD COLUMN {col_name} {col_def}"])
    for legacy in (
        'usd_cents_per_gpu_second', 'usd_cents_per_cpu_core_second',
        'usd_cents_per_ram_gb_second', 'usd_cents_per_io_mb',
        'default_leak_threshold', 'min_alert_cost_cents',
    ):
        if rmc_cols and legacy in rmc_cols:
            _run_migration(f"drop legacy {legacy} from resource_monitor_config",
                           [f"ALTER TABLE resource_monitor_config DROP COLUMN IF EXISTS {legacy}"])

    _run_migration("create task_resource_aggregate table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS task_resource_aggregate ("
        " id BIGSERIAL PRIMARY KEY,"
        " task_id INTEGER NOT NULL UNIQUE REFERENCES api_task_queue(id) ON DELETE CASCADE,"
        " tenant_id INTEGER NOT NULL REFERENCES tenants(id),"
        " user_id INTEGER REFERENCES users(id),"
        " gpu_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " cpu_core_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " ram_gb_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " vram_gb_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " disk_read_mb DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " disk_write_mb DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " wall_clock_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,"
        " started_at TIMESTAMPTZ,"
        " completed_at TIMESTAMPTZ,"
        " aggregated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " CONSTRAINT ck_task_resource_aggregate_nonneg_totals CHECK ("
        "  gpu_seconds >= 0 AND cpu_core_seconds >= 0 AND ram_gb_seconds >= 0"
        "  AND vram_gb_seconds >= 0 AND disk_read_mb >= 0 AND disk_write_mb >= 0"
        "  AND wall_clock_seconds >= 0)"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_task_resource_aggregate_tenant_completed "
        " ON task_resource_aggregate (tenant_id, completed_at)",
        "CREATE INDEX IF NOT EXISTS ix_task_resource_aggregate_user_completed "
        " ON task_resource_aggregate (user_id, completed_at)",
    ])

    # Drop dollarized cost-vector columns + rate_snapshot from the
    # aggregate table if they exist (pre-launch reshape; the leak
    # scan now compares native units directly).
    tra_cols = _get_cols('task_resource_aggregate')
    for legacy in (
        'gpu_cost_cents', 'cpu_cost_cents', 'io_cost_cents',
        'total_cost_cents', 'rate_snapshot',
    ):
        if tra_cols and legacy in tra_cols:
            _run_migration(f"drop legacy {legacy} from task_resource_aggregate",
                           [f"ALTER TABLE task_resource_aggregate DROP COLUMN IF EXISTS {legacy}"])
    if tra_cols and 'gpu_cost_cents' in tra_cols:
        # Old CHECK constraint references columns we just dropped.
        _run_migration("drop legacy cost-vector CHECK on task_resource_aggregate", [
            "ALTER TABLE task_resource_aggregate DROP CONSTRAINT IF EXISTS ck_task_resource_aggregate_nonneg_costs",
        ])

    # persistent_stacks — lifecycle failure reporting. Before the worker-task
    # completion handler existed nothing ever advanced a stack past "deploying",
    # so there was nowhere to record WHY a deploy died; these two columns are
    # what make a failed deploy legible instead of a stack stuck forever.
    ps_cols = _get_cols('persistent_stacks')
    for col_name, col_def in {
        'status_reason': "TEXT",
        'cleanup_pending': "BOOLEAN NOT NULL DEFAULT FALSE",
        # Per-service accelerator declarations parsed from x-mod.accelerator.
        # Requirements only — placement stays a just-in-time claim-time
        # decision, so there is deliberately no stored placement map here.
        'accelerator_requirements': "JSONB",
        # Ephemeral-NIM wake-ownership (services/nim_ephemeral.py) — TRUE only
        # when the on-demand path woke the NIM (so it auto-sleeps when idle);
        # FALSE for an already-on/user-pinned NIM (left running after a job).
        # Additive, inert on every non-NIM stack.
        'nim_wake_owned': "BOOLEAN NOT NULL DEFAULT FALSE",
    }.items():
        if ps_cols and col_name not in ps_cols:
            _run_migration(f"added {col_name} to persistent_stacks",
                           [f"ALTER TABLE persistent_stacks ADD COLUMN {col_name} {col_def}"])

    # Index for the secondary cleanup sweep: find stacks whose teardown was
    # dispatched but never confirmed, without scanning the whole table.
    if ps_cols:
        _run_migration("persistent_stacks cleanup_pending index",
                       ["CREATE INDEX IF NOT EXISTS ix_persistent_stacks_cleanup_pending "
                        "ON persistent_stacks (cleanup_pending) WHERE cleanup_pending = true"])

    # stack_rebalancer_config — master enable. Defaults FALSE so fixing the
    # lifecycle (which makes status='running' reachable for the first time)
    # doesn't silently activate automatic stack relocation on live servers.
    src_cols = _get_cols('stack_rebalancer_config')
    if src_cols and 'enabled' not in src_cols:
        _run_migration("added enabled to stack_rebalancer_config",
                       ["ALTER TABLE stack_rebalancer_config ADD COLUMN enabled "
                        "BOOLEAN NOT NULL DEFAULT FALSE"])


    _run_migration("worker_licenses paused-flip audit trigger (idempotent)", [
        """
        CREATE OR REPLACE FUNCTION trg_worker_paused_audit() RETURNS trigger AS $$
        BEGIN
            IF NEW.paused = true AND (OLD.paused IS DISTINCT FROM true) THEN
                RAISE LOG 'PAUSE_TRAP worker=% flipped paused false->true (pid=%, app=%)',
                    NEW.worker_name, pg_backend_pid(), current_setting('application_name', true);
                INSERT INTO audit_events (tenant_id, event_name, details, timestamp)
                VALUES (NULL, 'WORKER_PAUSED_TRAP', jsonb_build_object(
                    'worker_name', NEW.worker_name,
                    'pg_pid', pg_backend_pid(),
                    'app_name', current_setting('application_name', true)
                ), now());
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_worker_paused_audit'
            ) THEN
                CREATE TRIGGER trg_worker_paused_audit
                BEFORE UPDATE ON worker_licenses
                FOR EACH ROW EXECUTE FUNCTION trg_worker_paused_audit();
            END IF;
        END $$
        """,
    ])

    _run_migration("create wizard_event_log table (idempotent)", [
        "CREATE TABLE IF NOT EXISTS wizard_event_log ("
        " id BIGSERIAL PRIMARY KEY,"
        " event_type VARCHAR(60) NOT NULL,"
        " tenant_id INTEGER REFERENCES tenants(id),"
        " build_spec_id INTEGER REFERENCES build_specs(id),"
        " workflow_run_id BIGINT REFERENCES workflow_runs(id),"
        " metadata JSONB DEFAULT '{}'::jsonb,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")",
        "CREATE INDEX IF NOT EXISTS ix_wizard_event_log_type_created "
        "ON wizard_event_log (event_type, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_wizard_event_log_tenant "
        "ON wizard_event_log (tenant_id)",
    ])

    # mod-assistant-agent §1.4 — backfill an AssistantTenantConfig row
    # for every existing tenant so the orchestrator never has to guess
    # whether the row is missing vs. intentionally NULL. Idempotent:
    # the INSERT ... ON CONFLICT DO NOTHING means re-runs on every
    # startup are safe and cheap.
    _run_migration("backfill assistant_tenant_config rows for existing tenants", [
        "INSERT INTO assistant_tenant_config (tenant_id) "
        "SELECT id FROM tenants "
        "ON CONFLICT (tenant_id) DO NOTHING",
    ])

    # tenant-assistant-knowledge-base — per-tenant knowledge base: the
    # enable flag on assistant_tenant_config plus the document lifecycle
    # table. Both additive; retrieval/ingest stay inert until a tenant
    # flips knowledge_base_enabled on.
    if 'knowledge_base_enabled' not in _get_cols('assistant_tenant_config'):
        _run_migration("added knowledge_base_enabled to assistant_tenant_config", [
            "ALTER TABLE assistant_tenant_config "
            "ADD COLUMN knowledge_base_enabled BOOLEAN NOT NULL DEFAULT FALSE",
        ])
    if 'tenant_knowledge_documents' not in tables:
        _run_migration("created tenant_knowledge_documents table", [
            """CREATE TABLE tenant_knowledge_documents (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                source_type VARCHAR(16) NOT NULL,
                title VARCHAR(512) NOT NULL,
                file_id VARCHAR REFERENCES files(id) ON DELETE SET NULL,
                cog_id INTEGER REFERENCES cogs(id) ON DELETE SET NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                bytes INTEGER,
                error_message TEXT,
                created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""",
            "CREATE INDEX ix_tenant_knowledge_documents_tenant_id "
            "ON tenant_knowledge_documents (tenant_id)",
        ])

    # api-refactor — pricing_experiments lifecycle timestamps. The A/B test
    # start/stop/end service functions (services/admin_plans_ab_test_service.py)
    # have always set experiment.started_at / .paused_at / .completed_at, but
    # models.PricingExperiment never declared those columns, so SQLAlchemy
    # silently dropped every write (plain instance attributes, never flushed).
    # Additive + nullable: existing rows just backfill NULL for a lifecycle
    # stage they never had recorded.
    pe_cols = _get_cols('pricing_experiments')
    for col_name in ('started_at', 'paused_at', 'completed_at'):
        if col_name not in pe_cols:
            _run_migration(f"added {col_name} to pricing_experiments",
                           [f"ALTER TABLE pricing_experiments ADD COLUMN {col_name} TIMESTAMPTZ"])

    # iso-soft-delete-sweep (2026-09-22) — add the `deleted_at` column to every
    # business/audit model whose crud delete path is converted from a hard
    # `db.delete()` to a soft-delete stamp (ISO 27001 no-hard-delete — MEMORY
    # iso-no-hard-delete). Additive + nullable; existing rows backfill NULL
    # (== "live"). The model side adds SoftDeleteMixin (which also declares an
    # index on deleted_at for fresh create_all DBs); this backfills existing
    # deployments. Matches the pricing_experiments ADD-COLUMN pattern above.
    for _sd_table in (
        'accelerator_benchmark_strategies',
        'changelog_entries',
        'managers',
        'notifications',
        'tenant_knowledge_documents',
        'permission_templates',
        'smtp_settings',
        'storage_connections',
        'workflow_versions',
        'workflow_run_files',
        'tenant_node_entitlements',
        # iso-soft-delete-sweep (bulk-delete pass, 2026-09-22d) — the
        # workflow-version delete path (crud/workflow_cud.delete_workflow_version)
        # bulk-hard-deleted a version's trigger rules; now that WorkflowVersion is
        # soft-deleted (RESTRICT FK intact), the rules are soft-deleted too.
        'workflow_trigger_rules',
        # iso-soft-delete-sweep (services/routers pass, 2026-09-22b) —
        # tenant_idp_configs has no ORM model; its delete path
        # (crud/sso_idp.remove_idp_config) is converted to a raw soft-delete
        # UPDATE, so it needs the same deleted_at column.
        'tenant_idp_configs',
    ):
        if 'deleted_at' not in _get_cols(_sd_table):
            _run_migration(f"added deleted_at to {_sd_table}",
                           [f"ALTER TABLE {_sd_table} ADD COLUMN deleted_at TIMESTAMPTZ"])

    # iso-soft-delete-sweep (services/routers pass, 2026-09-22b) — the
    # tenant_idp_configs soft-delete needs its (tenant_id, name) uniqueness to
    # ignore tombstoned rows, so a re-create (or a create-rollback retry) with
    # the same name doesn't collide on a cancelled IdP. Swap the plain UNIQUE
    # constraint for a PARTIAL unique index scoped to live rows. Idempotent:
    # DROP CONSTRAINT IF EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS. The
    # keycloak_idp_alias UNIQUE is intentionally left as-is — the alias is
    # derived from a fresh (tenant_id, idp_id) per insert, so a tombstone can
    # never collide on it.
    if 'tenant_idp_configs' in tables or 'deleted_at' in _get_cols('tenant_idp_configs'):
        _run_migration(
            "tenant_idp_configs (tenant_id,name) unique -> partial (live-only)",
            [
                "ALTER TABLE tenant_idp_configs "
                "DROP CONSTRAINT IF EXISTS tenant_idp_configs_tenant_id_name_key",
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_tenant_idp_configs_tenant_name_live "
                "ON tenant_idp_configs (tenant_id, name) "
                "WHERE deleted_at IS NULL",
            ],
        )

    # GG.1 (launch-completion-megaplan, gg-commerce) — additive-only catalog
    # schema: package_items generalizes node_package_items to any kind
    # (node/stack/workflow) so a bundle can mix them; workflow_listings is a
    # published, version-pinned workflow snapshot for sale/claim (mirrors
    # node price/digest pinning); tenant_workflow_entitlements mirrors
    # TenantNodeEntitlement for workflow ownership. Nothing destructive:
    # node_package_items is untouched and stays the source of truth for
    # existing node-only readers until GG.2/GG.3 move to package_items.
    if 'package_items' not in tables:
        _run_migration("created package_items table + backfill from node_package_items", [
            """CREATE TABLE IF NOT EXISTS package_items (
                id SERIAL PRIMARY KEY,
                package_id INTEGER NOT NULL REFERENCES node_packages(id) ON DELETE CASCADE,
                kind VARCHAR(20) NOT NULL DEFAULT 'node',
                ref_id INTEGER NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                deleted_at TIMESTAMPTZ,
                CONSTRAINT ck_package_item_kind CHECK (kind IN ('node', 'stack', 'workflow')),
                CONSTRAINT uq_package_item_kind_ref UNIQUE (package_id, kind, ref_id)
            )""",
            "CREATE INDEX IF NOT EXISTS ix_package_items_package_id ON package_items (package_id)",
            "CREATE INDEX IF NOT EXISTS ix_package_items_kind_ref ON package_items (kind, ref_id)",
            "CREATE INDEX IF NOT EXISTS ix_package_items_deleted_at ON package_items (deleted_at)",
            # Idempotent backfill: one package_items(kind='node') row per
            # existing node_package_items row. ON CONFLICT DO NOTHING makes a
            # re-run (or a partial prior run) safe.
            "INSERT INTO package_items (package_id, kind, ref_id, created_at) "
            "SELECT package_id, 'node', node_definition_id, NOW() "
            "FROM node_package_items "
            "ON CONFLICT (package_id, kind, ref_id) DO NOTHING",
        ])

    if 'workflow_listings' not in tables:
        _run_migration("created workflow_listings table", [
            """CREATE TABLE IF NOT EXISTS workflow_listings (
                id SERIAL PRIMARY KEY,
                publisher_tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                source_workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
                version INTEGER NOT NULL DEFAULT 1,
                snapshot_json JSONB NOT NULL,
                node_digests JSONB,
                required_stack_listing_ids JSONB,
                price_cents INTEGER,
                price_tier VARCHAR(20),
                licence VARCHAR(20) NOT NULL DEFAULT 'open',
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                deleted_at TIMESTAMPTZ,
                CONSTRAINT uq_workflow_listing_source_version UNIQUE (source_workflow_id, version)
            )""",
            "CREATE INDEX IF NOT EXISTS ix_workflow_listings_publisher_tenant "
            "ON workflow_listings (publisher_tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_listings_source_workflow "
            "ON workflow_listings (source_workflow_id)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_listings_status ON workflow_listings (status)",
            "CREATE INDEX IF NOT EXISTS ix_workflow_listings_deleted_at ON workflow_listings (deleted_at)",
        ])

    if 'tenant_workflow_entitlements' not in tables:
        _run_migration("created tenant_workflow_entitlements table", [
            """CREATE TABLE IF NOT EXISTS tenant_workflow_entitlements (
                id SERIAL PRIMARY KEY,
                tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                workflow_listing_id INTEGER NOT NULL REFERENCES workflow_listings(id) ON DELETE CASCADE,
                installed_workflow_id INTEGER REFERENCES workflows(id) ON DELETE SET NULL,
                granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                granted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                source VARCHAR(20) NOT NULL DEFAULT 'admin_grant',
                deleted_at TIMESTAMPTZ,
                CONSTRAINT uq_tenant_workflow_entitlement UNIQUE (tenant_id, workflow_listing_id)
            )""",
            "CREATE INDEX IF NOT EXISTS ix_tenant_workflow_entitlements_tenant "
            "ON tenant_workflow_entitlements (tenant_id)",
            "CREATE INDEX IF NOT EXISTS ix_tenant_workflow_entitlements_listing "
            "ON tenant_workflow_entitlements (workflow_listing_id)",
            "CREATE INDEX IF NOT EXISTS ix_tenant_workflow_entitlements_installed_workflow "
            "ON tenant_workflow_entitlements (installed_workflow_id)",
            "CREATE INDEX IF NOT EXISTS ix_tenant_workflow_entitlements_deleted_at "
            "ON tenant_workflow_entitlements (deleted_at)",
        ])

    # GG.6c (launch-completion-megaplan, gg-commerce) — additive-only private
    # listings: visibility (public|private|unlisted, default 'public') +
    # allowed_tenant_ids (share-by-tenant allowlist, JSONB int array,
    # default '[]') on workflow_listings and node_packages. Default 'public'
    # preserves catalog-visible behavior for every pre-existing row — no
    # visibility regression on deploy. IF NOT EXISTS makes a rerun a no-op.
    _run_migration("workflow_listings + node_packages visibility columns", [
        "ALTER TABLE workflow_listings ADD COLUMN IF NOT EXISTS "
        "visibility VARCHAR(20) NOT NULL DEFAULT 'public'",
        "ALTER TABLE workflow_listings ADD COLUMN IF NOT EXISTS "
        "allowed_tenant_ids JSONB NOT NULL DEFAULT '[]'::jsonb",
        "CREATE INDEX IF NOT EXISTS ix_workflow_listings_visibility "
        "ON workflow_listings (visibility)",
        "ALTER TABLE node_packages ADD COLUMN IF NOT EXISTS "
        "visibility VARCHAR(20) NOT NULL DEFAULT 'public'",
        "ALTER TABLE node_packages ADD COLUMN IF NOT EXISTS "
        "allowed_tenant_ids JSONB NOT NULL DEFAULT '[]'::jsonb",
        "CREATE INDEX IF NOT EXISTS ix_node_packages_visibility "
        "ON node_packages (visibility)",
    ])

    # GG.6 (launch-completion-megaplan, gg-commerce) — workflow install
    # provenance: source_listing_id/source_version on `workflows`, so
    # services/workflow_listing_service.install_workflow can record which
    # WorkflowListing a clone came from (update_available compares
    # source_version against the listing's current published version).
    # NULL for every pre-existing/ordinary workflow — no behavior change.
    # OUTSIDE the create_all-gate on purpose: `workflows` always already
    # exists by the time _migrate_schema_impl runs.
    _run_migration("workflows source_listing_id/source_version columns", [
        "ALTER TABLE workflows ADD COLUMN IF NOT EXISTS "
        "source_listing_id INTEGER REFERENCES workflow_listings(id) ON DELETE SET NULL",
        "ALTER TABLE workflows ADD COLUMN IF NOT EXISTS source_version INTEGER",
        "CREATE INDEX IF NOT EXISTS ix_workflows_source_listing_id "
        "ON workflows (source_listing_id)",
    ])

    # GG.6b (launch-completion-megaplan, gg-commerce) — entitlement
    # hand-out lifecycle: `expires_at` soft-revocation marker on the node +
    # workflow entitlement rows. NULL = never expires (existing rows keep
    # permanent access). When set in the past, services.entitlement_source
    # .active_filter() treats the row as NOT entitled — no hard delete
    # (ISO 27001). `source` already exists on both tables (String, not an
    # enum) so new values (free/plan/grant/invite/trial) need no migration.
    # OUTSIDE the create_all-gate on purpose: both tables already exist on
    # every install by the time _migrate_schema_impl runs.
    _run_migration("entitlement source hand-out: expires_at columns", [
        "ALTER TABLE tenant_node_entitlements ADD COLUMN IF NOT EXISTS "
        "expires_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_tenant_node_entitlements_expires_at "
        "ON tenant_node_entitlements (expires_at)",
        "ALTER TABLE tenant_workflow_entitlements ADD COLUMN IF NOT EXISTS "
        "expires_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_tenant_workflow_entitlements_expires_at "
        "ON tenant_workflow_entitlements (expires_at)",
    ])

    # GG.6b (launch-completion-megaplan, gg-commerce) — redeemable listing
    # codes: `listing_codes` stores ONLY the sha256 hash of each code
    # (plaintext is shown once at creation, never persisted); soft delete
    # only (ISO 27001). `listing_code_redemptions` is an append-only ledger
    # whose unique (code_id, tenant_id) enforces one redemption per tenant
    # even under concurrent redeems. New tables — IF NOT EXISTS makes a
    # rerun a no-op; no ALTERs needed.
    _run_migration("listing_codes + listing_code_redemptions tables (GG.6b)", [
        """CREATE TABLE IF NOT EXISTS listing_codes (
            id SERIAL PRIMARY KEY,
            code_hash VARCHAR(64) NOT NULL,
            kind VARCHAR(20) NOT NULL,
            item_id INTEGER NOT NULL,
            source VARCHAR(20) NOT NULL,
            grant_days INTEGER,
            max_redemptions INTEGER,
            redemption_count INTEGER NOT NULL DEFAULT 0,
            expires_at TIMESTAMPTZ,
            created_by INTEGER,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            deleted_at TIMESTAMPTZ,
            CONSTRAINT uq_listing_codes_code_hash UNIQUE (code_hash)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_listing_codes_kind ON listing_codes (kind)",
        "CREATE INDEX IF NOT EXISTS ix_listing_codes_deleted_at ON listing_codes (deleted_at)",
        """CREATE TABLE IF NOT EXISTS listing_code_redemptions (
            id SERIAL PRIMARY KEY,
            code_id INTEGER NOT NULL REFERENCES listing_codes(id),
            tenant_id INTEGER NOT NULL,
            user_id INTEGER,
            redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_listing_code_redemption_code_tenant UNIQUE (code_id, tenant_id)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_listing_code_redemptions_code ON listing_code_redemptions (code_id)",
        "CREATE INDEX IF NOT EXISTS ix_listing_code_redemptions_tenant ON listing_code_redemptions (tenant_id)",
    ])

    # Backfill runs OUTSIDE the table-creation gate: create_tables() calls
    # metadata.create_all() before _migrate_schema(), so on an existing install
    # package_items already exists (empty) by the time the gate above is
    # checked and its INSERT never runs. ON CONFLICT keeps this idempotent.
    if 'node_package_items' in tables:
        _run_migration("backfill package_items from node_package_items", [
            "INSERT INTO package_items (package_id, kind, ref_id, created_at) "
            "SELECT package_id, 'node', node_definition_id, NOW() "
            "FROM node_package_items "
            "ON CONFLICT (package_id, kind, ref_id) DO NOTHING",
        ])


# B91b (startup-time) — `uvicorn --workers 8` boots EIGHT processes and each
# one runs create_tables() at import time, so the launch heal below ran 8x
# (measured 10-10: 56 tenant-heal operations, ~2.7s of import-time DB work
# per boot, 04:19:30,014 -> 04:19:32,740). The heal is idempotent and
# non-fatal, so it only needs to run once across the pod. A Redis SET NX EX
# lock (same idiom + same REDIS_URL fallback as services/node_scanner.py's
# digest-backfill hook and main.py's other startup single-flight hooks)
# makes it single-flight ACROSS the 8 workers: exactly one heals per window,
# the rest skip. TTL-bounded (never released) so a crash mid-heal can never
# wedge the key. Fail-open when Redis is unreachable — same posture as those
# hooks, and a degraded deploy keeps the pre-B91 behaviour (every worker heals).
_PLATFORM_HEAL_LOCK_KEY = "mod:startup:platform_unlimited_heal:lock"
_PLATFORM_HEAL_LOCK_EX_S = 15 * 60


def _platform_heal_lock_acquired() -> bool:
    """True when THIS process won the cross-process heal slot."""
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        if not _r.set(
            _PLATFORM_HEAL_LOCK_KEY, "1", nx=True, ex=_PLATFORM_HEAL_LOCK_EX_S
        ):
            logger.debug(
                "platform-unlimited heal: another worker holds the heal slot; skipping"
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.debug("platform-unlimited heal: lock unavailable (%s); proceeding", e)
        return True


def _ensure_platform_unlimited_plan_once():
    """Run the launch heal only if this process won the single-flight slot."""
    if _platform_heal_lock_acquired():
        ensure_platform_unlimited_plan()


def ensure_platform_unlimited_plan():
    """Launch heal — guarantee an unlimited 'Platform' plan exists and the
    platform org is subscribed to it.

    The platform owner (tim) is `is_platform_admin`, which bypasses credit gates
    for THEIR OWN requests — but non-admin sub-users in the platform tenant get
    no such bypass, and several subsystems (node builds, quotas, egress) charge
    the TENANT regardless of who acted. So the platform tenant itself must sit
    on an unlimited plan, or the org hits the Free-tier wall the moment a
    sub-user (or a system callback) does anything billable.

    Targets the platform tenant (first/lowest-id) plus any tenant containing a
    platform admin. Idempotent and non-fatal — never blocks startup.
    """
    from datetime import datetime, timedelta
    try:
        from utils.credit_manager import UNLIMITED_SENTINEL
    except Exception:
        UNLIMITED_SENTINEL = 1_000_000_000_000_000
    SENTINEL = int(UNLIMITED_SENTINEL)
    db = SessionLocal()
    try:
        plan = db.query(models.Plan).filter(models.Plan.name == "Platform").first()
        if plan is None:
            plan = models.Plan(name="Platform")
            db.add(plan)
        plan.credits_per_month = SENTINEL
        plan.max_storage_gb = SENTINEL
        plan.max_seats = SENTINEL
        plan.max_concurrent_processes = SENTINEL
        plan.max_cpu_hours_monthly = SENTINEL
        plan.price_in_cents = 0
        plan.api_rate_limit_per_minute = 0          # 0 → unlimited
        plan.allow_custom_nodes = True
        plan.allow_custom_registry = True
        plan.is_custom_plan = True
        plan.is_free_tier = False
        plan.is_visible = False                     # internal; not purchasable
        db.commit()
        db.refresh(plan)

        target_ids = set()
        first_tenant = db.query(models.Tenant).order_by(models.Tenant.id.asc()).first()
        if first_tenant is not None:
            target_ids.add(first_tenant.id)
        for u in db.query(models.User).filter(models.User.is_platform_admin.is_(True)).all():
            if u.tenant_id:
                target_ids.add(u.tenant_id)

        now = datetime.utcnow()
        far = now + timedelta(days=3650)
        for tid in target_ids:
            tenant = db.query(models.Tenant).filter(models.Tenant.id == tid).first()
            if tenant is None:
                continue
            sub = db.query(models.Subscription).filter(
                models.Subscription.tenant_id == tid,
            ).first()
            if sub is None:
                db.add(models.Subscription(
                    tenant_id=tid, plan_id=plan.id, status="active",
                    current_period_start=now, current_period_ends_at=far,
                ))
            elif sub.plan_id != plan.id:
                sub.plan_id = plan.id
                sub.status = "active"
            if (tenant.credits_remaining or 0) < SENTINEL:
                tenant.credits_remaining = SENTINEL
                tenant.credits_used_this_period = 0
                tenant.credit_period_start = now
                tenant.credit_period_end = far
            db.commit()
            logger.info("platform-unlimited heal: tenant %s on Platform plan (unlimited)", tid)
    except Exception as e:
        logger.warning("ensure_platform_unlimited_plan failed (non-fatal): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _reassign_new_table_ownership(engine):
    """Give every table the app just created to the STATIC role.

    The API connects as a rotating Vault credential, so anything
    `create_all()` materialises is owned by an ephemeral role like
    `v-approle-api-read-...`. The moment that credential rotates, the next
    role cannot ALTER what the previous one made — `must be owner of table` —
    and every future migration against it fails FOR EVER.

    Found 2026-08-15 with three tables already in that state. It had gone
    unnoticed because `_run_migration` logged the failure at DEBUG as
    "non-fatal", which it is not: it means the schema has silently stopped
    tracking the models, and the only symptom is a column that never appears.

    Membership is what makes this possible and also what makes it necessary.
    Vault's role grants the STATIC owner role — POSTGRES_USER, which is `core`
    on CE (the `modTech` fallback in _PG_OWNER_ROLE is the legacy SaaS default,
    not what CE names) — to each dynamic role, so a role CAN hand its own
    objects to it — but membership does NOT let it touch an object owned by a
    SIBLING ephemeral role. Transferring at creation is the only moment the
    current role is guaranteed to be the owner.

    1028c (BUG-1028b): the batch used to run as ONE engine.begin(), so a
    dropped connection 23 ms into cr13's first boot discarded all 345 ALTERs
    while the log still claimed success. Now: one short transaction PER object
    (a lost connection costs at most one ALTER), sequences/views/prepared
    tables are covered as well as plain tables, only objects the CURRENT
    connection user owns are touched (Postgres requires you to own an object
    to ALTER its owner), a failed object is retried ONCE on a fresh
    connection (3 attempts total per object, and a 120s wall-clock deadline for
    the whole pass so boot can never hang), and the pass is verified against
    pg_class afterwards so a residual orphan is an ERROR naming the count,
    never a success WARNING.

    Runs every startup and is cheap: on a settled database it matches nothing.
    """
    if not _PG_OWNER_ROLE:
        return
    # Imported locally to match _migrate_schema — neither is available at
    # module scope, and referencing them there is what broke startup.
    from sqlalchemy import text as sa_text
    from sqlalchemy.exc import OperationalError

    def _qi(s):
        # Catalog values are quoted, never f-string interpolated.
        return '"' + s.replace('"', '""') + '"'

    def _scan_sql(mine_only):
        # pg_class (not pg_tables): relkind r/p/S/v/m = regular, prepared,
        # sequence, view, materialized view. A sequence left owned by an
        # expired credential breaks AUTO-INCREMENT forever, same as a table.
        # Sequences reached by a serial/identity column move with their
        # table's ALTER TABLE ... OWNER, and `ALTER SEQUENCE` on an identity
        # sequence errors, so pg_depend 'a'/'i' attachments are excluded.
        # `mine_only` narrows the ALTER pass to objects the CURRENT
        # connection user owns (only the owner can ALTER an owner); the
        # verify re-scan runs the same query without that clause so sibling
        # orphans are counted too.
        return (
            "SELECT c.relkind, n.nspname, c.relname, pg_get_userbyid(c.relowner) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' "
            "AND c.relkind IN ('r','p','S','v','m') "
            "AND c.relowner <> (SELECT oid FROM pg_roles WHERE rolname = :owner) "
            + ("AND pg_get_userbyid(c.relowner) = current_user " if mine_only else "")
            + "AND NOT (c.relkind = 'S' AND EXISTS ("
            "SELECT 1 FROM pg_depend d "
            "WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid "
            "AND d.deptype IN ('a','i')))"
        )

    def _scan(conn, mine_only):
        return conn.execute(sa_text(_scan_sql(mine_only)),
                            {"owner": _PG_OWNER_ROLE}).fetchall()

    def _alter(conn, kind_sql, qual_sql):
        # Quoted: names here are ours, but an unquoted identifier would break
        # on anything case-sensitive.
        conn.execute(sa_text(
            f"ALTER {kind_sql} {qual_sql} OWNER TO {_qi(_PG_OWNER_ROLE)}"
        ))

    def _alter_one(conn, kind, schema, name):
        # `p` is a prepared table in the C source but ALTER's keyword is TABLE.
        kind_sql = {"r": "TABLE", "p": "TABLE", "S": "SEQUENCE",
                    "v": "VIEW", "m": "MATERIALIZED VIEW"}[kind]
        _alter(conn, kind_sql, f"{_qi(schema)}.{_qi(name)}")

    def _current_user(conn):
        # The connection's effective user (pg_get_userbyid of its uid), so a
        # `SET ROLE`-ed connection reports the role it is acting as.
        return conn.execute(sa_text("SELECT current_user")).fetchone()[0]

    # `engine` is the object we were handed: if _swap_engine/_rotate_credentials
    # replaces the MODULE engine mid-pass, this local still points at the
    # original, which is the one whose connections own the objects we just made.
    try:
        with engine.connect() as probe:
            current_user = _current_user(probe)
            rows = _scan(probe, True)
    except Exception as e:  # noqa: BLE001 — never block startup on this
        logger.warning("Could not scan table ownership (schema changes may "
                       "silently fail later): %s", e)
        return

    # Total bound: lock_timeout is PER object, so a pathological catalog
    # (hundreds of objects x 3 attempts x 5s) could otherwise burn ~2h of boot.
    # Past the deadline the object is left to the verify pass, which counts it.
    _deadline = time.monotonic() + 120.0

    reassigned = 0
    for (kind, schema, name, _owner) in rows:
        if time.monotonic() > _deadline:
            logger.warning("ownership: giving up after 120s — boot stays bounded; "
                           "the verify pass counts whatever is left")
            break
        if _owner != current_user:
            # Only the owner can ALTER an owner. A sibling ephemeral role's
            # object can never be fixed from here — the verify pass below
            # counts it and points at the db-owner-repair step.
            continue
        # One short transaction PER object, so a dropped backend costs at most
        # one ALTER. SET LOCAL lock_timeout: a stuck lock on one object must
        # not hang the whole boot. Retry is bounded at 3 attempts, then the
        # object is left to the verify pass, which counts it as residual.
        for attempt in range(3):
            try:
                with engine.begin() as conn:
                    conn.execute(sa_text("SET LOCAL lock_timeout = '5s'"))
                    _alter_one(conn, kind, schema, name)
                reassigned += 1
                break
            except OperationalError as e:  # dropped backend: retry, bounded
                if attempt == 2:
                    logger.warning("ownership: ALTER for %s %s.%s failed after "
                                   "3 attempts (non-fatal): %s", kind, schema,
                                   name, e)
            except Exception as e:  # noqa: BLE001 — one bad object must not stop the pass
                logger.warning("ownership: ALTER for %s %s.%s failed (non-fatal): %s",
                               kind, schema, name, e)
                break

    if reassigned:
        logger.info("ownership: reassigned %d object(s) from the rotating "
                    "credential to %s", reassigned, _PG_OWNER_ROLE)

    # Verify: the log claim must match the catalog. The bug was a WARNING that
    # claimed success while the COMMIT had been discarded.
    try:
        with engine.connect() as probe:
            current_user = _current_user(probe)
            residual = _scan(probe, False)
    except Exception as e:  # noqa: BLE001
        logger.error("ownership: verify pass failed — cannot confirm the "
                     "catalog: %s", e)
        return
    mine = [n for (_k, _s, n, o) in residual if o == current_user]
    other = [o for (_k, _s, _n, o) in residual if o != current_user]
    if not residual:
        return
    logger.error(
        "ownership: %d object(s) still not owned by %s (%d owned by other "
        "roles: run the db-owner-repair step; %d still owned by the current "
        "role: %s). Role names truncated to last 4.",
        len(residual), _PG_OWNER_ROLE, len(other), len(mine),
        sorted({o[-4:] for o in other})[:10])


def _db_bootstrap_lock_open():
    """Open the cross-process bootstrap lock file (fd must stay open: flock is
    per-fd, closing it releases the lock). Returns None when the path is not
    usable (read-only /run, tests) — the caller then runs unguarded."""
    try:
        return open(_DB_BOOTSTRAP_LOCK_PATH, "a")
    except OSError as e:
        logger.warning("create_tables: bootstrap lock file %s unavailable (%s); "
                       "running the DDL unguarded", _DB_BOOTSTRAP_LOCK_PATH, e)
        return None


def _db_bootstrap_try_lock(fh) -> bool:
    """Non-blocking flock attempt on an already-open fd."""
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as e:
        logger.warning("create_tables: bootstrap lock unusable (%s); running the "
                       "DDL unguarded", e)
        return True


def _db_bootstrap_unlock(fh) -> None:
    """Release the bootstrap flock on an already-open fd. Split out from
    `_bootstrap_ddl_single_flight()` (mirrors `_db_bootstrap_try_lock`) so tests
    can patch it instead of handing fcntl a stub fd."""
    try:
        fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass


def _db_bootstrap_blocking_lock(fh) -> None:
    """Block until the bootstrap lock is acquired (real flock, no NB flag).

    Used ONLY after the bounded poll times out. A holder that is merely SLOW
    (large DB / long `_migrate_schema`) is still running DDL — taking over at
    that point would risk concurrent/double DDL or a deadlock. A blocking
    flock is safe instead: the kernel releases the lock the instant the holder
    exits, whether that's a normal finish OR the holder process dying, so this
    can never hang against a live holder. Split out so tests can stub it
    rather than handing fcntl a non-fd stub object.
    """
    fcntl.flock(fh, fcntl.LOCK_EX)


def _bootstrap_ddl_mark_done() -> None:
    """Best-effort marker the DDL holder writes on SUCCESS only (pid/ts, no
    secrets) so a waiter that acquires the lock after the holder died mid-DDL
    can tell 'finished' apart from 'crashed before finishing'."""
    try:
        with open(_DB_BOOTSTRAP_DONE_MARKER_PATH, "w") as f:
            f.write(f"{os.getpid()} {int(time.time())}")
    except OSError:
        pass


def _bootstrap_ddl_marked_done() -> bool:
    return os.path.exists(_DB_BOOTSTRAP_DONE_MARKER_PATH)


def _bootstrap_ddl_clear_marker() -> None:
    """Remove a stale done-marker BEFORE running the DDL (best-effort).

    `/run` inside the container is an overlay mount, NOT tmpfs, so the marker
    survives `docker restart`. Without this, a crash-mid-DDL on one boot would
    leave a success marker from a PREVIOUS boot on disk, and a waiter on the
    NEXT boot would see it, wrongly conclude the DDL already ran, and serve
    against missing tables.
    """
    try:
        os.remove(_DB_BOOTSTRAP_DONE_MARKER_PATH)
    except OSError:
        pass


def _run_db_bootstrap_ddl(engine):
    """The DDL half of create_tables(): the part that must happen once per pod."""
    models.Base.metadata.create_all(bind=engine)
    # 1028c: kept here (harmless — idempotent, matches nothing on a settled DB)
    # so objects created by create_all are handed over before any later step
    # ALTERs them, AND re-run LAST below, after every step that can create an
    # object. See the tail of this function.
    _reassign_new_table_ownership(engine)
    _migrate_schema(engine)
    # 1811 — the migration-only TABLES (the CREATE TABLE DDL create_all never
    # runs). 1794 proved a fresh install bootstraps via create_all + inline
    # _migrate_schema and NEVER runs migrations/; 1794b then hoisted the CHECK/
    # FK/index OBJECTS but deliberately skipped the tables that host them, so
    # on a create_all-only DB every one of those objects aimed at e.g.
    # tenant_idp_configs or vector_search_embeddings was a WARNING + skip —
    # SSO config, SCIM and vector search are broken on every fresh CE install.
    # This lands the tables themselves (verbatim DDL, source cited per block)
    # plus their own indexes, so 1794b's fk_users_last_sso_idp_id and ix_vse_*
    # then attach. Runs FIRST of the three guard passes: the append-only
    # triggers and the constraints below both target these tables, so they
    # need the tables to already exist. Idempotent (every CREATE is IF NOT
    # EXISTS); a failure is a WARNING + skip, boot never fails because of it.
    try:
        import db_bootstrap_guards as _tbl_guards
        _table_summary = _tbl_guards.apply_migration_tables(engine)
        logging.info(
            "migration tables: %d/%d statements applied (%d failed, non-fatal)",
            _table_summary["applied"], _table_summary["statements"],
            len(_table_summary["failed"]))
    except Exception as e:  # boot must never fail because of a table
        logging.warning("migration tables skipped (non-fatal): %s", e)
    # 1794a — the migration-only append-only/no-hard-delete triggers. A fresh
    # install never runs migrations/ (1791), so without this the audit/ledger/
    # attestation tables accept UPDATE/DELETE/TRUNCATE and the ISO 27001
    # append-only guarantee is broken. Idempotent; a missing table is a WARNING
    # + skip, so boot never fails because of it.
    try:
        import db_bootstrap_guards
        _guard_summary = db_bootstrap_guards.apply_append_only_guards(engine)
        logging.info(
            "append-only guards: %d/%d statements applied (%d failed, non-fatal)",
            _guard_summary["applied"], _guard_summary["statements"],
            len(_guard_summary["failed"]))
    except Exception as e:  # boot must never fail because of the guards
        logging.warning("append-only guards skipped (non-fatal): %s", e)
    # 1794b — the migration-only CHECK/FK constraints + functional/unique
    # indexes. Same reasoning: create_all cannot express a Postgres regex
    # CHECK, a COALESCE/partial UNIQUE index or a JSONB-extraction index, and a
    # fresh install never runs migrations/ (1791), so a fresh DB ships dp_budget
    # _ledger accepting a bogus period, dp_budget_caps with duplicate cap rows
    # and app_id columns with no FK. CHECKs land NOT VALID + a separate VALIDATE
    # so a live DB with legacy rows never fails boot; every statement gets its
    # own SAVEPOINT and a failure is a WARNING + skip. No row is ever deleted.
    try:
        import db_bootstrap_guards as _mig_guards
        _constraint_summary = _mig_guards.apply_migration_constraints(engine)
        logging.info(
            "migration constraints: %d/%d statements applied (%d failed, non-fatal)",
            _constraint_summary["applied"], _constraint_summary["statements"],
            len(_constraint_summary["failed"]))
    except Exception as e:  # boot must never fail because of a constraint
        logging.warning("migration constraints skipped (non-fatal): %s", e)

    # 1028c — LAST, after EVERY step above that can create a table, sequence or
    # view (create_all, _migrate_schema, apply_migration_tables, the guards,
    # apply_migration_constraints). The early call above only covers create_all;
    # anything a later step materialises was left owned by the ephemeral
    # credential until now. Idempotent, so running it twice is cheap.
    _reassign_new_table_ownership(engine)


def _bootstrap_ddl_single_flight(engine) -> None:
    """Run the DDL half once per pod; wait for the holder if another worker has it.

    Kept as its own function so tests can exercise it without the conftest
    no-op patch on create_tables().
    """
    global _bootstrap_ddl_done
    if _bootstrap_ddl_done:
        return

    fh = _db_bootstrap_lock_open()
    if fh is None:
        # Lock file unusable (read-only /run, tests): fail open and run.
        _run_db_bootstrap_ddl(engine)
        _bootstrap_ddl_done = True
        return

    try:
        if _db_bootstrap_try_lock(fh):
            _bootstrap_ddl_clear_marker()
            _run_db_bootstrap_ddl(engine)
            _bootstrap_ddl_mark_done()
            _bootstrap_ddl_done = True
            return

        logger.info("create_tables: another process holds the bootstrap lock; waiting")
        acquired = False
        deadline = time.time() + _DB_BOOTSTRAP_WAIT_MAX_S
        while time.time() < deadline:
            time.sleep(_DB_BOOTSTRAP_POLL_INTERVAL_S)
            if _db_bootstrap_try_lock(fh):
                acquired = True
                break

        if not acquired:
            # Still held past the bounded poll: the holder is likely just slow
            # (large DB/migration), not dead. Do NOT take over here — block on
            # the kernel lock instead (see _db_bootstrap_blocking_lock).
            logger.warning(
                "create_tables: bootstrap lock still held after %ss; holder may "
                "be slow (large DB/migration) — blocking until it releases "
                "rather than risk concurrent DDL", int(_DB_BOOTSTRAP_WAIT_MAX_S))
            _db_bootstrap_blocking_lock(fh)

        if _bootstrap_ddl_marked_done():
            # The holder released AFTER marking success: tables already exist.
            logger.info("create_tables: bootstrap lock released; tables exist, skipping DDL")
            _bootstrap_ddl_done = True
            return

        # Lock released WITHOUT the done marker: the previous holder
        # crashed/died mid-DDL. We now hold the exclusive lock ourselves, so
        # no other process can be running DDL concurrently — running it here
        # is both safe and required (a waiter must not silently skip DDL and
        # serve against missing tables).
        logger.error(
            "create_tables: bootstrap lock holder released without finishing "
            "(crashed mid-DDL?); running the DDL ourselves")
        _bootstrap_ddl_clear_marker()
        _run_db_bootstrap_ddl(engine)
        _bootstrap_ddl_mark_done()
        _bootstrap_ddl_done = True
    finally:
        _db_bootstrap_unlock(fh)
        fh.close()


def create_tables():
    engine = get_engine()
    if engine is None:
        raise RuntimeError("Database not initialized")

    _bootstrap_ddl_single_flight(engine)
    # 2400CR2i: first boot of a fresh install logged "Error creating service user"
    # (not-ready condition) and came up only on the restart. Retry on a fresh
    # session, bounded (10 x 2s); after that log ERROR and continue, as before.
    for _attempt in range(10):
        if seed_service_user() is not False:
            break
        time.sleep(2)
    else:
        logger.error("service user not created after 10 attempts; platform-service key calls will fail")
    _ensure_platform_unlimited_plan_once()
