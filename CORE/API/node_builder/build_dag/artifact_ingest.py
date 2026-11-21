"""Helper for parsing Containerizer artifact JSON and merging it onto
a BuildSpec.

Pure logic — no orchestrator coupling. Called by:
  - The `system-wizard-ingest-artifacts` build-task stage when it runs
    as a normal node between `generate` and the variant builds.
  - Tests that simulate generate-phase completion.

There used to be a special-case hook in `_check_and_dispatch_downstream`
that did this work. That hook was an orchestrator blemish — it
embedded wizard-specific logic into a generic node-to-node dispatcher,
and a silent failure there had no UI surface (no node to click into).
The hook has been removed; this helper is invoked from a regular
DAG node now, so failures appear in the run timeline like any other
failed node.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple


# Env vars the platform/build system injects — never user-facing node
# settings. Anything a generated script reads that's on this list is
# plumbing, not a knob, and must NOT become a Settings field.
_SYSTEM_ENV_DENYLIST = frozenset({
    "API_KEY", "API_URL", "TENANT_ID", "BUILD_SPEC_ID", "NODE_NAME",
    "IMAGE_NAME", "OUTPUT_DIR", "INPUT_DIR", "OUTPUT_MODE", "STAGE",
    "REGISTRY_TARGET", "LOCAL_REGISTRY", "HARBOR_REGISTRY", "HARBOR_PROJECT",
    "HARBOR_URL", "HARBOR_USERNAME", "HARBOR_PASSWORD",
    "PLATFORM_HARBOR_USERNAME", "PLATFORM_HARBOR_PASSWORD",
    "LLM_API_KEY", "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL",
    "HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE",
    "SENTENCE_TRANSFORMERS_HOME", "TASK_ID", "CODEBASE_WORKSPACE",
    "CODEBASE_SOURCES", "INLINE_SOURCE_BODY", "INLINE_SOURCE_FILENAME",
    "USER_HINTS", "BUILD_VERIFY", "MOD_REGISTRY", "PATH", "PYTHONPATH",
    "CUDA_VISIBLE_DEVICES", "ADDITIONAL_INSTRUCTIONS",
})

# os.getenv("NAME", <default>) / os.environ.get("NAME", <default>) — default optional.
_ENV_READ_RE = re.compile(
    r"""os\.(?:getenv|environ\.get)\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']"""
    r"""\s*(?:,\s*(?P<default>"[^"]*"|'[^']*'|[^),]+))?""",
)
# os.environ["NAME"] — no default (required env).
_ENV_INDEX_RE = re.compile(r"""os\.environ\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]""")


def _strip_quotes(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        return s[1:-1]
    return s


def extract_env_settings_from_artifacts(artifacts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Scan the generated script(s) for env-var reads and return the
    user-facing settings as ``[{"name","default","description"}]``.

    Deterministic (regex over the actual source), so it gets the REAL
    defaults the script uses — not an LLM guess. Descriptions are enriched
    from the Containerizer's ``analysis.env_vars`` when present. Platform
    plumbing (``_SYSTEM_ENV_DENYLIST``) is excluded. Order preserved.
    """
    texts: List[str] = []
    for f in (artifacts.get("source_files") or []):
        if isinstance(f, dict) and isinstance(f.get("body"), str):
            texts.append(f["body"])
    if isinstance(artifacts.get("entrypoint_sh"), str):
        texts.append(artifacts["entrypoint_sh"])
    blob = "\n".join(texts)

    desc_by_name: Dict[str, str] = {}
    analysis = artifacts.get("analysis") or {}
    for ev in (analysis.get("env_vars") or []):
        if isinstance(ev, dict) and ev.get("name"):
            desc_by_name[str(ev["name"])] = str(ev.get("description") or "")

    found: Dict[str, str] = {}
    order: List[str] = []
    for m in _ENV_READ_RE.finditer(blob):
        name = m.group(1)
        if name in _SYSTEM_ENV_DENYLIST or name in found:
            continue
        found[name] = _strip_quotes(m.group("default")) if m.group("default") else ""
        order.append(name)
    for m in _ENV_INDEX_RE.finditer(blob):
        name = m.group(1)
        if name in _SYSTEM_ENV_DENYLIST or name in found:
            continue
        found[name] = ""
        order.append(name)

    return [
        {"name": n, "default": found[n], "description": desc_by_name.get(n, "")}
        for n in order
    ]


# Fields the generate-phase artifact JSON carries that the BuildSpec
# stores under the same name. `dockerfile` is the exception: BuildSpec
# names it `hero_dockerfile` (every downstream node reads that field),
# so we translate at ingest time.
_PASS_THROUGH_KEYS = (
    "entrypoint_sh",
    "build_and_push_sh",
    "dockerfile_variants",
    "gpu_variants",
    "hero_variant",
    "source_variants",
    "variant_adaptations",
    "source_files",
    "port_plans",
    "port_incompatible_reasons",
    "image_name",
)


def _sanitize_image_name(name: str, fallback: str) -> str:
    """Coerce a Containerizer-produced `image_name` into a clean
    registry-friendly slug. The codebase pipeline's LLM occasionally
    emits a full image ref (e.g. ``registry:5000/black_formatter``)
    instead of just the repo segment, which the downstream
    test_and_repair stage then concatenates with the harbor prefix
    to produce an invalid tag like
    ``example.com/tenant-1/registry:5000/black_formatter:latest``
    that docker rejects with ``invalid reference format``.

    Strip any host/port/path prefix down to the last path component,
    drop any trailing ``:tag``, then lowercase + collapse non-
    alphanumeric runs to ``-``. Empty result falls back to the
    caller-provided value (typically the spec's original
    ``image_name`` slug).
    """
    import re as _re
    s = (name or "").strip()
    # If the LLM emitted "host/path/repo:tag", keep only the final
    # repo component, then drop ":tag".
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.split(":", 1)[0]
    s = s.lower().strip()
    s = _re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or (fallback or "node")


def merge_artifacts_into_spec_json(
    spec_json: Dict[str, Any],
    artifacts: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a new spec_json dict with artifact fields merged in.

    Non-destructive on the input — returns a new dict. The caller is
    responsible for persisting back to the database.
    """
    out = dict(spec_json or {})
    if "dockerfile" in artifacts:
        # Translate to the platform-canonical field name.
        out["hero_dockerfile"] = artifacts["dockerfile"]
    for key in _PASS_THROUGH_KEYS:
        if key in artifacts:
            if key == "image_name":
                # Sanitize. Codebase-pipeline (Git mode) sometimes
                # produces a full image ref here; keep only the slug
                # segment so test_and_repair's `<prefix>/<image_name>:latest`
                # produces a valid tag. Use the existing spec image_name
                # as the fallback when the artifact yields nothing usable.
                out[key] = _sanitize_image_name(
                    artifacts[key],
                    fallback=(spec_json or {}).get("image_name", ""),
                )
            else:
                out[key] = artifacts[key]

    # Propagate the Containerizer's detected runtime egress needs
    # (analysis.allowed_egress = [{host, ports, reason}, ...]) onto the spec.
    # Without this the signal is DETECTED but DROPPED before the build stages,
    # so a node that legitimately fetches deps at runtime (git clone / pip from
    # a URL / API call) fails its smoke/repair probe on an isolated
    # --network=none with a DNS error the LLM can't fix. The probes read
    # spec.allowed_egress to route to the egress network — see
    # test_and_repair_one_variant / smoke_one_variant.
    _analysis = artifacts.get("analysis") or {}
    _egress = _analysis.get("allowed_egress")
    if _egress:
        out["allowed_egress"] = _egress

    # Translate the Containerizer's `dockerfile_variants` dict into the
    # typed `variants` list build_one_variant.py reads. Without this
    # translation, non-hero variant build tasks fetch the spec and find
    # spec["variants"] == [] — they can't locate their dockerfile and
    # exit with "variant 'cuda' not found in spec". The legacy
    # pre-generated-artifacts path populated spec.variants directly via
    # apply_variant_fanout; the inputs-only path needs this translation.
    dfv = artifacts.get("dockerfile_variants") or out.get("dockerfile_variants")
    if isinstance(dfv, dict) and dfv:
        hero_variant = (
            artifacts.get("hero_variant")
            or out.get("hero_variant")
            or (spec_json or {}).get("hero_variant")
            or ""
        ).lower()
        variant_adaptations = artifacts.get("variant_adaptations") or out.get("variant_adaptations") or {}
        existing_variants = {v.get("variant"): v for v in (out.get("variants") or []) if isinstance(v, dict)}
        translated: list[Dict[str, Any]] = []
        for variant_key, dockerfile in dfv.items():
            if not variant_key or not isinstance(dockerfile, str):
                continue
            # Skip the hero variant — it lives in spec.hero_dockerfile, not in spec.variants.
            if variant_key.lower() == hero_variant:
                continue
            entry = dict(existing_variants.get(variant_key, {}))
            entry["variant"] = variant_key
            entry["dockerfile"] = dockerfile
            # Carry adaptations through so the variant build can honor
            # incompatible markings.
            adaptation = variant_adaptations.get(variant_key) if isinstance(variant_adaptations, dict) else None
            if isinstance(adaptation, dict):
                entry["adaptations"] = adaptation
            translated.append(entry)
        if translated:
            out["variants"] = translated

    # User-facing node settings: extract env-var knobs from the generated
    # script so register_node can render them as configuration_schema.
    # extra_variables. Without this the analyzed env vars were dropped here
    # and every wizard node shipped with an empty Settings panel.
    env_settings = extract_env_settings_from_artifacts(artifacts)
    if env_settings:
        out["env_vars"] = env_settings
    return out


def apply_clone_bounds(artifacts: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """GSEC R-100 — deterministic clone-bounds admission over generated
    Dockerfiles in an artifact bundle.

    Rewrites every unbounded ``git clone`` in the hero Dockerfile and each
    variant Dockerfile to a shallow partial clone with LFS smudge skipped,
    so a large/malicious upstream repo cannot hang the build container or
    exhaust its disk. Returns ``(new_artifacts, changes, rejections)`` —
    ``rejections`` is non-empty when a construct cannot be safely bounded
    (``--unshallow`` / ``--no-depth``); in that case ``new_artifacts`` is the
    ORIGINAL dict (fail-closed) and the caller should reject the ingest with
    the reason.

    Pure + non-destructive: returns a new dict, never mutates the input.
    Bundles with no git clones pass through byte-for-byte (plus a dict copy).
    """
    from node_builder import clone_bounds  # local import: keep module import fast

    if not isinstance(artifacts, dict):
        return artifacts, [], []
    out = dict(artifacts)
    changes: List[str] = []
    rejections: List[str] = []

    def _bound(key: str, value: Any, label: str) -> None:
        if not isinstance(value, str) or not value or "git" not in value:
            return
        new_value, key_changes, key_rej = clone_bounds.normalize_dockerfile(value)
        if key_rej:
            rejections.extend(f"{label}: {r}" for r in key_rej)
            return
        out[key] = new_value
        changes.extend(f"{label}: {c}" for c in key_changes)

    _bound("dockerfile", artifacts.get("dockerfile"), "dockerfile")
    dfv = artifacts.get("dockerfile_variants")
    if isinstance(dfv, dict) and dfv:
        variants = dict(dfv)
        for variant_key, dockerfile in dfv.items():
            if not isinstance(dockerfile, str) or not dockerfile or "git" not in dockerfile:
                continue
            new_value, key_changes, key_rej = clone_bounds.normalize_dockerfile(dockerfile)
            if key_rej:
                rejections.extend(
                    f"dockerfile_variants[{variant_key!r}]: {r}" for r in key_rej
                )
                return artifacts, changes, rejections
            variants[variant_key] = new_value
            changes.extend(
                f"dockerfile_variants[{variant_key!r}]: {c}" for c in key_changes
            )
        out["dockerfile_variants"] = variants
    if rejections:
        # Fail-closed: hand back the original so the caller rejects on the
        # reason instead of persisting a half-rewritten bundle.
        return artifacts, changes, rejections
    return out, changes, []


def admit_clone_bounds(artifacts: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Admission-gate form of :func:`apply_clone_bounds`.

    Returns ``(bounded_artifacts, changes)`` or raises
    ``clone_bounds.CloneBoundsRejection`` with a clear operator-facing reason
    (the ingest endpoint turns that into a 400).
    """
    from node_builder import clone_bounds

    out, changes, rejections = apply_clone_bounds(artifacts)
    if rejections:
        raise clone_bounds.CloneBoundsRejection(rejections[0])
    return out, changes


def normalize_artifact_bundle(artifacts: Any) -> Any:
    """Flatten the nested-wrapper shape some weak LLM backends emit.

    granite-2b (and other small models) sometimes return the whole artifact
    object as a JSON STRING stuffed into the ``dockerfile`` field, leaving the
    sibling top-level keys empty::

        {"dockerfile": "{\\"dockerfile\\": \\"FROM ...\\",
                         \\"entrypoint_sh\\": \\"#!/bin/bash ...\\",
                         \\"build_and_push_sh\\": \\"...\\"}",
         "entrypoint_sh": "", "build_and_push_sh": "..."}

    ``validate_artifacts`` then rejects the bundle for a missing ``entrypoint_sh``
    even though the real one is present, just nested. This mirrors the tolerance
    ``node_definitions._extract_albedo_artifacts`` already applies on the
    log-scrape path, at the ingest path too.

    IMPORTANT — no-op for a well-formed (flat) bundle, which is the common case:
    a real Dockerfile begins with a directive/comment (``FROM`` / ``#``), never a
    JSON object, so the guard below returns the input untouched unless the
    ``dockerfile`` field itself parses to a dict carrying a nested ``dockerfile``
    key. When it does unwrap, a non-empty top-level script value is preferred over
    the nested one (never clobbered); only genuinely-empty fields are filled.
    """
    if not isinstance(artifacts, dict):
        return artifacts
    df = artifacts.get("dockerfile")
    if not isinstance(df, str):
        return artifacts
    s = df.strip()
    # Fast reject: a real Dockerfile never starts with a JSON object. This is
    # what keeps flat bundles completely untouched.
    if not (s.startswith("{") and '"dockerfile"' in s):
        return artifacts
    try:
        inner = json.loads(s)
    except (ValueError, TypeError):
        return artifacts
    if not isinstance(inner, dict) or not isinstance(inner.get("dockerfile"), str):
        return artifacts

    # Confirmed wrapper shape. Flatten it.
    merged = dict(artifacts)
    merged["dockerfile"] = inner["dockerfile"]  # the top-level was the wrapper
    for k in ("entrypoint_sh", "build_and_push_sh"):
        top = merged.get(k)
        if isinstance(top, str) and top.strip():
            continue  # keep a real top-level value — never clobber it
        iv = inner.get(k)
        if isinstance(iv, str) and iv.strip():
            merged[k] = iv
    # Carry through inner-only metadata (image_name, etc.) the top level lacks.
    for k, v in inner.items():
        if k in ("dockerfile", "entrypoint_sh", "build_and_push_sh"):
            continue
        cur = merged.get(k)
        if cur in (None, "", [], {}):
            merged[k] = v
    return merged


def validate_artifacts(artifacts: Any) -> Tuple[bool, str]:
    """Check that an artifact dict has the minimum required fields for
    downstream nodes to consume. Returns (ok, reason)."""
    if not isinstance(artifacts, dict):
        return False, f"artifacts is {type(artifacts).__name__}, expected dict"
    required = ("dockerfile", "entrypoint_sh", "build_and_push_sh")
    missing = [k for k in required if not artifacts.get(k)]
    if missing:
        return False, f"missing required keys: {', '.join(missing)}"
    return True, "ok"
