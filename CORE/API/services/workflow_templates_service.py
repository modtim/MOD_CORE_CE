"""Service layer for the workflow templates router.

Data access for curated + tenant-private WorkflowTemplate rows. The router
shapes the returned ORM rows into its response models.

1900Y3b — also the CE boot seeder for the curated rows. `migrations/20260425_seed_workflow_templates.py`
writes the 4 curated rows this module lists, but CE boot runs NO migrations
(no `mod-migrate` service in `open-core/overlay/docker-compose.yml`; a fresh
CE DB is only `models.Base.metadata.create_all` + the ALTER-only
`_migrate_schema()`), so on a fresh CE install the WorkflowBuilder "Templates"
gallery was an empty list (200, not 500). `seed_workflow_templates()` is the
startup-side seed for that data, registered in
`utils/startup_lock.SEED_GROUP_SINGLE_FLIGHT_TASKS` and called from `main.py`'s
lifespan task list.

It is INSERT-ONLY and idempotent: a curated row already present (matched on the
migration's natural key, `name` + `tenant_id IS NULL`) is left completely
untouched — never updated, never deleted — so an admin's later edit survives
every boot. In CE, a template whose graph references a node the CE node-seed
keeplist never seeds is SKIPPED (logged once per boot) rather than seeded
broken, because the clone endpoint resolves `data.nodeDefinitionName` against
the cloning tenant's visible NodeDefinitions. The catalogue itself is never
duplicated here — it is imported from the migration, which ships wholesale
(`open-core/manifest.yaml` allows `CORE/API/migrations/*.py`).
"""
from __future__ import annotations

import copy
import importlib
from product_names import themed_logger
from typing import List, Optional, Set

from sqlalchemy.orm import Session

import models

logger = themed_logger(__name__, 'workflows')

# The migration owns the catalogue; this module only decides when to write it.
_MIGRATIONS_MODULE = "migrations.20260425_seed_workflow_templates"


def list_visible_templates(
    db: Session, *, tenant_id: Optional[int], category: Optional[str] = None
) -> List[models.WorkflowTemplate]:
    """Curated (tenant_id NULL) + *tenant_id*-private templates, curated first then by name.

    Optionally filtered to *category* when it is truthy.
    """
    q = db.query(models.WorkflowTemplate).filter(
        (models.WorkflowTemplate.tenant_id.is_(None))
        | (models.WorkflowTemplate.tenant_id == tenant_id)
    )
    if category:
        q = q.filter(models.WorkflowTemplate.category == category)
    return q.order_by(
        models.WorkflowTemplate.is_curated.desc(),
        models.WorkflowTemplate.name.asc(),
    ).all()


def get_visible_template(
    db: Session, *, template_id: int, tenant_id: Optional[int]
) -> Optional[models.WorkflowTemplate]:
    """The template *template_id* if curated or private to *tenant_id*, else None."""
    return db.query(models.WorkflowTemplate).filter(
        models.WorkflowTemplate.id == template_id,
        (models.WorkflowTemplate.tenant_id.is_(None))
        | (models.WorkflowTemplate.tenant_id == tenant_id),
    ).first()


# --- 1900Y3b: CE boot seed for the curated rows ------------------------------
def _seed_specs() -> List[dict]:
    """The curated template specs, read from the migration (never re-declared)."""
    try:
        return list(importlib.import_module(_MIGRATIONS_MODULE).SEED_TEMPLATES)
    except Exception as e:  # noqa: BLE001 — seed is best-effort, never fatal
        logger.error(
            "seed_workflow_templates: cannot load the curated template catalogue "
            "from %s: %s — seeding nothing this boot", _MIGRATIONS_MODULE, e,
        )
        return []


def _ce_allowed_node_names() -> Optional[Set[str]]:
    """Node NAMES CE is allowed to seed, or None when seeding is unrestricted.

    Delegates to the one authoritative CE gate — `services/node_scanner.py`'s
    edition probe plus its `node-seed-keeplist.yaml` loader (which already
    fails CLOSED (empty keep-list, nothing seeded, a warning is logged) when
    the file is missing/unreadable). Imported
    lazily so this module cannot participate in an import cycle.
    """
    from services.node_scanner import _is_ce_edition, _load_ce_keeplist_names

    if not _is_ce_edition():
        return None
    return _load_ce_keeplist_names()


def _referenced_node_names(spec: dict) -> Set[str]:
    """The `nodeDefinitionName` values a template's graph asks for."""
    names: Set[str] = set()
    graph = spec.get("graph_json") or {}
    for node in graph.get("nodes") or []:
        name = (node.get("data") or {}).get("nodeDefinitionName")
        if name:
            names.add(name)
    return names


def _is_available(spec: dict, allowed: Optional[Set[str]]) -> bool:
    """True when every node the template references exists in this edition."""
    if allowed is None:
        return True
    missing = _referenced_node_names(spec) - allowed
    if missing:
        logger.info(
            "seed_workflow_templates: '%s' references node(s) CE never seeds "
            "(%s) — skipped, not seeded broken",
            spec["name"], ", ".join(sorted(missing)),
        )
        return False
    return True


def seed_workflow_templates(db: Session) -> int:
    """Insert the missing curated WorkflowTemplate rows. Idempotent, insert-only.

    Returns the number of rows newly inserted (0 on every boot after the first).
    """
    allowed = _ce_allowed_node_names()
    inserted = 0
    for spec in _seed_specs():
        if not _is_available(spec, allowed):
            continue
        existing = db.query(models.WorkflowTemplate).filter(
            models.WorkflowTemplate.name == spec["name"],
            models.WorkflowTemplate.tenant_id.is_(None),
        ).first()
        if existing:
            continue
        db.add(models.WorkflowTemplate(
            tenant_id=None,
            name=spec["name"],
            description=spec["description"],
            category=spec["category"],
            plan_required=spec["plan_required"],
            preview_nodes=copy.deepcopy(spec["preview_nodes"]),
            graph_json=copy.deepcopy(spec["graph_json"]),
            is_curated=True,
        ))
        inserted += 1

    if inserted:
        db.commit()
        logger.info(
            "seed_workflow_templates: seeded %d curated workflow template(s) "
            "(CE node gate: %s)",
            inserted, "unrestricted" if allowed is None else "keeplist",
        )
    return inserted
