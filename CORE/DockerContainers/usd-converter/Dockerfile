FROM python:3.11-slim

# usd-core is the official OpenUSD Python bindings (lighter than full USD)
# usd-core's .so files do NOT link to libxml2, libGL, or mesa — no apt deps needed.
# NOTE: usd-core has NO aarch64 wheel — this image is amd64-only.
RUN pip install --no-cache-dir usd-core==25.11 trimesh numpy==1.26.4 requests==2.31.0 Pillow==12.3.0

RUN mkdir -p /data/input /data/output
COPY convert.py /app/convert.py
COPY mod_node_auth.py /app/mod_node_auth.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
