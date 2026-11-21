# Hero (cuda, the ONLY variant) Dockerfile for tensorrt-resnet18-engine
# (corpus synthetic 20). NVIDIA-only by nature — no other variant exists.
# The base image ships TensorRT 8.6 + CUDA 12.1 + torch 2.0.x.
FROM nvcr.io/nvidia/tensorrt:22.12-py3

WORKDIR /app
RUN mkdir -p /data/input /data/output && chmod 1777 /data/input /data/output
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY main.py /app/main.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
