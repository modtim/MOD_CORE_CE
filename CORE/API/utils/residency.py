"""GY.V3a — data-residency pin + gate for tenant storage.

Partially closes GSEC R-089 / R-181 (federal civilian ICD 503 US-only,
CJIS, ITAR/EAR US residency). A platform setting
(``residency.region_pin``: "us" | "eu" | "" = off) declares where tenant
data may live. Provider region codes are mapped to residency zones in ONE
small table (``REGION_ZONE_MAP``); anything not in the table maps to
"unknown". When the pin is set:

- ``crud/storage_connection_cud.py`` refuses to create/update a
  StorageConnection whose region/endpoint maps outside the pin (or to
  unknown), writes an audit row and raises ``ResidencyPinError``
  (the router renders it as a 403).
- ``utils/storage.py::resolve_backend`` FAILS CLOSED — it raises
  ``ResidencyPinError`` instead of silently serving a store outside the
  pin or falling back to another store.

Pin off (default) means byte-identical behaviour to pre-GY.V3a. No
column is added: the zone is computed from the EXISTING ``region`` and
``endpoint`` fields of ``StorageConnection``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RESIDENCY_ZONES = ("us", "eu")

PIN_SETTING_KEY = "residency.region_pin"

# Provider region code -> residency zone. Unknown code -> "unknown"
# (checked by the caller, never mapped here).
REGION_ZONE_MAP: Dict[str, str] = {
    # AWS — US
    "us-east-1": "us", "us-east-2": "us",
    "us-west-1": "us", "us-west-2": "us",
    "us-gov-east-1": "us", "us-gov-west-1": "us",
    "us-iso-east-1": "us", "us-iso-west-1": "us",
    # GCP — US
    "us-central1": "us", "us-east1": "us", "us-east4": "us",
    "us-west1": "us", "us-west2": "us", "us-west3": "us", "us-west4": "us",
    # Azure — US (incl. Canada, which stays on the US side for ICD 503)
    "eastus": "us", "eastus2": "us", "westus": "us", "westus2": "us",
    "westus3": "us", "centralus": "us", "northcentralus": "us",
    "southcentralus": "us", "canadacentral": "us", "canadaeast": "us",
    # AWS — EU
    "eu-west-1": "eu", "eu-west-2": "eu", "eu-west-3": "eu",
    "eu-central-1": "eu", "eu-central-2": "eu",
    "eu-north-1": "eu", "eu-south-1": "eu", "eu-south-2": "eu",
    "eu-iso-west-1": "eu", "eu-iso-central-1": "eu",
    # GCP — EU
    "europe-west1": "eu", "europe-west2": "eu", "europe-west3": "eu",
    "europe-west4": "eu", "europe-north1": "eu", "europe-south1": "eu",
    # Azure — EU
    "northeurope": "eu", "westeurope": "eu",
    "uksouth": "eu", "ukwest": "eu",
    "francecentral": "eu", "francesouth": "eu",
    "germanycentral": "eu", "germanynorthwest": "eu",
    "switzerlandnorth": "eu", "switzerlandwest": "eu",
}


class ResidencyPinError(Exception):
    """A storage operation would place or serve data outside the
    platform data-residency pin. Fail closed: raise, never fall back
    to another store.

    Carries curated fields (``pin`` / ``zone`` / ``region`` — metadata
    only, never endpoints or credentials) so routers can render a safe
    4xx detail without interpolating raw exception text (api-lint R6)."""

    def __init__(self, message: str, *, pin: str = "", zone: str = "",
                 region: str = "") -> None:
        super().__init__(message)
        self.pin = pin
        self.zone = zone
        self.region = region


def _candidate_codes(region: Optional[str],
                     endpoint: Optional[str]) -> List[str]:
    """Region-code candidates, best first: the explicit region field,
    then tokens found in the endpoint host (AWS
    ``s3.eu-west-1.amazonaws.com``, GCP
    ``us-central1-storage.googleapis.com``, Azure
    ``acct.blob.westeurope.core.windows.net``)."""
    cands: List[str] = []
    r = (region or "").strip().lower()
    if r:
        cands.append(r)
    ep = (endpoint or "").strip().lower()
    if "://" in ep:
        ep = ep.split("://", 1)[1]
    host = ep.split("/", 1)[0]
    for part in host.split("."):
        part = part.strip()
        if not part:
            continue
        cands.append(part)
        if part.endswith("-storage"):  # GCP regional host suffix
            cands.append(part[: -len("-storage")])
        for tok in part.split("-"):
            if tok:
                cands.append(tok)
    return cands


def region_to_zone(region: Optional[str] = None,
                   endpoint: Optional[str] = None) -> str:
    """Map a provider region code (and/or endpoint host) to a residency
    zone: "us", "eu", or "unknown" when no candidate is in the table."""
    for cand in _candidate_codes(region, endpoint):
        zone = REGION_ZONE_MAP.get(cand)
        if zone is not None:
            return zone
    return "unknown"


def get_residency_pin(db: Any = None) -> str:
    """The platform data-residency pin: "us" / "eu", or "" = off."""
    from services import platform_settings as psr

    raw = psr.get_setting(PIN_SETTING_KEY, db=db)
    pin = str(raw or "").strip().lower()
    return pin if pin in RESIDENCY_ZONES else ""


def residency_violation(db: Any = None, region: Optional[str] = None,
                        endpoint: Optional[str] = None) -> Optional[Dict[str, str]]:
    """The pin violation for a region/endpoint pair, or None.

    None when the pin is off, or when the zone is inside the pin. The
    violation dict carries metadata only (pin / zone / region — never
    endpoints or credentials)."""
    pin = get_residency_pin(db)
    if not pin:
        return None
    zone = region_to_zone(region, endpoint)
    if zone == pin:
        return None
    return {"pin": pin, "zone": zone, "region": (region or "").strip()}


def _violation_message(viol: Dict[str, str]) -> str:
    return (
        f"Region '{viol['region'] or '<empty>'}' maps to residency zone "
        f"'{viol['zone']}', outside the platform data-residency pin "
        f"'{viol['pin']}'. Storage must stay inside the pinned region."
    )


def enforce_for_create_update(db: Any, tenant_id: int, user: Any,
                              region: Optional[str],
                              endpoint: Optional[str],
                              name: str = "") -> None:
    """Create/update gate (GY.V3a). Writes an audit row and raises
    ``ResidencyPinError`` when the pair maps outside the pin or to
    unknown; no-op when the pin is off or the zone is inside it.
    Callers must invoke this BEFORE any DB mutation."""
    viol = residency_violation(db, region, endpoint)
    if viol is None:
        return
    metadata = {
        "residency_pin_refused": True,
        "residency_pin": viol["pin"],
        "zone": viol["zone"],
        "region": viol["region"],
        "connection_name": name,
        "tenant_id": tenant_id,
    }
    try:
        import audit
        import models
        audit.log_security_event(
            db,
            models.AuditAction.SECURITY_CONFIG_ERROR,
            user_id=getattr(user, "id", None),
            tenant_id=tenant_id,
            metadata=metadata,
        )
    except Exception:  # noqa: BLE001 — audit failure must not mask the refusal
        logger.exception("residency: failed to write refusal audit row "
                         "(tenant=%s pin=%s)", tenant_id, viol["pin"])
    raise ResidencyPinError(
        _violation_message(viol),
        pin=viol["pin"], zone=viol["zone"], region=viol["region"],
    )


# ---------------------------------------------------------------------------
# GY.V3b — worker side of the data-residency pin
# ---------------------------------------------------------------------------

# Opt-in residency label: a worker's hardware tags (operator-set via the
# Go worker's WORKER_HARDWARE_TAGS, stored on
# WorkerLicense.hardware_specs['hardware_tags']) may carry exactly one
# residency tag — 'region:us' or 'region:eu'. No tag (or any other tag
# form) => zone "unknown", which the claim gate treats as OUTSIDE the pin
# (fail closed).
REGION_TAG_PREFIX = "region:"


def zone_from_tags(tags: Optional[List[str]] = None) -> str:
    """Residency zone from a worker's hardware tag list.

    'region:us' -> 'us', 'region:eu' -> 'eu', anything else -> 'unknown'
    (a worker with unknown location counts as outside the pin)."""
    for tag in tags or []:
        t = str(tag).strip().lower()
        if t.startswith(REGION_TAG_PREFIX):
            zone = t[len(REGION_TAG_PREFIX):].strip()
            if zone in RESIDENCY_ZONES:
                return zone
    return "unknown"


def zone_from_specs(specs: Optional[Dict[str, Any]] = None) -> str:
    """Residency zone from a WorkerLicense.hardware_specs dict (the
    per-heartbeat hardware block: {'hardware_tags': [...], ...})."""
    if not isinstance(specs, dict):
        return "unknown"
    return zone_from_tags(specs.get("hardware_tags"))


def worker_residency_zone(worker: Any) -> str:
    """Residency zone of a worker row (WorkerLicense-like: anything with a
    hardware_specs attribute). Computed — no column, no DB hit."""
    return zone_from_specs(getattr(worker, "hardware_specs", None))
