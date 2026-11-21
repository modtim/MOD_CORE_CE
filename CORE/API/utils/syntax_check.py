"""Dead-simple syntax pre-check for user-submitted wizard inputs.

Called from `POST /node_definitions/check-syntax` (fast, sync) and
optionally from `/analyze-script` (so we short-circuit the LLM call
when the user's script has a parse error).

Returns a uniform shape:
    { "ok": bool, "error": str, "line": int | None }

Non-Python/Dockerfile/bash content just returns ok=True — we only check
what we can cheaply check.
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from typing import Optional


def _result(ok: bool, error: str = "", line: Optional[int] = None) -> dict:
    return {"ok": ok, "error": error, "line": line}


def check_python(body: str) -> dict:
    """Compile the script. Returns the SyntaxError line + message if any."""
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    try:
        ast.parse(body)
    except SyntaxError as e:
        msg = e.msg or "SyntaxError"
        # e.lineno is 1-indexed; None for some edge cases.
        return _result(False, msg, e.lineno)
    return _result(True)


# Dockerfile directives we recognise. Anything else on a non-blank,
# non-comment line is flagged. Not a full Dockerfile spec — this is a
# "stupid simple" check per the user's ask.
_DF_DIRECTIVES = {
    "FROM", "RUN", "CMD", "LABEL", "MAINTAINER", "EXPOSE", "ENV",
    "ADD", "COPY", "ENTRYPOINT", "VOLUME", "USER", "WORKDIR", "ARG",
    "ONBUILD", "STOPSIGNAL", "HEALTHCHECK", "SHELL",
}


def check_dockerfile(body: str) -> dict:
    """Must contain at least one FROM directive. Flags bogus directives
    and reports the first line number that looks off."""
    if not body or not body.strip():
        return _result(False, "Dockerfile is empty", 1)

    saw_from = False
    in_continuation = False

    for i, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()
        if in_continuation:
            in_continuation = line.endswith("\\")
            continue
        if not line or line.startswith("#"):
            continue
        # Parser-directive line (e.g., `# syntax=`) starts with `#` already handled.
        first_word = line.split(None, 1)[0].upper()
        if first_word == "FROM":
            saw_from = True
        elif first_word not in _DF_DIRECTIVES:
            return _result(
                False,
                f"Unknown directive {first_word!r}. Expected one of: FROM, RUN, COPY, etc.",
                i,
            )
        # Track backslash continuation so `RUN foo \` + `  bar` counts as one line.
        in_continuation = line.endswith("\\")

    if not saw_from:
        return _result(False, "Dockerfile must have a FROM directive", 1)
    return _result(True)


def check_bash(body: str) -> dict:
    """bash -n does a no-exec syntax check. Returns stderr line on fail."""
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    if shutil.which("bash") is None:
        return _result(True)  # nothing to check with
    try:
        proc = subprocess.run(
            ["bash", "-n"],
            input=body.encode("utf-8", errors="replace"),
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return _result(False, "bash -n timed out", None)
    if proc.returncode == 0:
        return _result(True)
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    # bash errors look like: "bash: line N: ..."
    m = re.search(r"line\s+(\d+)", err)
    line = int(m.group(1)) if m else None
    return _result(False, err[:500] or "syntax error", line)


def _run_toolchain_check(
    cmd: list[str],
    body: str,
    *,
    uses_stdin: bool = True,
    line_regex: str = r"(?:line\s+|:)(\d+)",
) -> dict:
    """Shared shell-out pattern used by every toolchain-based checker:
    ruby -c, php -l, perl -c, etc. `cmd` is the binary + args; `body`
    is piped on stdin when uses_stdin=True, otherwise written to a
    temp file and the file path appended to cmd.

    Falls through to ok=True when the toolchain isn't installed —
    matches the design rule "never block on things we can't check
    cheaply." The wizard is allowed to surface obvious errors;
    silence beats false positives from a missing interpreter.
    """
    bin_name = cmd[0]
    if shutil.which(bin_name) is None:
        return _result(True)

    import tempfile
    input_bytes = body.encode("utf-8", errors="replace")
    if uses_stdin:
        try:
            proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            return _result(False, f"{bin_name} timed out", None)
    else:
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=True) as f:
            f.write(input_bytes)
            f.flush()
            try:
                proc = subprocess.run(cmd + [f.name], capture_output=True, timeout=5)
            except subprocess.TimeoutExpired:
                return _result(False, f"{bin_name} timed out", None)

    if proc.returncode == 0:
        return _result(True)
    err = (proc.stderr or b"").decode("utf-8", errors="replace").strip() \
        or (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    m = re.search(line_regex, err)
    line = int(m.group(1)) if m else None
    return _result(False, err[:500] or "syntax error", line)


def check_node(body: str) -> dict:
    """node --check parses a file without executing."""
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(["node", "--check", "-"], body, uses_stdin=True)


def check_typescript(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    # tsc supports `--noEmit` but not stdin; write a temp .ts file.
    return _run_toolchain_check(
        ["tsc", "--noEmit", "--strict", "false"], body, uses_stdin=False,
        line_regex=r":(\d+):\d+",
    )


def check_ruby(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(["ruby", "-c", "-"], body, uses_stdin=True)


def check_php(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(["php", "-l"], body, uses_stdin=False)


def check_perl(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(["perl", "-c", "-"], body, uses_stdin=True)


def check_lua(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(["luac", "-p"], body, uses_stdin=False)


def check_go(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    # gofmt exits non-zero on parse errors; -e reports all.
    return _run_toolchain_check(["gofmt", "-e"], body, uses_stdin=True,
                                 line_regex=r":(\d+):")


def check_rust(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(
        ["rustc", "--edition=2021", "--emit=metadata", "-o", "/dev/null"],
        body, uses_stdin=False, line_regex=r":(\d+):\d+",
    )


def check_c(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(
        ["gcc", "-x", "c", "-fsyntax-only", "-"], body, uses_stdin=True,
        line_regex=r":(\d+):\d+",
    )


def check_cpp(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    return _run_toolchain_check(
        ["g++", "-x", "c++", "-fsyntax-only", "-"], body, uses_stdin=True,
        line_regex=r":(\d+):\d+",
    )


def check_java(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    # javac wants a .java file whose name matches the public class.
    # That's too brittle for a quick syntax check; skip graceful.
    if shutil.which("javac") is None:
        return _result(True)
    import tempfile, os as _os
    with tempfile.TemporaryDirectory() as td:
        path = _os.path.join(td, "Scratch.java")
        with open(path, "w") as f:
            f.write(body)
        try:
            proc = subprocess.run(
                ["javac", "-d", td, path], capture_output=True, timeout=8,
            )
        except subprocess.TimeoutExpired:
            return _result(False, "javac timed out", None)
        if proc.returncode == 0:
            return _result(True)
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        m = re.search(r":(\d+):", err)
        return _result(False, err[:500] or "syntax error",
                       int(m.group(1)) if m else None)


def check_r(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Script is empty", 1)
    if shutil.which("R") is None:
        return _result(True)
    # Use `R -e 'parse(...)'` to catch parse errors without execution.
    import tempfile, os as _os
    with tempfile.TemporaryDirectory() as td:
        path = _os.path.join(td, "scratch.R")
        with open(path, "w") as f:
            f.write(body)
        try:
            proc = subprocess.run(
                ["R", "--no-save", "--no-restore", "-e", f"parse('{path}')"],
                capture_output=True, timeout=5,
            )
        except subprocess.TimeoutExpired:
            return _result(False, "R parse timed out", None)
        if proc.returncode == 0:
            return _result(True)
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return _result(False, err[:500] or "syntax error", None)


# -----------------------------------------------------------------------
# Data-format checkers (always work — no toolchain needed, pure stdlib).
# -----------------------------------------------------------------------

def check_json(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Document is empty", 1)
    import json as _json
    try:
        _json.loads(body)
    except _json.JSONDecodeError as e:
        return _result(False, e.msg, e.lineno)
    return _result(True)


def check_yaml(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Document is empty", 1)
    try:
        import yaml
    except ImportError:
        return _result(True)
    try:
        yaml.safe_load(body)
    except yaml.YAMLError as e:
        line = None
        if hasattr(e, "problem_mark") and e.problem_mark is not None:
            line = e.problem_mark.line + 1
        return _result(False, str(e)[:500], line)
    return _result(True)


def check_toml(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Document is empty", 1)
    try:
        import tomllib  # type: ignore
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return _result(True)
    try:
        tomllib.loads(body)
    except Exception as e:  # tomllib.TOMLDecodeError on 3.11+
        m = re.search(r"line\s+(\d+)", str(e))
        line = int(m.group(1)) if m else None
        return _result(False, str(e)[:500], line)
    return _result(True)


def check_xml(body: str) -> dict:
    if not body or not body.strip():
        return _result(False, "Document is empty", 1)
    from xml.etree import ElementTree as ET
    try:
        ET.fromstring(body)
    except ET.ParseError as e:
        line = None
        if hasattr(e, "position") and e.position:
            line = e.position[0]
        return _result(False, str(e)[:500], line)
    return _result(True)


# Top-20-by-popularity language dispatch. Every entry either:
#   - runs a pure-stdlib checker (Python, JSON/YAML/TOML/XML, Dockerfile)
#   - or shells out to a toolchain and falls through to ok=True on
#     missing-binary (ruby, php, perl, lua, go, rust, c, c++, java, R,
#     node, typescript, bash).
#
# Shell / Bash includes sh / zsh (bash -n accepts the syntax of all
# three well enough for our "obvious errors only" bar).
#
# Languages we intentionally do NOT cover because they need full
# project context to meaningfully parse (C# / Swift / Kotlin / Scala /
# Dart / MATLAB / SQL): no check is better than false-positive.
_DISPATCH = {
    "python": check_python,
    "python3": check_python,
    "py": check_python,

    "bash": check_bash,
    "sh": check_bash,
    "zsh": check_bash,
    "shell": check_bash,

    "javascript": check_node,
    "js": check_node,
    "node": check_node,
    "nodejs": check_node,

    "typescript": check_typescript,
    "ts": check_typescript,

    "ruby": check_ruby,
    "rb": check_ruby,

    "php": check_php,
    "perl": check_perl,
    "pl": check_perl,
    "lua": check_lua,

    "go": check_go,
    "golang": check_go,

    "rust": check_rust,
    "rs": check_rust,

    "c": check_c,
    "cpp": check_cpp,
    "c++": check_cpp,
    "cxx": check_cpp,

    "java": check_java,
    "r": check_r,

    # Data formats (always work — stdlib only).
    "json": check_json,
    "yaml": check_yaml,
    "yml": check_yaml,
    "toml": check_toml,
    "xml": check_xml,
}


def check_source(source_type: str, interpreter: str, body: str) -> dict:
    """Dispatch to the right checker based on the wizard source type +
    interpreter. Unknown combinations → ok=True (design rule: never
    block on things we can't check cheaply)."""
    if source_type == "dockerfile":
        return check_dockerfile(body)
    if source_type == "script":
        fn = _DISPATCH.get((interpreter or "").lower())
        if fn is not None:
            return fn(body)
    # repo / image / unknown interpreter: pass through
    return _result(True)


SUPPORTED_INTERPRETERS = sorted({
    "python3", "bash", "node", "typescript", "ruby", "php", "perl",
    "lua", "go", "rust", "c", "cpp", "java", "r",
    "json", "yaml", "toml", "xml",
})
