#!/usr/bin/env python3
"""mod-migrate — the one-shot installer/updater migration + seed runner.

Runs every CORE/API/migrations/*.py step that has not been recorded yet, in
filename order, then the first-run seeds, and records each
successfully applied step in the append-only `release_steps` table.

Ordering rule
-------------
Steps are discovered as `*.py` files in the migrations directory and sorted
by FILENAME (lexicographic byte order). The naming convention encodes the
order: `YYYYMMDD[_letter].slug.py` — the date prefix orders by creation
date and an optional single-letter suffix (…b, …e, …f) disambiguates
several migrations added the same day. That lexicographic order IS the
intended application order; never rename or reorder an applied file.

Non-uniform scripts
-------------------
The migration scripts are not a uniform framework. Classification (grep of
the whole directory):
  * Class A — argparse with a `--commit` flag; default (no flag) is a
    dry-run that prints SQL and exits 0. This is the common style
    (e.g. migrations/20261002e_app_user_types.py). mod-migrate passes
    `--commit` so a plain run applies.
  * Class B — scripts that take `--commit` plus extra flags with safe
    defaults (`--tenant-id`, `--backfill`, `--batch-size`): run with
    `--commit` only; defaults are the intended first-run behaviour.
  * Class C — hypothetical scripts without `--commit` at all: run as-is.
    (As of 2026-10-02 every *.py in the directory supports `--commit`,
    but detection is per-file, so a future Class-C file keeps working.)
Detection is per-file: if the script's source contains the literal
`--commit`, pass it; otherwise run the file unchanged.

release_steps
-------------
Every successfully applied step inserts ONE row (step_name UNIQUE,
sha256 checksum of the step file, applied_at, release_version,
duration_ms, runner_host) and commits immediately, so a later failing
step never loses earlier rows. A step already recorded with the same
name is skipped (no-op). If the file's checksum differs from the
recorded one, the step is SKIPPED with a loud warning — a changed file
must never be silently re-run against a live install. ISO 27001:
release_steps is append-only; BEFORE UPDATE OR DELETE triggers RAISE
(created by migrations/20261002f_release_steps.py — the runner also
CREATE TABLE IF NOT EXISTS the same DDL so a fresh install works even
before that migration is applied).

Failure semantics: the run exits non-zero on the FIRST failing step
(the step's own rc); steps after it are not started; earlier rows stay
intact.

Seeds (inventory, "(c) FIRST-RUN DATA")
-----------------------------------------------------
After the migrations, three idempotent first-run seeds are wired as named
steps (`seed:<name>`):
  seed:platform_admin    create_platform_admin.py          (KC user lookup, upsert)
  seed:mobile_client     provision_mobile_client.py        (ensure_mobile_client,
                                                            non-fatal by design)
  seed:platform_settings tools/seed_platform_settings.py --mode $DEPLOYMENT_MODE --commit
(Not wired — installer blocks without standalone entry points: React
email-templates→S3, tailscale client mirror, Keycloak tenant-group /
verifyEmail realm blocks, and the model-less `run_idempotent_migration`
loop, which this runner replaces. The "baseline seed data" rows of the
installer ARE migrations — 20260425_seed_workflow_templates.py,
20260512_seed_system_roles.py, 20260708_seed_mobile_builder_node.py —
and are covered by the migration loop.)

Usage
-----
    python3 tools/mod_migrate.py                 # apply pending steps + seeds
    python3 tools/mod_migrate.py --dry-run       # list what would run
    python3 tools/mod_migrate.py --baseline      # mark every current step
                                                 # (migrations AND seeds) as
                                                 # applied WITHOUT running it
                                                 # — REQUIRED for existing
                                                 # installs where start-
                                                 # main-server.sh already
                                                 # applied everything

`--migrations-dir` points the runner at a different directory (tests use
fake scripts so they never touch the real migrations).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MIGRATIONS_DIR = os.path.join(API_DIR, "migrations")
# CE-NOPAID-4.15 — the paid add-on migrations dir (CE-NOPAID-4.9c layout).
# Absent in the CE extract (open-core/manifest.yaml denies CORE/API/paid_addon/**),
# present on a paid tree, where the compliance migration steps now live.
PAID_ADDON_MIGRATIONS_DIR = os.path.join(API_DIR, "paid_addon", "migrations")
RUNNER_HOST = socket.gethostname()


def default_migrations_chain() -> list[str]:
    """CE-NOPAID-4.15 — the chain for the DEFAULT (real) migrations dir.

    `[DEFAULT_MIGRATIONS_DIR]` on CE (the add-on dir is denied/absent, so the
    chain is one dir and `discover_migrations_chain([primary])` returns exactly
    what `discover_migrations(primary)` did: same glob, same `p.name` sort, no
    dedupe warnings). On a paid tree the add-on dir is appended, so the moved
    paid-only steps keep their date position in the merged chain and the runner
    still skips already-applied steps by NAME (names are unchanged by the move,
    so an existing paid DB records/never re-runs them)."""
    chain = [DEFAULT_MIGRATIONS_DIR]
    if os.path.isdir(PAID_ADDON_MIGRATIONS_DIR):
        chain.append(PAID_ADDON_MIGRATIONS_DIR)
    return chain

# How much of a failing step's output to echo (short tail — never dump the
# whole transcript, and never env/secrets: only the step's own stdout/stderr).
OUTPUT_TAIL_LINES = 12

# ---------------------------------------------------------------------------
# release_steps DDL — MUST stay in lockstep with
# migrations/20261002f_release_steps.py (the migration is the canonical apply
# path; the runner repeats it so a fresh install runs before the migration
# and the table already exists with the same shape).
# ---------------------------------------------------------------------------
RELEASE_STEPS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS release_steps (
    id              SERIAL PRIMARY KEY,
    step_name       VARCHAR(255) NOT NULL UNIQUE,
    checksum        VARCHAR(64) NOT NULL,
    applied_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    release_version VARCHAR(64),
    duration_ms     INTEGER,
    runner_host     VARCHAR(255)
);
"""

RELEASE_STEPS_GUARD_FN_SQL = """
CREATE OR REPLACE FUNCTION release_steps_no_mutate()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Operation % is forbidden on append-only table % (ISO no-hard-delete)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END;
$$ LANGUAGE plpgsql
"""

RELEASE_STEPS_TRIGGERS = [
    ("trg_no_update_release_steps", "UPDATE"),
    ("trg_no_delete_release_steps", "DELETE"),
]

RELEASE_STEPS_STATEMENTS = (
    [("create table release_steps", RELEASE_STEPS_TABLE_SQL)]
    + [("create or replace function release_steps_no_mutate()", RELEASE_STEPS_GUARD_FN_SQL)]
    + [
        (f"drop trigger {name}", f"DROP TRIGGER IF EXISTS {name} ON release_steps")
        for name, _ in RELEASE_STEPS_TRIGGERS
    ]
    + [
        (
            f"create trigger {name} BEFORE {op} (row)",
            f"CREATE TRIGGER {name} BEFORE {op} ON release_steps "
            f"FOR EACH ROW EXECUTE FUNCTION release_steps_no_mutate()",
        )
        for name, op in RELEASE_STEPS_TRIGGERS
    ]
)


# ---------------------------------------------------------------------------
# Step discovery / classification
# ---------------------------------------------------------------------------

def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_migrations(migrations_dir: str | Path) -> list[Path]:
    """All *.py files in the dir, sorted by filename (the ordering rule —
    see module docstring). Non-.py files (README.md, __pycache__) are ignored."""
    d = Path(migrations_dir)
    if not d.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {d}")
    return sorted((p for p in d.glob("*.py") if p.is_file()), key=lambda p: p.name)


def discover_migrations_chain(migrations_dirs: list[str | Path], *,
                              log: Callable[[str], None] = print) -> list[Path]:
    """The merged step chain for `--extra-migrations-dir`.

    `migrations_dirs[0]` is the PRIMARY dir: missing it raises FileNotFoundError
    exactly like discover_migrations() (main() turns that into exit 2). Every
    later dir is an EXTRA dir: a missing one contributes zero steps and logs
    one line — it never raises (an internal-only dir name passed by mistake on CE
    must not break the boot). Files from every dir are merged and sorted GLOBALLY
    by `p.name` — the same key as discover_migrations() — so an add-on step keeps
    its date position among the core ones. Two dirs never produce two runs of
    the same step: dedupe by step_name() (the ledger key; release_steps.step_name
    is UNIQUE), the FIRST dir in chain order wins, and a shadowed duplicate logs
    one warning naming both paths (otherwise it would be a silent checksum-mismatch
    skip)."""
    dirs = list(migrations_dirs)
    if not dirs:
        raise FileNotFoundError("migrations dir not found: (empty chain)")
    primary = Path(dirs[0])
    if not primary.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {primary}")
    found: list[Path] = [p for p in primary.glob("*.py") if p.is_file()]
    for extra in dirs[1:]:
        d = Path(extra)
        if not d.is_dir():
            log(f"mod-migrate: extra dir {d} absent, skipped")
            continue
        found.extend(p for p in d.glob("*.py") if p.is_file())
    found.sort(key=lambda p: p.name)  # stable: primary dir first among equal names
    chain: list[Path] = []
    seen: dict[str, Path] = {}
    for p in found:
        name = step_name(p)
        first = seen.get(name)
        if first is None:
            seen[name] = p
            chain.append(p)
        else:
            log(f"WARNING  duplicate step {name}: keeping {first} "
                f"(first dir in chain wins); ignoring {p}")
    return chain


def step_name(path: str | Path) -> str:
    """Step identity recorded in release_steps: the file stem, e.g.
    '20261002e_app_user_types'. Stable across checksum changes."""
    return Path(path).stem


def supports_commit(path: str | Path) -> bool:
    """Class A/B (source mentions `--commit`) vs Class C (run as-is)."""
    try:
        text = Path(path).read_text(errors="replace")[:8000]
    except OSError:
        return False
    return "--commit" in text


def build_command(path: str | Path) -> list[str]:
    return [sys.executable, str(path)] + (["--commit"] if supports_commit(path) else [])


def load_skip_names(skip_file: Optional[str]) -> set[str]:
    """Names from a CE skip file (one step name per line).

    Blank lines and `#` comments are ignored, so a line may carry a trailing
    `# <reason>`. A trailing `.py` is accepted and stripped: the file lists
    step FILENAMES, the ledger keys on the stem (step_name())."""
    if not skip_file:
        return set()
    names = set()
    for raw in Path(skip_file).read_text(errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line[:-3] if line.endswith(".py") else line)
    return names


# ---------------------------------------------------------------------------
# Seeds ("(c) FIRST-RUN DATA")
# ---------------------------------------------------------------------------

def seed_steps(api_dir: str = API_DIR) -> list[tuple[str, str, list[str]]]:
    """(name, entry_script_path, argv). Each entry point is idempotent."""
    mode = os.environ.get("DEPLOYMENT_MODE", "saas")
    return [
        (
            "seed:platform_admin",
            os.path.join(api_dir, "create_platform_admin.py"),
            [sys.executable, os.path.join(api_dir, "create_platform_admin.py")],
        ),
        (
            "seed:mobile_client",
            os.path.join(api_dir, "provision_mobile_client.py"),
            [sys.executable, os.path.join(api_dir, "provision_mobile_client.py")],
        ),
        (
            "seed:platform_settings",
            os.path.join(api_dir, "tools", "seed_platform_settings.py"),
            [sys.executable, os.path.join(api_dir, "tools", "seed_platform_settings.py"),
             "--mode", mode, "--commit"],
        ),
    ]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _bootstrap_engine():
    """Same engine bootstrap the migration scripts use: database.initialize()
    (Vault dynamic creds, or static DATABASE_URL) then database.get_engine().
    Tests NEVER hit this path — they pass the sibling test engine in."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    import database  # type: ignore

    database.initialize()
    eng = database.get_engine()
    if eng is None:
        raise RuntimeError("Database engine is None; check Vault creds.")
    return eng


# P18-F2 — the parent's ONE minted credential payload + its lease expiry.
# Empty dict = "no fd": non-Vault/dev mode, or a parent that did not own the
# engine bootstrap (tests pass `db=`), so every step runs byte-identically to
# HEAD. The payload bytes are the only copy anywhere; they are never in env,
# argv, a file or a log (the fd pipe is the only channel).
_PARENT_CREDS: dict = {}
# True only when run() bootstrapped the engine itself (the production path):
# only that parent owns a Vault lease to hand down.
_RUNNER_OWNS_ENGINE = False


def _database_module():
    """The CORE/API/database.py module (same object every call, so tests'
    monkeypatched attributes are seen)."""
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    import database  # type: ignore

    return database


def _mint_parent_creds() -> Optional[bytes]:
    """P18-F2 — mint the dynamic DB creds ONCE in the parent (slow boot).

    Calls database._rotate_credentials() exactly once and reads the lease
    globals it just set (database.py:501/:132-138), returning ONE JSON line
    {user, password, lease_expires_at} as bytes for the step children. Returns
    None — and clears the holder, so children fall back to their own Vault
    path — when Vault is not configured (non-Vault/dev mode stays byte-identical
    to today) or the rotation failed. Nothing here logs a value.
    """
    database = _database_module()
    if not database._use_vault():
        _PARENT_CREDS.clear()
        return None
    if not database._rotate_credentials():
        _PARENT_CREDS.clear()
        return None
    user, password = database._cur_username, database._cur_password
    if not user or not password:
        _PARENT_CREDS.clear()
        return None
    expires_at = float(database._cred_fetched_at) + float(database._lease_duration)
    payload = (json.dumps({"user": user, "password": password,
                           "lease_expires_at": expires_at}) + "\n").encode()
    _PARENT_CREDS["payload"] = payload
    _PARENT_CREDS["expires_at"] = expires_at
    return payload


def _parent_cred_for(timeout: int, log: Callable[[str], None]) -> Optional[bytes]:
    """P18-F2 lease guard: the payload for the NEXT step, re-minted when the
    parent's lease would not cover it (remaining < timeout + 120 s).

    Empty holder = no fd at all (non-Vault / tests / handoff already given up).
    """
    if not _RUNNER_OWNS_ENGINE or not _PARENT_CREDS:
        return None
    remaining = float(_PARENT_CREDS["expires_at"]) - time.time()
    if remaining >= timeout + 120:
        return _PARENT_CREDS["payload"]
    log(f"-- P18-F2: lease has {int(remaining)}s left (< {timeout + 120}s) — "
        f"re-minting before the next step")
    return _mint_parent_creds()


def _as_connection(engine_or_conn):
    if hasattr(engine_or_conn, "dialect") and hasattr(engine_or_conn, "connect"):
        return engine_or_conn.connect()
    return engine_or_conn


def ensure_release_steps_table(conn) -> None:
    from sqlalchemy import text

    for _label, sql in RELEASE_STEPS_STATEMENTS:
        conn.execute(text(sql))


def load_recorded(conn) -> dict[str, str]:
    from sqlalchemy import text

    rows = conn.execute(text("SELECT step_name, checksum FROM release_steps")).fetchall()
    return {r[0]: r[1] for r in rows}


def _commit_runner(conn) -> None:
    """the runner must hold no transaction while a step runs (BUG-1027w-c).

    `ensure_release_steps_table` / `load_recorded` autobegin a transaction on the
    runner's own Connection; if it is still open when a step subprocess starts,
    that step's DDL on `release_steps` (or on anything the runner touched) waits
    forever on the runner's transactionid. End it before every subprocess."""
    try:
        open_txn = conn.in_transaction()
    except AttributeError:  # a Session (test harness) has no in_transaction()
        open_txn = conn.get_transaction() is not None
    if open_txn:
        conn.commit()


def record_step(conn, *, name: str, checksum: str, duration_ms: int,
                release_version: Optional[str]) -> None:
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO release_steps (step_name, checksum, release_version, "
            "duration_ms, runner_host) VALUES (:n, :c, :v, :d, :h)"
        ),
        {"n": name, "c": checksum, "v": release_version,
         "d": duration_ms, "h": RUNNER_HOST},
    )
    conn.commit()  # commit per step: a later failure never loses earlier rows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_DIGITS = set("0123456789")


def _is_bare_sql_identifier(name: str) -> bool:
    """ASCII [A-Za-z_][A-Za-z0-9_]* — the only shape allowed into PGOPTIONS."""
    if not name:
        return False
    if name[0] not in _LETTERS | {"_"}:
        return False
    return all(ch in _LETTERS | _DIGITS | {"_"} for ch in name[1:])


def _agent_token_absent_at_start() -> bool:
    """True when the Vault Agent token file is missing/empty in the PARENT at
    runner start (same path/default as database.py:83 `_VAULT_AGENT_TOKEN_PATH`).
    The parent's own database.initialize() already waited the full
    VAULT_AGENT_TOKEN_WAIT_S, so children must not each re-wait — that wait is
    the measured ~32s per migration step (database.py:172-196 `_vault_login`,
    6x5s) across ~374 steps."""
    path = os.environ.get("VAULT_AGENT_TOKEN_PATH", "/run/vault/token")
    try:
        return os.path.getsize(path) == 0
    except OSError:
        return True


_AGENT_TOKEN_ABSENT_AT_START = _agent_token_absent_at_start()


def _owner_session_env(log: Callable[[str], None]) -> dict[str, str]:
    """Extra env for EVERY migration subprocess (MOD-MIGRATE-OPTIONS).

    PGOPTIONS `-c role=<owner>`: libpq applies it to EVERY connection the
    subprocess opens — the SQLAlchemy engine AND hand-rolled psycopg2 ones
    (e.g. migrations/20260908_add_waiting_for_user_wizard_status.py) — so the
    whole step runs as the DB owner role and object-owner checks (ALTER TYPE
    ... ADD VALUE, `must be owner of type auditaction`) pass on CE, where the
    owner is POSTGRES_USER (`core`) and no role named modTech exists, so
    migrations/_owner_role.py:74 `maybe_set_role` no-ops there.

    Same derivation as database.py:35 `_PG_OWNER_ROLE` = POSTGRES_USER (internal:
    modTech, which the dyn role is GRANTed — vault-init.sh:1102; CE: core,
    which the dyn role is a member of — first-run-openbao-init.sh:312
    `IN ROLE "core"`).  Unset POSTGRES_USER -> no option.  A name that is not
    a bare SQL identifier is skipped with a warning (the VALUE is never
    printed) so nothing can be injected into PGOPTIONS.  The API runtime
    process never sees this env — only migration subprocesses.
    """
    extra: dict[str, str] = {}
    owner = os.environ.get("POSTGRES_USER")
    if owner:
        if _is_bare_sql_identifier(owner):
            extra["PGOPTIONS"] = f"{os.environ.get('PGOPTIONS', '')} -c role={owner}".strip()
        else:
            log(f"WARNING owner-session option skipped: POSTGRES_USER is not a bare SQL identifier ({len(owner)} chars, value not printed)")
    if _AGENT_TOKEN_ABSENT_AT_START:
        extra["VAULT_AGENT_TOKEN_WAIT_S"] = "0"
    return extra


def _run_subprocess(argv: list[str], timeout: int, log: Callable[[str], None],
                    cred: Optional[bytes] = None) -> tuple[int, str]:
    """Run one step subprocess. P18-F2: when `cred` (the parent's ONE minted
    credential JSON line) is set, hand it to the child on an INHERITED PIPE FD
    only — the env carries the fd NUMBER (MOD_MIGRATE_CRED_FD), never the creds
    (env is world-readable via /proc/<pid>/environ, argv via ps), so the pipe is
    the only safe channel. `cred is None` -> the call is byte-identical to HEAD
    (no fd, no env key). A pipe write error or a platform without pass_fds skips
    the fd so the child falls back to its own Vault path — never fail closed."""
    env = {**os.environ, **_owner_session_env(log)}
    r = None
    if cred is not None:
        w = None
        try:
            r, w = os.pipe()
            os.write(w, cred)  # ONE JSON line
            os.close(w)        # flush + close the write end BEFORE the fork
            env["MOD_MIGRATE_CRED_FD"] = str(r)
        except OSError as e:
            log(f"-- P18-F2: cred pipe unavailable ({type(e).__name__}); "
                f"step child falls back to its own Vault path")
            env.pop("MOD_MIGRATE_CRED_FD", None)
            for fd in (w, r):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            r = None
    kwargs = dict(capture_output=True, text=True, timeout=timeout, env=env)
    if r is not None:
        kwargs["pass_fds"] = (r,)
    try:
        proc = subprocess.run(argv, **kwargs)
    except ValueError:  # pass_fds unsupported (non-POSIX): retry once WITHOUT the fd
        log("-- P18-F2: pass_fds unsupported here; step child falls back to its own "
            "Vault path")
        env.pop("MOD_MIGRATE_CRED_FD", None)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              env=env)
    finally:
        if r is not None:
            try:
                os.close(r)  # the child got its own dup via pass_fds; close ours
            except OSError:
                pass
    tail_lines = (proc.stdout or "").splitlines()[-OUTPUT_TAIL_LINES:]
    tail_lines += (proc.stderr or "").splitlines()[-OUTPUT_TAIL_LINES:]
    return proc.returncode, "\n".join(line[:400] for line in tail_lines)


def _execute_step(*, name: str, checksum: str, argv: list[str],
                  conn, dry_run: bool, baseline: bool, release_version: Optional[str],
                  timeout: int, log: Callable[[str], None],
                  recorded: dict[str, str],
                  skip_names: Optional[frozenset] = None,
                  baseline_names: Optional[frozenset] = None) -> int:
    """Run (or baseline / dry-run) one step. Returns 0 or the step's rc.

    `skip_names` (BUG-1027w-b): a step listed there is neither run nor recorded
    — it stays pending, so the same file used internally would still apply it."""
    if skip_names and name in skip_names:
        log(f"skip     {name} (skip-file)")
        return 0
    if name in recorded:
        if recorded[name] != checksum:
            log(f"WARNING  {name}: file checksum changed since it was recorded "
                f"(recorded {recorded[name][:12]}…, now {checksum[:12]}…). "
                f"SKIPPED — a changed step file is never re-run against a live "
                f"install; investigate and apply deliberately.")
        else:
            log(f"skip     {name} (already applied)")
        return 0
    if dry_run:
        log(f"would run  {name}  ({' '.join(argv)})")
        return 0
    if baseline_names and name in baseline_names:
        record_step(conn, name=name, checksum=checksum, duration_ms=0,
                    release_version=release_version)
        log(f"baselined  {name} (fresh-install)")
        return 0
    if baseline:
        record_step(conn, name=name, checksum=checksum, duration_ms=0,
                    release_version=release_version)
        log(f"baselined  {name} (recorded as applied, NOT executed)")
        return 0
    _commit_runner(conn)  # the runner must hold no transaction while a step runs (BUG-1027w-c)
    started = time.monotonic()
    cred = _parent_cred_for(timeout, log)  # P18-F2 lease guard (None = no fd)
    try:
        # cred is None -> the HEAD call shape exactly (sibling tests monkeypatch
        # the 3-arg form); only the P18-F2 fd path adds the 4th argument.
        rc, tail = (_run_subprocess(argv, timeout, log, cred) if cred is not None
                    else _run_subprocess(argv, timeout, log))
    except subprocess.TimeoutExpired:
        log(f"FAIL     {name}: timed out after {timeout}s")
        return 124
    except OSError as e:
        log(f"FAIL     {name}: could not exec: {e}")
        return 125
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if rc != 0 and cred is not None and "SQLSTATE 42501" in tail:
        # Grants revoked mid-flight (the parent owns the lease, so the child
        # cannot renew it): re-mint ONCE and retry that step once. A second
        # failure keeps today's loud failure.
        log(f"retry    {name}: SQLSTATE 42501 in the step tail — re-minting the "
            f"parent lease and retrying this step once")
        fresh = _mint_parent_creds()
        _commit_runner(conn)
        started = time.monotonic()
        try:
            rc, tail = _run_subprocess(argv, timeout, log, fresh)
        except subprocess.TimeoutExpired:
            log(f"FAIL     {name}: timed out after {timeout}s (42501 retry)")
            return 124
        except OSError as e:
            log(f"FAIL     {name}: could not exec on retry: {e}")
            return 125
        elapsed_ms = int((time.monotonic() - started) * 1000)
    if rc != 0:
        log(f"FAIL     {name} (rc={rc}, {elapsed_ms}ms)")
        for line in tail.splitlines():
            log(f"  | {line}")
        return rc
    record_step(conn, name=name, checksum=checksum, duration_ms=elapsed_ms,
                release_version=release_version)
    log(f"applied  {name} ({elapsed_ms}ms)")
    return 0


def run(*, migrations_dirs: Optional[list[str | Path]] = None,
        migrations_dir: str = DEFAULT_MIGRATIONS_DIR, dry_run: bool = False,
        baseline: bool = False, release_version: Optional[str] = None,
        run_seeds: bool = True, db=None, step_timeout: int = 1800,
        skip_file: Optional[str] = None, baseline_list: Optional[str] = None,
        log: Callable[[str], None] = print) -> int:
    """Run all pending steps in order. Returns 0 or the first failing rc.

    `db` accepts a SQLAlchemy Engine (production path bootstraps one via
    database.initialize()/get_engine()), a Connection, or a Session bound to
    a connection (tests pass the conftest `db_session` — its commit() is a
    savepoint release, so tests roll back cleanly).

    `migrations_dirs`: the dir chain — primary dir first, then
    any add-on dirs (`--extra-migrations-dir`). With None (every existing caller)
    the code path and its output are byte-identical to HEAD: discovery stays
    discover_migrations(migrations_dir)."""
    migrations = (discover_migrations_chain(migrations_dirs, log=log)
                  if migrations_dirs is not None
                  else discover_migrations(migrations_dir))

    # BUG-1027w-b: validate the skip file BEFORE anything runs — including
    # before the engine bootstrap, so a typo cannot open a DB or create the
    # ledger table at all. With no --skip-file this block is inert and the
    # whole run is byte-identical to HEAD.
    skip_names = load_skip_names(skip_file)
    if skip_names:
        known = {step_name(p) for p in migrations} | {n for n, _, _ in seed_steps()}
        unknown = sorted(n for n in skip_names if n not in known)
        if unknown:
            raise FileNotFoundError(
                f"skip-file: unknown step {unknown[0]} — every skip-file entry "
                f"must name a discovered migration file (or a wired seed); "
                f"nothing was run")

    # P18-F1b: same rule for the fresh-install baseline list — an entry naming
    # no discovered file/seed exits 2 BEFORE the engine bootstrap, so a typo
    # cannot open a DB or create the ledger table at all.
    baseline_names = load_skip_names(baseline_list)
    if baseline_names:
        known = {step_name(p) for p in migrations} | {n for n, _, _ in seed_steps()}
        unknown = sorted(n for n in baseline_names if n not in known)
        if unknown:
            raise FileNotFoundError(
                f"baseline-list: unknown step {unknown[0]} — every baseline-list "
                f"entry must name a discovered migration file (or a wired seed); "
                f"nothing was run")

    if db is None:
        db = _bootstrap_engine()
        # P18-F2: this runner owns the Vault lease its initialize() just minted,
        # so it hands those creds to every step child on an fd instead of paying
        # the Vault login + fetch + engine build (~2.5 s) per step. Non-Vault
        # mode: _mint_parent_creds() returns None -> no fd -> byte-identical.
        global _RUNNER_OWNS_ENGINE
        _RUNNER_OWNS_ENGINE = True
        if not dry_run and not baseline:
            _mint_parent_creds()
    conn = _as_connection(db)

    ensure_release_steps_table(conn)
    _commit_runner(conn)
    recorded = load_recorded(conn)
    _commit_runner(conn)

    log(f"mod-migrate: dir={migrations_dir} dry_run={dry_run} baseline={baseline} "
        f"release_version={release_version!r} host={RUNNER_HOST}")

    if skip_names:
        log(f"-- skip-file: {len(skip_names)} step(s) will be skipped, not run "
            f"and not recorded")
    log(f"-- migrations: {len(migrations)} file(s) (filename order)")

    for path in migrations:
        rc = _execute_step(
            name=step_name(path), checksum=sha256_file(path),
            argv=build_command(path),
            conn=conn, dry_run=dry_run, baseline=baseline,
            release_version=release_version, timeout=step_timeout,
            log=log, recorded=recorded, skip_names=skip_names,
            baseline_names=baseline_names,
        )
        if rc != 0:
            return rc
        if not dry_run:
            recorded = load_recorded(conn)

    if run_seeds:
        seeds = seed_steps()
        log(f"-- seeds: {len(seeds)} step(s)")
        for name, entry, argv in seeds:
            if not Path(entry).is_file():
                log(f"skip     {name}: entry point not present ({entry}) — "
                    f"listed in the run report instead of failing the release")
                continue
            checksum = sha256_file(entry)
            rc = _execute_step(
                name=name, checksum=checksum,
                argv=argv,
                conn=conn, dry_run=dry_run, baseline=baseline,
                release_version=release_version, timeout=step_timeout,
                log=log, recorded=recorded, skip_names=skip_names,
            baseline_names=baseline_names,
            )
            if rc != 0:
                return rc
            if not dry_run:
                recorded = load_recorded(conn)
    else:
        log("-- seeds: disabled")

    log("mod-migrate: done.")
    _commit_runner(conn)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--migrations-dir", default=DEFAULT_MIGRATIONS_DIR,
                   help="Directory of migration scripts (tests point this at "
                        "fake scripts; never run the real dir by accident).")
    p.add_argument("--extra-migrations-dir", dest="extra_migrations_dirs",
                   action="append", default=None, metavar="DIR",
                   help="Repeatable. An ADD-ON migrations dir "
                        "appended to the chain after --migrations-dir (the primary "
                        "dir is always chain[0]) — an add-on carries its own "
                        "migrations. Steps from every dir are merged and sorted "
                        "GLOBALLY by filename, so an add-on step keeps its date "
                        "position; a duplicate step name keeps the FIRST dir in "
                        "the chain (one warning names both paths). A missing "
                        "extra dir is a no-op (one log line, zero steps); a "
                        "missing primary dir still exits 2. No flag = behaviour "
                        "identical to today.")
    p.add_argument("--dry-run", action="store_true",
                   help="List every step that would run; record nothing.")
    p.add_argument("--baseline", action="store_true",
                   help="Record every current step (migrations AND seeds) as "
                        "applied WITHOUT executing it. Required for existing "
                        "installs where start-main-server.sh already applied "
                        "all migrations/seeds.")
    p.add_argument("--baseline-list", default=None,
                   help="P18-F1b: file of step stems (same format as --skip-file) "
                        "recorded as applied on a from-scratch DB — the steps' "
                        "effects are already built by create_tables(), so they "
                        "are recorded WITHOUT executing. Every other step still "
                        "runs. An entry naming no discovered file exits 2 before "
                        "anything runs.")
    p.add_argument("--release-version", default=None,
                   help="Tag recorded rows with a release/version string.")
    p.add_argument("--no-seeds", action="store_true",
                   help="Run only the migration steps, skip the seed steps.")
    p.add_argument("--step-timeout", type=int, default=1800,
                   help="Per-step subprocess timeout in seconds (default 1800).")
    p.add_argument("--skip-file", default=None,
                   help="file listing of step filenames (one "
                        "per line; `#` comments and blanks ignored) to SKIP — "
                        "not run, not recorded. CE only: the listed steps are "
                        "historical data (named tenant/user ids, backfills). "
                        "An entry naming no discovered file exits 2 before "
                        "anything runs.")
    args = p.parse_args(argv)

    # CE-NOPAID-4.15: with the DEFAULT (real) migrations dir and no explicit
    # extra dirs, the chain is the core dir plus the paid add-on dir WHEN IT
    # EXISTS — a paid tree sees the moved compliance steps at their date
    # positions; CE (the dir is denied/absent) keeps the single-dir path, so
    # discovery and every log line are byte-identical to today.
    chain = args.extra_migrations_dirs
    if chain is None and args.migrations_dir == DEFAULT_MIGRATIONS_DIR:
        chain = default_migrations_chain()[1:] or None
    try:
        return run(
            migrations_dirs=([args.migrations_dir] + chain
                             if chain else None),
            migrations_dir=args.migrations_dir,
            dry_run=args.dry_run,
            baseline=args.baseline,
            baseline_list=args.baseline_list,
            release_version=args.release_version,
            run_seeds=not args.no_seeds,
            step_timeout=args.step_timeout,
            skip_file=args.skip_file,
        )
    except FileNotFoundError as e:
        print(f"mod-migrate: ERROR {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — surface a clean non-zero exit
        print(f"mod-migrate: ERROR {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
