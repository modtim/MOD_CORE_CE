"""GH.7d — Lambda Cloud (lambdalabs.com) burst provider (launch-completion-
megaplan group H, decomp/gh-burst.md).

Lambda Cloud has **no apache-libcloud driver**, so unlike the EC2 and Azure
adapters (`services/burst_provider_ec2.py`, `services/burst_provider_azure.py`,
both built on the generic `LibcloudBurstProvider`), this module implements the
`BurstProvider` protocol directly over Lambda's public REST API using
`httpx` (already a hard dependency — see `requirements.txt` — so, unlike the
libcloud import, this module's HTTP client import is NOT lazy).

Endpoint / field assumptions (FROM MEMORY of Lambda's public API docs, NOT
verified against a live account or a fetched OpenAPI spec — every one of
these MUST be checked against a real account in the live-account test before
this adapter is trusted with real money):

  * Base URL ``https://cloud.lambdalabs.com/api/v1``; auth
    ``Authorization: Bearer <api_key>`` (as directed for this task — Lambda's
    own docs, from memory, describe HTTP Basic auth with the API key as the
    username and an empty password; if the live test shows Basic auth is
    actually required, swap the header construction in `_build_client` only).
  * ``GET /instance-types`` -> ``{"data": {"<type-name>": {"instance_type":
    {"name", "price_cents_per_hour", ...}, "regions_with_capacity_available":
    [{"name": "<region>"}, ...]}}}``.
  * ``GET /instances`` -> ``{"data": [{"id", "name", "status", "ip",
    "private_ip", "region": {"name"}, "instance_type": {"name"}, ...}]}``.
    `status` is assumed to take values like "booting", "active",
    "unhealthy", "rebooting", "terminating", "terminated".
  * ``POST /instance-operations/launch`` <- ``{"region_name",
    "instance_type_name", "ssh_key_names", "quantity", "name", "user_data"}``
    -> ``{"data": {"instance_ids": [...]}}``. `user_data` is listed in this
    task's endpoint schema; whether Lambda's launch endpoint actually accepts
    and executes a cloud-init/user-data payload is NOT confirmed from memory
    — see the SELF-DESTRUCT WARNING below. Nothing here invents a field that
    is not in that schema.
  * ``POST /instance-operations/terminate`` <- ``{"instance_ids": [...]}``.
  * Capacity-exhausted launches return HTTP 400 with a body shaped like
    ``{"error": {"code": "...", "message": "..."}}`` where the code or
    message mentions "capacity" (e.g. an `insufficient-capacity`-style code).
    The exact code string is a guess from memory — `_is_capacity_error`
    matches on the substring "capacity" in either field specifically so it
    survives the exact spelling being wrong; the live test must confirm at
    least one real capacity-rejection body shape.

*** SELF-DESTRUCT WARNING ***
Lambda instances have exactly two billed states as far as this module can
tell from memory: running and terminated. There is no AWS-style
"InstanceInitiatedShutdownBehavior=terminate" and no Azure-style "deallocate
stops the meter" state. That means an in-guest `shutdown -h` (which is all
`arm_self_destruct` can ever do) does NOT stop Lambda billing even if the
`user_data` field is honored — the instance's control-plane record stays
"active" until something calls `POST /instance-operations/terminate` on it.
This adapter still splices the shutdown line into `user_data` (in case a
future/undocumented Lambda image does act on it, and so an orphan that loses
network to MOD at least stops doing useful/harmful work on its GPU), but
callers MUST NOT rely on it for cost control. The `services/burst_reconciler.py`
MOD-tag sweep (`provider.list_tagged` + `provider.teardown`) is the ONLY real
reaper for orphaned Lambda instances — if the reconciler cadence lapses for
this provider, a Lambda orphan bills indefinitely, unlike an EC2/Azure orphan
which at least has a chance of self-terminating past `max_lifetime_min`.

Tags: Lambda's public API has no tagging concept at all. MOD instances are
marked by a NAME PREFIX that embeds `mod_tag_value()` (see
`services.burst_provider`), sanitised to a conservative name charset
(``[A-Za-z0-9_-]``, since Lambda's exact name validation rules are not
confirmed from memory) and length (63 chars, also a conservative guess —
verify live). `list_tagged` decodes the marker back into a `tags` dict
carrying only `MOD_TAG_KEY` and (best-effort, from the sanitised name suffix)
`mod-burst-instance` — nothing else the caller stamped survives a round trip
through Lambda, because there is nowhere else to put it. `teardown` refuses
(raises `PermissionError`, touching nothing) any instance id whose current
name does not carry the marker — the same non-negotiable safety guard the
EC2 and Azure adapters have.

Capacity: `provision()` raises `BurstCapacityUnavailable` (defined here —
`services/burst_provider.py` has no such exception, and Lambda is expected
to run out of capacity far more often than AWS/Azure/GCP) when the launch
call is rejected specifically for lack of capacity. Callers (the GH.5
scale-up launcher, the reconciler, anything driving `provision()`) SHOULD
catch this distinctly from a bare `Exception`/`RuntimeError` and treat it as
transient, supply-side unavailability: do not mark the tenant's burst
account invalid, do not disable burst, just skip/retry this attempt (and
prefer a different provider/region if one is configured) rather than
surfacing it as a hard provisioning failure.
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Optional

import httpx

from services.burst_provider import (
    MOD_TAG_KEY,
    has_mod_tag,
    mod_tag_value,
    register_provider,
)
from services.burst_provider_ec2 import arm_self_destruct

BASE_URL = "https://cloud.lambdalabs.com/api/v1"
DEFAULT_REGION = "us-east-1"
# Same hard ceiling concept as the EC2/Azure adapters (see module docstring —
# on Lambda this is belt-and-braces at best, NOT a substitute for the
# reconciler's terminate sweep).
DEFAULT_MAX_LIFETIME_MIN = 720
# Set on every single outbound call (see module docstring / task contract).
DEFAULT_TIMEOUT_S = 15.0

# Matches burst_reconciler.BURST_INSTANCE_TAG_KEY. Duplicated as a literal
# (not imported) deliberately: Lambda has no real tag store, so this key is
# only ever used here to pick a value OUT of the `tags` dict a caller passes
# to `provision()` for embedding in the sanitised instance name, and to
# reconstruct it (best-effort) from that name later. It must stay in lock
# step with burst_reconciler's constant of the same name.
_INSTANCE_LABEL_TAG_KEY = "mod-burst-instance"

_NAME_PREFIX = f"{mod_tag_value()}-"
_MAX_NAME_LEN = 63  # conservative guess — verify Lambda's real limit live
_NAME_CHARSET_RE = re.compile(r"[^A-Za-z0-9_-]")


class BurstCapacityUnavailable(RuntimeError):
    """Lambda rejected a launch specifically for lack of capacity.

    Distinct from any other provisioning failure on purpose — see the
    "Capacity" section of the module docstring for how a caller should treat
    this (transient/retryable, not an account-invalidating error).
    """


# ============================================================================
# GPU size map — MOD VRAM need -> Lambda instance type name
# ============================================================================
# `price` is an approximate list $/hour figure (APPROX — Lambda's own price
# list changes and varies by region availability; this is for ranking
# "cheapest fitting" ONLY, never for billing). Unlike the EC2/Azure maps this
# does NOT carry vcpu/ram_gb: those per-type specs are not reliably known
# from memory for Lambda and are deliberately left out rather than guessed —
# pull them from a live `GET /instance-types` response if they are ever
# needed.
GPU_SIZE_MAP: dict[str, dict[str, Any]] = {
    "gpu_1x_a10":         {"gpu": "A10",   "count": 1, "vram_gb": 24, "price": 0.75},
    "gpu_1x_a100_sxm4":   {"gpu": "A100",  "count": 1, "vram_gb": 40, "price": 1.29},
    "gpu_1x_h100_pcie":   {"gpu": "H100",  "count": 1, "vram_gb": 80, "price": 2.49},
    "gpu_8x_h100_sxm5":   {"gpu": "H100",  "count": 8, "vram_gb": 80, "price": 23.92},
    "gpu_1x_gh200":       {"gpu": "GH200", "count": 1, "vram_gb": 96, "price": 1.49},
}


def total_vram_gb(instance_type: str) -> int:
    """Total VRAM (count * per-GPU) for a mapped Lambda type; 0 if unknown."""
    spec = GPU_SIZE_MAP.get(instance_type)
    return spec["count"] * spec["vram_gb"] if spec else 0


def pick_instance_type(
    min_vram_gb: int | float,
    allowed_instance_types: list[str] | None = None,
) -> str:
    """Pick the CHEAPEST mapped Lambda instance type whose total VRAM is at
    least `min_vram_gb`. Same semantics as the EC2/Azure `pick_instance_type`:
    `allowed_instance_types` (the account's `limits.allowed_instance_types`)
    restricts the choice when provided; raises `ValueError` with a clear
    message when the need is non-positive or no mapped type fits.
    """
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
            f"no Lambda GPU instance in the size map provides >= "
            f"{min_vram_gb} GB VRAM{where}; mapped types: {sorted(GPU_SIZE_MAP)}"
        )
    candidates.sort(key=lambda p: (p[0], p[1]))
    return candidates[0][1]


# ============================================================================
# Name-marker helpers (Lambda's stand-in for tags)
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
    """Best-effort reconstruction of a tags dict from a marked name. Only
    ever produces `MOD_TAG_KEY` and (if a suffix survives) the instance
    label — see the module docstring's "Tags" section for why nothing else
    can round-trip through Lambda."""
    name = str(name or "")
    if not _has_marker(name):
        return {}
    tags: dict[str, Any] = {MOD_TAG_KEY: mod_tag_value()}
    suffix = name[len(_NAME_PREFIX):]
    if suffix:
        tags[_INSTANCE_LABEL_TAG_KEY] = suffix
    return tags


# ============================================================================
# LambdaBurstProvider
# ============================================================================

class LambdaBurstProvider:
    """BurstProvider for Lambda Cloud, implemented directly over Lambda's
    public REST API (no libcloud driver exists for Lambda).

    `client` is any object exposing `.get(path, params=None, timeout=...)`
    and `.post(path, json=None, timeout=...)` returning a response with
    `.status_code` and `.json()` — in production an `httpx.Client` built by
    `_lambda_factory` (base_url + Bearer header baked in); in tests a
    hand-written duck-typed stand-in, exactly like the EC2/Azure adapters'
    `FakeEc2Driver`/`FakeAzureDriver`. This class never touches the network
    directly, only through `client`.
    """

    def __init__(
        self,
        client: Any,
        *,
        region: str = DEFAULT_REGION,
        ssh_key_names: list[str] | None = None,
        max_lifetime_min: int = DEFAULT_MAX_LIFETIME_MIN,
        redact_values: tuple[str, ...] | list[str] = (),
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self._client = client
        self.region = region
        self.ssh_key_names = list(ssh_key_names or [])
        self.max_lifetime_min = int(max_lifetime_min)
        self._redact_values = [v for v in redact_values if v]
        self._timeout = timeout

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

    @staticmethod
    def _is_capacity_error(status_code: int, body: Any) -> bool:
        if status_code != 400 or not isinstance(body, dict):
            return False
        err = body.get("error") or {}
        code = str(err.get("code") or "").lower()
        msg = str(err.get("message") or "").lower()
        return "capacity" in code or "capacity" in msg

    def _check(self, resp: Any, *, action: str) -> Any:
        """Raise on any non-2xx response; return the parsed JSON body on
        success. Every raised message is redacted before it can be
        logged/persisted."""
        try:
            body = resp.json()
        except Exception:
            body = None
        status = int(getattr(resp, "status_code", 0) or 0)
        if 200 <= status < 300:
            return body
        if action == "launch" and self._is_capacity_error(status, body):
            err = (body or {}).get("error") or {}
            raise BurstCapacityUnavailable(self._redact(
                f"lambda cloud has no available capacity for this launch "
                f"(HTTP {status}, code={err.get('code')!r}): "
                f"{err.get('message') or ''}"
            ))
        detail = body if body is not None else getattr(resp, "text", "")
        raise RuntimeError(self._redact(
            f"lambda cloud API {action} failed: HTTP {status}: {detail}"
        ))

    def _list_instances(self) -> list[dict[str, Any]]:
        resp = self._get("/instances")
        body = self._check(resp, action="list-instances") or {}
        items = body.get("data")
        return items if isinstance(items, list) else []

    def _instances_by_id(self) -> dict[str, dict[str, Any]]:
        return {str(item.get("id")): item for item in self._list_instances()}

    def _node_dict(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(item.get("id") or ""),
            "state": str(item.get("status") or "").lower(),
            "public_ip": item.get("ip"),
            "private_ip": item.get("private_ip"),
            "tags": _tags_from_name(item.get("name")),
        }

    # -- BurstProvider protocol ---------------------------------------------

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        # No network call: static approx-price map only, same spirit as
        # FakeBurstProvider. `available` is unknown without a live capacity
        # check (see `probe()` / `GET /instance-types`), so it is left None
        # rather than guessed.
        spec = GPU_SIZE_MAP.get(instance_type)
        price = float(spec["price"]) if spec else None
        return {
            "provider": "lambda",
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
        if not has_mod_tag(tags):
            raise ValueError(
                "provision() requires the MOD tag so teardown/list_tagged "
                "can find these nodes and nothing else"
            )
        label = tags.get(_INSTANCE_LABEL_TAG_KEY)
        name = _build_marked_name(label)
        # See the module docstring's SELF-DESTRUCT WARNING: this line is
        # still spliced in per the task's endpoint schema, but it is NOT
        # known to stop Lambda billing even if honored.
        armed = arm_self_destruct(user_data, self.max_lifetime_min)
        payload = {
            "region_name": self.region,
            "instance_type_name": instance_type,
            "ssh_key_names": self.ssh_key_names,
            "quantity": n,
            "name": name,
            "user_data": armed,
        }
        resp = self._post("/instance-operations/launch", payload)
        body = self._check(resp, action="launch") or {}
        ids = (body.get("data") or {}).get("instance_ids") or []
        return [
            {
                "id": str(i),
                "state": "booting",
                "public_ip": None,
                "private_ip": None,
                "tags": dict(tags),
            }
            for i in ids
        ]

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        by_id = self._instances_by_id()
        return [self._node_dict(by_id[i]) for i in node_ids if i in by_id]

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD (name-marked) nodes ONLY. A node whose current name
        does not carry the marker is refused — nothing in `node_ids` is
        terminated if even one of them fails the check, mirroring the
        EC2/Azure adapters' "refuse, touch nothing" guard."""
        by_id = self._instances_by_id()
        to_terminate: list[str] = []
        for i in node_ids:
            item = by_id.get(i)
            if item is None:
                continue  # unknown id: nothing to refuse or destroy
            if not _has_marker(item.get("name")):
                raise PermissionError(
                    f"refusing to tear down lambda instance {i}: "
                    f"name {item.get('name')!r} carries no MOD marker"
                )
            to_terminate.append(i)
        if not to_terminate:
            return []
        resp = self._post("/instance-operations/terminate",
                          {"instance_ids": to_terminate})
        self._check(resp, action="terminate")
        out = []
        for i in to_terminate:
            d = self._node_dict(by_id[i])
            d["state"] = "terminated"
            out.append(d)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in self._list_instances():
            if not _has_marker(item.get("name")):
                continue
            d = self._node_dict(item)
            ntags = d["tags"]
            if all(ntags.get(k) == v for k, v in tags.items()
                   if k != MOD_TAG_KEY):
                out.append(d)
        return out

    def probe(self) -> dict[str, Any]:
        """Read-only credential/capability probe: `GET /instance-types`
        (confirms the key is valid and the API is reachable) + `list_tagged`
        (via `GET /instances`). Returns ``{ok, region, visible_tagged,
        error}``, matching the EC2/Azure adapters' probe shape. On ANY
        exception `ok` is False and the API key is redacted from the error.
        """
        result: dict[str, Any] = {
            "ok": False,
            "region": self.region,
            "visible_tagged": None,
            "error": None,
        }
        try:
            resp = self._get("/instance-types")
            self._check(resp, action="list-instance-types")
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
    """The ONLY place this module builds a real HTTP client. `httpx` is a
    hard dependency (unlike libcloud for the EC2/Azure adapters) so this
    import is at module top level, not lazy."""
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=DEFAULT_TIMEOUT_S,
    )


def _lambda_factory(**cfg: Any) -> LambdaBurstProvider:
    """Build a LambdaBurstProvider from credential kwargs.

    `cfg` is the parsed tenant secret (see burst_accounts._creds_from_secret)
    plus optional account config. Credential key: `api_key` (or the EC2-style
    `provider_key`/`key` spellings, accepted for consistency with the other
    adapters' secret shape). Optional overrides: `region`, `ssh_key_names`
    (REQUIRED by Lambda's launch call — no default can be invented; an empty
    list here will make `provision()` fail at Lambda, not locally),
    `max_lifetime_min` (from the account's `limits`).
    """
    api_key = cfg.get("api_key") or cfg.get("provider_key") or cfg.get("key")
    region = cfg.get("region", DEFAULT_REGION)
    client = _build_client(api_key)
    return LambdaBurstProvider(
        client,
        region=region,
        ssh_key_names=cfg.get("ssh_key_names"),
        max_lifetime_min=int(cfg.get("max_lifetime_min", DEFAULT_MAX_LIFETIME_MIN)),
        redact_values=(api_key,),
    )


register_provider("lambda", _lambda_factory)
