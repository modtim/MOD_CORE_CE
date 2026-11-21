#!/usr/bin/env python3
"""jax-sobel-filter — corpus synthetic node (manifest entry 19).

Sobel edge detection on the first image in /data/input, computed with JAX
on the local accelerator (JAX picks the backend from the installed
distribution — CUDA/ROCm/CPU; nothing to select at run time). No model
download: only the fixed 3x3 Sobel kernels, defined in-process.
Output: /data/output/edges.png (grayscale edge map).
"""
import glob
import os
import sys

import jax
import jax.numpy as jnp
from jax.scipy.signal import convolve2d
from PIL import Image

IN_DIR = "/data/input"
OUT_DIR = "/data/output"


def sobel(gray: jnp.ndarray) -> jnp.ndarray:
    gx = jnp.array([[1.0, 0.0, -1.0], [2.0, 0.0, -2.0], [1.0, 0.0, -1.0]])
    gy = gx.T
    g = gray[None, :, :]
    mag = jnp.sqrt(convolve2d(g, gx, mode="same") ** 2
                   + convolve2d(g, gy, mode="same") ** 2)
    return jnp.clip(mag[0], 0.0, 255.0)


def main() -> int:
    print("jax devices:", [str(d) for d in jax.devices()])
    srcs = sorted(glob.glob(os.path.join(IN_DIR, "*")))
    if not srcs:
        print("no input file in /data/input", file=sys.stderr)
        return 1
    img = jnp.asarray(Image.open(srcs[0]).convert("L").resize((256, 256)),
                      jnp.float32)
    edges = sobel(img)
    out = Image.fromarray(jax.device_get(edges).astype("uint8"), mode="L")
    os.makedirs(OUT_DIR, exist_ok=True)
    out.save(os.path.join(OUT_DIR, "edges.png"))
    print("sobel edges written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
