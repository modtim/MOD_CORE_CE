"""Tests for the static seal scan (declared-egress-and-sealed-nodes C22).

``scan`` must flag runtime network fetches in ENTRYPOINT/CMD and in
Python/shell sources, while leaving build-time Dockerfile ``RUN`` lines,
offline HF loads, and non-code files alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stages"))

from _seal_scan import Finding, hf_offline, scan  # noqa: E402

BASE_DF = "FROM python:3.11-slim\nCOPY . /app\nENTRYPOINT [\"python\", \"/app/main.py\"]\n"


def rules(findings):
    return sorted(f.rule for f in findings)


# ---------------------------------------------------------------------------
# Shell-command rules: positive in source, negative for look-alikes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rule,line", [
    ("pip-install", "pip install requests"),
    ("pip-install", "python -m pip --no-cache-dir install torch"),
    ("pip-install", "pip3 install -r requirements.txt"),
    ("apt-install", "apt-get -y install ffmpeg"),
    ("apt-install", "apt install curl-less-pkg"),
    ("apk-install", "apk --no-cache add git-lfs"),
    ("npm-install", "npm install left-pad"),
    ("npm-install", "npm ci"),
    ("git-clone", "git clone https://github.com/x/y"),
    ("curl", "curl -fsSL https://example.com/model.bin -o m.bin"),
    ("wget", "wget -q https://example.com/model.bin"),
])
def test_shell_rule_positive(rule, line):
    found = scan(BASE_DF, {"entrypoint.sh": f"#!/bin/sh\nset -e\n{line}\n"})
    assert rule in rules(found)
    f = next(f for f in found if f.rule == rule)
    assert f == Finding("entrypoint.sh", 3, rule, line)


@pytest.mark.parametrize("line", [
    "pip freeze > /tmp/reqs.txt",
    "apt-cache policy ffmpeg",
    "apk info",
    "npm run build",
    "git status",
    "echo libcurl is linked; pycurl_opts=1",
    "ls wget_cache/",
    "# pip install requests  (comment only)",
])
def test_shell_rule_negative(line):
    assert scan(BASE_DF, {"run.sh": f"#!/bin/sh\n{line}\n"}) == []


def test_python_subprocess_list_form_flagged():
    src = 'import subprocess\nsubprocess.run(["pip", "install", "x"])\nsubprocess.run(["git", "clone", url])\n'
    found = scan(BASE_DF, {"app/main.py": src})
    assert [(f.rule, f.line) for f in found] == [("pip-install", 2), ("git-clone", 3)]


def test_non_code_files_ignored():
    sources = {
        "README.md": "Run `pip install foo` and `git clone bar`.",
        "requirements.txt": "curl-cffi==0.5\n",
        "notes": "wget this (no shebang, not a script)",
    }
    assert scan(BASE_DF, sources) == []


# ---------------------------------------------------------------------------
# Dockerfile: RUN exempt, ENTRYPOINT/CMD scanned
# ---------------------------------------------------------------------------

def test_run_lines_are_build_time_and_exempt():
    df = (
        "FROM python:3.11-slim\n"
        "RUN apt-get update && apt-get install -y git curl \\\n"
        "    && pip install torch \\\n"
        "    && git clone https://github.com/x/y /opt/y\n"
        "RUN wget -q https://example.com/w.bin && npm ci\n"
        "CMD [\"python\", \"main.py\"]\n"
    )
    assert scan(df, {}) == []


def test_cmd_shell_form_flagged_with_line():
    df = "FROM alpine\nRUN apk add curl\nCMD pip install x && python main.py\n"
    assert scan(df, {}) == [Finding("Dockerfile", 3, "pip-install", "CMD pip install x && python main.py")]


def test_entrypoint_exec_form_continuation_flagged():
    df = (
        "FROM alpine\n"
        "ENTRYPOINT [\"sh\", \"-c\", \\\n"
        "  \"curl -o /m https://x/m && exec python main.py\"]\n"
    )
    found = scan(df, {})
    assert [(f.rule, f.line, f.path) for f in found] == [("curl", 3, "Dockerfile")]


def test_extra_dockerfile_in_sources_scanned_run_exempt():
    sources = {"gpu/Dockerfile.cuda": "FROM x\nRUN pip install y\nCMD git clone z && run\n"}
    found = scan(BASE_DF, sources)
    assert [(f.path, f.line, f.rule) for f in found] == [("gpu/Dockerfile.cuda", 3, "git-clone")]


# ---------------------------------------------------------------------------
# Hugging Face downloads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    'AutoModel.from_pretrained("bert-base-uncased")',
    'hf_hub_download(repo_id="x/y", filename="m.bin")',
    'snapshot_download("x/y")',
])
def test_hf_positive(call):
    found = scan(BASE_DF, {"main.py": f"import x\nm = {call}\n"})
    assert [(f.rule, f.line) for f in found] == [("hf-download", 2)]


def test_hf_local_files_only_exempt_even_multiline():
    src = (
        "tok = AutoTokenizer.from_pretrained('/models/bert', local_files_only=True)\n"
        "m = AutoModel.from_pretrained(\n"
        "    '/models/bert',\n"
        "    torch_dtype=dtype(),\n"
        "    local_files_only=True,\n"
        ")\n"
        "p = snapshot_download('x/y', local_files_only=True)\n"
    )
    assert scan(BASE_DF, {"main.py": src}) == []


def test_hf_local_files_only_false_still_flagged():
    src = "m = AutoModel.from_pretrained('x', local_files_only=False)\n"
    assert rules(scan(BASE_DF, {"main.py": src})) == ["hf-download"]


def test_hf_local_files_only_on_other_call_does_not_exempt():
    src = "a = AutoModel.from_pretrained('x')\nb = other(local_files_only=True)\n"
    assert rules(scan(BASE_DF, {"main.py": src})) == ["hf-download"]


@pytest.mark.parametrize("env_line", [
    "ENV HF_HUB_OFFLINE=1",
    "ENV HF_HUB_OFFLINE 1",
    "ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=\"true\"",
    "ENV A=1 \\\n    HF_HUB_OFFLINE=1",
])
def test_hf_hub_offline_env_exempts(env_line):
    df = f"FROM python:3.11-slim\n{env_line}\nCMD [\"python\", \"main.py\"]\n"
    assert hf_offline(df)
    src = "m = AutoModel.from_pretrained('x')\nhf_hub_download('x/y', 'f')\n"
    assert scan(df, {"main.py": src}) == []


@pytest.mark.parametrize("df", [
    "FROM x\nENV HF_HUB_OFFLINE=0\n",
    "FROM x\nRUN HF_HUB_OFFLINE=1 python warm.py\n",
    "FROM x\n# ENV HF_HUB_OFFLINE=1\n",
])
def test_hf_hub_offline_not_set(df):
    assert not hf_offline(df)
    assert rules(scan(df, {"main.py": "AutoModel.from_pretrained('x')\n"})) == ["hf-download"]


def test_hf_hub_offline_does_not_exempt_other_rules():
    df = "FROM x\nENV HF_HUB_OFFLINE=1\n"
    assert rules(scan(df, {"main.py": "import os\nos.system('pip install x')\n"})) == ["pip-install"]


# ---------------------------------------------------------------------------
# torch.hub.load
# ---------------------------------------------------------------------------

def test_torch_hub_load_positive():
    src = "import torch\nm = torch.hub.load('pytorch/vision', 'resnet50')\n"
    assert scan(BASE_DF, {"main.py": src}) == [
        Finding("main.py", 2, "torch-hub-load", "m = torch.hub.load('pytorch/vision', 'resnet50')")
    ]


def test_torch_hub_negative():
    src = (
        "import torch\n"
        "sd = torch.load('/models/w.pt')\n"
        "m = torch.hub.load('/opt/repo', 'resnet50', source='local')\n"
        "# m = torch.hub.load('pytorch/vision', 'resnet50')\n"
    )
    assert scan(BASE_DF, {"main.py": src}) == []


def test_clean_node_has_no_findings():
    df = (
        "FROM python:3.11-slim\n"
        "ENV HF_HUB_OFFLINE=1\n"
        "RUN pip install transformers && python -c \"from transformers import AutoModel; AutoModel.from_pretrained('x')\"\n"
        "COPY . /app\n"
        "ENTRYPOINT [\"/app/entrypoint.sh\"]\n"
    )
    sources = {
        "entrypoint.sh": "#!/bin/sh\nexec python /app/main.py \"$@\"\n",
        "main.py": "from transformers import AutoModel\nm = AutoModel.from_pretrained('x')\n",
    }
    assert scan(df, sources) == []


# ---------------------------------------------------------------------------
# Push stage integration: C23 seal scan wired into push_one_variant
# ---------------------------------------------------------------------------

import os
from unittest import mock


def _build_push_spec(hero_df: str, variant: str = "hero",
                     extra_findings: bool = False) -> dict:
    """A minimal build spec that the push stage will process."""
    base = {
        "spec_id": 1,
        "image_name": "test-image",
        "tenant_id": 1,
        "hero_dockerfile": hero_df,
        "source_files": [],
        "source_overrides_by_variant": {},
        "variants": [
            {"variant": variant, "dockerfile": hero_df},
        ],
    }
    if extra_findings:
        base["source_overrides_by_variant"] = {
            variant: [
                {"filename": "app.py", "content": (
                    "from huggingface_hub import snapshot_download\n"
                    "snapshot_download('x/y')\n"
                )},
            ],
        }
    return base


_PUSH_ENV = {
    "API_URL": "http://localhost",
    "API_KEY": "test-key",
    "BUILD_SPEC_ID": "1",
    "VARIANT": "hero",
    "HARBOR_URL": "harbor.local",
    "HARBOR_USERNAME": "admin",
    "HARBOR_PASSWORD": "secret",
}

HUNGRY_DF = (
    "FROM python:3.11-slim\n"
    "COPY . /app\n"
    'ENTRYPOINT ["python", "/app/app.py"]\n'
)

CLEAN_DF = (
    "FROM python:3.11-slim\n"
    "ENV HF_HUB_OFFLINE=1\n"
    "RUN pip install transformers\n"
    "COPY . /app\n"
    "ENTRYPOINT [\"python\", \"/app/main.py\"]\n"
)


def test_enforce_blocks_push_on_hf_snapshot_download():
    """Under enforce, a Dockerfile with runtime snapshot_download must NOT
    push — the push call is never made and the stage returns 1."""
    import push_one_variant as push_mod

    spec = _build_push_spec(HUNGRY_DF, extra_findings=True)

    with mock.patch.dict(os.environ, {**_PUSH_ENV, "MOD_SEALED_LINT": "enforce"}):
        with mock.patch.object(push_mod, "fetch_build_spec", return_value=spec):
            with mock.patch.object(push_mod, "report_back") as mock_report:
                with mock.patch("subprocess.run") as mock_run:
                    with mock.patch("pathlib.Path.mkdir"):
                        result = push_mod.main()

    # Push must NOT have been called (only docker inspect for diagnostic).
    # The seal scan gate returns 1 before reaching push code.
    assert result == 1

    # report_back must have been called once with findings, success=False.
    assert mock_report.called
    call_kwargs = mock_report.call_args
    assert call_kwargs[1]["success"] is False
    payload = call_kwargs[1]["payload"]
    assert len(payload["seal_findings"]) > 0
    assert payload["seal_findings"][0]["rule"] == "hf-download"
    assert payload["exit_code"] == 1


def test_enforce_clean_dockerfile_pushes():
    """A clean Dockerfile (no runtime fetches) passes seal scan under enforce
    and proceeds to push normally."""
    import push_one_variant as push_mod

    spec = _build_push_spec(CLEAN_DF)

    with mock.patch.dict(os.environ, {**_PUSH_ENV, "MOD_SEALED_LINT": "enforce"}):
        with mock.patch.object(push_mod, "fetch_build_spec", return_value=spec):
            with mock.patch.object(push_mod, "report_back") as mock_report:
                with mock.patch("subprocess.run") as mock_run:
                    with mock.patch("pathlib.Path.mkdir"):
                        mock_run.return_value = mock.MagicMock(
                            returncode=1,
                            stdout="",
                            stderr="",
                        )
                        push_mod.main()

    # Should have called report_back.
    assert mock_report.called
    call_kwargs = mock_report.call_args
    payload = call_kwargs[1]["payload"]
    # Clean scan — no seal findings attached.
    assert payload.get("seal_findings", []) == []


def test_warn_mode_reports_findings_and_pushes():
    """In warn mode, findings are reported and the push continues."""
    import push_one_variant as push_mod

    spec = _build_push_spec(HUNGRY_DF, extra_findings=True)

    with mock.patch.dict(os.environ, {**_PUSH_ENV, "MOD_SEALED_LINT": "warn"}):
        with mock.patch.object(push_mod, "fetch_build_spec", return_value=spec):
            with mock.patch.object(push_mod, "report_back") as mock_report:
                with mock.patch("subprocess.run") as mock_run:
                    with mock.patch("pathlib.Path.mkdir"):
                        # Make docker inspect find the tag → skip recovery
                        inspect_ok = mock.MagicMock(
                            returncode=0,
                            stdout="abc123 harbor.local/tenant-1/test-image:latest",
                            stderr="",
                        )
                        mock_run.return_value = inspect_ok
                        push_mod.main()

    # report_back must have been called with findings in the report.
    assert mock_report.called
    call_kwargs = mock_report.call_args
    assert call_kwargs[1]["success"] is True
    payload = call_kwargs[1]["payload"]
    assert len(payload["seal_findings"]) > 0


def test_unknown_mode_treated_as_warn():
    """An unknown MOD_SEALED_LINT value is treated as warn (findings logged
    but push continues — here: seal gate returns 0)."""
    import push_one_variant as push_mod

    spec = _build_push_spec(HUNGRY_DF, extra_findings=True)

    with mock.patch.dict(os.environ, {
        **_PUSH_ENV, "MOD_SEALED_LINT": "garbage_value",
    }):
        with mock.patch.object(push_mod, "fetch_build_spec", return_value=spec):
            with mock.patch.object(push_mod, "report_back") as mock_report:
                with mock.patch("subprocess.run") as mock_run:
                    with mock.patch("pathlib.Path.mkdir"):
                        inspect_ok = mock.MagicMock(
                            returncode=0,
                            stdout="abc123 harbor.local/tenant-1/test-image:latest",
                            stderr="",
                        )
                        mock_run.return_value = inspect_ok
                        push_mod.main()

    # Treated as warn: report back with findings + success, then return 0.
    assert mock_report.called
    call_kwargs = mock_report.call_args
    assert call_kwargs[1]["success"] is True
    assert len(call_kwargs[1]["payload"]["seal_findings"]) > 0
