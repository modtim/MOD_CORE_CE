#!/usr/bin/env python3
"""corpus-embedding-node — corpus synthetic node (manifest syn-embedding).

Sentence-embedding node: reads the text in the first file of /data/input,
embeds it with the tiny pinned model ``all-MiniLM-L6-v2`` (384-dim), and
writes /data/output/embedding.json ({"text": ..., "embedding": [...]}).
LEAD DECISIONS item 3 exists so the corpus exercises the embedding/cosine
comparer path (no real node emits output_type: embedding).

The model is loaded LAZILY in main() — nothing downloads at import time.
On a cold cache the model (~90 MB) is fetched once from the HuggingFace Hub
and cached in /tmp; with the cache primed the node runs fully offline.
"""
import glob
import json
import os
import sys

IN_DIR = "/data/input"
OUT_DIR = "/data/output"
CACHE_DIR = "/tmp/corpus-embedding-cache"
# Pinned small model (see node_config.json / requirements.txt).
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def load_embedder():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(MODEL_NAME, cache_folder=CACHE_DIR)


def main() -> int:
    srcs = sorted(glob.glob(os.path.join(IN_DIR, "*")))
    if not srcs:
        print("no input file in /data/input", file=sys.stderr)
        return 1
    with open(srcs[0], "r", encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        print("input file is empty", file=sys.stderr)
        return 1

    embedder = load_embedder()
    vec = embedder.encode([text], normalize_embeddings=True)[0]
    result = {"text": text, "model": MODEL_NAME,
              "embedding": [float(v) for v in vec]}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "embedding.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    print(f"embedded {len(text)} chars -> {len(vec)}-dim vector")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
