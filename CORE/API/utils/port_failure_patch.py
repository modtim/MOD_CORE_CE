"""Phase 5 — LLM-driven port-failure patch proposer.

Sibling to `port_failure_explain.py`. Where the explainer produces a
human-readable reason + manual fix suggestion, this module produces a
machine-applicable **unified diff** that the wizard can hand straight
to ImageBuilder's `git apply` stage (INLINE_PATCH_DIFF env var).

The patch loop's end-to-end shape, shipped in Phase 5:

    build/smoke fails
    → explain_failure()  (prior P9o)
    → propose_patch()    (this module)
    → patch_loop_state.record_attempt() (sibling)
    → POST /.../variants/{v}/patch (variant_patch_dispatch router)
    → ImageBuilder applies INLINE_PATCH_DIFF → rebuild → smoke retry

The module owns exactly one LLM prompt surface and one Redis cache
namespace. It does NOT own the bounded-retry counter (that's
`patch_loop_state`) — caller composes.

Public surface:

    PatchProposal = {
      "diff":          str,        # unified diff (empty when source!="none")
      "confidence":    float,      # 0.0–1.0 LLM self-reported confidence
      "source":        str,        # "llm" | "cache" | "none"
      "token_cost":    int,        # ≈ prompt+completion tokens (0 on cache hit)
      "target_files":  list[str],  # filenames the diff touches
      "refuse_reason": str | None, # set when source == "none"
    }

    propose_patch(...) -> PatchProposal

Output contract — diffs only:
- MUST be a unified diff (lines starting `---`, `+++`, `@@`).
- MUST NOT be a full file rewrite (gated at >50% lines changed).
- MUST touch only files the caller listed in `eligible_files`.

Failure modes:
- Redis unreachable → cache-bypass; LLM call still happens.
- LLM unavailable → `source="none"` with `refuse_reason="LLM offline"`.
- LLM returns prose / non-diff → `source="none"` with a reason.
- LLM proposes a full rewrite → rejected (source="none").
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 7 * 24 * 3600  # a week — same window as port_failure_explain
MAX_DIFF_BYTES = 32_000              # ~32 KB — a generous unified diff ceiling
MAX_FILE_REPLACEMENT_RATIO = 0.50    # reject if LLM rewrites >50% of target file

# Prompt version rolls when the output contract changes. Folded into
# the Redis cache key so stale caches invalidate automatically.
PROMPT_TEMPLATE_VERSION = "v1"

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
        except Exception as e:  # noqa: BLE001
            logger.warning("port_failure_patch: Redis unavailable: %s", e)
            _redis = None
    return _redis


def _reset_client_for_tests():
    global _redis
    _redis = None


def _log_hash(log_tail: str) -> str:
    """SHA-256 of the log's first 4KB — mirrors port_failure_explain so
    both modules share a hashing cadence."""
    h = hashlib.sha256()
    h.update((log_tail or "")[:4096].encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def diff_hash(diff: str) -> str:
    """Public helper — stable hash of a unified diff, used by
    patch_loop_state to detect "LLM returned identical diff across
    retries" (one of the circuit breakers in the Phase 5 plan)."""
    h = hashlib.sha256()
    h.update((diff or "").encode("utf-8", errors="replace"))
    return h.hexdigest()[:32]


def _cache_key(
    *,
    log_tail: str,
    variant: str,
    porter_version: str,
    stage: str,
) -> str:
    return (
        f"port_failure_patch:{_log_hash(log_tail)}:{variant}:"
        f"{stage}:{porter_version}:{PROMPT_TEMPLATE_VERSION}"
    )


# ---------------------------------------------------------------------
# Diff validation — reject non-diffs and whole-file rewrites.
# ---------------------------------------------------------------------

_DIFF_HEADER_RE = re.compile(
    r"(?m)^(?:---\s+[^\s]+|Index:\s+[^\s]+|diff --git\s)", re.MULTILINE
)
_HUNK_RE = re.compile(r"(?m)^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@")


def _looks_like_unified_diff(text: str) -> bool:
    """Fast accept/reject: must contain at least one `@@` hunk header
    AND at least one `---`/`+++` pair. Catches the common LLM failure
    mode where the model writes prose + fenced code but no diff
    markup."""
    if not text:
        return False
    if not _HUNK_RE.search(text):
        return False
    if not _DIFF_HEADER_RE.search(text):
        return False
    return True


def _extract_target_files(diff: str) -> List[str]:
    """Parse `+++ b/path/file.py` lines to learn which files the diff
    touches. Tolerates `a/`, `b/`, or no prefix. Returns unique,
    sorted filenames."""
    out: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        rhs = line[4:].strip()
        # Drop git's `b/` prefix if present.
        if rhs.startswith("b/"):
            rhs = rhs[2:]
        elif rhs.startswith("a/"):
            rhs = rhs[2:]
        # `+++ /dev/null` means file deletion — not a target we'll
        # rewrite but still an eligible file.
        if rhs == "/dev/null":
            continue
        out.add(rhs)
    return sorted(out)


def _diff_within_ratio(diff: str, source_body_map: Dict[str, str]) -> Tuple[bool, str]:
    """Sanity: a patch that replaces >50% of any target file's lines
    is a red flag — the LLM abandoned context and wrote a from-scratch
    rewrite. Reject with a structured reason.

    Returns (ok, reject_reason). `source_body_map` maps filename →
    original body so we can measure replacement ratios; pass an empty
    dict when the caller doesn't have access to original bodies, in
    which case the check degrades to a byte-size-only cap.
    """
    if len(diff.encode("utf-8", errors="replace")) > MAX_DIFF_BYTES:
        return False, (
            f"patch exceeds {MAX_DIFF_BYTES} byte cap — LLM is "
            f"rewriting whole files instead of targeting the failure"
        )
    if not source_body_map:
        return True, ""
    # Count '-'/'+' body lines per target file. Lines starting with
    # `---`, `+++`, `@@` are headers and don't count.
    additions: Dict[str, int] = {}
    deletions: Dict[str, int] = {}
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            rhs = line[4:].strip()
            if rhs.startswith(("a/", "b/")):
                rhs = rhs[2:]
            current = rhs
            continue
        if current is None or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            additions[current] = additions.get(current, 0) + 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions[current] = deletions.get(current, 0) + 1
    for fname, added in additions.items():
        body = source_body_map.get(fname) or ""
        total = max(1, len(body.splitlines()))
        replaced = max(added, deletions.get(fname, 0))
        if replaced / total > MAX_FILE_REPLACEMENT_RATIO:
            return False, (
                f"patch would replace {replaced}/{total} lines of "
                f"{fname} ({int(replaced/total*100)}%); LLM abandoned "
                f"context — rejected"
            )
    return True, ""


# ---------------------------------------------------------------------
# LLM prompt + call.
# ---------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = (
    "You are an automated porting engineer for the MOD platform's "
    "CUDA→{HIP,SYCL,OpenVINO,CPU} kernel porter. A variant image "
    "failed to build OR passed build but failed the smoke probe. "
    "Your job is to produce a minimal unified diff that fixes the "
    "specific failure.\n\n"
    "HARD RULES:\n"
    "  1. Respond with STRICTLY this JSON shape:\n"
    '     {"diff": "<unified diff as a single string>", '
    '"confidence": <0.0 to 1.0>, "notes": "<≤200 chars what you changed>"}\n'
    "  2. The diff MUST be a real unified diff with `---`, `+++`, "
    "and `@@` hunk headers.\n"
    "  3. Change ONLY files listed in `eligible_files`. Do NOT add new "
    "files unless explicitly asked.\n"
    "  4. Do NOT rewrite more than 50% of any file — targeted edits "
    "only. If you'd need a whole-file rewrite, set `diff` to \"\" and "
    "explain in `notes`.\n"
    "  5. Do NOT output markdown fences, prose, or commentary. JUST "
    "the JSON object.\n"
    "  6. If the failure's root cause is hardware-limited (OOM, "
    "missing FP8 support on non-H100/MI300/PVC), set diff=\"\" and "
    "note the physical constraint — no patch can fix that.\n"
)


def _build_user_prompt(
    *,
    stage: str,
    variant: str,
    log_tail: str,
    port_report: Optional[Dict[str, Any]],
    eligible_files: List[Dict[str, str]],
    prior_explanation: Optional[Dict[str, Any]],
    prior_diff_hashes: List[str],
) -> str:
    """Assemble the user-facing prompt. Keeps eligible file bodies
    small (first 120 lines each) — the LLM has plenty of context from
    the log to localise the fix."""
    lines: List[str] = [
        f"Variant: {variant}",
        f"Stage: {stage}",
    ]
    if port_report and isinstance(port_report, dict):
        tool = port_report.get("tool") or "unknown"
        unsupported = port_report.get("unsupported_apis") or []
        lines.append(f"Porter tool: {tool}")
        if unsupported:
            lines.append(f"Unsupported APIs: {', '.join(unsupported[:5])}")
    if prior_explanation and isinstance(prior_explanation, dict):
        reason = (prior_explanation.get("reason") or "").strip()
        if reason:
            lines.append(f"Failure explanation: {reason}")
    if prior_diff_hashes:
        lines.append(
            "Prior-attempt diff hashes (do NOT repeat these): "
            + ", ".join(prior_diff_hashes[:5])
        )
    lines.append("")
    lines.append("Eligible files (you may modify only these):")
    for f in eligible_files[:8]:
        name = f.get("filename") or "<unknown>"
        body = (f.get("body") or "").splitlines()[:120]
        lines.append(f"--- FILE: {name} ---")
        lines.extend(body)
        lines.append("--- END FILE ---")
    lines.append("")
    lines.append("Log excerpt:")
    # Reuse port_failure_explain's excerpt heuristic for consistency.
    try:
        from utils.port_failure_explain import _extract_excerpt
        lines.append(_extract_excerpt(log_tail))
    except Exception:
        lines.append((log_tail or "")[-2000:])
    return "\n".join(lines)


def _try_llm(
    *,
    stage: str,
    variant: str,
    log_tail: str,
    port_report: Optional[Dict[str, Any]],
    eligible_files: List[Dict[str, str]],
    prior_explanation: Optional[Dict[str, Any]],
    prior_diff_hashes: List[str],
    max_tokens: int = 2000,
) -> Optional[Dict[str, Any]]:
    """Call the wizard's configured LLM. Returns None on any failure
    shape (provider offline, bad JSON, empty diff) so the caller
    cleanly surfaces "source=none"."""
    try:
        from utils.llm_client import call_wizard_llm, LLMClientError
    except Exception:
        return None

    user_prompt = _build_user_prompt(
        stage=stage,
        variant=variant,
        log_tail=log_tail,
        port_report=port_report,
        eligible_files=eligible_files,
        prior_explanation=prior_explanation,
        prior_diff_hashes=prior_diff_hashes,
    )

    try:
        resp = call_wizard_llm(
            system_prompt=_LLM_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=60,
            max_tokens=max_tokens,
        )
    except LLMClientError as e:
        logger.info("port_failure_patch: LLM unavailable: %s", e)
        return None
    except Exception:
        logger.exception("port_failure_patch: LLM call raised")
        return None

    if not isinstance(resp, dict):
        return None
    diff_txt = str(resp.get("diff") or "").strip()
    conf_raw = resp.get("confidence", 0.0)
    try:
        confidence = float(conf_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    notes = str(resp.get("notes") or "").strip()
    # Rough token cost — caller uses for budget gating.
    token_cost = len(user_prompt) // 4 + max_tokens // 4
    return {
        "diff": diff_txt,
        "confidence": confidence,
        "notes": notes,
        "token_cost": token_cost,
    }


# ---------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------

def propose_patch(
    *,
    stage: str,
    log_tail: str,
    variant: str,
    source_files: List[Dict[str, str]],
    port_report: Optional[Dict[str, Any]] = None,
    prior_explanation: Optional[Dict[str, Any]] = None,
    prior_diff_hashes: Optional[List[str]] = None,
    porter_version: str = "v1",
    eligible_files: Optional[List[Dict[str, str]]] = None,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """Produce a PatchProposal dict.

    Parameters:
      stage              — "build" | "smoke_drift" | "smoke_crash"
      log_tail           — worker's log_tail for the failing build
      variant            — "rocm" | "intel" | "npu" | "metal" | "cpu"
      source_files       — list of {filename, body} — the variant's
                           post-port source tree. Used both for the
                           prompt context AND for the replacement-
                           ratio sanity check.
      port_report        — plan.port_report dict (optional — enriches
                           the prompt when present).
      prior_explanation  — output of port_failure_explain on the same
                           failure (optional).
      prior_diff_hashes  — diff_hash() output of prior failed attempts
                           so the prompt can tell the LLM "don't
                           repeat these".
      porter_version     — folded into cache key (invalidates on
                           toolchain bump).
      eligible_files     — subset of source_files the LLM may modify;
                           defaults to all source_files.
      max_tokens         — completion ceiling per LLM call.

    Returns the PatchProposal dict documented in the module docstring.
    Never raises.
    """
    stage = (stage or "unknown").lower()
    prior_diff_hashes = list(prior_diff_hashes or [])
    eligible = eligible_files if eligible_files is not None else source_files
    source_body_map = {
        f.get("filename", ""): f.get("body", "")
        for f in source_files if isinstance(f, dict)
    }

    # Cache look-up first. Cache stores (diff, confidence, notes,
    # token_cost, target_files); the `source` field is reconstituted
    # per-hit below.
    r = _get_redis()
    key = _cache_key(
        log_tail=log_tail,
        variant=variant,
        porter_version=porter_version,
        stage=stage,
    )
    if r is not None:
        try:
            cached = r.get(key)
            if cached:
                parsed = json.loads(cached)
                parsed["source"] = "cache"
                parsed.setdefault("token_cost", 0)
                parsed.setdefault("refuse_reason", None)
                return parsed
        except Exception:
            logger.debug("port_failure_patch: cache read failed", exc_info=True)

    llm_out = _try_llm(
        stage=stage,
        variant=variant,
        log_tail=log_tail,
        port_report=port_report,
        eligible_files=eligible,
        prior_explanation=prior_explanation,
        prior_diff_hashes=prior_diff_hashes,
        max_tokens=max_tokens,
    )
    if llm_out is None:
        return {
            "diff": "",
            "confidence": 0.0,
            "source": "none",
            "token_cost": 0,
            "target_files": [],
            "refuse_reason": "LLM unavailable or returned unparseable JSON",
        }

    diff_txt: str = llm_out["diff"]
    if not diff_txt:
        return {
            "diff": "",
            "confidence": float(llm_out.get("confidence") or 0.0),
            "source": "none",
            "token_cost": int(llm_out.get("token_cost") or 0),
            "target_files": [],
            "refuse_reason": (
                llm_out.get("notes")
                or "LLM refused to propose a patch (hardware-limited or ambiguous)"
            )[:300],
        }

    # Shape gates — reject anything that isn't a unified diff, or that
    # would rewrite more than half a target file. Both are red flags
    # that the LLM lost the plot.
    if not _looks_like_unified_diff(diff_txt):
        return {
            "diff": "",
            "confidence": float(llm_out.get("confidence") or 0.0),
            "source": "none",
            "token_cost": int(llm_out.get("token_cost") or 0),
            "target_files": [],
            "refuse_reason": "LLM output did not parse as a unified diff",
        }

    # Eligible-file check FIRST — a diff touching a non-whitelisted
    # file is a more specific failure than an oversize replacement
    # ratio (the ratio check would fire spuriously on an unknown file
    # since we have no original body to compare against).
    target_files = _extract_target_files(diff_txt)
    eligible_names = {
        f.get("filename", "") for f in eligible if isinstance(f, dict)
    }
    illegal = [t for t in target_files if t not in eligible_names]
    if illegal:
        return {
            "diff": "",
            "confidence": float(llm_out.get("confidence") or 0.0),
            "source": "none",
            "token_cost": int(llm_out.get("token_cost") or 0),
            "target_files": target_files,
            "refuse_reason": (
                f"LLM touched files outside eligible_files: "
                f"{', '.join(illegal[:3])}"
            ),
        }

    ok, reject_reason = _diff_within_ratio(diff_txt, source_body_map)
    if not ok:
        return {
            "diff": "",
            "confidence": float(llm_out.get("confidence") or 0.0),
            "source": "none",
            "token_cost": int(llm_out.get("token_cost") or 0),
            "target_files": target_files,
            "refuse_reason": reject_reason,
        }

    proposal = {
        "diff": diff_txt,
        "confidence": float(llm_out.get("confidence") or 0.0),
        "source": "llm",
        "token_cost": int(llm_out.get("token_cost") or 0),
        "target_files": target_files,
        "refuse_reason": None,
    }

    # Cache (strip the `source` field — that's set per-hit).
    if r is not None:
        try:
            payload = {k: v for k, v in proposal.items() if k != "source"}
            r.setex(key, CACHE_TTL_SECONDS, json.dumps(payload))
        except Exception:
            logger.debug("port_failure_patch: cache write failed", exc_info=True)

    return proposal


__all__ = [
    "propose_patch",
    "diff_hash",
    "PROMPT_TEMPLATE_VERSION",
    "MAX_DIFF_BYTES",
    "MAX_FILE_REPLACEMENT_RATIO",
]
