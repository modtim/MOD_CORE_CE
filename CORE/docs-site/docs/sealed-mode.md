---
sidebar_position: 5.8
title: Sealed mode (no phone-home) for MOD CE
description: Turn on egress.sealed_mode to stop every gated outbound call from MOD and keep the CE install fully on the box.
---

# Sealed mode (no phone-home) for MOD CE

Sealed mode is MOD's master off-switch for outbound calls. One setting,
`egress.sealed_mode`, overrides every per-category egress switch: when it is
on, the API refuses all of the gated outbound calls. Nothing gated leaves the
box.

## What sealed mode is

The API-side decision point lives in `API/utils/egress_policy.py`. The
constant `SEALED_MODE_KEY` is the string `egress.sealed_mode`. The helper
`egress_allowed(category)` checks the master switch first: if sealed mode is
on it returns False for every category, without even reading the category's
own key. The default is False, so a normal install behaves exactly as before
until an admin turns the switch on.

## How an operator turns it on

`egress.sealed_mode` is a platform setting registered in
`API/services/platform_settings.py` (group `egress`, type bool, default
False). Turn it on from the platform settings surface like any other
setting; no code change and no redeploy is needed.

## What outbound calls it stops

The categories the code registers (all booleans, all default True, each
overridden by sealed mode):

- `egress.nim_enabled`: outbound calls to NIM catalog and endpoint servers.
  Refused calls fall back to the local DB catalog.
- `egress.model_download_enabled`: HuggingFace weight downloads and size
  probes. With the call stopped, only locally cached or object-store-mirrored weights
  are used, and a missing model is an explicit error.
- `egress.external_llm_enabled`: calls to external LLM providers.
- `egress.webhooks_enabled`: tenant-supplied webhooks and Slack notifications.
- `egress.wizard_internet_enabled`: the Node Wizard's internet lookups
  (GitHub READMEs, PyPI metadata).
- `egress.license_phonehome_enabled`: the license server phone-home. On CE
  this key is registered but gates nothing: CE has no license client and no
  call-home (see below).
- `egress.update_check_enabled`: worker self-update advertisement and the
  currency-rate fetch.
- `egress.telemetry_enabled`: the login page RUM beacon.

Under sealed mode every one of these is refused at once.

## License phone-home: not present in CE

MOD CE ships no license verification and no phone-home at all: CE installs
are unlimited, so there is no call-home to seal. The
`egress.license_phonehome_enabled` setting still exists in the settings
registry, but on CE it gates nothing: there is no license client in the CE
cut to make that call.

## What still works offline

- CE has no license check to fail: installs are unlimited, nothing phones
  home.
- Locally cached or object-store-mirrored model weights still load.
- The local NIM DB catalog is served instead of a remote fetch.
- Local and MOD-hosted model bindings are always allowed; only external
  bindings are refused, with a clear error.
- Runs, workers and the dashboard keep working; a gated call that
  is refused falls back (local catalog, cached weights, skipped send) instead
  of taking the feature down.

## How to confirm nothing leaves the box

Sealed mode is visible and auditable on the box itself. Read the switch back
from the platform settings surface: `GET /platform/settings/egress.sealed_mode`
returns `true`, like any other setting. Every refused call carries its
refusal reason: `egress_policy.refuse()` returns a string naming the switch
("egress disabled: egress.sealed_mode is True (sealed mode) - blocks
`<category>`") that the calling feature logs or shows in its error, so the API
log is the audit trail. The refusal ledger in `API/utils/egress_policy.py`
(`record_refusal` / `refusal_counts`) counts refusals per category with
stable site codes (never a URL, body, key or tenant id); on CE those counts
live in the process, not behind an endpoint. A process restart zeroes the
in-process ledger.

## Settings-outage caveat

Setting reads are fail-safe by design (`_bool` in `egress_policy.py`): if the
settings store errors, the master read is treated as NOT sealed. A settings
outage must never silently seal a live install and stop every outbound
feature, so during an outage the per-category defaults apply and sealed mode
is assumed off. If you rely on sealed mode, read the setting back
(`GET /platform/settings/egress.sealed_mode`) rather than assuming it.

## Not covered by sealed mode

The frontend CDN rows (three.js wasm, esm.sh loaders, Stripe.js, Tailwind
CDN) are a separate task and are NOT gated by this helper. Sealed mode covers
the API-side gates only.
