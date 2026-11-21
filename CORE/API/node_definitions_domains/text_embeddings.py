"""node_definitions_domains/text_embeddings — Offline text-embeddings NodeDefinition.

Registers one system node definition, `Embed Text`, backed by the
text-embeddings container (`CORE/DockerContainers/text-embeddings`): plain text
or JSONL in, `embeddings.jsonl` + `meta.json` out.

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_text_embeddings()` is the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already
  speaks: the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility
  checks, AND named ports (`text` -> `embeddings`) so the UI can label
  the ports this domain actually exposes.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it.
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as the sibling domain modules do — existing rows are never
  deleted.
- Every spec pins its own operation with a flat `EMBED_OP` default equal to
  its `NODE_DEFINITIONS_BY_OP` key.

Offline posture
---------------
The engine is sentence-transformers with models BAKED at image build time,
pinned by HF commit revision. Nothing at RUNTIME reaches out:
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. Models must be in the allowlist;
unknown or path-like values are rejected.
`trust_remote_code=False` is explicit everywhere.

Known gap — nomic-embed-text and other models requiring
`trust_remote_code=True` are NOT supported and are deliberately excluded.
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

CONTAINER_REPO_TAG = "text-embeddings:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/text-embeddings/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# text-embeddings entrypoint — embed one or more text documents.
#
#   export EMBED_OP=embed_text   (default, fixed)
#   python3 /app/embed_process.py
#
# I/O contract: the worker stages the node's input files into /data/input and
# collects /data/output. Parameters arrive as /data/input/fields.json.
#
# Offline posture: sentence-transformers with baked models.
# HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at runtime.
#
# Known gap: models that require trust_remote_code=True (e.g. nomic-embed-text)
# are NOT supported.

import json
import os
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_TEXT_CHARS = 8_000          # per-text length cap
MAX_LINES = 2_000               # JSONL line limit
BATCH_SIZE = 32                 # batch size for model inference
MODEL_MAX_LENGTHS = {
    "Snowflake/snowflake-arctic-embed-m-v1.5": 512,
    "mixedbread-ai/mxbai-embed-large-v1": 512,
}
# Model prefixes: only query mode uses a prefix (document mode = no prefix).
MODEL_PREFIXES = {
    "Snowflake/snowflake-arctic-embed-m-v1.5": "Represent this sentence for retrieval: ",
    "mixedbread-ai/mxbai-embed-large-v1": "",
}
ALLOWED_MODELS = frozenset(MODEL_MAX_LENGTHS.keys())
ENGINE = "sentence-transformers"

# Query vs document prefix. Arctic uses a query prefix for queries only.
INPUT_KIND_DOCUMENT = "document"
INPUT_KIND_QUERY = "query"


def _log(msg):
    print(msg, flush=True)


def _write_text(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(value, encoding="utf-8")


def _write_error(msg):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(json.dumps({"error": str(msg)}), encoding="utf-8")


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        raise ValueError("fields.json required on the input port")
    raw = p.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fields.json must be a JSON object")
    return _normalize_fields(data)


def _normalize_fields(data):
    out = {}
    for key, value in data.items():
        low = str(key).strip().lower()
        out[low] = value
        # Also accept common aliases
        if low == "input_text":
            out["text"] = value
        elif low == "input_kind":
            out["input_kind"] = value
        elif low == "normalize":
            out["normalize"] = value
        elif low == "model":
            out["model"] = value
    return out


def _validate_model(fields):
    model = str(fields.get("model", "")).strip()
    if not model:
        raise ValueError(
            "model is required: supply a model name (e.g. "
            "Snowflake/snowflake-arctic-embed-m-v1.5)"
        )
    # Reject path-like values BEFORE checking allowlist (safety: never load from path)
    if "/" in model.split("/")[-1] or model.startswith("/") or model.startswith("."):
        raise ValueError(
            f"model {model!r} looks like a path — must be a HF model id only"
        )
    if model not in ALLOWED_MODELS:
        raise ValueError(
            f"unknown model {model!r}; allowed: {', '.join(sorted(ALLOWED_MODELS))}; "
            "models requiring trust_remote_code=True (e.g. nomic-embed-text) are not supported"
        )
    return model


def _validate_input_kind(fields):
    kind = str(fields.get("input_kind", INPUT_KIND_DOCUMENT)).strip().lower()
    if kind not in (INPUT_KIND_DOCUMENT, INPUT_KIND_QUERY):
        raise ValueError(
            f"input_kind must be '{INPUT_KIND_DOCUMENT}' or '{INPUT_KIND_QUERY}', "
            f"got {kind!r}"
        )
    return kind


def _validate_normalize(fields):
    norm = fields.get("normalize", True)
    if isinstance(norm, str):
        norm = norm.lower() not in ("false", "0", "no")
    elif not isinstance(norm, bool):
        norm = bool(norm)
    return norm


def _load_texts(fields):
    # Read texts from fields.json: single text or JSONL lines.
    #
    # If 'text' is present as a string, treat as single-document mode.
    # If 'input_jsonl' is present (multi-line string or base64), treat as JSONL mode.
    # Returns a list of (id, text) tuples.
    single_text = str(fields.get("text", "")).strip()
    jsonl_raw = str(fields.get("input_jsonl", "")).strip()

    if jsonl_raw:
        # JSONL mode
        lines = jsonl_raw.split("\n")
        results = []
        for i, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            if len(results) >= MAX_LINES:
                raise ValueError(
                    f"JSONL exceeds {MAX_LINES}-line limit (error on line {i})"
                )
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"malformed JSON on line {i}: {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise ValueError(
                    f"JSONL line {i} must be a JSON object, not {type(obj).__name__}"
                )
            text = str(obj.get("text", ""))
            text_id = str(obj.get("id", f"line_{i}"))
            if len(text) > MAX_TEXT_CHARS:
                raise ValueError(
                    f"text on line {i} exceeds {MAX_TEXT_CHARS}-char cap ({len(text)} chars)"
                )
            results.append((text_id, text))
        return results
    elif single_text:
        # Single text mode
        if len(single_text) > MAX_TEXT_CHARS:
            raise ValueError(
                f"text exceeds {MAX_TEXT_CHARS}-char cap ({len(single_text)} chars)"
            )
        return [("input", single_text)]
    else:
        raise ValueError(
            "missing input: supply 'text' for single document or 'input_jsonl' for "
            f"JSONL (up to {MAX_LINES} lines, each text ≤{MAX_TEXT_CHARS} chars)"
        )


def _build_prefix(text, model, input_kind):
    # Apply model's documented prefix for query mode only.
    if input_kind == INPUT_KIND_QUERY:
        prefix = MODEL_PREFIXES.get(model, "")
        if prefix:
            return prefix + text
    return text


def _embed_texts(texts, model, input_kind, normalize):
    # Embed a list of (id, text) pairs using sentence-transformers.
    #
    # Returns a list of dicts: {"id", "model", "dim", "embedding": [floats]}.
    from sentence_transformers import SentenceTransformer

    model_obj = SentenceTransformer(
        model,
        device="cpu",
        trust_remote_code=False,
    )

    results = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        batch_texts = [_build_prefix(t, model, input_kind) for _, t in batch]

        embeddings = model_obj.encode(
            batch_texts,
            normalize_embeddings=normalize,
            show_progress_bar=False,
            batch_size=BATCH_SIZE,
        )

        for (text_id, _), emb in zip(batch, embeddings):
            results.append({
                "id": text_id,
                "model": model,
                "dim": int(len(emb)),
                "embedding": [float(v) for v in emb],
            })

    return results


def op_embed_text(fields):
    # Embed text(s) -> embeddings.jsonl + meta.json.
    model = _validate_model(fields)
    input_kind = _validate_input_kind(fields)
    normalize = _validate_normalize(fields)
    texts = _load_texts(fields)

    if not texts:
        raise ValueError("no texts to embed after parsing input")

    results = _embed_texts(texts, model, input_kind, normalize)

    # Ensure output directory exists
    OUT.mkdir(parents=True, exist_ok=True)

    # Write embeddings.jsonl
    lines = []
    for r in results:
        lines.append(json.dumps(r, separators=(",", ":")))
    (OUT / "embeddings.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Write meta.json
    meta = {
        "model": model,
        "dim": results[0]["dim"] if results else 0,
        "count": len(results),
        "normalized": normalize,
        "engine": ENGINE,
    }
    (OUT / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    _log(
        f"embedded {len(results)} text(s) with {model} "
        f"(kind={input_kind}, normalize={normalize}, dim={meta['dim']}) "
        "-> embeddings.jsonl"
    )


OPS = {
    "embed_text": op_embed_text,
}


def main():
    op = os.getenv("EMBED_OP", "embed_text").strip().lower()
    if op not in OPS:
        _log(f"ERROR: unknown EMBED_OP {op!r}")
        _write_error(f"unknown EMBED_OP {op!r}")
        sys.exit(1)
    try:
        fields = _read_fields()
        OPS[op](fields)
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        msg = str(exc) or exc.__class__.__name__
        _log(f"ERROR: {msg}")
        _write_error(msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
"""


# ---------------------------------------------------------------------------
# NodeDefinition specs (plain dicts — importable without a DB)
# ---------------------------------------------------------------------------

EMBED_TEXT_SPEC = {
    "name": "Embed Text",
    "description": (
        "Offline text embedding using sentence-transformers. Accepts plain text "
        "(one document) or JSONL (`{\"id\":..,\"text\":..}` per line, up to 2,000 "
        "lines, each text ≤8k chars) and emits `embeddings.jsonl` ("
        "`{\"id\", \"model\", \"dim\", \"embedding\":[floats]}` per line) plus "
        "meta.json {model, dim, count, normalized, engine}. Engine is "
        "sentence-transformers with models BAKED at image build time; "
        "HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at runtime. Models are "
        "restricted to the baked allowlist (Snowflake Arctic + Mixedbread). "
        "trust_remote_code=False is explicit. Supports INPUT_KIND (document|query, "
        "default document) and NORMALIZE (bool, default true). Known gap: models "
        "requiring trust_remote_code=True (e.g. nomic-embed-text) are NOT supported."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "embed_text",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "text": {"type": "text", "description": "Input text (single doc or fields.json)."},
    },
    "output_schema": {
        "output_type": "json",
        "embeddings": {
            "type": "json",
            "description": "Embeddings output (embeddings.jsonl).",
        },
    },
    "configuration_schema": {
        "EMBED_OP": {
            "type": "string",
            "default": "embed_text",
            "description": (
                "Fixed for this node: selects the embedding operation. "
                "Leave it as-is."
            ),
        },
        "extra_variables": {
            "MODEL": {
                "type": "string",
                "default": "Snowflake/snowflake-arctic-embed-m-v1.5",
                "enum": [
                    "Snowflake/snowflake-arctic-embed-m-v1.5",
                    "mixedbread-ai/mxbai-embed-large-v1",
                ],
                "description": (
                    "HuggingFace model ID. Must be one of the baked models. "
                    "Models requiring trust_remote_code=True are not supported."
                ),
            },
            "INPUT_KIND": {
                "type": "string",
                "default": "document",
                "enum": ["document", "query"],
                "description": (
                    "Input kind for model prefix handling. 'query' applies "
                    "the model's query prefix; 'document' does not."
                ),
            },
            "NORMALIZE": {
                "type": "string",
                "default": "true",
                "enum": ["true", "false"],
                "description": (
                    "Whether to L2-normalize embeddings. Default: true."
                ),
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 2,
}

NODE_DEFINITIONS = [
    EMBED_TEXT_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "embed_text": EMBED_TEXT_SPEC,
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


def register_text_embeddings(db: Session) -> int:
    """Register the text-embeddings operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping text-embeddings domain-primitive seed"
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
