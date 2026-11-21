"""push_one_variant: local-publish mode (Community, no registry) vs the paid
Harbor path.

CE has no Vault / Harbor, so a build's registry host is empty. The API stamps
``MOD_PUBLISH_MODE=local`` on the push stage only for that posture; the stage
then becomes a no-op that reports the local image Id as the digest. Everything
else - in particular an empty ``HARBOR_URL`` WITHOUT the stamp (a failed
registry lookup on a paid install) - behaves exactly as before:
``creds_missing``, or a real ``docker login`` + ``docker push``.

The worker side of the pin (Worker/internal/executor/executor.go
``imageDigestForInspect``) prefers RepoDigests and falls back to the image Id;
a never-pushed local image has no RepoDigests, so the Id this stage reports is
the value the worker compares against.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import push_one_variant as pov  # noqa: E402

IMAGE_ID = "sha256:" + "a" * 64
REPO_DIGEST = "sha256:" + "b" * 64
SPEC = {"image_name": "my-node", "tenant_id": 7, "variants": [], "hero_dockerfile": "FROM x\n"}


class _Daemon:
    """Fake docker/skopeo CLI. ``present``: tags the daemon holds."""

    def __init__(self, present=("my-node:latest",), repo_digest=None, push_rc=0):
        self.present = set(present)
        self.repo_digest = repo_digest
        self.push_rc = push_rc
        self.calls = []

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        bad = lambda err="": SimpleNamespace(returncode=1, stdout="", stderr=err)  # noqa: E731
        if cmd[:3] == ["docker", "image", "inspect"]:
            tag = cmd[-1]
            if tag not in self.present:
                return bad("No such image")
            return ok(IMAGE_ID if cmd[4] == "{{.Id}}" else f"{IMAGE_ID} {tag}")
        if cmd[:2] == ["docker", "inspect"]:
            tag = cmd[-1]
            if "RepoDigests" in cmd[3]:
                return ok(f"{tag}@{self.repo_digest}") if self.repo_digest else bad("index out of range")
            return ok(IMAGE_ID) if tag in self.present else bad()
        if cmd[:2] == ["docker", "images"]:
            return ok("\n".join(self.present))
        if cmd[:2] == ["docker", "tag"]:
            self.present.add(cmd[3])
            return ok()
        if cmd[:2] == ["docker", "push"]:
            return SimpleNamespace(returncode=self.push_rc, stdout="", stderr="push failed" if self.push_rc else "")
        return ok()

    def verbs(self):
        return [" ".join(c[:2]) for c in self.calls]


@pytest.fixture
def stage(monkeypatch):
    reports = []
    logs = []
    for k, v in {"API_URL": "http://api", "API_KEY": "tok", "BUILD_SPEC_ID": "5",
                 "VARIANT": "hero"}.items():
        monkeypatch.setenv(k, v)
    for k in ("HARBOR_URL", "HARBOR_USERNAME", "HARBOR_PASSWORD", "MOD_PUBLISH_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(pov, "fetch_build_spec", lambda *a, **k: dict(SPEC))
    monkeypatch.setattr(pov, "report_back", lambda *a, **k: reports.append(k))
    monkeypatch.setattr(pov, "log", lambda m: logs.append(m))
    monkeypatch.setattr(pov.time, "sleep", lambda s: None)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: None)

    def install(daemon):
        monkeypatch.setattr(pov.subprocess, "run", daemon.run)
        return daemon

    return SimpleNamespace(reports=reports, logs=logs, install=install, mp=monkeypatch)


# ----------------------------------------------------------------- local mode

def test_local_mode_is_a_noop_that_reports_the_local_image_id(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    d = stage.install(_Daemon())
    assert pov.main() == 0
    assert len(stage.reports) == 1
    rep = stage.reports[0]
    assert rep["success"] is True and rep["stage"] == "push"
    assert rep["payload"]["digest"] == IMAGE_ID
    assert rep["payload"]["pusher"] == "local-noop"
    assert rep["payload"]["image"] == "my-node:latest"  # bare ref, no registry/tenant prefix
    assert any("no registry configured" in m for m in stage.logs)
    assert not any(v in ("docker push", "docker login") for v in d.verbs())
    assert not any(c and c[0] == "skopeo" for c in d.calls)


def test_local_mode_with_registry_host_unset_never_defaults_to_platform_harbor(stage):
    """HARBOR_URL absent entirely (not just empty) must not fall through to the
    stage's registry.example.com default."""
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    d = stage.install(_Daemon())
    assert pov.main() == 0
    assert stage.reports[0]["payload"]["image"] == "my-node:latest"
    assert not any("ghcr.io" in " ".join(c) for c in d.calls)


def test_local_mode_variant_tag_and_byte_identical_variant_skip_registry_copy(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    stage.mp.setenv("VARIANT", "cuda")
    spec = dict(SPEC, variants=[{"variant": "cuda", "dockerfile": "FROM x\n"}])
    stage.mp.setattr(pov, "fetch_build_spec", lambda *a, **k: dict(spec))
    d = stage.install(_Daemon(present=("my-node:latest-cuda",)))
    assert pov.main() == 0
    assert stage.reports[0]["payload"]["image"] == "my-node:latest-cuda"
    assert not any(c and c[0] == "skopeo" for c in d.calls)


def test_local_mode_missing_image_fails_loudly(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    stage.install(_Daemon(present=()))
    assert pov.main() == 1
    rep = stage.reports[-1]
    assert rep["success"] is False
    assert rep["payload"]["stage_failed"] == "local_image_missing"


def test_local_mode_recovers_a_dropped_tag_from_a_sibling(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    d = stage.install(_Daemon(present=("my-node:latest-cuda",)))
    assert pov.main() == 0
    assert ["docker", "tag", "my-node:latest-cuda", "my-node:latest"] in d.calls
    assert stage.reports[-1]["payload"]["digest"] == IMAGE_ID


def test_local_mode_digest_is_the_id_the_worker_falls_back_to(stage):
    """RepoDigests absent on a never-pushed image: the reported pin is the
    image Id (sha256:<64 hex>), exactly the form imageDigestForInspect returns
    when RepoDigests is empty."""
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    stage.install(_Daemon(repo_digest=None))
    pov.main()
    digest = stage.reports[0]["payload"]["digest"]
    assert digest.startswith("sha256:") and len(digest) == 71 and digest == digest.lower()


def test_local_mode_still_runs_the_seal_scan_enforcement(stage):
    """Local publish must not become a way around MOD_SEALED_LINT=enforce."""
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "")
    stage.mp.setenv("MOD_SEALED_LINT", "enforce")
    stage.mp.setattr(pov, "seal_scan", lambda *a, **k: [
        SimpleNamespace(path="Dockerfile", line=1, rule="C23", snippet="x")])
    stage.install(_Daemon())
    assert pov.main() == 1
    assert stage.reports[-1]["success"] is False
    assert "seal_findings" in stage.reports[-1]["payload"]


# ------------------------------------------------------------- paid / Harbor

def test_empty_harbor_without_local_stamp_is_still_creds_missing(stage):
    """Paid install whose registry lookup failed: NOT a silent local publish."""
    stage.mp.setenv("HARBOR_URL", "")
    d = stage.install(_Daemon())
    assert pov.main() == 1
    rep = stage.reports[-1]
    assert rep["success"] is False
    assert rep["payload"]["stage_failed"] == "creds_missing"
    assert "docker push" not in d.verbs()


def test_harbor_with_creds_logs_in_and_pushes_as_before(stage):
    stage.mp.setenv("HARBOR_URL", "harbor.example")
    stage.mp.setenv("HARBOR_USERNAME", "robot")
    stage.mp.setenv("HARBOR_PASSWORD", "s3cret")
    tag = "harbor.example/tenant-7/my-node:latest"
    d = stage.install(_Daemon(present=(tag,), repo_digest=REPO_DIGEST))
    assert pov.main() == 0
    assert ["docker", "push", tag] in d.calls
    assert any(c[:3] == ["docker", "login", "harbor.example"] for c in d.calls)
    rep = stage.reports[-1]
    assert rep["success"] is True
    assert rep["payload"]["pusher"] == "docker-push"
    assert rep["payload"]["digest"] == REPO_DIGEST  # registry digest still preferred
    assert "s3cret" not in repr(rep)


def test_local_stamp_with_a_registry_host_still_pushes(stage):
    """The stamp only counts when there is no registry host."""
    stage.mp.setenv("MOD_PUBLISH_MODE", "local")
    stage.mp.setenv("HARBOR_URL", "harbor.example")
    stage.mp.setenv("HARBOR_USERNAME", "robot")
    stage.mp.setenv("HARBOR_PASSWORD", "pw")
    tag = "harbor.example/tenant-7/my-node:latest"
    d = stage.install(_Daemon(present=(tag,), repo_digest=REPO_DIGEST))
    assert pov.main() == 0
    assert ["docker", "push", tag] in d.calls
    assert stage.reports[-1]["payload"]["pusher"] == "docker-push"


def test_harbor_push_failure_is_still_a_failure(stage):
    stage.mp.setenv("HARBOR_URL", "harbor.example")
    stage.mp.setenv("HARBOR_USERNAME", "robot")
    stage.mp.setenv("HARBOR_PASSWORD", "pw")
    tag = "harbor.example/tenant-7/my-node:latest"
    stage.install(_Daemon(present=(tag,), push_rc=1))
    assert pov.main() == 1
    assert stage.reports[-1]["success"] is False


# ------------------------------------------------- optional registry (CE)

def test_anonymous_registry_pushes_without_login(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "registry_anonymous")
    stage.mp.setenv("HARBOR_URL", "localhost:5000")
    tag = "localhost:5000/tenant-7/my-node:latest"
    d = stage.install(_Daemon(present=(tag,), repo_digest=REPO_DIGEST))
    assert pov.main() == 0
    assert ["docker", "push", tag] in d.calls
    assert "docker login" not in d.verbs()
    assert not any(c and c[0] == "skopeo" for c in d.calls)
    assert stage.reports[-1]["payload"]["digest"] == REPO_DIGEST


def test_anonymous_stamp_without_registry_host_is_not_anonymous(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "registry_anonymous")
    stage.mp.setenv("HARBOR_URL", "")
    stage.install(_Daemon())
    assert pov.main() == 1
    assert stage.reports[-1]["payload"]["stage_failed"] == "creds_missing"


def test_anonymous_stamp_with_creds_logs_in_normally(stage):
    stage.mp.setenv("MOD_PUBLISH_MODE", "registry_anonymous")
    stage.mp.setenv("HARBOR_URL", "reg.lan:5000")
    stage.mp.setenv("HARBOR_USERNAME", "ci")
    stage.mp.setenv("HARBOR_PASSWORD", "pw")
    tag = "reg.lan:5000/tenant-7/my-node:latest"
    d = stage.install(_Daemon(present=(tag,), repo_digest=REPO_DIGEST))
    assert pov.main() == 0
    assert any(c[:3] == ["docker", "login", "reg.lan:5000"] for c in d.calls)


# ------------------------------------------------------ helper predicates

@pytest.mark.parametrize("mode,url,expected", [
    ("local", "", True),
    ("LOCAL", "  ", True),
    ("local", "harbor.example", False),
    ("", "", False),
    ("registry_anonymous", "", False),
])
def test_local_publish_mode_predicate(monkeypatch, mode, url, expected):
    from _common import local_publish_mode
    monkeypatch.setenv("MOD_PUBLISH_MODE", mode)
    assert local_publish_mode(url) is expected


@pytest.mark.parametrize("mode,url,user,pw,expected", [
    ("registry_anonymous", "localhost:5000", "", "", True),
    ("registry_anonymous", "localhost:5000", "u", "p", False),
    ("registry_anonymous", "", "", "", False),
    ("", "localhost:5000", "", "", False),
    ("local", "localhost:5000", "", "", False),
])
def test_anonymous_registry_push_predicate(monkeypatch, mode, url, user, pw, expected):
    from _common import anonymous_registry_push
    monkeypatch.setenv("MOD_PUBLISH_MODE", mode)
    assert anonymous_registry_push(url, user, pw) is expected
