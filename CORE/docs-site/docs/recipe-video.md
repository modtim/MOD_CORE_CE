---
sidebar_position: 23
title: "Recipe: Edit & clean up video"
description: Transcode video with MOD's video nodes.
wb_route: '/'
---

# Recipe: Edit & clean up video

**Goal:** take raw footage and transcode it.

**Nodes you'll use:** `Video Input` → `Video Converter`.

## Transcode

1. Add a **Video Input** node pointing at your clip.
2. Add **Video Converter** (change format/codec).
3. Connect, **Save**, **Run**, download.

## Over the API

Same loop as any workflow — upload the clip, run your saved video workflow:

```bash
# $AUTH is "Authorization: Bearer <token>" — get it from your sk_ key,
# see /api-reference#authentication
COG=$(curl -s -X POST "$BASE/assets/upload-file" -H "$AUTH" \
  -F "file=@./clip.mp4" | jq -r .id)
curl -s -X POST "$BASE/road/workflow-versions/<your_version_id>/run" -H "$AUTH" \
  -H "Content-Type: application/json" -d "{\"cog_ids\": [$COG]}"
```

## Related

- [Node Library → Video](/node-library#video)
- [Batch-process a folder](/recipe-batch) — run over a whole library of clips.
