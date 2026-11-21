"""node_definitions router — thin domain aggregator.

Gate 2a of the `stack-training-and-prewarm` openspec change SPLIT this file
(formerly ~13.5k lines / 106 endpoints spanning 8 domains) into the
`node_definitions_domains` sub-package — one module per domain from the API
audit's classification.json (design.md Workstream B, D5/D10; see
docs/api-audit/deep/monster-file-split.md + domain-clusters.md).

This module is now a behaviour-preserving AGGREGATOR: it re-exports the shared
helpers/models/constants (from ._shared) and every endpoint function (from the
per-domain sub-modules) so existing `from routers.node_definitions import
<name>` imports keep working, and it rebuilds `router` / `external_router` by
including each sub-module's router. main.py's existing include_router calls
therefore mount every route at the identical path / method / dependency /
schema / tags as before the split. No paths, methods, dependencies or schemas
changed — the actual service/CRUD refactor is Gate 2b.

Tag handling: the aggregator's routers carry the tags (exactly as the original
definitions did); the sub-routers carry none, so include_router + add_api_route
do not merge the tag twice. The `/v1/node-authoring` external surface keeps its
prefix on the aggregator's external_router; the per-domain external routers are
prefix-less and included into it.

UPDATE (clean-v2-api / node-definitions-collapse wave): every endpoint that
used to live under `/v1/node-definitions/*` has moved to the clean `nodes`
family — `routers/nodes.py` (`/v1/nodes/*`), `routers/nodes_builds.py`
(`/v1/woodsman/*` — the wizard build pipeline + worker callbacks),
`routers/nodes_marketplace.py` (`/v1/nodes/marketplace`, `/v1/nodes/packages`)
and `routers/nodes_remediation.py` (`/v1/nodes/remediation`). Those new
routers re-register the SAME functions this module's sub-modules define
(imported via `from routers.node_definitions import <name>`, which keeps
working — see below) at their new paths; the migrated functions' own
`@router.*`/`@external_router.*` decorators have been removed from the
`node_definitions_domains/*` sub-modules they live in, so `router` below is
now an EMPTY aggregator for every sub-module except the still-unmigrated
`/v1/node-authoring` external contract (node_marketplace.py, node_wizard.py
and task_queue_scheduler.py's `external_router`s — zero known callers found
in Worker/DockerContainers/WorkflowBuilder/Dashboard as of this wave, left
in place pending a follow-up audit). The functions/models themselves are
untouched and still importable from here for that reason. `/v1/node-definitions/*`
itself now serves nothing; main.py's mount of it is unchanged by this file
(main.py is the coordinator's to edit) pending every caller being repointed —
see the wave's report for the full caller list (DockerContainers/ImageBuilder
scripts hit these paths directly by hardcoded URL and must be repointed
before that old mount is dropped).
"""

from fastapi import APIRouter

# Re-export shared helpers, Pydantic models, imports and module constants so
# other modules/tests that do `from routers.node_definitions import <helper>`
# (or reference `routers.node_definitions.<name>`) keep resolving unchanged.
# (_shared re-stamps these objects' __module__ back to 'routers.node_definitions'
#  so the contract snapshot's model/dependency identities are preserved.)
from .node_definitions_domains._shared import *  # noqa: F401,F403

# Per-domain sub-modules (each owns an APIRouter with only its domain's endpoints).
from .node_definitions_domains import (  # noqa: F401
    assets_cogs_files,
    iam_permissions,
    node_authoring,
    node_marketplace,
    node_wizard,
    task_queue_scheduler,
    workers_fleet,
    workflows,
)

# Re-export each domain's endpoint functions by name (tests + services import
# handlers directly, e.g. `from routers.node_definitions import build_variant`).
from .node_definitions_domains.assets_cogs_files import *  # noqa: F401,F403
from .node_definitions_domains.iam_permissions import *  # noqa: F401,F403
from .node_definitions_domains.node_authoring import *  # noqa: F401,F403
from .node_definitions_domains.node_marketplace import *  # noqa: F401,F403
from .node_definitions_domains.node_wizard import *  # noqa: F401,F403
from .node_definitions_domains.task_queue_scheduler import *  # noqa: F401,F403
from .node_definitions_domains.workers_fleet import *  # noqa: F401,F403
from .node_definitions_domains.workflows import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# Aggregated routers — identical construction to the pre-split definitions.
# main.py mounts `router` at /node_definitions (+ /v1) and `external_router`
# at '' (+ /v1); the sub-routers below reproduce every original route.
# ---------------------------------------------------------------------------
router = APIRouter(
    tags=["Node Definitions"],
    responses={404: {"description": "Not found"}},
)

external_router = APIRouter(
    prefix="/node-authoring",
    tags=["External Node Authoring"],
    responses={404: {"description": "Not found"}},
)

# Include order follows each domain's first-appearance line in the original
# file, preserving the pre-split route registration order.
for _domain_module in (
    node_wizard,
    node_authoring,
    assets_cogs_files,
    workflows,
    iam_permissions,
    task_queue_scheduler,
    workers_fleet,
):
    router.include_router(_domain_module.router)

for _external_domain_module in (
    node_wizard,
    task_queue_scheduler,
    node_marketplace,
):
    external_router.include_router(_external_domain_module.external_router)

del _domain_module, _external_domain_module
