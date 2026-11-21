#!/bin/bash
# Entry point for the tensorrt-resnet18-engine synthetic corpus node.
# NVIDIA-only by nature: tensorrt + CUDA are required; no other variant.
set -euo pipefail
exec python /app/main.py
