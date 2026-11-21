---
sidebar_position: 10
title: Features Catalog
description: Curated list of every user-visible feature on MOD Core. Used by the support assistant for grounded answers — keep this list current.
category: features
---

# Features Catalog

Each feature below has three subsections: **What it does** (one-line user-facing summary), **How to use it** (concrete steps), and **Limits** (what Community Edition provides; CE has no tiers).

---

## Workflow Builder

**What it does**
Visual editor for chaining nodes (image processing, ML inference, file ops, etc.) into reusable workflows.

**How to use it**
1. Open the dashboard → Workflow Builder → New Workflow.
2. Drag nodes from the right-hand library onto the canvas.
3. Connect outputs to inputs by clicking + dragging between ports.
4. Click Run to execute against an input file or schedule.
5. Click Save to keep the workflow for re-use.

**Limits**
Community Edition has no limit on saved workflows.

---

## File / folder management

**What it does**
Upload, organize, share, rename, move, copy, and delete files and folders. Backed by content-addressed object storage with deduplication.

**How to use it**
1. Dashboard → Data → Files. Click into folders to browse.
2. Use the upload button or drag files in.
3. Select one or more items via the row checkboxes.
4. Use the bulk-actions bar to **Rename**, **Move**, **Copy**, **Download**, **Share**, or **Delete**.
5. Press **F2** with a single item selected to rename inline.

**Limits**
Community Edition: no plan limits and no storage cap; you are limited only by the disk or bucket you provide.

---

## Support assistant

**What it does**
Conversational helper that answers questions about how to use the platform, grounded in this documentation. Knows your tenant + role for tailored guidance.

**How to use it**
1. Click the **robot icon** in the top-right of any dashboard page to open the assistant panel.
2. Ask in plain English. The assistant replies with numbered steps, links to docs/feature pages, and one-click **action buttons** that take you straight to the relevant screen.

**Limits**
Community Edition: no plan limits.

---

## Logs viewer + LLM helper

**What it does**
Inspect workflow run logs, audit trails, and system task output. A side-rail LLM helper diagnoses failures using the visible log + task metadata.

**How to use it**
1. Dashboard → Logs → pick a tab (Workflow Runs / System Tasks; Log Streaming appears where your role shows it). Permission audit lives under Dashboard → Logs → **Audit log** (the signpost link on the Logs page).
2. Click a row to open its log.
3. Click the **robot icon** in the top bar to open the assistant panel.
4. Ask "Why did this fail?" or "How do I retry this?" — answers reference the actual log lines you're looking at.

**Limits**
Community Edition: no plan limits. The Logs viewer is included.

---

## API keys

**What it does**
Generate revocable keys for headless / CI usage of the platform. A key **inherits the permissions of the user who created it** (there are no separate scopes).

**How to use it**
1. Dashboard → Gearworks - Settings → Gearworks - API Keys → Create New API Key.
2. Give it a description.
3. Copy the key once — it starts with `sk_` and is never shown again.
4. Send it as the `X-API-Key:` header on API calls (or exchange a Bearer token). See the [API Reference](/api-reference).

**Limits**
Community Edition: no plan limits.

---

## Workflow scheduling

**What it does**
Run a workflow automatically on a cron-like schedule.

**How to use it**
1. Open a saved workflow → **Schedule** button on the toolbar.
2. Enter a **cron expression** and pick a **timezone**.
3. Save. The workflow then runs automatically on that schedule.

For event-driven runs (rather than time-based), see **Triggers** below.

**Limits**
Community Edition: no plan limits.

---

## Email provider configuration

**What it does**
Sends platform emails (signup verification, invitations, receipts and notices, password resets, support replies, workflow-failure alerts) through your configured email provider.

**How to use it**
1. Dashboard → Gearworks - Settings → Gearworks - Integrations → Email (SMTP).
2. Choose **Postmark** (the default) or **Custom SMTP** — Custom SMTP covers Gmail/Workspace, AWS SES, Mailgun, Brevo, or any other SMTP host.
3. Fill in the credentials / sender address.
4. Click **Send Test Email** to verify before saving — a successful test writes the config and the next outbound send picks up the new creds within ~60s.

**Limits**
Platform-admin only (this is a platform-level setting).

---

## File sharing (with expiration)

**What it does**
Generate a time-limited link or grant a permission to a specific user/email so others can view, edit, or download a file or folder.

**How to use it**
1. Files → select a file or folder → **Share**.
2. Pick the person (by user/email) and a **permission level** (viewer, editor, owner).
3. Optionally set an **expiration date**.
4. Grant. A folder grant cascades to its contents; a grant directly on a file overrides what it inherited.

**Limits**
Community Edition: no plan limits.

---

## Help mind map

**What it does**
Visual discovery view for the help docs. The Dashboard Help page ships two tabs — **Cards** (the classic searchable card grid) and **Mind Map** — a horizontally-growing tree of boxes. Clicking a box opens its sub-topics to the right; terminating leaves stack tightly while category branches space apart. The detail aside on the right shows the topic's description plus deep links to the docs page and the underlying dashboard feature where applicable.

**How to use it**
1. Dashboard → Help → **Mind Map** tab.
2. Click any box to drill into it. Click root ("Help") to collapse everything back.
3. Mind-map content is auto-generated from `CORE/docs-site/docs/` — every page and every h2/h3 heading becomes a node.

**Limits**
Community Edition: no plan limits.

---

## Light / dark theme

**What it does**
Top-bar moon/sun toggle flips the Dashboard between light and dark. The choice persists in a parent-domain cookie so it survives reload AND carries across to the WorkflowBuilder and mod-docs subdomains automatically — pick once and every MOD surface respects it.

**How to use it**
1. Click the moon icon in the top-right header (between user avatar and notification bell).
2. The page flips immediately; navigate to WorkflowBuilder or the docs site and they're already dark too.

**Limits**
Community Edition: no plan limits.

---

## System Status (platform admin)

**What it does**
A platform-admin-only panel on the Dashboard home page that shows the live health of the core platform services the API probes: the API itself, the PostgreSQL database, Keycloak (sign-in), object storage (versitygw), Redis, the platform registry, the model cache, workers, and the crypto self-test. Each service is shown as up, degraded, or down, with error detail when it is not healthy.

**How to use it**
1. Sign in as a platform admin.
2. Dashboard → Home → **System Status** card.
3. Expand any section to drill into details. Each unhealthy entry includes a manual-fix snippet (`docker logs … --tail 100`, etc.).

**Limits**
Platform-admin only.

---

## Audit log viewer

**What it does**
Full audit trail for every consequential action — admin saves, cross-tenant access blocks, permission grants/revokes, email-template misses, login failures — searchable and filterable by event name, actor, target, and time window. Each row exposes structured `details` JSON for triage.

**How to use it**
1. Dashboard → **Records - Logs** → **Audit log** signpost link (page title: Permission Audit Logs). The Records - Logs page has no Audit tab; a signpost there points here.
2. Filter by event name (e.g. `ADMIN_EMAIL_CONFIG_UPDATED`, `EMAIL_TEMPLATE_MISSING`) or actor email.
3. Click any row to expand its full details payload.

**Limits**
The Audit log is shown to super admins and platform admins. Each row is scoped to the signed-in user's tenant.

---

## Tenant Registry

**What it does**
Your tenant's **container-image registry** view: MOD's built-in registry for your tenant's node images (Community Edition runs no separate private registry service: on-prem images come from the local image store, or from the optional LAN registry profile when builds must be shared between workers). It lists the Docker images in your tenant's registry with their vulnerability-scan status and how much storage each image uses, and lets you soft-delete images to reclaim space. Scoped to your own tenant — it is not a cross-tenant admin console. If you believe you have found a vulnerability in the platform itself, report it per the vulnerability disclosure policy in `SECURITY.md` at the root of this repository.

**How to use it**
1. Dashboard → Data (Files) → **Tenant Registry** tab.
2. Review images, scan results, and how much storage each image uses.
3. Soft-delete an image to free space.

**Limits**
Community Edition: no plan limits.

---

## AI Configuration (per-tool tabs)

**What it does**
Per-AI-tool LLM configuration on a single Settings page (Gearworks - Settings; this tab is Gearworks - AI Configuration). Each MOD tool that uses an LLM gets its own sub-tab — **Node Wizard (`wizard`), Container Adapter (`adapter`), AI Assistant (`assistant`), Support chat (`support`), and Build Repair (`repair`)** — where you pick the model source (a MOD Stack you run, or a saved external endpoint) and tune model-specific knobs.

**How to use it**
1. Dashboard → Gearworks - Settings → Gearworks - AI Configuration.
2. Click the sub-tab for the tool you want to tune.
3. Open the row's dropdown and pick the model source: **Let MOD choose**, one of your MOD Stacks, or a saved external endpoint (listed by its URL). The pick saves immediately; there is no separate Bind or Save button.

**Limits**
Platform-admin only.

---

## Scheduler runtime config

**What it does**
Three live tuning sliders for the workflow scheduler — capacity floor (how much of the fleet to keep idle for new jobs), packer-aggressiveness threshold (how full the fleet must be before paying for tight planning), and variance penalty (how much the scheduler pads uncertain time estimates). Changes take effect within ~60s; presets jump between Default / Aggressive / Conservative.

**How to use it**
1. Dashboard → Gearworks - Settings → Gearworks - AI Configuration → **Scheduler** tab.
2. Drag the sliders or pick a preset.
3. Click Save — values write to the `scheduler_runtime_config` table; the next scheduler tick picks them up.

**Limits**
Platform-admin only.

---

## Storage configuration (platform admin)

**What it does**
Read-only view of the bundled S3-compatible object store's host-side data path. Shows current path, filesystem type, and free/used/total bytes. Changes to the path itself happen at the host level by re-running `start-main-server.sh`.

**How to use it**
1. Dashboard → Gearworks - Settings → **Storage** tab (platform-admin only).
2. Inspect the values; if you need to relocate, SSH to the host and re-run the bootstrap script.

**Limits**
Platform-admin only (platform operators).

---

## Triggers

**What it does**
Launch workflows automatically on events — not just on a schedule. Trigger types include **cron schedule**, **webhook** (inbound HTTP), **file event** (a new object in a watched storage location), **workflow completion**, **queue empty**, and **log-pattern alerts**, among others.

**How to use it**
1. Open a workflow → **Schedule** button → for time-based, set a cron; for the rest, choose **Other trigger types**.
2. Configure the event (URL, watched folder, upstream workflow, …) and save.

**Limits**
Community Edition: no plan limits on triggers.

---

## Persistent stacks

**What it does**
Run a long-lived **docker-compose project** on a MOD worker — e.g. a Postgres, Redis, or nginx service that stays up between workflow runs. MOD creates, deploys, stops, and tears the stack down for you.

**How to use it**
1. Dashboard > System > Stacks.
2. Start from a template (postgres / redis / nginx) or supply your own compose, then **Deploy**.
3. Stop or tear down from the same page.

**Limits**
Community Edition: no plan limits.

---

## Secrets

**What it does**
Store tenant secrets (API tokens, registry credentials, SMTP passwords) securely in **OpenBao**. Values are **write-only** — you can set or replace a secret, but the platform never returns the stored value back to the browser. In CE, tenant secret keys are wrapped with the operator-held master key (`SECRET_ENCRYPTION_KEY`). CE has OpenBao-backed keys.

**How to use it**
1. Dashboard → Gearworks - Settings → **Gearworks - Secrets**.
2. Add a secret by name + value; reference it from nodes/integrations that need it.

**Limits**
Community Edition: no plan limits.

---

## Sign-in

CE users sign in with local accounts in the bundled Keycloak.
