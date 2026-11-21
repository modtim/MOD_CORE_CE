"""Per-backend Dockerfile + source-rewrite recipes.

Phase 2 onwards: each recipe module exports

    render_dockerfile(*, hero_pip_packages, ...) -> str
    rewrite_source(source_files: dict[str, str]) -> RecipeOutput

The planner picks `RECIPE_MODULES[backend]` for any backend in
`implemented_recipes`, runs the recipe, and emits the rewritten
Dockerfile + source. Backends NOT in `implemented_recipes` fall back
to `STUB_RENDERERS[backend]` and an "incompatible — recipe not
shipped yet" reason from the negative classifier.

`STUB_RENDERERS` is kept as an alias so Phase-1-era callers still
work; `intel_xpu.render` and `intel_npu.render` forward to
`render_dockerfile` for that compatibility.
"""

from . import intel_xpu, intel_npu

# Phase-1 stub interface (kept for compatibility).
STUB_RENDERERS = {
    "intel": intel_xpu.render,
    "npu":   intel_npu.render,
}

# Phase-2+ recipe modules. Each exports `render_dockerfile(...)` and
# `rewrite_source(source_files)` returning a RecipeOutput.
RECIPE_MODULES = {
    "intel": intel_xpu,
    "npu":   intel_npu,
}

__all__ = ["STUB_RENDERERS", "RECIPE_MODULES", "intel_xpu", "intel_npu"]
