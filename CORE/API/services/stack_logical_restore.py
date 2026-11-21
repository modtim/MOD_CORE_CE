"""30.6.1 c1-J5a (Q-C1 = A) — the CE-present RESTORE half of logical stack backups.

Owner Q-C1 = A: CE gets exactly ONE logical restore, of an UPLOADED seed snapshot
(`source == "external"` + `seed_id`, written by the c1-J1 seed commit). Every other
logical op — taking a dump, restoring a platform-taken snapshot, backup schedules —
stays non-CE, so this module holds ONLY the restore-side primitives and the spec
builder; the dump-side command builders stay in `services/stack_logical_backup.py`
(the CE-denied/Paid module), which re-exports everything here so every existing
`_lb.<name>` caller and test keeps working. Like every engine builder here, the
restore argv NEVER carries a password — the secret rides the container env named
by `password_env`.
"""
from __future__ import annotations

import logging

from typing import Any, Dict, Optional, Tuple

from product_names import themed_logger
logger = themed_logger(__name__, 'stacks')


def _pg_connection(svc: dict) -> Dict[str, str]:
    env = svc.get("environment") or {}
    if not isinstance(env, dict):
        env = {}
    user = str(env.get("POSTGRES_USER") or "postgres")
    return {
        "user": user,
        "dbname": str(env.get("POSTGRES_DB") or user),
        "port": str(env.get("POSTGRES_PORT") or 5432),
    }


def _pg_restore_command(host: str, conn: Dict[str, str], in_path: str) -> list:
    return ["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges",
            "-h", host, "-p", conn["port"], "-U", conn["user"], "-d", conn["dbname"], in_path]


def _mysql_connection(svc: dict) -> Dict[str, str]:
    env = svc.get("environment") or {}
    if not isinstance(env, dict):
        env = {}
    # The mysql/mariadb template provisions a single `root` superuser via
    # MYSQL_ROOT_PASSWORD; there is no per-database user, so the dump always
    # connects as root (MYSQL_USER is honoured if a custom compose sets it).
    return {
        "user": str(env.get("MYSQL_USER") or "root"),
        "port": str(env.get("MYSQL_TCP_PORT") or 3306),
    }


def _mysql_restore_command(host: str, conn: Dict[str, str], in_path: str) -> list:
    # `mysql` has no "read this SQL file" flag (a positional is a DB name, so
    # the dump path must NOT be a trailing arg) — the executor feeds `path`
    # to `mysql`'s STDIN. Password via the `MYSQL_PWD` env, never argv.
    return ["mysql", "-h", host, f"-u{conn['user']}"]


def _mongo_connection(svc: dict) -> Dict[str, str]:
    env = svc.get("environment") or {}
    if not isinstance(env, dict):
        env = {}
    return {
        "user": str(env.get("MONGO_INITDB_ROOT_USERNAME") or "root"),
        "port": str(env.get("MONGO_PORT") or 27017),
    }


def _mongo_uri(host: str, conn: Dict[str, str]) -> str:
    # The password is deliberately NOT in this URI: mongodump/mongorestore
    # have no password env var of their own, and an in-URI secret would show
    # up on argv / in a log line. The secret is handed to the container only
    # through `password_env` (see the builder); the executor composes the
    # authenticated URI from that env value before exec, so the secret never
    # appears on a command line — the same "secret via env, never argv"
    # contract postgres uses with `PGPASSWORD`.
    return f"mongodb://{conn['user']}@{host}:{conn['port']}/"


def _mongo_restore_command(host: str, conn: Dict[str, str], in_path: str) -> list:
    # `--archive -i <file>` reads the archive from `path`; `--drop` replaces
    # each restored collection.
    return ["mongorestore", "--uri", _mongo_uri(host, conn), "--archive",
            "--drop", "-i", in_path]


# GA.ASW.5b: the dispatch/spec code below never branches on engine name
# directly, only through this table — that's how mysql/mariadb/mongo are
# added without touching the transport. Keys MUST match the image BASE name
# `find_engine_service` derives (last path segment before the `:`), so
# `mysql` and `mariadb` both resolve here and `mongo` resolves to MongoDB.
# c1-J5a: this is the RESTORE-only projection of that table — same keys, every
# field EXCEPT `dump_command` (the dump-side builders live in the Paid
# `services/stack_logical_backup.py`, which merges them back in).
_MYSQL_RESTORE_BUILDER: Dict[str, Any] = {
    "default_image": "mysql:8.4",
    "password_env": "MYSQL_PWD",               # container env the client reads
    "secret_key": "MYSQL_ROOT_PASSWORD",       # key under stack.secret_refs[svc]
    "connection": _mysql_connection,
    "restore_command": _mysql_restore_command,
    "dump_ext": "sql",
}

RESTORE_BUILDERS: Dict[str, Dict[str, Any]] = {
    "postgres": {
        "default_image": "postgres:16",
        "password_env": "PGPASSWORD",          # container env the client reads
        "secret_key": "POSTGRES_PASSWORD",      # key under stack.secret_refs[svc]
        "connection": _pg_connection,
        "restore_command": _pg_restore_command,
        "dump_ext": "dump",
    },
    # MariaDB speaks the MySQL wire protocol and its clients read MYSQL_PWD
    # too, so it shares the mysql builder (only the fallback image differs —
    # a detected service always carries its own image, so this is a dead
    # default kept for parity with postgres).
    "mysql": _MYSQL_RESTORE_BUILDER,
    "mariadb": {**_MYSQL_RESTORE_BUILDER, "default_image": "mariadb:11"},
    "mongo": {
        "default_image": "mongo:7.0",
        "password_env": "MONGODB_PWD",         # container env the executor composes the URI from
        "secret_key": "MONGO_INITDB_ROOT_PASSWORD",
        "connection": _mongo_connection,
        "restore_command": _mongo_restore_command,
        "dump_ext": "archive",
    },
}


def _project_name(stack: Any) -> str:
    # Mirrors routers/persistent_stacks/_shared.py:_project_name exactly. Kept
    # as a local one-liner (rather than imported) so this module has no
    # top-level dependency on the router package — that package imports this
    # module, and a top-level cycle would break at import time.
    refs = getattr(stack, "storage_refs", None) or {}
    return refs.get("project") or f"modstack{stack.id}g0"


def _network_name(project: str, svc: dict) -> str:
    nets = svc.get("networks")
    if isinstance(nets, dict) and nets:
        key = next(iter(nets))
    elif isinstance(nets, list) and nets:
        key = nets[0]
    else:
        key = "default"
    return f"{project}_{key}"


# GA.ASW.10.10 — image base -> canonical engine name for datastores that have
# a real STREAMING-replication story but no logical dump/restore one, so they
# can never be `ENGINE_BUILDERS` members (see `backup_ack_required`'s
# docstring: "there is no dump/restore builder for them to ever graduate out
# of 'none' into" — still true, unchanged by this). Gated by its OWN posture
# value, `x-mod.backup: streaming`, distinct from `logical` — this table is
# consulted ONLY for that posture, so it changes nothing about how a
# `logical`-posture service resolves, and `backup_ack_required`'s own
# `base in ENGINE_BUILDERS` gate stays untouched (redis/valkey never trip
# the "no backup plan" ack, same as before this constant existed). "valkey"
# maps to the same canonical "redis" engine name
# `services/stack_streaming_engines/redis.py` registers into
# `stack_streaming_migration.STREAMING_ENGINES` — the two speak the identical
# `REPLICAOF` replication protocol.
_STREAMING_ONLY_ENGINES: Dict[str, str] = {
    "redis": "redis",
    "valkey": "redis",
}


def find_engine_service(stack: Any) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """(engine, service_name, service_dict) for the compose service that BOTH:

      1. explicitly opts in with `x-mod.backup: logical` (dump/restore-
         capable engines) or `x-mod.backup: streaming` (streaming-only
         engines — GA.ASW.10.10 — see `_STREAMING_ONLY_ENGINES`) on that
         service, and
      2. runs an image this module (for `logical`) or
         `_STREAMING_ONLY_ENGINES` (for `streaming`) knows how to handle.

    Opt-in gates structural detection, never the other way around: an
    existing stack that merely happens to run a postgres image (gitea+
    postgres, nextcloud+postgres, a user's own compose) must NOT have its
    backup behaviour silently changed just because that image matched. Only
    a stack whose author (or template) declared the posture gets it — see
    `service_templates/postgres.yaml`'s `db` service for the template-side
    declaration.

    Both a `logical` and `streaming` engine name come out of THIS one
    function because `stack_streaming_migration.py`'s public builders all
    resolve their engine through this single call — a streaming-only engine
    registered anywhere else would simply never be reachable through that
    module's dispatch. The two postures never collide: `logical` requires
    `base in ENGINE_BUILDERS` (postgres/mysql/mariadb/mongo today) and
    `streaming` requires `base in _STREAMING_ONLY_ENGINES` (redis/valkey
    today) — an engine could in principle declare both builder tables and
    both postures, just not on the same service at once (a compose service
    has exactly one `x-mod.backup` value).

    c1-J5a: the `logical` membership test here is `RESTORE_BUILDERS` — the
    restore-only projection this module owns. Its key set is IDENTICAL to
    the Paid module's `ENGINE_BUILDERS` (that table is literally built from
    this one plus `dump_command`), so nothing about resolution changes.
    """
    services = ((getattr(stack, "compose_spec", None) or {}).get("services")) or {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        xmod = svc.get("x-mod")
        posture = xmod.get("backup") if isinstance(xmod, dict) else None
        if posture not in ("logical", "streaming"):
            continue
        image = str(svc.get("image") or "")
        base = image.split(":", 1)[0].rsplit("/", 1)[-1]
        if posture == "logical" and base in RESTORE_BUILDERS:
            return base, name, svc
        if posture == "streaming":
            canonical = _STREAMING_ONLY_ENGINES.get(base)
            if canonical:
                return canonical, name, svc
    return None, None, None


def _password_vault_ref(stack: Any, svc_name: str, secret_key: str) -> Optional[str]:
    refs = getattr(stack, "secret_refs", None)
    if not isinstance(refs, dict):
        return None
    return (refs.get(svc_name) or {}).get(secret_key)


def build_logical_restore_spec(stack: Any, snapshot_key: str) -> dict:
    """Pure: the ephemeral container spec for a logical (pg_restore) restore.

    c1-J5a: byte-identical to the Paid module's `_build_spec(..., direction=
    "restore")` — same keys, same values, same `path` (the dump's own
    extension, so a dump and its restore address the same object). Never
    touches Vault, the DB, or Docker; the password VALUE is never returned,
    only the vault ref the executor resolves into the container env.
    """
    engine, svc_name, svc = find_engine_service(stack)
    if engine is None:
        raise ValueError("stack has no supported database service for a logical backup")
    builder = RESTORE_BUILDERS[engine]
    conn = builder["connection"](svc)
    path = f"/backup/dump.{builder['dump_ext']}"
    return {
        "kind": "logical_restore",
        "engine": engine,
        "image": svc.get("image") or builder["default_image"],
        "network": _network_name(_project_name(stack), svc),
        "service": svc_name,
        "command": builder["restore_command"](svc_name, conn, path),
        "path": path,
        "password_env": builder["password_env"],
        "password_secret_key": builder["secret_key"],
        "password_vault_ref": _password_vault_ref(stack, svc_name, builder["secret_key"]),
        "snapshot_key": snapshot_key,
    }
