"""Which CPU architectures an image can actually run on.

Gate 11 step 2. The claim guard added in `cf250b03` refuses a task whose
`image_arches` does not include the worker's architecture, and treats an ABSENT
list as amd64-only — which is true of every image built before the CPU tier
went multi-arch. That fails closed, which is the right default and also means
an arm64 worker gets nothing until something declares otherwise.

This is what declares otherwise.

WHY A CACHE AND A BACKGROUND FILL, rather than probing at dispatch: the answer
comes from an HTTP call to a registry, and the place it is needed is a
SQLAlchemy insert hook. Holding a database transaction open across a network
round-trip — to a host that may be down — is how a slow registry becomes a
stalled queue. So the hook reads memory only. A miss stamps nothing, the task
stays amd64-only, and a background thread fills the cache so the NEXT task
carries the answer. Being one dispatch late is harmless; the wrong answer, or a
blocked insert, is not.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# image ref -> (expires_at, architectures)
_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_CACHE_TTL_S = 3600.0          # an image's platforms change only on a re-push
_INFLIGHT: set = set()
_LOCK = threading.Lock()

# What an image with no manifest list offers. A single-manifest image is
# whatever it was built as, and everything in this deployment was built amd64
# until the CPU tier went multi-arch.
ASSUMED_SINGLE_ARCH = ["amd64"]

# Docker Hub reference hosts. `docker.io` is the name used in image refs; the
# registry API actually lives at registry-1.docker.io.
_HUB = ("docker.io", "registry-1.docker.io", "index.docker.io")


def cached_architectures(image_ref: str) -> Optional[List[str]]:
    """Architectures for this image if already known, else None.

    Never does I/O. Safe to call from an insert hook.
    """
    if not image_ref:
        return None
    with _LOCK:
        hit = _CACHE.get(image_ref)
        if hit and hit[0] > time.time():
            return list(hit[1])
    return None


# Manifest LIST types must be requested explicitly. Ask for only the v2
# manifest type — as the preflight checker does — and a registry is entitled to
# resolve the list for you and hand back a single platform's manifest, which
# would make every multi-arch image look single-arch.
_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


def _registry_host_matches(stored: str, host: str) -> bool:
    """Does the platform harbor_registry row name the registry being asked about?

    FIX-B119D-L2 (SCOPE-B119D-RESOLVE-1108 H2): the sweep asks about the
    `ce_mirror_ref` host; the harbor row names the push host. Same catalogue,
    so the mirror host counts as the stored registry's host.
    """
    if stored == host:
        return True
    from utils.registries import _ce_image_registry
    ce = _ce_image_registry()
    return bool(ce) and host == ce.split("/", 1)[0]


def _registry_credentials(host: str):
    """(username, password) for a registry, from the platform secret store.

    Only the PLATFORM registry is looked up. It is the one whose images the
    scheduler must reason about, and reaching for per-tenant credentials from a
    background probe would cross a boundary this has no business crossing.

    FIX-B119D-L2: the three ways this used to return (None, None) silently —
    no harbor row, a host mismatch, and a swallowed Vault/DB read failure —
    each log their own reason at WARNING. The digest backfill sweep skipped
    all 182 rows on ONE indistinguishable line ("no pull credentials ...
    skipping"), which is why the gate had to be made readable. Never log a
    secret value or the exception text, only the exception class name.
    """
    try:
        from database import SessionLocal
        import crud
        db = SessionLocal()
        try:
            reg = (crud.get_decrypted_secret_by_name(db, "harbor_registry", 1) or "")
            reg = reg.split("://")[-1].strip("/")
            if reg and not _registry_host_matches(reg, host):
                logger.warning(
                    "registry credential lookup: host mismatch for %s "
                    "(platform harbor_registry names %s)",
                    host, reg,
                )
                return None, None
            user = crud.get_decrypted_secret_by_name(db, "harbor_username", 1) or None
            password = crud.get_decrypted_secret_by_name(db, "harbor_password", 1) or None
            if not user or not password:
                logger.warning(
                    "registry credential lookup: no harbor username/password row "
                    "for tenant 1 (host %s)", host,
                )
            return user, password
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "registry credential lookup failed for %s: read failure %s",
            host, type(e).__name__,
        )
        return None, None


def _bearer_for(parsed, image_ref: str, headers: dict) -> bool:
    """Attach a bearer token for a PRIVATE registry. True if one was added.

    A private registry refuses anonymous manifest reads outright — which is
    every platform image on Harbor. Without this, the probe returns None for
    all of them, `image_arches` is never stamped, and an arm64 worker is
    refused work whose image genuinely supports arm64. Demonstrated rather than
    assumed: minio-client:latest was pushed with amd64+arm64 in its manifest
    and still probed as unknown until this existed, making the push inert.

    Follows the WWW-Authenticate challenge the registry returns rather than
    assuming a token endpoint, because Harbor's realm is per-deployment.
    """
    import requests
    host = parsed.registry_host
    if host in _HUB:
        return False
    user, password = _registry_credentials(host)
    if not user or not password:
        return False
    try:
        chal = requests.get(f"https://{host}/v2/", timeout=6.0)
        auth = chal.headers.get("WWW-Authenticate", "")
        if not auth.lower().startswith("bearer"):
            return False
        parts = dict(kv.split("=", 1) for kv in
                     auth[len("Bearer"):].strip().split(",") if "=" in kv)
        realm = parts.get("realm", "").strip('"')
        service = parts.get("service", "").strip('"')
        if not realm:
            return False
        r = requests.get(realm, timeout=8.0, auth=(user, password),
                         params={"service": service,
                                 "scope": f"repository:{parsed.namespace}/{parsed.repo}:pull"})
        token = (r.json() or {}).get("token") if r.status_code == 200 else None
        if not token:
            return False
        headers["Authorization"] = f"Bearer {token}"
        return True
    except Exception:  # noqa: BLE001
        logger.debug("bearer auth failed for %s", image_ref, exc_info=True)
        return False


def _probe(image_ref: str) -> Optional[List[str]]:
    """Ask the registry what platforms this image offers."""
    try:
        import requests
        from utils.image_ref_preflight import (manifest_list_architectures,
                                               parse_image_ref)
    except Exception:  # noqa: BLE001
        return None

    parsed = parse_image_ref(image_ref)
    if parsed is None:
        return None
    # `docker.io` is the reference namespace, not the API endpoint — the
    # registry API lives at registry-1.docker.io. Probing the former returns
    # nothing and looks identical to an image with no manifest list.
    api_host = ("registry-1.docker.io" if parsed.registry_host in _HUB
                else parsed.registry_host)
    url = (f"https://{api_host}/v2/"
           f"{parsed.namespace}/{parsed.repo}/manifests/{parsed.tag}")
    headers = {"Accept": _ACCEPT}

    # Docker Hub refuses anonymous manifest reads without a pull token, so an
    # unauthenticated probe there returns 401 and this function would report
    # "unknown" for every public base image — silently, and looking exactly
    # like a single-arch image. Reuse the token helper the preflight checker
    # already has rather than writing a second one.
    if _bearer_for(parsed, image_ref, headers):
        pass
    elif parsed.registry_host in _HUB:
        try:
            from utils.image_ref_preflight import _docker_hub_anon_token
            token = _docker_hub_anon_token(parsed, timeout=6.0)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:  # noqa: BLE001
            logger.debug("hub token lookup failed for %s", image_ref, exc_info=True)

    try:
        r = requests.get(url, timeout=6.0, headers=headers)
    except Exception as e:  # noqa: BLE001
        logger.debug("image arch probe failed for %s: %s", image_ref, e)
        return None
    if r.status_code != 200:
        # 401 on a private registry is ordinary here. Anonymous probing is
        # enough for the public bases; anything else stays unknown, which
        # leaves the task amd64-only rather than guessing it wider.
        logger.debug("image arch probe: %s returned %s", image_ref, r.status_code)
        return None
    try:
        manifest = r.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(manifest, dict):
        return None
    arches = manifest_list_architectures(manifest)
    if arches:
        return arches
    # A single manifest (not a list) carries its platform in the config blob.
    # Without reading it, an arm64-only image (the GX10 vLLM mirror) probed as
    # amd64 and its stack could never land on the only box that can run it.
    # Anything unreadable stays amd64, keeping the fail-closed behaviour.
    arch = _single_manifest_arch(api_host, parsed, manifest, headers)
    return [arch] if arch else list(ASSUMED_SINGLE_ARCH)


def _single_manifest_arch(api_host: str, parsed, manifest: dict,
                          headers: dict) -> Optional[str]:
    import requests
    digest = (manifest.get("config") or {}).get("digest")
    if not isinstance(digest, str) or not digest:
        return None
    url = f"https://{api_host}/v2/{parsed.namespace}/{parsed.repo}/blobs/{digest}"
    blob_headers = {k: v for k, v in headers.items() if k != "Accept"}
    try:
        r = requests.get(url, timeout=8.0, headers=blob_headers)
        if r.status_code != 200:
            return None
        arch = (r.json() or {}).get("architecture")
    except Exception:  # noqa: BLE001
        logger.debug("image config probe failed for %s", digest, exc_info=True)
        return None
    if not isinstance(arch, str) or not arch:
        return None
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)


def warm(image_ref: str) -> None:
    """Fill the cache for this image, in the background, at most once at a time."""
    if not image_ref:
        return
    with _LOCK:
        if image_ref in _INFLIGHT:
            return
        hit = _CACHE.get(image_ref)
        if hit and hit[0] > time.time():
            return
        _INFLIGHT.add(image_ref)

    def _run():
        try:
            arches = _probe(image_ref)
            if arches:
                with _LOCK:
                    _CACHE[image_ref] = (time.time() + _CACHE_TTL_S, arches)
                logger.info("image arches: %s -> %s", image_ref, arches)
        finally:
            with _LOCK:
                _INFLIGHT.discard(image_ref)

    threading.Thread(target=_run, name="image-arch-probe", daemon=True).start()


def architectures_blocking(image_ref: str,
                           default: Optional[List[str]] = None) -> List[str]:
    """Cached architectures, probing synchronously if unknown.

    For callers that are NOT inside a transaction and genuinely need an answer
    now — an API endpoint rendering what a node can run on, for example.
    """
    known = cached_architectures(image_ref)
    if known:
        return known
    arches = _probe(image_ref)
    if arches:
        with _LOCK:
            _CACHE[image_ref] = (time.time() + _CACHE_TTL_S, arches)
        return arches
    return list(default if default is not None else ASSUMED_SINGLE_ARCH)


def image_supports_arch(image_ref: Optional[str], worker_arch: str) -> bool:
    """Can `image_ref` run on a worker of `worker_arch`? (cache-only)

    Mirrors the claim guard's arch rule exactly, so a caller can answer the
    question BEFORE dispatching instead of letting a pinned task sit unclaimed
    until the profiling deadline force-fails it:

      - no image → runs anywhere (True);
      - cached arch list → membership test;
      - UNKNOWN image (nothing in the cache) → the claim guard's fail-closed
        default of amd64-only (i.e. True for amd64, False for anything else).

    Reads the cache ONLY and never probes the registry: this is called from the
    profiling dispatch, which runs on the heartbeat path with a database
    transaction open. Holding that open across a network round-trip to a
    possibly-down registry is exactly what this module's docstring warns against
    — an unknown image is left to the fail-closed default rather than guessed
    wider, which is safe because the fleet is amd64-built and the claim guard
    already refuses unknown images on non-amd64 workers.
    """
    if not image_ref:
        return True
    known = cached_architectures(image_ref)
    if not known:
        return (worker_arch or "") == "amd64"
    return (worker_arch or "") in {str(a).strip().lower() for a in known}


# Every architecture a worker reports today (Go's GOARCH). What a task that runs
# no image of its own is stamped with, so the guard never withholds it.
ANY_ARCH = ["amd64", "arm64"]


def _compose_images(compose_yaml: str) -> Optional[List[str]]:
    """Every service image in a compose spec; None if any is not a plain ref.

    A build-only service or an interpolated `${VAR}` ref can't be resolved
    here, so the spec as a whole stays unknown (fail-closed amd64).
    """
    import yaml
    try:
        doc = yaml.safe_load(compose_yaml)
    except yaml.YAMLError:
        return None
    services = doc.get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict) or not services:
        return None
    images = []
    for svc in services.values():
        image = svc.get("image") if isinstance(svc, dict) else None
        if not isinstance(image, str) or not image or "$" in image:
            return None
        images.append(image)
    return images


def task_arches(payload: dict) -> Optional[List[str]]:
    """What a task can run on, from the cache only; None while unknown.

    A node task runs its `processing_image`. A persistent-stack task runs every
    image in its `compose_spec`, so it can only go where ALL of them can — a
    stack whose model server is multi-arch but whose weights init container is
    amd64-only must not land on arm64 (GX10 Strata, 2026-10-03). A stack op
    that carries no compose (stop/start/teardown/logs) runs no image at all:
    the worker drives compose on its own host, so it is claimable by any arch —
    otherwise an arm64 host could never stop the stack it is running.
    Cold images are warmed so a later stamp (insert or claim-path re-stamp)
    can answer.
    """
    image = payload.get("processing_image")
    if isinstance(image, str) and image:
        images = [image]
    else:
        compose = payload.get("compose_spec")
        if isinstance(compose, str) and compose.strip():
            images = _compose_images(compose)
            if not images:
                return None
        elif payload.get("stack_op"):
            return list(ANY_ARCH)
        else:
            return None
    cold = [i for i in images if not cached_architectures(i)]
    if cold:
        for i in cold:
            warm(i)
        return None
    common: Optional[List[str]] = None
    for i in images:
        arches = cached_architectures(i) or []
        common = arches if common is None else [a for a in common if a in arches]
    return common or None


def stamp_task_arches(mapper, connection, target) -> None:
    """SQLAlchemy before_insert hook: declare what a task's image can run on.

    Registered against ApiTaskQueue rather than patched into all 31 places that
    create one. Those sites will keep multiplying, and a rule enforced in one
    place holds for code nobody has written yet.

    Reads the cache ONLY — never the network. See the module docstring: this
    runs inside the insert transaction.

    Absent stays absent on a miss, which the claim guard reads as amd64-only.
    That is the fail-closed default and is correct: an unknown image is one
    nobody has confirmed runs anywhere else.
    """
    try:
        payload = getattr(target, "task_payload", None)
        if not isinstance(payload, dict):
            return
        if payload.get("image_arches"):
            return                      # explicitly set by the caller — respect it
        known = task_arches(payload)    # a miss warms, so the next task carries it
        if known:
            # Mutate in place AND reassign: JSONB columns are change-tracked by
            # identity, so an in-place edit alone can be missed on flush.
            payload = dict(payload)
            payload["image_arches"] = known
            target.task_payload = payload
    except Exception:  # noqa: BLE001
        # Never let arch annotation break task creation. Worst case the task
        # goes out amd64-only, which is what happened before this existed.
        logger.debug("image arch stamp skipped", exc_info=True)


def restamp_unstamped_tasks(db, worker_name: str, worker_arch: str,
                            limit: int = 50) -> int:
    """Late-stamp claimable tasks whose image arches were unknown at insert.

    `stamp_task_arches` reads the cache only, so a task inserted while its
    image was cold goes out with no `image_arches` and the claim guard reads
    that as amd64-only for the task's whole life. "One dispatch late" is not
    harmless for a task pinned to a non-amd64 worker: nothing else will ever
    claim it (GX10 profiling, 2026-10-03, sat until the deadline). So the claim
    path of a non-amd64 worker re-stamps such tasks from the now-warm cache
    before selecting. Cache-only (no network inside the claim transaction);
    a still-cold image is warmed so the next poll can stamp it.
    """
    if not worker_arch or worker_arch == "amd64":
        return 0
    from sqlalchemy import text
    rows = db.execute(text("""
        SELECT id, task_payload->>'processing_image' AS image,
               task_payload->>'compose_spec' AS compose,
               task_payload->>'stack_op' AS stack_op
        FROM api_task_queue
        WHERE status IN ('PENDING', 'ASSIGNED')
          AND claimed_by_worker IS NULL
          AND (assigned_to_worker = :worker OR assigned_to_worker IS NULL)
          AND task_payload->'image_arches' IS NULL
          AND (coalesce(task_payload->>'processing_image', '') <> ''
               OR coalesce(task_payload->>'stack_op', '') <> '')
        LIMIT :limit
    """), {"worker": worker_name, "limit": limit}).fetchall()
    stamped = 0
    for task_id, image, compose, stack_op in rows:
        known = task_arches({"processing_image": image, "compose_spec": compose,
                             "stack_op": stack_op})
        if not known:
            continue
        stamped += db.execute(text("""
            UPDATE api_task_queue
            SET task_payload = jsonb_set(task_payload, '{image_arches}',
                                         CAST(:arches AS jsonb))
            WHERE id = :id AND claimed_by_worker IS NULL
              AND task_payload->'image_arches' IS NULL
        """), {"id": task_id, "arches": json.dumps(known)}).rowcount
    if stamped:
        logger.info("late-stamped image arches on %d task(s) for %s worker %s",
                    stamped, worker_arch, worker_name)
    return stamped
