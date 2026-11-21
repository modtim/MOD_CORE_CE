"""Regression tests for `USER root` injection in synthesise_variant.

Past breakage: every MOD base image ends with `USER mod` (uid=10001)
for runtime hardening. When the wizard's hero Dockerfile (typically
FROM python:3.11-slim, root by default) was variant-swapped to
e.g. base-rocm:6.1-python3.11, every build-time `RUN apt-get install`
in the user's Dockerfile failed with `E: List directory
/var/lib/apt/lists/partial is missing. - Acquire (13: Permission
denied)` — surfaced to the user as exit code 100 with no clear cause.

Fix: synthesise_variant injects `USER root` right after the FROM/LABEL
so subsequent build-time RUNs run privileged. These tests pin the
behaviour so a future refactor doesn't accidentally drop it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_TOOLS_DIR = Path(__file__).resolve().parent.parent / "scripts" / "tools"


@pytest.fixture(scope="module")
def gpu_variants_module():
    """Load gpu_variants.py directly. We sidestep the package-level
    __init__.py which depends on llm/gitingest tooling not present in
    a unit test runner."""
    spec = importlib.util.spec_from_file_location(
        "gpu_variants_under_test",
        _TOOLS_DIR / "gpu_variants.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HERO_DOCKERFILE = """\
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y \\
    libjpeg-dev \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --index-url https://pypi.org/simple torch
COPY entrypoint.sh /app/entrypoint.sh
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
"""


class TestUserRootInjection:

    @pytest.mark.parametrize("variant", ["rocm", "cuda", "intel", "cpu", "tensorrt", "oneapi", "npu"])
    def test_synthesise_variant_injects_user_root_after_label(
        self, gpu_variants_module, variant,
    ):
        """Every variant produced by synthesise_variant MUST carry a
        `USER root` line above any `RUN` statement, otherwise build-time
        apt/pip operations fail when the underlying MOD base image ends
        with `USER mod`."""
        out = gpu_variants_module.synthesise_variant(_HERO_DOCKERFILE, variant)
        lines = out.splitlines()

        # Find the indices of FROM, USER root, and the first RUN.
        from_idx = next((i for i, l in enumerate(lines) if l.startswith("FROM ")), None)
        user_root_idx = next((i for i, l in enumerate(lines) if l.strip() == "USER root"), None)
        first_run_idx = next((i for i, l in enumerate(lines) if l.startswith("RUN ")), None)

        assert from_idx is not None, f"no FROM line in:\n{out}"
        assert user_root_idx is not None, (
            f"variant {variant!r} missing USER root injection — apt-get "
            f"RUNs will fail with 'Permission denied' against MOD base "
            f"images that end with USER mod.\n\n{out}"
        )
        assert first_run_idx is not None, f"no RUN line in:\n{out}"
        assert user_root_idx > from_idx, "USER root must come after FROM"
        assert user_root_idx < first_run_idx, (
            f"USER root must precede every RUN; got USER root at line "
            f"{user_root_idx} and first RUN at line {first_run_idx}"
        )

    def test_user_root_injected_immediately_after_label(self, gpu_variants_module):
        """Specific positional contract: FROM, then variant LABEL, then
        USER root, then everything else. Locks the directive order so
        adding a future LABEL doesn't accidentally split USER root from
        the FROM block."""
        out = gpu_variants_module.synthesise_variant(_HERO_DOCKERFILE, "rocm")
        lines = out.splitlines()
        # First three meaningful lines should be: FROM, LABEL mod.gpu_variant, USER root.
        from_line = next(l for l in lines if l.startswith("FROM "))
        from_pos = lines.index(from_line)
        assert lines[from_pos + 1] == 'LABEL mod.gpu_variant="rocm"'
        assert lines[from_pos + 2] == "USER root"

    def test_user_root_only_inserted_once_per_variant(self, gpu_variants_module):
        """Don't double-inject USER root if the hero already had one
        (paranoid defence for hand-edited heroes; current behaviour
        also inserts but is still safe — root is root). Allows up to 2
        in case the hero already declared USER root explicitly. Fail
        if more, which would point to a runaway loop."""
        hero_with_user_root = "FROM python:3.11-slim\nUSER root\nRUN apt-get update\n"
        out = gpu_variants_module.synthesise_variant(hero_with_user_root, "rocm")
        # The hero's own `USER root` plus the injected one — at most 2.
        count = sum(1 for l in out.splitlines() if l.strip() == "USER root")
        assert 1 <= count <= 2, (
            f"unexpected USER root count {count} — runaway insertion?\n{out}"
        )
