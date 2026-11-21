"""P9o — "Why did this fail?" explainer for port / build / smoke failures.

When a variant lands in an error state, the wizard's build-queue row
used to show "exit code 1 — check logs" and leave the user to parse a
multi-thousand-line stack trace. This helper generates a plain-English
postmortem with a concrete suggested fix.

Three sources of explanation, in precedence order:

  1. Redis cache (`port_failure_explain:{log_hash}:{porter_version}`)
     — repeat failures of the same shape hit cache and don't re-spend
     LLM tokens.
  2. LLM call via utils.llm_client.call_wizard_llm, constrained prompt
     that asks for a ≤100-word summary + one concrete fix.
  3. Static pattern table — regex-match common failure signatures
     ("undefined symbol", "cannot find -lhiprtc", "torch not compiled
     with CUDA", etc.). Ships with a curated set of the most common
     HIP / SYCL / OpenVINO / torch-wheel break modes so we can
     explain failures even when the LLM is down.

Output shape (stable — _apply_variant_smoke_result and the variant-
status PATCH handler both depend on it):

    {
      "reason": str,        # ≤ 200 chars, human-readable one-liner
      "manual_fix": str,    # ≤ 500 chars, one concrete suggestion
      "log_excerpt": str,   # ~15 line window around the first error
      "source": str,        # "llm" / "cache" / "pattern:undefined_symbol"
    }

Stage is one of: "port" / "build" / "smoke". Used to frame the prompt
and to narrow the pattern table (a "forward_call" probe failure gets
different heuristics than a build linker error).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 24 * 3600  # a week; patterns don't change fast
LOG_EXCERPT_LINES = 15
MAX_REASON_CHARS = 200
MAX_FIX_CHARS = 500

_redis = None


def _get_redis():
    global _redis
    if _redis is None:
        try:
            import redis
            _redis = redis.Redis.from_url(
                os.getenv("REDIS_URL", "redis://redis:6379/2"),
                decode_responses=True,
                socket_connect_timeout=2,
            )
            _redis.ping()
        except Exception as e:
            logger.warning("Redis port_failure_explain unavailable: %s", e)
            _redis = None
    return _redis


def _reset_client_for_tests():
    global _redis
    _redis = None


# ---------------------------------------------------------------------------
# Static pattern table — ordered by specificity. First-match wins.
# Each entry: (compiled_regex, stage_filter, reason_template, manual_fix)
# stage_filter is either None (matches any stage) or a set of stage
# names the pattern is relevant for.
# ---------------------------------------------------------------------------
_PATTERNS: List[Tuple[re.Pattern, Optional[set], str, str, str]] = [
    # Linker — undefined symbol is often a CUDA-only API missing from HIP/SYCL
    (
        re.compile(r"undefined (?:symbol|reference)[:\s]+[`'\"]?([a-zA-Z_][\w:]+)[`'\"]?", re.IGNORECASE),
        {"build", "port"},
        "Linker couldn't resolve `{0}` — this is typically a CUDA-only API "
        "with no direct equivalent on the target backend.",
        "Check the PortReport's `unsupported_apis` list for this variant. "
        "If `{0}` is listed, the port needs an LLM-cleanup pass or a manual "
        "rewrite to an equivalent API. If not listed, the porter may have "
        "skipped translation of a header — re-run with a fresh cache.",
        "pattern:undefined_symbol",
    ),

    # ROCm toolchain missing from base image
    (
        re.compile(r"cannot find -l?hiprtc|cannot find -l?hipblas|cannot find -l?rocblas", re.IGNORECASE),
        {"build"},
        "ROCm toolchain libraries aren't on the linker's path in the variant image.",
        "The rocm base image should provide these via /opt/rocm/lib. Confirm "
        "the Dockerfile's ROCM_HOME env is set and /opt/rocm/lib is on "
        "LD_LIBRARY_PATH. If the base image is stale, rebuild the rocm "
        "base image from `CORE/DockerContainers/BaseImages/rocm/`.",
        "pattern:rocm_toolchain_missing",
    ),

    # Intel oneAPI / DPC++ headers missing
    (
        re.compile(r"fatal error:.*['\"]sycl/sycl\.hpp['\"] file not found", re.IGNORECASE),
        {"build"},
        "Intel oneAPI SYCL headers missing — the DPC++ compiler can't find sycl.hpp.",
        "Check the intel base image has /opt/intel/oneapi/compiler/latest/include "
        "in its CPATH. The intel variant's Dockerfile should `source "
        "/opt/intel/oneapi/setvars.sh` before `python setup.py build_ext`.",
        "pattern:intel_headers_missing",
    ),

    # torch wheel mismatch — wheel doesn't match the variant's hardware
    (
        re.compile(r"torch.*not compiled with CUDA", re.IGNORECASE),
        {"smoke", "build"},
        "Installed torch wheel doesn't have CUDA support baked in.",
        "This variant needs a hardware-matching torch wheel. For cuda: "
        "`pip install torch --index-url https://download.pytorch.org/whl/cu121`. "
        "For rocm: `torch --index-url https://download.pytorch.org/whl/rocm5.7`. "
        "Check the Dockerfile's pip install line for a mismatched --index-url.",
        "pattern:torch_wheel_mismatch",
    ),

    # libtorch.so resolution at runtime — torch install is broken
    (
        re.compile(r"libtorch[_\w]*\.so[.\d]*: cannot open shared object file", re.IGNORECASE),
        {"smoke"},
        "torch's dynamic libraries can't be loaded at import time — broken install.",
        "The variant image's torch install is corrupted. Usually a failed "
        "pip install that partially succeeded. Rebuild the variant image "
        "with a clean pip cache: `pip install --no-cache-dir torch`.",
        "pattern:torch_so_missing",
    ),

    # CUDA toolkit missing (nvcc)
    (
        re.compile(r"nvcc: command not found|nvcc not found", re.IGNORECASE),
        {"build"},
        "CUDA toolkit (nvcc) isn't installed in the variant image.",
        "The cuda variant's base image should include /usr/local/cuda/bin in "
        "PATH. Check the base image pick is `base-cuda:...` not `python:...`. "
        "If this is a deep-CUDA repo, CUDAExtension needs nvcc at build time.",
        "pattern:nvcc_missing",
    ),

    # Forward pass OOM
    (
        re.compile(r"CUDA error: out of memory|HIP (?:error|out of memory)|XPU out of memory", re.IGNORECASE),
        {"smoke"},
        "Variant ran out of GPU memory on the smoke probe's synthetic input.",
        "The probe uses zero tensors of the declared input_schema shape. "
        "If the shape is larger than the worker's GPU can hold, the probe "
        "OOMs. Shrink input_schema.inputs[].shape in the NodeDefinition, "
        "or target a larger-memory worker tier.",
        "pattern:oom",
    ),

    # Smoke probe import failure — typical of a missing framework wheel
    (
        re.compile(r"import ['\"]?(\w+)['\"]? failed|No module named ['\"]?(\w+)['\"]?", re.IGNORECASE),
        {"smoke"},
        "Python import failed: the variant image is missing a module the "
        "node's code depends on.",
        "Check the variant's requirements.txt includes the missing module "
        "and that `requirements_patches` didn't strip it during the port. "
        "For rocm/intel/npu variants, framework wheels are swapped wholesale "
        "(torch-cuda → torch-rocm etc.) — a non-torch dep may have been "
        "removed by mistake.",
        "pattern:import_failed",
    ),

    # hipify-clang didn't translate all files
    (
        re.compile(r"error: use of undeclared identifier ['\"]?cuda", re.IGNORECASE),
        {"build"},
        "Source still references CUDA APIs that hipify-clang didn't translate.",
        "Open the Port diff viewer (P9n) on this task and look for .cu "
        "files whose after-body still contains `cuda*` symbols. Those "
        "files need a manual rewrite or an LLM-cleanup pass. Also check "
        "`PortReport.manual_cleanup_needed` for items the porter flagged.",
        "pattern:cuda_residual",
    ),

    # Generic Python tracebacks — pick up on Traceback + ExceptionType
    (
        re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE),
        None,
        "Unhandled Python exception during {stage}. Full traceback is in "
        "the log excerpt below.",
        "Read the exception type + message on the last Traceback line and "
        "match it against common patterns. For 'AttributeError' on a "
        "ported module, the porter may have renamed/removed a symbol; "
        "for 'RuntimeError', the device may not support the op.",
        "pattern:python_traceback",
    ),
]


def _log_hash(log_tail: str) -> str:
    """SHA-256 of the log's first 4KB — enough to disambiguate while
    staying stable across slight log tail trimming between retries."""
    h = hashlib.sha256()
    h.update((log_tail or "")[:4096].encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def _extract_excerpt(log_tail: str) -> str:
    """Pull the most relevant ~15 lines from the log_tail. If a marker
    (ERROR / Traceback / fatal error / undefined) appears, centre the
    window there; otherwise return the last N lines."""
    if not log_tail:
        return ""
    lines = log_tail.splitlines()
    markers = re.compile(
        r"(?i)(ERROR|FAILED|fatal error|Traceback|undefined reference|"
        r"undefined symbol|cannot find|CUDA error|HIP error|OOM)"
    )
    first_idx = -1
    for i, line in enumerate(lines):
        if markers.search(line):
            first_idx = i
            break
    if first_idx == -1:
        return "\n".join(lines[-LOG_EXCERPT_LINES:])
    start = max(0, first_idx - 2)
    end = min(len(lines), first_idx + LOG_EXCERPT_LINES - 2)
    return "\n".join(lines[start:end])


def _pattern_match(stage: str, log_tail: str) -> Optional[Dict[str, Any]]:
    """Walk the static pattern table and return the first hit."""
    if not log_tail:
        return None
    for regex, stage_filter, reason_tpl, fix, source in _PATTERNS:
        if stage_filter is not None and stage not in stage_filter:
            continue
        m = regex.search(log_tail)
        if not m:
            continue
        try:
            # {0} in the template refers to the first capture group if present,
            # else the whole match. Tolerate no groups gracefully.
            fmt_arg = m.group(1) if m.groups() else m.group(0)
        except IndexError:
            fmt_arg = m.group(0)
        reason = reason_tpl.replace("{0}", fmt_arg).replace("{stage}", stage)
        fix_rendered = fix.replace("{0}", fmt_arg).replace("{stage}", stage)
        return {
            "reason": reason[:MAX_REASON_CHARS],
            "manual_fix": fix_rendered[:MAX_FIX_CHARS],
            "log_excerpt": _extract_excerpt(log_tail),
            "source": source,
        }
    return None


_LLM_SYSTEM_PROMPT = (
    "You are a port-failure analyst for the MOD platform's CUDA→{HIP,SYCL,"
    "OpenVINO} automated kernel porter. Given a build or runtime log, "
    "you produce a short explanation and one concrete fix suggestion.\n\n"
    "Respond with STRICTLY this JSON shape, no prose:\n"
    '{"reason": "<≤200 chars>", "manual_fix": "<≤500 chars>"}\n'
    "Keep the reason to one sentence. Avoid hedging. The manual_fix must be "
    "one concrete action, not a list of possibilities."
)


def _try_llm(stage: str, variant: str, log_tail: str,
             port_report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Ask the wizard-configured LLM for an explanation. Returns None when
    the LLM is unavailable, times out, or returns unparseable output —
    the caller falls back to the static table."""
    try:
        from utils.llm_client import call_wizard_llm, LLMClientError
    except Exception:
        return None

    excerpt = _extract_excerpt(log_tail)
    if not excerpt:
        return None
    pr_summary = ""
    if isinstance(port_report, dict):
        tool = port_report.get("tool") or "unknown tool"
        unsupported = port_report.get("unsupported_apis") or []
        pr_summary = (
            f"Porter tool: {tool}. "
            f"Unsupported APIs: {', '.join(unsupported[:5]) or 'none'}.\n"
        )

    user_prompt = (
        f"Variant: {variant}\n"
        f"Stage: {stage}\n"
        f"{pr_summary}"
        f"Log excerpt:\n{excerpt}"
    )

    try:
        from utils.llm_client import LLMClientError
        resp = call_wizard_llm(
            system_prompt=_LLM_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=20,
            max_tokens=400,
        )
    except LLMClientError as e:
        logger.info("port_failure_explain: LLM unavailable (%s); using pattern fallback", e)
        return None
    except Exception:
        logger.exception("port_failure_explain: LLM call unexpected error")
        return None

    if not isinstance(resp, dict):
        return None
    reason = str(resp.get("reason") or "").strip()
    manual_fix = str(resp.get("manual_fix") or "").strip()
    if not reason or not manual_fix:
        return None
    return {
        "reason": reason[:MAX_REASON_CHARS],
        "manual_fix": manual_fix[:MAX_FIX_CHARS],
        "log_excerpt": excerpt,
        "source": "llm",
    }


def _cache_key(log_tail: str, variant: str, porter_version: str) -> str:
    return f"port_failure_explain:{_log_hash(log_tail)}:{variant}:{porter_version}"


def explain_failure(
    *,
    stage: str,
    log_tail: str,
    variant: str,
    port_report: Optional[Dict[str, Any]] = None,
    porter_version: str = "v1",
) -> Dict[str, Any]:
    """Produce a {reason, manual_fix, log_excerpt, source} dict for a
    failure. Always returns something — worst case, a generic "unknown
    failure" entry with the log excerpt so the user isn't staring at a
    blank panel.

    Never raises. Callers treat the returned dict as opaque structured
    data; they shouldn't depend on which source produced it.
    """
    if not stage:
        stage = "unknown"
    stage = stage.lower()

    # Cache hit short-circuits everything (LLM + pattern).
    r = _get_redis()
    key = _cache_key(log_tail, variant, porter_version)
    if r is not None:
        try:
            cached = r.get(key)
            if cached:
                parsed = json.loads(cached)
                parsed["source"] = "cache"
                return parsed
        except Exception:
            logger.debug("port_failure_explain: cache read failed", exc_info=True)

    # LLM first — if available and the prompt returns usable JSON.
    result = _try_llm(stage, variant, log_tail, port_report)

    # Pattern fallback second.
    if result is None:
        result = _pattern_match(stage, log_tail)

    # Last-ditch default — user still gets the log excerpt.
    if result is None:
        result = {
            "reason": f"{stage} stage failed for {variant} variant — no "
                      "matching explanation pattern. Review the log excerpt below.",
            "manual_fix": "Open full logs (bottom of build row) and check "
                          "the last ~50 lines of container stdout. If the "
                          "failure repeats, share the log with platform "
                          "support so we can add a pattern.",
            "log_excerpt": _extract_excerpt(log_tail),
            "source": "default",
        }

    # Cache write (best-effort — never fail the user-facing call on a
    # Redis hiccup). Don't cache the default/unknown result — patterns
    # can be added later and we want re-runs to try afresh.
    if r is not None and result.get("source") not in ("default",):
        try:
            payload = {k: v for k, v in result.items() if k != "source"}
            r.setex(key, CACHE_TTL_SECONDS, json.dumps(payload))
        except Exception:
            logger.debug("port_failure_explain: cache write failed", exc_info=True)

    return result
