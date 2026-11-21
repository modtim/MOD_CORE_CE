"""GS.PSR.5 — declarative initial values for the DB-backed platform-settings
registry (`services/platform_settings.py`), applied once per install instead
of the installer hand-writing behavioural keys into `.env.public`.

Why: `.env.public` is meant for bootstrap/secrets-adjacent config (domains,
ports, credentials-that-aren't-secrets) — see [[settings-not-env-files]].
Behavioural tunables/feature flags belong in the `platform_settings` table so
they're admin-editable at runtime (Dashboard → Admin → Platform Settings, or
`PATCH /v1/platform/settings/{key}`) without an env-var edit + container
recreate. This module is the bridge: it gives the installer (and API startup)
a single, idempotent place to establish each registered key's *initial*
value for a fresh install, per deployment mode.

SEED_VALUES is deliberately NOT a dump of the whole registry — most keys are
fine starting from their registered `default` (or an existing `env_fallback`
for an upgrading install). A key only appears here when there's a concrete
reason its out-of-the-box value should differ from the bare registry default,
or when experience showed operators reliably forget to flip it by hand (see
each entry's comment). Everything else is left alone: no seed row, ordinary
tenant > platform > env_fallback > default resolution.

Two entry points:
  - `seed_platform_settings(db, mode)` — WRITES. For each key in
    `SEED_VALUES` that has an entry for `mode`, inserts a platform-scope DB
    row via `platform_settings.set_setting(..., actor_user_id=None)` ONLY
    when no live platform-scope row exists yet (never overrides an admin's
    own choice, and is a pure no-op on re-run once a key is seeded). Flushes;
    the caller commits — mirrors `set_setting`'s own contract.
  - `preview_seed_platform_settings(db, mode)` — READ-ONLY. One row per
    *registered* key (uses `platform_settings.list_settings`, so it always
    covers every key GS.PSR.6 has registered) showing its current
    value/source plus what the seed would do for `mode`. This is what both
    the CLI `--dry-run` and a future "preview seed" admin view render.

Both only use the public `platform_settings` surface (register/get_setting/
set_setting/clear_setting/list_settings/resolve_source) — this module never
mutates `_REGISTRY` and is safe to import regardless of what keys
`GS.PSR.6` has registered by the time it runs.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TypedDict

from services import platform_settings as psr

from sqlalchemy import text as _sa_text

logger = logging.getLogger(__name__)

# 1665z (CE gap AB) — uvicorn's 3 boot workers all run this seeder
# concurrently against a fresh DB. The check-then-insert below is not
# transaction-safe (the partial-unique live-row index cannot lock a
# not-yet-existing key), so a raced boot used to leave 2 live rows per key
# and `get_setting`'s `query.scalar()` then raised
# "Multiple rows were found when exactly one was required" -> 502 on the
# first workflow run. A transaction-scoped advisory lock serialises the
# whole seed across processes: every worker still runs (ordering unchanged),
# but each one sees the rows the previous worker committed.
#
# Constant is unique across CORE/API: the other single-integer advisory keys
# in use are 0x7a8b5c00deadbee0..4 (main.py seed wrappers,
# crud/admin_email_config.py), 0x5e115e00deadbee1 (main.py),
# 918273645 (database.py migrate lock), 782341906 (node_scanner.py) and
# 0x574B4644 (worker_tasks.py, namespaced). None collides.
_SEED_ADVISORY_LOCK_ID = 0x5EED0000DEADBEE5  # noqa: E501

MODES = ("saas", "onprem")

# key -> {"saas": value, "onprem": value}. Omit a mode (or the whole key) to
# leave that combination on ordinary registry resolution.
SEED_VALUES: Dict[str, Dict[str, Any]] = {
    # [[wizard-build-feature-flags]] — both flags are meant to be ON in every
    # deployment (deterministic BuildPlan extraction + universal per-
    # accelerator variant fan-out); they were repeatedly left off because
    # nothing enforced it beyond "remember to check .env.public after every
    # re-provision". Seeding removes the manual step entirely.
    "wizard_docs_driven_builds_enabled": {"saas": True, "onprem": True},
    "wizard_variant_fanout_universal": {"saas": True, "onprem": True},

    # Node try-before-you-buy abuse protection matters for a multi-tenant
    # marketplace (SaaS); a single/few-tenant on-prem install can run a much
    # looser cap without meaningfully increasing risk. Registry default (60)
    # stays the SaaS value; on-prem gets 10x headroom, not unlimited.
    "node_trial_rate_limit_per_hour": {"saas": 60, "onprem": 600},

    # Both are scale-driven: batching telemetry writes and routing Execute
    # orchestration through the task queue pay off once there's enough
    # concurrent tenant traffic to matter (SaaS) and add operational
    # complexity that a single on-prem manager doesn't need yet. On-prem
    # stays on the registry default (False / in-process); SaaS opts in.
    "telemetry_batching_enabled": {"saas": True, "onprem": False},
    "orchestration_via_queue_enabled": {"saas": True, "onprem": False},
}


class SeedResult(TypedDict):
    key: str
    value: Any
    mode: str
    applied: bool
    reason: str  # "seeded" | "already_set" | "unregistered"


def _normalize_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m not in MODES:
        raise ValueError(f"unknown deployment mode {mode!r}; expected one of {MODES}")
    return m


def seed_platform_settings(db, mode: str) -> List[SeedResult]:
    """Idempotently apply `SEED_VALUES[mode]` to the platform-settings
    registry.

    For each seeded key: writes a DB value ONLY when no live platform-scope
    row exists yet — an admin's own prior choice, or a value this same
    function already seeded on an earlier boot, is never touched. Writes go
    through `platform_settings.set_setting(..., actor_user_id=None)` so the
    normal soft-supersede + append-only `platform_settings_audit` row is
    reused unchanged, with a null actor recording "system" as the source of
    the change.

    A key in `SEED_VALUES` that isn't registered yet (e.g. this runs before
    the owning module's `register()` call during an in-flight migration of
    readers) is skipped with a logged warning — never a crash. In production
    `services.platform_settings` is always imported (and every module-level
    `register()` call has run) before this function is ever called.

    Caller owns the transaction: flushes (via `set_setting`) but does not
    commit.

    1665z: the advisory lock is acquired on that same caller-owned
    transaction, so it is released at the caller's commit — the whole
    heal + check-then-insert runs as one serialised unit per boot worker.
    """
    mode = _normalize_mode(mode)
    results: List[SeedResult] = []

    # 1665z — blocking, transaction-scoped: the first worker holds it until
    # its commit, the others wait, then read the rows it committed.
    db.execute(_sa_text("SELECT pg_advisory_xact_lock(:k)"),
               {"k": _SEED_ADVISORY_LOCK_ID})

    healed = heal_duplicate_platform_rows(db)
    if healed:
        logger.info("platform_settings_seed: soft-deleted %s duplicate live "
                    "platform-scope row(s) from a raced boot", healed)

    for key, by_mode in SEED_VALUES.items():
        if mode not in by_mode:
            continue
        value = by_mode[mode]

        try:
            _current_value, source = psr.resolve_source(key, db)
        except psr.SettingNotRegisteredError:
            logger.warning(
                "platform_settings_seed: skipping unregistered key %r "
                "(mode=%s) — register() must run before seeding", key, mode)
            results.append(SeedResult(key=key, value=value, mode=mode,
                                       applied=False, reason="unregistered"))
            continue

        if source == "db":
            # A live platform-scope row already exists — either an admin set
            # it, or a previous run of this same seed did. Either way it is
            # never superseded here.
            results.append(SeedResult(key=key, value=value, mode=mode,
                                       applied=False, reason="already_set"))
            continue

        psr.set_setting(key, value, db=db, actor_user_id=None)
        logger.info("platform_settings_seed: seeded %s=%r (mode=%s)",
                    key, value, mode)
        results.append(SeedResult(key=key, value=value, mode=mode,
                                   applied=True, reason="seeded"))

    # GSEC R-002 — one conditional seed that cannot be expressed as a static
    # per-mode SEED_VALUES entry: licensing.trust_local_issuer is True only
    # when this install ALREADY carries a valid local-issuer-signed license
    # (existing installs keep working until the lead flips it off); fresh
    # installs stay on the secure registry default (False).
    results.extend(_seed_local_issuer_trust(db, mode))

    # GSEC R-012 — second conditional seed: mesh.acl_enforced is False on an
    # ESTABLISHED install (workers enrolled or stacks deployed — the tailnet
    # is in use, auto-flipping to default-deny would risk the worker plane)
    # and pinned True on a FRESH one (registry default made explicit, so the
    # legacy .env.public value cannot mask it).
    results.extend(_seed_mesh_acl_enforced(db, mode))

    return results


def heal_duplicate_platform_rows(db) -> int:
    """1665z (CE gap AB) — heal a DB that already booted with the seed race.

    For every platform-scope key (`scope='platform'`, `tenant_id IS NULL`)
    carrying more than one live row, keep the earliest row (lowest `id`) and
    stamp `deleted_at` on the rest — a SOFT delete, history retained
    ([[iso-no-hard-delete]]); nothing is ever removed from the table.
    Tenant-scope rows are never touched.

    Runs inside the caller's advisory-locked transaction, so two boot workers
    cannot both heal the same set. Returns the number of rows soft-deleted.
    Values are never logged.
    """
    from models import PlatformSetting
    from sqlalchemy import func as _func

    dup_keys = [
        row[0]
        for row in db.query(
            PlatformSetting.key,
            _func.count(PlatformSetting.id),
        ).filter(
            PlatformSetting.scope == "platform",
            PlatformSetting.tenant_id.is_(None),
            PlatformSetting.deleted_at.is_(None),
        ).group_by(PlatformSetting.key).having(
            _func.count(PlatformSetting.id) > 1
        ).all()
    ]
    if not dup_keys:
        return 0

    now = psr._utcnow()
    healed = 0
    for key in dup_keys:
        rows = db.query(PlatformSetting).filter(
            PlatformSetting.key == key,
            PlatformSetting.scope == "platform",
            PlatformSetting.tenant_id.is_(None),
            PlatformSetting.deleted_at.is_(None),
        ).order_by(PlatformSetting.id.asc()).all()
        for stale in rows[1:]:
            stale.deleted_at = now
            healed += 1
    db.flush()
    return healed


def _seed_local_issuer_trust(db, mode: str) -> List[SeedResult]:
    """GSEC R-002 — one-shot conditional seed of `licensing.trust_local_issuer`.

    Writes a live platform row (True) ONLY when:
      * no live platform-scope row exists yet (an admin's own choice — or a
        previous boot's seed — is never touched), and
      * the install already carries a signed license token that verifies
        against the locally auto-generated issuer key
        (LICENSE_ISSUER_PUBLIC_KEY_B64 in the API env).

    Rationale: pre-R-002, the verifier trusted that local key unconditionally,
    so every existing install that holds a local-issuer-signed license
    (e.g. MOD's own internal license minted by tools/mint_internal_license.py
    from the box's .env.issuer-secrets) depends on it. Seeding True once at
    boot preserves that continuity. Fresh installs have no license token, so
    nothing is seeded and they stay at the secure default (False) — a box
    cannot bootstrap trust for a license it mints for itself. The lead then
    ships the vendor public key into utils/license_trust/, installs a
    vendor-signed license, and flips this setting to False.

    The probe verifies with `verify_hardware=True` (default) against a
    local-only trust set, so a vendor-signed license does NOT flip the local
    trust on. No-expiry local licenses fail the probe (no-expiry requires a
    pinned vendor signature — that is the fix) and are correctly left
    untrusted.
    """
    import os

    from utils.paid_import import optional_paid
    lv = optional_paid("utils.license_verify")
    if lv is None:
        return []  # CE: no license concept, nothing to seed

    try:
        _, source = psr.resolve_source(lv.LOCAL_ISSUER_TRUST_SETTING, db)
    except psr.SettingNotRegisteredError:
        return []
    if source == "db":
        return [SeedResult(key=lv.LOCAL_ISSUER_TRUST_SETTING, value=True,
                           mode=mode, applied=False, reason="already_set")]

    token = lv.load_license_token()
    if not token:
        logger.info("trust_local_issuer: no signed credential installed "
                    "— leaving secure default (False)")
        return []

    local_pem = lv._decode_local_issuer_key()
    if not local_pem:
        logger.info("trust_local_issuer: no local key in env "
                    "— leaving secure default (False)")
        return []
    probe_trust: Dict[str, str] = {}
    fp = os.getenv("LICENSE_ISSUER_KEY_FINGERPRINT")
    if fp:
        probe_trust[str(fp)] = local_pem
    probe_trust.setdefault("*", local_pem)

    v = lv.verify_license(token, trust_set=probe_trust)
    if not v.valid:
        logger.info("trust_local_issuer: installed credential does not "
                    "verify against the local issuer key (%s) — leaving "
                    "secure default (False)", v.reason)
        return []

    psr.set_setting(lv.LOCAL_ISSUER_TRUST_SETTING, True, db=db,
                    actor_user_id=None)
    logger.info(
        "seeded licensing.trust_local_issuer=True — install carries a valid "
        "local-issuer-signed license (GSEC R-002 bootstrap; flip to False "
        "once the vendor key + vendor-signed license are in place)")
    return [SeedResult(key=lv.LOCAL_ISSUER_TRUST_SETTING, value=True,
                       mode=mode, applied=True, reason="seeded")]


def _seed_mesh_acl_enforced(db, mode: str) -> List[SeedResult]:
    """GSEC R-012 / FX.14 — one-shot conditional seed of `mesh.acl_enforced`.

    The registry default is True (fresh installs enforce the generated
    default-deny policy from day one — safe, because apply_acl_policy's
    preflight refuses to push until the manager node actually carries the
    API tag). This install predates enforcement: when workers are ALREADY
    enrolled or stacks ALREADY deployed, the tailnet is in use and an
    auto-flip to default-deny would risk severing the worker plane, so a
    live platform row of False is written — exactly mirroring the R-031
    pattern (seed the live install's current behaviour, never a fresh one).
    On a fresh install (neither marker present) a row of True is written so
    the secure default is explicit and the legacy `MOD_MESH_ACL_ENFORCE`
    value lingering in CORE/startup/.env.public (bootstrap-only, owned
    elsewhere) cannot mask it.

    Never touches a live row: an admin's own flip — or a previous boot's
    seed — wins, making this a no-op for the rest of the install's life.
    """
    key = "mesh.acl_enforced"
    try:
        _, source = psr.resolve_source(key, db)
    except psr.SettingNotRegisteredError:
        return []
    if source == "db":
        return [SeedResult(key=key, value=True, mode=mode, applied=False,
                           reason="already_set")]

    from models import PersistentStack, WorkerLicense
    try:
        established = (db.query(WorkerLicense.id).first() is not None
                       or db.query(PersistentStack.id).first() is not None)
    except Exception:  # noqa: BLE001 — unreadable markers read as established
        logger.warning("mesh.acl_enforced: install-state probe failed — "
                       "treating install as established (safe seed)",
                       exc_info=True)
        established = True

    value = False if established else True
    psr.set_setting(key, value, db=db, actor_user_id=None)
    logger.info(
        "GSEC R-012: seeded mesh.acl_enforced=%s (%s install) — %s",
        value, "established" if established else "fresh",
        "tailnet stays permissive until the lead previews "
        "(GET /platform/mesh-acl/preview) and flips the setting"
        if value is False else
        "default-deny enforcement active from first tagged-mesh push")
    return [SeedResult(key=key, value=value, mode=mode,
                       applied=True, reason="seeded")]


def preview_seed_platform_settings(db, mode: str) -> List[Dict[str, Any]]:
    """Dry-run: every REGISTERED setting's current value/source, plus what
    `seed_platform_settings(db, mode)` would do to it — without writing
    anything. Covers every key `platform_settings.list_settings` returns, so
    it always reflects whatever GS.PSR.6 has registered so far.
    """
    mode = _normalize_mode(mode)
    out: List[Dict[str, Any]] = []

    for row in psr.list_settings(db):
        key = row["key"]
        seed_value: Optional[Any] = SEED_VALUES.get(key, {}).get(mode)
        would_apply = seed_value is not None and row["source"] != "db"
        final_value = seed_value if would_apply else row["value"]
        final_source = "seed" if would_apply else row["source"]
        out.append({
            "key": key,
            "mode": mode,
            "current_value": row["value"],
            "current_source": row["source"],
            "seed_value": seed_value,
            "would_apply": would_apply,
            "final_value": final_value,
            "final_source": final_source,
            "group": row["group"],
            "description": row["description"],
        })

    return out
