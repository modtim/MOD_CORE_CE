---
sidebar_position: 2
title: "Bind a model to the Wizard"
description: A fresh MOD Core (Community Edition) install has no AI model bound to the Node Wizard. Pick one of your own stacks, or your own OpenAI-compatible endpoint, in Settings, then confirm the Wizard answers.
dashboard_route: '#settings'
---

# Bind a model to the Wizard

A fresh install has no language model bound to the AI slots, so the Wizard
cannot answer until you choose one. MOD does not pick or download a model for
you: you decide where the answers come from. This takes about two minutes.

The page has two views, **Easy** and **Advanced**. In Easy you bind each AI
slot in the **Who uses what** table. In Advanced you use the per-tool tab forms
(Node Wizard, Container Adapter, AI Assistant, Build Repair). The steps below
use the Advanced **Node Wizard** tab, and the same choices are in the Easy row.
The wizard's code-review model is set in the **Review model** panel on the
Node Wizard tab (Advanced).

## 1. What you see when nothing is bound

Open **Gearworks - Settings** (the Settings page), then the **Gearworks - AI
Configuration** tab. On Community Edition the
wizard's model is set on the **Node Wizard** tab: one form per tool, with
**Provider**, **Model**, **Base URL** and **API Key** fields. A tool nobody has
bound has an empty **Provider** and **Model**. In the wizard you get this message:

> Wizard LLM not configured. No model is bound to the Node Wizard yet. Open
> Settings > AI Configuration, choose a model for the Node Wizard (a model
> served by one of your own stacks, or your own OpenAI-compatible endpoint),
> then save.

The GitHub search message is a different problem. If the Wizard says it can't
search GitHub because the platform `github_token` isn't configured, a platform
admin sets it at **Settings > Secrets**. Binding a model does not fix that.

## 2. Where to click

You need one of: a chat model stack running on one of your own workers, or an
API key for an OpenAI-compatible provider.

1. Sign in as a platform admin and open **Gearworks - Settings > Gearworks - AI Configuration**.
2. At the top of the page, pick a view. **Easy** (the default) shows the
   **Who uses what** table: one row per AI slot, where you pick a stack, an
   endpoint and a model and save. **Advanced** shows the per-tool tabs. Your
   choice is remembered in this browser. For the wizard, either use the
   **Node Wizard** row in **Who uses what** (Easy), or click **Advanced** and
   open the **Node Wizard** tab. Its **Builder model** form takes
   **Provider**, **Model**, **Base URL** and **API Key**.

## 3. What to choose

### A model served by one of your own stacks

On the **Node Wizard** tab's **Builder model** form, set **Provider** to
**MOD Stack**, then choose the **Stack** (and the **Served model** if the stack
serves more than one), then click **Save**. Watch for **Saved.** under the form:
the form is only written when you click **Save**.

The stack must be a Persistent Stack that serves a chat model over an
OpenAI-compatible `/v1` endpoint (for example Ollama or vLLM). If your stack is
not in the list, create it on the Persistent Stacks page: paste a docker-compose
file into the **Create from compose** box, wait for it to start, then reload
**Settings > AI Configuration**.

### Your own OpenAI-compatible endpoint

1. Open **Settings > AI Configuration**, click **Advanced**, then the
   **Node Wizard** tab. Under **Builder model**, set **Provider**
   to **OpenAI-Compatible (managed vLLM / llama.cpp)**.
2. Fill in **Model**, **Base URL** and **API Key** for your endpoint, then
   click **Save** (it covers every tool tab, not only this one).

## 4. Confirm it works

Open the Node Wizard in the workflow builder and send a message. A reply means
the model is bound and reachable. If it still says not configured, check the
**Node Wizard** tab's **Builder model** fields are filled and that you saw
**Saved.** after clicking **Save**.

## Troubleshooting

- **Still says not configured.** The form was not saved. Reopen
  **Settings > AI Configuration**, click **Advanced**, go to the **Node Wizard** tab, fill in
  **Provider**, **Model** and **Base URL** (plus **API Key** for a hosted
  provider), then click **Save** and wait for **Saved.**
- **Can't reach LLM.** The endpoint is not reachable from the API container.
  Use the address the stack page shows: open **Endpoints…** on the stack's row and
  copy one of the addresses under **Internal (mesh)**, not `localhost` — inside the
  API container `localhost` is the container itself.
- **The stack is listed as down or sleeping.** Start it on the Persistent
  Stacks page, then check the row again.
