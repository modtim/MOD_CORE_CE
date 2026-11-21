"""node_definitions_domains/tts — Offline text-to-speech NodeDefinition
(Phase 6 domain primitive, sz-6.7.1).

Registers one system node definition backed by the tts container
(`CORE/DockerContainers/tts`): `tts_synthesize` — text in, WAV audio out.

Posture notes
-------------
- This is the SMALL OFFLINE CPU option. The GPU/NIM speech nodes (Studio Voice,
  Maxine TTS via seed_asr_nim_node.py) wake a model on demand; this node never
  touches a GPU, a NIM, or the network at runtime — the piper voice is baked
  into the container image at build time and espeak-ng is apt-installed as the
  fallback engine.
- Specs are plain dicts (same shape as `seed_converters.py` /
  `crypto_utils.py`) so the module is importable + testable without a DB;
  `register_tts()` is the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog speaks: the
  conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility
  checks, AND named ports (`text` -> `audio` + `meta`) so the UI can label the
  ports this domain actually exposes. `text`, `audio` and `json` are all in
  the catalog's port-type vocabulary.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing
  system row rather than duplicating it (system-node names are globally
  unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `crypto_utils.py` does — existing rows are never deleted.
- Op selection: the spec pins a flat `TTS_OP` default equal to its own
  NODE_DEFINITIONS_BY_OP key (the bug the sibling domain modules shipped: no
  op env var meant every node ran the container's default op).
- Runtime safety: VOICE is restricted to the baked allowlist (a value is never
  treated as a path, nothing is ever downloaded), SPEED is bounded 0.5..2.0,
  SENTENCE_SILENCE 0..2 s, text capped at 20k chars, subprocess is argv-list
  only (never shell=True) and the text always goes to STDIN, never into argv.
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

CONTAINER_REPO_TAG = "tts:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/tts/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# Module-level mirrors of the entrypoint's tunables (the entrypoint keeps its
# own copy inside the container).
VOICES = ("en_US-lessac-medium",)
ENGINES = ("piper", "espeak")
DEFAULT_ENGINE = "piper"
SPEED_MIN, SPEED_MAX = 0.5, 2.0
SILENCE_MIN, SILENCE_MAX = 0.0, 2.0

# ---------------------------------------------------------------------------
# Entrypoint source — mirrored in the Dockerfile via heredoc so the container
# is self-contained (one file, no sidecar to keep in sync).
# ---------------------------------------------------------------------------

ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# tts entrypoint — offline text-to-speech (sz-6.7.1). One operation per run.
#
#   export TTS_OP=synthesize          # the node spec default is tts_synthesize
#   python3 /app/tts_process.py
#
# I/O contract (same as every other MOD node container): the worker stages the
# node's input files into /data/input and collects /data/output. Parameters
# arrive as /data/input/fields.json; the text may also arrive as a staged
# text/plain file (/data/input/input.txt). Output: result.wav + meta.json
# {voice, sample_rate, duration_s, chars}. Non-zero exit on bad input with a
# JSON error in /data/output/error.json.
#
# Vet step 5 (fail-closed): on ANY failure, exactly ONE line of JSON goes to
# STDOUT and the process exits non-zero. Codes: NO_INPUT, BAD_PARAM,
# PROCESSING_FAILURE, OUTPUT_NOT_WRITTEN, INTERNAL. stderr logging is kept and
# error.json is still written for the worker. Input contents are never echoed.
#
# Offline posture: NO network at runtime. The piper voice (.onnx + config .json)
# is baked into the image at build time and espeak-ng is apt-installed, so the
# VOICE parameter is checked against an allowlist — it is never treated as a
# path and nothing is ever downloaded. subprocess is argv-list only (never
# shell=True) and the text always goes to STDIN, never into argv, so process
# listings never carry user text.

import json
import os
import subprocess
import sys
import wave
from pathlib import Path

IN = Path("/data/input")
OUT = Path("/data/output")
MAX_FIELDS = 10 * 1024 * 1024  # 10 MB fields.json cap
MAX_TEXT = 20_000              # characters

ENGINES = ("piper", "espeak")
DEFAULT_ENGINE = "piper"
VOICES = ("en_US-lessac-medium",)  # baked at image build — the only ones
ESPEAK_VOICE = {"en_US-lessac-medium": "en-us"}
VOICE_MODEL = {v: Path(f"/app/piper/models/{v}.onnx") for v in VOICES}
VOICE_CONFIG = {v: Path(f"/app/piper/models/{v}.json") for v in VOICES}
SPEED_MIN, SPEED_MAX = 0.5, 2.0
SILENCE_MIN, SILENCE_MAX = 0.0, 2.0
ESPEAK_BASE_WPM = 150
RUN_TIMEOUT = 300  # seconds of wall-clock per engine subprocess

CODE_NO_INPUT = "NO_INPUT"
CODE_BAD_PARAM = "BAD_PARAM"
CODE_PROCESSING = "PROCESSING_FAILURE"
CODE_OUTPUT_NOT_WRITTEN = "OUTPUT_NOT_WRITTEN"
CODE_INTERNAL = "INTERNAL"


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code=CODE_INTERNAL):
    # Vet step 5: one JSON line on stdout, then exit non-zero.
    _log(f"ERROR: {msg}")
    _write_error(msg, code)
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


class NodeError(ValueError):
    # A failure whose contract code is already decided by the caller.

    def __init__(self, msg, code):
        super().__init__(str(msg))
        self.code = code


def _write_error(msg, code=CODE_INTERNAL):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "error.json").write_text(
        json.dumps({"error": str(msg), "code": code}, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_fields():
    p = IN / "fields.json"
    if not p.is_file():
        # The text may arrive as a staged text/plain file instead; params then
        # fall back to their defaults.
        return {}
    raw = p.read_bytes()
    if len(raw) > MAX_FIELDS:
        raise NodeError(
            f"fields.json exceeds the {MAX_FIELDS}-byte input cap", CODE_NO_INPUT
        )
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise NodeError("fields.json must be a JSON object", CODE_BAD_PARAM)
    return _normalize_fields(data)


# The platform hands us fields.json keyed by the spec's parameter names, which
# this domain spells in caps (TEXT, TTS_ENGINE, TTS_VOICE, TTS_SPEED,
# TTS_SENTENCE_SILENCE). Fold them onto the lowercase short names the ops use.
def _normalize_fields(data):
    out = {}
    for k, v in data.items():
        key = str(k).strip().lower()
        if key.startswith("tts_"):
            key = key[len("tts_"):]
        out[key] = v
    return out
def _resolve_text(fields):
    # Text from the `text` field, else from a staged text/plain file.
    text = fields.get("text")
    if text is None:
        for name in ("input.txt", "text.txt", "text"):
            p = IN / name
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                break
    if text is None:
        raise NodeError(
            "no text on the input port (fields.json `text` or input.txt)", CODE_NO_INPUT
        )
    if not isinstance(text, str):
        raise NodeError("text must be a string", CODE_BAD_PARAM)
    text = text.strip()
    if not text:
        raise NodeError("text is empty", CODE_NO_INPUT)
    if len(text) > MAX_TEXT:
        raise NodeError(f"text exceeds the {MAX_TEXT}-character cap", CODE_BAD_PARAM)
    return text


def _validate_params(fields):
    # Engine/voice/speed/silence, validated against the baked allowlists.
    engine = str(fields.get("engine") or DEFAULT_ENGINE).strip().lower()
    if engine not in ENGINES:
        raise NodeError(
            f"unknown TTS_ENGINE {engine!r} (piper|espeak)", CODE_BAD_PARAM
        )
    voice = str(fields.get("voice") or VOICES[0]).strip()
    if voice not in VOICES:
        raise NodeError(
            f"unknown TTS_VOICE {voice!r} — only {', '.join(VOICES)} is baked into "
            "the image (no paths, no runtime downloads)",
            CODE_BAD_PARAM,
        )
    try:
        speed = float(fields.get("speed", 1.0))
    except (TypeError, ValueError):
        raise NodeError("TTS_SPEED must be a number", CODE_BAD_PARAM)
    if not SPEED_MIN <= speed <= SPEED_MAX:
        raise NodeError(f"TTS_SPEED {speed} outside {SPEED_MIN}..{SPEED_MAX}", CODE_BAD_PARAM)
    try:
        silence = float(fields.get("sentence_silence", 0.5))
    except (TypeError, ValueError):
        raise NodeError("TTS_SENTENCE_SILENCE must be a number", CODE_BAD_PARAM)
    if not SILENCE_MIN <= silence <= SILENCE_MAX:
        raise NodeError(
            f"TTS_SENTENCE_SILENCE {silence} outside {SILENCE_MIN}..{SILENCE_MAX}",
            CODE_BAD_PARAM,
        )
    return {"engine": engine, "voice": voice, "speed": speed, "silence": silence}


def _build_argv(params):
    # Argv list for the engine — the text is NEVER part of argv.
    voice, speed, silence = params["voice"], params["speed"], params["silence"]
    if params["engine"] == "piper":
        model = VOICE_MODEL[voice]
        if not model.is_file():
            raise NodeError(
                f"baked piper voice {voice} is missing from the image", CODE_PROCESSING
            )
        return [
            "piper",
            "--model", str(model),
            "--output_file", "-",
            "--speed", str(speed),
            "--sentence-silence", str(silence),
        ]
    return [
        "espeak-ng",
        "-v", ESPEAK_VOICE[voice],
        "-e", str(int(round(ESPEAK_BASE_WPM * speed))),
        "-z", str(int(round(silence * 1000))),
        "-w", str(OUT / "_espeak.wav"),
    ]


def _run_engine(argv, text):
    # Run argv with the text on STDIN; return raw WAV bytes.
    argv = list(argv)
    if argv[0] == "espeak-ng":
        out_path = Path(argv[-1])
        try:
            proc = subprocess.run(argv, input=text.encode("utf-8"), capture_output=True, timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise NodeError("espeak-ng timed out", CODE_PROCESSING)
        except FileNotFoundError:
            raise NodeError("espeak-ng binary is missing from the image", CODE_PROCESSING)
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise NodeError(
                str(proc.stderr.decode("utf-8", "ignore")).strip() or "espeak-ng failed",
                CODE_PROCESSING,
            )
        raw = out_path.read_bytes()
        out_path.unlink(missing_ok=True)
        return raw
    try:
        proc = subprocess.run(argv, input=text.encode("utf-8"), capture_output=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise NodeError("piper timed out", CODE_PROCESSING)
    except FileNotFoundError:
        raise NodeError("piper binary is missing from the image", CODE_PROCESSING)
    if proc.returncode != 0:
        raise NodeError(
            str(proc.stderr.decode("utf-8", "ignore")).strip() or "piper failed",
            CODE_PROCESSING,
        )
    if not proc.stdout:
        raise NodeError("piper produced no audio", CODE_PROCESSING)
    return proc.stdout


def _read_wav(raw):
    # ((channels, rate, samplewidth), frames) from raw WAV bytes.
    import io
    if not raw:
        raise NodeError("engine produced no audio", CODE_PROCESSING)
    with wave.open(io.BytesIO(raw), "rb") as w:
        params = (w.getnchannels(), w.getframerate(), w.getsampwidth())
        return params, w.readframes(w.getnframes())


def _silence_bytes(params, seconds):
    channels, rate, width = params
    frames = max(0, int(round(rate * seconds)))
    return b"\x00" * (frames * channels * width)


def _join_wavs(parts, silence_s):
    # Concatenate WAV byte-parts with `silence_s` of silence between them.
    import io
    if not parts:
        raise NodeError("engine produced no audio", CODE_PROCESSING)
    params, first = _read_wav(parts[0])
    chunks = [first]
    for raw in parts[1:]:
        p2, frames = _read_wav(raw)
        if p2 != params:
            raise NodeError("engine output formats differ", CODE_PROCESSING)
        chunks.append(frames)
    if silence_s > 0:
        pad = _silence_bytes(params, silence_s)
        chunks = [c for pair in zip(chunks, [pad] * len(chunks)) for c in pair][:-1]
    body = b"".join(chunks)
    channels, rate, width = params
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(body)
    return buf.getvalue(), params, len(body) // (channels * width)


def op_synthesize(fields):
    text = _resolve_text(fields)
    params = _validate_params(fields)
    argv = _build_argv(params)
    raw = _run_engine(argv, text)
    wav, fmt, nframes = _join_wavs([raw], params["silence"])
    _write_bytes("result.wav", wav)
    _write_json("meta.json", {
        "voice": params["voice"],
        "sample_rate": fmt[1],
        "duration_s": round(nframes / fmt[1], 3) if fmt[1] else 0.0,
        "chars": len(text),
    })
    _log(f"tts_synthesize ok: {params['engine']}/{params['voice']} {fmt[1]}Hz {nframes} frames")


def _write_json(name, value):
    _write_bytes(name, (json.dumps(value) + "\n").encode("utf-8"))


def _write_bytes(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        (OUT / name).write_bytes(value)
    except OSError as exc:
        raise NodeError(f"could not write {name}: {exc}", CODE_OUTPUT_NOT_WRITTEN)


OPS = {"synthesize": op_synthesize}


def main():
    try:
        op = os.getenv("TTS_OP", "synthesize").strip().lower()
        # The spec default is the node's own op key ("tts_synthesize"); the
        # container speaks the short name. Accept either spelling.
        if op not in OPS and op.startswith("tts_"):
            op = op[len("tts_"):]
        if op not in OPS:
            fail(f"unknown TTS_OP {op!r}", CODE_BAD_PARAM)
        fields = _read_fields()
        OPS[op](fields)
    except SystemExit:
        raise
    except NodeError as exc:
        fail(str(exc), exc.code)
    except Exception as exc:  # noqa: BLE001 — report, never crash with a traceback
        fail(f"{type(exc).__name__}: {exc}", CODE_INTERNAL)


if __name__ == "__main__":
    main()
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

TTS_SYNTHESIZE_SPEC = {
    "name": "Text to Speech",
    "description": (
        "Offline, CPU-only speech synthesis: text in, WAV audio out. Runs "
        "entirely inside the tts container — the piper voice is baked into the "
        "image at build time and espeak-ng is installed as the fallback "
        "engine — so it needs no GPU, no NIM wake, and no network at runtime. "
        "Pick this over the GPU/NIM speech nodes (Studio Voice, Maxine TTS) "
        "when you need a small deterministic voice on any worker: the text "
        "(up to 20k characters, from the `text` port or a staged text/plain "
        "file) is synthesised with the `TTS_VOICE` voice (only voices baked "
        "into the image are accepted), at `TTS_SPEED` 0.5..2.0 with "
        "`TTS_SENTENCE_SILENCE` 0..2 s between sentences, and emitted on the "
        "`audio` port plus a `meta` port carrying {voice, sample_rate, "
        "duration_s, chars}. Bad input exits non-zero with JSON in "
        "/data/output/error.json. Backed by the tts container (piper-tts + "
        "espeak-ng)."
    ),
    "type": "Data",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "tts_synthesize",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "text",
        "text": {
            "type": "text",
            "description": (
                "Text to speak (up to 20000 characters). Accepts a text "
                "port or a staged text/plain file."
            ),
        },
    },
    "output_schema": {
        "output_type": "audio",
        "audio": {
            "type": "audio",
            "description": "Synthesised speech as a WAV file.",
        },
        "meta": {
            "type": "json",
            "description": (
                "Synthesis metadata: voice, sample_rate, duration_s, chars."
            ),
        },
    },
    "configuration_schema": {
        "TTS_OP": {
            "type": "string",
            "default": "tts_synthesize",
            "description": (
                "Fixed for this node: selects the tts-container operation. "
                "Leave it as-is unless you have a reason to change it."
            ),
        },
        "extra_variables": {
            "TTS_ENGINE": {
                "type": "string",
                "default": "piper",
                "enum": ["piper", "espeak"],
                "description": (
                    "Synthesis engine. piper (neural, baked voice) is the "
                    "default; espeak is the plain fallback when you want a "
                    "tiny, always-present formant engine."
                ),
            },
            "TTS_VOICE": {
                "type": "string",
                "default": "en_US-lessac-medium",
                "enum": list(VOICES),
                "description": (
                    "Voice to use. Only voices baked into the tts image at "
                    "build time are accepted — this is never a path and "
                    "nothing is ever downloaded at runtime."
                ),
            },
            "TTS_SPEED": {
                "type": "number",
                "default": 1.0,
                "minimum": SPEED_MIN,
                "maximum": SPEED_MAX,
                "description": "Speech rate multiplier, 0.5 (slow) to 2.0 (fast).",
            },
            "TTS_SENTENCE_SILENCE": {
                "type": "number",
                "default": 0.5,
                "minimum": SILENCE_MIN,
                "maximum": SILENCE_MAX,
                "description": "Silence inserted between sentences, in seconds.",
            },
        },
    },
    "accepted_formats": ["text"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [
    TTS_SYNTHESIZE_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "tts_synthesize": TTS_SYNTHESIZE_SPEC,
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


def register_tts(db: Session) -> int:
    """Register the tts operation as a system node definition.

    Returns the number of NEW rows created. Idempotent: an existing system
    row with the same name has its schema/description/cost reconciled to this
    file (so a spec edit reaches a live deployment without a manual DB edit),
    and is not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping tts domain-primitive seed"
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

