"""Persistent Stacks (add-persistent-stacks).

Create / deploy / stop / teardown a docker-compose project MOD runs on a worker.
Lifecycle ops are dispatched as pinned PLATFORM_TASKs to the stack's worker via
the proven targeted-dispatch path (assigned_to_worker + ApiTaskQueue), the same
mechanism trigger-benchmark uses. Tenant-scoped to the owning user.
"""
from __future__ import annotations

import copy
import hmac
import inspect
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import models
import schemas
import permissions
from dependencies import get_db, get_current_user, require_platform_capability, require_stack_author_key
from routers.worker_tasks import _publish_event
from service_builder_schemas import (
    AssembleRequest, AssembleResponse, ComposeImportFinalizeRequest,
    ComposeImportPreviewResponse, ComposeImportRequest, EstimateRequest,
    EstimateResponse, EstimateServiceCost, PlacementCheckRequest,
    RenderPreviewRequest, RenderPreviewResponse, ServiceSpec,
    WizardPrefillResponse,
)

from product_names import themed_logger

logger = themed_logger(__name__, 'stacks')




# delegated-stack-authoring §2.1 — the router is gated on the STACK_AUTHOR
# platform capability (admins bypass via is_platform_admin inside the check).
# Admin-only endpoints below re-add Depends(_require_platform_admin).

# This manager's region — stacks created here are stamped with it (cell-ready:
# a workload carries a home zone and is reached via manager-as-directory).
MANAGER_REGION = os.getenv("MANAGER_REGION", "")


def _resolve_home_zone(requested: Optional[str]) -> Optional[str]:
    """Use the caller-supplied home_zone, else default to this manager's region so
    every stack is region-stamped (the mesh routes work to where its data lives)."""
    return requested or (MANAGER_REGION or None)


# --- Template catalog (add-persistent-stacks §7) ---------------------------
# Parameterized stacks a non-Docker user can stand up by picking one and filling
# a couple of fields. Each renders a validated compose project.
#
# GA.ASW.1 (launch-completion-megaplan) — the catalog itself now lives in
# CORE/API/service_templates/*.yaml, loaded/cached by services.service_templates.
# STACK_TEMPLATES / _render_template are re-exported here under their original
# names so every existing importer (routers, services, tests — see
# `grep -rn STACK_TEMPLATES`) keeps working unchanged.
from services.service_templates import STACK_TEMPLATES, render_template as _render_template  # noqa: E402
from utils.http_errors import internal_http_error
from utils.registries import platform_image


class TemplateCreate(BaseModel):
    template_id: str
    name: str
    params: Optional[dict] = None
    # WS4.Q8 (launch-completion-megaplan) — the builder's template on-ramp is a
    # PRE-FILL, not a create. True = render + validate the compose and return
    # it WITHOUT persisting a status=created PersistentStack (no row, no audit);
    # the deploy persists later through /assemble. False (default) = legacy
    # create behaviour, unchanged.
    preview: bool = False
    # GA.ASW.6 — see PersistentStackCreate.acknowledge_no_backup. Consulted
    # only on the persisting (non-preview) branch.
    acknowledge_no_backup: bool = False


class WizardCreate(BaseModel):
    name: str
    prompt: str
    # WS4.Q8 — /from-wizard is preview-only (it never persists a stack row,
    # mega-qa #50). The flag exists so BOTH on-ramps share one explicit
    # preview contract for the builder; it does not change behaviour here.
    preview: bool = False


def _project_name(stack: models.PersistentStack) -> str:
    # docker compose -p <name>; generation-aware so blue/green migration can stand
    # up a new generation (g1, g2, …) alongside the old before cutover.
    refs = stack.storage_refs or {}
    return refs.get("project") or f"modstack{stack.id}g0"


# The label the worker reads to decide whether a stack may be fenced.
HA_CLASS_LABEL = "com.example.ha-class"


# mod-connect-service-mesh §3 — per-service mesh identity by sidecar injection at
# deploy. A service opts in with `x-mod.reachable: true`; it gets a tailscale
# sidecar (ts-<svc>) and is rewritten to share the sidecar's network namespace, so
# the SERVICE (not the host) becomes a tailnet node with a stable MagicDNS name.
# The rewrite happens on the copy handed to the worker; the stored spec is
# untouched. Non-reachable services and opt-out stacks are unmodified.
MESH_SIDECAR_IMAGE = os.getenv("MOD_TS_SIDECAR_IMAGE", "tailscale/tailscale:latest")

# dual-anchor-mesh-federation §two-address-registration — a "mesh anchor" is one
# control plane a reachable service can be reached through. A dual-homed worker
# runs two: `site` (local headscale on the manager, the offline-survival plane)
# and `cloud` (the fixed cloud headscale/DERP, the roaming/rendezvous plane). A
# single-anchor install (today's default) has exactly one, so every path below
# collapses to the pre-existing single-address behaviour.
MESH_ANCHOR_SITE = "site"
MESH_ANCHOR_CLOUD = "cloud"
# Resolution order: site first (direct/offline-capable), cloud as the fallback.
MESH_ANCHOR_ORDER = (MESH_ANCHOR_SITE, MESH_ANCHOR_CLOUD)


def _public_control_plane_login_server(db=None) -> str:
    """FX.31 s1b — thin delegate to the ONE shared decision
    (`services.mesh_public_vhost_nginx.public_control_plane_login_server`), the
    same helper `routers/worker_management._anchor_login_server` asks: the public
    control-plane URL when `mesh.public_control_plane` is ON and this install
    advertises an off-site site address (MOD_SITE_LOGIN_SERVER_EXTERNAL), ""
    otherwise. The setting is never re-implemented here."""
    # 1788d CE carve: services/mesh_public_vhost_nginx.py is paid (manifest deny
    # `CORE/API/**/mesh_*.py`). The public control-plane vhost is a paid
    # capability: with the module absent the install advertises no public
    # control plane, so the resolver answers "" (the pre-existing OFF behaviour).
    from utils.paid_import import optional_paid
    _vhost = optional_paid("services.mesh_public_vhost_nginx")
    if _vhost is None:
        return ""
    return _vhost.public_control_plane_login_server(db=db) or ""


def _anchor_login_server(anchor: str, db=None) -> str:
    """The login (control) server a service's sidecar dials for `anchor`.
    site  -> MOD_SITE_LOGIN_SERVER, then MOD_TAILNET_SITE_LOGIN_SERVER (the name
             start-main-server actually writes for the dual-home site plane),
             then the legacy single MOD_TAILNET_LOGIN_SERVER so an un-migrated
             single-anchor install is unchanged.
    cloud -> MOD_CLOUD_LOGIN_SERVER (empty unless a cloud anchor is configured).

    NOTE: on a dual-anchor box MOD_TAILNET_LOGIN_SERVER is the CLOUD control URL
    (headwind), so the site anchor MUST prefer the site-specific vars first — else
    site-plane sidecars would silently enroll to the cloud anchor and the site
    (offline) plane would never form.

    GSEC FX.31 s1b — the SITE anchor asks the same shared public-control-plane
    decision the worker join uses FIRST, when the install advertises an off-site
    site address: with `mesh.public_control_plane` ON, FX.31 p1a keeps every
    headscale/headscale-tls host publish 127.0.0.1-or-LAN, so a sidecar on an
    isolated per-stack bridge can only enroll through the public nginx TLS vhost
    `https://mesh-control.<BASE_DOMAIN>:443`. Option OFF (the default) → the
    helper answers "" and the chain below is byte-for-byte the pre-FX.31
    behaviour; a co-located install (no MOD_SITE_LOGIN_SERVER_EXTERNAL) is
    unchanged even with the option ON."""
    if anchor == MESH_ANCHOR_CLOUD:
        return os.getenv("MOD_CLOUD_LOGIN_SERVER", "").strip()
    public = _public_control_plane_login_server(db)
    if public:
        return public
    return (os.getenv("MOD_SITE_LOGIN_SERVER", "").strip()
            or os.getenv("MOD_TAILNET_SITE_LOGIN_SERVER", "").strip()
            or os.getenv("MOD_TAILNET_LOGIN_SERVER", "").strip())


def _anchor_login_server_with_db(anchor: str, db=None) -> str:
    """Arity-tolerant call of `_anchor_login_server` that passes the caller's
    `db` when the live resolver accepts it (FX.31 s1b added the optional second
    parameter so the site anchor can read `mesh.public_control_plane` in the
    caller's transaction). Tests monkeypatch the resolver with its original 1-arg
    shape (test_stack_tailnet_egress), so a 1-arg resolver is called unchanged —
    same arity-detection pattern `_call_minter` uses."""
    try:
        nparams = len(inspect.signature(_anchor_login_server).parameters)
    except (TypeError, ValueError):
        nparams = 1
    return _anchor_login_server(anchor, db) if nparams >= 2 \
        else _anchor_login_server(anchor)


def _configured_mesh_anchors_with_db(db=None) -> list:
    """Arity-tolerant call of `_configured_mesh_anchors` that passes the caller's
    `db` when the live resolver accepts it (FX.31 s1b added the optional
    parameter so the site anchor can read `mesh.public_control_plane` in the
    caller's transaction). Tests monkeypatch the builder with its original 0-arg
    shape (test_stack_tailnet_egress), so a 0-arg builder is called unchanged —
    same arity-detection pattern `_call_minter` uses."""
    try:
        nparams = len(inspect.signature(_configured_mesh_anchors).parameters)
    except (TypeError, ValueError):
        nparams = 0
    return _configured_mesh_anchors(db) if nparams >= 1 else _configured_mesh_anchors()


def _configured_mesh_anchors(db=None) -> list:
    """The anchors this install actually has, in resolution order. Each entry is
    (anchor_name, login_server, headscale_client|None). An anchor is present only
    when its login server is configured; its client may still be None (no bundled
    control server wired for that plane) — provision treats "no client on any
    anchor" as the unchanged no-op path, and injection simply skips an anchor with
    no mintable key.

    Single-anchor installs (only MOD_TAILNET_LOGIN_SERVER set) yield exactly one
    entry, so injection/provision/revoke behave exactly as before."""
    from utils.paid_import import optional_paid
    _hs = optional_paid("utils.headscale_client")
    if _hs is None:
        return []  # CE — headscale_client is Paid; no mesh anchors to mint
    anchors = []
    for name in MESH_ANCHOR_ORDER:
        # FX.31 s1d — go through the arity-tolerant wrapper, not the resolver
        # directly: tests monkeypatch `_anchor_login_server` with its original
        # 1-arg shape (test_stack_tailnet_egress), and a positional 2-arg call
        # here would TypeError on those stubs (the outer `_configured_mesh_anchors`
        # wrapper only tolerates arity of the *builder*, not of the resolver it
        # calls internally).
        login = _anchor_login_server_with_db(name, db)
        if not login:
            continue
        anchors.append((name, login, _hs.get_headscale_client(name)))
    return anchors


def _call_minter(authkey_for, name: str, anchor: str):
    """Call a minter that may be the legacy 1-arg `authkey_for(name)` or the
    anchor-aware `authkey_for(name, anchor)`. Detecting arity (rather than a
    try/except on TypeError) keeps the existing single-anchor callers/tests
    working unchanged without masking a genuine TypeError from inside the minter."""
    try:
        nparams = len(inspect.signature(authkey_for).parameters)
    except (TypeError, ValueError):
        nparams = 1
    return authkey_for(name, anchor) if nparams >= 2 else authkey_for(name)


def _declares_an_endpoint(svc: dict, name: Optional[str] = None,
                          top_xmod: Optional[dict] = None) -> bool:
    """True when a service publishes something a mesh peer could dial: a
    non-empty `ports` list or a non-empty `expose` list, OR the stack declares
    it as an endpoint at the TOP level — the `x-mod.ingress.service` front door,
    or a service named by `x-mod.host_ports` / `x-mod.ports` (BUG.B61).

    GW.1 (`_render_stack_owned_ports`) MOVES the authored `ports:` off every
    service into the stack-level `x-mod.ports` record and re-renders only what
    `x-mod.host_ports` asks for, so on any stack imported since that change a
    published service can end up with NO per-service `ports:` key at all — stack
    114's `web` (the ingress front door, published via `x-mod.host_ports`) is
    exactly that case, and BUG.B61 was that it silently got no mesh. The stack
    still publishes its port, so it still has an endpoint to be reachable over.

    `name` and `top_xmod` are optional: callers that only have the service dict
    (a capability probe, an ACL posture walk) keep the pre-B61 ports/expose
    answer. An EMPTY list counts the same as ABSENT."""
    if not isinstance(svc, dict):
        return False
    for key in ("ports", "expose"):
        val = svc.get(key)
        if val and isinstance(val, (list, dict, str)):
            return True
    if name is None or not isinstance(top_xmod, dict):
        return False
    ingress = top_xmod.get("ingress")
    if isinstance(ingress, dict) and ingress.get("service") == name:
        return True
    for key in ("host_ports", "ports"):
        for entry in (top_xmod.get(key) or []):
            if isinstance(entry, dict) and entry.get("service") == name:
                return True
    return False


def _service_wants_mesh(svc, name: Optional[str] = None,
                        top_xmod: Optional[dict] = None) -> bool:
    """Whether a service is reachable over the mesh.

    Rules, in precedence order:
    1. An explicit boolean `x-mod.reachable` wins in BOTH directions —
       `true` forces meshing, `false` opts out. This is the existing owner
       decision (09-29) and is untouched.
    2. Absent the key, a service that declares NO `ports` and NO `expose`
       (an empty list counts as absent) is NOT meshed. Reachability is
       exercised by dialing a published port on the deterministic MagicDNS
       name (`<prefix>-<svc>`), so a portless service has nothing to be
       reachable over. This is what bites one-shot jobs (e.g. stack 34's
       `ollama-pull`, which runs `ollama pull` and exits 0): meshing it
       still injected three containers (the `ts-<svc>-net` holder + the
       site/cloud sidecars), all of which sit running forever after the
       job exits, and the owner expected "just ollama".
    3. Otherwise the answer is the install's mesh posture: True when ANY
       mesh anchor's login server is configured (env-only probe —
       `_anchor_login_server` per `MESH_ANCHOR_ORDER`; NOT
       `_configured_mesh_anchors()`, which builds headscale clients), False
       when no anchor is configured at all (nothing to be reachable over).

    BUG.B61 — rule 2 now ALSO counts a service the STACK declares as an
    endpoint: the `x-mod.ingress.service` front door, or one named by
    `x-mod.host_ports` / `x-mod.ports`. GW.1 strips the per-service `ports:`
    key off those, so the old ports-only reading dropped the front door (stack
    114's `web`, published on :8080 via host_ports, got no mesh). Pass `name`
    + `top_xmod` from a caller that has the whole spec; an explicit
    `x-mod.reachable: false` still wins (rule 1, opt-out preserved)."""
    xmod = svc.get("x-mod") if isinstance(svc, dict) else None
    if isinstance(xmod, dict) and isinstance(xmod.get("reachable"), bool):
        return xmod["reachable"]
    if not _declares_an_endpoint(svc, name=name, top_xmod=top_xmod):
        return False
    # A service that dials tailnet peers needs the tsegress carrier (kernel
    # tailscale0 + routes); the userspace mesh sidecar would own its netns and
    # leave it with no route to 100.64/10 — and _inject_tailnet_egress skips any
    # meshed service. 10-02: the mesh default silently did exactly that to the
    # switchyard on its first redeploy since 09-29, and every agent lane 502'd.
    # The egress cloud sidecar is itself a tailnet node, so nothing is lost.
    if _service_needs_tailnet_egress(svc):
        return False
    return any(_anchor_login_server(n) for n in MESH_ANCHOR_ORDER)


MESH_CA_DIR = "/tmp/mesh-ca"


def _trust_site_ca_at_start(sc: dict, login_server: str,
                            ca_bundle_host_path: Optional[str] = None) -> None:
    """Make a SITE mesh sidecar trust the manager's private CA on ANY worker.

    Two trust paths, in precedence order — the SAME two the tailnet-EGRESS site
    sidecar (`_tsegress_sidecar`) and the manager's own `mod_tailscale_site` client
    already use; nothing new is invented here:

    1. Bind-mount the LIVE host bundle read-only and point tailscaled's Go TLS at
       it via SSL_CERT_FILE — the mechanism the egress sidecar/worker path uses.
       The host path of the full chain (public roots + our root) is carried by
       MOD_MESH_CA_BUNDLE_HOST_PATH (self-healed next to the other site-anchor
       vars by start-main-server / the reconcile watchdog), so a cert/CA rotation
       is picked up on the next restart with nothing baked into the stored spec.
       This is the path that actually fixes P19 (gb10 stack 39): the fetch below
       dials the login host over plain HTTP from inside the stack's own netns, so
       when the login server is the internal control name (or the LAN IP) the
       fetch cannot resolve it and the sidecar enrolls with NO CA at all —
       `tailscale up` then dies on the TLS handshake and crash-loops from first
       boot. A bind-mount needs no outbound request from the sidecar.

    2. Otherwise fetch the CA at start (trust-on-first-fetch, `mesh.FetchSiteCA`)
       into a dir added to Go's SSL_CERT_DIR so the public roots still apply —
       the pre-existing behaviour for a remote worker with no host path to bind
       from. Fetching at start (not at render) keeps outbound HTTP out of the
       deploy transaction. A failed fetch just starts without it (public-cert
       servers).

    Never disables verification: no `--insecure`, no `-k`, no `TS_TLS_IGNORE_CERTS`
    — an untrusted chain fails the handshake loudly instead of silently enrolling
    against a spoofed control plane. Nothing key-bearing is ever logged."""
    from urllib.parse import urlparse
    ca_host_path = (ca_bundle_host_path
                    or os.getenv("MOD_MESH_CA_BUNDLE_HOST_PATH", "")).strip()
    if ca_host_path:
        # Read-only mount only — the sidecar must never write the manager's bundle.
        sc["environment"]["SSL_CERT_FILE"] = MESH_CA_MOUNT
        sc.setdefault("volumes", []).append(f"{ca_host_path}:{MESH_CA_MOUNT}:ro")
    host = urlparse(login_server or "").hostname
    if not host or not re.fullmatch(r"[A-Za-z0-9.\-]+", host):
        return  # it lands in a shell string; anything odd gets no CA fetch
    ca_file = f"{MESH_CA_DIR}/mod-local-ca.crt"
    sc["environment"]["SSL_CERT_DIR"] = f"/etc/ssl/certs:{MESH_CA_DIR}"
    sc["entrypoint"] = [
        "sh", "-c",
        f"mkdir -p {MESH_CA_DIR}; "
        f"wget -q -T 10 -O {ca_file} http://{host}/mod-local-ca.crt || rm -f {ca_file}; "
        "exec /usr/local/bin/containerboot",
    ]


def _inject_mesh_sidecars(spec: dict, name_prefix: str, authkey_for,
                          login_server: str, anchors: Optional[list] = None) -> dict:
    """Rewrite `spec` in place: for each service marked x-mod.reachable that has a
    mintable auth key, add a tailscale sidecar per configured anchor and share the
    carrier anchor's netns so the served container becomes a tailnet node on every
    plane (dual-homed). It registers under ONE logical MagicDNS name
    (`<prefix>-<svc>`, identical hostname on every plane) with one address per
    anchor — dual-anchor-mesh-federation §two-address-registration.

    `anchors` is a list of (anchor_name, login_server) pairs in resolution order.
    When omitted the single legacy anchor `[("site", login_server)]` is used, so an
    existing caller passing just `login_server` produces exactly the pre-existing
    single sidecar (`ts-<svc>`, one address). The carrier is the FIRST anchor that
    has a mintable key and keeps the legacy `ts-<svc>` name so single-anchor output
    is unchanged; a further anchor adds `ts-<svc>-<anchor>` sharing the carrier's
    network namespace.

    `authkey_for(service_name[, anchor]) -> Optional[str]`; a service with NO key
    on ANY anchor is left unmodified (fail-safe: no identity beats a broken
    deploy). An anchor that has no key is skipped, so the service still comes up on
    whatever anchors it did get a key for rather than failing."""
    services = spec.get("services")
    if not isinstance(services, dict):
        return spec
    if not anchors:
        anchors = [(MESH_ANCHOR_SITE, login_server)]
    # The site plane is only dialable when the stack's own authored networks do not
    # SHADOW the site login server (longest-prefix routing would send the dial to
    # the stack bridge, so the sidecar can never reach its control plane and
    # crash-loops forever — see _site_plane_shadowed). Drop the site anchor in
    # that case and tell the operator to re-subnet; the cloud anchor is unaffected.
    if any(n == MESH_ANCHOR_SITE for n, _l in anchors):
        site_login = next(l for n, l in anchors if n == MESH_ANCHOR_SITE)
        shadowed = (_site_plane_shadowed(spec, site_login)
                    or _site_plane_shadowed(spec, _egress_site_login_server(site_login)))
        if shadowed:
            net_key, subnet, ip = shadowed
            logger.warning(
                "site mesh plane skipped for stack %s: authored network '%s' "
                "(subnet %s) contains the site login server %s, so a site sidecar "
                "could never reach its control plane (the route is shadowed by the "
                "stack's own bridge network). Change that network's subnet to one "
                "that does not include this IP; other anchors are unaffected.",
                name_prefix, net_key, subnet, ip)
            anchors = [(n, l) for n, l in anchors if n != MESH_ANCHOR_SITE]
    vols = spec.get("volumes")
    if not isinstance(vols, dict):
        vols = {}
        spec["volumes"] = vols
    _top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else None
    for name in list(services.keys()):
        svc = services.get(name)
        if not isinstance(svc, dict) or not _service_wants_mesh(svc, name=name,
                                                              top_xmod=_top_xmod):
            continue
        mesh_name = f"{name_prefix}-{name}"   # ONE logical MagicDNS name, all planes
        # Netns owner: a control-plane-free no-op (the tailnet-egress holder
        # pattern). It carries the service's ports, networks and name alias, so a
        # tailscale sidecar that cannot enroll (unreachable login server, bad CA)
        # crash-loops ALONE instead of tearing down the served container's netns
        # and ports with it — 09-29, stacks 29/30/31/34 lost :8080/:11434 when
        # mesh reachability became the default and every carrier crash-looped.
        holder = f"ts-{name}-net"
        sidecars = []
        for anchor_name, anchor_login in anchors:
            key = _call_minter(authkey_for, name, anchor_name)
            if not key:
                continue  # no identity on this plane — skip it (may still be single-homed)
            # First plane keeps the legacy `ts-<svc>` name; extra planes get
            # `ts-<svc>-<anchor>`. All of them join the holder's netns, so the ONE
            # served container is present on every anchor at once (dual-homed).
            sidecar = f"ts-{name}" if not sidecars else f"ts-{name}-{anchor_name}"
            state_vol = (f"ts-state-{name}" if not sidecars
                         else f"ts-state-{name}-{anchor_name}")
            # P19 (gb10 stack-39 site sidecar, 10-31): the SITE sidecar rides an
            # isolated per-stack bridge with no route to the internal control name,
            # so it must dial the EXTERNAL site login server the platform advertises
            # to off-core clients — the SAME rewrite the tailnet-egress anchors and
            # the shadow check already use (`_egress_site_login_server`: MOD_SITE_LOGIN_SERVER_EXTERNAL,
            # falling back to the mesh login when unset, i.e. unchanged behaviour on
            # a co-located install). Without it the enrollment dial never resolves and
            # the sidecar crash-loops from first boot (never enrolled).
            login = (_egress_site_login_server(anchor_login)
                     if anchor_name == MESH_ANCHOR_SITE else anchor_login)
            env = {
                "TS_USERSPACE": "true",                   # 3.6 no NET_ADMIN needed
                "TS_AUTHKEY": key,                        # 3.5 masked by _SECRET_ENV_RE
                "TS_LOGIN_SERVER": login,
                # containerboot ignores TS_LOGIN_SERVER; without --login-server the
                # node dials controlplane.tailscale.com and never enrolls.
                "TS_EXTRA_ARGS": f"--login-server={login}",
                # several tailscaleds share this netns; each picks its own UDP port
                "TS_TAILSCALED_EXTRA_ARGS": "--port=0",
                "TS_STATE_DIR": "/var/lib/tailscale",
                "TS_HOSTNAME": mesh_name,                 # 3.2 same MagicDNS name
            }
            sc = {
                "image": MESH_SIDECAR_IMAGE,
                "environment": env,
                "volumes": [f"{state_vol}:/var/lib/tailscale"],  # 3.3 identity survives restart
                "restart": "unless-stopped",
                "network_mode": f"service:{holder}",
                "depends_on": [holder],
            }
            if anchor_name == MESH_ANCHOR_SITE:
                # Read fresh (not at render): the bundle path is kept current, and
                # the API recreated, by start-main-server / the reconcile watchdog,
                # so a manager move is tracked without a hardcode — same read the
                # tailnet-egress site sidecar makes in _inject_tailnet_egress.
                _trust_site_ca_at_start(
                    sc, login,
                    ca_bundle_host_path=(
                        os.getenv("MOD_MESH_CA_BUNDLE_HOST_PATH", "").strip() or None))
            services[sidecar] = sc
            vols.setdefault(state_vol, {})
            sidecars.append(sidecar)
        if not sidecars:
            continue  # no identity minted on any anchor — leave the service as authored
        hc = _tsegress_holder()
        hc["hostname"] = mesh_name
        # compose rejects network_mode alongside networks/hostname, so the holder
        # takes the authored networks, and answers to the service's name on them
        # so siblings still resolve it (ollama-pull's OLLAMA_HOST=http://ollama).
        authored = svc.pop("networks", None) or ["default"]
        if isinstance(authored, list):
            authored = {n: None for n in authored if isinstance(n, str)}
        holder_nets = {}
        for net, cfg in (authored.items() if isinstance(authored, dict) else []):
            cfg = dict(cfg) if isinstance(cfg, dict) else {}
            aliases = [a for a in (cfg.get("aliases") or []) if isinstance(a, str)]
            if name not in aliases:
                aliases.append(name)
            cfg["aliases"] = aliases
            holder_nets[net] = cfg
        if holder_nets:
            hc["networks"] = holder_nets
        # Authored host ports stay published (by the holder): dropping them broke
        # every host-port consumer once mesh reachability became the default.
        if "ports" in svc:
            hc["ports"] = svc.pop("ports")
        services[holder] = hc
        svc.pop("hostname", None)
        svc["network_mode"] = f"service:{holder}"
        for dep in [holder] + sidecars:
            _add_dep(svc, dep)
    return spec


# Tailnet EGRESS (not reachability): a service whose x-mod.egress.hosts names a
# tailnet peer (100.64.0.0/10, the CGNAT range headscale hands out) must be able
# to DIAL that peer. Docker Desktop containers have no route to the host's
# tailscale0 (2026-09-25, switchyard stack 22 could not reach its llama
# backends), so such a service shares one netns with tailscale sidecars — one per
# anchor, so it keeps working with the WAN down:
#
#   tsegress-<svc>        HOLDER: a no-op sleep loop that owns the netns (egress
#                         net, published ports, hostname). It runs no tailscaled,
#                         so a sidecar crash-looping (control plane unreachable)
#                         never drops the served container's inbound.
#   tsegress-site-<svc>   SITE anchor, USERSPACE, in the holder's netns. Enrolls on
#                         the local headscale, so it needs no internet. Site peers
#                         (MOD_TAILNET_SITE_PREFIX) are dialed through its SOCKS5 /
#                         HTTP proxy on loopback. State stays `tsegress-<svc>-state`
#                         (the identity it had when it was the netns owner).
#   tsegress-cloud-<svc>  CLOUD anchor, KERNEL mode, in the holder's netns: real
#                         tailscale0 + per-peer routes, so cloud 100.64.x.y peers
#                         are dialed directly, no proxy.
#
# Two kernel tailscales can't share a netns (both own routing table 52 and the
# ts-* chains), hence userspace for site. Either sidecar is optional: an anchor
# with no client or a failed mint (e.g. an offline deploy can't reach headwind)
# is skipped; the holder is always present once any anchor yields a key.
TSEGRESS_PREFIX = "tsegress-"
TSEGRESS_SITE_PREFIX = "tsegress-site-"
TSEGRESS_CLOUD_PREFIX = "tsegress-cloud-"
# Mount point for the SITE plane's trust bundle inside a sidecar. The site EXTERNAL
# login server (MOD_SITE_LOGIN_SERVER_EXTERNAL) fronts a private-CA (Caddy) control
# plane, so a site egress sidecar must trust that CA to enroll. The host path of the
# LIVE bundle — the FULL chain (public roots + our root) rendered by
# mesh-headscale-tls.sh — is carried by MOD_MESH_CA_BUNDLE_HOST_PATH, set by
# start-main-server next to the other self-healed site-anchor vars. Bound live so a
# cert/CA rotation is picked up on the next sidecar restart; nothing is copied into
# the stored spec. Unset (no dual-homed site plane) → no CA wiring, unchanged.
MESH_CA_MOUNT = "/mesh-ca/ca-bundle.crt"
_TAILNET_NET = __import__("ipaddress").ip_network("100.64.0.0/10")
_TAILNET_DNS_SUFFIXES = (".example.com",)
TAILNET_SITE_PREFIX = os.getenv("MOD_TAILNET_SITE_PREFIX", "100.96.0.0/12")
TAILNET_SITE_SOCKS = "127.0.0.1:1055"
TAILNET_SITE_HTTP_PROXY = "127.0.0.1:1056"


def _is_tailnet_host(host: Any) -> bool:
    h = str(host or "").strip().lower().rstrip(".")
    if not h:
        return False
    if h.endswith(_TAILNET_DNS_SUFFIXES):
        return True
    import ipaddress
    try:
        return ipaddress.ip_address(h) in _TAILNET_NET
    except ValueError:
        return False


def _service_needs_tailnet_egress(svc: Any) -> bool:
    """True for an x-mod.egress `hosts` service that names a tailnet peer."""
    xmod = svc.get("x-mod") if isinstance(svc, dict) else None
    eg = xmod.get("egress") if isinstance(xmod, dict) else None
    if not isinstance(eg, dict) or eg.get("mode") != "hosts":
        return False
    hosts = eg.get("hosts")
    return isinstance(hosts, list) and any(_is_tailnet_host(h) for h in hosts)


def _egress_site_login_server(mesh_login: str) -> str:
    """The SITE control URL an EGRESS sidecar must dial.

    A mesh (reachability) sidecar shares a core network where the internal control
    name (`MOD_TAILNET_SITE_LOGIN_SERVER`, e.g. https://headscale-tls:8443) resolves.
    An EGRESS sidecar rides an isolated per-stack bridge with no route to that name,
    so it must dial the LAN-reachable EXTERNAL login server the platform advertises to
    off-core clients: `MOD_SITE_LOGIN_SERVER_EXTERNAL`. That value is self-healed to the
    manager's current LAN IP / mDNS name — and the running API recreated — by
    start-main-server Step 7c-pre0 AND the periodic mod_mesh_reconcile_lan_ip watchdog,
    so reading it fresh here tracks a manager move without a redeploy. Falls back to the
    mesh login when no external is configured (single-anchor / non-dual-home install),
    i.e. exactly the prior behaviour."""
    return os.getenv("MOD_SITE_LOGIN_SERVER_EXTERNAL", "").strip() or mesh_login


def _site_plane_shadowed(spec: dict, login_server: str) -> Optional[tuple[str, str, str]]:
    """(network_key, subnet, ip) when an AUTHORED compose network in `spec`
    (a top-level `networks:` entry with `ipam.config[].subnet`) contains the
    login server's IP, else None.

    WHY: a site-plane tailscale sidecar must dial the site control plane to
    enroll. Routes match by longest prefix, so when the stack's own bridge
    subnet covers that IP, traffic to the login server is captured by the
    stack bridge and never leaves the host — the sidecar can never reach its
    control plane and crash-loops forever. Callers use this to SKIP the site
    anchor for that stack (the cloud anchor is unaffected) rather than render
    a sidecar that can only ever fail.

    Best-effort by contract: never raises, never blocks a deploy. An
    IP-literal host is used as-is; a hostname resolves via socket.getaddrinfo
    (any failure → treated as NOT shadowed). Malformed subnets and IPv4/IPv6
    version mismatches are ignored."""
    from urllib.parse import urlparse
    host = (urlparse(login_server or "").hostname or "").strip().lower()
    if not host:
        return None
    import ipaddress
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        import socket
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return None  # unresolvable at render time → NOT shadowed
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
    nets = spec.get("networks") if isinstance(spec, dict) else None
    if not isinstance(nets, dict):
        return None
    for key, cfg in nets.items():
        if not isinstance(cfg, dict):
            continue
        ipam = cfg.get("ipam")
        if not isinstance(ipam, dict):
            continue
        confs = ipam.get("config")
        if not isinstance(confs, list):
            continue
        for conf in confs:
            subnet = conf.get("subnet") if isinstance(conf, dict) else None
            if not isinstance(subnet, str):
                continue
            try:
                net = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                continue  # malformed subnet → ignore, never shadows
            for addr in addrs:
                if addr.version == net.version and addr in net:
                    return (str(key), subnet, str(addr))
    return None


def _tailnet_egress_anchors() -> Dict[str, tuple]:
    """{anchor: (login_server, client)} for the site/cloud anchors that have a
    control client. Empty → no injection. The SITE anchor's login is rewritten to
    the egress-reachable external control URL (see _egress_site_login_server)."""
    try:
        anchors = _configured_mesh_anchors()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("tailnet egress: anchor lookup failed: %s", e)
        return {}
    return {a[0]: (_egress_site_login_server(a[1]) if a[0] == MESH_ANCHOR_SITE else a[1], a[2])
            for a in anchors
            if a[0] in (MESH_ANCHOR_SITE, MESH_ANCHOR_CLOUD) and a[1] and a[2] is not None}


# Headscale user the egress sidecars enroll as. Platform-owned nodes (the
# manager's own tailscale clients) live under `mod`, whose ACL reaches every
# peer; `workers` could not reach user-owned peers like strix-llm (203.0.113.6).
TAILNET_EGRESS_USER = os.getenv("MOD_TAILNET_EGRESS_USER", "mod")


def _tailnet_egress_authkey(client) -> Optional[str]:
    """Single-use, non-ephemeral (the sidecar's state volume keeps its identity
    across restarts). GSEC FX.31 p2a — TTL comes from the centralized
    mesh.preauth_key_ttl_minutes setting (~1h default; raise the setting if a
    slow enrollment needs longer, rather than a one-off literal here). Any
    failure → None (that anchor is skipped)."""
    try:
        from utils.headscale_client import default_mint_kwargs
        return client.mint_preauth_key(TAILNET_EGRESS_USER, [], ephemeral=False,
                                       **default_mint_kwargs())
    except Exception as e:
        logger.warning("tailnet egress: preauth key mint failed: %s", e)
        return None


def _tsegress_sidecar(hostname: str, key: str, login_server: str, state_vol: str,
                      kernel: bool, ca_bundle_host_path: Optional[str] = None) -> Dict[str, Any]:
    env = {
        "TS_AUTHKEY": key,                # masked by _SECRET_ENV_RE
        "TS_ACCEPT_DNS": "false",
        "TS_STATE_DIR": "/var/lib/tailscale",
        "TS_HOSTNAME": hostname,
        "TS_EXTRA_ARGS": f"--login-server={login_server}",
        # Two tailscaleds may share this netns; let each pick its own UDP port.
        "TS_TAILSCALED_EXTRA_ARGS": "--port=0",
    }
    sc: Dict[str, Any] = {"image": MESH_SIDECAR_IMAGE, "environment": env,
                          "volumes": [f"{state_vol}:/var/lib/tailscale"],
                          "restart": "unless-stopped"}
    if ca_bundle_host_path:
        # The site external login server fronts a private-CA (Caddy) control plane,
        # so the sidecar needs that CA to complete TLS on enrollment. Bind the LIVE
        # host bundle (full chain: public roots + our root) read-only and point
        # tailscaled's Go TLS at it via SSL_CERT_FILE — the same mechanism the
        # manager's own mod_tailscale_site uses. Binding the live file (not its
        # bytes) means a cert/CA rotation is picked up on the next restart with
        # nothing baked into the stored spec.
        env["SSL_CERT_FILE"] = MESH_CA_MOUNT
        sc["volumes"].append(f"{ca_bundle_host_path}:{MESH_CA_MOUNT}:ro")
    if kernel:
        env["TS_USERSPACE"] = "false"     # real tailscale0 + per-peer routes
        sc["cap_add"] = ["NET_ADMIN", "NET_RAW"]
        sc["devices"] = ["/dev/net/tun:/dev/net/tun"]
    else:
        env["TS_USERSPACE"] = "true"
        env["TS_SOCKS5_SERVER"] = TAILNET_SITE_SOCKS
        env["TS_OUTBOUND_HTTP_PROXY_LISTEN"] = TAILNET_SITE_HTTP_PROXY
    return sc


def _tsegress_holder() -> Dict[str, Any]:
    """The netns owner for a tailnet-egress service: a no-op that exits cleanly on
    stop and never depends on a control plane, so the served container's ports
    and networks survive either tailscale sidecar crash-looping."""
    return {"image": MESH_SIDECAR_IMAGE,
            "entrypoint": ["sh", "-c",
                           "trap 'exit 0' TERM INT; while :; do sleep 3600 & wait $!; done"],
            "restart": "unless-stopped"}


def _add_dep(svc: dict, dep: str) -> None:
    deps = svc.get("depends_on")
    if isinstance(deps, dict):
        deps.setdefault(dep, {"condition": "service_started"})
    elif isinstance(deps, list):
        if dep not in deps:
            deps.append(dep)
    else:
        svc["depends_on"] = [dep]


def _free_name(services: dict, base: str) -> str:
    name, n = base, 2
    while name in services:
        name = f"{base}-{n}"
        n += 1
    return name


def _inject_tailnet_egress(spec: dict, name_prefix: str, authkey_for=None,
                           login_server: Optional[str] = None,
                           anchors: Optional[Dict[str, tuple]] = None) -> dict:
    """Rewrite `spec` in place: every service needing tailnet egress (see
    _service_needs_tailnet_egress) joins the netns of its tsegress HOLDER, as do
    its per-anchor tailscale sidecars (layout above). The holder carries the
    service's `ports:` / `networks:` / hostname (compose forbids them with
    network_mode) and becomes its network carrier, so _apply_egress_networks
    attaches the posture network to it.

    `anchors` is {anchor: (login_server, minter(svc) -> key|None)}; by default it
    is minted per anchor from _tailnet_egress_anchors(). The legacy
    `authkey_for`/`login_server` pair means a single CLOUD anchor.

    Skipped (left as authored): a service that already sets network_mode or is
    x-mod.reachable (its userspace mesh sidecar owns the netns), and one for which
    no anchor yields a key — the deploy then proceeds exactly as authored."""
    services = spec.get("services")
    if not isinstance(services, dict):
        return spec
    wanted = [n for n, s in services.items()
              if isinstance(s, dict) and _service_needs_tailnet_egress(s)]
    if not wanted:
        return spec
    if anchors is None:
        if authkey_for is not None and login_server is not None:
            anchors = {MESH_ANCHOR_CLOUD: (login_server, authkey_for)}
        else:
            anchors = {a: (login, (lambda c: lambda _svc: _tailnet_egress_authkey(c))(client))
                       for a, (login, client) in _tailnet_egress_anchors().items()}
    # As in _inject_mesh_sidecars: an authored network whose subnet contains the
    # site login server's IP routes the sidecar's enrollment dial to the stack
    # bridge, so the site sidecar would crash-loop forever. Drop the SITE anchor
    # (checked against the login as passed AND as _egress_site_login_server
    # rewrites it — whichever the sidecar actually dials); cloud is unaffected.
    if MESH_ANCHOR_SITE in anchors:
        site_login = anchors[MESH_ANCHOR_SITE][0]
        shadowed = (_site_plane_shadowed(spec, site_login)
                    or _site_plane_shadowed(spec, _egress_site_login_server(site_login)))
        if shadowed:
            net_key, subnet, ip = shadowed
            logger.warning(
                "site tailnet-egress skipped for stack %s: authored network '%s' "
                "(subnet %s) contains the site login server %s, so a site sidecar "
                "could never reach its control plane (the route is shadowed by the "
                "stack's own bridge network). Change that network's subnet to one "
                "that does not include this IP; other anchors are unaffected.",
                name_prefix, net_key, subnet, ip)
            anchors = dict(anchors)
            del anchors[MESH_ANCHOR_SITE]
    if not anchors:
        logger.warning("tailnet egress: no mesh anchor with a control client; "
                       "services %s cannot reach tailnet hosts", wanted)
        return spec
    vols = spec.get("volumes")
    for name in wanted:
        svc = services[name]
        if svc.get("network_mode") or _service_wants_mesh(
                svc, name=name,
                top_xmod=(spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else None)):
            # Only an explicit x-mod.reachable: true (or an authored
            # network_mode) lands here — the service names tailnet hosts it
            # has no route to, so say so loudly.
            logger.warning("tailnet egress: %s already sets network_mode / is reachable; "
                           "not adding %s%s — it cannot dial its tailnet hosts %s",
                           name, TSEGRESS_PREFIX, name,
                           [h for h in (svc.get("x-mod") or {}).get("egress", {}).get("hosts", [])
                            if _is_tailnet_host(h)])
            continue
        keys = {}
        for anchor in (MESH_ANCHOR_SITE, MESH_ANCHOR_CLOUD):
            if anchor in anchors:
                login, mint = anchors[anchor]
                key = mint(name)
                if key:
                    keys[anchor] = (login, key)
                else:
                    logger.warning("tailnet egress: no %s auth key for %s; it cannot "
                                   "reach %s tailnet hosts", anchor, name, anchor)
        if not keys:
            continue
        hostname = f"{name_prefix}-{name}-egress"
        holder = _free_name(services, f"{TSEGRESS_PREFIX}{name}")
        hc = _tsegress_holder()
        hc["hostname"] = hostname
        # The netns owner carries the service's published ports and its authored
        # networks (the egress rewrite replaces them anyway).
        if "ports" in svc:
            hc["ports"] = svc.pop("ports")
        if "networks" in svc:
            hc["networks"] = svc.pop("networks")
        svc.pop("hostname", None)  # compose rejects hostname with network_mode
        labels = svc.get("labels")
        ha = None
        if isinstance(labels, dict):
            ha = labels.get(HA_CLASS_LABEL)
        elif isinstance(labels, list):
            ha = next((l.split("=", 1)[1] for l in labels
                       if isinstance(l, str) and l.startswith(f"{HA_CLASS_LABEL}=")), None)
        if ha:
            hc["labels"] = {HA_CLASS_LABEL: ha}
        services[holder] = hc
        svc["network_mode"] = f"service:{holder}"
        _add_dep(svc, holder)

        def _join(sc: Dict[str, Any]) -> Dict[str, Any]:
            sc["network_mode"] = f"service:{holder}"
            sc["depends_on"] = [holder]
            if ha:
                sc["labels"] = {HA_CLASS_LABEL: ha}
            return sc

        new_vols = []
        if MESH_ANCHOR_SITE in keys:
            login, key = keys[MESH_ANCHOR_SITE]
            site = _free_name(services, f"{TSEGRESS_SITE_PREFIX}{name}")
            # Keep the state volume the site sidecar had as netns owner, so the
            # live tailnet identity survives the move behind the holder.
            new_vols.append(f"{holder}-state")
            # Only the SITE (userspace) sidecar dials the private-CA external control
            # plane, so only it needs the trust bundle. Read fresh: the path is kept
            # current (and the API recreated) by start-main-server / the reconcile
            # watchdog, so a manager move is tracked without a hardcode. Unset →
            # no CA mount (single-anchor / non-dual-home), behaviour unchanged.
            ca_path = os.getenv("MOD_MESH_CA_BUNDLE_HOST_PATH", "").strip() or None
            services[site] = _join(_tsegress_sidecar(hostname, key, login, new_vols[-1],
                                                     kernel=False,
                                                     ca_bundle_host_path=ca_path))
            _add_dep(svc, site)
            env = svc.get("environment")
            pairs = {"MOD_TAILNET_SITE_SOCKS": TAILNET_SITE_SOCKS,
                     "MOD_TAILNET_SITE_PREFIX": TAILNET_SITE_PREFIX}
            if isinstance(env, list):
                env.extend(f"{k}={v}" for k, v in pairs.items())
            else:
                if not isinstance(env, dict):
                    env = {}
                    svc["environment"] = env
                env.update(pairs)
        if MESH_ANCHOR_CLOUD in keys:
            login, key = keys[MESH_ANCHOR_CLOUD]
            cloud = _free_name(services, f"{TSEGRESS_CLOUD_PREFIX}{name}")
            new_vols.append(f"{cloud}-state")
            services[cloud] = _join(_tsegress_sidecar(hostname, key, login, new_vols[-1],
                                                      kernel=True))
            _add_dep(svc, cloud)
        if not isinstance(vols, dict):
            vols = {}
            spec["volumes"] = vols
        for v in new_vols:
            vols.setdefault(v, {})
    return spec


def _mesh_authkey_for(stack: "models.PersistentStack"):
    """Return a per-service auth-key minter for this stack. TODO(mod-connect-
    service-mesh §5): mint against the tenant's bundled Headscale via the control
    client. Until that lands this yields None for every service, so injection is a
    safe no-op and deploy behaviour is unchanged."""
    def _mint(_service_name: str):
        return None
    return _mint


def _reachable_service_names(stack: "models.PersistentStack") -> list:
    _spec = stack.compose_spec or {}
    _top_xmod = _spec.get("x-mod") if isinstance(_spec.get("x-mod"), dict) else None
    return [n for n, s in (_spec.get("services") or {}).items()
            if _service_wants_mesh(s, name=n, top_xmod=_top_xmod)]


def _resolve_stack_secrets(stack: "models.PersistentStack",
                           db: Optional[Session] = None,
                           user=None) -> dict:
    """stack-secrets-and-certs §2 — resolve x-mod.secrets `vault:` refs to values
    at deploy, using the EXISTING API Vault path (the credentials convention):
    read fresh, never cache, never store. Returns {service: {env: value}}.

    Each ref path holds `{value: <secret>}`. Guarded: with no Vault approle
    configured this returns {} for every ref but the one exception below
    (secrets simply unresolved) — same posture as the credentials endpoint. A
    declared ref that resolves to nothing fails the deploy closed, rather than
    starting the service without its secret.

    One exception: the managed-AI bearer token (`_MANAGED_LLM_VAULT_PATH`)
    predates Vault-backed stack secrets and has always had an env-var floor
    (MANAGED_LLM_API_KEY) — it is resolved through `managed_llm_api_key()`,
    the same Vault-first/env-fallback resolver every other reader of that key
    uses, so an operator who has only ever set the env var (never written it
    to Vault, or has no AppRole configured at all) does not have the two
    managed-AI stacks' deploys start failing, or start serving with no auth,
    the moment this ref exists. Every other declared secret stays strictly
    Vault-only, exactly as before."""
    refs = stack.secret_refs if isinstance(stack.secret_refs, dict) else {}
    if not refs:
        return {}
    # GT.D6 DS-3 — managed_ai_stacks is Paid: on CE it is absent. Tenant Vault
    # refs still resolve; a ref at a managed-LLM vault path is a managed-AI
    # stack, which CE cannot serve -> 402 pro_feature.
    from utils.paid_import import optional_paid
    _mas = optional_paid("services.managed_ai_stacks")
    if _mas is not None:
        _MANAGED_LLM_ROLE_PATHS = _mas._MANAGED_LLM_ROLE_PATHS
        _MANAGED_LLM_VAULT_PATH = _mas._MANAGED_LLM_VAULT_PATH
        managed_llm_api_key = _mas.managed_llm_api_key
    else:
        _MANAGED_LLM_VAULT_PATH = "infrastructure/managed-llm/api_key"
        _MANAGED_LLM_ROLE_PATHS = {"reasoner": "infrastructure/managed-llm/reasoner_api_key"}

        def managed_llm_api_key(role="generator"):
            raise HTTPException(
                status_code=402,
                detail="Managed-AI stacks are not available in "
                       "MOD Community Edition.")

    token_state: Dict[str, Any] = {"fetched": False, "token": None}

    def _approle_token():
        if not token_state["fetched"]:
            token_state["fetched"] = True
            role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
            secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
            if role_id and secret_id:
                from utils.vault_client import approle_login, VaultError
                try:
                    token_state["token"] = approle_login(role_id=role_id, secret_id=secret_id)
                except VaultError as e:
                    raise internal_http_error(502, "Secret resolution failed", e) from e
        return token_state["token"]

    from services import stack_tenant_secrets as _ts
    from services.stack_secret_refs import is_named_ref, named_secret_name

    named: Optional[dict] = None
    out: dict = {}
    for svc, envmap in refs.items():
        resolved = {}
        for env, path in (envmap or {}).items():
            # 13.5a — named TENANT secret: resolved through the secrets service
            # with a tenant-scope check (own or shared-with); cross-tenant →
            # 403, missing → 404 (the resolver raises, naming the secret,
            # never the value).
            if _ts.is_named_ref(path):
                if db is None:
                    raise HTTPException(
                        status_code=409,
                        detail=(f"secret {env!r} for service {svc!r} is a named "
                                "tenant secret that cannot be resolved here"))
                if named is None:
                    named = _ts.resolve_stack_tenant_secrets(db, stack)
                value = (named.get(svc) or {}).get(env)
                if value is None:
                    # 13.5f — the only way a named ref is ABSENT from the
                    # resolver's output is the package-declared optional
                    # secret that is missing for the tenant (required
                    # refs raise inside `resolve_stack_tenant_secrets`):
                    # skip it — the env var is simply not injected.
                    continue
                resolved[env] = value
                continue
            # 13.5a — tenant-scope guard on raw vault: paths at deploy. A system
            # deploy (user None) or platform admin is unconstrained; an ordinary
            # tenant user may only reference their own tenant prefix (plus the
            # platform/ and infrastructure/ namespaces the vetted catalog uses).
            if (isinstance(path, str) and user is not None
                    and not _ts._is_platform_admin(user)
                    and not _ts.vault_path_allowed(path, stack.tenant_id, user)):
                raise HTTPException(
                    status_code=403,
                    detail=f"secret {env!r} for service {svc!r} references another tenant's vault path")
            if path == _MANAGED_LLM_VAULT_PATH:
                value = managed_llm_api_key() or None
                if not value:
                    raise HTTPException(
                        status_code=502,
                        detail=f"secret {env!r} for service {svc!r} not found at vault:{path}")
                resolved[env] = value
                continue
            if path == _MANAGED_LLM_ROLE_PATHS["reasoner"]:
                value = managed_llm_api_key("reasoner") or None
                if not value:
                    raise HTTPException(
                        status_code=502,
                        detail=f"secret {env!r} for service {svc!r} not found at vault:{path}")
                resolved[env] = value
                continue
            token = _approle_token()
            if not token:
                continue
            from utils.vault_client import read_kv_v2, VaultError
            try:
                data = read_kv_v2(path, token=token)
            except VaultError as e:
                raise internal_http_error(502, "Secret resolution failed", e) from e
            if not isinstance(data, dict) or "value" not in data:
                raise HTTPException(
                    status_code=502,
                    detail=f"secret {env!r} for service {svc!r} not found at vault:{path}")
            resolved[env] = data["value"]
        if resolved:
            out[svc] = resolved
    return out


def _mesh_name_prefix(stack: "models.PersistentStack") -> str:
    """The stable, collision-proof INTERNAL name prefix — keyed on IDs, NOT the
    tenant's chosen name (caddy-stack-ingress binding architecture): the prefix is
    `t<tenant_id>-<stack_id>` and the per-service MagicDNS / internal name is
    `<prefix>-<service>`. This is like a Kubernetes Service/ClusterIP: a thousand
    tenants all naming a stack "test" get t5-42, t7-91, … so the namespace can
    never collide, the human name being only a label. It is the same address on
    the overlay (a MagicDNS node name) and on a single host (a docker network
    alias), and it follows the stack across workers unchanged.

    Supersedes the earlier name-based convention (`t<tid>-<name>`); because IDs are
    unique this also makes the create-time collision check moot (see
    `_reject_mesh_name_collision`)."""
    return f"t{stack.tenant_id}-{stack.id}"


def _candidate_mesh_names(tenant_id: int, name: str, compose_spec: dict) -> set:
    """Every MagicDNS node name this stack could mint: t<tid>-<stack>-<service>
    for ALL services (not just currently-reachable ones — reachability is a
    per-service toggle, but the name is derived deterministically regardless, so
    the whole namespace must be unambiguous). Used to reject collisions up front."""
    prefix = f"t{tenant_id}-{name}"
    services = (compose_spec or {}).get("services") or {}
    return {f"{prefix}-{svc}" for svc in services}


def _reject_mesh_name_collision(db: Session, tenant_id: int, name: str,
                                compose_spec: dict, exclude_id: Optional[int] = None) -> None:
    """No-op under the ID-based naming convention (caddy-stack-ingress binding
    architecture). The mesh / internal name is now `t<tid>-<stack_id>-<service>`,
    keyed on the stack's unique primary key rather than its human name. The stack
    id is followed by a `-` and contains no `-` itself, so it delimits
    unambiguously; two distinct (stack_id, service) pairs can never concatenate to
    the same node name, and two tenants are already separated by `t<tid>`. Naming
    collisions are therefore impossible by construction, so this check — which the
    old name-based convention needed at CREATE (before an id exists) — has nothing
    to reject and is retained only so its callers stay stable. Kept, not deleted,
    so the mod-connect-service-mesh create paths need no edit."""
    return


def _require_backup_ack(compose_spec: dict, acknowledge_no_backup: bool) -> None:
    """GA.ASW.6 — refuse to create a stack that runs a known-database image
    with no considered backup posture, unless the caller explicitly
    acknowledges it (`acknowledge_no_backup: true` on the create request).

    ONE shared gate for every create path (create_stack, create_from_template,
    import-compose finalize) that persists a `PersistentStack` row — each
    calls this with the FINAL compose_spec that is about to be stored, right
    before the `db.add`. 409 (not 422): the request is well-formed and would
    otherwise succeed; it is a conflict with a policy that a confirmed retry
    resolves, not a validation error the client must fix by changing shape —
    the same distinction `import_compose_finalize`'s duplicate-name check
    already draws with its own 409.
    """
    # GT.D6 DS-3 — the backup engine is Paid: on CE there is no backup posture
    # to acknowledge, so the create gate is a no-op (spec 2c).
    from utils.paid_import import optional_paid
    _lb = optional_paid("services.stack_logical_backup")
    if _lb is None:
        return
    if acknowledge_no_backup:
        return
    services = _lb.backup_ack_required(compose_spec)
    if not services:
        return
    names = ", ".join(f"{s['service']} ({s['image']})" for s in services)
    raise HTTPException(status_code=409, detail={
        "requires_ack": True,
        "reason": (f"{names} — this stack runs a known database image with no "
                   "backup posture declared, so its data is not backed up. "
                   "Retry with acknowledge_no_backup: true to create it anyway."),
        "services": services,
    })


def _ordered_anchor_names(entry: dict) -> list:
    """Anchor names for one service entry in resolution order (site first, cloud
    fallback). Empty for a legacy single-address entry with no `anchors` map."""
    anchors = entry.get("anchors") if isinstance(entry, dict) else None
    if isinstance(anchors, dict) and anchors:
        known = [a for a in MESH_ANCHOR_ORDER if a in anchors]
        extra = [a for a in anchors if a not in MESH_ANCHOR_ORDER]
        return known + extra
    return []


def _stack_mesh_endpoints(stack: "models.PersistentStack") -> dict:
    """Per-service overlay endpoints for reachable services: the deterministic
    MagicDNS name (<stack>-<service>) merged with any stored node_id/status and
    per-anchor addresses. Each entry carries an ordered `addresses` list
    (site-first, cloud-fallback) that a consumer dials via `_connect_with_fallback`
    — dual-anchor §two-address-registration. Empty when the stack has no reachable
    service."""
    stored = stack.mesh_endpoints if isinstance(stack.mesh_endpoints, dict) else {}
    prefix = _mesh_name_prefix(stack)
    out = {}
    for name in _reachable_service_names(stack):
        magic = f"{prefix}-{name}"
        entry = {"magicdns_name": magic}
        s = stored.get(name)
        if isinstance(s, dict):
            for k in ("node_id", "status"):
                if k in s:
                    entry[k] = s[k]
            if isinstance(s.get("anchors"), dict):
                entry["anchors"] = s["anchors"]
        # The ordered address list a consumer tries: site-first, cloud-fallback.
        # Each address is the ONE logical MagicDNS name resolved on that plane. The
        # D6 resolver (group 2) may re-order by live heartbeat, but this static
        # order is correct on its own so the connect-retry always has a fallback.
        ordered = _ordered_anchor_names(entry)
        if ordered:
            entry["addresses"] = [
                {"anchor": a,
                 "magicdns_name": (entry["anchors"][a] or {}).get("magicdns_name", magic),
                 "login_server": (entry["anchors"][a] or {}).get("login_server")}
                for a in ordered
            ]
        else:
            entry["addresses"] = [{"anchor": MESH_ANCHOR_SITE,
                                   "magicdns_name": magic, "login_server": None}]
        out[name] = entry
    return out


def _mesh_service_addresses(entry: dict) -> list:
    """The MagicDNS names to try for one resolved service entry, in order
    (site-first / cloud-fallback). Consumers dial these via
    `_connect_with_fallback`."""
    addrs = entry.get("addresses") if isinstance(entry, dict) else None
    if isinstance(addrs, list) and addrs:
        return [a.get("magicdns_name") for a in addrs if a.get("magicdns_name")]
    name = (entry or {}).get("magicdns_name")
    return [name] if name else []


def _connect_with_fallback(addresses, connect, *, attempts_per_address: int = 2):
    """Bounded connect-retry across a service's addresses (dual-anchor
    §two-address-registration, task 1.2). Try each address in order — site first,
    cloud fallback — up to `attempts_per_address` times; return the first
    successful `connect(addr)` result, else re-raise the last error. Covers the
    transition window where the heartbeat-driven registry is momentarily stale:
    the request still completes over the alternate address."""
    if not addresses:
        raise ValueError("no mesh addresses to connect to")
    last_exc: Optional[Exception] = None
    for addr in addresses:
        for _ in range(max(1, attempts_per_address)):
            try:
                return connect(addr)
            except Exception as e:  # noqa: BLE001 — bounded fall-through to the alternate
                last_exc = e
    assert last_exc is not None
    raise last_exc


def _public_hostname(stack: "models.PersistentStack") -> str:
    """The MOD-run PUBLIC FQDN for an exposed stack (caddy-stack-ingress §2):
    `t<tenant_id>-<stack_id>.apps.<BASE_DOMAIN>`. Keyed on IDs like the internal
    name, so it is collision-proof and wildcard-routed by Caddy — a newly-exposed
    stack needs zero edge config and the name follows the stack across workers.
    BASE_DOMAIN is the platform base domain; when it is unset (dev / single-box)
    the suffix is just `.apps` so the shape is still well-formed."""
    base = os.getenv("BASE_DOMAIN", "").strip().strip(".")
    # The FQDN label must equal a REAL per-service mesh node name (nodes are
    # t<tid>-<stackid>-<svc>), so the Caddy edge can derive the upstream straight
    # from the hostname and dial it — a stack-level t<tid>-<stackid> resolves to
    # nothing. We expose the stack's PRIMARY reachable service (first by name).
    svcs = sorted(_reachable_service_names(stack))
    svc = f"-{svcs[0]}" if svcs else ""
    label = f"t{stack.tenant_id}-{stack.id}{svc}.apps"
    return f"{label}.{base}" if base else label


def _validate_custom_domain(domain: str) -> str:
    """Normalise + sanity-check a BYO custom domain. A hostname only — no scheme,
    path, port, whitespace or wildcard; the tenant proves control out of band
    (CNAME to public_hostname) and the ingress `ask` gate is the runtime guard."""
    d = domain.strip().lower().rstrip(".")
    if not d:
        raise HTTPException(status_code=422, detail="custom_domain must be non-empty")
    if len(d) > 253:
        raise HTTPException(status_code=422, detail="custom_domain too long (max 253)")
    if any(c in d for c in ("/", ":", " ", "*", "\t")) or "://" in domain:
        raise HTTPException(status_code=422,
                            detail="custom_domain must be a bare hostname (no scheme, port, path or wildcard)")
    if "." not in d:
        raise HTTPException(status_code=422,
                            detail="custom_domain must be a fully-qualified hostname")
    return d


def _stack_edge_target() -> str:
    """friendly-stack-fqdn (D4) — the PUBLIC edge hostname a customer points their
    OWN DNS (a CNAME/A in their own zone) at so an exposed stack's friendly FQDN
    resolves to the MOD edge. Purely INFORMATIONAL: MOD provisions no customer DNS
    and holds no customer DNS credentials — this is just the value the UI hands the
    customer to point at.

    Ops sets STACK_INGRESS_EDGE_TARGET to the edge's public name (the caddy edge
    A/AAAA/host record). When it is unset we derive `edge.<BASE_DOMAIN>` from the
    platform base domain; when neither is set (single-box / dev, no public edge)
    the target is empty."""
    target = os.getenv("STACK_INGRESS_EDGE_TARGET", "").strip().strip(".").lower()
    if target:
        return target
    base = os.getenv("BASE_DOMAIN", "").strip().strip(".").lower()
    return f"edge.{base}" if base else ""


def _stack_reachability(stack: "models.PersistentStack", endpoint, is_local: bool,
                        observed_now: bool) -> dict:
    """mod-connect-service-mesh §2.2 / §5.10 — how (and whether) this stack is
    reachable, as pure logic over its resolved state so it can be tested without
    the DB. Reach is either the worker-local `endpoint` (host:port, legacy /
    non-mesh) or the provisioned per-service overlay identity (MagicDNS). A
    reachable service drops its published ports at deploy, so a mesh stack has
    `endpoint is None` and resolves over the overlay.

    "Live" means a worker is reporting the stack up right now (local) or it is a
    remote stack served by its owning manager (trusted). A stack that is running
    yet has no reachable path is the distinct §5.10 state — the fabric/control
    server gone while the containers stay up — not "stopped" and not "failed"."""
    mesh_eps = _stack_mesh_endpoints(stack)
    # Provisioned-ness is the STORED mint record (`stack.mesh_endpoints`), not
    # `_stack_mesh_endpoints` — the latter synthesises a deterministic name for
    # every reachable service whether or not an identity was ever minted, so it
    # cannot tell "reach declared" from "reach actually established".
    stored = stack.mesh_endpoints if isinstance(stack.mesh_endpoints, dict) else {}
    mesh_provisioned = any(
        (e.get("status") or "provisioning") != "revoked"
        for e in stored.values() if isinstance(e, dict)
    ) if stored else False
    live = observed_now if is_local else True
    endpoint_reach = bool(endpoint and live)
    mesh_reach = bool(mesh_provisioned and live)
    reachable = endpoint_reach or mesh_reach
    reach_mode = "mesh" if mesh_reach else ("local" if endpoint_reach else None)
    running_unreachable = bool(
        (getattr(stack, "status", None) == "running" or (is_local and observed_now))
        and not reachable
    )
    return {"mesh_endpoints": mesh_eps, "reachable": reachable,
            "reach_mode": reach_mode, "running_unreachable": running_unreachable}


def _mesh_tenant_user(stack: "models.PersistentStack") -> str:
    return f"tenant-{stack.tenant_id}"


def _provision_mesh_identity(db: Session, stack: "models.PersistentStack"):
    """mod-connect-service-mesh §5.8/§7.1 + dual-anchor §two-address-registration —
    at deploy, mint a preauth key per reachable service on EACH configured anchor
    (site + cloud) and record one logical mesh name carrying an address per anchor.
    Returns (authkey_for, carrier_login_server) for `_compose_yaml`.

    Guarded: with no bundled control server on any plane (or no reachable service)
    this returns the no-op minter and changes nothing — deploy is unmodified, and a
    single-anchor install mints exactly one address per service as before."""
    try:
        from utils.headscale_client import HeadscaleError, default_mint_kwargs
    except ImportError:
        HeadscaleError = Exception  # CE — headscale_client absent; `live` is empty below
        default_mint_kwargs = None
    try:
        from services import mesh_acl
    except ImportError:
        mesh_acl = None  # CE — mesh_acl absent; `live` is empty below
    reachable = _reachable_service_names(stack)
    anchors = _configured_mesh_anchors_with_db(db)
    live = [(n, login, c) for (n, login, c) in anchors if c is not None]
    if not live or not reachable:
        legacy_login = anchors[0][1] if anchors else os.getenv("MOD_TAILNET_LOGIN_SERVER", "")
        return _mesh_authkey_for(stack), legacy_login
    user = _mesh_tenant_user(stack)
    tags = [mesh_acl.tenant_tag(stack), mesh_acl.stack_tag(stack)]
    prefix = _mesh_name_prefix(stack)
    keys: dict = {}          # (service, anchor) -> key
    endpoints: dict = {}
    # GSEC FX.31 p2a — single-use, ~1h TTL (mesh.preauth_key_ttl_minutes);
    # the key is redeemed immediately by the sidecar at container start, so
    # the TTL only bounds "how long can this deploy sit un-started", not the
    # resulting node's session lifetime.
    mint_kwargs = default_mint_kwargs(db) if default_mint_kwargs else {}
    try:
        for anchor_name, login, client in live:
            client.ensure_user(user)
            for svc in reachable:
                keys[(svc, anchor_name)] = client.mint_preauth_key(user, tags, **mint_kwargs)
                entry = endpoints.setdefault(svc, {
                    "magicdns_name": f"{prefix}-{svc}",   # ONE logical name (top-level, back-compat)
                    "status": "provisioning",              # top-level (back-compat)
                    "anchors": {},
                })
                entry["anchors"][anchor_name] = {
                    "magicdns_name": f"{prefix}-{svc}",   # same name resolved on this plane
                    "login_server": login,
                    "status": "provisioning",
                }
    except HeadscaleError as e:
        # A control-server failure must surface as a deploy error, not a stack
        # that starts unreachable (mesh §5.9).
        raise internal_http_error(502, "Mesh identity provisioning failed", e) from e
    stack.mesh_endpoints = endpoints or None
    db.add(stack)
    db.flush()
    # §7.3 — regenerate the whole-tailnet ACL policy on EVERY plane so this stack's
    # posture (and default-deny for everyone else) takes effect on both anchors.
    for _n, _login, client in live:
        mesh_acl.apply_acl_policy(db, client)
    # `_compose_yaml` gates injection on the carrier login and rebuilds the anchor
    # list from `_configured_mesh_anchors()`; the anchor-aware minter supplies the
    # per-(service, anchor) keys.
    carrier_login = live[0][1]
    # Anchor-aware, but callable as the legacy 1-arg minter(svc) too — defaults to
    # the site anchor so existing callers (and _call_minter's arity probe) work.
    minter = lambda s, a=MESH_ANCHOR_SITE: keys.get((s, a))   # noqa: E731
    return minter, carrier_login


def _revoke_mesh_identity(db: Session, stack: "models.PersistentStack") -> None:
    """Teardown / HA re-placement — delete this stack's overlay nodes on every
    anchor so a moved or destroyed service leaves no lingering identity on either
    plane (§7.4). Best-effort: a revoke failure must not block teardown."""
    try:
        from utils.headscale_client import get_headscale_client, HeadscaleError
    except ImportError:
        get_headscale_client = None
        HeadscaleError = Exception  # CE — headscale_client absent; `live` is empty below
    live = [(n, login, c) for (n, login, c) in  _configured_mesh_anchors_with_db(db) if c is not None]
    if not live:
        # Legacy single-client install (env HEADSCALE_URL only, no anchor logins).
        c = get_headscale_client() if get_headscale_client is not None else None
        if c is not None:
            live = [(MESH_ANCHOR_SITE, "", c)]
    if not live:
        return
    _prune_mesh_nodes_for_redeploy(db, stack, live=live)
    stack.mesh_endpoints = None
    # §7.3 — this stack drops out of mesh_endpoints; regenerate the policy without
    # its rules on every plane.
    try:
        from services import mesh_acl
    except ImportError:
        return  # CE — mesh_acl absent; no plane to regenerate
    for _n, _login, client in live:
        mesh_acl.apply_acl_policy(db, client)


def _mesh_live_anchors(db) -> list:
    """The planes a node prune can act on: every configured anchor that has a
    bundled control server, plus the legacy single-client install (env
    HEADSCALE_URL only, no anchor logins) — the same resolution
    `_revoke_mesh_identity` uses. Empty list means "no plane to act on"."""
    try:
        from utils.headscale_client import get_headscale_client
    except ImportError:
        get_headscale_client = None  # CE — headscale_client absent
    live = [(n, login, c) for (n, login, c) in _configured_mesh_anchors_with_db(db)
            if c is not None]
    if not live:
        c = get_headscale_client() if get_headscale_client is not None else None
        if c is not None:
            live = [(MESH_ANCHOR_SITE, "", c)]
    return live


def _prune_mesh_nodes_for_redeploy(db: Session, stack: "models.PersistentStack",
                                   *, live: Optional[list] = None) -> None:
    """BUG-1028h J5 — prune-before-mint: delete THIS stack's own overlay nodes on
    every plane before a re-deploy mints a fresh identity, so a fence resume /
    re-placement loop cannot grow the tailnet node list (minting is not idempotent
    and there is no node-expiry verb — `expire_preauth_key` expires KEYS only).

    Tenant+ID scoped, never hostname-only: the `list_nodes` user filter is
    `_mesh_tenant_user(stack)` and the match set is the ID-keyed
    `t<tenant_id>-<stack_id>-<service>` names, so a node of another tenant or
    another stack can never be touched. Unlike `_revoke_mesh_identity` this does
    NOT clear `stack.mesh_endpoints` (the redeploy is about to re-mint them) and
    does NOT regenerate the ACL policy (the mint re-applies it).

    Fail-open: a per-anchor control-server failure is logged (key-free) and the
    loop continues — a prune must never block a resume."""
    if live is None:
        live = _mesh_live_anchors(db)
    if not live:
        return
    try:
        from utils.headscale_client import HeadscaleError
    except ImportError:
        HeadscaleError = Exception  # CE — headscale_client absent; `live` is empty above
    prefix = _mesh_name_prefix(stack)
    names = {f"{prefix}-{svc}" for svc in _reachable_service_names(stack)}
    if not names:
        return
    for anchor_name, _login, client in live:
        try:
            for node in client.list_nodes(_mesh_tenant_user(stack)):
                if node.get("name") in names or node.get("givenName") in names:
                    nid = node.get("id") or node.get("nodeId")
                    if nid:
                        client.delete_node(str(nid))
        except HeadscaleError as e:
            logger.warning("mesh prune failed for stack %s on anchor %s: %s",
                           stack.id, anchor_name, e)


# gw-stack-networking GW.1 — host ports are MOD-owned at render time too, not
# just at import. A stack whose stored compose_spec carries `x-mod.ports`
# (every stack imported/created since this change) renders NO `ports:` on any
# service except what `x-mod.host_ports` explicitly requests; a `ports:` key
# stored directly on a service (hand-edited, or created before this change —
# `x-mod.ports` absent) is left byte-for-byte alone, so a legacy stack's
# deploy does not silently change shape underneath it.
# BUG.B62 — the login gate a stack's ingress enforces (x-mod.ingress.auth).
# Read straight off the spec so the port renderer needs no DB and no extra
# import: `services.stack_forward_auth.stack_auth_config` is the DB-side twin.
# An unreadable block means "public stack" (same fail-open the ingress uses).
def _xmod_requires_login(xmod: Dict[str, Any]) -> bool:
    ingress = xmod.get("ingress")
    if not isinstance(ingress, dict):
        return False
    auth = ingress.get("auth")
    if not isinstance(auth, dict):
        return False
    return bool(auth.get("require_login", False))


def _render_stack_owned_ports(spec: Dict[str, Any]) -> None:
    top_xmod = spec.get("x-mod")
    if not isinstance(top_xmod, dict):
        return  # no x-mod at all — leave byte-for-byte
    has_ports = "ports" in top_xmod
    has_host_ports = "host_ports" in top_xmod
    if not has_ports and not has_host_ports:
        return  # no port declaration at all — leave byte-for-byte
    services = spec.get("services")
    if not isinstance(services, dict):
        return
    # BUG.B62 — a login-gated stack's web is front-gated by MOD's caddy
    # forward_auth, but ONLY for traffic that comes through the site host. A
    # host port published on the wildcard interface (0.0.0.0) is reachable
    # straight off the LAN, unauthenticated, bypassing that gate entirely
    # (203.0.113.243:20002/api/health = 200 on stack 114). So for a stack whose
    # ingress requires login, every published host port binds 127.0.0.1 — still
    # reachable by the front (host.docker.internal), never from the LAN. A stack
    # with no login gate keeps 0.0.0.0: there the LAN exposure is deliberate.
    gated = _xmod_requires_login(top_xmod)
    by_service: Dict[str, List[str]] = {}
    for entry in (top_xmod.get("host_ports") or []):
        if not isinstance(entry, dict):
            continue
        svc_name = entry.get("service")
        target = entry.get("target")
        if svc_name not in services or target is None:
            continue
        protocol = entry.get("protocol") or "tcp"
        host = entry.get("host")
        bind = str(entry.get("bind") or "").strip()
        if gated and (not bind or bind == "0.0.0.0"):
            # Stamp it back onto the entry, not just into the rendered string:
            # the `any` branch below renders NO host side, and the worker's
            # GW.2 allocator (publishForm) reads `bind` off the entry to decide
            # which interface its allocated port pins — that is the path that
            # produced stack 114's 0.0.0.0:20002->8080.
            bind = "127.0.0.1"
            entry["bind"] = bind
        if host == "any":
            # GW.2 (worker, Go) allocates a free host port from the range the
            # API pushes and reports the chosen mapping back. Until that
            # ships, a container-only short-syntax publish lets Docker pick
            # an ephemeral host port on its own.
            # TODO(GW.2): render the worker-managed range allocation instead.
            published = f"{target}/{protocol}"
        else:
            published = f"{bind or '0.0.0.0'}:{host}:{target}/{protocol}"
        by_service.setdefault(svc_name, []).append(published)
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        svc.pop("ports", None)
        published = by_service.get(svc_name)
        if published:
            svc["ports"] = published


def _move_netns_shared_ports(spec: Dict[str, Any]) -> None:
    """BUG.B76 — Docker refuses `ports:` on a container that shares another
    container's network namespace ("conflicting options: port publishing and
    the container type network mode"), so a service that landed on a netns
    holder (`network_mode: service:<holder>`) can never publish its own host
    ports. The holder OWNS the namespace, so a publish there reaches the served
    container's sockets — move every such service's `ports:` onto its holder
    (merge, de-dupe) and strip its own. `expose:` stays: it is namespace-shared
    metadata, not a host bind, and compose accepts it alongside network_mode.

    Runs as the LAST step of the render (after `_render_stack_owned_ports`, the
    mesh/tailnet-egress injections and the egress rewrite) so it sees the final
    shape whichever of them produced it, and is idempotent: a service already
    carrying no ports is untouched, and a holder that already lists a publish
    never gets it duplicated. A `service:` carrier that is not a service of this
    spec (an authored external carrier) and a non-`service:` mode (`host`,
    `none`, `private`) are left exactly as authored — only the netns-holder
    shape is rewritten. The bind (127.0.0.1 for a login-gated stack, BUG.B62)
    is carried verbatim, never re-derived."""
    services = spec.get("services")
    if not isinstance(services, dict):
        return
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        published = svc.get("ports")
        if not isinstance(published, list) or not published:
            continue
        nm = svc.get("network_mode")
        if not (isinstance(nm, str) and nm.startswith("service:")):
            continue
        holder = services.get(nm.split(":", 1)[1])
        if not isinstance(holder, dict):
            continue  # carrier is not one of this spec's services — as authored
        carried = holder.get("ports")
        if not isinstance(carried, list):
            carried = []
        for entry in published:
            if entry not in carried:
                carried.append(entry)
        holder["ports"] = carried
        svc.pop("ports", None)


# --- GX.S10a — stack variables + secrets (worker capability gate) ------------
# The `file_secrets` heartbeat capability (set by the worker release that
# ships GX.S10b) is authoritative when the worker ever reported it; older
# binaries never send it, so fall back to the agent-version gate.
FILE_SECRETS_MIN_VERSION = "v1.0.75"


def _version_tuple(v: Optional[str]):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)


def _worker_supports_file_secrets(worker: Optional[models.WorkerLicense]) -> bool:
    if worker is None:
        # No named target (claim-time placement): cannot know the worker, so
        # allow dispatch; the claim-time guard + worker failure surface the
        # gap. Only a NAMED/pinned target gets the hard 409 gate.
        return True
    specs = worker.hardware_specs if isinstance(worker.hardware_specs, dict) else {}
    if isinstance(specs.get("file_secrets"), bool):
        return specs["file_secrets"]
    return _version_tuple(getattr(worker, "agent_version", None)) >= \
        _version_tuple(FILE_SECRETS_MIN_VERSION)


def _require_worker_file_secrets(worker: Optional[models.WorkerLicense]) -> None:
    """409 when a secret_file stack would be placed on a worker that cannot
    materialise file secrets (contract §3)."""
    if not _worker_supports_file_secrets(worker):
        specs = worker.hardware_specs if (worker is not None and isinstance(worker.hardware_specs, dict)) else {}
        if specs.get("file_secrets") is False:
            detail = (f"worker {getattr(worker, 'worker_name', '?')} reports it cannot "
                      f"materialise file secrets (heartbeat file_secrets=false: the "
                      f"/run/mod-secrets tmpfs same-path bind is missing or failed its "
                      f"daemon round-trip probe)")
        else:
            detail = (f"worker v{FILE_SECRETS_MIN_VERSION[1:]}+ required for file "
                      f"secrets: {getattr(worker, 'worker_name', '?')} reports "
                      f"{getattr(worker, 'agent_version', 'unknown')}")
        raise HTTPException(status_code=409, detail=detail)


def _stack_wants_file_materialisation(stack: models.PersistentStack) -> bool:
    from services import stack_variables as _sv
    declared = _sv.declared_variables(stack.compose_spec or {})
    return any(isinstance(e, dict) and e.get("kind") in ("secret_file", "storage_file")
               for e in declared.values())


def _container_logging_policy(db=None) -> tuple:
    """Read the FX.57 container-log size policy. Returns ``(max_size, max_files)``
    from the ``logging.container_max_size`` / ``logging.container_max_files``
    platform settings, falling back to the 50m/5 defaults if either read fails
    (a broken setting must never break a deploy render)."""
    max_size, max_files = "50m", 5
    try:
        from services import platform_settings as _psr
        max_size = _psr.get_setting("logging.container_max_size", db)
    except Exception:  # noqa: BLE001 — degrade to the default, never raise
        max_size = "50m"
    try:
        from services import platform_settings as _psr
        max_files = _psr.get_setting("logging.container_max_files", db)
    except Exception:  # noqa: BLE001
        max_files = 5
    return (str(max_size), int(max_files))


def _stamp_container_logging(spec: Dict[str, Any],
                             policy: Optional[tuple] = None) -> None:
    """FX.57 part 3 — bound every stack service's on-disk log. The worker's
    json-file driver has no size cap by default, so an unbounded stack could
    fill the worker disk. Stamp a ``logging:`` block (json-file, capped) onto
    each service that does not already declare its own. Pure dict in/out so it
    is unit-testable without a DB; ``policy=(max_size, max_files)`` defaults to
    50m/5. Time-based retention is the nightly system-log archive job's concern,
    not these size options (see docs/runbooks/container-log-rotation.md)."""
    if policy is None:
        policy = ("50m", 5)
    max_size, max_files = policy
    for svc in (spec.get("services") or {}).values():
        if not isinstance(svc, dict) or "logging" in svc:
            continue
        svc["logging"] = {
            "driver": "json-file",
            "options": {"max-size": str(max_size), "max-file": str(max_files)},
        }


def _stamp_restart_policy(spec: Dict[str, Any]) -> None:
    """BUG.B82 — give every long-running stack service a crash-restart policy.

    A stack service that declares no `restart:` gets the Docker default,
    `restart: no`: the container that exits — for any reason — stays Exited
    forever and nothing brings it back. 10-09 04:25, stack 114 ("consign"): on
    a redeploy the `web` container (nginx proxying to `api:8000`) started while
    the api container was being recreated, died with `host not found in
    upstream "api"`, and stayed Exited (`docker inspect` RestartPolicy=no) while
    stack status still said running — the app was down until the operator ran
    `docker start` by hand. `unless-stopped` re-starts it on its own (and after
    the api container finishes being recreated) without overriding an explicit
    stop.

    Only services that declare NOTHING get the default: a service that authored
    its own `restart:` (any value, including `no`) keeps it, and a one-shot job
    is left alone. The module has no job marker of its own — the stack-34
    `ollama-pull` case in `_service_wants_mesh` rule 2 identifies jobs by
    publishing nothing (no `ports`/`expose`, no stack-level endpoint) — so a
    service is treated as one-shot when it declares `restart: "no"` /
    `restart: false` or sets `x-mod.job: true` (the marker the module honours
    if an author ever adds one), or when another service depends on it with
    `condition: service_completed_successfully` (F-1: it then renders `no`). A portless service that is neither marked
    (e.g. a sleep-loop holder) still gets the policy: with no endpoint there is
    nothing to mesh, so it stays exactly as authored apart from the default.

    Stamped at dispatch, after every injection step, so it also covers the
    mesh/tailnet holders + sidecars and the ingress forwarder rather than
    relying on each injection remembering to set it. Pure dict in/out, idempotent.
    """
    worker_start = stack_worker_start_enabled()
    completion_targets = _completion_dependency_targets(spec)
    for name, svc in (spec.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        declared = svc.get("restart", None)
        xmod = svc.get("x-mod")
        if declared is not None and declared != "":
            intent = "no" if declared is False else str(declared)  # author's choice (incl. `no`)
        elif isinstance(xmod, dict) and xmod.get("job") is True:
            intent = "no"  # an explicit one-shot marker: never loop a job
        elif name in completion_targets:
            # F-1 (GOLD3 11-20): another service waits for this one with
            # `condition: service_completed_successfully`, so it is a one-shot
            # init job by construction. unless-stopped would restart it after
            # its exit 0 forever, the dependant would stay `Created` and the
            # stack `deploying`. Render Docker's `no` explicitly.
            intent = "no"
            svc["restart"] = "no"
        else:
            # The platform default rendered below (Docker policy
            # unless-stopped). The loop's intent is `default` (review2 M4): a
            # crash or a host-shutdown stop is restarted, a clean exit 0 after
            # boot (an init/migration job) stays down and the stack stays
            # `running` (stackloop.go containerWantsRunning).
            intent = "default"
        if declared is None or declared == "":
            if intent != "no":
                svc["restart"] = "unless-stopped"
        if not worker_start:
            continue
        # Worker-controlled start (CE default, MOD_STACK_WORKER_START=1). The
        # render keeps the Docker policy exactly as above (owner 11-20: the WORKER
        # owns the policy; a worker without the loop, remote or paid, keeps
        # Docker restarts). A loop worker records each container's original
        # policy (this label, or its intent file for unlabelled containers),
        # switches the container to `no` itself after each deploy/start and at
        # worker start (Worker/internal/executor/stackloop.go), and restarts it
        # with Docker's own semantics for that policy.
        labels = svc.get("labels")
        if isinstance(labels, list):
            labels = [x for x in labels if not str(x).startswith(STACK_RESTART_INTENT_LABEL + "=")]
            labels.append(f"{STACK_RESTART_INTENT_LABEL}={intent}")
            svc["labels"] = labels
        else:
            labels = dict(labels) if isinstance(labels, dict) else {}
            labels[STACK_RESTART_INTENT_LABEL] = intent
            svc["labels"] = labels


def _completion_dependency_targets(spec: Dict[str, Any]) -> set:
    """Names of services some other service depends on with
    `condition: service_completed_successfully` (long `depends_on` form only;
    the short list form has no condition and marks nothing)."""
    targets = set()
    for svc in (spec.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        deps = svc.get("depends_on")
        if not isinstance(deps, dict):
            continue
        for dep_name, dep in deps.items():
            if isinstance(dep, dict) and dep.get("condition") == "service_completed_successfully":
                targets.add(str(dep_name))
    return targets


# Label carrying a stack service's intended restart policy when the worker,
# not Docker, restarts stacks (Worker/internal/executor/stackloop.go).
STACK_RESTART_INTENT_LABEL = "com.example.restart"


def stack_worker_start_enabled() -> bool:
    """True when stacks are started/restarted by the worker loop instead of
    Docker (the CE overlay sets MOD_STACK_WORKER_START=1 on api + worker).
    Unset = Docker restart policies as before (paid unchanged)."""
    return (os.environ.get("MOD_STACK_WORKER_START") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _compose_yaml_with_vars(stack: models.PersistentStack, authkey_for=None,
                            login_server: Optional[str] = None,
                            secret_env: Optional[dict] = None,
                            db: Optional[Session] = None,
                            variables_bundle: Optional[dict] = None,
                            worker: Optional[models.WorkerLicense] = None) -> tuple:
    """The YAML the worker writes to docker-compose.yml, with `ha_class`
    stamped onto every service as a label.

    Fencing is the reason. A worker that loses its manager stops every stack it
    hosts, which is right for data and wrong for everything else: on 2026-08-13
    it stopped a model server three times because the API takes longer to
    restart than the 120s fence lease. The worker cannot ask the database what
    a project is, so the classification has to travel WITH the stack — and a
    compose label is the one channel it already reads back off a running
    container.

    Stamped here rather than in `definitions()` because ha_class belongs to the
    stack ROW and can be changed by an operator after the spec was generated;
    materialising it at dispatch means the label always matches the current
    classification rather than whatever it was when the spec was written.
    """
    spec = copy.deepcopy(stack.compose_spec or {})
    _render_stack_owned_ports(spec)
    # GSEC FX.57 part 3 — bound every service's on-disk container log (json-file
    # has no size cap by default). Stamped at dispatch, not in `definitions()`,
    # so the size policy always matches the current platform settings.
    _stamp_container_logging(spec, _container_logging_policy(db))
    ha = getattr(stack, "ha_class", None)
    if ha:
        for svc in (spec.get("services") or {}).values():
            if isinstance(svc, dict):
                labels = svc.setdefault("labels", {})
                if isinstance(labels, dict):
                    labels[HA_CLASS_LABEL] = str(ha)
                elif isinstance(labels, list):
                    labels.append(f"{HA_CLASS_LABEL}={ha}")
    # mod-connect-service-mesh §3 + dual-anchor §two-address-registration — inject
    # per-service tailnet sidecars (one per anchor) on the copy handed to the
    # worker. Guarded: only when a login server is configured and some service
    # opts in; the minter is a no-op until §5 provisioning has run, so this changes
    # nothing on a reconcile/no-mint path.
    login = login_server if login_server is not None else _anchor_login_server_with_db(MESH_ANCHOR_SITE, db)
    minter = authkey_for if authkey_for is not None else _mesh_authkey_for(stack)
    anchors = [(n, l) for (n, l, _c) in  _configured_mesh_anchors_with_db(db)]
    if not anchors and login:
        anchors = [(MESH_ANCHOR_SITE, login)]
    _top_xmod = spec.get("x-mod") if isinstance(spec.get("x-mod"), dict) else None
    _mesh_wanted = {n: _service_wants_mesh(s, name=n, top_xmod=_top_xmod)
                    for n, s in (spec.get("services") or {}).items()}
    if anchors and not any(_mesh_wanted.values()):
        logger.warning("stack %s: mesh skipped — no service declares ports/expose/ingress "
                       "or x-mod.reachable", getattr(stack, "name", None) or _mesh_name_prefix(stack))
    if anchors and any(_mesh_wanted.values()):
        _inject_mesh_sidecars(spec, _mesh_name_prefix(stack), minter,
                              anchors[0][1], anchors=anchors)
    # Tailnet egress — a `hosts` service naming a 100.64/10 peer rides per-anchor
    # tailscale sidecars so it can actually dial it. Before the egress
    # rewrite so the sidecar is attached as the service's network carrier.
    if any(_service_needs_tailnet_egress(s)
           for s in (spec.get("services") or {}).values()):
        _inject_tailnet_egress(spec, _mesh_name_prefix(stack))
    # delegated-stack-authoring §5 — realise egress postures as network attachments,
    # AFTER mesh injection so a mesh-backed service is routed to its sidecar carrier.
    # No-op unless the paid egress enforcement layer is present and enabled
    # (see _apply_egress_networks).
    #
    # `_apply_egress_networks` raises `ValueError` on a 'mesh-only' service with
    # no mesh sidecar (its own unit tests assert that convention directly). Every
    # caller of `_compose_yaml` reaches this from an operator-triggered deploy
    # (HTTP endpoint or the managed-ai-apply loop, which already only catches
    # `HTTPException`), so an uncaught `ValueError` here surfaced as a bare 500
    # instead of the 422 every other validation failure in this file returns.
    # This is the one choke point every `_compose_yaml` caller shares, so
    # converting here fixes all of them without changing the lower-level
    # function's own tested exception type.
    try:
        _apply_egress_networks(spec, db, getattr(stack, "tenant_id", None))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # BUG.B76 — the netns move above (mesh holder / tailnet-egress holder) leaves
    # a service with `network_mode: service:<holder>`; Docker refuses `ports:` on
    # such a container, so any publish still sitting on it (GW.1's render, or an
    # authored `ports:`) is moved onto its holder here — LAST, so it sees the
    # final shape whichever injection produced the netns and cannot be undone by
    # a later step.
    _move_netns_shared_ports(spec)
    # BUG.B82 — every long-running service without an authored `restart:` gets
    # `unless-stopped` (the Docker default is `no`, which leaves a container that
    # died on a redeploy Exited forever). LAST so it also covers the mesh/tailnet
    # holders + sidecars and the ingress forwarder, not just authored services.
    _stamp_restart_policy(spec)
    # 13.5a — file-type named tenant secrets travel in `secret_env` (resolved by
    # `_resolve_stack_secrets`) but must NOT be injected as env vars: pop them
    # out of the env maps and keep them for the worker file-secret channel
    # below. Env-type named refs and vault: paths keep the existing env path.
    from services import stack_tenant_secrets as _sts
    _file_by_svc = _sts.file_secret_refs(stack)
    _file_vals: dict = {}
    if _file_by_svc and secret_env:
        for _svc_name, _files in _file_by_svc.items():
            _em = secret_env.get(_svc_name)
            if isinstance(_em, dict):
                for _sname in _files:
                    if _sname in _em:
                        _file_vals.setdefault(_svc_name, {})[_sname] = _em.pop(_sname)
    # stack-secrets-and-certs §2 — inject resolved secrets as env on the worker's
    # copy only; the stored spec keeps just the vault: refs. Fresh per deploy.
    for svc_name, envmap in (secret_env or {}).items():
        svc = (spec.get("services") or {}).get(svc_name)
        if isinstance(svc, dict) and envmap:
            env = svc.get("environment")
            if isinstance(env, list):
                env.extend(f"{k}={v}" for k, v in envmap.items())
            else:
                if not isinstance(env, dict):
                    env = {}
                    svc["environment"] = env
                env.update(envmap)
    # GX.S10a — resolve x-mod.variables on the worker copy ONLY (the stored
    # spec keeps placeholders/refs). Fails closed, naming the variable, when a
    # value cannot be resolved. The returned bundle's secret_files /
    # storage_files ride on the task payload for the worker to materialise.
    from services import stack_variables as _sv
    if variables_bundle is None:
        variables_bundle = _sv.resolve_variables(db, stack)
    # 13.5a — file-type named tenant secrets: materialise them on the SAME
    # worker file-secret channel GX.S10a's x-mod.variables use — a top-level
    # compose `secrets:` entry + a per-service read-only mount, with the value
    # base64'd into the task payload's `secret_files` (13.5b lands the
    # worker-side handling; the payload shape is already payload-ready). The
    # value rides ONLY in the task payload — never the compose text, logs, or
    # the stored spec.
    if _file_vals:
        import base64 as _b64
        for svc_name, files in _file_by_svc.items():
            svc = (spec.get("services") or {}).get(svc_name)
            if not isinstance(svc, dict):
                continue
            svc_secrets = svc.get("secrets")
            if not isinstance(svc_secrets, list):
                svc_secrets = []
                svc["secrets"] = svc_secrets
            for sname, target in files.items():
                value = (_file_vals.get(svc_name) or {}).get(sname)
                if value is None:
                    continue
                tgt = target or f"/run/secrets/{sname.lower()}"
                spec.setdefault("secrets", {})[sname] = {
                    "file": f"${{MOD_SECRETS_DIR}}/{sname}"}
                svc_secrets.append({"source": sname, "target": tgt, "mode": 0o400})
                variables_bundle.setdefault("secret_files", []).append({
                    "name": sname,
                    "content_b64": _b64.b64encode(str(value).encode("utf-8")).decode("ascii"),
                    "mode": 0o400,
                })
    # GX.S3 — stack identity: append the auto-injected, short-lived token as a
    # file secret (mod_identity_token) mounted in EVERY service + the MOD_API_URL
    # / MOD_IDENTITY_TOKEN_FILE env vars. Skips quietly (logging the stack id
    # only) when the platform setting is off or the named target worker lacks
    # the file-secret capability. The token value never enters the compose text.
    from services import stack_identity as _si
    _si.apply_stack_identity(db, stack, variables_bundle, spec, worker=worker)
    # Render on ANY resolved value, not just file mounts — gating on mounts alone
    # shipped secret_env / plain placeholders to the worker verbatim (10-02: the
    # switchyard got the literal "<STRATA_API_KEY>" and Strata answered 401).
    if variables_bundle.get("resolved") or variables_bundle.get("mounts"):
        _sv.render_worker_spec(spec, variables_bundle)
    return yaml.safe_dump(spec, default_flow_style=False, sort_keys=False), variables_bundle


def _compose_yaml(stack: models.PersistentStack, authkey_for=None,
                  login_server: Optional[str] = None,
                  secret_env: Optional[dict] = None,
                  db: Optional[Session] = None,
                  variables_bundle: Optional[dict] = None,
                  worker: Optional[models.WorkerLicense] = None) -> str:
    """Pre-GX.S10a shape: the rendered worker compose text only.

    Deploy call sites use `_compose_yaml_with_vars` so they can pass the
    resolved variables bundle's `secret_files` / `storage_files` onto the
    task payload; this wrapper keeps the historical str-only contract for
    callers (and tests) that don't need the bundle.
    """
    yaml_text, _bundle = _compose_yaml_with_vars(stack, authkey_for=authkey_for,
                                                 login_server=login_server,
                                                 secret_env=secret_env, db=db,
                                                 variables_bundle=variables_bundle,
                                                 worker=worker)
    return yaml_text


def _pick_worker(db: Session, home_zone: Optional[str]) -> Optional[models.WorkerLicense]:
    from services import persistent_stacks_shared_service as _svc
    return _svc.pick_worker(db, home_zone)


# Teardown is the one stack op worth retrying automatically. A failed deploy
# should surface to the operator (the compose spec is probably wrong, and
# re-running it just fails again), but a failed `compose down` usually means a
# transient daemon/lock condition — and leaving it un-retried strands
# containers and volumes on the host. The generic requeue path in
# worker_tasks.report_task_failed handles the retry; all we do is give teardown
# a budget greater than 1.
# `logs` is read-only and answers a human waiting on it: one attempt, because a
# retry only delays the answer, and "the host did not respond" is information.
_STACK_OP_MAX_ATTEMPTS = {"teardown": 3, "logs": 1}


# GF.7a (launch-completion-megaplan) — frozen package weights for a
# package-installed stack. GF.12 links an installed tenant stack back to its
# package as ``TenantStackEntitlement(installed_stack_id=stack.id)``
# (``listing_id -> StackListing.package_id -> StackPackage``); this helper
# walks that chain and reshapes the package's frozen weight manifests into
# the worker's ``WeightRef`` list (``artifact_ref`` / ``repo_id`` /
# ``revision`` / ``files[{path, sha256, size}]`` — see
# Worker/internal/transport WeightRef). The frozen entries carry NO
# credentials: the worker fetches the baked weights image with the Harbor
# creds it already pulls from /workers/tasks/{id}/credentials (its
# PickRegistryAuth picks the entry by the artifact_ref host).
#
# GF.7b reuses this for migrate payloads. No package link, no frozen
# weights, or malformed frozen data -> None, so the dispatch payload is
# unchanged.
#
# GF.19b (tasks.md 12.3 "an unentitled tenant is refused at enqueue and at the
# registry") — the entitlement is re-checked HERE, not just at install time:
# only a row that is still entitled for THIS stack's tenant contributes. A
# soft-revoked (``expires_at`` set and past) row and a row belonging to another
# tenant that happens to point at this stack id both fail closed (no refs, so
# no ``weights`` key and no other tenant's package refs on the payload).
def _stack_package_weights(db: Session, stack_id: Optional[int]) -> Optional[list]:
    if stack_id is None:
        return None
    stack = db.get(models.PersistentStack, stack_id)
    if stack is None:
        return None
    now = datetime.now(timezone.utc)
    ents = (db.query(models.TenantStackEntitlement)
            .filter(models.TenantStackEntitlement.installed_stack_id == stack_id,
                    models.TenantStackEntitlement.deleted_at.is_(None))
            .all())
    # One live entitlement normally points at the stack (GF.12 installs each
    # listing as its own stack); when more than one ever does, take the first
    # whose package actually carries frozen weights.
    for ent in ents:
        # GF.19b tenant isolation: an entitlement of another tenant must not
        # put this stack's package weights on the payload (nor leak that
        # tenant's refs) — the stack's own tenant is the only one entitled.
        if ent.tenant_id != stack.tenant_id:
            continue
        # GF.19b expiry: same soft-revocation convention as permissions.py's
        # grant check (expires_at set and <= now => not entitled). The column
        # is timezone-aware; a naive value is read as UTC so the comparison
        # never raises.
        if ent.expires_at is not None:
            expires_at = ent.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= now:
                continue
        listing = db.get(models.StackListing, ent.listing_id)
        if listing is None or listing.deleted_at is not None:
            continue
        pkg = db.get(models.StackPackage, listing.package_id)
        if pkg is None or pkg.deleted_at is not None:
            continue
        refs = _stack_package_weight_refs(pkg)
        if refs:
            return refs
    return None


def _stack_package_weight_refs(pkg: "models.StackPackage") -> Optional[list]:
    """Reshape one package's frozen weights into the worker WeightRef list;
    None when the package carries none (or only malformed entries)."""
    # frozen_spec is the GF.10b home for the frozen build data; the
    # `weights` column carries the same shape and covers packages frozen
    # before frozen_spec existed.
    spec = pkg.frozen_spec if isinstance(pkg.frozen_spec, dict) else {}
    weights = spec.get("weights")
    if weights is None:
        weights = pkg.weights
    if not isinstance(weights, dict):
        return None
    # GT.D6 DS-3 — weights_registry is Paid: on CE there is no weight mirror,
    # so store-bought CE stacks must use resolvable images (# CE-safe).
    from utils.paid_import import optional_paid
    wr = optional_paid("services.weights_registry")
    if wr is None:
        return None
    from utils import registries
    registry = registries.build_push_registry()
    refs = []
    for entry in weights.values():
        if not isinstance(entry, dict):
            continue
        files = []
        for f in entry.get("files") or []:
            if not isinstance(f, dict):
                continue
            path = f.get("path")
            sha256 = f.get("sha256")
            size = f.get("size")
            if (not isinstance(path, str) or not isinstance(sha256, str)
                    or not isinstance(size, int) or isinstance(size, bool)):
                continue
            files.append({"path": path, "sha256": sha256, "size": size})
        repo_id = entry.get("repo")
        revision = entry.get("revision") or "main"
        if (not isinstance(repo_id, str) or not repo_id
                or not isinstance(revision, str) or not revision or not files):
            continue
        try:
            ref = wr.artifact_ref(registry, repo_id, revision)
        except ValueError:
            logger.warning("stack package %s: skipping unresolvable weight "
                           "ref %s@%s", pkg.id, repo_id, revision)
            continue
        # sz-gf-3 — additive per-file cache refs (modcache://sha256/<hash> +
        # the cache object key) next to the legacy artifact_ref: a worker
        # that understands them pulls straight from the model cache's
        # content-addressed store, one store for all weights; an older
        # worker ignores the extra keys and keeps using artifact_ref.
        files = wr.resolve_weight_refs(files)
        refs.append({
            "artifact_ref": ref,
            "repo_id": repo_id,
            "revision": revision,
            "files": files,
        })
    return refs or None


def _dispatch_stack_task(db: Session, worker_name: Optional[str], tenant_id: int,
                         stack_op: str, project: str, compose_yaml: Optional[str] = None,
                         stack_id: Optional[int] = None,
                         accel_demand: Optional[dict] = None,
                         extra_payload: Optional[dict] = None) -> models.ApiTaskQueue:
    """Enqueue a stack lifecycle op as an ordinary PLATFORM_TASK.

    `stack_id` is carried on the payload so the completion handler can resolve
    the owning stack exactly. Reverse-mapping from `stack_project` was the
    alternative and is fragile: the fallback project name is generated in two
    places and `storage_refs["project"]` only exists after a deploy has run.

    `accel_demand` is the aggregate the host must satisfy whole (see
    _aggregate_accelerator_demand). It rides on the payload the same way
    `gpu_variants` rides on node tasks, and is evaluated by the claim-time
    guard in worker_tasks._try_claim_task — the platform checks capability
    just-in-time against what a worker reports at the moment it claims, and
    never precomputes a placement.

    `extra_payload` merges additional op-specific fields onto the payload (e.g.
    `mesh_reup`'s `mesh_service`/`mesh_login_server`/`mesh_authkey`/
    `mesh_hostname` — see Worker/internal/transport TaskPayload). Applied last,
    after the standard fields, so a caller can't be shadowed by them; callers
    are responsible for not colliding with the reserved keys above.

    `worker_name` None ⇒ the task is born PENDING and unpinned, and whichever
    capable worker claims it gets the work. That is how node tasks behave, and
    it is what makes the accelerator guard able to actually SELECT a host
    rather than merely veto a host someone already picked. A named worker still
    pins (targeted ops like teardown, which must run where the containers are).
    """
    # Worker-controlled start (H4, review 11-20): an operator stop/teardown/
    # migrate or a fresh deploy supersedes any boot_restore still queued for the
    # stack, so a restore can never start containers after the operator stopped
    # them. No-op when no boot_restore rows exist (paid unchanged).
    if stack_id is not None and stack_op in ("stop", "teardown", "migrate", "deploy"):
        from services.stack_secret_recovery import cancel_pending_boot_restores
        cancel_pending_boot_restores(db, stack_id, f"superseded by {stack_op}")
    # k8s-reduction-engine W15/W18: MOD_STACK_BACKEND=k8s records an in-flight
    # (RUNNING, SYSTEM-queue, never worker-claimable) task row here and runs the
    # K8s op after this transaction commits, off the request thread — never a
    # blocking K8s API call inside the caller's open DB txn. Docker mode falls through.
    from services import stack_backend_dispatch as _sbd
    _k8s_task = _sbd.schedule_stack_op(db, tenant_id=tenant_id, stack_op=stack_op,
                                       project=project, compose_yaml=compose_yaml,
                                       stack_id=stack_id)
    if _k8s_task is not _sbd.NotHandled:
        return _k8s_task
    payload = {
        "stack_op": stack_op,
        "stack_project": project,
        "container_env": {"PLATFORM_TASK": "1"},
        "node_kind": f"persistent-stack-{stack_op}",
    }
    # 27.6.s2 — a `bench` sweep's budget knob rides in container_env, which is
    # how every worker tunable reaches the worker (stack_bench.go benchConfig
    # reads BENCH_TOTAL_BUDGET_S; no new env vars invented). Every other sweep
    # knob keeps the worker's own default.
    if stack_op == "bench":
        # 1788d CE carve: services/stack_bench_profile.py is paid (manifest deny
        # 1115). The bench sweep's budget knob is a paid tunable; with it absent
        # the sweep runs on the worker's own defaults (no new env vars).
        from utils.paid_import import optional_paid
        _sbp = optional_paid("services.stack_bench_profile")
        if _sbp is not None:
            payload["container_env"].update(_sbp.bench_container_env(db))
    if stack_id is not None:
        payload["stack_id"] = stack_id
    if compose_yaml is not None:
        payload["compose_spec"] = compose_yaml
    # delegated-stack-authoring §5.4 — tell the worker to enforce network posture.
    # Only stamped when the paid enforcement layer is present and enabled, so
    # the worker's bypass-refusal stays dark until enforcement is deliberately
    # enabled; a spec attaching a service to the platform network then means a
    # hand-authored bypass and is refused.
    # A platform-network attachment that survived _apply_egress_networks is the
    # managed-AI/admin exemption the API deliberately preserved (delegated
    # authors are refused external nets at validation), so the worker's
    # bypass-refusal must not fire on it — it did, and every managed-AI
    # redeploy (the `ollama` stack) failed once enforcement was on.
    if _egress_enforced_for_payload(db, tenant_id, compose_yaml):
        payload["egress_enforced"] = True
    if accel_demand:
        payload["accel_demand"] = accel_demand
    # GF.7a — a package-installed stack ships its package's FROZEN weights
    # (offline HF cache priming). The worker materializes them before compose
    # up and authenticates with the Harbor creds from
    # /workers/tasks/{id}/credentials — nothing credential-shaped goes here.
    # deploy + migrate: a migrate lands the stack on a DIFFERENT host, so the
    # cold target needs the weights materialized too. No package link / no
    # frozen weights -> _stack_package_weights returns None and the payload
    # is unchanged (no key).
    if stack_op in ("deploy", "migrate"):
        weights = _stack_package_weights(db, stack_id)
        if weights:
            payload["weights"] = weights
    # NOAH: a persistent serving stack is pin-prone work — it must not land on a
    # worker that NOAH may reclaim mid-serve. No-op unless the
    # noah.require_pinnable_enabled platform setting is on (default True —
    # every worker is pinnable except NOAH idle-harvest hosts/opt-outs).
    from utils.paid_import import optional_paid
    _noah = optional_paid("services.noah")
    if _noah is not None:
        payload = _noah.add_pinnable_requirement(payload, db=db)
    if extra_payload:
        payload.update(extra_payload)
    # Durable host pin (§8.1 pinned_worker): an operator-pinned stack's tasks
    # must NEVER move to another worker — the assignment-expiry sweep, the
    # offline-worker orphan sweep, the NOAH release and the scheduler's
    # PENDING pass read `pinned_worker` off the payload
    # (utils.task_pinning.durable_target_worker) and hold the task on its host
    # for the task's whole life, exactly like the benchmark pin. Applied after
    # the extra_payload merge so a caller can't shadow the operator's decision.
    if stack_id is not None:
        from crud import persistent_stacks_domain as _psd
        _stack = _psd.get_stack_by_id(db, stack_id)
        _stack_pin = getattr(_stack, "pinned_worker", None) if _stack else None
        if _stack_pin:
            payload["pinned_worker"] = _stack_pin
        # GX.S10c4b — a deploy of a stack with a `secret_file` variable may only
        # be claimed by a file_secrets-capable worker. The requirement rides on
        # the payload (like accel_demand) and is enforced at claim time by
        # worker_tasks._try_claim_task's file_secrets_guard; no per-claim query.
        # Every deploy path funnels through this function.
        if stack_op == "deploy" and _stack is not None:
            from services import stack_variables as _sv
            _declared = _sv.declared_variables(_stack.compose_spec or {})
            if any(isinstance(e, dict) and e.get("kind") == "secret_file"
                   for e in _declared.values()):
                payload["needs_file_secrets"] = True
    task = models.ApiTaskQueue(
        node_id=f"stack-{stack_op}-{project}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=9,
        task_type="system",
        priority_class="normal",
        task_payload=payload,
        status=(models.ApiTaskStatus.ASSIGNED if worker_name
                else models.ApiTaskStatus.PENDING),
        assigned_to_worker=worker_name,
        max_attempts=_STACK_OP_MAX_ATTEMPTS.get(stack_op, 1),
        output_namespace="attempt_0",
    )
    db.add(task)
    db.flush()
    return task


def _cancel_superseded_deploy_tasks(db: Session, stack_id: int, reason: str,
                                    exclude_task_id: Optional[int] = None,
                                    created_not_after: Optional[datetime] = None,
                                    keep_started_when_desired_running: bool = False
                                    ) -> int:
    """stack-stop-release: transition every earlier non-terminal deploy/migrate
    task for `stack_id` to CANCELLED, recording `reason` on each.

    This is the actual GPU release: `services.gpu_reservation.cards_in_use()`
    counts every ASSIGNED/CLAIMED/RUNNING task carrying a `gpu_assignment` as
    holding VRAM, so a deploy task left in any of those states after its stack
    has been stopped/torn-down/superseded holds that reservation forever
    (incident 2026-09-28: stack #27's deploy task 39168 stayed RUNNING after
    the stack was stopped, and every subsequent deploy logged "unplaceable —
    all cards full"). Callers: the stop/teardown completion handler
    (`routers.worker_tasks._apply_stack_task_result`), a fresh `deploy_stack`
    call (idempotency — supersede, don't duplicate), and the reconciler
    self-heal sweep (`services.stack_reconciler.reconcile_stale_deploy_tasks`).

    Scoped strictly by `stack_id` — never touches another stack's tasks.
    """
    from crud import persistent_stacks_domain as psd_crud
    tasks = psd_crud.list_nonterminal_deploy_tasks(
        db, stack_id, exclude_task_id=exclude_task_id, created_not_after=created_not_after)
    if keep_started_when_desired_running and tasks:
        # stack-stop-race (FIX-STACK-DEPLOY-TASK-CANCELLED-BY-STOP-RACE, 11842):
        # a stop/teardown that COMPLETES after the worker already STARTED a
        # deploy for this stack must not cancel that deploy while the operator
        # still wants the stack running — the deploy is the live intent, and
        # cancelling it 409s the worker's report-started ("Task is cancelled")
        # and the row never reaches `running` (Gate 6 dash-stacks-live red on
        # dell + xeon). Only a task the worker has not started yet (PENDING/
        # ASSIGNED) is superseded. Opt-in: the fresh-deploy supersede and the
        # reconciler sweep keep cancelling everything.
        _st = psd_crud.get_stack_by_id(db, stack_id)
        if _st is not None and _st.desired_state == "running":
            tasks = [t for t in tasks
                     if t.status not in (models.ApiTaskStatus.CLAIMED,
                                         models.ApiTaskStatus.RUNNING)]
    for t in tasks:
        t.status = models.ApiTaskStatus.CANCELLED
        t.error_message = reason
    if tasks:
        logger.info("persistent stack %s: cancelled %d superseded deploy/migrate "
                    "task(s) (ids=%s): %s", stack_id, len(tasks),
                    [t.id for t in tasks], reason)
    return len(tasks)


def _get_owned(db: Session, stack_id: int, user: models.User) -> models.PersistentStack:
    from services import persistent_stacks_shared_service as _svc
    return _svc.get_owned_stack(db, stack_id, user)


_SECRET_ENV_RE = __import__("re").compile(r"(PASSWORD|SECRET|TOKEN|_KEY$|APIKEY|AUTHKEY|PRIVATE)", __import__("re").I)

# The exact placeholder `_redact_compose_secrets` writes over a secret-looking
# env value. Named (not just inlined) so stack-definition-edit's save path
# (services.persistent_stack_definition_service.unredact_against) can detect
# "the editor round-tripped this untouched" and restore the real value instead
# of overwriting a live secret with the literal string on save — an edit
# endpoint that shows a redacted GET and accepts the same shape back on PUT
# must not let that round-trip destroy the secret it was hiding.
REDACTED_SENTINEL = "***redacted***"


def _redact_compose_secrets(spec):
    """Return a deep copy of a compose spec with service env values whose KEY looks
    like a secret masked. The DB keeps the real values (the worker needs them);
    this only sanitises what we hand back over the API."""
    import copy
    if not isinstance(spec, dict):
        return spec
    out = copy.deepcopy(spec)
    for svc in (out.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        env = svc.get("environment")
        if isinstance(env, dict):
            for k in list(env.keys()):
                if _SECRET_ENV_RE.search(str(k)):
                    env[k] = REDACTED_SENTINEL
        elif isinstance(env, list):
            svc["environment"] = [
                (f"{e.split('=', 1)[0]}={REDACTED_SENTINEL}"
                 if isinstance(e, str) and "=" in e and _SECRET_ENV_RE.search(e.split("=", 1)[0])
                 else e)
                for e in env
            ]
    return out


def _out(stack: models.PersistentStack) -> "schemas.PersistentStack":
    """Serialize a stack for API return with compose secrets redacted (M3)."""
    dto = schemas.PersistentStack.model_validate(stack)
    dto.compose_spec = _redact_compose_secrets(dto.compose_spec)
    dto.tls_warnings = _tls_warnings(stack) or None
    # Always surface the deterministic, collision-proof internal name
    # (t<tid>-<stackid>-<svc>) for reachable services — not just the raw stored
    # mint record — so the UI (workstream D) shows the stable internal address
    # even before an identity is minted / on a single-host install.
    dto.mesh_endpoints = _stack_mesh_endpoints(stack) or None
    # friendly-stack-fqdn (D4) — surface the edge target the customer CNAMEs their
    # own DNS at (informational only; MOD provisions no customer DNS). Only
    # meaningful once the stack is exposed; None otherwise.
    dto.edge_target = (_stack_edge_target() or None) if getattr(stack, "exposed", False) else None
    # 30.6.13 c13-J3 — which App the running containers' identity token was minted
    # for, and when. Read straight off the mint record; only app_id + minted_at are
    # copied (never expires_at, never token bytes).
    rec = (stack.storage_refs or {}).get("identity")
    if isinstance(rec, dict):
        if isinstance(rec.get("app_id"), int):
            dto.identity_app_id = rec.get("app_id")
        if isinstance(rec.get("minted_at"), str):
            dto.identity_minted_at = rec.get("minted_at")
    return dto


def _out_with_cost(db: Session, stack: models.PersistentStack) -> "schemas.PersistentStack":
    """`_out` plus the per-stack projected reservation cost (§5.4). Kept out of
    `_out` itself because that is called from many write paths without needing a
    rate lookup; the GET endpoints (which show the stack card) opt in here. 0
    while metering is dark (rates 0) — the honest pre-launch number."""
    dto = _out(stack)
    # GT.D6 DS-3 — every read below is try/except-wrapped: on CE the Paid
    # modules are absent and the fields simply stay unset (# CE-safe).
    try:
        from services.stack_billing import stack_reservation_rate
        dto.reservation_cost_per_hour = round(stack_reservation_rate(db, stack), 6)
    except Exception as e:  # noqa: BLE001
        logger.debug("per-stack reservation cost unavailable for stack %s: %s",
                     stack.id, e)
    # CARVE-FIX-B (CE 2026-11-01) — stack_observation is a PAID module: the CE
    # open-core manifest denies `**/stack_observation.py` (open-core/manifest.yaml
    # :1611), so on CE the three observer reads below used to pay an ImportError
    # per request and swallow it — `health: null` on the card plus a WARNING
    # (exc_info) per stack row per GET, which is the HEALTHGRADE e2e red.
    # Load it through the repo's paid-import pattern instead (same call shape as
    # _shared.py:140/:218/:975 and services/persistent_stacks_domain_service.py:174):
    # `optional_paid` (utils/paid_import.py:79) returns None ONLY when the missing
    # module IS this one (`_is_absent` :70-74 + the first-party-prefix guard
    # :84-90). Anything else — an ImportError from a module the observer itself
    # imports, or a runtime error inside a read (the tz-naive `observed_at`
    # TypeError J78 fixed is the example) — PROPAGATES: fail closed, never a
    # swallowed null verdict.
    from utils.paid_import import optional_paid
    _so = optional_paid("services.stack_observation")
    if _so is None:
        # CE posture: one log line for the whole process, not per row, then an
        # EXPLICIT edition state on the card. `health` stays null only for a
        # stack the grader genuinely has no verdict for; on CE the grader does
        # not exist, and that must be visible, not a silent absence.
        # Shape the Dashboard already renders: `_psHealthBadge`
        # (CORE/Dashboard/js/persistent-stacks.js:594-601) turns ANY
        # `health.grade` into the ps-health-badge span (text + data-grade +
        # title "Health grade: <grade> — <reason>"), and the row mapper
        # (:3403) passes grade/reason through. So CE shows "unavailable" with
        # the edition reason in the tooltip instead of no badge at all.
        if not getattr(_out_with_cost, "_stack_obs_ce_logged", False):
            _out_with_cost._stack_obs_ce_logged = True
            logger.info(
                "services/stack_observation.py is absent in this edition (CE carve): "
                "per-stack gateway metrics, health grade and ha-class state are not "
                "graded here; the card reports health as 'unavailable' "
                "(logged once, not per stack row)")
        dto.health = schemas.StackHealth(
            grade="unavailable", reason="not graded in this edition")
    else:
        # Paid posture: byte-identical reads to before. CE 2026-11-04
        # (SWEEPRED-healthgrade): the absent-TABLE case is answered INSIDE
        # stack_health_grade — its table probe returns an honest grade and never
        # raises — so anything that still reaches this wrapper is a genuine grader
        # bug on an EXISTING table. Fail closed: log the stack and the exception
        # text (greppable per row; exc_info carries the frame) then RE-RAISE, so a
        # wrong product never serves a swallowed null verdict. The absent-MODULE
        # case is the `optional_paid` branch above, not this one.
        try:
            dto.gateway_metrics = _so.latest_gateway_metrics(db, stack.id)
            dto.health = schemas.StackHealth(**_so.stack_health_grade(db, stack))
            _ha = _so.ha_class_state(db, stack)
        except Exception as e:  # noqa: BLE001
            logger.warning("stack health grade unavailable for stack %s: %s",
                           stack.id, e, exc_info=True)
            raise
        _sh = _so.status_health_state(db, stack)
        dto.status_health = _sh["status_health"]
        dto.status_health_reason = _sh["status_health_reason"]
        dto.ha_class_observed = _ha["ha_class_observed"]
        dto.ha_class_mismatch = _ha["ha_class_mismatch"]
        dto.ha_class_mismatch_note = _ha["ha_class_mismatch_note"]
    return dto


def _audit_stack(db: Session, event_name: str, stack: models.PersistentStack,
                 user: models.User, extra: Optional[dict] = None) -> None:
    """Record a stack lifecycle event (create/deploy/teardown/migrate) — these
    move/destroy tenant data and must be auditable."""
    try:
        details = {"stack_id": stack.id, "name": stack.name,
                   "storage_class": stack.storage_class, "home_zone": stack.home_zone}
        if extra:
            details.update(extra)
        db.add(models.AuditEvent(tenant_id=user.tenant_id, user_id=user.id,
                                 event_name=event_name, details=details))
    except Exception as e:  # noqa: BLE001
        logger.warning("stack audit (%s) failed: %s", event_name, e)


def _parse_accelerator_requirements(spec) -> dict:
    """Extract + normalise per-service accelerator declarations from a compose spec.

    Declared as a compose extension field on the service, which `docker compose`
    preserves and ignores, so the worker keeps passing the YAML straight to
    `compose up` with no stripping pass:

        services:
          devstral:
            image: vllm/vllm-openai:latest
            x-mod:
              accelerator: {required: true, min_vram_gb: 48, count: 1, kind: cuda}

    Why not read `deploy.resources.reservations.devices`: compose's device
    reservation expresses count and capabilities, not SIZE. `count: 1` cannot
    say "48 GB", which is the entire question a GPU stack needs answered.

    Returns {service_name: {required, min_vram_gb, count, kind}} for services
    that declare one. Services without a declaration are absent — they are
    unconstrained, not "requires 0 GB".

    Normalising here means the claim-time capability guard reads one fixed
    shape instead of re-parsing free-form compose on every evaluation. It is a
    DECLARATION, not a placement decision: which worker satisfies it is
    evaluated just-in-time at claim, against what that worker reports then.
    """
    out: dict = {}
    services = (spec or {}).get("services") or {}
    if not isinstance(services, dict):
        return out
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        block = (svc.get("x-mod") or {}).get("accelerator") if isinstance(svc.get("x-mod"), dict) else None
        if block is None:
            continue
        out[name] = {
            "required": bool(block.get("required", True)),
            "min_vram_gb": float(block["min_vram_gb"]) if block.get("min_vram_gb") is not None else None,
            "count": int(block.get("count", 1)),
            "kind": (str(block["kind"]).strip().lower() if block.get("kind") else None),
        }
    return out


def _aggregate_accelerator_demand(reqs: dict) -> dict:
    """Collapse per-service declarations into ONE demand the host must satisfy whole.

    A stack is one compose project on one host — always. Splitting a project
    across hosts would break compose service-name DNS (services address each
    other by name on the project network), so it is a networking rewrite, not a
    placement decision. Independent placement means separate stacks.

    So every service's requirement has to be met by the SAME worker, and the
    aggregate is what the claim-time guard evaluates.

    Aggregating is NOT summing VRAM. A stack wanting 48 GB and 24 GB needs
    *two* devices — one of each size. A single 80 GB card does NOT satisfy it,
    because each process loads into one device; summing would place it there
    and OOM both. The correct test is domination: sort the demands descending,
    sort the worker's devices descending, and require device[i] >= demand[i]
    for every i. Largest device covers the largest demand, and so on.

    Returns {kind_or_"": [descending list of per-device minimums]}. `count: N`
    expands to N entries. Grouped by kind because a cuda demand cannot be met
    by a rocm device. Collapses to a single-element list for the overwhelmingly
    common one-service case.
    """
    demand: dict = {}
    for spec in (reqs or {}).values():
        if not spec.get("required", True):
            continue
        vram = spec.get("min_vram_gb")
        if vram is None:
            continue
        bucket = demand.setdefault(spec.get("kind") or "", [])
        bucket.extend([float(vram)] * max(1, int(spec.get("count") or 1)))
    for k in demand:
        demand[k].sort(reverse=True)
    return demand


def _parse_compose_memory(value) -> float:
    """Parse a compose memory string ('512M', '2g', '1073741824') to GB.

    Docker compose memory strings use 1024-based b/k/m/g suffixes; a bare number
    is bytes. Returns gibibytes (the unit the reservation rate is priced in).
    Unparseable input is 0 — a bad reservation string must never inflate a bill.
    """
    if value is None:
        return 0.0
    try:
        if isinstance(value, (int, float)):
            return float(value) / (1024 ** 3)
        s = str(value).strip().lower()
        if not s:
            return 0.0
        mult = 1
        for suffix, m in (("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024),
                          ("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024), ("b", 1)):
            if s.endswith(suffix):
                mult = m
                s = s[: -len(suffix)].strip()
                break
        return (float(s) * mult) / (1024 ** 3)
    except (TypeError, ValueError):
        return 0.0


def _capture_reservation_units(stack: "models.PersistentStack") -> dict:
    """Snapshot the priced reservation vector {cpu_cores, mem_gb, gpu:[vram_gb...]}.

    persistent-stack-billing §2.2/D7. Captured once at deploy so the meter prices
    a fixed, auditable vector and never re-parses free-form compose on every tick.

    - gpu: the aggregate accelerator demand (_aggregate_accelerator_demand),
      flattened across kinds into a descending VRAM list. This is what denies the
      pool, priced per reserved VRAM-GB.
    - cpu_cores / mem_gb: summed across services' compose
      deploy.resources.reservations (cpus / memory). Open Question 1 posture (a):
      we meter ONLY what is declared — GPU always, cpu/mem only where a
      reservation exists. A stack that declares no reservations captures 0 cpu/mem
      (and is billed on GPU alone), rather than imputing a floor.
    """
    spec = stack.compose_spec or {}
    demand = _aggregate_accelerator_demand(stack.accelerator_requirements or {})
    gpu: list = []
    for vram_list in demand.values():
        gpu.extend(float(v) for v in vram_list)
    gpu.sort(reverse=True)

    cpu_cores = 0.0
    mem_gb = 0.0
    services = (spec.get("services") or {}) if isinstance(spec, dict) else {}
    if isinstance(services, dict):
        for svc in services.values():
            if not isinstance(svc, dict):
                continue
            reservations = (((svc.get("deploy") or {}).get("resources") or {})
                            .get("reservations") or {})
            if not isinstance(reservations, dict):
                continue
            cpus = reservations.get("cpus")
            if cpus is not None:
                try:
                    cpu_cores += float(cpus)
                except (TypeError, ValueError):
                    pass
            mem_gb += _parse_compose_memory(reservations.get("memory"))

    return {
        "cpu_cores": round(cpu_cores, 6),
        "mem_gb": round(mem_gb, 6),
        "gpu": gpu,
    }


def _validate_accelerator_block(service_name: str, block) -> None:
    """422 on a malformed accelerator declaration at CREATE time rather than
    letting it surface as an unplaceable stack at deploy.

    Strict on types, tolerant of unknown keys: an older manager must not reject
    a spec written against a newer schema.

    `kind` is deliberately NOT validated against an enum. `hardware_tags` is
    open-ended by design (`Worker/internal/capabilities/detect.go:25` documents
    ["cuda","tensorrt"], ["rocm"], ["cpu"], and it can emit "intel"/"npu"), so
    an enum here would reject a worker's genuine hardware the first time
    someone reports an accelerator we hadn't hardcoded. A typo'd kind instead
    surfaces as a legible placement failure.
    """
    if not isinstance(block, dict):
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: x-mod.accelerator must be an object")
    if "min_vram_gb" in block and block["min_vram_gb"] is not None:
        v = block["min_vram_gb"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: min_vram_gb must be a number")
        if v <= 0:
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: min_vram_gb must be greater than 0")
    if "count" in block and block["count"] is not None:
        c = block["count"]
        if isinstance(c, bool) or not isinstance(c, int):
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: count must be an integer")
        if c < 1:
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: count must be at least 1")
    if "required" in block and not isinstance(block["required"], bool):
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: required must be a boolean")
    if "kind" in block and block["kind"] is not None:
        if not isinstance(block["kind"], str) or not block["kind"].strip():
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: kind must be a non-empty string")


# delegated-stack-authoring §4 — the egress posture. Three modes, matching the
# worker's existing networks (netpol.go): 'none' → userNetwork (deny-by-default,
# zero outbound), 'api' → apiNetwork (reach the API only), 'hosts' → egressNetwork
# with an outbound allow-list of domains (the #87 allowed_egress concept). Parsed
# and validated at create; RECORDED, not yet enforced (Group 5 does that).
_EGRESS_MODES = ("none", "api", "hosts")


def _stack_egress_host_is_broad_wildcard(host: str) -> bool:
    """True for a public-suffix wildcard (`*.com`, `*.co.uk`), optionally with a
    port. The shared grammar rejects these because they are BROAD; stacks are
    exactly where broad egress belongs (nodes get classify_breadth instead), so
    this validator tolerates the breadth while still requiring the shape: a
    leading `*.`, one hostname label, optional `:port`. A bare `*` never matches
    (no dot) and is rejected by the grammar proper."""
    h = str(host or "").strip().lower()
    if not h.startswith("*."):
        return False
    head = h[2:]
    if ":" in head:
        head = head.rpartition(":")[0]
    return bool(re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", head))


def _validate_egress_block(service_name: str, block) -> None:
    """422 on a malformed x-mod.egress declaration at CREATE time. Strict on the
    shape; an absent block is legal (nothing declared)."""
    if not isinstance(block, dict):
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: x-mod.egress must be an object")
    mode = block.get("mode")
    if mode not in _EGRESS_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"service {service_name!r}: x-mod.egress.mode must be one of {list(_EGRESS_MODES)}")
    hosts = block.get("hosts")
    if mode == "hosts":
        if not isinstance(hosts, list) or not hosts or not all(
            isinstance(h, str) and h.strip() for h in hosts
        ):
            raise HTTPException(
                status_code=422,
                detail=f"service {service_name!r}: x-mod.egress mode 'hosts' needs a non-empty list of host strings")
    elif hosts is not None:
        raise HTTPException(
            status_code=422,
            detail=f"service {service_name!r}: x-mod.egress.hosts is only valid with mode 'hosts'")
    # declared-egress-and-sealed-nodes C4 (2.1, 9.3) — the hosts list must parse
    # under the SAME grammar nodes use (services/egress_allowlist.compile_allowlist),
    # so a typo cannot sail through create and only fail at runtime. Stacks are
    # where BROAD egress belongs, so classify_breadth is deliberately NOT run here
    # (a node would be rejected for `*.com`; a stack is not).
    if hosts:
        from services.egress_allowlist import (compile_allowlist,
                                              EgressAllowlistError)
        for _host in hosts:
            try:
                compile_allowlist([_host])
            except EgressAllowlistError as exc:
                if not _stack_egress_host_is_broad_wildcard(_host):
                    raise HTTPException(
                        status_code=422,
                        detail=f"service {service_name!r}: x-mod.egress host {_host!r} "
                               f"is not a valid egress entry ({exc})")


def _parse_egress_policy(spec) -> dict:
    """Extract per-service egress postures from x-mod.egress, normalised to
    {service: {mode, hosts?}}. Returns {} when nothing is declared."""
    out: dict = {}
    if not isinstance(spec, dict):
        return out
    for name, svc in (spec.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        xmod = svc.get("x-mod")
        block = xmod.get("egress") if isinstance(xmod, dict) else None
        if isinstance(block, dict) and block.get("mode") in _EGRESS_MODES:
            entry = {"mode": block["mode"]}
            if block["mode"] == "hosts":
                entry["hosts"] = list(block.get("hosts") or [])
            out[name] = entry
    return out


# delegated-stack-authoring §5 — per-stack egress ENFORCEMENT is a paid layer
# (services/stack_egress_enforcement.py, absent from the Community Edition
# tree). Without it a stack gets the network behaviour its compose file asks
# for: the declared `x-mod.egress` posture above is recorded and validated but
# not realised as network attachments, and no `egress_enforced` flag is sent to
# the worker. With it present, the layer installs its helpers below.
def _apply_egress_networks(spec: dict, db: Optional[Session] = None,
                           tenant_id: Optional[int] = None) -> dict:
    """Default (no enforcement layer): the worker-bound spec is returned
    untouched."""
    return spec


def _egress_enforced_for_payload(db: Optional[Session], tenant_id: Optional[int],
                                 compose_yaml: Optional[str]) -> bool:
    """Default (no enforcement layer): the worker is never told to enforce."""
    return False


from utils.paid_import import optional_paid as _optional_paid  # noqa: E402
_stack_egress_layer = _optional_paid("services.stack_egress_enforcement")
if _stack_egress_layer is not None:
    _stack_egress_layer.install_stack_networks(globals())


# stack-secrets-and-certs §1 — secrets are Vault REFERENCES, never inline values.
# x-mod.secrets: {ENV_NAME: "vault:tenants/<id>/stacks/<name>/<svc>/<key>"}.
_VAULT_REF_PREFIX = "vault:"


def _validate_secrets_block(service_name: str, block, secret_prefix: Optional[str] = None) -> None:
    """422 on a malformed x-mod.secrets declaration. Every value must be a
    `vault:` reference (never an inline literal); when a prefix is supplied the
    path must sit under the stack's own `tenants/<id>/stacks/<name>/`."""
    if not isinstance(block, dict):
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: x-mod.secrets must be an object")
    for env, ref in block.items():
        if not isinstance(env, str) or not env.strip():
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: secret env name must be a non-empty string")
        # 13.5a — a named TENANT secret: {secret: "<name>"} (optionally with
        # "type": "file" and a "target" mount path). Tenant scoping (own or
        # shared-with) is enforced at DEPLOY, where the stack's tenant is
        # authoritative — not at create, which validates shape only.
        if isinstance(ref, dict):
            name = ref.get("secret")
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(
                    status_code=422,
                    detail=f"service {service_name!r}: secret {env!r}: named ref needs a non-empty 'secret' name")
            t = ref.get("type", "env")
            if t not in ("env", "file"):
                raise HTTPException(
                    status_code=422,
                    detail=f"service {service_name!r}: secret {env!r}: 'type' must be 'env' or 'file'")
            if "target" in ref and not (isinstance(ref["target"], str) and ref["target"].strip()):
                raise HTTPException(
                    status_code=422,
                    detail=f"service {service_name!r}: secret {env!r}: 'target' must be a non-empty path")
            continue
        if not isinstance(ref, str) or not ref.startswith(_VAULT_REF_PREFIX):
            raise HTTPException(
                status_code=422,
                detail=f"service {service_name!r}: secret {env!r} must be a '{_VAULT_REF_PREFIX}' reference or a {{'secret': <name>}} tenant secret, not an inline value")
        path = ref[len(_VAULT_REF_PREFIX):]
        if not path.strip():
            raise HTTPException(status_code=422,
                                detail=f"service {service_name!r}: secret {env!r} has an empty vault path")
        if secret_prefix and not path.startswith(secret_prefix):
            raise HTTPException(
                status_code=422,
                detail=f"service {service_name!r}: secret {env!r} path must be under {secret_prefix!r}")


def _parse_secret_refs(spec) -> dict:
    """Extract per-service secret references, normalised to {service: {env: ref}}
    with the `vault:` prefix stripped from path refs. 13.5a: a NAMED tenant
    secret ({"secret": name, "type": "env"|"file", "target": ...}) is carried
    through as the same dict (name + type + optional target only — never a
    value). Returns {} when nothing is declared."""
    out: dict = {}
    if not isinstance(spec, dict):
        return out
    for name, svc in (spec.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        xmod = svc.get("x-mod")
        block = xmod.get("secrets") if isinstance(xmod, dict) else None
        if isinstance(block, dict) and block:
            refs = {}
            for env, ref in block.items():
                if isinstance(ref, str) and ref.startswith(_VAULT_REF_PREFIX):
                    refs[env] = ref[len(_VAULT_REF_PREFIX):]
                elif isinstance(ref, dict) and isinstance(ref.get("secret"), str) \
                        and ref["secret"].strip():
                    entry = {"secret": str(ref["secret"]).strip()}
                    # `env` is the default delivery channel — only record an
                    # explicit `type` (i.e. "file") so env refs stay minimal.
                    if ref.get("type") == "file":
                        entry["type"] = "file"
                    if isinstance(ref.get("target"), str) and ref["target"].strip():
                        entry["target"] = ref["target"].strip()
                    refs[env] = entry
            if refs:
                out[name] = refs
    return out


# stack-secrets-and-certs §5 — certs are per-service, Vault-backed secrets.
# x-mod.tls: {mode: "auto"|"byo", fqdn: "<host>"}. `auto` is issued+renewed by
# the ingress (caddy-stack-ingress); `byo` is a cert+key the holder uploads,
# stored at the stack's cert path and validated before it lands.
_TLS_MODES = ("auto", "byo")


def _validate_tls_block(service_name: str, block) -> None:
    """422 on a malformed x-mod.tls declaration at CREATE. Strict on the shape;
    an absent block is legal (the service is not fronted by TLS)."""
    if not isinstance(block, dict):
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: x-mod.tls must be an object")
    mode = block.get("mode")
    if mode not in _TLS_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"service {service_name!r}: x-mod.tls.mode must be one of {list(_TLS_MODES)}")
    fqdn = block.get("fqdn")
    if not isinstance(fqdn, str) or not fqdn.strip():
        raise HTTPException(status_code=422,
                            detail=f"service {service_name!r}: x-mod.tls.fqdn must be a non-empty string")


def _parse_tls_config(spec) -> dict:
    """Extract per-service TLS declarations from x-mod.tls, normalised to
    {service: {mode, fqdn}}. Returns {} when nothing is declared."""
    out: dict = {}
    if not isinstance(spec, dict):
        return out
    for name, svc in (spec.get("services") or {}).items():
        if not isinstance(svc, dict):
            continue
        xmod = svc.get("x-mod")
        block = xmod.get("tls") if isinstance(xmod, dict) else None
        if isinstance(block, dict) and block.get("mode") in _TLS_MODES:
            out[name] = {"mode": block["mode"], "fqdn": str(block.get("fqdn") or "").strip()}
    return out


# stack-secrets-and-certs §4/§5 — the WRITE side of the config plane. A holder
# puts a value at their stack's own Vault path; the platform puts config under
# `platform/`. Both go through the existing approle write helper — no new client.
def _stack_secret_prefix(stack: "models.PersistentStack") -> str:
    # `tenants/` (plural) is the established Vault tenant-secret namespace — the
    # `api-tenant-secrets` approle policy grants `secret/data/tenants/*` and other
    # code (node_definitions tenant_harbor) already lives there. A singular
    # `tenant/` would 403 at runtime under that policy.
    return f"tenants/{stack.tenant_id}/stacks/{stack.name}/"


def _require_under_prefix(path: str, prefix: str) -> None:
    """The stack prefix is a security boundary, mirroring `_validate_secrets_block`
    on the read side: a holder may write only under their own stack's path. Reject
    an outside path (403) and any `..` traversal that could climb out (422)."""
    if not isinstance(path, str) or not path.strip():
        raise HTTPException(status_code=422, detail="secret path must be a non-empty string")
    if ".." in path.split("/"):
        raise HTTPException(status_code=422, detail="secret path must not contain '..'")
    if not path.startswith(prefix):
        raise HTTPException(status_code=403,
                            detail=f"secret path must be under {prefix!r}")


def _vault_write(path: str, data: dict) -> None:
    """Write `data` to a Vault KV path via the API approle (the same credential
    the read path uses). Fails closed (502) — a write that cannot reach Vault
    must surface, never be silently dropped."""
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if not (role_id and secret_id):
        raise HTTPException(status_code=502, detail="secret write: Vault approle not configured")
    from utils.vault_client import approle_login, write_kv_v2, VaultError
    try:
        token = approle_login(role_id=role_id, secret_id=secret_id)
        write_kv_v2(path, token=token, data=data)
    except VaultError as e:
        raise internal_http_error(502, "Secret write failed", e) from e


def _fqdn_matches_cert(cert, fqdn: str) -> bool:
    """True when `fqdn` is served by the cert — matched against the SAN DNS names
    (the modern authority) and, as a fallback, the subject CN. Supports a single
    leading-label wildcard (`*.example.com`)."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtensionOID
    names = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        names.extend(san.value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        pass
    names.extend(a.value for a in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME))
    fqdn = fqdn.strip().lower()
    for n in names:
        n = str(n).strip().lower()
        if n == fqdn:
            return True
        if n.startswith("*.") and "." in fqdn and fqdn.split(".", 1)[1] == n[2:]:
            return True
    return False


def _validate_cert_pair(cert_pem: str, key_pem: str, fqdn: str):
    """§5.2 — validate a byo cert+key BEFORE it is stored, all failures 422:
    the cert parses, it serves `fqdn`, the private key pairs with it, and it is
    not already expired. Returns the parsed notAfter (tz-aware UTC)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
    except Exception as e:  # noqa: BLE001
        raise internal_http_error(422, "TLS cert is not valid PEM", e) from e
    try:
        key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except Exception as e:  # noqa: BLE001
        raise internal_http_error(422, "TLS private key is not valid PEM", e) from e
    if not _fqdn_matches_cert(cert, fqdn):
        raise HTTPException(status_code=422,
                            detail=f"TLS cert does not match declared fqdn {fqdn!r} (checked SAN + CN)")
    cert_pub = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    key_pub = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if cert_pub != key_pub:
        raise HTTPException(status_code=422, detail="TLS private key does not pair with the cert")
    # not_valid_after_utc on cryptography>=42; older returns a naive UTC datetime.
    not_after = getattr(cert, "not_valid_after_utc", None)
    if not_after is None:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    if not_after <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422,
                            detail=f"TLS cert already expired ({not_after.isoformat()})")
    return not_after


# §5.3 — warn when a byo cert nears expiry so it can be rotated before it lapses.
# auto certs self-renew (owned by caddy-stack-ingress), so they are never warned.
_TLS_WARN_WITHIN_DAYS = 30


def _tls_warnings(stack: "models.PersistentStack") -> list:
    conf = stack.tls_config if isinstance(stack.tls_config, dict) else {}
    out = []
    now = datetime.now(timezone.utc)
    for svc, entry in conf.items():
        if not isinstance(entry, dict) or entry.get("mode") != "byo":
            continue
        iso = entry.get("not_after")
        if not iso:
            continue
        try:
            na = datetime.fromisoformat(str(iso))
        except ValueError:
            continue
        if na.tzinfo is None:
            na = na.replace(tzinfo=timezone.utc)
        days = (na - now).days
        if days <= _TLS_WARN_WITHIN_DAYS:
            fqdn = entry.get("fqdn") or "?"
            out.append(f"service {svc!r} TLS cert for {fqdn} expires in {days} days")
    return out


# delegated-stack-authoring §3 — the compose safety boundary. For a DELEGATED
# author (is_admin False) the validator is a security boundary, not a lint:
# hard-refuse host-escape constructs for everyone, refuse admin-only host-access
# constructs and unknown keys for a delegated author, and permit host bind mounts
# only under an explicitly allowed prefix (empty by default). Platform admins
# (is_admin True — includes the managed-AI provisioning path) keep their existing
# latitude; the hard refusals still apply to them as defense in depth.

# Host-escape constructs no author may use. Verified against the managed-AI
# spec builder (services/managed_ai_stacks.py) — it uses NONE of these.
_COMPOSE_HOST_NAMESPACE_KEYS = ("network_mode", "pid", "ipc", "userns_mode")

# Host-access constructs the managed-AI path legitimately needs (GPU device
# passthrough, seccomp profile, render/video groups) but a delegated author
# must not have.
_COMPOSE_ADMIN_ONLY_KEYS = ("devices", "security_opt", "group_add",
                            "cap_drop", "sysctls", "runtime", "network_mode")

# The allow-list a delegated author's service is held to (task 3.4): understood
# keys only; anything else is refused rather than forwarded.
_COMPOSE_DELEGATED_ALLOWED_KEYS = frozenset({
    "image", "build", "environment", "env_file", "volumes", "ports", "expose",
    "healthcheck", "restart", "command", "entrypoint", "depends_on", "labels",
    "working_dir", "user", "hostname", "stop_grace_period", "deploy", "networks",
    "x-mod", "init", "read_only", "tmpfs", "tty", "stdin_open", "dns",
})


def _iter_volume_sources(svc: dict):
    """Yield the source of every mount on a service (short or long syntax)."""
    for mount in (svc.get("volumes") or []):
        if isinstance(mount, str):
            src = mount.split(":", 1)[0]
            if src:
                yield src
        elif isinstance(mount, dict):
            src = mount.get("source")
            if src:
                yield src


def _is_docker_socket(src: str) -> bool:
    return os.path.basename(src.rstrip("/")) == "docker.sock" or "docker.sock" in src


def _is_host_path(src: str) -> bool:
    return src.startswith(("/", ".", "~"))


# delegated-stack-authoring §3.5 — the platform-admin acknowledgement. Written
# ONLY by POST /{stack_id}/acknowledge-host-access (admin-only), into the
# STORED compose_spec's per-service x-mod.admin_ack block, so it is explicit,
# per-service, stored with the stack, and audited (that endpoint writes the
# PermissionAuditLog entry). It is the ONLY way an admin-only construct
# (§3.4's _COMPOSE_ADMIN_ONLY_KEYS) reaches a delegated author's stack — never
# by the author acknowledging their own request.
_ADMIN_ACK_KEY = "admin_ack"


def _validate_service_security(name, svc, is_admin, allowed_host_mount_prefixes,
                               trust_admin_ack: bool = False) -> None:
    # --- hard refusals: every author, admin included (defense in depth) --------
    # §3.2 — no acknowledgement, admin-only or not, ever reaches this far: the
    # hard refusals below run before `is_admin`/ack are even consulted.
    if svc.get("privileged"):
        raise HTTPException(status_code=422,
                            detail=f"service {name!r}: 'privileged' is not permitted")
    if svc.get("cap_add"):
        raise HTTPException(status_code=422,
                            detail=f"service {name!r}: 'cap_add' is not permitted")
    for k in _COMPOSE_HOST_NAMESPACE_KEYS:
        v = svc.get(k)
        if isinstance(v, str) and v.split(":", 1)[0] == "host":
            raise HTTPException(status_code=422,
                                detail=f"service {name!r}: '{k}: host' is not permitted")
    for src in _iter_volume_sources(svc):
        if _is_docker_socket(src):
            raise HTTPException(status_code=422,
                                detail=f"service {name!r}: mounting the Docker socket is not permitted")

    if is_admin:
        return

    # --- delegated author: the boundary proper --------------------------------
    # §3.5/§3.7 — `trust_admin_ack` is True ONLY when the caller is validating
    # already-STORED stack data (the deploy-time gate); it is False (the
    # default) whenever the spec being checked is fresh, caller-supplied input
    # (create / from-wizard / assemble). A non-admin author's own submission can
    # therefore never carry `x-mod.admin_ack` at all — its mere presence is
    # refused outright, not silently stripped or ignored, so a forged marker
    # can never be planted for a later, trusting validation to honor.
    xmod = svc.get("x-mod")
    admin_ack = xmod.get(_ADMIN_ACK_KEY) if isinstance(xmod, dict) else None
    if admin_ack is not None and not trust_admin_ack:
        raise HTTPException(
            status_code=422,
            detail=(f"service {name!r}: 'x-mod.{_ADMIN_ACK_KEY}' may only be set by a "
                    f"platform admin, via POST /{{stack_id}}/acknowledge-host-access"))
    acknowledged = (trust_admin_ack and isinstance(admin_ack, dict)
                   and bool(admin_ack.get("by_user_id")))
    for k in _COMPOSE_ADMIN_ONLY_KEYS:
        if k in svc and not acknowledged:
            raise HTTPException(status_code=422,
                                detail=f"service {name!r}: '{k}' requires platform admin")
    # delegated-stack-authoring §3 + owner 11-12 T1 — a long-syntax mount that
    # says `type: bind` is a HOST bind even when its source is not an absolute
    # or `.`/`~`-rooted path (compose resolves a relative bind source against
    # the project dir), so hold every such mount to the allowed-prefix check.
    # Short-syntax `name:/target` entries with no `type:` stay named volumes.
    for mount in (svc.get("volumes") or []):
        src = (mount.split(":", 1)[0] if isinstance(mount, str)
               else (mount.get("source") if isinstance(mount, dict) else None))
        if not src:
            continue
        if _is_host_path(src) or (isinstance(mount, dict)
                                  and mount.get("type") == "bind"):
            real = os.path.realpath(os.path.expanduser(src))
            allowed = any(
                real == p or real.startswith(p.rstrip("/") + "/")
                for p in (allowed_host_mount_prefixes or ())
            )
            if not allowed:
                raise HTTPException(
                    status_code=422,
                    detail=f"service {name!r}: host bind mount {src!r} is not permitted "
                           f"(use a named volume instead)")
    # owner 11-12 T2 — a build context of `/` (or any absolute/`~`-rooted path)
    # hands the deploy a build over the host root; a delegated author gets
    # project-relative contexts only. Admin callers keep their latitude.
    _bld = svc.get("build")
    _ctx = _bld if isinstance(_bld, str) else (
        _bld.get("context") if isinstance(_bld, dict) else None)
    if isinstance(_ctx, str) and _is_host_path(_ctx) and not _ctx.startswith("./"):
        raise HTTPException(
            status_code=422,
            detail=f"service {name!r}: build context {_ctx!r} is not permitted "
                   f"(use a project-relative context, e.g. './app')")
    # An acknowledged service's admin-only keys are, by definition, expected —
    # widen the allow-list for THIS service only so §3.4 doesn't independently
    # refuse the very constructs the ack just cleared.
    allowed_keys = (_COMPOSE_DELEGATED_ALLOWED_KEYS | frozenset(_COMPOSE_ADMIN_ONLY_KEYS)
                    if acknowledged else _COMPOSE_DELEGATED_ALLOWED_KEYS)
    for k in svc:
        if k not in allowed_keys:
            raise HTTPException(
                status_code=422,
                detail=f"service {name!r}: unsupported construct {k!r} for a delegated author")


# ---------------------------------------------------------------------------
# GSEC FX.80 p4b (Q63=A, hosted GPU isolation) — read-only model-cache mounts.
#
# The platform's model caches (/var/lib/mod/hf-cache and friends) are SHARED
# host paths. A tenant stack that binds one WRITABLE can overwrite a weight
# file another tenant then loads — cross-tenant weight poisoning. So every
# tenant-owned stack mount of a known cache host path must be read-only
# (`:ro` short syntax or `read_only: true` long syntax).
#
# Exemptions (deliberate, so the live fleet keeps working):
#  * system/platform stacks — `tenant_id is None` (and the platform tenant
#    itself, PLATFORM_TENANT_ID, which owns the flat writable cache per R-159);
#  * downloader/fetcher services, which MUST write the cache they pull into.
#    ONE clean rule decides this: the explicit per-service flag
#    `x-mod.model_cache_writable: true`. MOD's own hf-weights-fetcher image
#    is recognised as carrying that flag automatically (MOD authors it), so
#    existing catalog/managed renders need no edit.
#
# Mode is the platform setting `stacks.model_cache_ro_mode` (warn|enforce,
# default warn) and the path list is `stacks.model_cache_ro_paths`. Warn =
# log + audit row, deploy proceeds; enforce = 422 with the offending service
# named. Registered in services/platform_settings.py (see the FX.80 p4b block
# at the end of that file).
# ---------------------------------------------------------------------------

_TENANT_UNKNOWN = object()  # caller did not say which tenant owns the spec
_MODEL_CACHE_RO_FLAG = "model_cache_writable"
_MODEL_CACHE_RO_EVENT = "model_cache_ro_violation"
_PLATFORM_FETCHER_IMAGE_MARKERS = ("hf-weights-fetcher",)
_MODEL_CACHE_RO_PATH_DEFAULTS = (
    "/var/lib/mod/hf-cache",
    "/var/lib/mod/models",
    "/var/lib/mod/model-cache",
)
_MODEL_CACHE_RO_MODES = ("warn", "enforce")
# The platform tenant that OWNS the flat writable cache (same constant
# services/managed_ai_stacks.py uses for R-159); its stacks are exempt.
_PLATFORM_TENANT_ID = 1


def _model_cache_ro_setting(db=None):
    """(mode, paths) from the platform-settings registry. Any read failure
    degrades to (warn, defaults) — a broken setting must never turn a
    safety check off, and must never refuse a live deploy on its own."""
    from services.platform_settings import get_setting
    mode, paths = "warn", list(_MODEL_CACHE_RO_PATH_DEFAULTS)
    try:
        raw_mode = get_setting("stacks.model_cache_ro_mode", db=db)
        if isinstance(raw_mode, str) and raw_mode in _MODEL_CACHE_RO_MODES:
            mode = raw_mode
    except Exception:  # noqa: BLE001
        pass
    try:
        raw_paths = get_setting("stacks.model_cache_ro_paths", db=db)
        if isinstance(raw_paths, list) and raw_paths:
            paths = [str(p) for p in raw_paths if str(p).strip()]
    except Exception:  # noqa: BLE001
        pass
    return mode, paths


def _is_model_cache_host_path(src: str, paths) -> bool:
    """True when a bind source IS (or sits under) a known cache host path."""
    if not src.startswith("/"):
        return False
    for p in paths:
        root = p.rstrip("/")
        if src == root or src.startswith(root + "/"):
            return True
    return False


def _is_model_cache_fetcher(svc: dict) -> bool:
    """The ONE exemption rule: an explicit `x-mod.model_cache_writable: true`
    on the service (MOD's own hf-weights-fetcher image counts as such)."""
    xmod = svc.get("x-mod")
    if isinstance(xmod, dict) and xmod.get(_MODEL_CACHE_RO_FLAG) is True:
        return True
    image = str(svc.get("image") or "")
    return any(m in image for m in _PLATFORM_FETCHER_IMAGE_MARKERS)


def _model_cache_ro_findings(spec, *, tenant_id=_TENANT_UNKNOWN,
                             db=None) -> list:
    """Violations: [(service, mount, mode)] for writable cache mounts. Empty
    when the spec is exempt (system/platform stack) or clean."""
    # Exempt: a caller that did not name the owning tenant (the internal
    # platform renderers — managed-AI / catalog / wizard — which build
    # platform stacks), an explicit system/platform stack (tenant None), and
    # the platform tenant itself, which owns the flat writable cache (R-159).
    if tenant_id is _TENANT_UNKNOWN or tenant_id is None:
        return []
    if int(tenant_id) == _PLATFORM_TENANT_ID:
        return []
    mode, paths = _model_cache_ro_setting(db=db)
    findings = []
    for name, svc in (spec.get("services") or {}).items():
        if not isinstance(svc, dict) or _is_model_cache_fetcher(svc):
            continue
        for mount in (svc.get("volumes") or []):
            if isinstance(mount, str):
                parts = mount.split(":")
                src = parts[0]
                ro = any(p == "ro" or p.endswith(":ro") for p in parts[1:])
            elif isinstance(mount, dict):
                src = mount.get("source") or ""
                ro = bool(mount.get("read_only"))
            else:
                continue
            if src and _is_model_cache_host_path(src, paths) and not ro:
                findings.append((name, str(src), mode))
    return findings


def _model_cache_ro_check(spec, *, tenant_id=_TENANT_UNKNOWN, db=None) -> None:
    """Apply the mode: warn = log + audit and proceed; enforce = 422."""
    findings = _model_cache_ro_findings(spec, tenant_id=tenant_id, db=db)
    if not findings:
        return
    mode = findings[0][2]
    first_service, first_path, _ = findings[0]
    logger.warning(
        "FX.80 p4b: %d writable model-cache mount(s) in a tenant stack "
        "(mode=%s): %s mounts %s",
        len(findings), mode, first_service, first_path)
    try:
        db.add(models.AuditEvent(
            tenant_id=(tenant_id if tenant_id is not _TENANT_UNKNOWN else None),
            user_id=None,
            event_name=_MODEL_CACHE_RO_EVENT,
            details={"mode": mode,
                     "violations": [{"service": s, "path": p}
                                    for s, p, _ in findings]}))
        db.flush()
    except Exception as e:  # noqa: BLE001
        logger.warning("FX.80 p4b audit write failed: %s", e)
    if mode == "enforce":
        raise HTTPException(
            status_code=422,
            detail=(f"service {first_service!r} mounts the shared model cache "
                    f"{first_path!r} writable; a tenant's model-cache mount "
                    "must be read-only (:ro). Set x-mod.model_cache_writable: "
                    "true on a downloader service that must write it."))


def _validate_compose(spec, *, is_admin: bool = True,
                      allowed_host_mount_prefixes=(),
                      secret_prefix: Optional[str] = None,
                      trust_admin_ack: bool = False,
                      tenant_id=_TENANT_UNKNOWN,
                      db=None) -> None:
    """add-persistent-stacks §7.3 + delegated-stack-authoring §3 — pre-deploy
    compose validation. Rejects a structurally-invalid spec (422) at create time,
    AND — for a delegated author (is_admin False) — enforces the host-escape
    safety boundary. Admin callers (default is_admin True) keep prior latitude
    apart from the hard refusals.

    `trust_admin_ack` (§3.5) gates whether a service's `x-mod.admin_ack` is
    honored to exempt it from the admin-only-construct refusal. Leave it False
    (the default) whenever `spec` is fresh, caller-supplied input — create,
    from-wizard, assemble — so a delegated author can never plant the marker
    themselves. Pass True only where `spec` is the STORED compose_spec being
    re-validated (the deploy-time gate), since only the admin-only
    acknowledge endpoint ever writes it there."""
    if not isinstance(spec, dict):
        raise HTTPException(status_code=422, detail="compose_spec must be an object")
    # 13.5a — normalize compose-native secret forms (${secret:NAME} env values,
    # top-level `secrets:` + service `secrets:` file lists) into the x-mod.secrets
    # named-ref shape BEFORE validation, so every downstream consumer
    # (secret_refs, deploy resolution) sees one shape. Mutates in place.
    from services.stack_tenant_secrets import (
        normalize_tenant_secret_refs)
    from services.stack_tenant_secrets import TenantSecretNormalizationError
    try:
        normalize_tenant_secret_refs(spec)
    except TenantSecretNormalizationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    services = spec.get("services")
    if not isinstance(services, dict) or not services:
        raise HTTPException(status_code=422, detail="compose_spec.services must be a non-empty object")
    # GPUCAP P1b — the GPU CARD pin (`x-mod.gpu_pin`, the top-level compose
    # extension written by the admin-only PUT /stacks/{id}/gpu-pin) is admin-set
    # ONLY. Without this guard a delegated STACK_AUTHOR could type a `gpu_pin`
    # into the definition editor / import-compose / wizard and self-pin cards —
    # and a forged card id there is an oracle for probing other tenants'
    # hardware. This is the SAME shared gate every authoring path runs
    # (create/from-template, from-wizard, assemble, import-compose/finalize,
    # PUT .../definition), so no authoring route can smuggle a pin past it.
    # `trust_admin_ack` is True ONLY where `spec` is the STORED compose_spec
    # being re-validated at deploy (services/persistent_stacks_domain_service
    # deploy gate) — there the pin already passed the admin endpoint, so
    # re-validating must not blow the deploy. Fresh caller-supplied input always
    # arrives with the default False, so an author's pin is refused here.
    if not is_admin and not trust_admin_ack:
        _top_xmod = spec.get("x-mod")
        if isinstance(_top_xmod, dict) and _top_xmod.get("gpu_pin"):
            raise HTTPException(
                status_code=422,
                detail="x-mod.gpu_pin can only be set by a platform admin "
                       "(PUT /stacks/{id}/gpu-pin)")
    for name, svc in services.items():
        if not isinstance(svc, dict):
            raise HTTPException(status_code=422, detail=f"service {name!r} must be an object")
        if not (svc.get("image") or svc.get("build")):
            raise HTTPException(status_code=422,
                                detail=f"service {name!r} needs an 'image' or 'build'")
        _validate_service_security(name, svc, is_admin, allowed_host_mount_prefixes,
                                   trust_admin_ack=trust_admin_ack)
    vols = spec.get("volumes")
    if vols is not None and not isinstance(vols, dict):
        raise HTTPException(status_code=422, detail="compose_spec.volumes must be an object")
    # named volumes referenced by services must be declared at the top level
    declared = set((vols or {}).keys())
    for name, svc in services.items():
        for mount in (svc.get("volumes") or []):
            if isinstance(mount, str) and ":" in mount:
                src = mount.split(":")[0]
                if src and not src.startswith((".", "/")) and src not in declared:
                    raise HTTPException(
                        status_code=422,
                        detail=f"service {name!r} mounts named volume {src!r} not declared in top-level volumes")
    # Accelerator declarations — validated here so a malformed one is a 422 at
    # create, alongside the missing-image and undeclared-volume checks, rather
    # than an unplaceable stack discovered at deploy.
    for name, svc in services.items():
        xmod = svc.get("x-mod")
        if isinstance(xmod, dict) and "accelerator" in xmod:
            _validate_accelerator_block(name, xmod["accelerator"])
        if isinstance(xmod, dict) and "egress" in xmod:
            _validate_egress_block(name, xmod["egress"])
        if isinstance(xmod, dict) and "reachable" in xmod and not isinstance(xmod["reachable"], bool):
            raise HTTPException(status_code=422,
                                detail=f"service {name!r}: x-mod.reachable must be a boolean")
        if isinstance(xmod, dict) and "secrets" in xmod:
            _validate_secrets_block(name, xmod["secrets"], secret_prefix)
        if isinstance(xmod, dict) and "tls" in xmod:
            _validate_tls_block(name, xmod["tls"])
    # FX.80 p4b — shared model-cache host paths must be read-only in a
    # tenant's stack (warn = log+audit, enforce = 422). Runs LAST so a
    # structurally-broken spec still 422s on its real problem first, and so
    # warn mode never blocks the other checks.
    _model_cache_ro_check(spec, tenant_id=tenant_id, db=db)




class _AcknowledgeHostAccessRequest(BaseModel):
    service: str
    reason: Optional[str] = None








# --- node -> managed-stack bindings for the WB node-stack overlay (D12) ------

class NodeStackBinding(BaseModel):
    """One node_kind's tie to a managed stack + that stack's live state."""
    stack_name: str
    family: str  # "nim" | "vlm" | "sdxl" | "llm" | "generic"
    state: str   # "running" | "asleep" | "waking" | "waiting" | "unknown"


class ManagedStackState(BaseModel):
    """One managed PersistentStack's family + live state, keyed by stack NAME.
    Lets the WB overlay resolve a PER-INSTANCE `requires_stack` (a stack the user
    assigned to one node, keyed by name, not node_kind)."""
    family: str  # "nim" | "vlm" | "sdxl" | "llm" | "generic"
    state: str   # "running" | "asleep" | "waking" | "waiting" | "unknown"


class NodeBindingsResponse(BaseModel):
    bindings: Dict[str, NodeStackBinding]
    # Every managed PersistentStack, keyed by NAME, for per-instance requires_stack
    # resolution in the WB overlay. Defaulted so older clients ignoring it are fine.
    stacks: Dict[str, ManagedStackState] = {}


def _stack_backed_node_kinds() -> set:
    """Every node_kind known to resolve to a managed stack. Reuses the SAME
    source maps stack_wake.resolve_required_stack consults (D2) — NIM kinds from
    nim_ephemeral.NIM_NODE_KINDS, VLM/SDXL from the brand kind maps, plus the
    served/wizard LLM keys — so this never invents a parallel mapping.
    Defensive: a missing/broken source contributes nothing rather than raising."""
    kinds: set = set()
    try:
        from services import nim_ephemeral as _nim
        kinds |= set(_nim.NIM_NODE_KINDS.keys())
    except Exception:  # noqa: BLE001
        logger.debug("node-bindings: NIM kind enumeration failed", exc_info=True)
    try:
        from services import brand_image_stacks as _brand
        kinds |= set(_brand.VLM_CLIENT_NODE_KINDS)
        kinds |= set(_brand.SDXL_CLIENT_STACK_BY_KIND.keys())
    except Exception:  # noqa: BLE001
        logger.debug("node-bindings: brand kind enumeration failed", exc_info=True)
    # The served/wizard LLM keys stack_wake resolves (all map to one stack).
    kinds |= {"wizard-llm", "wizard_llm", "llm-call", "llm_call"}
    return kinds


def _derive_stack_state(db, ref, row) -> str:
    """Map a PersistentStack row to the overlay's state vocabulary:
      * running  — status is actively serving (ACTIVE_STACK_STATUSES);
      * waking   — desired_state=running but not yet serving;
      * waiting  — off, but a task needing it is held (queue lookahead);
      * asleep   — desired_state=stopped / otherwise not running;
      * unknown  — no row, or any lookup failure.
    Never raises."""
    if row is None:
        return "unknown"
    try:
        from services import stack_wake
        active = set(stack_wake._active_statuses())
        status = getattr(row, "status", None)
        desired = getattr(row, "desired_state", None)
        if status in active:
            return "running"
        if desired == "running":
            return "waking"
        # Off / stopped: distinguish "held for capacity" from plain asleep.
        try:
            if stack_wake._stack_has_inflight_task(db, ref):
                return "waiting"
        except Exception:  # noqa: BLE001
            logger.debug("node-bindings: inflight check failed for %s",
                         getattr(ref, "stack_name", "?"), exc_info=True)
        return "asleep"
    except Exception:  # noqa: BLE001
        logger.debug("node-bindings: state derivation failed", exc_info=True)
        return "unknown"




# --- §8.2 rebalancer dial + master enable (admin-tunable) ------------------

class _RebalancerThreshold(BaseModel):
    # Both optional: the settings UI toggles `enabled` without needing to
    # resend the threshold, and the threshold dial saves without touching
    # the enable.
    threshold: Optional[float] = None
    enabled: Optional[bool] = None


class _ReconcilerConfig(BaseModel):
    # Both optional so the Health & recovery card can flip either checkbox
    # independently. `enabled` = run the reconciler at all; `live_mode` = let
    # it actually act (off = dry-run, reports only).
    enabled: Optional[bool] = None
    live_mode: Optional[bool] = None




def _unrunnable_reason_for(db: Session, stack) -> Optional[str]:
    """The shortfall the placement sweep already recorded, if there is one.

    §7.3 says reuse this rather than inventing a parallel explanation, and the
    reason is that the sweep runs against real queued work: it knows why THIS
    dispatch could not be placed, which is more specific than anything derived
    from a capacity snapshot after the fact. The snapshot below is the fallback
    for a stack that was never dispatched at all.
    """
    from services import persistent_stacks_shared_service as _svc
    return _svc.unrunnable_reason_for(db, stack)


# The manager's own accelerator reaches the fleet through this worker: the
# embedded worker auto-registers as LICENSED on first heartbeat and carries the
# manager card's hardware_profile_id.
# BUG-EMBEDDED-WORKER-UI — on CE/on-prem the SAME on-manager embedded worker
# runs under its own name (open-core/overlay/docker-compose.yml:949
# `WORKER_NAME=mod-worker-ce`), so the SaaS literal alone never matches a CE
# install and the manager card dropped out of fleet capacity there. The pair is
# main._EMBEDDED_WORKER_SEED_NAMES; mirrored here (not imported) so this module
# keeps its standalone-import guarantee.
_EMBEDDED_WORKER_NAME = "mod-worker-embedded"
_EMBEDDED_WORKER_NAMES = (_EMBEDDED_WORKER_NAME, "mod-worker-ce")


def _fleet_capacity_workers(db: Session) -> list:
    """Workers whose accelerators count toward fleet capacity.

    LICENSED workers, PLUS the manager's embedded worker regardless of status
    (system-managed-ai §1). A manager hosting a GPU is the reason a
    manager-local model server exists at all, and that card must stay visible
    even when the embedded worker is not currently licensed — it is otherwise
    the one accelerator the budget options this feeds cannot see. Deduped by
    worker_name so a licensed embedded worker is never counted twice; committed
    capacity is still excluded downstream by `capacity_report` /
    `available_devices`, so a card a running stack already holds is not offered
    a second time.
    """
    from crud import persistent_stacks_shared as _crud
    workers = _crud.get_licensed_workers(db)
    for _name in _EMBEDDED_WORKER_NAMES:
        if not any(w.worker_name == _name for w in workers):
            embedded = _crud.get_worker_by_name(db, _name)
            if embedded is not None:
                workers.append(embedded)
    return workers


def _fleet_worker_devices(db: Session, exclude_stack_ids=None) -> list:
    """One `{kind: [free_vram_gb, ...]}` map per licensed worker.

    Per worker and NOT merged: a model loads into one device on one host, so a
    fleet-wide union would happily claim a 22 GB model fits across two 16 GB
    cards in different boxes. Same source and same exclusions
    `_managed_ai_placement` uses, so the kinds chosen here and the placement
    decided there cannot disagree.
    """
    from utils.accelerator_fit import available_devices

    out = []
    workers = _fleet_capacity_workers(db)
    for w in workers:
        try:
            # The whole managed-AI rung is being replaced by this apply, so
            # every stack in it is excluded — each for the others, not merely
            # for itself. `_exclusion_set` takes the list, which is the same
            # call `_managed_ai_placement` makes a few lines down.
            devices = available_devices(db, w, exclude_stack_id=exclude_stack_ids)
        except Exception:  # noqa: BLE001
            logger.debug("device inventory unavailable for worker %s",
                         getattr(w, "worker_name", "?"), exc_info=True)
            continue
        if devices:
            out.append(devices)
    return out


def _managed_ai_placement(db: Session, demand: dict, preset: str,
                          exclude_stack_ids=None) -> dict:
    """Can anything in this fleet host `demand`, and if not, by how much does it miss?

    §7.3/§7.4. "Unplaceable" on its own is the least useful true statement
    available: it does not say whether the gap is 2 GB or 20, whether a card
    exists at all, or whether a smaller tier would run today. This deployment's
    16 GB card cannot host the 22.5 GB reasoner, and an operator should be able
    to read that off the screen rather than infer it.
    """
    from utils.accelerator_fit import available_devices, satisfies

    out = {"placeable": True, "demand": demand, "fits_on": [],
           "largest_available_gb": None, "shortfall_gb": None,
           "kind": None, "fits_instead": None, "reason": None}
    if not demand:
        return out

    # {kind: [per-device minimums]}. The explanation below mirrors
    # accelerator_fit.satisfies() exactly, which has THREE distinct ways to
    # fail, and conflating them sends an operator to the wrong fix:
    #
    #   1. no device of that kind at all      -> buy/enrol the hardware
    #   2. largest single demand > biggest card -> need a bigger card
    #   3. total demand > total free VRAM       -> need more cards, or free some
    #
    # Note (3) exists because processes DO co-tenant a card: two 20 GB models
    # fit one 48 GB device. Reporting that as "needs two devices" would be
    # wrong, and is what an earlier draft of this did.
    kind = next(iter(demand), None)
    out["kind"] = kind
    wanted = [float(v) for v in (demand.get(kind) or [])]
    required = max(wanted) if wanted else 0.0
    out["required_gb"] = required
    out["total_required_gb"] = round(sum(wanted), 1)
    out["devices_required"] = len(wanted)

    workers = _fleet_capacity_workers(db)

    largest = 0.0          # biggest single free device anywhere in the fleet
    best_pool = 0.0        # most total free VRAM on any ONE worker
    most_devices = 0
    for w in workers:
        try:
            # Excluding a rung's OWN stacks answers "could this rung fit",
            # rather than "could a second copy of it fit beside the one already
            # running" — which is what an operator reads a placement verdict as.
            devices = available_devices(db, w, exclude_stack_id=exclude_stack_ids)
        except Exception as e:  # noqa: BLE001
            logger.debug("available_devices failed for %s: %s", w.worker_name, e)
            continue
        free = [float(v or 0) for v in (devices.get(kind) or [])]
        if free:
            largest = max(largest, max(free))
            best_pool = max(best_pool, sum(free))
            most_devices = max(most_devices, len(free))
        if satisfies(devices, demand):
            out["fits_on"].append(w.worker_name)

    out["largest_available_gb"] = round(largest, 1) if workers else None
    out["total_available_gb"] = round(best_pool, 1) if workers else None
    out["devices_available"] = most_devices
    out["placeable"] = bool(out["fits_on"])
    if out["placeable"]:
        return out

    if largest <= 0:
        out["reason"] = (
            f"No worker in this deployment reports a free {kind} device, so "
            f"there is nothing for this stack to land on.")
    elif required > largest:
        out["shortfall_gb"] = round(required - largest, 1)
        out["reason"] = (
            f"Needs {required:g} GB of {kind}; the largest free device in this "
            f"fleet is {round(largest, 1):g} GB — short by "
            f"{out['shortfall_gb']:g} GB.")
    else:
        # Each part fits a card, but not all of them at once on one worker.
        # A stack is one compose project on one host, so the services cannot
        # be spread out — the fix is more free VRAM on a single worker, not a
        # bigger single card.
        out["shortfall_gb"] = round(sum(wanted) - best_pool, 1)
        out["reason"] = (
            f"Each service fits a card, but the stack needs "
            f"{round(sum(wanted), 1):g} GB of {kind} in total and the best "
            f"single worker has {round(best_pool, 1):g} GB free across "
            f"{most_devices} device(s) — short by {out['shortfall_gb']:g} GB. "
            f"A stack is one compose project on one host, so this cannot be "
            f"split across workers.")

    # §7.4 — name a rung that WOULD run here, if one would. Silence invites the
    # conclusion that managed AI is simply unavailable on this hardware, when
    # the honest answer is usually "not at this tier".
    out["fits_instead"] = _largest_fitting_tier(db, preset, workers)
    return out


def _largest_fitting_tier(db: Session, preset: str, workers: list) -> Optional[dict]:
    """The best rung below `preset` whose demand this fleet can satisfy."""
    from utils.accelerator_fit import available_devices, satisfies
    # GT.D6 DS-3 — managed_ai_stacks is Paid: on CE there is no rung ladder to
    # suggest, so placement simply omits `fits_instead` (# CE-safe).
    from utils.paid_import import optional_paid
    mas = optional_paid("services.managed_ai_stacks")
    if mas is None:
        return None
    try:
        from routers.ai_config import PRESET_LADDER
    except ImportError:
        return None  # CE — `PRESET_LADDER` ships in paid-only routers.ai_config

    try:
        below = list(PRESET_LADDER[:PRESET_LADDER.index(preset)])
    except ValueError:
        return None

    device_cache = []
    for w in workers:
        try:
            device_cache.append((w.worker_name, available_devices(db, w)))
        except Exception:  # noqa: BLE001
            continue

    for candidate in reversed(below):          # richest rung that still fits
        try:
            cand_defs = mas.definitions(preset=candidate)
        except Exception as e:  # noqa: BLE001 — e.g. unsupported accelerator
            logger.debug("definitions failed for preset %s: %s", candidate, e)
            continue
        demands = [_aggregate_accelerator_demand(
            _parse_accelerator_requirements(d["compose_spec"])) for d in cand_defs]
        # A CPU rung declares no demand at all, which trivially fits anywhere.
        if all(not dm for dm in demands):
            return {"preset": candidate, "reason": "runs on CPU, no accelerator needed"}
        if all(any(satisfies(devs, dm) for _, devs in device_cache)
               for dm in demands if dm):
            return {"preset": candidate, "reason": "fits the free capacity in this fleet"}
    return None




def _host_device_summary(db: Session, worker_name: str,
                         kind: Optional[str]) -> str:
    """What one host actually offers of `kind`, phrased for an operator.

    "Cannot host it" is not actionable on its own. Whether the host has no such
    device at all or merely a smaller one sends the reader to two different
    fixes — enrol different hardware, or free/choose a smaller rung.
    """
    from utils.accelerator_fit import available_devices
    from crud import persistent_stacks_shared as _crud

    w = _crud.get_worker_by_name(db, worker_name)
    if w is None:
        return "that host is not enrolled in this deployment"
    try:
        free = [float(v or 0) for v in (available_devices(db, w).get(kind) or [])]
    except Exception:  # noqa: BLE001 — a summary must never break the refusal
        logger.debug("locked-host summary: inventory unreadable for %s",
                     worker_name, exc_info=True)
        return "its accelerator inventory could not be read"
    if not free:
        return f"that host reports no {kind} device at all"
    return f"its largest free {kind} device is {round(max(free), 1)} GB"


def _redeploy_host(stack) -> Optional[str]:
    """The one host a redeploy must land on, or None to let placement choose.

    Two locks, and they are not the same thing.

    `pinned_worker` is a decision an operator made and can unmake, so it binds
    unconditionally.

    `target_worker` binds ONLY while the stack actually holds that host. A
    redeploy is pinned to the host it already occupies because the claim-time
    guard would otherwise reject it against its OWN live VRAM reservation on
    that card — a reason that exists exactly as long as something is running to
    hold the VRAM, which is what `ACTIVE_STACK_STATUSES` means. A stopped,
    fenced or torn-down stack holds nothing, so binding it to the host it last
    ran on is not caution; it is a lock nobody set. On 2026-08-12 that kept two
    fenced stacks bound to a CUDA box after their pin had been lifted, so
    unpinning them changed nothing and read as the fix not working.

    ONE function, both callers — the guard that refuses and the dispatch that
    targets must never be able to disagree about which host is meant. Two
    answers to that question is the defect this whole area was fixed for.
    """
    from utils.accelerator_fit import ACTIVE_STACK_STATUSES

    pinned = getattr(stack, "pinned_worker", None)
    if pinned:
        return pinned
    if getattr(stack, "status", None) in ACTIVE_STACK_STATUSES:
        return getattr(stack, "target_worker", None)
    return None


def _locked_host_rejection(db: Session, stack, verdict: Optional[dict],
                           preset: str) -> Optional[str]:
    """Why a redeploy cannot land, when the stack is locked to ONE host.

    `_managed_ai_placement` answers "can anything in this fleet host this",
    which is not the question a locked stack asks. A stack pinned by an
    operator — or redeployed onto the host already running it — has exactly one
    candidate, so a fleet-wide yes and a this-host no are both true at once and
    only the second decides whether the dispatch can land. The reconciler has
    always drawn this distinction (`stack_reconciler._pinned_worker_can_host`);
    apply did not, and dispatched to the lock while quoting the fleet's verdict.

    Refusing here rather than dispatching is the whole point. A task assigned to
    a host that cannot satisfy its `accel_demand` is never CLAIMED, so no claim
    expires, the reaper has nothing to reap, and the stack reads `deploying` for
    ever. Silent, unbounded, and indistinguishable from a broken platform — the
    operator who set the pin days earlier has no way to connect the two.
    """
    if not verdict or verdict.get("placeable") is False:
        return None                      # no demand, or already refused above
    host = _redeploy_host(stack)
    if not host:
        return None                      # unlocked: the claim-time guard places it
    fits_on = verdict.get("fits_on") or []
    if host in fits_on:
        return None
    pinned = getattr(stack, "pinned_worker", None)

    kind = verdict.get("kind")
    need = verdict.get("required_gb")
    have = _host_device_summary(db, host, kind)
    elsewhere = f" It fits on {', '.join(fits_on)}." if fits_on else ""

    # The two locks mean different things and have different remedies. A pin is
    # a decision someone made and can unmake; target_worker is just where the
    # stack happens to be running, and the way out is to stop it there.
    if pinned:
        at = getattr(stack, "pinned_at", None)
        when = f" on {at:%Y-%m-%d}" if at else ""
        remedy = (f" Unpin it (pinned to {pinned}{when}) or choose a tier "
                  f"{host} can run.")
    else:
        # Name the state rather than asserting "running". This branch is reached
        # for any status that holds the card, and telling an operator a fenced
        # stack is running sends them looking for a container that is not there.
        state = getattr(stack, "status", None) or "active"
        remedy = (f" It is {state} on {host}, which cannot take this tier — tear "
                  f"it down so it can be placed elsewhere.")

    return (f"{stack.name} is locked to {host}, which cannot run the {preset} "
            f"tier: it needs a {kind} device with {need} GB and {have}."
            f"{elsewhere}{remedy}")


def _relocatable_from_locked_host(stack, verdict: Optional[dict]) -> bool:
    """May a tier apply auto-MOVE this stack off the host it is bound to?

    Reached only once `_locked_host_rejection` has already established that the
    stack's current host cannot run the new tier. The question here is which of
    the two locks we are looking at, because they carry different intent:

      * `target_worker` — merely where the stack HAPPENS to be running. Nobody
        chose it; it is an artefact of a past placement. When another worker in
        the fleet can host the new tier, the right answer is to move the stack
        there (tear down on the old host, redeploy unpinned so the claim-time
        guard lands it on the capable worker) — not to make the operator tear
        it down by hand and re-apply. That dead-end is the defect this fixes.

      * `pinned_worker` — a decision an operator deliberately made. Silently
        relocating a pinned stack would override that choice and could move it
        out of the placement the operator intended, so a pin is left to REFUSE
        with its `Unpin it (…)` remedy. The operator lifts the pin themselves.

    Only relocatable when the fleet actually has somewhere for it to go
    (`fits_on` non-empty); with no eligible worker the caller keeps the clear
    refusal unchanged.
    """
    if getattr(stack, "pinned_worker", None):
        return False
    return bool((verdict or {}).get("fits_on"))


class ManagedAIApplyResponse(BaseModel):
    preset: str
    accelerator_kind: Optional[str] = None
    created: List[str] = []
    updated: List[str] = []
    deployed: List[str] = []
    skipped: List[Dict[str, Any]] = []
    already_correct: List[str] = []  # §2.2/§4.3 — running the current spec; not redeployed
    message: str




def apply_managed_ai_core(db: Session, user: models.User,
                          preset: Optional[str] = None,
                          source: str = "auto") -> ManagedAIApplyResponse:
    """GT.D6 DS-3 — thin guarded re-export. The managed-AI apply core moved to
    `services/persistent_stacks_paid_service.py` (it is a Paid feature; it reads
    `managed_ai_stacks` and the paid platform state). Existing callers keep
    importing this name from `_shared`; on CE the paid module is absent and
    `utils.paid_import.require_paid` raises HTTP 402 (upgrade path) instead of
    running paid logic or crashing with an ImportError 500."""
    from utils.paid_import import require_paid
    _paid = require_paid("services.persistent_stacks_paid_service",
                         "Managed-AI stack apply")
    return _paid.apply_managed_ai_core(db, user, preset, source=source)



def _best_fleet_preset(db: Session) -> Optional[str]:
    """Highest managed-AI tier the CURRENT fleet can plausibly host, or None.

    Walks presets largest→smallest and returns the first whose per-device VRAM
    floor fits the fleet's biggest free card AND whose required card count is
    available. Only a CANDIDATE: apply_managed_ai_core re-runs real placement
    and deploys nothing it can't actually fit, so an optimistic pick is safe.
    A CPU-only fleet returns None — the auto-reconcile never forces a GPU tier
    onto hardware that has no GPU.
    """
    from utils.paid_import import optional_paid
    mas = optional_paid("services.managed_ai_stacks")
    if mas is None:
        return None  # CE — managed-AI is Paid; no tier to auto-pick
    max_vram, total_cards = 0.0, 0
    for per_worker in _fleet_worker_devices(db):
        for _kind, frees in (per_worker or {}).items():
            vals = [float(v or 0) for v in (frees or [])]
            total_cards += len(vals)
            if vals:
                max_vram = max(max_vram, max(vals))
    if max_vram <= 0:
        return None
    for preset in ("production", "balanced", "compact", "minimal"):
        floor = mas._PRESET_MIN_VRAM_GB.get(preset)
        if floor is None:
            continue
        try:
            need_cards = mas.cards_required(preset)
        except Exception:  # noqa: BLE001
            need_cards = 1
        if floor <= max_vram and need_cards <= max(total_cards, 1):
            return preset
    return None


def reconcile_managed_ai(db: Session) -> Optional[str]:
    """Deploy-if-absent auto-provisioning of the managed LLM.

    Runs from the persistent-stack reconciler tick (the retry backbone) and on
    worker enrollment, so standing up a GPU worker brings the tier up on its
    own. Deliberately conservative — it acts ONLY when there is nothing to
    disturb:

      * managed-AI-as-stacks is enabled,
      * a platform admin exists (owns the platform stacks; before the first
        signup there is nobody to own them and nothing is using AI yet),
      * NO managed-AI stack already exists ("nothing is showing"),
      * the operator did not HAND-PICK CPU (managed state source=manual + cpu),
      * the fleet actually offers a GPU tier.

    When all hold it applies the best-fit tier through the same stack path a
    human Apply uses; the stack reconciler then retries the deploy until the
    worker has pulled the models. Once the stacks exist this returns early — it
    never churns and never downgrades. Upgrading a RUNNING tier when a bigger
    card is later added is intentionally out of scope here (that means replacing
    a live model) and stays a manual Apply. Returns a status string or None.
    """
    from utils.paid_import import optional_paid
    mas = optional_paid("services.managed_ai_stacks")
    if mas is None:
        return None  # CE — managed-AI auto-provisioning is Paid
    from crud import persistent_stacks_shared as _crud
    if not mas.enabled():
        return None
    if _crud.get_first_stack_by_names(db, mas.ALL_STACK_NAMES):
        return None  # something already provisioned — leave it alone
    owner = _crud.get_platform_admin_owner(db, 1)
    if owner is None:
        return None  # pre-first-signup: nobody to own it, nobody using AI yet
    try:
        from routers.ai_config import _read_vault_path, _MANAGED_STATE_PATH
        state = _read_vault_path(_MANAGED_STATE_PATH) or {}
        if state.get("source") == "manual" and str(state.get("preset") or "").lower() == "cpu":
            return None  # operator deliberately chose CPU — do not override
    except Exception:  # noqa: BLE001 — unreadable state → treat as unset
        pass
    preset = _best_fleet_preset(db)
    if not preset or preset == "cpu":
        return None  # no GPU tier the fleet can host
    try:
        res = apply_managed_ai_core(db, owner, preset, source="auto")
        logger.info("managed-AI reconcile: auto-deployed tier=%s (%s)",
                    preset, getattr(res, "message", ""))
        return getattr(res, "message", "applied")
    except Exception:  # noqa: BLE001 — best-effort; the reconciler tick retries
        logger.warning("managed-AI reconcile: auto-deploy failed (retries next tick)",
                       exc_info=True)
        return None














def _wizard_catalog_lines() -> str:
    """One line per service-template catalog entry (id, description, params +
    defaults), generated from `services.service_templates.STACK_TEMPLATES` (via
    the `STACK_TEMPLATES` re-export above) — GA.ASW.3: the wizard prompt is
    grounded on the SAME catalog `/from-template` renders from, so a newly
    added `service_templates/*.yaml` file shows up here with no code change."""
    lines = []
    for tid in sorted(STACK_TEMPLATES):
        tpl = STACK_TEMPLATES[tid]
        params = tpl.get("params") or []
        if params:
            params_txt = ", ".join(
                f"{p['key']} (default: {p.get('default')!r})" for p in params
            )
        else:
            params_txt = "none"
        lines.append(f"- {tid}: {tpl.get('description', '')} | params: {params_txt}")
    return "\n".join(lines)


# GA.ASW.3 — grounded on the service-template catalog: the wizard is asked to
# pick a catalog template_id (+ params) FIRST, and only fall back to a raw
# custom compose (the old, only, behaviour) when nothing in the catalog fits.
# `create_from_wizard` (persistent_stacks.py) renders a matched template_id
# through the SAME `_render_template` call `/from-template` uses, so a
# grounded wizard result behaves identically to picking that template by hand.
_WIZARD_SYSTEM = (
    "You turn a plain-language request into a persistent stack. Return ONLY a "
    "JSON object, no prose.\n\n"
    "First check if one of these catalog templates fits the request:\n"
    f"{_wizard_catalog_lines()}\n\n"
    "If one fits, return exactly "
    '{"template_id": "<catalog id>", "params": {"<key>": "<value>", ...}} '
    "using only param keys from that template's list above; omit a param to "
    "use its default — do not invent params, and do not invent a template_id "
    "that is not in the catalog above.\n\n"
    "If NOTHING in the catalog fits, return "
    '{"template_id": "custom", "compose": <a docker-compose spec dict with a '
    '"services" map (each service has an "image", and "volumes" like '
    '["data:/path"] for stateful services) and an optional top-level '
    '"volumes" map>, "storage_class": "local_fs" if the stack has named '
    'volumes holding state (databases, indexes), otherwise "s3_native"}.'
)












# delegated-stack-authoring §4.3 — the egress OBSERVATION side, distinct from
# the DECLARATION recorded at create (§4.1/§4.2, `PersistentStack.egress_policy`).
# A declaration says what a stack MAY reach; an observation says what it WAS
# SEEN reaching. Neither Group 5 (superseded to the mesh) nor its replacement
# is wired to report live traffic yet, so this is the recording side only —
# append-only, no new table (an AuditEvent row per reach, same store the rest
# of this router already audits into) — so group 6.1's pre-flip review has
# real evidence instead of a guess, and group 6.2's default-deny flip has a
# blast radius that was MEASURED before it was enabled.
_EGRESS_OBSERVED_EVENT = "STACK_EGRESS_OBSERVED"


def _destination_within_declared(stack: "models.PersistentStack", service: str,
                                 destination: str) -> Optional[bool]:
    """Whether `destination` falls inside `service`'s DECLARED egress posture.

    None when the service has no declaration at all — legal pre-flip (§4.5) —
    so a caller can tell "nothing to compare against" apart from an actual
    violation. False on a `none`-posture service (declares zero reach, so any
    observed destination is a mismatch) or a `hosts`-posture service reaching
    somewhere off its allow-list. `api`-posture is matched loosely (the
    destination label an observer uses for the manager API varies by source),
    since a false positive here only under-counts violations, never invents one.
    """
    policy = stack.egress_policy if isinstance(stack.egress_policy, dict) else {}
    entry = policy.get(service)
    if not isinstance(entry, dict):
        return None
    mode = entry.get("mode")
    if mode == "none":
        return False
    if mode == "hosts":
        return destination in (entry.get("hosts") or [])
    if mode == "api":
        return destination.lower() in ("api", "manager-api", "mod-api",
                                       os.getenv("MOD_MESH_API_TAG", "").lower())
    return None


def _record_egress_observations(db: Session, stack: "models.PersistentStack",
                                observations, user: Optional[models.User] = None) -> List[dict]:
    """Persist a batch of `{service, destination}` reach reports for `stack` as
    AuditEvent rows, each stamped with the within_declared verdict at write
    time. Malformed entries are skipped rather than failing the whole batch —
    one bad report must not lose the rest, the same posture `stack_observations`
    takes toward worker-reported data. Returns the recorded rows."""
    out: List[dict] = []
    for obs in observations or []:
        service = obs.get("service") if isinstance(obs, dict) else None
        destination = obs.get("destination") if isinstance(obs, dict) else None
        if not (isinstance(service, str) and service.strip()
                and isinstance(destination, str) and destination.strip()):
            continue
        details = {
            "stack_id": stack.id,
            "service": service,
            "destination": destination,
            "within_declared": _destination_within_declared(stack, service, destination),
        }
        db.add(models.AuditEvent(tenant_id=stack.tenant_id,
                                 user_id=(user.id if user is not None else None),
                                 event_name=_EGRESS_OBSERVED_EVENT, details=details))
        out.append(details)
    return out


def _record_egress_declarations(db: Session, stack: "models.PersistentStack",
                                old_compose_text, actor_user_id: int) -> List[dict]:
    """declared-egress-and-sealed-nodes C5b — audit every DECLARED egress change.

    Walks the stack's compose against the compose it had before and calls
    :func:`services.egress_allowlist.record_egress_declaration` (C5a) once per
    service whose DECLARED hosts actually changed, so the audit trail says what
    a service was promised to reach, with the diff, at the moment the author
    promised it. `old_compose_text` is the pre-change compose — either the
    stored dict or raw YAML text (parsed the same way `_attaches_platform_network`
    reads a compose doc); `None` on create, so every declared service is new.
    A service that HAD an `x-mod.egress` block and lost one is worth auditing
    too: `entries=[]` (the removal is the event). A service whose host SET is
    unchanged (a pure mode flip, or an edit that did not touch egress) writes
    nothing — the diff would be empty either way.

    `subject_id` is the string `"<stack_id>:<service>"`: the audit function's
    `subject_id` is untyped and lands verbatim in the AuditEvent JSONB `details`
    (`workload_id`), so the composite string is what a reviewer greps for — the
    stack id alone would not say WHICH service changed.
    """
    from services.egress_allowlist import record_egress_declaration

    def _hosts_by_service(compose) -> Dict[str, List[str]]:
        if isinstance(compose, str):
            try:
                compose = yaml.safe_load(compose)
            except yaml.YAMLError:
                compose = None
        return {name: list(entry.get("hosts") or [])
                for name, entry in _parse_egress_policy(
                        compose if isinstance(compose, dict) else {}).items()}

    new_hosts = _hosts_by_service(stack.compose_spec)
    old_hosts = _hosts_by_service(old_compose_text)
    out: List[dict] = []
    for name in sorted(set(new_hosts) | set(old_hosts)):
        new_set = frozenset(new_hosts.get(name, []))
        old_set = frozenset(old_hosts[name]) if name in old_hosts else None
        if old_set is not None and new_set == old_set:
            continue  # hosts unchanged — nothing was declared differently
        record_egress_declaration(
            db,
            tenant_id=stack.tenant_id,
            actor_user_id=actor_user_id,
            subject_type="stack_service",
            subject_id=f"{stack.id}:{name}",
            entries=new_hosts.get(name, []),
            previous_entries=old_hosts.get(name) if name in old_hosts else None,
        )
        out.append({"service": name,
                    "hosts": sorted(new_set),
                    "previous_hosts": sorted(old_set) if old_set is not None else None})
    return out


class _EgressObservationIn(BaseModel):
    service: str
    destination: str


class _EgressObservationBatch(BaseModel):
    observations: List[_EgressObservationIn]










# --- stack-secrets-and-certs §4/§5 — the secret-write surface ---------------
# The single input surface: a holder puts a value at their OWN stack's Vault
# path (never inline in the compose), a platform admin puts config under
# `platform/`, and a byo cert+key is validated then stored at the cert path.
# Every write is audited by PATH, never by value.

class StackSecretWrite(BaseModel):
    # The full Vault path the value lives at — the same shape `x-mod.secrets`
    # references (`tenants/<id>/stacks/<name>/<svc>/<key>`). Enforced under the
    # stack's own prefix, so a holder cannot write outside their stack.
    path: str
    value: str


class PlatformSecretWrite(BaseModel):
    # Platform config (e.g. the Headscale URL + key) lives under `platform/`.
    # `data` is the key/value map stored at the path, so a multi-field config
    # like {url, api_key} is one write.
    path: str
    data: Dict[str, Any]


class StackCertUpload(BaseModel):
    service: str
    cert: str  # PEM certificate (chain allowed)
    key: str   # PEM private key








class StackExposureUpdate(BaseModel):
    """caddy-stack-ingress §2 — set a stack's PUBLIC exposure. `exposed` toggles
    the magic front door; `custom_domain` is an optional BYO domain (only
    meaningful while exposed). Omit `custom_domain` to leave it unchanged; pass
    it as null/"" to clear it."""
    exposed: bool
    custom_domain: Optional[str] = Field(default=None)
    clear_custom_domain: bool = Field(default=False)
    # friendly-stack-fqdn (D4) — an optional human-friendly public alias for the
    # stack (aliased to its stable internal mesh name). Only meaningful while
    # exposed. Omit `friendly_fqdn` to leave it unchanged; pass it as null/"" or set
    # `clear_friendly_fqdn` to clear it. MOD provisions no DNS — it only stores the
    # alias and (via the ingress `ask` gate) authorises a per-host cert for it.
    friendly_fqdn: Optional[str] = Field(default=None)
    clear_friendly_fqdn: bool = Field(default=False)




SNAPSHOT_BUCKET = "stack-snapshots"
_ACTIVE_STATUSES = ("running", "deploying", "migrating")


# caddy-stack-ingress §2.2/§2.3 — the on-demand-TLS `ask` seam. Caddy calls this
# before it issues a certificate for a hostname it has never seen; a 200
# authorises issuance, anything else refuses it.
#
# Security model: the PRIMARY guard is the domain allowlist itself — the endpoint
# authorises ONLY a hostname already declared as a good-standing stack's TLS fqdn,
# and even a 200 buys nothing (only Caddy acts on it, and ACME still needs DNS
# control of that name). The api container is on the internal compose/overlay
# network and this path is not routed to the public edge. A shared token is
# OPTIONAL defense-in-depth, enforced ONLY when the token is provisioned —
# because Caddy's header-less `on_demand_tls.ask` cannot add a custom header, the
# token is accepted via header OR the `?token=` query param (Caddy preserves the
# configured query string and merely appends `&domain=`).
#
# CONSIGN §0.1 — the token is a SECRET, so it is read from Vault at CALL time
# (KV v2 slot `infrastructure/ingress`, key `ask_token`, the exact slot vault-init.sh
# already provisions), never from a bootstrap env var: switching/rotating it takes
# effect live, with no API container recreate. Same AppRole login + read_kv_v2
# helper every other on-demand Vault read in this module uses (_resolve_stack_secrets
# via _approle_token) — no new auth path. Cached briefly (≤60s) so a hot seam
# doesn't round-trip Vault per request while rotation still lands within a minute.
_INGRESS_ASK_TOKEN_VAULT_PATH = "infrastructure/ingress"
_INGRESS_ASK_TOKEN_CACHE_TTL_S = 60.0
_ingress_ask_token_cache: Dict[str, object] = {"value": None, "at": 0.0}


def _ingress_ask_token() -> str:
    """Current ingress ask token from Vault, "" when unprovisioned (never raises).

    Vault being down / empty must degrade to the seam's unset posture (see
    _ingress_token_ok), never take on-demand TLS issuance down. The value is
    NEVER logged. Cached for _INGRESS_ASK_TOKEN_CACHE_TTL_S so a rotation in
    Vault takes effect without a restart."""
    now = time.monotonic()
    cached = _ingress_ask_token_cache.get("value")
    if cached is not None and now - float(_ingress_ask_token_cache["at"]) < _INGRESS_ASK_TOKEN_CACHE_TTL_S:
        return str(cached)
    value = ""
    role_id = os.getenv("VAULT_APPROLE_ROLE_ID", "")
    secret_id = os.getenv("VAULT_APPROLE_SECRET_ID", "")
    if role_id and secret_id:
        try:
            from utils.vault_client import approle_login, read_kv_v2
            token = approle_login(role_id=role_id, secret_id=secret_id)
            data = read_kv_v2(_INGRESS_ASK_TOKEN_VAULT_PATH, token=token) or {}
            value = str(data.get("ask_token") or "").strip()
        except Exception:  # noqa: BLE001 — Vault outage must degrade, not raise
            logger.warning("ingress ask token: Vault read failed", exc_info=True)
    _ingress_ask_token_cache["value"] = value
    _ingress_ask_token_cache["at"] = now
    return value
_ASK_NEG_TTL_S = int(os.getenv("MOD_INGRESS_ASK_NEG_TTL", "30"))
_ask_neg_cache: Dict[str, float] = {}  # domain(lower) -> expiry epoch (§2.3 DoS blunt)


# GSEC R-141 (SEC-2c-005) — throttle the "token unset" log line so a
# misconfigured box (or the mesh resolver's 5s poll) can't flood the log.
_UNSET_TOKEN_LOG_THROTTLE_S = 300.0
_unset_token_log_at: Dict[str, float] = {}


def _stack_tokens_fail_closed() -> bool:
    """GSEC R-141 — whether the internal-token gate stays CLOSED when its
    token env is not provisioned (the `security.stack_tokens_fail_closed`
    platform setting). Default False (live-safety: the live deployment's
    mesh token is not wired in yet, and closing would strand the D6
    reachability resolver); once both tokens are provisioned the operator
    seeds True. A lookup error falls back to the setting's default (False —
    legacy open) rather than taking the seam down: the loud throttled log
    still marks the gap."""
    try:
        from services import platform_settings as _ps
        return bool(_ps.get_setting("security.stack_tokens_fail_closed"))
    except Exception:  # noqa: BLE001 — fall back to the registered default
        return False


def _log_unset_token(seam: str, allowed: bool) -> None:
    """GSEC R-141 — loud, throttled log for the token-env-unset path: the
    live-config gap must be visible whether we allowed or refused it."""
    now = time.time()
    if now - _unset_token_log_at.get(seam, 0.0) < _UNSET_TOKEN_LOG_THROTTLE_S:
        return
    _unset_token_log_at[seam] = now
    if allowed:
        logger.warning(
            "GSEC R-141: internal token env for '%s' is UNSET — pass-through "
            "allowed (security.stack_tokens_fail_closed is False); provision "
            "the token (vault-init seeds it) and then seed the setting True "
            "to fail closed.", seam)
    else:
        logger.error(
            "GSEC R-141: internal token env for '%s' is UNSET — request "
            "REFUSED (security.stack_tokens_fail_closed is True). Provision "
            "the token (vault-init seeds it) or seed the setting False to "
            "restore legacy behaviour.", seam)


def _ingress_token_ok(presented: Optional[str]) -> bool:
    """GSEC R-141 — the on-demand-TLS `ask` shared-token gate. Token provisioned
    (Vault `infrastructure/ingress`, see _ingress_ask_token) ⇒ constant-time match
    (mismatch/missing ⇒ False ⇒ 403). Token UNSET ⇒ the seam's posture comes from
    the `security.stack_tokens_fail_closed` platform setting (True ⇒ refuse; False
    ⇒ allow + loud throttled log so the provisioning gap cannot hide). See
    _stack_tokens_fail_closed()."""
    expected = _ingress_ask_token()
    if expected:
        return bool(presented) and hmac.compare_digest(presented, expected)
    if _stack_tokens_fail_closed():
        _log_unset_token("ingress ask (Vault infrastructure/ingress)", allowed=False)
        return False
    _log_unset_token("ingress ask (Vault infrastructure/ingress)", allowed=True)
    return True


def _stack_in_good_standing(stack: "models.PersistentStack") -> bool:
    """Intended-up (`desired_state == 'running'`, so a scaled-to-zero stack from
    `persistent-stack-billing` is refused) and active (not torn down / failed)."""
    return (getattr(stack, "desired_state", None) == "running"
            and stack.status in _ACTIVE_STATUSES)


def _ingress_authorized_domain(db: Session, domain: str) -> bool:
    """True iff `domain` maps to a stack in good standing that may serve it over
    TLS — intended-up (`desired_state == 'running'`, so a scaled-to-zero stack is
    refused) and active (not torn down). Two sources, checked in order:

    1. PUBLIC EXPOSURE (caddy-stack-ingress binding architecture, the primary
       source): a stack explicitly EXPOSED (opt-in) authorises its MOD-run
       `public_hostname`, any BYO `custom_domain`, and its optional friendly
       alias `friendly_fqdn` (friendly-stack-fqdn D4). Turning exposure off clears
       all three, so the names stop authorising immediately.
    2. Legacy per-service `x-mod.tls.fqdn` mode 'auto' (stack-secrets-and-certs
       §5): kept so a stack that declared an auto TLS fqdn without the exposure
       toggle still authorises. Only mode 'auto' authorises on-demand ISSUANCE; a
       'byo' cert is pre-loaded by the ingress from Vault and never ACME-issued.

    Fails closed: an unknown / suspended host returns False."""
    from services import persistent_stacks_shared_service as _svc
    return _svc.ingress_authorized_domain(
        db, domain, is_good_standing=_stack_in_good_standing,
        active_statuses=_ACTIVE_STATUSES)




# ---------------------------------------------------------------------------
# dual-anchor-mesh-federation §2.1 — the manager's heartbeat registry as the
# reachability source of truth, exposed to the D6 nginx resolver.
# ---------------------------------------------------------------------------
_MESH_REACH_TOKEN_ENV = "MOD_MESH_REACHABILITY_TOKEN"
# How stale a per-anchor heartbeat stamp may be before that anchor stops being a
# resolution candidate. Several heartbeat intervals (workers beat ~15s).
_MESH_ANCHOR_STALE_S = int(os.getenv("MOD_MESH_ANCHOR_STALE_S", "45"))
_MESH_UPSTREAM_PORT = os.getenv("MOD_MESH_UPSTREAM_PORT", "80")


def _mesh_reach_token_ok(supplied: Optional[str]) -> bool:
    """GSEC R-141 — the mesh-reachability shared-token gate. Token env set ⇒
    constant-time match (mismatch/missing ⇒ False ⇒ 403). Token env unset ⇒
    the seam's posture comes from the `security.stack_tokens_fail_closed`
    platform setting (True ⇒ refuse; False ⇒ allow + loud throttled log).
    Mirrors _ingress_token_ok."""
    expected = os.getenv(_MESH_REACH_TOKEN_ENV, "").strip()
    if expected:
        return bool(supplied) and hmac.compare_digest(supplied.strip(), expected)
    if _stack_tokens_fail_closed():
        _log_unset_token("mesh reach (MOD_MESH_REACHABILITY_TOKEN)", allowed=False)
        return False
    _log_unset_token("mesh reach (MOD_MESH_REACHABILITY_TOKEN)", allowed=True)
    return True


def _anchor_fresh(seen_iso: Optional[str], last_heartbeat, now) -> bool:
    """An anchor is a candidate when its explicit freshness stamp is recent. When
    the worker sent no per-anchor stamp (legacy single-address binary) fall back
    to last_heartbeat so today's single-anchor services keep resolving."""
    ts = None
    if seen_iso:
        try:
            ts = datetime.fromisoformat(seen_iso)
        except ValueError:
            ts = None
    if ts is None:
        ts = last_heartbeat
    if ts is None:
        return False
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return (now - ts).total_seconds() <= _MESH_ANCHOR_STALE_S


# Internal, UNauthenticated router — reached by the D6 nginx resolver over the
# compose network (api:80), so it must NOT inherit `router`'s STACK_AUTHOR gate.




# ---------------------------------------------------------------------------
# caddy-stack-ingress §2.4 — TENANT-LEVEL default TLS policy for exposed stacks.
#
# A tenant admin chooses, ONCE, how their publicly-exposed stacks get their
# HTTPS cert: `letsencrypt` (the platform issues + renews on-demand via the
# Caddy edge) or `byo` (the tenant pastes a cert+key — typically a wildcard for
# their own domain — which the cert-watch sidecar loads into Caddy). The choice
# is a tenant default applied to newly-exposed stacks that don't override it
# per-service. Cert MATERIAL is a secret and lives in Vault (never in Postgres);
# only non-secret metadata (mode, CN, notAfter) is kept in tenant_settings.
# ---------------------------------------------------------------------------
_TENANT_TLS_SETTING_KEY = "stack_tls"
_TENANT_TLS_MODES = ("letsencrypt", "byo")


def _is_tenant_admin(user: "models.User") -> bool:
    """Tenant admin = a platform admin, or a tenant's own top-level account
    (no parent). Mirrors routers/assistant._is_tenant_admin so the two admin
    surfaces stay consistent."""
    return bool(getattr(user, "is_platform_admin", False)
                or getattr(user, "parent_user_id", None) is None)


def _tenant_tls_vault_path(tenant_id: int) -> str:
    """Where a tenant's DEFAULT byo cert+key live. Under the same `tenants/`
    namespace the api-tenant-secrets approle already writes (see
    `_stack_secret_prefix`), so no new Vault policy is needed."""
    return f"tenants/{tenant_id}/stack-tls-default/cert"


def _cert_primary_name(cert_pem: str) -> str:
    """First SAN DNS name (modern authority), falling back to the subject CN.
    Used both to display what a byo cert covers and to give `_validate_cert_pair`
    a self-consistent fqdn to check the key-pairing against."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtensionOID
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        san = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        dns = san.value.get_values_for_type(x509.DNSName)
        if dns:
            return str(dns[0])
    except Exception:  # noqa: BLE001 — no SAN → fall back to CN
        pass
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(cn[0].value) if cn else ""


def _load_tenant_tls_setting(db: Session, tenant_id: int):
    from services import persistent_stacks_shared_service as _svc
    return _svc.load_tenant_tls_setting(db, tenant_id)


def _tenant_tls_public(val: dict) -> dict:
    """Non-secret view of the tenant TLS setting (safe to return to the UI)."""
    return {
        "mode": val.get("mode") or "letsencrypt",
        "has_cert": bool(val.get("cert_cn")),
        "cert_cn": val.get("cert_cn"),
        "cert_not_after": val.get("cert_not_after"),
        "updated_at": val.get("updated_at"),
    }


def effective_tenant_tls_mode(db: Session, tenant_id: int) -> str:
    """The default TLS mode for a tenant's exposed stacks. `letsencrypt` unless
    the tenant admin explicitly chose `byo`."""
    _, val = _load_tenant_tls_setting(db, tenant_id)
    mode = val.get("mode")
    return mode if mode in _TENANT_TLS_MODES else "letsencrypt"


class TenantTlsUpdate(BaseModel):
    """Set the tenant-level default TLS policy. `cert_pem`/`key_pem` are required
    only for `mode='byo'` when no cert is on file yet (or to replace it)."""
    mode: str = Field(..., description="letsencrypt | byo")
    cert_pem: Optional[str] = Field(default=None)
    key_pem: Optional[str] = Field(default=None)








def _stack_bucket(stack: models.PersistentStack) -> str:
    """The dedicated, tenant-scoped bucket for an s3-native stack."""
    return f"tenant-{stack.tenant_id}-stack-{stack.id}"


def _published_host_port(stack: models.PersistentStack) -> Optional[str]:
    """First published host port across the stack's compose services (e.g. '8080'
    from '8080:80'). None if the stack publishes nothing."""
    for svc in ((stack.compose_spec or {}).get("services") or {}).values():
        for p in (svc.get("ports") or []):
            spec = p if isinstance(p, str) else f"{p.get('published','')}:{p.get('target','')}"
            left = spec.split(":")[0].strip() if ":" in spec else ""
            if left.isdigit():
                return left
    return None


def _fallback_host_port(stack: models.PersistentStack) -> Optional[str]:
    """10-03 (whoami stack 96): endpoint port for a new-format stack whose
    compose `ports:` are stripped at deploy — it publishes only through
    `x-mod.host_ports`, so `_published_host_port` finds nothing and a healthy
    stack reports reachable=false. Fallback order: (a) the first host port the
    worker ALLOCATED for a `host: "any"` entry, read from
    storage_refs["host_port_mappings"] (the worker's documented shape, recorded
    by `record_host_port_allocation` after the deploy result is ingested);
    (b) the first `x-mod.host_ports` entry with a numeric `host` — an int or a
    digit string, and an "ip:port" style string contributes the part after the
    last ':' when it is digits. None if neither yields a port.
    `_stack_allocated_host_ports` is imported lazily inside the call: the
    package `__init__`/`persistent_stacks.py` already import from `_shared`,
    so a module-top import here would be circular."""
    try:
        from routers.persistent_stacks.persistent_stacks import _stack_allocated_host_ports
        rows = _stack_allocated_host_ports(stack)
        if rows:
            return str(rows[0]["host"])
    except Exception as e:  # noqa: BLE001
        logger.warning("allocated host-port lookup failed for stack %s: %s", stack.id, e)
    top = (stack.compose_spec or {}).get("x-mod")
    if not isinstance(top, dict):
        return None
    for e in (top.get("host_ports") or []):
        if not isinstance(e, dict):
            continue
        host = e.get("host")
        if isinstance(host, bool):
            continue
        if isinstance(host, int):
            return str(host)
        if isinstance(host, str):
            part = host.rsplit(":", 1)[-1].strip() if ":" in host else host.strip()
            if part.isdigit():
                return part
    return None


def _compute_lb_endpoint(stack: models.PersistentStack, worker: models.WorkerLicense) -> Optional[str]:
    """add-persistent-stacks §4.1 — the worker-local address an internal consumer
    connects to: the stack's published port on its worker host. The directory
    (§4.2) hands this back; central-nginx public cutover (§4.3) repoints to it.

    mod-connect-service-mesh §2.2 — RETIRED as the endpoint source for a stack
    with reachable (mesh) services. Those services drop their published `ports`
    at deploy (`_inject_mesh_sidecars`) because reach is over the overlay by
    per-service MagicDNS name — one endpoint per service, in `mesh_endpoints`.
    A single first-published-port `host:port` cannot describe a multi-service
    stack and, for a mesh stack, would point at a port that is no longer
    published. Such a stack therefore has NO single `lb_endpoint`; it resolves
    per-service via `mesh_endpoints`. The single-port form survives only as the
    legacy fallback for non-mesh stacks.

    Gated on the identity actually being MINTED (`stack.mesh_endpoints` set): on
    an install with no bundled control server a service marked reachable gets no
    sidecar and keeps its published ports, so the port fallback must still apply
    there rather than stranding the stack with no endpoint at all.

    09-29: mesh services KEEP their published ports again (the netns holder
    publishes them — see `_inject_mesh_sidecars`), so the host:port endpoint is
    valid for mesh stacks too and is returned alongside `mesh_endpoints`."""
    port = _published_host_port(stack)
    if not port:
        port = _fallback_host_port(stack)
    if not port:
        return None
    return f"{_stack_connect_host(worker)}:{port}"


# The name the API container uses to reach a port published on the Docker HOST.
# CE/on-prem compose gives the `api` service
# `extra_hosts: ["host.docker.internal:host-gateway"]` (open-core/overlay and
# CORE/startup compose); the same name `services/app_routing._MANAGER_HOST` and
# `stack_wake` already dial for the embedded worker.
_EMBEDDED_STACK_HOST = "host.docker.internal"


def _stack_connect_host(worker: models.WorkerLicense) -> str:
    """Host an internal consumer dials for a stack's published port.

    A stack on the EMBEDDED worker publishes its port on the Docker host, not
    inside the worker container, so the worker's container name/hostname
    (`mod-worker`) has nothing listening on it: dial the host gateway name
    instead. Every other (remote) worker keeps `hostname or worker_name`."""
    if getattr(worker, "worker_name", None) in _EMBEDDED_WORKER_NAMES:
        return _EMBEDDED_STACK_HOST
    return (worker.hostname or worker.worker_name)


def _provision_storage(stack: models.PersistentStack) -> None:
    """add-persistent-stacks §3.1/§3.3 — provision managed storage per storage_class.

    - s3_native: ensure a tenant-scoped bucket (relocation later just re-points it,
      no copy), record `storage_refs.bucket`.
    - local_fs:  the worker's `docker compose up` provisions the named volumes;
      we record them in `storage_refs.volumes` for snapshot/restore + cleanup.
    """
    refs = dict(stack.storage_refs or {})
    refs["storage_class"] = stack.storage_class
    if stack.storage_class == "s3_native":
        bucket = _stack_bucket(stack)
        try:
            from utils.minio_client import MinioClient
            mc = MinioClient(stack.tenant_id)
            if not mc.client.bucket_exists(bucket):
                mc.client.make_bucket(bucket)
        except Exception as e:  # noqa: BLE001 — record intent even if MinIO is transiently down
            logger.warning("s3_native bucket provision failed for stack %s: %s", stack.id, e)
        refs["bucket"] = bucket
    else:  # local_fs
        refs["volumes"] = _stack_volumes(stack)
    stack.storage_refs = refs


def _snapshot_retained_until(snap: dict):
    """GA.ASW.8 — the UTC instant a snapshot's soft-hold expires, or None.

    Set on the snapshot's storage_refs record by teardown with
    `data_policy="retain"` (see `teardown_stack` in
    services/persistent_stacks_domain_service.py). Both erase paths —
    teardown `_cleanup_storage` and retention `_prune_snapshots` — skip any
    snapshot whose deadline is still in the future. The stamp lives in
    storage_refs (no new column), next to the record's other per-snapshot
    bookkeeping (created_at/source/verified_at)."""
    raw = (snap or {}).get("retain_until")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cleanup_storage(stack: models.PersistentStack, *, data_policy: str = "erase") -> dict:
    """add-persistent-stacks §3.5 — on teardown, delete the stack's MinIO snapshots
    (the worker `down -v` already removes the volumes) and drop the s3-native bucket.
    Returns a small report. Never raises."""
    report = {"snapshots_removed": 0, "bucket_removed": None, "snapshots_retained": 0}
    refs = stack.storage_refs or {}
    now = datetime.now(timezone.utc)
    try:
        from utils.minio_client import MinioClient
        mc = MinioClient(stack.tenant_id)
        # delete recorded snapshots from the snapshot bucket. GA.ASW.8 — a
        # snapshot stamped `retain_until` in the future (teardown with
        # data_policy=retain) is SKIPPED: its objects stay in MinIO until the
        # deadline passes, and a later erase (prune or re-teardown) picks them
        # up. The record is kept in storage_refs on purpose — it is the
        # retention bookkeeping the skip depends on.
        for snap in (refs.get("snapshots") or {}).values():
            retain_until = _snapshot_retained_until(snap)
            if retain_until is not None and retain_until > now:
                report["snapshots_retained"] += 1
                continue
            bucket = snap.get("bucket", SNAPSHOT_BUCKET)
            for key in (snap.get("volumes") or {}).values():
                try:
                    mc.client.remove_object(bucket, key)
                    report["snapshots_removed"] += 1
                except Exception:  # noqa: BLE001
                    pass
        # drop the s3-native stack bucket (empty it first). data_policy=retain
        # keeps the bucket and every object in it — retain means keep the data.
        if data_policy == "retain" and refs.get("bucket"):
            report["bucket_retained"] = refs["bucket"]
        elif stack.storage_class == "s3_native" and refs.get("bucket"):
            b = refs["bucket"]
            try:
                if mc.client.bucket_exists(b):
                    for obj in mc.client.list_objects(b, recursive=True):
                        mc.client.remove_object(b, obj.object_name)
                    mc.client.remove_bucket(b)
                    report["bucket_removed"] = b
            except Exception as e:  # noqa: BLE001
                logger.warning("s3_native bucket cleanup failed for stack %s: %s", stack.id, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("storage cleanup failed for stack %s: %s", stack.id, e)
    return report


def _prune_snapshots(stack: models.PersistentStack, db=None) -> int:
    """add-persistent-stacks §3.5 — bound per-stack snapshot storage: keep the newest
    STACK_SNAPSHOT_RETENTION, deleting older ones from MinIO + storage_refs. Returns
    how many were pruned. Requires each snapshot to carry `created_at` (set on backup).

    GS.PSR.8 — the retention COUNT resolves through the platform-settings
    registry (stacks.snapshot_retention: DB > env > 5) via the helper on
    the module that owns backup retention; `db` is optional so existing
    call sites without a session keep working (short self-opened)."""
    # GY.C15 — legal hold: a held tenant's stack snapshots are NOT pruned
    # (supports preservation requirements, e.g. DFARS 252.204-7012 90-day).
    # Fail closed: with no session we can't check, so refuse to prune.
    if db is not None:
        try:
            from utils.paid_import import optional_paid
            _lhold = optional_paid("services.legal_hold")
            # FX.162.p5 — CE carve: no legal holds exist -> nothing held
            if _lhold is not None and _lhold.is_held(db, tenant_id=stack.tenant_id):
                logger.info("stack %s: snapshot prune skipped — legal hold "
                            "active on tenant %s; snapshots preserved",
                            stack.id, stack.tenant_id)
                return 0
        except Exception:  # noqa: BLE001 — fail closed (no session => can't verify)
            logger.warning("stack %s: legal-hold check failed — skipping "
                           "snapshot prune (fail closed)", stack.id)
            return 0
    elif stack.tenant_id is not None:
        logger.warning("stack %s: snapshot prune skipped — no session to "
                       "verify legal holds (fail closed)", stack.id)
        return 0

    # GT.D6 DS-3 — stack_backup is Paid: on CE fall back to the same default
    # the module mirrors (5) so retention pruning still bounds storage.
    from utils.paid_import import optional_paid
    _bk = optional_paid("services.stack_backup")
    retention = _bk.retention_count(db) if _bk is not None else 5
    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    if len(snaps) <= retention:
        return 0
    # GA.ASW.8 — a snapshot inside its teardown `retain` window is not a prune
    # candidate: the count cap bounds routine storage, but a deadline the
    # operator chose explicitly must outrank it. (The count check above still
    # uses the full set, so a retained copy that pushes the stack over the cap
    # simply delays pruning of the OTHER old copies, never the retained one.)
    now = datetime.now(timezone.utc)
    prunable = [(sid, s) for sid, s in snaps.items()
                if not (_snapshot_retained_until(s) is not None
                        and _snapshot_retained_until(s) > now)]
    if len(prunable) <= retention:
        return 0
    ordered = sorted(prunable, key=lambda kv: kv[1].get("created_at", ""))
    to_drop = ordered[:len(prunable) - retention]
    # §9.8 — never take a stack from "one backup" to "no backups". A retention
    # rule that can do that is not a retention rule, it is a scheduled
    # data-loss event. Unreachable while retention >= 1, and here precisely
    # because a misconfigured STACK_SNAPSHOT_RETENTION=0 would otherwise delete
    # every copy on the next backup, silently and immediately.
    if len(snaps) - len(to_drop) < 1:
        to_drop = ordered[:len(snaps) - 1]
        logger.warning(
            "stack %s: retention (%d) would have removed every snapshot; kept "
            "the newest. Data protection is not a setting that may reach zero.",
            stack.id, retention)
    pruned = 0
    try:
        from utils.minio_client import MinioClient
        mc = MinioClient(stack.tenant_id)
        for snap_id, snap in to_drop:
            for key in (snap.get("volumes") or {}).values():
                try:
                    mc.client.remove_object(snap.get("bucket", SNAPSHOT_BUCKET), key)
                except Exception:  # noqa: BLE001
                    pass
            snaps.pop(snap_id, None)
            pruned += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("snapshot prune failed for stack %s: %s", stack.id, e)
        return 0
    refs["snapshots"] = snaps
    stack.storage_refs = refs
    return pruned








def _stack_volumes(stack: models.PersistentStack) -> List[str]:
    """Docker volume names for the stack's compose-declared named volumes."""
    vols = (stack.compose_spec or {}).get("volumes") or {}
    project = _project_name(stack)
    return [f"{project}_{name}" for name in vols.keys()]


# FX.165 — worker wake for the stack-dispatch helpers below.
#
# Both helpers add an ApiTaskQueue row to a session the CALLER commits, so a
# publish here would fire before the row exists on disk (and would run inside an
# open transaction, the pattern that pins a PgBouncer connection). The wake is
# therefore queued on `db.info` and drained by the session's `after_commit` —
# the same one-shot listener shape `services/stack_backend_dispatch.py` uses.
# A rollback drops the queue: the row never existed, so a worker must not be
# woken to go find it.
_WAKE_CHANNELS_INFO_KEY = "ps_stack_wake_channels_after_commit"
_WAKE_LISTENER_INFO_KEY = "ps_stack_wake_listener_after_commit"


def _direct_wake_enabled(db: Session) -> bool:
    """Is the direct wake on? Read on the CALLER's session, at enqueue time.

    `wake_enabled` is a plain settings lookup (cached DB query, no external dial),
    so checking it here — inside the still-open transaction, before the commit
    that makes the row exist — is safe: nothing external is fetched, and the
    after-commit drain therefore never spawns a thread for a disabled platform.
    A settings read that fails is "off": the wake is an optimization.
    """
    # 1788d CE carve: services/worker_direct_wake.py is paid (manifest deny
    # 1159). The direct wake is an optimization only; with it absent the
    # platform leaves it OFF and workers find the task on their long-poll.
    from utils.paid_import import optional_paid
    _wake_mod = optional_paid("services.worker_direct_wake")
    if _wake_mod is None:
        return False
    try:
        return bool(_wake_mod.wake_enabled(db=db))
    except Exception:  # noqa: BLE001 — a wake is an optimization, not a result
        return False


def _queue_worker_wake(db: Session, queue_name: str,
                       worker_name: Optional[str] = None) -> None:
    """Queue the `task:available:<queue_name>` Valkey wake for after `db` commits.

    Workers already find these tasks on their next 30s long-poll / 10s backstop;
    the wake is what makes a pinned snapshot/restore start promptly instead.
    Best-effort by construction — the publish itself swallows every error.

    FX.165 p3b: `worker_name` is the row's `assigned_to_worker` (models.ApiTaskQueue
    has no worker-named queues — queue_name is a pool like CPU/CUDA, so "targets
    one worker" means the row was born ASSIGNED to it). When set, the after-commit
    drain also fires the direct wake at that worker (FX.165 p3a service).
    """
    from sqlalchemy import event
    if not db.info.get(_WAKE_LISTENER_INFO_KEY):
        event.listen(db, "after_commit", _on_wake_after_commit)
        event.listen(db, "after_rollback", _on_wake_after_rollback)
        db.info[_WAKE_LISTENER_INFO_KEY] = True
    db.info.setdefault(_WAKE_CHANNELS_INFO_KEY, []).append(
        (f"task:available:{queue_name}", worker_name,
         bool(worker_name) and _direct_wake_enabled(db)))


def _on_wake_after_commit(session) -> None:
    """Outermost commit landed — publish the queued wakes, never fail the request."""
    for channel, worker_name, direct in (
            session.info.pop(_WAKE_CHANNELS_INFO_KEY, None) or []):
        try:
            _publish_event(channel, "1")
        except Exception:  # noqa: BLE001 — a wake is an optimization, not a result
            logger.warning("persistent stack: worker wake for %s failed; "
                           "worker falls back to its long-poll", channel)
        if direct:
            # FX.165 p3b — the row is on disk now, so the pinned worker can be
            # nudged directly (the setting was already checked at enqueue time).
            _fire_direct_wake(worker_name)


def _fire_direct_wake(worker_name: str) -> None:
    """FX.165 p3b — fire-and-forget direct wake at a worker after its task committed.

    Never raises, never blocks the request. The load-bearing rule: `wake_worker`
    dials an external address, so it must NEVER run inside an open DB transaction
    (an external fetch between PgBouncer workers is the storming pattern
    `external-fetch-inside-db-transaction` warns about). We are on the after-commit
    drain — the row is on disk — and the wake still gets its OWN short-lived
    session (the caller's session is mid-`after_commit` and is not to be shared
    across threads), closed in `finally`. Exceptions are swallowed + logged at
    debug: the worker still finds the task on its long-poll.
    """
    # 1788d CE carve: worker_direct_wake is paid - no direct wake at all (the
    # worker still finds the task on its 30s long-poll).
    from utils.paid_import import optional_paid
    _wake_mod = optional_paid("services.worker_direct_wake")
    if _wake_mod is None:
        return

    def _run(name: str = worker_name) -> None:
        from database import SessionLocal
        fresh = SessionLocal()
        try:
            _wake_mod.wake_worker(fresh, name)
        except Exception as exc:  # noqa: BLE001 — swallowed: the worker still long-polls
            logger.debug("direct wake failed for %s: %s", name, exc)
        finally:
            fresh.close()

    try:
        threading.Thread(target=_run,
                         name=f"worker-direct-wake-{worker_name}",
                         daemon=True).start()
    except Exception as exc:  # noqa: BLE001 — thread spawn is best-effort
        logger.debug("direct wake thread not started for %s: %s", worker_name, exc)


def _on_wake_after_rollback(session) -> None:
    """Outermost rollback: the queued rows are gone — discard their wakes."""
    session.info.pop(_WAKE_CHANNELS_INFO_KEY, None)


def _dispatch_volume_task(db: Session, worker_name: str, tenant_id: int,
                          op: str, volume: str, bucket: str, key: str,
                          project: Optional[str] = None,
                          stack_id: Optional[int] = None,
                          snapshot_id: Optional[str] = None,
                          max_uncompressed_bytes: Optional[int] = None) -> None:
    """Dispatch a snapshot/restore op for a single volume (pinned PLATFORM_TASK).

    `project` is set only for an IN-PLACE restore of a deployed stack — the worker
    then stops the stack before restoring and starts it after (restoring over a
    live stateful container is a silent data-loss no-op). Migration/backfill
    restore into a fresh target and pass no project.

    `stack_id` is carried for attribution only. Snapshot/restore act on a volume,
    not on stack lifecycle, so the completion handler records their outcome
    without moving the stack's status."""
    payload = {
        "stack_op": op,
        "stack_volume": volume,
        "snapshot_bucket": bucket,
        "snapshot_key": key,
        "container_env": {"PLATFORM_TASK": "1"},
        "node_kind": f"persistent-stack-{op}",
    }
    if project:
        payload["stack_project"] = project
    if stack_id is not None:
        payload["stack_id"] = stack_id
    if snapshot_id is not None:
        # Carried so the completion handler can record the verdict against the
        # right snapshot. Deliberately explicit rather than parsed back out of
        # the object key: the key's shape is a storage layout, and making a
        # correctness-critical lookup depend on it would break the day someone
        # reasonably changes how snapshots are laid out in the bucket.
        payload["snapshot_id"] = snapshot_id
    # 30.6.1 c1-J3c: the seed-extract bomb budget comes from the PLATFORM setting
    # (read by the caller, never from the request), so a tenant cannot raise it.
    # Absent/0/negative => key omitted => the worker's own 512 GiB default stays.
    if max_uncompressed_bytes is not None and max_uncompressed_bytes > 0:
        payload["max_uncompressed_bytes"] = int(max_uncompressed_bytes)
    # NOAH: same pin-prone guard as _dispatch_stack_task — a snapshot/restore
    # is ASSIGNED to a specific worker and must not land on one NOAH may
    # reclaim mid-transfer. No-op unless noah.require_pinnable_enabled is on.
    from utils.paid_import import optional_paid
    _noah = optional_paid("services.noah")
    if _noah is not None:
        payload = _noah.add_pinnable_requirement(payload, db=db)
    row = models.ApiTaskQueue(
        node_id=f"stack-{op}-{volume}",
        tenant_id=tenant_id,
        queue_name="CPU",
        priority=9,
        task_type="system",
        priority_class="normal",
        task_payload=payload,
        status=models.ApiTaskStatus.ASSIGNED,
        assigned_to_worker=worker_name,
        max_attempts=1,
        output_namespace="attempt_0",
    )
    db.add(row)
    # FX.165 — the row is not committed yet (the caller commits), so queue the
    # wake; it publishes on the outermost commit and is dropped by a rollback.
    _queue_worker_wake(db, row.queue_name, row.assigned_to_worker)


def take_snapshot(db: Session, stack: models.PersistentStack,
                  source: str = "on_demand") -> dict:
    """Dispatch a snapshot of every backed-up volume. Does NOT commit.

    Factored out of the endpoint so the §9.1 scheduled sweep takes backups by
    exactly the same path an operator does. A second implementation would be a
    second set of retention and exclusion rules to keep in step, and the one
    that runs unattended would be the one nobody notices drifting.

    `source` is recorded on the snapshot so "who took this" is answerable later
    — the answer changes what a missing backup means.
    """
    if not stack.target_worker:
        raise ValueError("stack is not deployed to a worker")
    # GA.ASW.5 — a stack running a supported DB engine (structurally detected
    # from its compose_spec, not a stored posture column) gets one logical
    # (pg_dump) snapshot instead of a per-volume tar. Same snapshot-id/prefix
    # shape either way so retention pruning, rehearsal and restore only need
    # to branch on `snap.get("posture")`, not on how the stack was created.
    # GT.D6 DS-3 — logical backup is Paid: on CE there is no engine, so every
    # snapshot keeps the volume-tar path (# CE-safe).
    from utils.paid_import import optional_paid
    _lb = optional_paid("services.stack_logical_backup")
    engine = _lb.logical_backup_engine(stack) if _lb is not None else None
    # Lead review 2026-09-29: no released worker implements the
    # logical_backup/logical_restore stack ops yet (stackrunner.go default
    # case), and the logical path EXCLUDES the DB volume from the tar — so
    # until GA.ASW.5c ships, a logical snapshot would carry no DB data at
    # all. Keep the volume-tar path (DB volume included) unless explicitly on.
    if engine and os.getenv("STACK_LOGICAL_BACKUP_ENABLED", "false").lower() != "true":
        engine = None
    if engine:
        return _take_logical_snapshot(db, stack, engine, source)
    volumes = _backed_up_volumes(stack)
    if not volumes:
        raise ValueError("stack declares no named volumes to back up")

    snap_id = uuid.uuid4().hex[:12]
    project = _project_name(stack)
    # REV-FIX-CE-STACK-SEED-BUCKET NIT 1: nothing but the seed path creates the
    # platform 'stack-snapshots' bucket, so on a fresh CE a plain snapshot
    # request (no seed yet) would hand the worker a NoSuchBucket. Ensure it
    # here, once per request, BEFORE any task/credential for a bucket write is
    # dispatched. Deferred (function-local) import: stack_seed.py imports THIS
    # module at module level, so a module-level import here would be a cycle —
    # this keeps ONE implementation (stack_seed._ensure_snapshot_bucket) shared
    # by the seed path and the snapshot path, idempotent, and every other S3
    # error propagates unchanged exactly as the seed path does.
    from services.stack_seed import _ensure_snapshot_bucket
    from utils import storage
    _ensure_snapshot_bucket(storage.get_service_client())
    vol_keys = {}
    for v in volumes:
        key = f"{project}/{snap_id}/{v}.tar.gz"
        # GX.S9 — pass the project: an in-place snapshot of a DEPLOYED stack
        # write-freezes it (worker compose pause, thaw always) around the tar,
        # exactly as the in-place restore below passes it for stop/start.
        # Tarring a live, writing data dir with no freeze can yield a
        # snapshot that does not restore cleanly.
        _dispatch_volume_task(db, stack.target_worker, stack.tenant_id, "snapshot", v,
                              SNAPSHOT_BUCKET, key, project=project, stack_id=stack.id)
        vol_keys[v] = key
    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    snaps[snap_id] = {"bucket": SNAPSHOT_BUCKET, "volumes": vol_keys,
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "source": source,
                      # §9.5 — a snapshot starts UNVERIFIED and says so. The
                      # alternative, showing nothing until a rehearsal runs,
                      # reads as "fine" for the backups nobody has ever tested,
                      # which is all of them by default.
                      "verified_at": None, "verify_error": None}
    refs["snapshots"] = snaps
    stack.storage_refs = refs  # reassign so SQLAlchemy detects the JSONB change
    pruned = _prune_snapshots(stack, db)   # §3.5 — retention cap bounds snapshot storage
    logger.info("persistent stack %s backup %s dispatched (%s, %d volumes, pruned %d old)",
                stack.id, snap_id, source, len(volumes), pruned)
    return {"snapshot_id": snap_id, "volumes": list(vol_keys.keys()), "pruned": pruned}


def _dispatch_logical_task(db: Session, stack: models.PersistentStack,
                           op: str, spec: dict) -> None:
    """GA.ASW.5 — dispatch an ephemeral pg_dump/pg_restore container task via
    the SAME ApiTaskQueue mechanism `_dispatch_volume_task` uses for the tar
    path (pinned ASSIGNED task on the stack's worker). `spec` comes from
    `build_logical_dump_spec`/`build_logical_restore_spec`; its
    `password_vault_ref` (a vault PATH, never a value) is resolved here, at
    the last possible moment, via the same resolver stack deploys already use
    — the resolved secret is placed only in `container_env` and is never
    logged (only `op`/engine/network go to the log line below).

    GA.ASW.5c: a service template can now pass the DB password as a plain
    compose param instead of a Vault-backed secret — `password_vault_ref` is
    None for those stacks (nothing to resolve, `_resolve_stack_secrets` has
    no entry for it). Falling back to the ALREADY-RENDERED
    `stack.compose_spec` is reading exactly what the deployed container
    itself was started with, not a new secret source — the vault ref still
    wins whenever one is present, this only fills in for stacks that never
    had one."""
    container_env = {"PLATFORM_TASK": "1"}
    vault_path = spec.get("password_vault_ref")
    value = None
    if vault_path:
        # 13.5a — system-triggered context; db lets a named tenant secret
        # resolve if the spec carries one.
        resolved = _resolve_stack_secrets(stack, db=db)
        value = (resolved.get(spec["service"]) or {}).get(spec["password_secret_key"])
    else:
        svc = ((stack.compose_spec or {}).get("services") or {}).get(spec["service"]) or {}
        env = svc.get("environment")
        key = spec["password_secret_key"]
        if isinstance(env, dict):
            value = env.get(key)
        elif isinstance(env, list):
            prefix = f"{key}="
            for entry in env:
                if isinstance(entry, str) and entry.startswith(prefix):
                    value = entry[len(prefix):]
                    break
    if value:
        container_env[spec["password_env"]] = value
    payload = {
        "stack_op": op,
        "stack_engine": spec["engine"],
        "stack_image": spec["image"],
        "stack_network": spec["network"],
        "stack_service": spec["service"],
        "stack_command": spec["command"],
        "stack_path": spec["path"],
        "snapshot_bucket": SNAPSHOT_BUCKET,
        "snapshot_key": spec["snapshot_key"],
        "container_env": container_env,
        "node_kind": f"persistent-stack-{op}",
        "stack_id": stack.id,
        # Review2 N1: lets a loop worker serialise this op with the stack's
        # lifecycle ops (stackloop.go); the logical client itself ignores it.
        "stack_project": _project_name(stack),
    }
    from utils.paid_import import optional_paid
    _noah = optional_paid("services.noah")
    if _noah is not None:
        payload = _noah.add_pinnable_requirement(payload, db=db)
    row = models.ApiTaskQueue(
        node_id=f"stack-{op}-{spec['engine']}",
        tenant_id=stack.tenant_id,
        queue_name="CPU",
        priority=9,
        task_type="system",
        priority_class="normal",
        task_payload=payload,
        status=models.ApiTaskStatus.ASSIGNED,
        assigned_to_worker=stack.target_worker,
        max_attempts=1,
        output_namespace="attempt_0",
    )
    db.add(row)
    # FX.165 — same as the volume-tar path: wake after the caller's commit.
    _queue_worker_wake(db, row.queue_name, row.assigned_to_worker)
    logger.info("persistent stack %s logical %s dispatched (engine=%s, network=%s)",
                stack.id, op, spec["engine"], spec["network"])


def restore_logical_snapshot(db: Session, stack: models.PersistentStack, snap: dict) -> None:
    """GA.ASW.5 — the `pg_restore` counterpart of `restore_stack`'s per-volume
    tar loop, for a snapshot that has a `logical` component. Does NOT commit;
    the caller commits exactly as it already does for the tar path. Called
    ALONGSIDE the tar loop (not instead of it) for a mixed snapshot — see
    `restore_stack`."""
    # GT.D6 DS-3 — a logical restore needs the Paid engine; on CE 402.
    # 30.6.1 c1-J5b (Q-C1 = A) — the ONE exception on CE: a snapshot the c1-J1
    # seed commit wrote for an UPLOADED dump, which carries `source:"external"`
    # AND a `seed_id`. Both must be present, so a platform snapshot (`source`
    # absent / "manual" / "scheduled") can never be restored for free — and no
    # route lets a caller set or overwrite `source`/`seed_id` on an existing
    # snapshot (the only writers are `take_snapshot`'s server-side `source=`
    # literals, `_take_logical_snapshot`, and `services/stack_seed.py:281`; see
    # Result c1-J5b). Everything else still hits the unchanged 402 below.
    # Pro behaviour is byte-identical: when the Paid module is present we use it,
    # and it re-exports the same `build_logical_restore_spec` (c1-J5a parity).
    from utils.paid_import import optional_paid, require_paid
    _lb = optional_paid("services.stack_logical_backup")
    if _lb is None:
        if not (snap.get("source") == "external" and snap.get("seed_id")):
            require_paid("services.stack_logical_backup", "Logical stack backups")
        from services import stack_logical_restore as _lb

    key = (snap.get("logical") or {}).get("key")
    if not key:
        raise ValueError("logical snapshot has no dump object recorded")
    spec = _lb.build_logical_restore_spec(stack, key)
    _dispatch_logical_task(db, stack, "logical_restore", spec)


def _take_logical_snapshot(db: Session, stack: models.PersistentStack, engine: str,
                           source: str) -> dict:
    """GA.ASW.5 — a stack whose DB service explicitly opted in
    (`x-mod.backup: logical`) gets ONE snapshot id/prefix containing a
    `pg_dump` of that service PLUS the ordinary per-volume tar of every OTHER
    backed-up volume. The DB service's OWN data volume is excluded from the
    tar loop — the dump already covers it, and tarring a live Postgres data
    directory is not a consistent backup anyway. A DB-only stack (the bare
    `postgres` template) simply ends up with zero tar volumes; a gitea+
    postgres stack gets both parts under the one snapshot id.

    `volumes` in the snapshot record keeps the logical part too (under the
    `logical:<engine>` pseudo-key) so `_prune_snapshots`'s S3-cleanup loop —
    which only knows how to walk `volumes.values()` — deletes it on
    retention without needing to know this posture exists. `logical` carries
    the same key again, structured, for `restore_logical_snapshot`."""
    # GT.D6 DS-3 — only reachable when an engine was detected (Paid): on CE 402.
    from utils.paid_import import require_paid
    _lb = require_paid("services.stack_logical_backup", "Logical stack backups")

    project = _project_name(stack)
    exclude = {f"{project}_{v}" for v in _lb.db_service_volume_names(stack)}
    other_volumes = [v for v in _backed_up_volumes(stack) if v not in exclude]

    snap_id = uuid.uuid4().hex[:12]
    logical_key = f"{project}/{snap_id}/{engine}.dump"
    spec = _lb.build_logical_dump_spec(stack, logical_key)
    _dispatch_logical_task(db, stack, "logical_backup", spec)

    vol_keys = {}
    for v in other_volumes:
        key = f"{project}/{snap_id}/{v}.tar.gz"
        _dispatch_volume_task(db, stack.target_worker, stack.tenant_id, "snapshot", v,
                              SNAPSHOT_BUCKET, key, stack_id=stack.id)
        vol_keys[v] = key

    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    snaps[snap_id] = {"bucket": SNAPSHOT_BUCKET,
                      "volumes": {**vol_keys, f"logical:{engine}": logical_key},
                      "logical": {"engine": engine, "key": logical_key},
                      # GY.V2.s2 — the data-classification labels of the labelled
                      # bytes this backup covers, recorded so a restore (or an
                      # auditor reading the manifest) can see "this backup holds
                      # CUI / biometric / …". `unknown` when the labels could not
                      # be read — never an empty reading for a failed lookup.
                      "classification": _lb.backup_classification(db, stack),
                      "created_at": datetime.now(timezone.utc).isoformat(),
                      "source": source,
                      "verified_at": None, "verify_error": None}
    refs["snapshots"] = snaps
    stack.storage_refs = refs
    pruned = _prune_snapshots(stack, db)
    logger.info("persistent stack %s backup %s dispatched (%s, engine=%s, %d tar volume(s), pruned %d old)",
                stack.id, snap_id, source, engine, len(other_volumes), pruned)
    return {"snapshot_id": snap_id, "volumes": list(vol_keys.keys()), "engine": engine, "pruned": pruned}


def _backed_up_volumes(stack: models.PersistentStack) -> List[str]:
    """Project-prefixed volumes that a snapshot actually covers.

    §9.9 — model-weight caches are removed here. They are reconstructible by
    re-download, so including them adds tens of gigabytes to every snapshot to
    protect something that was never at risk. Host bind-mounts (how the managed
    vLLM stacks hold their weights) are already outside `_stack_volumes`; this
    catches the case where someone declares a NAMED volume mounted at a cache
    path, which nothing previously stopped.
    """
    # GT.D6 DS-3 — stack_backup is Paid: on CE nothing is excluded (CE stacks
    # carry no weight-cache volumes) (# CE-safe).
    from utils.paid_import import optional_paid
    _bk = optional_paid("services.stack_backup")

    project = _project_name(stack)
    excluded = {f"{project}_{v}" for v in _bk.weight_cache_volumes(stack)} if _bk is not None else set()
    keep = [v for v in _stack_volumes(stack) if v not in excluded]
    if excluded:
        logger.info("stack %s: excluding weight cache volume(s) %s from backup",
                    stack.id, sorted(excluded))
    return keep








def _mark_snapshot(db: Session, stack: models.PersistentStack,
                   snapshot_id: str, fields: dict) -> bool:
    """Merge fields into one snapshot's record. Does not commit."""
    refs = dict(stack.storage_refs or {})
    snaps = dict(refs.get("snapshots") or {})
    snap = dict(snaps.get(snapshot_id) or {})
    if not snap:
        return False
    snap.update(fields)
    snaps[snapshot_id] = snap
    refs["snapshots"] = snaps
    stack.storage_refs = refs
    return True


class BackupScheduleIn(BaseModel):
    """§9.1 — how often this stack is backed up.

    `interval_hours` null (or clear) removes the schedule, which is NOT the same
    as setting it to 0. Zero is "someone decided this stack needs no scheduled
    backups"; null is "nobody has decided", and the second is the state worth
    surfacing on a stack holding a customer's data.
    """
    interval_hours: Optional[int] = None
    clear: bool = False























class LLMIdleSettingsIn(BaseModel):
    """Platform-wide idle-reclaim defaults. Every field optional — a PUT that
    names one field changes only that field."""
    enabled: Optional[bool] = None
    idle_after_seconds: Optional[int] = Field(default=None, ge=60)
    warm_required_tools: Optional[List[str]] = None


class StackIdleOverrideIn(BaseModel):
    """Per-stack override. `null` means INHERIT the platform default, which is
    different from `false` (never reclaim this stack)."""
    idle_enabled: Optional[bool] = None
    idle_after_seconds: Optional[int] = Field(default=None, ge=60)
    clear: bool = False  # reset both fields to inherit

def _idle_settings_row(db: Session) -> models.PlatformLLMIdleSettings:
    """Fetch the singleton, creating it if the row is somehow missing."""
    from services import persistent_stacks_shared_service as _svc
    return _svc.idle_settings_row(db)






def _managed_ai_residency_lock_reason(stack) -> Optional[str]:
    """§5.5 — while System managed is in effect, its prewarm preference is the
    SINGLE control over managed-AI residency; a manual per-stack pin or idle
    override on a managed-AI stack would be a second answer to that one
    question, and two controls for one decision is how they end up disagreeing.

    Returns the refusal reason for such a change, or None when it is allowed:
    the stack is not a managed-AI stack (other stacks are unaffected — this is
    deliberately NOT a fleet-wide lock), or System managed is not in effect.
    Only ADDING a contradicting hold is refused; releasing a pin or clearing an
    override returns the stack to the preference and is always allowed.
    """
    try:
        from utils.paid_import import optional_paid
        _mai = optional_paid("services.managed_ai_stacks")
        if _mai is None:
            return None
        ALL_STACK_NAMES = _mai.ALL_STACK_NAMES
        from routers.ai_config import is_system_managed_effective
    except Exception:  # noqa: BLE001 — a missing import must never lock a stack
        return None
    if getattr(stack, "name", None) not in ALL_STACK_NAMES:
        return None
    if not is_system_managed_effective():
        return None
    return ("Managed-AI residency is governed by the prewarm preference in "
            "System managed mode. Change it there, or switch to Advanced to "
            "control this stack individually.")






class HostPinIn(BaseModel):
    """Pin a stack to a host, or release it.

    `worker` names the host; `null` (or clear) releases. A note is encouraged
    because a pin outlives the person who set it — six months on, "why can this
    stack never move" needs an answer that is not archaeology.
    """
    worker: Optional[str] = None
    reason: Optional[str] = None
    clear: bool = False


# ===========================================================================
# stack-definition-edit — everyone-facing stack composition+config editor
# ===========================================================================
# Edit an EXISTING stack's composition (services) and config (env/ports/
# volumes/reservations), revisioned + re-deployable. Modeled on node-code-
# edit-rebuild's edit/revision/rebuild shape
# (routers/node_code_edit.py) but for a stack's compose_spec instead of a
# node's source files. Gated by RBAC edit-permission on the stack (the
# router-wide STACK_AUTHOR platform capability + `_get_owned` tenant
# ownership — the same gate every other stack-mutating endpoint on this
# router already uses) PLUS the baseline `Cap.STACK_EDIT` capability (see
# services/persistent_stack_definition_service.require_stack_edit) — NOT the
# paid `Cap.STACK_WIZARD` and NOT a feature flag, per owner decision
# (clean-v2-api Wave 10): any tenant owner can edit their own stacks in every
# edition, gated by RBAC/ownership, not by license tier.


class StackDefinitionUpdate(BaseModel):
    """Save an edit to a stack's composition+config as a NEW revision.

    Exactly one of `services` / `compose_spec` / `compose_yaml` must be given:
      - `services`: the SAME `ServiceSpec` list shape `/assemble` accepts at
        create time — reuses the frontend's existing service-builder form and
        the exact same rendering path (Group B/C source resolution), so
        create-time and edit-time composition share one renderer.
      - `compose_spec`: a raw compose project (dict) for a caller that already
        holds one (e.g. round-tripping GET .../definition's `compose_spec`, or
        a hand-edited docker-compose import). Validated the same way either
        path produces it.
      - `compose_yaml`: the same compose project as raw docker-compose YAML
        text (e.g. round-tripping GET .../definition's `compose_yaml`, or a
        user pasting a normal docker-compose.yml — the Dashboard editor is
        YAML-native). Parsed with `services.stack_compose_import.
        normalize_compose` — the SAME safe-YAML parser the `/import-compose`
        endpoints use, so there is exactly one YAML-ingestion path in this
        router — then validated identically to `compose_spec`. JSON is valid
        YAML, so JSON text works here too.
    """
    services: Optional[List[ServiceSpec]] = None
    compose_spec: Optional[Dict[str, Any]] = None
    compose_yaml: Optional[str] = None
    message: Optional[str] = None
    # GX.S10a — optional variables to set/declare on this revision.
    # {NAME: {kind, value?, existing_path?, file?, mode?, services?,
    #          cog_id?, from_cog_id?, pin_version?}}. Secret `value`s are
    # written to Vault and the stored spec keeps refs only (never the value);
    # a detected placeholder left undeclared is a 422 naming it.
    variables: Optional[Dict[str, Any]] = None


class StackRevertRequest(BaseModel):
    target_revision_no: int = Field(..., ge=1)


class StackUpgradeRequest(BaseModel):
    """GA.ASW.7 — body for POST /{stack_id}/upgrade: the new pinned compose
    spec to try, and how long to wait for it to grade healthy before an
    automatic rollback to the prior spec."""
    new_spec: Dict[str, Any]
    window_s: Optional[float] = Field(None, gt=0)


class StackStreamingMigrationStart(BaseModel):
    """GA.ASW.10.7 — body for POST /{stack_id}/migrate-streaming: which stack
    to migrate the live database onto, plus the optional gates
    `services.stack_streaming_migration` already knows how to use — an
    optional customer smoke query the verify phase runs against the standby
    before cutover is allowed, and how long the cutover phase waits for the
    standby to drain to zero lag / turn healthy after promote before it
    auto-rolls-back (`start_streaming_migration`'s `cutover_window_s`)."""
    target_stack_id: int
    smoke_sql: Optional[str] = None
    cutover_window_s: Optional[float] = Field(None, gt=0)


class StackStreamingMigrationAbort(BaseModel):
    """GA.ASW.10.7 — optional operator-supplied reason recorded on the
    `rolled_back` migration record (POST .../migrate-streaming/abort)."""
    reason: Optional[str] = None


def host_pin_status(db: Session, stack) -> Optional[dict]:
    """§8.4 — what the pin means for this stack right now.

    A pin whose host can no longer satisfy the stack is the case that matters.
    The system must NEVER quietly relocate in that situation: the operator
    asserted a constraint the placement model cannot see, so overriding it
    silently converts their explicit instruction into a mystery outage on the
    machine they were avoiding. It is reported as unplaceable, naming the pin
    as the cause, so the fix is theirs to choose.
    """
    from services import persistent_stacks_shared_service as _svc
    return _svc.host_pin_status(
        db, stack, aggregate_accelerator_demand=_aggregate_accelerator_demand)


# ===========================================================================
# service-stack-builder Group A — assemble, estimate, expose
# ===========================================================================
# Compose N services (image / node / git / script / site) into a validated
# `compose_spec.services`, price the reservation vector, and mark exposed
# services `x-mod.reachable` (mesh-routed, no host-port publish). Endpoints are
# already behind the router-wide `require_platform_capability(STACK_AUTHOR)`
# gate (admins bypass on is_platform_admin), so a non-author gets 403 before any
# body is read. Validation runs as the caller's own privilege
# (`is_admin=is_platform_admin`), so a delegated author's forbidden mount /
# admin-only construct is rejected 422 exactly as on the create paths.


def _fragment_to_dict(fragment: Any) -> dict:
    """Normalise a source resolver's output (a `ServiceFragment` pydantic model
    OR a plain dict) into a compose-fragment dict. Resolvers are lazily imported
    from sibling Group B/C modules; either return shape is accepted so a parallel
    build that ships one shape or the other still composes."""
    if fragment is None:
        return {}
    if isinstance(fragment, dict):
        return dict(fragment)
    dump = getattr(fragment, "model_dump", None)
    if callable(dump):
        return dump()
    return dict(getattr(fragment, "__dict__", {}) or {})


def _resolve_service_fragment(db: Session, spec: ServiceSpec) -> tuple:
    """Resolve one service's source into a (fragment_dict, note).

    `image` resolves INLINE — the endpoint works with zero cross-module
    dependency. Every other source is resolved by LAZY import of its Group B/C
    resolver, each wrapped so a sibling module not yet present yields a clear
    422 "source not available" for that service rather than an import error that
    takes the whole endpoint down.
    """
    src = spec.source
    kind = src.kind

    if kind == "image":
        if not src.image:
            raise HTTPException(status_code=422,
                                detail=f"service {spec.name!r}: image source needs an 'image'")
        return ({"image": src.image}, None)

    if kind == "node":
        try:
            from node_builder.service_from_node import resolve_node_as_service
        except Exception as e:  # noqa: BLE001
            raise internal_http_error(422, "Node source not available", e) from e
        try:
            frag = resolve_node_as_service(db, src.node_definition_id)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise internal_http_error(422, "Could not resolve node source", e) from e
        return (_fragment_to_dict(frag), "resolved from MOD node")

    if kind in ("git", "script", "site"):
        try:
            from node_builder.service_sources import (
                service_from_git, service_from_script,
                service_from_static_site, service_from_image)  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise internal_http_error(422, "Service source not available", e) from e
        try:
            if kind == "git":
                frag = service_from_git(repo_url=src.repo_url, ref=src.ref,
                                        build_context=src.build_context)
                note = "built from git source"
            elif kind == "script":
                frag = service_from_script(script=src.script, base_image=src.base_image,
                                           filename=src.filename)
                note = "packaged from script"
            else:
                frag = service_from_static_site(site_source=src.site_source,
                                                site_ref=src.site_ref)
                note = "static/live site"
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise internal_http_error(422, "Service source failed to build", e) from e
        return (_fragment_to_dict(frag), note)

    raise HTTPException(status_code=422,
                        detail=f"service {spec.name!r}: unknown source kind {kind!r}")


def _compose_service_from_spec(spec: ServiceSpec, fragment: dict) -> dict:
    """Render one ServiceSpec + its resolved fragment into a compose service.

    Long-lived by construction (`restart: unless-stopped`). Container ports are
    emitted as compose `expose` (documented to the compose network) and NEVER as
    a host-port `ports:` publish — exposed services route over the mesh, internal
    services bind no host port (stack-public-exposure). Env/volumes/GPU/replicas
    from the spec are merged over the fragment.
    """
    svc: dict = {}
    if fragment.get("image"):
        svc["image"] = fragment["image"]
    if fragment.get("build"):
        svc["build"] = fragment["build"]

    command = spec.command or fragment.get("command")
    if command:
        svc["command"] = command

    env: dict = {}
    env.update(fragment.get("environment") or {})
    env.update(spec.env or {})
    if env:
        svc["environment"] = env

    # Container ports -> `expose` only. No host mapping is ever emitted.
    ports = list(dict.fromkeys([*(fragment.get("ports") or []), *(spec.ports or [])]))
    if ports:
        svc["expose"] = [str(p) for p in ports]

    if spec.volumes:
        svc["volumes"] = list(spec.volumes)

    svc["restart"] = fragment.get("restart") or "unless-stopped"

    # deploy: replicas + resource reservations (cpu / mem / gpu), merged over any
    # reservations the resolver already emitted.
    #
    # RESERVED == LIMIT == BILLED. A compose `reservation` is only a floor
    # (guaranteed minimum); on the shared-worker ICP that is not enough — a
    # service reserving 2 cpu could burn the whole host, which both under-bills
    # (the meter prices the reservation vector) and starves the pool for other
    # tenants. So we ALSO emit `deploy.resources.limits` = the reserved cpu/mem:
    # a service's reservation is its hard ceiling, so what it can use == what it
    # reserved == what it pays. GPU needs no cpu/mem-style limit — a device
    # reservation is whole-device (exclusive), so the count IS the cap.
    deploy = dict(fragment.get("deploy") or {})
    frag_res = dict(((deploy.get("resources") or {}).get("reservations") or {}))
    reservations = dict(frag_res)
    limits: dict = {}
    if spec.cpu and spec.cpu > 0:
        reservations["cpus"] = str(spec.cpu)
        limits["cpus"] = str(spec.cpu)
    if spec.mem_gb and spec.mem_gb > 0:
        reservations["memory"] = f"{spec.mem_gb}G"
        limits["memory"] = f"{spec.mem_gb}G"
    if spec.gpu and spec.gpu.vram_gb > 0:
        devices = list(frag_res.get("devices") or [])
        devices.append({"driver": "nvidia", "count": 1, "capabilities": ["gpu"]})
        reservations["devices"] = devices
    if spec.replicas and spec.replicas != 1:
        deploy["replicas"] = spec.replicas
    if reservations:
        deploy.setdefault("resources", {})["reservations"] = reservations
    if limits:
        deploy.setdefault("resources", {})["limits"] = limits
    if deploy:
        svc["deploy"] = deploy

    xmod: dict = {}
    # GPU size lives in x-mod.accelerator (vram-aware) — the reservation vector
    # (_capture_reservation_units) and placement read size from here, since a
    # compose device reservation expresses count, not VRAM.
    if spec.gpu and spec.gpu.vram_gb > 0:
        xmod["accelerator"] = {"required": True,
                               "min_vram_gb": float(spec.gpu.vram_gb),
                               "count": 1, "kind": "cuda"}
    # Expose publicly -> reachable over the mesh (Caddy magic URL); the host-port
    # publish is already absent above.
    if spec.expose:
        xmod["reachable"] = True
    if xmod:
        svc["x-mod"] = xmod

    return svc


def _collect_named_volumes(services: dict) -> set:
    """Named volume sources referenced by services (excludes host/relative binds)
    so they can be declared at the top level, which the compose validator
    requires."""
    declared: set = set()
    for svc in services.values():
        for mount in (svc.get("volumes") or []):
            if isinstance(mount, str) and ":" in mount:
                src = mount.split(":")[0]
                if src and not src.startswith((".", "/", "~")):
                    declared.add(src)
    return declared










# ============================================================================
# MCP tool fleet — /v1/stack/* (stack:author scope + stack:author capability)
# ----------------------------------------------------------------------------
# Locked-down mechanical persistent-stack lifecycle: list / create / get /
# start / stop. Gated by the stack:author API-key scope AND (parity with the
# in-app router's require_platform_capability(STACK_AUTHOR)) the key owner's
# stack:author platform capability. The LLM-backed /stacks/from-wizard
# path is deliberately NOT routed here.


# Restore the pre-split module identity on every class DEFINED here (Pydantic
# request/response models, etc.) so `_model_name` / OpenAPI report them under
# the package (`routers.persistent_stacks`) exactly as before the split — the
# contract snapshot compares request_model/response_model by `module:name`.
_pkg_name = __name__.rsplit(".", 1)[0]
for _obj in list(globals().values()):
    if isinstance(_obj, type) and getattr(_obj, "__module__", None) == __name__:
        try:
            _obj.__module__ = _pkg_name
        except Exception:
            pass

# Export EVERY symbol (incl. underscore helpers + imported names) so a
# `from ._shared import *` reproduces the pre-split module namespace in
# each domain sub-module and in the package __init__ aggregator.
__all__ = [__n for __n in list(globals()) if not __n.startswith('__')]
