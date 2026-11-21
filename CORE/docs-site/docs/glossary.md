---
sidebar_position: 3
slug: /glossary
title: Glossary
description: Plain-English definitions of every term you'll see in MOD — workflows, nodes, tenants, triggers, and the people they're talking about behind the scenes.
---

# Glossary

A plain-English guide to the words you'll see all over MOD. If you've
never written code or run a "workflow" before, start here — every
term below assumes zero technical background and links to related
ideas.

If a term you saw isn't here, ask the assistant (the robot icon in
the top-right of every page) and we'll add it.

---

## Tenant

**Your team's account in MOD.** Everything you upload, every workflow
you build, every user you invite — all of it lives inside your
tenant. A tenant is private to your team: another customer using the
same MOD installation can't see your files, runs, or settings.

If you're a one-person team that's still a tenant — yours, with one
user (you).

When the docs say "for this tenant", it just means "for your team's
account".

**Related:** [User](#user), [Plan](#plan)

---

## User

**One person logged into the tenant.** Users belong to a tenant. You
can invite teammates from Users (sidebar); each invited person gets
their own login but they all share the tenant's files and workflows.

The first user to sign up for a tenant is the **tenant admin** —
they can invite people and adjust settings. Other
users can do everything else.

**Related:** [Tenant](#tenant), [Permissions](#permissions)

---

## Workflow

**A recipe for processing files.** A workflow is a list of steps you
want MOD to do — say, "take this image, blur it, then convert it to
PNG, then put the result in this folder."

You build workflows visually by connecting boxes (called [nodes](#node))
on a canvas. You can save a workflow and run it again with different
files later. Workflows are the main thing you'll be doing on MOD.

If you've used something like Photoshop's batch automation or
Zapier's "zaps", a workflow is the MOD equivalent — but for any
file processing task, not just images or simple integrations.

**Related:** [Node](#node), [Workflow Run](#workflow-run), [Workflow Wizard](#workflow-wizard)

---

## Node

**One step in a workflow.** A node is a single box on the workflow
canvas that does one thing — resize an image, extract audio from a
video, run an AI model, send the result by email, anything.

Three kinds of nodes you'll see most often:

- **Input nodes** (blue) — they bring files INTO the workflow
- **Processing nodes** (purple) — they DO something to the files
- **Output nodes** (green) — they SAVE or SEND the result somewhere

You connect nodes with lines (called "edges") to control the order
of operations. The output of one node feeds into the input of the
next.

There are **200+** prebuilt nodes covering image, video, audio,
3D, AI, and data tasks. You can also build your own — see
[Node Wizard](#node-wizard).

**Related:** [Workflow](#workflow), [Node Wizard](#node-wizard)

---

## Workflow Run

**One execution of a workflow.** Every time you click "Run" on a
workflow, that's a Workflow Run. Each run gets its own page in the
Process Queue where you can watch it work in real time, see how
long each node took, and download the results.

If you run the same workflow ten times on ten different files,
you'll have ten Workflow Runs. Each one is independent — if one
fails, the others keep going.

**Related:** [Workflow](#workflow), [Process Queue](#process-queue), [Worker](#worker)

---

## Worker

**The computer that actually does the work.** A worker is the
machine that runs your workflow's nodes. When you click "Run" in
the Workflow Builder, MOD doesn't process anything on your laptop —
it sends the job to a worker, which is a separate machine (usually
a beefy one with a GPU) that does the heavy lifting and reports the
result back.

A worker does three things: it picks up tasks from MOD, runs each
node inside its own container, and returns the output. By default
your jobs run on the platform's **shared worker pool**, so you don't
need your own hardware to get started; you can also enroll
**tenant-owned** workers for dedicated capacity. Workers self-enroll
with an enrollment token (see [Worker Setup](/worker-setup)) and are
listed on the Workers page in the Dashboard.

**Related:** [Workflow Run](#workflow-run), [GPU](#gpu)

---

## Workflow Wizard

**A chat-style assistant that builds workflows for you.** Instead of
dragging boxes onto the canvas yourself, you tell the Workflow
Wizard what you want in plain English — "take a stack of product
photos, generate transparent-background versions, and save them as
PNGs" — and it builds the workflow for you. You can edit the result
just like a hand-built workflow.

Best for: starting a new workflow when you're not sure which nodes
to use.

Find it: in the Workflow Builder, click **Functions → Workflow
Wizard**.

**Related:** [Workflow Builder](#workflow-builder), [Workflow](#workflow), [Node Wizard](#node-wizard)

---

## Node Wizard

**Build a custom node from a description.** If none of the 200+
built-in nodes do what you need, the Node Wizard helps you make a
new one. You describe what it should do (or point at a Docker image,
script, or Dockerfile) and the wizard packages it into a node you
can drag onto the canvas.

This is more advanced than the Workflow Wizard — you'll often want
a developer involved.

Find it: in the Workflow Builder, click **Functions → New Node
Wizard**.

**Related:** [Node](#node), [Container](#container)

---

## Process Queue

**Live status of every workflow that's running.** Open the Process
Queue (Functions → Process Queue in the Workflow Builder) to see
what's running right now, what's waiting, and what's finished.
Click into any run to watch its nodes progress in real time.

This is also where you cancel a runaway run, retry a failed one,
or download finished results.

**Related:** [Workflow Run](#workflow-run), [Node Queue](#node-queue)

---

## Node Queue

**Live status of every node that's being built.** Different from
Process Queue — Node Queue tracks new *nodes* that the Node Wizard
is currently building (compiling, testing, registering).

Most users will rarely look at this. It's mainly useful when you've
just used the Node Wizard and want to see if your new node is ready
to drag onto the canvas.

**Related:** [Node Wizard](#node-wizard), [Node](#node)

---

## Trigger

**What kicks off a workflow.** Workflows don't have to be run by
clicking a button. A trigger is a rule that runs a workflow
automatically when something happens.

Trigger types you'll see:

- **Schedule** — run the workflow on a clock (e.g., every Monday at
  9am, or every 30 minutes)
- **Webhook** — run when another system makes an HTTP request
- **File Event** — run when a new file lands in a folder
- **Workflow Completion** — run after another workflow finishes
- **Log Pattern Alert** — run when a log line matches a pattern

You add triggers from the **Schedule** button on the Workflow
Builder toolbar (when you have a saved workflow loaded) — use it for
cron schedules directly, or pick "Other trigger types" for the rest.

**Related:** [Workflow](#workflow), [Schedule](#schedule), [Webhook](#webhook)

---

## Schedule

**A trigger that fires on a clock.** Tell MOD "run this workflow
every weekday at 8am" or "every 15 minutes" and it'll do it. Uses
[cron](https://en.wikipedia.org/wiki/Cron) syntax under the hood,
but the trigger dialog has plain-English presets so you usually
don't have to learn cron.

**Related:** [Trigger](#trigger)

---

## Webhook

**A trigger that fires when another website calls MOD.** Useful if
you have another tool (a form builder, an e-commerce site, a build
system) that you want to send work TO MOD. The webhook gives you a
URL; the other tool sends an HTTP request to that URL; MOD runs the
linked workflow.

**Related:** [Trigger](#trigger), [API](#api)

---

## File / Data / Files Browser

**Your tenant's file storage.** Everything you upload to MOD ends up
in the Files browser (sidebar → Data). Workflows read inputs from
here and write outputs back here. Like Dropbox or Google Drive, but
scoped to your tenant.

You can also point workflows at external storage (S3, FTP, etc.)
when you need to.

**Related:** [Tenant](#tenant), [Workflow](#workflow)

---

## Credit

**CE does not meter work.** A node run simply executes on the workers you install and costs nothing.

**Related:** [Worker](#worker), [Workflow](#workflow)

---

## Plan

**CE is one edition with one setup.** Everything ships enabled, and your install is ready as it stands.

**Related:** [Tenant](#tenant)

---

## Billing

**CE never asks you to pay.** There is no Billing page, no invoices, and nothing to activate.

**Related:** [Tenant](#tenant), [User](#user)

---

## Seat

**Users are the unit.** Every person who signs in is one user, and invited teammates join the same tenant.

**Related:** [User](#user), [Tenant](#tenant)

---

## Permissions

**Who can do what.** Permissions control which features a user can
use inside your tenant. The defaults handle most cases — tenant
admins can do everything, regular users can build and run workflows. You can also create custom permission
templates for specific roles (e.g., "QA reviewer" sees runs but
can't edit workflows).

**Related:** [User](#user)

---

## Container

**The "box" a node runs inside.** Every node is packaged as a
[Docker](https://en.wikipedia.org/wiki/Docker_%28software%29)
container — a self-contained mini-computer with exactly the right
tools and libraries to do its one job. You don't need to know how
containers work to use MOD; the Workflow Builder hides all of that.
The Node Wizard is what cares about containers.

**Related:** [Node](#node), [Worker](#worker)

---

## Canvas

**The whiteboard where you drag nodes.** The Canvas is the big grid
in the middle of the Workflow Builder. You drop nodes onto it,
connect them with lines, drag them around to keep things tidy, and
zoom in/out with the scroll wheel. The Canvas IS the workflow — if
it's empty, your workflow is empty.

**Related:** [Workflow Builder](#workflow-builder), [Node](#node), [Edge](#edge)

---

## Edge

**A line between two nodes.** Edges connect the output of one node
to the input of the next, telling MOD the order to run things in
and what data to pass along. To make an edge, click and drag from
one node's output dot to another node's input dot.

If a node has no edges coming in or going out, it's an island and
won't be part of the run.

**Related:** [Node](#node), [Canvas](#canvas)

---

## Capability

**A permission to do something to a resource.** In MOD's access
control, a capability is an *action* — like `view`, `download`,
`execute`, `share`, or `delete` — that can be granted on a resource
(a file/cog, a workflow, a user, etc.). Permission levels (viewer /
editor / owner) are bundles of capabilities; advanced sharing lets
you toggle individual ones. It is **not** the same as a node type —
that's a separate concept.

**Related:** [Permissions](#permissions), [Node](#node)

---

## Save / Version

**Snapshots of your workflow.** Each time you click Save in the
Workflow Builder you create a new Version of that workflow. Old
versions stick around — you can roll back, compare, or run an old
version on purpose. This means experimenting on a workflow is
safe: if you break it, restore the previous version.

**Related:** [Workflow](#workflow), [Run History](#run-history)

---

## GPU

**Graphics card, used for AI work.** Many of the AI nodes (image
generation, video upscaling, 3D reconstruction) run much faster on
a GPU. If your tenant has GPU workers available, the platform will
automatically send GPU-needing nodes to them. If not, those nodes
may run very slowly or refuse to run.

**Related:** [Worker](#worker)

---

## API

**The way other programs talk to MOD.** MOD has a REST API that
mirrors most of what the dashboard does — list your workflows, run
a workflow, download a result, manage users. The API is mainly for
integrations: connecting MOD to a build system, a custom dashboard,
or another product.

You can find your API key in Settings → API Keys. If you don't
plan to integrate MOD with another tool, you can ignore this.

**Related:** [Webhook](#webhook), [Integration](#integration)

---

## Integration

**A connection to another service.** Under **Settings → Integrations →
Email (SMTP)** (platform admins) you configure **email delivery** (SMTP —
Postmark or a custom host) so MOD can send notifications; the same
sub-tab holds the **email-verification** toggle. Other
outbound connections live elsewhere: **Slack** notifications are set
up in their own config, and workflows reach external
systems via **Webhooks** and the **API**.

**Related:** [API](#api), [Webhook](#webhook)

---

## Workflow Builder

**The visual canvas where you build workflows.** Drag nodes from
the sidebar onto the canvas, connect them with lines, click Run.
This is where you spend most of your time on MOD.

The Workflow Builder lives at its own URL (workflows.your-mod-domain)
and opens in a new tab from the Dashboard sidebar.

**Related:** [Workflow](#workflow), [Node](#node), [Workflow Wizard](#workflow-wizard)

---

## Dashboard

**The home page for everything that isn't workflow-building.** The
Dashboard is the rest of MOD — users, files, settings,
analytics. Workflows themselves live in the Workflow
Builder; the Dashboard is for managing your tenant.

---

## Assistant (Robot icon)

**Chat help on every page.** The robot icon in the top-right of
every page opens MOD Assistant — a chat that can take you to any
page, open any dialog, answer "how do I X" questions from the docs,
and help troubleshoot failed runs.

If you don't know where something is, ask the Assistant first.

**Related:** [Support Chat](#support-chat)

---

## Support Chat

**Reach a human (or a smart bot) for help.** The Support Chat is for
"I'm stuck and the Assistant didn't help" cases. It can answer FAQ
from the docs and, if needed, escalate to the support team.

**Related:** [Assistant](#assistant-robot-icon)

---

## Run History

**A log of every workflow run you've done.** Open a saved workflow
to see its Run History — date, who triggered it, how long it took,
whether it succeeded. Click any run to inspect what happened on
each node.

**Related:** [Workflow Run](#workflow-run)

---

## Onboarding

**The first-run setup wizard.** On first sign-in MOD walks you
through your profile: username, name, and company (new accounts
only). On self-hosted installs the wizard has no other steps. You
can change your name and username later from the account page.

---

## Sandbox

**The no-cost edition.** Community Edition is free and has no caps: there is no Free plan, no credit allowance, and no shared worker pool. CE runs on the workers you install. Paid plans are a paid-edition feature.

**Related:** [Plan](#plan)

---

## Cog

**The internal name for a file or folder.** Everything in your Data
area is stored as a "cog" — a `master_folder` (your "My Files"
root), an `asset_folder`, or a `file`. You never see the word in the
everyday UI, but it's why the API's file endpoints live under
`/v1/assets` (see the [API Reference](/api-reference)).

**Related:** [File / Data / Files Browser](#file--data--files-browser)

---

## Manager

**A regional control-plane node.** In larger, multi-region
deployments MOD runs one or more *managers* — each a self-contained
stack that owns the work and data for its region. Adding a manager
is an operator task (System → Managers). Most tenants never interact
with managers directly.

**Related:** [Worker](#worker)

---

## Persistent Stack

**A long-lived service MOD runs for you.** Beyond one-off workflow
runs, you can deploy a *persistent stack* — a docker-compose project
(e.g. Postgres, Redis, or nginx) that stays running on a worker
between jobs. Managed from the **Stacks** page (create, deploy,
stop, tear down).

**Related:** [Worker](#worker), [Container](#container)

---

## Still got questions?

Click the **robot icon** in the top right of any page and ask the
[Assistant](#assistant-robot-icon) — it knows this glossary and
the rest of the docs.
