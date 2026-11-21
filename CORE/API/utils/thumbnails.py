# CORE/API/utils/thumbnails.py
"""
Precomputed raster-image PREVIEW thumbnails.
============================================

The WorkflowBuilder file picker used to preview every image by fetching the
FULL-resolution object through ``GET /cogs/{id}/download`` and painting it into
an ``<img>``. For a folder of many/large images that re-downloads full-res on
every render — the slowness the user reported.

This module produces a small (max 256px) WebP thumbnail for raster images and
stores it once in the SAME tenant bucket (``thumbnails/<hash>_<px>.webp``). The
existing ``GET /cogs/{id}/thumbnail`` endpoint then serves a presigned URL to
that small object (fast, ~a few KB) instead of the full image.

Generation is:
- **idempotent** — keyed by content hash; if the thumbnail object already
  exists (or the cog already records a ``thumbnail_path``) it is reused.
- **cheap once** — the full image is downloaded/decoded exactly once, ever.
- **tenant-scoped** — the thumbnail lives in the tenant's own bucket, so there
  is no cross-tenant leakage.
- **best-effort** — any failure returns ``None`` and callers fall back to the
  full image; a thumbnail failure must never break upload or preview.

Two entry points:
- ``generate_thumbnail_bytes`` — pure (bytes in → WebP bytes out), unit-testable.
- ``ensure_raster_thumbnail_for_blob`` — resolves storage, generates+stores, and
  records ``processing_metadata.thumbnail_path`` on the cog. Used both at upload
  time (services/file_processing.py) and lazily on first request (routers/cogs.py).
"""

import io
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Feature flag. Default ON — the user explicitly accepted the extra drive space.
# Flip to false to stop generating new thumbnails (already-stored ones still serve).
PRECOMPUTE_IMAGE_THUMBNAILS = os.getenv(
    "PRECOMPUTE_IMAGE_THUMBNAILS", "true").strip().lower() == "true"

# Raster extensions Pillow decodes reliably and for which we emit a browser-
# paintable WebP thumbnail. Two groups:
#   - browser-native (png/jpg/jpeg/gif/bmp/webp): the picker can also paint the
#     full object, so the thumbnail is purely a size/speed win.
#   - browser-UNpaintable but Pillow-decodable (tif/tiff/tga): the browser can
#     NOT paint the raw object, so the WebP thumbnail is the ONLY preview these
#     get — without it the picker can only show a placeholder. Producing it here
#     (instead of the worker) keeps these on the cheap in-process raster path.
# SVG (vector, already tiny) and avif (optional Pillow plugin) stay excluded, and
# EXR/HDR are intentionally NOT here — those need tone-mapping and go through the
# hdr-converter worker task (see routers/cogs._dispatch_hdr_thumbnail_task).
RASTER_THUMBNAIL_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "tga"}

# Longest edge of the generated thumbnail, in pixels.
THUMB_MAX_PX = int(os.getenv("IMAGE_THUMBNAIL_MAX_PX", "256") or "256")

# Thumbnails share the folder the worker-generated / custom thumbnails already use
# so there is one place to reason about (and clean up) derived preview assets.
_THUMB_PREFIX = "thumbnails"


def is_raster_thumbnailable(ext: Optional[str]) -> bool:
    """True when ``ext`` is a raster image we precompute a thumbnail for."""
    return (ext or "").lower().lstrip(".") in RASTER_THUMBNAIL_EXTS


def thumb_object_key(content_hash: str, max_px: int = THUMB_MAX_PX) -> str:
    """Deterministic object key for a content hash's raster thumbnail.

    Content-addressed: the same image content (identical hash) always maps to the
    same key, so dedup'd uploads share one thumbnail and generation is idempotent.
    """
    return f"{_THUMB_PREFIX}/{content_hash}_{max_px}.webp"


def generate_thumbnail_bytes(image_bytes: bytes, max_px: int = THUMB_MAX_PX) -> Optional[bytes]:
    """Decode ``image_bytes`` and return a max ``max_px`` WebP thumbnail as bytes.

    Returns ``None`` (never raises) when Pillow is unavailable or the bytes are
    not a decodable image — the caller then falls back to the full image.
    """
    try:
        from PIL import Image, ImageOps, ImageFile
        # Some real-world uploads are slightly truncated; still produce a preview.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
    except Exception as e:  # pragma: no cover - Pillow is present in the image
        logger.warning("Pillow unavailable; cannot generate thumbnail: %s", e)
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as im:
            # Honour EXIF orientation so rotated phone photos preview upright.
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            # Animated GIF/WebP → first frame.
            if getattr(im, "is_animated", False):
                try:
                    im.seek(0)
                except Exception:
                    pass
            # Normalise mode for WebP. Preserve alpha where present.
            if im.mode in ("RGBA", "LA", "P", "PA"):
                im = im.convert("RGBA")
            elif im.mode != "RGB":
                im = im.convert("RGB")

            im.thumbnail((max_px, max_px), Image.LANCZOS)

            out = io.BytesIO()
            im.save(out, format="WEBP", quality=80, method=4)
            return out.getvalue()
    except Exception as e:
        logger.info("Thumbnail generation failed (%s); falling back to full image", e)
        return None


def _record_thumbnail_path(db, cog, key: str, source: str = "raster-auto") -> None:
    """Persist ``thumbnail_path`` on the cog's processing_metadata (idempotent)."""
    from sqlalchemy.orm.attributes import flag_modified
    meta = dict(cog.processing_metadata or {})
    meta["thumbnail_path"] = key
    meta["thumbnail_source"] = source
    cog.processing_metadata = meta
    flag_modified(cog, "processing_metadata")
    db.commit()


def ensure_raster_thumbnail_for_blob(
    db,
    *,
    cog_id: int,
    content_hash: str,
    tenant_id: int,
    ext: str,
    minio_client=None,
    max_px: int = THUMB_MAX_PX,
) -> Optional[str]:
    """Ensure a raster thumbnail exists for ``content_hash`` and is recorded on
    the cog. Returns the thumbnail object key, or ``None`` when not applicable /
    on any failure (caller falls back to the full image).

    Idempotent at three levels: the cog already records a thumbnail_path; the
    thumbnail object already exists in storage; or the content is dedup'd (same
    hash → same key). ``minio_client`` may be passed (request-scoped, tenant
    bucket) or is constructed here for background/upload callers.
    """
    if not PRECOMPUTE_IMAGE_THUMBNAILS:
        return None
    if not content_hash or not is_raster_thumbnailable(ext):
        return None

    import models
    cog = db.query(models.Cog).filter(
        models.Cog.id == cog_id,
        models.Cog.tenant_id == tenant_id,
    ).first()
    if cog is None:
        return None

    existing = (cog.processing_metadata or {}).get("thumbnail_path")
    if existing:
        return existing

    owns_client = False
    try:
        if minio_client is None:
            from utils.minio_client import MinioClient
            minio_client = MinioClient(tenant_id=tenant_id)
            owns_client = True

        bucket = minio_client.bucket_name
        key = thumb_object_key(content_hash, max_px)

        # Already in storage (prior generation, restart, or content dedup) → adopt it.
        try:
            minio_client.client.stat_object(bucket, key)
            _record_thumbnail_path(db, cog, key)
            return key
        except Exception:
            pass  # not present → generate below

        # Download the source blob once. The object name is the pure content hash.
        try:
            resp = minio_client.client.get_object(bucket, content_hash)
            try:
                data = resp.read()
            finally:
                resp.close()
                resp.release_conn()
        except Exception as e:
            logger.info("thumbnail: source blob unreadable (cog=%s hash=%s): %s",
                        cog_id, content_hash[:12], e)
            return None

        thumb = generate_thumbnail_bytes(data, max_px)
        if not thumb:
            return None

        try:
            minio_client.client.put_object(
                bucket, key, io.BytesIO(thumb), length=len(thumb),
                content_type="image/webp",
            )
        except Exception as e:
            logger.warning("thumbnail: upload failed (cog=%s key=%s): %s", cog_id, key, e)
            return None

        _record_thumbnail_path(db, cog, key)
        logger.info("Precomputed raster thumbnail cog=%s key=%s (%d bytes, src=%d)",
                    cog_id, key, len(thumb), len(data))
        return key
    except Exception as e:  # noqa: BLE001 — never fail the caller on a preview
        logger.warning("thumbnail: ensure failed for cog=%s: %s", cog_id, e)
        return None
    finally:
        # MinioClient holds pooled connections; nothing to close explicitly, but
        # keep the hook in case construction acquires resources later.
        _ = owns_client
