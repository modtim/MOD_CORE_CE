"""The Intel XPU recipe must mark itself incompatible when the
user's source has no torch import.

Stage repro: PDF→PNG converter ("import fitz" — no torch, no ML)
took 11+ minutes to build because the wizard generated an Intel
variant and pushed a ~5GB image across stage→platform Harbor.
The Intel variant has IPEX + transformers + PyTorch baked into
the base + recipe pip layer; for a non-torch node, none of that
runs at runtime, but the build/push tasks burned anyway.

The fix: intel_xpu.rewrite_source returns
``incompatible_reason="No torch / ML code detected..."`` when
no .py or .sh in the input mentions torch. The build_dag adapter
(build_dag/adapter.py:48) already skips variants whose
adaptations carry ``incompatible=true``, so this hooks straight
in — same path the NPU recipe uses when it can't find a
``from_pretrained()`` call.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

from tools.backends.recipes.intel_xpu import rewrite_source  # noqa: E402


def test_no_torch_anywhere_returns_incompatible():
    """Pure CPU Python — fitz/PIL/pandas-style. Intel recipe has
    nothing to do; should refuse to build the variant."""
    src = {
        "user_script.py": (
            "import fitz\n"
            "from pathlib import Path\n"
            "for pdf in Path('/data/input').glob('*.pdf'):\n"
            "    doc = fitz.open(pdf)\n"
        ),
        "entrypoint.sh": "#!/bin/sh\npython3 /app/user_script.py\n",
    }
    out = rewrite_source(src)
    assert out.incompatible_reason
    assert "no torch" in out.incompatible_reason.lower()
    # All files still present with original-ish bodies (recipe is
    # allowed to leave them untouched when refusing the variant).
    assert set(out.files.keys()) == set(src.keys())


def test_torch_import_keeps_variant_compatible():
    """User actually uses torch — Intel is genuinely useful here.
    Recipe should accept the variant (incompatible_reason=None)."""
    src = {
        "user_script.py": (
            "import torch\n"
            "import torch.nn as nn\n"
            "model = nn.Linear(10, 1)\n"
            "x = torch.randn(1, 10)\n"
            "print(model(x))\n"
        ),
    }
    out = rewrite_source(src)
    assert out.incompatible_reason is None


def test_torch_in_shell_heredoc_keeps_variant_compatible():
    """Some nodes embed Python in an entrypoint.sh heredoc rather
    than a separate .py file. The string-level torch scan should
    still pick those up."""
    src = {
        "entrypoint.sh": (
            "#!/bin/bash\n"
            "python3 <<'PYEOF'\n"
            "import torch\n"
            "print(torch.cuda.is_available())\n"
            "PYEOF\n"
        ),
    }
    out = rewrite_source(src)
    assert out.incompatible_reason is None


def test_no_python_files_at_all_returns_incompatible():
    """Edge case: source is entirely non-Python (e.g. a Dockerfile
    wrapping a pre-built binary). Nothing for the Intel recipe to
    optimise."""
    src = {"Dockerfile": "FROM busybox\nCMD ['echo', 'hi']\n"}
    out = rewrite_source(src)
    assert out.incompatible_reason
    assert "no torch" in out.incompatible_reason.lower()


def test_torch_in_comment_or_docstring_still_keeps_variant():
    """Plain string match for 'torch' is intentional: false
    positives (e.g. a docstring that mentions 'torch') are
    cheaper than false negatives (silently skipping a real ML
    node)."""
    src = {
        "user_script.py": (
            "# This script uses torch indirectly via a helper.\n"
            "from helpers import run_model\n"
            "run_model('/data/input')\n"
        ),
    }
    out = rewrite_source(src)
    # Even though no real torch import, the string 'torch' appears
    # in the comment — fall on the safe side and build the variant.
    assert out.incompatible_reason is None


# ----------------------------------------------------------------------
# #3 — workload_incompatible flag for shell-out workloads
# ----------------------------------------------------------------------

def test_no_torch_sets_workload_incompatible_flag():
    """The 'no torch' rejection is WORKLOAD-level (no operator hand-
    port can make IPEX add value), not recipe-level. The flag tells
    the planner to suppress the stub IPEX Dockerfile so the variant
    doesn't appear in the wizard as something that could be finished
    by editing."""
    src = {
        "user_script.py": (
            "import subprocess\n"
            "subprocess.run(['openMVG_main_SfMInit_ImageListing', '-i', '/data/input'])\n"
        ),
    }
    out = rewrite_source(src)
    assert out.incompatible_reason
    assert out.workload_incompatible is True


def test_torch_workload_doesnt_set_workload_incompatible():
    """When torch is present, the recipe IS applicable — the variant
    SHOULD show up as a buildable target."""
    src = {"user_script.py": "import torch\nx = torch.tensor([1, 2])\n"}
    out = rewrite_source(src)
    assert out.incompatible_reason is None
    # On the happy path the flag is irrelevant, but it must remain
    # False — the recipe could be flapping if it set both is_compatible
    # AND workload_incompatible.
    assert out.workload_incompatible is False
