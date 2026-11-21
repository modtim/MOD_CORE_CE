"""Universal mode dispatcher (wizard-rebuild §4 chunk 3).

Single entry point that turns a `/build-and-register-node` request
into a BuildPlan via the right per-mode extractor:

  wrap_base_image only        → Image    extractors/image.py
  source_repo_url             → Git      extractors/git.py
  inline_source_body +        → Dockerfile  extractors/dockerfile.py
    filename == Dockerfile
  inline_source_body          → Script   extractors/script.py
  (describe-codegen lands     → (out of scope until §8)
   alongside §8 cascade)

Returns a `DispatchResult` carrying:
  - the extracted plan
  - the mode string (for metrics + logging)
  - the harbor image name (derived from node_name slug for non-
    image modes; user-supplied image_name override or slug for
    image mode)
  - the repo_docs_extract row id when Git mode used the cache
    (None otherwise) — feeds BuildSpec.repo_docs_extract_id

All extractors are pure functions modulo network (Git mode hits
the §3c repo-docs cache; Image mode hits Docker Hub for digest
pinning + Config.User). The dispatcher itself is pure routing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from .types import BuildPlan

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DispatchResult:
    plan: BuildPlan
    mode: str            # "image" | "git" | "dockerfile" | "script"
    image_name: str
    repo_docs_extract_id: Optional[int] = None


def _slug(text: str) -> str:
    """Mirror of routers/node_definitions._node_image_slug. Duplicated
    here so the dispatcher has zero router-import dependency."""
    s = (text or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "node"


def dispatch_extraction(
    request: dict,
    *,
    db: Session,
    tenant_id: int,
    auth_token: Optional[str] = None,
) -> DispatchResult:
    """Route a wizard request to the right BuildPlan extractor.

    Args:
      request: the `/build-and-register-node` request body. Must
        carry `node_name` plus one of `wrap_base_image` /
        `source_repo_url` / `inline_source_body`.
      db / tenant_id: needed by the Git extractor for §3c cache lookup.
      auth_token: forwarded to repo_fetcher when Git mode fetches
        a private repo.

    Returns:
      DispatchResult.

    Raises:
      ValueError when no recognizable input shape is present (caller
      should surface 400 to the wizard).
    """
    node_name = (request.get("node_name") or "").strip()
    input_type = request.get("input_type") or "image"
    output_type = request.get("output_type") or "image"
    container_env = request.get("container_env")
    wrap_base_image = (request.get("wrap_base_image") or "").strip() or None
    source_repo_url = (request.get("source_repo_url") or "").strip() or None
    inline_source_body = request.get("inline_source_body") or ""
    inline_source_filename = (
        request.get("inline_source_filename") or ""
    ).strip().lower()

    derived_image_name = (
        (request.get("image_name") or "").strip()
        or _slug(node_name)
    )

    # ---- Git mode --------------------------------------------------
    # Highest priority because a repo URL implies the user wants the
    # docs-driven pipeline regardless of any other field they may
    # have also typed.
    if source_repo_url:
        from .extractors.git import extract_git_plan
        plan = extract_git_plan(
            db=db,
            tenant_id=tenant_id,
            repo_url=source_repo_url,
            node_name=node_name,
            input_type=input_type,
            output_type=output_type,
            container_env=container_env,
            auth_token=auth_token,
            wrap_base_image=wrap_base_image,
        )
        # The Git extractor uses the cache row internally; expose its
        # id so BuildSpec.repo_docs_extract_id can persist the link.
        # We re-query the store here rather than threading the id back
        # through extract_git_plan — keeps the extractor signature
        # narrow.
        repo_docs_extract_id = _lookup_repo_docs_extract_id(
            db, tenant_id=tenant_id, repo_url=source_repo_url, ref="",
        )
        return DispatchResult(
            plan=plan, mode="git",
            image_name=derived_image_name,
            repo_docs_extract_id=repo_docs_extract_id,
        )

    # ---- Dockerfile mode -------------------------------------------
    # Distinct from Script mode by filename hint. The wizard sets
    # inline_source_filename="Dockerfile" when the user chose the
    # Dockerfile source type; that signal beats body-sniffing.
    if inline_source_body and inline_source_filename == "dockerfile":
        from .extractors.dockerfile import (
            detect_unbundled_copy_sources,
            extract_dockerfile_plan,
        )
        # Reject submissions whose Dockerfile references local files
        # the wizard doesn't bundle. Saves the user a 7-attempt repair
        # thrash on `npm error code ENOENT package.json` (manual-test
        # repro 2026-05-22 spec=38 Node.js App).
        flagged = detect_unbundled_copy_sources(inline_source_body)
        if flagged:
            raise ValueError(
                f"Dockerfile contains COPY directives that reference "
                f"local files not bundled by the wizard: {flagged[:5]!r}. "
                f"Dockerfile mode ships ONLY the Dockerfile body — the "
                f"build context has no project sources alongside. Either "
                f"pick a base image that already has these files baked "
                f"in, or use Script mode where the wizard embeds your "
                f"script body directly."
            )
        plan = extract_dockerfile_plan(
            dockerfile_body=inline_source_body,
            node_name=node_name,
            input_type=input_type,
            output_type=output_type,
            container_env=container_env,
        )
        return DispatchResult(
            plan=plan, mode="dockerfile",
            image_name=derived_image_name,
        )

    # ---- Script mode -----------------------------------------------
    if inline_source_body:
        from .extractors.script import extract_script_plan
        plan = extract_script_plan(
            script_body=inline_source_body,
            node_name=node_name,
            input_type=input_type,
            output_type=output_type,
            wrap_base_image=wrap_base_image,
            container_env=container_env,
            system_packages=request.get("system_packages"),
        )
        return DispatchResult(
            plan=plan, mode="script",
            image_name=derived_image_name,
        )

    # ---- Image mode ------------------------------------------------
    if wrap_base_image:
        from .extractors.image import extract_image_plan
        plan = extract_image_plan(
            image_ref=wrap_base_image,
            node_name=node_name,
            input_type=input_type,
            output_type=output_type,
            image_name=derived_image_name,
            container_env=container_env,
            pin_digest=True,
        )
        return DispatchResult(
            plan=plan, mode="image",
            image_name=derived_image_name,
        )

    raise ValueError(
        "no extractable input shape in request — need one of "
        "wrap_base_image / source_repo_url / inline_source_body"
    )


def _lookup_repo_docs_extract_id(
    db: Session, *, tenant_id: int, repo_url: str, ref: str,
) -> Optional[int]:
    """Find the cache row id the Git extractor's get_or_extract
    just hit (or created). Same key as
    `store.get_or_extract`'s lookup. Returns None on any error
    (the persistence link is best-effort — Git mode works without
    it)."""
    try:
        from node_builder.build_plan.types import EXTRACTOR_VERSION
        from sqlalchemy import select
        import models
        version = EXTRACTOR_VERSION
        stmt = select(models.RepoDocsExtract.id).where(
            models.RepoDocsExtract.tenant_id == tenant_id,
            models.RepoDocsExtract.repo_url == repo_url,
            models.RepoDocsExtract.ref == ref,
            models.RepoDocsExtract.extractor_version == version,
        )
        return db.execute(stmt).scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "dispatcher: repo_docs_extract_id lookup failed: %s", exc,
        )
        return None
