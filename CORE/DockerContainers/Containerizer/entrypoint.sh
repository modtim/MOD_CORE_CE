#!/bin/bash
set -e
set -x  # Enable debug tracing

echo "=== Containerizer - Dockerfile Generator ===" >&2
echo "Workspace: /data" >&2

# llm-ping mode short-circuits past every other gate. It doesn't
# need input files, it doesn't need an output dir — it just curls
# the LLM's /api/tags endpoint and prints a diagnostic JSON blob.
# Used by POST /node_definitions/worker-llm-ping.
if [ "${OUTPUT_MODE:-}" = "llm-ping" ]; then
    exec python /app/scripts/app.py
fi

# Materialize the wizard's inline submission onto /data/input/ BEFORE
# the HAS_INPUT check. The API dispatch path stuffs the user's script
# or Dockerfile body into INLINE_SOURCE_BODY (+ INLINE_SOURCE_FILENAME
# for the filename hint) rather than staging files to MinIO or a
# shared volume. Without this materialization the entrypoint fails
# with "No input source found" even though the wizard sent the code.
if [ -n "$INLINE_SOURCE_BODY" ]; then
    mkdir -p /data/input
    FILENAME="${INLINE_SOURCE_FILENAME:-source.txt}"
    # Sanitize — filename must be a single basename, not a path.
    FILENAME=$(basename -- "$FILENAME")
    printf '%s' "$INLINE_SOURCE_BODY" > "/data/input/$FILENAME"
    echo "Materialized inline submission -> /data/input/$FILENAME" >&2
fi

# Validate that at least one input source exists
HAS_INPUT=false

if [ -n "$REPO_URL" ]; then
    echo "Input:    repo ($REPO_URL)" >&2
    HAS_INPUT=true
elif [ -n "$SOURCE_DIR" ]; then
    echo "Input:    directory ($SOURCE_DIR)" >&2
    HAS_INPUT=true
elif [ -n "$WRAP_BASE_IMAGE" ]; then
    # Wizard's image source-type — user pasted a pre-built image
    # they want wrapped as a node. app.py runs in wrap_image mode
    # and emits a Dockerfile that FROMs the chosen image plus a
    # thin entrypoint adapter; no source files are needed.
    echo "Input:    wrap base image ($WRAP_BASE_IMAGE)" >&2
    HAS_INPUT=true
elif [ -d "/data/input" ] && [ "$(ls -A /data/input 2>/dev/null)" ]; then
    echo "Input:    auto-detected files in /data/input/" >&2
    HAS_INPUT=true
fi

if [ "$HAS_INPUT" = false ]; then
    echo "ERROR: No input source found." >&2
    echo "" >&2
    echo "Provide one of:" >&2
    echo "  REPO_URL=https://github.com/user/repo   (clone a git repo)" >&2
    echo "  SOURCE_DIR=/path/to/folder               (local directory)" >&2
    echo "  WRAP_BASE_IMAGE=image:tag               (wrap a pre-built image)" >&2
    echo "  Mount files to /data/input/               (auto-detected)" >&2
    echo "  INLINE_SOURCE_BODY=<content>             (inline script/Dockerfile)" >&2
    # Contract JSON on stdout (CORE/scripts/vet-container.sh fail-closed smoke).
    echo '{"error":"No input source found","code":"NO_INPUT"}'
    exit 1
fi

if [ -z "$LLM_PROVIDER" ]; then
    echo "ERROR: LLM_PROVIDER environment variable is required." >&2
    echo "Supported: openai, anthropic, google, ollama, groq, mistral, deepseek, openai_compatible" >&2
    echo '{"error":"LLM_PROVIDER environment variable is required","code":"BAD_PARAM"}'
    exit 1
fi

echo "Provider: $LLM_PROVIDER / $LLM_MODEL" >&2

# Ensure output dir exists
OUTPUT_DIR="${OUTPUT_DIR:-/data/output}"
mkdir -p "$OUTPUT_DIR"
echo "Output:   $OUTPUT_DIR" >&2

echo "" >&2

# Run the Python CLI
exec python /app/scripts/app.py
