---
draft: true
sidebar_position: 26
slug: /deprecation-policy
title: Deprecation Policy
description: How MOD marks things deprecated or retired, what counts as a breaking API change, the 12-month notice window for public /v1 endpoints, how notices are delivered, and how deprecations work on an on-prem install.
---

# Deprecation Policy

MOD is a running system: nodes, models, workflows and API endpoints move over
time. This page states the rules for how that happens, so a build you wrote
last year still behaves the way you expect, and so a change that does affect
you is announced before it takes effect.

This page is a **draft**. It is published for review and may change.

## What counts as a breaking change

A change to a public API endpoint is **breaking** when a client that was
written against the old behaviour can no longer rely on it. Concretely, a
change is breaking when it:

- removes an endpoint, or removes it from the public surface;
- changes the request method, path, or the meaning of a path parameter;
- adds a required request field, or removes a response field a client reads;
- changes a response status code, error shape, or pagination contract;
- changes units, defaults, or the type of a field a client sends or parses.

A change is **not** breaking when it only adds an optional request field, adds
a new response field, widens an accepted range, or improves a description.
Clients that ignore unknown response fields keep working.

## The notice windows

| What is changing | Notice before it takes effect |
| --- | --- |
| A public `/v1` API endpoint is removed or changed in a breaking way | 12 months |
| A node or model is retired | 6 months |
| A node or model is deprecated but still runnable | stays runnable for at least 12 months after a newer version ships |

Both windows are platform settings, not fixed numbers in the code:

- `deprecation.notice_months` — the notice window that starts when something
  is marked deprecated. Default **6**.
- `deprecation.min_runnable_months` — how long a deprecated or retired node
  stays runnable for workflows that pinned it. Default **12**.

The node lifecycle itself is read through `lifecycle.notice_months` and
`lifecycle.min_runnable_months`, which fall back to the `deprecation.*`
keys above. The retirement sweep and its notices have their own settings:
`retirement.min_runnable_months` (the window the sweep enforces, default
**12**), `retirement.notice_lead_months` (how far ahead of the retirement
date the notice email goes out, default **6**),
`retirement.allow_below_minimum_window` (off by default; turning it on lets
an install shorten the enforced window deliberately) and the
`retirement.dry_run` / `retirement.notice_emails_enabled` /
`retirement.notice_emails_dry_run` switches for previewing a sweep without
touching anything.

A node carries a lifecycle `status` of `active`, `deprecated`, or `retired`,
plus `deprecated_at` and `retire_after`. A node cannot be marked retired
before it has been deprecated, and `retire_after` must be at least
`deprecation.min_runnable_months` after `deprecated_at`.

## How the notice is given

For a node or model retirement, MOD sends one notice to the tenant admins
whose tenants are affected, and records the change in the changelog.

- **Email to affected tenant admins** — built. A daily sweep finds
  deprecated nodes carrying a retirement date, works out which tenants use
  them, and sends one templated notice per node. The send is stamped on the
  node so the same notice is never sent twice.
- **Changelog entry** — built. The changelog is a published record of what
  changed; every deprecation and retirement gets an entry.
- **Palette badge** — Planned. A deprecated or retired node is meant to
  carry a visible badge in the node palette while it is still selectable.
- **`Deprecation` and `Sunset` HTTP response headers** — Planned. When a
  public `/v1` endpoint enters a notice window, responses from that endpoint
  are meant to carry a `Deprecation` header naming the replacement and a
  `Sunset` header giving the date the old behaviour stops. Today no endpoint
  sends those headers.
- **Changelog entry for an API change** — Planned for the API case. The
  changelog page exists; the 12-month API notice is not wired to it yet.

A client that needs to know whether an endpoint is going away should read the
changelog and, once the headers ship, the response headers on the endpoint
itself.

## The security exception

Sometimes keeping the old behaviour is itself the risk. When an endpoint or
node is being removed because the old behaviour exposes data or capability a
caller should not have — an authentication path that accepts a weaker
credential, an endpoint that returns more than a caller's plan should see —
MOD does not hold the old behaviour open for a full window. Those removals
ship sooner, with a notice that states what was removed and why. The shorter
window applies only to changes of that kind; ordinary removals and
replacements keep the full window.

## Versioning

The public API version in use is `/v1`. Every public endpoint is served under
that prefix.

- `/v1` stays. A deprecation never changes the version prefix, and never
  reuses a `/v1` path to mean something else.
- A new major version runs **in parallel** during the notice window. If a
  change is too large to be a notice on an existing endpoint, the new shape
  is published as a second version alongside `/v1`, both are live for the
  length of the window, and the old one is retired at the end of it.
- Planned: MOD does not run a `/v2` today. When one is introduced, it is
  announced at the same time as the notice on the `/v1` endpoint it replaces.

## On-prem installs

On an on-prem install, MOD does not change anything on its own. Deprecations
arrive when you take a release of the platform, and the notice window counts
from that release, not from the date the change was decided upstream.

- Your install keeps the node and model versions it has until you upgrade.
- Upgrading is a choice you make; a pinned workflow keeps running on the
  version it pinned until you choose to upgrade it.
- Planned: on an on-prem install, the release notes for the release you
  installed are the notice, and the changelog page in your install lists the
  deprecations that release carried.

## Choosing to upgrade

A workflow records the exact node and model versions it ran with. Re-running
it uses those same versions, which is what makes a re-run reproducible. To
move a workflow onto current versions you choose "upgrade" explicitly; nothing
upgrades a pinned workflow by itself.
