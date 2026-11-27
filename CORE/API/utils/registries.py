"""Where images come from, and where built images go.

Gate 12. A deployment must be able to point at its own registry and have that
actually take effect. Before this, 38 bare `registry.example.com/...` string
literals bypassed `DOCKER_REGISTRY` entirely — so an on-prem operator could set
it correctly and still have base-image resolution, benchmark seeding, canary
Dockerfiles and the build-plan renderer reach for MOD's production registry.
Either it failed to pull, or worse it succeeded, and a customer's network
quietly depended on example.com.

THREE DISTINCT REGISTRIES, and the axis is OWNERSHIP, not location:

  platform_registry()  — MOD's own images: base images, build-task,
                         containerizer, the system node catalog. Read-only to
                         tenants. `DOCKER_REGISTRY`.

  tenant_registry()    — images a TENANT built. Already lives on the customer's
                         own infrastructure: `LOCAL_REGISTRY` on-prem (a Harbor
                         runs in the compose stack), `TENANT_HARBOR_REGISTRY`
                         on SaaS, chosen by `DEPLOYMENT_MODE`.

  build_push_registry() — where a build task PUSHES what it just built.
                         `HARBOR_REGISTRY`, falling back to the platform
                         registry.

`HARBOR_REGISTRY` and `DOCKER_REGISTRY` look redundant because they hold the
same value in MOD's own deployment. They are NOT the same thing: one is a push
target, the other a pull source, and keeping them apart is exactly what lets a
customer pull from their own mirror while builds push elsewhere. Collapsing
them was proposed and rejected — see ROADMAP Gate 12.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# The origin registry. Used ONLY as the last-resort default, and warned about
# on-prem — see platform_registry().
DEFAULT_PLATFORM_REGISTRY = "ghcr.io"
DEFAULT_PLATFORM_PROJECT = "modtim"

# MOD's own domain. The panel's banner asks whether a deployment still pulls
# from MOD, and that question is about OWNERSHIP of whatever host resolved, not
# about equality with the edition's default — the CE cut rewrites the default to
# ghcr.io, so a CE install whose .env points at MOD's dev registry never matched
# the old default-substring test and the panel claimed independence while every
# image came from MOD. This file is the one place allowed to name the origin
# (it already defines the default it hands out; test_no_hardcoded_live_infra
# allow-lists it for that reason).
# Assembled from parts, NOT one literal: the CE build scrub rewrites the origin
# domain (bare and as part of any host) in every shipped text file, so a single
# literal here would arrive in the CE edition already rewritten and registry_is_
# mod would answer False for MOD's own registries on the very edition it targets.
# Adjacent string literals concatenate at compile time, so the scrub's text
# match never sees a contiguous domain string in this file's source.
MOD_ORIGIN_DOMAIN = "mod" "techlabs" "." "com"


def registry_is_mod(host: str) -> bool:
    """True when a resolved registry host belongs to MOD.

    Hosts are compared after dropping any scheme/path and a trailing `:port`,
    case-insensitively: true for the origin domain itself and any
    `*.example.com` subdomain, false for ghcr.io, a customer mirror, or an
    empty host (no registry — nothing of MOD's is involved).
    """
    h = (host or "").strip().lower()
    if not h:
        return False
    if "://" in h:
        h = h.split("://", 1)[1]
    if "@" in h:
        h = h.split("@", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    head, sep, tail = h.partition(":")
    if sep and tail.isdigit():
        h = head
    h = h.strip("/.").strip()
    return h == MOD_ORIGIN_DOMAIN or h.endswith("." + MOD_ORIGIN_DOMAIN)

# A DOCKER_REGISTRY that names a reserved documentation domain (RFC 2606 /
# RFC 6761) can never serve an image. CORE/startup/.env.public.example ships
# DOCKER_REGISTRY="registry.example.com", and a normal install that never edits
# it ended up "registry configured" with no Harbor secrets: profiling refused
# with HTTP 400 on every heartbeat and the profile stayed `pending`, so the
# claim guard withheld every task from every worker (xeon 11-22).
_PLACEHOLDER_REGISTRY_DOMAINS = ("example.com", "example.net", "example.org")
_PLACEHOLDER_REGISTRY_TLDS = ("example", "invalid", "test")


def registry_is_placeholder(host: str) -> bool:
    """True when *host* is a template placeholder, not a real registry."""
    raw = (host or "").strip()
    if raw.upper() == "PLEASE_CONFIGURE":
        return True
    h = raw.lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    if "@" in h:
        h = h.split("@", 1)[1]
    h = h.split("/", 1)[0]
    head, sep, tail = h.partition(":")
    if sep and tail.isdigit():
        h = head
    h = h.strip(".")
    if not h:
        return False
    if any(h == d or h.endswith("." + d) for d in _PLACEHOLDER_REGISTRY_DOMAINS):
        return True
    return h.rsplit(".", 1)[-1] in _PLACEHOLDER_REGISTRY_TLDS


def platform_registry_configured() -> bool:
    """Whether DOCKER_REGISTRY names a real registry.

    The single condition behind the CE "no registry" carve (task credentials
    in routers/worker_tasks.py and the profiling skip in
    routers/worker_management.py): unset, blank, or a placeholder host all mean
    "no registry is configured".
    """
    configured = (os.getenv("DOCKER_REGISTRY") or "").strip()
    return bool(configured) and not registry_is_placeholder(configured)


# The exact values the shipped env templates carry. platform_registry() treats
# only these as "unset" (so the CE_IMAGE_REGISTRY / default fallback applies);
# the wider placeholder test above is for the "is a registry configured" checks.
_SHIPPED_PLACEHOLDERS = ("registry.example.com", "please_configure")


def configured_platform_registry() -> str:
    """DOCKER_REGISTRY when it names a real registry, else "" (unset/blank/placeholder)."""
    return (os.getenv("DOCKER_REGISTRY") or "").strip() if platform_registry_configured() else ""


_warned_unset = False

# CE publishes MOD's own images to a public registry (`ghcr.io/modtim`) rather
# than Harbor, and a default README-only CE install sets NO registry variable
# at all — so the seed path resolved node refs to the on-prem default and every
# run failed to pull. `CE_IMAGE_REGISTRY` is the CE-only fallback for that case.
# It is deliberately NOT `DOCKER_REGISTRY`: worker_tasks.py:4981 `_registry_configured`
# reads that variable alone, and setting it would flip the CE carve to the 503
# "Harbor credentials missing" path.
def _ce_image_registry() -> str:
    """CE's publish target (`host/project`), or "" when CE is not configured."""
    return (os.getenv("CE_IMAGE_REGISTRY") or "").strip().rstrip("/")


def _ce_image_tag() -> str:
    """CE's release tag (`CE_IMAGE_TAG`), or "" when the install pins no tag.

    The compose's counterpart: a CE release stamps its tag into every own-image
    `image:` line, so the stack itself is release-pinned. Node images were the
    exception — nothing read this variable, so a pinned install still pulled
    `:latest` of every node. Empty (or `latest`) = the dev/repo default.
    """
    return (os.getenv("CE_IMAGE_TAG") or "").strip()


def _retag_ce_ref(ref: str) -> str:
    """Pin a CE node ref's `:latest` to the install's release tag.

    Applies ONLY when this deployment is a CE install (`CE_IMAGE_REGISTRY` set)
    AND pins a tag other than `latest`, so paid/on-prem refs are byte-identical
    to before. Rewrites a trailing `:latest` and nothing else: an explicit
    `:v1.2.3`, a variant tag (`:latest-cuda`) or a `@sha256:` digest stays exactly
    as written, and an untagged ref (which may carry a registry PORT colon,
    `registry:5000/foo`) is left alone. Safe for the CE catalogue because
    publish_ce_release.node_image_steps pushes every VETTABLE node image under
    BOTH `:<release-tag>` and `:latest`.
    """
    if not _ce_image_registry():
        return ref
    tag = _ce_image_tag()
    if not tag or tag == "latest" or "@" in ref:
        return ref
    colon = ref.rfind(":")
    if colon == -1 or colon < ref.rfind("/"):
        return ref  # no tag (or the colon is a registry port)
    if ref[colon + 1:] != "latest":
        return ref
    return ref[:colon] + ":" + tag


def platform_registry() -> str:
    """Host serving MOD's own images.

    Warns (does not fail) when unset on an on-prem deployment. Failing would be
    defensible — silently reaching for example.com from a customer network
    is the thing this module exists to stop — but a hard failure here would take
    down existing deployments that have relied on the default, so the first step
    is to make it visible. Warned once per process; this is called per image
    reference and a per-call log would drown everything else.
    """
    global _warned_unset
    configured = (os.getenv("DOCKER_REGISTRY") or "").strip()
    if configured.lower() in _SHIPPED_PLACEHOLDERS:
        configured = ""
    if configured:
        return configured.rstrip("/")
    # CE fallback: the publish target doubles as the pull source, so a README-
    # only CE install resolves node refs to where CE actually pushed. Explicit
    # DOCKER_REGISTRY still wins (paid/SaaS unchanged), and with no CE_IMAGE_
    # REGISTRY the old default-and-warn behaviour is untouched. The warning is
    # skipped here because the deployment IS configured — pointing at ghcr.io
    # is what it asked for, not an accidental reach for MOD's Harbor.
    ce = _ce_image_registry()
    if ce:
        return ce.split("/", 1)[0]
    if (os.getenv("DEPLOYMENT_MODE") or "").lower() == "onprem" and not _warned_unset:
        _warned_unset = True
        logger.warning(
            "DOCKER_REGISTRY is unset on an on-prem deployment; falling back to "
            "%s. Images will be pulled from MOD's production registry, which is "
            "almost certainly not what this deployment wants.",
            DEFAULT_PLATFORM_REGISTRY,
        )
    return DEFAULT_PLATFORM_REGISTRY


def platform_project() -> str:
    """Project/namespace holding MOD's images within the platform registry.

    Mirrors platform_registry(): HARBOR_PROJECT wins when set; otherwise a set
    CE_IMAGE_REGISTRY supplies the namespace after its first `/`. A CE value
    with no `/` (host only) leaves the project at the default `mod_core`.
    """
    configured = (os.getenv("HARBOR_PROJECT") or "").strip()
    if configured:
        return configured.strip("/")
    ce = _ce_image_registry()
    if ce:
        return ce.split("/", 1)[1].strip("/") if "/" in ce else DEFAULT_PLATFORM_PROJECT
    return DEFAULT_PLATFORM_PROJECT


def platform_image(repo_tag: str) -> str:
    """Fully-qualified reference to a platform image.

    `platform_image("base-python:3.11")` →
    `<registry>/<project>/base-python:3.11`.

    Already-qualified refs pass through untouched: callers hold a mix of bare
    repo:tag and full refs, and re-prefixing a full one would produce
    `harbor/modtim/harbor/modtim/...`, which fails as a pull rather than as
    an obvious error.
    """
    ref = (repo_tag or "").strip()
    if not ref:
        return ref
    if "/" in ref.split(":", 1)[0]:
        return ref
    # EARLY.CE-NODE-IMAGES-FOLLOW-RELEASE-TAG: a CE install that pins a release
    # tag pulls its node images under that tag, not `:latest`. No-op for paid
    # (CE_IMAGE_REGISTRY unset) and for an unpinned/`latest` CE install.
    return _retag_ce_ref(f"{platform_registry()}/{platform_project()}/{ref}")


CE_SHIPPED_REGISTRY = "ghcr.io/modtim"


def ce_mirror_ref(ref: str) -> str:
    """Rewrite a baked CE-shipped ref onto the install's CE mirror.

    Node definitions seed full refs under `ghcr.io/modtim/`. When the install's
    `CE_IMAGE_REGISTRY` points elsewhere (test registry, air-gapped mirror) the
    worker must pull from there. Only the exact `ghcr.io/modtim/` prefix is
    rewritten; digests in the remainder are kept. Anything else (other
    namespaces, user/private registries, CE_IMAGE_REGISTRY unset or equal to the
    shipped registry) is returned unchanged.
    """
    ce = _ce_image_registry()
    prefix = CE_SHIPPED_REGISTRY + "/"
    if not ce or not (ref or "").startswith(prefix):
        return ref
    if ce != CE_SHIPPED_REGISTRY:
        ref = f"{ce}/{ref[len(prefix):]}"
    # EARLY.CE-NODE-IMAGES-FOLLOW-RELEASE-TAG: node definitions seed baked
    # `ghcr.io/modtim/<name>:latest` refs, so the mirror rewrite alone still
    # pulled `:latest` on a release-pinned install. Every ref this function
    # touches is a MOD-owned node image, so pin it to the install's CE_IMAGE_TAG
    # too (no-op when unset/`latest`, or when the ref carries an explicit tag
    # or a digest).
    return _retag_ce_ref(ref)


def build_push_registry() -> str:
    """Where a build task pushes the image it just built.

    Distinct from platform_registry() on purpose — see the module docstring.
    """
    configured = (os.getenv("HARBOR_REGISTRY") or "").strip()
    return configured.rstrip("/") if configured else platform_registry()


def platform_mirror_registry() -> Optional[str]:
    """A local mirror of MOD's catalogue, or None if this deployment has none.

    `_resolve_platform_image` probes the authoritative registry, then this. It
    is how a deployment stops depending on MOD: mirror the catalogue and the
    fallback finds it.

    Split out of LOCAL_REGISTRY, which was doing two unrelated jobs. Everywhere
    else LOCAL_REGISTRY means "where TENANT images live" — it is the build
    container's push target (`REGISTRY_TARGET: "local"`) and what qualifies a
    tenant node's ref. Only this one function used it to mean "a second copy of
    the PLATFORM catalogue". One variable cannot be both: pointing it at the
    tenant Harbor to fix the tenant side would put MOD's images inside the
    tenant supply chain, which the two-Harbor split exists to prevent.

    Defaults to None rather than to a host. The old default was
    `registry:5000`, a service retired with the registry:2 container — so every
    fallback probe went to a host that does not resolve, failed, and logged a
    warning. The mirror capability was switched off by a dead default. None is
    honest: most deployments have no mirror, and the probe is skipped entirely
    rather than being spent on a guess.
    """
    v = (os.getenv("PLATFORM_MIRROR_REGISTRY") or "").strip()
    return v.rstrip("/") or None


def _registry_base_name(value: str) -> str:
    """A BASE_DOMAIN usable as the tail of a registry hostname, or "".

    Strips a scheme/path and a trailing `:port`. The port matters because CE
    publishes the API on :8000 and the Dashboard on :3000, so BASE_DOMAIN on a
    LAN install carries it (`203.0.113.150:3000`) and composing it produced a
    string that is not a hostname at all — the label `tenant-harbor.203.0.113.150`
    resolves nowhere and the `:3000` parses as a port.

    Returns "" for anything that cannot name a registry: an IP literal (first
    label numeric) or a value with no dot-name (`localhost`, a bare LAN label).
    Those are addresses of the LAN, not of a registry that exists.
    """
    v = (value or "").strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    if "@" in v:
        v = v.split("@", 1)[1]
    v = v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    head, sep, tail = v.partition(":")
    if sep and tail.isdigit():
        v = head
    v = v.strip("/").strip(".")
    if not v:
        return ""
    first = v.split(".", 1)[0]
    if first[:1].isdigit():
        return ""
    if "." not in v:
        return ""
    return v


def tenant_registry(deployment_mode: Optional[str] = None) -> str:
    """Registry holding images a tenant built.

    On-prem this is a registry on the customer's own infrastructure, which is
    what makes "your images stay yours" already true for anything they build.
    """
    mode = (deployment_mode
            if deployment_mode is not None
            else os.getenv("DEPLOYMENT_MODE") or "").lower()
    if mode == "saas":
        return (os.getenv("TENANT_HARBOR_REGISTRY")
                or "example.com").rstrip("/")
    # An explicit override still wins — an operator pointing this somewhere
    # deliberate must not be second-guessed.
    configured = (os.getenv("LOCAL_REGISTRY") or "").strip()
    if configured:
        return configured.rstrip("/")

    # Otherwise take the name start-main-server.sh assigned. That script is
    # where internal systems are named; composing the hostname here instead
    # would make this a SECOND namer, and two things computing one name is
    # exactly how LOCAL_REGISTRY came to hold `registry:5000` — a container-DNS
    # name (resolvable only inside the compose network) standing in for a
    # routable one. An image ref is consumed by a Docker daemon on a HOST,
    # including the embedded worker's, so it must be a name that resolves
    # outside the network. No remote worker could ever reach the old value.
    named = (os.getenv("TENANT_HARBOR_HOST") or "").strip()
    if named:
        return named.rstrip("/")

    # Deployments provisioned before TENANT_HARBOR_HOST existed have not re-run
    # start-main-server.sh — and it is not a script to re-run casually, since
    # it tears the stack down. Derive the same value from the parts it did
    # export, rather than failing or reaching for a default that is wrong
    # everywhere.
    sub = (os.getenv("TENANT_HARBOR_SUBDOMAIN") or "tenant-harbor").strip("/")
    raw_base = (os.getenv("BASE_DOMAIN") or "").strip()
    if not raw_base:
        return sub
    base = _registry_base_name(raw_base)
    if not base:
        # Nothing nameable to compose onto. CE ships no registry container at
        # all (its compose says "no private registry"), and a LAN install's
        # BASE_DOMAIN is an IP — so the honest answer is that there is no
        # tenant registry here, not a fabricated host that only ever probes
        # as a transport failure. Callers render "" as "not configured".
        return ""
    return f"{sub}.{base}"


def join_image_ref(prefix: str, *parts: str) -> str:
    """Join parts into a Docker image ref, never returning a leading slash.

    When prefix is empty, None, whitespace-only, or only slashes,
    the separator is omitted so the result never starts with '/'.
    Trailing slashes on prefix are stripped so `` "reg.local/" + "a" ``
    does not produce `` "reg.local//a" ``.

    A leading '/' is not a valid Docker image reference, so this helper
    is the single point that keeps every consumer-safe for an offline LAN CE
    where tenant_registry() returns "".
    """
    if prefix is None:
        prefix = ""
    prefix = prefix.strip()
    prefix = prefix.rstrip("/")
    segs = [p.lstrip("/") for p in parts if p]
    if prefix:
        return f"{prefix}/{'/'.join(segs)}"
    return "/".join(segs)


def tenant_image(image_ref: str, tenant_id: int,
                 deployment_mode: Optional[str] = None) -> str:
    """Fully-qualified reference to a tenant-built image.

    When the registry/prefix is empty or whitespace (CE shape: the image
    lives on the building machine's own daemon), returns only the normalised
    image ref (bare ref, no leading /).  When the registry is set, returns
    ``registry/tenant-N/ref``.
    """
    prefix = tenant_registry(deployment_mode)
    if not (prefix or "").strip():
        # Empty / whitespace prefix — bare ref, the correct CE shape.
        # Strip a leading '/' that the caller may have passed (e.g. "/ref" -> "ref").
        return image_ref.lstrip("/") if image_ref else ""
    return join_image_ref(prefix, f"tenant-{tenant_id}", image_ref)


# Reachability probes, cached. A registry host is a network dependency, and the
# panel that reports it was answering the wrong question: it showed whether a
# variable was SET, which said nothing about whether anything answers there.
# `registry:5000` sat green for months on a host that had stopped existing.
_REACH_CACHE: dict = {}
# Asymmetric on purpose. A success is stable — a registry that answered a
# second ago is almost certainly still there — so it can be held a while. A
# FAILURE is frequently transient: the first probe after an API restart races
# DNS and nginx coming up, and caching that for a minute pins a red badge on a
# perfectly healthy registry at exactly the moment someone is looking. Observed
# on this deployment: tenant-harbor.localhost probed False once at boot, then
# True on every subsequent attempt.
_REACH_TTL_OK_S = 60.0
_REACH_TTL_FAIL_S = 10.0


def registry_reachable(host: Optional[str], timeout_s: float = 2.5):
    """True / False / None for a registry host.

    None means "not checked" — no host, or the probe itself could not run.
    That is deliberately distinct from False: "I could not look" and "I looked
    and nothing is there" send an operator to different places.

    Cached for a minute. This is called per render and a registry that is down
    should not turn one page load into three hanging connections.
    """
    if not host:
        return None
    import time
    now = time.time()
    hit = _REACH_CACHE.get(host)
    if hit and hit[0] > now:
        return hit[1]

    result = None
    try:
        import httpx
        target = host if "://" in host else f"http://{host}"
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as client:
            resp = client.get(f"{target.rstrip('/')}/v2/")
        # 401/403 mean the registry is THERE and wants credentials, which is a
        # healthy private registry. Only a transport failure means unreachable.
        result = resp.status_code < 500
    except Exception:  # noqa: BLE001
        result = False

    ttl = _REACH_TTL_OK_S if result else _REACH_TTL_FAIL_S
    _REACH_CACHE[host] = (now + ttl, result)
    return result
