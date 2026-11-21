"""node_definitions_domains — per-domain split of the node_definitions router.

Created in Gate 2a of the stack-training-and-prewarm openspec change to let
Gate 2b migrate each domain independently. Each sub-module owns an
`APIRouter` carrying only its domain's endpoints; routers/node_definitions.py
aggregates them so main.py mounts everything at identical paths.
"""

from . import (  # noqa: F401
    assets_cogs_files,
    iam_permissions,
    node_authoring,
    node_marketplace,
    node_wizard,
    task_queue_scheduler,
    workers_fleet,
    workflows,
)
