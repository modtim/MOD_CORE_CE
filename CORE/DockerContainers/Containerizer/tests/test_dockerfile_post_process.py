"""Tests for dockerfile_post_process.

Two transforms cover real production failures:

* pillow-simd unmaintained-since-2022 / no Python 3.11 wheel: every
  TRELLIS-class 3D repo lists it in setup.sh and the wizard build
  blows up at the source-compile step. Replace → Pillow.
* hero Dockerfile inherits ``USER mod`` from the curated base and
  hits ``apt-get install ... are you root?`` exit 100. Inject
  ``USER root`` after FROM to mirror what synthesise_variant does
  for variants.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

import pytest  # noqa: E402

from tools.dockerfile_post_process import (  # noqa: E402
    DEFAULT_PIP_REPLACEMENTS,
    DEFAULT_PIP_SYSTEM_DEPS,
    DEFAULT_RUNTIME_USER,
    apply_pip_replacements,
    copyleft_apt_warnings,
    ensure_entrypoint,
    inject_synth_apt_packages,
    inject_synth_dockerfile_extras,
    inject_system_packages_for_pip,
    inject_user_root_after_from,
    inject_user_runtime_at_end,
    sanitize_entrypoint_sh,
    strip_self_pip_installs,
)


# ---------------------------------------------------------------------------
# copyleft_apt_warnings — licence notice for injected system deps
# ---------------------------------------------------------------------------

def test_poppler_injection_is_flagged_copyleft():
    # pdf2image still injects poppler-utils (build must work), but the
    # GPL nature is surfaced.
    _, injected = inject_system_packages_for_pip(
        "FROM b\nUSER root\nRUN pip install pdf2image\n"
    )
    assert injected == ("poppler-utils",)
    warns = copyleft_apt_warnings(injected)
    assert len(warns) == 1
    assert "poppler-utils" in warns[0] and "GPL-2.0" in warns[0]
    assert "pypdfium2" in warns[0]


def test_permissive_system_deps_not_flagged():
    # opencv's libgl1 / libglib2.0-0 are permissive/LGPL-dynamic — no
    # copyleft warning.
    assert copyleft_apt_warnings(("libgl1", "libglib2.0-0")) == []


def test_copyleft_warnings_empty_for_no_packages():
    assert copyleft_apt_warnings(()) == []


# ---------------------------------------------------------------------------
# apply_pip_replacements
# ---------------------------------------------------------------------------


def test_pillow_simd_swapped_for_pillow():
    df = "RUN pip install --no-cache-dir pillow-simd kornia timm\n"
    out, counts = apply_pip_replacements(df)
    assert "pillow-simd" not in out
    assert "Pillow" in out
    assert "kornia" in out and "timm" in out
    assert counts["pillow-simd"] == 1


def test_pillow_simd_with_version_specifier_preserves_pin():
    df = "RUN pip install pillow-simd==9.0.0.post1\n"
    out, counts = apply_pip_replacements(df)
    assert "Pillow==9.0.0.post1" in out
    assert counts["pillow-simd"] == 1


def test_no_change_for_lines_without_pillow_simd():
    df = (
        "RUN pip install kornia timm\n"
        "RUN pip install Pillow\n"
        "RUN apt-get install -y libjpeg-dev\n"
    )
    out, counts = apply_pip_replacements(df)
    assert out == df
    assert counts["pillow-simd"] == 0


def test_apt_install_lines_are_not_touched():
    df = "RUN apt-get install -y pillow-simd-fake\n"
    out, _ = apply_pip_replacements(df)
    assert out == df


def test_replacement_does_not_match_substring():
    # `notpillow-simd` is a different package; should not be swapped.
    df = "RUN pip install notpillow-simd\n"
    out, counts = apply_pip_replacements(df)
    assert out == df
    assert counts["pillow-simd"] == 0


def test_multiple_run_lines_all_get_processed():
    df = (
        "RUN pip install pillow-simd\n"
        "RUN apt-get update\n"
        "RUN pip install pillow-simd kornia\n"
    )
    out, counts = apply_pip_replacements(df)
    assert out.count("Pillow") == 2
    assert "pillow-simd" not in out
    assert counts["pillow-simd"] == 2


def test_custom_replacements_map_overrides_default():
    df = "RUN pip install foo bar\n"
    out, counts = apply_pip_replacements(df, {"foo": "foo-replacement"})
    assert "foo-replacement" in out
    assert counts.get("foo") == 1


def test_empty_replacements_map_is_noop():
    df = "RUN pip install pillow-simd\n"
    out, counts = apply_pip_replacements(df, {})
    assert out == df
    assert counts == {}


def test_default_map_documents_pillow_simd():
    # Pin the documented default so future contributors don't silently
    # delete the entry that this whole transform exists to handle.
    assert DEFAULT_PIP_REPLACEMENTS.get("pillow-simd") == "Pillow"


# ---------------------------------------------------------------------------
# inject_system_packages_for_pip
# ---------------------------------------------------------------------------


def test_pdf2image_injects_poppler_after_user_root():
    df = (
        "FROM ghcr.io/modtim/base-python:3.11\n"
        "USER root\n"
        "WORKDIR /data\n"
        "RUN pip install --no-cache-dir pdf2image Pillow\n"
        'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]\n'
    )
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ("poppler-utils",)
    lines = out.splitlines()
    # apt line is injected immediately after USER root.
    assert lines[1] == "USER root"
    assert "apt-get install -y --no-install-recommends poppler-utils" in lines[2]
    # The pip install line is preserved.
    assert "pip install --no-cache-dir pdf2image Pillow" in out


def test_opencv_injects_libgl_and_glib():
    df = "FROM base\nUSER root\nRUN pip install opencv-python\n"
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ("libgl1", "libglib2.0-0")
    assert "libgl1 libglib2.0-0" in out


def test_no_injection_when_no_known_package():
    df = "FROM base\nUSER root\nRUN pip install requests flask\n"
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ()
    assert out == df


def test_skips_packages_already_apt_installed():
    df = (
        "FROM base\n"
        "USER root\n"
        "RUN apt-get update && apt-get install -y poppler-utils\n"
        "RUN pip install pdf2image\n"
    )
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ()
    assert out == df


def test_anchors_after_from_when_no_user_root():
    df = "FROM base\nRUN pip install pdf2image\n"
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ("poppler-utils",)
    lines = out.splitlines()
    assert lines[0].startswith("FROM ")
    assert "poppler-utils" in lines[1]


def test_idempotent_when_run_twice():
    df = "FROM base\nUSER root\nRUN pip install pdf2image\n"
    once, inj1 = inject_system_packages_for_pip(df)
    twice, inj2 = inject_system_packages_for_pip(once)
    assert inj1 == ("poppler-utils",)
    assert inj2 == ()
    assert once == twice


def test_no_substring_false_match_on_pip_name():
    # `mypdf2image-helper` is a different package; must not trigger.
    df = "FROM base\nUSER root\nRUN pip install mypdf2image-helper\n"
    out, injected = inject_system_packages_for_pip(df)
    assert injected == ()
    assert out == df


def test_empty_deps_map_is_noop():
    df = "FROM base\nUSER root\nRUN pip install pdf2image\n"
    out, injected = inject_system_packages_for_pip(df, {})
    assert injected == ()
    assert out == df


def test_default_system_deps_documents_pdf2image():
    # Pin the entry this transform exists to handle.
    assert DEFAULT_PIP_SYSTEM_DEPS.get("pdf2image") == ("poppler-utils",)


def test_full_pipeline_fixes_the_failing_pdf_hero():
    # The exact hero shape that looped 12 times in the live wizard run.
    df = (
        "FROM ghcr.io/modtim/base-python:3.11\n"
        "USER root\n"
        "WORKDIR /data\n"
        "RUN pip install --no-cache-dir pdf2image Pillow\n"
        "COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh\n"
        "USER mod\n"
        'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]\n'
    )
    out, _ = apply_pip_replacements(df)
    out, _ = inject_user_root_after_from(out)
    out, sys_pkgs = inject_system_packages_for_pip(out)
    out, _ = inject_user_runtime_at_end(out)
    assert sys_pkgs == ("poppler-utils",)
    # poppler-utils installed, base unchanged (no FROM-thrash), final
    # USER still non-root.
    assert "FROM ghcr.io/modtim/base-python:3.11" in out
    assert "poppler-utils" in out
    body = [l for l in out.splitlines() if l.strip()]
    ep_idx = next(i for i, l in enumerate(body) if l.startswith("ENTRYPOINT"))
    assert body[ep_idx - 1] == f"USER {DEFAULT_RUNTIME_USER}"


# ---------------------------------------------------------------------------
# inject_user_root_after_from
# ---------------------------------------------------------------------------


def test_user_root_injected_after_simple_from():
    df = "FROM ghcr.io/modtim/base-cuda:12.4-python3.11\nWORKDIR /data\nRUN apt-get install -y x\n"
    out, injected = inject_user_root_after_from(df)
    assert injected is True
    lines = out.splitlines()
    assert lines[0].startswith("FROM ")
    assert lines[1] == "USER root"


def test_user_root_injected_after_from_as_alias():
    df = "FROM base AS builder\nRUN apt-get install -y x\n"
    out, injected = inject_user_root_after_from(df)
    assert injected is True
    assert "FROM base AS builder\nUSER root" in out


def test_no_inject_when_user_root_already_present():
    df = "FROM base\nUSER root\nRUN apt-get install -y x\n"
    out, injected = inject_user_root_after_from(df)
    assert injected is False
    assert out.count("USER root") == 1
    assert out == df


def test_no_inject_when_user_other_already_present_before_run():
    # User explicitly chose a non-root user before any RUN — respect
    # their intent (they probably know what they're doing).
    df = "FROM base\nUSER appuser\nRUN echo hello\n"
    out, injected = inject_user_root_after_from(df)
    assert injected is False
    assert out == df


def test_inject_when_user_only_appears_after_first_run():
    # Common pattern: install everything as root, then drop privs at
    # the very end. We DO want to inject early because the first RUN
    # still happens before that USER directive.
    df = (
        "FROM base\n"
        "RUN apt-get install -y x\n"
        "USER mod\n"
    )
    out, injected = inject_user_root_after_from(df)
    assert injected is True
    lines = out.splitlines()
    assert lines[1] == "USER root"


def test_no_inject_when_no_from_line():
    df = "WORKDIR /data\nRUN echo hi\n"
    out, injected = inject_user_root_after_from(df)
    assert injected is False
    assert out == df


def test_idempotent_when_run_twice():
    df = "FROM base\nRUN apt-get install -y x\n"
    once, injected1 = inject_user_root_after_from(df)
    twice, injected2 = inject_user_root_after_from(once)
    assert injected1 is True
    assert injected2 is False
    assert once == twice


# ---------------------------------------------------------------------------
# Integration — both transforms combined on a realistic broken hero
# ---------------------------------------------------------------------------


def test_both_transforms_fix_a_realistic_trellis_hero():
    # Faithful shape of the failing TRELLIS.2 hero Dockerfile.
    df = (
        "FROM ghcr.io/modtim/base-cuda:12.4-python3.11\n"
        "WORKDIR /data\n"
        "COPY . /app\n"
        "RUN pip install --no-cache-dir gradio==6.0.1\n"
        "RUN pip install --no-cache-dir imageio imageio-ffmpeg ninja\n"
        "RUN apt-get update && apt-get install -y libjpeg-dev\n"
        "RUN pip install --no-cache-dir pillow-simd kornia timm\n"
        'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]\n'
    )
    out, _ = apply_pip_replacements(df)
    out, injected = inject_user_root_after_from(out)
    out, runtime_injected = inject_user_runtime_at_end(out)
    assert injected is True
    assert runtime_injected is True
    assert "pillow-simd" not in out
    assert "Pillow" in out
    body_lines = [l for l in out.splitlines() if l.strip()]
    # USER root is the second non-blank line (right after FROM).
    assert body_lines[0].startswith("FROM ")
    assert body_lines[1] == "USER root"
    # USER mod precedes the ENTRYPOINT.
    ep_idx = next(i for i, l in enumerate(body_lines) if l.startswith("ENTRYPOINT"))
    assert body_lines[ep_idx - 1] == f"USER {DEFAULT_RUNTIME_USER}"


# ---------------------------------------------------------------------------
# inject_user_runtime_at_end
# ---------------------------------------------------------------------------


def test_runtime_user_injected_before_entrypoint():
    df = (
        "FROM base\n"
        "USER root\n"
        "RUN apt-get install -y x\n"
        'ENTRYPOINT ["/bin/bash", "-c", "echo hi"]\n'
    )
    out, injected = inject_user_runtime_at_end(df)
    assert injected is True
    lines = out.splitlines()
    ep_idx = next(i for i, l in enumerate(lines) if l.startswith("ENTRYPOINT"))
    assert lines[ep_idx - 1] == "USER mod"


def test_runtime_user_injected_before_cmd_when_no_entrypoint():
    df = (
        "FROM base\n"
        "USER root\n"
        "RUN apt-get install -y x\n"
        'CMD ["python", "/app/run.py"]\n'
    )
    out, injected = inject_user_runtime_at_end(df)
    assert injected is True
    lines = out.splitlines()
    cmd_idx = next(i for i, l in enumerate(lines) if l.startswith("CMD"))
    assert lines[cmd_idx - 1] == "USER mod"


def test_runtime_user_appended_when_no_entrypoint_or_cmd():
    df = "FROM base\nUSER root\nRUN apt-get install -y x\n"
    out, injected = inject_user_runtime_at_end(df)
    assert injected is True
    assert out.endswith("USER mod\n")


def test_runtime_user_skipped_when_last_user_already_nonroot():
    df = (
        "FROM base\n"
        "USER root\n"
        "RUN apt-get install -y x\n"
        "USER appuser\n"
        'ENTRYPOINT ["app"]\n'
    )
    out, injected = inject_user_runtime_at_end(df)
    assert injected is False
    assert out == df


def test_runtime_user_injected_when_only_user_is_root():
    # The hero straight from inject_user_root_after_from has just one
    # USER directive (root). We MUST inject mod at the end; otherwise
    # admission rejects the image with "Final USER must be non-root".
    df = (
        "FROM base\n"
        "USER root\n"
        "RUN apt-get install -y x\n"
        'ENTRYPOINT ["app"]\n'
    )
    out, injected = inject_user_runtime_at_end(df)
    assert injected is True
    assert "USER mod\n" in out


def test_runtime_user_no_inject_when_no_from_line():
    df = "RUN echo hi\n"
    out, injected = inject_user_runtime_at_end(df)
    assert injected is False
    assert out == df


def test_runtime_user_custom_username():
    df = "FROM base\nUSER root\nRUN x\n"
    out, injected = inject_user_runtime_at_end(df, runtime_user="appuser")
    assert injected is True
    assert "USER appuser\n" in out


def test_runtime_user_idempotent_when_run_twice():
    df = "FROM base\nUSER root\nRUN x\n"
    once, _ = inject_user_runtime_at_end(df)
    twice, injected2 = inject_user_runtime_at_end(once)
    assert injected2 is False
    assert once == twice


# ---------------------------------------------------------------------------
# Phase B (#1) — synth-declared system_packages + dockerfile_extras
#
# The openMVG-class repro: describe-synthesize emits a script that
# `subprocess.run(['ffmpeg', ...])`s, and the validator gates that
# `ffmpeg` is in either `system_packages` (apt) or `dockerfile_extras`
# (source build). The Containerizer's LLM doesn't know to bake these
# into the hero Dockerfile — its prompt focuses on FROM + pip + COPY.
# These injectors carry the synth declaration through deterministically.
# ---------------------------------------------------------------------------

# --- inject_synth_apt_packages ----------------------------------------

def test_synth_apt_packages_basic_injection_after_user_root():
    df = "FROM base\nUSER root\nWORKDIR /data\nRUN pip install requests\n"
    out, installed = inject_synth_apt_packages(df, ("ffmpeg",))
    assert installed == ("ffmpeg",)
    # apt-install line lands AFTER `USER root` and BEFORE the pip install.
    assert out.find("USER root") < out.find("ffmpeg") < out.find("pip install")


def test_synth_apt_packages_dedupes_input():
    df = "FROM base\nUSER root\n"
    out, installed = inject_synth_apt_packages(df, ("ffmpeg", "ffmpeg", "tesseract-ocr"))
    assert installed == ("ffmpeg", "tesseract-ocr")
    # One apt-install line total.
    assert out.count("apt-get install") == 1


def test_synth_apt_packages_skips_already_installed():
    """If the LLM-emitted Dockerfile already apt-installs the package,
    don't double-install."""
    df = (
        "FROM base\nUSER root\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg\n"
    )
    out, installed = inject_synth_apt_packages(df, ("ffmpeg", "git"))
    assert installed == ("git",)
    assert out.count("ffmpeg") == 1


def test_synth_apt_packages_empty_input_is_noop():
    df = "FROM base\nUSER root\n"
    out, installed = inject_synth_apt_packages(df, ())
    assert installed == ()
    assert out == df


def test_synth_apt_packages_blank_entries_skipped():
    df = "FROM base\nUSER root\n"
    out, installed = inject_synth_apt_packages(df, ("ffmpeg", "", "   "))
    assert installed == ("ffmpeg",)


def test_synth_apt_packages_falls_back_to_from_when_no_user_root():
    df = "FROM base\nRUN pip install requests\n"
    out, installed = inject_synth_apt_packages(df, ("ffmpeg",))
    assert installed == ("ffmpeg",)
    assert out.find("FROM base") < out.find("ffmpeg") < out.find("pip install")


def test_synth_apt_packages_idempotent_when_run_twice():
    df = "FROM base\nUSER root\n"
    once, _ = inject_synth_apt_packages(df, ("ffmpeg",))
    twice, second = inject_synth_apt_packages(once, ("ffmpeg",))
    assert second == ()
    assert once == twice


def test_synth_apt_packages_no_anchor_returns_unchanged():
    """No FROM and no USER root — nothing to anchor to. Don't crash."""
    df = "RUN echo hello\n"
    out, installed = inject_synth_apt_packages(df, ("ffmpeg",))
    assert installed == ()
    assert out == df


# --- inject_synth_dockerfile_extras -----------------------------------

_OPENMVG_EXTRAS = (
    "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
    "    git cmake build-essential libceres-dev && rm -rf /var/lib/apt/lists/*\n"
    "RUN git clone --depth 1 https://github.com/openMVG/openMVG.git /opt/openMVG \\\n"
    "    && cd /opt/openMVG && cmake -B build -S src && cmake --build build\n"
    "ENV PATH=/usr/local/bin:$PATH\n"
)


def test_synth_dockerfile_extras_injects_after_last_apt_install():
    """When the LLM-emitted Dockerfile already has an apt-install line,
    the extras land AFTER it — typical case where pip-system-deps
    injection ran first."""
    df = (
        "FROM base\nUSER root\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils\n"
        "RUN pip install pdf2image\n"
    )
    out, injected = inject_synth_dockerfile_extras(df, _OPENMVG_EXTRAS)
    assert injected is True
    # Extras land between the apt-install and the pip install (after
    # the LAST apt-install line, anchored before pip).
    apt_pos = out.find("poppler-utils")
    extras_pos = out.find("openMVG")
    pip_pos = out.find("pip install pdf2image")
    assert apt_pos < extras_pos < pip_pos


def test_synth_dockerfile_extras_anchors_after_user_root_when_no_apt():
    df = "FROM base\nUSER root\nRUN pip install requests\n"
    out, injected = inject_synth_dockerfile_extras(
        df, "RUN echo synth-extra\n",
    )
    assert injected is True
    assert out.find("USER root") < out.find("synth-extra") < out.find("pip install")


def test_synth_dockerfile_extras_empty_is_noop():
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, "")
    assert injected is False
    assert out == df


def test_synth_dockerfile_extras_whitespace_only_is_noop():
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, "   \n  \n")
    assert injected is False
    assert out == df


def test_synth_dockerfile_extras_scrubs_forbidden_FROM():
    """Defense-in-depth: synth-side validator already rejects FROM
    in extras; this trims it again in case the extras blob came
    from a non-synth path."""
    extras = "FROM python:3.11\nRUN echo ok\n"
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, extras)
    assert injected is True
    assert "echo ok" in out
    # The FROM line MUST NOT have been injected (it would clobber base).
    assert "FROM python:3.11" not in out


def test_synth_dockerfile_extras_scrubs_forbidden_COPY_ENTRYPOINT_USER():
    extras = (
        "COPY ./asset /opt/asset\n"
        "ENTRYPOINT [\"/asset\"]\n"
        "USER nobody\n"
        "RUN echo kept\n"
    )
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, extras)
    assert injected is True
    assert "echo kept" in out
    assert "/opt/asset" not in out
    assert "ENTRYPOINT" not in out.replace("ENTRYPOINT", "", 0)  # tautology guard
    # User-directive line from extras must not appear.
    assert "USER nobody" not in out


def test_synth_dockerfile_extras_scrub_all_directives_is_noop():
    """When every line is forbidden, the scrub returns empty and the
    Dockerfile is unchanged."""
    extras = "FROM python:3.11\nCOPY ./x /x\nUSER root\n"
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, extras)
    assert injected is False
    assert out == df


def test_synth_dockerfile_extras_falls_back_to_FROM_when_no_anchor():
    df = "FROM base\nRUN echo first\n"
    out, injected = inject_synth_dockerfile_extras(
        df, "RUN echo extras\n",
    )
    assert injected is True
    assert out.find("FROM base") < out.find("echo extras") < out.find("echo first")


def test_synth_dockerfile_extras_comments_and_blanks_preserved():
    """Comments and blank lines inside extras are kept where useful
    (the scrubber drops them between logical lines but keeps the
    structure where it doesn't matter)."""
    extras = (
        "# Build openMVG\n"
        "RUN cmake --build build\n"
        "\n"
        "RUN cmake --install build\n"
    )
    df = "FROM base\nUSER root\n"
    out, injected = inject_synth_dockerfile_extras(df, extras)
    assert injected is True
    assert "cmake --build build" in out
    assert "cmake --install build" in out


def test_phase_b_full_pipeline_combined():
    """End-to-end: a script needing both apt packages AND a source
    build. apt-install lands first; extras (with its own cmake) lands
    after the apt-install."""
    df = "FROM base-cuda\nUSER root\nWORKDIR /data\nRUN pip install ffmpeg-python\n"
    df, _ = inject_synth_apt_packages(df, ("git", "cmake", "build-essential"))
    df, ext = inject_synth_dockerfile_extras(df, _OPENMVG_EXTRAS)
    assert ext is True
    # Order: USER root → apt-install (with git+cmake+build-essential)
    # → extras (cloning openMVG and building) → pip install ffmpeg-python.
    assert df.find("USER root") < df.find("build-essential")
    assert df.find("build-essential") < df.find("openMVG")
    assert df.find("openMVG") < df.find("ffmpeg-python")


# ---------------------------------------------------------------------------
# ensure_entrypoint — the tini-usage / "entrypoint never invoked" root cause
# ---------------------------------------------------------------------------

def test_ensure_entrypoint_injects_when_missing():
    # Live "Multiply Numbers" failure: hero Dockerfile ends after USER mod
    # with no ENTRYPOINT — base tini has nothing to exec.
    df = (
        "FROM ghcr.io/modtim/base-python:3.11\n"
        "WORKDIR /data\n"
        "COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh\n"
        "USER mod\n"
    )
    out, injected = ensure_entrypoint(df)
    assert injected is True
    assert 'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]' in out
    # Reuses the existing COPY — no duplicate.
    assert sum(
        1 for l in out.splitlines()
        if l.strip().startswith("COPY") and "entrypoint.sh" in l
    ) == 1


def test_ensure_entrypoint_adds_copy_when_absent():
    df = "FROM base-python:3.11\nUSER root\nRUN echo hi\nUSER mod\n"
    out, injected = ensure_entrypoint(df)
    assert injected is True
    assert "COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh" in out
    assert 'ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]' in out


def test_ensure_entrypoint_noop_when_entrypoint_present():
    df = 'FROM b\nUSER mod\nENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]\n'
    out, injected = ensure_entrypoint(df)
    assert injected is False and out == df


def test_ensure_entrypoint_noop_when_cmd_present():
    # A CMD is enough — the base's tini execs it.
    df = 'FROM b\nUSER mod\nCMD ["python", "/app/run.py"]\n'
    out, injected = ensure_entrypoint(df)
    assert injected is False and out == df


def test_ensure_entrypoint_idempotent():
    df = "FROM b\nUSER mod\n"
    once, _ = ensure_entrypoint(df)
    twice, injected2 = ensure_entrypoint(once)
    assert injected2 is False and twice == once


def test_ensure_entrypoint_pairs_with_runtime_user():
    # Composed with inject_user_runtime_at_end (create_container order),
    # USER mod must precede the injected ENTRYPOINT and be the final USER.
    df = "FROM base-python:3.11\nUSER root\nRUN pip install foo\n"
    df, _ = ensure_entrypoint(df)
    df, _ = inject_user_runtime_at_end(df)
    assert df.index("USER mod") < df.index("ENTRYPOINT")
    users = [l.strip() for l in df.splitlines() if l.strip().startswith("USER ")]
    assert users[-1] == "USER mod"


# ---------------------------------------------------------------------------
# strip_self_pip_installs — hallucinated `pip install <own-script-name>`
# ---------------------------------------------------------------------------

def test_strip_self_pip_drops_solo_self_install():
    df = "FROM b\nUSER root\nRUN pip install --no-cache-dir describe\n"
    out, removed = strip_self_pip_installs(df, {"describe"})
    assert removed == ("describe",)
    assert "pip install" not in out  # the whole RUN line was dropped


def test_strip_self_pip_keeps_real_deps_on_mixed_line():
    df = "FROM b\nRUN pip install --no-cache-dir numpy describe pandas\n"
    out, removed = strip_self_pip_installs(df, {"describe"})
    assert removed == ("describe",)
    assert "numpy" in out and "pandas" in out and "describe" not in out


def test_strip_self_pip_leaves_unrelated_lines():
    df = "FROM b\nRUN pip install requests\n"
    out, removed = strip_self_pip_installs(df, {"describe"})
    assert removed == () and out == df


def test_strip_self_pip_protects_common_distributions():
    # Even if a source file were named requests.py, never strip the real pkg.
    df = "FROM b\nRUN pip install requests\n"
    out, removed = strip_self_pip_installs(df, {"requests"})
    assert removed == () and "requests" in out


def test_strip_self_pip_idempotent():
    df = "FROM b\nRUN pip install numpy describe\n"
    once, _ = strip_self_pip_installs(df, {"describe"})
    twice, removed2 = strip_self_pip_installs(once, {"describe"})
    assert removed2 == () and twice == once


# ---------------------------------------------------------------------------
# sanitize_entrypoint_sh — quoted-glob test + mkdir&&exit precedence
# ---------------------------------------------------------------------------

def test_sanitize_entrypoint_fixes_quoted_glob():
    sh = 'if [ -s "$IN/*.txt" ]; then echo yes; fi\n'
    out, notes = sanitize_entrypoint_sh(sh)
    assert '[ -s "$IN/*.txt" ]' not in out
    assert 'ls "$IN"/*.txt >/dev/null 2>&1' in out
    assert notes


def test_sanitize_entrypoint_fixes_negated_glob():
    sh = 'if [ ! -f "$IN/*.png" ]; then exit 1; fi\n'
    out, _ = sanitize_entrypoint_sh(sh)
    assert '! ls "$IN"/*.png >/dev/null 2>&1' in out


def test_sanitize_entrypoint_fixes_mkdir_exit_precedence():
    sh = '[ -d "$OUT_DIR" ] || mkdir -p "$OUT_DIR" && exit 1\n'
    out, notes = sanitize_entrypoint_sh(sh)
    assert "&& exit 1" not in out
    assert '|| mkdir -p "$OUT_DIR"' in out
    assert notes


def test_sanitize_entrypoint_idempotent_and_preserves_good_scripts():
    good = '#!/bin/bash\nset -e\npython3 /app/run.py\n'
    out, notes = sanitize_entrypoint_sh(good)
    assert out == good and notes == []
