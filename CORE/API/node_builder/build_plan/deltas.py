"""wizard-rebuild §7b — plan-delta apply + validation.

The §7 repair flow proposes 1-3 BuildPlan field changes per LLM call
instead of free-form Dockerfile rewrites. This module owns:

  - The allowlist of fields the LLM is permitted to mutate.
  - Per-field validation (delegates to the §0a `llm_input_sandbox`
    validators where they exist; adds plan-specific ones for the
    rest).
  - The apply step: produce a NEW BuildPlan with the delta merged in,
    appended to ``applied_deltas`` for audit + thrash detection.
  - The thrash-detection helper: ``deltas_signature(deltas)``
    canonicalizes a delta-set so the endpoint can compare attempt N's
    proposal to attempt N-1's.

Internal-consistency hard reject:
  - A `language` change paired with `install_steps` that keep the old
    package manager is contradictory (e.g. language=ruby + pip
    install). v1 catches only this one obvious case; broader
    consistency rules can land later.

A successful delta-set application returns the NEW plan; the caller is
expected to re-render the Dockerfile via `render_dockerfile(new_plan)`
and write both `spec.hero_dockerfile` and `spec.build_plan` back.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Fields the LLM is permitted to propose changes for. Every other
# field on BuildPlan is either provenance (extracted_from), state
# (applied_deltas, weak_fields), or schema metadata (mode,
# extractor_version) — none of which an LLM should mutate.
ALLOWED_DELTA_FIELDS = frozenset({
    "language",
    "language_version",
    "package_manager",
    "system_deps",
    "install_steps",
    "env_vars",
    "ports",
    "entrypoint",
    "workdir",
    "from_image_ref",
    "cuda_version",
    "gpu_required",
    # script_body is mutable too (the §6e fix already auto-rewrites
    # the Dockerfile base64 line when this changes via inner repair).
    "script_body",
})


MAX_DELTAS_PER_RESPONSE = 3
MIN_DELTAS_PER_RESPONSE = 1


# ---------------------------------------------------------------------
# Per-field validation
# ---------------------------------------------------------------------

def _validate_field(field: str, value: Any) -> List[str]:
    """Run field-specific validation. Returns list of rejection
    reasons; empty = OK."""
    rejections: List[str] = []
    # Lazy import — the sandbox module already validates these shapes.
    from utils import llm_input_sandbox as _sand

    if field == "install_steps":
        if not isinstance(value, list):
            return ["install_steps must be a list of strings"]
        for i, step in enumerate(value):
            if not isinstance(step, str):
                rejections.append(f"install_steps[{i}] must be a string")
                continue
            if not _sand.validate_install_step(step):
                rejections.append(
                    f"install_steps[{i}]={step!r} fails allowlist "
                    f"(unknown prefix or shell metachar)"
                )
        return rejections

    if field == "system_deps":
        if not isinstance(value, list):
            return ["system_deps must be a list of strings"]
        for i, dep in enumerate(value):
            if not isinstance(dep, str):
                rejections.append(f"system_deps[{i}] must be a string")
                continue
            if not _sand.validate_system_dep(dep):
                rejections.append(
                    f"system_deps[{i}]={dep!r} fails Debian-package allowlist"
                )
        return rejections

    if field == "entrypoint":
        if not isinstance(value, list):
            return ["entrypoint must be a list of strings"]
        if not _sand.validate_entrypoint(value):
            return [f"entrypoint={value!r} fails entrypoint allowlist"]
        return rejections

    if field == "env_vars":
        if not isinstance(value, list):
            return ["env_vars must be a list of objects"]
        for i, e in enumerate(value):
            if not isinstance(e, dict):
                rejections.append(f"env_vars[{i}] must be an object")
                continue
            name = e.get("name")
            if not isinstance(name, str) or not _sand.validate_env_var_name(name):
                rejections.append(
                    f"env_vars[{i}].name={name!r} fails env-name allowlist"
                )
        return rejections

    if field == "ports":
        if not isinstance(value, list):
            return ["ports must be a list of ints"]
        for i, p in enumerate(value):
            if not isinstance(p, int):
                rejections.append(f"ports[{i}] must be an int")
                continue
            if not (1 <= p <= 65535):
                rejections.append(f"ports[{i}]={p} out of range")
        return rejections

    if field == "language":
        if not isinstance(value, str) or not value.strip():
            return ["language must be a non-empty string"]
        # Light allowlist — these are the languages our renderer
        # has base images for. A typo would dead-end the build.
        valid = {"python", "node", "javascript", "typescript", "ruby",
                 "go", "rust", "java", "shell", "any"}
        if value.lower() not in valid:
            return [f"language={value!r} not in {sorted(valid)}"]
        return rejections

    if field == "language_version":
        if value is None:
            return rejections
        if not isinstance(value, str):
            return ["language_version must be a string or null"]
        import re as _re
        if not _re.match(r"^[A-Za-z0-9._-]{1,32}$", value):
            return [f"language_version={value!r} fails allowlist"]
        return rejections

    if field == "package_manager":
        if not isinstance(value, str):
            return ["package_manager must be a string"]
        # Same allowlist as install_steps prefixes — these are the
        # managers the renderer knows about.
        valid_managers = {
            "pip", "poetry", "uv", "pipx", "conda",
            "npm", "yarn", "pnpm", "bun",
            "cargo", "go-mod", "gradle", "maven", "bundle", "none",
        }
        if value not in valid_managers:
            return [f"package_manager={value!r} not in allowlist"]
        return rejections

    if field == "cuda_version":
        if value is None:
            return rejections
        if not isinstance(value, str):
            return ["cuda_version must be a string or null"]
        import re as _re
        if not _re.match(r"^\d+\.\d+$", value):
            return [f"cuda_version={value!r} must be MAJOR.MINOR"]
        return rejections

    if field == "gpu_required":
        if not isinstance(value, bool):
            return ["gpu_required must be a bool"]
        return rejections

    if field == "workdir":
        if not isinstance(value, str) or not value.startswith("/"):
            return ["workdir must be an absolute path string"]
        import re as _re
        if not _re.match(r"^/[A-Za-z0-9._/-]{0,255}$", value):
            return [f"workdir={value!r} fails path allowlist"]
        return rejections

    if field == "from_image_ref":
        if not isinstance(value, str) or not value.strip():
            return ["from_image_ref must be a non-empty string"]
        # Light shape check — full docker-ref validation lives in the
        # admission allowlist + image-mode resolver.
        import re as _re
        if not _re.match(r"^[A-Za-z0-9._/:@-]{3,400}$", value):
            return [f"from_image_ref={value!r} fails ref-char allowlist"]
        return rejections

    if field == "script_body":
        if not isinstance(value, str):
            return ["script_body must be a string"]
        # AST-validate Python scripts (same fix as the inner-repair
        # validation in §6e). If we can't tell the script's language
        # from the spec, only AST-check when it's clearly Python.
        if "import " in value or "def " in value or "print(" in value:
            try:
                import ast as _ast
                _ast.parse(value)
            except SyntaxError as exc:
                return [
                    f"script_body fails Python AST parse: {exc.msg!r} "
                    f"at line {exc.lineno}, offset {exc.offset}"
                ]
        return rejections

    # Unknown field — caught by allowlist check before we get here,
    # but defend the contract.
    return [f"unknown delta field {field!r}"]


# ---------------------------------------------------------------------
# Plan-level consistency
# ---------------------------------------------------------------------

def _check_consistency(plan_dict: dict, deltas: List[dict]) -> List[str]:
    """Reject contradictory delta-sets. v1 catches the obvious one:
    `language` change without a matching `package_manager` /
    `install_steps` change. (Switching to Ruby but keeping
    `pip install foo` would build but never run.)"""
    rejections: List[str] = []
    fields_set = {d["field"] for d in deltas if isinstance(d, dict)}
    if "language" in fields_set:
        new_lang = next(
            d["value"] for d in deltas if d.get("field") == "language"
        )
        new_lang = (new_lang or "").lower()
        # If language change to ruby/go/rust/etc., the install_steps
        # should also change away from pip/poetry/etc.
        lang_to_managers = {
            "python":     {"pip", "poetry", "uv", "pipx", "conda"},
            "javascript": {"npm", "yarn", "pnpm", "bun"},
            "typescript": {"npm", "yarn", "pnpm", "bun"},
            "node":       {"npm", "yarn", "pnpm", "bun"},
            "ruby":       {"bundle", "gem"},
            "go":         {"go"},
            "rust":       {"cargo"},
            "java":       {"mvn", "gradle"},
        }
        expected_managers = lang_to_managers.get(new_lang, set())
        if not expected_managers:
            return rejections  # unknown language, can't check
        # Look at install_steps — either from the delta or current plan.
        steps = None
        if "install_steps" in fields_set:
            steps = next(
                d["value"] for d in deltas if d.get("field") == "install_steps"
            )
        else:
            steps = plan_dict.get("install_steps") or []
        if not isinstance(steps, list):
            return rejections
        for step in steps:
            if not isinstance(step, str):
                continue
            first_tok = step.strip().split()[:1]
            if first_tok and first_tok[0] not in expected_managers:
                rejections.append(
                    f"contradictory delta: language={new_lang!r} but "
                    f"install_steps still use {first_tok[0]!r} "
                    f"(expected one of {sorted(expected_managers)})"
                )
                break
    return rejections


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------

def apply_deltas(
    plan_dict: dict,
    deltas: List[dict],
) -> Tuple[Optional[dict], List[str]]:
    """Apply 1-3 deltas to a plan. Returns ``(new_plan_dict, [])`` on
    success or ``(None, [rejection_reasons])`` on failure.

    Validation order:
      1. Shape: list, 1-3 entries, each {field, value, reason}.
      2. Field allowlist (`ALLOWED_DELTA_FIELDS`).
      3. Per-field validators (via _validate_field).
      4. Cross-field consistency (`_check_consistency`).
      5. Pydantic round-trip — confirms the resulting plan still
         parses cleanly. Catches type mismatches that slipped past
         field-level validation.

    On success, the returned dict is mergeable into spec_json:
    ``spec_data["build_plan"] = new_plan_dict``. Each accepted delta
    is appended to ``new_plan_dict["applied_deltas"]`` with an
    ``applied_at`` timestamp.

    Never mutates the input plan_dict; returns a fresh copy.
    """
    from node_builder.build_plan.types import BuildPlan, Delta

    rejections: List[str] = []

    if not isinstance(deltas, list):
        return None, ["deltas must be a list"]
    if len(deltas) < MIN_DELTAS_PER_RESPONSE:
        return None, [f"deltas list must have at least {MIN_DELTAS_PER_RESPONSE} entry"]
    if len(deltas) > MAX_DELTAS_PER_RESPONSE:
        return None, [
            f"too many deltas ({len(deltas)}); max {MAX_DELTAS_PER_RESPONSE} per response"
        ]

    # Shape + allowlist
    cleaned: List[dict] = []
    for i, d in enumerate(deltas):
        if not isinstance(d, dict):
            rejections.append(f"deltas[{i}] must be an object")
            continue
        field = d.get("field")
        if not isinstance(field, str) or field not in ALLOWED_DELTA_FIELDS:
            rejections.append(
                f"deltas[{i}].field={field!r} not in delta allowlist "
                f"(allowed: {sorted(ALLOWED_DELTA_FIELDS)})"
            )
            continue
        if "value" not in d:
            rejections.append(f"deltas[{i}] missing 'value'")
            continue
        reason = d.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            rejections.append(f"deltas[{i}] missing or empty 'reason'")
            continue
        cleaned.append({"field": field, "value": d["value"], "reason": reason})

    if rejections:
        return None, rejections

    # Per-field validators
    for d in cleaned:
        per_field_rej = _validate_field(d["field"], d["value"])
        rejections.extend(per_field_rej)
    if rejections:
        return None, rejections

    # Cross-field consistency
    cons_rej = _check_consistency(plan_dict, cleaned)
    if cons_rej:
        return None, cons_rej

    # Build the new plan
    new_dict = json.loads(json.dumps(plan_dict))  # deep copy via JSON
    now = datetime.now(timezone.utc).isoformat()
    for d in cleaned:
        new_dict[d["field"]] = d["value"]
    # Append to applied_deltas for audit + thrash detection.
    applied = list(new_dict.get("applied_deltas") or [])
    for d in cleaned:
        applied.append({
            "field": d["field"],
            "value": d["value"],
            "reason": d["reason"],
            "applied_at": now,
        })
    new_dict["applied_deltas"] = applied
    # Pydantic round-trip — catches type errors.
    try:
        new_plan = BuildPlan(**new_dict)
    except Exception as exc:
        return None, [f"applied plan fails BuildPlan validation: {exc}"]
    # Re-dump from the model to normalize defaults.
    return new_plan.model_dump(mode="python"), []


# ---------------------------------------------------------------------
# Thrash detection
# ---------------------------------------------------------------------

def deltas_signature(deltas: List[dict]) -> str:
    """Canonical signature for a delta-set, used to detect thrash.

    Two delta-sets that target the same fields with the same values
    have the same signature regardless of order. The `reason` text
    is excluded — the LLM often paraphrases when re-proposing the
    same fix, and that doesn't make it a different fix.
    """
    if not deltas:
        return ""
    norm: List[dict] = []
    for d in deltas:
        if not isinstance(d, dict):
            continue
        norm.append({
            "field": d.get("field"),
            "value": d.get("value"),
        })
    norm.sort(key=lambda x: x.get("field") or "")
    return json.dumps(norm, sort_keys=True, default=str)
