"""node_definitions_domains/mpc — Secret-sharing MPC NodeDefinitions.

Fortress 6.6 slice 4a. Registers the four platform-owned MPC nodes (MPC Joint
Sum, MPC Joint Mean, MPC Joint Count, MPC Contribute) so the catalog can speak
the primitives the `mpc-mpspdz` container implements
(`CORE/DockerContainers/mpc-mpspdz/entrypoint.py`, Fortress 6.6 slice 3).

Posture notes
-------------
- Specs are plain dicts (same shape as `he_seal.py`) so the module is
  importable + testable without a DB; `register_mpc()` is the only DB-touching
  entry point.
- One image, four rows: every spec shares `CONTAINER_IMAGE` and differs ONLY
  by the `MPC_OP` default (the entrypoint dispatches on the job's `op`) plus
  the per-node port shapes. Same image naming/tag convention as he-seal
  (`he_seal.py:48-50`): bare `repo:latest` tag through `platform_image()`.
- The op selector is spelled twice on purpose, exactly as `he_seal.py` does:
  `MPC_OP` is the top-level contract-conformant `*_OP` key tests/test_domain_primitives.py
  requires of every domain module, and `OP` (in `extra_variables`, i.e. injected
  into the container as env) mirrors it; the entrypoint itself reads the op from
  /data/input/job.json (`entrypoint.py:153`, `job.get("op")`). Both carry the
  same default.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `he_seal.py` does — existing rows are never deleted.
- HIDDEN while the image is unbuilt: every row is seeded with
  `node_role="system"` (→ derived `system_only=True`, models.py:2644) and
  `is_in_marketplace=False`, the same pair `he_seal.py` uses. The tenant palette
  query (`crud/node_definition_cud.py:285`) filters `system_only == False`, so
  these nodes are invisible to tenant palettes and the marketplace until the
  `mpc-mpspdz` image is built/pushed and a developer deliberately promotes them.
- Trust model (design /tmp/design_fortress_66.md §5 slice 4): replicated
  secret sharing (rep3-field) hides each participant's value from the OTHER
  participants' workflows, but the PLATFORM OPERATOR (who runs the engine
  container and holds the session state) sees the shares/inputs. The copy says
  so on every row; participants only ever receive the aggregate.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

CONTAINER_REPO_TAG = "mpc-mpspdz:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/mpc-mpspdz/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"
COST_IN_CREDITS = 1

# The entrypoint's op set (entrypoint.py:45, OPS = ("sum", "mean", "count"));
# "contribute" is the session-posting row driven by the session service, not
# the MP-SPDZ engine itself.
TRUST_MODEL_NOTE = (
    " Trust model: the platform operator sees the participants' inputs/shares "
    "(it runs the engine container); participants see only the aggregate and "
    "never each other's values (Fortress 6.6 rep3-field)."
)

SESSION_PORT = {
    "type": "string",
    "description": (
        "MPC session id (mpc_session_state row created via POST /mpc-sessions). "
        "The session pins the participants and the frozen security properties; "
        "this node refuses a session whose status is not ready."
    ),
}
VALUE_PORT = {
    "type": "number",
    "description": (
        "This participant's integer input. Shared as a replicated secret "
        "sharing: the value travels to the engine container only, never to the "
        "other participants' workflows."
    ),
}


def _input_ports(op: str) -> dict:
    """Named input ports for *op*: session_id + numeric value for every op."""
    return {"session_id": SESSION_PORT, "value": VALUE_PORT}


def _output_ports(op: str) -> dict:
    """Output ports for *op*: the aggregate only, except contribute (status).

    No op exposes a per-participant output port — the engine prints only the
    revealed aggregate (`entrypoint.py:79`, `(x + y + z).reveal()`), so nothing
    else exists to expose.
    """
    if op == "contribute":
        return {
            "output_type": "json",
            "status": {
                "type": "string",
                "description": (
                    "Contribution outcome (e.g. accepted). No aggregate is "
                    "produced by this node — it posts the input into the "
                    "session; the aggregate comes out of the joint nodes."
                ),
            },
        }
    return {
        "output_type": "json",
        "aggregate": {
            "type": "number",
            "description": (
                "The single revealed aggregate over all participants' shares. "
                "The only output: no per-participant value is ever emitted."
            ),
        },
    }


def _config(op: str) -> dict:
    """configuration_schema for *op*: the fixed op selector + extra_variables.

    The selector is spelled twice on purpose, exactly as he_seal.py does.
    `MPC_OP` is the top-level, contract-conformant `*_OP` key the domain-primitive
    contract test (tests/test_domain_primitives.py) requires every domain module
    to carry; `OP` lives in `extra_variables` (injected into the container as
    env) so the session service can hand the op to the entrypoint's job.json
    (`entrypoint.py:153`, `job.get("op")`) without re-deriving it. Both carry
    the same default.
    """
    op_desc = (
        "Fixed for this node: selects the MPC computation. Leave it as-is "
        "unless you have a reason to change it."
    )
    return {
        "MPC_OP": {
            "type": "string",
            "default": op,
            "description": op_desc,
        },
        "extra_variables": {
            "OP": {
                "type": "string",
                "default": op,
                "description": op_desc,
            },
        },
    }


def _spec(name: str, op: str, summary: str) -> dict:
    return {
        "name": name,
        "description": summary + TRUST_MODEL_NOTE,
        "type": "Privacy",
        "executable_path": CONTAINER_IMAGE,
        "process_type": PROCESS_TYPE,
        "node_kind": op,
        "batch_mode": BATCH_MODE,
        "input_schema": {"input_type": "json", **_input_ports(op)},
        "output_schema": _output_ports(op),
        "configuration_schema": _config(op),
        "accepted_formats": None,
        "cost_in_credits": COST_IN_CREDITS,
    }


# ---------------------------------------------------------------------------
# The four specs. One image; they differ ONLY by the op default + port shapes.
# ---------------------------------------------------------------------------

MPC_JOINT_SUM_SPEC = _spec(
    "MPC Joint Sum",
    "sum",
    "Sum of three participants' integers computed with replicated secret "
    "sharing (rep3-field, MP-SPDZ): each party contributes sint.get_input_from "
    "and only the revealed total is printed. No participant learns the others' "
    "values; the result is visible to all participants + the platform operator.",
)
MPC_JOINT_MEAN_SPEC = _spec(
    "MPC Joint Mean",
    "mean",
    "Mean of three participants' integers: the in-MPC sum (x + y + z) is "
    "revealed once, then divided by the public party count 3 in cleartext "
    "(entrypoint.py:68-71). Inputs stay secret; only the mean is emitted.",
)
MPC_JOINT_COUNT_SPEC = _spec(
    "MPC Joint Count",
    "count",
    "Joint count across three participants: each party's value is compared to "
    "zero inside MPC (replicated secret sharing) and the three non-zero "
    "indicators are summed — the result is how many participants submitted a "
    "non-zero value, never which ones or their actual values. A negative "
    "input is rejected as input_out_of_bound before any MP-SPDZ process "
    "spawns.",
)
MPC_CONTRIBUTE_SPEC = _spec(
    "MPC Contribute",
    "contribute",
    "Posts this tenant's integer into an MPC session (session_id + value) so "
    "the session service can collect all three inputs and dispatch the engine "
    "container. Emits only a status — no aggregate, and no other participant's "
    "input is readable through this node.",
)

NODE_DEFINITIONS = [
    MPC_JOINT_SUM_SPEC,
    MPC_JOINT_MEAN_SPEC,
    MPC_JOINT_COUNT_SPEC,
    MPC_CONTRIBUTE_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "sum": MPC_JOINT_SUM_SPEC,
    "mean": MPC_JOINT_MEAN_SPEC,
    "count": MPC_JOINT_COUNT_SPEC,
    "contribute": MPC_CONTRIBUTE_SPEC,
}

EXPECTED_NAMES = tuple(spec["name"] for spec in NODE_DEFINITIONS)

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_mpc(db: Session) -> int:
    """Register the 4 MPC nodes as system node definitions. Returns NEW rows.

    Idempotent: an existing system row with the same name has its schema/
    description/cost reconciled to this file, and is not counted as created.
    New rows are seeded HIDDEN — node_role="system" (→ derived system_only=True)
    plus is_in_marketplace=False — because the `mpc-mpspdz` image is not built/
    pushed yet, exactly as register_he_seal() does. The registry's post-savepoint
    curation pass may still flip a NEW row to is_in_marketplace=True; that does
    not publish it either, because the palette query filters `system_only ==
    False` (crud/node_definition_cud.py:285) AND marketplace visibility
    additionally requires a tenant entitlement row. Both guards hold while the
    image is unbuilt. A developer promotes a row (node_role="user") once the
    image ships.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping mpc-mpspdz MPC domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — platform-owned, not tenant-scoped
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
                node_role="system",
                is_in_marketplace=False,
            )
        )
        created += 1

    if created > 0:
        db.commit()

    return created
