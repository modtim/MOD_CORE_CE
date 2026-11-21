"""U1-U7 for stack-model-call. The HTTP call is stubbed at the requests boundary."""
import io
import json
import os
import sys
import pathlib
import importlib.util
import tempfile

NODE_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
TOKEN = "sekret-token-DO-NOT-LEAK"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff" + b"\x11" * 40
WEBP = b"RIFF$\x00\x00\x00WEBP" + b"\x22" * 24
SYMLINK = "<<symlink>>"


class _Resp:
    def __init__(self, status, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(reply="hello there"):
    return _Resp(200, {"id": "x", "object": "chat.completion", "created": 0,
                       "model": "served-m",
                       "choices": [{"index": 0,
                                    "message": {"role": "assistant", "content": reply},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 7, "completion_tokens": 3,
                                 "total_tokens": 10}})


def _run(responses, tmp, files=None, env=None, monkey=None):
    """Run entrypoint.main() with requests.post stubbed. Returns a result dict."""
    calls = []

    class Fake:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            if url.endswith("/token"):
                calls.append({"url": url, "body": None, "headers": headers or {}})
                return _Resp(200, {"token": TOKEN, "exp": 4e9})
            calls.append({"url": url, "body": json, "headers": headers or {}})
            idx = sum(1 for c in calls if not c["url"].endswith("/token")) - 1
            return responses[min(idx, len(responses) - 1)]

    ep = _load_entrypoint(Fake())
    ep.INPUT_DIR = pathlib.Path(tmp)
    ep.OUTPUT_DIR = pathlib.Path(tmp) / "out"
    os.environ.clear()
    os.environ.update({"MODEL": "served-m", "PROMPT": "describe these",
                       "API_URL": "http://api:80",
                       "MOD_TASK_TOKEN_URL": "http://worker/token",
                       "MOD_TASK_NONCE": "nonce-1"})
    os.environ.update(env or {})
    if files is not None:
        pathlib.Path(tmp).mkdir(parents=True, exist_ok=True)
        for name, blob in files.items():
            p = pathlib.Path(tmp) / name
            if blob is SYMLINK:
                outside = pathlib.Path(str(tmp) + "-outside")
                outside.mkdir(exist_ok=True)
                (outside / "target.png").write_bytes(PNG)
                p.symlink_to(outside / "target.png")
            else:
                p.write_bytes(blob)
    out, err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        try:
            rc = ep.main()
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.stdout, sys.stderr = real_out, real_err
    outdir = pathlib.Path(tmp) / "out"
    blobs = {p.name: p.read_bytes() for p in sorted(outdir.glob("*"))} if outdir.is_dir() else {}
    chat = [c for c in calls if not c["url"].endswith("/token")]
    return {"rc": rc, "chat": chat, "files": blobs,
            "stdout": out.getvalue(), "stderr": err.getvalue()}


def _load_entrypoint(fake_requests):
    sys.modules["requests"] = fake_requests
    for name in ("mod_node_auth", "entrypoint"):
        path = pathlib.Path(NODE_DIR) / (name + ".py")
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules["entrypoint"]


def _tmp():
    return pathlib.Path(tempfile.mkdtemp())


def _user(messages):
    return messages[-1]


def _parts(body):
    c = _user(body["messages"])["content"]
    return c if isinstance(c, list) else [{"type": "text", "text": c}]


# U1: 2 png + 1 jpeg -> 3 image_url parts (correct MIME), text part last, key set
def test_u1_images_mime_and_api_key():
    tmp = _tmp()
    res = _run([_ok()], tmp, files={"a.png": PNG, "b.jpeg": JPEG, "c.jpg": JPEG})
    assert res["rc"] == 0
    body = res["chat"][0]["body"]
    parts = _parts(body)
    imgs = [p for p in parts if p["type"] == "image_url"]
    assert len(imgs) == 3
    assert [p["image_url"]["url"].split(";")[0].split(":")[1] for p in imgs] == \
        ["image/png", "image/jpeg", "image/jpeg"]
    assert parts[-1] == {"type": "text", "text": "describe these"}
    assert res["chat"][0]["headers"].get("X-API-Key") == TOKEN


# U2: no images -> text-only content; SYSTEM_PROMPT -> system message first
def test_u2_text_only_and_system():
    tmp = _tmp()
    res = _run([_ok()], tmp, files={}, env={"SYSTEM_PROMPT": "be terse"})
    assert res["rc"] == 0
    msgs = res["chat"][0]["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert _user(msgs)["content"] == "describe these"


# U3: JSON_SCHEMA -> response_format json_schema; valid reply -> response.json;
#     invalid reply -> non-zero exit
def test_u3_json_mode():
    tmp = _tmp()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    res = _run([_ok(reply=json.dumps({"ok": True}))], tmp, files={},
               env={"JSON_SCHEMA": json.dumps(schema)})
    assert res["rc"] == 0
    fmt = res["chat"][0]["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "output"
    assert fmt["json_schema"]["schema"] == schema
    assert fmt["json_schema"]["strict"] is True
    assert json.loads(res["files"]["response.json"]) == {"ok": True}
    bad = _run([_ok(reply="not json at all")], _tmp(), files={},
               env={"JSON_SCHEMA": json.dumps(schema)})
    assert bad["rc"] != 0
    assert "not json at all" in bad["stdout"]


# U4: 9 images with MAX_IMAGES=8 -> non-zero, no HTTP call; oversize -> same;
#     a .png that is really text -> rejected
def test_u4_caps_and_magic():
    tmp = _tmp()
    nine = {f"i{n}.png": PNG for n in range(9)}
    res = _run([_ok()], tmp, files=nine, env={"MAX_IMAGES": "8"})
    assert res["rc"] != 0 and res["chat"] == []
    assert "too many images" in res["stdout"]
    big = _run([_ok()], _tmp(), files={"huge.png": PNG + b"\x00" * 4096},
               env={"MAX_IMAGE_BYTES": "100"})
    assert big["rc"] != 0 and big["chat"] == []
    assert "MAX_IMAGE_BYTES" in big["stdout"]
    fake = _run([_ok()], _tmp(), files={"lie.png": b"just plain text, no png here"})
    assert fake["rc"] != 0 and fake["chat"] == []
    assert "content is not" in fake["stdout"]
    webp_ok = _run([_ok()], _tmp(), files={"w.webp": WEBP})
    assert webp_ok["rc"] == 0
    assert _parts(webp_ok["chat"][0]["body"])[0]["image_url"]["url"].startswith("data:image/webp")


# U5: symlink in input -> skipped, never read/followed
def test_u5_symlink_skipped():
    tmp = _tmp()
    res = _run([_ok()], tmp, files={"link.png": SYMLINK})
    assert res["rc"] == 0
    assert "skipping symlink" in res["stdout"]
    assert res["chat"][0]["body"] is not None
    assert [p for p in _parts(res["chat"][0]["body"]) if p["type"] == "image_url"] == []


# U6 (C1): token appears 0 times in stdout/stderr and in every output file, on
#          both a 500 error and a success
def test_u6_token_never_leaks():
    tmp = _tmp()
    boom = _run([_Resp(500, text="server blew up " + "x" * 900)], tmp, files={})
    blob = boom["stdout"] + boom["stderr"] + b"".join(
        v for v in boom["files"].values()).decode("latin-1")
    assert boom["rc"] != 0
    assert TOKEN not in blob
    assert len([c for c in boom["chat"]]) == 1
    ok = _run([_ok()], tmp, files={"a.png": PNG})
    blob2 = ok["stdout"] + ok["stderr"] + b"".join(
        v for v in ok["files"].values()).decode("latin-1")
    assert ok["rc"] == 0 and TOKEN not in blob2
    assert set(ok["files"]) == {"response.txt", "result.json"}
    assert "describe these" not in blob2


# U7: 502 then 200 -> one retry, success. 400 -> no retry, non-zero. 413 -> clear error
def test_u7_retry_and_no_retry():
    tmp = _tmp()
    res = _run([_Resp(502, text="bad gateway"), _ok()], tmp, files={})
    assert res["rc"] == 0 and len(res["chat"]) == 2
    assert "retrying once" in res["stdout"]
    bad = _run([_Resp(400, text="bad request")], tmp, files={})
    assert bad["rc"] != 0 and len(bad["chat"]) == 1
    assert "HTTP 400" in bad["stdout"]
    too_big = _run([_Resp(413, text="payload too large")], tmp, files={})
    assert too_big["rc"] != 0 and len(too_big["chat"]) == 1
    assert "HTTP 413" in too_big["stdout"] or "too large" in too_big["stdout"]


# n7bR: 401/403 surface a clear "node token rejected" (no body echo, no retry);
#       bad REQUEST_TIMEOUT_SECONDS fails loud before any HTTP call
def test_u8_token_rejected_and_timeout_guard():
    tmp = _tmp()
    denied = _run([_Resp(401, text="proxy config leaked: X-API-Key=" + TOKEN)], tmp, files={})
    assert denied["rc"] != 0 and len(denied["chat"]) == 1
    assert "node token rejected" in denied["stdout"]
    assert TOKEN not in denied["stdout"] + denied["stderr"]
    bad_to = _run([_ok()], tmp, files={}, env={"REQUEST_TIMEOUT_SECONDS": "abc"})
    assert bad_to["rc"] != 0 and bad_to["chat"] == []
    assert "REQUEST_TIMEOUT_SECONDS" in bad_to["stdout"]


# n7f: fail-closed JSON contract (vet-container.sh:195-240). Every error path
#      ends with ONE stdout JSON line {"code": ..., "error": "<human msg>"} and
#      a non-zero exit. Codes: BAD_PARAM / PROCESSING_FAILURE / INTERNAL.
def _last_json(stdout):
    lines = [l.strip() for l in stdout.splitlines() if l.strip()]
    assert lines, "no stdout"
    obj = json.loads(lines[-1])
    assert isinstance(obj, dict) and isinstance(obj.get("error"), str)
    return obj


def test_n7f_bad_param():
    res = _run([_ok()], _tmp(), files={}, env={"MODEL": ""})
    assert res["rc"] != 0 and res["chat"] == []
    assert _last_json(res["stdout"])["code"] == "BAD_PARAM"
    assert "MODEL is required" in _last_json(res["stdout"])["error"]
    bad_schema = _run([_ok()], _tmp(), files={}, env={"JSON_SCHEMA": "{nope"})
    assert bad_schema["rc"] != 0 and bad_schema["chat"] == []
    assert _last_json(bad_schema["stdout"])["code"] == "BAD_PARAM"


def test_n7f_processing_failure():
    res = _run([_Resp(413, text="payload too large")], _tmp(), files={})
    assert res["rc"] != 0 and len(res["chat"]) == 1
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    denied = _run([_Resp(403, text="nope")], _tmp(), files={})
    assert _last_json(denied["stdout"])["code"] == "PROCESSING_FAILURE"
    assert TOKEN not in denied["stdout"]


def test_n7f_internal():
    res = _run([_ok()], _tmp(), files={}, env={"MAX_IMAGES": "abc"})
    assert res["rc"] != 0 and res["chat"] == []
    assert _last_json(res["stdout"])["code"] == "INTERNAL"
    assert "MAX_IMAGES" in _last_json(res["stdout"])["error"]


def test_n7f_caps_are_bad_param():
    over = _run([_ok()], _tmp(), files={f"i{n}.png": PNG for n in range(9)},
                env={"MAX_IMAGES": "8"})
    assert over["rc"] != 0 and over["chat"] == []
    assert _last_json(over["stdout"])["code"] == "BAD_PARAM"
    big = _run([_ok()], _tmp(), files={"huge.png": PNG + b"\x00" * 4096},
               env={"MAX_IMAGE_BYTES": "100"})
    assert big["rc"] != 0 and big["chat"] == []
    assert _last_json(big["stdout"])["code"] == "BAD_PARAM"
    fake = _run([_ok()], _tmp(), files={"lie.png": b"just plain text, no png here"})
    assert fake["rc"] != 0 and fake["chat"] == []
    assert _last_json(fake["stdout"])["code"] == "BAD_PARAM"


def test_n7f_no_input_subprocess():
    """Vet-style no-input smoke: real process, empty env, last stdout line parses."""
    import subprocess
    proc = subprocess.run([sys.executable, str(pathlib.Path(NODE_DIR) / "entrypoint.py")],
                          capture_output=True, text=True, env={}, timeout=30)
    assert proc.returncode != 0
    lines = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
    obj = json.loads(lines[-1])
    print("no-input last-line code:", obj["code"])
    assert obj["code"] == "BAD_PARAM"


# ---------------------------------------------------------------------------
# 30.6.7 c7-a5 — the NODE owns the total wake budget: it retries ONLY the
# model_waking 503 (detail / top-level code / dict-detail code), sleeps
# Retry-After (clamped 1..60) until a deadline computed ONCE from
# env MODEL_WAKE_TIMEOUT_SECONDS -> X-Model-Wake-Budget-Seconds -> 600, and
# fails MODEL_UNAVAILABLE when that budget is spent. A plain 503 never loops.
# time.sleep / time.monotonic are patched so no test ever really waits.
# ---------------------------------------------------------------------------

def _waking(shape="detail", retry_after=None, budget=None):
    """A 503 in one of the three model_waking shapes, with response headers."""
    if shape == "detail":
        payload = {"detail": "model_waking"}
    elif shape == "code":
        payload = {"detail": "the stack is cold", "code": "model_waking"}
    else:
        payload = {"detail": {"code": "model_waking", "message": "waking"}}
    resp = _Resp(503, payload)
    resp.headers = {}
    if retry_after is not None:
        resp.headers["Retry-After"] = str(retry_after)
    if budget is not None:
        resp.headers["X-Model-Wake-Budget-Seconds"] = str(budget)
    return resp


def _run_wake(responses, tmp, files=None, env=None):
    """_run with the entrypoint's clock faked: sleeps are recorded (res["slept"])
    and advance a virtual deadline instead of really waiting."""
    import time as _time
    real_monotonic, real_sleep = _time.monotonic, _time.sleep
    slept = []
    offset = [0.0]
    _time.monotonic = lambda: offset[0]  # pure virtual clock: no wall-clock drift

    def _sleep(n):
        slept.append(n)
        offset[0] += n

    _time.sleep = _sleep
    try:
        res = _run(responses, tmp, files=files, env=env)
    finally:
        _time.monotonic, _time.sleep = real_monotonic, real_sleep
    res["slept"] = slept
    return res


def _wake_lines(stdout):
    return [l for l in stdout.splitlines() if "model waking" in l]


# E1: two model_waking 503s (Retry-After 3, then 7) then 200 -> success, and the
#     sleeps equal the Retry-After values (no real wait: the clock is patched)
def test_e1_wake_loop_then_success():
    tmp = _tmp()
    res = _run_wake([_waking(retry_after=3), _waking(retry_after=7), _ok()], tmp, files={})
    assert res["rc"] == 0 and len(res["chat"]) == 3
    assert res["slept"] == [3, 7]
    assert len(_wake_lines(res["stdout"])) == 2
    assert "retrying in 3s" in res["stdout"] and "retrying in 7s" in res["stdout"]
    assert "retrying once" not in res["stdout"]  # the generic retry was not consumed


# E2: the budget is spent -> last stdout line is code MODEL_UNAVAILABLE
def test_e2_budget_spent_model_unavailable():
    tmp = _tmp()
    res = _run_wake([_waking(retry_after=60), _waking(retry_after=60)], tmp, files={},
                    env={"MODEL_WAKE_TIMEOUT_SECONDS": "10"})
    assert res["rc"] != 0
    assert res["slept"] == [10]
    assert _last_json(res["stdout"])["code"] == "MODEL_UNAVAILABLE"
    assert "MODEL_WAKE_TIMEOUT_SECONDS" in _last_json(res["stdout"])["error"]
    assert TOKEN not in res["stdout"] + res["stderr"]


# E3: a plain 503 (detail other) -> the existing single retry only, never the
#     wake loop
def test_e3_plain_503_uses_generic_retry_only():
    tmp = _tmp()
    res = _run([_Resp(503, text="upstream timeout"), _ok()], tmp, files={})
    assert res["rc"] == 0 and len(res["chat"]) == 2
    assert "retrying once" in res["stdout"]
    assert _wake_lines(res["stdout"]) == []


# E4: env 0 -> immediate MODEL_UNAVAILABLE after the first waking reply (no sleep)
def test_e4_zero_budget_fails_at_once():
    tmp = _tmp()
    res = _run_wake([_waking(retry_after=30)], tmp, files={},
                    env={"MODEL_WAKE_TIMEOUT_SECONDS": "0"})
    assert res["rc"] != 0 and res["slept"] == []
    assert len(res["chat"]) == 1
    assert _last_json(res["stdout"])["code"] == "MODEL_UNAVAILABLE"


# E5: the header budget is used when env is unset; the env overrides the header
def test_e5_header_budget_and_env_override():
    tmp = _tmp()
    hdr = _run_wake([_waking(retry_after=3, budget=4)], tmp, files={})
    assert hdr["rc"] != 0 and hdr["slept"] == [3, 1]  # 4s budget, deadline once
    assert _last_json(hdr["stdout"])["code"] == "MODEL_UNAVAILABLE"
    over = _run_wake([_waking(retry_after=3, budget=300)], tmp, files={},
                     env={"MODEL_WAKE_TIMEOUT_SECONDS": "2"})
    assert over["rc"] != 0 and over["slept"] == [2]  # env wins over the header
    assert _last_json(over["stdout"])["error"].endswith("(MODEL_WAKE_TIMEOUT_SECONDS)")


# E6: dict-detail {"detail":{"code":"model_waking"}} and a top-level code shape
#     are both detected as waking
def test_e6_all_three_waking_shapes():
    tmp = _tmp()
    for shape in ("detail", "code", "dict"):
        res = _run_wake([_waking(shape, retry_after=2), _ok()], tmp, files={})
        assert res["rc"] == 0 and len(res["chat"]) == 2
        assert res["slept"] == [2]
        assert len(_wake_lines(res["stdout"])) == 1


# E7: Retry-After garbage -> 5; 999 -> clamped to 60
def test_e7_retry_after_clamped():
    tmp = _tmp()
    junk = _run_wake([_waking(retry_after="soon"), _ok()], tmp, files={})
    assert junk["rc"] == 0 and junk["slept"] == [5]
    big = _run_wake([_waking(retry_after=999), _ok()], tmp, files={})
    assert big["rc"] == 0 and big["slept"] == [60]
    missing = _run_wake([_waking(), _ok()], tmp, files={})
    assert missing["rc"] == 0 and missing["slept"] == [5]


# E8: model_waking reply, then a plain 503, then 200 -> success (the generic
#     retry is still available after the wake loop exits)
def test_e8_waking_then_plain_503_then_ok():
    tmp = _tmp()
    res = _run_wake([_waking(retry_after=2), _Resp(503, text="upstream down"), _ok()],
                    tmp, files={})
    assert res["rc"] == 0 and len(res["chat"]) == 3
    assert res["slept"] == [2]
    assert "retrying once" in res["stdout"]
