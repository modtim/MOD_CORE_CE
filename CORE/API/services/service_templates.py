"""GA.ASW.1 (launch-completion-megaplan) — service template catalog loader.

The persistent-stacks "create from template" catalog (postgres, redis, nginx,
...) used to live in-code as `STACK_TEMPLATES` / `_render_template` in
`routers/persistent_stacks/_shared.py`. This module loads the same catalog
from `CORE/API/service_templates/*.yaml` instead, so adding/editing a
template no longer requires a code change.

Loaded and cached ONCE at import time: the catalog is a handful of small
local YAML files (no meaningful import-time IO cost), and eager loading means
a malformed file fails LOUDLY at process startup (import raises) rather than
silently on the first user request that happens to hit that template.

`STACK_TEMPLATES` is kept importable under its original name/shape
(`{template_id: {name, description, storage_class, params}}`) because other
modules and tests import it directly (see
`services/persistent_stacks_workers_fleet_service.py:list_templates`). The
render entry point is `render_template(template_id, params)`, returning the
exact `(compose_spec, storage_class)` tuple the old `_render_template`
returned; `routers/persistent_stacks/_shared.py` re-exports it as
`_render_template` so existing callers/imports are unchanged.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Dict, List, Tuple

import yaml
from fastapi import HTTPException

_TEMPLATES_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "service_templates")
)

_REQUIRED_KEYS = ("id", "name", "description", "storage_class", "params", "compose")


def _substitute(node: Any, values: Dict[str, str]) -> Any:
    """Recursively `str.format(**values)` every string leaf of a compose
    template. Dicts/lists are walked; non-string scalars (e.g. the `{}` empty
    volume marker) pass through unchanged. Deep-copies nothing itself — the
    caller is expected to hand in a fresh copy of the stored template."""
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    if isinstance(node, str):
        return node.format(**values)
    return node


def _fail(path: str, msg: str) -> None:
    raise RuntimeError(f"service_templates: {path}: {msg}")


def _load_one(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            _fail(path, f"malformed YAML: {e}")
    if not isinstance(raw, dict):
        _fail(path, "top-level document must be a YAML mapping")
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        _fail(path, f"missing required key(s): {missing}")
    if not isinstance(raw["id"], str) or not raw["id"]:
        _fail(path, "'id' must be a non-empty string")
    if not isinstance(raw["params"], list):
        _fail(path, "'params' must be a list")
    for p in raw["params"]:
        if not isinstance(p, dict) or "key" not in p:
            _fail(path, "each entry in 'params' needs a 'key'")
    if not isinstance(raw["compose"], dict):
        _fail(path, "'compose' must be a mapping")
    return raw


def _load_catalog() -> Dict[str, Dict[str, Any]]:
    if not os.path.isdir(_TEMPLATES_DIR):
        raise RuntimeError(f"service_templates: directory not found: {_TEMPLATES_DIR}")
    catalog: Dict[str, Dict[str, Any]] = {}
    for fname in sorted(os.listdir(_TEMPLATES_DIR)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(_TEMPLATES_DIR, fname)
        tpl = _load_one(path)
        tid = tpl["id"]
        if tid in catalog:
            _fail(path, f"duplicate template id '{tid}'")
        catalog[tid] = tpl
    return catalog


# Eager load — see module docstring for why this is not lazy.
_CATALOG: Dict[str, Dict[str, Any]] = _load_catalog()

STACK_TEMPLATES: Dict[str, Dict[str, Any]] = {
    tid: {
        "name": tpl["name"],
        "description": tpl["description"],
        "storage_class": tpl["storage_class"],
        "params": tpl["params"],
    }
    for tid, tpl in _CATALOG.items()
}


def render_template(template_id: str, params: dict) -> Tuple[dict, str]:
    """Return (compose_spec, storage_class) for a template + params — same
    contract/behavior as the old in-code `_render_template`: an unknown
    template raises HTTPException(404), and any param not supplied falls back
    to its declared default (coerced to str, matching the old per-template
    `str(params.get(...))` calls)."""
    tpl = _CATALOG.get(template_id)
    if tpl is None:
        raise HTTPException(status_code=404, detail=f"Unknown template '{template_id}'")
    supplied = params or {}
    values = {p["key"]: str(supplied.get(p["key"], p.get("default"))) for p in tpl["params"]}
    compose = _substitute(copy.deepcopy(tpl["compose"]), values)
    return compose, tpl["storage_class"]
