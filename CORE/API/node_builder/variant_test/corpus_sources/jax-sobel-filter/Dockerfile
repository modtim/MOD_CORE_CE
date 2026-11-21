# Hero (cpu) Dockerfile for jax-sobel-filter (corpus synthetic 19).
# Variant Dockerfiles swap base image + JAX distribution (node_config.json).
FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /data/input /data/output && chmod 1777 /data/input /data/output
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
RUN pip install --no-cache-dir "jax[cpu]==0.4.30"
COPY main.py /app/main.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
