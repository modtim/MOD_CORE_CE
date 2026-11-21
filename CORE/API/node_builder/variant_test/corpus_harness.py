"""GO.5 — golden-corpus harness for accelerator-variant verification.

Builds (entry x accelerator) verification cells from
``corpus_manifest.yaml``, probes the fleet's real hardware first (step 0) so
no ``spec`` verdict is ever guessed, then executes each cell through the
EXISTING wizard build path (single-variant rebuild DAG + per-variant
sample-test + ``/wizard/variant-sample-test-result`` scoring). Results are
written by the existing dispatcher into
``node_variant_verification`` (+ ``node_definitions.variant_performance``),
and this module only: loads/validates the manifest, plans cells, enqueues
work (never blocks in-request), and sweeps sample-test result rows back.

CLI (dry run writes nothing):

    python -m node_builder.variant_test.corpus_harness --dry-run
        [--only 01,02,syn-embedding] [--accel cuda,rocm] [--allow-non-wizard-fallback]
        [--verification-only]

``--verification-only`` (GO.10.s2, off by default) is the verification pass for
nodes the variant fan-out has ALREADY converted (``NodeDefinition.gpu_variants``
populated): it plans one cell per existing (node, variant) pair — no conversion,
no build step, no new variant fan-out — and ``collect_results`` records
``NodeDefinition.variant_verification_status[variant]`` for them. Unconverted
nodes (no ``gpu_variants``) are skipped outright. The default plan is unchanged.

Any non-dry-run behaviour is gated by the platform setting
``node_build.corpus_harness_enabled`` (DB-backed, default off). No env vars.

``--allow-non-wizard-fallback`` (off by default) additionally lets REAL corpus
nodes with no BuildSpec linked via a NodeVariantVerification row resolve one
from their own ``CORE/DockerContainers/<node>`` build source, reusing
``backfill.build_non_wizard_spec`` (the variant-backfill sweep's non-wizard
resolver) instead of skipping the cell with "no BuildSpec for cell".
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "corpus_manifest.yaml")

ACCELERATORS = ("cuda", "rocm", "intel", "npu", "cpu")
VERDICTS = ("pass", "pass-tol", "incompat", "spec", "na")
# Step 0 re-labels `spec` cells to this when the probed hardware lacks the
# facts needed to confirm the verdict — no guessing.
UNKNOWN_HW = "unknown-hw"
# The two spec entries excluded until their containers are registered
# (LEAD DECISIONS item 1, `pending-registration`).
EXCLUDED_PENDING_REGISTRATION = ("06", "22")
SYNTHETIC_PREFIX = "synthetic:"

# Hardware facts step 0 records per accelerator. `needs` lists the fact keys
# that must be KNOWN before a `spec` cell may keep its speculative label; a
# missing fact re-labels the cell `unknown-hw` instead of guessing.
#   rocm  — WSL (/dev/dxg) vs native (/dev/dri) changes what can be verified.
#   intel — GPU device model decides which EP/runtime is plausible.
#   npu   — NPU device model likewise.
_PROBE_FACTS_REQUIRED: Dict[str, List[str]] = {
    "rocm": ["rocm_version", "dri_device"],
    "intel": ["device_model"],
    "npu": ["device_model"],
    "cuda": [],
    "cpu": [],
}


class ManifestError(ValueError):
    """Raised when the corpus manifest fails validation."""


def _manifest_path() -> str:
    """Manifest location; tests may point elsewhere by copying the file —
    this stays importable without an env knob."""
    return _MANIFEST_PATH


def load_manifest(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the corpus manifest YAML. Returns {"entries": [...]}."""
    import yaml
    with open(path or _manifest_path(), "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ManifestError("corpus manifest must be a mapping with an 'entries' list")
    return data


def validate_manifest(data: Dict[str, Any]) -> None:
    """Validate manifest shape + the LEAD DECISIONS invariants. Raises
    ManifestError on the first problem so a corrupt manifest never plans."""
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("manifest has no entries")
    seen_ids: set = set()
    for i, e in enumerate(entries):
        ctx = f"entry #{i}"
        if not isinstance(e, dict):
            raise ManifestError(f"{ctx}: not a mapping")
        eid = str(e.get("id") or "")
        if not eid:
            raise ManifestError(f"{ctx}: missing id")
        ctx = f"entry {eid}"
        if eid in seen_ids:
            raise ManifestError(f"{ctx}: duplicate id")
        seen_ids.add(eid)
        if eid in EXCLUDED_PENDING_REGISTRATION:
            raise ManifestError(
                f"{ctx}: excluded pending-registration entry is present "
                f"(LEAD DECISIONS item 1)"
            )
        nk = e.get("node_kind")
        if not nk or not isinstance(nk, str) or not nk.strip():
            raise ManifestError(f"{ctx}: missing node_kind")
        if not str(e.get("class") or "").strip():
            raise ManifestError(f"{ctx}: missing class")
        if not str(e.get("origin") or "").strip():
            raise ManifestError(f"{ctx}: missing origin")
        if not str(e.get("comparer") or "").strip():
            raise ManifestError(f"{ctx}: missing comparer output_type")
        fast = e.get("fast")
        if fast is not None and not isinstance(fast, bool):
            raise ManifestError(
                f"{ctx}: optional 'fast' flag must be a boolean when present "
                f"(got {type(fast).__name__})"
            )
        exp = e.get("expected")
        if not isinstance(exp, dict) or not exp:
            raise ManifestError(f"{ctx}: missing expected verdict map")
        for accel in ACCELERATORS:
            if accel not in exp:
                raise ManifestError(f"{ctx}: expected verdict missing for {accel!r}")
            if exp[accel] not in VERDICTS:
                raise ManifestError(
                    f"{ctx}: expected[{accel!r}] = {exp[accel]!r} not in {VERDICTS}"
                )


def corpus_entry_ids() -> List[str]:
    """Validated manifest -> ordered id list (26 spec entries + syn-embedding)."""
    data = load_manifest()
    validate_manifest(data)
    return [str(e["id"]) for e in data["entries"]]


def fixture_ref(entry: Dict[str, Any]) -> Optional[str]:
    """Fixed input fixture ref for a manifest entry ('<type>/<filename>') or
    None for input-less nodes (e.g. the accelerator benchmark)."""
    ref = entry.get("fixture")
    if not ref:
        return None
    return str(ref)


def is_synthetic(node_kind: Optional[str]) -> bool:
    return bool(node_kind) and str(node_kind).startswith(SYNTHETIC_PREFIX)


def synthetic_name(node_kind: Optional[str]) -> Optional[str]:
    if not is_synthetic(node_kind):
        return None
    return str(node_kind)[len(SYNTHETIC_PREFIX):]


# ---------------------------------------------------------------------------
# Step 0 — hardware probe
# ---------------------------------------------------------------------------

def _online_worker_rows(db) -> List[Any]:
    """Online workers (same 'active worker' definition as the variant-test
    queue: LICENSED, heartbeating within 5 min, not paused, not mid-update),
    with their hardware profile."""
    import models
    stale_cutoff = _utcnow() - timedelta(minutes=5)
    return (
        db.query(models.WorkerLicense, models.HardwareProfile)
        .outerjoin(
            models.HardwareProfile,
            models.WorkerLicense.hardware_profile_id == models.HardwareProfile.id,
        )
        .filter(
            models.WorkerLicense.status == models.WorkerStatus.LICENSED,
            models.WorkerLicense.last_heartbeat > stale_cutoff,
            models.WorkerLicense.paused.is_(False),
            models.WorkerLicense.update_requested.is_(False),
            # BUG-1029b-2x: soft-deleted (tombstoned) workers are no longer part
            # of the fleet — their accelerators must not widen the probe.
            models.WorkerLicense.deleted_at.is_(None),
        )
        .all()
    )


def _accelerator_rows(db, hardware_profile_id: Optional[int]) -> List[Any]:
    import models
    if hardware_profile_id is None:
        return []
    return (
        db.query(models.AcceleratorProfile)
        .filter(models.AcceleratorProfile.hardware_profile_id == hardware_profile_id)
        .all()
    )


def _facts_for_kind(kind: str, devices: List[Any], profile) -> Dict[str, Any]:
    """Fold a worker profile's device rows into the per-accelerator facts.

    Facts are only recorded from what the workers actually reported — a
    missing value stays missing (that is what `unknown-hw` later keys off),
    never guessed.
    """
    from crud.accelerator_cud import is_placeholder_accelerator_name

    facts: Dict[str, Any] = {"device_count": len(devices)}
    names: List[str] = []
    for d in devices:
        if d.name and not is_placeholder_accelerator_name(d.name):
            names.append(str(d.name))
    if kind == "rocm":
        # rocm-smi/rocminfo report the ROCm runtime version as the driver
        # version; WSL hosts surface the /dev/dxg bridge (no native /dev/dri).
        vers = {str(d.driver_version) for d in devices if d.driver_version}
        facts["rocm_version"] = sorted(vers)[0] if vers else None
        # /dev/dri presence is only knowable from the worker's hardware_tags
        # / notes; record what we have and let absence drive unknown-hw.
        tags = _profile_tags(profile)
        dri = None
        if "dri" in tags:
            dri = "native"
        elif "dxg" in tags or "wsl" in tags:
            dri = "dxg"
        facts["dri_device"] = dri
    elif kind == "intel":
        models_seen = [n for n in names if n]
        facts["device_model"] = sorted(models_seen)[0] if models_seen else None
    elif kind == "npu":
        models_seen = [n for n in names if n]
        facts["device_model"] = sorted(models_seen)[0] if models_seen else None
    elif kind == "cuda":
        facts["device_model"] = sorted(names)[0] if names else None
        vers = {str(d.driver_version) for d in devices if d.driver_version}
        facts["driver_version"] = sorted(vers)[0] if vers else None
    return facts


def _profile_tags(profile) -> set:
    """A profile's hardware_tags + notes, lower-cased, for coarse facts like
    WSL /dev/dxg. Never guesses — returns what's stored."""
    out: set = set()
    specs = getattr(profile, "hardware_specs", None) or {}
    if isinstance(specs, dict):
        for t in (specs.get("hardware_tags") or []):
            out.add(str(t).lower())
    notes = getattr(profile, "notes", None)
    if notes:
        out.add(str(notes).lower())
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def probe_hardware(db) -> Dict[str, Any]:
    """STEP 0 — record per-accelerator facts from the ONLINE workers'
    reported capabilities. Returns ``{accelerator: facts_dict}`` where each
    facts dict carries the keys in ``_PROBE_FACTS_REQUIRED`` (None when the
    fleet reported nothing). Callers re-label ``spec`` cells using
    ``relabel_spec_cells``. No guessing: an absent fact is recorded as None.
    """
    facts: Dict[str, Dict[str, Any]] = {a: {} for a in ACCELERATORS}
    for wl, profile in _online_worker_rows(db):
        # `profile` is the joined HardwareProfile row itself (see
        # `_online_worker_rows`'s WorkerLicense/HardwareProfile outerjoin) —
        # its own PK is `.id`; AcceleratorProfile.hardware_profile_id is the
        # FK pointing back at that PK, not an attribute on HardwareProfile.
        hw_id = profile.id if profile is not None else None
        dev_rows = _accelerator_rows(db, hw_id)
        by_kind: Dict[str, List[Any]] = {}
        for d in dev_rows:
            by_kind.setdefault(str(d.kind or "").strip().lower(), []).append(d)
        for kind, rows in by_kind.items():
            if kind in facts:
                merged = _facts_for_kind(kind, rows, profile)
                base = facts[kind]
                for k, v in merged.items():
                    if k == "device_count":
                        base[k] = int(base.get(k) or 0) + int(v)
                    elif base.get(k) in (None, ""):
                        base[k] = v
    for accel, needed in _PROBE_FACTS_REQUIRED.items():
        for key in needed:
            facts[accel].setdefault(key, None)
    return facts


def relabel_spec_cells(
    expected: Dict[str, str],
    probe: Dict[str, Any],
) -> Dict[str, str]:
    """Re-label `spec` cells to `unknown-hw` where the probed accelerator
    lacks the facts needed to confirm the verdict. Non-spec cells are
    untouched. Pure function (no DB) so it is trivially testable."""
    out = dict(expected)
    for accel, verdict in out.items():
        if verdict != "spec":
            continue
        accel_probe = (probe or {}).get(accel) or {}
        for key in _PROBE_FACTS_REQUIRED.get(accel, []):
            if accel_probe.get(key) in (None, ""):
                out[accel] = UNKNOWN_HW
                break
    return out


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def _attempt_key() -> str:
    """Stable attempt key for the current harness run window (per UTC day),
    so a re-run of the same day resumes instead of re-planning, while a new
    day's run re-verifies against fresh hardware."""
    return datetime.now(timezone.utc).strftime("corpus-%Y%m%d")


def _row_corpus_meta(db, r) -> Dict[str, Any]:
    """The corpus metadata a verification row carries, if any.

    The dispatcher (``record_variant_test``) writes the attempt_history entry
    WITHOUT corpus fields — the row is scored through the ordinary wizard
    path. The harness's identity therefore travels through the BuildSpec:
    ``dispatch_cell`` stamps ``node_metadata["corpus_cell"]`` onto the
    BuildSpecRecord before dispatch, and the row's ``build_spec_id`` FK
    links it back. Falls back to an explicit ``attempt_history[-1]["corpus"]``
    stamp if one is ever present, then to {} (a non-corpus row)."""
    hist = getattr(r, "attempt_history", None) or []
    if hist and isinstance(hist, list):
        last = hist[-1]
        if isinstance(last, dict) and isinstance(last.get("corpus"), dict):
            return last["corpus"]
    if getattr(r, "build_spec_id", None) is not None:
        import models
        rec = (
            db.query(models.BuildSpecRecord)
            .filter(models.BuildSpecRecord.id == r.build_spec_id)
            .first()
        )
        if rec is not None and isinstance(rec.spec_json, dict):
            md = rec.spec_json.get("node_metadata")
            if isinstance(md, dict) and isinstance(md.get("corpus_cell"), dict):
                return md["corpus_cell"]
    return {}


def existing_attempt_cells(db, attempt_key: str) -> set:
    """(node_key, variant) pairs that already carry a NodeVariantVerification
    row for the current attempt key. One row per (node, variant, attempt)."""
    import models
    rows = (
        db.query(models.NodeVariantVerification)
        .filter(models.NodeVariantVerification.deleted_at.is_(None))
        .all()
    )
    seen = set()
    for r in rows:
        meta = _row_corpus_meta(db, r)
        if str(meta.get("attempt_key") or "") == attempt_key and meta.get("node_key"):
            seen.add((str(meta["node_key"]), str(r.variant or "").lower()))
    return seen


def converted_variant_cells(db) -> Dict[str, Dict[str, Any]]:
    """GO.10.s2 — the already-converted nodes: ``node_kind -> {variant: image}``
    for every live NodeDefinition whose ``gpu_variants`` is populated. A node
    with no ``gpu_variants`` is not a variant build (system catalog CPU tools,
    unconverted tenants) and contributes nothing — the verification pass never
    plans for it. Keys are lower-cased so they match the corpus cell accelerator
    vocabulary."""
    import models
    nodes = (
        db.query(models.NodeDefinition)
        .filter(models.NodeDefinition.deleted_at.is_(None))
        .all()
    )
    out: Dict[str, Dict[str, Any]] = {}
    for nd in nodes:
        nk = str(nd.node_kind or "").strip()
        if not nk:
            continue
        gv = nd.gpu_variants if isinstance(nd.gpu_variants, dict) else {}
        variants = {str(k).strip().lower(): v for k, v in gv.items()
                    if str(k).strip().lower()}
        if not variants:
            continue
        merged = out.setdefault(nk, {})
        merged.update(variants)
    return out


def plan_cells(
    db,
    entries: List[Dict[str, Any]],
    *,
    probe: Optional[Dict[str, Any]] = None,
    accelerators: Optional[List[str]] = None,
    attempt_key: Optional[str] = None,
    skip_done: bool = True,
    verification_only: bool = False,
) -> List[Dict[str, Any]]:
    """Build the (entry x accelerator) execution cells.

    Rules:
      - only accelerators with an ONLINE worker are planned (cpu always has
        one);
      - `incompat` (and `na`) cells are skipped — a known-incompatible cell
        is not re-run for its own sake;
      - `spec` cells are first re-labelled against the hardware probe: a cell
        whose accelerator lacks the needed facts becomes `unknown-hw` and is
        SKIPPED (no guessing);
      - resumable: a cell whose (node, variant) already has a
        NodeVariantVerification row for the current attempt key is skipped.
    Returns ordered cell dicts ready for the plan table / execution.

    ``verification_only`` (GO.10.s2, default False — the behaviour above is
    unchanged when it is off) switches the selection from "corpus entry x
    accelerator" to "already-converted node x its EXISTING variants": only
    nodes with a populated ``gpu_variants`` are considered, only variants that
    node already has are planned, and the manifest's expected verdict is used
    only as metadata (never to skip — a converted variant exists, so it must be
    verified regardless of what the corpus predicted). No conversion and no
    build/fan-out step is planned: the cell is a verification cell for an
    image that is already built.
    """
    online = online_kinds(db)
    requested = accelerators or list(ACCELERATORS)
    attempt_key = attempt_key or _attempt_key()
    done = existing_attempt_cells(db, attempt_key) if skip_done else set()
    probe = probe or probe_hardware(db)

    cells: List[Dict[str, Any]] = []
    converted = converted_variant_cells(db) if verification_only else {}
    for entry in entries:
        eid = str(entry["id"])
        node_key = entry["node_kind"]
        if verification_only:
            if is_synthetic(node_key):
                # Synthetic corpus nodes are built by the harness, never
                # converted in place — nothing to verify.
                continue
            node_variants = converted.get(node_key) or {}
            if not node_variants:
                continue  # unconverted node — verification mode never plans it
            for accel in requested:
                if accel not in ACCELERATORS or accel not in online:
                    continue
                if accel not in node_variants:
                    continue  # variant this node was never converted to
                expected = relabel_spec_cells(entry["expected"], probe)
                verdict = expected.get(accel)
                if verdict in ("incompat", "na"):
                    continue
                key = (node_key, accel)
                if key in done:
                    continue
                cells.append({
                    "id": eid,
                    "node_key": node_key,
                    "accelerator": accel,
                    "origin": entry["origin"],
                    "verdict": verdict if verdict in VERDICTS else "pass",
                    "comparer": entry["comparer"],
                    "fixture": fixture_ref(entry),
                    "attempt_key": attempt_key,
                    "verification_only": True,
                    "image_ref": node_variants[accel],
                })
            continue
        for accel in requested:
            if accel not in ACCELERATORS:
                continue
            if accel not in online:
                continue
            expected = relabel_spec_cells(entry["expected"], probe)
            verdict = expected.get(accel)
            if verdict in ("incompat", "na", UNKNOWN_HW):
                continue
            key = (node_key, accel)
            if key in done:
                continue
            cells.append({
                "id": eid,
                "node_key": node_key,
                "accelerator": accel,
                "origin": entry["origin"],
                "verdict": verdict,
                "comparer": entry["comparer"],
                "fixture": fixture_ref(entry),
                "attempt_key": attempt_key,
            })
    return cells


def online_kinds(db) -> set:
    """Accelerator kinds with an online worker (cpu is always available)."""
    from .queue import online_accelerator_kinds
    return online_accelerator_kinds(db) | {"cpu"}


# ---------------------------------------------------------------------------
# Execution (enqueue-only; never blocks in-request)
# ---------------------------------------------------------------------------

def _setting_enabled(db) -> bool:
    """The platform setting that gates any non-dry-run behaviour (default
    off). Imported lazily so module import never touches the registry."""
    from services import platform_settings
    try:
        return bool(platform_settings.get_setting(
            "node_build.corpus_harness_enabled", db=db,
        ))
    except platform_settings.SettingNotRegisteredError:
        return False


def register_harness_setting() -> None:
    """Register the enabling platform setting (idempotent). Called from the
    CLI entrypoint and importable by the admin settings page."""
    from services import platform_settings
    platform_settings.register(
        "node_build.corpus_harness_enabled",
        bool,
        False,
        group="node_build",
        description="GO.5 golden-corpus harness: allow non-dry-run execution "
                    "of the accelerator-variant verification corpus. Off by "
                    "default; the --dry-run CLI needs nothing set.",
    )


def dispatch_cell(db, cell: Dict[str, Any], *, spec: Any, actor_id: Optional[int]) -> Dict[str, Any]:
    """Enqueue ONE cell through the EXISTING single-variant rebuild DAG +
    per-variant sample-test. The DAG's sample-test node POSTs to
    ``/wizard/variant-sample-test-result`` which scores the output and writes
    the NodeVariantVerification row. Enqueue-only: this returns immediately
    with the run id; it never blocks on the build.

    The cell's corpus metadata (id / attempt_key / node_key / comparer /
    expected verdict) is stamped into the BuildSpec's node_metadata so
    ``collect_results`` can tie each verification row back to its corpus cell.
    """
    import models
    from node_builder.build_dag.submit import create_build_workflow
    from services.workflow_orchestrator import dispatch_trigger_run

    variant = cell["accelerator"]
    md = dict(getattr(spec, "node_metadata", None) or {})
    md["corpus_cell"] = {
        "id": cell["id"],
        "node_key": cell["node_key"],
        "variant": variant,
        "attempt_key": cell["attempt_key"],
        "comparer": cell["comparer"],
        "expected": cell["verdict"],
    }
    if cell.get("verification_only"):
        # GO.10.s2 — verification-only identity travels with the cell so a
        # later collect_results pass knows this row verified an already-
        # converted node (no build/fan-out happened).
        md["corpus_cell"]["verification_only"] = True
        if cell.get("image_ref") is not None:
            md["corpus_cell"]["image_ref"] = cell["image_ref"]
    spec.node_metadata = md

    rec = models.BuildSpecRecord(tenant_id=spec.tenant_id, spec_json=spec.model_dump())
    db.add(rec)
    db.commit()
    db.refresh(rec)
    spec._build_spec_id = rec.id  # type: ignore[attr-defined]

    wf_info = create_build_workflow(db, spec, user_id=actor_id, rebuild_variant=variant)
    wf = db.query(models.Workflow).filter(models.Workflow.id == wf_info["workflow_id"]).first()
    run = dispatch_trigger_run(
        db, workflow=wf, triggered_by_user_id=actor_id,
        extra_inputs={"build_spec_id": rec.id, "rebuild_variant": variant,
                      "corpus_harness": True},
    )
    rec.workflow_run_id = getattr(run, "id", None)
    db.commit()
    return {
        "id": cell["id"],
        "variant": variant,
        "build_spec_id": rec.id,
        "workflow_run_id": rec.workflow_run_id,
        "verification_only": bool(cell.get("verification_only")),
    }


def _service_actor(db):
    import models
    return (
        db.query(models.User).filter(models.User.is_platform_admin.is_(True)).first()
    )


def run_cells(
    db,
    cells: List[Dict[str, Any]],
    *,
    attempt_key: Optional[str] = None,
    allow_non_wizard_fallback: bool = False,
    verification_only: bool = False,
) -> List[Dict[str, Any]]:
    """Execute planned cells by enqueueing each through the build path.
    Gated by the platform setting; refuses when off (the CLI relies on this
    so a bare invocation never starts fleet builds). ``allow_non_wizard_fallback``
    (CLI ``--allow-non-wizard-fallback``, off by default) lets real corpus
    nodes with no linked BuildSpec resolve one from their own build source —
    see ``_spec_for_cell``. Returns one dispatch record per enqueued cell.

    ``verification_only`` (GO.10.s2) re-verification of already-converted nodes:
    the non-wizard fallback is force-disabled here (verification must never
    invent a build step — a converted node without a recoverable spec is simply
    skipped), and each dispatch record carries the flag so the caller can tell
    verification dispatches from fresh-build dispatches."""
    if not _setting_enabled(db):
        logger.info("corpus harness: node_build.corpus_harness_enabled is off — "
                    "no cells enqueued")
        return []
    attempt_key = attempt_key or _attempt_key()
    actor = _service_actor(db)
    actor_id = getattr(actor, "id", None)
    if verification_only:
        allow_non_wizard_fallback = False
    results: List[Dict[str, Any]] = []
    for cell in cells:
        spec = _spec_for_cell(db, cell, attempt_key=attempt_key,
                              allow_non_wizard_fallback=allow_non_wizard_fallback)
        if spec is None:
            logger.warning("corpus harness: no BuildSpec for cell %s/%s — skipped",
                           cell["id"], cell["accelerator"])
            continue
        try:
            results.append(dispatch_cell(db, cell, spec=spec, actor_id=actor_id))
        except Exception as exc:  # noqa: BLE001 — one bad cell must not kill the sweep
            logger.warning("corpus harness: dispatch failed for %s/%s: %s",
                           cell["id"], cell["accelerator"], exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    return results


# GO.5b — synthetic corpus entries ship their build source in this package
# (one dir per synthetic node: entrypoint.sh, node_config.json,
# requirements.txt, per-variant Dockerfiles). A synthetic cell with no
# source dir keeps the legacy skip (None) with a clear reason.
_SYNTHETIC_SOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "corpus_sources")
# The wizard build path (create_build_workflow + rebuild_variant) resolves
# the per-variant Dockerfile from spec.variants[<variant>], falling back to
# hero_dockerfile. Synthetic dirs name their per-variant files
# Dockerfile.<variant> next to the hero Dockerfile.
_SYNTHETIC_HERO_DOCKERFILE = "Dockerfile"
# Corpus cells build under the platform tenant (the same convention the
# platform/system builds use for their BuildSpecs — see
# dependencies.py PLATFORM_TENANT_ID).
_SYNTHETIC_TENANT_ID = 1


def synthetic_sources_dir(name: str) -> str:
    """Absolute path of a synthetic node's build-source dir (or the path it
    WOULD have) — callers must os.path.isdir() it before use."""
    return os.path.join(_SYNTHETIC_SOURCES_DIR, str(name))


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _synthetic_node_config(name: str) -> Dict[str, Any]:
    """Parse the dir's node_config.json (declared inputs/outputs + per-variant
    Dockerfile/requirements/env hints). Missing file -> {} (the spec then
    falls back to the manifest's comparer for I/O)."""
    raw = _read_text(os.path.join(synthetic_sources_dir(name), "node_config.json"))
    if raw is None:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _variant_dockerfile_for(name: str, variant: str) -> Optional[str]:
    """Per-variant Dockerfile content: Dockerfile.<variant> in the source
    dir, else the hero Dockerfile (single-Dockerfile nodes like the
    NVIDIA-only TensorRT one)."""
    d = synthetic_sources_dir(name)
    per_variant = os.path.join(d, f"Dockerfile.{variant}")
    if os.path.isfile(per_variant):
        return _read_text(per_variant)
    return _read_text(os.path.join(d, _SYNTHETIC_HERO_DOCKERFILE))


def build_synthetic_spec(name: str, variant: str,
                         comparer: str = "") -> Optional[Any]:
    """Assemble the BuildSpec for ONE synthetic corpus cell by reading the
    node's build-source dir (corpus_sources/<name>/). Returns None — the
    caller keeps its skip — when the dir is missing or has no entrypoint.

    Mirrors what a docs-driven wizard build produces: hero_dockerfile +
    per-variant Dockerfiles in spec.variants (the rebuild-variant DAG
    resolves spec.variants[<variant>], else hero_dockerfile), the
    entrypoint, every other text file as source_files, and the
    declared I/O from node_config.json (falling back to the manifest
    comparer)."""
    from node_builder.build_dag.types import (
        BuildArtifact,
        BuildSpec,
        BuildVariantSpec,
    )

    d = synthetic_sources_dir(name)
    if not os.path.isdir(d):
        return None
    entrypoint = _read_text(os.path.join(d, "entrypoint.sh"))
    if not entrypoint:
        logger.warning(
            "corpus harness: synthetic source dir %s has no entrypoint.sh", d)
        return None

    cfg = _synthetic_node_config(name)
    dockerfile = _read_text(os.path.join(d, _SYNTHETIC_HERO_DOCKERFILE))
    if not dockerfile:
        logger.warning("corpus harness: synthetic source dir %s has no Dockerfile", d)
        return None

    # One BuildVariantSpec per accelerator, ALWAYS — not just the ones with
    # their own Dockerfile.<var>. The single-variant rebuild DAG embeds
    # spec.variants[variant].dockerfile into the build-task's node data,
    # but the build-task CONTAINER independently re-reads
    # spec["variants"][variant] at build time and hard-fails
    # ("variant 'X' not found in spec") when the key is simply absent — it
    # does not fall back to hero_dockerfile itself (GO.6a finding: every
    # accelerator without its own Dockerfile.<var>, e.g. cpu for
    # onnx-multi-ep-classifier / jax-sobel-filter, failed every real
    # dispatch this way even though the manifest declares it `pass`).
    # `_variant_dockerfile_for` already has the correct
    # per-variant-else-hero fallback; reuse it here instead of leaving the
    # fallback to a downstream component that doesn't do it.
    variants: List[BuildVariantSpec] = []
    for var in ACCELERATORS:
        content = _variant_dockerfile_for(name, var)
        if content:
            variants.append(BuildVariantSpec(variant=var, dockerfile=content))

    source_files: List[BuildArtifact] = []
    for fname in sorted(os.listdir(d)):
        fpath = os.path.join(d, fname)
        if not os.path.isfile(fpath) or fname in (
                "entrypoint.sh", _SYNTHETIC_HERO_DOCKERFILE,
                "Dockerfile.cuda", "Dockerfile.rocm", "Dockerfile.intel",
                "Dockerfile.npu", "Dockerfile.cpu"):
            continue
        content = _read_text(fpath)
        if content is not None:
            source_files.append(BuildArtifact(filename=fname, content=content))

    input_type = str(cfg.get("input_type") or "image")
    output_type = str(cfg.get("output_type") or comparer or "image")
    spec = BuildSpec(
        image_name=f"corpus-syn-{name}",
        node_name=f"synthetic {name}",
        tenant_id=_SYNTHETIC_TENANT_ID,
        hero_dockerfile=dockerfile,
        entrypoint_sh=entrypoint,
        variants=variants,
        source_files=source_files,
        hero_variant=variant,
        input_type=input_type,
        output_type=output_type,
        process_type=str(cfg.get("process_type") or "GPU"),
        description=str(cfg.get("description") or f"corpus synthetic: {name}"),
        node_metadata={"corpus_synthetic": name},
    )
    return spec


def _spec_for_cell(db, cell: Dict[str, Any], *, attempt_key: str,
                   allow_non_wizard_fallback: bool = False) -> Optional[Any]:
    """Recover/derive the BuildSpec for a corpus cell. Two paths:

    - synthetic nodes (node_kind ``synthetic:<name>``): build the spec from
      the node's build-source dir (corpus_sources/<name>/) — no NodeDefinition
      row involved. A missing dir returns None (kept skip) with a reason.
    - real nodes: reuse the node's originating spec when a NodeDefinition row
      is already linked via a NodeVariantVerification row (the registered-node
      path). When no such linkage exists AND ``allow_non_wizard_fallback`` is
      set (CLI ``--allow-non-wizard-fallback``; off by default so a bare
      invocation's behaviour never changes), fall back to
      ``backfill.build_non_wizard_spec`` — the SAME source-resolution
      (``resolve_node_build_source``) and BuildSpec-construction code the
      variant-backfill sweep's non-wizard path uses, not a parallel schema.
      Still never guesses: returns None with a logged reason when the node's
      own build source isn't resolvable."""
    import models
    from node_builder.build_dag.types import BuildSpec

    node_key = cell["node_key"]
    if is_synthetic(node_key):
        name = synthetic_name(node_key)
        spec = build_synthetic_spec(
            name, cell["accelerator"], comparer=str(cell.get("comparer") or ""))
        if spec is None:
            logger.warning(
                "corpus harness: no build source for synthetic %s "
                "(expected %s) — cell %s/%s skipped", name,
                synthetic_sources_dir(name), cell["id"], cell["accelerator"])
        return spec
    nd = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.node_kind == node_key,
            models.NodeDefinition.deleted_at.is_(None),
        )
        .first()
    )
    if nd is None:
        return None
    bsid_row = (
        db.query(models.NodeVariantVerification.build_spec_id)
        .filter(
            models.NodeVariantVerification.node_definition_id == nd.id,
            models.NodeVariantVerification.build_spec_id.isnot(None),
            models.NodeVariantVerification.deleted_at.is_(None),
        )
        .order_by(models.NodeVariantVerification.id.desc())
        .first()
    )
    if not bsid_row:
        if allow_non_wizard_fallback:
            from . import backfill
            spec = backfill.build_non_wizard_spec(db, nd, cell["accelerator"])
            if spec is None:
                logger.warning(
                    "corpus harness: non-wizard fallback found no resolvable "
                    "build source for %s/%s — skipped", node_key, cell["accelerator"])
            return spec
        return None
    rec = (
        db.query(models.BuildSpecRecord)
        .filter(models.BuildSpecRecord.id == bsid_row[0],
                models.BuildSpecRecord.deleted_at.is_(None))
        .first()
    )
    if rec is None or not isinstance(rec.spec_json, dict):
        return None
    try:
        return BuildSpec(**rec.spec_json)
    except Exception as exc:  # noqa: BLE001
        logger.warning("corpus harness: BuildSpec reconstruct failed for %s: %s", node_key, exc)
        return None


# ---------------------------------------------------------------------------
# Result collection + compare
# ---------------------------------------------------------------------------

# GO.10.s2 — corpus verdict -> NodeDefinition.variant_verification_status label.
# The label vocabulary is the one the enqueue/routing side already speaks
# (routers/worker_tasks.py writes "verified"/"failed"/"numerical_drift";
# utils/queue_selector.py treats {"failed", "numerical_drift"} as known-broken
# and only "verified" as VERIFIED). "marginal" passed the publish gate, so it
# counts as verified for routing; an absent/untested verdict writes nothing.
_VERIFICATION_STATUS_FOR_VERDICT: Dict[str, str] = {
    "verified": "verified",
    "marginal": "verified",
    "drift": "numerical_drift",
    "failed": "failed",
    "incompatible": "incompatible",
}


def record_verification_status(db, node_key: str, variant: str,
                               verdict: Optional[str]) -> Optional[str]:
    """GO.10.s2 — write ``NodeDefinition.variant_verification_status[variant]``
    for one verified (node, variant) pair of an already-converted node. Returns
    the label written, or None when nothing was written (unknown verdict, no
    matching live NodeDefinition). Existing labels are overwritten only by a
    fresh corpus verdict — the harness re-verifies, so the newest result wins.
    Never touches ``gpu_variants`` (no conversion happens here)."""
    import models
    label = _VERIFICATION_STATUS_FOR_VERDICT.get(str(verdict or "").lower())
    if label is None:
        return None
    nd = (
        db.query(models.NodeDefinition)
        .filter(
            models.NodeDefinition.node_kind == node_key,
            models.NodeDefinition.deleted_at.is_(None),
        )
        .first()
    )
    if nd is None:
        return None
    current = dict(nd.variant_verification_status or {})
    if current.get(variant) == label:
        return label
    current[variant] = label
    nd.variant_verification_status = current
    # The harness owns this write (same pattern node_authoring_service uses for
    # the build path): persist it here so a caller that never commits — e.g.
    # the weekly gate returning early — still records the corpus verdict.
    db.commit()
    return label


def collect_results(
    db,
    cells: List[Dict[str, Any]],
    *,
    attempt_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read the sample-test result rows the dispatched runs wrote and fold
    each into a corpus result: build ok, run ok, parity score, wall time and
    peak VRAM (when reported). Comparison itself is delegated to the EXISTING
    ``comparer.dispatch_compare`` — the dispatcher already scored the output
    against the reference when it wrote the row, so this only re-derives the
    verdict the same way and attaches performance. No re-computation of bytes
    here (the dispatcher owns that).

    GO.10.s2 addition: for cells produced in verification-only mode (already-
    converted nodes), the verdict is also folded into
    ``NodeDefinition.variant_verification_status[variant]`` via
    ``record_verification_status`` so the palette/routing side sees the
    corpus's honest verdict. Default-mode cells keep their previous behaviour
    (the wizard build path already seeds that map); recording is idempotent
    and only ever writes a label the corpus actually observed."""
    import models
    from . import comparer

    attempt_key = attempt_key or _attempt_key()
    rows = (
        db.query(models.NodeVariantVerification)
        .filter(models.NodeVariantVerification.deleted_at.is_(None))
        .all()
    )
    by_key: Dict[tuple, Any] = {}
    for r in rows:
        meta = _row_corpus_meta(db, r)
        if str(meta.get("attempt_key") or "") == attempt_key and meta.get("node_key"):
            by_key[(str(meta["node_key"]), str(r.variant or "").lower())] = r

    out: List[Dict[str, Any]] = []
    for cell in cells:
        row = by_key.get((cell["node_key"], cell["accelerator"]))
        res = {
            "id": cell["id"],
            "node_key": cell["node_key"],
            "accelerator": cell["accelerator"],
            "attempt_key": attempt_key,
        }
        if row is None:
            res.update({"build_ok": False, "run_ok": False, "score": None,
                        "verdict": None, "wall_time_s": None, "peak_vram_mb": None})
            out.append(res)
            continue
        # Re-derive the compare verdict the same way the dispatcher would for
        # a fresh pair (dispatch_compare routes heavy types to the container).
        # The row already holds the authoritative score; we surface it.
        res.update({
            "build_ok": True,
            "run_ok": row.verdict not in ("failed", "incompatible"),
            "score": row.score,
            "verdict": row.verdict,
            "wall_time_s": _perf_wall_time(db, row),
            "peak_vram_mb": _perf_peak_vram(db, row),
            "output_type": row.output_type_at_test,
        })
        # comparer is the single routing point — record which mode would be
        # used for this cell's type (container vs inline) for the report.
        res["compare_mode"] = (
            "container" if comparer.needs_container_compare(cell["comparer"]) else "inline"
        )
        # GO.10.s2 — verification-only cells (already-converted nodes): fold
        # the observed verdict into the node's variant_verification_status so
        # the palette + scheduler routing reflect the corpus result. Default-
        # mode cells are untouched (the wizard build path owns that map).
        if cell.get("verification_only"):
            res["verification_status"] = record_verification_status(
                db, cell["node_key"], cell["accelerator"], row.verdict,
            )
        out.append(res)
    # GO.7.s6 (tasks.md 23.6): flag perf cliffs before the results are
    # published, so a >20% regression vs the CUDA cell of the same node is
    # never silently counted as a pass (openspec
    # launch-completion-megaplan/decomp/go-accel-conversion.md:88).
    flag_perf_cliffs(out)
    return out


def _perf_wall_time(db, row) -> Optional[float]:
    """Wall time of the run that produced this verification row, if the
    linked workflow_run's sample-test node reported one. None when unknown."""
    import models
    if row.build_spec_id is None:
        return None
    rec = (
        db.query(models.BuildSpecRecord).filter(models.BuildSpecRecord.id == row.build_spec_id)
        .first()
    )
    if rec is None or rec.workflow_run_id is None:
        return None
    node_runs = (
        db.query(models.NodeRun)
        .filter(models.NodeRun.workflow_run_id == rec.workflow_run_id)
        .all()
    )
    for nr in node_runs:
        outs = getattr(nr, "outputs", None) or {}
        if isinstance(outs, dict) and outs.get("wall_time_s") is not None:
            try:
                return float(outs["wall_time_s"])
            except (TypeError, ValueError):
                continue
    return None


def _perf_peak_vram(db, row) -> Optional[float]:
    """Peak VRAM (MB) the run reported, if any. None when not reported."""
    import models
    if row.build_spec_id is None:
        return None
    rec = (
        db.query(models.BuildSpecRecord).filter(models.BuildSpecRecord.id == row.build_spec_id)
        .first()
    )
    if rec is None or rec.workflow_run_id is None:
        return None
    node_runs = (
        db.query(models.NodeRun)
        .filter(models.NodeRun.workflow_run_id == rec.workflow_run_id)
        .all()
    )
    for nr in node_runs:
        outs = getattr(nr, "outputs", None) or {}
        if isinstance(outs, dict) and outs.get("peak_vram_mb") is not None:
            try:
                return float(outs["peak_vram_mb"])
            except (TypeError, ValueError):
                continue
    return None


# Owner bar (openspec launch-completion-megaplan/decomp/go-accel-conversion.md:88,
# tasks.md 23.6 / GO.7.s6): a variant that runs >20% slower than the CUDA cell of
# the same node is a perf cliff — it must be FLAGGED in the corpus results, never
# silently published as a pass into variant_performance.
PERF_CLIFF_MIN_RATIO = 0.80


def _is_positive_number(value: Any) -> bool:
    """True only for a real number > 0. bools are not numbers (True must never
    read as a wall time); None/str/NaN-inf style junk is not a measurement."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        # math.isfinite, not bare `> 0`: NaN and +/-inf are junk measurements,
        # never a real wall time (inf would otherwise divide every other row).
        return value > 0 and math.isfinite(value)
    except OverflowError:  # math.isfinite on an int too big for a float
        return False


def flag_perf_cliffs(
    results: List[Dict[str, Any]],
    *,
    min_ratio: float = PERF_CLIFF_MIN_RATIO,
) -> None:
    """Attach `perf_ratio_vs_cuda` and `perf_cliff` to each corpus result dict.

    Pure annotation (mutates the dicts in place, returns None) so the existing
    result shape stays additive: two new optional keys, nothing renamed.

    Per `node_key` group the reference is the result whose accelerator is
    `cuda` with `run_ok` true and a positive wall time. For every OTHER result
    in the group with a positive wall time: `ratio = ref_wall / wall` (1.0 =
    parity with CUDA, lower = slower); `perf_ratio_vs_cuda = round(ratio, 3)`
    and `perf_cliff = ratio < min_ratio` (strict: exactly the bar is not a
    cliff). A cuda row that ran (run_ok true, real wall) is parity by
    definition (1.0 / False); a failed cuda row gets None/None like any row
    without a reference.

    Fail closed toward unknown: no reference, no wall time, or a junk wall
    value yields `perf_ratio_vs_cuda = None` and `perf_cliff = None` — never
    False, which would read as "no cliff". Bad perf values must never raise
    out of `collect_results`: a result dict is still returned."""
    by_node: Dict[str, List[Dict[str, Any]]] = {}
    for res in results:
        by_node.setdefault(str(res.get("node_key") or ""), []).append(res)

    for group in by_node.values():
        ref_wall = None
        for res in group:
            if (str(res.get("accelerator") or "").lower() == "cuda"
                    and res.get("run_ok") is True
                    and _is_positive_number(res.get("wall_time_s"))):
                ref_wall = float(res["wall_time_s"])
                break

        for res in group:
            is_cuda = str(res.get("accelerator") or "").lower() == "cuda"
            if ref_wall is None:
                res["perf_ratio_vs_cuda"] = None
                res["perf_cliff"] = None
                continue
            if is_cuda:
                # A cuda row is parity by definition ONLY when it actually ran
                # with a real wall time; a failed cuda cell is not a reference
                # and must not be labelled 1.0/False (unknown, like any row
                # without a reference).
                if (res.get("run_ok") is True
                        and _is_positive_number(res.get("wall_time_s"))):
                    res["perf_ratio_vs_cuda"] = 1.0
                    res["perf_cliff"] = False
                else:
                    res["perf_ratio_vs_cuda"] = None
                    res["perf_cliff"] = None
                continue
            wall = res.get("wall_time_s")
            if not _is_positive_number(wall):
                res["perf_ratio_vs_cuda"] = None
                res["perf_cliff"] = None
                continue
            try:
                ratio = ref_wall / float(wall)
                res["perf_ratio_vs_cuda"] = round(ratio, 3)
                res["perf_cliff"] = bool(ratio < min_ratio)
            except (TypeError, ValueError, OverflowError):
                res["perf_ratio_vs_cuda"] = None
                res["perf_cliff"] = None


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------

def _fmt_table(rows: List[Dict[str, Any]], headers: List[str]) -> str:
    widths = [len(h) for h in headers]
    str_rows = []
    for r in rows:
        cells = [str(r.get(h, "")) for h in headers]
        str_rows.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for cells in str_rows:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)))
    return "\n".join(out)


def render_plan_table(cells: List[Dict[str, Any]],
                      skipped: Dict[str, List[Dict[str, Any]]]) -> str:
    headers = ["id", "node_key", "accelerator", "origin", "verdict", "comparer"]
    table = _fmt_table(cells, headers)
    lines = ["PLAN (cells to execute):", table or "  (none)"]
    for reason, items in sorted(skipped.items()):
        if items:
            ids = ", ".join(f"{i['id']}/{i['accelerator']}" for i in items[:20])
            lines.append(f"SKIPPED[{reason}] ({len(items)}): {ids}")
    return "\n".join(lines)


def build_plan(
    db,
    *,
    only: Optional[List[str]] = None,
    accelerators: Optional[List[str]] = None,
    attempt_key: Optional[str] = None,
    verification_only: bool = False,
) -> Dict[str, Any]:
    """Load+validate the manifest, run the probe, and produce the plan table
    data. Shared by --dry-run and the (setting-gated) live path. Writes
    nothing. ``verification_only`` (GO.10.s2) restricts the plan to the
    already-converted nodes' existing variants."""
    data = load_manifest()
    validate_manifest(data)
    entries = data["entries"]
    if only:
        want = {str(x).strip() for x in only}
        entries = [e for e in entries if str(e["id"]) in want]
        missing = want - {str(e["id"]) for e in entries}
        if missing:
            raise ManifestError(f"--only id(s) not in corpus: {sorted(missing)}")
    probe = probe_hardware(db)
    cells = plan_cells(db, entries, probe=probe,
                       accelerators=accelerators, attempt_key=attempt_key,
                       verification_only=verification_only)
    skipped: Dict[str, List[Dict[str, Any]]] = {
        "incompat": [], "unknown-hw": [], "already-verified": [], "no-hardware": [],
    }
    online = online_kinds(db)
    attempt_key = attempt_key or _attempt_key()
    done = existing_attempt_cells(db, attempt_key)
    converted = converted_variant_cells(db) if verification_only else {}
    if verification_only:
        skipped["not-converted"] = []
    for e in entries:
        nk = e["node_kind"]
        if verification_only:
            if is_synthetic(nk) or not converted.get(nk):
                skipped["not-converted"].append({"id": e["id"], "accelerator": "*"})
                continue
        for accel in (accelerators or list(ACCELERATORS)):
            if accel not in ACCELERATORS:
                continue
            if accel not in online:
                if (nk, accel) not in done:
                    skipped["no-hardware"].append({"id": e["id"], "accelerator": accel})
                continue
            if verification_only and accel not in (converted.get(nk) or {}):
                continue
            exp = relabel_spec_cells(e["expected"], probe)
            cell = {"id": e["id"], "accelerator": accel}
            if exp.get(accel) in ("incompat", "na"):
                skipped["incompat"].append(cell)
            elif exp.get(accel) == UNKNOWN_HW:
                skipped["unknown-hw"].append(cell)
            elif (nk, accel) in done:
                skipped["already-verified"].append(cell)
    if verification_only:
        return {
            "cells": cells,
            "skipped": skipped,
            "probe": probe,
            "online": sorted(online),
            "attempt_key": attempt_key,
            "entry_count": len(entries),
            "verification_only": True,
        }
    return {
        "cells": cells,
        "skipped": skipped,
        "probe": probe,
        "online": sorted(online),
        "attempt_key": attempt_key,
        "entry_count": len(entries),
    }


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m node_builder.variant_test.corpus_harness",
        description="Golden-corpus accelerator-variant verification harness. "
                    "--dry-run prints the plan and writes nothing.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan table and write nothing (default mode)")
    p.add_argument("--only", default=None,
                   help="comma-separated corpus ids to restrict the plan (e.g. 01,02,syn-embedding)")
    p.add_argument("--accel", default=None,
                   help="comma-separated accelerators (default all: cuda,rocm,intel,npu,cpu)")
    p.add_argument("--allow-non-wizard-fallback", action="store_true",
                   dest="allow_non_wizard_fallback",
                   help="when a real (non-synthetic) corpus node has no BuildSpec "
                        "linked via a NodeVariantVerification row, resolve one from "
                        "the node's own DockerContainers/<node> build source instead "
                        "of skipping the cell (same resolver the variant-backfill "
                        "sweep's non-wizard path uses). Off by default.")
    p.add_argument("--verification-only", action="store_true",
                   dest="verification_only",
                   help="GO.10.s2 verification pass: plan cells ONLY for nodes that "
                        "are already converted (NodeDefinition.gpu_variants populated), "
                        "one per existing variant, and record "
                        "NodeDefinition.variant_verification_status from the results. "
                        "No conversion, no new variant fan-out; unconverted nodes are "
                        "skipped. Off by default (the normal corpus plan is unchanged).")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    import database
    from database import SessionLocal

    register_harness_setting()
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    only = [x for x in (args.only or "").split(",") if x.strip()] or None
    accel = [x.strip() for x in (args.accel or "").split(",") if x.strip()] or None

    # Standalone CLI invocation (`python -m ...`) runs in a fresh process
    # that never went through main.py's startup — `database.initialize()`
    # was never called, so SessionLocal() would raise. Every other
    # standalone script in this tree (seed_*.py, scripts/*.py) calls
    # database.initialize() itself before using SessionLocal(); this CLI
    # never did, so it hard-failed on every real invocation outside the
    # test suite (tests monkeypatch database.SessionLocal directly and
    # never exercise this path). Lazily initialize only when needed so the
    # unit tests' monkeypatched SessionLocal is left untouched.
    try:
        db = SessionLocal()
    except RuntimeError:
        database.initialize()
        db = SessionLocal()
    try:
        plan = build_plan(db, only=only, accelerators=accel,
                          verification_only=args.verification_only)
        print(render_plan_table(plan["cells"], plan["skipped"]))
        print(f"\nonline accelerators: {', '.join(plan['online']) or '(none)'}")
        print(f"attempt key: {plan['attempt_key']}")
        print(f"entries: {plan['entry_count']}")

        if args.dry_run:
            print("\n[dry-run] nothing enqueued, nothing written.")
            return 0

        # Non-dry-run: gated by the platform setting (default off).
        if not _setting_enabled(db):
            print("\n[off] node_build.corpus_harness_enabled is OFF — set it "
                  "(platform settings) to allow live execution. Nothing enqueued.")
            return 0
        enqueued = run_cells(db, plan["cells"], attempt_key=plan["attempt_key"],
                            allow_non_wizard_fallback=args.allow_non_wizard_fallback,
                            verification_only=args.verification_only)
        print(f"\n[exec] enqueued {len(enqueued)} cell(s).")
        for r in enqueued:
            print(f"  {r['id']}/{r['variant']} build_spec={r['build_spec_id']} "
                  f"workflow_run={r['workflow_run_id']}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
