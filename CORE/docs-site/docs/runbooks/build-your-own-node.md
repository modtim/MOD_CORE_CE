---
sidebar_position: 65
slug: /runbooks/build-your-own-node
title: Build and add your own node
description: "How to read the Docker build contexts CE ships for its node images, extend one, build the result locally, and register the image you built as a new node by pointing a node definition's executable_path at your own registry."
---

# Build and add your own node

Last reviewed: 2026-11-07

Community Edition (CE) ships the **Docker build contexts** for the node images it publishes,
not just the images themselves. You can read every Dockerfile, change it, build the result
on your own machine, and register the image you built as a new node.

## What ships

CE ships **32 node build contexts** under `CORE/DockerContainers/<name>/` in the release tree.
A **build context** is one directory: the `Dockerfile` plus every file that Dockerfile
`COPY`s. The entrypoint **varies per node** — only 15 of 51 CE image directories name it `entrypoint.py`
or `entrypoint.sh`; the others `ENTRYPOINT` a script such as `/app/convert.py`, and 12 write that
script inline with a `RUN` heredoc, so the folder may hold only a `Dockerfile`. Check the
Dockerfile's `ENTRYPOINT` line to find the script. Some contexts (12 of 51) also carry
`mod_node_auth.py`; the worker injects the task token at run time either way, and no credential
is ever baked into a Dockerfile.
No `COPY` reads outside the context directory, so each directory builds on a stock
`docker build` with no extra setup.

The 32 shipped contexts:

```
3d-thumbnail-gen  FFMpeg_CPU      TextLLM         audio-converter  cad-converter
crypto-utils      doc-converters  datetime-utils  geo-utils        hdr-converter
image-converter   image-ops       mesh-converter  ocr              pdf-processor
qr-barcode        spreadsheet     text-utils      translation      tts
usd-converter     video-converter WebSearch       ReturnExport     audio-input
image-input       mesh-input      resource-input  script-input     text-input
video-input       stack-model-call
```

## Reading a Dockerfile

Every CE node Dockerfile is a plain Dockerfile. A short one, `stack-model-call`:

```dockerfile
FROM python:3.11-slim
RUN pip install --no-cache-dir requests==2.31.0
RUN mkdir -p /data/input /data/output
RUN useradd -m -u 10001 modnode && chown -R 10001:10001 /data
WORKDIR /data
COPY entrypoint.py /app/entrypoint.py
COPY mod_node_auth.py /app/mod_node_auth.py
USER modnode
ENTRYPOINT ["python3", "/app/entrypoint.py"]
```

| Pattern | What it means for you |
|---------|---------------------|
| `FROM python:3.11-slim` | Public base image on Docker Hub |
| `FROM python:3.11-slim@sha256:...` | Same base, digest-pinned (build is reproducible) |
| `RUN pip install --no-cache-dir ...` | Python deps installed at build time |
| `COPY entrypoint.py /app/entrypoint.py` | Helper file taken from the context directory |
| `USER modnode` | Node runs unprivileged inside the container |
| `ENTRYPOINT ["python3", "/app/entrypoint.py"]` | The command the worker runs per task |

Every `FROM` is a **public base** (`python:3.11-slim`, `alpine:3.24`, or a digest-pinned
variant of one); a few nodes (`ocr`, `pdf-processor`) pin a `python:3.11.17-alpine3.24`
variant by digest instead. All of them are on Docker Hub — CE never depends on a MOD-internal
base image, so nothing extra has to be fetched before you build.

## Building a context locally

Build from the release root, with the context directory as the build context:

```bash
docker build -f CORE/DockerContainers/stack-model-call/Dockerfile \
  CORE/DockerContainers/stack-model-call \
  -t registry.example.com/my-stack-model-call:v1
```

## Extending a node

1. Copy the context: `cp -r CORE/DockerContainers/image-ops my-image-ops`
2. Edit the `Dockerfile` — add a `RUN`, swap a dependency version, add a `COPY` of a file
   you drop into the same directory.
3. Edit the entrypoint/helper script you copied alongside it.
4. Build and tag it as above, then push it to your registry.

Keep the `ENTRYPOINT` contract (the script reads `/data/input`, writes `/data/output`) so the
worker can still drive your node.

## Registering the image you built

CE publishes and uses its own node images under `ghcr.io/modtim/`. A node
definition points at its image with `executable_path`; the seeds use bare
`<image>:latest`, and CE rewrites the prefix to `ghcr.io/modtim/` at node registration
through the CE image mirror setting (`CE_IMAGE_REGISTRY`, `ce_mirror_ref`,
`CORE/API/utils/registries.py`). When the install sets a CE image mirror
(`CE_IMAGE_REGISTRY`), that prefix points at the mirror instead.

To add your own image, give the node definition a **fully qualified** reference at your
registry — an already-qualified reference is left as it is:

```python
executable_path = "registry.example.com/my-stack-model-call:v1"
```

The change takes effect on the next run of that node; the node then behaves like
any other node in the workflow builder. See [Getting started](/getting-started) for the
install and [Worker setup](/worker-setup) for the worker that runs the image.

> **Air-gap note:** the `FROM` lines pull their base images from Docker Hub **at build time
> only**. Once an image exists in a registry you can reach, running and updating the node
> needs no Docker Hub access — pre-pull the bases if you build on an air-gapped host.

If you are replacing the box that holds an install, see
[Back up an install and restore it onto a new box](/runbooks/ce-backup-and-restore).
