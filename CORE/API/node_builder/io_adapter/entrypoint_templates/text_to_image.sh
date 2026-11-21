#!/bin/bash
# Text→Image entrypoint for Image-mode adapter.
# Designed for diffusion-class images (stable-diffusion, flux, etc.)
# that expose the model via the HuggingFace `diffusers` library.
# Falls back gracefully if the image's Python env doesn't have
# diffusers installed.
set -euo pipefail

INPUT_DIR=/data/input
OUTPUT_DIR=/data/output
mkdir -p "$OUTPUT_DIR"

# Find the prompt. We accept three input shapes:
#   1. A single .txt file: the file's contents IS the prompt.
#   2. A .json file with a "prompt" field.
#   3. Stdin (rare; supported for compatibility with workflow nodes
#      that pipe directly into a containerized step).
PROMPT=""
if compgen -G "$INPUT_DIR/*.txt" > /dev/null; then
    PROMPT="$(cat "$(ls "$INPUT_DIR"/*.txt | head -n 1)")"
elif compgen -G "$INPUT_DIR/*.json" > /dev/null; then
    PROMPT="$(python3 -c "import json,sys; print(json.load(open('$(ls "$INPUT_DIR"/*.json | head -n 1)'))['prompt'])")"
elif [ ! -t 0 ]; then
    PROMPT="$(cat)"
fi

if [ -z "$PROMPT" ]; then
    echo "ERROR: no prompt found in $INPUT_DIR (expected .txt, .json with .prompt, or stdin)" >&2
    exit 1
fi

echo "Prompt: $PROMPT" >&2

# Invoke diffusers. We embed the image_ref as the model identifier
# so the pipeline can fall back to downloading weights at runtime if
# they're not pre-baked into the image's filesystem at /model or the
# default HF cache location.
python3 <<PY
import os, sys, traceback
try:
    from diffusers import DiffusionPipeline
    import torch
except Exception as e:
    print(f"ERROR: diffusers/torch not available in this image: {e}", file=sys.stderr)
    print("Hint: re-run the wizard in Script mode and pip-install diffusers explicitly.", file=sys.stderr)
    sys.exit(1)

prompt = """$PROMPT"""
model_id = "{{ image_ref }}"

# Prefer locally pre-baked weights if present; else HF Hub.
local_candidates = ["/model", "/app/model", os.path.expanduser("~/.cache/huggingface/hub")]
local = next((p for p in local_candidates if os.path.isdir(p) and os.listdir(p)), None)
src = local or model_id
print(f"Loading pipeline from: {src}", file=sys.stderr)

dtype = torch.float16 if torch.cuda.is_available() else torch.float32
device = "cuda" if torch.cuda.is_available() else "cpu"

try:
    pipe = DiffusionPipeline.from_pretrained(src, torch_dtype=dtype)
    pipe = pipe.to(device)
    image = pipe(prompt).images[0]
    out_path = "/data/output/output.png"
    image.save(out_path)
    print(f"Wrote: {out_path}", file=sys.stderr)
except Exception as e:
    traceback.print_exc()
    sys.exit(1)
PY
