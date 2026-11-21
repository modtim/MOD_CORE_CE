"""wizard-rebuild §7a — diff_against_plan invariant.

After §3 every BuildSpec carries a BuildPlan, and the canonical
hero_dockerfile is whatever the renderer produces from that plan
(`render_dockerfile(plan)`). §7's repair state machine relies on
this invariant: when a repair attempt is about to fire, the spec
state should reflect what the LAST applied delta wanted the
Dockerfile to be.

External mutation can break the invariant — the most common cause
is a free-form outer-mode LLM repair that returned a Dockerfile
which then got written verbatim onto ``spec.hero_dockerfile``
without flowing through the plan-delta path. When that happens, the
next repair attempt would be working off a Dockerfile that no longer
matches the plan, and any plan-delta the LLM proposes would either
silently be re-clobbered by the stale free-form Dockerfile or
conflict with it.

The fix is mechanical: detect the divergence BEFORE the next LLM
call, REVERT the Dockerfile to `render(plan)`, and rebuild. No LLM
budget consumed; the spec just goes back to the last consistent
state. If the rebuilt-from-plan Dockerfile then fails the build for
the same underlying reason, the next iteration calls the LLM
normally — the difference is the LLM is now working from the clean
canonical state.

Public API:

  - `diff_against_plan(spec_data) -> Optional[str]`
    Returns the rendered plan Dockerfile if hero_dockerfile diverges
    from `render(plan)` after normalization; returns None if aligned
    or if the plan is absent/legacy.

Normalization strips comments, blank lines, and trailing whitespace
so cosmetic differences (a re-rendered comment, an extra newline)
don't trip the revert.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _normalize(dockerfile: str) -> str:
    """Strip comments, blank lines, and trailing whitespace.

    Two Dockerfiles that differ ONLY in comments / blank-line layout
    are considered aligned — those changes are cosmetic and don't
    affect the built image. Anything else (a RUN swap, a different
    FROM, an extra COPY) shows up as a real diff.
    """
    out: list[str] = []
    for raw in (dockerfile or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # Strip inline comments cautiously — Dockerfile comments are
        # full-line only (lines starting with #) per the OCI spec.
        # Inline `#` inside a RUN command is valid shell.
        if line.lstrip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def diff_against_plan(spec_data: dict) -> Optional[str]:
    """Return the canonical rendered Dockerfile if `hero_dockerfile`
    diverges from `render(plan)`, else None.

    Args:
      spec_data: the ``BuildSpecRecord.spec_json`` dict.

    Returns:
      The rendered Dockerfile string if a revert is needed.
      None if the plan is missing (legacy spec), unparseable, or if
      the spec's hero_dockerfile already matches the canonical
      render (cosmetic-only differences ignored).

    Never raises — failures to render or parse are treated as
    "no diff detected" so the rest of the repair pipeline can
    proceed with the LLM call. The repair endpoint logs the case
    if it cares.
    """
    if not isinstance(spec_data, dict):
        return None
    plan_dict = spec_data.get("build_plan")
    if not isinstance(plan_dict, dict) or not plan_dict.get("mode"):
        # Legacy spec without a unified BuildPlan — no canonical
        # render to compare against. Repair loop falls through to the
        # pre-§3 path which mutates hero_dockerfile free-form.
        return None
    hero_df = spec_data.get("hero_dockerfile") or ""
    if not hero_df:
        return None
    try:
        # Local imports — keep module-level surface small.
        from node_builder.build_plan.types import BuildPlan
        from node_builder.build_plan.renderer import render_dockerfile
        plan = BuildPlan(**plan_dict)
        canonical = render_dockerfile(plan)
    except Exception as exc:
        logger.debug(
            "diff_against_plan: render failed (%s) — treating as aligned "
            "so the repair pipeline can continue", exc,
        )
        return None
    if _normalize(canonical) == _normalize(hero_df):
        return None
    return canonical
