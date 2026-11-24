<!-- GENERATED from RELEASE-NOTES.tmpl by open-core/render_presentation.py: edit the template or docs/assets/presentation.config.yaml, then re-render. -->
# MOD Core CE 1.0.1

MOD Core CE is the free, open source edition of MOD Core. Build workflows and run them on your own hardware, on your CPUs by default.

![MOD Core CE 1.0.1: Every Machine You Own, Working As One](docs/images/ce-banner.svg)

[![Version](https://img.shields.io/badge/version-v1.0.1-blue.svg)](./CHANGELOG.md) [![Apache-2.0](https://img.shields.io/badge/Apache--2.0-green.svg)](./NOTICE) [![Self-hosted](https://img.shields.io/badge/self--hosted-your%20machines-informational.svg)](./README.md)

> **The "Oz" Release.** Every Machine You Own, Working As One. The first public release of the free, open source edition of MOD Core: build workflows on a visual canvas and run them on the computers you already own. Your data lives on machines you own and MOD sends no telemetry. The full release page text is [docs/release-body-v1.0.0.md](./docs/release-body-v1.0.0.md).

This page is the v1.0.1 update over that release. What changed since 1.0.0: the model cache now starts after the install's storage credentials exist, so a fresh install no longer shows an S3 AccessDenied error; the platform health probe retries once when the Docker socket listing is slow; the Managers bootstrap and self-config routes are covered by tests; the sealed-mode docs page compiles in the docs image; the release tooling can stamp an image tag different from the git tag. On the CE tree itself: no Burst to MOD Cloud code remains (CE never used it) and the installer step for it is gone, and wording that implied the images were already signed is corrected (cosign signatures are added by digest after release).

## 🚀 What's New

- **Visual workflow builder (Toto):** drag nodes onto a canvas, connect them, run and save.
- **Workers on your own machines (Quadlings):** a Go worker runs containerised nodes (CPU by default).
- **Node builder (Tinker):** on from the first start ([docs/node-builder.md](./docs/node-builder.md)).
- **Stacks (Emeralds):** long-lived services on your hardware, with a host-port check before deploy.
- **One-script install:** `./scripts/install.sh`, with random first-run credentials.

## ⚠️ Breaking Changes

None: v1.0.1 is a bug-fix release over v1.0.0. See "Known limits" below for what to expect.

---

## 🛠 Changelog

<details>
<summary><b>🐛 Bug Fixes (Click to expand)</b></summary>

- Workflow output sent with Return Assets now arrives in the Files area.
- Stack deploy refuses up front and names whoever already holds the host port.
- The stack More menu is always visible and closes before the chosen action runs.
- The Keep running toggle shows a clear message when turned off.

</details>

<details>
<summary><b>⚙️ Under the Hood</b></summary>

- Images are published to `ghcr.io/modtim`; Apache-2.0; see [docs/sbom.md](./docs/sbom.md) to generate an SBOM for the images you run.
- How releases are drafted and published: [docs/releasing.md](./docs/releasing.md).

</details>

---

## Highlights

<!-- Each line's source: overlay/CHANGELOG.md "### Highlights" (lines 12-19 area). -->
- Self-hosted MOD Core stack that starts after one first-run script, `scripts/first-run-secrets.sh` (CHANGELOG.md highlight 1), or with `./scripts/install.sh`.
- Visual workflow builder (WorkflowBuilder) for composing and running node pipelines (CHANGELOG.md highlight 2).
- Dashboard for runs, deployments and account settings, signed in through Keycloak single sign-on (CHANGELOG.md highlight 3).
- Go worker that executes containerized nodes and reports results back to the API (CHANGELOG.md highlight 4).
- Random infrastructure credentials generated on first run; no shared default passwords (CHANGELOG.md highlight 5).
- Secrets held in OpenBao, with S3-compatible object storage provided by versitygw (Apache-2.0) (CHANGELOG.md highlight 6).
- Memory profiles (small, medium, large) chosen automatically from the RAM the stack can see (CHANGELOG.md highlight 7).
- A guide to generating an SBOM for the images you run ([docs/sbom.md](./docs/sbom.md)).
- No seat, worker, concurrent-run or storage limits from the product's own licensing; ordinary API rate limits (6000 per minute per key) and a 1 TiB workflow checkpoint quota apply (an install holds one tenant) (docs/community-vs-paid.md, "Why the split").

## Install

```bash
git clone https://github.com/modtim/MOD_CORE_CE.git
cd MOD_CORE_CE
./scripts/install.sh
```

With no flag the installer detects this computer's network address and opens
MOD to it (one printed notice); on a terminal it asks "Open this install to other
computers on your network? [Y/n]". `--no-lan` keeps MOD on this computer only
(scripts/install.sh header). `--yes` takes the same default. On a first install the
script can stop and print one `sudo bash ...` line for the encrypted-storage
folders: run it, then run the installer again. A first run usually takes 10-20
minutes (several GB of images), longer on a slow connection.
Prerequisites and the manual steps are in [README.md](./README.md) and [docs/install-guide.md](./docs/install-guide.md).

## What is included

Nineteen compose services start by default on a default install (the node
builder is on from day 1): twelve long-running, the builder pair (`buildkitd-net`,
`buildkitd`) and five one-shot jobs which exit when done. Seventeen start if you
turn the node builder off. They are: PostgreSQL 17 for the app (`postgres-app`)
and a separate PostgreSQL 17 for Keycloak (`keycloak-db`), Valkey 8 (`redis`),
mailpit (local SMTP relay), versitygw, OpenBao with its one-shot init job,
Keycloak 26 with its one-shot admin-init job, the API (FastAPI), the Worker
(Go), the Dashboard, the docs site (`mod-docs`) and the WorkflowBuilder; the
five one-shot jobs are `openbao-init`, `keycloak-admin-init`, `mod-migrate`,
`db-owner-repair` and `ce-node-repair`.

## Components

See [docs/sbom.md](./docs/sbom.md) for how to generate an SBOM for the images you run.

## Known limits

- Nodes run on CPU only by default; GPU workers are a preview and not yet proven end to end ([docs/operations.md](./docs/operations.md), "GPU").
- The node wizard needs a model bound first; without one it has nothing to
  write the Dockerfile with ([docs/node-builder.md](./docs/node-builder.md)).
- Basic audio and RAG nodes are not in the seeded node list ([docs/community-vs-paid.md](./docs/community-vs-paid.md)).
- Some service containers run as root inside their containers. The compose file sets user 0 for the mod-worker, openbao-init, openbao-unseal, openbao-data-perms and registry-init services. The MOD-built dashboard, docs-site, buildkit-fw, valkey-enc, versitygw-enc, video-converter-h264 and ffmpeg_cpu-h264 images set no USER in their Dockerfiles and build on nginx, alpine or debian bases, which default to root, so they also run as root. Images built by MOD use the tag in CE_IMAGE_TAG (latest when unset) and are not pinned by digest in 1.0.1; third-party images in the compose file are digest-pinned.
- The break-glass OpenBao AppRole pair never expires (`secret_id_ttl=0`, no use limit), so setup can log in again at any time; each login token is capped at 15 minutes and revoked at the end of the run. A CE install holds one tenant, and some first-run steps pass secrets on the command line (visible in `ps`).
- The Docker socket policy proxy is off by default (worker.docker_proxy_mode in platform settings), so the platform's own task containers that need a Docker daemon (system build nodes, the ImageBuilder, the Trivy image scan and the hardware benchmark) use the raw socket and no policy runs; nodes and stacks you author cannot request the socket. Arming report mode routes those calls through the proxy, which logs what the policy would deny and forwards every request; enforce mode rejects requests that bind host paths on a deny list, add capabilities outside an allowlist, or call Docker API endpoints outside an allowlist.
- Bursting to your own cloud account (Balloon Cloud) is a preview: only a static page check exists, no end-to-end run is recorded.

## Upgrading

See [docs/operations.md](./docs/operations.md) and the upgrade notes in
[docs/install-guide.md](./docs/install-guide.md) (a re-run keeps your existing values).

## License

Apache License 2.0, see [LICENSE](./LICENSE) and [NOTICE](./NOTICE). Trademark
wording: see [TRADEMARK.md](./TRADEMARK.md).

v1.0.1 - bug-fix release over v1.0.0, the first public release. Full changelog: [CHANGELOG.md](./CHANGELOG.md).
