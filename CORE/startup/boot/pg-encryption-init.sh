#!/bin/bash
# GY.C6.s1 - postgres at-rest extension init, run by the STOCK postgres ENTRYPOINT, not
# instead of it. A compose bind-mounts this file read-only onto
# /docker-entrypoint-initdb.d/99-gy-c6s1-pg-encryption.sh (the stock postgres initdb
# drop-in dir), which docker-entrypoint.sh runs against its TEMP server AFTER booting it
# and BEFORE postgres ever accepts external connections - the ordering the GY-C6s1 boot
# proof asked for (the old custom entrypoint probed pg_isready/psql against an empty DB
# and aborted rc=2).
# Runs on the empty-volume init only; the CREATEd extensions persist in the DB.
# Fail-closed: set -e plus psql -v ON_ERROR_STOP=1; a failed pgcrypto CREATE aborts
# the boot (non-zero).
#
# pg_tle (version independent - no PG major is hard-coded here): the script asks the
# running server whether pg_tle is installed (pg_available_extensions) and preloaded
# (shared_preload_libraries; pg_tle cannot be CREATEd otherwise).
#   - MOD_REQUIRE_PG_TLE=1 (set by the CE compose, whose image is postgres-app, see
#     CORE/DockerContainers/postgres-app/Dockerfile): pg_tle missing OR not preloaded is
#     FATAL (stderr + exit 1), so a CE boot can never claim at-rest extension support it
#     lacks.
#   - otherwise (the paid/SaaS compose still runs STOCK postgres:13, which ships no
#     pg_tle): a warning, pgcrypto only, and the boot CONTINUES. That compose must never
#     fail its boot on this script.
# Marker presence only: this proves the extension CREATEs run against a live server;
# it is NOT proof that data is encrypted at rest.

# Source-safe (GY.C6.s1, 11-08): the stock postgres docker-entrypoint.sh runs an
# initdb.d *.sh with "$f" ONLY when it is executable, otherwise it SOURCES it (`. "$f"`)
# inside the ENTRYPOINT shell. git lands this blob at mode 100644 (core.fileMode=false
# strips the exec bit), so the SOURCED path is what runs on a fresh clone. Therefore the
# body below runs inside a subshell and there is NO top-level exit: a top-level `exit 0`
# would terminate the entrypoint shell mid-init (the temp server never stopped, boot
# aborted) and a top-level `set -e` would leak into the entrypoint shell. The subshell is
# the only construct that isolates BOTH, without changing the script's exit-status
# semantics: a failing CREATE still returns non-zero from the subshell, which the entrypoint's
# own `set -e` turns into an aborted boot (fail-closed is preserved).

(
set -e

echo "GY.C6.s1: pgcrypto extension init (/docker-entrypoint-initdb.d/99-gy-c6s1-pg-encryption.sh)"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"

# Ask the running (temp) server, not the filesystem: the extension dir depends on the PG
# major and pg_config is not in the runtime image.
PG_TLE_AVAILABLE="$(psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_tle'")"
PG_TLE_PRELOADED="$(psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT count(*) FROM unnest(string_to_array(replace(current_setting('shared_preload_libraries'), ' ', ''), ',')) AS lib WHERE lib = 'pg_tle'")"

if [ "$PG_TLE_AVAILABLE" = "1" ] && [ "$PG_TLE_PRELOADED" = "1" ]; then
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS pg_tle;"
elif [ "${MOD_REQUIRE_PG_TLE:-}" = "1" ]; then
  echo "FATAL: pg_tle unavailable (installed=$PG_TLE_AVAILABLE preloaded=$PG_TLE_PRELOADED) but MOD_REQUIRE_PG_TLE=1 - this image MUST ship pg_tle and preload it via shared_preload_libraries" >&2
  exit 1
else
  echo "GY.C6.s1: WARNING pg_tle not installed/preloaded (installed=$PG_TLE_AVAILABLE preloaded=$PG_TLE_PRELOADED) - KMS-wrapped pg_tle NOT installed (pgcrypto only); set MOD_REQUIRE_PG_TLE=1 to make this fatal"
fi
)
