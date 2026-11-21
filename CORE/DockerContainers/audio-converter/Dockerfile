# CE license note (brief 1611, empirically verified 10-14 on GPU2 against the
# real alpine:3.24 image, not Alpine's packaging policy):
#
#   Alpine's `ffmpeg` apk package is NOT LGPL-clean.
#   `ffmpeg -buildconf` inside that image reports
#     --enable-gpl --enable-version3 --enable-libx264 --enable-libx265
#   and `ldd $(which ffmpeg)` resolves libx264 + libx265 (both GPL-2.0+).
#
# So this image does NOT `apk add ffmpeg`. It builds FFmpeg from source
# with --disable-gpl --disable-nonfree; that build reports
# "License: LGPL version 2.1 or later" and links only libmp3lame/libvorbis/
# libopus (LGPL) plus FFmpeg's own native pcm/flac/aac encoders. Every codec
# convert.py selects survives, so convert.py's TARGET_FORMAT list is unchanged.

# ----- builder stage (toolchain + ffmpeg source, discarded at end) -----
FROM alpine:3.24 AS builder

RUN apk add --no-cache nasm yasm gperf pkgconf \
        gcc g++ make binutils coreutils \
        lame-dev libvorbis-dev libogg-dev opus-dev \
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
      --enable-openssl \
      --enable-shared \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /ffmpeg-9.0.2

# ----- final stage (runtime only) -----
FROM alpine:3.24

RUN apk add --no-cache lame-libs libvorbis opus openssl python3

# Runs as root on purpose: one-shot converter, no listening port, no privileged operation; it only reads /data/input and writes /data/output.
RUN mkdir -p /data/input /data/output
COPY --from=builder /usr/local /usr/local
COPY convert.py /app/convert.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
