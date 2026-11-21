"""stack-definition-edit business logic.

Owns the pure business logic the stack-definition-edit router endpoints
(``routers/persistent_stacks/persistent_stacks.py`` — GET/PUT
``/{stack_id}/definition``, ``/revisions``, ``/revisions/{n}/diff``,
``/revert``) delegate to: the ``stack_edit`` baseline-capability gate, the
editable-view shaping of a stored compose spec, and turning an edited
``ServiceSpec`` list back into a compose spec via the SAME resolvers
``/assemble`` uses (Group B/C source resolution + rendering) — one rendering
path for create-time and edit-time composition, not two.

Mirrors the split in ``services/node_code_edit_service.py``: capability/
business logic lives here; the ``authorize(...)``/RBAC calls and the
HTTPException-builders stay in the router (this module's callers are
monkeypatch-targeted by name from the router's own globals in tests).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import models
from services._base import ServiceError
from utils.entitlements import Cap, get_entitlements


class EntitlementRequiredError(ServiceError):
    """The acting tenant lacks the `stack_edit` capability (router maps this
    to a 402 with detail {"code": "entitlement_required", "capability": ...}).
    In practice this is unreachable in normal operation — `Cap.STACK_EDIT` is
    baseline in every edition (see `utils.entitlements` /
    `entitlements_from_plan`) — but the gate stays wired for defense in depth
    and so a future tier restructure has a single lever to pull."""

    def __init__(self) -> None:
        super().__init__(
            "stack_edit entitlement required",
            code="entitlement_required",
            details={"capability": Cap.STACK_EDIT},
        )


def require_stack_edit(db: Session, current_user: "models.User") -> None:
    """Gate stack DEFINITION EDITING (PUT .../definition, POST .../revert) on
    `Cap.STACK_EDIT` — a baseline capability granted in every edition/tier
    (owner decision, clean-v2-api Wave 10: "any tenant owner can edit their
    stacks in every edition"). This is intentionally a DIFFERENT capability
    from `Cap.STACK_WIZARD` ("mesh / multi-host / reservation authoring",
    which stays a paid gate and is untouched by this function) — editing an
    existing stack's compose/config is not the same act as authoring mesh/
    multi-host placement. RBAC (router-wide STACK_AUTHOR) + tenant ownership
    (`_get_owned`) remain the primary gate; this entitlement check is
    additional and, by construction, always satisfied. Resolved off the
    ACTING tenant, matching `node_code_edit_service.require_custom_nodes`."""
    es = get_entitlements(db, getattr(current_user, "tenant", None))
    if not es.has(Cap.STACK_EDIT):
        raise EntitlementRequiredError()


# =============================================================================
# --- compose_spec -> editable ServiceSpec-shaped view ---
# =============================================================================

def _service_view(name: str, svc: Dict[str, Any]) -> Dict[str, Any]:
    """One compose service, flattened into the SAME shape
    `service_builder_schemas.ServiceSpec` accepts, so the frontend's existing
    service-builder form (used at stack creation, via `/assemble`) can also
    drive editing of an already-deployed stack.

    Best-effort and LOSSY for `build`/node/git/script/site-originated services:
    a rendered compose service only carries the final `image`/`build`, not
    which resolver produced it, so every service reads back as `source.kind
    = "image"` (empty `image` for a build-based service). The user can still
    edit env/ports/volumes/resources/expose freely, or replace `image`
    outright; re-pointing a service at a different node/git/script/site source
    is a v1 limitation — remove + re-add the service instead."""
    env = svc.get("environment")
    if isinstance(env, list):
        env = {kv.split("=", 1)[0]: kv.split("=", 1)[1]
               for kv in env if isinstance(kv, str) and "=" in kv}
    elif not isinstance(env, dict):
        env = {}

    ports: List[int] = []
    for raw in list(svc.get("expose") or []) + list(svc.get("ports") or []):
        try:
            # A published "host:container" or "container" spec — take the
            # container side (last segment) since ServiceSpec.ports is
            # container-only (see service_builder_schemas.ServiceSpec).
            port = int(str(raw).split(":")[-1])
        except (TypeError, ValueError):
            continue
        if port not in ports:
            ports.append(port)

    deploy = svc.get("deploy") or {}
    reservations = ((deploy.get("resources") or {}).get("reservations") or {})
    cpu = 0.0
    try:
        cpu = float(reservations.get("cpus") or 0)
    except (TypeError, ValueError):
        cpu = 0.0
    from routers.persistent_stacks._shared import _parse_compose_memory
    mem_gb = _parse_compose_memory(reservations.get("memory"))

    xmod = svc.get("x-mod") if isinstance(svc.get("x-mod"), dict) else {}
    accel = xmod.get("accelerator") if isinstance(xmod.get("accelerator"), dict) else {}
    vram_gb = 0
    try:
        vram_gb = int(accel.get("min_vram_gb") or 0)
    except (TypeError, ValueError):
        vram_gb = 0

    replicas = deploy.get("replicas")
    try:
        replicas = int(replicas) if replicas is not None else 1
    except (TypeError, ValueError):
        replicas = 1

    return {
        "name": name,
        "source": {"kind": "image", "image": svc.get("image")},
        "env": env,
        "ports": ports,
        "volumes": [v for v in (svc.get("volumes") or []) if isinstance(v, str)],
        "gpu": {"vram_gb": vram_gb} if vram_gb > 0 else None,
        "replicas": replicas,
        "cpu": round(cpu, 6),
        "mem_gb": round(mem_gb, 6),
        "expose": bool(xmod.get("reachable")),
        "command": svc.get("command"),
    }


def services_view(compose_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    services = (compose_spec or {}).get("services") or {}
    if not isinstance(services, dict):
        return []
    return [_service_view(name, svc) for name, svc in services.items()
            if isinstance(svc, dict)]


def definition_view(stack: "models.PersistentStack",
                    rev: "models.PersistentStackRevision",
                    revisions: List["models.PersistentStackRevision"]) -> Dict[str, Any]:
    """The full GET .../definition payload."""
    from routers.persistent_stacks._shared import _redact_compose_secrets
    import services.persistent_stack_revisions as psr
    from services.stack_compose_import import compose_to_yaml

    redacted = _redact_compose_secrets(rev.compose_spec)
    return {
        "stack_id": stack.id,
        "name": stack.name,
        "editable": True,
        "current_revision_no": rev.revision_no,
        "revisions": [psr.revision_summary(r) for r in revisions],
        "compose_spec": redacted,
        # YAML rendering of the SAME redacted spec above (never the unredacted
        # stack.compose_spec) so the editor textarea can never leak a secret
        # env value that the JSON compose_spec already withholds. Must
        # round-trip: yaml.safe_load(compose_yaml) == compose_spec.
        "compose_yaml": compose_to_yaml(redacted),
        "services": services_view(redacted),
        "storage_class": rev.storage_class or stack.storage_class,
        "accelerator_requirements": stack.accelerator_requirements,
        "egress_policy": stack.egress_policy,
        # References only (Vault paths), never values — same posture as
        # `schemas.PersistentStack.secret_refs`.
        "secret_refs": stack.secret_refs,
        "tls_config": stack.tls_config,
    }


# =============================================================================
# --- restore secret env values an edit round-tripped as the redaction sentinel ---
# =============================================================================

def _unredact_env(new_env, prev_env):
    """One service's environment: replace any value equal to the redaction
    sentinel with the corresponding key's value from `prev_env`, IF that key
    still exists there and still looks like a secret. Leaves a genuinely new
    key holding the literal sentinel string untouched (nothing to restore it
    from) rather than guessing."""
    from routers.persistent_stacks._shared import _SECRET_ENV_RE, REDACTED_SENTINEL

    def _as_dict(env):
        if isinstance(env, dict):
            return dict(env)
        if isinstance(env, list):
            out = {}
            for e in env:
                if isinstance(e, str) and "=" in e:
                    k, v = e.split("=", 1)
                    out[k] = v
            return out
        return {}

    prev = _as_dict(prev_env)
    if isinstance(new_env, dict):
        out = dict(new_env)
        for k, v in list(out.items()):
            if v == REDACTED_SENTINEL and _SECRET_ENV_RE.search(str(k)) and k in prev:
                out[k] = prev[k]
        return out
    if isinstance(new_env, list):
        out = []
        for e in new_env:
            if isinstance(e, str) and "=" in e:
                k, v = e.split("=", 1)
                if v == REDACTED_SENTINEL and _SECRET_ENV_RE.search(k) and k in prev:
                    v = prev[k]
                out.append(f"{k}={v}")
            else:
                out.append(e)
        return out
    return new_env


def unredact_against(new_spec: Dict[str, Any], previous_spec: Optional[Dict[str, Any]]
                     ) -> Dict[str, Any]:
    """Before validating/storing an edited compose_spec, restore any secret-
    looking env value the editor round-tripped as the `***redacted***`
    placeholder (GET .../definition redacts on the way out — see
    `_redact_compose_secrets` — so a save that doesn't touch a given secret
    would otherwise overwrite it with the literal placeholder string). Applies
    uniformly whether the edit came in as a raw `compose_spec` or was rendered
    from an edited `services` list, since both produce this same shape before
    this point. A no-op for a stack with no prior revision (nothing to restore
    from) or a service the previous spec didn't have."""
    if not isinstance(previous_spec, dict) or not isinstance(new_spec, dict):
        return new_spec
    prev_services = previous_spec.get("services")
    new_services = new_spec.get("services")
    if not isinstance(prev_services, dict) or not isinstance(new_services, dict):
        return new_spec
    for name, svc in new_services.items():
        if not isinstance(svc, dict) or "environment" not in svc:
            continue
        prev_svc = prev_services.get(name)
        if not isinstance(prev_svc, dict):
            continue
        svc["environment"] = _unredact_env(svc["environment"], prev_svc.get("environment"))
    return new_spec


# =============================================================================
# --- edited ServiceSpec[] -> compose_spec (the SAME renderer /assemble uses) ---
# =============================================================================

def render_compose_from_services(db: Session, name: str, services: List[Any]) -> Dict[str, Any]:
    """Render an edited `ServiceSpec` list into a compose_spec, via the exact
    same per-service resolvers + renderer `/assemble` uses (Group B/C source
    resolution). Raises the SAME `HTTPException`s `/assemble` would (duplicate
    name, unresolvable source, ...) — the router lets those propagate."""
    from fastapi import HTTPException
    from routers.persistent_stacks._shared import (
        _resolve_service_fragment, _compose_service_from_spec, _collect_named_volumes,
    )

    rendered: Dict[str, Any] = {}
    for spec in services:
        if spec.name in rendered:
            raise HTTPException(status_code=422,
                                detail=f"duplicate service name {spec.name!r}")
        fragment, _note = _resolve_service_fragment(db, spec)
        rendered[spec.name] = _compose_service_from_spec(spec, fragment)

    compose_spec: Dict[str, Any] = {"services": rendered}
    declared = _collect_named_volumes(rendered)
    if declared:
        compose_spec["volumes"] = {v: {} for v in declared}
    return compose_spec
