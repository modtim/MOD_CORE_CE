# Software Bill of Materials (SBOM)

This page explains how to produce a Software Bill of Materials (SBOM) so you can
audit which third-party components (and license terms) are in the images you run.
The NOTICE file lists the third-party components and their licenses. Release
images are not signed and no per-release SBOM files are published; generate your
own with the steps below. This document also explains how to generate your own SBOM (for a local build,
a fork, or an image you built yourself); it does not embed a full SBOM (those
are large, generated artifacts).

## What's covered

The SBOM enumerates third-party dependencies pulled in by the components in
this repository's default (`docker compose up`) profile:

- **API** (Python / `pip`) — FastAPI, Uvicorn, SQLAlchemy, Pydantic, and
  transitive dependencies.
- **Worker** (Go / `go.mod`) — the Go standard library's module deps.
- **WorkflowBuilder** (Node/`npm`) — React, Next.js, and their dependency
  trees.
- **Dashboard** (Node/`npm`, build-time only) — Tailwind CSS (a development
  dependency) and its dependency tree.
- **Container base images** used by the default node pack and infra services
  (PostgreSQL, OpenBao, Keycloak) and the `valkey-enc` and `versitygw-enc`
  images (see `docker-compose.yml` for the exact image references).

See [NOTICE](../NOTICE) for the human-readable summary of notable components
and their licenses; the SBOM is the exhaustive, machine-readable version of
that same picture.

## Generating your own SBOM (optional)

Standard installs run the prebuilt release images. No per-release SBOM files
are published, so use this section to generate your own, for the images you run,
a local build, a fork, or a custom image.

We recommend [Syft](https://github.com/anchore/syft) (Anchore) to generate a
[CycloneDX](https://cyclonedx.org/) SBOM. Syft can scan source directories
directly (for the Python/Go/Node dependency manifests) and/or built container
images (to capture what's actually installed at runtime, including OS
packages).

### Source-directory scan (per component)

```bash
# API (Python)
syft dir:./CORE/API -o cyclonedx-json=sbom-api.cdx.json

# Worker (Go)
syft dir:./CORE/Worker -o cyclonedx-json=sbom-worker.cdx.json

# WorkflowBuilder (Node/Next.js)
syft dir:./CORE/WorkflowBuilder -o cyclonedx-json=sbom-workflowbuilder.cdx.json

# Dashboard (Node/Next.js)
syft dir:./CORE/Dashboard -o cyclonedx-json=sbom-dashboard.cdx.json
```

### Built-image scan (captures OS packages + pinned runtime deps)

```bash
# If you built your own images (e.g. `docker compose build` for a local
# fork), scan each built image, e.g.:
syft <api-image>:<tag>             -o cyclonedx-json=sbom-api-image.cdx.json
syft <worker-image>:<tag>          -o cyclonedx-json=sbom-worker-image.cdx.json
syft <workflowbuilder-image>:<tag> -o cyclonedx-json=sbom-workflowbuilder-image.cdx.json
syft <dashboard-image>:<tag>       -o cyclonedx-json=sbom-dashboard-image.cdx.json
```

### Merging into one SBOM

To produce a single combined CycloneDX document for a release, merge the
per-component outputs with the [CycloneDX CLI](https://github.com/CycloneDX/cyclonedx-cli):

```bash
cyclonedx merge \
  --input-files sbom-api.cdx.json sbom-worker.cdx.json \
                sbom-workflowbuilder.cdx.json sbom-dashboard.cdx.json \
  --output-file sbom-mod-core.cdx.json
```

The `cyclonedx merge` example above is untested: no script in this repository
runs that subcommand.

(`syft` can also emit SPDX instead of CycloneDX with `-o spdx-json=...` if
SPDX is preferred for a given consumer; pick one format consistently per
release.)

## Vulnerability scanning (optional, recommended)

Once an SBOM exists, [Grype](https://github.com/anchore/grype) (also Anchore,
pairs naturally with Syft) can scan it for known CVEs:

```bash
grype sbom:sbom-mod-core.cdx.json
```

## Publishing

The SBOM is a generated build artifact, not a hand-maintained file — it is
**not** checked into this repository. Generate one for the images you run with the steps above; no per-release SBOM files are published.
SBOM generation *supports* downstream auditing; it does not certify compliance.

## License compliance note

The SBOM is informational and does not modify the license terms of this
project's own code (Apache-2.0, see [LICENSE](../LICENSE)) or of any
third-party dependency (each under its own upstream license, per
[NOTICE](../NOTICE)). Always defer to each dependency's own license file for
authoritative terms.

The `SBOM + VEX (release)` workflow (`.github/workflows/sbom_vex.yml`) runs its
job `Release SBOM + VEX (syft/grype)` on release tag pushes (`v*`, plus manual
dispatch) and uploads the generated `sbom` and `vex` artifacts via
`actions/upload-artifact@v4`.
