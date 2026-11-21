#!/usr/bin/env python3
"""API convention lint gate — SKELETON.

Static (AST) scan of `CORE/API/routers/` that checks the canonical conventions
so the cleanup can't silt up again (D8: "enforce so it can't regrow"). It is
REPORT-ONLY / non-failing by default — it prints findings and exits 0 — until the
codebase has been migrated onto the conventions and someone flips the gate on in
CI. Nothing here is wired into CI yet.

Rules checked (each independently reportable):

    R1  max endpoints per router file            (--max-endpoints, default 40)
    R2  every endpoint has an auth dependency     (Depends(<auth dep>) on the
                                                   route, or a router-level
                                                   dependency)
    R3  every endpoint declares a response_model
    R4  no duplicate (METHOD, path) registration  (path = router prefix + route)
    R5  routers contain no raw `db.query(`        (must go through crud/service; a
                                                   lock-order/commit-boundary critical
                                                   site opts out with
                                                   `# r5-lock-ok: <reason>` on the
                                                   `db.query(` line or the line above)
    R6  HTTPException detail leaks raw exception   (GSEC R-019: `detail=f"...{e}..."`
        text                                      / `detail=str(e)` / `detail=e` must
        use `utils.http_errors.internal_http_error` instead)
    R7  raw requests/httpx fetch on a non-literal URL (a server-side
        fetch whose URL is a variable must go through utils/safe_http.py; a
        reviewed call site opts out with `# ssrf-ok: <reason>` on its call)

Usage:
    python -m tools.api_lint --report            # human summary (exit 0)
    python -m tools.api_lint --report --json      # machine-readable findings
    python -m tools.api_lint --strict             # exit 1 if any finding (future CI)
    python -m tools.api_lint --report --only R4,R5
    python -m tools.api_lint --routers-dir /app/routers
    python -m tools.api_lint --write-baseline tools/api_lint_baseline.json
    python -m tools.api_lint --strict --baseline tools/api_lint_baseline.json

Baseline (ratchet): `--write-baseline PATH` records the current findings keyed by
(rule, file basename, message) with a per-key count; line numbers are ignored so
unrelated edits don't churn it. `--baseline PATH` suppresses up to that many
findings per key, so a NEW finding (including another instance of an existing
key, e.g. one more db.query in an already-baselined router) still surfaces and
fails `--strict`. Without `--baseline` behaviour is unchanged.

Design notes / known skeleton limitations (documented, not bugs):
    * Path prefixes: R4 resolves a route's full path from `APIRouter(prefix=...)`
      when present. Prefixes applied at `include_router(prefix=...)` in main.py
      are NOT resolved here, so cross-file R4 hits are *candidates* to confirm,
      not certainties (this is exactly how the audit surfaced the real duplicate
      `DELETE /resource-permissions/{id}`).
    * Auth (R2): recognised by a heuristic name match on the dependency callable
      (`require_*`, `*current_user*`, `*api_key*`, `*_token*`, `authenticated_*`,
      `access`).  Bound closures (e.g. ``_view = require_capability(...);
      Depends(_view)``) are resolved via AST.  The explicit opt-out marker
      ``# api-lint: public-by-design: <reason>`` placed on the line(s) immediately
      above a route decorator also suppresses the finding.
      Public-by-design endpoints that carry the marker (or truly need no auth)
      will show as R2 findings until triaged (audit D13/§ no-auth set), not
      auto-failed.
    * The scan never imports the routers (no side effects, no DB); pure AST. A
      file that fails to parse is reported as a finding, never a crash.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
from collections import Counter
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Config / heuristics
# ---------------------------------------------------------------------------

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
DEFAULT_MAX_ENDPOINTS = 40

# --- R7: raw requests/httpx fetch on a variable URL ----------
# The guarded client is utils/safe_http.py; every server-side fetch to a URL
# that is not a hardcoded literal must go through it (SSRF: private/loopback/
# link-local/metadata blocking after resolve + on every redirect, http(s) only,
# timeouts + size caps).
_FETCH_MODULE_NAMES = ("requests", "httpx")
_FETCH_VERBS = {
    "get", "post", "put", "patch", "delete", "head", "options",
    "request", "send", "stream",
}
# Constructors that bind a name later called with a URL (client.head(url)).
_FETCH_CLIENT_CTORS = {"Client", "AsyncClient", "Session", "HTTPClient"}
# The guard implementation itself + the service that delegates to it: calls
# there ARE the guarded path, never a bypass.
_GUARD_MODULE_PATHS = (
    "utils/safe_http.py",
    "services/ssrf_guard.py",
    "services/http_triggers.py",
)
# Inline opt-out the rule honours: `# ssrf-ok: <reason>` anywhere on the call.
_R7_MARKER = "# ssrf-ok:"
# R2 explicit opt-out: `# api-lint: public-by-design: <reason>` placed on the
# line immediately above a route decorator (or on a comment line directly before
# the decorator block; we walk upward from the decorator looking for it).
# The marker MUST include a colon and non-empty reason text after it — a bare
# marker (no reason) is rejected so lazy markers stay loud.
_R2_MARKER = "# api-lint: public-by-design:"
# R5 explicit opt-out: `# r5-lock-ok: <reason>` placed on the `db.query(` line
# itself or the line directly above it — for lock-order/commit-boundary
# critical sites that must NOT get the mechanical crud/service split. The
# marker MUST include non-empty reason text — a bare marker (no reason) is
# rejected so lazy markers stay loud.
_R5_MARKER = "# r5-lock-ok:"
# In-code allowlist for reviewed call sites that keep a raw client on a
# variable URL, keyed (file basename, enclosing function) so line edits don't
# churn it. Each entry carries its reason. Migrate the site to utils.safe_http
# and delete the entry.
# file basename -> {enclosing function -> reason}
_R7_ALLOWLIST: Dict[str, Dict[str, str]] = {
    # (file basename, enclosing function) -> why this raw fetch is acceptable
    # today. All are reviewed sites; migrate to utils.safe_http and drop the
    # entry once the guarded client lands on them.
    "admin_scim_activity.py": {  # type: ignore[assignment]
        "_validate_webhook":
            "tenant webhook HEAD probe; egress_policy.refuse_reason(CATEGORY_"
            "WEBHOOKS) already refuses the probe in sealed "
            "mode, and the URL is the stored webhook, not free-form input",
    },
    "ai_config.py": {
        "_weight_total_bytes":
            "HF GGUF size probe; URL is a fixed huggingface.co host with only the "
            "public repo slug interpolated — no host/scheme control",
    },
    "hf_tokens.py": {
        "test_hf_token":
            "HF token check; URL is the module constant _WHOAMI_URL (hardcoded "
            "https://huggingface.co/api/whoami-v2)",
    },
    "model_cache_admin.py": {
        "_proxy":
            "platform-admin model-cache proxy; base URL is the platform setting "
            "model_cache.endpoint (admin-set, single upstream), platform-admin "
            "auth required on the route",
        "import_model_cache_bundle":
            "same admin-only model-cache upstream as _proxy (bundle import leg)",
    },
    "sso_profile.py": {
        "_kc_admin_get":
            "Keycloak admin API GET; base URL is keycloak_admin_client.admin_api_"
            "url (deployment env, not tenant input); platform-admin route",
        "_kc_admin_delete":
            "same Keycloak admin API base as _kc_admin_get (DELETE leg)",
    },
}

# Substrings that mark a dependency callable as an auth/permission guard.
_AUTH_NAME_HINTS = (
    "require_",
    "current_user",
    "api_key",
    "_token",
    "authenticated",
    "access",  # api_conventions.access / authenticated_user
    "impersonat",
    "service_account",
    "build_task",
    "build_scope",
    "scim_auth",  # SCIM 2.0 service discovery auth dependency (scim_v2.py)
    "unregistered_user",  # onboarding: get_unregistered_user calls get_current_user_claims internally
    "node_or_service_principal",  # utils/run_task_auth.py — run-task token or platform-admin principal
)

ALL_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
RULE_TITLES = {
    "R1": "Too many endpoints in one router file",
    "R2": "Endpoint has no auth dependency",
    "R3": "Endpoint has no response_model",
    "R4": "Duplicate (method, path) registration (candidate)",
    "R5": "Router contains raw db.query( (bypasses crud/service; or mark the "
    "site `# r5-lock-ok: <reason>` on the db.query( line or the line above)",
    "R6": "HTTPException detail leaks raw exception text (use utils.http_errors.internal_http_error)",
    "R7": "Raw requests/httpx fetch on a variable URL (use utils.safe_http; or mark the call `# ssrf-ok: <reason>`)",
}

# GSEC R-019 — variable names that hold a caught exception, interpolated into
# `HTTPException(..., detail=...)`. Attribute forms (e.message / e.code /
# e.reason) are curated service-layer fields and are NOT flagged.
_EXCEPTION_VAR_NAMES = ("e", "exc", "err", "ex", "error")


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    message: str

    def as_dict(self) -> dict:
        return {"rule": self.rule, "file": self.file, "line": self.line, "message": self.message}

    def baseline_key(self) -> Tuple[str, str, str]:
        return (self.rule, os.path.basename(self.file), _norm_message(self.message))


_ALSO_AT_LINE = re.compile(r"(also at [\w.\-]+\.py):\d+")


def _norm_message(message: str) -> str:
    """Drop the OTHER file's line number from an R4 'also at X.py:NNN' message so
    a baseline key doesn't go stale when that partner file is edited."""
    return _ALSO_AT_LINE.sub(r"\1", message)


@dataclass
class Endpoint:
    file: str
    line: int
    method: str
    path: str            # legacy: file-aggregate prefix + route (R2/R3 messages)
    full_path: str = ""  # R4: this route's own router-variable prefix + route
    func: str = ""
    has_response_model: bool = False
    has_route_auth: bool = False


@dataclass
class FileScan:
    file: str
    endpoints: List[Endpoint] = field(default_factory=list)
    router_prefix: str = ""
    router_has_auth: bool = False
    db_query_lines: List[int] = field(default_factory=list)
    raw_exc_detail_lines: List[int] = field(default_factory=list)
    raw_fetch_lines: List[Tuple[int, str]] = field(default_factory=list)
    parse_error: Optional[str] = None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _depends_arg_name(node: ast.AST) -> Optional[str]:
    """Return the callable name inside a `Depends(...)` expression, or None if
    `node` is not a Depends(...) call. Handles `Depends(x)`, `Depends(x())`,
    `Depends(mod.x)`, `Depends(mod.x(...))`."""
    if not (isinstance(node, ast.Call) and _callable_name(node.func) in ("Depends",)):
        return None
    if not node.args:
        return None
    inner = node.args[0]
    if isinstance(inner, ast.Call):
        inner = inner.func
    return _callable_name(inner)


def _callable_name(node: ast.AST) -> str:
    """Best-effort dotted/simple name for a call target or reference."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _callable_name(node.func)
    return ""


def _is_auth_dep_name(name: Optional[str]) -> bool:
    if not name:
        return False
    low = name.lower()
    return any(hint in low for hint in _AUTH_NAME_HINTS)


def _closure_auth_names(tree: ast.AST) -> set:
    """Walk the AST and collect module-level variable names bound to a call whose
    callable itself matches `_AUTH_NAME_HINTS` (e.g. ``_view = require_capability(...)``).
    Returns a set of target name strings.
    """
    names: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        val = node.value
        if not isinstance(val, ast.Call):
            continue
        if _is_auth_dep_name(_callable_name(val.func)):
            names.add(node.targets[0].id)
    return names


def _str_const(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _decorator_route(dec: ast.AST) -> Optional[Tuple[str, str, bool, Optional[str]]]:
    """If `dec` is `@<router_var>.<method>(path, ..., response_model=...)`,
    return (method, path, has_response_model, router_var); else None.
    router_var is None when the decorator owner is not a plain name."""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    method = dec.func.attr.lower()
    if method not in HTTP_METHODS:
        return None
    path = ""
    if dec.args:
        path = _str_const(dec.args[0]) or ""
    has_rm = any(kw.arg == "response_model" for kw in dec.keywords)
    owner = dec.func.value.id if isinstance(dec.func.value, ast.Name) else None
    return method, path, has_rm, owner


def _decorator_route_has_auth(dec: ast.Call, closure_auth_names: set | None = None) -> bool:
    """True if the route decorator itself carries `dependencies=[Depends(auth)]`."""
    if closure_auth_names is None:
        closure_auth_names = set()
    for kw in dec.keywords:
        if kw.arg == "dependencies" and isinstance(kw.value, (ast.List, ast.Tuple)):
            for elt in kw.value.elts:
                darg = _depends_arg_name(elt)
                if darg in closure_auth_names or _is_auth_dep_name(darg):
                    return True
    return False


def _func_params_have_auth(func: ast.AST, closure_auth_names: set | None = None) -> bool:
    """True if any parameter default is `Depends(<auth dep>)`."""
    if closure_auth_names is None:
        closure_auth_names = set()
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    args = func.args
    defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
    for d in defaults:
        darg = _depends_arg_name(d)
        if darg in closure_auth_names or _is_auth_dep_name(darg):
            return True
    return False


def _router_meta(tree: ast.AST) -> Tuple[str, bool]:
    """Aggregate (prefix, has_router_level_auth) for a file, used for the
    legacy single-prefix behaviour (R2 auth, R2/R3 message text). `prefix` is
    the last non-empty `APIRouter(prefix=...)` in the file; `has_auth` is True
    if ANY router carries a router-level auth dependency. NOTE: R4 does NOT use
    this — it resolves each route against its own router variable via
    `_router_prefixes`, so files with several routers don't collide.

    Uses `_closure_auth_names` so that router-level dependencies bound via
    `require_capability` closures (e.g. ``_view = require_capability(...);
    router = APIRouter(prefix=..., dependencies=[Depends(_view)])``) are caught.
    """
    closure = _closure_auth_names(tree)
    prefix = ""
    has_auth = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if not (isinstance(val, ast.Call) and _callable_name(val.func) == "APIRouter"):
            continue
        for kw in val.keywords:
            if kw.arg == "prefix":
                prefix = _str_const(kw.value) or prefix
            if kw.arg == "dependencies" and isinstance(kw.value, (ast.List, ast.Tuple)):
                for elt in kw.value.elts:
                    darg = _depends_arg_name(elt)
                    if darg in closure or _is_auth_dep_name(darg):
                        has_auth = True
    return prefix, has_auth


def _router_prefixes(tree: ast.AST) -> Dict[str, str]:
    """Map each `<name> = APIRouter(prefix=...)` binding to its own prefix
    ("" when unset). Used only by R4, so sibling routers in one file are
    compared on their full, distinct paths. Last write of a name wins."""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _callable_name(node.value.func) == "APIRouter"
        ):
            continue
        pfx = ""
        for kw in node.value.keywords:
            if kw.arg == "prefix":
                pfx = _str_const(kw.value) or pfx
        out[node.targets[0].id] = pfx
    return out


def _url_arg_node(call: ast.Call) -> Optional[ast.AST]:
    """The URL argument of a fetch call: first positional string-ish arg, or the
    `url=` keyword. Returns None when no URL argument exists."""
    for kw in call.keywords:
        if kw.arg in ("url", "uri"):
            return kw.value
    for arg in call.args:
        if isinstance(arg, ast.Constant) and not isinstance(arg.value, str):
            continue  # `client.request("POST", url)` -> skip the method literal
        return arg
    return None


def _url_is_literal(node: ast.AST) -> bool:
    """True when a URL argument is a hardcoded string (a plain literal, or an
    f-string with no interpolated expressions — both are fixed at the call site,
    so there is no attacker-controlled URL to guard)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return not any(isinstance(p, ast.FormattedValue) for p in node.values)
    return False


def _enclosing_func(tree: ast.AST) -> Dict[int, str]:
    """lineno -> innermost enclosing function name ('' = module level)."""
    out: Dict[int, str] = {}

    def walk(node: ast.AST, name: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                out[getattr(child, "lineno", 0)] = name
            walk(child, name)

    walk(tree, "")
    return out


def _has_r5_marker(source_lines: list, query_lineno: int) -> bool:
    """Return True if `# r5-lock-ok: <reason>` appears on the `db.query(` line
    itself or the line directly above it, with non-empty reason text after the
    marker. A bare marker (no reason) does NOT exempt (fail-loud)."""
    for i in (query_lineno - 1, query_lineno - 2):  # 0-based: same line, then line above
        if i < 0 or i >= len(source_lines):
            continue
        line = source_lines[i]
        idx = line.find(_R5_MARKER)
        if idx == -1:
            continue
        rest = line[idx + len(_R5_MARKER):].strip()
        if rest:  # non-empty reason -> valid opt-out
            return True
    return False


def _has_db_query(tree: ast.AST, source_lines: Optional[list] = None) -> List[int]:
    """Line numbers of any `<x>.query(` call whose object is a `db`/`session`
    name — the inline-data-access anti-pattern (D9). Skips a site whose
    `db.query(` line or the line directly above it carries the documented
    `# r5-lock-ok: <reason>` opt-out (see `_has_r5_marker`)."""
    lines: List[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "query"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("db", "session", "self")  # self covers db held on an obj
        ):
            if source_lines is not None and _has_r5_marker(source_lines, node.lineno):
                continue  # reviewed, documented lock-order/commit-boundary exception
            lines.append(node.lineno)
    return lines


def _detail_leaks_exception(node: ast.expr) -> bool:
    """True if a `detail=` value stringifies a caught exception *as raw text*
    (GSEC R-019). Flags:
        detail=f"... {e} ..." / detail=f"... {e!s} ..."   (f-string of the name)
        detail=str(e)                                      (str of the name)
        detail=e                                           (the bare name)
    Deliberately NOT flagged:
        detail=e.message / e.code / e.reason               (curated attribute)
        detail=f"... HTTP {resp.status_code}"              (no exc-name interpolation)
    Concatenations / BinOps that interpolate the exc name are still caught,
    because we walk the whole detail subtree for the exc-name forms."""
    if isinstance(node, ast.Name):
        return node.id in _EXCEPTION_VAR_NAMES
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "str":
            if child.args and isinstance(child.args[0], ast.Name) and child.args[0].id in _EXCEPTION_VAR_NAMES:
                return True
        elif isinstance(child, ast.FormattedValue):
            v = child.value
            if isinstance(v, ast.Name) and v.id in _EXCEPTION_VAR_NAMES:
                return True
    return False


def _raw_exc_detail_lines(tree: ast.AST) -> List[int]:
    """Line numbers of `HTTPException(..., detail=<raw exception text>)` calls
    that leak a caught exception to the client. Uses of
    `utils.http_errors.internal_http_error` do not appear here (different
    call name), so the rule self-satisfies after a fix."""
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
        if callee not in ("HTTPException", "StackVariableError"):
            continue
        for kw in node.keywords:
            if kw.arg == "detail" and _detail_leaks_exception(kw.value):
                lines.append(node.lineno)
                break
    return lines


def _is_guard_module(path: str) -> bool:
    """True if `path` is one of the SSRF guard modules (the guarded client and
    the service that delegates to it) — raw fetches there are the guarded path."""
    norm = os.path.normpath(path).replace("\\", "/")
    return any(norm.endswith(g) for g in _GUARD_MODULE_PATHS)


def _raw_fetch_lines(tree: ast.AST, source_lines: List[str]) -> List[Tuple[int, str]]:
    """R7: (lineno, enclosing-func) for every raw requests/httpx
    fetch whose URL is NOT a hardcoded string. Covers both the module-qualified
    form (`requests.get(url)`, `httpx.post(url)`) and the client form, where a
    client is built first and the URL arrives at the later call
    (`client = httpx.Client(); client.request("POST", url)` /
    `with httpx.Client() as c: c.head(url)`).

    Suppressed (never reported) when:
      * the URL is a literal (plain string, or an f-string with no holes);
      * the file IS the guard (utils/safe_http.py, services/ssrf_guard.py,
        services/http_triggers.py) — those calls are the guarded path;
      * the call carries the inline opt-out `# ssrf-ok: <reason>` on any line of
        its span (a human-reviewed site);
      * (file basename, enclosing function) is in _R7_ALLOWLIST with a reason.
    """
    # Names bound to a raw client/Session, so `name.<verb>(url)` is caught too.
    client_names: set = set()
    for node in ast.walk(tree):
        ctor = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            ctor = node.value
            targets = [t for t in [node.targets[0]] if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.With, ast.AsyncWith)) and node.items:
            ctor = node.items[0].context_expr
            targets = [it.optional_vars for it in node.items
                       if isinstance(it.optional_vars, ast.Name)]
        else:
            continue
        if isinstance(ctor, ast.Call) and _callable_name(ctor.func) in _FETCH_CLIENT_CTORS:
            for t in targets:
                client_names.add(t.id)

    funcs = _enclosing_func(tree)
    hits: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        verb = ""
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id in _FETCH_MODULE_NAMES and f.attr in _FETCH_VERBS:
                verb = f.attr                                   # requests.get(...)
            elif f.value.id in client_names and f.attr in _FETCH_VERBS:
                verb = f.attr                                   # client.head(...)
        if not verb:
            continue
        url = _url_arg_node(node)
        if url is None or _url_is_literal(url):
            continue
        lo = getattr(node, "lineno", 0)
        hi = getattr(node, "end_lineno", lo) or lo
        if any(_R7_MARKER in (source_lines[i] if i < len(source_lines) else "")
               for i in range(lo - 1, hi)):
            continue                                            # inline opt-out
        hits.append((lo, funcs.get(lo, "")))
    return hits


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _has_r2_marker(source_lines: list, dec_lineno: int) -> bool:
    """Return True if an `# api-lint: public-by-design: <reason>` marker is found
    on any line immediately preceding the decorator at `dec_lineno`.

    Walks upward from the decorator line (exclusive) through consecutive comment
    and blank lines. The marker must include colon-separated reason text
    (``: <text>``); a bare ``# api-lint: public-by-design`` with no reason
    after the trailing colon is rejected (fail-loud).
    """
    for i in range(dec_lineno - 2, -1, -1):  # dec_lineno is 1-based; -2 for 0-based
        line = source_lines[i].strip()
        if not line:
            continue  # blank line; keep walking
        if line.startswith("#"):
            if line.startswith(_R2_MARKER):
                # Must have reason text after the marker prefix.
                rest = line[len(_R2_MARKER):].strip()
                if rest:  # non-empty reason → valid opt-out
                    return True
                else:
                    break  # bare marker → reject, stop walking
            # Still a comment line — keep walking
            continue
        # Non-comment, non-blank: we've left the comment block → stop
        break
    return False


def scan_file(path: str) -> FileScan:
    """Parse one router file into a FileScan. Never raises — a parse failure is
    recorded on the scan."""
    scan = FileScan(file=path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
    except Exception as exc:  # unreadable / unparseable -> reportable, not fatal
        scan.parse_error = f"{type(exc).__name__}: {exc}"
        return scan

    scan.router_prefix, scan.router_has_auth = _router_meta(tree)
    prefix_by_owner = _router_prefixes(tree)
    source_lines = source.splitlines()
    scan.db_query_lines = _has_db_query(tree, source_lines)
    scan.raw_exc_detail_lines = _raw_exc_detail_lines(tree)
    # R7: the guard modules themselves (and anything that delegates to them)
    # hold the raw client on a variable URL by design — never a bypass.
    if not _is_guard_module(path):
        scan.raw_fetch_lines = _raw_fetch_lines(tree, source_lines)

    closure_auth_names = _closure_auth_names(tree)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            route = _decorator_route(dec)
            if route is None:
                continue
            method, route_path, has_rm, owner = route
            route_auth = (
                _decorator_route_has_auth(dec, closure_auth_names)
                or _func_params_have_auth(node, closure_auth_names)
                or _has_r2_marker(source_lines, dec.lineno)
            )
            # `path` keeps the legacy file-aggregate prefix so R2/R3 messages
            # are unchanged; `full_path` resolves THIS route against its own
            # router variable so sibling routers in one file (e.g. unprefixed
            # `_endpoints` + `service_router` with prefix=/x) are compared in
            # R4 on their full, distinct paths rather than colliding.
            own_prefix = prefix_by_owner.get(owner, scan.router_prefix)
            scan.endpoints.append(
                Endpoint(
                    file=path,
                    line=node.lineno,
                    method=method.upper(),
                    path=(scan.router_prefix + route_path),
                    full_path=(own_prefix + route_path),
                    func=node.name,
                    has_response_model=has_rm,
                    has_route_auth=route_auth,
                )
            )
    return scan


def collect_findings(scans: List[FileScan], *, max_endpoints: int) -> List[Finding]:
    findings: List[Finding] = []
    seen: Dict[Tuple[str, str], Endpoint] = {}

    for scan in scans:
        rel = scan.file
        if scan.parse_error:
            findings.append(Finding("R0", rel, 0, f"parse error: {scan.parse_error}"))
            continue

        # R1 — file size
        if len(scan.endpoints) > max_endpoints:
            findings.append(
                Finding(
                    "R1",
                    rel,
                    1,
                    f"{len(scan.endpoints)} endpoints (> {max_endpoints}); split along sub-domain seams",
                )
            )

        # R5 — inline db.query
        for ln in scan.db_query_lines:
            findings.append(
                Finding(
                    "R5",
                    rel,
                    ln,
                    "raw db.query( in a router; move into crud/service"
                    " (or mark the site `# r5-lock-ok: <reason>` on the db.query( line"
                    " or the line above, for a documented lock-order/commit-boundary exception)",
                )
            )

        # R6 — GSEC R-019: raw exception text in an HTTPException detail.
        for ln in scan.raw_exc_detail_lines:
            findings.append(
                Finding(
                    "R6",
                    rel,
                    ln,
                    "HTTPException detail leaks raw exception text; use utils.http_errors.internal_http_error",
                )
            )

        # R7 — raw requests/httpx fetch on a variable URL.
        for ln, fname in scan.raw_fetch_lines:
            if _R7_ALLOWLIST.get(os.path.basename(rel), {}).get(fname) is not None:
                continue  # reviewed site, reason recorded in the allowlist
            findings.append(
                Finding(
                    "R7",
                    rel,
                    ln,
                    f"raw requests/httpx fetch on a variable URL in {fname or '<module>'}"
                    " (use utils.safe_http, or mark the call `# ssrf-ok: <reason>`)",
                )
            )

        for ep in scan.endpoints:
            # R2 — auth
            if not (ep.has_route_auth or scan.router_has_auth):
                findings.append(
                    Finding("R2", rel, ep.line, f"{ep.method} {ep.path or '?'} ({ep.func}) has no auth dependency")
                )
            # R3 — response_model
            if not ep.has_response_model:
                findings.append(
                    Finding("R3", rel, ep.line, f"{ep.method} {ep.path or '?'} ({ep.func}) has no response_model")
                )
            # R4 — duplicate (method, path). Compare on `full_path` (each
            # route's own router-variable prefix + route), not `path` (file
            # aggregate), so a file's sibling routers with distinct prefixes
            # don't collide. Empty paths can't be compared.
            r4_path = ep.full_path or ep.path
            if r4_path:
                key = (ep.method, r4_path)
                prior = seen.get(key)
                if prior is not None:
                    findings.append(
                        Finding(
                            "R4",
                            rel,
                            ep.line,
                            f"{ep.method} {r4_path} also at {os.path.basename(prior.file)}:{prior.line} "
                            f"({prior.func}) — confirm (prefixes from include_router not resolved)",
                        )
                    )
                else:
                    seen[key] = ep

    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_routers_dir() -> str:
    # tools/api_lint.py -> CORE/API/tools -> CORE/API/routers
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "routers")


def _iter_router_files(routers_dir: str) -> List[str]:
    out: List[str] = []
    for name in sorted(os.listdir(routers_dir)):
        if name.endswith(".py") and not name.startswith("__"):
            out.append(os.path.join(routers_dir, name))
    return out


def run(routers_dir: str, *, max_endpoints: int, only: Optional[set]) -> Tuple[List[Finding], List[FileScan]]:
    files = _iter_router_files(routers_dir)
    scans = [scan_file(f) for f in files]
    findings = collect_findings(scans, max_endpoints=max_endpoints)
    if only:
        findings = [f for f in findings if f.rule in only]
    return findings, scans


BASELINE_VERSION = 1


def build_baseline(findings: List[Finding]) -> dict:
    counts = Counter(f.baseline_key() for f in findings)
    entries = [
        {"rule": rule, "file": fname, "message": msg, "count": n}
        for (rule, fname, msg), n in sorted(counts.items())
    ]
    return {"version": BASELINE_VERSION, "count": sum(counts.values()), "entries": entries}


def write_baseline(findings: List[Finding], path: str) -> dict:
    data = build_baseline(findings)
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    if path == "-":
        sys.stdout.write(payload)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(payload)
    return data


def load_baseline(path: str) -> Counter:
    """Read a baseline file into a Counter keyed by (rule, file basename, message).
    Raises ValueError on a malformed file so a broken baseline can't silently
    suppress nothing (or everything)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: not an api_lint baseline (missing 'entries' list)")
    allowed: Counter = Counter()
    for i, e in enumerate(data["entries"]):
        try:
            key = (str(e["rule"]), os.path.basename(str(e["file"])), _norm_message(str(e["message"])))
            n = int(e.get("count", 1))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: bad entry #{i}: {exc}") from exc
        if n > 0:
            allowed[key] += n
    return allowed


def apply_baseline(findings: List[Finding], allowed: Counter) -> Tuple[List[Finding], List[Finding]]:
    """Split findings into (new, baselined). Each key suppresses at most its
    baselined count; the excess is reported as new."""
    budget = Counter(allowed)
    new: List[Finding] = []
    suppressed: List[Finding] = []
    for f in findings:
        k = f.baseline_key()
        if budget[k] > 0:
            budget[k] -= 1
            suppressed.append(f)
        else:
            new.append(f)
    return new, suppressed


def _print_report(
    findings: List[Finding],
    scans: List[FileScan],
    routers_dir: str,
    *,
    baselined: Optional[int] = None,
) -> None:
    total_endpoints = sum(len(s.endpoints) for s in scans)
    by_rule: Dict[str, List[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)

    print("=" * 72)
    print("API convention lint — REPORT ONLY (non-failing skeleton)")
    print("=" * 72)
    print(f"scanned : {len(scans)} router files, {total_endpoints} endpoints")
    print(f"dir     : {routers_dir}")
    print(f"findings: {len(findings)}")
    if baselined is not None:
        print(f"baselined (suppressed): {baselined}")
    print("-" * 72)
    print(f"{'rule':<5} {'count':>6}  description")
    for rule in ["R0", *ALL_RULES]:
        hits = by_rule.get(rule, [])
        if rule == "R0" and not hits:
            continue
        title = "file failed to parse" if rule == "R0" else RULE_TITLES.get(rule, rule)
        # count routers affected for file-scoped rules for a truer picture
        print(f"{rule:<5} {len(hits):>6}  {title}")
    print("-" * 72)

    # Per-rule top offenders (files), capped so the report stays readable.
    for rule in ["R0", *ALL_RULES]:
        hits = by_rule.get(rule, [])
        if not hits:
            continue
        per_file: Dict[str, int] = {}
        for h in hits:
            per_file[os.path.basename(h.file)] = per_file.get(os.path.basename(h.file), 0) + 1
        top = sorted(per_file.items(), key=lambda kv: kv[1], reverse=True)[:10]
        title = "file failed to parse" if rule == "R0" else RULE_TITLES.get(rule, rule)
        print(f"\n[{rule}] {title} — top files:")
        for fname, count in top:
            print(f"    {count:>4}  {fname}")
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="API convention lint gate (skeleton, report-only).")
    parser.add_argument("--routers-dir", default=_default_routers_dir(), help="Directory of router .py files.")
    parser.add_argument("--max-endpoints", type=int, default=DEFAULT_MAX_ENDPOINTS, help="R1 threshold.")
    parser.add_argument("--report", action="store_true", help="Print a human-readable report (default action).")
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON.")
    parser.add_argument("--only", default="", help="Comma-separated rule ids to include, e.g. R4,R5.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any (non-baselined) finding. Default is exit 0.",
    )
    parser.add_argument(
        "--baseline",
        metavar="PATH",
        default=None,
        help="Baseline JSON; findings recorded there are suppressed (per-key count).",
    )
    parser.add_argument(
        "--write-baseline",
        metavar="PATH",
        default=None,
        help="Write the current findings as a baseline JSON to PATH ('-' = stdout) and exit 0.",
    )
    args = parser.parse_args(argv)

    only = {r.strip().upper() for r in args.only.split(",") if r.strip()} or None
    findings, scans = run(args.routers_dir, max_endpoints=args.max_endpoints, only=only)

    if args.write_baseline:
        data = write_baseline(findings, args.write_baseline)
        print(
            f"api_lint: wrote baseline ({data['count']} findings, {len(data['entries'])} keys) "
            f"to {args.write_baseline}",
            file=sys.stderr,
        )
        return 0

    baselined: Optional[List[Finding]] = None
    if args.baseline:
        try:
            allowed = load_baseline(args.baseline)
        except (OSError, ValueError) as exc:
            print(f"api_lint: cannot load baseline: {exc}", file=sys.stderr)
            return 2
        findings, baselined = apply_baseline(findings, allowed)

    if args.json:
        out = {"findings": [f.as_dict() for f in findings], "count": len(findings)}
        if baselined is not None:
            out["baselined"] = len(baselined)
        print(json.dumps(out, indent=2))
    else:
        _print_report(
            findings,
            scans,
            args.routers_dir,
            baselined=None if baselined is None else len(baselined),
        )

    if args.strict and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
