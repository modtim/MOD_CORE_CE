---
sidebar_position: 3
title: Node Library
description: The processing nodes that ship in MOD Community Edition, how nodes work, and how to discover them via the API.
wb_route: '/'
---

# Node Library

Community Edition ships a focused starter set of nodes, and the node builder lets you add your own (see My Nodes). Drag nodes onto the Workflow Builder canvas, or discover and run them over the [API](/api-reference).

## How a node works

Every node is a containerized capability with:

- **Input / output / configuration schemas** — what it consumes, produces, and the knobs it exposes (visible in the node's panel and via `GET /v1/nodes/{id}`).
- **Hardware variants** (`gpu_variants`) — the node builder can produce CUDA / ROCm / Intel / NPU / CPU builds, and MOD routes the run to matching hardware; CPU-only nodes run anywhere. Every node this edition ships runs on CPU; the 3D Thumbnail Generator renders on CPU via OSMesa (no GPU worker needed).
- **Batch mode** — most processing nodes accept a folder and fan out across its files.

```bash
# Discover the available nodes (and a node's full schema) over the API.
# $AUTH is "Authorization: Bearer <token>" — see /api-reference#authentication
BASE=http://localhost:8000
curl -s $BASE/v1/nodes/ -H "$AUTH" | jq '.[].name'
curl -s $BASE/v1/nodes/<node_id> -H "$AUTH" \
  | jq '{name, type, input_schema, output_schema}'
```

## Writing node outputs

A node's container reads its inputs under `/data/input` and **must write its results as files under `/data/output`** — one file per output. MOD uploads everything in `/data/output` as the run's outputs; stdout and stderr are logs only, never outputs. A node that only prints (for example `print(result)` with no file written) produces nothing and the run fails with:

```
Container produced no output files. Check container logs - the processing may have failed silently or received incorrect input data type.
```

A minimal script node entrypoint:

```bash
#!/bin/sh
# entrypoint.sh - read /data/input, write /data/output
mkdir -p /data/output
for f in /data/input/*; do
  cp "$f" "/data/output/$(basename "$f")"
done
```

Those 6 lines are the whole contract: create the output directory, walk the inputs, write one file per output under `/data/output`. Replace the `cp` with your own processing — whatever you write under `/data/output` becomes the run's downloadable outputs.

## Inputs

Every workflow starts with an input node. Image, Video, Audio, Mesh and Resource Inputs point at one of your files; Text Input takes the text directly; Script Input takes the Python code you type into it and emits it as a script file (`script.py`) for downstream use:

- **Image Input**, **Video Input**, **Audio Input**, **Mesh Input**, **Text Input**
- **Resource Input**, **Script Input**

## Image processing

- **Image Resize** — scale to target dimensions (WIDTH/HEIGHT, KEEP_ASPECT, or MAX_SIDE). With KEEP_ASPECT on (the default), WIDTH and HEIGHT define a fit-inside box, not exact output dimensions.
- **Image Crop** — crop to a LEFT/TOP/RIGHT/BOTTOM rectangle
- **Image Rotate** — rotate by ANGLE (90/180/270 or any whole angle)
- **Image Metadata** — read EXIF into `metadata.json` (GPS only when INCLUDE_GPS is set)
- **Image Strip Metadata** — remove every metadata marker from the image
- **Image Watermark** — stamp WATERMARK_TEXT onto the image (POSITION, OPACITY, FONT_SIZE, COLOR)
- **HDR Converter** — tone-map / convert HDR imagery
- **Image Converter** — convert between PNG, JPG, TIFF, BMP, WebP, GIF (image-converter (ffmpeg with Pillow fallback))

## 3D formats

- **Mesh Converter** — reads OBJ, GLB, glTF, PLY, STL, FBX, DAE, 3DS, OFF, 3MF, X3D, USD, USDZ, USDA and USDC; writes OBJ, GLB, glTF, PLY, STL, DAE, OFF or 3MF (set TARGET_FORMAT). FBX and USD are input-only.
- **CAD Converter** — convert STEP / STP to GLB (tessellated for browser 3D viewing)
- **3D Thumbnail Generator** — render a PNG preview for a mesh/model (OBJ, STL, GLB, FBX, USD, STEP); CPU software rendering (OSMesa)
- **USD Converter** — convert USD / USDA / USDC / USDZ scenes to GLB (OpenUSD + trimesh)

## Video

- **Video Converter** — transcode to MP4, MOV or WebM (FFmpeg CPU). MP4 and MOV output is VP9+AAC by default (the default image has no H.264 encoder); a MOV target is written as an MP4 container with a .mov name. H.264 is available as an optional add-on image (Cisco OpenH264): (1) pull the add-on image with `docker compose --profile h264 pull`; (2) in Settings, turn on `nodes.h264_addon_enabled`, then refresh the node palette; (3) add the "Video Converter (H.264)" node (mp4 and mov outputs encode as H.264 with AAC). The default Video Converter keeps producing VP9. The add-on needs a Debian-slim image from ghcr (online pull); for an air-gapped host, `docker save` the image on a connected machine and `docker load` it. The H.264 encoder is Cisco OpenH264 (a separate binary licence applies; see NOTICE). The Cisco binary is not part of the MOD Core image: at first start your own machine downloads it from Cisco into the add-on's cache volume, or, on an offline host, you mount a copy of the Cisco file into that volume yourself. OpenH264 Video Codec provided by Cisco Systems, Inc.

## Audio

- **Audio Converter** — MP3 / WAV / FLAC

## Control, LLM and search

- **Human Approval** — pauses a run until a person approves or rejects it. Built in: no container.
- **Text LLM** — generate text with a model you have bound (ROLE = `generator` or `reasoner`). The node needs a bound model and fails without one. Bind it in **Gearworks - Settings > Gearworks - AI Configuration**: in the **Easy** view (the default) use the **Node Wizard** row of **Who uses what**; in the **Advanced** view use the **Node Wizard** tab (**Provider**, **Model**, **Base URL**, **API key**, then **Save**); steps: [Bind a model to the Wizard](bind-a-model-to-the-wizard.md). Writes `output.txt` and `response.json`.
- **Stack Model Call** — ask your own model stack about one or more images (MODEL and PROMPT required; SYSTEM_PROMPT and JSON_SCHEMA optional). Calls the platform's own models; no third-party host and no key for you to supply. MODEL must be a model that one of your deployed stacks serves: start that stack on the **Persistent Stacks** page (see [Bind a model to the Wizard](bind-a-model-to-the-wizard.md), section 3).
- **system-web-search** — keyless DuckDuckGo search (with Wikipedia expansion) that returns results as JSON artifacts. A system node; it needs outbound internet from the node container (duckduckgo.com, en.wikipedia.org).

---

> Don't see what you need? Build your own with the custom container node wizard. The list above is not exhaustive — the full, live catalog is in the Workflow Builder's node sidebar and via `GET /v1/nodes/`.
