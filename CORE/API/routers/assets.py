# API/routers/assets.py
"""Assets — the clean `/v1/assets` resource (clean-v2-api "workflows + runs +
assets + files" wave, design.md target resource model: "assets (cogs)").

This REPLACES `/v1/cogs` as the primary asset surface: file/folder hierarchy,
uploads (multipart + hash-dedup), versions, tags, shareable links, thumbnails,
converted previews (STEP/point-cloud), downloads (single/zip/folder/jobs),
processing history, classification, legal hold. The `Cog` DB model / `cog_id`
field naming stays as-is internally — a data-model rename is out of scope for
this wave (design.md's collapse rules govern the URL surface, not storage) —
only the address changes.

REUSE, NOT REWRITE: same router objects `routers.cogs` already builds
(`router`, `service_router`, `public_router` — aggregates of
`routers/cogs/*.py`'s domain sub-modules: assets_cogs_files, workflows,
task_queue_scheduler, usage_metering). None of those bake an absolute
prefix into their own decorators (relative paths throughout), so the
package is reused here unedited via a plain re-export — behavior/auth/
validation are byte-identical, only the URL address changes.

The OLD `/v1/cogs/*` mount has been REMOVED (clean-v2-api final clean-cut):
every caller that used to hit it directly by hardcoded path — the baked
CORE/DockerContainers/* node-container images (file-hasher, InputValidator,
zip-download, PlatformExport, ReturnExport, *-input/validate.py),
CORE/Dashboard/js/*, and the listed WorkflowBuilder components — has been
repointed to `/v1/assets/*` and (for the baked images) rebuilt + re-pushed.
Dev server — no back-compat.

Note: `cogs` (this domain) is a Cloud/Paid-only module — absent from the
open-source CE build (denylisted in open-core/manifest.yaml) — so this
router must stay guarded behind the same `try/except ImportError` block
main.py already wraps the `cogs` import in.
"""
from routers.cogs import router, service_router, public_router  # noqa: F401 (re-exported for main.py's mount)

__all__ = ["router", "service_router", "public_router"]
