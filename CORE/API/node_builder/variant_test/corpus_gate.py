"""GO.8 — corpus regression gate for variant-build changes.

A pure-logic gate that decides (a) whether a change set NEEDS the golden
corpus to run, and (b) whether a finished corpus run meets the quality
bars. Dark by default: it is a CLI + a registered platform setting
(``node_build.corpus_gate_bars``); nothing here dispatches builds.

Weekly run (the GO.8 follow-up): two platform settings gate an APScheduler
job pair registered from ``services/scheduler.py:start_scheduler()`` —
``node_build.corpus_gate_weekly_enabled`` (bool, default false) arms the
weekly enqueue of the manifest's ``fast`` subset through the
``corpus_harness.run_cells`` path (attempt key ``corpus-weekly-<YYYYMMDD>``,
idempotent within the same UTC day), and ``node_build.wizard_release_blocked``
(bool, default false) is a RELEASE BLOCK that a bar breach flips on and
which is never auto-cleared (an operator clears it deliberately once the
corpus is green again). The evaluation step re-checks the latest weekly
attempt every 6h — the attempt the last enqueue recorded in
``node_build.corpus_gate_weekly_last_attempt`` (falling back to the
current UTC day's key when none is recorded), evaluated at most once per
attempt (``node_build.corpus_gate_weekly_last_evaluated``); once every
planned (node, accelerator) cell has a
verification row it runs ``evaluate()`` against ``get_bars()`` — on a
breach it appends one ``WIZARD_RELEASE_BLOCKED`` ``audit_events`` row with
the failing metrics and sets the block flag to true.

Trigger logic: ``needs_gate(changed_paths)`` returns True when the change
set touches anything the variant-build pipeline runs on —
``node_builder/build_dag/variant_strategy.py``, anything under
``node_builder/variant_test/``, the install-recipes module(s) the
wizard's adapt/repair path consumes (``node_builder/install_recipes.py``
+ the exemplar-retrieval learner), or the base image builds under
``CORE/DockerContainers/BaseImages/``. Everything else is a no-op
("gate not required").

Fast subset: manifest entries flagged ``fast: true`` (validated by
``corpus_harness.validate_manifest`` to be a boolean when present). A
triggered run does not have to re-verify the whole 27-entry corpus —
``fast_subset(manifest)`` is the minimum slice to run for a green gate.

Evaluation: rows come from ``corpus_harness.collect_results`` (one row per
entry x accelerator cell: ``build_ok`` / ``run_ok`` / ``score`` +
``verdict``). Rows are aggregated per (accelerator, class); the per-cell
class is the manifest entry's ``class``. Each metric rate is
passes/attempted within a (accelerator, class) group and must reach the
bar:

  - ``build``  — ``build_ok`` is True
  - ``run``    — ``run_ok`` is True (only attempted when build_ok — a
    failed build is scored against the build bar only)
  - ``parity`` — the compare verdict is pass/pass-tol (rows with no
    verdict score, e.g. build/run failures, are NOT attempted parity)

Bar comparison is inclusive (rate == bar passes), so a group exactly at
the bar is green.

Bars live in the platform-settings registry as
``node_build.corpus_gate_bars`` (str — the registry's supported types are
bool/int/float/str — default JSON ``{"build": 0.9, "run": 0.85,
"parity": 0.8}``, no env fallback, same ``node_build`` group as
``node_build.corpus_harness_enabled``). Malformed bars (bad JSON or a
non-numeric metric) fall back to the defaults with a warning — a
misconfigured bar must not make the gate vacuous or unrunnable.

CLI (dark by default; no env vars, no side effects):

    python -m node_builder.variant_test.corpus_gate \
        --changed-files changed.txt --results results.json

``--changed-files`` is a path to a newline-separated file of changed
paths, or "-" for stdin. Prints ``gate not required`` and exits 0 when
no trigger path changed; otherwise loads the bars, evaluates the rows and
exits 0 on a green gate, 1 listing each failing (accelerator, class,
metric) cell.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_BARS: Dict[str, float] = {"build": 0.9, "run": 0.85, "parity": 0.8}

BARS_SETTING_KEY = "node_build.corpus_gate_bars"

WEEKLY_SETTING_KEY = "node_build.corpus_gate_weekly_enabled"
BLOCKED_SETTING_KEY = "node_build.wizard_release_blocked"
# Bookkeeping for the weekly run: which attempt the evaluation job must
# look for, and which attempt it already evaluated (a run that finishes
# after midnight UTC must still be evaluated under ITS key, not the
# next day's).
LAST_ATTEMPT_KEY = "node_build.corpus_gate_weekly_last_attempt"
LAST_EVALUATED_KEY = "node_build.corpus_gate_weekly_last_evaluated"

_WEEKLY_LOGGER = logging.getLogger(__name__)

# Paths whose change requires the corpus gate (fnmatch globs, matched
# against repo-relative paths).
TRIGGER_GLOBS: Tuple[str, ...] = (
    "node_builder/build_dag/variant_strategy.py",
    "node_builder/variant_test/**",
    # The recipes module(s) the wizard adapt/repair path uses:
    # install-recipe persistence + the exemplar-retrieval learner.
    "node_builder/install_recipes.py",
    "node_builder/exemplar_retrieval.py",
    # Base image builds the variant DAG renders FROM.
    "CORE/DockerContainers/BaseImages/**",
)

# Verdicts that count as a parity pass (see corpus_harness.VERDICTS).
_PARITY_PASS_VERDICTS = frozenset(("pass", "pass-tol"))


@dataclass
class GateVerdict:
    """Outcome of evaluating corpus result rows against the bars."""

    passed: bool
    failing: List[Dict[str, Any]] = field(default_factory=list)


def _norm(path: str) -> str:
    """Normalise a changed path for glob matching (slash form, no
    leading ``./``/``/``, no ``.git/``)."""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./") or p.startswith("/"):
        p = p.lstrip("./")
    if not p or p == ".":
        return ""
    return p


def _matches_any_glob(path: str) -> bool:
    parts = path.split("/")
    if "git" in parts or parts[0] == ".git":
        return False
    # Changed paths may be repo-root-relative (CORE/API/node_builder/…) or
    # API-relative (node_builder/…), so match the full path AND every
    # path-segment suffix against each glob.
    candidates = [path]
    candidates += [path[i + 1:] for i, c in enumerate(path) if c == "/"]
    for pat in TRIGGER_GLOBS:
        for cand in candidates:
            if fnmatch(cand, pat) or fnmatch(cand, pat.rstrip("/**")):
                return True
    return False


def needs_gate(changed_paths: Iterable[str]) -> bool:
    """True when any changed path hits a TRIGGER_GLOB."""
    for p in changed_paths:
        n = _norm(p)
        if n and _matches_any_glob(n):
            return True
    return False


def fast_subset(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Manifest entries flagged ``fast: true`` (the minimum slice a
    triggered gate run must verify)."""
    entries = manifest.get("entries") or []
    return [e for e in entries if e.get("fast") is True]


def _metric_passes(row: Dict[str, Any], metric: str) -> Tuple[bool, bool]:
    """(attempted, passed) for one row against one metric."""
    build_ok = bool(row.get("build_ok"))
    if metric == "build":
        return True, build_ok
    if not build_ok:
        return False, False  # run/parity only count once a build exists
    if metric == "run":
        return True, bool(row.get("run_ok"))
    if metric == "parity":
        score = row.get("score")
        verdict = row.get("verdict")
        if score is None and not verdict:
            return False, False
        return True, str(verdict or "") in _PARITY_PASS_VERDICTS
    raise ValueError(f"unknown metric {metric!r}")


def evaluate(
    rows: List[Dict[str, Any]],
    bars: Dict[str, float],
    manifest: Optional[Dict[str, Any]] = None,
) -> GateVerdict:
    """Aggregate rows per (accelerator, class) and compare each metric
    rate (passes/attempted) to its bar (inclusive: rate == bar passes).

    ``manifest`` supplies the per-entry ``class``; entries without a
    manifest class fall back to ``"unknown"``."""
    if not isinstance(bars, dict) or not bars:
        raise ValueError("bars must be a non-empty dict of metric -> rate")

    classes: Dict[str, str] = {}
    if manifest:
        for e in manifest.get("entries") or []:
            # The shipped manifest uses ``node_kind``; synthetic test
            # manifests may use ``node_key`` — accept either.
            nk = e.get("node_key", e.get("node_kind"))
            cls = str(e.get("class") or "unknown")
            if nk is not None:
                classes[str(nk)] = cls

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in rows:
        accel = str(row.get("accelerator") or "unknown")
        node_key = str(row.get("node_key") or "")
        cls = classes.get(node_key, "unknown")
        groups.setdefault((accel, cls), []).append(row)

    failing: List[Dict[str, Any]] = []
    for (accel, cls), group in sorted(groups.items()):
        for metric in sorted(bars):
            bar = bars[metric]
            attempted = 0
            passed = 0
            for row in group:
                att, ok = _metric_passes(row, metric)
                if att:
                    attempted += 1
                    if ok:
                        passed += 1
            if attempted == 0:
                continue
            rate = passed / attempted
            if rate < bar:
                failing.append({
                    "accelerator": accel,
                    "class": cls,
                    "metric": metric,
                    "rate": round(rate, 6),
                    "bar": bar,
                })
    return GateVerdict(passed=not failing, failing=failing)


def _sanity_check_bars(bars: Any) -> Optional[str]:
    """Why `bars` is unusable (None = usable)."""
    if not isinstance(bars, dict) or not bars:
        return "must be a non-empty dict"
    for metric, v in bars.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"metric {metric!r} must be a number, got {v!r}"
        if not (0 <= float(v) <= 1):
            return f"metric {metric!r} must be within [0, 1], got {v!r}"
    return None


def _default_bars() -> Dict[str, float]:
    return dict(DEFAULT_BARS)


def _coerce_bars(raw: Any) -> Dict[str, float]:
    """Parse a str/dict bars value; malformed input falls back to the
    defaults (a broken bar must not make the gate unrunnable)."""
    import logging
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            logging.getLogger(__name__).warning(
                "corpus gate: bars %r is not valid JSON — using defaults %s",
                raw, DEFAULT_BARS,
            )
            return _default_bars()
    if isinstance(raw, dict) and raw:
        cleaned = {
            str(k): float(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if cleaned:
            return cleaned
    return _default_bars()


def get_bars(db: Optional[Any] = None) -> Dict[str, float]:
    """Resolve the bars from the ``node_build.corpus_gate_bars`` platform
    setting (DB > default; the registry type is str, no env fallback).
    Never raises for a bad stored value."""
    from services import platform_settings
    try:
        raw = platform_settings.get_setting(BARS_SETTING_KEY, db=db)
    except Exception:  # setting missing (not yet registered) or DB down
        return _default_bars()
    return _coerce_bars(raw)


def register_gate_setting() -> None:
    """Register the bars platform setting (idempotent, like
    ``corpus_harness.register_harness_setting``)."""
    from services import platform_settings
    platform_settings.register(
        BARS_SETTING_KEY,
        str,
        json.dumps(DEFAULT_BARS, sort_keys=True),
        group="node_build",
        description="GO.8 corpus regression gate bars: JSON object of "
                    "metric -> minimum passes/attempted rate (build/run/"
                    "parity), evaluated per (accelerator, class) group. "
                    "Inclusive — a group exactly at its bar is green.",
    )


def register_weekly_settings() -> None:
    """Register the weekly-run platform settings (idempotent, same
    registry as ``register_gate_setting`` — no env vars): the enable and
    release-block flags plus the attempt bookkeeping (last attempt key
    enqueued, last attempt key evaluated)."""
    from services import platform_settings
    platform_settings.register(
        WEEKLY_SETTING_KEY,
        bool,
        False,
        group="node_build",
        description="GO.8 corpus regression gate WEEKLY run: when true the "
                    "APScheduler job enqueues the manifest's fast subset "
                    "through the corpus harness once a week (off-peak, "
                    "Sunday 03:17 UTC) and an every-6h job evaluates the "
                    "latest attempt against the bars. Off by default.",
    )
    platform_settings.register(
        BLOCKED_SETTING_KEY,
        bool,
        False,
        group="node_build",
        description="GO.8 corpus regression gate release block: flipped to "
                    "true when the weekly evaluation breaches a bar (an "
                    "audit event with the failing metrics is written at the "
                    "same time). NEVER auto-cleared — an operator clears it "
                    "deliberately once the corpus is green again.",
    )
    platform_settings.register(
        LAST_ATTEMPT_KEY,
        str,
        "",
        group="node_build",
        description="GO.8 corpus regression gate weekly run bookkeeping: "
                    "the attempt key (corpus-weekly-<YYYYMMDD> UTC) the "
                    "last weekly enqueue used. The evaluation job "
                    "evaluates THIS attempt, so a run enqueued Sunday "
                    "03:17 that finishes after midnight UTC is still "
                    "evaluated. Empty = no enqueue has recorded one (the "
                    "evaluator falls back to the current UTC day's key).",
    )
    platform_settings.register(
        LAST_EVALUATED_KEY,
        str,
        "",
        group="node_build",
        description="GO.8 corpus regression gate weekly run bookkeeping: "
                    "the attempt key already evaluated against the bars "
                    "(pass or breach). Re-evaluating the same attempt is "
                    "a no-op. Empty = nothing evaluated yet.",
    )


# ---------------------------------------------------------------------------
# Weekly run — enqueue + evaluate (the APScheduler jobs call these)
# ---------------------------------------------------------------------------

def _attempt_key(when: Optional[datetime] = None) -> str:
    """Stable weekly attempt key (per UTC day) so the weekly enqueue is
    idempotent within the same window — ``plan_cells(skip_done=True)`` then
    never re-plans a cell that already has a verification row for the key."""
    when = when or datetime.now(timezone.utc)
    return "corpus-weekly-" + when.strftime("%Y%m%d")


def _weekly_enabled(db) -> bool:
    from services import platform_settings
    try:
        return bool(platform_settings.get_setting(WEEKLY_SETTING_KEY, db=db))
    except Exception:  # setting not yet registered / DB down -> off
        return False


def enqueue_weekly(db) -> Dict[str, Any]:
    """Enqueue the manifest's fast subset for the weekly run.

    Gated by ``node_build.corpus_gate_weekly_enabled`` (returns immediately
    when off). Reuses the harness's enqueue path (``run_cells`` over
    ``plan_cells`` of the fast subset — no duplication of the build/DAG
    dispatch). Returns a small summary; ``enqueued`` is the number of cells
    actually dispatched (already-verified cells are skipped by ``plan_cells``
    so a same-day re-run enqueues nothing new). The attempt key used is
    recorded in ``node_build.corpus_gate_weekly_last_attempt`` so the
    evaluation job still finds the attempt after midnight UTC.
    """
    from services import platform_settings
    from node_builder.variant_test import corpus_harness as ch

    if not _weekly_enabled(db):
        _WEEKLY_LOGGER.info(
            "corpus gate weekly: %s is off — not enqueuing", WEEKLY_SETTING_KEY)
        return {"enqueued": 0, "attempt_key": _attempt_key(), "enabled": False}

    ch.register_harness_setting()
    manifest = ch.load_manifest()
    ch.validate_manifest(manifest)
    entries = fast_subset(manifest)
    attempt_key = _attempt_key()

    cells = ch.plan_cells(db, entries, attempt_key=attempt_key, skip_done=True)
    enqueued = ch.run_cells(db, cells, attempt_key=attempt_key)

    # Record which attempt the evaluation job must look for: the cells
    # take hours, and a run that finishes after midnight UTC must still
    # be evaluated under THIS key, not the next day's.
    try:
        platform_settings.set_setting(
            LAST_ATTEMPT_KEY, attempt_key, db=db, actor_user_id=None)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        _WEEKLY_LOGGER.error(
            "corpus gate weekly: recording %s FAILED: %s",
            LAST_ATTEMPT_KEY, exc)
        raise

    _WEEKLY_LOGGER.info(
        "corpus gate weekly: enqueued %d fast-subset cell(s) (attempt_key=%s)",
        len(enqueued), attempt_key)
    return {"enqueued": len(enqueued), "attempt_key": attempt_key, "enabled": True,
            "records": enqueued}


def _planned_cells(db, manifest: Dict[str, Any],
                   attempt_key: str) -> List[Dict[str, str]]:
    """(node_key, accelerator) pairs the weekly run was supposed to produce
    a verification row for — the fast subset x every non-skipped accelerator
    (probe-independent, mirroring ``plan_cells`` minus hardware availability).
    Used to decide when the attempt is TERMINAL: every one of these pairs
    must have a verification row."""
    from node_builder.variant_test import corpus_harness as ch

    probe = None
    try:
        probe = ch.probe_hardware(db)
    except Exception:  # noqa: BLE001 — probe is best-effort for completeness
        probe = None
    online = ch.online_kinds(db)
    planned = []
    for e in fast_subset(manifest):
        nk = e.get("node_kind")
        expected = e.get("expected") or {}
        for accel in ch.ACCELERATORS:
            if accel not in online:
                continue
            exp = expected.get(accel)
            if exp in ("incompat", "na", ch.UNKNOWN_HW):
                continue
            if probe is not None:
                exp = ch.relabel_spec_cells(expected, probe).get(accel)
                if exp in ("incompat", "na", ch.UNKNOWN_HW):
                    continue
            # id/comparer ride along so these can feed collect_results
            # directly (it reads cell["id"]/cell["comparer"] per row).
            planned.append({"id": str(e.get("id")), "node_key": nk,
                            "accelerator": accel,
                            "comparer": e.get("comparer")})
    return planned


def _attempt_is_terminal(db, manifest: Dict[str, Any], attempt_key: str) -> bool:
    """True once every planned cell for the attempt has a verification row
    (i.e. all cells are terminal — built/scored or failed). An attempt with
    no planned cells is trivially terminal."""
    from node_builder.variant_test import corpus_harness as ch
    planned = _planned_cells(db, manifest, attempt_key)
    if not planned:
        return True
    done = ch.existing_attempt_cells(db, attempt_key)
    return all((str(p["node_key"]), str(p["accelerator"]).lower()) in done
               for p in planned)


def _last_attempt_key(db) -> str:
    """The attempt key the evaluation job must target: the one the last
    weekly enqueue recorded in ``node_build.corpus_gate_weekly_last_attempt``
    (a run enqueued Sunday 03:17 can finish after midnight UTC, so the
    CURRENT day's key would miss it), falling back to the current UTC day's
    key when no enqueue has recorded one yet."""
    from services import platform_settings
    try:
        recorded = str(platform_settings.get_setting(LAST_ATTEMPT_KEY, db=db)
                       or "").strip()
    except Exception:  # noqa: BLE001 — not registered / DB down
        recorded = ""
    return recorded or _attempt_key()


def _mark_attempt_evaluated(db, attempt_key: str) -> None:
    """Record that ``attempt_key`` has been evaluated (pass or breach) so
    the every-6h job is a no-op for it until the next enqueue."""
    from services import platform_settings
    try:
        platform_settings.set_setting(
            LAST_EVALUATED_KEY, attempt_key, db=db, actor_user_id=None)
    except Exception as exc:  # noqa: BLE001 — bookkeeping is best-effort
        _WEEKLY_LOGGER.warning(
            "corpus gate weekly: recording %s FAILED: %s",
            LAST_EVALUATED_KEY, exc)
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        pass


def evaluate_weekly(db) -> Dict[str, Any]:
    """Evaluate the LATEST weekly attempt against the bars.

    The latest attempt is the one the last ``enqueue_weekly`` recorded in
    ``node_build.corpus_gate_weekly_last_attempt`` (falling back to the
    current UTC day's key when nothing is recorded) — so a run that
    finishes after midnight UTC is still evaluated under its own key.
    Each attempt is evaluated at most once; once evaluated (pass or
    breach) the attempt key is recorded in
    ``node_build.corpus_gate_weekly_last_evaluated`` and further calls are
    a no-op (``reason: already_evaluated``) until the next enqueue.

    No-op (nothing to evaluate) until the attempt's cells are all
    terminal. On a bar breach it appends ONE ``WIZARD_RELEASE_BLOCKED``
    ``audit_events`` row with the failing metrics and sets
    ``node_build.wizard_release_blocked`` to true (never auto-cleared).
    On a green gate it does NOT touch the flag.
    """
    from services import platform_settings
    from node_builder.variant_test import corpus_harness as ch

    register_weekly_settings()
    ch.register_harness_setting()
    manifest = ch.load_manifest()
    ch.validate_manifest(manifest)
    attempt_key = _last_attempt_key(db)

    try:
        last_evaluated = str(platform_settings.get_setting(
            LAST_EVALUATED_KEY, db=db) or "").strip()
    except Exception:  # noqa: BLE001
        last_evaluated = ""
    if last_evaluated == attempt_key:
        _WEEKLY_LOGGER.info(
            "corpus gate weekly: attempt %s already evaluated — skipping",
            attempt_key)
        return {"evaluated": False, "reason": "already_evaluated",
                "attempt_key": attempt_key, "breached": False}

    if not _attempt_is_terminal(db, manifest, attempt_key):
        _WEEKLY_LOGGER.info(
            "corpus gate weekly: attempt %s not terminal yet — skipping "
            "evaluation", attempt_key)
        return {"evaluated": False, "attempt_key": attempt_key,
                "breached": False}

    cells = _planned_cells(db, manifest, attempt_key)
    rows = ch.collect_results(db, cells, attempt_key=attempt_key)
    bars = get_bars(db=db)
    verdict = evaluate(rows, bars, manifest)

    if verdict.passed:
        _WEEKLY_LOGGER.info(
            "corpus gate weekly: attempt %s PASSED all bars (attempt_key=%s)",
            attempt_key, attempt_key)
        _mark_attempt_evaluated(db, attempt_key)
        return {"evaluated": True, "attempt_key": attempt_key,
                "breached": False, "passed": True}

    failing = verdict.failing
    # One event per breach: if a previous evaluation already blocked the
    # release for this attempt, do not re-write the event or re-toggle the
    # flag (the flag is never auto-cleared — and never re-set either).
    try:
        already_blocked = bool(platform_settings.get_setting(
            BLOCKED_SETTING_KEY, db=db))
    except Exception:  # noqa: BLE001 — treat as not-blocked
        already_blocked = False
    if already_blocked:
        _WEEKLY_LOGGER.warning(
            "corpus gate weekly: attempt %s still breaching bars — "
            "already blocked, no new event: %s", attempt_key, failing)
        _mark_attempt_evaluated(db, attempt_key)
        return {"evaluated": True, "attempt_key": attempt_key,
                "breached": True, "passed": False, "failing": failing,
                "already_blocked": True}

    # One audit event (system-initiated, platform-scoped) with the metrics.
    try:
        import models
        db.add(models.AuditEvent(
            tenant_id=None,
            user_id=None,
            event_name="WIZARD_RELEASE_BLOCKED",
            details={
                "attempt_key": attempt_key,
                "reason": "corpus_gate_weekly_bar_breach",
                "failing": failing,
                "bars": bars,
            },
        ))
    except Exception as exc:  # noqa: BLE001 — audit is best-effort, keep blocking
        _WEEKLY_LOGGER.warning(
            "corpus gate weekly: audit append failed: %s", exc)

    # Flip the release-block flag on (never auto-cleared).
    try:
        platform_settings.set_setting(
            BLOCKED_SETTING_KEY, True, db=db, actor_user_id=None)
    except Exception as exc:  # noqa: BLE001
        _WEEKLY_LOGGER.error(
            "corpus gate weekly: setting %s to true FAILED: %s",
            BLOCKED_SETTING_KEY, exc)
        raise
    db.commit()
    _mark_attempt_evaluated(db, attempt_key)

    _WEEKLY_LOGGER.warning(
        "corpus gate weekly: attempt %s BREACHED bars -> release blocked: %s",
        attempt_key, failing)
    return {"evaluated": True, "attempt_key": attempt_key,
            "breached": True, "passed": False, "failing": failing}


def load_changed_paths(source: str) -> List[str]:
    """Read a newline-separated changed-path list (or "-" = stdin)."""
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, "r", encoding="utf-8") as f:
            text = f.read()
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m node_builder.variant_test.corpus_gate",
        description="Golden-corpus regression gate. Exits 0 when the gate "
                    "is not required or passes; 1 when a trigger path "
                    "changed and a (accelerator, class, metric) cell is "
                    "below its bar.",
    )
    parser.add_argument("--changed-files", required=True,
                        help="newline-separated file of changed paths "
                             "(or '-' for stdin)")
    parser.add_argument("--results", default=None,
                        help="JSON file of corpus result rows (list of "
                             "corpus_harness.collect_results rows, or "
                             "{'rows': [...], 'manifest': {...}})")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    register_gate_setting()

    changed = load_changed_paths(args.changed_files)
    if not needs_gate(changed):
        print("gate not required")
        return 0

    if not args.results:
        print("gate required but no --results given — cannot evaluate")
        return 1

    payload = _load_json(args.results)
    if isinstance(payload, dict):
        rows = payload.get("rows") or []
        manifest = payload.get("manifest")
    else:
        rows, manifest = payload, None
    if not isinstance(rows, list):
        print("gate required but results file has no row list")
        return 1

    bars = get_bars()
    verdict = evaluate(rows, bars, manifest)
    if verdict.passed:
        print("gate passed")
        return 0
    print("gate FAILED — failing cells (rate < bar):")
    for cell in verdict.failing:
        print(f"  accelerator={cell['accelerator']} class={cell['class']} "
              f"metric={cell['metric']} rate={cell['rate']} < bar={cell['bar']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
