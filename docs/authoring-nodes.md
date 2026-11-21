# Authoring nodes (by hand)

A MOD node is a **container** plus a **metadata definition**. The API scans node
metadata from the `/metadata` mount at startup and registers each as a runnable
capability; the orchestrator dispatches runs to a worker, which executes the
container.

> Core ships a **local / BYO-LLM node wizard** (point it at any
> OpenAI-compatible or Ollama endpoint) that builds a node for you — see
> [node-builder.md](./node-builder.md) and
> [Community Edition vs. other editions](./community-vs-paid.md). This guide covers
> authoring nodes directly, by hand or with the SDK, without the wizard.

## 1. The container

Create a directory under `CORE/DockerContainers/<YourNode>/` with a `Dockerfile`
and an entrypoint. The entrypoint reads its inputs from the staged input files /
parameters the worker provides and writes outputs to the staging location the
worker expects. Use the existing nodes as reference implementations — e.g. the
input, converter, and export nodes are the simplest.

## 2. The metadata

Add a metadata definition describing the node's inputs, outputs, parameters, and
the image to run. Model it on the seeded system nodes in `CORE/Metadata/system/`
(for example the `*_INPUT_Node.py` and `*_EXPORT_Node.py` files), which are the
canonical, minimal examples.

A metadata file is a Python file with a `# --- METADATA BLOCK ---` ...
`# --- END METADATA BLOCK ---` section of plain assignments. A file without a
`node_kind` is skipped by the scanner. The fields used by
`CORE/Metadata/system/Image_INPUT_Node.py` are: `name`, `description`,
`node_kind`, `category`, `executable_path` (the image to run),
`image_pull_policy`, `container_timeout`, `container_gpu_required`,
`input_schema`, `output_schema`, `configuration_schema`, `metadata`,
`batch_mode`, `cost_in_credits` and `tenant_id`.

Set `node_role: user` in the metadata to make the node visible in the
WorkflowBuilder palette (platform-scope nodes without a tenant default to
`system`, which the orchestrator can run but which stay hidden from the
palette; a node created under a tenant defaults to `user`).

## 3. Register and run

Rebuild/restart so the scanner picks up the new metadata, then the node appears
in the palette and can be wired into a workflow. The Community compose file
mounts `./CORE/Metadata` read-only at `/metadata`, which is where the scanner
looks (`METADATA_ROOT`).

> **Community Edition:** put your node's metadata in `CORE/Metadata/custom/` (create the
> directory if it does not exist - a fresh CE install ships only `CORE/Metadata/system/`).
> That directory is the scanner's user-node directory, mounted at `/metadata/custom`. Files
> there are scanned and registered on every restart, and are automatically
> marked user-facing (`node_role: user`) so they show up in the palette. The
> node seed keep-list only prunes the platform's own seeded `system/` nodes —
> you never need to add your node's name to it.

---

**Reference nodes to copy from** (shipped in Core):

- Inputs: `Image Input`, `Text Input`, `Resource Input`
- Converters: `Image Converter`, `Mesh Converter` (auto-inserted by the orchestrator)
- Export: `Return Assets Export`

> This guide is a starting point; expand it with the exact metadata schema and
> the worker input/output contract for your deployment.
