---
sidebar_position: 43
slug: /security/incident-response
title: Incident Response
description: "How Mod Tech Labs Inc. handles security incidents: scope, severity levels, the phases we work through (detect, triage, contain, eradicate, recover, review), what a customer can expect to be told and when, how to report a suspected incident, and what an operator of a self-hosted install should do first."
---

# Incident Response

This page describes, in plain terms, how security incidents are handled. It is
the customer- and auditor-facing summary; the internal operational detail lives
in runbooks we run with support.

## Scope

An "incident" here means a confirmed or suspected security event on MOD —
SaaS, on-prem, or Community Edition — that affects the confidentiality,
integrity, or availability of tenant data, or that has regulatory
consequences. Vulnerability reports from outside parties enter this process
through the disclosure policy (see below).

## Severity levels

We use four levels, the same ones our policy defines:

| Severity | Meaning | What it triggers |
| --- | --- | --- |
| Critical | Confirmed breach of customer data, active compromise, or regulatory impact | Customer notification (see below), evidence preservation, legal-hold consideration |
| High | Confirmed security event on confidentiality/integrity/availability, no confirmed data loss | Internal alerting only at this stage; no customer notice yet |
| Medium | Limited scope or unconfirmed impact, still under investigation | Tracked internally; no notice |
| Low | Non-critical event, minimal business impact | Recorded only |

A customer notice is sent for a confirmed breach of customer data (Critical),
not for every event.

## What happens

Every incident moves through six phases. Each phase has an owner, an exit
condition, and a decision record; the incident is closed only by the
designated incident lead.

1. **Detect** — the event is put on record as an incident with a severity.
   MOD raises security alerts from its audit history (for example, sign-in
   failure bursts and admin-role changes).
2. **Triage** — the incident lead and technical leads confirm or dismiss the
   breach using the audit log and time-boxed audit exports.
3. **Contain** — the exposure stops. Typical actions: revoking an API key and
   forcing sign-out of affected sessions while the investigation is open.
4. **Eradicate** — the cause is removed, not masked: credentials rotated,
   affected surfaces repaired, the code-level fix merged.
5. **Recover** — the customer notice is delivered, the deadline below is met,
   and the evidence set is handed to the customer.
6. **Review** — the incident is closed, the policy gap is written down, and
   the next guardrail is named.

Evidence rules throughout: MOD does not hard-delete. Audit records are
append-only, evidence stays addressable for the life of an incident, and no
key material or raw secret values ever appear in an evidence artifact.

## Notification

MOD Core CE is self-hosted and comes with no SLA. Because the software runs on
your own hardware, you are the operator of your data and of any incident
affecting it, and there is no notification deadline or service clock. To report
a vulnerability, write to the security contact in the Vulnerability Disclosure
Policy. We triage reports best-effort.

## How to report a suspected incident

- To report a **vulnerability or security flaw** in MOD, use the
  Vulnerability Disclosure Policy.
- If you believe your **account or sign-in security** is affected (for
  example, you are locked out or your factors are not behaving as expected),
  see [MFA Reset and Security Contact](/security/mfa-reset-and-security-contact)
  — the tenant security contact is also the channel our notices use.
- If you are an auditor reviewing an incident's effect on your data access,
  request an audit by contacting admin@modtechlabs.com.

## For self-hosted installs

If you operate your own MOD install, the first minutes of an incident are
yours to run: stop the exposure, preserve evidence, and bring support in when
needed. See [First minutes of an incident on your install](/runbooks/incident-first-steps).

## Planned, not live yet

Parts of this process are built but not yet shipped as in-product artifacts.
We do not describe these to a customer as available:

- An in-product incident tracker that carries the confirmation timestamp and
  the notification deadline (until it ships, these timestamps are recorded
  in the incident record).
- An admin-facing incident list/detail page.
- The breach-notice email template and in-app banner driven from that
  tracker (notice delivery today runs on the tenant security contact and
  email).
- A tenant-wide credential sweep tool.
- An incident post-mortem page.
