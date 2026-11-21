#!/usr/bin/env python3
"""GS.PSR.5 — installer-facing CLI for the platform-settings seed.

Applies (or previews) the declarative initial values in
`services.platform_settings_seed.SEED_VALUES` for one deployment mode. This
is what an on-prem installer runs instead of hand-writing behavioural
tunables into `.env.public`; `start-main-server.sh` calls this in `--commit`
mode near the end of a fresh install (existing installs are unaffected on
upgrade — a key already carrying a value, whether from an admin edit or a
prior `.env.public` env_fallback, is never touched, since `set_setting`
inside the seed only writes when NO live DB row exists).

Usage (from `CORE/API`, or `/app` inside the container):
    python3 -m tools.seed_platform_settings --dry-run  --mode onprem
    python3 -m tools.seed_platform_settings --commit   --mode saas

`--dry-run` never opens a write transaction; `--commit` commits once at the
end (or not at all, on any error — no partial commit).
"""
from __future__ import annotations

import argparse
import sys


def _print_dry_run(rows, mode: str) -> None:
    seeded = [r for r in rows if r["would_apply"]]
    print(f"platform-settings dry run (mode={mode}) — "
          f"{len(rows)} registered settings, {len(seeded)} would be seeded")
    print("")
    for row in rows:
        marker = "WOULD SEED" if row["would_apply"] else "no change "
        line = (f"  [{marker}] {row['key']:<40} "
                f"current={row['current_value']!r} ({row['current_source']})")
        if row["seed_value"] is not None:
            line += f"  seed-for-{mode}={row['seed_value']!r}"
        print(line)


def _print_commit(results, mode: str) -> None:
    applied = [r for r in results if r["applied"]]
    skipped = [r for r in results if not r["applied"]]
    print(f"platform-settings seed (mode={mode}) — "
          f"applied {len(applied)}, skipped {len(skipped)}")
    for r in applied:
        print(f"  SEEDED  {r['key']} = {r['value']!r}")
    for r in skipped:
        print(f"  skip    {r['key']} ({r['reason']})")


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("saas", "onprem"), required=True,
                        help="Deployment mode to seed for.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true",
                        help="List every registered setting and what the seed "
                             "would do. No writes, no transaction.")
    action.add_argument("--commit", action="store_true",
                        help="Apply the seed and commit.")
    args = parser.parse_args()

    from database import SessionLocal
    from services.platform_settings_seed import (
        preview_seed_platform_settings, seed_platform_settings,
    )

    db = SessionLocal()
    try:
        if args.dry_run:
            rows = preview_seed_platform_settings(db, args.mode)
            _print_dry_run(rows, args.mode)
            return 0

        try:
            results = seed_platform_settings(db, args.mode)
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(f"seed-platform-settings: FAILED — {e}", file=sys.stderr)
            return 1
        _print_commit(results, args.mode)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(_cli())
