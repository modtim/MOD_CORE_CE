"""B4 — push a single variant to Harbor.

Uses `skopeo copy docker-daemon:<image> docker://<registry>/<ref>`.
Despite the marketing, skopeo's `docker-daemon:` transport DOES
materialize the full image to a temporary tarball before uploading
(it calls `docker save` internally). The default temp location is
`$TMPDIR` → `/var/tmp` → `/tmp`, and the worker's hardened sandbox
caps both at tens of MB of tmpfs — guaranteed to fail for TRELLIS-
class 9-32 GB images.

We override `TMPDIR` to `/data`, which is the per-task host-disk-
backed volume the worker bind-mounts at `:rw` (see
`executor.go:1034`). Disk peak per push is one full image tarball
worth on real disk (host has 100+ GB free); no RAM pressure, no
concurrent-push collision (each task gets its own volume).

Why not `docker push`: docker push hangs all blob uploads in
"Waiting" against tenant-scoped Harbor repos then fails manifest
validation with "blob sha256:<X> not found" (different blob per
attempt — never converges). Reproduced on docker 29.4.0 + Harbor
2.x with both tenant-robot and admin creds. Almost certainly a
docker push bug in cross-repo blob-mount handling when the
destination tenant has adjacent repos.

Why not `crane push <tar>`: crane works correctly but only accepts
a file/dir path as input. Saving a 9-32 GB image to a tarball
first exhausts the build-task container's writable layer when 4
push tasks run concurrently. Wasted disk and a race condition.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from _common import (
    anonymous_registry_push, env, fetch_build_spec, local_publish_mode, log,
    report_back,
)
from _seal_scan import scan as seal_scan


def _pushed_digest(full_tag: str) -> str:
    """The pushed image's content digest, for the API to record on the
    rebuilt node (node-version-pinning GA.NMF.F8). Prefers RepoDigests (the
    canonical registry digest — the `:latest` ref's sha256, what the registry
    serves and the pin path compares) over `.Id` (local layer id), exactly like
    the legacy ImageBuilder's manifest_emit._image_digest. Returns "" on any
    failure — recording is best-effort, and the API treats an empty digest as
    "nothing to record"."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", full_tag],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if "@" in out:
            return out.split("@", 1)[1]
    except Exception:  # noqa: BLE001 — best-effort
        pass
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or ""
    except Exception:  # noqa: BLE001 — best-effort
        return ""


def _local_image_id(full_tag: str) -> str:
    """The local daemon's image Id (``sha256:...``) for ``full_tag``, "" if
    the tag is not in the daemon. This is the digest a local-only image is
    pinned by: the worker's ``imageDigestForInspect`` (executor.go) prefers
    RepoDigests, which a never-pushed image does not have, and falls back to
    the very same Id — so the recorded pin matches the worker's local check."""
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", full_tag],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out if out.startswith("sha256:") else ""
    except Exception:  # noqa: BLE001
        return ""


def _push_local(api_url: str, api_key: str, spec_id: int, variant: str,
                image_name: str, full_tag: str) -> int:
    """Local-publish mode: no registry is configured (Community install), the
    built image already lives in the local daemon under ``full_tag`` and the
    embedded worker runs it from there. Nothing to upload: report the local
    image Id as the digest and succeed."""
    image_id = _local_image_id(full_tag)
    if not image_id:
        # Same buildx tag-drop race the registry path recovers from: re-tag
        # from any sibling tag of this image_name.
        try:
            tags = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True, text=True, timeout=10,
            ).stdout.splitlines()
            sibling = next((t for t in tags if image_name in t and t != full_tag), None)
            if sibling:
                log(f"[push_variant] local tag-recovery: docker tag {sibling} {full_tag}")
                subprocess.run(["docker", "tag", sibling, full_tag],
                               capture_output=True, text=True, timeout=10)
                image_id = _local_image_id(full_tag)
        except Exception as exc:  # noqa: BLE001
            log(f"[push_variant] local tag-recovery failed: {exc}")
    if not image_id:
        log(f"[push_variant] local publish FAILED: image {full_tag} is not in the "
            f"local docker daemon")
        report_back(
            api_url, api_key, spec_id=spec_id, stage="push", variant=variant,
            success=False, payload={
                "exit_code": 1, "stage_failed": "local_image_missing",
                "image": full_tag, "pusher": "local-noop",
            },
        )
        return 1
    log(f"[push_variant] no registry configured (local publish): skipping push, "
        f"image {full_tag} stays on the local docker daemon, digest={image_id}")
    report_back(
        api_url, api_key, spec_id=spec_id, stage="push", variant=variant,
        success=True, payload={
            "exit_code": 0, "image": full_tag, "pusher": "local-noop",
            "digest": image_id, "local_only": True,
        },
    )
    return 0


def main() -> int:
    api_url = env("API_URL", required=True)
    api_key = env("API_KEY", required=True)
    spec_id = int(env("BUILD_SPEC_ID", required=True))
    variant = env("VARIANT", default="hero")
    harbor_url = env("HARBOR_URL", default="ghcr.io")
    if (env("MOD_PUBLISH_MODE", default="") or "").strip().lower() == "local" \
            and "HARBOR_URL" not in os.environ:
        # Local-publish install that carries no registry host at all: never
        # fall through to the platform default above.
        harbor_url = ""
    harbor_user = env("HARBOR_USERNAME", default="")
    harbor_pass = env("HARBOR_PASSWORD", default="")

    log(f"[push_variant] spec={spec_id} variant={variant}")
    spec = fetch_build_spec(api_url, api_key, spec_id)
    image_name = spec["image_name"]
    tag_suffix = "" if variant == "hero" else f"-{variant}"
    tenant_id = spec.get("tenant_id", 0)
    prefix = f"{harbor_url}/tenant-{tenant_id}/" if harbor_url else ""
    full_tag = f"{prefix}{image_name}:latest{tag_suffix}"
    local_mode = local_publish_mode(harbor_url)
    anon_mode = anonymous_registry_push(harbor_url, harbor_user, harbor_pass)

    # skopeo accepts dest creds on the CLI — no `docker login`
    # equivalent needed. Empty creds → anonymous (which Harbor
    # rejects for push), so always require non-empty. (Local-publish mode has
    # no registry at all, so there is nothing to authenticate to.)
    if not local_mode and not anon_mode and (not harbor_user or not harbor_pass):
        msg = "[push_variant] missing HARBOR_USERNAME or HARBOR_PASSWORD"
        log(msg)
        report_back(
            api_url, api_key, spec_id=spec_id, stage="push", variant=variant,
            success=False, payload={
                "exit_code": 1,
                "stage_failed": "creds_missing",
                "image": full_tag,
                "pusher": "skopeo",
            },
        )
        return 1

    # Force skopeo's intermediate tarball onto the host-disk volume
    # at /data. Skopeo IGNORES $TMPDIR for big-file scratch — the
    # `containers/image` library it embeds hardcodes /var/tmp via
    # `systemDefaultTmpdir`. The only way to override is the top-level
    # `--tmpdir` CLI flag, which sets `SystemContext.BigFilesTemporaryDir`.
    # Without this, TRELLIS-class images (9-32 GB) trip the 64 MB tmpfs
    # cap on /var/tmp from `Worker/internal/executor/sandbox.go:113`.
    import pathlib
    tmpdir = "/data/skopeo-tmp"
    if not local_mode and not anon_mode:
        pathlib.Path(tmpdir).mkdir(parents=True, exist_ok=True)

    # Byte-identical variant short-circuit: push-hero has already
    # uploaded the same digest to harbor (variants[i].dockerfile ==
    # hero_dockerfile for image/dockerfile mode and torch-free
    # git/script). A registry-to-registry skopeo copy from the
    # already-pushed hero is cheap and bypasses the daemon entirely,
    # sidestepping the "tag vanishes between build and push" race
    # that plagues the docker-daemon transport under concurrent load
    # (manual-test repro 2026-05-23 multiple specs: smoke-X passes
    # but push-X fails with `daemon-tags-matching-image_name: []`
    # even though sibling variants of the same digest pushed fine).
    hero_match = next(
        (v for v in spec.get("variants") or [] if v.get("variant") == variant),
        None,
    )
    variant_df = (hero_match or {}).get("dockerfile") if hero_match else None
    use_registry_copy = (
        not local_mode
        and not anon_mode
        and variant != "hero"
        and variant_df is not None
        and variant_df == spec.get("hero_dockerfile")
    )

    if use_registry_copy:
        hero_full_tag = f"{prefix}{image_name}:latest"
        cmd = [
            "skopeo",
            "--tmpdir", tmpdir,
            "copy",
            "--src-creds", f"{harbor_user}:{harbor_pass}",
            "--dest-creds", f"{harbor_user}:{harbor_pass}",
            f"docker://{hero_full_tag}",
            f"docker://{full_tag}",
        ]
        log(f"[push_variant] skopeo registry-copy {hero_full_tag} → {full_tag}")
    else:
        cmd = [
            "skopeo",
            "--tmpdir", tmpdir,
            "copy",
            "--dest-creds", f"{harbor_user}:{harbor_pass}",
            f"docker-daemon:{full_tag}",
            f"docker://{full_tag}",
        ]
        log(f"[push_variant] skopeo {full_tag} → harbor (--tmpdir {tmpdir})")

    # Diagnostic: dump the daemon's image list before skopeo runs so
    # the "No such image" failure mode (manual-test repro 2026-05-22,
    # spec=28 — image vanished between test_and_repair PASS and this
    # task) leaves a trail of what the daemon actually had. Cheap;
    # only fires once per push.
    #
    # Tag-recovery: when the named tag is missing, look for ANY
    # other tag on the daemon for this image_name (byte-identical
    # variants share a digest — hero + 4 variants for Image and
    # Dockerfile modes). If we find one, re-tag it under the
    # expected name. This rescues hero pushes from buildx-race tag
    # drops that build_one_variant's own recovery can't see
    # (manual-test repro 2026-05-23 spec=52 Test D: latest, -intel,
    # -rocm all dropped after parallel variant builds; -cuda and
    # -npu still present).
    try:
        listing = subprocess.run(
            ["docker", "image", "inspect", "--format",
             "{{.Id}} {{join .RepoTags \" \"}}", full_tag],
            capture_output=True, text=True, timeout=10,
        )
        if listing.returncode == 0:
            log(f"[push_variant] daemon-has-tag={full_tag}: {listing.stdout.strip()}")
        else:
            log(f"[push_variant] daemon-MISSING-tag={full_tag} "
                f"(stderr: {listing.stderr.strip()[:200]})")
            # Find any sibling tag matching image_name and re-tag from it.
            all_tags = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True, text=True, timeout=10,
            )
            sibling_candidates = [
                t for t in (all_tags.stdout or "").splitlines()
                if image_name in t and t != full_tag
            ]
            log(f"[push_variant] daemon-tags-matching-image_name={image_name}: "
                f"{sibling_candidates[:10]}")
            if sibling_candidates:
                sibling = sibling_candidates[0]
                log(f"[push_variant] attempting tag-recovery: docker tag {sibling} {full_tag}")
                tag_res = subprocess.run(
                    ["docker", "tag", sibling, full_tag],
                    capture_output=True, text=True, timeout=10,
                )
                if tag_res.returncode == 0:
                    log(f"[push_variant] tag-recovery success: {full_tag} ← {sibling}")
                else:
                    log(f"[push_variant] tag-recovery failed: {tag_res.stderr.strip()[:200]}")
            elif not local_mode and not anon_mode:
                # Harbor short-circuit: test_and_repair eager-pushes
                # hero at the end of its successful run, so the tag
                # may already be on harbor even when the daemon
                # dropped it. Inspect harbor; if present, report
                # success without re-uploading.
                inspect_res = subprocess.run(
                    ["skopeo", "inspect", "--raw",
                     f"--creds={harbor_user}:{harbor_pass}",
                     f"docker://{full_tag}"],
                    capture_output=True, text=True, timeout=30,
                )
                if inspect_res.returncode == 0:
                    log(f"[push_variant] harbor-short-circuit: {full_tag} "
                        f"already on harbor (eager-push)")
                    # The tag isn't on the local daemon here (that's why we
                    # short-circuit), so the content digest comes from the raw
                    # manifest skopeo just fetched — the repo digest is the
                    # sha256 of the manifest bytes.
                    import hashlib as _hashlib
                    _raw_digest = "sha256:" + _hashlib.sha256(
                        inspect_res.stdout.encode("utf-8", "replace")
                    ).hexdigest()
                    report_back(
                        api_url, api_key, spec_id=spec_id, stage="push",
                        variant=variant, success=True,
                        payload={
                            "exit_code": 0, "image": full_tag,
                            "pusher": "harbor-short-circuit",
                            "digest": _raw_digest,
                        },
                    )
                    return 0
                log(f"[push_variant] harbor short-circuit: tag NOT on harbor either: "
                    f"{inspect_res.stderr.strip()[:200]}")
    except Exception as _diag_exc:
        log(f"[push_variant] image-inspect diagnostic failed: {_diag_exc}")

    # -----------------------------------------------------------------------
    # Seal scan C23 (declared-egress) — warn or enforce before push
    # -----------------------------------------------------------------------
    lint_mode = env("MOD_SEALED_LINT", default="warn")
    if lint_mode not in ("warn", "enforce"):
        lint_mode = "warn"

    # Gather Dockerfile + source files for the scan.
    hero_match = next(
        (v for v in spec.get("variants") or [] if v.get("variant") == variant),
        None,
    )
    variant_df = (hero_match or {}).get("dockerfile") if hero_match else spec.get("hero_dockerfile", "")

    # Build source map: base files + per-variant overrides.
    staged_sources: dict = {}
    for sf in (spec.get("source_files") or []):
        if isinstance(sf, dict) and sf.get("filename") is not None:
            staged_sources[sf["filename"]] = sf.get("content", "")
    for ov in ((spec.get("source_overrides_by_variant") or {}).get(variant) or []):
        if isinstance(ov, dict) and ov.get("filename"):
            staged_sources[ov["filename"]] = ov.get("content", "")

    findings = []
    if variant_df:
        findings = seal_scan(variant_df, staged_sources)

    if findings and lint_mode == "enforce":
        log(
            f"[push_variant] seal scan FAILED: {len(findings)} finding(s) "
            f"in {spec.get('image_name', image_name)} — blocking push"
        )
        report_back(
            api_url, api_key, spec_id=spec_id, stage="push", variant=variant,
            success=False, payload={
                "exit_code": 1,
                "image": full_tag,
                "seal_findings": [
                    {"path": f.path, "line": f.line, "rule": f.rule, "snippet": f.snippet}
                    for f in findings
                ],
            },
        )
        return 1

    if findings:
        log(
            f"[push_variant] seal scan WARNING: {len(findings)} finding(s), "
            f"pushing anyway (mode=warn)"
        )

    if local_mode:
        return _push_local(api_url, api_key, spec_id, variant, image_name, full_tag)

    # Push. Two paths:
    #  - registry-copy (skopeo docker://→docker://): registry-to-registry, no
    #    daemon tar-export, reliable — keep skopeo here.
    #  - daemon push: use `docker push`, NOT skopeo's docker-daemon transport.
    #    skopeo exports the whole image to a tar first, which trips constantly on
    #    large images and on blobs the worker's post-build prune removed
    #    ("loading tar component blobs/sha256/…: file does not exist"). `docker
    #    push` has the daemon push its own image directly — no tar export.
    def _run(c, **kw):
        return subprocess.run(c, capture_output=True, text=True, **kw)

    def _retry(mk, tries=3, sleep_s=6):
        r = mk()
        for attempt in range(1, tries):
            if r.returncode == 0:
                break
            log(f"[push_variant] push retry {attempt}/{tries - 1} "
                f"(rc={r.returncode}): {(r.stderr or '')[-200:]}")
            time.sleep(sleep_s)
            r = mk()
        return r

    registry_host = full_tag.split("/", 1)[0]
    logged_in = False
    if use_registry_copy:
        pusher = "skopeo-registry"
        res = _retry(lambda: _run(cmd))
    else:
        pusher = "docker-push"
        if anon_mode:
            # Auth-less registry (Community's optional registry:2): nothing to
            # log in to, push straight from the daemon.
            res = _retry(lambda: _run(["docker", "push", full_tag]))
        else:
            login = _run(["docker", "login", registry_host, "-u", harbor_user, "--password-stdin"],
                         input=harbor_pass)
            if login.returncode != 0:
                log(f"[push_variant] docker login {registry_host} failed: {(login.stderr or '')[-200:]}")
                res = login
            else:
                logged_in = True
                res = _retry(lambda: _run(["docker", "push", full_tag]))

    ok = res.returncode == 0
    if logged_in:
        _run(["docker", "logout", registry_host])

    # Don't echo HARBOR_PASSWORD in error payloads — strip it before reporting.
    stderr_safe = (res.stderr or "").replace(harbor_pass, "***") if harbor_pass else (res.stderr or "")
    stdout_safe = (res.stdout or "").replace(harbor_pass, "***") if harbor_pass else (res.stdout or "")
    payload = {
        "exit_code": res.returncode,
        "stdout_tail": stdout_safe[-500:],
        "stderr_tail": stderr_safe[-500:],
        "image": full_tag,
        "pusher": pusher,
        "seal_findings": [
            {"path": f.path, "line": f.line, "rule": f.rule, "snippet": f.snippet}
            for f in findings
        ],
    }
    log(f"[push_variant] ok={ok} pusher={pusher} tag={full_tag}")
    if ok:
        # node-version-pinning GA.NMF.F8 — report the pushed content digest so
        # the API can record it on a rebuilt node. Empty → API records nothing.
        payload["digest"] = _pushed_digest(full_tag)
    else:
        log(f"[push_variant] push stderr: {stderr_safe[-500:]}")
    report_back(
        api_url, api_key, spec_id=spec_id, stage="push", variant=variant,
        success=ok, payload=payload,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
