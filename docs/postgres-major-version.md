# PostgreSQL major-version mismatch (data volume vs postgres image)

A PostgreSQL server cannot open a data directory written by a different major version
(`FATAL: database files are incompatible with server`). Recreating the postgres container
on such a volume leaves it in a restart loop and takes the database down.

The Community Edition database image is `postgres-app` (PostgreSQL **17**: the pinned
`postgres:17.11` base + `pg_tle`, built from `CORE/DockerContainers/postgres-app/Dockerfile`).
A data volume created by the earlier `postgres:17.11` image opens as is. An install whose
volume was initialised by an older major (for example PostgreSQL 13) must be migrated
before it can use this image.

`scripts/pg-version-guard.sh` checks this **read-only, before anything is changed**:

- it finds the postgres data volume (the mount on `my_postgres_db`, else
  `POSTGRES_DATA_PATH`, else the compose volume labelled `postgres_data`);
- it reads `PG_VERSION` from it (`docker run --rm --pull never --network none -v <vol>:/d:ro ...`,
  never writable, never pulls an image);
- it asks the target image for `postgres --version`;
- same major, or no/empty volume (fresh install) → pass; different major → **refuse**
  with `REFUSING: the postgres data volume <label> holds PG<n> data, but the target
  image <IMAGE> is PG<m>.` (exit 1; `scripts/pg-version-guard.sh:233`).

`scripts/install.sh` runs it for you. If you run compose by hand, run it yourself after `docker compose pull` and before `docker compose up -d`:

```bash
scripts/pg-version-guard.sh -f docker-compose.yml -p mod-core-ce
```

Exit codes: `0` pass · `1` refused (major mismatch) · `2` usage · `3` cannot decide
(image not present locally — pull/build first; non-empty directory without
`PG_VERSION`). `MOD_PG_GUARD_SKIP=1` bypasses the guard (emergency only, warns).

## When it refuses: dump, then restore into a fresh volume

Nothing has been changed yet. The old database is still running its **old** image.

1. **Dump while the old database is up** (use the old container, so the dump client
   matches the server):

   ```bash
   docker exec my_postgres_db sh -c 'pg_dumpall --globals-only -U "$POSTGRES_USER"' > pg-globals.sql
   docker exec my_postgres_db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' > pg-app.sql
   ls -l pg-globals.sql pg-app.sql      # non-empty, and keep them off this volume
   ```

   If the container was already recreated on the new image and is crash-looping, take
   the raw backup (step 2) first, then start a throwaway server of the **old** major
   on a copy of the volume and dump from that.
2. **Keep a raw backup of the old volume** until the restore is verified:
   `docker run --rm -v <vol>:/from:ro -v "$PWD":/to alpine tar czf /to/pgdata-backup.tgz -C /from .`
3. **Stop the stack without `-v`** (`docker compose down`), remove only the postgres
   data volume (`docker volume rm mod-core-ce_postgres_data`), start the postgres service
   alone (`docker compose up -d postgres-app`) — it initialises an empty cluster of the
   new major (and, for the CE image, runs `pg-encryption-init.sh`: pgcrypto + pg_tle).
4. **Restore**, then start everything:

   ```bash
   docker exec -i my_postgres_db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < pg-app.sql
   docker compose up -d
   ```

   Going to a *newer* major (PG13 → PG17) is the supported direction: restore a dump
   taken by the old server. Restoring a newer server's dump into an *older* one is a
   downgrade and can fail on statements the older server does not know; do not do it
   without reading the errors and verifying row counts.

Re-run the guard; it must print `OK: ... holds PG<n>, image ... is PG<n>` before you
`docker compose up -d` again.
