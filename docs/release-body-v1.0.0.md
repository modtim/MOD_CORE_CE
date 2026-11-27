<!--
This is the text for the GitHub Release page of v1.0.0 (Releases -> Draft a new release).
Copy everything below the line that says PASTE FROM HERE. Not part of the product docs.
-->

<!-- PASTE FROM HERE -->

![MOD Core CE v1.0.0: Every Machine You Own, Working As One](https://raw.githubusercontent.com/modtim/MOD_CORE_CE/main/docs/images/ce-banner.png)

[![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)](https://github.com/modtim/MOD_CORE_CE/releases/tag/v1.0.0) [![Apache-2.0](https://img.shields.io/badge/Apache--2.0-green.svg)](https://github.com/modtim/MOD_CORE_CE/blob/main/NOTICE) [![Self-hosted](https://img.shields.io/badge/self--hosted-your%20machines-informational.svg)](https://github.com/modtim/MOD_CORE_CE/blob/main/README.md)

> **The "Oz" Release.** Every Machine You Own, Working As One. MOD Core Community Edition v1.0.0 is the first public release of the free, open source edition of MOD Core. You build a workflow on a visual canvas and run it on the computers you already own, with your data on machines you own and no telemetry. One install holds one organization, with no seat, worker, concurrent-run or storage limits from the product's own licensing.

## 🚀 What's New

- **Visual workflow builder (Toto).** Drag nodes onto a canvas, connect them, press Run, and save the workflow to use again. See [docs/features.md](https://github.com/modtim/MOD_CORE_CE/blob/main/CORE/docs-site/docs/features.md) for the walkthrough.
- **Workers on the machines you own (Quadlings).** A Go worker runs your containerised nodes and reports results back. Nodes run on CPU by default; hardware labels let you target a machine.
- **Node builder (Tinker).** Build your own nodes from a repository or a description. It is on from the first start; the node wizard needs a model bound first ([docs/node-builder.md](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/node-builder.md)).
- **Stacks (Emeralds).** Run long-lived services such as model servers on your hardware, with a host-port check before deploy.
- **Burst to your own cloud (Balloon Cloud, preview).** Optionally send jobs to your own cloud account with your own credentials; nothing comes from MOD. Only the page and its naming are checked by a static test so far; no end-to-end run is recorded.
- **Your data stays home.** MOD sends no telemetry. Files live in the S3-compatible storage that comes with the install, or in a bucket you point it at. Secrets are held in OpenBao. Sign-in is through Keycloak.
- **Bring your own LLM endpoints.** Point the assistant, node wizard and logs helper at an endpoint you run or choose.
- **One-script install.** `./scripts/install.sh` starts the whole stack (on a first install it may print one `sudo` line to run first, and a first start usually takes 10-20 minutes, longer on a slow connection). It detects this computer's network address so other computers on your network can open it, or keeps it local with `--no-lan`.
- **Random credentials on first run.** No shared default passwords; every infrastructure credential is generated on your machine.

## Known limits

We would rather tell you now: 1.0 is a first public release and you may hit rough edges. What CE does not include is listed in [Community Edition scope](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/community-vs-paid.md). Support is best effort: questions go in Discussions, bugs in Issues.

- Nodes run on CPU only by default; GPU workers are supported, and the worker detects NVIDIA, AMD, Intel and NPU accelerators (see [GPU in the operations guide](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/operations.md#gpu)).
- Ordinary API rate limits (6000 requests per minute per key) and a 1 TiB workflow checkpoint quota apply; there are no seat, worker, run-count or storage license limits.
- The node wizard cannot write a Dockerfile until you bind a model to it.
- Bursting to your own cloud account (Balloon Cloud) is a preview: only a static page check exists, no end-to-end run is recorded.
- Basic audio and RAG nodes are not in the starting node list.
- A repository node build for a Ruby project fails with an unhelpful error instead of a clear message.
- In the More menu, keyboard focus does not return to the More button after you close it with Esc, and the menu can overflow in a very short window.

## ⚠️ Breaking Changes

**None: this is the first public release.** There is nothing to migrate.

---

## 🛠 Changelog

<details>
<summary><b>🐛 Bug Fixes (Click to expand)</b></summary>

- Workflow output sent with Return Assets now arrives in the Files area, where you can browse and download it.
- Deploying a stack now refuses up front, naming the process or stack that already holds the host port, instead of failing half-way.
- The stack row actions moved into a More menu that is always visible, and the menu closes before the chosen action runs.
- The Keep running toggle now shows a clear message when you turn it off.
- The install script checks required ports before it starts anything.

</details>

<details>
<summary><b>⚙️ Under the Hood</b></summary>

- Images are published to `ghcr.io/modtim` (set `CE_IMAGE_REGISTRY` and `CE_IMAGE_TAG` to change where they come from).
- Licensed under Apache-2.0 ([LICENSE](https://github.com/modtim/MOD_CORE_CE/blob/main/LICENSE), [NOTICE](https://github.com/modtim/MOD_CORE_CE/blob/main/NOTICE)).
- `scripts/install.sh` is the one install path; `scripts/first-run-secrets.sh` creates the first-run credentials.
- See [docs/sbom.md](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/sbom.md) to generate an SBOM for the images you run.
- Memory profiles (small, medium, large) are picked from the RAM the stack can see.
- How a release is drafted and published: [docs/releasing.md](https://github.com/modtim/MOD_CORE_CE/blob/main/docs/releasing.md). The image digests list is attached to each release.

</details>

---

## 💖 New Contributors

Thank you to the **MOD Tech Labs team and early testers** who built, broke and fixed this in the open. Want to be listed here next time? Start with [CONTRIBUTING.md](https://github.com/modtim/MOD_CORE_CE/blob/main/CONTRIBUTING.md).


