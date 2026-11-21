# CE license note (brief 1610): rebuilt from source with --disable-gpl
# --disable-nonfree to drop libx264/libx265/GPL obligation. Codec stack:
#   libvpx       (BSD, VP8/VP9)  libmp3lame (LGPL)  libvorbis (BSD)
#   libopus  (BSD)
#
# Brief 1666b: multi-stage (mirrors audio-converter / 1666). The toolchain and
# the -dev headers live only in the builder stage and are discarded, so the
# shipped image carries just the runtime shared libs. Same sha256 pin, same
# configure flags, same enabled libs -> still LGPL-only.

# ----- builder stage (toolchain + ffmpeg source, discarded at end) -----
FROM alpine:3.24 AS builder

RUN apk add --no-cache nasm yasm gperf pkgconf \
        gcc g++ make binutils coreutils \
        lame-dev libvorbis-dev libogg-dev opus-dev \
        libvpx-dev openssl-dev xz

# FFmpeg 9.0.2, pinned by sha256 (never a floating "latest" download).
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
      --enable-static \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /ffmpeg-9.0.2

# ----- final stage (runtime only: no toolchain, no -dev headers) -----
FROM alpine:3.24

# Runtime shared libs for the enabled codecs (LGPL/BSD only).
RUN apk add --no-cache lame-libs libvorbis libogg opus libvpx openssl bash

# Only the two binaries cross the stage boundary; the builder's static .a libs
# and headers stay behind. The runtime apk libs above satisfy the dynamic links.
COPY --from=builder /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/

RUN mkdir -p /data/input /data/output
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
