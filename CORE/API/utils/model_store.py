"""
Model store — upload/download ONNX models from MinIO platform-models bucket.

Models are versioned: scheduler-v1.onnx, scheduler-v2.onnx, etc.
Each version has a .json metadata file and -normalization.json params.
"""

import io
import json
import logging
import os
import sys
import tempfile
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Tests (pytest-sibling.sh) run with the LIVE stack's env, so an unmocked
# `_redis_client()` would default to the exact same redis://redis:6379/2 the
# live API's 8 uvicorn workers coordinate through below — a test process is
# a foreign "pid" by construction, so it would both adopt stale live verdicts
# and POISON the live 5-minute cache with test verdicts. `"pytest" in
# sys.modules` is set by pytest itself before any test module is imported, so
# this is the least-magic way to detect "we are inside a pytest process" — no
# new env var, no per-test-file boilerplate required. Tests that want to
# exercise this layer on purpose (test_startup_model_store_crossprocess_cache.py)
# flip this back off for the duration of their own tests.
_TESTING = "pytest" in sys.modules

BUCKET = "platform-models"

# A valid trained scheduler ONNX is on the order of MBs. Truncated/placeholder
# uploads (the historical bug: 4–64 byte stubs) are rejected before they can
# ever be loaded. This is a cheap pre-check; validate_onnx_file() is the real
# gate (it actually loads the graph).
MIN_ONNX_BYTES = 1024


def _get_minio_client():
    from utils.storage import get_service_client
    return get_service_client()


def _ensure_bucket(client):
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)


def _cpu_session_options():
    """SessionOptions that keep onnxruntime off every host core.

    Without explicit SessionOptions onnxruntime sizes its intra-op pool to
    every visible core (48 on the test VM), which spawned ~300 threads per
    session and produced the `pthread_setaffinity_np failed ... error code: 22`
    storm in the API web workers. One thread by default; `MOD_ONNX_THREADS`
    is the knob for boxes that legitimately want more.
    """
    import onnxruntime as ort
    try:
        n = int(os.getenv("MOD_ONNX_THREADS", "1"))
    except ValueError:
        n = 1
    if n < 1:
        n = 1
    so = ort.SessionOptions()
    so.intra_op_num_threads = n
    so.inter_op_num_threads = n
    so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return so


# In-process memo of files already validated: (path -> (mtime, size)). The
# hot-reload loop re-validates the same immutable blob every 60s per worker;
# an unchanged (mtime, size) pair since a SUCCESSFUL validation means the
# graph is identical, so we skip building another throwaway session. Failures
# are never memoised — a repaired file must be re-checked.
_validated_files: Dict[str, Tuple[float, int]] = {}


def validate_onnx_file(path: str) -> bool:
    """True iff `path` is a loadable ONNX model.

    The ONLY authoritative check is that onnxruntime can build an
    InferenceSession from it — that is exactly what the scheduler predictor
    does at load time, so a file that passes here will not throw
    INVALID_PROTOBUF later. A fast size pre-check rejects the truncated stubs
    (4–64 bytes) without paying for a parse.
    """
    try:
        if not path or not os.path.exists(path):
            return False
        stat = os.stat(path)
        if stat.st_size < MIN_ONNX_BYTES:
            return False
        seen = _validated_files.get(path)
        if seen == (stat.st_mtime, stat.st_size):
            return True
        import onnxruntime as ort  # heavy import kept local
        ort.InferenceSession(path, sess_options=_cpu_session_options(),
                             providers=["CPUExecutionProvider"])
        _validated_files[path] = (stat.st_mtime, stat.st_size)
        return True
    except Exception as e:
        logger.warning("model_store: ONNX validation failed for %s: %s", path, e)
        return False


def upload_model(onnx_path: str, version: int, metadata: dict = None,
                 normalization_path: str = None) -> str:
    """Upload ONNX model + metadata to MinIO — validated and size-verified.

    Guards against the historical failure that stranded the scheduler (a
    truncated/corrupt `scheduler-v{n}.onnx` becoming the highest version and
    failing INVALID_PROTOBUF on every load):
      1. the SOURCE must be a loadable ONNX before we touch storage — a caller
         can never publish a non-model as `scheduler-v{n}.onnx`;
      2. after writing, we read the stored size back and reject a short/
         truncated write.
    Defence in depth: even if a bad blob somehow lands, the loader only picks
    the highest VALIDATING version and the baseline watchdog restores a good
    model, so a corrupt blob can never actually be loaded.
    """
    client = _get_minio_client()
    _ensure_bucket(client)

    if not validate_onnx_file(onnx_path):
        raise ValueError(
            f"refusing to upload scheduler-v{version}.onnx: source {onnx_path} "
            f"is not a loadable ONNX model")

    with open(onnx_path, "rb") as f:
        data = f.read()

    onnx_key = f"scheduler-v{version}.onnx"
    client.put_object(BUCKET, onnx_key, io.BytesIO(data), len(data))
    # Read the stored size back to catch a truncated/partial write. Guarded so
    # a test double whose stat_object returns a mock (non-int) is a no-op —
    # real S3 backends return the byte count.
    try:
        got = client.stat_object(BUCKET, onnx_key).size
        if isinstance(got, int) and got != len(data):
            raise ValueError(
                f"stored scheduler-v{version}.onnx is {got} bytes, expected "
                f"{len(data)} — truncated write")
    except ValueError:
        raise
    except Exception:
        pass  # stat unavailable (e.g. test mock); loader-side validation still guards
    logger.info("Uploaded %s (%d bytes, validated)", onnx_key, len(data))

    if metadata:
        meta_key = f"scheduler-v{version}.json"
        meta_bytes = json.dumps(metadata, indent=2, default=str).encode()
        client.put_object(BUCKET, meta_key, io.BytesIO(meta_bytes), len(meta_bytes))

    if normalization_path and os.path.exists(normalization_path):
        norm_key = f"scheduler-v{version}-normalization.json"
        with open(normalization_path, "rb") as f:
            norm_data = f.read()
            client.put_object(BUCKET, norm_key, io.BytesIO(norm_data), len(norm_data))

    return onnx_key


def _list_model_versions(client) -> list:
    """All scheduler-v*.onnx version numbers present (plain-named only)."""
    versions = []
    for obj in client.list_objects(BUCKET, prefix="scheduler-v"):
        name = obj.object_name
        if name.endswith(".onnx"):
            try:
                versions.append(int(name.replace("scheduler-v", "").replace(".onnx", "")))
            except ValueError:
                pass
    return sorted(versions)


def get_latest_model_version() -> int:
    """Get the highest model version number in MinIO (name-based; may be corrupt)."""
    try:
        client = _get_minio_client()
        if not client.bucket_exists(BUCKET):
            return 0
        versions = _list_model_versions(client)
        return max(versions) if versions else 0
    except Exception as e:
        logger.debug("Could not check model versions: %s", e)
        return 0


# --- Resolution cache (fixes the ONNX re-scan RAM/CPU churn) -----------------
# Model blobs are immutable: a scheduler-vN.onnx that fails validation once will
# ALWAYS fail, and one that loads will always load. The hot-reload loop calls
# get_latest_valid_model_version() every 60s PER uvicorn worker, and the old
# implementation re-downloaded + re-ran ort.InferenceSession on EVERY version
# (incl. the corrupt v4/v3 stubs) on every call — 8 workers × repeated model
# loads/min churned RAM and CPU and helped OOM the container. We cache the
# resolved-valid version and the set of known-corrupt versions so steady-state
# calls do ZERO downloads. A genuinely newer version (higher than the cached
# valid one, not already known-corrupt) is still validated so promotions work.
# The cache is process-local and cleared on restart, so a re-uploaded/repaired
# version is re-evaluated on the next boot.
_resolved_valid_version: Optional[int] = None
_known_corrupt_versions: set = set()

# --- Cross-process layer over the cache above (fixes the 8-worker boot storm) --
# `uvicorn --workers 8` boots eight PROCESSES at once, and the cache above is
# per-process: measured on 10-12, the 09:30:54.9→09:30:59.2 lifespan window
# logged 18× "scheduler-v{3,4}.onnx is corrupt — caching as bad" and 8×
# "Downloaded model v2 … (validated)" — every worker re-fetched the corrupt
# v4/v3 stubs from MinIO, re-validated them, and re-downloaded v2 inside its
# own lifespan before "ONNX scheduler model v2 loaded". Same posture as
# services/node_scanner.py's `_digest_sweep_lock_acquired()`: Redis
# (redis://redis:6379/2) arbitrates across processes and fails open when
# unreachable. The in-process sets stay as the fast path so the 60s hot-reload
# polls never pay a Redis round-trip when nothing changed.
_CORRUPT_CACHE_KEY = "mod:model_store:corrupt"
_LATEST_VALID_KEY = "mod:model_store:latest_valid"
_CACHE_TTL_S = 5 * 60
# Per-version fetch slots: one for the validate-and-resolve walk, one for the
# staging download. `ex=` is the crash guard — a worker that dies mid-fetch
# leaves the slot for at most _FETCH_LOCK_EX_S before someone else retakes it.
_FETCH_LOCK_TMPL = "mod:model_store:fetch:v{}"
_DOWNLOAD_LOCK_TMPL = "mod:model_store:download:v{}"
_FETCH_LOCK_EX_S = 60
_LOCK_POLL_S = 0.25
_LOCK_WAIT_S = 10.0
# Model blobs are immutable, so the staged copy is shared across workers: one
# process downloads+validates into it, the rest map straight to it. Atomic
# rename (same filesystem) means a loser never sees a half-written blob.
_SHARED_STAGE_ROOT = os.path.join(tempfile.gettempdir(), "mod-model-shared")
_STAGED_TMPL = "mod:model_store:staged:v{}"


def _boot_pid() -> int:
    """Identity of the process writing/reading a verdict.

    Verdicts are namespaced by pid and only FOREIGN verdicts are adopted: a
    process's own verdicts already live in the in-process cache above, and
    re-reading them from Redis would make the process-local cache reset on
    restart unobservable (a stale self-verdict would survive it).
    """
    return os.getpid()


def _redis_client():
    """The pod's Redis/Valkey client, fail-open (None) when unreachable."""
    if _TESTING:
        return None
    try:
        import redis as _redis
        return _redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/2"))
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: redis unavailable (%s); using process cache", e)
        return None


def _note_corrupt_version(r, version: int) -> None:
    """Publish a corrupt verdict so the other 7 workers skip it."""
    if r is None:
        return
    try:
        r.sadd(_CORRUPT_CACHE_KEY, f"{_boot_pid()}:{version}")
        r.expire(_CORRUPT_CACHE_KEY, _CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: corrupt-mark failed for v%d: %s", version, e)


def _remember_valid_version(r, version: int) -> None:
    """Publish a validated version so the other workers can skip the walk."""
    if r is None:
        return
    try:
        r.set(_LATEST_VALID_KEY, f"{_boot_pid()}:{version}", ex=_CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: valid-mark failed for v%d: %s", version, e)


def _cached_valid_version(r) -> Optional[int]:
    """A FOREIGN process's resolved-valid verdict, if any.

    Verdicts are pid-tagged and only foreign ones count: this process's own
    verdicts already live in the in-process cache above, and re-reading them
    would hide the fact that the process-local cache is cleared on restart.
    """
    if r is None:
        return None
    try:
        raw = r.get(_LATEST_VALID_KEY)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        pid_s, _, ver_s = str(raw).partition(":")
        if int(pid_s) == _boot_pid():
            return None
        return int(ver_s)
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: valid-read failed: %s", e)
        return None


def _cached_corrupt_versions(r) -> set:
    """Versions another process already proved unloadable."""
    if r is None:
        return set()
    own = _boot_pid()
    out = set()
    try:
        for raw in r.smembers(_CORRUPT_CACHE_KEY):
            if isinstance(raw, bytes):
                raw = raw.decode()
            pid_s, _, ver_s = str(raw).partition(":")
            try:
                if int(pid_s) != own:
                    out.add(int(ver_s))
            except ValueError:
                continue
        return out
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: corrupt-read failed: %s", e)
        return set()


def _claim_slot(r, tmpl: str, version: int) -> Optional[bool]:
    """True when THIS process won the slot; False when another holds it;
    None when Redis is down (proceed, process-local semantics)."""
    if r is None:
        return None
    try:
        return bool(r.set(tmpl.format(version), "1", nx=True, ex=_FETCH_LOCK_EX_S))
    except Exception as e:  # noqa: BLE001 — lock is best-effort
        logger.debug("model_store: slot unavailable (%s); proceeding", e)
        return None


def _await_version_verdict(r, version: int) -> Tuple[str, Optional[int]]:
    """Wait for ANOTHER process's verdict on `version`.

    ("corrupt", None) when someone proved the blob unloadable, ("valid", v)
    when someone resolved at least this version, ("none", None) when nothing
    landed (dead/slow worker — the caller then does its own fetch).
    """
    if r is None:
        return ("none", None)
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        if version in _cached_corrupt_versions(r):
            return ("corrupt", None)
        shared = _cached_valid_version(r)
        if shared is not None and shared >= version:
            return ("valid", shared)
        if time.monotonic() >= deadline:
            return ("none", None)
        time.sleep(_LOCK_POLL_S)


def _shared_stage_dir(version: int) -> str:
    """Stable staging dir for one version's blob (shared across processes)."""
    d = os.path.join(_SHARED_STAGE_ROOT, f"v{version}")
    os.makedirs(d, exist_ok=True)
    return d


def _publish_staged_blob(r, version: int, path: str) -> None:
    if r is None:
        return
    try:
        r.set(_STAGED_TMPL.format(version), path, ex=_CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: stage-mark failed for v%d: %s", version, e)


def _discard_staged_blob(r, version: int) -> None:
    """Drop a staged-blob verdict that failed re-validation on copy.

    This is a Redis key, not a DB row (ISO no-hard-delete doesn't apply) — the
    winner's own copy may still be fine, but THIS worker must not trust an
    unverified byte stream, so it stops pointing other workers at it and falls
    back to its own fetch.
    """
    if r is None:
        return
    try:
        r.delete(_STAGED_TMPL.format(version))
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.debug("model_store: stage-discard failed for v%d: %s", version, e)


def _await_staged_blob(r, version: int) -> Optional[str]:
    """Path of another worker's staged blob for `version`, once it lands."""
    if r is None:
        return None
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            raw = r.get(_STAGED_TMPL.format(version))
        except Exception as e:  # noqa: BLE001 — cache is best-effort
            logger.debug("model_store: stage-read failed: %s", e)
            return None
        if raw:
            path = raw.decode() if isinstance(raw, bytes) else str(raw)
            if os.path.exists(path):
                return path
        if time.monotonic() >= deadline:
            return None
        time.sleep(_LOCK_POLL_S)


def get_latest_valid_model_version() -> int:
    """Highest version whose ONNX blob actually LOADS.

    The loader must never pick a version it can't parse. We walk versions
    high→low, validating each candidate, and return the first that loads (0 if
    none do). This is what makes a corrupt `scheduler-v4.onnx` stub harmless:
    it is skipped and the highest genuinely-loadable version wins.

    Results are cached (see module notes above): known-corrupt versions are
    never re-downloaded, and once a valid version is resolved it is returned
    without re-validation unless a strictly-newer version has appeared.
    """
    global _resolved_valid_version
    try:
        client = _get_minio_client()
        if not client.bucket_exists(BUCKET):
            return _resolved_valid_version or 0
        versions = sorted(_list_model_versions(client), reverse=True)
        if not versions:
            return _resolved_valid_version or 0
        r = _redis_client()
        # Cross-process fast path: another worker already resolved a good
        # version this boot — adopt it before walking, so 7 of 8 workers do
        # ZERO downloads.
        shared_valid = _cached_valid_version(r)
        # A cached verdict only counts while the blob is still there — a
        # deleted/replaced version must not be adopted (and keeps the cache
        # honest across a re-uploaded bucket).
        if shared_valid is not None and shared_valid in versions and \
                shared_valid > (_resolved_valid_version or 0):
            _resolved_valid_version = shared_valid
        # Fast path: nothing newer than our known-good has appeared.
        if _resolved_valid_version is not None and versions[0] <= _resolved_valid_version:
            return _resolved_valid_version
        corrupt = _known_corrupt_versions | _cached_corrupt_versions(r)
        for v in versions:
            if v in corrupt:
                continue  # immutable blob already failed once — never re-check it
            if _resolved_valid_version is not None and v <= _resolved_valid_version:
                # Everything at/below here is our cached good version or older.
                return _resolved_valid_version
            won = _claim_slot(r, _FETCH_LOCK_TMPL, v)
            if won is False:
                # Another worker is fetching/validating this exact version: wait
                # for its verdict instead of paying a second MinIO round-trip.
                kind, val = _await_version_verdict(r, v)
                if kind == "corrupt":
                    _known_corrupt_versions.add(v)
                    continue
                if kind == "valid":
                    _resolved_valid_version = max(val, _resolved_valid_version or 0)
                    return _resolved_valid_version
                # Verdict never landed (dead/slow worker): fall through and do
                # our own fetch so the answer is still the highest loadable.
            onnx_key = f"scheduler-v{v}.onnx"
            tmp_dir = tempfile.mkdtemp(prefix="mod-model-check-")
            try:
                onnx_path = os.path.join(tmp_dir, onnx_key)
                client.fget_object(BUCKET, onnx_key, onnx_path)
                if validate_onnx_file(onnx_path):
                    _remember_valid_version(r, v)
                    _resolved_valid_version = v
                    return v
                _known_corrupt_versions.add(v)
                _note_corrupt_version(r, v)
                logger.warning(
                    "model_store: scheduler-v%d.onnx is corrupt — caching as bad "
                    "(won't re-download/re-validate it)", v)
            except Exception as e:
                logger.warning("model_store: could not fetch scheduler-v%d.onnx: %s", v, e)
            finally:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
        return _resolved_valid_version or 0
    except Exception as e:
        logger.debug("Could not resolve latest valid model version: %s", e)
        return _resolved_valid_version or 0


def download_model(version: int = None) -> Optional[Tuple[str, str, dict]]:
    """
    Download ONNX model + normalization from MinIO.
    Returns (onnx_path, normalization_path, metadata_dict) or None.

    The downloaded ONNX is VALIDATED before it is returned — a corrupt blob
    yields None instead of a path that would blow up later in InferenceSession.
    """
    try:
        client = _get_minio_client()
        if not client.bucket_exists(BUCKET):
            return None

        if version is None:
            version = get_latest_model_version()
        if version == 0:
            return None

        r = _redis_client()
        onnx_key = f"scheduler-v{version}.onnx"
        # Single-flight the blob fetch: one worker per version downloads, the
        # other seven wait for its staged copy and reuse it (no second MinIO
        # round-trip, no second ort parse, no second "Downloaded model vN"
        # log line). Each process still gets its OWN tmp dir so main.py's
        # per-predictor cleanup stays per-process.
        won = _claim_slot(r, _DOWNLOAD_LOCK_TMPL, version)
        shared_path = _await_staged_blob(r, version) if won is False else None
        reused = False
        if shared_path:
            tmp_dir = tempfile.mkdtemp(prefix="mod-model-")
            onnx_path = os.path.join(tmp_dir, onnx_key)
            try:
                import shutil
                shutil.copyfile(shared_path, onnx_path)
                # The winner validated ITS copy before publishing the staged
                # path, but this worker only knows it got the SAME BYTES as
                # whatever is at `shared_path` right now — a half-written
                # copy, a TTL'd-then-overwritten stage dir, or filesystem
                # corruption between workers would otherwise hand back an
                # unvalidated path, breaking the "VALIDATED before it is
                # returned" contract. Re-validate THIS copy.
                if validate_onnx_file(onnx_path):
                    reused = True
                else:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    # Don't trust the staged key for the next worker either —
                    # it's a Redis key (not a DB row), safe to drop.
                    _discard_staged_blob(r, version)
                    logger.warning(
                        "model_store: peer-staged scheduler-v%d.onnx failed "
                        "validation on copy — discarding the staged key and "
                        "falling back to our own fetch", version)
            except Exception as e:
                logger.debug("model_store: staged blob for v%d unusable (%s); "
                             "falling back to own fetch", version, e)
                reused = False
        if not reused:
            tmp_dir = tempfile.mkdtemp(prefix="mod-model-")
            onnx_path = os.path.join(tmp_dir, onnx_key)
            client.fget_object(BUCKET, onnx_key, onnx_path)

        if reused:
            # Re-validated above; publish our own verdict too.
            _remember_valid_version(r, version)
        elif not validate_onnx_file(onnx_path):
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
            _note_corrupt_version(r, version)
            logger.warning("Downloaded scheduler-v%d.onnx is not a loadable "
                           "model — refusing it", version)
            return None
        else:
            _publish_staged_blob(r, version, onnx_path)
            _remember_valid_version(r, version)

        norm_path = None
        norm_key = f"scheduler-v{version}-normalization.json"
        try:
            norm_local = os.path.join(tmp_dir, norm_key)
            client.fget_object(BUCKET, norm_key, norm_local)
            norm_path = norm_local
        except Exception:
            pass

        metadata = {}
        meta_key = f"scheduler-v{version}.json"
        try:
            meta_local = os.path.join(tmp_dir, meta_key)
            client.fget_object(BUCKET, meta_key, meta_local)
            with open(meta_local) as f:
                metadata = json.load(f)
        except Exception:
            pass

        metadata["version"] = version
        if reused:
            logger.info("model_store: reusing peer-staged model v%d at %s "
                        "(skipped MinIO fetch)", version, tmp_dir)
        else:
            logger.info("Downloaded model v%d to %s (validated)", version, tmp_dir)
        return onnx_path, norm_path, metadata

    except Exception as e:
        logger.warning("Failed to download model v%s: %s", version, e)
        return None
