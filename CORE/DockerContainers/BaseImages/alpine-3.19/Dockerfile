# Curated MOD base image — Alpine 3.19 (minimal shell/bash environment)
FROM alpine:3.19

RUN set -eux; \
    apk update; \
    apk upgrade --no-cache; \
    apk add --no-cache \
        ca-certificates \
        bash \
        tini; \
    addgroup -S -g 10001 mod; \
    adduser  -S -u 10001 -G mod -h /app -s /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

WORKDIR /app
USER mod

ENTRYPOINT ["/sbin/tini", "--"]
