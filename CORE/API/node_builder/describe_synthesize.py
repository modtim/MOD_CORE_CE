"""δ Phase 7 — wrapper-script synthesizer.

Given the user's description and (optionally) a chosen GitHub
repo, produces:

- ``script_content`` — Python wrapper that reads from
  /data/input/, calls into the chosen repo / does the work, and
  writes outputs to /data/output/.
- ``requirements`` — pip install list. May include
  ``git+https://github.com/...`` for the chosen repo.
- ``base_image_hint`` — variant_dir of one of the curated base
  images (Phase 1's CURATED_BASE_IMAGES). MUST be on the
  allowlist; the wizard refuses other values.
- ``suggested_input_type`` / ``suggested_output_type`` — mapped to
  the wizard's known type enum so Configure auto-fills cleanly.

Hard validation:

- script MUST reference both ``/data/input`` and ``/data/output``;
  otherwise the synthesis is rejected before reaching the user.
- ``base_image_hint`` MUST be a known variant_dir; the LLM cannot
  invent a base image (a hallucinated tag would 404 at build time).
- ``requirements`` may not contain shell pipe / && characters or
  whitespace inside a single requirement string (defends against
  the LLM emitting "git clone X && pip install" disguised as a
  requirement, which would then run during pip install).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.curated_base_images import CURATED_BASE_IMAGES
from utils.license_allowlist import pip_license_warnings
from utils.llm_client import LLMClientError, call_wizard_llm

logger = logging.getLogger(__name__)


# Common-category hints shown to the LLM and the wizard UI dropdown.
# NOT a validation gate — the backend accepts any non-empty string the
# LLM returns, so domain-specific formats (ifc, parquet, dicom, fasta,
# step, dwg, ...) survive end-to-end as the input_type metadata. The
# real filter for file routing is `accepted_formats` (free-form list of
# extensions) which already supports anything.
#
# Why this changed: the previous whitelist rejected the wizard's
# perfectly reasonable LLM suggestions for niche domains (AEC, GIS,
# scientific computing, ML artifacts) and coerced them to "other",
# losing useful semantic metadata. The handful of categories below are
# kept as suggestions only.
_COMMON_TYPES: List[str] = [
    "image",
    "video",
    "audio",
    "text",
    "json",
    "csv",
    "binary",
    "mesh",
    "texture",
    "pointcloud",
    "pdf",
    "document",
    "other",
]


# File-extension fingerprints per category. Used by
# ``_validate_io_contract`` to catch the photogrammetry-class bug
# where the user says "image input" and the synth-emitted script
# globs for ``*.mp4``/``*.avi``/``*.mov`` (treating the input as a
# video to be frame-extracted). The mismatch breaks the runtime
# contract — the user's actual JPEG folder will hit the script's
# "no input found" branch.
#
# Scope rule: only well-known, stable categorical types are listed
# here. Domain-specific formats (ifc, dicom, fasta, …) are not
# enforced — too niche to police and the LLM rarely gets them wrong.
# This is deliberately a SMALL vocab; we'd rather miss an
# obscure-format mismatch than reject a legitimate script for a
# format the script-extension check doesn't know about.
_TYPE_EXTENSIONS: Dict[str, List[str]] = {
    "image":      ["jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp", "exr", "hdr", "tga"],
    "video":      ["mp4", "avi", "mov", "mkv", "webm", "m4v", "mpg", "mpeg", "wmv", "flv"],
    "audio":      ["wav", "mp3", "flac", "ogg", "m4a", "aac", "opus", "wma"],
    "pdf":        ["pdf"],
    "text":       ["txt", "md", "rst", "log"],
    "json":       ["json"],
    "csv":        ["csv", "tsv"],
    "mesh":       ["obj", "ply", "stl", "fbx", "glb", "gltf", "off", "dae", "3ds", "x3d"],
    "pointcloud": ["ply", "pcd", "las", "laz", "xyz", "pts"],
}


# Copyleft system binaries. The wizard's banner promises permissive-only
# licences (MIT/Apache/BSD/ISC/MPL/Unlicense); calling these via
# `subprocess` ships a tenant-facing image with GPL/AGPL/LGPL code at
# runtime. Each entry maps the binary name to (SPDX, permissive
# alternative). Surfaced to the LLM so it picks the alternative on
# retry instead of declaring the copyleft binary via system_packages.
#
# Scope: only well-known offenders that the LLM commonly emits for
# image / pdf / video tasks. We're aiming at the 90 % case, not a
# comprehensive licence audit (that's `pip_license_warnings`'s job
# for pip packages + a future SCA pass for apt packages).
#
# `convert` repro: 2026-05-31 photogrammetry submission. LLM picked
# `subprocess.run(['convert', ...])` for image-format conversion
# despite the prompt telling it to avoid ImageMagick. Validator
# caught it as undeclared, but the rejection text didn't name the
# licence policy — so a retry could land on `system_packages=
# ['imagemagick']` and pass the binary-declared check while still
# violating the licence policy.
_COPYLEFT_BINARIES: Dict[str, Tuple[str, str]] = {
    # ImageMagick — GPL via Ghostscript bundle.
    "convert":   ("GPL-2.0+ (ImageMagick)", "Pillow (pip) for image conversion"),
    "magick":    ("GPL-2.0+ (ImageMagick)", "Pillow (pip) for image conversion"),
    # 'imagemagick' isn't even a real command — the model shells it out as one
    # (repro 2026-09-03: subprocess.run(['imagemagick', ...]) -> FileNotFoundError,
    # no output). Block it as GPL ImageMagick and redirect to Pillow.
    "imagemagick": ("GPL-2.0+ (ImageMagick; not a real command)", "Pillow (pip)"),
    "mogrify":   ("GPL-2.0+ (ImageMagick)", "Pillow (pip)"),
    "identify":  ("GPL-2.0+ (ImageMagick)", "Pillow (pip) for image inspection"),
    "composite": ("GPL-2.0+ (ImageMagick)", "Pillow (pip)"),
    "montage":   ("GPL-2.0+ (ImageMagick)", "Pillow (pip)"),
    # Ghostscript — AGPL.
    "gs":        ("AGPL-3.0 (Ghostscript)", "pypdfium2 (pip) for PDF rasterise"),
    "gsx":       ("AGPL-3.0 (Ghostscript)", "pypdfium2 (pip)"),
    # Poppler — GPL.
    "pdftoppm":  ("GPL-2.0+ (poppler-utils)", "pypdfium2 (pip)"),
    "pdftocairo":("GPL-2.0+ (poppler-utils)", "pypdfium2 (pip)"),
    "pdfinfo":   ("GPL-2.0+ (poppler-utils)", "pypdfium2 (pip)"),
    "pdftotext": ("GPL-2.0+ (poppler-utils)", "pypdfium2 (pip) + .get_text()"),
    "pdfimages": ("GPL-2.0+ (poppler-utils)", "pypdfium2 (pip)"),
    # MuPDF / mutool — AGPL.
    "mutool":    ("AGPL-3.0 (MuPDF)", "pypdfium2 (pip)"),
}

# Copyleft apt packages. Same shape; covers the case where the LLM
# DECLARES the package in `system_packages` (passing the binary-
# declared check) but the package itself violates the licence policy.
_COPYLEFT_APT_PACKAGES: Dict[str, Tuple[str, str]] = {
    "imagemagick":            ("GPL-2.0+", "Pillow (pip)"),
    "imagemagick-6.q16":      ("GPL-2.0+", "Pillow (pip)"),
    "imagemagick-7.q16":      ("GPL-2.0+", "Pillow (pip)"),
    "libmagick++-dev":        ("GPL-2.0+", "Pillow (pip)"),
    "ghostscript":            ("AGPL-3.0", "pypdfium2 (pip)"),
    "libgs10":                ("AGPL-3.0", "pypdfium2 (pip)"),
    "libgs-dev":              ("AGPL-3.0", "pypdfium2 (pip)"),
    "poppler-utils":          ("GPL-2.0+", "pypdfium2 (pip)"),
    "libpoppler-dev":         ("GPL-2.0+", "pypdfium2 (pip)"),
    "libmupdf-dev":           ("AGPL-3.0", "pypdfium2 (pip)"),
    "mupdf-tools":            ("AGPL-3.0", "pypdfium2 (pip)"),
}


SYSTEM_PROMPT = """You write Python wrapper scripts for a node-
style worker.

The runtime contract is:
- Input files appear in /data/input/ (read-only mount).
- Outputs must be written to /data/output/ (writable).
- The script's process exits 0 on success, non-zero on error.
- Log progress to stdout; stderr is for errors only.

Given a user's task description and (optionally) a chosen GitHub
repository, write a script that does the work. Prefer using the
chosen repo's public APIs; if the chosen repo is unsuitable, fall
back to standard-library or well-known PyPI packages.

Constraints:
- Must read input from /data/input/ — discover files via
  os.listdir or pathlib.
- Must write output to /data/output/ — at least one file or one
  printed line.
- One file per requirement. NO shell pipes, NO &&, NO embedded
  bash.
- requirements MUST be valid pip identifiers (PEP 508). You MAY
  use `git+https://github.com/owner/repo` when the chosen repo
  isn't on PyPI.

REQUIRED SCRIPT SHAPE — your script MUST keep this input/output
skeleton EXACTLY (fill in only the WORK where marked). The strings
`/data/input` and `/data/output` MUST appear literally, or the build
is rejected:

    from pathlib import Path
    IN = Path("/data/input")
    OUT = Path("/data/output")
    OUT.mkdir(parents=True, exist_ok=True)
    for src in sorted(IN.rglob("*")):
        if not src.is_file():
            continue
        # --- DO THE WORK on `src`; write result(s) under OUT ---
        # e.g. dst = OUT / (src.stem + ".png"); ...write dst...
    print("done")

You may adapt the loop (e.g. filter by extension, read a single
file, collect files first) but ALWAYS read inputs from
`/data/input` and write outputs to `/data/output`. Never hardcode
any other path. Settings are optional env vars with defaults
(os.getenv("NAME", <default>)) — never required-or-crash.

LICENSE POLICY — permissive dependencies only. The output ships in
tenant-facing images, so EVERY library you pick (and the native
engine it wraps) must be permissively licensed (MIT, Apache-2.0,
BSD, ISC, MPL-2.0). Do NOT use libraries that are — or that shell
out to / bundle — GPL, LGPL, AGPL, or SSPL code. This trips up PDF,
document, and media tasks especially, where the popular pick is
often a copyleft engine. Known substitutions (use the RIGHT column):
  - PDF rasterize/convert: use `pypdfium2` (BSD/Apache, bundles
    Google's PDFium). Do NOT use `pdf2image`/poppler (GPL),
    `PyMuPDF`/`fitz` (AGPL), or ImageMagick/`Wand` (pulls Ghostscript,
    AGPL) for PDF page rendering.
  - Images: `Pillow` (permissive) is fine; `opencv-python` is fine.
  - When unsure whether a library's native backend is copyleft, pick
    the permissive alternative or note the uncertainty in `reasoning`.

BANNED SYSTEM BINARIES — the wizard HARD-REJECTS any script that calls
these via `subprocess.run([X, ...])` / `os.system('X ...')`, and ALSO
rejects any `system_packages` / `dockerfile_extras` apt-get install
that names the package providing them. Declaring the apt package
does NOT save you — the licence check fires either way. Use the
permissive alternative listed:

  - `convert` / `mogrify` / `identify` (ImageMagick, GPL):
      → use Pillow (`pip install Pillow`)
  - `gs` (Ghostscript, AGPL):
      → use pypdfium2 (`pip install pypdfium2`)
  - `pdftoppm` / `pdftocairo` / `pdfinfo` / `pdftotext` / `pdfimages`
    (poppler-utils, GPL):
      → use pypdfium2
  - `mutool` (MuPDF, AGPL):
      → use pypdfium2

Same applies to declaring apt packages: `imagemagick`,
`ghostscript`, `poppler-utils`, `mupdf-tools`, `libmupdf-dev`, and
their variants are blocked. Do not put them in `system_packages` or
in any `apt-get install` line inside `dockerfile_extras`.

Output a single JSON object on one line, no prose, no markdown:

{"script_content": "...", "requirements": ["..."],
 "base_image_hint": "python-3.11",
 "system_packages": ["ffmpeg"],
 "dockerfile_extras": "",
 "suggested_input_type": "image",
 "suggested_output_type": "json",
 "reasoning": "one-sentence explanation"}

base_image_hint MUST be one of the variant_dir values you'll be
told about in the user prompt.

USER-TUNABLE PARAMETERS — expose the knobs a user would reasonably want
to change as environment variables, read with `os.getenv("NAME", "default")`
(or `os.environ.get`). The wizard scans the script for these reads and
turns each one into a Settings field on the node, so the end user can
configure the node WITHOUT editing code. This is how a node gets settings —
a script that hardcodes every value produces a node with NO settings, which
is a UX failure for anything a user would want to adjust.

Rules:
  - Every parameter MUST have a sensible default so the node runs with zero
    configuration. Defaults come from the env read: `os.getenv("X", "512")`.
  - Env vars are strings — coerce types explicitly:
      `int(os.getenv("TARGET_WIDTH", "512"))`,
      `float(os.getenv("SCALE", "2.0"))`,
      `os.getenv("MAINTAIN_ASPECT", "true").lower() == "true"`.
  - Name them UPPER_SNAKE_CASE and descriptively (TARGET_WIDTH, not W).
  - Parameterize what's genuinely user-relevant; do NOT expose plumbing
    (input/output dirs are fixed at /data/input and /data/output — never
    make those settings). Typical knobs by task family:
      resize/scale → TARGET_WIDTH, TARGET_HEIGHT, SCALE, MAINTAIN_ASPECT
      encode/save  → OUTPUT_FORMAT, JPEG_QUALITY, PNG_COMPRESS
      detect/filter→ THRESHOLD, MIN_CONFIDENCE, TOP_K
      model choice → MODEL_NAME / MODEL_VARIANT (when several are valid)
  - If the task genuinely has no adjustable behaviour, expose none — don't
    invent knobs. But most real nodes have at least one.

SYSTEM BINARIES — declare every non-trivial binary your script shells
out to. The wizard validates each `subprocess.run([X, ...])` /
`subprocess.call([X, ...])` / `subprocess.Popen([X, ...])` /
`os.system('X ...')` call against your declarations. If X is not in
the small list of always-present POSIX utilities (mkdir, cp, mv, rm,
ls, cat, echo, mkfifo, mktemp, find, grep, sed, awk, head, tail,
sort, uniq, wc, tr, xargs, tar, gzip, gunzip, zip, unzip, true,
false, test, pwd, bash, sh, python, python3, pip, pip3) AND not
declared by one of the two mechanisms below, the synthesis is
REJECTED. Pick exactly ONE mechanism per binary:

  - `system_packages`: the binary is provided by an apt package in
    Debian/Ubuntu (the curated base images all derive from Debian-
    family bases with apt available). Examples:
      `ffmpeg` → ["ffmpeg"]
      `convert` (ImageMagick) → AVOID, copyleft (GPL via Ghostscript).
      `tesseract` → ["tesseract-ocr"]
      `cmake`, `make`, `gcc` → ["cmake", "build-essential"]
      `git` → ["git"] (already in some bases; declaring is safe — apt
                       no-ops if installed)
      `wget`, `curl` → ["wget"] / ["curl"]
      `pkg-config` → ["pkg-config"]

  - `dockerfile_extras`: raw Dockerfile lines for binaries that need
    a source build (no apt package, or the apt package is too old).
    Allowed directives ONLY: `RUN`, `ENV`, `WORKDIR`, `ARG`, `LABEL`.
    Forbidden: `FROM`, `COPY`, `ADD`, `ENTRYPOINT`, `CMD`, `USER`,
    `EXPOSE`, `VOLUME` — the wizard composes those. Lines must each
    start with one of the allowed directives (continuation lines
    with `\\` are fine).

    Use this for workloads like openMVG / openMVS / COLMAP /
    AliceVision / Meshroom — they require cloning + cmake + make
    against system libs. Pattern:
        RUN apt-get update && apt-get install -y --no-install-recommends \\
            git cmake build-essential libceres-dev libsuitesparse-dev \\
            && rm -rf /var/lib/apt/lists/*
        ENV GIT_LFS_SKIP_SMUDGE=1
        RUN git clone --depth 1 --filter=blob:none https://github.com/openMVG/openMVG.git /opt/openMVG \\
            && cd /opt/openMVG && cmake -B build -S src \\
            && cmake --build build --parallel 4 \\
            && cmake --install build
        ENV PATH=/usr/local/bin:$PATH

    `dockerfile_extras` is for the HARD case — if `system_packages`
    alone can install everything, leave `dockerfile_extras` as "".
    Don't double-declare a binary in both fields.

If your script doesn't shell out to anything beyond the always-
present utilities, leave both `system_packages` and
`dockerfile_extras` empty. Most pure-Python workloads (PIL, numpy,
torch, …) need NEITHER.

If the user's task fundamentally requires a binary that has no apt
package AND no straightforward source-build path you can write into
`dockerfile_extras`, return ok=false with a reasoning that explains
which binary blocked you. Better to fail synthesis cleanly than to
emit a script that calls binaries that don't exist in the image.

BASE IMAGE SELECTION — pick decisively, never punt to the user. End users do
NOT know what a base image is and asking them what to pick is a UX failure.
Use this decision rule, in order:

  1. Workload uses CUDA / NVIDIA GPU directly (torch+cuda, tensorflow-gpu,
     cupy, jax[cuda], triton, vllm, flash-attn, bitsandbytes, ...) → pick
     a `cuda-*-python*` variant.
  2. Workload uses a CUDA-accelerated C++/CLI tool (colmap, openMVG,
     openMVS, AliceVision, Meshroom, COLMAP, NeRF/Gaussian splatting
     tooling, instant-ngp, photogrammetry/Structure-from-Motion/MVS
     pipelines, etc.) → pick a `cuda-*-python*` variant. These bases
     include apt access so you can install cmake/gcc/ffmpeg in the
     Dockerfile.
  3. Workload uses ROCm / AMD GPU explicitly → pick a `rocm-*-python*`
     variant.
  4. Workload uses Intel XPU (intel_extension_for_pytorch, etc.) → pick
     an `intel-xpu-*` variant.
  5. Workload is pure-CPU Python → pick `python-3.11` (or `python-3.12`
     for newer stdlib needs).
  6. Workload is a compiled binary that needs to be built from source
     AND uses GPU acceleration → pick a `cuda-*-python*` variant (Python
     + apt available for the build chain).

If you cannot decide between options, default to the CUDA Python variant
for any 3D / computer vision / ML / video-understanding workload, else
the basic Python variant. NEVER return without a base. NEVER surface a
clarifying question about the base image — the JSON schema requires
base_image_hint and it must always be populated.

CREDIT COST PER INPUT-SIZE TIER (sz-tcp.4, tiered credit pricing) —
populate `suggested_credits_small`, `suggested_credits_medium` and
`suggested_credits_large` with NON-NEGATIVE INTEGERS. The platform
charges 1 credit per 30 CPU-seconds OR 6 GPU-seconds of compute, so
estimate the expected p95 runtime of YOUR script at each input size:
  - small input  (~1024x1024 image / 10k-triangle mesh / 1 MB JSON)
  - medium input (~4096x4096 / 500k tris / 50 MB)
  - large input  (~8192x8192 / 5M tris / 500 MB)
The tiers are byte-size boundaries (Small <= 2 MB, Medium <= 30 MB,
Large > 30 MB — `services/cost_estimator.get_tier_thresholds()` is the
authoritative source, don't hardcode). Keep the three non-decreasing
(small <= medium <= large). Omit a key only when you genuinely cannot
estimate that tier — the platform then falls back to its rule floors.

suggested_input_type and suggested_output_type should be the most
specific, conventional label for the data the node consumes or
produces. Examples:
  - common categories: image, video, audio, text, json, csv,
    binary, mesh, texture, pointcloud, pdf, document, other
  - specific formats: ifc, dwg, dxf, step, parquet, dicom, fasta,
    vcf, shp, geojson, las, onnx, safetensors, hdf5, xlsx, glb,
    fbx, etc.
Prefer the specific format when the node only handles one (e.g. an
IFC parser → "ifc", not "binary"). Use a category when the node
accepts a family (e.g. an image upscaler → "image"). Lowercase.
"""


@dataclass
class SynthesisResult:
    ok: bool
    script_content: str = ""
    requirements: List[str] = field(default_factory=list)
    base_image_hint: str = ""
    # System binaries the script depends on. ``system_packages`` are
    # apt package names; ``dockerfile_extras`` is raw Dockerfile
    # lines (RUN/ENV/WORKDIR/ARG/LABEL only) for source-build
    # workloads. Phase B (separate commit) will plumb these into
    # the Containerizer's Dockerfile composer; for now they ride on
    # the spec so the build pipeline sees them and the validator's
    # rejection reasons cite them by name.
    system_packages: List[str] = field(default_factory=list)
    dockerfile_extras: str = ""
    suggested_input_type: str = "other"
    suggested_output_type: str = "other"
    reasoning: str = ""
    # sz-tcp.4 — per-size-tier credit suggestions the LLM estimated for the
    # generated script (Small <= 2 MB, Medium <= 30 MB, Large > 30 MB —
    # thresholds from services/cost_estimator.get_tier_thresholds). None
    # means "the model didn't give a usable number for this tier": the
    # wizard then leaves the tier blank and admission falls back to the
    # rule floors. Negative / garbage input is normalised to None.
    suggested_credits_small: Optional[int] = None
    suggested_credits_medium: Optional[int] = None
    suggested_credits_large: Optional[int] = None
    rejection_reasons: List[str] = field(default_factory=list)
    # When ``ok=False``, hint the caller about the FAMILY of failure so
    # the wizard can route the user appropriately instead of showing a
    # generic "Hit a snag" toast. Possible values:
    #   - ``"license"`` — the chosen repo's recipe shells out to a
    #     copyleft binary OR declares a copyleft apt package; the
    #     wizard re-shows the candidate list so the user can pick
    #     a different repo or build fresh (same description, no repo
    #     attached). Retrying with the SAME repo will produce the
    #     same failure.
    #   - ``"contract"`` — I/O type / file-extension mismatch. The
    #     wizard could prompt the user to confirm or auto-retry.
    #   - ``"binary"`` — script calls an undeclared binary; same-repo
    #     retry COULD work if the LLM picks a different binary.
    #   - ``"other"`` — pip license, schema, base image, etc. Generic
    #     handling.
    rejection_category: Optional[str] = None
    # Number of synthesis attempts the inner retry loop spent on this
    # result. ``1`` means first-try; ``> 1`` means the LLM corrected a
    # validator rejection from a previous attempt. Telemetry uses this
    # to track how often the self-heal loop pays off.
    attempts: int = 1
    # Critic D — advisory, non-blocking notes to surface in the wizard's
    # Review step (e.g. "may not fully match your request: ..."). The script
    # is still accepted; these are heads-ups, not rejections.
    warnings: List[str] = field(default_factory=list)
    # Critic D v2 — when the intent is genuinely ambiguous, the synth returns a
    # single clarifying question instead of proceeding. The wizard shows it in
    # the describe chat; the answer is folded in for a fresh synth (which passes
    # already_clarified=True so we never ask twice). ``ok`` stays True (the
    # script is valid) — the wizard checks needs_clarification first.
    needs_clarification: bool = False
    clarifying_question: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "script_content": self.script_content,
            "requirements": list(self.requirements),
            "base_image_hint": self.base_image_hint,
            "system_packages": list(self.system_packages),
            "dockerfile_extras": self.dockerfile_extras,
            "suggested_input_type": self.suggested_input_type,
            "suggested_output_type": self.suggested_output_type,
            "reasoning": self.reasoning,
            "suggested_credits_small": self.suggested_credits_small,
            "suggested_credits_medium": self.suggested_credits_medium,
            "suggested_credits_large": self.suggested_credits_large,
            "rejection_reasons": list(self.rejection_reasons),
            "rejection_category": self.rejection_category,
            "attempts": self.attempts,
            "warnings": list(self.warnings),
            "needs_clarification": self.needs_clarification,
            "clarifying_question": self.clarifying_question,
        }


# Requirements may not contain these characters — we don't trust the
# LLM not to slip a shell command in. These are the genuine shell-
# injection vectors: whitespace (token smuggling), pipe, ampersand,
# semicolon, backtick, and dollar (command/var expansion).
#
# NOTE: `<` and `>` are intentionally NOT here — they are PEP 508
# version-specifier operators (`Pillow>=10.0.0`, `numpy<2.0`), not
# shell redirection in this context. Including them rejected EVERY
# pinned requirement, which froze the wizard's Describe→build flow.
_REQ_BAD_CHARS = re.compile(r"[\s|&;`$]")


def _allowed_base_dirs() -> List[str]:
    return [b.variant_dir for b in CURATED_BASE_IMAGES]


# Stdlib modules safe to auto-import when the script uses `mod.<attr>` but
# forgot to import them (only ones a bare `import mod` fixes). Conservative —
# stdlib only, never third-party.
_AUTO_IMPORT_STDLIB = frozenset({
    "sys", "os", "json", "re", "math", "time", "glob", "shutil", "subprocess",
    "base64", "io", "random", "csv", "hashlib", "traceback", "logging",
    "tempfile", "struct", "uuid", "socket", "pickle", "textwrap", "copy",
    "datetime", "collections", "itertools", "functools",
})

# Bare stdlib NAMES the model commonly uses without their `from …` import — most
# often `Path` (repro 2026-09-03: a valid grayscale build used Path(...) but only
# imported PIL + os, an uncaught NameError). Injected as the full from-statement.
_AUTO_IMPORT_FROM = {
    "Path": "from pathlib import Path",
    "defaultdict": "from collections import defaultdict",
    "Counter": "from collections import Counter",
    "OrderedDict": "from collections import OrderedDict",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "reduce": "from functools import reduce",
    "partial": "from functools import partial",
}


def _repair_missing_stdlib_imports(script: str) -> str:
    """Inject ``import <mod>`` for stdlib modules used as ``mod.<attr>`` but not
    imported/bound. Fixes the common LLM slip of ``sys.stderr`` with no
    ``import sys`` — an uncaught NameError that crashes the node on its first
    error path. Inserts after the existing import block so leading marker
    comments (e.g. ``# pip-requirements:``) are untouched. No-op if the script
    doesn't parse or nothing's missing."""
    if not isinstance(script, str) or not script.strip():
        return script
    import ast as _ast
    try:
        tree = _ast.parse(script)
    except SyntaxError:
        return script  # syntax is validated elsewhere; don't mangle it

    bound: set = set()
    used: set = set()          # module names used as `mod.attr`
    used_names: set = set()    # bare names used in Load context
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
            if node.module:
                bound.add(node.module.split(".")[0])
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Store):
            bound.add(node.id)
        elif isinstance(node, _ast.arg):
            bound.add(node.arg)
        elif (isinstance(node, _ast.Attribute)
              and isinstance(node.value, _ast.Name)
              and isinstance(node.value.ctx, _ast.Load)):
            used.add(node.value.id)
        elif isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
            used_names.add(node.id)

    missing = sorted(m for m in used if m in _AUTO_IMPORT_STDLIB and m not in bound)
    missing_from = sorted(
        n for n in used_names if n in _AUTO_IMPORT_FROM and n not in bound
    )
    if not missing and not missing_from:
        return script

    lines = script.splitlines(keepends=True)
    import_end = 0
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            import_end = max(import_end, node.end_lineno or node.lineno)
    injected = ("".join(f"import {m}\n" for m in missing)
                + "".join(_AUTO_IMPORT_FROM[n] + "\n" for n in missing_from))
    result = injected + script if import_end == 0 else \
        "".join(lines[:import_end] + [injected] + lines[import_end:])
    try:
        logger.info("codegen repair: injected missing stdlib import(s): %s",
                    missing + [_AUTO_IMPORT_FROM[n] for n in missing_from])
    except Exception:
        pass
    return result


# A write under /data/output that will SILENTLY produce nothing if the directory
# is missing: cv2.imwrite() and PIL/np/pandas .save()/.to_*()/dump() return
# without raising when the parent dir does not exist, so the node prints "saved…"
# and writes zero files. Coarse on purpose — a false positive only means we
# require an (always-safe, idempotent) mkdir; a miss just skips enforcement.
_OUTPUT_WRITE_RE = re.compile(
    r"imwrite|\.save\s*\(|\.savefig\s*\(|\.write\s*\(|\.write_text\s*\(|"
    r"\.write_bytes\s*\(|\.to_csv\s*\(|\.to_json\s*\(|\.to_parquet\s*\(|"
    r"\.dump\s*\(|\bopen\s*\("
)


def _writes_under_output(script: str) -> bool:
    return "/data/output" in (script or "") and bool(_OUTPUT_WRITE_RE.search(script or ""))


def _has_mkdir(script: str) -> bool:
    return "mkdir" in (script or "") or "makedirs" in (script or "")


def _repair_pil_submodule_imports(script: str) -> str:
    """Fix ``import PIL`` followed by ``PIL.Image.open(...)`` — a plain
    ``import PIL`` does NOT import its submodules, so ``PIL.Image`` raises
    ``AttributeError: module 'PIL' has no attribute 'Image'`` on first use. The
    weak coder emits this constantly (breadth test 2026-09-04: 4/9 failures were
    this exact bug). Deterministic + always-safe: for every ``PIL.<Sub>`` used
    with only a bare ``import PIL`` in scope, inject ``import PIL.<Sub>``. No-op
    if the submodule is already imported (``import PIL.Image`` /
    ``from PIL import Image``) or the script doesn't parse."""
    if not isinstance(script, str) or "PIL" not in script:
        return script
    import ast as _ast
    try:
        tree = _ast.parse(script)
    except SyntaxError:
        return script
    has_bare_pil = False
    imported_subs: set = set()      # PIL submodules already imported
    used_subs: set = set()          # PIL.<Sub> referenced as an attribute
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                if a.name == "PIL":
                    has_bare_pil = True
                elif a.name.startswith("PIL."):
                    imported_subs.add(a.name.split(".", 2)[1])
        elif isinstance(node, _ast.ImportFrom) and (node.module or "") == "PIL":
            for a in node.names:
                imported_subs.add(a.name)          # from PIL import Image
        elif (isinstance(node, _ast.Attribute)
              and isinstance(node.value, _ast.Name) and node.value.id == "PIL"):
            used_subs.add(node.attr)               # PIL.Image, PIL.ImageOps, …
    missing = sorted(s for s in used_subs if s not in imported_subs)
    if not has_bare_pil or not missing:
        return script
    lines = script.splitlines(keepends=True)
    import_end = 0
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            import_end = max(import_end, node.end_lineno or node.lineno)
    injected = "".join(f"import PIL.{s}\n" for s in missing)
    result = injected + script if import_end == 0 else \
        "".join(lines[:import_end] + [injected] + lines[import_end:])
    try:
        logger.info("codegen repair: injected PIL submodule import(s): %s", missing)
    except Exception:
        pass
    return result


def _repair_removed_pil_constants(script: str) -> str:
    """Rewrite ``Image.ANTIALIAS`` -> ``Image.LANCZOS``. ANTIALIAS was REMOVED in
    Pillow 10 (``AttributeError: module 'PIL.Image' has no attribute
    'ANTIALIAS'``); LANCZOS is its documented replacement and exists in every
    supported Pillow. The weak coder was trained on pre-10 Pillow and reaches for
    ANTIALIAS constantly (breadth test 2026-09-04: resize + border). Deterministic
    and always-safe — ANTIALIAS only ever named this resampling filter."""
    if not isinstance(script, str) or "ANTIALIAS" not in script:
        return script
    new = re.sub(r"\.ANTIALIAS\b", ".LANCZOS", script)
    if new != script:
        try:
            logger.info("codegen repair: Image.ANTIALIAS -> Image.LANCZOS "
                        "(ANTIALIAS removed in Pillow 10)")
        except Exception:
            pass
    return new


def _repair_missing_output_mkdir(script: str) -> str:
    """Inject ``OUT.mkdir(parents=True, exist_ok=True)`` when the script writes
    under /data/output through a Path variable but never creates the directory.
    cv2.imwrite()/PIL .save() SILENTLY no-op (return False, no exception) when the
    target dir is missing, so the node reports success while producing nothing
    (repro 2026-09-03: a coder-stall ESCALATION shipped a cv2 grayscale script
    with no mkdir — the escalation path never ran the directions critic). Runs on
    every _synthesize_once, so it covers the escalation path too. Deterministic
    and always-safe (mkdir is idempotent). No-op if it can't find a clean
    ``VAR = Path('/data/output'…)`` assignment — the fatal check in
    _validate_script then forces a regen rather than shipping a silent no-op."""
    if not isinstance(script, str) or not script.strip():
        return script
    if _has_mkdir(script) or not _writes_under_output(script):
        return script
    import ast as _ast
    try:
        tree = _ast.parse(script)
    except SyntaxError:
        return script  # syntax validated elsewhere; don't mangle it
    target = None  # (var, insert_after_lineno, indent)
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        v = node.value
        if (isinstance(t, _ast.Name)
                and isinstance(v, _ast.Call)
                and (getattr(v.func, "id", None) == "Path"
                     or getattr(v.func, "attr", None) == "Path")
                and v.args and isinstance(v.args[0], _ast.Constant)
                and isinstance(v.args[0].value, str)
                and "/data/output" in v.args[0].value):
            target = (t.id, node.end_lineno or node.lineno, node.col_offset)
            break
    if target is None:
        return script
    var, after_line, indent = target
    lines = script.splitlines(keepends=True)
    if after_line > len(lines):
        return script
    inject = f"{' ' * indent}{var}.mkdir(parents=True, exist_ok=True)\n"
    result = "".join(lines[:after_line] + [inject] + lines[after_line:])
    try:
        logger.info("codegen repair: injected %s.mkdir(parents=True, exist_ok=True) "
                    "(writes to /data/output without creating it)", var)
    except Exception:
        pass
    return result


def _validate_script(content: str) -> List[str]:
    reasons: List[str] = []
    if not content or len(content.strip()) < 20:
        reasons.append("script_content is empty or too short to be a real script")
    _io_fix = (
        " FIX: read inputs from /data/input and write outputs to /data/output, "
        "e.g.\nfrom pathlib import Path\nIN = Path('/data/input'); "
        "OUT = Path('/data/output'); OUT.mkdir(parents=True, exist_ok=True)\n"
        "for src in sorted(IN.rglob('*')):\n    if src.is_file():\n"
        "        ...  # do the work, write under OUT"
    )
    if "/data/input" not in content:
        reasons.append(
            "script does not reference /data/input — runtime input contract "
            "violated." + _io_fix
        )
    if "/data/output" not in content:
        reasons.append(
            "script does not reference /data/output — runtime output contract "
            "violated." + _io_fix
        )
    # AST parse — catches mojibake/smart-quote breakage and other
    # syntactic damage from the LLM's response before we hand the
    # script to the build pipeline (manual-test repro 2026-05-23
    # spec=148 Test F: LLM emitted `'w')` as `,&�w')` → 17 inner
    # repair attempts thrashing on SyntaxError).
    try:
        import ast as _ast
        _ast.parse(content)
    except SyntaxError as exc:
        reasons.append(
            f"script_content fails Python syntax check: {exc.msg!r} at "
            f"line {exc.lineno}, offset {exc.offset}"
        )
    # Deterministic, near-zero-false-positive "this WILL crash on first run"
    # checks (import pip, shell operators inside a subprocess list, undefined
    # names). FATAL here so a certain-crash script is a validation failure — it
    # never becomes a shippable result and is regenerated or fails cleanly,
    # never silently handed to the user (repro 2026-09-03: the hallucinated
    # Meshroom wrapper). These once relied on the advisory exec-critic, which
    # could ship the flagged script after the coder exhausted its retries.
    try:
        from node_builder.critic_synth import fatal_runtime_crashes
        reasons.extend(fatal_runtime_crashes(content))
    except Exception:  # noqa: BLE001 — never block validation on the checker
        pass
    # FATAL: writes under /data/output but never creates it. cv2.imwrite() and
    # PIL/np/pandas writers silently no-op (no exception) when the dir is missing,
    # so the node prints "saved…" and produces ZERO files — a false success.
    # _repair_missing_output_mkdir fixes the common `OUT = Path(...)` case before
    # this runs; if it couldn't (e.g. an inline path with no variable) this is
    # fatal so a regen is forced rather than shipping a silent no-op. Enforced on
    # EVERY path incl. the reasoner escalation (repro 2026-09-03).
    if _writes_under_output(content) and not _has_mkdir(content):
        reasons.append(
            "script writes under /data/output but never creates it — add "
            "OUT.mkdir(parents=True, exist_ok=True) after defining the output "
            "path. cv2.imwrite() and PIL .save() write NOTHING (no error) when "
            "the directory is missing, so the node reports success but produces "
            "no output."
        )
    return reasons


def _validate_requirements(reqs: Any) -> List[str]:
    reasons: List[str] = []
    if not isinstance(reqs, list):
        reasons.append("requirements must be a list")
        return reasons
    for item in reqs:
        if not isinstance(item, str):
            reasons.append(f"requirement is not a string: {item!r}")
            continue
        if not item.strip():
            reasons.append("blank requirement entry")
            continue
        if _REQ_BAD_CHARS.search(item):
            reasons.append(
                f"requirement {item!r} contains shell metacharacters; "
                "this looks like an attempt to smuggle a command — "
                "rejected."
            )
    # Hard backstop for the LICENSE POLICY in the system prompt: refuse
    # known GPL/AGPL PyPI packages (PyMuPDF, pdf2image→poppler, Wand→
    # Ghostscript, …) so copyleft never lands in a permissive-only image.
    reasons.extend(pip_license_warnings(reqs))
    return reasons


def _validate_base_hint(hint: Any) -> List[str]:
    if not isinstance(hint, str) or not hint.strip():
        return ["base_image_hint missing"]
    if hint not in _allowed_base_dirs():
        return [
            f"base_image_hint {hint!r} is not on the curated allowlist. "
            f"The LLM may have hallucinated a tag — caller must pick "
            f"one of: {', '.join(_allowed_base_dirs())}."
        ]
    return []


# ----------------------------------------------------------------------
# System binaries — system_packages + dockerfile_extras + script scan
#
# The photogrammetry-build repro (spec=11, 2026-05-30): the synth LLM
# wrote a script that did `subprocess.run(['openMVG_main_*', ...])`
# without declaring openMVG anywhere. The hero Dockerfile was just
# `FROM base-cuda + pip install ffmpeg-python` — no openMVG install.
# The container exited 1 with "FileNotFoundError: openMVG_main_..."
# and the build pipeline burned 5h 17m in repair attempts before
# giving up with gpu_variants={}. These validators catch that at
# synth time so the LLM gets a clear "missing X — declare via
# system_packages or dockerfile_extras" rejection and retries.
# ----------------------------------------------------------------------

# POSIX/coreutils + Python/pip — always present in any curated base
# image. Scripts can call these freely without declaring them.
# Conservative: only utilities we KNOW are universally present in
# Debian-family bases.
_ALWAYS_PRESENT_BINARIES = frozenset({
    # POSIX coreutils
    "ls", "cat", "cp", "mv", "rm", "mkdir", "rmdir", "ln",
    "echo", "printf", "true", "false", "test", "[",
    "pwd", "cd", "find", "grep", "egrep", "fgrep",
    "sed", "awk", "head", "tail", "sort", "uniq", "wc",
    "tr", "cut", "xargs", "tee", "yes", "seq", "basename",
    "dirname", "readlink", "realpath", "stat", "touch", "chmod", "chown",
    # Archive + transfer (in Debian base)
    "tar", "gzip", "gunzip", "zip", "unzip", "bzip2", "bunzip2",
    "xz", "unxz",
    # Shells + Python interpreters
    "bash", "sh", "dash",
    "python", "python3", "pip", "pip3",
    # Debian package management — present on every Debian-based image,
    # including the python:* and node:* family. The validator was
    # rejecting scripts that called these as a false positive.
    "apt-get", "apt", "apt-cache", "dpkg", "dpkg-query",
    "update-alternatives",
    # curl + wget — installed on most slim base images we use as
    # the runtime layer; also installed by the wizard's standard
    # base-image prep. False positives when a script uses them.
    "curl", "wget",
    # The runtime contract guarantees these:
    "mkfifo", "mktemp",
})

# Allowed Dockerfile directives in `dockerfile_extras`. Anything else
# (FROM/COPY/ADD/ENTRYPOINT/CMD/USER/EXPOSE/VOLUME/HEALTHCHECK/
# STOPSIGNAL/SHELL) is the wizard's job; the LLM emitting them would
# clobber the composed Dockerfile in unexpected ways.
_DOCKERFILE_EXTRAS_ALLOWED = frozenset({"RUN", "ENV", "WORKDIR", "ARG", "LABEL"})

# Apt package name shape: `[a-z0-9][a-z0-9+\-.]*`. We're permissive
# inside — apt accepts a wide range — but reject shell metacharacters
# that would let the LLM smuggle a command into the apt-get install
# line the composer will eventually emit.
_APT_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+\-.]*$")


def _validate_system_packages(value: Any) -> List[str]:
    reasons: List[str] = []
    if value is None:
        return reasons
    if not isinstance(value, list):
        reasons.append(
            f"system_packages must be a list (got {type(value).__name__})"
        )
        return reasons
    seen: set = set()
    for item in value:
        if not isinstance(item, str):
            reasons.append(f"system_packages entry is not a string: {item!r}")
            continue
        norm = item.strip().lower()
        if not norm:
            reasons.append("blank system_packages entry")
            continue
        if not _APT_PKG_RE.match(norm):
            reasons.append(
                f"system_packages entry {item!r} is not a valid apt "
                f"package name — only lowercase alphanumerics, '+', "
                f"'-', '.' allowed after the leading char."
            )
            continue
        if norm in seen:
            # Duplicate is harmless to the build but a sign of LLM
            # confusion — surface it without rejecting.
            continue
        seen.add(norm)
    return reasons


def _validate_dockerfile_extras(value: Any) -> List[str]:
    """Reject dockerfile_extras that uses disallowed directives or
    would let the LLM hijack composition (e.g. a stray FROM)."""
    reasons: List[str] = []
    if value is None or value == "":
        return reasons
    if not isinstance(value, str):
        reasons.append(
            f"dockerfile_extras must be a string (got {type(value).__name__})"
        )
        return reasons
    if len(value) > 8192:
        reasons.append(
            f"dockerfile_extras is too long ({len(value)} bytes; max 8192) — "
            f"split into multiple RUN steps or pre-build the dependency "
            f"into a custom base image instead."
        )
        return reasons
    # Strip comments and blank lines, fold continuation lines, then
    # require every logical line to start with an allowed directive.
    # The continuation-fold is line-level: a line ending in `\` carries
    # to the next physical line, no directive recheck needed.
    physical_lines = value.splitlines()
    logical_lines: List[str] = []
    buf = ""
    for raw in physical_lines:
        stripped = raw.rstrip()
        if stripped.lstrip().startswith("#") or not stripped.lstrip():
            # Comment / blank — only emit current buffer if any.
            if buf:
                logical_lines.append(buf)
                buf = ""
            continue
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
        else:
            buf += stripped
            logical_lines.append(buf)
            buf = ""
    if buf:
        logical_lines.append(buf)
    for line in logical_lines:
        # First whitespace-delimited token = directive.
        token = line.lstrip().split(None, 1)[0] if line.strip() else ""
        directive = token.upper()
        if directive not in _DOCKERFILE_EXTRAS_ALLOWED:
            reasons.append(
                f"dockerfile_extras contains disallowed directive {token!r}. "
                f"Only {', '.join(sorted(_DOCKERFILE_EXTRAS_ALLOWED))} are "
                f"permitted; the wizard composes FROM/COPY/ENTRYPOINT/USER. "
                f"Offending line: {line[:120]!r}"
            )
    return reasons


def _extract_subprocess_binaries(script: str) -> List[str]:
    """Walk the script's AST and return every binary it shells out to.

    Catches these patterns:
      - ``subprocess.run([X, ...])`` / ``subprocess.run((X, ...))``
      - ``subprocess.call([X, ...])`` / ``subprocess.check_call``
      - ``subprocess.check_output([X, ...])``
      - ``subprocess.Popen([X, ...])``
      - ``os.system('X ...')``  — first whitespace-delimited token
      - ``os.execv('/path/to/X', ...)`` / ``os.execvp('X', ...)``

    Only literal first arguments are extracted — dynamic invocations
    (e.g. ``subprocess.run([binary, ...])`` where ``binary`` is a
    variable) are NOT analysed here. That's an intentional limit:
    we'd rather miss a dynamic case than false-reject one. The
    repro case (openMVG) uses literal binary names, and that's the
    common shape.

    Returns the bare command name (the last path component if the
    LLM wrote an absolute path), lowercased.
    """
    if not isinstance(script, str) or not script.strip():
        return []
    import ast as _ast
    try:
        tree = _ast.parse(script)
    except SyntaxError:
        # Script syntax is already caught by `_validate_script`; if
        # we can't parse, no binaries to extract.
        return []

    found: List[str] = []

    # Resolve a first-arg VARIABLE bound to a list literal (cmd = ['gs', …];
    # subprocess.run(cmd)) to its first element. Without this, a copyleft or
    # non-existent binary shelled via a variable bypasses BOTH the licence and
    # existence checks (repro 2026-09-03: command = ['imagemagick', …];
    # subprocess.run(command) was detected as nothing). Only simple list-literal
    # assignments are resolved, so no dynamic case is ever false-rejected.
    _var_first: Dict[str, str] = {}
    for _n in _ast.walk(tree):
        if (isinstance(_n, _ast.Assign)
                and isinstance(_n.value, (_ast.List, _ast.Tuple)) and _n.value.elts):
            _el = _n.value.elts[0]
            if isinstance(_el, _ast.Constant) and isinstance(_el.value, str):
                for _t in _n.targets:
                    if isinstance(_t, _ast.Name):
                        _var_first[_t.id] = _el.value

    def _first_arg_to_name(node: _ast.expr) -> Optional[str]:
        # subprocess.run/call/Popen take a list-or-tuple of strings as
        # the FIRST positional arg, OR a single string (shell=True case).
        # We only extract LITERALS — anything else is a dynamic call.
        if isinstance(node, (_ast.List, _ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, _ast.Constant) and isinstance(first.value, str):
                return first.value
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            # shell=True or os.system: the binary is the first
            # whitespace-delimited token of the string.
            tok = node.value.strip().split(None, 1)
            return tok[0] if tok else None
        return None

    def _classify_call(call: _ast.Call) -> Optional[Tuple[str, _ast.expr]]:
        """Return (kind, first_arg_node) for shell-out calls we care
        about, else None.

        ``kind`` is one of ``subprocess`` / ``os.system`` / ``os.exec``.
        Each kind has slightly different first-arg semantics.
        """
        func = call.func
        # subprocess.run / subprocess.call / subprocess.Popen / etc.
        if (
            isinstance(func, _ast.Attribute)
            and isinstance(func.value, _ast.Name)
            and func.value.id == "subprocess"
            and func.attr in {"run", "call", "check_call", "check_output", "Popen"}
        ):
            if call.args:
                return "subprocess", call.args[0]
        # os.system('...')
        if (
            isinstance(func, _ast.Attribute)
            and isinstance(func.value, _ast.Name)
            and func.value.id == "os"
            and func.attr == "system"
        ):
            if call.args:
                return "os.system", call.args[0]
        # os.execv / os.execvp / os.execvpe / os.execlp / os.execl
        if (
            isinstance(func, _ast.Attribute)
            and isinstance(func.value, _ast.Name)
            and func.value.id == "os"
            and func.attr in {"execv", "execvp", "execvpe", "execl", "execlp", "execle", "execlpe"}
        ):
            if call.args:
                return "os.exec", call.args[0]
        return None

    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        classified = _classify_call(node)
        if not classified:
            continue
        kind, first_arg = classified
        name = _first_arg_to_name(first_arg)
        if not name and isinstance(first_arg, _ast.Name):
            name = _var_first.get(first_arg.id)   # cmd = ['gs', …]; run(cmd)
        if not name:
            continue
        # Reduce paths to the basename. `/usr/local/bin/openMVG_main_X`
        # → `openMVG_main_X`. The validator gate is on the binary,
        # not on its absolute path.
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        if name.startswith("-"):
            # Probably a flag mis-parsed as a binary; skip.
            continue
        found.append(name)

    # De-duplicate while preserving order — useful for the rejection
    # message so the LLM sees the first offending binary first.
    seen: set = set()
    dedup: List[str] = []
    for n in found:
        if n in seen:
            continue
        seen.add(n)
        dedup.append(n)
    return dedup


def _binaries_supplied_by_dockerfile_extras(extras: str) -> set:
    """Best-effort extract binary names made available by
    `dockerfile_extras`. We can't truly evaluate arbitrary Dockerfile
    RUN steps, so we scan for these well-known install patterns:

      - ``apt-get install -y ... pkg ...`` — assume pkg provides a
        binary of the same name (approximate but common).
      - ``cmake --install build`` / ``make install`` / ``ninja install``
        — assume the previous ``git clone`` URL hints at the binary.
        We DON'T try to enumerate those; instead we treat the
        presence of ANY ``install`` step as evidence the LLM is
        building from source. To avoid false-rejects on legitimate
        source builds, when this signal is present we accept any
        binary that matches a substring of a cloned repo URL.

    Returns lowercase basenames the dockerfile_extras likely
    provides. The caller uses this as a fuzzy "this binary is
    probably available" hint, not a hard guarantee.
    """
    if not isinstance(extras, str) or not extras.strip():
        return set()
    provided: set = set()
    # apt-get install -y --no-install-recommends pkg1 pkg2 ...
    for m in re.finditer(
        r"apt-get\s+install\s+(?:-[a-z\-]+\s+)*([^\\\n&|;]+)",
        extras, re.IGNORECASE,
    ):
        for pkg in m.group(1).split():
            pkg = pkg.strip()
            if pkg.startswith("-"):
                continue
            if _APT_PKG_RE.match(pkg.lower()):
                provided.add(pkg.lower())
                # Several common packages name themselves after a
                # binary that differs slightly — tesseract-ocr → tesseract,
                # libgl1-mesa-glx → libGL.so (no binary). For the
                # binary case, also expose the base name.
                if "-" in pkg.lower():
                    provided.add(pkg.lower().split("-", 1)[0])
    # Repos cloned via `git clone <url>` likely provide a binary
    # named after the repo. e.g. `https://github.com/openMVG/openMVG`
    # → openMVG_main_* family. We can't enumerate those, so when an
    # install step is present, accept any binary whose name shares
    # at least 6 chars with the cloned repo basename.
    repo_hints: List[str] = []
    for m in re.finditer(
        r"git\s+clone\s+(?:--depth\s+\S+\s+)?https?://[^\s\\]+/([A-Za-z0-9_\-.]+?)(?:\.git)?\b",
        extras,
    ):
        repo_hints.append(m.group(1).lower())
    if (
        re.search(r"\b(make|cmake|ninja|meson|cargo)\s+install\b", extras, re.IGNORECASE)
        or re.search(r"\bcmake\s+--install\b", extras, re.IGNORECASE)
    ):
        # Mark a sentinel that we'll dereference via repo_hints in the
        # caller's matcher.
        for hint in repo_hints:
            provided.add(f"__sourcebuilt__:{hint}")
    return provided


def _binary_satisfied(
    binary: str,
    system_packages: set,
    extras_provided: set,
) -> bool:
    """True when the script's binary call is satisfied by one of:
    always-present POSIX/python utils, declared apt packages, or a
    dockerfile_extras source build whose repo name shares >=6 chars
    with the binary basename.
    """
    norm = binary.strip().lower()
    if not norm:
        return True  # nothing to validate
    if norm in _ALWAYS_PRESENT_BINARIES:
        return True
    # Common interpreter variants: python3.11, pip3.11, etc.
    if re.match(r"^(python|pip)[0-9.]*$", norm):
        return True
    # Tarballed/Debian-conventional name variants:
    # ffmpeg-python pip package is irrelevant — we care about the
    # `ffmpeg` binary. Direct match is the dominant case.
    if norm in system_packages:
        return True
    # Common debian convention: package name suffixes -bin / -tools.
    for suffix in ("-bin", "-tools", "-common", "-utils"):
        if (norm + suffix) in system_packages:
            return True
    # Source-build hints: repo hint with >=6-char overlap.
    for marker in extras_provided:
        if not marker.startswith("__sourcebuilt__:"):
            if norm == marker:
                return True
            continue
        repo_hint = marker.split(":", 1)[1]
        # Substring match either way, requiring 6+ chars of overlap.
        if len(repo_hint) >= 6 and (repo_hint in norm or norm[:6] == repo_hint[:6]):
            return True
    return False


def _validate_binaries_declared(
    script: str,
    system_packages: Any,
    dockerfile_extras: Any,
) -> List[str]:
    """Every binary the script subprocess-calls must be declared.

    Rejection text names the offending binary AND points at the two
    declaration mechanisms so the LLM's retry has a clear path.
    """
    binaries = _extract_subprocess_binaries(script)
    if not binaries:
        return []
    sys_set: set = set()
    if isinstance(system_packages, list):
        sys_set = {
            str(p).strip().lower() for p in system_packages
            if isinstance(p, str) and p.strip()
        }
    extras_str = dockerfile_extras if isinstance(dockerfile_extras, str) else ""
    extras_provided = _binaries_supplied_by_dockerfile_extras(extras_str)
    reasons: List[str] = []
    for b in binaries:
        if _binary_satisfied(b, sys_set, extras_provided):
            continue
        reasons.append(
            f"script calls binary {b!r} via subprocess/os.system but "
            f"it is not in the always-present POSIX set and not "
            f"declared via `system_packages` (apt) or "
            f"`dockerfile_extras` (source build). The runtime image "
            f"won't have this binary, so the container will exit 1 "
            f"with FileNotFoundError. Either: (a) add the apt "
            f"package that ships {b!r} to system_packages, OR (b) "
            f"emit a RUN block in dockerfile_extras that installs "
            f"{b!r}, OR (c) rewrite the script to not need {b!r}."
        )
    return reasons


# Match http(s) URLs inside dockerfile_extras lines. Stop at quote /
# whitespace / shell metacharacters / `>` `<` `|` so we don't capture
# trailing redirects. Permissive on what URL chars look like — the
# validator HEADs the resulting URL, which is the real test of
# whether it's valid.
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+")


def _extract_urls_from_dockerfile_extras(extras: str) -> List[str]:
    """Pull out every http(s) URL the dockerfile_extras references.

    We're after URLs used by `curl`, `wget`, `git clone`, `pip
    install --index-url`, etc. False positives (URLs inside comments
    or string literals that aren't actually fetched) are harmless —
    a HEAD against a real URL just succeeds, and the validator
    soft-warns on network errors rather than failing the synth.
    """
    if not isinstance(extras, str) or not extras.strip():
        return []
    seen: set = set()
    urls: List[str] = []
    for m in _URL_RE.finditer(extras):
        u = m.group(0).rstrip(".,);'\"")
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def _validate_clone_bounds(dockerfile_extras: str) -> List[str]:
    """GSEC R-100 — reject dockerfile_extras that un-bounds the build-time
    clone.

    `git fetch --unshallow` / `--no-depth` exist precisely to defeat the
    shallow, partial clone the wizard build container enforces — they would
    re-download full history and every blob of the upstream repo, so they are
    rejected with an actionable reason (the plain unbounded `git clone` is
    rewritten safely by the normalizer and is NOT rejected here).
    """
    from node_builder.clone_bounds import has_unbounded_construct

    if not isinstance(dockerfile_extras, str) or not dockerfile_extras:
        return []
    reasons: List[str] = []
    for line in dockerfile_extras.splitlines():
        if has_unbounded_construct(line):
            reasons.append(
                f"dockerfile_extras uses a history-unshallowing git flag on "
                f"{line.strip()[:100]!r} — the wizard build container only "
                f"allows shallow partial clones (`git clone --depth 1 "
                f"--filter=blob:none`). Remove the `git fetch --unshallow` / "
                f"`--no-depth` step; the build does not need full history."
            )
    return reasons


def _validate_dockerfile_extras_urls(
    extras: Any,
    *,
    head_fn: Optional[Callable[..., Any]] = None,
) -> List[str]:
    """HEAD every URL the dockerfile_extras references and reject
    synth if any returns 4xx/5xx.

    Real failure mode 2026-05-31 spec=12: LLM emitted
    ``curl -L https://codeload.github.com/openMVG/openMVG/tar.gz/refs/tags/v1.3.0``
    but openMVG's actual tag is ``v1.3`` (no ``.0``). The build
    burned 9.5 minutes and 4 repair attempts thrashing on
    cosmetically-different URL rewrites that all 404'd the same way.
    HEAD-at-synth catches this in ~1 second.

    Soft-fail on transient errors (timeout, DNS, connection refused):
    we don't want to block the synth on a wobbly upstream when the
    URL is probably fine. The build will still attempt the download
    and the repair loop can handle a genuine network blip. Hard-fail
    only on a definitive 4xx/5xx where the server told us the
    resource isn't there.
    """
    if not isinstance(extras, str) or not extras.strip():
        return []
    urls = _extract_urls_from_dockerfile_extras(extras)
    if not urls:
        return []

    if head_fn is None:
        try:
            import requests as _requests  # imported lazily so the
            # module loads cleanly in test contexts that lack
            # requests installed (unlikely here, but defensive).
            head_fn = _requests.head
        except Exception:
            return []

    reasons: List[str] = []
    for url in urls[:10]:  # cap so a malicious payload can't burn time
        try:
            resp = head_fn(url, allow_redirects=True, timeout=5)
            status = int(getattr(resp, "status_code", 0))
        except Exception as e:
            # Soft-fail: log via the reasoning channel? No — keep
            # the validator pure. Network blip is the build's
            # problem; don't bounce the synth on it.
            logger.info("dockerfile_extras URL HEAD failed (soft pass): %s — %s",
                        url, e)
            continue
        if 400 <= status < 600:
            reasons.append(
                f"`dockerfile_extras` references {url!r} which returned "
                f"HTTP {status}. Likely a hallucinated version tag or "
                f"path. Verify against the upstream source — for "
                f"GitHub-hosted tarballs, list the actual tag names "
                f"with `git ls-remote --tags <repo>` (openMVG uses "
                f"e.g. 'v1.3', 'v2.0', 'v2.1' — no `.patch` suffix) "
                f"before pinning. Or use `git clone --depth 1 "
                f"--filter=blob:none` against the default branch, which "
                f"always resolves (add `ENV GIT_LFS_SKIP_SMUDGE=1` and "
                f"never `git fetch --unshallow` — the build container "
                f"only allows shallow clones)."
            )
    return reasons


def _validate_license_policy(
    script: str,
    system_packages: Any,
    dockerfile_extras: Any,
) -> List[str]:
    """Reject synth output that ships copyleft (GPL/AGPL/LGPL) code in
    a tenant-facing image — the wizard banner promises permissive-only
    licences.

    Three signals checked:

      1. Script calls a known copyleft binary via subprocess (`convert`,
         `gs`, `pdftoppm`, `mutool`, etc.). Even if the LLM later
         declares the corresponding apt package, the runtime invocation
         is the policy violation.

      2. `system_packages` declares a known copyleft apt package
         (`imagemagick`, `ghostscript`, `poppler-utils`, `mupdf-tools`,
         etc.). Catches the case where the LLM declared the package
         to satisfy the binary-presence gate but the package itself
         violates the policy.

      3. `dockerfile_extras` shells out to a known copyleft package via
         `apt-get install`. Same enforcement as (2), applied to the
         raw Dockerfile lines.

    Rejection text NAMES the SPDX licence AND points at the permissive
    alternative — gives the LLM a clear retry path. The pip-side
    version of this guard (`pip_license_warnings`) already lives in
    `utils.license_allowlist`; this is the binary/apt complement.
    """
    reasons: List[str] = []

    # (1) script-level subprocess calls
    binaries = _extract_subprocess_binaries(script) if script else []
    for b in binaries:
        norm = b.strip().lower()
        if norm in _COPYLEFT_BINARIES:
            spdx, alternative = _COPYLEFT_BINARIES[norm]
            reasons.append(
                f"script shells out to {b!r} which ships {spdx} code. "
                f"The wizard's licence policy is permissive-only "
                f"(MIT/Apache/BSD/ISC/MPL). Rewrite the script to use "
                f"{alternative} instead — adding the apt package to "
                f"`system_packages` will not fix this, the runtime "
                f"image must not contain copyleft binaries."
            )

    # (2) system_packages-declared apt packages
    if isinstance(system_packages, list):
        for raw in system_packages:
            if not isinstance(raw, str):
                continue
            norm = raw.strip().lower()
            if norm in _COPYLEFT_APT_PACKAGES:
                spdx, alternative = _COPYLEFT_APT_PACKAGES[norm]
                reasons.append(
                    f"`system_packages` declares {raw!r} which is "
                    f"{spdx}-licensed. The wizard's licence policy is "
                    f"permissive-only (MIT/Apache/BSD/ISC/MPL). Use "
                    f"{alternative} instead — pip has a permissive "
                    f"binding for this workload."
                )

    # (3) dockerfile_extras apt-get install lines
    if isinstance(dockerfile_extras, str) and dockerfile_extras.strip():
        for m in re.finditer(
            r"apt-get\s+install\s+(?:-[a-z\-]+\s+)*([^\\\n&|;]+)",
            dockerfile_extras, re.IGNORECASE,
        ):
            for pkg in m.group(1).split():
                norm = pkg.strip().lower()
                if not norm or norm.startswith("-"):
                    continue
                if norm in _COPYLEFT_APT_PACKAGES:
                    spdx, alternative = _COPYLEFT_APT_PACKAGES[norm]
                    reasons.append(
                        f"`dockerfile_extras` runs "
                        f"`apt-get install {pkg}` which is "
                        f"{spdx}-licensed. The wizard's licence policy "
                        f"is permissive-only (MIT/Apache/BSD/ISC/MPL). "
                        f"Use {alternative} instead."
                    )

    return reasons


def _validate_type(label: str, value: Any) -> List[str]:
    """No-op validator kept for call-site stability.

    Returns rejection only when the LLM gave us nothing useful (empty
    string or non-string). Domain-specific labels (ifc, parquet,
    dicom, ...) are passed through verbatim so wizard-built nodes for
    AEC / GIS / scientific / ML workloads keep their semantic
    input_type instead of getting flattened to "other".
    """
    if not isinstance(value, str) or not value.strip():
        return [f"{label} missing or empty"]
    return []


def _normalize_type_token(value: Optional[str]) -> Optional[str]:
    """Return the canonical lowercase token if it's one of the known
    categorical types we can enforce against, else None.

    The known set is whatever ``_TYPE_EXTENSIONS`` covers PLUS the
    extension-free categoricals (``binary``, ``texture``, ``document``,
    ``other``). For anything outside that set — domain formats like
    ``ifc`` / ``dicom`` / ``parquet`` — we return None so the
    enforcer skips the value rather than wrongly rejecting it.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    if not token:
        return None
    known = set(_TYPE_EXTENSIONS.keys()) | {"binary", "texture", "document", "other"}
    return token if token in known else None


def _script_extensions(script: str) -> set:
    """Extract the set of file extensions (lowercase, no dot) the
    script references in any of the common glob / suffix forms:

      - ``glob('*.foo')`` / ``glob(\"**/*.foo\")``
      - ``rglob('*.foo')``
      - ``endswith('.foo')``
      - ``Path(...).suffix == '.foo'``
      - hardcoded ``.foo`` literal in a string

    Returns extensions that look like file types (1–5 lowercase
    alphanumeric chars). We are intentionally fuzzy — false-positive
    detection here only causes the validator to recognise an
    extension that may not really be a file pattern, which is
    harmless. The real signal is the SET this returns.
    """
    if not isinstance(script, str) or not script:
        return set()
    found: set = set()
    # `*.ext` inside any string literal
    for m in re.finditer(r"\*\.([a-zA-Z0-9]{1,5})\b", script):
        found.add(m.group(1).lower())
    # `endswith('.ext')` / `endswith((".ext1", ".ext2"))`
    for m in re.finditer(r"endswith\s*\(\s*[\(\[]?\s*['\"]\.([a-zA-Z0-9]{1,5})['\"]", script):
        found.add(m.group(1).lower())
    # `.suffix == '.ext'` / `.suffix.lower() == '.ext'`
    for m in re.finditer(r"\.suffix(?:\.lower\(\))?\s*==\s*['\"]\.([a-zA-Z0-9]{1,5})['\"]", script):
        found.add(m.group(1).lower())
    return found


def _validate_io_contract(
    user_input_type: Optional[str],
    user_output_type: Optional[str],
    suggested_input_type: str,
    suggested_output_type: str,
    script: str,
) -> List[str]:
    """Reject the synthesis when it contradicts the user's stated I/O.

    Two checks, each gated on the user value being a *known
    categorical token* (so we only fire when there's a clear answer):

      1. **Type-token mismatch.** If the user told us "image" and the
         LLM's suggested_input_type is "video", that's a contract
         violation — the metadata the wizard records will say image
         but the script consumes video. Hard reject.

      2. **Script-extension mismatch.** If the user said "image" and
         the script ONLY references extensions from a different
         category (e.g. mp4/avi/mov but no image extensions), the
         script silently disagrees with the metadata even when the
         LLM's suggested_input_type matched. Hard reject.

    Why fail loudly here instead of warn: the runtime contract is
    binding. A node registered as input_type=image with a script
    that greps for *.mp4 will fail every actual user run with "no
    input found" — there's no recovery path other than rebuild.
    Failing during synthesis costs one LLM retry; failing at runtime
    costs the user's whole job + ~5 hours of build queue time.
    """
    reasons: List[str] = []

    in_user = _normalize_type_token(user_input_type)
    in_llm = _normalize_type_token(suggested_input_type)
    if in_user and in_llm and in_user != in_llm:
        reasons.append(
            f"user_input_type={in_user!r} but the synthesizer chose "
            f"suggested_input_type={in_llm!r}. The script must consume "
            f"the type the user picked — pick {in_user!r} or rewrite "
            f"the script to match the user's choice."
        )

    out_user = _normalize_type_token(user_output_type)
    out_llm = _normalize_type_token(suggested_output_type)
    if out_user and out_llm and out_user != out_llm:
        reasons.append(
            f"user_output_type={out_user!r} but the synthesizer chose "
            f"suggested_output_type={out_llm!r}. The script must produce "
            f"the type the user picked — pick {out_user!r} or rewrite "
            f"the script to match the user's choice."
        )

    # Script-extension check. Only fire when (a) the user picked a
    # category whose file extensions we know, AND (b) the script
    # references at least one extension we recognize. If the script
    # discovers inputs by some other means (mime-type sniff, magic
    # bytes, a `for path in input_dir.iterdir()` no-suffix-filter
    # walk), we have no evidence to act on, so we don't.
    if in_user and in_user in _TYPE_EXTENSIONS:
        exts_in_script = _script_extensions(script)
        if exts_in_script:
            expected = set(_TYPE_EXTENSIONS[in_user])
            # Any extension that belongs to a DIFFERENT known category
            # is a smoking gun for a contract violation.
            conflicting = set()
            for ext in exts_in_script:
                for cat, cat_exts in _TYPE_EXTENSIONS.items():
                    if cat == in_user:
                        continue
                    if ext in cat_exts:
                        conflicting.add(ext)
                        break
            overlap_with_expected = exts_in_script & expected
            # If the script greps for any extension from a different
            # category AND uses NO extension from the user's category,
            # the script is reading the wrong file type.
            if conflicting and not overlap_with_expected:
                reasons.append(
                    f"user_input_type={in_user!r} but the script discovers "
                    f"inputs by extension(s) {sorted(conflicting)!r} which "
                    f"belong to a different category. Expected one of "
                    f"{sorted(expected)!r}. Rewrite the script to glob for "
                    f"{in_user} files, not "
                    f"{', '.join(sorted({c for ext in conflicting for c, exts in _TYPE_EXTENSIONS.items() if ext in exts}))}."
                )

    return reasons


_INSTALL_INSTRUCTIONS = {
    "pip": (
        "This repository ships a Python package (setup.py or "
        "pyproject.toml at root). Install it via pip:\n"
        "  pip install git+<repo_url>\n"
        "Then import it directly in the script."
    ),
    "requirements": (
        "This repository has Python source files plus a "
        "requirements.txt but NO setup.py / pyproject.toml — it's not "
        "pip-installable. In the Dockerfile, clone the repo and add "
        "it to PYTHONPATH:\n"
        "  ENV GIT_LFS_SKIP_SMUDGE=1\n"
        "  RUN git clone --depth 1 --filter=blob:none <repo_url> /app/repo \\\n"
        "      && pip install -r /app/repo/requirements.txt\n"
        "  ENV PYTHONPATH=/app/repo\n"
        "Then import from it as if it were a local package."
    ),
    "dockerfile": (
        "This repository has its OWN Dockerfile at root. Use it as "
        "the base of your image:\n"
        "  FROM <build of the repo's own Dockerfile>\n"
        "Or, simpler, clone the repo + run its build script during "
        "your image build. Adapt the project's intended workflow."
    ),
    "clone": (
        "This repository is Python source files at root (no setup.py, "
        "no requirements.txt, no Dockerfile). It's not a package — "
        "treat it as a code drop. In the Dockerfile:\n"
        "  ENV GIT_LFS_SKIP_SMUDGE=1\n"
        "  RUN git clone --depth 1 --filter=blob:none <repo_url> /app/repo\n"
        "  ENV PYTHONPATH=/app/repo\n"
        "Then import the relevant module directly. Install any "
        "required packages (PIL, numpy, etc.) via your own pip-"
        "requirements list — the repo doesn't tell you what it needs, "
        "so infer from the imports in its source."
    ),
}


def _build_user_prompt(
    description: str,
    repo_url: Optional[str],
    repo_full_name: Optional[str],
    install_strategy: Optional[str],
    previous_attempt: Optional[Dict[str, Any]],
    user_input_type: Optional[str] = None,
    user_output_type: Optional[str] = None,
    exemplar: Optional[Dict[str, Any]] = None,
    repo_context: Optional[str] = None,
) -> str:
    lines = [f"User task: {description.strip()}"]
    # Authoritative I/O contract. When the wizard has clean type
    # tokens for what the user said goes in/comes out, surface them
    # as a CONTRACT block — not buried in description prose where
    # the LLM has rewritten them out before (the "photogrammetry =
    # video → mesh" hallucination repro). The validator hard-rejects
    # if the LLM's suggested_input_type / suggested_output_type
    # disagrees with these.
    in_norm = _normalize_type_token(user_input_type)
    out_norm = _normalize_type_token(user_output_type)
    if in_norm or out_norm:
        lines.append(
            "\nI/O CONTRACT — these are non-negotiable. The user has "
            "already told the wizard:"
        )
        if in_norm:
            ext_hint = ""
            if in_norm in _TYPE_EXTENSIONS:
                ext_hint = (
                    f" Discover inputs by globbing for these extensions: "
                    f"{', '.join(_TYPE_EXTENSIONS[in_norm])}."
                )
            lines.append(
                f"  - Input type: {in_norm}. The script MUST consume "
                f"{in_norm} files from /data/input/.{ext_hint} Set "
                f"suggested_input_type to {in_norm!r}."
            )
        if out_norm:
            lines.append(
                f"  - Output type: {out_norm}. The script MUST produce "
                f"{out_norm} files (or one printed line for text-like "
                f"outputs) in /data/output/. Set suggested_output_type "
                f"to {out_norm!r}."
            )
        lines.append(
            "  If the user's chosen library does not natively take "
            "this input type, your script must handle the conversion "
            "INSIDE the script — not change the input type. Example: "
            "if the user said 'image' input but the library wants a "
            "video, your script should assemble the images into a "
            "temporary video first; do NOT switch the node's input "
            "type to 'video'. The user picked 'image' for a reason."
        )
    if repo_url:
        lines.append(f"\nChosen repository: {repo_url}")
        if repo_full_name:
            lines.append(f"Full name: {repo_full_name}")
        # Ground the wrapper in the repo's ACTUAL documentation, not the
        # model's guess from the repo name. Without this the model
        # hallucinates a tool's usage (e.g. `pip install git+…colmap` on a
        # C++ repo, `import colmap`, invented subcommands). These are the
        # repo's own README / install / usage / config chunks, fetched +
        # intent-tagged by node_builder.repo_docs. Authoritative — the model
        # MUST follow the install + invocation the docs actually describe.
        if repo_context and repo_context.strip():
            lines.append(
                "\nDocumentation extracted from the chosen repository — use it "
                "as the authoritative guide to how this project is installed "
                "and invoked. Do NOT invent install commands, module names, or "
                "CLI subcommands that contradict these docs:\n"
                + repo_context.strip()
            )
        strategy_block = _INSTALL_INSTRUCTIONS.get(install_strategy or "")
        if strategy_block:
            lines.append("\n" + strategy_block)
        else:
            # Fallback: legacy "try pip git+..." behaviour for when the
            # caller didn't probe the repo (e.g. user-pasted URL path).
            lines.append(
                "\nUse this repository as the primary implementation. If it "
                "doesn't expose a clean Python API, install it via pip "
                "(`git+...`) and call its CLI."
            )
    else:
        lines.append("\nNo repository chosen — write the script from scratch.")
    # Rich base-image table — variant_dir | accelerator | description.
    # Without descriptions the LLM sees a flat list of cryptic names
    # (`cuda-12.4-python3.11`, `rocm-6.1-python3.11`, …) and asks the
    # user which one to pick. End users don't know — give the LLM
    # everything it needs to pick decisively.
    lines.append(
        "\nAvailable base images. You MUST pick exactly one variant_dir "
        "for base_image_hint — never ask the user. Format below is "
        "`variant_dir | accelerator | description`:"
    )
    for b in CURATED_BASE_IMAGES:
        lines.append(
            f"  - {b.variant_dir} | {b.process_type_hint} | {b.description.strip()}"
        )
    # Exemplar seeding: a prior node that was BUILT SUCCESSFULLY for a
    # similar task. The coder adapts a known-working recipe instead of
    # inventing tool CLIs / base images from scratch — the exact failure
    # (hallucinated OpenMVS CLI) this program targets. Reference only:
    # keep the current task's I/O contract, don't copy verbatim.
    if exemplar and exemplar.get("script"):
        ex_desc = (exemplar.get("description") or exemplar.get("name") or "").strip()[:200]
        # Two provenance flavours: a "learned" exemplar is a node the user's
        # own catalog already built + smoke-passed; a "pinned" exemplar is a
        # curated built-in reference recipe for a known-hard task (cold start,
        # before anything's been learned). Phrase honestly either way.
        if exemplar.get("source") == "pinned":
            header = (
                "\nREFERENCE RECIPE — a curated, known-good starting point for "
                "this KIND of task"
                + (f": \"{ex_desc}\"." if ex_desc else ".")
            )
        else:
            header = (
                "\nWORKING EXAMPLE — a node PREVIOUSLY BUILT SUCCESSFULLY "
                "(it passed a real smoke test) for a SIMILAR task"
                + (f": \"{ex_desc}\"." if ex_desc else ".")
            )
        lines.append(
            header
            + " Use it as a reference for the right base image, the correct "
            "way to install and DRIVE its tools (its CLI is known to work — "
            "prefer it over inventing one), and overall structure. ADAPT it "
            "to the task above; do NOT copy it verbatim and keep the I/O "
            "contract."
        )
        if exemplar.get("base_image"):
            lines.append(f"Example base_image_hint: {exemplar['base_image']}")
        pkgs = exemplar.get("system_packages") or []
        if pkgs:
            lines.append(f"Example system packages: {', '.join(str(p) for p in pkgs)}")
        extras = (exemplar.get("dockerfile_extras") or "").strip()
        if extras:
            lines.append(f"Example dockerfile steps:\n{extras}")
        lines.append(f"Example script (excerpt):\n{exemplar['script']}")
    if previous_attempt:
        prev_script = (previous_attempt.get("script") or "")[:1500]
        prev_error = (previous_attempt.get("error") or "")[:800]
        lines.append(
            "\nThe previous synthesis attempt failed with this error. "
            "Fix the issue.\n"
            f"Previous script (excerpt):\n{prev_script}\n\n"
            f"Error:\n{prev_error}"
        )
    return "\n".join(lines)


# Outer-retry tuning. Ten total attempts before giving up — matches
# the codebase's other LLM-retry patterns (calibration coordinator,
# wizard ML reranker, etc.). The vast majority of recoverable
# failures resolve within attempts 2-3; the rest of the budget is
# slack for the long tail where the LLM has to pivot strategy mid-
# flight (e.g. switch from Node to Python toolchain).
#
# Two safety nets bound worst-case wall time:
#   1. Early-exit on repeated rejection signature (see
#      _is_stuck_loop): if attempt N and N+1 produce the SAME
#      category + rejection-reason fingerprint, the LLM is
#      oscillating, not converging — surface to user immediately.
#   2. Per-call LLM timeout dropped to 30s (was 60s). Successful
#      simple nodes return in <15s, but a complex node (e.g.
#      photogrammetry) generates a large JSON payload — full script
#      plus a Meshroom `dockerfile_extras` block — that legitimately
#      streams for 40-80s. A 30s cap killed those mid-generation and
#      the FE retried the SAME prompt forever (each retry re-timing
#      out at 30s) → the wizard looked frozen. 90s lets the big
#      generations finish on the first attempt while still bailing on
#      a genuinely stuck call.
#
# Worst-case math: 10 × 90s = 15 min ceiling (only hit if every
# attempt stalls). Typical case is 1-3 × ~30s = 30-90s. Telemetry
# (`wizard_synth_attempts_total`) tracks both so we can tune.
_MAX_SYNTH_ATTEMPTS = 10

# node-build §grammar-json — the schema the synth output must match. Sent as a
# grammar constraint (`response_format: json_schema`) so even a weak local model
# emits a valid, parseable object instead of a ```python block (which is what
# granite-2b does when only given the `json_object` hint). The fields mirror
# exactly what the parser below reads; only script_content is required so a
# model is never forced to fabricate the optional metadata.
_SYNTH_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "script_content": {"type": "string"},
        "base_image_hint": {"type": "string"},
        "requirements": {"type": "array", "items": {"type": "string"}},
        "system_packages": {"type": "array", "items": {"type": "string"}},
        "dockerfile_extras": {"type": "string"},
        "suggested_input_type": {"type": "string"},
        "suggested_output_type": {"type": "string"},
        "reasoning": {"type": "string"},
        # sz-tcp.4 — tiered credit pricing. Optional (only script_content/
        # base_image_hint are required) so a model that can't estimate cost
        # just omits them and admission falls back to the rule floors.
        # Untyped on purpose: the grammar converters behind the weaker local
        # models choke on union `type` arrays, and an empty schema means
        # "any JSON value" — the parser below is what actually validates.
        "suggested_credits_small": {},
        "suggested_credits_medium": {},
        "suggested_credits_large": {},
    },
    "required": ["script_content", "base_image_hint"],
}


def _coerce_credits(value: Any) -> Optional[int]:
    """Normalise one LLM credit suggestion to a non-negative int (sz-tcp.4).

    Missing / non-numeric / bool / negative / garbage -> None so the tier
    stays blank and admission falls back to the rule floors. Never raises.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _tier_credits(parsed: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Parse the three per-size-tier credit suggestions out of the LLM's
    payload (sz-tcp.4). Legacy single-field fallback: when the model only
    emitted `suggested_credits` (or the node's single-value `cost_in_credits`
    basis), `medium` falls back to it — the platform's own invariant is that
    a node's single cost equals its medium-tier cost
    (ck_node_def_cost_in_credits_matches_medium in models.py)."""
    out = {
        "suggested_credits_small": _coerce_credits(parsed.get("suggested_credits_small")),
        "suggested_credits_medium": _coerce_credits(parsed.get("suggested_credits_medium")),
        "suggested_credits_large": _coerce_credits(parsed.get("suggested_credits_large")),
    }
    if out["suggested_credits_medium"] is None:
        for key in ("suggested_credits", "cost_in_credits"):
            legacy = _coerce_credits(parsed.get(key))
            if legacy is not None:
                out["suggested_credits_medium"] = legacy
                break
    return out


def _max_synth_attempts() -> int:
    """node-build §scale-with-model — retry attempts scale INVERSELY with model
    size. A weak local model produces the wrapper JSON fine (grammar-forced) but
    slips on code quality (a stray bracket), and each retry threads the error
    back so the next attempt corrects it — so a small model gets MANY chances
    ("works" beats "doesn't work"); a strong model lands it first-try and needs
    few. Env-tunable; never raises."""
    import os
    from utils.model_size import scaled_by_inverse_vram
    base = float(os.getenv("NODE_SYNTH_ATTEMPTS_BASE_GB", "100"))
    floor = int(os.getenv("NODE_SYNTH_ATTEMPTS_FLOOR", "3"))
    ceil = int(os.getenv("NODE_SYNTH_ATTEMPTS_CEIL", "25"))
    return scaled_by_inverse_vram(base, floor, ceil, tool="wizard")


def _synth_timeout_s() -> int:
    """node-build §scale-with-model — the LLM timeout scales INVERSELY with
    model size: a small local model is slow generating the wrapper JSON and must
    be allowed to finish ("works" beats "doesn't work"); a big/fast model
    completes quickly so a tight cap is fine. Someone who wants it faster buys a
    bigger GPU or points the tool at a cloud model. Env-tunable; never raises."""
    import os
    from utils.model_size import scaled_by_inverse_vram
    base = float(os.getenv("NODE_SYNTH_TIMEOUT_BASE_GB_S", "3000"))
    floor = int(os.getenv("NODE_SYNTH_TIMEOUT_FLOOR_S", "90"))
    ceil = int(os.getenv("NODE_SYNTH_TIMEOUT_CEIL_S", "900"))
    return scaled_by_inverse_vram(base, floor, ceil, tool="wizard")


def _synth_max_tokens() -> int:
    """Output-token cap for a synth generation. This is NOT model-size scaled:
    the ceiling is a function of the OUTPUT length (a full script wrapped in the
    JSON envelope), not the model's VRAM. The old default (`LLM_MAX_TOKENS`=4096)
    truncated granite's grammar-constrained JSON mid-string — the parser then saw
    an "unterminated string" and the attempt failed for a reason the model could
    never fix by retrying. A generous flat cap lets the whole script land in one
    shot. Env-tunable; never raises."""
    import os
    try:
        return max(4096, int(os.getenv("NODE_SYNTH_MAX_TOKENS", "12000")))
    except (TypeError, ValueError):
        return 12000

# Categories where same-input retry CAN'T fix the rejection (because
# the input itself is the problem):
#   - `repo_locked_license` — repo's own recipe is copyleft; retrying
#     with the same repo will produce the same recipe. The user must
#     pick a different repo.
# Everything else is retryable: a different LLM completion might
# choose a different pip package, declare missing apt deps, rewrite
# the script to use a different toolchain, etc.
_NEVER_RETRY_CATEGORIES: frozenset = frozenset({"repo_locked_license"})


# Prometheus counter for synth retry observability. Lazy import — the
# wider codebase pattern.
try:
    from prometheus_client import Counter as _PromCounter
    _METRIC_WIZARD_SYNTH_ATTEMPTS = _PromCounter(
        "wizard_synth_attempts_total",
        "Wizard synthesize attempts by outcome and attempt number.",
        ["outcome", "attempt", "category"],
    )
except Exception:  # pragma: no cover - defensive
    _METRIC_WIZARD_SYNTH_ATTEMPTS = None


def _emit_synth_attempt(outcome: str, attempt: int, category: str) -> None:
    if _METRIC_WIZARD_SYNTH_ATTEMPTS is None:
        return
    try:
        _METRIC_WIZARD_SYNTH_ATTEMPTS.labels(
            outcome=outcome,
            attempt=str(attempt),
            category=category or "n/a",
        ).inc()
    except Exception:
        pass


def _progress(progress_cb: Optional[Callable[..., Any]], event: str, **data: Any) -> None:
    """Emit a user-facing progress event (piece 3 streaming). The wrapper's
    internal loop is otherwise invisible to the UI, which just sees one long
    request; these events let the wizard show real stages ("generating →
    reviewing → revising → escalating") instead of a frozen-looking spinner.
    Best-effort: a bad callback never breaks synthesis."""
    if progress_cb is None:
        return
    try:
        progress_cb(event, data)
    except Exception:
        pass


def _rejection_signature(result: SynthesisResult) -> str:
    """Stable fingerprint of a rejection for stuck-loop detection.

    We can't compare full rejection_reasons strings — line offsets,
    error message phrasing, and the LLM's regenerated-but-equivalent
    output all churn the text superficially. The signature collapses
    each reason to its first ~60 chars (the actionable prefix —
    usually "script calls binary 'X'..." or "requirement 'X' fails
    license policy..."), sorted so order doesn't matter.
    """
    category = result.rejection_category or "n/a"
    reason_keys = sorted(
        (r.strip()[:60] for r in (result.rejection_reasons or [])),
    )
    return f"{category}|" + "||".join(reason_keys)


def _reasoner_generation_llm() -> Optional[Callable[..., Any]]:
    """A generation callable backed by the REASONER (gpt-oss via the 'critic'
    tool) instead of the fast coder — for escalation when the coder loop stalls.

    Returns None when no DISTINCT reasoner is available (e.g. vllm-big down, so
    the tool falls back to the coder model — escalation would be a no-op).
    Reachable here because synthesis runs in the API process, not the worker
    egress sandbox (gpt-oss is internal-only)."""
    try:
        from utils.llm_client import _get_tool_config
        renv = _get_tool_config("critic")
        wenv = _get_tool_config("wizard")
    except Exception:
        return None
    if not (renv.get("LLM_BASE_URL") and renv.get("LLM_MODEL")):
        return None
    if (renv.get("LLM_MODEL") or "") == (wenv.get("LLM_MODEL") or ""):
        return None  # no distinct reasoner — nothing to escalate to

    def _gen(**kwargs):
        from utils.llm_client import call_wizard_llm
        kwargs["_env_override"] = renv
        kwargs["_label"] = "reasoner-gen"
        kwargs["max_tokens"] = 12000  # full script + gpt-oss hidden reasoning
        kwargs["timeout_s"] = max(int(kwargs.get("timeout_s") or 0), 120)
        return call_wizard_llm(**kwargs)
    return _gen


def _reasoner_escalation_attempt(
    description: str,
    *,
    previous_attempt: Optional[Dict[str, Any]],
    repo_url: Optional[str],
    repo_full_name: Optional[str],
    install_strategy: Optional[str],
    user_input_type: Optional[str],
    user_output_type: Optional[str],
    already_clarified: bool,
    exemplar: Optional[Dict[str, Any]] = None,
    repo_context: Optional[str] = None,
) -> Optional[SynthesisResult]:
    """ONE final generation with the reasoner when the coder stalled. Returns a
    SynthesisResult (ok or not — our best shot), or None if no reasoner is
    available (caller then falls back to its normal give-up path). This is the
    last attempt: Critic D runs once for advisory warnings but never triggers
    another regen or a clarify question."""
    gen = _reasoner_generation_llm()
    if gen is None:
        return None
    logger.info("wizard synth: coder stalled — escalating generation to the reasoner")
    _emit_synth_attempt("reasoner_escalation", 0, "escalation")
    try:
        result = _synthesize_once(
            description, repo_url=repo_url, repo_full_name=repo_full_name,
            install_strategy=install_strategy, previous_attempt=previous_attempt,
            user_input_type=user_input_type, user_output_type=user_output_type,
            exemplar=exemplar, repo_context=repo_context,
            llm=gen,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("wizard synth: reasoner escalation errored (%s)", e)
        return None
    if not result.ok:
        return result  # reasoner also failed structurally — still our best shot
    try:
        from node_builder.critic_synth import critique_script
        critique = critique_script(
            description, result.script_content,
            input_type=result.suggested_input_type,
            output_type=result.suggested_output_type,
            already_clarified=True,  # final attempt: warn, never re-ask/re-gen
        )
        result.warnings = list(critique.warnings)
        if critique.regenerate_reasons:
            result.warnings.append("Heads up: " + critique.regenerate_reasons[0])
    except Exception:  # noqa: BLE001
        pass
    _emit_synth_attempt("reasoner_success", 0, "escalation")
    return result


# Intents (from node_builder.repo_docs.intent_tagger) that actually help a
# wrapper author, in priority order. "install"/"run" describe how the tool is
# set up and invoked; "env"/"data" cover config + I/O; "usage" is a catch-all.
_REPO_CONTEXT_INTENT_PRIORITY = ("install", "run", "usage", "env", "data")
_REPO_CONTEXT_MAX_CHARS = 6000


def fetch_repo_context(
    db: Any,
    *,
    repo_url: Optional[str],
    tenant_id: int,
    auth_token: Optional[str] = None,
) -> Optional[str]:
    """Fetch the chosen repo's own documentation (README / install / usage /
    config) and format it as a compact, prompt-ready block.

    This is what lets the synthesizer WRAP A REPO IT HAS ACTUALLY READ rather
    than guessing the tool's usage from its name. Uses the cached repo-docs
    pipeline (`node_builder.repo_docs.store.get_or_extract`) — 7-day TTL, so
    repeated builds of the same repo don't re-fetch. Best-effort: returns None
    on any failure (no network, unsupported host, empty repo) and the caller
    falls back to name-only synthesis.
    """
    if not repo_url or not repo_url.strip():
        return None
    try:
        from node_builder.repo_docs.store import get_or_extract, chunks_from_row
        row, _hit = get_or_extract(
            db, tenant_id=tenant_id, repo_url=repo_url.strip(),
            ref="", auth_token=auth_token,
        )
        chunks = chunks_from_row(row)
    except Exception as e:  # noqa: BLE001 — never block synthesis on doc fetch
        logger.info("repo-docs fetch skipped for %s: %s", repo_url, e)
        return None
    if not chunks:
        return None

    def _rank(chunk: Any) -> int:
        intents = set(getattr(chunk, "intents", None) or [])
        for i, name in enumerate(_REPO_CONTEXT_INTENT_PRIORITY):
            if name in intents:
                return i
        return len(_REPO_CONTEXT_INTENT_PRIORITY)

    ordered = sorted(chunks, key=_rank)
    parts: List[str] = []
    total = 0
    for c in ordered:
        content = (getattr(c, "content", "") or "").strip()
        if not content:
            continue
        src = getattr(c, "source_path", None) or getattr(c, "chunk_id", "") or ""
        block = f"--- {src} ---\n{content}"
        if total + len(block) > _REPO_CONTEXT_MAX_CHARS:
            block = block[: max(0, _REPO_CONTEXT_MAX_CHARS - total)]
        parts.append(block)
        total += len(block)
        if total >= _REPO_CONTEXT_MAX_CHARS:
            break
    return "\n\n".join(parts) if parts else None


def synthesize_wrapper(
    description: str,
    *,
    repo_url: Optional[str] = None,
    repo_full_name: Optional[str] = None,
    install_strategy: Optional[str] = None,
    previous_attempt: Optional[Dict[str, Any]] = None,
    user_input_type: Optional[str] = None,
    user_output_type: Optional[str] = None,
    exemplar: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[..., Any]] = None,
    llm: Optional[Callable[..., Any]] = None,
    max_attempts: Optional[int] = None,
    already_clarified: bool = False,
    repo_context: Optional[str] = None,
) -> SynthesisResult:
    """Generate a wrapper script.

    **Self-healing retry loop** — when the first attempt fails a
    validator, the rejection reasons get fed BACK into the LLM
    prompt as `previous_attempt`, and we call the LLM again.

    Categories that can't be helped by same-input retry (currently
    just ``repo_locked_license``) short-circuit on the first
    failure. Every other category retries up to ``_MAX_SYNTH_ATTEMPTS``
    times — the LLM almost always fixes the issue by the 3rd try
    because it sees the prior script + the exact rejection reasons.

    The final ``SynthesisResult`` carries ``attempts`` so the caller
    (and telemetry) can see how many tries it took.

    ``previous_attempt={'script': ..., 'error': ...}`` from an
    EXTERNAL caller (e.g. the user clicked "Build it again" in the
    UI) is honored as the starting point — the retry loop just
    threads further retries from there.
    """
    description = (description or "").strip()
    if not description:
        return SynthesisResult(
            ok=False,
            rejection_reasons=["description is required"],
        )
    if llm is None:
        llm = call_wizard_llm

    # No curated-recipe short-circuit. The wizard stands on its own: every node
    # is produced by docs-driven synthesis (description + the chosen repo's own
    # docs), guarded by the validators + critics — which now reject certain-crash
    # hallucinations as FATAL, so a weak model fails cleanly instead of shipping
    # garbage. A LEARNED exemplar (the user's own smoke-passed node) still seeds
    # the prompt when one exists; there are no hand-written tool recipes.
    attempts_cap = max_attempts if max_attempts is not None else _max_synth_attempts()
    current_prev = previous_attempt
    last_result: Optional[SynthesisResult] = None
    # Most recent STRUCTURALLY-VALID script. Critic D can ask for a regen after
    # a script already passed the validators; if that regen then comes back
    # broken, we ship this instead of failing the whole synthesis.
    last_valid_result: Optional[SynthesisResult] = None
    # Track the rejection-signature of the prior attempt so we can
    # short-circuit when the LLM is oscillating instead of converging.
    prior_signature: Optional[str] = None
    # When the coder oscillates (same rejection twice in a row), one root cause is
    # that the accumulated failed-attempt feedback in `current_prev` has the small
    # model FIXATED on a broken approach — more feedback deepens the rut instead of
    # breaking it. Before escalating/giving up, DISCARD that context and let it
    # draft fresh a few times: a clean slate often lands a different, working
    # approach. Env-tunable; 0 disables (falls straight through to escalation).
    import os as _os_fr
    fresh_restarts_used = 0
    fresh_restart_max = max(0, int(_os_fr.getenv("NODE_SYNTH_FRESH_RESTARTS", "2")))
    # Same oscillation problem on the CRITIC path: a structurally-valid script the
    # critic keeps flagging with the SAME finding churns the critic-regen loop to
    # exhaustion (repro 2026-09-04: grayscale burned 11 attempts on one repeated
    # directions finding). Track the critic finding-signature so the fresh-restart
    # logic covers this path too, not just structural rejections.
    prior_critic_signature: Optional[str] = None

    def _escalate(prev: Optional[Dict[str, Any]], attempt_count: int = 0) -> Optional[SynthesisResult]:
        """Escalate ONE generation to the reasoner (gpt-oss) with `prev` as the
        feedback context. None when no distinct reasoner is available. Stamps
        the escalation result with the true cumulative attempt count (the coder
        attempts already spent + this escalation) so telemetry isn't misleading
        — otherwise a fresh `_synthesize_once` result reports `attempts=1`."""
        _progress(progress_cb, "escalating")
        esc = _reasoner_escalation_attempt(
            description, previous_attempt=prev,
            repo_url=repo_url, repo_full_name=repo_full_name,
            install_strategy=install_strategy,
            user_input_type=user_input_type, user_output_type=user_output_type,
            already_clarified=already_clarified,
            exemplar=exemplar,
            repo_context=repo_context,
        )
        if esc is not None:
            esc.attempts = attempt_count + 1
        return esc

    if exemplar and exemplar.get("name"):
        _progress(progress_cb, "seeded", name=exemplar.get("name"))

    for attempt_idx in range(attempts_cap):
        attempt_num = attempt_idx + 1
        _progress(progress_cb, "generating", attempt=attempt_num, max=attempts_cap)
        result = _synthesize_once(
            description,
            repo_url=repo_url,
            repo_full_name=repo_full_name,
            install_strategy=install_strategy,
            previous_attempt=current_prev,
            user_input_type=user_input_type,
            user_output_type=user_output_type,
            exemplar=exemplar,
            repo_context=repo_context,
            llm=llm,
        )
        result.attempts = attempt_num
        last_result = result

        if result.ok:
            # Remember the last structurally-valid script — a Critic-D regen
            # must never be able to destroy a working result.
            last_valid_result = result

            # Critic D — feasibility/intent review (reasoning model) before we
            # accept. Runs only on structurally-valid scripts and never blocks:
            # any critic failure returns an empty critique (accept as-is).
            _progress(progress_cb, "reviewing", attempt=attempt_num)
            critique = None
            try:
                from node_builder.critic_synth import critique_script
                critique = critique_script(
                    description, result.script_content,
                    input_type=result.suggested_input_type,
                    output_type=result.suggested_output_type,
                    already_clarified=already_clarified,
                )
            except Exception as _cd_err:  # noqa: BLE001 — never block on the critic
                logger.warning("critic-d: skipped (%s)", _cd_err)
                critique = None
            # "Will this code actually RUN?" second-guess — an executability
            # review (deterministic patterns + an aggressive LLM pass) that
            # catches crash-on-run bugs the INTENT critic ignores (hallucinated
            # imports, pip-installing a C++ repo, invented CLI usage, str/Path
            # type bugs, brace-glob). Its findings feed the SAME regen loop so
            # the coder fixes them BEFORE the user ever sees the script.
            try:
                from node_builder.critic_synth import critique_executability
                _exec_crit = critique_executability(
                    description, result.script_content,
                    requirements=result.requirements,
                    system_packages=result.system_packages,
                    repo_context=repo_context,
                )
                if critique is None:
                    critique = _exec_crit
                elif _exec_crit is not None:
                    critique.regenerate_reasons.extend(_exec_crit.regenerate_reasons)
                    critique.warnings.extend(_exec_crit.warnings)
            except Exception as _ec_err:  # noqa: BLE001 — never block on the critic
                logger.warning("exec-critic: skipped (%s)", _ec_err)

            # The DIRECTIONS critic — a dedicated pass that checks the script
            # against the runtime contract and reports each broken direction as
            # "Direction not followed: …" so a weak local model gets an
            # unambiguous instruction-following signal into the regen loop.
            try:
                from node_builder.critic_synth import critique_directions
                _dir_crit = critique_directions(
                    result.script_content,
                    description=description,
                    requirements=result.requirements,
                    system_packages=result.system_packages,
                    dockerfile_extras=result.dockerfile_extras,
                )
                if _dir_crit.regenerate_reasons:
                    logger.info(
                        "directions-critic: %d direction(s) not followed",
                        len(_dir_crit.regenerate_reasons),
                    )
                if critique is None:
                    critique = _dir_crit
                else:
                    critique.regenerate_reasons.extend(_dir_crit.regenerate_reasons)
            except Exception as _dc_err:  # noqa: BLE001 — never block on the critic
                logger.warning("directions-critic: skipped (%s)", _dc_err)

            if critique is not None:
                result.warnings = list(critique.warnings)
                # v2 — genuinely ambiguous intent: ask the user ONE question
                # instead of proceeding. The script is valid (ok stays True);
                # the wizard checks needs_clarification and surfaces the Q.
                if critique.clarifying_question and not already_clarified:
                    result.needs_clarification = True
                    result.clarifying_question = critique.clarifying_question
                    _emit_synth_attempt("needs_clarification", attempt_num, "critic_d")
                    logger.info("critic-d: asking the user to clarify intent")
                    return result
                if critique.wants_regen and attempt_num < attempts_cap:
                    # Oscillation guard for the CRITIC path: if the critic returns
                    # the SAME finding as last time, feeding it back again won't
                    # help — try a fresh-context restart (drop the trail); once the
                    # budget is spent, stop churning and escalate/ship the last
                    # valid script rather than burning the whole attempt cap.
                    critic_sig = "|".join(sorted(
                        r.split(".")[0][:60] for r in critique.regenerate_reasons))
                    if critic_sig and critic_sig == prior_critic_signature:
                        if fresh_restarts_used < fresh_restart_max:
                            fresh_restarts_used += 1
                            _emit_synth_attempt("fresh_restart", attempt_num, "critic_d")
                            logger.info(
                                "critic-d: oscillating on the same finding — fresh-"
                                "context restart %d/%d (discarding critic trail)",
                                fresh_restarts_used, fresh_restart_max,
                            )
                            _progress(progress_cb, "restarting",
                                      attempt=attempt_num, why="critic-oscillation")
                            current_prev = None
                            prior_critic_signature = None
                            continue
                        # Budget spent and still the same finding — stop churning.
                        _emit_synth_attempt("critic_stuck", attempt_num, "critic_d")
                        logger.info(
                            "critic-d: stuck on the same finding after %d fresh "
                            "restarts — escalating, else shipping last valid script",
                            fresh_restart_max,
                        )
                        esc = _escalate({
                            "script": result.script_content,
                            "error": "\n".join(critique.regenerate_reasons[:8]),
                            "category": "critic_d",
                        }, attempt_count=attempt_num)
                        if esc is not None:
                            return esc
                        if last_valid_result is not None:
                            last_valid_result.warnings = list(last_valid_result.warnings) + [
                                "Heads up (unresolved after retries): "
                                + critique.regenerate_reasons[0]]
                            return last_valid_result
                        return result
                    prior_critic_signature = critic_sig
                    # Auto-fix: feed the critic's findings into the coder's retry
                    # loop. Bypass the structural-failure machinery — this isn't
                    # a validator rejection.
                    _emit_synth_attempt("critic_regen", attempt_num, "critic_d")
                    _progress(progress_cb, "revising", attempt=attempt_num, why="review")
                    logger.info(
                        "critic-d: attempt %d regen requested (%d finding(s))",
                        attempt_num, len(critique.regenerate_reasons),
                    )
                    current_prev = {
                        "script": result.script_content,
                        "error": "\n".join(critique.regenerate_reasons[:8]),
                        "category": "critic_d",
                    }
                    continue
                if critique.wants_regen:
                    # Coder spent its attempts and the critic STILL wants a regen
                    # (can't satisfy the feasibility/intent check) — escalate ONE
                    # generation to the reasoner before shipping the flagged
                    # script. This is the Meshroom-class case.
                    esc = _escalate({
                        "script": result.script_content,
                        "error": "\n".join(critique.regenerate_reasons[:8]),
                        "category": "critic_d",
                    }, attempt_count=attempt_num)
                    if esc is not None:
                        return esc

            _emit_synth_attempt("success", attempt_num, result.rejection_category or "")
            if attempt_num > 1:
                logger.info(
                    "wizard synth: succeeded on attempt %d/%d (prior category=%s)",
                    attempt_num, attempts_cap,
                    (current_prev or {}).get("category", "n/a"),
                )
            return result

        # A STRUCTURAL failure after we already had a valid script means a
        # Critic-D-driven regen made things worse. Ship the prior valid script
        # rather than fail the whole synthesis (ship-rather-than-block).
        if last_valid_result is not None:
            _emit_synth_attempt("critic_ship_prior", attempt_num, "critic_d")
            logger.info(
                "critic-d: regen on attempt %d came back broken (%s); shipping "
                "the prior valid script.",
                attempt_num, result.rejection_category or "?",
            )
            return last_valid_result

        # No retry on never-retry categories.
        if result.rejection_category in _NEVER_RETRY_CATEGORIES:
            _emit_synth_attempt("rejected_no_retry", attempt_num, result.rejection_category)
            return result

        # No retry on GENUINE infra/transport errors — a refused connection,
        # timeout, missing base URL or HTTP 5xx won't improve by re-sampling,
        # so retrying 10× just burns wall-clock. An unparseable/misshaped
        # OUTPUT is NOT infra — it's a weak-model slip that the next sample
        # usually fixes, so it falls through to the normal retry path below.
        if any(r.startswith("LLM unavailable:") or r.startswith("LLM raised")
               for r in result.rejection_reasons):
            _emit_synth_attempt("infra_failure", attempt_num, result.rejection_category or "")
            return result

        # Early-exit on stuck-loop: same fingerprint two attempts in a
        # row means the LLM is oscillating. The fingerprint is
        # category + sorted first-line-of-each-reason. We don't compare
        # full reasons because line offsets / strings often vary
        # superficially between attempts.
        current_signature = _rejection_signature(result)
        if prior_signature is not None and current_signature == prior_signature:
            # Oscillating. First try a FRESH-CONTEXT restart (drop the poisoned
            # feedback trail) up to the budget; only then escalate/give up.
            if fresh_restarts_used < fresh_restart_max:
                fresh_restarts_used += 1
                _emit_synth_attempt("fresh_restart", attempt_num,
                                    result.rejection_category or "")
                logger.info(
                    "wizard synth: oscillating on attempt %d (signature=%s) — "
                    "fresh-context restart %d/%d (discarding failed-attempt trail)",
                    attempt_num, current_signature[:60],
                    fresh_restarts_used, fresh_restart_max,
                )
                _progress(progress_cb, "restarting", attempt=attempt_num,
                          why="oscillation")
                current_prev = None        # clean slate — the fixation is in here
                prior_signature = None      # reset so the fresh run isn't insta-stuck
                continue
            _emit_synth_attempt("stuck_loop", attempt_num, result.rejection_category or "")
            logger.info(
                "wizard synth: stuck-loop after attempt %d (signature=%s), fresh "
                "restarts exhausted — escalating",
                attempt_num, current_signature[:80],
            )
            esc = _escalate(current_prev, attempt_count=attempt_num)   # coder oscillating — try the reasoner
            if esc is not None:
                return esc
            return result
        prior_signature = current_signature

        # Out of attempts — try the reasoner once, else surface the final
        # rejection to the user.
        if attempt_num >= attempts_cap:
            _emit_synth_attempt("exhausted", attempt_num, result.rejection_category or "")
            logger.warning(
                "wizard synth: exhausted %d attempts, last category=%s, reasons=%s",
                attempts_cap, result.rejection_category,
                "; ".join(result.rejection_reasons[:3]),
            )
            esc = _escalate(current_prev, attempt_count=attempt_num)
            if esc is not None:
                return esc
            return result

        # Set up the next attempt's feedback — include both the
        # generated script AND the specific rejection reasons so the
        # LLM has the maximum context to fix the actual problem.
        _emit_synth_attempt("retrying", attempt_num, result.rejection_category or "")
        _progress(progress_cb, "revising", attempt=attempt_num, why="validation")
        current_prev = {
            "script": result.script_content,
            "error": "\n".join(result.rejection_reasons[:8]),
            "category": result.rejection_category or "unknown",
        }
        logger.info(
            "wizard synth: attempt %d failed (category=%s), retrying with feedback",
            attempt_num, result.rejection_category,
        )

    # Defensive — loop body always returns; this is unreachable.
    return last_result if last_result is not None else SynthesisResult(
        ok=False,
        rejection_reasons=["synthesis retry loop exited without producing a result"],
    )


# Import-name -> real PyPI package. A weak model files the IMPORT name as the
# requirement (repro 2026-09-04: `PIL` for Pillow, `cv2` for opencv-python) — those
# are NOT installable and the build's `pip install` fails. Deterministic rewrite.
_REQ_ALIASES = {
    "pil": "Pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
    "skimage": "scikit-image", "bs4": "beautifulsoup4", "yaml": "PyYAML",
    "pil-simd": "Pillow-SIMD",
}


def _fix_requirement_aliases(reqs: Any) -> List[str]:
    """Rewrite import-name requirements to their real PyPI package and dedup by
    base name, so `['PIL','Pillow']` -> `['Pillow']`. Never raises."""
    if not isinstance(reqs, list):
        return reqs if isinstance(reqs, list) else []
    out: List[str] = []
    seen: set = set()
    for r in reqs:
        if not isinstance(r, str):
            out.append(r)                 # let _validate_requirements reject non-str
            continue
        base = _req_base_name(r)
        val = _REQ_ALIASES.get(base, r)
        key = _req_base_name(val)
        if key and key in seen:           # dedup only real, named packages
            continue
        if key:
            seen.add(key)
        out.append(val)                   # blanks (key == "") pass through to the validator
    return out


def _req_base_name(req: str) -> str:
    """Base pip package name of a requirement line — strips version
    specifiers (`==1.2`, `>=3`), extras (`pkg[foo]`), and environment
    markers (`; python_version<'3.9'`) so we can dedup a declared
    `Pillow==10.0` against a derived `Pillow`. Lower-cased, with
    underscores normalized to hyphens (PEP 503)."""
    s = (req or "").strip()
    if not s:
        return ""
    s = s.split(";", 1)[0]                       # env marker
    s = re.split(r"[<>=!~\[ ]", s, 1)[0]         # version/extras/space
    return s.strip().lower().replace("_", "-")


def _backfill_requirements_from_imports(
    script: str, declared: Any, declared_system_packages: Any = None
) -> tuple[List[str], List[str]]:
    """Weak-model hardening for the (requirements, system_packages) pair.

    A small local model reliably writes correct `import` lines but is
    sloppy about the dependency fields. Three deterministic corrections:

      1. BACKFILL — imports with no matching requirement get added, so
         the node doesn't ``ModuleNotFoundError`` at runtime. The model's
         own entries win (they may carry version pins); derived names
         only fill gaps.
      2. RECLASSIFY — a pip package the model mis-filed under
         `system_packages` (e.g. `apt-get install pypdfium2`, which does
         not exist) is detected by matching it against the script's
         actual Python imports, then moved to requirements. Self-
         correcting: no hardcoded package list, driven by what the
         script imports.
      3. APT RUNTIME DEPS — union in the apt libs the resolved pip
         packages need (libGL, ffmpeg, …).

    Returns (requirements, system_packages). Never raises; on any
    failure the declared lists pass through untouched."""
    declared_list = [str(x) for x in declared if isinstance(x, str)] if isinstance(declared, list) else []
    sys_list = [str(x) for x in declared_system_packages if isinstance(x, str)] if isinstance(declared_system_packages, list) else []
    try:
        from node_builder.build_plan.extractors.script import (
            _extract_python_imports,
            _infer_system_deps,
        )
        imported = _extract_python_imports(script or "")
    except Exception:  # noqa: BLE001 — backfill is best-effort
        return declared_list, sys_list

    # Bundled with every Python image — never a pip requirement even
    # though they aren't stdlib. A weak model sometimes writes `import pip`
    # to shell out an install; that's a code smell, not a dependency.
    _NOT_A_REQUIREMENT = {"pip", "setuptools", "wheel", "pkg-resources", "pkg_resources"}
    imported_bases = {_req_base_name(p) for p in imported}

    # (2) reclassify misplaced pip packages out of system_packages: an
    # apt entry that equals one of the script's Python imports is a pip
    # dep in the wrong field (apt has no such package).
    kept_sys: List[str] = []
    reclassified: List[str] = []
    for pkg in sys_list:
        if _req_base_name(pkg) in imported_bases:
            reclassified.append(pkg)
        else:
            kept_sys.append(pkg)

    # (1) backfill imports (+ reclassified) into requirements.
    have = {_req_base_name(r) for r in declared_list}
    merged = list(declared_list)
    for pkg in list(imported) + reclassified:
        base = _req_base_name(pkg)
        if base and base not in have and base not in _NOT_A_REQUIREMENT:
            merged.append(pkg)
            have.add(base)

    # (3) apt runtime deps for the resolved pip set, unioned with the
    # legitimate apt packages the model kept.
    try:
        apt_needed = _infer_system_deps([_req_base_name(r) for r in merged])
    except Exception:  # noqa: BLE001
        apt_needed = []
    have_sys = {p.strip().lower() for p in kept_sys}
    final_sys = list(kept_sys) + [p for p in apt_needed if p.lower() not in have_sys]
    return merged, final_sys


def _synthesize_once(
    description: str,
    *,
    repo_url: Optional[str] = None,
    repo_full_name: Optional[str] = None,
    install_strategy: Optional[str] = None,
    previous_attempt: Optional[Dict[str, Any]] = None,
    user_input_type: Optional[str] = None,
    user_output_type: Optional[str] = None,
    exemplar: Optional[Dict[str, Any]] = None,
    repo_context: Optional[str] = None,
    llm: Callable[..., Any],
) -> SynthesisResult:
    """Single LLM call + validation pass.

    Returns the raw result of one attempt — `synthesize_wrapper`
    wraps this in the retry loop.
    """
    user_prompt = _build_user_prompt(
        description,
        repo_url,
        repo_full_name,
        install_strategy,
        previous_attempt,
        user_input_type=user_input_type,
        user_output_type=user_output_type,
        exemplar=exemplar,
        repo_context=repo_context,
    )
    try:
        parsed = llm(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_format="json",
            json_schema=_SYNTH_JSON_SCHEMA,
            timeout_s=_synth_timeout_s(),
            max_tokens=_synth_max_tokens(),
        )
    except LLMClientError as e:
        # Distinguish a re-sampleable OUTPUT failure (a weak model emitted
        # one unparseable/misshaped sample — the next sample usually parses)
        # from a genuine INFRA failure (no base URL, connection refused,
        # timeout, HTTP 5xx). The retry loop short-circuits infra (retrying
        # a refused connection 10× just wastes wall-clock) but re-samples an
        # unparseable output like any other structural rejection.
        msg = str(e)
        _resampleable = (
            "unparseable JSON" in msg
            or "non-JSON response envelope" in msg
            or "unexpected LLM response shape" in msg
        )
        prefix = "LLM output unparseable (retryable):" if _resampleable else "LLM unavailable:"
        return SynthesisResult(
            ok=False,
            rejection_reasons=[f"{prefix} {e}"],
        )
    except Exception as e:
        logger.warning("describe-synthesize LLM raised: %s", e)
        return SynthesisResult(
            ok=False,
            rejection_reasons=[f"LLM raised an unexpected error: {e}"],
        )

    if not isinstance(parsed, dict):
        return SynthesisResult(
            ok=False,
            rejection_reasons=["LLM did not return a JSON object"],
        )

    script = parsed.get("script_content") or ""
    if not isinstance(script, str):
        script = ""
    # Repair the common LLM slip of using a stdlib module (esp. sys.stderr in an
    # except handler) without importing it — otherwise the node NameError-crashes
    # on its first error path. Runs before validation so the fixed script is what
    # gets stored + built.
    script = _repair_missing_stdlib_imports(script)
    # `import PIL` + `PIL.Image.open()` -> AttributeError; inject `import PIL.Image`.
    script = _repair_pil_submodule_imports(script)
    # Image.ANTIALIAS was removed in Pillow 10 -> rewrite to Image.LANCZOS.
    script = _repair_removed_pil_constants(script)
    # Inject OUT.mkdir(...) when the script writes under /data/output without
    # creating it — cv2.imwrite/PIL .save silently write nothing otherwise. Runs
    # on EVERY path (incl. the reasoner escalation, which skips the directions
    # critic), so a "saved…" no-op can't ship.
    script = _repair_missing_output_mkdir(script)
    requirements = parsed.get("requirements") or []
    base_hint = parsed.get("base_image_hint") or ""
    in_type = parsed.get("suggested_input_type") or "other"
    out_type = parsed.get("suggested_output_type") or "other"
    reasoning = parsed.get("reasoning") or ""
    system_packages = parsed.get("system_packages") or []
    # sz-tcp.4 — per-tier credit suggestions (missing/negative/garbage ->
    # None; legacy single `suggested_credits` feeds `medium`). Advisory:
    # never a rejection reason, the rule floors clamp them at admission.
    tier_credits = _tier_credits(parsed)
    # A non-list requirements/system_packages is a malformed payload. Reject it
    # HERE, on the RAW parsed value — the backfill below coerces a non-list to
    # [], which would otherwise make the "must be a list" reject unreachable and
    # silently drop whatever the model emitted.
    payload_type_rejections: List[str] = []
    _raw_reqs = parsed.get("requirements")
    if _raw_reqs is not None and not isinstance(_raw_reqs, list):
        payload_type_rejections.append("requirements must be a list")
    _raw_sys = parsed.get("system_packages")
    if _raw_sys is not None and not isinstance(_raw_sys, list):
        payload_type_rejections.append(
            f"system_packages must be a list (got {type(_raw_sys).__name__})"
        )
    # Reconcile the (requirements, system_packages) pair against what the
    # script actually imports — backfill missing pip deps, move pip
    # packages the model mis-filed under system_packages, and add apt
    # runtime libs. Deterministic; the model's pinned entries always win.
    requirements, system_packages = _backfill_requirements_from_imports(
        script, requirements, system_packages
    )
    requirements = _fix_requirement_aliases(requirements)
    dockerfile_extras = parsed.get("dockerfile_extras") or ""
    if not isinstance(dockerfile_extras, str):
        dockerfile_extras = ""
    # GSEC R-100 — deterministic clone-bounds pass over the LLM's raw
    # Dockerfile lines. Unbounded `git clone` lines are rewritten to
    # shallow partial clones with LFS smudge skipped BEFORE the remaining
    # validators run, so URL/license/binary checks see the bounded text the
    # build will actually execute. History-unshallowing constructs are NOT
    # rewritable and are rejected via _validate_clone_bounds below.
    if dockerfile_extras:
        try:
            from node_builder.clone_bounds import normalize_dockerfile
            _bounded, _clone_changes, _clone_rej = normalize_dockerfile(
                dockerfile_extras
            )
            if _clone_rej:
                # Unrewritable — the validator below turns this into a
                # rejection reason; leave the text for the reason to cite.
                pass
            else:
                if _clone_changes:
                    logger.info(
                        "describe-synthesize: bounded git clone(s) in "
                        "dockerfile_extras: %s", _clone_changes,
                    )
                dockerfile_extras = _bounded
        except Exception:  # noqa: BLE001 — normalizer must never sink synth
            logger.exception(
                "describe-synthesize: clone-bounds normalization failed "
                "(passing extras through unchanged)"
            )

    # Group validators by category so the wizard can route the user
    # appropriately. License rejections re-show the candidate list
    # (a retry on the same repo will produce the same failure);
    # contract / binary / other rejections may benefit from a retry.
    other_rejections: List[str] = []
    other_rejections.extend(payload_type_rejections)
    other_rejections.extend(_validate_script(script))
    other_rejections.extend(_validate_requirements(requirements))
    other_rejections.extend(_validate_base_hint(base_hint))
    other_rejections.extend(_validate_type("suggested_input_type", in_type))
    other_rejections.extend(_validate_type("suggested_output_type", out_type))
    contract_rejections: List[str] = _validate_io_contract(
        user_input_type=user_input_type,
        user_output_type=user_output_type,
        suggested_input_type=str(in_type),
        suggested_output_type=str(out_type),
        script=script,
    )
    other_rejections.extend(_validate_system_packages(system_packages))
    other_rejections.extend(_validate_dockerfile_extras(dockerfile_extras))
    other_rejections.extend(_validate_dockerfile_extras_urls(dockerfile_extras))
    other_rejections.extend(_validate_clone_bounds(dockerfile_extras))
    # Licence policy gate FIRST so a copyleft tool surfaces "GPL via
    # X — use Y instead" (an actionable rewrite hint) rather than
    # "binary X not declared" (a hint to declare the apt package,
    # which would still violate the policy).
    license_rejections: List[str] = _validate_license_policy(
        script=script,
        system_packages=system_packages,
        dockerfile_extras=dockerfile_extras,
    )
    binary_rejections: List[str] = _validate_binaries_declared(
        script=script,
        system_packages=system_packages,
        dockerfile_extras=dockerfile_extras,
    )

    rejections = (
        other_rejections
        + contract_rejections
        + license_rejections
        + binary_rejections
    )

    if rejections:
        # Priority order for category attribution: license > contract
        # > binary > other. Licence is most actionable (pick a
        # different repo); contract is a UX hint; binary may retry;
        # other is generic.
        if license_rejections:
            category = "license"
        elif contract_rejections:
            category = "contract"
        elif binary_rejections:
            category = "binary"
        else:
            category = "other"
        return SynthesisResult(
            ok=False,
            script_content=script,
            requirements=requirements if isinstance(requirements, list) else [],
            base_image_hint=str(base_hint),
            system_packages=(
                list(system_packages)
                if isinstance(system_packages, list)
                else []
            ),
            dockerfile_extras=dockerfile_extras,
            suggested_input_type=str(in_type),
            suggested_output_type=str(out_type),
            reasoning=str(reasoning)[:300],
            **tier_credits,
            rejection_reasons=rejections,
            rejection_category=category,
        )

    return SynthesisResult(
        ok=True,
        script_content=script,
        requirements=list(requirements),
        base_image_hint=str(base_hint),
        system_packages=list(system_packages) if isinstance(system_packages, list) else [],
        dockerfile_extras=dockerfile_extras,
        suggested_input_type=str(in_type),
        suggested_output_type=str(out_type),
        reasoning=str(reasoning)[:300],
        **tier_credits,
    )
