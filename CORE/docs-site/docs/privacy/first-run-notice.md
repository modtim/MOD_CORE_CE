---
draft: true
sidebar_position: 2
slug: /privacy/first-run-notice
title: First-Run Notice
description: The exact short notice to show at first admin login, plus where it should appear.
---

# First-Run Notice

This page holds the exact notice text for the first admin login in MOD
Community Edition. The UI part of the workstream should copy it verbatim; do
not paraphrase.

## Notice text — MOD CE (community edition)

> **Community Edition sends no data to MOD Tech Labs.**
>
> This edition sends no usage statistics, no scheduling metrics and no
> update-check requests.
>
> Outbound calls made by features you turn on yourself (for example an
> external model provider or a webhook URL) are listed on the page below.
> To block every outbound flow that MOD's egress policy covers, set
> `egress.sealed_mode = true`.
>
> Full list of every data flow: [What MOD Collects](/privacy/what-we-collect).

## Where the notice should appear

- **First admin login** (Dashboard, first session only): show the notice as a
  dismissable panel on the Settings page, with a direct link to the What MOD
  Collects page. Planned: the panel is part of the UI workstream and is not
  built yet.

## Status

- The CE notice text is written. The login panel is **Planned** (UI part of
  the workstream, later).
- The `telemetry.usage_stats` and `telemetry.update_check` settings are
  registered with default off. No code in this edition reads them, so turning
  them on changes nothing.
