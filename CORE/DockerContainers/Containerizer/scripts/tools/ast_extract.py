"""Python AST extraction — deterministic answers to
"what does this file import", "what's its entry point", "what env vars
does it read", "does it use argparse/click", "does it use torch.cuda".

This is the ground truth input to downstream LLM stages. When ast
disagrees with what an LLM would say, ast wins — it's reading the file
that Python itself would run.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


# Framework / runtime heuristics — looked up by qualified import names.
# The LLM reuses these to infer "is this a web app / ml inference / worker?".
WEB_FRAMEWORKS = {
    "flask": "Flask", "fastapi": "FastAPI", "starlette": "Starlette",
    "django": "Django", "tornado": "Tornado", "aiohttp": "aiohttp",
    "sanic": "Sanic", "bottle": "Bottle", "quart": "Quart",
}
QUEUE_FRAMEWORKS = {
    "celery": "Celery", "rq": "RQ", "dramatiq": "Dramatiq",
    "arq": "arq", "huey": "Huey",
}
ML_FRAMEWORKS = {
    "torch": "PyTorch", "tensorflow": "TensorFlow", "keras": "Keras",
    "jax": "JAX", "flax": "Flax", "transformers": "HuggingFace-Transformers",
    "diffusers": "HuggingFace-Diffusers", "sentence_transformers": "sentence-transformers",
    "onnxruntime": "ONNX-Runtime", "tensorrt": "TensorRT",
    "xformers": "xFormers", "vllm": "vLLM", "tensorflow_hub": "TF-Hub",
}
CLI_PARSERS = {"argparse", "click", "typer", "docopt", "fire"}


@dataclass
class FunctionDef:
    name: str
    lineno: int
    args: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    is_async: bool = False


@dataclass
class ClassDef:
    name: str
    lineno: int
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


@dataclass
class EnvRead:
    name: str                # the env var key, e.g. "OPENAI_API_KEY"
    lineno: int
    required: bool = False   # True if `os.environ["X"]` or `.get("X")` with no default


@dataclass
class PythonFile:
    path: str                              # relative to the repo root
    imports: list[str] = field(default_factory=list)       # e.g. ["torch", "torch.nn", "flask"]
    from_imports: list[tuple[str, str]] = field(default_factory=list)  # (module, name) pairs
    functions: list[FunctionDef] = field(default_factory=list)
    classes: list[ClassDef] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)  # labels: "__main__", "main()", "app", "handler", …
    env_reads: list[EnvRead] = field(default_factory=list)
    cli_parsers: list[str] = field(default_factory=list)   # subset of CLI_PARSERS
    web_frameworks: list[str] = field(default_factory=list)
    queue_frameworks: list[str] = field(default_factory=list)
    ml_frameworks: list[str] = field(default_factory=list)
    calls_subprocess: bool = False         # subprocess.run / Popen / os.system
    calls_exec_eval: bool = False          # exec() / eval() (security-relevant)
    uses_gpu: bool = False                 # torch.cuda, tf.config.gpus, etc.
    parse_error: Optional[str] = None      # SyntaxError message when ast.parse failed


def extract(repo_root: str, rel_path: str) -> PythonFile:
    """Parse one .py file into a PythonFile record."""
    path = Path(repo_root) / rel_path
    try:
        source = path.read_text(errors="replace")
    except OSError as e:
        return PythonFile(path=rel_path, parse_error=f"read failed: {e}")

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return PythonFile(path=rel_path, parse_error=f"SyntaxError line {e.lineno}: {e.msg}")

    pf = PythonFile(path=rel_path)
    visitor = _Visitor(pf)
    visitor.visit(tree)
    visitor.finalise(tree, source)
    return pf


def extract_all(repo_root: str, rel_paths: list[str]) -> list[PythonFile]:
    """Batch helper."""
    return [extract(repo_root, p) for p in rel_paths]


# ---------------------------------------------------------------------------
# Visitor
# ---------------------------------------------------------------------------

class _Visitor(ast.NodeVisitor):
    def __init__(self, pf: PythonFile):
        self.pf = pf
        self._top_level_names: set[str] = set()

    # -- imports ----------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.pf.imports.append(alias.name)
            self._note_framework(alias.name)
            self._top_level_names.add((alias.asname or alias.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if node.level:
            mod = ("." * node.level) + mod
        for alias in node.names:
            self.pf.from_imports.append((mod, alias.name))
            if alias.name != "*":
                self._top_level_names.add(alias.asname or alias.name)
        if mod:
            self._note_framework(mod)

    # -- funcs/classes ----------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_func(node, is_async=False); self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_func(node, is_async=True); self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.pf.classes.append(ClassDef(
            name=node.name, lineno=node.lineno,
            bases=[_unparse(b) for b in node.bases],
            decorators=[_unparse(d) for d in node.decorator_list],
        ))
        self._top_level_names.add(node.name)
        self.generic_visit(node)

    # -- env reads --------------------------------------------------

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # Matches os.environ["KEY"] and os.environ.get("KEY")
        if isinstance(node.value, ast.Attribute) and _dotted(node.value) == "os.environ":
            key = _literal_str(node.slice)
            if key:
                self.pf.env_reads.append(EnvRead(name=key, lineno=node.lineno, required=True))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = _dotted(node.func) or ""

        # os.environ.get("X") / os.environ.get("X", default)
        if callee == "os.environ.get" or callee.endswith(".environ.get"):
            key = _first_literal_str(node.args)
            if key:
                has_default = len(node.args) >= 2 or node.keywords
                self.pf.env_reads.append(
                    EnvRead(name=key, lineno=node.lineno, required=not has_default)
                )
        # os.getenv("X") / os.getenv("X", "default")
        if callee == "os.getenv":
            key = _first_literal_str(node.args)
            if key:
                has_default = len(node.args) >= 2 or node.keywords
                self.pf.env_reads.append(
                    EnvRead(name=key, lineno=node.lineno, required=not has_default)
                )

        # Security-relevant calls
        if callee in ("subprocess.run", "subprocess.Popen", "subprocess.call",
                      "subprocess.check_call", "subprocess.check_output",
                      "os.system", "os.popen"):
            self.pf.calls_subprocess = True
        if callee in ("exec", "eval") or callee.endswith(".exec") or callee.endswith(".eval"):
            self.pf.calls_exec_eval = True

        # GPU usage — the fast, cheap signal for "this needs CUDA".
        if callee in (
            "torch.cuda.is_available", "torch.cuda.device_count",
            "torch.cuda.current_device",
            "tensorflow.config.list_physical_devices",
        ) or "cuda" in callee.lower():
            self.pf.uses_gpu = True

        self.generic_visit(node)

    # -- helpers ----------------------------------------------------

    def _record_func(self, node, *, is_async: bool) -> None:
        self.pf.functions.append(FunctionDef(
            name=node.name, lineno=node.lineno, is_async=is_async,
            args=[a.arg for a in node.args.args],
            decorators=[_unparse(d) for d in node.decorator_list],
        ))
        self._top_level_names.add(node.name)

    def _note_framework(self, qualified_module: str) -> None:
        root = qualified_module.split(".")[0]
        if root in WEB_FRAMEWORKS and WEB_FRAMEWORKS[root] not in self.pf.web_frameworks:
            self.pf.web_frameworks.append(WEB_FRAMEWORKS[root])
        if root in QUEUE_FRAMEWORKS and QUEUE_FRAMEWORKS[root] not in self.pf.queue_frameworks:
            self.pf.queue_frameworks.append(QUEUE_FRAMEWORKS[root])
        if root in ML_FRAMEWORKS and ML_FRAMEWORKS[root] not in self.pf.ml_frameworks:
            self.pf.ml_frameworks.append(ML_FRAMEWORKS[root])
        if root in CLI_PARSERS and root not in self.pf.cli_parsers:
            self.pf.cli_parsers.append(root)

    def finalise(self, tree: ast.Module, source: str) -> None:
        # "__main__" block
        for stmt in tree.body:
            if isinstance(stmt, ast.If) and _is_name_eq_main(stmt.test):
                self.pf.entry_points.append("__main__")
                break

        # Top-level `app = Flask(...)` / `app = FastAPI()` etc. — useful
        # for detecting web-server entry points even when there's no
        # __main__ block.
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if isinstance(tgt, ast.Name) and tgt.id in ("app", "application", "handler"):
                        self.pf.entry_points.append(tgt.id)

        # If there's a top-level `main` function, call it out.
        if "main" in self._top_level_names and "main()" not in self.pf.entry_points:
            self.pf.entry_points.append("main()")

        # Dedupe entry points in-order.
        seen = set()
        self.pf.entry_points = [
            x for x in self.pf.entry_points
            if not (x in seen or seen.add(x))
        ]


# ---------------------------------------------------------------------------
# Small ast helpers
# ---------------------------------------------------------------------------

def _dotted(node: ast.AST) -> Optional[str]:
    """Build 'a.b.c' from an Attribute chain; None if not a plain chain."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr); cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _literal_str(node: ast.AST) -> Optional[str]:
    """Return the literal str in Subscript slice, handling both py3.8 Index
    and direct-expression styles."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # py3.8: ast.Index wrapper
    inner = getattr(node, "value", None)
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        return inner.value
    return None


def _first_literal_str(args: list[ast.AST]) -> Optional[str]:
    for a in args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return None


def _is_name_eq_main(test: ast.AST) -> bool:
    """True for `__name__ == "__main__"` in either order."""
    if not isinstance(test, ast.Compare):
        return False
    def is_main_literal(n):
        return isinstance(n, ast.Constant) and n.value == "__main__"
    def is_name(n):
        return isinstance(n, ast.Name) and n.id == "__name__"
    left_hit = is_name(test.left) and any(is_main_literal(c) for c in test.comparators)
    right_hit = is_main_literal(test.left) and any(is_name(c) for c in test.comparators)
    return left_hit or right_hit


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__
