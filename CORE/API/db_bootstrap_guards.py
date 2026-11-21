"""Fresh-install bootstrap: the migration-only append-only / no-hard-delete triggers.

1791 (`the internal design notes`) proved that
`database.py:_run_db_bootstrap_ddl` (create_all + inline `_migrate_schema`) NEVER
executes `CORE/API/migrations/*`. On a FRESH install (CE, any new SaaS/on-prem/colo
DB) none of the `trg_no_*` guards exist, so UPDATE/DELETE/TRUNCATE on the audit,
ledger and attestation tables SUCCEED — breaking the ISO 27001 no-hard-delete and
append-only guarantees.

This module hoists, VERBATIM, the trigger-bearing statements out of the migration
files that carry them, so the bootstrap can replay them once per fresh DB. Scope is
TRIGGERS + their guard functions only (CHECK/FK constraints and unique indexes are
1794b).

Two sourcing strategies, per the brief:

* **import** — 22 of the 26 trigger-bearing migrations expose their statement list
  as an importable module-level constant (`_STATEMENTS` / `STATEMENTS`) or a pure
  accessor (`_sql_statements()`); importing keeps the SQL single-sourced, so a
  migration edit is picked up by the bootstrap automatically. Importing these
  modules has no side effects (they are argparse CLIs whose `main()` only runs
  under `__main__`).
* **copy** — 4 migrations (`20260508_wizard_event_tables_append_only`,
  `20260907_add_wizard_runs`, `20260908_add_conversational_project_builder`,
  `20261002g_plan_tiers`) build their statements inline inside `main()`, so they
  expose nothing importable; those blocks are copied verbatim below, each citing
  its source file.

Every statement runs in its own SAVEPOINT/transaction. A failure (typically: the
table is absent because a paid model isn't created in CE) is logged as a WARNING
naming the table and skipped — API boot must NEVER fail because of this. The
statements are idempotent as written (`CREATE OR REPLACE FUNCTION`,
`DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`), so a second bootstrap run is a no-op.
"""
from __future__ import annotations

import logging
import os
import re

from sqlalchemy import text as satext

log = logging.getLogger("mod.db_bootstrap_guards")

# Migrations whose guard/trigger SQL is imported (never copied). Each exposes
# `_STATEMENTS` / `STATEMENTS` (list of (label, sql)) or `_sql_statements()`.
_MIGRATION_MODULES: tuple[str, ...] = (
    "20260929_platform_settings",
    "20260929_work_loans",
    "20261002_http_triggers",
    "20261002f_release_steps",
    "20261003_gy_c15_legal_hold",
    "20261003c_audit_append_only_triggers",
    "20261003f_conformance_check_results",
    "20261003i_erasure_ledger",
    "20261004a_audit_pii_scrub_path",
    "20261004c_consent_records",
    "20261004d_auditor_grants",
    "20261005_gy_c10_access_review_campaigns",
    "20261009e_fortress_42_compliance_attestations",
    "20261012u_fortress_36c_output_scan_exemptions",
    "20261012v_fortress_59_workflow_certifications",
    "20261012x_fortress_23c_node_run_attestations",
    "20261012y_fortress_63_dp_budget_ledger",
    "20261012z_fortress_67_mpc_session_state",
)

# Filled by _inline_blocks() (see below) — copied verbatim from the 4 migrations
# that build their trigger SQL inline inside main().
_INLINE_BLOCKS: list[tuple[str, str]] = []


def _keep(sql: str) -> bool:
    """True for guard functions, trigger CREATEs and ENABLE ALWAYS clauses."""
    up = sql.upper()
    return (
        "CREATE TRIGGER" in up
        or "CREATE OR REPLACE FUNCTION" in up
        or "ENABLE ALWAYS" in up
        or "DROP TRIGGER" in up
    )


def _tables_named(sql: str) -> str:
    """Table names a statement touches — for the WARNING message."""
    names = re.findall(r"\bON\s+([a-z_][a-z_0-9]*)", sql, re.I)
    return ",".join(dict.fromkeys(names)) or "unknown"


def _migration_dirs(root: str | None = None) -> list[str]:
    """Migration dirs in CHAIN order (CE-NOPAID-4.13e).

    `CORE/API/migrations` is the PRIMARY dir; `CORE/API/paid_addon/migrations`
    (present on a paid tree, absent on CE) is an EXTRA dir. A stem that only
    exists in the add-on dir is still importable here, so a paid-only trigger
    migration hoists its guards on a paid boot instead of silently warning.
    """
    if root is None:
        root = os.path.dirname(os.path.abspath(__file__))
    dirs = [os.path.join(root, "migrations")]
    addon = os.path.join(root, "paid_addon", "migrations")
    if os.path.isdir(addon):
        dirs.append(addon)
    return dirs


def _migration_step_paths(dirs: list[str] | None = None) -> dict[str, str]:
    """stem -> module path for every *.py step in the chain, FIRST dir wins.

    Resolution goes through `tools.mod_migrate.discover_migrations_chain` (CE-
    NOPAID-4.13a) so the bootstrap and the runner agree on one ordering/dedupe
    rule: the primary dir is first in the chain, an absent extra dir contributes
    nothing, and a stem present in BOTH dirs keeps the CORE copy (the chain's
    dedupe keeps the first dir and logs one warning naming both paths).
    """
    if dirs is None:
        dirs = _migration_dirs()
    steps: list[str] = []
    try:
        from tools.mod_migrate import discover_migrations_chain

        steps = list(discover_migrations_chain(dirs, log=lambda m: log.warning(m)))
    except (ImportError, FileNotFoundError) as e:  # noqa: BLE001 - boot never fails here
        # Same chain semantics without the runner: dirs in order, first dir wins.
        log.warning("append-only guards: mod_migrate chain unavailable (%s); "
                    "scanning the chain dirs directly", e)
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.endswith(".py"):
                    steps.append(os.path.join(d, f))
    by_stem: dict[str, str] = {}
    for p in steps:
        by_stem.setdefault(os.path.splitext(os.path.basename(str(p)))[0], str(p))
    return by_stem


def _load_imported_statements() -> list[tuple[str, str]]:
    """Import the migration modules and yield their trigger-bearing statements."""
    out: list[tuple[str, str]] = []
    steps = _migration_step_paths()
    for name in _MIGRATION_MODULES:
        path = steps.get(name)
        if not path:
            log.warning("append-only guards: migration %s missing, skipped", name)
            continue
        # Import by file path (the migrations are not a package).
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, path)
        loaded = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(loaded)
        except Exception as e:  # noqa: BLE001
            log.warning("append-only guards: import of %s failed (%s), skipped", name, e)
            continue
        stmts = None
        for attr in ("_STATEMENTS", "STATEMENTS"):
            cand = getattr(loaded, attr, None)
            if isinstance(cand, list):
                stmts = cand
                break
        if stmts is None and callable(getattr(loaded, "_sql_statements", None)):
            try:
                stmts = loaded._sql_statements()
            except Exception as e:  # noqa: BLE001
                log.warning("append-only guards: %s._sql_statements() failed (%s), skipped", name, e)
                continue
        if not isinstance(stmts, list):
            log.warning("append-only guards: %s exposes no statement list, skipped", name)
            continue
        for item in stmts:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                label, sql = str(item[0]), item[1]
            else:
                label, sql = "", str(item)
            if not isinstance(sql, str) or not _keep(sql):
                continue
            out.append((f"{name}: {label}", sql.strip()))
    return out


# ---------------------------------------------------------------------------
# COPIED VERBATIM — these 4 migrations build their trigger SQL inline inside
# main(), so they expose nothing importable. Source file cited above each block.
# ---------------------------------------------------------------------------
def _inline_blocks() -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []

    # migrations/20260508_wizard_event_tables_append_only.py — guard function
    # (the migration's `wizard_event_no_mutate()` step).
    blocks.append((
        "20260508_wizard_event_tables_append_only.py: create or replace "
        "wizard_event_no_mutate trigger function",
        """
        CREATE OR REPLACE FUNCTION wizard_event_no_mutate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Operation % is forbidden on append-only event table %',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql
        """,
    ))
    # Same file: for each target table (the two parents; the migration also
    # walks existing monthly partitions, which inherit the parent's triggers)
    # and each of UPDATE/DELETE/TRUNCATE, DROP TRIGGER IF EXISTS + CREATE TRIGGER.
    for _tbl in ("wizard_feedback_event", "wizard_finding_action_event"):
        for _op in ("UPDATE", "DELETE", "TRUNCATE"):
            _trig = f"trg_no_{_op.lower()}_{_tbl}"[:63]
            blocks.append((
                f"20260508_wizard_event_tables_append_only.py: drop trigger "
                f"{_trig} if exists on {_tbl}",
                f"DROP TRIGGER IF EXISTS {_trig} ON {_tbl}",
            ))
            blocks.append((
                f"20260508_wizard_event_tables_append_only.py: create trigger "
                f"{_trig} BEFORE {_op.lower()} on {_tbl}",
                f"""
                CREATE TRIGGER {_trig}
                    BEFORE {_op} ON {_tbl}
                    FOR EACH STATEMENT EXECUTE FUNCTION wizard_event_no_mutate()
                """,
            ))

    # migrations/20260907_add_wizard_runs.py — function + delete/truncate pair.
    blocks.append((
        "20260907_add_wizard_runs.py: create or replace wizard_run_no_hard_delete() function",
        """
        CREATE OR REPLACE FUNCTION wizard_run_no_hard_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'Hard delete/truncate is forbidden on % (ISO no-hard-delete); '
                'use soft-delete (set deleted_at) instead',
                TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql
        """,
    ))
    blocks.append((
        "20260907_add_wizard_runs.py: drop trigger trg_no_delete_wizard_runs if exists",
        "DROP TRIGGER IF EXISTS trg_no_delete_wizard_runs ON wizard_runs",
    ))
    blocks.append((
        "20260907_add_wizard_runs.py: create trigger trg_no_delete_wizard_runs BEFORE DELETE (row)",
        """
        CREATE TRIGGER trg_no_delete_wizard_runs
            BEFORE DELETE ON wizard_runs
            FOR EACH ROW EXECUTE FUNCTION wizard_run_no_hard_delete()
        """,
    ))
    blocks.append((
        "20260907_add_wizard_runs.py: drop trigger trg_no_truncate_wizard_runs if exists",
        "DROP TRIGGER IF EXISTS trg_no_truncate_wizard_runs ON wizard_runs",
    ))
    blocks.append((
        "20260907_add_wizard_runs.py: create trigger trg_no_truncate_wizard_runs BEFORE TRUNCATE (stmt)",
        """
        CREATE TRIGGER trg_no_truncate_wizard_runs
            BEFORE TRUNCATE ON wizard_runs
            FOR EACH STATEMENT EXECUTE FUNCTION wizard_run_no_hard_delete()
        """,
    ))

    # migrations/20260908_add_conversational_project_builder.py — function +
    # delete/truncate pair for each of the three project-builder tables.
    blocks.append((
        "20260908_add_conversational_project_builder.py: create or replace "
        "project_builder_no_hard_delete() function",
        """
        CREATE OR REPLACE FUNCTION project_builder_no_hard_delete() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'Hard delete/truncate is forbidden on % (ISO no-hard-delete); '
                'use soft-delete (set deleted_at) instead',
                TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql
        """,
    ))
    for _tbl in ("project_conversation_turns", "project_checklist_tasks",
                 "project_checkpoints"):
        for _op, _each in (("DELETE", "ROW"), ("TRUNCATE", "STATEMENT")):
            _trig = f"trg_no_{_op.lower()}_{_tbl}"
            blocks.append((
                f"20260908_add_conversational_project_builder.py: drop trigger "
                f"trg_no_{_op.lower()}_{_tbl} if exists",
                f"DROP TRIGGER IF EXISTS {_trig} ON {_tbl}",
            ))
            blocks.append((
                f"20260908_add_conversational_project_builder.py: create trigger "
                f"{_trig} BEFORE {_op} ({_each.lower()})",
                f"""
                CREATE TRIGGER {_trig}
                    BEFORE {_op} ON {_tbl}
                    FOR EACH {_each} EXECUTE FUNCTION project_builder_no_hard_delete()
                """,
            ))

    # migrations/20261002g_plan_tiers.py — guard function + update/delete pair
    # (verbatim from _AUDIT_DDL; its `CREATE TABLE IF NOT EXISTS` half is left
    # to create_all, which owns the model).
    blocks.append((
        "20261002g_plan_tiers.py: create or replace plan_tier_assignments_no_mutate() + trigger pair",
        """
        CREATE OR REPLACE FUNCTION plan_tier_assignments_no_mutate()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Operation % is forbidden on append-only table % (ISO no-hard-delete)',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'feature_not_supported';
        END;
        $$ LANGUAGE plpgsql;
        DROP TRIGGER IF EXISTS trg_no_update_plan_tier_assignments ON plan_tier_assignments;
        CREATE TRIGGER trg_no_update_plan_tier_assignments
            BEFORE UPDATE ON plan_tier_assignments
            FOR EACH ROW EXECUTE FUNCTION plan_tier_assignments_no_mutate();
        DROP TRIGGER IF EXISTS trg_no_delete_plan_tier_assignments ON plan_tier_assignments;
        CREATE TRIGGER trg_no_delete_plan_tier_assignments
            BEFORE DELETE ON plan_tier_assignments
            FOR EACH ROW EXECUTE FUNCTION plan_tier_assignments_no_mutate();
        """,
    ))
    return blocks


# ---------------------------------------------------------------------------
# 1794b — the migration-only CHECK/FK constraints + functional/unique indexes
# ---------------------------------------------------------------------------
def _migration_constraint_blocks() -> list[tuple[str, str]]:
    """(label, sql) for every migration-only constraint/index 1794b hoists.

    Every block is idempotent as written: constraints are wrapped in the same
    `DO $$ IF NOT EXISTS (pg_constraint ...) $$` guards their migration file
    uses, indexes use `CREATE [UNIQUE] INDEX IF NOT EXISTS`. CHECKs are added
    `NOT VALID` (so an existing live DB with legacy rows never fails boot) and
    VALIDATEd in a SEPARATE statement/savepoint — a failed validate is only a
    WARNING. Verbatim SQL from the cited migration file unless noted.
    """
    blocks: list[tuple[str, str]] = []

    def _add_check(table: str, name: str, expr: str, src: str) -> None:
        """ADD CONSTRAINT ... NOT VALID (guarded) + a separate VALIDATE."""
        blocks.append((
            f"{src}: ADD CONSTRAINT {name} CHECK ... NOT VALID",
            f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}') THEN
        ALTER TABLE {table}
            ADD CONSTRAINT {name}
            CHECK ({expr}) NOT VALID;
    END IF;
END
$$;
""",
        ))
        blocks.append((
            f"{src}: VALIDATE CONSTRAINT {name}",
            f"""
ALTER TABLE {table} VALIDATE CONSTRAINT {name};
""",
        ))

    # migrations/20261012y_fortress_63_dp_budget_ledger.py — the period-shape
    # CHECK lives in that migration's CREATE TABLE, which create_all never
    # runs; models.py documents it as deliberately omitted from the ORM
    # (Postgres-only `~`), so it is migration-only.
    _add_check(
        "dp_budget_ledger", "ck_dp_budget_ledger_period_shape",
        "period ~ '^[0-9]{4}-[0-9]{2}$'",
        "20261012y_fortress_63_dp_budget_ledger.py",
    )
    # migrations/20261012u_fortress_36c_output_scan_exemptions.py — both CHECKs
    # are listed in models.py's "enforced by the migration but NOT reproduced"
    # docstring for OutputScanExemption.
    _add_check(
        "output_scan_exemptions", "ck_output_scan_exemptions_reason_required",
        "LENGTH (BTRIM (reason)) > 0",
        "20261012u_fortress_36c_output_scan_exemptions.py",
    )
    _add_check(
        "output_scan_exemptions", "ck_output_scan_exemptions_dual_control",
        "approved_by_user_id IS NULL OR created_by_user_id IS NULL\n"
        "               OR approved_by_user_id <> created_by_user_id OR self_approved",
        "20261012u_fortress_36c_output_scan_exemptions.py",
    )

    # app_id FKs — models.py keeps app_id a plain Integer on all three tables
    # ("GX.A2 will add: ForeignKey(...)"), so the FK is migration-only. `apps`
    # IS an ORM table, so these apply on a fresh boot.
    blocks.append((
        "20261001_apps_scoped_keys.py: fk_email_messages_app_id",
        """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'email_messages' AND column_name = 'app_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'email_messages'::regclass
          AND c.contype = 'f'
          AND c.confrelid = 'apps'::regclass
    ) THEN
        ALTER TABLE email_messages
            ADD CONSTRAINT fk_email_messages_app_id
            FOREIGN KEY (app_id) REFERENCES apps(id);
    END IF;
END
$$;
""",
    ))
    blocks.append((
        "20261001_apps_scoped_keys.py: fk_tenant_llm_calls_app_id",
        """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenant_llm_calls' AND column_name = 'app_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'tenant_llm_calls'::regclass
          AND c.contype = 'f'
          AND c.confrelid = 'apps'::regclass
    ) THEN
        ALTER TABLE tenant_llm_calls
            ADD CONSTRAINT fk_tenant_llm_calls_app_id
            FOREIGN KEY (app_id) REFERENCES apps(id);
    END IF;
END
$$;
""",
    ))
    blocks.append((
        "20261002_http_triggers.py: http_triggers_app_id_fkey",
        _HTTP_TRIGGERS_APP_FK_SQL,
    ))

    # users.last_sso_idp_id FK (migrations/20260605_add_jit_provisioning_columns.py)
    # — models.py keeps it a plain Integer because `tenant_idp_configs` is raw-SQL
    # only, so the FK is migration-only and is a WARNING + skip on a DB where that
    # table was never created.
    blocks.append((
        "20260605_add_jit_provisioning_columns.py: fk_users_last_sso_idp_id",
        """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_users_last_sso_idp_id'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT fk_users_last_sso_idp_id
            FOREIGN KEY (last_sso_idp_id)
            REFERENCES tenant_idp_configs(id) ON DELETE SET NULL;
    END IF;
END
$$;
""",
    ))

    # Unique / functional indexes the ORM cannot express (COALESCE keys,
    # JSONB-extraction keys, partial predicates).
    blocks.extend((
        # migrations/20261012y_fortress_63_dp_budget_ledger.py (verbatim)
        (
            "20261012y_fortress_63_dp_budget_ledger.py: uq_dp_budget_caps_tenant_workflow",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_dp_budget_caps_tenant_workflow
    ON dp_budget_caps (tenant_id, COALESCE(workflow_id, 0));
""",
        ),
        # migrations/20261012u_fortress_36c_output_scan_exemptions.py (verbatim)
        (
            "20261012u_fortress_36c_output_scan_exemptions.py: uq_output_scan_exemptions_live_key",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_output_scan_exemptions_live_key
    ON output_scan_exemptions (tenant_id, COALESCE(workflow_id, 0),
                 COALESCE(node_kind, ''), COALESCE(scanner, ''),
                 COALESCE(finding_code, ''))
    WHERE deleted_at IS NULL;
""",
        ),
        # migrations/20260523_variant_verification.py — verbatim except the
        # migration's python `_index_exists` guard, replaced by the equivalent
        # `IF NOT EXISTS` clause.
        (
            "20260523_variant_verification.py: uq_nvv_build_spec_variant",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_nvv_build_spec_variant
    ON node_variant_verification (build_spec_id, variant)
    WHERE build_spec_id IS NOT NULL;
""",
        ),
        (
            "20260523_variant_verification.py: uq_nvv_node_def_variant",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_nvv_node_def_variant
    ON node_variant_verification (node_definition_id, variant)
    WHERE node_definition_id IS NOT NULL;
""",
        ),
        # migrations/20260426_expand_workflow_trigger_types.py (verbatim)
        (
            "20260426_expand_workflow_trigger_types.py: idx_workflow_triggers_completion_source",
            """
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_completion_source
ON workflow_triggers ((configuration->>'source_workflow_id'), tenant_id)
WHERE trigger_type = 'WORKFLOW_COMPLETION'
  AND deleted_at IS NULL
  AND is_active = true
""",
        ),
        (
            "20260426_expand_workflow_trigger_types.py: idx_workflow_triggers_file_bucket",
            """
CREATE INDEX IF NOT EXISTS idx_workflow_triggers_file_bucket
ON workflow_triggers ((configuration->>'bucket'))
WHERE trigger_type = 'FILE_EVENT'
  AND deleted_at IS NULL
  AND is_active = true
""",
        ),
        # migrations/20261011g_vector_search_embeddings.py — the table itself is
        # created only by 20261011a (not the ORM), so on a create_all-only DB this
        # is a WARNING + skip; on a migrated DB it lands.
        (
            "20261011g_vector_search_embeddings.py: ix_vse_tenant_collection_live",
            """
CREATE INDEX IF NOT EXISTS ix_vse_tenant_collection_live
    ON vector_search_embeddings (tenant_id, collection) WHERE deleted_at IS NULL;
""",
        ),
        # --- 1665z5: unique indexes a fresh CE install never gets -------------
        # 1665z (Gap AB, 12 platform_settings rows for 6 keys) proved that a
        # model-side `Index(...)` written OUTSIDE `__table_args__` is evaluated
        # and discarded, so create_all emits nothing for it; and that a UNIQUE
        # constraint written only in a migration's CREATE TABLE is never replayed.
        # Every block below is a name that exists in migrations/ but is emitted by
        # NEITHER create_all nor this function. All are dup-tolerant the same way
        # the neighbouring blocks are: `IF NOT EXISTS` + the caller's per-statement
        # savepoint, so a DB that already holds duplicate rows logs a WARNING and
        # skips — boot never fails and NO row is deleted to make an index fit
        # (ISO 27001). The index is then re-attempted on the next boot, after the
        # 1665z seed heal has deduplicated.
        (
            "20260929_platform_settings.py: uq_platform_settings_live",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_settings_live
    ON platform_settings (key, scope, COALESCE(tenant_id, 0))
    WHERE deleted_at IS NULL;
""",
        ),
        (
            "20261001_apps_scoped_keys.py: uq_apps_hostname",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_apps_hostname
    ON apps (hostname) WHERE deleted_at IS NULL;
""",
        ),
        (
            "20261005j_fx114_service_accounts.py: uq_service_accounts_tenant_name",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_accounts_tenant_name
    ON service_accounts (tenant_id, name) WHERE deleted_at IS NULL;
""",
        ),
        (
            "20260920_add_stack_profiles.py: uq_stack_profiles_stack_name",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stack_profiles_stack_name
    ON stack_profiles (stack_name);
""",
        ),
        (
            "20261002_stack_variables.py: uq_stack_secret_index_live_path",
            """
CREATE UNIQUE INDEX IF NOT EXISTS uq_stack_secret_index_live_path
    ON stack_secret_index (tenant_id, "path") WHERE deleted_at IS NULL;
""",
        ),
        (
            "20261012m_adversary_findings_run_unique.py: ux_adversary_findings_workflow_run",
            """
CREATE UNIQUE INDEX IF NOT EXISTS ux_adversary_findings_workflow_run
    ON adversary_findings (workflow_run_id)
    WHERE workflow_run_id IS NOT NULL AND deleted_at IS NULL;
""",
        ),
        (
            "20260507_calibration_plans_table.py: ux_calibration_plans_running_per_tenant",
            """
CREATE UNIQUE INDEX IF NOT EXISTS ux_calibration_plans_running_per_tenant
    ON calibration_plans (tenant_id) WHERE status = 'running';
""",
        ),
        # UNIQUE constraints that live only in a migration's CREATE TABLE (the ORM
        # declares no UniqueConstraint, so create_all emits nothing). A DB holding
        # duplicate rows fails here — WARNING + skip, same as the CHECK VALIDATEs.
        (
            "20261003i_erasure_ledger.py: uq_erasure_ledger_request_id",
            """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_erasure_ledger_request_id') THEN
        ALTER TABLE erasure_ledger
            ADD CONSTRAINT uq_erasure_ledger_request_id
            UNIQUE (request_id);
    END IF;
END
$$;
""",
        ),
        (
            "20260507_ai_scheduler_telemetry_schema.py: uq_worker_system_state_license",
            """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_worker_system_state_license') THEN
        ALTER TABLE worker_system_state
            ADD CONSTRAINT uq_worker_system_state_license
            UNIQUE (worker_license_id);
    END IF;
END
$$;
""",
        ),
    ))
    return blocks


_HTTP_TRIGGERS_APP_FK_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'apps')
       AND NOT EXISTS (SELECT 1 FROM information_schema.table_constraints
                       WHERE constraint_name = 'http_triggers_app_id_fkey')
    THEN
        ALTER TABLE http_triggers
            ADD CONSTRAINT http_triggers_app_id_fkey
            FOREIGN KEY (app_id) REFERENCES apps(id) ON DELETE SET NULL;
    END IF;
END
$$;
"""


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
_PG_OWNER_ROLE = os.getenv("POSTGRES_USER", "modTech")


def apply_append_only_guards(engine) -> dict:
    """Replay the migration-only append-only triggers onto a (fresh) DB.

    Called by `database.py:_run_db_bootstrap_ddl` right after `_migrate_schema`,
    gated by the `mod_schema_version` marker (bump `_SCHEMA_MIGRATION_VERSION`
    so existing DBs get it once at the next restart). Idempotent regardless:
    every statement is `CREATE OR REPLACE FUNCTION` or
    `DROP TRIGGER IF EXISTS` + `CREATE TRIGGER`.

    Each statement runs in its OWN transaction/SAVEPOINT. A failure — typically
    the table being absent because a paid model isn't created in CE — is logged
    as a WARNING naming the table and skipped. Boot never fails because of this.

    Returns a summary dict for logging/tests: {'applied': n, 'failed': [(label,
    err)], 'statements': total}.
    """
    from sqlalchemy import text as sa_text

    stmts = _load_imported_statements() + _inline_blocks()
    summary = {"applied": 0, "failed": [], "statements": len(stmts)}

    for label, sql in stmts:
        try:
            with engine.begin() as conn:
                # Best-effort elevation to the static owner role, in the SAME
                # transaction (a failed SET LOCAL ROLE rolls back only the
                # nested savepoint, so the rest of the transaction survives).
                if _PG_OWNER_ROLE:
                    # Same savepoint pattern as database.py:_run_migration — a
                    # failed SET LOCAL ROLE poisons the whole transaction in
                    # Postgres, so run it nested and roll back only that.
                    try:
                        with conn.begin_nested():
                            conn.execute(sa_text(f'SET LOCAL ROLE "{_PG_OWNER_ROLE}"'))
                    except Exception:
                        pass  # best-effort; continue as current role
                conn.execute(sa_text(sql))
            summary["applied"] += 1
        except Exception as e:  # noqa: BLE001 - boot must never fail on a guard
            summary["failed"].append((label, str(e)[:200]))
            log.warning(
                "append-only guards: statement for table(s) %s failed (non-fatal): %s",
                _tables_named(sql), str(e)[:200],
            )
    return summary


def apply_migration_constraints(engine) -> dict:
    """Replay the migration-only CHECK/FK constraints + functional indexes.

    1794b, the second half of 1794's fix for 1791: `create_all` cannot express
    Postgres regex CHECKs, COALESCE/partial UNIQUE indexes or JSONB-extraction
    indexes, and a fresh install never runs `migrations/`, so a fresh CE/SaaS/on-
    prem DB ships `dp_budget_ledger` that accepts a bogus `period`, `dp_budget_caps`
    with duplicate (tenant, workflow) cap rows, and app_id columns with no FK.

    Called by `database.py:_run_db_bootstrap_ddl` right after
    `apply_append_only_guards`, gated by the `mod_schema_version` marker
    (`_SCHEMA_MIGRATION_VERSION` bumped again). Idempotent regardless: every
    constraint is wrapped in a `DO $$ IF NOT EXISTS (pg_constraint ...) $$`
    guard and every index is `CREATE [UNIQUE] INDEX IF NOT EXISTS`.

    CHECKs are added `NOT VALID` first and VALIDATEd in a SEPARATE statement, so
    an existing live DB that holds legacy rows never fails boot — the failed
    VALIDATE is just a WARNING. Each statement gets its OWN SAVEPOINT/
    transaction; a failure (typically the table being absent because a paid
    model isn't created in CE) is logged as a WARNING naming the table and
    skipped. API boot must NEVER fail because of this, and NO row is ever
    deleted to make an index fit (ISO 27001).

    Returns the same summary dict as apply_append_only_guards.
    """
    from sqlalchemy import text as sa_text

    stmts = _migration_constraint_blocks()
    summary = {"applied": 0, "failed": [], "statements": len(stmts)}

    for label, sql in stmts:
        try:
            with engine.begin() as conn:
                if _PG_OWNER_ROLE:
                    # Same savepoint pattern as database.py:_run_migration — a
                    # failed SET LOCAL ROLE poisons the whole transaction.
                    try:
                        with conn.begin_nested():
                            conn.execute(sa_text(f'SET LOCAL ROLE "{_PG_OWNER_ROLE}"'))
                    except Exception:
                        pass  # best-effort; continue as current role
                conn.execute(sa_text(sql))
            summary["applied"] += 1
        except Exception as e:  # noqa: BLE001 - boot must never fail on a constraint
            summary["failed"].append((label, str(e)[:200]))
            log.warning(
                "migration constraints: statement for table(s) %s failed (non-fatal): %s",
                _tables_named(sql), str(e)[:200],
            )
    return summary


# ---------------------------------------------------------------------------
# 1811 — migration-only TABLES (the CREATE TABLE DDL create_all never runs)
# ---------------------------------------------------------------------------
# 1794 proved a fresh install bootstraps via create_all + _migrate_schema and
# NEVER runs migrations/. 1794b then replayed the migration-only CHECK/FK/index
# objects — but it deliberately skipped the TABLES those migrations create, so
# on a create_all-only DB every constraint/index aimed at one of them is a
# WARNING + skip (1794b's own test says exactly that about tenant_idp_configs
# and vector_search_embeddings). Inventory (migrations CREATE TABLEs minus
# __tablename__s minus database.py's inline _migrate_schema CREATEs) yields 17
# tables with no ORM model and no inline creator; SSO config, SCIM and vector
# search are therefore broken on every fresh CE install.
#
# This hoists each table's `CREATE TABLE IF NOT EXISTS` DDL — copied VERBATIM
# from the migration that creates it (file cited above each block) — plus that
# migration's indexes, so the bootstrap lands the table and 1794b's FKs/indexes
# then attach to it. No ORM model is added (the brief forbids it): these stay
# raw-SQL tables, exactly as their migrations leave them on a migrated DB.
#
# Partitioned parents (sso_login_audit, scim_activity_events, the two wizard
# event tables) also get their live-month partitions seeded — a partitioned
# parent with no partition rejects INSERT, so landing the parent alone would
# leave SSO login audit / SCIM activity still broken.
def _pgvector_ready(engine) -> bool:
    """True when the `vector` contrib actually loaded for this role.

    Same semantics as migrations/20261011a_vector_search_embeddings.py:
    `pg_available_extensions` only means the contrib is installed, not that
    this (Vault-rotated, ephemeral) role may CREATE EXTENSION, so the probe
    attempts it and downgrades to the JSONB embedding column on failure.
    """
    from sqlalchemy import text as sa_text

    try:
        with engine.begin() as conn:
            conn.execute(sa_text("CREATE EXTENSION IF NOT EXISTS vector;"))
        return True
    except Exception:  # noqa: BLE001 - no pgvector: JSONB embedding fallback
        return False


def _monthly_partitions(table: str, name_fmt: str, months: int = 3) -> list[str]:
    """CREATE TABLE IF NOT EXISTS ... PARTITION OF for the live months.

    Mirrors the migrations' `_seed_partitions` (current month + 2 future ones):
    20260605_add_scim_activity_events.py, 20260605_add_sso_login_audit.py and
    20260508_add_wizard_*_table.py. Idempotent; a bootstrap that runs a month
    later just adds the new month.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    out: list[str] = []
    for i in range(months):
        m = now.month + i
        y = now.year + (m - 1) // 12
        m = ((m - 1) % 12) + 1
        nm, ny = m + 1, y
        if nm > 12:
            nm, ny = 1, ny + 1
        out.append(
            f"CREATE TABLE IF NOT EXISTS {name_fmt.format(y=y, m=m)}"
            f" PARTITION OF {table}"
            f" FOR VALUES FROM ('{y:04d}-{m:02d}-01')"
            f" TO ('{ny:04d}-{nm:02d}-01');"
        )
    return out


# CE-SSO-SCIM-1 (owner 11-15: SSO/SCIM is OUT of CE — the 12 tables are removed
# from CE and every reader guards on table existence). The CE cut ships none
# of them, so the bootstrap must not create them either. This flag is the ONE
# switch: the source keeps `True` (paid keeps its behaviour/wording), and the
# open-core/manifest.yaml scrub row flips it to `False` in the CE COPY of this
# file only. Shared code, shipped to paid unchanged.
_SSO_SCIM_TABLES_SHIPPED = False


def _sso_scim_blocks_omitted(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """CE-SSO-SCIM-1: the CE cut ships none of the 12 SSO/SCIM tables.

    owner 11-15: SSO/SCIM is OUT of CE — the tables must not be created by the
    bootstrap either, or a CE install carries 12 empty tables for a feature it
    does not ship. Their CREATE blocks are dropped here (the ONLY place they
    are replayed), so `apply_migration_tables` lands the rest (vector search,
    the wizard events, release_steps, plan tiers, ...) and skips the SSO/SCIM
    group wholesale.

    The drop is by SOURCE MIGRATION (the label prefix every block cites): the
    8 migrations that create the 12 tables also carry their indexes and
    live-month partitions, so dropping the whole group drops the CREATEs, the
    indexes and the partitions together. Blocks that merely REFERENCE one of
    the tables (1794b's `fk_users_last_sso_idp_id` DO-block, whose ALTER is
    already wrapped in an existence guard) stay and are a no-op on a DB where
    the table was never created — same savepoint WARNING + skip semantics the
    other appliers already log.

    Paid is unchanged: the helper is a no-op there because the module flag
    `_SSO_SCIM_TABLES_SHIPPED` stays True in the source (paid keeps its
    wording/behaviour); the manifest scrub row flips it to False in the CE
    COPY of this file only. Shared code, shipped to paid unchanged.
    """
    if _SSO_SCIM_TABLES_SHIPPED:
        return blocks
    # The 8 migrations whose CREATEs make up the 12 tables (all 8 are denied
    # from the CE tree by open-core/manifest.yaml SSO-1 rows, so on a CE copy
    # these blocks are the only reason the tables would exist at all).
    sso_scim_migrations = (
        "20260605_add_sso_idp_configs.py",
        "20260605_add_sso_verified_domains.py",
        "20260605_add_sso_idp_domain_associations.py",
        "20260605_add_sso_login_audit.py",
        "20260605_add_scim_tokens.py",
        "20260605_add_scim_groups.py",
        "20260605_add_scim_idempotency_records.py",
        "20260605_add_scim_activity_events.py",
    )
    kept: list[tuple[str, str]] = []
    for label, sql in blocks:
        stem = label.split(":", 1)[0].strip()
        if stem in sso_scim_migrations:
            continue
        kept.append((label, sql))
    return kept


def _migration_table_blocks(has_pgvector: bool) -> list[tuple[str, str]]:
    """(label, sql) for every migration-only table 1811 hoists.

    DDL copied VERBATIM from the cited migration (only additions: `IF NOT
    EXISTS` on the two wizard parents, whose migrations guard the CREATE with
    a python `_table_exists()` check instead, and the partition seeds). FK
    targets come first: tenant_idp_configs + tenant_verified_domains before
    tenant_idp_domain_associations, scim_groups before its memberships/mappings.
    """
    blocks: list[tuple[str, str]] = []

    # migrations/20260605_add_sso_idp_configs.py — SSO IdP configs (raw SQL in
    # crud/sso_idp.py, no ORM model).
    blocks.append((
        "20260605_add_sso_idp_configs.py: tenant_idp_configs",
        """
CREATE TABLE IF NOT EXISTS tenant_idp_configs (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    protocol VARCHAR(8) NOT NULL CHECK (protocol IN ('saml', 'oidc')),
    keycloak_idp_alias VARCHAR(64) NOT NULL,
    metadata_url VARCHAR(2048),
    metadata_xml TEXT,
    state VARCHAR(16) NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'active', 'inactive')),
    is_active BOOLEAN NOT NULL DEFAULT false,
    last_test_run_at TIMESTAMP WITH TIME ZONE,
    last_test_result_json JSON,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name),
    UNIQUE (keycloak_idp_alias)
);
""",
    ))
    blocks.append((
        "20260605_add_sso_idp_configs.py: ix_tenant_idp_configs_tenant_active",
        """
CREATE INDEX IF NOT EXISTS ix_tenant_idp_configs_tenant_active
    ON tenant_idp_configs (tenant_id, is_active);
""",
    ))

    # migrations/20260605_add_sso_verified_domains.py
    blocks.append((
        "20260605_add_sso_verified_domains.py: tenant_verified_domains",
        """
CREATE TABLE IF NOT EXISTS tenant_verified_domains (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    domain VARCHAR(255) NOT NULL,
    verification_token VARCHAR(64) NOT NULL,
    verification_state VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (verification_state IN ('pending', 'verified', 'failed')),
    verification_started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_verified_at TIMESTAMP WITH TIME ZONE,
    last_check_at TIMESTAMP WITH TIME ZONE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_resolver_responses_json JSON,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (domain)
);
""",
    ))
    blocks.append((
        "20260605_add_sso_verified_domains.py: tenant_verified_domains indexes",
        """
CREATE INDEX IF NOT EXISTS ix_tenant_verified_domains_tenant
    ON tenant_verified_domains (tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_verified_domains_active_lookup
    ON tenant_verified_domains (domain)
    WHERE verification_state = 'verified';
CREATE INDEX IF NOT EXISTS ix_tenant_verified_domains_sweep
    ON tenant_verified_domains (verification_state, last_check_at);
""",
    ))

    # migrations/20260605_add_sso_idp_domain_associations.py — FKs into the two
    # tables above, so it must come after them.
    blocks.append((
        "20260605_add_sso_idp_domain_associations.py: tenant_idp_domain_associations",
        """
CREATE TABLE IF NOT EXISTS tenant_idp_domain_associations (
    idp_config_id INTEGER NOT NULL
        REFERENCES tenant_idp_configs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL
        REFERENCES tenant_verified_domains(id) ON DELETE CASCADE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (idp_config_id, domain_id)
);
CREATE INDEX IF NOT EXISTS ix_idp_domain_assoc_by_domain
    ON tenant_idp_domain_associations (domain_id);
""",
    ))

    # migrations/20260605_add_sso_login_audit.py — partitioned parent + live
    # month partitions (a parent with no partition rejects INSERT).
    blocks.append((
        "20260605_add_sso_login_audit.py: sso_login_audit parent",
        """
CREATE TABLE IF NOT EXISTS sso_login_audit (
    id BIGSERIAL,
    tenant_id INTEGER NOT NULL,
    user_id INTEGER,
    event_type VARCHAR(64) NOT NULL,
    idp_alias VARCHAR(64),
    flow_id VARCHAR(64),
    metadata_json JSON,
    source_ip VARCHAR(45),
    user_agent VARCHAR(512),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
""",
    ))
    blocks.append((
        "20260605_add_sso_login_audit.py: sso_login_audit indexes",
        """
CREATE INDEX IF NOT EXISTS ix_sso_login_audit_tenant_time
    ON sso_login_audit (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_sso_login_audit_flow
    ON sso_login_audit (flow_id)
    WHERE flow_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_sso_login_audit_tenant_event
    ON sso_login_audit (tenant_id, event_type, created_at DESC);
""",
    ))
    for _sql in _monthly_partitions("sso_login_audit", "sso_login_audit_{y:04d}{m:02d}"):
        blocks.append(("20260605_add_sso_login_audit.py: sso_login_audit partition", _sql))

    # migrations/20260605_add_scim_tokens.py
    blocks.append((
        "20260605_add_scim_tokens.py: scim_tokens",
        """
CREATE TABLE IF NOT EXISTS scim_tokens (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    vault_path VARCHAR(255) NOT NULL,
    ip_allowlist JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INTEGER NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT,
    rotated_at TIMESTAMPTZ NULL,
    revoked_at TIMESTAMPTZ NULL,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_scim_tokens_tenant_revoked
    ON scim_tokens (tenant_id, revoked_at);
CREATE INDEX IF NOT EXISTS ix_scim_tokens_tenant_created
    ON scim_tokens (tenant_id, created_at DESC);
""",
    ))

    # migrations/20260605_add_scim_groups.py — three tables + indexes.
    blocks.append((
        "20260605_add_scim_groups.py: scim_groups",
        """
CREATE TABLE IF NOT EXISTS scim_groups (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    external_id VARCHAR(255) NULL,
    display_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL,
    UNIQUE (tenant_id, display_name)
);
""",
    ))
    blocks.append((
        "20260605_add_scim_groups.py: scim_group_memberships",
        """
CREATE TABLE IF NOT EXISTS scim_group_memberships (
    group_id BIGINT NOT NULL REFERENCES scim_groups(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (group_id, user_id)
);
""",
    ))
    blocks.append((
        "20260605_add_scim_groups.py: scim_group_role_mappings",
        """
CREATE TABLE IF NOT EXISTS scim_group_role_mappings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    scim_group_display_name VARCHAR(255) NOT NULL,
    mod_permission_template_id INTEGER NULL,
    effective_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE (tenant_id, scim_group_display_name, effective_from)
);
""",
    ))
    blocks.append((
        "20260605_add_scim_groups.py: scim indexes",
        """
CREATE INDEX IF NOT EXISTS ix_scim_groups_tenant_external
    ON scim_groups (tenant_id, external_id)
    WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_scim_group_memberships_user
    ON scim_group_memberships (user_id);
CREATE INDEX IF NOT EXISTS ix_scim_role_mappings_tenant_name_eff
    ON scim_group_role_mappings (tenant_id, scim_group_display_name,
                                 effective_from DESC);
""",
    ))

    # migrations/20260605_add_scim_idempotency_records.py
    blocks.append((
        "20260605_add_scim_idempotency_records.py: scim_idempotency_records",
        """
CREATE TABLE IF NOT EXISTS scim_idempotency_records (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    response_status INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, key_hash)
);
CREATE INDEX IF NOT EXISTS ix_scim_idempotency_expires
    ON scim_idempotency_records (expires_at);
""",
    ))

    # migrations/20260605_add_scim_activity_events.py — partitioned parent +
    # webhooks + archives + live partitions.
    blocks.append((
        "20260605_add_scim_activity_events.py: scim_activity_events parent",
        """
CREATE TABLE IF NOT EXISTS scim_activity_events (
    id BIGSERIAL,
    tenant_id INTEGER NOT NULL,
    scim_token_id BIGINT NULL,
    method VARCHAR(8) NOT NULL,
    path VARCHAR(255) NOT NULL,
    external_id VARCHAR(255) NULL,
    request_body JSONB NULL,
    response_status INTEGER NOT NULL,
    response_body JSONB NULL,
    error_detail VARCHAR(255) NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    client_ip VARCHAR(45) NULL,
    user_agent VARCHAR(512) NULL,
    idempotency_replayed BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
""",
    ))
    blocks.append((
        "20260605_add_scim_activity_events.py: scim_activity_events indexes",
        """
CREATE INDEX IF NOT EXISTS ix_scim_activity_tenant_time
    ON scim_activity_events (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_scim_activity_tenant_status
    ON scim_activity_events (tenant_id, response_status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_scim_activity_external_id
    ON scim_activity_events (external_id)
    WHERE external_id IS NOT NULL;
""",
    ))
    for _sql in _monthly_partitions("scim_activity_events", "scim_activity_events_{y:04d}_{m:02d}"):
        blocks.append(("20260605_add_scim_activity_events.py: scim_activity_events partition", _sql))
    blocks.append((
        "20260605_add_scim_activity_events.py: scim_failure_webhooks",
        """
CREATE TABLE IF NOT EXISTS scim_failure_webhooks (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL UNIQUE REFERENCES tenants(id) ON DELETE CASCADE,
    url VARCHAR(2048) NOT NULL,
    last_fired_at TIMESTAMPTZ NULL,
    last_validated_at TIMESTAMPTZ NULL,
    last_validation_ok BOOLEAN NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ))
    blocks.append((
        "20260605_add_scim_activity_events.py: scim_activity_archives",
        """
CREATE TABLE IF NOT EXISTS scim_activity_archives (
    id BIGSERIAL PRIMARY KEY,
    partition_name VARCHAR(255) NOT NULL UNIQUE,
    s3_object_key VARCHAR(2048) NOT NULL,
    row_count BIGINT NOT NULL,
    bytes BIGINT NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
""",
    ))

    # migrations/20261011a_vector_search_embeddings.py — the embedding column
    # branches on pgvector exactly as the migration does.
    blocks.append((
        "20261011a_vector_search_embeddings.py: vector_search_embeddings",
        f"""
CREATE TABLE IF NOT EXISTS vector_search_embeddings (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants (id),
    collection VARCHAR(100) NOT NULL,
    document_id VARCHAR(255) NOT NULL,
    {"embedding VECTOR(768) NOT NULL" if has_pgvector else "embedding JSONB NOT NULL"},
    content TEXT,
    metadata JSONB,
    deleted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_vector_search_embeddings_tenant_collection
    ON vector_search_embeddings (tenant_id, collection);
"""
        + ("""
CREATE INDEX IF NOT EXISTS ix_vector_search_embeddings_embedding_ivfflat
    ON vector_search_embeddings (embedding vector_cosine_ops)
    WITH (lists = 100);
""" if has_pgvector else ""),
    ))

    # migrations/20260508_add_wizard_feedback_event_table.py — the migration
    # guards the CREATE with a python _table_exists() check, so IF NOT EXISTS
    # is added here; everything else verbatim.
    blocks.append((
        "20260508_add_wizard_feedback_event_table.py: wizard_feedback_event parent",
        """
CREATE TABLE IF NOT EXISTS wizard_feedback_event (
    id BIGSERIAL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proposal_id INTEGER NOT NULL,
    apply_mode TEXT NOT NULL CHECK (apply_mode IN ('replace', 'merge')),
    action TEXT NOT NULL CHECK (
        action = 'accepted'
        OR action = 'dismissed_without_action'
        OR action LIKE 'rejected_%'
    ),
    was_edited BOOLEAN NOT NULL DEFAULT FALSE,
    time_to_action_ms INTEGER,
    applied_node_ids_diff JSONB,
    intent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    feedback_event_metadata JSONB,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
""",
    ))
    blocks.append((
        "20260508_add_wizard_feedback_event_table.py: wizard_feedback_event indexes",
        """
CREATE INDEX IF NOT EXISTS ix_wfe_tenant ON wizard_feedback_event(tenant_id);
CREATE INDEX IF NOT EXISTS ix_wfe_action ON wizard_feedback_event(action);
CREATE INDEX IF NOT EXISTS ix_wfe_proposal ON wizard_feedback_event(proposal_id);
CREATE INDEX IF NOT EXISTS ix_wfe_created_at ON wizard_feedback_event(created_at);
""",
    ))
    for _sql in _monthly_partitions("wizard_feedback_event", "wizard_feedback_event_y{y}m{m:02d}"):
        blocks.append(("20260508_add_wizard_feedback_event_table.py: wizard_feedback_event partition", _sql))

    # migrations/20260508_add_wizard_finding_action_event_table.py
    blocks.append((
        "20260508_add_wizard_finding_action_event_table.py: wizard_finding_action_event parent",
        """
CREATE TABLE IF NOT EXISTS wizard_finding_action_event (
    id BIGSERIAL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    audit_session_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('error', 'warning', 'info')),
    action TEXT NOT NULL CHECK (
        action IN ('fix_via_wizard', 'open_node', 'ignore', 'dismissed_without_action')
    ),
    finding_title_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finding_event_metadata JSONB,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
""",
    ))
    blocks.append((
        "20260508_add_wizard_finding_action_event_table.py: wizard_finding_action_event indexes",
        """
CREATE INDEX IF NOT EXISTS ix_wfae_tenant ON wizard_finding_action_event(tenant_id);
CREATE INDEX IF NOT EXISTS ix_wfae_session ON wizard_finding_action_event(audit_session_id);
CREATE INDEX IF NOT EXISTS ix_wfae_finding ON wizard_finding_action_event(finding_id);
CREATE INDEX IF NOT EXISTS ix_wfae_action ON wizard_finding_action_event(action);
CREATE INDEX IF NOT EXISTS ix_wfae_created_at ON wizard_finding_action_event(created_at);
""",
    ))
    for _sql in _monthly_partitions("wizard_finding_action_event", "wizard_finding_action_event_y{y}m{m:02d}"):
        blocks.append(("20260508_add_wizard_finding_action_event_table.py: wizard_finding_action_event partition", _sql))

    # migrations/20261002f_release_steps.py — CREATE only (its append-only
    # triggers are already imported via _MIGRATION_MODULES).
    blocks.append((
        "20261002f_release_steps.py: release_steps",
        """
CREATE TABLE IF NOT EXISTS release_steps (
    id              SERIAL PRIMARY KEY,
    step_name       VARCHAR(255) NOT NULL UNIQUE,
    checksum        VARCHAR(64) NOT NULL,
    applied_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    release_version VARCHAR(64),
    duration_ms     INTEGER,
    runner_host     VARCHAR(255)
);
""",
    ))

    # migrations/20261002g_plan_tiers.py — CREATE only (its triggers are the
    # copied inline block above).
    blocks.append((
        "20261002g_plan_tiers.py: plan_tier_assignments",
        """
CREATE TABLE IF NOT EXISTS plan_tier_assignments (
    id           SERIAL PRIMARY KEY,
    plan_id      INTEGER NOT NULL,
    plan_name    VARCHAR(100) NOT NULL,
    old_tier     VARCHAR(20),
    new_tier     VARCHAR(20) NOT NULL,
    applied_by   VARCHAR(255) NOT NULL,
    applied_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
""",
    ))

    # migrations/20261026a_workflow_run_idempotency_keys.py (2400GX-n8) — raw
    # SQL table (services/run_idempotency.py, no ORM model). CE boot never runs
    # migrations, so a fresh CE needs it here or every keyed run 500s.
    blocks.append((
        "20261026a_workflow_run_idempotency_keys.py: workflow_run_idempotency_keys",
        """
CREATE TABLE IF NOT EXISTS workflow_run_idempotency_keys (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    workflow_version_id INTEGER NOT NULL,
    idem_key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    workflow_run_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wf_run_idem_scope
    ON workflow_run_idempotency_keys (tenant_id, workflow_version_id, idem_key);
CREATE INDEX IF NOT EXISTS ix_wf_run_idem_created_at
    ON workflow_run_idempotency_keys (created_at);
""",
    ))
    return _sso_scim_blocks_omitted(blocks)


def apply_migration_tables(engine) -> dict:
    """Replay the migration-only CREATE TABLE DDL onto a (fresh) DB.

    1811, the third leg of 1794: 1794b hoisted the migration-only CHECK/FK/
    index OBJECTS but not the TABLES that host them, so on a create_all-only
    DB every one of those objects targeting e.g. `tenant_idp_configs` or
    `vector_search_embeddings` was a WARNING + skip — SSO config, SCIM and
    vector search are broken on every fresh CE install. This lands the tables
    themselves (verbatim DDL, source cited per block) plus their own indexes,
    so 1794b's `fk_users_last_sso_idp_id` and `ix_vse_*` then attach.

    MUST run BEFORE `apply_migration_constraints` (FK/index targets need their
    table) and AFTER `apply_append_only_guards` is fine either way — the
    append-only triggers on release_steps / plan_tier_assignments land after
    their tables exist. Idempotent: every CREATE is `IF NOT EXISTS`, so a
    second call is a no-op. Same savepoint/owner-role pattern as the other
    two appliers: a failure is a WARNING naming the table + skip, boot never
    fails because of this.

    Returns the same summary dict as the other appliers.
    """
    from sqlalchemy import text as sa_text

    stmts = _migration_table_blocks(_pgvector_ready(engine))
    summary = {"applied": 0, "failed": [], "statements": len(stmts)}

    for label, sql in stmts:
        try:
            with engine.begin() as conn:
                if _PG_OWNER_ROLE:
                    try:
                        with conn.begin_nested():
                            conn.execute(sa_text(f'SET LOCAL ROLE "{_PG_OWNER_ROLE}"'))
                    except Exception:
                        pass  # best-effort; continue as current role
                conn.execute(sa_text(sql))
            summary["applied"] += 1
        except Exception as e:  # noqa: BLE001 - boot must never fail on a table
            summary["failed"].append((label, str(e)[:200]))
            log.warning(
                "migration tables: statement for table(s) %s failed (non-fatal): %s",
                _tables_named(sql), str(e)[:200],
            )
    return summary
