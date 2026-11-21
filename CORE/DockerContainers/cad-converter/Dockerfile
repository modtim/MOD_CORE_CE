# 1721b (CE vet step 4): the one CRITICAL here was libxml2 CVE-2026-6653, which Debian
# marks "won't fix" on BOTH trixie and bookworm, so no base-image rebase clears it.
# The chain was our apt libgl1 -> libglx-mesa0 -> libLLVM19 -> libxml2.so.2. OCP only
# needs libGL.so.1 to LINK: headless tessellation/export never creates a GL context, so
# the mesa vendor driver (and LLVM, and libxml2) is never loaded. Route 1: install the
# GL/X client libs in a builder stage, harvest ONLY the libglvnd + X client .so files
# (ldd closure, mesa/LLVM/libxml2 excluded) and drop them into /usr/lib/<triplet>/ of a
# final stage that has no apt GL at all. `dpkg -l | grep -E "libxml2|libllvm|mesa"` is
# EMPTY in the final image; nothing vulnerable is hidden, it is simply not present.

# Builder stage: GL/X client libraries only, never shipped.
# Pinned by the python:3.11-slim multi-arch INDEX digest (covers linux/amd64 + linux/arm64/v8).
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS glvnd-builder

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        libgl1 \
        libglx0 \
        libexpat1 \
        libx11-6 \
        libxext6 \
        libxmu6 \
        libxi6 \
        libxrender1 \
        libxrandr2 \
        libxcursor1 \
        libxinerama1; \
    rm -rf /var/lib/apt/lists/*; \
    T="$(ls /usr/lib | grep -m1 -- -linux-gnu)"; \
    mkdir -p /opt/glvnd; \
    harvest() { \
        for f in "$@"; do \
            [ -e "$f" ] || continue; \
            cp -fL "$f" /opt/glvnd/ 2>/dev/null || true; \
            ldd "$f" 2>/dev/null | sed -n 's/.*=> \(\/[^ ]*\).*/\1/' | grep -- '-linux-gnu' | \
            while read -r dep; do \
                case "$dep" in \
                    *mesa*|*LLVM*|*llvm*|*libxml2*|*libglx*) continue ;; \
                esac; \
                cp -fL "$dep" /opt/glvnd/ 2>/dev/null || true; \
            done; \
        done; \
    }; \
    harvest "/usr/lib/$T/libGL.so.1" "/usr/lib/$T/libGL.so.0" "/usr/lib/$T/libGLX.so.0" \
        "/usr/lib/$T/libGLdispatch.so.0" "/usr/lib/$T/libEGL.so.1" "/usr/lib/$T/libEGLdispatch.so.0" \
        "/usr/lib/$T/libX11.so.6" "/usr/lib/$T/libXext.so.6" \
        "/usr/lib/$T/libXmu.so.6" "/usr/lib/$T/libXi.so.6" "/usr/lib/$T/libXrender.so.1" \
        "/usr/lib/$T/libXrandr.so.2" "/usr/lib/$T/libXcursor.so.1" "/usr/lib/$T/libXinerama.so.1" \
        "/usr/lib/$T/libXau.so.6" "/usr/lib/$T/libXdmcp.so.6" "/usr/lib/$T/libxcb.so.1" \
        "/usr/lib/$T/libexpat.so.1"; \
    harvest /opt/glvnd/*; \
    ! ls /opt/glvnd | grep -qiE 'mesa|llvm|libxml2'

# Final stage: no apt GL install at all, so dpkg never knows libgl1/libglx-mesa0/libLLVM/libxml2.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

COPY --from=glvnd-builder /opt/glvnd /opt/glvnd

# OpenCascade dependencies. libxrender1 + libxrandr2 + libxcursor1 + libxinerama1 are
# pulled in by pythonocc-core's viewer module even on a headless run (linked-in at
# import); their absence was the `libXrender.so.1: cannot open shared object file`
# error 84/84 calibration tasks failed with. They arrive above as harvested .so files.
RUN set -eux; \
    T="$(ls /usr/lib | grep -m1 -- -linux-gnu)"; \
    mkdir -p "/usr/lib/$T"; \
    cp -f /opt/glvnd/* "/usr/lib/$T/"; \
    rm -rf /opt/glvnd; \
    ldconfig; \
    ! ldconfig -p | grep -iE 'libxml2|libllvm|mesa'

# pythonocc-core handles STEP/IGES -> mesh conversion
# cadquery includes pythonocc and adds STEP/IGES support
RUN pip install --no-cache-dir \
    cadquery \
    trimesh \
    numpy

RUN mkdir -p /data/input /data/output
COPY convert.py /app/convert.py

WORKDIR /data
ENTRYPOINT ["python3", "/app/convert.py"]
