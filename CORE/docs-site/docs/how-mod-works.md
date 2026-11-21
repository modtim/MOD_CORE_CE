---
sidebar_position: 0
title: How MOD Works
description: The mental model behind MOD — files, nodes, workflows, runs, and workers — and what you can build with it.
dashboard_route: '#home'
---

# How MOD Works

MOD turns heavy file-processing into something you can **draw**: drop your files in, wire up a pipeline of processing steps on a canvas, hit run, and MOD farms the work out to powerful machines and hands you the results. You run the stack yourself with Docker; MOD schedules work onto whatever hardware you have.

This page is the 2-minute mental model. Once it clicks, the rest of the product is obvious.

## The five pieces

| Concept | In one line |
|---------|-------------|
| **File (cog)** | Anything you upload — an image, mesh, video, audio clip, document. Lives in your **Data** area. |
| **Node** | One processing step ("remove background", "generate a normal map", "transcribe audio"). MOD ships a curated seed list of nodes. |
| **Workflow** | A pipeline of nodes wired together on the canvas. It's **versioned**, so you can iterate safely. |
| **Run** | One execution of a workflow version against your files. Goes `queued → running → completed` (or `failed`). |
| **Worker** | The machine that does the work: the embedded worker (`mod-worker-embedded`) runs in your compose, or enroll your own. |

That's the whole model: **files → nodes → workflow → run → (workers) → results**.

## How a job flows

```text
  You                Dashboard / Builder            Workers
  ───                ───────────────────            ───────
  upload a file  ─►  stored as a "cog"
  build a workflow ► nodes wired on a canvas
  click Run      ─►  run created  ──────────────►   a worker picks up each
                                                     node's task, runs it in a
                     progress + logs  ◄──────────    container, returns output
  download result ◄─ output cogs
```

You watch progress live (per-node bars, streaming logs); MOD handles scheduling the tasks onto whatever hardware fits.

## Two places you'll work

- **Dashboard** — everything *around* workflows: your files, team, settings and analytics. Lives at your main MOD URL.
- **Workflow Builder** — the visual canvas where you *build and run* workflows. Opens in its own tab from the Dashboard sidebar.

The same things are available over a [REST API](/api-reference), so anything you do by clicking, you can automate.

## What you can build

MOD is general-purpose file processing. A taste:

- **Image cleanup** — resize, crop, rotate, strip metadata, watermark.
- **Format conversion** — image / audio / video / mesh / CAD / HDR / USD converters, 3D thumbnails.
- **Video** — FFmpeg (CPU) transcode.
- **AI/LLM nodes** — Text LLM and Stack Model Call run against your own bound models; system-web-search answers with no managed-AI dependency.
- **Your own code** — wrap any script, Docker image, or repo into a reusable node (the Node Builder ships from day 1).

See the full [Node Library](/node-library) for the catalog.

## Start here

1. **[Getting Started](/getting-started)** — sign up, upload, build, run (5 min).
2. **[Tutorial: Your First Workflow](/first-workflow)** — a complete worked example (remove a background), in the UI and the API.
3. **[Workflow Builder Guide](/workflow-builder)** — nodes, connections, batch mode.
4. **[API Reference](/api-reference)** — automate everything, with a live "try it" explorer.

New to the vocabulary? The [Glossary](/glossary) defines every term in plain English.
