#!/bin/bash
# Entry point for the onnx-multi-ep-classifier synthetic corpus node.
# MOD_EP is set per variant by the generated variant Dockerfile (node_config
# variant_env). Runs the classifier; exits non-zero on any failure.
set -euo pipefail
exec python /app/main.py
