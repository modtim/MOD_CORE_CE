"""stack-model-call — N image cogs + PROMPT / SYSTEM_PROMPT / JSON_SCHEMA -> the tenant's own model stack.

Sends POST {API_URL}/v1/llm/chat/completions (the same base URL the reference
nodes read from ``API_URL`` — 3d-thumbnail-gen/generate_thumbnail.py:273) with the
node's run_task token from the vendored ``mod_node_auth.get_headers()``.
No other host is ever contacted and there is no BASE_URL override env.
"""
import base64
import json
import os
import pathlib
import sys
import time

import requests

import mod_node_auth

INPUT_DIR = pathlib.Path("/data/input")
OUTPUT_DIR = pathlib.Path("/data/output")

EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif"}
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"),
          (b"\xff\xd8\xff", "image/jpeg"),
          (b"GIF87a", "image/gif"),
          (b"GIF89a", "image/gif"))
RETRY_STATUS = (502, 503, 504)
BODY_ERR_CHARS = 500
REPLY_ERR_CHARS = 200
DEFAULT_MODEL_WAKE_TIMEOUT_SECONDS = 600
MODEL_WAKE_TIMEOUT_MIN_SECONDS = 0
MODEL_WAKE_TIMEOUT_MAX_SECONDS = 3600
RETRY_AFTER_MIN_SECONDS = 1
RETRY_AFTER_MAX_SECONDS = 60
RETRY_AFTER_DEFAULT_SECONDS = 5


def log(message):
    print(message, flush=True)


def fail(msg: str, code: str = "INTERNAL") -> None:
    """CE node JSON error contract: ONE stdout JSON line, exit non-zero.

    Mirrors CORE/DockerContainers/WebSearch/entrypoint.py:87-92. Messages name
    variables only — never token values, URL userinfo, or input file contents;
    response bodies stay truncated by the existing BODY_ERR_CHARS/REPLY_ERR_CHARS
    caps. json.dumps keeps the payload on a single stdout line."""
    print(json.dumps({"code": code, "error": msg}, sort_keys=True), flush=True)
    sys.exit(2)


def _env_number(name, cast, default, lo=None, hi=None):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number (got {raw!r})")
    if cast is int and value <= 0:
        raise ValueError(f"{name} must be > 0")
    if lo is not None and value < lo:
        raise ValueError(f"{name} must be >= {lo}")
    if hi is not None and value > hi:
        raise ValueError(f"{name} must be <= {hi}")
    return value


def _scan_images(input_dir, max_images, max_image_bytes):
    """Return [(mime, raw_bytes)] for regular image files under input_dir.

    Symlinks are skipped (never followed, never read); the listing is
    non-recursive so nothing outside input_dir is reachable. Raises ValueError
    when the count/size caps or the extension-vs-magic check fails — nothing is
    silently dropped.
    """
    if not input_dir.is_dir():
        return []
    candidates = []
    for entry in sorted(input_dir.iterdir(), key=lambda e: str(e)):
        if entry.is_symlink():
            log(f"skipping symlink (not followed): {entry.name}")
            continue
        if not entry.is_file():
            continue
        if entry.suffix.lower() in EXT_MIME:
            candidates.append(entry)
    if len(candidates) > max_images:
        raise ValueError(f"too many images: {len(candidates)} found, MAX_IMAGES={max_images}")
    found = []
    for entry in candidates:
        size = entry.stat().st_size
        if size > max_image_bytes:
            raise ValueError(f"image {entry.name} is {size} bytes, MAX_IMAGE_BYTES={max_image_bytes}")
        with entry.open("rb") as fh:
            raw = fh.read()
        want = EXT_MIME[entry.suffix.lower()]
        head = raw[:16]
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            got = "image/webp"
        else:
            got = next((mime for sig, mime in _MAGIC if head.startswith(sig)), None)
        if got != want:
            raise ValueError(f"image {entry.name} extension says {want} but content is not ({got or 'unknown'})")
        found.append((want, raw))
    return found


def _build_messages(prompt, system_prompt, images):
    parts = [{"type": "image_url",
              "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}}
             for mime, raw in images]
    parts.append({"type": "text", "text": prompt})
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": parts if images else prompt})
    return messages


def _model_wake_timeout_seconds(headers):
    """Total wake budget in seconds: env MODEL_WAKE_TIMEOUT_SECONDS wins, then
    the API's X-Model-Wake-Budget-Seconds header, then 600 (entrypoint.py:31).
    Garbage in either is ignored, never a crash; the result is clamped 0..3600
    (S4: the budget comes ONLY from env/header/default and is clamped)."""
    raw = (os.environ.get("MODEL_WAKE_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None:
            return min(MODEL_WAKE_TIMEOUT_MAX_SECONDS, max(MODEL_WAKE_TIMEOUT_MIN_SECONDS, value))
    raw = str(headers.get("X-Model-Wake-Budget-Seconds") or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None:
            return min(MODEL_WAKE_TIMEOUT_MAX_SECONDS, max(MODEL_WAKE_TIMEOUT_MIN_SECONDS, value))
    return DEFAULT_MODEL_WAKE_TIMEOUT_SECONDS


def _retry_after_seconds(headers):
    """Retry-After as int seconds, clamped 1..60; missing/garbage -> 5."""
    try:
        value = int(str(headers.get("Retry-After") or "").strip())
    except ValueError:
        return RETRY_AFTER_DEFAULT_SECONDS
    return min(RETRY_AFTER_MAX_SECONDS, max(RETRY_AFTER_MIN_SECONDS, value))


def _is_model_waking(status, data):
    """True only for the API's machine-readable "model_waking" 503 (c7-a3/c14).

    Accepts all three shapes the route may send: detail == "model_waking", a
    top-level code == "model_waking", or a dict detail whose code is
    "model_waking". A non-dict body -> False, so a plain 502/503/504 is never
    wake-looped (S3)."""
    if status != 503 or not isinstance(data, dict):
        return False
    detail = data.get("detail")
    return (detail == "model_waking"
            or data.get("code") == "model_waking"
            or (isinstance(detail, dict) and detail.get("code") == "model_waking"))


def _post_chat(api_base, body, timeout):
    """One POST to the MOD LLM route. Returns (status_code, parsed_json_or_None,
    text, response_headers). Headers come from ``getattr(resp, "headers", None) or {}``
    so the existing test stubs (which have no .headers attribute) keep working."""
    url = f"{api_base.rstrip('/')}/v1/llm/chat/completions"
    resp = requests.post(url, json=body, headers=mod_node_auth.get_headers(), timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status_code, data, resp.text, getattr(resp, "headers", None) or {}


def _wake_chat(model, api_base, body, timeout, status, data, text, headers):
    """30.6.7 (c7-a5) — the NODE owns the total wake budget.

    Loops ONLY while the reply is the API's model_waking 503 (S3), sleeping
    Retry-After (clamped 1..60) each turn, until the deadline computed ONCE
    from env/header/default (S4). Returns the final (status, data, text)
    triple; a budget of 0 or a spent deadline fails MODEL_UNAVAILABLE (S1:
    bounded by the deadline, no other exit). Logs name the model and the
    sleep only — never a token or URL (S2)."""
    budget = _model_wake_timeout_seconds(headers)
    deadline = time.monotonic() + budget
    while _is_model_waking(status, data):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(f"model '{model}' is still waking after {budget}s "
                 "(MODEL_WAKE_TIMEOUT_SECONDS)", "MODEL_UNAVAILABLE")
        sleep_s = min(_retry_after_seconds(headers), remaining)
        log(f"model waking — retrying in {sleep_s}s")
        time.sleep(sleep_s)
        status, data, text, headers = _post_chat(api_base, body, timeout)
    return status, data, text


def main():
    model = (os.environ.get("MODEL") or "").strip()
    prompt = os.environ.get("PROMPT")
    if not model:
        fail("MODEL is required: set MODEL in this node's panel in the Workflow "
             "Builder to a model one of your deployed stacks serves (Dashboard > Stacks).",
             "BAD_PARAM")
    if prompt is None or prompt == "":
        fail("PROMPT is required: type the question about the images into PROMPT in "
             "this node's panel in the Workflow Builder.", "BAD_PARAM")
    system_prompt = os.environ.get("SYSTEM_PROMPT") or ""
    schema_raw = (os.environ.get("JSON_SCHEMA") or "").strip()
    schema = None
    if schema_raw:
        try:
            schema = json.loads(schema_raw)
        except ValueError:
            fail("JSON_SCHEMA is not valid JSON", "BAD_PARAM")
        if not isinstance(schema, dict):
            fail("JSON_SCHEMA must be a JSON object", "BAD_PARAM")
    api_base = os.environ.get("API_URL", "").strip()
    if not api_base:
        fail("API_URL is not set", "BAD_PARAM")
    try:
        max_images = _env_number("MAX_IMAGES", int, 8, lo=1)
        max_image_bytes = _env_number("MAX_IMAGE_BYTES", int, 8 * 1024 * 1024, lo=1)
        max_tokens = _env_number("MAX_TOKENS", int, None)
        temperature = _env_number("TEMPERATURE", float, None, lo=0.0, hi=2.0)
        timeout = _env_number("REQUEST_TIMEOUT_SECONDS", int, 300, lo=1)
    except ValueError as exc:
        # bad tunables (not a number / out of range) — genuinely unexpected here
        fail(str(exc), "INTERNAL")
    try:
        images = _scan_images(INPUT_DIR, max_images, max_image_bytes)
    except ValueError as exc:
        # too many images / oversize / magic-byte mismatch = bad user input
        fail(str(exc), "BAD_PARAM")

    body = {"model": model, "messages": _build_messages(prompt, system_prompt, images)}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "output", "schema": schema, "strict": True}}

    status, data, text, headers = _post_chat(api_base, body, timeout)
    if _is_model_waking(status, data):
        status, data, text = _wake_chat(model, api_base, body, timeout, status, data, text, headers)
    if status in RETRY_STATUS:
        log(f"HTTP {status} from the LLM route — retrying once")
        mod_node_auth.refresh()
        status, data, text, _ = _post_chat(api_base, body, timeout)
    if status in (401, 403):
        fail(f"node token rejected by the LLM route (HTTP {status}) — "
             "the run_task credential is not allowed on /v1/llm; body not echoed",
             "PROCESSING_FAILURE")
    if status == 413:
        fail("the API rejected the request as too large (HTTP 413) — "
             "lower MAX_IMAGES / MAX_IMAGE_BYTES / MAX_TOKENS",
             "PROCESSING_FAILURE")
    # 404 guard (FIX-STACK-MODEL-CALL-ENTRYPOINT-404-GUARD): a plain FastAPI 404
    # sends {"detail": "Not Found"} — a STRING detail, so the old
    # `(data.get("detail") or data).get("error")` raised AttributeError on a str
    # and the node died with no JSON line. Take the error object from `detail`
    # only when it is a dict (else from `data` when data is a dict); anything
    # else (string detail, list, None, non-JSON) falls through to the existing
    # generic `LLM route returned HTTP <status>: <body[:BODY_ERR_CHARS]>` message,
    # which names the status and the bounded body. The dict-shaped unbound-model
    # message below is byte-identical to the previous fix's.
    detail404 = data.get("detail") if isinstance(data, dict) else None
    err404 = detail404 if isinstance(detail404, dict) else \
        (data if isinstance(data, dict) else None)
    if status == 404 and isinstance(err404, dict) and err404.get("error") == "model not found":
        # the tenant has no deployed stack serving this model id
        available = detail404.get("available_models") if isinstance(detail404, dict) else None
        names = ", ".join(f"'{m}'" for m in (available or []))
        fail(f"model '{model}' is not one your stacks serve: deploy a stack that "
             f"serves it (Dashboard > Stacks) or set MODEL in this node's panel in "
             f"the Workflow Builder to one of the models you can use now"
             + (f" — available now: {names}" if names else " — no model is available yet"),
             "MODEL_UNAVAILABLE")
    if status == 503 and isinstance(data, dict) and \
            str((data.get("detail") or "")).startswith("Model ") and \
            "not reachable" in str(data.get("detail") or ""):
        # a deployed stack that is stopped/unreachable (the API's _down_detail,
        # not the machine-readable model_waking 503 the wake loop above owns)
        fail(f"model '{model}' is not reachable right now: start its stack on "
             "the Stacks page (Dashboard > Stacks), then run again.",
             "MODEL_UNAVAILABLE")
    if status != 200:
        fail(f"LLM route returned HTTP {status}: {text[:BODY_ERR_CHARS]}",
             "PROCESSING_FAILURE")

    choice = (data or {}).get("choices") or [{}]
    message = choice[0].get("message") or {}
    reply = message.get("content") or ""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "response.txt").write_text(reply, encoding="utf-8")
    if schema is not None:
        try:
            parsed = json.loads(reply)
        except ValueError:
            fail(f"model reply was not valid JSON: {reply[:REPLY_ERR_CHARS]}",
                 "PROCESSING_FAILURE")
        (OUTPUT_DIR / "response.json").write_text(json.dumps(parsed), encoding="utf-8")
    result = {"model": (data or {}).get("model", model),
              "usage": (data or {}).get("usage"),
              "finish_reason": choice[0].get("finish_reason")}
    (OUTPUT_DIR / "result.json").write_text(json.dumps(result), encoding="utf-8")
    log(f"OK: {len(images)} image(s) -> model {model}, {len(reply)} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
