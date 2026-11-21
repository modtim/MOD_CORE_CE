"""workflow-expressiveness-primitives 427 — pure `implements` interface validation.

Spec (the internal design notes):
a workflow may declare `implements: <interface_name>` where the interface
defines REQUIRED INPUT PORT TYPES + REQUIRED OUTPUT PORT TYPES
(example: `image-upscaler = {input: image, output: image}`).

This module is the PURE validator only. The `implements` COLUMN (spec 1.3)
and the by-interface ENDPOINT (spec 3.4) are out of scope here — they are
lead / later tasks. Nothing in this file touches the DB, an endpoint, or a
router: it takes a `graph_json` + a caller-supplied `node_catalog` and
returns strings.

Port types come from `services.workflow_schema_export.build_workflow_mcp_schema`,
which turns a workflow's IO ports into a JSON Schema pair:

    {"input_schema":  {"type": "object", "properties": {<node_id>: <fragment>},
                       "required": [<node_id>, ...]},
     "output_schema": {...})

FIELD THAT CARRIES THE PORT TYPE: each entry in `properties` is the node
definition's `input_schema` / `output_schema` JSONB embedded as-is, and the
port type is that fragment's **`input_type`** (inputs) / **`output_type`**
(outputs) key — the same convention `services/port_type_enforcement.py`
reads (`_norm(((r.input_schema or {}) or {}).get("input_type"))`) and
`services/node_trial.py` falls back on. A fragment with no `input_type`
may still name its type with the JSON-Schema-ish `type` key, so we accept
that as a fallback ONLY when its value is not a JSON Schema structural
keyword (`object`/`array`/`string`/... — those say nothing about the port).
Anything else → `None` ("type unknown"), which `are_types_compatible`
treats as a wildcard, matching the catalog's "no type information"
sentinel behaviour.

Type comparison is delegated to `services.port_type_enforcement.are_types_compatible`
so an interface written `image` is satisfied by a port typed `resource`,
`IMAGE`, or `image,text` — exactly the rules the wizard already uses.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from services.workflow_schema_export import build_workflow_mcp_schema
from services.port_type_enforcement import are_types_compatible

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# JSON Schema structural keywords: a fragment's `type` is usually one of
# these and therefore carries NO port-type information.
_SCHEMA_KEYWORD_TYPES = {
    "object", "array", "string", "number", "integer", "boolean", "null", "any",
}


class InterfaceSpecError(ValueError):
    """Raised by `parse_interface` for a malformed interface declaration."""


@dataclass(frozen=True)
class InterfaceSpec:
    """A declared interface: slug name + required input/output port types."""

    name: str
    inputs: Tuple[str, ...] = ()
    outputs: Tuple[str, ...] = ()


def _side_of(obj: Dict[str, Any], key: str) -> List[str]:
    """One side of a declaration: absent key -> [], present -> parsed."""
    if key not in obj:
        return []
    return _types_of(obj[key])


def _types_of(value: Any) -> List[str]:
    """Normalise one interface side ("image" / ["image", "text"]) to a list.

    Raises InterfaceSpecError when the side is missing-shaped (not a string,
    not a sequence), holds a non-string entry, or yields no types at all.
    """
    if isinstance(value, str):
        raw: List[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise InterfaceSpecError(
            "interface port types must be a string or a list of strings, "
            f"got {type(value).__name__}"
        )
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise InterfaceSpecError(
                f"interface port type must be a string, got {item!r}"
            )
        item = item.strip()
        if not item:
            raise InterfaceSpecError("interface port type must not be empty")
        out.append(item)
    if not out:
        raise InterfaceSpecError("interface must declare at least one port type")
    return out


def parse_interface(obj: Any) -> InterfaceSpec:
    """Parse an interface declaration into an `InterfaceSpec`.

    Accepts `{"name": "image-upscaler", "input": "image", "output": "image"}`
    and the plural spellings `inputs` / `outputs` (both spellings, when
    present, are merged in `input`-then-`inputs` order). Duplicate types are
    KEPT — two required `image` inputs really do need two image ports.

    Raises `InterfaceSpecError` (a ValueError) with a clear message on a bad
    name, an empty side, or a non-string type.
    """
    if not isinstance(obj, dict):
        raise InterfaceSpecError(
            f"interface declaration must be an object, got {type(obj).__name__}"
        )
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        raise InterfaceSpecError(
            "interface declaration needs a non-empty string 'name'"
        )
    if not _NAME_RE.match(name):
        raise InterfaceSpecError(
            "interface name must be a slug: ^[a-z0-9][a-z0-9-]{0,63}$ "
            f"(got {name!r})"
        )
    if "input" not in obj and "inputs" not in obj:
        raise InterfaceSpecError(
            f"interface {name!r} declares no input port types "
            "(need 'input' or 'inputs')"
        )
    if "output" not in obj and "outputs" not in obj:
        raise InterfaceSpecError(
            f"interface {name!r} declares no output port types "
            "(need 'output' or 'outputs')"
        )
    inputs = _side_of(obj, "input") + _side_of(obj, "inputs")
    outputs = _side_of(obj, "output") + _side_of(obj, "outputs")
    return InterfaceSpec(name=name, inputs=tuple(inputs), outputs=tuple(outputs))


def _fragment_port_type(fragment: Any, key: str) -> Any:
    """Pull one port's type out of its schema fragment, or None.

    `fragment` is the NodeDefinition's `input_schema` / `output_schema` JSONB
    embedded as-is by `build_workflow_mcp_schema`. The type lives under
    `input_type` / `output_type` (the convention port_type_enforcement reads);
    a bare JSON-Schema `type` is accepted as a fallback only when it is not a
    structural keyword, because `{"type": "object"}` says nothing about the
    port. Unknown → None, which `are_types_compatible` treats as a wildcard.
    """
    if not isinstance(fragment, dict):
        return None
    value = fragment.get(key)
    if isinstance(value, str) and value.strip():
        return value
    value = fragment.get("type")
    if isinstance(value, str) and value.strip() \
            and value.strip().lower() not in _SCHEMA_KEYWORD_TYPES:
        return value
    return None


def workflow_port_types(graph_json: Any, node_catalog: Any) -> Dict[str, List[Any]]:
    """A workflow's entry-input / terminal-output port TYPES.

    Derived from `build_workflow_mcp_schema`, whose `properties` are keyed by
    node id and whose values are the definition's schema fragments — see the
    module docstring for which fragment key carries the type. One entry per
    port, in the schema's own order; `None` when the port's type is unknown.

    Returns `{"inputs": [...], "outputs": [...]}`. Never raises: the schema
    builder is itself never-raising, so a malformed graph yields empty lists.
    """
    schema = build_workflow_mcp_schema(graph_json or {}, node_catalog or {})
    out: Dict[str, List[Any]] = {"inputs": [], "outputs": []}
    for side, attr in (("inputs", "input_schema"), ("outputs", "output_schema")):
        props = (schema.get(attr) or {}).get("properties")
        if not isinstance(props, dict):
            continue
        for fragment in props.values():
            out[side].append(_fragment_port_type(fragment, "input_type"
                                                 if side == "inputs" else "output_type"))
    return out


def validate_implements(graph_json: Any, node_catalog: Any,
                        interface: Any) -> List[str]:
    """Does this workflow implement `interface`? [] = it does.

    `interface` is an `InterfaceSpec` (a raw declaration dict is parsed for
    you). Each REQUIRED input type must be accepted by at least one DISTINCT
    workflow entry input (`are_types_compatible(interface_type, port_type)`);
    each required output type must be produced by at least one DISTINCT
    terminal output (`are_types_compatible(port_type, interface_type)`).
    Matching is greedy one-to-one, so an interface requiring two `image`
    inputs needs two image entry ports — one port never satisfies two
    requirements. Extra workflow ports beyond the interface are allowed.

    Never raises: a malformed / empty graph yields violations, not an
    exception. A port whose type is unknown (`None`) is a wildcard, matching
    the catalog's "no type information" sentinel behaviour. Messages name the
    interface and the missing type only — no tenant data, so they are safe to
    surface in a shared listing.
    """
    if not isinstance(interface, InterfaceSpec):
        try:
            interface = parse_interface(interface)
        except InterfaceSpecError as exc:
            return [f"interface is not a valid declaration: {exc}"]

    ports = workflow_port_types(graph_json or {}, node_catalog or {})
    problems: List[str] = []

    free_inputs = list(ports["inputs"])
    for want in interface.inputs:
        hit = next((i for i, have in enumerate(free_inputs)
                    if are_types_compatible(want, have)), None)
        if hit is None:
            problems.append(
                f"workflow does not implement interface {interface.name!r}: "
                f"no entry input port accepts {want!r}"
            )
        else:
            free_inputs.pop(hit)

    free_outputs = list(ports["outputs"])
    for want in interface.outputs:
        hit = next((i for i, have in enumerate(free_outputs)
                    if are_types_compatible(have, want)), None)
        if hit is None:
            problems.append(
                f"workflow does not implement interface {interface.name!r}: "
                f"no terminal output port produces {want!r}"
            )
        else:
            free_outputs.pop(hit)

    return problems
