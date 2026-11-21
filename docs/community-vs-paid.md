# Community Edition vs. other editions

MOD Core is open-core. This repository is the **Community Edition (CE)**: a
runnable, batteries-included workflow engine (Apache-2.0). Mod Tech Labs Inc.
also offers **MOD Cloud** and other editions, distributed separately, that build
on the same engine and add operational capabilities that are intentionally **not**
included here.

**What CE is for:** one organization running its own workflow engine on its own
machines. **What it is not:** a hosted service or a multi-organization manager;
those come from the other editions. Support for CE is best effort.

This page is a boundary map, not a feature announcement: everything listed as
"Community" ships in this repository today; everything listed as "Other editions"
lives outside this repo's source.

## Feature boundary

| Feature area | Community Edition | Other editions |
| --- | --- | --- |
| Workflow engine (DAG execution, orchestrator, scheduler) | Yes | Yes (same engine) |
| Go Worker (task execution, hardware detection) | Yes | Yes |
| WorkflowBuilder (visual editor) and Dashboard | Yes | Yes, plus hosted operations UI |
| Node authoring (hand-written + SDK) | Yes | Yes |
| Node Wizard (docs-driven, BYO/local-LLM) | Yes — wizard UI, API, and the `build-task` / `containerizer` builder images and `base-*` base images (all published to `ghcr.io/modtim`). Built nodes stay on the machine that built them; an optional small `registry:2` compose profile shares them with more than one worker. See [node-builder.md](./node-builder.md). | Yes, plus hosted/managed build services |
| Tenancy | Single default tenant (basic tenant list/create/delete only) | Managing more than one tenant |
| Authentication | Local auth via Keycloak; admin MFA (enrolment + recovery codes) is available, and enforcement is opt-in through the `auth.mfa_required_for_admins` / `auth.mfa_required_for_all` platform settings | Same, plus additional provisioning integrations |
| Secrets backend | OpenBao (MPL-2.0, Vault-API-compatible) bundled in the Community compose file; tenant secrets are encrypted with a local per-tenant key | Same OpenBao/Vault path, plus a managed per-tenant key provider (see key shredding below) |
| Persistent Stacks (on-demand model/stack lifecycle) | Basic lifecycle (start/stop, local/BYO endpoints) | Predictive pre-warm/characterization, warm-time-aware optimization |
| Compute scaling | As many workers as you run yourself, one Kubernetes cluster or several | Burst/GPU-pool, mesh/federation (tailnet, DERP, manager failover) |
| Node pack: Input/Export, auto-converters, Human Approval, web search | Yes | Yes |
| Node pack: basic FFmpeg (trim/concat/transcode) | Yes | Yes, plus advanced FFmpeg profiles |
| Node pack: basic image (resize/crop/convert/watermark) | Yes (resize, crop, rotate, metadata, strip-metadata, watermark — text overlay only) | Yes |
| Node pack: basic audio | Audio Input and Audio Converter ship; no audio-processing nodes are in the CE seed-list (`node-seed-keeplist.yaml`) | Yes |
| Node pack: BYO-LLM text + basic RAG (BYO vector/embeddings) | BYO-LLM text node seeded; basic RAG not yet | Managed-AI hosted LLM/RAG |
| Node pack: AEC/field-capture vertical (capture-preflight, COLMAP) | No | Yes |
| Node pack: advanced ML trainers, enterprise external executors (e.g. Kubernetes/Qube/Deadline-style) | No | Yes |
| K8s stack backend | Yes, one cluster or several (executor tasks + persistent stacks) | GPU sharing, per-stack network policy, HA |
| Scheduling | Overlap scheduler (runs overlap on a worker when the hardware allows) | Same |
| Limits | No caps on workers, users, concurrent runs or storage from the product's own licensing; ordinary API rate limits (6000 per minute per key) and a 1 TiB workflow checkpoint quota apply; the only optional quota is an LLM token budget you set yourself (an install holds one tenant) | Limits on the hosted service |
| Storage | Built-in object store, bring-your-own S3, and a custom container registry | Same |
| Usage metering | LLM call log with an optional monthly token quota that you set yourself (off unless you set it), and a resource-usage report (`GET /v1/road/analytics/resource-usage`) | Plus chargeback / cost reporting |
| Audit log | Your own audit log (list, detail, plain export) | Tamper-evident signed export and integrity verification, long retention |
| Roles | The built-in system roles (Admin, Standard User, Guest, External Collaborator) and assigning them to users | Custom roles and capability sets, cross-organization role views |
| Compliance surfaces (evidence bundles, SOC2/CMMC/FedRAMP, data markings/export approvals, break-glass, legal hold) | No | Yes |
| Upload malware scanning (clamd scan of uploads/downloaded artefacts) | No | Yes |
| Per-tenant key shredding on tenant deletion | No | Yes |
| Support | Community (issues, discussion) | Hosted support chat/tickets |

### Edition notes

- Malware scanning of uploads is not included in MOD Community Edition
  (available in other editions). CE uploads store without a scan; no upload is
  ever refused with a 402/422/503 because of a scanner.
- Tenant deletion in Community Edition deletes the tenant's stored data but
  does not cryptographically shred per-tenant keys (key shredding is available
  in other editions).
- If you open a page that belongs to another edition, MOD shows a notice that it
  is not available in this edition and the menu entry is hidden.
- An API path that does not exist returns HTTP 404 with `{"detail": "Not Found"}`.

### Not in Community Edition today

- Audio-processing nodes (beyond Audio Input / Audio Converter) and basic RAG
  (BYO vector/embeddings) are not included in Community Edition today. They
  are not in `CORE/API/services/node-seed-keeplist.yaml` — the sole CE node-inclusion
  source (`CORE/API/services/ce_inclusion.py`) — so they do not seed into a
  fresh CE install.
- The CE build tool (not shipped in the extract) refuses any file the
  manifest neither allows nor denies, so no file ships by accident.

## Why the split

Community Edition is the full single-tenant engine — unlimited, free, and
yours to self-host: no caps on workers, users, concurrent runs or storage
(an install holds one tenant; the only quota is an optional LLM token budget you set yourself). Additional capabilities are available in
separately distributed editions and in MOD Cloud, which add the operational
surface a hosted service needs (scale-out, managed integrations and
commercial node packs) built on top of the same engine. The split keeps
the engine itself fully open.

## Contributing

Found a gap, bug, or missing node in the Community Edition? See
[CONTRIBUTING.md](../CONTRIBUTING.md) for how to propose a change, the DCO
sign-off requirement, and the PR process.
