"""Pin the build-task's build-spec GET/POST to the /v1/woodsman mount.

Regression this protects (RUN-XEON-NB-PROOF-B-P2, 5082413826): the hero
build died before any FROM line with

    requests.exceptions.HTTPError: 404 Client Error: Not Found for url:
    http://api:80/v1/nodes/builds/build-specs/2

at stages/test_and_repair_one_variant.py:754. That line calls
_common.fetch_build_spec, and the OZ-F7 mount rename (975c4421c6) moved
the whole builds family IN PLACE to /v1/woodsman/* with NO aliases, so
the old /v1/nodes/builds path 404s. Owner rule: the node builder works
on day 1, so the task container must call the live mount.

Auth is unchanged: the scoped build-task token goes in x-api-key (a
bearer on GET build-specs 422s by design).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

import _common as _c  # noqa: E402
import build_one_variant as bo  # noqa: E402
import requests  # noqa: E402

OLD_MOUNT = "/v1/nodes/builds"
NEW_MOUNT = "/v1/woodsman"


class _FakeResp:
    status_code = 200

    def json(self):
        return {"image_name": "nb-proof-b", "tenant_id": 0}

    def raise_for_status(self):
        return None


def _call_with_mocked(method, fn, kwargs):
    """Run fn with requests.<method> mocked; return the URL it requested."""
    seen = {}

    def fake(url, **kw):
        seen["url"] = url
        headers = kw.get("headers") or {}
        assert "X-API-Key" in headers, "the scoped build-task token must be x-api-key"
        return _FakeResp()

    with patch.object(requests, method, side_effect=fake):
        fn(**kwargs)
    return seen["url"]


def test_fetch_build_spec_gets_the_woodsman_build_specs_url():
    """Fails on the old path, passes on the new one."""
    url = _call_with_mocked(
        "get",
        lambda: _c.fetch_build_spec("http://api:80", "scoped-token", 2),
        {},
    )
    assert url == "http://api:80" + NEW_MOUNT + "/build-specs/2", (
        f"expected the /v1/woodsman mount, got {url}"
    )
    assert OLD_MOUNT not in url


def test_report_back_posts_to_the_woodsman_stage_result_url():
    url = _call_with_mocked(
        "post",
        lambda: _c.report_back(
            "http://api:80",
            "scoped-token",
            spec_id=2,
            stage="build",
            variant="hero",
            success=True,
            payload={},
        ),
        {},
    )
    assert url == "http://api:80" + NEW_MOUNT + "/build-specs/2/stage-result", (
        f"stage-result must live under /v1/woodsman, got {url}"
    )
    assert OLD_MOUNT not in url


def test_build_context_stream_uses_the_woodsman_mount():
    src = inspect.getsource(bo._fetch_and_extract_context)
    assert 'f"{api_url}' + NEW_MOUNT + '/build-specs/{spec_id}/context"' in src
    assert OLD_MOUNT not in src, "context stream must not use the old mount"


def test_no_build_task_stage_calls_the_old_mount():
    """Mutant catcher: reverting ANY caller to /v1/nodes/builds goes red."""
    stages = Path(__file__).resolve().parents[1] / "stages"
    offenders = []
    for p in sorted(stages.glob("*.py")):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith(("#", '"', "'")):
                continue
            if OLD_MOUNT in line:
                offenders.append(f"{p.name}:{i}")
    assert offenders == [], f"runtime callers on the renamed-away mount: {offenders}"
