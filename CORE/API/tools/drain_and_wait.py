#!/usr/bin/env python3
"""drain-and-wait CLI.

The lead runs this INSIDE the API container (it uses the API's own
database session factory, never a test fixture):

    docker exec my_fastapi_app python3 -m tools.drain_and_wait --timeout 1800

It pauses dispatch (sets the `dispatch.paused` platform setting through the
same settings service the admin UI uses), then polls the in-flight run/task
counts every 10s, printing progress, and exits 0 the moment the platform is
`ready_for_restart` (paused AND both counts 0). If the counts do not reach 0
within `--timeout` seconds it prints the final state and exits 1 (the operator
can then override / force the restart).

    docker exec my_fastapi_app python3 -m tools.drain_and_wait --resume

Un-pauses dispatch (sets `dispatch.paused` = False).

Exit codes:
    0  drain reached ready_for_restart (or --resume succeeded)
    1  timed out waiting for the platform to drain
    2  a settings/DB error
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# Make `services` / `database` importable when run as `python3 -m tools.drain_and_wait`
# from /app (= CORE/API) — the container's CWD; harmless if already on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from services import platform_settings as psr  # noqa: E402
from routers.system_drain import _build_status  # noqa: E402

_POLL_S = 10.0


def _set(paused: bool) -> bool:
    db = SessionLocal()
    try:
        psr.set_setting("dispatch.paused", paused, db=db, actor_user_id=None)
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: failed to set dispatch.paused={paused}: {e}")
        return False
    finally:
        db.close()


def _resume() -> int:
    if not _set(False):
        return 2
    print("dispatch resumed (dispatch.paused=False)")
    return 0


def _drain(timeout_s: int) -> int:
    if not _set(True):
        return 2
    print("dispatch paused (dispatch.paused=True); draining in-flight work…")
    deadline = time.monotonic() + timeout_s
    while True:
        db = SessionLocal()
        try:
            status = _build_status(db)
        finally:
            db.close()
        runs = status["in_flight_runs"]
        tasks = status["in_flight_tasks"]
        stale = status["stale_runs_excluded"]
        print(f"  in_flight_runs={runs} in_flight_tasks={tasks} "
              f"stale_excluded={stale} ready={status['ready_for_restart']}")
        if status["ready_for_restart"]:
            print("READY_FOR_RESTART — safe to restart the API now.")
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"TIMEOUT after {timeout_s}s waiting to drain — "
                  f"still {runs} run(s) / {tasks} task(s) in flight. "
                  f"Operator may force the restart.")
            return 1
        time.sleep(min(_POLL_S, remaining))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--timeout", type=int, default=1800,
                   help="seconds to wait for in-flight work to drain "
                        "(default 1800)")
    p.add_argument("--resume", action="store_true",
                   help="unpause dispatch instead of draining")
    args = p.parse_args(argv)
    if args.resume:
        return _resume()
    return _drain(args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
