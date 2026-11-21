#!/bin/bash
# Entry point for the jax-sobel-filter synthetic corpus node.
# The installed JAX distribution (cpu / cuda / rocm) determines the backend;
# no env selection needed at run time.
set -euo pipefail
exec python /app/main.py
