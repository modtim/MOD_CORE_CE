FROM python:3.11-slim

RUN pip install --no-cache-dir "trimesh[easy]" "numpy>=1.24.0"

RUN mkdir -p /data/input /data/output
COPY convert.py /app/convert.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
