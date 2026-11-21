# Curated MOD base image — Python 3.11
# Built nightly by the base-image-rebuild scheduler job; patches land here so
# every tenant's next rebuild inherits fixes.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Apply upstream security updates, install only the minimum system
# packages most Python nodes will want, then harden.
RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    apt-get -y --no-install-recommends install \
        ca-certificates \
        git \
        tini; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --upgrade pip wheel setuptools; \
    groupadd --system --gid 10001 mod; \
    useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
