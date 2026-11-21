# CORE/utils/minio_client.py
"""Compatibility shim for the P6 module rename ``minio_client`` -> ``s3_client``.

The canonical module is ``utils/s3_client.py``. This shim re-exports EVERY public
name by making this module the SAME module object as ``utils.s3_client``
(``sys.modules[__name__] = _s3_client``), so every existing import form and
patch target keeps working unchanged:

- ``from utils.minio_client import MinioClient`` / ``import utils.minio_client``
- ``patch("utils.minio_client.MinioClient")`` / ``patch("utils.minio_client.Minio")``
  (and every other attribute) — patches resolve against the real module, so
  call-time imports inside the module see the patched attribute exactly as
  before (verified by probe).
- ``utils.minio_client.get_tenant_bucket_name`` and the private helpers
  (``_bound_client``, ``_build_http_pool``, ``S3_CONNECT_TIMEOUT_KEY``, ...)
  all resolve to the single implementation in ``utils/s3_client.py``.

No code lives here; edit ``utils/s3_client.py``.
"""
from utils import s3_client as _s3_client
import sys

sys.modules[__name__] = _s3_client
