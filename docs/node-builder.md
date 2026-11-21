# Building your own nodes with the Node Wizard

The Node Wizard turns a script, a repository or a Dockerfile into a MOD node.
You describe what you have, an LLM you choose (your own API key, or a local
Ollama / OpenAI-compatible endpoint) drafts the Dockerfile and entrypoint, and
MOD builds the image, tests it, and registers it so it shows up in the palette.

Community Edition includes all of it: the wizard screens, the API, and the
container images that do the work. This page explains what runs where, what you
need, and — plainly — what the limits are.

## What you get

| Piece | Image | What it does |
|---|---|---|
| Generator | `ghcr.io/modtim/containerizer` | Reads your source, asks your LLM to write the Dockerfile and entrypoint |
| Builder | `ghcr.io/modtim/build-task` | Builds the image, smoke-tests it, runs a sample, registers the node |
| Base images | `ghcr.io/modtim/base-*` | MOD's curated starting points the generated Dockerfiles build `FROM` |

All of them are published, signed, and pullable without a login, like every other
Community Edition image. `docker-compose.yml` already points the API at the first
two (`BUILD_TASK_IMAGE` and `CONTAINERIZER_IMAGE`), so there is nothing to
configure for the default setup.

The base images are `base-python:3.11`, `base-python:3.12`, `base-node:20`,
`base-node:22`, `base-go:1.22`, `base-rust:1.78`, `base-java:21`,
`base-alpine:3.19`, `base-distroless-python:3.11`, `base-distroless-node:20`,
and the accelerator ones `base-cuda:12.4-python3.11`,
`base-rocm:6.1-python3.11`, `base-intel-xpu:2025.0`, `base-npu:openvino-2025`.
Each one is a pinned upstream vendor image plus security updates and a
non-root `mod` user. The accelerator images are `linux/amd64` only.

## Before your first build

1. **Bring your own model.** MOD does not ship or pick a model for the wizard;
   a fresh install has none bound, and the wizard says "No model is bound to the
   Node Wizard yet" until you bind one. Open **Gearworks - Settings > Gearworks - AI Configuration**,
   then either use the **Node Wizard** row of **Who uses what** (the **Easy** view,
   the default) or click **Advanced**, open the **Node Wizard** tab and fill in its
   **Builder model** form
   (**Provider**, **Model**, **Base URL**, **API key**, then click **Save**) for
   either a chat model served by one of your own stacks or your own OpenAI-compatible endpoint:
   - a local server (Ollama, vLLM, llama.cpp): base URL
     `http://<your-server-host>:<port>/v1`, model name as the server lists it;
   - a hosted provider: its `/v1` base URL, an API key (`<your-api-key>`) and the
     model name from its catalogue.

   Step-by-step:
   [Bind a model to the Wizard](../CORE/docs-site/docs/bind-a-model-to-the-wizard.md).
   Without a bound model the wizard has nothing to write the Dockerfile with.
2. **Make room.** The first build pulls `build-task` (about 0.5 GB) and
   `containerizer` (about 2.5 GB on disk — it ships a CPU-only PyTorch for its
   code-search step) and then whatever base image the generated Dockerfile
   uses. The GPU bases are several GB each. You can pull the two builder images
   ahead of time so the first build does not sit waiting on a download:

   ```bash
   docker pull ghcr.io/modtim/build-task:latest
   docker pull ghcr.io/modtim/containerizer:latest
   ```

   (Use your `CE_IMAGE_REGISTRY` / `CE_IMAGE_TAG` values if you changed them.)

## Where your built nodes live (the default)

**On the machine that built them.** When the wizard finishes, the new node is a
Docker image in the local image store of the worker that ran the build — on a
normal install, the embedded worker on the same machine as MOD. The worker is set
to prefer local images (`MOD_WORKER_PREFER_LOCAL_IMAGES=1`), so it runs your node
straight from that store. No registry is involved and nothing is uploaded.

What that means in practice:

- **One machine works out of the box.** Build a node, drop it in a workflow, run it.
- **Pruning deletes your node.** `docker system prune -a`, or removing the image,
  removes the node's code. Keep the source you gave the wizard, or save the image
  with `docker save`.
- **A second worker machine cannot run it.** Another worker has no copy of the
  image and nowhere to pull it from. If you add workers and want them to run the
  nodes you build, turn on the registry below.

## Optional: a small registry for more than one worker

The `registry` compose profile starts a plain `registry:2` (the standard open
source image registry — not Harbor) that your builds push to and every worker
pulls from. It is **off by default**; a single-machine install does not need it.

1. The installer already generated the password for you. The `registry` profile
   reads `REGISTRY_USER` and `REGISTRY_PASSWORD` from `.env`; `first-run-secrets.sh`
   (run by `install.sh`) writes a fresh random `REGISTRY_PASSWORD` into `.env`
   (mode 0600) along with every other credential, so there is nothing to choose and
   no example value on this page is ever a working password. Set `REGISTRY_USER`
   only if you want a name other than `mod`, and set `LOCAL_REGISTRY` to the address
   the other machines will use:

   ```bash
   REGISTRY_USER=mod
   # REGISTRY_PASSWORD is already in .env (generated; do not commit .env)
   # Address other machines will use to reach this one:
   LOCAL_REGISTRY=192.0.2.10:5000
   ```

   To read the generated value, look up the `REGISTRY_PASSWORD` line in `.env`. The
   registry is plain HTTP on the LAN unless you add a certificate.

2. Start it:

   ```bash
   docker compose --profile registry up -d registry
   docker compose up -d        # so the API picks up LOCAL_REGISTRY
   ```

   `registry-init` writes the password file once and exits; `registry` then
   listens on port 5000 (change it with `REGISTRY_PORT`). It listens on **every**
   network interface of the machine so other workers can reach it. To keep it to
   this machine only, set `REGISTRY_BIND=127.0.0.1`.

3. **On every machine that runs a worker, log in once:**

   ```bash
   docker login 192.0.2.10:5000 -u mod      # prompts for the password
   ```

   The login is stored in that machine's Docker config and is what lets its Docker
   pull your nodes.

### Plain HTTP versus TLS

Out of the box the registry speaks plain HTTP, and the password travels in the
clear. That is acceptable on a closed home or lab network and nowhere else.

- Docker only talks plain HTTP to `localhost`. For any other address, each worker
  machine must list the registry as insecure in `/etc/docker/daemon.json`, then
  restart Docker:

  ```json
  { "insecure-registries": ["192.0.2.10:5000"] }
  ```

- **Better: use TLS.** Put a certificate and key for the registry's name in a
  directory, point the profile at it, and workers then need no special Docker
  setting if the certificate is from a CA they trust (for a private CA, install
  the CA on each worker machine):

  ```bash
  REGISTRY_TLS_DIR=./registry-tls          # contains server.crt and server.key
  REGISTRY_TLS_CERT=/certs/server.crt
  REGISTRY_TLS_KEY=/certs/server.key
  ```

  Then `docker compose --profile registry up -d registry` again.

## Limits, honestly

- **Single machine = local only.** Without the registry profile, built nodes exist
  only on the machine that built them.
- **Multi-worker needs the registry profile** (or a registry you already run).
  Nothing syncs images between workers on its own.
- **One shared login.** The registry has a single user. It does not separate teams
  or tenants; anyone with the password can read and overwrite every image in it.
- **No built-in cleanup or backup.** Images pile up in the `registry-data` volume.
  Back that volume up if you care about the nodes in it; deleting it deletes them.
- **Builds run on the worker machine.** With the `builder` profile on, builds go
  through the isolated rootless BuildKit sidecar (`buildkitd`), which holds no
  Docker socket and whose network egress is firewalled by `buildkitd-net`.
  `MOD_REQUIRE_ISOLATED_BUILD` defaults to 1, so a build refuses to run in the
  host Docker daemon when no BuildKit host is set. Separately, the embedded
  worker (`mod-worker-embedded`) holds the machine's Docker socket to run nodes.
  Either way the build executes Dockerfiles that an LLM wrote. Treat the machine
  that builds nodes accordingly: do not run it on a host that also holds data you
  could not afford to lose, and read what the wizard generated before you trust a
  node with sensitive data.
- **The LLM has to be reachable** from the build, and what it writes is only as
  good as the model you gave it. Failed builds are normal; the wizard retries and
  shows you the log.
- **GPU bases are `linux/amd64` only**, and a node built `FROM` one only runs on a
  worker with a matching GPU.

## Related

- [Authoring nodes by hand](./authoring-nodes.md) — no wizard, no LLM, no builder images.
- [Community Edition vs. other editions](./community-vs-paid.md).
