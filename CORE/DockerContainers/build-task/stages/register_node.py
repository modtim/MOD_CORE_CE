"""B4 — register the NodeDefinition row at the end of the build pipeline.

Calls the API's existing register-node helper (or finalize-registration
when the build was deferred for γ approval). All variants pushed +
sample-test passed → safe to write the NodeDefinition.
"""
from __future__ import annotations

import json
import sys

import requests

from _common import env, fetch_build_spec, log, report_back


def compose_gpu_variants(
    *,
    spec: dict,
    prefix: str,
    image_name: str,
    hero_ref: str,
) -> dict:
    """Map every successfully-built variant key → its full image ref.

    Used by `POST /v1/nodes/` so the NodeDefinition row can:
      * route the node to a worker with the right backend (cuda /
        intel / rocm / npu / cpu) via the scheduler's variants_guard;
      * be matched by the Tenant Registry soft-delete cascade-hide
        (falls back to executable_path when this is empty, but
        per-variant fidelity beats the fallback);
      * surface per-variant verified/pending/failed dots in the
        WorkflowBuilder UI.

    Hero (the `:latest` tag with no suffix) gets the key from
    ``spec["hero_variant"]`` — defaults to "cpu" for non-ML wizard
    builds. Every other variant in ``spec["variants"]`` was filtered
    to "compatible" by ``build_dag/adapter.py:48`` BEFORE the spec
    was persisted, so everything in this list got built + pushed.

    Tag convention matches push_one_variant.py:
        hero  → ``{prefix}{image_name}:latest``
        other → ``{prefix}{image_name}:latest-{variant_key}``

    Extracted as a pure function for unit testing — register_node's
    main() does I/O (env / requests / report_back) and is awkward
    to mock; this helper isolates the only meaningful logic.
    """
    hero_variant_key = (spec.get("hero_variant") or "").strip() or "cpu"
    gpu_variants: dict = {hero_variant_key: hero_ref}
    for v in spec.get("variants") or []:
        if not isinstance(v, dict):
            continue
        variant_key = (v.get("variant") or "").strip()
        if not variant_key or variant_key == hero_variant_key:
            continue
        gpu_variants[variant_key] = f"{prefix}{image_name}:latest-{variant_key}"
    return gpu_variants


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))

    log(f"[register_node] spec={spec_id}")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    if spec.get("defer_registration"):
        log("[register_node] defer_registration=true; skipping. Wizard will finalize.")
        report_back(api_url, api_key, spec_id=spec_id, stage="register",
                    variant=None, success=True,
                    payload={"deferred": True})
        return 0

    # Build the NodeDefinition payload from the spec.
    image_name = spec["image_name"]
    # Tenant Harbor admission requires the full host/project/repo ref,
    # not the bare image_name. Mirror the prefix the build/push stages
    # tagged the image with: ${HARBOR_URL}/tenant-{tenant_id}/{image}.
    harbor_url = env("HARBOR_URL", default="").strip()
    tenant_id = spec.get("tenant_id", 0)
    prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
    executable_path = f"{prefix}{image_name}:latest"

    gpu_variants = compose_gpu_variants(
        spec=spec, prefix=prefix, image_name=image_name,
        hero_ref=executable_path,
    )

    # `tenant_id` is included in the payload (alongside the API-key auth
    # in the headers) so the endpoint attributes the new NodeDefinition
    # to the spec's tenant rather than falling back to the caller's
    # `current_user.tenant_id`. With a single platform-internal API key
    # mapped to a fixed service user, omitting this would mis-route
    # every tenant's wizard build into the service user's tenant. The
    # service-account branch in the endpoint requires `tenant_id` to be
    # explicit; humans get their tenant inferred from the JWT.
    # node_kind is the routing key the downstream-dispatch path uses
    # to identify what kind of task to enqueue. System nodes get short
    # kinds (`image_input`, `return_assets_export`); wizard-built
    # tenant nodes derive theirs from the image_name slug. Without
    # this, `_check_and_dispatch_downstream` silently skips any
    # downstream process node (line 189: `if not node_kind: continue`)
    # — observed on workflow_run 14 where PDF-to-PNG converter
    # process-1 was never dispatched and the run "completed" with
    # only the Image Input root having actually executed.
    node_kind = (image_name or "").replace("-", "_").lower()

    # `type` is the WorkflowBuilder palette category. The wizard's
    # smart-category suggester (manual-test fix 2026-05-22) writes
    # `node_type` onto the spec; we propagate it here. Empty / None
    # → fall back to "Custom" for backwards compatibility with
    # specs from before the suggester landed.
    node_type = (spec.get("node_type") or "").strip() or "Custom"

    # User-facing settings. artifact-ingest extracted the script's env-var
    # knobs onto spec["env_vars"] as [{"name","default","description"}];
    # render them as configuration_schema.extra_variables so the node's
    # Settings panel exposes them. Empty → {} (no settings), matching the
    # prior behaviour for nodes that genuinely have no knobs.
    extra_variables = {}
    for ev in (spec.get("env_vars") or []):
        if not isinstance(ev, dict) or not ev.get("name"):
            continue
        name = str(ev["name"])
        extra_variables[name] = {
            "type": "string",
            "default": str(ev.get("default", "")),
            "description": str(ev.get("description") or name),
        }
    configuration_schema = {"extra_variables": extra_variables} if extra_variables else {}
    log(f"[register_node] exposing {len(extra_variables)} setting(s): "
        f"{', '.join(sorted(extra_variables)) or '(none)'}")

    # sz-tcp.5c — the wizard's explicit tier prices (sz-tcp.5b persisted
    # cost_tiers + cost_basis onto build_specs.spec_json). Land them verbatim
    # so the node row carries the wizard-chosen {small,medium,large} map with
    # basis 'manual' instead of utils/node_cost re-deriving 0.5x/1x/2.5x.
    # Absent (legacy specs / no tiers chosen) → neither key, unchanged payload.
    body = {
        "name": spec["node_name"],
        "description": spec.get("description") or "",
        "executable_path": executable_path,
        "gpu_variants": gpu_variants,
        "node_kind": node_kind,
        "input_schema": {"input_type": spec.get("input_type", "image")},
        "output_schema": {"output_type": spec.get("output_type", "image")},
        "configuration_schema": configuration_schema,
        "type": node_type,
        "process_type": spec.get("process_type", "CPU"),
        "cost_in_credits": int(spec.get("cost_in_credits", 1)),
        "container_timeout": int(spec.get("container_timeout", 7200)),
        "tenant_id": int(tenant_id) if tenant_id else None,
        # So the API's credit charge bills the build's triggering user
        # (admin → free) instead of the service account that authenticates
        # this callback.
        "build_spec_id": spec_id,
    }
    _cost_tiers = spec.get("cost_tiers")
    if isinstance(_cost_tiers, dict) and _cost_tiers:
        _tier_map = {
            k: _cost_tiers[k]
            for k in ("small", "medium", "large")
            if k in _cost_tiers
        }
        if _tier_map:
            body["cost_credits_by_tier"] = _tier_map
            _cost_basis = (spec.get("cost_basis") or "").strip()
            if _cost_basis:
                body["cost_basis"] = _cost_basis

    r = requests.post(
        f"{api_url}/v1/nodes/",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    if r.status_code >= 400:
        log(f"[register_node] register failed: {r.status_code} {r.text[:500]}")
        report_back(api_url, api_key, spec_id=spec_id, stage="register",
                    variant=None, success=False,
                    payload={"status": r.status_code, "body": r.text[:500]})
        return 1

    nd = r.json()
    nd_id = nd.get("id")
    log(f"[register_node] registered NodeDefinition id={nd_id}")

    # §5 — backfill node_definition_id on build-time
    # node_variant_verification rows so the UI + reference-variant
    # PATCH endpoint can look them up by Node id. Best-effort; the
    # endpoint is idempotent so a retry is safe.
    if nd_id:
        try:
            fr = requests.post(
                f"{api_url}/v1/woodsman/wizard/finalize-verification",
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json={"build_spec_id": spec_id, "node_definition_id": int(nd_id)},
                timeout=30,
            )
            if fr.status_code == 200:
                log(
                    f"[register_node] finalize-verification "
                    f"promoted={fr.json().get('promoted', 0)} rows"
                )
            else:
                log(
                    f"[register_node] warn: finalize-verification "
                    f"returned {fr.status_code}: {fr.text[:200]}"
                )
        except Exception as exc:
            log(f"[register_node] warn: finalize-verification call failed: {exc}")

    report_back(api_url, api_key, spec_id=spec_id, stage="register",
                variant=None, success=True,
                payload={"node_definition_id": nd_id,
                         "node_name": nd.get("name")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
