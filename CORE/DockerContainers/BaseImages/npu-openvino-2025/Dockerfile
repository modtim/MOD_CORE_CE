# Curated MOD base image — OpenVINO NPU 2025 (dev flavor)
#
# P9c: switched from ubuntu22_runtime → ubuntu22_dev so openvino-dev
# + model conversion tools are available at BUILD time. The wizard's
# NPU porter needs the `ovc` CLI + `openvino.tools.mo.convert_model`
# to turn torch.onnx.export outputs into OpenVINO IR during
# `docker build`. Runtime-only image lacked these and forced us to
# layer openvino-dev via pip on every node build — slower and
# failure-prone on networks with slow PyPI.
FROM openvino/ubuntu22_dev:2025.0.0

ENV DEBIAN_FRONTEND=noninteractive

USER root
RUN set -eux; \
    apt-get update; \
    apt-get -y --no-install-recommends upgrade; \
    apt-get -y --no-install-recommends install \
        python3-pip \
        ca-certificates \
        tini; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    pip install --upgrade pip wheel setuptools; \
    getent group mod  >/dev/null 2>&1 || groupadd --system --gid 10001 mod; \
    id mod             >/dev/null 2>&1 || useradd  --system --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /sbin/nologin mod; \
    mkdir -p /app /data/input /data/output; \
    chown -R mod:mod /app /data

WORKDIR /app
USER mod

ENTRYPOINT ["/usr/bin/tini", "--"]
