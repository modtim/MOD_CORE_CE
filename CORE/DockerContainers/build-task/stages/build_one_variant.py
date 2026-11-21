"""B4 — build a single variant of a BuildSpec.

Reads BUILD_SPEC_ID + VARIANT from env. Pulls the BuildSpec from
the API. Materialises the right Dockerfile (hero or per-variant)
into a build context, runs `docker build`, reports the image digest
back to the API.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from _common import (
    build_command, build_subprocess_env, build_timeout_s,
    check_docker_disk_space, docker_login, env, fetch_build_spec,
    log, min_free_gb_default, registry_is_unset, report_back,
)


# Smoke shortcut injected into every entrypoint. smoke_one_variant.py
# runs the image with MOD_VARIANT_SMOKE=1 and `--network=none` and
# expects exit 0 — without this guard, the user's actual script runs
# against empty input dirs and almost always fails.
_SMOKE_GUARD = (
    'if [ "${MOD_VARIANT_SMOKE:-}" = "1" ]; then\n'
    '    echo \'{"smoke":"ok"}\'\n'
    '    exit 0\n'
    'fi\n'
)


def _fetch_and_extract_context(api_url: str, api_key: str, spec_id: int, dest: str) -> None:
    """Stream the raw build-context tarball from the API (scoped build-task
    token) and extract it into `dest`. Safe extraction: reject absolute paths,
    parent traversal, and links (the same guards as the platform's model
    upload)."""
    import io
    import tarfile
    import requests

    url = f"{api_url}/v1/woodsman/build-specs/{spec_id}/context"
    resp = requests.get(url, headers={"X-API-Key": api_key}, timeout=600)
    resp.raise_for_status()
    base = os.path.realpath(dest)
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:*") as tar:
        for m in tar.getmembers():
            if m.islnk() or m.issym():
                raise RuntimeError(f"context tar contains a link member: {m.name}")
            target = os.path.realpath(os.path.join(dest, m.name))
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError(f"context tar member escapes dest: {m.name}")
        tar.extractall(dest)


def _resolve_staged_sources(spec: dict, variant: str) -> dict:
    """Compute the {filename: content} map to write into the build context.

    Base = spec.source_files (the hero source shared by every variant). Then
    the operator's explicit per-variant overrides
    (spec.source_overrides_by_variant[variant], set only by the wizard Preview
    "Rebuild with my changes" flow) are layered ON TOP: an edited file replaces
    the base one; a brand-new filename is added. Empty overrides → the base
    map unchanged, so every normal build is byte-identical to before.
    """
    staged: dict = {}
    for sf in spec.get("source_files", []) or []:
        if isinstance(sf, dict) and sf.get("filename") is not None:
            staged[sf["filename"]] = sf.get("content", "")
    overrides = (spec.get("source_overrides_by_variant") or {}).get(variant) or []
    for ov in overrides:
        fn = ov.get("filename") if isinstance(ov, dict) else None
        if fn:
            staged[fn] = ov.get("content", "")
    return staged


def _inject_smoke_guard(entrypoint_text: str) -> str:
    """Prepend the smoke-probe shortcut to the user's entrypoint, after
    the shebang if present so the right interpreter still picks it up."""
    if not entrypoint_text:
        return "#!/bin/sh\n" + _SMOKE_GUARD
    if entrypoint_text.startswith("#!"):
        eol = entrypoint_text.find("\n")
        if eol == -1:
            return entrypoint_text + "\n" + _SMOKE_GUARD
        return entrypoint_text[: eol + 1] + _SMOKE_GUARD + entrypoint_text[eol + 1 :]
    return "#!/bin/sh\n" + _SMOKE_GUARD + entrypoint_text


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))
    variant = env("VARIANT", default="hero")
    harbor_url = env("HARBOR_URL", default="").strip()

    # Pre-flight: refuse to start the build if the docker daemon's disk
    # is critically low. See _common.check_docker_disk_space for the
    # full repro (TRELLIS.2 thrashed for 5 LLM iterations on bogus
    # "docker socket" errors that were actually disk-pressure cancels).
    disk_warning = check_docker_disk_space(min_gb=min_free_gb_default())
    if disk_warning:
        log(f"[build_variant] ERROR: {disk_warning}")
        report_back(
            api_url, api_key, spec_id=spec_id, stage="build", variant=variant,
            success=False,
            payload={"preflight_failure": "insufficient_disk", "message": disk_warning},
        )
        return 1

    # Pull-side auth for FROM lines that reference private platform Harbor
    # base images (e.g. modtim/base-python). Without this, `docker build`
    # fails on the FROM with `authorization failed: no basic auth credentials`.
    plat_url = env("PLATFORM_HARBOR_URL", default="").strip()
    plat_user = env("PLATFORM_HARBOR_USERNAME", default="").strip()
    plat_pass = env("PLATFORM_HARBOR_PASSWORD", default="").strip()
    if registry_is_unset(plat_url) or registry_is_unset(plat_user) or registry_is_unset(plat_pass):
        # Nothing to authenticate against: no platform Harbor host (CE: the
        # auto-seeded placeholder counts as unset, same as everywhere else), or
        # no creds for the host that IS set. Naming the reason keeps the real
        # base-image error the first thing the build reports.
        log("[build_variant] platform Harbor not configured (unset/placeholder) — "
            "skipping the base-image login")
    elif plat_user and plat_pass:
        try:
            docker_login(plat_url, plat_user, plat_pass)
            log(f"[build_variant] logged in to platform Harbor {plat_url}")
        except Exception as e:
            log(f"[build_variant] platform Harbor login failed (continuing — public bases may still build): {e}")

    log(f"[build_variant] spec={spec_id} variant={variant}")
    spec = fetch_build_spec(api_url, api_key, spec_id)

    if variant == "hero":
        dockerfile = spec["hero_dockerfile"]
    else:
        match = next(
            (v for v in spec.get("variants", []) if v.get("variant") == variant),
            None,
        )
        if not match:
            log(f"variant {variant!r} not found in spec")
            return 1
        dockerfile = match["dockerfile"]

    ctx = tempfile.mkdtemp(prefix="build-variant-")
    try:
        if spec.get("raw_context"):
            # Bring-your-own context: stream the tarball from the API (scoped
            # build-task token) + extract it into the build context, then write
            # the Dockerfile. NO wizard staging (no source_files, no smoke-guard
            # entrypoint) — the image builds exactly as authored.
            _fetch_and_extract_context(api_url, api_key, spec_id, ctx)
            with open(os.path.join(ctx, "Dockerfile"), "w") as f:
                f.write(dockerfile)
            log(f"[build_variant] raw context extracted to {ctx}")
        else:
            # Wizard path: stage inline text source_files + Dockerfile + a
            # smoke-guarded entrypoint.
            staged = _resolve_staged_sources(spec, variant)
            n_over = len((spec.get("source_overrides_by_variant") or {}).get(variant) or [])
            if n_over:
                log(f"[build_variant] applying {n_over} operator source "
                    f"override(s) for variant={variant}")
            for filename, content in staged.items():
                with open(os.path.join(ctx, filename), "w") as f:
                    f.write(content)
            with open(os.path.join(ctx, "Dockerfile"), "w") as f:
                f.write(dockerfile)
            with open(os.path.join(ctx, "entrypoint.sh"), "w") as f:
                f.write(_inject_smoke_guard(spec.get("entrypoint_sh", "")))

        image_name = spec["image_name"]
        tag_suffix = "" if variant == "hero" else f"-{variant}"
        # Tag with Harbor prefix so push_variant can push without
        # retagging. Tenant-scoped to avoid name collisions across
        # tenants (matches legacy ImageBuilder convention).
        tenant_id = spec.get("tenant_id", 0)
        prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
        full_tag = f"{prefix}{image_name}:latest{tag_suffix}"

        # Byte-identical variant short-circuit: when this variant's
        # Dockerfile equals hero's (image / dockerfile mode always;
        # git / script mode when no torch URL split), rebuilding it
        # in parallel via buildx races against the daemon image
        # store and tags drop (manual-test repro 2026-05-23 spec=
        # 70/71 push-* "No such image" after concurrent variant
        # builds). Just retag from hero — same digest, no race, no
        # wasted CPU.
        hero_match = next(
            (v for v in spec.get("variants", []) if v.get("variant") == variant),
            None,
        )
        variant_df = (hero_match or {}).get("dockerfile") if hero_match else None
        if (
            variant != "hero"
            and variant_df is not None
            and variant_df == spec.get("hero_dockerfile")
        ):
            hero_tag = f"{prefix}{image_name}:latest"
            inspect_hero = subprocess.run(
                ["docker", "image", "inspect", "--format", "{{.Id}}", hero_tag],
                capture_output=True, text=True,
            )
            hero_id = inspect_hero.stdout.strip()
            if hero_id:
                tag_res = subprocess.run(
                    ["docker", "tag", hero_id, full_tag],
                    capture_output=True, text=True,
                )
                if tag_res.returncode == 0:
                    log(f"[build_variant] retag from hero: {full_tag} ← {hero_id}")
                    report_back(
                        api_url, api_key, spec_id=spec_id, stage="build",
                        variant=variant, success=True,
                        payload={"image": full_tag, "digest": hero_id, "retag_from_hero": True},
                    )
                    return 0
                log(f"[build_variant] retag failed: {tag_res.stderr.strip()[:200]}; falling back to buildx")
            else:
                log(f"[build_variant] hero tag {hero_tag} missing; falling back to buildx")

        log(f"[build_variant] building {full_tag}")
        # `docker buildx build` (assembled in _common.build_command, the one
        # place the build argv lives) with attestations EXPLICITLY DISABLED:
        #
        #   --provenance=false  drops the in-toto provenance manifest
        #   --sbom=false        drops the SBOM attestation manifest
        #   --output=type=docker  loads the result into the local
        #                         daemon's image store (default for
        #                         buildx is type=cacheonly, which would
        #                         leave nothing for push_one_variant.py
        #                         to actually push)
        #
        # WHY: with default buildx settings, the produced image is an
        # OCI image *index* that references an attestation manifest
        # plus a 32-byte empty-layer blob. `docker push` against
        # Harbor uploads the index + main image manifest but never
        # uploads the 32-byte blob, and Harbor rejects the manifest
        # with `error from registry: blob sha256:4f4fb700... not
        # found`. We need BuildKit for `COPY --chmod=...` in synth-
        # generated dockerfiles, so disabling BuildKit isn't an
        # option — but we don't need attestations.
        # GSEC FX.16 R-023: no API_KEY / registry creds in the buildx
        # child env (untrusted Dockerfile surface); daemon auth comes
        # from the earlier `docker login --password-stdin`.
        build_env = build_subprocess_env()
        timeout_s = build_timeout_s()
        try:
            res = subprocess.run(
                build_command(full_tag, ctx),
                capture_output=True, text=True, env=build_env,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"docker build exceeded BUILD_TIMEOUT_S={timeout_s}s and was killed"
            log(f"[build_variant] {msg}")
            tail = exc.stderr if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
            report_back(
                api_url, api_key, spec_id=spec_id, stage="build", variant=variant,
                success=False,
                payload={"stage_failed": "build_timeout", "message": msg,
                         "stderr_tail": tail[-1000:]},
            )
            return 124
        if res.returncode != 0:
            log(f"[build_variant] docker build failed: {res.stderr[-1000:]}")
            report_back(
                api_url, api_key, spec_id=spec_id, stage="build", variant=variant,
                success=False, payload={"stderr_tail": res.stderr[-1000:]},
            )
            return res.returncode

        # Get digest for reporting + force-tag belt-and-suspenders.
        # Concurrent `docker buildx build --output=type=docker` calls
        # for byte-identical Image-mode variants race against each
        # other; sometimes a variant's tag drops off the daemon's
        # image-list view even though build reported success
        # (manual-test repro 2026-05-23 spec=45 push-rocm:
        # `daemon-MISSING-tag=...latest-rocm` while latest-intel WAS
        # there). Explicit `docker tag` after build forces the tag
        # to exist regardless of buildkit's load semantics —
        # idempotent + cheap.
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True,
        )
        digest = inspect.stdout.strip()
        if not digest:
            # Tag missing — buildx race. Try to recover by tagging
            # from the digest the build emitted (last `=>` line is
            # the image id).
            log(
                f"[build_variant] warn: tag {full_tag} missing after "
                f"build (buildx race?). Re-tagging from hero if "
                f"available."
            )
            # Hero is always present at push time (this stage runs
            # AFTER hero test_and_repair completion via the §4
            # chunk-4 DAG gate). Re-tag from hero — for Image mode
            # variants are byte-identical to hero anyway.
            hero_tag = f"{prefix}{image_name}:latest"
            re_inspect = subprocess.run(
                ["docker", "inspect", "--format", "{{.Id}}", hero_tag],
                capture_output=True, text=True,
            )
            hero_id = re_inspect.stdout.strip()
            if hero_id:
                subprocess.run(
                    ["docker", "tag", hero_id, full_tag],
                    capture_output=True, text=True,
                )
                inspect = subprocess.run(
                    ["docker", "inspect", "--format", "{{.Id}}", full_tag],
                    capture_output=True, text=True,
                )
                digest = inspect.stdout.strip()
                log(
                    f"[build_variant] recovered tag {full_tag} from "
                    f"hero ({hero_tag}) → {digest}"
                )
        log(f"[build_variant] success: {digest}")

        report_back(
            api_url, api_key, spec_id=spec_id, stage="build", variant=variant,
            success=True, payload={"image": full_tag, "digest": digest},
        )
        return 0
    finally:
        shutil.rmtree(ctx, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
