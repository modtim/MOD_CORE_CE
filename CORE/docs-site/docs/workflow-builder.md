---
sidebar_position: 2
title: Workflow Builder Guide
description: Visual drag-and-drop editor for creating processing pipelines. Nodes, edges, run history, build queue.
dashboard_route: '#workflows'
wb_route: '/'
---

# Workflow Builder Guide

The Workflow Builder is a visual drag-and-drop editor for creating processing pipelines.

## Nodes

Nodes are the building blocks of workflows. Each node performs one operation.

### Node Types

- **Input Nodes** (blue) — select files to process
- **Process Nodes** (purple) — apply transformations (resize, texture generation, 3D conversion, etc.)
- **Output Nodes** (green) — define where results go
- **Logic Nodes** — conditional branching (If/Else, Switch, Split)

### Adding Nodes

1. **Node Library** (left panel) — browse by category, search by name
2. **Drag and Drop** — drag a node from the library onto the canvas
3. **Context Menu** — right-click a node for its menu.

### Connecting Nodes

Click an output port and drag to an input port. The builder validates connections — incompatible types show a warning.

## Execution

### Single Run
Click **Run** with files selected on input nodes.

### Batch Mode
Select multiple files on an input node. Each file is processed independently through the workflow.

### Monitoring
- Per-run progress bar with rolling ETA based on the scheduler's
  prediction model (refines as upstream nodes finish).
- Click any running node to open its live log tail (streamed from
  Redis as the worker writes).
- Failed nodes show the error message inline; retry / skip / cancel
  options are in the node's right-click menu.
- Long-running batches show per-file status in the Wizard Queue
  panel. Open **Functions** in the node library sidebar and click
  **Behind The Curtain - Wizard Queue**, or click a build notification in the bell.

### Where do my results go
Finished runs are listed in the dialog named **The Crossroads - Process Queue** (node library sidebar > **Functions** > **The Crossroads - Process Queue**) and in per-node run history on the canvas; the workflow toolbar has no **Runs** menu. Right-click a node and choose **Output versions…** to download that node's output files. **Functions > The Crossroads - Process Queue** is where you follow a run while it is running. Your uploaded files and images are listed on the **Data** page.

## Saving & Loading

- **Save** — saves the current workflow version
- **Save as…** (in the Save dropdown) — creates a copy with a new name
- **Load** — browse your saved workflows and versions
- **Import/Export** — share workflows as JSON files

## Edit Node Definitions

Open the **Actions** menu in the toolbar and click **Edit** (pencil icon) to manage node definitions:
- View all registered nodes
- See hardware variant build status
- Trigger calibration runs (`POST /nodes/{id}/calibrate`)
- Delete custom nodes

## Automate it (API)

Anything you build visually can be driven over the [API](/api-reference). A workflow is **versioned** — you run a specific version id.

```bash
BASE=http://localhost:8000/v1

# Get a Bearer token from your sk_ key (see /api-reference#authentication)
TOKEN=$(curl -s -X POST "$BASE/api-keys/token" -H "Content-Type: application/json" \
  -d '{"api_key": "sk_your_key_here"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

# Export a workflow as a ZIP bundle (back it up, diff it, or move it between tenants)
curl -s "$BASE/road/7/export" -H "$AUTH" -o my-workflow.zip

# Import that ZIP elsewhere (multipart upload)
curl -s -X POST "$BASE/road/import-zip" -H "$AUTH" \
  -F "file=@my-workflow.zip"

# Run a saved version against input files. Replace <your_version_id> with the
# latest_version_id field of your workflow in GET /workflows/, and <your_cog_id>
# with the cog_id returned by the file upload (POST /files/upload).
curl -s -X POST "$BASE/road/workflow-versions/<your_version_id>/run" -H "$AUTH" \
  -H "Content-Type: application/json" -d '{"cog_ids": [<your_cog_id>]}'
```

See [Getting Started → Quickstart with the API](/getting-started#quickstart-with-the-api) for the full upload → run → poll → download loop in Python and JavaScript.

## Versions

Every **Save** creates a new version of the workflow; **Save as…** makes a copy under a new name. You can **Load** any saved workflow and pick an earlier version to open or run. (There's no in-place "rollback" button — load the older version and save it forward if you want to revert.)
