# Hero (cuda) Dockerfile — NVIDIA ffmpeg build so h264_nvenc exists.
# All variants share this image shape (ffmpeg present everywhere); the
# negative verdict comes from the RUN (no NVENC encoder off-NVIDIA), not
# the build.
FROM jrottenberg/ffmpeg:6.1-nvidia

WORKDIR /app
RUN mkdir -p /data/input /data/output && chmod 1777 /data/input /data/output
COPY main.sh /app/main.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
