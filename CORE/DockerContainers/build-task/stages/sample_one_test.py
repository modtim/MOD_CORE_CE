"""B4 — γ sample-input test on the hero image.

Stage repro: the wizard built and registered a node whose entrypoint
just printed `ffmpeg --help` and exited 0. The old gate (`ok =
res.returncode == 0`) accepted it. Now we require the container to
actually PRODUCE a non-empty output file under /data/output before
the build can register.

If the spec carries `sample_input`, we use it as before. If not, we
synthesize a minimum-valid input for the declared `input_type` — we
do NOT skip silently anymore, because a silent skip is how the empty
ffmpeg node slipped through last time.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

from _common import env, fetch_build_spec, log, probe_network, report_back, resolve_data_host_path
from _synth_input import (
    collect_outputs, has_real_output, looks_like_runtime_failure,
    output_matches_declared_type, synth_input_for,
)


def _seed_input(in_dir: str, spec: dict) -> dict:
    """Drop a real input file into in_dir. Prefer the explicit sample
    from the wizard if present; fall back to a synthesized stub. Returns
    a small `{source, filename, size}` dict for the report-back payload
    so operators can see which path we took."""
    sample = spec.get("sample_input")
    if sample and sample.get("body_b64") and sample.get("filename"):
        body = base64.b64decode(sample["body_b64"])
        in_path = os.path.join(in_dir, os.path.basename(sample["filename"]))
        with open(in_path, "wb") as f:
            f.write(body)
        return {"source": "wizard_sample", "filename": sample["filename"], "size": len(body)}
    input_type = spec.get("input_type") or "image"
    fname, size = synth_input_for(input_type, in_dir)
    return {"source": "synth_stub", "input_type": input_type, "filename": fname, "size": size}


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))

    log(f"[sample_test] spec={spec_id}")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    image_name = spec["image_name"]
    tag = f"{image_name}:latest"

    # Place probe dirs under /data (a docker-managed bind mount from
    # the worker) so we can pass the HOST path to nested `docker run -v`.
    # See _common.resolve_data_host_path for the full rationale.
    import uuid as _uuid
    data_host = resolve_data_host_path()
    probe_id = _uuid.uuid4().hex[:12]
    if data_host:
        tmp = f"/data/sample-test-{probe_id}"
        tmp_host = f"{data_host}/sample-test-{probe_id}"
    else:
        tmp = tempfile.mkdtemp(prefix="sample-test-")
        tmp_host = tmp
    in_dir = os.path.join(tmp, "input"); os.makedirs(in_dir, exist_ok=True)
    out_dir = os.path.join(tmp, "output"); os.makedirs(out_dir, exist_ok=True)
    in_dir_host = os.path.join(tmp_host, "input")
    out_dir_host = os.path.join(tmp_host, "output")
    try:
        seed = _seed_input(in_dir, spec)
        os.chmod(out_dir, 0o777)
        os.chmod(tmp, 0o755)
        log(f"[sample_test] seeded {seed}")

        probe_net = probe_network(spec)
        log(f"[sample_test] probe network={probe_net}")
        res = subprocess.run(
            ["docker", "run", "--rm", f"--network={probe_net}",
             "-v", f"{in_dir_host}:/data/input:ro",
             "-v", f"{out_dir_host}:/data/output:rw",
             tag],
            capture_output=True, text=True, timeout=300,
        )
        out_files = collect_outputs(out_dir)
        produced_output = has_real_output(out_files)
        expected_output_type = spec.get("output_type") or spec.get("suggested_output_type") or ""

        # Three-layer gate (stronger than the legacy any-non-empty rule):
        #   1. exit code must be 0
        #   2. at least one non-empty file must exist in /data/output
        #   3. stdout/stderr must NOT contain a runtime-failure marker
        #      (catches scripts that caught a missing-dep error like
        #      "Is poppler installed and in PATH?" and wrote an error log)
        #   4. produced files must MATCH the declared output_type by
        #      extension + magic bytes (catches "wrote error.json when
        #      output_type=image" and "wrote a renamed text file with a
        #      .png extension"). Unknown / open output_types fall back
        #      to the permissive rule.
        runtime_failure = looks_like_runtime_failure(res.stdout or "", res.stderr or "")
        type_ok, type_reason = output_matches_declared_type(
            out_dir, out_files, expected_output_type,
        )
        ok = (
            res.returncode == 0
            and produced_output
            and runtime_failure is None
            and type_ok
        )
        failure_reason = None
        if res.returncode != 0:
            failure_reason = f"container exited {res.returncode}"
        elif not produced_output:
            failure_reason = (
                "container exited 0 but produced no output files under /data/output — "
                "the entrypoint likely doesn't do work on /data/input (e.g. it just "
                "prints --help). Verify the entrypoint reads from /data/input and "
                "writes to /data/output."
            )
        elif runtime_failure is not None:
            failure_reason = (
                f"container exited 0 but stdout/stderr contains a runtime-failure marker: "
                f"{runtime_failure!r}. The script probably caught a missing dependency / "
                f"import error and wrote an error log instead of real output."
            )
        elif not type_ok:
            failure_reason = (
                f"output does not match declared output_type={expected_output_type!r}: "
                f"{type_reason}"
            )

        payload = {
            "exit_code": res.returncode,
            "stdout_tail": res.stdout[-2000:],
            "stderr_tail": res.stderr[-1000:],
            "output_files": out_files,
            "produced_output": produced_output,
            "runtime_failure_marker": runtime_failure,
            "type_check_ok": type_ok,
            "type_check_reason": type_reason,
            "expected_output_type": expected_output_type,
            "input_seed": seed,
            "failure_reason": failure_reason,
        }
        log(
            f"[sample_test] ok={ok} exit={res.returncode} "
            f"out_files={len(out_files)} produced_output={produced_output} "
            f"runtime_failure={runtime_failure!r} type_ok={type_ok} "
            f"type_reason={type_reason!r}"
        )
        if failure_reason:
            log(f"[sample_test] failure_reason: {failure_reason}")
        report_back(api_url, api_key, spec_id=spec_id, stage="sample_test",
                    variant=None, success=ok, payload=payload)
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
