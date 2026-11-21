---
sidebar_position: 54
slug: /runbooks/stack-deploy-and-recover
title: Deploy and recover a persistent stack
description: "Runbook: start, re-start, or move a persistent stack (an LLM server, an embedding model, an upscaler) on a worker of a self-hosted MOD install, and clear the deploy failure modes that leave a stack stuck while its GPUs stay held."
---

# Deploy and recover a persistent stack

Last reviewed: 2026-10-07

Start, re-start, or move a persistent stack — a persistent compose project
such as an LLM server, an embedding model, or an upscaling engine — on a
worker of your install, and clear the deploy failure modes that leave a stack
stuck while its GPUs stay held.

## When to use

- A stack row reads `stopped`, `failed`, or `deploying` and will not come back.
- A deploy is stuck and the cards on that box are not being released.
- Do NOT use this to ship API code — see
  [Restart the API without losing runs](/runbooks/api-restart-and-drain). Do
  not use it while a host reboot is in progress. Stacks stay down until
  their box is back. After a worker reboot, redeploy each stack that was
  running on it by hand from the stack page (or the API) once the worker is
  back online.

## Pre-checks

Set the base once and get `$TOKEN` from the Getting Started token step
(`$BASE` has no `/v1`; the paths below carry it):

```
BASE=http://localhost:8000
AUTH="Authorization: Bearer $TOKEN"
```

- [ ] `curl -sk -H "$AUTH" $BASE/v1/stacks/{stack_id}` — expect a JSON stack
      with a `status` and a `target_worker`. A `null` `target_worker` means
      the stack has no host on record, so there is nowhere to ask for logs
      and the deploy must name a worker.
- [ ] `curl -sk -H "$AUTH" $BASE/v1/stacks/{stack_id}/definition` — expect the
      stored compose spec. That is what the next deploy renders; read it
      before deploying, not after.
- [ ] `curl -sk -H "$AUTH" $BASE/v1/workers` — expect the worker list with a
      hardware report per box. The card sizes on that report are what the
      claim guard compares the stack's accelerator demand against.
- [ ] No live deploy/migrate task already sitting on this stack (see Known
      issue 2 for why that matters). Check before deploying if a deploy is
      already stuck.

## Steps

1. Deploy or re-deploy the stack.
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/stacks/{stack_id}/deploy
   ```
   Expect the stack to read `deploying`, then `running` when the deploy task reports success. An optional `?to_worker=<name>` pins
   the deploy to one box; omit it and the task is born `PENDING` and whichever
   capable worker claims it gets the work.
2. Watch the task, not the stack row. A stack deploy is an ordinary entry in
   the platform task queue. Poll the host's queue:
   ```
   curl -sk -H "$AUTH" $BASE/v1/workers/{worker_name}/queue
   ```
   Expect the deploy in `assigned` (PENDING or ASSIGNED) and then in
   `in_flight` (RUNNING or CLAIMED); when it is gone from both, it has
   finished. A deploy that stays in `assigned` with no logs is Known issue 1.
3. Read the stack's own logs if it died. The deploy operation returns compose
   output ("Starting / Started"), which says the container daemon accepted the
   container, not that it stayed up:
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/stacks/{stack_id}/logs
   ```
   Pipe any log output through a redaction filter; never print raw logs.
4. If the deploy is stuck, cancel it before starting another (Known issue 2
   fix — that cancel is the GPU release), then re-run step 1.
5. Stop deliberately without destroying — keeps the row, the compose spec and
   the data, and sends a stop to the host that runs the stack (the stop is
   refused with 409 when no host is on record):
   ```
   curl -sk -X POST -H "$AUTH" $BASE/v1/stacks/{stack_id}/stop
   ```
6. Move a stack to another box (platform admin):
   `POST /v1/stacks/{stack_id}/migrate`. It lands the stack on a different
   host and materializes package weights on the cold target, same as deploy.

### Known issue 1 — deploy sits in `ASSIGNED` indefinitely, no logs

- Symptom: the deploy task never moves past `ASSIGNED`; the box shows no
  container activity.
- Cause: the worker's card sizes are missing on its record. The claim guard
  builds the worker's capability vector from one row per physical device
  rather than the single summary size, and only fills a missing size from
  benchmark-measured values. A card with no size answers "no cards".
- Fix: re-read the worker's hardware report (`GET /v1/workers`) and get the
  box to heartbeat and benchmark again so the per-device rows carry sizes.
  Do not hand-edit the report.

### Known issue 2 — "unplaceable (all cards full)"

- Symptom: deploys log `unplaceable — all cards full`, including for stacks
  on other boxes.
- Cause: an older deploy/migrate task for the stack is still in a live state
  (`ASSIGNED`/`CLAIMED`/`RUNNING`) and its card assignment counts toward the
  in-use tally indefinitely.
- Fix: cancel the stale task first. A fresh deploy call supersedes it by
  itself (supersede, do not duplicate).

### Known issue 3 — "host port in use" with no containers on the box

- Symptom: the deploy refuses a host port, but the box shows nothing holding
  it.
- Cause: a stale copy on the worker record. Placement reads the host-bound
  port set written at heartbeat; an empty port set omitted by the worker
  leaves the previous reading in place.
- Fix: let the box heartbeat again with its real port set — the next heartbeat
  overwrites the key. Nothing on the stack side needs changing.

### Known issue 4 — a stack on a distant box is unreachable after redeploy

- Symptom: the stack row says `running`, the endpoint refuses, and the box is
  fine.
- Cause: the stack's external-network sidecar is not up. A stack that dials
  peers outside the install needs its egress carrier; without it the service
  has no route out.
- Fix: check the stack's egress sidecar is running before anything else —
  before suspecting the model, the compose, or the box. Then re-deploy.

## Verify

- `curl -sk -H "$AUTH" $BASE/v1/stacks/{stack_id}` — expect `status: running`
  and a `target_worker`.
- `curl -sk -H "$AUTH" $BASE/v1/workers/{worker_name}/queue` — expect the
  deploy task gone from both `assigned` and `in_flight`.
- `curl -sk -H "$AUTH" $BASE/v1/stacks/{stack_id}/endpoint` — expect the
  reachable endpoint for the service.
- No `unplaceable` line in the deploy task's error message.

If verification fails, go to Rollback immediately; do not improvise.

## Rollback

- Stuck deploy: cancel it (step 4) and stop. Do not keep re-deploying — each
  attempt adds another task holding cards.
- Wrong box: `POST /v1/stacks/{stack_id}/teardown` on the wrong host, then
  deploy with an explicit `to_worker`. Teardown destroys the stack and its
  storage (erase by default; `retain` keeps the last confirmed snapshot for
  the retention setting's number of days), so it is DESTRUCTIVE — stop (step 5)
  is the non-destructive down.
- A stack that stopped because the API was down past the fence lease: let it
  wake through its normal deploy path one at a time; do not restart the API
  hoping to fix the stack (see
  [Restart the API without losing runs](/runbooks/api-restart-and-drain)).
