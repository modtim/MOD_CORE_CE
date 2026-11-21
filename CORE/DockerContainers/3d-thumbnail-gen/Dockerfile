# 1723b route B: Debian base, but OSMesa is BUILT (not apt-installed) with the
# softpipe gallium driver and LLVM disabled, so nothing on the render path links
# libLLVM.so.19.1 -> libxml2 (libxml2 carries the "won't fix" CRITICAL
# CVE-2026-6653). Route A (alpine rebase) was ruled out: Alpine 3.24 mesa has no
# osmesa subpackage at all (apk search osmesa = empty; mesa split pkgs are
# gl/egl/gles/gbm/va/rusticl/vulkan only; no libOSMesa.so anywhere), and PyOpenGL
# needs libOSMesa.so.6/8.
#
# Builder stage: pinned mesa source (gitlab freedesktop archive, sha256-pinned).
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    meson \
    ninja-build \
    pkgconf \
    byacc \
    flex \
    libdrm-dev \
    libx11-dev \
    libxcb1-dev \
    libxext-dev \
    libxfixes-dev \
    xcb-proto \
    libpng-dev \
    libexpat-dev \
    python3-mako \
    python3-yaml \
    && rm -rf /var/lib/apt/lists/*

ADD https://gitlab.freedesktop.org/mesa/mesa/-/archive/mesa-24.2.5/mesa-mesa-24.2.5.tar.gz /tmp/mesa.tar.gz
RUN echo "b2465ee744904f1e5f5aeb01564a301fc93964a6dfa89100c1c0fa5585412b0a  /tmp/mesa.tar.gz" | sha256sum -c -

RUN export PYTHONPATH=/usr/lib/python3/dist-packages \
 && mkdir -p /tmp/mesa-src \
 && tar xzf /tmp/mesa.tar.gz -C /tmp/mesa-src --strip-components=1 \
 && rm -f /tmp/mesa.tar.gz \
 && sed -i "s/dep_xcb_xrandr = dependency('xcb-randr')/dep_xcb_xrandr = dependency('xcb-randr', required : false)/" /tmp/mesa-src/meson.build \
 && meson setup /tmp/mesa-build /tmp/mesa-src \
      --prefix=/usr/local \
      --libdir=/usr/local/lib/mesa \
      -Ddri-drivers-path=/usr/local/lib/mesa/dri \
      -Dgallium-drivers=softpipe \
      -Dllvm=disabled \
      -Dosmesa=true \
      -Dplatforms=x11 \
      -Dglx=xlib \
      -Dxlib-lease=false \
      -Degl=disabled \
      -Dgles1=disabled \
      -Dgles2=disabled \
      -Dshared-glapi=enabled \
      -Dvulkan-drivers= \
      -Dbuild-aco-tests=false \
      -Dbuildtype=release \
 && ninja -C /tmp/mesa-build install \
 && rm -rf /tmp/mesa-src

# Final stage: runtime only. No libosmesa6-dev, no libLLVM, no libxml2.
FROM python:3.11-slim

# libx11/libxrender/libxext: pyglet (pulled in by pyrender) dlopens XRenderFindVisualFormat
# at import; without these the node could never render at all (the product bug).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libx11-6 \
    libxrender1 \
    libxext6 \
    libxcb1 \
    libglu1-mesa \
    libdrm2 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/mesa/ /usr/local/lib/

# PyOpenGL dlopens "OSMesa" via ctypes.util.find_library, which only searches the
# ldconfig cache; register /usr/local/lib so libOSMesa.so.8 resolves.
RUN echo "/usr/local/lib" > /etc/ld.so.conf.d/zz-mesa-osmesa.conf && ldconfig

ENV PYOPENGL_PLATFORM=osmesa

RUN pip install --no-cache-dir \
    trimesh[easy] \
    PyOpenGL==3.1.10 \
    networkx==3.4.2 \
    pyrender \
    Pillow \
    minio \
    requests \
    numpy \
    cadquery \
    usd-core

RUN mkdir -p /app /data/input /data/output

COPY generate_thumbnail.py /app/generate_thumbnail.py
COPY mod_node_auth.py /app/mod_node_auth.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

WORKDIR /data

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
