"""Registry-driven seed for every node_definitions_domains module.

This module centralises what the startup seeder in main.py was missing:
the domain-node modules each define a ``register_<domain>() -> int``
function that was *never called* at startup, leaving every "seed batch
pending" node invisible.

Usage (in main.py startup block):

.. code-block:: python

    from node_definitions_domains._registry import register_all_domain_nodes

    result = register_all_domain_nodes(db_session)
    db_session.commit()  # caller commits after all SAVEPOINTs resolve

Each entry runs inside a SAVEPOINT so one module's failure rolls back
only itself and the rest of the batch continues.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from sqlalchemy.orm.attributes import flag_modified

from models import NodeDefinition
from node_definitions_domains._shared import accepted_secrets_from_schema
from utils.paid_import import optional_paid

logger = logging.getLogger(__name__)


def _spec_names(mod) -> list[str]:
    """Node names a domain module registers.

    Generic across modules: prefer ``NODE_DEFINITIONS`` (list of spec dicts);
    fall back to the values of ``NODE_DEFINITIONS_BY_OP``.
    """
    specs = getattr(mod, "NODE_DEFINITIONS", None)
    if not isinstance(specs, list):
        by_op = getattr(mod, "NODE_DEFINITIONS_BY_OP", None)
        specs = list(by_op.values()) if isinstance(by_op, dict) else []
    names: list[str] = []
    for spec in specs:
        if isinstance(spec, dict):
            name = spec.get("name")
            if name:
                names.append(name)
    return names


def _existing_system_names(db, names: list[str]) -> set[str]:
    """Names that already have a live system row (tenant_id IS NULL, not
    soft-deleted). Pre-existing rows are admin-curated: never touched."""
    if not names:
        return set()
    rows = db.query(NodeDefinition.name).filter(
        NodeDefinition.tenant_id.is_(None),
        NodeDefinition.deleted_at.is_(None),
        NodeDefinition.name.in_(names),
    ).all()
    return {r[0] for r in rows}

# Explicit list of (module_name, function_name) for every domain module
# that exports a ``register_*`` function. Kept here so the caller (main.py)
# is one line and so the test can verify coverage completeness.
DOMAIN_REGISTRARS: tuple[tuple[str, str], ...] = (
    ("audio_embeddings", "register_audio_embeddings"),
    ("browser_automation", "register_browser_automation"),
    ("chat_webhooks", "register_chat_webhooks"),
    ("cloud_storage_upload", "register_cloud_storage_upload"),
    ("crypto_utils", "register_crypto_utils"),
    ("database_crud", "register_database_crud"),
    ("datetime_utils", "register_datetime_utils"),
    ("email_send", "register_email_send"),
    ("file_converters", "register_file_converters"),
    ("geo", "register_geo"),
    ("he_seal", "register_he_seal"),
    ("mpc", "register_mpc"),
    ("image_embeddings", "register_image_embeddings"),
    ("image_ops", "register_image_ops"),
    ("ocr", "register_ocr"),
    ("pdf_processing", "register_pdf_processing"),
    ("qr_barcode", "register_qr_barcode"),
    ("sms_push", "register_sms_push"),
    ("spreadsheet", "register_spreadsheet"),
    ("text_embeddings", "register_text_embeddings"),
    ("text_utils", "register_text_utils"),
    ("translation", "register_translation"),
    ("tts", "register_tts"),
    ("web_scraper", "register_web_scraper"),
    ("webhook_post", "register_webhook_post"),
)

# ---------------------------------------------------------------------------
# Paid-optional domain modules (GT.D4b posture)
# ---------------------------------------------------------------------------
# open-core/manifest.yaml denies these domain modules from the CE carve
# (he_seal.py, manifest:1975), but they stay listed in DOMAIN_REGISTRARS so
# the paid tree keeps seeding them and the CE node-seed keeplist stays
# honest. In a CE image the module is simply absent: register_all_domain_nodes
# resolves it through utils.paid_import.optional_paid and skips it, and the
# phase-6 registry/file checks treat these names as "registered, file
# legitimately absent in this edition" instead of an orphan registration.
# Strictness is unchanged for every other module: a name NOT listed here with
# no file on disk is still an orphan, and a present module still registers.
PAID_OPTIONAL_DOMAIN_MODULES: frozenset[str] = frozenset({"he_seal"})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_all_domain_nodes(
    db: Session,
    *,
    keeplist_names: Optional[set[str]] = None,
) -> dict[str, int | str]:
    """Run every domain-node registrar inside a SAVEPOINT.

    When ``keeplist_names`` is provided, only modules whose node names
    overlap the keeplist are registered (used by CE edition to limit
    seeding to the open-source palette).  Modules with no overlap are
    logged and skipped.

    Returns a dict mapping *module_name* → rows-upserted (int) or
    ``"error: <ClassName>"`` (str) on failure.

    One module's SAVEPOINT failure does NOT cancel another module's
    work; the session remains usable after the full run.
    """
    results: dict[str, int | str] = {}
    skipped: int = 0

    for mod_name, func_name in DOMAIN_REGISTRARS:
        try:
            if mod_name in PAID_OPTIONAL_DOMAIN_MODULES:
                # Paid-optional (CE carve): absent in a CE image is expected.
                mod = optional_paid(f"node_definitions_domains.{mod_name}")
                if mod is None:
                    skipped += 1
                    results[mod_name] = 0
                    continue
                fn = getattr(mod, func_name)
            else:
                mod = importlib.import_module(
                    f"node_definitions_domains.{mod_name}"
                )
                fn = getattr(mod, func_name)
        except Exception as exc:  # noqa: BLE001
            err = f"error: {exc.__class__.__name__}"
            results[mod_name] = err
            logger.warning(
                "register_all_domain_nodes: %s — %s", mod_name, err
            )
            continue

        # CE gate: skip modules whose node names are not on the keeplist.
        if keeplist_names is not None:
            spec_names = _spec_names(mod)
            if not any(n in keeplist_names for n in spec_names):
                skipped += 1
                results[mod_name] = 0
                continue

        try:
            # B98c: snapshot which of this module's node names already have a
            # live system row BEFORE the SAVEPOINT runs, so after it we can
            # tell which rows this call newly created. Newly created rows get
            # curated into the marketplace (is_in_marketplace=True) so tenant
            # palettes and the starter-package sync can see them. Pre-existing
            # rows are never touched — admin curation stays authoritative.
            spec_names = _spec_names(mod)
            pre_existing = _existing_system_names(db, spec_names)

            with db.begin_nested():
                count = fn(db)
            results[mod_name] = count

            for new_name in spec_names:
                if new_name in pre_existing:
                    continue
                new_row = db.query(NodeDefinition).filter(
                    NodeDefinition.name == new_name,
                    NodeDefinition.tenant_id.is_(None),
                    NodeDefinition.deleted_at.is_(None),
                ).first()
                if new_row is not None and not new_row.is_in_marketplace:
                    new_row.is_in_marketplace = True

            # ---- Reconcile accepted_secrets after savepoint resolves ----
            try:
                mod_spec = importlib.import_module(
                    f"node_definitions_domains.{mod_name}"
                )
                node_defs = getattr(mod_spec, "NODE_DEFINITIONS", [])
                if isinstance(node_defs, list):
                    for spec in node_defs:
                        row_name = spec.get("name")
                        if not row_name:
                            continue
                        row = db.query(NodeDefinition).filter(
                            NodeDefinition.name == row_name,
                            NodeDefinition.tenant_id.is_(None),
                        ).first()
                        if row is None:
                            continue

                        derived = accepted_secrets_from_schema(
                            spec.get("configuration_schema") or {}
                        )

                        existing_meta: dict = (
                            (row.custom_metadata or {}).copy()
                        )
                        current_accepted = existing_meta.get(
                            "accepted_secrets"
                        )
                        source_marker = existing_meta.get(
                            "accepted_secrets_source"
                        )

                        if derived:
                            # Only overwrite if we own the value (no marker means
                            # it's brand-new; "domain_schema" means we wrote it).
                            # Never overwrite manual accepted_secrets (present but
                            # no marker).
                            if current_accepted is None or source_marker == "domain_schema":
                                existing_meta["accepted_secrets"] = derived
                                existing_meta["accepted_secrets_source"] = (
                                    "domain_schema"
                                )
                                row.custom_metadata = existing_meta
                                flag_modified(row, "custom_metadata")
                        elif source_marker == "domain_schema":
                            existing_meta.pop("accepted_secrets", None)
                            existing_meta.pop(
                                "accepted_secrets_source", None
                            )
                            row.custom_metadata = (
                                existing_meta if existing_meta else {}
                            )
                            flag_modified(row, "custom_metadata")
            except Exception as _reconcile_exc:
                logger.warning(
                    "accepted_secrets reconcile for %s failed — %s",
                    mod_name,
                    _reconcile_exc,
                )
        except Exception as exc:  # noqa: BLE001
            err = f"error: {exc.__class__.__name__}"
            results[mod_name] = err
            logger.warning(
                "register_all_domain_nodes: %s — %s", mod_name, err
            )

    ok = sum(1 for v in results.values() if isinstance(v, int))
    fail = sum(1 for v in results.values() if isinstance(v, str))
    logger.info(
        "register_all_domain_nodes complete: %d ok, %d failed, %d skipped "
        "out of %d",
        ok, fail, skipped, len(DOMAIN_REGISTRARS),
    )
    return results
