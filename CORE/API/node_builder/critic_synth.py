"""Critic D — pre-Review semantic + feasibility review of a synthesized node.

Runs INSIDE ``describe_synthesize.synthesize_wrapper``'s loop, AFTER the
structural validators pass, so problems are caught before the script ever
reaches the user's Review step (and long before an expensive build). Two
layers, cheap-deterministic first:

  1. MECHANICAL (no LLM, deterministic, high-confidence) — runtime pitfalls the
     structural validators miss. Auto-fixed by feeding the finding back into the
     coder's existing retry loop (regenerate).

  2. REASONING VERDICT (gpt-oss reasoner) — intent + feasibility. It judges
     whether the script does what the user asked and whether it can plausibly
     build/run. High-confidence mismatch -> regenerate with a fix hint;
     ambiguous / low-confidence -> advisory warning surfaced in Review (a future
     revision escalates ambiguity to a user question).

Design rules (Tim's): auto-fix mechanical, flag semantic, HIGH bar to auto-
change, and — above all — GRACEFUL DEGRADE: any critic error, timeout, or
unparseable verdict accepts the script as-is. The critic never blocks a build.

The reasoner is internal-only (vllm-big), which is fine because this runs in the
API process, not the worker sandbox. The coder (devstral) still does any
regeneration via the normal synth loop.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from utils.llm_client import LLMClientError

logger = logging.getLogger(__name__)

# Kill-switch. Graceful-degrade already makes the critic safe, but a hard off
# switch lets an operator disable the extra reasoning call instantly.
def _enabled() -> bool:
    if os.getenv("WIZARD_CRITIC_D_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        return False
    # Operator's Node Wizard code-review setting (Dashboard AI settings):
    # critic_mode == "off" disables the LLM review; "self"/"reasoner" keep it (the
    # model choice is handled by the critic tool's role resolution).
    try:
        # GT.P2.6c — `_wizard_critic_mode` is paid-only. On CE read the same
        # wizard `critic_mode` key through the CE shim (same Vault path).
        from utils.paid_import import optional_paid
        _ai_cfg = optional_paid("routers.ai_config")
        if _ai_cfg is not None:
            return _ai_cfg._wizard_critic_mode() != "off"
        from utils.tool_config import read_tool_config
        return (read_tool_config("wizard") or {}).get("critic_mode") != "off"
    except Exception:  # noqa: BLE001 — never let config lookup break the critic
        return True


# Only auto-regenerate on a mismatch we're at least this confident is real.
# High by design — we'd rather ship a plausibly-right script than churn a good
# one. Below this, the concern becomes an advisory warning, not a rewrite.
_MISMATCH_AUTOFIX_THRESHOLD = float(os.getenv("WIZARD_CRITIC_D_THRESHOLD", "0.85"))

# Generous completion budget — gpt-oss streams hidden chain-of-thought that
# counts against max_tokens BEFORE the answer, so a normal 4k budget can return
# empty. 8k leaves room for the reasoning + the small JSON verdict.
_VERDICT_MAX_TOKENS = 8192


@dataclass
class CritiqueResult:
    """What Critic D wants the synth loop to do."""
    # Findings to feed back into the coder loop -> triggers a regenerate.
    regenerate_reasons: List[str] = field(default_factory=list)
    # Advisory notes to surface in Review (non-blocking).
    warnings: List[str] = field(default_factory=list)
    # v2 — a single clarifying question to ASK the user when the intent is
    # genuinely ambiguous and we've NOT already asked. The synth loop returns
    # this to the wizard, which surfaces it in the describe chat; the answer is
    # folded back in as a refinement for a fresh synth. None => nothing to ask.
    clarifying_question: Optional[str] = None
    # Verbatim verdict for logging/telemetry.
    verdict: Optional[str] = None
    confidence: Optional[float] = None

    @property
    def wants_regen(self) -> bool:
        return bool(self.regenerate_reasons)


# ---------------------------------------------------------------------------
# Layer 1 — deterministic mechanical checks
# ---------------------------------------------------------------------------

def _dedup(items: List[str]) -> List[str]:
    """Order-preserving de-duplication of feedback strings."""
    seen: set = set()
    out: List[str] = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _mechanical_issues(script: str) -> List[str]:
    """Deterministic, high-confidence runtime-crash checks the structural
    validators don't cover. Each returned string is phrased as actionable
    coder feedback (fed into the regenerate loop)."""
    issues: List[str] = []
    s = script or ""

    # (a) JPEG/lossy save without an RGB conversion -> crashes on RGBA / palette
    #     (PNG-with-alpha, GIF) inputs with "cannot write mode RGBA as JPEG".
    uses_pil = bool(re.search(r"from\s+PIL\s+import|Image\.open\(", s))
    saves_jpeg = bool(re.search(
        r"""(?ix)
        \.save\([^)]*(?:["']jpe?g["']|\.jpe?g["'])      # .save(..., "JPEG") / "....jpg"
        | ["']JPE?G["']\.upper                            # OUTPUT_FORMAT ... == "JPEG"
        | getenv\(\s*["']OUTPUT_FORMAT["']\s*,\s*["']jpe?g["']   # default JPEG
        """, s,
    ))
    converts_rgb = bool(re.search(r"""\.convert\(\s*["']RGB["']\s*\)""", s))
    if uses_pil and saves_jpeg and not converts_rgb:
        issues.append(
            "The script saves images as JPEG but never converts them to RGB. "
            "JPEG cannot hold an alpha channel, so this crashes on RGBA/palette "
            "inputs (e.g. PNGs with transparency) with 'cannot write mode RGBA "
            "as JPEG'. Convert with img.convert('RGB') before saving as JPEG."
        )

    # (b) os.environ["X"] subscript with no default -> KeyError when unset.
    #     A node's settings must be OPTIONAL (os.getenv with a default), not
    #     required-or-crash.
    for m in re.finditer(r"""os\.environ\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]""", s):
        name = m.group(1)
        issues.append(
            f"The script reads os.environ['{name}'] with no default — it will "
            f"KeyError if that setting is unset. Use os.getenv('{name}', "
            f"<sensible-default>) so it's an optional, configurable setting."
        )

    # (c) Docker-in-Docker: shelling out to `docker run` / `docker build` /
    #     `docker-compose`. The node ALREADY runs inside a container in the
    #     worker sandbox with NO docker socket, so `docker` isn't available and
    #     this fails immediately. The tool must be installed DIRECTLY in the
    #     node's own image (system_packages / dockerfile_extras) and its binary
    #     called — never a nested container. Matches both the list form
    #     (["docker","run",...]) and the string form (os.system("docker run")).
    if (re.search(r"""["']docker["']\s*,\s*["'](?:run|build|pull|create|compose)["']""", s)
            or re.search(r"""(?:os\.system|subprocess\.[a-z_]+)\([^)]*["']\s*docker[\s-]+(?:run|build|pull|compose)""", s)
            or re.search(r"""["']docker-compose["']""", s)):
        issues.append(
            "The script shells out to `docker run`/`docker build`. This node "
            "already runs INSIDE a container in a sandbox with NO Docker socket, "
            "so `docker` is unavailable and this fails immediately (Docker-in-"
            "Docker is not supported). Install the tool directly in the node's "
            "image via system_packages / dockerfile_extras and call its binary "
            "directly — do NOT launch a nested container."
        )

    # De-dup while preserving order.
    seen: set = set()
    out: List[str] = []
    for i in issues:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ---------------------------------------------------------------------------
# Layer 1b — deterministic EXECUTABILITY checks ("will it actually run?")
# ---------------------------------------------------------------------------

def fatal_runtime_crashes(script: str) -> List[str]:
    """Deterministic, near-zero-false-positive checks for code that is CERTAIN to
    crash on its first run. Enforced as FATAL by the structural validator (so
    such a script is never shipped, even after the coder exhausts retries) and
    also reported by the exec-critic. Empty when the script parses and has none.

    Covers the exact classes a weak local model produces when it 'wraps' a tool
    from its docs (repro 2026-09-03, the hallucinated Meshroom wrapper):
      (c) `import pip` + programmatic pip use — pip has no public API.
      (d) shell operators (&&, ||, |, ;, >) as literal elements of a subprocess
          argument LIST — the list form runs no shell, so they break the command.
      (e) names used but never bound anywhere in the module — a guaranteed
          NameError (e.g. TARGET_WIDTH/SCALE used but never defined).
    """
    issues: List[str] = []
    s = script or ""

    # (a) runtime `pip install` — a shelled-out install at run time (esp.
    #     `pip install git+…` of a non-Python/C++ repo, the classic weak-model
    #     hallucination, repro 2026-09-03). Deps belong in requirements/
    #     system_packages, baked into the image.
    if re.search(r"""(?ix)
        (?:subprocess\.\w+\s*\(|os\.system\s*\()   # shelled out …
        [^)]*?pip['"\s,]+install                    # … to `pip install`
        """, s) or re.search(r"""(?ix)['"]pip['"]\s*,\s*['"]install['"]""", s):
        issues.append(
            "The script runs `pip install` at RUNTIME (a shelled-out install, "
            "often `pip install git+…` of a non-Python repo). This does not work "
            "in the node sandbox. Declare Python deps in `requirements` and "
            "system/C++ tools in `system_packages` / `dockerfile_extras`, baked "
            "into the image, then import or call them directly."
        )

    # (a3) WRAPPER BLEED — the model prints/builds the synthesis RESPONSE JSON
    #      (script_content/base_image_hint/suggested_input_type/…) inside the node
    #      script, usually a trailing json.dumps({...script_content...}) that
    #      NameErrors on the undefined script_content (repro 2026-09-03: the loop
    #      kept re-emitting this same line and stuck). Those keys are the FORMAT
    #      the coder returns to the wizard, never part of the node. >=2 keys => bleed.
    _wrapper_keys = ("script_content", "base_image_hint", "suggested_input_type",
                     "suggested_output_type", "dockerfile_extras")
    if sum(1 for k in _wrapper_keys
           if re.search(rf"['\"]{k}['\"]|(?<![\w]){k}(?![\w])", s)) >= 2:
        issues.append(
            "The script includes the synthesis RESPONSE fields (script_content, "
            "base_image_hint, suggested_input_type, …) — usually a trailing "
            "print(json.dumps({...})). That JSON is the FORMAT you return to the "
            "wizard; it must NOT appear inside the node script and script_content "
            "is undefined there (NameError). DELETE that line entirely — the "
            "script should ONLY read /data/input, do the work, and write "
            "/data/output."
        )

    # (a2) cwd-mangled contract path — /data/input and /data/output are ABSOLUTE
    #      mounts; prepending os.getcwd() (`getcwd() + '/data/input'`) points at
    #      <cwd>/data/input, which does not exist, so the node reads/writes the
    #      wrong place and silently finds no files (repro 2026-09-03).
    if re.search(r"""(?ix)getcwd\s*\(\s*\)\s*\+\s*["']/?data/""", s):
        issues.append(
            "The script prepends os.getcwd() to a /data path (e.g. "
            "getcwd() + '/data/input'). /data/input and /data/output are ABSOLUTE "
            "mount points — never join them to the working directory. Use them "
            "literally: Path('/data/input') and Path('/data/output')."
        )

    import ast as _ast
    try:
        tree = _ast.parse(s)
    except SyntaxError:
        return issues  # syntax errors are the structural validator's job

    # (b) group-alternation glob — Python glob does NOT expand `{jpg,png}` OR
    #     `(jpg,png)`; the pattern matches literally → ZERO files, node produces
    #     nothing (repro 2026-09-03). AST-based (a regex over the source
    #     false-matched across lines, e.g. `{ext}` in an f-string + a distant
    #     comma in cv2.cvtColor(img, ...)): only inspect real STRING LITERALS in
    #     a glob-family call, so f-string interpolation `f'*.{ext}'` is ignored.
    _group_re = re.compile(r"[{(][^{}()]*,[^{}()]*[)}]")

    def _str_literals(node: "_ast.AST"):
        """Yield plain str constants inside an arg (direct, or nested in calls
        like os.path.join(dir, '*.{jpg,png}')). f-string literal parts are
        yielded too, but their {…} interpolations are not part of the constant."""
        for n in _ast.walk(node):
            if isinstance(n, _ast.Constant) and isinstance(n.value, str):
                yield n.value

    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr in {"glob", "iglob", "rglob", "fnmatch", "filter"}):
            hit = any(_group_re.search(lit) for a in node.args for lit in _str_literals(a))
            if hit:
                issues.append(
                    "The glob pattern uses a group like `*.{jpg,png}` or "
                    "`*.(jpg,png)` — Python's glob does NOT expand braces or "
                    "parentheses, so it matches ZERO files and the node produces "
                    "nothing. Iterate over each extension separately (loop the "
                    "extensions and glob each one)."
                )
                break

    def _is_subprocess_or_system(call: "_ast.Call") -> bool:
        f = call.func
        if isinstance(f, _ast.Attribute) and isinstance(f.value, _ast.Name):
            if f.value.id == "subprocess" and f.attr in {
                "run", "call", "check_call", "check_output", "Popen",
            }:
                return True
            if f.value.id == "os" and f.attr in {"system", "popen"}:
                return True
        return False

    # (c) import pip / from pip import …
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import) and any(
            a.name == "pip" or a.name.startswith("pip.") for a in node.names
        ):
            issues.append(
                "The script imports `pip` and uses it programmatically (e.g. "
                "pip.get_installed/pip.main). pip has NO supported public API — "
                "this raises AttributeError/ImportError at runtime. Never import "
                "pip; declare dependencies in `requirements` (pip) or "
                "`system_packages` (apt) and import/use them directly."
            )
            break
        if isinstance(node, _ast.ImportFrom) and (node.module or "").split(".")[0] == "pip":
            issues.append(
                "The script imports from `pip` — pip has NO supported public API. "
                "Declare dependencies in `requirements`/`system_packages` instead."
            )
            break

    # (d) shell operator as a literal element of a subprocess arg list.
    _shell_ops = {"&&", "||", "|", ";", ">", ">>", "<", "2>", "2>&1", "&"}
    _flagged_op = False
    for node in _ast.walk(tree):
        if _flagged_op:
            break
        if isinstance(node, _ast.Call) and _is_subprocess_or_system(node):
            for arg in node.args:
                if isinstance(arg, (_ast.List, _ast.Tuple)):
                    for el in arg.elts:
                        if (isinstance(el, _ast.Constant)
                                and isinstance(el.value, str)
                                and el.value.strip() in _shell_ops):
                            issues.append(
                                f"A subprocess argument list contains the shell "
                                f"operator '{el.value.strip()}'. The list form of "
                                f"subprocess runs NO shell, so operators like && "
                                f"are passed as a literal argument and the command "
                                f"fails. Run each step as a SEPARATE subprocess "
                                f"call (preferred), or pass one command string "
                                f"with shell=True."
                            )
                            _flagged_op = True
                            break
                if _flagged_op:
                    break

    # (e) names used (Load) but never bound anywhere in the module. Flat
    #     over-approximation of bindings => only genuinely-undefined names flag
    #     (very low false positives). Skipped under `from x import *`.
    _has_star = any(
        isinstance(n, _ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in _ast.walk(tree)
    )
    if not _has_star:
        import builtins as _bi
        bound: set = set(dir(_bi))
        bound.update({"__file__", "__name__", "__doc__", "__builtins__"})
        used: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Name):
                if isinstance(node.ctx, _ast.Store):
                    bound.add(node.id)
                elif isinstance(node.ctx, _ast.Load):
                    used.add(node.id)
            elif isinstance(node, _ast.arg):
                bound.add(node.arg)
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, _ast.Import):
                for a in node.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                for a in node.names:
                    bound.add(a.asname or a.name)
            elif isinstance(node, (_ast.Global, _ast.Nonlocal)):
                bound.update(node.names)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                # `except Exception as e:` binds the name `e` (a str on the
                # handler, NOT a Name node) — missing this false-rejected a valid
                # script that referenced `e` in its except body (repro 2026-09-03).
                bound.add(node.name)
            elif isinstance(node, getattr(_ast, "MatchAs", ())) and getattr(node, "name", None):
                bound.add(node.name)  # match/case capture: `case x:` / `... as x`
            elif isinstance(node, getattr(_ast, "MatchStar", ())) and getattr(node, "name", None):
                bound.add(node.name)  # `case [*rest]:`
        undefined = sorted(n for n in used if n not in bound)
        if undefined:
            names = ", ".join(undefined[:8])
            issues.append(
                f"The script uses name(s) that are never defined or imported: "
                f"{names}. This raises NameError on the first run. Define them "
                f"(e.g. read a setting via os.getenv('NAME', <default>)), import "
                f"them, or remove the reference."
            )

    # (f) `.save()` on a numpy array — ndarray has NO .save() method (that's
    #     PIL's Image.save). The model confuses them (repro 2026-09-03:
    #     grey = np.array(img.convert('L')); grey.save(dst)) — AttributeError.
    #     Track names assigned from np.array/np.asarray/... and flag .save() on
    #     them, plus the inline np.array(...).save() form.
    _np_ctors = {"array", "asarray", "frombuffer", "zeros", "ones", "empty",
                 "full", "stack", "concatenate", "reshape"}
    def _is_np_call(node: "_ast.AST") -> bool:
        return (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr in _np_ctors
                and isinstance(node.func.value, _ast.Name)
                and node.func.value.id in {"np", "numpy"})
    _np_names: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign) and _is_np_call(node.value):
            for tgt in node.targets:
                if isinstance(tgt, _ast.Name):
                    _np_names.add(tgt.id)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "save"):
            base = node.func.value
            if ((isinstance(base, _ast.Name) and base.id in _np_names)
                    or _is_np_call(base)):
                issues.append(
                    "The script calls .save() on a numpy array — numpy arrays have "
                    "NO .save(path) method (that is PIL's Image.save), so this "
                    "raises AttributeError. To save an array as an image, wrap it: "
                    "PIL.Image.fromarray(arr).save(path). Or skip numpy and use "
                    "the PIL image directly (img.convert('L').save(path))."
                )
                break

    # (g) glob/rglob/iglob called with NO pattern argument — always a TypeError
    #     ('pattern' is required). The model builds the pattern into the PATH
    #     (IN / '*.jpg') then calls .glob() empty (repro 2026-09-03). Crashes on
    #     the first iteration -> no output. (Bare *args forwarding is exempt.)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr in {"glob", "rglob", "iglob"}
                and not node.args
                and not any(isinstance(a, _ast.Starred) for a in node.args)):
            issues.append(
                "A .glob()/.rglob() call has NO pattern argument — that raises "
                "TypeError (the pattern is required) and the node crashes before "
                "writing anything. Pass the pattern to glob, e.g. "
                "IN.glob(f'*.{ext}'); do NOT build the pattern into the path "
                "(IN / '*.jpg') and then call .glob() with no argument."
            )
            break

    # (h) .join() on a pathlib Path — Path has NO .join() (that is str.join /
    #     os.path.join); paths join with the / operator. repro 2026-09-04:
    #     OUT.join(src.name) -> AttributeError: 'PosixPath' object has no 'join'.
    def _is_path_ctor(n: "_ast.AST") -> bool:
        return (isinstance(n, _ast.Call)
                and (getattr(n.func, "id", None) == "Path"
                     or getattr(getattr(n, "func", None), "attr", None) == "Path"))
    _path_names: set = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Assign) and _is_path_ctor(node.value):
            for t in node.targets:
                if isinstance(t, _ast.Name):
                    _path_names.add(t.id)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "join"):
            base = node.func.value
            if ((isinstance(base, _ast.Name) and base.id in _path_names)
                    or _is_path_ctor(base)):
                issues.append(
                    "The script calls .join() on a pathlib Path — Path has NO "
                    ".join() method (AttributeError). Join path segments with the / "
                    "operator instead, e.g. OUT / src.name — not OUT.join(src.name)."
                )
                break

    # (i) subscripting a PIL Image (numpy-style img[::-1]) — a PIL Image is NOT
    #     subscriptable -> TypeError. repro 2026-09-04:
    #     Image.fromarray(img.convert('RGB')[::-1, :, :]). Inline-only (Image.open(..)
    #     / .convert(..) result) so a numpy array reassigned to the same name is safe.
    def _returns_image(n: "_ast.AST") -> bool:
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute):
            if n.func.attr == "convert":
                return True
            if n.func.attr == "open" and (
                (isinstance(n.func.value, _ast.Name) and n.func.value.id == "Image")
                or (isinstance(n.func.value, _ast.Attribute)
                    and n.func.value.attr == "Image")):
                return True
        return False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Subscript) and _returns_image(node.value):
            issues.append(
                "The script subscripts a PIL Image (e.g. img.convert('RGB')[::-1]) — "
                "a PIL Image is NOT subscriptable, so this raises TypeError. For "
                "array ops convert first: import numpy as np; arr = np.array(img); "
                "arr[::-1]; or use an Image method (img.transpose(Image.FLIP_LEFT_RIGHT))."
            )
            break
    return issues


def _executability_mechanical(script: str) -> List[str]:
    """Deterministic runtime-crash patterns — the exact classes of bug a weak
    model produces when it 'wraps' a tool it doesn't understand. All live in
    `fatal_runtime_crashes` now (so the structural validator enforces the same
    set as FATAL and a certain-crash script is never shipped); this thin wrapper
    keeps the exec-critic reporting them for the regen loop's feedback."""
    return _dedup(fatal_runtime_crashes(script))


# ---------------------------------------------------------------------------
# Layer 2 — reasoning-LLM intent / feasibility verdict
# ---------------------------------------------------------------------------

_VERDICT_SYSTEM_PROMPT = """You audit whether a generated Python node script
ACCOMPLISHES the user's request AND can plausibly be built and run. You are the
last automated check before a human reviews it.

The node reads inputs from /data/input and writes outputs to /data/output.

Judge on two lenses:
- INTENT: does the script perform the OPERATION the user asked for? (Not style,
  not micro-bugs — a separate pass handles those.)
- FEASIBILITY: can this realistically build/run? Flag things like: shelling out
  to a tool that has no installable package and needs a multi-hour source build
  (e.g. AliceVision/Meshroom, COLMAP from source); pip-installing a package that
  does not exist; downloading a model/binary from a URL that is clearly invented.

Decide ONE verdict:
- "match": it does the requested operation and looks buildable. DEFAULT here
  when it plausibly does the job — do NOT nitpick stylistic choices (any
  reasonable enhancement/algorithm is fine when the user didn't specify one).
- "mismatch": it performs a FUNDAMENTALLY DIFFERENT operation than requested,
  OR it depends on something that clearly cannot build/install. Use this ONLY
  when highly confident. Set fix_hint to what it SHOULD do instead.
- "ambiguous": the request OMITS something the node fundamentally needs, and the
  script had to GUESS it among MATERIALLY DIFFERENT options the user never
  stated — e.g. a target output FORMAT ("convert my files" -> to what?), target
  DIMENSIONS, or which of two+ distinct operations was meant. Set `question` to
  ONE short question that pins down exactly that missing decision. This is a
  narrow bar: only when a WRONG guess would produce the wrong result. NOT for
  stylistic latitude.

Never invent requirements the user did not state.

Examples:
- Request "convert my files"; script converts CSV to JSON. -> "ambiguous",
  question "What format should the files be converted from and to?" (the target
  format is a required decision the user never gave; CSV->JSON is one guess of
  many).
- Request "resize images"; script hardcodes 800x600 with no setting. ->
  "ambiguous", question "What target width and height should images be resized
  to?"
- Request "make my images better"; script sharpens them. -> "match" (any
  reasonable enhancement is acceptable when the user didn't specify one).
- Request "convert PDFs to PNG"; script rasterises PDF pages to PNG. -> "match"
  (both formats were stated).

Output ONLY a single JSON object, no markdown, no prose:
{"verdict":"match|mismatch|ambiguous",
 "confidence":0.0-1.0,
 "reason":"one sentence",
 "fix_hint":"present when mismatch: what to build instead",
 "question":"present when ambiguous: ONE short, plain-language question whose answer would resolve the ambiguity",
 "warnings":["optional short advisory notes for the reviewer"]}
"""


def _critic_slot() -> str:
    """Which AI SLOT the Node Wizard's review pass runs on, honouring the
    operator's ``critic_mode``: "self" → the wizard (generator) slot, anything
    else → the ``critic`` slot (the independently configured reviewer, or the
    tier's reasoner role when left managed)."""
    try:
        from utils.paid_import import optional_paid
        _ai_cfg = optional_paid("routers.ai_config")
        if _ai_cfg is not None:
            return ("wizard" if _ai_cfg._wizard_critic_mode() == "self"
                    else "critic")
        mode = None
        try:
            from utils.tool_config import read_tool_config
            mode = (read_tool_config("wizard") or {}).get("critic_mode")
        except Exception:  # noqa: BLE001
            mode = None
        return "wizard" if mode == "self" else "critic"
    except Exception:  # noqa: BLE001
        return "critic"


def _default_reason_llm(
    tenant_id: Optional[int] = None,
) -> Callable[..., Any]:
    """A callable that hits the REASONING model (the 'critic' slot -> gpt-oss),
    with the JSON + token-headroom knobs Critic D needs.

    ai-config-stack-binding 3.4 — it no longer "falls back to the wizard
    model": an unbound / stopped / missing / unreachable review slot raises
    :class:`LLMClientError` carrying the slot resolver's cause sentence, so
    the caller surfaces "the review model is unavailable: <cause>" instead of
    quietly reviewing (or skipping the review) with someone else's model.
    """
    from utils.llm_client import call_wizard_llm, LLMClientError

    def _call(*, system_prompt: str, user_prompt: str) -> Any:
        # separate-reasoner-model §2 — honour the operator's critic_mode:
        # self-review runs on the wizard/coder model; "Separate reasoner model"
        # runs on the independently-configured reviewer (the 'critic' slot), or
        # the tier's reasoner role when that slot is left managed.
        slot = _critic_slot()
        try:
            from utils.tool_config import critic_env
            env = critic_env()
        except Exception:  # noqa: BLE001
            from utils.llm_client import _get_tool_config
            env = _get_tool_config(slot)
        # ai-config-stack-binding 3.4 — an empty env used to mean "borrow the
        # wizard model" (or just fail deep inside the transport). Now: when
        # THIS slot has a named failure, raise it so the caller surfaces the
        # cause sentence; the wizard is never quietly substituted.
        from utils.llm_client import LLMClientError, _slot_failure_cause
        if not ((env.get("LLM_BASE_URL") or "").strip()
                and (env.get("LLM_MODEL") or "").strip()):
            cause = _slot_failure_cause(slot, tenant_id=tenant_id)
            if cause:
                raise LLMClientError(cause)
        return call_wizard_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format="json",
            timeout_s=90,
            max_tokens=_VERDICT_MAX_TOKENS,
            _env_override=env,
            _label="critic",
        )

    return _call


def _intent_verdict(
    description: str,
    script: str,
    *,
    input_type: str,
    output_type: str,
    llm: Optional[Callable[..., Any]] = None,
) -> Optional[dict]:
    """ai-config-stack-binding 3.4 — a NAMED slot failure (the resolver's cause
    sentence) is re-raised as :class:`LLMClientError` instead of being folded
    into "no opinion", so the caller can tell "the review model is down:
    <cause>" from "the reviewer had nothing to say".

    Returns the reasoner's parsed verdict dict, or None on any other failure
    (caller treats None as 'accept, no opinion')."""
    if llm is None:
        llm = _default_reason_llm()
    user_prompt = (
        f"INTENT (what the user asked for):\n{description.strip()}\n\n"
        f"DECLARED IO: {input_type or 'other'} -> {output_type or 'other'}\n\n"
        f"GENERATED SCRIPT:\n```python\n{script.strip()}\n```"
    )
    try:
        parsed = llm(system_prompt=_VERDICT_SYSTEM_PROMPT, user_prompt=user_prompt)
    except LLMClientError as e:
        # ai-config-stack-binding 3.4 — a NAMED slot failure is not "no
        # opinion": the caller turns it into a visible advisory so the user
        # learns the review was skipped because the review model is down.
        logger.info("critic-d: review slot unavailable (%s) — re-raising", e)
        raise
    except Exception as e:  # noqa: BLE001 — any failure => no opinion
        logger.info("critic-d: reasoning verdict unavailable (%s) — accepting", e)
        return None
    if not isinstance(parsed, dict):
        logger.info("critic-d: verdict not a dict (%r) — accepting", type(parsed))
        return None
    return parsed


def _coerce_confidence(raw: Any) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, c))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def critique_script(
    description: str,
    script: str,
    *,
    input_type: str = "",
    output_type: str = "",
    already_clarified: bool = False,
    llm: Optional[Callable[..., Any]] = None,
) -> CritiqueResult:
    """Run Critic D over a structurally-valid synthesized script.

    ``already_clarified`` — True once the user has answered a clarifying
    question for this request. When set, an ambiguous verdict degrades to an
    advisory warning instead of asking again (we ask AT MOST once).

    Never raises — any internal failure yields an empty (accept) result.
    """
    result = CritiqueResult()
    if not _enabled() or not (script or "").strip():
        return result

    try:
        # Layer 1 — mechanical (deterministic).
        result.regenerate_reasons.extend(_mechanical_issues(script))

        # Layer 2 — reasoning verdict (intent + feasibility).
        verdict = _intent_verdict(
            description or "", script,
            input_type=input_type, output_type=output_type, llm=llm,
        )
        if isinstance(verdict, dict):
            v = str(verdict.get("verdict") or "").strip().lower()
            conf = _coerce_confidence(verdict.get("confidence"))
            reason = str(verdict.get("reason") or "").strip()[:400]
            fix_hint = str(verdict.get("fix_hint") or "").strip()[:600]
            question = str(verdict.get("question") or "").strip()[:300]
            result.verdict, result.confidence = v, conf

            # Model-supplied advisory warnings pass straight through.
            for w in (verdict.get("warnings") or []):
                if isinstance(w, str) and w.strip():
                    result.warnings.append(w.strip()[:300])

            if v == "mismatch" and conf >= _MISMATCH_AUTOFIX_THRESHOLD:
                # High-confidence mismatch → auto-fix (regenerate).
                msg = "Does not match the request"
                if reason:
                    msg += f": {reason}"
                if fix_hint:
                    msg += f" Build this instead: {fix_hint}"
                result.regenerate_reasons.append(msg)
            elif v in ("mismatch", "ambiguous"):
                # Not confident enough to rewrite. If we can ask a good
                # question and haven't already, ASK the user (v2); otherwise
                # degrade to a heads-up. (Only when there's nothing to
                # regenerate — mechanical fixes take priority this round.)
                if question and not already_clarified and not result.regenerate_reasons:
                    result.clarifying_question = question
                elif reason:
                    result.warnings.append(
                        f"Heads up — this may not fully match your request: {reason}"
                    )
    except LLMClientError as e:
        # ai-config-stack-binding 3.4 — the review never ran because the
        # review SLOT did not serve. The critic still must not block the
        # build, but the cause sentence is surfaced as an advisory the user
        # sees in Review (never a silently-skipped review, never another
        # model). Mechanical findings from layer 1 survive.
        logger.info("critic-d: review slot unavailable (%s) — advising", e)
        result.warnings.append(f"AI review skipped: {e}")
    except Exception as e:  # noqa: BLE001 — critic must never block a build
        logger.warning("critic-d: unexpected failure (%s) — accepting script as-is", e)
        return CritiqueResult()

    if result.regenerate_reasons:
        logger.info(
            "critic-d: requesting regen (verdict=%s conf=%s reasons=%d)",
            result.verdict, result.confidence, len(result.regenerate_reasons),
        )
    return result


# ---------------------------------------------------------------------------
# Executability critic — "will this code actually RUN?" (the self-review a
# human does at a glance). Distinct from critique_script's INTENT lens: a
# script can do the right thing conceptually and still crash on execution
# (hallucinated imports, pip-installing a C++ repo, invented CLI subcommands,
# str/Path type bugs, brace-glob). Runs BEFORE the user sees the result and
# feeds fatal findings into the SAME regenerate loop so the coder fixes them.
# ---------------------------------------------------------------------------

# Executability is more objective than intent-match, so regen on a lower bar —
# a false positive just costs one extra coder attempt; shipping code that
# crashes costs the user a failed build.
_EXEC_REGEN_THRESHOLD = float(os.getenv("WIZARD_EXEC_CRITIC_THRESHOLD", "0.6"))

_EXECUTABILITY_SYSTEM_PROMPT = """You are doing a FINAL pre-flight review of a \
script that will run UNMODIFIED inside a container. Your ONLY question: will \
this code RUN TO COMPLETION without crashing or silently producing nothing? \
Ignore style, performance, and whether it is the "best" approach — flag ONLY \
bugs that cause a crash or a guaranteed no-op.

The container has: Python 3.11, the pip packages listed in REQUIREMENTS, the \
apt packages listed in SYSTEM_PACKAGES, and standard OS tools. It reads \
/data/input and writes /data/output. If REPO DOCS are given, they are the \
tool's REAL, authoritative usage — judge the code against them.

Hunt specifically for RUNTIME-FATAL bugs:
- Imports that fail: importing a module that is NOT in REQUIREMENTS and NOT part \
of the Python standard library, or a module that does not exist at all (e.g. \
`import colmap` — COLMAP is a C++ program, there is no `colmap` python module).
- Bad install: `pip install`-ing something that is not a Python package (a \
C++/CMake repo) or a package name that does not exist on PyPI. Dependencies \
belong in REQUIREMENTS/SYSTEM_PACKAGES, not a runtime subprocess call.
- CLI misuse: shelling out to a command, subcommand, or flag that does not \
exist (invented usage the docs don't support).
- Type bugs: using `/` on a `str` (only pathlib.Path supports it), calling a \
method the object doesn't have, indexing/opening the wrong type.
- File-pattern bugs: Python glob/fnmatch do NOT expand brace groups like \
`*.{jpg,png}` — that matches nothing.
- Guaranteed-empty output: running a tool over an empty/never-populated \
directory, or reading a file the code never wrote.

Return ONLY a JSON object, no markdown:
{"runs": true|false,
 "fatal_issues": ["one specific, actionable fix per crash-causing issue"],
 "confidence": 0.0-1.0}
Set runs=false ONLY for issues you are confident would crash or no-op. When \
genuinely unsure, runs=true — do not churn a plausibly-working script."""


def critique_executability(
    description: str,
    script: str,
    *,
    requirements: Optional[List[str]] = None,
    system_packages: Optional[List[str]] = None,
    repo_context: Optional[str] = None,
    llm: Optional[Callable[..., Any]] = None,
) -> CritiqueResult:
    """Second-guess a structurally-valid script: 'will it actually run?'.

    Returns a CritiqueResult whose ``regenerate_reasons`` carry each fatal bug
    found (deterministic patterns + an aggressive reasoning-LLM pass). Empty =>
    looks runnable. Never raises — a critic failure means 'no opinion, accept'.
    """
    result = CritiqueResult()
    # Deterministic layer first — free, high-confidence, catches the exact
    # weak-model patterns even if the LLM is unavailable or misses them.
    result.regenerate_reasons.extend(_executability_mechanical(script))

    if not _enabled():
        return result
    if llm is None:
        llm = _default_reason_llm()
    reqs = ", ".join(requirements or []) or "(none)"
    pkgs = ", ".join(system_packages or []) or "(none)"
    user_prompt = (
        f"TASK: {description.strip()}\n\n"
        f"REQUIREMENTS (pip, already installed): {reqs}\n"
        f"SYSTEM_PACKAGES (apt, already installed): {pkgs}\n\n"
        f"SCRIPT:\n```python\n{script.strip()}\n```"
    )
    if repo_context and repo_context.strip():
        user_prompt += (
            "\n\nREPO DOCS (the chosen tool's real usage — the code MUST match "
            "these):\n" + repo_context.strip()[:3000]
        )
    try:
        parsed = llm(
            system_prompt=_EXECUTABILITY_SYSTEM_PROMPT, user_prompt=user_prompt,
        )
    except LLMClientError as e:
        # ai-config-stack-binding 3.4 — the review model's slot named a
        # failure: surface it, do not pretend the script was reviewed.
        logger.info("exec-critic: review slot unavailable (%s) — advising", e)
        result.warnings.append(f"AI executability review skipped: {e}")
        return result
    except Exception as e:  # noqa: BLE001 — never block on the critic
        logger.info("exec-critic: verdict unavailable (%s) — accepting", e)
        return result
    if isinstance(parsed, dict) and parsed.get("runs") is False:
        conf = _coerce_confidence(parsed.get("confidence"))
        if conf >= _EXEC_REGEN_THRESHOLD:
            for iss in (parsed.get("fatal_issues") or []):
                if isinstance(iss, str) and iss.strip():
                    result.regenerate_reasons.append(iss.strip()[:400])
    if result.regenerate_reasons:
        logger.info(
            "exec-critic: script won't run cleanly (%d issue(s)) — requesting regen",
            len(result.regenerate_reasons),
        )
    return result


# ---------------------------------------------------------------------------
# Layer 1c — the DIRECTIONS critic ("did you follow the instructions?")
# ---------------------------------------------------------------------------

_IO_SKELETON = (
    "from pathlib import Path\n"
    "IN = Path('/data/input'); OUT = Path('/data/output')\n"
    "OUT.mkdir(parents=True, exist_ok=True)\n"
    "for src in sorted(IN.rglob('*')):\n"
    "    if src.is_file():\n"
    "        ...  # do the work on src, write result(s) under OUT"
)

# Idioms that count as "produces output": a file write, an image/df/plot save,
# a copy into the output dir, or a printed result line (the contract allows one
# printed line as output).
_WRITES_RE = re.compile(
    r"\.(?:write|write_text|write_bytes|save|savefig|imwrite|to_csv|to_json|"
    r"to_parquet|dump|writelines)\s*\(|open\s*\([^)]*['\"][wax][b+]?['\"]|"
    r"shutil\.(?:copy|copy2|copyfile|copytree|move)\s*\(|json\.dump\s*\(|"
    r"cv2\.imwrite\s*\(",
    re.IGNORECASE,
)
_PRINT_RE = re.compile(r"\bprint\s*\(")


# Pixel/content transformation verbs — when the user asks for one of these, a
# script that writes the input image back UNCHANGED (a passthrough/copy) is doing
# the wrong thing. Deliberately NOT generic ("convert"/"png"): a format-only copy
# (jpg->png via imread+imwrite) is legitimate and must not be flagged.
_TRANSFORM_INTENT = (
    "grayscale", "greyscale", "gray", "grey", "resize", "rescale", "downscale",
    "upscale", "thumbnail", "blur", "sharpen", "rotate", "flip", "mirror",
    "crop", "invert", "negative", "threshold", "binariz", "denoise", "brighten",
    "contrast", "gamma", "sepia", "edge", "sobel", "canny", "dilate", "erode",
    "sketch", "cartoon", "posteriz", "saturat", "desaturat", "hue", "normaliz",
    "equaliz", "pixelate", "watermark", "overlay",
)


# Invented-package guard. A weak model coins a plausible-but-nonexistent PyPI
# name from the task ("image_conversion" for image conversion) and declares +
# imports it — pip install then fails at build (repro 2026-09-03). Verify each
# declared requirement actually exists on PyPI. Best-effort + cached: a network
# error or non-404 is treated as UNKNOWN (never flagged), so the check can only
# ever reject a package PyPI positively reports as absent.
_PYPI_CACHE: dict = {}


def _pypi_exists(name: str) -> Optional[bool]:
    key = name.lower()
    if key in _PYPI_CACHE:
        return _PYPI_CACHE[key]
    import urllib.request as _u
    import urllib.error as _ue
    result: Optional[bool] = None
    try:
        with _u.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=5) as r:
            result = (r.status == 200)
    except _ue.HTTPError as e:
        result = False if e.code == 404 else None  # 404 = absent; else unknown
    except Exception:  # noqa: BLE001 — network/DNS/etc. -> unknown, never flag
        result = None
    _PYPI_CACHE[key] = result
    return result


def unknown_pypi_requirements(requirements: Optional[List[str]]) -> List[str]:
    """Declared plain-PyPI requirements that PyPI reports as ABSENT (invented
    packages). Skips git+/URL/local specs and anything with spaces; strips
    version specifiers and extras before the lookup."""
    out: List[str] = []
    for raw in (requirements or []):
        r = (raw or "").strip()
        if (not r or " " in r
                or r.startswith(("git+", "http://", "https://", ".", "/", "-e "))):
            continue
        name = re.split(r"[<>=!~;\[\( ]", r, 1)[0].strip()
        if name and _pypi_exists(name) is False:
            out.append(name)
    return out


def critique_directions(
    script: str,
    *,
    description: Optional[str] = None,
    requirements: Optional[List[str]] = None,
    system_packages: Optional[List[str]] = None,
    dockerfile_extras: Optional[str] = None,
) -> "CritiqueResult":
    """The DIRECTIONS critic — a dedicated pass whose only job is to check the
    generated script against the runtime CONTRACT (the directions the coder was
    given) and tell it, plainly, which direction it broke. Every finding is
    phrased 'Direction not followed: …' so a weak local model gets an
    unambiguous "you didn't do what you were told" signal, and feeds the same
    regenerate loop as the other critics.

    Deterministic on purpose: the small local critic models emit unreliable JSON
    and can be talked out of a real violation — a directions check must not be.
    """
    result = CritiqueResult(verdict="directions")
    s = script or ""
    reasons: List[str] = []

    # Direction: read inputs from /data/input; write outputs to /data/output.
    if "/data/input" not in s:
        reasons.append(
            "Direction not followed: read inputs from /data/input — the script "
            "never references it. Use this exact shape:\n" + _IO_SKELETON
        )
    if "/data/output" not in s:
        reasons.append(
            "Direction not followed: write outputs to /data/output — the script "
            "never references it. Use this exact shape:\n" + _IO_SKELETON
        )

    # Direction: the node must PRODUCE output — at least one written file or one
    # printed line. Referencing the path but never writing is a silent no-op.
    if "/data/output" in s and not _WRITES_RE.search(s) and not _PRINT_RE.search(s):
        reasons.append(
            "Direction not followed: the node must PRODUCE output. The script "
            "references /data/output but never writes a file there (or prints a "
            "result line) — it would run and produce nothing. Write at least one "
            "output file under /data/output."
        )

    # A node that WRITES to /data/output must CREATE it first. cv2.imwrite() and
    # PIL .save() silently return False (no exception) when the target directory
    # is missing, so the node prints "saved …" while writing nothing (repro
    # 2026-09-03). The prescribed skeleton has OUT.mkdir(parents=True,
    # exist_ok=True) — enforce it.
    if ("/data/output" in s and _WRITES_RE.search(s)
            and "mkdir" not in s and "makedirs" not in s):
        reasons.append(
            "Direction not followed: create /data/output before writing — add "
            "OUT.mkdir(parents=True, exist_ok=True). cv2.imwrite() and PIL "
            ".save() silently fail (write nothing) if the directory does not "
            "exist, so the node can report success while producing no output."
        )

    # Crash-level direction violations (import pip, && in a subprocess list,
    # undefined names, runtime pip install, brace-glob) — restated as directions
    # so they read as instruction-following failures, not just runtime bugs.
    for f in fatal_runtime_crashes(s):
        reasons.append("Direction not followed: " + f)

    # Other mechanical contract violations (required-or-crash settings via
    # os.environ[...] subscript, Docker-in-Docker, JPEG-without-RGB).
    for m in _mechanical_issues(s):
        reasons.append("Direction not followed: " + m)

    # STUB detector: a non-stdlib package imported but NEVER used means the actual
    # work is missing or commented out (repro 2026-09-03: `import image_conversion`
    # then the conversion call is a comment and the loop just prints a status —
    # the node ran and did nothing). A tool you import must be CALLED to do the job.
    import ast as _ast
    try:
        _tree = _ast.parse(s)
    except SyntaxError:
        _tree = None
    if _tree is not None:
        import sys as _sys
        _stdlib = set(getattr(_sys, "stdlib_module_names", ()))
        _imported: dict = {}   # local name -> top-level module it comes from
        _used: set = set()
        for node in _ast.walk(_tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    _imported[(a.asname or a.name).split(".")[0]] = a.name.split(".")[0]
            elif isinstance(node, _ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                for a in node.names:
                    if a.name != "*":
                        _imported[a.asname or a.name] = mod
            elif isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
                _used.add(node.id)
            elif isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name):
                _used.add(node.value.id)
        for _name, _mod in _imported.items():
            if _name not in _used and _mod and _mod not in _stdlib and _name not in _stdlib:
                reasons.append(
                    f"Direction not followed: you import `{_name}` but never call "
                    f"it — the actual work is missing or commented out (a stub that "
                    f"just prints is not doing the job). Use a REAL, importable "
                    f"library to do the work and write results to /data/output, or "
                    f"remove the unused import."
                )

    # UNCHANGED-PASSTHROUGH: the user asked for a pixel transform (grayscale,
    # resize, …) but the script writes the image it READ back UNMODIFIED — the
    # transform is missing or its result was discarded (repro 2026-09-03:
    # `img=imread(src); imwrite(dst,img)` and `cvtColor(img,GRAY,dst=img)` whose
    # channel-changing result can't write in place, so img stays colour). Runs,
    # produces output, but does the WRONG operation — neither a crash-smoke nor
    # the other checks catch it. Only fires when the DESCRIPTION requests a
    # transform, so a legit format-only copy (jpg->png) is not flagged.
    _desc = (description or "").lower()
    if _tree is not None and any(k in _desc for k in _TRANSFORM_INTENT):
        def _is_read(node: "_ast.AST") -> bool:
            return (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                    and (node.func.attr == "imread"
                         or (node.func.attr == "open"
                             and isinstance(node.func.value, _ast.Name)
                             and node.func.value.id == "Image")))
        _assign_kinds: dict = {}   # var -> set of {'read','other'}
        for node in _ast.walk(_tree):
            if isinstance(node, _ast.Assign):
                kind = "read" if _is_read(node.value) else "other"
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        _assign_kinds.setdefault(t.id, set()).add(kind)
            # `with Image.open(src) as img:` binds `img` to the read via a
            # withitem, not an Assign — the idiomatic Pillow form. Without this
            # a passthrough written as `with Image.open(src) as img: img.save(dst)`
            # slips past the check (repro 2026-09-03 run 4: opened + re-saved as
            # PNG with no grayscale convert, shipped ok=True).
            elif isinstance(node, _ast.With):
                for item in node.items:
                    if isinstance(item.optional_vars, _ast.Name):
                        kind = "read" if _is_read(item.context_expr) else "other"
                        _assign_kinds.setdefault(
                            item.optional_vars.id, set()).add(kind)
        _written: set = set()
        for node in _ast.walk(_tree):
            if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute):
                if (node.func.attr == "imwrite" and len(node.args) >= 2
                        and isinstance(node.args[1], _ast.Name)):
                    _written.add(node.args[1].id)   # cv2.imwrite(dst, VAR)
                elif node.func.attr == "save" and isinstance(node.func.value, _ast.Name):
                    _written.add(node.func.value.id)  # VAR.save(dst)
        # A written var whose ONLY assignment source is a read = unmodified copy.
        if any(_assign_kinds.get(v) == {"read"} for v in _written):
            reasons.append(
                "Direction not followed: the task asks to transform the image "
                "(e.g. grayscale/resize), but the script writes the image it READ "
                "back UNCHANGED — the operation is missing or its result was "
                "discarded (a common slip: cv2.cvtColor(img, code, dst=img) can't "
                "convert in place when channels change; capture the return value: "
                "gray = cv2.cvtColor(img, code); then imwrite(dst, gray)). Apply "
                "the operation and write its RESULT."
            )

    # Invented PyPI packages — reject a declared requirement PyPI says doesn't
    # exist (repro 2026-09-03: `image_conversion`, coined from "image conversion").
    for pkg in unknown_pypi_requirements(requirements):
        reasons.append(
            f"Direction not followed: the requirement `{pkg}` does NOT exist on "
            f"PyPI — it was invented from the task name. Use a REAL, installable "
            f"library and its actual API (e.g. Pillow or opencv-python for images)."
        )

    result.regenerate_reasons = _dedup(reasons)
    return result
