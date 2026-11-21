"""node_definitions_domains/image_embeddings — Offline image-embeddings NodeDefinition.

Registers one system node definition, `Embed Image`, backed by the
image-embeddings container (`CORE/DockerContainers/image-embeddings`): image
files in, `embeddings.jsonl` + `meta.json` out.

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_image_embeddings()` is the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already
  speaks: the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility
  checks, AND named ports (`file` -> `embeddings`) so the UI can label
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
unknown or path-like values are rejected. `trust_remote_code=False` is explicit
everywhere.

License note (confirmed via HF API at review time)
---------------------------------------------------
`sentence-transformers/clip-ViT-B-32` carries NO `license` field in its HF
API metadata or model-card front-matter (checked at review time via
`GET https://huggingface.co/api/models/sentence-transformers/clip-ViT-B-32`).
The weights trace to OpenAI's original CLIP release, which IS MIT-licensed
per the `openai/CLIP` GitHub repo's LICENSE file — but that is external
knowledge, not something this specific HF mirror asserts. Re-check the HF
repo and the upstream OpenAI LICENSE before shipping if this ever changes.
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

CONTAINER_REPO_TAG = "image-embeddings:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/image-embeddings/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# image-embeddings entrypoint — embed one or more image files (CLIP-class).
#
#   export EMBED_OP=embed_image   (default, fixed)
#   python3 /app/embed_process.py
#
# I/O contract: the worker stages the node's input files into /data/input and
# collects /data/output. Parameters arrive as /data/input/fields.json.
#
# Offline posture: sentence-transformers with models baked at IMAGE BUILD time.
# HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at runtime.
# trust_remote_code=False is explicit everywhere.
#
# Baked models:
#   - sentence-transformers/clip-ViT-B-32  (no license tag on this HF repo's
#     model card; traces to OpenAI's MIT-licensed CLIP release)

import json
import os
import sys
from pathlib import Path
from PIL import Image

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_FILES = 256                   # batch cap (image files in /data/input)
MAX_PIXELS = 20_000_000           # per-image pixel cap (20 MP)
MAX_BYTES = 25 * 1024 * 1024      # per-image file-size cap (25 MiB)
BATCH_SIZE = 32                   # batch size for model inference
MODEL_DIMS = {
    "sentence-transformers/clip-ViT-B-32": 512,
}
ALLOWED_MODELS = frozenset(MODEL_DIMS.keys())
ENGINE = "sentence-transformers"
ALLOWED_FORMATS = frozenset({"png", "jpg", "jpeg", "webp"})

# PIL refuses to even open files above the cap (decompression-bomb guard), so
# the pixel cap is enforced before any full decode happens.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def _log(msg):
    print(msg, flush=True)


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
        if low == "model":
            out["model"] = value
        elif low == "normalize":
            out["normalize"] = value
    return out


def _validate_model(fields):
    model = str(fields.get("model", "")).strip()
    if not model:
        raise ValueError(
            "model is required: supply a model name (e.g. "
            "sentence-transformers/clip-ViT-B-32)"
        )
    # Reject path-like values BEFORE checking allowlist (safety: never load from path)
    if "/" in model.split("/")[-1] or model.startswith("/") or model.startswith("."):
        raise ValueError(
            f"model {model!r} looks like a path — must be a HF model id only"
        )
    if model not in ALLOWED_MODELS:
        raise ValueError(
            f"unknown model {model!r}; allowed: {', '.join(sorted(ALLOWED_MODELS))}; "
            "models requiring trust_remote_code=True are not supported"
        )
    return model


def _validate_normalize(fields):
    norm = fields.get("normalize", True)
    if isinstance(norm, str):
        norm = norm.lower() not in ("false", "0", "no")
    elif not isinstance(norm, bool):
        norm = bool(norm)
    return norm


def _list_images():
    # Image files staged on the input port, sorted by name, fields.json excluded.
    # Directories are not walked (a directory/zip-free batch): only the plain
    # files the worker stages directly under /data/input count.
    if not IN.is_dir():
        raise ValueError("input port /data/input is missing")
    names = []
    for p in sorted(IN.iterdir()):
        if not p.is_file():
            raise ValueError(
                f"input port contains a non-file entry {p.name!r}; "
                "directories/zips are not accepted"
            )
        if p.name == "fields.json":
            continue
        names.append(p.name)
    if not names:
        raise ValueError("no image files on the input port")
    if len(names) > MAX_FILES:
        raise ValueError(
            f"input port exceeds {MAX_FILES}-file batch cap ({len(names)} files)"
        )
    return names


def _check_image(name):
    # Validate one staged file BEFORE decoding it fully: size cap from stat,
    # format + pixel cap from the header PIL reads lazily.
    path = IN / name
    size_bytes = path.stat().st_size
    if size_bytes > MAX_BYTES:
        raise ValueError(
            f"{name} is {size_bytes} bytes, over the {MAX_BYTES}-byte (25 MiB) cap"
        )
    try:
        with Image.open(path) as img:
            fmt, dims = img.format, img.size
    except Exception:  # noqa: BLE001 — report the file name, never its bytes
        raise ValueError(f"{name} is not a readable image file") from None
    if fmt is None or str(fmt).strip().lower() not in ALLOWED_FORMATS:
        raise ValueError(
            f"{name} has format {fmt!r}; allowed: {', '.join(sorted(ALLOWED_FORMATS))}"
        )
    pixels = int(dims[0]) * int(dims[1])
    if pixels > MAX_PIXELS:
        raise ValueError(
            f"{name} is {pixels} px, over the {MAX_PIXELS}-pixel (20 MP) cap"
        )
    return fmt


def _embed_images(names, model, normalize):
    # Embed a list of file names using sentence-transformers.
    #
    # Returns a list of dicts: {"id", "model", "dim", "embedding": [floats]}.
    from sentence_transformers import SentenceTransformer

    model_obj = SentenceTransformer(
        model,
        device="cpu",
        trust_remote_code=False,
    )

    results = []
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        images = [str(IN / name) for name in batch]
        vectors = model_obj.encode(
            images,
            normalize_embeddings=normalize,
            show_progress_bar=False,
            batch_size=BATCH_SIZE,
        )

        for name, emb in zip(batch, vectors):
            results.append({
                "id": name,
                "model": model,
                "dim": int(len(emb)),
                "embedding": [float(v) for v in emb],
            })

    return results


def op_embed_image(fields):
    # Embed image(s) -> embeddings.jsonl + meta.json.
    model = _validate_model(fields)
    normalize = _validate_normalize(fields)
    names = _list_images()

    for name in names:
        _check_image(name)

    results = _embed_images(names, model, normalize)

    if not results:
        raise ValueError("no embeddings produced after embedding input")

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
        f"embedded {len(results)} image(s) with {model} "
        f"(normalize={normalize}, dim={meta['dim']}) -> embeddings.jsonl"
    )


OPS = {
    "embed_image": op_embed_image,
}


def main():
    op = os.getenv("EMBED_OP", "embed_image").strip().lower()
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

EMBED_IMAGE_SPEC = {
    "name": "Embed Image",
    "description": (
        "Offline image embedding (CLIP-class) using sentence-transformers. "
        "Accepts one image or a batch of up to 256 image files (png/jpeg/webp) "
        "staged on the input port, each ≤20 MP and ≤25 MiB (checked with PIL "
        "before any full decode), and emits `embeddings.jsonl` "
        "(`{\"id\", \"model\", \"dim\", \"embedding\":[floats]}` per line, id = "
        "filename) plus meta.json {model, dim, count, normalized, engine}. "
        "Engine is sentence-transformers with models BAKED at image build time; "
        "HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at runtime. Models are "
        "restricted to the baked allowlist (sentence-transformers/clip-ViT-B-32, "
        "weights trace to OpenAI's MIT-licensed CLIP). trust_remote_code=False "
        "is explicit. Errors "
        "name the offending file only — never its bytes. NORMALIZE (bool, "
        "default true) L2-normalizes embeddings."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "embed_image",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to embed."},
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
            "default": "embed_image",
            "description": (
                "Fixed for this node: selects the embedding operation. "
                "Leave it as-is."
            ),
        },
        "extra_variables": {
            "MODEL": {
                "type": "string",
                "default": "sentence-transformers/clip-ViT-B-32",
                "enum": ["sentence-transformers/clip-ViT-B-32"],
                "description": (
                    "HuggingFace model ID. Must be one of the baked models. "
                    "Models requiring trust_remote_code=True are not supported."
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
    "accepted_formats": ["png", "jpg", "jpeg", "webp"],
    "cost_in_credits": 2,
}

NODE_DEFINITIONS = [
    EMBED_IMAGE_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "embed_image": EMBED_IMAGE_SPEC,
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


def register_image_embeddings(db: Session) -> int:
    """Register the image-embeddings operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping image-embeddings domain-primitive seed"
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
