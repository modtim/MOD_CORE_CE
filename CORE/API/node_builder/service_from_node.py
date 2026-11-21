"""service-stack-builder Group B — node -> long-lived service resolver.

A MOD node is a **one-shot executor**: the build path bakes a smoke-guard
entrypoint (`MOD_VARIANT_SMOKE`) over a `/data/input -> /data/output -> exit`
contract (see DockerContainers/build-task/stages/build_one_variant.py). That
specialization is exactly what makes a node auto-exit — useless as a persistent
service.

`resolve_node_as_service` reads a `NodeDefinition` and emits a **de-specialized**
compose-service fragment: the node image plus its declared env/ports/GPU, run on
the image's OWN long-lived command under `restart: unless-stopped`. It emits NONE
of the one-shot specialization — no smoke-guard, no `/data/input` or
`/data/output` wiring, no `MOD_VARIANT_SMOKE`, no auto-shutdown.

A node that declares no runnable long-lived command (only the one-shot contract)
and whose caller supplies none raises `UnrunnableNodeError` rather than emitting a
service that would exit immediately (design.md "Node de-specialization" risk).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from service_builder_schemas import ServiceFragment

# Accelerator process_type values that mean the node wants a GPU. NodeDefinition
# stores the accelerator kind here ("CUDA" / "GPU" / "ROCM" / "CPU").
_GPU_PROCESS_TYPES = {"CUDA", "GPU", "ROCM"}

# custom_metadata keys a node may use to declare its OWN long-lived command
# (the command to run as a service, distinct from the one-shot entrypoint the
# build path baked in). Checked in order.
_COMMAND_KEYS = ("service_command", "command", "long_lived_command")


class UnrunnableNodeError(ValueError):
    """Raised when a node has only the one-shot executor contract and no
    long-lived command is declared or supplied — deploying it as a service
    would exit immediately, so we refuse before deploy."""


def _resolve_image(node) -> str:
    """The node's container image reference. Primary source is
    `executable_path`; fall back to a declared GPU variant image."""
    image = (getattr(node, "executable_path", None) or "").strip()
    if image:
        return image
    variants = getattr(node, "gpu_variants", None) or {}
    if isinstance(variants, dict):
        for key in ("cuda", "rocm", "gpu", "cpu"):
            candidate = (variants.get(key) or "").strip() if variants.get(key) else ""
            if candidate:
                return candidate
    return ""


def _resolve_command(node, command: Optional[str]) -> Optional[str]:
    """The long-lived command to run. A caller-supplied command wins; otherwise
    read a command the node declared for itself in custom_metadata. Returns None
    if neither exists (the node only carries the one-shot contract)."""
    if command and command.strip():
        return command.strip()
    md = getattr(node, "custom_metadata", None) or {}
    if isinstance(md, dict):
        for key in _COMMAND_KEYS:
            declared = md.get(key)
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
    return None


def _resolve_environment(node) -> Dict[str, str]:
    """Env the node declares for itself (custom_metadata.environment / .env).
    Only what's configured — a node may declare none."""
    md = getattr(node, "custom_metadata", None) or {}
    if not isinstance(md, dict):
        return {}
    declared = md.get("environment")
    if not isinstance(declared, dict):
        declared = md.get("env")
    if not isinstance(declared, dict):
        return {}
    return {str(k): str(v) for k, v in declared.items()}


def _resolve_ports(node) -> List[int]:
    """Container ports the node declares (custom_metadata.ports). A node service
    may declare many ports or none (a showcase/dashboard with no input)."""
    md = getattr(node, "custom_metadata", None) or {}
    if not isinstance(md, dict):
        return []
    declared = md.get("ports") or []
    ports: List[int] = []
    if isinstance(declared, (list, tuple)):
        for p in declared:
            try:
                ports.append(int(p))
            except (TypeError, ValueError):
                continue
    return ports


def _resolve_gpu_deploy(node) -> Dict:
    """Emit `deploy.resources.reservations` for a GPU node, or {} for a
    CPU-only node. Uses the compose device-reservation shape the stack GPU
    path already speaks (driver nvidia + capabilities [gpu])."""
    process_type = str(getattr(node, "process_type", None) or "").upper()
    wants_gpu = process_type in _GPU_PROCESS_TYPES
    variants = getattr(node, "gpu_variants", None) or {}
    if isinstance(variants, dict) and any(k in variants for k in ("cuda", "rocm", "gpu")):
        wants_gpu = True

    count = 1
    md = getattr(node, "custom_metadata", None) or {}
    if isinstance(md, dict):
        gpu_meta = md.get("gpu")
        if isinstance(gpu_meta, dict):
            if gpu_meta.get("required") is False:
                wants_gpu = False
            try:
                count = max(1, int(gpu_meta.get("count", 1)))
            except (TypeError, ValueError):
                count = 1

    if not wants_gpu:
        return {}
    return {
        "resources": {
            "reservations": {
                "devices": [
                    {"driver": "nvidia", "count": count, "capabilities": ["gpu"]},
                ],
            },
        },
    }


def resolve_node_as_service(
    db,
    node_definition_id: int,
    *,
    command: Optional[str] = None,
) -> ServiceFragment:
    """Resolve a `NodeDefinition` into a long-lived compose-service fragment.

    Reads the node's image reference + declared env/ports/GPU and emits a
    `ServiceFragment` that runs the image's own long-lived command under
    `restart: unless-stopped`, with NONE of the one-shot node specialization
    (no smoke-guard, no /data/input|/data/output wiring, no MOD_VARIANT_SMOKE,
    no auto-shutdown).

    Args:
        db: SQLAlchemy session.
        node_definition_id: the NodeDefinition to pull in.
        command: optional operator-supplied long-lived command; required when
            the node declares none of its own.

    Raises:
        LookupError: no such NodeDefinition.
        UnrunnableNodeError: the node carries only the one-shot contract (no
            long-lived command declared or supplied), or has no image to run.
    """
    from models import NodeDefinition

    node = (
        db.query(NodeDefinition)
        .filter(NodeDefinition.id == node_definition_id)
        .first()
    )
    if node is None:
        raise LookupError(f"NodeDefinition {node_definition_id} not found")

    md = getattr(node, "custom_metadata", None) or {}
    if isinstance(md, dict) and md.get("no_container") is True:
        raise UnrunnableNodeError(
            f"Node {node_definition_id} ({node.name!r}) has no container "
            f"(custom_metadata.no_container) — it cannot run as a service."
        )

    image = _resolve_image(node)
    if not image:
        raise UnrunnableNodeError(
            f"Node {node_definition_id} ({node.name!r}) declares no image "
            f"reference (executable_path) — nothing to run as a service."
        )

    resolved_command = _resolve_command(node, command)
    if not resolved_command:
        raise UnrunnableNodeError(
            f"Node {node_definition_id} ({node.name!r}) declares no long-lived "
            f"command — it only carries the one-shot executor contract "
            f"(/data/input -> /data/output -> exit). Supply a `command` for the "
            f"service, or declare one on the node, before deploying it as a "
            f"long-lived service."
        )

    return ServiceFragment(
        image=image,
        command=resolved_command,
        environment=_resolve_environment(node),
        ports=_resolve_ports(node),
        deploy=_resolve_gpu_deploy(node),
        restart="unless-stopped",
    )
