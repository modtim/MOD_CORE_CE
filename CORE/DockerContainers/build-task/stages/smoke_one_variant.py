"""B4 — run the β pre-register smoke probe on a single variant.

The old probe (set MOD_VARIANT_SMOKE=1 env var, expect the in-image
script to print {"smoke":"ok"}) only worked for images that had a
probe handler. Wizard-built images don't have one, so the probe
effectively just ran the entrypoint with no args and accepted any
exit-0 result — including `exec ffmpeg` printing usage text.

New probe: mount a synthetic /data/input file (minimum-valid PNG/PDF/
WAV/etc. based on declared input_type), let the container's normal
entrypoint run, and require a non-empty file under /data/output. This
matches how the runtime actually invokes the container, so a build
that passes smoke is one that will produce output on the user's first
real run too.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from _common import env, fetch_build_spec, log, probe_network, report_back, resolve_data_host_path
from _synth_input import (
    collect_outputs, docker_device_args_for_variant, has_real_output,
    looks_like_missing_accelerator, looks_like_runtime_failure,
    output_matches_declared_type, synth_input_for,
)


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))
    variant = env("VARIANT", default="hero")

    log(f"[smoke_variant] spec={spec_id} variant={variant}")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    image_name = spec["image_name"]
    tag_suffix = "" if variant == "hero" else f"-{variant}"
    harbor_url = env("HARBOR_URL", default="").strip()
    tenant_id = spec.get("tenant_id", 0)
    prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
    full_tag = f"{prefix}{image_name}:latest{tag_suffix}"

    # Place probe dirs under /data (a docker-managed bind mount from
    # the worker) so we can pass the HOST path to nested `docker run -v`.
    # See _common.resolve_data_host_path for the full rationale.
    import uuid as _uuid
    data_host = resolve_data_host_path()
    probe_id = _uuid.uuid4().hex[:12]
    if data_host:
        tmp = f"/data/smoke-{variant}-{probe_id}"
        tmp_host = f"{data_host}/smoke-{variant}-{probe_id}"
    else:
        tmp = tempfile.mkdtemp(prefix=f"smoke-{variant}-")
        tmp_host = tmp
    in_dir = os.path.join(tmp, "input"); os.makedirs(in_dir, exist_ok=True)
    out_dir = os.path.join(tmp, "output"); os.makedirs(out_dir, exist_ok=True)
    in_dir_host = os.path.join(tmp_host, "input")
    out_dir_host = os.path.join(tmp_host, "output")
    try:
        input_type = spec.get("input_type") or "image"
        fname, size = synth_input_for(input_type, in_dir)
        os.chmod(out_dir, 0o777)
        # Parent must be searchable by the wizard image's non-root USER.
        os.chmod(tmp, 0o755)
        log(f"[smoke_variant] synth input: type={input_type} file={fname} size={size}")

        # Pre-flight: confirm the tag is on the daemon. Concurrent
        # variant builds against the same digest can drop tags
        # (manual-test repro 2026-05-23 spec=63 Test A smoke-cuda
        # exit 125 while smoke-rocm/intel/npu on same digest passed).
        # Recovery order: (1) re-tag from a sibling tag if one is
        # still on the daemon (byte-identical variants share the
        # digest); (2) docker pull from harbor (push-hero runs
        # before any variant smoke per the §4 DAG so the same
        # digest is uploaded), then retag locally.
        inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True, timeout=10,
        )
        if inspect.returncode != 0:
            log(f"[smoke_variant] tag {full_tag} missing pre-run; attempting tag-recovery")
            all_tags = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True, text=True, timeout=10,
            )
            siblings = [
                t for t in (all_tags.stdout or "").splitlines()
                if image_name in t and t != full_tag
            ]
            recovered = False
            if siblings:
                tag_res = subprocess.run(
                    ["docker", "tag", siblings[0], full_tag],
                    capture_output=True, text=True, timeout=10,
                )
                if tag_res.returncode == 0:
                    log(f"[smoke_variant] tag-recovery: {full_tag} ← {siblings[0]}")
                    recovered = True
                else:
                    log(f"[smoke_variant] tag-recovery failed: {tag_res.stderr.strip()[:200]}")
            if not recovered:
                # Last resort — pull hero from harbor (push-hero
                # always completes before variant smokes per DAG)
                # and retag locally. Byte-identical variant
                # (image/dockerfile or torch-free git/script).
                hero_full_tag = f"{prefix}{image_name}:latest"
                harbor_url_env = env("HARBOR_URL", default="").strip()
                harbor_user = env("HARBOR_USERNAME", default="").strip()
                harbor_pass = env("HARBOR_PASSWORD", default="").strip()
                # docker login if creds available — pull may need it
                if harbor_url_env and harbor_user and harbor_pass:
                    subprocess.run(
                        ["docker", "login", harbor_url_env,
                         "-u", harbor_user, "--password-stdin"],
                        input=harbor_pass, capture_output=True, text=True, timeout=20,
                    )
                pull_res = subprocess.run(
                    ["docker", "pull", hero_full_tag],
                    capture_output=True, text=True, timeout=180,
                )
                if pull_res.returncode == 0:
                    subprocess.run(
                        ["docker", "tag", hero_full_tag, full_tag],
                        capture_output=True, text=True, timeout=10,
                    )
                    log(f"[smoke_variant] tag-recovery via harbor pull: {full_tag} ← {hero_full_tag}")
                else:
                    log(f"[smoke_variant] tag-recovery via harbor pull failed: {pull_res.stderr.strip()[:200]}")

        device_args = list(docker_device_args_for_variant(variant))
        probe_net = probe_network(spec)
        log(f"[smoke_variant] probe network={probe_net}")
        res = subprocess.run(
            ["docker", "run", "--rm", f"--network={probe_net}",
             *device_args,
             "-v", f"{in_dir_host}:/data/input:ro",
             "-v", f"{out_dir_host}:/data/output:rw",
             full_tag],
            capture_output=True, text=True, timeout=120,
        )
        out_files = collect_outputs(out_dir)
        produced_output = has_real_output(out_files)
        expected_output_type = spec.get("output_type") or spec.get("suggested_output_type") or ""

        # Three-layer gate (matches test_and_repair_one_variant + sample_one_test).
        # An error log written to /data/output used to look like real output;
        # the strict gate catches that so per-variant smoke also fails the
        # right way.
        runtime_failure = looks_like_runtime_failure(res.stdout or "", res.stderr or "")
        type_ok, type_reason = output_matches_declared_type(
            out_dir, out_files, expected_output_type,
        )
        accelerator_absence = looks_like_missing_accelerator(
            res.stdout or "", res.stderr or "", variant,
        )
        ok = (
            res.returncode == 0
            and produced_output
            and runtime_failure is None
            and type_ok
            and accelerator_absence is None
        )

        failure_reason = None
        if res.returncode != 0:
            failure_reason = f"container exited {res.returncode}"
        elif not produced_output:
            failure_reason = (
                "container exited 0 but produced no output under /data/output. "
                "The entrypoint is probably a no-op (e.g. `ffmpeg` with no args). "
                "Fix the entrypoint to read /data/input and write /data/output."
            )
        elif runtime_failure is not None:
            failure_reason = (
                f"container exited 0 but stdout/stderr contains a runtime-failure "
                f"marker: {runtime_failure!r}. The script caught a runtime error and "
                f"wrote an error log instead of a real result."
            )
        elif not type_ok:
            failure_reason = (
                f"output does not match declared output_type={expected_output_type!r}: "
                f"{type_reason}"
            )
        elif accelerator_absence is not None:
            failure_reason = (
                f"variant={variant!r} expected an accelerator but stdout/stderr says "
                f"{accelerator_absence!r} — the script silently fell back to CPU. "
                f"Fix the variant's Dockerfile (accelerator wheel, runtime base "
                f"image, LD_LIBRARY_PATH, etc.) so the variant actually uses its "
                f"hardware before it's marked verified."
            )

        payload = {
            "exit_code": res.returncode,
            "stdout_tail": res.stdout[-500:],
            "stderr_tail": res.stderr[-500:],
            "output_files": out_files,
            "produced_output": produced_output,
            "runtime_failure_marker": runtime_failure,
            "type_check_ok": type_ok,
            "type_check_reason": type_reason,
            "expected_output_type": expected_output_type,
            "input_seed": {
                "source": "synth_stub", "input_type": input_type,
                "filename": fname, "size": size,
            },
            "failure_reason": failure_reason,
        }
        log(
            f"[smoke_variant] ok={ok} exit={res.returncode} "
            f"produced_output={produced_output}"
        )
        if failure_reason:
            log(f"[smoke_variant] failure_reason: {failure_reason}")
        report_back(
            api_url, api_key, spec_id=spec_id, stage="smoke", variant=variant,
            success=ok, payload=payload,
        )
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
