"""workflow-expressiveness-primitives 3.4 (slice 1) — by-interface resolver.

tasks.md 3.4 is the `/road/by-interface/{name}/run` endpoint, which
dispatches to the tenant's DEFAULT implementation of an interface, or to an
explicitly named implementation. This slice is the RESOLVER only: no router,
no endpoint, no side effects. It answers one question — "for this tenant and
this interface name, which workflow do we run, and how did we decide?"

The two columns it reads (`workflows.implements`, `workflows.extends_workflow_id`)
come from migrations/20261012k_workflow_extends_implements.py and are NOT yet
in models.py, so every read here is `sqlalchemy.text` against the table name —
never `models.Workflow.implements` (that attribute does not exist yet). The
DDL is the source of truth until the model lands.

Interface-name rules are REUSED, not copied: `services.workflow_interfaces._NAME_RE`
(`^[a-z0-9][a-z0-9-]{0,63}$`) is the single definition of a legal slug.

The tenant default lives in the platform-settings registry
(`expr.interface_default_implementations`, a JSON object {interface_name:
workflow_id}, default {}, tenant-overridable) — registration/validator shape
copied from services/adversary_rate_limit.py.

Every read runs inside `db.begin_nested()` (a SAVEPOINT): a missing column —
which is exactly the state of the live DB until the lead applies migration k —
must not poison the caller's session. Nothing here ever commits. Nothing here
ever raises: a broken DB reads as `{"status": "unavailable"}`.

ISO 27001: soft-deleted workflows (`deleted_at IS NOT NULL`, the SoftDeleteMixin
column on `class Workflow`) are invisible to every query, and a default that
points at a soft-deleted row is stale — it falls through to the count rules
rather than dispatching a deleted workflow.
"""
from __future__ import annotations

from product_names import themed_logger
from typing import Any

from sqlalchemy import text as satext
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from services import platform_settings as psr
from services.workflow_interfaces import _NAME_RE  # reuse the slug rule

logger = themed_logger(__name__, 'workflows')

_TABLE = "workflows"  # migration k's _TABLE; models.py has no `implements` yet

DEFAULTS_KEY = "expr.interface_default_implementations"
DEFAULT_DEFAULTS: dict = {}
_MAX_DEFAULT_ENTRIES = 200
_MAX_LIST = 100


def _validate_default_map(value) -> None:
    """Honest-range rule for the default map: dict, <=200 entries, keys are
    legal interface slugs, values are positive ints (workflow ids)."""
    if not isinstance(value, dict):
        raise ValueError(f"must be an object, got {type(value).__name__}")
    if len(value) > _MAX_DEFAULT_ENTRIES:
        raise ValueError(f"must have <=200 entries, got {len(value)}")
    for name, wf_id in value.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(
                "keys must be interface slugs "
                "^[a-z0-9][a-z0-9-]{0,63}$ "
                f"(got {name!r})"
            )
        if isinstance(wf_id, bool) or not isinstance(wf_id, int) or wf_id < 1:
            raise ValueError(
                f"values must be positive ints, got {wf_id!r} for {name!r}"
            )


psr.register(
    DEFAULTS_KEY,
    dict,
    DEFAULT_DEFAULTS,
    validator=_validate_default_map,
    description="workflow-expressiveness-primitives 3.4 (tasks.md 3.4) — per-tenant "
                "DEFAULT implementation of an interface: JSON object "
                "{interface_name: workflow_id}. The by-interface dispatch "
                "endpoint uses this when the caller names no implementation; "
                "a tenant-scope row overrides the platform row. Default {} "
                "(no defaults configured). Keys must be legal interface slugs "
                "(^[a-z0-9][a-z0-9-]{0,63}$), values positive workflow ids, "
                "max 200 entries.",
    group="expr",
)


def _valid_name(interface_name: Any) -> bool:
    """The interface-name rule, reused from services/workflow_interfaces."""
    return isinstance(interface_name, str) and bool(_NAME_RE.match(interface_name))


def _rows(db: Session, tenant_id: int, interface_name: str,
          limit: int = _MAX_LIST) -> list[tuple[int, str]]:
    """Live implementations of `interface_name` for ONE tenant, id-ordered.

    `implements` / `extends_workflow_id` are not in models.py yet (migration
    k is uncommitted), so this is raw DDL-side text with BIND PARAMS ONLY —
    the caller's strings never reach the SQL source.
    """
    stmt = satext(
        f"SELECT id, name FROM {_TABLE} "
        "WHERE tenant_id = :t AND implements = :n AND deleted_at IS NULL "
        "ORDER BY id"
    )
    found = db.execute(stmt, {"t": int(tenant_id), "n": interface_name}).fetchall()
    return [(int(r[0]), str(r[1])) for r in found[:limit]]


def list_implementations(db: Session, *, tenant_id: int,
                         interface_name: str) -> list[dict]:
    """[{id, name}] for one tenant's live implementations, id-ordered, max 100.

    Returns [] (never raises) on a bad name or a broken DB read.
    """
    if not _valid_name(interface_name):
        return []
    try:
        with db.begin_nested():
            rows = _rows(db, tenant_id, interface_name)
    except SQLAlchemyError as exc:
        logger.warning("workflow_interface_resolver list read failed: %s",
                       type(exc).__name__)
        return []
    return [{"id": wf_id, "name": name} for wf_id, name in rows]


def _tenant_default(db: Session, tenant_id: int,
                    interface_name: str) -> tuple[int | None, str]:
    """The tenant's default workflow id for this interface, plus where it came from.

    Fail-closed like services/adversary_rate_limit._limit: any settings-layer
    failure means "no default" (the count rules then decide), never a guess.
    """
    try:
        value = psr.get_setting(DEFAULTS_KEY, db=db, tenant_id=tenant_id)
    except Exception:  # noqa: BLE001 — fail-closed: broken settings layer
        logger.exception("workflow_interface_resolver: default setting unreadable")
        return None, "unreadable"
    if not isinstance(value, dict):
        logger.warning("workflow_interface_resolver: default setting not a dict: %s",
                       "TypeError")
        return None, "unreadable"
    wf_id = value.get(interface_name)
    if isinstance(wf_id, bool) or not isinstance(wf_id, int) or wf_id < 1:
        return None, "unset"
    return wf_id, "setting"


def _qualifies(db: Session, tenant_id: int, wf_id: int,
               interface_name: str) -> bool:
    """Does a specific workflow still qualify as an implementation?

    Same tenant, live, and it really does declare `implements = name`. A
    default that no longer qualifies (soft-deleted, re-typed, or a row that
    belongs to another tenant) is treated as stale and falls through.
    """
    stmt = satext(
        f"SELECT EXISTS (SELECT 1 FROM {_TABLE} "
        "WHERE id = :i AND tenant_id = :t AND implements = :n "
        "AND deleted_at IS NULL)"
    )
    row = db.execute(stmt, {"i": int(wf_id), "t": int(tenant_id),
                            "n": interface_name}).first()
    return bool(row and row[0])


def resolve_implementation(db: Session, *, tenant_id: int,
                           interface_name: str,
                           implementation_id: int | None = None) -> dict:
    """Decide which workflow answers `interface_name` for this tenant.

    Outcomes (never raises, never commits):
      * `{"status": "invalid_interface"}` — the name is not a legal slug.
      * `{"status": "not_found"}` — an explicit `implementation_id` that is
        not this tenant's live implementation of this interface. Deliberately
        identical for "other tenant's row", "soft-deleted" and "does not
        implement this interface": the shape never reveals another tenant's
        rows exist.
      * `{"status": "no_implementation"}` — nothing implements it.
      * `{"status": "ambiguous", "candidates": [ids]}` — more than one and no
        default picked one.
      * `{"status": "unavailable"}` — the read itself failed (e.g. the
        `implements` column is not on the live DB until migration k lands).
      * `{"status": "resolved", "workflow_id": id, "source": ...}` where
        source is "explicit" (caller named it), "default" (the tenant's
        setting named it) or "sole" (exactly one implementation existed).
    """
    if not _valid_name(interface_name):
        return {"status": "invalid_interface"}

    try:
        with db.begin_nested():
            if implementation_id is not None:
                try:
                    wanted = int(implementation_id)
                except (TypeError, ValueError):
                    return {"status": "not_found"}
                if wanted < 1 or not _qualifies(db, tenant_id, wanted,
                                                interface_name):
                    return {"status": "not_found"}
                return {"status": "resolved", "workflow_id": wanted,
                        "source": "explicit"}

            default_id, _origin = _tenant_default(db, tenant_id, interface_name)
            if default_id is not None:
                if _qualifies(db, tenant_id, default_id, interface_name):
                    return {"status": "resolved", "workflow_id": default_id,
                            "source": "default"}
                # A default that no longer qualifies (soft-deleted row, a row
                # that stopped declaring this interface): fall through to the
                # count rules. Class-free message — the setting key plus the
                # stale id is enough to find it in the log.
                logger.warning(
                    "workflow_interface_resolver: stale default %s for %s of "
                    "tenant %s no longer qualifies; falling through",
                    default_id, interface_name, int(tenant_id),
                )

            rows = _rows(db, tenant_id, interface_name)

    except SQLAlchemyError as exc:
        logger.warning("workflow_interface_resolver read failed: %s",
                       type(exc).__name__)
        return {"status": "unavailable"}

    if not rows:
        return {"status": "no_implementation"}
    if len(rows) == 1:
        return {"status": "resolved", "workflow_id": rows[0][0], "source": "sole"}
    return {"status": "ambiguous",
            "candidates": [wf_id for wf_id, _name in rows]}
