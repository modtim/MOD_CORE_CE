"""FIX-NODE-STACK-MODEL-CALL-UNBOUND-MESSAGE (lane r9700-2).

One test per unactionable case: the message must be ONE sentence naming the
fix with the real UI path, exit non-zero, and leak nothing. The HTTP call is
stubbed at the requests boundary (same posture as test_entrypoint.py:1-90).
"""
import io
import json
import os
import sys
import pathlib
import importlib.util
import tempfile

NODE_DIR = str(pathlib.Path(__file__).resolve().parent.parent)
TOKEN = "sekret-token-DO-NOT-LEAK"


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
    return _Resp(200, {"id": "x", "model": "served-m",
                       "choices": [{"index": 0,
                                    "message": {"role": "assistant", "content": reply},
                                    "finish_reason": "stop"}],
                       "usage": {"total_tokens": 10}})


def _run(responses, env=None, files=None):
    """Run entrypoint.main() with requests.post stubbed; returns rc/stdout/chat/files."""
    calls = []

    class Fake:
        def post(self, url, json=None, headers=None, timeout=None, data=None):
            if url.endswith("/token"):
                calls.append({"url": url, "body": None, "headers": headers or {}})
                return _Resp(200, {"token": TOKEN, "exp": 4e9})
            calls.append({"url": url, "body": json, "headers": headers or {}})
            idx = sum(1 for c in calls if not c["url"].endswith("/token")) - 1
            return responses[min(idx, len(responses) - 1)]

    sys.modules["requests"] = Fake()
    for name in ("mod_node_auth", "entrypoint"):
        path = pathlib.Path(NODE_DIR) / (name + ".py")
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    ep = sys.modules["entrypoint"]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="smpc-"))
    ep.INPUT_DIR = tmp
    ep.OUTPUT_DIR = tmp / "out"
    if files is not None:
        tmp.mkdir(parents=True, exist_ok=True)
        for name, blob in files.items():
            (tmp / name).write_bytes(blob)
    os.environ.clear()
    os.environ.update({"MODEL": "served-m", "PROMPT": "describe these",
                       "API_URL": "http://api:80",
                       "MOD_TASK_TOKEN_URL": "http://worker/token",
                       "MOD_TASK_NONCE": "nonce-1"})
    os.environ.update(env or {})
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
    outdir = tmp / "out"
    blobs = {p.name: p.read_bytes() for p in sorted(outdir.glob("*"))} if outdir.is_dir() else {}
    return {"rc": rc, "stdout": out.getvalue(),
            "chat": [c for c in calls if not c["url"].endswith("/token")],
            "files": blobs}


def _last_json(stdout):
    return json.loads([ln for ln in stdout.splitlines() if ln.startswith("{")][-1])


def test_c1_model_empty_message_names_the_fix():
    res = _run([_ok()], env={"MODEL": ""})
    assert res["rc"] != 0
    err = _last_json(res["stdout"])["error"]
    assert err.startswith("MODEL is required:")
    assert "node's panel in the Workflow Builder" in err
    assert "Dashboard > Stacks" in err
    assert _last_json(res["stdout"])["code"] == "BAD_PARAM"
    assert res["chat"] == []          # fails before any HTTP call


def test_c2_model_not_served_by_any_stack():
    body = {"detail": {"error": "model not found", "model": "no-such-m",
                       "available_models": ["served-m", "other-m"]}}
    res = _run([_Resp(404, body)], env={"MODEL": "no-such-m"})
    assert res["rc"] != 0
    payload = _last_json(res["stdout"])
    assert payload["code"] == "MODEL_UNAVAILABLE"
    err = payload["error"]
    assert "not one your stacks serve" in err
    assert "Dashboard > Stacks" in err
    assert "available now: 'served-m', 'other-m'" in err
    assert "no-such-m" in err


def test_c2b_no_models_available_at_all():
    body = {"detail": {"error": "model not found", "model": "no-such-m",
                       "available_models": []}}
    res = _run([_Resp(404, body)], env={"MODEL": "no-such-m"})
    assert res["rc"] != 0
    assert "no model is available yet" in _last_json(res["stdout"])["error"]


def test_c3_stack_deployed_but_down():
    body = {"detail": "Model 'served-m' is not reachable right now "
                      "(endpoint unresolved is down or has no route). Start its stack and retry."}
    res = _run([_Resp(503, body)])
    assert res["rc"] != 0
    payload = _last_json(res["stdout"])
    assert payload["code"] == "MODEL_UNAVAILABLE"
    assert "start its stack on the Stacks page (Dashboard > Stacks)" in payload["error"]
    assert "endpoint" not in payload["error"]   # no backend endpoint internals leaked


def test_c4_prompt_empty_message_names_the_fix():
    res = _run([_ok()], env={"PROMPT": ""})
    assert res["rc"] != 0
    err = _last_json(res["stdout"])["error"]
    assert err.startswith("PROMPT is required:")
    assert "node's panel in the Workflow Builder" in err
    assert res["chat"] == []


def test_c5_success_path_unchanged():
    res = _run([_ok(reply="a red car")])
    assert res["rc"] == 0
    assert res["files"]["response.txt"] == b"a red car"
    assert json.loads(res["files"]["result.json"])["model"] == "served-m"
    assert res["stdout"].startswith("OK: 0 image(s) -> model served-m")


def test_c6_plain_errors_keep_the_old_raw_shape():
    """A non-model error (e.g. 429) still prints the untouched generic message."""
    res = _run([_Resp(429, {"detail": "LLM rate limit exceeded. Try again shortly."})])
    assert res["rc"] != 0
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    assert "LLM route returned HTTP 429" in _last_json(res["stdout"])["error"]


# --- FIX-STACK-MODEL-CALL-ENTRYPOINT-404-GUARD ---
# c7: the BLOCKING repro from REV b0489af46 item (3b): a plain FastAPI 404 body
# {"detail": "Not Found"} must NOT raise AttributeError ('str' object has no
# attribute 'get'); the node must print the generic clean JSON error naming the
# status and the bounded body. Then the table cases for every other shape.

def test_c7_string_detail_404_no_attribute_error():
    res = _run([_Resp(404, {"detail": "Not Found"})])
    assert res["rc"] != 0
    payload = _last_json(res["stdout"])
    assert payload["code"] == "PROCESSING_FAILURE"
    err = payload["error"]
    assert "LLM route returned HTTP 404" in err
    assert "Not Found" in err
    assert "AttributeError" not in res["stdout"]


def test_c7b_dict_detail_error_still_unbound_message():
    """The dict-shaped 404 the previous fix handled stays byte-identical."""
    body = {"detail": {"error": "model not found", "model": "no-such-m",
                       "available_models": ["served-m"]}}
    res = _run([_Resp(404, body)], env={"MODEL": "no-such-m"})
    assert res["rc"] != 0
    payload = _last_json(res["stdout"])
    assert payload["code"] == "MODEL_UNAVAILABLE"
    assert "not one your stacks serve" in payload["error"]
    assert "available now: 'served-m'" in payload["error"]


def test_c7c_list_detail_404_generic():
    res = _run([_Resp(404, {"detail": ["Not", "Found"]})])
    assert res["rc"] != 0
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    assert "LLM route returned HTTP 404" in _last_json(res["stdout"])["error"]


def test_c7d_null_detail_404_generic():
    res = _run([_Resp(404, {"detail": None})])
    assert res["rc"] != 0
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    assert "LLM route returned HTTP 404" in _last_json(res["stdout"])["error"]


def test_c7e_json_list_body_404_generic():
    res = _run([_Resp(404, ["Not", "Found"])])
    assert res["rc"] != 0
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    assert "LLM route returned HTTP 404" in _last_json(res["stdout"])["error"]


def test_c7f_nonjson_body_404_generic():
    """resp.json() raises ValueError -> data is None; the text path still reports."""
    res = _run([_Resp(404, None, text="Not Found at all")])
    assert res["rc"] != 0
    assert _last_json(res["stdout"])["code"] == "PROCESSING_FAILURE"
    assert "LLM route returned HTTP 404" in _last_json(res["stdout"])["error"]


def test_c7g_huge_body_message_is_bounded():
    res = _run([_Resp(404, {"detail": "x" * 9000})])
    assert res["rc"] != 0
    err = _last_json(res["stdout"])["error"]
    assert "LLM route returned HTTP 404" in err
    assert len(err) < 600          # BODY_ERR_CHARS = 500
