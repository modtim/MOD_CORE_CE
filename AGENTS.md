# AGENTS.md

Guidance for coding assistants and automated agents working in this repo (MOD Core CE, Apache-2.0).

## What this repo is

MOD Core CE: a self-hosted platform that runs any container as a node. The user supplies the image; files go in through `/data/input` and come out through `/data/output`. MOD schedules each job onto a matching worker, isolates it, reports resource usage, and records the run. It is not a CAD or 3D tool; the 3D, STEP, GLB, USD, trimesh and pyrender nodes that ship are samples.

## Layout

- `CORE/API/`: FastAPI application (scheduling, worker placement, run history, settings)
- `CORE/Worker/`: Go worker (runs containerized nodes, reports to the API; `go.mod` module `github.com/modtim/MOD_CORE_CE/CORE/Worker`)
- `CORE/Dashboard/`: sign-in home, runs, deployments, settings pages (JS)
- `CORE/WorkflowBuilder/`: visual canvas and node wizard (TypeScript, jest tests)
- `CORE/docs-site/`: documentation source (Docusaurus-style pages)
- `CORE/DockerContainers/`: example node containers (these are example nodes; any container image you supply can be a node)

## Facts (verify against these files before repeating them)

- Product version 1.1.0: `CORE/API/main.py` (`PRODUCT_VERSION`), `CORE/Dashboard/package.json`
- GPU workers are supported (not preview): AMD, Intel and NVIDIA GPUs work on Linux and under WSL2; the worker detects NVIDIA (CUDA), AMD (ROCm), Intel and NPU: `CORE/Worker/internal/capabilities/detect.go`. macOS workers are a development path; the Metal GPU probe is planned: `CORE/docs-site/docs/macos-worker.md`
- No quantity caps in CE (seat, worker, app, workflow, concurrent-run, storage, workflow-checkpoint, API rate limit; defaults 0 = unlimited): `CORE/API/utils/usage_enforcement.py`, `CORE/API/services/platform_settings.py` (`_ce_unlimited`), `CORE/API/services/workflow_checkpoint_quota.py`. Request-size and abuse guards remain (admin-tunable): `CORE/API/middleware/upload_size_cap.py`
- Workflow API prefix is `/v1/road/`: `CORE/API/routers/workflow_interface_run.py`
- MOD is the compute layer for machines you own, beneath tools like ComfyUI; complementary to them and to Kubernetes or Rancher: `README.md`
- Windows workers run through WSL2 + Docker Desktop: `CORE/docs-site/docs/windows-worker.md` (the native no-container worker is planned, not released)
- MCP servers ship in CE: `CORE/workflow-runner-mcp/`, `CORE/automations-mcp/`

## How to run tests

- WorkflowBuilder: jest per `CORE/WorkflowBuilder/package.json` (`"test": "jest"`)
- Worker: `go test` under `CORE/Worker` (Go is needed; if absent, run it in a Go toolchain container with a private cache volume)
- Report pass claims as counts ("N passed"), never bare "it works"

## Contributing to this repo

- This repo is the Apache-2.0 Community Edition; see [CONTRIBUTING.md](./CONTRIBUTING.md) for the sign-off and pull-request process, and [docs/community-vs-paid.md](./docs/community-vs-paid.md) for what is in scope here.
- Every commit needs a `Signed-off-by` trailer (`git commit -s`); the secret-scan CI check in `.github/` must pass before a PR merges.
- Back every factual claim you write with a file you actually read in this repo; if you cannot find one, say so instead of asserting it.
- To run the product from this tree, follow [docs/install-guide.md](./docs/install-guide.md): it is `docker compose up` from the compose file in this repo.
- Keep docs and code in the same change: a behavior change needs the doc page that describes it updated in the same pull request.
