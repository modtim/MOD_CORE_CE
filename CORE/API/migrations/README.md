# Database Migrations

## Apply Secret Unique Constraint Fix

To fix the issue where secrets with the same name cannot be re-added after deletion, run:

```bash
# Connect to your PostgreSQL database
docker exec -i <postgres-container-name> psql -U modTech -d modTech < /path/to/fix_secret_unique_constraint.sql

# Or if using docker-compose from the CORE directory:
docker-compose exec postgres-app psql -U modTech -d modTech < API/migrations/fix_secret_unique_constraint.sql
```

### What this fixes:

- **Before**: Deleting a secret and trying to create a new one with the same name would fail with a uniqueness constraint error
- **After**: Deleted secrets (soft-deleted with `deleted_at` set) no longer block reusing the same name

### How it works:

The migration replaces the standard UNIQUE constraint with a PostgreSQL partial unique index that only applies to rows where `deleted_at IS NULL`. This allows multiple soft-deleted records with the same name while still enforcing uniqueness for active secrets.

## mod-migrate

`tools/mod_migrate.py` is the one-shot installer/updater runner: it applies every
migration script in this directory that has not been recorded yet, then the
first-run/upgrade seeds, and records each successfully applied step in the
append-only `release_steps` table.

### Ordering

Steps are discovered as `*.py` files in this directory and run **in filename
(lexicographic) order** — that IS the intended order. The naming convention
`YYYYMMDD[_letter].slug.py` encodes it: date prefix orders by creation date,
an optional single-letter suffix (`…b`, `…e`, `…f`) disambiguates same-day
migrations. Never rename or reorder an applied file.

### Non-uniform scripts

The scripts are not a uniform framework. The runner classifies each file
individually:

- **Class A** — argparse with `--commit` (default = dry-run that prints SQL);
  the common style (e.g. `20261002e_app_user_types.py`). The runner passes
  `--commit` so a plain run applies.
- **Class B** — `--commit` plus extra flags with safe defaults
  (`--tenant-id`, `--backfill`, `--batch-size`); run with `--commit` only.
- **Class C** — no `--commit` at all; run as-is. (As of 2026-10-02 every
  `*.py` here supports `--commit`, but detection is per-file so a future
  Class-C file keeps working.)

### `release_steps` ledger

Each successfully applied step inserts one row — `step_name` (the file stem,
UNIQUE), `checksum` (sha256 of the file at apply time), `applied_at`,
`release_version` (nullable), `duration_ms`, `runner_host` — and commits
immediately, so a later failing step never loses earlier rows. A step already
recorded is skipped (no-op); if its file checksum changed since it was
recorded, it is **skipped with a warning** — a changed step file is never
silently re-run against a live install. The table is ISO append-only:
BEFORE UPDATE OR DELETE triggers RAISE (see
`20261002f_release_steps.py`). The run exits non-zero on the FIRST failing
step; steps after it do not run.

### `--baseline` — required for existing installs

Installs brought up by `start-main-server.sh` already have every migration
and seed applied but no `release_steps` history. On their first
`mod-migrate` run, record the history WITHOUT re-running anything:

```bash
docker compose --profile migrate run --rm mod-migrate --baseline \
    --release-version <current-release>
```

Fresh installs (or upgrades from a clean state) just run
`docker compose --profile migrate run --rm mod-migrate` — pending steps apply,
already-recorded steps are no-ops. `--dry-run` lists what would run and
records nothing.
