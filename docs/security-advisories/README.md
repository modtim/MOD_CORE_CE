# Security Advisories (MOD-SA)

Conventions for public security advisories published with each MOD Core
release. This folder is part of the open-core overlay: everything in it is
public — never reference internal triage detail, customer data, or
unpatched-internal findings.

## Naming

`MOD-SA-YYYY-NNN.md` — calendar year of first publication, three-digit
sequential number (001, 002, …). Never reuse or silently rewrite a
published advisory: when an advisory changes, bump nothing — add a
"Supersedes / Revised" line and commit the change; the git history of
this file is the record of every revision.

## Lifecycle

1. **Draft** — written from the maintainers' internal VEX
   triage entry while still private.
2. **Published** — committed here and referenced from
   the release notes. The per-release VEX statement for the same
   vulnerability id must agree with the advisory's disposition (affected /
   not_affected / fixed) — VEX is the machine-readable form, the advisory
   is the human form.
3. **Superseded** — a later advisory that obsoletes it names it; the old
   file is kept (append-only, ISO 27001 A.12.4.1 retention).

## Contents

Copy `MOD-SA-0000-000.template.md`. Every published advisory states, for
the running release line: the affected versions, the impact, the fix
version (or "no fix; mitigation: …"), and the triage status as of
publication. Wording is "the release supports …" — never "compliant" or
"certified" (GY.D1).

## Signing

CE advisories are published as plain files in this repository and are not
cryptographically signed. Release images are not signed either (see
[docs/sbom.md](../sbom.md)). Read an advisory from this repository at the
release tag you run, and report a suspected forged advisory through
[SECURITY.md](../../SECURITY.md).
