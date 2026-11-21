# jax-sobel-filter (corpus synthetic, manifest entry 19)

Sobel edge detection with JAX. `main.py` loads the first image in
`/data/input`, applies the 3x3 Sobel operator on the installed JAX backend
(CUDA / ROCm / CPU — the backend follows the per-variant JAX distribution;
nothing is selected at run time), and writes the grayscale edge map to
`/data/output/edges.png` (image comparer, pass-tol on rocm).

The per-variant JAX distribution is pinned in `node_config.json`
(`variant_pip`) and the variant Dockerfiles — plain `jax[cpu]` for
cpu/intel, `jax-cuda12-py311` on the CUDA runtime, `jax[rocm]` on the ROCm
dev image. No model is involved; no download.
