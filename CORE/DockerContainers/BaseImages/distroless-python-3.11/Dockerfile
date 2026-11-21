# Curated MOD base image — Distroless Python 3.11
# No apt/apk, no shell — minimal attack surface for nodes that don't
# need package-manager access at runtime (most statically-packaged
# Python inference nodes fit here).
FROM gcr.io/distroless/python3-debian12:nonroot

# Distroless ships as uid 65532 (nonroot). We keep that uid — no way to
# useradd here — but alias /app /data so the contract matches the
# apt-based bases.
WORKDIR /app

# Entrypoint is the Python interpreter by default; tenants that need
# their own entrypoint can ENTRYPOINT in their Dockerfile.
