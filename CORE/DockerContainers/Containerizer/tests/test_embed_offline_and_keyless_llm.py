"""Containerizer: the retrieval index degrades quietly offline, and a keyless
self-hosted LLM endpoint is accepted.

The generate container's egress allow-list reaches the LLM host only, so the
Hugging Face Hub is unreachable at run time. ``embed_index._get_model`` prefers
a copy baked into the image, never downloads when the container is declared
offline, and turns any load failure into ``IndexUnavailable`` (remembered, so
later stages do not retry) - ``codebase_scan`` catches that and carries on
without RAG.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent / "scripts" / "tools"


def _load(name):
    """Load one tools module by path. The ``tools`` package __init__ pulls in
    heavy deps (gitingest, tree-sitter, ...) these tests do not need, so a bare
    package stub stands in for it just long enough to import the module."""
    stub = None
    if "tools" not in sys.modules:
        stub = types.ModuleType("tools")
        stub.__path__ = [str(_TOOLS)]
        sys.modules["tools"] = stub
    try:
        spec = importlib.util.spec_from_file_location(f"_ce_{name}", _TOOLS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if stub is not None:
            sys.modules.pop("tools", None)
            for k in [k for k in sys.modules if k.startswith("tools.")]:
                sys.modules.pop(k, None)


embed_index = _load("embed_index")
llm_providers = _load("llm_providers")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(embed_index, "_MODEL", None)
    monkeypatch.setattr(embed_index, "_MODEL_ERROR", None)
    monkeypatch.setattr(embed_index, "_ST_OK", True)
    monkeypatch.setattr(embed_index, "EMBED_BAKED_DIR", str(tmp_path / "baked"))
    for k in ("CODEBASE_EMBED_OFFLINE", "HF_HUB_OFFLINE"):
        monkeypatch.delenv(k, raising=False)


class _FakeST:
    loads = []

    def __init__(self, name, device="cpu"):
        _FakeST.loads.append(name)


@pytest.fixture
def fake_st(monkeypatch):
    _FakeST.loads = []
    monkeypatch.setattr(embed_index, "SentenceTransformer", _FakeST, raising=False)
    return _FakeST


def _bake(tmp_path):
    d = tmp_path / "baked" / embed_index.EMBED_MODEL_NAME.split("/")[-1]
    d.mkdir(parents=True)
    return str(d)


def test_baked_model_is_loaded_from_disk_not_the_hub(fake_st, tmp_path, monkeypatch):
    baked = _bake(tmp_path)
    monkeypatch.setenv("CODEBASE_EMBED_OFFLINE", "1")
    embed_index._get_model()
    assert fake_st.loads == [baked]


def test_offline_without_baked_model_raises_index_unavailable_without_download(fake_st, monkeypatch):
    monkeypatch.setenv("CODEBASE_EMBED_OFFLINE", "1")
    with pytest.raises(embed_index.IndexUnavailable):
        embed_index._get_model()
    assert fake_st.loads == []  # never tried the network


def test_hf_hub_offline_counts_as_offline(fake_st, monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    with pytest.raises(embed_index.IndexUnavailable):
        embed_index._get_model()
    assert fake_st.loads == []


def test_online_without_baked_model_still_fetches_by_name(fake_st):
    embed_index._get_model()
    assert fake_st.loads == [embed_index.EMBED_MODEL_NAME]


def test_load_failure_becomes_index_unavailable_and_is_remembered(monkeypatch):
    attempts = []

    class Boom:
        def __init__(self, *a, **k):
            attempts.append(1)
            raise OSError("no route to huggingface.co")

    monkeypatch.setattr(embed_index, "SentenceTransformer", Boom, raising=False)
    for _ in range(3):
        with pytest.raises(embed_index.IndexUnavailable):
            embed_index._get_model()
    assert len(attempts) == 1  # later stages fail fast, no repeated download attempts


def test_search_and_index_surface_index_unavailable_when_offline(monkeypatch):
    monkeypatch.setenv("CODEBASE_EMBED_OFFLINE", "1")
    monkeypatch.setattr(embed_index, "_get_client", lambda: object())
    with pytest.raises(embed_index.IndexUnavailable):
        embed_index.search("codebase:t", "q")


# ---------------------------------------------------------------- keyless LLM

def _cfg(**kw):
    base = dict(provider="openai_compatible", model="m", api_key="", base_url="http://llm:8000")
    base.update(kw)
    return llm_providers.LLMConfig(**base)


def test_openai_compatible_without_key_is_valid_with_a_base_url():
    _cfg().validate()  # does not raise (vLLM / llama.cpp / LM Studio / a MOD stack)


def test_openai_compatible_still_requires_a_base_url():
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        _cfg(base_url=None).validate()


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "groq", "mistral", "deepseek"])
def test_hosted_providers_still_require_a_key(provider):
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        _cfg(provider=provider, base_url=None).validate()
