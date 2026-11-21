---
draft: true
sidebar_position: 1
slug: /privacy/what-we-collect
title: What MOD Collects
description: Every data flow that can leave a MOD install, the setting that turns each one off, and the master offline switch.
---

# What MOD Collects

Your work, files and data stay on your own hardware. A small number of
optional outbound connections exist (listed below) and can be turned off.

This page lists every data flow that can send information out of a MOD
install. It is written from the code that ships: each row names the fields
that flow, where they go, how often, and the platform setting that
disables the flow. A flow found in the code but missing from this page is a
documentation defect — report it so the page can be corrected.

There is no analytics SDK, no beacon network, and no ad tracker in the
frontend. MOD's own domains (`*.modtechlabs.com`) are MOD-operated
infrastructure; on an on-prem install they become the customer's own LAN
domains.

## Flows that ship today

| # | Data sent (fields seen in code) | Destination | Purpose | Frequency | Turn it off |
|---|---|---|---|---|---|
| 1 | Nothing is sent out. The worker heartbeat (`last_fetch_at`, `fetch_loop_age_s`, agent version) goes to MOD's own API over the local/mesh channel; the API compares its own stored latest version | MOD API only (`routers/worker_management.py`, fed by `utils/worker_releases`) | Advertise a newer worker build so a worker can self-update | Every heartbeat pass, only when a newer version is known | `egress.update_check_enabled = false` (workers are never advertised a newer version), or `egress.sealed_mode = true` |
| 2 | Repo/package names queried (no tenant content) | `https://api.github.com`, `https://pypi.org` | Node Wizard lookups: GitHub READMEs, PyPI package metadata, web query expansion | On wizard use (repo/library lookups) | `egress.wizard_internet_enabled = false` (lookups return empty; the wizard proceeds without them), or `egress.sealed_mode = true` |
| 3 | Tenant content: model weight URLs/refs (the size probe leaks which model refs a tenant requests); an `HF_TOKEN` rides the gated-repo path | `https://huggingface.co` and its CDN hosts | Download embedding/GGUF weights, probe model size | First weight fetch; size probe on AI-config model add | `egress.model_download_enabled = false` (only locally cached / object-store-mirrored weights are used; a missing model is an explicit error), or `egress.sealed_mode = true` |
| 4 | Tenant content: prompts, conversation, documents | External LLM providers (OpenAI, Anthropic, or a user-set base URL) | Assistant / tool-calling / external-LLM nodes | On assistant or external-binding node runs | `egress.external_llm_enabled = false` (external bindings refused with a clear error; local / MOD-hosted bindings always allowed), or `egress.sealed_mode = true` |
| 5 | Tenant content: run/workflow metadata, forwarded bodies; completion payloads carry run id, status, error text, result URL | Tenant-supplied webhook URLs (workflow triggers, HTTP-poll triggers, run-completion callbacks) | Triggers and run-completion notifications | On trigger fire / run completion | `egress.webhooks_enabled = false` (sends skipped with an audit-safe log line; the run is not failed), or `egress.sealed_mode = true` |
| 6 | Metadata: user emails, names, verification/welcome content, password-reset links | SMTP host (Postmark or customer SMTP) | Transactional email | On signup / invite / reset | No `smtp_host` configured = email disabled; also refused under `egress.sealed_mode` |
| 7 | None (browser UA/URL only) | `https://cdn.jsdelivr.net` (three.js physics loader, WebXR controller assets), `https://esm.sh` (LAS loader) | Frontend CDN assets | On the affected page load | Planned: frontend CDN rows are a separate task and are not covered by the egress switches |
| 8 | None (installer script only) | `https://tailscale.com/install.sh`, `get.docker.com`, `storage.googleapis.com` | Worker bootstrap fallback | Only when the manager mirror is unavailable during remote worker setup | Planned: no switch today (fallback is unconditional) |
| 9 | The first 5 characters of the SHA-1 hash of a new password (a k-anonymity range lookup; the password and the full hash are never sent) | `https://api.pwnedpasswords.com/range/<prefix>` | Checks a new password against known breaches when a password is set or changed; a bundled offline blocklist still applies if the service is unreachable | Each password change | Set `egress.sealed_mode` to true (it skips this lookup); there is no separate switch for this call |

Defaults today: every `egress.*` switch above defaults to `true` (flow
allowed); `egress.sealed_mode` defaults to `false`.

## Telemetry, usage stats and update checks

MOD Community Edition sends no usage statistics, no scheduling metrics and
no update-check requests. The `telemetry.usage_stats` and
`telemetry.update_check` settings exist and default to off, but no code in
this edition sends data when they are on.

The worker heartbeat compares versions inside your own MOD API. No request
leaves your network for it.

To block every outbound flow that MOD's egress policy covers, set
`egress.sealed_mode = true`.

## Keys and secrets

CE keys are OpenBao-backed; hardware-crypto and KMS integrations are not part of this edition.

## Offline / air-gapped mode

The master switch is `egress.sealed_mode` (platform setting, group
`egress`, default `false`). When it is `true`, every per-category egress
switch is treated as disabled — `utils/egress_policy.py::egress_allowed`
checks it first, so no per-category setting can re-open a path.

Sealed mode means zero outbound calls from the API for the gated
categories: model downloads, external LLM, webhooks/Slack, wizard
internet lookups, and update checks. The worker heartbeat stays on the
local/mesh channel to MOD's own API.

Sealed mode is the air-gapped profile entry in the compliance-profile
catalog (`sealed_mode` — "Zero outbound calls (telemetry, phone-home,
HF, external LLMs, CDN, update checks)"). Turning it on is one setting
change from the admin settings UI; it is the one-click off for every
flow above.

## First-run notice

Planned: shown at first admin login (see `privacy/first-run-notice` for the
exact text).

> Community Edition sends no data to MOD Tech Labs. This edition sends no
> usage statistics, no scheduling metrics and no update-check requests.
