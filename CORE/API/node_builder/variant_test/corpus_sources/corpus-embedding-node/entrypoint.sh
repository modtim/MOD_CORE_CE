#!/bin/bash
# Entry point for the corpus-embedding-node synthetic corpus node.
# The sentence-transformer model is pinned (node_config.json) and cached in
# /tmp — loaded lazily, nothing at import time.
set -euo pipefail
exec python /app/main.py
