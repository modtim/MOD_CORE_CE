# Node Version Pinning

Runs execute an **immutable image digest**, not a mutable `:latest` tag, so a
node update never silently changes an existing workflow. Upgrading is an
explicit, opt-in re-pin.

## Model

- **`node_versions`** — one append-only row per build, identity = `image_digest`
  (`uq_node_version_digest`). `version` is a per-node display counter (v1, v2…)
  assigned at record time, *not* `NodeDefinition.version` (which the build path
  never bumps).
- **`workflow_nodes.pinned_node_version_id`** — the build a workflow node runs.
  `NULL` = resolve the current build at dispatch (legacy).
- **`node_runs.executed_image_digest` / `node_version_id`** — the exact build a
  run executed, for reproducibility/audit.

## Flow

1. **Build** → `set_node_manifest_metadata` (imagebuilder digest callback) calls
   `crud.record_node_version`, recording the new immutable build. `:latest` may
   still move; the digest is the durable handle.
2. **Dispatch** → `enqueue_task_internal` calls `crud.resolve_node_run_pin`. When
   the node is pinned it sends `image_digest` (+ snapshot `gpu_variants` /
   `gpu_variant_digests` / `executable_path`) in the task payload and stamps the
   `NodeRun`. Unpinned nodes keep live resolution.
3. **Worker** → `resolveProcessingImage` / `pinToDigest` pull and run
   `<repo>@sha256:<digest>` when a digest is present, else the tag (legacy).
4. **Cache** → `NodeOutputCache` keys on the digest when present, so an old
   pinned build and a newer `:latest` no longer collide on the tag.

## Rollout order (IMPORTANT — do not do these together)

1. **Deploy the worker first.** The digest-pull change is inert: it only acts on
   payloads that carry a digest, and nothing sends one yet. Deploying it first
   guarantees no worker ever receives a digest it cannot pull.
2. **Apply the migration** (idempotent, dry-run by default):
   ```
   docker exec my_fastapi_app python3 \
     migrations/20260901_add_node_version_pinning.py            # dry run
   docker exec my_fastapi_app python3 \
     migrations/20260901_add_node_version_pinning.py --commit   # apply
   ```
   It creates the table/columns, backfills one `node_versions` row per node that
   has a digest, and pins existing `workflow_nodes` to their **current** build —
   so nothing changes under existing workflows. Digest-less legacy nodes stay
   `NULL` → tag path.
3. **Deploy the API.** Now dispatch starts sending digests for pinned nodes.
   Watch for pull failures and confirm `NodeOutputCache` hit-rate stays healthy
   (the key changes from tag to digest for pinned runs).

**Rollback:** with no digests in payloads the worker's tag path is unchanged; the
added tables/columns are additive and nullable. Reverting the API alone reverts
the behaviour.

## Verify on the server

- `record_node_version` fires on a rebuild → a new `node_versions` row with a new
  digest; the prior row is untouched.
- A workflow pinned to digest-A still runs digest-A after the node is rebuilt to
  digest-B (`node_runs.executed_image_digest` == A).
- A legacy no-digest task still pulls by tag.

## Follow-ups (out of scope here)

- **Frontend (Group 5):** show the pinned version on a node and an explicit
  "update to vN" re-pin action; pin newly-added nodes to the current build at
  save time. Needs one re-pin mutation — design alongside the UI, do not build a
  speculative endpoint first.
- **Per-variant digests:** the build pipeline records only the default
  `image_digest` today; `node_versions.gpu_variant_digests` is left `NULL` so
  variants pull by tag. Populate it when the builder emits per-variant digests.
- **Registry GC:** immutable history retains old manifests. Define a retention/GC
  policy separately; retained manifests are the price of reproducibility.
