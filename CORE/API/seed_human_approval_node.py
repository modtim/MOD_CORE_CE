"""Seed the Human Approval node (ROADMAP Gate 5).

The node was created by hand in the database during Gate 5, which meant
`start-main-server.sh` — a reformat that drops every named volume — would take
it with it. The canvas entry survives that (the operator primitives are
client-side constants in `node-library.tsx`, which is how they bypass the
marketplace gate that hides uncurated system NodeDefinitions), but the row it
points at would be gone and `data.nodeDefinitionId` would dangle.

WHAT THIS ROW IS FOR, given the node never runs a container. The orchestrator
branches on `node_kind == "human_approval"` BEFORE dispatch and parks the run,
so nothing here is ever pulled or executed — `executable_path` is deliberately
NULL. The row exists so the id on the canvas resolves to something real for
anything that looks a definition up, and so queue selection has a
NodeDefinition to be authoritative about rather than falling back to a guess.

Idempotent on EITHER identity key (system-scope name OR node_kind), matching
seed_vector_nodes' name key and widened to cover the kind drift that made the
palette show the gate twice (CE sweep 17, wb-ce-batch24).

open-core-tier-structure §4.1 — this is part of MOD's bundled node catalog.
A fresh install no longer seeds it by default; existing deployments are
untouched (this only gates the INSERT path, never deletes/modifies rows
already in the DB). Set MOD_SEED_BUNDLED_NODES=false to skip. Defaults to
true so current/dev behavior is unchanged until an operator opts out.
"""
import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition

logger = logging.getLogger(__name__)

HUMAN_APPROVAL_NODE = {
    "name": "Human Approval",
    "node_kind": "human_approval",
    "type": "Operators",
    "description": (
        "Pauses the run until a person approves or rejects. "
        "No worker is held while it waits."
    ),
    # ANY in, ANY out — a gate sits between two arbitrary nodes and cares
    # about neither's payload.
    "input_schema": {"input_type": "ANY", "input_variables": "NONE"},
    "output_schema": {"output_type": "ANY", "file_mode": "both",
                      "output_variables": "NONE"},
    "process_type": "CPU",
    "batch_mode": "batch",
    "cost_in_credits": 0,
    "scan_status": "approved",
    "calibration_status": "uncalibrated",
    "container_timeout": 300,
    "custom_metadata": {"no_container": True},
    # WB14-PRODFIX (CE launch) — the CE-inclusion SOURCE OF TRUTH (the same
    # stamp node_scanner.py:1296 writes on CREATE). Without it this seeded row
    # lands on models.py:2726's server_default FALSE, which the palette reads as
    # "not CE": no CE chip on the gate and the dedupe arm misses (wb-ce-batch14
    # :42 / wb-ce-batch24 :95). Applied below only when the column exists, so a
    # DB predating migrations/20261007zu_fx73_p4c_ce_included.py still seeds.
    "ce_included": True,
}


def _ce_included_column_present(db: Session) -> bool:
    """True when node_definitions.ce_included is applied on this DB."""
    from sqlalchemy import inspect as sa_inspect
    try:
        bind = db.get_bind() if hasattr(db, "get_bind") else db
        columns = {c["name"] for c in sa_inspect(bind).get_columns("node_definitions")}
    except Exception:  # noqa: BLE001
        return False
    return "ce_included" in columns


def seed_human_approval_node(db: Session) -> int:
    """Register the Human Approval node. Returns rows created (0 if present).

    open-core-tier-structure §4.1: skipped entirely when MOD_SEED_BUNDLED_NODES
    is false — a fresh install then starts with no bundled Human Approval
    node. Existing rows already in the DB are never touched by this check;
    it only guards the INSERT path below.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping bundled Human "
            "Approval node seed"
        )
        return 0

    # Two identity keys for the same gate, either of which means "already
    # seeded": the system-scope name (seed_vector_nodes' key) and node_kind.
    # The name index (uq_system_live_node_def_name, models.py __table_args__)
    # only pins unique NAMES, so a pre-existing system row under another name
    # carrying node_kind == "human_approval" (hand-created at Gate 5, or
    # renamed) used to pass this guard and a second row was inserted — which
    # is what made the WB palette show the gate twice (CE sweep 17,
    # wb-ce-batch24). Soft-deleted rows do not count: they are outside the
    # partial unique index and the palette hides them, so re-seeding after a
    # soft delete is correct.
    existing = db.query(NodeDefinition).filter(
        NodeDefinition.tenant_id.is_(None),
        NodeDefinition.deleted_at.is_(None),
        (
            (NodeDefinition.name == HUMAN_APPROVAL_NODE["name"])
            | (NodeDefinition.node_kind == HUMAN_APPROVAL_NODE["node_kind"])
        ),
    ).first()
    if existing:
        return 0

    nd = NodeDefinition(
        name=HUMAN_APPROVAL_NODE["name"],
        tenant_id=None,  # system node — available to every tenant
        description=HUMAN_APPROVAL_NODE["description"],
        type=HUMAN_APPROVAL_NODE["type"],
        node_kind=HUMAN_APPROVAL_NODE["node_kind"],
        # NULL on purpose: this node is intercepted before dispatch and never
        # becomes a container, so there is no image to name.
        executable_path=None,
        process_type=HUMAN_APPROVAL_NODE["process_type"],
        batch_mode=HUMAN_APPROVAL_NODE["batch_mode"],
        input_schema=HUMAN_APPROVAL_NODE["input_schema"],
        output_schema=HUMAN_APPROVAL_NODE["output_schema"],
        cost_in_credits=HUMAN_APPROVAL_NODE["cost_in_credits"],
        scan_status=HUMAN_APPROVAL_NODE["scan_status"],
        calibration_status=HUMAN_APPROVAL_NODE["calibration_status"],
        container_timeout=HUMAN_APPROVAL_NODE["container_timeout"],
        custom_metadata=HUMAN_APPROVAL_NODE["custom_metadata"],
        system_only=False,
        version=0,
        is_adapter=False,
        image_size_bytes=0,
        review_requested=False,
        # Only when the column is applied (a DB predating the FX.73 p4c column
        # would reject the kwarg); the scanner's wide self-heal sweep covers the
        # stamp on the next scan once the migration lands.
        **({"ce_included": HUMAN_APPROVAL_NODE["ce_included"]}
           if _ce_included_column_present(db) else {}),
    )
    db.add(nd)
    db.commit()
    return 1
