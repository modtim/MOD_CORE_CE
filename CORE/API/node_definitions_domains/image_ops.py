"""node_definitions_domains/image_ops — common image-manipulation NodeDefinitions.

launch-completion-megaplan sz-6.12.1 (Phase 6: Domain primitives). Registers
six system node definitions backed by the image-ops container
(`CORE/DockerContainers/image-ops`): resize, crop, rotate, read metadata, strip
metadata, and watermark.

Format conversion is deliberately NOT one of them: `seed_converters.py` already
registers an "Image Converter" system node (`image-converter:latest`, TARGET_FORMAT
= png/jpg/tiff/bmp/webp) that owns that operation. Duplicating it would give the
palette two nodes for the same job, so this module references it instead.

Posture notes
-------------
- Specs are plain dicts (same shape as `seed_converters.py` / `pdf_processing.py`)
  so the module is importable + testable without a DB; `register_image_ops()` is
  the only DB-touching entry point.
- `input_schema` / `output_schema` carry BOTH shapes the catalog already speaks:
  the conventional `input_type` / `output_type` pair that
  `services/port_type_enforcement.py` reads for wizard type-compatibility checks,
  AND named ports (`file` -> `file` / `metadata`) so the UI can label the ports
  this domain actually exposes. `image` and `json` are both in
  `node_builder/analyze_prompt.py`'s VALID_INPUT_TYPES.
- Idempotent by (name, tenant_id IS NULL): a re-run reconciles an existing system
  row rather than duplicating it (system-node names are globally unique).
- `MOD_SEED_BUNDLED_NODES` gates the INSERT path only (open-core-tier-structure
  §4.1), exactly as `seed_converters.py` does — existing rows are never deleted.
- `configuration_schema` uses the FLAT form `seed_converters.py` uses (variable
  name → {type, default, description}), and every spec pins its own `IMAGE_OP`
  default to its operation. One container serves five nodes, so the op is part
  of the spec, not something the user may leave unset — an unpinned op would
  silently run the container's default for every node.
- The container entrypoint lives here as `ENTRYPOINT_SOURCE` so the Dockerfile's
  quoted heredoc and the tests can both read one authoritative copy. The
  operation-specific pure functions (`resize_image`, `crop_image`, ...) are
  importable/execable without a container, which is what the unit tests do.
"""

import logging
import os

from sqlalchemy.orm import Session

from models import NodeDefinition
from utils.registries import platform_image

logger = logging.getLogger(__name__)

# Bare repo:tag of the container behind all five operations. The fully-qualified
# ref (registry + project) is resolved at seed time by platform_image() so a
# private-Harbor / on-prem deployment points at its own registry.
CONTAINER_REPO_TAG = "image-ops:latest"
CONTAINER_IMAGE = platform_image(CONTAINER_REPO_TAG)
DOCKERFILE_PATH = "CORE/DockerContainers/image-ops/Dockerfile"

PROCESS_TYPE = "CPU"
BATCH_MODE = "batch"

# The container entrypoint, kept here as the single authoritative copy so the
# Dockerfile's quoted heredoc and the unit tests can both read it. The Dockerfile
# writes exactly this text to /app/image_ops.py.
ENTRYPOINT_SOURCE = r"""#!/usr/bin/env python3
# image-ops entrypoint. One container, six operations, selected by IMAGE_OP:
#
#   IMAGE_OP=resize     width/height/keep_aspect/max_side -> same-named image
#   IMAGE_OP=crop       left/top/right/bottom             -> same-named image
#   IMAGE_OP=rotate     90/180/270 or any whole angle    -> same-named image
#   IMAGE_OP=metadata   EXIF -> JSON (GPS only on request) -> metadata.json
#   IMAGE_OP=strip      remove every metadata marker      -> same-named image
#   IMAGE_OP=watermark  TEXT/POSITION/OPACITY/FONT_SIZE/COLOR -> same-named image
#
# Same I/O contract as every other MOD node container: the worker stages the
# node's input files into /data/input and collects /data/output. Parameters
# come from the node's configuration_schema, injected as env vars (WIDTH,
# HEIGHT, KEEP_ASPECT, MAX_SIDE, LEFT, TOP, RIGHT, BOTTOM, ANGLE, INCLUDE_GPS);
# a params.json on the input port overrides them for hand-runs. IMAGE_OP is
# pinned by each NodeDefinition, never left to this default. Everything here
# treats its inputs as untrusted: no eval, no exec, no shell, a hard input-size
# cap, a decompression-bomb cap; bad input prints one line of error JSON and
# exits non-zero so the worker reports a failure instead of a crash.

import json
import os
import sys
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont
from PIL.Image import DecompressionBombError, Transpose
from PIL.TiffTags import TAGS_V2

# Decompression-bomb guard: 100 MP is far above anything MOD resizes, and
# Pillow's own default (~178 MP) is too generous for a shared box.
Image.MAX_IMAGE_PIXELS = 100_000_000

IN = Path("/data/input")
OUT = Path("/data/output")

MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_SIDE = 16384
JPEG_QUALITY = 92
MAX_FONT_SIZE = 2000
MAX_WATERMARK_TEXT_LEN = 500

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")
SAVE_FORMAT = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP",
               "tiff": "TIFF", "tif": "TIFF", "bmp": "BMP"}
ROTATE_STEPS = {90: Transpose.ROTATE_90, 180: Transpose.ROTATE_180,
                270: Transpose.ROTATE_270}
GPS_IFD = 0x0001


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def fail(msg, code="INTERNAL"):
    # every bad-input path ends here: one line of error JSON on stdout, then a
    # non-zero exit. code names the failure class (CE node JSON contract).
    print(json.dumps({"error": msg, "code": code}, sort_keys=True), flush=True)
    sys.exit(1)


def inputs():
    return [f for f in sorted(IN.iterdir()) if f.is_file() and not f.name.startswith(".")]


def images():
    return [f for f in inputs() if f.suffix.lower() in IMAGE_SUFFIXES]


PARAM_ENV_NAMES = ("WIDTH", "HEIGHT", "KEEP_ASPECT", "MAX_SIDE", "LEFT", "TOP",
                   "RIGHT", "BOTTOM", "ANGLE", "INCLUDE_GPS",
                   "WATERMARK_TEXT", "POSITION", "OPACITY", "FONT_SIZE", "COLOR")


def read_params():
    # Configuration comes from the node's configuration_schema, which the worker
    # injects as env vars (same contract as the pdf-processor container). A
    # params.json on the input port is accepted as an override for hand-runs.
    params = {name.lower(): os.getenv(name, "") for name in PARAM_ENV_NAMES}
    path = IN / "params.json"
    if not path.is_file():
        return params
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail("params.json is not valid JSON: %s" % exc, "BAD_PARAM")
        return params
    if not isinstance(data, dict):
        fail("params.json must be a JSON object", "BAD_PARAM")
    # params.json keys may be written either way (WIDTH/width); both map to the
    # same lowercase key the op functions read.
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    allowed = {name.lower() for name in PARAM_ENV_NAMES}
    unknown = sorted(set(lowered) - allowed)
    if unknown:
        fail("params.json keys %s are not image-ops parameters" % ", ".join(unknown), "BAD_PARAM")
    params.update({k: v for k, v in lowered.items() if k in allowed})
    return params


def check_size(path):
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        fail("%s is %d bytes, over the %d MB input cap"
             % (path.name, size, MAX_INPUT_BYTES // (1024 * 1024)), "BAD_PARAM")
    return path


def open_image(path):
    try:
        return Image.open(path)
    except DecompressionBombError as exc:
        fail("refused as a decompression bomb: %s" % exc, "PROCESSING_FAILURE")
    except OSError as exc:
        fail("cannot decode %s: %s" % (path.name, exc), "PROCESSING_FAILURE")


def as_bool(raw, name, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    fail("%s must be true or false" % name, "BAD_PARAM")
    return default


def as_int(raw, name, default):
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        fail("%s must be a whole number" % name, "BAD_PARAM")
    return default


def as_side(raw, name, default):
    value = as_int(raw, name, default)
    if value is None:
        return None
    if value < 1 or value > MAX_SIDE:
        fail("%s must be between 1 and %d pixels" % (name, MAX_SIDE), "BAD_PARAM")
    return value


def plan_resize(width, height, params):
    # Returns the target (w, h), or None when the image already fits.
    max_side = as_side(params.get("max_side"), "max_side", MAX_SIDE)
    keep = as_bool(params.get("keep_aspect"), "keep_aspect", True)
    target_w = as_side(params.get("width"), "width", None)
    target_h = as_side(params.get("height"), "height", None)
    if target_w is None and target_h is None:
        if max(width, height) <= max_side:
            return None
        target_w = target_h = max_side
    if keep:
        scale = min(
            (target_w or MAX_SIDE) / width,
            (target_h or MAX_SIDE) / height,
            max_side / max(width, height),
        )
        new_w = max(1, int(round(width * scale)))
        new_h = max(1, int(round(height * scale)))
    else:
        new_w = target_w or width
        new_h = target_h or height
    if max(new_w, new_h) > max_side:
        fail("target %dx%d exceeds max_side %d" % (new_w, new_h, max_side), "BAD_PARAM")
    if (new_w, new_h) == (width, height):
        return None
    return new_w, new_h


def plan_crop(size, params):
    width, height = size
    left = as_int(params.get("left"), "left", 0)
    top = as_int(params.get("top"), "top", 0)
    right = as_int(params.get("right"), "right", width)
    bottom = as_int(params.get("bottom"), "bottom", height)
    if left < 0 or top < 0 or right > width or bottom > height:
        fail("crop box %d,%d,%d,%d is outside the %dx%d image"
             % (left, top, right, bottom, width, height), "BAD_PARAM")
    if right - left < 1 or bottom - top < 1:
        fail("crop box %d,%d,%d,%d is empty" % (left, top, right, bottom), "BAD_PARAM")
    return left, top, right, bottom


def plan_angle(params):
    raw = as_int(params.get("angle"), "angle", 0)
    if raw < -360 or raw > 360:
        fail("angle must be between -360 and 360 degrees", "BAD_PARAM")
    return raw % 360


def jsonable(value):
    # EXIF values are ints, strings, tuples, Fractions and bytes; only
    # JSON-safe shapes may reach the metadata port.
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return str(value)


def metadata_dict(img, include_gps):
    exif = img.getexif()
    tags = {}
    for tag_id, value in exif.items():
        if tag_id == GPS_IFD and not include_gps:
            continue
        name = TAGS_V2[tag_id].name if tag_id in TAGS_V2 else "tag_%d" % tag_id
        tags[name] = jsonable(value)
    out = {
        "width": img.size[0],
        "height": img.size[1],
        "mode": img.mode,
        "format": img.format,
        "has_exif": bool(exif),
        "exif": tags,
    }
    if include_gps:
        out["gps"] = {
            "GPS_%d" % tag_id: jsonable(value)
            for tag_id, value in exif.get_ifd(GPS_IFD).items()
        }
    return out


def save_image(img, path):
    fmt = SAVE_FORMAT.get(path.suffix.lower().lstrip("."), "PNG")
    if fmt in ("JPEG", "BMP") and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if fmt == "JPEG":
        img.save(path, format=fmt, quality=JPEG_QUALITY)
    else:
        img.save(path, format=fmt)


def op_resize(paths):
    params = read_params()
    for path in paths:
        img = open_image(path)
        target = plan_resize(img.size[0], img.size[1], params)
        if target is None:
            log("INFO: %s already fits — copied through" % path.name)
        else:
            img = img.resize(target, Image.Resampling.LANCZOS)
            log("INFO: %s resized to %dx%d" % (path.name, target[0], target[1]))
        save_image(img, OUT / path.name)


def op_crop(paths):
    params = read_params()
    for path in paths:
        img = open_image(path)
        box = plan_crop(img.size, params)
        img.crop(box).save(OUT / path.name)
        log("INFO: %s cropped to %dx%d" % (path.name, box[2] - box[0], box[3] - box[1]))


def op_rotate(paths):
    angle = plan_angle(read_params())
    for path in paths:
        img = open_image(path)
        if angle in ROTATE_STEPS:
            img = img.transpose(ROTATE_STEPS[angle])
        else:
            img = img.rotate(angle, expand=True)
        save_image(img, OUT / path.name)
        log("INFO: %s rotated %d degrees" % (path.name, angle))


def op_metadata(paths):
    include_gps = as_bool(read_params().get("include_gps"), "include_gps", False)
    if len(paths) > 1:
        log("WARN: metadata reads the first image only: %s" % paths[0].name)
    img = open_image(paths[0])
    payload = json.dumps(metadata_dict(img, include_gps), indent=2, sort_keys=True)
    (OUT / "metadata.json").write_text(payload + "\n", encoding="utf-8")


def op_strip(paths):
    for path in paths:
        img = open_image(path)
        clean = img.copy()
        # Pillow carries JPEG/TIFF markers in .info; clearing it drops EXIF,
        # JFIF, dpi and the TIFF tag block, so the re-encode is metadata-free.
        clean.info.clear()
        save_image(clean, OUT / path.name)
        log("INFO: %s written with no metadata markers" % path.name)


def _parse_opacity(raw):
    if raw is None or raw == "":
        return 0.5
    try:
        val = float(raw)
    except (TypeError, ValueError):
        fail("OPACITY must be a number 0.0–1.0", "BAD_PARAM")
    if val < 0.0 or val > 1.0:
        fail("OPACITY must be between 0.0 and 1.0", "BAD_PARAM")
    return val


def _parse_position(raw):
    valid = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
    if raw is None or raw == "":
        return valid[-1]  # default: bottom-right
    text = str(raw).strip().lower().replace(" ", "-")
    if text not in valid:
        fail("POSITION must be one of %s" % ", ".join(valid), "BAD_PARAM")
    return text


def _parse_font_size(raw, image_height):
    if raw is None or raw == "":
        return max(8, int(image_height * 0.08))  # default ~8% of height
    try:
        val = int(raw)
    except (TypeError, ValueError):
        fail("FONT_SIZE must be a whole number", "BAD_PARAM")
    if val < 1 or val > MAX_FONT_SIZE:
        fail("FONT_SIZE must be between 1 and %d pixels" % MAX_FONT_SIZE, "BAD_PARAM")
    return val


def _text_position(position, text_size, image_size, margin=10):
    width, height = image_size
    tw, th = text_size
    if position == "top-left":
        return (margin, margin)
    if position == "top-right":
        return (width - tw - margin, margin)
    if position == "bottom-left":
        return (margin, height - th - margin)
    if position == "bottom-right":
        return (width - tw - margin, height - th - margin)
    # center
    return ((width - tw) // 2, (height - th) // 2)


def op_watermark(paths):
    params = read_params()
    text = params.get("watermark_text")
    if text is None or text == "":
        fail("WATERMARK_TEXT is required and must not be blank", "BAD_PARAM")
    if len(text) > MAX_WATERMARK_TEXT_LEN:
        fail("WATERMARK_TEXT must be %d characters or fewer" % MAX_WATERMARK_TEXT_LEN, "BAD_PARAM")
    position = _parse_position(params.get("position"))
    opacity = _parse_opacity(params.get("opacity"))
    color_raw = params.get("color", "white")
    color = str(color_raw).strip() if color_raw else "white"
    try:
        rgb = ImageColor.getrgb(color)
    except ValueError:
        fail("COLOR must be a valid CSS colour name or hex code", "BAD_PARAM")
    img_count = len(paths)
    for idx, path in enumerate(paths):
        img = open_image(path)
        img_w, img_h = img.size
        fs = _parse_font_size(params.get("font_size"), img_h)
        draw = ImageDraw.Draw(img)
        # Measure text
        font = ImageFont.load_default(size=fs)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x, y = _text_position(position, (tw, th), (img_w, img_h))
        # Draw as semi-transparent overlay
        alpha_val = int(opacity * 255)
        layer = Image.new("RGBA", (img_w, img_h), (255, 255, 255, 0))
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((x, y), text, font=font, fill=rgb + (alpha_val,))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, layer)
        img = img.convert("RGB")
        save_image(img, OUT / path.name)
        log("INFO: %s watermarked (%s, %g opacity)" % (path.name, position, opacity))
        if idx + 1 < img_count:
            log("INFO: watermarking image %d/%d" % (idx + 2, img_count))


OPS = {
    "resize": op_resize,
    "crop": op_crop,
    "rotate": op_rotate,
    "metadata": op_metadata,
    "strip": op_strip,
    "watermark": op_watermark,
}


def main():
    op = os.getenv("IMAGE_OP", "resize").strip().lower()
    if op not in OPS:
        fail("unknown IMAGE_OP %r — expected one of %s" % (op, sorted(OPS)), "BAD_PARAM")
    paths = [check_size(p) for p in images()]
    if not paths:
        fail("no image found in /data/input", "NO_INPUT")
    log("=== image-ops: %s (%d image) ===" % (op, len(paths)))
    OPS[op](paths)
    expected = ["metadata.json"] if op == "metadata" else [p.name for p in paths]
    missing = [n for n in expected
               if not (OUT / n).is_file() or (OUT / n).stat().st_size == 0]
    if missing:
        fail("expected output not written: %s" % ", ".join(missing),
             "OUTPUT_NOT_WRITTEN")


try:
    main()
except SystemExit:
    raise
except Exception as exc:
    fail("%s: %s" % (type(exc).__name__, exc), "INTERNAL")
"""

# ---------------------------------------------------------------------------
# NodeDefinitionSpec dicts — one per operation.
# ---------------------------------------------------------------------------

IMAGE_RESIZE_SPEC = {
    "name": "Image Resize",
    "description": (
        "Resize image files on the input port (`file`) and emit the resized "
        "files on `file`, keeping each file's name and format. Sides are "
        "capped at 16384 px. Set WIDTH/HEIGHT for an explicit target, KEEP_ASPECT "
        "(default true) to preserve the ratio, or MAX_SIDE to fit the longest "
        "edge. An image that already fits is copied through unchanged. Backed by "
        "the image-ops container (Pillow). For format changes use Image Converter."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_resize",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to resize."},
    },
    "output_schema": {
        "output_type": "image",
        "file": {"type": "image", "description": "Resized image, same name/format."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "resize",
            "description": "Fixed for this node: image-ops operation to run.",
        },
        "WIDTH": {
            "type": "string",
            "default": "",
            "description": "Target width in pixels (1-16384). Blank = unset.",
        },
        "HEIGHT": {
            "type": "string",
            "default": "",
            "description": "Target height in pixels (1-16384). Blank = unset.",
        },
        "KEEP_ASPECT": {
            "type": "string",
            "default": "true",
            "description": "Keep the original aspect ratio (default true).",
        },
        "MAX_SIDE": {
            "type": "string",
            "default": "16384",
            "description": "Longest allowed edge in pixels (1-16384).",
        },
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

IMAGE_CROP_SPEC = {
    "name": "Image Crop",
    "description": (
        "Crop image files on the input port (`file`) to a pixel box and emit "
        "them on `file`. The box is LEFT/TOP/RIGHT/BOTTOM in pixels (RIGHT/BOTTOM "
        "default to the image's own width/height), must stay inside the image and "
        "must not be empty — an out-of-range box is an error, not a silent clamp. "
        "Backed by the image-ops container (Pillow)."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_crop",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to crop."},
    },
    "output_schema": {
        "output_type": "image",
        "file": {"type": "image", "description": "Cropped image, same name/format."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "crop",
            "description": "Fixed for this node: image-ops operation to run.",
        },
        "LEFT": {"type": "string", "default": "0",
                 "description": "Left edge of the crop box, px."},
        "TOP": {"type": "string", "default": "0",
                "description": "Top edge of the crop box, px."},
        "RIGHT": {"type": "string", "default": "",
                  "description": "Right edge of the crop box, px (blank = full width)."},
        "BOTTOM": {"type": "string", "default": "",
                   "description": "Bottom edge of the crop box, px (blank = full height)."},
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

IMAGE_ROTATE_SPEC = {
    "name": "Image Rotate",
    "description": (
        "Rotate image files on the input port (`file`) and emit them on `file`. "
        "ANGLE is whole degrees: 90/180/270 are lossless quarter-turns, any other "
        "whole angle up to ±360 rotates with the canvas expanded so no corner is "
        "clipped. Backed by the image-ops container (Pillow)."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_rotate",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to rotate."},
    },
    "output_schema": {
        "output_type": "image",
        "file": {"type": "image", "description": "Rotated image, same name/format."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "rotate",
            "description": "Fixed for this node: image-ops operation to run.",
        },
        "ANGLE": {
            "type": "string",
            "default": "90",
            "description": "Whole degrees to rotate; 90/180/270 are lossless.",
        },
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

IMAGE_METADATA_SPEC = {
    "name": "Image Metadata",
    "description": (
        "Read the metadata of an image on the input port (`file`) and emit it as "
        "JSON on `metadata`: width, height, mode, container format, and the EXIF "
        "tag set by human-readable name. GPS is NOT included by default — set "
        "INCLUDE_GPS=true to get the GPS block, so location stays out of the "
        "compliance trail unless someone asked for it. Backed by the image-ops "
        "container (Pillow); no network, no geocoding."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_metadata",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image to read metadata from."},
    },
    "output_schema": {
        "output_type": "json",
        "metadata": {"type": "json", "description": "Image metadata as JSON."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "metadata",
            "description": "Fixed for this node: image-ops operation to run.",
        },
        "INCLUDE_GPS": {
            "type": "string",
            "default": "false",
            "description": "Include the GPS block (default false).",
        },
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

IMAGE_STRIP_SPEC = {
    "name": "Image Strip Metadata",
    "description": (
        "Remove every metadata marker from image files on the input port (`file`) "
        "and emit clean files on `file`: EXIF, JFIF, DPI and the TIFF tag block "
        "are all cleared before the re-encode, so camera/authorship/GPS data "
        "cannot travel with the asset. Pixel data is untouched. Backed by the "
        "image-ops container (Pillow)."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_strip",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to clean."},
    },
    "output_schema": {
        "output_type": "image",
        "file": {"type": "image", "description": "Metadata-free image, same name/format."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "strip",
            "description": "Fixed for this node: image-ops operation to run.",
        },
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

IMAGE_WATERMARK_SPEC = {
    "name": "Image Watermark",
    "description": (
        "Overlay a text watermark on image files on the input port (`file`) and "
        "emit the watermarked files on `file`. Requires WATERMARK_TEXT. Position "
        "defaults to bottom-right; choose from top-left, top-right, bottom-left, "
        "bottom-right, center. OPACITY is 0.0 (invisible) to 1.0 (solid); COLOR "
        "is a CSS colour name or hex code; FONT_SIZE defaults to ~8 % of image "
        "height. Backed by the image-ops container (Pillow)."
    ),
    "type": "Image",
    "executable_path": CONTAINER_IMAGE,
    "process_type": PROCESS_TYPE,
    "node_kind": "image_watermark",
    "batch_mode": BATCH_MODE,
    "input_schema": {
        "input_type": "image",
        "file": {"type": "image", "description": "Image file(s) to watermark."},
    },
    "output_schema": {
        "output_type": "image",
        "file": {"type": "image", "description": "Watermarked image, same name/format."},
    },
    "configuration_schema": {
        "IMAGE_OP": {
            "type": "string",
            "default": "watermark",
            "description": "Fixed for this node: image-ops operation to run.",
        },
        "WATERMARK_TEXT": {
            "type": "string",
            "default": "",
            "description": "Text to render as watermark (required).",
        },
        "POSITION": {
            "type": "string",
            "default": "bottom-right",
            "description": "Corner or center: top-left / top-right / bottom-left / bottom-right / center.",
        },
        "OPACITY": {
            "type": "string",
            "default": "0.5",
            "description": "Watermark opacity, 0.0 (invisible) to 1.0 (solid).",
        },
        "FONT_SIZE": {
            "type": "string",
            "default": "",
            "description": "Font size in pixels (blank = ~8 % of image height).",
        },
        "COLOR": {
            "type": "string",
            "default": "white",
            "description": "Watermark colour, CSS name or hex (default white).",
        },
    },
    "accepted_formats": ["png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp"],
    "cost_in_credits": 1,
}

NODE_DEFINITIONS = [
    IMAGE_RESIZE_SPEC,
    IMAGE_CROP_SPEC,
    IMAGE_ROTATE_SPEC,
    IMAGE_METADATA_SPEC,
    IMAGE_STRIP_SPEC,
    IMAGE_WATERMARK_SPEC,
]

NODE_DEFINITIONS_BY_OP = {
    "image_resize": IMAGE_RESIZE_SPEC,
    "image_crop": IMAGE_CROP_SPEC,
    "image_rotate": IMAGE_ROTATE_SPEC,
    "image_metadata": IMAGE_METADATA_SPEC,
    "image_strip": IMAGE_STRIP_SPEC,
    "image_watermark": IMAGE_WATERMARK_SPEC,
}

# The op each spec runs inside the container (IMAGE_OP env var). One container
# backs all five nodes, so the op is pinned per spec rather than left to the
# container default — see the test that asserts spec default == this map.
OP_ENV_VALUES = {
    "image_resize": "resize",
    "image_crop": "crop",
    "image_rotate": "rotate",
    "image_metadata": "metadata",
    "image_strip": "strip",
    "image_watermark": "watermark",
}

# Operations the image-ops container actually implements (mirrors the OPS map in
# ENTRYPOINT_SOURCE). Anything else the container accepts is a bug in this file.
CONTAINER_OPS = ("resize", "crop", "rotate", "metadata", "strip", "watermark")

RECONCILED_FIELDS = (
    "description",
    "node_kind",
    "executable_path",
    "input_schema",
    "output_schema",
    "configuration_schema",
    "accepted_formats",
    "cost_in_credits",
)


def register_image_ops(db: Session) -> int:
    """Register the 6 image operations as system node definitions.

    Returns the number of NEW rows created. Idempotent: an existing system row
    with the same name has its schema/description/cost reconciled to this file
    (so a spec edit reaches a live deployment without a manual DB edit), and is
    not counted as created.
    """
    if os.getenv("MOD_SEED_BUNDLED_NODES", "true").lower() not in ("true", "1", "yes"):
        logger.info(
            "MOD_SEED_BUNDLED_NODES is false — skipping image domain-primitive seed"
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
