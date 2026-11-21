"""Text LLM node — unbound-model preflight (FIX-NODE-TEXT-LLM-UNBOUND-MESSAGE).

The CE compose ships no ollama / vllm-big service, so a fresh CE with no bound
model used to die deep in main() with a raw urllib connection error. The entrypoint
now fails up front with one sentence naming the binding step, ONLY when the
orchestrator injected nothing at all (no LLM_BASE_URL, no LLM_MODEL/MODEL, and
both role endpoints still the built-in defaults).

Run: pytest <path>/CORE/DockerContainers/TextLLM/tests/test_unbound_model_preflight.py

The entrypoint is a script, not a package, so each case runs it as a subprocess
with a fully controlled env (the real contract: exit code + the text the user
sees). No network is needed: every case either fails before any request, or
targets a host that does not resolve in the test container.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ENTRYPOINT = "TextLLM/entrypoint.py"


def _entrypoint_path() -> pathlib.Path:
    d = pathlib.Path(__file__).resolve().parent
    while d != d.parent:
        cand = d / ENTRYPOINT
        if cand.exists():
            return cand
        d = d.parent
    raise AssertionError("TextLLM/entrypoint.py not found from the test path")


def _case_env(**over):
    env = {
        "PROMPT": "say hello",
        "ROLE": "generator",
        "TEMPERATURE": "0.3",
        "MAX_TOKENS": "8",
        "LLM_TIMEOUT": "5",
    }
    env.update({k: v for k, v in over.items() if v is not None})
    return env


def _spawn(env: dict[str, str]) -> tuple[int, str, str]:
    p = subprocess.run(
        [sys.executable, str(_entrypoint_path())],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    return p.returncode, p.stdout, p.stderr


def test_unbound_exits_nonzero_with_binding_message():
    """Nothing injected -> non-zero, one sentence naming the real UI path."""
    rc, out, err = _spawn(_case_env())
    blob = out + err
    assert rc != 0, f"unbound case must exit non-zero, got rc={rc}"
    assert "NO_MODEL_BOUND" in out, f"fail-closed JSON line missing: {blob[:400]}"
    assert "no model bound" in blob, f"message missing: {blob[:400]}"
    assert "Settings > AI Configuration" in blob, "must name the real UI path"
    assert "bind a model" in blob, "must name the user action"
    assert "PROCESSING_FAILURE" not in out, "preflight has its own code"


def test_llm_base_url_set_proceeds_past_preflight():
    """User/operator set LLM_BASE_URL -> preflight skipped, request stage reached."""
    rc, out, err = _spawn(_case_env(LLM_BASE_URL="http://mcw-no-such-host:8000"))
    blob = out + err
    assert "NO_MODEL_BOUND" not in out, "a set URL must not hit the preflight"
    assert rc != 0, "no network in the test: it must fail at the request"
    # Existing behaviour kept: no model named -> discovery GET fails -> the
    # pre-existing PROCESSING_FAILURE line (NOT the new NO_MODEL_BOUND one).
    assert ("PROCESSING_FAILURE" in out or "no MODEL set" in blob), \
        f"existing path expected: {blob[:400]}"


def test_only_llm_model_set_keeps_existing_behaviour():
    """Only LLM_MODEL set -> preflight does NOT fire (a model IS named).

    Existing behaviour kept: the node still tries the role default alias
    (http://ollama:11434 on CE) and fails on the raw connection error.
    """
    rc, out, err = _spawn(_case_env(LLM_MODEL="mcw-test-model"))
    blob = out + err
    assert "NO_MODEL_BOUND" not in out, "a named model must not hit the preflight"
    assert rc != 0, "no network: the role-default alias is unreachable here"
    assert ("ollama" in blob or "cannot reach" in blob
            or "unreachable" in blob), f"existing path expected: {blob[:400]}"


def test_bound_pair_behaviour_unchanged():
    """LLM_BASE_URL + LLM_MODEL (the bound-model spread) -> unchanged to the POST."""
    rc, out, err = _spawn(_case_env(LLM_BASE_URL="http://mcw-no-such-host:8000",
                                    LLM_MODEL="mcw-test-model"))
    blob = out + err
    assert "NO_MODEL_BOUND" not in out
    assert "POST /v1/chat/completions" in blob, "reached the request stage as before"
    assert rc != 0, "no network in the test"
