"""
CRUD helpers for AcceleratorProfile and AcceleratorBenchmarkStrategy.

Called from the heartbeat handler to upsert per-device rows when a worker
reports its `accelerators` list, and from the benchmark dispatcher to
resolve strategies + track results.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import AcceleratorProfile, AcceleratorBenchmarkStrategy, HardwareProfile

logger = logging.getLogger(__name__)


def is_placeholder_accelerator_name(name: Optional[str]) -> bool:
    """True when `name` is a worker enrollment placeholder or empty.

    The Go worker's tag/operator fallback (WORKER_HARDWARE_TAGS /
    WORKER_ACCELERATOR_DEVICES) invents names like
    ``"cuda (from tag, details pending benchmark)"`` when no detection tool
    (nvidia-smi/rocm-smi/xpu-smi) is available. These must never survive a
    completed benchmark and must never overwrite a real device name.
    """
    if not name:
        return True
    return "details pending benchmark" in name.lower()


def clean_accelerator_name(
    existing: Optional[str],
    kind: str,
    index: int,
    incoming: Optional[str] = None,
) -> str:
    """Best available non-placeholder name for an accelerator.

    Preference order: a real incoming name > a real existing name > a clean
    ``"<KIND> #<index>"`` fallback (e.g. "CUDA #0"). Never returns a
    "... details pending benchmark" placeholder.
    """
    if incoming and not is_placeholder_accelerator_name(incoming):
        return incoming
    if existing and not is_placeholder_accelerator_name(existing):
        return existing
    return f"{(kind or 'accel').upper()} #{index}"


def upsert_accelerators(
    db: Session,
    hardware_profile_id: int,
    accelerators: List[Dict[str, Any]],
) -> List[AcceleratorProfile]:
    """
    Upsert AcceleratorProfile rows from the worker's heartbeat payload.

    For each device in the list, either update an existing row (matched by
    hardware_profile_id + kind + index) or create a new one. Returns the
    full list of rows for this profile — including any devices that were
    previously stored but not in the current heartbeat (they keep their
    last-known state).

    Side effect: if any row's incoming driver_version differs from its
    baseline_driver_version (the value snapshotted at last calibration),
    the parent HardwareProfile is flipped to calibration_status='stale'
    so the staleness evaluator picks it up on the next sweep. Profiles
    with no baseline yet (never calibrated) are not flagged — they're
    'uncalibrated' or in flight.
    """
    if not accelerators:
        return []

    existing = {
        (p.kind, p.index): p
        for p in db.query(AcceleratorProfile).filter(
            AcceleratorProfile.hardware_profile_id == hardware_profile_id
        ).all()
    }

    upserted = []
    driver_drift_detected = False
    for dev in accelerators:
        kind = (dev.get("kind") or "").strip().lower()
        index = int(dev.get("index", 0))
        if not kind:
            continue

        key = (kind, index)
        row = existing.get(key)
        incoming_driver = (dev.get("driver_version") or "").strip() or None
        if row:
            # Name resolution — never let the heartbeat's tag placeholder
            # overwrite a real/benchmarked name. Accept an incoming name only
            # when it's a genuine device name, or when nothing is stored yet.
            incoming_name = dev.get("name")
            if incoming_name and not is_placeholder_accelerator_name(incoming_name):
                row.name = incoming_name
            elif not row.name:
                row.name = incoming_name
            # else: keep the existing name (real name wins over a placeholder;
            # a pending placeholder is left untouched until a benchmark runs).
            row.uuid = dev.get("uuid") or row.uuid
            row.vendor_id = dev.get("vendor_id") or row.vendor_id
            row.device_id = dev.get("device_id") or row.device_id
            row.pci_bus_id = dev.get("pci_bus_id") or row.pci_bus_id
            if incoming_driver and row.baseline_driver_version and incoming_driver != row.baseline_driver_version:
                driver_drift_detected = True
                logger.info(
                    "driver drift on hardware_profile=%s device=%s/%s: baseline=%s -> incoming=%s",
                    hardware_profile_id, kind, index, row.baseline_driver_version, incoming_driver,
                )
            row.driver_version = incoming_driver or row.driver_version
            if dev.get("vram_gb"):
                row.vram_gb = float(dev["vram_gb"])
        else:
            row = AcceleratorProfile(
                hardware_profile_id=hardware_profile_id,
                kind=kind,
                index=index,
                name=dev.get("name"),
                uuid=dev.get("uuid"),
                vendor_id=dev.get("vendor_id"),
                device_id=dev.get("device_id"),
                pci_bus_id=dev.get("pci_bus_id"),
                driver_version=incoming_driver,
                vram_gb=float(dev["vram_gb"]) if dev.get("vram_gb") else None,
                benchmark_status="pending",
            )
            db.add(row)
        upserted.append(row)

    db.flush()

    if driver_drift_detected:
        _mark_hardware_profile_stale(db, hardware_profile_id, reason="driver_version drift")

    return upserted


def _mark_hardware_profile_stale(db: Session, hardware_profile_id: int, reason: str = "") -> bool:
    """Flip a HardwareProfile's calibration_status to 'stale'.

    No-ops on profiles already 'stale' or 'uncalibrated' (no point flipping
    something that's never been calibrated to begin with). Returns True
    when a transition actually happened.
    """
    profile = db.query(HardwareProfile).filter(HardwareProfile.id == hardware_profile_id).first()
    if not profile:
        return False
    if profile.calibration_status in ("stale", "uncalibrated"):
        return False
    profile.calibration_status = "stale"
    db.flush()
    logger.info(
        "hardware_profile=%s calibration_status=stale (reason=%s)",
        hardware_profile_id, reason or "unspecified",
    )
    return True


def resolve_strategy(
    db: Session,
    kind: str,
    vendor_id: Optional[str] = None,
    device_id: Optional[str] = None,
) -> Optional[AcceleratorBenchmarkStrategy]:
    """
    Find the best-matching benchmark strategy for a device.

    Priority (most specific wins):
      1. Exact match on kind + vendor_id + device_id_pattern
      2. Kind + vendor_id (device_id_pattern IS NULL)
      3. Kind only (vendor_id IS NULL, device_id_pattern IS NULL)
      4. None found → caller should trigger LLM generation.
    """
    kind = (kind or "").strip().lower()

    if vendor_id and device_id:
        exact = db.query(AcceleratorBenchmarkStrategy).filter(
            AcceleratorBenchmarkStrategy.kind == kind,
            AcceleratorBenchmarkStrategy.vendor_id == vendor_id,
            AcceleratorBenchmarkStrategy.device_id_pattern == device_id,
        ).first()
        if exact:
            return exact

    if vendor_id:
        vendor_match = db.query(AcceleratorBenchmarkStrategy).filter(
            AcceleratorBenchmarkStrategy.kind == kind,
            AcceleratorBenchmarkStrategy.vendor_id == vendor_id,
            AcceleratorBenchmarkStrategy.device_id_pattern.is_(None),
        ).first()
        if vendor_match:
            return vendor_match

    kind_match = db.query(AcceleratorBenchmarkStrategy).filter(
        AcceleratorBenchmarkStrategy.kind == kind,
        AcceleratorBenchmarkStrategy.vendor_id.is_(None),
        AcceleratorBenchmarkStrategy.device_id_pattern.is_(None),
    ).first()
    return kind_match


def get_pending_accelerators(db: Session, hardware_profile_id: int) -> List[AcceleratorProfile]:
    """Return accelerator rows that still need benchmarking."""
    return db.query(AcceleratorProfile).filter(
        AcceleratorProfile.hardware_profile_id == hardware_profile_id,
        AcceleratorProfile.benchmark_status == "pending",
    ).all()


def all_benchmarks_complete(db: Session, hardware_profile_id: int) -> bool:
    """True when every AcceleratorProfile for this hardware has finished."""
    pending = db.query(AcceleratorProfile).filter(
        AcceleratorProfile.hardware_profile_id == hardware_profile_id,
        AcceleratorProfile.benchmark_status.in_(["pending", "running"]),
    ).count()
    return pending == 0
