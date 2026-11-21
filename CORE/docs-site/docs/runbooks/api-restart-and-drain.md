---
sidebar_position: 52
slug: /runbooks/api-restart-and-drain
title: Restart the API without losing runs
description: "Runbook: ship new API code on a self-hosted install by restarting the API container, after draining in-flight work and inside the worker self-fencing window so no worker stops the persistent stacks it hosts."
---

# Restart the API without losing runs

Last reviewed: 2026-10-07

Ship new API code by restarting the API container, keeping the downtime inside
the worker self-fencing window so no worker stops the persistent stacks it
hosts, and without abandoning in-flight workflow runs or queued tasks.

## When to use

- You changed the API's code and need it served. On a self-hosted install the
  API runs its code bind-mounted, so a code-only deploy is a container
  restart — no image rebuild.
- Do NOT use this while a reboot-window or restore runbook is mid-flight;
  those restart the whole install themselves.

## Pre-checks

Set `MOD_API_BASE` to your install's API URL (the one you open the Dashboard on;
on a local CE install that is `http://localhost:8000`).

- [ ] API is up: `curl -sk {MOD_API_BASE}/health` — expect a JSON liveness
      payload with status 200.
- [ ] Count what is still alive before you stop anything (see Steps 1-2 for
      how to query runs and queued tasks). Both numbers should be small; if
      they are large, the drain will take a while. That is expected — do not
      skip it.

## Steps

1. **Stop dispatching new work.** Set the platform setting that pauses
   dispatch — the key is `dispatch.paused` (group `ops`, default `false`), on
   the Dashboard **Settings** page's **Platform Settings** tab, not an env
   var (`POST /v1/system/drain` does the same thing and reports in-flight
   counts), so no new workflow runs or task claims start. Workers keep finishing
   what they already hold.
2. **Wait for in-flight work to finish.** Poll until both are zero:
   - workflow runs in a running state for the active workflows, and
   - pending/running rows in the platform task queue (workers claim from
     this queue; a task left mid-flight when the API dies has to be re-driven,
     so finish it first).
   Re-check after each minute. Do not proceed on a "looks idle" guess.
3. **Restart the API.** Run every `docker compose` command below from the checkout root (the directory with `docker-compose.yml` and `.env`).
   ```
   docker compose restart api
   ```
   Time it: start a timer at the restart command and stop it when `/health`
   answers again (Step 5).
4. **Do not exceed the fence lease.** The lease is not an env var you set: the
   manager derives it and pushes it to every worker on each heartbeat. It is
   `stack_observation_grace_s` (default 180) minus `stack_fence_margin_s`
   (default 60) = **120 seconds** by default, floored at 30 s and clamped below
   the grace period; both keys live on the same Platform Settings tab as Step 1.
   A worker whose own outbound calls to the manager keep failing past that lease
   **self-fences and stops the external GPU stacks it hosts** — correct behaviour,
   but every stack then pays a stop-and-wake cycle. If your restart is going past
   ~120 s of downtime, stop: something is wrong (port hang, slow boot). Investigate
   before restarting again.
5. **Verify the API came back.**
   ```
   curl -sk {MOD_API_BASE}/health
   ```
   Expect 200. Then read the boot log **redacted** — never print raw logs,
   they can carry token material:
   ```
   docker compose logs --tail 50 api | grep -vE "Bearer|authorization|access_token"
   ```
   Expect a clean startup (no tracebacks).
6. **Re-check the drain counters** from Step 2 — they should be zero, and
   new work should be dispatching again (undo the Step-1 pause).

## Verify

- `/health` answers 200 within ~120 s of the restart command.
- The redacted boot log shows no exceptions.
- No worker reported a fence event during the window (their stacks stayed up;
  check worker heartbeat status, not the API container).
- The pending count in the platform task queue is not growing.

If verification fails, go to Rollback immediately; do not improvise.

## Rollback

- If the new code is bad: restore the previous code (revert the files to the
  previous revision in your tree) and restart the API again. If the deploy
  was image-based, restore the previous image tag instead.
- Never `docker commit` a live container to "save" a working state — it
  freezes stale environment and layers and becomes an untraceable artifact.
- If a fence already fired (stacks stopped), do not restart the API again
  hoping to fix it: let the stacks wake through their normal deploy path one
  at a time, and find the downtime cause before repeating Step 3.

## Related

- Owner restart window — full checklist
  — a drained window that also swaps images and settings.
- [Deploy and recover a persistent stack](/runbooks/stack-deploy-and-recover)
  — how a fenced stack wakes back up.
- [Database restart checks](/runbooks/database-pooler-dns-recovery)
