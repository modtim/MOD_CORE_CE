# Curated MOD base image — AMD ROCm 6.1 + Python 3.11
FROM rocm/dev-ubuntu-22.04:6.1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    apt-get -y --no-install-recommends install \
        python3.11 \
        python3.11-venv \
        python3-pip \
        ca-certificates \
        tini \
        hipify-clang \
        git; \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1; \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --upgrade pip wheel setuptools; \
    groupadd --system --gid 10001 mod; \
    useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

# MOD GPU VRAM cap: auto-installed torch import hook (megaplan 14.5).
# No-ops unless MOD_GPU_VRAM_LIMIT_MB + MOD_GPU_CARD_VRAM_MB are set and
# torch.cuda.is_available(); ROCm torch uses the same API.
COPY sitecustomize.py /opt/mod_gpu_cap/sitecustomize.py
ENV PYTHONPATH=/opt/mod_gpu_cap${PYTHONPATH:+:$PYTHONPATH}

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
