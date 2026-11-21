"""GH.1 — burst provider abstraction: MOD stands up ephemeral tenant-owned
workers inside the CUSTOMER's own cloud account.

The interface (`BurstProvider`) is provider-agnostic; two implementations
ship now:

  * `FakeBurstProvider` — in-memory, deterministic. Used by tests and dev.
  * `LibcloudBurstProvider` — generic over an apache-libcloud `NodeDriver`
    (EC2, Azure ARM, GCE, ...). The libcloud import is LAZY (inside the
    class/factory) because the running API image does not have libcloud
    installed yet and a top-level import would crash API startup.

Safety rules (non-negotiable, per the internal design notes
decomp/gh-burst.md):

  * every MOD instance is created WITH the MOD tag (`mod-burst=<value>`);
  * `teardown` ONLY destroys nodes carrying that tag — a node without it is
    someone else's machine and is refused, never touched;
  * `list_tagged` is the reconciler's eye: it lists what MOD owns.

Nodes are returned as plain dicts: `{id, state, public_ip, private_ip, tags}`.
"""
from __future__ import annotations

import itertools
from typing import Any, Callable, Protocol, runtime_checkable

# The MOD ownership tag. Every instance MOD creates carries it; teardown and
# the reconciler only ever act on instances that carry it.
MOD_TAG_KEY = "mod-burst"


def mod_tag_value() -> str:
    """The value MOD writes under MOD_TAG_KEY. Fixed: this IS the ownership
    marker, it must not drift between the writer and the teardown guard."""
    return "mod-core"


def has_mod_tag(tags: dict[str, Any] | None) -> bool:
    """True if `tags` marks the node as MOD-owned."""
    return bool(tags) and tags.get(MOD_TAG_KEY) == mod_tag_value()


def _node_dict(node: Any) -> dict[str, Any]:
    """Coerce a libcloud `Node` (or anything with the same attributes) into
    the plain-dict shape this module returns. Missing attrs become None."""
    extra: dict[str, Any] = {}
    for attr in ("extra", "metadata"):
        val = getattr(node, attr, None)
        if isinstance(val, dict):
            extra.update(val)
    return {
        "id": str(getattr(node, "id", "") or ""),
        "state": str(getattr(node, "state", "") or ""),
        "public_ip": (getattr(node, "public_ip", None) or [None])[0]
        if isinstance(getattr(node, "public_ip", None), (list, tuple))
        else getattr(node, "public_ip", None),
        "private_ip": (getattr(node, "private_ip", None) or [None])[0]
        if isinstance(getattr(node, "private_ip", None), (list, tuple))
        else getattr(node, "private_ip", None),
        "tags": dict(extra),
    }


@runtime_checkable
class BurstProvider(Protocol):
    """What MOD needs from any cloud target for burst compute."""

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        """Estimate cost/capacity for `n` × `instance_type` in `region`.
        Returns a plain dict (provider-specific keys allowed; must at least
        say whether it can be done and an estimated cost if known)."""
        ...

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Create `n` instances. Every created node MUST carry `tags`
        (which must include the MOD tag)."""
        ...

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Current state of the given MOD node ids."""
        ...

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD nodes. MUST refuse any node without the MOD tag."""
        ...

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        """All nodes in the account matching `tags` (reconciler's eye)."""
        ...


# ============================================================================
# FakeBurstProvider — in-memory, deterministic
# ============================================================================

class FakeBurstProvider:
    """Deterministic in-memory provider for tests and dev.

    Node ids are sequential (`fake-1`, `fake-2`, ...). Provisioned nodes are
    immediately in state "running" with a predictable IP scheme, so tests can
    assert on them without any clock or randomness.
    """

    def __init__(self, region: str = "fake-1", price_per_hour: float = 0.5):
        self.region = region
        self.price_per_hour = price_per_hour
        self._counter = itertools.count(1)
        self._nodes: dict[str, dict[str, Any]] = {}

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        # Deterministic estimate: flat price_per_hour per instance.
        return {
            "provider": "fake",
            "instance_type": instance_type,
            "n": n,
            "region": region,
            "available": True,
            "estimated_cost_per_hour": round(self.price_per_hour * n, 6),
        }

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        out = []
        for _ in range(n):
            i = next(self._counter)
            node = {
                "id": f"fake-{i}",
                "state": "running",
                "public_ip": f"203.0.113.{i % 250}",
                "private_ip": f"10.10.0.{i % 250}",
                "tags": dict(tags),
                "instance_type": instance_type,
                "user_data": user_data,
            }
            self._nodes[node["id"]] = node
            out.append(node)
        return out

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        return [
            dict(self._nodes[i]) for i in node_ids if i in self._nodes
        ]

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        # The safety guard is in the SHARED helper: only MOD-tagged nodes die.
        out = []
        for i in node_ids:
            node = self._nodes.get(i)
            if node is None:
                continue
            if not has_mod_tag(node.get("tags")):
                raise PermissionError(
                    f"refusing to tear down node {i}: missing MOD tag"
                )
            node["state"] = "terminated"
            out.append(node)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        want = {k: v for k, v in tags.items() if k != MOD_TAG_KEY}
        out = []
        for node in self._nodes.values():
            ntags = node.get("tags") or {}
            if has_mod_tag(ntags) and all(ntags.get(k) == v for k, v in want.items()):
                out.append(node)
        return out


# ============================================================================
# LibcloudBurstProvider — generic over a libcloud NodeDriver
# ============================================================================

class LibcloudBurstProvider:
    """BurstProvider backed by an apache-libcloud `NodeDriver`.

    The driver is injected (built by a factory that does the lazy import +
    credential wiring), so this class itself never imports libcloud.

    libcloud drivers differ in how they accept tags at create time: EC2 takes
    `ex_metadata` (and `ex_tags` on newer versions), Azure takes `ex_tags`
    (as a list of dicts). We try `ex_tags` first, then fall back to
    `ex_metadata`, keeping the adapter deliberately small.
    """

    def __init__(
        self,
        driver: Any,
        instance_size_lookup: Callable[[str], str] | None = None,
        image_id: str = "mod-burst-image",
    ):
        self.driver = driver
        self.image_id = image_id
        # provider-specific gap: map MOD instance_type -> driver size id
        self._size = instance_size_lookup or (lambda it: it)
        self._counter = itertools.count(1)

    def quote(self, instance_type: str, n: int, region: str) -> dict[str, Any]:
        # Best-effort: ask the driver's size catalogue when it has one.
        size = None
        try:
            size = self._size(instance_type)
        except Exception:
            size = None
        price = None
        list_sizes = getattr(self.driver, "list_sizes", None)
        if callable(list_sizes):
            try:
                for s in list_sizes():
                    if getattr(s, "id", None) == size:
                        price = getattr(s, "price", None)
                        break
            except Exception:
                price = None
        return {
            "provider": type(self.driver).__name__,
            "instance_type": instance_type,
            "size": size,
            "n": n,
            "region": region,
            "price_per_hour": price,
            "estimated_cost_per_hour": (
                round(price * n, 6) if price is not None else None
            ),
        }

    def _create_node(
        self, name: str, size: str, user_data: str, tags: dict[str, Any]
    ) -> Any:
        kwargs: dict[str, Any] = {
            "name": name,
            "size": size,
            "image": self.image_id,
            "ex_userdata": user_data,
        }
        # Tags: try the kwarg each driver accepts. ex_tags first, then
        # ex_metadata — keep the adapter small, don't reverse-engineer
        # every driver.
        for kw in ("ex_tags", "ex_metadata"):
            try:
                return self.driver.create_node(**kwargs, **{kw: dict(tags)})
            except TypeError:
                continue
        raise TypeError(
            "driver %s rejects both ex_tags and ex_metadata; this provider "
            "cannot stamp the MOD tag and will not create untagged nodes"
            % type(self.driver).__name__
        )

    def provision(
        self, n: int, instance_type: str, user_data: str, tags: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not has_mod_tag(tags):
            raise ValueError(
                "provision() requires the MOD tag so teardown can find these "
                "nodes and nothing else"
            )
        size = self._size(instance_type)
        out = []
        for _ in range(n):
            i = next(self._counter)
            node = self._create_node(f"mod-burst-{i}", size, user_data, tags)
            out.append(_node_dict(node))
        return out

    def status(self, node_ids: list[str]) -> list[dict[str, Any]]:
        by_id = {str(nd.id): nd for nd in self.driver.list_nodes()}
        return [_node_dict(by_id[i]) for i in node_ids if i in by_id]

    def teardown(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Destroy MOD nodes ONLY. A node without the MOD tag is refused —
        this is the guard that keeps burst from ever destroying a customer's
        non-MOD machines, even if MOD's own bookkeeping is wrong."""
        by_id = {str(nd.id): nd for nd in self.driver.list_nodes()}
        out = []
        for i in node_ids:
            node = by_id.get(i)
            if node is None:
                continue
            if not has_mod_tag(_node_dict(node)["tags"]):
                raise PermissionError(
                    f"refusing to tear down node {i}: missing MOD tag"
                )
            self.driver.destroy_node(node)
            d = _node_dict(node)
            d["state"] = "terminated"
            out.append(d)
        return out

    def list_tagged(self, tags: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for node in self.driver.list_nodes():
            d = _node_dict(node)
            ntags = d["tags"]
            if ntags.get(MOD_TAG_KEY) != mod_tag_value():
                continue
            if all(ntags.get(k) == v for k, v in tags.items()
                   if k != MOD_TAG_KEY):
                out.append(d)
        return out


# ============================================================================
# Registry
# ============================================================================

_PROVIDERS: dict[str, Callable[..., Any]] = {}


def register_provider(name: str, factory: Callable[..., Any]) -> None:
    """Register a factory that builds a BurstProvider from keyword config."""
    _PROVIDERS[name] = factory


def get_provider(name: str, **cfg: Any) -> BurstProvider:
    """Build (or build on demand) the provider registered as `name`."""
    if name not in _PROVIDERS:
        raise KeyError(
            f"no burst provider registered as {name!r}; "
            f"known: {sorted(_PROVIDERS)}"
        )
    return _PROVIDERS[name](**cfg)


def _fake_factory(**cfg: Any) -> FakeBurstProvider:
    return FakeBurstProvider(**cfg)


def _libcloud_factory(
    driver_class: str = "libcloud.compute.providers.get_provider",
    provider_name: str = "aws",
    **cfg: Any,
) -> LibcloudBurstProvider:
    """Lazy factory: this is the ONLY place in the module that imports
    libcloud, so the API starts fine without it installed."""
    import libcloud.compute.providers as lp  # noqa: PLC0415 (deliberately lazy)

    driver = lp.get_provider(provider_name)(**cfg)
    return LibcloudBurstProvider(driver)


register_provider("fake", _fake_factory)
register_provider("libcloud", _libcloud_factory)

# GH.7a — register the first real cloud provider ("ec2"). Imported LAST (after
# every name it needs is defined) so the circular import resolves in either
# import order, and the ec2 module keeps its libcloud import lazy, so API
# startup without libcloud still boots. Plain `import ...` (side-effect form)
# is the circular-import-safe shape; importing it has no libcloud side effects.
import services.burst_provider_ec2  # noqa: E402,F401 (registration import)
import services.burst_provider_azure  # noqa: E402,F401 (registration import)
import services.burst_provider_lambda  # noqa: E402,F401 (registration import)
import services.burst_provider_gcp  # noqa: E402,F401 (registration import)
# GH.7e — register the fifth real cloud provider ("oci"). Built directly on
# the official `oci` SDK (no usable libcloud driver for OCI exists), so
# unlike ec2/azure/gcp it never touches libcloud at all — its own import is
# lazy over `oci` instead, same lazy-import safety as the others.
import services.burst_provider_oci  # noqa: E402,F401 (registration import)
import services.burst_provider_runpod  # noqa: E402,F401 (registration import)
