---
sidebar_position: 27
sidebar_label: AI Deployer Duties
slug: /ai-deployer-duties
title: AI Deployer Duties
description: "What a customer that deploys AI features built on MOD is responsible for: human oversight, using models as documented, input-data quality, monitoring and incident reporting, keeping the records MOD produces, telling affected people, AI literacy of their staff, and their own assessment for high-risk use."
---

# AI Deployer Duties

MOD is the platform and the tool provider: it gives you the workflow tools, the node
palette, the models that run on your own hardware, and the records described in the
AI Use Statement. When you put MOD output in front of your
own end users, you are the one presenting that output, so the duties listed here are
yours. This page describes them in plain language. It is not legal advice, and it does
not name the obligations that apply to you: confirm your own obligations, and the
wording you will use for them, with your legal or compliance team.

## What MOD provides

MOD is designed to help a deployer do these things, not to decide them.

Built today:

- **Capability tags on nodes.** Nodes that can carry out uses that the usage rules
  forbid are tagged, and a run that includes such a node is stopped until
  your team records that it understands the rule. The acknowledgement is a tenant
  setting, so it is per workspace, and the stop happens before the run starts.
- **A per-output record.** Every AI output is stored with its run, the node run that
  produced it, a content hash, and the file metadata, so you can trace an output back
  to the inputs and the steps that made it.
- **Watermark records.** When media is watermarked (visible, invisible, or forensic),
  the act is written to the audit trail with the watermark type and id.
- **An audit trail you can read and export.** The trail is append-only, and MOD has an
  export route for it.
- **An incident export job** a platform admin can start to package records for one
  incident.
- **A self-serve full data export** (workflows, files, outputs, audit log) in a
  machine-readable format.
- **Likeness consent records** for voice and face work: who was consented, the scope,
  expiry reminders, and revocation.

Planned:

- **Planned:** a visible "AI-generated" label rendered on likeness and deepfake-class
  outputs at the point of display. The tenant setting and the decision helper exist;
  the media writers that carry the label are still being built.
- **Planned:** provenance metadata (C2PA / XMP) written into exported media, so the
  disclosure travels with the file.
- **Planned:** public model-card pages for the bundled models. A model-card template
  and a bias-evaluation report template exist internally for your team to fill in.

## What the deployer must do

Each of these is your call to make and your record to keep.

### Human oversight

Decide where a person reviews AI output before it is acted on or published, and put
that step in the workflow, not only in your head. MOD runs the nodes you wire; it does
not read your output for you, and it does not approve it. Where a wrong output could
reach a person and change something about them, the review step and the people who do
it should be written down in your workflow documentation.

### Using models per their instructions

The models MOD runs on your hardware are published by others. Their publishers write
what each model is for and what it is not for. Read that documentation before you
build, and stay inside the intended use. MOD describes the models it bundles and links
their cards; MOD does not retrain them and does not speak for them.

### Input-data relevance

If your output is about people, the data you feed matters. Keep the input data
representative of the people you are actually presenting output to, keep its source
and quality documented, and keep the record of what was fed in. MOD records the inputs
of each run; whether the inputs were fit for your purpose is your assessment.

### Monitoring and incident reporting

Watch the AI features in service, not only at build time: check outputs in normal use,
note failure modes you see, and report anything that matters to MOD. Your own support
policy defines the severity scale and the email channel; the platform-admin incident
export job packages the records MOD holds for one incident, and the audit trail export
gives you the wider record. Keep what MOD produces: the audit trail, the per-output
provenance records, the watermark events, and the consent records. They are your
evidence, and MOD keeps them for you (the audit trail has no hard-delete path).

### Telling affected people

Where an incident or a change of practice affects people who use your deployed
features, tell them. MOD does not contact your end users; the notice, its timing, and
its wording are yours.

### AI literacy of your staff

People who build, operate, or support MOD AI features should know what those features
do, what they do not do, where their output can be biased, and where human review sits.
Your own staff training should carry an AI-literacy course for the people who run these
features, given at onboarding and annually; if you staff the deployed side yourself, a
matching course for them is a duty you should arrange.

### High-risk use cases need your own assessment

If your deployed system makes decisions about people (employment, education, health,
finance, legal proceedings, or anything where a wrong decision harms a person), the
Act's high-risk branch likely applies to you, and MOD cannot assess it for you. You
should carry out your own assessment of the risks of your use case, write down the
measures you take about them, and keep the record. MOD's job here is to not prevent
that: the tags, the run gate, the provenance records, and the exports are there so your
assessment can be built from real platform records.

## Where MOD's own write-ups live

- AI Use Statement — how MOD uses AI, and the line that you
  remain responsible for verifying outputs and complying with applicable law.

## Not answered here

This page does not tell you which obligations apply to you, what to write in a
compliance record, or when a rule stops applying. Ask your legal team. The Community
Edition does not ship a compliance answer library or trust pages, so those questions
are answered by your own programme, not by this repo.
