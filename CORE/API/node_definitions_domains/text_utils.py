"""node_definitions_domains/text_utils — Regex / JSON / XML domain-primitive NodeDefinitions.

launch-completion-megaplan sz-6.13.1 (Phase 6: Domain primitives). Registers eight
system node definitions backed by the text-utils container
(`CORE/DockerContainers/text-utils`): regex match / extract / replace, JSON parse /
validate / get, and JSON<->XML conversion.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py`) so the module is
  importable + testable without a DB; `register_text_utils()` is the only
  DB-touching entry point.
- Every spec carries its own fixed op variable `TEXT_OP` as a FLAT default in
  `configuration_schema` (the `seed_converters.py` form). The already-reviewed
  domain modules omit this, so every node ran the container's default op — this
  module must not repeat that bug, and a test asserts each spec's `TEXT_OP`
  default equals its `NODE_DEFINITIONS_BY_OP` key.
- `input_schema` / `output_schema` carry both the conventional `input_type` /
  `output_type` pair that `services/port_type_enforcement.py` reads and named
  ports (`text`, `data`, `schema`, `json`, `xml`). Every port type is in
  `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES (`json`, `text`).
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it.
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only; existing rows are never deleted.
- Untrusted input posture: no eval/exec, no shell=True, pattern <= 1000 chars,
  input <= 10 MB, and the regex ops run in a FORKED SUBPROCESS with a hard
  `REGEX_TIMEOUT_SECONDS` join — exactly like `services/ai_primitives/eval.py:_regex`,
  because a thread timeout cannot interrupt the sre loop (it holds the GIL).
- `jsonschema` is NOT in `CORE/API/requirements.txt`, so the container carries a
  minimal validator (type/required/properties/items/enum/min/max/length).
- Untrusted XML is NEVER parsed through `xml.etree` (vet step 2, bandit B313/B405):
  the image pins `defusedxml==0.7.1` and parsing goes through
  `defusedxml.ElementTree.fromstring`. `xml.etree.ElementTree` is imported for XML
  GENERATION only (`json_to_xml` builds a tree we control, never parses input).
  There is no hand-written fallback parser and no path back to stdlib XML parsing:
  if `defusedxml` fails to import (it is pinned in the image, so this should never
  happen outside a broken build), `xml_to_json` fails closed with `INTERNAL`
  instead of silently downgrading to a different parser.
- Vet step 5 (fail-closed): every failure path ends in `fail()`, which prints
  exactly one JSON line (`{"code": ..., "error": ...}`) on stdout and exits
  non-zero; an unexpected exception is reported as `INTERNAL`, never as a
  traceback.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# Bare repo:tag of the container behind all eight operations. The fully-qualified
# ref (registry + project) is resolved at seed time by platform_image() so a
# private-Harbor / on-prem deployment points at its own registry.
CONTAINER_REPO_TAG = "text-utils:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/text-utils/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"
NODE_TYPE = "Data"
ENV_VAR = "TEXT_OP"

# Must match the entrypoint constants below.
MAX_PATTERN_LEN = 1000
MAX_INPUT_BYTES = 10 * 1024 * 1024
REGEX_TIMEOUT_SECONDS = 1.0


def _spec(name, op, description, in_port, out_port, extra_config=None):
    """Build one spec with the fixed TEXT_OP default (the flat seed_converters form)."""
    return {
        "name": name,
        "description": description,
        "type": NODE_TYPE,
        "executable_path": CONTAINER_IMAGE,
        "process_type": PROCESS_TYPE,
        "node_kind": op,
        "batch_mode": BATCH_MODE,
        "input_schema": {"input_type": in_port[1], in_port[0]: {"type": in_port[1], "description": in_port[2]}},
        "output_schema": {"output_type": out_port[1], out_port[0]: {"type": out_port[1], "description": out_port[2]}},
        "configuration_schema": {
            ENV_VAR: {
                "type": "string",
                "default": op,
                "description": f"Operation this node runs. Fixed to '{op}'.",
            },
            **(extra_config or {}),
        },
        "accepted_formats": ACCEPTED_FORMATS[op],
        "cost_in_credits": 1,
    }


ACCEPTED_FORMATS = {
    "regex_match": ["txt", "md", "csv", "json", "xml"],
    "regex_extract": ["txt", "md", "csv", "json", "xml"],
    "regex_replace": ["txt", "md", "csv", "json", "xml"],
    "json_parse": ["json"],
    "json_validate": ["json"],
    "json_get": ["json"],
    "json_to_xml": ["json"],
    "xml_to_json": ["xml"],
}

REGEX_PATTERN_DESC = (
    "Python regular expression, <= 1000 chars. Runs in a forked subprocess with a "
    f"{REGEX_TIMEOUT_SECONDS}s hard timeout, exactly like services/ai_primitives/eval.py "
    "(a thread timeout cannot interrupt the sre loop, which holds the GIL)."
)

REGEX_MATCH_SPEC = _spec(
    "Regex Match",
    "regex_match",
    "Test a regular expression against text on the `text` port and emit `json`: "
    '{"matched": true, "matches": ["..."]}. A non-matching input is a valid result, '
    "not an error; a pattern that exceeds the length limit or times out (catastrophic "
    "backtracking) exits non-zero with an error JSON.",
    ("text", "text", "Text to test the pattern against."),
    ("json", "json", "Match result: matched flag and matched strings."),
    {"REGEX_PATTERN": {"type": "string", "default": "", "description": REGEX_PATTERN_DESC}},
)

REGEX_EXTRACT_SPEC = _spec(
    "Regex Extract",
    "regex_extract",
    "Collect every match of a regular expression from text on the `text` port into a "
    "JSON array on `json` (group 0, in scan order). Optional `GROUP` selects a capture "
    "group. Same pattern-length and forked-subprocess timeout posture as Regex Match.",
    ("text", "text", "Text to scan."),
    ("json", "json", "Array of matched strings."),
    {
        "REGEX_PATTERN": {"type": "string", "default": "", "description": REGEX_PATTERN_DESC},
        "GROUP": {"type": "integer", "default": 0, "description": "Capture group to emit (0 = whole match)."},
    },
)

REGEX_REPLACE_SPEC = _spec(
    "Regex Replace",
    "regex_replace",
    "Replace every match of a regular expression in text on the `text` port and emit the "
    "rewritten text on `text`. `REPLACEMENT` may reference capture groups (\\1, \\g<name>). "
    "Same pattern-length and forked-subprocess timeout posture as Regex Match.",
    ("text", "text", "Text to rewrite."),
    ("text", "text", "Text with every match replaced."),
    {
        "REGEX_PATTERN": {"type": "string", "default": "", "description": REGEX_PATTERN_DESC},
        "REPLACEMENT": {"type": "string", "default": "", "description": "Replacement string (supports \\1 groups)."},
    },
)

JSON_PARSE_SPEC = _spec(
    "JSON Parse",
    "json_parse",
    "Parse a JSON document on the `data` port and re-emit it as validated JSON on `json`. "
    "Malformed JSON exits non-zero with an error JSON naming the offset. Inputs are "
    "untrusted: the document is parsed with json.loads only, never evaluated.",
    ("data", "json", "JSON document to parse."),
    ("json", "json", "Parsed document."),
)

JSON_VALIDATE_SPEC = _spec(
    "JSON Validate",
    "json_validate",
    "Validate the JSON document on `data` against the JSON Schema on `schema` and emit "
    '{"valid": bool, "errors": [...]} on `json`. jsonschema is not a container dependency, '
    "so this implements the draft-07 subset the catalog actually uses (type, required, "
    "properties, items, enum, min/max, length). No remote refs are fetched — the "
    "container has no network access.",
    ("data", "json", "JSON document to validate."),
    ("json", "json", "Validation result: valid flag and error list."),
    {"SCHEMA_FILE": {"type": "string", "default": "schema.json", "description": "Schema file name on the `schema` port."}},
)

JSON_GET_SPEC = _spec(
    "JSON Get",
    "json_get",
    "Read one value out of the JSON document on `data` by a dotted path "
    "(`JSON_PATH`, e.g. `user.roles[2]`) and emit it as JSON on `json`. Plain path "
    "walk only — no eval, no JSONPath engine, no function calls.",
    ("data", "json", "JSON document to read from."),
    ("json", "json", "Value at the path, re-serialized as JSON."),
    {"JSON_PATH": {"type": "string", "default": "", "description": "Dotted path, e.g. 'user.roles' or 'items[0].name'."}},
)

JSON_TO_XML_SPEC = _spec(
    "JSON to XML",
    "json_to_xml",
    "Convert the JSON document on `data` to an XML document on `xml` "
    "(object keys become elements, arrays repeat the element, scalars become text). "
    "`XML_ROOT` names the root element. Output is escaped text only — no DTD, no entities.",
    ("data", "json", "JSON document to convert."),
    ("xml", "document", "Generated XML document."),
    {"XML_ROOT": {"type": "string", "default": "root", "description": "Root element name."}},
)

XML_TO_JSON_SPEC = _spec(
    "XML to JSON",
    "xml_to_json",
    "Convert the XML document on `data` to JSON on `json`. Parsing is XXE / "
    "billion-laughs safe: any DOCTYPE or ENTITY declaration is rejected before parsing, "
    "and no external entity is ever resolved. Inputs larger than 10 MB are refused.",
    ("data", "document", "XML document to convert."),
    ("json", "json", "Document as JSON (elements as objects, repeated elements as arrays)."),
)

NODE_DEFINITIONS = [
    REGEX_MATCH_SPEC,
    REGEX_EXTRACT_SPEC,
    REGEX_REPLACE_SPEC,
    JSON_PARSE_SPEC,
    JSON_VALIDATE_SPEC,
    JSON_GET_SPEC,
    JSON_TO_XML_SPEC,
    XML_TO_JSON_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "regex_match": REGEX_MATCH_SPEC,
    "regex_extract": REGEX_EXTRACT_SPEC,
    "regex_replace": REGEX_REPLACE_SPEC,
    "json_parse": JSON_PARSE_SPEC,
    "json_validate": JSON_VALIDATE_SPEC,
    "json_get": JSON_GET_SPEC,
    "json_to_xml": JSON_TO_XML_SPEC,
    "xml_to_json": XML_TO_JSON_SPEC,
}

RECONCILED_FIELDS = (
    "description",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_text_utils(db: Session) -> int:
    """Register the 8 text/JSON/XML operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping text-utils domain-primitive seed"
        )
        return 0

    created = 0
    for spec in NODE_DEFINITIONS:
        existing = db.query(NodeDefinition).filter(
            NodeDefinition.name == spec["name"],
            NodeDefinition.tenant_id.is_(None),
        ).first()

        if existing:
            for field in RECONCILED_FIELDS:
                setattr(existing, field, spec[field])
            continue

        db.add(
            NodeDefinition(
                name=spec["name"],
                tenant_id=None,  # system node — available to all tenants
                description=spec["description"],
                type=spec["type"],
                executable_path=spec["executable_path"],
                process_type=spec["process_type"],
                node_kind=spec["node_kind"],
                batch_mode=spec["batch_mode"],
                input_schema=spec["input_schema"],
                output_schema=spec["output_schema"],
                configuration_schema=spec["configuration_schema"],
                accepted_formats=spec["accepted_formats"],
                cost_in_credits=spec["cost_in_credits"],
            )
        )
        created += 1

    if created > 0:
        db.commit()
    return created


# ---------------------------------------------------------------------------
# Entrypoint source — the same text as the Dockerfile's quoted heredoc, kept
# here as a constant so the op functions are unit-testable without building
# the image (tests exec this string). Keep the two in sync.
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r'''
#!/usr/bin/env python3
"""text-utils entrypoint. Mirrors the heredoc in CORE/DockerContainers/text-utils.

Untrusted input posture: no eval/exec, no shell=True, bounded input size, and the
regex ops run in a forked subprocess with a hard timeout (a thread timeout cannot
interrupt the sre loop, which holds the GIL). No network access at runtime.
"""

import json
import multiprocessing as mp
import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

# Vet step 2 (SAST / bandit B313): untrusted XML is NEVER parsed through xml.etree.
# ElementTree stays imported for XML GENERATION only (json_to_xml builds a tree we
# control); parsing goes through defusedxml, pinned in the image. There is no
# hand-written fallback parser: if this import fails, op_xml_to_json fails closed
# with CODE_INTERNAL instead of silently using a different parser.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:
    _xml_fromstring = None

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_PATTERN_LEN = 1000
MAX_INPUT_BYTES = 10 * 1024 * 1024
REGEX_TIMEOUT_SECONDS = 1.0

# Vet step 5 (fail-closed): every failure path ends in fail(), which prints
# exactly one JSON line on stdout and exits non-zero.
CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code=CODE_INTERNAL):
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    log("ERROR: " + msg)
    sys.exit(1)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def read_bounded(path):
    size = path.stat().st_size
    if size == 0:
        fail("input %s is empty" % path.name, CODE_NO_INPUT)
    if size > MAX_INPUT_BYTES:
        fail("input %s is %d bytes, over the %d byte limit" % (path.name, size, MAX_INPUT_BYTES), CODE_NO_INPUT)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        fail("input %s is not valid UTF-8 text: %s" % (path.name, exc), CODE_NO_INPUT)
    return None


def _regex_worker(kind, pattern, text, extra, queue):
    try:
        rx = re.compile(pattern)
        if kind == "match":
            queue.put(("ok", {"matched": rx.search(text) is not None,
                              "matches": [m.group(0) for m in rx.finditer(text)]}))
        elif kind == "extract":
            group = int(extra or 0)
            queue.put(("ok", [m.group(group) for m in rx.finditer(text)]))
        else:
            queue.put(("ok", rx.sub(extra or "", text)))
    except Exception as exc:
        queue.put(("error", "%s: %s" % (type(exc).__name__, exc)))


def op_regex(kind, pattern, text, extra=None, timeout=REGEX_TIMEOUT_SECONDS):
    if len(pattern) > MAX_PATTERN_LEN:
        fail("regex pattern exceeds %d chars (got %d)" % (MAX_PATTERN_LEN, len(pattern)), CODE_BAD_PARAM)
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        fail("regex input exceeds %d bytes" % MAX_INPUT_BYTES, CODE_NO_INPUT)
    try:
        re.compile(pattern)
    except re.error as exc:
        fail("invalid regex %r: %s" % (pattern, exc), CODE_BAD_PARAM)

    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    proc = ctx.Process(target=_regex_worker, args=(kind, pattern, text, extra, queue), daemon=True)
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(1.0)
        if proc.is_alive():
            proc.kill()
            proc.join()
        fail("regex %r exceeded %ss timeout (likely catastrophic backtracking)" % (pattern, timeout), CODE_PROCESSING)
    if queue.empty():
        fail("regex %r worker exited without a result (code %s)" % (pattern, proc.exitcode), CODE_PROCESSING)
    status, payload = queue.get()
    if status == "error":
        fail("regex %r raised %s" % (pattern, payload), CODE_PROCESSING)
    return payload


def op_parse(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        fail("invalid JSON at offset %d: %s" % (exc.pos, exc.msg), CODE_BAD_PARAM)
    return None


TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def op_validate(doc, schema, path="$"):
    errors = []
    want = schema.get("type")
    if want:
        wanted = want if isinstance(want, list) else [want]
        allowed = tuple(TYPE_MAP[t] for t in wanted if t in TYPE_MAP)
        ok = bool(allowed) and isinstance(doc, allowed)
        if "integer" in wanted and isinstance(doc, bool):
            ok = False
        if not ok:
            return ["%s: expected %s" % (path, ",".join(wanted))]
    if "enum" in schema and doc not in schema["enum"]:
        errors.append("%s: value not in enum" % path)
    if isinstance(doc, dict):
        for key in schema.get("required", []):
            if key not in doc:
                errors.append("%s: missing required %r" % (path, key))
        props = schema.get("properties", {})
        for key, value in doc.items():
            if key in props:
                errors.extend(op_validate(value, props[key], "%s.%s" % (path, key)))
        if "minProperties" in schema and len(doc) < schema["minProperties"]:
            errors.append("%s: fewer than %d properties" % (path, schema["minProperties"]))
        if "maxProperties" in schema and len(doc) > schema["maxProperties"]:
            errors.append("%s: more than %d properties" % (path, schema["maxProperties"]))
    if isinstance(doc, list):
        item = schema.get("items")
        for i, value in enumerate(doc):
            if item:
                errors.extend(op_validate(value, item, "%s[%d]" % (path, i)))
        if "minItems" in schema and len(doc) < schema["minItems"]:
            errors.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(doc) > schema["maxItems"]:
            errors.append("%s: more than %d items" % (path, schema["maxItems"]))
    if isinstance(doc, str):
        if "minLength" in schema and len(doc) < schema["minLength"]:
            errors.append("%s: shorter than %d chars" % (path, schema["minLength"]))
        if "maxLength" in schema and len(doc) > schema["maxLength"]:
            errors.append("%s: longer than %d chars" % (path, schema["maxLength"]))
    if isinstance(doc, (int, float)) and not isinstance(doc, bool):
        if "minimum" in schema and doc < schema["minimum"]:
            errors.append("%s: below minimum %s" % (path, schema["minimum"]))
        if "maximum" in schema and doc > schema["maximum"]:
            errors.append("%s: above maximum %s" % (path, schema["maximum"]))
    return errors


def op_get(doc, path):
    cur = doc
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", path or ""):
        if token.startswith("["):
            idx = int(token[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                fail("path token %s is not a valid array index" % token, CODE_BAD_PARAM)
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or token not in cur:
                fail("path token %r not found" % token, CODE_BAD_PARAM)
            cur = cur[token]
    return cur


def op_json_to_xml(value, root="root"):
    el = ElementTree.Element(root)
    _fill(el, value)
    return ElementTree.tostring(el, encoding="unicode")


def _fill(el, value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", str(key)):
                fail("cannot emit XML element name from key %r" % key, CODE_BAD_PARAM)
            _fill(ElementTree.SubElement(el, str(key)), child)
    elif isinstance(value, list):
        for child in value:
            item = ElementTree.SubElement(el, "item")
            _fill(item, child)
    elif isinstance(value, bool):
        el.text = "true" if value else "false"
    elif value is None:
        el.text = ""
    else:
        el.text = str(value)


def op_xml_to_json(text):
    # XXE / billion-laughs guard: reject any DTD or entity declaration BEFORE
    # parsing, and never resolve an external entity. The only parser is
    # defusedxml (pinned in the image). There is no hand-written fallback
    # parser: if defusedxml failed to import (e.g. a broken image build, or
    # the API test image, which ships no pip deps), that is an INTERNAL
    # contract error, not a silent downgrade to a different parser.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        fail("DTD/entity declarations are rejected (XXE / billion-laughs unsafe)", CODE_BAD_PARAM)
    if _xml_fromstring is None:
        fail("defusedxml is not installed in this image", CODE_INTERNAL)
    try:
        root = _xml_fromstring(text)
    except Exception as exc:
        fail("invalid XML: %s" % exc, CODE_PROCESSING)
        return None
    return {root.tag: _node(root)}


def _node(el):
    kids = list(el)
    if not kids:
        return el.text if el.text is not None else ""
    out = {}
    for child in kids:
        value = _node(child)
        if child.tag in out:
            if not isinstance(out[child.tag], list):
                out[child.tag] = [out[child.tag]]
            out[child.tag].append(value)
        else:
            out[child.tag] = value
    return out


def op_validate_result(doc, schema):
    errors = op_validate(doc, schema)
    return {"valid": not errors, "errors": errors}


def op_json_validate(files):
    doc = op_parse(read_bounded(files[0]))
    wanted = os.getenv("SCHEMA_FILE", "schema.json")
    schema_file = next((f for f in files if f.name == wanted), None)
    if schema_file is None:
        fail("no schema file named %s on the `schema` port" % wanted)
    return op_validate_result(doc, op_parse(read_bounded(schema_file)))


def op_json_get(files):
    return op_get(op_parse(read_bounded(files[0])), os.getenv("JSON_PATH", ""))


def op_json_to_xml_files(files):
    return op_json_to_xml(op_parse(read_bounded(files[0])), os.getenv("XML_ROOT", "root"))


OPS = {
    "regex_match": lambda files: op_regex("match", os.getenv("REGEX_PATTERN", ""), read_bounded(files[0])),
    "regex_extract": lambda files: op_regex("extract", os.getenv("REGEX_PATTERN", ""), read_bounded(files[0]), os.getenv("GROUP", "0")),
    "regex_replace": lambda files: op_regex("replace", os.getenv("REGEX_PATTERN", ""), read_bounded(files[0]), os.getenv("REPLACEMENT", "")),
    "json_parse": lambda files: op_parse(read_bounded(files[0])),
    "json_validate": op_json_validate,
    "json_get": op_json_get,
    "json_to_xml": op_json_to_xml_files,
    "xml_to_json": lambda files: op_xml_to_json(read_bounded(files[0])),
}


def main():
    out_name = "output.json"
    try:
        op = os.getenv("TEXT_OP", "").strip().lower()
        if op not in OPS:
            fail("unknown TEXT_OP %r (expected one of %s)" % (op, ", ".join(sorted(OPS))), CODE_BAD_PARAM)
        files = inputs()
        if not files:
            fail("no input file in /data/input", CODE_NO_INPUT)
        log("=== text-utils: %s (%d file) ===" % (op, len(files)))
        result = OPS[op](files)
        out_name = "output.xml" if op == "json_to_xml" else "output.json"
        payload = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / out_name).write_text(payload, encoding="utf-8")
        log("wrote /data/output/%s" % out_name)
    except SystemExit:
        raise
    except OSError as exc:
        fail("could not write /data/output/%s: %s" % (out_name, exc), CODE_OUTPUT_NOT_WRITTEN)
    except Exception as exc:  # vet step 5: never leak a traceback to stdout
        fail("%s: %s" % (type(exc).__name__, exc), CODE_INTERNAL)


if __name__ == "__main__":
    main()
'''
