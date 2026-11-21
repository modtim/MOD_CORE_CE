FROM python:3.11-slim

# Avoid interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Install git and ssh client (needed for repo mode with private repos)
RUN apt-get update && apt-get install -y \
    git \
    openssh-client \
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Create application directories
RUN mkdir -p /app/scripts /data/repos /data/input /data/output

# Install Python dependencies.
#
# torch is installed FIRST from TORCH_INDEX_URL (default: the CPU-only wheel
# index) so pip's resolver finds sentence-transformers' torch requirement already
# satisfied. Without this, sentence-transformers pulls the default CUDA torch
# wheels (~4 GB of nvidia-* libraries) into an image that never touches a GPU:
# the embedding model runs on CPU (CODEBASE_EMBED_DEVICE=cpu). Set
# --build-arg TORCH_INDEX_URL= (empty) to get the default PyPI torch instead.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
COPY requirements.txt /app/requirements.txt
# FIX-CONTAINERIZER-IMAGE-SLIM-SAFE-SET (PREPARED, NOT YET BUILT; plan:
# results-1110/DIAG-CONTAINERIZER-IMAGE-SIZE-LAYER-BREAKDOWN-AND-RANKED-SLIM-PLAN-D75.md
# ranks 1-2): the deletions MUST sit in the SAME RUN as the installs, otherwise the
# pip layer still carries the bytes. huggingface_hub is installed here too (moved
# from the bake RUN below) so pip's own tree can be removed in this same layer;
# nothing in scripts/ or entrypoint.sh runs pip at run time. Removed: torch/test,
# torch/include (C++ headers), pip, and every directory named `tests` under
# site-packages (about 300 MB, identical embedding output proved on the Dell).
# NOT removed (their absence breaks `import torch`): torchgen, sympy, torch/
# distributed, torch/_dynamo, torch/_inductor, torch/testing. __pycache__ is KEPT
# (deleting it costs about +5 s cold start per task container, plan rank 3).
RUN if [ -n "$TORCH_INDEX_URL" ]; then \
        pip install --no-cache-dir torch --index-url "$TORCH_INDEX_URL"; \
    fi \
 && pip install --no-cache-dir -r /app/requirements.txt \
 && pip install --no-cache-dir huggingface_hub \
 && P=/usr/local/lib/python3.11/site-packages \
 && rm -rf "$P/torch/test" "$P/torch/include" "$P/pip" "$P"/pip-*.dist-info \
 && find "$P" -type d -name tests -prune -exec rm -rf {} +

# Bake the embedding model into the image at build time so no network is needed
# at runtime. sentence-transformers loads from $CODEBASE_EMBED_BAKED_DIR/<model_name>
# which resolves to /opt/embed-models/snowflake-arctic-embed-s when baked.
RUN python3 -c 'from huggingface_hub import snapshot_download; snapshot_download(repo_id="Snowflake/snowflake-arctic-embed-s", revision="e596f507467533e48a2e17c007f0e1dacc837b33", local_dir="/opt/embed-models/snowflake-arctic-embed-s", allow_patterns=["config.json", "config_sentence_transformers.json", "model.safetensors", "modules.json", "sentence_bert_config.json", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt", "1_Pooling/*"])' \
 && chmod -R a+r /opt/embed-models/snowflake-arctic-embed-s
ENV CODEBASE_EMBED_BAKED_DIR=/opt/embed-models

# Copy application scripts
COPY scripts/ /app/scripts/

# Copy entrypoint script
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && \
    test -x /app/entrypoint.sh || (echo "Entrypoint not executable!" && exit 1)

# Set working directory
WORKDIR /data

# Entrypoint - use shell form to ensure bash processes it
ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
