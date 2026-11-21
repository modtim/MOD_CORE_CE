---
sidebar_position: 22
title: "Recipe: Batch-process a folder"
description: Run one workflow over every file in a folder — process hundreds of images, meshes, or clips in a single run.
wb_route: '/'
---

# Recipe: Batch-process a folder

**Goal:** apply the same workflow to **every file in a folder** — resize 500 product shots, generate textures for a whole library, transcode a folder of clips — in one run instead of one at a time.

## In the Workflow Builder

1. Upload all your files into one folder in **Data**.
2. Build your workflow as usual (e.g. `Image Input → Resize → output`).
3. On the **Image Input** node, select the **folder** (or multi-select the files) instead of a single file.
4. **Run.** Each file flows through the workflow **independently** — one file failing doesn't stop the rest.

*(Screenshot: an input node with a folder selected, and the Build queue showing per-file status.)*

Open the **Build queue** panel from the canvas toolbar to watch per-file status across the batch.

## Make it automatic

Pair batch mode with a [**File event** trigger](/triggers) so the workflow runs whenever a new file lands in the folder — a hands-off "process anything dropped here" pipeline.

## Over the API

Pass multiple input cog ids (or a folder's contents) in `cog_ids`:

```bash
# $AUTH is "Authorization: Bearer <token>" — get it from your sk_ key,
# see /api-reference#authentication
# BASE has no /v1; the path below carries it (never write /v1/v1).
BASE=http://localhost:8000
curl -s -X POST "$BASE/v1/road/workflow-versions/<your_version_id>/run" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"cog_ids": [101, 102, 103, 104]}'
```

## Tips

- Batch runs execute **per file** — large batches take proportionally longer, so size the batch to your available workers.
- Outputs land as separate cogs; add a **Zip Folder** node if you want a single download.

## Related

- [Workflow Builder Guide](/workflow-builder)
- [Triggers](/triggers) — run batches automatically.
