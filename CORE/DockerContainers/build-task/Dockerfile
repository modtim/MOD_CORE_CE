# build-task — δ DAG-wizard B4 multi-stage entrypoint container.
# One image, dispatched per-stage via STAGE env var: build_variant /
# smoke_variant / push_variant / sample_test / register_node.
FROM python:3.11-slim

# Docker CLI for the build/smoke stages + skopeo for the push stage.
#
# curl is only needed to fetch the Docker apt key, so it (and libcurl) is purged at the
# end of that RUN: nothing in /app/stages shells out to it, and a build container
# running LLM-written Dockerfiles does not need a general-purpose HTTP client in it.
#
# Why skopeo instead of `docker push`: docker push against tenant-
# scoped Harbor repos hangs all blob uploads in "Waiting" then
# errors with `blob sha256:<X> not found` (different blob per
# attempt). Reproduced on docker 29.4.0 + Harbor 2.x with both
# tenant-robot and admin creds — almost certainly a docker push
# bug in cross-repo blob-mount handling.
#
# Why skopeo instead of crane (previous fix): crane works correctly
# but requires a tarball input (`crane push <tarball> <ref>`). For
# TRELLIS-class images (9-32 GB per variant) on 4-variant concurrent
# pushes that's ~87 GB of intermediate tarballs into the build-task
# container's /tmp, which exhausts the host's docker storage and
# trips "no space left on device" mid-write. skopeo's
# `docker-daemon:` transport streams blobs directly from the host
# daemon to the registry — no intermediate file, no /tmp pressure,
# concurrent-push-safe.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg skopeo \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
 && apt-get purge -y --auto-remove curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh /app/entrypoint.sh
COPY stages/ /app/stages/
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
