"""Common helpers shared across all node_definitions_domains modules.

AI-primitive category tagging lives here so every domain module can
read / write the ``ai_category`` key inside a NodeDefinition's
``custom_metadata`` JSONB column.
"""

import logging
import re
from typing import Any

from database import SessionLocal
from models import NodeDefinition

from utils.paid_import import optional_paid

logger = logging.getLogger(__name__)

# 1647c2 (CE boot): services.ai_primitives.contracts is denied in the CE carve
# and this module is loaded at boot, so the module-level import was a CE boot
# breaker. Only the valid-category KEY set is used here (never the contract
# models), so CE gets that key set as a fallback.
_CE_CATEGORY_KEYS = (
    "embeddings",
    "vector_search",
    "llm_call",
    "rag_pattern",
    "tool_use",
    "fine_tune",
    "eval",
)

_contracts = optional_paid("services.ai_primitives.contracts")
CATEGORY_CONTRACTS = getattr(_contracts, "CATEGORY_CONTRACTS", None)


def _valid_category_keys() -> tuple[str, ...]:
    """Valid ai-primitive categories, with the CE fallback when the paid
    contracts module is absent."""
    if CATEGORY_CONTRACTS is None:
        return _CE_CATEGORY_KEYS
    return tuple(sorted(CATEGORY_CONTRACTS.keys()))

_SECRET_PROP_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

AI_CATEGORY_KEY = "ai_category"


def ai_category_of(node_def: Any) -> str | None:
    """Return the AI-primitive category stored in *node_def*.

    Safely walks ``custom_metadata`` and returns the value keyed by
    ``AI_CATEGORY_KEY`` or ``None`` when absent or the value is not
    a string.
    """
    meta = getattr(node_def, "custom_metadata", None)
    if not isinstance(meta, dict):
        return None
    return meta.get(AI_CATEGORY_KEY)


def tag_ai_category(
    db,
    node_definition_id: int,
    category: str,
    *,
    actor_id: int | None = None,
) -> bool:
    """Tag a NodeDefinition with an AI-primitive category.

    * *category* must be one of the keys in ``CATEGORY_CONTRACTS``
      (``embeddings``, ``vector_search``, ``llm_call``,
      ``rag_pattern``, ``tool_use``, ``fine_tune``, ``eval``).
    * Only system rows (``tenant_id IS NULL``) may be tagged.
      Tenant/user-owned copies raise ``ValueError``.
    * Soft-deleted rows (``deleted_at IS NOT NULL``) are excluded.

    Returns ``True`` if the value was changed, ``False`` if the row
    was already tagged with the same category.
    """
    valid_keys = _valid_category_keys()
    valid_cats = ", ".join(valid_keys)
    if category not in valid_keys:
        raise ValueError(
            f"unknown ai-primitive category {category!r}; "
            f"valid: {valid_cats}"
        )

    row = (
        db.query(NodeDefinition)
        .filter(NodeDefinition.id == node_definition_id)
        .first()
    )

    if row is None:
        return False

    # Must be a live system row (tenant_id IS NULL, not soft-deleted).
    if row.tenant_id is not None:
        raise ValueError(
            "can only tag system (tenant_id IS NULL) NodeDefinition rows; "
            f"row {row.id} has tenant_id={row.tenant_id}"
        )
    if row.deleted_at is not None:
        return False

    old_meta = getattr(row, "custom_metadata", None) or {}
    if not isinstance(old_meta, dict):
        old_meta = {}

    if old_meta.get(AI_CATEGORY_KEY) == category:
        return False

    new_meta = {**old_meta, AI_CATEGORY_KEY: category}
    row.custom_metadata = new_meta
    db.commit()
    return True


def list_by_ai_category(db, category: str):
    """Return live system rows tagged with *category*.

    Returns a list of NodeDefinition ORM instances where
    ``tenant_id IS NULL``, ``deleted_at IS NULL``, and
    ``custom_metadata->>'ai_category' == category``.
    """
    valid_keys = _valid_category_keys()
    valid_cats = ", ".join(valid_keys)
    if category not in valid_keys:
        raise ValueError(
            f"unknown ai-primitive category {category!r}; "
            f"valid: {valid_cats}"
        )

    return (
        db.query(NodeDefinition)
        .filter(
            NodeDefinition.tenant_id.is_(None),
            NodeDefinition.deleted_at.is_(None),
            NodeDefinition.custom_metadata[AI_CATEGORY_KEY].as_string() == category,
        )
        .all()
    )


# ---------------------------------------------------------------------------
# Accepted-secrets derivation from configuration_schema
# ---------------------------------------------------------------------------


def _scan_config_keys(
    props: dict, required_set: set[str], results: list[dict[str, Any]]
) -> None:
    """Inspect *props* dict values for ``secret=True`` and append entries."""
    for key, value in props.items():
        if not isinstance(value, dict):
            continue
        if value.get("secret") is not True:
            continue
        if not _SECRET_PROP_RE.match(key):
            logger.warning(
                "accepted_secrets_from_schema: skipping non-class-name prop %r",
                key,
            )
            continue
        entry: dict[str, Any] = {
            "env_var": key,
            "description": value.get("description", ""),
            "required": key in required_set,
            "oauth_providers": (
                list(value["oauth_providers"])
                if value.get("oauth_providers")
                else None
            ),
        }
        results.append(entry)


def accepted_secrets_from_schema(
    configuration_schema: dict,
) -> list[dict[str, Any]]:
    """Derive accepted_secrets entries from a configuration_schema dict.

    Scans top-level keys and the ``extra_variables`` sub-dict (if present)
    for properties with ``"secret": True``.  Property names must match
    class-name pattern ``^[A-Z][A-Z0-9_]{0,63}$`` or they are silently skipped.

    Returns a deterministic list (sorted by ``env_var``) of dicts::

        [{"env_var": str, "description": str, "required": bool, "oauth_providers": list|None}, ...]
    """
    if not isinstance(configuration_schema, dict):
        return []

    required_set: set[str] = set(configuration_schema.get("required", []))
    results: list[dict[str, Any]] = []

    _scan_config_keys(configuration_schema, required_set, results)

    # Also scan extra_variables sub-dict if present
    extra = configuration_schema.get("extra_variables")
    if isinstance(extra, dict):
        _scan_config_keys(extra, required_set, results)

    # Deduplicate by env_var (keep first occurrence)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in results:
        if entry["env_var"] not in seen:
            seen.add(entry["env_var"])
            deduped.append(entry)

    return sorted(deduped, key=lambda e: e["env_var"])
