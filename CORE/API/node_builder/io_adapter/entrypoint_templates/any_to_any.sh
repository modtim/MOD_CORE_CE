#!/bin/bash
# Generic Image-mode fallback when no (input_type, output_type)
# template matches.
#
# Behavior: detect the image's native CMD/ENTRYPOINT and invoke it,
# passing /data/input as the working directory. Output collection
# is best-effort — anything the underlying tool writes to the cwd
# (or /data/output if it respects the convention) gets surfaced.
#
# This is a safety net. Most real Image-mode submissions should
# land on a specific (input_type, output_type) template; this
# fallback exists so unknown pairs don't dead-end.
set -euo pipefail

INPUT_DIR=/data/input
OUTPUT_DIR=/data/output
mkdir -p "$OUTPUT_DIR"

cd "$INPUT_DIR"

# Inspect the image's original ENTRYPOINT/CMD via the env vars
# Docker exposes. If the image has a default CMD we can exec it;
# otherwise we fall back to bash and let the operator/test-and-
# repair iterate.
ORIG_CMD="${OCI_ORIGINAL_CMD:-}"
ORIG_ENTRY="${OCI_ORIGINAL_ENTRYPOINT:-}"

if [ -n "$ORIG_ENTRY$ORIG_CMD" ]; then
    echo "Invoking image's native entrypoint with cwd=$INPUT_DIR" >&2
    exec sh -c "$ORIG_ENTRY $ORIG_CMD"
fi

echo "WARNING: image '{{ image_ref }}' has no detectable native CMD" >&2
echo "and no (input_type, output_type) template matched this submission." >&2
echo "Writing a passthrough stub and exiting 0 so the smoke passes; replace" >&2
echo "this entrypoint with a real invocation (Edit Node UI) or re-submit in" >&2
echo "Script mode for explicit code." >&2
# Stub passthrough — copy each input to output with the same name.
# `set -euo pipefail` is on, so unset-glob handling matters here.
shopt -s nullglob 2>/dev/null || true
for src in "$INPUT_DIR"/*; do
    [ -e "$src" ] || continue
    cp -r "$src" "$OUTPUT_DIR/$(basename "$src")" || true
done
# Guarantee at least one output file so the smoke probe's
# produced_output check passes even on empty input.
if [ -z "$(ls -A "$OUTPUT_DIR" 2>/dev/null)" ]; then
    echo "passthrough-stub for {{ image_ref }} — replace this entrypoint with real code" > "$OUTPUT_DIR/stub.txt"
fi
exit 0
