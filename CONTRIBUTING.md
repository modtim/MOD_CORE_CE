# Contributing to MOD Core

Thanks for your interest in contributing to MOD Core. This document covers the
mechanics of getting a change merged: sign-off, the PR process, and how
maintainer trust is earned over time.

## Developer Certificate of Origin (DCO) — no CLA

MOD Core uses the [Developer Certificate of Origin (DCO)](https://developercertificate.org/)
instead of a Contributor License Agreement (CLA). There is no separate
paperwork to sign. By signing off a commit, you certify that you wrote it (or
otherwise have the right to submit it) under the project's license.

Every commit must carry a `Signed-off-by` trailer. The easiest way is to
commit with `-s`:

```bash
git commit -s -m "Add thing"
```

This appends a line to your commit message:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name and a reachable email address — anonymous or pseudonymous
sign-offs are not accepted. If you forgot to sign off, you can amend:

```bash
git commit --amend -s
```

For a multi-commit branch, sign off every commit (`git rebase --exec 'git commit --amend --no-edit -s' -i <base>`
is a fast way to backfill). Pull requests with unsigned commits will be asked
to fix sign-off before review continues; maintainers check the Signed-off-by line
when they review a pull request.

## Before you start

- For anything beyond a small fix (new node, behavior change, API surface
  change), open an issue or discussion first so the approach can be agreed on
  before you invest the work.
- Check existing issues and open PRs to avoid duplicate effort.
- Read [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — it applies to all project
  spaces (issues, PRs, discussions, chat).

## Development setup

See the [README](./README.md#-from-download-to-running-in-three-steps) for the `docker compose up`
quickstart, and [`docs/`](./docs) for node authoring and configuration guides.

## Pull request process

1. **Fork and branch.** Branch from `main`; use a short, descriptive branch
   name.
2. **Keep changes focused.** One logical change per PR. Large, unrelated
   changes are harder to review and more likely to stall.
3. **Sign off every commit** (see DCO above).
4. **Tests.** The secret-scan workflow (`.github/workflows/secret-scan.yml`)
   must pass. Add or update tests for the behavior you change.
5. **Describe the change.** In the PR description, explain *what* changed and
   *why* — link the issue it addresses if there is one.
6. **Review.** A maintainer will review, request changes if needed, and merge
   once approved and the secret-scan workflow passes. Small/obvious fixes may be merged with a
   single maintainer approval; larger or riskier changes may need broader
   review or a maintainer discussion (see [GOVERNANCE.md](./GOVERNANCE.md) for
   the lazy-consensus decision model).
7. **Scope boundary.** MOD Core (this repository) is the Apache-2.0 Community
   Edition. Contributions that only make sense for the proprietary MOD
   Cloud/Paid product are out of scope for this repo —
   see the [Community Edition vs. paid](./docs/community-vs-paid.md)
   page.

## Reporting bugs and requesting features

Use GitHub Issues on the public repository. Include repro steps, expected vs.
actual behavior, and relevant logs/config for bugs. Do **not** report security
vulnerabilities as public issues — see [SECURITY.md](./SECURITY.md).

## Contributing a node

Nodes are the primary extension point. See
[docs/authoring-nodes.md](./docs/authoring-nodes.md) for the container +
metadata format, and open an issue first if you're proposing a new node for
the core "batteries-included" pack (vs. maintaining it out-of-tree).

## Becoming a maintainer

MOD Core's maintainer path is meritocratic, not appointed by fiat. There is
currently one Lead Maintainer (see [MAINTAINERS.md](./MAINTAINERS.md)). As
contributors accumulate a track record of substantial, well-reviewed merged
PRs and demonstrate good judgment in reviews/discussions, they may be
nominated for maintainer status by the Lead Maintainer or an existing
maintainer. The specific criteria and process are documented in
[MAINTAINERS.md](./MAINTAINERS.md) and [GOVERNANCE.md](./GOVERNANCE.md). Decisions
are made by the maintainers; there is no steering committee.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License, Version 2.0](./LICENSE), the same license as the rest of the
project. See [NOTICE](./NOTICE) for trademark and third-party attribution.
