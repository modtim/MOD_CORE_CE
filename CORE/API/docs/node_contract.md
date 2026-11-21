<!-- node-contract-version: 1 -->
# MOD Node Contract (v1)

This is the reference an external developer (or their AI coding agent) needs to
build a node that MOD will accept through the **external node-authoring API**
(`/v1/node-authoring/*`) — no platform source required. It ships with the
free/open build.

A "node" is a container that MOD runs as one step in a workflow. MOD stages the
step's inputs on disk, runs your container, and collects whatever it writes.

## The container contract

Your container image MUST:

1. **Read inputs from `/data/input/`** — MOD mounts the step's input files here.
2. **Write outputs to `/data/output/`** — MOD collects everything your
   container writes here as the step's results.
3. **Be configured through environment variables** — any per-run configuration
   (declared in `configuration_schema`) is passed as env vars. Do not expect
   command-line arguments.
4. **Base the image on an allowlisted base image** — the `FROM` (build-from-context)
   or the pre-built image reference (image mode) must match the platform's
   base-image allowlist. Fetch the current list from
   `GET /v1/node-authoring/base-images`.
5. **Run as a non-root user** — the final `USER` directive must be non-root
   (uid `10001` is the convention). Root at build time is fine; drop back to a
   non-root user before the final directive.
6. **Define its own `ENTRYPOINT`** (build-from-context) — the entrypoint should
   read `/data/input`, do its work, and write `/data/output`, then exit `0` on
   success and non-zero on failure.

MOD applies these gates after you submit, regardless of how you built the image:
base-image admission, an image vulnerability scan (results gate palette use),
a per-node outbound egress allowlist (default-deny), non-root enforcement, and
an optional sandbox runtime. Externally-authored nodes always land as **private
drafts** — never system nodes, never auto-published to a marketplace.

## Authentication

All calls authenticate with an **API key** that carries the `node:author`
scope, sent as the `X-API-Key` header (a read-only key is rejected). Mint one
with `POST /api-keys/` and `capability_scope: ["node:author"]`. The node is
created under the key owner's tenant and user.

## Submission shapes

### 1. Register a pre-built image (`source: "image"`)

You built and pushed an image to a registry MOD can pull (e.g. your tenant
registry). Register it directly — MOD does not build anything.

```http
POST /v1/node-authoring/nodes
X-API-Key: sk_...
Content-Type: application/json

{
  "source": "image",
  "image": "ghcr.io/modtim/base-python@sha256:...",
  "name": "My Resize Node",
  "type": "processor",
  "description": "Resizes every image in /data/input to 512px.",
  "input_schema":  {"input_type": "image", "file_mode": "both"},
  "output_schema": {"output_type": "image", "file_mode": "both", "paired": true},
  "configuration_schema": {"MAX_EDGE": {"type": "int", "default": 512}},
  "accepted_formats": ["png", "jpg"],
  "cost_in_credits": 1,
  "batch_mode": "batch",
  "container_timeout": 1800,
  "allowed_egress": [],
  "use_sandbox_runtime": false
}
```

Response `201`:

```json
{ "id": 123, "name": "My Resize Node", "node_role": "user", "scan_status": "pending_scan" }
```

### 2. Build from a Dockerfile (`source: "dockerfile"`)

You have a self-contained Dockerfile. MOD builds and pushes the image for you,
then registers it. No LLM is involved.

```http
POST /v1/node-authoring/nodes
X-API-Key: sk_...
Content-Type: application/json

{
  "source": "dockerfile",
  "dockerfile": "FROM ghcr.io/modtim/base-python:latest\nCOPY run.py /run.py\nUSER 10001\nENTRYPOINT [\"python3\", \"/run.py\"]\n",
  "name": "My Built Node",
  "type": "processor",
  "description": "...",
  "input_schema":  {"input_type": "image", "file_mode": "both"},
  "output_schema": {"output_type": "image", "file_mode": "both"},
  "configuration_schema": {},
  "cost_in_credits": 1
}
```

Response `202`:

```json
{ "task_id": 777, "step": "external-node-authoring", "status": "pending" }
```

Poll the build with the task id (`get_build_status` in the MCP, or the task
status endpoint). The node registers as a draft when the build succeeds.

> The Dockerfile must be **self-contained**: define its own `ENTRYPOINT` and end
> on a non-root `USER`. You do not supply entrypoint or build scripts — MOD
> builds and pushes from the Dockerfile alone.

## Validate before you submit

`POST /v1/node-authoring/nodes/validate` runs the same admission checks as the
real submission but **creates nothing** and returns `{passed, rule, reason}`.
Use it to iterate on your image reference or Dockerfile locally (this is the
MCP's `validate_submission` tool).

## Manifest field reference

| Field | Required | Meaning |
|-------|----------|---------|
| `source` | yes | `"image"` or `"dockerfile"`. |
| `image` | image mode | Pullable pre-built image reference. Becomes the node's `executable_path`. |
| `dockerfile` | dockerfile mode | Self-contained Dockerfile (`FROM` an allowlisted base, non-root final `USER`, own `ENTRYPOINT`). |
| `container_env` | no | Extra env vars baked into the built image (dockerfile mode). |
| `name` | yes | Node display name. |
| `type` | yes | Node type / category (e.g. `processor`). |
| `description` | no | Human-readable description. |
| `input_schema` | no | How the node consumes `/data/input` (input type, file mode). |
| `output_schema` | no | How the node produces `/data/output`. |
| `configuration_schema` | no | Per-run config exposed as env vars. |
| `accepted_formats` | no | Allowed input file extensions; omit/null = accept all. |
| `cost_in_credits` | no | Credit cost per run (default 0). |
| `batch_mode` | no | `"batch"` (one file at a time) or `"grouped"`. |
| `container_timeout` | no | Per-run timeout, seconds (default 1800). |
| `allowed_egress` | no | Outbound egress allowlist: list of `{host, ports, protocol}`. Omit/empty = default-deny. |
| `use_sandbox_runtime` | no | Run under the sandbox runtime (gVisor) when available. |
| `gpu_variants` | no | Optional `{backend: image-tag}` map for per-hardware variants. |
| `process_type` | no | Informational hardware label (`CPU`/`GPU`). |

Identity- and privilege-sensitive attributes (tenant, user, system role,
marketplace/publish flags, build spec id) are **not** part of this contract —
they are always derived server-side, so a submission cannot set them.

## Minimal conformant starter node

`run.py`:

```python
import os, pathlib

IN = pathlib.Path("/data/input")
OUT = pathlib.Path("/data/output")
OUT.mkdir(parents=True, exist_ok=True)

max_edge = int(os.getenv("MAX_EDGE", "512"))  # from configuration_schema

for src in IN.iterdir():
    if not src.is_file():
        continue
    # ... do work; write results under OUT ...
    (OUT / src.name).write_bytes(src.read_bytes())  # placeholder: copy through
```

`Dockerfile`:

```dockerfile
FROM ghcr.io/modtim/base-python:latest
COPY run.py /run.py
USER 10001
ENTRYPOINT ["python3", "/run.py"]
```

Submit `run.py` inside a self-contained Dockerfile via `source: "dockerfile"`,
or build and push it yourself and register via `source: "image"`.
