#!/usr/bin/env python3
"""CE-MIGROLE-1 — one shared owner-role resolver for CORE/API/migrations.

Every migration that re-owns an object (`ALTER ... OWNER TO "modTech"`) or
switches into the owner role (`SET ROLE "modTech"`) hardcodes the SaaS
superuser role `modTech`.  On a CE install there IS no `modTech` role: the
DB superuser/owner is POSTGRES_USER (`core`), so those ALTERs die with
`must be able to SET ROLE "modTech"` / `permission denied for schema public`
and mod-migrate exits 1.

This module gives the migrations ONE way to ask "which role owns DB objects
right now?" without changing SaaS behaviour:

    owner_role(cur) -> str
        the QUOTED SQL token `"modTech"` when a role named modTech exists on
        this DB, else the QUOTED token for the connection's current user
        (CE: `"core"`).  Callers interpolate it as
        `f'ALTER TABLE {t} OWNER TO {owner_role(cur)}'`.

    maybe_set_role(cur) -> bool
        `SET ROLE "modTech"` when that role exists, no-op otherwise.
        Returns True only when the role was actually switched into, so the
        caller pairs it with `RESET ROLE`.

Both take a live psycopg2 DBAPI cursor (not a connection) and never raise
for a missing role.  Migrations import it the same way they already import
their siblings (`sys.path.insert(0, <migrations dir>); import _owner_role`):
mod_migrate.py runs each step as a bare-file subprocess, so the script's own
directory is already on sys.path; the insert makes the same import work when
a step is loaded by file path from elsewhere.

Never prints or returns a secret: role names only.
"""
from __future__ import annotations

OWNER_ROLE = "modTech"
SET_ROLE_SAVEPOINT = "mod_migrate_set_role"


def _quote(name: str) -> str:
    """Postgres quoted-identifier token for `name` (`"` doubled, per the spec).

    Callers interpolate the returned token straight into SQL, so an embedded
    quote in a role/user name must not be able to close the identifier.
    """
    return '"' + str(name).replace('"', '""') + '"'


def owner_role(cur) -> str:
    """Quoted SQL role token for whoever owns DB objects on THIS database."""
    if has_owner_role(cur):
        return _quote(OWNER_ROLE)
    cur.execute("SELECT current_user")
    row = cur.fetchone()
    return _quote(row[0] if row else OWNER_ROLE)


def has_owner_role(cur) -> bool:
    """True when a role named modTech exists on this database."""
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (OWNER_ROLE,))
    return cur.fetchone() is not None


def _is_autocommit(cur) -> bool:
    """True when the cursor's connection is in psycopg autocommit mode.

    psycopg2 and psycopg3 both expose `connection.autocommit`; a cursor that
    carries no `connection` attribute (a mock, or a driver that hides it) is
    treated as NOT autocommit so it keeps the older SAVEPOINT-guarded path.
    """
    conn = getattr(cur, "connection", None)
    if conn is None:
        return False
    return bool(getattr(conn, "autocommit", False))


def maybe_set_role(cur) -> bool:
    """SET ROLE into modTech when it exists; no-op (False) otherwise.

    Callers must `RESET ROLE` only when this returns True.

    The role existing is not enough: the connecting user also has to be a
    MEMBER of it, and in Postgres a failed statement aborts the whole
    transaction.  HEAD's per-migration blocks guarded the SET ROLE with
    try/except and printed "(SET ROLE modTech unavailable: ...; creating as
    current role)"; that guard now lives here, wrapped in a SAVEPOINT so a
    non-member's failure rolls back to the savepoint instead of poisoning the
    migration's remaining statements.
    """
    if not has_owner_role(cur):
        return False
    if _is_autocommit(cur):
        # An autocommit connection has no transaction to savepoint into:
        # `SAVEPOINT ...` is itself illegal there (the enum-value migrations
        # run this way).  A failed SET ROLE cannot poison anything either -
        # every statement already committed on its own - so guard with a bare
        # try/except and keep the same message.
        try:
            cur.execute(f'SET ROLE "{OWNER_ROLE}"')
        except Exception as e:  # noqa: BLE001
            print(f"(SET ROLE {OWNER_ROLE} unavailable: {e}; creating as current role)")
            return False
        return True
    cur.execute(f"SAVEPOINT {SET_ROLE_SAVEPOINT}")
    try:
        cur.execute(f'SET ROLE "{OWNER_ROLE}"')
    except Exception as e:  # noqa: BLE001
        cur.execute(f"ROLLBACK TO SAVEPOINT {SET_ROLE_SAVEPOINT}")
        print(f"(SET ROLE {OWNER_ROLE} unavailable: {e}; creating as current role)")
        return False
    cur.execute(f"RELEASE SAVEPOINT {SET_ROLE_SAVEPOINT}")
    return True
