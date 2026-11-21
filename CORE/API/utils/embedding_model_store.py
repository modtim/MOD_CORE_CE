"""Load sentence-transformers embedding models from MinIO, not HuggingFace.

MOD self-hosts its embedding models in the `platform-models` bucket
(`embeddings/<name>/…`). Models are resolved **MinIO-first**; HuggingFace is a
last-resort SEED used only when the model is in neither the local image nor
MinIO — and after a seed the model is mirrored to MinIO so it never needs HF
again. This keeps HF out of the build/deploy critical path (build hosts get
rate-limited; MinIO doesn't).
"""
import logging
import os

logger = logging.getLogger(__name__)

BUCKET = "platform-models"
PREFIX = "embeddings"  # platform-models/embeddings/<name>/<files>

# model-selection-by-role §4 — the store holds more than embeddings now.
#
# The type is DECLARED at upload, not detected. Detection is guesswork over
# archive contents, and getting it wrong means an embedding model offered where
# a language model is wanted — which fails on a worker at serve time, far from
# the upload that caused it. Declaration is one field and is right by
# construction; detection is better used later as a CHECK on the declaration.
MODEL_TYPES = ("embedding", "llm")

# `embedding` keeps the original prefix, so nothing already stored moves. The
# existing objects ARE that type — the store has only ever held embeddings.
_TYPE_PREFIX = {"embedding": PREFIX, "llm": "llm"}
DEFAULT_MODEL_TYPE = "embedding"


def prefix_for(mtype: str | None) -> str:
    """Storage prefix for a model type. Unknown types are refused rather than
    quietly filed under embeddings, where they would be offered to the wrong
    consumer."""
    mt = (mtype or DEFAULT_MODEL_TYPE).strip().lower()
    if mt not in _TYPE_PREFIX:
        raise ValueError(f"unknown model type {mtype!r}; expected one of {MODEL_TYPES}")
    return _TYPE_PREFIX[mt]


def type_of_prefix(prefix: str) -> str:
    for mt, pfx in _TYPE_PREFIX.items():
        if pfx == prefix:
            return mt
    return DEFAULT_MODEL_TYPE


def _client():
    from utils.storage import get_service_client
    return get_service_client()


_WEIGHT_FILES = (
    "model.safetensors", "pytorch_model.bin", "tf_model.h5",
    "model.ckpt.index", "flax_model.msgpack",
)


def _has_model(local_dir: str) -> bool:
    # A COMPLETE sentence-transformers model dir has config.json AND real weight
    # files. Checking only config.json let a weight-less dir (or a mirror that
    # carried just configs + junk `.tmp` partials) masquerade as present, so
    # embeddings then failed at load time with "no model.safetensors found".
    if not (os.path.isdir(local_dir) and os.path.exists(os.path.join(local_dir, "config.json"))):
        return False
    for _root, _dirs, files in os.walk(local_dir):
        if any(w in files for w in _WEIGHT_FILES):
            return True
    return False


def _download_from_minio(name: str, local_dir: str, mtype: str = DEFAULT_MODEL_TYPE) -> bool:
    try:
        client = _client()
        if not client.bucket_exists(BUCKET):
            return False
        prefix = f"{prefix_for(mtype)}/{name}/"
        objs = list(client.list_objects(BUCKET, prefix=prefix, recursive=True))
        if not objs:
            return False
        for o in objs:
            rel = o.object_name[len(prefix):]
            if not rel:
                continue
            # Never pull partial-download artifacts (a broken earlier fetch left
            # `.tmp*` files that got mirrored; skip so they can't re-corrupt the
            # local dir or satisfy _has_model with junk).
            if os.path.basename(rel).startswith("."):
                continue
            dst = os.path.join(local_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            client.fget_object(BUCKET, o.object_name, dst)
        if _has_model(local_dir):
            logger.info("embedding model '%s' loaded from MinIO (%s)", name, BUCKET)
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("MinIO fetch of embedding model '%s' failed: %s", name, e)
        return False


def _minio_has(name: str, mtype: str = DEFAULT_MODEL_TYPE) -> bool:
    try:
        client = _client()
        if not client.bucket_exists(BUCKET):
            return False
        # "Present" means a real WEIGHT object exists — not merely any object.
        # A mirror holding only config.json + junk `.tmp` partials must read as
        # ABSENT so seed_from_hf re-fetches a complete model from HuggingFace.
        prefix = f"{prefix_for(mtype)}/{name}/"
        for o in client.list_objects(BUCKET, prefix=prefix, recursive=True):
            if os.path.basename(o.object_name) in _WEIGHT_FILES:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _mirror_to_minio(name: str, local_dir: str, mtype: str = DEFAULT_MODEL_TYPE) -> None:
    try:
        client = _client()
        if not client.bucket_exists(BUCKET):
            client.make_bucket(BUCKET)
        prefix = f"{prefix_for(mtype)}/{name}/"
        for root, _dirs, files in os.walk(local_dir):
            for f in files:
                # Never mirror partial-download temp files (`.tmp*`, `*.part`) or
                # other hidden artifacts — mirroring them is what corrupted the
                # store before (they masquerade as the model in _minio_has).
                if f.startswith(".") or f.endswith(".part"):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, local_dir).replace(os.sep, "/")
                client.fput_object(BUCKET, prefix + rel, full)
        logger.info("mirrored embedding model '%s' to MinIO for next time", name)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not mirror embedding model '%s' to MinIO: %s", name, e)


def list_models(mtype: str | None = None) -> list:
    """Models in MinIO: [{name, type, files, bytes}].

    §4.5 — the type travels with every row. Listing one type must never return
    another: an embedding model offered where a language model is wanted fails
    on a worker at serve time, long after the mistake was made.

    `mtype=None` lists everything, which is what an admin view wants.
    """
    try:
        client = _client()
        if not client.bucket_exists(BUCKET):
            return []
        wanted = [mtype] if mtype else list(MODEL_TYPES)
        agg: dict = {}
        for mt in wanted:
            pfx = prefix_for(mt)
            for o in client.list_objects(BUCKET, prefix=f"{pfx}/", recursive=True):
                rest = o.object_name[len(pfx) + 1:]
                name = rest.split("/", 1)[0]
                if not name:
                    continue
                a = agg.setdefault((mt, name),
                                   {"name": name, "type": mt, "files": 0, "bytes": 0})
                a["files"] += 1
                a["bytes"] += int(o.size or 0)
        return sorted(agg.values(), key=lambda x: (x["type"], x["name"]))
    except Exception:  # noqa: BLE001
        return []


def mirror_dir(name: str, local_dir: str, mtype: str = DEFAULT_MODEL_TYPE) -> None:
    """Public: upload a validated model dir to MinIO under this name and type."""
    _mirror_to_minio(name, local_dir, mtype)


def has_config(local_dir: str) -> bool:
    return _has_model(local_dir)


# ---------------------------------------------------------------------------
# Upload gates — read-only inspection of an extracted model dir. No model load,
# no network. Used by routers/embedding_models.py to reject junk before it ever
# reaches MinIO and to detect the embedding dimension (which decides vector-store
# compatibility — see the dimension-migration notes).
# ---------------------------------------------------------------------------

MANIFEST = "mod_manifest.json"  # our sidecar written into the model dir

_WEIGHT_FILES = (
    "model.safetensors", "pytorch_model.bin", "tf_model.h5",
    "model.ckpt.index", "flax_model.msgpack", "rust_model.ot",
)
_WEIGHT_INDEX = ("model.safetensors.index.json", "pytorch_model.bin.index.json")


def _read_json(path: str):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def has_weights(local_dir: str) -> bool:
    """True if the dir carries model weights (single-file, sharded, or a
    known framework format). Guards against a config-only 'model'."""
    for f in _WEIGHT_FILES + _WEIGHT_INDEX:
        if os.path.exists(os.path.join(local_dir, f)):
            return True
    # Sharded safetensors without an index file (rare) — any *.safetensors.
    try:
        return any(f.endswith(".safetensors") for f in os.listdir(local_dir))
    except OSError:
        return False


def is_sentence_transformer(local_dir: str) -> bool:
    """A real sentence-transformers model advertises its module pipeline via
    modules.json (or the ST config). Plain HF encoders lack it but still embed
    once wrapped — so this is a *quality* signal, not a hard gate."""
    return os.path.exists(os.path.join(local_dir, "modules.json")) or \
        os.path.exists(os.path.join(local_dir, "config_sentence_transformers.json"))


def detect_dimension(local_dir: str):
    """Best-effort embedding dimension from config files (no model load).

    Order of truth: ST pooling config → ST config → base transformer config.
    Returns int or None. The pooling dim is the actual output width for ST
    models; hidden_size is the transformer width (usually equal)."""
    # 1) sentence-transformers pooling module records the real output width.
    for pooldir in ("1_Pooling", "2_Pooling"):
        j = _read_json(os.path.join(local_dir, pooldir, "config.json"))
        if isinstance(j, dict) and isinstance(j.get("word_embedding_dimension"), int):
            return j["word_embedding_dimension"]
    # 2) ST top-level config sometimes carries it.
    j = _read_json(os.path.join(local_dir, "config_sentence_transformers.json"))
    if isinstance(j, dict):
        for k in ("word_embedding_dimension", "hidden_size", "dim"):
            if isinstance(j.get(k), int):
                return j[k]
    # 3) base transformer config.
    j = _read_json(os.path.join(local_dir, "config.json"))
    if isinstance(j, dict):
        for k in ("hidden_size", "d_model", "dim", "hidden_dim", "n_embd"):
            if isinstance(j.get(k), int):
                return j[k]
    return None


def has_gguf(local_dir: str) -> bool:
    """A GGUF weight file anywhere in the directory."""
    for root, _dirs, files in os.walk(local_dir):
        if any(f.lower().endswith(".gguf") for f in files):
            return True
    return False


def validate_for_type(local_dir: str, mtype: str) -> tuple:
    """§4.2 — does this upload meet the expectations of its DECLARED type?

    Returns (ok, detail). Validation cannot be shared, and that is the concrete
    reason a type had to be declared at all: the embedding check requires
    `config.json` plus safetensors/pytorch weights, and a GGUF language model
    carries neither. Run one check over both and you either reject valid
    language models or accept archives that cannot be served and only fail
    later, on a worker, at deploy time.
    """
    mt = (mtype or DEFAULT_MODEL_TYPE).strip().lower()
    if mt == "embedding":
        if not has_config(local_dir):
            return False, ("expected a sentence-transformers directory with "
                           "config.json at its root; none found")
        if not has_weights(local_dir):
            return False, ("config.json is present but no model weights "
                           "(model.safetensors / pytorch_model.bin) are")
        return True, "sentence-transformers directory"
    if mt == "llm":
        if has_gguf(local_dir):
            return True, "GGUF weights"
        # A safetensors directory is servable by a vLLM rung, so it is accepted
        # as a language model too — the shapes differ, the type does not.
        if has_config(local_dir) and has_weights(local_dir):
            return True, "safetensors/pytorch weights with config.json"
        return False, ("expected either a .gguf weight file or a model "
                       "directory with config.json and weights; found neither")
    return False, f"unknown model type {mtype!r}"


def inspect_model_dir(local_dir: str) -> dict:
    """One-shot gate report for an extracted model dir."""
    return {
        "has_config": has_config(local_dir),
        "has_weights": has_weights(local_dir),
        "is_sentence_transformer": is_sentence_transformer(local_dir),
        "dimension": detect_dimension(local_dir),
    }


def write_manifest(local_dir: str, data: dict) -> None:
    import json
    try:
        with open(os.path.join(local_dir, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not write model manifest: %s", e)


def read_manifest(name: str, mtype: str = DEFAULT_MODEL_TYPE) -> dict:
    """Read our sidecar manifest for a stored model from MinIO (dim, flags)."""
    try:
        client = _client()
        if not client.bucket_exists(BUCKET):
            return {}
        obj = client.get_object(BUCKET, f"{prefix_for(mtype)}/{name}/{MANIFEST}")
        import json
        return json.loads(obj.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def model_exists(name: str, mtype: str = DEFAULT_MODEL_TYPE) -> bool:
    """True if a model already occupies this name in MinIO (collision guard)."""
    return _minio_has(name)


def _heal_manifest_dim(name: str, dim: int) -> None:
    """Persist a detected dimension into a stored model's MinIO manifest — for
    models seeded outside the upload endpoint (which had no manifest)."""
    try:
        import io
        import json
        m = read_manifest(name) or {}
        m["name"] = name
        m["dimension"] = int(dim)
        data = json.dumps(m).encode("utf-8")
        _client().put_object(BUCKET, f"{prefix_for(mtype)}/{name}/{MANIFEST}",
                             io.BytesIO(data), len(data), content_type="application/json")
    except Exception as e:  # noqa: BLE001
        logger.debug("manifest dim self-heal failed for %s: %s", name, e)


def dimension_of(name: str):
    """Best-effort embedding dimension for a MinIO-stored model. Manifest first;
    if it lacks a dimension (e.g. the model was seeded by the HF-fallback path,
    not the upload gate), detect it from the model's config JSONs in MinIO and
    self-heal the manifest so the list shows a real width, not '—'."""
    d = (read_manifest(name) or {}).get("dimension")
    if isinstance(d, int):
        return d
    try:
        import json
        client = _client()
        if not client.bucket_exists(BUCKET):
            return None

        def _j(rel):
            try:
                return json.loads(client.get_object(BUCKET, f"{PREFIX}/{name}/{rel}").read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return None

        # Same order of truth as detect_dimension(), but reading from MinIO.
        for pooldir in ("1_Pooling", "2_Pooling"):
            j = _j(f"{pooldir}/config.json")
            if isinstance(j, dict) and isinstance(j.get("word_embedding_dimension"), int):
                _heal_manifest_dim(name, j["word_embedding_dimension"])
                return j["word_embedding_dimension"]
        j = _j("config_sentence_transformers.json")
        if isinstance(j, dict):
            for k in ("word_embedding_dimension", "hidden_size", "dim"):
                if isinstance(j.get(k), int):
                    _heal_manifest_dim(name, j[k])
                    return j[k]
        j = _j("config.json")
        if isinstance(j, dict):
            for k in ("hidden_size", "d_model", "dim", "hidden_dim", "n_embd"):
                if isinstance(j.get(k), int):
                    _heal_manifest_dim(name, j[k])
                    return j[k]
    except Exception:  # noqa: BLE001
        return None
    return None


# The embedding models the platform seeds into MinIO on deploy so that RUNTIME
# never needs HuggingFace. Kept here (not Vault) because a fresh server has no
# pins yet — these must exist for both the unpinned defaults (all-MiniLM / arctic)
# and the standard pinned model (mxbai). All are plain encoders (no remote code).
# name -> HuggingFace id.
STANDARD_EMBEDDING_MODELS = {
    "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
    "snowflake-arctic-embed-l": "Snowflake/snowflake-arctic-embed-l",
    "mxbai-embed-large-v1": "mixedbread-ai/mxbai-embed-large-v1",
}


# Only what SentenceTransformer(local_dir) needs. Excludes the multi-GB onnx/ and
# openvino/ variants these repos also ship (fnmatch: "*.json" etc. never match
# .onnx/.bin/.xml weights).
_SNAPSHOT_ALLOW_PATTERNS = [
    "*.json", "*.txt", "*.safetensors", "1_Pooling/*", "2_Normalize/*",
    "sentencepiece.bpe.model", "tokenizer*", "vocab*", "merges.txt", "modules.json",
]


def _snapshot_to_dir(hf_id: str, local_dir: str) -> None:
    """File-level download of the model repo into ``local_dir``. Deliberately NOT
    ``SentenceTransformer(hf_id).save()``: that materialises fp32 weights in RAM and
    OOM-killed the 2 GiB CE api on first boot (2400CR2l)."""
    from huggingface_hub import snapshot_download
    snapshot_download(repo_id=hf_id, local_dir=local_dir,
                      allow_patterns=_SNAPSHOT_ALLOW_PATTERNS)


def _no_hf_requested() -> bool:
    """True when MOD_EMBED_NO_HF is on. Read EXACTLY the way ``ensure_model``
    reads it (:490) so the runtime refusal and the deploy-time seed agree — one
    flag, one meaning, no second knob."""
    return os.getenv("MOD_EMBED_NO_HF", "").strip().lower() in ("1", "true", "yes")


def seed_from_hf(name: str, hf_id: str, local_dir: str | None = None) -> bool:
    """Download a model from HuggingFace and mirror it to MinIO. This is the
    INTENTIONAL deploy-time seed — paid/dev servers still self-populate their
    MinIO this way. Idempotent: skips if already in MinIO, and a copy already
    local (baked into the image) is mirrored with ZERO HF contact.

    FAILS CLOSED when MOD_EMBED_NO_HF is on and the model is neither in MinIO
    nor local: no HuggingFace fetch is attempted, one warning names the flag,
    and the caller reports it (never "seeded"). Best-effort — returns False
    (logged) if HF is unreachable/rate-limited, so a later boot can retry."""
    local_dir = local_dir or f"/app/models/embeddings/{name}"
    try:
        if _minio_has(name):
            return True
        if _has_model(local_dir):
            # Baked/previously fetched: seed MinIO from it, no network at all.
            _mirror_to_minio(name, local_dir)
            return _minio_has(name)
        if _no_hf_requested():
            logger.warning(
                "seed_from_hf('%s') SKIPPED — MOD_EMBED_NO_HF is on and the model is "
                "neither in MinIO nor baked into the image, so no HuggingFace fetch is "
                "attempted (bake it in CORE/API/Dockerfile or upload it via Settings).",
                name,
            )
            return False
        os.makedirs(local_dir, exist_ok=True)
        logger.info("seeding embedding model '%s' from HuggingFace (%s)…", name, hf_id)
        _snapshot_to_dir(hf_id, local_dir)
        _mirror_to_minio(name, local_dir)
        return _minio_has(name)
    except Exception as e:  # noqa: BLE001
        logger.warning("seed_from_hf('%s') failed (will retry next boot): %s", name, e)
        return False


def seed_platform_embedding_models() -> dict:
    """Ensure the platform's standard embedding models are in MinIO.

    Per model, in order: already in MinIO -> ``present``; baked into the image
    (``MOD_EMBED_MODEL_DIR_BASE``/name) -> mirror it to MinIO with ZERO HF
    contact and report ``baked``; MOD_EMBED_NO_HF on and nothing local ->
    ``no-hf`` (the CE cut: first boot fetches NOTHING, the platform default is
    the baked copy); otherwise download from HuggingFace -> ``seeded|failed``.
    Idempotent + best-effort; called at API startup so every paid/dev server
    self-populates its own MinIO on deploy. Returns {name: present|baked|no-hf|seeded|failed}.
    """
    base = os.getenv("MOD_EMBED_MODEL_DIR_BASE", "/app/models/embeddings")
    out = {}
    for name, hf in STANDARD_EMBEDDING_MODELS.items():
        if _minio_has(name):
            out[name] = "present"
            continue
        local_dir = f"{base}/{name}"
        if _has_model(local_dir):
            _mirror_to_minio(name, local_dir)
            out[name] = "baked"
            continue
        if _no_hf_requested():
            out[name] = "no-hf"
            continue
        out[name] = "seeded" if seed_from_hf(name, hf, local_dir) else "failed"
    return out


def ensure_model(hf_id: str, local_dir: str, name: str, allow_hf: bool = True) -> str:
    """Return a local path to the model dir, sourced MinIO-first, HF-last.

    1. already local (baked/previously fetched) → use it,
    2. else pull the dir from MinIO,
    3. else (last resort) download from HuggingFace ONCE and mirror to MinIO.

    ``allow_hf=False`` forbids step 3: a missing model raises IMMEDIATELY instead
    of doing a blocking ``SentenceTransformer(hf_id).save()``. RUNTIME embed paths
    (RAG/KB query + ingest) pass this so a model that is missing — or a
    HuggingFace that is unreachable/rate-limited — can NEVER pin the caller's DB
    transaction for the full ~40s HF retry window. That blocking-fetch-while-
    holding-a-connection was a live pool-exhaustion / "API down" cause. The
    deploy-time seed (seed_from_hf / seed_platform_embedding_models) is a separate
    path and still uses HuggingFace as intended.
    """
    if _has_model(local_dir):
        # Local/baked already. Opportunistically seed MinIO from it (once) so the
        # NEXT build — even with HF down — pulls from MinIO instead of HuggingFace.
        if not _minio_has(name):
            _mirror_to_minio(name, local_dir)
        return local_dir
    os.makedirs(local_dir, exist_ok=True)
    if _download_from_minio(name, local_dir):
        return local_dir
    # Self-host guarantee: in prod, refuse to reach out to HuggingFace. Set
    # MOD_EMBED_NO_HF=1 once every model the platform pins is seeded to MinIO —
    # then a missing model fails loudly instead of silently depending on HF.
    # allow_hf=False is the per-call equivalent for runtime hot paths.
    # GSEC FX.19 R-042 — the platform DB switch is the admin-facing
    # equivalent: sealed mode / egress.model_download_enabled False forbids
    # the remote fetch and the error below names the setting.
    from utils import egress_policy
    egress_off = egress_policy.refuse_reason(
        egress_policy.CATEGORY_MODEL_DOWNLOAD) is not None
    if (not allow_hf or egress_off
            or os.getenv("MOD_EMBED_NO_HF", "").strip().lower() in ("1", "true", "yes")):
        raise RuntimeError(
            f"Embedding model '{name}' is not in the local image or MinIO, and "
            f"runtime HuggingFace fetch is disabled — refusing to block on a "
            f"HuggingFace download. Upload it via Settings → AI Configuration → "
            f"Models, or let the startup seed populate it."
        )
    logger.warning(
        "embedding model '%s' not local and not in MinIO — seeding from HuggingFace "
        "(one-time; requires HF reachable). It will be mirrored to MinIO afterwards.",
        name,
    )
    _snapshot_to_dir(hf_id, local_dir)
    _mirror_to_minio(name, local_dir)
    return local_dir
