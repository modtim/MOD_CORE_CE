# Curated MOD base image — Node 20 LTS
FROM node:20-bookworm-slim

ENV NODE_ENV=production

RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    apt-get -y --no-install-recommends install ca-certificates tini; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    # `npm@latest` unpinned is a time bomb: npm 12 requires node >= 22.22,
    # so this line began failing on Node 20 the day npm 12 published — on
    # EVERY architecture, not just arm64. The published image predates that,
    # so the breakage stayed invisible until something tried to rebuild. If
    # the nightly rebuild had been running against this tag it would have been
    # failing silently. Pinned to the newest npm this runtime supports.
    npm i -g npm@^10; \
    groupadd --system --gid 10001 mod; \
    useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
