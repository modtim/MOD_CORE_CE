# Curated MOD base image — Intel oneAPI / Level Zero 2025.3
#
# P9d: bumped from 2025.0.0 → 2025.3.2 (last dpct release before
# Intel's post-2025.3 deprecation; Phase 9j migrates to open-source
# SYCLomatic without a Dockerfile change — porter interface already
# decoupled). Adds:
#   - /usr/local/bin/dpct symlink so kernel_porter's subprocess call
#     doesn't have to know the exact /opt/intel/oneapi path.
#   - intel_extension_for_pytorch pip wheel so CUDAExtension ported
#     to DPCPPExtension imports cleanly at extension-build time
#     inside the node's image, no extra pip install required.
FROM intel/oneapi-basekit:2025.3.2-0-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    # FIX 11-19: do NOT apt-install intel-level-zero-gpu.
    # The pinned oneapi basekit already ships the NEO compute runtime
    # (libze-intel-gpu1 25.18.x + libze1 + libigc2/libigdfcl2, all newer
    # than the repo's intel-level-zero-gpu 1.3.30049.10-950). The repo
    # build of intel-level-zero-gpu Depends: libigc1 (>= 1.0.12812), and
    # apt aborts with `libigc2 : Conflicts: libigc1 ...` ->
    # `pkgProblemResolver::Resolve generated breaks` -> exit 100.
    # level-zero (the Level Zero loader) installs cleanly and is kept.
    apt-get -y --no-install-recommends install \
        python3.11 \
        python3-pip \
        ca-certificates \
        tini \
        level-zero; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --upgrade pip wheel setuptools; \
    # intel_extension_for_pytorch provides DPCPPExtension /
    # DpcppBuildExtension — the SYCL equivalents of CUDAExtension /
    # BuildExtension. Baked into the base so deep-CUDA repos ported
    # to Intel don't need a second pip install round-trip at
    # node-build time.
    pip install --no-cache-dir intel_extension_for_pytorch; \
    # dpct binary is the DPC++ Compatibility Tool shipped by the
    # oneAPI basekit. Its install path is version-coupled
    # (/opt/intel/oneapi/dpcpp-ct/latest/bin/dpct); symlink onto
    # /usr/local/bin/dpct so kernel_porter.DpctTranslator can just
    # call `dpct` without version-aware path logic.
    ln -sf /opt/intel/oneapi/dpcpp-ct/latest/bin/dpct /usr/local/bin/dpct; \
    groupadd --system --gid 10001 mod; \
    useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

# MOD GPU VRAM cap: auto-installed torch import hook (megaplan 14.5).
# No-ops unless MOD_GPU_VRAM_LIMIT_MB + MOD_GPU_CARD_VRAM_MB are set and
# torch.cuda.is_available(); XPU/CPU torch simply reports no CUDA.
COPY sitecustomize.py /opt/mod_gpu_cap/sitecustomize.py
ENV PYTHONPATH=/opt/mod_gpu_cap${PYTHONPATH:+:$PYTHONPATH}

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
