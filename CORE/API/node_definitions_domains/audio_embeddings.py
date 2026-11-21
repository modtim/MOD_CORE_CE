"""node_definitions_domains/audio_embeddings — Offline audio-embeddings NodeDefinition.

Registers one system node definition, `Embed Audio`, backed by the
audio-embeddings container (`CORE/DockerContainers/audio-embeddings`): audio
files in, `embeddings.jsonl` + `meta.json` out.

Posture notes
-------------
- Specs are plain dicts so the module is importable + testable without a DB;
  `register_audio_embeddings()` is the only DB-touching entry point.
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
The engine is `transformers` (ClapModel + ClapProcessor) with the model BAKED
at image build time, pinned by HF commit revision. Nothing at RUNTIME reaches
out: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. Models must be in the
allowlist; unknown or path-like values are rejected. `trust_remote_code=False`
is explicit everywhere.

Decoding: soundfile (+ libsndfile) for wav/flac/ogg; mp3 via the ffmpeg CLI as
an argv list (never a shell, `-nostdin`, hard timeout, and the input path is
always the one `_input_path()` proved lives under /data/input).

License note
------------
`laion/clap-htsat-unfused` is Apache-2.0 (confirmed via the HF models API
`cardData.license` field, sha 8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a).
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

CONTAINER_REPO_TAG = "audio-embeddings:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/audio-embeddings/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# audio-embeddings entrypoint — embed one or more audio files (LAION-CLAP).
#
#   export EMBED_OP=embed_audio   (default, fixed)
#   python3 /app/embed_process.py
#
# I/O contract: the worker stages the node's input files into /data/input and
# collects /data/output. Parameters arrive as /data/input/fields.json.
#
# Offline posture: transformers CLAP with the model baked at IMAGE BUILD time.
# HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at runtime.
# trust_remote_code=False is explicit everywhere.
#
# Decoding: soundfile (+ libsndfile) for wav/flac/ogg; mp3 via the ffmpeg CLI
# as an argv list (never a shell, -nostdin, hard timeout, input path always
# proven to sit under /data/input).
#
# Baked models:
#   - laion/clap-htsat-unfused  (Apache-2.0, confirmed via the HF models API
#     cardData.license field, sha 8fa0f1c6d0433df6e97c127f64b2a1d6c0dcda8a)

import io
import json
import os
import subprocess
import sys
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_FILES = 64                    # batch cap (audio files in /data/input)
MAX_BYTES = 25 * 1024 * 1024      # per-file size cap (25 MiB)
MAX_SECONDS = 60                  # per-file duration cap, after decode
TARGET_RATE = 48000               # CLAP sampling rate (48 kHz)
BATCH_SIZE = 16                   # inference batch size
PROBE_TIMEOUT = 60                # seconds for the ffprobe header read
DECODE_TIMEOUT = 120              # seconds for the ffmpeg mp3 decode
MODEL_DIMS = {
    "laion/clap-htsat-unfused": 512,
}
ALLOWED_MODELS = frozenset(MODEL_DIMS.keys())
ENGINE = "transformers"
ALLOWED_FORMATS = frozenset({"wav", "flac", "mp3", "ogg"})


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
            "laion/clap-htsat-unfused)"
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


def _input_path(name):
    # Resolve one staged file and prove it is a plain file directly under the
    # input port. The ffmpeg/ffprobe argv always receives THIS absolute path,
    # never a caller-supplied one.
    path = IN / name
    if not path.is_file():
        raise ValueError(f"{name} is not a regular file on the input port")
    if path.parent.resolve() != IN.resolve():
        raise ValueError(f"{name} escapes the /data/input input port")
    return path


def _list_audios():
    # Audio files staged on the input port, sorted by name, fields.json excluded.
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
        raise ValueError("no audio files on the input port")
    if len(names) > MAX_FILES:
        raise ValueError(
            f"input port exceeds {MAX_FILES}-file batch cap ({len(names)} files)"
        )
    return names


def _probe_mp3(path, name):
    # ffprobe (the ffmpeg CLI) reads the mp3 header BEFORE any decode. argv is a
    # list, never a shell string, and -nostdin keeps it off the controlling tty.
    argv = [
        "ffprobe",
        "-nostdin",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{name} header probe timed out") from None
    if proc.returncode != 0:
        raise ValueError(f"{name} is not a decodable audio file") from None
    try:
        duration = float((proc.stdout or "").strip())
    except ValueError:
        raise ValueError(f"{name} header has no duration") from None
    return "mp3", duration


def _check_audio(name):
    # Validate one staged file BEFORE decoding it fully: size cap from stat, then
    # format + duration from the header (soundfile.info / ffprobe), never from a
    # full decode. Errors name the FILE only, never its bytes.
    try:
        import soundfile as sf
    except ImportError as exc:  # noqa: BLE001
        raise ValueError("soundfile is required for audio decode — install soundfile") from exc  # noqa: B904
    path = _input_path(name)
    size_bytes = path.stat().st_size
    if size_bytes > MAX_BYTES:
        raise ValueError(
            f"{name} is {size_bytes} bytes, over the {MAX_BYTES}-byte (25 MiB) cap"
        )
    ext = name.rsplit(".", 1)[-1].strip().lower() if "." in name else ""
    if ext == "mp3":
        fmt, duration = _probe_mp3(path, name)
    else:
        try:
            info = sf.info(str(path))
        except Exception:  # noqa: BLE001 — report the file name, never its bytes
            raise ValueError(f"{name} is not a decodable audio file") from None
        fmt = str(info.format).strip().lower()
        duration = float(info.frames) / float(info.samplerate)
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(
            f"{name} has format {fmt!r}; allowed: {', '.join(sorted(ALLOWED_FORMATS))}"
        )
    if duration > MAX_SECONDS:
        raise ValueError(
            f"{name} is {duration:.1f} s, over the {MAX_SECONDS}-second duration cap"
        )
    return fmt, duration


def _to_mono(data):
    import numpy as np
    # libsndfile hands back (frames, channels); CLAP wants one channel.
    if data.ndim > 1:
        return data.mean(axis=1)
    return data


def _decode_ffmpeg(path, name):
    import soundfile as sf
    # mp3 via the ffmpeg CLI: fixed argv list, -nostdin, hard timeout, no shell,
    # and the input path is the one _input_path() proved lives under /data/input.
    argv = [
        "ffmpeg",
        "-nostdin",
        "-v", "error",
        "-i", str(path),
        "-ac", "1",
        "-ar", str(TARGET_RATE),
        "-f", "wav",
        "-",
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=DECODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ValueError(f"{name} decode timed out") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError(f"{name} is not a decodable audio file") from None
    samples, rate = sf.read(io.BytesIO(proc.stdout), always_2d=True)
    return _to_mono(samples)


def _resample(data, src_rate):
    import numpy as np
    # Resample mono float samples to TARGET_RATE with plain numpy (linear
    # interpolation): no scipy, no extra native dep, deterministic.
    src_rate = int(src_rate)
    if src_rate == TARGET_RATE:
        return data
    if src_rate <= 0 or data.size == 0:
        raise ValueError("cannot resample an empty or rate-less track")
    src = np.asarray(data, dtype=np.float64)
    out_len = int(round(src.shape[0] * TARGET_RATE / src_rate))
    if out_len <= 0:
        raise ValueError("track is too short to resample")
    pos = np.linspace(0.0, src.shape[0] - 1, out_len)
    left = np.floor(pos).astype(np.int64)
    right = np.minimum(left + 1, src.shape[0] - 1)
    frac = pos - left
    return src[left] * (1.0 - frac) + src[right] * frac


def _decode(name, fmt):
    import soundfile as sf
    # Decode one validated file to mono samples at the model's rate.
    path = _input_path(name)
    if fmt == "mp3":
        return _decode_ffmpeg(path, name)
    data, rate = sf.read(str(path), always_2d=True)
    return _resample(_to_mono(data), rate)


def _l2_normalize(vector):
    import numpy as np
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def _embed_audios(names, model, normalize, audios):
    # Embed a list of file names with transformers CLAP.
    #
    # Returns a list of dicts: {"id", "model", "dim", "embedding": [floats]}.
    from transformers import ClapModel, ClapProcessor

    model_obj = ClapModel.from_pretrained(model, trust_remote_code=False)
    model_obj = model_obj.to("cpu")
    processor = ClapProcessor.from_pretrained(model, trust_remote_code=False)

    results = []
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i:i + BATCH_SIZE]
        features = processor(
            audio=[audios[name] for name in batch],
            sampling_rate=TARGET_RATE,
            return_tensors="pt",
        )
        vectors = model_obj(**features).embeddings

        for name, emb in zip(batch, vectors):
            vector = [float(v) for v in emb]
            if normalize:
                vector = _l2_normalize(vector)
            results.append({
                "id": name,
                "model": model,
                "dim": len(vector),
                "embedding": vector,
            })

    return results


def op_embed_audio(fields):
    # Embed audio(s) -> embeddings.jsonl + meta.json.
    model = _validate_model(fields)
    normalize = _validate_normalize(fields)
    names = _list_audios()

    audios = {}
    for name in names:
        fmt, _duration = _check_audio(name)
        audios[name] = _decode(name, fmt)

    results = _embed_audios(names, model, normalize, audios)

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
        "sampling_rate": TARGET_RATE,
    }
    (OUT / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    _log(
        f"embedded {len(results)} audio file(s) with {model} "
        f"(normalize={normalize}, dim={meta['dim']}, rate={TARGET_RATE}) -> embeddings.jsonl"
    )


OPS = {
    "embed_audio": op_embed_audio,
}


def main():
    op = os.getenv("EMBED_OP", "embed_audio").strip().lower()
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

EMBED_AUDIO_SPEC = {
    "name": "Embed Audio",
    "description": (
        "Offline audio embedding (LAION-CLAP text+audio contrastive model) using "
        "transformers ClapModel + ClapProcessor. Accepts one audio file or a batch "
        "of up to 64 files (wav/flac/mp3/ogg) staged on the input port, each ≤25 MiB "
        "and ≤60 s — size from stat and duration from the file header (soundfile.info "
        "or an ffprobe header read) are checked BEFORE any full decode — and emits "
        "`embeddings.jsonl` (`{\"id\", \"model\", \"dim\", \"embedding\":[floats]}` "
        "per line, id = filename) plus meta.json {model, dim, count, normalized, "
        "engine, sampling_rate}. Tracks are resampled to the model's 48 kHz mono "
        "input rate. Engine is transformers with the model BAKED at image build time, "
        "pinned by HF commit revision; HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1 at "
        "runtime. Models are restricted to the baked allowlist "
        "(laion/clap-htsat-unfused, Apache-2.0). trust_remote_code=False is explicit. "
        "mp3 decodes through the ffmpeg CLI as an argv list (never a shell, -nostdin, "
        "hard timeout, input path always proven under /data/input). Errors name the "
        "offending file only — never its bytes. NORMALIZE (bool, default true) L2-"
        "normalizes embeddings."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "embed_audio",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "audio",
        "file": {"type": "audio", "description": "Audio file(s) to embed."},
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
            "default": "embed_audio",
            "description": (
                "Fixed for this node: selects the embedding operation. "
                "Leave it as-is."
            ),
        },
        "extra_variables": {
            "MODEL": {
                "type": "string",
                "default": "laion/clap-htsat-unfused",
                "enum": ["laion/clap-htsat-unfused"],
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
    "accepted_formats": ["wav", "flac", "mp3", "ogg"],
    "cost_in_credits": 3,
}

NODE_DEFINITIONS = [
    EMBED_AUDIO_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "embed_audio": EMBED_AUDIO_SPEC,
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


def register_audio_embeddings(db: Session) -> int:
    """Register the audio-embeddings operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping audio-embeddings domain-primitive seed"
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
