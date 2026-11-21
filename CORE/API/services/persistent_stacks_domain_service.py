"""Persistent-stacks (core sub-module) service layer (Gate 2b).

Named `persistent_stacks_domain_service` (with the `_domain` suffix) to avoid
colliding with `services/stack_wake.py`, the `persistent_stacks` router
package, and every other `services/stack_*.py` module already in this
codebase.

Holds the policy extracted out of the query-heavy handlers in
`routers/persistent_stacks/persistent_stacks.py` (the Gate-2a core-CRUD
sub-module). Per SHARED RULES (wave 1): this is a body-only move — each
function below is a verbatim extraction of logic that used to live inline in
the router endpoint of the same name; control flow, log messages, audit
events and commit points are preserved exactly so the contract snapshot and
runtime behavior stay byte-identical.

Data access goes through `crud.persistent_stacks_domain` (the only place
`db.query(models.X)` lives for this slice). Everything else this domain
already factored out into `routers/persistent_stacks/_shared.py` (compose
validation, mesh/secret/storage helpers, audit, response shaping, etc.) is
reused AS-IS via a lazy, function-local import — `_shared` is imported from,
never edited (SHARED RULES / task scope), and the import is function-local
(not module-level) to avoid forcing the whole `routers.persistent_stacks`
package to initialize as an import-time side effect of importing this service
module, matching this codebase's existing convention of lazily importing
heavy/potentially-cyclic modules inside functions (see e.g.
`services/stack_billing.py`, `services/llm_idle.py`, which already do
`from routers.persistent_stacks import (...)` the same way).

Errors: these handlers already raise plain `HTTPException` inline (this
router does not use the `services._base.ServiceError` scaffold), so the
migrated services keep doing the same — swapping to `ServiceError` here would
change nothing observable but would be an unrequested behavior change.

GT.D5 (CE carve): the non-CE ops layer — managed AI as stacks, observability,
fleet placement, predictive sleep — lives in
`services/persistent_stacks_paid_service.py`. This CE module no longer
defines those functions; `__getattr__` below re-exports them through a
guarded shim so the pre-split call sites keep resolving while they are
repointed (DS-2/3/4), and in a CE build (the non-CE module dropped) the domain
module still imports and its CE functions fall back to fleet-derived
behaviour.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

import models
from crud import persistent_stacks_domain as psd_crud
from utils import paid_import as _paid_import

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')


# =============================================================================
# node_stack_bindings — GET /node-bindings
# =============================================================================

def node_stack_bindings(db: Session, user: "models.User"):
    from routers.persistent_stacks._shared import (
        NodeBindingsResponse, NodeStackBinding, ManagedStackState,
        logger, _stack_backed_node_kinds, _derive_stack_state,
    )

    bindings: Dict[str, NodeStackBinding] = {}
    try:
        from services import stack_wake
    except Exception:  # noqa: BLE001
        logger.debug("node-bindings: stack_wake import failed", exc_info=True)
        return NodeBindingsResponse(bindings={})

    for node_kind in _stack_backed_node_kinds():
        try:
            ref = stack_wake.resolve_required_stack(node_kind)
            if ref is None:
                continue  # not actually stack-backed
            try:
                row = stack_wake._find_row(db, ref)
            except Exception:  # noqa: BLE001
                row = None
            bindings[node_kind] = NodeStackBinding(
                stack_name=ref.stack_name,
                family=ref.family,
                state=_derive_stack_state(db, ref, row),
            )
        except Exception:  # noqa: BLE001 — one bad kind never sinks the map
            logger.debug("node-bindings: resolve failed for %s", node_kind,
                         exc_info=True)

    # Per-instance requires_stack resolution: every managed PersistentStack row,
    # keyed by NAME, with its family + live state. The WB overlay uses this to
    # light up a node the user pinned to a stack by name (not node_kind). Reuses
    # the same list_stacks query + _derive_stack_state helper. Family comes from
    # the bindings map when a stack is a known NIM/VLM/SDXL/LLM stack, else
    # "generic" (a user-managed/assignable stack). Fully defensive: any per-row
    # failure degrades that row rather than failing the request.
    stacks: Dict[str, ManagedStackState] = {}
    family_by_name = {b.stack_name: b.family for b in bindings.values()}
    try:
        rows = psd_crud.list_bound_persistent_stacks(db, user.tenant_id)
    except Exception:  # noqa: BLE001 — a broken listing yields an empty map
        logger.debug("node-bindings: stack listing failed", exc_info=True)
        rows = []
    for row in rows:
        try:
            name = getattr(row, "name", None)
            if not name:
                continue
            family = family_by_name.get(name, "generic")
            ref = stack_wake.StackRef(
                key=name, stack_name=name,
                tenant_id=getattr(row, "tenant_id", user.tenant_id),
                family=family,
            )
            stacks[name] = ManagedStackState(
                family=family,
                state=_derive_stack_state(db, ref, row),
            )
        except Exception:  # noqa: BLE001 — one bad row never sinks the map
            logger.debug("node-bindings: stack state failed for %s",
                         getattr(row, "name", "?"), exc_info=True)

    return NodeBindingsResponse(bindings=bindings, stacks=stacks)


# =============================================================================
# resolve_stack_endpoint — GET /{stack_id}/endpoint
# =============================================================================

def resolve_stack_endpoint(db: Session, stack_id: int, user: "models.User"):
    from routers.persistent_stacks._shared import (
        _get_owned, MANAGER_REGION, _stack_reachability, logger,
    )

    stack = _get_owned(db, stack_id, user)
    # A stack stuck at 'deploying' because nothing in the fleet satisfies its
    # accelerator demand should say so here, not sit silent. The sweep records
    # the shortfall on the queued task; surface it on the stack the operator is
    # actually looking at.
    if stack.status == "deploying" and not stack.status_reason:
        try:
            stuck = psd_crud.find_stuck_pending_deploy_task(db, stack.id)
            if stuck is not None and stuck.unrunnable_reason:
                stack.status_reason = stuck.unrunnable_reason
        except Exception as e:  # noqa: BLE001 — columns not migrated / transient
            logger.debug("unrunnable lookup failed for stack %s: %s", stack.id, e)
    zone = stack.home_zone
    routed_region = None
    endpoint = stack.lb_endpoint              # default: worker-local host:port
    is_local = True
    if zone and MANAGER_REGION and zone != MANAGER_REGION:
        # Lives in another region — resolve that region's owning manager.
        peer = psd_crud.find_approved_active_peer_manager(
            db, zone, os.getenv("MANAGER_ID", "01m"))
        if peer is not None:
            endpoint = peer.url               # the owning manager's entry URL
            routed_region = zone
            is_local = False
    # §5.4 — reachability requires CURRENT observation, not a past successful
    # deploy. `status == "running"` alone is the outcome of the last dispatched
    # task, which says nothing about the hours since; a caller routed to a
    # stack on that basis gets connection-refused and no explanation. A stack
    # is reachable when a worker is telling us, now, that it is running.
    #
    # Only applies to LOCAL stacks. A remote one is served by its owning
    # manager, and this manager sees no observations for it — treating absence
    # of local observation as unreachable would black-hole every cross-region
    # stack in the mesh.
    _so = _paid_import.optional_paid("services.stack_observation")
    # CE: the observer module is absent (open-core/manifest.yaml denies
    # CORE/API/**/stack_observation.py), so there is no observation channel at
    # all — absence of observation is not evidence of absence. For a local
    # stack in an edition without the observer, fall back to the stack's own
    # status (the last dispatched outcome) rather than black-holing it.
    observed_now = (bool(_so.observed_hosts(db, stack.id))
                    if (_so is not None and is_local)
                    else (is_local and stack.status == "running"))
    reach = _stack_reachability(stack, endpoint, is_local, observed_now)

    return {
        "stack_id": stack.id,
        "name": stack.name,
        "status": stack.status,
        # Why the stack isn't serving, when it isn't. "not reachable" with no
        # explanation was the old behaviour for EVERY stack (nothing ever set
        # status='running'), which made a real deploy failure indistinguishable
        # from the lifecycle bug.
        "status_reason": stack.status_reason,
        "cleanup_pending": bool(stack.cleanup_pending),
        "worker": stack.target_worker,
        "endpoint": endpoint,
        # mod-connect-service-mesh §2 — per-service overlay endpoints for reachable
        # services (MagicDNS name + stored node/status). Empty for stacks with no
        # reachable service; those still resolve via `endpoint` above.
        "mesh_endpoints": reach["mesh_endpoints"],
        "reachable": reach["reachable"],
        # How it is reachable (§2.2): "mesh" (per-service MagicDNS), "local"
        # (worker host:port), or null when not reachable.
        "reach_mode": reach["reach_mode"],
        # §5.10 — running (or observed up) yet unreachable: a distinct state from
        # "stopped" and from "failed deploy". For a mesh stack this is the fabric
        # or control server gone while the containers stay up.
        "running_unreachable": reach["running_unreachable"],
        # Distinguishes "no worker is reporting this" from "the deploy failed",
        # which look identical through `reachable` alone.
        "observed_running_on": (_so.observed_hosts(db, stack.id)
                                if (_so is not None and is_local) else None),
        "home_zone": stack.home_zone,
        "is_local": is_local,
        "routed_to_region": routed_region,     # null when served locally
    }


# =============================================================================
# list_accelerator_kinds — GET /accelerator-kinds
# =============================================================================

# GT.D5 CE fallback — static kinds the wizard/import paths can express with
# tenant-OWNED workers only (the managed-LLM catalog in `managed_ai_stacks`
# is a paid module and is dropped from CE builds). A kind is offered when
# it is a recognised fleet kind OR already present on a worker's hardware
# profile; `present_in_fleet` separates "you own this hardware" from
# "untested here".
_CE_ACCELERATOR_KINDS = (
    "cuda", "rocm", "vulkan", "intel", "npu", "cpu",
)


def list_accelerator_kinds(db: Session, preset: Optional[str] = None):
    mas = _paid_import.optional_paid("services.managed_ai_stacks")

    if mas is not None:
        kinds = mas.accelerator_kinds()
        if preset:
            for k in kinds:
                k["compatibility"] = mas.preset_compatibility(preset, k["kind"])

        # What this deployment actually has, so the UI can separate "untested"
        # from "you do not own this hardware".
        present = set(psd_crud.distinct_accelerator_profile_kinds(db))
        for k in kinds:
            k["present_in_fleet"] = k["kind"] in present

        return {
            "default": mas.DEFAULT_ACCELERATOR_KIND,
            "kinds": kinds,
            "fleet_kinds": sorted(present),
        }

    # CE: no managed-LLM catalog. The wizard/import paths only need the kinds
    # the tenant's own fleet has, so derive from the fleet hardware profiles
    # and keep the static default kinds as the selectable set.
    present = set(psd_crud.distinct_accelerator_profile_kinds(db))
    known = sorted(present & set(_CE_ACCELERATOR_KINDS))
    kinds = [
        {"kind": k, "label": k.capitalize() if k != "cpu" else "CPU (no accelerator)",
         "verified": False, "note": "CE fleet kind (no managed-LLM catalog).",
         "available_presets": [], "image": None,
         "present_in_fleet": True}
        for k in known
    ] + [
        {"kind": k, "label": k.capitalize() if k != "cpu" else "CPU (no accelerator)",
         "verified": False,
         "note": "Default CE kind (not present on any worker yet).",
         "available_presets": [], "image": None,
         "present_in_fleet": False}
        for k in _CE_ACCELERATOR_KINDS
        if k not in present
    ]
    return {"default": "cuda", "kinds": kinds, "fleet_kinds": sorted(present)}


# =============================================================================
# mesh_reachability — GET (internal) /mesh/reachability
# =============================================================================

def mesh_reachability(db: Session):
    from routers.persistent_stacks._shared import (
        _mesh_name_prefix, _anchor_fresh, _reachable_service_names,
        _MESH_UPSTREAM_PORT,
    )

    now = datetime.utcnow()
    stacks = psd_crud.list_stacks_with_mesh_endpoints(db)
    worker_names = {s.target_worker for s in stacks if s.target_worker}
    workers = {}
    if worker_names:
        for w in psd_crud.list_workers_by_names(db, list(worker_names)):
            workers[w.worker_name] = w
    services: Dict[str, Any] = {}
    for stack in stacks:
        prefix = _mesh_name_prefix(stack)
        worker = workers.get(stack.target_worker)
        specs = worker.hardware_specs if (worker and isinstance(worker.hardware_specs, dict)) else {}
        seen = specs.get("anchor_seen") if isinstance(specs.get("anchor_seen"), dict) else {}
        site_ip = specs.get("advertise_address_site") or specs.get("advertise_address")
        cloud_ip = specs.get("advertise_address_cloud")
        lhb = getattr(worker, "last_heartbeat", None)
        site = (f"{site_ip}:{_MESH_UPSTREAM_PORT}"
                if site_ip and _anchor_fresh(seen.get("site"), lhb, now) else None)
        cloud = (f"{cloud_ip}:{_MESH_UPSTREAM_PORT}"
                 if cloud_ip and _anchor_fresh(seen.get("cloud"), lhb, now) else None)
        prefer = "site" if site else ("cloud" if cloud else "site")
        for svc in _reachable_service_names(stack):
            services[f"{prefix}-{svc}"] = {"site": site, "cloud": cloud, "prefer": prefer}
    return {"services": services, "generated_at": now.isoformat() + "Z"}


# =============================================================================
# ingress_byo_certs — GET /ingress/byo-certs
# =============================================================================

def ingress_byo_certs(db: Session):
    from routers.persistent_stacks._shared import _TENANT_TLS_SETTING_KEY
    rows = psd_crud.list_tenant_settings_by_key(db, _TENANT_TLS_SETTING_KEY)
    out = []
    for r in rows:
        v = r.value if isinstance(r.value, dict) else {}
        if v.get("mode") == "byo" and v.get("cert_vault_path"):
            out.append({"path": v["cert_vault_path"],
                        "fqdn": v.get("cert_cn") or ""})
    return {"certs": out}


# =============================================================================
# deploy_stack — POST /{stack_id}/deploy
# =============================================================================

def deploy_stack(db: Session, stack_id: int, to_worker: Optional[str], user: "models.User",
                 image_overrides: Optional[Dict[str, str]] = None,
                 idempotency_key: Optional[str] = None,
                 key_id: Optional[int] = None):
    from routers.persistent_stacks._shared import (
        _get_owned, _validate_compose, _project_name,
        _aggregate_accelerator_demand, _provision_mesh_identity,
        _stack_mesh_endpoints, _dispatch_stack_task, _compose_yaml_with_vars,
        _resolve_stack_secrets, _capture_reservation_units, _provision_storage,
        _audit_stack, _publish_event, _cancel_superseded_deploy_tasks, logger,
        _require_worker_file_secrets, _stack_wants_file_materialisation,
        _parse_accelerator_requirements, _parse_egress_policy,
        _parse_secret_refs, _parse_tls_config, _record_egress_declarations,
    )
    import copy
    import services.persistent_stack_revisions as _psr
    from services import stack_variables as _sv
    from services import stack_deploy_policy as _sdp
    from services import platform_settings as _ps
    from utils.http_errors import ApiError
    from services import run_idempotency as _idem
    from utils.request_principal import get_api_key
    import hashlib
    import json
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    stack = _get_owned(db, stack_id, user)

    # 30.6.3 c3-J4c — the deploy credential. `idempotency_key` is validated with
    # the SAME posture n8 already uses (services/run_idempotency.py `resolve_key`,
    # KEY_RE = 1-255 chars of [A-Za-z0-9._:-]) and the 64-char cap the design
    # (:43) puts on it, so a bad charset / over-length key is a 422 and NOTHING
    # is written. The raw key is never logged, never put in audit details and
    # never echoed in an error body (S2): only its sha256 and the credential's
    # `ApiKey.id` (`key_id`) are ever recorded.
    _idem_key = None
    _idem_req_hash = None
    if idempotency_key is not None:
        if len(idempotency_key) > 64:
            raise HTTPException(status_code=422, detail="idempotency_key must be 1-64 chars")
        try:
            _idem_key = _idem.resolve_key(body_key=idempotency_key, header_key=None)
        except ValueError as _bad:
            raise HTTPException(status_code=422, detail=str(_bad))
        if _idem_key is not None:
            _idem_req_hash = hashlib.sha256(json.dumps(
                {"image_overrides": (sorted(image_overrides.items())
                                     if image_overrides else None),
                 "to_worker": to_worker},
                sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

    def _idem_lookup():
        """The (request_hash, revision_id) ledger row for THIS scope, or None.
        Scope is (tenant_id, stack_id, key) — never global, so a tenant can
        never learn that another tenant used the same key (S1, no oracle)."""
        if _idem_key is None:
            return None
        return db.execute(text(
            "SELECT request_hash, revision_id FROM stack_deploy_idempotency_keys "
            "WHERE tenant_id = :t AND stack_id = :s AND idem_key = :k"),
            {"t": stack.tenant_id, "s": stack.id, "k": _idem_key}).first()

    _idem_won = False

    def _idem_claim():
        """Claim the key in the CALLER's transaction (no commit) — the n8 posture.
        True when this call won; False when the unique scope index says another
        call did (a concurrent in-flight winner makes this INSERT wait for its
        outcome, so a loser never queues a second deploy task).

        c3-J4c-fix: an override deploy claims once BEFORE the revision write
        (:511) and the shared dispatch tail claims again at :755 — the SAME
        request must not be its own loser, so the win is remembered per call.
        """
        nonlocal _idem_won
        if _idem_key is None:
            return False
        if _idem_won:
            return True
        got = None
        try:
            # c3-J4c-fix: the INSERT is isolated in its OWN savepoint. Postgres'
            # `ON CONFLICT ... DO NOTHING RETURNING id` yields no row (None) for
            # the loser, so the loser path is driven off `got is None`; if an
            # IntegrityError ever does surface (a second constraint), ONLY this
            # savepoint unwinds — never the caller's transaction/savepoint, which
            # is what a bare db.rollback() would poison.
            with db.begin_nested():
                got = db.execute(text(
                    "INSERT INTO stack_deploy_idempotency_keys "
                    "(tenant_id, stack_id, idem_key, request_hash, created_at) "
                    "VALUES (:t, :s, :k, :h, clock_timestamp()) "
                    "ON CONFLICT (tenant_id, stack_id, idem_key) DO NOTHING "
                    "RETURNING id"),
                    {"t": stack.tenant_id, "s": stack.id, "k": _idem_key,
                     "h": _idem_req_hash}).first()
        except IntegrityError:
            return False
        _idem_won = got is not None
        return _idem_won

    def _idem_replay():
        """Same key + same normalized body -> the stack as-is (200), nothing
        written; same key + DIFFERENT body -> 409 `idempotency_key_reused`
        (the code n8's deploy already documents). A denied first attempt
        recorded nothing, so a denied retry re-runs the policy and 403s again
        (Q4 = A)."""
        if _idem_key is None:
            return False
        row = _idem_lookup()
        if row is None:
            return False
        if row[0] != _idem_req_hash:
            raise ApiError(409, "idempotency_key_reused",
                           "this idempotency_key was already used for a different deploy")
        return True

    def _idem_lost():
        """Q3 = A: a concurrent identical POST lost the unique scope index, so
        it takes the winner's replay answer. A genuinely different body -> 409;
        a winner still mid-transaction (its row not yet visible) -> the stack
        as-is, exactly the live-in-flight posture already used below."""
        row = _idem_lookup()
        if row is not None and row[0] != _idem_req_hash:
            raise ApiError(409, "idempotency_key_reused",
                           "this idempotency_key was already used for a different deploy")
        return True

    def _idem_attach(rev):
        """Point the claimed key row at the revision this deploy created (the
        forever-retained credential columns on the revision row itself are the
        product-side record; the ledger row is dev-infra metadata purged at
        >=24h, Q2 = A)."""
        if _idem_key is None or rev is None:
            return
        db.execute(text(
            "UPDATE stack_deploy_idempotency_keys SET revision_id = :r "
            "WHERE tenant_id = :t AND stack_id = :s AND idem_key = :k"),
            {"t": stack.tenant_id, "s": stack.id, "k": _idem_key,
             "r": rev.id if hasattr(rev, "id") else rev})

    # 30.6.3 c3-J4b2 — `image_overrides` are checked HERE, before the live-in-
    # flight early return below and before any task/revision write, so a denied
    # override leaves 0 new rows (ApiTaskQueue, PersistentStackRevision). The
    # refs themselves are never logged and never echoed — the helper's 403
    # `image_override_denied` carries only the reason.
    if image_overrides:
        # current_refs comes from the SAME compose the deploy gate below
        # re-validates (`stack.compose_spec`, read by `_validate_compose` at
        # :377 and by `_compose_yaml_with_vars` at :530): services live in
        # spec["services"] and a service's image is its `image` key
        # (routers/persistent_stacks/_shared.py:3415 `svc.get("image")`). A
        # build-only service has no `image:` key, so it is ABSENT here and an
        # override for it takes the helper's not-in-current branch → 403.
        current_refs = {name: str(svc.get("image"))
                        for name, svc in ((stack.compose_spec or {}).get("services")
                                          or {}).items()
                        if isinstance(svc, dict) and svc.get("image")}
        allow = _sdp.load_registry_allow(db)
        _sdp.check_overrides(image_overrides, current_refs, allow)

        # 30.6.3 c3-J4c — replay lookup comes AFTER the override policy (so a
        # denied deploy still records nothing, Q4 = A) and BEFORE the live-in-
        # flight early return below.
        if _idem_key is not None and _idem_replay():
            return stack

        _prior = psd_crud.list_nonterminal_deploy_tasks(db, stack.id)
        _now = datetime.now(timezone.utc)
        if [t for t in _prior
            if t.status in (models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING)
            and t.claim_expires_at and t.claim_expires_at > _now]:
            # A worker is actively `compose up`-ing this stack right now. An
            # override changes WHAT that deploy carries, so the idempotent
            # "return the stack as-is" answer below is not acceptable here —
            # refuse and write nothing (an unclaimed prior task keeps today's
            # supersede-and-redeploy behaviour, and the no-body early return
            # stays byte-identical so J4a's re-mint keeps working).
            raise ApiError(409, "deploy_in_progress",
                           "a deploy is already in flight for this stack")

        # Policy passed — apply the new spec through the SAME revision path the
        # PUT .../definition edit route uses (persistent_stacks.py:792
        # `create_revision(origin=ORIGIN_EDITED)` + the live-row write at
        # :804-814), so /revert keeps working and history is never rewritten.
        # Same "make sure a chain exists" step the PUT .../definition edit route
        # takes (persistent_stacks.py:712), so the pre-override spec is revision
        # 1 and /revert can go back to it (O10).
        _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)
        new_spec = copy.deepcopy(stack.compose_spec or {})
        for name, new_ref in image_overrides.items():
            new_spec["services"][name]["image"] = new_ref
        _pre_edit_compose = copy.deepcopy(stack.compose_spec)
        if _idem_key is not None and not _idem_claim():
            _idem_lost()
            return stack
        _req_key = get_api_key()
        _key_id = key_id if key_id is not None else getattr(_req_key, "id", None)
        rev = _psr.create_revision(
            db, stack,
            origin=_psr.ORIGIN_EDITED,
            author_user_id=getattr(user, "id", None),
            compose_spec=new_spec,
            storage_class=stack.storage_class,
            message="image_overrides via deploy",
            # 30.6.3 c3-J4c — the revision names the credential that carried
            # this deploy, so /revert can undo a credential-driven deploy.
            idempotency_key=_idem_key,
            key_id=_key_id,
        )
        stack.compose_spec = new_spec
        stack.accelerator_requirements = _parse_accelerator_requirements(new_spec) or None
        stack.egress_policy = _parse_egress_policy(new_spec) or None
        stack.secret_refs = _parse_secret_refs(new_spec) or None
        stack.tls_config = _parse_tls_config(new_spec) or None
        db.add(stack)
        _audit_stack(db, "STACK_IMAGE_OVERRIDES_APPLIED", stack, user,
                     {"revision_no": rev.revision_no,
                      "services": sorted(image_overrides),
                      "count": len(image_overrides)})
        _record_egress_declarations(db, stack, _pre_edit_compose,
                                    getattr(user, "id", None))
        db.commit()
        db.refresh(stack)
        _idem_attach(rev)
        db.commit()
        logger.info("persistent stack %s deploy applied %s image override(s) on services "
                    "%s (revision %s)", stack.id, len(image_overrides),
                    sorted(image_overrides), rev.revision_no)

    # stack-stop-release §3 — idempotent deploy. Two POSTs close together (a
    # UI double-click, or a retry racing the first request) must not each
    # queue their own deploy task for the same stack — that is exactly how
    # incident 2026-09-28 produced tasks 39852/39856 for stack #25/#27.
    #
    # An unclaimed (PENDING/ASSIGNED) prior task is stale the instant a fresh
    # deploy is requested — its compose spec may already be superseded by
    # what this call is about to (re)validate and dispatch, so cancel it
    # rather than leave two deploys racing to be claimed.
    #
    # A CLAIMED/RUNNING one is a worker actively doing `compose up` for this
    # same stack right now. If its claim is still live, this request IS that
    # deploy — answered idempotently by returning the stack as-is instead of
    # piling a second task on top of a live one. If the claim already
    # expired, the worker that held it is presumed gone (crashed, lost
    # network) and the task would otherwise hold its gpu_assignment in
    # `gpu_reservation.cards_in_use()` forever with nothing left to complete
    # it — cancel and supersede, same as the reconciler backstop would
    # eventually do.
    # 30.6.3 c3-J4c — the replay lookup for a keyed deploy with NO overrides
    # (which creates no revision today, so the revision columns alone cannot
    # dedup it) lives here, after the policy and before the live-in-flight
    # early return: the n8 ledger posture (Q1 = C) covers both shapes.
    if _idem_key is not None and not _idem_won and _idem_replay():
        # c3-J4c-fix: an override deploy already claimed the key earlier in THIS
        # request (:511) and committed it, so the ledger row is visible here —
        # that is not a replay, it is the winner continuing to queue its own
        # task. Only a request that never won the claim may take the replay
        # answer.
        return stack

    existing = psd_crud.list_nonterminal_deploy_tasks(db, stack.id)
    if existing:
        now = datetime.now(timezone.utc)
        live_inflight = [
            t for t in existing
            if t.status in (models.ApiTaskStatus.CLAIMED, models.ApiTaskStatus.RUNNING)
            and t.claim_expires_at and t.claim_expires_at > now
        ]
        if live_inflight:
            logger.info("persistent stack %s: deploy already in flight (task %s, %s) "
                       "— not queuing a duplicate", stack.id, live_inflight[0].id,
                       live_inflight[0].status.value)
            return stack
        _cancel_superseded_deploy_tasks(
            db, stack.id,
            reason="cancelled: superseded by a new deploy request for the same stack")

    _validate_compose(stack.compose_spec,   # §7.3 — final gate before dispatch
                      is_admin=bool(getattr(user, "is_platform_admin", False)),
                      # §3.5 — this re-validates STORED data (nothing the caller
                      # supplied), so a prior admin acknowledgement embedded in
                      # it is trusted rather than treated as forged.
                      trust_admin_ack=True,
                      # FX.80 p4b — the deploy gate needs the OWNING tenant so
                      # it can tell a tenant stack (cache mounts must be RO)
                      # from a system/platform stack (exempt).
                      tenant_id=stack.tenant_id, db=db)

    # Placement is a claim-time decision, not a dispatch-time one. The deploy
    # goes into the queue carrying the stack's aggregate accelerator demand,
    # and whichever worker satisfies it claims the work — exactly how node
    # tasks are matched on gpu_variants. Previously this called _pick_worker
    # ("first LICENSED worker") and pinned, which could put a 48 GB model on a
    # CPU-only box; a guard alone could not fix that, because a pinned task can
    # only ever be vetoed, never re-homed.
    #
    # `to_worker` keeps explicit targeting available for "run it on that box".
    #
    # §8.3 — a host pin outranks claim-time placement. Without this a pinned
    # stack survives stop/start (which reuses target_worker) but silently
    # escapes its pin on redeploy, because an unpinned deploy is placed by
    # whichever worker claims it. "The pin holds unless you redeploy" is not a
    # pin. An explicit `to_worker` still wins: that is a person overriding
    # their own constraint deliberately, in one call, and §8.4 reports the
    # resulting mismatch on every read rather than hiding it.
    pinned = getattr(stack, "pinned_worker", None)
    if pinned and not to_worker:
        to_worker = pinned
        logger.info("persistent stack %s deploy targeted at pinned host %s",
                    stack.id, pinned)

    if to_worker:
        target = psd_crud.get_licensed_worker_by_name(db, to_worker)
        if not target:
            # §8.4 — refuse, never re-place. An unavailable pinned host is
            # reported with the pin named as the cause, because the operator
            # asserted a constraint the placement model cannot see and deploying
            # elsewhere would convert their instruction into a silent relocation
            # onto the machine they were avoiding.
            if pinned and to_worker == pinned:
                raise HTTPException(
                    status_code=409,
                    detail=(f"This stack is pinned to {pinned!r}, which is not "
                            f"available. It will NOT be deployed elsewhere — "
                            f"the pin is the constraint. Restore that host, or "
                            f"clear the pin, or pass to_worker to override this "
                            f"deploy deliberately."))
            raise HTTPException(status_code=409, detail=f"Worker {to_worker!r} is not available")
        # Fail fast when the target's hardware can never run this stack (wrong
        # GPU kind, too few/too small cards, or its only card of that kind is
        # quarantined). Otherwise the task sits ASSIGNED forever: the claim path
        # withholds a quarantined kind's queue, so nothing ever picks it up and
        # the stack shows "deploying" with no reason given.
        from utils.accelerator_fit import hardware_gap, installed_devices
        _devs, _dead = installed_devices(db, target)
        gap = hardware_gap(_devs, _aggregate_accelerator_demand(
            stack.accelerator_requirements or {}), _dead)
        if gap:
            raise HTTPException(
                status_code=409,
                detail=f"{to_worker} can't run {stack.name}: {gap}. "
                       + (f"The stack is pinned there — pick another system "
                          f"in the Worker column." if to_worker == pinned else
                          "Pick a system with the required hardware."))
        # D76 (owner 11-18) — the busy-port check must run when the user hits
        # Deploy, against the host the stack can ACTUALLY land on, and name the
        # holder. The unpinned branch below already answers it via
        # `pick_worker_for_host_ports` (re-home onto a free worker, refuse only
        # when every eligible one is busy). A named target — a `pinned_worker`
        # or an explicit `to_worker` — has nowhere to re-home to, so without
        # this the deploy queues and the claim-time guard parks the stack at
        # "deploying" for the 10-minute give-up window before the row shows
        # anything. Refuse NOW: no task row, no revision, no status change,
        # the stack keeps its previous state (created/running/stopped).
        # Fail-open (no heartbeat data / probe error) -> proceed as today.
        from services import stack_port_placement as _spp_deploy
        _port_msg = _spp_deploy.deploy_host_port_refusal(
            db, stack.compose_spec, target, exclude_stack_ids=[stack.id])
        if _port_msg:
            raise HTTPException(status_code=409, detail=_port_msg)
    else:
        # Fail fast rather than enqueueing into an empty fleet. This is a
        # sanity check on the fleet, NOT a placement decision.
        if not psd_crud.first_licensed_worker(db):
            raise HTTPException(status_code=409, detail="No approved worker is available to host the stack: add or approve a worker (Dashboard > Workers)")
        # gw-stack-networking GW.3 — a FIXED host port is a placement resource,
        # not just a deploy-time wish. Without this a stack requesting a host
        # port that is in use (or reserved by another stack) on EVERY eligible
        # worker would be enqueued and sit PENDING with no reason to read, or
        # land on one host and fail at `compose up` with "port is already
        # allocated". The picker picks a host where the port is free and pins
        # the deploy there; with no such host the deploy is refused with the
        # port named. `host: "any"` never blocks (the worker allocates).
        from services import stack_port_placement
        _w, _port_reason = stack_port_placement.pick_worker_for_host_ports(
            db, stack.compose_spec, home_zone=stack.home_zone,
            exclude_stack_ids=[stack.id])
        if _port_reason is not None:
            raise HTTPException(status_code=409, detail=_port_reason)
        if _w is not None:
            to_worker = _w.worker_name
            target = _w
        else:
            # FX bug 996 (stack 114 outage 10-12) — an unpinned redeploy with
            # no fixed host port to place by landed on whichever worker won
            # the claim race, with zero regard for which host already has the
            # stack's images. `target_worker` is this row's "last deployed
            # on" field (set by `_apply_stack_task_result` from the actual
            # claimer on a prior success — see its docstring); it is read
            # HERE, before the unconditional `stack.target_worker = to_worker`
            # below clears it for this new attempt. If that last host is
            # still LICENSED (and not paused/draining), pin the redeploy
            # there so it keeps reusing the host that already has its images
            # instead of re-rolling placement from scratch. An explicit
            # `to_worker`/`pinned_worker` (handled above) still wins, a first
            # deploy (no prior host) falls through unpinned exactly as
            # before, and a last host that is gone/unlicensed/paused also
            # falls through unpinned (today's behaviour).
            _last_host = stack.target_worker
            if _last_host:
                _sticky = psd_crud.get_licensed_worker_by_name(db, _last_host)
                # Broken-host guard: `target_worker` only moves on SUCCESS, so
                # after a failed attempt it still names the last host that
                # WORKED, not the host that just failed — sticking to it is
                # still correct unless that failed attempt was ITSELF on this
                # exact host (e.g. a persistently broken compose socket),
                # which would otherwise retry the same broken host forever.
                _last_task = psd_crud.get_last_terminal_deploy_task(db, stack.id)
                _last_tried_broken = bool(
                    _last_task is not None
                    and _last_task.status == models.ApiTaskStatus.FAILED
                    and (_last_task.claimed_by_worker or _last_task.assigned_to_worker)
                        == _last_host)
                if _sticky is not None and not getattr(_sticky, "paused", False) \
                        and not _last_tried_broken:
                    to_worker = _sticky.worker_name
                    target = _sticky
                    logger.info(
                        "stack %s: redeploy pinned to last host %s (sticky)",
                        stack.name, _sticky.worker_name)
                elif _last_tried_broken:
                    logger.info(
                        "stack %s: last host %s just failed a deploy on it "
                        "(task %s) — not sticking, falling back to claim-time "
                        "placement", stack.name, _last_host, _last_task.id)

    try:
        from services import stack_observation as _so
        _so.claim_free_project_name(db, stack)   # D76: avoid orphan projects
    except Exception:  # noqa: BLE001 — naming must never block a deploy
        logger.debug("free project-name check failed for stack %s", stack.id,
                     exc_info=True)
    project = _project_name(stack)
    demand = _aggregate_accelerator_demand(stack.accelerator_requirements or {})
    _mesh = _provision_mesh_identity(db, stack)   # mint per-service identity (no-op unless configured)
    # caddy-stack-ingress binding architecture — persist the deterministic,
    # collision-proof internal names (t<tid>-<stackid>-<svc>) for every reachable
    # service, so the stable INTERNAL address always exists and is queryable even
    # on a single-host install with no bundled control server (there it resolves
    # as a docker network alias rather than a MagicDNS node). `_stack_mesh_endpoints`
    # merges these names over any node_id/status just minted above, so this never
    # clobbers a real mint record — it only fills in the deterministic name.
    stack.mesh_endpoints = _stack_mesh_endpoints(stack) or None
    # GX.S10a — a secret_file/storage_file stack needs a worker release that can
    # materialise file secrets; refuse (409) before dispatch when a NAMED target
    # is known to lack it (claim-time placement cannot know, so it is allowed
    # through and the claim guard + worker failure surface the gap).
    if _stack_wants_file_materialisation(stack):
        # `target` is only bound on the named/pinned paths above; claim-time
        # placement (no named worker) resolves to None → the gate allows
        # dispatch (see _require_worker_file_secrets).
        _require_worker_file_secrets(locals().get("target"))
    _compose, _var_bundle = _compose_yaml_with_vars(
        stack, *_mesh,
        # 13.5a — db+user let the resolver handle NAMED tenant secrets
        # (tenant-scoped) and enforce the deploy-time tenant-prefix guard on
        # raw vault: paths for non-admin authors.
        secret_env=_resolve_stack_secrets(stack, db=db, user=user), db=db,
        worker=locals().get("target"))
    if _idem_key is not None:
        # Claim AFTER the live-in-flight early return so a replay never reaches
        # here, and in the SAME transaction as the deploy task below (one
        # commit) — a failed/denied deploy records nothing (S6).
        if not _idem_claim():
            _idem_lost()
            return stack

    _task = _dispatch_stack_task(db, to_worker, stack.tenant_id, "deploy", project,
                                 _compose,
                                 stack_id=stack.id, accel_demand=demand,
                                 extra_payload=_sv.payload_files(_var_bundle))
    # target_worker is recorded by the completion handler from whichever worker
    # actually claimed it — the row then reflects what happened rather than what
    # was guessed. Cleared here so a redeploy doesn't advertise a stale host.
    stack.target_worker = to_worker
    stack.lb_endpoint = None
    from services.stack_backend_dispatch import is_k8s_task
    if not is_k8s_task(_task):
        stack.status = "deploying"
    # The operator asking for a deploy IS the statement of intent. Set here
    # rather than only at creation so redeploying a stack that was previously
    # stopped resumes reconciling it, instead of leaving the reconciler
    # convinced it is meant to stay down.
    stack.desired_state = "running"
    # A redeploy after a failed attempt starts clean — the old reason describes
    # the previous try and would otherwise linger on a now-healthy stack.
    stack.status_reason = None
    # persistent-stack-billing §2.3 — snapshot the priced reservation vector at
    # deploy (re-captured on every redeploy, so a spec change can never leave the
    # snapshot stale) and seed the meter anchor to NOW so the deploying/wake
    # interval before this point is never billed.
    stack.reservation_units = _capture_reservation_units(stack)
    stack.metered_through_at = datetime.now(timezone.utc)
    refs = dict(stack.storage_refs or {})
    refs.setdefault("project", project)
    refs.setdefault("generation", 0)
    stack.storage_refs = refs
    _provision_storage(stack)   # §3.1/§3.3 — managed storage per storage_class
    # Surface the capacity trade at the point of decision. On a single-GPU
    # site, hosting managed AI means no accelerated node tasks run there at
    # all — legitimate, but the operator must be told rather than discover it
    # through a queue that silently never drains.
    warnings = []
    if demand:
        try:
            from utils.accelerator_fit import would_exhaust_accelerators
            candidates = ([target] if to_worker else psd_crud.list_licensed_workers(db))
            for w in candidates:
                msg = would_exhaust_accelerators(db, w, demand)
                if msg:
                    warnings.append(msg)
        except Exception as e:  # noqa: BLE001
            logger.debug("capacity warning computation failed for stack %s: %s", stack.id, e)
    for msg in warnings:
        logger.warning("persistent stack %s: %s", stack.id, msg)

    # 30.6.3 c3-J4c — emitted ONCE per keyed deploy that actually queued work
    # (never on a replay, never on a denial). The raw key is NEVER in the
    # details: sha256 + key_id + request_hash only (S2).
    if _idem_key is not None:
        _rev_key = locals().get("rev")
        _req_key = get_api_key()
        _audit_stack(db, "STACK_DEPLOY_KEY_USED", stack, user,
                     {"key_id": (key_id if key_id is not None
                                  else (getattr(_req_key, "id", None)
                                        if _req_key is not None else None)),
                      "key_hash": hashlib.sha256(_idem_key.encode("utf-8")).hexdigest(),
                      "request_hash": _idem_req_hash,
                      "revision_no": (getattr(_rev_key, "revision_no", None)
                                      if _rev_key is not None else None)})
        logger.info("persistent stack %s deploy queued with idempotency key hash %s "
                    "(key_id=%s)", stack.id,
                    hashlib.sha256(_idem_key.encode("utf-8")).hexdigest()[:8],
                    (getattr(_req_key, "id", None) if _req_key is not None else None))

    _audit_stack(db, "STACK_DEPLOYED", stack, user,
                 {"worker": to_worker or "(claim-time)", "accel_demand": demand or None,
                  "capacity_warnings": warnings or None})
    db.commit()
    db.refresh(stack)
    _publish_event("task:available:CPU")
    logger.info("persistent stack %s deploy queued (target=%s demand=%s storage_class=%s)",
                stack.id, to_worker or "any capable worker", demand or "none", stack.storage_class)
    return stack


# =============================================================================
# teardown_stack — POST /{stack_id}/teardown
# =============================================================================

def teardown_stack(db: Session, stack_id: int, user: "models.User",
                   data_policy: Optional[str] = "erase"):
    from routers.persistent_stacks._shared import (
        _get_owned, _revoke_mesh_identity, _dispatch_stack_task, _project_name,
        _publish_event, _cleanup_storage, _audit_stack, logger,
    )

    if data_policy not in ("erase", "retain"):
        raise HTTPException(status_code=400,
                            detail=f"data_policy must be 'erase' or 'retain', got {data_policy!r}")

    stack = _get_owned(db, stack_id, user)
    worker_name = stack.target_worker

    # A deploy that hasn't been claimed yet must be cancelled, or it lands
    # AFTER this teardown and leaves containers nothing is tracking. This
    # became reachable when deploys stopped being pinned: target_worker is
    # unknown until a worker claims, so "teardown before the deploy ran" is a
    # real window rather than a theoretical one.
    outstanding = psd_crud.list_outstanding_deploy_tasks(db, stack.id)
    for t in outstanding:
        t.status = models.ApiTaskStatus.CANCELLED
        t.error_message = "cancelled: stack torn down before deploy was claimed"
    if outstanding:
        logger.info("persistent stack %s teardown cancelled %d unclaimed deploy task(s)",
                    stack.id, len(outstanding))

    # A deploy already CLAIMED or RUNNING cannot be recalled — the worker may
    # be mid `compose up`. We don't know where it landed (that's recorded on
    # completion), so flag for the cleanup sweep rather than pretend it's gone.
    inflight = psd_crud.count_inflight_deploy_tasks(db, stack.id)

    if worker_name:
        _revoke_mesh_identity(db, stack)   # delete overlay nodes (no-op unless configured)
        _dispatch_stack_task(db, worker_name, stack.tenant_id, "teardown",
                             _project_name(stack), stack_id=stack.id,
                             extra_payload={"data_policy": data_policy})
        _publish_event("task:available:CPU")
    elif inflight:
        stack.cleanup_pending = True
        stack.status_reason = ("torn down while a deploy was in flight; containers may "
                               "exist on the claiming worker")
        logger.warning("persistent stack %s torn down with %d in-flight deploy(s) and no "
                       "known host — flagged cleanup_pending", stack.id, inflight)

    # GA.ASW.8 — data policy for teardown. "erase" (default) reclaims EVERYTHING,
    # the pre-existing behaviour. "retain" keeps the LAST *successful* snapshot
    # until now + STACK_SNAPSHOT_RETENTION days (parsed as days; 30 when unset)
    # by stamping `retain_until` on its storage_refs record — the erase path
    # (teardown _cleanup_storage and the retention prune) skips any snapshot
    # whose `retain_until` is still in the future. Nothing is hard-deleted
    # either way; retain is a matter of how long the copy is protected.
    #
    # The mark happens BEFORE the storage cleanup below: a snapshot taken but
    # never confirmed by its worker task (status_reason = "<op> failed") has no
    # data in MinIO to protect, so it is not retained — and teardown still
    # removes its (absent) objects as it always has.
    retain_report = None
    if data_policy == "retain":
        # GS.PSR.8 — resolved through the platform-settings registry
        # (stacks.snapshot_retention_days: DB > env > 30), read per
        # teardown so a UI change takes effect without a restart.
        # GT.D5 — the reader (and the setting's registration) moved to the
        # paid module with the backup config surface; CE has no knob to
        # read, so retain keeps its documented 30-day default.
        _paid = _paid_import.optional_paid(
            "services.persistent_stacks_paid_service")
        retention_days = (_paid.snapshot_retention_days(db)
                          if _paid is not None else 30)
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        retain_report = _retain_last_successful_snapshot(
            stack, now + timedelta(days=retention_days), retention_days)
        if retain_report:
            db.commit()
            db.refresh(stack)

    # §3.5 — reclaim managed storage: MinIO snapshots + the s3-native bucket (the
    # worker's `compose down -v` removes the local volumes). Skips snapshots
    # stamped `retain_until` in the future by the retain policy above.
    # GA.ASW.8 — the policy must reach the storage cleanup: with "retain" the
    # s3-native bucket (and its objects) must NOT be removed.
    cleanup = _cleanup_storage(stack, data_policy=data_policy)
    stack.status = "torn_down"
    # §2.3 — record that this stack is DOWN ON PURPOSE. Without it the
    # reconciler cannot distinguish a teardown from a crash, and would treat
    # every deliberate stop as a gap to close: the operator tears a stack down,
    # the reconciler puts it straight back, and neither side can win.
    stack.desired_state = "stopped"
    _audit_stack(db, "STACK_TORN_DOWN", stack, user,
                 {"storage_cleanup": cleanup, "cancelled_deploys": len(outstanding),
                  "inflight_deploys": inflight, "data_policy": data_policy,
                  "retained_snapshot": retain_report})
    db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s teardown dispatched (host=%s); data_policy=%s; "
                "storage cleanup=%s; retained=%s",
                stack.id, worker_name or "unknown", data_policy, cleanup,
                retain_report)
    return stack


def _retain_last_successful_snapshot(stack: "models.PersistentStack",
                                     deadline: "datetime",
                                     retention_days: int) -> Optional[dict]:
    """GA.ASW.8 — mark the most recent confirmed snapshot `retain_until` the
    deadline (soft-keep: no objects are deleted, only the erase paths learn to
    skip it). A snapshot is "successful" when the worker's completion handler
    did NOT leave a failure on the stack (`<op> failed` in status_reason).

    Returns a small report (None when there is nothing to retain — no snapshot
    records, or no confirmed one). Does NOT commit; the caller owns the
    transaction. Reuses the snapshot records' existing `created_at`/`source`/
    `verified_at` bookkeeping inside `storage_refs` — deliberately NOT a new
    column: retention is per-SNAPSHOT metadata, and storage_refs is where
    every other per-snapshot fact already lives.

    "Successful" = not the snapshot whose worker task last failed. The
    completion handler records a snapshot op's failure as
    `status_reason = "snapshot failed"` on the stack (there is no per-snapshot
    status column to write to), so when that marker is present the NEWEST
    record is unconfirmed and we fall back to the next-newest. With only one
    record and a failed marker there is nothing confirmed to keep."""

    refs = dict(stack.storage_refs or {})
    snaps = refs.get("snapshots") or {}
    if not snaps:
        return None
    ordered = sorted(snaps.items(), key=lambda kv: kv[1].get("created_at", ""))
    newest_unconfirmed = (stack.status_reason == "snapshot failed")
    if newest_unconfirmed and len(ordered) < 2:
        return None
    snap_id, snap = ordered[-2] if newest_unconfirmed else ordered[-1]
    snap = dict(snap)
    snap["retain_until"] = deadline.isoformat()
    snap["retain_source"] = "teardown_retain"
    new_snaps = dict(snaps)
    new_snaps[snap_id] = snap
    new_refs = dict(refs)
    new_refs["snapshots"] = new_snaps
    stack.storage_refs = new_refs
    return {"snapshot_id": snap_id,
            "retain_until": snap["retain_until"],
            "retention_days": retention_days,
            "created_at": snap.get("created_at")}


# =============================================================================
# set_host_pin — PUT /{stack_id}/host-pin
# =============================================================================

def set_host_pin(db: Session, stack_id: int, body, user: "models.User"):
    from routers.persistent_stacks._shared import _get_owned, _aggregate_accelerator_demand, _audit_stack, logger

    stack = _get_owned(db, stack_id, user)

    if body.clear or not body.worker:
        stack.pinned_worker = None
        stack.pinned_reason = None
        stack.pinned_at = None
        stack.pinned_by_user_id = None
        db.commit()
        _audit_stack(db, "STACK_HOST_PIN_CLEARED", stack, user, {})
        db.commit()
        return {"stack_id": stack.id, "pinned_worker": None}

    worker = psd_crud.get_worker_by_name(db, body.worker)
    if worker is None:
        raise HTTPException(status_code=404,
                            detail=f"No worker named {body.worker!r}")

    # REFUSE when the host's hardware can never run it — no GPU of the right
    # kind, too few, or cards too small. Such a pin yields a stack reported
    # unplaceable forever; the picker already greys these hosts out, and this
    # is the same rule enforced for any other caller.
    from utils.accelerator_fit import hardware_gap, installed_devices
    demand = _aggregate_accelerator_demand(stack.accelerator_requirements or {})
    _devs, _dead = installed_devices(db, worker)
    gap = hardware_gap(_devs, demand, _dead)
    if gap:
        raise HTTPException(
            status_code=409,
            detail=f"{body.worker} can't run {stack.name}: {gap}. "
                   f"Pick a system with the required hardware.")

    # Warn, do not block, when the host HAS the hardware but cannot currently
    # host it — a worker that is temporarily full may free up. §8.4 reports the
    # consequence on every read, so this never becomes an invisible
    # misconfiguration.
    fits = True
    if demand:
        try:
            from utils.accelerator_fit import worker_can_host, resolve_stack_demand
            # CANDIDATE demand for the named stack — resolve to measured serving
            # VRAM (behind FIT_GATE_MEASURED_VRAM; no-op when off / unprofiled).
            demand = resolve_stack_demand(db, stack.name, demand)
            fits = worker_can_host(db, worker, demand, exclude_stack_id=stack.id)
        except Exception:  # noqa: BLE001
            logger.debug("host-pin fit check failed for stack %s", stack.id,
                         exc_info=True)

    stack.pinned_worker = body.worker
    stack.pinned_reason = body.reason
    stack.pinned_at = datetime.now(timezone.utc)
    stack.pinned_by_user_id = user.id
    db.commit()
    _audit_stack(db, "STACK_HOST_PINNED", stack, user,
                 {"worker": body.worker, "reason": body.reason, "fits_now": fits})
    db.commit()

    return {
        "stack_id": stack.id,
        "pinned_worker": stack.pinned_worker,
        "pinned_reason": stack.pinned_reason,
        "fits_now": fits,
        "warning": (None if fits else
                    f"{body.worker} cannot currently satisfy this stack's "
                    f"accelerator demand. The pin is recorded and will be "
                    f"honoured — the stack will be reported unplaceable rather "
                    f"than moved elsewhere."),
    }


# =============================================================================
# set_gpu_pin — PUT /stacks/{stack_id}/gpu-pin (GPUCAP P1, admin-only)
#
# A per-SERVICE GPU CARD PIN, stacked on top of the host pin: cards are only
# meaningful on one worker, so a stack with no `pinned_worker` has nothing to
# pin. Stored as the top-level compose extension `x-mod.gpu_pin`
# ({service: [card_id, ...]}) in a NEW revision — the `x-mod.host_ports` idiom,
# deliberately NOT inside `accelerator_requirements` (which seven+ writers
# re-parse and would silently wipe) and NOT a new column (no migration).
#
# The pin is an ALLOWED SET, not an assignment: the claim-time planner
# (`services/gpu_reservation.plan_service_gpu_assignments`) intersects the
# worker's already-filtered card pool with these ids and then applies its
# normal exclusive / dedicated-tenant / scrub rules unchanged, so a pin can
# never bypass `admin_disabled`, tenant isolation or scrub. A longer list than
# the service's `count` is a pool the planner still picks `count` of.
#
# Does NOT redeploy and does NOT move the stack — same posture as host-pin:
# placement and relocation are separate acts, and the next Deploy applies the
# pin. Validation here is write-time so the admin sees a too-small / wrong-kind
# / disabled card immediately instead of an UNPLACEABLE retry loop.
# =============================================================================

def set_gpu_pin(db: Session, stack_id: int, body, user: "models.User"):
    from routers.persistent_stacks._shared import (
        _get_owned, _audit_stack, _parse_accelerator_requirements, logger)
    from routers.worker_tasks import _get_authorized_tenants, _is_tenant_authorized
    from utils.accelerator_quarantine import UNSCHEDULABLE_STATUSES
    from utils.accelerator_fit import _VRAM_SLACK_GB
    from services.gpu_reservation import cards_in_use
    import services.persistent_stack_revisions as _psr
    import copy
    import models

    stack = _get_owned(db, stack_id, user)
    spec = copy.deepcopy(stack.compose_spec or {})
    top_xmod = dict(spec["x-mod"]) if isinstance(spec.get("x-mod"), dict) else {}
    had_pin = "gpu_pin" in top_xmod

    # --- clear path(idempotent: clearing an unpinned stack changes nothing) ---
    if body.clear or not body.pin:
        if not had_pin:
            return {"stack_id": stack.id, "gpu_pin": None,
                    "worker": stack.pinned_worker, "revision_no": None,
                    "warning": None}
        top_xmod.pop("gpu_pin", None)
        if top_xmod:
            spec["x-mod"] = top_xmod
        else:
            # The `x-mod` dict did not exist before a pin and is now empty: drop
            # the empty top-level key. Any other x-mod key (host_ports, ingress,
            # ports, secrets) keeps the dict and stays untouched.
            spec.pop("x-mod", None)
        rev = _psr.create_revision(
            db, stack,
            origin=_psr.ORIGIN_EDITED,
            author_user_id=user.id,
            compose_spec=spec,
            storage_class=stack.storage_class,
            message="gpu pin cleared")
        stack.compose_spec = spec
        db.add(stack)
        _audit_stack(db, "STACK_GPU_PIN_CLEARED", stack, user,
                     {"revision_no": rev.revision_no, "reason": body.reason})
        db.commit()
        db.refresh(stack)
        return {"stack_id": stack.id, "gpu_pin": None,
                "worker": stack.pinned_worker, "revision_no": rev.revision_no,
                "warning": None}

    # --- a pin needs a host: card ids are per-worker ---
    if not stack.pinned_worker:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_host_pin",
                    "detail": ("this stack has no host pin. Card ids name devices "
                               "on one specific worker, so a GPU card pin only means "
                               "anything once the stack is pinned to a host. "
                               "Set the host pin first.")})

    worker = psd_crud.get_worker_by_name(db, stack.pinned_worker)
    if worker is None or getattr(worker, "revoked_at", None) is not None \
            or worker.status == models.WorkerStatus.OFFLINE:
        raise HTTPException(
            status_code=409,
            detail={"code": "worker_unavailable",
                    "detail": (f"host {stack.pinned_worker!r} is missing, revoked or "
                               f"offline; its cards cannot be pinned until it is "
                               f"licensed again.")})

    # Tenant fence: an admin may only pin cards of workers the STACK's tenant
    # may run tasks on. Reuses the claim-path source of truth
    # (`routers/worker_tasks._get_authorized_tenants` + `_is_tenant_authorized`,
    # the same pair `services/host_lockdown._authorized_tenant_ids` and the
    # claim guard at worker_tasks.py:10034 use) rather than a hand-rolled
    # `assigned_tenant_ids` check, so the pin can never claim a worker the
    # claim path would refuse (native / tenant_owned / platform_only rules
    # included).
    stack_tenant = stack.tenant_id if stack.tenant_id is not None else user.tenant_id
    authorized = _get_authorized_tenants(worker)
    if not _is_tenant_authorized(authorized, stack_tenant):
        raise HTTPException(
            status_code=403,
            detail={"code": "worker_not_permitted",
                    "detail": (f"host {stack.pinned_worker!r} is not authorized to "
                               f"serve this stack's tenant, so its cards cannot be "
                               f"pinned for it.")})

    # --- per-service rules (2 and 4) against the compose's own declarations ---
    declared = _parse_accelerator_requirements(spec)
    pin_in = body.pin
    if not isinstance(pin_in, dict):
        raise HTTPException(
            status_code=422,
            detail={"code": "pin_not_a_dict",
                    "detail": ("x-mod.gpu_pin must be an object mapping a service "
                               "name to a list of card ids.")})
    normalized: Dict[str, list] = {}
    for svc_name, cards in pin_in.items():
        name = str(svc_name)
        decl = declared.get(name)
        if decl is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "pin_service_unknown",
                        "detail": (f"service {name!r} does not declare "
                                   f"x-mod.accelerator in this stack, so it has no "
                                   f"GPU cards to pin.")})
        if not isinstance(cards, list) or not cards:
            raise HTTPException(
                status_code=422,
                detail={"code": "pin_card_invalid",
                        "detail": (f"service {name!r}: the pin must be a non-empty "
                                   f"list of card ids.")})
        ids = []
        seen = set()
        for raw in cards:
            cid = str(raw).strip() if raw is not None else ""
            if not cid or len(cid) > 128:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "pin_card_invalid",
                            "detail": (f"service {name!r}: a card id must be a "
                                       f"non-empty string of at most 128 characters.")})
            if cid in seen:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "pin_card_invalid",
                            "detail": (f"service {name!r}: card id {cid!r} is "
                                       f"listed twice.")})
            seen.add(cid)
            ids.append(cid)
        if len(ids) < int(decl.get("count") or 1):
            raise HTTPException(
                status_code=422,
                detail={"code": "pin_count_short",
                        "detail": (f"service {name!r} declares count "
                                   f"{int(decl.get('count') or 1)} but only "
                                   f"{len(ids)} card(s) are pinned.")})
        normalized[name] = ids

    # --- card lookup: ONLY this worker's hardware profile, never by id across
    #     workers. An id that is not here is `unknown_card` with identical text
    #     whether it exists on another worker or nowhere at all, so the error
    #     cannot be used to probe other tenants' hardware. ---
    rows = []
    if getattr(worker, "hardware_profile_id", None):
        rows = (db.query(models.AcceleratorProfile)
                .filter(models.AcceleratorProfile.hardware_profile_id ==
                        worker.hardware_profile_id)
                .all())
    by_id: Dict[str, models.AcceleratorProfile] = {}
    for row in rows:
        kind_norm = str(row.kind).strip().lower()
        card_id = row.uuid or f"{worker.worker_name}:{kind_norm}:{row.index}"
        by_id[str(card_id)] = row

    slack_gb = float(_VRAM_SLACK_GB)
    for svc_name, ids in normalized.items():
        decl = declared[svc_name]
        want_kind = decl.get("kind") or "cuda"
        min_gb = decl.get("min_vram_gb")
        for cid in ids:
            row = by_id.get(cid)
            if row is None:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "unknown_card",
                            "detail": (f"card id {cid!r} is not a card of host "
                                       f"{stack.pinned_worker!r}.")})
            if getattr(row, "admin_disabled", False) \
                    or getattr(row, "benchmark_status", None) in UNSCHEDULABLE_STATUSES \
                    or not row.vram_gb:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "card_disabled",
                            "detail": (f"card {cid!r} on host {stack.pinned_worker!r} "
                                       f"is admin-disabled, unschedulable or has no "
                                       f"known VRAM; it cannot be pinned.")})
            have_kind = str(row.kind).strip().lower()
            if have_kind != want_kind:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "card_kind_mismatch",
                            "detail": (f"service {svc_name!r} declares kind "
                                       f"{want_kind!r}; card {cid!r} is "
                                       f"{have_kind!r}.")})
            if min_gb is not None and float(row.vram_gb) + slack_gb < float(min_gb):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "card_too_small",
                            "detail": (f"service {svc_name!r} needs {min_gb:g} GB, "
                                       f"card {cid!r} has {float(row.vram_gb):g} GB "
                                       f"on host {stack.pinned_worker!r}.")})

    # Warn, do not block: a pinned card already held by something else is a
    # legal admin choice (the card frees later; the planner then waits). Same
    # warn-not-block posture as host-pin's `fits_now`.
    warning = None
    try:
        held = cards_in_use(db, [c for ids in normalized.values() for c in ids])
        busy = sorted(cid for cid, res in held.items() if res)
        if busy:
            warning = (f"pinned card(s) {', '.join(busy)} are currently in use by "
                       f"another holder. The pin is saved; the stack will wait for "
                       f"the card rather than being placed elsewhere.")
    except Exception:  # noqa: BLE001 — a warning read must never fail the write
        logger.debug("gpu-pin in-use read failed for stack %s", stack.id, exc_info=True)

    top_xmod["gpu_pin"] = normalized
    spec["x-mod"] = top_xmod
    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)
    rev = _psr.create_revision(
        db, stack,
        origin=_psr.ORIGIN_EDITED,
        author_user_id=user.id,
        compose_spec=spec,
        storage_class=stack.storage_class,
        message="gpu pin updated")
    stack.compose_spec = spec
    db.add(stack)
    _audit_stack(db, "STACK_GPU_PIN_SET", stack, user,
                 {"revision_no": rev.revision_no,
                  "gpu_pin": {k: list(v) for k, v in normalized.items()},
                  "reason": body.reason})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s gpu pin set on %s (revision %s) by user %s",
                stack.id, stack.pinned_worker, rev.revision_no, user.id)
    return {"stack_id": stack.id,
            "gpu_pin": normalized,
            "worker": stack.pinned_worker,
            "revision_no": rev.revision_no,
            "warning": warning}


# =============================================================================
# set_keep_running — PUT /stacks/{stack_id}/keep-running (30.6.7 c7-a2)
#
# The tenant-admin setter for the GX.S5 c7 hard floor. `keep_running` (models.py
# :8563, NOT NULL server_default false) is READ by GET /v1/llm/models (c7-a4) and
# by the eviction guard (c7-a6), but nothing wrote it — the collective's ask
# (30.6.7 (b)) had no way to set it. This is that setter: owner-only (the caller
# resolved the stack through `_get_owned`, so a foreign tenant's row already 404s
# before we get here), CE (no `_require_paid`), audited ONLY on a real state
# change (the design says audit on state change, so a same-value re-PUT is a
# 200 with no audit row — idempotent, no audit spam).
# =============================================================================

def set_keep_running(db: Session,
                     stack: "models.PersistentStack",
                     user: "models.User",
                     enabled: bool) -> "models.PersistentStack":
    """Set a stack's keep-running floor. Returns the refreshed stack row.

    A soft-deleted stack (status == "deleted") is refused 409 `stack_deleted`
    (c14a contract: `utils.http_errors.ApiError`, lower snake_case code) — the
    row is dead from the platform's view and re-pinning it would be a silent
    resurrection of a stack the operator ended.

    Commit + audit shape copied from `set_host_pin` above (:752 — mutate,
    `db.commit()`, `_audit_stack`, second `db.commit()`), which is this file's
    neighbouring setter; `db.refresh` follows `delete_stack`'s caller in the
    router so the returned row carries the persisted value.
    """
    from utils.http_errors import ApiError
    from routers.persistent_stacks._shared import _audit_stack, logger

    if stack.status == "deleted":
        raise ApiError(409, "stack_deleted", "Persistent stack is deleted")

    # Idempotent: only a real change gets an audit row (design: audit on state
    # change). Read the OLD value before assigning — `keep_running` is a plain
    # Boolean column, so the ORM value is already materialised on a fetched row.
    changed = bool(getattr(stack, "keep_running", False)) != bool(enabled)

    stack.keep_running = enabled
    db.commit()
    if changed:
        _audit_stack(db,
                     "STACK_KEEP_RUNNING_SET" if enabled
                     else "STACK_KEEP_RUNNING_CLEARED",
                     stack, user, {"keep_running": enabled})
        db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s keep_running set to %s by user %s",
                stack.id, stack.keep_running, user.id)
    return stack


# =============================================================================
# GT.D5 paid-module re-export shims
# =============================================================================
# The functions below moved to services/persistent_stacks_paid_service.py
# (managed AI, observability, fleet placement, predictive sleep). Every one
# is re-exported here, guarded, so the call sites that have NOT been repointed
# yet (DS-2/3/4 own those files: routers/persistent_stacks/persistent_stacks.py,
# routers/persistent_stacks/_shared.py, services/scheduler.py) keep importing
# cleanly, and so the module still imports on a CE build where the paid
# module is absent (the names simply don't resolve there — by design, the CE
# router answers 402 for those endpoints instead of calling them).
# Delete this block as DS-2/3/4 repoint each caller.

_PAID_REEXPORTS = (
    "SNAPSHOT_RETENTION_DAYS_SETTING_KEY",
    "snapshot_retention_days",
    "managed_ai_status",
    "provision_managed_ai",
    "wake_metrics",
    "get_rebalancer_config",
    "get_reconciler_config",
    "stack_observations",
    "egress_observations",
    "placement_overview",
    "backfill_stack",
    "migrate_stack",
    "placement_check",
    "get_llm_idle_settings",
    "update_stack_idle_override",
    "pin_stack_warm",
    "wake_stack",
)


def __getattr__(name):
    if name in _PAID_REEXPORTS:
        _paid = _paid_import.optional_paid(
            "services.persistent_stacks_paid_service")  # DS-2/3/4 repoint
        if _paid is None:
            # CE: the paid module is dropped. Raising AttributeError keeps this
            # module importable (the router answers 402 for these endpoints
            # before ever reaching the shim) instead of erroring at import time.
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r} "
                "(paid module absent in CE)")
        return getattr(_paid, name)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}")


# =============================================================================
# finalize_stack_meter — guarded no-op in CE
# =============================================================================

def finalize_stack_meter(db: Session, stack: "models.PersistentStack") -> Optional[Dict[str, Any]]:
    """CE-safe wrapper around the paid reservation meter.

    GT.D5 §2a — `stack_billing.finalize_stack_meter` is a Paid module, dropped
    from CE. The router's stop path (and the teardown path) call the final
    reservation charge on the way a stack goes down. On a full (paid) build this
    resolves to the real meter; on CE there is no meter to finalize, so this is
    a guarded no-op that returns None rather than raising. Callers already
    wrap this in try/except, so both shapes behave identically to today.
    """
    sb = _paid_import.optional_paid("services.stack_billing")
    if sb is None:
        return None  # CE — no reservation meter to finalize
    return sb.finalize_stack_meter(db, stack)
