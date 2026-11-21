# CE license note (brief 1724, mirrors the 1701 video-converter finding):
#
#   This image used to be `FROM python:3.11-slim` + `apt-get install ffmpeg`.
#   That Debian base fails three CE vet steps:
#     step 4 (vuln): grype reports CRITICALs inherited from Debian trixie -
#       apt ffmpeg CVE-2026-75143, libtiff6 CVE-2026-52490, libxml2
#       CVE-2026-6653. `apt-get upgrade` does NOT clear them (they are
#       "won't fix" in the tracker) and we never add grype ignores.
#     step 6 (license): Debian's ffmpeg is built with --enable-gpl, so
#       `ffmpeg -buildconf` trips the GPL gate.
#
# Owner decision 1A: CE ships NO GPL and NO H.264. So this image does not
# install any distro ffmpeg. Exactly like video-converter (1701, c7eac9464)
# and audio-converter, it builds FFmpeg 9.0.2 from the unmodified upstream
# tarball (pinned by sha256) in an alpine:3.24 builder stage with
# --disable-gpl --disable-nonfree, enabling only the image codecs and filters
# this converter needs (png/jpeg/webp/tiff/bmp/gif + scale). The final stage is
# Alpine, so no Debian userspace at all ships here: none of the Debian CRITICAL
# packages (ffmpeg, libtiff6, libxml2, libcurl4t64) is present. convert.py
# keeps Pillow as its fallback path, so the node still works when this LGPL
# build has no encoder for an exotic input.
#
# Verified against the built image, not against packaging policy: `ffmpeg
# -buildconf` reports "License: LGPL version 2.1 or later" with no
# --enable-gpl and no --enable-nonfree, and `ldd $(which ffmpeg)` resolves no
# libx264/libx265/libfdk_aac.

# ----- builder stage (toolchain + ffmpeg source, discarded at end) -----
FROM alpine:3.24 AS builder

# zlib-dev is NOT optional here: FFmpeg's PNG codec (and its deflate paths)
# silently fall back to "disabled" when zlib headers are missing at configure
# time, and the built image then reports "no decoder found for: png".
RUN apk add --no-cache nasm yasm gperf pkgconf \
        gcc g++ make binutils coreutils \
        zlib-dev libwebp-dev openssl-dev xz

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
      --enable-libwebp \
      --enable-openssl \
      --enable-shared \
 && make -j"$(nproc)" \
 && make install \
 && rm -rf /ffmpeg-9.0.2

# ----- final stage (runtime only) -----
FROM alpine:3.24

# python3 for convert.py + the shared libs the source-built ffmpeg links
# against. Deliberately NO Debian packages and no GPL-encumbered codec libs:
# nothing here carries the Debian CRITICAL CVEs and nothing is GPL.
# Alpine splits libwebp into four runtime libs; FFmpeg's webp codec links all
# of them (libavcodec.so.63 needs libwebpmux.so.3 symbols at load time).
# py3-pillow is the runtime fallback for inputs this LGPL build cannot decode.
RUN apk add --no-cache python3 py3-pillow zlib libwebp libwebpdecoder libwebpdemux libwebpmux openssl

RUN mkdir -p /data/input /data/output
COPY --from=builder /usr/local /usr/local
COPY convert.py /app/convert.py

# No ldconfig/ld.so.conf step needed here, and none is added: musl (Alpine's
# libc) ignores /etc/ld.so.conf and /etc/ld.so.cache entirely — that's a glibc
# mechanism. musl only consults /etc/ld-musl-<arch>.path (absent here) or its
# compiled-in default search path, and Alpine's musl build already bakes
# /usr/local/lib into that default, arch-agnostically. Verified empirically,
# not from docs: with zero ld.so.conf/ld-musl-*.path/ldconfig anywhere in this
# image, `ldd $(which ffmpeg)` still resolves every libav*/libsw* .so from
# /usr/local/lib.
WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
