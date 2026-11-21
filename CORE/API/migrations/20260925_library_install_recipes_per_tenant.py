"""GB.LEAK.3 — per-tenant library install recipes (library_install_recipes)

Cross-tenant leak fix. `library_profiles.install_recipe_by_accel` held the
RUN/COPY block of ONE tenant's working Dockerfile and
`successful_build_count_by_accel` counted that tenant's builds; both were
global (PK = library name), so every other tenant's repair-LLM prompt and
analyze-script difficulty prediction read them. This adds the per-tenant
replacement table, mirroring models.LibraryInstallRecipe:

    id, tenant_id (FK tenants, NOT NULL, indexed), library_name,
    accelerator, recipe, success_count (default 0),
    provenance ('captured' | 'backfill_attributed' | 'legacy_unattributed',
    default 'captured'), created_at, updated_at,
    deleted_at / deleted_by_user_id (SoftDeleteWithActorMixin)
    + partial UNIQUE (tenant_id, library_name, accelerator) WHERE deleted_at IS NULL

Index names match what Base.metadata.create_all emits for the ORM model, so a
fresh install (create_all) and a migrated install end up identical.

Purely additive: CREATE ... IF NOT EXISTS only. No DROP / TRUNCATE / DELETE /
UPDATE; the legacy JSON columns on library_profiles are left in place (they
simply stop being read — GB.LEAK.5).

GB.LEAK.4 — `--backfill` copies existing legacy recipes into this table.
For each library_profiles row with a non-empty install_recipe_by_accel and
each (accel, recipe) in it, the originating tenant(s) are recovered by
re-running node_builder.install_recipes.extract_install_block over every
non-deleted build_specs row (spec_json.hero_dockerfile and each
spec_json.variants[].dockerfile) and exact-matching the stored recipe:
  * matched   -> one row per tenant, provenance='backfill_attributed',
                 success_count = that tenant's matching-spec count
  * unmatched -> one row for tenant 1 (Platform),
                 provenance='legacy_unattributed',
                 success_count = the legacy global count (operator review)
Idempotent: a (tenant, library, accelerator) with an ACTIVE row is skipped.
INSERT-only into library_install_recipes; library_profiles is only read.

Idempotent; safe to re-run. Dry-run by default (prints the plan; with
--backfill it reads the DB but writes nothing); pass --commit to apply.

Run inside the API container:
    docker exec my_fastapi_app python3 \\
        migrations/20260925_library_install_recipes_per_tenant.py --commit
    docker exec my_fastapi_app python3 \\
        migrations/20260925_library_install_recipes_per_tenant.py --commit --backfill
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Iterable


STEPS: list[tuple[str, str]] = [
    (
        "create table library_install_recipes",
        """
CREATE TABLE IF NOT EXISTS library_install_recipes (
    id                 SERIAL PRIMARY KEY,
    tenant_id          INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    library_name       VARCHAR(255) NOT NULL,
    accelerator        VARCHAR(32) NOT NULL,
    recipe             TEXT NOT NULL,
    success_count      INTEGER NOT NULL DEFAULT 0,
    provenance         VARCHAR(32) NOT NULL DEFAULT 'captured',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ,
    deleted_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
""",
    ),
    (
        "index tenant_id",
        "CREATE INDEX IF NOT EXISTS ix_library_install_recipes_tenant_id "
        "ON library_install_recipes (tenant_id);",
    ),
    (
        "index deleted_at",
        "CREATE INDEX IF NOT EXISTS ix_library_install_recipes_deleted_at "
        "ON library_install_recipes (deleted_at);",
    ),
    (
        "partial unique (tenant_id, library_name, accelerator) where active",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_library_install_recipe_unique_active "
        "ON library_install_recipes (tenant_id, library_name, accelerator) "
        "WHERE deleted_at IS NULL;",
    ),
]


def _get_conn():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import database as _db  # type: ignore
    _db.initialize()
    eng = _db.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    raw = eng.raw_connection()
    dbapi = (
        getattr(raw, "driver_connection", None)
        or getattr(raw, "connection", None)
        or raw
    )
    dbapi.autocommit = True
    return dbapi


# ---------------------------------------------------------------- GB.LEAK.4
PLATFORM_TENANT_ID = 1


def _extract_install_block(dockerfile: str) -> str:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from node_builder.install_recipes import extract_install_block  # type: ignore
    return extract_install_block(dockerfile)


def _spec_blocks(spec_json: Any) -> set[str]:
    """Install blocks of every Dockerfile in one build spec."""
    if isinstance(spec_json, str):
        try:
            spec_json = json.loads(spec_json)
        except ValueError:
            return set()
    if not isinstance(spec_json, dict):
        return set()
    dockerfiles = [spec_json.get("hero_dockerfile")]
    variants = spec_json.get("variants") or []
    if isinstance(variants, list):
        dockerfiles += [v.get("dockerfile") for v in variants if isinstance(v, dict)]
    blocks: set[str] = set()
    for d in dockerfiles:
        if isinstance(d, str) and d:
            block = _extract_install_block(d)
            if block:
                blocks.add(block)
    return blocks


def attribute_recipe(recipe: str, specs: Iterable[tuple[int, Any]]) -> dict[int, int]:
    """Map tenant_id -> number of that tenant's specs whose install block
    equals `recipe` exactly. `specs` is an iterable of (tenant_id, spec_json).
    A spec counts once even if several of its Dockerfiles match."""
    out: dict[int, int] = {}
    if not recipe:
        return out
    for tenant_id, spec_json in specs:
        blocks = _spec_blocks(spec_json)
        if recipe in blocks:
            out[tenant_id] = out.get(tenant_id, 0) + 1
    return out


def plan_backfill(
    profiles: Iterable[tuple[str, Any, Any]],
    specs: list[tuple[int, Any]],
    active: set[tuple[int, str, str]],
) -> tuple[list[dict[str, Any]], int]:
    """Pure planner. `profiles` = (name, install_recipe_by_accel,
    successful_build_count_by_accel). Returns (rows_to_insert, skipped)."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    seen = set(active)
    for lib_name, recipes, counts in profiles:
        if not isinstance(recipes, dict):
            continue
        counts = counts if isinstance(counts, dict) else {}
        for accel, recipe in recipes.items():
            if not isinstance(recipe, str) or not recipe.strip():
                continue
            accel_key = str(accel).lower().strip()
            matched = attribute_recipe(recipe, specs)
            if matched:
                targets = [(t, n, "backfill_attributed") for t, n in sorted(matched.items())]
            else:
                try:
                    n = int(counts.get(accel) or counts.get(accel_key) or 0)
                except (TypeError, ValueError):
                    n = 0
                targets = [(PLATFORM_TENANT_ID, n, "legacy_unattributed")]
            for tenant_id, n, provenance in targets:
                key = (tenant_id, lib_name, accel_key)
                if key in seen:
                    skipped += 1
                    continue
                seen.add(key)
                rows.append({
                    "tenant_id": tenant_id, "library_name": lib_name,
                    "accelerator": accel_key, "recipe": recipe,
                    "success_count": max(0, n), "provenance": provenance,
                })
    return rows, skipped


def backfill(db_session, dry_run: bool = True) -> dict[str, Any]:
    """GB.LEAK.4 backfill legacy recipes from library_profiles into
    library_install_recipes. Returns a summary dict with keys:
      'profiles', 'specs', 'rows' (planned), 'by_provenance', 'skipped',
      'inserted' (0 when dry_run).

    Idempotent: skips any (tenant_id, library_name, accelerator) that
    already has an ACTIVE row in library_install_recipes. Only INSERTs;
    library_profiles is only read.
    """
    from sqlalchemy import text

    summary: dict[str, Any] = {
        "profiles": 0, "specs": 0, "by_provenance": {},
        "skipped": 0, "inserted": 0, "rows": [],
    }
    # Read-only scans. JSON column: filter empties in Python (json vs jsonb safe).
    profiles = [
        (r[0], r[1], r[2])
        for r in db_session.execute(text(
            "SELECT name, install_recipe_by_accel, successful_build_count_by_accel "
            "FROM library_profiles WHERE install_recipe_by_accel IS NOT NULL "
            "ORDER BY name"
        )).all()
        if isinstance(r[1], dict) and r[1]
    ]
    summary["profiles"] = len(profiles)
    if not profiles:
        return summary

    specs = [
        (r[0], r[1])
        for r in db_session.execute(text(
            "SELECT tenant_id, spec_json FROM build_specs "
            "WHERE deleted_at IS NULL ORDER BY id"
        )).all()
    ]
    summary["specs"] = len(specs)
    active = {
        (r[0], r[1], r[2])
        for r in db_session.execute(text(
            "SELECT tenant_id, library_name, accelerator "
            "FROM library_install_recipes WHERE deleted_at IS NULL"
        )).all()
    }

    rows, skipped = plan_backfill(profiles, specs, active)
    summary["skipped"] = skipped
    summary["rows"] = rows
    for r in rows:
        p = r["provenance"]
        summary["by_provenance"][p] = summary["by_provenance"].get(p, 0) + 1

    if dry_run or not rows:
        return summary

    # INSERT-only; NOT EXISTS re-checks the active slot so a concurrent
    # capture can't trip the partial unique index.
    ins = text(
        "INSERT INTO library_install_recipes "
        "(tenant_id, library_name, accelerator, recipe, success_count, provenance) "
        "SELECT :tenant_id, :library_name, :accelerator, :recipe, :success_count, :provenance "
        "WHERE NOT EXISTS (SELECT 1 FROM library_install_recipes "
        "WHERE tenant_id = :tenant_id AND library_name = :library_name "
        "AND accelerator = :accelerator AND deleted_at IS NULL)"
    )
    for r in rows:
        res = db_session.execute(ins, r)
        if res.rowcount:
            summary["inserted"] += 1
        else:
            summary["skipped"] += 1
    db_session.commit()
    return summary


def main() -> int:
    p = argparse.ArgumentParser(description="GB.LEAK.3 library_install_recipes")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--backfill", action="store_true")
    args = p.parse_args()

    if args.backfill:
        return _run_backfill(commit=args.commit)

    if not args.commit:
        for label, stmt in STEPS:
            print(f"DRY    {label}")
            print(f"       {stmt.strip()}")
        print("\n(dry-run; pass --commit to apply)")
        return 0

    conn = _get_conn()
    cur = conn.cursor()
    for label, stmt in STEPS:
        print(f"WRITE  {label}")
        cur.execute(stmt)
    cur.close()
    print("\nlibrary_install_recipes ready (additive; no existing data touched).")
    return 0


def _run_backfill(*, commit: bool) -> int:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import database as _db  # type: ignore
    _db.initialize()
    session = _db.SessionLocal()
    try:
        s = backfill(session, dry_run=not commit)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    tag = "WRITE " if commit else "DRY   "
    for r in s["rows"]:
        print(f"{tag} tenant={r['tenant_id']} lib={r['library_name']} "
              f"accel={r['accelerator']} count={r['success_count']} "
              f"provenance={r['provenance']}")
    print(f"\nbackfill summary: profiles_with_recipes={s['profiles']} "
          f"build_specs_scanned={s['specs']} planned={len(s['rows'])} "
          f"inserted={s['inserted']} skipped_active={s['skipped']} "
          f"by_provenance={s['by_provenance']}")
    if s["by_provenance"].get("legacy_unattributed"):
        print("NOTE: legacy_unattributed rows were assigned to tenant 1 (Platform); "
              "review them.")
    print("library_profiles not modified." + (
        "" if commit else "\n(dry-run; pass --commit --backfill to apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
