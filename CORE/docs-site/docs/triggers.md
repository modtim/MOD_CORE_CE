---
sidebar_position: 9
title: Triggers
description: Run workflows automatically — on a schedule, a webhook, a new file, an upstream workflow finishing, and more.
dashboard_route: '#triggers'
---

# Triggers

A **trigger** is a rule that runs a workflow automatically when something happens — so you don't have to click **Run** every time.

## How to add one

1. Open a saved workflow in the **Workflow Builder**.
2. Click the **Schedule** button on the toolbar.
3. For a time-based run, enter a **cron expression** + **timezone** and save.
4. For everything else, choose **Other trigger types**, pick the event, configure it, and save.

## Trigger types

| Type | Fires when… |
|------|-------------|
| **Schedule (cron)** | a cron expression matches (e.g. every Monday 9am) |
| **Webhook** | an inbound HTTP request hits the trigger's URL |
| **File event** | a new object lands in a watched storage location |
| **Workflow completion** | another workflow finishes |
| **Queue empty** | a processing queue drains |
| **Log-pattern alert** | a log line matches a pattern |

The Workflow Builder chooser also offers message-bus triggers.

## Tips

- **Webhooks** give you a URL to POST to — wire it into another system (CI, a form, an IoT device) to kick off a workflow on demand.
- **File event** triggers are great for "process anything dropped in this folder" pipelines.
- Combine with [batch mode](/workflow-builder) so one trigger processes many files.

## Related

- [Workflow Builder Guide](/workflow-builder)
- [Workflow webhooks](/features)
