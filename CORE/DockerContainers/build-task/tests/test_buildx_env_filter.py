"""GSEC FX.16 R-023 — buildx/skopeo subprocess env filter.

The stage container holds API_KEY + registry creds (claim-time
secret_refs); the docker CLI child must NOT inherit them, because the
build runs an untrusted, LLM-generated Dockerfile. Registry auth is
established by a separate `docker login --password-stdin`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

from _common import filtered_build_env  # noqa: E402


def test_credential_envs_are_dropped(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret-token")
    monkeypatch.setenv("HARBOR_USERNAME", "robot-user")
    monkeypatch.setenv("HARBOR_PASSWORD", "robot-pass")
    monkeypatch.setenv("HARBOR_URL", "registry.example.com")
    monkeypatch.setenv("PLATFORM_HARBOR_USERNAME", "plat-user")
    monkeypatch.setenv("PLATFORM_HARBOR_PASSWORD", "plat-pass")
    monkeypatch.setenv("PLATFORM_HARBOR_URL", "registry.example.com")
    monkeypatch.setenv("MINIO_SESSION_TOKEN", "sts-tok")
    monkeypatch.setenv("SOME_API_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("VAULT_APPROLE_SECRET_ID", "sid")

    env = filtered_build_env({"DOCKER_BUILDKIT": "1"})

    for k in (
        "API_KEY",
        "HARBOR_USERNAME", "HARBOR_PASSWORD", "HARBOR_URL",
        "PLATFORM_HARBOR_USERNAME", "PLATFORM_HARBOR_PASSWORD",
        "PLATFORM_HARBOR_URL",
        "MINIO_SESSION_TOKEN", "SOME_API_TOKEN",
        "POSTGRES_PASSWORD", "VAULT_APPROLE_SECRET_ID",
    ):
        assert k not in env, f"{k} leaked into the build subprocess env"
    assert env["DOCKER_BUILDKIT"] == "1"


def test_non_secret_envs_pass_through(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/mod")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("DOCKER_CONFIG", "/home/mod/.docker")
    monkeypatch.setenv("API_KEY", "secret-token")

    env = filtered_build_env()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/mod"
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert env["DOCKER_CONFIG"] == "/home/mod/.docker"
    assert "API_KEY" not in env


def test_extra_overrides_applied(monkeypatch):
    monkeypatch.setenv("BUILD_INACTIVITY_TIMEOUT_S", "300")
    env = filtered_build_env({"BUILD_INACTIVITY_TIMEOUT_S": "120"})
    assert env["BUILD_INACTIVITY_TIMEOUT_S"] == "120"


def test_caller_env_unmutated(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret-token")
    monkeypatch.setenv("HARBOR_PASSWORD", "robot-pass")
    import os
    filtered_build_env()
    # The filter copies os.environ; the stage process must keep its own.
    assert os.environ["API_KEY"] == "secret-token"
    assert os.environ["HARBOR_PASSWORD"] == "robot-pass"
