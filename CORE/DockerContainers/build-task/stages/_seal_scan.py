"""Static seal scan for build-task stages.

A sealed node must not reach the network at runtime: everything it needs is
baked into the image at build time. ``scan`` is a pure function (no docker, no
I/O) that inspects a node's Dockerfile and source files for runtime fetches:

* package installs (pip / apt / apt-get / apk / npm), ``git clone``,
  ``curl`` / ``wget`` in ENTRYPOINT/CMD or in shell/Python sources;
* Hugging Face ``from_pretrained`` / ``hf_hub_download`` /
  ``snapshot_download`` calls, unless the call passes
  ``local_files_only=True`` or the Dockerfile sets ``ENV HF_HUB_OFFLINE=1``;
* ``torch.hub.load`` (unless ``source="local"``).

Dockerfile ``RUN`` lines execute at build time and are exempt.
"""
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    snippet: str


DOCKERFILE_PATH = "Dockerfile"

_SEP = r"""(?:["' \t,]|\\\n)+"""
_OPTS = rf"""(?:{_SEP}-{{1,2}}[\w=.-]+)*"""

_SHELL_RULES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("pip-install", re.compile(rf"\bpip(?:3(?:\.\d+)?)?\b{_OPTS}{_SEP}install\b")),
    ("apt-install", re.compile(rf"\bapt(?:-get)?\b{_OPTS}{_SEP}install\b")),
    ("apk-install", re.compile(rf"\bapk\b{_OPTS}{_SEP}(?:add|install)\b")),
    ("npm-install", re.compile(rf"\bnpm\b{_OPTS}{_SEP}(?:install|i|ci)\b")),
    ("git-clone", re.compile(rf"\bgit\b{_OPTS}{_SEP}clone\b")),
    ("curl", re.compile(r"(?<![\w.-])curl(?![\w.-])")),
    ("wget", re.compile(r"(?<![\w.-])wget(?![\w.-])")),
]

_HF_CALL = re.compile(r"\b(from_pretrained|hf_hub_download|snapshot_download)\s*\(")
_LOCAL_ONLY = re.compile(r"\blocal_files_only\s*=\s*True\b")
_TORCH_HUB = re.compile(r"\btorch\.hub\.load\s*\(")
_TORCH_LOCAL = re.compile(r"""\bsource\s*=\s*["']local["']""")

_OFFLINE_TRUE = {"1", "true", "yes", "on"}
_SCRIPT_EXTS = (".py", ".sh", ".bash")


def _logical_instructions(dockerfile: str) -> Iterator[Tuple[int, str, str]]:
    """Yield (start_line, INSTRUCTION, body) with ``\\`` continuations joined
    (newlines kept inside body so offsets map back to physical lines)."""
    lines = dockerfile.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        start = i + 1
        parts = [raw]
        while parts[-1].rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            parts.append(lines[i])
        i += 1
        text = "\n".join(parts).lstrip()
        m = re.match(r"(\w+)\s*", text)
        if not m:
            continue
        yield start, m.group(1).upper(), text


def hf_offline(dockerfile: str) -> bool:
    """True when the Dockerfile sets ``ENV HF_HUB_OFFLINE`` to a truthy value."""
    for _, instr, text in _logical_instructions(dockerfile):
        if instr != "ENV":
            continue
        body = text[3:].replace("\\\n", " ").strip()
        kv = re.findall(r"""\bHF_HUB_OFFLINE\s*=\s*["']?([^\s"']*)""", body)
        if not kv:
            legacy = re.match(r"""HF_HUB_OFFLINE\s+["']?([^\s"']*)""", body)
            kv = [legacy.group(1)] if legacy else []
        if any(v.lower() in _OFFLINE_TRUE for v in kv):
            return True
    return False


def _call_args(text: str, open_paren: int) -> str:
    depth = 0
    for j in range(open_paren, len(text)):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren:j + 1]
    return text[open_paren:]


def _mask_comments(text: str) -> str:
    return "\n".join(" " * len(ln) if ln.lstrip().startswith("#") else ln for ln in text.split("\n"))


def _line_at(text: str, offset: int) -> Tuple[int, str]:
    line_no = text.count("\n", 0, offset)
    return line_no, text.split("\n")[line_no].strip()[:200]


def _scan_text(path: str, text: str, base_line: int, offline: bool) -> List[Finding]:
    masked = _mask_comments(text)
    hits: List[Tuple[int, str]] = []
    for rule, pat in _SHELL_RULES:
        hits += [(m.start(), rule) for m in pat.finditer(masked)]
    for m in _HF_CALL.finditer(masked):
        if offline:
            continue
        if _LOCAL_ONLY.search(_call_args(masked, m.end() - 1)):
            continue
        hits.append((m.start(), "hf-download"))
    for m in _TORCH_HUB.finditer(masked):
        if _TORCH_LOCAL.search(_call_args(masked, m.end() - 1)):
            continue
        hits.append((m.start(), "torch-hub-load"))

    out: List[Finding] = []
    seen = set()
    for offset, rule in sorted(hits):
        rel, snippet = _line_at(text, offset)
        key = (rel, rule)
        if key in seen:
            continue
        seen.add(key)
        out.append(Finding(path, base_line + rel, rule, snippet))
    return out


def _scan_dockerfile(path: str, dockerfile: str, offline: bool) -> List[Finding]:
    out: List[Finding] = []
    for start, instr, text in _logical_instructions(dockerfile):
        if instr in ("ENTRYPOINT", "CMD"):
            out += _scan_text(path, text, start, offline)
    return out


def _is_dockerfile(path: str) -> bool:
    base = os.path.basename(path)
    return base.startswith("Dockerfile") or base.endswith(".dockerfile")


def _is_script(path: str, text: str) -> bool:
    if path.endswith(_SCRIPT_EXTS):
        return True
    return "." not in os.path.basename(path) and text.startswith("#!")


def scan(dockerfile: str, sources: Dict[str, str], dockerfile_path: Optional[str] = None) -> List[Finding]:
    """Return runtime-fetch findings for a node's Dockerfile + sources.

    ``sources`` maps relative path -> file text. Python and shell sources
    (``.py``/``.sh``/``.bash`` or extension-less files with a shebang) are
    scanned; other files are ignored. Extra Dockerfiles in ``sources`` are
    scanned like the main one (ENTRYPOINT/CMD only).
    """
    offline = hf_offline(dockerfile)
    findings = _scan_dockerfile(dockerfile_path or DOCKERFILE_PATH, dockerfile, offline)
    for path in sorted(sources):
        text = sources[path]
        if _is_dockerfile(path):
            findings += _scan_dockerfile(path, text, offline or hf_offline(text))
        elif _is_script(path, text):
            findings += _scan_text(path, text, 1, offline)
    return findings
