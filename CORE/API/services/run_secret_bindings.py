"""PERRUN-BINDINGS-SLICE-1 — run-body ``secret_bindings`` (named secrets).

A run may choose WHICH stored secret a node gets for this run only:
``secret_bindings = {<ref env_var or binding_name>: <stored secret NAME>}``.

Rules:
  * only a graph ``secret_refs`` entry marked ``"bindable": true`` with a
    non-empty ``"bindable_prefix"`` accepts a binding, and only for names that
    start with that prefix; anything else is a 422 naming the ref (the key),
    never a secret value;
  * the values here are secret NAMES. Plaintext never passes through this
    module; the resolver (routers/worker_tasks.py resolve_task_secrets) is
    unchanged and still returns bytes only inside the encrypted envelope;
  * the chosen name is written into the node's ``secret_refs`` at dispatch, so
    the resolver (which trusts the task payload) sees it;
  * tenant isolation is the existing name lookup: a name that belongs to
    another tenant is indistinguishable from a missing name (the access check
    returns it as missing).

Pure helpers, no DB and no model imports (safe to import on its own).
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Optional

MAX_BINDINGS = 20
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

# Persisted (names only) key inside ``project_info`` of the run record.
PERSISTED_KEY = "secret_binding_names"
REQUEST_KEY = "secret_bindings"


class BindingError(Exception):
    """A binding the graph does not allow. ``ref`` is the binding KEY (the
    node's env var or binding name), never a secret value."""

    def __init__(self, ref: str, reason: str):
        self.ref = ref
        self.reason = reason
        super().__init__(f"secret binding for '{ref}' rejected: {reason}")

    def detail(self) -> Dict[str, Any]:
        return {"code": "secret_binding_invalid", "ref": self.ref,
                "message": f"Secret binding for '{self.ref}' is not allowed ({self.reason}).",
                "reason": self.reason}


def validate_bindings_shape(value: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Strict shape check (pydantic field validator). Empty -> None."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("secret_bindings must be an object of name strings")
    if not value:
        return None
    if len(value) > MAX_BINDINGS:
        raise ValueError(f"secret_bindings accepts at most {MAX_BINDINGS} entries")
    for key, name in value.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            raise ValueError("secret_bindings has an invalid binding key")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"secret_bindings entry '{key}' has an invalid secret name")
    return dict(value)


def _ref_key(ref: Dict[str, Any]) -> Optional[str]:
    k = ref.get("binding_name") or ref.get("env_var")
    return k if isinstance(k, str) and k else None


def _is_named_secret_ref(ref: Any) -> bool:
    """A user named-secret ref (not an OAuth connection ref, not a build ref)."""
    return (isinstance(ref, dict)
            and not ref.get("oauth_connection_id") and not ref.get("source"))


def _is_bindable(ref: Any) -> bool:
    if not _is_named_secret_ref(ref) or ref.get("bindable") is not True:
        return False
    pfx = ref.get("bindable_prefix")
    return isinstance(pfx, str) and bool(pfx)


def _iter_refs(graph: Optional[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for node in ((graph or {}).get("nodes") or []):
        for ref in (((node or {}).get("data") or {}).get("secret_refs") or []):
            if isinstance(ref, dict):
                yield ref


def validate_against_graph(graph: Optional[Dict[str, Any]],
                           bindings: Optional[Dict[str, str]]) -> None:
    """Raise BindingError for the first binding the graph does not allow."""
    for key, name in (bindings or {}).items():
        matching = [r for r in _iter_refs(graph) if _ref_key(r) == key]
        bindable = [r for r in matching if _is_bindable(r)]
        if not bindable or len(bindable) != len(matching):
            raise BindingError(key, "not_bindable")
        for ref in bindable:
            if not name.startswith(ref["bindable_prefix"]):
                raise BindingError(key, "name_outside_prefix")


def bound_secret_names(bindings: Optional[Dict[str, str]]) -> List[str]:
    """Sorted unique chosen secret names (for the access check / masking)."""
    return sorted({n for n in (bindings or {}).values() if isinstance(n, str) and n})


def apply_to_node_data(node_data: Dict[str, Any],
                       bindings: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Return node_data with each matching bindable ref's secret_name replaced
    by the run's chosen name. Never mutates the input; re-checks bindable +
    prefix (defence in depth), leaving the default on any mismatch."""
    if not bindings or not isinstance(node_data, dict):
        return node_data
    refs = node_data.get("secret_refs")
    if not isinstance(refs, list) or not refs:
        return node_data
    out_refs, changed = [], False
    for ref in refs:
        key = _ref_key(ref) if isinstance(ref, dict) else None
        name = bindings.get(key) if key else None
        if name and _is_bindable(ref) and name.startswith(ref["bindable_prefix"]):
            new_ref = copy.copy(ref)
            new_ref["secret_name"] = name
            out_refs.append(new_ref)
            changed = True
        else:
            out_refs.append(ref)
    if not changed:
        return node_data
    merged = dict(node_data)
    merged["secret_refs"] = out_refs
    return merged


def persisted_names(bindings: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Names-only record kept on the run row (key -> stored secret NAME).
    Downstream node dispatch re-reads the run record, so the choice must
    survive; it carries names only, never any secret value."""
    return dict(bindings) if bindings else {}


def bindings_from_project_info(project_info: Any) -> Dict[str, str]:
    """In-memory request bindings first, else the persisted names record."""
    pi = project_info if isinstance(project_info, dict) else {}
    raw = pi.get(REQUEST_KEY) or pi.get(PERSISTED_KEY) or {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def bound_names_from_run_parameters(parameters: Any) -> List[str]:
    """Chosen secret names recorded on a run row (masking mask sets)."""
    params = parameters if isinstance(parameters, dict) else {}
    return bound_secret_names(bindings_from_project_info(params.get("project_info")))


def strip_for_persistence(conf: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of a run conf with ``project_info.secret_bindings`` removed and the
    names-only record stored instead. Never mutates ``conf``."""
    pi = (conf or {}).get("project_info")
    if not isinstance(pi, dict) or REQUEST_KEY not in pi:
        return conf
    new_pi = {k: v for k, v in pi.items() if k != REQUEST_KEY}
    names = persisted_names(pi.get(REQUEST_KEY))
    if names:
        new_pi[PERSISTED_KEY] = names
    out = dict(conf)
    out["project_info"] = new_pi
    return out
