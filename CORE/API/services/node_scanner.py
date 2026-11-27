"""
Node Scanner.

Scans node-metadata `.py` files for METADATA BLOCK comments and
registers each as a NodeDefinition row in the database. The files
themselves are pure metadata — no Airflow / Celery imports, no
runtime logic. The metadata-file naming convention persists for historical
reasons but the contents are just declarative blocks consumed here.

Files live under `CORE/Metadata/system/` on the host, mounted into
the API container at `/metadata/system/`. Tenant-generated metadata
(from the wizard's commit path) lands at
`/metadata/tenant_generated/`.
"""

import ast
import importlib.util
import os
import re
import logging
import threading
import time
from typing import Optional, Dict, Any, List, Set

from sqlalchemy.orm import Session

from utils.registries import ce_mirror_ref, platform_image
# FX.162.p1 (owner Q157) — the ONE CE/paid sentinel (services/edition_sentinel;
# CUT-A2 decoupled it from the wizard_ml feature package).
from utils import paid_import

logger = logging.getLogger(__name__)

# 1900Y3c — ONE source of truth for the calibration-exclusion name list.
# migrations/20260514_node_definitions_excluded_from_calibration.py used to
# own its own copy, but that migration never runs in CE, so a fresh DB left
# node_definitions.excluded_from_calibration FALSE everywhere and system/admin
# one-shots entered the calibration matrix (wasted GPU passes, skewed
# baselines — readers services/calibration_coordinator.py:195/211,
# calibration_dispatcher.py:190, calibration_fixture_audit.py:159 all filter
# excluded_from_calibration.is_(False)). The scan below now stamps the flag
# itself; the migration imports this tuple so the two can never drift.
CE_EXCLUDED_FROM_CALIBRATION_NAMES = (
    # System / admin-only one-shots — no scheduler signal to extract.
    "LLM Generate Benchmark Strategy",
    "ML Retrain",
    # Wizard build-pipeline steps (env-var wiring calibration can't supply).
    "system-build-one-variant",
    "system-smoke-one-variant",
    "system-push-one-variant",
    "system-sample-test",
    "system-register-node",
    # Trivially fast file-handler input nodes — sub-second I/O.
    "Image Input",
    "Text Input",
    "Resource Input",
    "Audio Input",
    "Video Input",
    "Mesh Input",
    "Nerf Input",
    "Script Input",
    "Comic Image Input",
    "Texture Set",
    # I/O-only exports.
    "Unity Export",
    "Unreal Export",
    "Return Assets Export",
    # GPU + multi-GB model — never runs on CPU calibration hosts.
    "Text To Image",
)


def _apply_excluded_from_calibration(db: Session) -> int:
    """Seed + self-heal node_definitions.excluded_from_calibration (1900Y3c).

    UPDATE-only: flips FALSE -> TRUE for rows whose ``name`` is in
    CE_EXCLUDED_FROM_CALIBRATION_NAMES (system row + every tenant copy), and
    NEVER flips TRUE -> FALSE. Covers both halves of the brief in one pass:
    rows the scan just CREATED (they land at the server_default FALSE) and
    pre-existing FALSE rows on installs that never ran the 20260514
    migration. Best-effort: a DB without the column (pre-migration SaaS tree)
    logs and rolls back rather than failing the scan.
    """
    import models
    try:
        rows = (
            db.query(models.NodeDefinition)
            .filter(
                models.NodeDefinition.name.in_(
                    list(CE_EXCLUDED_FROM_CALIBRATION_NAMES)
                ),
                models.NodeDefinition.excluded_from_calibration.is_(False),
            )
            .all()
        )
        for nd in rows:
            nd.excluded_from_calibration = True
        db.commit()
        return len(rows)
    except Exception as e:
        logger.warning(
            "excluded_from_calibration seed/heal failed: %s", e
        )
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _module_available(module_name: str) -> bool:
    """CE build check (open-core/manifest.yaml `deny`) — same pattern as
    services/scheduler.py's `_module_available` (WS2.3): the CE carve strips
    whole service modules out of the tree, so a module's absence at runtime
    is the signal this is a CE install, not the full SaaS/stage tree.
    """
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _is_ce_edition() -> bool:
    """True iff running against the CE (open-source) carve.

    FX.162.p1 (owner Q157): the sentinel is now the ONE shared one —
    `utils.paid_import.PAID_SENTINEL_MODULE` (services/edition_sentinel, the
    marker open-core/manifest.yaml denies wholesale — CUT-A2), not the wizard_ml
    feature package. The previous sentinel
    (`services.node_authoring_service`) is now api_allow, so it no longer
    marked CE and every check using it mis-detected a CE image as paid.
    MOD_CE_EDITION lets tests force the branch without faking module absence
    (unset = infer from the module; only an explicit value overrides, and only
    under pytest: see utils.paid_import.edition_test_override).
    """
    override = paid_import.edition_test_override()
    if override is not None:
        return override
    return paid_import.is_ce_edition()


# WS2.1 (launch-completion-megaplan, decomp/ws2-ce.md): the CE (single-tenant,
# open-core) edition assigns every signup to ONE default tenant. The id is
# operator-tunable via DEFAULT_TENANT_ID (default 1 — the same row
# `database.seed_service_user` bootstraps as the platform tenant).
DEFAULT_TENANT_ID_ENV = "DEFAULT_TENANT_ID"


def default_tenant_id() -> int:
    """The tenant id every CE signup is assigned to. Read from
    DEFAULT_TENANT_ID (default 1)."""
    raw = os.getenv(DEFAULT_TENANT_ID_ENV, "").strip()
    try:
        return int(raw) if raw else 1
    except ValueError:
        logger.warning(
            "DEFAULT_TENANT_ID=%r is not an integer; falling back to 1", raw,
        )
        return 1


def ensure_default_tenant(db: Session) -> Optional[int]:
    """CE startup seed: make sure the default tenant row exists.

    Idempotent and soft — creates the row only if the id is absent (a
    `select_for_update` holds the id slot against concurrent startups; a
    lost race returns the winner's row). NEVER modifies or deletes an
    existing tenants row: if the id is taken, the existing tenant is the
    default tenant and it is returned as-is.

    No-op outside the CE edition (SaaS / licensed on-prem keep their
    per-customer tenants — `create_tenant_and_parent_user` never routes
    here). Returns the tenant id, or None when no row could be ensured.
    """
    if not _is_ce_edition():
        return None
    from models import Tenant

    tenant_id = default_tenant_id()
    existing = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if existing is not None:
        return existing.id
    tenant = Tenant(id=tenant_id, name=f"default-tenant-{tenant_id}")
    db.add(tenant)
    try:
        db.commit()
    except Exception:
        db.rollback()
        winner = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if winner is not None:
            return winner.id
        raise
    db.refresh(tenant)
    logger.info(
        "CE default tenant seeded: id=%s name=%r", tenant.id, tenant.name,
    )
    return tenant.id


# Where the keep-list ships:
#  - CE runtime image: open-core/build_public_repo.py's overlay copies
#    open-core/node-seed-keeplist.yaml to
#    CORE/API/services/node-seed-keeplist.yaml (co-located with this module)
#    because the CE docker build context is CORE/API only — a sibling
#    open-core/ directory never lands in the built image.
#  - Full source checkout (dev/stage, and tests run from this repo): fall
#    back to the canonical open-core/node-seed-keeplist.yaml at the repo
#    root, which IS present there (open-core/ is excluded from the CE
#    *carve*, not from the source tree stage/tests run against).
_KEEPLIST_ENV = "MOD_CE_NODE_SEED_KEEPLIST_PATH"
_KEEPLIST_COLOCATED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "node-seed-keeplist.yaml")
_KEEPLIST_REPO_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "open-core", "node-seed-keeplist.yaml",
)


def _keeplist_candidates() -> List[str]:
    override = os.getenv(_KEEPLIST_ENV)
    if override:
        return [override]
    return [_KEEPLIST_COLOCATED, _KEEPLIST_REPO_ROOT]


def _load_ce_keeplist_names() -> Set[str]:
    """Flatten open-core/node-seed-keeplist.yaml's `keep:` mapping into a set
    of `node_definitions.name` values that CE is allowed to seed.

    CE-REMOVE-S08S09-6 — FAILS CLOSED: a missing / unreadable / zero-name keep-list
    (or a MOD_CE_NODE_SEED_KEEPLIST_PATH override pointing at one) returns the EMPTY
    set, never None, so every caller registers NOTHING instead of the full catalogue.
    The old fail-open contract let a CE install seed NIM/AEC/Qube/Deadline/Kubernetes
    rows whenever the shipped keep-list failed to land — a removal leak. The empty set
    is the safe worst case: the boot still succeeds (no crash), it just seeds no
    keep-list-gated node kinds, and the warning names the paths checked so an
    integrator can see why. The paid path is unaffected: every caller reaches this
    loader only under `_is_ce_edition()` (or treats an empty set exactly as it treated
    None), and paid's own seeding never consults it.
    """
    for path in _keeplist_candidates():
        if not os.path.isfile(path):
            continue
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            keep = doc.get("keep") or {}
            names: Set[str] = set()
            for category_names in keep.values():
                for n in category_names or []:
                    names.add(n)
            if not names:
                logger.warning(
                    "CE node-seed keep-list at %s parsed to zero names; "
                    "seeding NOTHING (fail-closed) this pass",
                    path,
                )
                return set()
            return names
        except Exception as e:
            logger.warning(
                "CE node-seed keep-list at %s failed to load (%s); "
                "seeding NOTHING (fail-closed) this pass",
                path, e,
            )
            return set()
    logger.warning(
        "CE edition detected but no node-seed keep-list found (checked %s); "
        "seeding NOTHING (fail-closed) — no keep-list-gated node will register",
        _keeplist_candidates(),
    )
    return set()

# Opt-in H.264 add-on node (Debian-slim image video-converter-h264). The API
# cannot see which images the Docker host holds, so the gate is an explicit
# platform setting the user flips after `docker compose --profile h264 pull`.
# Paid never seeds it (its image is not published to the paid registry).
H264_ADDON_NODE_NAMES = frozenset({"Video Converter (H.264)"})


def _h264_addon_enabled(db) -> bool:
    if not _is_ce_edition():
        return False
    try:
        from services.platform_settings import get_setting
        return bool(get_setting("nodes.h264_addon_enabled", db=db))
    except Exception as e:  # a settings read must never break the scan
        logger.warning("H.264 add-on gate unreadable (%s); node stays hidden", e)
        return False


# Cross-process singleflight key for scan_and_register_nodes (see the function).
# The API runs `uvicorn --workers 8`; the WorkflowBuilder fires POST
# /node_definitions/refresh on EVERY page load, so a scan thread can start in
# EACH worker process at once. A per-process threading.Lock cannot serialize
# across processes, so up to 8 scans ran concurrently — each holding long
# node_definitions transactions (+ UPDATE scan_status) that collided with the
# schema DDL, producing the node_definitions lock convoy that exhausted the
# connection pool (platform-wide 502 storm). A Postgres advisory lock serializes
# across ALL workers: at most one scan runs anywhere; concurrent callers skip.
_SCAN_ADVISORY_KEY = 782341906


# ---------------------------------------------------------------------------
# GA.NMF.1b — image-digest backfill for scanned (system/tenant-generated) nodes
#
# node-version-pinning has nothing to pin to until node_definitions.image_digest
# is populated, and the scan paths (this module, system_tasks._scan_node_metadata,
# main._startup_scan_nodes) never set it — only the build pipeline's
# manifest-metadata callback does. So every scanned node stays at digest NULL.
#
# This resolves each scanned node's image digest from the platform registry
# (manifest HEAD -> Docker-Content-Digest, the same registry call the workers
# make when they pull) and writes it back, best-effort: a registry or
# credential failure degrades the node to tag-only resolution, it must never
# break the scan.
# ---------------------------------------------------------------------------

# Per-process throttle: the refresh endpoint fires on every WorkflowBuilder page
# load, and a full digest sweep is N registry round-trips — don't re-sweep on
# every page load. Per-process is enough: the sweep itself is idempotent and
# cheap once digests are populated (rows whose digest matches are skipped).
_DIGEST_SWEEP_MIN_INTERVAL_S = 15 * 60
_last_digest_sweep = 0.0

# B119 — the platform tenant's palette rows (seed_task_merge_node.py and its
# siblings) are tenant_id=_PLATFORM_TENANT_ID / user_id NULL, not tenant_id
# NULL, so the original tenant_id IS NULL sweep never reached them. Same
# convention as dependencies.PLATFORM_TENANT_ID / audit.PLATFORM_AUDIT_TENANT_ID.
_PLATFORM_TENANT_ID = 1
_sweep_lock = threading.Lock()


# ---------------------------------------------------------------------------
# WB14-PRODFIX (CE launch, wb-ce-batch14 :55 / :187) — the CE keep-list seed
# must be idempotent and self-healing:
#
# * RED :55 — a keep-listed row that ALREADY exists (the startup seed wrote it
#   before the CE re-registration, and models.py:2726's server_default stored
#   ce_included FALSE) never reached the CREATE-only stamp at :1165, so the
#   palette's CE badge rendered nothing. Stamp it here, on the UPDATE path,
#   every scan (idempotent: only FALSE/NULL rows are written, never TRUE->X).
# * RED :187 — the CE re-registration at :1098 inserted a fresh tenant row per
#   pass instead of reusing the scanner-registered row already in that scope,
#   so the palette showed 3x "Image Converter". Reuse (skip the insert) when a
#   scanner row for the same name AND node_kind exists in the SAME scope.
#
# Scanner origin, never name alone: a row qualifies only when it is tenant/system
# scoped (user_id NULL), shares name AND node_kind, and carries no user-author
# marker (source_origin generated|copied|forked|edited, or a wizard manifest_ref).
# Never crosses tenants: the reuse lookup is pinned to the scan's own tenant
# scope; the stamp additionally covers the system-scope twin (tenant_id NULL),
# which is the same seeded row this scan registers.
def _ce_scanner_origin_ok(row) -> bool:
    origin = getattr(row, "source_origin", None)
    if origin in ("generated", "copied", "forked", "edited"):
        return False
    return getattr(row, "manifest_ref", None) is None


def _ce_keep_list_rows(db, *, name, node_kind, tenant_id, include_system_scope):
    """Scanner-registered rows for a keep-listed node, pinned to one scope."""
    import models
    if not (name and node_kind):
        # Name alone is never the key.
        return []
    q = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.name == name,
        models.NodeDefinition.node_kind == node_kind,
        models.NodeDefinition.user_id.is_(None),
    )
    if include_system_scope:
        q = q.filter(
            (models.NodeDefinition.tenant_id == tenant_id)
            | models.NodeDefinition.tenant_id.is_(None)
        )
    else:
        q = q.filter(models.NodeDefinition.tenant_id == tenant_id)
    return [r for r in q.all() if _ce_scanner_origin_ok(r)]


def _ce_keep_list_has_column(db) -> bool:
    """True when node_definitions.ce_included is applied on this DB."""
    from sqlalchemy import inspect as sa_inspect
    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else db
        columns = {c["name"] for c in sa_inspect(bind).get_columns("node_definitions")}
    except Exception:  # noqa: BLE001
        return False
    return "ce_included" in columns


def _ce_keep_list_stamp_existing(db, *, name, node_kind, tenant_id) -> int:
    """RED :55 — idempotent self-heal: stamp ce_included TRUE on the scanner
    rows for this keep-listed node that are missing it. Returns rows stamped."""
    if not _ce_keep_list_has_column(db):
        return 0
    stamped = 0
    for row in _ce_keep_list_rows(
        db, name=name, node_kind=node_kind, tenant_id=tenant_id,
        include_system_scope=True,
    ):
        if getattr(row, "ce_included", None) is not True:
            row.ce_included = True
            stamped += 1
    if stamped:
        db.commit()
    return stamped


def _ce_keep_list_reuse_row(db, *, name, node_kind, tenant_id):
    """RED :187 — the existing scanner row for this keep-listed node in THIS
    tenant scope (lowest id), or None when the scan must create one."""
    rows = _ce_keep_list_rows(
        db, name=name, node_kind=node_kind, tenant_id=tenant_id,
        include_system_scope=False,
    )
    if not rows:
        return None
    return sorted(rows, key=lambda r: int(r.id))[0]


def _ce_keeplist_wide_self_heal(db, *, tenant_id) -> int:
    """WB14-PRODFIX (CE launch, part B) — keep-list-WIDE self-heal sweep.

    The per-name stamp at :1212 only fires for names the walk actually found as
    `.py` metadata under METADATA_ROOT. Rows the STARTUP SEEDERS wrote (the
    domain registry, seed_human_approval_node) pre-exist the scan, so they never
    reach the CREATE-only stamp at :1296 and land on models.py:2726's
    server_default FALSE — the palette's CE badge then renders nothing and the
    dedupe arm misses. This sweep heals EVERY keep-list name on every scan.

    Wider than `_ce_keep_list_rows` (which needs name AND node_kind, unknown for
    seeded rows): keyed on name alone, but still scanner-origin only (user_id
    NULL, no user-author marker) and pinned to this scan's tenant scope plus the
    system-scope twin — never crosses tenants. Idempotent (only non-TRUE rows
    are written, never TRUE->X) and a no-op when the column is not applied yet,
    so it needs no migration.
    """
    if not _is_ce_edition():
        return 0
    names = _load_ce_keeplist_names()
    if not names:
        return 0
    if not _ce_keep_list_has_column(db):
        return 0
    import models
    # CE is single-tenant (WS2.1): an unscoped scan still heals only the default
    # tenant's rows plus the system-scope twins, never another tenant's copies.
    scope = tenant_id if tenant_id is not None else default_tenant_id()
    q = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.name.in_(sorted(names)),
        models.NodeDefinition.user_id.is_(None),
    )
    if tenant_id is not None:
        q = q.filter(
            (models.NodeDefinition.tenant_id == tenant_id)
            | models.NodeDefinition.tenant_id.is_(None)
        )
    stamped = 0
    for row in q.all():
        if not _ce_scanner_origin_ok(row):
            continue
        if getattr(row, "ce_included", None) is not True:
            row.ce_included = True
            stamped += 1
    if stamped:
        db.commit()

    # FIX-CE-NODE-SCANNER-RESEED-MISSING — the sweep above only STAMPS rows that
    # already exist. A keep-list name can have NO live system row at all: the
    # rows come from the single-flight boot task (`main.py:519
    # ("seed_domain_nodes", _startup_seed_domain_nodes)`), which `main.py:628`
    # skips when `startup_single_flight` returns False, and whose key is
    # `mod:startup:<boot-token>:<name>` with the boot token = hostname + init
    # starttime (`utils/startup_lock.py:60 _read_boot_token`). A DB wipe WITHOUT
    # a container restart keeps the token, so the seed never re-runs: nothing to
    # stamp, no palette row, and wb-ce-batch13:139 / wb-ce-batch19:83 go red
    # (the `Image Resize` family, `open-core/node-seed-keeplist.yaml:40`).
    # Re-seed exactly the names with no live system row through the EXISTING
    # registry gate: `register_all_domain_nodes` filters modules by keeplist
    # overlap and every registrar is an insert-only idempotent upsert
    # (`image_ops.py:801` reconciles an existing row, else `db.add(...)`), so
    # this is ADDITIVE — admin-curated rows are never touched, no new predicate,
    # no new module, no migration, no main.py/startup_lock edit.
    # Fail-closed: an unreadable keep-list makes `_load_ce_keeplist_names()`
    # return the EMPTY set (CE-REMOVE-S08S09-6), which already returned 0 above,
    # so this leg can never insert on a blind set. Tombstoned names (`deleted_at`
    # set) are NOT re-seeded — a
    # soft-delete is an admin decision, not a lost seed.
    live = {
        r[0]
        for r in db.query(models.NodeDefinition.name).filter(
            models.NodeDefinition.tenant_id.is_(None),
            models.NodeDefinition.name.in_(sorted(names)),
        ).all()
    }
    missing = sorted(n for n in names if n not in live)
    if not missing:
        return stamped

    from node_definitions_domains._registry import register_all_domain_nodes
    # The registry already wraps EVERY registrar in its own SAVEPOINT
    # (`_registry.py:184 with db.begin_nested(): count = fn(db)`) and rolls back
    # per module, so one module's exception never poisons the session and never
    # aborts the scan; a nested savepoint HERE would additionally make the
    # registrars' own `db.commit()` illegal. Roll back only around the whole leg.
    try:
        results = register_all_domain_nodes(db, keeplist_names=set(missing))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "CE keep-list re-seed skipped (%d name(s) missing): %s",
            len(missing),
            exc.__class__.__name__,
        )
        db.rollback()
        return stamped
    created = sum(v for v in results.values() if isinstance(v, int))
    if created:
        db.commit()
        logger.info(
            "CE keep-list re-seed: %d row(s) created for keep-list names "
            "with no live system row (%d names missing)",
            created,
            len(missing),
        )
    return stamped + created


def _digest_sweep_allowed() -> bool:
    """True at most once per _DIGEST_SWEEP_MIN_INTERVAL_S (per process)."""
    global _last_digest_sweep
    now = time.monotonic()
    with _sweep_lock:
        if now - _last_digest_sweep < _DIGEST_SWEEP_MIN_INTERVAL_S:
            return False
        _last_digest_sweep = now
        return True


# B91 (startup-time) — the per-process throttle above is NOT enough at API boot.
# `uvicorn --workers 8` means EIGHT processes each pass it on their first
# request, and a full sweep is one blocking registry HEAD per system node
# (10s timeout each) — measured on 10-10 at ~150s per worker, run INSIDE
# main._startup_scan_nodes, i.e. on the lifespan path that gates /health. That
# alone was ~2.5 min of the ~5 min restart, and past the 120s window after which
# workers fence stacks. A Redis lock (same idiom + same REDIS_URL fallback as
# main.py's other startup single-flight hooks) makes the sweep single-flight
# ACROSS the pod: exactly one worker sweeps per interval, the rest skip. TTL-
# bounded (never released) so a crash mid-sweep can never wedge the key past the
# interval. Fail-open when Redis is unreachable — same posture as those hooks.
_DIGEST_SWEEP_LOCK_KEY = "mod:node_digest_backfill:lock"
_DIGEST_SWEEP_LOCK_EX_S = 15 * 60


def _digest_sweep_lock_acquired() -> bool:
    """True when THIS process won the cross-process sweep slot."""
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
        if not _r.set(
            _DIGEST_SWEEP_LOCK_KEY, "1", nx=True, ex=_DIGEST_SWEEP_LOCK_EX_S
        ):
            logger.debug(
                "digest backfill: another worker holds the sweep lock; skipping"
            )
            return False
        return True
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.debug("digest backfill: lock unavailable (%s); proceeding", e)
        return True


def _is_platform_registry_ref(ref: str) -> bool:
    """True when ``ref`` points at THIS deployment's platform image namespace
    (``<platform_registry>/<platform_project>/…``).

    B119 — the digest sweep now also covers the tenant-1 platform-palette nodes
    (seed_task_merge_node.py and its siblings seed tenant_id=1, user_id NULL).
    Those rows must be filtered by ref, not just by tenant: a tenant-1 node can
    also carry a private tenant-registry ref (``registry:5000/tenant-1/…``), and
    asking the platform registry for that ref's digest is a guaranteed 404 that
    would burn a sweep slot per row. System nodes (tenant_id NULL) always
    qualify — their bare refs get qualified by platform_image below."""
    from utils.registries import platform_project, platform_registry

    ref = (ref or "").strip()
    if not ref:
        return False
    if "/" not in ref:
        return True  # bare repo:tag — platform_image() qualifies it below
    first_segment = ref.split("/")[0]
    if "." not in first_segment and ":" not in first_segment:
        return True  # no registry host in front — platform_image() qualifies it
    prefix = f"{platform_registry()}/{platform_project()}".strip("/").lower()
    return ref.lower().startswith(prefix + "/")


def _node_image_refs(nd) -> List[str]:
    """Every image ref a node pulls: executable_path + all gpu_variants refs
    (bare names qualified the same way the dispatch path does, so they resolve
    against this deployment's own platform registry)."""
    refs: List[str] = []
    raw = (getattr(nd, "executable_path", None) or "").strip()
    if raw and raw.lower() not in ("none",):
        refs.append(ce_mirror_ref(platform_image(raw)))
    variants = getattr(nd, "gpu_variants", None)
    if isinstance(variants, dict):
        for backend, ref in variants.items():
            ref = (ref or "").strip()
            if ref and ref.lower() != "none":
                refs.append(ce_mirror_ref(platform_image(str(ref))))
    return refs


def resolve_image_digest(image_ref: str) -> Optional[str]:
    """Resolve an image ref to its content digest via a registry manifest HEAD
    (the `Docker-Content-Digest` response header), following the registry's
    WWW-Authenticate bearer challenge the way utils/image_arch.py already does
    for arch probes. Returns None on ANY failure (network, auth, 404) — the
    caller treats that as "not resolvable", never an error. Pull credentials
    come from the platform tenant's secret store (via image_arch); they are
    never logged."""
    try:
        import requests
        from utils.image_ref_preflight import parse_image_ref
        from utils import image_arch

        parsed = parse_image_ref(image_ref)
        if parsed is None:
            return None
        if parsed.host_is_default:
            # docker.io is never where MOD's own node images live.
            return None
        if "@" in (image_ref or ""):
            # Already digest-pinned — nothing to resolve.
            return None

        headers = {"Accept": image_arch._ACCEPT}
        if not image_arch._bearer_for(parsed, image_ref, headers):
            logger.warning(
                "digest backfill: no pull credentials for %s; skipping", image_ref,
            )
            return None

        host = parsed.registry_host
        base = host if "://" in host else f"https://{host}"
        url = f"{base}/v2/{parsed.namespace}/{parsed.repo}/manifests/{parsed.tag}"
        resp = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
        if resp.status_code != 200:
            logger.warning(
                "digest backfill: HEAD %s returned HTTP %s", image_ref, resp.status_code,
            )
            return None
        return resp.headers.get("Docker-Content-Digest")
    except Exception as e:  # noqa: BLE001 — best-effort by contract
        logger.debug("digest backfill failed for %s: %s", image_ref, e)
        return None


def _resolve_variant_digests(variants) -> Dict[str, str]:
    """Resolve every `gpu_variants` ref to its digest (the parallel of
    NodeVersion.gpu_variant_digests the worker consumes to pin a variant by
    digest instead of tag). Best-effort: a backend whose ref can't be resolved
    is simply OMITTED from the result (the worker then pulls that variant by
    tag, exactly as before). Bare refs are qualified the same way the dispatch
    path does, so they resolve against this deployment's own registry. Never
    raises — a variant failure must never block the primary."""
    out: Dict[str, str] = {}
    if not isinstance(variants, dict):
        return out
    for backend, ref in variants.items():
        ref = (ref or "").strip()
        if not ref or ref.lower() == "none":
            continue
        try:
            d = resolve_image_digest(ce_mirror_ref(platform_image(str(ref))))
        except Exception as e:  # noqa: BLE001 — one bad variant never stops the sweep
            logger.debug("digest backfill: variant %s resolve failed: %s", backend, e)
            d = None
        if d:
            out[str(backend)] = d
    return out


def _record_version_with_variants(
    db: Session, nd, variant_digests: Optional[Dict[str, str]]
) -> Optional["models.NodeVersion"]:
    """Append-only NodeVersion recorder that ALSO captures per-variant digests.

    Mirrors crud.record_node_version (idempotent on the PRIMARY digest, the
    node's identity under uq_node_version_digest) but stamps the resolved
    `gpu_variant_digests` onto the NEW row. It never mutates an existing row:
    because a node has at most one version per primary digest, a variant-only
    change with an unchanged primary cannot create a second row, so it resolves
    to the existing primary-keyed version (variant digests ride along the next
    primary move, which is what a real rebuild produces)."""
    from sqlalchemy import func as _func
    from sqlalchemy.exc import IntegrityError

    import models

    digest = (getattr(nd, "image_digest", None) or "").strip()
    if not digest:
        return None
    existing = (
        db.query(models.NodeVersion)
        .filter(
            models.NodeVersion.node_definition_id == nd.id,
            models.NodeVersion.image_digest == digest,
        )
        .first()
    )
    if existing:
        return existing
    next_version = (
        db.query(_func.max(models.NodeVersion.version))
        .filter(models.NodeVersion.node_definition_id == nd.id)
        .scalar()
        or 0
    ) + 1
    nv = models.NodeVersion(
        node_definition_id=nd.id,
        node_kind=nd.node_kind,
        version=next_version,
        image_digest=digest,
        gpu_variants=nd.gpu_variants,
        gpu_variant_digests=variant_digests or None,
        executable_path=nd.executable_path,
        manifest_ref=nd.manifest_ref,
    )
    # SAVEPOINT so a concurrent recorder racing on the unique digest rolls back
    # only this insert, then we return the row the winner created.
    try:
        with db.begin_nested():
            db.add(nv)
            db.flush()
    except IntegrityError:
        return (
            db.query(models.NodeVersion)
            .filter(
                models.NodeVersion.node_definition_id == nd.id,
                models.NodeVersion.image_digest == digest,
            )
            .first()
        )
    return nv


def backfill_node_image_digests(db: Session, node_ids=None, stats=None) -> int:
    """Resolve + persist the pushed-image digest for every node whose image
    lives in THIS deployment's platform registry: the system nodes (tenant_id
    IS NULL) plus the tenant-1 platform-palette rows (tenant_id ==
    _PLATFORM_TENANT_ID, filtered by ref via _is_platform_registry_ref), so
    node-version pinning has a handle to pin to.

    B119 — the tenant-1 half was the gap. Nodes seeded by a seed script
    (``seed_task_merge_node.py`` and its siblings) are tenant-shared rows in
    the PLATFORM tenant (tenant_id=1, user_id NULL), not tenant_id NULL, so
    the original sweep never saw them: their ``image_digest`` stayed NULL, no
    ``node_versions`` row existed, ``resolve_pin_for_new_workflow_node``
    returned None, and the dispatched payload carried only the ``:latest`` tag
    — which under ``worker.prefer_local_images`` means "whatever :latest the
    host cached", i.e. a rebuilt image never runs (run 2437 merge-1, 1640c
    diagnosis).

    A row is written when its stored digest is empty OR differs from the digest
    the registry reports now (system images are rebuilt out-of-band, moving
    `:latest`, so a stale pin must be refreshed). An unchanged digest is a
    no-op — no write, no new version row. This is throttled by the caller
    (`_maybe_run_digest_backfill`) so a full re-sweep is at most one per interval
    per process, not one per page load.

    On a write it records an immutable NodeVersion (append-only) and captures
    the resolved per-variant digests on that version so a pinned run can pin
    variants by digest too. `node_ids` narrows the sweep to specific rows
    (used by tests to stay hermetic against a shared DB pre-populated with
    system nodes); when omitted it sweeps every system node.

    Returns the number of rows whose digest was written/changed. Best-effort
    throughout: no exception escapes, so the scan can never break over a
    registry blip."""
    import models

    written = 0
    if stats is not None:
        # B119d — the sweep's outcome must be WITNESSABLE at the room's INFO
        # level even when it writes nothing: a sweep that ran and pinned 0 rows
        # left only the dispatch line before, indistinguishable from a sweep
        # that never ran. The caller logs these counts (never a ref/host/cred).
        stats["attempted"] = 0
        stats["resolvable"] = 0
    q = db.query(models.NodeDefinition).filter(
        models.NodeDefinition.tenant_id.is_(None)
        | (
            (models.NodeDefinition.tenant_id == _PLATFORM_TENANT_ID)
            & models.NodeDefinition.user_id.is_(None)
        )
    )
    if node_ids is not None:
        q = q.filter(models.NodeDefinition.id.in_(list(node_ids)))
    rows = q.all()
    for nd in rows:
        try:
            refs = _node_image_refs(nd)
            if not refs:
                continue
            if getattr(nd, "tenant_id", None) is not None and not _is_platform_registry_ref(
                ce_mirror_ref((getattr(nd, "executable_path", None) or "").strip())
            ):
                # A tenant row (the tenant-1 palette sweep) whose ref is NOT in
                # this deployment's platform namespace — e.g. a tenant-private
                # registry upload. The platform registry cannot resolve it;
                # those rows register their digest through the build path.
                continue
            # resolve_image_digest is pure (no writes on this session) — a
            # failure here needs no rollback, and must not disturb the loop.
            if stats is not None:
                stats["attempted"] += 1
            try:
                digest = resolve_image_digest(refs[0])
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "digest backfill: resolve failed for node %s: %s",
                    getattr(nd, "id", "?"), e,
                )
                continue
            if not digest:
                continue  # not resolvable (no creds / 404 / network) — skip
            if stats is not None:
                stats["resolvable"] += 1
            current = (nd.image_digest or "").strip()
            if current == digest:
                continue  # unchanged — no-op
            # Resolve per-variant digests best-effort, AFTER the primary is
            # already resolved, so a variant failure can never block the
            # primary. A variant that can't be resolved is omitted (pulled by
            # tag by the worker).
            variant_digests = _resolve_variant_digests(getattr(nd, "gpu_variants", None))
            # The persist step touches this session — roll back on failure so
            # one bad row can't poison the remaining rows.
            try:
                nd.image_digest = digest
                _record_version_with_variants(db, nd, variant_digests)
                db.commit()
                written += 1
                logger.info(
                    "digest backfill: node %s (%s) %s to %s (%d variant digest(s))",
                    nd.name, nd.node_kind,
                    "updated" if current else "pinned",
                    digest[:19], len(variant_digests),
                )
            except Exception as e:  # noqa: BLE001
                logger.debug(
                    "digest backfill: persist failed for node %s: %s",
                    getattr(nd, "id", "?"), e,
                )
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 — belt & braces: one bad row never stops the sweep
            logger.debug("digest backfill: row %s skipped: %s", getattr(nd, "id", "?"), e)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    if written:
        logger.info("digest backfill: updated %d node definition digest(s)", written)
    return written


def _maybe_run_digest_backfill(db_factory, force_digest_sweep: bool = False) -> None:
    """Fire the digest backfill on a fresh session, throttled. Never raises —
    the scan must survive a registry outage, so failures are logged at debug.

    B91 (startup-time) — the sweep is a blocking registry HEAD per system node
    (10s timeout each), so running it inline made the caller wait for it. On
    API boot the caller is main._startup_scan_nodes, INSIDE the lifespan that
    gates /health: measured on 10-10 at ~150s per uvicorn worker, i.e. ~2.5
    min of the ~5 min restart, and past the 120s window after which workers
    fence stacks. The sweep is pure background work (idempotent, best-effort,
    never needed to serve a request), so it is dispatched to a daemon thread
    — same posture as main._startup_seed_embedding_models — and the cross-
    process lock above keeps it single-flight across the 8 workers. The
    throttle + lock checks run INSIDE the thread so a caller that holds the
    lock still pays nothing here.

    B119d — `force_digest_sweep=True` (the explicit POST /nodes/refresh path
    ONLY) skips the per-process throttle and the cross-process lock: a user who
    clicked refresh asked for a sweep, and silently dropping it left the room
    witness of a 202 that never touched a digest (counts unmoved 150/92). The
    boot/onboarding/workflow callers keep the default (throttled) posture.
    Forced sweeps are still best-effort: never raises, per-row rollback kept."""
    if not force_digest_sweep and not _digest_sweep_allowed():
        return

    def _sweep():
        if not force_digest_sweep and not _digest_sweep_lock_acquired():
            return
        stats = {"attempted": 0, "resolvable": 0, "written": 0}
        try:
            sweep_db = db_factory()
            try:
                stats["written"] = backfill_node_image_digests(sweep_db, stats=stats)
            finally:
                sweep_db.close()
        except Exception as e:  # noqa: BLE001 — best-effort by contract
            logger.warning("digest backfill sweep skipped: %s", e)
            return
        # B119d — ONE completion line per sweep, at INFO, COUNTS ONLY (never a
        # ref, host or credential): the dispatch line alone could not tell a
        # sweep that ran and pinned 0 rows from one that never ran.
        logger.info(
            "digest backfill sweep completed: %d attempted, %d resolvable, "
            "%d digest(s) written",
            stats["attempted"], stats["resolvable"], stats["written"],
        )

    threading.Thread(target=_sweep, daemon=True, name="NodeDigestBackfill").start()
    logger.info("digest backfill dispatched to background thread (non-blocking)")


# Default metadata directories (relative to the metadata mount point).
# Override with METADATA_ROOT to point at a different mount path.
METADATA_ROOT = os.getenv("METADATA_ROOT", "/metadata")
# User-authored metadata dir (sibling of system/ and tenant_generated/). The CE
# node-seed keep-list never applies to it or to tenant_generated/.
USER_METADATA_DIRNAME = "custom"


# Multi-variant system nodes that need gpu_variants pre-seeded at first
# registration. Edited by hand when a new system node ships variants.
# Keyed by `node_kind`. Values are `{backend: image_tag}` dicts, matching
# the shape wizard-built rows land with.
#
# Only consulted on CREATE (see routers.node_definitions:
# create_or_update_node_definition). After the row exists, wizard
# /build-and-register-local owns the value. Rescans cannot clobber it.
SYSTEM_GPU_VARIANT_SEEDS: Dict[str, Dict[str, str]] = {
    "accelerator_benchmark": {
        "cuda":  "accelerator-benchmark-cuda:latest",
        "rocm":  "accelerator-benchmark-rocm:latest",
        "intel": "accelerator-benchmark-intel:latest",
        "npu":   "accelerator-benchmark-npu:latest",
        "cpu":   "accelerator-benchmark-generic:latest",
    },
    "llm_generate_strategy": {
        "cuda":  "mod-llm-runner-granite-8b-cuda:latest",
        "rocm":  "mod-llm-runner-granite-8b-rocm:latest",
        "intel": "mod-llm-runner-granite-8b-intel:latest",
        "cpu":   "mod-llm-runner-granite-8b-cpu:latest",
    },
    # Single-image GPU-coded nodes — the only build in Harbor is the
    # CUDA build (verified via the local docker cache + Harbor catalog
    # on 2026-05-19). Marking with one `cuda` key sends them to the
    # CUDA queue (queue_selector.select_queue_for_node: 1 variant →
    # that backend's queue); CPU-only or non-CUDA workers won't pick
    # them up. When ROCm / Intel / NPU images get built later, adding
    # those keys auto-promotes the node to the ACCELERATOR meta-queue
    # (any-GPU semantic) per the same selector's 2+ variant rule.
    "chatterbox_gradio_audio_process": {"cuda": "chatterbox-gpu:latest"},
    "ffmpeg_gpu_video_process":        {"cuda": "ffmpeg_gpu:latest"},
    "text_to_image":                   {"cuda": "stablediffusion:latest"},
    "supersplat_gaussiansplat_process": {"cuda": "super_splat:latest"},
    "thumbnail_3d_mesh_process":       {"cuda": "3d-thumbnail-gen:latest"},
    # COLMAP SIFT is CUDA-only; the single image runs CPU too and the node
    # picks GPU-vs-CPU at runtime via nvidia-smi, so one cuda key routes it to
    # the CUDA worker while still functioning if it ever lands on CPU.
    "capture_preflight":               {"cuda": "capture-preflight:latest"},
    # NVIDIA Maxine NIM (LOCAL / self-hosted) nodes. The nimbridge container
    # proxies gRPC to a self-hosted Maxine NIM that REQUIRES an NVIDIA CUDA GPU
    # on the same host. Without a variant key these registered with NULL
    # gpu_variants → select_queue_for_node returned "CPU" → the scheduler could
    # place them on a GPU-less / non-NVIDIA worker where the NIM can't run. One
    # `cuda` key routes them to the CUDA queue and variants_guard blocks any
    # worker that doesn't advertise the `cuda` hardware tag. (The *Cloud*
    # variants call NVIDIA's hosted NVCF endpoint and correctly stay CPU/NULL.)
    # Split out of the shared nimbridge into dedicated, per-service images
    # (split-vsr-studiovoice-out-of-nimbridge). eye-contact / relighting / bnr
    # stay on nimbridge; only VSR + Studio Voice moved to their own containers.
    "nim_vsr_local":                   {"cuda": platform_image("videosuperres:latest")},
    "nim_studiovoice_local":           {"cuda": platform_image("studiovoice:latest")},
    # Q-L-ASR: speech-to-text NIM — same CUDA-only rationale as the other nim_*_local
    # rows (gRPC proxy to a self-hosted Riva ASR NIM needing a local CUDA GPU).
    "nim_asr_local":                   {"cuda": platform_image("nimbridge:latest")},
    # SDXL character LoRA trainer — GPU training, CUDA-only (bitsandbytes 8-bit
    # Adam + SDXL fine-tune). One `cuda` key routes it to the CUDA queue and
    # variants_guard blocks any worker without the `cuda` tag; it must NOT be
    # labelled cpu (it OOMs / has no CPU path). Image built from
    # DockerContainers/sdxl-lora-trainer (FROM the sdxl-server base so the LoRA
    # trains against the same baked RealVisXL model the brand-sdxl stack serves).
    "sdxl_lora_train":                 {"cuda": platform_image("sdxl-lora-trainer:latest")},
    # SDXL-Turbo character LoRA trainer — same CUDA-only rationale as
    # sdxl_lora_train, but FROM the sdxl-turbo-server base so the LoRA trains
    # against the same baked stabilityai/sdxl-turbo the brand-sdxl-turbo stack
    # serves.
    "sdxl_turbo_lora_train":           {"cuda": platform_image("sdxl-turbo-lora-trainer:latest")},
    # RIFE frame interpolation ("Frame Boost") — CUDA-only (PyTorch RIFE on the
    # GPU). One `cuda` key routes it to the CUDA queue.
    "rife_frame_boost":                {"cuda": platform_image("rife-frame-boost:latest")},
    # Video HDRify (SDR->HDR inverse tone mapping) — hevc_nvenc encode, CUDA.
    "video_hdrify":                    {"cuda": platform_image("video-hdrify:latest")},
}


def seeded_gpu_variants(node_kind, current=None):
    """`gpu_variants` for a row of ``node_kind``: ``current`` when it is a
    non-empty dict (never overwritten), else a COPY of the seed from
    SYSTEM_GPU_VARIANT_SEEDS when the kind has one, else ``current`` unchanged.

    Used where a row is created without going through the scanner CREATE path
    (tenant palette copies) so a NULL source value cannot leave a CUDA-coded
    kind without variants, which routes its tasks to the CPU queue."""
    if isinstance(current, dict) and current:
        return current
    seed = SYSTEM_GPU_VARIANT_SEEDS.get(node_kind or "")
    return dict(seed) if seed else current



def parse_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """
    Parse the metadata block from a node metadata file.

    Reads lines between "# --- METADATA BLOCK ---" and "# --- END METADATA BLOCK ---",
    or until the first import statement, and extracts their literal values via
    `ast.parse` + `ast.literal_eval` — the block is NEVER executed (GSEC R-007:
    these files are also tenant-uploadable through the zip workflow import).

    Extracted from Node_Dag_Gen_Dag.py util_parse_metadata (lines 15-81).
    """
    if not file_path or not os.path.exists(file_path):
        return None

    metadata_lines = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            current_statement = ""
            bracket_count = 0

            for line in f:
                stripped = line.strip()

                # Stop conditions
                if stripped.startswith('import ') or stripped.startswith('from '):
                    break
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    break
                if '--- END METADATA BLOCK ---' in stripped:
                    break

                # Skip comments and empty lines
                if stripped.startswith('#') or not stripped:
                    continue

                # Multi-line statement handling
                if bracket_count > 0:
                    current_statement += " " + stripped
                    bracket_count += stripped.count('{') + stripped.count('[') + stripped.count('(')
                    bracket_count -= stripped.count('}') + stripped.count(']') + stripped.count(')')
                    if bracket_count <= 0:
                        metadata_lines.append(current_statement)
                        current_statement = ""
                        bracket_count = 0
                elif "=" in stripped:
                    current_statement = stripped
                    bracket_count = stripped.count('{') + stripped.count('[') + stripped.count('(')
                    bracket_count -= stripped.count('}') + stripped.count(']') + stripped.count(')')
                    if bracket_count <= 0:
                        metadata_lines.append(current_statement)
                        current_statement = ""
                        bracket_count = 0

    except Exception as e:
        logger.error("Could not read file %s: %s", file_path, e)
        return None

    if not metadata_lines:
        return None

    # GSEC R-007 (SEC-6-001): the metadata block was historically `exec()`ed
    # to read its Python values. That path also runs on zip-imported DAG files
    # (routers/workflows_domains/workflows.py `POST /import-zip` installs
    # tenant .py content into tenant_generated/, then this scans it) — i.e.
    # executing tenant-supplied code inside the API process = tenant RCE. The
    # block is pure declarative literal assignments, so parse it as an AST and
    # pull out ONLY literal values. Nothing is ever executed: a non-literal
    # value (name reference, call, ...) is skipped with a warning.
    try:
        tree = ast.parse("\n".join(metadata_lines))
    except SyntaxError as e:
        logger.error("Could not parse metadata from %s: %s", file_path, e)
        return None

    metadata = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            metadata[target.id] = ast.literal_eval(node.value)
        except Exception as e:  # noqa: BLE001 — never exec; log and skip
            logger.warning(
                "%s: %s is not a literal value; skipping (no exec): %s",
                file_path, target.id, e,
            )

    metadata['source_file'] = os.path.basename(file_path)
    return metadata


def is_literal_metadata_manifest(source: str) -> bool:
    """GSEC R-007 (SEC-6-001): True iff `source` is a pure node-metadata
    manifest — a module docstring and/or comments plus simple
    `name = <literal>` assignments ONLY.

    The zip workflow import (routers/workflows_domains/workflows.py
    `POST /import-zip`) installs tenant-supplied `dags/*.py` files that the
    node scanner later reads. Before this fix the scanner `exec()`ed the block,
    so a malicious zip could run arbitrary code in the API process. This
    validator is the gate: an import whose .py carries any non-literal or
    non-assignment statement (a call, a name reference, a function/class/
    import, ...) is rejected with 422 rather than ever parsed as executable.
    Nothing here executes the source — it is only `ast.parse`d.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    assignments = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            try:
                ast.literal_eval(node.value)
            except Exception:  # noqa: BLE001 — not a literal; reject
                return False
            assignments += 1
            continue
        return False
    return assignments > 0


def _schema_dict(metadata: dict, key: str, file_path: str) -> dict:
    """A schema field as a dict. A bare "NONE"/None (a metadata-file slip) would
    fail NodeDefinitionCreate validation and silently keep the node out of the
    palette, so it maps to {} with a warning naming the file."""
    value = metadata.get(key)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    logger.warning("%s: %s should be a dict, got %r; registering with {}", file_path, key, value)
    return {}


def _egress_domains(metadata: dict, key: str, file_path: str) -> Optional[list]:
    """FX.91 p2a — the manifest's `egress_domains` list, validated + normalised.

    Same degrade-not-drop posture as `_schema_dict`: a manifest slip (a
    scheme, a port, an IP, an uppercase entry, >50 entries, ...) would
    otherwise fail NodeDefinitionCreate and silently keep the whole node out
    of the palette. Here the bad declaration is logged naming the file and
    the node registers with None = default-deny — the SAFE posture (the
    worker puts it on the internal-only network), never a silently-granted
    one. A valid list is stored canonically (deduped + sorted).
    """
    value = metadata.get(key)
    if value is None:
        return None
    from services.node_egress_domains import describe_egress_domains
    verdict = describe_egress_domains(value)
    if not verdict["ok"]:
        err = verdict["error"]
        logger.warning(
            "%s: %s rejected (%s): %s; registering with None "
            "(default-deny, no egress granted)",
            file_path, key, err["code"], err["message"],
        )
        return None
    return verdict["domains"] or None


def scan_and_register_nodes(
    db_factory, tenant_id: Optional[int] = None, force_digest_sweep: bool = False
):
    """
    Scan metadata files and register/update node definitions.

    Replaces Node_Dag_Gen_Dag. Scans system/ and tenant_generated/ directories.

    Registration happens IN-PROCESS (a direct call to the node-definitions
    register handler on a local Session), NOT via an HTTP POST to
    ``http://api:80/v1/node-definitions/``. The old self-call was a self-DoS: a
    full rescan fired one blocking POST per node back into the API's own tiny
    connection pool, and under load those calls read-timed-out (the pool was
    already exhausted) — the scanner then held a worker while starving the very
    endpoint it was calling. In-process registration removes the round-trip
    entirely: no self-HTTP, no extra pool pressure, naturally serialized.

    Args:
        db_factory: Callable that returns a new SQLAlchemy Session
        tenant_id: If provided, only scan tenant-specific DAGs
    """
    # CROSS-PROCESS singleflight via a Postgres advisory lock (see _SCAN_ADVISORY_KEY
    # above): at most ONE scan runs across all uvicorn workers. A caller that finds
    # the lock held skips — a fresh full rescan is about to have the same effect,
    # and all callers are fire-and-forget passing tenant_id=None. This is what
    # prevents the 8-way concurrent-scan node_definitions lock convoy.
    from sqlalchemy import text as _sa_text
    lock_db = db_factory()
    try:
        got = lock_db.execute(
            _sa_text("SELECT pg_try_advisory_lock(:k)"), {"k": _SCAN_ADVISORY_KEY}
        ).scalar()
    except Exception as e:
        logger.warning("node scan: advisory-lock check failed (%s); skipping", e)
        try:
            lock_db.close()
        except Exception:
            pass
        return
    if not got:
        logger.info("Node scan already running in another worker — skipping duplicate refresh")
        try:
            lock_db.close()
        except Exception:
            pass
        return
    try:
        db = db_factory()
        try:
            _scan_and_register(db, tenant_id)
        except Exception as e:
            logger.error("Node scan failed: %s", e)
        finally:
            db.close()
        # 1900Y3c — seed + self-heal node_definitions.excluded_from_calibration
        # for the CE_EXCLUDED_FROM_CALIBRATION_NAMES rows (the 20260514
        # migration never runs in CE, so a fresh DB left them FALSE and the
        # CE-allowed calibration readers fed system/admin one-shots into the
        # matrix). UPDATE-only, never TRUE->FALSE; best-effort.
        heal_db = db_factory()
        try:
            healed = _apply_excluded_from_calibration(heal_db)
            if healed:
                logger.info(
                    "excluded_from_calibration seed/heal: %d row(s) flipped FALSE->TRUE",
                    healed,
                )
        finally:
            heal_db.close()
        # GA.NMF.1b — pin node-version-pinning's handle: resolve each scanned
        # node's image digest from the registry and persist it. Throttled +
        # best-effort (never raises); a registry outage leaves nodes tag-only.
        # B119d — the kwarg is additive: only the explicit refresh path forces
        # a sweep; boot/onboarding/workflow callers keep the throttled posture.
        _maybe_run_digest_backfill(db_factory, force_digest_sweep=force_digest_sweep)
    finally:
        try:
            lock_db.execute(_sa_text("SELECT pg_advisory_unlock(:k)"), {"k": _SCAN_ADVISORY_KEY})
        except Exception:
            pass
        try:
            lock_db.close()
        except Exception:
            pass


def _scan_and_register(db: Session, tenant_id: Optional[int] = None):
    """Internal: scan metadata files and register node definitions IN-PROCESS."""
    # In-process registration wiring. Imported lazily so this service module
    # stays importable from the routers that call it (no import cycle at load
    # time — the router imports node_scanner, node_scanner imports the router
    # only when a scan actually runs).
    import schemas
    from fastapi import Response
    from routers.node_definitions import create_or_update_node_definition
    from dependencies import BuildAuthContext, _resolve_platform_service_user

    search_dirs = [
        os.path.join(METADATA_ROOT, "system"),
        os.path.join(METADATA_ROOT, "tenant_generated"),
        # 2400LR-node1: user-authored nodes (hand-written metadata) live here;
        # the CE keep-list prunes only the platform's own seeded `system/` nodes.
        os.path.join(METADATA_ROOT, USER_METADATA_DIRNAME),
    ]

    # The scanner registers system + tenant-generated nodes with the platform
    # service identity (system privileges), exactly as the old X-API-Key self-call
    # authenticated. Resolve it once for the whole scan.
    service_user = _resolve_platform_service_user(db)
    if service_user is None:
        logger.error(
            "Node scan aborted: platform-service identity unavailable "
            "(PLATFORM_SERVICE_API_KEY unset or its api_key/user row is missing)."
        )
        return
    auth_ctx = BuildAuthContext(user=service_user, via="master_key")

    # WS2.5 (launch-completion-megaplan open-source-mod-core §2.2): CE seeds
    # ONLY the runnable-empty keep-list (open-core/node-seed-keeplist.yaml) —
    # domain/build-farm/wizard/mobile/vector `system-*` nodes are dropped.
    # None means "unrestricted" (full SaaS/stage). CE-REMOVE-S08S09-6: the
    # loader now FAILS CLOSED — a missing/unreadable/zero-name keep-list returns
    # the EMPTY set (not None), so the filter is active and nothing keep-list-gated
    # registers. This ONLY filters what gets
    # newly *seeded*; it never touches or soft-deletes existing rows (the
    # tenant_1 copies of system nodes are the WorkflowBuilder palette).
    ce_keeplist_names = _load_ce_keeplist_names() if _is_ce_edition() else None

    registered = 0
    errors = 0
    skipped_not_in_keeplist = 0

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            # `tenant_generated` is legitimately absent until a tenant generates nodes.
            log = logger.warning if os.path.basename(search_dir) == "system" else logger.info
            log("Metadata directory not found: %s", search_dir)
            continue

        for root, dirs, files in os.walk(search_dir):
            # Skip _HIDDEN, _EXAMPLES, INTERNAL, __pycache__
            dirs[:] = [d for d in dirs if not d.startswith('_') and d != '__pycache__' and d != 'INTERNAL']

            for filename in files:
                if not filename.endswith(".py"):
                    continue

                filepath = os.path.join(root, filename)
                metadata = parse_metadata(filepath)
                if not metadata or not metadata.get("node_kind"):
                    continue

                node_name = metadata.get("name", "Unnamed Node")
                is_ce_keeplisted = ce_keeplist_names is not None and node_name in ce_keeplist_names
                is_user_authored = not filepath.startswith(os.path.join(METADATA_ROOT, "system") + os.sep)
                if node_name in H264_ADDON_NODE_NAMES and not is_user_authored:
                    # Opt-in add-on node: seeded only on CE with the
                    # nodes.h264_addon_enabled setting on; never on paid.
                    if not _h264_addon_enabled(db):
                        skipped_not_in_keeplist += 1
                        continue
                    is_ce_keeplisted = True
                if ce_keeplist_names is not None and not is_ce_keeplisted and not is_user_authored:
                    skipped_not_in_keeplist += 1
                    continue

                # Determine tenant_id from path
                node_tenant_id = None
                if '/tenant_generated/' in filepath:
                    match = re.search(r'tenant_(\d+)_', os.path.basename(filepath))
                    if match:
                        node_tenant_id = int(match.group(1))

                # CE exception (WS2.7): register keep-listed system nodes as
                # tenant-scoped rows for the CE default tenant instead of
                # system-scoped (tenant_id NULL). The system-scope visibility
                # path (crud.get_node_definitions' system_clause) requires
                # BOTH is_in_marketplace=True AND a TenantNodeEntitlement row
                # — curated via a paid marketplace-toggle/curate endpoint
                # that doesn't exist in CE (services/node_marketplace_paid.py
                # is denied there) and an entitlement mechanism
                # (starter-package -> plan-grant) gated on the tenant having
                # a Subscription, which a CE install never creates. The
                # tenant-scoped path (tenant_clause) has neither gate — same
                # mechanism the live palette already relies on (memory:
                # "the tenant_1 system-node copies populate the WB palette").
                # CE is single-tenant (WS2.1 default_tenant_id()), so scoping
                # to the default tenant covers every CE user.
                if node_tenant_id is None and (is_ce_keeplisted or (is_user_authored and ce_keeplist_names is not None)):
                    node_tenant_id = default_tenant_id()

                # Skip if filtering by tenant and this isn't it
                if tenant_id is not None and node_tenant_id != tenant_id:
                    continue

                # WB14-PRODFIX (RED :187) — idempotent CE re-registration. The
                # block above re-scopes keep-listed system nodes to the default
                # tenant on EVERY pass; the handler below then upserts by NAME
                # only, so a name+node_kind twin already in that scope (a
                # previous scan, a re-scan after a seed) used to fall through to
                # CREATE and insert a duplicate tenant row — the palette then
                # rendered 3x "Image Converter". Reuse the scanner row instead:
                # skip the insert, and let the stamp helper below heal its
                # ce_included. Keyed on name AND node_kind (never name alone)
                # and pinned to this scan's tenant scope (never crosses
                # tenants); user-author rows are never matched, so a user's
                # same-name node still registers normally.
                if is_ce_keeplisted:
                    # RED :55 — self-heal the stamp on the rows this scan owns
                    # (this tenant scope + the system-scope twin), BEFORE the
                    # upsert below decides create-vs-update: the handler's
                    # update path strips ce_included (CREATE-only), so an
                    # already-seeded row would otherwise never get it.
                    _ce_healed = _ce_keep_list_stamp_existing(
                        db,
                        name=node_name,
                        node_kind=metadata.get("node_kind", ""),
                        tenant_id=node_tenant_id,
                    )
                    if _ce_healed:
                        logger.info(
                            "CE keep-list stamp self-heal: %d row(s) for %s",
                            _ce_healed, node_name,
                        )
                    if node_tenant_id is not None and _ce_keep_list_reuse_row(
                        db,
                        name=node_name,
                        node_kind=metadata.get("node_kind", ""),
                        tenant_id=node_tenant_id,
                    ) is not None:
                        logger.info(
                            "CE keep-list re-registration reused row for %s "
                            "(node_kind=%s); %d row(s) self-healed",
                            node_name, metadata.get("node_kind", ""), _ce_healed,
                        )
                        continue

                node_type = metadata.get("category") or metadata.get("type", "General")

                # process_type deliberately omitted — DAGs no longer
                # carry it. NodeDefinition.process_type stays nullable;
                # the scheduler derives queue_name from gpu_variants at
                # enqueue time via select_queue_for_node.
                #
                # gpu_variants comes from the inline SYSTEM_GPU_VARIANT_SEEDS
                # dict above — authoritative source for multi-variant
                # system nodes on fresh install. Metadata source files no
                # longer carry gpu_variants. The server-side handler
                # applies write-once semantics — gpu_variants is taken
                # only on CREATE; on UPDATE the field is stripped so a
                # re-scan can't clobber wizard-authored variants. See
                # routers/node_definitions.create_or_update_node_definition.
                seeded_variants = SYSTEM_GPU_VARIANT_SEEDS.get(metadata.get("node_kind", ""))
                payload = {
                    "name": node_name,
                    "description": metadata.get("description", ""),
                    "node_kind": metadata.get("node_kind", ""),
                    "type": node_type,
                    "executable_path": metadata.get("executable_path", ""),
                    "input_schema": _schema_dict(metadata, "input_schema", filepath),
                    "output_schema": _schema_dict(metadata, "output_schema", filepath),
                    "configuration_schema": _schema_dict(metadata, "configuration_schema", filepath),
                    "custom_metadata": {
                        **(metadata.get("metadata") or {}),
                        **({"accepted_secrets": metadata["accepted_secrets"]} if "accepted_secrets" in metadata else {}),
                    },
                    "cost_in_credits": metadata.get("cost_in_credits", 0),
                    "batch_mode": metadata.get("batch_mode", "batch"),
                    "tenant_id": node_tenant_id,
                    # node-role §1 — the authoritative classification. A dev
                    # marks a seed node user-facing with `node_role: user` in its
                    # metadata; absent → the create default applies (system-shared
                    # seeds default to 'system', hidden until promoted). Only used
                    # on CREATE — re-scans preserve the DB classification.
                    #
                    # CE exception (WS2.7): a keep-listed node (open-core/
                    # node-seed-keeplist.yaml) is, by definition, meant to be
                    # runnable by the CE user out of the box — it exists so a
                    # fresh Core install has a usable Input->Export workflow.
                    # Leaving it at the 'system' default would hide it from
                    # every tenant's WorkflowBuilder palette (see
                    # crud.get_node_definitions' system_only filter), and CE
                    # has no curate/marketplace-toggle endpoint to promote it
                    # later — that UI is entirely paid (services/
                    # node_marketplace_paid.py, denied in open-core/
                    # manifest.yaml, 404s in CE). So the scan itself, the one
                    # privileged place a CE install seeds these rows, marks
                    # keep-listed nodes user-facing on CREATE — unless the
                    # metadata file already says otherwise.
                    "node_role": metadata.get("node_role") or ("user" if (is_ce_keeplisted or (is_user_authored and ce_keeplist_names is not None)) else None),
                    # FX.73 p4c (owner Q56=A) — the CE-inclusion SOURCE OF TRUTH
                    # per node. Written here, the only place a CE install decides
                    # what it ships: TRUE for a keep-listed node, NULL otherwise
                    # (never FALSE for an unknown — the palette badge renders
                    # nothing rather than a guessed "CE"). CREATE-only, like
                    # gpu_variants: the re-scan path strips it so a later scan
                    # cannot clobber an operator's stamp.
                    "ce_included": True if is_ce_keeplisted else None,
                    "system_only": metadata.get("system_only", False),
                    "container_timeout": metadata.get("container_timeout", 1800),
                    "gpu_variants": seeded_variants,
                    "accepted_formats": metadata.get("accepted_formats"),
                    # FX.91 p2a (owner Q77=A) — the manifest's declared
                    # external hostnames (egress default-deny contract).
                    # Validated + normalised here, the one place a manifest
                    # is parsed; a bad declaration registers as None =
                    # default-deny rather than dropping the node.
                    "egress_domains": _egress_domains(
                        metadata, "egress_domains", filepath
                    ),
                }

                try:
                    node_create = schemas.NodeDefinitionCreate(**payload)
                    # Direct handler call — same code path the HTTP endpoint ran,
                    # minus the self-HTTP round-trip. The handler commits on the
                    # shared session; on the next iteration SQLAlchemy re-acquires
                    # a pooled connection lazily, so nothing is held between nodes.
                    create_or_update_node_definition(
                        node_def=node_create,
                        response=Response(),
                        db=db,
                        _auth=auth_ctx,
                    )
                    registered += 1
                except Exception as e:
                    # Isolate failures: the handler may have left the shared
                    # session mid-transaction, so roll back before the next node.
                    logger.warning("Failed to register node %s: %s", payload["name"], e)
                    errors += 1
                    try:
                        db.rollback()
                    except Exception:
                        pass

    logger.info(
        "Node scan complete: %d registered, %d errors, %d skipped (not in CE keep-list)",
        registered, errors, skipped_not_in_keeplist,
    )

    # WB14-PRODFIX (CE launch, part B) — keep-list-WIDE self-heal: the stamp
    # above only reached names the walk visited; startup-seeded rows (domain
    # registry, Human Approval) pre-exist the scan and never got the CREATE-only
    # stamp. Heal every keep-list name on every scan (idempotent, no-op when the
    # column is absent — no migration).
    try:
        _ce_healed_wide = _ce_keeplist_wide_self_heal(db, tenant_id=tenant_id)
        if _ce_healed_wide:
            logger.info(
                "CE keep-list wide stamp self-heal: %d row(s) across the keep-list",
                _ce_healed_wide,
            )
    except Exception as e:
        logger.warning("CE keep-list wide stamp self-heal skipped: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
