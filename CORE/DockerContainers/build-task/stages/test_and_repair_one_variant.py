"""B4 — auto-iterating build/test/repair loop for the hero variant.

Replaces the linear `build → smoke → push` flow for the hero with an
iterative one:

    build → smoke → (pass: push) | (fail: LLM repair → goto build)

The loop runs up to 30 inner-mode iterations (script-only fixes) and
10 outer-mode iterations (Dockerfile rewrites). Each iteration calls
the API's /wizard/repair-artifacts endpoint, which mutates the
BuildSpec in place; this stage just re-pulls the spec on each pass
and rebuilds from the latest artifacts.

Cost model: on-prem wizard LLM, so each repair call is effectively
free. The dominant cost is docker layer caching — script-only changes
hit the cache up to the COPY scripts step, so inner iterations are
fast (<10s rebuilds typical). Outer iterations fully rebuild.

Progress is emitted to stdout as line-prefixed events so the
WorkflowBuilder UI's build-progress component can render real-time
"Attempt 3/30: smoke failed, calling LLM repair..." messages.

Exit codes:
  0  → smoke passed (image is ready to push)
  1  → both budgets exhausted; final smoke report is in the payload
  2  → infrastructure error (API unreachable, docker socket down, etc.)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from typing import Dict, List, Optional, Tuple

import requests

from _common import (
    build_command, build_subprocess_env, build_timeout_s,
    check_docker_disk_space, docker_login, env, fetch_build_spec,
    log, min_free_gb_default,
    probe_network, report_back, resolve_data_host_path,
    resolve_data_volume_name,
)
import remediation_client
from _synth_input import (
    collect_outputs, docker_device_args_for_variant, has_real_output,
    looks_like_runtime_failure, looks_like_missing_accelerator,
    output_matches_declared_type, synth_input_for,
)


def _classify_for_outer(stderr: str) -> bool:
    """Mirror node_builder.repair_loop.classify_failure_mode without
    needing the API package import — the stage is a thin docker shim.
    Returns True iff this failure needs an outer-mode (Dockerfile)
    repair."""
    if not stderr:
        return False
    OUTER_TRIGGERS = (
        "ModuleNotFoundError",
        "ImportError: No module named",
        "executable file not found",
        "not found in $PATH",
        "command not found",
        "/bin/sh: 1: ",
        "/bin/bash: line ",
    )
    EXEMPTIONS = (
        "command not found: /app/scripts/",
        "command not found: /data/",
    )
    for ex in EXEMPTIONS:
        if ex in stderr:
            return False
    return any(tr in stderr for tr in OUTER_TRIGGERS)


class _SkipSync(Exception):
    """Sentinel raised to skip the hero-only sync-variants-from-hero call
    from the non-hero variant branch without hitting the broad ``except
    Exception`` that follows the call (which would log it as a warning)."""


def _resolve_variant_dockerfile(spec: dict, variant: str) -> str:
    """Return the Dockerfile content for ``variant``. Hero uses
    spec.hero_dockerfile (legacy); non-hero variants look up the matching
    entry in spec.variants[] — that's where per-accelerator Dockerfiles
    live (CUDA wheel, ROCm base image, …). Returns "" if the variant key
    isn't found, which the caller's docker build will fail on."""
    if variant == "hero":
        return spec.get("hero_dockerfile") or ""
    match = next(
        (v for v in (spec.get("variants") or []) if v.get("variant") == variant),
        None,
    )
    if match is None:
        return ""
    return match.get("dockerfile") or ""


def _materialise_build_context(spec: dict, ctx_dir: str, variant: str = "hero") -> None:
    """Drop the spec's Dockerfile + scripts + source files into ctx_dir
    so we can `docker build` it. Each iteration starts fresh — the
    previous iteration's stale files are wiped via shutil.rmtree.

    For non-hero variants we use the variant-specific Dockerfile (from
    spec.variants[]); entrypoint + scripts + source files are shared
    across all variants (hero's loop propagates them through
    /wizard/sync-variants-from-hero on PASS)."""
    with open(os.path.join(ctx_dir, "Dockerfile"), "w") as f:
        f.write(_resolve_variant_dockerfile(spec, variant))
    if spec.get("entrypoint_sh"):
        with open(os.path.join(ctx_dir, "entrypoint.sh"), "w") as f:
            f.write(spec["entrypoint_sh"])
        os.chmod(os.path.join(ctx_dir, "entrypoint.sh"), 0o755)
    if spec.get("build_and_push_sh"):
        with open(os.path.join(ctx_dir, "build_and_push.sh"), "w") as f:
            f.write(spec["build_and_push_sh"])
        os.chmod(os.path.join(ctx_dir, "build_and_push.sh"), 0o755)
    for sf in (spec.get("source_files") or []):
        fname = sf.get("filename")
        content = sf.get("content")
        if not fname or content is None:
            continue
        # Path-traversal guard: the validator on the API side rejects
        # ".." and absolute paths in script filenames; double-check here
        # so a misbehaving spec can't escape the build context.
        if ".." in fname or fname.startswith("/"):
            continue
        target = os.path.join(ctx_dir, fname)
        os.makedirs(os.path.dirname(target) or ctx_dir, exist_ok=True)
        with open(target, "w") as f:
            f.write(content)


def _build_image(ctx_dir: str, tag: str) -> Tuple[bool, str]:
    """Run `docker build` against ctx_dir. Returns (ok, output_tail).

    Uses `docker buildx build --output=type=docker` (same pattern as
    build_one_variant.py) instead of plain `docker build`. The
    default buildx output is `type=cacheonly`, which leaves the
    image in BuildKit's container cache rather than the daemon's
    image store at /var/lib/docker/overlay2/. skopeo's
    `docker-daemon:` transport in push_one_variant.py reads ONLY
    from the daemon's image store and returns "No such image"
    against cache-only images, even though the in-stage smoke
    probe sees the image fine (docker run auto-loads from buildkit).
    `--output=type=docker` forces the load. `--provenance=false` +
    `--sbom=false` drop the attestation manifests that Harbor
    rejects. Manual-test repro 2026-05-22 spec=33 (Python88) —
    push_variant failed with `daemon-MISSING-tag` even after a
    successful in-stage smoke probe; this fix is the symmetric
    twin of the build_one_variant.py fix from the legacy path.
    """
    # GSEC FX.16 R-023: no API_KEY / registry creds in the buildx child
    # env (untrusted Dockerfile surface); daemon auth comes from the
    # earlier `docker login --password-stdin`.
    build_env = build_subprocess_env()
    # Two guards. (1) INACTIVITY-based timeout: a build still making
    # progress (e.g. slowly downloading a multi-GB wheel/base over a poor link)
    # keeps emitting buildx `--progress=plain` lines and is NEVER killed — a slow
    # network is not a build failure. We only kill a build that goes SILENT (no
    # output) for inactivity_s (genuinely hung). (2) a wall-clock cap
    # (BUILD_TIMEOUT_S, default 3600s, "0" disables) for a build that keeps
    # emitting output but never finishes; the worker's per-task
    # container_timeout stays the outer backstop across repair attempts.
    inactivity_s = int(os.getenv("BUILD_INACTIVITY_TIMEOUT_S", "300"))   # 5 min idle = hung
    import time as _time
    import threading as _threading
    from collections import deque as _deque

    wall_s = build_timeout_s()
    started_at = _time.time()
    proc = subprocess.Popen(
        build_command(tag, ctx_dir, progress="plain"),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=build_env,
    )
    tail = _deque(maxlen=400)
    last_activity = [_time.time()]

    def _drain():
        assert proc.stdout is not None
        for line in proc.stdout:
            tail.append(line.rstrip("\n"))
            last_activity[0] = _time.time()

    reader = _threading.Thread(target=_drain, daemon=True)
    reader.start()

    killed_reason = None
    while proc.poll() is None:
        _time.sleep(2)
        if _time.time() - last_activity[0] > inactivity_s:
            killed_reason = f"no build progress for {inactivity_s}s — treating as hung"
            proc.kill()
            break
        if wall_s is not None and _time.time() - started_at > wall_s:
            killed_reason = f"build exceeded BUILD_TIMEOUT_S={wall_s}s"
            proc.kill()
            break
    reader.join(timeout=5)
    output = "\n".join(tail)
    if killed_reason is not None:
        return False, (output[-1800:] + f"\n[build killed: {killed_reason}]")
    return proc.returncode == 0, output[-2000:]


def _assert_smoke_input_visible(in_dir: str, fname: str, size: int) -> None:
    """Pre-run guard: the staged synth input must be present and
    non-empty where the probe container will read it, or we refuse to
    run the node against nothing.

    Without this, a staging/mount failure (e.g. the daemon handing the
    probe an auto-created empty dir) surfaces only as a confusing
    "produced no output" after the node's input loop ran 0 times —
    and the LLM repair loop then thrashes on a bug that isn't in the
    script (WS6.8d: hflip/blur smokes failed this way while the same
    script passed in another run).
    """
    staged = os.path.join(in_dir, fname)
    if not os.path.isfile(staged) or os.path.getsize(staged) == 0:
        raise RuntimeError(
            f"smoke input staging empty: {staged} "
            f"({size} bytes expected for this input type) is not visible "
            f"under {in_dir} after staging — refusing to run the node "
            f"against an empty /data/input"
        )


def _smoke_probe(
    tag: str, input_type: str, *,
    data_host_path: Optional[str],
    expected_output_type: Optional[str] = None,
    variant: str = "hero",
    probe_net: str = "none",
    data_volume_name: Optional[str] = None,
    data_root: str = "/data",
) -> dict:
    """Run the image against synth input. Returns the failure-context
    dict expected by /wizard/repair-artifacts.

    Mounting strategy (in priority order):

    1. `data_volume_name` set — /data IS a docker named volume. The
       nested probe mounts the SAME named volume (`docker run -v
       {vol}:/data`), so the probe reads /data/input and writes
       /data/output on the very bytes this container already has at
       /data. No host-path indirection at all. This is the only
       strategy that works on Docker-Desktop/WSL2 hosts, where the
       daemon lives in a VM and the volume's on-host `Mountpoint`
       (/var/lib/docker/volumes/<vol>/_data) does not exist on the
       daemon's filesystem — a bind-mount of that path silently
       auto-creates an EMPTY dir and the node runs with 0 input
       files (WS6.8d repro: hflip/blur, 09-29 s2_golden).
       Because the probe then sees the task's canonical /data/input
       and /data/output, they are cleaned (recreated + 0o777) before
       every probe so a stale file can never be mistaken for the
       node's output.

    2. `data_host_path` set (bind-mounted /data, e.g. bare-metal
       Linux where the daemon shares the host filesystem) — probe
       dirs live under /data and we pass the HOST path to the nested
       `docker run -v` so the daemon resolves it correctly.

    3. neither (running outside docker, dev) — tempfile.mkdtemp;
       that path doesn't survive the docker daemon boundary, so the
       smoke probe in that mode is best-effort.

    Raises RuntimeError("smoke input staging empty ...") when the
    staged input is not visible pre-run — the caller must fail
    loudly instead of letting the node run on nothing.
    """
    probe_id = uuid.uuid4().hex[:12]
    use_volume = bool(data_volume_name)
    if use_volume:
        # Canonical dirs on the shared volume (strategy 1). Cleaned
        # below so the probe never sees leftovers. `data_root` is the
        # container-side /data equivalent (overridable for unit tests
        # that cannot write to /data).
        in_dir_container = os.path.join(data_root, "input")
        out_dir_container = os.path.join(data_root, "output")
        in_dir_host = out_dir_host = ""
    elif data_host_path:
        # /data is a docker volume; carve out a probe-specific subdir.
        # Container-side path (used by os.makedirs / chmod / file writes)
        # and host-side path (used by `docker run -v`) point at the
        # same inode.
        in_dir_container = f"/data/probe-{probe_id}/input"
        out_dir_container = f"/data/probe-{probe_id}/output"
        in_dir_host = f"{data_host_path}/probe-{probe_id}/input"
        out_dir_host = f"{data_host_path}/probe-{probe_id}/output"
    else:
        tmp = tempfile.mkdtemp(prefix=f"probe-{probe_id}-")
        in_dir_container = os.path.join(tmp, "input")
        out_dir_container = os.path.join(tmp, "output")
        in_dir_host = in_dir_container
        out_dir_host = out_dir_container

    if use_volume:
        # Wipe any stale content so a leftover file can't be mistaken
        # for the node's output and the node can't "process" a foreign
        # input. (The task's build context lives in /tmp, not /data.)
        shutil.rmtree(in_dir_container, ignore_errors=True)
        shutil.rmtree(out_dir_container, ignore_errors=True)
        os.makedirs(in_dir_container, exist_ok=True)
        os.makedirs(out_dir_container, exist_ok=True)
    else:
        os.makedirs(in_dir_container, exist_ok=True)
        os.makedirs(out_dir_container, exist_ok=True)
    try:
        fname, size = synth_input_for(input_type, in_dir_container)
        # 0o777 so a non-root USER inside the wizard container can
        # write to /data/output. The wizard's base images set
        # USER mod (uid 10001); without this chmod, mkdir of any
        # subpath fails with Permission denied.
        os.chmod(out_dir_container, 0o777)
        # Same chmod on the probe-{id} parent so the container can
        # `ls` and `cd` through it. Without 0o755+, a non-root user
        # can't even SEE /data/output through /data/probe-{id}.
        os.chmod(os.path.dirname(out_dir_container), 0o755)
        _assert_smoke_input_visible(in_dir_container, fname, size)
        device_args = list(docker_device_args_for_variant(variant))
        log(f"[test-and-repair] probe network={probe_net}")
        if use_volume:
            run_args = ["docker", "run", "--rm", f"--network={probe_net}",
                        *device_args,
                        "-v", f"{data_volume_name}:/data",
                        tag]
        else:
            run_args = ["docker", "run", "--rm", f"--network={probe_net}",
                        *device_args,
                        "-v", f"{in_dir_host}:/data/input:ro",
                        "-v", f"{out_dir_host}:/data/output:rw",
                        tag]
        res = subprocess.run(
            run_args,
            capture_output=True, text=True, timeout=180,
        )
        out_files = collect_outputs(out_dir_container)

        # Three-layer gate (matches sample_one_test). The convergence loop
        # treats produced_output=False as a failure → fires LLM repair. We
        # used to set produced_output=any-non-empty-file, which let an
        # error-log file ("Is poppler installed and in PATH?") look like
        # real output → loop exited, broken node was registered. The
        # tightened gate makes each of these classes of failure a repair-
        # eligible signal:
        #
        #   1. exit_code != 0 (unchanged — process error / non-zero exit)
        #   2. NO non-empty file in /data/output (unchanged — entrypoint
        #      did no work)
        #   3. stdout/stderr contains a runtime-failure marker (NEW)
        #   4. produced files don't match the declared output_type by
        #      extension + magic bytes (NEW)
        #
        # On a strict-gate failure we synthesize a stderr_tail line that
        # names the failure mode and replaces the raw container stderr with
        # something the LLM repair endpoint can act on (the raw stderr is
        # usually empty when the script swallowed the error; the diagnostic
        # we add tells the LLM exactly what to fix).
        raw_stdout = (res.stdout or "")[-2000:]
        raw_stderr = (res.stderr or "")[-2000:]
        any_non_empty = has_real_output(out_files)
        runtime_failure = looks_like_runtime_failure(raw_stdout, raw_stderr)
        type_ok, type_reason = output_matches_declared_type(
            out_dir_container, out_files, expected_output_type,
        )
        accelerator_absence = looks_like_missing_accelerator(
            raw_stdout, raw_stderr, variant,
        )
        gate_pass = (
            res.returncode == 0
            and any_non_empty
            and runtime_failure is None
            and type_ok
            and accelerator_absence is None
        )
        diagnostic_lines: list[str] = []
        if runtime_failure is not None:
            diagnostic_lines.append(
                f"STRICT-GATE: runtime-failure marker {runtime_failure!r} "
                f"present in stdout/stderr — the script caught an error and "
                f"exited 0. Fix the root cause (e.g. missing apt package, "
                f"wrong import, missing CLI tool) instead of writing an "
                f"error log to /data/output."
            )
        if not type_ok:
            diagnostic_lines.append(
                f"STRICT-GATE: output does not match declared output_type="
                f"{expected_output_type!r}: {type_reason}"
            )
        if accelerator_absence is not None:
            diagnostic_lines.append(
                f"STRICT-GATE: variant={variant!r} expected an accelerator "
                f"but stdout/stderr says {accelerator_absence!r} — the script "
                f"silently fell back to CPU. A 'verified {variant}' badge "
                f"must mean the variant actually used its accelerator. Fix "
                f"the variant's Dockerfile (CUDA wheel, runtime base image, "
                f"LD_LIBRARY_PATH, etc.) or remove the variant if it can't "
                f"support this hardware."
            )
        if diagnostic_lines and res.returncode == 0:
            raw_stderr = (
                "\n".join(diagnostic_lines)
                + "\n--- original stderr tail ---\n"
                + raw_stderr
            )[-2000:]
        return {
            "exit_code": res.returncode,
            "stdout_tail": raw_stdout,
            "stderr_tail": raw_stderr,
            "output_files": out_files,
            "produced_output": gate_pass,
            "runtime_failure_marker": runtime_failure,
            "type_check_ok": type_ok,
            "type_check_reason": type_reason,
            "accelerator_absence_marker": accelerator_absence,
            "variant": variant,
            "expected_output_type": expected_output_type,
            "input_seed": {"source": "synth_stub", "filename": fname,
                           "size": size, "input_type": input_type},
        }
    finally:
        # Best-effort cleanup of the probe-{id} subdir so /data doesn't
        # accumulate forgotten probe trees across iterations. In
        # volume mode the probe ran on the task's canonical
        # /data/input and /data/output — remove only the probe's own
        # files (the node's outputs are collected first; the task
        # volume is deleted by the worker when the build task ends).
        try:
            if use_volume:
                for f in os.listdir(in_dir_container):
                    try:
                        os.remove(os.path.join(in_dir_container, f))
                    except OSError:
                        pass
            else:
                parent = os.path.dirname(out_dir_container) if data_host_path else os.path.dirname(in_dir_container)
                shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------
# --help grounding (wizard-grounding-learning-program piece 1)
#
# When a smoke run fails because the script invoked a command that
# doesn't exist (or drove a real tool with a hallucinated sub-command —
# the OpenMVS `openMVS_main <subcmd>` class), the repair LLM used to get
# only "command not found" and would guess again. Here we PROBE THE
# ACTUAL IMAGE for ground truth: which of the called binaries exist,
# their real `--help`, and the list of executables actually installed.
# That reality is threaded into the repair prompt so the LLM corrects
# against the true interface instead of re-inventing one.
#
# Safety: the probe reuses the already-built image with --network=none
# (no egress → no injection path) and is entirely best-effort — any
# failure just means grounding is absent for that attempt.
# ---------------------------------------------------------------------

# Command name shape. Also the shell-injection guard: only names
# matching this are ever interpolated into the probe's `sh -c`.
_CMD_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]*$")

# Ubiquitous coreutils / interpreters — probing these wastes budget and
# they're never the hallucinated tool. Skipped as candidates.
_UBIQUITOUS_CMDS = {
    "sh", "bash", "cp", "mv", "mkdir", "rm", "ls", "echo", "cat", "cd",
    "python", "python3", "pip", "pip3", "chmod", "chown", "touch",
    "sleep", "true", "false", "test", "env", "export", "set", "source",
    "which", "command", "head", "tail", "grep", "sed", "awk", "cut",
}

# stderr signatures that name a missing binary (group 1 = the name).
_STDERR_MISSING_PATS = [
    re.compile(r"([^\s:]+):\s*command not found"),
    re.compile(r"command not found:\s*(\S+)"),
    re.compile(r"/bin/(?:sh|bash):[^:]*:\s*([^\s:]+):\s*not found"),
    re.compile(r'executable file not found[^"]*"([^"]+)"'),
    re.compile(r'exec:\s*"([^"]+)"'),
    re.compile(r"No such file or directory:\s*'(/(?:usr|opt|bin)/[^']+)'"),
]

# script call sites whose first token is the invoked binary.
_SCRIPT_CMD_PATS = [
    re.compile(
        r"subprocess\.(?:run|call|check_call|check_output|Popen)\(\s*\[\s*[rfb]?[\"']([^\"']+)[\"']"
    ),
    re.compile(
        r"subprocess\.(?:run|call|check_call|check_output|Popen)\(\s*[rfb]?[\"']([^\"']+)[\"']"
    ),
    re.compile(r"os\.system\(\s*[rfb]?[\"']([^\"']+)[\"']"),
]


def _looks_like_missing_command(stderr: str) -> bool:
    """True when stderr indicates an invoked command didn't exist —
    the only case where a --help probe is worth a docker run."""
    s = stderr or ""
    if "command not found" in s:
        return True
    if "executable file not found" in s:
        return True
    if "not found in $PATH" in s:
        return True
    # absolute-path exec of a system binary that isn't installed
    if re.search(r"No such file or directory: '/(usr|opt|bin)/", s):
        return True
    return False


def _candidate_binaries(
    stderr: str, scripts: Dict[str, str], *, limit: int = 12,
) -> List[str]:
    """Binaries worth probing: the ones stderr says are missing (first,
    highest signal) plus the domain tools the scripts invoke. Sanitised
    to `_CMD_NAME_RE` — the return value is interpolated into a shell."""
    ordered: List[str] = []

    def add(raw: str) -> None:
        raw = (raw or "").strip().strip('"').strip("'")
        if not raw or raw.startswith(("/app", "/data", "./", "../")):
            return
        name = raw.rsplit("/", 1)[-1]          # basename; keeps the tool name
        if not _CMD_NAME_RE.match(name):
            return
        if name in _UBIQUITOUS_CMDS or name in ordered:
            return
        ordered.append(name)

    for pat in _STDERR_MISSING_PATS:
        for m in pat.finditer(stderr or ""):
            add(m.group(1))

    for content in (scripts or {}).values():
        for pat in _SCRIPT_CMD_PATS:
            for m in pat.finditer(content or ""):
                tok = (m.group(1) or "").split()
                if tok:
                    add(tok[0])
        if len(ordered) >= limit * 2:
            break

    return ordered[:limit]


def _probe_binary_reality(
    tag: str, stderr: str, scripts: Dict[str, str],
) -> Optional[str]:
    """Run one throwaway container off the built image to capture the
    REAL command surface: for each candidate binary, whether it exists
    + its `--help`; plus the executables actually installed under
    /usr/local/bin and /opt/*/bin (where source-built tools land — the
    place a hallucinated CLI's real siblings live). Returns a text block
    for the repair prompt, or None if nothing useful was gathered."""
    cands = _candidate_binaries(stderr, scripts)
    if not cands:
        return None
    cand_str = " ".join(cands)  # each already matches _CMD_NAME_RE
    probe = (
        'command -v timeout >/dev/null 2>&1 && TO="timeout 5" || TO="";\n'
        f"for b in {cand_str}; do\n"
        '  p=$(command -v "$b" 2>/dev/null) || p="";\n'
        '  if [ -n "$p" ]; then\n'
        '    echo "### EXISTS: $b -> $p";\n'
        '    $TO "$b" --help </dev/null 2>&1 | head -c 1000;\n'
        '    echo; echo "---";\n'
        "  else\n"
        '    echo "### MISSING (called but not installed): $b";\n'
        "  fi;\n"
        "done;\n"
        'echo "### EXECUTABLES ACTUALLY INSTALLED (/usr/local/bin, /opt/*/bin):";\n'
        "{ ls -1 /usr/local/bin 2>/dev/null; ls -1 /opt/*/bin 2>/dev/null; } "
        "| sort -u | head -200;\n"
    )
    try:
        res = subprocess.run(
            ["docker", "run", "--rm", "--network=none",
             "--entrypoint", "sh", tag, "-c", probe],
            capture_output=True, text=True, timeout=45,
        )
    except Exception:
        return None
    out = (res.stdout or "").strip()
    return out[:4000] if out else None


def _call_repair_endpoint(
    api_url: str, api_key: str, spec_id: int, mode: str,
    attempt_number: int, failure: dict,
    current_dockerfile: Optional[str], current_scripts: Dict[str, str],
    cache_hint: Optional[str] = None,
    known_bad: Optional[List[str]] = None,
    promoted_diff: Optional[str] = None,
) -> dict:
    body = {
        "spec_id": spec_id, "mode": mode, "attempt_number": attempt_number,
        "failure": failure, "current_scripts": current_scripts,
    }
    if current_dockerfile is not None:
        body["current_dockerfile"] = current_dockerfile
    # Remediation-cache context (see remediation_client). The endpoint
    # splices cache_hint + known_bad into the repair prompt and, when
    # promoted_diff is set for an outer repair, applies it directly and
    # skips the LLM.
    if cache_hint:
        body["cache_hint"] = cache_hint
    if known_bad:
        body["known_bad"] = known_bad
    if promoted_diff:
        body["promoted_diff"] = promoted_diff
    # The /wizard/repair-artifacts endpoint lives on the Woodsman Converter
    # router (module nodes_builds.py), which main.py mounts at /v1/woodsman. Hitting the bare
    # `/wizard/repair-artifacts` path 404s — caught on the first live
    # wizard run (TRELLIS.2 spec 8, task 10806). Other stages use
    # `/v1/woodsman/` consistently; this one is no different.
    r = requests.post(
        f"{api_url}/v1/woodsman/wizard/repair-artifacts",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        json=body, timeout=300,
    )
    r.raise_for_status()
    return r.json()


def _scripts_from_spec(spec: dict) -> Dict[str, str]:
    """Pull the editable scripts out of the spec into a flat dict for
    the LLM. Convention matches what the repair endpoint expects."""
    scripts: Dict[str, str] = {}
    if spec.get("entrypoint_sh"):
        scripts["entrypoint.sh"] = spec["entrypoint_sh"]
    if spec.get("build_and_push_sh"):
        scripts["build_and_push.sh"] = spec["build_and_push_sh"]
    for sf in (spec.get("source_files") or []):
        fname = sf.get("filename")
        content = sf.get("content")
        if fname and content is not None:
            scripts[fname] = content
    return scripts


def _progress(msg: str) -> None:
    """Emit a line the WorkflowBuilder build-progress component picks up."""
    print(f"[test-and-repair] {msg}", flush=True)


def _image_present_locally(ref: str) -> bool:
    """True when ``ref`` is already in the local docker image store.

    The FROM pre-flight asks this BEFORE any ``docker pull``: a tag loaded
    into the daemon (air-gapped install, or a base that is private until
    published) is usable by the build and must not be reported unresolvable
    because the registry says "denied". Any error (no docker CLI, timeout)
    answers False so the registry pull still decides, exactly as before."""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", ref],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))
    # Variant identity. The DAG entrypoint routes both STAGE=
    # test_and_repair_variant for hero AND (since the generator now
    # enqueues per-variant repair nodes) for non-hero through this
    # same file. variant="hero" preserves the legacy default for
    # callers that don't set the env.
    variant = env("VARIANT", default="hero")
    is_hero = (variant == "hero")

    _progress(f"starting loop spec={spec_id} variant={variant}")
    # Pre-flight: refuse to start a multi-GB wizard build when the host
    # docker daemon doesn't have enough space. A disk-pressure failure
    # mid-build produces error messages the LLM mis-interprets ("docker
    # daemon" keywords appear in the cancel error), the loop never
    # converges, and the user wastes 5-10 minutes before seeing a
    # useless message. Catching this upfront saves the wait. Stage repro:
    # TRELLIS.2 spec 12 ran 5 outer attempts thrashing on bogus
    # "docker socket" repairs while the actual problem was 92% disk usage.
    disk_warning = check_docker_disk_space(min_gb=min_free_gb_default())
    if disk_warning:
        _progress(f"ERROR: {disk_warning}")
        report_back(
            api_url, api_key, spec_id=spec_id, stage="test_and_repair",
            variant=variant, success=False,
            payload={"preflight_failure": "insufficient_disk", "message": disk_warning},
        )
        return 1

    # Pull-side auth for FROM lines that reference private platform Harbor
    # base images (e.g. modtim/base-python:3.11). build_one_variant.py
    # already does this for non-hero variants; test_and_repair (hero) was
    # missing it — without the login, the first `docker build` pull
    # returns 401 Unauthorized, the LLM repair "fixes" by swapping to a
    # public base image like python:3.11-slim, and the wrapper's
    # `USER mod` directive then thrashes because the public base doesn't
    # provision the mod user (manual-test repro 2026-05-23 spec=41/42).
    plat_url = env("PLATFORM_HARBOR_URL", default="").strip()
    plat_user = env("PLATFORM_HARBOR_USERNAME", default="").strip()
    plat_pass = env("PLATFORM_HARBOR_PASSWORD", default="").strip()
    if plat_url and plat_user and plat_pass:
        try:
            docker_login(plat_url, plat_user, plat_pass)
            _progress(f"logged in to platform Harbor {plat_url}")
        except Exception as e:
            _progress(
                f"warn: platform Harbor login failed ({e}) — "
                f"curated base images may 401 on pull"
            )
    # Resolve /data's host path once. Without this, nested `docker run
    # -v` calls mount empty root-owned dirs and the wizard image's
    # non-root USER can't write to /data/output. See
    # _resolve_data_host_path for the full repro.
    data_host_path = resolve_data_host_path()
    if data_host_path:
        _progress(f"probe dirs will live under /data (host: {data_host_path})")
    else:
        _progress(
            "warn: could not resolve /data host path — smoke probe will "
            "use tempfile.mkdtemp(), which produces empty mounts under "
            "nested docker. Smoke output may be unreliable."
        )
    # WS6.8d: prefer the NAMED VOLUME backing /data over its host
    # Mountpoint. On Docker-Desktop/WSL2 hosts the daemon lives in a
    # VM and the Mountpoint path (/var/lib/docker/volumes/<vol>/_data)
    # does not exist on the daemon's filesystem — a nested
    # `docker run -v {mountpoint}/...:/data/input` then gets an
    # auto-created EMPTY dir and the node's input loop runs 0 times
    # (reproduced 09-29: hflip/blur s2_golden). Mounting the named
    # volume itself at /data has no host-path indirection and works
    # on every daemon-root layout.
    data_volume_name = resolve_data_volume_name()
    if data_volume_name:
        _progress(f"smoke probe will mount named volume {data_volume_name} at /data")
    elif data_host_path:
        _progress("smoke probe will use /data host-path bind mounts")
    # Reset the BuildSpec's repair counters before doing anything. A
    # previous run (or a prior worker that crashed mid-attempt) may
    # have left the budget exhausted — without this, the API rejects
    # every repair with "budget exhausted" and the loop bails in 2
    # attempts. Stage repro: spec 10 ran 10 outer attempts on a prior
    # run, user retried, immediate FAIL. Now: reset → fresh 30+10
    # budget on every dispatch.
    try:
        rr = requests.post(
            f"{api_url}/v1/woodsman/wizard/reset-repair-budget",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json={"spec_id": spec_id}, timeout=30,
        )
        if rr.ok:
            rrj = rr.json() or {}
            prev_i = rrj.get("previous_inner_used", 0)
            prev_o = rrj.get("previous_outer_used", 0)
            if prev_i or prev_o:
                _progress(f"reset prior repair budget (was inner={prev_i}, outer={prev_o})")
        else:
            _progress(f"warn: reset-repair-budget returned {rr.status_code} — continuing anyway")
    except Exception as e:
        _progress(f"warn: reset-repair-budget call failed ({e}) — continuing anyway")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    image_name = spec["image_name"]
    harbor_url = env("HARBOR_URL", default="").strip()
    tenant_id = spec.get("tenant_id", 0)
    prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
    tag_suffix = "" if is_hero else f"-{variant}"
    tag = f"{prefix}{image_name}:latest{tag_suffix}"

    # Hero gets the full repair budget (30 inner + 10 outer = 40 attempts).
    # Non-hero variants share the hero's repaired source via
    # /wizard/sync-variants-from-hero, so most of their failures are
    # already gone before they enter this loop — give them a smaller
    # budget so a variant-specific bug (e.g. a CUDA wheel that doesn't
    # exist) iterates a few times before giving up and marking the
    # variant incompatible. Overrideable via spec for power users.
    if is_hero:
        inner_max = int(spec.get("repair_attempts_inner_max", 30))
        outer_max = int(spec.get("repair_attempts_outer_max", 10))
    else:
        inner_max = int(spec.get("variant_repair_attempts_inner_max", 5))
        outer_max = int(spec.get("variant_repair_attempts_outer_max", 3))
    inner_attempt = 0
    outer_attempt = 0
    total_attempt = 0
    last_failure: Optional[dict] = None
    last_build_output: str = ""

    # Item #6 (fullproof extension) — pre-flight base-image pull.
    # Before allocating any of the outer-repair budget, do a single
    # `docker pull` of the proposed FROM line(s). If they don't
    # resolve, synthesize an immediate failure so the existing
    # repair path picks it up on attempt 1 — saving the 5-15 min
    # docker-build cycle the LLM would have wasted "discovering"
    # the missing image.
    _hero_df = _resolve_variant_dockerfile(spec, variant)
    if _hero_df:
        import re as _re
        _from_refs = [
            m.group(1).strip()
            for m in _re.finditer(
                r"^\s*FROM\s+([A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?)",
                _hero_df, flags=_re.MULTILINE | _re.IGNORECASE,
            )
        ]
        _bad_froms: list[str] = []
        for _ref in _from_refs:
            if not _ref:
                continue
            # An image already in the local docker store must pass the
            # pre-flight without a registry round-trip: air-gapped installs
            # and bases that are private until published (a registry resolve
            # answers "denied" for a tag that is loaded locally) depend on it.
            if _image_present_locally(_ref):
                _progress(f"pre-flight: FROM {_ref} present in the local image store — OK")
                continue
            try:
                _pull = subprocess.run(
                    ["docker", "pull", _ref],
                    capture_output=True, text=True, timeout=180,
                )
            except Exception as _pull_exc:  # noqa: BLE001
                _progress(f"pre-flight: FROM {_ref} pull errored ({_pull_exc}) — continuing")
                continue
            if _pull.returncode != 0:
                _bad_froms.append(_ref)
                _progress(
                    f"pre-flight: FROM {_ref} did NOT resolve "
                    f"({(_pull.stderr or _pull.stdout)[:150].strip()})"
                )
            else:
                _progress(f"pre-flight: FROM {_ref} resolved OK")
        if _bad_froms:
            _progress(
                f"pre-flight: {len(_bad_froms)} unresolvable FROM line(s); "
                f"seeding repair loop with image-not-found failure so "
                f"the LLM fixes on attempt 1 instead of attempt N+1"
            )
            last_failure = {
                "exit_code": 1,
                "stdout_tail": "",
                "stderr_tail": (
                    f"pre-flight base-image pull failed for: {', '.join(_bad_froms)}. "
                    f"failed to resolve source metadata. Pick a verified base "
                    f"from the IMAGE_NOT_FOUND hint."
                ),
                "produced_output": False,
                "output_files": [],
                "input_seed": {},
            }

    # Track consecutive identical rejection reasons. If the LLM endpoint
    # keeps rejecting for the same reason (e.g. "context length exceeded"
    # on a multi-megabyte spec), there's no point burning the rest of
    # the budget on the same prompt — bail out after 2 in a row.
    consecutive_same_rejection: int = 0
    last_rejection_signature: str = ""
    BAIL_AFTER_SAME_REJECTIONS = 2

    # Thrashing detection. Stage repro 1 (spec 10): LLM cycled
    # "Added Docker CLI", "Removed Docker CLI", "Added Docker CLI"
    # across 10 attempts. Stage repro 2 (spec 11): LLM cycled
    # 7 attempts as A, B, C, C, A, B, C — three unique ideas swapped
    # around. Neither fired the old detector because each individual
    # explanation didn't appear 3 times within a 4-attempt window.
    #
    # New approach: keep ALL accepted explanations (not just the last
    # 4) and bail when:
    #   • the SAME explanation has appeared twice already (3rd time is
    #     evidence of looping on a fix that didn't help), OR
    #   • a 2-step or 3-step alternation has repeated (A,B,A,B or
    #     A,B,C,A,B,C — pure churn between known-bad ideas).
    all_explanations: List[str] = []
    MAX_REPEAT_OF_SAME_IDEA = 2  # 3rd occurrence of the same idea → bail
    MIN_CYCLE_REPEATS = 2        # 2 full cycles of A,B,A,B → bail

    def _is_thrashing(new_explanation: str) -> bool:
        """True iff the LLM is repeating itself or cycling between a
        small set of ideas."""
        import re as _re
        def _norm(s: str) -> str:
            s = _re.sub(r"\s+", " ", (s or "").lower()).strip()
            return s[:120]   # match on a prefix so paraphrases still hit
        new_n = _norm(new_explanation)
        if not new_n:
            return False
        history_n = [_norm(e) for e in all_explanations]
        # (a) Same idea appeared MAX_REPEAT_OF_SAME_IDEA times AND we're
        # about to add a 3rd. 3 attempts at the same fix that didn't
        # work is enough signal.
        if history_n.count(new_n) >= MAX_REPEAT_OF_SAME_IDEA:
            return True
        # (b) k-cycle detection. After appending new_n, the last
        # k*MIN_CYCLE_REPEATS entries form a tile of period k.
        prospective = history_n + [new_n]
        for k in (2, 3):
            need = k * MIN_CYCLE_REPEATS
            if len(prospective) < need:
                continue
            tail = prospective[-need:]
            window = tail[:k]
            is_cycle = all(
                tail[i] == window[i % k] for i in range(need)
            )
            if is_cycle and len(set(window)) >= 2:
                return True
        return False

    # Remediation-cache state. Populated on the FIRST failure (the
    # "entry error" that started this loop); all lookups/records key off
    # that signature. Every cache call is fail-open — a cache outage
    # never blocks the build.
    cache = {
        "looked_up": False, "sig": None, "text": None,
        "hint": None, "known_bad": None, "promoted": None,
    }
    # Dockerfile content built in the current attempt — used to record a
    # negative-cache entry when a *repaired* attempt (total_attempt > 1)
    # fails, so that exact failed fix isn't proposed again.
    last_built_df: Optional[str] = None

    def _cache_first_failure(failure: dict) -> None:
        """On the first failure, compute the entry-error signature and
        query the remediation cache for a prior fix + known-bad list."""
        if cache["looked_up"]:
            return
        cache["looked_up"] = True
        cache["text"] = failure.get("stderr_tail") or ""
        cache["sig"] = remediation_client.error_signature(cache["text"])
        resp = remediation_client.lookup(
            api_url, api_key, cache["sig"], cache["text"],
        )
        if not resp:
            return
        cache["known_bad"] = remediation_client.known_bad_excerpts(resp)
        if resp.get("promoted") and resp.get("diff"):
            cache["promoted"] = resp["diff"]
            _progress(
                f"remediation cache: promoted fix found "
                f"(trust={resp.get('trust')}) — applying, skipping LLM"
            )
        elif resp.get("diff"):
            cache["hint"] = resp["diff"]
            _progress(
                f"remediation cache: hint found "
                f"(match_kind={resp.get('match_kind')}, trust={resp.get('trust')})"
            )
        if cache["known_bad"]:
            _progress(
                f"remediation cache: avoiding {len(cache['known_bad'])} "
                f"known-bad fix(es)"
            )

    def _record_failed_if_repaired() -> None:
        """A repaired attempt (total_attempt > 1) just failed — remember
        its Dockerfile as a dead end. The first attempt is the original
        (the error baseline), not a fix, so it's never recorded."""
        if total_attempt > 1 and cache["sig"] and last_built_df:
            remediation_client.record_failed_attempt(
                api_url, api_key, cache["sig"], last_built_df, cache["text"],
            )

    ctx_root = tempfile.mkdtemp(prefix="test-repair-")
    try:
        while True:
            total_attempt += 1
            ctx_dir = os.path.join(ctx_root, f"attempt-{total_attempt}")
            os.makedirs(ctx_dir, exist_ok=True)
            _materialise_build_context(spec, ctx_dir, variant=variant)

            # Diagnostic: dump Dockerfile size + FROM line so we can
            # tell at a glance whether _materialise_build_context wrote
            # the latest spec content (wfr 60 mystery: saved spec was
            # 298 bytes clean but the build failed on an apt block —
            # logging here will show whether the on-disk Dockerfile
            # matches the spec or not).
            try:
                with open(os.path.join(ctx_dir, "Dockerfile"), "r") as _df_in:
                    _df_content = _df_in.read()
                last_built_df = _df_content
                _df_size = len(_df_content)
                _df_first = (_df_content.splitlines() or ["(empty)"])[0]
                _df_apt = "apt-get install" in _df_content
                _progress(
                    f"attempt {total_attempt}: ctx Dockerfile "
                    f"bytes={_df_size} first_line={_df_first!r} "
                    f"has_apt_install={_df_apt}"
                )
            except Exception as _df_exc:
                _progress(f"attempt {total_attempt}: ctx Dockerfile read errored: {_df_exc}")

            _progress(f"attempt {total_attempt}: building image (inner={inner_attempt}/{inner_max}, outer={outer_attempt}/{outer_max})")
            built, last_build_output = _build_image(ctx_dir, tag)
            if not built:
                # Build itself failed — treat as outer-loop trigger. The
                # repair endpoint will get the build output as stderr.
                _progress(f"attempt {total_attempt}: docker build failed; escalating to outer repair")
                failure = {
                    "exit_code": -1,
                    "stdout_tail": "",
                    "stderr_tail": last_build_output,
                    "output_files": [],
                    "produced_output": False,
                    "input_seed": {"source": "build_failure"},
                }
                last_failure = failure
                _cache_first_failure(failure)
                _record_failed_if_repaired()
                if outer_attempt >= outer_max:
                    _progress("outer budget exhausted on build failure; giving up")
                    break
                outer_attempt += 1
                repair = _call_repair_endpoint(
                    api_url, api_key, spec_id, "outer", outer_attempt,
                    failure, _resolve_variant_dockerfile(spec, variant), _scripts_from_spec(spec),
                    cache_hint=cache["hint"], known_bad=cache["known_bad"],
                    promoted_diff=cache["promoted"],
                )
                cache["promoted"] = None  # apply a promoted fix at most once
                if not repair.get("accepted"):
                    reasons = repair.get("rejection_reasons") or []
                    _progress(f"outer repair rejected: {reasons}")
                    # Bail-out: a consistently-rejected response means the
                    # LLM can't see / act on the prompt (e.g. context
                    # length exceeded). Retrying with the same prompt
                    # produces the same rejection — burn the rest of the
                    # budget for nothing. Two in a row is enough signal.
                    signature = "|".join(str(r)[:200] for r in reasons)
                    if signature and signature == last_rejection_signature:
                        consecutive_same_rejection += 1
                    else:
                        consecutive_same_rejection = 1
                        last_rejection_signature = signature
                    if consecutive_same_rejection >= BAIL_AFTER_SAME_REJECTIONS:
                        _progress(
                            f"giving up: same rejection {consecutive_same_rejection} times "
                            f"in a row — retrying won't help"
                        )
                        break
                    if outer_attempt >= outer_max:
                        break
                    continue
                explanation = repair.get("explanation") or ""
                _progress(f"outer repair {outer_attempt}: {explanation}")
                if _is_thrashing(explanation):
                    _progress(
                        f"giving up: LLM is repeating the same fix "
                        f"(thrashing) — the failure is not what the LLM "
                        f"thinks it is. Final explanation: {explanation}"
                    )
                    break
                all_explanations.append(explanation)
                # Re-fetch the spec — the endpoint just mutated it.
                consecutive_same_rejection = 0
                last_rejection_signature = ""
                spec = fetch_build_spec(api_url, api_key, spec_id)
                continue

            _progress(f"attempt {total_attempt}: smoke probing")
            try:
                failure = _smoke_probe(
                    tag, spec.get("input_type") or "image",
                    data_host_path=data_host_path,
                    expected_output_type=(
                        spec.get("output_type") or spec.get("suggested_output_type")
                    ),
                    variant=variant,
                    probe_net=probe_network(spec),
                    data_volume_name=data_volume_name,
                )
            except RuntimeError as staging_err:
                # The staged input is not visible where the probe will
                # read it. Running the node anyway would 0-loop on an
                # empty /data/input and the repair LLM would thrash on
                # a bug that isn't in the script (WS6.8d). Fail loudly
                # and immediately — this is an infra/staging problem,
                # not a code problem the LLM can fix.
                _progress(f"FAIL: {staging_err}")
                report_back(
                    api_url, api_key, spec_id=spec_id, stage="test_and_repair",
                    variant=variant, success=False,
                    payload={
                        "total_attempts": total_attempt,
                        "staging_failure": "smoke_input_staging_empty",
                        "message": str(staging_err),
                        "input_seed": {"source": "synth_stub",
                                       "input_type": spec.get("input_type") or "image"},
                    },
                )
                return 1
            last_failure = failure
            if failure["exit_code"] == 0 and failure["produced_output"]:
                _progress(
                    f"PASS after {total_attempt} attempt(s) "
                    f"(inner={inner_attempt}/{inner_max}, outer={outer_attempt}/{outer_max})"
                )
                # §4 chunk-4 Issue 2 fix — propagate the (possibly
                # repaired) hero_dockerfile to non-hero Image-mode
                # variants before they fire. The DAG gates non-hero
                # variant builds behind this stage's completion;
                # without this sync they'd build with the stale
                # pre-repair Dockerfile.
                # sync-variants-from-hero only runs in the hero branch —
                # it propagates the hero's (possibly repaired) Dockerfile
                # + scripts to the non-hero variant rows on the spec so
                # they build against the converged state. A non-hero
                # repair loop must NOT call this endpoint; that would
                # overwrite hero with the variant's repair.
                if not is_hero:
                    _progress(
                        f"sync-variants-from-hero: skipped (variant={variant})"
                    )
                try:
                    if not is_hero:
                        raise _SkipSync()
                    r = requests.post(
                        f"{api_url}/v1/woodsman/wizard/sync-variants-from-hero",
                        headers={"X-API-Key": api_key,
                                 "Content-Type": "application/json"},
                        json={"build_spec_id": spec_id},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        body = r.json()
                        _progress(
                            f"sync-variants-from-hero: propagated="
                            f"{body.get('propagated', 0)} "
                            f"skipped_non_image={body.get('skipped_non_image', 0)}"
                        )
                    else:
                        _progress(
                            f"warn: sync-variants-from-hero returned "
                            f"{r.status_code} — variants may smoke-fail"
                        )
                except _SkipSync:
                    pass  # non-hero variant — sentinel raised above
                except Exception as e:
                    _progress(
                        f"warn: sync-variants-from-hero failed ({e}) — "
                        f"variants may smoke-fail"
                    )
                # Defensive eager push to harbor. Docker 29.4.0 +
                # buildkit's containerd snapshotter has a known race
                # ("extraction snapshot removal failed" → image's
                # underlying layers can be GC'd by buildkit cleanup
                # after the buildx command exits) that occasionally
                # drops the just-built hero tag from the daemon
                # between the smoke PASS and the next task starting
                # push-hero. Pushing here, while the image is still
                # warm in the daemon, ensures harbor has it; the
                # downstream push-hero task can fall back to a
                # harbor-side check if its daemon view shows the
                # tag missing.
                hb_url = env("HARBOR_URL", default="").strip()
                hb_user = env("HARBOR_USERNAME", default="").strip()
                hb_pass = env("HARBOR_PASSWORD", default="").strip()
                if hb_url and hb_user and hb_pass:
                    import pathlib
                    pathlib.Path("/data/skopeo-tmp").mkdir(parents=True, exist_ok=True)
                    push_res = subprocess.run(
                        ["skopeo", "--tmpdir", "/data/skopeo-tmp", "copy",
                         "--dest-creds", f"{hb_user}:{hb_pass}",
                         f"docker-daemon:{tag}", f"docker://{tag}"],
                        capture_output=True, text=True, timeout=600,
                    )
                    if push_res.returncode == 0:
                        _progress(f"eager-push: hero {tag} on harbor")
                    else:
                        _progress(
                            f"warn: eager-push hero failed "
                            f"({push_res.stderr.strip()[-200:]}) — "
                            f"push-hero task will retry"
                        )
                # Learn: if this build only passed after repair(s),
                # record the converged Dockerfile as the fix for the
                # entry error so a future build with the same error
                # skips the loop. A first-attempt pass has no cache sig
                # (nothing failed) and is correctly skipped.
                if cache["sig"]:
                    remediation_client.record_success(
                        api_url, api_key, cache["sig"],
                        _resolve_variant_dockerfile(spec, variant) or last_built_df or "",
                        cache["text"],
                    )
                report_back(
                    api_url, api_key, spec_id=spec_id, stage="test_and_repair",
                    variant=variant, success=True,
                    payload={
                        "total_attempts": total_attempt,
                        "inner_attempts": inner_attempt,
                        "outer_attempts": outer_attempt,
                        "final_smoke": failure,
                    },
                )
                return 0

            _cache_first_failure(failure)
            _record_failed_if_repaired()
            needs_outer = _classify_for_outer(failure.get("stderr_tail", ""))
            mode = "outer" if needs_outer else "inner"
            if mode == "inner" and inner_attempt >= inner_max:
                if outer_attempt < outer_max:
                    _progress(f"inner budget exhausted; escalating to outer")
                    mode = "outer"
                else:
                    _progress("inner and outer budgets exhausted; giving up")
                    break
            if mode == "outer" and outer_attempt >= outer_max:
                _progress("outer budget exhausted; giving up")
                break

            this_attempt_number = (
                inner_attempt + 1 if mode == "inner" else outer_attempt + 1
            )
            _progress(
                f"smoke failed (exit={failure['exit_code']}, "
                f"produced_output={failure['produced_output']}) — "
                f"calling LLM {mode} repair #{this_attempt_number}"
            )

            # --help grounding: if the failure is a missing/hallucinated
            # command, capture the image's real command surface and thread
            # it into the repair prompt (via failure["binary_reality"]).
            # Best-effort — never blocks the repair.
            if _looks_like_missing_command(failure.get("stderr_tail", "")):
                try:
                    reality = _probe_binary_reality(
                        tag, failure.get("stderr_tail", ""),
                        _scripts_from_spec(spec),
                    )
                    if reality:
                        failure["binary_reality"] = reality
                        _progress(
                            f"attempt {total_attempt}: grounded repair with "
                            f"real command surface ({len(reality)} chars)"
                        )
                except Exception as _probe_exc:
                    _progress(f"binary-reality probe skipped: {_probe_exc}")

            repair = _call_repair_endpoint(
                api_url, api_key, spec_id, mode, this_attempt_number,
                failure, spec.get("hero_dockerfile"), _scripts_from_spec(spec),
                cache_hint=cache["hint"], known_bad=cache["known_bad"],
                promoted_diff=(cache["promoted"] if mode == "outer" else None),
            )
            if mode == "outer":
                cache["promoted"] = None  # apply a promoted fix at most once
            if not repair.get("accepted"):
                reasons = repair.get("rejection_reasons") or []
                _progress(f"{mode} repair rejected: {reasons}")
                # Treat as a used attempt so we don't infinite-loop on a
                # consistently-rejected response. Bail out on repeated
                # identical rejection — see the build-failure branch
                # above for the rationale.
                signature = "|".join(str(r)[:200] for r in reasons)
                if signature and signature == last_rejection_signature:
                    consecutive_same_rejection += 1
                else:
                    consecutive_same_rejection = 1
                    last_rejection_signature = signature
                if consecutive_same_rejection >= BAIL_AFTER_SAME_REJECTIONS:
                    _progress(
                        f"giving up: same rejection {consecutive_same_rejection} times "
                        f"in a row — retrying won't help"
                    )
                    break
                if mode == "inner":
                    inner_attempt += 1
                else:
                    outer_attempt += 1
                continue

            explanation = repair.get("explanation") or ""
            _progress(f"{mode} repair {this_attempt_number} explanation: {explanation}")
            if _is_thrashing(explanation):
                _progress(
                    f"giving up: LLM is repeating the same fix "
                    f"(thrashing) — the failure is not what the LLM "
                    f"thinks it is. Final explanation: {explanation}"
                )
                break
            all_explanations.append(explanation)
            consecutive_same_rejection = 0
            last_rejection_signature = ""
            if mode == "inner":
                inner_attempt += 1
            else:
                outer_attempt += 1
            spec = fetch_build_spec(api_url, api_key, spec_id)

        # Loop exited without success. Print the actual underlying
        # failure so the user can act on it. Without this, the UI shows
        # "FAIL after 5 attempts" with no signal about what to actually
        # fix — the LLM's "repair explanations" are misleading when the
        # model is pattern-matching wrong (e.g. fixating on "docker"
        # in stderr when the real issue is a missing dep).
        _progress(
            f"FAIL after {total_attempt} attempt(s) "
            f"(inner={inner_attempt}/{inner_max}, outer={outer_attempt}/{outer_max})"
        )
        _progress("--- ACTUAL FAILURE OUTPUT (last attempt's stderr) ---")
        # Whichever signal was the LAST one the LLM saw, print it: that's
        # the information the user needs to manually intervene. For build
        # failures `last_failure.stderr_tail` is the docker build output;
        # for smoke failures it's the wizard container's stderr.
        # Print directly so multi-line output isn't [test-and-repair]-prefixed.
        printed = False
        if last_failure:
            stderr_tail = (last_failure.get("stderr_tail") or "").rstrip()
            if stderr_tail:
                print("STDERR:", flush=True)
                print(stderr_tail[-2000:], flush=True)
                printed = True
            stdout_tail = (last_failure.get("stdout_tail") or "").rstrip()
            if stdout_tail:
                print("STDOUT:", flush=True)
                print(stdout_tail[-1000:], flush=True)
                printed = True
        if not printed and last_build_output:
            print("BUILD OUTPUT:", flush=True)
            print(last_build_output[-2000:], flush=True)
        _progress("--- END FAILURE OUTPUT ---")
        _progress(
            "Auto-repair could not converge. Open the wizard's Dockerfile "
            "editor and inspect the error above. Common causes the LLM "
            "struggles with: pinned package versions that don't exist on "
            "PyPI (e.g. gradio==6.0.1), CUDA wheel URLs that 404, or "
            "packages whose setup.py runs other deps at install time."
        )
        report_back(
            api_url, api_key, spec_id=spec_id, stage="test_and_repair",
            variant=variant, success=False,
            payload={
                "total_attempts": total_attempt,
                "inner_attempts": inner_attempt,
                "outer_attempts": outer_attempt,
                "final_smoke": last_failure,
                "build_output_tail": last_build_output[-1000:] if last_build_output else "",
            },
        )
        return 1
    finally:
        shutil.rmtree(ctx_root, ignore_errors=True)
        # In named-volume mode the smoke probe ran on the task's
        # CANONICAL /data/output, so the worker's post-task output
        # upload (Step 6, non-SkipUpload) would otherwise ship the
        # probe's node outputs as if they were this task's results.
        # Best-effort wipe of the probe's products (the dirs themselves
        # are re-created per probe; the task volume is deleted by the
        # worker when the task ends regardless).
        if data_volume_name:
            try:
                for _f in os.listdir("/data/output"):
                    p = os.path.join("/data/output", _f)
                    if os.path.isfile(p) or os.path.islink(p):
                        os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
