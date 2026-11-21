# Curated MOD base images

Platform-wide floor that tenant Dockerfile/Git/Script-mode nodes must
`FROM` (enforced by `CORE/API/utils/node_admission.py` against
`CORE/API/config/base_image_allowlist.json`). Each base:

1. `FROM` a pinned upstream vendor tag.
2. `apt-get upgrade` / `apk upgrade` baked in at build time so security
   patches land nightly via the rebuild DAG (Layer 4), not per-tenant.
3. Ends with `USER mod` (uid `10001`), never root.
4. No `HEALTHCHECK` — tenant nodes declare their own.
5. Standard working directory `/app`, input `/data/input/`, output
   `/data/output/`.

## Variants

| Dir                            | Image ref                                                            | Purpose                      |
|--------------------------------|----------------------------------------------------------------------|------------------------------|
| `python-3.11`                  | `ghcr.io/modtim/base-python:3.11`                   | Default Python workloads     |
| `python-3.12`                  | `ghcr.io/modtim/base-python:3.12`                   | Python 3.12                  |
| `node-20`                      | `ghcr.io/modtim/base-node:20`                       | Node 20 (LTS)                |
| `node-22`                      | `ghcr.io/modtim/base-node:22`                       | Node 22                      |
| `go-1.22`                      | `ghcr.io/modtim/base-go:1.22`                       | Go toolchain                 |
| `rust-1.78`                    | `ghcr.io/modtim/base-rust:1.78`                     | Rust toolchain               |
| `java-21`                      | `ghcr.io/modtim/base-java:21`                       | JDK 21                       |
| `cuda-12.4-python3.11`         | `ghcr.io/modtim/base-cuda:12.4-python3.11`          | NVIDIA CUDA + Python         |
| `rocm-6.1-python3.11`          | `ghcr.io/modtim/base-rocm:6.1-python3.11`           | AMD ROCm + Python            |
| `intel-xpu-2025.0`             | `ghcr.io/modtim/base-intel-xpu:2025.0`              | Intel oneAPI / Level Zero    |
| `npu-openvino-2025`            | `ghcr.io/modtim/base-npu:openvino-2025`             | OpenVINO NPU runtime         |
| `alpine-3.19`                  | `ghcr.io/modtim/base-alpine:3.19`                   | Minimal (shell scripts)      |
| `distroless-python-3.11`       | `ghcr.io/modtim/base-distroless-python:3.11`        | Minimal Python (no apt)      |
| `distroless-node-20`           | `ghcr.io/modtim/base-distroless-node:20`            | Minimal Node (no apt)        |

## Nightly rebuild (Layer 4)

the nightly base-image rebuild job rebuilds each of these and republishes it under the same version tag to the CE release registry (`ghcr.io/modtim`, via `open-core/build_node_images.sh --base-images`; see `PUBLISHED_IMAGES`). CE has no Harbor and no `base_image_bumped` webhook - downstream tenant images rebuild on their own schedule.
