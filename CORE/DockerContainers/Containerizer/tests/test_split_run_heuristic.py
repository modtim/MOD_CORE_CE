"""Tests for the split-RUN heuristic (issue #2b/#2d).

The Containerizer's LLM emits mega-RUN pip install blocks. When any
single package fails, the user sees a buried error inside a 14-package
install. The heuristic post-processes the Dockerfile so each VCS dep,
each version-pinned dep, and each apt package gets its own RUN line —
build logs then name the offending package on its own line.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the Containerizer's `tools` package importable from the tests dir.
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

import pytest  # noqa: E402

from tools.split_run_heuristic import (  # noqa: E402
    DEFAULT_THRESHOLD,
    _classify_pip_tokens,
    _is_pinned_pkg,
    _is_vcs_pkg,
    _parse_pip_install_line,
    _parse_apt_install_line,
    split_long_runs,
)


# ---------------------------------------------------------------------------
# Token classifiers
# ---------------------------------------------------------------------------

class TestIsVcsPkg:
    @pytest.mark.parametrize("tok", [
        "git+https://github.com/foo/bar.git",
        "git+https://github.com/foo/bar.git@abc123",
        "git://github.com/foo/bar.git",
        "hg+https://hg.example.com/repo",
        "svn+https://example.com/svn/trunk",
        "bzr+lp:project",
    ])
    def test_vcs_specs(self, tok):
        assert _is_vcs_pkg(tok) is True

    @pytest.mark.parametrize("tok", [
        "numpy", "torch==2.1.0", "pillow>=10",
        "package[extra]", "package[extra]==1.0",
    ])
    def test_non_vcs(self, tok):
        assert _is_vcs_pkg(tok) is False


class TestIsPinnedPkg:
    @pytest.mark.parametrize("tok", [
        "torch==2.6.0", "numpy>=1.20", "pillow<=11.0",
        "scipy~=1.10", "foo!=2.0", "bar>1.0", "baz<3.0",
        "pkg[extra]==1.2.3",
    ])
    def test_pinned(self, tok):
        assert _is_pinned_pkg(tok) is True

    @pytest.mark.parametrize("tok", [
        "numpy", "pillow", "package[extra]",
        "git+https://example.com/repo.git",  # VCS isn't classified pinned
    ])
    def test_unpinned(self, tok):
        assert _is_pinned_pkg(tok) is False


class TestClassifyPipTokens:
    def test_three_buckets(self):
        vcs, pinned, plain = _classify_pip_tokens([
            "numpy",
            "torch==2.1.0",
            "git+https://github.com/foo/bar.git",
            "pillow>=10",
            "requests",
        ])
        assert vcs == ["git+https://github.com/foo/bar.git"]
        assert pinned == ["torch==2.1.0", "pillow>=10"]
        assert plain == ["numpy", "requests"]

    def test_preserves_order_within_bucket(self):
        vcs, pinned, plain = _classify_pip_tokens(["b", "a", "c"])
        assert plain == ["b", "a", "c"]


# ---------------------------------------------------------------------------
# Line parsers
# ---------------------------------------------------------------------------

class TestParsePipInstallLine:
    def test_returns_none_for_non_pip_run(self):
        assert _parse_pip_install_line("RUN apt-get update") is None
        assert _parse_pip_install_line("FROM python:3.11") is None
        assert _parse_pip_install_line("WORKDIR /app") is None

    def test_simple_three_packages(self):
        flags, pkgs = _parse_pip_install_line(
            "RUN pip install numpy pillow requests"
        )
        assert flags == []
        assert pkgs == ["numpy", "pillow", "requests"]

    def test_no_cache_dir_flag_forwarded(self):
        flags, pkgs = _parse_pip_install_line(
            "RUN pip install --no-cache-dir numpy pillow"
        )
        assert flags == ["--no-cache-dir"]
        assert pkgs == ["numpy", "pillow"]

    def test_extra_index_url_consumes_value(self):
        flags, pkgs = _parse_pip_install_line(
            "RUN pip install --no-cache-dir --extra-index-url https://example.com/wheels torch==2.1.0"
        )
        assert flags == ["--no-cache-dir", "--extra-index-url", "https://example.com/wheels"]
        assert pkgs == ["torch==2.1.0"]

    def test_pip3_form(self):
        flags, pkgs = _parse_pip_install_line("RUN pip3 install numpy")
        assert flags == []
        assert pkgs == ["numpy"]


class TestParseAptInstallLine:
    def test_returns_none_for_non_apt(self):
        assert _parse_apt_install_line("RUN pip install numpy") is None
        assert _parse_apt_install_line("FROM python:3.11") is None

    def test_returns_none_for_apt_update_chain(self):
        # We deliberately don't split lines that prefix `apt-get update &&`
        # because reordering update vs install would break the install.
        result = _parse_apt_install_line(
            "RUN apt-get update && apt-get install -y libjpeg-dev"
        )
        assert result is None

    def test_simple_install(self):
        flags, pkgs, trailing = _parse_apt_install_line(
            "RUN apt-get install -y libjpeg-dev libpng-dev"
        )
        assert flags == ["-y"]
        assert pkgs == ["libjpeg-dev", "libpng-dev"]
        assert trailing == ""

    def test_captures_cleanup_tail(self):
        flags, pkgs, trailing = _parse_apt_install_line(
            "RUN apt-get install -y libjpeg-dev && rm -rf /var/lib/apt/lists/*"
        )
        assert flags == ["-y"]
        assert pkgs == ["libjpeg-dev"]
        assert trailing == "rm -rf /var/lib/apt/lists/*"


# ---------------------------------------------------------------------------
# Top-level transform
# ---------------------------------------------------------------------------

class TestSplitLongRuns:
    def test_passthrough_short_pip_install(self):
        # 3 packages, below threshold (default 5) — no split.
        df = "RUN pip install numpy pillow requests"
        assert split_long_runs(df) == df

    def test_passthrough_at_threshold(self):
        # Exactly threshold packages — still no split.
        df = "RUN pip install a b c d e"
        assert split_long_runs(df, threshold=5) == df

    def test_splits_above_threshold(self):
        # 6 packages above default threshold of 5 — should split.
        df = "RUN pip install a b c d e f"
        result = split_long_runs(df)
        # All 6 are plain → one grouped RUN, no real split. The heuristic
        # ONLY emits when split is informative (> 1 RUN).
        assert result == df

    def test_splits_pinned_into_individual_runs(self):
        df = (
            "RUN pip install --no-cache-dir "
            "numpy pillow torch==2.1.0 transformers gradio==4.44.0 requests"
        )
        result = split_long_runs(df)
        # 6 packages: pinned=[torch==2.1.0, gradio==4.44.0], plain=[the rest]
        # Expect: 1 RUN per pinned + 1 grouped RUN for plain.
        assert "RUN pip install --no-cache-dir torch==2.1.0" in result
        assert "RUN pip install --no-cache-dir gradio==4.44.0" in result
        assert "RUN pip install --no-cache-dir numpy pillow transformers requests" in result
        # The original mega-line is gone.
        assert df not in result

    def test_splits_vcs_into_isolated_runs(self):
        df = (
            "RUN pip install --no-cache-dir "
            "imageio imageio-ffmpeg tqdm easydict opencv-python-headless ninja "
            "trimesh transformers gradio==4.44.0 tensorboard pandas lpips zstandard "
            "kornia timm git+https://github.com/EasternJournalist/utils3d.git@abc123 "
            "pillow-simd"
        )
        result = split_long_runs(df)
        # The VCS dep gets its own line.
        assert (
            "RUN pip install --no-cache-dir "
            "git+https://github.com/EasternJournalist/utils3d.git@abc123"
        ) in result
        # The pinned gradio gets its own line.
        assert "RUN pip install --no-cache-dir gradio==4.44.0" in result

    def test_apt_get_install_splits_per_package(self):
        df = (
            "RUN apt-get install -y --no-install-recommends "
            "libjpeg-dev libpng-dev libtiff-dev libgl1 libglib2.0-0 libsm6"
        )
        result = split_long_runs(df)
        # 6 apt packages > 5 threshold → one RUN per package.
        for pkg in ["libjpeg-dev", "libpng-dev", "libtiff-dev", "libgl1", "libglib2.0-0", "libsm6"]:
            assert f"RUN apt-get install -y --no-install-recommends {pkg}" in result

    def test_apt_cleanup_attaches_to_last_emitted_run(self):
        df = (
            "RUN apt-get install -y "
            "libjpeg-dev libpng-dev libtiff-dev libgl1 libglib2.0-0 libsm6 "
            "&& rm -rf /var/lib/apt/lists/*"
        )
        result = split_long_runs(df)
        lines = [l for l in result.split("\n") if "apt-get install" in l]
        assert len(lines) == 6
        # The cleanup is on the LAST line only.
        for l in lines[:-1]:
            assert "rm -rf" not in l
        assert "&& rm -rf /var/lib/apt/lists/*" in lines[-1]

    def test_multiline_pip_install_coalesced_then_split(self):
        df = (
            "RUN pip install --no-cache-dir \\\n"
            "    numpy pillow \\\n"
            "    torch==2.1.0 \\\n"
            "    git+https://github.com/foo/bar.git \\\n"
            "    transformers gradio==4.44.0"
        )
        result = split_long_runs(df)
        assert "RUN pip install --no-cache-dir torch==2.1.0" in result
        assert "RUN pip install --no-cache-dir gradio==4.44.0" in result
        assert "RUN pip install --no-cache-dir git+https://github.com/foo/bar.git" in result
        assert "RUN pip install --no-cache-dir numpy pillow transformers" in result

    def test_passes_through_other_dockerfile_lines(self):
        df = (
            "FROM python:3.11-slim\n"
            "WORKDIR /app\n"
            "COPY . .\n"
            "RUN pip install --no-cache-dir a==1 b==2 c==3 d==4 e==5 f==6\n"
            "EXPOSE 8080\n"
            "CMD [\"python\", \"app.py\"]\n"
        )
        result = split_long_runs(df)
        assert "FROM python:3.11-slim" in result
        assert "WORKDIR /app" in result
        assert "EXPOSE 8080" in result
        assert "CMD [\"python\", \"app.py\"]" in result
        # All pinned packages got individual RUNs.
        for pkg in ("a==1", "b==2", "c==3", "d==4", "e==5", "f==6"):
            assert f"RUN pip install --no-cache-dir {pkg}" in result

    def test_empty_input(self):
        assert split_long_runs("") == ""

    def test_below_threshold_no_change(self):
        df = "RUN pip install --no-cache-dir torch==2.1.0 numpy pillow"
        assert split_long_runs(df, threshold=5) == df
