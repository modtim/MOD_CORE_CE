#!/usr/bin/env python3
"""Text LLM node — the prompt-composer / general text step for MOD workflows.

Takes a text PROMPT (+ optional CONTEXT — e.g. the `context.txt` a RAG Query
node emits) and calls your configured text LLM (an OpenAI-compatible endpoint),
writing the generated text to /data/output. Its headline use is composing an
SDXL prompt from a request plus retrieved character/brand context, but it is a
general text step: summarise, rewrite, classify, extract, converse.

HOW THIS NODE REACHES THE CONFIGURED LLM
-------------------------------------
It calls the SAME served endpoint the wizard/critic use, resolved the SAME way
`routers/ai_config.py` resolves it — by ROLE to the compose alias the managed-AI
stack publishes on the platform network:

    generator -> http://ollama:11434     (ai_config.py _LOCAL_BASE / minimal tier)
    reasoner  -> http://vllm-big:8000     (ai_config.py LLM_BIG_BASE_URL)

No credential is injected and none is needed: the configured LLM is served on the
platform network with no auth (its API key is the `sk-vllm-no-auth` placeholder
`_resolved_effective_env` also emits when no real key is configured). Every
GENERAL processing node is already granted platform-network reachability by the
orchestrator (it sets MOD_NEEDS_API_TOKEN, which the worker turns into
MOD_TASK_TOKEN_URL, which netpol reads to place the container on the main
network) — so the alias resolves here exactly as it does for the API. That is
why this is a normal tenant-shared node, NOT a tenant_id=NULL credential-injected
one like the vector/RAG nodes: those need a minted vector token to reach
`/v1/cyclone/vector/*`; this needs nothing but the network it already gets.

The endpoint is NOT hardcoded into a workflow: it is resolved at runtime and can
be overridden by env (LLM_BASE_URL, or LLM_GENERATOR_URL / LLM_REASONER_URL) so a
future orchestrator injection or an on-prem deployment pointing elsewhere wins
with no image change. LLM_MODEL / MODEL win when set; otherwise the served model
is discovered from the endpoint's /v1/models (llama.cpp, vLLM and Ollama all
expose it), so the node works out of the box without the operator naming a model.

CONFIG (extra_variables — the node-config panel uppercases keys into env)
------------------------------------------------------------------------
  ROLE           generator | reasoner  (default "generator"). Picks the endpoint.
  MODEL          explicit served model id (optional; else auto-discovered).
  SYSTEM_PROMPT  optional system message (the composer instruction).
  TEMPERATURE    sampling temperature (default 0.3).
  MAX_TOKENS     max completion tokens (default 1024).
  PROMPT         the request text (optional here; usually staged as input).

INPUT (first found wins)
------------------------
  PROMPT :  env PROMPT, else /data/input/prompt.txt, else the first .txt/.md in
            /data/input that is not the context file.
  CONTEXT:  env CONTEXT, else /data/input/context.txt (the RAG Query output name).

OUTPUT
------
  /data/output/output.txt    the generated text (ready to feed SDXL / a next node)
  /data/output/response.json  {prompt, context_used, model, endpoint, role,
                               temperature, max_tokens, finish_reason, usage, text}
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

IN = pathlib.Path("/data/input")
OUT = pathlib.Path("/data/output")
OUT.mkdir(parents=True, exist_ok=True)

# Role -> served endpoint alias. Mirrors routers/ai_config.py so this node hits
# the same configured LLM the wizard does. Each is env-overridable for portability.
_ROLE_ENDPOINTS = {
    "generator": os.getenv("LLM_GENERATOR_URL", "http://ollama:11434"),
    "reasoner": os.getenv("LLM_REASONER_URL", "http://vllm-big:8000"),
}

ROLE = (os.getenv("ROLE") or "generator").strip().lower()
if ROLE not in _ROLE_ENDPOINTS:
    ROLE = "generator"

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or ""
try:
    TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3") or "0.3")
except ValueError:
    TEMPERATURE = 0.3
try:
    MAX_TOKENS = int(float(os.getenv("MAX_TOKENS", "1024") or "1024"))
except ValueError:
    MAX_TOKENS = 1024

# An explicit base URL (operator / future platform injection) always wins over
# the role map. LLM_BASE_URL is the exact var `_resolved_effective_env` emits.
BASE_URL = (os.getenv("LLM_BASE_URL") or _ROLE_ENDPOINTS[ROLE]).rstrip("/")
API_KEY = (os.getenv("LLM_API_KEY") or "").strip()
MODEL = (os.getenv("MODEL") or os.getenv("LLM_MODEL") or "").strip()
try:
    TIMEOUT = float(os.getenv("LLM_TIMEOUT", "600") or "600")
except ValueError:
    TIMEOUT = 600.0


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: str) -> None:
    """Print a JSON error line to stdout and exit 1 (fail-closed contract)."""
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


def _preflight_binding() -> None:
    """Fail-closed preflight for a fresh CE with nothing bound (day-1 actionable).

    Reachable ONLY when the orchestrator injected nothing at all: no LLM_BASE_URL,
    no LLM_MODEL/MODEL, and both role endpoints still the built-in defaults (a
    user-set LLM_BASE_URL / LLM_GENERATOR_URL / LLM_REASONER_URL is an explicit
    endpoint and keeps the existing behaviour byte-for-byte). The CE compose ships
    no ollama / vllm-big service, so without a binding the old path died deep in
    main() with a raw connection error; this names the user action instead.
    """
    if (os.getenv("LLM_BASE_URL") or "").strip():
        return
    if MODEL:
        return
    if ((os.getenv("LLM_GENERATOR_URL") or "").strip()
            or (os.getenv("LLM_REASONER_URL") or "").strip()):
        return
    die("Text LLM has no model bound: bind a model in Settings > AI Configuration, "
        "then run again.", "NO_MODEL_BOUND")


def _read_first(*names: str) -> str:
    for n in names:
        f = IN / n
        if f.exists() and f.is_file():
            return f.read_text(errors="replace").strip()
    return ""


def _read_prompt() -> str:
    q = (os.getenv("PROMPT") or "").strip()
    if q:
        return q
    q = _read_first("prompt.txt", "input.txt", "request.txt")
    if q:
        return q
    # Any staged text file that is not the context file.
    for f in sorted(IN.rglob("*")):
        if (f.is_file() and f.suffix.lower() in {".txt", ".md"}
                and f.name.lower() != "context.txt"):
            t = f.read_text(errors="replace").strip()
            if t:
                return t
    return ""


def _read_context() -> str:
    c = (os.getenv("CONTEXT") or "").strip()
    if c:
        return c
    return _read_first("context.txt")


def _http_json(url: str, data: bytes | None, method: str):
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _discover_model() -> str:
    """The served model id, from the endpoint's /v1/models. Empty on failure."""
    try:
        body = _http_json(f"{BASE_URL}/v1/models", None, "GET")
        items = body.get("data") or body.get("models") or []
        if items:
            first = items[0]
            return first.get("id") or first.get("name") or ""
    except Exception:
        pass
    return ""


def main() -> int:
    _preflight_binding()
    prompt = _read_prompt()
    if not prompt:
        die("no prompt provided", "NO_INPUT")
    context = _read_context()

    model = MODEL or _discover_model()
    if not model:
        die("no MODEL set and model discovery failed", "PROCESSING_FAILURE")

    # Compose the messages. Context is presented as grounding the model must use
    # rather than free text, which is what makes this a prompt-composer: the
    # PROMPT is the request, the CONTEXT is the retrieved material to compose from.
    messages = []
    if SYSTEM_PROMPT:
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    if context:
        user = (
            "Use the following context to answer the request. "
            "If it is not relevant, rely on your own knowledge.\n\n"
            f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
            f"=== REQUEST ===\n{prompt}"
        )
    else:
        user = prompt
    messages.append({"role": "user", "content": user})

    body = {
        "model": model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }

    host = BASE_URL.split("://")[-1].split("@")[-1].split("/")[0] if "://" in BASE_URL else BASE_URL
    log(f"POST /v1/chat/completions to {host}  role={ROLE} model={model} "
        f"temp={TEMPERATURE:g} max_tokens={MAX_TOKENS} "
        f"context={'yes' if context else 'no'}")
    try:
        resp = _http_json(
            f"{BASE_URL}/v1/chat/completions",
            json.dumps(body).encode("utf-8"),
            "POST",
        )
    except urllib.error.HTTPError as e:
        log(f"ERROR: LLM endpoint returned HTTP {e.code}")
        die("llm endpoint returned HTTP error", "PROCESSING_FAILURE")
    except urllib.error.URLError as e:
        log(f"ERROR: cannot reach the configured LLM at {host}: {e.reason}")
        die("llm endpoint unreachable", "PROCESSING_FAILURE")
    except Exception as e:
        log(f"ERROR: unexpected error calling LLM endpoint at {host}: {e}")
        die("llm endpoint request failed", "PROCESSING_FAILURE")

    try:
        choice = (resp.get("choices") or [])[0]
        text = (choice.get("message") or {}).get("content", "")
        finish = choice.get("finish_reason", "")
    except (IndexError, AttributeError, KeyError):
        log(f"ERROR: unexpected response shape")
        die("unexpected response from LLM endpoint", "INTERNAL")

    text = (text or "").strip()
    if not text:
        die("the LLM returned an empty completion", "PROCESSING_FAILURE")

    (OUT / "output.txt").write_text(text)
    (OUT / "response.json").write_text(json.dumps({
        "prompt": prompt,
        "context_used": bool(context),
        "role": ROLE,
        "endpoint": BASE_URL,
        "model": model,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "finish_reason": finish,
        "usage": resp.get("usage") or {},
        "text": text,
    }, indent=2))

    if not (OUT / "output.txt").exists() or not (OUT / "response.json").exists():
        die("expected output files not written", "OUTPUT_NOT_WRITTEN")

    usage = resp.get("usage") or {}
    log(f"OK: {len(text)} chars generated "
        f"(prompt_tokens={usage.get('prompt_tokens', '?')}, "
        f"completion_tokens={usage.get('completion_tokens', '?')}, "
        f"finish_reason={finish or '?'})")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        log(f"ERROR: unhandled exception in main: {e}")
        die("internal error", "INTERNAL")
