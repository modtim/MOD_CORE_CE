# CE license note (brief 1701, empirically verified against the real alpine:3.24
# image, not Alpine's packaging policy):
#
#   Alpine's `ffmpeg` apk package is NOT LGPL-clean. `ffmpeg -buildconf` inside
#   that image reports --enable-gpl --enable-version3 --enable-libx264
#   --enable-libx265 and `ldd $(which ffmpeg)` resolves libx264 + libx265
#   (both GPL-2.0+). Owner decision 1A: "CE has no H.264; VP9+AAC fallback".
#
# So this image does NOT `apk add ffmpeg`. It builds FFmpeg 9.0.2 from the
# unmodified upstream tarball with --disable-gpl --disable-nonfree and
# --enable-libvpx (BSD VP8/VP9 encoder). That build reports
# "License: LGPL version 2.1 or later", links only libmp3lame (LGPL),
# libvorbis/libopus/libvpx (BSD) plus FFmpeg's own native encoders, and has
# no x264/x265 of any kind. convert.py probes for an H.264 encoder at runtime
# and falls back to VP9 + FFmpeg-native AAC when there is none, so the node
# keeps working in CE and would still use H.264 in a future build.

# ----- builder stage (toolchain + ffmpeg source, discarded at end) -----
FROM alpine:3.24 AS builder

RUN apk add --no-cache nasm yasm gperf pkgconf \
        gcc g++ make binutils coreutils \
        lame-dev libvorbis-dev libogg-dev opus-dev libvpx-dev \
        openssl-dev xz

ADD https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz /tmp/ffmpeg-9.0.2.tar.xz
RUN echo "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e  /tmp/ffmpeg-9.0.2.tar.xz" | sha256sum -c -

RUN tar xfJ /tmp/ffmpeg-9.0.2.tar.xz \
 && rm -f /tmp/ffmpeg-9.0.2.tar.xz \
 && cd ffmpeg-9.0.2 \
 && ./configure \
      --prefix=/usr/local \
      --disable-gpl \
      --disable-nonfree \
      --disable-doc \
      --enable-libmp3lame \
      --enable-libvorbis \
      --enable-libopus \
      --enable-libvpx \
      --enable-openssl \
      --enable-shared \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /ffmpeg-9.0.2

# ----- final stage (runtime only) -----
FROM alpine:3.24

# Shared libs the source-built ffmpeg links against + python3. Deliberately NO
# libx264/libx265 package here: nothing GPL-encumbered ships in this image.
RUN apk add --no-cache lame-libs libvorbis opus libvpx openssl python3

RUN mkdir -p /data/input /data/output
COPY --from=builder /usr/local /usr/local
COPY convert.py /app/convert.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
