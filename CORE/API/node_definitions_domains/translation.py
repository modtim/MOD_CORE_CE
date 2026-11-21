"""node_definitions_domains/translation — Translation domain-primitive
NodeDefinitions (launch-completion-megaplan sz-6.8.1, OFFLINE).

Registers one system node definition, `Translate Text`, backed by the
translation container (`CORE/DockerContainers/translation`): plain text in,
translated `.txt` plus `meta.json` {source, target, chars, engine} out.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py` / the sibling
  domain modules) so the module is importable + testable without a DB;
  `register_translation()` is the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already
  speaks: the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility
  checks, AND named ports (`text` -> `translated`) so the UI can label the
  ports this domain actually exposes. `text` is in
  `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it (system-node names are globally
  unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as the sibling domain modules do — existing rows are never
  deleted.
- Every spec pins its own operation with a flat `TRANSLATE_OP` default equal
  to its `NODE_DEFINITIONS_BY_OP` key (the bug the earlier domain modules
  shipped: no op env var meant every node silently ran the container's
  default op).

Offline posture
---------------
The engine is argostranslate and the six language packages (en<->es, en<->fr,
en<->de) are baked into the image at BUILD time, sha256-verified, installed
with `argostranslate.package.install_from_path`. Nothing at RUNTIME ever
reaches out: the engine is loaded only from `ARGOS_PACKAGES_DIR`, and a pair
that is not baked is REJECTED, never fetched. `SOURCE=auto` is deliberately
NOT supported — the engine has to know both ends before it can load a
specific baked package, and guessing would mean loading packages this image
does not have. Pivoting (es->fr through en) is NOT done: argostranslate gives
no quality guarantee for a two-hop machine translation, so an unbaked pair is
an error, not a silent degradation.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTAINER_REPO_TAG = "translation:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/translation/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# translation entrypoint — one operation per run, selected by TRANSLATE_OP.
#
#   export TRANSLATE_OP=translate_text
#   python3 /app/translate_process.py
#
# I/O contract is the same as every other MOD node container: the worker stages
# the node's input files into /data/input and collects /data/output. Parameters
# arrive as /data/input/fields.json (the node's `text` port plus SOURCE/TARGET).
#
# Offline posture: the engine is argostranslate and the six language packages
# (en<->es, en<->fr, en<->de) are baked into the image at BUILD time, sha256-
# verified. Nothing here ever reaches out to the internet: the engine is loaded
# ONLY from ARGOS_PACKAGES_DIR, and a pair that is not baked is rejected, never
# fetched. SOURCE=auto is not supported (the engine must load a specific baked
# package). Bad input exits non-zero with a JSON error in
# /data/output/error.json.

import json
import os
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_CHARS = 50_000            # input cap
CHUNK_MAX_CHARS = 5_000       # per engine call
ENGINE = "argostranslate"

# Baked pairs only. "auto" is deliberately NOT in here: see the header.
ALLOWED_PAIRS = ("en>es", "en>fr", "en>de", "es>en", "fr>en", "de>en")
ARGOS_PACKAGES_DIR = os.environ.get("ARGOS_PACKAGES_DIR", "/app/argos-packages")


def _log(msg):
    print(msg, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def fail(msg, code="BAD_PARAM"):
    # one line of error JSON on stdout, then a non-zero exit. Keep writing the
    # error.json sidecar other callers (and the golden-path test) expect.
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    _write_error(msg)
    sys.exit(1)


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        raise ValueError("fields.json required on the input port")
    raw = p.read_bytes()
    if len(raw) > MAX_CHARS * 4:
        raise ValueError(f"fields.json exceeds the {MAX_CHARS * 4}-byte input cap")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fields.json must be a JSON object")
    return _normalize_fields(data)


# The platform hands us fields.json keyed by the spec's parameter names, which
# this domain spells in caps (SOURCE, TARGET). Accept the lowercase spelling
# too, and fold the text-port spellings onto one `text` key so the op stays a
# single pure function.
FIELD_ALIASES = {
    "source_language": "source",
    "target_language": "target",
    "payload": "text",
    "input_text": "text",
}


def _normalize_fields(data):
    out = {}
    for key, value in data.items():
        low = str(key).strip().lower()
        out[low] = value
        out[FIELD_ALIASES.get(low, low)] = value
    return out


def _text(fields):
    raw = str(fields.get("text", ""))
    if not raw.strip():
        raise ValueError(
            "Missing text: supply the text to translate on the node's text port"
        )
    if len(raw) > MAX_CHARS:
        raise ValueError(
            f"text is {len(raw)} chars, over the {MAX_CHARS}-char input cap — "
            "split it upstream (Text Split) or raise the cap deliberately"
        )
    return raw


def _pair(fields):
# Validate SOURCE/TARGET against the baked-pair allowlist.
# 
# "auto" is rejected on both ends: the engine has to load one specific baked
# package, so it cannot guess either language.
    source = str(fields.get("source", "")).strip().lower()
    target = str(fields.get("target", "")).strip().lower()
    if not source or not target:
        raise ValueError(
            "SOURCE and TARGET are both required (SOURCE=auto is not supported — "
            "the engine loads one specific baked package)"
        )
    if source == target:
        raise ValueError("SOURCE and TARGET must differ")
    pair = f"{source}>{target}"
    if pair not in ALLOWED_PAIRS:
        raise ValueError(
            f"pair {source}->{target} is not baked in this image "
            f"(allowed: {', '.join(ALLOWED_PAIRS)}); unbaked pairs are rejected, "
            "never downloaded, and never pivoted through a third language"
        )
    return source, target


def _chunks(text):
# Split on paragraph boundaries, packing paragraphs into <=CHUNK_MAX_CHARS
# calls. Order is preserved; a single oversized paragraph is hard-split on
# character boundaries as a last resort.
    paragraphs = [p for p in text.split("\n\n") if p.strip() != ""]
    chunks = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= CHUNK_MAX_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(para) > CHUNK_MAX_CHARS:
            chunks.append(para[:CHUNK_MAX_CHARS])
            para = para[CHUNK_MAX_CHARS:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def _baked_package_path(source, target):
# Locate the .argosmodel baked for this pair, or None.
# 
# Only ever looks inside ARGOS_PACKAGES_DIR — the baked dir. There is no
# download path in this file at all.
    base = Path(ARGOS_PACKAGES_DIR)
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.argosmodel")):
        name = path.name.lower()
        if f"{source}2{target}" in name or (
            f"{source}_{target}" in name or f"{source}-to-{target}" in name
        ):
            return str(path)
    return None


def _load_engine(source, target):
# Return the argostranslate translation object for this baked pair.
    path = _baked_package_path(source, target)
    if path is None:
        raise ValueError(
            f"no baked package found for {source}->{target} in {ARGOS_PACKAGES_DIR}"
        )
    from argostranslate import package, translate

    # Offline: install_from_path on the already-baked local file just indexes it.
    # There is no URL anywhere in this file — argostranslate never fetches here.
    package.install_from_path(path)
    engine = translate.get_translation_from_codes(source, target)
    if engine is None:
        raise ValueError(f"installed package for {source}->{target} not found after install")
    return engine


def op_translate_text(fields):
# Translate the payload -> translated.txt + meta.json.
    text = _text(fields)
    source, target = _pair(fields)
    engine = _load_engine(source, target)
    pieces = [engine.translate(chunk) for chunk in _chunks(text)]
    translated = "\n\n".join(p for p in pieces if p is not None)
    _write_text("translated.txt", translated)
    _write_text(
        "meta.json",
        json.dumps(
            {
                "source": source,
                "target": target,
                "chars": len(translated),
                "engine": ENGINE,
            },
            indent=2,
        ),
    )
    _log(
        f"translated {len(text)} chars {source}->{target} in {len(pieces)} chunk(s) "
        f"-> translated.txt ({len(translated)} chars)"
    )


OPS = {
    "translate_text": op_translate_text,
}


def main():
    op = os.getenv("TRANSLATE_OP", "translate_text").strip().lower()
    # The spec default is the node's own op key ("translate_text"); the container
    # speaks the same name. Accept a "translation_" prefix just in case.
    if op not in OPS and op.startswith("translation_"):
        op = op[len("translation_"):]
    if op not in OPS:
        fail(f"unknown TRANSLATE_OP {op!r}", code="BAD_PARAM")
    try:
        fields = _read_fields()
        OPS[op](fields)
    except ValueError as exc:
        fail(str(exc) or exc.__class__.__name__, code="BAD_PARAM")
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        fail(f"{type(exc).__name__}: {exc}", code="INTERNAL")


if __name__ == "__main__":
    main()
"""


# ---------------------------------------------------------------------------
# NodeDefinition specs (plain dicts — importable without a DB)
# ---------------------------------------------------------------------------

TRANSLATE_TEXT_SPEC = {
    "name": "Translate Text",
    "description": (
        "Offline machine translation of plain text. Reads the text on the node's "
        "input port (`text`) and emits `translated` — the translated text — plus "
        "meta.json {source, target, chars, engine}. Pick the language pair with the "
        "SOURCE and TARGET parameters; the image bakes en<->es, en<->fr and en<->de "
        "only, and any other pair is rejected rather than downloaded. SOURCE=auto is "
        "NOT supported: the engine loads one specific baked package, so it must be "
        "told both ends. Input is capped at 50k chars and is chunked by paragraph "
        "(<=5k chars per engine call) with order preserved. Engine is argostranslate; "
        "there is no network access at runtime."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "translate_text",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "text": {"type": "text", "description": "Text to translate (fields.json)."},
    },
    "output_schema": {
        "output_type": "text",
        "translated": {
            "type": "text",
            "description": "Translated text (translated.txt).",
        },
    },
    "configuration_schema": {
        "TRANSLATE_OP": {
            "type": "string",
            "default": "translate_text",
            "description": (
                "Fixed for this node: selects the translation operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "SOURCE": {
                "type": "string",
                "default": "en",
                "enum": ["en", "es", "fr", "de"],
                "description": (
                    "Source language code. Must be paired with a compatible TARGET; "
                    "only en<->es, en<->fr and en<->de are baked. 'auto' is not "
                    "supported — the engine loads one specific baked package."
                ),
            },
            "TARGET": {
                "type": "string",
                "default": "es",
                "enum": ["es", "fr", "de", "en"],
                "description": (
                    "Target language code. Must differ from SOURCE and form one of "
                    "the baked pairs."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 2,
}

NODE_DEFINITIONS = [
    TRANSLATE_TEXT_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "translate_text": TRANSLATE_TEXT_SPEC,
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


def register_translation(db: Session) -> int:
    """Register the translation operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping translation domain-primitive seed"
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
