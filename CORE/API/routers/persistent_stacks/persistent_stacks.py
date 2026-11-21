from __future__ import annotations
from ._shared import *  # noqa: F401,F403
from routers.persistent_stacks import (_require_platform_admin, _require_stack_author,  # noqa: F401  (defined in __init__; keeps __module__ stable)
                              _require_paid)

# Gate 2b: query-heavy handlers below delegate to crud/services (module-level
# import is safe here — neither module imports back into this package at
# import time; see services/persistent_stacks_domain_service.py docstring).
from crud import persistent_stacks_domain as _psd_crud
from services import persistent_stacks_domain_service as _psd_svc
# GT.D5 / DS-2 — CE carve: `services.stack_logical_backup` is a Paid module
# dropped from the CE tree, so its module-top import had to go. The pure
# `backup_ack_required` check is now resolved at call time through
# `_backup_ack_list` below (CE: no backup engine ⇒ nothing to acknowledge,
# so the audit just omits the ack fields). `_require_backup_ack` (the 409
# posture gate) still comes from `_shared` and is itself guarded there.
from utils.paid_import import optional_paid as _optional_paid

# GT.D5 §2b — the capability names the wholly-Paid endpoints below 402 on.
_PAID_SERVICE = "services.persistent_stacks_paid_service"

from fastapi import (  # noqa: F811  (local, prefix-free; the aggregator carries prefix/tags/deps)
    APIRouter,
    Body,
    Request,
)
from utils.http_errors import (  # 30.6.1 c1-J4b: ApiError carries stack_not_empty / stack_probe_unavailable
    ApiError,
    internal_http_error,
)
# FX.96 (Q82=A): Vault/secret paths in system logs are hashed, never printed.
from utils.secret_redaction import hash_for_log
# 30.6.3 c3-J2b: the six stack operational routes (GET /{stack_id}, deploy,
# revert, logs, backup, backups) also accept a raw stack-pinned API key / X-API-Key.
# Such keys are confined by services/app_key_scope.check_stack_key_scope over
# STACK_SCOPE_ALLOWLIST (own-stack pin + the key's stack:* scopes, default-deny);
# interactive Bearers keep
# every gate get_current_user runs (BUG-1027p: require_bearer_or_api_key now
# delegates its interactive branch to _resolve_current_user(require_terms=True)).
from dependencies import require_bearer_or_api_key
# 30.6.7 c7-a2 — the keep-running setter's body + response models live HERE
# (not schemas.py / _shared.py, which other lanes own). StrictBool so `"yes"`
# 422s; extra="forbid" so a stray key 422s (threat case 4).
from pydantic import (ConfigDict, StrictBool, constr, conlist,
                      field_validator as _pyd_field_validator)
mesh_internal_router = APIRouter()
# Machine-facing caddy-stack-ingress seams (no user session; guarded by the
# optional ingress token + network isolation). Mounted WITHOUT the router-wide
# stack:author gate — Caddy's on-demand-TLS `ask` carries no user credential.
ingress_router = APIRouter()
router = APIRouter()
stack_router = APIRouter()


def _ce_ha_class_default(compose: dict, explicit):
    """GT.D5 / DS-2 — CE fallback for the Paid `services.stack_ha_class`.

    On a full build the create paths call `resolve_create_ha_class` /
    `infer_ha_class` (GR.FEN.3a inference from mounts). On CE that module is
    dropped, so: an EXPLICIT class request (a confirmations override, or a
    compose-level `x-mod.ha_class` declaration) is a Pro ask → 402; otherwise
    fall back to the historical default class (`external`, what an unset
    ha_class always meant — see services/stack_ha_class.py header) with no
    inference.
    """
    _hc = _optional_paid("services.stack_ha_class")
    if _hc is not None:
        return None  # caller uses the real resolver
    top = compose.get("x-mod") if isinstance(compose, dict) else None
    declared = top.get("ha_class") if isinstance(top, dict) else None
    # Asking for the CE default ("", "external") is not a Pro ask; any other class is.
    _ce_ok = (None, "", "external")
    if explicit not in _ce_ok or declared not in _ce_ok:
        raise HTTPException(
            status_code=402,
            detail="HA-class posture inference is not available "
                   "in MOD Community Edition.")
    return "external", "CE default (no inference)"


def _backup_ack_list(compose: dict) -> list:
    """GT.D5 / DS-2 — CE-safe form of `stack_logical_backup.backup_ack_required`.

    The audit trail records WHICH known-database images a create acknowledged
    ("this stack runs postgres with no backup posture, and the operator said
    so"). That pure check lives in a Paid module dropped from the CE tree, so
    on CE there is no backup engine and nothing to acknowledge: return the
    empty list, which makes the create audits skip their `backup_ack*` keys —
    the same shape a stack with no database images already produces.
    """
    _lb = _optional_paid("services.stack_logical_backup")
    if _lb is None:
        return []
    return _lb.backup_ack_required(compose)

@router.get("/node-bindings", response_model=NodeBindingsResponse)
def node_stack_bindings(db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    """Which node kinds are tied to a managed stack, and each stack's live state
    (for the WorkflowBuilder node-stack overlay, generalized-stack-wake D12).

    One entry per node_kind that resolves to a managed stack, reusing
    stack_wake's resolver + row lookup (NO new mapping). Bounded (a small, fixed
    kind set) and defensive: any per-kind failure degrades that kind to "unknown"
    rather than failing the request — this endpoint never 500s."""
    return _psd_svc.node_stack_bindings(db, user)

@router.get("/managed-ai", dependencies=[Depends(_require_platform_admin)])
def managed_ai_status(db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    """The managed-AI stack definitions and whether the stack path is enabled.

    Off by default: the manager-compose path (profile "ollama") stays the
    default until the stack path is proven on real GPU hardware. A single-box
    install where the manager IS the GPU host should not pay stack + placement
    complexity for no benefit.
    """
    _require_paid(_PAID_SERVICE, "Managed-AI stacks")
    return _psd_svc.managed_ai_status(db, user)

@router.get("/wake-metrics", dependencies=[Depends(_require_platform_admin)])
def stack_wake_metrics(days: int = 7,
                       db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """GA.LSW.4 — per-stack (and fleet-wide) wake / idle-reclaim metrics, computed
    read-only from the append-only `stack.wake` / `stack.sleep` / `stack.evict`
    audit rows `services/stack_wake._audit` writes.

    Window defaults to the last 7 days; `?days=N` widens it. Platform-admin only
    (fleet-wide wake/sleep history crosses tenants). Read-only — no writes, no
    schema change. The wake-duration split (cached vs fetched) is NOT derivable
    from the existing audit rows (they carry no duration or weights-source
    marker), so those fields are null and `duration_available` is false — see the
    response for the exact note.
    """
    _require_paid(_PAID_SERVICE, "Stack wake metrics")
    return _psd_svc.wake_metrics(db, user, days=days)

@router.post("/managed-ai/provision", dependencies=[Depends(_require_platform_admin)])
def provision_managed_ai(db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    """Create the managed-AI stacks. Idempotent — re-running refreshes the
    compose spec (e.g. after a preset change) without duplicating stacks."""
    _require_paid(_PAID_SERVICE, "Managed-AI stacks")
    return _psd_svc.provision_managed_ai(db, user)

@router.get("/rebalancer-config", dependencies=[Depends(_require_platform_admin)])
def get_rebalancer_config(db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """Effective rebalancer settings (DB → env → default) + source."""
    _require_paid(_PAID_SERVICE, "Fleet rebalancer config")
    return _psd_svc.get_rebalancer_config(db, user)

@router.get("/reconciler-config", dependencies=[Depends(_require_platform_admin)])
def get_reconciler_config(db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """Effective automatic-recovery reconciler settings (DB → env → default)
    plus source, for the Health & recovery card toggles."""
    _require_paid(_PAID_SERVICE, "Automatic-recovery reconciler")
    return _psd_svc.get_reconciler_config(db, user)

@router.post("/placement-check")
def placement_check(body: PlacementCheckRequest,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    """Pre-deploy 'does this fit the fleet?' for a service's GPU reservation.

    Read-only: returns the fleet placement verdict (placeable / fits_on /
    shortfall_gb / reason) for the given demand, using the SAME engine the
    managed-AI tier ladder and model availability use, so the builder's
    answer never disagrees with what a claim at deploy time would decide.
    Authed like /estimate (any signed-in user); no DB writes, no tasks."""
    _require_paid(_PAID_SERVICE, "Fleet placement check")
    return _psd_svc.placement_check(db, body, user)


def _render_stack_template(db: Session, user: models.User, template_id: str,
                           params: Optional[dict], name: str):
    """Shared render step for /from-template AND the wizard's catalog-grounded
    path (GA.ASW.3): render the catalog compose + reject a mesh-name collision.
    Deliberately does NOT run `_validate_compose` — catalog templates are
    vetted YAML (this is the exact call sequence `/from-template` has always
    used), unlike a free-form custom compose, which validates separately."""
    compose, storage_class = _render_template(template_id, params or {})
    _reject_mesh_name_collision(db, user.tenant_id, name, compose)
    return compose, storage_class


@router.post("/from-template", response_model=schemas.PersistentStack)
def create_from_template(body: TemplateCreate,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    compose, storage_class = _render_stack_template(db, user, body.template_id, body.params, body.name)
    # GR.FEN.3a — catalog templates carry no ha_class, so the class is
    # inferred from the rendered compose (a template that ships read-only
    # weight caches lands in cache_backed instead of the old silent
    # `external` default). Inference raising is impossible for a valid class;
    # an invalid x-mod.ha_class in a catalog template is a 422, not a 500.
    _ce = _ce_ha_class_default(compose, None)
    if _ce:
        ha_class, ha_class_reason = _ce
    else:
        try:
            from services.stack_ha_class import InvalidHaClass, infer_ha_class
        except ImportError:
            raise  # CE — unreachable: `_ce_ha_class_default` above returned the CE default
        try:
            ha_class, ha_class_reason = infer_ha_class(compose)
        except InvalidHaClass as e:
            raise HTTPException(status_code=422, detail=e.args[0] if e.args else str(e))

    if body.preview:
        # WS4.Q8 (launch-completion-megaplan) — non-persisting preview: the
        # builder's template on-ramp is a PRE-FILL (it renders the compose into
        # its editable form and deploys later through /assemble → /stacks).
        # Persisting here left an orphan `status=created` row for every template
        # the user ever pre-filled (same defect class as mega-qa #50 on
        # /from-wizard, which is already preview-only). No db.add, no audit —
        # the row the deploy later creates owns the STACK_CREATED audit trail.
        # The response is the same shape as the create path with `id: 0` and
        # `preview: true` so a client that reads `.compose_spec` is unchanged.
        logger.info("persistent stack pre-fill rendered from template %s for tenant %s",
                    body.template_id, user.tenant_id)
        return schemas.PersistentStack(
            id=0,
            tenant_id=user.tenant_id,
            status="created",
            preview=True,
            name=body.name,
            compose_spec=_redact_compose_secrets(compose),
            accelerator_requirements=_parse_accelerator_requirements(compose) or None,
            egress_policy=_parse_egress_policy(compose) or None,
            secret_refs=_parse_secret_refs(compose) or None,
            tls_config=_parse_tls_config(compose) or None,
            storage_class=storage_class,
            home_zone=_resolve_home_zone(None),
            ha_class=ha_class,
            ha_class_reason=ha_class_reason,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    # GA.ASW.6 — a known-database image with no backup posture declared
    # requires an explicit acknowledge_no_backup before the row is created.
    # Catalog templates (e.g. service_templates/postgres.yaml) already
    # declare `x-mod.backup: logical` so this is a no-op for them; it only
    # bites a custom `params` override that strips it out.
    _require_backup_ack(compose, body.acknowledge_no_backup)
    _backup_ack_services = _backup_ack_list(compose)

    stack = models.PersistentStack(
        tenant_id=user.tenant_id,
        created_by_user_id=user.id,
        name=body.name,
        compose_spec=compose,
        accelerator_requirements=_parse_accelerator_requirements(compose) or None,
        egress_policy=_parse_egress_policy(compose) or None,
        secret_refs=_parse_secret_refs(compose) or None,
        tls_config=_parse_tls_config(compose) or None,
        storage_class=storage_class,
        home_zone=_resolve_home_zone(None),
        status="created",
        ha_class=ha_class,
    )
    db.add(stack)
    db.commit()
    db.refresh(stack)
    _audit_extra = {"template_id": body.template_id}
    _audit_extra["ha_class"] = ha_class
    if ha_class_reason != "explicitly set at create":
        _audit_extra["ha_class_inferred"] = True
        _audit_extra["ha_class_reason"] = ha_class_reason
    if _backup_ack_services:
        _audit_extra["backup_ack"] = True
        _audit_extra["backup_ack_services"] = _backup_ack_services
    _audit_stack(db, "STACK_CREATED", stack, user, _audit_extra)
    # C5b — audit each DECLARED egress posture the author just created (previous
    # is None: on create every declared service is new).
    _record_egress_declarations(db, stack, None, user.id)
    db.commit()
    logger.info("persistent stack %s created from template %s", stack.id, body.template_id)
    out = _out(stack)
    out.ha_class_reason = ha_class_reason
    return out

@router.post("/from-wizard", response_model=WizardPrefillResponse)
def create_from_wizard(body: WizardCreate,
                       db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """Plain-language → compose via the Wizard LLM (add-persistent-stacks §7.2).

    This is a pre-fill on-ramp, NOT a create. The builder turns the returned
    compose into its editable form, then deploys through /assemble →
    /stacks. Persisting a PersistentStack here (as this endpoint used
    to) left an orphan `status=created` row for every prompt the user ever typed
    into the wizard — the row was never deployed from and never cleaned up
    (mega-qa #50). So no db.add: we validate and return the pre-fill only."""
    from utils.llm_client import call_wizard_llm
    from services import wizard_queue as _wq

    # Surface this pre-fill in the Wizard Queue (observational only — a
    # queue failure must never break the pre-fill itself; wizard-queue-
    # redesign GE.WQR.3). The FULL original prompt is stashed in `extra`
    # (target_name only keeps the first 60 chars) so a later retry
    # (GE.WQR.10) can re-run the exact same pre-fill request.
    wizard_run = None
    try:
        wizard_run = _wq.create_run(
            db,
            wizard_type=models.WizardRunType.STACK,
            target_name=(body.prompt or "")[:60],
            creator=user,
            log_source="llm_step",
            extra={"prompt": body.prompt},
        )
    except Exception as _e:  # noqa: BLE001
        logger.debug("wizard-queue emit (stack prefill) failed: %s", _e)

    try:
        result = call_wizard_llm(
            _WIZARD_SYSTEM,
            f"Create a persistent stack for: {body.prompt}",
            response_format="json", timeout_s=120, _label="persistent-stack-wizard",
        )
    except Exception as e:  # noqa: BLE001
        if wizard_run is not None:
            try:
                _wq.append_log(db, wizard_run, f"Wizard LLM error: {e}",
                               level="error", commit=False)
                _wq.set_status(db, wizard_run, models.WizardRunStatus.FAILED,
                               error_message=str(e))
            except Exception as _e2:  # noqa: BLE001
                logger.debug("wizard-queue fail-mark (stack prefill) failed: %s", _e2)
        raise internal_http_error(502, "Wizard LLM error", e) from e

    if wizard_run is not None:
        try:
            _wq.append_log(db, wizard_run,
                           f"Wizard produced a compose pre-fill for '{body.name}'",
                           level="info", commit=False)
            wizard_run.target_ref_type = "stack-prefill"
            _wq.set_status(db, wizard_run, models.WizardRunStatus.SUCCEEDED)
        except Exception as _e:  # noqa: BLE001
            logger.debug("wizard-queue success-mark (stack prefill) failed: %s", _e)

    # GA.ASW.3 — re-grounded on the service-template catalog: the wizard is
    # asked to name a catalog `template_id` (+ params) FIRST; only when
    # nothing in the catalog fits does it fall back to a raw custom compose
    # (the original, only, behaviour). Parsed defensively: an unrecognized
    # (hallucinated) template_id degrades to the custom path if a compose was
    # also given, else 422 — same terminal outcome the old code always had for
    # anything that wasn't a valid compose.
    template_id = result.get("template_id") if isinstance(result, dict) else None
    raw_compose = result.get("compose") if isinstance(result, dict) else None

    if isinstance(template_id, str) and template_id not in ("", "custom") \
            and template_id in STACK_TEMPLATES:
        params = result.get("params") if isinstance(result.get("params"), dict) else {}
        compose, storage_class = _render_stack_template(db, user, template_id, params, body.name)
    else:
        compose = raw_compose
        if not isinstance(compose, dict) or "services" not in compose:
            raise HTTPException(status_code=422, detail="Wizard did not produce a valid compose project")

        # Reconcile named volumes exactly as /assemble does: a service that mounts a
        # named volume (`pgdata:/x`) with no matching top-level `volumes:` entry is
        # rejected by _validate_compose. The LLM routinely omits the top-level
        # declaration, so auto-declare any named source before validating (mega-qa —
        # from-wizard was missing the step /assemble already had, so a valid wizard
        # compose 422'd on an undeclared named volume).
        declared = _collect_named_volumes(compose.get("services") or {})
        if declared:
            top = dict(compose.get("volumes") or {})
            for v in declared:
                top.setdefault(v, {})
            compose["volumes"] = top

        storage_class = result.get("storage_class") or ("local_fs" if compose.get("volumes") else "s3_native")
        if storage_class not in ("local_fs", "s3_native"):
            storage_class = "local_fs"

        # Validate the LLM-produced spec at the on-ramp (not only at deploy) so a bad
        # wizard output is rejected up front. Only the custom/raw-compose path needs
        # this — a matched catalog template goes through `_render_stack_template`,
        # the SAME (unvalidated, vetted-YAML) path `/from-template` uses.
        _validate_compose(compose, is_admin=bool(getattr(user, "is_platform_admin", False)),
                          secret_prefix=f"tenants/{user.tenant_id}/stacks/{body.name}/",
                          tenant_id=user.tenant_id, db=db)

    logger.info("persistent stack pre-fill produced from wizard prompt for tenant %s", user.tenant_id)
    return WizardPrefillResponse(
        name=body.name,
        compose_spec=compose,
        storage_class=storage_class,
        accelerator_requirements=_parse_accelerator_requirements(compose) or None,
        egress_policy=_parse_egress_policy(compose) or None,
        secret_refs=_parse_secret_refs(compose) or None,
        tls_config=_parse_tls_config(compose) or None,
    )

@router.get("/{stack_id}/endpoint")
def resolve_stack_endpoint(stack_id: int,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """add-persistent-stacks §4.2 / add-multi-region-mesh §8.3 — manager-as-directory
    resolution: where to reach this stack.

    If the stack's home zone is THIS manager's region (or unset / single-manager),
    the local worker endpoint is authoritative — return it. If it lives in another
    region that an approved peer manager owns, return that region's entry endpoint so
    the caller is directed to the manager that actually hosts the work (instead of
    being silently served a local/empty endpoint). This is the seam that makes a
    region-stamped workload reachable through the mesh rather than always-local."""
    return _psd_svc.resolve_stack_endpoint(db, stack_id, user)

@router.get("/accelerator-kinds", dependencies=[Depends(_require_platform_admin)])
def list_accelerator_kinds(preset: Optional[str] = None, db: Session = Depends(get_db)):
    """Accelerators a managed vLLM stack can target, and what works on each.

    multi-vendor-gpu-stacks. Every kind is offered — the product decision is
    warn-and-allow, not block — but `verified` and `warnings` carry the truth
    so the UI can say plainly that a choice is untested rather than presenting
    NVIDIA, AMD and Intel as equals when only one has ever been run here.

    When `preset` is given, each kind also reports whether that specific preset
    has a valid quantization on it. Marlin is CUDA-only, so an AWQ preset has
    no honest AMD answer and is reported unsupported rather than silently
    downgraded to weights that will not fit.
    """
    return _psd_svc.list_accelerator_kinds(db, preset)

@router.get("/observations", dependencies=[Depends(_require_platform_admin)])
def stack_observations(db: Session = Depends(get_db)):
    """Raw per-worker observations, newest first.

    The evidence behind /observations/drift. An empty list here across a fleet
    that is running stacks means no worker is reporting observations at all —
    check that workers are on a binary new enough to send `hosted_stacks`,
    since a worker that never reports is indistinguishable from one whose
    stacks all vanished if you only read the drift summary.
    """
    _require_paid(_PAID_SERVICE, "Stack observability")
    return _psd_svc.stack_observations(db)

@router.post("/{stack_id}/egress-observations", dependencies=[Depends(_require_platform_admin)])
def record_egress_observations(stack_id: int, body: _EgressObservationBatch,
                               db: Session = Depends(get_db),
                               user: models.User = Depends(get_current_user)):
    """delegated-stack-authoring §4.3 — record what this DEPLOYED stack was
    actually seen reaching. Admin-gated ingestion (a reporter — worker or mesh-
    side collector — authenticates as the platform), mirroring the existing
    `/observations` reporting posture rather than opening a new unauthenticated
    surface."""
    stack = _get_owned(db, stack_id, user)
    recorded = _record_egress_observations(
        db, stack, [o.model_dump() for o in body.observations], user)
    db.commit()
    return {"recorded": len(recorded), "observations": recorded}

@router.get("/observations/egress", dependencies=[Depends(_require_platform_admin)])
def egress_observations(stack_id: Optional[int] = None, db: Session = Depends(get_db)):
    """The evidence behind group 6.1's pre-flip review: recent recorded
    observations, each already marked against its stack's DECLARED posture.
    `within_declared: null` means the service has no declaration yet (legal
    pre-flip); `false` is the row that matters — reach that group 6.2's
    default-deny flip would cut off."""
    _require_paid(_PAID_SERVICE, "Egress observability")
    return _psd_svc.egress_observations(db, stack_id)

@router.get("/placement", dependencies=[Depends(_require_platform_admin)])
def placement_overview(db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """§8.4/§8.8/§8.9 — placement constraints and who is enforcing them.

    One call, because these are one question in practice: an operator asking
    "why is this stack where it is, and will anything move it" needs the pin,
    the rebalancer's state and the reconciler's state together. Split across
    three endpoints, the answer requires knowing which three to ask.

    States plainly when the reconciler is disabled. Nobody should have to infer
    from an empty action list that automatic recovery is not running — that is
    the assumption that turns a quiet outage into a long one.
    """
    _require_paid(_PAID_SERVICE, "Fleet placement overview")
    return _psd_svc.placement_overview(db, user)

@router.get("/tenant-tls")
def get_tenant_tls(db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """Return the tenant's default TLS policy (tenant-admin only). Never returns
    cert/key material — only the mode and the on-file cert's CN + expiry."""
    if not _is_tenant_admin(user):
        raise HTTPException(status_code=403,
                            detail="only a tenant admin can view the TLS policy")
    _, val = _load_tenant_tls_setting(db, user.tenant_id)
    return _tenant_tls_public(val)

@router.put("/tenant-tls")
def set_tenant_tls(body: TenantTlsUpdate,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """Set the tenant's default TLS policy (tenant-admin only).

    - `letsencrypt`: clears any stored byo-cert metadata; the platform issues +
      renews on-demand for exposed stacks.
    - `byo`: validates the pasted cert+key (parses, pairs, not expired), writes
      the material to the tenant's Vault path, and records the CN + notAfter so
      the UI can show + warn on expiry. Passing byo with no cert is allowed ONLY
      if a cert is already on file (re-affirming the mode)."""
    if not _is_tenant_admin(user):
        raise HTTPException(status_code=403,
                            detail="only a tenant admin can change the TLS policy")
    mode = (body.mode or "").strip().lower()
    if mode not in _TENANT_TLS_MODES:
        raise HTTPException(status_code=422,
                            detail=f"mode must be one of {_TENANT_TLS_MODES}")
    if mode == "byo":
        # FIX-CE-HIDE-BYO (FIND-2, owner verdict A): CE never loads a bring-your-
        # own certificate — caddy-entrypoint.sh:444-466 loads none and the cert-
        # watch sidecar files are not shipped — so CE refuses the mode outright.
        # The guard is INSIDE `if is_ce_edition()`: paid behaviour and wording
        # are byte-unchanged (utils/paid_import.is_ce_edition is the ONE edition
        # predicate). The UI hide is the client half; this is the real boundary.
        from utils.paid_import import is_ce_edition
        if is_ce_edition():
            raise HTTPException(status_code=409,
                                detail="bring-your-own TLS is not available in this edition")
    row, val = _load_tenant_tls_setting(db, user.tenant_id)
    val["mode"] = mode

    if mode == "byo":
        if body.cert_pem and body.key_pem:
            primary = _cert_primary_name(body.cert_pem)
            if not primary:
                base = os.getenv("BASE_DOMAIN", "").strip()
                primary = f"*.apps.{base}" if base else "*"
            not_after = _validate_cert_pair(body.cert_pem, body.key_pem, primary)
            _vault_write(_tenant_tls_vault_path(user.tenant_id),
                         {"cert": body.cert_pem, "key": body.key_pem,
                          "fqdn": primary})
            val["cert_cn"] = primary
            val["cert_not_after"] = not_after.isoformat()
            val["cert_vault_path"] = _tenant_tls_vault_path(user.tenant_id)
        elif not val.get("cert_cn"):
            raise HTTPException(
                status_code=422,
                detail="bring-your-own TLS requires both cert_pem and key_pem")
        # else: byo re-affirmed with a cert already on file — keep it.
    else:  # letsencrypt — drop byo metadata so the UI reflects automatic issuance
        for k in ("cert_cn", "cert_not_after", "cert_vault_path"):
            val.pop(k, None)

    val["updated_at"] = datetime.now(timezone.utc).isoformat()
    val["updated_by"] = user.id

    if row is None:
        row = models.TenantSetting(tenant_id=user.tenant_id,
                                   key=_TENANT_TLS_SETTING_KEY, value=val)
        db.add(row)
    else:
        row.value = val
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "value")
    db.commit()
    logger.info("tenant %s TLS policy set to %s by user %s (cert=%s)",
                user.tenant_id, mode, user.id, val.get("cert_cn") or "-")
    return _tenant_tls_public(val)

# NOTE: everything with a literal single-segment path must be declared ABOVE
# this route. Starlette matches in declaration order and `{stack_id}` compiles
# to `[^/]+`, so a later `/placement` would be swallowed here and fail as an
# invalid integer instead of running. Two-segment paths (`/reconcile/preview`,
# `/observations/drift`) are unaffected and may stay below.
@router.get("/{stack_id}", response_model=schemas.PersistentStack)
def get_stack(stack_id: int,
              db: Session = Depends(get_db),
              user: models.User = Depends(require_bearer_or_api_key)):
    stack = _get_owned(db, stack_id, user)
    # A soft-deleted stack is gone from the platform's view: the list hides it,
    # so GET must 404 it too, or the row would be reachable by id while being
    # invisible in the directory (and re-deployable behind the operator's back).
    if stack.status == "deleted":
        raise HTTPException(status_code=404, detail="Persistent stack not found")
    return _out_with_cost(db, stack)


# =============================================================================
# soft-delete — DELETE /stacks/{stack_id}
#
# The lifecycle gap found live: a stack row that was created but never
# deployed had no way to be removed. `DELETE /stacks/{id}` was 405 (no delete
# verb anywhere in this router package), and every other verb needs a host —
# `stop` refuses without one, `teardown` is the storage-reclaim path for a
# stack that actually landed somewhere.
#
# ISO 27001, no hard deletes: the row keeps its id, compose spec, revisions and
# audit history. `status` moves to "deleted" — the model's EXISTING lifecycle
# field (String(20), default "created"); no column is added, and the Dashboard
# already renders "deleted" as a dead status (persistent-stacks.js). `desired_state`
# goes to "stopped" so the reconciler's candidate set (`desired_state ==
# "running"`) never re-places it, and "deleted" is absent from every active-status
# tuple (PERSISTENT_STACK_ACTIVE_STATUSES, ACTIVE_STACK_STATUSES, the provision
# quota's _ACTIVE_STATUSES), so it stops counting toward capacity and quota.
#
# Gated exactly like every other stack mutation: the router-wide
# `stack:author` platform capability (routers/persistent_stacks/__init__.py)
# plus `_get_owned`'s tenant scoping — a foreign tenant's stack 404s.
# =============================================================================
@router.delete("/{stack_id}", response_model=schemas.PersistentStack)
def delete_stack(stack_id: int,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """Soft-delete a persistent stack that was created but never deployed.

    Only a stack that never went anywhere can be removed this way. Anything
    deployed (or with a deploy/migrate still in flight) is ended by
    `POST /stacks/{id}/teardown`, which reclaims its storage and records the
    teardown — silently flipping `status` there would leave containers on a
    host that nothing tracks any more."""
    stack = _get_owned(db, stack_id, user)
    if stack.status == "deleted":
        raise HTTPException(status_code=404, detail="Persistent stack not found")

    inflight = _psd_crud.list_nonterminal_deploy_tasks(db, stack.id)
    if stack.status != "created" or stack.target_worker or inflight:
        raise HTTPException(
            status_code=409,
            detail=(f"stack {stack.id} is not a never-deployed stack "
                    f"(status={stack.status}, host={stack.target_worker}, "
                    f"deploy/migrate tasks in flight={len(inflight)}); "
                    "remove a deployed stack with "
                    f"POST /stacks/{stack_id}/teardown, which also reclaims its storage"))

    stack.status = "deleted"
    # DOWN ON PURPOSE — without this the reconciler reads the row as a gap in
    # a stack that is meant to run and re-places it.
    stack.desired_state = "stopped"
    stack.status_reason = "deleted by operator (never deployed)"
    _audit_stack(db, "STACK_SOFT_DELETED", stack, user,
                 {"never_deployed": True,
                  "compose_services": len(((stack.compose_spec or {}).get("services") or {}))})
    db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s soft-deleted by operator (never deployed)", stack.id)
    return _out(stack)

# =============================================================================
# stack-definition-edit — edit an existing stack's composition + config,
# revisioned + re-deployable (everyone-facing; gated by RBAC edit-permission
# on the stack + the baseline Cap.STACK_EDIT capability, NOT the paid
# Cap.STACK_WIZARD and NOT a feature flag — owner decision, clean-v2-api
# Wave 10: "any tenant owner can edit their stacks in every edition").
#
# RBAC edit-permission on a PersistentStack, in this codebase, IS the
# combination this router already enforces on every other stack-mutating
# endpoint (set_stack_exposure, set_backup_schedule, upload_stack_cert, ...):
# the router-wide `require_platform_capability(STACK_AUTHOR)` dependency
# (platform admins bypass it) PLUS `_get_owned`'s tenant-scoped ownership
# lookup (404s a foreign tenant's stack). There is no separate per-resource
# ACL model for stacks (unlike NODE_DEFINITION's `authorize(...)`), so these
# endpoints use the SAME gate as their siblings rather than inventing one.
# `Cap.STACK_EDIT` is baseline in every edition/tier (see
# utils.entitlements / entitlements_from_plan), so in practice
# this additional check is always satisfied — it exists for defense in depth
# and to keep the same entitlement-check shape node_code_edit uses.
# =============================================================================

def _require_stack_edit_or_402(db: Session, user: models.User) -> None:
    import services.persistent_stack_definition_service as _psds
    try:
        _psds.require_stack_edit(db, user)
    except _psds.EntitlementRequiredError:
        raise HTTPException(
            status_code=402,
            detail={"code": "entitlement_required", "capability": "stack_edit"},
        )


@router.get("/{stack_id}/definition")
def get_stack_definition(stack_id: int,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    """The editable composition+config (services, env, ports, volumes,
    reservations) plus revision history, for the stack editor UI."""
    import services.persistent_stack_definition_service as _psds
    import services.persistent_stack_revisions as _psr

    stack = _get_owned(db, stack_id, user)
    rev = _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)
    revisions = _psr.list_revisions(db, stack.id)
    view = _psds.definition_view(stack, rev, revisions)
    # 13.5c — Copy…: the dashboard's "Copy" clones via this endpoint (redacted
    # spec) and re-imports it as a fresh stack. Restore any env the redactor
    # masked with a ${secret:NAME} ref WHEN the source already names that
    # tenant secret, so the clone references the same secret instead of a
    # ***redacted*** placeholder the caller would have to re-enter. Redacted
    # values the source does NOT back with a named ref stay redacted (today's
    # "replace before creating" behaviour is kept).
    from services import stack_secret_extract as sse
    spec, info = sse.rehydrate_clone(view.get("compose_spec"), stack)
    if info["restored"]:
        from services.stack_compose_import import compose_to_yaml
        from services.persistent_stack_definition_service import services_view
        view["compose_spec"] = spec
        view["compose_yaml"] = compose_to_yaml(spec)
        view["services"] = services_view(spec)
        # Copy… surfaces which refs were carried over + which redacted values
        # still need a real value (client shows this, then blocks Create).
        view["clone_secret_refs"] = info
    return view


@router.put("/{stack_id}/definition", response_model=schemas.PersistentStack)
def put_stack_definition(stack_id: int,
                         body: StackDefinitionUpdate,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    """Save an edited composition+config as a NEW revision (append-only —
    ISO 27001, no hard deletes) and apply it to the live stack row. Validated
    through the SAME `_validate_compose` gate `/assemble` and `/create` use, so
    an edit cannot smuggle past the delegated-author safety boundary those
    paths already enforce. Does NOT redeploy — call POST .../deploy (or the
    UI's "Save & redeploy") to push the new revision to the worker."""
    import services.persistent_stack_definition_service as _psds
    import services.persistent_stack_revisions as _psr

    stack = _get_owned(db, stack_id, user)
    _require_stack_edit_or_402(db, user)
    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)

    _provided = [name for name, val in (("services", body.services),
                                        ("compose_spec", body.compose_spec),
                                        ("compose_yaml", body.compose_yaml))
                if val is not None]
    if len(_provided) > 1:
        raise HTTPException(
            status_code=422,
            detail="pass exactly one of 'services', 'compose_spec', or 'compose_yaml', "
                   f"not {' and '.join(_provided)}")
    if body.services is not None:
        if not body.services:
            raise HTTPException(status_code=422, detail="a stack needs at least one service")
        compose_spec = _psds.render_compose_from_services(db, stack.name, body.services)
    elif body.compose_spec is not None:
        compose_spec = body.compose_spec
    elif body.compose_yaml is not None:
        # SAME safe-YAML parser `/import-compose` uses (never a second parser
        # for this router) — a bad YAML doc or a non-mapping top level is a
        # 422 with the parser's own message, which for a `yaml.YAMLError`
        # includes the offending line/column.
        from services import stack_compose_import as _sci
        # 27.4 r274 — "Save & redeploy" honours this stack's own bench ceiling:
        # an `x-mod.serving` profile in the saved spec is clamped to what the
        # newest bench row proved it can serve (slots / ctx can only LOWER).
        # Best-effort: a bench lookup that blows up must never fail the save, so
        # it degrades to the unclamped profile and says so in the log.
        # `/stacks/import-compose` (a NEW stack, nothing benched) keeps calling
        # normalize_compose without admission — previews stay unclamped.
        admission = None
        try:
            from services import stack_bench_profile as _sbp
            admission = _sbp.admission_limits_for_stack(db, stack.id)
        except Exception as e:  # noqa: BLE001 — admission is advisory only
            logger.warning("bench admission for stack %s lookup failed: %s",
                           stack.id, e)
            admission = None
        try:
            compose_spec = _sci.normalize_compose(body.compose_yaml,
                                                  admission=admission)
        except _sci.ComposeImportError as e:
            raise HTTPException(status_code=422, detail=e.as_text())
    else:
        raise HTTPException(
            status_code=422,
            detail="one of 'services', 'compose_spec', or 'compose_yaml' is required")

    # GET .../definition redacts secret-looking env values as "***redacted***"
    # (M3 — never round-trip a secret value over the API); restore any that
    # came back untouched from the live pre-edit spec so an edit that doesn't
    # touch a given secret can't overwrite it with that literal placeholder.
    compose_spec = _psds.unredact_against(compose_spec, stack.compose_spec)

    # GX.S10a — optional variables on the definition body: secret values go to
    # Vault and the stored spec keeps refs only; 422 naming any placeholder
    # left unresolved. No-op (returns the spec unchanged) when omitted.
    from services import stack_variables as _sv
    compose_spec = _sv.apply_variables(
        db, compose_spec, body.variables, tenant_id=user.tenant_id,
        stack_name=stack.name, user=user)

    # Same gate /assemble, create/from-template and import-compose/finalize use
    # — a delegated (non-admin) author's edit is held to the exact same host-
    # escape / admin-only-construct boundary as a fresh create. `trust_admin_ack`
    # stays False: this spec is fresh caller-supplied input, not the stored
    # spec being re-validated at deploy.
    #
    # GPUCAP P1b — the card pin is admin-set only (see the `_validate_compose`
    # gpu_pin guard), so a delegated author must neither invent one nor drop or
    # alter an existing admin's pin through the editor round-trip. The editor
    # GETs the whole compose (the pin included) and PUTs it back, so a pin the
    # author never touched must survive verbatim: for a NON-admin only, force the
    # incoming spec's `x-mod.gpu_pin` to the stack's CURRENT stored value
    # whenever the stack HAS one — the author's edit can then only ever be a
    # no-op on the pin (same value) or an attempted change, which is silently
    # reverted to the admin's. When the stack has NO stored pin the incoming key
    # is left untouched so the guard above 422s the invention (rather than
    # deleting it and letting a self-pin through). Admins keep their latitude:
    # an admin edit posts the pin back or changes it as they please.
    # The value that goes to the validator: for a NON-admin on a stack that
    # ALREADY has an admin pin, the pin is not author-supplied content (the
    # editor GETs the whole compose and PUTs it back), so it is lifted out of
    # the copy handed to the gate — the guard above refuses author pins, and
    # re-checking the admin's own stored pin would 422 every edit of a pinned
    # stack. `compose_spec` itself still carries the FORCED stored pin, which
    # is what gets stored. With no stored pin the incoming key stays, so the
    # guard 422s an invented one.
    _validation_spec = compose_spec
    if not bool(getattr(user, "is_platform_admin", False)):
        _stored_xmod = (stack.compose_spec or {}).get("x-mod")
        _stored_pin = (_stored_xmod.get("gpu_pin")
                       if isinstance(_stored_xmod, dict) else None)
        if _stored_pin:
            _incoming_xmod = (compose_spec["x-mod"]
                              if isinstance(compose_spec.get("x-mod"), dict)
                              else {})
            if _incoming_xmod.get("gpu_pin") != _stored_pin:
                _incoming_xmod = dict(_incoming_xmod)
                _incoming_xmod["gpu_pin"] = _stored_pin
                compose_spec["x-mod"] = _incoming_xmod
            _validation_spec = copy.deepcopy(compose_spec)
            _validation_spec["x-mod"].pop("gpu_pin", None)
    _validate_compose(_validation_spec,
                      is_admin=bool(getattr(user, "is_platform_admin", False)),
                      secret_prefix=f"tenants/{user.tenant_id}/stacks/{stack.name}/",
                      tenant_id=user.tenant_id, db=db)

    # Re-derive storage_class the SAME way import-compose/from-wizard do
    # (local_fs when the edited spec still declares named volumes, else
    # s3_native) rather than blindly keeping the pre-edit value — an edit that
    # drops the last named volume, or adds one to a stack that had none,
    # should be reflected.
    from services import stack_compose_import as _sci
    storage_class = _sci.derive_stack_fields(compose_spec, None)["storage_class"]

    rev = _psr.create_revision(
        db, stack,
        origin=_psr.ORIGIN_EDITED,
        author_user_id=user.id,
        compose_spec=compose_spec,
        storage_class=storage_class,
        message=body.message,
    )

    # C5b — snapshot the PRE-edit hosts before the spec is overwritten, so the
    # declaration audit can diff them (unchanged services write nothing).
    _pre_edit_compose = copy.deepcopy(stack.compose_spec)
    stack.compose_spec = compose_spec
    stack.accelerator_requirements = _parse_accelerator_requirements(compose_spec) or None
    stack.egress_policy = _parse_egress_policy(compose_spec) or None
    stack.secret_refs = _parse_secret_refs(compose_spec) or None
    stack.tls_config = _parse_tls_config(compose_spec) or None
    stack.storage_class = storage_class
    db.add(stack)
    _audit_stack(db, "STACK_DEFINITION_EDITED", stack, user,
                 {"revision_no": rev.revision_no})
    _record_egress_declarations(db, stack, _pre_edit_compose, user.id)
    db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s definition saved as revision %s by user %s",
                stack.id, rev.revision_no, user.id)
    return _out(stack)


@router.get("/{stack_id}/revisions")
def list_stack_revisions(stack_id: int,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    import services.persistent_stack_revisions as _psr

    stack = _get_owned(db, stack_id, user)
    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)
    revisions = _psr.list_revisions(db, stack.id)
    return {"revisions": [_psr.revision_summary(r) for r in revisions]}


@router.get("/{stack_id}/revisions/{revision_no}/diff")
def diff_stack_revision(stack_id: int,
                        revision_no: int,
                        against: Optional[int] = Query(
                            None, description="revision_no to diff against; "
                                              "defaults to the one immediately before it"),
                        db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    import services.persistent_stack_revisions as _psr

    stack = _get_owned(db, stack_id, user)
    from_no = against if against is not None else max(1, revision_no - 1)
    try:
        d = _psr.diff(db, stack.id, from_no, revision_no)
    except ValueError:
        raise HTTPException(status_code=404, detail="revision number not found")
    return {"from": from_no, "to": revision_no, "diff": d}


@router.post("/{stack_id}/revert", response_model=schemas.PersistentStack)
def revert_stack_definition(stack_id: int,
                            body: StackRevertRequest,
                            db: Session = Depends(get_db),
                            user: models.User = Depends(require_bearer_or_api_key)):
    """Revert to a prior revision's composition+config by appending a NEW
    revision equal to it (the intervening history is preserved, never
    rewritten) and applying it to the live stack row. Does NOT redeploy."""
    import services.persistent_stack_definition_service as _psds
    import services.persistent_stack_revisions as _psr

    stack = _get_owned(db, stack_id, user)
    _require_stack_edit_or_402(db, user)
    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)

    target = _psr.get_by_no(db, stack.id, body.target_revision_no)
    if target is None:
        raise HTTPException(status_code=404, detail="revision not found")

    # GPUCAP P1b — `/{id}/revert` is an authoring path too, and the card pin is
    # admin-set only: a delegated STACK_AUTHOR could revert to a revision made
    # BEFORE the admin pinned (dropping the pin) or to one carrying a DIFFERENT
    # pin (swapping cards). Same posture as the PUT .../definition round-trip
    # guard above: for a NON-admin only, the reverted spec's `x-mod.gpu_pin` is
    # forced to the stack's CURRENT stored value — and REMOVED when the stack
    # has none, so a revert can never re-introduce a pin the current admin state
    # does not have. (The editor path leaves an invented key in place instead,
    # because there the shared `_validate_compose` gate must 422 it; this path
    # takes no fresh caller input, only stored history, so it strips silently.)
    # Admins keep their latitude: the forced spec IS the target's.
    # The revision row must record the spec as written, so the force happens
    # BEFORE the history entry is appended — `_psr.revert` is exactly this
    # `create_revision` call (origin REVERTED, content = the target's), with
    # the forced spec substituted.
    _reverted_spec = copy.deepcopy(dict(target.compose_spec or {}))
    if not bool(getattr(user, "is_platform_admin", False)):
        _stored_xmod = (stack.compose_spec or {}).get("x-mod")
        _stored_pin = (_stored_xmod.get("gpu_pin")
                       if isinstance(_stored_xmod, dict) else None)
        if _stored_pin:
            _incoming_xmod = (dict(_reverted_spec["x-mod"])
                              if isinstance(_reverted_spec.get("x-mod"), dict)
                              else {})
            _incoming_xmod["gpu_pin"] = copy.deepcopy(_stored_pin)
            _reverted_spec["x-mod"] = _incoming_xmod
        elif isinstance(_reverted_spec.get("x-mod"), dict) and \
                "gpu_pin" in _reverted_spec["x-mod"]:
            _incoming_xmod = dict(_reverted_spec["x-mod"])
            _incoming_xmod.pop("gpu_pin", None)
            if _incoming_xmod:
                _reverted_spec["x-mod"] = _incoming_xmod
            else:
                _reverted_spec.pop("x-mod", None)

    rev = _psr.create_revision(
        db, stack,
        origin=_psr.ORIGIN_REVERTED,
        author_user_id=user.id,
        compose_spec=_reverted_spec,
        storage_class=target.storage_class,
    )

    # C5b — snapshot the pre-revert hosts so the declaration audit can diff.
    _pre_revert_compose = copy.deepcopy(stack.compose_spec)
    stack.compose_spec = rev.compose_spec
    stack.accelerator_requirements = _parse_accelerator_requirements(rev.compose_spec) or None
    stack.egress_policy = _parse_egress_policy(rev.compose_spec) or None
    stack.secret_refs = _parse_secret_refs(rev.compose_spec) or None
    stack.tls_config = _parse_tls_config(rev.compose_spec) or None
    if rev.storage_class:
        stack.storage_class = rev.storage_class
    db.add(stack)
    _audit_stack(db, "STACK_DEFINITION_REVERTED", stack, user,
                 {"revision_no": rev.revision_no, "target_revision_no": target.revision_no})
    _record_egress_declarations(db, stack, _pre_revert_compose, user.id)
    db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s definition reverted to revision %s (new revision %s) by user %s",
                stack.id, target.revision_no, rev.revision_no, user.id)
    return _out(stack)


# Declared BEFORE the parameterized `/{stack_id}/secrets` route so the literal
# `platform-config` segment is not swallowed as a stack_id (FastAPI matches in
# declaration order).
@router.post("/platform-config/secrets", dependencies=[Depends(_require_platform_admin)])
def write_platform_secret(body: PlatformSecretWrite,
                          db: Session = Depends(get_db),
                          user: models.User = Depends(get_current_user)):
    """§4.1 — platform config paths are admin-only. Confined to `platform/` so
    this endpoint can never reach a tenant's stack path. Only the path + the key
    NAMES are audited, never the values."""
    if not body.path.startswith("platform/") or ".." in body.path.split("/"):
        raise HTTPException(status_code=403,
                            detail="platform config paths must be under 'platform/'")
    if not isinstance(body.data, dict) or not body.data:
        raise HTTPException(status_code=422, detail="data must be a non-empty object")
    _vault_write(body.path, body.data)
    try:
        db.add(models.AuditEvent(tenant_id=user.tenant_id, user_id=user.id,
                                 event_name="PLATFORM_SECRET_WRITTEN",
                                 details={"path": body.path, "keys": sorted(body.data.keys())}))
    except Exception as e:  # noqa: BLE001
        logger.warning("platform secret audit failed: %s", e)
    db.commit()
    logger.info("platform config written at %s", hash_for_log(body.path))
    return {"ok": True, "path": body.path}

@router.post("/{stack_id}/secrets")
def write_stack_secret(stack_id: int,
                       body: StackSecretWrite,
                       db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """§4.1 — write a secret value to this stack's own Vault path. The value is
    stored as `{value: ...}` (how `_resolve_stack_secrets` reads it back) and
    NEVER echoed or logged; only the path is audited."""
    stack = _get_owned(db, stack_id, user)
    prefix = _stack_secret_prefix(stack)
    _require_under_prefix(body.path, prefix)
    _vault_write(body.path, {"value": body.value})
    _audit_stack(db, "STACK_SECRET_WRITTEN", stack, user, {"path": body.path})
    db.commit()
    logger.info("stack %s secret written at %s", stack.id,
                hash_for_log(body.path))
    return {"ok": True, "path": body.path}

@router.post("/{stack_id}/tls", response_model=schemas.PersistentStack)
def upload_stack_cert(stack_id: int,
                      body: StackCertUpload,
                      db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    """§5.2 — upload a byo cert+key for a service declaring `x-mod.tls.mode: byo`.
    Validated before it is stored (parses, matches the declared fqdn, key pairs,
    not expired), then written to the stack's cert path as one secret. Overwrite
    = rotate: the ingress (caddy-stack-ingress) re-reads it live, no redeploy.
    The cert/key never round-trip back over the API."""
    stack = _get_owned(db, stack_id, user)
    tls = (stack.tls_config or {}).get(body.service)
    if not isinstance(tls, dict):
        raise HTTPException(status_code=422,
                            detail=f"service {body.service!r} has no x-mod.tls declaration")
    if tls.get("mode") != "byo":
        raise HTTPException(status_code=422,
                            detail=f"service {body.service!r} is tls.mode {tls.get('mode')!r}; "
                                   f"byo cert upload is only for mode 'byo'")
    fqdn = tls.get("fqdn") or ""
    not_after = _validate_cert_pair(body.cert, body.key, fqdn)
    # One secret at the stack's cert path (mode 'byo' → …/tls). The values live
    # only in Vault; tls_config keeps just the parsed expiry for the read warning.
    cert_path = f"{_stack_secret_prefix(stack)}tls"
    _vault_write(cert_path, {"cert": body.cert, "key": body.key,
                             "fqdn": fqdn, "not_after": not_after.isoformat()})
    conf = dict(stack.tls_config or {})
    entry = dict(conf.get(body.service) or {})
    entry["not_after"] = not_after.isoformat()
    conf[body.service] = entry
    stack.tls_config = conf
    db.add(stack)
    _audit_stack(db, "STACK_CERT_UPLOADED", stack, user,
                 {"service": body.service, "fqdn": fqdn,
                  "not_after": not_after.isoformat(), "path": cert_path})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s byo cert stored for service %s (fqdn=%s, expires %s)",
                stack.id, body.service, fqdn, not_after.isoformat())
    return _out(stack)

@router.put("/{stack_id}/exposure", response_model=schemas.PersistentStack)
def set_stack_exposure(stack_id: int,
                       body: StackExposureUpdate,
                       db: Session = Depends(get_db),
                       user: models.User = Depends(get_current_user)):
    """caddy-stack-ingress §2 — turn a stack's PUBLIC exposure on or off.

    Exposure is opt-in per stack (default internal-only). When turned ON the stack
    is given its MOD-run FQDN `t<tid>-<stackid>.apps.<BASE_DOMAIN>` (computed +
    stored in `public_hostname`; wildcard-routed by Caddy, so no edge config is
    needed) and MAY carry a BYO `custom_domain`. When turned OFF, `public_hostname`
    is cleared so the ingress `ask` endpoint stops authorising it for TLS; the
    custom_domain is also cleared (an unexposed stack has no public names).

    Tenant-scoped to the owning user, under the router-wide STACK_AUTHOR gate."""
    stack = _get_owned(db, stack_id, user)

    if body.exposed:
        stack.exposed = True
        stack.public_hostname = _public_hostname(stack)
        if body.clear_custom_domain:
            stack.custom_domain = None
        elif body.custom_domain is not None and body.custom_domain.strip() != "":
            stack.custom_domain = _validate_custom_domain(body.custom_domain)
        # else: leave any existing custom_domain untouched
        # friendly-stack-fqdn (D4) — accept + validate the optional friendly alias
        # (a valid hostname; MOD stores it and wires routing, but provisions no
        # DNS). Same clear/leave-untouched idiom as custom_domain.
        if body.clear_friendly_fqdn:
            stack.friendly_fqdn = None
        elif body.friendly_fqdn is not None and body.friendly_fqdn.strip() != "":
            stack.friendly_fqdn = _validate_custom_domain(body.friendly_fqdn)
        # else: leave any existing friendly_fqdn untouched
    else:
        # Off: no public names. Clear all so nothing lingers as authorised.
        stack.exposed = False
        stack.public_hostname = None
        stack.custom_domain = None
        stack.friendly_fqdn = None

    db.add(stack)
    _audit_stack(db, "STACK_EXPOSURE_SET", stack, user,
                 {"exposed": stack.exposed,
                  "public_hostname": stack.public_hostname,
                  "custom_domain": stack.custom_domain,
                  "friendly_fqdn": getattr(stack, "friendly_fqdn", None)})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s exposure set: exposed=%s public_hostname=%s custom_domain=%s friendly_fqdn=%s",
                stack.id, stack.exposed, stack.public_hostname or "-",
                stack.custom_domain or "-", getattr(stack, "friendly_fqdn", None) or "-")
    return _out(stack)

# =============================================================================
# 30.6.7 c7-a2 — PUT /stacks/{stack_id}/keep-running (GX.S5 c7 hard floor).
#
# The tenant-admin setter for `keep_running` (models.py:8563 — TRUE = idle reclaim
# can never sleep or evict this stack, whatever the platform default and the
# per-stack `idle_enabled` override say). c7-a1 landed the column, c7-a4 reads it
# on GET /v1/llm/models and c7-a6 honours it in the eviction guard, but nothing
# wrote it, so the collective's ask (30.6.7 (b)) was unusable. This is that setter.
#
# Shape copied from `set_stack_exposure` directly above (:971): same verb, same
# `_get_owned` tenant scoping (a foreign tenant's stack 404s — threat case 1),
# same `_audit_stack` call, same commit point. CE: NO `_require_paid` here
# (persistent_stacks.py is api_allow in open-core/manifest.yaml).
#
# Stack-pinned keys (formerly deploy tokens): DENIED by default, no per-route deny
# needed. `require_bearer_or_api_key` runs `_enforce_api_key_policy` (dependencies.py),
# which now calls `services/app_key_scope.check_stack_key_scope` for EVERY key; that is
# default-deny over the implicit GET /v1/stacks/{stack_id} + STACK_SCOPE_ALLOWLIST
# (app_key_scope.py — deploy/logs/seed/backups:read/backups:run; no keep-running rule),
# so PUT .../keep-running falls through to
# `_deny(... detail="deploy_token_scope_denied")` (app_key_scope.py).
# =============================================================================


class StackKeepRunningUpdate(BaseModel):
    """30.6.7 c7-a2 — set a stack's keep-running floor. `enabled` is a STRICT
    bool: `"yes"`/`1`/`"true"` are rejected 422, never coerced (threat case 4).
    `extra="forbid"` so a typo'd or smuggled key 422s instead of being ignored."""
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


class StackKeepRunningOut(BaseModel):
    """The minimal read-back: the flag we just wrote, plus the stack's raw
    lifecycle status so the UI can show the row it acted on. No compose spec,
    no secrets, no host names."""
    stack_id: int
    keep_running: bool
    status: str


@router.put("/{stack_id}/keep-running", response_model=StackKeepRunningOut)
def set_stack_keep_running(stack_id: int,
                           body: StackKeepRunningUpdate,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(require_bearer_or_api_key)):
    """GX.S5 c7 — pin a stack as never-sleep / never-evict (or clear the pin).

    Tenant-scoped to the owning user, under the router-wide STACK_AUTHOR gate.
    Idempotent: re-sending the current value is a 200 that writes NO audit row
    (the design audits on state change). A soft-deleted stack 409s
    `stack_deleted` — re-pinning a row the operator ended would resurrect it
    behind the reconciler's back."""
    stack = _get_owned(db, stack_id, user)
    stack = _psd_svc.set_keep_running(db, stack, user, body.enabled)
    return {"stack_id": stack.id,
            "keep_running": bool(stack.keep_running),
            "status": stack.status}

# =============================================================================
# gw-stack-networking GW.4 — host-port / subnet usage + per-stack host ports.
#
# GW.1 made MOD own host ports and subnets at import + render time, but left the
# READ side unowned: the fleet-wide report existed as a service function with no
# endpoint, and a stack's `x-mod.host_ports` (and what a worker actually
# published for them) had nowhere to be viewed or edited except the raw
# compose. These two endpoints own that: the usage report is a read-only admin
# view of pre-GW.1 legacy stacks; host-ports is a stack-scoped read + save that
# round-trips through the SAME validated shape the importer accepts
# (`services.stack_compose_import._validate_host_ports`) so the UI can never
# smuggle a shape past the import boundary.
# =============================================================================

# The edge's public name the customer CNAMEs their own zone at — the SAME value
# `_out` exposes as `edge_target` on an exposed stack (single source: the
# STACK_INGRESS_EDGE_TARGET / edge.<BASE_DOMAIN> derivation in `_stack_edge_target`).
def _stack_networking_edge_target() -> Optional[str]:
    t = _stack_edge_target()
    return t or None


def _stack_host_port_entries(stack) -> list:
    """The stack's declared host publishes, in display form. A NEW-format stack
    (has `x-mod.ports`) takes them from `x-mod.host_ports` — the ONLY place a
    host publish survives post-GW.1. A legacy stack (no `x-mod.ports`) predates
    MOD owning ports, so its raw `ports:` entries ARE its host publishes;
    surfaced as {host: None, bind: None} — 'not MOD-managed', which is exactly
    the truth the report endpoint also conveys. Entries keep the importer's
    canonical field order so the save endpoint can round-trip them unmodified."""
    spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
    top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}
    services = spec.get("services") if isinstance(spec.get("services"), dict) else {}
    if "ports" in top_xmod:
        out = []
        for e in (top_xmod.get("host_ports") or []):
            if not isinstance(e, dict):
                continue
            out.append({"service": e.get("service"), "target": e.get("target"),
                        "host": e.get("host"), "protocol": e.get("protocol") or "tcp",
                        "bind": e.get("bind") or None})
        return out
    out = []
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for p in (svc.get("ports") or []):
            out.append({"service": svc_name, "target": None, "host": None,
                        "protocol": "tcp", "bind": None})
    return out


def _stack_host_port_allocations(db: Session, stack) -> dict:
    """What the worker ACTUALLY published per stack: the freshest stack
    observation's `host_port_allocations`, keyed by service. GW.2's worker ships
    this in the heartbeat/stack observation; until that helper lands, this
    returns {} and the UI renders `any` entries as 'pending'."""
    try:
        # GT.D5 / DS-2 — CE: `stack_observation` is dropped, so there is no
        # gateway/host-port observation enrichment to read; skip (the UI
        # renders `any` entries as 'pending', the same shape as today).
        _so = _optional_paid("services.stack_observation")
        if _so is None:
            return {}
        fn = getattr(_so, "latest_host_port_allocations", None)
        if fn is None:
            return {}
        allocs = fn(db, stack.id)
    except Exception as e:  # noqa: BLE001 — allocation display is best-effort
        logger.debug("host-port allocation read failed for stack %s: %s",
                     stack.id, e)
        return {}
    return allocs if isinstance(allocs, dict) else {}


def _normalize_allocated_host_port(service_fallback: Optional[str], e: Any) -> Optional[dict]:
    """gw-stack-networking GW.4c — one entry of the worker's
    `host_port_mappings` report in display form, or None when the entry is not
    salvageable (display field: never raises). A valid entry needs an integer
    host port in 1..65535 — the worker's own report already sanitises, but the
    stored JSONB is trusted-as-data, not trusted-as-shape."""
    if not isinstance(e, dict):
        return None
    host = e.get("host")
    if isinstance(host, bool) or not isinstance(host, int) or not 1 <= host <= 65535:
        return None
    target = e.get("target")
    if isinstance(target, bool) or not isinstance(target, int):
        target = None
    service = e.get("service") if e.get("service") is not None else service_fallback
    return {"service": str(service) if service is not None else None,
            "target": target, "host": host,
            "protocol": e.get("protocol") if e.get("protocol") in ("tcp", "udp") else "tcp"}


def _stack_allocated_host_ports(stack) -> list:
    """gw-stack-networking GW.4c — the host ports the worker ALLOCATED for
    `host: "any"` requests, read from `storage_refs["host_port_mappings"]`
    (recorded verbatim by `record_host_port_allocation` when a deploy result is
    ingested). Flat list of {service, target, host, protocol}; the worker's
    documented shape. A `{service: [entries...]}` dict mapping is tolerated for
    forward-compat, same as the ingest side. Malformed storage (wrong type,
    junk entries) yields whatever is salvageable — an all-junk mapping is just
    the empty list; a host port is only meaningful on the worker the stack
    runs on, so the caller pairs these with `target_worker`."""
    refs = getattr(stack, "storage_refs", None)
    if not isinstance(refs, dict):
        return []
    mapping = refs.get("host_port_mappings")
    if isinstance(mapping, list):
        entries = [(None, e) for e in mapping]
    elif isinstance(mapping, dict):
        entries = [(str(svc), e) for svc, items in mapping.items()
                   if isinstance(items, list)
                   for e in items]
    else:
        return []
    out = []
    for svc, e in entries:
        row = _normalize_allocated_host_port(svc, e)
        if row is not None:
            out.append(row)
    return out


def _stack_subnets_declared(stack) -> list:
    """Pinned `networks.*.ipam` subnets still on the stored spec (a legacy
    stack — GW.1 strips them on re-import). MOD allocates subnets; a pin here
    means the stack predates that and will collide if it shares a worker with
    a stack that needs the same range."""
    spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
    networks = spec.get("networks") if isinstance(spec.get("networks"), dict) else {}
    out = []
    for net_name, net_cfg in networks.items():
        if not isinstance(net_cfg, dict):
            continue
        ipam = net_cfg.get("ipam")
        if not isinstance(ipam, dict):
            continue
        for cfg in (ipam.get("config") or []):
            if isinstance(cfg, dict) and cfg.get("subnet"):
                out.append({"network": net_name, "subnet": cfg["subnet"]})
    return out


@router.get("/networking/usage", dependencies=[Depends(_require_platform_admin)])
def stack_networking_usage(db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """gw-stack-networking GW.4 — the fleet-wide host-port / subnet usage view.

    `report_host_port_and_subnet_usage` (GW.1) is read-only over the WHOLE fleet
    because the operator's question is fleet-scoped: "which stacks still publish
    raw host ports or pin subnets, and will they collide with each other?" — so
    it is admin-gated like the other fleet-wide views (/placement,
    /observations). `legacy` carries the raw `ports:` / pinned `ipam` offenders
    as GW.1 reported them; `managed` lists NEW-format stacks' declared
    `x-mod.host_ports` (what a deploy WOULD publish) so a fixed-port request can
    be checked against the fleet before two stacks ask for it. No writes."""
    from services import persistent_stacks_shared_service as _svc
    legacy = _svc.report_host_port_and_subnet_usage(db)
    rows = db.query(models.PersistentStack).all()
    managed = []
    for stack in rows:
        spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
        if not isinstance(spec.get("x-mod"), dict) or "ports" not in spec.get("x-mod"):
            continue
        hps = spec["x-mod"].get("host_ports") or []
        if not hps:
            continue
        managed.append({"id": stack.id, "name": stack.name, "host_ports": hps})
    return {"legacy": legacy, "managed": managed}


class StackHostPortsUpdate(BaseModel):
    """gw-stack-networking GW.4 — replace a stack's declared host publishes.
    Each entry is exactly the `x-mod.host_ports` shape the importer validates:
    {service, target, host: "any"|int, protocol: tcp|udp, bind?}. `host: "any"`
    asks the worker to allocate a free port (GW.2) — the UI shows the chosen
    mapping once the worker reports it, or 'pending' until then."""
    host_ports: List[dict] = Field(default_factory=list)


@router.put("/{stack_id}/host-ports", response_model=schemas.PersistentStack)
def set_stack_host_ports(stack_id: int,
                         body: StackHostPortsUpdate,
                         db: Session = Depends(get_db),
                         user: models.User = Depends(get_current_user)):
    """gw-stack-networking GW.4 — edit a stack's host-port requests (fixed port
    vs `any`), for a NEW-format stack (`x-mod.ports` present).

    Validated through the SAME `_validate_host_ports` the importer runs — the
    UI cannot smuggle a host publish the import boundary would refuse. Saved as
    a NEW revision (the stack-definition-edit idiom: append-only, revertible,
    does NOT redeploy — Deploy applies it to the worker). Same gate as every
    other stack-mutating endpoint here (router-wide STACK_AUTHOR + `_get_owned`
    tenant scoping + baseline `stack_edit`); audited like its siblings."""
    import services.persistent_stack_revisions as _psr
    from services import stack_compose_import as _sci

    stack = _get_owned(db, stack_id, user)
    _require_stack_edit_or_402(db, user)
    spec = dict(stack.compose_spec or {})
    top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}
    if "ports" not in top_xmod:
        raise HTTPException(status_code=409, detail={
            "code": "legacy_stack",
            "detail": ("this stack predates MOD-owned host ports (no x-mod.ports). "
                       "Re-import it, or edit its definition, before its host ports can "
                       "be managed here.")})
    try:
        normalized = _sci._validate_host_ports(
            body.host_ports, set(spec.get("services") or {}))
    except _sci.ComposeImportError as e:
        raise HTTPException(status_code=422, detail=e.as_text())
    new_top_xmod = dict(top_xmod)
    new_top_xmod["host_ports"] = normalized
    spec["x-mod"] = new_top_xmod

    _psr.latest(db, stack.id) or _psr.materialize_initial(db, stack)
    rev = _psr.create_revision(
        db, stack,
        origin=_psr.ORIGIN_EDITED,
        author_user_id=user.id,
        compose_spec=spec,
        storage_class=stack.storage_class,
        message="host ports updated")
    stack.compose_spec = spec
    db.add(stack)
    _audit_stack(db, "STACK_HOST_PORTS_SET", stack, user,
                 {"revision_no": rev.revision_no,
                  "host_ports": [str(e) for e in normalized]})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s host ports set to %s (revision %s) by user %s",
                stack.id, len(normalized), rev.revision_no, user.id)
    return _out(stack)


@router.get("/{stack_id}/networking")
def stack_networking(stack_id: int,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """gw-stack-networking GW.4 — this stack's endpoints, as the Dashboard's
    Endpoints cell shows them:

      * `mesh` — every reachable service's stable internal name + container
        port (the `<stack>-<service>` mesh address; port from the spec's
        `expose`/`ports`, falling back to `x-mod.ports` for a new-format stack);
      * `public` — the front door when exposed: MOD FQDN, custom domain,
        friendly FQDN, the front-door service/port (`x-mod.ingress`) and the
        edge target to CNAME at;
      * `host_ports` — the declared requests plus, per entry, the worker-
        reported allocation when present (GW.2) and `pending` when the request
        is `any` but no allocation has arrived yet;
      * `allocated_host_ports` (GW.4c) — the host ports the worker ACTUALLY
        bound for `any` requests, from `storage_refs["host_port_mappings"]`,
        each paired with `target_worker` (the host the port lives on — a host
        port is only meaningful with its host); empty when nothing allocated;
      * `subnets` — any pinned `ipam` subnet still on the spec (legacy).

    Read-only; tenant-scoped like the other stack GETs."""
    import copy as _copy
    stack = _get_owned(db, stack_id, user)
    spec = stack.compose_spec if isinstance(stack.compose_spec, dict) else {}
    services = spec.get("services") if isinstance(spec.get("services"), dict) else {}
    top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}

    # container port per service — mirror the UI's own read order (expose, then
    # legacy ports), then the new-format x-mod.ports record.
    ports_by_service = {}
    for p in (top_xmod.get("ports") or []):
        if isinstance(p, dict) and isinstance(p.get("service"), str):
            ports_by_service[p["service"]] = p.get("target")
    def _container_port(svc_name):
        svc = services.get(svc_name)
        if isinstance(svc, dict):
            exp = svc.get("expose")
            if isinstance(exp, list) and exp:
                v = str(exp[0]).split("/")[0].strip()
                if v:
                    return v
            pl = svc.get("ports")
            if isinstance(pl, list) and pl:
                p0 = pl[0]
                if isinstance(p0, dict) and p0.get("target") is not None:
                    return str(p0["target"])
                if p0 is not None:
                    parts = str(p0).split("/")[0].split(":")
                    tail = parts[-1].strip()
                    if tail:
                        return tail
        p = ports_by_service.get(svc_name)
        return str(p) if p is not None else None

    mesh = []
    for svc_name, entry in _stack_mesh_endpoints(stack).items():
        port = _container_port(svc_name)
        mesh.append({"service": svc_name, "name": entry.get("magicdns_name"),
                     "port": port,
                     "full": f"{entry.get('magicdns_name')}:{port}" if port
                             else entry.get("magicdns_name")})

    ingress = top_xmod.get("ingress") if isinstance(top_xmod.get("ingress"), dict) else None
    public = None
    if stack.exposed:
        public = {
            "public_hostname": stack.public_hostname,
            "custom_domain": stack.custom_domain,
            "friendly_fqdn": getattr(stack, "friendly_fqdn", None),
            "edge_target": _stack_networking_edge_target(),
            "ingress": ingress,
        }

    allocations = _stack_host_port_allocations(db, stack)
    host_ports = []
    for e in _stack_host_port_entries(stack):
        if e["host"] is None:
            # legacy raw `ports:` — not MOD-managed (no allocation concept)
            host_ports.append({**e, "allocation": None, "pending": False,
                               "legacy": True})
            continue
        alloc = allocations.get(e["service"]) if e["service"] in services else None
        pending = (e["host"] == "any") and not alloc
        host_ports.append({**e, "allocation": alloc, "pending": pending,
                           "legacy": False})

    return {
        "id": stack.id, "name": stack.name,
        "mesh": mesh,
        "public": public,
        "host_ports": host_ports,
        "allocated_host_ports": _stack_allocated_host_ports(stack),
        "target_worker": getattr(stack, "target_worker", None),
        "subnets": _stack_subnets_declared(stack),
        "lb_endpoint": stack.lb_endpoint,
    }

@ingress_router.get("/ingress/tls-authorize")
def tls_authorize(domain: str = Query(..., min_length=1, max_length=253),
                  token: Optional[str] = Query(None),
                  x_mod_ingress_token: Optional[str] = Header(None),
                  db: Session = Depends(get_db)):
    """caddy-stack-ingress §2.2 — internal on-demand-TLS `ask` endpoint. 200
    authorises Caddy to issue a cert for `domain`; 403 (token set but not matched)
    or 404 (host not authorised) refuses. A short negative-answer cache (§2.3)
    blunts on-demand issuance used as a DoS amplifier."""
    if not _ingress_token_ok(x_mod_ingress_token or token):
        raise HTTPException(status_code=403, detail="ingress authorisation denied")
    d = domain.strip().lower()
    now = time.time()
    exp = _ask_neg_cache.get(d)
    if exp is not None and exp > now:
        raise HTTPException(status_code=404, detail="host not authorised for TLS")
    if _ingress_authorized_domain(db, d):
        _ask_neg_cache.pop(d, None)
        return {"authorized": True, "domain": d}
    _ask_neg_cache[d] = now + _ASK_NEG_TTL_S
    if len(_ask_neg_cache) > 4096:  # bound the cache under a hostname flood
        for k, v in list(_ask_neg_cache.items()):
            if v <= now:
                _ask_neg_cache.pop(k, None)
    raise HTTPException(status_code=404, detail="host not authorised for TLS")

@mesh_internal_router.get("/mesh/reachability")
def mesh_reachability(x_mod_mesh_token: Optional[str] = Header(None),
                      token: Optional[str] = Query(None),
                      db: Session = Depends(get_db)):
    """dual-anchor-mesh-federation §2.1 — the manager's heartbeat registry as the
    reachability source of truth. Returns, per logical mesh name, the site and
    cloud overlay addresses and the currently-preferred anchor, derived purely
    from worker heartbeats (no new probing). The D6 resolver polls this and
    answers site-first / cloud-fallback. Empty `services` ⇒ the resolver keeps
    its MagicDNS-name behaviour (byte-identical to a mesh-off box)."""
    if not _mesh_reach_token_ok(x_mod_mesh_token or token):
        raise HTTPException(status_code=403, detail="mesh reachability denied")
    return _psd_svc.mesh_reachability(db)

@ingress_router.get("/ingress/byo-certs")
def ingress_byo_certs(token: Optional[str] = Query(None),
                      x_mod_ingress_token: Optional[str] = Header(None),
                      db: Session = Depends(get_db)):
    """caddy-stack-ingress §5 — internal discovery source the cert-watch sidecar
    can poll instead of the static `MOD_CADDY_BYO_CERTS` env: the byo cert paths
    to load into Caddy. Returns every tenant whose default TLS mode is `byo` and
    has a cert on file (each `{path, fqdn}` points at a Vault KV path). Guarded by
    the same optional ingress token as the `ask` endpoint; network-isolated
    otherwise. Fails closed on a bad token."""
    if not _ingress_token_ok(x_mod_ingress_token or token):
        raise HTTPException(status_code=403, detail="ingress authorisation denied")
    return _psd_svc.ingress_byo_certs(db)

@ingress_router.get("/ingress/tls-min-version")
def ingress_tls_min_version(token: Optional[str] = Query(None),
                            x_mod_ingress_token: Optional[str] = Header(None),
                            db: Session = Depends(get_db)):
    """FX.124 PART 3 (owner Q111=A) — internal seam the caddy-stack-ingress
    entrypoint (scripts/caddy-entrypoint.sh) reads AT RENDER TIME to learn the
    active TLS minimum version, so the stack-ingress site renders ``tls {
    protocols tls1.3 }`` on a gov (cmmc / fedramp_high_il5) install instead of
    ``tls1.2 tls1.3``. The value is the platform setting ``tls.min_version``
    (locked to "1.3" by the gov profiles — see services/compliance_profiles.
    py); the ingress reads it through the existing settings/profile API rather
    than a new env var, so a profile change is picked up on the next stack
    deploy with no edge code change. Guarded by the same optional ingress
    token as the ``ask``/byo-certs seams; network-isolated otherwise. Fails
    closed on a bad token; an unreadable setting degrades to "1.2" (never
    weaker than the baseline)."""
    if not _ingress_token_ok(x_mod_ingress_token or token):
        raise HTTPException(status_code=403, detail="ingress authorisation denied")
    from services import platform_settings as psr
    try:
        value = psr.get_setting("tls.min_version", db=db)
    except Exception:  # noqa: BLE001 — unreadable => baseline, never weaker
        value = "1.2"
    value = str(value)
    return {"min_version": value if value in ("1.2", "1.3") else "1.2"}

@ingress_router.get("/ingress/auth-snippet")
def ingress_auth_snippet(host: str = Query(...),
                         token: Optional[str] = Query(None),
                         x_mod_ingress_token: Optional[str] = Header(None),
                         db: Session = Depends(get_db)):
    """GX.S1c-stacks — internal snippet source for caddy-stack-ingress. The
    stack sites are rendered by the EXTERNAL ingress container (this API only
    serves its on-demand-TLS `ask` seam), so the ingress fetches the per-host
    forward-auth Caddy body here and drops it VERBATIM into the on-demand
    site, BEFORE its own reverse_proxy handler (Caddy directive order: the
    `handle_path /_mod/auth*` block must intercept before forward_auth would
    401/302 the login flow, and forward_auth before the handler proxies).

    Returns `{host, snippet, require_login}`. `snippet` is the
    `auth_flow_directives()` + `forward_auth_directives(cfg)` rendering from
    the stack's stored compose `x-mod.ingress.auth` block — "" (and
    require_login false) when the stack is not login-protected or the host is
    not a serving stack host. Fails closed: unknown host ⇒ empty snippet, bad
    token ⇒ 403 (same optional ingress token as the `ask`/byo-certs seams).
    """
    if not _ingress_token_ok(x_mod_ingress_token or token):
        raise HTTPException(status_code=403, detail="ingress authorisation denied")
    from services import stack_forward_auth
    stack, cfg = stack_forward_auth.get_stack_by_hostname(db, host)
    if stack is None:
        return {"host": (host or "").split(":")[0].strip().lower(),
                "snippet": "", "require_login": False}
    return {"host": (host or "").split(":")[0].strip().lower(),
            "snippet": stack_forward_auth.stack_forward_auth_site(stack),
            "require_login": bool(cfg["require_login"])}

@ingress_router.get("/ingress/hosts")
def ingress_hosts(token: Optional[str] = Query(None),
                  x_mod_ingress_token: Optional[str] = Header(None),
                  db: Session = Depends(get_db)):
    """GX.S1c-stacks p2B — the discovery seam caddy-stack-ingress calls BEFORE it
    renders, to learn which stack hosts need browser login in front of them.

    Returns `[{host, require_login}]`, one row per serving hostname (public_hostname
    / custom_domain / friendly_fqdn) of every EXPOSED, good-standing stack, with
    `require_login` from that stack's stored compose `x-mod.ingress.auth` block
    (same `stack_auth_config` the `auth-snippet` seam reads). The ingress fetches
    this once at render time, then fetches `/ingress/auth-snippet?host=<h>` for
    each `require_login` true host and emits an explicit site block for it BEFORE
    the wildcard site — so a login-protected host is never proxied unauthenticated
    (the ingress turns a failed/empty snippet fetch into a 503 responder).

    Hosts whose stack has no auth block are listed with require_login false so the
    ingress can tell "public" apart from "unknown": an absent host simply keeps the
    wildcard site's behaviour. Fails closed on a bad token (same optional ingress
    token as the `ask`/byo-certs seams). A DB failure yields 503 (fail closed,
    2400GX-n3g): never an empty list, which the edge would read as "no login
    hosts" and render wildcard-only, proxying login-protected stacks
    unauthenticated. On 503 the edge retries at start then exits, and at poll
    time keeps its current config. Only a successful read with zero exposed
    stacks returns `200 []`.
    """
    if not _ingress_token_ok(x_mod_ingress_token or token):
        raise HTTPException(status_code=403, detail="ingress authorisation denied")
    from services import stack_forward_auth
    try:
        from crud import persistent_stacks_shared as _pss_crud
        rows = _pss_crud.get_exposed_stacks(db)
    except Exception:  # noqa: BLE001 — unreadable ⇒ fail closed (503), never `[]`
        logger.warning("stack ingress host discovery failed", exc_info=True)
        raise HTTPException(status_code=503,
                            detail="ingress host discovery unavailable")
    hosts: Dict[str, bool] = {}
    for stack in rows:
        if not getattr(stack, "exposed", False):
            continue
        if not _stack_in_good_standing(stack):
            continue
        cfg = stack_forward_auth.stack_auth_config(stack)
        for name in (getattr(stack, "public_hostname", None),
                     getattr(stack, "custom_domain", None),
                     getattr(stack, "friendly_fqdn", None)):
            name = str(name or "").split(":")[0].strip().lower()
            if not name:
                continue
            # Two stacks claiming one host: the login-protected one wins (fail
            # closed — the ingress must never proxy a host unauthenticated).
            hosts[name] = hosts.get(name, False) or bool(cfg["require_login"])
    return [{"host": h, "require_login": r} for h, r in sorted(hosts.items())]


class StackIngressAuthUpdate(BaseModel):
    """30.4.3.6 — the stack's MOD login gate (`x-mod.ingress.auth`). Exactly the
    three keys `app_forward_auth.validate_config` stores; unknown keys are a 422."""
    model_config = {"extra": "forbid"}
    require_login: bool = False
    allowed_groups: List[str] = Field(default_factory=list)
    public_paths: List[str] = Field(default_factory=list)


@router.get("/{stack_id}/ingress-auth")
def get_stack_ingress_auth(stack_id: int,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """The stack's login gate as the ingress reads it
    (`stack_forward_auth.stack_auth_config`): {require_login, allowed_groups,
    public_paths}. Tenant-scoped via `_get_owned` (a foreign id is a 404)."""
    from services import stack_forward_auth
    stack = _get_owned(db, stack_id, user)
    return stack_forward_auth.stack_auth_config(stack)


@router.put("/{stack_id}/ingress-auth")
def set_stack_ingress_auth(stack_id: int,
                           body: StackIngressAuthUpdate,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """Set the stack's login gate — `x-mod.ingress.auth` — without hand-editing
    the compose (the stack twin of `PUT /apps/{id}/auth-config`).

    Same gate as `PUT .../definition`: router-wide STACK_AUTHOR + `_get_owned`
    tenant scoping + baseline `stack_edit`. Validated by the shared
    `app_forward_auth.validate_config` (400 with its message; nothing written).
    ONLY `x-mod.ingress.auth` changes: the spec is deep-copied, the block set,
    and the column REASSIGNED (JSONB mutation is not tracked). The audit row
    carries the old/new gate config and the actor — never the compose spec.

    Applies "on next ingress reload"; nothing is restarted here. A RUNNING
    stack also needs a redeploy for the login-gated host-port rebinding
    (`_render_stack_owned_ports`) — the gate alone takes effect at the next
    ingress re-render."""
    from services import app_forward_auth, stack_forward_auth

    stack = _get_owned(db, stack_id, user)
    _require_stack_edit_or_402(db, user)
    try:
        new_cfg = app_forward_auth.validate_config(
            body.require_login, body.allowed_groups, body.public_paths)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    new_cfg.pop("fail_closed", None)  # c2-design.md:84 — stack seam is platform-only (security.stack_tokens_fail_closed); no per-stack fail_closed
    old_cfg = stack_forward_auth.stack_auth_config(stack)

    spec = copy.deepcopy(stack.compose_spec) if isinstance(stack.compose_spec, dict) else {}
    xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else {}
    ingress = xmod.get("ingress") if isinstance(xmod.get("ingress"), dict) else {}
    ingress["auth"] = new_cfg
    xmod["ingress"] = ingress
    spec["x-mod"] = xmod
    stack.compose_spec = spec
    db.add(stack)
    _audit_stack(db, "STACK_INGRESS_AUTH_UPDATED", stack, user,
                 {"old": old_cfg, "new": new_cfg})
    db.commit()
    db.refresh(stack)
    logger.info("stack %s ingress-auth set (require_login=%s) by user %s",
                stack.id, new_cfg["require_login"], user.id)
    return {**new_cfg, "applies": "on next ingress reload"}

class StackDeployRequest(BaseModel):
    """30.6.3 c3-J4b2 — the deploy body. EVERYTHING here is optional: a bodiless
    POST .../deploy behaves EXACTLY as before (O6 regression), so the reconciler,
    the UI's plain "Redeploy" and every existing caller keep their contract.

    `image_overrides` maps a service NAME to the image ref to swap in for THIS
    deploy. The policy (services/stack_deploy_policy.py, c3-J4b1) is enforced in
    the SERVICE layer before any dispatch, so a denied override writes 0 rows;
    the model here only bounds the shape (≤32 entries, non-empty names ≤63 chars
    and refs ≤255 chars — 422 on anything else, `extra="forbid"` so a typo'd or
    smuggled key 422s rather than being ignored)."""
    model_config = ConfigDict(extra="forbid")
    image_overrides: Optional[Dict[constr(min_length=1, max_length=63),
                                   constr(min_length=1, max_length=255)]] = None
    # 30.6.3 c3-J4c — the deploy credential (30.6.3 design :43, body-only: no
    # `Idempotency-Key` header is added here, so the OpenAPI surface stays
    # unchanged). Charset/length are validated in the SERVICE layer with the
    # n8 posture (services/run_idempotency.py resolve_key) + a 64-char cap;
    # the model here only bounds the shape so a 65+ char key 422s at parse.
    idempotency_key: Optional[constr(min_length=1, max_length=64)] = None

    @_pyd_field_validator("image_overrides")
    def _at_most_32_overrides(v):
        if v is not None and len(v) > 32:
            raise ValueError("at most 32 image_overrides per deploy")
        return v


def _authorising_api_key(request: Request, db: Session = Depends(get_db)):
    """30.6.3 c3-J4c — the ApiKey row that authorised THIS request (None for a
    human Bearer / session caller), so the revision + audit can name its `id`
    (`key_id`).

    c3-J4c-fix: the contextvar alone is NOT enough — `require_bearer_or_api_key`
    is a SYNC dep, so FastAPI runs it (and this one) in a threadpool worker
    thread whose contextvar writes do not propagate back (the same gotcha
    routers/workflows_domains/workflow_by_slug.py:89 documents). The request-
    state marker the SAME funnel writes (dependencies.py:1360
    `_api_key_policy_enforced_id`) is the fallback; a human Bearer call sets
    neither, so `key_id` stays NULL (K3b)."""
    from utils.request_principal import get_api_key
    key = get_api_key()
    if key is not None:
        return key
    key_id = getattr(getattr(request, "state", None),
                     "_api_key_policy_enforced_id", None)
    if key_id is None:
        return None
    return (db.query(models.ApiKey)
            .filter(models.ApiKey.id == int(key_id)).first())


@router.post("/{stack_id}/deploy", response_model=schemas.PersistentStack)
def deploy_stack(stack_id: int,
                 to_worker: Optional[str] = None,
                 body: Optional[StackDeployRequest] = Body(None),
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_bearer_or_api_key),
                 authorising_key: Optional[models.ApiKey] = Depends(_authorising_api_key)):
    stack = _psd_svc.deploy_stack(db, stack_id, to_worker, user,
                                  image_overrides=getattr(
                                      body, "image_overrides", None),
                                  idempotency_key=getattr(
                                      body, "idempotency_key", None),
                                  key_id=(getattr(authorising_key, "id", None)
                                          if authorising_key is not None else None))
    return _out(stack)

@router.post("/{stack_id}/logs")
def stack_logs(stack_id: int,
               timeout_s: float = 60.0,
               db: Session = Depends(get_db),
               user: models.User = Depends(require_bearer_or_api_key)):
    """Fetch a remote stack's container logs, by asking the host that has them.

    WHY THIS EXISTS. A stack that dies on a remote worker is otherwise
    invisible from here. The deploy op returns COMPOSE's output — "Starting /
    Started" — which says the daemon accepted the container, not that it stayed
    up. On 2026-08-13 a model server was stopped repeatedly and the only way to
    read its reason was SSH-ing to the box by hand.

    Read-only and synchronous: the caller is a human looking at a broken stack,
    and handing them a task id to poll would be answering a different question.
    """
    stack = _get_owned(db, stack_id, user)
    # D76: a deploy refused at placement (fixed host port busy) never created a
    # container of this stack; anything under its project name belongs to
    # someone else (e.g. a leftover project from a previous install). Say so
    # instead of dispatching a logs read that would show that other stack.
    if (stack.status in ("deploying", "failed", "created")
            and (stack.status_reason or "").startswith("Host port ")
            and "is already used by" in (stack.status_reason or "")):
        return {
            "stack_id": stack.id,
            "project": _project_name(stack),
            "worker": stack.target_worker,
            "status": "completed",
            "logs": "no containers yet: this stack has not been placed. "
                    + stack.status_reason,
            "error": None,
            "stack_status": stack.status,
            "stack_reason": stack.status_reason,
        }

    worker = stack.target_worker
    if not worker:
        raise HTTPException(
            status_code=409,
            detail=f"stack {stack.id} has no host on record, so there is nowhere to ask")

    task = _dispatch_stack_task(db, worker, stack.tenant_id, "logs",
                                _project_name(stack), stack_id=stack.id)
    db.commit()
    task_id = task.id
    # Capture what the response needs as plain values, then RELEASE the request
    # DB connection before the poll. Under pgbouncer transaction pooling, holding
    # the request-scoped Session open across this (up to timeout_s) sleep-loop —
    # re-opening a transaction every 0.4s via db.get() — pins a server backend
    # for the whole wait and starves the pool. Each poll uses its own short-lived
    # session that is released immediately (see worker_tasks.py for the pattern).
    stack_id_val = stack.id
    project = _project_name(stack)
    # The stack's own status travels with the logs: a stack whose deploy failed
    # before any container existed has NO log output, and an empty pane reads as
    # "still loading" forever unless the caller is told why it is empty.
    stack_status = stack.status
    stack_reason = stack.status_reason
    _publish_event("task:available:CPU")
    db.close()

    from database import SessionLocal
    import time
    deadline = time.monotonic() + max(5.0, float(timeout_s))
    terminal = (models.ApiTaskStatus.COMPLETED, models.ApiTaskStatus.FAILED,
                models.ApiTaskStatus.CANCELLED, models.ApiTaskStatus.EXPIRED)
    status = None
    result = {}
    error_message = None
    while True:
        time.sleep(0.4)
        with SessionLocal() as poll_db:
            row = poll_db.get(models.ApiTaskQueue, task_id)
            status = row.status if row else None
            if status in terminal:
                result = dict(row.result_payload or {}) if row else {}
                error_message = (row.error_message or None) if row else None
                break
        if time.monotonic() > deadline:
            raise HTTPException(
                status_code=504,
                detail=(f"{worker} did not return logs for stack {stack_id_val} within "
                        f"{timeout_s:g}s — the host may be unreachable, which is "
                        f"itself the answer"))

    return {
        "stack_id": stack_id_val,
        "project": project,
        "worker": worker,
        "status": getattr(status, "value", str(status)),
        # Named `logs` even when the op failed: the worker returns its reason in
        # the same field, and a caller looking for "why is this broken" should
        # not have to know which key holds the answer today.
        "logs": (result.get("log_tail") or "").strip(),
        "error": error_message,
        "stack_status": stack_status,
        "stack_reason": stack_reason,
    }

@router.post("/{stack_id}/teardown", response_model=schemas.PersistentStack)
def teardown_stack(stack_id: int,
                   data_policy: Optional[str] = Query(
                       "erase", pattern="^(erase|retain)$",
                       description="erase (default): reclaim all managed storage as "
                                   "before. retain: keep the last confirmed snapshot "
                                   "until now + STACK_SNAPSHOT_RETENTION days."),
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    stack = _psd_svc.teardown_stack(db, stack_id, user, data_policy=data_policy)
    return _out(stack)

@router.post("/{stack_id}/backup")
def backup_stack(stack_id: int,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_bearer_or_api_key)):
    stack = _get_owned(db, stack_id, user)
    try:
        out = take_snapshot(db, stack, source="on_demand")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    stack.backup_last_run_at = datetime.now(timezone.utc)
    stack.backup_last_error = None
    stack.backup_last_error_at = None
    db.commit()
    _publish_event("task:available:CPU")
    return out

def _err(description: str) -> dict:
    """responses= entry shape (billing.py:586) — same ErrorResponse model, own words."""
    return {"model": schemas.ErrorResponse, "description": description}


_PROBE_TIMEOUT_S = 60.0


def _probe_stack_empty(db: Session, stack: models.PersistentStack, snap: dict,
                      timeout_s: Optional[float] = None) -> dict:
    """Ask the stack's worker what is actually in the stack's volumes + DB right now.

    WHY THIS EXISTS. A restore is destructive in one direction only: it writes the
    snapshot over whatever the volumes hold, and a volume that already has data
    ends up merged (or half-overwritten), not replaced. That is only harmless when
    the stack is empty — which is exactly the case an EXTERNAL seed snapshot is
    used for (a tenant brings their own archive and lands it on a fresh stack).
    Platform snapshots (manual/scheduled) are restores of THAT STACK's own data,
    so they never ask.

    Fail closed: the only answer this returns is a COMPLETED probe's payload.
    FAILED / CANCELLED / EXPIRED or a timeout raises 503 `stack_probe_unavailable`
    — an unknown emptiness is never treated as empty, and never silently allowed.

    Polls like `stack_logs` above: the request session is committed and left
    untouched (no open transaction across the wait, which under pgbouncer
    transaction pooling would pin a server backend for the whole sleep-loop),
    and every poll opens its own short-lived session.
    """
    from database import SessionLocal
    import time
    probe_volumes = [v for v in (snap.get("volumes") or {})
                     if not v.startswith("logical:")]
    task = _dispatch_stack_task(db, stack.target_worker, stack.tenant_id, "probe_empty",
                                _project_name(stack), stack_id=stack.id,
                                extra_payload={"probe_volumes": probe_volumes,
                                               "probe_logical": bool(snap.get("logical"))})
    db.commit()
    task_id = task.id
    worker = stack.target_worker
    stack_id_val = stack.id
    _publish_event("task:available:CPU")

    limit = float(timeout_s) if timeout_s is not None else float(_PROBE_TIMEOUT_S)
    terminal = (models.ApiTaskStatus.COMPLETED, models.ApiTaskStatus.FAILED,
                models.ApiTaskStatus.CANCELLED, models.ApiTaskStatus.EXPIRED)
    deadline = time.monotonic() + max(5.0, limit)
    status = None
    result: dict = {}
    while True:
        time.sleep(0.4)
        with SessionLocal() as poll_db:
            row = poll_db.get(models.ApiTaskQueue, task_id)
            status = row.status if row else None
            if status in terminal:
                result = dict(row.result_payload or {}) if row else {}
                break
        if time.monotonic() > deadline:
            raise ApiError(
                503, "stack_probe_unavailable",
                (f"worker {worker} did not answer the emptiness probe for stack "
                 f"{stack_id_val} within {limit:g}s, so the stack's emptiness is "
                 f"unknown and the restore is refused"))
    if status is not models.ApiTaskStatus.COMPLETED:
        raise ApiError(
            503, "stack_probe_unavailable",
            (f"worker {worker} could not run the emptiness probe for stack "
             f"{stack_id_val} (task ended {getattr(status, 'value', status)}), so the "
             f"stack's emptiness is unknown and the restore is refused"))
    return result


@router.post(
    "/{stack_id}/restore",
    response_model=schemas.PersistentStack,
    responses={
        404: _err("No snapshot with that id exists on this stack."),
        409: _err("Stack is not deployed to a worker, or (code stack_not_empty) an "
                  "external seed snapshot found the stack already non-empty and "
                  "force was not set."),
        503: _err("code stack_probe_unavailable: the emptiness probe did not complete "
                  "on the stack's worker, so emptiness is unknown and the restore is "
                  "refused (fail closed)."),
    },
)
def restore_stack(stack_id: int, snapshot_id: str,
                  force: bool = Query(
                      False,
                      description="Only meaningful for an external (seed-committed) "
                                  "snapshot: skip the emptiness probe and restore over "
                                  "a stack that already has data. Audited as "
                                  "STACK_RESTORE_FORCED. A no-op on platform snapshots."),
                  db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    stack = _get_owned(db, stack_id, user)
    if not stack.target_worker:
        raise HTTPException(status_code=409, detail="Stack is not deployed to a worker")
    snap = ((stack.storage_refs or {}).get("snapshots") or {}).get(snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found for this stack")
    if snap.get("source") == "external":
        # 30.6.1 c1-J4 (Collective MUST): an external archive lands ON THIS STACK.
        # If the stack already holds data the tar merge is not a restore, it is a
        # silent corruption of someone else's work — so emptiness is PROVEN first,
        # before a single logical or volume task exists. `force` is the operator's
        # explicit "I know, overwrite anyway", and it is audited because it is the
        # one path that destroys data with no proof behind it.
        if force:
            _audit_stack(db, "STACK_RESTORE_FORCED", stack, user,
                         {"snapshot_id": snapshot_id, "seed_id": snap.get("seed_id")})
        else:
            probe = _probe_stack_empty(db, stack, snap)
            counts = probe.get("volumes") or {}
            dirty = sorted(v for v, n in counts.items() if (n or 0) > 0)
            tables = probe.get("user_tables")
            if dirty or (tables or 0) > 0:
                why = ", ".join(dirty) if dirty else ""
                if tables:
                    why = (f"{why}, {tables} user tables" if why
                           else f"{tables} user tables")
                raise ApiError(
                    409, "stack_not_empty",
                    (f"stack {stack_id} already holds data — {why} — so this external "
                     f"snapshot would overwrite it; pass force=true to restore anyway"))
    # GA.ASW.5 — a mixed/logical snapshot's `pg_restore` part runs ALONGSIDE
    # the ordinary tar-restore loop below, not instead of it: `volumes` still
    # carries the tar entries for every OTHER backed-up volume (the DB
    # service's own volume was excluded when the snapshot was taken, in
    # favor of the dump), so both must run for the stack to come back whole.
    if snap.get("logical"):
        try:
            restore_logical_snapshot(db, stack, snap)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    # In-place restore of a DEPLOYED stack — pass the project so the worker stops
    # the stack before restoring and starts it after (restoring over a live
    # container silently loses data).
    project = _project_name(stack)
    # 30.6.1 c1-J3c: the tar-extract bomb budget for an EXTERNAL seed comes from the
    # platform setting — read once per request, with NO tenant_id, so a tenant-scoped
    # PlatformSetting row can never raise it. Non-external restores pass None, which
    # leaves the payload byte-identical to before this change.
    cap = None
    if snap.get("source") == "external":
        from services import platform_settings as psr
        from services import stack_seed as _stack_seed  # noqa: F401  (registers the stack.seed.* settings)
        cap = int(psr.get_setting("stack.seed.max_uncompressed_bytes", db=db))
    for v, key in snap["volumes"].items():
        if v.startswith("logical:"):
            continue  # the pseudo-entry the dump is filed under above; not a real docker volume
        # Review2 N1: stack_id lets the worker-controlled start loop see the
        # in-place restore in flight (it stops the stack on purpose) and never
        # queue a boot_restore next to it.
        _dispatch_volume_task(db, stack.target_worker, stack.tenant_id, "restore", v,
                              snap["bucket"], key, project=project,
                              stack_id=stack.id,
                              max_uncompressed_bytes=cap)
    db.commit()
    _publish_event("task:available:CPU")
    logger.info("persistent stack %s restore %s dispatched (in-place stop/start)", stack.id, snapshot_id)
    return _out(stack)

@router.post("/{stack_id}/rehearse")
def rehearse_restore(stack_id: int, snapshot_id: str,
                     db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """§9.4 — prove a snapshot restores, without touching the live stack.

    An untested backup is a hypothesis. The first restore attempt should not be
    during an incident — but the obvious way to test one is also the single most
    destructive thing you can do to a healthy stack, which is why nobody does
    it. So this never goes near the real volume: the worker restores into a
    scratch volume, checks what actually landed, and removes the scratch copy on
    every path out.

    Deliberately NOT the `restore` op with a different volume name. That op
    stops and starts the project when it is given one, and a rehearsal's whole
    guarantee is that it cannot stop the live service — a guarantee that must
    not depend on this function remembering to omit an argument.

    Reporting failure is the point of the feature. A tar that unpacks cleanly to
    an empty directory exits zero, so the worker fails the task when nothing
    lands rather than reporting a successful restore of nothing.
    """
    stack = _get_owned(db, stack_id, user)
    if not stack.target_worker:
        raise HTTPException(status_code=409, detail="Stack is not deployed to a worker")
    snap = ((stack.storage_refs or {}).get("snapshots") or {}).get(snapshot_id)
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot not found for this stack")

    cap = None
    if snap.get("source") == "external":
        from services import platform_settings as psr
        from services import stack_seed as _stack_seed  # noqa: F401  (registers the stack.seed.* settings)
        cap = int(psr.get_setting("stack.seed.max_uncompressed_bytes", db=db))
    for v, key in (snap.get("volumes") or {}).items():
        _dispatch_volume_task(db, stack.target_worker, stack.tenant_id, "rehearse", v,
                              snap.get("bucket", SNAPSHOT_BUCKET), key,
                              stack_id=stack.id, snapshot_id=snapshot_id,
                              max_uncompressed_bytes=cap)
    # Clearing the previous verdict is deliberate: while a rehearsal is in
    # flight, the honest answer to "is this verified" is "we are finding out",
    # not last month's result. Leaving a stale `verified_at` up would mean a
    # rehearsal that is about to fail still reads as proven.
    _mark_snapshot(db, stack, snapshot_id, {
        "rehearsal_started_at": datetime.now(timezone.utc).isoformat(),
        "rehearsal_volumes": {},
        "verified_at": None,
        "verify_error": None,
    })
    _audit_stack(db, "STACK_RESTORE_REHEARSED", stack, user,
                 {"snapshot_id": snapshot_id})
    db.commit()
    _publish_event("task:available:CPU")
    logger.info("persistent stack %s rehearsal of snapshot %s dispatched to %s",
                stack.id, snapshot_id, stack.target_worker)
    return {
        "stack_id": stack.id,
        "snapshot_id": snapshot_id,
        "volumes": list((snap.get("volumes") or {}).keys()),
        "note": ("Rehearsing into a scratch volume. The live stack is not "
                 "stopped and its data is not touched. Verification status "
                 "appears against the snapshot when the worker reports back."),
    }

@router.put("/{stack_id}/backup-schedule")
def set_backup_schedule(stack_id: int, body: BackupScheduleIn,
                        db: Session = Depends(get_db),
                        user: models.User = Depends(get_current_user)):
    stack = _get_owned(db, stack_id, user)

    if body.clear or body.interval_hours is None:
        stack.backup_interval_hours = None
        db.commit()
        _audit_stack(db, "STACK_BACKUP_SCHEDULE_CLEARED", stack, user, {})
        db.commit()
        return {"stack_id": stack.id, "interval_hours": None,
                "note": "No schedule. This stack is backed up only when someone asks."}

    if body.interval_hours < 0:
        raise HTTPException(status_code=422,
                            detail="interval_hours cannot be negative")
    # An hourly floor, not a minutely one. A snapshot tars and uploads every
    # volume the stack owns; scheduling that more often than it takes to run
    # would queue backups faster than they drain.
    if 0 < body.interval_hours < 1:
        raise HTTPException(status_code=422,
                            detail="The shortest supported interval is 1 hour")

    stack.backup_interval_hours = body.interval_hours
    db.commit()
    _audit_stack(db, "STACK_BACKUP_SCHEDULE_SET", stack, user,
                 {"interval_hours": body.interval_hours})
    db.commit()
    return {
        "stack_id": stack.id,
        "interval_hours": body.interval_hours,
        "note": ("Scheduled backups are disabled for this stack."
                 if body.interval_hours == 0 else
                 f"A snapshot will be taken every {body.interval_hours}h."),
    }

@router.get("/{stack_id}/backups")
def stack_backups(stack_id: int,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(require_bearer_or_api_key)):
    """§9.2/§9.5/§9.6/§9.7/§9.9 — everything about this stack's recoverability.

    One call, because a user asking "is my data safe" is asking all of it at
    once: is anything scheduled, did the last one work, has any of it ever been
    proven to restore, how far back does it reach, and what is about to be
    deleted. Answered separately, the questions nobody thinks to ask are the
    ones that matter.
    """
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_backup", "Stack backups")
    try:
        from services.stack_backup import retention_report, weight_cache_note
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    stack = _get_owned(db, stack_id, user)
    snaps = ((stack.storage_refs or {}).get("snapshots") or {})

    rows = []
    for snap_id, snap in sorted(snaps.items(),
                                key=lambda kv: kv[1].get("created_at", ""),
                                reverse=True):
        rows.append({
            "snapshot_id": snap_id,
            "created_at": snap.get("created_at"),
            "source": snap.get("source") or "on_demand",
            "volumes": list((snap.get("volumes") or {}).keys()),
            # §9.5 — three states, not two. "Verified", "failed verification"
            # and "never tested" are different, and collapsing the last two
            # would report an untested backup as a broken one (or worse, the
            # reverse).
            "verified_at": snap.get("verified_at"),
            "verify_error": snap.get("verify_error"),
            "verification": ("verified" if snap.get("verified_at")
                             else "failed" if snap.get("verify_error")
                             else "untested"),
        })

    # §9.2 — a stack with no schedule must be distinguishable from one with a
    # schedule, and from one deliberately set to none.
    interval = stack.backup_interval_hours
    if interval is None:
        schedule = {"state": "unset", "interval_hours": None,
                    "note": ("No backup schedule has been set for this stack. "
                             "It is backed up only when someone remembers to "
                             "ask.")}
    elif interval == 0:
        schedule = {"state": "disabled", "interval_hours": 0,
                    "note": "Scheduled backups are deliberately off for this stack."}
    else:
        schedule = {"state": "scheduled", "interval_hours": interval,
                    "note": f"A snapshot is taken every {interval}h."}
    schedule["last_run_at"] = (stack.backup_last_run_at.isoformat()
                               if stack.backup_last_run_at else None)

    unverified = [r for r in rows if r["verification"] != "verified"]
    return {
        "stack_id": stack.id,
        "name": stack.name,
        "schedule": schedule,
        # §9.3 — the last scheduled failure, against the stack.
        "last_error": stack.backup_last_error,
        "last_error_at": (stack.backup_last_error_at.isoformat()
                          if stack.backup_last_error_at else None),
        "snapshots": rows,
        "retention": retention_report(stack),
        # §9.9 — say why the weights are not in here, where someone would
        # otherwise assume they were.
        "weight_cache_note": weight_cache_note(stack),
        # §9.5 — the alert. Not a per-snapshot detail: "no snapshot of this
        # stack has ever been proven to restore" is a statement about the
        # stack's recoverability and belongs at the top.
        "recoverability_warning": (
            None if not rows else
            ("No snapshot of this stack has ever been verified. Its "
             "recoverability is unproven — rehearse a restore to find out "
             "before an incident does." if len(unverified) == len(rows) else None)),
    }

@router.post("/{stack_id}/backfill", dependencies=[Depends(_require_platform_admin)])
def backfill_stack(stack_id: int, to_worker: str, target_region: Optional[str] = None,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """add-multi-region-mesh §6.2 — pull+cache the stack's data into another
    region's worker, ONLY when work must run there. Reuses the snapshot/restore-
    to-MinIO primitive; the source copy stays intact (it remains the durability
    replica). No-op when the data already lives in the target region (data-gravity:
    nothing to move). Two-phase and race-free: if no snapshot exists yet, it
    creates one and the restore is a follow-up call once the snapshot lands."""
    _require_paid(_PAID_SERVICE, "Multi-region fleet backfill")
    return _psd_svc.backfill_stack(db, stack_id, to_worker, target_region, user)

@router.post("/{stack_id}/migrate", response_model=schemas.PersistentStack, dependencies=[Depends(_require_platform_admin)])
def migrate_stack(stack_id: int, to_worker: Optional[str] = None, freeze: bool = False,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """Blue/green move the stack to a new generation (optionally on another worker):
    snapshot the current volumes → restore into a fresh generation → bring it up →
    drain the old. One pinned `migrate` PLATFORM_TASK on the target worker.

    `freeze=true` (§5.2) stops the old generation BEFORE the final snapshot so no
    write is missed during the migrate window — a brief write-freeze for write-heavy
    stacks. Default false = live migrate (no downtime; fine for cold/idle stacks)."""
    _require_paid(_PAID_SERVICE, "Stack migrate")
    stack = _psd_svc.migrate_stack(db, stack_id, to_worker, freeze, user)
    return _out(stack)

@router.post("/{stack_id}/upgrade", response_model=schemas.PersistentStack,
             dependencies=[Depends(_require_platform_admin)])
def upgrade_stack(stack_id: int, body: StackUpgradeRequest,
                  db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    """GA.ASW.7 — safe stack upgrade: snapshot (if the stack backs itself up)
    -> deploy `body.new_spec` -> grade health -> auto-rollback to the prior
    spec if it does not turn healthy within `body.window_s`.

    Returns immediately with the stack mid-deploy; it does not block on the
    grading window. `services.stack_upgrade.evaluate_upgrade` is the step
    function that carries the upgrade to a terminal (succeeded/rolled_back)
    state, polled rather than awaited here."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_upgrade", "Safe stack upgrade")
    try:
        from services import stack_upgrade as _su
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    kwargs = {}
    if body.window_s is not None:
        kwargs["window_s"] = body.window_s

    # C5b — snapshot the PRE-upgrade hosts before the service overwrites
    # `stack.compose_spec` (it does: `services/stack_upgrade.py` sets
    # `stack.compose_spec = new_spec` and commits inside `upgrade_stack`), so
    # the declaration audit can diff them. The service owns the ownership
    # check + the 409 "upgrade already in progress" guard, so this read is
    # only the snapshot; the audit itself runs after the service's
    # STACK_UPGRADE_STARTED audit, on the refreshed stack it returns.
    _pre_upgrade_compose = copy.deepcopy(_get_owned(db, stack_id, user).compose_spec)
    stack = _su.upgrade_stack(db, stack_id, body.new_spec, user, **kwargs)
    _record_egress_declarations(db, stack, _pre_upgrade_compose, user.id)
    db.commit()
    return _out(stack)

@router.post("/{stack_id}/migrate-streaming", status_code=202,
             dependencies=[Depends(_require_platform_admin)])
def migrate_stack_streaming(stack_id: int, body: StackStreamingMigrationStart,
                            db: Session = Depends(get_db),
                            user: models.User = Depends(get_current_user)):
    """GA.ASW.10.7 — start a streaming (near-zero-downtime) DB migration from
    `stack_id` onto `body.target_stack_id`: `pg_basebackup`-seeds the target
    as a standby, replicates until lag drains, verifies (row counts + optional
    `body.smoke_sql`), then waits for an explicit `.../cutover` call. Returns
    202 immediately with the fresh `prepare`-phase record — the state machine
    is stepped forward by `services.stack_streaming_migration
    .evaluate_streaming_migration`, polled by the GA.ASW.10.8 scheduler tick,
    not awaited here.

    Validation errors (unsupported engine, cross-tenant target, target ==
    source, migration already in progress) are surfaced as 422."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_streaming_migration", "Streaming stack migration")
    try:
        from services import stack_streaming_migration as _ssm
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    kwargs = {}
    if body.cutover_window_s is not None:
        kwargs["cutover_window_s"] = body.cutover_window_s
    try:
        stack = _ssm.start_streaming_migration(
            db, stack_id, body.target_stack_id, user,
            smoke_sql=body.smoke_sql, **kwargs)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _ssm.get_streaming_migration(stack)

@router.get("/{stack_id}/migrate-streaming",
            dependencies=[Depends(_require_platform_admin)])
def get_stack_migrate_streaming(stack_id: int, db: Session = Depends(get_db),
                                user: models.User = Depends(get_current_user)):
    """GA.ASW.10.7 — the current `streaming_migration` record for `stack_id`
    (phase, lag, row-count diff, timings, error/reason), or 404 if no
    streaming migration has ever been started for this stack."""
    stack = _get_owned(db, stack_id, user)
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_streaming_migration", "Streaming stack migration")
    try:
        from services import stack_streaming_migration as _ssm
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    record = _ssm.get_streaming_migration(stack)
    if record is None:
        raise HTTPException(status_code=404,
                            detail="no streaming migration on record for this stack")
    return record

@router.post("/{stack_id}/migrate-streaming/cutover",
             dependencies=[Depends(_require_platform_admin)])
def migrate_stack_streaming_cutover(stack_id: int, db: Session = Depends(get_db),
                                    user: models.User = Depends(get_current_user)):
    """GA.ASW.10.7 — operator confirmation to proceed from `verify` into
    `cutting_over` once the verify gate has passed (row counts match, lag
    drained, smoke query ok). `advance_phase` itself enforces that the
    transition is actually allowed from the record's current phase, so a
    premature call (still in `seeding`/`replicating`/`verify` with a failed
    gate) is rejected as 422 rather than forced through."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_streaming_migration", "Streaming stack migration")
    try:
        from services import stack_streaming_migration as _ssm
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    try:
        record = _ssm.advance_phase(db, stack_id, _ssm.PHASE_CUTTING_OVER, user)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return record

@router.post("/{stack_id}/migrate-streaming/abort",
             dependencies=[Depends(_require_platform_admin)])
def migrate_stack_streaming_abort(stack_id: int, body: StackStreamingMigrationAbort,
                                  db: Session = Depends(get_db),
                                  user: models.User = Depends(get_current_user)):
    """GA.ASW.10.7 — abandon an in-flight streaming migration (any
    non-terminal phase) and mark it `rolled_back`. The source stack was never
    stopped/mutated before `cutting_over`, so aborting earlier phases is just
    dropping the record; aborting mid `cutting_over` relies on the same
    rollback path `advance_phase`'s transition table already exercises for a
    failed verify gate."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_streaming_migration", "Streaming stack migration")
    try:
        from services import stack_streaming_migration as _ssm
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    try:
        record = _ssm.advance_phase(db, stack_id, _ssm.PHASE_ROLLED_BACK, user,
                                    reason=body.reason)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return record

@router.get("/observations/drift", dependencies=[Depends(_require_platform_admin)])
def stack_drift(db: Session = Depends(get_db)):
    """Read-only disagreement between expected and observed stack placement.

    stack-ha-reconciliation §1.4. Reports, acts on nothing — the reconciler
    (§4) is the only thing that decides to close a gap.

    Two shapes, kept separate because they call for opposite responses:

      `claimed_not_observed` — the stack row says running and no worker sees it.
        This is the failure measured on the dev deployment: two vLLM stacks
        exited 2026-07-28 and status still read `running` two days later, each
        holding its accelerator reservation out of the node-task pool the whole
        time because nothing ever asked a worker what it was running.

      `observed_not_claimed` — containers running that no stack row claims,
        usually a teardown whose worker task never landed. Surfaced rather than
        reaped: killing containers on the strength of an unmatched project name
        is not a decision to make automatically.

    A stack absent from BOTH lists is either genuinely healthy or deliberately
    stopped; `status` and (once §2 lands) desired state distinguish those.
    """
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_observation", "Stack drift observability")
    try:
        from services.stack_observation import drift_report
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    return drift_report(db)

@router.post("/{stack_id}/stop", response_model=schemas.PersistentStack)
def stop_stack(stack_id: int,
               db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """Stop a stack deliberately, without tearing it down.

    stack-ha-reconciliation §2.3. The distinction from teardown is the point:
    teardown destroys the stack and its storage, this says "stay down for now"
    while keeping the row, the compose spec and the data.

    Recording the intent is what stops reconciliation from fighting the
    operator. Without it, a deliberate stop is indistinguishable from a crash —
    the operator stops the stack, the reconciler puts it straight back, and
    neither side can win.
    """
    stack = _get_owned(db, stack_id, user)
    worker_name = stack.target_worker
    # A `stop` must reach the specific box holding the containers — there is no
    # other way to stop a compose project. Without a recorded host there is
    # nowhere to send it, and a PENDING+unpinned stop task would be claimable by
    # ANY worker (incident 2026-09-28: task 44953 was claimed by
    # worker-example-host, "stopped" a project it does not host, reported
    # COMPLETED, and the row was marked stopped while the stack still ran on
    # its real host). Refuse rather than record a false stop.
    if not worker_name:
        raise HTTPException(
            status_code=409,
            detail=f"stack {stack.id} has no host on record, so there is nowhere to stop it")

    # persistent-stack-billing §5.1 — bill the interval actually held (final
    # partial charge to metered_through_at), then clear the anchor so nothing
    # accrues while the stack is stopped. No refund of an already-billed
    # interval: you held the slot, you pay for the time you held it.
    # GT.D5 / DS-2 — CE: `stack_billing` is dropped, so there is no
    # reservation meter to finalize; skip (no-op).
    _sb = _optional_paid("services.stack_billing")
    try:
        if _sb is not None:
            _sb.finalize_stack_meter(db, stack)
    except Exception as e:  # noqa: BLE001
        logger.warning("stack %s: final reservation charge failed: %s", stack.id, e)

    stack.desired_state = "stopped"
    stack.nim_wake_owned = False  # ownership released with the stop
    _dispatch_stack_task(db, worker_name, stack.tenant_id, "stop",
                         _project_name(stack), stack_id=stack.id)
    _publish_event("task:available:CPU")
    stack.status_reason = "stopped by operator"
    _audit_stack(db, "STACK_STOPPED", stack, user, {"host": worker_name})
    db.commit()
    db.refresh(stack)
    logger.info("persistent stack %s stopped by operator (host=%s)",
                stack.id, worker_name or "none")
    return _out(stack)

@router.get("/reconcile/preview", dependencies=[Depends(_require_platform_admin)])
def reconcile_preview(db: Session = Depends(get_db)):
    """What reconciliation would do right now, without doing it.

    stack-ha-reconciliation §4.2. Always a dry run regardless of
    STACK_RECONCILER_LIVE, so this is safe to call at any time.

    Returns every gap AND every decision, including the ones where the answer
    was "do nothing" — because "the reconciler ignored this" and "the
    reconciler decided not to act, for this reason" look identical from the
    outside, and the second is usually the correct behaviour. An `external`
    stack that is down is reported here with the reason it will never be
    re-placed automatically, which is exactly the thing an operator needs to
    see during an incident.
    """
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_reconciler", "Automatic-recovery reconciler")
    try:
        from services.stack_reconciler import (
            live_mode_enabled, read_last_run, reconcile_once, reconciler_enabled)
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    result = reconcile_once(db, dry_run=True)
    result["reconciler_enabled"] = reconciler_enabled(db)
    result["live_mode"] = live_mode_enabled(db)
    # What the SCHEDULED job last decided, as opposed to what this on-demand
    # call just computed. The job runs in whichever uvicorn worker holds the
    # scheduler lock, so this is the only way to see its output from an
    # arbitrary worker — and the only trace it leaves at all while in dry run.
    result["last_scheduled_run"] = read_last_run()
    if not result["live_mode"]:
        result["note"] = ("live mode is OFF — the scheduled job reports but "
                          "never enqueues. Set STACK_RECONCILER_LIVE=true to "
                          "let it act.")
    return result

@router.get("/llm-idle/preview", dependencies=[Depends(_require_platform_admin)])
def llm_idle_preview(db: Session = Depends(get_db)):
    """What idle reclamation would stop right now, without stopping it.

    llm-stack-wake-idle §2. Always a dry run regardless of LLM_IDLE_ENABLED, so
    it is safe to call at any time.

    Reports every managed-LLM stack with its verdict, including the ones being
    KEPT and why — a stack held up because an interactive tool requires warm
    capacity looks identical from outside to one the reclaimer simply missed,
    and the difference is the whole point of per-tool policy.
    """
    # GT.D5 / DS-2 — predictive sleep surface is wholly Paid (HARD RULE): 402.
    _require_paid("services.llm_idle", "Predictive LLM-stack idle reclaim")
    try:
        from services.llm_idle import reclaim_once
        from services.llm_usage import usage_summary
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    result = reclaim_once(db, dry_run=True)
    # The call spacing an operator needs in order to pick a threshold at all.
    result["usage"] = usage_summary(db, limit=25)
    return result

@router.get("/llm-idle/settings", dependencies=[Depends(_require_platform_admin)])
def get_llm_idle_settings(db: Session = Depends(get_db)):
    """Current platform defaults, plus every stack's effective policy.

    Returns the resolved value AND where it came from. "Why did this stack
    stop?" is the question an admin actually asks, and "the platform default
    said so" and "you set 900s on this stack" are different answers.
    """
    _require_paid(_PAID_SERVICE, "Predictive LLM-stack idle reclaim")
    return _psd_svc.get_llm_idle_settings(db)

@router.put("/llm-idle/settings", dependencies=[Depends(_require_platform_admin)])
def update_llm_idle_settings(body: LLMIdleSettingsIn,
                             current_user: models.User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    """Set platform-wide idle-reclaim defaults.

    Turning this on is a real operational change — stacks will begin stopping
    themselves — so the response echoes what would be reclaimed under the new
    settings rather than leaving the admin to guess.
    """
    # GT.D5 / DS-2 — predictive sleep surface is wholly Paid (HARD RULE): 402.
    # FIX-AICONFIG-IDLE-APPLY-CE — DENY BEFORE MUTATION: the guard used to sit
    # BELOW the row write + `db.commit()`, so a CE Apply created the singleton
    # row, wrote `enabled`/`idle_after_seconds`/`updated_by_user_id`, COMMITTED
    # them, and only then 402s — platform state mutated by a call that then
    # reports failure (the Dashboard showed "Could not apply: Pro feature: ...").
    # The sibling routes already guard first (GET :2575, per-stack PUT :2632);
    # this is the same order they use. No-op for the optional path.
    _require_paid("services.llm_idle", "Predictive LLM-stack idle reclaim")
    row = _idle_settings_row(db)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.idle_after_seconds is not None:
        row.idle_after_seconds = body.idle_after_seconds
    if body.warm_required_tools is not None:
        row.warm_required_tools = [t.strip() for t in body.warm_required_tools if t.strip()]
    row.updated_by_user_id = current_user.id
    db.commit()
    db.refresh(row)

    try:
        from services.llm_idle import reclaim_once
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    preview = reclaim_once(db, dry_run=True)
    return {
        "platform": {
            "enabled": row.enabled,
            "idle_after_seconds": row.idle_after_seconds,
            "warm_required_tools": row.warm_required_tools or [],
        },
        "would_reclaim": [p for p in preview["plans"] if p["action"] == "reclaim"],
        # §7.6 — what is being KEPT, and why. "Nothing is idle enough to
        # reclaim" and "three stacks are idle but held up by an operator pin"
        # look identical when only the reclaim list is shown, and the second is
        # the one that explains why capacity never comes back.
        "kept": [{"stack_id": p["stack_id"], "name": p["name"],
                  "reason": p.get("reason"),
                  "pinned": "pinned" in (p.get("reason") or "").lower(),
                  "would_reclaim": bool(p.get("would_reclaim"))}
                 for p in preview["plans"] if p["action"] == "keep"],
    }

@router.put("/{stack_id}/llm-idle", dependencies=[Depends(_require_platform_admin)])
def update_stack_idle_override(stack_id: int, body: StackIdleOverrideIn,
                               db: Session = Depends(get_db)):
    """Override idle reclaim for ONE stack.

    `clear` resets both fields to inherit. Setting `idle_enabled=false` is not
    the same as clearing it: false pins this stack warm regardless of the
    platform default, which is what an always-on interactive assistant wants.
    """
    _require_paid(_PAID_SERVICE, "Predictive LLM-stack idle reclaim")
    return _psd_svc.update_stack_idle_override(db, stack_id, body)

@router.post("/{stack_id}/pin-warm", dependencies=[Depends(_require_platform_admin)])
def pin_stack_warm(stack_id: int, pinned: bool = True,
                   db: Session = Depends(get_db)):
    """Hold a stack up regardless of idle policy, or release that hold.

    The operator override. Per-tool policy covers "this tool needs warm
    capacity"; this covers "leave this specific stack alone", which is what
    somebody wants at 2am during an incident and should not require editing an
    environment variable and restarting the API.
    """
    _require_paid(_PAID_SERVICE, "Predictive LLM-stack idle reclaim")
    return _psd_svc.pin_stack_warm(db, stack_id, pinned)

@router.post("/{stack_id}/wake", dependencies=[Depends(_require_platform_admin)])
def wake_stack(stack_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    """Operator-triggered wake of one stack (llm-stack-wake-idle §4.2.3).

    `pin-warm` refuses to let a stack sleep; it never brings an OFF stack up.
    This is the generic wake endpoint: it routes the request through the SAME
    `ensure_stack_ready` engine a node uses, so the fleet rules (competitor
    eviction, the won't-fit HELD signal, wake-ownership) behave exactly as they
    do for a node dispatch. Returns the engine's verdict, not a bare 202, so
    an operator can see whether the stack is up, was woken by this call, or is
    HELD because it would not fit the fleet.
    """
    _require_paid(_PAID_SERVICE, "Operator-triggered stack wake")
    return _psd_svc.wake_stack(db, stack_id, actor=user.id)

@router.put("/{stack_id}/host-pin", dependencies=[Depends(_require_platform_admin)])
def set_host_pin(stack_id: int, body: HostPinIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    """§8.1/§8.3/§8.5 — fix a stack to a host, permanently.

    Distinct from /migrate's `to_worker`, which moves a stack ONCE and then
    stops meaning anything — the next rebalancer pass was free to move it
    straight back. This constrains placement until someone clears it, and
    survives stop/start because it lives on the stack row rather than in the
    dispatch that happened to move it.

    Setting a pin does NOT move the stack. Placement and relocation are
    separate acts: an operator pinning a stack to where it already runs expects
    nothing to happen, and a pin that silently triggered a migration would make
    a safety measure into an outage. Use /migrate to move it, then pin.
    """
    return _psd_svc.set_host_pin(db, stack_id, body, user)


class GpuPinIn(BaseModel):
    """Admin-set GPU CARD pin for a stack (GPUCAP P1) — `{service: [card_id, ...]}`.

    `null` / `{}` / `clear: true` removes the pin. All rules (which services may
    be pinned, which cards exist on the host-pinned worker, disabled / wrong-kind
    / too-small cards, the tenant fence) are validated in
    `services.persistent_stacks_domain_service.set_gpu_pin` — the model here only
    bounds the shape: ≤16 services, ≤16 cards each, ids ≤128 chars, `extra="forbid"`
    so a smuggled key 422s instead of being ignored."""
    model_config = ConfigDict(extra="forbid")
    pin: Optional[Dict[constr(min_length=1, max_length=128),
                       conlist(constr(min_length=1, max_length=128),
                               max_length=16)]] = None
    clear: bool = False
    reason: Optional[constr(max_length=500)] = None


@router.put("/{stack_id}/gpu-pin", dependencies=[Depends(_require_platform_admin)])
def set_gpu_pin(stack_id: int, body: GpuPinIn,
                db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    """GPUCAP P1 — pin a stack's GPU services to specific cards of its host.

    Admin-only (same gate as host-pin, NOT paid-gated, since host-pin is not)
    plus `_get_owned` tenant scoping plus the baseline stack-edit gate. The pin
    rides the compose as the top-level extension `x-mod.gpu_pin` in a NEW
    revision (revisioned + revertible, like host-ports), audited
    STACK_GPU_PIN_SET / STACK_GPU_PIN_CLEARED.

    Does NOT redeploy and does NOT move the stack — same posture as host-pin:
    placement and relocation are separate acts, and the next Deploy applies the
    pin. The pin is an ALLOWED SET, not an assignment: the claim-time planner
    intersects the worker's filtered card pool with these ids and keeps applying
    its exclusive / dedicated-tenant / scrub rules unchanged, so a pin can never
    bypass `admin_disabled`, tenant isolation or scrub. A longer list than the
    service's `count` is a pool the planner still picks `count` of.
    """
    return _psd_svc.set_gpu_pin(db, stack_id, body, user)

@router.post("/assemble", response_model=AssembleResponse)
def assemble_stack(body: AssembleRequest,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """service-stack-composer — resolve each service's source into a compose
    fragment, render the stack's `compose_spec.services`, run the existing
    delegated-stack-authoring compose validator, and return the validated compose
    (HTTP 422 with the validator's reason otherwise). Group F deploys the returned
    compose via the existing create/deploy internals."""
    if not body.services:
        raise HTTPException(status_code=422, detail="a stack needs at least one service")

    services: dict = {}
    notes: dict = {}
    for spec in body.services:
        if spec.name in services:
            raise HTTPException(status_code=422,
                                detail=f"duplicate service name {spec.name!r}")
        fragment, note = _resolve_service_fragment(db, spec)
        services[spec.name] = _compose_service_from_spec(spec, fragment)
        if note:
            notes[spec.name] = note
        # Surface exposure so the builder can show the operator what will be
        # reachable (the concrete magic URL is minted at deploy, Group F).
        if spec.expose:
            notes[spec.name] = ((notes.get(spec.name, "") + "; ").lstrip("; ")
                                + "exposed publicly (mesh-routed, no host port)")

    compose_spec: dict = {"services": services}
    declared = _collect_named_volumes(services)
    if declared:
        compose_spec["volumes"] = {v: {} for v in declared}

    _validate_compose(
        compose_spec,
        is_admin=bool(getattr(user, "is_platform_admin", False)),
        secret_prefix=f"tenants/{user.tenant_id}/stacks/{body.name}/",
        tenant_id=user.tenant_id, db=db)

    return AssembleResponse(compose_spec=compose_spec, notes=notes)

@router.post("/estimate", response_model=EstimateResponse)
def estimate_stack(body: EstimateRequest,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    """Price the builder's current reservation vector. Wraps the EXISTING
    `stack_billing.price_reservation_units` + current reservation rates, so the
    estimate equals what the metering tick charges for the same vector. 0 while
    metering is dark (rates 0) — the honest pre-launch number."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.stack_billing", "Stack reservation estimate")
    try:
        from services.stack_billing import price_reservation_units
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    # CARVE-U4 (CE carve, CRASH class) — utils/pricing.py is manifest-denied
    # (open-core/manifest.yaml :1381, the CE overlay ships a no-op stub), so the
    # bare import above raised ModuleNotFoundError -> 500 on the CE image. The
    # endpoint is wholly Paid (GT.D5/DS-2): gate the pricing module with the
    # SAME 402 posture as the stack_billing gate above, so CE sees the upgrade
    # path, never a 500. Paid behaviour unchanged (require_paid returns the module).
    _require_paid("utils.pricing", "Stack reservation estimate")
    try:
        from utils.pricing import (
            get_reservation_cpu_rate, get_reservation_mem_rate,
            get_reservation_gpu_rate, reservation_metering_enabled)
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first

    cpu_rate = get_reservation_cpu_rate(db)
    mem_rate = get_reservation_mem_rate(db)
    gpu_rate = get_reservation_gpu_rate(db)

    per_service: list = []
    total = 0.0
    for s in body.services:
        units = {"cpu_cores": s.cpu, "mem_gb": s.mem_gb, "gpu": list(s.gpu or [])}
        rate = price_reservation_units(units, cpu_rate, mem_rate, gpu_rate)
        total += rate
        per_service.append(EstimateServiceCost(name=s.name,
                                               credits_per_hour=round(rate, 6)))

    total = round(total, 6)
    return EstimateResponse(
        per_service=per_service,
        total_credits_per_hour=total,
        projected_credits_per_month=round(total * 730, 6),
        rates={"cpu": cpu_rate, "mem": mem_rate, "gpu": gpu_rate},
        metering_enabled=reservation_metering_enabled(db),
    )

# ============================================================================
# stack-wizard-compose-import — bring your own docker-compose
# ----------------------------------------------------------------------------
# A managed stack IS a compose project (compose_spec). These two endpoints are
# the import front door: PREVIEW normalizes a user-supplied compose into the same
# compose_spec shape the wizard emits and returns the MOD x-mod metadata to
# confirm; FINALIZE re-normalizes, applies the confirmations as x-mod fields,
# runs the SAME validator create/from-wizard/assemble use (_validate_compose),
# and — on pass — creates a normal PersistentStack via the SAME internals as
# create_stack, so it deploys / wakes / probes with no downstream special-casing.
# Authed like its peers (create / assemble / from-wizard use get_current_user).
# ============================================================================
@router.post("/import-compose", response_model=ComposeImportPreviewResponse)
def import_compose_preview(body: ComposeImportRequest,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """Parse + normalize a user docker-compose and return the candidate
    compose_spec plus the x-mod metadata to confirm (D1/D2). A bad compose is a
    422 with the reason — never a 500."""
    from services import stack_compose_import as sci
    try:
        spec = sci.normalize_compose(body.compose)
        annotated, needs = sci.infer_xmod(spec)
    except sci.ComposeImportError as e:
        raise HTTPException(status_code=422, detail=e.as_text())
    # 13.5c — "Move to Secrets": list the secret-looking inline env values so
    # the client can offer to store them as tenant secrets, and mask their
    # VALUES in the returned spec (the client never needs the plaintext back —
    # it re-sends the RAW compose to finalize, where the selected values are
    # written to the secret store and replaced with ${secret:NAME} before the
    # spec is persisted).
    from services import stack_secret_extract as sse
    inline = sse.detect_inline_secrets(annotated)
    if inline:
        sse.redact_preview_spec(annotated)
    return ComposeImportPreviewResponse(
        name=body.name,
        compose_spec=annotated,
        needs_confirmation=needs,
        storage_class=sci.derive_stack_fields(annotated, {})["storage_class"],
        inline_secrets=inline,
        warnings=_stack_port_preview_warnings(db, annotated),
    )


def _stack_port_preview_warnings(db: Session, spec: dict) -> list:
    """D76: warn at Preview when a requested FIXED host port is already in use
    on every licensed worker (names the holder project when the worker reports
    it). Fail-open: any error yields no warning."""
    try:
        from services import stack_port_placement as _spp
        from crud import persistent_stacks_domain as _psd_crud
        if not _spp.requested_host_ports(spec):
            return []
        workers = list(_psd_crud.list_licensed_workers(db))
        if not workers or _spp.host_port_shortfall_reason(db, spec, workers) is None:
            return []
        return [_spp.host_port_conflict_message(
            workers[0], _spp.host_port_conflict(db, workers[0], spec) or "")]
    except Exception:  # noqa: BLE001 — advisory only
        return []

# ============================================================================
# 27.4 slice 2 — LLM serving-profile render preview
# ----------------------------------------------------------------------------
# Expand a top-level `x-mod.serving` profile into concrete per-GPU engine
# services + the SGLang gateway `lb` and return the rendered compose YAML —
# a read-only dry run with NO stack creation. Authed like the admin-only
# placement surface (router-wide STACK_AUTHOR gate + per-endpoint
# `_require_platform_admin`); a bad serving profile is a 422, never a 500.
# ============================================================================
@router.post("/render-preview", response_model=RenderPreviewResponse,
             dependencies=[Depends(_require_platform_admin)])
def render_serving_preview(body: RenderPreviewRequest,
                           db: Session = Depends(get_db),
                           user: models.User = Depends(get_current_user)):
    """Render an `x-mod.serving` profile into the concrete compose services
    it will generate (platform-admin only). Returns the rendered YAML plus the
    service names; `rendered` is False when the compose had no profile.
    Never persists anything."""
    # GT.D5 / DS-2 — wholly Paid in CE: 402.
    _require_paid("services.llm_serving_render", "LLM serving gateway render")
    try:
        from services import llm_serving_render as _lsr
    except ImportError:
        raise  # CE — unreachable: `_require_paid` above 402s first
    try:
        out = _lsr.render_preview(body.compose_yaml)
    except _lsr.ServingRenderError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return RenderPreviewResponse(**out)

@router.post("/import-compose/finalize", response_model=schemas.PersistentStack)
def import_compose_finalize(body: ComposeImportFinalizeRequest,
                            db: Session = Depends(get_db),
                            user: models.User = Depends(get_current_user)):
    """Normalize → apply confirmations → validate through the EXISTING wizard
    validator → create a normal PersistentStack (D3/D4). Reuses _validate_compose
    (same gate as create / from-wizard / assemble) and the create_stack internals
    (parse_* + PersistentStack + audit). A validation failure surfaces the
    validator's own 422; a parse/normalize/confirmation problem is a 422 too."""
    from services import stack_compose_import as sci
    is_admin = bool(getattr(user, "is_platform_admin", False))
    secret_prefix = f"tenants/{user.tenant_id}/stacks/{body.name}/"

    def _validate(spec: dict) -> None:
        _validate_compose(spec, is_admin=is_admin, secret_prefix=secret_prefix,
                          tenant_id=user.tenant_id, db=db)

    def _create(spec: dict, stack_fields: dict):
        # 13.5c — "Move to Secrets": BEFORE the spec is persisted, create/update
        # the selected tenant secrets and rewrite their inline values to
        # ${secret:NAME}, so plaintext never lands in compose_spec. No-op when
        # the caller selected nothing (today's behaviour). Runs before
        # apply_variables so a moved secret's ${secret:NAME} ref is what gets
        # stored (a variable could not substitute INTO an existing inline value
        # anyway).
        from services import stack_secret_extract as sse
        spec = sse.move_inline_secrets(
            db, user.tenant_id, spec, body.move_secrets, user=user)
        _ce = _ce_ha_class_default(spec, (body.confirmations or {}).get("ha_class"))
        if _ce:
            ha_class, ha_class_reason = _ce
        else:
            try:
                from services.stack_ha_class import InvalidHaClass, resolve_create_ha_class
            except ImportError:
                raise  # CE — unreachable: `_ce_ha_class_default` above took the CE path
            # GR.FEN.3a — the RAW confirmations.ha_class is the explicit override:
            # a valid one wins; a non-empty invalid one is a 422 (not a silent
            # coercion to the previous "external when unknown" default); absent,
            # the class is inferred from the (normalized) spec's mounts, honoring
            # a top-level x-mod.ha_class declared in the pasted compose. Reading
            # `stack_fields["ha_class"]` here instead would ALWAYS see its
            # derive_stack_fields fallback ("external") and inference could never
            # run — so the confirmation is read pre-fallback.
            try:
                ha_class, ha_class_reason = resolve_create_ha_class(
                    spec, (body.confirmations or {}).get("ha_class"))
            except InvalidHaClass as e:
                raise HTTPException(status_code=422, detail=e.args[0] if e.args else str(e))
        # GX.S10a — optional variables on the import body: secret values go to
        # Vault and the stored spec keeps refs only; 422 naming any placeholder
        # left unresolved. No-op when omitted. Runs before the mesh-collision
        # check so a name substituted from a variable is what gets checked.
        from services import stack_variables as _sv
        spec = _sv.apply_variables(
            db, spec, body.variables, tenant_id=user.tenant_id,
            stack_name=body.name, user=user)
        _reject_mesh_name_collision(db, user.tenant_id, body.name, spec)
        # `_reject_mesh_name_collision` is a documented no-op under the
        # ID-based mesh-naming convention, and `persistent_stacks.name` has no
        # DB unique constraint — so without this check a duplicate/retried
        # import (same tenant + name) would silently create a second row. A
        # retried submit is exactly the failure mode a UI "Save" button invites
        # (double-click, timeout-then-retry), so refuse it explicitly rather
        # than doubling the stack.
        existing = (db.query(models.PersistentStack)
                   .filter(models.PersistentStack.tenant_id == user.tenant_id,
                           models.PersistentStack.name == body.name,
                           # A soft-deleted stack's name is free to reuse — it
                           # is gone from the directory, so it must not block
                           # a retried/renamed import either.
                           models.PersistentStack.status != "deleted")
                   .first())
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"a stack named {body.name!r} already exists (id {existing.id})")
        # GA.ASW.6 — a known-database image with no backup posture declared
        # requires an explicit acknowledge_no_backup before the row is
        # created. Checked on the NORMALIZED+confirmed `spec` (post
        # apply_confirmations), same as _validate_compose above it.
        _require_backup_ack(spec, body.acknowledge_no_backup)
        _backup_ack_services = _backup_ack_list(spec)
        stack = models.PersistentStack(
            tenant_id=user.tenant_id,
            created_by_user_id=user.id,
            name=body.name,
            compose_spec=spec,
            accelerator_requirements=_parse_accelerator_requirements(spec) or None,
            egress_policy=_parse_egress_policy(spec) or None,
            secret_refs=_parse_secret_refs(spec) or None,
            tls_config=_parse_tls_config(spec) or None,
            storage_class=stack_fields["storage_class"],
            home_zone=_resolve_home_zone(body.home_zone),
            quota_bytes=body.quota_bytes,
            status="created",
            ha_class=ha_class,
            desired_state="running",
        )
        db.add(stack)
        db.commit()
        db.refresh(stack)
        _audit_extra = {"source": "compose-import"}
        _audit_extra["ha_class"] = ha_class
        if ha_class_reason != "explicitly set at create":
            _audit_extra["ha_class_inferred"] = True
            _audit_extra["ha_class_reason"] = ha_class_reason
        if _backup_ack_services:
            _audit_extra["backup_ack"] = True
            _audit_extra["backup_ack_services"] = _backup_ack_services
        _audit_stack(db, "STACK_CREATED", stack, user, _audit_extra)
        _record_egress_declarations(db, stack, None, user.id)
        db.commit()
        logger.info("persistent stack %s created from imported compose", stack.id)
        # stack-training-and-prewarm §2.1 — characterize the imported stack using
        # the readiness probe + smoke request declared in its x-mod
        # (best-effort so it never fails the import).
        # GT.D5 / DS-2 — CE: predictive characterization is dropped; skip.
        _sc = _optional_paid("services.stack_calibration")
        if _sc is not None:
            _sc.maybe_enqueue_characterization(db, stack)
        out = _out(stack)
        out.ha_class_reason = ha_class_reason
        return out

    try:
        created = sci.finalize_import(
            name=body.name,
            yaml_text=body.compose,
            compose_spec=body.compose_spec,
            confirmations=body.confirmations,
            validate_fn=_validate,
            create_fn=_create,
        )
    except sci.ComposeImportError as e:
        raise HTTPException(status_code=422, detail=e.as_text())
    # `_create` returns the redacted DTO (with ha_class_reason attached), so
    # serialize nothing here — `_out` would double-redact and would need the
    # ORM row.
    return created

@stack_router.get("", response_model=List[schemas.PersistentStack])
def st_list_stacks(db: Session = Depends(get_db),
                   user: models.User = Depends(_require_stack_author)):
    """List the caller tenant's persistent stacks."""
    # Gate-2a split fix: list_stacks moved to the workers_fleet sibling and is
    # re-exported on the package. Resolve it through the package at call time
    # (not a submodule-local binding) so it stays consistent with the rest of
    # the facade and monkeypatch-overridable in tests. Function-local import
    # avoids any import-time cycle.
    import routers.persistent_stacks as _pkg
    return _pkg.list_stacks(db=db, user=user)

@stack_router.post("", response_model=schemas.PersistentStack)
def st_create_stack(body: schemas.PersistentStackCreate,
                    db: Session = Depends(get_db),
                    user: models.User = Depends(_require_stack_author)):
    """Create a persistent stack from a compose spec (non-wizard)."""
    # Gate-2a split fix: create_stack moved to the workers_fleet sibling and is
    # re-exported on the package; resolve through the package at call time.
    import routers.persistent_stacks as _pkg
    return _pkg.create_stack(body=body, db=db, user=user)

@stack_router.get("/{stack_id}", response_model=schemas.PersistentStack)
def st_get_stack(stack_id: int,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(_require_stack_author)):
    """Get one of the caller's stacks (404 for a foreign stack)."""
    return get_stack(stack_id=stack_id, db=db, user=user)

__all__ = [
    'node_stack_bindings',
    'managed_ai_status',
    'stack_wake_metrics',
    'provision_managed_ai',
    'get_rebalancer_config',
    'get_reconciler_config',
    'create_from_template',
    'create_from_wizard',
    'resolve_stack_endpoint',
    'list_accelerator_kinds',
    'stack_observations',
    'record_egress_observations',
    'egress_observations',
    'placement_overview',
    'get_stack',
    'get_stack_definition',
    'put_stack_definition',
    'list_stack_revisions',
    'diff_stack_revision',
    'revert_stack_definition',
    'write_platform_secret',
    'write_stack_secret',
    'upload_stack_cert',
    'set_stack_exposure',
    'tls_authorize',
    'mesh_reachability',
    'get_tenant_tls',
    'set_tenant_tls',
    'ingress_byo_certs',
    'deploy_stack',
    'stack_logs',
    'teardown_stack',
    'backup_stack',
    'restore_stack',
    'rehearse_restore',
    'set_backup_schedule',
    'stack_backups',
    'backfill_stack',
    'migrate_stack',
    'stack_drift',
    'stop_stack',
    'reconcile_preview',
    'llm_idle_preview',
    'get_llm_idle_settings',
    'update_llm_idle_settings',
    'update_stack_idle_override',
    'pin_stack_warm',
    'wake_stack',
    'set_host_pin',
    'assemble_stack',
    'estimate_stack',
    'import_compose_preview',
    'import_compose_finalize',
    'st_list_stacks',
    'st_create_stack',
    'st_get_stack',
]
