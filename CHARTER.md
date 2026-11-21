# Technical Charter

This is the MOD Core project's **technical charter** — the single document an
readers use to understand the project's mission, its
governing body, its roles, and its IP policy. It is a *charter*, not a
narrative: where it states a rule, that rule is the rule. Every rule in this
version is decided — there are no open items and nothing here may be treated as
pending.

For the day-to-day description of how decisions get made today, see
[GOVERNANCE.md](./GOVERNANCE.md); this charter is consistent with it and does
not replace it.

## 1. Mission and scope

**Mission.** MOD Core is an open-source, batteries-included media/workflow
engine: nodes run as containers, the WorkflowBuilder composes them, and the
Community Edition is the Apache-2.0 edition of that engine, developed in the
open by Mod Tech Labs Inc.
([GOVERNANCE.md § Project structure](./GOVERNANCE.md)).

**Scope — in.** Everything in this repository: the engine/API tree, the
WorkflowBuilder, the Dashboard, the Worker, node metadata, node image build
contexts, and the CE docs. Which paths ship is decided by the open-core build
manifest (a build input, not a file in this distribution).

**Scope — out.** Contributions that only make sense for the proprietary MOD
Cloud/Paid product are out of scope for this repository
([CONTRIBUTING.md § Scope boundary](./CONTRIBUTING.md)). The
Community Edition / paid boundary is a governance-level decision, not a
contribution-level one ([GOVERNANCE.md](./GOVERNANCE.md)).

**Scope.** MOD Core is an open-source project developed by Mod Tech Labs Inc.; day-to-day governance is described in
[GOVERNANCE.md](./GOVERNANCE.md).

## 2. Governance today

MOD Core currently runs a **Lead Maintainer + lazy consensus** model
([GOVERNANCE.md § Current model](./GOVERNANCE.md)):

- Any maintainer may propose a change via pull request, or a decision via
  issue/discussion.
- A proposal is accepted if no maintainer objects within a reasonable review
  window — typically a few days for routine changes, longer for larger design
  decisions.
- Silence is consent; no explicit "+1" from every maintainer is required.
- An objection means the proposer and objector work to consensus through
  discussion.
- If consensus cannot be reached, the Lead Maintainer has final decision-making
  authority and breaks the tie; this is a deliberate fallback, used rarely.

The model is intentionally lightweight: it scales down to one active
maintainer and scales up as maintainers join, without a governance rewrite at
each step.

## 3. No steering committee

The project has no steering committee, no voting body and no foundation. MOD
Tech Labs maintains it, the maintainers decide, and the community gives input
through issues and discussions. See [GOVERNANCE.md](./GOVERNANCE.md).

## 4. Roles

| Role | What it means | How it is obtained | Source |
| --- | --- | --- | --- |
| **Contributor** | Anyone who opens a signed-off pull request or issue under [CONTRIBUTING.md](./CONTRIBUTING.md). | No appointment; self-service. | CONTRIBUTING.md |
| **Maintainer** ("committer") | Repository write/merge access, scoped to an area at the Lead Maintainer's discretion. | Earned by track record; nominated and approved per MAINTAINERS.md. | MAINTAINERS.md |
| **Lead Maintainer** | Final review/merge authority, issue triage, release cutting, nomination approvals, and project representation to external parties. | Currently one holder; listed in MAINTAINERS.md. | MAINTAINERS.md |

Maintainer access may be scoped to a subsystem (node packs, the WorkflowBuilder,
the Go worker) rather than blanket repository-wide (MAINTAINERS.md).
Inactive maintainers may be moved to an emeritus section and return without
repeating nomination (MAINTAINERS.md).

## 5. Decision process

- **Routine contributions** (bug fixes, small features, node additions, docs)
  go through the normal pull-request process with standard maintainer review
  ([GOVERNANCE.md](./GOVERNANCE.md);
  [CONTRIBUTING.md § Pull request process](./CONTRIBUTING.md)).
- **Larger decisions** — a breaking API change, a new maintainer, a change to
  the governance model, a license or trademark policy change, or anything
  affecting the CE/paid boundary — are raised as a written proposal (issue or
  design doc) so the history stays legible even while there is one maintainer
  (GOVERNANCE.md).
- **Tie-break:** the Lead Maintainer (GOVERNANCE.md).

## 6. Amendments to this charter

Changes to this charter follow the same process as changes to
[GOVERNANCE.md](./GOVERNANCE.md): propose in writing, allow time for objection,
and the Lead Maintainer resolves disagreement
(GOVERNANCE.md).

## 7. IP policy

### 7.1 License for code

The source code in this repository is licensed under the **Apache License,
Version 2.0** ([LICENSE](./LICENSE); [GOVERNANCE.md § License and trademark
governance](./GOVERNANCE.md)), with two carve-outs: the third-party
components listed in [NOTICE](./NOTICE), each of which is under its
own license, and the MOD name and logos, which are not licensed under Apache-2.0
at all ([NOTICE](./NOTICE);
[TRADEMARK.md](./TRADEMARK.md)). The governance model does not change the
license. By contributing, you agree your contributions are licensed
Apache-2.0, the same license as the rest of the project
([CONTRIBUTING.md § License](./CONTRIBUTING.md)).

### 7.2 License for docs and diagrams

Documentation, diagrams, node metadata, and every other non-code artifact in
this repository are licensed **Apache-2.0**, the same license as the code.
[NOTICE](./NOTICE), already covers "the files in this distribution",
so no artifact needs a second license and no artifact may be treated as
unlicensed. The two carve-outs in §7.1 still apply: the third-party components
listed in [NOTICE](./NOTICE), keep their own licenses, and the MOD
name and logos are not licensed at all — see
[TRADEMARK.md](./TRADEMARK.md) and [NOTICE](./NOTICE).

### 7.3 Sign-off: DCO, not CLA

MOD Core uses the **Developer Certificate of Origin (DCO)** instead of a
Contributor License Agreement (CLA). There is no separate paperwork to sign; by
signing off a commit you certify that you wrote it, or otherwise have the right
to submit it, under the project's license
([CONTRIBUTING.md § Developer Certificate of Origin (DCO) — no CLA](./CONTRIBUTING.md)).
Every commit must carry a `Signed-off-by` trailer
(CONTRIBUTING.md); real names and reachable email addresses are
required, anonymous or pseudonymous sign-offs are not accepted (CONTRIBUTING.md). DCO sign-off is required by project policy and checked in review,
and pull requests with unsigned commits are asked to fix sign-off before review
continues (CONTRIBUTING.md).

## 8. Trademark, antitrust and compliance

- The MOD name and logos are owned and retained by **Mod Tech Labs Inc.**,
  independent of the source-code license
  ([GOVERNANCE.md](./GOVERNANCE.md);
  [TRADEMARK.md](./TRADEMARK.md)). Using the source code grants no
  right to use the marks ([TRADEMARK.md](./TRADEMARK.md);
  [NOTICE](./NOTICE)).
- Trademark policy changes follow the same review process as other governance
  changes (GOVERNANCE.md).
**Compliance statement.** The project asserts exactly two legal
policies: **Apache-2.0 for the code and the non-code artifacts in this
repository** (§7.1, §7.2), and **[TRADEMARK.md](./TRADEMARK.md) for the MOD
Marks**, which is the project's only non-code legal policy. The project makes no
other legal claim — no export-control obligation is asserted beyond
what Apache-2.0 itself carries, and no security-compliance or cryptographic
certification claim is made for this repository.

## 9. Code of conduct

The project's code of conduct is the existing
[CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md) — Contributor Covenant, version 2.1
(CODE_OF_CONDUCT.md). It applies within all community
spaces and to everyone who maintains or represents the project, including the
Lead Maintainer (CODE_OF_CONDUCT.md).
This charter does not restate it and must not be read as weakening it.

## 10. What this charter does not change

The authoritative description of how decisions are made is
[GOVERNANCE.md](./GOVERNANCE.md), plus the maintainer criteria in
[MAINTAINERS.md](./MAINTAINERS.md). Nothing in this charter
changes the license, the trademark policy, or the CE/paid boundary.
