"""Data-driven container_env for workflow nodes whose image reads cog-style env.

Some images were written for a cog dispatch (routers/cogs/_shared.py) that sets
their env directly. When the SAME image runs as a generic workflow node, nothing
sets that env and the container dies at startup (3d-thumbnail-gen:
KeyError CONTENT_HASH, then MINIO_BUCKET). This table maps a node_kind to the env
variables its image reads and WHERE each value comes from in the task payload,
which `enqueue_task_internal` has already resolved from the upstream outputs or
the input cog (`source_file_location`, `file_extension`, `cog_ids`).

Storage endpoint/credentials are NOT here: the worker mints them per task
(GET /workers/tasks/{id}/credentials) like every other node. Kinds without an
entry get {} so every other node is unchanged.

Source spec (the table value): a payload key, `key.N` for the Nth item of a list
payload key, or `@tenant_id` / `@bucket` for the dispatch's tenant id and tenant
bucket name.
"""
from typing import Any, Callable, Dict, Optional

CONTAINER_ENV_MAPPINGS: Dict[str, Dict[str, str]] = {
    # generate_thumbnail.py main(): CONTENT_HASH and MINIO_BUCKET are required;
    # FILE_EXTENSION defaults to glb; COG_ID / TENANT_ID are optional context.
    "thumbnail_3d_mesh_process": {
        "CONTENT_HASH": "source_file_location",
        "FILE_EXTENSION": "file_extension",
        "COG_ID": "cog_ids.0",
        "TENANT_ID": "@tenant_id",
        "MINIO_BUCKET": "@bucket",
    },
}


def _resolve(spec: str, payload: dict, tenant_id: Any,
             bucket_fn: Optional[Callable[[], Any]]) -> Any:
    if spec == "@tenant_id":
        return tenant_id
    if spec == "@bucket":
        return (payload.get("minio_bucket")
                or (bucket_fn() if bucket_fn else None))
    key, _, idx = spec.partition(".")
    val = payload.get(key)
    if idx:
        try:
            val = val[int(idx)]
        except (TypeError, IndexError, KeyError, ValueError):
            return None
    return val


def container_env_for_node_kind(
    node_kind: Optional[str],
    payload: dict,
    tenant_id: Any = None,
    bucket_fn: Optional[Callable[[], Any]] = None,
) -> Dict[str, str]:
    """Env for `node_kind` built from `payload`; {} for an unmapped kind.
    Empty/None values are omitted; FILE_EXTENSION drops a leading dot."""
    mapping = CONTAINER_ENV_MAPPINGS.get(node_kind or "")
    if not mapping or not isinstance(payload, dict):
        return {}
    env: Dict[str, str] = {}
    for var, spec in mapping.items():
        val = _resolve(spec, payload, tenant_id, bucket_fn)
        if val is None or val == "":
            continue
        val = str(val)
        if var == "FILE_EXTENSION":
            val = val.lower().lstrip(".")
            if not val:
                continue
        env[var] = val
    return env
