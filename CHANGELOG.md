# Changelog

All notable changes to MOD Core Community Edition (CE) are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.1] - 2026-11-23

Bug-fix release over 1.0.0, the "Oz" first public release.

### Fixed

- Model cache: a fresh install no longer shows an S3 AccessDenied error; the cache service is (re)created after the install's storage credentials exist.
- Platform health probe: the sidecar probe retries once on a slow Docker socket listing instead of reporting the stack unhealthy.
- Managers: the bootstrap bundle and self-config routes are now covered by tests.
- Docs: the sealed-mode docs page compiles in the docs image.
- Release tooling: publish.sh can stamp an image tag different from the git release tag.

### Changed

- The CE tree no longer contains any Burst to MOD Cloud code (CE never used it); the installer step for it is removed.
- Wording corrected where it implied the images were already signed: cosign signatures are added by digest after release.

## [1.0.0] - 2026-11-20

First public release of MOD Core Community Edition by Mod Tech Labs Inc.

### Highlights

- Self-hosted MOD Core stack that starts after one command, `./scripts/install.sh` (it runs `scripts/first-run-secrets.sh`, which calls `scripts/first-run-sidecar.sh` itself; `first-run-openbao-init.sh` runs as the openbao-init container entrypoint).
- Visual workflow builder (WorkflowBuilder) for composing and running node pipelines.
- Dashboard for runs, deployments and account settings, signed in through Keycloak single sign-on.
- Go worker that executes containerized nodes and reports results back to the API.
- Random infrastructure credentials generated on first run; no shared default passwords.
- Secrets held in OpenBao, with S3-compatible object storage provided by versitygw (Apache-2.0).
- Memory profiles (small, medium, large) chosen automatically from the RAM the stack can see.
- A guide to generating an SBOM for the images you run.

### What's included

Seventeen compose services start by default: twelve long-running and five one-shot jobs (openbao-init, keycloak-admin-init, mod-migrate, db-owner-repair, ce-node-repair):

- PostgreSQL 17 (application database) and a separate PostgreSQL 17 for Keycloak
- Valkey 8 (cache and pub/sub)
- versitygw (S3-compatible object storage)
- OpenBao, plus its one-shot init job
- Keycloak 26 (authentication), plus its one-shot admin-init job
- API (FastAPI)
- Worker (Go)
- Dashboard
- WorkflowBuilder

### Requirements

- Docker with Docker Compose.
- At least 4 GB RAM and 2 CPU cores (the `small` profile).
- At least 20 GiB free disk; the first install downloads about 9 GB of images.

### Install

Follow the three install steps in [README.md](README.md#-from-download-to-running-in-three-steps).

### Components

See [docs/sbom.md](docs/sbom.md) to generate an SBOM for the images you run.

### Known issues

- WorkflowBuilder may ask for a second login after you sign in to the Dashboard. [remove if fixed before tag]
- The first image pull can take tens of minutes on a slow connection.
- Hosts below 4 GB RAM or 2 CPUs are not supported.

### License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright Mod Tech Labs Inc.

### Security contact

Report vulnerabilities to admin@modtechlabs.com. Please do not open a public
issue for a security problem. See [SECURITY.md](SECURITY.md).
