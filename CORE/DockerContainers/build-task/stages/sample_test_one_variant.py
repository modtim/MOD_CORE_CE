"""wizard-rebuild §5 — per-variant sample-test stage.

Runs after each ``push-<variant>`` task in the build DAG. For the
named VARIANT this stage:

  1. Picks the variant's image tag — same convention as smoke /
     push (``stage-harbor.../tenant-X/<image_name>:latest[-variant]``).
  2. Seeds synthetic input (or the wizard's sample input, if
     attached to the spec) under /data/input.
  3. Runs the variant image with /data I/O bind mounts. Captures
     the largest produced file under /data/output.
  4. POSTs the result + output bytes to the API:
     ``POST /v1/woodsman/wizard/variant-sample-test-result``
     The API persists the output to MinIO, scores it via the
     `variant_test.dispatcher`, and returns a verdict
     ({verified, marginal, drift, failed, incompatible}).
  5. Reports back to the orchestrator. Exit code:
     - 0 when the API returns ``publish_ok=True`` (hero verified /
       marginal, or any non-hero verdict including drift /
       incompatible since those don't fail the workflow).
     - 1 when ``publish_ok=False`` (hero failed publish gate, or
       the docker run errored before we got a verdict).

Why the API does the scoring (vs computing locally in the build-task):
  - The comparer needs to read the reference variant's stored output
    from MinIO — and only the API has the bucket creds. Doing it in
    the build-task would require us to forward those creds through
    yet another env var path.
  - Centralizes the scoring logic so re-scoring on reference swap
    (§5c) uses the same path.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile

import requests

from _common import (
    env, fetch_build_spec, log, report_back, resolve_data_host_path,
)
from _synth_input import (
    collect_outputs, docker_device_args_for_variant, has_real_output,
    looks_like_missing_accelerator, looks_like_runtime_failure,
    output_matches_declared_type, synth_input_for,
)


_MAX_UPLOAD_BYTES = 32 * 1024 * 1024  # 32 MB cap — bigger payloads get
                                       # uploaded by the wizard via MinIO
                                       # presign, not inline through the API.


def _seed_input(in_dir: str, spec: dict) -> dict:
    """Drop a real input file into in_dir. Prefer the wizard's sample
    when attached, else a synthesized minimum-valid stub. Returns a
    ``{source, filename, size}`` dict for the report-back payload."""
    sample = spec.get("sample_input") or {}
    if sample and sample.get("body_b64") and sample.get("filename"):
        body = base64.b64decode(sample["body_b64"])
        in_path = os.path.join(in_dir, os.path.basename(sample["filename"]))
        with open(in_path, "wb") as f:
            f.write(body)
        return {
            "source": "wizard_sample",
            "filename": sample["filename"],
            "size": len(body),
        }
    input_type = spec.get("input_type") or "image"
    fname, size = synth_input_for(input_type, in_dir)
    return {
        "source": "synth_stub", "input_type": input_type,
        "filename": fname, "size": size,
    }


def _largest_output(out_dir: str, out_files: list[dict]) -> tuple[str, bytes]:
    """Return ``(filename, bytes)`` for the largest produced file
    under /data/output, or ``("", b"")`` if none qualifies.

    `out_files` is the output of `_synth_input.collect_outputs`,
    which returns ``{"name": <basename>, "size": <int>}`` dicts.
    """
    if not out_files:
        return "", b""
    largest = max(out_files, key=lambda f: int(f.get("size") or 0))
    fname = largest.get("name") or largest.get("filename") or ""
    if not fname:
        return "", b""
    path = os.path.join(out_dir, fname)
    if not os.path.isfile(path):
        return "", b""
    try:
        with open(path, "rb") as f:
            data = f.read(_MAX_UPLOAD_BYTES)
        return fname, data
    except Exception as exc:
        log(f"[sample_test_variant] failed reading {path}: {exc}")
        return "", b""


def _call_api(api_url: str, api_key: str, payload: dict) -> dict:
    """POST the result + base64-encoded output to the API.

    Returns the parsed JSON response on 200 OK; raises on any other
    status so the caller can fall back to the failure path.
    """
    r = requests.post(
        f"{api_url}/v1/woodsman/wizard/variant-sample-test-result",
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"variant-sample-test-result returned {r.status_code}: "
            f"{r.text[:500]}"
        )
    return r.json()


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))
    variant = env("VARIANT", default="hero")

    log(f"[sample_test_variant] spec={spec_id} variant={variant}")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    image_name = spec["image_name"]
    tag_suffix = "" if variant == "hero" else f"-{variant}"
    harbor_url = env("HARBOR_URL", default="").strip()
    tenant_id = spec.get("tenant_id", 0)
    prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
    full_tag = f"{prefix}{image_name}:latest{tag_suffix}"

    # Probe dirs under /data so we can pass the host path to nested
    # `docker run -v` (see _common.resolve_data_host_path).
    import uuid as _uuid
    data_host = resolve_data_host_path()
    probe_id = _uuid.uuid4().hex[:12]
    if data_host:
        tmp = f"/data/sample-{variant}-{probe_id}"
        tmp_host = f"{data_host}/sample-{variant}-{probe_id}"
    else:
        tmp = tempfile.mkdtemp(prefix=f"sample-{variant}-")
        tmp_host = tmp
    in_dir = os.path.join(tmp, "input")
    os.makedirs(in_dir, exist_ok=True)
    out_dir = os.path.join(tmp, "output")
    os.makedirs(out_dir, exist_ok=True)
    in_dir_host = os.path.join(tmp_host, "input")
    out_dir_host = os.path.join(tmp_host, "output")

    try:
        seed = _seed_input(in_dir, spec)
        os.chmod(out_dir, 0o777)
        os.chmod(tmp, 0o755)
        log(f"[sample_test_variant] seeded {seed}")

        # Pre-flight: same tag-recovery pattern as smoke_one_variant.
        # Concurrent variant builds against the same digest can drop
        # tags under docker 29.4.0 + buildkit's containerd snapshotter
        # race. (1) re-tag from sibling; (2) docker pull from harbor.
        inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True, timeout=10,
        )
        if inspect.returncode != 0:
            log(f"[sample_test_variant] tag {full_tag} missing pre-run; attempting tag-recovery")
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
                    log(f"[sample_test_variant] tag-recovery: {full_tag} ← {siblings[0]}")
                    recovered = True
                else:
                    log(f"[sample_test_variant] tag-recovery failed: {tag_res.stderr.strip()[:200]}")
            if not recovered:
                hero_full_tag = f"{prefix}{image_name}:latest"
                harbor_url_env = env("HARBOR_URL", default="").strip()
                harbor_user = env("HARBOR_USERNAME", default="").strip()
                harbor_pass = env("HARBOR_PASSWORD", default="").strip()
                if harbor_url_env and harbor_user and harbor_pass:
                    subprocess.run(
                        ["docker", "login", harbor_url_env,
                         "-u", harbor_user, "--password-stdin"],
                        input=harbor_pass, capture_output=True, text=True, timeout=20,
                    )
                # Pull THIS variant's tag if it's already on harbor
                # (push-<variant> uploads byte-identical-mode variants
                # via registry-side copy; hero is eager-pushed). Fall
                # back to hero if the variant isn't on harbor yet.
                pull_target = full_tag
                pull_res = subprocess.run(
                    ["docker", "pull", pull_target],
                    capture_output=True, text=True, timeout=300,
                )
                if pull_res.returncode != 0 and full_tag != hero_full_tag:
                    pull_target = hero_full_tag
                    pull_res = subprocess.run(
                        ["docker", "pull", pull_target],
                        capture_output=True, text=True, timeout=300,
                    )
                if pull_res.returncode == 0:
                    if pull_target != full_tag:
                        subprocess.run(
                            ["docker", "tag", pull_target, full_tag],
                            capture_output=True, text=True, timeout=10,
                        )
                    log(f"[sample_test_variant] tag-recovery via harbor pull: {full_tag} ← {pull_target}")
                else:
                    log(f"[sample_test_variant] tag-recovery via harbor pull failed: {pull_res.stderr.strip()[:200]}")

        device_args = list(docker_device_args_for_variant(variant))
        res = subprocess.run(
            ["docker", "run", "--rm", "--network=none",
             *device_args,
             "-v", f"{in_dir_host}:/data/input:ro",
             "-v", f"{out_dir_host}:/data/output:rw",
             full_tag],
            capture_output=True, text=True, timeout=300,
        )
        out_files = collect_outputs(out_dir)
        any_non_empty = has_real_output(out_files)
        expected_output_type = spec.get("output_type") or spec.get("suggested_output_type") or ""

        # Three-layer strict gate (matches hero's sample_one_test +
        # test_and_repair smoke probe). Non-hero variant verdicts don't
        # block register, but they DO drive the per-variant chip state
        # in the UI — passing an error-log as "verified" is exactly the
        # wrong signal to surface.
        runtime_failure = looks_like_runtime_failure(res.stdout or "", res.stderr or "")
        type_ok, type_reason = output_matches_declared_type(
            out_dir, out_files, expected_output_type,
        )
        accelerator_absence = looks_like_missing_accelerator(
            res.stdout or "", res.stderr or "", variant,
        )
        produced_output = (
            any_non_empty
            and runtime_failure is None
            and type_ok
            and accelerator_absence is None
        )

        output_filename, output_bytes = _largest_output(out_dir, out_files)
        if not produced_output:
            output_bytes = b""

        log(
            f"[sample_test_variant] container exit={res.returncode} "
            f"out_files={len(out_files)} produced_output={produced_output} "
            f"runtime_failure={runtime_failure!r} type_ok={type_ok} "
            f"largest_filename={output_filename!r} largest_size={len(output_bytes)}"
        )
        if res.returncode != 0 or not produced_output:
            # Surface container output so the operator can see why
            # the entrypoint didn't produce usable bytes. Truncated
            # so the log_tail field stays under PG TOAST limits.
            _stdout = (res.stdout or "")[-1500:]
            _stderr = (res.stderr or "")[-1500:]
            log(f"[sample_test_variant] container stdout: {_stdout}")
            log(f"[sample_test_variant] container stderr: {_stderr}")

        # POST to API — it does the scoring + persistence.
        api_payload = {
            "build_spec_id": spec_id,
            "variant": variant,
            "exit_code": res.returncode,
            "produced_output": produced_output,
            "output_type": spec.get("output_type") or "binary",
            "output_filename": output_filename,
            "output_body_b64": (
                base64.b64encode(output_bytes).decode("ascii")
                if output_bytes else ""
            ),
        }
        try:
            verdict_resp = _call_api(api_url, api_key, api_payload)
        except Exception as exc:
            log(f"[sample_test_variant] API verdict call failed: {exc}")
            verdict_resp = {
                "verdict": "failed" if variant == "hero" else "incompatible",
                "score": 0.0,
                "publish_ok": False,
                "scored_against": None,
                "is_marginal": False,
                "_api_error": str(exc)[:300],
            }

        _score = verdict_resp.get("score")
        _score_s = f"{_score:.3f}" if isinstance(_score, (int, float)) else "n/a"
        log(
            f"[sample_test_variant] verdict={verdict_resp.get('verdict')!r} "
            f"score={_score_s} publish_ok={verdict_resp.get('publish_ok')}"
        )

        # §6 — non-hero sample-tests run AFTER register has fired
        # (hero gates register; non-hero runs in background per the
        # rolling-verification DAG). Notify the API so it can
        # add/remove this variant from the registered Node's
        # gpu_variants map retroactively. The endpoint is idempotent
        # and a no-op when register hasn't yet fired (e.g. for a
        # legacy DAG where non-hero also blocks register).
        if variant != "hero":
            verdict = verdict_resp.get("verdict") or ""
            try:
                _vv = requests.post(
                    f"{api_url}/v1/woodsman/wizard/variant-verified",
                    headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                    json={
                        "build_spec_id": spec_id,
                        "variant": variant,
                        "verdict": verdict,
                        "image_ref": full_tag,
                    },
                    timeout=20,
                )
                if _vv.status_code == 200:
                    _vd = _vv.json()
                    log(
                        f"[sample_test_variant] variant-verified callback: "
                        f"nd_id={_vd.get('node_definition_id')} "
                        f"gpu_variants_count={len(_vd.get('gpu_variants_after') or {})}"
                    )
                else:
                    log(
                        f"[sample_test_variant] variant-verified callback "
                        f"returned {_vv.status_code}: {_vv.text[:200]}"
                    )
            except Exception as exc:
                log(f"[sample_test_variant] variant-verified callback failed: {exc}")

        report_back(
            api_url, api_key, spec_id=spec_id, stage="sample_test_variant",
            variant=variant,
            success=bool(verdict_resp.get("publish_ok")),
            payload={
                "exit_code": res.returncode,
                "stdout_tail": (res.stdout or "")[-1000:],
                "stderr_tail": (res.stderr or "")[-1000:],
                "input_seed": seed,
                "out_files": out_files,
                "verdict": verdict_resp.get("verdict"),
                "score": verdict_resp.get("score"),
                "scored_against": verdict_resp.get("scored_against"),
                "is_marginal": verdict_resp.get("is_marginal"),
            },
        )
        return 0 if verdict_resp.get("publish_ok") else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
