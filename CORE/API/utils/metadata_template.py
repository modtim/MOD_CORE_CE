"""Pure DAG-metadata rendering — no env vars, no I/O, no side effects.

This is the single source of truth for the METADATA BLOCK that the
wizard's Review step produces and the node-scanner reads back.

One live caller renders through this module:

  - The API's wizard commit path — takes the user's edited metadata
    dict from the wizard Review step, calls `render(metadata)`, hands
    the result to ImageBuilder. This is the server-side re-render
    that guarantees what the user sees in the preview is what
    actually gets committed (no byte drift).

A TypeScript mirror lives at `CORE/WorkflowBuilder/lib/metadataTemplate.ts`
for the wizard's live preview. Parity between the two is enforced by
`CORE/API/tests/test_metadata_template.py` — both renderers consume the
same fixture set and must produce byte-identical output.

The function is intentionally primitive. No Jinja, no f-string kwargs
shenanigans, no conditional nesting. If the TypeScript side diverges,
it's obvious why.

DAGs intentionally do NOT carry variant/queue info. `process_type` and
`gpu_variants` live on the `NodeDefinition` DB row; the scheduler reads
them at enqueue time and picks the queue. Keeping the metadata file free of
those fields means re-scanning a DAG can't silently overwrite the
wizard's chosen variants. See `CORE/API/utils/queue_selector.py`
(`select_queue_for_node`) for the routing rule.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Optional


VALID_INPUT_TYPES = {
    "image", "video", "mesh", "audio", "text", "texture",
    "pointcloud", "pdf", "document", "json", "csv", "binary", "other",
}
VALID_OUTPUT_TYPES = VALID_INPUT_TYPES
# Kept defined for non-DAG consumers (node_builder.analyze_prompt,
# NodeDefinition validation). Not used by the worker renderer any more.
VALID_PROCESS_TYPES = {"CPU", "CUDA", "ROCM", "INTEL", "NPU", "ACCELERATOR"}


@dataclass
class NodeMetadata:
    """Typed shape of the metadata dict the renderer consumes."""
    node_name: str
    executable_path: str
    description: str = ""
    input_type: str = "image"
    output_type: str = "image"
    cost_in_credits: int = 1
    tenant_id: Optional[int] = None
    container_timeout: int = 7200
    container_env_extras: Optional[dict[str, str]] = None
    accepted_formats: Optional[list[str]] = None

    def to_dict(self) -> dict:
        return asdict(self)


def to_snake(name: str) -> str:
    """'FFmpeg Video Converter' -> 'ffmpeg_video_converter'.
    The TS mirror must produce the same output."""
    s = re.sub(r"[^a-zA-Z0-9\s]", "", name)
    return re.sub(r"\s+", "_", s.strip()).lower()


def safe_name(name: str) -> str:
    """'FFmpeg Video Converter' -> 'FFmpegVideoConverter'. Used for the
    filename. No spaces, no special chars."""
    return re.sub(r"[^a-zA-Z0-9]", "", name.replace(" ", ""))


def infer_category(input_type: str, output_type: str) -> str:
    """Bucket for the Dag.py filename suffix: Texture/Mesh/Video/Audio/General."""
    type_map = {
        "image": "Texture",
        "texture": "Texture",
        "mesh": "Mesh",
        "video": "Video",
        "audio": "Audio",
        "text": "General",
    }
    return type_map.get(
        output_type.lower(),
        type_map.get(input_type.lower(), "General"),
    )


def default_description(node_name: str, input_type: str, output_type: str) -> str:
    return f"{node_name} - processes {input_type} files and produces {output_type} output"


def default_accepted_formats(input_type: str) -> list[str]:
    fmt_map = {
        "image": ["jpg", "jpeg", "png", "tif", "tiff"],
        "texture": ["jpg", "jpeg", "png", "tif", "tiff"],
        "video": ["mp4", "mov", "mkv", "webm"],
        "audio": ["wav", "mp3", "flac", "ogg"],
        "mesh": ["obj", "fbx", "gltf", "glb", "stl"],
        "text": ["txt", "md", "json", "yaml", "csv"],
        "pdf": ["pdf"],
        "document": ["docx", "doc", "odt", "rtf"],
        "pointcloud": ["ply", "pcd", "las", "laz", "xyz"],
        "json": ["json"],
        "csv": ["csv", "tsv"],
        # `binary` and `other` intentionally stay open (empty list →
        # accepted_formats=None upstream, meaning accept-all).
    }
    return fmt_map.get(input_type.lower(), [])


def render_filename(node_name: str, category: str, tenant_id: Optional[int]) -> str:
    base = safe_name(node_name)
    if tenant_id is not None:
        return f"tenant_{tenant_id}_{base}_PROCESS_Node.py"
    return f"{base}_{category.upper()}_PROCESS_Node.py"


def _json_dump_compact(obj: Any) -> str:
    """Compact JSON for embedding in the generated .py file. Stable
    key order. Must match the TS equivalent `JSON.stringify(obj, null, 0)`
    for parity — both default to insertion order for objects."""
    return json.dumps(obj, separators=(", ", ": "), ensure_ascii=False)


def render(metadata: dict) -> dict:
    """Render a METADATA BLOCK from a metadata dict.

    Returns `{"filename": str, "content": str}`. The content is the
    full `.py` file text, ready to be written to disk or stored in the
    `metadata_file_source` DB column.

    Deterministic — same input always produces the same output, byte
    for byte. The TS mirror must match this output exactly.
    """
    m = NodeMetadata(**{k: v for k, v in metadata.items() if k in NodeMetadata.__dataclass_fields__})

    # Derived fields.
    node_kind = to_snake(m.node_name)
    category = infer_category(m.input_type, m.output_type)
    description = m.description or default_description(m.node_name, m.input_type, m.output_type)
    accepted_formats = m.accepted_formats if m.accepted_formats is not None \
        else default_accepted_formats(m.input_type)
    tenant_line = (
        f"tenant_id = {m.tenant_id}"
        if m.tenant_id is not None
        else "tenant_id = None"
    )

    # configuration_schema: compact JSON-in-Python.
    if m.container_env_extras:
        extras = {k: "string" for k in m.container_env_extras}
        config_schema = "{\"extra_variables\": " + _json_dump_compact(extras) + "}"
    else:
        config_schema = "{\"extra_variables\": \"NONE\"}"

    # input/output schemas are stable shapes.
    input_schema = (
        "{\"input_type\": \"" + m.input_type + "\", "
        "\"file_mode\": \"both\", "
        "\"input_variables\": \"NONE\"}"
    )
    output_schema = (
        "{\"output_type\": \"" + m.output_type + "\", "
        "\"file_mode\": \"both\", "
        "\"paired\": True, "
        "\"output_variables\": \"NONE\"}"
    )

    accepted_formats_literal = _json_dump_compact(accepted_formats)

    content = (
        "# --- METADATA BLOCK ---\n"
        f"name = \"{m.node_name}\"\n"
        f"description = \"{description}\"\n"
        f"node_kind = \"{node_kind}\"\n"
        f"category = \"{category}\"\n"
        f"executable_path = \"{m.executable_path}\"\n"
        f"input_schema = {input_schema}\n"
        f"output_schema = {output_schema}\n"
        f"configuration_schema = {config_schema}\n"
        "metadata = {\"accepts_metadata\": True, \"metadata_required\": False}\n"
        "batch_mode = \"batch\"\n"
        f"accepted_formats = {accepted_formats_literal}\n"
        f"cost_in_credits = {m.cost_in_credits}\n"
        f"{tenant_line}\n"
        "image_pull_policy = \"always\"\n"
        f"container_timeout = {m.container_timeout}\n"
        "# --- END METADATA BLOCK ---\n"
    )

    return {
        "filename": render_filename(m.node_name, category, m.tenant_id),
        "content": content,
        "node_kind": node_kind,
        "category": category,
    }


def coerce_and_validate(metadata: dict) -> dict:
    """Server-side sanitiser. Clips enums to valid values, enforces
    numeric ranges, strips unknown keys. Never raises — the wizard's
    commit step calls this before render() so the output is always
    canonical even if the user (or a buggy client) sent junk."""
    m = dict(metadata or {})

    name = (m.get("node_name") or "").strip()
    if not name:
        name = "Untitled Node"

    exe = (m.get("executable_path") or "").strip()
    if not exe:
        exe = safe_name(name).lower() + ":latest"

    def _enum(v, valid, default):
        return v if isinstance(v, str) and v in valid else default

    def _open_type(v, default):
        # input_type / output_type are metadata labels, not a closed
        # enum — domain-specific formats (ifc, parquet, dicom, ...) are
        # legitimate values. Pass any non-empty string through; fall
        # back only when the field is missing.
        if not isinstance(v, str):
            return default
        cleaned = v.strip().lower()
        return cleaned or default

    input_type = _open_type(m.get("input_type"), "other")
    output_type = _open_type(m.get("output_type"), "other")

    def _clamp(v, lo, hi, default):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return default
        if n < lo:
            return lo
        if n > hi:
            return hi
        return n

    cost_in_credits = _clamp(m.get("cost_in_credits", 1), 0, 10_000, 1)
    container_timeout = _clamp(m.get("container_timeout", 7200), 60, 86_400, 7200)

    tenant_id = m.get("tenant_id")
    if tenant_id is not None:
        try:
            tenant_id = int(tenant_id)
        except (TypeError, ValueError):
            tenant_id = None

    container_env_extras = m.get("container_env_extras") or None
    if container_env_extras and not isinstance(container_env_extras, dict):
        container_env_extras = None

    accepted_formats = m.get("accepted_formats")
    if accepted_formats is not None and not isinstance(accepted_formats, list):
        accepted_formats = None

    description = (m.get("description") or "").strip()

    eph = m.get("ephemeral_resources")
    if not isinstance(eph, dict):
        eph = {}
    shm_mb = _clamp(eph.get("shm_mb", 0), 0, 16_384, 0)
    tmpfs_mb = _clamp(eph.get("tmpfs_mb", 0), 0, 16_384, 0)

    out = {
        "node_name": name[:200],
        "executable_path": exe[:300],
        "description": description[:400],
        "input_type": input_type,
        "output_type": output_type,
        "cost_in_credits": cost_in_credits,
        "container_timeout": container_timeout,
        "tenant_id": tenant_id,
        "container_env_extras": container_env_extras,
        "accepted_formats": accepted_formats,
    }
    if shm_mb or tmpfs_mb:
        out["ephemeral_resources"] = {"shm_mb": shm_mb, "tmpfs_mb": tmpfs_mb}
    return out
