"""Pin the URL the test-and-repair stage uses for /wizard/repair-artifacts.

Stage repro the test protects against: TRELLIS.2 wizard run, task 10806,
hit `404 Not Found for url: https://example.com/wizard/
repair-artifacts`. The endpoint is mounted under /v1/woodsman (per
main.py, single /v1 mount), and the stage was hitting the bare path — so
the loop 404'd on every iteration and never converted to a real repair
attempt.

The integration tests in tests/test_repair_endpoint_integration.py hit
the endpoint at the correct URL directly, so they couldn't catch this
gap — the stage's URL construction is its own concern.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "stages"),
)

import test_and_repair_one_variant as tar  # noqa: E402


def test_repair_endpoint_url_includes_node_definitions_prefix():
    src = inspect.getsource(tar._call_repair_endpoint)
    assert "/v1/woodsman/wizard/repair-artifacts" in src, \
        "URL must include the /v1/woodsman router prefix"


def test_repair_endpoint_url_is_not_the_bare_path():
    """Defensive: a future refactor that "simplifies" the URL by dropping
    the prefix would 404 in production. Pin against that specific
    regression — the bare path must not appear as the requests.post
    target."""
    src = inspect.getsource(tar._call_repair_endpoint)
    # Find the requests.post call and inspect its URL arg.
    assert 'f"{api_url}/v1/woodsman/wizard/repair-artifacts"' in src
    # The literal bare-path URL must NOT appear inside requests.post(.
    # Doc strings can mention it freely (the file comment does).
    post_block_start = src.find("requests.post(")
    if post_block_start != -1:
        post_block_end = src.find(")", post_block_start)
        post_block = src[post_block_start:post_block_end]
        assert 'f"{api_url}/wizard/repair-artifacts"' not in post_block, \
            "regression: requests.post is hitting the prefix-less path again"
        assert 'f"{api_url}/node_definitions/wizard/repair-artifacts"' not in post_block, \
            "regression: requests.post is hitting the pre-/v1 unprefixed path again"
