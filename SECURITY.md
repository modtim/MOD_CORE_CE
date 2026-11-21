# MOD Vulnerability Disclosure Policy

One policy, all surfaces. This covers every product and website we operate:

- **MOD SaaS** — the hosted multi-tenant service (API, Dashboard, Workflow
  Builder, workers, node images).
- **MOD on-prem** — the full self-hosted install (API, orchestrator, Go
  worker, WorkflowBuilder, Dashboard, and the node images maintained here).
- **MOD Core (Community Edition)** — the open-source codebase in this
  repository, and its releases.
- **Our public websites** — the modtechlabs.com marketing site and the
  documentation in [docs/](./docs) and [CORE/docs-site/](./CORE/docs-site).

Report to us through the channels below regardless of which surface a
vulnerability affects — a single mailbox routes everything.

## Out of scope

The following are out of scope for this policy. If you believe you have found
a real issue in one of these areas, you are still welcome to write to us —
we will triage it and tell you plainly if it is not something we can act on:

- Denial-of-service attacks.
- Social engineering, phishing, or pretexting.
- Physical attacks or data-center access.
- Vulnerabilities in third-party services and dependencies (report those
  upstream; a heads-up is always welcome so we can track the fix).
- Issues that are already known and being addressed, or that require access
  you do not legitimately hold.

## How to report

Email **`admin@modtechlabs.com`**. Please do **not** report security
vulnerabilities through public GitHub issues, discussions, or pull requests.

To speed triage, include, where applicable:

- A description of the vulnerability and its potential impact.
- The affected product, version, and environment (SaaS, on-prem, or CE).
- Steps to reproduce, or a proof-of-concept (see the safe-harbor rules below).
- Any known mitigations or workarounds.

We can also accept an **encrypted report on request** — ask in your initial
email and we will reply with a PGP key or an encrypted upload link.

## Our commitments

- **Response:** we triage reports on a best-effort basis. Community Edition has no
  support SLA and we do not promise response or fix times.
- **Coordinated disclosure:** we will work with you on a coordinated public
  disclosure once a fix is available, on a timeline we agree together.
- **Credit:** with your permission, we will credit you in the release notes
  or security advisory. If you prefer to stay anonymous, we will.

Security fixes ship as normal patch releases and are announced in the release
notes of the affected product.

## Good-faith safe harbor

We will not take legal action against a researcher who:

1. Reports through this policy in good faith.
2. Avoids accessing, copying, or exfiltrating data that is not yours —
   in particular, **do not access other tenants' data; use your own accounts
   and your own install**.
3. Avoids destroying data or interrupting service, and stops at a proof of
   concept rather than exploiting the vulnerability further than needed to
   demonstrate it.

Where this policy conflicts with a website's or product's terms of service,
this policy governs good-faith security research.

## Supported versions

Security fixes go into the latest 1.x release of MOD Core CE. We fix the
latest release and `main`; older releases are not patched, so please upgrade
when a fix ships. We will help you move to the latest version when you report.
This is a small team and support is best effort.

This policy supports the security process described above; it does not
claim that any product meets or is validated against any standard.
