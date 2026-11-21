# Governance

This document describes how decisions get made in the MOD Core project today,
and how governance is expected to evolve as the contributor base grows.

## Project structure

MOD Core is an open-source project (Apache-2.0) developed by Mod Tech Labs Inc. in the open. Technical governance is described below.day-to-day technical governance is described below.

## Current model: Lead Maintainer + lazy consensus

MOD Core currently has a single **Lead Maintainer** (see
[MAINTAINERS.md](./MAINTAINERS.md)) and operates under **lazy consensus**:

- Any maintainer may propose a change (via PR) or a decision (via issue/
  discussion).
- The proposal is considered accepted if no maintainer objects within a
  reasonable review window (typically a few days for routine changes; longer
  for larger design decisions).
- Silence is treated as consent — proposals do not require an explicit "+1"
  from every maintainer to move forward.
- If an objection is raised, the proposer and objector work to reach
  consensus through discussion.
- If consensus cannot be reached, the **Lead Maintainer has final
  decision-making authority** and breaks the tie. This is a deliberate
  fallback to keep the project moving, not a default mode of operation —
  the expectation is that most decisions are made through ordinary
  discussion and review, and the tie-break is used rarely.

This is intentionally lightweight. It scales down to a single active
maintainer (today) and scales up as more maintainers join, without requiring a
governance rewrite for every step.

## What requires broader review

Routine contributions (bug fixes, small features, node additions, docs) go
through the normal [pull request process](./CONTRIBUTING.md#pull-request-process)
with standard maintainer review.

Larger decisions — a breaking API change, a new maintainer, a change to this
governance model, a license or trademark policy change, or anything affecting
the CE/paid boundary — should be raised as a written proposal (issue or design
doc) so there's a record, even while there's only one maintainer, so the
history is legible once more maintainers join.

## Becoming a maintainer

Maintainer status is earned through a track record of contribution, not
granted by request. See [MAINTAINERS.md](./MAINTAINERS.md) for the specific
criteria and current process (today: nomination and approval by the Lead
Maintainer).

## How decisions are made today

MOD Tech Labs maintains this project. The maintainers decide, and the Lead
Maintainer breaks ties. Community input comes through issues and discussions:
anyone can open one, and maintainers read them. There is no steering
committee, no voting body and no foundation. If that changes, this file will be
updated first; nothing here promises that it will.

## License and trademark governance

- **Source code license:** [Apache License, Version 2.0](./LICENSE) for
  everything in this repository. This governance model does not change the
  license; license changes would require the same broader-review process
  described above.
- **Trademark:** the MOD name and logos are owned and retained by Mod
  Tech Labs Inc., independent of the source code
  license, per [TRADEMARK.md](./TRADEMARK.md). Trademark policy changes follow
  the same review process as other governance changes.

## Amending this document

Changes to GOVERNANCE.md follow the "larger decisions" process above: propose
in writing, allow time for objection, the Lead Maintainer resolves disagreement.
