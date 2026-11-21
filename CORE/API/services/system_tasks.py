"""
System task dispatcher — runs platform maintenance as worker tasks.

Instead of running heavy initialization in the API process, these tasks
are dispatched to the worker task queue. They show up in the System Tasks
tab and have proper logging.
"""

import os
import json
import logging
from sqlalchemy.orm import Session

from models import ApiTaskQueue, ApiTaskStatus
from utils.registries import platform_registry

logger = logging.getLogger(__name__)

REGISTRY = platform_registry()

# The task_functions run_system_task knows how to dispatch. Kept in sync with
# the dispatch table in run_system_task; used to decide whether a stranded
# PENDING SYSTEM row can be safely drained (run) or must be failed as unknown.
_KNOWN_SYSTEM_TASK_FUNCTIONS = frozenset({
    "seed_account_settings",
    "seed_pricing_plans",
    "seed_capabilities",
    "seed_converter_nodes",
    "seed_vector_nodes",
    "seed_export_pipeline_nodes",
    "seed_mobile_builder",
    "seed_brand_generator_node",
    "scan_node_metadata",
    "sync_nim_catalog",
    "configure_sso_first_broker_login_flow",
    "configure_sso_identity_provider_mapper",
    # FX.90 p3a (Q76=A) — async malware scan of large uploads. These rows are
    # NOT seed-style work: they carry their target in the payload, so the
    # drain below hands them to services/malware_scan_async.py rather than
    # running them through run_system_task (which takes only a function name).
    "scan_pending_artefact",
    # sz-fortress-p34.3 — async output-safety scan (NSFW/PII registry) of a
    # worker-reported workflow output. Same shape as scan_pending_artefact:
    # the row carries its own target (content_hash) in the payload, so the
    # drain below hands it to services/output_safety_scan_async.py directly.
    "scan_output_artefact",
})


def create_system_task_record(
    db: Session,
    task_name: str,
    task_function: str,
    description: str = "",
):
    """
    Create a system task record in the queue for tracking/logging.

    These tasks execute in the API background thread (not the Go worker).
    The record exists so they appear in the System Tasks UI.
    """
    from datetime import datetime, timezone

    task = ApiTaskQueue(
        workflow_run_id=None,
        node_run_id=None,
        node_id=f"system-{task_name}",
        tenant_id=1,
        queue_name="SYSTEM",  # Special queue — Go worker ignores this
        priority=1,
        task_payload={
            "node_kind": f"system_{task_name}",
            "task_type": "system_internal",
            "task_function": task_function,
            "description": description,
            "processing_image": "api-internal",
        },
        status=ApiTaskStatus.RUNNING,
        claimed_by_worker=None,  # No FK constraint — system tasks run in API process
        claimed_at=datetime.now(timezone.utc),
        max_attempts=1,
        output_namespace="system",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def reap_orphaned_system_tasks(db: Session, grace_minutes: int = 30) -> int:
    """Fail SYSTEM-queue tasks left RUNNING by a process that died mid-run.

    `create_system_task_record` writes the row as RUNNING up front, and the
    caller in main.py finalizes it after `run_system_task` returns. That
    finalization happens on a DAEMON thread, so anything that ends the process
    first — a restart, a crash, or a pytest run whose TestClient lifespan
    started the thread and then exited — leaves the row RUNNING forever. It has
    no worker to time it out either: `claimed_by_worker` is deliberately NULL
    because these run inside the API process, so every stale-task sweep aimed
    at the Go worker skips them by construction.

    Measured on the dev deployment 2026-07-31: 17 such rows against 8,609
    normally-completed ones, the oldest stuck for 108 minutes, accumulating a
    few per API restart. Harmless individually, but they misreport the System
    Tasks UI as permanently mid-run and grow without bound.

    A system task can only be running inside an API process, so on startup any
    pre-existing RUNNING row is orphaned by definition. The grace window is
    only there for multi-replica deployments, where a peer may legitimately
    have one in flight — these finish in seconds, so 30 minutes is far past
    any real run.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_minutes)
    orphans = (db.query(ApiTaskQueue)
               .filter(ApiTaskQueue.queue_name == "SYSTEM",
                       ApiTaskQueue.status == ApiTaskStatus.RUNNING,
                       ApiTaskQueue.claimed_at < cutoff)
               .all())
    if not orphans:
        return 0

    now = datetime.now(timezone.utc)
    for task in orphans:
        task.status = ApiTaskStatus.FAILED
        task.error_message = (
            f"orphaned: the API process ended before this task finished. "
            f"Claimed {task.claimed_at.isoformat() if task.claimed_at else 'unknown'}, "
            f"reaped after {grace_minutes} min with no completion.")
        task.completed_at = now
    # k8s-reduction-engine W26: a k8s-deferred stack op orphaned here leaves its
    # PersistentStack at "deploying"; capture the targets before commit and
    # reconcile them from actual cluster state off-thread after it.
    k8s_targets = []
    try:
        from services.stack_backend_dispatch import k8s_reap_targets
        k8s_targets = k8s_reap_targets(orphans)
    except Exception:  # noqa: BLE001 — never block the reaper
        logger.debug("k8s reap-target extraction failed", exc_info=True)
    db.commit()
    logger.warning("Reaped %d orphaned system task(s) left RUNNING by a "
                   "previous process", len(orphans))
    if k8s_targets:
        try:
            from services.stack_backend_dispatch import reconcile_reaped_k8s_stacks
            reconcile_reaped_k8s_stacks(k8s_targets)
        except Exception:  # noqa: BLE001
            logger.exception("k8s stack reconcile after reap failed to start")
    return len(orphans)


def drain_pending_system_tasks(db: Session, grace_minutes: int = 5) -> int:
    """Run/finalize SYSTEM-queue rows stranded in PENDING — nothing else will.

    SYSTEM tasks are normally born RUNNING (`create_system_task_record`) and
    finalized inline on the startup maintenance thread. NOTHING consumes a
    PENDING SYSTEM row: the Go worker ignores the SYSTEM queue by design, and no
    poller selects `queue_name=='SYSTEM' AND status=='PENDING'`. So a row that
    ends up PENDING — from an older build or a manual requeue — sits forever
    (observed: task 11108 `scan_node_metadata`, stranded for days), and while
    its work doesn't run, dependent workflows fail (e.g. HarborScanFollowup:
    "root nodes … missing node_kind metadata").

    This is the missing consumer. For a KNOWN task_function it CLAIMS the row
    (RUNNING, so a concurrent tick/replica won't double-run), executes it via
    `run_system_task`, and finalizes COMPLETED/FAILED exactly like the startup
    path. An unknown/unsupported function is failed with a clear reason so it
    can't strand. A short grace avoids racing a just-enqueued row. Meant to run
    periodically so recovery is automatic and permanent. Returns rows drained.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_minutes)
    pending = (db.query(ApiTaskQueue)
               .filter(ApiTaskQueue.queue_name == "SYSTEM",
                       ApiTaskQueue.status == ApiTaskStatus.PENDING,
                       ApiTaskQueue.created_at < cutoff)
               .all())
    if not pending:
        return 0

    drained = 0
    for task in pending:
        payload = task.task_payload or {}
        fn = payload.get("task_function")
        # CLAIM first so a concurrent tick / peer replica can't double-run it.
        task.status = ApiTaskStatus.RUNNING
        task.claimed_at = datetime.now(timezone.utc)
        db.commit()
        try:
            if fn == "scan_pending_artefact":
                # FX.90 p3a: the row IS the job (cog_id in the payload).
                # scan_one owns its own status transitions + backoff, so the
                # generic finalize below must not overwrite them.
                from utils.paid_import import optional_paid
                _scan_mod = optional_paid("services.malware_scan_async")
                if _scan_mod is None:
                    # CE: no async scan module. Complete the row quietly so
                    # it does not strand; no error text.
                    task.status = ApiTaskStatus.COMPLETED
                    task.completed_at = datetime.now(timezone.utc)
                    task.result_payload = {"skipped": True,
                                           "drained_from_pending": True}
                    db.commit()
                    drained += 1
                    continue
                _scan_mod.scan_one(db, task)
                drained += 1
                continue
            if fn == "scan_output_artefact":
                # sz-fortress-p34.3: the row IS the job (content_hash in the
                # payload). scan_one owns its own status transitions + backoff.
                from services.output_safety_scan_async import scan_one as _scan_output_one
                _scan_output_one(db, task)
                drained += 1
                continue
            if not fn or fn not in _KNOWN_SYSTEM_TASK_FUNCTIONS:
                task.status = ApiTaskStatus.FAILED
                task.error_message = (
                    f"drained: unknown/unsupported system task_function "
                    f"{fn!r} — cannot run, failing so it does not strand.")[:500]
                task.completed_at = datetime.now(timezone.utc)
                db.commit()
                drained += 1
                logger.warning("Drained stranded PENDING system task %d "
                               "(unknown function %r → FAILED)", task.id, fn)
                continue

            result = run_system_task(db, fn)
            if result.get("skipped"):
                task.status = ApiTaskStatus.COMPLETED
                task.error_message = result["error"][:500]
                logger.info("System task %s skipped (module not in this edition): %s",
                            fn, result["error"])
            elif result["error"]:
                task.status = ApiTaskStatus.FAILED
                task.error_message = result["error"][:500]
            else:
                task.status = ApiTaskStatus.COMPLETED
                if result["has_warnings"]:
                    task.error_message = "Completed with warnings"
            task.completed_at = datetime.now(timezone.utc)
            task.result_payload = {"log_tail": result["log"] or "",
                                   "drained_from_pending": True}
            db.commit()
            drained += 1
            logger.warning("Drained stranded PENDING system task %d (%s → %s)",
                           task.id, fn, task.status.value)
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the sweep
            db.rollback()
            try:
                task.status = ApiTaskStatus.FAILED
                task.error_message = f"drain failed: {e}"[:500]
                task.completed_at = datetime.now(timezone.utc)
                db.commit()
                drained += 1
            except Exception:
                db.rollback()
            logger.warning("Drain of PENDING system task %d raised: %s", task.id, e)

    return drained


def run_system_task(db: Session, task_function: str) -> dict:
    """
    Execute a system task function by name.

    Returns dict with:
        log: str — captured log output
        has_warnings: bool — True if warnings/errors were logged
        error: str | None — error message if task raised an exception
    """
    import io

    log_buffer = io.StringIO()
    handler = logging.StreamHandler(log_buffer)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    error = None
    missing_paid = None
    try:
        if task_function == "seed_account_settings":
            _seed_account_settings(db)
        elif task_function == "seed_pricing_plans":
            _seed_pricing_plans(db)
        elif task_function == "seed_capabilities":
            _seed_capabilities(db)
        elif task_function == "seed_converter_nodes":
            _seed_converter_nodes(db)
        elif task_function == "seed_vector_nodes":
            _seed_vector_nodes(db)
        elif task_function == "seed_export_pipeline_nodes":
            _seed_export_pipeline_nodes(db)
        elif task_function == "seed_mobile_builder":
            _seed_mobile_builder(db)
        elif task_function == "seed_switchyard_llm_node":
            _seed_switchyard_llm_node(db)
        elif task_function == "seed_stack_model_call_node":
            _seed_stack_model_call_node(db)
        elif task_function == "seed_brand_generator_node":
            _seed_brand_generator_node(db)
        elif task_function == "scan_node_metadata":
            _scan_node_metadata(db)
        elif task_function == "sync_nim_catalog":
            _sync_nim_catalog(db)
        elif task_function == "configure_sso_first_broker_login_flow":
            _configure_sso_first_broker_login_flow(db)
        elif task_function == "configure_sso_identity_provider_mapper":
            _configure_sso_identity_provider_mapper(db)
        else:
            logger.error(f"Unknown system task function: {task_function}")
    except Exception as e:
        error = str(e)
        logger.error(f"Task {task_function} raised: {e}")
        # 1647a: CE paid-module skip — if the error is a missing
        # first-party import, surface it as a non-failure "skipped".
        from utils.paid_import import classify_missing_import
        missing_paid = classify_missing_import(e)
        if missing_paid is not None:
            error = (
                f"{missing_paid} is a paid feature not available "
                "in MOD Community Edition"
            )
    finally:
        root_logger.removeHandler(handler)

    log_text = log_buffer.getvalue()
    has_warnings = any(w in log_text.lower() for w in ['warning', 'error', 'failed', 'could not'])
    if not log_text.strip():
        # Tim bug 11-05 — a system task that legitimately had nothing to say
        # (an idempotent seeder re-run: capabilities / converter nodes already
        # seeded) left result_payload.log_tail empty, so the Dashboard's Task
        # Logs viewer fell back to its "(No logs captured)" placeholder — text
        # that tells a user logging is broken when it is not. The viewer now
        # always gets a real line; carve-skips log their own reason above.
        log_text = "completed with no log output (nothing to report)"

    return {
        "log": log_text,
        "has_warnings": has_warnings,
        "error": error,
        "skipped": missing_paid is not None,
    }


def _seed_account_settings(db: Session):
    # D76: the CE cut removes this model class from models.py (scrub row), so
    # the bare import raised ImportError ("cannot import name") on every first
    # boot and the seed task landed FAILED. classify_missing_import cannot
    # treat it as a skip (exc.name is the installed `models` module), so skip
    # here. Paid: the class exists, the import succeeds, behaviour unchanged.
    try:
        from models import BillingSettings
    except ImportError:
        logger.info("SKIPPED (settings model absent in this edition): account settings seed")
        return
    existing = db.query(BillingSettings).first()
    if not existing:
        settings = BillingSettings(
            postpaid_credit_price_cents=10,
            overage_credit_price_cents=8,
            credit_price_cents=10,
            grace_period_enabled=True,
            pricing_change_grace_period_days=30,
        )
        db.add(settings)
        db.commit()
        logger.info("Initialized default billing settings")
    else:
        logger.info("Billing settings already configured")


def _seed_pricing_plans(db: Session):
    from utils.paid_import import optional_paid
    # CARVE-U3: bind through optional_paid — the bare `from seed_plans import ...`
    # below used to raise ModuleNotFoundError on the CE image (the module is
    # carve-denied), so the system-task row landed FAILED. Same paid behaviour,
    # reached through the binding; absent -> skip the task.
    _m = optional_paid("seed_plans")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_plans")
        return
    _m.main()


def _seed_capabilities(db: Session):
    from seed_capabilities import seed_all_capabilities
    created, updated = seed_all_capabilities(db)
    if created > 0:
        logger.info(f"Seeded {created} new capabilities")
    if updated > 0:
        logger.info(f"Updated {updated} existing capabilities")


def _seed_converter_nodes(db: Session):
    from seed_converters import seed_converter_nodes
    created = seed_converter_nodes(db)
    if created > 0:
        logger.info(f"Seeded {created} converter node definitions")


def _seed_vector_nodes(db: Session):
    # 2400CR1zm — the seed module is CE-denied, so the bare import above raised
    # ModuleNotFoundError before the _is_ce_edition() guard ever ran. Make the
    # import itself safe; the keep-list guard below is unchanged.
    from utils.paid_import import optional_paid
    _m = optional_paid("seed_vector_nodes")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_vector_nodes")
        return
    from services.node_scanner import _is_ce_edition, _load_ce_keeplist_names

    if _is_ce_edition():
        keeplist = _load_ce_keeplist_names()
        if keeplist is not None and not any(
                n["name"] in keeplist for n in _m.VECTOR_NODES
        ):
            logger.info(
                "SKIPPED (CE keep-list excludes Vector Query/Vector Upsert): seed_vector_nodes"
            )
            return

    created = _m.seed_vector_nodes(db)
    if created > 0:
        logger.info(f"Seeded {created} RAG vector node definitions")


def _seed_export_pipeline_nodes(db: Session):
    # 2400CR1zl — seed_export_pipeline_nodes.py is CE-denied (open-core/manifest.yaml),
    # so the bare import used to raise ModuleNotFoundError. classify_missing_import
    # cannot rescue it (top-level module, not in _FIRST_PARTY_PREFIXES), so the
    # system-task row landed FAILED with a warning. Skip cleanly at INFO instead.
    from utils.paid_import import optional_paid
    _m = optional_paid("seed_export_pipeline_nodes")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_export_pipeline_nodes")
        return
    created = _m.seed_export_pipeline_nodes(db)
    if created > 0:
        logger.info(f"Seeded {created} RAG/LoRA Export pipeline node definition(s)")


def _seed_mobile_builder(db: Session):
    from utils.paid_import import optional_paid
    _m = optional_paid("seed_mobile_builder")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_mobile_builder")
        return
    created = _m.seed_mobile_builder(db)
    if created > 0:
        logger.info(f"Seeded {created} mobile-builder node definition(s)")
    # Mirror the durable signing keystore from .env.secrets into the platform
    # secret store so the mobile_build node can pull it via a secret_name ref.
    try:
        _m.seed_mobile_keystore_secret(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Mobile keystore seed skipped: {exc}")


def _seed_stack_model_call_node(db: Session):
    """2400GX-n7c (30.4.7): the Stack Model Call node (image cogs -> the
    tenant's own model stack via /v1/llm). CE-shipped: the seed module is in
    open-core/manifest.yaml api_allow and its image is in vet-container.sh
    VETTABLE, so no optional_paid guard is needed (same posture as
    _seed_converter_nodes)."""
    from seed_stack_model_call_node import seed_stack_model_call_node
    created = seed_stack_model_call_node(db)
    if created > 0:
        logger.info("Seeded the Stack Model Call node definition")
    else:
        logger.info("Stack Model Call node already seeded")


def _seed_switchyard_llm_node(db: Session):
    from utils.paid_import import optional_paid
    _m = optional_paid("seed_switchyard_llm_node")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_switchyard_llm_node")
        return
    created = _m.seed_switchyard_llm_node(db)
    if created > 0:
        logger.info("Seeded the Switchyard LLM node definition")


def _seed_brand_generator_node(db: Session):
    """1614 (launch-completion-megaplan): the Brand Asset Generate node was
    only seedable via its standalone __main__ path — wire it into the startup
    seed sequence so a fresh (non-CE) install gets it. The seed itself is
    idempotent by (name, tenant_id); do not re-implement that here."""
    from utils.paid_import import optional_paid
    _m = optional_paid("seed_brand_generator_node")
    if _m is None:
        logger.info("SKIPPED (not in this edition): seed_brand_generator_node")
        return
    from services.node_scanner import _is_ce_edition, _load_ce_keeplist_names

    if _is_ce_edition():
        keeplist = _load_ce_keeplist_names()
        if keeplist is not None and _m.BRAND_GENERATOR_NODE["name"] not in keeplist:
            logger.info(
                "SKIPPED (CE keep-list excludes Brand Asset Generate): seed_brand_generator_node"
            )
            return

    created = _m.seed_brand_generator_node(db)
    if created > 0:
        logger.info("Seeded the Brand Asset Generate node definition")


def _scan_node_metadata(db: Session):
    import models
    from services.node_scanner import parse_metadata, METADATA_ROOT
    from utils.metadata_template import coerce_and_validate

    search_dir = os.path.join(METADATA_ROOT, "system")
    if not os.path.isdir(search_dir):
        logger.warning(f"Metadata directory not found: {search_dir}")
        return

    registered = 0
    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if not d.startswith('_') and d != '__pycache__' and d.lower() != 'internal']
        for filename in files:
            if not filename.endswith(".py"):
                continue
            metadata = parse_metadata(os.path.join(root, filename))
            if not metadata or not metadata.get("node_kind"):
                continue

            node_kind = metadata["node_kind"]
            node_name = metadata.get("name", "Unnamed")
            node_type = metadata.get("category") or metadata.get("type", "General")

            ephemeral = coerce_and_validate(metadata).get("ephemeral_resources")

            # Build custom_metadata (includes accepted_secrets + node metadata)
            custom_meta = {**(metadata.get("metadata") or {})}
            if "accepted_secrets" in metadata:
                custom_meta["accepted_secrets"] = metadata["accepted_secrets"]
            if ephemeral:
                custom_meta["ephemeral_resources"] = ephemeral

            existing = db.query(models.NodeDefinition).filter(
                (models.NodeDefinition.node_kind == node_kind) |
                (models.NodeDefinition.name == node_name),
                models.NodeDefinition.tenant_id.is_(None),
            ).first()

            if existing:
                # Update custom_metadata if it was missing (e.g., accepted_secrets added later)
                if custom_meta and not existing.custom_metadata:
                    new_meta = custom_meta
                elif isinstance(existing.custom_metadata, dict):
                    # Keep ephemeral_resources in sync with the metadata file.
                    # Reassign (not mutate): the JSONB column isn't mutation-tracked.
                    new_meta = dict(existing.custom_metadata)
                    if ephemeral:
                        new_meta["ephemeral_resources"] = ephemeral
                    else:
                        new_meta.pop("ephemeral_resources", None)
                else:
                    new_meta = existing.custom_metadata
                if new_meta != existing.custom_metadata:
                    existing.custom_metadata = new_meta
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                continue

            nd = models.NodeDefinition(
                name=node_name, tenant_id=None,
                description=metadata.get("description", ""),
                node_kind=node_kind, type=node_type,
                executable_path=metadata.get("executable_path", ""),
                process_type=metadata.get("process_type", "CPU"),
                input_schema=metadata.get("input_schema", {}),
                output_schema=metadata.get("output_schema", {}),
                configuration_schema=metadata.get("configuration_schema", {}),
                custom_metadata=custom_meta if custom_meta else None,
                cost_in_credits=metadata.get("cost_in_credits", 0),
                batch_mode=metadata.get("batch_mode", "batch"),
                accepted_formats=metadata.get("accepted_formats"),
            )
            db.add(nd)
            try:
                db.commit()
                registered += 1
            except Exception:
                db.rollback()

    logger.info(f"Metadata scan complete: {registered} new nodes registered")


def _sync_nim_catalog(db: Session):
    try:
        # 2400LR-ceMNF-F2 CE carve: routers/nim.py is stripped from the CE tree
        # (manifest deny), so the bare import above raised ModuleNotFoundError on
        # EVERY sync_nim_catalog system task and this except logged a WARNING per
        # tick. optional_paid answers None for an edition carve (no import
        # attempted, nothing logged). SaaS: same call, same order, same try/except.
        from utils.paid_import import optional_paid
        _nim = optional_paid("routers.nim")
        if _nim is not None:
            _nim.sync_nim_catalog(db)
        else:
            # Tim bug 11-05 (Dashboard "No logs captured" on the nim_catalog
            # system task): every other CE-carve seeder above logs a
            # "SKIPPED (not in this edition): ..." INFO line, so its
            # system-task row carries a real log_tail and the viewer shows
            # WHY nothing ran. This branch logged nothing at all, so the row
            # completed with an empty log_tail and the Dashboard fell back to
            # the "(No logs captured)" placeholder — a silent task, not a
            # missing one. Same shape as seed_switchyard_llm_node / seed_mobile_builder.
            logger.info("SKIPPED (not in this edition): sync_nim_catalog")
    except Exception as e:
        logger.warning(f"NIM catalog sync: {e}")


# ---------------------------------------------------------------------------
# add-self-service-sso §2.8 — First Broker Login flow seeding
#
# When a tenant's SSO IdP attaches an unknown identity, Keycloak's default
# "first broker login" flow silently creates a brand-new realm user — which
# would duplicate existing password users on every tenant turning SSO on
# after Day 1. The fix per the sso-account-linking capability is to use a
# custom flow that surfaces an explicit "we found a MOD account with this
# email. Link it?" consent screen.
#
# Rather than modify the built-in flow (which would also change behavior
# for the existing GitHub / Google / LinkedIn / Facebook / Microsoft social-
# login providers — see KeycloakAdmin.PROVIDER_CONFIGS), we clone the
# built-in flow to a new alias `first-broker-login-consent` and tighten
# the `Confirm Link Existing Account` execution requirement to REQUIRED.
# New tenant-managed IdPs are then created with
# `firstBrokerLoginFlowAlias = "first-broker-login-consent"`; existing
# social-login IdPs continue to use the built-in default.
#
# Idempotent: re-runs find the existing flow and no-op.
# ---------------------------------------------------------------------------

_CUSTOM_FBL_FLOW_ALIAS = "first-broker-login-consent"
_BUILTIN_FBL_FLOW_ALIAS = "first broker login"


def _configure_sso_first_broker_login_flow(db: Session):
    """Provision the custom First Broker Login flow that surfaces the
    account-link consent screen.
    """
    import httpx

    from keycloak_auth import keycloak_admin_client

    admin_url = keycloak_admin_client.admin_api_url
    token = keycloak_admin_client._get_token()
    headers = {"Authorization": f"Bearer {token}"}

    # Step 1: does the custom flow already exist?
    resp = httpx.get(f"{admin_url}/authentication/flows",
                     headers=headers, timeout=10)
    resp.raise_for_status()
    flows = resp.json()
    existing = next(
        (f for f in flows if f.get("alias") == _CUSTOM_FBL_FLOW_ALIAS),
        None,
    )
    if existing is None:
        # Step 2: copy the built-in flow under the new alias.
        logger.info("Cloning '%s' → '%s'",
                    _BUILTIN_FBL_FLOW_ALIAS, _CUSTOM_FBL_FLOW_ALIAS)
        # Keycloak's copy endpoint expects the source alias URL-encoded
        # (built-in alias has a space in it).
        from urllib.parse import quote
        src = quote(_BUILTIN_FBL_FLOW_ALIAS, safe='')
        resp = httpx.post(
            f"{admin_url}/authentication/flows/{src}/copy",
            headers=headers,
            json={"newName": _CUSTOM_FBL_FLOW_ALIAS},
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            logger.error("Failed to copy built-in FBL flow: HTTP %s %s",
                         resp.status_code, resp.text[:300])
            return
    else:
        logger.info("Custom FBL flow '%s' already exists; no-op.",
                    _CUSTOM_FBL_FLOW_ALIAS)

    # Step 3: ensure `Confirm Link Existing Account` is REQUIRED in the
    # custom flow. The default in the built-in flow is ALTERNATIVE,
    # which lets Keycloak skip the consent screen if the user can be
    # silently linked — the exact behavior the sso-account-linking
    # capability forbids.
    resp = httpx.get(
        f"{admin_url}/authentication/flows/{_CUSTOM_FBL_FLOW_ALIAS}/executions",
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    executions = resp.json()
    target = next(
        (e for e in executions
         if e.get("displayName") == "Confirm link existing account"
         or e.get("providerId") == "idp-confirm-link"),
        None,
    )
    if target is None:
        logger.warning(
            "Custom FBL flow missing the 'Confirm link existing account' "
            "execution — Keycloak default flow may have changed shape. "
            "Manual review needed."
        )
        return
    if target.get("requirement") != "REQUIRED":
        target["requirement"] = "REQUIRED"
        resp = httpx.put(
            f"{admin_url}/authentication/flows/{_CUSTOM_FBL_FLOW_ALIAS}/executions",
            headers=headers,
            json=target,
            timeout=10,
        )
        if resp.status_code in (200, 204):
            logger.info("Set 'Confirm Link Existing Account' = REQUIRED on '%s'",
                        _CUSTOM_FBL_FLOW_ALIAS)
        else:
            logger.warning("Failed to tighten requirement: HTTP %s %s",
                           resp.status_code, resp.text[:200])
    else:
        logger.info("'Confirm Link Existing Account' already REQUIRED; no-op.")


# ---------------------------------------------------------------------------
# add-self-service-sso §5.6 — identity_provider claim mapper
# ---------------------------------------------------------------------------
# Ensures the `dashboard-client` (and `workflow-client` when present) emits
# an `identity_provider` claim in the access token. Without this mapper,
# Keycloak's default access token does not carry the brokering provider,
# and we cannot distinguish a password login from an SSO login at
# /auth/exchange-code — so we cannot reliably emit SSO_PASSWORD_BYPASS_USED.
#
# Mapper type: oidc-usersessionmodel-note-mapper, sourcing
# `identity_provider` user-session note (set automatically by Keycloak
# when a session was established via broker login).
#
# Idempotent: re-runs find the existing mapper by name and no-op.
# ---------------------------------------------------------------------------

_SSO_IDP_MAPPER_NAME = "identity_provider"
_SSO_IDP_CLIENT_IDS = ("dashboard-client", "workflow-client")


def _configure_sso_identity_provider_mapper(db: Session):
    """Provision the `identity_provider` User Session Note mapper on
    every dashboard-facing OIDC client. Required for §5.6 audit.
    """
    import httpx

    from keycloak_auth import keycloak_admin_client

    admin_url = keycloak_admin_client.admin_api_url
    token = keycloak_admin_client._get_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.get(f"{admin_url}/clients", headers=headers, timeout=10)
    resp.raise_for_status()
    clients = resp.json()

    for client_id_str in _SSO_IDP_CLIENT_IDS:
        client = next(
            (c for c in clients if c.get("clientId") == client_id_str),
            None,
        )
        if client is None:
            logger.info(
                "client '%s' not present in realm; skipping identity_provider mapper.",
                client_id_str,
            )
            continue
        client_uuid = client["id"]

        mappers_resp = httpx.get(
            f"{admin_url}/clients/{client_uuid}/protocol-mappers/models",
            headers=headers,
            timeout=10,
        )
        mappers_resp.raise_for_status()
        existing_mappers = mappers_resp.json()
        if any(m.get("name") == _SSO_IDP_MAPPER_NAME for m in existing_mappers):
            logger.info(
                "client '%s' already has identity_provider mapper; no-op.",
                client_id_str,
            )
            continue

        body = {
            "name": _SSO_IDP_MAPPER_NAME,
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usersessionmodel-note-mapper",
            "consentRequired": False,
            "config": {
                "user.session.note": "identity_provider",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "userinfo.token.claim": "true",
                "claim.name": "identity_provider",
                "jsonType.label": "String",
            },
        }
        create_resp = httpx.post(
            f"{admin_url}/clients/{client_uuid}/protocol-mappers/models",
            headers=headers,
            json=body,
            timeout=10,
        )
        if create_resp.status_code in (200, 201, 204):
            logger.info(
                "added identity_provider mapper to client '%s'",
                client_id_str,
            )
        else:
            logger.warning(
                "failed to add identity_provider mapper to '%s': HTTP %s %s",
                client_id_str, create_resp.status_code, create_resp.text[:200],
            )
