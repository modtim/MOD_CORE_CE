# text-utils - MOD node container for the Regex / JSON / XML domain primitives
# (launch-completion-megaplan sz-6.13.1). One container, eight operations,
# selected by TEXT_OP (every NodeDefinition carries its own fixed TEXT_OP default,
# so a node never falls back to the container default op):
#
#   TEXT_OP=regex_match     pattern test on text          -> output.json
#   TEXT_OP=regex_extract   every match as a JSON array   -> output.json
#   TEXT_OP=regex_replace   rewrite text                  -> output.json (text)
#   TEXT_OP=json_parse      parse a JSON document         -> output.json
#   TEXT_OP=json_validate   validate against schema.json  -> output.json
#   TEXT_OP=json_get        dotted-path get               -> output.json
#   TEXT_OP=json_to_xml     JSON -> XML                   -> output.xml
#   TEXT_OP=xml_to_json     XML -> JSON (XXE-safe)        -> output.json
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output.
#
# One pip dep: defusedxml==0.7.1 (vet step 2 / bandit B313) - untrusted XML is
# parsed through defusedxml.ElementTree.fromstring, never xml.etree. jsonschema
# is NOT in CORE/API/requirements.txt, so the entrypoint carries its own draft-07
# subset validator. xml.etree is imported for XML GENERATION only (json_to_xml
# builds a tree we control). There is no hand-written fallback XML parser: if
# defusedxml fails to import, xml_to_json fails closed with an INTERNAL
# contract error instead of delegating to a different parser. Vet step 5:
# every failure path prints exactly one {"code": ..., "error": ...} JSON line
# on stdout and exits non-zero.
# No network access at runtime; inputs are untrusted (no eval/exec, no shell=True,
# bounded input size, regex ops run in a forked subprocess with a hard timeout).
#
# The entrypoint below is the same text as ENTRYPOINT_SOURCE in
# CORE/API/node_definitions_domains/text_utils.py (differs only by one leading newline).
# Authored only in sz-6.13.1 - build/push is a lead deploy step.

# Pinned to the python:3.11-slim multi-arch INDEX digest (not a single-arch
# manifest digest), verified with `docker buildx imagetools inspect` on the
# test VM (2026-10-15): python:3.11.17-slim-trixie.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# Vet step 2 (SAST / bandit B313): untrusted XML parsing goes through defusedxml.
RUN pip install --no-cache-dir \
        defusedxml==0.7.1

RUN mkdir -p /data/input /data/output /app

RUN cat > /app/text_util.py <<'PYEOF'
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
PYEOF

WORKDIR /data
ENTRYPOINT ["python3", "/app/text_util.py"]
