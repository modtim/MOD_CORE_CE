# Use an official Python runtime as a parent image
#
# FIX-API-IMAGE-SLIM (owner approved the safe set 11-17): this file is now TWO
# stages. Stage `build` (this one) is the unchanged single-stage build: every
# pip install, the torch wheel, both embedding bakes, the optional Pro compile
# and compileall. The final stage at the bottom starts from a clean
# python:3.11-slim and COPYs only the finished trees out of `build`, so bytes
# that an early layer wrote and a later layer overwrote/deleted (about 4.9 GB
# measured on head-e618b6503d99) never reach the shipped image.
FROM python:3.11-slim AS build

# Set the working directory in the container
WORKDIR /app

# Unbuffered stdout/stderr.
#
# Without this, Python block-buffers stdout when it is a pipe — which it always
# is under Docker. Log lines sit in an 8KB buffer until it fills, so a running
# service can appear silent indefinitely and `docker logs` shows only whatever
# happened to be flushed earlier.
#
# Measured on the dev deployment: `docker logs my_fastapi_app` stopped at
# 2026-07-28 21:11:08 and captured nothing for three days across many restarts,
# while the API stayed healthy and served requests throughout. Every "no errors
# in the logs" check in that window was vacuous — an empty stream and a clean
# one are indistinguishable, which is worse than no logging at all because it
# reads as reassurance.
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl libatomic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# wizard-learning-from-feedback §2 — install CPU-only torch BEFORE
# requirements.txt so sentence-transformers (which depends on torch)
# resolves against the CPU wheel and we don't drag in the ~3 GB
# CUDA wheel from PyPI default. The CPU wheel is ~200 MB.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
    "torch>=2.2.0,<3.0.0"  # dep-pinning: allow-unhashed download.pytorch.org publishes no wheel digests; range is the CPU-wheel guard (see comment above)

# Install the rest of the Python dependencies (pinned versions)
# Hash-locked install. requirements.lock.txt is GENERATED from the
# live image's wheel set (see its header + scripts/regen_python_locks.sh) and
# every entry carries a sha256, so a rebuild can never silently swap in a
# yanked/typosquatted wheel. requirements.txt stays as the human-edited source
# (floors/ranges + WHY comments) and is COPYed too so the regen has it in the
# image. --no-deps: the lock is already the full transitive closure.
COPY requirements.txt .
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements.lock.txt

# Test dependencies ship in the image on purpose. This project's documented way
# to run the suite is `docker exec my_fastapi_app python -m pytest ...` — every
# MANUAL_TEST_*.md says so — and conftest.py points at the modtech_test DB in
# the compose Postgres, so the tests are meant to run in this container against
# real services rather than in a separate harness.
#
# Leaving them out meant pytest only existed where someone had pip-installed it
# into a RUNNING container by hand, so any `up -d --force-recreate` of this
# service silently removed the ability to run tests — the failure looks like
# "No module named pytest" long after the recreate that caused it.
#
# Cost is negligible: pytest + plugins are a few MB against the ~200 MB torch
# wheel and the ~1.3 GB embedding model already in this image.
#
# `-c requirements.txt` is load-bearing. requirements-test.txt re-declares a
# subset of the app's dependencies with LOOSER floors (fastapi>=0.110,
# sqlalchemy>=2.0.28) than the pinned production set. Installed unconstrained
# it could upgrade a pinned production dependency as a side effect of adding a
# test runner — a build-time change to what actually serves traffic. Using it
# as a constraints file lets pip add the test framework while refusing to move
# anything requirements.txt has already pinned.
#
# The constraints file has to be DERIVED, not requirements.txt itself: pip
# rejects extras in a constraints file outright ("ERROR: Constraints cannot
# have extras"), and requirements.txt carries four — uvicorn[standard],
# pydantic[email], python-jose[cryptography], passlib[bcrypt]. Passing it
# directly made this image unbuildable, which
# is why the deployed image predated the test-deps change: nobody could build
# it. Stripping the extras loses nothing, because a constraint only ever
# contributes its version specifier — the extras are already installed by the
# requirements.txt install above.
COPY requirements-test.txt .
COPY requirements-test.lock.txt .
RUN pip install --no-cache-dir --no-deps --require-hashes -r requirements-test.lock.txt

# FIX-API-IMAGE-SLIM-SAFE-SET (PREPARED, NOT YET BUILT; plan: results-1110/
# DIAG-API-IMAGE-SIZE-LAYER-BREAKDOWN-AND-RANKED-SLIM-PLAN-D75.md items 2-4):
# drop files nothing imports at run time, no behaviour change.
#  (2) directories named exactly `test` or `tests` under site-packages (about
#      290 MB; `testing` dirs such as numpy.testing / torch.testing stay);
#  (3) __pycache__ of the lazy-only packages oci, libcloud, kubernetes (about
#      270 MB; they are imported only by the cloud-provider / k8s services on demand,
#      so only those paths pay a one-time in-memory compile; torch, transformers
#      and numpy keep their .pyc so start-up time is unchanged);
#  (4) torch C++ headers, torch test binaries and protoc (about 115 MB;
#      torch_shm_manager and torch/lib are kept).
# This is the build stage: the runtime stage COPYs the finished site-packages,
# so the deletions are what ships. The import smoke at the end FAILS the build
# loudly if a deletion broke an eager import (the model-bake RUNs below are
# best-effort and would otherwise hide it behind their `|| echo` fallback).
RUN P=/usr/local/lib/python3.11/site-packages \
 && find "$P" -type d \( -name test -o -name tests \) -prune -exec rm -rf {} + \
 && for d in oci libcloud kubernetes; do \
      if [ -d "$P/$d" ]; then find "$P/$d" -type d -name __pycache__ -prune -exec rm -rf {} +; fi; \
    done \
 && rm -rf "$P/torch/include" "$P"/torch/bin/protoc* "$P"/torch/bin/*Test \
 && python -c "import torch, sentence_transformers, pandas, sklearn, scipy, onnxruntime, numpy; print('slim smoke ok')"

# wizard-learning-from-feedback §2 — pre-download the embedding
# model into the image so first-request latency isn't dominated by a
# HuggingFace download. The model artifact (~1.3 GB) lives at
# /app/models/embeddings/snowflake-arctic-embed-l. Build host needs
# internet access to huggingface.co for this step. Override the
# model id at build time via --build-arg WIZARD_EMBEDDING_MODEL=…
# if you want a smaller variant for dev (e.g. arctic-embed-s).
ARG WIZARD_EMBEDDING_MODEL=Snowflake/snowflake-arctic-embed-l
ENV WIZARD_EMBEDDING_MODEL=${WIZARD_EMBEDDING_MODEL}
# BEST-EFFORT only — the model is resolved MinIO-first at runtime
# (utils/embedding_model_store), so the build MUST NOT fail if HuggingFace is
# unreachable/rate-limited. If HF is reachable it warms the image; otherwise the
# first runtime use pulls from MinIO (or seeds HF once and mirrors to MinIO).
# FIX-API-IMAGE-WRITABLE-PATHS: mkdir + chown INSIDE this RUN (same layer — a
# separate `RUN chown -R` after a ~1.3 GB bake would double the layer) so the
# uid-10001 API can write/replace the baked model files (CHECK-5: 3 x
# "could not mirror embedding model ... Permission denied").
RUN mkdir -p /app/models/embeddings && python -c "\
import os; \
from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer(os.environ['WIZARD_EMBEDDING_MODEL']); \
m.save('/app/models/embeddings/snowflake-arctic-embed-l'); \
print('wizard embedding model baked:', os.environ['WIZARD_EMBEDDING_MODEL'])" \
    || echo "NOTE: HF prefetch skipped (unreachable) — model loads from MinIO at runtime"; chown -R 10001:10001 /app/models/embeddings 2>/dev/null || true

# FIX-CE-EMBED-MODEL-NO-HF-FETCH — owner rule (owner 11-07, option B): the CE
# install must fetch NOTHING from HuggingFace at first boot (MOD_EMBED_NO_HF=1 in
# the CE compose api: env), so the PLATFORM default embedding model
# all-MiniLM-L6-v2 (utils/embeddings.py MODEL_NAME) is baked here too. This
# download happens on MOD's build host (sanctioned at IMAGE BUILD, same as the
# arctic bake above); the CE install then has the model present at first boot, so
# docs RAG ingest + the Knowledge Base work with no admin upload.
ARG CE_PLATFORM_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ENV CE_PLATFORM_EMBEDDING_MODEL=${CE_PLATFORM_EMBEDDING_MODEL}
# BEST-EFFORT only — same contract as the arctic bake: the model is resolved
# MinIO-first at runtime, so the build MUST NOT fail if HuggingFace is
# unreachable/rate-limited.
RUN mkdir -p /app/models/embeddings && python -c "\
import os; \
from sentence_transformers import SentenceTransformer; \
m = SentenceTransformer(os.environ['CE_PLATFORM_EMBEDDING_MODEL']); \
m.save('/app/models/embeddings/all-MiniLM-L6-v2'); \
print('platform embedding model baked:', os.environ['CE_PLATFORM_EMBEDDING_MODEL'])" \
    || echo "NOTE: HF prefetch skipped (unreachable) — model loads from MinIO at runtime"; chown -R 10001:10001 /app/models/embeddings 2>/dev/null || true

# Copy application code (see .dockerignore for exclusions)
COPY . .

# ============================================================================
# Optional Nuitka compile stage. OPT-IN, default OFF.
#
# The dev/stage build (no --build-arg) is UNCHANGED: this whole RUN is a
# no-op echo when MOD_PRO_COMPILE != 1, so it adds no apt/pip cost to the
# normal image.
#
# When MOD_PRO_COMPILE=1, the restricted modules (open-core/manifest.yaml deny list
# for CORE/API) are compiled with Nuitka and their .py is deleted from /app,
# so the image holds no source for them, only .so. The compile tooling
# (pro_compile.py + its build_public_repo.py import + manifest.yaml) is NOT a separate COPY: the Pro build
# harness stages both files into the CORE/API build context before
# `docker build --build-arg MOD_PRO_COMPILE=1` (they arrive in /app via the
# `COPY . .` above), then verifies with open-core/check_pro_image.py.
# Compilers + nuitka are purged at the end of the same RUN so the final
# stage ships no build toolchain. See open-core/README.md "Pro build (GT.P3)".
# ============================================================================
ARG MOD_PRO_COMPILE=0
# GT.P6 — per-customer fingerprint (see open-core/README.md "Pro build").
# MOD_CUSTOMER_ID: validated [A-Za-z0-9_-]{3,64} by pro_compile.py; EMPTY
# (default) = no fingerprint at all, so CE/dev builds are byte-identical.
# MOD_FP: the fingerprint the image LABEL carries — the build harness gets it
# from `pro_compile.py --customer-id <id> --list-only` (prints
# `image label: com.example.fp=<fp>`); pro_compile.py refuses (exit 2) if
# MOD_FP disagrees with the marker it writes, so label and marker can never
# ship inconsistent.
ARG MOD_CUSTOMER_ID=
ARG MOD_FP=
RUN if [ "$MOD_PRO_COMPILE" != "1" ]; then \
        echo "pro compile (GT.P3): OFF (MOD_PRO_COMPILE=$MOD_PRO_COMPILE) — no-op"; \
    else \
        for f in pro_compile.py build_public_repo.py manifest.yaml; do \
            test -f /app/$f || { echo "ERROR: /app/$f missing — Pro build must stage open-core/{pro_compile.py,build_public_repo.py,manifest.yaml} into the context (open-core/README.md \"Pro build\")"; exit 1; }; \
        done; \
        echo "pro compile (GT.P3): ON — compiling restricted modules"; \
        FP_ARGS=""; \
        if [ -n "$MOD_CUSTOMER_ID" ]; then FP_ARGS="$FP_ARGS --customer-id=$MOD_CUSTOMER_ID"; fi; \
        if [ -n "$MOD_FP" ]; then FP_ARGS="$FP_ARGS --expect-fp=$MOD_FP"; fi; \
        apt-get update \
            && apt-get install -y --no-install-recommends gcc g++ libc6-dev python3-dev patchelf ccache \
            && rm -rf /var/lib/apt/lists/* \
            && pip install --no-cache-dir nuitka ordered-set zstandard \
            && python3 /app/pro_compile.py /app --context-root /app/.pro-src-sentinel --manifest /app/manifest.yaml $FP_ARGS \
            && rm -f /app/pro_compile.py /app/build_public_repo.py /app/manifest.yaml \
            && pip uninstall -y nuitka ordered-set zstandard \
            && apt-get remove -y --purge gcc g++ libc6-dev python3-dev patchelf ccache \
            && apt-get autoremove -y \
            && rm -rf /var/lib/apt/lists/* /root/.cache; \
    fi # dep-pinning: allow-unhashed build-only Nuitka toolchain, purged in the same RUN (pip uninstall below)

# GT.P6 — the image LABEL carries the per-customer fingerprint (empty when
# MOD_FP is unset = CE/dev builds carry no fingerprint). trace_leak.py reads
# it via `docker image inspect`.
# J156 — precompile the app tree so the 347 migrate/step subprocesses on a
# fresh CE DB don't each re-compile /app (measured: `import database` ~= 0.9-1.0
# s per process on an image holding 0 .pyc files; ~= 4-5 min across the migrate
# run). Placed AFTER the final COPY of app code and after the GT.P3 pro stage,
# so it compiles exactly the sources that actually ship (a Pro build has
# already deleted the source .py by this point, and __pycache__ entries for
# deleted sources are inert anyway — Python only consults them when the .py
# exists). This RUN executes as root, before the final `USER 10001` switch.
# PYTHONDONTWRITEBYTECODE is not set in this image, so the .pyc set written
# here is also what the runtime user (uid 10001) imports read.
RUN python3 -m compileall -q -j 0 /app

# FIX-API-IMAGE-SLIM-SAFE-SET item 1 (PREPARED, NOT YET BUILT): park the baked
# models (about 1.4 GB, source dir mode/owner kept by mv) OUTSIDE /app so the
# runtime stage can COPY them as their OWN layer ahead of the code layer. The
# bake RUNs above still write /app/models/embeddings (the writable-paths guard
# test pins that text); only the hand-off path changes. Without this a new pin
# re-moves the 1.4 GB model layer for 45 MB of changed code.
RUN mkdir -p /opt/baked && mv /app/models /opt/baked/models

LABEL com.example.fp="${MOD_FP}"

# scheduler-ml-pipeline-hardening §2 — bake in a stage-trained scheduler
# baseline ONNX so a fresh on-prem deploy has a working model from boot.
# Bump the default when a new stage-blessed baseline is published; refresh
# the files in CORE/API/ml-baseline/ to match. See ml-baseline/README.md
# for the pull recipe.
ARG ML_BASELINE_VERSION=2
ENV ML_BASELINE_VERSION=${ML_BASELINE_VERSION}
RUN test -f /app/ml-baseline/scheduler-v${ML_BASELINE_VERSION}.onnx \
    && test -f /app/ml-baseline/scheduler-v${ML_BASELINE_VERSION}.json \
    && test -f /app/ml-baseline/scheduler-v${ML_BASELINE_VERSION}-normalization.json \
    || (echo "ERROR: ML baseline files for v${ML_BASELINE_VERSION} missing under /app/ml-baseline/. Pull them from stage MinIO and rebuild — see CORE/API/ml-baseline/README.md" && exit 1)

# Make the new entrypoint script executable
RUN chmod +x /app/entrypoint.sh

# Set the new script as the entrypoint for the container
ENTRYPOINT ["/app/entrypoint.sh"]

EXPOSE 80

# C9S3-FIX2: run as non-root (uid 10001)
RUN groupadd -g 10001 app && useradd -u 10001 -g app -s /bin/sh -d /app app
# FIX-API-IMAGE-WRITABLE-PATHS: /run is root:root 755 in the image, so uid 10001
# gets EACCES on the DB-bootstrap lock/done/creds markers (CHECK-5: 28 x
# "Permission denied: /run/mod-db-bootstrap.lock"). Non-recursive chown of the
# single directory; compose mounts (/run/openbao etc.) land on top, harmless.
RUN chown 10001:10001 /run
USER 10001

# ============================================================================
# FIX-API-IMAGE-SLIM — RUNTIME STAGE (this is the image that ships).
#
# Clean python:3.11-slim + the same runtime apt packages + the finished trees
# from `build`. Nothing else crosses over: no pip build layers, no intermediate
# bake layers. Plain COPY (no --chown) on purpose: COPY --from keeps the source
# uid/gid per file, so /app/models/embeddings stays 10001:10001 (set inside the
# bake RUNs) and everything else stays root-owned, byte-for-byte the ownership
# of the single-stage image. A blanket --chown 10001:10001 would make the
# compiled code tree writable by the API user, which it is not today.
# Same user, WORKDIR, ENV, LABEL, EXPOSE, ENTRYPOINT as the build stage.
# ============================================================================
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl libatomic1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
# FIX-API-IMAGE-SLIM-SAFE-SET item 1 (PREPARED, NOT YET BUILT): models first, in
# their own layer (same files, same path /app/models, same 10001 ownership on
# /app/models/embeddings), then the code layer. The build stage moved the models
# to /opt/baked/models, so the `/app` COPY below no longer carries them and a
# code-only pin re-uses this layer.
COPY --from=build /opt/baked/models /app/models
COPY --from=build /app /app

# The single-stage image carried ~200 stdlib .pyc files (written by the build's
# own python runs); the clean base has none, and uid 10001 cannot write
# /usr/local/lib, so every API/migrate process would re-compile the stdlib in
# memory (the J156 cost). Recreate them here as root; site-packages and /app
# already carry their .pyc from `build`. `|| true`: stdlib test data holds
# deliberately uncompilable files, which make compileall exit 1.
RUN python3 -m compileall -q -j 0 -x '/(site-packages|test|tests|lib2to3)/' /usr/local/lib/python3.11 >/dev/null 2>&1 || true

# ARGs do not cross stage boundaries: redeclare with the same defaults so a
# --build-arg override still lands in the shipped image's ENV/LABEL.
ARG WIZARD_EMBEDDING_MODEL=Snowflake/snowflake-arctic-embed-l
ENV WIZARD_EMBEDDING_MODEL=${WIZARD_EMBEDDING_MODEL}
ARG CE_PLATFORM_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ENV CE_PLATFORM_EMBEDDING_MODEL=${CE_PLATFORM_EMBEDDING_MODEL}
ARG ML_BASELINE_VERSION=2
ENV ML_BASELINE_VERSION=${ML_BASELINE_VERSION}
ARG MOD_FP=
LABEL com.example.fp="${MOD_FP}"

ENTRYPOINT ["/app/entrypoint.sh"]

EXPOSE 80

RUN groupadd -g 10001 app && useradd -u 10001 -g app -s /bin/sh -d /app app
RUN chown 10001:10001 /run
USER 10001