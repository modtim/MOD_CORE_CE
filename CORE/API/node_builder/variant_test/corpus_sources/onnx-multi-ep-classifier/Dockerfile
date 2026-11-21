# Hero (cpu) Dockerfile for onnx-multi-ep-classifier (corpus synthetic 11).
# Variant Dockerfiles (cuda/rocm/intel) swap the base image and the
# onnxruntime package (see node_config.json variant_base / variant_pip).
FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /data/input /data/output && chmod 1777 /data/input /data/output
RUN pip install --no-cache-dir onnxruntime==1.18.1
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY main.py /app/main.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENV MOD_EP=cpu
ENTRYPOINT ["/entrypoint.sh"]
