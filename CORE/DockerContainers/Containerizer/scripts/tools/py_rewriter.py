"""Phase 7 — libcst-driven Python source rewriter for variant porting.

Tree-sitter is read-only (the polyglot parser `ast_polyglot.py` uses
it for syntax-aware detection). libcst is the right tool for
whitespace-preserving rewrites; the regex-based source rewriter in
`backends/recipes/intel_xpu.py` handles the simple cases but misses
indirect / conditional / factory-function device transfers.

Transformer classes:

  CudaIsAvailableRewriter  —
      `torch.cuda.is_available()` →
      `getattr(torch, _DEVICE_BACKEND, torch.cuda).is_available()`
      The dispatcher falls back to torch.cuda when the variant's
      backend module isn't importable (e.g. an xpu node running on a
      CUDA host during debug).

  DeviceStringRewriter     — replaces every literal `"cuda"` / `'cuda'`
      string inside `.to(...)`, `.cuda(...)`, `.device(...)`,
      `torch.device(...)` calls AND inside factory functions
      (`torch.zeros`, `torch.ones`, `torch.empty`, `torch.rand`, ...)
      when they're passed `device=` kwargs.

  CudaCallRewriter         — rewrites `.cuda()` bare calls to
      `.to(_DEVICE)`. Handles chained calls:
      `model.cuda().eval()` → `model.to(_DEVICE).eval()`.

  AmpAutocastRewriter      — namespace-respecting swap for
      `torch.cuda.amp.autocast` → `torch.amp.autocast("<device>")`.
      Respects context-manager nesting; only swaps the type reference,
      not nested autocast() calls inside it.

All transformers operate on a parsed libcst Module and return a new
Module. Round-trip (parse → serialize) on untouched files must be
byte-identical — guarded by `test_py_rewriter.py`'s fixture sweep.

Public API:

    from py_rewriter import rewrite_python_source
    rewritten = rewrite_python_source(body, variant="intel")
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import libcst as cst
    _LIBCST_AVAILABLE = True
except ImportError:  # noqa: BLE001
    _LIBCST_AVAILABLE = False
    cst = None  # type: ignore


# Variant → (device string, backend module name).
_VARIANT_MAP: Dict[str, Tuple[str, str]] = {
    "intel":  ("xpu", "xpu"),
    "rocm":   ("cuda", "cuda"),  # HIP uses torch.cuda under the hood on AMD
    "cpu":    ("cpu", "cpu"),
    "metal":  ("mps", "mps"),
    "npu":    ("cpu", "cpu"),   # NPU runtime doesn't use torch.device
}


# ---------------------------------------------------------------------
# Transformers.
# ---------------------------------------------------------------------

if _LIBCST_AVAILABLE:

    class CudaIsAvailableRewriter(cst.CSTTransformer):
        """`torch.cuda.is_available()` → `getattr(torch, "<bm>",
        torch.cuda).is_available()`. Preserves surrounding call chains
        and argument lists (is_available takes none, but future-proof)."""

        def __init__(self, backend_module: str):
            self._bm = backend_module

        def leave_Attribute(
            self, original_node, updated_node,
        ):
            # Target shape: Attribute(
            #   value=Attribute(value=Name('torch'), attr=Name('cuda')),
            #   attr=Name('is_available')
            # )
            if not isinstance(updated_node.attr, cst.Name) or \
                    updated_node.attr.value != "is_available":
                return updated_node
            inner = updated_node.value
            if not isinstance(inner, cst.Attribute):
                return updated_node
            if not (isinstance(inner.value, cst.Name)
                    and inner.value.value == "torch"):
                return updated_node
            if not (isinstance(inner.attr, cst.Name)
                    and inner.attr.value == "cuda"):
                return updated_node

            # No-op when backend_module is "cuda" — the HIP path uses
            # torch.cuda directly, so rewriting to getattr is a drag.
            if self._bm == "cuda":
                return updated_node

            # Build: getattr(torch, "<bm>", torch.cuda).is_available
            getattr_expr = cst.Call(
                func=cst.Name("getattr"),
                args=[
                    cst.Arg(value=cst.Name("torch")),
                    cst.Arg(value=cst.SimpleString(f'"{self._bm}"')),
                    cst.Arg(value=cst.Attribute(
                        value=cst.Name("torch"),
                        attr=cst.Name("cuda"),
                    )),
                ],
            )
            return updated_node.with_changes(
                value=getattr_expr,
            )


    class DeviceStringRewriter(cst.CSTTransformer):
        """Swap literal "cuda" strings inside device-related calls.
        Covers: `.to("cuda")`, `.device("cuda")`, `torch.device("cuda")`,
        `torch.zeros(..., device="cuda")`, factory functions with
        `device=` kwargs.

        Conservative: does NOT rewrite every bare "cuda" string —
        that would false-positive on docstrings, URLs, or arbitrary
        user strings that happen to say "cuda".
        """

        def __init__(self, device_str: str):
            self._device = device_str
            self._in_call_stack: List[cst.Call] = []

        def visit_Call(self, node: cst.Call) -> None:
            self._in_call_stack.append(node)

        def leave_Call(self, original_node, updated_node):
            self._in_call_stack.pop()
            return updated_node

        def leave_Arg(
            self, original_node, updated_node,
        ):
            """When the enclosing call is a device-related method
            (.to, .device, .cuda, torch.device, torch factory with
            device=), rewrite literal "cuda" string args."""
            if not self._in_call_stack:
                return updated_node
            call = self._in_call_stack[-1]
            if not self._is_device_context(call, original_node):
                return updated_node
            value = updated_node.value
            if isinstance(value, cst.SimpleString):
                stripped = value.value.strip('"').strip("'")
                if stripped == "cuda":
                    quote = value.value[0]
                    return updated_node.with_changes(
                        value=cst.SimpleString(f"{quote}{self._device}{quote}"),
                    )
            return updated_node

        def _is_device_context(
            self, call: cst.Call, arg: cst.Arg,
        ) -> bool:
            func = call.func
            # .to(...), .cuda(...), .device(...) — covers both
            # `model.to("cuda")` and `torch.device("cuda")`.
            if isinstance(func, cst.Attribute):
                attr_name = func.attr.value if isinstance(func.attr, cst.Name) else ""
                if attr_name in ("to", "cuda", "device"):
                    return True
                # factory functions with device= kwarg
                if attr_name in _FACTORY_FUNCS and self._is_torch_attr(func):
                    return arg.keyword is not None and \
                        isinstance(arg.keyword, cst.Name) and \
                        arg.keyword.value == "device"
            elif isinstance(func, cst.Name):
                # bare device(...) — uncommon but tolerable.
                if func.value == "device":
                    return True
            return False

        @staticmethod
        def _is_torch_attr(attr: cst.Attribute) -> bool:
            v = attr.value
            return isinstance(v, cst.Name) and v.value == "torch"


    class CudaCallRewriter(cst.CSTTransformer):
        """`.cuda()` → `.to(_DEVICE)` where _DEVICE is the variant's
        device string. Handles chained calls — `model.cuda().eval()`
        rewrites to `model.to("xpu").eval()`."""

        def __init__(self, device_str: str):
            self._device = device_str

        def leave_Call(self, original_node, updated_node):
            func = updated_node.func
            if not isinstance(func, cst.Attribute):
                return updated_node
            if not (isinstance(func.attr, cst.Name) and func.attr.value == "cuda"):
                return updated_node
            # Only rewrite when `.cuda()` is called with no positional
            # device index — `.cuda(0)` / `.cuda(device_id)` already
            # encodes a device index which `.to(...)` handles
            # differently. Leave those to the operator.
            if updated_node.args:
                return updated_node
            # Build `.to("<device>")`
            return cst.Call(
                func=cst.Attribute(
                    value=func.value,
                    attr=cst.Name("to"),
                ),
                args=[
                    cst.Arg(value=cst.SimpleString(f'"{self._device}"')),
                ],
            )


    class AmpAutocastRewriter(cst.CSTTransformer):
        """Rewrite the `torch.cuda.amp.autocast` attribute reference
        to `torch.amp.autocast` + pass the device as first arg.

        Target patterns (both get rewritten):
          `with torch.cuda.amp.autocast():`
              → `with torch.amp.autocast("<device>"):`
          `@torch.cuda.amp.autocast()`
              → `@torch.amp.autocast("<device>")`

        Does NOT recursively rewrite usages inside nested `with`
        blocks — libcst's transformer walks the tree for us and
        each autocast() call site is processed independently.
        """

        def __init__(self, device_str: str):
            self._device = device_str

        def leave_Call(self, original_node, updated_node):
            # Shape: Call(func=Attribute(
            #   value=Attribute(
            #     value=Attribute(value=Name('torch'), attr=Name('cuda')),
            #     attr=Name('amp')),
            #   attr=Name('autocast')))
            func = updated_node.func
            if not self._is_cuda_amp_autocast(func):
                return updated_node
            # Build: torch.amp.autocast("<device>", <existing_args>)
            new_func = cst.Attribute(
                value=cst.Attribute(
                    value=cst.Name("torch"),
                    attr=cst.Name("amp"),
                ),
                attr=cst.Name("autocast"),
            )
            # Insert the device arg as first positional; retain any
            # existing args (dtype=..., enabled=..., etc.)
            device_arg = cst.Arg(value=cst.SimpleString(f'"{self._device}"'))
            existing = list(updated_node.args)
            # Set comma on the new arg when there are existing args.
            if existing:
                device_arg = device_arg.with_changes(
                    comma=cst.Comma(
                        whitespace_after=cst.SimpleWhitespace(" "),
                    ),
                )
            return updated_node.with_changes(
                func=new_func,
                args=[device_arg] + existing,
            )

        @staticmethod
        def _is_cuda_amp_autocast(func) -> bool:
            if not isinstance(func, cst.Attribute):
                return False
            if not (isinstance(func.attr, cst.Name) and func.attr.value == "autocast"):
                return False
            amp = func.value
            if not isinstance(amp, cst.Attribute):
                return False
            if not (isinstance(amp.attr, cst.Name) and amp.attr.value == "amp"):
                return False
            cuda = amp.value
            if not isinstance(cuda, cst.Attribute):
                return False
            if not (isinstance(cuda.attr, cst.Name) and cuda.attr.value == "cuda"):
                return False
            if not (isinstance(cuda.value, cst.Name) and cuda.value.value == "torch"):
                return False
            return True

else:  # pragma: no cover — the tests skip when libcst is unavailable

    CudaIsAvailableRewriter = None  # type: ignore
    DeviceStringRewriter = None  # type: ignore
    CudaCallRewriter = None  # type: ignore
    AmpAutocastRewriter = None  # type: ignore


# Subset of torch factory functions that accept `device=` kwargs.
# Not exhaustive — covers the common cases in HF inference code.
_FACTORY_FUNCS = {
    "zeros", "ones", "empty", "full", "rand", "randn", "randint",
    "arange", "linspace", "tensor", "as_tensor",
}


# ---------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------

def rewrite_python_source(body: str, *, variant: str) -> str:
    """Apply the full Phase 7 transformer chain to `body` and return
    the rewritten source. Preserves whitespace, comments, blank lines,
    and trailing newlines.

    Returns `body` unchanged when:
      * libcst is unavailable (ImportError path),
      * the variant isn't in _VARIANT_MAP,
      * parsing fails (syntax error),
      * the variant's device doesn't need rewriting (cuda itself).

    Silent no-op on unsupported shapes — callers rely on the regex
    recipes as a safety net.
    """
    if not _LIBCST_AVAILABLE:
        return body
    if variant not in _VARIANT_MAP:
        return body
    if not body or not body.strip():
        return body
    device_str, backend_module = _VARIANT_MAP[variant]

    try:
        module = cst.parse_module(body)
    except cst.ParserSyntaxError:
        # Malformed source (e.g. an operator's in-progress edit) —
        # don't mangle it; fall back to the original bytes.
        return body
    except Exception:  # noqa: BLE001
        logger.exception("py_rewriter: unexpected parse error")
        return body

    transformers = [
        CudaIsAvailableRewriter(backend_module),
        DeviceStringRewriter(device_str),
        CudaCallRewriter(device_str),
        AmpAutocastRewriter(device_str),
    ]
    new_module = module
    for t in transformers:
        new_module = new_module.visit(t)
    return new_module.code


def round_trip_is_identity(body: str) -> bool:
    """Test helper — parse → serialize == original. Guards against
    libcst grammar drift that corrupts untouched files."""
    if not _LIBCST_AVAILABLE:
        return True
    try:
        module = cst.parse_module(body)
    except cst.ParserSyntaxError:
        # Syntax-error input is never "identity" — but the rewriter
        # short-circuits on parse failures, so identity preservation
        # isn't meaningful here.
        return True
    return module.code == body


__all__ = [
    "rewrite_python_source",
    "round_trip_is_identity",
    "CudaIsAvailableRewriter",
    "DeviceStringRewriter",
    "CudaCallRewriter",
    "AmpAutocastRewriter",
]
