FROM python:3.11-slim

# imageio[freeimage] provides the FreeImage plugin which reads EXR / HDR
# even when the primary OpenEXR python module can't (e.g. malformed
# header). Without this the fallback path
# (`import imageio; imageio.imread(...)`) errors with
# "Could not find a backend to open <path>.exr".
RUN pip install --no-cache-dir Pillow numpy imageio[ffmpeg] imageio[freeimage] OpenEXR \
 && python3 -c "import imageio; imageio.plugins.freeimage.download()"

RUN mkdir -p /data/input /data/output
COPY convert.py /app/convert.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
