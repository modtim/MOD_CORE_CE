"""2400GX-c1 (launch-completion-megaplan Group X) — external seed routes.

A thin HTTP layer over `services/stack_seed.py`. Tenant admin of the stack
(the router-wide STACK_AUTHOR capability gate + `_get_owned` from `_shared`,
the same guard restore/rehearse use — `_shared.py:2128`); the tenant is
pinned from the verified user, never from the body (S1). Keys are server-minted
under `{project}/{seed_id}/`; a client never supplies a key/bucket/URL.

CE: this path never calls `require_paid` — a logical part is ALLOWED to
register on CE (owner Q-C1 pending with Tim); restoring a logical snapshot
stays wherever it is today (the paid engine, `_shared.py:5292`).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

from ._shared import (
    Session,
    models,
    get_db,
    get_current_user,
)

router = APIRouter()


# --- request/response models -------------------------------------------------

from pydantic import BaseModel  # noqa: E402


class SeedRegister(BaseModel):
    logical: Optional[dict] = None
    volumes: Optional[dict] = None


class SeedPartWindow(BaseModel):
    part: str
    from_part: int
    count: int


class SeedCommit(BaseModel):
    """`parts` maps every declared seed label to ONE of two shapes:

    - a list `[{"part_number": int, "etag": str}, ...]` — the chunks the
      client PUT (the CLI path; unchanged); or
    - the literal string `"server"` — the server lists the chunks of the
      upload IT minted with S3 ListParts, so a browser never has to read the
      PUT's `ETag` response header (which needs
      `Access-Control-Expose-Headers: ETag`, unavailable on versitygw v1.7.0
      and unsettable on BYO-S3 tenants).

    The shape is validated in `services/stack_seed.py:commit_seed` (422
    `seed_invalid` for any other string, 422 `seed_part_unknown` for a part
    with no / gap / duplicate / over-count chunks), NOT with a looser pydantic
    type here — `dict` stays backward compatible with the existing tests.
    Mixing shapes in one body is allowed."""
    parts: dict


# --- routes (declared in this order: static segments first) ------------------

@router.post("/{stack_id}/seed", status_code=202)
def register_stack_seed(
        stack_id: int,
        body: SeedRegister,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """POST /v1/stacks/{id}/seed — register a PENDING external seed: validate
    the declared parts (≤16, caps, `x-mod.backup: logical` service / declared
    volume, format whitelist), mint one multipart upload slot per part
    server-side, and record `storage_refs["seeds"][seed_id]`. 202 with the
    upload slots — never a presigned URL (those come from the parts route)."""
    from services import stack_seed as _sd
    return _sd.register_seed(db, stack_id, user,
                             {"logical": body.logical, "volumes": body.volumes})


@router.post("/{stack_id}/seed/{seed_id}/parts")
def seed_part_urls(
        stack_id: int,
        seed_id: str,
        body: SeedPartWindow,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """POST /v1/stacks/{id}/seed/{seed}/parts — presigned `upload_part` PUT
    URLs for a window (from_part..+count, count ≤100) of one part's chunks,
    TTL `stack.seed.url_ttl_seconds`. The host comes from
    `utils.storage.get_presigned_client` (the external-endpoint client the
    file-download presigned URLs already use) — never hand-built. The ONLY
    place presigned URLs ever appear (S3)."""
    from services import stack_seed as _sd
    return _sd.part_urls(db, stack_id, user, seed_id, body.part,
                         body.from_part, body.count)


@router.post("/{stack_id}/seed/{seed_id}/commit")
def commit_stack_seed(
        stack_id: int,
        seed_id: str,
        body: SeedCommit,
        db: Session = Depends(get_db),
        user: models.User = Depends(get_current_user)):
    """POST /v1/stacks/{id}/seed/{seed}/commit — complete each multipart
    upload server-side, HEAD + sniff every object (PGDMP / 1f8b magic, size
    ≤ declared ≤ caps), then write a NORMAL snapshot record into
    `storage_refs["snapshots"]` plus `source:"external"` / `posture` /
    `seed_id` / `verified_at:null` so restore + rehearse find it unchanged.
    A second commit → 409 `seed_already_committed`."""
    from services import stack_seed as _sd
    return _sd.commit_seed(db, stack_id, user, seed_id, {"parts": body.parts})
