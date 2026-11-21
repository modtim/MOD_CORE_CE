---
sidebar_position: 10
slug: /legal/ai-use-statement
title: AI Use Statement
description: How MOD uses AI, data handling, model transparency, and governance.
draft: true
---

# AI Use Statement

**Last updated: November 2026**

## How MOD Uses AI

MOD Core is a workflow platform that integrates artificial intelligence capabilities through containerized models and external provider APIs. AI features are tools that augment user workflows — they enable tasks like image generation, video processing, language understanding, and design analysis. MOD does not use AI to make automated decisions about people, create content without user consent, or substitute human judgment in sensitive domains.

Users control which AI features are enabled in their workspace, and each feature clearly indicates what data is sent where and which model is being used.

## Your Data Is Not Used for Training Without Your Opt-In

**Policy:** MOD's policy: customer content—files, prompts, workflow data, and outputs—is not used to train or fine-tune any model (MOD's own models or those of external providers) unless your organization turns on the AI training opt-in setting (off by default). The opt-in is tenant-wide.

This applies regardless of deployment mode (SaaS or on-premise).

**What we do track (without content):** MOD collects pseudonymized scheduling metrics to improve platform performance—job duration statistics, task counts per accelerator kind, and whether scheduling decisions came from the model or the heuristic. These metrics do not include your files, prompts, or workflow definitions. This data helps MOD optimize scheduler accuracy, predict job duration, and allocate resources fairly.

## Local-First Models

MOD prioritizes models that run locally on your infrastructure. Bundled models (e.g., text generation, image processing) execute in containerized environments you control, without customer data leaving your network. Local models:

- Process your data on your hardware
- Make no external AI API calls; the worker may probe the public Hugging Face hub to check model-cache availability
- Model cards for bundled models are planned and not yet published.
- Support fully offline workflows

## External AI Providers

Some workflows may use external AI services (e.g., specialized APIs, cloud-based inference). The following principles apply:

**Subprocessor Status:** External AI providers used by MOD are processed as data subprocessors under data protection law. MOD's data processing agreements with these providers prohibit use of your data for model training.

## Human Oversight and Control

MOD workflows are tools, not autonomous agents. Workflows execute on your schedule or explicit trigger, and all outputs are reviewed and decided upon by your team before use. MOD does not:

- Auto-generate decisions about people (hiring, lending, health, criminal justice)
- Deploy AI outputs without human review
- Assume liability for AI-generated content—customers decide how workflows and their outputs are used

You remain responsible for verifying AI outputs, understanding model limitations, and complying with applicable law (e.g., labeling AI-generated media, disclosure of AI use in regulated sectors).

## Governance Aligned to NIST AI RMF

MOD's AI governance aligns with the [NIST AI Risk Management Framework](https://airc.nist.gov/):

**Govern:** MOD maintains policies for responsible AI integration, data handling, and transparency. This statement and tenant data isolation document MOD's governance approach. Model cards for bundled models and bias evaluation reports are (planned). Admin audit-log views are available in the Compliance Center.

**Map:** MOD identifies AI risks in bundled models (e.g., bias, adversarial robustness, out-of-distribution failure) through local-first execution and documentation. When customers use MOD to build systems that make decisions about people, they may be operating a high-risk system under the EU AI Act and may need to perform their own impact assessment. Output labels are available via the X-MOD-AI-Generated header in assistant replies.

**Measure:** MOD measures AI system performance for bundled models. Evaluation reports for each bundled model are (planned). Customers can measure downstream impacts of AI workflows in their own domains.

**Manage:** MOD mitigates identified risks through:
- Local-first execution (data isolation)
- Tenant data isolation
- Transparency controls; admin audit-log views are available in the Compliance Center
- Clear labeling of AI outputs: assistant replies carry the X-MOD-AI-Generated header

## Contact

For questions about MOD's AI use, data handling, or to request audit information:

- **Security & AI questions:** admin@modtechlabs.com
- **Legal:** admin@modtechlabs.com
- **Privacy:** admin@modtechlabs.com
- [Data Subject Access Request](/legal/privacy-policy#organization-level-requests-tenant-dsar)
