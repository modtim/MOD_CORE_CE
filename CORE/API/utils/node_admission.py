"""Layer-1 admission control for user-submitted nodes.

Called from every creation endpoint in routers/node_definitions.py. Rejects
submissions at the door so the imagebuilder task never dispatches (no credit
charge, no compute wasted) when the submission violates the platform policy.

SaaS-only. On-prem deployments (`DEPLOYMENT_MODE` != "saas") short-circuit
to pass — the operator is the trusted party and can ship whatever they want.
Platform admins can bypass via `_force` even in SaaS (audit-logged).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from utils.registries import (
    DEFAULT_PLATFORM_PROJECT,
    DEFAULT_PLATFORM_REGISTRY,
    platform_project,
    platform_registry,
)

logger = logging.getLogger(__name__)


DEFAULT_ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "base_image_allowlist.json"


@dataclass
class AdmissionResult:
    """Outcome of an admission check. `passed` is the only field callers
    should branch on; `reason` is a human-readable string for 422 detail
    and audit logs; `rule` identifies which rule fired for metrics."""
    passed: bool
    reason: str = ""
    rule: str = ""

    @classmethod
    def ok(cls) -> "AdmissionResult":
        return cls(passed=True)

    @classmethod
    def fail(cls, rule: str, reason: str) -> "AdmissionResult":
        return cls(passed=False, rule=rule, reason=reason)


def _is_saas() -> bool:
    """Mirror of routers.node_definitions._is_saas. Duplicated here so the
    admission module has no reverse dependency on the router."""
    return os.getenv("DEPLOYMENT_MODE", "onprem").lower() == "saas"


@lru_cache(maxsize=1)
def _load_allowlist_cached(path: str) -> dict:
    with open(path, "r") as f:
        return json.load(f)


def load_allowlist(path: Optional[Path] = None) -> dict:
    """Load the policy JSON. Cached so the hot path doesn't re-read on
    every admission. Call clear_allowlist_cache() to reload after an edit
    (exposed for the hot-reload endpoint in the plan's Layer-1 bullet)."""
    p = str(path or DEFAULT_ALLOWLIST_PATH)
    return _load_allowlist_cached(p)


def clear_allowlist_cache() -> None:
    _load_allowlist_cached.cache_clear()


_FROM_RE = re.compile(r"^\s*FROM\s+(\S+)(?:\s+AS\s+\S+)?\s*$", re.MULTILINE | re.IGNORECASE)


def extract_from_lines(dockerfile_text: str) -> list[str]:
    """Every FROM line's image ref, in source order. Multi-stage builds
    can reference intermediate stages by name (e.g. `FROM builder`) —
    those don't need allowlist validation because they reference an
    earlier stage, not a pulled image. Caller separates by checking
    whether the ref appears earlier as an `AS <name>` target."""
    return _FROM_RE.findall(dockerfile_text or "")


def _is_stage_alias(ref: str, all_froms: Iterable[str], dockerfile_text: str) -> bool:
    # If `FROM <base> AS <name>` was seen earlier and `ref` == `<name>`,
    # this FROM is referring to a prior stage, not a registry image.
    stage_names = set(re.findall(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", dockerfile_text or "", re.MULTILINE | re.IGNORECASE))
    return ref in stage_names


def _base_image_allowed(ref: str, allowlist: dict, tenant_grants: Optional[list[str]] = None,
                        strict: bool = False) -> bool:
    # Relaxed-allowlist mode (SaaS + WIZARD_ALLOW_ANY_IMAGE=true) lets
    # users pick any well-formed base. The Containerizer's prompt
    # constraint still steers the LLM toward a curated base when no
    # explicit user choice was made, so this only kicks in when a user
    # specifically asked for a non-curated image.
    # `strict=True` opts out of the relaxation entirely (used by the external
    # node-authoring path, which must always enforce the allowlist).
    if not strict and _is_saas() and os.getenv("WIZARD_ALLOW_ANY_IMAGE", "true").lower() == "true":
        return _looks_like_image_ref(ref)

    prefixes: list[str] = list(allowlist.get("allowed_from_prefixes") or [])
    if tenant_grants:
        prefixes.extend(tenant_grants)
    # CE mirrors MOD's curated base images into its own public registry
    # (registries.py resolves `platform_image('base-...')` there via the
    # CE_IMAGE_REGISTRY fallback). The JSON allowlist is seeded for paid
    # Harbor only, so derive the CE prefix the same way registries.py does
    # — same TENANT_HARBOR-derivation pattern as validate_base_image_ref.
    # Only the curated `base-` namespace is admitted, never the whole
    # registry, and nothing is added when the registry is unset/empty.
    ce_base_prefix = _ce_base_prefix()
    if ce_base_prefix:
        prefixes.append(ce_base_prefix)
    for prefix in prefixes:
        if ref.startswith(prefix):
            return True
    return False


def _ce_base_prefix() -> str:
    """`<registry>/<project>/base-` when the deploy resolves platform images
    through a non-default (CE) registry, else ""."""
    registry = platform_registry()
    project = platform_project()
    if not registry or not project:
        return ""
    if registry == DEFAULT_PLATFORM_REGISTRY and project == DEFAULT_PLATFORM_PROJECT:
        return ""
    return f"{registry.rstrip('/')}/{project.strip('/')}/base-"


_IMAGE_REF_SYNTAX_RE = re.compile(
    # Loose Docker image ref shape: optional registry host with dot/colon/port,
    # path segments, optional tag/digest. Catches obvious garbage (empty,
    # whitespace, control chars) without being a full Docker validator.
    r"^[a-z0-9][a-z0-9._\-/:@]{1,254}$",
    re.IGNORECASE,
)


def _looks_like_image_ref(ref: str) -> bool:
    """Minimal syntactic check used in relaxed-allowlist mode. Rejects
    empty strings, whitespace, and refs with characters that would never
    appear in a valid Docker image reference. Not a full parse — just
    enough to refuse `''`, `'   '`, and `'rm -rf /'`."""
    if not ref or not ref.strip():
        return False
    return bool(_IMAGE_REF_SYNTAX_RE.match(ref.strip()))


def validate_base_image_ref(
    image_ref: str,
    *,
    allowlist: Optional[dict] = None,
    tenant_grants: Optional[list[str]] = None,
    strict: bool = False,
) -> AdmissionResult:
    """Image mode — validate a pre-built image reference.

    Two modes:

    1. **Strict allowlist** (default): the ref must start with one of
       `allowed_image_prefixes_for_image_mode` (Harbor mod_core + internal
       tenant registry). Used for on-prem deployments and for SaaS when
       the operator wants to lock down what tenants can run.

    2. **Relaxed (SaaS + WIZARD_ALLOW_ANY_IMAGE=true)**: any syntactically
       valid image ref passes. Lets tenants pull community images
       (HuggingFace, NVIDIA NGC, etc.) directly instead of forcing them
       through the curated-base + wrapper-script flow. The build still
       runs Trivy at push time, so vulnerabilities surface — they just
       surface later. Per Tim's feedback 2026-05-20: 'why does it have
       to be on the allow list? why can't we just use any docker
       container?'

    `strict=True` disables mode 2: the ref must always match an allowlisted
    prefix regardless of WIZARD_ALLOW_ANY_IMAGE. The external node-authoring
    path uses this so externally-submitted images cannot ride the relaxation.
    """
    if not strict and _is_saas() and os.getenv("WIZARD_ALLOW_ANY_IMAGE", "true").lower() == "true":
        if _looks_like_image_ref(image_ref):
            return AdmissionResult.ok()
        return AdmissionResult.fail(
            "image-ref-malformed",
            f"Image reference {image_ref!r} is empty or syntactically invalid.",
        )

    al = allowlist or load_allowlist()
    prefixes: list[str] = list(al.get("allowed_image_prefixes_for_image_mode") or [])
    if tenant_grants:
        prefixes.extend(tenant_grants)
    # Auto-allow whatever tenant-harbor URL the deployment is configured
    # to push to. The wizard's build_and_register_local pushes images to
    # ``<TENANT_HARBOR_EXTERNAL_URL>/tenant-<id>/<image>:<tag>``; the
    # registration step then submits that exact ref back here. Hardcoding
    # the host (e.g. ``example.com/tenant-``) in
    # base_image_allowlist.json drifts every time a deployment renames
    # or moves its tenant Harbor — derive instead from env so each
    # deployment self-configures. Strip protocol so we can use prefix
    # matching against ``host/tenant-...``.
    tenant_harbor_url = (os.getenv("TENANT_HARBOR_EXTERNAL_URL") or "").strip()
    if tenant_harbor_url:
        host = tenant_harbor_url.split("://", 1)[-1].rstrip("/")
        if host:
            prefixes.append(f"{host}/tenant-")
    for p in prefixes:
        if image_ref.startswith(p):
            return AdmissionResult.ok()
    return AdmissionResult.fail(
        "image-ref-not-allowlisted",
        f"Image reference {image_ref!r} does not start with any allowlisted prefix. "
        f"Allowed: {prefixes}",
    )


def validate_dockerfile(
    dockerfile_text: str,
    *,
    allowlist: Optional[dict] = None,
    tenant_grants: Optional[list[str]] = None,
    strict: bool = False,
) -> AdmissionResult:
    """Dockerfile / Git / Script modes — validate every FROM against the
    curated-base allowlist and scan the body for forbidden patterns.

    `strict=True` forces FROM validation against the allowlist even when
    WIZARD_ALLOW_ANY_IMAGE relaxation is on (external node-authoring path)."""
    if not (dockerfile_text or "").strip():
        return AdmissionResult.fail("empty-dockerfile", "Dockerfile is empty")

    al = allowlist or load_allowlist()
    froms = extract_from_lines(dockerfile_text)
    if not froms:
        return AdmissionResult.fail(
            "no-from-directive",
            "Dockerfile has no FROM directive",
        )

    for ref in froms:
        if _is_stage_alias(ref, froms, dockerfile_text):
            continue  # multi-stage intra-build reference; not a pull
        if not _base_image_allowed(ref, al, tenant_grants=tenant_grants, strict=strict):
            return AdmissionResult.fail(
                "base-image-not-allowlisted",
                f"Base image {ref!r} is not in the curated allowlist. "
                f"Use one of: {al.get('allowed_from_prefixes')}",
            )

    for rule in al.get("forbidden_dockerfile_patterns", []):
        # The historical "USER-root-final" regex was
        #   (?mi)^\s*USER\s+(root|0)\s*$
        # which rejects ANY `USER root` line — including the legitimate
        # build-time pattern of switching to root for apt-get/pip and
        # dropping back to mod before the entrypoint. Skip the regex
        # for this rule; the structured check below handles "final USER"
        # correctly by reading the LAST USER directive.
        if (rule.get("name") or "") == "USER-root-final":
            continue
        try:
            pattern = re.compile(rule["pattern"])
        except re.error as e:
            logger.warning("Bad regex in admission allowlist rule %s: %s", rule.get("name"), e)
            continue
        if pattern.search(dockerfile_text):
            return AdmissionResult.fail(
                rule.get("name") or "forbidden-pattern",
                rule.get("reason") or f"Dockerfile contains forbidden pattern: {rule['pattern']}",
            )

    # Structured check: the FINAL USER directive must be non-root.
    # Build-time USER root is fine — many curated bases set USER mod
    # for runtime hardening, and the wizard's hero post-process
    # injects USER root for build-time apt then USER mod before the
    # entrypoint. The runtime user is what matters; intermediate
    # USER root lines are normal.
    final_user_check = _check_final_user_nonroot(dockerfile_text)
    if final_user_check is not None:
        return final_user_check

    return AdmissionResult.ok()


_USER_DIRECTIVE_RE = re.compile(
    r"^\s*USER\s+(?P<user>\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _check_final_user_nonroot(dockerfile_text: str) -> Optional[AdmissionResult]:
    """Return a failing AdmissionResult iff the LAST ``USER`` directive
    in the Dockerfile is root (``root`` or numeric ``0``). Returns
    ``None`` (pass) when there's no USER directive at all OR the last
    one is non-root.
    """
    matches = list(_USER_DIRECTIVE_RE.finditer(dockerfile_text))
    if not matches:
        return None  # no USER directive — base image's runtime user prevails
    last = matches[-1].group("user").strip()
    if last in ("root", "0"):
        return AdmissionResult.fail(
            "USER-root-final",
            "Final USER must be non-root. If you need root at build time, "
            "drop back to non-root before the last directive.",
        )
    return None


def validate_git_url(url: str, *, allowlist: Optional[dict] = None) -> AdmissionResult:
    al = allowlist or load_allowlist()
    patterns = al.get("allowed_git_host_patterns") or []
    for pat in patterns:
        try:
            if re.match(pat, url or ""):
                return AdmissionResult.ok()
        except re.error:
            continue
    return AdmissionResult.fail(
        "git-host-not-allowlisted",
        f"Git URL {url!r} does not match any allowlisted host pattern. "
        f"Use github.com, gitlab.com, bitbucket.org, or codeberg.org.",
    )


def validate_script(
    interpreter: str,
    body: str,
    *,
    allowlist: Optional[dict] = None,
) -> AdmissionResult:
    al = allowlist or load_allowlist()
    allowed = al.get("allowed_script_interpreters") or []
    if (interpreter or "").strip() not in allowed:
        return AdmissionResult.fail(
            "script-interpreter-not-allowlisted",
            f"Interpreter {interpreter!r} not allowed. Use one of: {allowed}",
        )
    # Reuse the Dockerfile forbidden-pattern list for script bodies too —
    # curl|sh and friends are just as bad in a script body as in a Dockerfile.
    for rule in al.get("forbidden_dockerfile_patterns", []):
        try:
            pattern = re.compile(rule["pattern"])
        except re.error:
            continue
        if pattern.search(body or ""):
            return AdmissionResult.fail(
                "script-" + (rule.get("name") or "forbidden-pattern"),
                "Script body " + (rule.get("reason") or "contains a forbidden pattern"),
            )
    return AdmissionResult.ok()


def validate_for_mode(
    mode: str,
    payload: dict,
    *,
    tenant_id: Optional[int],
    is_platform_admin: bool = False,
    is_service_account: bool = False,
    force_admission: bool = False,
    tenant_grants: Optional[list[str]] = None,
    strict: bool = False,
) -> AdmissionResult:
    """Dispatch to the per-mode validator. Short-circuits to pass when:
    - Running on-prem (`DEPLOYMENT_MODE` != "saas")
    - `tenant_id` is None (system node, dispatched by core MOD code)
    - `is_service_account` (the internal api_service registers nodes for
      the imagebuilder task — already admitted upstream)
    - `is_platform_admin` and `force_admission=True` (audit-logged bypass)

    `mode` is one of: 'image', 'dockerfile', 'repo', 'script'.

    `strict=True` enforces the base-image allowlist even when
    WIZARD_ALLOW_ANY_IMAGE relaxation is enabled — the external
    node-authoring path passes it so external submissions can't ride the
    any-image shortcut. The deployment-mode short-circuits above are
    unaffected (on-prem still passes).
    """
    if not _is_saas():
        return AdmissionResult.ok()
    if tenant_id is None:
        return AdmissionResult.ok()
    if is_service_account:
        return AdmissionResult.ok()
    if is_platform_admin and force_admission:
        logger.warning(
            "Platform admin force-admission bypass: mode=%s payload_keys=%s tenant_id=%s",
            mode, list(payload.keys()), tenant_id,
        )
        return AdmissionResult.ok()

    if mode == "image":
        ref = payload.get("executable_path") or payload.get("source_image") or ""
        return validate_base_image_ref(ref, tenant_grants=tenant_grants, strict=strict)

    if mode in ("dockerfile", "repo"):
        dockerfile = payload.get("dockerfile") or payload.get("dockerfile_text") or ""
        if mode == "repo":
            url = payload.get("repo_url") or payload.get("source_image") or ""
            if url:
                git_result = validate_git_url(url)
                if not git_result.passed:
                    return git_result
        if dockerfile:
            return validate_dockerfile(dockerfile, tenant_grants=tenant_grants, strict=strict)
        # Git-mode submissions may not yet have a Dockerfile — it gets
        # generated by the LLM downstream, and admission re-validates
        # the LLM output before the imagebuilder dispatches.
        return AdmissionResult.ok()

    if mode == "script":
        interp = payload.get("interpreter") or "python3"
        body = payload.get("script") or payload.get("script_content") or ""
        return validate_script(interp, body)

    return AdmissionResult.fail("unknown-mode", f"Unknown ingestion mode: {mode!r}")
