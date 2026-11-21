"""Shared interface every backend recipe implements.

A "recipe" is a pair of pure functions:

  render_dockerfile(*, image_name, hero_pip_packages, **kw) -> str
      Return the Dockerfile body for this backend. Operates on metadata
      only — does NOT see the source body. Templates pin the right base
      image + runtime install for the backend.

  rewrite_source(source_files: dict[str, str]) -> RecipeOutput
      Apply the backend-specific source transformation. Returns the
      rewritten files keyed by name, a per-file change list (so the
      Wizard's diff view can render before/after with line numbers),
      and an `incompatible_reason` set to None on success or a string
      when the recipe couldn't safely transform the source. The planner
      uses incompatible_reason to decide whether to emit a "ready" or
      "incompatible" variant tile.

Recipes that don't need a custom Dockerfile or don't need to rewrite
source can return the originals unchanged. Both halves run on every
wizard build per backend that's promoted to `implemented_recipes`.

The interface is deliberately small: backend-specific quirks belong
inside the recipe, not leaking out into the planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FileChange:
    """One rewrite the recipe applied. Mirrors the variant_adaptations
    'changes' shape so the wizard's existing diff renderer picks it up
    without modification."""
    before: str
    after: str
    line: int
    reason: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "before": self.before,
            "after": self.after,
            "line": self.line,
            "reason": self.reason,
        }


@dataclass
class FileWarning:
    """Recipe-level warning the wizard surfaces in the variant tile.
    `severity` is one of 'info', 'warn', 'error' (mirrors the existing
    adapter warnings shape)."""
    severity: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"severity": self.severity, "message": self.message}


@dataclass
class RewrittenFile:
    body: str
    changes: List[FileChange] = field(default_factory=list)
    warnings: List[FileWarning] = field(default_factory=list)


@dataclass
class RecipeOutput:
    """The unified return shape every recipe.rewrite_source() must
    produce. The planner translates this into the
    variant_adaptations[v] payload the API forwards to the wizard."""
    files: Dict[str, RewrittenFile]
    incompatible_reason: Optional[str] = None
    # Backend-level warnings that aren't tied to a specific file.
    warnings: List[FileWarning] = field(default_factory=list)
    # Phase 8 — structured signal for architecture auto-discovery.
    # Set when arch_resolver proposed a previously-unseen HF→OV
    # mapping that unblocked the port. Consumed by the API writer
    # that persists the mapping onto PortOutcome.diagnostics so the
    # Phase 10 nightly promotion job can harvest high-observation
    # entries into the static allowlist.
    discovered_arch_mapping: Optional[Dict[str, object]] = None
    # #3 — workload-level vs. recipe-level incompatibility.
    #
    # When True, the workload itself is the wrong shape for this
    # backend — no amount of operator hand-porting will make it fit.
    # Examples: a shell-out-to-binary photogrammetry script has no
    # torch code, so IPEX literally has nothing to optimise; a script
    # without `from_pretrained()` has nothing for OpenVINO to export.
    # The planner suppresses the stub Dockerfile emission for these
    # so the wizard doesn't show a misleading "incompatible but with
    # IPEX/OpenVINO baked in" template the operator can't actually
    # finish.
    #
    # When False (recipe-level incompat), the workload IS in scope
    # but this backend's auto-port can't handle the specific shape
    # (e.g. NPU's HF class not on the optimum-intel allowlist). The
    # stub Dockerfile is still useful — operator can hand-port.
    workload_incompatible: bool = False

    def is_compatible(self) -> bool:
        return self.incompatible_reason is None

    def to_adaptation(self, *, language_for: callable) -> Dict[str, object]:
        """Render the variant_adaptations[v] dict the API expects."""
        compat = "native" if self.is_compatible() else "incompatible"
        out: Dict[str, object] = {
            "compat": compat,
            "incompatible": not self.is_compatible(),
            "incompatible_reason": self.incompatible_reason or "",
            "files": {
                fn: {
                    "changes": [c.to_dict() for c in rf.changes],
                    "warnings": [w.to_dict() for w in rf.warnings],
                    "language": language_for(fn),
                    "incompatible": not self.is_compatible(),
                    "incompatible_reason": self.incompatible_reason or "",
                    # Phase 8 — flip llm_used when an arch mapping was
                    # discovered via the LLM resolver for this recipe.
                    "llm_used": bool(self.discovered_arch_mapping),
                }
                for fn, rf in self.files.items()
            },
        }
        if self.discovered_arch_mapping:
            out["arch_mapping"] = dict(self.discovered_arch_mapping)
        return out
