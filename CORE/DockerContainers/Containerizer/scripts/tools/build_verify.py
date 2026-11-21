"""Stage 9 — actually docker-build the generated Dockerfile.

After static validation (Stage 8) passes but before the user sees a
preview, we attempt to build the image. This catches a class of
failures that static analysis can't: missing base image on Harbor,
apt-get typos, pip resolution conflicts, COPY targets that don't
exist in the repo, layer-order problems.

A failed build feeds BACK into the refine loop as a cross-artifact
validation issue (most commonly pointing at the Dockerfile). The
refine prompt gets the first ~80 lines of `docker build` output,
which is usually enough for the LLM to identify and fix the
offending directive.

Smoke-running the built image (Stage 9b) is kept small and
opt-in-per-call via `smoke_test=True`. A real smoke test requires
synthetic input + output directories and domain-specific success
criteria that the pipeline can't always infer. The default path
verifies BUILD only; the benchmark stage (Phase 6) extends this with
actual runs.

Requires docker-outside-of-docker. The Containerizer already has
`/var/run/docker.sock` mounted for `build-and-push.sh`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.validate_static import Issue


logger = logging.getLogger(__name__)


def _join_image_ref(prefix, *parts):
    """Same rule as CORE/API/utils/registries.py join_image_ref.

    The Containerizer runs standalone in its own container (its Dockerfile
    COPYs scripts/ only) and cannot import the API util, so the rule is
    implemented inline here: an empty/None/whitespace-only/slash-only
    registry yields a BARE ref (the CE shape — the image lives on the
    building machine's own daemon); a set registry yields
    `prefix/part`, byte-identical to the old f-string. A leading '/' is
    never produced: it is not a valid Docker image reference.
    """
    if prefix is None:
        prefix = ""
    prefix = prefix.strip().rstrip("/")
    segs = [p.lstrip("/") for p in parts if p]
    if prefix:
        return f"{prefix}/{'/'.join(segs)}"
    return "/".join(segs)


BUILD_TIMEOUT_SECONDS = 1200   # 20 min — first build with base-image pull
SMOKE_TIMEOUT_SECONDS = 120    # smoke runs cap at 2 min
BUILD_LOG_TAIL_CHARS = 6000    # what we forward to the refine prompt


@dataclass
class BuildOutcome:
    success: bool = False
    stage: str = "build"            # "build" | "smoke"
    tag: str = ""                   # the temp tag used for this attempt
    image_id: Optional[str] = None  # short id if build succeeded
    log: str = ""                   # full output (stdout+stderr interleaved)
    log_tail: str = ""              # tail of log, cheap to forward to LLM
    duration_seconds: float = 0.0
    issues: list[Issue] = field(default_factory=list)


def build_and_verify(
    artifacts: dict,
    workspace: str,
    *,
    smoke_test: bool = False,
    registry_hint: str = "example.com",
) -> BuildOutcome:
    """Materialise the generated Dockerfile + entrypoint.sh in a scratch
    build context, invoke `docker build`, optionally smoke-run.

    `workspace` is the directory holding the user's codebase (the output
    of Stage 1 ingest). Its contents get copied into the build context
    so that the generated Dockerfile's `COPY . /app` (or similar) works.
    """
    tag = _join_image_ref(registry_hint, f"mod-verify:{int(time.time())}")
    outcome = BuildOutcome(tag=tag)

    with tempfile.TemporaryDirectory(prefix="mod-verify-") as ctx:
        ctx_path = Path(ctx)

        # Copy the user's codebase into the build context so COPY directives
        # can resolve. We skip the _meta.json ingest artifact.
        _copy_tree(Path(workspace), ctx_path, skip_names={"_meta.json"})

        # Place the generator outputs at context root.
        (ctx_path / "Dockerfile").write_text(artifacts.get("dockerfile", ""))
        (ctx_path / "entrypoint.sh").write_text(artifacts.get("entrypoint_sh", ""))
        (ctx_path / "build-and-push.sh").write_text(artifacts.get("build_and_push_sh", ""))
        (ctx_path / "entrypoint.sh").chmod(0o755)

        # Build
        t0 = time.time()
        logger.info("docker build starting (tag=%s, ctx=%s)", tag, ctx_path)
        try:
            r = subprocess.run(
                ["docker", "build", "-t", tag, "."],
                cwd=str(ctx_path),
                capture_output=True,
                timeout=BUILD_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            outcome.log = (e.stdout or b"").decode("utf-8", errors="replace")[-BUILD_LOG_TAIL_CHARS:]
            outcome.log_tail = outcome.log
            outcome.duration_seconds = time.time() - t0
            outcome.issues.append(Issue(
                severity="error", artifact="dockerfile",
                kind="build_timeout",
                message=f"docker build timed out after {BUILD_TIMEOUT_SECONDS}s. "
                        f"Log tail:\n{outcome.log_tail[-2000:]}",
            ))
            return outcome

        stdout = r.stdout.decode("utf-8", errors="replace")
        stderr = r.stderr.decode("utf-8", errors="replace")
        outcome.log = (stdout + stderr).strip()
        outcome.log_tail = outcome.log[-BUILD_LOG_TAIL_CHARS:]
        outcome.duration_seconds = time.time() - t0

        if r.returncode != 0:
            outcome.issues.append(Issue(
                severity="error", artifact="dockerfile",
                kind="build_failed",
                message=(
                    f"docker build exited {r.returncode}. "
                    f"Log tail (last ~2000 chars):\n{outcome.log_tail[-2000:]}"
                ),
            ))
            return outcome

        # Success — pull out the image id for completeness.
        try:
            ins = subprocess.run(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                capture_output=True, timeout=20,
            )
            outcome.image_id = ins.stdout.decode(errors="replace").strip()[:20]
        except Exception:
            pass

        outcome.success = True

        # Optional smoke test
        if smoke_test:
            outcome = _smoke_run(outcome, tag)

    # Always try to clean up the temp image so we don't leak GB per
    # wizard run. Push-back: the refine loop might want to compare
    # successive builds; that's out of scope here.
    try:
        subprocess.run(
            ["docker", "image", "rm", "-f", tag],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        logger.warning("post-build image rm failed for %s: %s", tag, e)

    return outcome


# ---------------------------------------------------------------------------
# Smoke run — run the image with a minimal synthetic /data/input and
# verify the entrypoint exits cleanly OR produces some signal in
# /data/output. MOD nodes are batch-style so "container exited 0" is
# a strong enough signal for first pass.
# ---------------------------------------------------------------------------

def _smoke_run(outcome: BuildOutcome, tag: str) -> BuildOutcome:
    outcome.stage = "smoke"
    with tempfile.TemporaryDirectory(prefix="mod-smoke-") as scratch:
        inp = Path(scratch) / "input"
        out = Path(scratch) / "output"
        inp.mkdir(); out.mkdir()
        # Drop a placeholder so entrypoint.sh's "is /data/input empty?"
        # check can succeed. The LLM's generated code may or may not
        # know how to handle the file — we're testing Dockerfile +
        # entrypoint.sh plumbing, not model correctness.
        (inp / "placeholder.txt").write_text("mod wizard smoke probe\n")

        t0 = time.time()
        try:
            r = subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{inp}:/data/input:ro",
                    "-v", f"{out}:/data/output",
                    tag,
                ],
                capture_output=True, timeout=SMOKE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            outcome.issues.append(Issue(
                severity="warn", artifact="entrypoint_sh",
                kind="smoke_timeout",
                message=f"Smoke run timed out after {SMOKE_TIMEOUT_SECONDS}s. "
                        f"The image builds, but the entrypoint may be waiting on "
                        f"real input or an external service.",
            ))
            return outcome
        elapsed = time.time() - t0
        outcome.duration_seconds += elapsed
        if r.returncode != 0:
            log = (r.stdout + r.stderr).decode("utf-8", errors="replace")
            outcome.issues.append(Issue(
                severity="warn", artifact="entrypoint_sh",
                kind="smoke_failed",
                message=f"Smoke run exited {r.returncode} in {elapsed:.1f}s. "
                        f"Log tail (last ~1500 chars):\n{log[-1500:]}",
            ))
        else:
            logger.info("Smoke run OK (%.1fs)", elapsed)
    return outcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SKIP_BUILD_CTX_NAMES = {".git", "node_modules", "__pycache__", ".venv", "venv"}


def _copy_tree(src: Path, dst: Path, *, skip_names: set) -> None:
    """Copy src into dst, skipping SKIP_BUILD_CTX_NAMES + user-supplied
    names. Walk to avoid Python's shutil.copytree requiring dst absent."""
    skip = _SKIP_BUILD_CTX_NAMES | skip_names
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip]
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f in skip:
                continue
            s = Path(root) / f
            d = dst / rel / f
            try:
                shutil.copy2(s, d)
            except OSError:
                continue
