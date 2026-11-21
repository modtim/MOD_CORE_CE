"""wep1.c — save-time workflow parameter-declaration validator.

``graph_json.parameters`` is the workflow's declared input surface:
``{name, type, default?, required, description?}`` where ``type`` is one of the
platform's port types. The shape is checked when the VERSION IS SAVED (beside
the ``_swv`` composition gate in ``create_workflow_version``), so a graph with
an unknown type, a duplicate/bad name, an unknown key, or a default that
disagrees with its own declared type is refused 422 before any
``WorkflowVersion`` row exists — never discovered at run time.

Pure function, no DB, no settings: the run-time behaviour of these
declarations is gated later (``workflows.parameters_enabled``, wep1.f).

Error messages name the parameter and the machine code only; they never echo a
default value or description text (both are tenant-authored content that would
otherwise leak into the audit ``details`` and the HTTP body twice).
"""

from __future__ import annotations

import copy
import json
import re

PARAM_TYPES = frozenset({
    "image", "json", "csv", "tensor", "text", "number", "boolean", "list",
})

PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# wep1.f — an in-node reference to a declared parameter: ``${param.<name>}``
PARAM_TOKEN_RE = re.compile(r"\$\{param\.([A-Za-z_][A-Za-z0-9_]{0,63})\}")
MAX_SUBSTITUTED_LEN = 1_000_000
MAX_PARAMS = 64
MAX_DESCRIPTION_LEN = 500
DECLARED_KEYS = frozenset({"name", "type", "required", "default", "description"})


class WorkflowParametersError(ValueError):
    """422-carrier: ``str(e)`` is the HTTP detail; ``e.code`` / ``e.param_name``
    go to the audit row."""

    def __init__(self, message: str, code: str, param_name=None):
        super().__init__(message)
        self.code = code
        self.param_name = param_name


def _reject(code: str, param_name, message: str):
    raise WorkflowParametersError(f"{code}: {message}", code=code, param_name=param_name)


def _safe_repr(value, limit: int = 80) -> str:
    """Bounded repr for attacker-controlled values that may land in an error
    message — never reflect an arbitrary-length / control-char value raw."""
    try:
        text = repr(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[:limit] + "...'"


def _check_default(decl):
    """Type-check a declared default against its declared type."""
    name, ptype, value = decl["name"], decl["type"], decl.get("default")
    if ptype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject("default_type_mismatch", name,
                    f"parameter '{name}' declares type number but its default is not a number")
    elif ptype == "boolean":
        if not isinstance(value, bool):
            _reject("default_type_mismatch", name,
                    f"parameter '{name}' declares type boolean but its default is not a boolean")
    elif ptype in ("text", "image", "csv", "tensor"):
        if not isinstance(value, str):
            _reject("default_type_mismatch", name,
                    f"parameter '{name}' declares type {ptype} but its default is not a string")
    elif ptype == "list":
        if not isinstance(value, list):
            _reject("default_type_mismatch", name,
                    f"parameter '{name}' declares type list but its default is not a list")
    elif ptype == "json":
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            _reject("default_type_mismatch", name,
                    f"parameter '{name}' declares type json but its default is not JSON")


def _type_ok(ptype: str, value) -> bool:
    """Check that a supplied runtime value matches the declared parameter type.

    Mirrors ``_check_default``'s type rules:
    * number  → int or float (bool is excluded)
    * boolean → bool
    * text/image/csv/tensor → str
    * list    → list
    * json    → json.dumps-able (dict, list, str, int, float, bool, None)
    """
    if ptype == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if ptype == "boolean":
        return isinstance(value, bool)
    if ptype in ("text", "image", "csv", "tensor"):
        return isinstance(value, str)
    if ptype == "list":
        return isinstance(value, list)
    if ptype == "json":
        try:
            json.dumps(value)
            return True
        except (TypeError, ValueError):
            return False
    return False


def bind_run_parameters(declarations: list, supplied) -> dict:
    """Bind and validate runtime parameter values against declarations.

    ``declarations`` is the output of :func:`validate_parameter_declarations`.
    ``supplied`` is the raw ``parameters`` dict from the run request (or ``None``).

    Returns ``{name: value}`` for all bound parameters (supplied or default).
    Raises :class:`WorkflowParametersError` on any validation failure.
    """
    if supplied is None:
        bound: dict = {}
    elif not isinstance(supplied, dict):
        _reject("invalid_run_parameters", None,
                "parameters must be a JSON object (dict); got " + type(supplied).__name__)
        return None  # unreachable — _reject raises, but type-checkers need it
    else:
        bound = dict(supplied)

    declared_names = {d["name"] for d in declarations}
    decl_map = {d["name"]: d for d in declarations}

    # Unknown keys
    for key in bound:
        if key not in declared_names:
            _reject("unknown_parameter", key,
                    f"unknown parameter {_safe_repr(key)}")

    # Type check all supplied values
    for name, value in list(bound.items()):
        ptype = decl_map[name]["type"]
        if not _type_ok(ptype, value):
            _reject("parameter_type_mismatch", name,
                    f"parameter '{name}' expects {ptype}, got {type(value).__name__}")

    # Required + default resolution for missing keys
    for d in declarations:
        name = d["name"]
        ptype = d["type"]
        required = d.get("required", False)
        has_default = "default" in d
        is_present = name in bound

        if not is_present:
            if required and not has_default:
                _reject("missing_required_parameter", name,
                        f"required parameter '{name}' is not provided and has no default")
            elif has_default:
                bound[name] = d["default"]
            # optional, no default → omit (do nothing)

    return bound


def validate_parameter_declarations(graph_json) -> list:
    """Return the normalized declaration list, or raise
    :class:`WorkflowParametersError`."""
    if graph_json is None:
        raw = []
    elif isinstance(graph_json, dict):
        raw = graph_json.get("parameters")
        if raw is None:
            raw = []
        elif not isinstance(raw, list):
            _reject("invalid_parameter_declaration", "parameters",
                    "graph_json.parameters must be a list of parameter declarations")
    else:
        _reject("invalid_parameter_declaration", "parameters",
                "graph_json must be an object to carry parameter declarations")

    if len(raw) > MAX_PARAMS:
        _reject("too_many_parameters", None,
                f"workflow declares more than {MAX_PARAMS} parameters")

    seen = set()
    normalized = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            _reject("invalid_parameter_declaration", None,
                    f"parameter #{idx + 1} is not an object")

        unknown = set(item) - DECLARED_KEYS
        if unknown:
            _reject("unknown_parameter_key", item.get("name") if isinstance(item.get("name"), str) else None,
                    f"parameter declares unknown key(s): {', '.join(sorted(unknown))}")

        name = item.get("name")
        if not isinstance(name, str) or not PARAM_NAME_RE.match(name):
            _reject("invalid_parameter_declaration", name if isinstance(name, str) else None,
                    "parameter name is missing or not [A-Za-z_][A-Za-z0-9_]{0,63}")
        if name in seen:
            _reject("duplicate_parameter", name,
                    f"parameter '{name}' is declared more than once")
        seen.add(name)

        ptype = item.get("type")
        if not isinstance(ptype, str) or ptype not in PARAM_TYPES:
            _reject("invalid_parameter_type", name,
                    f"parameter '{name}' declares type {_safe_repr(ptype)}, not a platform port type")

        if "required" not in item:
            _reject("invalid_parameter_declaration", name,
                    f"parameter '{name}' is missing the required key")
        required = item["required"]
        if not isinstance(required, bool):
            _reject("invalid_parameter_declaration", name,
                    f"parameter '{name}' required key is not a boolean")

        description = item.get("description")
        if description is not None:
            if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LEN:
                _reject("invalid_parameter_declaration", name,
                        f"parameter '{name}' description is not a string of at most {MAX_DESCRIPTION_LEN} characters")

        if "default" in item:
            _check_default({**item, "name": name, "type": ptype})

        decl = {"name": name, "type": ptype, "required": required}
        if "default" in item:
            decl["default"] = item["default"]
        if description is not None:
            decl["description"] = description
        normalized.append(decl)

    return normalized


def substitute_parameters(nodes: list, bindings: dict, declared_names: set) -> list:
    """wep1.f — splice ``${param.<name>}`` references in node configs.

    Returns a NEW deep-copied ``nodes`` list with every ``${param.<name>}``
    token inside each node's ``data`` subtree replaced by its bound value:

    * a string that is *exactly* one token becomes the bound value itself,
      type preserved (number stays number, list stays list, bool stays bool);
    * a string with a token among other text gets each token spliced in as
      ``str(value)`` for strings, ``json.dumps(value)`` for anything else;
    * ids, positions, edges and every other non-``data`` field are left alone.

    The input is never mutated (it may be ``db_version.graph_json`` — writing
    into that ORM row would dirty the stored version). Substituted values are
    inserted verbatim, never re-scanned, so a bound value that itself contains
    ``"${param.x}"`` is not expanded again (injection guard).

    Raises :class:`WorkflowParametersError` for a reference to an undeclared
    name (``unknown_parameter_reference``), a declared name with no bound
    value (``unbound_parameter``), or a spliced string that grew past
    ``MAX_SUBSTITUTED_LEN`` characters (``parameter_value_too_large``).

    Pure string work: nothing here ever compiles or runs node content.
    """
    out = copy.deepcopy(nodes)
    for node in out:
        if not isinstance(node, dict):
            continue
        data = node.get("data")
        if isinstance(data, dict):
            node["data"] = _substitute_value(data, bindings, declared_names)
    return out


def _substitute_value(value, bindings: dict, declared_names: set):
    """Recursive helper for :func:`substitute_parameters` (dicts/lists/strings)."""
    if isinstance(value, str):
        return _substitute_string(value, bindings, declared_names)
    if isinstance(value, dict):
        return {k: _substitute_value(v, bindings, declared_names)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_value(v, bindings, declared_names) for v in value]
    return value


def _substitute_string(text: str, bindings: dict, declared_names: set):
    """Replace parameter tokens in one string; type-preserving when exact."""
    match = PARAM_TOKEN_RE.fullmatch(text)
    if match:
        name = match.group(1)
        if name not in declared_names:
            _reject("unknown_parameter_reference", name,
                    f"node config references undeclared parameter '{name}'")
        if name not in bindings:
            _reject("unbound_parameter", name,
                    f"declared parameter '{name}' has no bound value for this run")
        return bindings[name]

    if PARAM_TOKEN_RE.search(text) is None:
        return text

    def _splice(match):
        name = match.group(1)
        if name not in declared_names:
            _reject("unknown_parameter_reference", name,
                    f"node config references undeclared parameter '{name}'")
        if name not in bindings:
            _reject("unbound_parameter", name,
                    f"declared parameter '{name}' has no bound value for this run")
        value = bindings[name]
        return value if isinstance(value, str) else json.dumps(value)

    result = PARAM_TOKEN_RE.sub(_splice, text)
    if len(result) > MAX_SUBSTITUTED_LEN:
        _reject("parameter_value_too_large", None,
                f"substituted node value exceeds {MAX_SUBSTITUTED_LEN} characters")
    return result
