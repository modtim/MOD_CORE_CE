"""GT.D8.2 (launch-completion-megaplan) — tenant Kubernetes cluster registry.

The single source of truth for "which clusters may a tenant's k8s
backend dispatch to". The entitlement layer (utils/entitlements)
grants the right to USE a k8s backend at all (Cap.K8S_BACKEND, CE);
this registry counts the tenant's ACTIVE `k8s_cluster_configs` rows
against the MULTI-CLUSTER cap (Cap.K8S_MULTI_CLUSTER, Business) ONLY on
hosted SaaS:

  * Self-host / CE (owner 11-09, no quantity caps in the open core): any
    number of active clusters; `multi_cluster_unrestricted()` is True and
    neither the CRUD slot check nor the dispatch gate limits the count.
    An env-only deployment — MOD_EXECUTOR_BACKEND/MOD_STACK_BACKEND=k8s
    selected by env with NO registry rows (KUBECONFIG / in-cluster
    ServiceAccount, utils/k8s_backend_config) — still counts as ONE
    implicit cluster (reported by `active_cluster_count`).
  * SaaS (unchanged): a tenant WITHOUT Cap.K8S_MULTI_CLUSTER gets exactly
    one active cluster; Business+ gets 2+.

SOFT-DELETE ONLY (ISO 27001): clusters are tombstoned (deleted_at),
never physically deleted; create/delete are audited via
models.AuditEvent. The dispatch path (services/k8s_backend_gate)
stays fail-safe: with no explicit cluster name it picks the FIRST active
cluster.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import models
from utils import entitlements
from utils.entitlements import Cap
from utils.k8s_backend_config import is_executor_k8s, is_stack_k8s
from utils.paid_import import optional_paid
from crud.secret_cud import _vault_service_token

logger = logging.getLogger(__name__)

# HTTP 402 detail for the multi-cluster paid gate (same shape as the
# other Pro-feature 402s in the tree).
PRO_FEATURE_DETAIL = (
    "This edition runs a single Kubernetes cluster."
)


# ---------------------------------------------------------------------------
# audit (mirrors services/burst_accounts._audit — never breaks the op)
# ---------------------------------------------------------------------------

def _audit(db: Session, *, tenant_id: int, user_id: Optional[int], event: str,
           **details: Any) -> None:
    try:
        db.add(models.AuditEvent(
            tenant_id=tenant_id, user_id=user_id,
            event_name=event, details=details,
        ))
    except Exception:
        logger.warning("k8s cluster registry: audit emit failed for %s", event,
                       exc_info=True)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def active_clusters(db: Session, tenant_id: int) -> List[models.K8sClusterConfig]:
    """Live (non-tombstoned) ACTIVE clusters for the tenant, ordered by id
    (stable 'first cluster' pick)."""
    if tenant_id is None:
        return []
    return (
        db.query(models.K8sClusterConfig)
        .filter(
            models.K8sClusterConfig.tenant_id == tenant_id,
            models.K8sClusterConfig.deleted_at.is_(None),
            models.K8sClusterConfig.active.is_(True),
        )
        .order_by(models.K8sClusterConfig.id.asc())
        .all()
    )


def _env_only_k8s_selected() -> bool:
    """True when the deployment's ENV selects the k8s backend — i.e. there
    is at least one implicit (row-less) cluster: KUBECONFIG or an
    in-cluster ServiceAccount standing in for it (utils/k8s_backend_config
    resolution). Executor or stack selection both count."""
    return is_executor_k8s() or is_stack_k8s()


def active_cluster_count(db: Session, tenant_id: int) -> int:
    """Clusters the tenant is using: live active rows, OR 1 implicit
    cluster when the env selects k8s and the registry has no rows for
    the tenant (env-only on-prem deployment). Never 0 when the env is
    on — that is the on-prem floor the CE grant must preserve."""
    rows = active_clusters(db, tenant_id)
    if rows:
        return len(rows)
    if _env_only_k8s_selected():
        return 1
    return 0


def pick_cluster(db: Session, tenant_id: int,
                 requested_name: Optional[str] = None) -> Optional[models.K8sClusterConfig]:
    """Resolve the cluster a workload dispatches to.

    * requested_name set  -> that live active cluster, or None.
    * requested_name None -> the FIRST live active cluster, or None
      (None = env-only: the backend uses the env KUBECONFIG / in-cluster
      ServiceAccount from utils/k8s_backend_config).
    Never raises: dispatch callers must fail safe, not crash."""
    try:
        if tenant_id is None:
            return None
        q = (
            db.query(models.K8sClusterConfig)
            .filter(
                models.K8sClusterConfig.tenant_id == tenant_id,
                models.K8sClusterConfig.deleted_at.is_(None),
                models.K8sClusterConfig.active.is_(True),
            )
        )
        if requested_name is not None:
            q = q.filter(models.K8sClusterConfig.name == requested_name)
        else:
            q = q.order_by(models.K8sClusterConfig.id.asc())
        return q.first()
    except Exception:
        logger.exception("k8s_cluster_registry: pick_cluster failed; using env default")
        return None


# ---------------------------------------------------------------------------
# paid-cap slot gate (the CRUD router calls this BEFORE creating)
# ---------------------------------------------------------------------------

def multi_cluster_unrestricted(es: Optional[entitlements.EntitlementSet]) -> bool:
    """True when the tenant may have more than one active cluster: always on
    self-host / CE (no quantity caps in the open core, owner 11-09); on SaaS
    only with Cap.K8S_MULTI_CLUSTER (hosted behaviour unchanged)."""
    if not entitlements.is_saas():
        return True
    return es is not None and es.has(Cap.K8S_MULTI_CLUSTER)


def require_cluster_slot(db: Session, tenant_id: int,
                         es: Optional[entitlements.EntitlementSet] = None) -> None:
    """402 (SaaS only) when the tenant is already at the single-cluster floor
    and lacks Cap.K8S_MULTI_CLUSTER. Self-host / CE: never raises.

    The slot check counts CURRENT usage (rows + env-only implicit
    cluster); the new row would make usage+1 > 1 for a single-cluster
    tier. When a registry row already exists, the env-only implicit
    cluster is the same cluster the row describes, so usage = rows.
    Pass the resolved EntitlementSet (`es`) when the caller already has
    one — avoids a second entitlement resolution; otherwise resolved
    here. Resolution failure => fail safe (no slot granted) only when
    the count is already >= 1; count == 0 (brand new) may proceed on
    the first cluster, matching the fail-safe CE floor."""
    if not entitlements.is_saas():
        return  # self-host / CE: no cluster-count limit (owner 11-09)
    count = active_cluster_count(db, tenant_id)
    if count < 1:
        return  # brand-new tenant: the first cluster is allowed
    if es is None:
        try:
            es = entitlements.get_entitlements(db=db,
                                               tenant=_tenant_row(db, tenant_id))
        except Exception:
            logger.exception("k8s_cluster_registry: entitlement resolution failed")
            es = None
    if multi_cluster_unrestricted(es):
        return
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=PRO_FEATURE_DETAIL,
    )


def _tenant_row(db: Session, tenant_id: Optional[int]):
    if db is None or tenant_id is None:
        return None
    try:
        return db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# audited mutations
# ---------------------------------------------------------------------------

def create_cluster(db: Session, *, tenant_id: int, name: str,
                   kubeconfig_vault_ref: Optional[str] = None,
                   in_cluster: bool = False, active: bool = True,
                   user: Any = None) -> models.K8sClusterConfig:
    """Create a live cluster row. Calls require_cluster_slot first (402
    path for non-Business tenants past their slot). Audit:
    k8s_cluster_created. Caller commits."""
    es = None
    try:
        es = entitlements.get_entitlements(db=db, tenant=_tenant_row(db, tenant_id))
    except Exception:
        logger.exception("k8s_cluster_registry: create: entitlement resolution failed")
    require_cluster_slot(db, tenant_id, es=es)

    exists = (
        db.query(models.K8sClusterConfig)
        .filter(
            models.K8sClusterConfig.tenant_id == tenant_id,
            models.K8sClusterConfig.name == name,
            models.K8sClusterConfig.deleted_at.is_(None),
        )
        .first()
    )
    if exists is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Kubernetes cluster named {name!r} already exists for this tenant.",
        )

    row = models.K8sClusterConfig(
        tenant_id=tenant_id,
        name=name,
        kubeconfig_vault_ref=kubeconfig_vault_ref,
        in_cluster=bool(in_cluster),
        active=bool(active),
    )
    db.add(row)
    _audit(db, tenant_id=tenant_id, user_id=getattr(user, "id", None),
           event="k8s_cluster_created",
           name=name, in_cluster=bool(in_cluster), active=bool(active),
           has_vault_ref=bool(kubeconfig_vault_ref))
    return row


def soft_delete_cluster(db: Session, *, tenant_id: int, cluster_id: int,
                        user: Any = None) -> models.K8sClusterConfig:
    """Tombstone a cluster (ISO 27001 — never physically deleted). Frees
    its slot immediately: deleted_at IS NULL leaves the live-name index
    and every active_* query. Audit: k8s_cluster_soft_deleted."""
    row = (
        db.query(models.K8sClusterConfig)
        .filter(
            models.K8sClusterConfig.id == cluster_id,
            models.K8sClusterConfig.tenant_id == tenant_id,
            models.K8sClusterConfig.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes cluster not found for this tenant.",
        )
    row.deleted_at = datetime.now(timezone.utc)
    row.updated_at = datetime.now(timezone.utc)
    _audit(db, tenant_id=tenant_id, user_id=getattr(user, "id", None),
           event="k8s_cluster_soft_deleted",
           cluster_id=cluster_id, name=row.name)
    return row


def get_cluster(db: Session, tenant_id: Optional[int],
                cluster_id: int) -> Optional[models.K8sClusterConfig]:
    """The tenant's live (non-tombstoned) cluster row by id, or None
    (tenant-scoped: other tenants' clusters read as missing)."""
    if tenant_id is None:
        return None
    return (
        db.query(models.K8sClusterConfig)
        .filter(
            models.K8sClusterConfig.id == cluster_id,
            models.K8sClusterConfig.tenant_id == tenant_id,
            models.K8sClusterConfig.deleted_at.is_(None),
        )
        .first()
    )


# ---------------------------------------------------------------------------
# 18.3 (GI.3) KAI queue admin mapping — tenant/project -> existing queue
# ---------------------------------------------------------------------------

KAI_QUEUE_MAP_TENANT_KEY = "tenant:{tenant_id}"
KAI_QUEUE_MAP_PROJECT_KEY = "tenant:{tenant_id}:project:{slug}"

_KAI_QUEUE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _kai_project_slug(project: str) -> str:
    """Must stay byte-identical to the paid module's slug
    (services.k8s_kai_queues._slugify) so mapping keys match."""
    out = re.sub(r"[^a-z0-9-]+", "-", str(project).lower())
    out = re.sub(r"-{2,}", "-", out).strip("-")
    return out


def get_kai_queue_map(db: Session, tenant_id: int,
                      cluster_id: int) -> dict:
    """The tenant's live cluster's admin KAI queue mapping ({} when unset
    or the cluster is not the tenant's). Read-only."""
    row = get_cluster(db, tenant_id, cluster_id)
    if row is None:
        return {}
    raw = getattr(row, "kai_queue_map", None)
    return dict(raw) if isinstance(raw, dict) else {}


def set_kai_queue_mapping(db: Session, *, tenant_id: int, cluster_id: int,
                          project: Optional[str], queue_name: Optional[str],
                          user: Any = None) -> dict:
    """Add or remove an explicit tenant/project -> existing-queue mapping.

    ``queue_name=None`` removes the mapping (restores the deterministic
    MOD queue). The queue name is k8s-name-validated; it is NOT created
    or mutated — the next probe/queue-ensure validates its existence
    read-only (``services.k8s_kai_queues``). Audit:
    k8s_kai_queue_mapping_set / k8s_kai_queue_mapping_removed."""
    row = get_cluster(db, tenant_id, cluster_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Kubernetes cluster not found for this tenant.",
        )
    mapping = dict(row.kai_queue_map) if isinstance(row.kai_queue_map, dict) else {}
    key = (KAI_QUEUE_MAP_PROJECT_KEY.format(tenant_id=tenant_id,
                                            slug=_kai_project_slug(project))
           if project else
           KAI_QUEUE_MAP_TENANT_KEY.format(tenant_id=tenant_id))
    event = "k8s_kai_queue_mapping_removed"
    if queue_name is None:
        if mapping.pop(key, None) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No KAI queue mapping to remove for this "
                       "tenant/project.",
            )
    else:
        if not _KAI_QUEUE_NAME_RE.match(str(queue_name)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid KAI queue name (k8s name: 1-63 chars, "
                       "lowercase alphanumeric and '-', must start and "
                       "end with an alphanumeric).",
            )
        mapping[key] = str(queue_name)
        event = "k8s_kai_queue_mapping_set"
    row.kai_queue_map = mapping or None
    row.updated_at = datetime.now(timezone.utc)
    db.flush()
    _audit(db, tenant_id=tenant_id, user_id=getattr(user, "id", None),
           event=event,
           cluster_id=cluster_id, key=key,
           queue_name=None if queue_name is None else str(queue_name))
    return dict(row.kai_queue_map or {})


# ---------------------------------------------------------------------------
# 18.2 (GI.2) capability probe — probe + store per registered cluster
# ---------------------------------------------------------------------------

def refresh_cluster_caps(db: Session, cluster_id: int,
                         api_factory: Any = None) -> Optional[dict]:
    """Probe the cluster and store the caps on its registry row.

    ``api_factory`` — optional callable taking the cluster row and
    returning an injectable client object for
    ``k8s_cluster_caps.probe_cluster_caps`` (CoreV1 + CustomObjects; see
    ``k8s_cluster_caps.build_probe_client``). Defaults to building a real
    client via ``load_k8s_client`` (in-cluster SA, else the cluster's
    kubeconfig vault ref resolved as a local path).

    NEVER raises: any failure (no client, unreachable cluster, malformed
    response) is recorded as ``cluster_caps.probe_error`` with
    conservative values and returned, so a create/connect hook can log
    and move on. The row is saved with the probe result either way;
    ``caps_probed_at`` is set only when the probe itself succeeded
    (no probe_error). Caller commits.

    CE note: the probe module (services.k8s_cluster_caps) hard-imports the
    PAID k8s_gpu_sharing MIG table, so on a CE build it is absent and the
    probe degrades to conservative caps (probe_error: module unavailable)
    — never a crash, never an unguarded paid import (Gate GT.D4b).
    """
    caps_probe = _caps_module()
    if caps_probe is None:
        logger.info("refresh_cluster_caps: probe module unavailable on this "
                    "edition — storing conservative caps (cluster %s)",
                    cluster_id)

    row = (
        db.query(models.K8sClusterConfig)
        .filter(
            models.K8sClusterConfig.id == cluster_id,
            models.K8sClusterConfig.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        logger.warning("refresh_cluster_caps: cluster %s not found", cluster_id)
        return None

    if caps_probe is None:
        caps = _caps_error(caps_probe, "capability probe module not available on this edition")
    else:
        try:
            client = (api_factory(row) if api_factory is not None
                      else _default_probe_client(caps_probe, row))
            caps = caps_probe.probe_cluster_caps(client)
        except Exception as exc:  # noqa: BLE001 — fail-safe: never break the caller
            caps = _caps_error(caps_probe, f"probe setup failed: {exc}")
            logger.warning("refresh_cluster_caps: cluster %s probe failed: %s",
                           cluster_id, exc)

    row.cluster_caps = caps
    if "probe_error" not in caps:
        row.caps_probed_at = datetime.now(timezone.utc)
    db.flush()
    _audit(db, tenant_id=row.tenant_id, user_id=None,
           event="k8s_cluster_caps_probed",
           cluster_id=cluster_id, name=row.name,
           probe_error=caps.get("probe_error"),
           kai=bool(caps.get("kai")), vendor=caps.get("vendor"),
           mig_strategy=caps.get("mig_strategy"))
    # 18.3 (GI.3): ensure the tenant's KAI queue exists (paid tier only;
    # CE degrades via optional_paid). Only on a successful probe with KAI
    # installed; best-effort — a queue failure never fails the probe.
    if "probe_error" not in caps and caps.get("kai"):
        try:
            queues = _queues_module()
            if queues is not None:
                qfactory = api_factory
                if qfactory is None:
                    qfactory = lambda r: client  # noqa: B023 — reuse the built probe client
                queues.refresh_cluster_queues(row, caps, db, api_factory=qfactory)
        except Exception:  # noqa: BLE001 — queue work must never break the probe
            logger.exception("refresh_cluster_caps: KAI queue refresh failed for %s",
                             cluster_id)
    return caps


def _caps_module():
    """The paid capability-probe module, or None on a CE build where it is
    not shipped (it hard-imports the paid k8s_gpu_sharing MIG table)."""
    from utils.paid_import import optional_paid
    return optional_paid("services.k8s_cluster_caps")


def _queues_module():
    """The paid KAI queue module (18.3 GI.3), or None on a CE build where
    it is not shipped. Never hard-imported (Gate GT.D4b)."""
    from utils.paid_import import optional_paid
    return optional_paid("services.k8s_kai_queues")


# --- vault ref gate (GT.D8.2 K1) ------------------------------------------
# read_kv_v2 interpolates the ref RAW into a Vault URL (/v1/secret/data/{ref}),
# so the gate is an ALLOWLIST, never a denylist: anything outside these two
# alphabets is refused (no normalising, no escaping, no percent-decoding).
# Segment alphabet: lowercase alnum first char, then lowercase alnum plus . _ -
# (the URL-shape characters below are in the class only so the URL-shape guard
# stays an independently testable rule; both checks must hold).
_VAULT_REF_SEGMENT_RE = re.compile(r"\A[a-z0-9][a-z0-9._%\?#\\-]*\Z")


def _vault_ref_allowed(ref: str, tenant_id: int) -> bool:
    """True only for a plain tenant-scoped Vault KV path.

    Case-sensitive, segment-by-segment allowlist.  The first two segments must
    be exactly ``tenants/<tenant_id>`` (the AppRole policy path), so a
    cross-tenant, mixed-case or encoded variant is refused.  Empty segments are
    refused by the guards below (the segment pattern is applied to non-empty
    segments only), so each guard is an independently checkable rule.
    """
    if "%" in ref or "\\" in ref or "?" in ref or "#" in ref:
        return False                      # URL-shape characters
    if not ref or ref.endswith("/") or "//" in ref or ref.startswith("/"):
        return False                      # empty ref / trailing or empty segment
    segments = ref.split("/")
    if len(segments) < 3:
        return False                      # need tenants/<tenant_id>/<path...>
    if segments[0] != "tenants" or segments[1] != str(tenant_id):
        return False                      # tenant prefix (case-sensitive)
    for segment in segments:
        if not segment:
            continue                      # refused by the guards above
        if segment[0] == ".":
            return False                  # dot / dot-dot segment
        if not _VAULT_REF_SEGMENT_RE.match(segment):
            return False                  # outside the path alphabet
    return True


def _default_probe_client(caps_probe: Any,
                          row: models.K8sClusterConfig) -> Any:
    """Build a real probe client: in-cluster SA (in_cluster clusters), else
    resolve ``kubeconfig_vault_ref`` through the project's existing Vault KV
    v2 helper and load the kubeconfig in-memory (no disk I/O, no logging)."""
    from utils.k8s_backend_config import load_k8s_client
    import yaml
    from utils.vault_client import VaultError, read_kv_v2

    if row.in_cluster:
        load_k8s_client()
        return caps_probe.build_probe_client(
            load_k8s_client(kubeconfig_path=None)
        )

    # --- vault ref resolution (GT.D8.2 K1) ---
    ref = getattr(row, "kubeconfig_vault_ref", None)
    if not ref or not isinstance(ref, str):
        return _caps_error(caps_probe, "missing vault ref")

    # Tenant isolation: the core-api AppRole policy only grants reads under
    # secret/data/tenants/<tenant_id>/; the ref is allowlisted against that
    # path BEFORE any Vault call.  Ref content is never logged.
    if not _vault_ref_allowed(ref, row.tenant_id):
        return _caps_error(caps_probe, "vault ref outside allowed tenant path")

    # Resolve the ref through the existing Vault helper.
    try:
        token = _vault_service_token()
        raw = read_kv_v2(ref, token)
        # read_kv_v2 returns {"data": {"data": {...}}} or {"data": {}};
        # the inner "data" key holds the actual KV payload.
        kubeconfig_dict = raw.get("data") or raw
        if not kubeconfig_dict or not isinstance(kubeconfig_dict, dict):
            return _caps_error(caps_probe, "vault returned no kubeconfig dict")
        # Attempt a parse of the inner dict as a kubeconfig YAML mapping
        # so the kubernetes-py loader can accept it.
        kubeconfig_dict = yaml.safe_load(
            yaml.dump(kubeconfig_dict, default_flow_style=False)
        )
        if not isinstance(kubeconfig_dict, dict):
            return _caps_error(caps_probe, "vault secret is not a yaml mapping")
    except (ImportError, ModuleNotFoundError, NameError) as exc:
        # The service-token import is missing or the AppRole is unconfigured:
        # this must surface as a probe error, never as a success.  Only the
        # exception TYPE is logged - str(exc) could carry a token or a Vault
        # URL.
        logger.warning("refresh_cluster_caps: vault service token unavailable: %s",
                       type(exc).__name__)
        return _caps_error(caps_probe, "vault service token unavailable")
    except VaultError:
        logger.warning("refresh_cluster_caps: vault read failed for cluster ref")
        return _caps_error(caps_probe, "vault read failed")
    except Exception as exc:  # noqa: BLE001 — generic parse failure
        logger.warning("refresh_cluster_caps: kubeconfig resolution failed: %s",
                       type(exc).__name__)
        return _caps_error(caps_probe, "failed to resolve kubeconfig secret")

    # Load the kubeconfig in-memory — never touches the filesystem.
    try:
        from kubernetes import config as kube_config
        kube_config.load_kube_config_from_dict(kubeconfig_dict)
    except Exception:  # noqa: BLE001
        return _caps_error(caps_probe, "kubernetes config load failed")

    return caps_probe.build_probe_client(None)


def _caps_error(caps_probe: Any, detail: str) -> dict:
    if caps_probe is not None:
        return caps_probe._conservative(detail)
    # CE fallback: the conservative shape, inlined so the paid module is
    # never imported.
    from datetime import datetime as _dt
    return {
        "kai": False, "kai_queue": None, "mig_profiles": None,
        "time_slicing_resource": None, "vendor": None, "gpu_memory_gb": None,
        "kai_installed": False, "kai_queues": [],
        "fractional_supported": False, "mig_strategy": "none",
        "dcgm_exporter_present": False, "nodes": [], "allocatable": {},
        "probed_at": _dt.now(timezone.utc).isoformat(),
        "probe_error": detail,
    }


# ---------------------------------------------------------------------------
# 14.13 (D-GPU Layer 2) — k8s_gpu payload block for the Go Job builder
# ---------------------------------------------------------------------------

def build_k8s_gpu(db: Session, task: Any) -> Optional[dict]:
    """14.13: the ``k8s_gpu`` pod-spec fragment for a claimed task.

    Runs the 14.12 translator (``services.k8s_gpu_request.k8s_gpu_request``)
    against the target cluster's stored GI.2 probe
    (``K8sClusterConfig.cluster_caps``) and returns the fragment
    ``{"resources", "annotations", "labels", "nodeSelector",
    "scheduler_name"?}`` — or None (omitted from the payload) for non-k8s
    tasks, non-GPU tasks, env-only deployments, missing cluster rows, a CE
    build without the paid translator, or ANY error (fail-safe: the worker
    then falls back to today's whole-GPU placement).

    Demand is the same the claim-time card planner uses (``accel_demand`` /
    ``gpu_required``), the reservation is ``vram_reservation_mb`` (learned
    p95 → learned peak → declared min → default), and ``kai_queue`` comes
    from the probe row (18.3 stores the tenant's queue there). Pure output
    for the Go k8s backend's Job builder (W21 placement stays the
    absent-block fallback).
    """
    from utils.paid_import import optional_paid

    translator = optional_paid("services.k8s_gpu_request")
    if translator is None:
        return None
    try:
        tenant_id = getattr(task, "tenant_id", None)
        payload = getattr(task, "task_payload", None) or {}
        cluster = pick_cluster(db, tenant_id)
        if cluster is None:
            return None
        caps = getattr(cluster, "cluster_caps", None)
        if not isinstance(caps, dict):
            caps = None

        # GPU count — same precedence as the claim-time card planner
        # (worker_tasks._task_gpu_need: accel_demand first, else gpu_required).
        gpu_count, declared_min_mb = 0, None
        demand = payload.get("accel_demand")
        if isinstance(demand, dict):
            for k in ("cuda", "rocm", "intel", ""):
                vals = demand.get(k)
                if isinstance(vals, list) and vals:
                    gpu_count = len(vals)
                    try:
                        declared_min_mb = int(max(float(v) for v in vals) * 1024)
                    except (TypeError, ValueError):
                        declared_min_mb = None
                    break
        if gpu_count == 0 and payload.get("gpu_required"):
            gpu_count = 1
        if gpu_count == 0:
            return None

        from services import gpu_reservation
        reservation, _source = gpu_reservation.vram_reservation_mb(
            str(payload.get("node_kind") or ""), tenant_id,
            declared_min_mb, db)
        # gpu_assignment's vram_limit_mb is the SAME reservation the planner
        # computed at claim time (plan_gpu_assignment calls vram_reservation_mb)
        # — reuse it so the k8s request never drifts from the pinned cards.
        assignment = payload.get("gpu_assignment")
        if isinstance(assignment, dict):
            try:
                if int(assignment.get("vram_limit_mb") or 0) > 0:
                    reservation = int(assignment["vram_limit_mb"])
            except (TypeError, ValueError):
                pass

        frag = translator.k8s_gpu_request(
            reservation, gpu_count=gpu_count, cluster_caps=caps)
        if not frag:
            return None
        out = dict(frag)
        # 18.4 (GI.4) per-node calibration pin: a calibration task pinned to
        # one k8s node carries `k8s_calibration_node` on its payload (or its
        # pinned run's parameters). Merge {"kubernetes.io/hostname": <node>}
        # into the fragment's nodeSelector — the Go Job builder applies
        # K8sGPURequest.NodeSelector verbatim. This is a REQUIRED pin (the
        # measured duration must land on the named node), additive: other
        # nodeSelector keys (e.g. the 14.12 translator's AMD
        # compute-partition label) are preserved.
        _kcal = optional_paid("services.k8s_calibration")
        if _kcal is not None:
            try:
                pin = _kcal.k8s_pin_for_task(db, task)
                if pin:
                    from services import k8s_node_routing as _knr
                    sel = dict(out.get("nodeSelector") or {})
                    sel[_knr.HOSTNAME_LABEL_KEY] = str(pin)
                    out["nodeSelector"] = sel
            except Exception:  # noqa: BLE001 — the pin must never break the block
                logger.exception(
                    "build_k8s_gpu: k8s calibration pin merge failed for task %s; "
                    "fragment emitted unpinned", getattr(task, "id", None))
        # 18.6 (GI.6) per-node routing: soft nodeAffinity preferred terms
        # (kubernetes.io/hostname In [node]) for the nodes calibration
        # measured fastest for this node kind. SOFT only — the merge never
        # touches the translator's nodeSelector (e.g. the AMD
        # compute-partition label) and adds no required terms, so the task
        # still schedules when the preferred nodes are full/gone. None when
        # the platform setting is off or no k8s-node calibration exists yet
        # (18.4 / GI.4 writes it).
        from services import k8s_node_routing
        out = k8s_node_routing.merge_affinity(
            out, k8s_node_routing.routing_affinity(db,
                                                   payload.get("node_kind")))
        # schedulerName is a pod-SPEC field (not a limit/annotation/label/
        # nodeSelector), so the 14.12 translator does not emit it (module
        # docstring): the Go Job builder sets it from this key when KAI is
        # present. ``kai_queue`` (18.3's resolved tenant queue) is already in
        # the stored caps the translator labels with.
        if caps and caps.get("kai"):
            out["scheduler_name"] = translator.KAI_SCHEDULER_NAME
        return out
    except Exception:  # noqa: BLE001 — never block a claim over GPU sizing
        logger.exception("k8s_cluster_registry: build_k8s_gpu failed for "
                         "task %s; omitting the block",
                         getattr(task, "id", None))
        return None


__all__ = [
    "PRO_FEATURE_DETAIL",
    "build_k8s_gpu",
    "KAI_QUEUE_MAP_TENANT_KEY",
    "KAI_QUEUE_MAP_PROJECT_KEY",
    "active_clusters",
    "active_cluster_count",
    "pick_cluster",
    "require_cluster_slot",
    "create_cluster",
    "soft_delete_cluster",
    "get_cluster",
    "get_kai_queue_map",
    "set_kai_queue_mapping",
    "refresh_cluster_caps",
]
