"""#3 — planner suppresses stub Dockerfiles for workload-incompatible variants.

Spec=11 (openMVG photogrammetry, 5h 17m wasted build) repro: the
Intel and NPU recipes correctly identified the workload as fundamentally
wrong (no torch / no HF model_id), but the planner emitted IPEX +
OpenVINO stub Dockerfiles anyway. The wizard then attempted to build
them (and either auto-skipped them via the build-time TODO_MODEL_ID
gate or pushed ~5GB images that did nothing).

The fix: recipes set ``workload_incompatible=True`` on the
fundamental-shape rejection paths; the planner reads that flag and
omits the variant from ``dockerfiles`` entirely. ``variant_adaptations``
still records the rejection so the wizard tile can explain it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

from tools.backends.planner import plan_unsupported_variants  # noqa: E402


_SHELL_OUT_SCRIPT = (
    "import subprocess\n"
    "from pathlib import Path\n"
    "input_dir = Path('/data/input')\n"
    "subprocess.run(['openMVG_main_SfMInit_ImageListing', '-i', str(input_dir)])\n"
)


def _plan(source: str, *, enabled=("intel", "npu"), detected=()):
    return plan_unsupported_variants(
        source_text=source,
        source_files={"user_script.py": source},
        source_filenames=["user_script.py"],
        enabled_variants=enabled,
        detected_variants=detected,
        image_name="test-node",
        implemented_recipes=("intel", "npu"),
    )


def test_shell_out_script_omits_intel_dockerfile():
    plan = _plan(_SHELL_OUT_SCRIPT)
    # `intel` SHOULD appear in incompatible_reasons (with the recipe's
    # workload-rejection text) and `variant_adaptations` (with the
    # workload_incompatible flag), but NOT in `dockerfiles`.
    assert "intel" in plan.incompatible_reasons
    assert "intel" in plan.variant_adaptations
    assert plan.variant_adaptations["intel"]["workload_incompatible"] is True
    assert "intel" not in plan.dockerfiles, (
        "Intel stub Dockerfile must be suppressed — no operator hand-"
        "port can make IPEX add value to a shell-out workload."
    )


def test_shell_out_script_omits_npu_dockerfile():
    plan = _plan(_SHELL_OUT_SCRIPT)
    assert "npu" in plan.incompatible_reasons
    assert "npu" in plan.variant_adaptations
    assert plan.variant_adaptations["npu"]["workload_incompatible"] is True
    assert "npu" not in plan.dockerfiles


def test_shell_out_script_preserves_source_variants():
    """The hero source still appears in source_variants so the wizard's
    inspector can show 'here's what this would be' even though no
    Dockerfile got emitted."""
    plan = _plan(_SHELL_OUT_SCRIPT)
    assert "intel" in plan.source_variants
    assert "npu" in plan.source_variants


def test_recipe_level_incompat_still_emits_stub_dockerfile():
    """The 'recipe can't auto-port this specific HF class' case is
    DIFFERENT — an operator could hand-port the OpenVINO export by
    swapping the model_id. Stub stays.

    Uses a SUPPORTED HF class (GPT2LMHeadModel — in _HF_TO_OV) but
    with a computed/env-derived model_id, which trips the
    'we can't bake the model into the image at build time' branch.
    That's recipe-level: an operator can hand-port by hard-coding
    the model_id."""
    src = (
        "import os\n"
        "from transformers import GPT2LMHeadModel\n"
        "model = GPT2LMHeadModel.from_pretrained(os.environ['MODEL_ID'])\n"
        "model.run('/data/input')\n"
    )
    plan = _plan(src)
    assert "npu" in plan.incompatible_reasons
    assert plan.variant_adaptations["npu"].get(
        "workload_incompatible", False
    ) is False
    # Stub Dockerfile present so the wizard can render the OpenVINO
    # boilerplate for the operator to fill in.
    assert "npu" in plan.dockerfiles


def test_genuinely_compatible_workload_emits_native_dockerfile():
    """Control: a torch+HF workload gets a native intel Dockerfile
    (not a stub) and ZERO workload_incompatible flag."""
    src = (
        "import torch\n"
        "from transformers import AutoModelForCausalLM\n"
        "model = AutoModelForCausalLM.from_pretrained('gpt2')\n"
        "model.to('cuda')\n"
    )
    plan = _plan(src)
    # intel: torch present → IPEX rewrite happy path.
    assert "intel" in plan.dockerfiles
    intel_adapt = plan.variant_adaptations["intel"]
    assert intel_adapt["compat"] == "native"
    assert intel_adapt.get("workload_incompatible", False) is False


def test_workload_incompat_variant_adaptations_has_empty_files():
    """For workload-incompatible variants, files={} (no per-file
    annotations to show — the source is not the problem, the workload
    shape is). This pins the contract the wizard inspects against."""
    plan = _plan(_SHELL_OUT_SCRIPT)
    assert plan.variant_adaptations["intel"]["files"] == {}
    assert plan.variant_adaptations["npu"]["files"] == {}
