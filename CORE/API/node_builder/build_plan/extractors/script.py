"""Script-mode BuildPlan extractor (wizard-rebuild §3 chunk 3).

User pastes a script body (Python / Node / Bash). The extractor:

  1. Detects the interpreter (shebang first, declared interpreter
     second, default python3).
  2. Picks a curated base via `node_builder/base_detector.py` (already
     handles the GPU-framework upgrade rule). User can override with
     `wrap_base_image`.
  3. For Python bodies, heuristically extracts top-level imports and
     maps them to pip install steps (stdlib filtered out via a frozen
     allowlist; known aliases mapped — `cv2` → `opencv-python`,
     `PIL` → `Pillow`, etc.).
  4. Emits a BuildPlan with `mode="script"`, `script_body=<the body>`,
     `script_filename="script.py"` (or .js / .sh), and an entrypoint
     that invokes the script.

Confidence:
  - High (0.80) when interpreter is unambiguous AND we extracted
    install_steps cleanly from imports we recognize.
  - Medium (0.55) when interpreter inferred but no deps or deps
    contain unrecognized names (might be transitive).
  - Low (0.30) when interpreter had to be guessed (no shebang, no
    declared interpreter, no Python markers).

No LLM call. No network call. Deterministic.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Optional

from ..types import (
    EXTRACTOR_VERSION,
    BuildPlan,
    EnvVar,
    default_ports_for_types,
)

logger = logging.getLogger(__name__)


# Mapping (import-name → pip-package-name) for the cases where they
# differ. Anything not in this map and not in _PYTHON_STDLIB falls
# through as `pip install <import-name>` (which is correct ~85% of
# the time for pip packages whose Python name matches their distro
# name). Repair flow corrects the rest at runtime.
_KNOWN_PIP_ALIASES: dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary",
    "google.cloud": "google-cloud",
}


# Curated apt packages a pip package needs at RUNTIME (the LLM Containerizer
# "just knew" these; the deterministic path needs the table). Keyed by the
# RESOLVED pip name (post-alias). Populating plan.system_deps up front means
# the common "libGL.so.1: cannot open shared object file" / "libsndfile not
# found" classes build first-try instead of costing a repair-loop round trip.
# Conservative — only well-known, non-GPL runtime libs.
_SYSTEM_DEPS_FOR_PIP: dict[str, list[str]] = {
    "opencv-python":          ["libgl1", "libglib2.0-0"],
    "opencv-contrib-python":  ["libgl1", "libglib2.0-0"],
    "easyocr":                ["libgl1", "libglib2.0-0"],
    "pyzbar":                 ["libzbar0"],
    "soundfile":              ["libsndfile1"],
    "librosa":                ["libsndfile1", "ffmpeg"],
    "pydub":                  ["ffmpeg"],
    "moviepy":                ["ffmpeg"],
    "imageio-ffmpeg":         ["ffmpeg"],
    "pytesseract":            ["tesseract-ocr"],
    "cairosvg":               ["libcairo2"],
    "mysqlclient":            ["default-libmysqlclient-dev", "gcc"],
    "dlib":                   ["build-essential", "cmake"],
    "face-recognition":       ["build-essential", "cmake"],
    "face_recognition":       ["build-essential", "cmake"],
}


def _infer_system_deps(pip_deps: list[str]) -> list[str]:
    """Map resolved pip packages → the apt packages they need at runtime.
    Deterministic mirror of what the LLM Containerizer inferred by hand. The
    §7 repair loop still backfills anything we miss, so this is a
    first-try-success optimization, not a correctness dependency."""
    out: set[str] = set()
    for pkg in pip_deps:
        out.update(_SYSTEM_DEPS_FOR_PIP.get(pkg.lower(), ()))
    return sorted(out)


# Python 3.11 stdlib top-level module names. Built from
# sys.stdlib_module_names + a few common __future__/typing aliases.
# We don't import sys.stdlib_module_names at runtime because it's
# version-specific; baking the snapshot in keeps extraction
# deterministic across deployments.
_PYTHON_STDLIB: frozenset[str] = frozenset({
    "__future__", "_thread", "abc", "aifc", "argparse", "array",
    "ast", "asynchat", "asyncio", "asyncore", "atexit", "audioop",
    "base64", "bdb", "binascii", "bisect", "builtins", "bz2",
    "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code",
    "codecs", "codeop", "collections", "colorsys", "compileall",
    "concurrent", "configparser", "contextlib", "contextvars",
    "copy", "copyreg", "crypt", "csv", "ctypes", "curses",
    "dataclasses", "datetime", "dbm", "decimal", "difflib", "dis",
    "distutils", "doctest", "email", "encodings", "ensurepip",
    "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
    "fnmatch", "fractions", "ftplib", "functools", "gc", "genericpath",
    "getopt", "getpass", "gettext", "glob", "graphlib", "grp",
    "gzip", "hashlib", "heapq", "hmac", "html", "http", "idlelib",
    "imaplib", "imghdr", "imp", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
    "locale", "logging", "lzma", "mailbox", "mailcap", "marshal",
    "math", "mimetypes", "mmap", "modulefinder", "msilib", "msvcrt",
    "multiprocessing", "netrc", "nis", "nntplib", "ntpath", "numbers",
    "operator", "optparse", "os", "ossaudiodev", "parser", "pathlib",
    "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
    "plistlib", "poplib", "posix", "posixpath", "pprint", "profile",
    "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "pyexpat",
    "queue", "quopri", "random", "re", "readline", "reprlib",
    "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
    "selectors", "shelve", "shlex", "shutil", "signal", "site",
    "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
    "sqlite3", "ssl", "stat", "statistics", "string", "stringprep",
    "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig",
    "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile",
    "termios", "test", "textwrap", "threading", "time", "timeit",
    "tkinter", "token", "tokenize", "tomllib", "trace", "traceback",
    "tracemalloc", "tty", "turtle", "turtledemo", "types", "typing",
    "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "winreg", "winsound",
    "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile",
    "zipimport", "zlib", "zoneinfo",
})


# Tokens that disambiguate an unclear body toward "Python" — used as
# a tiebreaker when there's no shebang and no declared interpreter.
_PY_HINTS_RE = re.compile(
    r"^(?:import|from)\s+\w|^def\s+\w|^class\s+\w|print\s*\(",
    re.MULTILINE,
)


# Shebang sniff. Captures up to two whitespace-delimited tokens so
# we can handle both forms:
#   #!/usr/bin/python3        → group(1) = "/usr/bin/python3"
#   #!/usr/bin/env python3    → group(1) = "/usr/bin/env",
#                                group(2) = "python3"
# Whichever token actually names the interpreter (python / node /
# bash) wins.
_SHEBANG_RE = re.compile(r"^#!\s*(\S+)(?:\s+(\S+))?")


def extract_script_plan(
    *,
    script_body: str,
    node_name: str,
    input_type: str = "image",
    output_type: str = "image",
    interpreter: Optional[str] = None,
    wrap_base_image: Optional[str] = None,
    container_env: Optional[dict[str, str]] = None,
    system_packages: Optional[list[str]] = None,
) -> BuildPlan:
    """Build a `BuildPlan` for a Script-mode submission.

    Args:
      script_body: the literal source code the user pasted.
      node_name: human-readable Node display name.
      input_type / output_type: legacy singleton port types.
      interpreter: optional explicit interpreter ("python3" /
        "bash" / "node"). When unset, we sniff the shebang.
      wrap_base_image: optional override of the curated base. When
        set, takes precedence over the detector's pick.
      container_env: env vars from the wizard's Configure step.
      system_packages: EXPLICIT apt packages the caller declared (a pinned
        recipe's ``system_packages``, or the synthesizer's declared binaries).
        Unioned into ``system_deps`` on top of pip→apt inference — a binary the
        script only shells out to (colmap, ffmpeg) is invisible to import
        scanning, so without this channel it never gets apt-installed and the
        built node fails at runtime with "command not found".

    Returns:
      A BuildPlan with `mode="script"`, populated language /
      `from_image_ref` / `install_steps` / `script_body` /
      `script_filename` / `entrypoint`. Whatever couldn't be
      inferred gets added to `weak_fields` so §7 repair knows
      to look there first.

    Raises:
      ValueError if `script_body` is empty or whitespace-only.
    """
    if not script_body or not script_body.strip():
        raise ValueError("script_body must be non-empty")

    weak_fields: list[str] = []

    # Step 1: detect interpreter. Explicit > shebang > heuristic.
    detected_interp, interp_source = _detect_interpreter(
        interpreter, script_body,
    )
    if interp_source == "guess":
        weak_fields.append("language")

    language, ext = _interpreter_to_language(detected_interp)

    # Step 2: pick base. User override > curated.
    from_image_ref: Optional[str]
    if wrap_base_image:
        from_image_ref = wrap_base_image
    else:
        from_image_ref = _curated_base_for(detected_interp, script_body)
        if from_image_ref is None:
            weak_fields.append("from_image_ref")

    # Step 3: extract pip deps (Python only for v1).
    install_steps: list[str] = []
    system_deps: list[str] = []
    package_manager = "pip" if language == "python" else "none"
    if language == "python":
        deps = _extract_python_imports(script_body)
        system_deps = _infer_system_deps(deps)
        if deps:
            # One pip install per group keeps the install RUN
            # deterministic and lint-clean (one RUN, sorted args).
            install_steps.append(
                "pip install --no-cache-dir " + " ".join(sorted(deps))
            )
        else:
            weak_fields.append("install_steps")
    elif language in ("node", "shell"):
        # No automatic dep extraction yet. Repair flow surfaces
        # missing-module errors and adds installs.
        weak_fields.append("install_steps")

    # Fold in EXPLICIT apt packages the caller declared (recipe / synthesizer),
    # regardless of language — inference only covers pip→apt runtime deps, so a
    # binary the script shells out to (e.g. colmap) is only apt-installed if the
    # caller declared it here.
    if system_packages:
        _merged = set(system_deps)
        _merged.update(
            p.strip() for p in system_packages
            if isinstance(p, str) and p.strip()
        )
        system_deps = sorted(_merged)

    # Step 4: pick filename + entrypoint based on language.
    script_filename = f"script{ext}"
    workdir = "/app"
    entrypoint = _entrypoint_for(language, workdir, script_filename)

    in_ports, out_ports = default_ports_for_types(input_type, output_type)

    env_vars: list[EnvVar] = []
    if container_env:
        for name, default in container_env.items():
            if not name:
                continue
            env_vars.append(EnvVar(
                name=name,
                required=True,
                default=str(default) if default is not None else None,
            ))

    # Confidence tier — see module docstring.
    if interp_source != "guess" and install_steps:
        confidence = 0.80
    elif interp_source != "guess":
        confidence = 0.55
    else:
        confidence = 0.30

    plan = BuildPlan(
        mode="script",
        language=language,
        package_manager=package_manager,
        workdir=workdir,
        entrypoint=entrypoint,

        from_image_ref=from_image_ref,
        system_deps=system_deps,
        install_steps=install_steps,
        env_vars=env_vars,

        input_ports=in_ports,
        output_ports=out_ports,

        script_body=script_body,
        script_filename=script_filename,

        confidence=confidence,
        weak_fields=weak_fields,
        extractor_version=EXTRACTOR_VERSION,
    )
    return plan


# ---------------------------------------------------------------------------
# Interpreter detection
# ---------------------------------------------------------------------------

def _detect_interpreter(
    declared: Optional[str], body: str,
) -> tuple[str, str]:
    """Return (interpreter, source) where source is one of
    "declared" / "shebang" / "heuristic" / "guess"."""
    declared_norm = (declared or "").strip().lower()
    if declared_norm in ("python", "python3", "py"):
        return ("python3", "declared")
    if declared_norm in ("node", "nodejs", "js"):
        return ("node", "declared")
    if declared_norm in ("bash", "sh", "shell"):
        return ("bash", "declared")

    m = _SHEBANG_RE.match(body or "")
    if m:
        # If the first token is `/usr/bin/env` (or similar), the
        # interpreter name is in the second token. Otherwise the
        # interpreter is at the tail of the first token.
        first, second = m.group(1), m.group(2)
        candidate = second if first.endswith("/env") and second else first
        if "python" in candidate:
            return ("python3", "shebang")
        if "node" in candidate:
            return ("node", "shebang")
        if "bash" in candidate or candidate.endswith("/sh") or candidate == "sh":
            return ("bash", "shebang")

    # Body-pattern heuristic. Python imports / def / class / print()
    # are the most reliable single-language signal in pasted snippets.
    if _PY_HINTS_RE.search(body or ""):
        return ("python3", "heuristic")

    # No signal at all — fall back to Python (overwhelming majority
    # of MOD wizard submissions). Tag in weak_fields so repair knows.
    return ("python3", "guess")


def _interpreter_to_language(interp: str) -> tuple[str, str]:
    """Map interpreter → (BuildPlan.language, filename extension)."""
    if interp == "python3":
        return ("python", ".py")
    if interp == "node":
        return ("node", ".js")
    if interp == "bash":
        return ("shell", ".sh")
    return ("python", ".py")


def _curated_base_for(interp: str, body: str) -> Optional[str]:
    """Resolve the curated base image ref for a given interpreter,
    deferring to base_detector for the Python+CUDA upgrade rule."""
    try:
        from node_builder.base_detector import detect_from_script
        detected = detect_from_script(interp, body)
        return detected.image_ref
    except Exception as exc:
        logger.warning(
            "extract_script_plan: curated-base lookup failed for "
            "interp=%r: %s (continuing without from_image_ref)",
            interp, exc,
        )
        return None


def _entrypoint_for(
    language: str, workdir: str, script_filename: str,
) -> list[str]:
    """Entrypoint exec-form list per language."""
    script_path = f"{workdir}/{script_filename}".replace("//", "/")
    if language == "python":
        return ["python", script_path]
    if language == "node":
        return ["node", script_path]
    if language == "shell":
        return ["/bin/sh", script_path]
    return ["/bin/sh", script_path]


# ---------------------------------------------------------------------------
# Python import extraction
# ---------------------------------------------------------------------------

def _extract_python_imports(body: str) -> list[str]:
    """Walk the body's AST and return the unique pip-install names.

    Stdlib modules filtered out. Known aliases (cv2 → opencv-python,
    PIL → Pillow, etc.) mapped via _KNOWN_PIP_ALIASES. Unknown names
    fall through verbatim (correct for most pip packages whose Python
    module name == distro name).

    Returns an empty list if the body isn't valid Python — caller
    handles confidence demotion.
    """
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return []

    deps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _PYTHON_STDLIB:
                    continue
                deps.add(_KNOWN_PIP_ALIASES.get(root, root))
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (`from .foo import bar`) have no
            # module; skip them — they reference local files, not
            # pip packages.
            if node.level and not node.module:
                continue
            mod = (node.module or "").split(".", 1)[0]
            if not mod or mod in _PYTHON_STDLIB:
                continue
            deps.add(_KNOWN_PIP_ALIASES.get(mod, mod))

    return sorted(deps)
