# corpus-embedding-node (corpus synthetic, manifest syn-embedding)

LEAD DECISIONS item 3: a tiny sentence-embedding node so the corpus
exercises the embedding/cosine comparer path (no real node emits
`output_type: embedding`). The model is pinned in `node_config.json` +
requirements: `sentence-transformers/all-MiniLM-L6-v2` (384-dim). It is
loaded LAZILY in `main()` — nothing downloads at import time — and cached in
`/tmp/corpus-embedding-cache`, so a primed cache runs the node fully
offline.

`main.py` reads the first file in `/data/input` as text and writes
`/data/output/embedding.json` (`{"text", "model", "embedding"}`; embedding
comparer). Expected verdicts: cuda pass (torch CUDA build), cpu pass,
rocm/intel/npu spec (probe decides).
