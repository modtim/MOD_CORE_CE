---
sidebar_position: 9
slug: /legal/privacy-policy
title: Privacy Policy
description: How MOD Tech Labs handles personal data, cookies, retention, third-party services, and user rights.
---

# Privacy Policy

This describes how the self-hosted Community Edition handles data; you operate it.

**Last updated: November 2026**

> **IMPORTANT: This is a template. Have a lawyer review before launching.**

## 1. Information We Collect

### Personal Information (PII)
- **Account data**: name, email, company name, username
- **Authentication**: managed by Keycloak, a self-hosted identity provider that runs inside our own infrastructure (it is not a third-party service)

### Usage Data
- **File metadata**: filenames, sizes, types, upload dates (NOT file content, except as described below)
- **Workflow metadata**: workflow names, run times, completion status
- **Resource metrics**: CPU usage, RAM, GPU utilization, network throughput — recorded per tenant (associated with your tenant record) and used for scheduling optimization. The metrics contain no file content and no personal information.
- **API access logs**: IP addresses, endpoints called, timestamps

### Content Analysis
Some processing nodes inspect file content as part of their function:
- Thumbnail generation reads image content
- Blur detection analyzes pixel data
- Format detection examines file headers
- AI-powered nodes may send content to third-party inference services when your tenant has opted in to an external AI provider. This opt-in is per tenant (not per node) and is off by default: unless you configure and enable an external binding at your tenant, prompts and documents are only processed by AI services you control (for example, your own on-premises endpoints).

We do not systematically read, scan, or analyze the content of your files beyond what the processing nodes you select require.

## 2. How We Use Your Data

- **Service delivery**: processing your files through workflows
- **Scheduling optimization**: resource metrics (associated with your tenant record, with no PII in the metrics) train and tune our scheduling model
- **Security**: audit logging, anomaly detection
- **Communication**: service emails (welcome emails)

## 3. Data Sharing (Subprocessors)

We do NOT sell personal data. We do NOT share data with advertisers.

Data may be processed by the following subprocessors:

- **External LLM providers** (OpenAI, Anthropic, Groq, Mistral, Google, or a custom endpoint — only when your tenant has opted in and bound one): prompt and conversation content
- **Hugging Face** (only during gated model downloads): model references and access token
- **Slack** (only when your tenant has connected a workspace): notification payloads
- **Worker infrastructure**: your files are processed on worker machines (yours or platform-managed)

Keycloak, our authentication provider, is self-hosted software running inside our own infrastructure; authentication data stays on your install, so Keycloak is not a subprocessor.

## 4. Data Storage and Security

- Files stored in S3-compatible object storage (versitygw) with tenant-isolated buckets
- Secrets encrypted via OpenBao with tenant-scoped keys
- CE keys are OpenBao-backed; hardware-crypto and KMS integrations are not part of this edition.
- Database access controlled by OpenBao dynamic credentials (rotated hourly)
- All API traffic encrypted via TLS
- Audit logs maintained with HMAC integrity verification, following an ISO 27001-aligned audit design

## 5. Data Retention

- **Active accounts**: data retained for as long as the account is active. Each tenant has a data retention period (default 90 days) that controls how long workflow outputs remain available.
- **Deleted accounts**: when you request account deletion, the account is deactivated immediately (soft-deleted — we do not hard-delete database rows). A daily privacy-erasure job then destroys your file content and crypto-shreds your tenant encryption key after a grace period (default 30 days, configurable). The database rows that reference your account are retained for legal and audit purposes, but the content and keys that made them usable are destroyed. If you use your own S3 storage (BYO-S3), erasure is reported to you and deletion happens in your storage.
- **Audit and security logs**: audit logs, including IP addresses and user agents, are retained as security evidence under legitimate interest. A fixed expiry has not been set yet.
- **ML training data**: we record resource metrics for each scheduled task (task metadata, node type, GPU model, run durations, memory peaks) associated with your tenant record. We use these metrics to schedule and optimise your workflows. We keep identifiable metrics for 90 days. After 90 days we replace the tenant identifier in these records with a one-way pseudonym, so the metrics can no longer be traced back to your tenant. Pseudonymised records are never deleted; after 24 months we roll them up into aggregate totals that carry no tenant link. These metrics never include your file content or prompts.
## 6. International Transfers

Where data is transferred to a subprocessor in a different jurisdiction than the US, we rely on contractual safeguards, including Standard Contractual Clauses (SCCs) where applicable.

US-based processors we use:

- **External LLM providers** — jurisdiction depends on the provider you bind

## 7. Your Rights (GDPR)

If you are in the EU/EEA, you have the right to:
- **Access**: request a copy of your data (`GET /v1/me/data-export`)
- **Rectification**: update your profile in the Dashboard
- **Erasure**: request account deletion (`DELETE /v1/me/account`). The account is soft-deleted and your file content and keys are destroyed by the privacy-erasure process (see Section 5).
- **Portability**: export your data in JSON format
- **Object**: opt out of external AI processing for your tenant (remove the external binding)

We respond to GDPR requests within 30 days.

### Organization-level requests (tenant DSAR)

MOD Core supports tenant-scoped (organization-level) data requests on top
of the per-individual routes above:

- **Tenant export** — `GET /tenant/dsar/export`. The tenant admin (or a
  platform administrator) receives a bundle: each member's own personal
  data export plus the tenant-level records (member emails, invitations,
  and file records). A legal hold does not block an
  export.
- **Tenant erasure** — two-step: `POST /tenant/dsar/erasure` returns a
  single-use confirmation token; `POST /tenant/dsar/erasure/{id}/confirm`
  completes the erasure after a mandatory time delay. Every member
  account is deactivated and anonymized (soft-deleted, never hard-deleted),
  the tenant record is anonymized, and file content is removed through the
  storage purge path. Anonymized audit records are retained
  for legal compliance.
- **Legal holds** — if any data in the tenant is under an active legal
  hold, tenant erasure is refused (HTTP 409) until the hold is released;
  exports remain available.

These routes support GDPR-style requests at the organization level; they
do not certify any compliance posture on behalf of the tenant.

## 8. Cookies and Client-Side Storage

The Dashboard and WorkflowBuilder store session data in your browser:

- **Identity cookie** (`mod_active_identity`), set on the parent domain: an opaque session reference (not a credential), SameSite=Lax, not HttpOnly.
- **Session tokens**: Dashboard tokens live in sessionStorage (cleared when the tab closes); WorkflowBuilder also stores tokens in localStorage, which persists until you clear it.
- **UI preferences** (theme, panel layout, view modes) are stored in cookies and localStorage.

We do not use tracking cookies, third-party analytics cookies, or third-party error telemetry. For the full inventory, see [Cookies and Client-Side Storage](/legal/cookies-and-storage).

## 9. Children

The Service is not intended for users under 18. We do not knowingly collect data from minors.

## 10. Changes

We may update this policy. Material changes are communicated via email. Continued use constitutes acceptance.

## Contact

Data Protection Officer: admin@modtechlabs.com
