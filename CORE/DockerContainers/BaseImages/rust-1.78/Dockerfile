# Curated MOD base image — Rust 1.78
FROM rust:1.78-bookworm

RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    apt-get -y --no-install-recommends install ca-certificates tini; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --system --gid 10001 mod; \
    useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
