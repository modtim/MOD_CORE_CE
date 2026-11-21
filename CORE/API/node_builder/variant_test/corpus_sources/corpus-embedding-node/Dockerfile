# Hero (cpu) Dockerfile for corpus-embedding-node (corpus synthetic
# syn-embedding). CPU is the verified origin here; GPU variants (cuda pass,
# rocm/intel/npu spec) reuse this image shape with a GPU base when planned.
FROM python:3.11-slim

WORKDIR /app
RUN mkdir -p /data/input /data/output && chmod 1777 /data/input /data/output
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY main.py /app/main.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
