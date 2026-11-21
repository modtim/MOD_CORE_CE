# Maintainers

This file lists the current maintainers of MOD Core and describes how
maintainer status is granted. See [GOVERNANCE.md](./GOVERNANCE.md) for how
maintainers make decisions together.

## Lead Maintainer

| Name | GitHub | Affiliation | Role |
| --- | --- | --- | --- |
| Timothy Porter | `@modtim` | Mod Tech Labs Inc. | Lead Maintainer |

The Lead Maintainer has final decision-making authority under the current
lazy-consensus governance model (see [GOVERNANCE.md](./GOVERNANCE.md)) and is
responsible for:

- Final review/merge authority on pull requests.
- Triaging and prioritizing issues.
- Cutting releases.
- Approving or denying maintainer nominations.
- Representing the project to external parties.

Contact: `admin@modtechlabs.com`

## Maintainers

_(none yet — MOD Core currently has a single Lead Maintainer. This table will
grow as contributors are promoted; see "Becoming a maintainer" below.)_

| Name | GitHub | Affiliation | Areas |
| --- | --- | --- | --- |
| — | — | — | — |

## Becoming a maintainer

MOD Core's maintainer path is meritocratic: maintainer status is earned by
demonstrated, sustained contribution and judgment, not granted by request,
seniority, or employer.

### Criteria

A contributor is generally eligible for nomination once they have, over a
sustained period (guideline: at least 2–3 months of active involvement):

- **Substantial merged contributions** — a track record of non-trivial PRs
  merged with minimal rework (bug fixes, features, node additions, or
  significant docs/infra work), not just typo fixes.
- **Review participation** — consistently helpful, technically sound reviews
  on other contributors' PRs, not just their own.
- **Good judgment in community spaces** — issue triage, design discussion, and
  conduct consistent with the [Code of Conduct](./CODE_OF_CONDUCT.md).
- **Reliability** — responsive and following through on commitments (reviews
  requested, issues assigned).

There is no fixed PR count threshold; the Lead Maintainer (see [GOVERNANCE.md](./GOVERNANCE.md)) weighs quality and consistency
over volume.

### Process

1. **Nomination.** Any existing maintainer (currently, the Lead Maintainer)
   may nominate a contributor, or a contributor may ask to be considered.
2. **Review.** Existing maintainers discuss the nomination against the
   criteria above, referencing specific merged PRs and review history.
3. **Decision.** Under lazy consensus, the nomination is accepted if no
   maintainer objects within the review window; the Lead Maintainer
   tie-breaks in case of disagreement.
4. **Onboarding.** New maintainers are added to this file, granted repository
   write/merge access scoped to their area, and pointed to
   [GOVERNANCE.md](./GOVERNANCE.md) for how decisions are made.

### Scope of maintainer access

Maintainer access may be scoped (e.g., to a subsystem such as node packs, the
WorkflowBuilder, or the Go worker) rather than blanket repository-wide access,
at the Lead Maintainer's discretion, especially as the maintainer group grows.

### Stepping down / inactivity

Maintainers who become inactive for an extended period (guideline: 6+ months
with no reviews/commits and no communicated leave) may be moved to an
"emeritus" section by the Lead Maintainer, with a good-faith attempt to
contact them first. Emeritus maintainers are welcome to become active again
without repeating the full nomination process.

## Emeritus maintainers

_(none yet)_

## How the maintainer group is run

Maintainers are chosen by the Lead Maintainer. There is no steering committee
and no voting body; see [GOVERNANCE.md](./GOVERNANCE.md#how-decisions-are-made-today).
