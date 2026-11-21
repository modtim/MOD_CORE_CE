"""wizard-rebuild §8 — cascade-wide types.

Each step returns a list of `Candidate` rows with a normalized
shape so `cascade.run_cascade` can rank them across all steps with
a single sort key.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


CandidateKind = Literal[
    "container",   # step A: pre-built docker image (Image mode)
    "repo",        # step B: github repo (Git mode)
    "library",     # step C: pip/npm library (Script mode wrapping import)
    "codegen",     # step D: LLM-authored script (Script mode)
]


class Candidate(BaseModel):
    """One ranked candidate for satisfying the user's description.

    `kind` drives the downstream submission shape (Image / Git /
    Script). `confidence` is the cascade's ranking key — orchestrator
    short-circuits on the first step that yields a candidate above
    the floor (default 0.7).
    """
    kind: CandidateKind
    # User-visible label ("ffmpeg official image", "psf/requests").
    label: str
    # Submission payload — the WorkflowBuilder posts this to
    # /build-and-register-node verbatim. Shape varies per kind.
    submission: Dict[str, Any]
    # 0..1 — how confident this step is that this candidate matches.
    confidence: float = Field(ge=0.0, le=1.0)
    # Per-step source/backend (e.g. "docker_hub", "github", "pypi").
    backend: str
    # Optional ETA-to-build (seconds). UI shows this so users can
    # pick the fastest option. None when not known.
    eta_seconds: Optional[int] = None
    # Per-variant coverage hint — which accelerators the cascade
    # thinks this candidate can target. Empty list = "uncertain".
    likely_variants: List[str] = Field(default_factory=list)
    # Free-form note for the UI ("Already verified on stage", "8 GB
    # container", etc.).
    reason: str = ""


class StepResult(BaseModel):
    """One step's output."""
    step: Literal["A", "B", "C", "D"]
    candidates: List[Candidate] = Field(default_factory=list)
    # Per-backend rate-limit notices accumulated during this step.
    rate_limited: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    # Any backend that errored (timeout, 5xx) — surfaced so the UI
    # can show "Docker Hub temporarily unavailable" without failing
    # the whole cascade.
    errors: Dict[str, str] = Field(default_factory=dict)
    # Set when the step short-circuited the cascade with a high-
    # confidence pick (>= floor).
    short_circuited: bool = False


class CascadeResult(BaseModel):
    """Final orchestrator output — everything the endpoint returns."""
    description: str
    confidence_floor: float
    candidates: List[Candidate] = Field(default_factory=list)
    per_step: List[StepResult] = Field(default_factory=list)
    rate_limited: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    errors: Dict[str, str] = Field(default_factory=dict)
    stopped_at_step: Optional[Literal["A", "B", "C", "D"]] = None
    # "short-circuited" — picked because a step hit the confidence
    # floor; "exhausted" — walked all 4 steps; "fallback-codegen" —
    # only codegen produced a candidate.
    reason: str = ""
