"""FIX-BUILD-LOGIN-PLACEHOLDER — the build stage must not `docker login` a placeholder.

A CE install seeds the platform Harbor Secrets with the auto-seeded Vault
placeholder (`PLEASE_CONFIGURE`). Even once the API stops stamping it
(FIX-BUILD-LOGIN-PLACEHOLDER on submit.py), the stage guard here must treat the
same value as "no registry": `docker login PLEASE_CONFIGURE` hits
`https://PLEASE_CONFIGURE/v2/` (`server misbehaving`) and buries the real base-image
error underneath it. Symmetric with the API-side guard, and it also covers the
symmetric push-side skip NB-LOCALPUB pins (an empty registry is a posture, not a
failure - the build must still run).

Everything is hermetic: the docker CLI never runs, the API is never called.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

from _common import registry_is_unset  # noqa: E402
import build_one_variant as biv  # noqa: E402

API_URL = "http://api:80"
SPEC_ID = 1


class _StopAfterLogin(Exception):
    """Raised where main() would start the build - the login window is all
    this test cares about, and nothing below it touches the registry."""


def _stop_after_login(monkeypatch):
    """Run main() up to (and past) the login window, then bail out."""
    monkeypatch.setenv("API_URL", API_URL)
    monkeypatch.setenv("API_KEY", "not-a-real-key")
    monkeypatch.setenv("BUILD_SPEC_ID", str(SPEC_ID))
    monkeypatch.setenv("VARIANT", "hero")
    monkeypatch.setattr(biv, "check_docker_disk_space", lambda min_gb: None)
    monkeypatch.setattr(biv, "fetch_build_spec", lambda *a, **k: _raise())

    def _raise():
        raise _StopAfterLogin()

    try:
        biv.main()
    except _StopAfterLogin:
        return
    raise AssertionError("main() never reached the build step - the login window is untested")


def _login_calls(monkeypatch):
    """Record every docker_login attempt (argv only, no secrets)."""
    calls = []
    monkeypatch.setattr(biv, "docker_login", lambda url, user, pw: calls.append((url, user)))
    return calls


def _lines(monkeypatch):
    lines = []
    monkeypatch.setattr(biv, "log", lambda msg, *a, **k: lines.append(msg))
    return lines


# ---------------------------------------------------------------------------
# registry_is_unset - the shared "no registry" test
# ---------------------------------------------------------------------------

def test_registry_is_unset_covers_placeholder_and_blank():
    for value in (None, "", "   ", "PLEASE_CONFIGURE", " please_configure "):
        assert registry_is_unset(value), repr(value)
    for value in ("ghcr.io", "ghcr.io/modtim", "localhost:5000",
                  "PLEASE_CONFIGURED"):
        assert not registry_is_unset(value), repr(value)


# ---------------------------------------------------------------------------
# the build stage's login window
# ---------------------------------------------------------------------------

def test_placeholder_never_reaches_docker_login(monkeypatch):
    monkeypatch.setenv("PLATFORM_HARBOR_URL", "PLEASE_CONFIGURE")
    monkeypatch.setenv("PLATFORM_HARBOR_USERNAME", "PLEASE_CONFIGURE")
    monkeypatch.setenv("PLATFORM_HARBOR_PASSWORD", "PLEASE_CONFIGURE")
    calls = _login_calls(monkeypatch)
    lines = _lines(monkeypatch)
    _stop_after_login(monkeypatch)
    assert calls == [], "a garbage login was attempted"
    assert any("skipping the base-image login" in ln for ln in lines), lines


def test_empty_registry_skips_login_and_still_builds(monkeypatch):
    """CE posture (NB-LOCALPUB's symmetric case): no URL at all - the skip is
    a named no-op, NOT a reported failure, and the build continues."""
    for k in ("PLATFORM_HARBOR_URL", "PLATFORM_HARBOR_USERNAME",
              "PLATFORM_HARBOR_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    calls = _login_calls(monkeypatch)
    lines = _lines(monkeypatch)
    reported = []
    monkeypatch.setattr(biv, "report_back", lambda *a, **k: reported.append(k))
    _stop_after_login(monkeypatch)
    assert calls == []
    assert reported == [], "a skipped login must not report a preflight failure"
    assert any("skipping the base-image login" in ln for ln in lines), lines


def test_real_host_logs_in_exactly_as_before(monkeypatch):
    monkeypatch.setenv("PLATFORM_HARBOR_URL", "registry.example.com")
    monkeypatch.setenv("PLATFORM_HARBOR_USERNAME", "plat-user")
    monkeypatch.setenv("PLATFORM_HARBOR_PASSWORD", "plat-pass")
    calls = _login_calls(monkeypatch)
    lines = _lines(monkeypatch)
    _stop_after_login(monkeypatch)
    assert calls == [("registry.example.com", "plat-user")], calls
    assert any("logged in to platform Harbor registry.example.com" in ln for ln in lines), lines


def test_real_host_with_placeholder_creds_skips(monkeypatch):
    """A real host whose creds row is still the placeholder is not a login."""
    monkeypatch.setenv("PLATFORM_HARBOR_URL", "registry.example.com")
    monkeypatch.setenv("PLATFORM_HARBOR_USERNAME", "PLEASE_CONFIGURE")
    monkeypatch.setenv("PLATFORM_HARBOR_PASSWORD", "PLEASE_CONFIGURE")
    calls = _login_calls(monkeypatch)
    lines = _lines(monkeypatch)
    _stop_after_login(monkeypatch)
    assert calls == [], calls
    assert any("skipping the base-image login" in ln for ln in lines), lines
