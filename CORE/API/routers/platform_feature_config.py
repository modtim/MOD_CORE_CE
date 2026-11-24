"""δ Phase 0 — feature-config endpoint that gates wizard features
on whether their platform-admin secrets are configured.

Wizard's Describe-and-build source type (δ) needs a GitHub PAT for
the search API. The token lives as a platform-tenant Secret row
(same pattern as harbor_username/password). This endpoint reports
whether each gated feature has its prerequisites in place so the
wizard can grey out tabs / show admin notices instead of failing
opaquely at build time.

Read by:
- WorkflowBuilder on wizard-open (gates the Describe tab)
- Any future client that wants to know if δ is available
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import crud
import models
from dependencies import get_db, require_active_and_onboarded_user

logger = logging.getLogger(__name__)


# Tenant-of-record for platform-shared secrets (Harbor, GitHub, etc.).
# Matches the convention in routers/harbor.py and routers/secrets.py.
PLATFORM_TENANT_ID = 1


router = APIRouter(
    prefix="/platform",
    tags=["Platform"],
    dependencies=[Depends(require_active_and_onboarded_user)],
)


class FeatureConfigResponse(BaseModel):
    describe_and_build_available: bool
    missing_secrets: List[str]
    is_platform_admin: bool
    # node-code-edit-rebuild Contract 3 — gates the wizard's node source
    # view/edit/copy/rebuild UI. Backend endpoints (routers/node_code_edit.py)
    # enforce the same env var independently (404 when off) — this field only
    # lets the client hide the UI instead of hitting a wall of 404s.
    wizard_code_edit_enabled: bool = False
    # conversational-project-builder §1.5 — gates the Project Wizard's
    # conversational (align → checklist → checkpoint) UI. The new
    # /projects/{id}/conversation family of endpoints enforce the same flag
    # independently (404 when off); this field lets the client render the
    # conversational surface instead of the legacy one-shot flow, and hide it
    # entirely (falling back to the legacy view) when the flag is off.
    project_conversation_enabled: bool = False


def _platform_secret_present(db: Session, name: str) -> bool:
    """True iff the named secret is set on the platform tenant and
    not still the auto-created PLEASE_CONFIGURE placeholder."""
    decrypted = crud.get_decrypted_secret_by_name(db, name, PLATFORM_TENANT_ID)
    if not decrypted:
        return False
    if decrypted.strip() == "" or decrypted == "PLEASE_CONFIGURE":
        return False
    return True


@router.get("/feature-config", response_model=FeatureConfigResponse)
def feature_config(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(require_active_and_onboarded_user),
):
    """Reports which gated features have their prerequisite secrets
    configured. Read by the wizard on open to decide whether to
    enable the Describe-and-build source tab.

    Note: this returns booleans only — never the secret values
    themselves. Surfacing the missing-name list helps the platform
    admin (who CAN configure them) know what to fill in via
    Settings → Secrets, but it doesn't leak whether a non-admin's
    own tenant has the secret (we always check the platform tenant).
    """
    missing: List[str] = []

    if not _platform_secret_present(db, "github_token"):
        missing.append("github_token")

    from utils.paid_import import optional_paid
    _pl = optional_paid("services.project_loop")
    project_conversation_enabled = (
        bool(_pl.project_conversation_enabled()) if _pl is not None else False
    )

    return FeatureConfigResponse(
        describe_and_build_available=("github_token" not in missing),
        missing_secrets=missing,
        is_platform_admin=bool(current_user.is_platform_admin),
        wizard_code_edit_enabled=os.getenv("WIZARD_CODE_EDIT_ENABLED", "false").lower() == "true",
        project_conversation_enabled=project_conversation_enabled,
    )


# =============================================================================
# Gate 12 — where this deployment's images come from
# =============================================================================

class RegistryEntry(BaseModel):
    role: str
    host: str
    env_var: str
    # Whether an operator set this, or it fell through to MOD's origin. The
    # distinction is the whole point of the panel: a deployment that BELIEVES
    # it is self-hosted while silently defaulting to registry.example.com is
    # the exact failure this gate exists to make visible.
    configured: bool
    note: str
    # Where an operator changes this. Some of these live in the encrypted
    # secret store and are editable in the UI; others are environment
    # variables that need a file edit and a restart. Linking the second kind
    # to a Secrets page it cannot be set on would be worse than no link.
    settable_in: str          # "secrets" | "env"
    secret_name: Optional[str] = None
    # Set when the same value is ALSO read from somewhere else. The push
    # registry is read from the `harbor_registry` secret by the build paths
    # (build_dag/submit.py, node_definitions.py) and from HARBOR_REGISTRY
    # everywhere else — so the two can disagree, and the panel must say so
    # rather than presenting one of them as the answer.
    also_read_from: Optional[str] = None
    # True / False / None. None is "not checked", which is a different claim
    # from False ("checked, nothing answers") and sends the reader somewhere
    # different. `configured` alone was answering the wrong question: whether
    # a variable is SET says nothing about whether a host exists.
    reachable: Optional[bool] = None


class RegistriesResponse(BaseModel):
    entries: List[RegistryEntry]
    deployment_mode: str
    # True when anything still resolves to MOD's origin. Named for what an
    # operator wants to know rather than for the mechanism.
    depends_on_mod: bool


@router.get("/registries", response_model=RegistriesResponse)
def get_registries(
    current_user=Depends(require_active_and_onboarded_user),
):
    """Which registries this deployment actually resolves to.

    "Where do my images come from" was previously answerable only by reading
    source — and the answer was frequently not what the environment said,
    because 33 hardcoded literals bypassed the env var. Now that they resolve,
    the resolved values are worth showing: configuration that cannot be
    inspected is configuration nobody trusts.
    """
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin only")

    import os
    from utils.registries import (DEFAULT_PLATFORM_REGISTRY, build_push_registry,
                                  platform_registry, registry_is_mod, configured_platform_registry,
                                  registry_reachable, tenant_registry)

    mode = (os.getenv("DEPLOYMENT_MODE") or "unknown").lower()
    tenant_var = "TENANT_HARBOR_REGISTRY" if mode == "saas" else "LOCAL_REGISTRY"

    tenant_host = tenant_registry()
    tenant_note = ("Images your tenants build. These already live on your own "
                   "infrastructure.")
    if not tenant_host:
        tenant_note = ("No local registry — not configured on this edition. CE "
                       "ships no registry container, so tenant-built images stay "
                       "as local images on the machine that built them. Set "
                       "LOCAL_REGISTRY to point at one you run yourself.")

    entries = [
        RegistryEntry(
            role="Platform images",
            host=platform_registry(),
            env_var="DOCKER_REGISTRY",
            configured=bool(configured_platform_registry()),
            note="Base images, build tooling and the system node catalogue. "
                 "Point this at your own mirror to stop depending on MOD.",
            settable_in="env",
        ),
        RegistryEntry(
            role="Your built images",
            host=tenant_host,
            env_var=tenant_var,
            configured=bool((os.getenv(tenant_var) or "").strip()),
            note=tenant_note,
            settable_in="env",
        ),
        RegistryEntry(
            role="Build push target",
            host=build_push_registry(),
            env_var="HARBOR_REGISTRY",
            configured=bool((os.getenv("HARBOR_REGISTRY") or "").strip()),
            note="Where a build pushes what it just made. Separate from the "
                 "pull source on purpose, so you can mirror one without the "
                 "other.",
            settable_in="secrets",
            secret_name="harbor_registry",
            also_read_from="the `harbor_registry` secret, which the build "
                           "paths prefer over this environment variable — if "
                           "they disagree, builds push somewhere else",
        ),
    ]
    for e in entries:
        e.reachable = registry_reachable(e.host)

    return RegistriesResponse(
        entries=entries,
        deployment_mode=mode,
        # Ownership, not default-equality: an install whose .env points the
        # platform registry at one of MOD's own hosts pulls from MOD even when
        # the edition's default is a public registry, and the old substring-of-
        # -default test missed that. The old test is kept as well, so paid/onprem
        # behaviour is unchanged.
        depends_on_mod=any(registry_is_mod(e.host)
                           or DEFAULT_PLATFORM_REGISTRY in e.host
                           for e in entries),
    )


# =============================================================================
# Gate 11 — which architectures each base image can run on
# =============================================================================

class BaseImageArch(BaseModel):
    display_name: str
    image_tag: str
    image_ref: str
    category: str
    # CPU / CUDA / ROCM / INTEL / NPU. Accelerator tiers are amd64 by nature,
    # so a missing arm64 there is a fact about the hardware, not a gap.
    process_type_hint: str
    architectures: List[str]
    runs_on_arm: bool
    # False when the registry would not tell us. The platform Harbor is
    # private and refuses anonymous manifest reads, so an unknown image falls
    # back to amd64 — which is the right SCHEDULING default and a lie if
    # rendered as a finding. "We could not look" and "we looked and it is
    # amd64" must not appear identical on screen.
    determined: bool


class BaseImageArchResponse(BaseModel):
    images: List[BaseImageArch]
    arm_ready: int
    # How many the registry would not answer for. A private registry refusing
    # anonymous reads is the common case and is not a fault.
    undetermined: int
    total: int


@router.get("/base-image-architectures", response_model=BaseImageArchResponse)
def base_image_architectures(
    current_user=Depends(require_active_and_onboarded_user),
):
    """What each curated base image can actually run on.

    "Will this run on my Raspberry Pi?" was previously answerable only by
    pulling the image and watching it fail with `exec format error` — and
    because the scheduler now REFUSES work a worker's architecture cannot
    execute, the newer symptom is quieter: an arm64 worker that reports
    healthy and simply never picks anything up. This is where you check
    before buying the hardware.
    """
    if not getattr(current_user, "is_platform_admin", False):
        raise HTTPException(status_code=403, detail="Platform admin only")

    from utils.curated_base_images import CURATED_BASE_IMAGES
    from utils.image_arch import ASSUMED_SINGLE_ARCH, cached_architectures, warm

    # FX.166.p3 — this endpoint must answer from the cache ONLY. It used to
    # call architectures_blocking(), which probes the registry synchronously
    # per image (6s timeout each). With a cold cache that is ~84s for the 14
    # curated images, so the Dashboard's #base-image-arch panel sat on its
    # static "Loading…" placeholder past the render window: the page looked
    # broken even though the module ran. Unknown images are now handed to the
    # background warm loop and this request answers with the honest scheduling
    # default (amd64, "not determined") — which is exactly what the panel
    # already renders for an undetermined image.
    out: List[BaseImageArch] = []
    for img in CURATED_BASE_IMAGES:
        ref = img.image_ref
        # A cache hit is the honest signal that the registry actually told us
        # something; a miss is "not determined", never a guessed answer.
        arches = cached_architectures(ref)
        determined = arches is not None
        if not determined:
            warm(ref)
            arches = list(ASSUMED_SINGLE_ARCH)
        out.append(BaseImageArch(
            display_name=img.display_name,
            image_tag=img.image_tag,
            image_ref=ref,
            category=img.category,
            process_type_hint=img.process_type_hint,
            architectures=arches,
            runs_on_arm="arm64" in arches,
            determined=determined,
        ))
    return BaseImageArchResponse(
        images=out,
        arm_ready=sum(1 for i in out if i.runs_on_arm),
        undetermined=sum(1 for i in out if not i.determined),
        total=len(out),
    )
