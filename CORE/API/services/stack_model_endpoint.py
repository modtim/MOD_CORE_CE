"""CE-safe stack model resolution — served names + reachable base URL.

GX.S5 (`/v1/llm/chat/completions`) and n7 ("Stack model call") resolve a
tenant's model against the tenant's own Persistent Stacks. That logic used to
live in two non-CE modules (`services.llm_idle.served_models`,
`routers.ai_config._stack_custom_base_url`), so on CE every model call 404ed
even when the tenant had a running stack serving it.

The logic itself is pure: it reads only the stack row's own fields
(`compose_spec`, `mesh_endpoints`, `lb_endpoint`, `target_worker`) plus the
target worker's `hardware_specs.advertise_address`. Nothing here is edition
dependent, so it now has ONE home — this file — and both editions import it.
`services.llm_idle` and `routers.ai_config` keep their names as thin
re-exports/delegates so every existing caller (and test) is unchanged.

Imports: `models` + stdlib only. No denied module is imported here.
"""
from __future__ import annotations

from typing import List, Optional

import models


def served_models(stack) -> List[str]:
    """Model names a stack serves, from its compose command.

    Parsed from `--served-model-name`, which is exactly what the API sends as
    the `model` field, so this is the join key to the usage signal by
    construction. Falls back to `--model` (the HF repo) when no served name is
    declared, because vLLM then serves under the repo id.
    """
    out: List[str] = []
    services = ((stack.compose_spec or {}).get("services") or {})
    for svc in services.values():
        cmd = (svc or {}).get("command") or []
        if isinstance(cmd, str):
            cmd = cmd.split()
        for flag in ("--served-model-name", "--model"):
            if flag in cmd:
                i = cmd.index(flag)
                if i + 1 < len(cmd):
                    out.append(str(cmd[i + 1]))
                    break
    return out


def served_model_ref(compose_spec: Optional[dict]) -> Optional[str]:
    """The model reference a compose spec serves, read off `x-mod.served.model`
    — the same field `managed_ai_wanted_models` reads. First service that
    declares one; the generator and reasoner specs each declare exactly one
    service, so there is never a second to disagree with the first.
    """
    for svc in ((compose_spec or {}).get("services") or {}).values():
        ref = ((svc.get("x-mod") or {}).get("served") or {}).get("model")
        if ref:
            return ref
    return None


def _service_port(spec) -> int:
    cmd = (spec or {}).get("command") or []
    if isinstance(cmd, str):
        cmd = cmd.split()
    if "--port" in cmd:
        i = cmd.index("--port")
        if i + 1 < len(cmd):
            try:
                return int(cmd[i + 1])
            except (TypeError, ValueError):
                pass
    return 8000


def custom_base_url(st, db) -> Optional[str]:
    """A reachable base URL for a NON-managed stack: the mesh MagicDNS name of
    the service that serves the model, else the legacy published port, else
    the hosting worker's advertised address. No new probing — the reachability
    verdict reuses `_host_resolves` on whatever this returns.
    """
    served = served_models(st)
    mesh = st.mesh_endpoints or {}
    chosen = None
    port = 8000
    for svc, spec in ((st.compose_spec or {}).get("services") or {}).items():
        ep = mesh.get(svc)
        if not (isinstance(ep, dict) and ep.get("magicdns_name")):
            continue
        if served_model_ref({"services": {svc: spec}}) or \
                any(n in ((spec or {}).get("command") or []) for n in served):
            chosen = ep["magicdns_name"]
            port = _service_port(spec)
            break
        if chosen is None:
            chosen = ep["magicdns_name"]
            port = _service_port(spec)
    if chosen:
        return f"http://{chosen}:{port}"
    # Legacy: published port on the worker host.
    if st.lb_endpoint:
        host, _, port = st.lb_endpoint.partition(":")
        if host:
            return f"http://{host}:{port or 8000}"
    if not st.target_worker:
        return None
    try:
        worker = (
            db.query(models.WorkerLicense)
            .filter(models.WorkerLicense.worker_name == st.target_worker)
            .first()
        )
        addr = ((worker.hardware_specs or {}).get("advertise_address")
                if worker is not None else None)
    except Exception:  # noqa: BLE001
        addr = None
    if addr:
        return f"http://{addr}:8000"
    return None
