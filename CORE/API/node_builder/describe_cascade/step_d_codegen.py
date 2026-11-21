"""wizard-rebuild §8 step D — codegen (last resort).

Wraps the existing `describe_synthesize.synthesize_wrapper` helper.
The Containerizer LLM authors a Script-mode wrapper from scratch
based on the user's description. Lowest cascade rank because every
other step (container / repo / library) reuses something
pre-existing; codegen requires the LLM to invent the whole flow,
which is the slowest path to converge.

This step doesn't have its own rate-limit window — the
describe_synthesize endpoint already has its own (10/15min in
`routers.node_definitions._check_and_inc_synth_rate`).
"""
from __future__ import annotations

import logging
from typing import Optional

from ._types import Candidate, StepResult
from utils.registries import platform_image

logger = logging.getLogger(__name__)


def run_step_d(description: str) -> StepResult:
    result = StepResult(step="D")
    try:
        from node_builder import describe_synthesize as ds
        synth = ds.synthesize_wrapper(description)
    except Exception as exc:
        result.errors["codegen"] = str(exc)[:200]
        return result
    if not synth.ok:
        # Surface the rejection reasons in errors so the UI can show
        # them without dragging in the synth response shape.
        result.errors["codegen"] = "; ".join(synth.rejection_reasons or []) or "synth rejected"
        return result
    # Confidence is medium-low — codegen is always available but
    # rarely the user's first preference vs container / repo /
    # library. UI uses this score for ranking only; user can always
    # pick it explicitly.
    confidence = 0.6 if synth.script_content else 0.0
    result.candidates.append(Candidate(
        kind="codegen",
        label=f"LLM-authored {synth.suggested_input_type or 'text'} → {synth.suggested_output_type or 'text'} wrapper",
        submission={
            "inline_source_body": synth.script_content,
            "inline_source_filename": "main.py",
            "wrap_base_image": synth.base_image_hint or
                platform_image("base-python:3.11"),
            "input_type": synth.suggested_input_type or "text",
            "output_type": synth.suggested_output_type or "text",
        },
        confidence=confidence,
        backend="codegen",
        eta_seconds=540,
        reason=synth.reasoning or "LLM-authored wrapper from scratch",
    ))
    return result
