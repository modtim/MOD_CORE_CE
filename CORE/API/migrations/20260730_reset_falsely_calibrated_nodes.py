"""Reset node definitions that were marked calibrated without ever being measured.

`_sweep_completed_nodes()` used to treat FAILED as terminal-equivalent to
COMPLETED, so a node whose entire 84-task matrix failed was swept and flipped to
`calibration_status='calibrated'`. Measured on the dev stack 2026-07-29: 46 nodes
were marked calibrated, and **41 of them had zero successful calibration tasks**.
The only 5 that genuinely calibrated (84/84) are those needing no source file.

That is not merely a wrong label. `_pick_next_node()` selects only
`uncalibrated`/`stale`, so a falsely-calibrated node is **permanently excluded
from every future calibration plan** and can never self-heal.

A second population: nodes left in `calibration_status='calibrating'` whose tasks
have all reached a terminal state — stranded by a cancelled or interrupted plan,
and equally invisible to `_pick_next_node()`. 15 were stranded this way.

Both are reset to `uncalibrated` so a future plan picks them up.

Matches on **measured emptiness**, not a hardcoded id list, so it stays correct if
the population shifts before it runs. A node calibrated legitimately in the past
whose task rows have since been pruned is NOT touched — it must have task rows
AND zero successes among them to qualify.

Dry-run by default. Pass --commit to apply.

    docker exec -w /app my_fastapi_app python \\
        migrations/20260730_reset_falsely_calibrated_nodes.py
    docker exec -w /app my_fastapi_app python \\
        migrations/20260730_reset_falsely_calibrated_nodes.py --commit
"""
from __future__ import annotations

import argparse
import os
import sys


def _get_session():
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    import database as _db  # type: ignore
    _db.initialize()
    return _db.SessionLocal()


def _falsely_calibrated(session, models):
    """Nodes marked 'calibrated' that have calibration task rows and zero
    COMPLETED among them."""
    ApiTaskQueue = models.ApiTaskQueue
    ApiTaskStatus = models.ApiTaskStatus
    NodeDefinition = models.NodeDefinition

    out = []
    for node in session.query(NodeDefinition).filter(
        NodeDefinition.calibration_status == "calibrated",
    ).all():
        rows = session.query(ApiTaskQueue.status).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node.id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ).all()
        if not rows:
            continue  # no evidence either way — leave it alone
        if any(s == ApiTaskStatus.COMPLETED for (s,) in rows):
            continue  # genuinely measured something
        out.append((node, len(rows)))
    return out


def _stranded_calibrating(session, models):
    """Nodes stuck in 'calibrating' with no non-terminal calibration tasks."""
    ApiTaskQueue = models.ApiTaskQueue
    ApiTaskStatus = models.ApiTaskStatus
    NodeDefinition = models.NodeDefinition
    terminal = (ApiTaskStatus.COMPLETED, ApiTaskStatus.FAILED, ApiTaskStatus.CANCELLED)

    out = []
    for node in session.query(NodeDefinition).filter(
        NodeDefinition.calibration_status == "calibrating",
    ).all():
        rows = session.query(ApiTaskQueue.status).filter(
            ApiTaskQueue.task_payload["node_definition_id"].as_string() == str(node.id),
            ApiTaskQueue.task_payload["calibration_run"].as_string() == "true",
        ).all()
        if any(s not in terminal for (s,) in rows):
            continue  # still live
        out.append((node, len(rows)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true",
                    help="apply the reset (default is dry-run)")
    args = ap.parse_args()

    session = _get_session()
    import models  # noqa: F401

    try:
        false_cal = _falsely_calibrated(session, models)
        stranded = _stranded_calibrating(session, models)

        print(f"falsely 'calibrated' (0 successes, has task rows): {len(false_cal)}")
        for node, n in false_cal[:10]:
            print(f"    id={node.id:<5} {node.name[:44]:<44} 0/{n} succeeded")
        if len(false_cal) > 10:
            print(f"    ... and {len(false_cal) - 10} more")

        print(f"stranded in 'calibrating' (no live tasks): {len(stranded)}")
        for node, n in stranded[:10]:
            print(f"    id={node.id:<5} {node.name[:44]:<44} {n} terminal task(s)")
        if len(stranded) > 10:
            print(f"    ... and {len(stranded) - 10} more")

        total = len(false_cal) + len(stranded)
        if total == 0:
            print("nothing to reset.")
            return 0

        if not args.commit:
            print(f"\n(dry-run) would reset {total} node(s) to 'uncalibrated'. "
                  f"Re-run with --commit to apply.")
            return 0

        for node, _ in false_cal + stranded:
            node.calibration_status = "uncalibrated"
        session.commit()
        print(f"\nreset {total} node(s) to 'uncalibrated'.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
