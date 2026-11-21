"""Critic D — Pre-build Repair-Fix Auditor.

Reviews the repair LLM's proposed Dockerfile/script change BEFORE
the build-task stage attempts a docker build (which costs 5-15
minutes per attempt). The critic asks one focused question:

  Given the build failure described by stderr X, does the proposed
  fix actually address X — or did the LLM make a surface-level
  guess that fixes nothing?

Rejection categories the critic catches:
  - Fix doesn't touch the file/component cited in the error
  - Fix pins a non-existent package version
  - Fix introduces a known-broken pattern (groupadd docker, useradd
    against pre-baked user, etc.)
  - Fix re-applies a pattern that already failed in repair_history

The critic does NOT police taste, style, or completeness — only
"would this plausibly fix the cited error?" Pass-through latency
is one extra LLM call (~3-5s) per repair attempt vs. 5-15 min for
a wasted docker build, so the cost-benefit is heavily favorable.

Returns up to ``max_iters`` LLM calls total. On critic exhaustion
or critic infra failure, the last proposer attempt is shipped
unverified (graceful degrade — never block the build pipeline on
critic-layer failures, per the existing critic_loop convention).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from services.critic_loop import (
    LoopResult,
    Verdict,
    critic_loop,
    format_issues_for_proposer,
)
from utils.llm_client import LLMClientError

logger = logging.getLogger(__name__)


_CRITIC_SYSTEM_PROMPT = """You are reviewing one proposed Dockerfile \
or script change that a repair assistant generated to fix a failing \
build. Your ONLY job: decide whether the proposed change PLAUSIBLY \
addresses the cited build error.

Specifically check:

1. DOES THE FIX TOUCH THE RIGHT THING? If the stderr says "CMake \
not found", the fix should add cmake (apt-get install / etc.) — \
not rename a Python module, not bump a version pin, not edit \
unrelated lines.

2. PACKAGE VERSIONS THE FIX PINS — do they EXIST? Reject pinning \
to versions you cannot verify exist on PyPI / the relevant index \
(e.g. "nerfstudio==1.1.8" when 1.1.x maxes at 1.1.5). When unsure, \
PREFER unpinned over a guessed-pin.

3. KNOWN-BROKEN PATTERNS — flag and reject:
   - `RUN groupadd docker` / `useradd -G docker` inside the image \
     (no docker.sock is mounted at runtime; this pattern never \
     helps and bloats the image)
   - Removing `OUTPUT_DIR` / `/data/output` writes from the \
     entrypoint
   - Removing FROM directive
   - Removing CMD / ENTRYPOINT

4. CYCLES — if repair_history shows a similar fix already tried \
and the stderr signature is unchanged after that prior attempt, \
reject. The repair assistant is local-minimum hopping and needs \
to step back to a structurally different approach.

You are NOT reviewing: code style, comments, whether the LLM \
"could have done better", or anything other than "would this \
PLAUSIBLY fix the cited error?"

Output JSON only, no prose, no markdown:
{"ok": true} if the fix plausibly addresses the cited error.
{"ok": false, "issues": ["..."]} if any of the checks fail. Each \
issue is one short concrete sentence saying WHICH check failed and \
WHAT change is needed. Example: "The fix pins nerfstudio==1.1.8 \
but 1.1.x maxes at 1.1.5 on PyPI. Use 1.1.5 or leave unpinned."
"""


def _build_critic_user_prompt(
    *,
    stderr_signature: str,
    prior_dockerfile: str,
    proposed_dockerfile: Optional[str],
    proposed_scripts: Dict[str, str],
    llm_explanation: str,
    repair_history_tail: List[Dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append("Failing build's stderr signature:")
    lines.append("```")
    lines.append((stderr_signature or "")[-1500:])
    lines.append("```")
    lines.append("")
    lines.append("Prior (failing) Dockerfile:")
    lines.append("```")
    lines.append((prior_dockerfile or "")[-2500:])
    lines.append("```")
    if proposed_dockerfile is not None and proposed_dockerfile != prior_dockerfile:
        lines.append("")
        lines.append("Proposed new Dockerfile:")
        lines.append("```")
        lines.append((proposed_dockerfile or "")[-2500:])
        lines.append("```")
    if proposed_scripts:
        lines.append("")
        lines.append("Proposed script changes:")
        for path, body in list(proposed_scripts.items())[:5]:
            lines.append(f"--- {path} ---")
            lines.append((body or "")[-1000:])
    lines.append("")
    lines.append("Repair assistant's explanation of the fix:")
    lines.append((llm_explanation or "").strip()[:1000])
    if repair_history_tail:
        lines.append("")
        lines.append("Recent prior repair attempts in this build's history (for cycle detection):")
        for a in repair_history_tail[-5:]:
            n = a.get("attempt", "?")
            mode = a.get("mode", "?")
            expl = (a.get("llm_explanation") or "").strip()[:200]
            sig = (a.get("stderr_signature") or "").strip()[-300:]
            lines.append(f"  attempt {n} ({mode}): {expl}")
            if sig:
                lines.append(f"    → resulted in error: {sig}")
    return "\n".join(lines)


def _critic_call(
    *,
    stderr_signature: str,
    prior_dockerfile: str,
    proposed_dockerfile: Optional[str],
    proposed_scripts: Dict[str, str],
    llm_explanation: str,
    repair_history_tail: List[Dict[str, Any]],
    llm: Callable[..., Any],
) -> Verdict:
    user_prompt = _build_critic_user_prompt(
        stderr_signature=stderr_signature,
        prior_dockerfile=prior_dockerfile,
        proposed_dockerfile=proposed_dockerfile,
        proposed_scripts=proposed_scripts,
        llm_explanation=llm_explanation,
        repair_history_tail=repair_history_tail,
    )
    try:
        parsed = llm(
            system_prompt=_CRITIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=15,
            temperature=0.0,
            max_tokens=400,
        )
    except LLMClientError as e:
        # Critic LLM unavailable — graceful degrade, approve so the
        # build pipeline isn't blocked on critic infra. The proposer's
        # output ships unverified (matches the convention in the
        # existing wizard critics A/B/C).
        logger.info("critic_repair_fix LLM unavailable: %s", e)
        return Verdict(ok=True, issues=[], notes={"critic_error": str(e)})

    if not isinstance(parsed, dict):
        return Verdict(ok=True, issues=[], notes={"critic_error": "non-dict response"})
    ok = parsed.get("ok")
    if not isinstance(ok, bool):
        return Verdict(ok=True, issues=[], notes={"critic_error": "missing ok"})
    if ok:
        return Verdict(ok=True, issues=[])

    raw = parsed.get("issues") or []
    if not isinstance(raw, list):
        return Verdict(ok=False, issues=["(reviewer raised an objection but its response was malformed)"])
    issues = [str(i).strip() for i in raw if str(i).strip()][:5]
    if not issues:
        issues = ["(reviewer raised an objection with no concrete reason — try again)"]
    return Verdict(ok=False, issues=issues)


def audit_repair_fix(
    *,
    stderr_signature: str,
    prior_dockerfile: str,
    repair_history_tail: List[Dict[str, Any]],
    propose_fn: Callable[[Optional[List[str]]], Dict[str, Any]],
    llm: Callable[..., Any],
    max_iters: int = 2,
) -> Dict[str, Any]:
    """Run the repair-LLM proposer through the critic, retrying up to
    ``max_iters`` times when the critic rejects.

    ``propose_fn`` is a closure that takes optional prior_issues (the
    critic's feedback) and returns the repair LLM's parsed response
    (the dict with ``updated_dockerfile`` / ``updated_scripts`` /
    ``explanation``). It bakes in the actual call_wizard_llm
    invocation so this module stays LLM-client-agnostic.

    ``max_iters`` defaults to 2 (one initial + one retry). Each
    iteration costs one repair LLM call + one critic LLM call.
    Higher values catch more cases but linearly increase per-repair
    latency + cost. 2 is a conservative starting point.

    Returns the (possibly improved) repair response dict. On critic
    exhaustion, returns the last attempt — graceful degrade,
    consistent with the existing wizard critics.
    """

    def proposer(prev_attempt: Any, prior_issues: Optional[List[str]]) -> Dict[str, Any]:
        # Pass the critic's prior_issues down to the repair LLM so the
        # next attempt sees the rejection reasons explicitly. The
        # closure (in node_definitions.py) splices these into the
        # repair user prompt as an extra constraint block.
        return propose_fn(prior_issues)

    def critic(output: Dict[str, Any]) -> Verdict:
        # If the proposer returned a malformed response, skip the
        # critic — the caller's validate_repair_response layer will
        # reject it via the existing rejection_reasons path.
        if not isinstance(output, dict):
            return Verdict(ok=True, issues=[], notes={"skipped": "non-dict-proposer"})

        # Deterministic image-existence check FIRST — catches the
        # LLM's hallucinated CUDA tags (nvidia/cuda:12.4.1-base,
        # harbor.../base-cuda:12.4-python3.11-slim, etc.) before
        # wasting an LLM critic round-trip. wfr 48 spent 4+ attempts
        # cycling through hallucinated tags; this rejects them
        # without burning compute.
        proposed_df = output.get("updated_dockerfile") or ""
        if proposed_df:
            from node_builder.known_bases import validate_dockerfile_from_lines
            reject = validate_dockerfile_from_lines(proposed_df)
            if reject:
                return Verdict(
                    ok=False, issues=[reject],
                    notes={"layer": "deterministic_image_existence"},
                )

            # No-op repair check. wfr 57 cycled 10 outer attempts on a
            # libgl1-mesa-glx error where each "fix" left the bad
            # package still in the Dockerfile — either because the
            # LLM returned a byte-identical Dockerfile and hallucinated
            # the explanation, or because it tweaked surface lines
            # without touching the failing apt-get line. Reject both
            # patterns deterministically here so the budget isn't
            # burned on no-ops.
            if proposed_df.strip() == (prior_dockerfile or "").strip():
                return Verdict(
                    ok=False,
                    issues=[(
                        "Proposed Dockerfile is byte-identical to the "
                        "prior failing one. Your explanation describes "
                        "a change you did not actually make in the "
                        "`updated_dockerfile` field. Re-emit the full "
                        "Dockerfile with the described change actually "
                        "applied."
                    )],
                    notes={"layer": "deterministic_no_op_repair"},
                )

            # Apt-package-still-present check. Catches the libgl1-mesa-glx
            # loop where the LLM claims "removed package X" but X is
            # still in the new apt-get install line. Greps the proposed
            # Dockerfile for any package name flagged by the most recent
            # apt failure in stderr (`E: Package 'X' has no installation
            # candidate`). If found, reject with concrete naming —
            # cheaper and more reliable than waiting on the critic LLM
            # to spot it.
            import re as _re_apt
            apt_missing = _re_apt.findall(
                r"E:\s+Package\s+'([A-Za-z0-9.+\-_]+)'\s+has\s+no\s+installation\s+candidate",
                stderr_signature or "",
            )
            apt_still_present = [
                pkg for pkg in set(apt_missing)
                if _re_apt.search(rf"(?<![A-Za-z0-9._\-+]){_re_apt.escape(pkg)}(?![A-Za-z0-9._\-+])", proposed_df)
            ]
            if apt_still_present:
                pkgs = ", ".join(sorted(apt_still_present))
                return Verdict(
                    ok=False,
                    issues=[(
                        f"Build failed because apt-get could not install: {pkgs}. "
                        f"Your proposed Dockerfile STILL references {pkgs} in an "
                        f"apt-get install or RUN line. The fix must REMOVE "
                        f"{pkgs} from every apt-get install line (or substitute "
                        f"the correct package name on this base — e.g. "
                        f"libgl1-mesa-glx was removed in Debian 13/trixie, "
                        f"use `libgl1` or omit entirely if opencv-python-headless "
                        f"is the only OpenCV variant)."
                    )],
                    notes={"layer": "deterministic_apt_still_present"},
                )

            # FROM-thrash detection. wfr 65/66/67 hit this: stderr said
            # "pip install failed (PEP-668)" or similar install-time
            # issue, and the LLM responded by repeatedly swapping the
            # FROM line through debian:bookworm-slim → python:3.11-slim →
            # ubuntu:22.04 → harbor/base-python → ... never touching
            # the actual install RUN line. Pattern: 3+ outer entries
            # in repair_history each changed FROM, but the stderr
            # signature is install-time (matches pip / PEP-668 /
            # similar regex). Diagnosis: LLM is debugging the wrong
            # layer. Force it to leave FROM alone for the next attempt.
            import re as _re_thrash
            def _from_of(df: str) -> str:
                m = _re_thrash.search(
                    r"^\s*FROM\s+(\S+)", df or "",
                    _re_thrash.MULTILINE | _re_thrash.IGNORECASE,
                )
                return (m.group(1) if m else "").lower()
            recent = repair_history_tail[-3:] if repair_history_tail else []
            recent_outer = [h for h in recent if h.get("mode") == "outer"]
            install_time_markers = (
                r"externally-managed-environment",       # PEP-668
                r"error:.*pep\s*668",
                r"pip:\s*not\s+found",
                r"pip3:\s*not\s+found",
                r"error\s*:\s*subprocess-exited-with-error",
                r"could\s+not\s+build\s+wheels",
                r"failed\s+building\s+wheel",
                r"no\s+matching\s+distribution\s+found",
                r"erroneous\s+pin",
            )
            looks_install_time = any(
                _re_thrash.search(p, stderr_signature or "", _re_thrash.IGNORECASE)
                for p in install_time_markers
            )
            if (
                len(recent_outer) >= 3
                and looks_install_time
                and _from_of(proposed_df) != _from_of(prior_dockerfile or "")
            ):
                prior_from = _from_of(prior_dockerfile or "") or "(unknown)"
                return Verdict(
                    ok=False,
                    issues=[(
                        "FROM-thrash detected. The stderr error is at the "
                        "INSTALL step (pip / wheel / PEP-668), not the base "
                        "image. The last 3 outer-repair attempts all "
                        "swapped FROM and the same install-time error "
                        "kept appearing. Keep `FROM "
                        f"{prior_from}` exactly as-is and fix the install "
                        "RUN line instead — typical resolutions: use "
                        "`pip3 install` instead of `pip install`; add "
                        "`--break-system-packages` for PEP-668 distros; "
                        "use a virtual env via `RUN python -m venv /opt/venv "
                        "&& . /opt/venv/bin/activate && pip install ...`; "
                        "or split apt-get installs of `python3-pip` "
                        "before the pip RUN."
                    )],
                    notes={"layer": "deterministic_from_thrash"},
                )

            # Catch the --only-binary anti-pattern on source-distributed
            # packages. wfr 50 cycled 3+ attempts here: LLM read the
            # WHEEL_BUILD_FAILED hint's diagnostic suggestion (`pip install
            # --only-binary=:all: PACKAGE`) and applied it as a FIX —
            # which guarantees failure for packages with no PyPI wheel
            # (nerfstudio, tinycudann, gsplat, mmcv-full). Reject before
            # docker build.
            _no_wheel_libs = (
                "nerfstudio", "tinycudann", "tiny_cuda_nn", "gsplat",
                "mmcv-full", "mmcv_full", "detectron2",
                "diff_gaussian_rasterization",
            )
            if "--only-binary" in proposed_df or "--prefer-binary" in proposed_df:
                _bad_lib = next((l for l in _no_wheel_libs if l in proposed_df), None)
                if _bad_lib:
                    return Verdict(
                        ok=False,
                        issues=[(
                            f"Dockerfile uses --only-binary/--prefer-binary with {_bad_lib}, "
                            f"which has NO pre-built wheel on PyPI — this guarantees "
                            f"install failure. Remove the --only-binary flag and let "
                            f"pip source-build instead. Make sure the base image has "
                            f"the build toolchain (g++, cmake, python3-dev, "
                            f"nvcc if compiling CUDA extensions)."
                        )],
                        notes={"layer": "deterministic_anti_pattern"},
                    )

        return _critic_call(
            stderr_signature=stderr_signature,
            prior_dockerfile=prior_dockerfile,
            proposed_dockerfile=output.get("updated_dockerfile"),
            proposed_scripts=output.get("updated_scripts") or {},
            llm_explanation=output.get("explanation") or "",
            repair_history_tail=repair_history_tail,
            llm=llm,
        )

    result: LoopResult = critic_loop(
        proposer,
        critic,
        max_iters=max_iters,
        name="repair_fix",
        logger_=logger,
    )
    return result.output
