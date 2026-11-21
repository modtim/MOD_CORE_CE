"""Tests for the Accept-header fix on harbor_tag_verify.httpx_fetch_factory.

Bug from stage repro: the Containerizer's `verify` stage reported
404 on every base-image manifest fetch — base-python:3.11,
base-intel-xpu:2025.0, base-npu:openvino-2025 — even though
GET /v2/<repo>/tags/list returned 200 with each tag in the list.

Root cause: Docker Registry V2 / OCI Distribution requires the
client to send an `Accept` header on /v2/<repo>/manifests/<ref>
GETs listing the manifest schemas it understands. Harbor (Docker
Distribution v2.8+) returns 404 when the Accept header is missing
because content negotiation can't pick a supported schema.

`docker pull` itself sends the four-MIME-type Accept set; we now
match it. Without this fix the wizard ships Dockerfiles whose
base images all "fail" the verify step even when they exist —
poisoning the downstream regenerate-with-corrections prompt and
shipping artifacts that look broken even when they aren't.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "scripts"),
)

# Skip if httpx isn't available — the factory imports it lazily and
# unit tests in environments without httpx (e.g. pure offline
# linting) should still pass the rest of the suite.
httpx = pytest.importorskip("httpx")

from tools.harbor_tag_verify import httpx_fetch_factory  # noqa: E402


class _StubResp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


def _build_fetch(captured):
    """Build a fetch callable that records every (url, headers) it
    was called with into `captured` and returns a 200 stub."""
    factory = httpx_fetch_factory(timeout_s=1.0)
    # Monkey-patch the underlying httpx.Client.get the factory holds
    # so we capture what the production code WOULD send to Harbor
    # without actually opening a network connection.
    inner = factory.__closure__[0].cell_contents  # the captured client
    def _stub_get(url, headers=None, **_kw):
        captured.append((url, dict(headers or {})))
        return _StubResp(200)
    inner.get = _stub_get
    return factory


def test_manifest_fetch_sends_accept_header():
    """The exact bug: /manifests/<ref> calls MUST include the
    docker+oci manifest media types in Accept."""
    captured = []
    fetch = _build_fetch(captured)

    url = "https://registry.example.com/v2/modtim/base-python/manifests/3.11"
    fetch(url, {"Authorization": "Basic Zm9vOmJhcg=="})

    assert len(captured) == 1
    sent_url, sent_headers = captured[0]
    assert sent_url == url
    accept = sent_headers.get("Accept", "")
    # All four media types `docker pull` sends should be present.
    assert "application/vnd.docker.distribution.manifest.v2+json" in accept
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in accept
    assert "application/vnd.oci.image.manifest.v1+json" in accept
    assert "application/vnd.oci.image.index.v1+json" in accept


def test_manifest_fetch_preserves_auth_header():
    """Adding Accept must not drop the Authorization header the
    caller passed in (the verify code injects Basic auth)."""
    captured = []
    fetch = _build_fetch(captured)

    fetch(
        "https://registry.example.com/v2/some/repo/manifests/v1",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    _, headers = captured[0]
    assert headers.get("Authorization") == "Basic dGVzdDp0ZXN0"


def test_tags_list_fetch_doesNOT_need_accept_to_be_added():
    """Tags-list is a plain JSON endpoint and doesn't go through
    content negotiation. We don't need to mangle its headers.
    Spec'd behavior: the factory only adds Accept when the URL
    path contains `/manifests/`."""
    captured = []
    fetch = _build_fetch(captured)

    fetch(
        "https://registry.example.com/v2/some/repo/tags/list",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    _, headers = captured[0]
    # No Accept injected for non-manifest URLs.
    assert "Accept" not in headers


def test_caller_supplied_accept_is_not_clobbered():
    """If a test or override passes a custom Accept, the factory
    leaves it alone — keeps the override path testable."""
    captured = []
    fetch = _build_fetch(captured)

    fetch(
        "https://registry.example.com/v2/x/manifests/y",
        {"Accept": "application/json", "Authorization": "Basic abc"},
    )
    _, headers = captured[0]
    assert headers["Accept"] == "application/json"
