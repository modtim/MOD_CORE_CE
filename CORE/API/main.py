# API/main.py

# --- Existing Imports ---
import faulthandler
# uvicorn children were segfaulting in libpython (dmesg: segfault at a8);
# dump every thread's Python stack to stderr on SIGSEGV/SIGABRT so the
# crashing call site shows up in `docker logs`.
faulthandler.enable(all_threads=True)
from fastapi import FastAPI, HTTPException, Request
from fastapi.security import OAuth2AuthorizationCodeBearer
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import ipaddress
import os
import logging
import database
import models
from database import SessionLocal, create_tables

# Setup logging
logger = logging.getLogger(__name__)

# Check if debug mode is enabled (may expose sensitive data). GS.PSR.9:
# resolved through the DB-backed platform-settings registry (DB > DEBUG_MODE
# env > default) rather than the env var directly.
from services.debug_mode import is_debug_mode

DEBUG_MODE = is_debug_mode()

# Silence noisy third-party HTTP libraries at DEBUG/INFO. They generate
# 3-5 log lines per request and the API makes 100s of internal calls
# per minute to MinIO/Keycloak/Vault — formatting all those records is
# pure CPU waste with zero operator value. Set DEBUG_MODE=true to
# restore full verbosity for ad-hoc troubleshooting.
if not DEBUG_MODE:
    for noisy in ("urllib3", "urllib3.connectionpool", "httpcore",
                  "httpcore.http11", "httpcore.connection",
                  "httpx", "asyncio",
                  # redis-py logs "Failed to enable maintenance
                  # notifications: unknown subcommand 'MAINT_NOTIFICATIONS'"
                  # at DEBUG on every connection it opens. The server simply
                  # predates that command; nothing is wrong and nothing acts
                  # on it. Measured once stdout buffering was fixed: 82 of
                  # 144 log lines per minute — 57% of total volume — were
                  # this one message, burying everything an operator would
                  # actually want to see.
                  #
                  # Safe under ISO 27001: this list only raises the floor on
                  # THIRD-PARTY library chatter, and only to WARNING, so their
                  # errors still surface. No access decision, data change or
                  # authentication event is recorded at DEBUG anywhere in this
                  # codebase — audit.py and permissions.py contain zero
                  # logger.debug calls, and the audit trail is written to
                  # Postgres (audit_events / permission_audit_logs), never to
                  # stdout. DEBUG_MODE=true restores full verbosity.
                  "redis", "redis.connection"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
from routers import (
    auth,
    embedding_models,
    permissions,
    bulk_permissions,
    permission_templates,
    permission_analytics,
    permission_requests,
    audit_logs,
    workflows,
    workflow_callbacks,
    secrets,
    locks,
    web_search_result,
    tags,
    webhooks,
    invitations,
    me,
    users,
    teams,
    rbac,
    capabilities,
    api_keys,
    node_definitions,
    node_code_edit,  # node-code-edit-rebuild Contract 2 — source view/edit/copy/rebuild
    nodes,
    nodes_builds,
    # FIX-CE-STORE-API-DENY-PART-2 (owner 11-16, "store api routes are Deny!!!", WAVE 5
    # second rebuild): the FOUR store routers listing_visibility / listing_codes /
    # library / workflow_listings moved api_allow -> deny (CE has NO store scope). They are
    # imported below via optional_paid (the platform_license seam), NEVER in this block —
    # a top-level import of a denied module breaks the CE boot (ModuleNotFoundError).
    nodes_remediation,
    onboarding,
    files,
    workflow_run_files,
    interventions,
    intervention_callbacks,  # Slack interactive-button callback for intervention gates
    runs,  # clean-v2-api: /v1/runs (clean alias of /v1/node-runs)
    notifications,
    stream_tickets,  # WS5.34b — SSE ticket mint, registered next to notifications below
    wizard_queue,
    smtp_settings,
    private,
    private_permissions,
    storage_connections,
    hf_tokens,  # GF.C0b.2 — per-tenant HuggingFace token CRUD
    harbor,
    worker_management,
    worker_tasks,
    node_credentials,  # worker-mediated-node-credentials §3 — worker-mint run_task token endpoint
    registry_auth,
    workflow_triggers,
    trigger_fire,  # add-workflow-triggers §5 — public fire endpoints (webhook, …)
    integrations,
    resource_perms,
    platform_health,                  # Gate 1 health merge — unified /v1/health (?verbose/?extended)
    platform_feature_config,
    platform_settings,                # GS.PSR.3 — DB-backed platform settings admin surface
    changelog,
    admin_email_config,        # launch-day-followup-5 §1A
    discovery_admin,
    port_coverage,
    workflow_templates,
    platform_wizard_stats,            # wizard-rebuild §0d
    variant_patch_dispatch,
    variant_calibration,
    admin_port_analytics,
)

# --- CE-guarded router imports ---
try:
    from routers import service_accounts as _r_service_accounts
except (ImportError, ModuleNotFoundError):
    _r_service_accounts = None
    logger.warning("service_accounts router not available")

try:
    from routers import hf_cache_relayout as _r_hf_cache_relayout
except (ImportError, ModuleNotFoundError):
    _r_hf_cache_relayout = None
    logger.warning("hf_cache_relayout router not available")

try:
    from routers import node_lockdown_report as _r_node_lockdown_report
except (ImportError, ModuleNotFoundError):
    _r_node_lockdown_report = None
    logger.warning("node_lockdown_report router not available")

try:
    from routers import node_lockdown_exceptions as _r_node_lockdown_exceptions
except (ImportError, ModuleNotFoundError):
    _r_node_lockdown_exceptions = None
    logger.warning("node_lockdown_exceptions router not available")

try:
    from routers import worker_host_exec as _r_worker_host_exec
except (ImportError, ModuleNotFoundError):
    _r_worker_host_exec = None
    logger.warning("worker_host_exec router not available")

try:
    from routers import worker_host_exec_access as _r_worker_host_exec_access
except (ImportError, ModuleNotFoundError):
    _r_worker_host_exec_access = None
    logger.warning("worker_host_exec_access router not available")

try:
    from routers import worker_host_exec_output as _r_worker_host_exec_output
except (ImportError, ModuleNotFoundError):
    _r_worker_host_exec_output = None
    logger.warning("worker_host_exec_output router not available")

try:
    from routers import host_exec_targets as _r_host_exec_targets
except (ImportError, ModuleNotFoundError):
    _r_host_exec_targets = None
    logger.warning("host_exec_targets router not available")

try:
    from routers import platform_backups as _r_platform_backups
except (ImportError, ModuleNotFoundError):
    _r_platform_backups = None
    logger.warning("platform_backups router not available")

try:
    from routers import staff_register as _r_staff_register
except (ImportError, ModuleNotFoundError):
    _r_staff_register = None
    logger.warning("staff_register router not available")

try:
    from routers import ai_config as ai_config
except (ImportError, ModuleNotFoundError):
    ai_config = None
    logger.warning("ai_config router not available")

try:
    from routers import ai_transparency as _r_ai_transparency
except (ImportError, ModuleNotFoundError):
    _r_ai_transparency = None
    logger.warning("ai_transparency router not available")

try:
    from routers import subprocessor_changes as _r_subprocessor_changes
except (ImportError, ModuleNotFoundError):
    _r_subprocessor_changes = None
    logger.warning("subprocessor_changes router not available")

try:
    from routers import support_bundle as _r_support_bundle
except (ImportError, ModuleNotFoundError):
    _r_support_bundle = None
    logger.warning("support_bundle router not available")

try:
    from routers import dsar_tracker as _r_dsar_tracker
except (ImportError, ModuleNotFoundError):
    _r_dsar_tracker = None
    logger.warning("dsar_tracker router not available")

try:
    from routers import wizard_knowledge as _r_wizard_knowledge
except (ImportError, ModuleNotFoundError):
    _r_wizard_knowledge = None
    logger.warning("wizard_knowledge router not available")

# 30.6.10 /v1/credit-rate — the router module is PAID (open-core
# manifest paid-module deny: it imports utils/pricing.py, which
# CE never ships), so CE builds strip it. Import it optionally and mount the
# route only when present (the _r_* pattern above / GT.D4 entitlements_admin
# seam) — an unconditional import crashes the CE image at boot.
try:
    from routers import credit_rate as _r_credit_rate
except (ImportError, ModuleNotFoundError):
    _r_credit_rate = None
    logger.warning("credit_rate router not available (not in this edition)")

# GG.6b platform-admin grant/revoke of entitlements — PAID (open-core
# manifest deny). The CE build strips it, so import it optionally and let the
# route register only when present (GT.D4 stack_wake pattern).
try:
    from routers import entitlements_admin  # noqa: F401
except ImportError:  # noqa: BLE001 — CE build: the paid module is simply absent
    entitlements_admin = None
    logger.debug("entitlements_admin not available (not in this edition)")

# Load environment variables from a .env file
load_dotenv()

# Configure logging based on LOG_AGGREGATION_LEVEL
from utils.log_config import configure_logging
configure_logging()

# Initialize database connection (Vault dynamic creds or static fallback)
database.initialize()
create_tables()

# Import for background tasks
import threading
import time
# FX.162.p1 (owner Q157) — the ONE CE/paid detector (sentinel
# services/edition_sentinel — CUT-A2 decoupled it from the wizard_ml feature
# package; same find_spec main.py's /v1/platform/features edition line uses).
# Every edition check in the API resolves through here.
from utils import paid_import as _paid_import
from utils.paid_import import optional_paid  # FX.162.p1 module-level block (line ~2263) calls it bare — C1 commit

# 1645b (CE-boot, owner decision 10-15) — the license surface is PAID-only: CE
# denies utils/license_client.py and routers/platform_license.py outright, so
# main.py must boot with ZERO license modules present. optional_paid() returns
# None for an edition carve (and re-raises any real dependency failure).
platform_license = _paid_import.optional_paid("routers.platform_license")
_license_client = _paid_import.optional_paid("utils.license_client")
# Module-level name kept (bound to the real callable, not a bare re-export of
# the function object) so existing tests' `patch("main.validate_license", ...)`
# / `monkeypatch.setattr(main, "validate_license", ...)` keep working verbatim.
# None in CE, where the middleware itself is never registered (see below).
validate_license = _license_client.validate_license if _license_client is not None else None

# GG.UI.7d seller-scoped listing invite codes — PAID (open-core/manifest.yaml deny
# :2620/:2621, APPLY-CE-REMOVE-S01S02-01): the CE carve strips routers/seller_listing_codes.py
# + services/seller_listing_codes.py, so this router goes through the SAME optional_paid
# seam as platform_license above and is mounted only when present. A paid tree carries
# the module, so paid behaviour is unchanged; a CE build degrades to "not offered".
seller_listing_codes = _paid_import.optional_paid("routers.seller_listing_codes")

# FIX-CE-STORE-API-DENY-PART-2 (owner 11-16, "store api routes are Deny!!!", WAVE 5 second
# rebuild): CE has NO store scope (no Marketplace, Library, Seller, stack publishing), so
# routers/library.py, routers/listing_codes.py, routers/listing_visibility.py and
# routers/workflow_listings.py moved api_allow -> deny (open-core/manifest.yaml, Group A-store
# block). Same optional_paid seam as seller_listing_codes above: a paid tree carries every
# module so paid behaviour is byte-identical; a CE build degrades to "route not offered"
# (404), never a boot failure and never a silent 500.
library = _paid_import.optional_paid("routers.library")
listing_codes = _paid_import.optional_paid("routers.listing_codes")
listing_visibility = _paid_import.optional_paid("routers.listing_visibility")
workflow_listings = _paid_import.optional_paid("routers.workflow_listings")

# B91 (startup-time) — the pure-DB seed group main.py's `lifespan` single-flights
# across the uvicorn worker pool. The set lives in utils/startup_lock.py so the
# policy has ONE source of truth (main.py imports it, tests assert it); the lock
# helper itself is imported lazily inside the runner so a startup that never
# touches the group pays nothing.
from utils.startup_lock import SEED_GROUP_SINGLE_FLIGHT_TASKS as _SEED_GROUP_SINGLE_FLIGHT


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Keycloak OAuth2 Configuration ---
KEYCLOAK_EXTERNAL_URL = os.getenv("KEYCLOAK_EXTERNAL_URL", "http://localhost:8080")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM_NAME", "modtex")
KEYCLOAK_FASTAPI_CLIENT_ID = os.getenv("KEYCLOAK_FASTAPI_CLIENT_ID", "fastapi-app")

# OAuth2 scheme for dependency injection in protected routes
oauth2_scheme = OAuth2AuthorizationCodeBearer(
    authorizationUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth",
    tokenUrl=f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
)

# Lifespan context manager — replaces @app.on_event("startup")
from contextlib import asynccontextmanager

# unified-authorization RD-3 — the production process runs lifespan EXACTLY once.
# Tests, however, enter `TestClient(app)` as a context manager per test, which
# re-runs this whole lifespan each time; startup spawns ~15 daemon loop threads
# that are never stopped, so re-entry accumulates sets of them that then race on
# psycopg2 -> libpq corruption -> SIGSEGV. This guard makes startup run at most
# once per process (a no-op in production, where it runs once anyway), so a
# re-entering test gets a working app without a second set of background threads.
_LIFESPAN_STARTED = False


@asynccontextmanager
async def lifespan(app):
    """Run all startup tasks. Replaces deprecated on_event('startup')."""
    global _LIFESPAN_STARTED
    if _LIFESPAN_STARTED:
        # Re-entry (tests only). Serve the app; do NOT re-run startup/shutdown.
        yield
        return
    _LIFESPAN_STARTED = True

    # WS5.34 — the SSE stream endpoints (notifications, wizard logs) take the
    # Keycloak JWT as ``?access_token=`` because EventSource can't set headers.
    # uvicorn's ``uvicorn.access`` logger writes the full request line (query
    # string included) to the access log and does NOT propagate to the root
    # logger, so the app-level redaction filter never sees those records.
    # Attach the filter here: this runs once per web worker, AFTER uvicorn's
    # ``dictConfig`` (which would wipe an earlier attach), so it survives.
    from utils.secret_redaction import install_secret_redaction_for_logger
    install_secret_redaction_for_logger("uvicorn.access")

    # F4 — the filter above only masks values; it cannot bring back a handler.
    # A dictConfig applied with disable_existing_loggers left at its True
    # default (or a handlers block that replaces uvicorn's own) drops the
    # uvicorn.access/uvicorn.error sinks, and those loggers do NOT propagate to
    # the root, so every request line is silently discarded after a relaunch:
    # the API serves but `docker logs` shows no access lines. Re-attach them
    # (idempotent; levels and formats untouched).
    from utils.log_config import ensure_uvicorn_access_logging
    ensure_uvicorn_access_logging()

    # uvicorn's dictConfig may have wiped the root console handler set by
    # configure_logging(); restore it so the "main" logger (used by the
    # unhandled-exception handler) emits tracebacks to stdout.
    from utils.log_config import ensure_root_console_handler
    ensure_root_console_handler()

    # scale-tiered-workloads D3 / tasks §3.1 — the scheduler, ML inference and
    # ingest loops are BACKGROUND work; they must not run in a web request
    # worker once the roles are split. `should_run_background_work()` is True for
    # the `background` role and ALSO for a default single-process `web` (see
    # utils/worker_sizing.py), so a deploy with no env changes still runs
    # everything exactly as today — the split only takes effect once a
    # `background` sibling is deployed and MOD_SINGLE_PROCESS=0 is set on web.
    from utils.worker_sizing import should_run_background_work
    _run_bg = should_run_background_work()

    # The subset of startup tasks that are background work (APScheduler jobs,
    # ONNX predictor, RAG/embedding ingest). Everything else — the idempotent
    # DB/palette seeders, HMAC audit, capability scan — is one-shot setup the
    # web role still needs and is intentionally NOT gated here.
    _BACKGROUND_ONLY_TASKS = {
        "seed_embedding_models",  # HuggingFace embedding-model ingest
        "docs_rag_ingest",        # Qdrant RAG ingest
        "overlap_scheduler",      # overlap/queue scheduler loop
        "ml_baseline_seed",       # ML baseline seed (feeds onnx_model)
        "onnx_model",             # ONNX scheduler predictor + hot-reload thread
        "job_scheduler",          # APScheduler (services.scheduler)
    }

    # B91 (startup-time) — the pure-DB seed group, single-flight across the
    # uvicorn worker pool (utils.startup_lock.startup_single_flight). `uvicorn
    # --workers 8` runs this whole list EIGHT times against ONE database; the
    # 10-12 cold restart measured 16.2 s per worker of which ~10 s is this
    # group (8x "register_all_domain_nodes complete: 25 ok", 8x "All system
    # maintenance tasks completed", 8x "seed_marketplace_catalogue", ...).
    # Every entry below is an idempotent insert-only seeder/healer, so exactly
    # ONE worker per boot runs them and the other seven return immediately.
    #
    # Order WITHIN the group is unchanged (a workflow seeder must still follow
    # the nodes it references, `heal_system_workflow_nodes` still runs last).
    #
    # DELIBERATELY NOT locked: migrations, Vault/DB init, tenant creation
    # (`ensure_default_tenant` / `ensure_free_tier_plan`), `verify_email_sync`,
    # `push_realm_session_tier` and `audit_hmac` — those are order-critical
    # against the tasks around them (the realm syncs read the platform-settings
    # registry the seed group is still writing, and `audit_hmac` must stay put
    # relative to `nim_catalog`), so letting one worker own them while the
    # others race ahead would change what the other seven see. NEEDS-OWNER:
    # whether `audit_hmac` can move into the locked group once its ordering
    # dependency on `nim_catalog` is proven one-directional.
    # (The locked set itself is imported at module scope — see the
    # `from utils.startup_lock import SEED_GROUP_SINGLE_FLIGHT_TASKS` line —
    # so the policy has one source of truth.)

    # These are called in order during startup. Each is wrapped in try/except
    # so one failure doesn't block the others.
    _startup_tasks = [
        # mod-assistant-agent §2.3 — load the chat-agent site map
        # before anything else so the assistant router can serve as
        # soon as the API is up. A malformed YAML logs ERROR and
        # leaves the in-process cache None; assistant requests then
        # 503 until reload.
        ("assistant_site_map", _startup_assistant_site_map),
        # mesh-ready-build-credentials: ensure THIS manager can mint build-task
        # tokens with its own keypair. No-op for the bootstrap manager (env key);
        # a freshly-enrolled peer manager loads-or-generates its per-manager key
        # from its own Vault. Fail-safe (never crashes startup).
        ("build_task_keypair", _startup_build_task_keypair),
        # sz-mesh-infra.7w — idempotent local DERP region registration, gated
        # on mesh.tailnet_enabled. See _startup_mesh_bootstrap for details.
        ("mesh_bootstrap", _startup_mesh_bootstrap),
        # GS.PSR.5 — establish this install's initial behavioural tunables in
        # the DB-backed platform-settings registry (mode-specific, idempotent,
        # never overrides an admin's own value). Runs early so anything later
        # in startup that reads a seeded key (e.g. the wizard flags) sees the
        # seeded value, not the bare registry default.
        ("seed_platform_settings", _startup_seed_platform_settings),
        # 1681 — CE: seed the first admin's `users` row from the KC admin
        # user that keycloak-init.sh creates. Idempotent, fail-closed, only
        # in on-prem/CE (never SaaS). Runs early so the admin can use the
        # API before any manual `create_platform_admin.py` step.
        ("seed_ce_admin_user", _startup_seed_ce_admin_user),
        # CE-MOBSEED — CE: the compose `mod-migrate` one-shot now really does
        # skip subprocess seeds (--no-seeds), so the `seed:mobile_client` step
        # that used to provision the public `mod-mobile` KC client no longer
        # runs. Boot covers it in-process right here, same gate as the admin
        # user above. Idempotent + fail-safe (never crashes startup).
        ("seed_ce_mobile_client", _startup_seed_ce_mobile_client),
        # KEYCLOAK-CLI-DEVICE-CLIENT — CE: same shape, one line down: the
        # public `mod-cli` Keycloak client (device-authorization grant) so
        # command-line tools (e.g. the Scarecrow CLI) can log a user in
        # interactively. Owner owner 11-25: yes, public, no cap, no licence
        # gate. Idempotent + fail-safe, on-prem/CE only (never SaaS).
        ("seed_ce_cli_client", _startup_seed_ce_cli_client),
        # Docs RAG: idempotent ingest into Qdrant so both bots
        # (support_chat + mod-assistant-agent) have the help content
        # Self-host: seed the platform's standard embedding models into MinIO
        # (download from HF once per server on deploy) so runtime never needs
        # HuggingFace. Best-effort background thread; idempotent on warm deploys.
        ("seed_embedding_models", _startup_seed_embedding_models),
        # to answer from. Skips when the collection is already
        # populated, so this is cheap on warm deploys.
        ("docs_rag_ingest", _startup_docs_rag_ingest),
        # tenant-assistant-knowledge-base: reap KB docs stuck in 'processing'
        # from a prior run (in-process BackgroundTasks are lost on restart) so
        # a doc mid-ingest at shutdown doesn't hang forever — flip to 'failed'
        # so a tenant admin can reindex. Best-effort; never crashes startup.
        ("kb_stale_sweep", _startup_kb_stale_sweep),
        # per-rung-reasoner §2.4 — the managed-AI ladder's floors must describe
        # hardware that can actually host what each rung provisions. Runs before
        # anything provisions a stack, and compares declared numbers only, so it
        # needs no accelerator and works on a card-less box.
        ("managed_ai_ladder_check", _startup_managed_ai_ladder_check),
        # ai-config-stack-binding 1.2 (design D6) — record each legacy Vault
        # blob's alias answer as a queryable ai_slot_binding row, once, before
        # anything else reads a binding. Clean no-op while the table has not
        # been migrated in (see the guarded probe in run_migration()).
        ("ai_binding_alias_migration", _startup_ai_binding_alias_migration),
        ("seed_embedded_worker_enrollment", _startup_seed_embedded_worker_enrollment),
        # GSEC R-031 — keep the live install's name-only embedded auth window
        # alive across restarts (fresh installs are unaffected: they get the
        # registry default True). Runs before worker_discovery so the flag is
        # settled before the embedded worker's first task-plane call lands.
        ("seed_embedded_name_only_gate", _startup_seed_embedded_name_only_gate),
        ("worker_discovery", _startup_worker_discovery),
        # scheduler-runtime-config-panel: load the runtime config cache
        # before overlap_scheduler / queue_packer touch any of the
        # tunable knobs.
        ("scheduler_runtime_cache", _startup_scheduler_runtime_cache),
        ("overlap_scheduler", _startup_overlap_scheduler),
        # scheduler-ml-pipeline-hardening §2 — seed the baked-in baseline
        # into MinIO before the ONNX hot-reload thread starts polling, so
        # a fresh on-prem deploy has a model to load instead of falling
        # back to heuristics-only.
        ("ml_baseline_seed", _startup_ml_baseline_seed),
        ("onnx_model", _startup_onnx_model),
        ("job_scheduler", _startup_job_scheduler),
        # add-multi-region-mesh §3.2 — periodic per-manager gossip/sweep/failover
        # loop (per-MANAGER Redis lock; distinct managers gossip independently).
        ("mesh_gossip_loop", _startup_mesh_gossip_loop),
        # add-workflow-triggers §6.3 — DB-row LISTEN/NOTIFY trigger listener (opt-in).
        ("db_row_trigger_listener", _startup_db_row_trigger_listener),
        # add-workflow-triggers §8.2 — MESSAGE_BUS subscribers (opt-in).
        ("message_bus_subscribers", _startup_message_bus_subscribers),
        # wsip 7.1 (event half) — in-process event-bus subscriber for the
        # autonomous operator (drift + adversary-critical degradation events).
        ("autonomous_operator_subscriber", _startup_autonomous_operator_subscriber),
        ("system_maintenance", _startup_system_maintenance),
        # seed_plans removed (#15, launch-day-followup-2) — pricing
        # plans are admin-managed via Dashboard → Admin → Plans, no
        # longer auto-seeded at API startup. BUT: a fresh stack with
        # no plans at all creates a chicken-and-egg (no signup → no
        # admin → can't manage plans). LDF-4 #8 adds a minimal
        # ensure_free_tier so first-signup can complete.
        # WS2.1 — CE (open-core) editions are single-tenant: ensure the
        # DEFAULT_TENANT_ID row exists before first signup can land. No-op
        # outside the CE carve (SaaS / licensed on-prem keep per-customer
        # tenants).
        ("ensure_default_tenant", _startup_ensure_default_tenant),
        ("ensure_free_tier_plan", _startup_ensure_free_tier_plan),
        # Keep realm verifyEmail in lockstep with whether platform email is
        # configured — protects the first-admin bootstrap (no SMTP yet) and
        # heals the flag after any Keycloak recreate.
        ("verify_email_sync", _startup_verify_email_sync),
        # FX.54 p3e — re-converge the realm's session-tier + remember-me fields
        # on every API boot. The after-commit settings sync (platform_settings ->
        # session_tier_enforcement.converge_session_tier) is inert while Keycloak
        # is down, so a settings change made then would otherwise only heal on the
        # next Keycloak start; this heals it on the next API start instead.
        ("push_realm_session_tier", _startup_push_realm_session_tier),
        # LDF-4 §4.4 — flag tenant secrets whose name now collides with
        # the platform-reserved registry. One-time-style sweep that
        # audit-logs every collision so platform admins can see what
        # to clean up.
        ("audit_reserved_secret_collisions", _startup_audit_reserved_secret_collisions),
        ("trigger_run_redispatch_boot", _startup_trigger_run_redispatch),
        ("seed_capabilities", _startup_seed_capabilities),
        ("backfill_cog_owner_grants", _startup_backfill_cog_owner_grants),
        ("seed_converters", _startup_seed_converters),
        ("seed_domain_nodes", _startup_seed_domain_nodes),
        ("seed_human_approval_node", _startup_seed_human_approval_node),
        ("seed_text_llm_node", _startup_seed_text_llm_node),
        ("scan_nodes", _startup_scan_nodes),
        ("seed_build_dag_system_nodes", _startup_seed_build_dag_system_nodes),
        # Q-L-ASR — speech-to-text NIM node (inert until the owner's NGC image
        # warm-up). Seeded BEFORE the talk-to-task workflow below.
        ("seed_asr_nim_node", _startup_seed_asr_nim_node),
        ("seed_harbor_scan_followup_node", _startup_seed_harbor_scan_followup_node),
        ("seed_harbor_scan_followup_workflow", _startup_seed_harbor_scan_followup_workflow),
        # talk-to-task — task-extract / task-merge / plane system nodes,
        # seeded BEFORE the workflow graph that references them.
        ("seed_task_extract_node", _startup_seed_task_extract_node),
        ("seed_task_merge_node", _startup_seed_task_merge_node),
        ("seed_plane_node", _startup_seed_plane_node),
        ("seed_talk_to_task_workflow", _startup_seed_talk_to_task_workflow),
        ("seed_talk_to_task_stack", _startup_seed_talk_to_task_stack),
        # GF.C3 — idempotent Harbor proxy-cache bootstrap (registry
        # endpoints + non-public projects for docker.io/nvcr.io/ghcr.io).
        # Gated by the harbor_proxy_cache_enabled platform setting; a
        # deployment with no reachable platform Harbor should flip it off.
        ("ensure_harbor_proxy_cache", _startup_ensure_harbor_proxy_cache),
        # add-web-search-node — node seeder BEFORE workflow seeder.
        ("seed_web_search_node", _startup_seed_web_search_node),
        ("seed_web_search_workflow", _startup_seed_web_search_workflow),
        # Ephemeral local-NIM stacks — register the on-demand Studio Voice NIM as
        # a first-class PersistentStack (visible in the Stacks UI), resting
        # asleep. It is woken/slept per job by services/nim_ephemeral.
        ("seed_nim_stacks", _startup_seed_nim_stacks),
        # stack-training-and-prewarm §2.1 — seed a characterization for the
        # already-known NIM stacks at startup (paid layer; no-op without it).
        # Runs AFTER seed_nim_stacks so the rows exist.
        ("seed_nim_characterizations", _startup_seed_nim_characterizations),
        # marketplace-catalogue — flag a curated set of real system nodes into
        # the public marketplace (+ price a flagship). Runs AFTER scan_nodes so
        # the system nodes it flags already exist. Idempotent.
        ("seed_marketplace_catalogue", _startup_seed_marketplace_catalogue),
        # 1900Y3b — CE boot never runs migrations so the curated WorkflowTemplate
        # rows land at startup via this startup-side seed. Insert-only + idempotent.
        ("seed_workflow_templates", _startup_seed_workflow_templates),
        # 1795 (CE Gap O) — one-shot boot reconcile of the MOD Core starter
        # package. The palette gate (crud/node_definition_cud.py:260-276) shows a
        # system node only when it is curated AND the tenant holds a
        # `tenant_node_entitlements` row, and the ONLY writers of those rows are
        # the 15-min scheduler tick (services/scheduler.py `starter_package_sync`,
        # first run +90s) and the signup inline hook (crud/tenant_cud.py:345 /
        # services/subscriptions_service.py:206). A CE admin never reaches that
        # hook — `seed_ce_admin_user` routes them into the existing tenant 1, so
        # no subscription-creation path runs for them — so on a fresh CE boot the
        # table is empty for the first tick and every system node is hidden.
        # Runs AFTER the node seeds and `seed_marketplace_catalogue` above (the
        # package only takes curated nodes) and AFTER database.create_tables()
        # put the admin tenant on a plan. Edition-neutral: on SaaS this is the
        # identical idempotent sync the tick already performs, so a fresh signup
        # gets its palette before its first tick too; paid nodes stay out because
        # the eligibility filter keeps price_cents NULL/<=0.
        ("starter_package_sync", _startup_starter_package_sync),
        # 1615 (fresh-install-seed-audit-1014) — activate the workflow-driven
        # platform system-op path ("workflow drives, API executes") on a fresh
        # Paid/SaaS install: seeds the `Platform: Weekly Overage Accounting`
        # is_system Workflow + its SCHEDULE trigger (cron `0 3 * * 1`) whose
        # config names the whitelisted services.system_ops action. Without this
        # wiring nothing ever calls scripts/seed_platform_system_workflows.py,
        # so the paid job ran only on the legacy APScheduler cron fallback and
        # produced no WorkflowRun history row. Paid-tree gated (the script is
        # denied in the CE carve, so CE skips it silently — no spurious warn);
        # idempotent (the script matches an existing trigger by internal_action)
        # and needs a platform-admin `User` row to own the workflow, so a
        # zero-user fresh boot logs and re-seeds on the next boot.
        ("seed_platform_system_workflows", _startup_seed_platform_system_workflows),
        # BUG.B85c1 — heal system workflows seeded without their `workflow_nodes`
        # rows (the orchestrator makes a NodeRun only for a graph node that has
        # one, so they ran as no-ops forever). Runs LAST of the seed group so
        # every seeded workflow already exists. Insert-only + idempotent.
        ("heal_system_workflow_nodes", _startup_heal_system_workflow_nodes),
        ("audit_hmac", _startup_audit_hmac),
        ("nim_catalog", _startup_nim_catalog),
        ("nightly_freshness", _startup_nightly_freshness),
        ("base_image_rebuild", _startup_base_image_rebuild_loop),
        # Q.1a: the storage_reconcile loop was removed — tenants.storage_used_bytes
        # has ONE writer, services/storage_measure.recompute_all (scheduler job
        # "storage_metrics"); DISABLE_STORAGE_RECONCILE_LOOP now gates that job.
        # remove-minio-versitygw Gate 3 (D4): the MinIO trigger-notification
        # reconcile is gone — file_event triggers are emitted API-side now, so
        # startup makes NO bucket-notification-config call.
        # calibration-health-fixtures-and-timeouts §6 — one-shot, read-only
        # fixture-presence audit for every calibration-eligible NodeDef.
        ("calibration_fixture_audit", _startup_calibration_fixture_audit),
        ("wizard_llm_warmup", _startup_wizard_llm_warmup),
        ("changelog_publish_loop", _startup_changelog_publish_loop),
        ("payments_readiness_check", _startup_payments_readiness_check),
        # GY.C2a — boot known-answer crypto self-test (SC-13), cached in-process.
        ("crypto_self_test", _startup_crypto_self_test),
        # 1049c — per-environment release signing key self-provision.
        ("release_key_bootstrap", _startup_release_key_bootstrap),
    ]
    for name, func in _startup_tasks:
        # scale-tiered-workloads D3 — skip background-only loops when this
        # process does not run background work (i.e. a web replica with a
        # deployed background sibling). Default deploy runs them all.
        if name in _BACKGROUND_ONLY_TASKS and not _run_bg:
            logger.info(
                "Startup task '%s' skipped — background work runs on the "
                "background role, not this web process", name)
            continue
        # B91 — pure-DB seed group: one worker per boot runs it, the rest skip.
        # The lock helper logs the skip itself and never raises (fail-open when
        # Redis is down), so the per-task try/except below still covers the
        # tasks that run.
        if name in _SEED_GROUP_SINGLE_FLIGHT:
            from utils.startup_lock import startup_single_flight
            if not startup_single_flight(name, ex=1800):
                continue
        try:
            func()
        except Exception as e:
            logger.error(f"Startup task '{name}' failed: {e}")

    # --- queue_empty tenant cache ---------------------------------------
    try:
        from services.queue_empty_cache import refresh_tenant_cache as _refresh_qe
        _db = SessionLocal()
        try:
            _refresh_qe(_db)
        finally:
            _db.close()
    except Exception as e:
        logger.error("queue_empty_cache startup refresh failed: %s", e)

    # --- log_pattern_alert trigger handler -------------------------------
    # Registered here (not in _startup_tasks) because attaching to the
    # asyncio loop and spawning the consumer task both require the
    # running event loop, which only exists inside lifespan.
    consumer_task = None
    try:
        import asyncio as _asyncio
        from services.log_trigger_handler import (
            get_handler,
            log_trigger_consumer,
            refresh_active_snapshot,
        )
        handler = get_handler()
        # Floor at WARNING so DEBUG/INFO records short-circuit before
        # reaching the regex check.
        handler.setLevel(logging.WARNING)
        # Attach the loop FIRST so the handler can enqueue from any
        # thread once the snapshot is populated.
        handler.attach_loop(_asyncio.get_running_loop())
        # Register on the root logger.
        root = logging.getLogger()
        if handler not in root.handlers:
            root.addHandler(handler)
        # Populate the snapshot from the DB.
        _db = SessionLocal()
        try:
            refresh_active_snapshot(_db)
        finally:
            _db.close()
        # Start the consumer task.
        consumer_task = _asyncio.create_task(log_trigger_consumer())
        logger.info("log_pattern_alert handler installed (snapshot=%d)",
                    handler.snapshot_size())
    except Exception as e:
        logger.error("log_pattern_alert handler startup failed: %s", e)

    # --- scheduler MAE gauge refresh loop (Phase 2 §3.3) -----------------
    # scale-tiered-workloads D3 — scheduler telemetry is background work; only
    # start it where background work runs (background role, or default web).
    mae_gauge_task = None
    if _run_bg:
        try:
            import asyncio as _asyncio2
            from services.scheduler_mae_gauge import refresh_loop as _mae_refresh_loop
            mae_gauge_task = _asyncio2.create_task(_mae_refresh_loop(SessionLocal))
            logger.info("scheduler_mae_gauge refresh loop started")
        except Exception as e:
            logger.error("scheduler_mae_gauge startup failed: %s", e)
    else:
        logger.info("scheduler_mae_gauge skipped — background work not on this role")

    # --- mesh/replication metrics refresh loop (per-worker) --------------
    mesh_metrics_task = None
    try:
        from utils.paid_import import optional_paid
        _mesh_mod = optional_paid("services.mesh_metrics")
        if _mesh_mod is None:
            logger.info("services.mesh_metrics not in this edition; skipped")
        else:
            import asyncio as _asyncio3
            mesh_metrics_task = _asyncio3.create_task(_mesh_mod.refresh_loop(SessionLocal))
            logger.info("mesh_metrics refresh loop started")
    except Exception as e:
        logger.error("mesh_metrics startup failed: %s", e)

    # --- compliance-fortress 9.1a: egress-denial metric refresh (per-worker) --
    egress_denial_metrics_task = None
    try:
        from utils.paid_import import optional_paid
        _ed_mod = optional_paid("services.egress_denial_metrics")
        if _ed_mod is None:
            logger.info("services.egress_denial_metrics not in this edition; skipped")
        else:
            import asyncio as _asyncio4
            egress_denial_metrics_task = _asyncio4.create_task(
                _ed_mod.refresh_loop(SessionLocal)
            )
            logger.info("egress_denial_metrics refresh loop started")
    except Exception as e:
        logger.error("egress_denial_metrics startup failed: %s", e)

    # --- orchestration queue consumer (scale-tiered-workloads Phase 4 / D2) --
    # When MOD_ORCHESTRATION_VIA_QUEUE is on, Execute enqueues an `orchestration`
    # ApiTaskQueue row instead of running start_workflow_execution as an
    # in-process request-worker BackgroundTask. This daemon drains that queue on
    # the background role (row-claim sharded across replicas — NOT under the
    # leader lock, since claims are naturally exclusive). It is started only
    # where background work runs AND only when the flag is on, so a default
    # deploy (flag off) starts no new thread and behaves exactly as today.
    try:
        from services.orchestration_consumer import (
            orchestration_via_queue_enabled,
            start_orchestration_consumer,
        )
        if _run_bg and orchestration_via_queue_enabled():
            start_orchestration_consumer()
            logger.info("orchestration queue consumer started (D2)")
        else:
            logger.info(
                "orchestration queue consumer not started "
                "(run_bg=%s, via_queue=%s)",
                _run_bg, orchestration_via_queue_enabled())
    except Exception as e:
        logger.error("orchestration_consumer startup failed: %s", e)

    yield

    # --- shutdown --------------------------------------------------------
    if consumer_task is not None:
        consumer_task.cancel()
    if mae_gauge_task is not None:
        mae_gauge_task.cancel()
    if mesh_metrics_task is not None:
        mesh_metrics_task.cancel()
    if egress_denial_metrics_task is not None:
        egress_denial_metrics_task.cancel()


# Instantiate the main FastAPI application
# API docs are always public (standard SaaS practice)
app = FastAPI(
    lifespan=lifespan,
    title="MOD Platform API",
    description=(
        "API for MOD — visual computing workflow automation platform. Manage files, workflows, and workers.\n\n"
        "**To try the API:** click **Authorize**, then sign in with your MOD account "
        "(you are taken to the normal sign-in page). The Bearer box is only for "
        "pasting an access token you already have."
    ),
    # EARLY.API-REPORTS-PRODUCT-VERSION (lane r9700, 11-25): the FastAPI app
    # version (and therefore the OpenAPI `info.version` via version=app.version
    # in custom_openapi) reports the PRODUCT version, not the FastAPI's own
    # 2.0.0. Single source of truth: PRODUCT_VERSION env (startup/.env.example
    # ships it), default pinned by test_api_reports_product_version.py to
    # CORE/Dashboard/package.json "version" (1.1.0) — the same value the Helm
    # pin test (test_helm_chart_version_matches_product.py) and the release
    # stamp read. No runtime file needed in the API image: the env default
    # keeps the container honest even when Dashboard/package.json is absent.
    version=os.getenv("PRODUCT_VERSION", "1.1.0"),
    # GSEC R-014: docs are gated by the `api.public_docs_enabled` platform
    # setting (default OFF). FastAPI's built-in docs_url/redoc_url/openapi_url
    # are disabled and re-created as explicit, setting- and admin-gated routes
    # below (FastAPI's documented custom-docs pattern).
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    swagger_ui_init_oauth={
        "clientId": KEYCLOAK_FASTAPI_CLIENT_ID,
        "usePkceWithAuthorizationCodeGrant": True,
        "scopes": "openid profile email",
    },
    contact={"name": "MOD Tech Labs", "url": "https://example.com"},
)

# Add secure logging middleware for ISO 27001 compliance
from utils.secure_logging import SecureLoggingMiddleware, RequestContextMiddleware
app.add_middleware(SecureLoggingMiddleware, log_requests=True, log_responses=False)
app.add_middleware(RequestContextMiddleware)

# Defense-in-depth for Starlette's redirect_slashes: rewrite the Location on
# trailing-slash 307/308 redirects to a relative path so the browser keeps
# the request's own origin, instead of whatever scheme/host uvicorn resolved
# for that hop (wrong when a request bypasses the Caddy/nginx proxy chain
# that forwards Host + X-Forwarded-Proto). See launch-completion-megaplan
# (dash-permissions:33/69, dash-files:111) and middleware/relative_redirect_middleware.py.
from middleware.relative_redirect_middleware import RelativeSlashRedirectMiddleware
app.add_middleware(RelativeSlashRedirectMiddleware)

# Add audit middleware — logs every authenticated request to audit_events table
# Compliance: ISO 27001 A.12.4.1, CMMC AU-2, SOC2 CC6.1
from middleware.audit_middleware import AuditMiddleware
app.add_middleware(AuditMiddleware, db_factory=SessionLocal)

# GSEC R-155 — API-side upload size cap (per path class; platform-settings
# tunables with in-process cache, fail-safe defaults).
from middleware.upload_size_cap import UploadSizeCapMiddleware
app.add_middleware(UploadSizeCapMiddleware)

# FX.133 p3 (owner Q121=A) — Global Privacy Control (Sec-GPC) honoured +
# recorded. A browser sending `Sec-GPC: 1` has declared "do not use my data
# for tracking/advertising/sale": we record that opt-out ONCE per subject as
# a `restriction` DSR on the data-subject ledger (+ one audit row) and the
# consent/tracking decisions read it (services/email_unsubscribe.gpc_opted_out
# suppresses every non-service category for such a subject). Gated by the
# dsr.gpc_enabled platform setting (registered in-module in
# services/dsr_requests.py, default True). The docs-site public-trust page
# claims this reading; this is what makes it true.
try:
    from middleware.gpc_middleware import GPCMiddleware
    app.add_middleware(GPCMiddleware, db_factory=SessionLocal)
except ImportError:  # open-source Core build: middleware not present
    pass

# add-scim-directory-sync §5.2 — synchronous SCIM activity log middleware.
# Captures every /scim/v2/* request+response to scim_activity_events
# BEFORE returning the response, so admins can debug failed syncs in
# real time. Per-request overhead is one write to a partitioned table.
# SCIM is a Paid module: present in the full build, absent in CE. Add the
# activity-log middleware only if present. No-op when the module IS present.
try:
    from middleware.scim_activity_middleware import ScimActivityMiddleware
    app.add_middleware(ScimActivityMiddleware, db_factory=SessionLocal)
except ImportError:
    pass  # open-source Core build: SCIM middleware not present


# add-scim-directory-sync §2.5 — SCIM-shaped error body. SCIM clients
# (Okta, Azure AD) reject FastAPI's default {"detail": "..."} response;
# they require the RFC 7644 §3.12 schemas+status+scimType+detail tuple.
# Route /scim/v2/* exceptions through this handler so they get the
# right shape.
#
# `scim` (CORE/API/scim/*.py) is a Paid module: denied in
# open-core/manifest.yaml like the SCIM router/middleware above. Guard the
# import the same way so a CE boot doesn't crash on an unconditional
# `from scim.errors import ...` (open-core boot-clean smoke test, WS2.2).
_scim_errors = optional_paid("scim.errors")  # scim/* is a Paid module (manifest deny)
if _scim_errors is not None:
    from fastapi.responses import JSONResponse as _ScimJSONResponse
    from fastapi.requests import Request as _ScimRequest

    @app.exception_handler(_scim_errors.ScimHTTPException)
    async def _handle_scim_error(request: _ScimRequest, exc: _scim_errors.ScimHTTPException):
        body = _scim_errors.scim_error_body(
            status_code=exc.status_code,
            detail=exc.scim_detail or str(exc.detail),
            scim_type=exc.scim_type,
        )
        return _ScimJSONResponse(
            status_code=exc.status_code,
            content=body,
            media_type="application/scim+json",
            headers=getattr(exc, "scim_extra_headers", None) or {},
        )
else:  # open-source Core build: SCIM package not present
    logger.debug("scim.errors not available (not in this edition)")


# api-30.6.14 (2400GX-c14a) — ApiError handler (developer-controlled codes)
# + a general HTTPException handler that auto-injects `code` for
# snake_case string detail; dict detail gets top-level code from
# detail["code"] / detail["error"] if it matches CODE_RE.
# Starlette MRO: ScimHTTPException (822) wins over HTTPException for SCIM.
import utils.http_errors as _api_errors
from utils.http_errors import ApiError as _ApiError, CODE_RE as _CODE_RE
from starlette.exceptions import HTTPException as _StarletteHTTPException


@app.exception_handler(_ApiError)
async def _handle_api_error(request: Request, exc: _ApiError):
    from fastapi.responses import JSONResponse
    import services.platform_settings as _ps

    body: dict = {"detail": exc.detail}
    if _CODE_RE.fullmatch(exc.code):
        body["code"] = exc.code

    if exc.status_code in (429, 503):
        if exc.retry_after is None:
            try:
                retry_after = _ps.get_setting("api.retry_after_default_seconds", int, 30)
            except Exception:
                retry_after = 30
            if not isinstance(retry_after, int):
                retry_after = 30
            if retry_after < 1:
                retry_after = 1
            elif retry_after > 3600:
                retry_after = 3600
        else:
            retry_after = exc.retry_after
        headers: dict[str, str] = {"Retry-After": str(retry_after)}
    else:
        headers = {}

    if exc.headers:
        headers.update(exc.headers)

    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=headers,
    )


# api-30.6.14 (2400GX-c14a) — general HTTPException handler.
# Catches starlette.exceptions.HTTPException (covers FastAPI's subclass
# and router 404/405). Starlette resolves handlers by MRO, so the SCIM
# handler (ScimHTTPException, main.py:822) still wins for SCIM.
# List/None detail passes through unchanged; non-matching str detail →
# {"detail": d} only; dict/detail gets top-level code from code/error key
# if it matches CODE_RE. 422 RequestValidationError is untouched.
@app.exception_handler(_StarletteHTTPException)
async def _handle_http_exception(request: Request, exc: _StarletteHTTPException):
    from fastapi.responses import JSONResponse

    # FastAPI wraps the real data in HTTPException.data; starlette's default
    # ServerErrorMiddleware returns {"detail": str(exc)} for 204/304 no-body.
    # We match that shape exactly: pass list/None body through, inject code
    # for snake_case str, or extract code from dict detail.
    detail = exc.detail
    if isinstance(detail, str):
        if _CODE_RE.fullmatch(detail):
            body = {"detail": detail, "code": detail}
        else:
            body = {"detail": detail}
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=getattr(exc, "headers", {}) or {},
        )
    if isinstance(detail, dict):
        code_val: str | None = None
        if "code" in detail and isinstance(detail["code"], str):
            code_val = detail["code"]
        elif "error" in detail and isinstance(detail["error"], str):
            code_val = detail["error"]
        if code_val and _CODE_RE.fullmatch(code_val):
            body = {"detail": detail, "code": code_val}
        else:
            body = {"detail": detail}
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=getattr(exc, "headers", {}) or {},
        )
    # list / None / anything else → pass through unchanged (FastAPI default)
    return JSONResponse(
        status_code=exc.status_code,
        content=detail,
        headers=getattr(exc, "headers", {}) or {},
    )


# managed-ai-surfaces-and-stack-ops §2.4 — a rate-limit refusal is a decision,
# not a crash. RateLimited is raised deep inside utils.llm_client, far from any
# router, so without this it would surface as a 500 and clients would retry it
# as though the server were broken. 429 with Retry-After is the answer a client
# can actually act on.
try:
    from services.llm_rate_limit import RateLimited as _RateLimited

    @app.exception_handler(_RateLimited)
    async def _handle_llm_rate_limited(request, exc: _RateLimited):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=429,
            content={
                "detail": exc.message,
                "cap": exc.cap,
                "retry_after_seconds": exc.retry_after_s,
                "code": "rate_limited",
            },
            headers={"Retry-After": str(exc.retry_after_s)},
        )
except ImportError:
    pass  # llm_rate_limit not available


# 1634p (re-scoped, lead decision) — central edition-missing handler. Rather
# than hand-wrapping all 199 function-level paid-only imports in
# try/except->None (which just turns a 500 ImportError into a 500 TypeError
# one line later), catch ImportError/ModuleNotFoundError app-wide and, ONLY
# when utils.paid_import.classify_missing_import() confirms the missing name
# is a first-party module genuinely absent from this edition's build, return
# a clean 402. Anything else (third-party dep missing, or a module that
# exists but broke while loading — a real bug) re-raises: Starlette's
# ExceptionMiddleware propagates the re-raise out to the ServerErrorMiddleware
# / _handle_unhandled_exception below, so it still gets the normal 500 +
# error_id + traceback log, nothing is swallowed.
@app.exception_handler(ImportError)
async def _handle_edition_missing_import(request: Request, exc: ImportError):
    from fastapi.responses import JSONResponse
    from utils.paid_import import classify_missing_import

    missing = classify_missing_import(exc)
    if missing is None:
        raise exc
    return JSONResponse(
        status_code=402,
        content={
            "error_code": "edition_feature_unavailable",
            "detail": "This feature is not available in this edition.",
            "module": missing,
        },
    )


# api-500-traceback (launch-completion-megaplan) — every unhandled exception
# must produce exactly ONE ERROR log with a full traceback plus an error_id,
# and the client must get a sanitized 500 JSON carrying that same error_id
# (CLAUDE.md: "Server error — 500 with error_id (UUID) for troubleshooting").
#
# Before this handler existed there was no @app.exception_handler(Exception):
# Starlette's default ServerErrorMiddleware caught the exception, returned a
# bare "Internal Server Error" plain-text 500 (no JSON, no error_id), and the
# ONLY thing logged anywhere was SecureLoggingMiddleware's
# `logger.warning(f"Request to {path} failed: {type(e).__name__}")` —
# WARNING level, no exc_info/traceback, no exception message, no error_id.
# That's why `raise Exception(...)` in crud/secret_cud.py (POST /v1/secrets)
# produced a 500 with NOTHING useful in `docker logs my_fastapi_app`.
#
# This is registered as a plain `Exception` handler, which Starlette treats
# specially: it becomes the `ServerErrorMiddleware` handler (the outermost
# middleware), so it only fires for exceptions no router/dependency/other
# exception_handler already handled — HTTPException (404s, 4xxs, the SCIM
# and RateLimited handlers above) never reach it, so there's no double log
# for expected, deliberate error responses.
@app.exception_handler(Exception)
async def _handle_unhandled_exception(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    import uuid as _uuid

    error_id = str(_uuid.uuid4())
    # Log ONLY method + path + exception type/message + traceback. Never the
    # request body or headers (Authorization, secret values, etc. may be in
    # either) — ISO 27001 A.12.4.1 plaintext-credentials-in-logs constraint.
    logger.error(
        "Unhandled exception error_id=%s method=%s path=%s",
        error_id,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error_id": error_id},
    )


# FIX-API-DB-DNS-FAILURE (FIND-4 follow-up, xeon trace 2026-11-15 22:26:46Z):
# during the boot/recovery window the DB host name ("postgres-app") can be
# unresolvable for a few seconds; every request that touched the DB then
# produced a full 500 + traceback log per request (the _handle_unhandled_exception
# path above). A DB connection failure is transient by nature, so answer 503
# with Retry-After: 5 and exactly ONE warning line per failure instead.
# Non-connection DBAPIErrors (IntegrityError etc.) keep today's 500 behaviour.
# Shared code: paid builds get the same 503+Retry-After on DB connection failure.
from sqlalchemy.exc import DBAPIError as _DbApiError, OperationalError as _DbOperationalError

_DB_CONNECTION_MARKERS = (
    "could not translate host name",
    "connection refused",
    "server closed the connection",
    "connection invalidated",
    "couldn't get a connection",
    "network name is unknown",
)


def _is_db_connection_failure(exc) -> bool:
    if isinstance(exc, _DbOperationalError) or getattr(exc, "connection_invalidated", False):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _DB_CONNECTION_MARKERS)


@app.exception_handler(_DbApiError)
async def _handle_db_connection_failure(request: Request, exc: _DbApiError):
    from fastapi.responses import JSONResponse

    if not _is_db_connection_failure(exc):
        # Not a connection-class failure: fall back to the generic 500 +
        # error_id handler (re-raising out of this handler reaches it).
        raise exc
    # One warning line per failure, sanitized: exception class + first 200
    # chars of the message with anything after "://" removed (never the DSN,
    # host credentials or password - ISO 27001 A.12.4.1).
    msg = str(exc)[:200]
    if "://" in msg:
        msg = msg.split("://", 1)[0]
    # str(DBAPIError) carries newlines ("[SQL: ...]" / "(Background on ...") -
    # collapse them so the failure stays ONE log line.
    msg = " ".join(msg.split())
    logger.warning("DB connection failure type=%s path=%s msg=%s", type(exc).__name__, request.url.path, msg)
    return JSONResponse(
        status_code=503,
        content={"detail": "database temporarily unavailable"},
        headers={"Retry-After": "5"},
    )


# Custom OpenAPI schema to add OAuth2 security scheme
_CE_DOCS_DESCRIPTION = (
    "API for MOD - visual computing workflow automation platform.\n\n"
    "**To try the API from this page (3 steps):**\n\n"
    "1. In the MOD Dashboard open **Settings**, then the **API Keys** tab, create a key "
    "and copy it.\n"
    "2. Below, open **API Keys > POST /v1/api-keys/token**, click **Try it out**, put your "
    "key in the `api_key` field, click **Execute**, and copy the `access_token` value "
    "from the response.\n"
    "3. Click **Authorize** at the top of this page, paste that token into the Value box, "
    "click **Authorize**, then **Close**.\n\n"
    "The token lasts one hour; repeat step 2 for a new one. Outside this page, send it as "
    "`Authorization: Bearer <token>`."
)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    # --- Swagger "Authorize" support ---------------------------------------
    # NOTE: get_openapi() already emits an "OAuth2AuthorizationCodeBearer"
    # security scheme (derived from the OAuth2AuthorizationCodeBearer dependency)
    # and every protected operation references it by that exact name. The old
    # code REPLACED components.securitySchemes with a differently-named
    # "OAuth2AuthorizationCode" entry, which deleted the scheme the operations
    # actually point at — so Swagger UI's Authorize button offered a scheme no
    # endpoint used, and "Try it out" never attached a credential. We now MERGE
    # instead of replacing, and add a simple HTTP Bearer scheme so a user can
    # paste a JWT and have it applied to every request.
    if "components" not in openapi_schema:
        openapi_schema["components"] = {}
    security_schemes = openapi_schema["components"].setdefault("securitySchemes", {})

    auth_url = f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/auth"
    token_url = f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    oauth_scopes = {
        "openid": "OpenID Connect",
        "profile": "User profile",
        "email": "Email address",
    }

    # HTTP Bearer — paste a token directly (works even if the OAuth redirect
    # flow can't complete in a given environment).
    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Paste a Keycloak access token (JWT). It is sent as "
                       "'Authorization: Bearer <token>' on every request.",
    }

    # Ensure the auto-generated OAuth2 authorization-code scheme carries the
    # Keycloak realm URLs + scopes so Swagger UI's OAuth login (with PKCE, per
    # swagger_ui_init_oauth) can drive the modtex realm end-to-end.
    security_schemes["OAuth2AuthorizationCodeBearer"] = {
        "type": "oauth2",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": auth_url,
                "tokenUrl": token_url,
                "scopes": oauth_scopes,
            },
            # No password flow: the fastapi-app Keycloak client has
            # directAccessGrantsEnabled=false, so a Swagger username/password
            # form could never work. Sign-in goes through the redirect flow.
        },
    }

    # Offer BearerAuth on every protected operation (alongside the OAuth2
    # requirement FastAPI already attached) so a pasted token authorizes any
    # "Try it out" call, and set a global default for the rest.
    openapi_schema["security"] = [
        {"BearerAuth": []},
        {"OAuth2AuthorizationCodeBearer": list(oauth_scopes.keys())},
    ]
    for path_item in openapi_schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            if "security" in operation:
                # Contract hygiene: FastAPI emits one requirement per SecurityBase
                # INSTANCE, so tenant-scoped ops list OAuth2AuthorizationCodeBearer
                # twice (the route's required oauth2_scheme + the tenant_lifecycle
                # gate's auto_error=False oauth2_scheme_optional via
                # get_optional_current_user). Same scheme, same scopes -> dedup the
                # doc (docs-only; runtime enforcement is the dependency graph).
                deduped: list = []
                seen_reqs: set = set()
                for req in operation["security"]:
                    if not isinstance(req, dict):
                        continue
                    fingerprint = tuple(
                        (k, tuple(v) if isinstance(v, list) else v)
                        for k, v in sorted(req.items())
                    )
                    if fingerprint in seen_reqs:
                        continue
                    seen_reqs.add(fingerprint)
                    deduped.append(req)
                names = {next(iter(req.keys())) for req in deduped if req}
                if "BearerAuth" not in names:
                    deduped.append({"BearerAuth": []})
                operation["security"] = deduped

    # --- CE /docs Authorize: ONE scheme (Tim, Gate 7 sitting 3, "how does someone use
    # this???"). CE's only browser-capable Keycloak client for :8000 (fastapi-app) is
    # CONFIDENTIAL, so Swagger's OAuth2 form shows client_id + client_secret boxes that a
    # normal user cannot fill, and no public client accepts the :8000 docs redirect
    # without a realm change. CE therefore documents the path that works end to end:
    # API key -> POST /v1/api-keys/token -> paste the access_token in BearerAuth. Paid
    # keeps the OAuth2 block unchanged. Docs-only; runtime auth is untouched.
    if _paid_import.is_ce_edition():
        _ce_oauth = "OAuth2AuthorizationCodeBearer"
        security_schemes.pop(_ce_oauth, None)
        security_schemes["BearerAuth"]["description"] = (
            "Paste the access_token you got from POST /v1/api-keys/token (see the steps "
            "at the top of this page). Paste only the token, without the word Bearer."
        )
        openapi_schema["info"]["description"] = _CE_DOCS_DESCRIPTION
        openapi_schema["security"] = [{"BearerAuth": []}]
        for path_item in openapi_schema.get("paths", {}).values():
            for operation in path_item.values():
                if isinstance(operation, dict) and "security" in operation:
                    operation["security"] = [
                        req for req in operation["security"]
                        if isinstance(req, dict) and _ce_oauth not in req
                    ]

    # --- 30.6.6 c6-J3: OpenAPI 3.1 `webhooks:` contract for the signed run-completion
    # callback (docs-only; the sender is services/run_completion_webhooks.py).
    if str(openapi_schema.get("openapi", "")).startswith("3.1"):
        from services import run_completion_webhooks as _rcwh  # header names, not literals
        from schemas import WebhookEventPayload as _WebhookEventPayload

        openapi_schema.setdefault("webhooks", {})["runCompletion"] = {"post": {
            "summary": "MOD run-completion callback (run.completed / run.failed / run.cancelled)",
            "description": (
                "MOD POSTs this to every registered webhook subscription when a workflow run "
                "reaches a terminal state. Verify the body with HMAC-SHA256 over "
                "\"<X-MOD-Timestamp>.<raw request body>\" keyed by the subscription's signing "
                "secret, compared in constant time against the `sha256=<hex>` value in "
                "`X-MOD-Signature`; reject timestamps older than your tolerance window. "
                "Any non-2xx response or a >10s timeout is retried on a 1/5/30/120/360-minute "
                "ladder (6 attempts total). The signing secret is shown only at create/rotate."
            ),
            "parameters": [
                {"name": _rcwh.SIG_HEADER, "in": "header", "required": True,
                 "schema": {"type": "string"},
                 "description": "Signature: `sha256=<hex HMAC-SHA256 of \"<timestamp>.<raw body>\">`."},
                {"name": _rcwh.TIMESTAMP_HEADER, "in": "header", "required": True,
                 "schema": {"type": "integer"},
                 "description": "Unix seconds at signing time; the exact value inside the signature."},
                {"name": _rcwh.EVENT_HEADER, "in": "header", "required": True,
                 "schema": {"type": "string"},
                 "description": "Event name: run.completed, run.failed or run.cancelled."},
                {"name": _rcwh.DELIVERY_HEADER, "in": "header", "required": True,
                 "schema": {"type": "string"},
                 "description": "Delivery UUID; identical across retries of the same delivery."},
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/WebhookEventPayload"}}},
            },
            "responses": {"2XX": {"description": "Acknowledged. Any non-2xx or timeout is retried."}},
        }}
        if "WebhookEventPayload" not in openapi_schema["components"]["schemas"]:
            _payload_schema = _WebhookEventPayload.model_json_schema(
                ref_template="#/components/schemas/{model}")
            for _dname, _ddef in _payload_schema.pop("$defs", {}).items():
                openapi_schema["components"]["schemas"][_dname] = _ddef
            openapi_schema["components"]["schemas"]["WebhookEventPayload"] = _payload_schema

    # 30.6.14 c14f: publish the c14a error body as components.schemas.ErrorResponse
    # (same merge path c6-J3 uses for WebhookEventPayload; get_openapi only emits
    # models a route references, so it has to be injected here).
    if "ErrorResponse" not in openapi_schema["components"]["schemas"]:
        from schemas import ErrorResponse as _ErrorResponse
        _err_schema = _ErrorResponse.model_json_schema(
            ref_template="#/components/schemas/{model}")
        for _dname, _ddef in _err_schema.pop("$defs", {}).items():
            openapi_schema["components"]["schemas"][_dname] = _ddef
        openapi_schema["components"]["schemas"]["ErrorResponse"] = _err_schema

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# --- GSEC R-014: settings-gated docs / openapi ------------------------------
# Docs are OFF by default (`api.public_docs_enabled`, platform setting).
# Anonymous callers get 404 while disabled; authenticated platform admins can
# always open them. FastAPI's documented custom-docs pattern: docs_url=
# None on the app + explicit routes.
def _public_docs_enabled() -> bool:
    from services import platform_settings as _ps
    try:
        return bool(_ps.get_setting("api.public_docs_enabled"))
    except Exception:
        # Fails closed: settings lookup trouble = docs stay hidden from
        # anonymous callers (admins are still allowed below).
        return False


def _require_docs_access(request: Request) -> None:
    """404 anonymous callers while `api.public_docs_enabled` is OFF.

    Admin check is best-effort on the raw bearer token: JWT RS256 signature
    verified against the realm JWKS (no network call per request beyond the
    cached JWKS) plus a users-table lookup for is_platform_admin. No token,
    bad token, or non-admin -> HTTPException(404) (not 401, so disabled docs
    are invisible, not just forbidden)."""
    if _public_docs_enabled():
        return
    # In-container callers (the compose healthcheck and update-system.sh probe
    # `curl http://127.0.0.1:80/docs`) must keep working. uvicorn only trusts
    # X-Forwarded-For from 127.0.0.1, so a proxied request never appears as
    # loopback here.
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        return
    auth = (request.headers.get("authorization") or "").strip()
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=404, detail="Not Found")
    token = auth[7:].strip()
    try:
        from keycloak_auth import get_current_user_claims
        claims = get_current_user_claims(token)
        import crud
        with SessionLocal() as _db:
            user = crud.get_user_by_keycloak_id(_db, keycloak_user_id=claims.get("sub", ""))
        if user is None or not getattr(user, "is_platform_admin", False):
            raise HTTPException(status_code=404, detail="Not Found")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/openapi.json", include_in_schema=False, tags=["Meta"])
def gated_openapi(request: Request):
    _require_docs_access(request)
    return app.openapi()


@app.get("/docs", include_in_schema=False, tags=["Meta"])
def gated_swagger_ui(request: Request):
    _require_docs_access(request)
    from fastapi.openapi.docs import get_swagger_ui_html
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=f"{request.base_url}docs/oauth2-redirect",
        init_oauth=app.swagger_ui_init_oauth,
    )


@app.get("/docs/oauth2-redirect", include_in_schema=False, tags=["Meta"])
def gated_swagger_redirect(request: Request):
    _require_docs_access(request)
    from fastapi.openapi.docs import get_swagger_ui_oauth2_redirect_html
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False, tags=["Meta"])
def gated_redoc(request: Request):
    _require_docs_access(request)
    from fastapi.openapi.docs import get_redoc_html
    return get_redoc_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - ReDoc",
    )


@app.get("/health", status_code=200, include_in_schema=False)
def health_check():
    """A simple endpoint to confirm the API is running.

    EARLY.API-REPORTS-PRODUCT-VERSION (lane r9700, 11-25): the body stays the
    literal {"status": "ok"} — the compose/docker healthcheck and the CE guard
    test_ce_healthcheck_ungated_probe.py pin it. The product version moved to
    the ungated, DB-free GET /v1/version below.
    """
    return {"status": "ok"}


@app.get("/v1/version", status_code=200, include_in_schema=False)
def product_version_route():
    """Public product-version route (EARLY.API-REPORTS-PRODUCT-VERSION).

    Ungated and DB-free like the /health liveness probe: no _require_docs_access,
    no database session, no paid license-module import (that module is a manifest
    deny — it does not exist in the CE extract). Reads the same
    PRODUCT_VERSION env the app version and the Dashboard stamp follow;
    default pinned to CORE/Dashboard/package.json "version" by test.
    """
    return {"product_version": os.getenv("PRODUCT_VERSION", "1.1.0")}


def _is_saas():
    """Check if running in SaaS mode."""
    return os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas"


def _get_stripe_publishable_key():
    """Get Stripe publishable key from Vault."""
    # 1788 CE carve: utils/stripe_api.py is paid. CE has no Stripe at all, so
    # the publishable key is simply empty — the same value the paid path
    # returns when Vault has no key. Never a 500, never a fake key.
    from utils.paid_import import optional_paid
    stripe_api = optional_paid("utils.stripe_api")
    if stripe_api is None:
        return ""
    return stripe_api.get_stripe_publishable_key() or ""


# In-process cache for LLM (managed-ollama / vLLM) health. /platform/features
# is hit by every dashboard page load by every user, so the request path must
# NEVER block on this probe. `llm_available` only drives an admin "wizard
# offline" banner — it is not worth a single millisecond of user-visible
# latency. The actual network probe therefore runs on a background thread; the
# request path only ever reads the last cached value (non-blocking).
#
# 60s TTL: a crash still becomes visible to admins within a minute, while the
# probe (which can cost ~2.5s when the LLM host is a dead DNS name — see below)
# runs at most once a minute regardless of request volume.
_LLM_HEALTH_CACHE = {"available": None, "checked_at": 0.0}
_LLM_HEALTH_TTL_SEC = 60.0
_LLM_HEALTH_REFRESHING = threading.Event()


def _llm_health_url() -> str:
    """Cheap, non-Keycloak-touching resolution of the LLM /health URL.

    The old hot path called ``routers.ai_config._role_base_url('generator')``,
    which did DB/config work and triggered Keycloak JWKS fetches (~2.5s) just
    to learn a hostname. That resolution has no place in a liveness probe.
    Prefer explicit env, else the compose-level ollama host alias.
    """
    return (
        os.getenv("MANAGED_OLLAMA_URL")
        or os.getenv("OLLAMA_INTERNAL_URL")
        or "http://ollama:11434"
    ).rstrip("/")


def _probe_llm_health_blocking() -> bool:
    """Do the actual network probe. Runs on a background thread only."""
    try:
        import httpx
        # Enforce a real, short CONNECT timeout. The previous 1.5s total
        # timeout was not honored on connect/DNS failure — a dead LLM host
        # (getaddrinfo failure) burned ~2.5s. A 0.5s connect budget caps that.
        timeout = httpx.Timeout(1.0, connect=0.5)
        resp = httpx.get(f"{_llm_health_url()}/health", timeout=timeout)
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _refresh_llm_health_async() -> None:
    """Spawn a one-shot background refresh of the LLM-health cache, guarded so
    only one probe runs at a time. Never blocks the caller."""
    if _LLM_HEALTH_REFRESHING.is_set():
        return
    _LLM_HEALTH_REFRESHING.set()

    def _worker():
        try:
            available = _probe_llm_health_blocking()
            _LLM_HEALTH_CACHE["available"] = available
            _LLM_HEALTH_CACHE["checked_at"] = time.time()
        finally:
            _LLM_HEALTH_REFRESHING.clear()

    threading.Thread(target=_worker, name="llm-health-probe", daemon=True).start()


def _is_llm_available() -> bool:
    """Return the cached LLM availability WITHOUT ever blocking the request.

    If the cache is stale, kick off a background refresh and return the last
    known value immediately (defaulting to False until the first probe lands).
    Used by the dashboard to surface an admin "wizard offline" banner so
    platform admins know LLM is the cause when the wizard reports 'unreachable'.
    """
    now = time.time()
    if (now - _LLM_HEALTH_CACHE["checked_at"]) >= _LLM_HEALTH_TTL_SEC:
        _refresh_llm_health_async()
    cached = _LLM_HEALTH_CACHE["available"]
    return bool(cached) if cached is not None else False


@app.get("/v1/release/pubkey")
def release_pubkey_endpoint():
    """1049c — THIS environment's release signing public key, PEM text.
    Unauthenticated: a worker build on this environment fetches it to bake
    in as the verification key (build-side fetch is a later task). 404 when
    Vault has no key yet (sealed/unreachable/not provisioned) — never the
    private key, never an error that leaks Vault internals."""
    from fastapi.responses import PlainTextResponse
    # 1788 CE carve: services/release_key_bootstrap.py is paid. With it absent
    # this environment provisions no release signing key, which is exactly the
    # 404 below — the same response an unprovisioned key already gets.
    from utils.paid_import import optional_paid
    release_key_bootstrap = optional_paid("services.release_key_bootstrap")
    pub = release_key_bootstrap.get_public_key() if release_key_bootstrap else None
    if not pub:
        raise HTTPException(status_code=404, detail="no release signing key provisioned")
    return PlainTextResponse(content=pub, media_type="text/plain")


@app.get("/v1/platform/launch-promo-status")
def launch_promo_status_endpoint():
    """launch-pricing-lockdown §6.6 — read-only counter for the
    pricing-page banner countdown ("27 spots left"). No auth — the
    pricing page is public.
    """
    # 1788 CE carve: utils/launch_promo.py is paid, so CE has no launch-promo
    # counter. The banner must read "no promo" — the identical shape
    # get_launch_promo_status returns once the counter is exhausted (available
    # False, remaining 0). Never a 500.
    from utils.paid_import import optional_paid
    db = SessionLocal()
    try:
        launch_promo = optional_paid("utils.launch_promo")
        if launch_promo is None:
            return {"available": False, "coupon_id": None, "remaining": 0,
                    "redeemed": 0, "max": 0}
        return launch_promo.get_launch_promo_status(db)
    finally:
        db.close()


def _compliance_needs_block(db) -> dict:
    """GT.D11.s1 — the GY.C0 declared-environment "needs" list for the
    tier-aware UI (GET /v1/platform/features → entitlements.compliance).

    The declaration itself is the ordinary ``compliance.declared_environment``
    platform setting (registered unconditionally, so CE can record intent);
    the environment→controls map lives in services/compliance_environments.py,
    which the FX.162.p5 CE carve strips (open-core/manifest.yaml deny). So the
    CE shape carries the declared values + a pointer to the paid admin read
    endpoint, and never invents a needs list. Names only — never a secret.
    Never raises: any failure yields {} so the features endpoint stays 200.
    """
    try:
        from services import platform_settings as _ps
        declared, source = _ps.resolve_source(
            "compliance.declared_environment", db, tenant_id=None)
        declared = [e for e in (declared or []) if isinstance(e, str)]
        from utils.paid_import import optional_paid
        cenv = optional_paid("services.compliance_environments")
        if cenv is None:  # CE carve — no map, so no needs list to render
            return {"declared_environment": declared, "source": source,
                    "edition": "ce", "map_version": 0, "needs": []}
        needs = cenv._needs_for(
            [e for e in declared if e in cenv.ALLOWED_ENVIRONMENTS],
            cenv._is_ce())
        return {
            "declared_environment": [
                e for e in declared if e in cenv.ALLOWED_ENVIRONMENTS],
            "source": source,
            "edition": cenv.edition(),
            "map_version": cenv.MAP_VERSION,
            "needs": needs,
        }
    except Exception:  # noqa: BLE001 — the features endpoint never 500s
        logger.exception("platform/features: compliance needs block failed")
        return {}


def _platform_features_entitlements() -> dict:
    """GT.D11a — additive `entitlements` block for the tier-aware UI.

    Names only: capability/limit names, edition, tier, source. Never a
    license key or secret. Never raises — on failure the endpoint gets a
    minimal stub instead of a 500.

    GT.D11.s1 adds the GY.C0 declared-environment "needs" list under
    ``compliance`` (see _compliance_needs_block) so the WB + Dashboard can
    show the declared install environment + what it needs + the upgrade
    link instead of letting users click into a 402.
    """
    saas = _is_saas()
    try:
        import importlib.util
        from utils import entitlements as ent

        db = SessionLocal()
        try:
            es = ent.get_entitlements(db)
            # GT.D11.s1 — the GY.C0 needs block shares this short session.
            compliance = _compliance_needs_block(db)
        finally:
            db.close()
        # "paid tree present" = the wizard-ml service ships in this build.
        paid_tree = _paid_import.is_paid_tree()
        edition = "saas" if saas else ("pro" if paid_tree else "ce")
        return {
            "edition": edition,
            "tier_name": es.tier_name,
            "source": es.source,
            "capabilities": sorted(es.features),
            "limits": {
                name: ("unlimited" if v >= ent.UNLIMITED else int(v))
                for name, v in es.limits.items()
            },
            # GT.D11.s1 — GY.C0 declared environment + what it needs.
            "compliance": compliance,
        }
    except Exception:
        logger.exception("platform/features: entitlements block failed to build")
        return {"edition": "saas" if saas else "ce", "error": "unresolved"}


def _egress_sealed_mode() -> bool:
    """GY.C8 — the `egress.sealed_mode` master switch for /platform/features.

    Fail-open: a settings-store read error reports NOT sealed (the
    utils.egress_policy contract — a settings outage must not seal a live
    install and hide the paid UI).
    """
    try:
        from utils import egress_policy
        return bool(egress_policy.sealed_mode())
    except Exception:  # noqa: BLE001
        return False


@app.get("/v1/platform/features")
def platform_features():
    """Returns platform feature flags. No auth required — used by UI to toggle features."""
    saas = _is_saas()
    # Hardware-tag advertising is now driven entirely by what active
    # workers report, not a closed admin whitelist. Fall back to the
    # live cluster_capabilities set; the PlatformGPUSettings row is
    # retained for backwards-compat on the dashboard's settings page
    # but no longer filters worker advertisements.
    hardware_tags: list[str] = ["cpu"]
    try:
        from services.cluster_capabilities import (
            get_cluster_capabilities, rank_capability,
        )
        db = SessionLocal()
        try:
            caps = get_cluster_capabilities(db)
            if caps:
                hardware_tags = sorted(caps, key=rank_capability)
        finally:
            db.close()
    except Exception:
        pass
    from services.scheduler import iso_mode_on
    # GS.PSR.8 — the credits kill-switch resolves through the
    # platform-settings registry (credits_enabled: DB > env >
    # True). No session is open here, so db=None (short self-opened).
    try:
        from utils.credit_manager import credits_enabled as _credits_enabled
    except ImportError as _cm_exc:
        from utils.paid_import import _is_absent
        if _is_absent(_cm_exc, "utils.credit_manager"):
            _credits_enabled = False
            logger.warning("credit_manager module absent — credits disabled (CE)")
        else:
            raise
    return {
        "deployment_mode": "saas" if saas else "onprem",
        "iso_mode": iso_mode_on(),
        # GY.C8 — sealed mode (zero outbound calls). The Dashboard skips
        # loading Stripe.js on the paid page when this is True.
        "sealed_mode": _egress_sealed_mode(),
        "credits_enabled": saas and _credits_enabled(),
        "billing_enabled": saas,
        "plan_limits_enabled": saas,
        "storage_quotas_enabled": saas,
        "trial_enabled": saas,
        "seat_limits_enabled": saas,
        "usage_enforcement_enabled": saas,
        "hardware_tags": hardware_tags,
        "gpu_backends": hardware_tags,  # backward compat
        "stripe_publishable_key": _get_stripe_publishable_key() if saas else None,
        "llm_available": _is_llm_available(),
        "entitlements": _platform_features_entitlements(),
    }


# --- Add CORS Middleware ---
# Construct origins from environment variables
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "localhost")
PROTOCOL = os.getenv("PROTOCOL", "http")
DASHBOARD_SUBDOMAIN = os.getenv("DASHBOARD_SUBDOMAIN", "dashboard")
WORKFLOWS_SUBDOMAIN = os.getenv("WORKFLOWS_SUBDOMAIN", "workflows")
API_SUBDOMAIN = os.getenv("API_SUBDOMAIN", "api")
MINIO_SUBDOMAIN = os.getenv("S3_SUBDOMAIN") or os.getenv("MINIO_SUBDOMAIN", "minio")

# CE single-host installs set BASE_DOMAIN to a host that ALREADY carries the
# published port (e.g. `203.0.113.150:3000`).  Gluing that value into the
# subdomain/`:80` forms below produced junk (`http://.203.0.113.150:3000`,
# `http://203.0.113.150:3000:80`) and the real Dashboard origin only matched
# by accident - on another host/port or https every fetch said "Failed to
# fetch".  A port-carrying BASE_DOMAIN has no subdomains: emit the exact
# origin once per scheme, PLUS the WorkflowBuilder origin - the WB is
# published on its OWN host port on the SAME host (CORE/WorkflowBuilder/
# build-and-deploy.sh:19 HOST_PORT="3001"; CORE/Dashboard/inject-config.sh:36
# composes WORKFLOWS_URL="${PROTOCOL}://${BASE_DOMAIN}:3001"), so without it
# every WB browser fetch to the API preflighted to a 400 with no ACAO ->
# fetch() threw -> WB showed "Workers unavailable" + "Workflow Builder Not
# Available".  The WB host is therefore the BASE_DOMAIN host with the port
# swapped, never a subdomain and never KEYCLOAK_HOSTNAME (that var is the
# Keycloak host - a bare DNS label, consumed as such by
# CORE/API/services/app_hostname.py and CORE/Dashboard/inject-config.sh - and
# the CE compose never sets it to a host:port).  The WB port comes from the
# deployer-set WORKFLOW_BUILDER_PORT env (default 3001, the compose's
# published port).  A bare domain (`example.com`) keeps the HEAD list
# byte-for-byte so paid/on-prem deployments are unchanged.  Empty subdomain
# envs (CE defaults) are skipped instead of composing `http://..<host>`.
# Only deployer-set env may add an origin (never anything derived from the
# request); scheme is restricted to http/https; hosts carrying whitespace,
# `/`, `@`, `#` or any non-hostname character, and non-numeric/out-of-range
# ports, are rejected outright - never emitted, never `*`.
def _cors_host_ok(value: str) -> bool:
    """True for a bare hostname/IP (ASCII alnum plus `.-_`) or a bracketed
    IPv6 literal such as `[::1]` / `[fd00::1]` (inner part must parse as v6)."""
    if value.startswith("[") and value.endswith("]"):
        try:
            ipaddress.IPv6Address(value[1:-1])
        except ValueError:
            return False
        return True
    return bool(value) and value.isascii() and all(
        c.isalnum() or c in ".-_" for c in value)


def _cors_port_ok(value: str) -> bool:
    """True for a decimal port in 1..65535."""
    return value.isascii() and value.isdigit() and 1 <= int(value) <= 65535


def _cors_split_base(base: str) -> tuple[str, str]:
    """Split BASE_DOMAIN into (host, port); a `[v6]` literal keeps its colons."""
    if base.startswith("["):
        end = base.find("]")
        if end == -1:
            return base, ""
        host, rest = base[:end + 1], base[end + 1:]
        return host, rest[1:] if rest.startswith(":") else ("" if not rest else rest)
    host, _, port = base.partition(":")
    return host, port


def _cors_https_twin(host: str) -> bool:
    """The https twin on an http install is kept for localhost / IP literals /
    single-label LAN names (CE default: TLS may be terminated in front); a
    public dotted DNS name gets only the configured scheme (CORS-4)."""
    if host.startswith("[") or host == "localhost" or "." not in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _cors_origins() -> list[str]:
    """Allowed CORS origins from BASE_DOMAIN/PROTOCOL (no wildcards)."""
    host, port = _cors_split_base(BASE_DOMAIN)
    if not _cors_host_ok(host):
        return []
    if port or host != BASE_DOMAIN:
        if not port or not _cors_port_ok(port):
            return []
        scheme = "https" if PROTOCOL == "https" else "http"
        schemes = [scheme] + (
            ["https"] if scheme != "https" and _cors_https_twin(host) else [])
        out: list[str] = []
        # The WB has its own published host port ONLY on the single-host
        # install (WORKFLOWS_SUBDOMAIN empty = CE default); on subdomain
        # installs it is already served at `workflows.<BASE_DOMAIN>`, which the
        # bare branch emits.
        wb_port = os.getenv("WORKFLOW_BUILDER_PORT", "3001").strip()
        if WORKFLOWS_SUBDOMAIN or not _cors_port_ok(wb_port):
            wb_port = ""
        for extra in ((port, wb_port) if wb_port else (port,)):
            for s in schemes:
                origin = f"{s}://{host}:{extra}"
                if origin not in out:
                    out.append(origin)
        return out
    if PROTOCOL not in ("http", "https"):
        return []
    out: list[str] = []
    out.append(f"{PROTOCOL}://{BASE_DOMAIN}")
    out.append(f"{PROTOCOL}://{BASE_DOMAIN}:80")
    for sub in (DASHBOARD_SUBDOMAIN, WORKFLOWS_SUBDOMAIN, API_SUBDOMAIN,
                MINIO_SUBDOMAIN):
        if sub and _cors_host_ok(f"{sub}.{BASE_DOMAIN}"):
            out.append(f"{PROTOCOL}://{sub}.{BASE_DOMAIN}")
    kc = os.getenv('KEYCLOAK_HOSTNAME', 'keycloak.' + BASE_DOMAIN)
    kc_host, kc_port = _cors_split_base(kc)
    if _cors_host_ok(kc_host) and (not kc_port or _cors_port_ok(kc_port)):
        out.append(f"{PROTOCOL}://{kc}")
    return out


origins = _cors_origins() + [
    # Legacy localhost support for development
    "http://localhost",
    "http://localhost:80",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://localhost:8000",
]

logger.info("=" * 60)
logger.info("CORS Configuration:")
logger.info(f"  BASE_DOMAIN: {BASE_DOMAIN}")
logger.info(f"  PROTOCOL: {PROTOCOL}")
logger.info(f"  Allowed Origins:")
for origin in origins:
    logger.info(f"    - {origin}")
logger.info("=" * 60)

# CMMC/ISO 27001 Compliance: SC-7 (Boundary Protection)
# Restrict CORS to only methods and headers actually needed by the application
ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = [
    "Authorization",
    "Content-Type",
    "Accept",
    "Origin",
    "X-Requested-With",
    "X-Impersonate-User-Id",  # For service account impersonation
    "X-API-Key",              # For API key authentication
    "X-Request-ID",           # For request tracing
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=ALLOWED_METHODS,
    allow_headers=ALLOWED_HEADERS,
    expose_headers=["X-Error-Code", "X-Request-ID"]
)


# --- License Enforcement Middleware ---
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
# 1645b (CE-boot) — no top-level `from utils.license_client import validate_license`:
# CE denies that module, so it would ImportError at import time. The
# module-level `validate_license` name above (bound near `_license_client`)
# stands in for it; dispatch() below calls that name, same as before.

# Paths that are allowed even when license is expired or missing
# These allow users to log in and update their license key
LICENSE_EXEMPT_PATHS = [
    "/health",  # covers the bare liveness probe AND the unified /v1/health surface (prefix match)
    "/docs",
    "/openapi.json",
    "/redoc",
    "/platform/license",  # Allow license status and update endpoints
    "/auth",              # Allow authentication (login, token refresh, logout)
    "/me",                # Allow user info (needed to check if platform admin)
    "/onboarding",        # Allow onboarding for new users
    "/whats-new/",        # Public unauthenticated release notes (#5 / 17.6)
    "/embed/run",         # embeddable-run-surface — public token-scoped run API
    "/.well-known/security.txt",  # GY.C14 — RFC 9116 disclosure info (public)
]


class LicenseEnforcementMiddleware(BaseHTTPMiddleware):
    """
    Middleware for license state awareness -- pass-through (no block).

    All requests are allowed through via `call_next`.  Paid-feature gating
    is performed at the capability level by `evaluate_feature()` /
    `get_entitlements()` which falls back to the built-in baseline after the
    14-day ``license.grace_days`` window (``utils/license_file.py``).

    The legacy 403-block branches (expired / invalid_key / configuration)
    have been removed: see 1645b2.
    """

    async def dispatch(self, request, call_next):
        # Always allow OPTIONS requests (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Check if path is exempt from license enforcement.
        # Gate 1: all routers now serve under /v1, so normalize a leading
        # /v1 away before matching the (unprefixed) exempt list. The bare
        # /health liveness probe stays unprefixed and still matches directly.
        path = request.url.path
        norm_path = path[3:] if path.startswith("/v1/") else path
        for exempt_path in LICENSE_EXEMPT_PATHS:
            if path.startswith(exempt_path) or norm_path.startswith(exempt_path):
                return await call_next(request)

        # WS1.LIC.1 — a valid signed `license_type=="internal"` license (MOD's
        # own install) is exempt from this middleware's hard cutoff entirely,
        # independent of whatever the LEGACY trial/server key (checked below)
        # is doing. This is a type check, not a date comparison; trial and
        # commercial licenses fall straight through to the existing behavior.
        try:
            _license_verify = optional_paid("utils.license_verify")  # utils/license_verify.py is Paid (manifest deny)
            if _license_verify is None:
                # CE: license_verify module absent → no internal license check,
                # fall through to validate_license() below.
                logger.debug("utils.license_verify not in this edition — internal-license bypass check skipped")
            else:
                signed = _license_verify.current_verified()
                if signed is not None and signed.valid and signed.license_type == _license_verify.LICENSE_TYPE_INTERNAL:
                    return await call_next(request)
        except Exception:  # noqa: BLE001 — never let this check itself block requests
            logger.exception("internal-license middleware bypass check raised; ignoring")

        # Validate the license
        license_result = validate_license()

        # If license is valid (including grace period), allow the request
        if license_result.get("valid", False):
            # Add license warning header if in grace period
            response = await call_next(request)
            if license_result.get("in_grace_period"):
                days_left = license_result.get("days_until_cutoff", 0)
                response.headers["X-License-Warning"] = f"Grace period: {days_left} days until shutdown"
            return response

        # License is invalid - pass through to per-capability entitlement
        # checks. The middleware no longer blocks requests; paid-feature gates
        # live in `evaluate_feature()` / `get_entitlements()` which fall back
        # to the built-in baseline after the grace window.  1645b2.
        return await call_next(request)


# Add the license enforcement middleware
# Note: Middleware is executed in reverse order of addition
# So this runs AFTER CORS but BEFORE route handlers
if _license_client is not None:
    app.add_middleware(LicenseEnforcementMiddleware)
    logger.info("License enforcement middleware enabled")
else:
    logger.info("License enforcement middleware skipped (CE edition: unlimited, no license concept)")


# --- Security Headers Middleware ---
@app.middleware("http")
async def security_headers_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # X-Frame-Options removed: clickjacking defence has moved to CSP
    # `frame-ancestors` (JSON here in the API; the nginx proxy on paid), which
    # supports a cross-subdomain allowlist (X-Frame-Options can only DENY or
    # SAMEORIGIN). The Dashboard's PDF preview iframes the API's
    # /assets/{id}/pdf-view route — DENY blocked that even though
    # frame-ancestors explicitly permitted the dashboard origin.
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if PROTOCOL == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # CSP-1 phase 1: clickjacking defence for the API itself. In CE the API
    # container is reached directly (no nginx in front), so the frame-ancestors
    # policy lives here: JSON responses only (a framed JSON body renders
    # nothing useful), NEVER over a response that already carries a
    # Content-Security-Policy (routers/run_surface_public.py sets its own
    # per-link frame-ancestors allowlist), and never on the /assets/*/pdf-view
    # route, which the Dashboard (js/file-preview.js) and the Workflow
    # Builder (components/file-browser.tsx) frame cross-origin by design —
    # frame-ancestors there would break PDF preview.
    _path = request.url.path
    if (
        "content-security-policy" not in response.headers
        and not _path.endswith("/pdf-view")
        and response.headers.get("content-type", "").startswith("application/json")
    ):
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return response

logger.info("Security headers middleware enabled")


# --- Per-Plan Rate Limiting Middleware ---
RATE_LIMIT_SKIP_PATHS = {"/health", "/docs", "/redoc", "/openapi.json", "/metrics"}
RATE_LIMIT_SKIP_PREFIXES = ("/public/",)

# FX.106 (item 1): the health/metrics surface is exempt, plus every
# webhook RECEIVER (signature/secret-authed, external cadence — limiting
# them here would drop vendor deliveries). The full /webhooks path is a
# management surface, so only the receiver sub-paths are exempt.
FX106_WEBHOOK_RECEIVER_PREFIXES = (
    "/webhooks/bounces",
    "/triggers/webhook/",
    "/triggers/storage-event",
    "/triggers/minio",
    "/triggers/fs-event",
    "/triggers/git/",
    "/triggers/email",
    "/stripe-webhooks/",
    "/marketplace/purchase-webhook",
)


def _fx106_exempt_path(path: str) -> bool:
    # Webhook receivers are mounted at the root (not under /v1), but the
    # app also serves a canonical /v1 mount, so match their prefixes
    # against BOTH the raw and the /v1-normalized path.
    norm = path[3:] if path.startswith("/v1/") else path
    for prefix in FX106_WEBHOOK_RECEIVER_PREFIXES:
        if path.startswith(prefix) or norm.startswith(prefix):
            return True
    if path in RATE_LIMIT_SKIP_PATHS or norm in RATE_LIMIT_SKIP_PATHS:
        return True
    if any(path.startswith(p) or norm.startswith(p)
           for p in RATE_LIMIT_SKIP_PREFIXES):
        return True
    return False


# FX.106 (item 1): standard RateLimit-* response headers set on
# request.state by the dependency-time check (dependencies.py).
FX106_RESPONSE_HEADERS_ATTR = "fx106_response_headers"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply per-plan rate limiting for authenticated requests."""
    path = request.url.path
    # Gate 1: normalize a leading /v1 so skip lists (authored unprefixed)
    # still match under the single canonical mount.
    norm_path = path[3:] if path.startswith("/v1/") else path

    # FX.106 (item 2): a client IP that tripped the auto-block is denied
    # BEFORE dispatch — on EVERY path, exempt or not (a blocked IP must not
    # even reach webhook receivers or /health). This is an auto-BLOCK, not a
    # throttle, so exempting it would defeat the purpose. The per-plan /
    # per-key THROTTLING below is still exempt-able.
    try:
        from services import api_rate_limit_guard as _fx106
        blocked_resp = _fx106.check_ip_block(request)
        if blocked_resp is not None:
            return blocked_resp
    except Exception:
        pass  # fail-open (R-083 contract), observable in the guard

    if (
        path in RATE_LIMIT_SKIP_PATHS
        or norm_path in RATE_LIMIT_SKIP_PATHS
        or any(path.startswith(p) or norm_path.startswith(p) for p in RATE_LIMIT_SKIP_PREFIXES)
    ):
        return await call_next(request)

    response = await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer ") and hasattr(request.state, "_rate_limit_tenant_id"):
        tenant_id = request.state._rate_limit_tenant_id
        # FX.106 p1a: when the guard ran it already RESOLVED the plan limit
        # (api.rate_limit.plan_limits map > plan column > default_rpm) and
        # stamped it on request.state. Reading the plan column again through
        # utils.rate_limiter would show a cap that is no longer enforced
        # after an admin edits the settings map, so the guard's value wins;
        # the column read stays as the fallback for when the guard did not
        # run (disabled, fail-open, or an exempt path).
        plan_limit_state = getattr(request.state, "fx106_plan_limit", None)
        try:
            if plan_limit_state is not None:
                limit = int(plan_limit_state)
            else:
                from utils.rate_limiter import _get_plan_rate_limit
                limit = _get_plan_rate_limit(tenant_id)
            if limit == 0:
                response.headers["X-RateLimit-Limit"] = "unlimited"
            else:
                response.headers["X-RateLimit-Limit"] = str(limit)
        except Exception:
            pass

    # FX.106 (item 1): stamp the standard RateLimit-* headers the
    # dependency-time check computed (plan/key buckets).
    fx106_headers = getattr(request.state, FX106_RESPONSE_HEADERS_ATTR, None)
    if isinstance(fx106_headers, dict):
        for _hk, _hv in fx106_headers.items():
            response.headers[_hk] = _hv

    return response

logger.info("Per-plan rate limiting middleware enabled")


# --- FX.145 p3b: Platform-Admin Ingress Allowlist Middleware ---
# Requests to the admin surface (/v1/admin*, /v1/platform*) are refused
# unless the real client IP sits inside security.admin_ingress_cidrs.
# Empty list = gate OFF (default posture, today's behaviour unchanged).
# Loopback is always allowed; a settings/internal error fails OPEN
# (services/admin_ingress_allowlist.py). Runs outermost (added last) so a
# refusal never reaches a router, i.e. nothing is written.
@app.middleware("http")
async def admin_ingress_allowlist_middleware(request: Request, call_next):
    denied = None
    try:
        _ingress_allowlist = optional_paid("services.admin_ingress_allowlist")  # services/admin_ingress_allowlist.py is Paid (manifest deny)
        if _ingress_allowlist is not None:
            denied = _ingress_allowlist.check_request(request)
    except Exception:  # noqa: BLE001 — the gate must never 500 traffic
        denied = None
    if denied is None:
        return await call_next(request)
    return JSONResponse(
        status_code=403,
        content={
            "detail": {
                "code": denied,
                "message": "Admin access is not allowed from this network.",
            }
        },
    )

logger.info("Platform-admin ingress allowlist middleware enabled")


# --- Prometheus Metrics ---
# GSEC R-014: /metrics is gated by the `api.metrics_require_auth` platform
# setting (default ON). No Vault scrape token exists yet (grep for
# METRICS_TOKEN/metrics_token/scrape found none), so when auth is required,
# access is allowed to: (1) authenticated platform admins (bearer token,
# same best-effort check as the docs gate above), or (2) local / private-
# network callers — the in-cluster Prometheus (mod_prometheus on
# modtex-network, scrape target my_fastapi_app:80 in
# startup/prometheus/prometheus.yml) reaches the API as a docker-bridge
# (RFC1918) source, so its scrape keeps working. GSEC FX.38: the raw left-most
# XFF hop is attacker-supplied and must NEVER be checked — the source IP comes
# from utils/client_ip.py's trusted_client_ip, which honours XFF only when the
# direct TCP peer is inside net.trusted_proxy_cidrs (the docker-bridge default
# covers the in-cluster Prometheus scrape) and takes the right-most untrusted
# hop. If a remote (non-private) scraper is ever added, a scrape token must be
# introduced.
def _metrics_require_auth() -> bool:
    from services import platform_settings as _ps
    try:
        return bool(_ps.get_setting("api.metrics_require_auth"))
    except Exception:
        return True  # fails closed


def _is_private_source_ip(ip: str) -> bool:
    import ipaddress
    try:
        net = ipaddress.ip_address(ip)
    except ValueError:
        return False  # TestClient's "testclient" host etc. → deny
    return bool(net.is_loopback or net.is_private)


def _require_metrics_access(request: Request) -> None:
    """Gate /metrics (GSEC R-014). Fails closed on any lookup trouble."""
    if not _metrics_require_auth():
        return
    # 1) platform-admin bearer (same best-effort check as _require_docs_access)
    auth = (request.headers.get("authorization") or "").strip()
    if auth.startswith("Bearer "):
        try:
            from keycloak_auth import get_current_user_claims
            claims = get_current_user_claims(auth[7:].strip())
            import crud
            with SessionLocal() as _db:
                user = crud.get_user_by_keycloak_id(_db, keycloak_user_id=claims.get("sub", ""))
            if user is not None and getattr(user, "is_platform_admin", False):
                return
        except Exception:
            pass  # bad token / non-admin → fall through to source-IP check
    # 2) local / docker-internal scrape (in-cluster Prometheus).
    # GSEC FX.38 — the raw left-most XFF hop is attacker-supplied, so it must
    # never be checked. services/client_ip.py's resolve_client_ip honours XFF
    # only when the direct TCP peer is inside a trusted-proxy range (net.
    # trusted_proxy_cidrs UNION the FX.106 p3a ranges setting network.trusted_
    # proxy_cidrs — the docker-bridge default covers the in-cluster Prometheus
    # scrape) and takes the right-most untrusted hop, so a spoofed hop from a
    # public peer is ignored.
    # 1788b CE carve: services/client_ip is paid; the KEPT utils/client_ip
    # resolver is the same trust model (peer address, XFF only behind a
    # trusted-proxy range), so the /metrics source-IP gate still fails closed
    # in CE — it never becomes "allow everyone".
    from utils.paid_import import optional_paid
    _client_ip = optional_paid("services.client_ip")
    if _client_ip is not None:
        ip = _client_ip.resolve_client_ip(request) or ""
    else:
        from utils.client_ip import trusted_client_ip
        ip = trusted_client_ip(request) or ""
    if _is_private_source_ip(ip):
        return
    raise HTTPException(status_code=403, detail="Metrics access denied")


try:
    from fastapi import Depends as _MetricsDepends
    from prometheus_fastapi_instrumentator import Instrumentator

    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/health", "/metrics"],
    )
    instrumentator.instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        dependencies=[_MetricsDepends(_require_metrics_access)],
    )
    logger.info("Prometheus metrics enabled at /metrics (gated by api.metrics_require_auth)")
except ImportError:
    logger.warning("prometheus-fastapi-instrumentator not installed — metrics disabled")


# --- Define and Include Routers ---
# Marketplace is Paid (open-core manifest deny) — absent from the CE tree, so it
# must be optional. Kept as a splice (not an append) so it still precedes nodes.router.
try:
    from routers import nodes_marketplace as _nodes_marketplace
    _MARKETPLACE_ROUTERS = [
        (_nodes_marketplace.router, ""),
        (_nodes_marketplace.packages_router, ""),
    ]
except ImportError:
    _MARKETPLACE_ROUTERS = []
# 2400GX-storeB (owner Q-STORE-BROWSE=B, then D71 11-16 "store api routes are Deny!!!"):
# the unified store catalog router + its service closure are DENIED in CE again (open-core
# manifest deny rows: routers/catalog.py + services/catalog_service.py
# + crud/node_marketplace_paid.py + services/publisher_verification.py), so on CE
# _CATALOG_ROUTERS is EMPTY via the try/except ImportError and the CE store surface is
# absent (the UI shows the hidden-nav / Not-available posture). The try/except ImportError
# stays as the safety net only — its PAID routes
# (quote / checkout / stack-packages / publish) call utils.paid_import.require_paid
# first and answer 402, and the denied stack_packages + closure_fulfilment +
# nodes_marketplace modules are loaded via optional_paid, so absence degrades to
# 402 rather than breaking the import.
try:
    from routers import catalog as _catalog
    _CATALOG_ROUTERS = [(_catalog.router, "")]
except ImportError:
    _CATALOG_ROUTERS = []
# /v1/tenants: CE collection CRUD (tenants.py) + Paid sub-resources
# overage-cap/trial/pricing (tenants_paid.py, manifest-denied, absent in CE).
# MT-CE (11-05): tenants.py is manifest-DENIED in the CE cut (open-core/manifest.yaml),
# so the import is guarded — CE mounts NOTHING for /v1/tenants (404, fail closed; the
# tenants_paid mount stays nested so it only exists when the CRUD router shipped).
try:
    from routers import tenants as _tenants
    _TENANTS_ROUTERS = [(_tenants.router, "")]
    try:
        from routers import tenants_paid as _tenants_paid
        _TENANTS_ROUTERS.append((_tenants_paid.router, ""))
    except ImportError:
        pass
except ImportError as _tenants_exc:
    # FIX-MT-CE-MAINPY (11-05): swallow ONLY the absent module itself — same
    # predicate the CE-absent guards already use (utils.paid_import._is_absent,
    # as at main.py:1576 for utils.credit_manager). An ImportError raised INSIDE
    # tenants.py (a real bug, or a missing third-party dep) has a different
    # exc.name and re-raises, so CE boot never hides it.
    from utils.paid_import import _is_absent
    if not _is_absent(_tenants_exc, "routers.tenants"):
        raise
    _TENANTS_ROUTERS = []
    logger.info("tenants router not in this build")

# MT-CE-4B (11-06): CE read-only single-tenant GET /v1/tenants (owner Q-MT1=B).
# tenants.py is manifest-DENIED in the CE cut, so the Dashboard tenant picker
# (~9 fetchApi('/tenants') call sites) would 404; routers/tenants_ce.py ships
# in the CE cut (manifest api_allow) and serves ONLY the caller's own tenant,
# read-only. Mount it ONLY when the paid tenants router is ABSENT — never both
# (two GET /tenants routes would shadow one another silently).
if not _TENANTS_ROUTERS:
    try:
        from routers import tenants_ce as _tenants_ce
        _TENANTS_ROUTERS = [(_tenants_ce.router, "")]
    except ImportError as _tenants_ce_exc:
        from utils.paid_import import _is_absent
        if not _is_absent(_tenants_ce_exc, "routers.tenants_ce"):
            raise
        logger.info("tenants_ce router not in this build")

# 10-11 lead mounts — WSIP / EXPR routers (each declares its own prefix).
from routers import (
    workflow_causal as _r_workflow_causal,
    workflow_resume_event as _r_workflow_resume_event,
    workflow_interface_run as _r_workflow_interface_run,
    workflow_extraction_suggestions as _r_workflow_extraction_suggestions,
    workflow_parameter_versions as _r_workflow_parameter_versions,
    workflow_mcp_exposure_router as _r_workflow_mcp_exposure_router,
    wsip_flags as _r_wsip_flags,
)

# CE-guarded WSIP/EXPR routers — may be absent in CE build.
try:
    from routers import autonomous_proposals as _r_autonomous_proposals
except (ImportError, ModuleNotFoundError):
    _r_autonomous_proposals = None
    logger.warning("autonomous_proposals router not available")

try:
    from routers import admin_adversary_findings as _r_admin_adversary_findings
except (ImportError, ModuleNotFoundError):
    _r_admin_adversary_findings = None
    logger.warning("admin_adversary_findings router not available")

try:
    from routers import failure_prediction_calibration as _r_failure_prediction_calibration
except (ImportError, ModuleNotFoundError):
    _r_failure_prediction_calibration = None
    logger.warning("failure_prediction_calibration router not available")

try:
    from routers import drift_events as _r_drift_events
except (ImportError, ModuleNotFoundError):
    _r_drift_events = None
    logger.warning("drift_events router not available")

try:
    from routers import goal_planner as _r_goal_planner
except (ImportError, ModuleNotFoundError):
    _r_goal_planner = None
    logger.warning("goal_planner router not available")

try:
    from routers import fewshot_plan as _r_fewshot_plan
except (ImportError, ModuleNotFoundError):
    _r_fewshot_plan = None
    logger.warning("fewshot_plan router not available")

try:
    from routers import training_steps as _r_training_steps
except (ImportError, ModuleNotFoundError):
    _r_training_steps = None
    logger.warning("training_steps router not available")

try:
    from routers import public_workflow_docs as _r_public_workflow_docs
except (ImportError, ModuleNotFoundError):
    _r_public_workflow_docs = None
    logger.warning("public_workflow_docs router not available")

try:
    from routers import mpc_sessions as _r_mpc_sessions
except (ImportError, ModuleNotFoundError):
    _r_mpc_sessions = None
    logger.warning("mpc_sessions router not available")

router_mapping = [
    (auth.router, "/auth"),
    (onboarding.router, "/onboarding"),
    (embedding_models.router, ""),
    (me.router, "/me"),
    (api_keys.router, "/api-keys"),
    (api_keys.public_router, "/api-keys"),  # Public token exchange endpoint
    *([(_r_service_accounts.router, "")] if _r_service_accounts is not None else []),  # FX.114 — /v1/service-accounts
    # Public: a pairing install has no credential until the exchange succeeds.
    (users.router, "/users"),
    *_TENANTS_ROUTERS,  # clean-v2-api identity wave: /v1/tenants (moved out of users.py)
    (invitations.router, ""),  # clean-v2-api: prefix now /users/invitations on the router
    (invitations.public_router, ""),
    (invitations.private_router, ""),
    (teams.router, "/teams"),
    (rbac.router, "/rbac"),
    (capabilities.router, ""),  # Granular capabilities and resource permissions
    (permissions.router, "/permissions"),
    (bulk_permissions.router, ""),
    (permission_templates.router, ""),
    (permission_analytics.router, ""),
    (permission_requests.router, ""),
    (audit_logs.router, ""),
    (secrets.router, "/secrets"),
    (secrets.service_router, "/secrets"),  # Service router for API key access
    (webhooks.router, "/webhooks"),
    (tags.router, "/tags"),
    (locks.router, "/locks"),
    (harbor.router, ""),  # Harbor credentials status check
    (node_definitions.router, "/node-definitions"),
    (node_code_edit.router, "/node-definitions"),  # node-code-edit-rebuild Contract 2
    # clean-v2-api nodes wave: node-definitions → clean /v1/nodes family.
    # Sub-resource routers MUST precede nodes.router (static /nodes/marketplace &
    # /nodes/packages would otherwise be shadowed by dynamic /nodes/{id}).
    # OZ-F7 (11-15): the builds family mount moved IN PLACE to /v1/woodsman
    # (router prefix; module + identifiers stay nodes_builds) — a disjoint
    # top-level segment, so it no longer needs to precede nodes.router.
    (nodes_builds.router, ""),
    *_MARKETPLACE_ROUTERS,
    *([(listing_visibility.router, "")] if listing_visibility is not None
      else []),  # GG.6c private listings publisher endpoints (store deny PART 2; CE-guarded)
    *([(entitlements_admin.router, "")] if entitlements_admin is not None
      else []),  # GG.6b platform-admin grant/revoke entitlements (paid; CE-guarded)
    *([(listing_codes.router, "")] if listing_codes is not None
      else []),  # GG.6b redeemable listing codes (store deny PART 2; CE-guarded)
    *([(library.router, "")] if library is not None
      else []),  # GG.UI.0a store-screens: GET /v1/library + POST /v1/codes/redeem (store deny PART 2; CE-guarded)
    # GG.UI.7d: /catalog/mine/codes* MUST precede catalog.router below so its
    # 2-segment literal path wins over catalog.router's dynamic
    # /catalog/{kind}/{id} (same reasoning as catalog.get_my_catalog's own
    # docstring re: /catalog/mine).
    *([(seller_listing_codes.router, "")] if seller_listing_codes is not None
      else []),  # GG.UI.7d seller-scoped listing invite codes (PAID; CE-guarded)
    *_CATALOG_ROUTERS,  # GG.2 unified store catalog (GET /v1/catalog) — Paid, optional in CE
    *([(workflow_listings.router, "")] if workflow_listings is not None
      else []),  # GG.6 workflow publish + delivery (store deny PART 2; CE-guarded)
    (nodes_remediation.router, ""),
    (nodes.router, ""),
    (node_definitions.external_router, ""),  # External node-authoring API (/v1/node-authoring/*)
    *([(_r_autonomous_proposals.router, "")] if _r_autonomous_proposals is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    *([(_r_admin_adversary_findings.router, "")] if _r_admin_adversary_findings is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    *([(_r_failure_prediction_calibration.router, "")] if _r_failure_prediction_calibration is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    *([(_r_drift_events.router, "")] if _r_drift_events is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    (_r_workflow_causal.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    (_r_workflow_resume_event.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    (_r_workflow_interface_run.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    *([(_r_goal_planner.router, "")] if _r_goal_planner is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    (_r_workflow_extraction_suggestions.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    (_r_workflow_parameter_versions.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    *([(_r_fewshot_plan.router, "")] if _r_fewshot_plan is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    *([(_r_training_steps.router, "")] if _r_training_steps is not None else []),  # EXPR 10.3 — POST /v1/road/{id}/training-steps
    *([(_r_public_workflow_docs.router, "")] if _r_public_workflow_docs is not None else []),  # 10-11 WSIP/EXPR/Fortress mount
    (_r_workflow_mcp_exposure_router.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    (_r_wsip_flags.router, ""),  # 10-11 WSIP/EXPR/Fortress mount (static /workflows/* before workflows.router)
    *([(_r_mpc_sessions.router, "")] if _r_mpc_sessions is not None else []),  # 10-11 Fortress 6.6-6.8 mount
    (workflows.router, "/road"),
    (workflows.service_router, "/road"),  # Service router for token exchange
    (workflows.workflow_runner_router, ""),  # MCP fleet: workflow-run tools (/v1/workflow-runner/*)
    (workflow_callbacks.router, ""),  # Workflow completion callbacks (prefix in router)
    (workflow_triggers.router, ""),  # Workflow triggers (prefix in router)
    (workflow_triggers.nested_router, ""),  # Rotate-secret + future nested trigger routes
    (workflow_triggers.automations_router, ""),  # MCP fleet: automations (/v1/automations/*)
    (trigger_fire.router, ""),  # add-workflow-triggers §5 — public fire endpoints (webhook, …)
    (files.router, "/files"),
    (workflow_run_files.router, "/road/runs"),
    (runs.router, "/runs"),  # clean-v2-api: /v1/runs
    (runs.service_router, "/runs"),
    (notifications.router, "/notifications"),
    (stream_tickets.router, "/stream-tickets"),  # WS5.34b
    (wizard_queue.router, "/wizard-queue"),
    (interventions.router, ""),  # Gate 5 — human approval gates (prefix in router)
    (intervention_callbacks.router, ""),  # Slack block_actions for intervention approve/reject
    (smtp_settings.router, "/smtp-settings"),
    (private.router, "/private"),
    (private_permissions.router, ""),
    (web_search_result.router, ""),  # add-web-search-node — container result-return (internal, service-account auth; POST /admin/web-search/result)
    (storage_connections.router, "/storage-connections"),  # Gate 5 — per-tenant BYO-S3 connections
    (hf_tokens.router, ""),  # GF.C0b.2 — per-tenant HF token (prefix in router: /hf-tokens/*)
    *([(platform_license.router, "/platform")] if platform_license is not None else []),  # Platform license management endpoints (CE: denied via optional_paid)
    *([(_r_hf_cache_relayout.router, "")] if _r_hf_cache_relayout is not None else []),  # GSEC R-159 — automated HF cache re-layout
    *([(_r_node_lockdown_report.router, "")] if _r_node_lockdown_report is not None else []),  # GSEC FX.115 p2 — node lockdown breakage report
    *([(_r_node_lockdown_exceptions.router, "")] if _r_node_lockdown_exceptions is not None else []),  # GSEC FX.115 p3 — lockdown exceptions
    (worker_management.router, ""),  # Worker registration and licensing (no auth required)
    (worker_tasks.router, ""),  # Remote worker task polling (enrolled worker auth)
    *([(_r_worker_host_exec.router, "")] if _r_worker_host_exec is not None else []),  # hx.5 host-exec API (/v1/workers/{worker_id}/exec)
    *([(_r_worker_host_exec_access.router, "")] if _r_worker_host_exec_access is not None else []),  # hx.15b host-exec access policy/grants
    *([(_r_worker_host_exec_output.router, "")] if _r_worker_host_exec_output is not None else []),  # hx.6 host-exec output ingest + live tail
    *([(_r_host_exec_targets.router, "")] if _r_host_exec_targets is not None else []),  # hx.14 exec-able worker listing for agents
    (node_credentials.router, ""),  # worker-mediated-node-credentials §3 — worker-mint run_task token (enrolled worker auth)
    (registry_auth.router, ""),  # Local container registry token auth (no prefix — /registry/auth)
    (integrations.router, ""),  # Social login + SMTP config (platform admin only)
    (resource_perms.router, ""),  # Unified resource permissions + role presets
    (platform_health.router, ""),  # Unified /v1/health (status + ?verbose detailed + ?extended deep) — Gate 1 health merge
    (platform_feature_config.router, ""),  # δ Phase 0 — wizard feature gating
    (platform_settings.router, ""),  # GS.PSR.3 — DB-backed platform settings (admin)
    *([(_r_platform_backups.router, "")] if _r_platform_backups is not None else []),  # FX.84 part 2a — on-prem Backups (admin)
    *([(_r_staff_register.router, "")] if _r_staff_register is not None else []),  # GSEC FX.153 part 2 — staff register (admin)
    *([(_r_ai_transparency.router, "")] if _r_ai_transparency is not None else []),  # GY.V10 — AI transparency model inventory (admin)
    *([(_r_subprocessor_changes.router, "")] if _r_subprocessor_changes is not None else []),  # GSEC FX.85 p1a — subprocessor change log
    *([(_r_support_bundle.router, "")] if _r_support_bundle is not None else []),  # FX.104 — encrypted support bundle export (admin)
    *([(_r_dsar_tracker.router, "")] if _r_dsar_tracker is not None else []),  # FX.42 — DSAR request tracker (admin)
    (changelog.router, ""),  # Release notes / changelog
    (admin_email_config.router, ""),  # launch-day-followup-5 §1A — platform email config
    *([(_r_wizard_knowledge.router, "")] if _r_wizard_knowledge is not None else []),  # wizard-knowledge-pack Phase 2 (986e) — /wizard/update-knowledge
    (workflow_templates.router, ""),  # P-wizard B — Workflow Templates gallery + clone
    (variant_patch_dispatch.router, ""),  # P5 — self-healing variant patch loop
    (variant_calibration.router, ""),     # P9 — INT8 calibration UX
    (admin_port_analytics.router, ""),    # P10 — telemetry → recipe co-evolution
    (discovery_admin.router, ""),  # human-assisted-discovery — operator surface for learned crossovers (platform-admin)
    (port_coverage.router, ""),  # P9g.3 — Port Coverage read-only endpoints
    (platform_wizard_stats.router, ""),  # wizard-rebuild §0d — wizard pipeline stats for dashboard
    (ai_config.router, ""),  # AI Advanced: feature->stack binding surface — CE (AA-CE)
    *([(_r_credit_rate.router, "")] if _r_credit_rate is not None else []),  # 30.6.10 — /v1/credit-rate (PAID module: CE strips it, route 404s)
]

# ---------------------------------------------------------------------------
# FX.162.p1 (owner Q157: MOD CE must not do anything with compliance) — the
# ONE list of paid-only compliance routers, mounted through ONE predicate
# (utils.paid_import.is_paid_tree — the same sentinel main.py's /v1/platform/
# features edition line uses: services/edition_sentinel present; CUT-A2 moved
# it off the wizard_ml feature package). Under CE these
# are NOT mounted, so their paths 404 naturally (no per-router 402).
# Inventory = the audit's (fx162-ce-no-compliance-audit.md §2): the 12
# unconditional router_mapping entries + the 3 platform_security_* try-mounts
# (alert engine / incident records / FX.158 advisories). Everything CE still
# needs (base audit log, ISO no-hard-delete, password policy, outbound
# webhooks, backup ledger, staff register, DSAR tracker, subprocessor log,
# support bundle, AI transparency inventory) stays above in router_mapping.
# ---------------------------------------------------------------------------
_PAID_COMPLIANCE_ROUTERS = []
try:
    def _fx162_paid_router(_fx162_name):
        # FIND-11-PART6: the flat block below aborted on the
        # FIRST optional_paid whose import raised for a TRANSITIVE paid module
        # (routers/compliance_conformance.py imports services.conformance, which
        # the paid LAYER image never COPYs), so the outer except caught it and
        # ALL 12 paid compliance routers stayed unmounted even with is_paid_tree()
        # True and the router .so importable -> route_paid=404 next to 12/12 +
        # 8/8 + green sentinels (PART 4 F-2 / PART 5). Per-router guard: a module
        # that cannot import disables ONLY that surface. CE unchanged: the carve
        # strips these files, optional_paid returns None (_is_absent matches the
        # module itself), nothing mounts, route_ce stays 404.
        try:
            return optional_paid(_fx162_name)
        except ImportError as _fx162_inner:
            logger.warning("FX.162: paid compliance router %s unavailable (%s) — "
                           "that surface disabled" % (_fx162_name, _fx162_inner))
            return None

    for _fx162_name in (
        "routers.compliance_conformance",
        "routers.compliance_feature_reference",
        "routers.compliance_reports",
        "routers.compliance_egress_status",
        "routers.compliance_watermark_decode",
        "routers.legal_hold",
        "routers.coppa_consent",
        "routers.evidence_bundle",
        "routers.compliance_auditors",
        "routers.incident_export",
        "routers.compliance_profiles",
        "routers.compliance_service_accounts",
    ):
        _fx162_m = _fx162_paid_router(_fx162_name)
        if _fx162_m is not None:
            _PAID_COMPLIANCE_ROUTERS.append((_fx162_m.router, ""))
    # 2400LR-sw1 T3 / BUG.B53a fix: these 12 optional_paid-bound compliance
    # routers (incl. routers/coppa_consent.py — /v1/platform/consent/*) were
    # bound but never mounted, so every paid-only compliance surface 404ed in
    # the PAID tree too (test_fx162_p4 pins the list at 18 entries; test_fx162_p1
    # demands all 15 paths mounted in pro). Mount iff present: optional_paid
    # returns None on the CE carve, so CE stays clean and paid gets 12 + 6.
except ImportError as _fx162_exc:
    # CE carve (Q157 manifest deny) stripped the compliance router files.
    logger.warning("FX.162: compliance router modules unavailable (%s) — "
                   "compliance surfaces not in this edition" % _fx162_exc)
try:
    from routers import compliance_attestations
    _PAID_COMPLIANCE_ROUTERS.append((compliance_attestations.router, ""))  # fortress 4.2b — attestations (auditor/admin)
except ImportError:
    logger.warning("fortress-4.2b: compliance_attestations router unavailable")
try:
    from routers import compliance_output_scanners
    _PAID_COMPLIANCE_ROUTERS.append((compliance_output_scanners.router, ""))  # fortress 3.7 — output-scanner config (tenant/workflow)
except ImportError:
    logger.warning("fortress-3.7: compliance_output_scanners router unavailable")
try:
    from routers import workflow_certifications
    _PAID_COMPLIANCE_ROUTERS.append((workflow_certifications.router, ""))  # fortress 5.9 S3 — workflow certifications (platform/third-party issue, revoke)
except ImportError:
    logger.warning("fortress-5.9: workflow_certifications router unavailable")
try:
    from routers import compliance_submission_packages
    _PAID_COMPLIANCE_ROUTERS.append((compliance_submission_packages.router, ""))  # fortress 5.5 S5 — submission packages (auditor/admin)
except ImportError:
    logger.warning("fortress-5.5: compliance_submission_packages router unavailable")
try:
    from routers import compliance_policies
    _PAID_COMPLIANCE_ROUTERS.append((compliance_policies.router, ""))  # fortress 5.4 S1 — tenant policy API (upload/list/subscribe)
    _PAID_COMPLIANCE_ROUTERS.append((compliance_policies.violations_router, ""))  # fortress 5.4 S1b — uniform violation surface (/compliance/violations)
except ImportError:
    logger.warning("fortress-5.4: compliance_policies router unavailable")


def _mount_if_paid(entries):
    """Mount paid-only routers iff this image carries the paid tree."""
    if not _paid_import.is_paid_tree():
        logger.info("compliance routers not mounted (not in this edition)")
        return
    for _r, _pfx in entries:
        router_mapping.append((_r, _pfx))


_mount_if_paid(_PAID_COMPLIANCE_ROUTERS)

# ---------------------------------------------------------------------------
# Cloud/Paid routers — OPTIONAL. Absent in the open-source MOD Core build
# (denylisted in open-core/manifest.yaml), present in the SaaS build. Guarded
# so a missing module disables its routes instead of crashing startup. Keep
# this set in sync with the manifest's denied routers (open-source-mod-core §4.1).
#
# 2400UI14 — CE silent-router-drop fix. The shape here used to be ONE
# all-or-nothing `from routers import (a, b, c, ...)` whose `except ImportError:`
# was a bare `pass`. In the CE carve the statement raises on the FIRST denied
# name, so every CE-eligible module later in the same tuple (system_drain,
# assets, ml, calibration, admin_calibration, marketplace_remote, gpu_settings,
# tenant_permission_settings, tenant_slack, marketplace_webhook, burst_accounts)
# was silently dropped — /v1/assets and /v1/system/drain-status 404ed even though
# the files ship in the CE image (ce-api-404-rootcause-2400.md). Mount each
# module independently instead: a missing module disables only ITS routes and
# logs, like the per-name guards above at main.py:128-220. On the dev/SaaS build
# every module imports, so the mounted set + order is byte-identical to before.
# ---------------------------------------------------------------------------
_CE_GUARDED_ROUTER_MODULES = {}  # module name -> bound module (for reference/debug)


def _mount_optional_routers(entries):
    """Mount (module_name, attr, prefix) entries one module at a time.

    A module absent in this edition (CE carve) disables only its own routes and
    logs a warning; the rest of the list still mounts. Entries are mounted in
    list order, so the dev/SaaS route set is unchanged.
    """
    for _mod_name, _attr, _pfx in entries:
        _mod = _paid_import.optional_paid("routers." + _mod_name)
        if _mod is None:
            logger.warning("%s router not available (not in this edition)", _mod_name)
            continue
        _CE_GUARDED_ROUTER_MODULES[_mod_name] = _mod
        router_mapping.append((getattr(_mod, _attr), _pfx))


_CLOUD_PAID_ROUTER_MOUNTS = [
        ("subscriptions", "router", "/subscriptions"),
        ("accounting", "router", ""),
        ("stripe_webhooks", "router", "/stripe-webhooks"),
        ("marketplace_webhook", "router", "/marketplace"),  # MKT.C2 — Marketplace purchase-delivery webhook (signature-auth)
        ("marketplace_remote", "router", "/marketplace"),  # GG.RS.6 — on-prem Dashboard proxy for the Marketplace PUBLIC catalog (MKT.C1)
        ("overage_cap", "router", ""),
        ("admin_plans", "router", ""),
        ("pricing", "router", ""),  # clean-v2-api: the clean /v1/pricing resource (config/overrides), replacing the /admin/plans/pricing/config/* fragments admin_plans.router used to serve
        ("admin_gpu_pool", "router", ""),
        ("system_drain", "router", ""),  # GT.C2 — /v1/system/drain-status|drain|resume
        ("admin_resource_monitor", "router", ""),
        ("admin_cancellations", "router", ""),
        ("platform_analytics", "router", ""),
        ("tenant", "router", "/tenant"),
        ("tenant_permission_settings", "router", ""),
        ("tenant_permission_settings", "private_router", ""),
        ("tenant_slack", "router", ""),
        ("burst_accounts", "router", "/burst"),  # GH.2: /burst/accounts/* (tenant-admin)
        ("assets", "router", "/assets"),  # clean-v2-api: /v1/assets
        ("assets", "service_router", "/assets/service"),
        ("assets", "public_router", "/assets"),
        ("ml", "router", ""),
        ("nim", "router", ""),
        ("gpu_settings", "router", ""),  # Gate 1: folded into /platform/accelerators/* (gpu + hardware aliases retired)
        ("calibration", "router", ""),
        ("admin_calibration", "router", ""),
        ("support_chat", "router", ""),
        ("support_chat", "workflow_authoring_router", ""),  # MCP fleet: workflow-wizard mechanical core (/v1/workflow-authoring/*)
        ("support_chat", "project_authoring_router", ""),  # MCP fleet: project-wizard mechanical core (/v1/project-authoring/*)
    ]
_mount_optional_routers(_CLOUD_PAID_ROUTER_MOUNTS)

# ---------------------------------------------------------------------------
# More Cloud/Paid routers — OPTIONAL (Gate 4b CE-boot). These were previously
# imported + mounted unconditionally, but their modules are denylisted in the
# CE build (mobile builder, external-collaborator invitations, hosted assistant/
# support agent, embeddable run surface, worker provisioning, external-executor
# farms, capture field client/preflight, conversational project agent, admin/
# platform-ops surfaces). Guarded so a missing module disables its routes
# instead of crashing startup; a no-op in the full build where all are present.
# ---------------------------------------------------------------------------
# 2400UI14 — same per-module guard as the block above (see that comment): one
# all-or-nothing tuple here dropped every module after the first CE-denied name.
_GATE4B_ROUTER_MOUNTS = [
        ("mobile", "router", ""),
        ("mobile_signing", "router", ""),  # BUG.B40b — /mobile/signing/* platform-admin inputs
        ("external_invitations", "router", "/external-invitations"),
        ("external_invitations", "public_router", ""),  # Public accept flow (token-authorized)
        # FX.110 (owner decision Q97=A) — outside collaborators: terms,
        # review listing, remove-all, gov/DoD approval (admin + public
        # accept surfaces).
        ("external_collab", "router", "/external-collab"),
        ("external_collab", "public_router", ""),  # /public/external-collab/terms/accept
        ("project_conversation", "router", ""),  # conversational-project-builder §2
        ("capture_field_client", "router", ""),  # Gate 15 — field client (prefix in router)
        ("field_ingest", "router", ""),  # 19.3.s1 — first-slice push
        ("admin_harbor_followup", "router", ""),  # Stuck-Trivy-scan recovery (internal, service-account auth)
        ("ai_assistant", "router", ""),  # AI Assistant endpoints
        ("ai_assistant", "service_router", ""),  # AI Assistant service endpoints (for DAGs)
        ("worker_provisioning", "router", ""),  # Worker provisioning via Ansible (auth required)
        ("worker_install", "router", ""),  # generic install/download routes split out of worker_provisioning; ships in CE (MUST stay right after worker_provisioning: same /workers route order as before the split)
        ("system_external", "router", ""),  # external-executor-nodes — System → External load
        ("admin_storage", "router", ""),  # launch-day-followup-6 §1 — read-only storage status
        ("postmark_webhooks", "router", ""),  # launch-day-followup-5 §1A — Postmark bounce webhook
        ("assistant", "router", ""),  # mod-assistant-agent §2 — chat-agent endpoints
        ("assistant", "platform_router", ""),  # GE.MAA.2 (§9.6/9.7) — /platform/assistant/config
        ("run_surface", "router", ""),  # embeddable-run-surface — owner link management
        ("run_surface_public", "router", ""),  # embeddable-run-surface — public token run API
        ("project_handoff", "router", ""),  # project-agent groups 6-10 — human handoff (prefix in router)
        ("capture_preflight", "router", ""),  # capture-preflight §3.2 — server report (prefix in router)
        ("platform_signup_orphans", "router", ""),  # signup tracking — partial-signup orphans + resend
    ]
_mount_optional_routers(_GATE4B_ROUTER_MOUNTS)

# FX.140 part 1 (lead review defect 3) — PUBLIC one-click unsubscribe
# endpoint (RFC 8058): POST/GET /v1/unsubscribe?u=&c=&t=. Token-authorized
# (HMAC over the mod-unsubscribe-v1 domain-prefixed payload), no Keycloak —
# same shape as the other token-authorized public routes.
try:
    from routers import email_unsubscribe as _email_unsubscribe_router
    router_mapping.append((_email_unsubscribe_router.router, ""))
except ImportError:
    logger.warning("email_unsubscribe router not available — one-click unsubscribe disabled")

# FX.140 part 2 (Q128=A) — email-preferences API for the Dashboard page:
# GET/PUT /v1/me/email-preferences over the append-only consent record.
try:
    from routers import email_preferences as _email_preferences_router
    router_mapping.append((_email_preferences_router.router, ""))
except ImportError:
    logger.warning("email_preferences router not available — email preferences disabled")

# FX.133 p3 (owner Q121=A) — Global Privacy Control state for the subject:
# GET /v1/me/gpc reads the recorded Sec-GPC opt-out off the DSR ledger so the
# consent/tracking decisions (and the Dashboard consent page) can show what
# they are acting on. Read-only — the signal is never client-submitted.
try:
    from routers import gpc as _gpc_router
    router_mapping.append((_gpc_router.router, ""))
except ImportError:
    logger.warning("gpc router not available — GPC state page disabled")

# add-self-service-sso §3 — IdP CRUD router. Loaded conditionally so
# a fresh install without the SSO migrations applied doesn't crash on
# import. The router's own dependency-layer plan gate returns 402 for
# tenants without SSO enabled, so mounting it is safe even pre-pilot.
# add-self-service-sso §3 — IdP CRUD router.
try:
    from routers import sso_idp as _sso_idp
except ImportError:
    pass  # sso package not present in this build
else:
    router_mapping.append((_sso_idp.router, ""))

# add-self-service-sso §4 — Domain verification router.
try:
    from routers import sso_domains as _sso_domains
except ImportError:
    pass
else:
    router_mapping.append((_sso_domains.router, ""))

# add-self-service-sso §5.2 — public home-realm-discovery resolver.
# This endpoint is UNAUTHENTICATED — the login page hits it before
# the user has any session. Mounted into the same router_mapping so
# the same root + /v1 versioning wraps it.
try:
    from routers import sso_resolve as _sso_resolve
except ImportError:
    pass
else:
    router_mapping.append((_sso_resolve.router, ""))
try:
    from routers import sso_banner as _sso_banner
except ImportError:
    pass
else:
    router_mapping.append((_sso_banner.router, ""))
try:
    from routers import sso_rum as _sso_rum
except ImportError:
    pass
else:
    router_mapping.append((_sso_rum.router, ""))
try:
    from routers import sso_link_retry as _sso_link_retry
except ImportError:
    pass
else:
    router_mapping.append((_sso_link_retry.router, ""))
try:
    from routers import sso_profile as _sso_profile
except ImportError:
    pass
else:
    router_mapping.append((_sso_profile.router, ""))
try:
    from routers import scim_v2 as _scim_v2
except ImportError:
    pass
else:
    router_mapping.append((_scim_v2.router, ""))
try:
    from routers import admin_scim_tokens as _admin_scim_tokens
except ImportError:
    pass
else:
    router_mapping.append((_admin_scim_tokens.router, ""))
try:
    from routers import admin_scim_mapping as _admin_scim_mapping
except ImportError:
    pass
else:
    router_mapping.append((_admin_scim_mapping.router, ""))
try:
    from routers import admin_scim_activity as _admin_scim_activity
except ImportError:
    pass
else:
    router_mapping.append((_admin_scim_activity.router, ""))

# Per-device accelerator profiles + strategy admin
try:
    from routers import accelerators
    router_mapping.append((accelerators.router, ""))
except ImportError:
    pass

# Training data generation
try:
    from routers import training
    router_mapping.append((training.router, ""))
except ImportError:
    pass

# scheduler-runtime-config-panel — platform-admin sliders for queue
# packer aggressiveness + calibration capacity floor.
try:
    from routers import scheduler_runtime_config
    router_mapping.append((scheduler_runtime_config.router, ""))
except ImportError:
    pass

# GSEC FX.59 p2b/p2c — central tenant-lifecycle gate. Imported here (before
# the direct `app.include_router` calls below) so both the tenant-scoped
# direct includes AND the single canonical loop mount can attach
# `dependencies=[_Depends(_tenant_lifecycle_gate)]`.
from fastapi import Depends as _Depends
from utils.tenant_lifecycle import tenant_lifecycle_gate as _tenant_lifecycle_gate

# Scheduler admin (separate to avoid circular imports)
try:
    from routers import scheduler_admin
    # OZ-F5 (owner 11-14, Clockwork Scheduler): mount prefix renamed in place
    # /v1 -> /v1/clockwork. No alias mount, no redirect: the old
    # /v1/system/scheduler/* paths now 404. The `scheduler_admin` module and
    # identifier stay; callers move in OZ-F6.
    app.include_router(scheduler_admin.router, prefix="/v1/clockwork")
except ImportError:
    pass  # scheduler_admin not available

# GSEC FX.119 p3a (owner decision Q106=A) — the on-call ACK endpoint.
# POST /v1/oncall/pages/{id}/ack?token=... — the capability link the on-call
# person's phone taps (attached to the page by services/paging_service.py).
# No KC session behind it: the single-use ack token IS the credential, so the
# router mounts bare (no tenant-lifecycle gate — a page is platform-level).
try:
    from routers import oncall_ack as _oncall_ack
    app.include_router(_oncall_ack.router, prefix="/v1")
except ImportError:
    logger.warning("oncall_ack router not available — route disabled")

# GY.C4 — break-glass follow-up review.
# POST /v1/admin/break-glass/{audit_id}/review (platform admin only).
_admin_break_glass = optional_paid("routers.admin_break_glass")  # routers/admin_break_glass.py is Paid (manifest deny)
if _admin_break_glass is not None:
    app.include_router(_admin_break_glass.router, prefix="/v1")
else:
    logger.warning("admin_break_glass router not available — route disabled")

# GSEC FX.126 (owner Q113=A) — platform asset inventory (register of
# workers/servers/stacks + admin-set owner/data-class/site + alerts).
# GET /v1/admin/assets (?format=csv|json), PATCH
# /v1/admin/assets/workers/{name} (platform admin only). Dashboard page
# is part 2.
_admin_assets = optional_paid("routers.admin_assets")  # routers/admin_assets.py is Paid (manifest deny)
if _admin_assets is not None:
    app.include_router(_admin_assets.router, prefix="/v1")
else:
    logger.warning("admin_assets router not available — route disabled")

# GSEC FX.90 p2b (owner decision Q76=A) — malware-quarantine admin surface:
# GET /v1/admin/malware-quarantine lists flagged artefacts, POST
# /v1/admin/malware-quarantine/{cog_id}/release clears the flag (audited).
_admin_malware_quarantine = optional_paid("routers.admin_malware_quarantine")  # routers/admin_malware_quarantine.py is Paid (manifest deny)
if _admin_malware_quarantine is not None:
    app.include_router(_admin_malware_quarantine.router, prefix="/v1")
else:
    logger.warning("admin_malware_quarantine router not available — route disabled")

# GSEC FX.156 part 2 (owner decision Q143=B) — platform security
# alerts (services/security_alert_engine.py +
# services/security_alert_delivery.py). GET
# /v1/platform/security-alerts (?acked=&limit=&offset=) lists every
# alert (platform-level + all tenants, soft-deletes excluded); POST
# /v1/platform/security-alerts/{id}/ack stamps acked_at/acked_by +
# one SECURITY_ALERT_ACKED audit row. Platform admin only. The
# tenant-scoped equivalents live in the tenant router package
# (routers/tenant/security_alerts.py, mounted under /v1/tenant).
try:
    from routers import platform_security_alerts as _platform_security_alerts
    if _paid_import.is_paid_tree():  # FX.162.p1: compliance is paid-only (Q157)
        app.include_router(_platform_security_alerts.router, prefix="/v1")
    else:
        logger.info("FX.162: platform_security_alerts not mounted (CE edition)")
except ImportError:
    logger.warning("platform_security_alerts router not available — "
                   "route disabled")

# GSEC FX.49 part 2 (owner decision Q32=A) — platform break-glass
# custody (services/break_glass_custody.py). Platform admin only: the
# use-request lifecycle (request/approve/deny/use — use is single-use,
# two-person, and fires the alert + platform-admin email), the
# forced-rotation status (the Dashboard banner feed), and the rotation
# (returns fresh shares once, no-store). New use is refused (409) while
# a credential awaits rotation.
try:
    from routers import platform_break_glass as _platform_break_glass
    app.include_router(_platform_break_glass.router, prefix="/v1")
except ImportError:
    logger.warning("platform_break_glass router not available — "
                   "route disabled")

# GSEC FX.70 part 2 (owner decision Q53=A) — in-app support tickets:
# list-all + respond + resolve (platform admin only).
#   * GET  /v1/platform/support-tickets (?status=&overdue=&limit=&offset=)
#   * POST /v1/platform/support-tickets/{id}/respond  (first response +
#     one SUPPORT_TICKET_FIRST_RESPONSE audit row; idempotent)
#   * POST /v1/platform/support-tickets/{id}/resolve  (one
#     SUPPORT_TICKET_RESOLVED audit row; idempotent)
# The tenant-scoped open/list live in the tenant router package
# (routers/tenant/support_tickets.py, mounted under /v1/tenant).
try:
    from routers import platform_support_tickets as _platform_support_tickets
    app.include_router(_platform_support_tickets.router, prefix="/v1")
except ImportError:
    logger.warning("platform_support_tickets router not available — "
                   "route disabled")

# GSEC FX.73 part 2 — platform manual-review queue (Paid-only: the router
# is denied out of CE, so its mount branch is dropped from the CE extract).

# GSEC FX.72 p3a (owner decision Q55=C) — "AI in this platform"
# transparency API (platform admin only):
#   * GET /v1/platform/ai-inventory
try:
    from routers import platform_ai_inventory as _platform_ai_inventory
    app.include_router(_platform_ai_inventory.router, prefix="/v1")
except ImportError:
    logger.warning("platform_ai_inventory router not available — "
                   "route disabled")

# GSEC FX.65 p3b — BYOK platform-admin clear-dark:
#   * POST /v1/platform/tenants/{tenant_id}/kms/clear-dark
try:
    from routers import platform_kms as _platform_kms
    app.include_router(_platform_kms.router, prefix="/v1")
except ImportError:
    logger.warning("platform_kms router not available — route disabled")

# GSEC FX.63 part 1 (owner decisions Q46=A + amendment, Q23=A) — signed
# offline licence file: local verify + upload-to-renew (services/
# offline_licence.py; reuses utils.license_verify/license_file, no
# parallel crypto). Platform admin only, inert (404) on SaaS:
#   * GET  /v1/admin/licence  (state, expiry, capabilities — no secrets)
#   * POST /v1/admin/licence  (upload file -> verify -> store; audited;
#     refuses unsigned/expired-on-upload)
try:
    from routers import offline_licence as _offline_licence
    app.include_router(_offline_licence.router, prefix="/v1")
except ImportError:
    logger.warning("offline_licence router not available — route disabled")

# GSEC FX.59 part 4a — platform-admin tenant suspension / reactivation
# (utils/tenant_lifecycle.py's set_lifecycle_state is the single
# mutation entry point; this router owns the HTTP surface + legality
# check). Platform admin only:
#   * POST /v1/platform/tenants/{id}/suspend     {reason} -> 409 if
#     already suspended
#   * POST /v1/platform/tenants/{id}/reactivate  {reason} -> 409 if
#     already active
try:
    from routers import platform_tenant_lifecycle as _platform_tenant_lifecycle
    app.include_router(_platform_tenant_lifecycle.router, prefix="/v1")
except ImportError:
    logger.warning("platform_tenant_lifecycle router not available — "
                   "route disabled")

# GSEC FX.50 part 2 (owner decision Q33=A) — breach-notification
# incident tracker (services/security_incidents.py). Platform admin
# only: list/get/create/confirm incidents, manage follow-ups, mark
# notified and close. The 72h notification + 24h gov/DoD heads-up
# deadline clocks are computed at confirm time from the incident.*
# platform settings; every state change writes a SECURITY_INCIDENT_*
# audit row. Email delivery + in-app banner are a later part (FX.50
# p3).
try:
    from routers import platform_security_incidents as _platform_security_incidents
    if _paid_import.is_paid_tree():  # FX.162.p1: compliance is paid-only (Q157)
        app.include_router(_platform_security_incidents.router, prefix="/v1")
    else:
        logger.info("FX.162: platform_security_incidents not mounted (CE edition)")
except ImportError:
    logger.warning("platform_security_incidents router not available — "
                   "route disabled")

# GSEC FX.95 p1 (owner decision Q81=A) — marketplace listing review
# state machine (services/listing_review.py). Platform admin only:
# GET the staff approval queue, POST approve/reject a listing awaiting
# staff_review. submitted -> auto_checks -> staff_review -> live|
# rejected; every transition writes a LISTING_* audit row.
_admin_listing_review = optional_paid("routers.admin_listing_review")  # routers/admin_listing_review.py is Paid (manifest deny)
if _admin_listing_review is not None:
    # GSEC FX.59 p2c — the ONE non-staff route on this router is
    # POST /v1/publisher-verifications/request, the publisher's own tenant
    # admin asking to verify their identity: tenant-scoped, so it carries the
    # lifecycle gate (a read_only/suspended tenant cannot request
    # verification). The /v1/admin/listing-review/* staff routes below are
    # platform-admin-gated and exempt by prefix, so the gate is a no-op there.
    app.include_router(_admin_listing_review.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
else:
    logger.warning("admin_listing_review router not available — "
                   "route disabled")

# GSEC FX.69 part 2 (owner decision Q52=A; Q71=C) — hosted-uptime SLA
# monthly report (services/uptime.py). Platform admin only: read-only
# GET /admin/uptime/monthly (uptime samples from the fx69_uptime_sampling
# scheduler job + maintenance windows per the Q52=A rules). The public
# status page is a later part (FX.69 p3).
try:
    from routers import platform_uptime as _platform_uptime
    app.include_router(_platform_uptime.router, prefix="/v1")
except ImportError:
    logger.warning("platform_uptime router not available — "
                   "route disabled")

# GSEC FX.69 part 3 (owner decision Q71=C) — public status-page data:
# GET /public/status (no auth, IP rate-limited inside the router).
# Measured per-component state, 90-day uptime bars, announced maintenance
# windows and incident history. No tenant data or internal names in the
# payload. Q71=C: the page runs on the same site as MOD.
try:
    from routers import public_status as _public_status
    app.include_router(_public_status.router, prefix="/v1")
except ImportError:
    logger.warning("public_status router not available — "
                   "route disabled")

# sz-tcp.8 (launch-completion-megaplan) — public credit-pricing rate
# constants + tier thresholds: GET /v1/platform/credit-rate (no auth, same
# posture as /v1/platform/features). Wizard hint copy only; no tenant data.
try:
    from routers import platform_credit_rate as _platform_credit_rate
    app.include_router(_platform_credit_rate.router, prefix="/v1")
except ImportError:
    logger.warning("platform_credit_rate router not available — "
                   "route disabled")

# sz-tcp.17 — register the per-format-family credit-tier byte-size thresholds
# setting (pricing.credit_tier_thresholds). Side-effect import only: the
# module-level register() call must have run before platform_settings_seed /
# the admin settings page enumerate the registry. Nothing consumes it yet —
# rewiring _determine_size_tier is a separate sz-tcp task.
try:
    from services import credit_tier_thresholds as _credit_tier_thresholds  # noqa: F401
except ImportError as e:
    logger.warning("credit_tier_thresholds settings module not available: %s", e)
except Exception as e:
    logger.warning("credit_tier_thresholds settings module failed to load: %s", e)

# 23.7/GO.8 s2 — register the on-change variant regression gate's platform
# settings (node_build.variant_change_gate_enabled + score_drop) at import.
# Side-effect import only: the module-level register_settings() call must have
# run before platform_settings_seed / the admin settings page enumerate the
# registry, otherwise a dark-by-default gate can never be enabled (PATCH
# /platform/settings/{key} rejects unregistered keys).
try:
    from node_builder.variant_test import change_gate as _change_gate_settings  # noqa: F401
except Exception as e:
    logger.warning("change_gate settings module not available: %s", e)

# wsip1.11 — register the self-improving-production per-tenant flags (all
# default off). Side-effect import only; no consumer exists yet.
try:
    from services import wsip_feature_flags as _wsip_feature_flags  # noqa: F401
except Exception as e:
    logger.warning("wsip_feature_flags settings module not available: %s", e)

# GSEC FX.76 p5a — public trust-centre NDA / security-documentation
# request form: POST /v1/public/trust/nda-requests (no auth, IP rate-
# limited + honeypot inside the router). Writes a review-queue row;
# response never echoes the submitted text.
try:
    from routers import public_nda_requests as _public_nda_requests
    app.include_router(_public_nda_requests.router, prefix="/v1")
except ImportError:
    logger.warning("public_nda_requests router not available — "
                   "route disabled")

# GSEC FX.158 p2a (Q145=A) — public trust-page security-advisory feed:
# GET /v1/public/trust/advisories (no auth, IP rate-limited inside the
# router). Published advisories only, newest first; drafts (published_at
# NULL) and soft-deleted rows are excluded in the service query.
try:
    from routers import public_security_advisories as _public_security_advisories
    app.include_router(_public_security_advisories.router, prefix="/v1")
except ImportError:
    logger.warning("public_security_advisories router not available — "
                   "route disabled")

# GSEC FX.158 p2a companion — platform-admin advisory CRUD (create/edit/
# publish/soft-delete) behind the same platform-admin gate as
# routers/platform_security_incidents.py; every write writes one
# AuditAction.SECURITY_ADVISORY_* row.
try:
    from routers import platform_security_advisories as _platform_security_advisories
    if _paid_import.is_paid_tree():  # FX.162.p1: compliance is paid-only (Q157)
        app.include_router(_platform_security_advisories.router, prefix="/v1")
    else:
        logger.info("FX.162: platform_security_advisories not mounted (CE edition)")
except ImportError:
    logger.warning("platform_security_advisories router not available — "
                   "route disabled")

# GSEC FX.88 p3 — last-admin MFA recovery: public intake (no auth, IP
# rate-limited; uniform 202, never enumerates tenants/users) + the
# platform-admin two-person review/approve path that performs the actual
# Keycloak reset.
try:
    from routers import public_mfa_recovery as _public_mfa_recovery
    app.include_router(_public_mfa_recovery.router, prefix="/v1")
except ImportError:
    logger.warning("public_mfa_recovery router not available — "
                   "route disabled")
try:
    from routers import admin_mfa_recovery as _admin_mfa_recovery
    app.include_router(_admin_mfa_recovery.router, prefix="/v1")
except ImportError:
    logger.warning("admin_mfa_recovery router not available — "
                   "route disabled")

# GSEC FX.63 p2 (owner Q46=A) — outbound-telemetry preview: the exact
# anonymous payload an install would send + per-kind allow state
# (telemetry.update_check / telemetry.usage_stats) + the fedramp_high_il5
# hard-block. Platform admin only; nothing here sends anything.
_admin_telemetry = optional_paid("routers.admin_telemetry")  # routers/admin_telemetry.py is Paid (manifest deny)
if _admin_telemetry is not None:
    app.include_router(_admin_telemetry.router, prefix="/v1")
else:
    logger.warning("admin_telemetry router not available — route disabled")

# GSEC FX.157.p3a (owner Q144=A host hardening) — install/upgrade-time host-hardening
# results (latest run per host, per-check pass/fail/skipped + evidence, plus the
# install-time audit rows). Platform admin only; read-only.
_admin_hardening_results = optional_paid("routers.admin_hardening_results")  # routers/admin_hardening_results.py is Paid (manifest deny)
if _admin_hardening_results is not None:
    app.include_router(_admin_hardening_results.router, prefix="/v1")
else:
    logger.warning("admin_hardening_results router not available — route disabled")

# FX.143 p2c — the owner-facing capacity surfaces: the monthly capacity report
# (services/capacity_alerts.monthly_capacity_report) and the 90-day capacity
# forecast (capacity_forecast_90d). Platform admin only; read-only.
_admin_capacity_report = optional_paid("routers.admin_capacity_report")  # routers/admin_capacity_report.py is Paid (manifest deny)
if _admin_capacity_report is not None:
    app.include_router(_admin_capacity_report.router, prefix="/v1")
else:
    logger.warning("admin_capacity_report router not available — route disabled")

# GSEC FX.163 p2 [B] (owner Q150=A) — segregation of duties / maker-checker:
# the second-approver queue's decision surface (GET list + approve/reject).
# Tenant-admin only; the four gated actions (role grant, full export, BYOK
# revoke, data-marking clear) return 202 awaiting_approval while
# approvals.require_second_approver is ON (or a gov/DoD / banking profile
# forces it) — see services/approval_gate.py.
try:
    from routers import pending_approvals as _pending_approvals
    # GSEC FX.59 p2c — the second-approver queue is the tenant's own
    # decision surface (tenant-admin capability-gated, every lookup scoped to
    # the caller's tenant): gate it so a suspended tenant cannot approve/replay
    # a queued action and a read_only tenant cannot decide one. Pending rows
    # carry expires_at and lapse on their own, so nothing needs to stay
    # reachable during the contract-end window.
    app.include_router(_pending_approvals.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("pending_approvals router not available — route disabled")

# GSEC FX.144 p2b (owner Q131=A) — the owner's ONE-CLICK release approve
# (platform-admin only; install-wide, so no tenant-lifecycle gate needed).
# FX.144 p5 (owner 10-07, Q157=A): release approvals are a COMPLIANCE feature,
# so they are PAID-only — mounted through the ONE edition predicate
# (utils.paid_import.is_paid_tree, the same gate FX.162.p1 uses for the
# compliance routers and the 3 platform_security_* try-mounts). Under CE the
# router is not mounted, so its paths 404 naturally (no per-router 402), and
# open-core/manifest.yaml denies the router + services + Dashboard page/js.
try:
    from routers import release_approvals as _release_approvals
    if _paid_import.is_paid_tree():  # FX.144 p5: compliance is paid-only (Q157)
        app.include_router(_release_approvals.router, prefix="/v1")
    else:
        logger.info("FX.144: release_approvals not mounted (CE edition)")
except ImportError:
    logger.warning("release_approvals router not available — route disabled")

# GSEC FX.125 PART 1 (owner Q112=A) — platform key rotation: runtime
# key/cert/credential inventory with age + keys.* schedule + overdue
# flags (services/key_inventory.py, fails soft per source). The daily
# 04:52 UTC overdue sweep lives in services/scheduler.py. Rotation
# jobs + the admin page UI are part 2.
_admin_keys = optional_paid("routers.admin_keys")  # routers/admin_keys.py is Paid (manifest deny)
if _admin_keys is not None:
    app.include_router(_admin_keys.router, prefix="/v1")
else:
    logger.warning("admin_keys router not available — route disabled")

# FX.71 (job part, Q54=A) — quarterly platform access review (platform
# admins + KC realm admins + manual Vault/Harbor/git/cloud rows).
# GET/POST /v1/admin/platform-access-reviews/* (platform admin only).
_admin_par = optional_paid("routers.admin_platform_access_review")  # routers/admin_platform_access_review.py is Paid (manifest deny)
if _admin_par is not None:
    app.include_router(_admin_par.router, prefix="/v1")
else:
    logger.warning("admin_platform_access_review router not available — route disabled")

# FX.93 part 1 (owner decision Q79=A) — abuse reporting backend.
# POST /v1/abuse/reports (any authenticated user, rate-limited) and
# GET /v1/admin/abuse/reports + POST /v1/admin/abuse/reports/{id}/triage
# (platform admin only). Soft-delete only (ISO 27001); triage
# 'suspend_target' records the requested suspension only (Planned).
try:
    from routers import abuse_reports as _abuse_reports
    # GSEC FX.59 p2c — tenant user's abuse REPORTS stay gated (a suspended
    # tenant cannot create reports); the /v1/admin/abuse/* routes below are
    # platform-admin-gated and exempt by prefix.
    app.include_router(_abuse_reports.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("abuse_reports router not available — route disabled")

# FX.67 part 1 (owner decision Q50=A) — customer lockbox, staff side.
# POST /v1/platform/lockbox/requests (platform admin files a request for a
# tenant-approved, time-limited, scoped grant to read another tenant's
# content) + GET /v1/platform/lockbox/requests (my own requests). The
# tenant side (approve/deny/revoke) mounts under the tenant package at
# /v1/tenant/lockbox/requests. Break-glass remains the sole bypass.
try:
    from routers import lockbox_platform as _lockbox_platform
    app.include_router(_lockbox_platform.router, prefix="/v1")
except ImportError:
    logger.warning("lockbox_platform router not available — route disabled")

# FX.97 part 1 (owner decision Q83=A) — per-output provenance report.
# GET /v1/workflow-runs/{run_id}/provenance (JSON; PDF + C2PA Planned).
# Same run-read permission check as the run-detail endpoint. Writes one
# PROVENANCE_REPORT_EXPORTED audit row per export.
try:
    from routers import provenance_report as _provenance_report
    app.include_router(_provenance_report.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("provenance_report router not available — route disabled")

# FX.98 part 1 (Q84=A) — likeness consent register: tenant-admin CRUD over
# /v1/tenant/likeness-consents (real-person likeness grants for nodes
# tagged 'likeness'; soft-delete only, ISO 27001). Guarded so a fresh
# build without the migration/model disables the route instead of
# crashing.
try:
    from routers import likeness_consent as _likeness_consent
    app.include_router(_likeness_consent.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("likeness_consent router not available — route disabled")

# GX.R4 (launch-completion-megaplan Group X) — ephemeral run inputs.
# POST /v1/runs/ephemeral-inputs (multipart) — a short-TTL file a run can feed
# an input node without it landing in the user's library. Guarded so a fresh
# build without the migration/model disables the route instead of crashing.
try:
    from routers import ephemeral_run_inputs as _ephemeral_run_inputs
    app.include_router(_ephemeral_run_inputs.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("ephemeral_run_inputs router not available — route disabled")

# wizard-learning-from-feedback — admin endpoints backing the
# Settings → AI Configuration → Wizard Learning UI tab.
wizard_ml_admin = optional_paid("routers.wizard_ml_admin")  # routers/wizard_ml_admin.py is Paid (manifest deny)
if wizard_ml_admin is not None:
    app.include_router(wizard_ml_admin.router, prefix="/v1")
else:
    pass  # wizard_ml_admin not available

# Phase 5 — auto-node-build dispatch: POST /v1/auto-node-build/dispatch.
try:
    from routers import auto_node_build as _auto_node_build
    app.include_router(_auto_node_build.router, prefix="/v1/auto-node-build",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("auto_node_build router not available — route disabled")

# GX.S4 (launch-completion-megaplan Group X) — general email send API.
# POST /v1/email/send (CE) + GET /v1/email/messages[/{id}] via the
# tenant's existing SMTP config. Guarded so a fresh build without the
# migration/model disables the route instead of crashing.
try:
    from routers import email_send_api as _email_send_api
    app.include_router(_email_send_api.router, prefix="/v1")
except ImportError:
    logger.warning("email_send_api router not available — route disabled")

# GX.A2 (launch-completion-megaplan Group X) — apps built on MOD: one
# hostname + per-app OIDC client + scoped API key each. POST /v1/apps
# (tenant admin), list/get, update-target, rotate-key, scope, retry-
# provisioning, revoke. Guarded so a fresh build without the apps table
# disables the route instead of crashing.
try:
    from routers import apps as _apps
    app.include_router(_apps.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("apps router not available — route disabled")

# GX.S1b (launch-completion-megaplan Group X) — app user types + app users
# (tenant-admin management: define types, invite/self-signup toggle, list /
# soft-disable, audit). Guarded so a fresh build without the app_user_types
# tables disables the route instead of crashing.
try:
    from routers import app_user_types as _app_user_types
    app.include_router(_app_user_types.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("app_user_types router not available — route disabled")

# GX.S5 (launch-completion-megaplan Group X) — tenant LLM endpoint.
# POST /v1/llm/chat/completions (CE; SSE passthrough on stream:true) +
# GET /v1/llm/models, routed to the tenant's own model stacks. Guarded so a
# fresh build without the migration/model disables the route instead of
# crashing.
try:
    from routers import tenant_llm_api as _tenant_llm_api
    app.include_router(_tenant_llm_api.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("tenant_llm_api router not available — route disabled")

# 2400GX-n6 (30.4.5) — generic through-MOD stack call route:
# /v1/stacks/{id}/services/{svc}/call/{path} (streamed; basic proxy is CE).
try:
    from routers import stack_call as _stack_call
    app.include_router(_stack_call.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("stack_call router not available — route disabled")

# GX.S6 (launch-completion-megaplan Group X) — per-stack/app storage
# scopes: presigned PUT/GET links + object listing/soft-delete under
# stacks/{id}/ and apps/{id}/ in the tenant's file store (or BYO S3).
# Guarded so a fresh build without the migration/model disables the
# route instead of crashing.
try:
    from routers import storage_scopes as _storage_scopes
    app.include_router(_storage_scopes.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("storage_scopes router not available — route disabled")

# GX.S7 (launch-completion-megaplan Group X) — scheduled HTTP triggers:
# signed cron/interval HTTP calls to Persistent Stack / App targets, with
# append-only run history + three-channel failure alerting. Guarded so a
# fresh build without the migration/model disables the route instead of
# crashing.
try:
    from routers import http_triggers as _http_triggers
    # OZ-F3 (launch-completion-megaplan Group OZ): the scheduled-HTTP-triggers
    # family is publicly named /v1/monkeys (rename in place at the MOUNT; the
    # router module and its identifiers stay). No alias mount, no redirect:
    # the old (pre-rename) path is gone (404).
    app.include_router(_http_triggers.router, prefix="/v1/monkeys",
                       tags=["Monkeys"],
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("http_triggers router not available — route disabled")

# GT.D8.2 (launch-completion-megaplan) — tenant k8s cluster registry CRUD.
# Guarded so a fresh build without the migration/model disables the route
# instead of crashing.
try:
    from routers import k8s_clusters as _k8s_clusters
    app.include_router(_k8s_clusters.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("k8s_clusters router not available — route disabled")

# CUIP.5b (FEAT.CUI-PHONES) — mobile-device enrollment/revoke for tenants.
# Guarded so a fresh build without the migration/model disables the route
# instead of crashing.
try:
    from routers import mobile_devices as _mobile_devices
    app.include_router(_mobile_devices.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    logger.warning("mobile_devices router not available — route disabled")

# add-rag-vector-store — API-mediated per-tenant user vector store (RAG).
try:
    from routers import vector_store as _vector_store
    app.include_router(_vector_store.router, prefix="/v1/cyclone",
                       tags=["Cyclone"],
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    pass  # vector_store not available

# named-artifact-registry — per-tenant named handles for RAG corpora + LoRAs,
# so nodes resolve artifacts by name (wired handles + resolve-by-name).
try:
    from routers import named_artifacts as _named_artifacts
    app.include_router(_named_artifacts.router, prefix="/v1",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])
except ImportError:
    pass  # named_artifacts not available

# model-cache-per-tenant-gated-repos — internal endpoint mod-model-cache calls
# to resolve a tenant's own HF token (tenant_id from its verified token only).
try:
    from routers import model_cache_internal as _model_cache_internal
    app.include_router(_model_cache_internal.router, prefix="/v1")
except ImportError:
    pass  # model_cache_internal not available

# GF.C4 — Dashboard "Model cache" panel proxy (platform-admin only): stats /
# models / prefetch / evict against the manager-side mod-model-cache service.
_model_cache_admin = optional_paid("routers.model_cache_admin")  # routers/model_cache_admin.py is Paid (manifest deny)
if _model_cache_admin is not None:
    app.include_router(_model_cache_admin.router, prefix="/v1")
else:
    pass  # model_cache_admin not available

# GF.C1b — "cache as a worker role": platform-admin enable/disable + the
# per-site registry the Dashboard (GF.C4) reads.
try:
    from routers import model_cache_sites as _model_cache_sites
    app.include_router(_model_cache_sites.router, prefix="/v1")
except ImportError:
    pass  # model_cache_sites not available

# GSEC FX.159 p3a (Q146=A) — Enterprise contract-obligations tracker: what a
# customer's contract puts on MOD (SLA / breach-notice windows / data
# location / deletion terms), dated + audited. Platform-admin only (the
# tracker spans every customer relationship; the Security officer is the
# register's owner role). Due-date reminders are p3b.
try:
    from routers import admin_contract_obligations as _contract_obligations
    app.include_router(_contract_obligations.router, prefix="/v1")
except ImportError:
    pass  # admin_contract_obligations not available

# delegated-stack-authoring §1.4 — grant/revoke platform-scoped capabilities
try:
    from routers import platform_capabilities
    router_mapping.append((platform_capabilities.router, ""))
except ImportError:
    pass  # platform_capabilities not available

# add-persistent-stacks — create/deploy/teardown persistent docker-compose stacks
try:
    from routers import persistent_stacks
    # Ingress seams first: /stacks/ingress/* must not be swallowed by /stacks/{stack_id}/...
    router_mapping.append((persistent_stacks.ingress_router, ""))  # caddy-stack-ingress (token-guarded, no user auth)
    router_mapping.append((persistent_stacks.router, ""))
    router_mapping.append((persistent_stacks.stack_router, ""))  # MCP fleet: stack lifecycle (/v1/stack/*)
    router_mapping.append((persistent_stacks.mesh_internal_router, ""))  # dual-anchor §2.1 internal reachability
except ImportError:
    pass  # persistent_stacks not available

# managed-ai-surfaces-and-stack-ops §1 — per-tenant LLM rate limits, settable
# at runtime because a limit needing a redeploy is a limit nobody tunes.
try:
    from routers import llm_rate_limits
    router_mapping.append((llm_rate_limits.router, ""))
    router_mapping.append((llm_rate_limits.usage_router, ""))
except ImportError:
    pass  # llm_rate_limits not available

# add-multi-region-mesh — peer-manager enrollment + registry
try:
    from routers import managers as managers_router
    router_mapping.append((managers_router.router, ""))
except ImportError:
    pass  # managers router not available

# add-multi-region-mesh — fencing-token ownership handoff
try:
    from routers import mesh_handoff
    router_mapping.append((mesh_handoff.router, ""))
except ImportError:
    pass  # mesh_handoff router not available

# add-multi-region-mesh — pooled LLM routing
try:
    from routers import mesh_llm
    router_mapping.append((mesh_llm.router, ""))
except ImportError:
    pass  # mesh_llm router not available

# add-multi-region-mesh — manager-as-directory + write-routing-to-owner
try:
    from routers import mesh_directory
    router_mapping.append((mesh_directory.router, ""))
except ImportError:
    pass  # mesh_directory router not available

# add-multi-region-mesh — data-gravity placement (§6)
try:
    from routers import mesh_gravity
    router_mapping.append((mesh_gravity.router, ""))
except ImportError:
    pass  # mesh_gravity router not available

# manager-work-loans (GC.MWL.3) — revocation gossip receiving side
try:
    from routers import mesh_revocation
    router_mapping.append((mesh_revocation.router, ""))
except ImportError:
    pass  # mesh_revocation router not available

# add-multi-region-mesh — replication RPO dial (§4.2)
try:
    from routers import mesh_replication_config
    router_mapping.append((mesh_replication_config.router, ""))
except ImportError:
    pass  # mesh_replication_config router not available

# manager-work-loans — lease grant (lender side): owner stays owner, a peer
# borrows execution under a time-bounded lease (GC.MWL.6)
try:
    from routers import mesh_loans
    router_mapping.append((mesh_loans.router, ""))
except ImportError:
    pass  # mesh_loans router not available

# 24.5.s2 — NOAH lend/pause (the server-side twin of the worker's local pin):
# the desktop owner's "lend now" / "pause lending (I'm using it)" switch,
# owner-or-admin authz. Its own router (not worker_management.py) so that file
# stays inside the api_lint endpoint budget.
try:
    from routers import noah_mode
    router_mapping.append((noah_mode.router, ""))
except ImportError:
    pass  # noah_mode router not available

# manager-work-loans — collect (owner side, GC.MWL.9) + the borrower's
# staged-outputs peer endpoint it depends on.
try:
    from routers import mesh_loan_transfer
    router_mapping.append((mesh_loan_transfer.router, ""))
except ImportError:
    pass  # mesh_loan_transfer router not available

# mod-connect-service-mesh §7.3 — mesh ACL view/edit (platform-admin)
try:
    from routers import mesh_acl_config
    router_mapping.append((mesh_acl_config.router, ""))
except ImportError:
    pass  # mesh_acl_config router not available

# add-multi-region-mesh — interim ingress mode + direct data URLs (§8.5)
mesh_ingress = optional_paid("routers.mesh_ingress")  # routers/mesh_ingress.py is Paid (manifest deny)
if mesh_ingress is not None:
    router_mapping.append((mesh_ingress.router, ""))
else:
    pass  # mesh_ingress router not available

# add-multi-region-mesh — cross-region trigger relay (triggers <-> mesh seam)
try:
    from routers import mesh_trigger
    router_mapping.append((mesh_trigger.router, ""))
except ImportError:
    pass  # mesh_trigger router not available

# tailnet-plan task 7.9 — MOD DERP admission (verify-client-URL) + authorized nodes.
try:
    from routers import derp_verify as _derp_verify
    router_mapping.append((_derp_verify.router, ""))
except ImportError:
    pass  # derp_verify router not available

# tenant-assistant-knowledge-base §3 — tenant-admin KB management endpoints.
# Mounted defensively so a deployment where the ingester util hasn't landed yet
# still boots (the router imports utils.tenant_kb_ingester at module load).
try:
    from routers import assistant_knowledge
    router_mapping.append((assistant_knowledge.router, ""))
except ImportError:
    logger.warning("assistant_knowledge router not available — KB endpoints disabled")

# GC.PFM.8b — platform-admin calibration-fixture status/migrate + migrator callback.
try:
    from routers import admin_fixtures
    router_mapping.append((admin_fixtures.router, ""))
except ImportError:
    logger.warning("admin_fixtures router not available — fixture admin endpoints disabled")

# GY.C4c — platform-admin inactive-account sweep (AC-2(3)).
admin_accounts = optional_paid("routers.admin_accounts")  # routers/admin_accounts.py is Paid (manifest deny)
if admin_accounts is not None:
    router_mapping.append((admin_accounts.router, ""))
else:
    logger.warning("admin_accounts router not available — inactive-account sweep endpoint disabled")

# GY.C4.MFA — admin 2FA reset on MOD pages (replaces the Keycloak console).
try:
    from routers import admin_mfa
    router_mapping.append((admin_mfa.router, ""))
except ImportError:
    logger.warning("admin_mfa router not available — admin 2FA reset endpoint disabled")

# GSEC FX.78 p3 (owner Q61=A) — WebAuthn required for admins under the gov
# profile: the admin-facing requirement read (Dashboard banner), same shape
# as GET /v1/me/mfa/enforcement.
webauthn_enforcement = optional_paid("routers.webauthn_enforcement")  # routers/webauthn_enforcement.py is Paid (manifest deny)
if webauthn_enforcement is not None:
    router_mapping.append((webauthn_enforcement.router, ""))
else:
    logger.warning("webauthn_enforcement router not available — gov WebAuthn "
                   "admin requirement read disabled")

# GSEC R-096 — admin locked-account listing + unlock (KC attack detection).
admin_lockout = optional_paid("routers.admin_lockout")  # routers/admin_lockout.py is Paid (manifest deny)
if admin_lockout is not None:
    router_mapping.append((admin_lockout.router, ""))
else:
    logger.warning("admin_lockout router not available — locked-account endpoints disabled")

# FX.106 (owner Q93=A) — admin rate-limit auto-block list + unblock.
admin_api_blocks = optional_paid("routers.admin_api_blocks")  # routers/admin_api_blocks.py is Paid (manifest deny)
if admin_api_blocks is not None:
    router_mapping.append((admin_api_blocks.router, ""))
else:
    logger.warning("admin_api_blocks router not available — FX.106 block admin endpoints disabled")

# FX.106 p2b — platform-admin IP-block page (list + unblock, audited).
platform_ip_blocks = optional_paid("routers.platform_ip_blocks")  # routers/platform_ip_blocks.py is Paid (manifest deny)
if platform_ip_blocks is not None:
    router_mapping.append((platform_ip_blocks.router, ""))
else:
    logger.warning("platform_ip_blocks router not available — FX.106 p2b block page disabled")

# GSEC FX.120 p2c (owner Q107=A) — the platform-admin JIT elevation page
# (file / list / read / approve / deny / revoke a production elevation).
try:
    from routers import platform_elevation
    router_mapping.append((platform_elevation.router, ""))
except ImportError:
    logger.warning("platform_elevation router not available — FX.120 p2c elevation page disabled")

# fortress-2.1d — read + verify a run's signed proof bundle.
try:
    from routers import run_proof_bundles
    router_mapping.append((run_proof_bundles.router, ""))
except ImportError:
    logger.warning("run_proof_bundles router not available — proof bundle read disabled")

# Phase 4 — goal-directed planner: skeleton -> concrete graph specialization,
# planner sessions. Served at /v1/planner/*; every endpoint already requires
# require_active_and_onboarded_user and is tenant-scoped internally.
try:
    from routers import goal_directed_planner
    router_mapping.append((goal_directed_planner.router, ""))
except ImportError:
    logger.warning("goal_directed_planner router not available — /v1/planner disabled")

# GSEC FX.59 p2b — central tenant-lifecycle gate. Attached ONCE here (the
# single canonical mount) so every tenant-scoped router inherits the
# method-aware checks: GET/HEAD/OPTIONS -> require_tenant_active (403 when
# suspended, reads open in read_only), mutating -> require_tenant_writable
# (423 when read_only/suspended). The gate self-skips on exempt prefixes
# (auth/login/me/subscription/checkout/tenant-export/support/health/
# webhooks/platform//admin//workers — see utils.tenant_lifecycle
# .GATE_EXEMPT_PREFIXES) and when no tenant user is resolvable (public,
# worker, and signature-auth routes keep their own auth). Platform admins
# bypass. Platform- and admin-scoped routes carry their own
# require_platform_admin / require_auditor auth, so the gate's extra tenant
# check is a no-op for them (admins bypass; service callers resolve no
# tenant user and are skipped). (`_Depends` / `_tenant_lifecycle_gate` are
# imported once near the first direct include above.)

# Gate 1: single canonical mount — every router serves only under /v1.
for router, prefix in router_mapping:
    app.include_router(router, prefix=f"/v1{prefix}",
                       dependencies=[_Depends(_tenant_lifecycle_gate)])

# workflow-productivity-floor — live debugging (breakpoints, frame/step/
# hot-swap), budgets (cost_economics) and OAuth connections (4.1). The HTTP
# routers are gated per tenant by the productivity-floor rollout on top of the
# lifecycle gate. The debug WS router gates itself (ticket + assert_rollout_ws)
# and takes no router-level deps: tenant_lifecycle_gate needs a Request.
from routers import (
    mcp_tools_router as _mcp_tools_router,
    workflow_breakpoints as _workflow_breakpoints,
    workflows_debug as _workflows_debug,
    workflows_debug_ws as _workflows_debug_ws,
)

# CE-guarded productivity-floor routers.
try:
    from routers import oauth_connections as _oauth_connections
except (ImportError, ModuleNotFoundError):
    _oauth_connections = None
    logger.warning("oauth_connections router not available")

try:
    from routers import workflow_budgets as _workflow_budgets
except (ImportError, ModuleNotFoundError):
    _workflow_budgets = None
    logger.warning("workflow_budgets router not available")

try:
    from services.productivity_floor_rollout_dep import (
        require_rollout as _pf_require_rollout,
    )
except (ImportError, ModuleNotFoundError):
    _pf_require_rollout = None
    logger.warning("productivity_floor_rollout_dep not available")

_pf_routers = [
    (_workflows_debug.router, "live_debugging"),
    (_workflow_breakpoints.router, "live_debugging"),
]
if _workflow_budgets is not None:
    _pf_routers.append((_workflow_budgets.router, "cost_economics"))
if _oauth_connections is not None:
    _pf_routers.append((_oauth_connections.router, None))
_pf_routers.append((_mcp_tools_router.router, None))  # mcpt.a2/a3 exposed-workflow MCP tools (/v1/mcp-tools/*)

for _pf_router, _pf_feature in _pf_routers:
    _pf_deps = [_Depends(_tenant_lifecycle_gate)]
    if _pf_feature and _pf_require_rollout is not None:
        _pf_deps.append(_Depends(_pf_require_rollout(_pf_feature)))
    app.include_router(_pf_router, prefix="/v1", dependencies=_pf_deps)
app.include_router(_workflows_debug_ws.router, prefix="/v1")

logger.info("API versioning: all routes served under /v1/ (single canonical mount)")

# GY.C14 — RFC 9116 security.txt. The RFC pins the path at
# /.well-known/security.txt, so this mounts OUTSIDE the /v1 sweep (like the
# /health liveness probe above): public, unauthenticated, no tenant data.
try:
    from routers import security_txt as _security_txt
except (ImportError, ModuleNotFoundError):
    _security_txt = None
    logger.warning("security_txt router not available")
if _security_txt is not None:
    app.include_router(_security_txt.router, prefix="")

# 30.6.5 (GX.S1c c5a) — JWKS for MOD's RS256 tokens. The router carries its own
# full paths (/v1/.well-known/jwks.json + the unversioned /.well-known/jwks.json
# alias), so it mounts with prefix="" like security_txt: public, unauthenticated,
# served from memory only (no outbound fetch), Cache-Control + ETag.
try:
    from routers import well_known as _well_known
except (ImportError, ModuleNotFoundError):
    _well_known = None
    logger.warning("well_known router not available")
if _well_known is not None:
    app.include_router(_well_known.router, prefix="")


# --- Worker Discovery Background Task ---
def discover_workers_background():
    """
    Background task that periodically discovers workers and updates database.

    Runs every minute to keep worker list fresh.
    """
    from routers.worker_management import discover_workers, mark_offline_workers

    while True:
        try:
            time.sleep(60)  # Run every minute

            # Get database session
            db = SessionLocal()
            try:
                discovered = discover_workers(db)
                mark_offline_workers(db, discovered)
                from routers.worker_tasks import cleanup_expired_task_claims
                cleanup_expired_task_claims(db)
            finally:
                db.close()

        except Exception as e:
            logger.warning(f"Worker discovery error: {e}")
            time.sleep(60)


def _startup_assistant_site_map():
    """Load + validate the assistant site map. Raises SiteMapError on
    a malformed YAML or schema violation; the lifespan wrapper logs
    the failure at ERROR level so it's loudly visible in startup
    logs. When the load fails, the assistant routes refuse to serve
    until a platform admin POSTs /assistant/site-map/reload with a
    fixed YAML — the rest of the API stays up.
    """
    from utils.paid_import import optional_paid
    _asm = optional_paid("utils.assistant_site_map")
    if _asm is None:
        logger.info("assistant_site_map not in this edition")
        return
    site_map = _asm.load_and_validate()
    logger.info(
        "Assistant site map ready: %d pages, %d distinct dialogs",
        len(site_map.pages), len(site_map.dialog_id_to_pages),
    )


def _startup_seed_embedding_models():
    """Seed the platform's standard embedding models into MinIO on deploy, so
    every server self-populates from HuggingFace once and RUNTIME never needs HF
    (MOD_EMBED_NO_HF can then be set safely). Best-effort, idempotent, background
    — a warm server sees them already present and skips instantly. Single-flight
    across uvicorn workers so N processes don't all download the same ~GB models.
    """
    def _seed():
        try:
            import redis as _redis
            _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
            if not _r.set("mod:embed_model_seed:lock", "1", nx=True, ex=3600):
                logger.info("Embedding seed: another worker holds the lock; skipping")
                return
        except Exception as e:  # noqa: BLE001 — lock is best-effort
            logger.warning("Embedding seed: lock unavailable (%s); proceeding", e)
        try:
            from utils.embedding_model_store import seed_platform_embedding_models
            logger.info("Embedding models seed: %s", seed_platform_embedding_models())
        except Exception as e:  # noqa: BLE001
            logger.error("Embedding models seed failed: %s", e)

    threading.Thread(target=_seed, daemon=True, name="EmbedModelSeed").start()
    logger.info("Embedding model seed dispatched to background thread (non-blocking)")


def _startup_kb_stale_sweep():
    """Reset tenant-KB documents stuck in 'processing' from a prior run.

    Ingestion runs via in-process BackgroundTasks, which are lost on an API
    restart, so a document mid-ingest at shutdown would stay 'processing'
    forever. Flip stale rows to 'failed' so a tenant admin can reindex.
    Best-effort — never crashes startup.
    """
    try:
        from database import SessionLocal
        from utils.paid_import import optional_paid
        _kb_mod = optional_paid("utils.tenant_kb_ingester")
        if _kb_mod is None:
            logger.info("utils.tenant_kb_ingester not in this edition; skipped")
            return
        startup_sweep = _kb_mod.startup_sweep
        db = SessionLocal()
        try:
            n = startup_sweep(db)
            if n:
                logger.info(
                    "KB stale-sweep: reset %s stuck 'processing' document(s) to 'failed'", n
                )
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 — best-effort startup hook
        logger.warning("KB stale-sweep skipped: %s", e)


def _startup_docs_rag_ingest():
    """Idempotent docs ingestion into Qdrant. Both bots (support_chat
    + mod-assistant-agent) read from the docs_embeddings collection;
    without this hook, a fresh stack would have an empty collection
    until a platform admin manually POSTed
    /platform/support/reindex-docs.

    Strategy: if the collection has zero points (or doesn't exist),
    run ingest_docs() once. If it already has points, log the count
    and skip — keeping startup cheap on warm deploys. Manual
    re-index endpoint is the rebuild path when docs content changes
    mid-deploy.

    CRITICAL (launch-day): the actual embed is dispatched to a BACKGROUND
    THREAD. On a fresh deploy the docs corpus embed takes minutes at ~16s/
    batch, and running it synchronously here blocked the lifespan — so uvicorn
    never bound :80 and EVERY request (including /auth/exchange-code) 502'd,
    producing a signup loop. A Redis single-flight lock means only ONE worker/
    manager does the embed instead of all N uvicorn workers (thundering herd).
    """
    from utils.paid_import import optional_paid
    _docs = optional_paid("utils.docs_ingester")
    if _docs is None:
        logger.info("docs_ingester not in this edition")
        return
    _get_qdrant_client = _docs._get_qdrant_client
    _get_embedding_model = _docs._get_embedding_model
    QDRANT_COLLECTION = _docs.QDRANT_COLLECTION
    ingest_docs = _docs.ingest_docs
    client = _get_qdrant_client()
    if client is None:
        logger.warning(
            "Docs RAG: qdrant client unavailable — both bots will "
            "answer without docs context. Check qdrant-client install "
            "+ mod_qdrant container.",
        )
        return
    # NOTE: the embedding-model availability check moved INTO the background
    # thread below — on a fresh server the model is seeded there first, so a
    # synchronous check here would wrongly skip ingest before the seed lands.
    try:
        info = client.get_collection(QDRANT_COLLECTION)
        if info.points_count and info.points_count > 0:
            logger.info(
                "Docs RAG: collection %s already has %d points; skipping ingest",
                QDRANT_COLLECTION, info.points_count,
            )
            return
    except Exception:
        # Collection missing — ingest_docs() will create it.
        pass

    def _ingest():
        # Single-flight across workers/managers: only one process embeds.
        try:
            import redis as _redis
            _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
            if not _r.set("mod:docs_rag_ingest:lock", "1", nx=True, ex=1800):
                logger.info("Docs RAG: another worker holds the ingest lock; skipping")
                return
        except Exception as e:  # noqa: BLE001 — lock is best-effort, never block ingest on it
            logger.warning("Docs RAG: ingest lock unavailable (%s); proceeding", e)
        # Ensure the embedding model is in MinIO before embedding (fresh server).
        try:
            from utils.embedding_model_store import seed_platform_embedding_models
            seed_platform_embedding_models()
        except Exception as e:  # noqa: BLE001
            logger.warning("Docs RAG: embedding model seed failed: %s", e)
        if _get_embedding_model() is None:
            logger.warning("Docs RAG: embedding model unavailable after seed — skipping ingest.")
            return
        try:
            result = ingest_docs()
            logger.info("Docs RAG ingest: %s", result)
        except Exception as e:
            logger.error("Docs RAG ingest failed: %s", e)

    threading.Thread(target=_ingest, daemon=True, name="DocsRagIngest").start()
    logger.info("Docs RAG ingest dispatched to background thread (non-blocking)")


def _startup_build_task_keypair():
    """mesh-ready-build-credentials — ensure this manager has its own build-task
    signing keypair (env for the bootstrap manager; per-manager Vault key for
    enrolled peers). Self-heal is fail-safe and idempotent."""
    from utils.build_task_token import ensure_manager_keypair
    status = ensure_manager_keypair()
    logger.info("build-task signing key: %s", status)


def _startup_mesh_bootstrap():
    """sz-mesh-infra.7w — idempotent mesh bring-up registrations (local DERP
    region). Gated on `mesh.tailnet_enabled` (default False): a mesh-less
    install does no file I/O here at all. `bootstrap_mesh()` already swallows
    its own exceptions (see services/mesh_bootstrap.py), and the
    `_startup_tasks` runner wraps every entry in try/except as a second
    backstop, so a failure here is logged and never blocks API startup.
    Local-file I/O only (no network calls), so it runs inline like the other
    sync startup tasks rather than via run_in_executor."""
    from utils.paid_import import optional_paid
    _mesh = optional_paid("services.mesh_identity")
    if _mesh is None:
        logger.info("mesh_identity not in this edition")
        return
    from services import platform_settings as _ps
    TAILNET_ENABLED_KEY = _mesh.TAILNET_ENABLED_KEY
    if not bool(_ps.get_setting(TAILNET_ENABLED_KEY)):
        logger.info("mesh_bootstrap: mesh.tailnet_enabled is False — skipped")
        return
    from utils.paid_import import optional_paid
    _mb = optional_paid("services.mesh_bootstrap")
    if _mb is None:
        logger.info("mesh_bootstrap not in this edition")
        return
    bootstrap_mesh = _mb.bootstrap_mesh
    bootstrap_mesh()


def _startup_managed_ai_ladder_check():
    """per-rung-reasoner §2.4 — verify the managed-AI ladder adds up.

    A rung's advertised floor is what an operator buys hardware against. When it
    describes less than the rung provisions, the symptom is a stack that
    provisions successfully, waits, and never runs — with the reason buried in
    placement arithmetic nobody reads. That shipped: `compact` advertised a
    16 GB card and inherited a 22.5 GB reasoner.

    LOUD BUT NOT FATAL, deliberately. The startup runner logs and continues, and
    that is the right outcome here: a bad ladder affects managed-AI provisioning
    only, and refusing to boot the whole API over it would turn a configuration
    error into an outage of everything else. CI is where this is fatal —
    `assert_ladder_integrity()` raises, so the test suite fails before a bad
    rung can be merged, which is earlier and cheaper than catching it here.

    Compares declared numbers only. No device probing, so it cannot be affected
    by what hardware happens to be present, and it reports the same verdict on a
    laptop as in production.
    """
    from utils.paid_import import optional_paid
    _mai = optional_paid("services.managed_ai_stacks")
    if _mai is None:
        logger.info("managed_ai_stacks not in this edition")
        return
    check_ladder_integrity = _mai.check_ladder_integrity

    rows = check_ladder_integrity()
    bad = [r for r in rows if not r["ok"]]
    if not bad:
        logger.info(
            "managed-AI ladder OK: %s",
            ", ".join(f"{r['preset']}={r['requirement']}" for r in rows))
        return
    for r in bad:
        logger.error(
            "managed-AI ladder: rung %r advertises %s but %s. Stacks on this "
            "rung will provision and then fail to place.",
            r["preset"], r["requirement"], "; ".join(r["problems"]))


def _startup_ai_binding_alias_migration():
    """ai-config-stack-binding 1.2 (design D6) — one-time alias → binding pass.

    The legacy answer to "which model serves the wizard" was a docker HOST
    ALIAS in a Vault blob (`http://ollama:11434`, `http://vllm-big:8000`).
    Aliases are only right while some container happens to carry one, and
    `vllm-big` has no container at all, so a slot pointing there was silently
    dead. This records each blob's answer in the queryable `ai_slot_binding`
    table instead: `local` → the `PersistentStack` that ACTUALLY serves the
    role (resolved from the stacks' own compose aliases, never a hardcoded
    container name), an alias nothing serves → `auto` ("Let MOD choose"), and
    `self_hosted`/`cloud`/`advanced` → `external` with `key_ref` at the Vault
    path — the raw key is never copied into DB.

    Idempotent (a slot that already has an enabled row is skipped) and
    audited (append-only `AuditEvent` per row written). The Vault blobs are
    NOT deleted or rewritten — ISO 27001, they just stop being the source of
    truth.

    Guarded no-op: while the `ai_slot_binding` table has not been migrated in
    yet, `run_migration()` probes on its own short-lived session, logs, and
    returns {} — so this hook is inert on the current stack and never
    poisons a session or crashes startup.
    """
    try:
        from utils.paid_import import optional_paid
        _ab_mod = optional_paid("services.ai_binding_migrate")
        if _ab_mod is None:
            logger.info("services.ai_binding_migrate not in this edition; skipped")
            return
        run_migration = _ab_mod.run_migration
        summary = run_migration()
        if summary:
            logger.info(
                "ai-binding alias migration: scanned=%s stack=%s external=%s "
                "auto=%s skipped=%s no_blob=%s refused=%s",
                summary.get("scanned", 0), summary.get("stack", 0),
                summary.get("external", 0), summary.get("auto", 0),
                summary.get("skipped", 0), summary.get("no_blob", 0),
                summary.get("refused", 0))
        else:
            logger.info("ai-binding alias migration: no-op "
                        "(ai_slot_binding table not present yet)")
    except Exception as e:  # noqa: BLE001 — never crash startup
        logger.error("ai-binding alias migration failed: %s", e)


def _startup_backfill_cog_owner_grants():
    """Gate 24 (unified-authorization) — ensure every existing FOLDER cog's owner
    holds an OWNER `ResourcePermission`, so removing the `cog.owner_id == user`
    short-circuit does not lock existing owners out. Also a safety-net for any
    create path that misses grant-on-create. FILE cogs excluded by design (they
    keep ownership via files.user_id). Runs after seed_capabilities + the cog
    permission backfill. Idempotent and additive — a no-op once materialised."""
    from database import SessionLocal
    from resource_permissions import backfill_cog_owner_grants
    db = SessionLocal()
    try:
        n = backfill_cog_owner_grants(db)
        if n:
            logger.info("Cog owner-grant backfill: %s folders ensured", n)
    finally:
        db.close()


def _startup_seed_platform_settings():
    """GS.PSR.5 — apply the declarative platform-settings seed for this
    install's DEPLOYMENT_MODE, once per boot.

    Idempotent: `seed_platform_settings` only writes a key that has no live
    platform-scope DB row yet, so an admin's own edit (or a previous boot's
    seed) is never touched. This replaces installers hand-writing behavioural
    keys into `.env.public` — the on-prem installer also runs
    `python3 -m tools.seed_platform_settings --commit` for the same effect
    on an existing install that skips a full re-provision.
    """
    from database import SessionLocal
    from services.platform_settings_seed import seed_platform_settings
    mode = "saas" if os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas" else "onprem"
    db = SessionLocal()
    try:
        results = seed_platform_settings(db, mode)
        db.commit()
        applied = [r["key"] for r in results if r["applied"]]
        if applied:
            logger.info("Platform-settings seed (mode=%s): seeded %s",
                        mode, ", ".join(applied))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# 1675b — the platform worker names that get a bootstrap-token enrollment row
# from the SAME Vault token. EVERY edition runs the embedded worker as
# `mod-worker-embedded` (CORE/startup/docker-compose.yml and
# open-core/overlay/docker-compose.yml; the Go worker pins the name). The legacy
# CE name `mod-worker-ce` stays in the set ONLY so an install that has not been
# updated yet keeps authenticating (migration 20261109a renames its row). Both read
# KV v2 `infrastructure/embedded-worker` via GetInfraSecret("embedded-worker"),
# so they share one token and one seed loop. Adding a name here is the ONLY
# way a worker gets a row: the heartbeat handler never auto-creates an
# enrollment for an arbitrary caller (that was the 1675 fail-open hole).
_EMBEDDED_WORKER_SEED_NAMES = ("mod-worker-embedded", "mod-worker-ce")


def _startup_seed_embedded_worker_enrollment():
    """Provision the embedded worker's bootstrap token in Vault + seed its hash.

    The embedded worker (mod-worker-embedded) runs on the Docker network and,
    unlike remote workers, is never taken through the enrollment flow — it is
    auto-created on first heartbeat. Since heartbeat auth now requires a valid
    X-Bootstrap-Token for every enrolled worker regardless of source IP (network
    position is not a credential — the API is only reachable via nginx, whose
    proxy IP would otherwise read as "internal"), the embedded worker needs a
    token too.

    Secrets live in Vault (never .env). The token is provisioned by vault-init.sh
    at KV v2 `infrastructure/embedded-worker` (key `bootstrap_token`) — the same
    path the Go worker reads via GetInfraSecret("embedded-worker"). The API's
    AppRole is read-only on infrastructure/*, so here we only READ it and hash it
    onto a WorkerEnrollment row for the heartbeat handler to verify. The worker
    `depends_on: api (service_healthy)`, so this runs before the worker boots and
    both read the same Vault value. Fail-safe: if Vault lacks the secret, skip
    (handler falls back to the internal auto-create path).
    """
    import os as _os
    role_id = _os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = _os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        logger.warning(
            "Embedded worker enrollment seed skipped: no VAULT_APPROLE_* "
            "(cannot read the bootstrap token from Vault)")
        return
    vault_path = "infrastructure/embedded-worker"
    try:
        from utils.vault_client import approle_login, read_kv_v2
        vtoken = approle_login(role_id=role_id, secret_id=secret_id)
        data = read_kv_v2(vault_path, token=vtoken) or {}
        token = (data.get("bootstrap_token") or "").strip()
    except Exception as exc:  # fail-safe — never crash startup
        logger.warning("Embedded worker enrollment seed skipped: Vault read failed (%s)", exc)
        return
    if not token:
        logger.warning(
            "Embedded worker enrollment seed skipped: no bootstrap_token at Vault "
            "%s (run vault-init.sh to provision it)", vault_path)
        return
    from sqlalchemy.exc import IntegrityError
    from database import SessionLocal
    from models import WorkerEnrollment, WorkerEnrollmentStatus
    from models import WorkerLicense
    from security import hash_api_key
    db = SessionLocal()
    try:
        token_hash = hash_api_key(token)

        def _seed_one(_seed_name):
            def _update_existing():
                r = db.query(WorkerEnrollment).filter(
                    WorkerEnrollment.worker_name == _seed_name
                ).first()
                if r is not None:
                    r.bootstrap_token_hash = token_hash
                    r.status = WorkerEnrollmentStatus.APPROVED
                    # BUG-1029: the seed reuses whatever row owns the name,
                    # including a tombstone left by a decommission. Revive it
                    # (same contract as crud/worker_management.reactivate_*)
                    # or the seeded embedded worker stays invisible to every
                    # `deleted_at IS NULL` live lookup forever.
                    r.deleted_at = None
                    r.deleted_by_user_id = None
                    lic = db.query(WorkerLicense).filter(
                        WorkerLicense.worker_name == _seed_name
                    ).first()
                    if lic is not None:
                        lic.deleted_at = None
                        lic.deleted_by_user_id = None
                    db.commit()

            row = db.query(WorkerEnrollment).filter(
                WorkerEnrollment.worker_name == _seed_name
            ).first()
            if row is None:
                db.add(WorkerEnrollment(
                    worker_name=_seed_name,
                    bootstrap_token_hash=token_hash,
                    approval_code="000000",
                    status=WorkerEnrollmentStatus.APPROVED,
                ))
                try:
                    db.commit()
                except IntegrityError:
                    # Runs once per uvicorn worker process — another beat us to the
                    # INSERT. Roll back and update the row that now exists.
                    db.rollback()
                    _update_existing()
            else:
                _update_existing()

        for _seed_name in _EMBEDDED_WORKER_SEED_NAMES:
            _seed_one(_seed_name)
        logger.info(
            "Embedded-worker bootstrap-token enrollment seeded/updated for %s",
            ", ".join(_EMBEDDED_WORKER_SEED_NAMES),
        )
    finally:
        db.close()


def _startup_seed_ce_admin_user():
    """1681 — CE/on-prem: seed the first admin's `users` row from the
    Keycloak admin user that keycloak-init.sh creates.

    Only runs in on-prem/CE mode (not SaaS). Idempotent: skips if a row
    for this email already exists. Fail-safe: never crashes startup.

    1786 — the 3 uvicorn boot workers run this concurrently. The insert is
    wrapped in a Postgres advisory transaction lock (same pattern as
    `_startup_seed_harbor_scan_followup_workflow` / `_startup_seed_web_search_workflow`)
    so the existence-check and the INSERT serialize, and the commit is additionally
    guarded against `IntegrityError` (same pattern as
    `_startup_seed_embedded_worker_enrollment`): the loser rolls back cleanly and
    logs exactly one INFO line, no traceback.
    """
    # Stable identifier for the advisory lock. Any unique int works; the value is
    # just a key into a Postgres-wide namespace (bee0/bee1 are the workflow seeds).
    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee2  # noqa: E501
    if _is_saas():
        logger.info("seed_ce_admin_user: SaaS mode — skipped")
        return

    # The CE realm seeds this exact user (core-realm.json:114-117).
    # In non-CE installs the env var may differ; read it from the
    # realm's configured admin email.
    admin_email = os.getenv("INITIAL_USER_EMAIL", "admin@core.local").strip('"').strip("'")

    from database import SessionLocal
    from models import User, UserStatus, Tenant

    db = SessionLocal()
    try:
        from sqlalchemy import text as _sa_text
        from sqlalchemy.exc import IntegrityError

        # Held until COMMIT / ROLLBACK on this session — serializes the boot
        # workers so only one of them sees "no existing row".
        db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": ADVISORY_LOCK_ID})

        # Skip if a row for this email already exists (idempotent).
        existing = db.query(User).filter(User.email == admin_email).first()
        if existing:
            # 2400LR-sec1: self-heal rows seeded before the Admin role was
            # assigned (require_capability has no platform-admin bypass, so a
            # role-less admin got 403 on every capability-gated route).
            if existing.custom_role_id is None:
                from crud.tenant_cud import ensure_tenant_system_roles
                _roles = ensure_tenant_system_roles(db, existing.tenant_id)
                existing.custom_role_id = _roles["Admin"].id
                db.commit()
                logger.info("seed_ce_admin_user: assigned Admin role to existing %s", admin_email)
                return
            logger.info("seed_ce_admin_user: row for %s already exists — skip", admin_email)
            return

        platform_tenant = db.query(Tenant).filter(Tenant.id == 1).first()
        if not platform_tenant:
            logger.warning(
                "seed_ce_admin_user: tenant 1 not found — "
                "run create_tenant.py first")
            return

        # Look up the KC admin user by email.
        from keycloak_auth import keycloak_admin_client
        kc_users = keycloak_admin_client.get_users_by_email(admin_email)
        if not kc_users:
            logger.warning(
                "seed_ce_admin_user: no Keycloak user found at email %s — "
                "run keycloak-init.sh first", admin_email)
            return
        if len(kc_users) > 1:
            logger.warning(
                "seed_ce_admin_user: %d Keycloak users share email %s — "
                "ambiguous, creating no row", len(kc_users), admin_email)
            return

        kc = kc_users[0]
        # 2400LR-sec1: the seeded admin must hold the tenant's system Admin
        # role, else require_capability (no platform-admin bypass) 403s.
        # ensure_tenant_system_roles commits internally, so it lives INSIDE the
        # IntegrityError guard below — a boot worker that loses the race must
        # still roll back cleanly and log one INFO line, no traceback.
        from crud.tenant_cud import ensure_tenant_system_roles
        try:
            _admin_role = ensure_tenant_system_roles(db, platform_tenant.id)["Admin"]
            user = User(
                custom_role_id=_admin_role.id,
                keycloak_user_id=kc["id"],
                username=kc.get("username", admin_email),
                email=admin_email,
                first_name=kc.get("firstName", "Platform"),
                last_name=kc.get("lastName", "Admin"),
                tenant_id=platform_tenant.id,
                status=UserStatus.ACTIVE,
                onboarding_complete=True,
                is_platform_admin=True,
            )
            db.add(user)
            db.commit()
        except IntegrityError:
            # Another uvicorn boot worker beat us to the INSERT (the advisory
            # lock serializes the check, but a seeder from a later boot or a
            # concurrent request can still win). Roll the aborted session back
            # cleanly and log one INFO line — no traceback.
            db.rollback()
            logger.info(
                "seed_ce_admin_user: row for %s already seeded by another "
                "boot worker — nothing to do", admin_email,
            )
            return
        db.refresh(user)
        logger.info(
            "seed_ce_admin_user: created users row id=%d for %s (KC id=%s)",
            user.id, kc.get("username"), kc.get("id"),
        )
    except Exception as _e:
        err_msg = str(_e)
        if "403" in err_msg or "Forbidden" in err_msg:
            logger.warning(
                "seed_ce_admin_user: Keycloak admin API returned 403 — "
                "fastapi-admin-service likely missing realm-management "
                "role(s) (view-users / manage-users). "
                "Check open-core/overlay/keycloak/core-realm.json "
                "service account clientRoles. (non-fatal)")
        else:
            logger.exception("seed_ce_admin_user: unexpected error (non-fatal)")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _startup_seed_ce_mobile_client():
    """CE-MOBSEED — CE/on-prem: provision the public `mod-mobile` Keycloak
    client on boot.

    Same gate as `_startup_seed_ce_admin_user` (on-prem/CE only, never SaaS).
    The compose `mod-migrate` one-shot now really does skip the subprocess
    seeds (`--no-seeds`), so the `seed:mobile_client` step (tools/mod_migrate.py
    -> provision_mobile_client.py -> keycloak_auth.KeycloakAdmin.ensure_mobile_client)
    no longer runs on the CE boot path; this covers it in-process.

    Idempotent (ensure_mobile_client updates in place if the client already
    exists) and fail-safe: any failure is logged with the exception TYPE only
    — never the message, which could carry a client secret — and never raised,
    so boot always continues.
    """
    if _is_saas():
        logger.info("seed_ce_mobile_client: SaaS mode — skipped")
        return
    try:
        from keycloak_auth import KeycloakAdmin
        result = KeycloakAdmin().ensure_mobile_client()
        logger.info("seed_ce_mobile_client: %s — client %s",
                    result.get("action", "provisioned"), result.get("client_id"))
    except Exception as exc:
        # Fail-safe: log the exception TYPE only — `str(exc)` from the KC admin
        # API can carry the client secret, so it must never reach the log.
        logger.warning("seed_ce_mobile_client: provisioning skipped (non-fatal): %s",
                       type(exc).__name__)


def _startup_seed_ce_cli_client():
    """KEYCLOAK-CLI-DEVICE-CLIENT — CE/on-prem: provision the public `mod-cli`
    Keycloak client (OAuth device-authorization grant) on boot, so command-line
    tools can log a user in interactively (user code approved in the browser,
    the CLI stores a Bearer and refreshes it). Owner owner 11-25: yes — public
    client, no quantity cap, no licence gate; CE gets it with the mobile
    client.

    Same gate as `_startup_seed_ce_mobile_client` (on-prem/CE only, never SaaS)
    and the same fail-safe contract: any failure is logged with the exception
    TYPE only — never `str(exc)`, which could carry a client secret — and never
    raised, so boot always continues. Idempotent (ensure_cli_client updates in
    place if the client already exists).
    """
    if _is_saas():
        logger.info("seed_ce_cli_client: SaaS mode — skipped")
        return
    try:
        from keycloak_auth import KeycloakAdmin
        result = KeycloakAdmin().ensure_cli_client()
        logger.info("seed_ce_cli_client: %s — client %s",
                    result.get("action", "provisioned"), result.get("client_id"))
    except Exception as exc:
        # Fail-safe: log the exception TYPE only — `str(exc)` from the KC admin
        # API can carry a client secret, so it must never reach the log.
        logger.warning("seed_ce_cli_client: provisioning skipped (non-fatal): %s",
                       type(exc).__name__)


def _startup_seed_ce_wizard_binding():
    """NODE-BUILDER-ON-DAY-1-WIZARD-MODEL — CE: bind a default local model
    to the Node Wizard at install so the node builder works on day 1
    (owner 11-15: ON by default, no admin switch, no licence gate).

    Two idempotent writes, both CE-only (the `is_ce_edition()` gate keeps
    paid trees byte-identical: paid keeps its own managed-AI path and its
    original wording):
      * the Vault AI-config blob `platform/ai-config/wizard` — the path
        `utils/tool_config.read_tool_config` reads (the BYO shape:
        provider openai_compatible at the managed default host
        `http://ollama:11434`, the host alias the CORE/startup vLLM
        service already carries, and the balanced preset's served model);
      * the queryable `ai_slot_binding` row (slot=wizard, scope=platform,
        target_kind=external) so the binding table matches what the chip
        reads (models.py:11382; a later Settings save overwrites both).

    Skipped when an operator already stored a wizard config or a live
    wizard binding row (never overrides an admin's choice), and in SaaS
    mode. Fail-safe: any error is logged with the exception TYPE only —
    never the message, which could carry Vault material — and never
    raised, so boot always continues. The advisory transaction lock
    serializes the 3 uvicorn boot workers (same pattern as
    `_startup_seed_ce_admin_user`).
    """
    if _is_saas():
        logger.info("seed_ce_wizard_binding: SaaS mode — skipped")
        return
    from utils.paid_import import is_ce_edition
    if not is_ce_edition():
        logger.info("seed_ce_wizard_binding: paid tree — skipped")
        return

    from utils.vault_client import get_vault_token, read_kv_v2, write_kv_v2
    from database import SessionLocal
    from models import AiSlotBinding
    from sqlalchemy import text as _sa_text
    from sqlalchemy.exc import IntegrityError

    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee3  # noqa: E501
    base_url = "http://ollama:11434"
    model_name = "devstral-small-2"

    db = SessionLocal()
    try:
        db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": ADVISORY_LOCK_ID})

        blob: dict = {}
        token = None
        try:
            token = get_vault_token()
            if token:
                blob = read_kv_v2("platform/ai-config/wizard", token) or {}
        except Exception as exc:
            logger.warning(
                "seed_ce_wizard_binding: Vault read failed (non-fatal): %s",
                type(exc).__name__)

        existing = db.query(AiSlotBinding).filter(
            AiSlotBinding.slot == "wizard",
            AiSlotBinding.scope == "platform",
            AiSlotBinding.enabled.is_(True),
        ).first()
        if blob or existing:
            logger.info("seed_ce_wizard_binding: wizard already configured — skip")
            return

        try:
            db.add(AiSlotBinding(
                slot="wizard", scope="platform", target_kind="external",
                base_url=base_url, model=model_name,
                capability="chat", priority=0, enabled=True,
            ))
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("seed_ce_wizard_binding: another boot worker seeded "
                        "the row — skip")
            return

        try:
            if token:
                write_kv_v2("platform/ai-config/wizard", token, {
                    "execution_mode": "self_hosted",
                    "provider": "openai_compatible",
                    "base_url": base_url,
                    "model": model_name,
                })
                logger.info("seed_ce_wizard_binding: default model %s at %s "
                            "bound to the Node Wizard", model_name, base_url)
            else:
                logger.warning(
                    "seed_ce_wizard_binding: no Vault token — binding row "
                    "seeded, Vault blob skipped (non-fatal)")
        except Exception as exc:
            logger.warning("seed_ce_wizard_binding: Vault write failed "
                           "(non-fatal): %s", type(exc).__name__)
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


def _startup_seed_embedded_name_only_gate():
    """GSEC R-031 — keep the live install's name-only embedded-auth window
    alive across API restarts; fresh installs keep the credential-only default.

    The registry default for ``worker.embedded_requires_secret`` is True (new
    installs: the embedded worker is credential-gated from day one). This
    install predates the embedded bootstrap token, so its embedded worker
    binary may not yet carry it: to avoid the manager losing its worker the
    moment this code deploys, we SEED a platform row of False ONLY for an
    ESTABLISHED install — i.e. one where the embedded worker has already
    heartbeated (a LIVE worker_licenses row for ``mod-worker-embedded`` exists
    — BUG-1029b: tombstoned rows from a soft-deleted worker don't count)
    AND no admin has ever stored a value for the key (``set_setting``
    soft-supersedes and writes an audit row, so an admin's True/False choice
    is never clobbered; a fresh install has neither condition true and keeps
    the credential-only default). The lead flips it to True (admin settings
    UI, or ``PATCH /v1/platform-settings/worker.embedded_requires_secret``
    with ``{"value": true}``) once the embedded worker is verified sending
    its Vault token — after which this startup step is a no-op for the life
    of the install. Every name-only acceptance while the gate is off is
    logged + audit-written by routers.worker_tasks.require_enrolled_worker.
    Fail-safe: any error is logged and swallowed — this step must never block
    startup."""
    try:
        from database import SessionLocal
        from models import PlatformSetting, WorkerLicense
        from services import platform_settings as _psr_r031
        db = SessionLocal()
        try:
            ever_stored = db.query(PlatformSetting.key).filter(
                PlatformSetting.key == "worker.embedded_requires_secret",
            ).first() is not None
            if ever_stored:
                return  # admin (or a prior run) owns this key
            established = db.query(WorkerLicense.id).filter(
                WorkerLicense.deleted_at.is_(None),
                WorkerLicense.worker_name == "mod-worker-embedded",
            ).first() is not None
            if not established:
                return  # fresh install — keep the credential-only default
            _psr_r031.set_setting(
                "worker.embedded_requires_secret",
                False,
                db=db,
                actor_user_id=None,
            )
            db.commit()
            logger.info(
                "GSEC R-031: seeded worker.embedded_requires_secret=False for "
                "the live install (name-only embedded auth stays accepted + "
                "audited until an admin flips the setting to True)")
        finally:
            db.close()
    except Exception as exc:  # fail-safe — never crash startup
        logger.warning(
            "GSEC R-031 embedded name-only gate seed skipped: %s", exc)


def _startup_worker_discovery():
    """Start background worker discovery when API starts"""

    # Start discovery thread
    discovery_thread = threading.Thread(
        target=discover_workers_background,
        daemon=True,
        name="WorkerDiscovery"
    )
    discovery_thread.start()

    logger.info("Worker discovery background task started")
    logger.info("   → Discovering workers every minute")
    logger.info("   → Unlicensed workers will be paused automatically")


def _startup_verify_email_sync():
    """Keep realm verifyEmail in sync with whether platform email is configured.

    Bootstrap catch-22: a fresh deploy has no SMTP until the FIRST admin signs
    in and configures it on the Integrations page — but if verifyEmail is true,
    Keycloak blocks that first admin at registration (no inbox to verify), and
    they're the one who sets up SMTP. So: no email configured -> verifyEmail
    false (the first admin bootstraps; they become a platform admin and bypass
    the app's email gate anyway). Email configured -> verifyEmail true. Re-runs
    every boot so a Keycloak recreate can't leave the flag stuck either way.
    """
    try:
        from routers.admin_email_config import (
            _get_or_create_platform_email_row, _read_email_creds_from_vault,
        )
        from keycloak_auth import keycloak_admin_client
        with SessionLocal() as db:
            row = _get_or_create_platform_email_row(db)
            vault_data = _read_email_creds_from_vault() if row.password_vault_path else {}
            password_set = bool(vault_data.get("smtp_password")) or (
                bool(row.encrypted_smtp_password) and not row.password_vault_path
            )
            is_configured = bool(
                row.smtp_host and row.smtp_user and row.sender_email and password_set
            )
        ok, status_code, detail = keycloak_admin_client.set_verify_email(is_configured)
        if ok:
            logger.info(
                "Realm verifyEmail synced to %s (platform email configured=%s)",
                is_configured, is_configured,
            )
        else:
            logger.warning("verifyEmail sync skipped (Keycloak %s): %s", status_code, detail)
    except Exception as e:
        logger.warning("verify-email sync startup task failed (non-fatal): %s", e)


def _startup_push_realm_session_tier():
    """FX.54 p3e — converge the realm's session-tier + remember-me fields at boot.

    The settings-change seam (platform_settings -> session_tier_enforcement.converge)
    is inert while Keycloak is down, so a tier/remember-me edit made then would
    otherwise only heal on the next Keycloak start (keycloak-init.sh). Re-running
    the same idempotent partial-PUT push here on every API boot means the next
    API start heals it regardless. No-op when the Keycloak admin client is not
    configured (no admin service secret); never raises — any failure is a logged
    warning and startup continues.
    """
    try:
        from keycloak_auth import keycloak_admin_client
        if not getattr(keycloak_admin_client, "client_secret", None):
            logger.info("Realm session-tier push skipped — Keycloak admin client "
                        "not configured")
            return
        db = SessionLocal()
        try:
            result = keycloak_admin_client.push_realm_session_tier(db)
        finally:
            db.close()
        if result.get("changed"):
            logger.info("Realm session-tier push at boot: %s", result.get("note"))
        elif result.get("applied"):
            logger.info("Realm session-tier already converged at boot")
        else:
            logger.warning("Realm session-tier push at boot did not apply: %s",
                           result.get("note"))
    except Exception as e:
        logger.warning("realm session-tier push startup task failed (non-fatal): %s", e)


# Global scheduler instance
_overlap_scheduler = None

def get_overlap_scheduler():
    """Get the global OverlapScheduler instance."""
    return _overlap_scheduler


def _startup_scheduler_runtime_cache():
    """Load the scheduler-runtime-config cache before any scheduler code
    runs. Falls back through env vars + in-code defaults if the
    `scheduler_runtime_config` table is empty (e.g. fresh deploy).
    """
    try:
        from services import scheduler_runtime_cache
        from database import SessionLocal
        with SessionLocal() as db:
            snapshot = scheduler_runtime_cache.refresh(db)
        logger.info(
            "scheduler_runtime_cache loaded: capacity_floor=%d packer_full_at=%d "
            "variance_penalty_s=%d preset=%s source=%s",
            snapshot.capacity_floor_pct,
            snapshot.packer_full_above_util_pct,
            snapshot.variance_penalty_s,
            snapshot.preset_name,
            snapshot.source,
        )
    except Exception as e:
        logger.warning("Failed to load scheduler_runtime_cache: %s", e)


def _startup_ml_baseline_seed():
    """scheduler-ml-pipeline-hardening §2 — seed the baked-in scheduler
    ONNX baseline into MinIO platform-models/ if the bucket is empty.
    Idempotent; no-op once any scheduler model exists.
    """
    try:
        from services.ml_baseline import seed_baseline_if_empty
        with SessionLocal() as db:
            outcome = seed_baseline_if_empty(db)
        logger.info("ml_baseline.seed: %s", outcome)
    except Exception as e:
        # seed_baseline_if_empty already swallows internal exceptions; this
        # outer guard catches anything truly unexpected (import errors,
        # SessionLocal issues) so a broken baseline never blocks startup.
        logger.warning("ml_baseline.seed bootstrap failed: %s", e)


def _startup_overlap_scheduler():
    """Start the predictive overlap scheduler background loop."""
    global _overlap_scheduler
    try:
        from scheduler.overlap_scheduler import OverlapScheduler
        from database import SessionLocal
        _overlap_scheduler = OverlapScheduler(db_factory=SessionLocal)
        _overlap_scheduler.start_background_loop()
    except Exception as e:
        logger.warning("Failed to start overlap scheduler: %s", e)


def _startup_job_scheduler():
    """Start the APScheduler for periodic maintenance jobs.

    Also starts the leader-lock watchdog (δ §20.4) in every uvicorn worker,
    so if the leader process's scheduler dies silently and its lock expires
    without re-acquisition, another worker will pick it up within ~30s.
    """
    try:
        from services.scheduler import start_scheduler, start_scheduler_watchdog
        start_scheduler()
        start_scheduler_watchdog()
    except Exception as e:
        logger.warning("Failed to start job scheduler: %s", e)

    # GX.A2 — app hostname route reconcile (hourly DB→caddy-front sweep +
    # external-target SSRF re-fence). Only the scheduler-leader worker runs
    # it; per-change reconciles are fired from the apps router on every
    # worker (fire-and-forget, idempotent full-replace).
    try:
        from services import app_routing as _app_routing
        _app_routing.start_reconcile_job()
        # FX.124 PART 3 (Q111=A) — TLS floor boot window: the front Caddy's
        # base is rendered by a shell entrypoint that cannot read the
        # tls.min_version setting, so a gov install that reboots runs TLS 1.2
        # until the first hourly tick. Fire one reconcile NOW (fire-and-forget,
        # idempotent full-replace; fail-closed if the base file is not yet
        # published, in which case the hourly tick retries) so the platform
        # sites pick up the locked "1.3" floor immediately after boot.
        _app_routing.trigger_reconcile_async()
    except Exception as e:
        logger.warning("Failed to start app-route reconcile job: %s", e)


def _startup_db_row_trigger_listener():
    """add-workflow-triggers §6.3 — start the DB-row LISTEN/NOTIFY trigger listener
    (opt-in via TRIGGER_DB_ROW_LISTENER; inert by default)."""
    try:
        from services.triggers.db_events import start_db_row_listener
        start_db_row_listener()
    except Exception as e:
        logger.warning("Failed to start db-row trigger listener: %s", e)


def _startup_message_bus_subscribers():
    """add-workflow-triggers §8.2 — start MESSAGE_BUS subscribers (opt-in via
    TRIGGER_STREAMING; Redis works, other providers degraded if client absent)."""
    try:
        from services.triggers.streaming import start_message_bus_subscribers
        start_message_bus_subscribers()
    except Exception as e:
        logger.warning("Failed to start message-bus subscribers: %s", e)


_AUTONOMOUS_OPERATOR_SUBSCRIBER = None


def _startup_autonomous_operator_subscriber():
    """wsip 7.1 (event half) — start the OperatorEventSubscriber at API startup.

    This is the in-process event-bus listener that reacts to drift /
    adversary-critical degradation events the moment they are published,
    complementing the 15-minute APScheduler poll half.
    """
    global _AUTONOMOUS_OPERATOR_SUBSCRIBER
    if _AUTONOMOUS_OPERATOR_SUBSCRIBER is not None:
        return
    try:
        from database import SessionLocal
        from services.autonomous_operator_subscriber import OperatorEventSubscriber
        from services.autonomous_pipeline_adapter import make_pipeline_factory
        _db_factory = SessionLocal
        _pipeline_factory = make_pipeline_factory(_db_factory)
        _AUTONOMOUS_OPERATOR_SUBSCRIBER = OperatorEventSubscriber(
            db_factory=_db_factory,
            pipeline_factory=_pipeline_factory,
        )
        _AUTONOMOUS_OPERATOR_SUBSCRIBER.start()
    except Exception as e:
        logger.warning("Failed to start autonomous operator subscriber: %s",
                       type(e).__name__)


def _startup_mesh_gossip_loop():
    """add-multi-region-mesh §3.2 — start the periodic per-manager gossip loop.

    Each manager process starts it; a per-MANAGER Redis lock keeps exactly one
    loop running per manager (not per uvicorn worker), while distinct managers
    each gossip independently. The loop heartbeats peers, marks silent ones
    stale, and auto-fails-over (promotes) a dead peer's work to the survivor.
    """
    try:
        from utils.paid_import import optional_paid
        _mgr_mod = optional_paid("routers.managers")
        if _mgr_mod is None:
            logger.info("routers.managers not in this edition; skipped")
            return
        start_gossip_loop = _mgr_mod.start_gossip_loop
        start_gossip_loop()
    except Exception as e:
        logger.warning("Failed to start mesh gossip loop: %s", e)

    # workflow-agentic-execution-loop §10 — eagerly import the agentic
    # loop metrics module so its Prometheus families register at
    # uvicorn worker startup (before any wizard request arrives).
    # Without this the /metrics endpoint omits them until the first
    # wizard turn fires an emit, which makes empty-state dashboards
    # show "metric not found".
    try:
        from services import wizard_metrics  # noqa: F401
    except Exception as e:
        logger.warning("Failed to register wizard agentic-loop metrics: %s", e)


def _startup_system_maintenance():
    """
    Dispatch all platform maintenance tasks to a background thread.
    Each task is tracked in ApiTaskQueue as a system task (workflow_run_id=NULL)
    so it appears in the System Tasks tab with logs.
    """
    def _run_maintenance():
        import time
        time.sleep(3)  # Let API finish startup first

        from services.system_tasks import create_system_task_record, run_system_task
        from datetime import datetime, timezone

        task_list = [
            ("account_settings", "seed_account_settings", "Initialize default account settings"),
            # ("pricing_plans", ...) — removed in launch-day-followup-2
            # #15. Plans are admin-managed via Dashboard → Admin → Plans.
            ("capabilities", "seed_capabilities", "Seed RBAC permission capabilities"),
            ("converter_nodes", "seed_converter_nodes", "Register converter container nodes"),
            ("vector_nodes", "seed_vector_nodes", "Register RAG vector nodes"),
            ("export_pipeline_nodes", "seed_export_pipeline_nodes", "Register RAG/LoRA Export pipeline nodes"),
            ("mobile_builder_node", "seed_mobile_builder", "Register the mobile app builder node"),
            ("switchyard_llm_node", "seed_switchyard_llm_node", "Register the Switchyard LLM (BYO OpenAI-compatible endpoint) node"),
            ("stack_model_call_node", "seed_stack_model_call_node", "Register the Stack Model Call (image cogs -> the tenant's own model stack via /v1/llm) node"),
            ("brand_generator_node", "seed_brand_generator_node", "Register the Brand Asset Generate node"),
            ("node_metadata", "scan_node_metadata", "Scan metadata files and register node definitions"),
            ("nim_catalog", "sync_nim_catalog", "Sync NIM model catalog"),
        ]

        # 2400CR1zm — orphan node seeders, round 2 (see the manifest deny block at
        # ~1345 and open-core/tests/test_ce_seeded_images_vettable.py, which proves
        # no other ALLOWed seeder names an image outside vet-container.sh VETTABLE).
        # seed_vector_nodes (vectorupsert/vectorquery) and seed_brand_generator_node
        # (brand-asset-generate) have NO CE build, so manifest.yaml denies those
        # modules; drop their dispatch entries here with a quiet INFO skip — never a
        # "System task ... failed" warning or a FAILED system-task row. The dispatch
        # is by NAME (the import itself lives in services/system_tasks.py, which this
        # brief does not touch), so gating the task_list is sufficient and CE-safe.
        # seed_rag_pipeline_nodes (ragbuilder/ragquery) has no main.py caller at all
        # — it is only ever run as a standalone script — so there is nothing to gate.
        from utils.paid_import import optional_paid
        _ce_orphan_seeders = ("seed_vector_nodes", "seed_brand_generator_node", "seed_export_pipeline_nodes")
        task_list = [
            entry for entry in task_list
            if entry[1] not in _ce_orphan_seeders
            or optional_paid(entry[1]) is not None
        ]
        for _skipped in _ce_orphan_seeders:
            if not any(e[1] == _skipped for e in task_list):
                logger.info(
                    "%s: module not in this edition (CE); skipping the seed", _skipped
                )

        db = SessionLocal()
        try:
            # Clear rows left RUNNING by a process that died before finalizing
            # them. These have no worker to time them out — claimed_by_worker
            # is NULL by design because system tasks run in-process — so every
            # stale-task sweep aimed at the Go worker skips them, and they
            # accumulate a few per restart. Reaped BEFORE the run below so this
            # cycle's own records are never candidates.
            try:
                from services.system_tasks import reap_orphaned_system_tasks
                reap_orphaned_system_tasks(db)
            except Exception as e:  # noqa: BLE001
                logger.warning("orphaned system-task reap failed (non-fatal): %s", e)

            for task_name, task_func, description in task_list:
                try:
                    task = create_system_task_record(db, task_name, task_func, description)
                    result = run_system_task(db, task_func)

                    if result["error"]:
                        task.status = models.ApiTaskStatus.FAILED
                        task.error_message = result["error"][:500]
                    elif result["has_warnings"]:
                        task.status = models.ApiTaskStatus.COMPLETED
                        task.error_message = "Completed with warnings"
                    else:
                        task.status = models.ApiTaskStatus.COMPLETED

                    task.completed_at = datetime.now(timezone.utc)
                    task.result_payload = {"log_tail": result["log"] or ""}
                    db.commit()
                except Exception as e:
                    logger.warning(f"System task '{task_name}' failed: {e}")
                    try:
                        task.status = models.ApiTaskStatus.FAILED
                        task.error_message = str(e)[:500]
                        task.completed_at = datetime.now(timezone.utc)
                        db.commit()
                    except Exception:
                        db.rollback()
        finally:
            db.close()

        logger.info("All system maintenance tasks completed")

    maintenance_thread = threading.Thread(
        target=_run_maintenance, daemon=True, name="SystemMaintenance"
    )
    maintenance_thread.start()
    logger.info("System maintenance tasks dispatched to background thread")


# Legacy startup functions removed — now handled by run_system_maintenance_tasks above
# Keeping check_audit_hmac_key and sync_nim_catalog as they were already handled above


# REMOVED: seed_pricing_plans, seed_capabilities, seed_converter_nodes,
#           scan_dag_node_definitions — all run via system_tasks background thread now


def _startup_trigger_run_redispatch():
    """GO.6e — one-shot boot re-dispatch of stranded trigger-fired runs.

    A worker that died mid-dispatch (uvicorn recycle between the QUEUED
    commit and the dispatch thread running) leaves runs QUEUED with zero
    NodeRuns/tasks. The 5-minute scheduler job covers steady state; this
    boot pass covers "the API restarted, so the stranded runs have been
    sitting since the previous process died". Non-blocking (daemon
    thread, staggered past boot), exception-contained, and self-gated by
    the orchestration.trigger_run_redispatch_enabled platform setting."""
    import threading
    import time

    def _run():
        time.sleep(3)  # let migrations/seeds settle, like system_maintenance
        from database import SessionLocal
        db = SessionLocal()
        try:
            from services.workflow_orchestrator import (
                sweep_stranded_trigger_runs,
            )
            result = sweep_stranded_trigger_runs(db)
            if result.get("re_dispatched"):
                logger.info("Boot trigger-run re-dispatch: %s", result)
        except Exception as e:  # noqa: BLE001 — never crash startup
            logger.error("Boot trigger-run re-dispatch failed: %s", e)
        finally:
            db.close()

    threading.Thread(target=_run, name="trigger-run-redispatch-boot",
                     daemon=True).start()


def _startup_audit_reserved_secret_collisions():
    """LDF-4 §4.4 — flag tenant secrets that collide with the
    platform-reserved registry.

    The §4.1-§4.3 gate stops NEW collisions from being created, but
    pre-existing tenant secrets named e.g. SMTP_PASSWORD don't get
    rejected — they live on. This task scans every tenant secret on
    API startup, matches case-insensitively against the registry,
    and emits a `RESERVED_SECRET_COLLISION_DETECTED` audit event
    per collision so platform admins can see what to rename.

    Idempotent — re-scanning is harmless. The audit event carries
    the tenant_id + secret_id + matched name, so the operator can
    correlate to the offending row.
    """
    from models import Secret, AuditEvent
    from validators.platform_secret_names import is_reserved_secret_name

    db = SessionLocal()
    try:
        # Soft-deleted secrets are no longer effective — skip.
        rows = db.query(Secret).filter(Secret.deleted_at.is_(None)).all()
        collisions = []
        for r in rows:
            if r.name and is_reserved_secret_name(r.name):
                collisions.append(r)
        if not collisions:
            logger.info("Reserved-secret collision sweep: no collisions in %d tenant secrets", len(rows))
            return
        for r in collisions:
            try:
                db.add(AuditEvent(
                    tenant_id=r.tenant_id,
                    user_id=None,
                    event_name="RESERVED_SECRET_COLLISION_DETECTED",
                    details={
                        "secret_id": r.id,
                        "secret_name": r.name,
                        "matched_reserved_name": r.name.upper(),
                        "remediation": (
                            "This tenant-managed secret collides with a platform-reserved "
                            "name. The platform admin should rename or delete this secret to "
                            "avoid shadowing the platform's value."
                        ),
                    },
                ))
            except Exception as e:
                logger.warning("Audit emit for collision (tenant=%s name=%s) failed: %s",
                               r.tenant_id, r.name, e)
        db.commit()
        logger.warning(
            "Reserved-secret collision sweep: flagged %d collision(s) — see RESERVED_SECRET_COLLISION_DETECTED audit events",
            len(collisions),
        )
    except Exception as e:
        logger.warning("Reserved-secret collision sweep failed (non-fatal): %s", e)
    finally:
        db.close()


def _startup_ensure_default_tenant():
    """WS2.1 (launch-completion-megaplan, decomp/ws2-ce.md) — CE editions are
    single-tenant: every signup belongs to the DEFAULT_TENANT_ID tenant.

    Ensures that tenants row exists at startup (idempotent, soft: never
    modifies or deletes an existing row). No-op outside the CE carve —
    SaaS and licensed on-prem keep their per-customer tenants, and their
    signup path is untouched.
    """
    from services.node_scanner import ensure_default_tenant

    db = SessionLocal()
    try:
        tid = ensure_default_tenant(db)
        if tid is not None:
            logger.info(f"CE default tenant ready (id={tid})")
    except Exception as e:
        logger.error(f"ensure_default_tenant failed: {e}")
        db.rollback()
    finally:
        db.close()


def _startup_ensure_free_tier_plan():
    """LDF-4 §1 fix for #8 registration loop.

    Scope: only the **non-platform-admin** signup path needs this.
    The platform admin (the first human to sign up, elevated via
    `_has_human_platform_admin`) is routed into the existing platform
    tenant (id=1) and never calls `crud.create_tenant_and_parent_user`
    — they don't need a Free plan to register. Every OTHER first-time
    signup creates a new
    tenant which DOES need a Free-tier Plan to attach a subscription
    to; without one the create-tenant flow aborts with a 500 +
    leaks tenant/user rows that block retry with "user already
    registered" 400s.

    This task ensures EXACTLY ONE Free-tier Plan exists at startup.
    Other plans stay admin-managed via Dashboard → Admin → Plans
    (LDF-2 #15 intent preserved). Idempotent: a no-op when any
    `is_free_tier=True` plan already exists.

    1796 — the check-then-insert is now serialized by a Postgres advisory
    transaction lock (same pattern as `_startup_seed_ce_admin_user` /
    `_startup_seed_web_search_workflow`), so the 3-8 uvicorn boot workers can
    no longer race each other into a `plans_name_key` UniqueViolation.

    1900Y3a — the CE boot now stamps Free's `tier` column to `'community'`
    (the CE capability bundle) so `entitlements_from_plan` resolves the
    community floor correctly on a fresh DB where the migration never ran.
    Self-heals an existing Free plan whose `tier IS NULL` and writes an
    append-only ledger row to `plan_tier_assignments` (idempotent: skip if
    a row for this plan_id already exists).
    """
    from models import Plan

    # Stable identifier for the advisory lock. Any unique int works; the value
    # is just a key into a Postgres-wide namespace (bee0/bee1 are the workflow
    # seeds, bee2 the CE admin user, bee3 this one).
    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee3  # noqa: E501

    # GT.D12 / 1900Y3a — canonical Free tier mapping (mirrors migration)
    _FREE_PLAN_TIER = "community"

    db = SessionLocal()
    try:
        from sqlalchemy import text as _sa_text
        from sqlalchemy.exc import IntegrityError

        # Held until COMMIT / ROLLBACK on this session — serializes the boot
        # workers so only one of them sees "no existing free-tier plan".
        db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": ADVISORY_LOCK_ID})

        existing = db.query(Plan).filter(Plan.is_free_tier == True).first()
        if existing is not None:
            logger.info(f"Free-tier plan already present (id={existing.id}, name={existing.name!r})")
            # Self-heal: stamp tier if NULL, write ledger once (1900Y3a).
            _self_heal_free_tier(db, existing.id, existing.name, _FREE_PLAN_TIER)
            return
        # Community Edition has no quantity limits: every plan column that
        # could cap seats, storage, concurrent runs or credits is unlimited.
        free_plan = Plan(
            name="Free",
            price_in_cents=0,
            credits_per_month=1_000_000_000_000_000,
            max_seats=1_000_000_000_000_000,
            max_storage_gb=1_000_000_000_000_000,
            max_concurrent_processes=1_000_000_000_000_000,
            # 60/min (1/sec) is unusable for real UI bulk actions —
            # marketplace toggles, tenant-admin set-up flows etc.
            # torch it instantly. 300/min (5/sec) is the floor for a
            # browseable Free tier. Stays in lockstep with
            # seed_launch_pricing.py's Free row.
            api_rate_limit_per_minute=300,
            overage_credit_price_cents=None,
            is_free_tier=True,
            display_order=1,
            is_visible=True,
            # GT.D12 / 1900Y3a — the CE capability bundle floor.
            tier=_FREE_PLAN_TIER,
        )
        db.add(free_plan)
        try:
            db.commit()
        except IntegrityError:
            # The advisory lock normally prevents this; if a non-boot writer
            # (Dashboard → Plans, seed_launch_pricing.py) beat us to the same
            # name, roll back and treat the existing plan as good enough.
            db.rollback()
            logger.info("Free-tier plan already present (lost insert race) — skip")
            return
        logger.info("Seeded Free-tier plan so first-signup can complete (LDF-4 #8)")
        # 1900Y3a — ledger row for the create path.
        _write_tier_ledger(db, free_plan.id, "Free", None, _FREE_PLAN_TIER)
    except Exception as e:
        logger.error(f"ensure_free_tier_plan failed: {e}")
        db.rollback()
    finally:
        db.close()


def _self_heal_free_tier(db, plan_id, plan_name, tier):
    """Stamp NULL tier → `tier` and write one ledger row (1900Y3a).

    Idempotent: if the plan already has a tier, nothing happens.
    The ledger write skips if a row for this plan_id already exists.
    """
    from sqlalchemy import text as _sa_text

    # Check if tier is already set (re-read from DB to be safe).
    cur_tier = db.execute(
        _sa_text("SELECT tier FROM plans WHERE id = :pid AND deleted_at IS NULL"),
        {"pid": plan_id},
    ).scalar_one_or_none()

    if cur_tier is not None:
        logger.info(f"Free-tier plan (id={plan_id}) tier already {cur_tier!r} — no heal needed")
        return

    logger.info(f"Healing Free-tier plan (id={plan_id}) tier NULL → {tier!r}")
    db.execute(
        _sa_text("UPDATE plans SET tier = :t WHERE id = :pid AND tier IS NULL AND deleted_at IS NULL"),
        {"t": tier, "pid": plan_id},
    )
    db.commit()
    # Ledger write (idempotent via check).
    _write_tier_ledger(db, plan_id, plan_name, None, tier)


def _ensure_tier_ledger_table(db):
    """CREATE TABLE IF NOT EXISTS plan_tier_assignments (idempotent)."""
    from sqlalchemy import text as _sa_text
    db.execute(_sa_text("""
        CREATE TABLE IF NOT EXISTS plan_tier_assignments (
            id     SERIAL PRIMARY KEY,
            plan_id INTEGER NOT NULL,
            plan_name VARCHAR(100) NOT NULL,
            old_tier VARCHAR(20),
            new_tier VARCHAR(20) NOT NULL,
            applied_by VARCHAR(255) NOT NULL DEFAULT 'ce-boot',
            applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
        )
    """))
    db.commit()


def _write_tier_ledger(db, plan_id, plan_name, old_tier, new_tier):
    """Append one row to plan_tier_assignments if none exists for plan_id."""
    from sqlalchemy import text as _sa_text

    _ensure_tier_ledger_table(db)

    # Check for existing ledger row for this plan.
    exists = db.execute(
        _sa_text("SELECT 1 FROM plan_tier_assignments WHERE plan_id = :pid LIMIT 1"),
        {"pid": plan_id},
    ).scalar_one_or_none()
    if exists is not None:
        return

    db.execute(
        _sa_text(
            "INSERT INTO plan_tier_assignments "
            "(plan_id, plan_name, old_tier, new_tier, applied_by) "
            "VALUES (:pid, :pn, :ot, :nt, 'ce-boot')"),
        {"pid": plan_id, "pn": plan_name, "ot": old_tier, "nt": new_tier},
    )
    db.commit()
    logger.info(f"Ledger: plan_tier_assignments(plan_id={plan_id}, "
                f"old_tier={old_tier}, new_tier={new_tier!r})")


def _startup_seed_capabilities():
    """Seed the capabilities table with all available permissions on startup.

    Then re-heal system-role capabilities. The role bootstrap in
    database.initialize() runs BEFORE this task, so on a fresh DB the system
    roles (Admin / Standard User / …) are created while the Capability catalog
    is still empty — Admin's "all capabilities" resolves to ZERO and a tenant
    Admin can see nothing. Re-running ensure_tenant_system_roles here, now that
    the catalog exists, backfills those empty roles so the FIRST boot comes up
    correct (no second restart needed). Idempotent and only touches empty
    roles, so per-tenant customisations are preserved."""
    from seed_capabilities import seed_all_capabilities

    db = SessionLocal()
    try:
        created, updated = seed_all_capabilities(db)
        if created > 0:
            logger.info(f"Seeded {created} new capabilities")
        if updated > 0:
            logger.info(f"Updated {updated} existing capabilities")
        if created == 0 and updated == 0:
            logger.info("All capabilities already seeded")

        # Backfill any system role left empty by the earlier bootstrap.
        try:
            import models
            from crud.tenant_cud import ensure_tenant_system_roles
            healed = 0
            for tenant in db.query(models.Tenant).all():
                ensure_tenant_system_roles(db, tenant.id)
                healed += 1
            logger.info(f"Re-synced system roles for {healed} tenant(s) after capability seed")
        except Exception as e:
            logger.warning(f"Post-seed system-role heal failed: {e}")
    except Exception as e:
        logger.warning(f"Could not seed capabilities: {e}")
    finally:
        db.close()


def _startup_seed_human_approval_node():
    """Register the Gate 5 approval gate as a selectable node.

    Seeded rather than hand-created so `start-main-server.sh` — which drops
    every named volume — cannot take it away and leave the canvas entry
    pointing at a nodeDefinitionId that no longer exists.
    """
    from seed_human_approval_node import seed_human_approval_node as _seed
    db = SessionLocal()
    try:
        created = _seed(db)
        if created > 0:
            logger.info("Seeded the Human Approval node definition")
        else:
            logger.info("Human Approval node already seeded")
    except Exception as e:
        logger.warning(f"Could not seed the Human Approval node: {e}")
    finally:
        db.close()


def _startup_seed_text_llm_node():
    """Seed the BYO-LLM Text LLM node (tenant-shared, tenant_id=1).

    Idempotent: seed_text_llm_node returns 0 when the row already exists.
    FX.162.p1 (launch-completion-megaplan): this node was only seedable via
    the standalone __main__ path — wire it into the startup seed sequence
    so a fresh CE install gets it.
    """
    from seed_text_llm_node import seed_text_llm_node as _seed
    db = SessionLocal()
    try:
        created = _seed(db)
        if created > 0:
            logger.info("Seeded the Text LLM node definition")
        else:
            logger.info("Text LLM node already seeded")
    except Exception as e:
        logger.warning(f"Could not seed the Text LLM node: {e}")
    finally:
        db.close()


def _startup_seed_marketplace_catalogue():
    """marketplace-catalogue — surface a curated set of real system nodes in the
    public marketplace and price a flagship, so Browse/Download/Try/Buy/paywall
    are reachable instead of a dark, empty catalogue. Idempotent."""
    # 1788 CE carve: seed_marketplace.py is paid (the paid marketplace
    # catalogue/flagship pricing seed). CE skips the seed entirely — nothing
    # else at startup depends on it, so the catalogue stays as seeded by the
    # CE node seeds.
    from utils.paid_import import optional_paid
    seed_marketplace = optional_paid("seed_marketplace")
    if seed_marketplace is None:
        return
    seed_marketplace_catalogue = seed_marketplace.seed_marketplace_catalogue
    db = SessionLocal()
    try:
        flagged, priced = seed_marketplace_catalogue(db)
        if flagged or priced:
            logger.info("Seeded marketplace catalogue: flagged=%d priced=%d", flagged, priced)
        else:
            logger.info("Marketplace catalogue already seeded")
    except Exception as e:
        logger.warning(f"Could not seed marketplace catalogue: {e}")
    finally:
        db.close()


def _startup_seed_workflow_templates():
    """1900Y3b — seed the curated WorkflowTemplate rows that the WB "Templates"
    gallery lists. `migrations/20260425_seed_workflow_templates.py` writes these
    on SaaS (where the `mod-migrate` service runs), but CE boot never migrates —
    a fresh CE DB has an empty gallery without this startup-side seed.

    Insert-only, idempotent: rows matched on (name, tenant_id IS NULL) are left
    untouched if already present. CE node gate: templates whose graph references
    a node CE never seeds are skipped. Never crashes startup.
    """
    from services.workflow_templates_service import seed_workflow_templates

    db = SessionLocal()
    try:
        n = seed_workflow_templates(db)
        if n:
            logger.info("seed_workflow_templates: seeded %d curated row(s)", n)
        else:
            logger.info("seed_workflow_templates: no new rows (already seeded)")
    except Exception as e:
        logger.warning(
            "seed_workflow_templates: seed failed (non-fatal): %s", e,
        )
    finally:
        db.close()


def _startup_starter_package_sync():
    """1795 (CE Gap O) — one-shot boot reconcile of the MOD Core starter package.

    The palette gate shows a system node only when it is curated AND the tenant
    holds a `tenant_node_entitlements` row; the only writers of those rows are
    the 15-min scheduler tick and the signup inline hook, neither of which runs
    for the CE admin (seed_ce_admin_user routes them into the existing tenant 1,
    so no subscription-creation path fires). On a fresh CE boot the table is
    therefore empty and every system node is hidden until the first tick.

    `sync_starter_package` is the existing self-heal (services/starter_package.py
    26.3): package membership + plan_grant entitlements + tenant node copies, for
    every subscribed tenant. It is idempotent and edition-neutral, so this is a
    no-op on a warm deploy and on SaaS it only front-runs the tick the tick would
    otherwise do. Never crashes startup.
    """
    from services.starter_package import sync_starter_package

    db = SessionLocal()
    try:
        summary = sync_starter_package(db)
        logger.info(
            "starter package boot sync: package_id=%s nodes_added=%d plans_bound=%d "
            "tenants=%d granted=%d copies=%d",
            summary.get("package_id"), summary.get("added_nodes", 0),
            summary.get("bound_plans", 0), summary.get("tenants", 0),
            summary.get("granted", 0), summary.get("copies_created", 0),
        )
    except Exception as e:
        logger.warning(f"starter package boot sync failed (non-fatal): {e}")
        db.rollback()
    finally:
        db.close()


def _startup_seed_converters():
    """Register converter containers as selectable workflow nodes."""
    from seed_converters import seed_converter_nodes as _seed
    db = SessionLocal()
    try:
        created = _seed(db)
        if created > 0:
            logger.info(f"Seeded {created} converter node definitions")
        else:
            logger.info("Converter nodes already seeded")
    except Exception as e:
        logger.warning(f"Could not seed converter nodes: {e}")
    finally:
        db.close()


def _startup_seed_domain_nodes():
    """B98 — register the workflow-productivity-floor domain primitives
    (node_definitions_domains: OCR, translation, DB CRUD, ...) as system
    NodeDefinitions. Idempotent upsert; one module failing never blocks the
    others (per-module SAVEPOINT); the registry leaves the commit to us.

    CE edition: pass the keeplist to the registry so only modules whose node
    names overlap the keeplist are registered (image_ops — 6 nodes).
    """
    from services import node_scanner
    from node_definitions_domains._registry import DOMAIN_REGISTRARS, register_all_domain_nodes

    keeplist_names: set | None = None
    if node_scanner._is_ce_edition():
        keeplist_names = node_scanner._load_ce_keeplist_names()
        if keeplist_names:
            logger.info(
                "seed_domain_nodes: CE edition — %d families "
                "filtered to keeplist overlap",
                len(DOMAIN_REGISTRARS),
            )

    db = SessionLocal()
    try:
        results = register_all_domain_nodes(db, keeplist_names=keeplist_names)
        db.commit()
        failed = sorted(k for k, v in results.items() if isinstance(v, str))
        logger.info("Domain nodes registered: %d modules, %d failed %s",
                    len(results), len(failed), failed or "")
    except Exception as e:
        db.rollback()
        logger.warning("Could not register domain nodes: %s", e.__class__.__name__)
    finally:
        db.close()


def _startup_seed_harbor_scan_followup_node():
    """harbor-scan-followup-node — seed the system NodeDefinition for
    the HarborScanFollowup container. Idempotent (upsert by name).

    The container itself carries no Harbor / Vault / DB credentials;
    the worker dispatch passes API_URL + API_KEY via env vars from
    the configuration_schema below, and the container POSTs to the
    /admin/harbor-followup/* internal endpoints to do the actual
    Harbor side effects."""
    # 2400SYS1 — this node's whole job is POSTing to /admin/harbor-followup/*,
    # whose router is CE-denied (open-core/manifest.yaml deny block, line 1340).
    # Reuse the EXISTING router-presence gate: `_mount_optional_routers`
    # (main.py:2135) mounts this very module via
    # `utils.paid_import.optional_paid("routers.admin_harbor_followup")`, which
    # returns None on the CE carve. Absent here -> seed nothing at all, so CE
    # never creates the node, the hourly workflow, or a FAILED system run.
    # Paid/on-prem behaviour is unchanged.
    from utils.paid_import import optional_paid
    if optional_paid("routers.admin_harbor_followup") is None:
        logger.info(
            "seed_harbor_scan_followup_node: /admin/harbor-followup router not "
            "in this edition (CE); skipping the node seed"
        )
        return
    from utils.registries import platform_image
    HARBOR_FOLLOWUP_IMAGE = os.getenv(
        "HARBOR_SCAN_FOLLOWUP_IMAGE",
        platform_image("harborscanfollowup:latest"),
    )
    NAME = "system-harbor-scan-followup"
    try:
        db = SessionLocal()
        try:
            existing = db.query(models.NodeDefinition).filter(
                models.NodeDefinition.name == NAME,
                models.NodeDefinition.tenant_id.is_(None),
            ).first()
            payload = dict(
                executable_path=HARBOR_FOLLOWUP_IMAGE,
                system_only=True,
                description=(
                    "Recovery for tenant-Harbor artifacts stuck in "
                    "Pending / Running scan status. Calls Harbor's "
                    "manual /scan endpoint via the platform API. "
                    "Hourly cron via system workflow; can also be "
                    "invoked ad-hoc with scope=tenant."
                ),
                node_kind="system_harbor_scan_followup",
            )
            if existing is not None:
                for k, v in payload.items():
                    setattr(existing, k, v)
            else:
                db.add(models.NodeDefinition(
                    name=NAME,
                    tenant_id=None,
                    type="System",
                    process_type="CPU",
                    cost_in_credits=0,
                    input_schema={},
                    output_schema={
                        "retriggered": "integer",
                        "skipped_fresh": "integer",
                        "errors": "array",
                    },
                    configuration_schema={
                        "extra_variables": {
                            # Default invocation — overridden in the
                            # workflow version's node-data when a
                            # specific scope/tenant is desired.
                            "SCOPE": "all-tenants",
                            "STUCK_AGE_MINUTES": "30",
                        },
                    },
                    batch_mode="single",
                    **payload,
                ))
            db.commit()
            logger.info("Seeded HarborScanFollowup system NodeDefinition")
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_harbor_scan_followup_node failed: %s", e)


def _startup_seed_harbor_scan_followup_workflow():
    """harbor-scan-followup-node — seed the hourly cron workflow that
    drives the HarborScanFollowup node.

    Idempotent: skipped if a workflow with the seed name already
    exists for the platform tenant. Wraps the insert in a Postgres
    advisory transaction lock keyed on a fixed integer so concurrent
    uvicorn workers serialize on the seed; without this two workers
    racing through the existence-check both insert and we get
    duplicate workflows. The recurring SCHEDULE trigger is picked up
    by the existing `_run_workflow_triggers` evaluator in
    services/scheduler.py — no new scheduler job needed.
    """
    # 2400SYS1 — same CE router-presence gate as the node seed above: the
    # workflow drives that node, so on CE there is nothing to drive and the
    # hourly cron would only log a FAILED system run every hour.
    from utils.paid_import import optional_paid
    if optional_paid("routers.admin_harbor_followup") is None:
        logger.info(
            "seed_harbor_scan_followup_workflow: /admin/harbor-followup router "
            "not in this edition (CE); skipping the workflow + cron seed"
        )
        # An install that seeded this BEFORE the gate existed still holds the
        # workflow + its hourly SCHEDULE trigger, so the sweep kept logging a
        # FAILED system run every hour. ISO: never delete — flip the same
        # `is_active` flag an operator toggles (the idempotence branch above
        # already treats a disabled trigger as the operator's own choice).
        try:
            db = SessionLocal()
            try:
                stale = db.query(
                    models.WorkflowTrigger, models.Workflow,
                ).filter(
                    models.Workflow.name
                    == "HarborScanFollowup hourly sweep",
                    models.Workflow.deleted_at.is_(None),
                    models.WorkflowTrigger.workflow_id
                    == models.Workflow.id,
                    models.WorkflowTrigger.trigger_type
                    == models.WorkflowTriggerType.SCHEDULE,
                    models.WorkflowTrigger.deleted_at.is_(None),
                    models.WorkflowTrigger.is_active.is_(True),
                ).all()
                for _trig, _wf in stale:
                    _trig.is_active = False
                    logger.warning(
                        "seed_harbor_scan_followup_workflow: CE edition — "
                        "deactivated SCHEDULE trigger for workflow %d "
                        "(harbor-followup router not in this edition)",
                        _wf.id,
                    )
                if stale:
                    db.commit()
            finally:
                db.close()
        except Exception as _ce_exc:  # noqa: BLE001
            logger.warning(
                "seed_harbor_scan_followup_workflow: CE trigger sweep skipped: %s",
                _ce_exc,
            )
        return
    # Stable identifier for the advisory lock. Any unique int works;
    # the value is just an int key into a Postgres-wide namespace.
    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee0  # noqa: E501
    WORKFLOW_NAME = "HarborScanFollowup hourly sweep"
    try:
        db = SessionLocal()
        try:
            from sqlalchemy import text as _sa_text
            # Held until COMMIT / ROLLBACK on this session.
            db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                       {"k": ADVISORY_LOCK_ID})
            # Resolve platform tenant (the first tenant, by convention).
            tenant = db.query(models.Tenant).order_by(models.Tenant.id.asc()).first()
            if tenant is None:
                logger.info(
                    "seed_harbor_scan_followup_workflow: no tenants yet; skipping",
                )
                return

            # Resolve the seed node we just registered.
            node_def = db.query(models.NodeDefinition).filter(
                models.NodeDefinition.name == "system-harbor-scan-followup",
                models.NodeDefinition.tenant_id.is_(None),
            ).first()
            if node_def is None:
                logger.warning(
                    "seed_harbor_scan_followup_workflow: NodeDefinition not seeded yet; "
                    "skipping (node seed runs first)",
                )
                return

            # Idempotent — skip if already present.
            existing = db.query(models.Workflow).filter(
                models.Workflow.tenant_id == tenant.id,
                models.Workflow.name == WORKFLOW_NAME,
                models.Workflow.deleted_at.is_(None),
            ).execution_options(include_deleted=True).first()
            if existing is not None:
                # Make sure the trigger is still active — operator may have
                # disabled it; we don't override that, just log.
                trig = db.query(models.WorkflowTrigger).filter(
                    models.WorkflowTrigger.workflow_id == existing.id,
                    models.WorkflowTrigger.trigger_type == models.WorkflowTriggerType.SCHEDULE,
                    models.WorkflowTrigger.deleted_at.is_(None),
                ).execution_options(include_deleted=True).first()
                if trig is None:
                    logger.warning(
                        "seed_harbor_scan_followup_workflow: workflow exists but no "
                        "SCHEDULE trigger; reseeding the trigger only",
                    )
                    db.add(models.WorkflowTrigger(
                        workflow_id=existing.id,
                        tenant_id=tenant.id,
                        trigger_type=models.WorkflowTriggerType.SCHEDULE,
                        configuration={"cron": "0 * * * *", "timezone": "UTC"},
                        is_active=True,
                    ))
                    db.commit()

                # Repair a graph seeded before the root-node fix.
                #
                # Idempotence is what made the original bug permanent: the
                # early return meant a deployment that seeded the broken graph
                # kept it forever, and the sweep failed hourly with nothing
                # ever revisiting it. Correcting the seed alone would fix only
                # deployments that had never run it — i.e. none of them.
                #
                # Narrow on purpose. It rewrites only when a ROOT carries no
                # node_kind, which is precisely the unrunnable shape and not a
                # thing an operator would author. is_system is asserted too, so
                # this can never touch a tenant's own workflow.
                try:
                    ver = db.query(models.WorkflowVersion).filter(
                        models.WorkflowVersion.id == existing.latest_version_id,
                    ).first()
                    graph = (ver.graph_json or {}) if ver else {}
                    nodes = graph.get("nodes") or []
                    if ver is not None and existing.is_system and nodes:
                        tgts = {e.get("target") for e in graph.get("edges") or []}
                        roots = [n for n in nodes if n.get("id") not in tgts]
                        broken = roots and not all(
                            (n.get("data") or {}).get("node_kind") for n in roots
                        )
                        if broken:
                            ver.graph_json = {
                                "nodes": [
                                    {"id": "process-1", "type": "process",
                                     "position": {"x": 360, "y": 120},
                                     "data": {
                                         "label": "Harbor scan followup",
                                         "nodeDefinitionName": "system-harbor-scan-followup",
                                         "nodeDefinitionId": node_def.id,
                                         "node_kind": node_def.node_kind,
                                         "extra_variables": {
                                             "SCOPE": "all-tenants",
                                             "STUCK_AGE_MINUTES": "30",
                                         },
                                     }},
                                ],
                                "edges": [],
                            }
                            db.commit()
                            logger.warning(
                                "seed_harbor_scan_followup_workflow: repaired "
                                "workflow %d — its root node carried no "
                                "node_kind, so every run failed with 'No root "
                                "nodes had a node_kind'", existing.id,
                            )
                except Exception as _rep:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "seed_harbor_scan_followup_workflow: graph repair "
                        "skipped: %s", _rep,
                    )
                return

            # Fresh insert: Workflow + WorkflowVersion + cron Trigger.
            workflow = models.Workflow(
                tenant_id=tenant.id,
                name=WORKFLOW_NAME,
                is_system=True,
            )
            db.add(workflow)
            db.flush()

            graph_json = {
                # One node, and it carries its own node_kind. Both of those
                # are load-bearing; the original graph got each wrong and the
                # sweep failed on every run from 2026-07-26 to 2026-08-17 —
                # 452 consecutive failures, and it never once succeeded.
                #
                # It was `input-1 (Tick) -> process-1 -> output-1`, which reads
                # like a workflow but is not one the orchestrator can start.
                # Roots are nodes with no incoming edge, so the root was the
                # decorative Tick; `enqueue` reads `data.node_kind`, found none,
                # skipped it, and with every root skipped the run failed before
                # reaching process-1. A trigger node is a drawing, not a step.
                #
                # process-1 would not have run even as the root. It identified
                # its definition by `nodeDefinitionName`, and nothing resolves
                # names at dispatch: `_enrich_from_def` keys on
                # `nodeDefinitionId`, returns node_data untouched when it is
                # absent, and the name is used only by the build-DAG submit
                # path. So the id is what makes enrichment work — and node_kind
                # is set explicitly anyway, so a single lookup failure cannot
                # silently take the sweep out again. The name stays for humans
                # reading the row.
                "nodes": [
                    {"id": "process-1", "type": "process",
                     "position": {"x": 360, "y": 120},
                     "data": {
                         "label": "Harbor scan followup",
                         "nodeDefinitionName": "system-harbor-scan-followup",
                         "nodeDefinitionId": node_def.id,
                         "node_kind": node_def.node_kind,
                         "extra_variables": {
                             "SCOPE": "all-tenants",
                             "STUCK_AGE_MINUTES": "30",
                         },
                     }},
                ],
                "edges": [],
            }
            version = models.WorkflowVersion(
                workflow_id=workflow.id,
                version_number=1,
                graph_json=graph_json,
            )
            db.add(version)
            db.flush()
            workflow.latest_version_id = version.id

            db.add(models.WorkflowTrigger(
                workflow_id=workflow.id,
                tenant_id=tenant.id,
                trigger_type=models.WorkflowTriggerType.SCHEDULE,
                configuration={"cron": "0 * * * *", "timezone": "UTC"},
                is_active=True,
            ))

            # BUG.B85c1 — the graph -> workflow_nodes sync. The orchestrator
            # only makes a NodeRun for a graph node that HAS a WorkflowNode
            # row, so a seeded workflow without them ran as a no-op on every
            # hourly trigger (and the FX.91 egress gate could not resolve the
            # node under cmmc). Same sync every other creation path runs.
            from crud import workflow_cud as _wfcud
            seeded_rows = _wfcud.sync_workflow_nodes(db, workflow, graph_json)
            db.commit()
            logger.info(
                "Seeded HarborScanFollowup hourly sweep workflow "
                "(workflow_id=%d) with cron '0 * * * *' and %d workflow_node "
                "row(s)", workflow.id, seeded_rows,
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_harbor_scan_followup_workflow failed: %s", e)


def _startup_seed_platform_system_workflows():
    """1615 — boot the Paid/SaaS platform system-op workflow seed.

    `scripts/seed_platform_system_workflows.py` was only ever run by hand, so a
    fresh Paid install never got the `Platform: Weekly Overage Accounting`
    is_system Workflow + SCHEDULE trigger that drives the paid job through the
    "workflow drives, API executes" path (scheduler._run_system_op_trigger ->
    services.system_ops -> a WorkflowRun history row). The paid job itself still
    worked via the legacy weekly APScheduler cron — this closes the
    observability gap, it does not replace that fallback.

    Edition gate: the script is denied in the CE carve (open-core/manifest.yaml)
    and the job is Paid/SaaS-only, so CE returns before any DB work and logs
    nothing — never a spurious warning. The script's own `optional_paid` guard
    stays as the second line of defence.

    Idempotent: the script skips a spec whose SCHEDULE trigger already exists
    (matched by `configuration.internal_action`), so a second boot is a no-op.
    The check-then-insert is serialized with a Postgres advisory lock on THIS
    session — the same pattern `_startup_seed_web_search_workflow` uses — so the
    3-8 uvicorn boot workers can't race into duplicate Workflow rows.

    Ownership needs a platform-admin `User` row; on a genuinely fresh install
    with zero users the script raises SystemExit, which this wrapper turns into
    a log line (the seed then happens on a later boot). Never crashes startup.
    """
    from utils.paid_import import is_paid_tree
    if not is_paid_tree():
        return
    # Stable identifier for the advisory lock (unique to this seed).
    ADVISORY_LOCK_ID = 0x5e115e00deadbee1  # noqa: E501
    from scripts.seed_platform_system_workflows import seed as seed_system_workflows
    from sqlalchemy import text as _sa_text

    db = SessionLocal()
    try:
        db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                   {"k": ADVISORY_LOCK_ID})
        seeded = seed_system_workflows(db)
        if seeded:
            logger.info("seed_platform_system_workflows: %s new system workflow(s)",
                        seeded)
    except SystemExit as e:  # no platform-admin user yet — re-seeds next boot
        db.rollback()
        logger.warning("seed_platform_system_workflows skipped: %s", e)
    except Exception as e:
        db.rollback()
        logger.warning("seed_platform_system_workflows failed: %s", e)
    finally:
        db.close()


def _startup_heal_system_workflow_nodes(db=None) -> int:
    """BUG.B85c1 — self-heal system workflows whose graph has nodes but no
    `workflow_nodes` rows.

    The rows are what the orchestrator joins on to make a NodeRun; without
    them the workflow runs as a no-op forever (HarborScanFollowup: 1 node in
    its version graph, 0 rows in `workflow_nodes`, so zero NodeRuns, zero
    tasks, and under compliance profile cmmc the FX.91 egress gate could not
    resolve the node at all). Seeders that predate the sync left that state
    permanently — idempotence means nothing revisits them.

    Idempotent and insert-only: `sync_workflow_nodes` find-or-creates per
    (workflow_id, graph_node_id) and never deletes or re-pins an existing row,
    so re-running heals nothing the second time. Scoped to `is_system` so an
    operator's own half-built canvas is never completed behind their back.

    `db` is injectable (tests pass the hermetic fixture session); the boot path
    calls it with no args and gets its own SessionLocal. Returns workflows
    healed.
    """
    from crud import workflow_cud as _wfcud
    own_db = db is None
    healed = 0
    try:
        if own_db:
            db = SessionLocal()
        workflows = db.query(models.Workflow).filter(
            models.Workflow.is_system.is_(True),
            models.Workflow.deleted_at.is_(None),
        ).all()
        for wf in workflows:
            ver = (db.query(models.WorkflowVersion)
                   .filter(models.WorkflowVersion.id == wf.latest_version_id)
                   .first()) if wf.latest_version_id else None
            graph = (ver.graph_json or {}) if ver else {}
            if not (isinstance(graph, dict) and (graph.get("nodes") or [])):
                continue
            if db.query(models.WorkflowNode).filter(
                models.WorkflowNode.workflow_id == wf.id,
            ).first() is not None:
                continue
            created = _wfcud.sync_workflow_nodes(db, wf, graph)
            if created:
                healed += 1
                logger.warning(
                    "heal_system_workflow_nodes: workflow %d (%s) had %d "
                    "graph node(s) and 0 workflow_nodes rows — materialized "
                    "%d row(s); its runs were no-ops until now",
                    wf.id, wf.name, len(graph.get("nodes") or []), created,
                )
        db.commit()
        if healed:
            logger.info(
                "heal_system_workflow_nodes: healed %d system workflow(s)",
                healed,
            )
        return healed
    except Exception as e:
        logger.warning("heal_system_workflow_nodes failed: %s", e)
        return healed
    finally:
        if own_db:
            db.close()


def _startup_seed_task_extract_node():
    """talk-to-task — idempotently seed the TaskExtract system node."""
    # 2400CR1zk — taskextractnode has no CE build (not in vet-container.sh VETTABLE),
    # so open-core/manifest.yaml denies seed_task_extract_node.py. Absent module ->
    # quiet INFO skip (same style as _startup_seed_harbor_scan_followup_node, 2400SYS1),
    # never a "seed_task_extract_node failed" warning or a FAILED system-task row.
    from utils.paid_import import optional_paid
    if optional_paid("seed_task_extract_node") is None:
        logger.info(
            "seed_task_extract_node: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_task_extract_node import seed_task_extract_node
        db = SessionLocal()
        try:
            node_id = seed_task_extract_node(db)
            if node_id:
                logger.info("Seeded TaskExtract system NodeDefinition (id=%s)", node_id)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_task_extract_node failed: %s", e)


def _startup_seed_task_merge_node():
    """talk-to-task — idempotently seed the TaskMerge system node."""
    # 2400CR1zk — taskmergenode has no CE build; module denied in manifest.yaml.
    from utils.paid_import import optional_paid
    if optional_paid("seed_task_merge_node") is None:
        logger.info(
            "seed_task_merge_node: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_task_merge_node import seed_task_merge_node
        db = SessionLocal()
        try:
            node_id = seed_task_merge_node(db)
            if node_id:
                logger.info("Seeded TaskMerge system NodeDefinition (id=%s)", node_id)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_task_merge_node failed: %s", e)


def _startup_seed_plane_node():
    """talk-to-task — idempotently seed the Plane system node."""
    # 2400CR1zk — planenode has no CE build; module denied in manifest.yaml.
    from utils.paid_import import optional_paid
    if optional_paid("seed_plane_node") is None:
        logger.info(
            "seed_plane_node: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_plane_node import seed_plane_node
        db = SessionLocal()
        try:
            node_id = seed_plane_node(db)
            if node_id:
                logger.info("Seeded Plane system NodeDefinition (id=%s)", node_id)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_plane_node failed: %s", e)


def _startup_seed_asr_nim_node():
    """Q-L-ASR — idempotently seed the speech-to-text (Riva ASR) NIM node.

    Runs BEFORE _startup_seed_talk_to_task_workflow so the definition exists
    ahead of any graph that might reference it. The node is registered inert
    (enabled=False) until the owner's one-time NGC image warm-up.
    """
    # 2400CR1zk — nimbridge has no CE build; module denied in manifest.yaml.
    from utils.paid_import import optional_paid
    if optional_paid("seed_asr_nim_node") is None:
        logger.info(
            "seed_asr_nim_node: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_asr_nim_node import seed_asr_nim_node
        db = SessionLocal()
        try:
            node_id = seed_asr_nim_node(db)
            if node_id:
                logger.info("Seeded ASR NIM system NodeDefinition (id=%s)", node_id)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_asr_nim_node failed: %s", e)


def _startup_seed_talk_to_task_workflow():
    """talk-to-task — seed the workflow graph that drives the task nodes.

    Runs AFTER the three node seeders above so the definitions it
    references already exist. Idempotent: the seeder skips when the
    workflow already exists for the platform tenant.
    """
    # 2400CR1zk — the graph points at taskextractnode/taskmergenode/planenode,
    # none of which CE builds, so the workflow seeder is denied too: CE must not
    # ship a workflow whose nodes are missing.
    from utils.paid_import import optional_paid
    if optional_paid("seed_talk_to_task_workflow") is None:
        logger.info(
            "seed_talk_to_task_workflow: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_talk_to_task_workflow import seed_talk_to_task_workflow
        db = SessionLocal()
        try:
            seed_talk_to_task_workflow(db, tenant_id=1)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_talk_to_task_workflow failed: %s", e)


def _startup_seed_talk_to_task_stack():
    """talk-to-task — publish the free "MOD Talk -> Plane" stack package.

    Runs AFTER the workflow seeder above so the graph it points at already
    exists. Idempotent: the seeder skips when the stack already exists for
    the platform tenant.
    """
    # 2400CR1zk — the stack packages the talk-to-task graph, whose nodes CE does
    # not build; denied alongside the node seeds so CE ships no stack over missing
    # nodes.
    from utils.paid_import import optional_paid
    if optional_paid("seed_talk_to_task_stack") is None:
        logger.info(
            "seed_talk_to_task_stack: module not in this edition (CE); skipping the seed"
        )
        return
    try:
        from seed_talk_to_task_stack import seed_talk_to_task_stack
        db = SessionLocal()
        try:
            seed_talk_to_task_stack(db, tenant_id=1)
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_talk_to_task_stack failed: %s", e)


def _startup_ensure_harbor_proxy_cache():
    """GF.C3 — idempotently ensure Harbor registry endpoints + non-public
    proxy-cache projects for docker.io/nvcr.io/ghcr.io exist on platform
    Harbor, so node/NIM image pulls can be routed through Harbor's cache.

    Best-effort: any failure (Harbor unreachable, no admin creds
    configured) is logged and never blocks the rest of startup — this is
    exactly the same posture as the other Harbor bootstrap seeders above.

    Single-flight across uvicorn workers via a Redis lock (same idiom as
    ``_startup_seed_nim_stacks``/``_startup_seed_nim_characterizations``
    above) so N worker processes don't each make the same Harbor calls
    and log the same warnings once per deployment, not once per worker.
    """
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        if not _r.set("mod:harbor_proxy_cache_seed:lock", "1", nx=True, ex=300):
            logger.info("ensure_harbor_proxy_cache: another worker holds the lock; skipping")
            return
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.warning("ensure_harbor_proxy_cache: lock unavailable (%s); proceeding", e)
    try:
        from services.harbor_proxy_cache import ensure_harbor_proxy_cache_projects
        db = SessionLocal()
        try:
            counters = ensure_harbor_proxy_cache_projects(db)
            logger.info("ensure_harbor_proxy_cache: %s", counters)
        finally:
            db.close()
    except Exception as e:
        logger.warning("ensure_harbor_proxy_cache failed: %s", e)


def _startup_seed_nim_stacks():
    """Register the on-demand local-NIM stacks (Studio Voice) as first-class
    PersistentStack rows so they show in the Stacks UI, resting asleep.

    Idempotent (upsert by name+tenant). Registration only — never deploys or
    contends for a GPU; the ephemeral orchestrator wakes/sleeps it per job. A
    fresh install gets the row automatically via this hook. Single-flight across
    uvicorn workers via a Redis lock so N processes don't race to insert the row
    (which otherwise leaves duplicates)."""
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        if not _r.set("mod:nim_stacks_seed:lock", "1", nx=True, ex=300):
            logger.info("NIM stacks seed: another worker holds the lock; skipping")
            return
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.warning("NIM stacks seed: lock unavailable (%s); proceeding", e)
    # CE carve (FIX-CE-CARVE-GUARD-NIM-EPHEMERAL-IMPORTS): services/nim_ephemeral.py
    # is a manifest `deny:` row (S35-safe apply 347128c905), so the bare import at
    # the old line 5911 raised ImportError inside a try whose only handler was the
    # `finally` — the seed task logged "skipped" but the import was never protected.
    # Route it through the repo's optional_paid seam: CE = no ephemeral NIM, so the
    # seed is a clean no-op (no error, no log spam); paid behaviour is unchanged when
    # the module exists.
    from utils.paid_import import optional_paid
    _nim_mod = optional_paid("services.nim_ephemeral")
    if _nim_mod is None:
        logger.info("NIM stacks seed: services.nim_ephemeral not in this edition; skipping")
        return
    try:
        db = SessionLocal()
        try:
            results = _nim_mod.apply(db)
            logger.info("NIM stacks seed: %s", results)
        finally:
            db.close()
    except Exception as e:
        logger.warning("NIM stacks seed skipped (%s)", e)


def _startup_seed_nim_characterizations():
    """stack-training-and-prewarm §2.1 — seed a StackProfile characterization for
    the already-known on-demand NIM stacks at startup.

    Characterization (measured VRAM / warm time) is part of the paid predictive
    layer: the work lives in `services.stack_calibration`, absent from the
    Community Edition tree, where this startup task is a no-op."""
    from utils.paid_import import optional_paid
    _sc = optional_paid("services.stack_calibration")
    if _sc is None:
        logger.info("stack characterization not in this edition")
        return
    _sc.seed_nim_characterizations(logger)


def _startup_seed_web_search_node():
    """add-web-search-node — seed the system NodeDefinition for the
    WebSearch container. Idempotent (upsert by name).

    The container carries no platform-DB credentials; the orchestrator
    injects API_URL + the platform-service key (as API_KEY) at dispatch,
    and the container POSTs its OUTPUT artifact back to the internal
    /admin/web-search/result endpoint. QUERY / TOP_K are threaded to the
    container as env via configuration_schema.extra_variables (overridden
    per-run in the workflow version's node-data)."""
    from utils.registries import platform_image
    WEB_SEARCH_IMAGE = os.getenv(
        "WEB_SEARCH_IMAGE",
        platform_image("websearch:latest"),
    )
    NAME = "system-web-search"
    try:
        db = SessionLocal()
        try:
            existing = db.query(models.NodeDefinition).filter(
                models.NodeDefinition.name == NAME,
                models.NodeDefinition.tenant_id.is_(None),
            ).first()
            payload = dict(
                executable_path=WEB_SEARCH_IMAGE,
                # node_role is authoritative; the @validates hook syncs
                # system_only=True from it (contract: node_role="system").
                node_role="system",
                description=(
                    "Keyless web search (DuckDuckGo) that mines results "
                    "for domain jargon, github.com repos and pypi.org "
                    "packages. Reuses the node wizard's web_expand "
                    "extraction. Invoked on demand by the node wizard / "
                    "orchestrator; returns its artifact to the platform API."
                ),
                node_kind="system_web_search",
            )
            if existing is not None:
                for k, v in payload.items():
                    setattr(existing, k, v)
            else:
                db.add(models.NodeDefinition(
                    name=NAME,
                    tenant_id=None,
                    type="System",
                    process_type="CPU",
                    cost_in_credits=0,
                    input_schema={
                        # Threaded to the container as env (see extra_variables).
                        "QUERY": "string",
                        "TOP_K": "string",
                    },
                    output_schema={
                        "results": "array",
                        "github_repos": "array",
                        "pypi_packages": "array",
                        "jargon": "array",
                        "ok": "boolean",
                    },
                    configuration_schema={
                        "extra_variables": {
                            # Default invocation — overridden in the
                            # workflow version's node-data per run.
                            "QUERY": "",
                            "TOP_K": "5",
                        },
                    },
                    batch_mode="single",
                    **payload,
                ))
            db.commit()
            logger.info("Seeded WebSearch system NodeDefinition")
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_web_search_node failed: %s", e)


def _startup_seed_web_search_workflow():
    """add-web-search-node — seed the hidden system workflow that drives
    the WebSearch node. Invoked on demand (NO cron/schedule trigger).

    Idempotent: skipped if a workflow with the seed name already exists
    for the platform tenant. Wraps the insert in a Postgres advisory
    transaction lock so concurrent uvicorn workers serialize on the seed.
    """
    # Stable identifier for the advisory lock. Any unique int works.
    ADVISORY_LOCK_ID = 0x7a8b5c00deadbee1  # noqa: E501
    WORKFLOW_NAME = "System — Web Search"
    try:
        db = SessionLocal()
        try:
            from sqlalchemy import text as _sa_text
            # Held until COMMIT / ROLLBACK on this session.
            db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
                       {"k": ADVISORY_LOCK_ID})
            # Resolve platform tenant (the first tenant, by convention).
            tenant = db.query(models.Tenant).order_by(models.Tenant.id.asc()).first()
            if tenant is None:
                logger.info(
                    "seed_web_search_workflow: no tenants yet; skipping",
                )
                return

            # Resolve the seed node we just registered.
            node_def = db.query(models.NodeDefinition).filter(
                models.NodeDefinition.name == "system-web-search",
                models.NodeDefinition.tenant_id.is_(None),
            ).first()
            if node_def is None:
                logger.warning(
                    "seed_web_search_workflow: NodeDefinition not seeded yet; "
                    "skipping (node seed runs first)",
                )
                return

            # Idempotent — skip if already present.
            existing = db.query(models.Workflow).filter(
                models.Workflow.tenant_id == tenant.id,
                models.Workflow.name == WORKFLOW_NAME,
                models.Workflow.deleted_at.is_(None),
            ).execution_options(include_deleted=True).first()
            if existing is not None:
                # Repair a graph seeded before the root-node fix — the same
                # unrunnable shape that broke the harbor sweep. Narrow on
                # purpose: rewrites only when a ROOT carries no node_kind,
                # and only on an is_system workflow.
                try:
                    ver = db.query(models.WorkflowVersion).filter(
                        models.WorkflowVersion.id == existing.latest_version_id,
                    ).first()
                    graph = (ver.graph_json or {}) if ver else {}
                    nodes = graph.get("nodes") or []
                    if ver is not None and existing.is_system and nodes:
                        tgts = {e.get("target") for e in graph.get("edges") or []}
                        roots = [n for n in nodes if n.get("id") not in tgts]
                        broken = roots and not all(
                            (n.get("data") or {}).get("node_kind") for n in roots
                        )
                        if broken:
                            ver.graph_json = {
                                "nodes": [
                                    {"id": "process-1", "type": "process",
                                     "position": {"x": 360, "y": 120},
                                     "data": {
                                         "label": "Web search",
                                         "nodeDefinitionName": "system-web-search",
                                         "nodeDefinitionId": node_def.id,
                                         "node_kind": node_def.node_kind,
                                         "extra_variables": {
                                             "QUERY": "",
                                             "TOP_K": "5",
                                         },
                                     }},
                                ],
                                "edges": [],
                            }
                            db.commit()
                            logger.warning(
                                "seed_web_search_workflow: repaired workflow "
                                "%d — its root node carried no node_kind, so "
                                "every run failed with 'No root nodes had a "
                                "node_kind'", existing.id,
                            )
                except Exception as _rep:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "seed_web_search_workflow: graph repair skipped: %s",
                        _rep,
                    )
                return

            # Fresh insert: hidden system Workflow + WorkflowVersion.
            # NO trigger — invoked on demand by the wizard / orchestrator.
            workflow = models.Workflow(
                tenant_id=tenant.id,
                name=WORKFLOW_NAME,
                is_system=True,
            )
            db.add(workflow)
            db.flush()

            graph_json = {
                # One process node, carrying its own node_kind +
                # nodeDefinitionId + extra_variables, edges:[]. Both the
                # node_kind and the id are load-bearing: a decorative root
                # with no node_kind makes runs fail "No root nodes had a
                # node_kind" (the bug that broke the harbor sweep for weeks).
                "nodes": [
                    {"id": "process-1", "type": "process",
                     "position": {"x": 360, "y": 120},
                     "data": {
                         "label": "Web search",
                         "nodeDefinitionName": "system-web-search",
                         "nodeDefinitionId": node_def.id,
                         "node_kind": node_def.node_kind,
                         "extra_variables": {
                             "QUERY": "",
                             "TOP_K": "5",
                         },
                     }},
                ],
                "edges": [],
            }
            version = models.WorkflowVersion(
                workflow_id=workflow.id,
                version_number=1,
                graph_json=graph_json,
            )
            db.add(version)
            db.flush()
            workflow.latest_version_id = version.id
            db.commit()
            logger.info(
                "Seeded System — Web Search workflow (workflow_id=%d), "
                "on-demand (no schedule trigger)", workflow.id,
            )
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_web_search_workflow failed: %s", e)


def _startup_seed_build_dag_system_nodes():
    """δ DAG-wizard B4c — seed the system NodeDefinitions the build-
    task + generate pipeline references. Most point at the same
    build-task image (STAGE env var picks the entrypoint at dispatch
    time); the wizard-generate node points at the Containerizer image.

    Both image references compose from DOCKER_REGISTRY (the env var
    every other component reads for the platform Harbor URL) so
    on-prem deployments swapping registries get the right images
    automatically. Per-image env overrides (BUILD_TASK_IMAGE,
    CONTAINERIZER_IMAGE) still win when set.

    Idempotent: rows are upserted by name.
    """
    from utils.registries import platform_registry as _platform_registry
    platform_registry = _platform_registry()
    BUILD_TASK_IMAGE = os.getenv(
        "BUILD_TASK_IMAGE",
        f"{platform_registry}/modtim/build-task:latest",
    )
    CONTAINERIZER_IMAGE = os.getenv(
        "CONTAINERIZER_IMAGE",
        f"{platform_registry}/modtim/containerizer:latest",
    )
    seeds = [
        {
            "name": "system-build-one-variant",
            "node_kind": "system_build_one_variant",
            "description": "δ DAG — build a single image variant from a stored BuildSpec.",
            "stage_env": "build_variant",
        },
        {
            "name": "system-smoke-one-variant",
            "node_kind": "system_smoke_one_variant",
            "description": "δ DAG — run β smoke probe on a built variant.",
            "stage_env": "smoke_variant",
        },
        {
            "name": "system-push-one-variant",
            "node_kind": "system_push_one_variant",
            "description": "δ DAG — push a variant image to Harbor.",
            "stage_env": "push_variant",
        },
        {
            "name": "system-sample-test",
            "node_kind": "system_sample_test",
            "description": "δ DAG — legacy γ sample-test on hero image only (pre-§5 builds).",
            "stage_env": "sample_test",
        },
        {
            # §5 — per-variant sample-test. Runs after each
            # push-<variant> in the build DAG; uploads output to
            # MinIO and POSTs the result to the API which scores it
            # against the spec's reference (hero by default) and
            # writes a node_variant_verification row.
            "name": "system-sample-test-one-variant",
            "node_kind": "system_sample_test_one_variant",
            "description": "wizard-rebuild §5 — per-variant sample-test + drift scoring.",
            "stage_env": "sample_test_variant",
        },
        {
            "name": "system-register-node",
            "node_kind": "system_register_node",
            "description": "δ DAG — register the NodeDefinition row at the end of a successful build.",
            "stage_env": "register_node",
        },
        {
            # Auto-iterating build + smoke + LLM repair loop for the
            # hero variant. Internally rebuilds the image and re-runs
            # smoke until either the entrypoint produces real output
            # on synthetic input OR the inner/outer repair budgets in
            # the BuildSpec are exhausted. Replaces the linear
            # build-hero + smoke-hero pair in the DAG (see
            # build_dag/generator.py).
            "name": "system-test-and-repair-one-variant",
            "node_kind": "system_test_and_repair_one_variant",
            "description": "δ DAG — iterate build → smoke → LLM repair until smoke passes.",
            "stage_env": "test_and_repair_variant",
        },
        {
            # Generate phase — wraps the Containerizer container. Runs
            # FIRST in every wizard build's DAG. When this node
            # completes, the orchestrator's downstream-dispatch hook
            # parses the artifacts from result_payload (via the
            # sentinel-wrapped JSON) and writes them onto the BuildSpec,
            # so downstream nodes (test-and-repair, push, register)
            # read the LLM's Dockerfile/entrypoint/scripts directly
            # from the spec. No more client-side polling-and-dispatching;
            # the workflow orchestrator runs the same transition machinery
            # it runs for every other multi-node workflow on the platform.
            "name": "system-wizard-generate",
            "node_kind": "system_wizard_generate",
            "description": "δ DAG — first phase: Containerizer LLM generates Dockerfile + entrypoint + scripts.",
            "stage_env": "",   # not a build-task stage; dispatched via the Containerizer image directly
            "executable_image": CONTAINERIZER_IMAGE,
        },
        {
            # Second phase: read the generate node's artifact JSON from
            # /data/input/albedo-artifacts.json (placed there by the
            # workflow's normal node-to-node data flow), validate it,
            # and merge onto the BuildSpec via the API. Runs as a
            # regular build-task stage — no orchestrator-side hook.
            # Failures surface as a failed node in the run timeline,
            # same as any other DAG node.
            "name": "system-wizard-ingest-artifacts",
            "node_kind": "system_wizard_ingest_artifacts",
            "description": "δ DAG — second phase: merge Containerizer artifacts onto the BuildSpec.",
            "stage_env": "ingest_artifacts",
        },
    ]
    try:
        db = SessionLocal()
        try:
            for seed in seeds:
                existing = db.query(models.NodeDefinition).filter(
                    models.NodeDefinition.name == seed["name"],
                    models.NodeDefinition.tenant_id.is_(None),
                ).first()
                # Per-seed executable image — most are build-task,
                # but the wizard-generate node points at the
                # Containerizer image.
                seed_image = seed.get("executable_image") or BUILD_TASK_IMAGE
                # Generous 90-min container cap for build-DAG stages. These pull
                # multi-GB accelerator bases + install large wheels (e.g. torch)
                # over whatever the deploy's network is; the real hang-guard is
                # the build-task's INACTIVITY timeout (kill only if no progress),
                # so this wall-clock cap only needs to be a "can't run forever"
                # backstop set high enough that a slow-but-progressing build is
                # never clipped. Was the 1800s (30 min) column default, which
                # killed legitimate slow installs on poor links.
                _build_dag_timeout = int(seed.get("container_timeout", 5400))
                if existing is not None:
                    # Refresh executable_path + system_only flag + node_kind
                    # so the seed stays current with code rotations.
                    existing.executable_path = seed_image
                    existing.system_only = True
                    existing.description = seed["description"]
                    existing.node_kind = seed["node_kind"]
                    existing.container_timeout = _build_dag_timeout
                    continue
                nd = models.NodeDefinition(
                    name=seed["name"],
                    description=seed["description"],
                    tenant_id=None,
                    node_kind=seed["node_kind"],
                    type="System",
                    executable_path=seed_image,
                    process_type="CPU",
                    cost_in_credits=0,
                    container_timeout=_build_dag_timeout,
                    input_schema={},
                    output_schema={},
                    configuration_schema={
                        "extra_variables": {
                            # Build-task stages set STAGE env; the
                            # Containerizer ignores it. Empty stage_env
                            # means we don't inject STAGE at all.
                            **({"STAGE": seed["stage_env"]} if seed.get("stage_env") else {}),
                        },
                    },
                    system_only=True,
                    batch_mode="single",
                )
                db.add(nd)
            db.commit()
            logger.info("Seeded δ DAG system NodeDefinitions (%d)", len(seeds))
        finally:
            db.close()
    except Exception as e:
        logger.warning("seed_build_dag_system_nodes failed: %s", e)


def _startup_scan_nodes():
    """Scan node metadata files and register node definitions on startup.

    DELEGATE to scan_and_register_nodes (services/node_scanner) which applies
    the CE keep-list filter and creates tenant-scoped rows for keep-listed
    nodes. The old inline seed loop (which created system-scoped rows for
    ALL metadata files without keep-list filtering) was removed — it
    overwrote tenant-scoped keep-listed rows with system-scoped duplicates.
    """
    try:
        from services.node_scanner import scan_and_register_nodes
        # Single-flight across uvicorn workers; the scan is idempotent.
        scan_and_register_nodes(SessionLocal)
    except Exception as e:
        logger.warning(f"Node scanner failed: {e}")

    # GA.NMF.1c — pin node-version-pinning's handle on the STARTUP scan path
    # (this function), not only on the WB-refresh path (scan_and_register_nodes).
    # After an API restart, digests must be resolved + recorded without waiting
    # for the next WorkflowBuilder page-load. This runs inside the existing
    # background startup task (`_startup_tasks` runs in `lifespan`, off the
    # request path), so the registry round-trips are non-blocking for startup.
    # Placed AFTER the scan's try/except (matching scan_and_register_nodes) so a
    # scan hiccup still lets already-registered nodes get pinned. The helper
    # opens its OWN session on a fresh connection — per-row commit, never a
    # registry call inside the scan's transaction that would pin a PgBouncer
    # conn — and is throttled + never raises: a registry outage leaves nodes
    # tag-only, it can't break startup.
    try:
        from services.node_scanner import _maybe_run_digest_backfill
        _maybe_run_digest_backfill(SessionLocal)
    except Exception as e:
        logger.warning(f"Digest backfill on startup failed: {e}")


def _startup_audit_hmac():
    """Check AUDIT_HMAC_KEY is configured for audit log integrity (CMMC AU-9)."""
    hmac_key = os.getenv("AUDIT_HMAC_KEY", "")
    if not hmac_key:
        logger.warning(
            "⚠️  AUDIT_HMAC_KEY not configured — audit integrity verification disabled. "
            "Set AUDIT_HMAC_KEY environment variable for CMMC AU-9 compliance."
        )
    else:
        logger.info("✅ AUDIT_HMAC_KEY configured — audit log integrity verification enabled")


_onnx_predictor = None
_onnx_model_version = 0


def get_onnx_predictor():
    return _onnx_predictor


def _resolve_onnx_target_version(active_version: int) -> tuple[int | None, str]:
    """scheduler-ml-pipeline-hardening §3.1 — apply the pin if set.

    Returns (target_version, outcome) where outcome is one of
    'unpinned' | 'pinned' | 'pin_invalid'. The caller decides what to
    do with each (load target, fall back to active, etc.).
    """
    from services.ml_pin import resolve_target_version
    return resolve_target_version(os.environ, active_version)


def _load_onnx_model_with_pin(active_version: int):
    """Resolve the pin, attempt to load the requested version, fall back
    to active on miss. Returns (predictor, loaded_version) or
    (None, 0). Audit-emits MODEL_PIN_* events as appropriate.
    """
    from utils.model_store import download_model
    from services import ml_pin

    target, outcome = _resolve_onnx_target_version(active_version)

    if outcome == ml_pin.OUTCOME_PIN_INVALID:
        try:
            with SessionLocal() as db:
                ml_pin.emit_pin_invalid(db, os.environ.get(ml_pin.PIN_ENV_VAR, ""))
        except Exception as e:
            logger.debug("pin_invalid audit emit failed: %s", e)

    if target is None or target == 0:
        return (None, 0)

    # If pinned to a different version than active, surface a stale-pin
    # audit event when the active is newer than the pin.
    if outcome == ml_pin.OUTCOME_PINNED and active_version and target < active_version:
        try:
            with SessionLocal() as db:
                ml_pin.emit_pin_active(db, target, active_version)
        except Exception as e:
            logger.debug("pin_active audit emit failed: %s", e)

    result = download_model(target)
    if result is None and outcome == ml_pin.OUTCOME_PINNED and target != active_version:
        # Pinned to a non-existent version — fall back to active.
        try:
            with SessionLocal() as db:
                ml_pin.emit_pin_missing(db, target, active_version)
        except Exception as e:
            logger.debug("pin_missing audit emit failed: %s", e)
        result = download_model(active_version) if active_version else None
        target = active_version

    if not result:
        return (None, 0)

    onnx_path, norm_path, metadata = result
    from scheduler.onnx_predictor import ONNXSchedulerPredictor
    try:
        return (ONNXSchedulerPredictor(onnx_path, norm_path, metadata), target)
    except Exception:
        # A corrupt/unloadable model would otherwise leak this /tmp/mod-model-*
        # dir on every 60s poll × every uvicorn worker (was 9k+ dirs pegging
        # the box). Clean up before re-raising so a bad model can't snowball.
        import shutil
        shutil.rmtree(os.path.dirname(onnx_path), ignore_errors=True)
        raise


def _startup_onnx_model():
    """Load ONNX scheduler model from MinIO if available. Start hot-reload thread."""
    global _onnx_predictor, _onnx_model_version
    import threading

    try:
        # Pick the highest version whose ONNX actually LOADS — a corrupt/truncated
        # blob (the historical scheduler-v4 stub) is skipped instead of being
        # loaded and failing INVALID_PROTOBUF forever. The baseline watchdog
        # self-heals storage in the background if nothing valid is present.
        from utils.model_store import get_latest_valid_model_version
        active = get_latest_valid_model_version()
        if active == 0:
            logger.info("No loadable ONNX scheduler model found — using heuristic fallback")
            return

        predictor, loaded_version = _load_onnx_model_with_pin(active)
        if predictor is not None:
            _onnx_predictor = predictor
            _onnx_model_version = loaded_version
            if loaded_version != active:
                logger.info("ONNX scheduler model v%d loaded (pinned; latest is v%d)",
                            loaded_version, active)
            else:
                logger.info("ONNX scheduler model v%d loaded", loaded_version)
        else:
            logger.info("Could not download ONNX model — using heuristic fallback")
    except Exception as e:
        logger.warning("ONNX model startup failed (non-fatal, using heuristic): %s", e)

    def _hot_reload_loop():
        global _onnx_predictor, _onnx_model_version
        import time
        _failed_versions = set()  # versions that failed to load — don't re-download/retry every 60s
        while True:
            time.sleep(60)
            _current_target = None
            try:
                from utils.model_store import get_latest_valid_model_version
                from services import ml_pin
                latest = get_latest_valid_model_version()
                target, outcome = _resolve_onnx_target_version(latest)
                _current_target = target

                # Surface stale-pin to the audit log on every poll cycle
                # (rate-limited internally to once per hour per process).
                if outcome == ml_pin.OUTCOME_PINNED and latest and target and target < latest:
                    try:
                        with SessionLocal() as db:
                            ml_pin.emit_pin_active(db, target, latest)
                    except Exception as e:
                        logger.debug("pin_active audit emit failed in poll loop: %s", e)

                # Decide whether we need to swap. We swap when the
                # currently-loaded version differs from the resolved
                # target — covers BOTH "pin changed" and "latest
                # advanced past current" cases in one branch.
                if target is None or target == 0:
                    continue
                if target == _onnx_model_version:
                    continue
                if target in _failed_versions:
                    continue  # known-bad version — skip without re-downloading it every minute

                predictor, loaded_version = _load_onnx_model_with_pin(latest)
                if predictor is not None and loaded_version != _onnx_model_version:
                    prior = _onnx_model_version
                    _onnx_predictor = predictor
                    _onnx_model_version = loaded_version
                    logger.info("Hot-reloaded ONNX model v%d (was v%d)", loaded_version, prior)
                else:
                    _failed_versions.add(target)
            except Exception as e:
                if _current_target:
                    _failed_versions.add(_current_target)
                logger.debug("ONNX hot-reload check failed for v%s (won't retry): %s", _current_target, e)

    threading.Thread(target=_hot_reload_loop, daemon=True).start()


def _startup_nightly_freshness():
    """Layer-4 freshness — background loop that enqueues Trivy rescans for
    approved nodes whose scanned_at is older than FRESHNESS_STALE_DAYS.
    SaaS-only; no-op on on-prem (the enqueue helper itself short-circuits).

    Cadence: 1 hour check interval, dispatches only nodes that have actually
    crossed the stale threshold. Batch-limited to 200 rescans per tick so a
    long-idle deployment doesn't flood the queue in one burst.

    Kill switch: DISABLE_FRESHNESS_LOOP=true skips the thread entirely.
    """
    if os.getenv("DISABLE_FRESHNESS_LOOP", "false").lower() == "true":
        logger.info("Freshness loop disabled via DISABLE_FRESHNESS_LOOP")
        return

    interval_seconds = int(os.getenv("FRESHNESS_CHECK_INTERVAL_SECONDS", "3600"))

    def _freshness_loop():
        import time as _time
        # First tick waits a full interval so we don't hammer the DB during
        # cold boot when migrations + seeds are still churning.
        _time.sleep(interval_seconds)
        while True:
            db = SessionLocal()
            try:
                from utils.freshness import enqueue_stale_rescans
                ids = enqueue_stale_rescans(db)
                if ids:
                    logger.info("Freshness loop enqueued %d rescan(s)", len(ids))
            except Exception as e:
                logger.warning("Freshness loop tick failed (non-fatal): %s", e)
            finally:
                db.close()
            _time.sleep(interval_seconds)

    threading.Thread(target=_freshness_loop, daemon=True, name="FreshnessLoop").start()
    logger.info("Freshness loop started (interval=%ds)", interval_seconds)


def _startup_crypto_self_test():
    """GY.C2a boot self-check: run the crypto known-answer self-test once at
    startup (SHA-256 / HMAC-SHA256 / AES-256-GCM / ECDSA P-384) and cache the
    result in-process. Failure logs ERROR inside the service; readiness is only
    marked unhealthy when crypto.fips_mode_required is true. Never crashes the
    process, and no provider is installed here."""
    from utils.paid_import import optional_paid
    crypto_status = optional_paid("services.crypto_status")
    if crypto_status is None:
        logger.info("crypto_status not in this edition")
        return
    result = crypto_status.run_self_test()
    logger.info("[crypto self-test] startup result: %s", result["status"])


def _startup_payments_readiness_check():
    """15b.1c boot self-check: WARN when a SaaS install with payments on can't
    take money (key, whsec, a paid plan with a price id, real checkout URLs).
    Off-thread: it reads Vault and the DB."""
    from utils.paid_import import optional_paid
    _stripe = optional_paid("utils.stripe_api")
    if _stripe is None:
        logger.info("stripe_api not in this edition")
        return
    log_payments_readiness = _stripe.log_payments_readiness
    threading.Thread(target=log_payments_readiness, name="payments-readiness", daemon=True).start()


def _startup_release_key_bootstrap():
    """1049c: each environment self-provisions its OWN release signing
    keypair the first time it boots with none in Vault (secret/release/cosign)
    — so promoting this stack to a new environment, or someone else
    launching it fresh, doesn't require a human to remember to cut a key by
    hand. Off-thread: it reads Vault and the DB, and must never block or
    fail startup (Vault sealed/unreachable degrades to a retry next boot)."""
    from utils.paid_import import optional_paid
    _rkb = optional_paid("services.release_key_bootstrap")
    if _rkb is None:
        logger.info("release_key_bootstrap not in this edition")
        return
    ensure_release_key = _rkb.ensure_release_key
    threading.Thread(
        target=ensure_release_key, name="release-key-bootstrap", daemon=True,
    ).start()


def _startup_changelog_publish_loop():
    """Tick the What's New scheduled-publish queue every minute. Cheap
    query (indexed `scheduled_for`); flips drafts whose
    `scheduled_for <= now()` to published and emits the in-app
    notification fan-out. Safe to call repeatedly — already-published
    rows are excluded by the predicate.

    Disable via DISABLE_CHANGELOG_PUBLISH_LOOP=true.
    """
    if os.getenv("DISABLE_CHANGELOG_PUBLISH_LOOP", "false").lower() == "true":
        logger.info("Changelog publish loop disabled via env")
        return
    interval = int(os.getenv("CHANGELOG_PUBLISH_INTERVAL_SECONDS", "60"))

    def _loop():
        import time as _time
        _time.sleep(15)  # let API + migrations settle first
        from routers.changelog import publish_scheduled_changelogs
        while True:
            db = SessionLocal()
            try:
                n = publish_scheduled_changelogs(db)
                if n:
                    logger.info("Changelog publish loop: published %d entries", n)
            except Exception as e:
                logger.warning("Changelog publish loop tick failed: %s", e)
            finally:
                db.close()
            _time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="ChangelogPublishLoop").start()
    logger.info("Changelog publish loop started (interval=%ds)", interval)


def _startup_wizard_llm_warmup():
    """One-shot warmup 15s after API startup so the wizard's LLM is
    resident before a user opens the New Node wizard.

    No keep-warm loop: our per-request body already passes
    `keep_alive: -1` (from the wizard tool's Vault config, wired via
    _resolved_effective_env('wizard')), which tells Ollama to never
    idle-evict this model. Pinging periodically would be pure waste.

    Evictions still happen if Ollama itself restarts or hits VRAM
    pressure from other models — but the next wizard click cold-loads
    it once, keep_alive=-1 pins it again, and we're back to warm.

    Kill switch: DISABLE_WIZARD_WARMUP=true.
    """
    if os.getenv("DISABLE_WIZARD_WARMUP", "false").lower() == "true":
        logger.info("Wizard LLM startup warmup disabled via env")
        return

    def _warm():
        import time as _time
        _time.sleep(15)  # let the rest of startup settle first
        try:
            from utils.llm_client import call_wizard_llm, LLMClientError
            call_wizard_llm(
                system_prompt="Warmup.",
                user_prompt="ok",
                response_format="text",
                timeout_s=300,
                max_tokens=4,
            )
            logger.info("Wizard LLM warmup completed — model resident")
        except LLMClientError as e:
            logger.info("Wizard LLM warmup skipped (non-fatal): %s", e)
        except Exception as e:
            logger.warning("Wizard LLM warmup failed (non-fatal): %s", e)

    threading.Thread(target=_warm, daemon=True, name="WizardLLMWarmup").start()
    logger.info("Wizard LLM warmup dispatched (keep_alive=-1 pins it from there)")


def _startup_calibration_fixture_audit():
    """calibration-health-fixtures-and-timeouts §6 — one-time, read-only
    audit pass: for every calibration-eligible NodeDef, check whether its
    sample fixture exists in its tenant bucket for every size tier the
    coordinator might dispatch. Idempotent (runs fresh on every restart,
    writes nothing). INFO-level summary, DEBUG per-row, full miss list
    dropped to /tmp/calibration_health_<ts>.txt for the operator.

    Runs in its own thread —
    so a slow or unreachable object-storage backend during the audit can
    never delay the rest of startup (spec: "The audit never blocks
    startup"). Kill switch: DISABLE_CALIBRATION_FIXTURE_AUDIT=true.
    """
    if os.getenv("DISABLE_CALIBRATION_FIXTURE_AUDIT", "false").lower() == "true":
        logger.info("Calibration fixture audit disabled via env")
        return

    def _run():
        try:
            from services.calibration_fixture_audit import run_fixture_audit_startup
            run_fixture_audit_startup()
        except Exception as e:  # noqa: BLE001 — belt-and-suspenders; the
            # function above already swallows its own errors.
            logger.warning(
                "[calibration_health] startup fixture audit thread failed "
                "(non-fatal): %s", e,
            )

    threading.Thread(
        target=_run, daemon=True, name="CalibrationFixtureAudit").start()


def _startup_base_image_rebuild_loop():
    """Layer-4 freshness — nightly curated-base-image rebuild dispatcher.
    Enqueues imagebuilder tasks for every modtim/base-* variant so
    upstream security patches land in tenant-inheritable bases without
    operator intervention. SaaS-only; no-op on on-prem.

    Cadence default 86400s (24h). Kill switch DISABLE_BASE_REBUILD_LOOP=true.
    """
    if os.getenv("DISABLE_BASE_REBUILD_LOOP", "false").lower() == "true":
        logger.info("Base-image rebuild loop disabled via DISABLE_BASE_REBUILD_LOOP")
        return

    interval_seconds = int(os.getenv("BASE_REBUILD_INTERVAL_SECONDS", "86400"))

    def _loop():
        import time as _time
        # Stagger past cold boot — let other startup tasks settle first.
        _time.sleep(min(interval_seconds, 300))
        from utils.paid_import import optional_paid
        _bir_mod = optional_paid("utils.base_image_rebuild")
        if _bir_mod is None:
            logger.info("utils.base_image_rebuild not in this edition; skipped")
            return
        while True:
            db = SessionLocal()
            try:
                enqueue_nightly_base_rebuilds = _bir_mod.enqueue_nightly_base_rebuilds
                _is_saas = _bir_mod._is_saas
                CURATED_VARIANTS = _bir_mod.CURATED_VARIANTS
                enqueued = enqueue_nightly_base_rebuilds(db)
                if enqueued:
                    logger.info("Base-image rebuild loop enqueued %d", len(enqueued))
                elif _is_saas() and CURATED_VARIANTS:
                    # An empty return is legitimate on-prem, and a real fault in
                    # SaaS. `if enqueued:` treated both as nothing to say, so a
                    # loop that had stopped dispatching entirely looked exactly
                    # like a loop with nothing to do — for 24h at a time.
                    logger.error(
                        "Base-image rebuild tick dispatched NOTHING despite %d "
                        "curated variants. Curated bases are not being rebuilt.",
                        len(CURATED_VARIANTS))
            except Exception as e:
                logger.warning("Base-image rebuild tick failed (non-fatal): %s", e)
            finally:
                db.close()
            _time.sleep(interval_seconds)

    threading.Thread(target=_loop, daemon=True, name="BaseImageRebuildLoop").start()
    logger.info("Base-image rebuild loop started (interval=%ds)", interval_seconds)


def _startup_nim_catalog():
    """Sync NIM model catalog from nim_catalog.json into the database."""
    # 1788 CE carve: routers/nim.py is paid, so CE mounts no NIM router and has
    # no NIM model catalog to sync — skip the startup sync (same no-op as the
    # existing except branch, but without importing a denied module).
    from utils.paid_import import optional_paid
    nim = optional_paid("routers.nim")
    if nim is None:
        return

    db = SessionLocal()
    try:
        nim.sync_nim_catalog(db)
    except Exception as e:
        logger.warning(f"Could not sync NIM catalog: {e}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Background role entrypoint (scale-tiered-workloads D1 / tasks §2.1)
# ---------------------------------------------------------------------------
def run_background_worker():
    """Run the `background` role: all startup/background work, NO HTTP listener.

    The entrypoint calls this instead of uvicorn when MOD_API_ROLE=background.
    It reuses the EXACT `lifespan` context manager the web process runs, so the
    background loops (scheduler, ONNX predictor, RAG/embedding ingest), the
    asyncio refresh loops, and the orchestration queue consumer are wired up
    identically — the difference is purely that no uvicorn server is bound, so
    the background role serves no HTTP request path (tasks §2.4).

    `lifespan`'s startup runs the background subset because
    `should_run_background_work()` is True for this role; a web replica with a
    background sibling runs the same lifespan but skips that subset. The daemon
    threads spawned during startup keep the process working; this coroutine then
    parks on a stop event until SIGTERM/SIGINT, at which point the `async with`
    exits and lifespan's shutdown cancels the asyncio tasks cleanly.
    """
    import asyncio
    import signal

    from utils.worker_sizing import get_api_role

    logger.info("Starting API in BACKGROUND role (no HTTP listener) — role=%s",
                get_api_role())

    async def _run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                # add_signal_handler is unavailable on some platforms; the
                # process can still be stopped by the container runtime.
                pass
        # Reuse the web process's lifespan so startup/shutdown are identical.
        async with lifespan(app):
            logger.info("BACKGROUND role ready — draining background work")
            await stop.wait()
            logger.info("BACKGROUND role stopping — running lifespan shutdown")

    asyncio.run(_run())


if __name__ == "__main__":
    # Allow `python3 -m main` / `python3 main.py` to launch the background role
    # directly, mirroring what the entrypoint does for MOD_API_ROLE=background.
    from utils.worker_sizing import is_background_role
    if is_background_role():
        run_background_worker()