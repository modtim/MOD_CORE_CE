"""RunPod burst provider (launch-completion-megaplan group H, CLOUD compute):
the sixth real cloud target for bursting into the CUSTOMER's own cloud
account, after EC2, Azure, GCP, Lambda and OCI. Modelled directly on
`services/burst_provider_lambda.py` (direct REST over `httpx`, duck-typed
client, redaction, probe, factory + `register_provider`) — RunPod has no
apache-libcloud driver either.

WHAT THE POD RUNS (the "user_data" equivalent)
----------------------------------------------
RunPod pods are **containers**, not cloud instances with cloud-init: there is
no `user_data`/cloud-init concept at all. The bootstrap script MOD normally
renders (`burst_bootstrap.render_burst_user_data`, a bash cloud-init script)
is therefore handed to the pod as its **start command**: the pod's
`dockerStartCmd` carries `/bin/sh -lc <script>`, and the same script is also
put in the pod's `env` map under `MOD_BURST_BOOTSTRAP` so a RunPod image whose
start command is ignored by its own init still has the script available.
Consequence, stated plainly: the script runs as the pod's PID-1 workload, so
the pod's own idle/max-lifetime self-destruct units inside it (systemd
timers, `shutdown -h`) are meaningless in a container — see the SELF-DESTRUCT
WARNING below. The one-time enrol token the script carries is a SECRET: it is
never logged, never put in an error string, and redacted exactly like the API
key (`redact_values=(api_key, enrol_token)` — the factory cannot see the
token, so `provision()` adds it to the redaction set for the rest of the
provider's lifetime).

Endpoint / field assumptions (FROM MEMORY of RunPod's public REST docs, NOT
verified against a live account or a fetched OpenAPI spec — every one of
these MUST be checked against a real account in the live-account test before
this adapter is trusted with real money; nothing here blocks on them and
nothing here calls the API to check them):

  * Base URL ``https://rest.runpod.io/v1``; auth ``Authorization: Bearer
    <api_key>``.
  * ``POST /pods`` <- ``{"name", "type" ("GPU"|"CPU"), "gpuNames" (list of
    GPU type names), "cloudInstance", "hardware" ({"gpuType": ...}),
    "dockerImage", "containerAttrs", "volumeMounts", "env" ({"<K>":
    {"value": "<V>"}}), "dockerStartCmd", "template", "imageRef", "networking"
    ({"httpPort": ...}), "autoDeletePods", "flashMemory", "secure"}`` ->
    ``{"id", "name", "pods"...}`` (a single pod object; the exact response
    shape is a guess — `_node_dict` reads `id`/`name` defensively).
  * ``GET /pods`` -> ``{"pods": [{"id", "name", "type", "gpuNames", "gpuType",
    "cpuType", "created", "started", "ended", "uptimeSeconds", "cpuPercent",
    "gpuPercent", "diskUsage", "networkMode", "containerIds", "streamUrl",
    "deviceSharePath", "tensorBoardUrlBase", "sshKeysInherit", "active",
    "subPods"}]}``.
  * ``GET /pods/{id}`` -> one pod object, same fields.
  * ``DELETE /pods/{id}`` -> terminates one pod (RunPod has no batch
    terminate; teardown issues one DELETE per pod).
  * ``GET /account`` -> ``{"username", "userId", "activeSubscriptions",
    "access", "gpu", "membership", "streamLink", "deviceShareLink",
    "apiKeys", "pods", "vector", "gpuPerHour", "gpuPerSecond"}`` (probe's
    read-only credential check).
  * Capacity-exhausted creates return HTTP 400 with a body shaped like
    ``{"error": {"code": "...", "message": "..."}}`` where the code or
    message mentions "capacity" (e.g. an `insufficient-capacity`-style code).
    The exact code string is a guess — `_is_capacity_error` matches the
    substring "capacity" in either field specifically so it survives the
    exact spelling being wrong; the live test must confirm at least one real
    capacity-rejection body shape.

*** SELF-DESTRUCT WARNING ***
A RunPod pod CANNOT power itself off: `shutdown -h` has no meaning inside the
container, and the pod must NOT carry the RunPod API key inside it to delete
itself (that would hand a tenant-wide credential to whatever runs in the pod,
and RunPod's own `autoDeletePods` flag is account-wide, not per-pod, so it is
never set here). Therefore the MOD side is the ONLY reaper:
`services/burst_reconciler.py`'s `provider.list_tagged` + `provider.teardown`
sweep is what ends an orphaned RunPod pod's billing. If the reconciler cadence
lapses for this provider, a RunPod pod bills indefinitely — worse than an
EC2/Azure orphan, which at least has a chance of self-terminating past
`max_lifetime_min`. To let the reconciler apply the max-lifetime ceiling
without a second clock, every node dict this adapter returns carries
`created` (the pod's creation time, ISO-8601 UTC) and `expired` (True when the
pod is older than `max_lifetime_min`) — see `list_tagged`/`status` below.

Tags: RunPod pods have names and env, not cloud tags. MOD pods are marked by
a NAME PREFIX that embeds `mod_tag_value()` (see `services.burst_provider`),
exactly like the Lambda adapter's `_build_marked_name`/`_has_marker`.
`teardown` refuses (raises `PermissionError`, touching nothing) any pod whose
current name does not carry the marker — the same non-negotiable safety guard
the EC2/Azure/Lambda/GCP/OCI adapters have, and all-or-nothing on a mixed
batch: if one requested pod fails the check, NO pod is deleted.

Capacity: `provision()` raises `BurstCapacityUnavailable` (imported from
`services.burst_provider_lambda`, as `services/burst_provider_oci.py` does)
when the create call is rejected specifically for lack of capacity. Callers
SHOULD catch this distinctly from a bare `Exception`/`RuntimeError` and treat
it as transient, supply-side unavailability: do not mark the tenant's burst
account invalid, do not disable burst, just skip/retry this attempt.
"""
from __future__ import annotations

import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from services.burst_provider import (
    MOD_TAG_KEY,
    has_mod_tag,
    mod_tag_value,
    register_provider,
)
from services.burst_provider_ec2 import arm_self_destruct
from services.burst_provider_lambda import BurstCapacityUnavailable
from services.burst_bootstrap import (
    DEFAULT_IDLE_MINUTES as BURST_DEFAULT_IDLE_MINUTES,
    DEFAULT_MAX_LIFETIME_HOURS as BURST_DEFAULT_MAX_LIFETIME_HOURS,
)

BASE_URL = "https://rest.runpod.io/v1"
DEFAULT_REGION = "SECURE"  # RunPod "cloudInstance": "SECURE" == private GPU
DEFAULT_CLOUD_INSTANCE = "SECURE"  # community/spot cloud ONLY if configured
DEFAULT_TIMEOUT_S = 15.0
# Hard adapter ceiling on pods per provision() call — belt-and-braces ONLY,
# the account limits (burst_accounts.limits.max_instances) are the real cap
# and the reconciler sweep is the real reaper. No default here can create an
# unbounded number of pods.
DEFAULT_MAX_PODS_PER_CALL = 4


def default_max_pods_per_call(ce: "bool | None" = None) -> int:
    """FIX-CE-UNLIMITED-F: the paid adapter ceiling is
    DEFAULT_MAX_PODS_PER_CALL (4, byte-for-byte); Community Edition has no
    quantity caps so it gets 0 = no per-call bound (the account's
    limits.max_instances stays the real cap). On CE this client applies no
    per-call pod-count limit of its own when no value is supplied; an
    explicit value is honoured (0 explicit = reject all). The provider's own
    limits are not asserted here. Shared `_ce_unlimited` helper: an erroring
    edition probe returns the PAID value."""
    from services.platform_settings import _ce_unlimited
    return _ce_unlimited(DEFAULT_MAX_PODS_PER_CALL, unlimited=0, ce=ce)
# burst_bootstrap's defaults, imported (not copied): idle self-destruct 30
# min, hard max lifetime 12 h -> max_lifetime_min default 720.
DEFAULT_IDLE_MINUTES = BURST_DEFAULT_IDLE_MINUTES
DEFAULT_MAX_LIFETIME_MIN = BURST_DEFAULT_MAX_LIFETIME_HOURS * 60
# Env key under which the bootstrap script is ALSO carried (see the
# "WHAT THE POD RUNS" section). Not a behaviour tunable and not a secret:
# the tunables live in the platform-settings registry, the token is a
# per-provision secret handled by redaction.
_BOOTSTRAP_ENV_KEY = "MOD_BURST_BOOTSTRAP"
# Matches burst_reconciler.BURST_INSTANCE_TAG_KEY. Duplicated as a literal
# (not imported) deliberately, exactly as the Lambda adapter does: this key
# is only used to pick a value OUT of the `tags` dict a caller passes and to
# reconstruct it later from the sanitised pod name.
_INSTANCE_LABEL_TAG_KEY = "mod-burst-instance"
# Node-dict keys the reconciler needs to apply the max-lifetime ceiling.
_CREATED_KEY = "created"
_EXPIRED_KEY = "expired"

_NAME_PREFIX = f"{mod_tag_value()}-"
_MAX_NAME_LEN = 64  # conservative guess — verify RunPod's real limit live
_NAME_CHARSET_RE = re.compile(r"[^A-Za-z0-9_-]")


# ============================================================================
# GPU size map — MOD VRAM need -> RunPod gpuType name
# ============================================================================
# `price` is an approximate list $/hour figure (APPROX — RunPod's price list
# changes and varies by region/availability; this is for ranking "cheapest
# fitting" ONLY, never for billing). Like the Lambda map this deliberately
# carries no vcpu/ram_gb: those per-type specs are not reliably known and are
# left out rather than guessed.
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "gpu_1x_a10":          {"gpu": "A10",   "count": 1, "vram_gb": 24, "price": 0.50},
    "gpu_1x_a100_40gb":    {"gpu": "A100",  "count": 1, "vram_gb": 40, "price": 0.90},
    "gpu_1x_a100_80gb":    {"gpu": "A100",  "count": 1, "vram_gb": 80, "price": 1.30},
    "gpu_1x_h100_80gb":    {"gpu": "H100",  "count": 1, "vram_gb": 80, "price": 1.99},
    "gpu_1x_gh200_141gb":  {"gpu": "GH200", "count": 1, "vram_gb": 141, "price": 1.99},
    "gpu_2x_a6000":        {"gpu": "A6000", "count": 2, "vram_gb": 48, "price": 1.10},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped RunPod type; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Cheapest mapped RunPod type whose total VRAM >= `min_vram_gb`. Same
    semantics as the EC2/Azure/Lambda `pick_instance_type`: an
    `allowed_instance_types` allowlist (the account's
    `limits.allowed_instance_types`) restricts the choice; `ValueError` when
    the need is non-positive or nothing fits."""
    try:
        need_val = float(min_vram_gb)
    except (TypeError, ValueError):
        raise ValueError(f"min_vram_gb must be a number, got {min_vram_gb!r}")
    if need_val <= 0:
        raise ValueError(f"min_vram_gb must be > 0, got {min_vram_gb!r}")

    allowed = (
        set(allowed_instance_types)
        if allowed_instance_types is not None
        else None
    )
    candidates: list[tuple[float, str]] = []
    for itype, spec in GPU_SIZE_MAP.items():
        if allowed is not None and itype not in allowed:
            continue
        if spec["count"] * spec["vram_gb"] >= need_val:
            candidates.append((float(spec["price"]), itype))
    if not candidates:
        where = (
            f" within allowed {sorted(allowed)}" if allowed is not None else ""
        )
        raise ValueError(
            f"no RunPod GPU instance in the size map provides >= "
            f"{min_vram_gb} GB VRAM{where}; mapped types: {sorted(GPU_SIZE_MAP)}"
        )
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# Name-marker helpers (RunPod's stand-in for tags)
# ============================================================================

def _sanitize_name_part(s: Any) -> str:
    s = _NAME_CHARSET_RE.sub("-", str(s))
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "x"


def _build_marked_name(label: Any = None) -> str:
    suffix = _sanitize_name_part(label) if label else uuid.uuid4().hex[:8]
    return f"{_NAME_PREFIX}{suffix}"[:_MAX_NAME_LEN]


def _has_marker(name: Any) -> bool:
    return str(name or "").startswith(_NAME_PREFIX)


def _tags_from_name(name: Any) -> dict[str, Any]:
    """Best-effort reconstruction of a tags dict from a marked pod name. Only
    ever produces `MOD_TAG_KEY` and (if a suffix survives) the instance label
    — RunPod has nowhere else a caller's stamp can round-trip through."""
    name = str(name or "")
    if not _has_marker(name):
        return {}
    tags: dict[str, Any] = {MOD_TAG_KEY: mod_tag_value()}
    suffix = name[len(_NAME_PREFIX):]
    if suffix:
        tags[_INSTANCE_LABEL_TAG_KEY] = suffix
    return tags


# ============================================================================
# RunpodBurstProvider
# ============================================================================

class RunpodBurstProvider:
    """BurstProvider for RunPod, implemented directly over RunPod's public
    REST API (no libcloud driver exists for RunPod).

    `client` is any object exposing `.get(path, params=None, timeout=...)`,
    `.delete(path, timeout=...)` and `.post(path, json=None, timeout=...)`
    returning a response with `.status_code` and `.json()` — in production an
    `httpx.Client` built by `_runpod_factory` (base_url + Bearer header baked
    in, via `_build_client`, the ONLY place a real client is built); in tests
    a hand-written duck-typed stand-in, exactly like the Lambda adapter's
    `FakeLambdaClient`. This class never touches the network directly, only
    through `client`.
    """

    def __init__(
        self,
        client: Any,
        *,
        region: str = DEFAULT_REGION,
        cloud_instance: str | None = None,
        gpu_type: str | None = None,
        hardware: dict[str, Any] | None = None,
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        max_pods_per_call: "int | None" = None,
        redact_values: tuple[str, ...] | list[str] = (),
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._client = client
        self.region = region
        # Community/spot cloud ONLY when the account config says so; the
        # default is the secure (private) cloud instance.
        self.cloud_instance = cloud_instance or DEFAULT_CLOUD_INSTANCE
        self.gpu_type = gpu_type
        self.hardware = dict(hardware) if hardware else None
        self.max_lifetime_min = int(max_lifetime_min)
        # None = edition default (paid 4, CE unbounded). An explicit account
        # value ALWAYS wins on both editions and is kept as a plain int:
        # explicit 0 = reject every n (as on paid before); it is never
        # "unbounded". CE-unbounded is a separate flag set only when the
        # value was not supplied and the edition default is 0.
        if max_pods_per_call is None:
            edition_default = default_max_pods_per_call()
            self._unbounded = edition_default == 0
            self.max_pods_per_call = (
                DEFAULT_MAX_PODS_PER_CALL if self._unbounded
                else int(edition_default)
            )
        else:
            self._unbounded = False
            self.max_pods_per_call = int(max_pods_per_call)
        self._redact_values = [v for v in redact_values if v]
        self._timeout = timeout
        # Injectable clock for the `expired` flag (tests pass a fake clock;
        # nothing sleeps). None = use the real clock, fail-closed.
        self._now: datetime | None = None

    def set_clock(self, now: datetime | None) -> None:
        """Pin the clock used for the `expired` flag (tests only; the
        reconciler passes its own `now` when it sweeps)."""
        self._now = now

    # -- transport ---------------------------------------------------------

    def _redact(self, message: Any) -> str:
        s = str(message)
        for v in self._redact_values:
            if v:
                s = s.replace(v, "[REDACTED]")
        return s

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._client.get(path, params=params, timeout=self._timeout)

    def _post(self, path: str, json_body: dict[str, Any]) -> Any:
        return self._client.post(path, json=json_body, timeout=self._timeout)

    def _delete(self, path: str) -> Any:
        return self._client.delete(path, timeout=self._timeout)

    @staticmethod
    def _is_capacity_error(status_code: int, body: Any) -> bool:
        if not isinstance(body, dict):
            return False
        err = body.get("error")
        if not isinstance(err, dict):
            err = {}
        code = str(err.get("code") or body.get("code") or "").lower()
        msg = str(err.get("message") or body.get("message") or "").lower()
        return "capacity" in code or "capacity" in msg

    def _check(self, resp: Any, *, action: str) -> Any:
        """Raise on any non-2xx response; return the parsed JSON body on
        success. Every raised message is redacted before it can be
        logged/persisted (the API key and any enrol token are in
        `_redact_values`)."""
        try:
            body = resp.json()
        except Exception:
            body = None
        status = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= status < 300:
            return body
        if action == "create-pod" and self._is_capacity_error(status, body):
            err = (body or {}).get("error") or {}
            raise BurstCapacityUnavailable(self._redact(
                f"runpod has no available capacity for this pod "
                f"(HTTP {status}, code={err.get('code')!r}): "
                f"{err.get('message') or ''}"
            ))
        detail = body if body is not None else getattr(resp, "text", "")
        raise RuntimeError(self._redact(
            f"runpod API {action} failed: HTTP {status}: {detail}"
        ))

    def _list_pods(self) -> list[dict[str, Any]]:
        resp = self._get("/pods")
        body = self._check(resp, action="list-pods") or {}
        items = body.get("pods")
        if not isinstance(items, list):
            items = body.get("data") if isinstance(body.get("data"), list) else []
        return items

    def _pods_by_id(self) -> dict[str, dict[str, Any]]:
        return {str(item.get("id")): item for item in self._list_pods()}

    def _node_dict(self, item: dict[str, Any]) -> dict[str, Any]:
        """Plain node dict + the max-lifetime fields the reconciler needs
        (`created`, `expired`) — see the SELF-DESTRUCT WARNING."""
        created = item.get("created") or item.get("started")
        return {
            "id": str(item.get("id") or ""),
            "state": self._state_of(item),
            "public_ip": None,  # a pod is a container: no cloud IP to report
            "private_ip": None,
            "tags": _tags_from_name(item.get("name")),
            _CREATED_KEY: created,
            _EXPIRED_KEY: self._is_expired(created, now=self._now),
        }

    def _state_of(self, item: dict[str, Any]) -> str:
        if item.get("terminated") is True or item.get("active") is False:
            return "terminated"
        return str(item.get("state") or item.get("status") or "running").lower()

    def _is_expired(self, created: Any, now: Any = None) -> bool:
        """True when the pod is older than `max_lifetime_min`. Clock-free and
        fail-closed: an unparseable/missing creation time is NOT reported
        expired (the reconciler's own boot-grace handles young pods). `now`
        is injectable (tests pass a fake clock; nothing sleeps)."""
        raw = str(created or "").replace("Z", "+00:00")
        try:
            launched = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if launched.tzinfo is None:
            launched = launched.replace(tzinfo=timezone.utc)
        ref = now if isinstance(now, datetime) else datetime.now(timezone.utc)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        return launched <= ref - timedelta(minutes=self.max_lifetime_min)

    # -- BurstProvider protocol ---------------------------------------------

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        # No network call: static approx-price map only, same spirit as
        # FakeBurstProvider. `available` is unknown without a live capacity
        # check (see `probe()`), so it is left None rather than guessed.
        spec = GPU_SIZE_MAP.get(instance_type)
        price = float(spec["price"]) if spec else None
        return {
            "provider": "runpod",
            "instance_type": instance_type,
            "n": n,
            "region": region,
            "available": None,
            "price_per_hour": price,
            "estimated_cost_per_hour": (
                round(price * n, 6) if price is not None else None
            ),
        }

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Create `n` pods. Every pod carries the MOD marker in its name and
        the bootstrap script as its start command.

        COST SAFETY, fail closed (no HTTP call happens on any rejection):
        `n` must be >= 1 and <= `max_pods_per_call` (adapter ceiling, default
        4 on paid; no per-call bound on Community Edition when no value is
        supplied; an explicit 0 rejects every n); a pod request without a GPU type / cloud type raises before any
        HTTP call; community/spot cloud only if the account config said so.
        """
        if not has_mod_tag(tags):
            raise ValueError(
                "provision() requires the MOD tag so teardown/list_tagged "
                "can find these pods and nothing else"
            )
        try:
            count = int(n)
        except (TypeError, ValueError):
            raise ValueError(f"n must be an integer, got {n!r}")
        if count < 1:
            raise ValueError(f"n must be >= 1, got {n!r}")
        if not self._unbounded and count > self.max_pods_per_call:
            raise ValueError(
                f"n must be <= {self.max_pods_per_call} (adapter ceiling; "
                f"the account's limits.max_instances is the real cap) — "
                f"nothing here can create an unbounded number of pods"
            )
        gpu_type = self.gpu_type or GPU_SIZE_MAP.get(
            instance_type, {}
        ).get("gpu")
        if not gpu_type:
            raise ValueError(
                f"no GPU type for instance_type {instance_type!r}: RunPod "
                f"needs an explicit gpuType (or cloudInstance) before any "
                f"pod is created"
            )
        if not self.cloud_instance:
            raise ValueError(
                "cloudInstance is required (default SECURE); a pod request "
                "without a cloud type must not reach RunPod"
            )

        label = tags.get(_INSTANCE_LABEL_TAG_KEY)
        name = _build_marked_name(label)
        # The bootstrap script is a cloud-init script; RunPod has no cloud-
        # init, so it is handed over as the pod's start command (and in env,
        # see the module docstring). `arm_self_destruct` is imported from the
        # EC2 adapter and splices the hard max-lifetime line in — it is
        # documented NOT to stop RunPod billing (SELF-DESTRUCT WARNING); it
        # is carried so an orphan at least stops doing work.
        armed = arm_self_destruct(user_data, self.max_lifetime_min)
        # The script carries the one-time enrol token: add it to the
        # redaction set so it can never appear in an error string/log.
        if armed not in self._redact_values:
            self._redact_values.append(armed)
        payload: dict[str, Any] = {
            "name": name,
            "type": "GPU",
            "gpuNames": [gpu_type],
            "cloudInstance": self.cloud_instance,
            "hardware": dict(self.hardware) if self.hardware
            else {"gpuType": gpu_type},
            "dockerStartCmd": f"/bin/sh -lc {shlex.quote(armed)}",
            "env": {_BOOTSTRAP_ENV_KEY: {"value": armed}},
        }
        out: list[dict[str, Any]] = []
        for _ in range(count):
            resp = self._post("/pods", payload)
            body = self._check(resp, action="create-pod") or {}
            pod_id = body.get("id") or (
                (body.get("pods") or [{}])[0].get("id")
                if isinstance(body.get("pods"), list)
                else None
            )
            out.append({
                "id": str(pod_id or ""),
                "state": "booting",
                "public_ip": None,
                "private_ip": None,
                "tags": dict(tags),
                _CREATED_KEY: None,
                _EXPIRED_KEY: False,
            })
        return out

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        by_id = self._pods_by_id()
        return [self._node_dict(by_id[i]) for i in node_ids if i in by_id]

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD (name-marked) pods ONLY. A pod whose current name does
        not carry the marker is refused — nothing in `node_ids` is deleted if
        even one of them fails the check, mirroring the EC2/Azure/Lambda
        adapters' "refuse, touch nothing" guard."""
        by_id = self._pods_by_id()
        to_terminate: list[str] = []
        for i in node_ids:
            item = by_id.get(i)
            if item is None:
                continue  # unknown id: nothing to refuse or destroy
            if not _has_marker(item.get("name")):
                raise PermissionError(
                    f"refusing to tear down runpod pod {i}: "
                    f"name {item.get('name')!r} carries no MOD marker"
                )
            to_terminate.append(i)
        if not to_terminate:
            return []
        out = []
        for i in to_terminate:
            d = self._node_dict(by_id[i])
            resp = self._delete(f"/pods/{i}")
            self._check(resp, action="terminate")
            d["state"] = "terminated"
            out.append(d)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in self._list_pods():
            if not _has_marker(item.get("name")):
                continue
            d = self._node_dict(item)
            ntags = d["tags"]
            if all(ntags.get(k) == v for k, v in tags.items()
                   if k != MOD_TAG_KEY):
                out.append(d)
        return out

    def probe(self) -> dict[str, Any]:
        """Read-only credential/capability probe: `GET /account` (confirms the
        key is valid and the API is reachable) + `list_tagged` (via `GET /pods
        `). Returns ``{ok, region, visible_tagged, error}``, matching the
        Lambda/EC2 adapters' probe shape. On ANY exception `ok` is False and
        the API key is redacted from the error.
        """
        result: dict[str, Any] = {
            "ok": False,
            "region": self.region,
            "visible_tagged": None,
            "error": None,
        }
        try:
            resp = self._get("/account")
            self._check(resp, action="get-account")
            tagged = self.list_tagged({MOD_TAG_KEY: mod_tag_value()})
            result["visible_tagged"] = len(tagged)
            result["ok"] = True
        except Exception as e:  # noqa: BLE001 — probe must never raise
            result["error"] = self._redact(e)
        return result


# ============================================================================
# Client factory + registration
# ============================================================================

def _build_client(api_key: str) -> httpx.Client:
    """The ONLY place this module builds a real HTTP client (Bearer auth).
    `httpx` is a hard dependency (see requirements.txt) so this import is at
    module top level, not lazy — same as the Lambda adapter. Unused by tests,
    which inject a duck-typed fake client instead."""
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=DEFAULT_TIMEOUT_S,
    )


def _runpod_factory(**cfg: Any) -> RunpodBurstProvider:
    """Build a RunpodBurstProvider from credential kwargs.

    `cfg` is the parsed tenant secret (see burst_accounts._creds_from_secret)
    plus optional account config. The account row stores the SECRET NAME
    only; the value arrives through crud.secret_cud/Vault at probe/provision
    time. Credential key: `api_key` (the EC2-style `provider_key`/`key`
    spellings are accepted too, like the other adapters). No env var, no
    settings read of the key, no log of it. Optional overrides: `region`,
    `cloud_instance` (community/spot cloud ONLY if the account config says
    so; default SECURE), `gpu_type`, `hardware`, `max_lifetime_min` (from the
    account's `limits`), `max_pods_per_call`.
    """
    api_key = cfg.get("api_key") or cfg.get("provider_key") or cfg.get("key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(
            "runpod burst account is missing its api_key: set the `api_key` "
            "secret on the account — RunPod calls are Bearer-authenticated "
            "and MOD will not call RunPod without one"
        )
    client = _build_client(api_key)
    return RunpodBurstProvider(
        client,
        region=cfg.get("region", DEFAULT_REGION),
        cloud_instance=cfg.get("cloud_instance"),
        gpu_type=cfg.get("gpu_type"),
        hardware=cfg.get("hardware"),
        max_lifetime_min=int(
            cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)
        ),
        max_pods_per_call=(
            int(cfg["max_pods_per_call"])
            if cfg.get("max_pods_per_call") is not None else None
        ),
        redact_values=(api_key,),
    )


register_provider("runpod", _runpod_factory)
